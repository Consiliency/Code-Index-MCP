#!/usr/bin/env python3
"""Disposable installed-runtime probe; copied outside the checkout by release_smoke."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import timedelta
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TOKEN = "release_smoke_token"
SOURCE = 'def release_smoke_token():\n    return "installed lexical fixture"\n'
PASSWORD = "synthetic-smoke-admin-password-00000000"


def environment(root: Path) -> dict[str, str]:
    """Use only synthetic configuration, never operator credentials or live indexes."""
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "home" / ".config"),
        "MCP_REPO_REGISTRY": str(root / "registry.json"),
        "MCP_INDEX_STORAGE_PATH": str(root / "indexes"),
        "MCP_ALLOWED_ROOTS": str(root),
        "MCP_WORKSPACE_ROOT": str(root / "fixture"),
        "SEMANTIC_SEARCH_ENABLED": "false",
        "RERANKER_TYPE": "none",
        "MCP_AUTO_INDEX": "false",
        "MCP_METRICS_PORT": "0",
        "MCP_SKIP_PLUGIN_PREINDEX": "true",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "MCP_ENVIRONMENT": "development",
        "JWT_SECRET_KEY": "synthetic-smoke-jwt-key-00000000000000",
        "DEFAULT_ADMIN_PASSWORD": PASSWORD,
        "DEFAULT_ADMIN_EMAIL": "admin@localhost",
        "CORS_ORIGINS": "http://localhost",
    }


def command(argv: list[str], root: Path) -> None:
    subprocess.run(
        argv, cwd=root, env=environment(root), check=True, timeout=120, stdout=subprocess.DEVNULL
    )


def prepare(root: Path, entrypoint: str) -> None:
    fixture = root / "fixture"
    fixture.mkdir()
    (root / "home").mkdir()
    (root / "unregistered").mkdir()
    (fixture / "smoke.py").write_text(SOURCE)
    (fixture / ".gitignore").write_text(".mcp-index/\n")
    command(["git", "init", "-b", "main", str(fixture)], root)
    command(["git", "-C", str(fixture), "add", "."], root)
    command(
        [
            "git",
            "-C",
            str(fixture),
            "-c",
            "user.name=Smoke",
            "-c",
            "user.email=smoke@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "synthetic fixture",
        ],
        root,
    )
    command(
        [entrypoint, "repository", "register", str(fixture), "--no-auto-sync", "--no-artifacts"],
        root,
    )


def schema_probe(root: Path) -> None:
    import mcp_server
    from mcp_server.storage.sqlite_store import SQLiteStore

    module = Path(mcp_server.__file__).resolve()
    if "site-packages" not in module.parts:
        raise AssertionError(f"Probe imported checkout code: {module}")
    resources = files("mcp_server.storage").joinpath("migrations")
    assert resources.joinpath("007_schema_reconciliation.sql").is_file()
    for name in ("fresh", "upgrade"):
        path = root / f"{name}-schema.db"
        if name == "upgrade" and not path.exists():
            with sqlite3.connect(path) as conn:
                conn.executescript(resources.joinpath("001_initial_schema.sql").read_text())
                conn.execute("ALTER TABLE symbols ADD COLUMN token_count INTEGER")
                conn.execute("INSERT INTO repositories(path,name) VALUES('/retained','retained')")
        SQLiteStore(str(path)).close()
        SQLiteStore(str(path)).close()
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (7,)
            assert {"tracked_branch", "git_common_dir"} <= {
                row[1] for row in conn.execute("PRAGMA table_info(repositories)")
            }
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='update_chunk_timestamp'"
            ).fetchone()
            assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            if name == "upgrade":
                assert conn.execute("SELECT name FROM repositories").fetchall() == [("retained",)]
    print(
        json.dumps(
            {
                "schema": "passed",
                "version": version("index-it-mcp"),
                "module": str(module),
                "uid": getattr(os, "getuid", lambda: None)(),
            }
        )
    )


def payload(result, *, expected_error=False):
    assert result.isError == expected_error, result
    return json.loads("".join(block.text for block in result.content if hasattr(block, "text")))


def rows(value):
    return value if isinstance(value, list) else value.get("results", [])


def unavailable(value, state, *, code="index_unavailable"):
    assert value["code"] == code, value
    assert value["safe_fallback"] == "native_search", value
    assert value["readiness"]["state"] == state, value


async def stdio_probe(root: Path, entrypoint: str) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    fixture = str(root / "fixture")
    params = StdioServerParameters(
        command=entrypoint, args=["stdio"], env=environment(root), cwd=str(root)
    )
    for attempt in range(2):
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=60)
            ) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert {"reindex", "search_code", "symbol_lookup", "get_status"} <= {
                    tool.name for tool in tools.tools
                }
                if attempt == 0:
                    rebuilt = payload(await client.call_tool("reindex", {"repository": fixture}))
                    assert "error" not in rebuilt, rebuilt
                found = payload(
                    await client.call_tool(
                        "search_code", {"query": TOKEN, "repository": fixture, "semantic": False}
                    )
                )
                assert rows(found), found
                symbol = payload(
                    await client.call_tool(
                        "symbol_lookup", {"symbol": TOKEN, "repository": fixture}
                    )
                )
                assert symbol.get("symbol", symbol.get("name")) == TOKEN, symbol
                miss = payload(
                    await client.call_tool(
                        "search_code",
                        {"query": "nonexistent_literal_739195", "repository": fixture},
                    )
                )
                assert rows(miss) == [] and not (isinstance(miss, dict) and miss.get("code")), miss
                refused = payload(
                    await client.call_tool(
                        "search_code", {"query": TOKEN, "repository": str(root / "unregistered")}
                    ),
                    expected_error=True,
                )
                unavailable(refused, "unregistered_repository")
                status = payload(await client.call_tool("get_status", {}))
                assert any(
                    row.get("readiness") == "ready" for row in status["repositories"]
                ), status
                command(["git", "-C", fixture, "checkout", "-b", f"unsupported-{attempt}"], root)
                try:
                    refused = payload(
                        await client.call_tool(
                            "search_code", {"query": TOKEN, "repository": fixture}
                        ),
                        expected_error=True,
                    )
                    unavailable(refused, "wrong_branch")
                finally:
                    command(["git", "-C", fixture, "checkout", "main"], root)
        print(json.dumps({"stdio_session": attempt + 1, "queries": "passed"}))


def python_probe(root: Path) -> None:
    from mcp_server import ClientSearchOptions, open_client
    from mcp_server.cli.bootstrap import initialize_stateless_services

    stores, resolver, dispatcher, registry, manager = initialize_stateless_services(
        root / "registry.json"
    )
    try:
        repo = registry.list_all()[0]
        result = manager.rebuild_repository_index(repo.repository_id)
        assert result.action == "full_index", result.error
    finally:
        dispatcher.shutdown()
        stores.shutdown()
    with open_client(
        workspace_root=root / "fixture", registry_path=root / "registry.json"
    ) as client:
        assert client.search_code(ClientSearchOptions(query=TOKEN)).results
        assert not client.search_code(ClientSearchOptions(query="absent_data_84963")).results
        assert client.symbol_lookup(TOKEN).found
    print(json.dumps({"python_client": "passed"}))


def http_probe(root: Path, url: str, rebuild: bool) -> None:
    def request(path, *, query=None, data=None, token=None, forwarded=None):
        if query:
            path += "?" + urlencode(query)
        headers = {"Content-Type": "application/json"}
        if forwarded:
            headers["X-Forwarded-For"] = forwarded
        if token:
            headers["Authorization"] = "Bearer " + token
        req = Request(
            url + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers=headers,
        )
        try:
            with urlopen(req, timeout=120) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            try:
                return exc.code, json.load(exc)
            except json.JSONDecodeError:
                raise AssertionError(f"HTTP {exc.code} returned non-JSON for {path}") from exc

    assert os.getuid() != 0, "Container probe must run as the configured non-root user"
    prior_session = root / "synthetic-http-session.json"
    if not rebuild:
        prior = json.loads(prior_session.read_text())
        code, _ = request("/status", token=prior["access_token"])
        assert code == 401, "access session survived process restart"
        code, _ = request(
            "/api/v1/auth/refresh", query={"refresh_token": prior["refresh_token"]}, data={}
        )
        assert code == 401, "refresh session survived process restart"
    code, auth = request("/api/v1/auth/login", data={"username": "admin", "password": PASSWORD})
    assert code == 200, code
    if rebuild:
        prior_session.write_text(
            json.dumps({key: auth[key] for key in ("access_token", "refresh_token")})
        )
        prior_session.chmod(0o600)
    token = auth["access_token"]
    fixture = str(root / "fixture")
    if rebuild:
        code, value = request("/reindex", query={"repository": fixture}, data={}, token=token)
        assert code == 200 and value.get("status") in {"completed", "success"}, (code, value)
    code, value = request(
        "/search", query={"q": TOKEN, "repository": fixture}, token=token, forwarded="198.51.100.77"
    )
    assert code == 200 and rows(value), (code, value)
    code, value = request(
        "/search", query={"q": "nonexistent_literal_739195", "repository": fixture}, token=token
    )
    assert code == 200 and rows(value) == [], (code, value)
    code, value = request("/symbol", query={"symbol": TOKEN, "repository": fixture}, token=token)
    assert code == 200 and value.get("symbol", value.get("name")) == TOKEN, (code, value)
    code, value = request(
        "/search", query={"q": TOKEN, "repository": str(root / "unregistered")}, token=token
    )
    assert code == 503, (code, value)
    unavailable(value["detail"], "unregistered_repository", code="unregistered_repository")
    branch = "unsupported-http-" + ("first" if rebuild else "restart")
    command(["git", "-C", fixture, "checkout", "-b", branch], root)
    try:
        code, value = request("/search", query={"q": TOKEN, "repository": fixture}, token=token)
        assert code == 503, (code, value)
        unavailable(value["detail"], "wrong_branch", code="wrong_branch")
    finally:
        command(["git", "-C", fixture, "checkout", "main"], root)
    print(json.dumps({"http_queries": "passed", "after_restart": not rebuild}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=["prepare", "schema", "python", "stdio", "http"], required=True
    )
    parser.add_argument("--entrypoint", default="index-it-mcp")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    os.environ.update(environment(args.root))
    try:
        if args.mode == "prepare":
            prepare(args.root, args.entrypoint)
        elif args.mode == "schema":
            schema_probe(args.root)
        elif args.mode == "python":
            python_probe(args.root)
        elif args.mode == "stdio":
            asyncio.run(stdio_probe(args.root, args.entrypoint))
        else:
            http_probe(args.root, args.url, not args.restart)
    finally:
        # Only synthetic smoke outputs; permit the host tempfile owner to clean mounted dirs.
        for path in args.root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o777)


if __name__ == "__main__":
    main()
