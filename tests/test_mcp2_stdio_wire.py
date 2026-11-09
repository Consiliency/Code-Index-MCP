"""Wire contract of the STDIO server on the mcp 2.x SDK.

Drives the real lowlevel server over in-memory JSON-RPC streams with raw
messages, so these checks see exactly what a client receives. They pin the
behaviour the mcp 1.x SDK gave clients and that the 2.x port keeps
(Consiliency/Code-Index-MCP#124), plus ``serverInfo.version``
(Consiliency/Code-Index-MCP#128).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Any

import anyio
import mcp.types as types
import pytest
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage

from mcp_server import __version__
from mcp_server.cli import stdio_runner
from mcp_server.cli.task_runtime import (
    MODEL_IMMEDIATE_RESPONSE_KEY,
    RELATED_TASK_METADATA_KEY,
    TaskRuntime,
    supports_tasks,
)
from mcp_server.storage.mcp_task_registry import MCPTaskRegistry

HANDSHAKE_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")


class _Wire:
    def __init__(self, send: Any, receive: Any) -> None:
        self._send = send
        self._receive = receive
        self._next_id = 0

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        await self._send.send(
            SessionMessage(
                types.JSONRPCRequest(
                    jsonrpc="2.0", id=self._next_id, method=method, params=params
                )
            )
        )
        with anyio.fail_after(10):
            while True:
                message = await self._receive.receive()
                data = message.message.model_dump(by_alias=True, mode="json", exclude_none=True)
                if data.get("id") == self._next_id:
                    return data

    async def result(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        reply = await self.request(method, params)
        assert "error" not in reply, reply
        return reply["result"]

    async def notify(self, method: str) -> None:
        await self._send.send(SessionMessage(types.JSONRPCNotification(jsonrpc="2.0", method=method)))


@asynccontextmanager
async def _connected(
    protocol_version: str | None = "2025-11-25",
) -> AsyncIterator[tuple[_Wire, dict[str, Any] | None]]:
    runtime = TaskRuntime(MCPTaskRegistry())
    server = stdio_runner._build_server(runtime)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as tg:

            async def serve() -> None:
                async with runtime.running():
                    await server.run(
                        server_read, server_write, stdio_runner._initialization_options(server)
                    )

            tg.start_soon(serve)
            wire = _Wire(client_write, client_read)
            init = None
            if protocol_version is not None:
                init = await wire.result(
                    "initialize",
                    {
                        "protocolVersion": protocol_version,
                        "capabilities": {},
                        "clientInfo": {"name": "wire-test", "version": "0"},
                    },
                )
                await wire.notify("notifications/initialized")
            try:
                yield wire, init
            finally:
                tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_initialize_reports_package_version_not_mcp_library_version():
    async with _connected() as (_, init):
        assert init["serverInfo"]["name"] == "code-index-mcp-fast-search"
        assert init["serverInfo"]["version"] == __version__
        assert init["serverInfo"]["version"] != version("mcp")


@pytest.mark.asyncio
async def test_initialize_advertises_the_v1_capability_set():
    async with _connected() as (_, init):
        assert init["capabilities"] == {
            "experimental": {},
            "tools": {"listChanged": False},
            "tasks": {"list": {}, "cancel": {}, "requests": {"tools": {"call": {}}}},
        }
        assert init["instructions"] == stdio_runner._SERVER_INSTRUCTIONS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offered", "negotiated"),
    [*((v, v) for v in HANDSHAKE_VERSIONS), ("2099-01-01", "2025-11-25")],
)
async def test_initialize_negotiates_handshake_protocol_versions(offered, negotiated):
    async with _connected(offered) as (wire, init):
        assert init["protocolVersion"] == negotiated
        assert supports_tasks(negotiated)
        tools = await wire.result("tools/list", {})
        assert len(tools["tools"]) == len(stdio_runner._build_tool_list())


@pytest.mark.asyncio
async def test_tools_list_is_protocol_valid_and_keeps_task_support():
    async with _connected() as (wire, _):
        tools = {tool["name"]: tool for tool in (await wire.result("tools/list", {}))["tools"]}
    assert set(tools) == {tool.name for tool in stdio_runner._build_tool_list()}
    for name, tool in tools.items():
        assert tool["inputSchema"]["type"] == "object"
        assert tool["outputSchema"]["type"] == "object"
        assert all(branch["type"] == "object" for branch in tool["outputSchema"]["oneOf"])
        expected = {"taskSupport": "optional"} if name in {"reindex", "write_summaries"} else None
        assert tool.get("execution") == expected


@pytest.mark.asyncio
async def test_invalid_arguments_return_the_v1_input_validation_error_result():
    async with _connected() as (wire, _):
        result = await wire.result("tools/call", {"name": "search_code", "arguments": {}})
    assert result == {
        "content": [
            {"type": "text", "text": "Input validation error: 'query' is a required property"}
        ],
        "isError": True,
    }


@pytest.mark.asyncio
async def test_handler_exceptions_stay_tool_error_results(monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(stdio_runner, "call_tool", explode)
    async with _connected() as (wire, _):
        result = await wire.result("tools/call", {"name": "get_status", "arguments": {}})
    assert result == {
        "content": [{"type": "text", "text": "synthetic failure"}],
        "isError": True,
    }


@pytest.mark.asyncio
async def test_task_mode_on_a_non_task_tool_is_a_tool_error(monkeypatch):
    monkeypatch.setattr(stdio_runner, "dispatcher", object())
    async with _connected() as (wire, _):
        result = await wire.result(
            "tools/call",
            {"name": "search_code", "arguments": {"query": "x"}, "task": {"ttl": 1000}},
        )
    assert result == {
        "content": [
            {"type": "text", "text": "This tool does not support task-augmented invocation"}
        ],
        "isError": True,
    }


@pytest.mark.asyncio
async def test_task_lifecycle_matches_the_v1_sdk_runtime(monkeypatch):
    release = anyio.Event()

    async def fake_call_tool(name, arguments, *, session=None, task_request=None):
        assert task_request is not None and task_request.is_task

        async def work(task):
            await task.update_status("halfway")
            await release.wait()
            return types.CallToolResult(
                content=[types.TextContent(type="text", text='{"ok": true}')],
                structured_content={"ok": True},
            )

        return await task_request.run_task(work, model_immediate_response="queued")

    monkeypatch.setattr(stdio_runner, "call_tool", fake_call_tool)
    async with _connected() as (wire, _):
        created = await wire.result(
            "tools/call", {"name": "reindex", "arguments": {}, "task": {"ttl": 60_000}}
        )
        task_id = created["task"]["taskId"]
        assert created["task"]["status"] == "working"
        assert created["task"]["ttl"] == 60_000
        assert created["task"]["pollInterval"] == 500
        assert created["task"]["createdAt"].endswith("Z")
        assert created["_meta"] == {MODEL_IMMEDIATE_RESPONSE_KEY: "queued"}

        listed = await wire.result("tasks/list", {})
        assert [task["taskId"] for task in listed["tasks"]] == [task_id]

        cancelled = await wire.result("tasks/cancel", {"taskId": task_id})
        assert cancelled["taskId"] == task_id
        assert cancelled["statusMessage"] == (
            "Cancellation requested; waiting for a safe stop point."
        )

        release.set()
        payload = await wire.result("tasks/result", {"taskId": task_id})
        assert payload == {
            "_meta": {RELATED_TASK_METADATA_KEY: {"taskId": task_id}},
            "content": [{"type": "text", "text": '{"ok": true}'}],
            "isError": False,
            "structuredContent": {"ok": True},
        }
        assert (await wire.result("tasks/get", {"taskId": task_id}))["status"] == "completed"

        terminal_cancel = await wire.request("tasks/cancel", {"taskId": task_id})
        assert terminal_cancel["error"]["code"] == 0
        assert "terminal state" in terminal_cancel["error"]["message"]

        missing = await wire.request("tasks/get", {"taskId": "missing"})
        assert missing["error"] == {"code": -32602, "message": "Task not found: missing"}


@pytest.mark.asyncio
async def test_failed_task_work_marks_the_task_failed(monkeypatch):
    async def fake_call_tool(name, arguments, *, session=None, task_request=None):
        async def work(task):
            raise RuntimeError("work failed")

        return await task_request.run_task(work)

    monkeypatch.setattr(stdio_runner, "call_tool", fake_call_tool)
    async with _connected() as (wire, _):
        created = await wire.result(
            "tools/call", {"name": "reindex", "arguments": {}, "task": {}}
        )
        task_id = created["task"]["taskId"]
        payload = await wire.result("tasks/result", {"taskId": task_id})
        assert payload == {"_meta": {RELATED_TASK_METADATA_KEY: {"taskId": task_id}}}
        task = await wire.result("tasks/get", {"taskId": task_id})
    assert task["status"] == "failed"
    assert task["statusMessage"] == "work failed"


def test_modern_protocol_revisions_have_no_task_surface():
    runtime = TaskRuntime(MCPTaskRegistry())
    server = stdio_runner._build_server(runtime)

    assert not supports_tasks("2026-07-28")
    assert server.get_capabilities(protocol_version="2026-07-28").tasks is None

    class _Ctx:
        protocol_version = "2026-07-28"
        session = None

    params = types.CallToolRequestParams(name="reindex", arguments={}, task=types.TaskMetadata())
    assert runtime.request(_Ctx(), params).is_task is False
