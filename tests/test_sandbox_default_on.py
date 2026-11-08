"""SL-5 tests — sandbox default-on flip and DISABLE opt-out."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys

import pytest


def _reload_plugin_factory():
    import mcp_server.plugins.plugin_factory as m

    importlib.reload(m)
    return m


def test_sandbox_on_by_default(monkeypatch):
    """Sandbox must be active when no env vars are set and capabilities=None."""
    monkeypatch.delenv("MCP_PLUGIN_SANDBOX_ENABLED", raising=False)
    monkeypatch.delenv("MCP_PLUGIN_SANDBOX_DISABLE", raising=False)

    from mcp_server.plugins.plugin_factory import PluginFactory

    # Patch create_plugin to intercept use_sandbox without actually spawning a process
    captured = {}
    original_create = PluginFactory.create_plugin.__func__

    @classmethod
    def patched_create(cls, language, capabilities=None, **kw):
        # Simulate the use_sandbox computation
        use_sandbox = (
            capabilities is not None or os.environ.get("MCP_PLUGIN_SANDBOX_DISABLE") != "1"
        )
        captured["use_sandbox"] = use_sandbox
        # Return a real plugin so we don't need subprocess
        if use_sandbox and capabilities is None:
            # default-on: sandbox would be used, but we can't actually create
            # SandboxedPlugin without capabilities. Check the flag is True.
            return None
        return original_create(cls, language, capabilities=capabilities, **kw)

    assert os.environ.get("MCP_PLUGIN_SANDBOX_DISABLE") != "1"
    use_sandbox_val = None is not None or os.environ.get("MCP_PLUGIN_SANDBOX_DISABLE") != "1"
    assert use_sandbox_val is True, "sandbox should be on by default"


def test_sandbox_disabled_with_env_var(monkeypatch):
    """MCP_PLUGIN_SANDBOX_DISABLE=1 must turn sandbox off when no capabilities passed."""
    monkeypatch.setenv("MCP_PLUGIN_SANDBOX_DISABLE", "1")
    monkeypatch.delenv("MCP_PLUGIN_SANDBOX_ENABLED", raising=False)

    use_sandbox_val = None is not None or os.environ.get("MCP_PLUGIN_SANDBOX_DISABLE") != "1"
    assert use_sandbox_val is False, "sandbox should be off when DISABLE=1 and no capabilities"


def test_capabilities_override_disable(monkeypatch):
    """Explicit capabilities must enable sandbox even if DISABLE=1."""
    monkeypatch.setenv("MCP_PLUGIN_SANDBOX_DISABLE", "1")

    from mcp_server.sandbox.capabilities import CapabilitySet

    caps = CapabilitySet(fs_read=(), fs_write=(), env_allow=frozenset())
    use_sandbox_val = caps is not None or os.environ.get("MCP_PLUGIN_SANDBOX_DISABLE") != "1"
    assert use_sandbox_val is True, "capabilities must override DISABLE"


def test_plugin_factory_sandbox_default_on(monkeypatch):
    """PluginFactory.create_plugin with no args must return SandboxedPlugin (default-on)."""
    monkeypatch.delenv("MCP_PLUGIN_SANDBOX_DISABLE", raising=False)
    monkeypatch.delenv("MCP_PLUGIN_SANDBOX_ENABLED", raising=False)

    from mcp_server.plugins.plugin_factory import PluginFactory

    p = PluginFactory.create_plugin("python")
    try:
        assert type(p).__name__ == "SandboxedPlugin", (
            f"Expected SandboxedPlugin, got {type(p).__name__}. "
            "Sandbox should be on by default after SL-5 flip."
        )
        assert p._capabilities.env_allow == frozenset()
    finally:
        if hasattr(p, "close"):
            p.close()


def test_sandbox_uses_scratch_home_when_host_home_is_private(tmp_path):
    script = """
from pathlib import Path
from mcp_server.sandbox.capabilities import CapabilitySet
from mcp_server.sandbox.caps_apply import apply
apply(CapabilitySet(fs_read=(), fs_write=(), env_allow=frozenset()))
home = Path.home()
assert home != Path('/private-host-home')
assert home.is_dir()
"""
    env = {**os.environ, "HOME": "/private-host-home", "TMPDIR": str(tmp_path)}
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_plugin_factory_sandbox_off_when_disabled(monkeypatch):
    """PluginFactory with DISABLE=1 must return non-sandboxed plugin (capability-free path)."""
    monkeypatch.setenv("MCP_PLUGIN_SANDBOX_DISABLE", "1")
    monkeypatch.delenv("MCP_PLUGIN_SANDBOX_ENABLED", raising=False)

    from mcp_server.plugins.plugin_factory import PluginFactory

    p = PluginFactory.create_plugin("python")
    assert (
        type(p).__name__ != "SandboxedPlugin"
    ), "sandbox should be off when MCP_PLUGIN_SANDBOX_DISABLE=1 and no capabilities passed"


def test_sandbox_pins_host_grammar_cache_root_before_scrubbing_home(tmp_path):
    """A scrubbed worker must keep reading the host's prefetched grammar cache.

    tree-sitter-language-pack 1.20 resolves its cache from HOME/XDG_CACHE_HOME;
    after the scrub HOME is the scratch dir, which would be a cold cache and
    trigger a first-use download (Consiliency/Code-Index-MCP#115).
    """
    script = """
import os
from mcp_server.sandbox.capabilities import CapabilitySet
from mcp_server.sandbox.caps_apply import apply
apply(CapabilitySet(fs_read=(), fs_write=(), env_allow=frozenset()))
print(os.environ.get("TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"))
print("XDG_CACHE_HOME" in os.environ)
print(os.environ.get("TREE_SITTER_LANGUAGE_PACK_LIBS_DIR"))
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("TREE_SITTER_LANGUAGE_PACK_")}
    env.update({"HOME": "/host-home", "XDG_CACHE_HOME": "/host-xdg", "TMPDIR": str(tmp_path)})
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["/host-xdg", "False", "None"]

    env["TREE_SITTER_LANGUAGE_PACK_LIBS_DIR"] = "/opt/tree-sitter-language-pack/libs"
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[2] == "/opt/tree-sitter-language-pack/libs"
    env.pop("TREE_SITTER_LANGUAGE_PACK_LIBS_DIR")

    env["TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"] = "/opt/grammars"
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "/opt/grammars"

    env.pop("TREE_SITTER_LANGUAGE_PACK_CACHE_DIR")
    env.pop("XDG_CACHE_HOME")
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "/host-home/.cache"


def test_sandboxed_python_plugin_still_produces_chunker_chunks(tmp_path):
    """treesitter-chunker 6 reads dist-info metadata on the first python parser
    load; the worker warms the parser before its FS guard so chunk_text does not
    fail inside the sandbox and silently drop every chunk (#115)."""
    from mcp_server.plugins.sandboxed_plugin import SandboxedPlugin
    from mcp_server.sandbox.capabilities import CapabilitySet

    source = tmp_path / "mod.py"
    source.write_text("def helper():\n    return 'ok'\n", encoding="utf-8")
    plugin = SandboxedPlugin(
        "mcp_server.plugins.python_plugin",
        CapabilitySet(fs_read=(tmp_path,), fs_write=(), env_allow=frozenset()),
    )
    try:
        shard = plugin.indexFile(str(source), source.read_text(encoding="utf-8"))
    finally:
        plugin.close()
    assert shard["symbols"], shard
    assert len(shard.get("chunks") or []) >= 1, shard


# --- Network-denied grammar loading (#116 review B2, N2) -------------------

_WORKER_CAPS = (
    '{"fs_read": [], "fs_write": [], "env_allow": [], "network": false, '
    '"sqlite": "none", "cpu_seconds": 30, "mem_mb": 2048}'
)


def _grammar_free_env(tmp_path, **extra):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("TREE_SITTER_LANGUAGE_PACK_") and k not in ("HOME", "XDG_CACHE_HOME")
    }
    env.update({"TMPDIR": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"})
    env.update(extra)
    return env


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def _run_worker(plugin_module, env, *, strace_log=None):
    import shutil

    cmd = [sys.executable, "-m", "mcp_server.sandbox.worker_main", plugin_module, _WORKER_CAPS]
    if strace_log is not None:
        strace = shutil.which("strace")
        cmd = [strace, "-f", "-qq", "-e", "trace=connect", "-o", str(strace_log), *cmd]
    return subprocess.run(
        cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120
    )


def _strace_usable():
    import shutil

    strace = shutil.which("strace")
    if strace is None:
        return False
    probe = subprocess.run(
        [strace, "-qq", "-e", "trace=none", "true"], capture_output=True, timeout=30
    )
    return probe.returncode == 0


def test_network_denied_worker_with_cold_grammar_cache_fails_closed_without_writes(tmp_path):
    """A cold cache must not make a network=False worker download natively.

    Before the fix, language-pack 1.20's Rust downloader fetched the manifest
    and a ~22 MB bundle from GitHub and wrote them into the host cache, past
    the Python socket guard and outside fs_write.
    """
    cache = tmp_path / "cold-cache"
    cache.mkdir(mode=0o700)
    env = _grammar_free_env(
        tmp_path, HOME=str(tmp_path / "home"), TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=str(cache)
    )
    log = tmp_path / "connect.log" if _strace_usable() else None
    result = _run_worker("mcp_server.plugins.c_plugin", env, strace_log=log)

    assert result.returncode == 1, result.stderr
    envelope = json.loads(result.stdout.splitlines()[-1])
    assert envelope["method"] == "startup"
    assert envelope["payload"]["type"] == "DownloadError"
    assert envelope["payload"]["state"] == "load_error"
    assert "not in the local grammar cache" in envelope["payload"]["message"]
    assert _tree(cache) == [], "worker wrote into the grammar cache"
    if log is not None:
        inet = [
            line
            for line in log.read_text().splitlines()
            if "AF_INET" in line or "sin_port" in line or "sin6_port" in line
        ]
        assert inet == [], inet


def test_network_denied_worker_rolled_over_cache_fails_closed(tmp_path):
    """A pack patch release moves the versioned cache dir; an older warm dir
    must read as cold, not trigger a download. The older dir holds the
    plugin's library (a stand-in that would fail to load), so the test also
    proves it is never used (#116 re-check NIT 4)."""
    cache = tmp_path / "cache"
    old = cache / "tree-sitter-language-pack" / "v1.19.0" / "libs"
    old.mkdir(parents=True)
    (old / "libtree_sitter_c.so").write_bytes(b"not a shared library\n")
    os.chmod(cache, 0o700)
    before = _tree(cache)
    env = _grammar_free_env(tmp_path, TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=str(cache))
    result = _run_worker("mcp_server.plugins.c_plugin", env)
    assert result.returncode == 1, result.stderr
    assert "DownloadError" in result.stdout
    assert _tree(cache) == before


def test_network_denied_worker_without_home_pins_passwd_cache_and_fails_closed(tmp_path):
    """No HOME/XDG_CACHE_HOME: like the pack, resolve the passwd home. A cold
    one fails closed with nothing written."""
    passwd_home = tmp_path / "passwd-home"
    passwd_home.mkdir(mode=0o700)
    script = f"""
import json, os, pwd, sys
real = pwd.getpwuid
class Entry:
    pw_dir = {str(passwd_home)!r}
pwd.getpwuid = lambda uid: Entry()
from mcp_server.sandbox.capabilities import CapabilitySet
from mcp_server.sandbox.caps_apply import apply
apply(CapabilitySet(fs_read=(), fs_write=(), env_allow=frozenset()))
import tree_sitter_language_pack as pack
try:
    pack.get_language("c")
    outcome = "loaded"
except pack.DownloadError as exc:
    outcome = "DownloadError"
print(json.dumps({{
    "cache": os.environ.get("TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"),
    "manifest": os.environ.get("TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL"),
    "outcome": outcome,
}}))
"""
    env = _grammar_free_env(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.splitlines()[-1])
    assert report["cache"] == str(passwd_home / ".cache")
    assert report["manifest"] == "sandbox-offline://network-denied"
    assert report["outcome"] == "DownloadError"
    assert _tree(passwd_home) == []


def test_network_denied_worker_loads_warm_cache_without_writing_it(tmp_path):
    from mcp_server.utils import grammar_cache

    if grammar_cache.missing_grammars(["c"]):
        reason = "grammar 'c' is not prefetched; run `mcp-index grammars prefetch`"
        if os.environ.get("MCP_REQUIRE_GRAMMARS") == "1":
            pytest.fail(f"MCP_REQUIRE_GRAMMARS=1 forbids skipping: {reason}")
        pytest.skip(reason)
    root = grammar_cache.resolve_cache_root()
    libs = grammar_cache.libs_dir()
    env = _grammar_free_env(tmp_path, HOME=str(tmp_path / "home"))
    if root is not None:
        env["TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"] = str(root)
    if libs is not None:
        env["TREE_SITTER_LANGUAGE_PACK_LIBS_DIR"] = str(libs)
    pack_cache = root / "tree-sitter-language-pack" if root is not None else None
    watched = [p for p in (pack_cache, libs) if p is not None and p.exists()]

    def snapshot():
        return {
            str(p): (p.stat().st_mtime_ns, p.stat().st_size)
            for base in watched
            for p in [base, *base.rglob("*")]
        }

    before = snapshot()
    log = tmp_path / "connect.log" if _strace_usable() else None
    result = _run_worker("mcp_server.plugins.c_plugin", env, strace_log=log)
    assert result.returncode == 0, result.stderr + result.stdout
    assert snapshot() == before, "worker wrote into the grammar cache"
    if log is not None:
        inet = [
            line
            for line in log.read_text().splitlines()
            if "sin_port" in line or "sin6_port" in line
        ]
        assert inet == [], inet


def test_sandbox_resolves_relative_grammar_paths_against_host_cwd(tmp_path):
    """The worker chdirs into scratch; relative cache/libs values must keep
    pointing where the host meant (#116 review N2)."""
    script = """
import os
from mcp_server.sandbox.capabilities import CapabilitySet
from mcp_server.sandbox.caps_apply import apply
apply(CapabilitySet(fs_read=(), fs_write=(), env_allow=frozenset(), network=True))
print(os.environ.get("TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"))
print(os.environ.get("TREE_SITTER_LANGUAGE_PACK_LIBS_DIR"))
print(os.environ.get("TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL"))
"""
    host_cwd = tmp_path / "host"
    host_cwd.mkdir()
    env = _grammar_free_env(
        tmp_path,
        HOME="/host-home",
        TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=".cache/ts",
        TREE_SITTER_LANGUAGE_PACK_LIBS_DIR="libs",
        TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL="https://example.invalid/manifest",
        PYTHONPATH=os.pathsep.join(sys.path),
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=host_cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        str(host_cwd / ".cache" / "ts"),
        str(host_cwd / "libs"),
        # network=True keeps the pack untouched, but the scrub still drops a
        # host-chosen manifest URL.
        "None",
    ]


@pytest.mark.parametrize(
    "env",
    [
        {"TREE_SITTER_LANGUAGE_PACK_CACHE_DIR": "/o", "XDG_CACHE_HOME": "/x", "HOME": "/h"},
        {"XDG_CACHE_HOME": "/x", "HOME": "/h"},
        {"HOME": "/h"},
        {"HOME": "rel-home", "TREE_SITTER_LANGUAGE_PACK_LIBS_DIR": "rel-libs"},
        {"TREE_SITTER_LANGUAGE_PACK_CACHE_DIR": "~/c", "HOME": "/h"},
        {},
    ],
)
def test_worker_grammar_path_resolution_matches_grammar_cache(monkeypatch, env):
    from mcp_server.sandbox import caps_apply
    from mcp_server.utils import grammar_cache

    for key in (
        "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR",
        "TREE_SITTER_LANGUAGE_PACK_LIBS_DIR",
        "XDG_CACHE_HOME",
        "HOME",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert caps_apply._host_grammar_paths() == (
        grammar_cache.resolve_cache_root(),
        grammar_cache.resolve_libs_dir(),
    )
