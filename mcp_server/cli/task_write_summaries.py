"""Task-backed write_summaries execution helpers."""

from __future__ import annotations

import json
from typing import Any

import anyio
import mcp.types as types
from mcp.server.experimental.task_context import ServerTaskContext

from mcp_server.config.settings import Settings
from mcp_server.indexing.summarization import ComprehensiveChunkWriter
from mcp_server.storage.mcp_task_registry import MCPTaskRegistry


def _call_tool_result(payload: dict[str, Any], *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2))],
        structuredContent=payload,
        isError=is_error,
    )


async def run_write_summaries_task(
    *,
    task: ServerTaskContext,
    registry: MCPTaskRegistry,
    active_store: Any,
    current_session: Any,
    client_name: str | None,
    limit_arg: int,
    model_used: str | None,
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
    )

    total_written = 0
    total_attempted = 0
    total_missing = 0
    remaining_budget = limit_arg
    persisted_any = False
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
                cancel_check=lambda: task.is_cancelled,
            )
            total_written += result.summaries_written
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
            if result.cancelled or task.is_cancelled:
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
