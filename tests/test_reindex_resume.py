"""Tests for P13 SL-1: checkpoint-resume for reindexing."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from mcp_server.core.path_resolver import PathResolver
from mcp_server.core.repo_context import RepoContext
from mcp_server.indexing.change_detector import FileChange
from mcp_server.indexing.checkpoint import (
    REINDEX_STATE_VERSION,
    ReindexCheckpoint,
    clear,
    load,
    save,
)
from mcp_server.indexing.incremental_indexer import IncrementalIndexer
from mcp_server.indexing.lock_registry import IndexingLockRegistry, lock_registry
from mcp_server.storage.multi_repo_manager import RepositoryInfo
from mcp_server.storage.sqlite_store import SQLiteStore

# ---------------------------------------------------------------------------
# SL-1.1: checkpoint schema + helpers
# ---------------------------------------------------------------------------


def _make_ckpt(**kwargs) -> ReindexCheckpoint:
    defaults = dict(
        repo_id="abc123",
        started_at="2026-01-01T00:00:00Z",
        last_completed_path="src/foo.py",
        remaining_paths=["src/foo.py", "src/bar.py"],
        errors=[{"path": "src/bad.py", "error": "oops"}],
        schema_version=REINDEX_STATE_VERSION,
    )
    defaults.update(kwargs)
    return ReindexCheckpoint(**defaults)


def test_roundtrip_all_fields(tmp_path: Path) -> None:
    ckpt = _make_ckpt()
    save(ckpt, tmp_path)
    loaded = load(tmp_path)
    assert loaded is not None
    assert loaded.repo_id == ckpt.repo_id
    assert loaded.started_at == ckpt.started_at
    assert loaded.last_completed_path == ckpt.last_completed_path
    assert loaded.remaining_paths == ckpt.remaining_paths
    assert loaded.errors == ckpt.errors
    assert loaded.schema_version == ckpt.schema_version


def test_load_missing_file_returns_none(tmp_path: Path) -> None:
    assert load(tmp_path) is None


def test_load_schema_version_mismatch_returns_none(tmp_path: Path) -> None:
    state_file = tmp_path / ".reindex-state"
    state_file.write_text(
        json.dumps(
            {
                "repo_id": "x",
                "started_at": "2026-01-01T00:00:00Z",
                "last_completed_path": "",
                "remaining_paths": [],
                "errors": [],
                "schema_version": 999,
            }
        )
    )
    assert load(tmp_path) is None


def test_load_schema_version_zero_returns_none(tmp_path: Path) -> None:
    state_file = tmp_path / ".reindex-state"
    state_file.write_text(
        json.dumps(
            {
                "repo_id": "x",
                "started_at": "2026-01-01T00:00:00Z",
                "last_completed_path": "",
                "remaining_paths": [],
                "errors": [],
                "schema_version": 0,
            }
        )
    )
    assert load(tmp_path) is None


def test_save_is_atomic(tmp_path: Path) -> None:
    """save() must write to a tmp file then rename — no .reindex-state.tmp after completion."""
    ckpt = _make_ckpt()
    save(ckpt, tmp_path)
    assert (tmp_path / ".reindex-state").exists()
    assert not (tmp_path / ".reindex-state.tmp").exists()


def test_clear_removes_state_file(tmp_path: Path) -> None:
    ckpt = _make_ckpt()
    save(ckpt, tmp_path)
    assert (tmp_path / ".reindex-state").exists()
    clear(tmp_path)
    assert not (tmp_path / ".reindex-state").exists()


def test_clear_noop_when_missing(tmp_path: Path) -> None:
    # Should not raise
    clear(tmp_path)


# ---------------------------------------------------------------------------
# SL-1.2 / SL-1.3: resume integration
# ---------------------------------------------------------------------------


class _CountingDispatcher:
    """Dispatcher that records index_file calls and can raise on specific paths."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.indexed: list[Path] = []
        self._fail_on = fail_on

    def index_file(self, *args) -> None:
        path = args[-1]
        if self._fail_on and Path(path).name == self._fail_on:
            raise RuntimeError(f"Simulated failure on {path}")
        self.indexed.append(Path(path))

    def remove_file(self, path: Path) -> None:
        pass

    def move_file(self, old_path: Path, new_path: Path, content_hash: str) -> None:
        pass


@pytest.fixture
def repo_env(tmp_path: Path):
    """Create a repo with 1000 .py files and an IncrementalIndexer."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    # Create 1000 files
    for i in range(1000):
        f = repo_path / f"file_{i:04d}.py"
        f.write_text(f"# file {i}\n")

    store = SQLiteStore(
        str(tmp_path / "code_index.db"),
        path_resolver=PathResolver(repo_path),
    )
    store.create_repository(str(repo_path), "test-repo")

    repo_info = RepositoryInfo(
        repository_id="test-repo",
        name="test-repo",
        path=repo_path,
        index_path=Path(store.db_path),
        language_stats={},
        total_files=0,
        total_symbols=0,
        indexed_at=datetime.now(),
        tracked_branch="main",
    )
    ctx = RepoContext(
        repo_id="test-repo",
        sqlite_store=store,
        workspace_root=repo_path,
        tracked_branch="main",
        registry_entry=repo_info,
    )

    return repo_path, store, ctx


def _make_changes(repo_path: Path, n: int = 1000) -> List[FileChange]:
    return [
        FileChange(
            path=f"file_{i:04d}.py",
            change_type="added",
        )
        for i in range(n)
    ]


def test_crash_at_file_500_leaves_correct_checkpoint(repo_env, tmp_path: Path) -> None:
    """When a process crashes at file #500, the first-error checkpoint survives.

    Post-P17 semantics (SL-2): the checkpoint is cleared unconditionally at
    clean loop exit — so to observe the saved checkpoint we must simulate a
    real process crash by patching ``_clear_ckpt`` to raise.  Any exception
    that escapes ``update_from_changes`` leaves the on-disk checkpoint intact.
    """
    repo_path, store, ctx = repo_env
    fail_name = "file_0499.py"
    dispatcher = _CountingDispatcher(fail_on=fail_name)

    indexer = IncrementalIndexer(
        store=store,
        dispatcher=dispatcher,
        repo_path=repo_path,
        ctx=ctx,
    )

    changes = _make_changes(repo_path)

    class _Crash(Exception):
        pass

    def _simulate_crash(_repo_path):
        raise _Crash("simulated process crash before cleanup")

    with patch(
        "mcp_server.indexing.incremental_indexer._clear_ckpt",
        side_effect=_simulate_crash,
    ):
        with pytest.raises(_Crash):
            indexer.update_from_changes(changes)

    ckpt = load(repo_path)
    assert ckpt is not None, "checkpoint must exist after a crash before cleanup"
    # last_completed_path is paths[498] (last successfully indexed before the failure)
    assert (
        ckpt.last_completed_path == "file_0498.py"
    ), f"expected file_0498.py, got {ckpt.last_completed_path!r}"
    # remaining_paths starts at the failed file
    assert (
        ckpt.remaining_paths[0] == "file_0499.py"
    ), f"expected file_0499.py at remaining[0], got {ckpt.remaining_paths[0]!r}"
    # 1 error recorded
    assert len(ckpt.errors) >= 1
    assert any(e["path"] == fail_name for e in ckpt.errors)


def test_resume_skips_already_indexed_files(repo_env, tmp_path: Path) -> None:
    """Re-invoking update_from_changes resumes from checkpoint, skipping completed files.

    Post-P17 semantics: the checkpoint is cleared at clean loop exit, so to
    exercise the resume path we simulate a process crash on the first run so
    the checkpoint survives into the second run.
    """
    repo_path, store, ctx = repo_env
    fail_name = "file_0499.py"

    class _Crash(Exception):
        pass

    def _simulate_crash(_repo_path):
        raise _Crash("simulated process crash before cleanup")

    # First run: crash after file 499 is recorded in checkpoint, before cleanup
    dispatcher1 = _CountingDispatcher(fail_on=fail_name)
    indexer1 = IncrementalIndexer(
        store=store,
        dispatcher=dispatcher1,
        repo_path=repo_path,
        ctx=ctx,
    )
    changes = _make_changes(repo_path)
    with patch(
        "mcp_server.indexing.incremental_indexer._clear_ckpt",
        side_effect=_simulate_crash,
    ):
        with pytest.raises(_Crash):
            indexer1.update_from_changes(changes)

    # First run: indexer continues past failure, so 999 files succeed (0-998 except 499)
    first_run_count = len(dispatcher1.indexed)
    assert first_run_count == 999, f"expected 999 indexed (1 failure), got {first_run_count}"

    # Second run: no failures — resume from checkpoint at file_0499
    dispatcher2 = _CountingDispatcher(fail_on=None)
    indexer2 = IncrementalIndexer(
        store=store,
        dispatcher=dispatcher2,
        repo_path=repo_path,
        ctx=ctx,
    )
    indexer2.update_from_changes(changes)

    # Should only have indexed from file_0499 onward (501 files: 499..999)
    second_run_count = len(dispatcher2.indexed)
    assert (
        second_run_count == 501
    ), f"expected 501 files on resume (499..999), got {second_run_count}"


def test_resume_wraps_lock_registry(repo_env) -> None:
    """update_from_changes acquires the per-repo lock from lock_registry."""
    repo_path, store, ctx = repo_env
    dispatcher = _CountingDispatcher()
    indexer = IncrementalIndexer(store=store, dispatcher=dispatcher, repo_path=repo_path, ctx=ctx)
    changes = _make_changes(repo_path, n=5)

    acquired_ids: list[str] = []
    original_acquire = lock_registry.acquire

    def spy_acquire(repo_id: str, **kwargs):
        acquired_ids.append(repo_id)
        return original_acquire(repo_id, **kwargs)

    with patch.object(lock_registry, "acquire", side_effect=spy_acquire):
        indexer.update_from_changes(changes)

    assert len(acquired_ids) >= 1, "lock_registry.acquire must be called at least once"
    repo_id = indexer._get_repository_id()
    assert (
        repo_id in acquired_ids
    ), f"acquire must be called with the repo's id ({repo_id!r}), got {acquired_ids}"


def test_checkpoint_cleared_on_clean_completion(repo_env) -> None:
    """After a clean run (no errors), the checkpoint file is removed."""
    repo_path, store, ctx = repo_env
    dispatcher = _CountingDispatcher()
    indexer = IncrementalIndexer(store=store, dispatcher=dispatcher, repo_path=repo_path, ctx=ctx)
    changes = _make_changes(repo_path, n=10)

    indexer.update_from_changes(changes)

    assert load(repo_path) is None, "checkpoint file must be cleared after clean run"
