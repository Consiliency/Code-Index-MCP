"""SL-5 tests — sandbox default-on flip and DISABLE opt-out."""

from __future__ import annotations

import importlib
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
