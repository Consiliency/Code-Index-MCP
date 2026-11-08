"""BAML source, generated SDK and installed bridge must use one exact version."""

import ast
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest


def test_baml_generator_runtime_and_generated_client_match():
    root = Path(__file__).resolve().parents[1]
    runtime = version("baml-bridge")
    assert runtime == "0.23.0"
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert f"baml-bridge=={runtime}" in project["project"]["dependencies"]
    assert f'VERSION = "{runtime}"' in (root / "scripts/generate_baml_sdk.py").read_text()
    assert (root / "baml.toml").is_file()
    assert (
        f"BAML generation and runtime are pinned together at {runtime}"
        in (root / "docs/SUPPORT_MATRIX.md").read_text()
    )

    from baml_sdk import SummarizeChunkAlone_spec, SummarizeFileChunks_spec

    assert callable(SummarizeChunkAlone_spec)
    assert callable(SummarizeFileChunks_spec)


# baml-bridge 0.21+ records every call under BAML_HOME (~/.baml/btel) and uploads
# the recordings to Boundary when BOUNDARY_API_KEY or a saved login exists. The
# bridge does that I/O natively (Rust), so patching Python's socket module can't
# observe or block it. These probes run in a subprocess with only a fake key,
# point BOUNDARY_API_URL at a loopback listener, and count what reaches it.
_FAKE_BOUNDARY_KEY = "bnd_fake_offline_test_key"

# Import baml_sdk before anything in mcp_server: the default must hold whoever
# initializes the runtime first.
_RECORDING_PROBE = r"""
import os

from baml_sdk import SummarizeChunkAlone_spec

import mcp_server.indexing.summarization  # noqa: F401

os.environ["CEREBRAS_API_KEY"] = "dummy-offline"
spec = SummarizeChunkAlone_spec(
    language="python",
    symbol="f",
    line_start=1,
    line_end=1,
    content="def f(): return 1",
    parent_context="",
    file_context="",
)
assert spec.build_request().url == "https://api.cerebras.ai/v1/chat/completions"
"""


class _LoopbackBoundary:
    """Loopback stand-in for the Boundary API that counts accepted connections."""

    def __init__(self):
        self.connections = 0
        self._server = socket.create_server(("127.0.0.1", 0))
        self._server.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self._server.getsockname()[1]

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (TimeoutError, OSError):
                continue
            self.connections += 1
            with conn:
                conn.settimeout(2)
                try:
                    conn.recv(65536)
                    conn.sendall(
                        b"HTTP/1.1 503 Service Unavailable\r\n"
                        b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                except OSError:
                    pass

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)
        self._server.close()


def _run_recording_probe(tmp_path, telemetry, *, boundary_key=_FAKE_BOUNDARY_KEY):
    """Return (connections to the fake Boundary API, files under the probe HOME)."""
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    home.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "MCP_BAML_TELEMETRY" and not key.startswith(("BAML_", "BOUNDARY_"))
    }
    env.update(HOME=str(home), BAML_HOME=str(home / ".baml"), DO_NOT_TRACK="1")
    if telemetry is not None:
        env["MCP_BAML_TELEMETRY"] = telemetry
    with _LoopbackBoundary() as boundary:
        env["BOUNDARY_API_URL"] = boundary.url
        if boundary_key is not None:
            env["BOUNDARY_API_KEY"] = boundary_key
        subprocess.run(
            [sys.executable, "-c", _RECORDING_PROBE],
            cwd=root,
            env=env,
            check=True,
            timeout=120,
        )
        # Uploads flush when the bridge shuts down at interpreter exit.
        time.sleep(0.5)
    files = sorted(str(path.relative_to(home)) for path in home.rglob("*") if path.is_file())
    return boundary.connections, files


def test_generated_sdk_bakes_recording_off_and_binds_mcp_variable():
    """The SDK's own default is off; only MCP_BAML_TELEMETRY can raise it."""
    root = Path(__file__).resolve().parents[1]
    module = ast.parse((root / "baml_sdk/_inlinedbaml.py").read_text())
    embedded = next(
        node.value.value
        for node in module.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "EMBEDDED_BAML_TOML"
    )
    telemetry = tomllib.loads(embedded)["__baml_codegen"]["telemetry"]
    assert telemetry["recording_level"] == "off"
    assert telemetry["recording_level_envvar"] == "MCP_BAML_TELEMETRY"
    assert telemetry["destination_overrides"] == {"kind": "default"}
    assert "embedded" not in telemetry


@pytest.mark.parametrize(
    "telemetry",
    [None, "", "   \t", "off", "OFF", "false", "true"],
    ids=["unset", "blank", "whitespace", "off", "OFF", "false", "true"],
)
def test_baml_records_and_uploads_nothing_by_default(tmp_path, telemetry):
    connections, files = _run_recording_probe(tmp_path, telemetry)
    assert connections == 0
    assert files == []


def test_baml_upload_probe_sees_explicit_opt_in(tmp_path):
    """Positive control: the probe can observe an upload, so 0 above is real."""
    connections, _files = _run_recording_probe(tmp_path, "medium")
    assert connections > 0


def test_baml_local_recording_probe_sees_explicit_opt_in(tmp_path):
    """Positive control without a key: recordings land under BAML_HOME."""
    _connections, files = _run_recording_probe(tmp_path, "medium", boundary_key=None)
    assert [path for path in files if "btel" in path]


def test_baml_regeneration_and_formatting_are_reproducible(tmp_path):
    root = Path(__file__).resolve().parents[1]
    toolchain = os.environ.get("BAML_TOOLCHAIN") or shutil.which("baml")
    if not toolchain:
        if os.environ.get("BAML_REQUIRE_REGEN") == "1":
            pytest.fail("BAML 0.23.0 toolchain is required by the release gate")
        pytest.skip("matching BAML 0.23.0 toolchain is not installed")
    checked = subprocess.run(
        [toolchain, "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={**os.environ, "DO_NOT_TRACK": "1"},
    )
    if not checked.stdout.strip().endswith("0.23.0"):
        if os.environ.get("BAML_REQUIRE_REGEN") == "1":
            pytest.fail("BAML 0.23.0 toolchain is required by the release gate")
        pytest.skip("matching BAML 0.23.0 toolchain is not installed")

    shutil.copytree(root / "baml_src", tmp_path / "baml_src")
    (tmp_path / "scripts").mkdir()
    shutil.copy2(root / "scripts/generate_baml_sdk.py", tmp_path / "scripts")
    shutil.copy2(root / "baml.toml", tmp_path / "baml.toml")
    subprocess.run(
        [sys.executable, str(tmp_path / "scripts/generate_baml_sdk.py")],
        cwd=tmp_path,
        env={**os.environ, "BAML_TOOLCHAIN": toolchain},
        check=True,
        capture_output=True,
        timeout=120,
    )
    expected = {
        path.relative_to(root / "baml_sdk"): path.read_bytes()
        for path in (root / "baml_sdk").rglob("*")
        if path.is_file()
        and (path.suffix in {".py", ".pyi"} or path.name in {".gitignore", "py.typed"})
    }
    observed = {
        path.relative_to(tmp_path / "baml_sdk"): path.read_bytes()
        for path in (tmp_path / "baml_sdk").rglob("*")
        if path.is_file()
        and (path.suffix in {".py", ".pyi"} or path.name in {".gitignore", "py.typed"})
    }
    assert observed == expected
