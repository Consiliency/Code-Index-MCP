"""Repo-local MCP task registry and TaskStore implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import anyio
from mcp.types import Result, Task, TaskMetadata, TaskStatus
from pydantic import TypeAdapter

# mcp 2.x removed the experimental Tasks runtime (``mcp.shared.experimental``)
# when SEP-1686 left the specification; the ``Task*`` wire types remain in
# ``mcp.types``. These helpers keep the v1 SDK semantics this registry relied on
# (Consiliency/Code-Index-MCP#124).
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
DEFAULT_POLL_INTERVAL_MS = 500


_DATETIME_ADAPTER: TypeAdapter[datetime] = TypeAdapter(datetime)


def wire_time(moment: datetime) -> str:
    """Serialize ``moment`` as the v1 SDK put task timestamps on the wire.

    mcp 2.x types ``Task.created_at``/``last_updated_at`` as ISO strings where
    v1 used ``datetime``; pydantic's JSON form keeps the wire value identical
    (for example ``2026-11-09T15:59:10.123456Z``).
    """
    return str(_DATETIME_ADAPTER.dump_python(moment, mode="json"))


def is_terminal(status: TaskStatus) -> bool:
    """Return whether ``status`` is terminal (completed, failed or cancelled)."""
    return status in TERMINAL_TASK_STATUSES


def create_task_state(metadata: TaskMetadata, task_id: str | None = None) -> Task:
    """Create a new task in ``working`` status, as the v1 SDK helper did."""
    now = datetime.now(timezone.utc)
    return Task(
        task_id=task_id or str(uuid4()),
        status="working",
        created_at=wire_time(now),
        last_updated_at=wire_time(now),
        ttl=metadata.ttl,
        poll_interval=DEFAULT_POLL_INTERVAL_MS,
    )


@dataclass
class TaskRecord:
    """Implementation-owned task bookkeeping beyond the public MCP Task fields."""

    task: Task
    metadata: TaskMetadata
    tool_name: str | None = None
    repository: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    cancellation_requested: bool = False
    progress: dict[str, Any] = field(default_factory=dict)
    terminal_result: Result | None = None
    terminal_error: str | None = None
    expires_at: datetime | None = None


class MCPTaskRegistry:
    """Single-process task store with extra repo-local bookkeeping."""

    def __init__(self, page_size: int = 50) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._page_size = page_size
        self._update_events: dict[str, anyio.Event] = {}

    def _calculate_expiry(self, ttl_ms: int | None) -> datetime | None:
        if ttl_ms is None:
            return None
        return datetime.now(timezone.utc) + timedelta(milliseconds=ttl_ms)

    def _cleanup_expired(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [
            task_id
            for task_id, record in self._tasks.items()
            if record.expires_at is not None and now >= record.expires_at
        ]
        for task_id in expired:
            del self._tasks[task_id]
            self._update_events.pop(task_id, None)

    def _copy_task(self, task: Task) -> Task:
        return Task(**task.model_dump())

    def _get_record(self, task_id: str) -> TaskRecord:
        record = self._tasks.get(task_id)
        if record is None:
            raise ValueError(f"Task with ID {task_id} not found")
        return record

    async def create_task(
        self,
        metadata: TaskMetadata,
        task_id: str | None = None,
    ) -> Task:
        self._cleanup_expired()
        task = create_task_state(metadata, task_id)
        if task.task_id in self._tasks:
            raise ValueError(f"Task with ID {task.task_id} already exists")

        now = datetime.now(timezone.utc)
        self._tasks[task.task_id] = TaskRecord(
            task=task,
            metadata=metadata,
            created_at=now,
            updated_at=now,
            expires_at=self._calculate_expiry(metadata.ttl),
        )
        return self._copy_task(task)

    async def get_task(self, task_id: str) -> Task | None:
        self._cleanup_expired()
        record = self._tasks.get(task_id)
        if record is None:
            return None
        return self._copy_task(record.task)

    async def update_task(
        self,
        task_id: str,
        status: TaskStatus | None = None,
        status_message: str | None = None,
    ) -> Task:
        record = self._get_record(task_id)
        if status is not None and status != record.task.status and is_terminal(record.task.status):
            raise ValueError(f"Cannot transition from terminal status '{record.task.status}'")

        status_changed = False
        if status is not None and record.task.status != status:
            record.task.status = status
            status_changed = True
        if status_message is not None:
            record.task.status_message = status_message

        record.updated_at = datetime.now(timezone.utc)
        record.task.last_updated_at = wire_time(record.updated_at)
        if status is not None and is_terminal(status) and record.task.ttl is not None:
            record.expires_at = self._calculate_expiry(record.task.ttl)
        if status == "failed":
            record.terminal_error = status_message
        if status_changed:
            await self.notify_update(task_id)
        return self._copy_task(record.task)

    async def store_result(self, task_id: str, result: Result) -> None:
        record = self._get_record(task_id)
        record.terminal_result = result
        record.updated_at = datetime.now(timezone.utc)

    async def get_result(self, task_id: str) -> Result | None:
        record = self._tasks.get(task_id)
        return None if record is None else record.terminal_result

    async def list_tasks(
        self,
        cursor: str | None = None,
    ) -> tuple[list[Task], str | None]:
        self._cleanup_expired()
        task_ids = list(self._tasks.keys())
        start_index = 0
        if cursor is not None:
            try:
                start_index = task_ids.index(cursor) + 1
            except ValueError as exc:
                raise ValueError(f"Invalid cursor: {cursor}") from exc
        page_ids = task_ids[start_index : start_index + self._page_size]
        tasks = [self._copy_task(self._tasks[task_id].task) for task_id in page_ids]
        next_cursor = None
        if start_index + self._page_size < len(task_ids) and page_ids:
            next_cursor = page_ids[-1]
        return tasks, next_cursor

    async def delete_task(self, task_id: str) -> bool:
        if task_id not in self._tasks:
            return False
        del self._tasks[task_id]
        self._update_events.pop(task_id, None)
        return True

    async def wait_for_update(self, task_id: str) -> None:
        if task_id not in self._tasks:
            raise ValueError(f"Task with ID {task_id} not found")
        self._update_events[task_id] = anyio.Event()
        await self._update_events[task_id].wait()

    async def notify_update(self, task_id: str) -> None:
        if task_id in self._update_events:
            self._update_events[task_id].set()

    async def bind_task(
        self,
        task_id: str,
        *,
        tool_name: str,
        repository: str | None,
    ) -> None:
        record = self._get_record(task_id)
        record.tool_name = tool_name
        record.repository = repository
        record.updated_at = datetime.now(timezone.utc)
        await self.notify_update(task_id)

    async def request_cancellation(
        self, task_id: str, *, status_message: str | None = None
    ) -> Task:
        record = self._get_record(task_id)
        if is_terminal(record.task.status):
            raise ValueError(f"Cannot cancel task in terminal state '{record.task.status}'")
        record.cancellation_requested = True
        if status_message is not None:
            record.task.status_message = status_message
        record.updated_at = datetime.now(timezone.utc)
        record.task.last_updated_at = wire_time(record.updated_at)
        await self.notify_update(task_id)
        return self._copy_task(record.task)

    async def is_cancellation_requested(self, task_id: str) -> bool:
        return self._get_record(task_id).cancellation_requested

    async def record_progress(
        self,
        task_id: str,
        *,
        status_message: str,
        progress: dict[str, Any],
    ) -> Task:
        record = self._get_record(task_id)
        record.progress = dict(progress)
        record.updated_at = datetime.now(timezone.utc)
        record.task.last_updated_at = wire_time(record.updated_at)
        record.task.status_message = status_message
        await self.notify_update(task_id)
        return self._copy_task(record.task)

    async def record_terminal_error(self, task_id: str, *, error: str) -> None:
        record = self._get_record(task_id)
        record.terminal_error = error
        record.updated_at = datetime.now(timezone.utc)
        await self.notify_update(task_id)

    async def get_record(self, task_id: str) -> TaskRecord:
        record = self._get_record(task_id)
        return TaskRecord(
            task=self._copy_task(record.task),
            metadata=record.metadata,
            tool_name=record.tool_name,
            repository=record.repository,
            created_at=record.created_at,
            updated_at=record.updated_at,
            cancellation_requested=record.cancellation_requested,
            progress=dict(record.progress),
            terminal_result=record.terminal_result,
            terminal_error=record.terminal_error,
            expires_at=record.expires_at,
        )

    def cleanup(self) -> None:
        self._tasks.clear()
        self._update_events.clear()
