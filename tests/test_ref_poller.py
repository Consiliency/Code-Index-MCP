"""Tests for RefPoller (SL-2) — tracked-ref poller."""

import subprocess
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from mcp_server.storage.multi_repo_manager import RepositoryInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True
    )
    (repo / "hello.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def _head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_repo_info(repo: Path, commit: str, tracked_branch: str = "main") -> RepositoryInfo:
    index_dir = repo.parent / "index"
    index_dir.mkdir(exist_ok=True)
    return RepositoryInfo(
        repository_id="test-repo-id",
        name="test-repo",
        path=repo,
        index_path=index_dir / "current.db",
        language_stats={},
        total_files=1,
        total_symbols=0,
        indexed_at=datetime.now(),
        last_indexed_commit=commit,
        tracked_branch=tracked_branch,
        current_branch="main",
    )


def _advance_branch(repo: Path) -> str:
    """Make an empty commit on main and return new HEAD SHA."""
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "advance"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return _head_sha(repo)


# ---------------------------------------------------------------------------
# SL-2.1 tests
# ---------------------------------------------------------------------------


def test_ref_advance_triggers_reindex(tmp_path):
    """Poller detects ref advance and calls sync_repository_index."""
    from mcp_server.watcher.ref_poller import RefPoller

    repo = _make_git_repo(tmp_path)
    initial_sha = _head_sha(repo)
    repo_info = _make_repo_info(repo, initial_sha)

    mock_registry = MagicMock()
    mock_registry.list_all.return_value = [repo_info]

    mock_git_index_manager = MagicMock()
    mock_dispatcher = MagicMock()
    mock_resolver = MagicMock()

    poller = RefPoller(
        mock_registry,
        mock_git_index_manager,
        mock_dispatcher,
        mock_resolver,
        interval_seconds=1,
    )
    poller.start()

    try:
        # Advance the branch
        new_sha = _advance_branch(repo)
        # Wait up to 2 * interval_seconds
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if mock_git_index_manager.sync_repository_index.called:
                break
            time.sleep(0.05)

        mock_git_index_manager.sync_repository_index.assert_called_with("test-repo-id")
    finally:
        poller.stop()


def test_unset_tracked_branch_is_skipped(tmp_path):
    """Repos with tracked_branch=None are silently skipped."""
    from mcp_server.watcher.ref_poller import RefPoller

    repo = _make_git_repo(tmp_path)
    initial_sha = _head_sha(repo)
    repo_info = _make_repo_info(repo, initial_sha, tracked_branch=None)

    mock_registry = MagicMock()
    mock_registry.list_all.return_value = [repo_info]
    mock_git_index_manager = MagicMock()

    poller = RefPoller(
        mock_registry,
        mock_git_index_manager,
        MagicMock(),
        MagicMock(),
        interval_seconds=1,
    )
    poller.start()

    try:
        _advance_branch(repo)
        time.sleep(1.5)
        mock_git_index_manager.sync_repository_index.assert_not_called()
    finally:
        poller.stop()


def test_missing_ref_file_is_skipped_with_warning(tmp_path, caplog):
    """Repos whose refs/heads/<branch> doesn't exist are skipped (no crash)."""
    import logging

    from mcp_server.watcher.ref_poller import RefPoller

    repo = _make_git_repo(tmp_path)
    initial_sha = _head_sha(repo)
    # Use a branch name that doesn't exist
    repo_info = _make_repo_info(repo, initial_sha, tracked_branch="nonexistent-branch")

    mock_registry = MagicMock()
    mock_registry.list_all.return_value = [repo_info]
    mock_git_index_manager = MagicMock()

    poller = RefPoller(
        mock_registry,
        mock_git_index_manager,
        MagicMock(),
        MagicMock(),
        interval_seconds=1,
    )

    with caplog.at_level(logging.WARNING):
        poller.start()
        try:
            time.sleep(1.5)
            mock_git_index_manager.sync_repository_index.assert_not_called()
            # Should have logged a warning
            assert any(
                "nonexistent-branch" in r.message or "nonexistent-branch" in str(r)
                for r in caplog.records
            )
        finally:
            poller.stop()


def test_wrong_branch_checkout_is_skipped(tmp_path):
    """Wrong-branch checkout does not schedule sync or full rescan."""
    from mcp_server.watcher.ref_poller import RefPoller

    repo = _make_git_repo(tmp_path)
    initial_sha = _head_sha(repo)
    repo_info = _make_repo_info(repo, initial_sha, tracked_branch="main")
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True, capture_output=True)

    mock_registry = MagicMock()
    mock_registry.list_all.return_value = [repo_info]
    mock_git_index_manager = MagicMock()

    poller = RefPoller(
        mock_registry,
        mock_git_index_manager,
        MagicMock(),
        MagicMock(),
        interval_seconds=1,
    )

    poller._poll_one(repo_info)

    mock_git_index_manager.sync_repository_index.assert_not_called()
    mock_git_index_manager.enqueue_full_rescan.assert_not_called()


def test_read_ref_uses_git_c_for_packed_refs(tmp_path):
    """_read_ref resolves packed refs through git -C instead of direct .git files."""
    from mcp_server.watcher.ref_poller import RefPoller

    repo = _make_git_repo(tmp_path)
    initial_sha = _head_sha(repo)
    subprocess.run(["git", "pack-refs", "--all"], cwd=repo, check=True, capture_output=True)
    loose_ref = repo / ".git" / "refs" / "heads" / "main"
    assert not loose_ref.exists()

    poller = RefPoller(MagicMock(), MagicMock(), MagicMock(), MagicMock())

    assert poller._read_ref(repo, "main") == initial_sha


def test_exception_in_one_repo_does_not_kill_poller(tmp_path):
    """Exception in one repo's poll doesn't stop polling of other repos."""
    from mcp_server.watcher.ref_poller import RefPoller

    # Two repos: first raises, second should still be polled
    r1_base = tmp_path / "r1"
    r1_base.mkdir()
    repo1 = _make_git_repo(r1_base)
    r2_base = tmp_path / "r2"
    r2_base.mkdir()
    repo2 = _make_git_repo(r2_base)

    sha1 = _head_sha(repo1)
    sha2 = _head_sha(repo2)

    info1 = RepositoryInfo(
        repository_id="repo-1",
        name="repo1",
        path=repo1,
        index_path=tmp_path / "idx1" / "current.db",
        language_stats={},
        total_files=1,
        total_symbols=0,
        indexed_at=datetime.now(),
        last_indexed_commit=sha1,
        tracked_branch="main",
        current_branch="main",
    )
    info2 = RepositoryInfo(
        repository_id="repo-2",
        name="repo2",
        path=repo2,
        index_path=tmp_path / "idx2" / "current.db",
        language_stats={},
        total_files=1,
        total_symbols=0,
        indexed_at=datetime.now(),
        last_indexed_commit=sha2,
        tracked_branch="main",
        current_branch="main",
    )

    mock_registry = MagicMock()
    mock_registry.list_all.return_value = [info1, info2]

    mock_git_index_manager = MagicMock()

    # First repo raises, second should still get called
    def side_effect(repo_id):
        if repo_id == "repo-1":
            raise RuntimeError("simulated failure")

    mock_git_index_manager.sync_repository_index.side_effect = side_effect

    poller = RefPoller(
        mock_registry,
        mock_git_index_manager,
        MagicMock(),
        MagicMock(),
        interval_seconds=1,
    )
    poller.start()

    try:
        # Advance both repos
        _advance_branch(repo1)
        _advance_branch(repo2)

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            calls = [c[0][0] for c in mock_git_index_manager.sync_repository_index.call_args_list]
            if "repo-2" in calls:
                break
            time.sleep(0.05)

        calls = [c[0][0] for c in mock_git_index_manager.sync_repository_index.call_args_list]
        assert "repo-2" in calls, "Second repo should still be polled despite first repo error"
    finally:
        poller.stop()


def test_stop_joins_thread_within_one_second(tmp_path):
    """stop() joins the poller thread within 1 second."""
    from mcp_server.watcher.ref_poller import RefPoller

    mock_registry = MagicMock()
    mock_registry.list_all.return_value = []

    poller = RefPoller(
        mock_registry,
        MagicMock(),
        MagicMock(),
        MagicMock(),
        interval_seconds=30,
    )
    poller.start()
    assert poller._thread is not None
    assert poller._thread.is_alive()

    t0 = time.monotonic()
    poller.stop()
    elapsed = time.monotonic() - t0

    assert not poller._thread.is_alive(), "Thread should be dead after stop()"
    assert elapsed < 1.0, f"stop() took {elapsed:.2f}s, expected < 1s"


@pytest.mark.parametrize("disabled_field", ["auto_sync", "active"])
@pytest.mark.parametrize("indexed", [False, True])
def test_disabled_registration_never_probes_or_indexes(
    tmp_path, monkeypatch, disabled_field, indexed
):
    from mcp_server.watcher.ref_poller import RefPoller

    repo = _make_git_repo(tmp_path)
    info = _make_repo_info(repo, _head_sha(repo) if indexed else None)
    _advance_branch(repo)
    setattr(info, disabled_field, False)
    manager = MagicMock()
    poller = RefPoller(MagicMock(), manager, MagicMock(), MagicMock())

    def forbidden(*args):
        pytest.fail("disabled registration reached Git probe")

    monkeypatch.setattr(poller, "_current_branch", forbidden)
    poller._poll_one(info)
    manager.sync_repository_index.assert_not_called()
    manager.enqueue_full_rescan.assert_not_called()
