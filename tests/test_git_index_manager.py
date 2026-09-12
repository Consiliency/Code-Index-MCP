"""Tests for GitAwareIndexManager branch-change reindex guard (SL-3)."""

import json
import multiprocessing
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mcp_server.core.repo_context import RepoContext
from mcp_server.dispatcher.dispatcher_enhanced import IndexResult, IndexResultStatus
from mcp_server.health.repository_readiness import (
    ReadinessClassifier,
    RepositoryReadinessState,
)
from mcp_server.storage.git_index_manager import (
    ChangeSet,
    GitAwareIndexManager,
    IndexSyncResult,
    UpdateResult,
    should_reindex_for_branch,
)
from mcp_server.storage.multi_repo_manager import RepositoryInfo
from mcp_server.storage.repository_registry import RepositoryRegistry
from mcp_server.storage.sqlite_store import SQLiteStore
from mcp_server.storage.store_registry import StoreRegistry

# ---------------------------------------------------------------------------
# SL-3.1a: should_reindex_for_branch unit tests
# ---------------------------------------------------------------------------


def test_should_reindex_same_branch():
    assert should_reindex_for_branch("main", "main") is True


def test_should_reindex_different_branch():
    assert should_reindex_for_branch("feature/noise", "main") is False


def test_should_reindex_no_current_branch():
    assert should_reindex_for_branch(None, "main") is False


def test_should_reindex_no_tracked_branch():
    assert should_reindex_for_branch("main", None) is False


def test_sync_all_repositories_signature_no_longer_advertises_parallel():
    params = signature(GitAwareIndexManager.sync_all_repositories).parameters
    assert "parallel" not in params


def test_changed_files_excludes_internal_index_state_and_preserves_rename_edges(
    tmp_path, monkeypatch
):
    detected = [
        SimpleNamespace(change_type="added", path=".mcp-index/trace.json", old_path=None),
        SimpleNamespace(change_type="added", path="src/new.py", old_path=None),
        SimpleNamespace(change_type="modified", path=".venv/cache.py", old_path=None),
        SimpleNamespace(change_type="deleted", path="src/old.py", old_path=None),
        SimpleNamespace(
            change_type="renamed", path="src/from_state.py", old_path=".indexes/from.py"
        ),
        SimpleNamespace(change_type="renamed", path="build/to.py", old_path="src/to_state.py"),
        SimpleNamespace(change_type="renamed", path="src/new_name.py", old_path="src/name.py"),
        SimpleNamespace(
            change_type="renamed", path=".mcp-index/new.json", old_path=".indexes/old.json"
        ),
    ]
    detector = MagicMock()
    detector.get_changes_since_commit.return_value = detected
    monkeypatch.setattr(
        "mcp_server.storage.git_index_manager.ChangeDetector", lambda _repo_path: detector
    )

    changes = GitAwareIndexManager._get_changed_files(
        MagicMock(), tmp_path, "from-commit", "to-commit"
    )

    assert changes.added == ["src/new.py", "src/from_state.py"]
    assert changes.modified == []
    assert changes.deleted == ["src/old.py", "src/to_state.py"]
    assert changes.renamed == [("src/name.py", "src/new_name.py")]


# ---------------------------------------------------------------------------
# Helpers for integration tests
# ---------------------------------------------------------------------------


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a git repo with an initial commit and return its path."""
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


def _get_head_commit(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_repo_info(repo: Path, commit: str) -> RepositoryInfo:
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
        current_commit=commit,
        last_indexed_commit=commit,
        tracked_branch="main",
        current_branch="main",
    )


def _make_manager(registry_mock) -> GitAwareIndexManager:
    return GitAwareIndexManager(registry=registry_mock, dispatcher=MagicMock())


class CtxSignatureDispatcher:
    def __init__(
        self,
        fail_index: set[Path] | None = None,
        index_results: dict[Path, IndexResultStatus] | None = None,
        remove_results: dict[Path, IndexResultStatus] | None = None,
        move_results: dict[tuple[Path, Path], IndexResultStatus] | None = None,
    ) -> None:
        self.calls = []
        self.fail_index = fail_index or set()
        self.index_results = index_results or {}
        self.remove_results = remove_results or {}
        self.move_results = move_results or {}

    def remove_file(self, ctx, path):
        assert isinstance(ctx, RepoContext)
        path = Path(path)
        self.calls.append(("remove", ctx, path))
        status = self.remove_results.get(path, IndexResultStatus.DELETED)
        return IndexResult(status=status, path=path, observed_hash=None, actual_hash=None)

    def move_file(self, ctx, old_path, new_path, content_hash=None):
        assert isinstance(ctx, RepoContext)
        old_path = Path(old_path)
        new_path = Path(new_path)
        self.calls.append(("move", ctx, old_path, new_path, content_hash))
        status = self.move_results.get((old_path, new_path), IndexResultStatus.MOVED)
        return IndexResult(status=status, path=new_path, observed_hash=None, actual_hash=None)

    def index_file(self, ctx, path):
        assert isinstance(ctx, RepoContext)
        path = Path(path)
        if path in self.fail_index:
            return IndexResult(
                status=IndexResultStatus.ERROR,
                path=path,
                observed_hash=None,
                actual_hash=None,
                error="boom",
            )
        self.calls.append(("index", ctx, path))
        status = self.index_results.get(path, IndexResultStatus.INDEXED)
        error = "synthetic status" if status is not IndexResultStatus.INDEXED else None
        return IndexResult(
            status=status, path=path, observed_hash=None, actual_hash=None, error=error
        )

    def index_directory(self, ctx, path, recursive=True):
        assert isinstance(ctx, RepoContext)
        self.calls.append(("index_directory", ctx, Path(path), recursive))
        return {"indexed_files": 1, "failed_files": 0, "errors": []}


def _make_ctx(repo_id: str, repo_path: Path, index_path: Path) -> RepoContext:
    store = SQLiteStore(str(index_path))
    return RepoContext(
        repo_id=repo_id,
        sqlite_store=store,
        workspace_root=repo_path,
        tracked_branch="main",
        registry_entry=MagicMock(repository_id=repo_id, path=repo_path),
    )


def _seed_index(index_path: Path, repo_path: Path, relative_path: str) -> None:
    store = SQLiteStore(str(index_path))
    repo_row = store.ensure_repository_row(repo_path, name="test-repo")
    store.store_file(
        repo_row,
        path=repo_path / relative_path,
        relative_path=relative_path,
    )
    store.close()


class DurableFullIndexDispatcher(CtxSignatureDispatcher):
    def index_directory(self, ctx, path, recursive=True):
        row_id = ctx.sqlite_store.ensure_repository_row(path, name="test-repo")
        ctx.sqlite_store.store_file(
            row_id,
            path=Path(path) / "hello.py",
            relative_path="hello.py",
        )
        return super().index_directory(ctx, path, recursive=recursive)


def _make_rebuild_manager(repo_info: RepositoryInfo, commit: str):
    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}
    registry.update_indexed_commit.return_value = commit

    def update_staleness(_repo_id, reason):
        repo_info.staleness_reason = reason
        return True

    registry.update_staleness_reason.side_effect = update_staleness
    manager = GitAwareIndexManager(registry, DurableFullIndexDispatcher())
    manager._resolve_ctx = MagicMock(return_value=None)
    return manager, registry


def _generation_reader(registry_path, repo_id, channel):
    registry = RepositoryRegistry(registry_path)
    stores = StoreRegistry.for_registry(registry)
    old = stores.get(repo_id)
    try:
        with old._get_connection() as connection:
            connection.execute("BEGIN")
            before = [row[0] for row in connection.execute("SELECT relative_path FROM files")]
            channel.send(before)
            assert channel.poll(10)
            assert channel.recv() == "published"
            held = [row[0] for row in connection.execute("SELECT relative_path FROM files")]
        new = stores.get(repo_id)
        with new._get_connection() as connection:
            after = [row[0] for row in connection.execute("SELECT relative_path FROM files")]
        channel.send((held, after, new is not old))
    finally:
        stores.shutdown()
        channel.close()


def _crash_rebuild(registry_path, repo_id):
    manager = GitAwareIndexManager(RepositoryRegistry(registry_path), DurableFullIndexDispatcher())

    def checkpoint(stage):
        if stage == "after_replacement":
            os._exit(37)

    manager._rebuild_checkpoint = checkpoint
    manager.rebuild_repository_index(repo_id)


@pytest.mark.parametrize("crash", [False, True])
def test_generation_publication_with_external_reader_and_restart(tmp_path, crash):
    repo = _make_git_repo(tmp_path)
    registry = RepositoryRegistry(tmp_path / "registry.json")
    repo_id = registry.register_repository(str(repo))
    info = registry.get(repo_id)
    info.index_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_index(info.index_path, repo, "old.py")
    registry.update_indexed_commit(repo_id, _get_head_commit(repo), branch="main")
    original_path = info.index_path
    original_inode = original_path.stat().st_ino
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    reader = context.Process(
        target=_generation_reader, args=(registry.registry_path, repo_id, child)
    )
    reader.start()
    child.close()
    writer = None
    try:
        assert parent.poll(10)
        assert parent.recv() == ["old.py"]
        if crash:
            writer = context.Process(target=_crash_rebuild, args=(registry.registry_path, repo_id))
            writer.start()
            writer.join(10)
            assert writer.exitcode == 37
            restarted = RepositoryRegistry(registry.registry_path).get(repo_id)
            assert restarted.staleness_reason == "index_publication_pending"
            assert not ReadinessClassifier.classify_registered(restarted).ready
            assert restarted.index_path == original_path
        else:
            manager = GitAwareIndexManager(registry, DurableFullIndexDispatcher())
            result = manager.rebuild_repository_index(repo_id)
            assert result.action == "full_index", result.error
            restarted = RepositoryRegistry(registry.registry_path).get(repo_id)
            assert restarted.index_path != original_path
            assert ReadinessClassifier.classify_registered(restarted).ready
            parent.send("published")
            assert parent.poll(10)
            assert parent.recv() == (["old.py"], ["hello.py"], True)
            reader.join(10)
            assert reader.exitcode == 0
        assert original_path.stat().st_ino == original_inode
    finally:
        parent.close()
        for worker in (reader, writer):
            if worker is not None:
                if worker.is_alive():
                    worker.terminate()
                worker.join(10)
                worker.close()


def test_staged_rebuild_quarantines_unproven_index_and_publishes(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = None
    _seed_index(repo_info.index_path, repo, "old.py")
    manager, registry = _make_rebuild_manager(repo_info, commit)

    result = manager.rebuild_repository_index(repo_info.repository_id)

    assert result.action == "full_index", result.error
    assert result.readiness["previous_state"] == "missing_provenance"
    quarantine_path = Path(result.readiness["quarantine_path"])
    assert quarantine_path.exists()
    with sqlite3.connect(repo_info.index_path) as conn:
        paths = {row[0] for row in conn.execute("SELECT relative_path FROM files")}
    assert paths == {"hello.py"}
    registry.publish_generation.assert_called_once()
    assert registry.publish_generation.call_args.kwargs["commit"] == commit
    assert registry.publish_generation.call_args.kwargs["branch"] == "main"
    assert repo_info.index_path.parent.name == "generations"
    assert not list(repo_info.index_path.parent.glob(".current.db.staging-*"))


def test_staged_rebuild_bootstraps_missing_index(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    manager, _registry = _make_rebuild_manager(repo_info, commit)

    result = manager.rebuild_repository_index(repo_info.repository_id)

    assert result.action == "full_index", result.error
    assert result.readiness["previous_state"] == "missing_index"
    assert ReadinessClassifier.classify_registered(repo_info).ready is True


def test_staged_rebuild_populates_empty_index(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    SQLiteStore(str(repo_info.index_path)).close()
    manager, _registry = _make_rebuild_manager(repo_info, commit)

    result = manager.rebuild_repository_index(repo_info.repository_id)

    assert result.action == "full_index", result.error
    assert result.readiness["previous_state"] == "index_empty"
    assert ReadinessClassifier.classify_registered(repo_info).ready is True


def test_staged_rebuild_refreshes_stale_index(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-commit"
    _seed_index(repo_info.index_path, repo, "old.py")
    manager, _registry = _make_rebuild_manager(repo_info, commit)

    result = manager.rebuild_repository_index(repo_info.repository_id)

    assert result.action == "full_index", result.error
    assert result.readiness["previous_state"] == "stale_commit"
    assert ReadinessClassifier.classify_registered(repo_info).ready is True


def test_staged_rebuild_quarantines_corrupt_bytes_without_reopening_them(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    corrupt_bytes = b"not-a-sqlite-database"
    repo_info.index_path.write_bytes(corrupt_bytes)
    manager, _registry = _make_rebuild_manager(repo_info, commit)

    result = manager.rebuild_repository_index(repo_info.repository_id)

    assert result.action == "full_index", result.error
    assert result.readiness["previous_state"] == "corrupt_sqlite"
    quarantine_path = Path(result.readiness["quarantine_path"])
    assert quarantine_path.read_bytes() == corrupt_bytes
    manager._resolve_ctx.assert_not_called()
    assert ReadinessClassifier.classify_registered(repo_info).ready is True


def test_staged_rebuild_failure_before_replacement_preserves_active_bytes(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = None
    _seed_index(repo_info.index_path, repo, "old.py")
    original = repo_info.index_path.read_bytes()
    manager, registry = _make_rebuild_manager(repo_info, commit)

    def checkpoint(stage):
        if stage == "before_replacement":
            raise RuntimeError(stage)

    manager._rebuild_checkpoint = MagicMock(side_effect=checkpoint)

    result = manager.rebuild_repository_index(repo_info.repository_id)

    assert result.action == "failed"
    assert repo_info.index_path.read_bytes() == original
    registry.update_indexed_commit.assert_not_called()
    assert repo_info.staleness_reason == "partial_index_failure"


@pytest.mark.parametrize("failure_stage", ["after_replacement", "before_provenance"])
def test_staged_rebuild_interrupted_publication_remains_non_ready(tmp_path, failure_stage):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = None
    _seed_index(repo_info.index_path, repo, "old.py")
    manager, registry = _make_rebuild_manager(repo_info, commit)

    def checkpoint(stage):
        if stage == failure_stage:
            raise RuntimeError(stage)

    manager._rebuild_checkpoint = MagicMock(side_effect=checkpoint)

    result = manager.rebuild_repository_index(repo_info.repository_id)

    assert result.action == "failed"
    assert repo_info.staleness_reason == "partial_index_failure"
    readiness = ReadinessClassifier.classify_registered(repo_info)
    assert readiness.state == RepositoryReadinessState.MISSING_PROVENANCE
    registry.update_indexed_commit.assert_not_called()


def test_staged_rebuild_refuses_wrong_branch_without_mutation(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    subprocess.run(["git", "checkout", "-qb", "feature"], cwd=repo, check=True)
    repo_info = _make_repo_info(repo, commit)
    repo_info.current_branch = "feature"
    _seed_index(repo_info.index_path, repo, "old.py")
    original = repo_info.index_path.read_bytes()
    manager, registry = _make_rebuild_manager(repo_info, commit)
    registry.update_git_state.return_value = {"commit": commit, "branch": "feature"}

    result = manager.rebuild_repository_index(repo_info.repository_id)

    assert result.action == "refused"
    assert result.code == "wrong_branch"
    assert repo_info.index_path.read_bytes() == original
    registry.update_indexed_commit.assert_not_called()


def test_staged_rebuild_requires_pending_marker_before_replacement(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = None
    _seed_index(repo_info.index_path, repo, "old.py")
    original = repo_info.index_path.read_bytes()
    manager, registry = _make_rebuild_manager(repo_info, commit)
    registry.begin_generation_mutation.side_effect = OSError("injected pending fence failure")

    result = manager.rebuild_repository_index(repo_info.repository_id)

    assert result.action == "failed"
    assert repo_info.index_path.read_bytes() == original
    registry.update_indexed_commit.assert_not_called()


def test_rebuild_repository_index_serializes_same_repo(tmp_path):
    manager = GitAwareIndexManager(MagicMock(), MagicMock())
    manager.registry.get_repository.return_value.path = tmp_path
    concurrent = 0
    max_concurrent = 0
    counter_lock = threading.Lock()

    def rebuild(_repo_id):
        nonlocal concurrent, max_concurrent
        with counter_lock:
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
        time.sleep(0.05)
        with counter_lock:
            concurrent -= 1
        return IndexSyncResult(action="full_index", commit="commit")

    manager._rebuild_repository_index_locked = rebuild
    threads = [
        threading.Thread(target=manager.rebuild_repository_index, args=("same-repo",))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert max_concurrent == 1


# ---------------------------------------------------------------------------
# SL-3.1b: Integration — branch-switch yields wrong_branch
# ---------------------------------------------------------------------------


def test_branch_switch_yields_wrong_branch(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)

    repo_info = _make_repo_info(repo, commit)
    # Simulate switching to feature branch
    subprocess.run(
        ["git", "checkout", "-b", "feature/noise"], cwd=repo, check=True, capture_output=True
    )
    (repo / "hello.py").write_text("print('changed')\n")

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    feature_commit = _get_head_commit(repo)
    registry.update_git_state.return_value = {
        "commit": feature_commit,
        "branch": "feature/noise",
    }

    manager = _make_manager(registry)
    manager._full_index = MagicMock(return_value=0)
    manager._incremental_index_update = MagicMock()

    manager._has_remote_artifact = MagicMock()
    manager._download_commit_index = MagicMock()
    manager.on_branch_drift = MagicMock()

    result = manager.sync_repository_index("test-repo-id")

    assert result.action == "wrong_branch", f"Expected wrong_branch, got {result.action!r}"
    assert result.code == "wrong_branch"
    assert result.readiness is not None
    assert result.readiness["state"] == "wrong_branch"
    assert manager._full_index.call_count == 0, "_full_index should not be called on branch switch"
    assert (
        manager._incremental_index_update.call_count == 0
    ), "_incremental_index_update should not be called on branch switch"
    manager._has_remote_artifact.assert_not_called()
    manager._download_commit_index.assert_not_called()
    registry.update_indexed_commit.assert_not_called()
    manager.on_branch_drift.assert_not_called()


# ---------------------------------------------------------------------------
# SL-3.1c: Integration — same-branch advance still triggers incremental
# ---------------------------------------------------------------------------


def test_same_branch_advance_triggers_incremental(tmp_path):
    repo = _make_git_repo(tmp_path)
    old_commit = _get_head_commit(repo)

    repo_info = _make_repo_info(repo, old_commit)
    repo_info.index_path.touch()

    # Make a new commit on main
    (repo / "hello.py").write_text("print('updated')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "update"], cwd=repo, check=True, capture_output=True)
    new_commit = _get_head_commit(repo)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": new_commit, "branch": "main"}
    registry.update_indexed_commit.return_value = True

    manager = _make_manager(registry)
    manager._full_index = MagicMock(return_value=0)
    manager._should_full_reindex = MagicMock(return_value=False)

    from mcp_server.storage.git_index_manager import UpdateResult

    incremental_mock = MagicMock(return_value=UpdateResult(indexed=1))
    manager._incremental_index_update = incremental_mock

    manager.sync_repository_index("test-repo-id")

    assert (
        incremental_mock.call_count == 1
    ), f"_incremental_index_update should be called once; got {incremental_mock.call_count}"


def test_incremental_update_uses_same_ctx_for_all_dispatcher_mutations(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.index_path.touch()
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    (repo / "added.py").write_text("print('added')\n")
    (repo / "renamed.py").write_text("print('renamed')\n")

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    dispatcher = CtxSignatureDispatcher()
    manager = GitAwareIndexManager(registry, dispatcher)

    result = manager._incremental_index_update(
        repo_info.repository_id,
        ctx,
        ChangeSet(
            added=["added.py"],
            modified=[],
            deleted=["deleted.py"],
            renamed=[("old.py", "renamed.py")],
        ),
    )

    assert result.clean
    assert [call[0] for call in dispatcher.calls] == ["remove", "move", "index"]
    assert all(call[1] is ctx for call in dispatcher.calls)


def test_incremental_update_skipped_required_index_is_not_clean(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.index_path.touch()
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)
    changed_file = repo / "hello.py"

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    dispatcher = CtxSignatureDispatcher(
        index_results={changed_file: IndexResultStatus.SKIPPED_UNCHANGED}
    )
    manager = GitAwareIndexManager(registry, dispatcher)

    result = manager._incremental_index_update(
        repo_info.repository_id,
        ctx,
        ChangeSet(added=[], modified=["hello.py"], deleted=[], renamed=[]),
    )

    assert not result.clean
    assert result.indexed == 0
    assert result.skipped == 1
    assert any("hello.py" in error for error in result.errors)


def test_sync_repository_index_persists_partial_index_failure(tmp_path):
    repo = _make_git_repo(tmp_path)
    old_commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, old_commit)
    repo_info.index_path.touch()

    (repo / "hello.py").write_text("print('updated')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "update"], cwd=repo, check=True, capture_output=True)
    new_commit = _get_head_commit(repo)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": new_commit, "branch": "main"}

    manager = _make_manager(registry)
    manager._should_full_reindex = MagicMock(return_value=False)
    manager._incremental_index_update = MagicMock(
        return_value=UpdateResult(skipped=1, errors=["required mutation skipped"])
    )

    result = manager.sync_repository_index("test-repo-id")

    assert result.action == "failed"
    registry.begin_generation_mutation.assert_called_once_with(
        repo_info.repository_id,
        expected_registration_id=repo_info.registration_id,
        expected_generation=repo_info.index_generation,
    )
    registry.update_staleness_reason.assert_called_with("test-repo-id", "partial_index_failure")


def test_full_index_without_durable_rows_does_not_advance_commit(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = None
    repo_info.index_path.touch()

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = _make_manager(registry)
    manager._resolve_ctx = MagicMock(
        return_value=_make_ctx(repo_info.repository_id, repo, repo_info.index_path)
    )
    manager._full_index = MagicMock(return_value=UpdateResult(indexed=1))

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    assert result.action == "failed"
    assert result.error == "Staged rebuild failed (RuntimeError)"
    registry.begin_generation_mutation.assert_called_once_with(
        repo_info.repository_id,
        expected_registration_id=repo_info.registration_id,
        expected_generation=repo_info.index_generation,
    )
    registry.update_staleness_reason.assert_called_with(
        repo_info.repository_id, "partial_index_failure"
    )
    registry.update_indexed_commit.assert_not_called()


def test_incremental_missing_rename_destination_is_clean_only_after_delete_success(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.index_path.touch()
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    dispatcher = CtxSignatureDispatcher()
    manager = GitAwareIndexManager(registry, dispatcher)

    result = manager._incremental_index_update(
        repo_info.repository_id,
        ctx,
        ChangeSet(added=[], modified=[], deleted=[], renamed=[("old.py", "new.py")]),
    )

    assert result.clean
    assert result.deleted == 1
    assert result.moved == 0


def test_incremental_missing_rename_destination_not_clean_when_delete_not_found(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.index_path.touch()
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)
    old_full = repo / "old.py"

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    dispatcher = CtxSignatureDispatcher(remove_results={old_full: IndexResultStatus.NOT_FOUND})
    manager = GitAwareIndexManager(registry, dispatcher)

    result = manager._incremental_index_update(
        repo_info.repository_id,
        ctx,
        ChangeSet(added=[], modified=[], deleted=[], renamed=[("old.py", "new.py")]),
    )

    assert not result.clean
    assert result.deleted == 0
    assert result.failed == 1


def test_skipped_required_incremental_result_does_not_advance_commit(tmp_path):
    repo = _make_git_repo(tmp_path)
    old_commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, old_commit)
    repo_info.index_path.touch()

    changed_file = repo / "hello.py"
    changed_file.write_text("print('updated')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "update"], cwd=repo, check=True, capture_output=True)
    new_commit = _get_head_commit(repo)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": new_commit, "branch": "main"}

    manager = GitAwareIndexManager(
        registry,
        CtxSignatureDispatcher(index_results={changed_file: IndexResultStatus.SKIPPED_UNCHANGED}),
    )
    manager._get_changed_files = MagicMock(
        return_value=ChangeSet(added=[], modified=["hello.py"], deleted=[], renamed=[])
    )
    manager._should_full_reindex = MagicMock(return_value=False)

    result = manager.sync_repository_index(repo_info.repository_id)

    assert result.action == "failed"
    registry.update_indexed_commit.assert_not_called()


def test_missing_index_does_not_incremental_dry_run_or_advance_commit(tmp_path):
    repo = _make_git_repo(tmp_path)
    old_commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, old_commit)
    repo_info.index_path.unlink(missing_ok=True)

    (repo / "hello.py").write_text("print('updated')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "update"], cwd=repo, check=True, capture_output=True)
    new_commit = _get_head_commit(repo)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": new_commit, "branch": "main"}
    registry.update_indexed_commit.return_value = True

    manager = GitAwareIndexManager(registry, CtxSignatureDispatcher())
    manager._get_changed_files = MagicMock(
        return_value=ChangeSet(added=[], modified=["hello.py"], deleted=[], renamed=[])
    )
    manager._should_full_reindex = MagicMock(return_value=False)
    manager._incremental_index_update = MagicMock(wraps=manager._incremental_index_update)
    full_result = UpdateResult(indexed=1)
    manager._full_index = MagicMock(return_value=full_result)

    result = manager.sync_repository_index(repo_info.repository_id)

    assert manager._incremental_index_update.call_count == 0
    assert manager._full_index.call_count == 1
    assert result.action in {"full_index", "failed"}
    assert result.action != "incremental_update"


def test_incremental_partial_failure_does_not_advance_commit(tmp_path):
    repo = _make_git_repo(tmp_path)
    old_commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, old_commit)
    repo_info.index_path.touch()

    failing_file = repo / "hello.py"
    failing_file.write_text("print('updated')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "update"], cwd=repo, check=True, capture_output=True)
    new_commit = _get_head_commit(repo)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": new_commit, "branch": "main"}

    manager = GitAwareIndexManager(
        registry,
        CtxSignatureDispatcher(fail_index={failing_file}),
    )
    manager._get_changed_files = MagicMock(
        return_value=ChangeSet(added=[], modified=["hello.py"], deleted=[], renamed=[])
    )
    manager._should_full_reindex = MagicMock(return_value=False)

    result = manager.sync_repository_index(repo_info.repository_id)

    assert result.action == "failed"
    registry.update_indexed_commit.assert_not_called()


def test_clean_full_rebuild_advances_commit_only_with_durable_index(tmp_path):
    repo = _make_git_repo(tmp_path)
    old_commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, old_commit)
    repo_info.last_indexed_commit = None
    repo_info.index_path.touch()

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": old_commit, "branch": "main"}
    registry.update_indexed_commit.return_value = True

    class DurableFullIndexDispatcher(CtxSignatureDispatcher):
        def index_directory(self, ctx, path, recursive=True):
            row_id = ctx.sqlite_store.ensure_repository_row(path, name="test-repo")
            ctx.sqlite_store.store_file(
                row_id, path=Path(path) / "hello.py", relative_path="hello.py"
            )
            return super().index_directory(ctx, path, recursive=recursive)

    manager = GitAwareIndexManager(registry, DurableFullIndexDispatcher())

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    assert result.action == "full_index"
    registry.publish_generation.assert_called_once()
    publication = registry.publish_generation.call_args.kwargs
    assert publication["commit"] == old_commit
    assert publication["branch"] == "main"
    assert publication["index_path"] == repo_info.index_path
    assert publication["generation"] == repo_info.index_generation


def test_full_index_returns_exact_semantic_blocker_when_summaries_still_missing(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    dispatcher = MagicMock()
    dispatcher.index_directory.return_value = {
        "indexed_files": 1,
        "failed_files": 0,
        "errors": [],
        "summaries_written": 1,
        "summary_chunks_attempted": 2,
        "summary_missing_chunks": 1,
        "semantic_indexed": 0,
        "semantic_failed": 0,
        "semantic_skipped": 0,
        "semantic_blocked": 1,
        "semantic_stage": "blocked_missing_summaries",
        "semantic_error": "Missing authoritative summaries blocked strict semantic indexing",
    }

    manager = GitAwareIndexManager(registry=registry, dispatcher=dispatcher)
    result = manager._full_index(repo_info.repository_id, ctx)

    assert result.indexed == 1
    assert result.failed == 0
    assert not result.clean
    assert result.semantic is not None
    assert result.semantic["semantic_blocked"] == 1
    assert result.semantic["semantic_stage"] == "blocked_missing_summaries"
    assert result.errors == ["Missing authoritative summaries blocked strict semantic indexing"]


def test_full_index_returns_exact_semantic_blocker_when_summary_progress_plateaus(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    dispatcher = MagicMock()
    dispatcher.index_directory.return_value = {
        "indexed_files": 1,
        "failed_files": 0,
        "errors": [],
        "summaries_written": 12,
        "summary_chunks_attempted": 12,
        "summary_missing_chunks": 4,
        "summary_passes": 3,
        "semantic_indexed": 0,
        "semantic_failed": 0,
        "semantic_skipped": 0,
        "semantic_blocked": 1,
        "semantic_stage": "blocked_summary_plateau",
        "semantic_error": "Summary generation plateaued before strict semantic indexing could start",
    }

    manager = GitAwareIndexManager(registry=registry, dispatcher=dispatcher)
    result = manager._full_index(repo_info.repository_id, ctx)

    assert result.indexed == 1
    assert not result.clean
    assert result.semantic is not None
    assert result.semantic["summary_passes"] == 3
    assert result.semantic["semantic_stage"] == "blocked_summary_plateau"
    assert result.errors == [
        "Summary generation plateaued before strict semantic indexing could start"
    ]


def test_full_index_preserves_exact_summary_call_timeout_details(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    dispatcher = MagicMock()
    dispatcher.index_directory.return_value = {
        "indexed_files": 1,
        "failed_files": 0,
        "errors": [],
        "summaries_written": 0,
        "summary_chunks_attempted": 1,
        "summary_missing_chunks": 4,
        "summary_passes": 0,
        "summary_remaining_chunks": 4,
        "summary_scope_drained": False,
        "summary_call_timed_out": True,
        "summary_call_file_path": str(repo / "README.md"),
        "summary_call_chunk_ids": ["chunk-1"],
        "summary_call_timeout_seconds": 30.0,
        "semantic_indexed": 0,
        "semantic_failed": 0,
        "semantic_skipped": 0,
        "semantic_blocked": 1,
        "semantic_stage": "blocked_summary_call_timeout",
        "semantic_error": (
            "Authoritative summary call timed out after 30 seconds before any summary was "
            f"written for {repo / 'README.md'}; 4 chunks still require summaries"
        ),
    }

    manager = GitAwareIndexManager(registry=registry, dispatcher=dispatcher)
    result = manager._full_index(repo_info.repository_id, ctx)

    assert not result.clean
    assert result.semantic is not None
    assert result.semantic["summary_call_timed_out"] is True
    assert result.semantic["summary_call_file_path"] == str(repo / "README.md")
    assert result.semantic["summary_call_chunk_ids"] == ["chunk-1"]
    assert result.semantic["summary_call_timeout_seconds"] == 30.0
    assert result.semantic["semantic_stage"] == "blocked_summary_call_timeout"


def test_force_full_timeout_fences_active_runtime_and_preserves_exact_blocker(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    index_base = Path(repo_info.index_location)
    index_base.mkdir(parents=True, exist_ok=True)

    active_store = SQLiteStore(str(repo_info.index_path))
    repo_row = active_store.ensure_repository_row(repo, name="test-repo")
    active_store.store_file(repo_row, path=repo / "hello.py", relative_path="hello.py")
    active_store.close()
    semantic_qdrant = index_base / "semantic_qdrant"
    semantic_qdrant.mkdir(parents=True, exist_ok=True)
    (semantic_qdrant / "marker.txt").write_text("original", encoding="utf-8")

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = _make_manager(registry)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)
    manager._resolve_ctx = MagicMock(return_value=ctx)

    def _mutating_full_index(_repo_id, active_ctx):
        assert active_ctx.staging
        assert Path(active_ctx.sqlite_store.db_path) != repo_info.index_path
        mutated_store = active_ctx.sqlite_store
        mutated_repo_row = mutated_store.ensure_repository_row(repo, name="test-repo")
        mutated_store.store_file(
            mutated_repo_row,
            path=repo / "extra.py",
            relative_path="extra.py",
        )
        staged_vectors = Path(active_ctx.sqlite_store.db_path).with_suffix(".semantic")
        staged_vectors.mkdir()
        (staged_vectors / "marker.txt").write_text("mutated", encoding="utf-8")
        return UpdateResult(
            indexed=2,
            errors=[
                "Authoritative summary call timed out after 30 seconds before any summary was "
                f"written for {repo / 'README.md'}; 4 chunks still require summaries"
            ],
            semantic={
                "summaries_written": 0,
                "summary_chunks_attempted": 1,
                "summary_missing_chunks": 4,
                "summary_passes": 0,
                "summary_remaining_chunks": 4,
                "summary_scope_drained": False,
                "summary_call_timed_out": True,
                "summary_call_file_path": str(repo / "README.md"),
                "summary_call_chunk_ids": ["chunk-1"],
                "summary_call_timeout_seconds": 30.0,
                "semantic_indexed": 0,
                "semantic_failed": 0,
                "semantic_skipped": 0,
                "semantic_blocked": 1,
                "semantic_stage": "blocked_summary_call_timeout",
            },
        )

    manager._full_index = MagicMock(side_effect=_mutating_full_index)

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    assert result.action == "failed"
    assert result.error == "Staged rebuild failed (RuntimeError)"
    assert "runtime restored via" not in result.error
    assert result.semantic is not None
    assert result.semantic["summary_call_timed_out"] is True
    assert result.semantic["semantic_stage"] == "blocked_summary_call_timeout"
    restored_store = SQLiteStore(str(repo_info.index_path))
    with restored_store._get_connection() as conn:
        files_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        file_paths = {row[0] for row in conn.execute("SELECT relative_path FROM files").fetchall()}
        semantic_points = conn.execute("SELECT COUNT(*) FROM semantic_points").fetchone()[0]
    restored_store.close()
    assert files_count == 1
    assert file_paths == {"hello.py"}
    assert semantic_points == 0
    assert (semantic_qdrant / "marker.txt").read_text(encoding="utf-8") == "original"
    registry.update_indexed_commit.assert_not_called()
    registry.update_staleness_reason.assert_called_with(
        repo_info.repository_id, "partial_index_failure"
    )


def test_force_full_storage_closeout_fences_runtime_and_preserves_exact_blocker(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    index_base = Path(repo_info.index_location)
    index_base.mkdir(parents=True, exist_ok=True)
    semantic_qdrant = index_base / "semantic_qdrant"
    semantic_qdrant.mkdir(parents=True)
    (semantic_qdrant / "marker.txt").write_text("original", encoding="utf-8")

    base_store = SQLiteStore(str(repo_info.index_path))
    base_repo_row = base_store.ensure_repository_row(repo, name="test-repo")
    base_store.store_file(
        base_repo_row,
        path=repo / "hello.py",
        relative_path="hello.py",
    )
    base_store.close()

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)
    manager._resolve_ctx = MagicMock(return_value=ctx)

    def _mutating_full_index(_repo_id, active_ctx):
        assert active_ctx.staging
        assert Path(active_ctx.sqlite_store.db_path) != repo_info.index_path
        mutated_store = active_ctx.sqlite_store
        mutated_repo_row = mutated_store.ensure_repository_row(repo, name="test-repo")
        mutated_store.store_file(
            mutated_repo_row,
            path=repo / "extra.py",
            relative_path="extra.py",
        )
        staged_vectors = Path(active_ctx.sqlite_store.db_path).with_suffix(".semantic")
        staged_vectors.mkdir()
        (staged_vectors / "marker.txt").write_text("mutated", encoding="utf-8")
        return UpdateResult(
            indexed=2,
            failed=1,
            errors=["disk I/O error"],
            semantic={
                "summaries_written": 0,
                "summary_chunks_attempted": 0,
                "summary_missing_chunks": 0,
                "summary_passes": 0,
                "summary_remaining_chunks": 0,
                "summary_scope_drained": True,
                "summary_call_timed_out": False,
                "semantic_indexed": 0,
                "semantic_failed": 0,
                "semantic_skipped": 0,
                "semantic_blocked": 1,
                "semantic_stage": "blocked_storage_error",
                "semantic_error": "disk I/O error",
                "semantic_blocker": {
                    "code": "storage_closeout",
                    "message": "disk I/O error",
                },
                "storage_failure_family": "sqlite_operational",
                "storage_failure_reason": "disk_io_error",
                "storage_failure_message": "disk I/O error",
                "storage_diagnostics": {
                    "status": "degraded",
                    "journal_mode": "WAL",
                    "readonly": True,
                },
            },
        )

    manager._full_index = MagicMock(side_effect=_mutating_full_index)

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert result.error == "Staged rebuild failed (RuntimeError)"
    assert "runtime restored via" not in result.error
    assert result.semantic is not None
    assert result.semantic["storage_failure_reason"] == "disk_io_error"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "storage_closeout"
    assert trace["storage_failure_family"] == "sqlite_operational"
    assert trace["storage_failure_reason"] == "disk_io_error"
    assert trace["runtime_restore_performed"] is False
    restored_store = SQLiteStore(str(repo_info.index_path))
    with restored_store._get_connection() as conn:
        files_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        file_paths = {row[0] for row in conn.execute("SELECT relative_path FROM files").fetchall()}
        semantic_points = conn.execute("SELECT COUNT(*) FROM semantic_points").fetchone()[0]
    restored_store.close()
    assert files_count == 1
    assert file_paths == {"hello.py"}
    assert semantic_points == 0
    assert (semantic_qdrant / "marker.txt").read_text(encoding="utf-8") == "original"
    registry.update_indexed_commit.assert_not_called()
    registry.update_staleness_reason.assert_called_with(
        repo_info.repository_id, "partial_index_failure"
    )


def test_full_index_preserves_bounded_summary_continuation_details(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    dispatcher = MagicMock()
    dispatcher.index_directory.return_value = {
        "indexed_files": 2,
        "failed_files": 0,
        "errors": [],
        "summaries_written": 8,
        "summary_chunks_attempted": 8,
        "summary_missing_chunks": 17,
        "summary_passes": 8,
        "summary_remaining_chunks": 17,
        "summary_scope_drained": False,
        "summary_continuation_required": True,
        "semantic_indexed": 0,
        "semantic_failed": 0,
        "semantic_skipped": 0,
        "semantic_blocked": 2,
        "semantic_stage": "blocked_missing_summaries",
        "semantic_error": (
            "Missing authoritative summaries blocked strict semantic indexing "
            "after 8 bounded summary passes; 17 chunks still require summaries"
        ),
    }

    manager = GitAwareIndexManager(registry=registry, dispatcher=dispatcher)
    result = manager._full_index(repo_info.repository_id, ctx)

    assert result.indexed == 2
    assert not result.clean
    assert result.semantic is not None
    assert result.semantic["summary_passes"] == 8
    assert result.semantic["summary_remaining_chunks"] == 17
    assert result.semantic["summary_scope_drained"] is False
    assert result.semantic["summary_continuation_required"] is True
    assert result.errors == [
        "Missing authoritative summaries blocked strict semantic indexing after "
        "8 bounded summary passes; 17 chunks still require summaries"
    ]


def test_full_index_returns_exact_low_level_blocker_when_lexical_stage_times_out(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    dispatcher = MagicMock()
    dispatcher.index_directory.return_value = {
        "indexed_files": 0,
        "failed_files": 1,
        "errors": [],
        "lexical_stage": "blocked_file_timeout",
        "lexical_files_attempted": 1,
        "lexical_files_completed": 0,
        "last_progress_path": None,
        "in_flight_path": str(repo / "hello.py"),
        "semantic_stage": "not_run",
        "low_level_blocker": {
            "code": "lexical_file_timeout",
            "message": "Lexical indexing timed out while processing hello.py",
        },
        "storage_diagnostics": {
            "journal_mode": "WAL",
            "busy_timeout_ms": 5000,
        },
    }

    manager = GitAwareIndexManager(registry=registry, dispatcher=dispatcher)
    result = manager._full_index(repo_info.repository_id, ctx)

    assert result.indexed == 0
    assert not result.clean
    assert result.low_level is not None
    assert result.low_level["lexical_stage"] == "blocked_file_timeout"
    assert result.low_level["low_level_blocker"]["code"] == "lexical_file_timeout"
    assert result.errors == ["Lexical indexing timed out while processing hello.py"]


def test_full_index_preserves_semantic_ready_stats_when_force_full_rebuild_succeeds(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    dispatcher = MagicMock()
    dispatcher.index_directory.return_value = {
        "indexed_files": 2,
        "failed_files": 0,
        "errors": [],
        "summaries_written": 8,
        "summary_chunks_attempted": 8,
        "summary_missing_chunks": 0,
        "semantic_indexed": 2,
        "semantic_failed": 0,
        "semantic_skipped": 0,
        "semantic_blocked": 0,
        "semantic_stage": "indexed",
        "semantic_error": None,
    }

    manager = GitAwareIndexManager(registry=registry, dispatcher=dispatcher)
    result = manager._full_index(repo_info.repository_id, ctx)

    assert result.indexed == 2
    assert result.failed == 0
    assert result.clean
    assert result.semantic is not None
    assert result.semantic["summaries_written"] == 8
    assert result.semantic["summary_chunks_attempted"] == 8
    assert result.semantic["summary_missing_chunks"] == 0
    assert result.semantic["semantic_indexed"] == 2
    assert result.semantic["semantic_blocked"] == 0
    assert result.semantic["semantic_stage"] == "indexed"


def test_full_index_carries_forward_downstream_blocker_after_changelog_repair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    dispatcher = MagicMock()
    dispatcher.index_directory.return_value = {
        "indexed_files": 2,
        "failed_files": 0,
        "errors": [],
        "lexical_stage": "completed",
        "lexical_files_attempted": 2,
        "lexical_files_completed": 2,
        "last_progress_path": str(repo / "CHANGELOG.md"),
        "in_flight_path": None,
        "summaries_written": 0,
        "summary_chunks_attempted": 2,
        "summary_missing_chunks": 2,
        "semantic_indexed": 0,
        "semantic_failed": 0,
        "semantic_skipped": 0,
        "semantic_blocked": 1,
        "semantic_stage": "blocked_missing_summaries",
        "semantic_error": "Missing authoritative summaries blocked strict semantic indexing",
    }

    manager = GitAwareIndexManager(registry=registry, dispatcher=dispatcher)
    result = manager._full_index(repo_info.repository_id, ctx)

    assert result.indexed == 2
    assert not result.clean
    assert result.low_level is None
    assert result.semantic is not None
    assert result.semantic["semantic_stage"] == "blocked_missing_summaries"
    assert result.errors == ["Missing authoritative summaries blocked strict semantic indexing"]


def test_full_index_carries_forward_downstream_blocker_after_roadmap_repair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    dispatcher = MagicMock()
    dispatcher.index_directory.return_value = {
        "indexed_files": 2,
        "failed_files": 0,
        "errors": [],
        "lexical_stage": "completed",
        "lexical_files_attempted": 2,
        "lexical_files_completed": 2,
        "last_progress_path": str(repo / "ROADMAP.md"),
        "in_flight_path": None,
        "summaries_written": 0,
        "summary_chunks_attempted": 2,
        "summary_missing_chunks": 2,
        "semantic_indexed": 0,
        "semantic_failed": 0,
        "semantic_skipped": 0,
        "semantic_blocked": 1,
        "semantic_stage": "blocked_missing_summaries",
        "semantic_error": "Missing authoritative summaries blocked strict semantic indexing",
    }

    manager = GitAwareIndexManager(registry=registry, dispatcher=dispatcher)
    result = manager._full_index(repo_info.repository_id, ctx)

    assert result.indexed == 2
    assert not result.clean
    assert result.low_level is None
    assert result.semantic is not None
    assert result.semantic["semantic_stage"] == "blocked_missing_summaries"
    assert result.errors == ["Missing authoritative summaries blocked strict semantic indexing"]


def test_full_index_carries_forward_downstream_blocker_after_final_analysis_repair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    dispatcher = MagicMock()
    dispatcher.index_directory.return_value = {
        "indexed_files": 2,
        "failed_files": 0,
        "errors": [],
        "lexical_stage": "completed",
        "lexical_files_attempted": 2,
        "lexical_files_completed": 2,
        "last_progress_path": str(repo / "FINAL_COMPREHENSIVE_MCP_ANALYSIS.md"),
        "in_flight_path": None,
        "summaries_written": 0,
        "summary_chunks_attempted": 2,
        "summary_missing_chunks": 2,
        "semantic_indexed": 0,
        "semantic_failed": 0,
        "semantic_skipped": 0,
        "semantic_blocked": 1,
        "semantic_stage": "blocked_missing_summaries",
        "semantic_error": "Missing authoritative summaries blocked strict semantic indexing",
    }

    manager = GitAwareIndexManager(registry=registry, dispatcher=dispatcher)
    result = manager._full_index(repo_info.repository_id, ctx)

    assert result.indexed == 2
    assert not result.clean
    assert result.low_level is None
    assert result.semantic is not None
    assert result.semantic["semantic_stage"] == "blocked_missing_summaries"
    assert result.errors == ["Missing authoritative summaries blocked strict semantic indexing"]


def test_full_index_carries_forward_downstream_blocker_after_agents_repair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    dispatcher = MagicMock()
    dispatcher.index_directory.return_value = {
        "indexed_files": 2,
        "failed_files": 0,
        "errors": [],
        "lexical_stage": "completed",
        "lexical_files_attempted": 2,
        "lexical_files_completed": 2,
        "last_progress_path": str(repo / "AGENTS.md"),
        "in_flight_path": None,
        "summaries_written": 0,
        "summary_chunks_attempted": 2,
        "summary_missing_chunks": 2,
        "semantic_indexed": 0,
        "semantic_failed": 0,
        "semantic_skipped": 0,
        "semantic_blocked": 1,
        "semantic_stage": "blocked_missing_summaries",
        "semantic_error": "Missing authoritative summaries blocked strict semantic indexing",
    }

    manager = GitAwareIndexManager(registry=registry, dispatcher=dispatcher)
    result = manager._full_index(repo_info.repository_id, ctx)

    assert result.indexed == 2
    assert not result.clean
    assert result.low_level is None
    assert result.semantic is not None
    assert result.semantic["semantic_stage"] == "blocked_missing_summaries"
    assert result.errors == ["Missing authoritative summaries blocked strict semantic indexing"]


def test_full_index_carries_forward_downstream_blocker_after_readme_repair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    dispatcher = MagicMock()
    dispatcher.index_directory.return_value = {
        "indexed_files": 3,
        "failed_files": 0,
        "errors": [],
        "lexical_stage": "completed",
        "lexical_files_attempted": 3,
        "lexical_files_completed": 3,
        "last_progress_path": str(repo / "README.md"),
        "in_flight_path": None,
        "summaries_written": 0,
        "summary_chunks_attempted": 3,
        "summary_missing_chunks": 3,
        "semantic_indexed": 0,
        "semantic_failed": 0,
        "semantic_skipped": 0,
        "semantic_blocked": 1,
        "semantic_stage": "blocked_missing_summaries",
        "semantic_error": "Missing authoritative summaries blocked strict semantic indexing",
    }

    manager = GitAwareIndexManager(registry=registry, dispatcher=dispatcher)
    result = manager._full_index(repo_info.repository_id, ctx)

    assert result.indexed == 3
    assert not result.clean
    assert result.low_level is None
    assert result.semantic is not None
    assert result.semantic["semantic_stage"] == "blocked_missing_summaries"
    assert result.errors == ["Missing authoritative summaries blocked strict semantic indexing"]


def test_force_full_sync_does_not_advance_commit_when_semantic_stage_is_blocked(tmp_path):
    repo = _make_git_repo(tmp_path)
    old_commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, old_commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": old_commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=1,
            failed=0,
            errors=["Summary generation plateaued before strict semantic indexing could start"],
            semantic={
                "summary_passes": 3,
                "summaries_written": 12,
                "summary_chunks_attempted": 12,
                "summary_missing_chunks": 4,
                "semantic_indexed": 0,
                "semantic_failed": 0,
                "semantic_skipped": 0,
                "semantic_blocked": 1,
                "semantic_stage": "blocked_summary_plateau",
                "semantic_error": "Summary generation plateaued before strict semantic indexing could start",
            },
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    assert result.action == "failed"
    assert result.error == "Staged rebuild failed (RuntimeError)"
    assert result.semantic["semantic_stage"] == "blocked_summary_plateau"
    registry.begin_generation_mutation.assert_called_once_with(
        repo_info.repository_id,
        expected_registration_id=repo_info.registration_id,
        expected_generation=repo_info.index_generation,
    )
    registry.update_staleness_reason.assert_called_with(
        repo_info.repository_id, "partial_index_failure"
    )
    registry.update_indexed_commit.assert_not_called()


def test_force_full_sync_preserves_exact_summary_call_timeout_blocker(tmp_path):
    repo = _make_git_repo(tmp_path)
    old_commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, old_commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": old_commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=1,
            failed=0,
            errors=[
                "Authoritative summary call timed out after 30 seconds before any summary was "
                f"written for {repo / 'README.md'}; 4 chunks still require summaries"
            ],
            semantic={
                "summary_passes": 0,
                "summaries_written": 0,
                "summary_chunks_attempted": 1,
                "summary_missing_chunks": 4,
                "summary_remaining_chunks": 4,
                "summary_scope_drained": False,
                "summary_call_timed_out": True,
                "summary_call_file_path": str(repo / "README.md"),
                "summary_call_chunk_ids": ["chunk-1"],
                "summary_call_timeout_seconds": 30.0,
                "semantic_indexed": 0,
                "semantic_failed": 0,
                "semantic_skipped": 0,
                "semantic_blocked": 1,
                "semantic_stage": "blocked_summary_call_timeout",
                "semantic_error": (
                    "Authoritative summary call timed out after 30 seconds before any summary "
                    f"was written for {repo / 'README.md'}; 4 chunks still require summaries"
                ),
            },
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    assert result.action == "failed"
    assert result.semantic is not None
    assert result.semantic["summary_call_timed_out"] is True
    assert result.semantic["summary_call_file_path"] == str(repo / "README.md")
    assert result.semantic["summary_call_chunk_ids"] == ["chunk-1"]
    assert result.semantic["summary_call_timeout_seconds"] == 30.0
    registry.begin_generation_mutation.assert_called_once_with(
        repo_info.repository_id,
        expected_registration_id=repo_info.registration_id,
        expected_generation=repo_info.index_generation,
    )
    registry.update_staleness_reason.assert_called_with(
        repo_info.repository_id, "partial_index_failure"
    )


def test_force_full_sync_preserves_durable_exit_trace_on_interrupt(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"
    repo_info.index_path.touch()

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = _make_manager(registry)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)

    def _interrupting_full_index(repo_id, _ctx):
        manager._write_force_full_exit_trace(
            repo_info,
            {
                "status": "interrupted",
                "stage": "blocked_summary_call_timeout",
                "stage_family": "semantic_closeout",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "README.md"),
                "in_flight_path": str(repo / "README.md"),
                "summary_call_timed_out": True,
                "summary_call_file_path": str(repo / "README.md"),
                "summary_call_chunk_ids": ["chunk-1"],
                "summary_call_timeout_seconds": 30.0,
                "blocker_source": "summary_call_shutdown",
            },
        )
        raise KeyboardInterrupt("synthetic interrupt")

    manager._full_index = MagicMock(side_effect=_interrupting_full_index)

    try:
        manager.sync_repository_index(repo_info.repository_id, force_full=True)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("Expected synthetic KeyboardInterrupt")

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "interrupted"
    assert trace["stage"] == "blocked_summary_call_timeout"
    assert trace["stage_family"] == "semantic_closeout"
    assert trace["current_commit"] == commit
    assert trace["indexed_commit_before"] == "older-indexed-commit"
    assert trace["summary_call_timed_out"] is True
    assert trace["blocker_source"] == "summary_call_shutdown"
    registry.update_indexed_commit.assert_not_called()


def test_force_full_sync_terminalizes_running_trace_when_later_test_pair_crashes(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"
    repo_info.index_path.touch()

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = _make_manager(registry)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)

    prior_file = repo / "tests" / "test_deployment_runbook_shape.py"
    blocked_file = repo / "tests" / "test_reindex_resume.py"

    def _crashing_full_index(repo_id, _ctx, progress_callback=None):
        assert progress_callback is not None
        progress_callback(
            {
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "blocker_source": "lexical_mutation",
                "lexical_stage": "walking",
                "semantic_stage": "not_run",
                "last_progress_path": str(prior_file),
                "in_flight_path": str(blocked_file),
                "summary_call_timed_out": False,
                "summary_call_file_path": None,
                "summary_call_chunk_ids": [],
                "summary_call_timeout_seconds": None,
            }
        )
        raise RuntimeError("synthetic crash after later lexical pair")

    manager._full_index = MagicMock(side_effect=_crashing_full_index)

    with pytest.raises(RuntimeError, match="synthetic crash after later lexical pair"):
        manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "interrupted"
    assert trace["stage"] == "lexical_walking"
    assert trace["stage_family"] == "lexical"
    assert trace["current_commit"] == commit
    assert trace["indexed_commit_before"] == "older-indexed-commit"
    assert trace["last_progress_path"] == str(prior_file)
    assert trace["in_flight_path"] == str(blocked_file)
    assert trace["blocker_source"] == "lexical_mutation"
    assert ".devcontainer/devcontainer.json" not in (trace.get("last_progress_path") or "")
    assert "scripts/quick_mcp_vs_native_validation.py" not in (
        trace.get("last_progress_path") or ""
    )
    assert "tests/test_artifact_publish_race.py" not in (trace.get("last_progress_path") or "")
    registry.update_indexed_commit.assert_not_called()


def test_force_full_sync_terminalizes_running_trace_when_later_root_test_pair_crashes(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"
    repo_info.index_path.touch()

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = _make_manager(registry)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)

    prior_file = repo / "tests" / "root_tests" / "test_voyage_api.py"
    blocked_file = repo / "tests" / "root_tests" / "run_reranking_tests.py"

    def _crashing_full_index(repo_id, _ctx, progress_callback=None):
        assert progress_callback is not None
        progress_callback(
            {
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "blocker_source": "lexical_mutation",
                "lexical_stage": "walking",
                "semantic_stage": "not_run",
                "last_progress_path": str(prior_file),
                "in_flight_path": str(blocked_file),
                "summary_call_timed_out": False,
                "summary_call_file_path": None,
                "summary_call_chunk_ids": [],
                "summary_call_timeout_seconds": None,
            }
        )
        raise RuntimeError("synthetic crash after later root-test pair")

    manager._full_index = MagicMock(side_effect=_crashing_full_index)

    with pytest.raises(RuntimeError, match="synthetic crash after later root-test pair"):
        manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "interrupted"
    assert trace["stage"] == "lexical_walking"
    assert trace["stage_family"] == "lexical"
    assert trace["current_commit"] == commit
    assert trace["indexed_commit_before"] == "older-indexed-commit"
    assert trace["last_progress_path"] == str(prior_file)
    assert trace["in_flight_path"] == str(blocked_file)
    assert trace["blocker_source"] == "lexical_mutation"
    assert ".devcontainer/devcontainer.json" not in (trace.get("last_progress_path") or "")
    assert "scripts/validate_mcp_comprehensive.py" not in (trace.get("last_progress_path") or "")
    assert "tests/test_artifact_publish_race.py" not in (trace.get("last_progress_path") or "")
    assert "tests/test_reindex_resume.py" not in (trace.get("last_progress_path") or "")
    registry.update_indexed_commit.assert_not_called()


def test_force_full_sync_marks_durable_exit_trace_completed_on_clean_closeout(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = None
    repo_info.index_path.touch()

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}
    registry.update_indexed_commit.return_value = True

    manager = _make_manager(registry)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=1,
            semantic={
                "semantic_stage": "indexed",
            },
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "full_index"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_completed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["current_commit"] == commit
    assert trace["indexed_commit_before"] is None
    registry.publish_generation.assert_called_once()
    publication = registry.publish_generation.call_args.kwargs
    assert publication["commit"] == commit
    assert publication["branch"] == "main"
    assert publication["generation"] == repo_info.index_generation


def test_force_full_progress_callback_persists_fresh_in_flight_snapshot(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"

    manager = GitAwareIndexManager(registry=MagicMock(), dispatcher=MagicMock())
    callback = manager._make_force_full_progress_callback(
        repo_info=repo_info,
        current_commit=commit,
        indexed_commit_before=repo_info.last_indexed_commit,
    )

    callback(
        {
            "stage": "lexical_walking",
            "stage_family": "lexical",
            "blocker_source": "lexical_mutation",
            "lexical_stage": "walking",
            "semantic_stage": "not_run",
            "last_progress_path": str(repo / "ai_docs" / "pytest_overview.md"),
            "in_flight_path": str(repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"),
            "summary_call_timed_out": False,
            "summary_call_file_path": None,
            "summary_call_chunk_ids": [],
            "summary_call_timeout_seconds": None,
        }
    )

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "running"
    assert trace["stage"] == "lexical_walking"
    assert trace["stage_family"] == "lexical"
    assert trace["current_commit"] == commit
    assert trace["indexed_commit_before"] == "older-indexed-commit"
    assert trace["last_progress_path"] == str(repo / "ai_docs" / "pytest_overview.md")
    assert trace["in_flight_path"] == str(repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md")
    assert isinstance(trace["process_id"], int)
    assert trace["trace_timestamp"]


def test_force_full_progress_callback_persists_later_test_pair_snapshot(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"

    manager = GitAwareIndexManager(registry=MagicMock(), dispatcher=MagicMock())
    callback = manager._make_force_full_progress_callback(
        repo_info=repo_info,
        current_commit=commit,
        indexed_commit_before=repo_info.last_indexed_commit,
    )
    prior_file = repo / "tests" / "test_deployment_runbook_shape.py"
    blocked_file = repo / "tests" / "test_reindex_resume.py"

    callback(
        {
            "stage": "lexical_walking",
            "stage_family": "lexical",
            "blocker_source": "lexical_mutation",
            "lexical_stage": "walking",
            "semantic_stage": "not_run",
            "last_progress_path": str(prior_file),
            "in_flight_path": str(blocked_file),
            "summary_call_timed_out": False,
            "summary_call_file_path": None,
            "summary_call_chunk_ids": [],
            "summary_call_timeout_seconds": None,
        }
    )

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "running"
    assert trace["stage"] == "lexical_walking"
    assert trace["stage_family"] == "lexical"
    assert trace["current_commit"] == commit
    assert trace["indexed_commit_before"] == "older-indexed-commit"
    assert trace["last_progress_path"] == str(prior_file)
    assert trace["in_flight_path"] == str(blocked_file)
    assert ".devcontainer/devcontainer.json" not in (trace.get("last_progress_path") or "")
    assert "scripts/quick_mcp_vs_native_validation.py" not in (
        trace.get("last_progress_path") or ""
    )
    assert "tests/test_artifact_publish_race.py" not in (trace.get("last_progress_path") or "")
    assert trace["trace_timestamp"]


def test_force_full_progress_callback_persists_later_script_pair_snapshot(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"

    manager = GitAwareIndexManager(registry=MagicMock(), dispatcher=MagicMock())
    callback = manager._make_force_full_progress_callback(
        repo_info=repo_info,
        current_commit=commit,
        indexed_commit_before=repo_info.last_indexed_commit,
    )
    prior_file = repo / "scripts" / "run_test_batch.py"
    blocked_file = repo / "scripts" / "validate_mcp_comprehensive.py"

    callback(
        {
            "stage": "lexical_walking",
            "stage_family": "lexical",
            "blocker_source": "lexical_mutation",
            "lexical_stage": "walking",
            "semantic_stage": "not_run",
            "last_progress_path": str(prior_file),
            "in_flight_path": str(blocked_file),
            "summary_call_timed_out": False,
            "summary_call_file_path": None,
            "summary_call_chunk_ids": [],
            "summary_call_timeout_seconds": None,
        }
    )

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "running"
    assert trace["stage"] == "lexical_walking"
    assert trace["stage_family"] == "lexical"
    assert trace["current_commit"] == commit
    assert trace["indexed_commit_before"] == "older-indexed-commit"
    assert trace["last_progress_path"] == str(prior_file)
    assert trace["in_flight_path"] == str(blocked_file)
    assert ".devcontainer/devcontainer.json" not in (trace.get("last_progress_path") or "")
    assert "scripts/quick_mcp_vs_native_validation.py" not in (
        trace.get("last_progress_path") or ""
    )
    assert "tests/test_artifact_publish_race.py" not in (trace.get("last_progress_path") or "")
    assert trace["trace_timestamp"]


def test_force_full_progress_callback_persists_later_root_test_pair_snapshot(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"

    manager = GitAwareIndexManager(registry=MagicMock(), dispatcher=MagicMock())
    callback = manager._make_force_full_progress_callback(
        repo_info=repo_info,
        current_commit=commit,
        indexed_commit_before=repo_info.last_indexed_commit,
    )
    prior_file = repo / "tests" / "root_tests" / "test_voyage_api.py"
    blocked_file = repo / "tests" / "root_tests" / "run_reranking_tests.py"

    callback(
        {
            "stage": "lexical_walking",
            "stage_family": "lexical",
            "blocker_source": "lexical_mutation",
            "lexical_stage": "walking",
            "semantic_stage": "not_run",
            "last_progress_path": str(prior_file),
            "in_flight_path": str(blocked_file),
            "summary_call_timed_out": False,
            "summary_call_file_path": None,
            "summary_call_chunk_ids": [],
            "summary_call_timeout_seconds": None,
        }
    )

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "running"
    assert trace["stage"] == "lexical_walking"
    assert trace["stage_family"] == "lexical"
    assert trace["current_commit"] == commit
    assert trace["indexed_commit_before"] == "older-indexed-commit"
    assert trace["last_progress_path"] == str(prior_file)
    assert trace["in_flight_path"] == str(blocked_file)
    assert ".devcontainer/devcontainer.json" not in (trace.get("last_progress_path") or "")
    assert "scripts/validate_mcp_comprehensive.py" not in (trace.get("last_progress_path") or "")
    assert "tests/test_artifact_publish_race.py" not in (trace.get("last_progress_path") or "")
    assert "tests/test_reindex_resume.py" not in (trace.get("last_progress_path") or "")
    assert trace["trace_timestamp"]


def test_force_full_progress_callback_persists_swift_database_efficiency_pair_snapshot(
    tmp_path,
):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"

    manager = GitAwareIndexManager(registry=MagicMock(), dispatcher=MagicMock())
    callback = manager._make_force_full_progress_callback(
        repo_info=repo_info,
        current_commit=commit,
        indexed_commit_before=repo_info.last_indexed_commit,
    )
    prior_file = repo / "tests" / "root_tests" / "test_swift_plugin.py"
    blocked_file = repo / "tests" / "root_tests" / "test_mcp_database_efficiency.py"

    callback(
        {
            "stage": "lexical_walking",
            "stage_family": "lexical",
            "blocker_source": "lexical_mutation",
            "lexical_stage": "walking",
            "semantic_stage": "not_run",
            "last_progress_path": str(prior_file),
            "in_flight_path": str(blocked_file),
            "summary_call_timed_out": False,
            "summary_call_file_path": None,
            "summary_call_chunk_ids": [],
            "summary_call_timeout_seconds": None,
        }
    )

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "running"
    assert trace["stage"] == "lexical_walking"
    assert trace["stage_family"] == "lexical"
    assert trace["current_commit"] == commit
    assert trace["indexed_commit_before"] == "older-indexed-commit"
    assert trace["last_progress_path"] == str(prior_file)
    assert trace["in_flight_path"] == str(blocked_file)
    assert ".codex/phase-loop" not in (trace.get("last_progress_path") or "")
    assert "tests/root_tests/run_reranking_tests.py" not in (trace.get("last_progress_path") or "")
    assert trace["trace_timestamp"]


def test_force_full_progress_callback_persists_route_auth_sandbox_pair_snapshot(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"

    manager = GitAwareIndexManager(registry=MagicMock(), dispatcher=MagicMock())
    callback = manager._make_force_full_progress_callback(
        repo_info=repo_info,
        current_commit=commit,
        indexed_commit_before=repo_info.last_indexed_commit,
    )
    prior_file = repo / "tests" / "security" / "test_route_auth_coverage.py"
    blocked_file = repo / "tests" / "security" / "test_p24_sandbox_degradation.py"

    callback(
        {
            "stage": "lexical_walking",
            "stage_family": "lexical",
            "blocker_source": "lexical_mutation",
            "lexical_stage": "walking",
            "semantic_stage": "not_run",
            "last_progress_path": str(prior_file),
            "in_flight_path": str(blocked_file),
            "summary_call_timed_out": False,
            "summary_call_file_path": None,
            "summary_call_chunk_ids": [],
            "summary_call_timeout_seconds": None,
        }
    )

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "running"
    assert trace["stage"] == "lexical_walking"
    assert trace["stage_family"] == "lexical"
    assert trace["current_commit"] == commit
    assert trace["indexed_commit_before"] == "older-indexed-commit"
    assert trace["last_progress_path"] == str(prior_file)
    assert trace["in_flight_path"] == str(blocked_file)
    assert "tests/root_tests/test_swift_plugin.py" not in (trace.get("last_progress_path") or "")
    assert "tests/security/fixtures/mock_plugin/plugin.py" not in (
        trace.get("in_flight_path") or ""
    )
    assert trace["trace_timestamp"]


def test_force_full_progress_callback_preserves_last_progress_path_across_semantic_handoff(
    tmp_path,
):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"

    manager = GitAwareIndexManager(registry=MagicMock(), dispatcher=MagicMock())
    callback = manager._make_force_full_progress_callback(
        repo_info=repo_info,
        current_commit=commit,
        indexed_commit_before=repo_info.last_indexed_commit,
    )
    devcontainer_path = repo / ".devcontainer" / "devcontainer.json"

    callback(
        {
            "stage": "force_full_closeout_handoff",
            "stage_family": "final_closeout",
            "blocker_source": "final_closeout",
            "lexical_stage": "completed",
            "semantic_stage": "not_run",
            "last_progress_path": str(devcontainer_path),
            "in_flight_path": None,
        }
    )
    callback(
        {
            "stage": "summary_shutdown_started",
            "stage_family": "summary_shutdown",
            "blocker_source": "summary_call_shutdown",
            "lexical_stage": "completed",
            "semantic_stage": "not_run",
            "last_progress_path": None,
            "in_flight_path": None,
        }
    )

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["stage"] == "summary_shutdown_started"
    assert trace["stage_family"] == "summary_shutdown"
    assert trace["last_progress_path"] == str(devcontainer_path)
    assert trace["in_flight_path"] is None


def test_force_full_progress_callback_preserves_exact_devcontainer_tail_pair(
    tmp_path,
):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"

    manager = GitAwareIndexManager(registry=MagicMock(), dispatcher=MagicMock())
    callback = manager._make_force_full_progress_callback(
        repo_info=repo_info,
        current_commit=commit,
        indexed_commit_before=repo_info.last_indexed_commit,
    )
    post_create = repo / ".devcontainer" / "post_create.sh"
    config_file = repo / ".devcontainer" / "devcontainer.json"

    callback(
        {
            "stage": "lexical_walking",
            "stage_family": "lexical",
            "blocker_source": "lexical_mutation",
            "lexical_stage": "walking",
            "semantic_stage": "not_run",
            "last_progress_path": str(post_create),
            "in_flight_path": str(config_file),
            "summary_call_timed_out": False,
            "summary_call_file_path": None,
            "summary_call_chunk_ids": [],
            "summary_call_timeout_seconds": None,
        }
    )

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["stage"] == "lexical_walking"
    assert trace["stage_family"] == "lexical"
    assert trace["last_progress_path"] == str(post_create)
    assert trace["in_flight_path"] == str(config_file)
    assert "ai_docs/prometheus_overview.md" not in (trace.get("last_progress_path") or "")
    assert "ai_docs/README.md" not in (trace.get("in_flight_path") or "")


def test_force_full_progress_callback_preserves_later_included_path_after_ignored_tail(
    tmp_path,
):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"

    manager = GitAwareIndexManager(registry=MagicMock(), dispatcher=MagicMock())
    callback = manager._make_force_full_progress_callback(
        repo_info=repo_info,
        current_commit=commit,
        indexed_commit_before=repo_info.last_indexed_commit,
    )
    later_file = repo / "mcp_server" / "cli" / "repository_commands.py"
    ignored_tail = repo / "test_workspace" / "real_repos" / "search_scaling" / "package.json"

    callback(
        {
            "stage": "lexical_walking",
            "stage_family": "lexical",
            "blocker_source": "lexical_mutation",
            "lexical_stage": "walking",
            "semantic_stage": "not_run",
            "last_progress_path": str(later_file),
            "in_flight_path": None,
        }
    )
    callback(
        {
            "stage": "force_full_closeout_handoff",
            "stage_family": "final_closeout",
            "blocker_source": "final_closeout",
            "lexical_stage": "completed",
            "semantic_stage": "not_run",
            "last_progress_path": None,
            "in_flight_path": None,
        }
    )

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["stage"] == "force_full_closeout_handoff"
    assert trace["stage_family"] == "final_closeout"
    assert trace["last_progress_path"] == str(later_file)
    assert trace["in_flight_path"] is None
    assert str(ignored_tail) not in (trace.get("last_progress_path") or "")


def test_force_full_sync_terminalizes_running_trace_when_same_devcontainer_relapse_persists(
    tmp_path,
):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"
    repo_info.index_path.touch()

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = _make_manager(registry)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)

    blocked_file = repo / ".devcontainer" / "devcontainer.json"

    def _crashing_full_index(repo_id, _ctx, progress_callback=None):
        assert progress_callback is not None
        progress_callback(
            {
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "blocker_source": "lexical_mutation",
                "lexical_stage": "walking",
                "semantic_stage": "not_run",
                "last_progress_path": str(blocked_file),
                "in_flight_path": None,
                "summary_call_timed_out": False,
                "summary_call_file_path": None,
                "summary_call_chunk_ids": [],
                "summary_call_timeout_seconds": None,
            }
        )
        raise RuntimeError("synthetic crash after renewed devcontainer relapse")

    manager._full_index = MagicMock(side_effect=_crashing_full_index)

    with pytest.raises(RuntimeError, match="synthetic crash after renewed devcontainer relapse"):
        manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "interrupted"
    assert trace["stage"] == "lexical_walking"
    assert trace["stage_family"] == "lexical"
    assert trace["current_commit"] == commit
    assert trace["indexed_commit_before"] == "older-indexed-commit"
    assert trace["last_progress_path"] == str(blocked_file)
    assert trace["in_flight_path"] is None
    assert trace["blocker_source"] == "lexical_mutation"
    assert ".devcontainer/post_create.sh" not in (trace.get("last_progress_path") or "")
    assert "tests/test_reindex_resume.py" not in (trace.get("last_progress_path") or "")
    assert "scripts/validate_mcp_comprehensive.py" not in (trace.get("last_progress_path") or "")
    assert "tests/root_tests/run_reranking_tests.py" not in (trace.get("last_progress_path") or "")
    registry.update_indexed_commit.assert_not_called()


def test_force_full_sync_terminalizes_running_trace_with_exact_devcontainer_tail_pair(
    tmp_path,
):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"
    repo_info.index_path.touch()

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = _make_manager(registry)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)

    post_create = repo / ".devcontainer" / "post_create.sh"
    config_file = repo / ".devcontainer" / "devcontainer.json"

    def _crashing_full_index(repo_id, _ctx, progress_callback=None):
        assert progress_callback is not None
        progress_callback(
            {
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "blocker_source": "lexical_mutation",
                "lexical_stage": "walking",
                "semantic_stage": "not_run",
                "last_progress_path": str(post_create),
                "in_flight_path": str(config_file),
                "summary_call_timed_out": False,
                "summary_call_file_path": None,
                "summary_call_chunk_ids": [],
                "summary_call_timeout_seconds": None,
            }
        )
        raise RuntimeError("synthetic crash after exact devcontainer tail pair")

    manager._full_index = MagicMock(side_effect=_crashing_full_index)

    with pytest.raises(RuntimeError, match="synthetic crash after exact devcontainer tail pair"):
        manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "interrupted"
    assert trace["stage"] == "lexical_walking"
    assert trace["stage_family"] == "lexical"
    assert trace["current_commit"] == commit
    assert trace["indexed_commit_before"] == "older-indexed-commit"
    assert trace["last_progress_path"] == str(post_create)
    assert trace["in_flight_path"] == str(config_file)
    assert trace["blocker_source"] == "lexical_mutation"
    assert "ai_docs/prometheus_overview.md" not in (trace.get("last_progress_path") or "")
    assert "ai_docs/README.md" not in (trace.get("in_flight_path") or "")
    registry.update_indexed_commit.assert_not_called()


def test_get_repository_status_includes_force_full_exit_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "blocked_summary_call_timeout",
                "stage_family": "semantic_closeout",
                "trace_timestamp": "2026-04-29T09:00:00Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "README.md"),
                "in_flight_path": str(repo / "README.md"),
                "summary_call_timed_out": True,
                "summary_call_file_path": str(repo / "README.md"),
                "summary_call_chunk_ids": ["chunk-1"],
                "summary_call_timeout_seconds": 30.0,
                "blocker_source": "summary_call_shutdown",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "blocked_summary_call_timeout"
    assert status["force_full_exit_trace"]["stage_family"] == "semantic_closeout"
    assert status["force_full_exit_trace"]["blocker_source"] == "summary_call_shutdown"


def test_get_repository_status_terminalizes_inactive_running_force_full_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "running",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T11:00:00Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "tests" / "test_p8_historical_sweep.py"),
                "in_flight_path": str(repo / "tests" / "test_p26_public_alpha_decision.py"),
                "process_id": 999999,
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "tests" / "test_p8_historical_sweep.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "tests" / "test_p26_public_alpha_decision.py"
    )


def test_get_repository_status_preserves_visualization_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T12:30:00Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "mcp_server" / "visualization" / "__init__.py"),
                "in_flight_path": str(repo / "mcp_server" / "visualization" / "quick_charts.py"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "mcp_server" / "visualization" / "__init__.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "mcp_server" / "visualization" / "quick_charts.py"
    )
    assert "scripts/validate_mcp_comprehensive.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_force_full_sync_does_not_advance_commit_when_low_level_blocker_fires(tmp_path):
    repo = _make_git_repo(tmp_path)
    old_commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, old_commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": old_commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=0,
            failed=1,
            errors=["Lexical indexing timed out while processing hello.py"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 1,
                "lexical_files_completed": 0,
                "last_progress_path": None,
                "in_flight_path": str(repo / "hello.py"),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing hello.py",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    assert result.action == "failed"
    assert result.error == "Staged rebuild failed (RuntimeError)"


def test_force_full_sync_does_not_advance_commit_when_later_script_pair_blocks(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"
    repo_info.index_path.touch()

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}
    registry.update_indexed_commit.return_value = True

    manager = _make_manager(registry)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)

    prior_file = repo / "scripts" / "run_test_batch.py"
    blocked_file = repo / "scripts" / "validate_mcp_comprehensive.py"

    def _crashing_full_index(repo_id, _ctx, progress_callback=None):
        assert progress_callback is not None
        progress_callback(
            {
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "blocker_source": "lexical_mutation",
                "lexical_stage": "walking",
                "semantic_stage": "not_run",
                "last_progress_path": str(prior_file),
                "in_flight_path": str(blocked_file),
                "summary_call_timed_out": False,
                "summary_call_file_path": None,
                "summary_call_chunk_ids": [],
                "summary_call_timeout_seconds": None,
            }
        )
        raise RuntimeError("synthetic crash after later lexical script pair")

    manager._full_index = MagicMock(side_effect=_crashing_full_index)

    with pytest.raises(RuntimeError, match="synthetic crash after later lexical script pair"):
        manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "interrupted"
    assert trace["stage"] == "lexical_walking"
    assert trace["stage_family"] == "lexical"
    assert trace["current_commit"] == commit
    assert trace["indexed_commit_before"] == "older-indexed-commit"
    assert trace["last_progress_path"] == str(prior_file)
    assert trace["in_flight_path"] == str(blocked_file)
    assert trace["blocker_source"] == "lexical_mutation"
    assert ".devcontainer/devcontainer.json" not in (trace.get("last_progress_path") or "")
    assert "scripts/quick_mcp_vs_native_validation.py" not in (
        trace.get("last_progress_path") or ""
    )
    assert "tests/test_artifact_publish_race.py" not in (trace.get("last_progress_path") or "")
    registry.update_indexed_commit.assert_not_called()


def test_force_full_sync_does_not_advance_commit_when_later_root_test_pair_blocks(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"
    repo_info.index_path.touch()

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = _make_manager(registry)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)

    prior_file = repo / "tests" / "root_tests" / "test_voyage_api.py"
    blocked_file = repo / "tests" / "root_tests" / "run_reranking_tests.py"

    def _blocked_full_index(repo_id, _ctx, progress_callback=None):
        assert progress_callback is not None
        progress_callback(
            {
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "blocker_source": "lexical_mutation",
                "lexical_stage": "walking",
                "semantic_stage": "not_run",
                "last_progress_path": str(prior_file),
                "in_flight_path": str(blocked_file),
                "summary_call_timed_out": False,
                "summary_call_file_path": None,
                "summary_call_chunk_ids": [],
                "summary_call_timeout_seconds": None,
            }
        )
        return UpdateResult(
            indexed=1,
            failed=1,
            errors=["synthetic root-test blocker"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 2,
                "lexical_files_completed": 1,
                "last_progress_path": str(prior_file),
                "in_flight_path": str(blocked_file),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "synthetic root-test blocker",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )

    manager._full_index = MagicMock(side_effect=_blocked_full_index)

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["current_commit"] == commit
    assert trace["indexed_commit_before"] == "older-indexed-commit"
    assert trace["last_progress_path"] == str(prior_file)
    assert trace["in_flight_path"] == str(blocked_file)
    assert trace["blocker_source"] == "lexical_mutation"
    assert ".devcontainer/devcontainer.json" not in (trace.get("last_progress_path") or "")
    assert "scripts/validate_mcp_comprehensive.py" not in (trace.get("last_progress_path") or "")
    assert "tests/test_artifact_publish_race.py" not in (trace.get("last_progress_path") or "")
    assert "tests/test_reindex_resume.py" not in (trace.get("last_progress_path") or "")
    registry.update_indexed_commit.assert_not_called()


def test_force_full_sync_trace_moves_past_fast_report_family_after_boundary_repair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=1,
            failed=1,
            errors=["Lexical indexing timed out while processing README.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 2,
                "lexical_files_completed": 1,
                "last_progress_path": str(repo / "src" / "main.py"),
                "in_flight_path": str(repo / "README.md"),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing README.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert "fast_test_results/fast_report_" not in (trace.get("last_progress_path") or "")
    assert "fast_test_results/fast_report_" not in (trace.get("in_flight_path") or "")
    assert trace["last_progress_path"] == str(repo / "src" / "main.py")
    assert trace["in_flight_path"] == str(repo / "README.md")
    registry.update_indexed_commit.assert_not_called()
    registry.update_indexed_commit.assert_not_called()


def test_force_full_sync_trace_moves_past_pytest_overview_after_bounded_ai_docs_repair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=2,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing docs/status/SEMANTIC_DOGFOOD_REBUILD.md"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 3,
                "lexical_files_completed": 2,
                "last_progress_path": str(repo / "src" / "semantic_preflight.py"),
                "in_flight_path": str(repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing " "SEMANTIC_DOGFOOD_REBUILD.md"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert "pytest_overview.md" not in (trace.get("last_progress_path") or "")
    assert "pytest_overview.md" not in (trace.get("in_flight_path") or "")
    assert trace["last_progress_path"] == str(repo / "src" / "semantic_preflight.py")
    assert trace["in_flight_path"] == str(repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md")
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    registry.update_indexed_commit.assert_not_called()


def test_force_full_sync_durable_trace_moves_past_jedi_ai_doc(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.index_path.touch()
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing ai_docs/tree_sitter_queries.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"),
                "in_flight_path": str(repo / "ai_docs" / "tree_sitter_queries.md"),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing " "tree_sitter_queries.md"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert "ai_docs/jedi.md" not in (trace.get("last_progress_path") or "")
    assert "ai_docs/jedi.md" not in (trace.get("in_flight_path") or "")
    assert trace["last_progress_path"] == str(
        repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"
    )
    assert trace["in_flight_path"] == str(repo / "ai_docs" / "tree_sitter_queries.md")
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    registry.update_indexed_commit.assert_not_called()


def test_get_repository_status_preserves_later_ai_docs_overview_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    black_doc = repo / "ai_docs" / "black_isort_overview.md"
    sqlite_doc = repo / "ai_docs" / "sqlite_fts5_overview.md"
    black_doc.parent.mkdir(parents=True, exist_ok=True)
    black_doc.write_text("# Black & isort AI Context\n", encoding="utf-8")
    sqlite_doc.write_text("# SQLite FTS5 Comprehensive Guide for Code Indexing\n", encoding="utf-8")
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T21:50:51Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(black_doc),
                "in_flight_path": str(sqlite_doc),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(black_doc)
    assert status["force_full_exit_trace"]["in_flight_path"] == str(sqlite_doc)
    assert "tests/fixtures/multi_repo.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_force_full_sync_durable_trace_moves_past_later_ai_docs_overview_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.index_path.touch()
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    later_file = repo / "mcp_search_code_test_results.json"
    in_flight_file = repo / "MIGRATION_LOG.md"

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=5,
            failed=1,
            errors=["Lexical indexing timed out while processing MIGRATION_LOG.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 6,
                "lexical_files_completed": 5,
                "last_progress_path": str(later_file),
                "in_flight_path": str(in_flight_file),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing MIGRATION_LOG.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert "black_isort_overview.md" not in (trace.get("last_progress_path") or "")
    assert "sqlite_fts5_overview.md" not in (trace.get("in_flight_path") or "")
    assert trace["last_progress_path"] == str(later_file)
    assert trace["in_flight_path"] == str(in_flight_file)
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    registry.update_indexed_commit.assert_not_called()


def test_get_repository_status_preserves_exact_ai_docs_readme_tail_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    prometheus_doc = repo / "ai_docs" / "prometheus_overview.md"
    ai_docs_readme = repo / "ai_docs" / "README.md"
    prometheus_doc.parent.mkdir(parents=True, exist_ok=True)
    prometheus_doc.write_text("# Prometheus Overview\n", encoding="utf-8")
    ai_docs_readme.write_text("# AI Documentation Index\n", encoding="utf-8")
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-30T06:17:40Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(prometheus_doc),
                "in_flight_path": str(ai_docs_readme),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(prometheus_doc)
    assert status["force_full_exit_trace"]["in_flight_path"] == str(ai_docs_readme)
    assert "scripts/demo_centralized_indexes.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_force_full_sync_durable_trace_moves_past_exact_ai_docs_readme_tail_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.index_path.touch()
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    later_file = repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"
    in_flight_file = repo / "ai_docs" / "tree_sitter_queries.md"

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=4,
            failed=1,
            errors=["Lexical indexing timed out while processing ai_docs/tree_sitter_queries.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 5,
                "lexical_files_completed": 4,
                "last_progress_path": str(later_file),
                "in_flight_path": str(in_flight_file),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing " "tree_sitter_queries.md"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert "ai_docs/prometheus_overview.md" not in (trace.get("last_progress_path") or "")
    assert "ai_docs/README.md" not in (trace.get("in_flight_path") or "")
    assert trace["last_progress_path"] == str(later_file)
    assert trace["in_flight_path"] == str(in_flight_file)
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    registry.update_indexed_commit.assert_not_called()


def test_force_full_sync_durable_trace_moves_past_validation_doc_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.index_path.touch()
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_doc = repo / "docs" / "validation" / "ga-closeout-decision.md"
    blocked_doc = repo / "docs" / "validation" / "mre2e-evidence.md"
    later_doc = repo / "docs" / "validation" / "secondary-tool-readiness-evidence.md"
    later_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing "
                "docs/validation/secondary-tool-readiness-evidence.md"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_doc),
                "in_flight_path": str(later_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing "
                        "secondary-tool-readiness-evidence.md"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_doc)
    assert trace["in_flight_path"] == str(later_doc)
    assert str(prior_doc) in {trace["last_progress_path"], trace["in_flight_path"]} or str(
        blocked_doc
    ) in {trace["last_progress_path"], trace["in_flight_path"]}
    assert "mcp_server/visualization/quick_charts.py" not in (trace.get("last_progress_path") or "")
    assert "mcp_server/visualization/quick_charts.py" not in (trace.get("in_flight_path") or "")


def test_force_full_sync_durable_trace_moves_past_benchmark_doc_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.index_path.touch()
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_doc = (
        repo
        / "docs"
        / "benchmarks"
        / "mcp_vs_native_benchmark_fullrepo_fireworks_qwen_voyage_local_iter5_rerun.md"
    )
    blocked_doc = repo / "docs" / "benchmarks" / "production_benchmark.md"
    later_doc = repo / "docs" / "status" / "semantic_tail.md"
    later_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing docs/status/semantic_tail.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_doc),
                "in_flight_path": str(later_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing semantic_tail.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_doc)
    assert trace["in_flight_path"] == str(later_doc)
    assert str(prior_doc) in {trace["last_progress_path"], trace["in_flight_path"]} or str(
        blocked_doc
    ) in {trace["last_progress_path"], trace["in_flight_path"]}


def test_force_full_sync_durable_trace_moves_past_docs_governance_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.index_path.touch()
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_doc = repo / "tests" / "docs" / "test_mre2e_evidence_contract.py"
    blocked_doc = repo / "tests" / "docs" / "test_gagov_governance_contract.py"
    later_doc = repo / "tests" / "docs" / "test_phase_loop_operator_notes.py"
    later_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing "
                "tests/docs/test_phase_loop_operator_notes.py"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_doc),
                "in_flight_path": str(later_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing "
                        "test_phase_loop_operator_notes.py"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_doc)
    assert trace["in_flight_path"] == str(later_doc)
    assert str(prior_doc) in {trace["last_progress_path"], trace["in_flight_path"]} or str(
        blocked_doc
    ) in {trace["last_progress_path"], trace["in_flight_path"]}
    assert "mcp_server/visualization/quick_charts.py" not in (trace.get("last_progress_path") or "")
    assert "tests/root_tests/run_reranking_tests.py" not in (trace.get("in_flight_path") or "")


def test_force_full_sync_durable_trace_moves_past_docs_test_tail_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.index_path.touch()
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_doc = repo / "tests" / "docs" / "test_gaclose_evidence_closeout.py"
    blocked_doc = repo / "tests" / "docs" / "test_p8_deployment_security.py"
    later_doc = repo / "tests" / "docs" / "test_phase_loop_operator_notes.py"
    later_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing "
                "tests/docs/test_phase_loop_operator_notes.py"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_doc),
                "in_flight_path": str(later_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing "
                        "test_phase_loop_operator_notes.py"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_doc)
    assert trace["in_flight_path"] == str(later_doc)
    assert str(prior_doc) not in (trace.get("in_flight_path") or "")
    assert "tests/docs/test_mre2e_evidence_contract.py" not in (
        trace.get("last_progress_path") or ""
    )
    assert "scripts/create_semantic_embeddings.py" not in (trace.get("last_progress_path") or "")


def test_force_full_sync_durable_trace_moves_past_mock_plugin_fixture_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.index_path.touch()
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_plugin = repo / "tests" / "security" / "fixtures" / "mock_plugin" / "plugin.py"
    blocked_init = repo / "tests" / "security" / "fixtures" / "mock_plugin" / "__init__.py"
    later_test = repo / "tests" / "security" / "test_plugin_sandbox.py"
    later_test.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing "
                "tests/security/test_plugin_sandbox.py"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_init),
                "in_flight_path": str(later_test),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing " "test_plugin_sandbox.py"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_init)
    assert trace["in_flight_path"] == str(later_test)
    assert str(prior_plugin) not in (trace.get("in_flight_path") or "")
    assert "tests/docs/test_gaclose_evidence_closeout.py" not in (
        trace.get("last_progress_path") or ""
    )
    assert "scripts/create_semantic_embeddings.py" not in (trace.get("last_progress_path") or "")


def test_force_full_sync_durable_trace_moves_past_docs_contract_tail_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.index_path.touch()
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_doc = repo / "tests" / "docs" / "test_semincr_contract.py"
    blocked_doc = repo / "tests" / "docs" / "test_gabase_ga_readiness_contract.py"
    later_doc = repo / "ai_docs" / "qdrant.md"
    later_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing ai_docs/qdrant.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_doc),
                "in_flight_path": str(later_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing qdrant.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_doc)
    assert trace["in_flight_path"] == str(later_doc)
    assert str(prior_doc) not in (trace.get("in_flight_path") or "")
    assert "tests/security/fixtures/mock_plugin/plugin.py" not in (
        trace.get("last_progress_path") or ""
    )
    assert "tests/docs/test_gaclose_evidence_closeout.py" not in (
        trace.get("last_progress_path") or ""
    )


def test_force_full_sync_durable_trace_moves_past_ga_release_docs_tail_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.index_path.touch()
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_doc = repo / "tests" / "docs" / "test_garc_rc_soak_contract.py"
    blocked_doc = repo / "tests" / "docs" / "test_garel_ga_release_contract.py"
    later_doc = repo / "ai_docs" / "qdrant.md"
    later_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing ai_docs/qdrant.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_doc),
                "in_flight_path": str(later_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing qdrant.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_doc)
    assert trace["in_flight_path"] == str(later_doc)
    assert str(prior_doc) not in (trace.get("in_flight_path") or "")
    assert "tests/docs/test_semincr_contract.py" not in (trace.get("last_progress_path") or "")
    assert "tests/security/fixtures/mock_plugin/plugin.py" not in (
        trace.get("last_progress_path") or ""
    )


def test_get_repository_status_preserves_claude_command_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T14:41:32Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / ".claude" / "commands" / "execute-lane.md"),
                "in_flight_path": str(repo / ".claude" / "commands" / "plan-phase.md"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / ".claude" / "commands" / "execute-lane.md"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / ".claude" / "commands" / "plan-phase.md"
    )
    assert "test_gagov_governance_contract.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_get_repository_status_preserves_docs_test_tail_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T17:45:52Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(
                    repo / "tests" / "docs" / "test_gaclose_evidence_closeout.py"
                ),
                "in_flight_path": str(repo / "tests" / "docs" / "test_p8_deployment_security.py"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "tests" / "docs" / "test_gaclose_evidence_closeout.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "tests" / "docs" / "test_p8_deployment_security.py"
    )
    assert "test_gagov_governance_contract.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )
    assert "consolidate_real_performance_data.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_get_repository_status_preserves_docs_contract_tail_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T18:52:55Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "tests" / "docs" / "test_semincr_contract.py"),
                "in_flight_path": str(
                    repo / "tests" / "docs" / "test_gabase_ga_readiness_contract.py"
                ),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "tests" / "docs" / "test_semincr_contract.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "tests" / "docs" / "test_gabase_ga_readiness_contract.py"
    )
    assert "test_p8_deployment_security.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )
    assert "mock_plugin/plugin.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_get_repository_status_preserves_ga_release_docs_tail_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T19:13:19Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(
                    repo / "tests" / "docs" / "test_garc_rc_soak_contract.py"
                ),
                "in_flight_path": str(
                    repo / "tests" / "docs" / "test_garel_ga_release_contract.py"
                ),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "tests" / "docs" / "test_garc_rc_soak_contract.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "tests" / "docs" / "test_garel_ga_release_contract.py"
    )
    assert "test_gabase_ga_readiness_contract.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )
    assert "mock_plugin/plugin.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_get_repository_status_preserves_later_legacy_codex_phase_loop_rebound_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-30T00:30:24Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(
                    repo
                    / ".codex"
                    / "phase-loop"
                    / "runs"
                    / "20260424T190651Z-01-garc-plan"
                    / "launch.json"
                ),
                "in_flight_path": str(
                    repo
                    / ".codex"
                    / "phase-loop"
                    / "runs"
                    / "20260427T075236Z-05-idxsafe-repair"
                    / "terminal-summary.json"
                ),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / ".codex" / "phase-loop" / "runs" / "20260424T190651Z-01-garc-plan" / "launch.json"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T075236Z-05-idxsafe-repair"
        / "terminal-summary.json"
    )


def test_get_repository_status_preserves_legacy_codex_phase_loop_relapse_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-30T01:52:29Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(
                    repo
                    / ".codex"
                    / "phase-loop"
                    / "runs"
                    / "20260427T081107Z-08-ciflow-plan"
                    / "terminal-summary.json"
                ),
                "in_flight_path": str(
                    repo
                    / ".codex"
                    / "phase-loop"
                    / "runs"
                    / "20260427T081107Z-08-ciflow-plan"
                    / "launch.json"
                ),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T081107Z-08-ciflow-plan"
        / "terminal-summary.json"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / ".codex" / "phase-loop" / "runs" / "20260427T081107Z-08-ciflow-plan" / "launch.json"
    )
    assert "run_comprehensive_query_test.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "index_all_repos_semantic_full.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )
    assert "test_garc_rc_soak_contract.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "test_garel_ga_release_contract.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_get_repository_status_preserves_legacy_codex_phase_loop_heartbeat_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-30T02:53:44Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(
                    repo
                    / ".codex"
                    / "phase-loop"
                    / "runs"
                    / "20260427T071207Z-01-artpub-plan"
                    / "launch.json"
                ),
                "in_flight_path": str(
                    repo
                    / ".codex"
                    / "phase-loop"
                    / "runs"
                    / "20260427T071207Z-01-artpub-plan"
                    / "heartbeat.json"
                ),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / ".codex" / "phase-loop" / "runs" / "20260427T071207Z-01-artpub-plan" / "launch.json"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T071207Z-01-artpub-plan"
        / "heartbeat.json"
    )
    assert "test_route_auth_coverage.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "test_p24_sandbox_degradation.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )
    assert "20260427T081107Z-08-ciflow-plan" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_get_repository_status_preserves_docs_truth_tail_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T19:33:16Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "tests" / "docs" / "test_p23_doc_truth.py"),
                "in_flight_path": str(
                    repo / "tests" / "docs" / "test_semdogfood_evidence_contract.py"
                ),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "tests" / "docs" / "test_p23_doc_truth.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "tests" / "docs" / "test_semdogfood_evidence_contract.py"
    )
    assert "test_garel_ga_release_contract.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "mock_plugin/plugin.py" not in (status["force_full_exit_trace"]["in_flight_path"] or "")


def test_get_repository_status_preserves_test_repo_index_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T22:08:25Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "scripts" / "check_test_index_schema.py"),
                "in_flight_path": str(repo / "scripts" / "ensure_test_repos_indexed.py"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "scripts" / "check_test_index_schema.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "scripts" / "ensure_test_repos_indexed.py"
    )
    assert "scripts/consolidate_real_performance_data.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "tests/docs/test_p8_deployment_security.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_get_repository_status_preserves_missing_repo_semantic_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T22:27:04Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "scripts" / "index_missing_repos_semantic.py"),
                "in_flight_path": str(repo / "scripts" / "identify_working_indexes.py"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "scripts" / "index_missing_repos_semantic.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "scripts" / "identify_working_indexes.py"
    )
    assert "scripts/check_test_index_schema.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "scripts/ensure_test_repos_indexed.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_get_repository_status_preserves_utility_verification_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T22:44:15Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(
                    repo / "scripts" / "utilities" / "prepare_index_for_upload.py"
                ),
                "in_flight_path": str(repo / "scripts" / "utilities" / "verify_tool_usage.py"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "scripts" / "utilities" / "prepare_index_for_upload.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "scripts" / "utilities" / "verify_tool_usage.py"
    )
    assert "scripts/index_missing_repos_semantic.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "scripts/identify_working_indexes.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_get_repository_status_preserves_qdrant_report_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T23:02:25Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "scripts" / "map_repos_to_qdrant.py"),
                "in_flight_path": str(repo / "scripts" / "create_claude_code_aware_report.py"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "scripts" / "map_repos_to_qdrant.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "scripts" / "create_claude_code_aware_report.py"
    )
    assert "scripts/utilities/prepare_index_for_upload.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "scripts/utilities/verify_tool_usage.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_get_repository_status_preserves_mock_plugin_fixture_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T18:06:41Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(
                    repo / "tests" / "security" / "fixtures" / "mock_plugin" / "plugin.py"
                ),
                "in_flight_path": str(
                    repo / "tests" / "security" / "fixtures" / "mock_plugin" / "__init__.py"
                ),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "tests" / "security" / "fixtures" / "mock_plugin" / "plugin.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "tests" / "security" / "fixtures" / "mock_plugin" / "__init__.py"
    )
    assert "test_gaclose_evidence_closeout.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "consolidate_real_performance_data.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_get_repository_status_preserves_route_auth_sandbox_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-30T02:33:57Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(
                    repo / "tests" / "security" / "test_route_auth_coverage.py"
                ),
                "in_flight_path": str(
                    repo / "tests" / "security" / "test_p24_sandbox_degradation.py"
                ),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "tests" / "security" / "test_route_auth_coverage.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "tests" / "security" / "test_p24_sandbox_degradation.py"
    )
    assert "test_swift_plugin.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "fixtures/mock_plugin/plugin.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_get_repository_status_preserves_late_v7_phase_plan_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T15:51:12Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(
                    repo / "plans" / "phase-plan-v7-SEMCODEXLOOPRELAPSETAIL.md"
                ),
                "in_flight_path": str(repo / "plans" / "phase-plan-v7-SEMGARELTAIL.md"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "plans" / "phase-plan-v7-SEMCODEXLOOPRELAPSETAIL.md"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "plans" / "phase-plan-v7-SEMGARELTAIL.md"
    )
    assert "phase-plan-v5-garecut.md" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_force_full_progress_callback_preserves_archive_tail_successor_handoff(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)

    manager = GitAwareIndexManager(registry=MagicMock(), dispatcher=MagicMock())
    callback = manager._make_force_full_progress_callback(
        repo_info=repo_info,
        current_commit=commit,
        indexed_commit_before="oldercommit",
    )

    prior_file = (
        repo / "analysis_archive" / "scripts_archive" / "scripts_test_files" / "verify_mcp_fix.py"
    )
    prior_file.parent.mkdir(parents=True, exist_ok=True)
    archive_json = repo / "analysis_archive" / "semantic_vs_sql_comparison_1750926162.json"

    callback(
        {
            "stage": "lexical_walking",
            "stage_family": "lexical",
            "blocker_source": "lexical_mutation",
            "last_progress_path": str(prior_file),
            "in_flight_path": str(archive_json),
            "semantic_stage": "not_run",
            "lexical_stage": "walking",
        }
    )

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    first_trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert first_trace["last_progress_path"] == str(prior_file)
    assert first_trace["in_flight_path"] == str(archive_json)

    callback(
        {
            "stage": "force_full_closeout_handoff",
            "stage_family": "final_closeout",
            "blocker_source": "final_closeout",
            "last_progress_path": str(archive_json),
            "in_flight_path": None,
            "semantic_stage": "not_run",
            "lexical_stage": "completed",
        }
    )

    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["last_progress_path"] == str(archive_json)
    assert trace["in_flight_path"] is None
    assert str(prior_file) not in (trace.get("in_flight_path") or "")
    assert ".devcontainer/devcontainer.json" not in (trace.get("last_progress_path") or "")
    assert "docs/benchmarks/production_benchmark.md" not in (trace.get("last_progress_path") or "")


def test_force_full_sync_durable_trace_moves_past_late_v7_phase_plan_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_doc = repo / "plans" / "phase-plan-v7-SEMCODEXLOOPRELAPSETAIL.md"
    blocked_doc = repo / "plans" / "phase-plan-v7-SEMGARELTAIL.md"
    later_doc = repo / "plans" / "phase-plan-v6-WATCH.md"
    later_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing plans/phase-plan-v6-WATCH.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_doc),
                "in_flight_path": str(later_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing "
                        "plans/phase-plan-v6-WATCH.md"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_doc)
    assert trace["in_flight_path"] == str(later_doc)
    assert str(prior_doc) in {trace["last_progress_path"], trace["in_flight_path"]} or str(
        blocked_doc
    ) in {trace["last_progress_path"], trace["in_flight_path"]}
    assert "phase-plan-v5-garecut.md" not in (trace.get("last_progress_path") or "")


def test_force_full_sync_durable_trace_moves_past_docs_truth_tail_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_doc = repo / "tests" / "docs" / "test_p23_doc_truth.py"
    blocked_doc = repo / "tests" / "docs" / "test_semdogfood_evidence_contract.py"
    later_doc = repo / "specs" / "phase-plans-v7.md"
    later_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing specs/phase-plans-v7.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_doc),
                "in_flight_path": str(later_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing specs/phase-plans-v7.md"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_doc)
    assert trace["in_flight_path"] == str(later_doc)
    assert str(prior_doc) not in (trace.get("in_flight_path") or "")
    assert "test_garel_ga_release_contract.py" not in (trace.get("last_progress_path") or "")
    assert "mock_plugin/plugin.py" not in (trace.get("last_progress_path") or "")


def test_force_full_sync_durable_trace_moves_past_historical_phase_plan_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_doc = repo / "plans" / "phase-plan-v6-WATCH.md"
    blocked_doc = repo / "plans" / "phase-plan-v1-p19.md"
    later_doc = repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"
    later_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing "
                "docs/status/SEMANTIC_DOGFOOD_REBUILD.md"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_doc),
                "in_flight_path": str(later_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing "
                        "docs/status/SEMANTIC_DOGFOOD_REBUILD.md"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_doc)
    assert trace["in_flight_path"] == str(later_doc)
    assert str(prior_doc) in {trace["last_progress_path"], trace["in_flight_path"]} or str(
        blocked_doc
    ) in {trace["last_progress_path"], trace["in_flight_path"]}
    assert "phase-plan-v7-SEMCODEXLOOPRELAPSETAIL.md" not in (trace.get("last_progress_path") or "")


def test_get_repository_status_preserves_historical_v1_phase_plan_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T20:12:35Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "plans" / "phase-plan-v1-p13.md"),
                "in_flight_path": str(repo / "plans" / "phase-plan-v1-p3.md"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "plans" / "phase-plan-v1-p13.md"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "plans" / "phase-plan-v1-p3.md"
    )
    assert "docs/markdown-table-of-contents.md" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_get_repository_status_preserves_optimized_final_report_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    report_dir = repo / "final_optimized_report_final_report_1750958096"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_json = report_dir / "final_report_data.json"
    report_markdown = report_dir / "FINAL_OPTIMIZED_ANALYSIS_REPORT.md"
    report_json.write_text(
        '{"business_metrics": {"time_reduction_percent": 81.0}}\n', encoding="utf-8"
    )
    report_markdown.write_text(
        "# Optimized Enhanced MCP Analysis - Final Report\n", encoding="utf-8"
    )
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T21:15:52Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(report_json),
                "in_flight_path": str(report_markdown),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(report_json)
    assert status["force_full_exit_trace"]["in_flight_path"] == str(report_markdown)
    assert "phase-plan-v7-SEMCROSSDOGTAIL.md" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_force_full_sync_durable_trace_moves_past_historical_v1_phase_plan_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_doc = repo / "plans" / "phase-plan-v1-p13.md"
    blocked_doc = repo / "plans" / "phase-plan-v1-p3.md"
    later_doc = repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"
    in_flight_doc = repo / "ai_docs" / "qdrant.md"
    later_doc.parent.mkdir(parents=True, exist_ok=True)
    in_flight_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing ai_docs/qdrant.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(later_doc),
                "in_flight_path": str(in_flight_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing ai_docs/qdrant.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(later_doc)
    assert trace["in_flight_path"] == str(in_flight_doc)
    assert str(prior_doc) not in {trace["last_progress_path"], trace["in_flight_path"]}
    assert str(blocked_doc) not in {trace["last_progress_path"], trace["in_flight_path"]}


def test_force_full_sync_durable_trace_moves_past_optimized_final_report_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    report_dir = repo / "final_optimized_report_final_report_1750958096"
    report_dir.mkdir(parents=True, exist_ok=True)
    prior_doc = report_dir / "final_report_data.json"
    blocked_doc = report_dir / "FINAL_OPTIMIZED_ANALYSIS_REPORT.md"
    later_doc = repo / "scripts" / "generate_final_optimized_report.py"
    later_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing scripts/generate_final_optimized_report.py"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_doc),
                "in_flight_path": str(later_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing "
                        "scripts/generate_final_optimized_report.py"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_doc)
    assert trace["in_flight_path"] == str(later_doc)
    assert str(prior_doc) not in {trace["last_progress_path"], trace["in_flight_path"]}
    assert "phase-plan-v7-SEMCROSSDOGTAIL.md" not in (trace.get("last_progress_path") or "")


def test_get_repository_status_preserves_semjedi_p4_phase_plan_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T20:38:17Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "plans" / "phase-plan-v7-SEMJEDI.md"),
                "in_flight_path": str(repo / "plans" / "phase-plan-v1-p4.md"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "plans" / "phase-plan-v7-SEMJEDI.md"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "plans" / "phase-plan-v1-p4.md"
    )
    assert "phase-plan-v1-p13.md" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_force_full_sync_durable_trace_moves_past_semjedi_p4_phase_plan_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_doc = repo / "plans" / "phase-plan-v7-SEMJEDI.md"
    blocked_doc = repo / "plans" / "phase-plan-v1-p4.md"
    later_doc = repo / "specs" / "phase-plans-v7.md"
    in_flight_doc = repo / "ai_docs" / "qdrant.md"
    later_doc.parent.mkdir(parents=True, exist_ok=True)
    in_flight_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing ai_docs/qdrant.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(later_doc),
                "in_flight_path": str(in_flight_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing ai_docs/qdrant.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(later_doc)
    assert trace["in_flight_path"] == str(in_flight_doc)
    assert str(prior_doc) not in {trace["last_progress_path"], trace["in_flight_path"]}
    assert str(blocked_doc) not in {trace["last_progress_path"], trace["in_flight_path"]}


def test_get_repository_status_preserves_support_docs_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T19:52:03Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "docs" / "markdown-table-of-contents.md"),
                "in_flight_path": str(repo / "docs" / "SUPPORT_MATRIX.md"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "docs" / "markdown-table-of-contents.md"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "docs" / "SUPPORT_MATRIX.md"
    )
    assert "test_semdogfood_evidence_contract.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_get_repository_status_preserves_integration_obs_smoke_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    integration_init = repo / "tests" / "integration" / "__init__.py"
    obs_smoke = repo / "tests" / "integration" / "obs" / "test_obs_smoke.py"
    obs_smoke.parent.mkdir(parents=True, exist_ok=True)
    integration_init.write_text('"""Integration tests for Code-Index-MCP."""\n', encoding="utf-8")
    obs_smoke.write_text(
        "def test_secret_redaction_via_http():\n    assert True\n", encoding="utf-8"
    )
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-30T00:46:51Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(integration_init),
                "in_flight_path": str(obs_smoke),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(integration_init)
    assert status["force_full_exit_trace"]["in_flight_path"] == str(obs_smoke)
    assert ".codex/phase-loop/runs/20260424T190651Z-01-garc-plan/launch.json" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_force_full_sync_durable_trace_moves_past_support_docs_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_doc = repo / "docs" / "markdown-table-of-contents.md"
    blocked_doc = repo / "docs" / "SUPPORT_MATRIX.md"
    later_doc = repo / "ai_docs" / "qdrant.md"
    later_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing ai_docs/qdrant.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_doc),
                "in_flight_path": str(later_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing ai_docs/qdrant.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_doc)
    assert trace["in_flight_path"] == str(later_doc)
    assert str(prior_doc) not in (trace.get("in_flight_path") or "")
    assert "test_semdogfood_evidence_contract.py" not in (trace.get("last_progress_path") or "")


def test_get_repository_status_preserves_optimized_upload_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T23:19:22Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "scripts" / "execute_optimized_analysis.py"),
                "in_flight_path": str(repo / "scripts" / "index-artifact-upload-v2.py"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "scripts" / "execute_optimized_analysis.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "scripts" / "index-artifact-upload-v2.py"
    )
    assert "create_claude_code_aware_report.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_get_repository_status_preserves_edit_retrieval_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T23:37:26Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "scripts" / "analyze_claude_code_edits.py"),
                "in_flight_path": str(repo / "scripts" / "verify_mcp_retrieval.py"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "scripts" / "analyze_claude_code_edits.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "scripts" / "verify_mcp_retrieval.py"
    )
    assert "execute_optimized_analysis.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "index-artifact-upload-v2.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_force_full_sync_durable_trace_moves_past_optimized_upload_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_script = repo / "scripts" / "execute_optimized_analysis.py"
    blocked_script = repo / "scripts" / "index-artifact-upload-v2.py"
    later_file = repo / "docs" / "status" / "semantic_tail_8.md"
    later_file.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing docs/status/semantic_tail_8.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_script),
                "in_flight_path": str(later_file),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing docs/status/semantic_tail_8.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_script)
    assert trace["in_flight_path"] == str(later_file)
    assert str(prior_script) not in (trace.get("in_flight_path") or "")
    assert "create_claude_code_aware_report.py" not in (trace.get("last_progress_path") or "")


def test_force_full_sync_durable_trace_moves_past_edit_retrieval_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_script = repo / "scripts" / "analyze_claude_code_edits.py"
    blocked_script = repo / "scripts" / "verify_mcp_retrieval.py"
    later_file = repo / "docs" / "status" / "semantic_tail_9.md"
    later_file.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing docs/status/semantic_tail_9.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_script),
                "in_flight_path": str(later_file),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing docs/status/semantic_tail_9.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_script)
    assert trace["in_flight_path"] == str(later_file)
    assert str(prior_script) not in (trace.get("in_flight_path") or "")
    assert "execute_optimized_analysis.py" not in (trace.get("last_progress_path") or "")


def test_get_repository_status_preserves_comprehensive_query_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-30T00:12:10Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "scripts" / "run_comprehensive_query_test.py"),
                "in_flight_path": str(repo / "scripts" / "index_all_repos_semantic_full.py"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "scripts" / "run_comprehensive_query_test.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "scripts" / "index_all_repos_semantic_full.py"
    )
    assert "analyze_claude_code_edits.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "verify_mcp_retrieval.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_get_repository_status_preserves_swift_database_efficiency_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-30T02:14:12Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "tests" / "root_tests" / "test_swift_plugin.py"),
                "in_flight_path": str(
                    repo / "tests" / "root_tests" / "test_mcp_database_efficiency.py"
                ),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "tests" / "root_tests" / "test_swift_plugin.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "tests" / "root_tests" / "test_mcp_database_efficiency.py"
    )
    assert ".codex/phase-loop" not in (status["force_full_exit_trace"]["last_progress_path"] or "")
    assert "tests/root_tests/run_reranking_tests.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_get_repository_status_preserves_centralization_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-30T01:13:41Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "scripts" / "real_strategic_recommendations.py"),
                "in_flight_path": str(repo / "scripts" / "migrate_to_centralized.py"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "scripts" / "real_strategic_recommendations.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "scripts" / "migrate_to_centralized.py"
    )
    assert "tests/integration/__init__.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "tests/integration/obs/test_obs_smoke.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_get_repository_status_preserves_reindex_demo_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-30T05:58:46Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "scripts" / "reindex_current_repository.py"),
                "in_flight_path": str(repo / "scripts" / "demo_centralized_indexes.py"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "scripts" / "reindex_current_repository.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "scripts" / "demo_centralized_indexes.py"
    )
    assert "plans/phase-plan-v7-SEMVERIFYSIMTAIL.md" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "plans/phase-plan-v7-SEMAPIDOCSTAIL.md" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_force_full_sync_durable_trace_moves_past_comprehensive_query_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_script = repo / "scripts" / "run_comprehensive_query_test.py"
    blocked_script = repo / "scripts" / "index_all_repos_semantic_full.py"
    later_file = repo / "docs" / "status" / "semantic_tail_10.md"
    later_file.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing docs/status/semantic_tail_10.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_script),
                "in_flight_path": str(later_file),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing docs/status/semantic_tail_10.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_script)
    assert trace["in_flight_path"] == str(later_file)
    assert str(prior_script) not in (trace.get("in_flight_path") or "")
    assert "analyze_claude_code_edits.py" not in (trace.get("last_progress_path") or "")


def test_force_full_sync_terminalizes_running_trace_when_swift_database_efficiency_pair_crashes(
    tmp_path,
):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    repo_info.last_indexed_commit = "older-indexed-commit"
    repo_info.index_path.touch()

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = _make_manager(registry)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)

    prior_file = repo / "tests" / "root_tests" / "test_swift_plugin.py"
    blocked_file = repo / "tests" / "root_tests" / "test_mcp_database_efficiency.py"

    def _crashing_full_index(repo_id, _ctx, progress_callback=None):
        assert progress_callback is not None
        progress_callback(
            {
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "blocker_source": "lexical_mutation",
                "lexical_stage": "walking",
                "semantic_stage": "not_run",
                "last_progress_path": str(prior_file),
                "in_flight_path": str(blocked_file),
                "summary_call_timed_out": False,
                "summary_call_file_path": None,
                "summary_call_chunk_ids": [],
                "summary_call_timeout_seconds": None,
            }
        )
        raise RuntimeError("synthetic crash after swift/database-efficiency pair")

    manager._full_index = MagicMock(side_effect=_crashing_full_index)

    with pytest.raises(RuntimeError, match="synthetic crash after swift/database-efficiency pair"):
        manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["status"] == "interrupted"
    assert trace["stage"] == "lexical_walking"
    assert trace["stage_family"] == "lexical"
    assert trace["current_commit"] == commit
    assert trace["indexed_commit_before"] == "older-indexed-commit"
    assert trace["last_progress_path"] == str(prior_file)
    assert trace["in_flight_path"] == str(blocked_file)
    assert trace["blocker_source"] == "lexical_mutation"
    assert ".codex/phase-loop" not in (trace.get("last_progress_path") or "")
    assert "tests/root_tests/run_reranking_tests.py" not in (trace.get("last_progress_path") or "")
    registry.update_indexed_commit.assert_not_called()


def test_force_full_sync_durable_trace_moves_past_centralization_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_script = repo / "scripts" / "real_strategic_recommendations.py"
    blocked_script = repo / "scripts" / "migrate_to_centralized.py"
    later_file = repo / "docs" / "status" / "semantic_tail_12.md"
    later_file.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing docs/status/semantic_tail_12.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_script),
                "in_flight_path": str(later_file),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing docs/status/semantic_tail_12.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_script)
    assert trace["in_flight_path"] == str(later_file)
    assert str(prior_script) not in (trace.get("in_flight_path") or "")
    assert "tests/integration/__init__.py" not in (trace.get("last_progress_path") or "")


def test_force_full_sync_durable_trace_moves_past_swift_database_efficiency_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_file = repo / "tests" / "root_tests" / "test_swift_plugin.py"
    blocked_file = repo / "tests" / "root_tests" / "test_mcp_database_efficiency.py"
    later_file = repo / "docs" / "status" / "semantic_tail_14.md"
    later_file.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing docs/status/semantic_tail_14.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_file),
                "in_flight_path": str(later_file),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing docs/status/semantic_tail_14.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_file)
    assert trace["in_flight_path"] == str(later_file)
    assert str(prior_file) not in (trace.get("in_flight_path") or "")
    assert ".codex/phase-loop" not in (trace.get("last_progress_path") or "")


def test_force_full_sync_durable_trace_moves_past_legacy_codex_phase_loop_heartbeat_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_file = (
        repo / ".codex" / "phase-loop" / "runs" / "20260427T071207Z-01-artpub-plan" / "launch.json"
    )
    blocked_file = (
        repo
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T071207Z-01-artpub-plan"
        / "heartbeat.json"
    )
    later_file = repo / "docs" / "status" / "semantic_tail_15.md"
    later_file.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing docs/status/semantic_tail_15.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_file),
                "in_flight_path": str(later_file),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing docs/status/semantic_tail_15.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_file)
    assert trace["in_flight_path"] == str(later_file)
    assert str(prior_file) not in (trace.get("in_flight_path") or "")
    assert "test_route_auth_coverage.py" not in (trace.get("last_progress_path") or "")


def test_get_repository_status_preserves_legacy_codex_phase_loop_garecut_heartbeat_pair_trace(
    tmp_path,
):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-30T03:26:35Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(
                    repo
                    / ".codex"
                    / "phase-loop"
                    / "runs"
                    / "20260425T051448Z-01-garecut-execute"
                    / "launch.json"
                ),
                "in_flight_path": str(
                    repo
                    / ".codex"
                    / "phase-loop"
                    / "runs"
                    / "20260425T051448Z-01-garecut-execute"
                    / "heartbeat.json"
                ),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260425T051448Z-01-garecut-execute"
        / "launch.json"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260425T051448Z-01-garecut-execute"
        / "heartbeat.json"
    )
    assert "20260427T071207Z-01-artpub-plan" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "20260427T081107Z-08-ciflow-plan" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_force_full_sync_durable_trace_moves_past_legacy_codex_phase_loop_garecut_heartbeat_pair(
    tmp_path,
):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_launch = (
        repo
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260425T051448Z-01-garecut-execute"
        / "launch.json"
    )
    blocked_heartbeat = (
        repo
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260425T051448Z-01-garecut-execute"
        / "heartbeat.json"
    )
    later_file = repo / "docs" / "status" / "semantic_tail_16.md"
    later_file.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing docs/status/semantic_tail_16.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_heartbeat),
                "in_flight_path": str(later_file),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing docs/status/semantic_tail_16.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_heartbeat)
    assert trace["in_flight_path"] == str(later_file)
    assert str(prior_launch) not in (trace.get("in_flight_path") or "")
    assert "20260427T071207Z-01-artpub-plan" not in (trace.get("last_progress_path") or "")
    assert "20260427T081107Z-08-ciflow-plan" not in (trace.get("last_progress_path") or "")


def test_get_repository_status_preserves_legacy_codex_phase_loop_ciflow_execute_relapse_pair_trace(
    tmp_path,
):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-30T04:05:03Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(
                    repo
                    / ".codex"
                    / "phase-loop"
                    / "runs"
                    / "20260427T081704Z-09-ciflow-execute"
                    / "terminal-summary.json"
                ),
                "in_flight_path": str(
                    repo
                    / ".codex"
                    / "phase-loop"
                    / "runs"
                    / "20260427T081704Z-09-ciflow-execute"
                    / "launch.json"
                ),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T081704Z-09-ciflow-execute"
        / "terminal-summary.json"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T081704Z-09-ciflow-execute"
        / "launch.json"
    )
    assert "20260424T190651Z-01-garc-plan" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "20260427T075236Z-05-idxsafe-repair" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_force_full_sync_durable_trace_moves_past_legacy_codex_phase_loop_ciflow_execute_relapse_pair(
    tmp_path,
):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_terminal = (
        repo
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T081704Z-09-ciflow-execute"
        / "terminal-summary.json"
    )
    blocked_launch = (
        repo
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T081704Z-09-ciflow-execute"
        / "launch.json"
    )
    later_heartbeat = (
        repo
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260424T225641Z-01-garel-execute"
        / "heartbeat.json"
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing .codex/phase-loop/runs/20260424T225641Z-01-garel-execute/heartbeat.json"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_launch),
                "in_flight_path": str(later_heartbeat),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing .codex/phase-loop/runs/20260424T225641Z-01-garel-execute/heartbeat.json",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_launch)
    assert trace["in_flight_path"] == str(later_heartbeat)
    assert str(prior_terminal) not in (trace.get("in_flight_path") or "")
    assert "20260424T190651Z-01-garc-plan" not in (trace.get("last_progress_path") or "")


def test_get_repository_status_preserves_p24_plugin_tail_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-30T04:41:42Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "tests" / "test_p24_plugin_availability.py"),
                "in_flight_path": str(repo / "tests" / "test_dispatcher_extension_gating.py"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "tests" / "test_p24_plugin_availability.py"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "tests" / "test_dispatcher_extension_gating.py"
    )
    assert "20260424T225641Z-01-garel-execute" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "20260425T022006Z-01-garecut-plan" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_force_full_sync_durable_trace_moves_past_p24_plugin_tail_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_test = repo / "tests" / "test_p24_plugin_availability.py"
    blocked_test = repo / "tests" / "test_dispatcher_extension_gating.py"
    later_doc = repo / "tests" / "docs" / "test_semincr_contract.py"
    later_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing tests/docs/test_semincr_contract.py"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(blocked_test),
                "in_flight_path": str(later_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing tests/docs/test_semincr_contract.py",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(blocked_test)
    assert trace["in_flight_path"] == str(later_doc)
    assert str(prior_test) not in (trace.get("in_flight_path") or "")
    assert "20260424T225641Z-01-garel-execute" not in (trace.get("last_progress_path") or "")


def test_get_repository_status_preserves_architecture_api_markdown_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-30T04:59:53Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "docs" / "architecture" / "P2B-known-limits.md"),
                "in_flight_path": str(repo / "docs" / "api" / "API-REFERENCE.md"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "docs" / "architecture" / "P2B-known-limits.md"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "docs" / "api" / "API-REFERENCE.md"
    )
    assert "tests/test_p24_plugin_availability.py" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )
    assert "tests/test_dispatcher_extension_gating.py" not in (
        status["force_full_exit_trace"]["in_flight_path"] or ""
    )


def test_force_full_sync_durable_trace_moves_past_architecture_api_markdown_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_doc = repo / "docs" / "architecture" / "P2B-known-limits.md"
    blocked_doc = repo / "docs" / "api" / "API-REFERENCE.md"
    later_doc = repo / "docs" / "runbooks" / "operator-guide.md"
    in_flight_doc = repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"
    later_doc.parent.mkdir(parents=True, exist_ok=True)
    in_flight_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing docs/status/SEMANTIC_DOGFOOD_REBUILD.md"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(later_doc),
                "in_flight_path": str(in_flight_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing "
                        "docs/status/SEMANTIC_DOGFOOD_REBUILD.md"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(later_doc)
    assert trace["in_flight_path"] == str(in_flight_doc)
    assert str(prior_doc) not in {trace["last_progress_path"], trace["in_flight_path"]}
    assert str(blocked_doc) not in {trace["last_progress_path"], trace["in_flight_path"]}


def test_get_repository_status_preserves_mixed_version_phase_plan_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-29T16:29:12Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "plans" / "phase-plan-v7-SEMPHASETAIL.md"),
                "in_flight_path": str(repo / "plans" / "phase-plan-v5-gagov.md"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "plans" / "phase-plan-v7-SEMPHASETAIL.md"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "plans" / "phase-plan-v5-gagov.md"
    )
    assert "ai_docs/celery_overview.md" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_get_repository_status_preserves_rebound_phase_plan_pair_trace(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace_path.write_text(
        json.dumps(
            {
                "status": "interrupted",
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "trace_timestamp": "2026-04-30T05:36:47Z",
                "current_commit": commit,
                "indexed_commit_before": "older-indexed-commit",
                "last_progress_path": str(repo / "plans" / "phase-plan-v7-SEMVERIFYSIMTAIL.md"),
                "in_flight_path": str(repo / "plans" / "phase-plan-v7-SEMAPIDOCSTAIL.md"),
                "blocker_source": "lexical_mutation",
            }
        ),
        encoding="utf-8",
    )

    registry = MagicMock()
    registry.get_repository.return_value = repo_info

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    status = manager.get_repository_status(repo_info.repository_id)

    assert status["force_full_exit_trace"]["status"] == "interrupted"
    assert status["force_full_exit_trace"]["stage"] == "lexical_walking"
    assert status["force_full_exit_trace"]["stage_family"] == "lexical"
    assert status["force_full_exit_trace"]["last_progress_path"] == str(
        repo / "plans" / "phase-plan-v7-SEMVERIFYSIMTAIL.md"
    )
    assert status["force_full_exit_trace"]["in_flight_path"] == str(
        repo / "plans" / "phase-plan-v7-SEMAPIDOCSTAIL.md"
    )
    assert "phase-plan-v7-SEMCODEXLOOPRELAPSETAIL.md" not in (
        status["force_full_exit_trace"]["last_progress_path"] or ""
    )


def test_force_full_sync_durable_trace_moves_past_rebound_phase_plan_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_doc = repo / "plans" / "phase-plan-v7-SEMVERIFYSIMTAIL.md"
    blocked_doc = repo / "plans" / "phase-plan-v7-SEMAPIDOCSTAIL.md"
    later_doc = repo / "docs" / "status" / "semantic_tail_4.md"
    in_flight_doc = repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"
    later_doc.parent.mkdir(parents=True, exist_ok=True)
    in_flight_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing docs/status/SEMANTIC_DOGFOOD_REBUILD.md"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(later_doc),
                "in_flight_path": str(in_flight_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing "
                        "docs/status/SEMANTIC_DOGFOOD_REBUILD.md"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(later_doc)
    assert trace["in_flight_path"] == str(in_flight_doc)
    assert str(prior_doc) not in {trace["last_progress_path"], trace["in_flight_path"]}
    assert str(blocked_doc) not in {trace["last_progress_path"], trace["in_flight_path"]}


def test_force_full_sync_durable_trace_moves_past_mixed_version_phase_plan_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    prior_doc = repo / "plans" / "phase-plan-v7-SEMPHASETAIL.md"
    blocked_doc = repo / "plans" / "phase-plan-v5-gagov.md"
    later_doc = repo / "ai_docs" / "celery_overview.md"
    in_flight_doc = repo / "ai_docs" / "qdrant.md"
    later_doc.parent.mkdir(parents=True, exist_ok=True)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=3,
            failed=1,
            errors=["Lexical indexing timed out while processing ai_docs/qdrant.md"],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(later_doc),
                "in_flight_path": str(in_flight_doc),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": "Lexical indexing timed out while processing ai_docs/qdrant.md",
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert trace["status"] == "completed"
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    assert trace["blocker_source"] == "lexical_mutation"
    assert trace["last_progress_path"] == str(later_doc)
    assert trace["in_flight_path"] == str(in_flight_doc)
    assert str(prior_doc) not in {trace["last_progress_path"], trace["in_flight_path"]}
    assert str(blocked_doc) not in {trace["last_progress_path"], trace["in_flight_path"]}


def test_force_full_sync_trace_moves_past_visual_report_script_after_exact_python_repair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=2,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing docs/status/SEMANTIC_DOGFOOD_REBUILD.md"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 3,
                "lexical_files_completed": 2,
                "last_progress_path": str(repo / "src" / "semantic_preflight.py"),
                "in_flight_path": str(repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing " "SEMANTIC_DOGFOOD_REBUILD.md"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert "create_multi_repo_visual_report.py" not in (trace.get("last_progress_path") or "")
    assert "create_multi_repo_visual_report.py" not in (trace.get("in_flight_path") or "")
    assert trace["last_progress_path"] == str(repo / "src" / "semantic_preflight.py")
    assert trace["in_flight_path"] == str(repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md")
    assert trace["stage"] == "force_full_failed"


def test_force_full_sync_trace_moves_past_preflight_upgrade_script_pair_after_exact_repair(
    tmp_path,
):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=2,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing docs/status/SEMANTIC_DOGFOOD_REBUILD.md"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 3,
                "lexical_files_completed": 2,
                "last_progress_path": str(repo / "docs" / "status" / "semantic_tail.md"),
                "in_flight_path": str(repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing " "SEMANTIC_DOGFOOD_REBUILD.md"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert "preflight_upgrade.sh" not in (trace.get("last_progress_path") or "")
    assert "preflight_upgrade.sh" not in (trace.get("in_flight_path") or "")
    assert "test_mcp_protocol_direct.py" not in (trace.get("last_progress_path") or "")
    assert "test_mcp_protocol_direct.py" not in (trace.get("in_flight_path") or "")
    assert trace["last_progress_path"] == str(repo / "docs" / "status" / "semantic_tail.md")
    assert trace["in_flight_path"] == str(repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md")
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    registry.update_indexed_commit.assert_not_called()


def test_force_full_sync_trace_moves_past_verify_simulator_script_pair_after_exact_repair(
    tmp_path,
):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=2,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing docs/status/SEMANTIC_DOGFOOD_REBUILD.md"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 3,
                "lexical_files_completed": 2,
                "last_progress_path": str(repo / "docs" / "status" / "semantic_tail_2.md"),
                "in_flight_path": str(repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing " "SEMANTIC_DOGFOOD_REBUILD.md"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert "verify_embeddings.py" not in (trace.get("last_progress_path") or "")
    assert "verify_embeddings.py" not in (trace.get("in_flight_path") or "")
    assert "claude_code_behavior_simulator.py" not in (trace.get("last_progress_path") or "")
    assert "claude_code_behavior_simulator.py" not in (trace.get("in_flight_path") or "")
    assert trace["last_progress_path"] == str(repo / "docs" / "status" / "semantic_tail_2.md")
    assert trace["in_flight_path"] == str(repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md")
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    registry.update_indexed_commit.assert_not_called()


def test_force_full_sync_trace_moves_past_script_language_audit_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=2,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing docs/status/SEMANTIC_DOGFOOD_REBUILD.md"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"),
                "in_flight_path": str(repo / "docs" / "status" / "semantic_tail.md"),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing " "SEMANTIC_DOGFOOD_REBUILD.md"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert "scripts/migrate_large_index_to_multi_repo.py" not in (
        trace.get("last_progress_path") or ""
    )
    assert "scripts/check_index_languages.py" not in (trace.get("last_progress_path") or "")
    assert "scripts/migrate_large_index_to_multi_repo.py" not in (trace.get("in_flight_path") or "")
    assert "scripts/check_index_languages.py" not in (trace.get("in_flight_path") or "")
    assert ".claude/commands/plan-phase.md" not in (trace.get("last_progress_path") or "")
    assert "tests/root_tests/run_reranking_tests.py" not in (trace.get("last_progress_path") or "")
    assert trace["last_progress_path"] == str(
        repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"
    )
    assert trace["in_flight_path"] == str(repo / "docs" / "status" / "semantic_tail.md")
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    registry.update_indexed_commit.assert_not_called()


def test_force_full_sync_trace_moves_past_embed_consolidation_pair(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}

    manager = GitAwareIndexManager(registry=registry, dispatcher=MagicMock())
    manager._resolve_ctx = MagicMock(return_value=ctx)
    manager._index_exists = MagicMock(return_value=True)
    manager._index_path_has_durable_rows = MagicMock(return_value=True)
    manager._full_index = MagicMock(
        return_value=UpdateResult(
            indexed=2,
            failed=1,
            errors=[
                "Lexical indexing timed out while processing docs/status/SEMANTIC_DOGFOOD_REBUILD.md"
            ],
            low_level={
                "lexical_stage": "blocked_file_timeout",
                "lexical_files_attempted": 4,
                "lexical_files_completed": 3,
                "last_progress_path": str(repo / "docs" / "status" / "semantic_tail_3.md"),
                "in_flight_path": str(repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"),
                "low_level_blocker": {
                    "code": "lexical_file_timeout",
                    "message": (
                        "Lexical indexing timed out while processing " "SEMANTIC_DOGFOOD_REBUILD.md"
                    ),
                },
            },
            semantic={"semantic_stage": "not_run"},
        )
    )

    result = manager.sync_repository_index(repo_info.repository_id, force_full=True)

    trace_path = Path(repo_info.index_location) / "force_full_exit_trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert result.action == "failed"
    assert "create_semantic_embeddings.py" not in (trace.get("last_progress_path") or "")
    assert "create_semantic_embeddings.py" not in (trace.get("in_flight_path") or "")
    assert "consolidate_real_performance_data.py" not in (trace.get("last_progress_path") or "")
    assert "consolidate_real_performance_data.py" not in (trace.get("in_flight_path") or "")
    assert trace["last_progress_path"] == str(repo / "docs" / "status" / "semantic_tail_3.md")
    assert trace["in_flight_path"] == str(repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md")
    assert trace["stage"] == "force_full_failed"
    assert trace["stage_family"] == "final_closeout"
    registry.update_indexed_commit.assert_not_called()


def test_incremental_update_aggregates_semantic_mutation_stats(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _get_head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    ctx = _make_ctx(repo_info.repository_id, repo, repo_info.index_path)

    class SemanticDispatcher(CtxSignatureDispatcher):
        def index_file(self, ctx, path):
            result = super().index_file(ctx, path)
            result.semantic = {
                "summaries_written": 1,
                "summary_chunks_attempted": 1,
                "semantic_indexed": 1,
                "semantic_stage": "indexed",
            }
            return result

        def move_file(self, ctx, old_path, new_path, content_hash=None):
            result = super().move_file(ctx, old_path, new_path, content_hash)
            result.semantic = {
                "vectors_deleted": 2,
                "summaries_preserved": 1,
                "semantic_stage": "indexed",
            }
            return result

    dispatcher = SemanticDispatcher()
    manager = GitAwareIndexManager(
        registry=MagicMock(get_repository=MagicMock(return_value=repo_info)), dispatcher=dispatcher
    )
    changes = ChangeSet(
        added=[],
        modified=["hello.py"],
        deleted=[],
        renamed=[("old.py", "hello.py")],
    )

    result = manager._incremental_index_update(repo_info.repository_id, ctx, changes)

    assert result.failed == 0
    assert result.semantic is not None
    assert result.semantic["summaries_written"] == 1
    assert result.semantic["semantic_indexed"] == 1
    assert result.semantic["vectors_deleted"] == 2
    assert result.semantic["summaries_preserved"] == 1
