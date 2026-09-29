"""SL-1 unit tests — envelope, supervisor, SandboxedPlugin round-trip."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_server.core.repo_context import RepoContext
from mcp_server.plugins.memory_aware_manager import MemoryAwarePluginManager
from mcp_server.plugins.plugin_set_registry import PluginSetRegistry
from mcp_server.plugins.sandboxed_plugin import SandboxedPlugin
from mcp_server.sandbox.capabilities import CapabilitySet
from mcp_server.sandbox.protocol import (
    MAX_LINE_BYTES,
    Envelope,
    ProtocolError,
    decode,
    encode,
)
from mcp_server.sandbox.supervisor import (
    SandboxCallError,
    SandboxSupervisor,
    SandboxTimeout,
)

# Repo root (ancestor containing mcp_server/ and tests/). Passed to child
# subprocesses as PYTHONPATH so they resolve the worker module plus fixtures.
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("form", ["builtin", "pathlib", "io", "os", "bytes", "fd"])
def test_capability_guard_rejects_outside_read_forms(tmp_path, form):
    outside = tmp_path / "outside.txt"
    outside.write_text("synthetic-private-content")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    script = r"""
import builtins, io, os, sys
from pathlib import Path
from mcp_server.sandbox.capabilities import CapabilitySet, SandboxViolation
from mcp_server.sandbox.caps_apply import install_fs_guard
outside, allowed, form = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
fd = os.open(outside, os.O_RDONLY)
install_fs_guard(CapabilitySet(fs_read=(allowed,), fs_write=(), env_allow=frozenset()))
try:
    if form == "builtin":
        builtins.open(outside).close()
    elif form == "pathlib":
        outside.read_text()
    elif form == "io":
        io.open(outside).close()
    elif form == "os":
        os.close(os.open(outside, os.O_RDONLY))
    elif form == "bytes":
        builtins.open(os.fsencode(outside)).close()
    else:
        builtins.open(fd, closefd=False).close()
except SandboxViolation:
    print("denied")
else:
    raise AssertionError("outside read permitted")
finally:
    os.close(fd)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(outside), str(allowed), form],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "denied"


@pytest.mark.parametrize("form", ["string", "path", "uri", "uri_rw", "uri_rwc"])
def test_readonly_sqlite_forms_reject_write(tmp_path, form):
    script = r"""
import sqlite3, sys
from pathlib import Path
from mcp_server.sandbox.capabilities import CapabilitySet
from mcp_server.sandbox.caps_apply import _patch_sqlite
path = Path(sys.argv[1]) / "index with spaces.db"
with sqlite3.connect(path) as conn:
    conn.execute("CREATE TABLE marker (value TEXT)")
caps = CapabilitySet(fs_read=(path.parent,), fs_write=(), env_allow=frozenset(), sqlite="readonly")
_patch_sqlite(caps)
form = sys.argv[2]
kwargs = {}
database = str(path) if form == "string" else path
if form.startswith("uri"):
    database = path.as_uri() + ("?mode=" + form[4:] if form != "uri" else "")
    kwargs["uri"] = True
with sqlite3.connect(database, **kwargs) as conn:
    assert conn.execute("SELECT count(*) FROM marker").fetchone()[0] == 0
    try:
        conn.execute("INSERT INTO marker VALUES ('late-write')")
    except sqlite3.OperationalError:
        print("readonly")
    else:
        raise AssertionError("write permitted")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), form],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "readonly"


# ---------------------------------------------------------------------------
# protocol — envelope codec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["outside", "attach", "dbapi2", "bytes"])
def test_sqlite_readonly_boundaries(tmp_path, operation):
    script = r"""
import os, sqlite3, sys
from pathlib import Path
from mcp_server.sandbox.capabilities import CapabilitySet, SandboxViolation
from mcp_server.sandbox.caps_apply import _patch_sqlite
root = Path(sys.argv[1])
allowed = root / "allowed"
allowed.mkdir()
inside, outside = allowed / "db", root / "outside.db"
for path in (inside, outside):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE marker (value TEXT)")
_patch_sqlite(CapabilitySet(fs_read=(allowed,), fs_write=(), env_allow=frozenset(), sqlite="readonly"))
operation = sys.argv[2]
if operation == "outside":
    try:
        sqlite3.connect(outside.as_uri() + "?mode=ro", uri=True)
    except SandboxViolation:
        pass
    else:
        raise AssertionError("outside database accepted")
else:
    connect = sqlite3.dbapi2.connect if operation == "dbapi2" else sqlite3.connect
    with connect(os.fsencode(inside) if operation == "bytes" else inside) as conn:
        assert conn.execute("SELECT count(*) FROM marker").fetchone()[0] == 0
        try:
            if operation == "attach":
                conn.execute("ATTACH DATABASE ? AS other", (str(outside),))
            else:
                conn.execute("INSERT INTO marker VALUES ('write')")
        except sqlite3.DatabaseError:
            pass
        else:
            raise AssertionError("mutation accepted")
print("guarded")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), operation],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "guarded"


def test_filesystem_allowed_forms_and_dirfd_boundary(tmp_path):
    script = r"""
import builtins, io, os, sys
from pathlib import Path
from mcp_server.sandbox.capabilities import CapabilitySet, SandboxViolation
from mcp_server.sandbox.caps_apply import install_fs_guard
root = Path(sys.argv[1])
allowed = root / "allowed"
allowed.mkdir()
path = allowed / "sample"
path.write_text("synthetic")
outside = root / "outside"
outside.write_text("private")
directory = os.open(allowed, os.O_RDONLY)
install_fs_guard(CapabilitySet(fs_read=(allowed,), fs_write=(allowed,), env_allow=frozenset()))
assert path.read_text() == "synthetic"
with io.open(os.fsencode(path)) as f:
    assert f.read() == "synthetic"
fd = os.open("sample", os.O_RDONLY, dir_fd=directory)
with builtins.open(fd) as f:
    assert f.read() == "synthetic"
path.write_text("updated")
try:
    os.open("../outside", os.O_RDONLY, dir_fd=directory)
except SandboxViolation:
    pass
else:
    raise AssertionError("dir_fd escaped root")
os.close(directory)
print("guarded")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "guarded"


@pytest.mark.parametrize("payload", ['{"sqlite":"write"}', '{"network":"false"}'])
def test_capability_json_rejects_ambiguous_permissions(payload):
    with pytest.raises(ValueError):
        CapabilitySet.from_json(payload)


def test_envelope_roundtrip():
    e = Envelope(v=1, id="abc", kind="call", method="supports", payload={"x": 1})
    line = encode(e)
    assert line.endswith(b"\n")
    e2 = decode(line)
    assert e2 == e


def test_envelope_oversize_rejected():
    huge = "x" * (MAX_LINE_BYTES + 10)
    e = Envelope(v=1, id="a", kind="call", method="m", payload={"s": huge})
    with pytest.raises(ProtocolError):
        encode(e)


def test_envelope_decode_rejects_garbage():
    with pytest.raises(ProtocolError):
        decode(b"not-json-at-all\n")


def test_envelope_decode_rejects_wrong_version():
    with pytest.raises(ProtocolError):
        decode(b'{"v":2,"id":"a","kind":"call","method":"m","payload":{}}\n')


# ---------------------------------------------------------------------------
# supervisor — spawn / call / close against a trivial echo worker
# ---------------------------------------------------------------------------


def _echo_worker_cmd(tmp_path: Path) -> list:
    """Build a worker command that runs an inline echo script.

    The inline script acts as a minimal IPC responder: it reads one envelope
    per line from stdin and writes the same envelope back with kind=result.
    """
    script = textwrap.dedent("""
        import sys, json
        stdin = sys.stdin.buffer
        stdout = sys.stdout.buffer
        while True:
            line = stdin.readline()
            if not line:
                break
            try:
                env = json.loads(line.decode('utf-8'))
            except Exception:
                break
            env['kind'] = 'result'
            env['payload'] = {'echoed': env.get('payload', {})}
            stdout.write((json.dumps(env) + "\\n").encode('utf-8'))
            stdout.flush()
        """)
    path = tmp_path / "echo_worker.py"
    path.write_text(script)
    return [sys.executable, str(path)]


def test_supervisor_spawn_and_close(tmp_path: Path):
    caps = CapabilitySet(fs_read=(), fs_write=(), env_allow=frozenset())
    sup = SandboxSupervisor(_echo_worker_cmd(tmp_path), caps)
    resp = sup.call("ping", {"a": 1}, timeout=5.0)
    assert resp == {"echoed": {"a": 1}}
    assert sup.worker_pid is not None
    assert sup.is_worker_running is True
    assert sup.worker_rss_bytes() > 0
    sup.close()
    # Proc gone.
    assert sup._proc is None
    assert sup.worker_pid is None
    assert sup.is_worker_running is False


def test_supervisor_serializes_concurrent_large_calls(tmp_path: Path):
    caps = CapabilitySet(fs_read=(), fs_write=(), env_allow=frozenset())
    sup = SandboxSupervisor(_echo_worker_cmd(tmp_path), caps)
    payloads = [{"index": i, "content": str(i) * 32_768} for i in range(12)]
    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            responses = list(
                executor.map(lambda payload: sup.call("echo", payload, timeout=5.0), payloads)
            )
    finally:
        sup.close()

    assert [response["echoed"] for response in responses] == payloads


def test_supervisor_introspection_does_not_spawn(tmp_path: Path):
    caps = CapabilitySet(fs_read=(), fs_write=(), env_allow=frozenset())
    sup = SandboxSupervisor(_echo_worker_cmd(tmp_path), caps)
    assert sup.worker_pid is None
    assert sup.is_worker_running is False
    assert sup.worker_rss_bytes() == 0


def test_supervisor_call_after_close_does_not_respawn(tmp_path: Path):
    caps = CapabilitySet(fs_read=(), fs_write=(), env_allow=frozenset())
    sup = SandboxSupervisor(_echo_worker_cmd(tmp_path), caps)
    sup.call("ping", {}, timeout=5.0)
    original_pid = sup.worker_pid
    assert original_pid is not None

    sup.close()

    with pytest.raises(SandboxCallError, match="SupervisorClosed"):
        sup.call("ping", {}, timeout=5.0)
    assert sup.worker_pid is None
    assert sup.is_worker_running is False
    assert sup._proc is None


def test_registry_worker_count_scales_with_used_languages(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    python_file = repo / "sample.py"
    rust_file = repo / "lib.rs"
    python_file.write_text("def sample():\n    return 1\n", encoding="utf-8")
    rust_file.write_text("fn sample() -> i32 { 1 }\n", encoding="utf-8")
    ctx = RepoContext(
        repo_id="sandbox-scale-probe",
        sqlite_store=None,
        workspace_root=repo,
        tracked_branch="main",
        registry_entry=SimpleNamespace(),
    )
    manager = MemoryAwarePluginManager(max_memory_mb=1024, max_workers=4)
    registry = PluginSetRegistry(manager)
    started = time.monotonic()
    try:
        assert len(registry.plugins_for_file(ctx, python_file)) == 1
        first = manager.resource_snapshot(force=True)
        assert (first.reserved_workers, first.live_workers) == (1, 1)

        assert len(registry.plugins_for_file(ctx, rust_file)) == 1
        second = manager.resource_snapshot(force=True)
        assert (second.reserved_workers, second.live_workers) == (2, 2)
        assert time.monotonic() - started < 20.0
    finally:
        registry.shutdown()

    closed = manager.resource_snapshot(force=True)
    assert (closed.reserved_workers, closed.live_workers) == (0, 0)


def test_supervisor_timeout(tmp_path: Path):
    """A worker that never responds triggers SandboxTimeout."""
    script = textwrap.dedent("""
        import sys, time
        # Read one line then sleep forever, never reply.
        sys.stdin.buffer.readline()
        time.sleep(60)
        """)
    path = tmp_path / "sleepy.py"
    path.write_text(script)
    caps = CapabilitySet(fs_read=(), fs_write=(), env_allow=frozenset())
    sup = SandboxSupervisor([sys.executable, str(path)], caps)
    try:
        with pytest.raises(SandboxTimeout):
            sup.call("ping", {}, timeout=0.5)
    finally:
        sup.close()


def test_supervisor_worker_exited(tmp_path: Path):
    """Worker that exits before responding surfaces SandboxCallError."""
    script = textwrap.dedent("""
        import sys
        sys.exit(3)
        """)
    path = tmp_path / "die.py"
    path.write_text(script)
    caps = CapabilitySet(fs_read=(), fs_write=(), env_allow=frozenset())
    sup = SandboxSupervisor([sys.executable, str(path)], caps)
    try:
        with pytest.raises((SandboxCallError, SandboxTimeout)):
            sup.call("ping", {}, timeout=2.0)
    finally:
        sup.close()


# ---------------------------------------------------------------------------
# SandboxedPlugin — end-to-end with the mock plugin fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pythonpath_env():
    """Ensure the worker subprocess can import tests.security.fixtures.*."""
    orig = os.environ.get("PYTHONPATH")
    existing = [p for p in (orig.split(os.pathsep) if orig else []) if p]
    repo = str(REPO_ROOT)
    if repo not in existing:
        existing.insert(0, repo)
    os.environ["PYTHONPATH"] = os.pathsep.join(existing)
    try:
        yield
    finally:
        if orig is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = orig


def _caps_for_mock() -> CapabilitySet:
    # Generous caps so the mock plugin imports cleanly; keep env_allow loose
    # enough that PATH/HOME/PYTHONPATH reach the child.
    return CapabilitySet(
        fs_read=(REPO_ROOT,),
        fs_write=(),
        env_allow=frozenset({"PATH", "HOME", "PYTHONPATH", "LANG", "LC_ALL"}),
        network=True,  # mock does no network; keep caps unchanged for speed
        sqlite="none",
    )


def test_sandboxed_plugin_supports_roundtrip():
    p = SandboxedPlugin(
        "tests.security.fixtures.mock_plugin",
        _caps_for_mock(),
        gh_cmd=sys.executable,
    )
    try:
        assert p.supports("x.py") is True
    finally:
        p.close()


def test_sandboxed_plugin_indexfile_roundtrip():
    p = SandboxedPlugin(
        "tests.security.fixtures.mock_plugin",
        _caps_for_mock(),
        gh_cmd=sys.executable,
    )
    try:
        shard = p.indexFile("x.py", "print(1)")
        assert shard["file"] == "x.py"
        assert shard["language"] == "mock"
        assert shard["symbols"] == [{"name": "echo", "len": len("print(1)")}]
    finally:
        p.close()


def test_sandboxed_plugin_find_references_roundtrip():
    p = SandboxedPlugin(
        "tests.security.fixtures.mock_plugin",
        _caps_for_mock(),
        gh_cmd=sys.executable,
    )
    try:
        refs = list(p.findReferences("foo"))
        assert len(refs) == 1
        assert refs[0].file == "mock.py"
        assert refs[0].start_line == 1
    finally:
        p.close()


def test_sandboxed_plugin_language_cached():
    p = SandboxedPlugin(
        "tests.security.fixtures.mock_plugin",
        _caps_for_mock(),
        gh_cmd=sys.executable,
    )
    try:
        assert p.language == "mock"
        assert p.language == "mock"  # cached; no second IPC call
    finally:
        p.close()


# ---------------------------------------------------------------------------
# PluginFactory sandbox opt-in surface
# ---------------------------------------------------------------------------


def test_plugin_factory_sandbox_on_by_default():
    """Default behavior is sandbox-on after SL-5 flip. DISABLE=1 is required to turn it off."""
    from mcp_server.plugins.plugin_factory import PluginFactory

    # Clear any stray env that a parallel test might have set.
    os.environ.pop("MCP_PLUGIN_SANDBOX_ENABLED", None)
    os.environ.pop("MCP_PLUGIN_SANDBOX_DISABLE", None)
    try:
        p = PluginFactory.create_plugin("python")
        # SL-5: default-on means no-cap path still returns SandboxedPlugin.
        assert type(p).__name__ == "SandboxedPlugin"
    finally:
        if hasattr(p, "close"):
            p.close()


def test_plugin_factory_sandbox_opt_in_via_capabilities():
    """Passing capabilities= must flip the factory to SandboxedPlugin."""
    from mcp_server.plugins.plugin_factory import PluginFactory

    caps = CapabilitySet(
        fs_read=(REPO_ROOT,),
        fs_write=(),
        env_allow=frozenset({"PATH", "HOME", "PYTHONPATH"}),
        network=True,
        sqlite="none",
    )
    p = PluginFactory.create_plugin("python", capabilities=caps)
    try:
        assert type(p).__name__ == "SandboxedPlugin"
    finally:
        if hasattr(p, "close"):
            p.close()
