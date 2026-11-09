#!/usr/bin/env python3
"""Release smoke checks against actual installed wheel and non-root image entrypoints."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
import venv
import zipfile
from email.parser import BytesParser
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


_GRAMMAR_ENV = ("TREE_SITTER_LANGUAGE_PACK_CACHE_DIR", "TREE_SITTER_LANGUAGE_PACK_LIBS_DIR")


def _clean_env(root: Path) -> dict[str, str]:
    # Grammar-location paths only (not credentials): reuse prefetched grammars (#115).
    grammar = {key: os.environ[key] for key in _GRAMMAR_ENV if os.environ.get(key)}
    return {
        **grammar,
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(root),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def validate_wheel_source(wheel: Path, repo: Path = REPO) -> dict:
    """Bind the wheel's install contract and payload to the accepted source checkout."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    project = tomllib.loads((repo / "pyproject.toml").read_text())["project"]

    def requirement(value, extra=None):
        parsed = Requirement(value)
        marker = str(parsed.marker) if parsed.marker else ""
        if extra is not None:
            marker = f'({marker}) and extra == "{extra}"' if marker else f'extra == "{extra}"'
            from packaging.markers import Marker

            marker = str(Marker(marker))
        return (
            canonicalize_name(parsed.name),
            tuple(sorted(canonicalize_name(e) for e in parsed.extras)),
            str(parsed.specifier),
            parsed.url,
            marker,
        )

    expected_dependencies = {requirement(value) for value in project["dependencies"]}
    for extra, values in project.get("optional-dependencies", {}).items():
        expected_dependencies.update(requirement(value, extra) for value in values)
    tracked = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--", "mcp_server", "baml_sdk"],
            cwd=repo,
            timeout=30,
        )
        .decode()
        .split("\0")
    )
    payload = {
        name
        for name in tracked
        if name.endswith((".py", ".pyi"))
        or name == "mcp_server/py.typed"
        or name == "baml_sdk/py.typed"
        or name.startswith("mcp_server/storage/migrations/")
        and name.endswith(".sql")
    }
    info = (
        canonicalize_name(project["name"]).replace("-", "_") + f'-{project["version"]}.dist-info/'
    )
    metadata_files = {
        info + name
        for name in (
            "METADATA",
            "WHEEL",
            "RECORD",
            "entry_points.txt",
            "top_level.txt",
            "licenses/LICENSE",
        )
    }
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if not payload or len(names) != len(set(names)) or set(names) != payload | metadata_files:
            raise ValueError("Wheel payload membership differs from reviewed source")
        metadata = BytesParser().parsebytes(archive.read(info + "METADATA"))
        for key, expected in (
            ("Name", project["name"]),
            ("Version", project["version"]),
            ("Requires-Python", project["requires-python"]),
        ):
            if metadata.get_all(key) != [expected]:
                raise ValueError(f"Wheel {key} differs from reviewed source")
        actual_dependencies = {
            requirement(value) for value in metadata.get_all("Requires-Dist", [])
        }
        if actual_dependencies != expected_dependencies:
            raise ValueError("Wheel Requires-Dist differs from reviewed source")
        if set(metadata.get_all("Provides-Extra", [])) != set(
            project.get("optional-dependencies", {})
        ):
            raise ValueError("Wheel extras differ from reviewed source")
        entrypoints = configparser.ConfigParser(interpolation=None)
        entrypoints.read_string(archive.read(info + "entry_points.txt").decode())
        if (
            entrypoints.sections() != ["console_scripts"]
            or dict(entrypoints["console_scripts"]) != project["scripts"]
        ):
            raise ValueError("Wheel entry points differ from reviewed source")
        for name in sorted(payload):
            if archive.read(name) != (repo / name).read_bytes():
                raise ValueError(f"Wheel package content differs from reviewed source: {name}")
        if archive.read(info + "licenses/LICENSE") != (repo / "LICENSE").read_bytes():
            raise ValueError("Wheel license differs from reviewed source")
    return {
        "name": project["name"],
        "version": project["version"],
        "source_files_verified": len(payload),
    }


def build_wheel(dist: Path) -> tuple[Path, str]:
    """Build one wheel from this checkout; return it with its SHA256."""
    _run(["uv", "build", "--wheel", "--out-dir", str(dist)], timeout=300)
    wheels = sorted(dist.glob("index_it_mcp-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Expected exactly one built wheel")
    return wheels[0], hashlib.sha256(wheels[0].read_bytes()).hexdigest()


def _stage_wheel(dist: Path, wheel_path: Path | None, expected_sha256: str | None) -> Path:
    """Build or copy the wheel into ``dist``, check its digest and bind it to source."""
    if (wheel_path is None) != (expected_sha256 is None):
        raise ValueError("Delivered wheel requires its registry SHA256")
    if wheel_path is None:
        wheel, digest = build_wheel(dist)
    else:
        dist.mkdir()
        wheel = dist / wheel_path.name
        shutil.copyfile(wheel_path, wheel)
        if len(list(dist.glob("index_it_mcp-*.whl"))) != 1:
            raise RuntimeError("Expected exactly one built wheel")
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or digest != expected_sha256:
            raise ValueError("Delivered wheel registry digest mismatch")
    contract = validate_wheel_source(wheel)
    print(
        json.dumps({"wheel": wheel.name, "sha256": digest, "source_contract": contract}), flush=True
    )
    return wheel


def smoke_wheel(wheel_path: Path | None = None, expected_sha256: str | None = None) -> None:
    """Install the wheel under the lock's versions and exercise the installed runtime."""
    if (wheel_path is None) != (expected_sha256 is None):
        raise ValueError("Delivered wheel requires its registry SHA256")
    with tempfile.TemporaryDirectory(prefix="mcp-release-wheel-") as tmp:
        root = Path(tmp)
        dist, venv_dir, runtime = root / "dist", root / "venv", root / "runtime"
        runtime.mkdir()
        probe = runtime / PROBE.name
        shutil.copyfile(PROBE, probe)
        shutil.copyfile(SAFETY_PROBE, runtime / SAFETY_PROBE.name)
        wheels = [_stage_wheel(dist, wheel_path, expected_sha256)]
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
                "-c",
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


#: Python for the unconstrained install: the oldest supported interpreter, the
#: one release smoke and the PMCP pilot run on.
UNCONSTRAINED_PYTHON = "3.12"
#: The only environment the unconstrained install sees. Any other installer
#: setting (``UV_OFFLINE``, ``UV_DEFAULT_INDEX``, ``UV_FIND_LINKS``,
#: ``UV_EXCLUDE``, constraints, overrides, date cutoffs...) could resolve older
#: versions than a PyPI user gets, and ``--no-config`` does not block
#: environment variables, so everything else is dropped (#125 review S2).
#: These cover locating the interpreter, the uv cache, temp space, locale,
#: TLS trust and proxies; none of them selects package versions.
_UNCONSTRAINED_ENV_ALLOW = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
#: Resolve against public PyPI, the index a ``pip install index-it-mcp`` user gets.
UNCONSTRAINED_INDEX = "https://pypi.org/simple"


def _unconstrained_install_env() -> dict[str, str]:
    return {key: os.environ[key] for key in _UNCONSTRAINED_ENV_ALLOW if key in os.environ}


def _output(cmd: list[str], *, cwd: Path = REPO, env: dict[str, str] | None = None) -> str:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(
        cmd, cwd=cwd, env=env, check=True, timeout=120, capture_output=True, text=True
    ).stdout


def smoke_wheel_unconstrained(
    wheel_path: Path | None = None,
    expected_sha256: str | None = None,
    *,
    python: str = UNCONSTRAINED_PYTHON,
) -> dict[str, str]:
    """Install the wheel with no lock constraints and start the installed STDIO server.

    ``smoke_wheel`` installs under ``uv.lock``; a user running ``pip install
    index-it-mcp`` gets the newest versions the wheel's requirements allow
    instead. 1.4.1 and 1.5.0 declared ``mcp>=1.0.0``, which resolves mcp 2.x
    and cannot import ``mcp.shared.exceptions.McpError``; lock-constrained smoke
    and the PMCP pilot (mcp 1.27.0) could not see that. This smoke installs the
    wheel alone into a fresh venv, prefetches grammars into an explicit cache,
    and checks ``--version``, a client-independent MCP initialize and
    tools/list, and the lexical STDIO query probe.
    """
    if (wheel_path is None) != (expected_sha256 is None):
        raise ValueError("Delivered wheel requires its registry SHA256")
    with tempfile.TemporaryDirectory(prefix="mcp-release-unconstrained-") as tmp:
        root = Path(tmp)
        dist, venv_dir, runtime = root / "dist", root / "venv", root / "runtime"
        runtime.mkdir()
        probe = runtime / PROBE.name
        shutil.copyfile(PROBE, probe)
        wheel = _stage_wheel(dist, wheel_path, expected_sha256)
        install_env = _unconstrained_install_env()
        _run(
            ["uv", "venv", "--no-config", "--python", python, str(venv_dir)],
            cwd=root,
            env=install_env,
        )
        venv_python = _venv_bin(venv_dir, "python")
        # No -c/--constraint and no project or user uv config: only the wheel's
        # own Requires-Dist bounds the resolution.
        _run(
            [
                "uv",
                "pip",
                "install",
                "--no-config",
                "--default-index",
                UNCONSTRAINED_INDEX,
                "--resolution",
                "highest",
                "--python",
                str(venv_python),
                str(wheel),
            ],
            cwd=root,
            env=install_env,
            timeout=900,
        )
        _run(["uv", "pip", "check", "--python", str(venv_python)], cwd=root, env=install_env)
        frozen = _output(
            ["uv", "pip", "freeze", "--python", str(venv_python)], cwd=root, env=install_env
        )
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name

        installed = {
            canonicalize_name(name): version
            for name, version in (
                line.split("==", 1) for line in frozen.splitlines() if "==" in line
            )
        }
        project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
        direct = sorted(
            {canonicalize_name(Requirement(value).name) for value in project["dependencies"]}
        )
        resolved = {name: installed.get(name, "missing") for name in direct}
        print(json.dumps({"unconstrained_resolution": resolved}), flush=True)

        # Grammar location only (never credentials); an explicit cache root the
        # prefetch and every later process share. A caller's LIBS_DIR is
        # dropped: it holds parser libraries for the locked language pack.
        cache = Path(os.environ.get(_GRAMMAR_ENV[0]) or root / "grammar-cache")
        env = {key: value for key, value in _clean_env(root).items() if key not in _GRAMMAR_ENV}
        env[_GRAMMAR_ENV[0]] = str(cache)
        entrypoint = _venv_bin(venv_dir, "index-it-mcp")
        reported = _output([str(entrypoint), "--version"], cwd=runtime, env=env)
        if f"version {project['version']}" not in reported:
            raise RuntimeError(f"Installed --version differs from source: {reported.strip()}")
        _run(
            [str(entrypoint), "grammars", "prefetch", "--cache-dir", str(cache)],
            cwd=runtime,
            env=env,
            timeout=600,
        )
        for mode in ("prepare", "initialize", "stdio"):
            _run(
                [
                    str(venv_python),
                    "-I",
                    str(probe),
                    "--root",
                    str(runtime),
                    "--entrypoint",
                    str(entrypoint),
                    "--mode",
                    mode,
                ],
                cwd=runtime,
                env=env,
                timeout=300,
            )
        print(json.dumps({"unconstrained_smoke": "passed", "python": python}), flush=True)
        return resolved


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


def smoke_container(
    image_ref: str | None = None, *, network: str = "bridge", image_signer_sha: str | None = None
) -> None:
    if image_ref is not None and not re.fullmatch(
        r"ghcr\.io/[a-z0-9_./-]+@sha256:[0-9a-f]{64}", image_ref
    ):
        raise ValueError("Delivered image requires an immutable GHCR digest reference")
    if image_signer_sha is not None and not re.fullmatch(r"[0-9a-f]{40}", image_signer_sha):
        raise ValueError("Image signing workflow SHA is invalid")
    if shutil.which("docker") is None:
        raise RuntimeError("docker is required for --container smoke")
    if image_ref is None:
        _run(
            [
                "docker",
                "build",
                *(["--network", "host"] if network == "host" else []),
                "-f",
                "docker/dockerfiles/Dockerfile.production",
                "-t",
                IMAGE,
                ".",
            ],
            timeout=600,
        )
    else:
        _run(["docker", "pull", image_ref], timeout=300)
        digests = json.loads(
            subprocess.check_output(
                ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image_ref],
                text=True,
                timeout=30,
            )
        )
        if image_ref not in digests:
            raise ValueError("Delivered image registry digest mismatch")
        source = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True, timeout=30
        ).strip()
        labels = json.loads(
            subprocess.check_output(
                ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", image_ref],
                text=True,
                timeout=30,
            )
        )
        project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
        expected_labels = {
            "org.opencontainers.image.version": "v" + project["version"],
            "org.opencontainers.image.revision": source,
        }
        if not isinstance(labels, dict) or any(
            labels.get(key) != value for key, value in expected_labels.items()
        ):
            raise ValueError("Delivered image labels differ from accepted source")
        if shutil.which("cosign") is None:
            raise RuntimeError("cosign is required to verify delivered image provenance")
        if image_signer_sha is not None:
            _run(["git", "merge-base", "--is-ancestor", source, image_signer_sha])
            _run(["git", "merge-base", "--is-ancestor", image_signer_sha, "origin/main"])
            _run(
                [
                    "git",
                    "cat-file",
                    "-e",
                    image_signer_sha + ":.github/workflows/sign-published-image.yml",
                ]
            )
        signer_workflow = (
            "sign-published-image.yml" if image_signer_sha is not None else "release-automation.yml"
        )
        _run(
            [
                "cosign",
                "verify",
                image_ref,
                "--certificate-identity",
                "https://github.com/Consiliency/Code-Index-MCP/.github/workflows/"
                + signer_workflow
                + "@refs/heads/main",
                "--certificate-oidc-issuer",
                "https://token.actions.githubusercontent.com",
                "--certificate-github-workflow-sha",
                image_signer_sha or source,
            ],
            timeout=120,
        )
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image_ref or IMAGE],
        text=True,
        timeout=30,
    ).strip()
    print(json.dumps({"image": image_ref or IMAGE, "image_id": image_id}), flush=True)
    # The BAML calls run in a child process with a blank MCP_BAML_TELEMETRY (the
    # value compose's `${VAR:-}` produces) and no mcp_server import first, so
    # the generated SDK's baked "off" default is what is exercised. The bridge
    # writes recordings from native code, including at exit, so the parent
    # checks the child's HOME afterwards. The socket patch only guards
    # Python-level clients; it cannot see the bridge's native HTTP.
    baml_contract = r"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

calls = r'''
from importlib.metadata import version
import os
from unittest.mock import patch

from baml_sdk import ChunkInput, SummarizeChunkAlone_spec, SummarizeFileChunks_spec

assert version("baml-bridge") == "0.23.0"
assert os.environ["MCP_BAML_TELEMETRY"] == ""
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
'''
home = Path(tempfile.mkdtemp(prefix="baml-smoke-home-"))
env = {
    key: value
    for key, value in os.environ.items()
    if not key.startswith(("BAML_", "BOUNDARY_", "MCP_BAML_"))
}
env.update(
    HOME=str(home),
    BAML_HOME=str(home / ".baml"),
    MCP_BAML_TELEMETRY="",
    CEREBRAS_API_KEY="dummy-offline",
)
subprocess.run([sys.executable, "-c", calls], env=env, check=True, timeout=120)
written = sorted(str(path) for path in home.rglob("*") if path.is_file())
assert written == [], f"BAML recorded under HOME by default: {written}"
"""
    _run(["docker", "run", "--rm", image_id, "python", "-c", baml_contract])
    with tempfile.TemporaryDirectory(prefix="mcp-release-container-") as tmp:
        root = Path(tmp)
        root.chmod(0o777)
        shutil.copyfile(PROBE, root / PROBE.name)
        shutil.copyfile(SAFETY_PROBE, root / SAFETY_PROBE.name)
        (root / PROBE.name).chmod(0o644)
        (root / SAFETY_PROBE.name).chmod(0o644)
        mount = ["-v", f"{root}:/smoke"]
        probe = ["python", "-I", "/smoke/installed_runtime_smoke.py", "--root", "/smoke"]
        # The image's configured USER owns the fixture; no root override or fake services.
        for mode in ("schema", "prepare", "python"):
            _run(["docker", "run", "--rm", *mount, image_id, *probe, "--mode", mode])
        port = 8000 if network == "host" else _free_port()
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
        container = "mcp-release-smoke-" + uuid.uuid4().hex
        try:
            subprocess.check_output(
                [
                    "docker",
                    "run",
                    "--name",
                    container,
                    "-d",
                    *mount,
                    *(
                        ["--network", "host"]
                        if network == "host"
                        else ["-p", f"127.0.0.1:{port}:8000"]
                    ),
                    *args,
                    image_id,
                ],
                text=True,
                timeout=60,
            )
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
    parser.add_argument(
        "--unconstrained",
        action="store_true",
        help="Also install the wheel with no lock constraints and start STDIO",
    )
    parser.add_argument(
        "--unconstrained-python",
        default=UNCONSTRAINED_PYTHON,
        help="Python version for the unconstrained venv (default: %(default)s)",
    )
    parser.add_argument(
        "--all", action="store_true", help="Run wheel/STDIO, unconstrained and container smoke"
    )
    parser.add_argument("--docker-network", choices=("bridge", "host"), default="bridge")
    parser.add_argument("--wheel-path", type=Path, help="Use a delivered wheel without building")
    parser.add_argument("--wheel-sha256", help="Expected SHA256 from the package registry")
    parser.add_argument(
        "--image-ref", help="Use a delivered immutable GHCR digest without building"
    )
    parser.add_argument(
        "--image-signer-sha",
        help="Protected-main workflow commit for an image signed after publication",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if (args.wheel_path is None) != (args.wheel_sha256 is None):
        raise SystemExit("Supply both --wheel-path and --wheel-sha256")
    if args.image_signer_sha is not None and args.image_ref is None:
        raise SystemExit("--image-signer-sha requires --image-ref")
    delivered = args.wheel_path is not None or args.image_ref is not None
    if args.all:
        args.wheel = args.stdio = args.unconstrained = args.container = True
    args.wheel = args.wheel or args.wheel_path is not None
    args.container = args.container or args.image_ref is not None
    if delivered and (
        (args.wheel or args.stdio or args.unconstrained)
        and args.wheel_path is None
        or args.container
        and args.image_ref is None
    ):
        raise SystemExit("Delivered mode cannot mix registry artifacts with local builds")
    if not (args.wheel or args.stdio or args.unconstrained or args.container):
        raise SystemExit("Choose --wheel, --stdio, --unconstrained, --container, or --all")
    if args.wheel or args.stdio or args.unconstrained:
        # Build once so the locked and unconstrained smokes test the same bytes.
        with tempfile.TemporaryDirectory(prefix="mcp-release-build-") as tmp:
            wheel_path, wheel_sha256 = args.wheel_path, args.wheel_sha256
            if wheel_path is None:
                wheel_path, wheel_sha256 = build_wheel(Path(tmp))
            if args.wheel or args.stdio:
                smoke_wheel(wheel_path, wheel_sha256)
            if args.unconstrained:
                smoke_wheel_unconstrained(
                    wheel_path, wheel_sha256, python=args.unconstrained_python
                )
    if args.container:
        smoke_container(
            args.image_ref, network=args.docker_network, image_signer_sha=args.image_signer_sha
        )


if __name__ == "__main__":
    main()
