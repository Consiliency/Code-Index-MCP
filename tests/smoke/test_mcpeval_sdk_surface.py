"""MCPEVAL official SDK compatibility smoke."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import mcp.types as types
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult

from tests.fixtures.multi_repo import boot_test_server, build_production_matrix
from tests.test_release_metadata import EXPECTED_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}


def _json_text_payload(result: CallToolResult) -> object:
    assert result.content, "expected text fallback content from MCP tool call"
    return json.loads(result.content[0].text)


class _RawStdioJsonRpc:
    """Newline-delimited JSON-RPC over the server's STDIO.

    The mcp 2.x Python SDK removed its Tasks client (Tasks left the MCP
    specification), and its ``ClientSession`` validates every ``tools/call``
    result as ``CallToolResult``. A handshake-era task client therefore speaks
    the 2025-11-25 wire directly (Consiliency/Code-Index-MCP#124).
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._next_id = 0

    async def request(self, method: str, params: dict[str, object] | None = None) -> dict:
        self._next_id += 1
        message: dict[str, object] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        await self.send(message)
        while True:
            line = await asyncio.wait_for(self._process.stdout.readline(), 60)
            assert line, f"server closed STDIO before answering {method}"
            reply = json.loads(line)
            if reply.get("id") == self._next_id:
                assert "error" not in reply, reply
                return reply["result"]

    async def send(self, message: dict[str, object]) -> None:
        self._process.stdin.write((json.dumps(message) + "\n").encode())
        await self._process.stdin.drain()


async def _wait_for_terminal_task(client: _RawStdioJsonRpc, task_id: str) -> dict:
    for _ in range(80):
        task = await client.request("tasks/get", {"taskId": task_id})
        if task["status"] in TERMINAL_TASK_STATUSES:
            return task
        await asyncio.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach a terminal status")


@pytest.mark.asyncio
async def test_official_sdk_proves_mcpeval_metadata_structured_fail_closed_and_task_surfaces(
    tmp_path: Path,
) -> None:
    matrix = build_production_matrix(tmp_path)
    linked = matrix.linked_worktree()
    registry_path = tmp_path / "registry.json"

    with boot_test_server(
        tmp_path,
        matrix.repos,
        extra_roots=[linked.worktree_path],
    ):
        pass

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["MCP_ALLOWED_ROOTS"] = os.pathsep.join(
        [
            str(matrix.alpha.path.resolve()),
            str(matrix.beta.path.resolve()),
            str(linked.worktree_path.resolve()),
        ]
    )
    env["MCP_REPO_REGISTRY"] = str(registry_path)
    env["SEMANTIC_SEARCH_ENABLED"] = "false"

    params = StdioServerParameters(
        command=str(RUNNER_PYTHON),
        args=["-m", "mcp_server.cli.stdio_runner"],
        cwd=str(matrix.alpha.path),
        env=env,
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            assert init.protocol_version == "2025-11-25"
            assert init.capabilities is not None
            assert init.capabilities.tasks is not None
            assert init.capabilities.tasks.requests is not None
            assert init.capabilities.tasks.requests.tools is not None
            assert init.capabilities.tasks.requests.tools.call is not None

            tools = await session.list_tools()
            tool_map = {tool.name: tool for tool in tools.tools}

            assert tool_map["search_code"].title == "Search Code"
            assert tool_map["search_code"].output_schema is not None
            assert tool_map["reindex"].execution is not None
            assert tool_map["reindex"].execution.task_support == "optional"

            status = await session.call_tool("get_status", {})
            assert status.is_error is False
            assert status.structured_content is not None
            assert status.structured_content["version"] == EXPECTED_VERSION
            assert _json_text_payload(status) == status.structured_content

            lookup = await session.call_tool(
                "symbol_lookup",
                {"repository": str(matrix.alpha.path), "symbol": matrix.alpha.symbol},
            )
            assert lookup.is_error is False
            assert lookup.structured_content is not None
            assert lookup.structured_content["symbol"] == matrix.alpha.symbol
            assert lookup.structured_content["defined_in"].endswith("alpha.py")
            assert _json_text_payload(lookup) == lookup.structured_content

            ready_search = await session.call_tool(
                "search_code",
                {
                    "repository": str(matrix.alpha.path),
                    "query": matrix.alpha.token,
                    "semantic": False,
                },
            )
            assert ready_search.is_error is False
            assert ready_search.structured_content is not None
            assert ready_search.structured_content["results"]
            assert any(
                result["file"].endswith("alpha.py")
                for result in ready_search.structured_content["results"]
            )
            assert _json_text_payload(ready_search) == ready_search.structured_content["results"]

            fail_closed = await session.call_tool(
                "search_code",
                {
                    "repository": str(linked.worktree_path),
                    "query": matrix.alpha.token,
                    "semantic": False,
                },
            )
            assert fail_closed.is_error is True
            assert fail_closed.structured_content is not None
            assert fail_closed.structured_content["code"] == "index_unavailable"
            assert fail_closed.structured_content["safe_fallback"] == "native_search"
            assert fail_closed.structured_content["readiness"]["state"] == "unsupported_worktree"
            assert _json_text_payload(fail_closed) == fail_closed.structured_content

            listed = await session.send_request(types.ListTasksRequest(), types.ListTasksResult)
            assert listed.tasks == []

    process = await asyncio.create_subprocess_exec(
        params.command,
        *params.args,
        cwd=params.cwd,
        env=params.env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=16 * 1024 * 1024,
    )
    try:
        client = _RawStdioJsonRpc(process)
        init = await client.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "mcpeval-task-client", "version": "0"},
            },
        )
        assert init["capabilities"]["tasks"]["requests"]["tools"]["call"] == {}
        await client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        created = await client.request(
            "tools/call",
            {
                "name": "reindex",
                "arguments": {"path": str(matrix.alpha.path / "alpha.py")},
                "task": {"ttl": 60_000},
            },
        )
        assert created["task"]["status"] in {"working", "completed"}
        task_id = created["task"]["taskId"]

        listed = await client.request("tasks/list", {})
        assert any(task["taskId"] == task_id for task in listed["tasks"])

        terminal = await _wait_for_terminal_task(client, task_id)
        assert terminal["status"] == "completed"

        raw_payload = await client.request("tasks/result", {"taskId": task_id})
        assert raw_payload["_meta"]["io.modelcontextprotocol/related-task"] == {"taskId": task_id}
        payload = CallToolResult.model_validate(raw_payload)
        assert payload.is_error is False
        assert payload.structured_content is not None
        assert payload.structured_content["mode"] == "file"
        assert payload.structured_content["mutation_performed"] is True
        assert payload.structured_content["indexed_files"] == 1
        assert _json_text_payload(payload) == payload.structured_content
        await asyncio.sleep(0.2)
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 30)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
