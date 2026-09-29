"""Task-backed reindex tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mcp.types import TaskMetadata

from mcp_server.cli.task_reindex import _record_reindexed_files, run_reindex_task
from mcp_server.storage.mcp_task_registry import MCPTaskRegistry


class _FakeTask:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self._cancelled = False

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def request_cancellation(self) -> None:
        self._cancelled = True


class _FakeDispatcher:
    def __init__(self, *, cancel_on_start: bool = False) -> None:
        self.cancel_on_start = cancel_on_start

    def index_directory(
        self, ctx, target_path, recursive=True, progress_callback=None, cancel_check=None
    ):
        files = sorted(path for path in target_path.rglob("*.py"))
        if self.cancel_on_start and cancel_check is not None and cancel_check():
            return {
                "cancelled": True,
                "indexed_files": 0,
                "ignored_files": 0,
                "failed_files": 0,
                "total_files": len(files),
                "by_language": {"python": len(files)},
                "semantic_indexed": 0,
                "semantic_failed": 0,
                "semantic_skipped": 0,
                "semantic_blocked": 0,
                "semantic_stage": "cancelled",
                "summaries_written": 0,
                "summary_chunks_attempted": 0,
                "summary_missing_chunks": 0,
                "total_embedding_units": 0,
                "semantic_error": None,
                "semantic_blocker": None,
                "semantic_paths_queued": 0,
                "semantic_indexer_present": False,
            }
        for file_path in files:
            if progress_callback is not None:
                progress_callback(
                    {
                        "stage": "lexical_walking",
                        "last_progress_path": str(file_path),
                        "semantic_stage": "not_run",
                    }
                )
        return {
            "indexed_files": len(files),
            "ignored_files": 0,
            "failed_files": 0,
            "total_files": len(files),
            "by_language": {"python": len(files)},
            "semantic_indexed": 0,
            "semantic_failed": 0,
            "semantic_skipped": 0,
            "semantic_blocked": 0,
            "semantic_stage": "skipped",
            "summaries_written": 0,
            "summary_chunks_attempted": 0,
            "summary_missing_chunks": 0,
            "total_embedding_units": 0,
            "semantic_error": None,
            "semantic_blocker": None,
            "semantic_paths_queued": len(files),
            "semantic_indexer_present": False,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "semantic_failed", "semantic_blocked", "unchanged"])
async def test_file_task_preserves_failure_and_unchanged_result(tmp_path, failure):
    from mcp_server.dispatcher.dispatcher_enhanced import IndexResult, IndexResultStatus

    target = tmp_path / "example.py"
    target.write_text("value = 1\n")
    registry = MCPTaskRegistry()
    state = await registry.create_task(TaskMetadata())
    task = _FakeTask(state.taskId)
    store = MagicMock()
    store.path_resolver.normalize_path.return_value = "example.py"
    store._get_connection.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (
        1,
    )
    ctx = SimpleNamespace(repo_id="repo-1", workspace_root=tmp_path, sqlite_store=store)
    status = (
        IndexResultStatus.SKIPPED_UNCHANGED
        if failure == "unchanged"
        else (IndexResultStatus.ERROR if failure == "error" else IndexResultStatus.INDEXED)
    )
    dispatcher = MagicMock()
    dispatcher.index_file.return_value = IndexResult(
        status,
        target,
        None,
        None,
        semantic={failure: 1} if failure.startswith("semantic_") else None,
    )
    arguments = dict(
        task=task,
        registry=registry,
        dispatcher=dispatcher,
        ctx=ctx,
        active_store=store,
        target_path=target,
        requested_path=str(target),
    )
    if failure == "unchanged":
        result = await run_reindex_task(**arguments)
        assert result.structuredContent["indexed_files"] == 0
        assert result.structuredContent["mutation_performed"] is False
    else:
        with pytest.raises(Exception):
            await run_reindex_task(**arguments)
        store._get_connection.assert_not_called()
        assert (tmp_path / ".reindex-state").exists()


@pytest.mark.asyncio
async def test_run_reindex_task_returns_merge_payload_and_clears_checkpoint(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("print('a')\n", encoding="utf-8")
    (repo / "b.py").write_text("print('b')\n", encoding="utf-8")

    registry = MCPTaskRegistry()
    task_state = await registry.create_task(TaskMetadata())
    task = _FakeTask(task_state.taskId)
    active_store = MagicMock()
    active_store.ensure_repository_row.return_value = "repo-row"
    active_store.rebuild_fts_code.return_value = 2
    active_store.path_resolver.normalize_path.return_value = "."
    active_store._get_connection.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (
        2,
    )
    ctx = SimpleNamespace(repo_id="repo-1", workspace_root=repo, sqlite_store=active_store)

    result = await run_reindex_task(
        task=task,
        registry=registry,
        dispatcher=_FakeDispatcher(),
        ctx=ctx,
        active_store=active_store,
        target_path=repo,
        requested_path=None,
    )

    payload = result.structuredContent
    assert payload["mode"] == "merge"
    assert payload["mutation_performed"] is True
    assert payload["indexed_files"] == 2
    assert payload["durable_files"] == 2
    assert not (repo / ".reindex-state").exists()


@pytest.mark.asyncio
async def test_run_reindex_task_marks_terminal_payload_cancelled(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("print('a')\n", encoding="utf-8")

    registry = MCPTaskRegistry()
    task_state = await registry.create_task(TaskMetadata())
    await registry.request_cancellation(task_state.taskId)
    task = _FakeTask(task_state.taskId)
    active_store = MagicMock()
    active_store.ensure_repository_row.return_value = "repo-row"
    ctx = SimpleNamespace(repo_id="repo-1", workspace_root=repo)

    result = await run_reindex_task(
        task=task,
        registry=registry,
        dispatcher=_FakeDispatcher(cancel_on_start=True),
        ctx=ctx,
        active_store=active_store,
        target_path=repo,
        requested_path=None,
    )

    payload = result.structuredContent
    assert payload["cancelled"] is True
    record = await registry.get_record(task_state.taskId)
    assert record.task.status == "cancelled"
    assert record.terminal_result is not None


@pytest.mark.asyncio
async def test_run_reindex_task_rejects_path_outside_selected_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside.py"
    repo.mkdir()
    outside.write_text("pass\n", encoding="utf-8")

    registry = MCPTaskRegistry()
    task_state = await registry.create_task(TaskMetadata())
    task = _FakeTask(task_state.taskId)
    dispatcher = MagicMock()
    active_store = MagicMock()
    ctx = SimpleNamespace(repo_id="repo-1", workspace_root=repo)

    result = await run_reindex_task(
        task=task,
        registry=registry,
        dispatcher=dispatcher,
        ctx=ctx,
        active_store=active_store,
        target_path=outside,
        requested_path=str(outside),
    )

    assert result.isError is True
    assert result.structuredContent["code"] == "path_outside_selected_repository"
    assert result.structuredContent["mutation_performed"] is False
    dispatcher.index_file.assert_not_called()
    dispatcher.index_directory.assert_not_called()
    active_store.store_file.assert_not_called()
    record = await registry.get_record(task_state.taskId)
    assert record.task.status == "failed"


def test_record_reindexed_files_skips_foreign_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside.py"
    repo.mkdir()
    outside.write_text("pass\n", encoding="utf-8")
    link = repo / "linked.py"
    link.symlink_to(outside)
    active_store = MagicMock()
    active_store.ensure_repository_row.return_value = "repo-row"

    recorded = _record_reindexed_files(active_store, repo, link)

    assert recorded == 0
    active_store.store_file.assert_not_called()
