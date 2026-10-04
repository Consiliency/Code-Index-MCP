"""MCPAUTH regression tests for the local STDIO handshake boundary."""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import mcp.types as types
import pytest

from mcp_server.cli import stdio_runner
from mcp_server.cli.handshake import HandshakeGate


def _payload(result: types.CallToolResult) -> Any:
    assert result.content
    assert isinstance(result.content[0], types.TextContent)
    return json.loads(result.content[0].text)


@pytest.fixture
def restore_stdio_globals():
    saved = {
        "_gate": stdio_runner._gate,
        "_lazy_summarizer": stdio_runner._lazy_summarizer,
        "_init_lock": stdio_runner._init_lock,
        "sqlite_store": stdio_runner.sqlite_store,
        "dispatcher": stdio_runner.dispatcher,
        "initialization_error": stdio_runner.initialization_error,
    }
    yield
    stdio_runner._gate = saved["_gate"]
    stdio_runner._lazy_summarizer = saved["_lazy_summarizer"]
    stdio_runner._init_lock = saved["_init_lock"]
    stdio_runner.sqlite_store = saved["sqlite_store"]
    stdio_runner.dispatcher = saved["dispatcher"]
    stdio_runner.initialization_error = saved["initialization_error"]


@pytest.mark.asyncio
async def test_handshake_tool_logs_redacted_secret(caplog, restore_stdio_globals):
    stdio_runner._gate = HandshakeGate(secret="top-secret")
    stdio_runner._lazy_summarizer = object()
    stdio_runner.sqlite_store = object()
    stdio_runner.initialization_error = None

    with caplog.at_level(logging.INFO, logger="mcp_server.cli.stdio_runner"):
        result = await stdio_runner.call_tool("handshake", {"secret": "top-secret"})

    assert _payload(result) == {"authenticated": True}
    tool_logs = [
        record.getMessage() for record in caplog.records if "MCP Tool Call" in record.getMessage()
    ]
    assert tool_logs
    assert any("[REDACTED]" in message for message in tool_logs)
    assert all("top-secret" not in message for message in tool_logs)


@pytest.mark.asyncio
async def test_non_handshake_tool_logs_metadata_without_arguments(
    monkeypatch, caplog, restore_stdio_globals
):
    stdio_runner._gate = HandshakeGate(secret="gate-secret")
    assert stdio_runner._gate.verify("gate-secret") is True
    stdio_runner._lazy_summarizer = object()
    stdio_runner.sqlite_store = object()
    stdio_runner.initialization_error = None

    async def fake_search_code(**kwargs):
        return [
            types.TextContent(
                type="text",
                text=stdio_runner.tool_handlers._ensure_response({"results": []}),
            )
        ]

    monkeypatch.setattr(stdio_runner.tool_handlers, "handle_search_code", fake_search_code)

    with caplog.at_level(logging.INFO, logger="mcp_server.cli.stdio_runner"):
        result = await stdio_runner.call_tool("search_code", {"query": "demo"})

    assert _payload(result) == {"results": []}
    tool_logs = [
        record.getMessage() for record in caplog.records if "MCP tool call" in record.getMessage()
    ]
    assert tool_logs
    assert any("search_code" in message for message in tool_logs)
    assert "demo" not in caplog.text


@pytest.mark.asyncio
async def test_handshake_gate_precedes_all_lazy_initialization(
    monkeypatch, restore_stdio_globals
) -> None:
    monkeypatch.setenv("MCP_CLIENT_SECRET", "pre-init-secret")
    initialize_services = AsyncMock()
    monkeypatch.setattr(stdio_runner, "initialize_services", initialize_services)
    stdio_runner._gate = None
    stdio_runner._lazy_summarizer = None
    stdio_runner._init_lock = None
    stdio_runner.sqlite_store = None
    stdio_runner.dispatcher = None
    stdio_runner.initialization_error = None

    blocked = await stdio_runner.call_tool("search_code", {"query": "demo"})
    assert _payload(blocked)["code"] == "handshake_required"
    initialize_services.assert_not_awaited()

    authenticated = await stdio_runner.call_tool("handshake", {"secret": "pre-init-secret"})
    assert _payload(authenticated) == {"authenticated": True}
    initialize_services.assert_not_awaited()


@pytest.mark.asyncio
async def test_stateless_dispatcher_skips_legacy_lazy_initialization(
    monkeypatch, restore_stdio_globals
) -> None:
    initialize_services = AsyncMock()
    serving_dispatcher = object()
    monkeypatch.setattr(stdio_runner, "initialize_services", initialize_services)
    monkeypatch.setattr(
        stdio_runner.tool_handlers,
        "handle_get_status",
        AsyncMock(return_value=[types.TextContent(type="text", text='{"status":"ok"}')]),
    )
    stdio_runner._gate = HandshakeGate(secret=None)
    stdio_runner.dispatcher = serving_dispatcher
    stdio_runner.sqlite_store = None
    stdio_runner.initialization_error = None

    result = await stdio_runner.call_tool("get_status", {})

    assert _payload(result) == {"status": "ok"}
    initialize_services.assert_not_awaited()
    assert stdio_runner.dispatcher is serving_dispatcher
