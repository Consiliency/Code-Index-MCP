"""Task-backed write_summaries execution helpers."""

from __future__ import annotations

import json
from typing import Any, Callable

import anyio
import mcp.types as types

from mcp_server.cli.task_runtime import TaskContext
from mcp_server.config.settings import Settings
from mcp_server.indexing.summarization import ComprehensiveChunkWriter
from mcp_server.storage.mcp_task_registry import MCPTaskRegistry

REBUILD_IN_PROGRESS = "rebuild_in_progress"
WRITER_BUSY = "writer_busy"

_REFUSALS: dict[str, tuple[str, str, str]] = {
    REBUILD_IN_PROGRESS: (
        "Rebuild in progress",
        "A staged index rebuild owns this repository's published generation; "
        "summaries written to it now would be lost when the rebuild publishes.",
        "Retry after the staged rebuild publishes (already summarized chunks are skipped).",
    ),
    WRITER_BUSY: (
        "Index writer busy",
        "Another index writer (a sync, or `mcp-index repository summarize`) holds the "
        "repository writer lock. No rebuild is running; summaries that could not be "
        "stored were not written.",
        "Retry shortly (already summarized chunks are skipped).",
    ),
}


def summary_write_refusal(
    code: str,
    tool: str,
    *,
    readiness: dict[str, Any] | None = None,
    committed: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    """The shared envelope for a summary write that could not be stored (#145).

    ``code`` is ``rebuild_in_progress`` (the generation is being replaced) or
    ``writer_busy`` (another writer holds the lock; retry shortly).
    ``committed`` is the number of summaries this request itself stored before
    the refusal (not those it generated, which another writer may have stored
    meanwhile); it alone decides ``mutation_performed``. Committed work always
    precedes any rebuild's snapshot, so it is kept. ``extra`` carries the
    tool's partial counters.
    """
    error, message, remediation = _REFUSALS[code]
    written = int(committed)
    payload: dict[str, Any] = {
        "error": error,
        "code": code,
        "tool": tool,
        "mutation_performed": written > 0,
        "summaries_committed": written,
        "retryable": True,
        "readiness": readiness,
        "message": message,
        "remediation": remediation,
        **extra,
    }
    if written and "note" not in payload:
        payload["note"] = (
            f"{written} summaries were committed before the write was refused and are "
            "kept; the rest were not written."
        )
    return payload


def _call_tool_result(payload: dict[str, Any], *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2))],
        structured_content=payload,
        is_error=is_error,
    )


async def run_write_summaries_task(
    *,
    task: TaskContext,
    registry: MCPTaskRegistry,
    active_store: Any,
    current_session: Any,
    client_name: str | None,
    limit_arg: int,
    model_used: str | None,
    write_blocked: Callable[[], bool] | None = None,
    write_fence: Callable[[], Any] | None = None,
) -> types.CallToolResult:
    if active_store is None:
        raise RuntimeError("Task-backed write_summaries requires an initialized sqlite store")

    await registry.bind_task(
        task.task_id,
        tool_name="write_summaries",
        repository=str(getattr(active_store, "db_path", "unknown")),
    )

    settings = Settings.from_environment()
    profile_id = settings.get_semantic_default_profile()
    writer = ComprehensiveChunkWriter(
        db_path=active_store.db_path,
        qdrant_client=None,
        session=current_session,
        client_name=client_name,
        summarization_config={
            **settings.get_profile_summarization_config(profile_id),
            "profile_id": profile_id,
        },
        write_fence=write_fence,
    )

    total_written = 0
    total_committed = 0
    total_attempted = 0
    total_missing = 0
    remaining_budget = limit_arg
    persisted_any = False
    write_fenced = False
    write_busy = False
    per_pass_limit = min(64, limit_arg)

    async def cancel_observer() -> None:
        while not task.is_cancelled:
            if await registry.is_cancellation_requested(task.task_id):
                task.request_cancellation()
                return
            await anyio.sleep(0.1)

    async with anyio.create_task_group() as tg:
        tg.start_soon(cancel_observer)
        while remaining_budget > 0 and not task.is_cancelled:
            await task.update_status(
                f"Summarizing chunks in bounded passes ({total_written}/{limit_arg} complete)..."
            )
            result = await writer.process_scope(
                limit=min(per_pass_limit, remaining_budget),
                max_batches=1,
                # A staged rebuild that starts mid-task stops it too (#145).
                cancel_check=lambda: task.is_cancelled
                or (write_blocked is not None and write_blocked()),
            )
            write_fenced = write_fenced or bool(getattr(result, "write_fenced", False))
            write_busy = write_busy or bool(getattr(result, "write_busy", False))
            total_written += result.summaries_written
            total_committed += int(getattr(result, "summaries_committed", 0) or 0)
            total_attempted += result.chunks_attempted
            total_missing = result.remaining_chunks
            persisted_any = persisted_any or result.summaries_written > 0
            remaining_budget = max(0, remaining_budget - result.summaries_written)
            await registry.record_progress(
                task.task_id,
                status_message=(
                    f"Summaries written: {total_written}; attempted: {total_attempted}; "
                    f"remaining chunks: {total_missing}"
                ),
                progress={
                    "chunks_summarized": total_written,
                    "summary_chunks_attempted": total_attempted,
                    "summary_missing_chunks": total_missing,
                    "model_used": model_used,
                    "persisted": persisted_any,
                },
            )
            if result.cancelled or task.is_cancelled or write_fenced or write_busy:
                break
            if result.summaries_written == 0 or result.remaining_chunks == 0:
                break
        tg.cancel_scope.cancel()

    payload = {
        "chunks_summarized": total_written,
        "limit": limit_arg,
        "model_used": model_used,
        "persisted": persisted_any,
        "semantic_vectors_written": False,
        "summary_chunks_attempted": total_attempted,
        "summary_missing_chunks": total_missing,
    }
    refusal = (
        REBUILD_IN_PROGRESS
        if write_fenced or (write_blocked is not None and write_blocked())
        else WRITER_BUSY if write_busy else None
    )
    if refusal is not None and not task.is_cancelled:
        # Same envelope as the synchronous tool, with the task's counters (#145).
        counters = {key: value for key, value in payload.items() if key != "persisted"}
        counters["chunks_summarized"] = total_committed
        return _call_tool_result(
            summary_write_refusal(refusal, "write_summaries", committed=total_committed, **counters)
        )
    if task.is_cancelled:
        payload["cancelled"] = True
        result = _call_tool_result(payload)
        await registry.store_result(task.task_id, result)
        await registry.update_task(
            task.task_id,
            status="cancelled",
            status_message="Cancelled before the next bounded summarization pass.",
        )
        return result
    return _call_tool_result(payload)
