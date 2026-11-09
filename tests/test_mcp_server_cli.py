"""
Tests for mcp_server_cli.py — auto-index, deferred FileWatcher, MCP_AUTO_INDEX
escape hatch, indexing_in_progress response flag, and WAL gitignore writes.
"""

import asyncio
import importlib.util
import json
import os
import subprocess
import threading
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import mcp.types as types
import pytest

# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------

CLI_PATH = Path(__file__).parent.parent / "mcp_server" / "cli" / "stdio_runner.py"


def _load_cli_module():
    """Import mcp_server_cli as a fresh module object (isolated, not in sys.modules)."""
    spec = importlib.util.spec_from_file_location("mcp_server_cli_test", CLI_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _reset_globals(mod):
    """Reset module-level globals to None between tests."""
    mod.dispatcher = None
    mod.plugin_manager = None
    mod.sqlite_store = None
    mod.initialization_error = None
    mod._file_watcher = None
    mod._indexing_thread = None
    mod._fts_rebuild_thread = None
    mod._repo_resolver = None
    mod._store_registry = None
    mod._git_index_manager = None
    mod._local_ctx = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_discovery_mock(index_path=None, enabled=True):
    """Return a mock IndexDiscovery that optionally has an existing index."""
    mock = MagicMock()
    mock.get_index_info.return_value = {
        "enabled": enabled,
        "search_paths": [],
        "location_type": "local",
    }
    mock.get_local_index_path.return_value = index_path
    mock.get_index_config.return_value = None
    mock._classify_location.return_value = "local"
    return mock


class _FakeEnhancedDispatcher:
    """Real class so isinstance(dispatcher, EnhancedDispatcher) passes in tests.

    When we patch cli.EnhancedDispatcher with this class, the created instance
    IS a _FakeEnhancedDispatcher, so the isinstance check in initialize_services()
    passes and the FileWatcher / thread code paths are actually reached.
    """

    def __init__(self, *args, **kwargs):
        self.supported_languages = ["python", "javascript"]
        self._search_results = []

    def get_statistics(self):
        return {"total_files": 0, "total_symbols": 0}

    def health_check(self):
        return {"status": "operational"}

    def index_directory(self, *args, **kwargs):
        return {"indexed": 0, "errors": 0, "skipped": 0}

    def search(self, *args, **kwargs):
        return self._search_results

    def lookup_symbol(self, *args, **kwargs):
        return []

    def get_supported_extensions(self):
        return {".py", ".js"}


def _std_patches(cli, mock_disc, mock_watcher):
    """Return standard patch objects for initialize_services() tests."""
    return [
        patch.object(cli, "IndexDiscovery", return_value=mock_disc),
        patch.object(cli, "SQLiteStore", return_value=MagicMock()),
        patch.object(cli, "EnhancedDispatcher", _FakeEnhancedDispatcher),
        patch.object(cli, "FileWatcher", return_value=mock_watcher),
        patch.object(
            cli, "validate_index", return_value={"valid": True, "stats": {}, "issues": []}
        ),
        patch.object(cli, "PluginManager", return_value=MagicMock()),
    ]


def _apply_patches(patches, extra=None):
    """Return an ExitStack that applies all patches."""
    stack = ExitStack()
    for p in (patches or []) + (extra or []):
        stack.enter_context(p)
    return stack


def _structured_and_text_payload(result: types.CallToolResult) -> tuple[dict, object]:
    assert isinstance(result, types.CallToolResult)
    assert result.content
    assert isinstance(result.content[0], types.TextContent)
    assert result.structured_content is not None
    return result.structured_content, json.loads(result.content[0].text)


# ---------------------------------------------------------------------------
# 1. Gitignore WAL entries written on auto-init
# ---------------------------------------------------------------------------


class TestAutoInitGitignore:
    """WAL sidecar entries must be added to .gitignore when a new index is created."""

    def _run_init(self, tmp_path, monkeypatch):
        """Run initialize_services() in a fresh tmp dir with no existing index."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MCP_AUTO_INDEX", "false")  # keep test fast; no real thread

        cli = _load_cli_module()
        _reset_globals(cli)
        mock_disc = _make_discovery_mock(index_path=None)
        mock_watcher = MagicMock()

        with _apply_patches(_std_patches(cli, mock_disc, mock_watcher)):
            asyncio.run(cli.initialize_services())

        return cli, tmp_path

    def test_creates_wal_and_shm_entries(self, tmp_path, monkeypatch):
        self._run_init(tmp_path, monkeypatch)
        content = (tmp_path / ".gitignore").read_text()
        assert ".mcp-index/code_index.db-wal" in content
        assert ".mcp-index/code_index.db-shm" in content

    def test_creates_db_and_metadata_entries(self, tmp_path, monkeypatch):
        self._run_init(tmp_path, monkeypatch)
        content = (tmp_path / ".gitignore").read_text()
        assert ".mcp-index/code_index.db" in content
        assert ".mcp-index/.index_metadata.json" in content

    def test_idempotent_when_entries_already_present(self, tmp_path, monkeypatch):
        """Running init twice must not duplicate gitignore entries."""
        pre = (
            ".mcp-index/code_index.db\n"
            ".mcp-index/code_index.db-shm\n"
            ".mcp-index/code_index.db-wal\n"
            ".mcp-index/.index_metadata.json\n"
        )
        (tmp_path / ".gitignore").write_text(pre)
        self._run_init(tmp_path, monkeypatch)
        content = (tmp_path / ".gitignore").read_text()
        assert content.count(".mcp-index/code_index.db\n") == 1

    def test_appends_to_existing_gitignore(self, tmp_path, monkeypatch):
        (tmp_path / ".gitignore").write_text("*.pyc\n__pycache__/\n")
        self._run_init(tmp_path, monkeypatch)
        content = (tmp_path / ".gitignore").read_text()
        assert "*.pyc" in content
        assert ".mcp-index/code_index.db-wal" in content

    def test_no_write_when_index_already_exists(self, tmp_path, monkeypatch):
        """No gitignore changes when the index already existed."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MCP_AUTO_INDEX", "false")

        cli = _load_cli_module()
        _reset_globals(cli)

        index_dir = tmp_path / ".mcp-index"
        index_dir.mkdir()
        index_db = index_dir / "code_index.db"
        index_db.touch()

        mock_disc = _make_discovery_mock(index_path=index_db)

        with _apply_patches(_std_patches(cli, mock_disc, MagicMock())):
            asyncio.run(cli.initialize_services())

        assert not (tmp_path / ".gitignore").exists()


# ---------------------------------------------------------------------------
# 2. MCP_AUTO_INDEX=false escape hatch
# ---------------------------------------------------------------------------


class TestMcpAutoIndexEscapeHatch:
    """MCP_AUTO_INDEX=false must prevent the background indexing thread from starting."""

    def _run_and_capture_threads(self, tmp_path, monkeypatch, env_value):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MCP_AUTO_INDEX", env_value)

        cli = _load_cli_module()
        _reset_globals(cli)
        mock_disc = _make_discovery_mock(index_path=None)
        indexing_threads_started = []
        original_thread = threading.Thread

        def capture_thread(*args, **kwargs):
            if kwargs.get("name") == "mcp-initial-index":
                # Return a no-op mock so no actual background work happens
                noop = MagicMock(spec=original_thread)
                noop.is_alive.return_value = False
                indexing_threads_started.append(noop)
                return noop
            return original_thread(*args, **kwargs)

        extra = [patch("threading.Thread", side_effect=capture_thread)]
        with _apply_patches(_std_patches(cli, mock_disc, MagicMock()), extra):
            asyncio.run(cli.initialize_services())

        return indexing_threads_started

    def test_false_skips_background_thread(self, tmp_path, monkeypatch):
        threads = self._run_and_capture_threads(tmp_path, monkeypatch, "false")
        assert len(threads) == 0, "No indexing thread should start when MCP_AUTO_INDEX=false"

    def test_true_starts_background_thread(self, tmp_path, monkeypatch):
        threads = self._run_and_capture_threads(tmp_path, monkeypatch, "true")
        assert len(threads) == 1, "One indexing thread should start when MCP_AUTO_INDEX=true"

    def test_thread_not_started_when_index_already_exists(self, tmp_path, monkeypatch):
        """No auto-index thread when an index was already found."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MCP_AUTO_INDEX", "true")

        cli = _load_cli_module()
        _reset_globals(cli)

        index_dir = tmp_path / ".mcp-index"
        index_dir.mkdir()
        index_db = index_dir / "code_index.db"
        index_db.touch()

        mock_disc = _make_discovery_mock(index_path=index_db)
        indexing_threads_started = []
        original_thread = threading.Thread

        def capture_thread(*args, **kwargs):
            if kwargs.get("name") == "mcp-initial-index":
                noop = MagicMock(spec=original_thread)
                noop.is_alive.return_value = False
                indexing_threads_started.append(noop)
                return noop
            return original_thread(*args, **kwargs)

        extra = [patch("threading.Thread", side_effect=capture_thread)]
        with _apply_patches(_std_patches(cli, mock_disc, MagicMock()), extra):
            asyncio.run(cli.initialize_services())

        assert len(indexing_threads_started) == 0, "No thread when index already exists"


# ---------------------------------------------------------------------------
# 3. Deferred FileWatcher start
# ---------------------------------------------------------------------------


class TestDeferredFileWatcher:
    """FileWatcher.start() must be deferred until after initial indexing completes."""

    def test_watcher_not_started_during_auto_index(self, tmp_path, monkeypatch):
        """When MCP_AUTO_INDEX=false (no thread), watcher is created but not started."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MCP_AUTO_INDEX", "false")

        cli = _load_cli_module()
        _reset_globals(cli)
        mock_disc = _make_discovery_mock(index_path=None)
        mock_watcher = MagicMock()

        with _apply_patches(_std_patches(cli, mock_disc, mock_watcher)):
            asyncio.run(cli.initialize_services())

        # Watcher created but NOT started (deferred)
        assert cli._file_watcher is mock_watcher
        mock_watcher.start.assert_not_called()

    def test_watcher_started_immediately_when_index_exists(self, tmp_path, monkeypatch):
        """When index already exists (no auto-index), watcher starts immediately."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MCP_AUTO_INDEX", "false")

        cli = _load_cli_module()
        _reset_globals(cli)

        index_dir = tmp_path / ".mcp-index"
        index_dir.mkdir()
        index_db = index_dir / "code_index.db"
        index_db.touch()

        mock_disc = _make_discovery_mock(index_path=index_db)
        mock_watcher = MagicMock()

        with _apply_patches(_std_patches(cli, mock_disc, mock_watcher)):
            asyncio.run(cli.initialize_services())

        mock_watcher.start.assert_called_once()

    def test_watcher_started_after_initial_index_thread_completes(self, tmp_path, monkeypatch):
        """The background index thread callback must call _file_watcher.start()."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MCP_AUTO_INDEX", "true")

        cli = _load_cli_module()
        _reset_globals(cli)
        mock_disc = _make_discovery_mock(index_path=None)
        mock_watcher = MagicMock()
        captured_target = []
        original_thread = threading.Thread

        def capture_thread(*args, **kwargs):
            if kwargs.get("name") == "mcp-initial-index":
                captured_target.append(kwargs["target"])
                noop = MagicMock(spec=original_thread)
                noop.is_alive.return_value = False
                return noop
            return original_thread(*args, **kwargs)

        extra = [patch("threading.Thread", side_effect=capture_thread)]
        with _apply_patches(_std_patches(cli, mock_disc, mock_watcher), extra):
            asyncio.run(cli.initialize_services())

        # Watcher not started yet
        mock_watcher.start.assert_not_called()

        # Execute the thread target synchronously (simulates thread finishing)
        assert captured_target, "Thread target was not captured"
        captured_target[0]()

        # Watcher should be started now
        mock_watcher.start.assert_called_once()


# ---------------------------------------------------------------------------
# 4. indexing_in_progress flag in search_code responses
# ---------------------------------------------------------------------------


class TestIndexingInProgressFlag:
    """search_code responses include indexing_in_progress when the thread is alive."""

    def _alive_thread(self):
        t = MagicMock(spec=threading.Thread)
        t.is_alive.return_value = True
        return t

    def _dead_thread(self):
        t = MagicMock(spec=threading.Thread)
        t.is_alive.return_value = False
        return t

    async def _call_search(self, cli, query="def foo"):
        """Invoke search_code via the module-level call_tool function.

        @server.call_tool() returns the original function unchanged, so
        cli.call_tool is the decorated async function we can call directly.
        """
        result = await cli.call_tool("search_code", {"query": query})
        structured, text_payload = _structured_and_text_payload(result)
        return structured, text_payload

    def _make_cli_with_dispatcher(self, search_results):
        """Return a fresh cli module with dispatcher pre-set to avoid MagicMock attrs
        that trigger the stale-index validation path (e.g. hasattr(mock, '_sqlite_store')
        is True for any MagicMock attribute).
        """
        cli = _load_cli_module()
        _reset_globals(cli)
        # Use _FakeEnhancedDispatcher so hasattr checks return False for missing attrs
        disp = _FakeEnhancedDispatcher()
        disp._search_results = search_results
        cli.dispatcher = disp
        cli.sqlite_store = MagicMock()
        readiness = SimpleNamespace(
            ready=True,
            repository_id="test-repo",
            to_dict=lambda: {"state": "ready", "ready": True, "code": None},
        )
        resolver = MagicMock()
        resolver.classify.return_value = readiness
        resolver.resolve.return_value = MagicMock()
        cli._repo_resolver = resolver
        return cli

    @pytest.mark.asyncio
    async def test_empty_results_flag_when_thread_alive(self):
        """indexing_in_progress=true must appear in empty results while indexing."""
        cli = self._make_cli_with_dispatcher([])
        cli._indexing_thread = self._alive_thread()

        structured, text_payload = await self._call_search(cli)
        assert structured.get("indexing_in_progress") is True
        assert text_payload["indexing_in_progress"] is True

    @pytest.mark.asyncio
    async def test_empty_results_no_flag_when_thread_done(self):
        """No indexing_in_progress flag after the indexing thread finishes."""
        cli = self._make_cli_with_dispatcher([])
        cli._indexing_thread = self._dead_thread()

        structured, text_payload = await self._call_search(cli)
        assert "indexing_in_progress" not in structured
        assert "indexing_in_progress" not in text_payload

    @pytest.mark.asyncio
    async def test_empty_results_informative_message_during_indexing(self):
        """Message should indicate index is still building when thread is alive."""
        cli = self._make_cli_with_dispatcher([])
        cli._indexing_thread = self._alive_thread()

        structured, text_payload = await self._call_search(cli)
        assert "building" in structured.get("message", "").lower()
        assert "building" in text_payload.get("message", "").lower()

    @pytest.mark.asyncio
    async def test_non_empty_results_include_flag_when_thread_alive(self):
        """Even with results, indexing_in_progress must appear while indexing."""
        cli = self._make_cli_with_dispatcher(
            [{"file": "/tmp/foo.py", "line": 1, "snippet": "def foo(): pass", "symbol": "foo"}]
        )
        cli._indexing_thread = self._alive_thread()

        structured, text_payload = await self._call_search(cli, query="foo")
        assert structured.get("indexing_in_progress") is True
        assert "results" in structured
        assert text_payload["results"]

    @pytest.mark.asyncio
    async def test_non_empty_results_no_flag_when_no_thread(self):
        """Results with no indexing thread must NOT include indexing_in_progress."""
        cli = self._make_cli_with_dispatcher(
            [{"file": "/tmp/foo.py", "line": 1, "snippet": "def foo(): pass", "symbol": "foo"}]
        )
        cli._indexing_thread = None

        structured, text_payload = await self._call_search(cli, query="foo")
        assert "indexing_in_progress" not in structured
        assert isinstance(structured["results"], list)
        assert isinstance(text_payload, list)


# ---------------------------------------------------------------------------
# 5. End-to-end: real SQLiteStore + real EnhancedDispatcher on a tiny repo
# ---------------------------------------------------------------------------


class TestFreshRepoEndToEnd:
    """BM25 content must be populated and searchable after the auto-index thread runs."""

    @pytest.mark.asyncio
    async def test_bm25_populated_and_searchable_after_initial_index(self, tmp_path, monkeypatch):
        """After the background index thread finishes, search_code returns real results."""
        # --- Create a tiny real repo ---
        (tmp_path / "greet.py").write_text("def greet_world():\n    return 'hello world'\n")
        (tmp_path / "math_utils.py").write_text("def add_numbers(a, b):\n    return a + b\n")
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Code Index Test",
                "-c",
                "user.email=code-index-test@example.invalid",
                "commit",
                "-m",
                "seed test repository",
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MCP_AUTO_INDEX", "true")
        monkeypatch.setenv("MCP_USE_SIMPLE_DISPATCHER", "false")
        monkeypatch.setenv("MCP_PLUGIN_SANDBOX_DISABLE", "1")

        cli = _load_cli_module()
        _reset_globals(cli)

        mock_disc = _make_discovery_mock(index_path=None)
        mock_watcher = MagicMock()
        captured_target = []
        original_thread = threading.Thread

        def capture_thread(*args, **kwargs):
            if kwargs.get("name") == "mcp-initial-index":
                captured_target.append(kwargs["target"])
                noop = MagicMock(spec=original_thread)
                noop.is_alive.return_value = False
                return noop
            return original_thread(*args, **kwargs)

        mock_pm = MagicMock()
        mock_pm.get_active_plugins.return_value = {}
        mock_pm.load_plugins_safe.return_value = MagicMock(success=True, metadata={})

        patches = [
            patch.object(cli, "IndexDiscovery", return_value=mock_disc),
            patch.object(cli, "FileWatcher", return_value=mock_watcher),
            patch.object(cli, "PluginManager", return_value=mock_pm),
            # SQLiteStore and EnhancedDispatcher are intentionally NOT mocked
        ]
        extra = [patch("threading.Thread", side_effect=capture_thread)]

        with _apply_patches(patches, extra):
            await cli.initialize_services()

        assert captured_target, "Auto-index thread target was not captured"

        # Run the background index synchronously (simulates thread completing)
        captured_target[0]()

        # Mark thread as done so indexing_in_progress flag is absent
        cli._indexing_thread = None

        # Search for a symbol that exists in greet.py
        result = await cli.call_tool("search_code", {"query": "greet_world"})
        structured, text_payload = _structured_and_text_payload(result)

        results = structured.get("results", [])
        assert isinstance(text_payload, list)
        assert results, (
            "Expected BM25 results after indexing real files, got empty results. "
            f"Full response: {structured}"
        )


@pytest.mark.asyncio
async def test_call_tool_preserves_create_task_result_branch():
    cli = _load_cli_module()
    _reset_globals(cli)
    cli.dispatcher = _FakeEnhancedDispatcher()
    cli.sqlite_store = MagicMock()

    created = types.CreateTaskResult(
        task=types.Task(
            taskId="task-1",
            status="working",
            statusMessage="queued",
            # mcp 2.x types task timestamps as ISO strings.
            createdAt=datetime.now(timezone.utc).isoformat(),
            lastUpdatedAt=datetime.now(timezone.utc).isoformat(),
            ttl=None,
            pollInterval=500,
        )
    )

    async def _fake_reindex(**kwargs):
        return created

    with patch.object(cli.tool_handlers, "handle_reindex", side_effect=_fake_reindex):
        result = await cli.call_tool("reindex", {"path": "/tmp/example"})

    assert result is created
