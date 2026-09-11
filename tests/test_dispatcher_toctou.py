"""Tests for TOCTOU-guarded dispatch path (SL-3)."""

import hashlib
import logging
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_server.core.repo_context import RepoContext
from mcp_server.dispatcher import EnhancedDispatcher as Dispatcher
from mcp_server.dispatcher.dispatcher_enhanced import IndexResult, IndexResultStatus
from mcp_server.storage.multi_repo_manager import RepositoryInfo

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _bytes_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_dispatcher(tmp_path: Path) -> Dispatcher:
    mock_plugin = MagicMock()
    mock_plugin.lang = "python"
    mock_plugin.supports = MagicMock(return_value=True)
    mock_plugin.indexFile = MagicMock(return_value={"symbols": []})
    mock_plugin.language = "python"
    dispatcher = Dispatcher([mock_plugin], semantic_search_enabled=False)
    return dispatcher, mock_plugin


def _make_ctx(tmp_path: Path) -> RepoContext:
    registry_entry = MagicMock(spec=RepositoryInfo)
    registry_entry.path = tmp_path
    registry_entry.name = "test-repo"
    return RepoContext(
        repo_id="test-repo",
        sqlite_store=MagicMock(),
        workspace_root=tmp_path,
        tracked_branch="main",
        registry_entry=registry_entry,
    )


# ---------------------------------------------------------------------------
# SL-3.1 tests
# ---------------------------------------------------------------------------


class TestIndexFileGuarded:
    def test_matching_hash_returns_indexed(self, tmp_path):
        """Matching expected_hash → INDEXED and plugin.indexFile is called."""
        dispatcher, mock_plugin = _make_dispatcher(tmp_path)
        ctx = _make_ctx(tmp_path)

        test_file = tmp_path / "test.py"
        content = b"x = 1\n"
        test_file.write_bytes(content)
        expected_hash = _bytes_hash(content)

        result = dispatcher.index_file_guarded(ctx, test_file, expected_hash)

        assert result.status == IndexResultStatus.INDEXED
        assert result.path == test_file
        assert result.observed_hash == expected_hash
        assert result.actual_hash == expected_hash
        assert result.error is None
        mock_plugin.indexFile.assert_called_once()

    def test_mismatched_hash_returns_skipped_toctou(self, tmp_path):
        """Mismatched hash → SKIPPED_TOCTOU, plugin.indexFile NOT called."""
        dispatcher, mock_plugin = _make_dispatcher(tmp_path)
        ctx = _make_ctx(tmp_path)

        test_file = tmp_path / "test.py"
        test_file.write_bytes(b"x = 1\n")
        stale_hash = _bytes_hash(b"old content\n")  # differs from actual file

        result = dispatcher.index_file_guarded(ctx, test_file, stale_hash)

        assert result.status == IndexResultStatus.SKIPPED_TOCTOU
        assert result.path == test_file
        assert result.observed_hash == stale_hash
        actual = _bytes_hash(b"x = 1\n")
        assert result.actual_hash == actual
        mock_plugin.indexFile.assert_not_called()

    def test_toctou_emits_log_event(self, tmp_path, caplog):
        """SKIPPED_TOCTOU emits dispatcher.index.toctou_skipped log with path/observed/actual."""
        dispatcher, mock_plugin = _make_dispatcher(tmp_path)
        ctx = _make_ctx(tmp_path)

        test_file = tmp_path / "test.py"
        test_file.write_bytes(b"x = 1\n")
        stale_hash = _bytes_hash(b"old\n")

        with caplog.at_level(logging.WARNING, logger="mcp_server.dispatcher.dispatcher_enhanced"):
            result = dispatcher.index_file_guarded(ctx, test_file, stale_hash)

        assert result.status == IndexResultStatus.SKIPPED_TOCTOU
        toctou_records = [r for r in caplog.records if "toctou_skipped" in r.getMessage()]
        assert len(toctou_records) >= 1
        record = toctou_records[0]
        assert hasattr(record, "path") or str(test_file) in record.getMessage()
        assert hasattr(record, "observed") or stale_hash in record.getMessage()

    def test_concurrent_writer_yields_skipped_toctou(self, tmp_path):
        """Thread A hashes file; thread B rewrites file; thread A calls guarded → SKIPPED_TOCTOU."""
        dispatcher, mock_plugin = _make_dispatcher(tmp_path)
        ctx = _make_ctx(tmp_path)

        test_file = tmp_path / "test.py"
        original_content = b"# original\n"
        new_content = b"# rewritten by thread B\n"
        test_file.write_bytes(original_content)

        # Thread A observes the hash of original content (watcher side)
        observed_hash = _bytes_hash(original_content)

        barrier = threading.Barrier(2)
        results = []

        def thread_b():
            barrier.wait()  # sync with thread A
            # Rewrite file after thread A already computed hash
            test_file.write_bytes(new_content)

        def thread_a():
            barrier.wait()  # both threads proceed together
            time.sleep(0.01)  # small delay so B writes first
            result = dispatcher.index_file_guarded(ctx, test_file, observed_hash)
            results.append(result)

        t_b = threading.Thread(target=thread_b)
        t_a = threading.Thread(target=thread_a)
        t_b.start()
        t_a.start()
        t_b.join()
        t_a.join()

        assert len(results) == 1
        result = results[0]
        assert result.status == IndexResultStatus.SKIPPED_TOCTOU
        # plugin must NOT have been invoked
        mock_plugin.indexFile.assert_not_called()

    def test_unchanged_file_returns_skipped_unchanged(self, tmp_path):
        """File already indexed with same content → SKIPPED_UNCHANGED on second call."""
        dispatcher, mock_plugin = _make_dispatcher(tmp_path)
        ctx = _make_ctx(tmp_path)

        test_file = tmp_path / "test.py"
        content = b"x = 1\n"
        test_file.write_bytes(content)
        expected_hash = _bytes_hash(content)

        # First call: INDEXED
        result1 = dispatcher.index_file_guarded(ctx, test_file, expected_hash)
        assert result1.status == IndexResultStatus.INDEXED

        # Second call with same hash and no file modification
        result2 = dispatcher.index_file_guarded(ctx, test_file, expected_hash)
        assert result2.status == IndexResultStatus.SKIPPED_UNCHANGED

    def test_missing_file_returns_error(self, tmp_path):
        """Non-existent file → ERROR result."""
        dispatcher, mock_plugin = _make_dispatcher(tmp_path)
        ctx = _make_ctx(tmp_path)

        missing = tmp_path / "missing.py"
        result = dispatcher.index_file_guarded(ctx, missing, "deadbeef")

        assert result.status == IndexResultStatus.ERROR
        assert result.error is not None


class TestWatcherUsesGuardedDispatch:
    """Watcher integration: modifying a file triggers index_file_guarded with observed hash."""

    def test_watcher_calls_index_file_guarded_with_observed_hash(self, tmp_path):
        """_trigger_reindex_with_ctx should call index_file_guarded with computed observed_hash."""
        from mcp_server.watcher_multi_repo import MultiRepositoryHandler

        ctx = MagicMock(spec=RepoContext)
        ctx.repo_id = "repo1"
        ctx.root = str(tmp_path)
        ctx.workspace_root = tmp_path
        ctx.tracked_branch = "main"

        mock_dispatcher = MagicMock()
        mock_dispatcher.index_file_guarded = MagicMock(
            return_value=IndexResult(
                status=IndexResultStatus.INDEXED,
                path=tmp_path / "test.py",
                observed_hash="abc",
                actual_hash="abc",
            )
        )

        mock_parent = MagicMock()
        mock_parent.dispatcher = mock_dispatcher

        handler = MultiRepositoryHandler.__new__(MultiRepositoryHandler)
        handler.ctx = ctx
        handler.repo_id = "repo1"
        handler.repo_path = tmp_path
        handler.parent_watcher = mock_parent
        handler._inner_handler = MagicMock()
        handler._inner_handler.code_extensions = {".py"}

        # Mock branch check and gitignore filter
        handler._get_current_branch = MagicMock(return_value="main")
        handler._gitignore_filter = MagicMock(return_value=False)

        test_file = tmp_path / "test.py"
        content = b"x = 1\n"
        test_file.write_bytes(content)

        with patch("mcp_server.watcher_multi_repo.lock_registry") as mock_lr:
            mock_lr.acquire.return_value.__enter__ = MagicMock(return_value=None)
            mock_lr.acquire.return_value.__exit__ = MagicMock(return_value=False)
            with patch(
                "mcp_server.watcher_multi_repo.should_reindex_for_branch", return_value=True
            ):
                handler._trigger_reindex_with_ctx(test_file)

        mock_dispatcher.index_file_guarded.assert_called_once()
        call_args = mock_dispatcher.index_file_guarded.call_args
        # Third argument should be the observed hash of the file bytes
        observed_hash_passed = (
            call_args[0][2] if len(call_args[0]) >= 3 else call_args[1].get("expected_hash")
        )
        expected = _bytes_hash(content)
        assert observed_hash_passed == expected
