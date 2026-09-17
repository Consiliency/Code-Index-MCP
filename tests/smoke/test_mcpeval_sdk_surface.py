"""MCPEVAL official SDK compatibility smoke."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    ClientRequest,
    CreateTaskResult,
    GetTaskPayloadRequest,
    GetTaskPayloadRequestParams,
    GetTaskRequest,
    GetTaskRequestParams,
    GetTaskResult,
    ListTasksRequest,
    ListTasksResult,
    TaskMetadata,
)

from tests.fixtures.multi_repo import boot_test_server, build_production_matrix
from tests.test_release_metadata import EXPECTED_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}


def _json_text_payload(result: CallToolResult) -> object:
    assert result.content, "expected text fallback content from MCP tool call"
    return json.loads(result.content[0].text)


async def _create_task(
    session: ClientSession,
    *,
    name: str,
    arguments: dict[str, object],
) -> CreateTaskResult:
    return await session.send_request(
        ClientRequest(
            CallToolRequest(
                params=CallToolRequestParams(
                    name=name,
                    arguments=arguments,
                    task=TaskMetadata(ttl=60_000),
                )
            )
        ),
        CreateTaskResult,
    )


async def _get_task(session: ClientSession, task_id: str) -> GetTaskResult:
    return await session.send_request(
        ClientRequest(GetTaskRequest(params=GetTaskRequestParams(taskId=task_id))),
        GetTaskResult,
    )


async def _get_task_payload(session: ClientSession, task_id: str) -> CallToolResult:
    return await session.send_request(
        ClientRequest(GetTaskPayloadRequest(params=GetTaskPayloadRequestParams(taskId=task_id))),
        CallToolResult,
    )


async def _wait_for_terminal_task(session: ClientSession, task_id: str) -> GetTaskResult:
    for _ in range(80):
        task = await _get_task(session, task_id)
        if task.status in TERMINAL_TASK_STATUSES:
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
            assert init.protocolVersion == "2025-11-25"
            assert init.capabilities is not None
            assert init.capabilities.tasks is not None
            assert init.capabilities.tasks.requests is not None
            assert init.capabilities.tasks.requests.tools is not None
            assert init.capabilities.tasks.requests.tools.call is not None

            tools = await session.list_tools()
            tool_map = {tool.name: tool for tool in tools.tools}

            assert tool_map["search_code"].title == "Search Code"
            assert tool_map["search_code"].outputSchema is not None
            assert tool_map["reindex"].execution is not None
            assert tool_map["reindex"].execution.taskSupport == "optional"

            status = await session.call_tool("get_status", {})
            assert status.isError is False
            assert status.structuredContent is not None
            assert status.structuredContent["version"] == EXPECTED_VERSION
            assert _json_text_payload(status) == status.structuredContent

            lookup = await session.call_tool(
                "symbol_lookup",
                {"repository": str(matrix.alpha.path), "symbol": matrix.alpha.symbol},
            )
            assert lookup.isError is False
            assert lookup.structuredContent is not None
            assert lookup.structuredContent["symbol"] == matrix.alpha.symbol
            assert lookup.structuredContent["defined_in"].endswith("alpha.py")
            assert _json_text_payload(lookup) == lookup.structuredContent

            ready_search = await session.call_tool(
                "search_code",
                {
                    "repository": str(matrix.alpha.path),
                    "query": matrix.alpha.token,
                    "semantic": False,
                },
            )
            assert ready_search.isError is False
            assert ready_search.structuredContent is not None
            assert ready_search.structuredContent["results"]
            assert any(
                result["file"].endswith("alpha.py")
                for result in ready_search.structuredContent["results"]
            )
            assert _json_text_payload(ready_search) == ready_search.structuredContent["results"]

            fail_closed = await session.call_tool(
                "search_code",
                {
                    "repository": str(linked.worktree_path),
                    "query": matrix.alpha.token,
                    "semantic": False,
                },
            )
            assert fail_closed.isError is True
            assert fail_closed.structuredContent is not None
            assert fail_closed.structuredContent["code"] == "index_unavailable"
            assert fail_closed.structuredContent["safe_fallback"] == "native_search"
            assert fail_closed.structuredContent["readiness"]["state"] == "unsupported_worktree"
            assert _json_text_payload(fail_closed) == fail_closed.structuredContent

            created = await _create_task(
                session,
                name="reindex",
                arguments={"path": str(matrix.alpha.path / "alpha.py")},
            )
            assert created.task.status in {"working", "completed"}
            task_id = created.task.taskId

            listed = await session.send_request(
                ClientRequest(ListTasksRequest()),
                ListTasksResult,
            )
            assert any(task.taskId == task_id for task in listed.tasks)

            terminal = await _wait_for_terminal_task(session, task_id)
            assert terminal.status == "completed"

            payload = await _get_task_payload(session, task_id)
            assert payload.isError is False
            assert payload.structuredContent is not None
            assert payload.structuredContent["mode"] == "file"
            assert payload.structuredContent["mutation_performed"] is True
            assert payload.structuredContent["indexed_files"] == 1
            assert _json_text_payload(payload) == payload.structuredContent
            await asyncio.sleep(0.2)
