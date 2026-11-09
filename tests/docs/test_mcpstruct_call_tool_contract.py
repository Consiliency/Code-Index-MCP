"""MCPSTRUCT call_tool contract tests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import mcp.types as types

from mcp_server.cli import stdio_runner


def _run(coro):
    return asyncio.run(coro)


def _configure_runner(monkeypatch) -> None:
    monkeypatch.setattr(stdio_runner, "dispatcher", MagicMock())
    monkeypatch.setattr(stdio_runner, "sqlite_store", MagicMock())
    monkeypatch.setattr(stdio_runner, "initialization_error", None)
    monkeypatch.setattr(stdio_runner, "_repo_resolver", MagicMock())
    monkeypatch.setattr(stdio_runner, "_lazy_summarizer", MagicMock())
    monkeypatch.setattr(
        stdio_runner,
        "_gate",
        SimpleNamespace(
            enabled=False, check=lambda *_args, **_kwargs: None, verify=lambda *_: True
        ),
    )


def test_call_tool_returns_call_tool_result_with_object_structured_content(monkeypatch):
    _configure_runner(monkeypatch)
    payload = [{"file": "/tmp/demo.py", "line": 7, "symbol": "demo"}]

    async def _handle_search_code(**_kwargs):
        return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

    monkeypatch.setattr(
        stdio_runner.tool_handlers,
        "handle_search_code",
        _handle_search_code,
    )

    result = _run(stdio_runner.call_tool("search_code", {"query": "demo"}))

    assert isinstance(result, types.CallToolResult)
    assert result.is_error is False
    assert result.structured_content == {"results": payload}
    assert json.loads(result.content[0].text) == payload


def test_call_tool_sets_is_error_for_structured_error_payloads(monkeypatch):
    _configure_runner(monkeypatch)
    payload = {
        "error": "Search failed",
        "details": "boom",
        "query": "demo",
    }

    async def _handle_search_code(**_kwargs):
        return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

    monkeypatch.setattr(
        stdio_runner.tool_handlers,
        "handle_search_code",
        _handle_search_code,
    )

    result = _run(stdio_runner.call_tool("search_code", {"query": "demo"}))

    assert isinstance(result, types.CallToolResult)
    assert result.is_error is True
    assert result.structured_content == payload
    assert json.loads(result.content[0].text) == payload
