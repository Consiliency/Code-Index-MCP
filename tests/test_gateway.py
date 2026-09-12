"""
Comprehensive tests for the MCP Server API Gateway.

This module tests all API endpoints including:
- Symbol lookup
- Search functionality (fuzzy and semantic)
- Server status
- Plugin information
- Reindexing operations
"""

import os
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import ANY, Mock, patch

import pytest
from fastapi.testclient import TestClient

from mcp_server.client_types import ClientSearchMatch, ClientSearchResult
from mcp_server.dispatcher.simple_dispatcher import SimpleDispatcher
from mcp_server.plugin_base import SearchResult, SymbolDef
from mcp_server.security import AuthManager, SecurityConfig, UserRole
from mcp_server.storage.sqlite_store import SQLiteStore

# ---------------------------------------------------------------------------
# Local fixture overrides for SL-1 compatibility
#
# conftest.py creates SimpleDispatcher([python_plugin]) which fails after SL-1
# removed the positional-plugin-list constructor arg. These local overrides
# shadow the conftest fixtures for test_gateway.py only.
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatcher_with_plugins(sqlite_store: SQLiteStore) -> SimpleDispatcher:
    """Local override: create a SimpleDispatcher compatible with the SL-1 API."""
    return SimpleDispatcher()


@pytest.fixture
def test_client(monkeypatch):
    """Local override: authenticated client without running gateway startup hooks."""
    import asyncio

    import mcp_server.gateway as gateway

    original_startup = list(gateway.app.router.on_startup)
    original_shutdown = list(gateway.app.router.on_shutdown)
    gateway.app.router.on_startup = []
    gateway.app.router.on_shutdown = []

    auth_manager = getattr(gateway.app.state, "auth_manager", None)
    if auth_manager is None:
        auth_manager = AuthManager(
            SecurityConfig(
                jwt_secret_key=os.environ["JWT_SECRET_KEY"],
                jwt_algorithm="HS256",
                access_token_expire_minutes=30,
            )
        )
        gateway.app.state.auth_manager = auth_manager
    auth_manager.rate_limiter.max_requests = 10000

    admin_user = asyncio.run(auth_manager.get_user_by_username("pytest_gateway_admin"))
    if admin_user is None:
        admin_user = asyncio.run(
            auth_manager.create_user(
                username="pytest_gateway_admin",
                password="PytestGatewayAdmin123!",
                email="pytest_gateway_admin@test.local",
                role=UserRole.ADMIN,
            )
        )
    token = asyncio.run(auth_manager.create_access_token(admin_user))

    client = TestClient(gateway.app, headers={"Authorization": f"Bearer {token}"})
    try:
        yield client
    finally:
        client.close()
        gateway.app.router.on_startup = original_startup
        gateway.app.router.on_shutdown = original_shutdown


@pytest.fixture
def startup_test_client():
    """Client that keeps the gateway startup/shutdown lifecycle intact."""
    import mcp_server.gateway as gateway

    client = TestClient(gateway.app)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def test_client_with_dispatcher(
    test_client,
    dispatcher_with_plugins: SimpleDispatcher,
    sqlite_store: SQLiteStore,
    monkeypatch,
):
    """Local override: test client with initialized dispatcher (SL-1 compatible)."""
    import mcp_server.gateway as gateway

    if test_client is None:
        pytest.skip("FastAPI app unavailable")

    monkeypatch.setattr(gateway, "dispatcher", dispatcher_with_plugins)
    monkeypatch.setattr(gateway, "sqlite_store", sqlite_store)
    monkeypatch.setattr(gateway, "repo_resolver", None)
    for _attr in (
        "bm25_indexer",
        "hybrid_search",
        "fuzzy_indexer",
        "semantic_indexer",
        "query_cache",
    ):
        if hasattr(gateway, _attr):
            monkeypatch.setattr(gateway, _attr, None)

    test_client.app.state.dispatcher = dispatcher_with_plugins
    test_client.app.state.sqlite_store = sqlite_store
    return test_client


@contextmanager
def measure_time(test_name: str, benchmark_results: dict):
    """Context manager to measure test execution time."""
    start = time.time()
    yield
    elapsed = time.time() - start
    if test_name not in benchmark_results:
        benchmark_results[test_name] = []
    benchmark_results[test_name].append(elapsed)


class TestGatewayStartupShutdown:
    """Test server startup and shutdown events."""

    @patch("mcp_server.gateway.format_preflight_report")
    @patch("mcp_server.gateway.run_startup_preflight")
    @patch("mcp_server.gateway.SQLiteStore")
    @patch("mcp_server.gateway.EnhancedDispatcher")
    @patch("mcp_server.gateway.MultiRepositoryWatcher")
    def test_startup_success(
        self,
        mock_watcher,
        mock_dispatcher,
        mock_store,
        mock_run_preflight,
        mock_format_preflight,
        startup_test_client,
    ):
        """Test successful server startup."""
        mock_run_preflight.return_value = type(
            "PreflightResult", (), {"status": "warning", "checks": []}
        )()
        mock_format_preflight.return_value = ["warning"]
        # Trigger startup event
        with startup_test_client:
            # Verify components were initialized
            mock_run_preflight.assert_called_once()
            mock_store.assert_called_once()
            mock_dispatcher.assert_called_once()
            mock_watcher.assert_called_once()

    @patch("mcp_server.gateway.SQLiteStore")
    def test_startup_failure(self, mock_store, startup_test_client):
        """Test server startup with initialization failure."""
        mock_store.side_effect = Exception("Database error")

        with pytest.raises(Exception, match="Database error"):
            with startup_test_client:
                pass

    @patch("mcp_server.gateway.EnhancedDispatcher")
    @patch("mcp_server.gateway.MultiRepositoryWatcher")
    def test_shutdown_stops_watcher(
        self, mock_watcher_class, mock_dispatcher_class, startup_test_client, monkeypatch
    ):
        """Test that shutdown stops the multi-repo watcher."""
        import mcp_server.gateway as gateway

        mock_watcher = Mock()
        mock_watcher_class.return_value = mock_watcher
        monkeypatch.setattr(gateway, "dispatcher", SimpleDispatcher())

        # Trigger startup+shutdown via context manager
        with startup_test_client:
            pass

        # stop_watching_all should have been called during shutdown
        mock_watcher.stop_watching_all.assert_called_once()


class TestSymbolEndpoint:
    """Test /symbol endpoint."""

    def test_symbol_lookup_success(self, test_client_with_dispatcher, sample_symbol_def):
        """Test successful symbol lookup."""
        # Configure dispatcher mock
        test_client_with_dispatcher.app.state.dispatcher.lookup = Mock(
            return_value=sample_symbol_def
        )

        response = test_client_with_dispatcher.get("/symbol?symbol=sample_function")

        assert response.status_code == 200
        data = response.json()
        assert data["symbol"] == "sample_function"
        assert data["kind"] == "function"
        assert data["defined_in"] == "/test/sample.py"
        assert data["start_line"] == 10

    def test_symbol_lookup_not_found(self, test_client_with_dispatcher):
        """Test symbol lookup when symbol doesn't exist."""
        test_client_with_dispatcher.app.state.dispatcher.lookup = Mock(return_value=None)

        response = test_client_with_dispatcher.get("/symbol?symbol=nonexistent")

        assert response.status_code == 200
        assert response.json() is None

    def test_symbol_lookup_no_dispatcher(self, test_client, monkeypatch):
        """Test symbol lookup when dispatcher is not initialized."""
        import mcp_server.gateway as gateway

        monkeypatch.setattr(gateway, "dispatcher", None)

        response = test_client.get("/symbol?symbol=test")

        assert response.status_code == 503
        assert "Dispatcher not ready" in response.json()["detail"]

    def test_symbol_lookup_error(self, test_client_with_dispatcher):
        """Test symbol lookup with internal error."""
        test_client_with_dispatcher.app.state.dispatcher.lookup = Mock(
            side_effect=Exception("Lookup error")
        )

        response = test_client_with_dispatcher.get("/symbol?symbol=test")

        assert response.status_code == 500
        assert "Internal error during symbol lookup" in response.json()["detail"]

    @pytest.mark.parametrize(
        "symbol_name",
        [
            "simple_name",
            "CamelCaseName",
            "name_with_underscores",
            "name123",
            "🐍python_emoji",  # Unicode support
        ],
    )
    def test_symbol_lookup_various_names(self, test_client_with_dispatcher, symbol_name):
        """Test symbol lookup with various name formats."""
        mock_symbol = SymbolDef(
            symbol=symbol_name,
            kind="function",
            language="python",
            signature="def f()",
            doc=None,
            defined_in="/test.py",
            start_line=1,
            end_line=1,
            span=(1, 1),
        )
        test_client_with_dispatcher.app.state.dispatcher.lookup = Mock(return_value=mock_symbol)

        response = test_client_with_dispatcher.get(f"/symbol?symbol={symbol_name}")

        assert response.status_code == 200
        assert response.json()["symbol"] == symbol_name


class TestSearchEndpoint:
    """Test /search endpoint."""

    def test_search_basic(self, test_client_with_dispatcher, sample_search_results):
        """Test basic search functionality."""
        test_client_with_dispatcher.app.state.dispatcher.search = Mock(
            return_value=sample_search_results
        )

        response = test_client_with_dispatcher.get("/search?q=function")

        assert response.status_code == 200
        results = response.json()
        assert len(results) == 3
        assert results[0]["file"] == "/test/file1.py"

    def test_search_semantic(self, test_client_with_dispatcher):
        """Test semantic search."""
        with patch(
            "mcp_server.gateway.execute_search_service",
            return_value=ClientSearchResult(query="test"),
        ) as mock_execute:
            response = test_client_with_dispatcher.get("/search?q=test&semantic=true")

        assert response.status_code == 200
        assert mock_execute.call_args.kwargs["options"].semantic is True
        assert mock_execute.call_args.kwargs["options"].limit == 20

    def test_search_auto_prefers_semantic_path_when_requested(
        self, test_client_with_dispatcher, monkeypatch
    ):
        """Auto mode should prefer semantic path instead of hybrid when semantic=true."""
        import mcp_server.gateway as gateway

        class FakeHybrid:
            async def search(self, query, filters, limit):
                return [{"file": "htmlcov/function_index.html", "line": 1, "snippet": "bad"}]

        gateway.hybrid_search = FakeHybrid()
        gateway.query_cache = None

        with patch(
            "mcp_server.gateway.execute_search_service",
            return_value=ClientSearchResult(
                query="test",
                results=(
                    ClientSearchMatch(
                        file="mcp_server/gateway.py",
                        line=1,
                        snippet="gateway",
                    ),
                ),
            ),
        ) as mock_execute:
            response = test_client_with_dispatcher.get("/search?q=test&semantic=true")

        assert response.status_code == 200
        payload = response.json()
        assert payload[0]["file"] == "mcp_server/gateway.py"
        assert mock_execute.call_args.kwargs["options"].semantic is True

    def test_search_normalizes_internal_result_shapes(self, test_client_with_dispatcher):
        """Search should normalize internal file_path/filepath payloads to API schema."""
        with patch(
            "mcp_server.gateway.execute_search_service",
            return_value=ClientSearchResult(
                query="test",
                results=(
                    ClientSearchMatch(file="a.py", line=1, snippet="hello"),
                    ClientSearchMatch(file="b.py", line=4, snippet="world"),
                ),
            ),
        ):
            response = test_client_with_dispatcher.get("/search?q=test&semantic=true&mode=semantic")

        assert response.status_code == 200
        payload = response.json()
        assert payload[0]["file"] == "a.py"
        assert payload[0].get("line", payload[0].get("start_line")) == 1
        assert payload[1]["file"] == "b.py"
        assert payload[1].get("line", payload[1].get("start_line")) == 4

    def test_search_with_limit(self, test_client_with_dispatcher):
        """Test search with custom limit."""
        with patch(
            "mcp_server.gateway.execute_search_service",
            return_value=ClientSearchResult(query="test"),
        ) as mock_execute:
            response = test_client_with_dispatcher.get("/search?q=test&limit=50")

        assert response.status_code == 200
        assert mock_execute.call_args.kwargs["options"].limit == 50

    def test_search_accepts_friction_query_params(self, test_client_with_dispatcher):
        with patch(
            "mcp_server.gateway.execute_search_service",
            return_value=ClientSearchResult(
                query="test",
                results=(
                    ClientSearchMatch(
                        file="/repo/file.py",
                        line=2,
                        snippet="TODO",
                        source_metadata={
                            "schema_version": "search_source_metadata.v1",
                            "records": [
                                {
                                    "source_type": "friction",
                                    "category": "todo",
                                    "line": 2,
                                    "description": "todo",
                                    "pattern": "TODO",
                                }
                            ],
                        },
                    ),
                ),
            ),
        ) as execute_search_service:
            response = test_client_with_dispatcher.get(
                "/search?q=test&source_type=friction&friction_categories=todo&include_source_metadata=true"
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload[0]["source_metadata"]["records"][0]["category"] == "todo"
        options = execute_search_service.call_args.kwargs["options"]
        assert options.source_type is not None and options.source_type.value == "friction"
        assert options.friction_categories == ("todo",)
        assert options.include_source_metadata is True

    def test_search_cache_key_includes_source_filters(
        self, test_client_with_dispatcher, monkeypatch
    ):
        import mcp_server.gateway as gateway

        class FakeConfig:
            enabled = True

        class FakeQueryCache:
            config = FakeConfig()

            def __init__(self):
                self.get_calls = []
                self.cache_calls = []

            async def get_cached_result(self, query_type, **params):
                self.get_calls.append((query_type, params))
                return None

            async def cache_result(self, query_type, result, **params):
                self.cache_calls.append((query_type, params))
                return True

        fake_cache = FakeQueryCache()
        monkeypatch.setattr(gateway, "query_cache", fake_cache)

        with patch(
            "mcp_server.gateway.execute_search_service",
            return_value=ClientSearchResult(
                query="test",
                results=(
                    ClientSearchMatch(
                        file="/repo/file.py",
                        line=2,
                        snippet="TODO",
                    ),
                ),
            ),
        ):
            response = test_client_with_dispatcher.get(
                "/search?q=test&source_type=friction&friction_categories=todo&include_source_metadata=true"
            )

        assert response.status_code == 200
        for _, params in fake_cache.get_calls + fake_cache.cache_calls:
            assert params["repo_id"] == "default"
            assert params["source_type"] == "friction"
            assert params["friction_categories"] == ("todo",)
            assert params["history_labels"] == ()
            assert params["history_repos"] == ()
            assert params["include_source_metadata"] is True

    def test_search_accepts_history_query_params(self, test_client_with_dispatcher):
        with patch(
            "mcp_server.gateway.execute_search_service",
            return_value=ClientSearchResult(
                query="test",
                results=(
                    ClientSearchMatch(
                        file="/repo/.mcp-index/history/owner/repo/issues/1.md",
                        line=1,
                        snippet="Reflection issue",
                        source_metadata={
                            "schema_version": "search_source_metadata.v1",
                            "records": [
                                {
                                    "source_type": "history",
                                    "type": "reflection",
                                    "repo": "owner/repo",
                                    "number": 1,
                                    "title": "Reflection issue",
                                    "labels": ["reflection"],
                                    "state": "closed",
                                    "created_at": "2026-07-01T00:00:00Z",
                                    "updated_at": "2026-07-02T00:00:00Z",
                                    "url": "https://github.com/owner/repo/issues/1",
                                    "summary": "Reflection issue",
                                    "learnings": [],
                                }
                            ],
                        },
                    ),
                ),
            ),
        ) as execute_search_service:
            response = test_client_with_dispatcher.get(
                "/search?q=test&source_type=history&history_labels=reflection&history_repos=owner/repo&include_source_metadata=true"
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload[0]["source_metadata"]["records"][0]["repo"] == "owner/repo"
        options = execute_search_service.call_args.kwargs["options"]
        assert options.source_type is not None and options.source_type.value == "history"
        assert options.history_labels == ("reflection",)
        assert options.history_repos == ("owner/repo",)
        assert options.include_source_metadata is True

    def test_search_rejects_unknown_friction_category(self, test_client_with_dispatcher):
        response = test_client_with_dispatcher.get("/search?q=test&friction_categories=bad")

        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "Invalid friction categories"

    def test_search_empty_query(self, test_client_with_dispatcher):
        """Test search with empty query."""
        test_client_with_dispatcher.app.state.dispatcher.search = Mock(return_value=[])

        response = test_client_with_dispatcher.get("/search?q=")

        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "Invalid query"

    def test_search_no_dispatcher(self, test_client, monkeypatch):
        """Test search when dispatcher is not initialized."""
        import mcp_server.gateway as gateway

        monkeypatch.setattr(gateway, "dispatcher", None)
        for _attr in ("bm25_indexer", "hybrid_search", "fuzzy_indexer"):
            if hasattr(gateway, _attr):
                monkeypatch.setattr(gateway, _attr, None)

        response = test_client.get("/search?q=test&mode=classic")

        assert response.status_code == 503
        detail = response.json()["detail"]
        assert "not ready" in detail or "not available" in detail

    def test_search_error(self, test_client_with_dispatcher):
        """Test search with internal error."""
        test_client_with_dispatcher.app.state.dispatcher.search = Mock(
            side_effect=Exception("Search error")
        )

        response = test_client_with_dispatcher.get("/search?q=test")

        assert response.status_code == 500
        assert "Internal error during search" in response.json()["detail"]

    @pytest.mark.parametrize(
        "query,expected_results",
        [
            ("a", 5),
            ("test function", 10),
            ("very long query " * 10, 2),
        ],
    )
    def test_search_various_queries(self, test_client_with_dispatcher, query, expected_results):
        """Test search with various query types."""
        results = [
            SearchResult(file=f"/file{i}.py", start_line=1, end_line=1, snippet=f"result_{i}")
            for i in range(expected_results)
        ]
        test_client_with_dispatcher.app.state.dispatcher.search = Mock(return_value=results)

        response = test_client_with_dispatcher.get(f"/search?q={query}")

        assert response.status_code == 200
        assert len(response.json()) == expected_results

    def test_search_various_queries_rejects_empty_query(self, test_client_with_dispatcher):
        test_client_with_dispatcher.app.state.dispatcher.search = Mock(return_value=[])

        response = test_client_with_dispatcher.get("/search?q=")

        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "Invalid query"


class TestStatusEndpoint:
    """Test /status endpoint."""

    def test_status_operational(self, test_client_with_dispatcher, populated_sqlite_store):
        """Test status endpoint when server is operational."""
        dispatcher = test_client_with_dispatcher.app.state.dispatcher
        # Mock dispatcher statistics and plugin list via public Protocol methods
        dispatcher.get_statistics = Mock(return_value={"total": 10, "by_language": {"python": 10}})
        dispatcher.plugins = Mock(return_value=[Mock()])  # 1 plugin
        test_client_with_dispatcher.app.state.sqlite_store = populated_sqlite_store

        response = test_client_with_dispatcher.get("/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "operational"
        assert data["plugins"] == 1
        assert data["indexed_files"]["total"] == 10
        assert "database" in data
        assert data["version"] == "0.1.0"

    def test_status_no_dispatcher(self, test_client, monkeypatch):
        """Test status when dispatcher is not initialized."""
        import mcp_server.gateway as gateway

        monkeypatch.setattr(gateway, "dispatcher", None)

        response = test_client.get("/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"
        assert data["message"] == "Dispatcher not initialized"

    def test_status_with_error(self, test_client_with_dispatcher):
        """Test status endpoint with internal error."""
        test_client_with_dispatcher.app.state.dispatcher.get_statistics = Mock(
            side_effect=Exception("Stats error")
        )

        response = test_client_with_dispatcher.get("/status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "error"
        assert "Stats error" in data["message"]

    def test_status_plugin_statistics(self, test_client_with_dispatcher):
        """Test status reports plugin count via plugins() Protocol method."""
        mock_plugin = Mock()
        mock_plugin.get_indexed_count.return_value = 5
        mock_plugin.language = "python"
        dispatcher = test_client_with_dispatcher.app.state.dispatcher
        # Return two plugins from the public Protocol method
        dispatcher.plugins = Mock(return_value=[mock_plugin, mock_plugin])
        # get_statistics returns basic data; we verify plugins count comes from plugins()
        dispatcher.get_statistics = Mock(return_value={"total": 0, "by_language": {}})

        response = test_client_with_dispatcher.get("/status")

        assert response.status_code == 200
        data = response.json()
        # plugins count comes from plugins() Protocol method
        assert data["plugins"] == 2
        assert "indexed_files" in data


class TestPluginsEndpoint:
    """Test /plugins endpoint."""

    def test_plugins_list(self, test_client_with_dispatcher, monkeypatch):
        """Test listing loaded plugins."""
        import mcp_server.gateway as gateway

        # Build mock plugin_manager using the actual structure expected by the endpoint
        # Note: Mock(name=...) sets the mock's display name, not .name attribute
        mock_py_info = Mock(
            version="1.0", description="", author="", language="python", file_extensions=[".py"]
        )
        mock_py_info.name = "PythonPlugin"
        mock_js_info = Mock(
            version="1.0", description="", author="", language="javascript", file_extensions=[".js"]
        )
        mock_js_info.name = "JavaScriptPlugin"
        mock_pm = Mock()
        mock_pm._registry.list_plugins.return_value = [mock_py_info, mock_js_info]
        mock_pm.get_plugin_status.return_value = {}
        monkeypatch.setattr(gateway, "plugin_manager", mock_pm)

        response = test_client_with_dispatcher.get("/plugins")

        assert response.status_code == 200
        plugins = response.json()
        assert len(plugins) == 2
        assert plugins[0]["name"] == "PythonPlugin"
        assert plugins[0]["language"] == "python"
        assert plugins[1]["name"] == "JavaScriptPlugin"
        assert plugins[1]["language"] == "javascript"

    def test_plugins_no_dispatcher(self, test_client, monkeypatch):
        """Test plugins endpoint when plugin manager is not initialized."""
        import mcp_server.gateway as gateway

        monkeypatch.setattr(gateway, "plugin_manager", None)

        response = test_client.get("/plugins")

        assert response.status_code == 503
        assert "not ready" in response.json()["detail"]

    def test_plugins_error(self, test_client_with_dispatcher, monkeypatch):
        """Test plugins endpoint with internal error."""
        import mcp_server.gateway as gateway

        mock_pm = Mock()
        mock_pm._registry.list_plugins.side_effect = Exception("Plugin error")
        monkeypatch.setattr(gateway, "plugin_manager", mock_pm)

        response = test_client_with_dispatcher.get("/plugins")

        assert response.status_code == 500
        assert "Internal error getting plugins" in response.json()["detail"]


class TestReindexEndpoint:
    """Test /reindex endpoint."""

    @pytest.mark.asyncio
    async def test_reindex_all(self, test_client_with_dispatcher, temp_code_directory, monkeypatch):
        """Whole-repository reindex uses the staged Git-aware manager."""
        import mcp_server.gateway as gateway

        ctx = Mock(repo_id="repo-a", workspace_root=temp_code_directory)
        result = Mock(
            action="full_index",
            files_processed=2,
            commit="abc123",
            readiness={"state": "ready"},
        )
        manager = Mock()
        manager.rebuild_repository_index.return_value = result
        monkeypatch.setattr(gateway, "get_repo_ctx", lambda _request: ctx)
        monkeypatch.setattr(gateway, "git_index_manager", manager)

        response = test_client_with_dispatcher.post("/reindex")

        assert response.status_code == 200
        assert response.json()["mode"] == "staged_full"
        manager.rebuild_repository_index.assert_called_once_with("repo-a")

    def test_reindex_nonready_scoped_path_refuses_before_store_resolution(
        self, test_client_with_dispatcher, tmp_path, monkeypatch
    ):
        import mcp_server.gateway as gateway
        from mcp_server.health.repository_readiness import (
            RepositoryReadiness,
            RepositoryReadinessState,
        )

        target = tmp_path / "sample.py"
        target.write_text("pass", encoding="utf-8")
        resolver = Mock()
        resolver.classify.return_value = RepositoryReadiness(
            state=RepositoryReadinessState.MISSING_SCHEMA,
            repository_id="repo-a",
            registered_path=str(tmp_path),
        )
        resolver.resolve.side_effect = AssertionError("must not open the store")
        monkeypatch.setattr(gateway, "repo_resolver", resolver)
        test_client_with_dispatcher.app.state.dispatcher.index_file = Mock()

        response = test_client_with_dispatcher.post(f"/reindex?repository=repo-a&path={target}")

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "full_rebuild_required"
        resolver.resolve.assert_not_called()
        test_client_with_dispatcher.app.state.dispatcher.index_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_reindex_specific_file(
        self, test_client_with_dispatcher, temp_code_directory, monkeypatch
    ):
        """Test reindexing a specific file."""
        test_client_with_dispatcher.app.state.dispatcher.index_file = Mock()
        file_path = temp_code_directory / "sample.py"
        import mcp_server.gateway as gateway

        ctx = Mock(repo_id="repo-a", workspace_root=temp_code_directory)
        monkeypatch.setattr(gateway, "get_repo_ctx", lambda _request: ctx)

        response = test_client_with_dispatcher.post(f"/reindex?path={file_path}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert "Reindexed 1 files" in data["message"]

        test_client_with_dispatcher.app.state.dispatcher.index_file.assert_called_once_with(
            ANY, file_path
        )

    @pytest.mark.asyncio
    async def test_reindex_directory(
        self, test_client_with_dispatcher, temp_code_directory, monkeypatch
    ):
        """Test reindexing a directory."""
        test_client_with_dispatcher.app.state.dispatcher.index_directory = Mock(
            return_value={"indexed_files": 2, "failed_files": 0}
        )
        import mcp_server.gateway as gateway

        ctx = Mock(repo_id="repo-a", workspace_root=temp_code_directory, staging=False)
        monkeypatch.setattr(gateway, "get_repo_ctx", lambda _request: ctx)

        response = test_client_with_dispatcher.post(f"/reindex?path={temp_code_directory}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert "Reindexed" in data["message"]

        test_client_with_dispatcher.app.state.dispatcher.index_directory.assert_called_once_with(
            ctx, temp_code_directory, recursive=True
        )

    @pytest.mark.asyncio
    async def test_reindex_nonexistent_path(self, test_client_with_dispatcher):
        """Test reindexing with non-existent path."""
        response = test_client_with_dispatcher.post("/reindex?path=/nonexistent/path")

        assert response.status_code == 404
        assert "Path not found" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_reindex_no_dispatcher(self, test_client):
        """Test reindex when dispatcher is not initialized."""
        import mcp_server.gateway as gateway

        gateway.dispatcher = None

        response = test_client.post("/reindex")

        assert response.status_code == 503
        assert "Dispatcher not ready" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_reindex_error(self, test_client_with_dispatcher, tmp_path, monkeypatch):
        """Test reindex with internal error."""
        test_client_with_dispatcher.app.state.dispatcher.index_file = Mock(
            side_effect=Exception("Index error")
        )
        # Create a real file so the existence check passes
        test_file = tmp_path / "test.py"
        test_file.write_text("def foo(): pass")
        import mcp_server.gateway as gateway

        ctx = Mock(repo_id="repo-a", workspace_root=tmp_path)
        monkeypatch.setattr(gateway, "get_repo_ctx", lambda _request: ctx)

        response = test_client_with_dispatcher.post(f"/reindex?path={test_file}")

        assert response.status_code == 500
        assert "Reindexing failed" in response.json()["detail"]

    def test_reindex_rejects_path_outside_selected_repo(
        self, test_client_with_dispatcher, tmp_path, monkeypatch
    ):
        import mcp_server.gateway as gateway

        repo_root = tmp_path / "repo"
        sibling = tmp_path / "sibling.py"
        repo_root.mkdir()
        sibling.write_text("pass")
        ctx = Mock(repo_id="repo-a", workspace_root=repo_root)
        monkeypatch.setattr(gateway, "get_repo_ctx", lambda _request: ctx)

        response = test_client_with_dispatcher.post(f"/reindex?path={sibling}")

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "path_outside_selected_repository"

    def test_reindex_rejects_symlink_escape(
        self, test_client_with_dispatcher, tmp_path, monkeypatch
    ):
        import mcp_server.gateway as gateway

        repo_root = tmp_path / "repo"
        outside = tmp_path / "outside.py"
        repo_root.mkdir()
        outside.write_text("pass")
        link = repo_root / "linked.py"
        link.symlink_to(outside)
        ctx = Mock(repo_id="repo-a", workspace_root=repo_root)
        monkeypatch.setattr(gateway, "get_repo_ctx", lambda _request: ctx)

        response = test_client_with_dispatcher.post(f"/reindex?path={link}")

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "path_outside_selected_repository"


class TestErrorHandling:
    """Test error handling across all endpoints."""

    def test_invalid_endpoint(self, test_client):
        """Test accessing invalid endpoint."""
        response = test_client.get("/invalid")
        assert response.status_code == 404

    def test_method_not_allowed(self, test_client):
        """Test using wrong HTTP method."""
        response = test_client.post("/symbol")
        assert response.status_code == 405

    @pytest.mark.parametrize(
        "endpoint,method",
        [
            ("/symbol", "get"),
            ("/search", "get"),
            ("/status", "get"),
            ("/plugins", "get"),
        ],
    )
    def test_missing_required_params(self, test_client_with_dispatcher, endpoint, method):
        """Test endpoints with missing required parameters."""
        client_method = getattr(test_client_with_dispatcher, method)

        if endpoint == "/symbol":
            # Symbol requires 'symbol' parameter
            response = client_method(endpoint)
            assert response.status_code == 422  # Unprocessable Entity
        elif endpoint == "/search":
            # Search requires 'q' parameter
            response = client_method(endpoint)
            assert response.status_code == 422


class TestConcurrency:
    """Test concurrent request handling."""

    @pytest.mark.parametrize("num_requests", [10, 50])
    def test_concurrent_requests(self, test_client_with_dispatcher, num_requests):
        """Test handling multiple concurrent requests."""
        import concurrent.futures

        test_client_with_dispatcher.app.state.dispatcher.search = Mock(return_value=[])

        def make_request(i):
            return test_client_with_dispatcher.get(f"/search?q=test{i}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(make_request, i) for i in range(num_requests)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # All requests should succeed
        assert all(r.status_code == 200 for r in results)
        assert test_client_with_dispatcher.app.state.dispatcher.search.call_count == num_requests


class TestPerformance:
    """Performance benchmarks for API endpoints."""

    @pytest.mark.benchmark
    def test_symbol_lookup_performance(self, test_client_with_dispatcher, benchmark_results):
        """Benchmark symbol lookup performance."""
        test_client_with_dispatcher.app.state.dispatcher.lookup = Mock(
            return_value=SymbolDef(
                symbol="test",
                kind="function",
                language="python",
                signature="def test()",
                doc=None,
                defined_in="/test.py",
                start_line=1,
                end_line=1,
                span=(1, 1),
            )
        )

        with measure_time("symbol_lookup", benchmark_results):
            for _ in range(100):
                response = test_client_with_dispatcher.get("/symbol?symbol=test")
                assert response.status_code == 200

    @pytest.mark.benchmark
    def test_search_performance(self, test_client_with_dispatcher, benchmark_results):
        """Benchmark search performance."""
        test_client_with_dispatcher.app.state.dispatcher.search = Mock(
            return_value=[
                SearchResult(file=f"/file{i}.py", start_line=1, end_line=1, snippet=f"result_{i}")
                for i in range(20)
            ]
        )

        with measure_time("search", benchmark_results):
            for _ in range(100):
                response = test_client_with_dispatcher.get("/search?q=test")
                assert response.status_code == 200


class TestRepoCtxResolution:
    """SL-3.1: Tests for get_repo_ctx helper and per-request RepoContext resolution."""

    def test_search_resolves_via_x_repo_id_header(
        self, test_client_with_dispatcher, monkeypatch, tmp_path
    ):
        """POST /search with X-Repo-Id header resolves context via RepoResolver."""
        import mcp_server.gateway as gateway
        from mcp_server.core import RepoContext

        # Build a fake RepoContext to be returned by resolve()
        fake_ctx = Mock(spec=RepoContext)
        fake_ctx.repo_id = "resolved"
        fake_ctx.workspace_root = tmp_path
        fake_resolver = Mock()
        fake_resolver.resolve.return_value = fake_ctx

        monkeypatch.setattr(gateway, "repo_resolver", fake_resolver)
        test_client_with_dispatcher.app.state.dispatcher.search = Mock(return_value=[])

        response = test_client_with_dispatcher.get(
            "/search?q=test", headers={"X-Repo-Id": str(tmp_path)}
        )

        assert response.status_code == 200
        fake_resolver.resolve.assert_called_once_with(str(tmp_path))
        test_client_with_dispatcher.app.state.dispatcher.search.assert_called_with(
            fake_ctx, "test", semantic=False, fuzzy=False, limit=20
        )

    def test_search_resolves_via_repository_query_param(
        self, test_client_with_dispatcher, monkeypatch, tmp_path
    ):
        """POST /search with ?repository=<path> resolves context via RepoResolver."""
        import mcp_server.gateway as gateway
        from mcp_server.core import RepoContext

        fake_ctx = Mock(spec=RepoContext)
        fake_ctx.repo_id = "resolved"
        fake_ctx.workspace_root = tmp_path
        fake_resolver = Mock()
        fake_resolver.resolve.return_value = fake_ctx

        monkeypatch.setattr(gateway, "repo_resolver", fake_resolver)
        test_client_with_dispatcher.app.state.dispatcher.search = Mock(return_value=[])

        response = test_client_with_dispatcher.get(f"/search?q=test&repository={tmp_path}")

        assert response.status_code == 200
        fake_resolver.resolve.assert_called_once_with(str(tmp_path))
        test_client_with_dispatcher.app.state.dispatcher.search.assert_called_with(
            fake_ctx, "test", semantic=False, fuzzy=False, limit=20
        )

    def test_explicit_repository_alias_is_preserved(self, test_client_with_dispatcher, monkeypatch):
        import mcp_server.gateway as gateway
        from mcp_server.core import RepoContext

        fake_ctx = Mock(spec=RepoContext)
        fake_ctx.repo_id = "repo-a"
        fake_ctx.workspace_root = Path("/repo/a")
        fake_resolver = Mock()
        fake_resolver.resolve.return_value = fake_ctx
        monkeypatch.setattr(gateway, "repo_resolver", fake_resolver)
        test_client_with_dispatcher.app.state.dispatcher.search = Mock(return_value=[])

        response = test_client_with_dispatcher.get("/search?q=test&repository=frontend")

        assert response.status_code == 200
        fake_resolver.resolve.assert_called_once_with("frontend")

    def test_unknown_explicit_repository_fails_closed(
        self, test_client_with_dispatcher, monkeypatch
    ):
        import mcp_server.gateway as gateway
        from mcp_server.health.repository_readiness import (
            RepositoryReadiness,
            RepositoryReadinessState,
        )

        fake_resolver = Mock()
        fake_resolver.resolve.return_value = None
        fake_resolver.classify.return_value = RepositoryReadiness(
            state=RepositoryReadinessState.UNREGISTERED_REPOSITORY,
            requested_path="missing-repo",
        )
        monkeypatch.setattr(gateway, "repo_resolver", fake_resolver)

        response = test_client_with_dispatcher.get("/search?q=test&repository=missing-repo")

        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "unregistered_repository"
        fake_resolver.resolve.assert_not_called()

    @pytest.mark.parametrize("endpoint", ["/search?q=test", "/symbol?symbol=test", "/status"])
    def test_nonready_http_reads_refuse_before_store_resolution(
        self, endpoint, test_client_with_dispatcher, monkeypatch
    ):
        import mcp_server.gateway as gateway
        from mcp_server.health.repository_readiness import (
            RepositoryReadiness,
            RepositoryReadinessState,
        )

        resolver = Mock()
        resolver.classify.return_value = RepositoryReadiness(
            state=RepositoryReadinessState.MISSING_SCHEMA,
            repository_id="repo-a",
            registered_path="/repo/a",
        )
        resolver.resolve.side_effect = AssertionError("must not open the store")
        monkeypatch.setattr(gateway, "repo_resolver", resolver)

        separator = "&" if "?" in endpoint else "?"
        response = test_client_with_dispatcher.get(f"{endpoint}{separator}repository=repo-a")

        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "missing_schema"
        resolver.resolve.assert_not_called()

    def test_alternate_search_backends_are_scoped_per_repository(self, monkeypatch, tmp_path):
        import mcp_server.gateway as gateway

        store_a = SQLiteStore(str(tmp_path / "a.db"))
        store_b = SQLiteStore(str(tmp_path / "b.db"))
        ctx_a = Mock(repo_id="repo-a", sqlite_store=store_a)
        ctx_b = Mock(repo_id="repo-b", sqlite_store=store_b)
        monkeypatch.setattr(gateway, "_repo_search_backends", {})

        try:
            bm25_a, fuzzy_a, hybrid_a = gateway._search_backends_for_repo(ctx_a)
            bm25_b, fuzzy_b, hybrid_b = gateway._search_backends_for_repo(ctx_b)

            assert bm25_a is not bm25_b
            assert fuzzy_a is not fuzzy_b
            assert hybrid_a is not hybrid_b
            assert bm25_a.storage is store_a
            assert bm25_b.storage is store_b
            assert hybrid_a.storage is store_a
            assert hybrid_b.storage is store_b
        finally:
            store_a.close()
            store_b.close()

    def test_search_falls_back_to_cwd_resolution(
        self, test_client_with_dispatcher, monkeypatch, tmp_path
    ):
        """POST /search without header/param falls back to resolver.resolve(cwd)."""
        import mcp_server.gateway as gateway
        from mcp_server.core import RepoContext

        fake_ctx = Mock(spec=RepoContext)
        fake_ctx.repo_id = "resolved"
        fake_ctx.workspace_root = tmp_path
        fake_resolver = Mock()
        fake_resolver.resolve.return_value = fake_ctx

        monkeypatch.setattr(gateway, "repo_resolver", fake_resolver)
        test_client_with_dispatcher.app.state.dispatcher.search = Mock(return_value=[])

        response = test_client_with_dispatcher.get("/search?q=test")

        assert response.status_code == 200
        # resolver.resolve should have been called (with cwd)
        fake_resolver.resolve.assert_called_once()
        test_client_with_dispatcher.app.state.dispatcher.search.assert_called_with(
            fake_ctx, "test", semantic=False, fuzzy=False, limit=20
        )

    def test_no_private_attr_access_in_gateway(self):
        """Verify gateway.py contains no dispatcher._plugins / _graph_ / _sqlite_store references."""
        import re
        import subprocess

        result = subprocess.run(
            ["grep", "-n", r"dispatcher\._", "mcp_server/gateway.py"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        private_pattern = re.compile(
            r"dispatcher\.(_(plugins|graph_|sqlite_store|multi_repo_manager))"
        )
        matches = [line for line in result.stdout.splitlines() if private_pattern.search(line)]
        assert matches == [], f"Found forbidden private-attr access: {matches}"

    def test_dispatcher_ctor_no_sqlite_store_kwarg(self):
        """Verify EnhancedDispatcher ctor calls in gateway.py pass no sqlite_store kwarg."""
        import subprocess

        result = subprocess.run(
            ["grep", "-n", "EnhancedDispatcher(", "mcp_server/gateway.py"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        for line in result.stdout.splitlines():
            assert (
                "sqlite_store=" not in line
            ), f"Found sqlite_store kwarg in EnhancedDispatcher ctor: {line}"
