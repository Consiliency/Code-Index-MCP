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
import subprocess
import sys
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx
import psutil

REPO = Path(__file__).resolve().parents[1]
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
    if kind in {"offline", "live"}:
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
        "live": {"allowance_ledger", "runtime_provenance"},
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
        raise PilotRefused("proof_not_implemented:" + args.mode)
    print(json.dumps({"mode": args.mode, "root": str(root), "source": result["source"]}))


if __name__ == "__main__":
    try:
        main()
    except PilotRefused as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
