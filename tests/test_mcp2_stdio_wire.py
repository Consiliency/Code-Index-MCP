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
from mcp_server.storage.mcp_task_registry import MCPTaskRegistry, TaskCapacityError

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
                types.JSONRPCRequest(jsonrpc="2.0", id=self._next_id, method=method, params=params)
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
        await self._send.send(
            SessionMessage(types.JSONRPCNotification(jsonrpc="2.0", method=method))
        )


@asynccontextmanager
async def _connected(
    protocol_version: str | None = "2025-11-25",
    registry: MCPTaskRegistry | None = None,
) -> AsyncIterator[tuple[_Wire, dict[str, Any] | None]]:
    runtime = TaskRuntime(registry if registry is not None else MCPTaskRegistry())
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
        created = await wire.result("tools/call", {"name": "reindex", "arguments": {}, "task": {}})
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


MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "claude-code", "version": "2.1.321"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest.mark.asyncio
async def test_modern_revision_serves_the_same_tools_and_results(monkeypatch):
    """Clients such as Claude Code 2.1.x negotiate 2026-07-28 via server/discover.

    The tools and tool results are the handshake-era ones; the revision only
    drops the Tasks surface and adds its own result envelope fields.
    """
    seen_task_requests = []

    async def fake_call_tool(name, arguments, *, session=None, task_request=None):
        seen_task_requests.append(task_request.is_task)
        return stdio_runner._to_call_tool_result(
            [types.TextContent(type="text", text='{"results": [], "code": "index_unavailable"}')]
        )

    monkeypatch.setattr(stdio_runner, "call_tool", fake_call_tool)
    call = {"name": "search_code", "arguments": {"query": "x"}, "task": {"ttl": 1000}}

    async with _connected() as (wire, _):
        legacy_tools = (await wire.result("tools/list", {}))["tools"]
        legacy_call = await wire.result(
            "tools/call", {k: v for k, v in call.items() if k != "task"}
        )
    async with _connected(None) as (wire, _):
        discover = await wire.result("server/discover", {"_meta": MODERN_META})
        modern_tools = (await wire.result("tools/list", {"_meta": MODERN_META}))["tools"]
        modern_call = await wire.result("tools/call", {"_meta": MODERN_META, **call})
        modern_tasks = await wire.request("tasks/list", {"_meta": MODERN_META})

    assert "2026-07-28" in discover["supportedVersions"]
    assert "tasks" not in discover["capabilities"]
    assert modern_tools == [
        {key: value for key, value in tool.items() if key != "execution"} for tool in legacy_tools
    ]
    assert {key: modern_call[key] for key in legacy_call} == legacy_call
    assert set(modern_call) - set(legacy_call) == {"resultType", "_meta"}
    assert modern_call["resultType"] == "complete"
    assert modern_call["_meta"]["io.modelcontextprotocol/serverInfo"]["version"] == __version__
    # Task metadata is not part of the 2026-07-28 tools/call: the call runs synchronously.
    assert seen_task_requests == [False, False]
    assert modern_tasks["error"] == {
        "code": -32601,
        "message": "Method not found",
        "data": "tasks/list",
    }


# ---------------------------------------------------------------------------
# Consiliency/Code-Index-MCP#132 review fixes
# ---------------------------------------------------------------------------


def _ok_result():
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="{}")], structured_content={}
    )


def _blocking_task_call_tool(release):
    async def fake_call_tool(name, arguments, *, session=None, task_request=None):
        async def work(task):
            await release.wait()
            return _ok_result()

        return await task_request.run_task(work)

    return fake_call_tool


@pytest.mark.asyncio
async def test_concurrent_task_results_for_one_task_are_all_answered(monkeypatch):
    """S2: a second tasks/result used to orphan the first, which never replied."""
    release = anyio.Event()
    monkeypatch.setattr(stdio_runner, "call_tool", _blocking_task_call_tool(release))

    async with _connected() as (wire, _):
        created = await wire.result(
            "tools/call", {"name": "reindex", "arguments": {}, "task": {"ttl": 60_000}}
        )
        task_id = created["task"]["taskId"]
        for request_id in (101, 102, 103):
            await wire._send.send(
                SessionMessage(
                    types.JSONRPCRequest(
                        jsonrpc="2.0",
                        id=request_id,
                        method="tasks/result",
                        params={"taskId": task_id},
                    )
                )
            )
        await anyio.sleep(0.2)
        release.set()
        replies = {}
        with anyio.fail_after(5):
            while len(replies) < 3:
                message = await wire._receive.receive()
                data = message.message.model_dump(by_alias=True, mode="json", exclude_none=True)
                if data.get("id") in (101, 102, 103):
                    replies[data["id"]] = data

    assert sorted(replies) == [101, 102, 103]
    for reply in replies.values():
        assert reply["result"]["_meta"] == {RELATED_TASK_METADATA_KEY: {"taskId": task_id}}


@pytest.mark.asyncio
async def test_client_disconnect_mid_task_finishes_the_task_without_crashing(monkeypatch):
    release = anyio.Event()
    finished = anyio.Event()
    runtime = TaskRuntime(MCPTaskRegistry())
    server = stdio_runner._build_server(runtime)

    async def fake_call_tool(name, arguments, *, session=None, task_request=None):
        async def work(task):
            await release.wait()
            await task.update_status("after disconnect")
            finished.set()
            return _ok_result()

        return await task_request.run_task(work)

    monkeypatch.setattr(stdio_runner, "call_tool", fake_call_tool)
    served = anyio.Event()
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:

            async def serve():
                async with runtime.running():
                    await server.run(
                        server_streams[0],
                        server_streams[1],
                        stdio_runner._initialization_options(server),
                    )
                served.set()

            tg.start_soon(serve)
            wire = _Wire(client_streams[1], client_streams[0])
            await wire.result(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "x", "version": "0"},
                },
            )
            await wire.notify("notifications/initialized")
            created = await wire.result(
                "tools/call", {"name": "reindex", "arguments": {}, "task": {}}
            )
            await client_streams[1].aclose()
            await client_streams[0].aclose()
            await anyio.sleep(0.2)
            release.set()
            with anyio.fail_after(5):
                await served.wait()

    assert finished.is_set()
    task = await runtime.registry.get_task(created["task"]["taskId"])
    assert task is not None and task.status == "completed"


def test_startup_refuses_an_mcp_without_the_middleware_contract():
    """S1: Server.middleware is provisional in mcp 2.x; fail clearly, not per request."""
    from mcp_server.cli.task_runtime import TESTED_MCP_RANGE, ensure_middleware_contract

    ensure_middleware_contract(stdio_runner._build_server(None))

    class _NoMiddleware:
        middleware = None

    with pytest.raises(RuntimeError) as refused:
        ensure_middleware_contract(_NoMiddleware())
    assert TESTED_MCP_RANGE in str(refused.value)
    assert "Server.middleware is not a list" in str(refused.value)
    assert TESTED_MCP_RANGE == ">=2.2,<2.4"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task", "expected_ttl"),
    [
        ({}, 3_600_000),
        ({"ttl": 10**12}, 3_600_000),
        ({"ttl": 60_000}, 60_000),
        # #132 re-check R1: a one-minute floor.
        ({"ttl": 0}, 60_000),
        ({"ttl": 1}, 60_000),
    ],
)
async def test_task_ttl_is_clamped_and_defaulted(monkeypatch, task, expected_ttl):
    release = anyio.Event()
    release.set()
    monkeypatch.setattr(stdio_runner, "call_tool", _blocking_task_call_tool(release))
    async with _connected() as (wire, _):
        created = await wire.result(
            "tools/call", {"name": "reindex", "arguments": {}, "task": task}
        )
    assert created["task"]["ttl"] == expected_ttl


@pytest.mark.asyncio
async def test_task_retention_evicts_finished_tasks_and_refuses_when_all_run():
    # min_ttl_ms=0: this case is about eviction order, not the retention floor.
    registry = MCPTaskRegistry(max_tasks=2, min_ttl_ms=0)
    first = await registry.create_task(types.TaskMetadata())
    await registry.update_task(first.task_id, status="completed")
    second = await registry.create_task(types.TaskMetadata())
    third = await registry.create_task(types.TaskMetadata())

    assert await registry.get_task(first.task_id) is None
    assert {(await registry.get_task(t.task_id)).status for t in (second, third)} == {"working"}
    with pytest.raises(TaskCapacityError, match="retry after one finishes"):
        await registry.create_task(types.TaskMetadata())


@pytest.mark.asyncio
async def test_running_tasks_never_expire_and_deleting_wakes_waiters():
    registry = MCPTaskRegistry(max_ttl_ms=0)
    running = await registry.create_task(types.TaskMetadata(ttl=0))
    await anyio.sleep(0.01)
    assert await registry.get_task(running.task_id) is not None

    woke = anyio.Event()

    async def waiter():
        await registry.wait_for_update(running.task_id)
        woke.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(waiter)
        tg.start_soon(waiter)
        await anyio.sleep(0.05)
        assert await registry.delete_task(running.task_id)
        with anyio.fail_after(2):
            await woke.wait()

    finished = await registry.create_task(types.TaskMetadata())
    await registry.update_task(finished.task_id, status="completed")
    await anyio.sleep(0.01)
    assert await registry.get_task(finished.task_id) is None


@pytest.mark.asyncio
async def test_malformed_stdio_lines_get_json_rpc_errors_and_valid_lines_pass():
    import asyncio
    import json

    reader = asyncio.StreamReader()
    for line in (
        b"not json\n",
        b"\n",
        b'{"jsonrpc":"2.0","id":8}\n',
        # #132 re-check R2: malformed responses are dropped, never answered.
        b'{"jsonrpc":"2.0","id":11,"error":{"code":"x"}}\n',
        b'{"jsonrpc":"2.0","id":12,"result":1}\n',
        b'{"jsonrpc":"2.0","id":13,"error":{"message":"no code"}}\n',
        b'{"jsonrpc":"1.0","id":14,"result":{}}\n',
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n',
    ):
        reader.feed_data(line)
    reader.feed_eof()

    class _Output:
        def __init__(self):
            self.lines = []

        async def write(self, value):
            self.lines.append(json.loads(value))

    output = _Output()
    eof = []
    passed = [
        line async for line in stdio_runner._stdio_input(reader, lambda: eof.append(1), output)
    ]

    assert passed == ['{"jsonrpc":"2.0","id":1,"method":"ping"}\n']
    assert output.lines == [
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
        {"jsonrpc": "2.0", "id": 8, "error": {"code": -32600, "message": "Invalid Request"}},
    ]
    assert eof == [1]


@pytest.mark.parametrize(
    ("protocol_version", "expected"), [("2025-11-25", True), ("2026-07-28", False)]
)
def test_sampling_is_skipped_on_protocol_revisions_without_back_channel(
    monkeypatch, protocol_version, expected
):
    from types import SimpleNamespace

    from mcp_server.indexing import summarization

    monkeypatch.setattr(summarization, "commercial_egress_allowed", lambda: True)
    writer = object.__new__(summarization.ChunkWriter)
    writer.session = SimpleNamespace(
        protocol_version=protocol_version,
        client_params=SimpleNamespace(capabilities=SimpleNamespace(sampling=object())),
    )
    assert writer._has_sampling_capability() is expected


# ---------------------------------------------------------------------------
# Consiliency/Code-Index-MCP#132 re-check nits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ttl_zero_task_result_still_reaches_the_waiting_client(monkeypatch):
    """R1: ttl 0 used to expire the finished task before the woken waiter read it."""
    release = anyio.Event()
    monkeypatch.setattr(stdio_runner, "call_tool", _blocking_task_call_tool(release))
    async with _connected() as (wire, _):
        created = await wire.result(
            "tools/call", {"name": "reindex", "arguments": {}, "task": {"ttl": 0}}
        )
        task_id = created["task"]["taskId"]
        assert created["task"]["ttl"] == 60_000

        async def finish_later():
            await anyio.sleep(0.1)
            release.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(finish_later)
            payload = await wire.result("tasks/result", {"taskId": task_id})
        assert payload["_meta"] == {RELATED_TASK_METADATA_KEY: {"taskId": task_id}}
        assert payload["structuredContent"] == {}
        assert (await wire.result("tasks/get", {"taskId": task_id}))["status"] == "completed"


@pytest.mark.asyncio
async def test_finished_task_within_the_floor_survives_its_expiry():
    """R1: even an already-due expiry keeps a task that finished under a minute ago."""
    from datetime import datetime, timedelta, timezone

    registry = MCPTaskRegistry()
    task = await registry.create_task(types.TaskMetadata(ttl=0))
    await registry.store_result(task.task_id, _ok_result())
    await registry.update_task(task.task_id, status="completed")
    registry._tasks[task.task_id].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert (await registry.get_task(task.task_id)).status == "completed"
    assert await registry.get_result(task.task_id) is not None

    registry._tasks[task.task_id].finished_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    assert await registry.get_task(task.task_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("with_waiter", [True, False])
async def test_full_registry_keeps_a_just_finished_task_instead_of_evicting_it(with_waiter):
    """R1, 255 running plus one just-finished task: refuse the new task, keep the result.

    This is the reviewer's eviction-after-wake case: the finished task's waiter
    has been woken but not yet scheduled when another task is created.
    """
    registry = MCPTaskRegistry()  # 256 slots
    for _ in range(255):
        await registry.create_task(types.TaskMetadata())
    finished = await registry.create_task(types.TaskMetadata())
    await registry.store_result(finished.task_id, _ok_result())
    results = []

    async def waiter():
        while (await registry.get_task(finished.task_id)).status != "completed":
            await registry.wait_for_update(finished.task_id)
        results.append(await registry.get_result(finished.task_id))

    async with anyio.create_task_group() as tg:
        if with_waiter:
            tg.start_soon(waiter)
            await anyio.sleep(0.05)  # park the waiter
        # Completing wakes the waiter; creating a task before it runs must not evict it.
        await registry.update_task(finished.task_id, status="completed")
        with pytest.raises(TaskCapacityError, match="retry after one finishes"):
            await registry.create_task(types.TaskMetadata())

    assert (await registry.get_task(finished.task_id)).status == "completed"
    if with_waiter:
        assert results and results[0] is not None


@pytest.mark.asyncio
async def test_eviction_takes_older_finished_tasks_before_recent_ones():
    from datetime import datetime, timedelta, timezone

    registry = MCPTaskRegistry(max_tasks=3)
    old = await registry.create_task(types.TaskMetadata())
    await registry.update_task(old.task_id, status="completed")
    registry._tasks[old.task_id].finished_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    recent = await registry.create_task(types.TaskMetadata())
    await registry.update_task(recent.task_id, status="completed")
    await registry.create_task(types.TaskMetadata())  # running

    new = await registry.create_task(types.TaskMetadata())

    assert await registry.get_task(old.task_id) is None
    assert (await registry.get_task(recent.task_id)).status == "completed"
    assert (await registry.get_task(new.task_id)).status == "working"


@pytest.mark.asyncio
async def test_full_task_slots_return_the_actionable_message_through_call_tool(monkeypatch):
    """R4: the capacity refusal reached clients only as "details": "ValueError"."""
    release = anyio.Event()

    async def fake_handle_reindex(**kwargs):
        async def work(task):
            await release.wait()
            return _ok_result()

        return await kwargs["request_experimental"].run_task(work)

    monkeypatch.setattr(stdio_runner, "dispatcher", object())
    monkeypatch.setattr(stdio_runner.tool_handlers, "handle_reindex", fake_handle_reindex)
    call = {"name": "reindex", "arguments": {}, "task": {"ttl": 60_000}}
    async with _connected(registry=MCPTaskRegistry(max_tasks=1)) as (wire, _):
        first = await wire.result("tools/call", call)
        refused = await wire.result("tools/call", call)
        release.set()

    assert first["task"]["status"] == "working"
    assert refused["isError"] is True
    text = refused["content"][0]["text"]
    assert text == "Too many tasks (1 running or awaiting retrieval); retry after one finishes"
    assert "ValueError" not in text
