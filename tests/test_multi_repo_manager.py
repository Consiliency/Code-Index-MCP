"""
Tests for Multi-Repository Manager

These tests verify the functionality of the multi-repository search system
including registration, cross-repository search, and health monitoring.
"""

import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from mcp_server.storage.multi_repo_manager import (
    MultiRepositoryManager,
    RepositoryInfo,
    get_multi_repo_manager,
)
from mcp_server.storage.repository_registry import RepositoryRegistry
from mcp_server.storage.sqlite_store import SQLiteStore


class TestMultiRepositoryManager:
    """Test multi-repository management functionality."""

    @pytest.fixture
    def temp_registry(self):
        """Create temporary registry path."""
        temp_dir = tempfile.mkdtemp()
        registry_path = Path(temp_dir) / "test_registry.json"
        yield registry_path
        shutil.rmtree(temp_dir)

    @pytest.fixture
    def manager(self, temp_registry):
        """Create a test manager instance."""
        return MultiRepositoryManager(central_index_path=temp_registry, max_workers=2)

    @pytest.fixture
    def mock_repo_info(self):
        """Create mock repository info."""
        return RepositoryInfo(
            repository_id="test_repo_123",
            name="test-repo",
            path=Path("/test/repo"),
            index_path=Path("/test/repo/.mcp-index/index.db"),
            language_stats={"python": 50, "javascript": 30},
            total_files=80,
            total_symbols=500,
            indexed_at=datetime.now(),
            active=True,
            priority=1,
        )

    @staticmethod
    def _create_test_index(
        repo_dir: Path, repository_id: str, language: str, symbol_name: str, priority: int = 0
    ) -> RepositoryInfo:
        """Create a minimal SQLite index for testing."""
        index_dir = repo_dir / ".mcp-index"
        index_dir.mkdir(parents=True, exist_ok=True)
        index_path = index_dir / "index.db"

        store = SQLiteStore(str(index_path))

        with store._get_connection() as conn:
            repo_row_id = conn.execute(
                "INSERT INTO repositories (path, name, metadata) VALUES (?, ?, ?)",
                (str(repo_dir), repo_dir.name, "{}"),
            ).lastrowid

            file_name = f"{symbol_name}.{'py' if language == 'python' else 'js'}"
            file_path = repo_dir / file_name

            file_cursor = conn.execute(
                """
                INSERT INTO files
                    (repository_id, path, relative_path, language, size, hash, content_hash,
                     last_modified, indexed_at, metadata, is_deleted)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, FALSE)
                """,
                (
                    repo_row_id,
                    str(file_path),
                    file_name,
                    language,
                    0,
                    f"{symbol_name}_hash",
                    f"{symbol_name}_content",
                    "{}",
                ),
            )
            file_id = file_cursor.lastrowid

            conn.execute(
                """
                INSERT INTO symbols
                    (file_id, name, kind, line_start, line_end, column_start, column_end,
                     signature, documentation, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (file_id, symbol_name, "function", 5, 5, 0, 0, "", "", "{}"),
            )

        return RepositoryInfo(
            repository_id=repository_id,
            name=repo_dir.name,
            path=repo_dir,
            index_path=index_path,
            language_stats={language: 1},
            total_files=1,
            total_symbols=1,
            indexed_at=datetime.now(),
            active=True,
            priority=priority,
            current_commit="fixture",
            last_indexed_commit="fixture",
            current_branch="main",
            tracked_branch="main",
            last_indexed_branch="main",
        )

    def test_initialization(self, manager, temp_registry):
        """Test manager initialization."""
        assert manager.central_index_path == temp_registry
        assert manager.max_workers == 2
        assert isinstance(manager.registry, RepositoryRegistry)
        assert len(manager._store_registry._cache) == 0

    def test_default_registry_path(self, monkeypatch):
        """Test default registry path generation."""
        monkeypatch.delenv("MCP_REPO_REGISTRY", raising=False)
        manager = MultiRepositoryManager()
        expected = Path.home() / ".mcp" / "repository_registry.json"
        assert manager.central_index_path == expected

    def test_repository_registration(self, manager):
        """Test registering a repository."""
        # Create mock repository with index
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir)
            index_dir = repo_path / ".mcp-index"
            index_dir.mkdir()
            index_path = index_dir / "code_index.db"

            # Create minimal SQLite index
            conn = sqlite3.connect(str(index_path))
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE files (
                    id INTEGER PRIMARY KEY,
                    path TEXT,
                    language TEXT,
                    is_deleted INTEGER DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE TABLE symbols (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    is_deleted INTEGER DEFAULT 0
                )
            """)
            cursor.execute("INSERT INTO files (path, language) VALUES ('test.py', 'python')")
            cursor.execute("INSERT INTO symbols (name) VALUES ('test_function')")
            conn.commit()
            conn.close()

            # Register repository
            with patch.object(manager, "_generate_repository_id", return_value="test_id_123"):
                repo_info = manager.register_repository(
                    repository_path=repo_path, name="Test Repo", priority=2
                )

            assert repo_info.repository_id == "test_id_123"
            assert repo_info.name == "Test Repo"
            assert repo_info.path == repo_path
            assert repo_info.priority == 2
            assert repo_info.total_files == 1
            assert repo_info.total_symbols == 1
            assert "python" in repo_info.language_stats

    def test_repository_listing(self, manager, mock_repo_info):
        """Test listing repositories."""
        # Add repositories to registry
        manager.registry.register(mock_repo_info)

        # Create another repo with lower priority
        repo2 = RepositoryInfo(
            repository_id="test_repo_456",
            name="low-priority-repo",
            path=Path("/test/repo2"),
            index_path=Path("/test/repo2/.mcp-index/index.db"),
            language_stats={"go": 20},
            total_files=20,
            total_symbols=100,
            indexed_at=datetime.now(),
            active=True,
            priority=0,
        )
        manager.registry.register(repo2)

        # List all repositories
        repos = manager.list_repositories()

        assert len(repos) == 2
        # Should be sorted by priority descending
        assert repos[0].priority == 1
        assert repos[1].priority == 0

        # Test active_only filter
        manager.registry.update_status("test_repo_456", active=False)
        active_repos = manager.list_repositories(active_only=True)
        assert len(active_repos) == 1
        assert active_repos[0].repository_id == "test_repo_123"

    @pytest.mark.asyncio
    async def test_symbol_search(self, manager, mock_repo_info):
        """Test cross-repository symbol search."""
        # Register repository
        manager.registry.register(mock_repo_info)

        # Mock the search method
        mock_results = [
            {
                "name": "test_function",
                "type": "function",
                "language": "python",
                "file_path": "test.py",
                "line": 10,
            }
        ]

        with (
            patch.object(manager, "_get_connection") as mock_get_conn,
            patch.object(manager, "_query_ready", return_value=True),
        ):
            mock_store = Mock()
            mock_store.search_symbols.return_value = mock_results
            mock_get_conn.return_value = mock_store

            # Perform search
            results = await manager.search_symbol(query="test", language="python", limit=10)

        assert len(results) == 1
        result = results[0]
        assert result.repository_id == "test_repo_123"
        assert result.repository_name == "test-repo"
        assert len(result.results) == 1
        assert result.results[0]["symbol"] == "test_function"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_symbol_search_with_registered_indexes(self, manager):
        """Search across multiple real indexes and normalize results."""
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            repo_a = base_dir / "repo_a"
            repo_b = base_dir / "repo_b"

            repo_a_info = self._create_test_index(
                repo_a, "repo_a", "python", "alpha_func", priority=0
            )
            repo_b_info = self._create_test_index(
                repo_b, "repo_b", "javascript", "beta_func", priority=1
            )

            manager.registry.register(repo_a_info)
            manager.registry.register(repo_b_info)

            results = await manager.search_symbol(query="func", limit=5)

        assert len(results) == 2
        # Higher priority repo_b should appear first
        assert results[0].repository_id == "repo_b"
        assert results[1].repository_id == "repo_a"

        repo_b_result = results[0].results[0]
        repo_a_result = results[1].results[0]

        assert repo_b_result["symbol"] == "beta_func"
        assert repo_b_result["file"].endswith("beta_func.js")
        assert repo_b_result["type"] == "function"
        assert repo_b_result["line"] == 5

        assert repo_a_result["symbol"] == "alpha_func"
        assert repo_a_result["language"] == "python"
        assert repo_a_result["file"].endswith("alpha_func.py")

    @pytest.mark.asyncio
    async def test_parallel_search(self, manager):
        """Test parallel search across multiple repositories."""
        # Register multiple repositories
        repos = []
        for i in range(3):
            repo = RepositoryInfo(
                repository_id=f"repo_{i}",
                name=f"Repo {i}",
                path=Path(f"/test/repo{i}"),
                index_path=Path(f"/test/repo{i}/.mcp-index/index.db"),
                language_stats={"python": 10},
                total_files=10,
                total_symbols=50,
                indexed_at=datetime.now(),
                priority=i,
            )
            repos.append(repo)
            manager.registry.register(repo)

        # Mock connections and results
        with patch.object(manager, "_get_connection") as mock_get_conn:
            mock_stores = []
            for i in range(3):
                mock_store = Mock()
                mock_store.search_symbols.return_value = [
                    {
                        "name": f"function_{i}",
                        "type": "function",
                        "language": "python",
                        "file_path": f"file{i}.py",
                        "line": i * 10,
                    }
                ]
                mock_stores.append(mock_store)

            mock_get_conn.side_effect = mock_stores

            # Perform parallel search
            results = await manager.search_symbol("function", limit=10)

        assert len(results) == 3
        # Results should be sorted by priority descending
        assert results[0].repository_id == "repo_2"
        assert results[1].repository_id == "repo_1"
        assert results[2].repository_id == "repo_0"

    def test_health_check(self, manager, mock_repo_info):
        """Test repository health check."""
        # Register repository
        manager.registry.register(mock_repo_info)

        # Mock path existence
        with patch.object(Path, "exists") as mock_exists:
            # First call for index_path, second for repo path
            mock_exists.side_effect = [True, True]

            with patch.object(manager, "_get_connection", return_value=Mock()):
                health = manager.health_check()

        assert health["healthy"] == 1
        assert health["unhealthy"] == 0
        assert len(health["repositories"]) == 1

        repo_health = health["repositories"][0]
        assert repo_health["repository_id"] == "test_repo_123"
        assert repo_health["status"] == "healthy"
        assert len(repo_health["issues"]) == 0

    def test_health_check_unhealthy(self, manager, mock_repo_info):
        """Test health check with unhealthy repository."""
        # Register repository
        manager.registry.register(mock_repo_info)

        # Mock missing index file
        with patch.object(Path, "exists", return_value=False):
            health = manager.health_check()

        assert health["healthy"] == 0
        assert health["unhealthy"] == 1

        repo_health = health["repositories"][0]
        assert repo_health["status"] == "unhealthy"
        assert "Index file not found" in repo_health["issues"]
        assert "Repository path not found" in repo_health["issues"]

    def test_statistics(self, manager):
        """Test statistics gathering."""
        # Register multiple repositories
        repos = [
            RepositoryInfo(
                repository_id="repo1",
                name="Python Repo",
                path=Path("/test/repo1"),
                index_path=Path("/test/repo1/.mcp-index/index.db"),
                language_stats={"python": 100, "javascript": 20},
                total_files=120,
                total_symbols=600,
                indexed_at=datetime.now(),
            ),
            RepositoryInfo(
                repository_id="repo2",
                name="JS Repo",
                path=Path("/test/repo2"),
                index_path=Path("/test/repo2/.mcp-index/index.db"),
                language_stats={"javascript": 80, "typescript": 40},
                total_files=120,
                total_symbols=400,
                indexed_at=datetime.now(),
            ),
        ]

        for repo in repos:
            manager.registry.register(repo)

        stats = manager.get_statistics()

        assert stats["repositories"]["total"] == 2
        assert stats["repositories"]["active"] == 2
        assert stats["repositories"]["total_files"] == 240
        assert stats["repositories"]["total_symbols"] == 1000

        assert stats["languages"]["python"] == 100
        assert stats["languages"]["javascript"] == 100  # 20 + 80
        assert stats["languages"]["typescript"] == 40

    def test_connection_caching(self, manager, mock_repo_info):
        """Test connection caching.

        Post-refactor, ``StoreRegistry.get`` eagerly constructs a
        ``ConnectionPool`` which pre-opens ``sqlite3.connect`` handles at
        construction time.  The test's mock index path does not exist on disk,
        so we also stub ``sqlite3.connect`` and ``ConnectionPool`` to prevent
        that pre-open from raising ``OperationalError: unable to open database``.
        """
        repo_path = manager.central_index_path.parent / "connection-cache-repo"
        repo_path.mkdir()
        mock_repo_info.path = repo_path
        mock_repo_info.index_path = repo_path / ".mcp-index" / "index.db"
        manager.registry.register(mock_repo_info)

        with (
            patch("mcp_server.storage.store_registry.SQLiteStore") as mock_store_class,
            patch("mcp_server.storage.store_registry.ConnectionPool") as mock_pool_class,
        ):
            mock_store = Mock()
            mock_store_class.return_value = mock_store
            mock_pool_class.return_value = Mock()

            # First call should create connection
            conn1 = manager._get_connection("test_repo_123")
            assert conn1 == mock_store
            assert "test_repo_123" in manager._store_registry._cache

            # Second call should return cached
            conn2 = manager._get_connection("test_repo_123")
            assert conn2 == conn1

            # Should only create one instance
            mock_store_class.assert_called_once()

    def test_optimize_indexes(self, manager, mock_repo_info):
        """Test index optimization."""
        manager.registry.register(mock_repo_info)

        with patch("sqlite3.connect") as mock_connect:
            mock_conn = Mock()
            mock_cursor = Mock()
            mock_conn.cursor.return_value = mock_cursor
            mock_connect.return_value = mock_conn

            with patch.object(Path, "exists", return_value=True):
                manager.optimize_indexes()

            # Should run VACUUM and ANALYZE
            mock_cursor.execute.assert_any_call("VACUUM")
            mock_cursor.execute.assert_any_call("ANALYZE")
            mock_conn.close.assert_called_once()

    def test_close(self, manager, mock_repo_info):
        """Test manager cleanup."""
        manager.registry.register(mock_repo_info)

        # Add a mock connection directly to the store registry cache
        mock_conn = Mock()
        manager._store_registry._cache["test_repo_123"] = mock_conn

        # Close manager
        manager.close()

        # Should close all connections and clear the cache
        mock_conn.close.assert_called_once()
        assert len(manager._store_registry._cache) == 0

    def test_search_error_handling(self, manager, mock_repo_info):
        """Test error handling during search."""
        manager.registry.register(mock_repo_info)

        # Mock connection failure
        with (
            patch.object(manager, "_get_connection", side_effect=Exception("Connection failed")),
            patch.object(manager, "_query_ready", return_value=True),
        ):
            # Use synchronous version for testing
            result = manager._search_repository("test_repo_123", "test", None, 10)

        # Should return None on connection failure
        assert result is None


class TestRepositoryRegistry:
    """Test repository registry functionality."""

    @pytest.fixture
    def temp_registry_path(self):
        """Create temporary registry path."""
        temp_dir = tempfile.mkdtemp()
        registry_path = Path(temp_dir) / "test_registry.json"
        yield registry_path
        shutil.rmtree(temp_dir)

    @pytest.fixture
    def registry(self, temp_registry_path):
        """Create test registry instance."""
        return RepositoryRegistry(temp_registry_path)

    def test_save_and_load(self, registry, temp_registry_path):
        """Test saving and loading registry."""
        # Create repository info
        repo_info = RepositoryInfo(
            repository_id="test_123",
            name="Test Repo",
            path=Path("/test/repo"),
            index_path=Path("/test/repo/.mcp-index/index.db"),
            language_stats={"python": 50},
            total_files=50,
            total_symbols=200,
            indexed_at=datetime.now(),
        )

        # Register and save
        registry.register(repo_info)

        # Create new registry instance to test loading
        new_registry = RepositoryRegistry(temp_registry_path)

        # Should load the saved repository
        loaded = new_registry.get("test_123")
        assert loaded is not None
        assert loaded.name == "Test Repo"
        assert loaded.language_stats["python"] == 50

    def test_find_by_path(self, registry):
        """Test finding repository by path."""
        repo_path = Path("/test/my-repo")

        repo_info = RepositoryInfo(
            repository_id="path_test",
            name="Path Test",
            path=repo_path,
            index_path=repo_path / ".mcp-index/index.db",
            language_stats={},
            total_files=0,
            total_symbols=0,
            indexed_at=datetime.now(),
        )

        registry.register(repo_info)

        # Find by path
        found_id = registry.find_by_path(repo_path)
        assert found_id == "path_test"

        # Non-existent path
        assert registry.find_by_path(Path("/non/existent")) is None

    def test_cleanup(self, registry):
        """Test registry cleanup of invalid entries."""
        # Register repository with non-existent index
        repo_info = RepositoryInfo(
            repository_id="invalid_repo",
            name="Invalid",
            path=Path("/fake/path"),
            index_path=Path("/fake/path/.mcp-index/index.db"),
            language_stats={},
            total_files=0,
            total_symbols=0,
            indexed_at=datetime.now(),
        )

        registry.register(repo_info)

        # Run cleanup
        removed = registry.cleanup()

        assert removed == 1
        assert registry.get("invalid_repo") is None


class TestSL2Delegation:
    """SL-2: MultiRepositoryManager delegates to compute_repo_id."""

    @pytest.fixture
    def temp_registry(self):
        temp_dir = tempfile.mkdtemp()
        registry_path = Path(temp_dir) / "test_registry.json"
        yield registry_path
        shutil.rmtree(temp_dir)

    @pytest.fixture
    def manager(self, temp_registry):
        return MultiRepositoryManager(central_index_path=temp_registry, max_workers=2)

    def test_generate_repository_id_delegates_to_compute_repo_id(self, manager, tmp_path):
        """_generate_repository_id delegates to compute_repo_id."""
        import subprocess as sp

        sp.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
        sp.run(
            ["git", "config", "user.email", "t@t.com"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        sp.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "f.txt").write_text("x")
        sp.run(["git", "add", "f.txt"], cwd=tmp_path, check=True, capture_output=True)
        sp.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

        with patch("mcp_server.storage.multi_repo_manager.compute_repo_id") as mock_compute:
            from mcp_server.storage.repo_identity import RepoIdentity

            mock_compute.return_value = RepoIdentity(
                repo_id="abcd1234abcd1234",
                git_common_dir=tmp_path / ".git",
                source="git_common_dir",
            )
            result = manager._generate_repository_id(tmp_path)

        mock_compute.assert_called_once_with(tmp_path)
        assert result == "abcd1234abcd1234"

    def test_resolve_repo_id_delegates_to_compute_repo_id_for_path(self, tmp_path):
        """resolve_repo_id calls compute_repo_id for filesystem paths."""
        import subprocess as sp

        sp.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
        sp.run(
            ["git", "config", "user.email", "t@t.com"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        sp.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "f.txt").write_text("x")
        sp.run(["git", "add", "f.txt"], cwd=tmp_path, check=True, capture_output=True)
        sp.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

        with patch("mcp_server.storage.multi_repo_manager.compute_repo_id") as mock_compute:
            from mcp_server.storage.repo_identity import RepoIdentity

            mock_compute.return_value = RepoIdentity(
                repo_id="deadbeefdeadbeef",
                git_common_dir=tmp_path / ".git",
                source="git_common_dir",
            )
            result = MultiRepositoryManager.resolve_repo_id(str(tmp_path))

        assert mock_compute.call_count >= 1
        assert result == "deadbeefdeadbeef"

    def test_resolve_repo_id_returns_16hex_id_as_is(self):
        """resolve_repo_id returns a 16-hex id unchanged without calling compute_repo_id."""
        with patch("mcp_server.storage.multi_repo_manager.compute_repo_id") as mock_compute:
            result = MultiRepositoryManager.resolve_repo_id("abcdef0123456789")

        mock_compute.assert_not_called()
        assert result == "abcdef0123456789"


class TestSingletonManager:
    """Test singleton manager functionality."""

    def test_singleton_instance(self, monkeypatch):
        """Test that get_multi_repo_manager returns singleton."""
        monkeypatch.delenv("MCP_REPO_REGISTRY", raising=False)
        # Clear any existing instance
        import mcp_server.storage.multi_repo_manager as module

        module._manager_instance = None

        try:
            manager1 = get_multi_repo_manager()
            manager2 = get_multi_repo_manager()

            assert manager1 is manager2
        finally:
            # Clean up
            module._manager_instance = None
