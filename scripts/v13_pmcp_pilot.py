"""Installed PMCP pilot driver. Live inference is a separate explicit effect."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import psutil

REPO = Path(__file__).resolve().parents[1]
QUERY_TEXTS = {
    "ledger": "calculate available balance from credits and debits",
    "catalog": "find a product by its code",
}
QDRANT_IMAGE = (
    "qdrant/qdrant@sha256:f1c7272cdac52b38c1a0e89313922d940ba50afd90d593a1605dbbc214e66ffb"
)
GOALS = {
    "offline": {
        "installed_identity",
        "provisioning",
        "two_repositories",
        "sibling_refusal",
        "wrong_branch_refusal",
        "stale_refusal",
        "no_match",
        "handshake",
        "reconnect",
        "repeat_provisioning",
        "installed_lifecycle",
        "metrics_contention",
        "privacy",
    },
    "live": {
        "retrieval",
        "provenance",
        "rename",
        "delete",
        "rebuild",
        "restart",
        "contention",
        "synthetic_only",
        "local_only",
        "budget_enforced",
    },
    "browser": {
        "inspector_queries",
        "inspector_refusals",
        "inspector_reconnect",
        "admin_queries",
        "admin_refusals",
        "admin_reindex",
        "console_checked",
        "screenshots",
    },
}


class PilotRefused(RuntimeError):
    """A missing acceptance condition; messages contain metadata only."""


def digest_json(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_receipt(receipt: dict, manifest: dict, kind: str) -> None:
    for key, expected in {
        "kind": kind,
        "source": manifest["source"],
        "wheel_sha256": manifest["wheel_sha256"],
        "manifest_sha256": digest_json(manifest),
    }.items():
        if receipt.get(key) != expected:
            raise PilotRefused("receipt_binding_mismatch")
    goals = receipt.get("goals", {})
    if any(goals.get(name) is not True for name in GOALS[kind]):
        raise PilotRefused("receipt_goals_incomplete")
    if kind in {"offline", "live", "browser"}:
        durations = receipt.get("shutdown_seconds", [])
        rss = receipt.get("peak_rss_mib")
        if (
            not durations
            or any(
                not isinstance(t, (int, float)) or not math.isfinite(t) or not 0 <= t <= 5
                for t in durations
            )
            or receipt.get("surviving_children") != []
            or not isinstance(rss, (int, float))
            or not math.isfinite(rss)
            or not 0 < rss <= 2048
        ):
            raise PilotRefused("operational_limits_failed")
    if kind == "live":
        if receipt.get("rehearsal") is True:
            raise PilotRefused("rehearsal_is_not_live_acceptance")
        budget = receipt.get("budget", {})
        if (
            not 0 < budget.get("reserved_input_units", math.inf) <= 100000
            or not 0 <= budget.get("elapsed_seconds", math.inf) <= 900
            or budget.get("blocked") is not None
        ):
            raise PilotRefused("budget_failed")
        for name, limit in {"symbol": 100, "lexical": 500, "semantic": 500}.items():
            values = receipt.get("latencies_ms", {}).get(name, [])
            if (
                len(values) < 40
                or any(
                    not isinstance(t, (int, float)) or not math.isfinite(t) or t < 0 for t in values
                )
                or sorted(values)[math.ceil(len(values) * 0.95) - 1] > limit
            ):
                raise PilotRefused("latency_failed")
            if receipt.get("contention_successes", {}).get(name, 0) < 20:
                raise PilotRefused("contention_incomplete")


def source_identity() -> dict:
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()

    if git("status", "--porcelain"):
        raise PilotRefused("candidate_worktree_dirty")
    return {
        "source": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"),
        "lock_sha256": digest_file(REPO / "uv.lock"),
    }


def write_json(path: Path, value: dict, *, exclusive=False) -> None:
    with path.open("x" if exclusive else "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    path.chmod(0o600)


def clean_env(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "home" / ".config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "UV_CACHE_DIR": str(root / "uv-cache"),
        "UV_NO_ENV_FILE": "1",
        "UV_NO_CONFIG": "1",
        "PYTHONUNBUFFERED": "1",
    }


def run_command(argv: list[str], root: Path, label: str, *, env=None, timeout=300, cwd=None) -> str:
    result = subprocess.run(
        argv,
        cwd=cwd or root,
        env=env or clean_env(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    (root / f"{label}.stdout").write_bytes(result.stdout)
    (root / f"{label}.stderr").write_bytes(result.stderr)
    if result.returncode:
        raise PilotRefused(f"command_failed:{label}:{result.returncode}")
    return result.stdout.decode()


def uvx_prefix(root: Path, wheel: Path, python: str) -> list[str]:
    return [
        shutil.which("uvx") or "uvx",
        "--isolated",
        "--no-env-file",
        "--no-config",
        "--python",
        python,
        "--from",
        f"index-it-mcp[production] @ {wheel.as_uri()}",
        "--constraints",
        str(root / "constraints.txt"),
    ]


def prepare(root: Path) -> dict:
    identity = source_identity()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    (root / "home").mkdir()
    python = str(Path(sys.base_prefix) / "bin" / "python3.12")
    if sys.version_info[:2] != (3, 12) or not Path(python).is_file():
        raise PilotRefused("python_312_required")
    run_command(
        ["uv", "build", "--wheel", "--out-dir", str(root / "dist")],
        root,
        "pilot-build",
        env=clean_env(root),
        cwd=REPO,
    )
    wheels = list((root / "dist").glob("*.whl"))
    if len(wheels) != 1:
        raise PilotRefused("wheel_count")
    wheel = wheels[0]
    run_command(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--extra",
            "production",
            "--no-emit-project",
            "--no-hashes",
            "--output-file",
            str(root / "constraints.txt"),
        ],
        root,
        "pilot-lock-export",
        env=clean_env(root),
        cwd=REPO,
    )
    for name in (
        "v13_pmcp_pilot.py",
        "installed_runtime_smoke.py",
        "safety_runtime_smoke.py",
        "v13_pilot_estimate.py",
        "v13_pilot_budget.py",
    ):
        shutil.copyfile(REPO / "scripts" / name, root / name)
    prefix = uvx_prefix(root, wheel, python)
    installed = json.loads(
        run_command(
            prefix
            + [
                "python",
                "-I",
                str(root / "v13_pmcp_pilot.py"),
                "--mode",
                "identity",
                "--wheel",
                str(wheel),
            ],
            root,
            "installed-identity",
        )
    )
    manifest = {
        "schema": "v13-pilot-manifest.v1",
        **identity,
        "wheel": wheel.name,
        "wheel_sha256": digest_file(wheel),
        "constraints_sha256": digest_file(root / "constraints.txt"),
        "installed": installed,
        "uvx_prefix": prefix,
        "python": python,
        "pmcp_version": run_command(["pmcp", "--version"], root, "pmcp-version").strip(),
    }
    write_json(root / "manifest.json", manifest, exclusive=True)
    return manifest


def installed_identity(wheel: Path) -> dict:
    from importlib.metadata import version

    import mcp_server

    package = Path(mcp_server.__file__).resolve().parent
    if "site-packages" not in package.parts or sys.version_info[:2] != (3, 12):
        raise PilotRefused("installed_identity_failed")
    checked = 0
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith("mcp_server/") and not name.endswith("/"):
                if (package.parent / name).read_bytes() != archive.read(name):
                    raise PilotRefused("installed_artifact_mismatch")
                checked += 1
    return {
        "python": sys.executable,
        "python_version": list(sys.version_info[:3]),
        "package_module": str(package / "__init__.py"),
        "version": version("index-it-mcp"),
        "prometheus_client_version": version("prometheus-client"),
        "wheel_files_verified": checked,
    }


def load_manifest(root: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text())
    if any(manifest[key] != value for key, value in source_identity().items()):
        raise PilotRefused("candidate_binding_changed")
    if digest_file(root / "dist" / manifest["wheel"]) != manifest["wheel_sha256"]:
        raise PilotRefused("wheel_binding_changed")
    if digest_file(root / "constraints.txt") != manifest["constraints_sha256"]:
        raise PilotRefused("constraints_binding_changed")
    return manifest


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def create_fixture(root: Path, manifest: dict, *, label: str) -> dict:
    from v13_pilot_estimate import SYNTHETIC_CORPUS

    fixture = root / label
    fixture.mkdir()
    for directory in ("home", "project", "repos", "locks", "unregistered"):
        (fixture / directory).mkdir()
    env = clean_env(fixture)
    env["UV_CACHE_DIR"] = str(root / "uv-cache")
    secret = secrets.token_urlsafe(36)
    env.update(
        {
            "MCP_REPO_REGISTRY": str(fixture / "registry.json"),
            "MCP_INDEX_STORAGE_PATH": str(fixture / "indexes"),
            "MCP_ALLOWED_ROOTS": str(fixture),
            "MCP_DEPLOYMENT_PROFILE": "lexical_only",
            "SEMANTIC_SEARCH_ENABLED": "false",
            "SEMANTIC_DEFAULT_PROFILE": "legacy-default",
            "MCP_AUTO_INDEX": "false",
            "MCP_SKIP_PLUGIN_PREINDEX": "true",
            "RERANKER_TYPE": "none",
            "RERANKING_ENABLED": "false",
            "MCP_METRICS_PORT": str(free_port()),
            "MCP_CLIENT_SECRET": secret,
            "LOG_LEVEL": "WARNING",
            "MCP_ENVIRONMENT": "development",
            "JWT_SECRET_KEY": secrets.token_urlsafe(36),
            "DEFAULT_ADMIN_PASSWORD": secrets.token_urlsafe(36),
            "DEFAULT_ADMIN_EMAIL": "admin@localhost",
            "CORS_ORIGINS": "http://localhost",
        }
    )
    for repo, files in SYNTHETIC_CORPUS.items():
        path = fixture / "repos" / repo
        path.mkdir()
        for name, content in files.items():
            (path / name).write_text(content)
        (path / ".gitignore").write_text("/.mcp-index/\n")
        run_command(["git", "init", "-b", "main", str(path)], fixture, repo + "-init", env=env)
        commit_fixture(path, env)
        run_command(
            manifest["uvx_prefix"]
            + [
                "mcp-index",
                "repository",
                "register",
                str(path),
                "--no-auto-sync",
                "--no-artifacts",
            ],
            fixture,
            repo + "-register",
            env=env,
        )
    sibling = fixture / "repos" / "ledger-sibling"
    run_command(
        [
            "git",
            "-C",
            str(fixture / "repos" / "ledger"),
            "worktree",
            "add",
            "--detach",
            str(sibling),
        ],
        fixture,
        "sibling",
        env=env,
    )
    config = {
        "mcpServers": {
            "index-it-mcp": {
                "command": manifest["uvx_prefix"][0],
                "args": manifest["uvx_prefix"][1:] + ["index-it-mcp", "stdio"],
                "env": env,
            }
        }
    }
    write_json(fixture / "project" / ".mcp.json", config)
    write_json(
        fixture / "policy.json",
        {
            "servers": {"allowlist": ["index-it-mcp"]},
            "tools": {"allowlist": ["index-it-mcp::*"]},
            "resources": {"denylist": ["*"]},
            "prompts": {"denylist": ["*"]},
        },
    )
    return {"root": fixture, "env": env, "secret": secret}


def commit_fixture(path: Path, env: dict) -> None:
    output = path.parent.parent
    run_command(["git", "-C", str(path), "add", "-A"], output, path.name + "-git-add", env=env)
    run_command(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Pilot",
            "-c",
            "user.email=pilot@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Synthetic pilot input",
        ],
        output,
        path.name + "-git-commit",
        env=env,
    )


class OwnedProcess:
    def __init__(self, argv: list[str], root: Path, env: dict, label: str):
        self.log = (root / f"{label}.log").open("wb")
        self.proc = subprocess.Popen(argv, cwd=root, env=env, stdout=self.log, stderr=self.log)
        self.children = {}
        self.peak_rss_mib = 0.0
        self.exit_seconds = None
        self.survivors = []

    def observe(self):
        try:
            owner = psutil.Process(self.proc.pid)
            children = owner.children(recursive=True)
            for child in children:
                self.children[child.pid] = child
            rss = owner.memory_info().rss + sum(child.memory_info().rss for child in children)
            self.peak_rss_mib = max(self.peak_rss_mib, rss / 1024**2)
        except psutil.Error:
            pass

    def stop(self):
        self.observe()
        started = time.monotonic()
        try:
            if self.proc.poll() is None:
                self.proc.send_signal(signal.SIGTERM)
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            self.exit_seconds = time.monotonic() - started
            self.survivors = [
                child.pid
                for child in self.children.values()
                if child.is_running() and child.status() != psutil.STATUS_ZOMBIE
            ]
        finally:
            for child in self.children.values():
                try:
                    if child.is_running():
                        child.kill()
                except psutil.Error:
                    pass
            psutil.wait_procs(list(self.children.values()), timeout=2)
            self.log.close()


def tool_payload(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict) and "content" in value:
        text = "".join(item.get("text", "") for item in value["content"])
        return json.loads(text)
    return value


@asynccontextmanager
async def gateway(fixture: dict, label: str):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    root, env = fixture["root"], fixture["env"]
    port = free_port()
    owner = OwnedProcess(
        [
            shutil.which("pmcp") or "pmcp",
            "-p",
            str(root / "project"),
            "-c",
            str(root / "project" / ".mcp.json"),
            "--policy",
            str(root / "policy.json"),
            "--lock-dir",
            str(root / "locks"),
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--request-timeout",
            "120",
            "--log-level",
            "warn",
        ],
        root,
        env,
        label,
    )
    fixture.setdefault("processes", []).append(owner)
    try:
        async with httpx.AsyncClient(trust_env=False) as http:
            for _ in range(300):
                if owner.proc.poll() is not None:
                    raise PilotRefused("pmcp_startup_failed")
                try:
                    if (
                        await http.get(f"http://127.0.0.1:{port}/health", timeout=1)
                    ).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise PilotRefused("pmcp_startup_timeout")
        async with streamablehttp_client(
            f"http://127.0.0.1:{port}/mcp",
            httpx_client_factory=lambda **kwargs: httpx.AsyncClient(trust_env=False, **kwargs),
        ) as (read, write, _):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=120)
            ) as client:
                await client.initialize()
                yield client, owner
    finally:
        await asyncio.to_thread(owner.stop)


async def discover(client) -> dict:
    provision = tool_payload(
        await client.call_tool("gateway.provision", {"server_name": "index-it-mcp"})
    )
    if not provision.get("ok"):
        raise PilotRefused("provision_failed")
    catalog = tool_payload(
        await client.call_tool(
            "gateway.catalog_search", {"filters": {"server": "index-it-mcp"}, "limit": 100}
        )
    )
    tools = {row["tool_name"]: row["tool_id"] for row in catalog["results"]}
    if not {"search_code", "symbol_lookup", "reindex", "get_status", "handshake"} <= tools.keys():
        raise PilotRefused("catalog_incomplete")
    return tools


async def invoke(client, tools: dict, name: str, arguments: dict):
    envelope = tool_payload(
        await client.call_tool(
            "gateway.invoke",
            {
                "tool_id": tools[name],
                "arguments": arguments,
                "options": {"timeout_ms": 120000, "max_output_chars": 100000},
            },
        )
    )
    if not envelope.get("ok") or envelope.get("truncated"):
        raise PilotRefused("gateway_invoke_failed:" + name)
    return tool_payload(envelope["result"])


async def offline(root: Path, manifest: dict) -> dict:
    fixture = create_fixture(root, manifest, label="offline")
    goals = {"installed_identity": True}
    cases = []

    def checkpoint():
        result = {
            "kind": "offline",
            "source": manifest["source"],
            "wheel_sha256": manifest["wheel_sha256"],
            "manifest_sha256": digest_json(manifest),
            "goals": dict(goals),
            "shutdown_seconds": [p.exit_seconds for p in fixture.get("processes", [])]
            + [case["exit_seconds"] for case in cases],
            "surviving_children": [pid for p in fixture.get("processes", []) for pid in p.survivors]
            + [pid for case in cases for pid in case["surviving_children"]],
            "lifecycle": list(cases),
            "peak_rss_mib": max((p.peak_rss_mib for p in fixture.get("processes", [])), default=0),
        }
        write_json(root / "offline.partial.json", result)
        return result

    for attempt in range(2):
        async with gateway(fixture, f"pmcp-{attempt}") as (client, owner):
            tools = await discover(client)
            goals["provisioning"] = True
            denied = await invoke(client, tools, "get_status", {})
            if denied.get("code") != "handshake_required":
                raise PilotRefused("handshake_not_enforced")
            authenticated = await invoke(client, tools, "handshake", {"secret": fixture["secret"]})
            if authenticated.get("authenticated") is not True:
                raise PilotRefused("handshake_failed")
            goals["handshake"] = True
            for repo, symbol in (("ledger", "available_balance"), ("catalog", "find_product")):
                path = str(fixture["root"] / "repos" / repo)
                if attempt == 0:
                    rebuilt = await invoke(client, tools, "reindex", {"repository": path})
                    if "error" in rebuilt:
                        raise PilotRefused("reindex_failed")
                found = await invoke(
                    client,
                    tools,
                    "search_code",
                    {"repository": path, "query": symbol, "semantic": False},
                )
                if not (found if isinstance(found, list) else found.get("results")):
                    raise PilotRefused("query_failed")
                matched = await invoke(
                    client, tools, "symbol_lookup", {"repository": path, "symbol": symbol}
                )
                if matched.get("symbol", matched.get("name")) != symbol:
                    raise PilotRefused("symbol_failed")
            goals["two_repositories"] = True
            missing = await invoke(
                client,
                tools,
                "search_code",
                {"repository": path, "query": "absent_739152", "semantic": False},
            )
            if (missing if isinstance(missing, list) else missing.get("results")) != [] or (
                isinstance(missing, dict) and missing.get("code")
            ):
                raise PilotRefused("no_match_failed")
            goals["no_match"] = True
            sibling = await invoke(
                client,
                tools,
                "search_code",
                {
                    "repository": str(fixture["root"] / "repos" / "ledger-sibling"),
                    "query": "available_balance",
                },
            )
            if (
                sibling.get("code") != "index_unavailable"
                or sibling.get("safe_fallback") != "native_search"
            ):
                raise PilotRefused("sibling_refusal_failed")
            goals["sibling_refusal"] = True
            if attempt == 0:
                repo_path = fixture["root"] / "repos" / "catalog"
                run_command(
                    ["git", "-C", str(repo_path), "checkout", "-b", "unsupported-pilot"],
                    fixture["root"],
                    "branch-switch",
                    env=fixture["env"],
                )
                try:
                    wrong = await invoke(
                        client,
                        tools,
                        "search_code",
                        {"repository": str(repo_path), "query": "find_product"},
                    )
                    if wrong.get("readiness", {}).get("state") != "wrong_branch":
                        raise PilotRefused("wrong_branch_refusal_failed")
                    goals["wrong_branch_refusal"] = True
                finally:
                    run_command(
                        ["git", "-C", str(repo_path), "checkout", "main"],
                        fixture["root"],
                        "branch-restore",
                        env=fixture["env"],
                    )
                source = repo_path / "catalog.py"
                source.write_text(source.read_text() + "\n# Synthetic committed revision.\n")
                commit_fixture(repo_path, fixture["env"])
                stale = await invoke(
                    client,
                    tools,
                    "search_code",
                    {"repository": str(repo_path), "query": "find_product"},
                )
                if stale.get("readiness", {}).get("state") != "stale_commit":
                    raise PilotRefused("stale_refusal_failed")
                goals["stale_refusal"] = True
                refreshed = await invoke(client, tools, "reindex", {"repository": str(repo_path)})
                if "error" in refreshed:
                    raise PilotRefused("stale_rebuild_failed")
            before = set(
                child.pid for child in psutil.Process(owner.proc.pid).children(recursive=True)
            )
            again = tool_payload(
                await client.call_tool("gateway.provision", {"server_name": "index-it-mcp"})
            )
            after = set(
                child.pid for child in psutil.Process(owner.proc.pid).children(recursive=True)
            )
            if again.get("status") != "already_running" or before != after:
                raise PilotRefused("repeat_provisioning_failed")
            goals["repeat_provisioning"] = True
            owner.observe()
        if attempt == 1:
            goals["reconnect"] = True
        checkpoint()
    lifecycle_root = root / "lifecycle"
    lifecycle_root.mkdir()
    for script in ("installed_runtime_smoke.py", "safety_runtime_smoke.py"):
        shutil.copyfile(root / script, lifecycle_root / script)
    python = manifest["installed"]["python"]
    entrypoint = str(Path(python).parent / "index-it-mcp")
    run_command(
        [
            python,
            "-I",
            str(lifecycle_root / "installed_runtime_smoke.py"),
            "--mode",
            "prepare",
            "--root",
            str(lifecycle_root),
            "--entrypoint",
            entrypoint,
        ],
        lifecycle_root,
        "prepare",
    )
    output = run_command(
        [
            python,
            "-I",
            str(lifecycle_root / "safety_runtime_smoke.py"),
            "--root",
            str(lifecycle_root),
            "--entrypoint",
            entrypoint,
        ],
        lifecycle_root,
        "lifecycle",
    )
    lifecycle = json.loads(output.strip().splitlines()[-1])
    cases = lifecycle["cases"]
    if len(cases) != 6 or any(not case["metrics_ports"] for case in cases):
        raise PilotRefused("installed_metrics_lifecycle_missing")
    goals["installed_lifecycle"] = True
    checkpoint()
    # A separate owned listener occupies the configured port; startup must not steal it.
    collision = create_fixture(root, manifest, label="metrics-collision")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", int(collision["env"]["MCP_METRICS_PORT"])))
        listener.listen(1)
        async with gateway(collision, "pmcp-collision") as (client, owner):
            denied = tool_payload(
                await client.call_tool("gateway.provision", {"server_name": "index-it-mcp"})
            )
            if denied.get("ok") is not False or denied.get("status") != "failed":
                raise PilotRefused("metrics_collision_not_refused")
            if not listener.getsockname()[1]:
                raise PilotRefused("metrics_listener_lost")
            owner.observe()
        if listener.fileno() < 0:
            raise PilotRefused("metrics_listener_closed")
    async with gateway(collision, "pmcp-collision-recovery") as (client, owner):
        tools = await discover(client)
        await invoke(client, tools, "handshake", {"secret": collision["secret"]})
        status = await invoke(client, tools, "get_status", {})
        if "repositories" not in status:
            raise PilotRefused("metrics_recovery_status_failed")
        async with httpx.AsyncClient(trust_env=False) as http:
            metrics = await http.get(
                f"http://127.0.0.1:{collision['env']['MCP_METRICS_PORT']}/metrics"
            )
            if metrics.status_code != 200 or "mcp_tool_calls_total" not in metrics.text:
                raise PilotRefused("metrics_recovery_listener_failed")
        owner.observe()
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", int(collision["env"]["MCP_METRICS_PORT"])))
    goals["metrics_contention"] = True
    fixture["processes"].extend(collision["processes"])
    for directory, secret in (
        (fixture["root"], fixture["secret"]),
        (collision["root"], collision["secret"]),
    ):
        if any(secret in log.read_text(errors="replace") for log in directory.glob("*.log")):
            raise PilotRefused("credential_log_exposure")
    goals["privacy"] = True
    result = checkpoint()
    validate_receipt(result, manifest, "offline")
    write_json(root / "offline.json", result)
    return result


async def browser_session(root: Path, manifest: dict, inspector: Path) -> dict:
    package = json.loads((inspector.resolve().parents[3] / "package.json").read_text())
    if (
        package.get("name") != "@modelcontextprotocol/inspector"
        or package.get("version") != "2.6.0"
    ):
        raise PilotRefused("inspector_identity_mismatch")
    fixture = create_fixture(root, manifest, label="browser")
    directory = fixture["root"]
    processes = []
    result = {
        "source": manifest["source"],
        "wheel_sha256": manifest["wheel_sha256"],
        "manifest_sha256": digest_json(manifest),
        "inspector_version": package["version"],
        "inspector_entrypoint_sha256": digest_file(inspector),
        "session_started": False,
    }
    try:
        async with gateway(fixture, "pmcp-browser") as (client, owner):
            tools = await discover(client)
            await invoke(client, tools, "handshake", {"secret": fixture["secret"]})
            for repo in ("ledger", "catalog"):
                indexed = await invoke(
                    client, tools, "reindex", {"repository": str(directory / "repos" / repo)}
                )
                if "error" in indexed:
                    raise PilotRefused("browser_fixture_index_failed")
            argv = owner.proc.args
            pmcp_port = argv[argv.index("--port") + 1]
            admin_port, inspector_port = free_port(), free_port()
            env = dict(fixture["env"])
            env["MCP_METRICS_PORT"] = str(free_port())
            processes.append(
                OwnedProcess(
                    manifest["uvx_prefix"]
                    + [
                        "uvicorn",
                        "mcp_server.gateway:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(admin_port),
                        "--log-level",
                        "warning",
                        "--no-access-log",
                    ],
                    directory,
                    env,
                    "admin-browser",
                )
            )
            inspector_env = dict(fixture["env"])
            inspector_env.update(
                {
                    "CLIENT_PORT": str(inspector_port),
                    "MCP_SANDBOX_PORT": str(free_port()),
                    "HOST": "127.0.0.1",
                    "MCP_AUTO_OPEN_ENABLED": "false",
                    "MCP_INSPECTOR_API_TOKEN": secrets.token_urlsafe(36),
                }
            )
            processes.append(
                OwnedProcess(
                    [
                        shutil.which("node") or "node",
                        str(inspector),
                        "--web",
                        "--transport",
                        "http",
                        "--server-url",
                        f"http://127.0.0.1:{pmcp_port}/mcp",
                    ],
                    directory,
                    inspector_env,
                    "inspector-browser",
                )
            )
            async with httpx.AsyncClient(trust_env=False) as http:
                for _ in range(300):
                    if any(process.proc.poll() is not None for process in processes):
                        raise PilotRefused("browser_surface_start_failed")
                    try:
                        schema = await http.get(f"http://127.0.0.1:{admin_port}/openapi.json")
                        page = await http.get(f"http://127.0.0.1:{inspector_port}/")
                        if schema.status_code == page.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise PilotRefused("browser_surface_start_timeout")
                login = await http.post(
                    f"http://127.0.0.1:{admin_port}/api/v1/auth/login",
                    json={"username": "admin", "password": env["DEFAULT_ADMIN_PASSWORD"]},
                )
                if login.status_code != 200:
                    raise PilotRefused("browser_admin_auth_failed")
                write_json(directory / "auth.json", {"token": login.json()["access_token"]})
                write_json(directory / "openapi.json", schema.json())
            control = {
                **result,
                "pmcp_url": f"http://127.0.0.1:{pmcp_port}/mcp",
                "admin_url": f"http://127.0.0.1:{admin_port}/docs",
                "inspector_url": f"http://127.0.0.1:{inspector_port}/",
                "tools": tools,
                "root": str(directory),
            }
            write_json(directory / "control.json", control)
            print(json.dumps(control), flush=True)
            result["session_started"] = True
            started = time.monotonic()
            while time.monotonic() - started < 300 and not (directory / "stop").exists():
                for process in processes + [owner]:
                    process.observe()
                await asyncio.sleep(0.1)
            result["explicit_stop"] = (directory / "stop").is_file()
    finally:
        for process in reversed(processes):
            await asyncio.to_thread(process.stop)
        all_processes = processes + fixture.get("processes", [])
        result.update(
            {
                "shutdown_seconds": [p.exit_seconds for p in all_processes],
                "surviving_children": [pid for p in all_processes for pid in p.survivors],
                "peak_rss_mib": sum(p.peak_rss_mib for p in all_processes),
            }
        )
        write_json(directory / "session.json", result)
    return result


def verify_saved_receipt(root: Path, manifest: dict, kind: str) -> dict:
    result = json.loads((root / f"{kind}.json").read_text())
    validate_receipt(result, manifest, kind)
    required_roles = {
        "browser": {
            "inspector_screenshot",
            "admin_screenshot",
            "browser_actions",
            "browser_session",
        },
        "live": {"allowance_ledger", "runtime_provenance", "runtime_metadata", "workload"},
    }[kind]
    artifacts = result.get("artifacts", [])
    if not required_roles <= {item.get("role") for item in artifacts}:
        raise PilotRefused("receipt_artifacts_incomplete")
    for item in artifacts:
        path = root / item["path"]
        if (
            Path(item["path"]).is_absolute()
            or not path.resolve().is_relative_to(root.resolve())
            or not path.is_file()
            or digest_file(path) != item.get("sha256")
        ):
            raise PilotRefused("receipt_artifact_mismatch")
    if kind == "browser":
        from PIL import Image

        evidence = {}
        for role in ("browser_session", "browser_actions"):
            paths = [root / item["path"] for item in artifacts if item["role"] == role]
            if len(paths) != 1:
                raise PilotRefused("browser_artifact_ambiguous")
            evidence[role] = json.loads(paths[0].read_text())
            for key in ("source", "wheel_sha256", "manifest_sha256"):
                if evidence[role].get(key) != result[key]:
                    raise PilotRefused("browser_artifact_binding_mismatch")
        session = evidence["browser_session"]
        if session.get("session_started") is not True:
            raise PilotRefused("browser_session_not_started")
        validate_receipt({**result, **session}, manifest, "browser")
        actions = evidence["browser_actions"].get("events", [])
        for goal in GOALS["browser"]:
            matching = [event for event in actions if event.get("goal") == goal]
            if not matching or any(
                event.get("ok") is not True or not event.get("observed") for event in matching
            ):
                raise PilotRefused("browser_actions_incomplete")
        screenshot_paths = [
            root / item["path"] for item in artifacts if item["role"].endswith("_screenshot")
        ]
        if len(set(screenshot_paths)) < 2:
            raise PilotRefused("browser_screenshots_not_distinct")
        for path in screenshot_paths:
            try:
                with Image.open(path) as picture:
                    if picture.format != "PNG" or min(picture.size) < 100:
                        raise PilotRefused("browser_screenshot_invalid")
                    picture.verify()
            except (OSError, ValueError):
                raise PilotRefused("browser_screenshot_invalid") from None
    else:
        _verify_live_records(root, manifest, result)
    return result


def _verify_live_records(root: Path, manifest: dict, result: dict, *, rehearsal=False) -> None:
    if __package__:
        from .v13_pilot_budget import ENDPOINTS, BudgetDenied, BudgetLedger
        from .v13_pilot_estimate import REQUEST_ENVELOPES, SYNTHETIC_CORPUS
    else:
        from v13_pilot_budget import ENDPOINTS, BudgetDenied, BudgetLedger
        from v13_pilot_estimate import REQUEST_ENVELOPES, SYNTHETIC_CORPUS

    try:
        paths = {}
        for role in ("allowance_ledger", "runtime_provenance", "runtime_metadata", "workload"):
            matches = [root / item["path"] for item in result["artifacts"] if item["role"] == role]
            if len(matches) != 1:
                raise PilotRefused("live_artifact_ambiguous")
            paths[role] = matches[0]
        if len(set(paths.values())) != 4 or paths["allowance_ledger"].name != "ledger.sqlite":
            raise PilotRefused("live_artifact_invalid")
        ledger = BudgetLedger(
            paths["allowance_ledger"].parent, digest_json(manifest), read_only=True
        )
        snapshot = ledger.snapshot()
        recorded = {
            key: value for key, value in result["budget"].items() if key != "elapsed_seconds"
        }
        if recorded != snapshot or any(
            row["outcome"] in {"inflight", "transport_unknown"} for row in snapshot["requests"]
        ):
            raise PilotRefused("live_accounting_mismatch")
        elapsed = result["budget"]["elapsed_seconds"]
        if not snapshot["requests"] or elapsed < max(
            snapshot["last_wall"] - snapshot["started_wall"],
            snapshot["last_monotonic"] - snapshot["started_monotonic"],
        ):
            raise PilotRefused("live_accounting_incomplete")
        for request_class, envelope in REQUEST_ENVELOPES.items():
            rows = [row for row in snapshot["requests"] if row["request_class"] == request_class]
            if not 0 < len(rows) <= envelope["requests"]:
                raise PilotRefused("live_envelope_mismatch")
            for row in rows:
                expected_roles = (
                    {"embedding", "enrichment"}
                    if request_class == "provenance_probe"
                    else {"enrichment" if request_class == "summary" else "embedding"}
                )
                if (
                    row["role"] not in expected_roles
                    or type(row["input_units"]) is not int
                    or not 0
                    < row["input_units"]
                    <= envelope["max_input_utf8_bytes"] + envelope["framing_input_units"]
                    or row["finished_wall"] is None
                    or not snapshot["started_wall"]
                    <= row["started_wall"]
                    <= row["finished_wall"]
                    <= snapshot["last_wall"]
                ):
                    raise PilotRefused("live_request_invalid")
        if any(row["request_class"] not in REQUEST_ENVELOPES for row in snapshot["requests"]):
            raise PilotRefused("live_request_class_invalid")

        workload = json.loads(paths["workload"].read_text())
        metadata = json.loads(paths["runtime_metadata"].read_text())
        if (
            result.get("rehearsal") is not rehearsal
            or result.get("workflow_completed") is not True
            or workload.get("rehearsal") is not rehearsal
            or workload["manifest_sha256"] != digest_json(manifest)
            or workload["corpus"] != SYNTHETIC_CORPUS
            or workload["request_envelopes"] != REQUEST_ENVELOPES
            or workload["query_texts"] != QUERY_TEXTS
            or workload["measured_queries_per_class_per_repository"] != 20
            or metadata["workload_sha256"] != digest_json(workload)
            or metadata["qdrant_image"] != QDRANT_IMAGE
            or (not rehearsal and metadata["endpoints"] != ENDPOINTS)
        ):
            raise PilotRefused("live_workload_mismatch")
        for role in ENDPOINTS:
            if not isinstance(metadata["models"][role], str) or not metadata["models"][role]:
                raise PilotRefused("live_model_missing")
        if type(metadata["dimension"]) is not int or metadata["dimension"] <= 0:
            raise PilotRefused("live_dimension_invalid")

        samples, intervals = result["samples"], result["index_intervals"]
        if len(samples) != 120 or not intervals:
            raise PilotRefused("live_observations_incomplete")
        for observation in samples + intervals:
            if (
                observation["repository"] not in SYNTHETIC_CORPUS
                or any(
                    type(observation[key]) not in (int, float)
                    or not math.isfinite(observation[key])
                    for key in ("started", "ended")
                )
                or not snapshot["started_monotonic"]
                <= observation["started"]
                <= observation["ended"]
                <= snapshot["started_monotonic"] + elapsed + 0.001
            ):
                raise PilotRefused("live_interval_invalid")
        if any(type(interval["success"]) is not bool for interval in intervals):
            raise PilotRefused("live_interval_invalid")
        for sample in samples:
            if (
                sample["kind"] not in {"symbol", "lexical", "semantic"}
                or sample["ready_success"] is not True
                or not math.isclose(
                    sample["milliseconds"],
                    (sample["ended"] - sample["started"]) * 1000,
                    abs_tol=0.000001,
                )
            ):
                raise PilotRefused("live_sample_invalid")
        for kind in ("symbol", "lexical", "semantic"):
            selected = [sample for sample in samples if sample["kind"] == kind]
            if any(
                sum(sample["repository"] == repo for sample in selected) != 20
                for repo in SYNTHETIC_CORPUS
            ):
                raise PilotRefused("live_sample_count_mismatch")
            latencies = [sample["milliseconds"] for sample in selected]
            contention = sum(
                any(
                    interval["success"]
                    and interval["repository"] != sample["repository"]
                    and interval["started"]
                    <= sample["started"]
                    <= sample["ended"]
                    <= interval["ended"]
                    for interval in intervals
                )
                for sample in selected
            )
            if (
                result["latencies_ms"][kind] != latencies
                or result["contention_successes"][kind] != contention
            ):
                raise PilotRefused("live_observation_summary_mismatch")

        records = json.loads(paths["runtime_provenance"].read_text())["repositories"]
        if len(records) != 2 or {record["repository"] for record in records} != set(
            SYNTHETIC_CORPUS
        ):
            raise PilotRefused("live_provenance_incomplete")
        for record in records:
            sentinel = record["collection_manifest"]
            provenance = record["embedding_provenance"]
            point_ids = record["point_ids"]
            filename = "bookkeeping.py" if record["repository"] == "ledger" else "catalog.py"
            if (
                record["attested"] is not True
                or not record["generation"]
                or not isinstance(point_ids, list)
                or not point_ids
                or not all(isinstance(value, str) and value.isdecimal() for value in point_ids)
                or len(set(point_ids)) != len(point_ids)
                or len(point_ids) != record["point_count"]
                or sentinel["indexed_commit"] != record["commit"]
                or sentinel["point_set_id"]
                != hashlib.sha256("\n".join(sorted(point_ids)).encode()).hexdigest()
                or sentinel["corpus_sha256"] != hashlib.sha256(filename.encode()).hexdigest()
                or not sentinel["profile_fingerprint"]
                or sentinel["provenance_version"] != "collection-provenance.v1"
                or sentinel["provider_id"] != metadata["models"]["embedding"]
                or provenance["served_model_id"]["source"] != "reported"
                or provenance["served_model_id"]["value"] != metadata["models"]["embedding"]
                or provenance["dimension"]["source"] != "reported"
                or provenance["dimension"]["value"] != metadata["dimension"]
                or provenance["model_revision"]["source"] not in {"reported", "declared"}
                or provenance["model_revision"]["value"] != metadata["immutable_revision"]
            ):
                raise PilotRefused("live_provenance_mismatch")
    except (BudgetDenied, sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
        raise PilotRefused("live_records_invalid:" + type(exc).__name__) from None


def select_model(catalog: dict, preferred: str) -> str:
    ids = {row.get("id") for row in catalog.get("data", []) if isinstance(row, dict)}
    ids = {value for value in ids if isinstance(value, str) and value}
    if preferred in ids:
        return preferred
    if len(ids) == 1:
        return ids.pop()
    raise PilotRefused("model_catalog_ambiguous")


def rehearsal_provider() -> ThreadingHTTPServer:
    """Loopback protocol fixture, never retrieval-quality or performance evidence."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.respond({"data": [{"id": "synthetic-rehearsal"}]})

        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/v1/embeddings":
                inputs = value["input"]
                if isinstance(inputs, str):
                    inputs = [inputs]
                self.respond(
                    {
                        "object": "list",
                        "model": "synthetic-rehearsal",
                        "data": [
                            {"object": "embedding", "index": i, "embedding": [1.0] + [0.0] * 7}
                            for i in range(len(inputs))
                        ],
                        "usage": {"prompt_tokens": 1, "total_tokens": 1},
                    }
                )
            else:
                prompt = "\n".join(item["content"] for item in value["messages"])
                ids = [
                    line.removeprefix("chunk_id: ").strip()
                    for line in prompt.splitlines()
                    if line.startswith("chunk_id: ")
                ]
                content = (
                    json.dumps(
                        {
                            "summaries": [
                                {
                                    "chunk_id": key,
                                    "summary": "This function processes synthetic records. It returns the computed result.",
                                }
                                for key in ids
                            ]
                        }
                    )
                    if ids
                    else "This function processes synthetic records. It returns the computed result."
                )
                self.respond(
                    {
                        "id": "rehearsal",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "synthetic-rehearsal",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                )

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


async def runtime_provenance(fixture: dict, qdrant_url: str) -> list[dict]:
    registry = json.loads((fixture["root"] / "registry.json").read_text())
    reported = json.loads((fixture["root"] / "runtime-metadata.json").read_text())
    records = []
    async with httpx.AsyncClient(trust_env=False) as http:
        for info in registry.values():
            database = Path(info["index_path"])
            if not database.resolve().is_relative_to(fixture["root"]):
                raise PilotRefused("provenance_path_outside_fixture")
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                points = db.execute("SELECT point_id, collection FROM semantic_points").fetchall()
            collections = {row[1] for row in points}
            if len(collections) != 1:
                raise PilotRefused("provenance_points_missing")
            collection = collections.pop()
            response = await http.post(
                f"{qdrant_url}/collections/{collection}/points/scroll",
                json={"limit": 100, "with_payload": True, "with_vector": False},
            )
            response.raise_for_status()
            resident = response.json()["result"]["points"]
            sentinels = [
                p["payload"] for p in resident if p.get("payload", {}).get("__provenance__")
            ]
            actual_ids = {
                str(p["id"]) for p in resident if not p.get("payload", {}).get("__provenance__")
            }
            expected_ids = {str(row[0]) for row in points}
            if len(sentinels) != 1 or actual_ids != expected_ids:
                raise PilotRefused("provenance_point_set_mismatch")
            sentinel = sentinels[0]
            expected_set = hashlib.sha256("\n".join(sorted(expected_ids)).encode()).hexdigest()
            expected_paths = sorted(
                path.name for path in (fixture["root"] / "repos" / info["name"]).glob("*.py")
            )
            expected_corpus = hashlib.sha256("\n".join(expected_paths).encode()).hexdigest()
            if (
                sentinel.get("indexed_commit") != info["last_indexed_commit"]
                or sentinel.get("point_set_id") != expected_set
                or sentinel.get("corpus_sha256") != expected_corpus
                or not sentinel.get("profile_fingerprint")
            ):
                raise PilotRefused("provenance_binding_mismatch")
            metadata_paths = list(database.with_suffix(".semantic").rglob(".index_metadata.json"))
            profiles = [
                json.loads(path.read_text()).get("semantic_profiles", {}).get("pilot", {})
                for path in metadata_paths
            ]
            profiles = [p for p in profiles if p.get("collection_name") == collection]
            if len(profiles) != 1 or profiles[0].get("attested") is not True:
                raise PilotRefused("provenance_attestation_missing")
            derived = profiles[0].get("provenance") or {}
            if (
                derived.get("served_model_id", {}).get("source") != "reported"
                or derived.get("served_model_id", {}).get("value")
                != reported["models"]["embedding"]
                or derived.get("dimension", {}).get("source") != "reported"
                or derived.get("dimension", {}).get("value") != reported["dimension"]
            ):
                raise PilotRefused("provenance_reported_identity_mismatch")
            records.append(
                {
                    "repository": info["name"],
                    "commit": info["last_indexed_commit"],
                    "generation": info["index_generation"],
                    "point_count": len(points),
                    "point_ids": sorted(expected_ids),
                    "attested": profiles[0]["attested"],
                    "collection_manifest": sentinel,
                    "embedding_provenance": profiles[0].get("provenance"),
                }
            )
    return records


async def inference_pilot(root: Path, manifest: dict, *, rehearsal: bool) -> dict:
    from v13_pilot_budget import ENDPOINTS, BudgetLedger, LocalForwarder
    from v13_pilot_estimate import REQUEST_ENVELOPES, SYNTHETIC_CORPUS

    if not rehearsal:
        validate_receipt(json.loads((root / "offline.json").read_text()), manifest, "offline")
        verify_saved_receipt(root, manifest, "browser")
        previous = json.loads((root / "rehearsal.json").read_text())
        if previous.get("manifest_sha256") != digest_json(manifest) or not previous.get(
            "workflow_completed"
        ):
            raise PilotRefused("rehearsal_required")
    label = "rehearsal" if rehearsal else "live"
    fixture = create_fixture(root, manifest, label=label)
    directory = fixture["root"]
    workload = {
        "corpus": SYNTHETIC_CORPUS,
        "request_envelopes": REQUEST_ENVELOPES,
        "measured_queries_per_class_per_repository": 20,
        "semantic_tool_attempt_limit": 48,
        "max_rebuilds_per_contention_window": 3,
        "query_texts": QUERY_TEXTS,
        "mutation": "append a synthetic function-body comment",
        "rename": "ledger/balance.py to ledger/bookkeeping.py",
        "delete": "catalog/catalog.py; old ready forbidden; restore and rebuild",
        "rehearsal": rehearsal,
        "manifest_sha256": digest_json(manifest),
    }
    write_json(directory / "workload.json", workload, exclusive=True)
    allowance = directory / "allowance" if rehearsal else root.parent / "v13-PILOT-allowance"
    ledger = None
    servers = []
    threads = []
    container = None
    sampling = None
    finished = asyncio.Event()
    result = {
        "kind": "live",
        "source": manifest["source"],
        "wheel_sha256": manifest["wheel_sha256"],
        "manifest_sha256": digest_json(manifest),
        "rehearsal": rehearsal,
        "goals": {},
        "samples": [],
        "index_intervals": [],
        "workflow_completed": False,
    }
    image = QDRANT_IMAGE

    def start_server(server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        servers.append(server)
        threads.append(thread)
        thread.start()

    async def sample():
        while not finished.is_set():
            for process in fixture.get("processes", []):
                process.observe()
            await asyncio.sleep(0.05)

    async def query(client, tools, repo, kind, *, measured=False):
        arguments = {"repository": repo}
        if kind == "symbol":
            arguments["symbol"] = "available_balance" if repo == "ledger" else "find_product"
            tool = "symbol_lookup"
        else:
            tool = "search_code"
            arguments.update(
                query=(
                    workload["query_texts"][repo]
                    if kind == "semantic"
                    else "available_balance" if repo == "ledger" else "find_product"
                ),
                semantic=kind == "semantic",
                limit=5,
            )
        started = time.monotonic()
        value = await invoke(client, tools, tool, arguments)
        ended = time.monotonic()
        expected_file = (
            "bookkeeping.py"
            if (directory / "repos/ledger/bookkeeping.py").is_file() and repo == "ledger"
            else "balance.py" if repo == "ledger" else "catalog.py"
        )
        valid = (
            not isinstance(value, dict) or not value.get("error")
        ) and expected_file in json.dumps(value)
        if isinstance(value, dict) and value.get("code"):
            valid = False
        if measured:
            result["samples"].append(
                {
                    "repository": repo,
                    "kind": kind,
                    "started": started,
                    "ended": ended,
                    "milliseconds": (ended - started) * 1000,
                    "ready_success": bool(valid),
                }
            )
        if not valid:
            raise PilotRefused("retrieval_failed:" + repo + ":" + kind)
        return value

    async def rebuild(client, tools, repo):
        started = time.monotonic()
        value = await invoke(client, tools, "reindex", {"repository": repo})
        result["index_intervals"].append(
            {
                "repository": repo,
                "started": started,
                "ended": time.monotonic(),
                "success": not value.get("error"),
            }
        )
        if value.get("error") or value.get("mutation_performed") is not True:
            write_json(directory / "reindex-failure.json", value)
            raise PilotRefused("semantic_rebuild_failed:" + repo)
        return value

    try:
        port = free_port()
        container = (
            await asyncio.to_thread(
                run_command,
                [
                    "docker",
                    "run",
                    "--rm",
                    "-d",
                    "-p",
                    f"127.0.0.1:{port}:6333",
                    "-e",
                    "QDRANT__TELEMETRY_DISABLED=true",
                    image,
                ],
                directory,
                "qdrant-start",
            )
        ).strip()
        if len(container) != 64 or any(c not in "0123456789abcdef" for c in container):
            raise PilotRefused("qdrant_owner_identity")
        qdrant_url = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(trust_env=False) as http:
            for _ in range(100):
                try:
                    if (await http.get(qdrant_url + "/readyz", timeout=1)).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise PilotRefused("qdrant_start_failed")
        endpoints = None
        if rehearsal:
            fake = rehearsal_provider()
            start_server(fake)
            endpoints = dict.fromkeys(
                ("embedding", "enrichment"), f"http://127.0.0.1:{fake.server_port}/v1"
            )
        BudgetLedger.initialize(allowance, digest_json(manifest))
        ledger = BudgetLedger(allowance, digest_json(manifest))
        token = secrets.token_urlsafe(36)
        guard = LocalForwarder(
            ledger,
            endpoints,
            envelopes=REQUEST_ENVELOPES,
            queries=tuple(workload["query_texts"].values()),
        ).server(token)
        start_server(guard)
        bases = {
            role: f"http://127.0.0.1:{guard.server_port}/{role}/v1"
            for role in ("embedding", "enrichment")
        }
        models = {}
        async with httpx.AsyncClient(
            trust_env=False, timeout=90, headers={"Authorization": "Bearer " + token}
        ) as http:
            for role, preferred in (
                ("embedding", "Qwen/Qwen3-Embedding-8B"),
                ("enrichment", "chat"),
            ):
                response = await http.get(bases[role] + "/models")
                if response.status_code != 200:
                    raise PilotRefused("model_catalog_unavailable:" + role)
                models[role] = select_model(response.json(), preferred)
            response = await http.post(
                bases["embedding"] + "/embeddings",
                json={
                    "model": models["embedding"],
                    "input": ["synthetic dimension probe"],
                    "encoding_format": "float",
                },
            )
            if response.status_code != 200:
                raise PilotRefused("dimension_probe_failed")
            vector = response.json()["data"][0]["embedding"]
            if (
                not isinstance(vector, list)
                or not vector
                or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector)
            ):
                raise PilotRefused("dimension_probe_invalid")
        profile = {
            "provider": "openai_compatible",
            "model_name": models["embedding"],
            "model_version": "unreported",
            "vector_dimension": len(vector),
            "distance_metric": "cosine",
            "normalization_policy": "provider-default",
            "chunk_schema_version": "1",
            "chunker_version": "4.0.0",
            "build_metadata": {
                "embedding_api_base": bases["embedding"],
                "openai_api_base": bases["embedding"],
                "embedding_api_key_env": "PILOT_GUARD_KEY",
                "openai_api_key_env": "PILOT_GUARD_KEY",
                "enrichment_api_base": bases["enrichment"],
                "enrichment_model_name": models["enrichment"],
                "enrichment_api_key_env": "PILOT_GUARD_KEY",
            },
        }
        fixture["env"].update(
            SEMANTIC_PROFILES_JSON=json.dumps({"pilot": profile}),
            SEMANTIC_DEFAULT_PROFILE="pilot",
            SEMANTIC_SEARCH_ENABLED="true",
            MCP_DEPLOYMENT_PROFILE="fleet_local",
            QDRANT_URL=qdrant_url,
            QDRANT_USE_SERVER="true",
            PILOT_GUARD_KEY=token,
        )
        config_path = directory / "project/.mcp.json"
        config = json.loads(config_path.read_text())
        config["mcpServers"]["index-it-mcp"]["env"] = fixture["env"]
        write_json(config_path, config)
        write_json(
            directory / "runtime-metadata.json",
            {
                "models": models,
                "dimension": len(vector),
                "immutable_revision": "unreported",
                "qdrant_image": image,
                "endpoints": endpoints or ENDPOINTS,
                "workload_sha256": digest_json(workload),
            },
        )
        sampling = asyncio.create_task(sample())
        async with gateway(fixture, "pmcp-inference") as (client, owner):
            tools = await discover(client)
            await invoke(client, tools, "handshake", {"secret": fixture["secret"]})
            for repo in SYNTHETIC_CORPUS:
                await rebuild(client, tools, repo)
            for target, indexing in (("ledger", "catalog"), ("catalog", "ledger")):
                for kind in ("symbol", "lexical", "semantic"):
                    await query(client, tools, target, kind)
                path = directory / "repos" / indexing / next(iter(SYNTHETIC_CORPUS[indexing]))
                path.write_text(path.read_text() + "    # Synthetic pilot modification.\n")
                commit_fixture(path.parent, fixture["env"])
                queries_finished = asyncio.Event()

                async def indexing_window():
                    for _ in range(3):
                        await rebuild(client, tools, indexing)
                        if queries_finished.is_set():
                            break

                async with asyncio.timeout(300):
                    task = asyncio.create_task(indexing_window())
                    try:
                        for _ in range(20):
                            for kind in ("symbol", "lexical", "semantic"):
                                await query(client, tools, target, kind, measured=True)
                    finally:
                        queries_finished.set()
                        await task
            source = directory / "repos/ledger/balance.py"
            source.rename(source.with_name("bookkeeping.py"))
            commit_fixture(source.parent, fixture["env"])
            await rebuild(client, tools, "ledger")
            renamed = await query(client, tools, "ledger", "semantic")
            if "balance.py" in json.dumps(renamed):
                raise PilotRefused("rename_retained_old_path")
            result["goals"]["rename"] = True
            await rebuild(client, tools, "ledger")
            await query(client, tools, "ledger", "semantic")
            result["goals"]["rebuild"] = True
            deleted = directory / "repos/catalog/catalog.py"
            content = deleted.read_text()
            deleted.unlink()
            commit_fixture(deleted.parent, fixture["env"])
            empty = await invoke(client, tools, "reindex", {"repository": "catalog"})
            stale = await invoke(
                client,
                tools,
                "search_code",
                {"repository": "catalog", "query": "find_product", "semantic": True},
            )
            if (
                stale.get("code") != "index_unavailable"
                or stale.get("safe_fallback") != "native_search"
            ):
                raise PilotRefused("delete_exposed_old_generation")
            write_json(directory / "delete-control.json", {"reindex": empty, "query": stale})
            deleted.write_text(content)
            commit_fixture(deleted.parent, fixture["env"])
            await rebuild(client, tools, "catalog")
            await query(client, tools, "catalog", "semantic")
            result["goals"]["delete"] = True
        async with gateway(fixture, "pmcp-restart") as (client, owner):
            tools = await discover(client)
            await invoke(client, tools, "handshake", {"secret": fixture["secret"]})
            await query(client, tools, "ledger", "semantic")
            await query(client, tools, "catalog", "semantic")
            result["goals"]["restart"] = True
            provenance = await runtime_provenance(fixture, qdrant_url)
            write_json(directory / "runtime-provenance.json", {"repositories": provenance})
        result["goals"].update(
            retrieval=True,
            provenance=True,
            synthetic_only=True,
            local_only=True,
            budget_enforced=True,
        )
        result["workflow_completed"] = True
    finally:
        finished.set()
        if sampling:
            await sampling
        for server in reversed(servers):
            await asyncio.to_thread(server.shutdown)
            server.server_close()
        for thread in threads:
            await asyncio.to_thread(thread.join, 5)
        if container:
            await asyncio.to_thread(
                run_command, ["docker", "stop", "--time", "5", container], directory, "qdrant-stop"
            )
        if ledger:
            budget = ledger.snapshot()
            budget["elapsed_seconds"] = (
                time.time() - budget["started_wall"] if budget["started_wall"] else 0
            )
            result["budget"] = budget
            write_json(directory / "budget.json", budget)
            (directory / "allowance-ledger").mkdir(mode=0o700)
            shutil.copyfile(
                allowance / "ledger.sqlite", directory / "allowance-ledger/ledger.sqlite"
            )
        processes = fixture.get("processes", [])
        result.update(
            shutdown_seconds=[p.exit_seconds for p in processes],
            surviving_children=[pid for p in processes for pid in p.survivors],
            peak_rss_mib=max((p.peak_rss_mib for p in processes), default=0),
        )
        result["latencies_ms"] = {
            kind: [
                s["milliseconds"]
                for s in result["samples"]
                if s["kind"] == kind and s["ready_success"]
            ]
            for kind in ("symbol", "lexical", "semantic")
        }
        result["contention_successes"] = {
            kind: sum(
                s["ready_success"]
                and s["kind"] == kind
                and any(
                    i["success"]
                    and i["repository"] != s["repository"]
                    and i["started"] <= s["started"]
                    and s["ended"] <= i["ended"]
                    for i in result["index_intervals"]
                )
                for s in result["samples"]
            )
            for kind in ("symbol", "lexical", "semantic")
        }
        result["goals"]["contention"] = all(
            n >= 20 for n in result["contention_successes"].values()
        )
        write_json(root / (label + ".partial.json"), result)
    if not rehearsal:
        validate_receipt(result, manifest, "live")
    result["artifacts"] = [
        {"role": role, "path": str(path.relative_to(root)), "sha256": digest_file(path)}
        for role, path in (
            ("runtime_provenance", directory / "runtime-provenance.json"),
            ("allowance_ledger", directory / "allowance-ledger/ledger.sqlite"),
            ("runtime_metadata", directory / "runtime-metadata.json"),
            ("workload", directory / "workload.json"),
        )
    ]
    _verify_live_records(root, manifest, result, rehearsal=rehearsal)
    write_json(root / (label + ".json"), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        required=True,
        choices=[
            "prepare",
            "offline",
            "browser",
            "live",
            "rehearsal",
            "verify-live",
            "verify-browser",
            "identity",
        ],
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--inspector", type=Path)
    args = parser.parse_args()
    if args.mode == "identity":
        print(json.dumps(installed_identity(args.wheel)))
        return
    root = args.root or REPO / ".phase-loop" / "runs" / (
        "v13-PILOT-" + source_identity()["source"][:12]
    )
    root = root.resolve()
    expected = REPO / ".phase-loop" / "runs"
    if root.parent != expected or not root.name.startswith("v13-PILOT-"):
        raise PilotRefused("scratch_root_outside_plan")
    if args.mode == "prepare":
        result = prepare(root)
    elif args.mode == "offline":
        result = asyncio.run(offline(root, load_manifest(root)))
    elif args.mode == "browser":
        if args.inspector is None:
            raise PilotRefused("inspector_entrypoint_required")
        result = asyncio.run(browser_session(root, load_manifest(root), args.inspector.resolve()))
    elif args.mode.startswith("verify-"):
        result = verify_saved_receipt(root, load_manifest(root), args.mode.removeprefix("verify-"))
    else:
        result = asyncio.run(
            inference_pilot(root, load_manifest(root), rehearsal=args.mode == "rehearsal")
        )
    print(json.dumps({"mode": args.mode, "root": str(root), "source": result["source"]}))


if __name__ == "__main__":
    try:
        main()
    except PilotRefused as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
