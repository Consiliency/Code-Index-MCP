#!/usr/bin/env python3
"""Release smoke checks against actual installed wheel and non-root image entrypoints."""

from __future__ import annotations

import argparse
import hashlib
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
PROBE = REPO / "scripts/installed_runtime_smoke.py"
SAFETY_PROBE = REPO / "scripts/safety_runtime_smoke.py"


def _run(
    cmd: list[str], *, cwd: Path = REPO, env: dict[str, str] | None = None, timeout: int = 300
) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, env=env, check=True, timeout=timeout)


def _venv_bin(root: Path, name: str) -> Path:
    return (
        root
        / ("Scripts" if os.name == "nt" else "bin")
        / (f"{name}.exe" if os.name == "nt" else name)
    )


def _clean_env(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(root),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def smoke_wheel() -> None:
    with tempfile.TemporaryDirectory(prefix="mcp-release-wheel-") as tmp:
        root = Path(tmp)
        dist, venv_dir, runtime = root / "dist", root / "venv", root / "runtime"
        runtime.mkdir()
        probe = runtime / PROBE.name
        shutil.copyfile(PROBE, probe)
        shutil.copyfile(SAFETY_PROBE, runtime / SAFETY_PROBE.name)
        _run(["uv", "build", "--wheel", "--out-dir", str(dist)], timeout=300)
        wheels = sorted(dist.glob("index_it_mcp-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("Expected exactly one built wheel")
        print(
            json.dumps(
                {
                    "wheel": wheels[0].name,
                    "sha256": hashlib.sha256(wheels[0].read_bytes()).hexdigest(),
                }
            ),
            flush=True,
        )
        requirements = root / "requirements.txt"
        _run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--no-hashes",
                "--output-file",
                str(requirements),
            ]
        )
        venv.EnvBuilder(with_pip=False).create(venv_dir)
        python = _venv_bin(venv_dir, "python")
        _run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "-r",
                str(requirements),
                str(wheels[0]),
            ],
            cwd=root,
            timeout=300,
        )
        env = _clean_env(root)
        for entrypoint in CANONICAL_ENTRYPOINTS:
            binary = _venv_bin(venv_dir, entrypoint)
            assert binary.is_file(), entrypoint
            _run([str(binary), "--help"], cwd=runtime, env=env)
        for entrypoint in REMOVED_ENTRYPOINTS:
            assert not _venv_bin(venv_dir, entrypoint).exists(), entrypoint
        for mode in ("schema", "prepare", "python", "stdio"):
            _run(
                [
                    str(python),
                    "-I",
                    str(probe),
                    "--root",
                    str(runtime),
                    "--entrypoint",
                    str(_venv_bin(venv_dir, "index-it-mcp")),
                    "--mode",
                    mode,
                ],
                cwd=runtime,
                env=env,
                timeout=300,
            )
        _run(
            [
                str(python),
                "-I",
                str(runtime / SAFETY_PROBE.name),
                "--root",
                str(runtime),
                "--entrypoint",
                str(_venv_bin(venv_dir, "index-it-mcp")),
            ],
            cwd=runtime,
            env=env,
            timeout=300,
        )


def smoke_stdio() -> None:
    """STDIO is tested from the installed wheel, never from test fixture dispatchers."""
    smoke_wheel()


def _poll_health(port: int, *, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                if json.load(response).get("status") == "healthy":
                    return
        except (OSError, URLError, json.JSONDecodeError) as exc:
            last_error = type(exc).__name__
        time.sleep(1)
    raise RuntimeError(f"Container health timeout ({last_error})")


def smoke_container() -> None:
    if shutil.which("docker") is None:
        raise RuntimeError("docker is required for --container smoke")
    _run(
        ["docker", "build", "-f", "docker/dockerfiles/Dockerfile.production", "-t", IMAGE, "."],
        timeout=600,
    )
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE], text=True, timeout=30
    ).strip()
    print(json.dumps({"image": IMAGE, "image_id": image_id}), flush=True)
    with tempfile.TemporaryDirectory(prefix="mcp-release-container-") as tmp:
        root = Path(tmp)
        root.chmod(0o777)
        shutil.copyfile(PROBE, root / PROBE.name)
        shutil.copyfile(SAFETY_PROBE, root / SAFETY_PROBE.name)
        mount = ["-v", f"{root}:/smoke"]
        probe = ["python", "-I", "/smoke/installed_runtime_smoke.py", "--root", "/smoke"]
        # The image's configured USER owns the fixture; no root override or fake services.
        for mode in ("schema", "prepare", "python"):
            _run(["docker", "run", "--rm", *mount, image_id, *probe, "--mode", mode])
        port = _free_port()
        env = {
            "HOME": "/smoke/home",
            "MCP_REPO_REGISTRY": "/smoke/registry.json",
            "MCP_INDEX_STORAGE_PATH": "/smoke/indexes",
            "MCP_ALLOWED_ROOTS": "/smoke",
            "MCP_WORKSPACE_ROOT": "/smoke/fixture",
            "SEMANTIC_SEARCH_ENABLED": "false",
            "MCP_AUTO_INDEX": "false",
            "MCP_SKIP_PLUGIN_PREINDEX": "true",
            "MCP_METRICS_PORT": "0",
            "MCP_ENVIRONMENT": "development",
            "JWT_SECRET_KEY": "synthetic-smoke-jwt-key-00000000000000",
            "DEFAULT_ADMIN_PASSWORD": "synthetic-smoke-admin-password-00000000",
            "DEFAULT_ADMIN_EMAIL": "admin@localhost",
            "CORS_ORIGINS": "http://localhost",
        }
        args = [part for key, value in env.items() for part in ("-e", f"{key}={value}")]
        container = subprocess.check_output(
            ["docker", "run", "-d", *mount, "-p", f"127.0.0.1:{port}:8000", *args, image_id],
            text=True,
            timeout=60,
        ).strip()
        try:
            _poll_health(port)
            _run(["docker", "exec", container, *probe, "--mode", "http"])
            _run(["docker", "restart", container], timeout=60)
            _poll_health(port)
            _run(["docker", "exec", container, *probe, "--mode", "http", "--restart"])
            captured = subprocess.run(
                ["docker", "logs", container],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            logs = captured.stdout + captured.stderr
            assert "release_smoke_token" not in logs, "HTTP query content leaked into logs"
            assert "refresh_token=" not in logs, "Refresh credential query leaked into logs"
            assert "198.51.100.77" not in logs, "Untrusted forwarded peer reached access logs"
            print(
                json.dumps({"http_log_privacy": "passed", "untrusted_proxy": "ignored"}), flush=True
            )
        except Exception:
            # This container has only synthetic inputs and no operator credentials.
            subprocess.run(["docker", "logs", "--tail", "100", container], timeout=30, check=False)
            raise
        finally:
            subprocess.run(
                ["docker", "rm", "-f", container], check=True, timeout=60, stdout=subprocess.DEVNULL
            )
        _run(
            [
                "docker",
                "run",
                "--rm",
                *mount,
                image_id,
                "python",
                "-I",
                "/smoke/safety_runtime_smoke.py",
                "--root",
                "/smoke",
            ],
            timeout=300,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wheel", action="store_true", help="Build, install and exercise real STDIO"
    )
    parser.add_argument(
        "--stdio", action="store_true", help="Exercise STDIO from the installed wheel"
    )
    parser.add_argument(
        "--container", action="store_true", help="Exercise the configured non-root image"
    )
    parser.add_argument("--all", action="store_true", help="Run wheel/STDIO and container smoke")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.all:
        args.wheel = args.stdio = args.container = True
    if not (args.wheel or args.stdio or args.container):
        raise SystemExit("Choose --wheel, --stdio, --container, or --all")
    if args.wheel or args.stdio:
        smoke_wheel()
    if args.container:
        smoke_container()


if __name__ == "__main__":
    main()
