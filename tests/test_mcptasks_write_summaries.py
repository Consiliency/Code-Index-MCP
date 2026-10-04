"""Task-backed write_summaries tests."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import TaskMetadata

from mcp_server.cli.task_write_summaries import run_write_summaries_task
from mcp_server.indexing.summarization import SummaryGenerationResult
from mcp_server.storage.mcp_task_registry import MCPTaskRegistry


class _FakeTask:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self._cancelled = False
        self.status_messages: list[str] = []

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def request_cancellation(self) -> None:
        self._cancelled = True

    async def update_status(self, message: str) -> None:
        self.status_messages.append(message)


@pytest.mark.asyncio
async def test_run_write_summaries_task_returns_sync_shaped_payload() -> None:
    registry = MCPTaskRegistry()
    task_state = await registry.create_task(TaskMetadata())
    task = _FakeTask(task_state.taskId)
    active_store = SimpleNamespace(db_path="/tmp/code_index.db")

    with (
        patch("mcp_server.cli.task_write_summaries.Settings.from_environment") as settings_factory,
        patch("mcp_server.cli.task_write_summaries.ComprehensiveChunkWriter") as writer_cls,
    ):
        settings_factory.return_value = SimpleNamespace(
            semantic_default_profile="legacy-default",
            get_semantic_default_profile=lambda: "oss_high",
            get_profile_summarization_config=lambda _profile: {},
        )
        writer = MagicMock()
        writer.process_scope = AsyncMock(
            return_value=SummaryGenerationResult(
                chunks_attempted=3,
                summaries_written=3,
                remaining_chunks=0,
            )
        )
        writer_cls.return_value = writer

        result = await run_write_summaries_task(
            task=task,
            registry=registry,
            active_store=active_store,
            current_session=None,
            client_name=None,
            limit_arg=10,
            model_used="gpt-test",
        )
        assert writer_cls.call_args.kwargs["summarization_config"]["profile_id"] == "oss_high"

    payload = result.structuredContent
    assert payload["chunks_summarized"] == 3
    assert payload["summary_chunks_attempted"] == 3
    assert payload["summary_missing_chunks"] == 0
    assert payload["persisted"] is True


@pytest.mark.asyncio
async def test_run_write_summaries_task_records_cancelled_terminal_result() -> None:
    registry = MCPTaskRegistry()
    task_state = await registry.create_task(TaskMetadata())
    await registry.request_cancellation(task_state.taskId)
    task = _FakeTask(task_state.taskId)
    active_store = SimpleNamespace(db_path="/tmp/code_index.db")

    with (
        patch("mcp_server.cli.task_write_summaries.Settings.from_environment") as settings_factory,
        patch("mcp_server.cli.task_write_summaries.ComprehensiveChunkWriter") as writer_cls,
    ):
        settings_factory.return_value = SimpleNamespace(
            semantic_default_profile="oss_high",
            get_semantic_default_profile=lambda: "oss_high",
            get_profile_summarization_config=lambda _profile: {},
        )
        writer = MagicMock()
        writer.process_scope = AsyncMock(
            return_value=replace(
                SummaryGenerationResult(
                    chunks_attempted=1, summaries_written=0, remaining_chunks=5
                ),
                cancelled=True,
            )
        )
        writer_cls.return_value = writer

        result = await run_write_summaries_task(
            task=task,
            registry=registry,
            active_store=active_store,
            current_session=None,
            client_name=None,
            limit_arg=10,
            model_used="gpt-test",
        )

    payload = result.structuredContent
    assert payload["cancelled"] is True
    record = await registry.get_record(task_state.taskId)
    assert record.task.status == "cancelled"
