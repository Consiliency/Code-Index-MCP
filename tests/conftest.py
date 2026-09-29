"""
Pytest configuration and fixtures for Code-Index-MCP tests.

This module provides reusable fixtures for testing all components of the
MCP server including API endpoints, database operations, file system
operations, and plugin functionality.
"""

import asyncio
import os
import socket
from pathlib import Path
from typing import Dict, List
from unittest.mock import MagicMock, Mock

import pytest
from fastapi.testclient import TestClient

from mcp_server.core.repo_context import RepoContext
from mcp_server.dispatcher import EnhancedDispatcher
from mcp_server.dispatcher.protocol import DispatcherProtocol
from mcp_server.dispatcher.simple_dispatcher import SimpleDispatcher as Dispatcher
from mcp_server.storage.multi_repo_manager import RepositoryInfo
from mcp_server.storage.repository_registry import RepositoryRegistry
from mcp_server.storage.store_registry import StoreRegistry

# Provide required security env vars for tests so gateway startup doesn't raise RuntimeError
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-exactly-32chars!")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password-123!")

# Import our modules
gateway_import_error = None
try:
    from mcp_server.gateway import app
except Exception as exc:  # pragma: no cover - optional for non-API tests
    app = None  # type: ignore
    gateway_import_error = exc
from mcp_server.plugin_base import IPlugin, SearchResult, SymbolDef
from mcp_server.plugins.python_plugin.plugin import Plugin as PythonPlugin
from mcp_server.security import AuthManager, SecurityConfig, User, UserRole
from mcp_server.storage.sqlite_store import SQLiteStore
from mcp_server.watcher import FileWatcher


# Performance tracking
@pytest.fixture(scope="session")
def benchmark_results():
    """Fixture to collect benchmark results across all tests."""
    results = {}
    yield results

    # Print summary at the end
    if results:
        print("\n\n=== Performance Benchmark Summary ===")
        for test_name, timings in results.items():
            avg_time = sum(timings) / len(timings)
            print(
                f"{test_name}: avg={avg_time:.3f}s, min={min(timings):.3f}s, max={max(timings):.3f}s"
            )


# Database fixtures
@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Create a temporary database file path."""
    return tmp_path / "test_code_index.db"


@pytest.fixture
def sqlite_store(temp_db_path: Path) -> SQLiteStore:
    """Create a SQLiteStore instance with a temporary database."""
    store = SQLiteStore(str(temp_db_path))
    yield store
    # Cleanup is automatic when tmp_path is cleaned up


@pytest.fixture
def populated_sqlite_store(sqlite_store: SQLiteStore) -> SQLiteStore:
    """Create a SQLiteStore with sample data."""
    # Create a repository
    repo_id = sqlite_store.create_repository("/test/repo", "test-repo", {"type": "git"})

    # Create some files
    file1_id = sqlite_store.store_file(
        repo_id,
        "/test/repo/main.py",
        "main.py",
        language="python",
        size=1024,
        hash="abc123",
    )
    file2_id = sqlite_store.store_file(
        repo_id,
        "/test/repo/utils.py",
        "utils.py",
        language="python",
        size=512,
        hash="def456",
    )

    # Create some symbols
    sqlite_store.store_symbol(
        file1_id,
        "main",
        "function",
        line_start=1,
        line_end=10,
        signature="def main() -> None",
        documentation="Main entry point",
    )
    sqlite_store.store_symbol(
        file1_id,
        "MyClass",
        "class",
        line_start=15,
        line_end=50,
        signature="class MyClass",
        documentation="A sample class",
    )
    sqlite_store.store_symbol(
        file2_id,
        "helper_function",
        "function",
        line_start=5,
        line_end=15,
        signature="def helper_function(x: int) -> str",
    )

    return sqlite_store


# ---------------------------------------------------------------------------
# P2B fixtures (SL-4)
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_ctx(sqlite_store, tmp_path):
    """RepoContext wrapping a live SQLiteStore with a fake repo_id."""
    registry_entry = MagicMock(spec=RepositoryInfo)
    registry_entry.tracked_branch = "main"
    return RepoContext(
        repo_id="test-repo-p2b-0001",
        sqlite_store=sqlite_store,
        workspace_root=tmp_path,
        tracked_branch="main",
        registry_entry=registry_entry,
    )


@pytest.fixture
def store_registry(tmp_path):
    """In-memory StoreRegistry backed by a temporary RepositoryRegistry."""
    registry = RepositoryRegistry(registry_path=tmp_path / "test_registry.json")
    sr = StoreRegistry(registry)
    yield sr
    sr.shutdown()


@pytest.fixture
def repo_resolver(repo_ctx):
    """Minimal RepoResolver that always returns repo_ctx regardless of path."""
    resolver = MagicMock()
    resolver.resolve.return_value = repo_ctx
    return resolver


@pytest.fixture
def dispatcher_factory():
    """Factory that produces an EnhancedDispatcher conforming to DispatcherProtocol."""

    def _factory(plugins=None):
        return EnhancedDispatcher(plugins=plugins or [])

    return _factory


# ---------------------------------------------------------------------------
# File system fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_code_directory(tmp_path: Path) -> Path:
    """Create a temporary directory with sample code files."""
    code_dir = tmp_path / "test_code"
    code_dir.mkdir()

    # Create Python files
    (code_dir / "sample.py").write_text("""
def hello_world():
    '''Say hello to the world.'''
    print("Hello, World!")

class Calculator:
    '''A simple calculator class.'''
    
    def add(self, a: int, b: int) -> int:
        return a + b
    
    def subtract(self, a: int, b: int) -> int:
        return a - b

if __name__ == "__main__":
    hello_world()
""")

    # Create JavaScript file
    (code_dir / "app.js").write_text("""
function greet(name) {
    return `Hello, ${name}!`;
}

class UserManager {
    constructor() {
        this.users = [];
    }
    
    addUser(user) {
        this.users.push(user);
    }
}

module.exports = { greet, UserManager };
""")

    # Create nested directory structure
    (code_dir / "src").mkdir()
    (code_dir / "src" / "utils.py").write_text("""
import os
from typing import List

def process_files(paths: List[str]) -> None:
    for path in paths:
        if os.path.exists(path):
            print(f"Processing {path}")

class FileHandler:
    def __init__(self, base_path: str):
        self.base_path = base_path
""")

    return code_dir


@pytest.fixture
def mock_file_system(monkeypatch):
    """Mock file system operations for isolated testing."""
    mock_fs = {}

    def mock_exists(path):
        return str(path) in mock_fs

    def mock_read_text(path):
        if str(path) not in mock_fs:
            raise FileNotFoundError(f"File not found: {path}")
        return mock_fs[str(path)]

    def mock_write_text(path, content):
        mock_fs[str(path)] = content

    # Patch Path methods
    monkeypatch.setattr(Path, "exists", mock_exists)
    monkeypatch.setattr(Path, "read_text", mock_read_text)
    monkeypatch.setattr(Path, "write_text", mock_write_text)

    return mock_fs


# Plugin fixtures
@pytest.fixture
def mock_plugin() -> Mock:
    """Create a mock plugin for testing dispatcher."""
    plugin = Mock(spec=IPlugin)
    plugin.lang = "mock"
    plugin.supports.return_value = True
    plugin.indexFile.return_value = {
        "symbols": [
            {
                "name": "test_function",
                "kind": "function",
                "line": 1,
                "signature": "def test_function()",
            }
        ]
    }
    plugin.getDefinition.return_value = SymbolDef(
        name="test_function",
        kind="function",
        path="/test/file.py",
        line=1,
        signature="def test_function()",
    )
    plugin.search.return_value = [
        SearchResult(name="test_function", kind="function", path="/test/file.py", score=1.0)
    ]
    return plugin


@pytest.fixture
def python_plugin(sqlite_store: SQLiteStore) -> PythonPlugin:
    """Create a Python plugin instance with SQLite store."""
    return PythonPlugin(sqlite_store=sqlite_store, preindex=False)


@pytest.fixture
def dispatcher_with_plugins(python_plugin: PythonPlugin) -> Dispatcher:
    """Create a dispatcher with real plugins."""
    return EnhancedDispatcher(plugins=[python_plugin])


@pytest.fixture
def dispatcher_with_mock(mock_plugin: Mock) -> Dispatcher:
    """Create a dispatcher with mock plugin."""
    return EnhancedDispatcher(plugins=[mock_plugin])


# File watcher fixtures
@pytest.fixture
def file_watcher(temp_code_directory: Path, dispatcher_with_plugins: Dispatcher) -> FileWatcher:
    """Create a file watcher instance."""
    watcher = FileWatcher(temp_code_directory, dispatcher_with_plugins)
    yield watcher
    # Ensure watcher is stopped
    try:
        watcher.stop()
    except Exception:
        pass


# API client fixtures
@pytest.fixture
def test_client(tmp_path: Path, monkeypatch) -> TestClient:
    """Create a test client for the FastAPI app."""
    if app is None:
        pytest.skip(f"FastAPI app unavailable: {gateway_import_error}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("MCP_INDEX_STORAGE_PATH", str(tmp_path / ".indexes"))
    monkeypatch.setenv("MCP_REPO_REGISTRY", str(tmp_path / "repository_registry.json"))
    with TestClient(app) as client:
        auth_mgr = getattr(client.app.state, "auth_manager", None)
        if auth_mgr is None:
            auth_mgr = AuthManager(
                SecurityConfig(
                    jwt_secret_key=os.environ["JWT_SECRET_KEY"],
                    jwt_algorithm="HS256",
                    access_token_expire_minutes=30,
                )
            )
            client.app.state.auth_manager = auth_mgr

        admin_user = asyncio.run(auth_mgr.get_user_by_username("pytest_admin"))
        if admin_user is None:
            admin_user = asyncio.run(
                auth_mgr.create_user(
                    username="pytest_admin",
                    password="PytestAdminPass123!",
                    email="pytest_admin@test.local",
                    role=UserRole.ADMIN,
                )
            )

        token = asyncio.run(auth_mgr.create_access_token(admin_user))
        client.headers.update({"Authorization": f"Bearer {token}"})
        yield client


@pytest.fixture
def test_client_with_dispatcher(
    test_client: TestClient,
    dispatcher_with_plugins: Dispatcher,
    sqlite_store: SQLiteStore,
    monkeypatch,
) -> TestClient:
    """Create a test client with initialized dispatcher."""
    if app is None:
        pytest.skip(f"FastAPI app unavailable: {gateway_import_error}")
    # Patch the global variables in gateway module
    import mcp_server.gateway as gateway

    monkeypatch.setattr(gateway, "dispatcher", dispatcher_with_plugins)
    monkeypatch.setattr(gateway, "sqlite_store", sqlite_store)
    # Clear search-mode globals so tests that mock dispatcher.search use classic mode
    for _attr in (
        "bm25_indexer",
        "hybrid_search",
        "fuzzy_indexer",
        "semantic_indexer",
        "query_cache",
    ):
        if hasattr(gateway, _attr):
            monkeypatch.setattr(gateway, _attr, None)

    # Also set in app.state
    test_client.app.state.dispatcher = dispatcher_with_plugins
    test_client.app.state.sqlite_store = sqlite_store

    return test_client


# Async fixtures
@pytest.fixture
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# Sample data fixtures
@pytest.fixture
def sample_symbol_def() -> SymbolDef:
    """Sample SymbolDef for testing."""
    return SymbolDef(
        symbol="sample_function",
        kind="function",
        language="python",
        signature="def sample_function(x: int) -> str",
        doc="A sample function for testing",
        defined_in="/test/sample.py",
        start_line=10,
        end_line=12,
        span=(10, 12),
    )


@pytest.fixture
def sample_search_results() -> List[SearchResult]:
    """Sample search results for testing."""
    return [
        SearchResult(file="/test/file1.py", start_line=1, end_line=1, snippet="function_one"),
        SearchResult(file="/test/file2.py", start_line=1, end_line=1, snippet="function_two"),
        SearchResult(file="/test/file3.py", start_line=1, end_line=1, snippet="ClassOne"),
    ]


# Environment fixtures
@pytest.fixture(autouse=True)
def test_environment(monkeypatch, tmp_path):
    """Set up test environment variables."""
    monkeypatch.setenv("MCP_TEST_MODE", "1")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("MCP_SKIP_PLUGIN_PREINDEX", "true")
    # Prevent tests from loading the local 3.6GB production vector store.
    # The dispatcher skips Qdrant init when QDRANT_PATH doesn't exist.
    monkeypatch.setenv("QDRANT_PATH", str(tmp_path / "qdrant-not-created"))
    monkeypatch.setenv("SEMANTIC_SEARCH_ENABLED", "false")
    # Every test owns its registry/storage, including callers that omit explicit paths.
    monkeypatch.setenv("MCP_REPO_REGISTRY", str(tmp_path / "isolated-registry.json"))
    monkeypatch.setenv("MCP_INDEX_STORAGE_PATH", str(tmp_path / "isolated-indexes"))
    monkeypatch.delenv("MCP_WORKSPACE_ROOT", raising=False)


@pytest.fixture(autouse=True)
def block_unmarked_network(request, monkeypatch):
    """Fail tests that open TCP connections without an explicit marker."""
    if request.node.get_closest_marker("requires_network") is not None:
        return

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def deny_connect(sock, address):
        if sock.family == socket.AF_UNIX:
            return original_connect(sock, address)
        raise OSError(f"Unmarked test attempted a network connection to {address!r}")

    def deny_connect_ex(sock, address):
        if sock.family == socket.AF_UNIX:
            return original_connect_ex(sock, address)
        raise OSError(f"Unmarked test attempted a network connection to {address!r}")

    def deny_create_connection(address, *args, **kwargs):
        raise OSError(f"Unmarked test attempted a network connection to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", deny_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_connect_ex)
    monkeypatch.setattr(socket, "create_connection", deny_create_connection)


# CWD fixtures
@pytest.fixture(autouse=True)
def restore_cwd():
    """Restore working directory after each test to prevent CWD leakage between tests."""
    original = os.getcwd()
    yield
    os.chdir(original)


# Cleanup fixtures
@pytest.fixture(autouse=True)
def cleanup_logs(tmp_path):
    """Ensure logs are written to temp directory during tests."""
    log_file = tmp_path / "test.log"
    os.environ["MCP_LOG_FILE"] = str(log_file)
    yield
    # Cleanup happens automatically with tmp_path


# Helper utilities
class TestDataBuilder:
    """Helper class to build test data."""

    @staticmethod
    def create_python_file(path: Path, content: str = None) -> Path:
        """Create a Python file with optional content."""
        if content is None:
            content = '''
def test_function():
    """Test function."""
    pass

class TestClass:
    """Test class."""
    def method(self):
        pass
'''
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    @staticmethod
    def create_project_structure(base_path: Path) -> Dict[str, Path]:
        """Create a sample project structure."""
        files = {}

        # Create main module
        files["main"] = TestDataBuilder.create_python_file(
            base_path / "main.py",
            '''
import sys
from src.utils import helper

def main():
    """Main entry point."""
    helper()
    return 0

if __name__ == "__main__":
    sys.exit(main())
''',
        )

        # Create src directory
        src_dir = base_path / "src"
        src_dir.mkdir()

        files["utils"] = TestDataBuilder.create_python_file(
            src_dir / "utils.py",
            '''
def helper():
    """Helper function."""
    print("Helper called")

class Config:
    """Configuration class."""
    def __init__(self):
        self.debug = True
''',
        )

        # Create tests
        test_dir = base_path / "tests"
        test_dir.mkdir()

        files["test_main"] = TestDataBuilder.create_python_file(
            test_dir / "test_main.py",
            '''
import pytest
from main import main

def test_main():
    """Test main function."""
    assert main() == 0
''',
        )

        return files


@pytest.fixture
def test_data_builder():
    """Provide test data builder to tests."""
    return TestDataBuilder


@pytest.fixture
def temp_workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace directory."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def create_test_index():
    """Return a callable that creates a minimal valid SQLite index at a given path."""
    import sqlite3

    def _create(db_path: Path) -> Path:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS files " "(id INTEGER PRIMARY KEY, path TEXT, language TEXT)"
        )
        conn.commit()
        conn.close()
        return db_path

    return _create


# Performance helpers
import time
from contextlib import contextmanager


@contextmanager
def measure_time(test_name: str, benchmark_results: dict):
    """Context manager to measure test execution time."""
    start = time.time()
    yield
    elapsed = time.time() - start

    if test_name not in benchmark_results:
        benchmark_results[test_name] = []
    benchmark_results[test_name].append(elapsed)


def pytest_collection_modifyitems(config, items):
    """Skip requires_gh_auth tests unless RUN_GH_AUTHENTICATED_TESTS=1."""
    if os.getenv("RUN_GH_AUTHENTICATED_TESTS") == "1":
        return
    skip_mark = pytest.mark.skip(reason="set RUN_GH_AUTHENTICATED_TESTS=1 to run")
    for item in items:
        if list(item.iter_markers(name="requires_gh_auth")):
            item.add_marker(skip_mark)
