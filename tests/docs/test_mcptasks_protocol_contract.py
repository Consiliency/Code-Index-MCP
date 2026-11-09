"""MCPTASKS protocol contract checks."""

from __future__ import annotations

import mcp.types as types

from mcp_server.cli.stdio_runner import _build_server, _build_tool_list, _initialization_options
from mcp_server.cli.task_runtime import TaskRuntime
from mcp_server.storage.mcp_task_registry import MCPTaskRegistry


def _tool_map():
    return {tool.name: tool for tool in _build_tool_list()}


def test_only_reindex_and_write_summaries_opt_into_task_augmented_execution():
    tools = _tool_map()
    assert tools["reindex"].execution is not None
    assert tools["reindex"].execution.task_support == "optional"
    assert tools["write_summaries"].execution is not None
    assert tools["write_summaries"].execution.task_support == "optional"
    for tool_name in (
        "symbol_lookup",
        "search_code",
        "get_status",
        "list_plugins",
        "summarize_sample",
        "handshake",
    ):
        assert tools[tool_name].execution is None


def test_server_capabilities_advertise_task_surface_when_enabled():
    # mcp 2.x removed the SDK tasks runtime; the repo-owned TaskRuntime keeps
    # the v1 capability and the tasks/* methods (Consiliency/Code-Index-MCP#124).
    server = _build_server(TaskRuntime(MCPTaskRegistry()))

    capabilities = _initialization_options(server).capabilities
    assert capabilities.tasks is not None
    assert capabilities.tasks.list is not None
    assert capabilities.tasks.cancel is not None
    assert capabilities.tasks.requests is not None
    assert capabilities.tasks.requests.tools is not None
    assert capabilities.tasks.requests.tools.call is not None
    for method in ("tasks/get", "tasks/result", "tasks/list", "tasks/cancel"):
        assert server.get_request_handler(method) is not None


def test_server_without_task_runtime_advertises_no_task_surface():
    server = _build_server(None)

    assert _initialization_options(server).capabilities.tasks is None
    assert server.get_request_handler("tasks/get") is None


def test_call_tool_request_params_expose_task_metadata_field():
    params = types.CallToolRequestParams(name="reindex", arguments={}, task=types.TaskMetadata())
    assert params.task is not None
