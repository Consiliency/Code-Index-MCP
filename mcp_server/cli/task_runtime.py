"""Repo-owned MCP Tasks runtime for handshake-era STDIO connections.

The mcp 1.x SDK shipped an experimental Tasks runtime (SEP-1686):
``server.experimental.enable_tasks()``, ``ServerTaskContext`` and the default
``tasks/get``, ``tasks/result``, ``tasks/list`` and ``tasks/cancel`` handlers.
mcp 2.x removed that runtime when Tasks left the specification; only the
``Task*`` wire types remain in ``mcp.types``.

``reindex`` and ``write_summaries`` advertise ``execution.taskSupport =
"optional"`` and the README documents the task-augmented path, so this module
keeps the same wire behaviour on connections negotiated at a handshake-era
protocol version (2025-11-25 and earlier), where the protocol schema still
defines Tasks (Consiliency/Code-Index-MCP#124):

- the ``tasks`` server capability is advertised in the ``initialize`` result;
- a task-augmented ``tools/call`` returns ``CreateTaskResult`` and runs the work
  in a background task group that lives as long as the server loop;
- ``tasks/get``, ``tasks/result``, ``tasks/list`` and ``tasks/cancel`` behave as
  the v1 SDK defaults did, including error codes.

Connections negotiated at 2026-07-28 or later have no Tasks in their schema:
the SDK strips ``execution`` from ``tools/list`` there, task metadata on a
``tools/call`` is ignored (the call runs synchronously) and ``tasks/*`` answer
``-32601`` (method not found).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio
import anyio.abc
import mcp.types as types
from mcp.server import Server, ServerRequestContext
from mcp.server.context import CallNext, HandlerResult
from mcp.shared.exceptions import MCPError
from mcp.types.methods import serialize_server_result
from mcp.types.version import HANDSHAKE_PROTOCOL_VERSIONS

from mcp_server.storage.mcp_task_registry import MCPTaskRegistry, is_terminal

logger = logging.getLogger(__name__)

MODEL_IMMEDIATE_RESPONSE_KEY = "io.modelcontextprotocol/model-immediate-response"
RELATED_TASK_METADATA_KEY = "io.modelcontextprotocol/related-task"

TaskWork = Callable[["TaskContext"], Awaitable[types.Result]]


def supports_tasks(protocol_version: str | None) -> bool:
    """Return whether ``protocol_version`` is a handshake era that defines Tasks."""
    return protocol_version in HANDSHAKE_PROTOCOL_VERSIONS


def tasks_capability() -> types.ServerTasksCapability:
    """The ``tasks`` capability the v1 SDK advertised once tasks were enabled."""
    return types.ServerTasksCapability(
        list=types.TasksListCapability(),
        cancel=types.TasksCancelCapability(),
        requests=types.ServerTasksRequestsCapability(
            tools=types.TasksToolsCapability(call=types.TasksCallCapability())
        ),
    )


def _tool_result_payload(result: types.Result, protocol_version: str) -> dict[str, Any]:
    """Wire form of a stored ``CallToolResult`` for the negotiated revision.

    A custom method's result skips the SDK's per-revision sieve, so apply the
    ``tools/call`` surface here; otherwise 2026-era fields such as
    ``resultType`` would leak onto a handshake-era wire.
    """
    dumped = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    try:
        return serialize_server_result("tools/call", protocol_version, dumped)
    except Exception:
        dumped.pop("resultType", None)
        return dumped


class TaskContext:
    """What task work sees; the subset of the v1 ``ServerTaskContext`` it used."""

    def __init__(self, task: types.Task, registry: MCPTaskRegistry, session: Any = None) -> None:
        self._task = task
        self._registry = registry
        self._session = session
        self._cancelled = False

    @property
    def task_id(self) -> str:
        return self._task.task_id

    @property
    def task(self) -> types.Task:
        return self._task

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def request_cancellation(self) -> None:
        self._cancelled = True

    async def update_status(self, message: str, *, notify: bool = True) -> None:
        self._task = await self._registry.update_task(self.task_id, status_message=message)
        if notify:
            await self._send_notification()

    async def complete(self, result: types.Result) -> None:
        await self._registry.store_result(self.task_id, result)
        self._task = await self._registry.update_task(self.task_id, status="completed")
        await self._send_notification()

    async def fail(self, error: str) -> None:
        self._task = await self._registry.update_task(
            self.task_id, status="failed", status_message=error
        )
        await self._send_notification()

    async def refresh(self) -> types.Task:
        """Re-read the task, which work may have finalized through the registry."""
        current = await self._registry.get_task(self.task_id)
        if current is not None:
            self._task = current
        return self._task

    async def _send_notification(self) -> None:
        if self._session is None:
            return
        task = self._task
        try:
            await self._session.send_notification(
                types.TaskStatusNotification(
                    params=types.TaskStatusNotificationParams(
                        task_id=task.task_id,
                        status=task.status,
                        status_message=task.status_message,
                        created_at=task.created_at,
                        last_updated_at=task.last_updated_at,
                        ttl=task.ttl,
                        poll_interval=task.poll_interval,
                    )
                )
            )
        except Exception as exc:  # status notifications are advisory
            logger.debug("task status notification not sent (%s)", type(exc).__name__)


class TaskCreated(Exception):
    """Carries a ``CreateTaskResult`` past the SDK's ``tools/call`` result check.

    The vendored 2025-11-25 result surface only accepts ``CallToolResult`` for
    ``tools/call``, although that protocol revision allows ``CreateTaskResult``.
    ``TaskRuntime.middleware`` turns this into the wire result.
    """

    def __init__(self, result: types.CreateTaskResult) -> None:
        super().__init__("task created")
        self.result = result


class TaskRequest:
    """Task view of one ``tools/call``; the duck type of the v1 ``Experimental``."""

    def __init__(
        self,
        metadata: types.TaskMetadata | None,
        runtime: TaskRuntime | None,
        session: Any = None,
    ) -> None:
        self.task_metadata = metadata
        self._runtime = runtime
        self._session = session

    @property
    def is_task(self) -> bool:
        return self.task_metadata is not None

    def validate_for_tool(self, tool: types.Tool) -> None:
        """Reject a call whose task mode the tool does not allow (-32601)."""
        mode = (tool.execution.task_support if tool.execution else None) or "forbidden"
        if mode == "required" and not self.is_task:
            raise MCPError(types.METHOD_NOT_FOUND, "This tool requires task-augmented invocation")
        if mode == "forbidden" and self.is_task:
            raise MCPError(
                types.METHOD_NOT_FOUND, "This tool does not support task-augmented invocation"
            )

    async def run_task(
        self,
        work: TaskWork,
        *,
        task_id: str | None = None,
        model_immediate_response: str | None = None,
    ) -> types.CreateTaskResult:
        if self._runtime is None:
            raise RuntimeError("Task support not enabled.")
        if self.task_metadata is None:
            raise RuntimeError("Request is not task-augmented (no task field in params).")
        return await self._runtime.run_task(
            self.task_metadata,
            work,
            session=self._session,
            task_id=task_id,
            model_immediate_response=model_immediate_response,
        )


class TaskRuntime:
    """Background task group plus the ``tasks/*`` handlers for one server."""

    def __init__(self, registry: MCPTaskRegistry) -> None:
        self.registry = registry
        self._task_group: anyio.abc.TaskGroup | None = None

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """Own the task group background work runs in.

        As in the v1 SDK, leaving the context waits for in-flight task work
        rather than cancelling it: mutating work runs in worker threads that are
        never abandoned mid-write.
        """
        async with anyio.create_task_group() as tg:
            self._task_group = tg
            try:
                yield
            finally:
                self._task_group = None

    def request(self, ctx: ServerRequestContext[Any], params: types.CallToolRequestParams) -> TaskRequest:
        metadata = params.task if supports_tasks(ctx.protocol_version) else None
        return TaskRequest(metadata, self, ctx.session)

    async def run_task(
        self,
        metadata: types.TaskMetadata,
        work: TaskWork,
        *,
        session: Any = None,
        task_id: str | None = None,
        model_immediate_response: str | None = None,
    ) -> types.CreateTaskResult:
        if self._task_group is None:
            raise RuntimeError("Task runtime is not running.")
        task = await self.registry.create_task(metadata, task_id)
        task_ctx = TaskContext(task, self.registry, session)

        async def execute() -> None:
            try:
                result = await work(task_ctx)
                if not is_terminal((await task_ctx.refresh()).status):
                    await task_ctx.complete(result)
            except Exception as exc:
                try:
                    if not is_terminal((await task_ctx.refresh()).status):
                        await task_ctx.fail(str(exc))
                except Exception:
                    logger.exception("could not record failure for task %s", task_ctx.task_id)

        self._task_group.start_soon(execute)
        meta = (
            {MODEL_IMMEDIATE_RESPONSE_KEY: model_immediate_response}
            if model_immediate_response is not None
            else None
        )
        return types.CreateTaskResult(task=task, _meta=meta)

    def install(self, server: Server[Any]) -> None:
        """Register the ``tasks/*`` handlers and the task-result middleware."""
        server.add_request_handler("tasks/get", types.GetTaskRequestParams, self._get_task)
        server.add_request_handler(
            "tasks/result", types.GetTaskPayloadRequestParams, self._get_task_result
        )
        server.add_request_handler("tasks/list", types.PaginatedRequestParams, self._list_tasks)
        server.add_request_handler("tasks/cancel", types.CancelTaskRequestParams, self._cancel_task)
        server.middleware.append(self.middleware)

    async def middleware(self, ctx: ServerRequestContext[Any], call_next: CallNext) -> HandlerResult:
        try:
            return await call_next(ctx)
        except TaskCreated as created:
            return created.result.model_dump(by_alias=True, mode="json", exclude_none=True)

    @staticmethod
    def _require_tasks(ctx: ServerRequestContext[Any]) -> None:
        if not supports_tasks(ctx.protocol_version):
            raise MCPError(types.METHOD_NOT_FOUND, "Method not found", ctx.method)

    async def _require_task(self, task_id: str) -> types.Task:
        task = await self.registry.get_task(task_id)
        if task is None:
            raise MCPError(types.INVALID_PARAMS, f"Task not found: {task_id}")
        return task

    async def _get_task(
        self, ctx: ServerRequestContext[Any], params: types.GetTaskRequestParams
    ) -> types.GetTaskResult:
        self._require_tasks(ctx)
        task = await self._require_task(params.task_id)
        return types.GetTaskResult(**task.model_dump())

    async def _get_task_result(
        self, ctx: ServerRequestContext[Any], params: types.GetTaskPayloadRequestParams
    ) -> dict[str, Any]:
        """Block until the task is terminal, then return its stored payload.

        Returned as a wire dict: mcp 2.x models drop unknown fields, and the
        payload is a full ``CallToolResult`` carried by ``GetTaskPayloadResult``.
        """
        self._require_tasks(ctx)
        task_id = params.task_id
        related = {
            RELATED_TASK_METADATA_KEY: types.RelatedTaskMetadata(task_id=task_id).model_dump(
                by_alias=True, mode="json", exclude_none=True
            )
        }
        while True:
            task = await self._require_task(task_id)
            if is_terminal(task.status):
                result = await self.registry.get_result(task_id)
                if result is None:
                    return {"_meta": related}
                payload = _tool_result_payload(result, ctx.protocol_version)
                payload["_meta"] = {**(payload.get("_meta") or {}), **related}
                return payload
            await self.registry.wait_for_update(task_id)

    async def _list_tasks(
        self, ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams
    ) -> types.ListTasksResult:
        self._require_tasks(ctx)
        tasks, next_cursor = await self.registry.list_tasks(params.cursor)
        return types.ListTasksResult(tasks=tasks, next_cursor=next_cursor)

    async def _cancel_task(
        self, ctx: ServerRequestContext[Any], params: types.CancelTaskRequestParams
    ) -> types.CancelTaskResult:
        """Request cooperative cancellation; work stops at its next safe point."""
        self._require_tasks(ctx)
        task = await self.registry.request_cancellation(
            params.task_id,
            status_message="Cancellation requested; waiting for a safe stop point.",
        )
        return types.CancelTaskResult(**task.model_dump())
