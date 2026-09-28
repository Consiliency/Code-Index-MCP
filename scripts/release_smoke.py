#!/usr/bin/env python3
"""Release smoke checks for wheel, lexical MCP, and production container paths."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import venv
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

REPO = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/consiliency/code-index-mcp:local-smoke"
CANONICAL_ENTRYPOINTS = ("mcp-index", "index-it-mcp")
REMOVED_ENTRYPOINTS = ("code-index-mcp",)


def _run(cmd: list[str], *, cwd: Path = REPO, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    subprocess.run(cmd, cwd=str(cwd), env=run_env, check=True)


def _venv_bin(root: Path, name: str) -> Path:
    if os.name == "nt":
        return root / "Scripts" / f"{name}.exe"
    return root / "bin" / name


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def smoke_wheel() -> None:
    with tempfile.TemporaryDirectory(prefix="mcp-release-wheel-") as tmp:
        root = Path(tmp)
        dist = root / "dist"
        venv_dir = root / "venv"
        dist.mkdir()

        _run([sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)])
        wheels = sorted(dist.glob("index_it_mcp-*.whl"))
        if not wheels:
            raise RuntimeError(f"No index_it_mcp wheel produced in {dist}")

        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
        python = _venv_bin(venv_dir, "python")
        _run(
            [str(python), "-m", "pip", "install", str(wheels[-1])],
            env={"PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        )
        for entrypoint in CANONICAL_ENTRYPOINTS:
            script = _venv_bin(venv_dir, entrypoint)
            if not script.exists():
                raise RuntimeError(f"Missing expected wheel entrypoint: {entrypoint}")
            _run([str(script), "--help"])
        for entrypoint in REMOVED_ENTRYPOINTS:
            script = _venv_bin(venv_dir, entrypoint)
            if script.exists():
                raise RuntimeError(f"Unexpected legacy wheel entrypoint present: {entrypoint}")


def smoke_stdio() -> None:
    sys.path.insert(0, str(REPO))
    from tests.fixtures.multi_repo import boot_test_server, build_temp_repo

    source = """
class SmokeWidget:
    pass


def release_smoke_token():
    return "p22 lexical smoke"
"""
    with tempfile.TemporaryDirectory(prefix="mcp-release-stdio-") as tmp:
        tmp_path = Path(tmp)
        repo_path, _ = build_temp_repo(
            tmp_path,
            "fixture_repo",
            seed_files={"smoke.py": source.lstrip()},
        )
        unregistered_path = tmp_path / "unregistered_repo"
        unregistered_path.mkdir()
        old_qdrant_path = os.environ.get("QDRANT_PATH")
        old_semantic_enabled = os.environ.get("SEMANTIC_SEARCH_ENABLED")
        os.environ["QDRANT_PATH"] = str(tmp_path / "missing-vector-index.qdrant")
        os.environ["SEMANTIC_SEARCH_ENABLED"] = "false"
        try:
            with boot_test_server(tmp_path, [repo_path], extra_roots=[unregistered_path]) as server:
                search = server.call_tool(
                    "search_code",
                    {
                        "query": "release_smoke_token",
                        "repository": str(repo_path),
                        "semantic": False,
                        "limit": 5,
                    },
                )
                if not isinstance(search, list) or not search:
                    raise AssertionError(f"search_code did not return results: {search!r}")

                lookup = server.call_tool(
                    "symbol_lookup",
                    {"symbol": "release_smoke_token", "repository": str(repo_path)},
                )
                if lookup.get("symbol") != "release_smoke_token":
                    raise AssertionError(f"symbol_lookup failed: {lookup!r}")

                status = server.call_tool("get_status", {})
                rows = status.get("repositories", [])
                if len(rows) != 1 or rows[0].get("readiness") != "ready":
                    raise AssertionError(f"get_status readiness failed: {status!r}")

                fallback = server.call_tool(
                    "search_code",
                    {
                        "query": "release_smoke_token",
                        "repository": str(unregistered_path),
                        "semantic": False,
                    },
                )
                if (
                    fallback.get("code") != "index_unavailable"
                    or fallback.get("safe_fallback") != "native_search"
                    or fallback.get("readiness", {}).get("state") != "unregistered_repository"
                ):
                    raise AssertionError(f"fallback contract failed: {fallback!r}")
        finally:
            if old_qdrant_path is None:
                os.environ.pop("QDRANT_PATH", None)
            else:
                os.environ["QDRANT_PATH"] = old_qdrant_path
            if old_semantic_enabled is None:
                os.environ.pop("SEMANTIC_SEARCH_ENABLED", None)
            else:
                os.environ["SEMANTIC_SEARCH_ENABLED"] = old_semantic_enabled


def _poll_health(port: int, *, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/health"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") == "healthy":
                return
        except (OSError, URLError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(1.0)
    raise RuntimeError(f"Container health check did not pass: {last_error}")


def smoke_container() -> None:
    if shutil.which("docker") is None:
        raise RuntimeError("docker is required for --container smoke")

    _run(["docker", "build", "-f", "docker/dockerfiles/Dockerfile.production", "-t", IMAGE, "."])
    _run(["docker", "run", "--rm", IMAGE, "mcp-index", "--help"])
    container_contract = r"""
import asyncio
from importlib.metadata import version
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from baml_sdk import ChunkInput, SummarizeChunkAlone_spec, SummarizeFileChunks_spec
from mcp_server.cli.tool_handlers import handle_get_status, handle_search_code
from mcp_server.health.repository_readiness import RepositoryReadiness, RepositoryReadinessState


class Dispatcher:
    def get_statistics(self):
        return {}

    def health_check(self):
        return {}


class Resolver:
    def classify(self, path):
        return RepositoryReadiness(
            state=RepositoryReadinessState.UNREGISTERED_REPOSITORY,
            requested_path=str(Path(path).resolve()),
            remediation="Register this repository path before querying it.",
        )

    def resolve(self, path):
        return None


async def main():
    assert version("baml-bridge") == "0.20.1"
    os.environ["CEREBRAS_API_KEY"] = "dummy-offline"
    with patch("socket.socket.connect", side_effect=AssertionError("BAML smoke must stay offline")):
        batch = SummarizeFileChunks_spec(
            language="python",
            file_path="smoke.py",
            file_content="def alpha(): return 1",
            chunks=[ChunkInput(
                chunk_id="smoke-1",
                symbol="alpha",
                node_type="function_definition",
                line_start=1,
                line_end=1,
                content="return 1",
            )],
        )
        assert batch.build_request().url == "https://api.cerebras.ai/v1/chat/completions"
        assert "chunk_id: smoke-1" in batch.prompt().messages()[1].content
        single = SummarizeChunkAlone_spec(
            language="python",
            symbol="alpha",
            line_start=1,
            line_end=1,
            content="return 1",
            parent_context="",
            file_context="smoke.py",
        )
        assert single.build_request().url == "https://api.cerebras.ai/v1/chat/completions"
        assert 'Set chunk_id to "alpha"' in single.prompt().messages()[1].content

    with tempfile.TemporaryDirectory(prefix="mcp-container-fallback-") as tmp:
        os.environ["MCP_ALLOWED_ROOTS"] = tmp
        repo = Path(tmp) / "unregistered"
        repo.mkdir()
        dispatcher = Dispatcher()
        resolver = Resolver()
        fallback = json.loads(
            (
                await handle_search_code(
                    arguments={
                        "query": "release_smoke_token",
                        "repository": str(repo),
                        "semantic": False,
                    },
                    dispatcher=dispatcher,
                    repo_resolver=resolver,
                )
            )[0].text
        )
        status = json.loads(
            (
                await handle_get_status(
                    arguments={},
                    dispatcher=dispatcher,
                    repo_resolver=resolver,
                    server_version="container-smoke",
                )
            )[0].text
        )
        assert fallback["code"] == "index_unavailable", fallback
        assert fallback["safe_fallback"] == "native_search", fallback
        assert fallback["readiness"]["state"] == "unregistered_repository", fallback
        assert "repositories" in status, status


asyncio.run(main())
"""
    _run(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            "SEMANTIC_SEARCH_ENABLED=false",
            IMAGE,
            "python",
            "-c",
            container_contract,
        ]
    )

    port = _free_port()
    container_id = subprocess.check_output(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-p",
            f"127.0.0.1:{port}:8000",
            "-e",
            "MCP_ENVIRONMENT=development",
            "-e",
            "JWT_SECRET_KEY=p22-release-smoke-jwt-secret-key-00000000",
            "-e",
            "DEFAULT_ADMIN_PASSWORD=p22-release-smoke-admin-password-00000000",
            "-e",
            "DEFAULT_ADMIN_EMAIL=admin@localhost",
            "-e",
            "CORS_ORIGINS=http://localhost",
            IMAGE,
        ],
        cwd=str(REPO),
        text=True,
    ).strip()
    try:
        _poll_health(port)
    finally:
        subprocess.run(
            ["docker", "stop", container_id],
            cwd=str(REPO),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wheel",
        action="store_true",
        help="Build wheel, install it in a fresh venv, and run mcp-index --help",
    )
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Run lexical search_code and symbol_lookup against a fixture repo",
    )
    parser.add_argument(
        "--container", action="store_true", help="Build and smoke the production container image"
    )
    parser.add_argument(
        "--all", action="store_true", help="Run wheel, stdio, and container smoke checks"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.all:
        args.wheel = args.stdio = args.container = True
    if not (args.wheel or args.stdio or args.container):
        raise SystemExit("Choose at least one of --wheel, --stdio, --container, or --all")

    if args.wheel:
        smoke_wheel()
    if args.stdio:
        smoke_stdio()
    if args.container:
        smoke_container()


if __name__ == "__main__":
    main()
