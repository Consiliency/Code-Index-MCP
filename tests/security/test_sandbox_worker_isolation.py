"""Sandbox workers import the host's ``mcp_server``, never a shadow copy (#146).

A worker started as ``python -m mcp_server.sandbox.worker_main`` puts its working
directory first on ``sys.path`` and honours ``PYTHONPATH`` and the user site, so a
checkout or environment containing a top-level ``mcp_server`` package would
replace the worker. These tests plant such a shadow package in both places.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import mcp_server
from mcp_server.plugins.sandboxed_plugin import (
    SandboxedPlugin,
    build_worker_command,
    host_package_root,
)
from mcp_server.sandbox.capabilities import CapabilitySet
from mcp_server.sandbox.supervisor import SandboxCallError, SandboxSupervisor

REPO_ROOT = Path(__file__).resolve().parents[2]
_CAPS_JSON = (
    '{"fs_read": [], "fs_write": [], "env_allow": [], "network": false, '
    '"sqlite": "none", "cpu_seconds": 30, "mem_mb": 2048}'
)


def _plant_shadow(base: Path, marker: Path) -> Path:
    """Create a hostile ``mcp_server`` (plus a plugin) that records any import."""
    shadow = base / "shadow"
    record = f"open({str(marker)!r}, 'a').write(__name__ + '\\n')\n"
    for rel in (
        "mcp_server/__init__.py",
        "mcp_server/sandbox/__init__.py",
        "mcp_server/sandbox/worker_main.py",
        "shadow_only_plugin/__init__.py",
    ):
        path = shadow / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(record)
    return shadow


@pytest.fixture
def hostile_env(tmp_path, monkeypatch):
    marker = tmp_path / "shadow-imported.txt"
    shadow = _plant_shadow(tmp_path, marker)
    monkeypatch.chdir(shadow)
    monkeypatch.setenv("PYTHONPATH", str(shadow))
    monkeypatch.setenv("PYTHONSTARTUP", str(shadow / "mcp_server" / "__init__.py"))
    monkeypatch.setenv("PYTHONUSERBASE", str(shadow))
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    return shadow, marker


def _mock_caps() -> CapabilitySet:
    return CapabilitySet(
        fs_read=(REPO_ROOT,),
        fs_write=(),
        env_allow=frozenset({"PATH", "HOME", "PYTHONPATH", "LANG", "LC_ALL"}),
        network=True,
        sqlite="none",
    )


def test_worker_command_is_isolated_and_never_uses_dash_m():
    cmd = build_worker_command(sys.executable, "pkg.plugin", _CAPS_JSON)
    assert cmd[0] == sys.executable
    assert cmd[1] == "-I"
    assert "-m" not in cmd
    assert cmd[-3:] == [host_package_root(), "pkg.plugin", _CAPS_JSON]


def test_host_package_root_is_the_imported_package_parent():
    pkg_dir = Path(next(iter(mcp_server.__path__))).resolve()
    assert Path(host_package_root()) == pkg_dir.parent


def test_sandboxed_plugin_ignores_shadow_package_in_cwd_and_pythonpath(hostile_env):
    _, marker = hostile_env
    plugin = SandboxedPlugin(
        "tests.security.fixtures.mock_plugin", _mock_caps(), gh_cmd=sys.executable
    )
    try:
        assert plugin.language == "mock"
        assert plugin.indexFile("x.py", "print(1)")["language"] == "mock"
    finally:
        plugin.close()
    assert not marker.exists(), marker.read_text()


def test_worker_cannot_load_a_plugin_that_exists_only_on_pythonpath(hostile_env):
    _, marker = hostile_env
    plugin = SandboxedPlugin("shadow_only_plugin", _mock_caps(), gh_cmd=sys.executable)
    try:
        with pytest.raises(SandboxCallError) as excinfo:
            plugin.supports("x.py")
        assert excinfo.value.details.get("state") == "unsupported"
    finally:
        plugin.close()
    assert not marker.exists(), marker.read_text()


def _probe(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_legacy_dash_m_launch_would_import_the_shadow(hostile_env):
    """Control: the pre-fix ``-m`` launch really is hijacked by this setup."""
    shadow, marker = hostile_env
    proc = _probe([sys.executable, "-m", "mcp_server.sandbox.worker_main", "x", _CAPS_JSON], shadow)
    assert proc.returncode == 0, proc.stderr
    assert "mcp_server" in marker.read_text()


def test_bootstrap_runs_the_host_worker_main(hostile_env):
    shadow, marker = hostile_env
    cmd = build_worker_command(sys.executable, "pkg.plugin", _CAPS_JSON)
    probe = textwrap.dedent("""\
        import runpy, sys
        def report(name, **kw):
            import mcp_server, mcp_server.sandbox.worker_main as wm
            print(sys.argv[1:])
            print(mcp_server.__file__)
            print(wm.__file__)
        runpy.run_module = report
        """)
    cmd[3] = probe + cmd[3]
    proc = _probe(cmd, shadow)
    assert proc.returncode == 0, proc.stderr
    argv, pkg_file, worker_file = proc.stdout.splitlines()
    assert argv == str(["pkg.plugin", _CAPS_JSON])
    root = Path(host_package_root())
    assert Path(pkg_file).resolve().parent.parent == root
    assert Path(worker_file).resolve().parent.parent.parent == root
    assert not marker.exists(), marker.read_text()


_SPEC_PROBE = textwrap.dedent("""\
    import runpy
    def report(name, **kw):
        import importlib.util
        print(importlib.util.find_spec("mcp_server").origin)
    runpy.run_module = report
    """)


def test_bootstrap_resolves_host_package_without_any_install(hostile_env):
    """A plain source checkout (nothing on site-packages) still finds the host copy."""
    shadow, marker = hostile_env
    cmd = build_worker_command(sys.executable, "pkg.plugin", _CAPS_JSON)
    cmd[1:2] = ["-I", "-S"]  # -S: no site-packages, so no editable finder either
    cmd[4] = _SPEC_PROBE + cmd[4]
    proc = _probe(cmd, shadow)
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()).resolve().parent.parent == Path(host_package_root())
    assert not marker.exists(), marker.read_text()


def test_bootstrap_fails_closed_when_package_resolves_elsewhere(tmp_path):
    cmd = build_worker_command(sys.executable, "pkg.plugin", _CAPS_JSON)
    cmd[3] = _SPEC_PROBE + cmd[3]
    cmd[4] = str(tmp_path)  # a root that does not hold the importable mcp_server
    proc = _probe(cmd, tmp_path)
    assert proc.returncode == 3
    assert proc.stdout == ""
    assert "does not resolve to the host package" in proc.stderr


_ROOT_PROBE_PLUGIN = textwrap.dedent("""\
    import os

    import mcp_server.plugin_base as plugin_base
    from mcp_server.plugin_base import IPlugin


    class Plugin(IPlugin):
        lang = os.path.realpath(plugin_base.__file__)

        def __init__(self, sqlite_store=None, enable_semantic=False):
            pass

        def supports(self, path):
            return True

        def indexFile(self, path, content):
            return {"file": str(path), "symbols": [], "language": "probe"}

        def getDefinition(self, symbol):
            return None

        def findReferences(self, symbol):
            return []

        def search(self, query, opts=None):
            return []
    """)


def test_hidden_user_site_root_never_outranks_stdlib_or_installed_packages(tmp_path):
    """A ``pip install --user`` root (hidden by ``-I``) is appended, never prepended.

    The simulated user site holds a full ``mcp_server`` copy next to fake
    standard-library and dependency modules. The host copy that the default
    path resolves (the editable install) must not be used either: the worker
    loads ``mcp_server`` and every submodule from the pinned root only.
    """
    marker = tmp_path / "fake-stdlib-imported.txt"
    user_site = tmp_path / "user-site"
    user_site.mkdir()
    shutil.copytree(
        Path(host_package_root()) / "mcp_server",
        user_site / "mcp_server",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    record = f"open({str(marker)!r}, 'a').write(__name__ + '\\n')\n"
    for name in ("dataclasses", "json", "inspect", "typing_extensions"):
        (user_site / f"{name}.py").write_text(record)
    (user_site / "user_site_probe_plugin.py").write_text(_ROOT_PROBE_PLUGIN)

    cmd = build_worker_command(sys.executable, "user_site_probe_plugin", _CAPS_JSON)
    cmd[4] = str(user_site)
    sup = SandboxSupervisor(cmd, CapabilitySet((), (), frozenset()))
    try:
        loaded_from = Path(sup.call("language", {})["value"])
    finally:
        sup.close()

    assert loaded_from == (user_site / "mcp_server" / "plugin_base.py").resolve()
    assert not marker.exists(), marker.read_text()


def test_root_only_reachable_through_bootstrap_is_appended_after_stdlib(tmp_path):
    """Reviewer repro: with nothing else resolving ``mcp_server`` (``-S`` hides
    every site dir, as ``-I`` hides a user site), the root must still be appended,
    so a ``dataclasses.py`` beside the package never shadows the standard library."""
    marker = tmp_path / "fake-stdlib-imported.txt"
    user_site = tmp_path / "user-site"
    (user_site / "mcp_server").mkdir(parents=True)
    (user_site / "mcp_server" / "__init__.py").write_text("")
    record = f"open({str(marker)!r}, 'a').write(__name__ + '\\n')\n"
    for name in ("dataclasses", "json", "inspect"):
        (user_site / f"{name}.py").write_text(record)
    probe = textwrap.dedent("""\
        import runpy, sys
        def report(name, **kw):
            import dataclasses, inspect, json, mcp_server
            print(mcp_server.__file__)
            print(sys.path[-1])
        runpy.run_module = report
        """)
    cmd = build_worker_command(sys.executable, "pkg.plugin", _CAPS_JSON)
    cmd[1:2] = ["-I", "-S"]
    cmd[4] = probe + cmd[4]
    cmd[5] = str(user_site)
    proc = _probe(cmd, tmp_path)
    assert proc.returncode == 0, proc.stderr
    pkg_file, last_path_entry = proc.stdout.splitlines()
    assert Path(pkg_file).resolve() == (user_site / "mcp_server" / "__init__.py").resolve()
    assert last_path_entry == str(user_site)
    assert not marker.exists(), marker.read_text()
