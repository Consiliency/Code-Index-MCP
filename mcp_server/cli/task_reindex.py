"""Task-backed reindex execution helpers."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
from mcp.server.experimental.task_context import ServerTaskContext

from mcp_server.core.repo_resolver import RepositoryMutationCancelled, run_repository_mutation
from mcp_server.dispatcher.dispatcher_enhanced import IndexResult, IndexResultStatus
from mcp_server.indexing.checkpoint import ReindexCheckpoint
from mcp_server.indexing.checkpoint import clear as clear_checkpoint
from mcp_server.indexing.checkpoint import save
from mcp_server.storage.mcp_task_registry import MCPTaskRegistry


def _call_tool_result(payload: dict[str, Any], *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2))],
        structuredContent=payload,
        isError=is_error,
    )


def _file_mutation_count(mutation: Any) -> int:
    """Accept completed file mutations, including truthful no-op results."""
    if not isinstance(mutation, IndexResult):
        raise RuntimeError("File indexing did not return a completion result")
    semantic = mutation.semantic or {}
    if semantic.get("semantic_failed") or semantic.get("semantic_blocked"):
        raise RuntimeError("Required semantic mutation did not complete")
    if mutation.status == IndexResultStatus.SKIPPED_UNCHANGED:
        return 0
    if mutation.status != IndexResultStatus.INDEXED:
        raise RuntimeError("File indexing did not complete")
    return 1


def _record_reindexed_files(active_store: Any, workspace_root: Path, target_path: Path) -> int:
    """Count dispatcher-persisted rows without importing working-tree files."""
    if active_store is None:
        return 0
    try:
        target_path.resolve().relative_to(workspace_root.resolve())
    except ValueError:
        return 0
    relative = active_store.path_resolver.normalize_path(target_path)
    with active_store._get_connection() as connection:
        if relative == ".":
            return connection.execute(
                "SELECT COUNT(*) FROM files WHERE COALESCE(is_deleted, 0) = 0"
            ).fetchone()[0]
        prefix = relative.rstrip("/") + "/"
        return connection.execute(
            "SELECT COUNT(*) FROM files WHERE COALESCE(is_deleted, 0) = 0 "
            "AND (relative_path = ? OR substr(relative_path, 1, ?) = ?)",
            (relative, len(prefix), prefix),
        ).fetchone()[0]


def _candidate_checkpoint_paths(target_path: Path, workspace_root: Path) -> list[str]:
    workspace_root = workspace_root.expanduser().resolve(strict=True)
    if target_path.is_file():
        try:
            return [
                target_path.expanduser().resolve(strict=True).relative_to(workspace_root).as_posix()
            ]
        except (OSError, ValueError):
            return []
    candidates: list[str] = []
    for path in target_path.rglob("*"):
        if not path.is_file() or ".git" in path.parts or ".mcp-index" in path.parts:
            continue
        try:
            candidates.append(
                path.expanduser().resolve(strict=True).relative_to(workspace_root).as_posix()
            )
        except (OSError, ValueError):
            continue
    return candidates


def _save_checkpoint(
    *,
    ctx: Any,
    pending_paths: list[str],
    last_completed_path: str,
) -> None:
    save(
        ReindexCheckpoint(
            repo_id=ctx.repo_id,
            started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            last_completed_path=last_completed_path,
            remaining_paths=pending_paths,
        ),
        ctx.workspace_root,
    )


async def run_reindex_task(
    *,
    task: ServerTaskContext,
    registry: MCPTaskRegistry,
    dispatcher: Any,
    ctx: Any,
    active_store: Any,
    target_path: Path,
    requested_path: str | None,
    repo_resolver: Any = None,
) -> types.CallToolResult:
    repository_scope = str(ctx.workspace_root) if ctx is not None else str(target_path)
    await registry.bind_task(
        task.task_id,
        tool_name="reindex",
        repository=repository_scope,
    )

    if ctx is None:
        raise RuntimeError("Task-backed reindex requires a repository context")

    workspace_root = ctx.workspace_root.expanduser().resolve(strict=True)
    target_path = target_path.expanduser().resolve(strict=True)
    try:
        target_path.relative_to(workspace_root)
    except ValueError:
        payload = {
            "error": "Reindex path is outside the selected repository",
            "code": "path_outside_selected_repository",
            "repository_id": ctx.repo_id,
            "path": str(target_path),
            "mutation_performed": False,
        }
        result = _call_tool_result(payload, is_error=True)
        await registry.store_result(task.task_id, result)
        await registry.update_task(
            task.task_id,
            status="failed",
            status_message="Reindex path is outside the selected repository.",
        )
        return result

    pending_paths = _candidate_checkpoint_paths(target_path, ctx.workspace_root)
    last_completed_path = ""
    _save_checkpoint(ctx=ctx, pending_paths=pending_paths, last_completed_path=last_completed_path)

    loop = asyncio.get_running_loop()

    async def cancel_observer() -> None:
        while not task.is_cancelled:
            if await registry.is_cancellation_requested(task.task_id):
                task.request_cancellation()
                return
            await anyio.sleep(0.1)

    def publish_progress(progress: dict[str, Any]) -> None:
        nonlocal last_completed_path, pending_paths

        progress_path = progress.get("last_progress_path")
        if progress_path:
            progress_file = Path(progress_path)
            try:
                last_completed_path = progress_file.relative_to(ctx.workspace_root).as_posix()
            except ValueError:
                last_completed_path = progress_file.name
            if last_completed_path in pending_paths:
                pending_paths = pending_paths[pending_paths.index(last_completed_path) + 1 :]
            _save_checkpoint(
                ctx=ctx,
                pending_paths=pending_paths,
                last_completed_path=last_completed_path,
            )

        status_message = f"Reindex {progress.get('stage', 'working')}" + (
            f": {progress_path}" if progress_path else ""
        )
        asyncio.run_coroutine_threadsafe(
            registry.record_progress(
                task.task_id,
                status_message=status_message,
                progress=progress,
            ),
            loop,
        )

    def do_work(current) -> dict[str, Any]:
        if task.is_cancelled:
            return {"cancelled": True, "indexed_files": 0, "mutation_performed": False}
        if requested_path and target_path.is_file():
            mutation = dispatcher.index_file(current, target_path)
            indexed_files = _file_mutation_count(mutation)
            durable_files = _record_reindexed_files(
                current.sqlite_store, ctx.workspace_root, target_path
            )
            return {
                "path": str(target_path),
                "mode": "file",
                "indexed_files": indexed_files,
                "durable_files": durable_files,
                "mutation_performed": bool(indexed_files),
                "cancelled": task.is_cancelled,
                "message": (
                    f"Reindexed file: {requested_path}" if indexed_files else "File unchanged"
                ),
                "error": getattr(mutation, "error", None),
            }

        outcome = dispatcher.index_directory(
            current,
            target_path,
            recursive=True,
            progress_callback=publish_progress,
            cancel_check=lambda: task.is_cancelled,
        )
        if not outcome.get("cancelled") and not task.is_cancelled:
            outcome["durable_files"] = _record_reindexed_files(
                current.sqlite_store, ctx.workspace_root, target_path
            )
            outcome["lexical_rows"] = current.sqlite_store.rebuild_fts_code()
        outcome["cancelled"] = bool(outcome.get("cancelled") or task.is_cancelled)
        outcome["mutation_performed"] = not getattr(current, "staging", False)
        return outcome

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(cancel_observer)
            # Do not abandon a mutating worker when its request scope is cancelled.
            try:
                outcome = await anyio.to_thread.run_sync(
                    run_repository_mutation, repo_resolver, ctx, do_work, abandon_on_cancel=False
                )
            except RepositoryMutationCancelled as exc:
                outcome = exc.outcome
            tg.cancel_scope.cancel()
    except Exception:
        if ctx is not None:
            _save_checkpoint(
                ctx=ctx,
                pending_paths=pending_paths,
                last_completed_path=last_completed_path,
            )
        raise

    if outcome.get("cancelled") or task.is_cancelled:
        mutation_performed = bool(outcome.get("mutation_performed"))
        if mutation_performed:
            clear_checkpoint(ctx.workspace_root)
        payload = {
            "path": str(target_path),
            "mode": "file" if requested_path and target_path.is_file() else "merge",
            "mutation_performed": mutation_performed,
            "indexed_files": outcome.get("indexed_files"),
            "ignored_files": outcome.get("ignored_files"),
            "failed_files": outcome.get("failed_files"),
            "total_files": outcome.get("total_files"),
            "by_language": outcome.get("by_language"),
            "lexical_rows": None,
            "durable_files": None,
            "semantic_indexed": outcome.get("semantic_indexed"),
            "semantic_failed": outcome.get("semantic_failed"),
            "semantic_skipped": outcome.get("semantic_skipped"),
            "semantic_blocked": outcome.get("semantic_blocked"),
            "semantic_stage": outcome.get("semantic_stage"),
            "summaries_written": outcome.get("summaries_written"),
            "summary_chunks_attempted": outcome.get("summary_chunks_attempted"),
            "summary_missing_chunks": outcome.get("summary_missing_chunks"),
            "total_embedding_units": outcome.get("total_embedding_units"),
            "semantic_error": outcome.get("semantic_error"),
            "semantic_blocker": outcome.get("semantic_blocker"),
            "semantic_paths_queued": outcome.get("semantic_paths_queued"),
            "semantic_indexer_present": outcome.get("semantic_indexer_present"),
            "merge_note": (
                "Cancellation observed after a mutation; published changes were not rolled back."
                if mutation_performed
                else "Reindex cancelled without publishing a generation."
            ),
            "cancelled": True,
        }
        result = _call_tool_result(payload)
        await registry.store_result(task.task_id, result)
        await registry.update_task(
            task.task_id,
            status="cancelled",
            status_message=payload["merge_note"],
        )
        return result

    clear_checkpoint(ctx.workspace_root)
    if requested_path and target_path.is_file():
        return _call_tool_result(outcome)
    durable_files = outcome["durable_files"]
    lexical_rows = outcome["lexical_rows"]
    return _call_tool_result(
        {
            "path": str(target_path),
            "mode": "merge",
            "mutation_performed": True,
            "indexed_files": outcome.get("indexed_files"),
            "ignored_files": outcome.get("ignored_files"),
            "failed_files": outcome.get("failed_files"),
            "total_files": outcome.get("total_files"),
            "by_language": outcome.get("by_language"),
            "lexical_rows": lexical_rows,
            "durable_files": durable_files,
            "semantic_indexed": outcome.get("semantic_indexed"),
            "semantic_failed": outcome.get("semantic_failed"),
            "semantic_skipped": outcome.get("semantic_skipped"),
            "semantic_blocked": outcome.get("semantic_blocked"),
            "semantic_stage": outcome.get("semantic_stage"),
            "summaries_written": outcome.get("summaries_written"),
            "summary_chunks_attempted": outcome.get("summary_chunks_attempted"),
            "summary_missing_chunks": outcome.get("summary_missing_chunks"),
            "total_embedding_units": outcome.get("total_embedding_units"),
            "semantic_error": outcome.get("semantic_error"),
            "semantic_blocker": outcome.get("semantic_blocker"),
            "semantic_paths_queued": outcome.get("semantic_paths_queued"),
            "semantic_indexer_present": outcome.get("semantic_indexer_present"),
            "merge_note": (
                "Changed/new files updated; deleted files are not purged — "
                "FileWatcher handles those on next change, or reindex again after deletions."
            ),
        }
    )
