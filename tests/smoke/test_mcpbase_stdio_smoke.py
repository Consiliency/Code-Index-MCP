"""MCPBASE official Python SDK STDIO smoke."""

from __future__ import annotations

import json
import os
from importlib.metadata import version
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.fixtures.multi_repo import boot_test_server, build_temp_repo
from tests.test_release_metadata import EXPECTED_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
EXPECTED_TOOLS = {
    "symbol_lookup",
    "search_code",
    "get_status",
    "list_plugins",
    "reindex",
    "write_summaries",
    "summarize_sample",
    "handshake",
}


def _text_payload(result) -> dict:
    assert result.content, "expected text content from MCP tool call"
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_official_sdk_client_can_initialize_list_tools_and_call_baseline_tools(
    tmp_path: Path,
):
    token = "mcpbase_stdio_probe_token"
    symbol = "mcpbase_stdio_probe_symbol"
    repo_path, repo_id = build_temp_repo(
        tmp_path,
        "mcpbase_stdio_repo",
        seed_files={"seed.py": f"def {symbol}():\n    return '{token}'\n"},
    )
    registry_path = tmp_path / "registry.json"
    with boot_test_server(tmp_path, [repo_path]):
        pass

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["MCP_ALLOWED_ROOTS"] = str(repo_path.resolve())
    env["MCP_REPO_REGISTRY"] = str(registry_path)
    env["SEMANTIC_SEARCH_ENABLED"] = "false"
    params = StdioServerParameters(
        command=str(RUNNER_PYTHON),
        args=["-m", "mcp_server.cli.stdio_runner"],
        cwd=str(repo_path),
        env=env,
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            assert init.protocolVersion == "2025-11-25"
            assert version("mcp") == "1.27.0"

            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS

            status = _text_payload(await session.call_tool("get_status", {}))
            assert status["status"] in {"healthy", "unknown"}
            assert status["version"] == EXPECTED_VERSION

            plugins = _text_payload(await session.call_tool("list_plugins", {}))
            assert "plugin_availability" in plugins
            assert plugins["availability_counts"]

            lookup = _text_payload(
                await session.call_tool(
                    "symbol_lookup",
                    {"repository": str(repo_path), "symbol": symbol},
                )
            )
            assert lookup["symbol"] == symbol
            assert lookup["defined_in"].endswith("seed.py")

            search = _text_payload(
                await session.call_tool(
                    "search_code",
                    {"repository": str(repo_path), "query": token, "semantic": False},
                )
            )
            search_results = search["results"] if isinstance(search, dict) else search
            assert any(result.get("file", "").endswith("seed.py") for result in search_results)

    assert repo_id
