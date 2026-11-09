"""MCPBASE official Python SDK STDIO smoke."""

from __future__ import annotations

import json
import os
import sqlite3
from importlib.metadata import version
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from mcp_server import __version__
from mcp_server.storage.repository_registry import RepositoryRegistry
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
            assert init.protocol_version == "2025-11-25"
            assert version("mcp").split(".")[0] == "2"
            # serverInfo reports the package, not the mcp library (Consiliency/Code-Index-MCP#128).
            assert init.server_info.name == "code-index-mcp-fast-search"
            assert init.server_info.version == __version__ == EXPECTED_VERSION

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


@pytest.mark.asyncio
async def test_reindexed_decorated_symbols_survive_stdio_restart(tmp_path: Path):
    source = (
        "@missing\n"
        "def FrozenEval():\n"
        "    return 1\n"
        "@missing\n"
        "async def asynchronous():\n"
        "    value = 1\n"
        "    return value\n"
        "@missing\n"
        "class RepositoryReadiness:\n"
        "    @missing\n"
        "    async def ready(self):\n"
        "        value = True\n"
        "        return value\n"
        "class FrozenEvalError:\n"
        "    pass\n"
        "class OnlyLongerName:\n"
        "    pass\n"
    )
    files = {
        "source.py": source,
        "examples.md": (
            "# Symbol examples\n\n"
            "documentation_probe_token\n\n"
            "```python\ndef FrozenEval():\n    pass\n"
            "class RepositoryReadiness:\n    pass\n```\n"
        ),
    }
    repo_path, repo_id = build_temp_repo(tmp_path, "symbol_restart_repo", seed_files=files)
    with boot_test_server(tmp_path, [repo_path]):
        pass
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "MCP_ALLOWED_ROOTS": str(repo_path.resolve()),
            "MCP_REPO_REGISTRY": str(tmp_path / "registry.json"),
            "SEMANTIC_SEARCH_ENABLED": "false",
            "MCP_AUTO_INDEX": "false",
            "MCP_SKIP_PLUGIN_PREINDEX": "true",
        }
    )
    env.pop("MCP_PLUGIN_SANDBOX_DISABLE", None)
    params = StdioServerParameters(
        command=str(RUNNER_PYTHON),
        args=["-m", "mcp_server.cli.stdio_runner"],
        cwd=str(repo_path),
        env=env,
    )
    expected = [
        ("FrozenEval", "function", 2, 3),
        ("asynchronous", "function", 5, 7),
        ("RepositoryReadiness", "class", 9, 13),
        ("ready", "method", 11, 13),
    ]
    for first_session in (True, False):
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                if first_session:
                    for filename in files:
                        result = await session.call_tool(
                            "reindex", {"path": str(repo_path / filename)}
                        )
                        assert not result.is_error
                        payload = _text_payload(result)
                        assert payload == result.structured_content
                        assert payload["mutation_performed"] is True
                        assert payload["indexed_files"] == 1
                registration = RepositoryRegistry(tmp_path / "registry.json").get(repo_id)
                assert registration is not None
                with sqlite3.connect(registration.index_path) as conn:
                    rows = conn.execute(
                        "SELECT s.name, s.kind, s.line_start, s.line_end, f.path, f.language "
                        "FROM symbols s JOIN files f ON s.file_id = f.id"
                    ).fetchall()
                for name, kind, start, end in expected:
                    assert any(
                        row[:4] == (name, kind, start, end) and row[4].endswith("source.py")
                        for row in rows
                    ), (name, rows)
                    result = await session.call_tool(
                        "symbol_lookup", {"repository": str(repo_path), "symbol": name}
                    )
                    assert not result.is_error
                    payload = _text_payload(result)
                    assert payload == result.structured_content
                    assert payload["symbol"] == name
                    assert payload["kind"] == kind
                    assert payload["line"] == start
                    assert payload["defined_in"].endswith("source.py")
                assert any(
                    row[0] == "FrozenEval"
                    and row[4].endswith("examples.md")
                    and row[5] == "markdown"
                    for row in rows
                )
                miss = _text_payload(
                    await session.call_tool(
                        "symbol_lookup", {"repository": str(repo_path), "symbol": "OnlyLonger"}
                    )
                )
                assert miss["result"] == "not_found"
                assert miss["readiness"]["state"] == "ready"
                search = _text_payload(
                    await session.call_tool(
                        "search_code",
                        {
                            "repository": str(repo_path),
                            "query": "documentation_probe_token",
                            "semantic": False,
                        },
                    )
                )
                results = search["results"] if isinstance(search, dict) else search
                assert any(row["file"].endswith("examples.md") for row in results)
    assert repo_id
