"""Tests for branch-drift readiness and non-mutating rescan guards.

Covers:
- should_reindex_for_branch still returns False for drift
- drift triggers exactly one `branch.drift.detected` WARN log with all 3 fields
- drift returns wrong_branch readiness without scheduling mutation
- guarded full rescan stays on the tracked branch guard
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from mcp_server.storage.git_index_manager import (
    GitAwareIndexManager,
    should_reindex_for_branch,
)
from mcp_server.watcher_multi_repo import MultiRepositoryWatcher

# ---------------------------------------------------------------------------
# SL-3.1a: should_reindex_for_branch must still return False for drift
# ---------------------------------------------------------------------------


def test_should_reindex_for_branch_returns_false_on_drift():
    """Drift (both non-None, different) must return False — not trigger reindex."""
    assert should_reindex_for_branch("feature/x", "main") is False
    assert should_reindex_for_branch("hotfix/y", "main") is False
    assert should_reindex_for_branch("main", "develop") is False


def test_should_reindex_for_branch_returns_true_on_match():
    assert should_reindex_for_branch("main", "main") is True


def test_should_reindex_for_branch_returns_false_no_tracked():
    """No tracked branch → not drift, just unconfigured."""
    assert should_reindex_for_branch("main", None) is False


def test_should_reindex_for_branch_returns_false_no_current():
    assert should_reindex_for_branch(None, "main") is False


# ---------------------------------------------------------------------------
# SL-3.1b: drift triggers exactly one WARN log with all 3 fields
# ---------------------------------------------------------------------------


def _make_registry_with_drift(
    repo_id="test-repo", current_branch="feature/noise", tracked_branch="main"
):
    """Build a mock registry that simulates branch drift."""
    repo_info = MagicMock()
    repo_info.path = "/tmp/repo"
    repo_info.tracked_branch = tracked_branch
    repo_info.current_branch = current_branch
    repo_info.current_commit = "abc123"
    repo_info.last_indexed_commit = "abc123"
    repo_info.artifact_enabled = False

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {
        "commit": "abc123",
        "branch": current_branch,
    }
    return registry, repo_info


def test_drift_emits_warn_log_with_all_three_fields(caplog):
    """When current != tracked (both non-None), a WARN log is emitted with all 3 extra fields."""
    repo_id = "test-repo"
    registry, _ = _make_registry_with_drift(
        repo_id=repo_id, current_branch="feature/noise", tracked_branch="main"
    )
    enqueue_cb = MagicMock()
    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager.on_branch_drift = enqueue_cb

    with caplog.at_level(logging.WARNING, logger="mcp_server.storage.git_index_manager"):
        manager.sync_repository_index(repo_id)

    drift_records = [r for r in caplog.records if r.getMessage() == "branch.drift.detected"]
    assert len(drift_records) == 1, f"Expected exactly 1 drift log, got {len(drift_records)}"
    record = drift_records[0]
    assert record.levelno == logging.WARNING
    assert record.repo_id == repo_id
    assert record.current_branch == "feature/noise"
    assert record.tracked_branch == "main"


def test_no_drift_log_when_branches_match(caplog):
    """No drift log when current == tracked."""
    repo_id = "test-repo"
    registry, repo_info = _make_registry_with_drift(
        repo_id=repo_id, current_branch="main", tracked_branch="main"
    )
    # current == last_indexed_commit so it'll short-circuit to up_to_date
    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager.on_branch_drift = MagicMock()

    with caplog.at_level(logging.WARNING, logger="mcp_server.storage.git_index_manager"):
        manager.sync_repository_index(repo_id)

    drift_records = [r for r in caplog.records if r.getMessage() == "branch.drift.detected"]
    assert len(drift_records) == 0


def test_no_drift_log_when_no_tracked_branch(caplog):
    """No drift log when tracked_branch is None (unconfigured, not drift)."""
    repo_info = MagicMock()
    repo_info.path = "/tmp/repo"
    repo_info.tracked_branch = None
    repo_info.current_branch = "main"
    repo_info.current_commit = "abc123"
    repo_info.last_indexed_commit = "abc123"
    repo_info.artifact_enabled = False

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": "abc123", "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager.on_branch_drift = MagicMock()

    with caplog.at_level(logging.WARNING, logger="mcp_server.storage.git_index_manager"):
        manager.sync_repository_index("test-repo")

    drift_records = [r for r in caplog.records if r.getMessage() == "branch.drift.detected"]
    assert len(drift_records) == 0
    manager.on_branch_drift.assert_not_called()


# ---------------------------------------------------------------------------
# SL-3.1c: on_branch_drift callback is not invoked by wrong-branch sync
# ---------------------------------------------------------------------------


def test_on_branch_drift_callback_not_called_on_drift():
    """on_branch_drift is diagnostic-only and not called from sync drift."""
    repo_id = "test-repo"
    registry, _ = _make_registry_with_drift(
        repo_id=repo_id, current_branch="feature/noise", tracked_branch="main"
    )
    enqueue_cb = MagicMock()
    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager.on_branch_drift = enqueue_cb

    manager.sync_repository_index(repo_id)

    enqueue_cb.assert_not_called()


def test_on_branch_drift_not_called_on_same_branch():
    """on_branch_drift is NOT called when current == tracked."""
    repo_id = "test-repo"
    registry, repo_info = _make_registry_with_drift(
        repo_id=repo_id, current_branch="main", tracked_branch="main"
    )
    enqueue_cb = MagicMock()
    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager.on_branch_drift = enqueue_cb

    manager.sync_repository_index(repo_id)

    enqueue_cb.assert_not_called()


def test_on_branch_drift_not_called_when_no_callback():
    """sync_repository_index does not raise when on_branch_drift is None."""
    repo_id = "test-repo"
    registry, _ = _make_registry_with_drift(
        repo_id=repo_id, current_branch="feature/noise", tracked_branch="main"
    )
    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    # on_branch_drift intentionally not set (None by default)

    # Should not raise
    manager.sync_repository_index(repo_id)


# ---------------------------------------------------------------------------
# SL-3.1d: enqueue_full_rescan enqueues (not executes inline)
# ---------------------------------------------------------------------------


def _make_watcher(registry=None, index_manager=None):
    """Build a MultiRepositoryWatcher with mocked components."""
    registry = registry or MagicMock()
    dispatcher = MagicMock()
    index_manager = index_manager or MagicMock()
    watcher = MultiRepositoryWatcher(
        registry=registry,
        dispatcher=dispatcher,
        index_manager=index_manager,
    )
    watcher.running = True
    watcher.watchers["my-repo"] = MagicMock()
    watcher.watchers["my-repo"].ctx.registry_entry.path = Path.cwd()
    watcher.watchers["my-repo"].ctx.registry_entry.registration_id = "admitted-registration"
    return watcher


def test_enqueue_full_rescan_submits_to_executor():
    """enqueue_full_rescan must submit work to executor, not execute inline."""
    watcher = _make_watcher()

    with patch.object(watcher.executor, "submit") as mock_submit:
        watcher.enqueue_full_rescan("my-repo")
        mock_submit.assert_called_once()
        # First arg to submit must be callable
        callable_arg = mock_submit.call_args[0][0]
        assert callable(callable_arg)


def test_enqueue_full_rescan_returns_immediately():
    """enqueue_full_rescan returns without blocking (no future.result() call)."""
    watcher = _make_watcher()
    future_mock = MagicMock()
    future_mock.result = MagicMock(side_effect=AssertionError("result() was called — blocking!"))

    with patch.object(watcher.executor, "submit", return_value=future_mock):
        watcher.enqueue_full_rescan("my-repo")  # must not raise


def test_enqueue_full_rescan_passes_repo_id():
    """The callable submitted to executor receives the correct repo_id."""
    index_manager = MagicMock()
    watcher = _make_watcher(index_manager=index_manager)

    submitted_callables = []

    def _capture_submit(fn, *args, **kwargs):
        submitted_callables.append((fn, args, kwargs))
        future = MagicMock()
        return future

    with patch.object(watcher.executor, "submit", side_effect=_capture_submit):
        watcher.enqueue_full_rescan("my-repo")

    assert len(submitted_callables) == 1
    fn, args, kwargs = submitted_callables[0]
    # Call the submitted closure — it should invoke a guarded force-full sync.
    fn(*args, **kwargs)
    index_manager.sync_repository_index.assert_called_once_with(
        "my-repo",
        force_full=True,
        expected_registration_id="admitted-registration",
        require_auto_sync=True,
    )


# ---------------------------------------------------------------------------
# SL-3.1e: watcher wires on_branch_drift as a diagnostic hook
# ---------------------------------------------------------------------------


def test_watcher_wires_drift_callback_to_index_manager():
    """MultiRepositoryWatcher.__init__ sets index_manager.on_branch_drift."""
    index_manager = MagicMock()
    watcher = _make_watcher(index_manager=index_manager)

    assert hasattr(index_manager, "on_branch_drift"), "on_branch_drift must be set on index_manager"
    # The assigned attribute must be callable
    cb = index_manager.on_branch_drift
    assert callable(cb)


def test_watcher_drift_callback_does_not_enqueue_full_rescan():
    """The wired drift callback records diagnostics without scheduling mutation."""
    index_manager = MagicMock()
    watcher = _make_watcher(index_manager=index_manager)

    with patch.object(watcher, "enqueue_full_rescan") as mock_enqueue:
        # Simulate drift: call the wired callback directly
        cb = index_manager.on_branch_drift
        cb("my-repo", "feature/x", "main")
        mock_enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# SL-3.1f: bypass_branch_guard is inert on wrong-branch sync
# ---------------------------------------------------------------------------


def test_bypass_branch_guard_is_inert_on_wrong_branch(caplog):
    """The compatibility bypass parameter must not bypass wrong-branch readiness."""
    repo_id = "test-repo"
    registry, _ = _make_registry_with_drift(
        repo_id=repo_id, current_branch="feature/noise", tracked_branch="main"
    )
    # Mock out _full_index so it returns without actually indexing
    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager.on_branch_drift = MagicMock()
    manager._full_index = MagicMock(return_value=5)
    registry.update_indexed_commit = MagicMock(return_value=True)

    with caplog.at_level(logging.WARNING, logger="mcp_server.storage.git_index_manager"):
        result = manager.sync_repository_index(repo_id, force_full=True, bypass_branch_guard=True)

    drift_records = [r for r in caplog.records if r.getMessage() == "branch.drift.detected"]
    assert len(drift_records) == 1
    assert result.action == "wrong_branch"
    manager.on_branch_drift.assert_not_called()
    manager._full_index.assert_not_called()
