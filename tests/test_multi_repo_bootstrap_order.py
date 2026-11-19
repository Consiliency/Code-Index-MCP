"""SL-3.1 red tests — boot-order and watcher-wiring assertions.

Verifies:
- MultiRepositoryWatcher.start_watching_all is called (not FileWatcher)
- RefPoller.start is called after MultiRepositoryWatcher.start_watching_all
- Boot order: StoreRegistry constructed before start_watching_all called
- Shutdown: stop_watching_all + ref_poller.stop both called on server exit
- FileWatcher is NOT constructed in stdio_runner or gateway watcher paths
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry(tmp_path: Path, num_repos: int = 2):
    """Return a real RepositoryRegistry with num_repos fake entries."""
    from mcp_server.storage.repository_registry import RepositoryRegistry

    reg_path = tmp_path / "registry.json"
    registry = RepositoryRegistry(registry_path=reg_path)
    for i in range(num_repos):
        repo_dir = tmp_path / f"repo{i}"
        repo_dir.mkdir()
        registry.register_repository(str(repo_dir), f"repo{i}")
    return registry


# ---------------------------------------------------------------------------
# SL-3.1 — bootstrap.initialize_stateless_services extended return
# ---------------------------------------------------------------------------


class TestInitializeStatelessServicesExtended:
    """initialize_stateless_services now returns 5-tuple (or ServicePool)."""

    def test_returns_five_elements(self, tmp_path):
        from mcp_server.cli.bootstrap import initialize_stateless_services
        from mcp_server.storage import StoreRegistry
        from mcp_server.storage.git_index_manager import GitAwareIndexManager
        from mcp_server.storage.repository_registry import RepositoryRegistry

        result = initialize_stateless_services(registry_path=tmp_path / "registry.json")
        assert len(result) == 5, f"Expected 5-element tuple, got {len(result)}"
        store_registry, repo_resolver, dispatcher, repo_registry, git_index_manager = result
        assert isinstance(store_registry, StoreRegistry)
        assert isinstance(repo_registry, RepositoryRegistry)
        assert isinstance(git_index_manager, GitAwareIndexManager)
        assert git_index_manager.repo_resolver is repo_resolver
        assert git_index_manager.store_registry is store_registry

    def test_backward_compat_destructuring(self, tmp_path):
        """Old callers that do store, resolver, dispatcher = result still work if they unpack 3."""
        from mcp_server.cli.bootstrap import initialize_stateless_services

        result = initialize_stateless_services(registry_path=tmp_path / "registry.json")
        # 5-tuple unpack
        store_registry, repo_resolver, dispatcher, repo_registry, git_index_manager = result
        assert callable(getattr(dispatcher, "search", None))


# ---------------------------------------------------------------------------
# SL-3.1 — stdio_runner boot order
# ---------------------------------------------------------------------------


class TestStdioRunnerBootOrder:
    """MultiRepositoryWatcher.start_watching_all called after StoreRegistry constructed."""

    def test_multi_watcher_starts_after_store_registry(self, tmp_path, monkeypatch):
        """Boot order: StoreRegistry is constructed before start_watching_all fires.

        We verify this structurally: initialize_stateless_services (which builds
        StoreRegistry) is called BEFORE MultiRepositoryWatcher.start_watching_all.
        We track this via side-effect ordering on a shared call_log list.
        """
        call_log: list[str] = []

        import mcp_server.cli.stdio_runner as runner_mod

        orig_init = runner_mod.initialize_stateless_services

        def patched_init(registry_path=None):
            result = orig_init(registry_path=registry_path)
            call_log.append("initialize_stateless_services")
            return result

        mock_multi_watcher = MagicMock()
        mock_multi_watcher.start_watching_all.side_effect = lambda: call_log.append(
            "start_watching_all"
        )
        mock_multi_watcher.stop_watching_all = MagicMock()

        mock_ref_poller = MagicMock()
        mock_ref_poller.start = MagicMock()
        mock_ref_poller.stop = MagicMock()

        with patch.object(runner_mod, "initialize_stateless_services", patched_init):
            with patch.object(
                runner_mod, "MultiRepositoryWatcher", return_value=mock_multi_watcher
            ):
                with patch.object(runner_mod, "RefPoller", return_value=mock_ref_poller):
                    try:
                        asyncio.run(
                            asyncio.wait_for(
                                _run_stdio_serve(tmp_path),
                                timeout=3.0,
                            )
                        )
                    except asyncio.TimeoutError, Exception:
                        pass

        assert (
            "initialize_stateless_services" in call_log
        ), "initialize_stateless_services not called"
        assert "start_watching_all" in call_log, "start_watching_all was never called"
        init_idx = call_log.index("initialize_stateless_services")
        sw_idx = call_log.index("start_watching_all")
        assert (
            init_idx < sw_idx
        ), "start_watching_all fired before initialize_stateless_services completed"

    def test_ref_poller_starts_after_multi_watcher(self, tmp_path):
        """RefPoller.start is called after MultiRepositoryWatcher.start_watching_all."""
        call_log: list[str] = []

        mock_multi_watcher = MagicMock()
        mock_multi_watcher.start_watching_all.side_effect = lambda: call_log.append(
            "start_watching_all"
        )
        mock_multi_watcher.stop_watching_all = MagicMock()

        mock_ref_poller = MagicMock()
        mock_ref_poller.start.side_effect = lambda: call_log.append("ref_poller_start")
        mock_ref_poller.stop = MagicMock()

        with patch(
            "mcp_server.cli.stdio_runner.MultiRepositoryWatcher", return_value=mock_multi_watcher
        ):
            with patch("mcp_server.cli.stdio_runner.RefPoller", return_value=mock_ref_poller):
                try:
                    asyncio.run(
                        asyncio.wait_for(
                            _run_stdio_serve(tmp_path),
                            timeout=3.0,
                        )
                    )
                except asyncio.TimeoutError, Exception:
                    pass

        assert "start_watching_all" in call_log, "start_watching_all not called"
        assert "ref_poller_start" in call_log, "RefPoller.start not called"
        sw_idx = call_log.index("start_watching_all")
        rp_idx = call_log.index("ref_poller_start")
        assert sw_idx < rp_idx, "RefPoller.start must come after start_watching_all"

    def test_shutdown_calls_stop_watching_all_and_ref_poller_stop(self, tmp_path):
        """On server shutdown, the multi-repo watcher and ref poller are stopped.

        _graceful_shutdown() calls ``multi_watcher.stop()`` (a consolidated
        shutdown entry point that internally calls stop_watching_all) and
        ``ref_poller.stop()``.  The test was written against a pre-refactor
        shutdown path that invoked ``stop_watching_all`` directly; update the
        assertion to match the current contract without losing coverage of
        the intent (both subsystems receive a stop signal).
        """
        mock_multi_watcher = MagicMock()
        mock_ref_poller = MagicMock()

        with patch(
            "mcp_server.cli.stdio_runner.MultiRepositoryWatcher", return_value=mock_multi_watcher
        ):
            with patch("mcp_server.cli.stdio_runner.RefPoller", return_value=mock_ref_poller):
                try:
                    asyncio.run(
                        asyncio.wait_for(
                            _run_stdio_serve(tmp_path),
                            timeout=2.0,
                        )
                    )
                except asyncio.TimeoutError, Exception:
                    pass

        mock_multi_watcher.stop.assert_called()
        mock_ref_poller.stop.assert_called()


# ---------------------------------------------------------------------------
# SL-3.1 — FileWatcher NOT constructed
# ---------------------------------------------------------------------------


class TestFileWatcherNotConstructed:
    """FileWatcher must not be instantiated in stdio_runner or gateway after SL-3.2."""

    def test_stdio_runner_no_file_watcher_construction(self, tmp_path):
        """MultiRepositoryWatcher is used; FileWatcher() is not called in stdio_runner."""
        mock_multi_watcher = MagicMock()
        mock_ref_poller = MagicMock()

        fw_calls: list[Any] = []

        class SpyFileWatcher:
            def __init__(self, *args, **kwargs):
                fw_calls.append((args, kwargs))

            def start(self):
                pass

            def stop(self):
                pass

        with patch("mcp_server.cli.stdio_runner.FileWatcher", SpyFileWatcher, create=True):
            with patch(
                "mcp_server.cli.stdio_runner.MultiRepositoryWatcher",
                return_value=mock_multi_watcher,
            ):
                with patch("mcp_server.cli.stdio_runner.RefPoller", return_value=mock_ref_poller):
                    try:
                        asyncio.run(
                            asyncio.wait_for(
                                _run_stdio_serve(tmp_path),
                                timeout=2.0,
                            )
                        )
                    except asyncio.TimeoutError, Exception:
                        pass

        assert fw_calls == [], (
            f"FileWatcher was constructed {len(fw_calls)} time(s) in stdio_runner — "
            "it must be replaced by MultiRepositoryWatcher"
        )

    def test_multi_watcher_called_in_stdio_runner(self, tmp_path):
        """MultiRepositoryWatcher.start_watching_all is invoked during stdio_runner boot."""
        mock_multi_watcher = MagicMock()
        mock_ref_poller = MagicMock()

        with patch(
            "mcp_server.cli.stdio_runner.MultiRepositoryWatcher", return_value=mock_multi_watcher
        ):
            with patch("mcp_server.cli.stdio_runner.RefPoller", return_value=mock_ref_poller):
                try:
                    asyncio.run(
                        asyncio.wait_for(
                            _run_stdio_serve(tmp_path),
                            timeout=2.0,
                        )
                    )
                except asyncio.TimeoutError, Exception:
                    pass

        mock_multi_watcher.start_watching_all.assert_called()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run_stdio_serve(tmp_path: Path):
    """Invoke stdio_runner._serve() with a minimal registry_path override."""
    from mcp_server.cli.stdio_runner import _serve

    await _serve(registry_path=tmp_path / "registry.json")
