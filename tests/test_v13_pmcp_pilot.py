"""Receipt reduction must not promote incomplete installed or live evidence."""

import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.v13_pmcp_pilot import (
    GOALS,
    REHEARSAL_INDEX_DELAY_SECONDS,
    OwnedContainer,
    OwnedProcess,
    PilotRefused,
    cgroup_processes,
    digest_json,
    validate_receipt,
    verify_saved_receipt,
)


def test_plugin_discovery_does_not_construct_declared_language(tmp_path):
    from mcp_server.plugin_system.discovery import PluginDiscovery

    class Plugin:
        lang = "python"

        def __init__(self, sqlite_store):
            raise AssertionError("plugin construction during discovery")

        def get_language(self):
            return self.lang

    assert PluginDiscovery()._extract_language(Plugin, tmp_path / "unknown_plugin.py") == "python"


@pytest.mark.parametrize("detach_at_shutdown", [False, True])
def test_owned_scope_catches_detached_children(tmp_path, detach_at_shutdown):
    marker = tmp_path / "child.pid"
    program = r"""
import os, signal, sys, time
def detach(*args):
    pid = os.fork()
    if pid == 0:
        os.setsid()
        signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))
        with open(sys.argv[1], 'w') as stream:
            stream.write(str(os.getpid()))
        time.sleep(60)
        sys.exit(0)
    if args:
        sys.exit(0)
if sys.argv[2] == 'late':
    signal.signal(signal.SIGTERM, detach)
else:
    detach()
time.sleep(60)
"""
    owner = OwnedProcess(
        [sys.executable, "-c", program, str(marker), "late" if detach_at_shutdown else "early"],
        tmp_path,
        dict(os.environ),
        "owned-test",
    )
    try:
        if not detach_at_shutdown:
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert marker.exists()
        else:
            time.sleep(0.2)
        owner.observe()
        owner.stop()
        assert not cgroup_processes(owner.group)
        assert owner.peak_rss_mib > 0
        if detach_at_shutdown:
            # A child born after group SIGTERM must remain a failed receipt even
            # though final SIGKILL cleanup removes it from the owned cgroup.
            assert owner.survivors and owner.exit_seconds >= 5
        else:
            assert not owner.survivors and owner.exit_seconds <= 5
    finally:
        if not owner.log.closed:
            owner.stop()


@pytest.mark.parametrize("elapsed,exit_code", [(1, 0), (6, 0), (1, 137)])
def test_container_retirement_records_slow_and_forced_exit(
    monkeypatch, tmp_path, manifest, elapsed, exit_code
):
    from scripts import v13_pmcp_pilot as pilot

    owner = OwnedContainer.__new__(OwnedContainer)
    owner.container, owner.root, owner.group = "a" * 64, tmp_path, tmp_path / "absent-cgroup"
    owner.peak_rss_mib = 80
    clock = iter([0, elapsed])
    monkeypatch.setattr(pilot.time, "monotonic", lambda: next(clock))
    calls = []

    def command(args, *rest, **kwargs):
        calls.append((args, kwargs))
        return (
            json.dumps({"Running": False, "ExitCode": exit_code, "OOMKilled": False})
            if args[1] == "inspect"
            else ""
        )

    monkeypatch.setattr(pilot, "run_command", command)
    owner.stop()
    value = receipt(manifest, "live")
    measured = value["resource_owners"][-1]
    measured.update(
        shutdown_seconds=owner.exit_seconds,
        peak_rss_mib=owner.peak_rss_mib,
        survivors=owner.survivors,
        exit_state=owner.exit_state,
    )
    value.update(
        shutdown_seconds=[1, owner.exit_seconds],
        surviving_children=owner.survivors,
        peak_rss_mib=60 + owner.peak_rss_mib,
    )
    if elapsed > 5 or exit_code == 137:
        with pytest.raises(PilotRefused, match="operational"):
            validate_receipt(value, manifest, "live")
    else:
        validate_receipt(value, manifest, "live")
    assert calls[0][1]["timeout"] == 6
    assert calls[-1][0] == ["docker", "rm", "--force", owner.container]


@pytest.fixture
def manifest():
    return {"source": "a" * 40, "tree": "b" * 40, "wheel_sha256": "c" * 64}


def receipt(manifest, kind):
    from scripts.v13_pmcp_pilot import QDRANT_IMAGE

    unit = "code-index-v13-" + "b" * 32 + ".scope"
    return {
        "kind": kind,
        "manifest_sha256": digest_json(manifest),
        "source": manifest["source"],
        "wheel_sha256": manifest["wheel_sha256"],
        "goals": dict.fromkeys(GOALS[kind], True),
        "shutdown_seconds": [1, 3.1],
        "surviving_children": [],
        "peak_rss_mib": 100,
        "qdrant_measured": True,
        "resource_owners": [
            {
                "kind": "process",
                "identity": unit,
                "pid": 123,
                "cgroup": "/sys/fs/cgroup/user.slice/" + unit,
                "shutdown_seconds": 1,
                "peak_rss_mib": 60,
                "survivors": [],
                "exit_state": {"running": False, "exit_code": 0, "oom_killed": False},
            },
            {
                "kind": "qdrant",
                "identity": "a" * 64,
                "pid": 456,
                "cgroup": "/sys/fs/cgroup/system.slice/docker-" + "a" * 64 + ".scope",
                "image": QDRANT_IMAGE,
                "shutdown_seconds": 3.1,
                "peak_rss_mib": 40,
                "survivors": [],
                "exit_state": {"running": False, "exit_code": 0, "oom_killed": False},
            },
        ],
        "latencies_ms": {"symbol": [20] * 40, "lexical": [60] * 40, "semantic": [100] * 40},
        "contention_successes": dict.fromkeys(("symbol", "lexical", "semantic"), 20),
        "budget": {"reserved_input_units": 2000, "elapsed_seconds": 20, "blocked": None},
    }


@pytest.mark.parametrize("kind", ["offline", "live", "browser"])
def test_complete_receipt_contract(manifest, kind):
    validate_receipt(receipt(manifest, kind), manifest, kind)


@pytest.mark.parametrize(
    "damage",
    [
        "missing_flag",
        "false_flag",
        "no_owners",
        "missing_qdrant",
        "duplicate_qdrant",
        "rss",
        "duration",
        "exit_state",
        "identity",
        "group",
    ],
)
def test_live_receipt_requires_complete_owned_measurements(manifest, damage):
    value = receipt(manifest, "live")
    if damage == "missing_flag":
        value.pop("qdrant_measured")
    elif damage == "false_flag":
        value["qdrant_measured"] = False
    elif damage == "no_owners":
        value.pop("resource_owners")
    elif damage == "missing_qdrant":
        value["resource_owners"].pop()
    elif damage == "duplicate_qdrant":
        value["resource_owners"].append(copy.deepcopy(value["resource_owners"][-1]))
    elif damage == "rss":
        value["peak_rss_mib"] = 60
    elif damage == "duration":
        value["shutdown_seconds"] = [1]
    elif damage == "exit_state":
        value["resource_owners"][-1]["exit_state"]["exit_code"] = 137
    elif damage == "identity":
        value["resource_owners"][-1]["identity"] = "c" * 64
    else:
        value["resource_owners"][-1]["cgroup"] = "/outside"
    with pytest.raises(PilotRefused):
        validate_receipt(value, manifest, "live")


@pytest.mark.parametrize("field", ["source", "wheel_sha256", "manifest_sha256"])
def test_candidate_or_manifest_drift_refused(manifest, field):
    value = receipt(manifest, "offline")
    value[field] = "wrong"
    with pytest.raises(PilotRefused, match="binding"):
        validate_receipt(value, manifest, "offline")


@pytest.mark.parametrize("kind", ["offline", "live", "browser"])
def test_missing_or_non_boolean_goal_cannot_pass(manifest, kind):
    value = receipt(manifest, kind)
    name = next(iter(GOALS[kind]))
    for missing in (False, True):
        broken = copy.deepcopy(value)
        if missing:
            del broken["goals"][name]
        else:
            broken["goals"][name] = "passed"
        with pytest.raises(PilotRefused, match="goals"):
            validate_receipt(broken, manifest, kind)


@pytest.mark.parametrize(
    "field,value",
    [
        ("shutdown_seconds", [5.01]),
        ("surviving_children", [123]),
        ("peak_rss_mib", 2048.01),
    ],
)
def test_operational_thresholds_are_not_advisory(manifest, field, value):
    result = receipt(manifest, "offline")
    result[field] = value
    with pytest.raises(PilotRefused, match="operational"):
        validate_receipt(result, manifest, "offline")


@pytest.mark.parametrize(
    "field,value",
    [
        ("reserved_input_units", 100001),
        ("elapsed_seconds", 900.01),
        ("blocked", "unsettled_transport"),
    ],
)
def test_live_allowance_limits_are_enforced(manifest, field, value):
    result = receipt(manifest, "live")
    result["budget"][field] = value
    with pytest.raises(PilotRefused, match="budget"):
        validate_receipt(result, manifest, "live")


@pytest.mark.parametrize("kind", ["symbol", "lexical", "semantic"])
def test_refusals_do_not_count_as_successful_contention_samples(manifest, kind):
    result = receipt(manifest, "live")
    result["contention_successes"][kind] = 19
    result["contention_refusals"] = {kind: 100}
    with pytest.raises(PilotRefused, match="contention"):
        validate_receipt(result, manifest, "live")


@pytest.mark.parametrize("kind,value", [("symbol", 101), ("lexical", 501), ("semantic", 501)])
def test_latency_threshold_and_missing_samples_refused(manifest, kind, value):
    result = receipt(manifest, "live")
    for values in ([value] * 40, []):
        result["latencies_ms"][kind] = values
        with pytest.raises(PilotRefused, match="latency"):
            validate_receipt(result, manifest, "live")


def test_browser_goals_without_artifacts_cannot_pass(tmp_path, manifest):
    (tmp_path / "browser.json").write_text(json.dumps(receipt(manifest, "browser")))
    with pytest.raises(PilotRefused, match="artifacts"):
        verify_saved_receipt(tmp_path, manifest, "browser")


@pytest.mark.asyncio
async def test_browser_teardown_attempts_every_owned_process():
    from scripts.v13_pmcp_pilot import stop_browser_processes

    calls = []

    class Owner:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        def stop(self):
            calls.append(self.name)
            if self.fail:
                raise RuntimeError("stop failed")

    assert await stop_browser_processes([Owner("admin"), Owner("inspector", True)])
    assert calls == ["inspector", "admin"]


def test_pilot_fingerprint_includes_configured_enrichment_identity():
    from mcp_server.artifacts.semantic_profiles import SemanticProfile
    from scripts.v13_pmcp_pilot import expected_profile_fingerprint

    profile = {
        "provider": "openai_compatible",
        "model_name": "embed",
        "model_version": "unreported",
        "vector_dimension": 8,
        "distance_metric": "cosine",
        "normalization_policy": "provider-default",
        "chunk_schema_version": "1",
        "chunker_version": "4.0.0",
        "build_metadata": {
            "enrichment_model_name": "chat",
            "enrichment_api_base": "http://127.0.0.1:1234/v1",
        },
    }
    explicit = copy.deepcopy(profile)
    explicit["build_metadata"].update(
        enrichment_model="chat", enrichment_base_url="http://127.0.0.1:1234/v1"
    )
    assert (
        expected_profile_fingerprint({"selected_profile": profile})
        == SemanticProfile.from_dict("pilot", explicit).compatibility_fingerprint
    )


@pytest.mark.parametrize("valid", [True, False])
def test_prepare_delivered_wheel_never_builds(tmp_path, monkeypatch, valid):
    from scripts import release_smoke
    from scripts import v13_pmcp_pilot as pilot

    wheel = tmp_path / "index_it_mcp-1.4.1-py3-none-any.whl"
    wheel.write_bytes(b"registry wheel fixture")
    root = tmp_path / "owned"
    calls = []
    monkeypatch.setattr(pilot, "source_identity", lambda: {"source": "a" * 40})
    monkeypatch.setattr(
        pilot,
        "pmcp_distribution_identity",
        lambda path, expected=None: {"version": "3.0.3", "node_executable": None},
    )
    monkeypatch.setattr(
        pilot, "browser_node_identity", lambda path: {"path": str(path), "sha256": "node"}
    )
    monkeypatch.setattr(release_smoke, "validate_wheel_source", lambda *args: {"version": "1.4.1"})

    def run(command, directory, label, **kwargs):
        assert command[:2] != ["uv", "build"]
        calls.append(command)
        if label == "pilot-lock-export":
            (directory / "constraints.txt").write_text("dependency==1\n")
        return '{"version": "1.4.1"}' if label == "installed-identity" else "pmcp 3.0.3"

    monkeypatch.setattr(pilot, "run_command", run)
    digest = pilot.digest_file(wheel) if valid else "0" * 64
    if not valid:
        with pytest.raises(PilotRefused, match="digest_mismatch"):
            pilot.prepare(root, wheel, digest)
        assert not calls
    else:
        result = pilot.prepare(root, wheel, digest)
        assert result["artifact_origin"] == "registry"
        assert result["wheel_sha256"] == digest
        assert (root / "dist" / wheel.name).read_bytes() == wheel.read_bytes()
        assert (root / "dist" / wheel.name).as_uri() in " ".join(result["uvx_prefix"])


CONSTRAINTS = "dependency==1\ntreesitter-chunker==7.0.0 \\\n    --hash=sha256:00\n"


@pytest.mark.parametrize(
    "field",
    [
        "schema",
        "uvx_prefix",
        "python",
        "installed",
        "pmcp_path",
        "pmcp_sha256",
        "pmcp_distribution",
        "browser_node",
        "pmcp_version",
        "helper_sha256",
        "grammar_cache",
    ],
)
def test_prepared_runtime_identity_rejects_manifest_tampering(tmp_path, monkeypatch, field):
    from scripts import v13_pmcp_pilot as pilot

    (tmp_path / "dist").mkdir()
    wheel = tmp_path / "dist/fixture.whl"
    wheel.write_bytes(b"wheel")
    (tmp_path / "constraints.txt").write_text(CONSTRAINTS)
    executable = tmp_path / "pmcp"
    executable.write_bytes(b"#!/bin/sh\n")
    monkeypatch.setattr(pilot, "source_identity", lambda: {"source": "a" * 40})
    monkeypatch.setattr(pilot.shutil, "which", lambda name: str(executable))
    monkeypatch.setattr(
        pilot, "pmcp_distribution_identity", lambda path, expected=None: {"version": "3.0.3"}
    )
    node_identity = {"path": str(tmp_path / "node"), "sha256": "node"}
    monkeypatch.setattr(pilot, "browser_node_identity", lambda path: node_identity)
    for name in pilot.PILOT_HELPERS:
        (tmp_path / name).write_bytes((pilot.REPO / "scripts" / name).read_bytes())
    python = str(Path(sys.base_prefix) / "bin" / pilot.PILOT_PYTHON_BIN)
    manifest = {
        "schema": "v13-pilot-manifest.v1",
        "source": "a" * 40,
        "wheel": wheel.name,
        "wheel_sha256": pilot.digest_file(wheel),
        "constraints_sha256": pilot.digest_file(tmp_path / "constraints.txt"),
        "wheel_source_contract": {"version": "1.4.1"},
        "installed": {
            "version": "1.4.1",
            "python_version": [*pilot.PILOT_PYTHON, 0],
            "wheel_files_verified": 2,
            "baml_files_verified": 1,
            "treesitter_chunker_version": "7.0.0",
        },
        "uvx_prefix": pilot.uvx_prefix(tmp_path, wheel, python),
        "python": python,
        "grammar_cache": str(tmp_path),
        "pmcp_path": str(executable),
        "pmcp_sha256": pilot.digest_file(executable),
        "pmcp_distribution": {"version": "3.0.3"},
        "browser_node": node_identity,
        "pmcp_version": "pmcp 3.0.3",
        "helper_sha256": {name: pilot.digest_file(tmp_path / name) for name in pilot.PILOT_HELPERS},
    }
    pilot.validate_manifest(tmp_path, manifest)
    # A manifest whose prefetched grammar cache is gone cannot start any mode (#127).
    with pytest.raises(PilotRefused, match="grammar_cache_unavailable"):
        pilot.validate_manifest(tmp_path, {**manifest, "grammar_cache": str(tmp_path / "gone")})
    original = manifest[field]
    manifest[field] = "tampered"
    with pytest.raises(PilotRefused):
        pilot.validate_manifest(tmp_path, manifest)
    manifest[field] = original
    # The installed chunker must be the one the exported lock pins.
    for chunker_version in (None, "6.0.0"):
        installed = {**manifest["installed"], "treesitter_chunker_version": chunker_version}
        with pytest.raises(PilotRefused, match="installed_chunker_changed"):
            pilot.validate_manifest(tmp_path, {**manifest, "installed": installed})

    for artifact, rejection in (
        (wheel, "wheel_binding_changed"),
        (tmp_path / "constraints.txt", "constraints_binding_changed"),
    ):
        artifact.write_bytes(b"tampered")
        monkeypatch.setattr(
            pilot,
            "run_command",
            lambda *args, **kwargs: pytest.fail("executed untrusted artifact"),
        )
        with pytest.raises(PilotRefused, match=rejection):
            pilot.validate_manifest(tmp_path, manifest, execute=True)
        artifact.write_bytes(b"wheel" if artifact == wheel else CONSTRAINTS.encode())


def test_pilot_profile_has_no_hard_coded_chunker_version():
    """Review of #148: the pilot profile must fingerprint the installed chunker,
    not a literal (it was ``"4.0.0"``), or v6 and v7 runs share a fingerprint."""
    import ast

    from scripts import v13_pmcp_pilot as pilot

    tree = ast.parse(Path(pilot.__file__).read_text(encoding="utf-8"))
    values = [
        value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant) and key.value == "chunker_version"
    ]
    assert values, "pilot no longer configures a semantic profile chunker_version"
    assert not any(isinstance(value, ast.Constant) for value in values), [
        ast.unparse(value) for value in values
    ]


def test_pilot_chunker_version_matches_installed_distribution():
    """The configured profile's chunker_version is the installed distribution,
    in the automatic-detection format, so it moves with the chunker."""
    from importlib.metadata import version

    from mcp_server.artifacts.semantic_profiles import SemanticProfile
    from mcp_server.config.settings import Settings
    from scripts import v13_pmcp_pilot as pilot

    installed = {"treesitter_chunker_version": version("treesitter-chunker")}
    label = pilot.chunker_version_label(installed)
    assert label == Settings()._detect_treesitter_chunker_version()
    assert label == f"treesitter-chunker@{version('treesitter-chunker')}"
    with pytest.raises(PilotRefused, match="installed_chunker_unknown"):
        pilot.chunker_version_label({})

    def fingerprint(chunker: str) -> str:
        profile = {
            "provider": "openai_compatible",
            "model_name": "embed",
            "model_version": "unreported",
            "vector_dimension": 8,
            "distance_metric": "cosine",
            "normalization_policy": "provider-default",
            "chunk_schema_version": "1",
            "chunker_version": pilot.chunker_version_label({"treesitter_chunker_version": chunker}),
        }
        return SemanticProfile.from_dict("pilot", profile).compatibility_fingerprint

    assert fingerprint("6.0.0") != fingerprint("7.0.0")


def test_constrained_version_reads_uv_export_pins():
    from scripts import v13_pmcp_pilot as pilot

    assert pilot.constrained_version(CONSTRAINTS, "treesitter-chunker") == "7.0.0"
    assert pilot.constrained_version(CONSTRAINTS, "dependency") == "1"
    assert pilot.constrained_version("xtreesitter-chunker==1\n", "treesitter-chunker") is None
    # Canonical-name, extras, markers, hashes and comments are accepted.
    assert (
        pilot.constrained_version(
            'TreeSitter_Chunker[extra]==7.0.0 ; python_version >= "3.14" \\\n'
            "    --hash=sha256:00  # via index-it-mcp\n",
            "treesitter-chunker",
        )
        == "7.0.0"
    )
    # Anything other than one exact pin is unusable.
    assert pilot.constrained_version("treesitter-chunker>=7\n", "treesitter-chunker") is None
    assert (
        pilot.constrained_version(
            "treesitter-chunker==7.0.0\ntreesitter-chunker==6.0.0\n", "treesitter-chunker"
        )
        is None
    )


def test_pmcp_interpreter_digest_checked_before_execution(tmp_path, monkeypatch):
    from scripts import v13_pmcp_pilot as pilot

    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"tampered interpreter")
    launcher = tmp_path / "pmcp"
    launcher.write_bytes(f"#!{interpreter}\n".encode())
    monkeypatch.setattr(
        pilot.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("executed tampered interpreter"),
    )
    with pytest.raises(PilotRefused, match="pmcp_interpreter_changed"):
        pilot.pmcp_distribution_identity(
            launcher,
            expected={"interpreter": str(interpreter), "interpreter_sha256": "0" * 64},
        )


@pytest.mark.parametrize("changed_file", ["dependency.py", "startup.pth"])
def test_pmcp_runtime_drift_rejected_before_interpreter_execution(
    tmp_path, monkeypatch, changed_file
):
    from scripts import v13_pmcp_pilot as pilot

    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"trusted interpreter")
    launcher = tmp_path / "pmcp"
    launcher.write_bytes(f"#!{interpreter}\n".encode())
    site = tmp_path / "site-packages"
    site.mkdir()
    (site / "dependency.py").write_text("value = 1\n")
    (site / "startup.pth").write_text("# startup\n")
    monkeypatch.setattr(pilot, "pmcp_runtime_roots", lambda executable, python: [site])
    runtime_sha256, runtime_files = pilot.pmcp_runtime_digest([site])
    expected = {
        "launcher": str(launcher),
        "launcher_sha256": pilot.digest_file(launcher),
        "console": str(launcher),
        "console_sha256": pilot.digest_file(launcher),
        "interpreter": str(interpreter),
        "interpreter_sha256": pilot.digest_file(interpreter),
        "runtime_sha256": runtime_sha256,
        "runtime_files_verified": runtime_files,
    }
    (site / changed_file).write_text("changed\n")
    monkeypatch.setattr(
        pilot.importlib.metadata,
        "distributions",
        lambda **kwargs: pytest.fail("inspected unverified distribution"),
    )
    monkeypatch.setattr(
        pilot.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("executed unverified interpreter"),
    )
    with pytest.raises(PilotRefused, match="pmcp_runtime_changed"):
        pilot.pmcp_distribution_identity(launcher, expected=expected)


def test_pmcp_external_pth_path_is_refused_before_interpreter_execution(tmp_path, monkeypatch):
    from scripts import v13_pmcp_pilot as pilot

    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"trusted interpreter")
    launcher = tmp_path / "pmcp"
    launcher.write_bytes(f"#!{interpreter}\n".encode())
    site = tmp_path / "site-packages"
    site.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "dependency.py").write_text("value = 1\n")
    (site / "external.pth").write_text(f"{external}\n")
    monkeypatch.setattr(pilot, "pmcp_runtime_roots", lambda executable, python: [site])
    monkeypatch.setattr(
        pilot.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("executed unbounded interpreter"),
    )
    with pytest.raises(PilotRefused, match="pmcp_runtime_unbounded"):
        pilot.pmcp_distribution_identity(launcher)


def test_pmcp_release_wrapper_binds_python_314_and_node_runtime(tmp_path, monkeypatch):
    from scripts import v13_pmcp_pilot as pilot

    release = tmp_path / "release"
    launcher = release / "bin/pmcp"
    console = release / "python/bin/pmcp"
    interpreter = release / "python/bin/python3.14"
    node_bin = release / "node-v24/bin"
    site = release / "python/lib/python3.14/site-packages"
    for directory in (launcher.parent, console.parent, node_bin, site):
        directory.mkdir(parents=True, exist_ok=True)
    interpreter.write_bytes(b"trusted python")
    console.write_text(f"#!{interpreter}\n")
    config = release / "python/pyvenv.cfg"
    config.write_text(f"home = {interpreter.parent}\ninclude-system-site-packages = false\n")
    node = node_bin / "node"
    node.write_bytes(b"trusted node")
    node.chmod(0o755)
    npm = node_bin.parent / "lib/node_modules/npm/index.js"
    npm.parent.mkdir(parents=True)
    npm.write_text("trusted npm\n")
    (site / "dependency.py").write_text("value = 1\n")
    launcher.write_text(f'#!/bin/sh\nexport PATH={node_bin}:"$PATH"\nexec {console} "$@"\n')
    assert pilot.pmcp_console_script(launcher) == (console, node_bin.parent)
    assert site in pilot.pmcp_runtime_roots(console, interpreter)
    monkeypatch.setattr(pilot, "pmcp_runtime_roots", lambda executable, python: [site])
    roots = [site, node_bin.parent]
    runtime_sha256, runtime_files = pilot.pmcp_runtime_digest(roots)
    expected = {
        "launcher": str(launcher),
        "launcher_sha256": pilot.digest_file(launcher),
        "console": str(console),
        "console_sha256": pilot.digest_file(console),
        "venv_sha256": pilot.digest_file(config),
        "node_executable": str(node),
        "interpreter": str(interpreter),
        "interpreter_sha256": pilot.digest_file(interpreter),
        "runtime_sha256": runtime_sha256,
        "runtime_files_verified": runtime_files,
    }
    monkeypatch.setattr(
        pilot.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("executed unverified PMCP runtime"),
    )
    node.write_bytes(b"changed node")
    with pytest.raises(PilotRefused, match="pmcp_runtime_changed"):
        pilot.pmcp_distribution_identity(launcher, expected=expected)
    node.write_bytes(b"trusted node")
    config.write_text(config.read_text() + "# changed\n")
    with pytest.raises(PilotRefused, match="pmcp_runtime_changed"):
        pilot.pmcp_distribution_identity(launcher, expected=expected)
    config.write_text(f"home = {interpreter.parent}\ninclude-system-site-packages = false\n")
    npm.write_text("changed npm\n")
    with pytest.raises(PilotRefused, match="pmcp_runtime_changed"):
        pilot.pmcp_distribution_identity(launcher, expected=expected)
    npm.write_text("trusted npm\n")
    node.chmod(0o700)
    with pytest.raises(PilotRefused, match="pmcp_runtime_changed"):
        pilot.pmcp_distribution_identity(launcher, expected=expected)
    node.chmod(0o755)
    console.write_text(f"#!{interpreter}\n# changed\n")
    with pytest.raises(PilotRefused, match="pmcp_launcher_changed"):
        pilot.pmcp_distribution_identity(launcher, expected=expected)
    launcher.write_text(launcher.read_text() + "echo unbound\n")
    with pytest.raises(PilotRefused, match="pmcp_launcher_unknown"):
        pilot.pmcp_distribution_identity(launcher, expected=expected)


def _pmcp3_release(base: Path, name: str, interpreter: Path) -> Path:
    """Lay out one dev0 tooling release the way pmcp 3.x deploys it."""
    release = base / "archive" / name
    python = release / "python"
    site = python / "lib/python3.14/site-packages"
    node_bin = release / "node-v24.22.3-linux-x64/bin"
    for directory in (release / "bin", python / "bin", site / "pmcp", node_bin):
        directory.mkdir(parents=True, exist_ok=True)
    # A venv interpreter: python -> python3 -> the base python3.14.
    (python / "bin/python3").symlink_to(interpreter)
    (python / "bin/python").symlink_to("python3")
    (python / "pyvenv.cfg").write_text(
        f"home = {interpreter.parent}\ninclude-system-site-packages = false\n" "version = 3.14.4\n"
    )
    named = base / "releases" / name
    (python / "bin/pmcp").write_text(
        f"#!{named}/python/bin/python\nfrom pmcp.cli import main\nmain()\n"
    )
    node = node_bin / "node"
    node.write_bytes(b"trusted node")
    node.chmod(0o755)
    (site / "pmcp/__init__.py").write_text('__version__ = "3.0.3"\n')
    info = site / "pmcp-3.0.3.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.4\nName: pmcp\nVersion: 3.0.3\n")
    (info / "RECORD").write_text(
        "pmcp/__init__.py,,\npmcp-3.0.3.dist-info/METADATA,,\npmcp-3.0.3.dist-info/RECORD,,\n"
    )
    (release / "bin/pmcp").write_text(
        "#!/bin/sh\n"
        f'export PATH={named}/node-v24.22.3-linux-x64/bin:"$PATH"\n'
        f'exec {named}/python/bin/pmcp "$@"\n'
    )
    # dev0 moved the release to an archive volume and left a symlink behind.
    named.parent.mkdir(exist_ok=True)
    named.symlink_to(release)
    return named


def test_pmcp3_archived_release_wrapper_binds_resolved_release(tmp_path, monkeypatch):
    """#133: a pmcp 3.x release archived behind a symlink still binds."""
    from scripts import v13_pmcp_pilot as pilot

    interpreter = tmp_path / "base/bin/python3.14"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"trusted python")
    named = _pmcp3_release(tmp_path, "sync-1", interpreter)
    # The pilot binds the resolved launcher, which lands in the archive.
    launcher = (named / "bin/pmcp").resolve()
    assert launcher.is_relative_to(tmp_path / "archive")
    console, node_root = pilot.pmcp_console_script(launcher)
    assert console == named / "python/bin/pmcp"
    assert node_root == named / "node-v24.22.3-linux-x64"
    monkeypatch.setattr(
        pilot.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("executed unverified PMCP runtime"),
    )
    identity = pilot.pmcp_distribution_identity(launcher)
    assert identity["version"] == pilot.PMCP_VERSION == "3.0.3"
    assert identity["console"] == str(console)
    assert identity["node_executable"] == str(node_root / "bin/node")
    assert pilot.pmcp_distribution_identity(launcher, expected=identity) == identity
    (named / "python/lib/python3.14/site-packages/pmcp/__init__.py").write_text("changed\n")
    with pytest.raises(PilotRefused, match="pmcp_runtime_changed"):
        pilot.pmcp_distribution_identity(launcher, expected=identity)


def test_pmcp3_wrapper_naming_another_release_is_refused(tmp_path):
    from scripts import v13_pmcp_pilot as pilot

    interpreter = tmp_path / "base/bin/python3.14"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"trusted python")
    named = _pmcp3_release(tmp_path, "sync-1", interpreter)
    other = _pmcp3_release(tmp_path, "sync-2", interpreter)
    launcher = (named / "bin/pmcp").resolve()
    launcher.write_text(launcher.read_text().replace(str(named), str(other)))
    with pytest.raises(PilotRefused, match="pmcp_launcher_unknown"):
        pilot.pmcp_console_script(launcher)
    # Mixing releases: Node from this one, the console script from another.
    launcher.write_text(
        "#!/bin/sh\n"
        f'export PATH={named}/node-v24.22.3-linux-x64/bin:"$PATH"\n'
        f'exec {other}/python/bin/pmcp "$@"\n'
    )
    with pytest.raises(PilotRefused, match="pmcp_launcher_unknown"):
        pilot.pmcp_console_script(launcher)


@pytest.mark.parametrize("version", ["2.7.3", "3.0.2", "3.1.0"])
def test_prepare_refuses_a_pmcp_other_than_the_pin(tmp_path, monkeypatch, version):
    from scripts import v13_pmcp_pilot as pilot

    calls = []
    _prepare_fakes(monkeypatch, pilot, calls)
    monkeypatch.setattr(
        pilot,
        "pmcp_distribution_identity",
        lambda path, expected=None: {"version": version, "node_executable": None},
    )
    wheel = tmp_path / "index_it_mcp-1.6.0-py3-none-any.whl"
    wheel.write_bytes(b"registry wheel fixture")
    with pytest.raises(PilotRefused, match="pmcp_version_mismatch"):
        pilot.prepare(tmp_path / "owned", wheel, pilot.digest_file(wheel))


@pytest.fixture
def held_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        yield sock.getsockname()[1]


def test_qdrant_host_mode_defaults_to_distinct_ephemeral_ports():
    """#138: host networking never binds the stock 6333/6334 by default."""
    from scripts import v13_pmcp_pilot as pilot

    http_port, grpc_port = pilot.qdrant_ports("host")
    assert grpc_port is not None
    assert http_port != grpc_port
    assert not {http_port, grpc_port} & set(pilot.QDRANT_STOCK_PORTS)
    args = pilot.qdrant_network_args("host", http_port, grpc_port)
    assert f"QDRANT__SERVICE__HTTP_PORT={http_port}" in args
    assert f"QDRANT__SERVICE__GRPC_PORT={grpc_port}" in args
    assert "QDRANT__SERVICE__HOST=127.0.0.1" in args


def test_qdrant_default_ephemeral_choice_skips_stock_ports(monkeypatch):
    from scripts import v13_pmcp_pilot as pilot

    offered = iter([6333, 6334, 6333, 40001, 40001, 40002])
    monkeypatch.setattr(pilot, "free_port", lambda: next(offered))
    assert pilot.qdrant_ports("host") == (40001, 40002)


def test_qdrant_bridge_mode_publishes_grpc_only_when_requested():
    from scripts import v13_pmcp_pilot as pilot

    http_port, grpc_port = pilot.qdrant_ports("bridge")
    assert grpc_port is None
    assert pilot.qdrant_network_args("bridge", http_port, None) == [
        "-p",
        f"127.0.0.1:{http_port}:6333",
    ]
    http_port, grpc_port = pilot.qdrant_ports("bridge", grpc_port=pilot.free_port())
    assert pilot.qdrant_network_args("bridge", http_port, grpc_port) == [
        "-p",
        f"127.0.0.1:{http_port}:6333",
        "-p",
        f"127.0.0.1:{grpc_port}:6334",
    ]


def test_qdrant_explicit_ports_are_honoured_and_validated(held_port):
    from scripts import v13_pmcp_pilot as pilot

    http_port, grpc_port = pilot.free_port(), pilot.free_port()
    if http_port == grpc_port:
        grpc_port = pilot.free_port()
    assert pilot.qdrant_ports("host", http_port, grpc_port) == (http_port, grpc_port)
    for network in ("host", "bridge"):
        with pytest.raises(PilotRefused, match=f"qdrant_port_in_use:{held_port}"):
            pilot.qdrant_ports(network, http_port=held_port)
        with pytest.raises(PilotRefused, match=f"qdrant_port_in_use:{held_port}"):
            pilot.qdrant_ports(network, grpc_port=held_port)
    with pytest.raises(PilotRefused, match="qdrant_ports_conflict"):
        pilot.qdrant_ports("host", http_port, http_port)
    for invalid in (0, 65536):
        with pytest.raises(PilotRefused, match="qdrant_port_invalid"):
            pilot.qdrant_ports("host", http_port=invalid)


@pytest.mark.asyncio
async def test_qdrant_port_in_use_refuses_before_any_fixture_or_container(
    tmp_path, manifest, monkeypatch, held_port
):
    from scripts import v13_pmcp_pilot as pilot

    monkeypatch.syspath_prepend(str(pilot.REPO / "scripts"))
    monkeypatch.setattr(
        pilot, "create_fixture", lambda *args, **kwargs: pytest.fail("built a fixture")
    )
    monkeypatch.setattr(pilot, "run_command", lambda *args, **kwargs: pytest.fail("ran a command"))
    with pytest.raises(PilotRefused, match=f"qdrant_port_in_use:{held_port}"):
        await pilot.inference_pilot(
            tmp_path,
            manifest,
            rehearsal=True,
            qdrant_network="host",
            qdrant_grpc_port=held_port,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_host_mode_rehearsal_starts_qdrant_on_the_chosen_ports(
    tmp_path, manifest, monkeypatch, explicit
):
    from scripts import v13_pmcp_pilot as pilot

    monkeypatch.syspath_prepend(str(pilot.REPO / "scripts"))
    monkeypatch.setattr(pilot, "create_fixture", lambda *args, **kwargs: {"root": tmp_path})
    calls = []

    def run(command, *args, **kwargs):
        calls.append(command)
        if command[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(command, 1)
        return ""

    monkeypatch.setattr(pilot, "run_command", run)
    requested = {}
    if explicit:
        requested = {"qdrant_http_port": pilot.free_port(), "qdrant_grpc_port": pilot.free_port()}
        if requested["qdrant_http_port"] == requested["qdrant_grpc_port"]:
            requested["qdrant_grpc_port"] = pilot.free_port()
    with pytest.raises(subprocess.TimeoutExpired):
        await pilot.inference_pilot(
            tmp_path, manifest, rehearsal=True, qdrant_network="host", **requested
        )
    start = calls[0]
    assert start[:2] == ["docker", "run"]
    env = dict(
        start[index + 1].partition("=")[::2]
        for index, arg in enumerate(start)
        if arg == "-e" and "=" in start[index + 1]
    )
    http_port = int(env["QDRANT__SERVICE__HTTP_PORT"])
    grpc_port = int(env["QDRANT__SERVICE__GRPC_PORT"])
    assert http_port != grpc_port
    assert not {http_port, grpc_port} & set(pilot.QDRANT_STOCK_PORTS)
    if explicit:
        assert (http_port, grpc_port) == (
            requested["qdrant_http_port"],
            requested["qdrant_grpc_port"],
        )
    assert calls[-1][:3] == ["docker", "rm", "--force"]


def _qdrant_endpoint(api_key, *, anonymous=401, keyed=200, fail=False):
    """A mock HTTP endpoint behaving like a Qdrant that knows ``api_key``."""
    import httpx

    def handler(request):
        if fail:
            raise httpx.ConnectError("reset", request=request)
        if request.headers.get("api-key") is None:
            return httpx.Response(anonymous)
        return httpx.Response(keyed if request.headers["api-key"] == api_key else 401)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _container_states(monkeypatch, pilot, states):
    calls = []

    def run(command, directory, label, **kwargs):
        assert command[:3] == ["docker", "inspect", "--format"]
        calls.append(label)
        return json.dumps(states[len(calls) - 1])

    monkeypatch.setattr(pilot, "run_command", run)
    return calls


RUNNING = {"Running": True, "Pid": 4242}


@pytest.mark.asyncio
async def test_qdrant_endpoint_served_by_the_owned_container_is_verified(tmp_path, monkeypatch):
    from scripts import v13_pmcp_pilot as pilot

    calls = _container_states(monkeypatch, pilot, [RUNNING, RUNNING])
    async with _qdrant_endpoint("our-key") as http:
        await pilot.verify_qdrant_endpoint(
            http, "http://127.0.0.1:41001", "our-key", "a" * 64, 4242, tmp_path
        )
    assert calls == ["qdrant-endpoint-state", "qdrant-endpoint-recheck"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "unauthenticated_squatter",
        "foreign_key_qdrant",
        "exited_before_probe",
        "pid_changed",
        "exited_during_probe",
        "connection_reset",
    ],
)
async def test_qdrant_endpoint_not_owned_by_the_container_fails_closed(tmp_path, monkeypatch, case):
    """#138 review: a squatter that won the port race must never be used."""
    from scripts import v13_pmcp_pilot as pilot

    states = [RUNNING, RUNNING]
    endpoint = {}
    if case == "unauthenticated_squatter":
        endpoint = {"anonymous": 200}
    elif case == "foreign_key_qdrant":
        endpoint = {"keyed": 401}
    elif case == "exited_before_probe":
        states[0] = {"Running": False, "Pid": 0}
    elif case == "pid_changed":
        states[0] = {"Running": True, "Pid": 9999}
    elif case == "exited_during_probe":
        states[1] = {"Running": False, "Pid": 0}
    elif case == "connection_reset":
        endpoint = {"fail": True}
    _container_states(monkeypatch, pilot, states)
    async with _qdrant_endpoint("our-key", **endpoint) as http:
        with pytest.raises(PilotRefused, match="qdrant_endpoint_unverified"):
            await pilot.verify_qdrant_endpoint(
                http, "http://127.0.0.1:41001", "our-key", "a" * 64, 4242, tmp_path
            )


@pytest.mark.asyncio
async def test_rehearsal_refuses_a_squatter_on_the_qdrant_port_and_removes_its_container(
    tmp_path, manifest, monkeypatch
):
    import httpx

    from scripts import v13_pmcp_pilot as pilot

    monkeypatch.syspath_prepend(str(pilot.REPO / "scripts"))
    monkeypatch.setattr(pilot, "create_fixture", lambda *args, **kwargs: {"root": tmp_path})
    container_id = "c" * 64
    calls = []

    def run(command, *args, **kwargs):
        calls.append(command)
        if command[:2] == ["docker", "run"]:
            return container_id + "\n"
        if command[:2] == ["docker", "inspect"]:
            # Our container still runs, but the squatter holds the HTTP port.
            return json.dumps(RUNNING)
        return ""

    class Container:
        def __init__(self, identity, root):
            self.container, self.pid, self.group = identity, RUNNING["Pid"], root
            self.exit_seconds, self.peak_rss_mib, self.survivors = 0.1, 1.0, []
            self.exit_state = None

        def observe(self):
            pass

        def stop(self):
            calls.append(["stop", self.container])

    # The squatter answers readiness and serves data without any API key.
    squatter = httpx.MockTransport(lambda request: httpx.Response(200))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        pilot.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=squatter),
    )
    monkeypatch.setattr(pilot, "run_command", run)
    monkeypatch.setattr(pilot, "OwnedContainer", Container)
    with pytest.raises(PilotRefused, match="qdrant_endpoint_unverified"):
        await pilot.inference_pilot(tmp_path, manifest, rehearsal=True, qdrant_network="host")
    assert calls[-1] == ["stop", container_id]
    assert not (tmp_path / "allowance").exists()


@pytest.mark.parametrize("flag", ["--qdrant-http-port", "--qdrant-grpc-port"])
@pytest.mark.parametrize("mode", ["identity", "prepare", "offline", "browser", "verify-live"])
def test_qdrant_ports_require_an_inference_mode(monkeypatch, mode, flag):
    from scripts import v13_pmcp_pilot as pilot

    monkeypatch.setattr(sys, "argv", ["v13_pmcp_pilot.py", "--mode", mode, flag, "6333"])
    monkeypatch.setattr(pilot, "source_identity", lambda: pytest.fail("reached mode dispatch"))
    monkeypatch.setattr(
        pilot, "installed_identity", lambda wheel: pytest.fail("reached identity mode")
    )
    with pytest.raises(PilotRefused, match="qdrant_ports_require_inference_mode"):
        pilot.main()


def test_semantic_sample_refuses_lexical_fallback_and_wrong_generation():
    from scripts.v13_pmcp_pilot import semantic_sample_valid

    sample = {
        "semantic_requested": True,
        "semantic_source": "semantic",
        "semantic_fallback_status": "not_attempted",
        "semantic_profile_id": "pilot",
        "semantic_collection_name": "pilot-collection",
        "results": [
            {
                "file": "bookkeeping.py",
                "semantic_source": "semantic",
                "semantic_profile_id": "pilot",
                "semantic_collection_name": "pilot-collection",
            }
        ],
    }
    assert semantic_sample_valid(sample)
    for field, value in (
        ("semantic_source", "lexical"),
        ("semantic_fallback_status", "lexical_fallback"),
        ("semantic_profile_id", "other"),
        ("semantic_collection_name", "other"),
    ):
        corrupted = copy.deepcopy(sample)
        corrupted[field] = value
        assert not semantic_sample_valid(corrupted)


@pytest.mark.asyncio
async def test_ambiguous_qdrant_creation_cleans_owned_name(tmp_path, manifest, monkeypatch):
    from scripts import v13_pmcp_pilot as pilot

    monkeypatch.syspath_prepend(str(pilot.REPO / "scripts"))
    monkeypatch.setattr(pilot, "create_fixture", lambda *args, **kwargs: {"root": tmp_path})
    calls = []

    def run(command, *args, **kwargs):
        calls.append(command)
        if command[:2] == ["docker", "run"]:
            assert "--name" in command
            raise subprocess.TimeoutExpired(command, 1)
        return ""

    monkeypatch.setattr(pilot, "run_command", run)
    with pytest.raises(subprocess.TimeoutExpired):
        await pilot.inference_pilot(tmp_path, manifest, rehearsal=True)
    name = calls[0][calls[0].index("--name") + 1]
    assert name.startswith("mcp-v13-pilot-")
    assert calls[-1] == ["docker", "rm", "--force", name]
    assert not (tmp_path / "allowance").exists()


def test_artifact_replacement_cannot_change_the_validated_snapshot(tmp_path, manifest, monkeypatch):
    from scripts import v13_pmcp_pilot as pilot

    result = receipt(manifest, "browser")
    asset = tmp_path / "asset.json"
    asset.write_text('{"original": true}')
    result["artifacts"] = [
        {"role": role, "path": asset.name, "sha256": pilot.digest_file(asset)}
        for role in (
            "inspector_screenshot",
            "admin_screenshot",
            "browser_actions",
            "browser_results",
            "browser_session",
        )
    ]
    (tmp_path / "browser.json").write_text(json.dumps(result))

    def replace_then_check(root, manifest, kind, result, copies, expected_approval):
        replacement = root / "replacement.json"
        replacement.write_text('{"unvalidated": true}')
        replacement.replace(asset)
        assert json.loads(copies[asset].read_bytes()) == {"original": True}

    monkeypatch.setattr(pilot, "_verify_receipt_artifacts", replace_then_check)
    with pytest.raises(PilotRefused, match="evidence_changed"):
        verify_saved_receipt(tmp_path, manifest, "browser")


@pytest.mark.parametrize("damage", ["missing", "drift", "outside"])
def test_browser_artifacts_are_bound_and_confined(tmp_path, manifest, damage):
    from scripts.v13_pmcp_pilot import digest_file

    result = receipt(manifest, "browser")
    asset = tmp_path / "asset.json"
    asset.write_text("{}")
    result["artifacts"] = [
        {"role": role, "path": "asset.json", "sha256": digest_file(asset)}
        for role in (
            "inspector_screenshot",
            "admin_screenshot",
            "browser_actions",
            "browser_session",
        )
    ]
    if damage == "missing":
        asset.unlink()
    elif damage == "drift":
        asset.write_text("changed")
    else:
        result["artifacts"][0]["path"] = "../asset.json"
    (tmp_path / "browser.json").write_text(json.dumps(result))
    with pytest.raises(PilotRefused, match="artifact"):
        verify_saved_receipt(tmp_path, manifest, "browser")


def test_rehearsal_cannot_be_live_acceptance(manifest):
    result = receipt(manifest, "live")
    result["rehearsal"] = True
    with pytest.raises(PilotRefused, match="rehearsal"):
        validate_receipt(result, manifest, "live")


@pytest.mark.parametrize(
    "ids,preferred,expected",
    [
        (["chat", "other"], "chat", "chat"),
        (["served-alias"], "chat", "served-alias"),
    ],
)
def test_model_selection_uses_reported_catalog(ids, preferred, expected):
    from scripts.v13_pmcp_pilot import select_model

    assert select_model({"data": [{"id": value} for value in ids]}, preferred) == expected


@pytest.mark.parametrize("catalog", [{}, {"data": []}, {"data": [{"id": "a"}, {"id": "b"}]}])
def test_ambiguous_model_catalog_refused(catalog):
    from scripts.v13_pmcp_pilot import select_model

    with pytest.raises(PilotRefused, match="model_catalog"):
        select_model(catalog, "unreported")


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "binding",
        "unstarted",
        "ungraceful",
        "identity",
        "shutdown",
        "actions",
        "image",
        "blank",
        "duplicate",
        "results",
        "forged",
    ],
)
def test_browser_artifact_contents_are_verified(tmp_path, manifest, damage):
    from PIL import Image, ImageDraw

    from scripts.v13_pmcp_pilot import digest_file

    result = receipt(manifest, "browser")
    result["inspector_version"] = "2.6.0"
    result["inspector_entrypoint_sha256"] = "d" * 64
    binding = {key: result[key] for key in ("source", "wheel_sha256", "manifest_sha256")}
    session = {
        **binding,
        "session_started": True,
        "explicit_stop": True,
        "inspector_version": "2.6.0",
        "inspector_entrypoint_sha256": "d" * 64,
        "shutdown_seconds": [1],
        "surviving_children": [],
        "peak_rss_mib": 100,
    }
    actions = {
        **binding,
        "events": [
            {"goal": goal, "ok": True, "observed": observed}
            for goal, observed in {
                "admin_queries": {"symbol": "200", "search": "200", "no_match": True},
                "admin_refusals": {
                    "sibling_status": "503\nUndocumented",
                    "index_unavailable": True,
                },
                "admin_reindex": {"repository": "catalog", "status": "200"},
                "inspector_queries": {"ledger": True, "catalog": True, "no_match": True},
                "inspector_refusals": {"sibling_refused": True},
                "inspector_reconnect": {"reconnected": True, "query_after_reconnect": True},
                "console_checked": {"page_errors": 0},
                "screenshots": {"admin": "admin.png", "inspector": "inspector.png"},
            }.items()
        ],
    }
    sibling = str(tmp_path / "browser/repos/ledger-sibling")
    observations = (
        [
            {
                "surface": "admin",
                "path": path,
                "fields": {field: value},
                "status": status,
                "marker_found": True,
                "empty_result": value == "absent_739152",
                "refusal": status.startswith("503"),
                "page_errors": [],
            }
            for path, field, value, status in (
                ("/symbol", "symbol", "available_balance", "200"),
                ("/search", "q", "find_product", "200"),
                ("/search", "q", "absent_739152", "200"),
                ("/symbol", "repository", sibling, "503\nUndocumented"),
                ("/reindex", "repository", "catalog", "200"),
            )
        ]
        + [
            {
                "surface": "inspector",
                "repository": repo,
                "query": query,
                "ok": True,
                "result_found": True,
                "not_found": query == "absent_739152",
                "refusal": repo == sibling,
                "page_errors": [],
            }
            for repo, query in (
                ("ledger", "available_balance"),
                ("catalog", "find_product"),
                ("ledger", "absent_739152"),
                (sibling, "available_balance"),
            )
        ]
        + [
            {
                "surface": "inspector",
                "reconnected": True,
                "query_after_reconnect": True,
                "page_errors": [],
            }
        ]
    )
    observations[3]["fields"]["symbol"] = "available_balance"
    if damage == "results":
        observations.pop()
    elif damage == "forged":
        actions["events"] = [
            {"goal": goal, "ok": True, "observed": {"fixture": True}} for goal in GOALS["browser"]
        ]
    if damage == "binding":
        session["source"] = "wrong"
    elif damage == "unstarted":
        session["session_started"] = False
    elif damage == "ungraceful":
        session["explicit_stop"] = False
    elif damage == "identity":
        session["inspector_entrypoint_sha256"] = "e" * 64
    elif damage == "shutdown":
        session["shutdown_seconds"] = [6]
    elif damage == "actions":
        actions["events"].pop()
    (tmp_path / "session.json").write_text(json.dumps(session))
    (tmp_path / "actions.json").write_text(json.dumps(actions))
    (tmp_path / "results.json").write_text(json.dumps(observations))
    for name in ("admin", "inspector"):
        picture = Image.new("RGB", (100, 100), "white")
        ImageDraw.Draw(picture).rectangle(
            (10, 10, 90, 90), fill="black" if name == "admin" else "blue"
        )
        picture.save(tmp_path / (name + ".png"))
    if damage == "image":
        (tmp_path / "admin.png").write_bytes(b"not an image")
    elif damage == "blank":
        Image.new("RGB", (100, 100), "white").save(tmp_path / "admin.png")
    elif damage == "duplicate":
        (tmp_path / "inspector.png").write_bytes((tmp_path / "admin.png").read_bytes())
    result["artifacts"] = [
        {"role": role, "path": name, "sha256": digest_file(tmp_path / name)}
        for role, name in (
            ("browser_session", "session.json"),
            ("browser_actions", "actions.json"),
            ("browser_results", "results.json"),
            ("admin_screenshot", "admin.png"),
            ("inspector_screenshot", "inspector.png"),
        )
    ]
    (tmp_path / "browser.json").write_text(json.dumps(result))
    if damage:
        with pytest.raises(PilotRefused):
            verify_saved_receipt(tmp_path, manifest, "browser")
    else:
        verify_saved_receipt(tmp_path, manifest, "browser")


def test_hashes_and_goal_flags_cannot_replace_live_records(tmp_path, manifest):
    from scripts.v13_pmcp_pilot import digest_file

    (tmp_path / "not-a-ledger.json").write_text("{}")
    result = receipt(manifest, "live")
    result["rehearsal"] = False
    result["artifacts"] = [
        {
            "role": role,
            "path": "not-a-ledger.json",
            "sha256": digest_file(tmp_path / "not-a-ledger.json"),
        }
        for role in ("allowance_ledger", "runtime_provenance")
    ]
    (tmp_path / "live.json").write_text(json.dumps(result))
    with pytest.raises(PilotRefused):
        verify_saved_receipt(tmp_path, manifest, "live")


@pytest.fixture(params=["original", "renewed"])
def live_records(tmp_path, manifest, request, monkeypatch):
    import hashlib

    from scripts import v13_pilot_budget as budget
    from scripts.v13_pilot_budget import ENDPOINTS, BudgetLedger
    from scripts.v13_pilot_estimate import REQUEST_ENVELOPES, SYNTHETIC_CORPUS
    from scripts.v13_pmcp_pilot import QDRANT_IMAGE, QUERY_TEXTS

    result = receipt(manifest, "live")
    result.update(rehearsal=False, workflow_completed=True, samples=[], index_intervals=[])
    ledger_root = tmp_path / "ledger"
    approval = budget.RENEWED_APPROVAL if request.param == "renewed" else budget.APPROVAL
    if request.param == "renewed":
        monkeypatch.setattr(budget, "RENEWED_ROOT", ledger_root)
    BudgetLedger.initialize(ledger_root, digest_json(manifest), approval=approval)
    ledger = BudgetLedger(
        ledger_root, digest_json(manifest), clock=lambda: (1000.0, 100.0), approval=approval
    )
    for request_class, count in (
        ("provenance_probe", 2),
        ("summary", 1),
        ("document_embedding", 1),
        ("query_embedding", 40),
    ):
        for _ in range(count):
            request = ledger.reserve(
                "enrichment" if request_class == "summary" else "embedding",
                100,
                request_class=request_class,
                envelope=REQUEST_ENVELOPES[request_class],
            )
            ledger.finish(request, "success", 200)
    result["budget"] = {**ledger.snapshot(), "elapsed_seconds": 20}
    for repo_index, repo in enumerate(SYNTHETIC_CORPUS):
        for index in range(20):
            for kind_index, kind in enumerate(("symbol", "lexical", "semantic")):
                started = 101 + repo_index * 3 + (index * 3 + kind_index) * 0.03
                ended = started + 0.02
                result["samples"].append(
                    {
                        "kind": kind,
                        "repository": repo,
                        "started": started,
                        "ended": ended,
                        "milliseconds": (ended - started) * 1000,
                        "ready_success": True,
                    }
                )
    result["index_intervals"] = [
        {"repository": "catalog", "started": 100.0, "ended": 103.0, "success": True},
        {"repository": "ledger", "started": 103.0, "ended": 107.0, "success": True},
    ]
    result["latencies_ms"] = {
        kind: [sample["milliseconds"] for sample in result["samples"] if sample["kind"] == kind]
        for kind in ("symbol", "lexical", "semantic")
    }
    result["contention_successes"] = dict.fromkeys(("symbol", "lexical", "semantic"), 40)
    workload = {
        "corpus": SYNTHETIC_CORPUS,
        "request_envelopes": REQUEST_ENVELOPES,
        "query_texts": QUERY_TEXTS,
        "measured_queries_per_class_per_repository": 20,
        "rehearsal_index_delay_seconds": 0,
        "rehearsal": False,
        "manifest_sha256": digest_json(manifest),
    }
    metadata = {
        "models": {"embedding": "unit-fixture", "enrichment": "unit-chat"},
        "selected_profile": {
            "provider": "openai_compatible",
            "model_name": "unit-fixture",
            "model_version": "unreported",
            "vector_dimension": 8,
            "distance_metric": "cosine",
            "normalization_policy": "provider-default",
            "chunk_schema_version": "1",
            "chunker_version": "4.0.0",
        },
        "dimension": 8,
        "immutable_revision": "unreported",
        "qdrant_image": QDRANT_IMAGE,
        "qdrant_network": "host",
        "qdrant_http_port": 41001,
        "qdrant_grpc_port": 41002,
        "qdrant_ports_requested": {"http": False, "grpc": False},
        "qdrant_endpoint_verified": True,
        "endpoints": ENDPOINTS,
        "workload_sha256": digest_json(workload),
    }
    repositories = []
    from scripts.v13_pmcp_pilot import expected_profile_fingerprint

    fingerprint = expected_profile_fingerprint(metadata)
    for repo, filename in (("ledger", "bookkeeping.py"), ("catalog", "catalog.py")):
        repositories.append(
            {
                "repository": repo,
                "commit": "a" * 40,
                "generation": "b" * 32,
                "point_count": 1,
                "mapping_count": 1,
                "point_ids": ["1"],
                "attested": True,
                "collection_manifest": {
                    "indexed_commit": "a" * 40,
                    "point_set_id": hashlib.sha256(b"1").hexdigest(),
                    "corpus_sha256": hashlib.sha256(filename.encode()).hexdigest(),
                    "profile_fingerprint": fingerprint,
                    "provider_id": "unit-fixture",
                    "provider_revision": "declared",
                    "provenance_version": "collection-provenance.v1",
                },
                "embedding_provenance": {
                    "served_model_id": {"source": "reported", "value": "unit-fixture"},
                    "dimension": {"source": "reported", "value": 8},
                    "model_revision": {"source": "declared", "value": "unreported"},
                },
            }
        )
    return (
        result,
        ledger,
        {
            "workload": workload,
            "runtime_metadata": metadata,
            "runtime_provenance": {"repositories": repositories},
            "resource_measurements": {
                **{key: result[key] for key in ("source", "wheel_sha256", "manifest_sha256")},
                "owners": copy.deepcopy(result["resource_owners"]),
            },
            "qdrant_start": "a" * 64,
            "qdrant_stop_state": {"Running": False, "ExitCode": 0, "OOMKilled": False},
        },
    )


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "ledger",
        "accounting",
        "inflight",
        "role_overlap",
        "cross_role_overlap",
        "envelope",
        "unknown_class",
        "samples_missing",
        "sample_refusal",
        "sample_timing",
        "sample_outside_budget",
        "sample_repository",
        "contention_count",
        "contention_interval",
        "workload",
        "endpoints",
        "provenance",
        "corpus",
        "point_ids",
        "mapping_count",
        "model",
        "dimension",
        "revision_missing",
        "revision_mismatch",
        "profile_fingerprint",
        "provider_revision",
        "rehearsal",
        "incomplete",
        "duplicate_artifact",
        "resources_missing_qdrant",
        "resources_binding",
        "resource_rss",
        "resource_shutdown",
        "qdrant_start",
        "qdrant_exit",
        "qdrant_ports_missing",
        "qdrant_network_unknown",
        "qdrant_port_range",
        "qdrant_port_type",
        "qdrant_ports_equal",
        "qdrant_host_grpc_missing",
        "qdrant_stock_port_default",
        "qdrant_stock_grpc_default",
        "qdrant_requested_missing",
        "qdrant_unpublished_grpc_requested",
        "qdrant_bridge_grpc_unrequested",
        "qdrant_endpoint_unverified",
        "qdrant_stock_port_requested",
        "qdrant_bridge_unpublished_grpc",
    ],
)
def test_live_record_reduction_is_consistent_and_read_only(
    tmp_path, manifest, live_records, damage
):
    import sqlite3

    from scripts.v13_pmcp_pilot import digest_file

    result, ledger, documents = live_records
    ledger_path = ledger.root / "ledger.sqlite"
    if damage == "ledger":
        ledger_path.write_bytes(b"{}")
    elif damage == "accounting":
        result["budget"]["reserved_input_units"] += 1
    elif damage in {"inflight", "role_overlap", "cross_role_overlap", "envelope", "unknown_class"}:
        with sqlite3.connect(ledger_path) as db:
            if damage in {"role_overlap", "cross_role_overlap"}:
                # Row 1 is an embedding probe; row 3 is the enrichment summary.
                row = 1 if damage == "role_overlap" else 3
                db.execute("UPDATE requests SET finished_wall=1001.0 WHERE rowid=?", (row,))
                db.execute("UPDATE allowance SET last_wall=1001.0")
            elif damage == "inflight":
                db.execute(
                    "UPDATE requests SET outcome='inflight',finished_wall=NULL WHERE rowid=1"
                )
            else:
                db.execute(
                    "UPDATE requests SET request_class=?",
                    ("summary" if damage == "envelope" else "unknown",),
                )
        result["budget"] = {**ledger.snapshot(), "elapsed_seconds": 20}
    elif damage == "samples_missing":
        result["samples"].pop()
    elif damage == "sample_refusal":
        result["samples"][0]["ready_success"] = False
    elif damage == "sample_timing":
        result["samples"][0]["milliseconds"] = 0
    elif damage == "sample_outside_budget":
        result["samples"][0].update(started=10000, ended=10000.02)
    elif damage == "sample_repository":
        result["samples"][0]["repository"] = "catalog"
    elif damage == "contention_count":
        result["contention_successes"]["symbol"] = 39
    elif damage == "contention_interval":
        result["index_intervals"][0]["success"] = False
    elif damage == "workload":
        documents["workload"]["corpus"] = {}
    elif damage == "endpoints":
        documents["runtime_metadata"]["endpoints"] = {}
    elif damage == "provenance":
        documents["runtime_provenance"]["repositories"] = []
    elif damage == "corpus":
        documents["runtime_provenance"]["repositories"][0]["collection_manifest"][
            "corpus_sha256"
        ] = None
    elif damage == "point_ids":
        documents["runtime_provenance"]["repositories"][0]["point_ids"] = ["2"]
    elif damage == "mapping_count":
        documents["runtime_provenance"]["repositories"][0]["mapping_count"] = 0
    elif damage == "model":
        documents["runtime_provenance"]["repositories"][0]["embedding_provenance"][
            "served_model_id"
        ]["value"] = "wrong"
    elif damage == "dimension":
        documents["runtime_metadata"]["dimension"] = 9
    elif damage == "revision_missing":
        del documents["runtime_provenance"]["repositories"][0]["embedding_provenance"][
            "model_revision"
        ]
    elif damage == "revision_mismatch":
        documents["runtime_metadata"]["immutable_revision"] = "invented-revision"
    elif damage == "profile_fingerprint":
        documents["runtime_provenance"]["repositories"][0]["collection_manifest"][
            "profile_fingerprint"
        ] = ("a" * 64)
    elif damage == "provider_revision":
        documents["runtime_provenance"]["repositories"][0]["collection_manifest"][
            "provider_revision"
        ] = "invented-revision"
    elif damage == "rehearsal":
        documents["workload"]["rehearsal"] = True
    elif damage == "incomplete":
        result["workflow_completed"] = False
    elif damage == "resources_missing_qdrant":
        documents["resource_measurements"]["owners"].pop()
    elif damage == "resources_binding":
        documents["resource_measurements"]["source"] = "d" * 40
    elif damage == "resource_rss":
        documents["resource_measurements"]["owners"][-1]["peak_rss_mib"] += 1
    elif damage == "resource_shutdown":
        documents["resource_measurements"]["owners"][-1]["shutdown_seconds"] = 9
    elif damage == "qdrant_start":
        documents["qdrant_start"] = "d" * 64
    elif damage == "qdrant_exit":
        documents["qdrant_stop_state"]["ExitCode"] = 137
    elif damage and damage.startswith("qdrant_"):
        metadata = documents["runtime_metadata"]
        if damage == "qdrant_ports_missing":
            del metadata["qdrant_http_port"]
        elif damage == "qdrant_network_unknown":
            metadata["qdrant_network"] = "overlay"
        elif damage == "qdrant_port_range":
            metadata["qdrant_http_port"] = 65536
        elif damage == "qdrant_port_type":
            metadata["qdrant_http_port"] = "41001"
        elif damage == "qdrant_ports_equal":
            metadata["qdrant_grpc_port"] = metadata["qdrant_http_port"]
        elif damage == "qdrant_host_grpc_missing":
            metadata["qdrant_grpc_port"] = None
        elif damage == "qdrant_stock_port_default":
            metadata["qdrant_http_port"] = 6333
        elif damage == "qdrant_stock_grpc_default":
            metadata["qdrant_grpc_port"] = 6334
        elif damage == "qdrant_requested_missing":
            del metadata["qdrant_ports_requested"]
        elif damage == "qdrant_unpublished_grpc_requested":
            metadata.update(qdrant_network="bridge", qdrant_grpc_port=None)
            metadata["qdrant_ports_requested"]["grpc"] = True
        elif damage == "qdrant_bridge_grpc_unrequested":
            # Bridge publishes gRPC only on request, so this receipt is impossible.
            metadata["qdrant_network"] = "bridge"
        elif damage == "qdrant_endpoint_unverified":
            metadata["qdrant_endpoint_verified"] = False
        # The last two are valid: an explicit stock port, and bridge without gRPC.
        elif damage == "qdrant_stock_port_requested":
            metadata.update(qdrant_http_port=6333, qdrant_grpc_port=6334)
            metadata["qdrant_ports_requested"] = {"http": True, "grpc": True}
        elif damage == "qdrant_bridge_unpublished_grpc":
            metadata.update(qdrant_network="bridge", qdrant_grpc_port=None)
    paths = {"allowance_ledger": ledger_path.relative_to(tmp_path).as_posix()}
    for role, document in documents.items():
        paths[role] = role + ".json"
        (tmp_path / paths[role]).write_text(
            document if isinstance(document, str) else json.dumps(document)
        )
    result["artifacts"] = [
        {"role": role, "path": path, "sha256": digest_file(tmp_path / path)}
        for role, path in paths.items()
    ]
    if damage == "duplicate_artifact":
        result["artifacts"].append(result["artifacts"][0])
    (tmp_path / "live.json").write_text(json.dumps(result))
    before = {path: (tmp_path / path).read_bytes() for path in paths.values()}
    valid = {None, "cross_role_overlap", "qdrant_stock_port_requested"}
    valid.add("qdrant_bridge_unpublished_grpc")
    if damage not in valid:
        with pytest.raises(PilotRefused):
            verify_saved_receipt(tmp_path, manifest, "live", expected_approval=ledger.approval)
    else:
        verify_saved_receipt(tmp_path, manifest, "live", expected_approval=ledger.approval)
        from scripts.v13_pilot_budget import RENEWED_APPROVAL

        if ledger.approval == RENEWED_APPROVAL:
            with pytest.raises(PilotRefused):
                verify_saved_receipt(tmp_path, manifest, "live")
    assert before == {path: (tmp_path / path).read_bytes() for path in paths.values()}


@pytest.mark.parametrize("live_records", ["original"], indirect=True)
@pytest.mark.parametrize("damage", [None, "binding", "not_rehearsal", "incomplete", "artifact"])
def test_saved_rehearsal_requires_actual_bound_records(tmp_path, manifest, live_records, damage):
    from scripts.v13_pmcp_pilot import digest_file

    result, ledger, documents = live_records
    result["rehearsal"] = documents["workload"]["rehearsal"] = True
    documents["workload"]["rehearsal_index_delay_seconds"] = REHEARSAL_INDEX_DELAY_SECONDS
    documents["runtime_metadata"]["workload_sha256"] = digest_json(documents["workload"])
    paths = {"allowance_ledger": (ledger.root / "ledger.sqlite").relative_to(tmp_path).as_posix()}
    for role, document in documents.items():
        paths[role] = role + ".json"
        (tmp_path / paths[role]).write_text(
            document if isinstance(document, str) else json.dumps(document)
        )
    result["artifacts"] = [
        {"role": role, "path": path, "sha256": digest_file(tmp_path / path)}
        for role, path in paths.items()
    ]
    if damage == "binding":
        result["manifest_sha256"] = "0" * 64
    elif damage == "not_rehearsal":
        result["rehearsal"] = False
    elif damage == "incomplete":
        result["workflow_completed"] = False
    elif damage == "artifact":
        (tmp_path / paths["workload"]).write_text("{}")
    (tmp_path / "rehearsal.json").write_text(json.dumps(result))
    if damage:
        with pytest.raises(PilotRefused):
            verify_saved_receipt(tmp_path, manifest, "rehearsal")
    else:
        verify_saved_receipt(tmp_path, manifest, "rehearsal")


@pytest.mark.asyncio
async def test_runtime_provenance_counts_points_separately_from_mappings(tmp_path, httpx_mock):
    import hashlib
    import sqlite3

    from scripts.v13_pmcp_pilot import expected_profile_fingerprint, runtime_provenance

    repo = tmp_path / "repos/ledger"
    repo.mkdir(parents=True)
    (repo / "bookkeeping.py").write_text("pass\n")
    database = tmp_path / "generation.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE semantic_points(point_id INTEGER, collection TEXT)")
        db.executemany("INSERT INTO semantic_points VALUES (?,?)", [(1, "fixture"), (1, "fixture")])
    info = {
        "name": "ledger",
        "index_path": str(database),
        "index_generation": "generation",
        "last_indexed_commit": "a" * 40,
    }
    (tmp_path / "registry.json").write_text(json.dumps({"repo": info}))
    reported = {
        "models": {"embedding": "model"},
        "dimension": 8,
        "selected_profile": {
            "provider": "openai_compatible",
            "model_name": "model",
            "model_version": "unreported",
            "vector_dimension": 8,
            "distance_metric": "cosine",
            "normalization_policy": "provider-default",
            "chunk_schema_version": "1",
            "chunker_version": "4.0.0",
        },
    }
    fingerprint = expected_profile_fingerprint(reported)
    (tmp_path / "runtime-metadata.json").write_text(json.dumps(reported))
    metadata_dir = database.with_suffix(".semantic")
    metadata_dir.mkdir()
    (metadata_dir / ".index_metadata.json").write_text(
        json.dumps(
            {
                "semantic_profiles": {
                    "pilot": {
                        "collection_name": "fixture",
                        "attested": True,
                        "compatibility_fingerprint": fingerprint,
                        "provenance": {
                            "served_model_id": {"source": "reported", "value": "model"},
                            "dimension": {"source": "reported", "value": 8},
                            "model_revision": {"source": "declared", "value": "unreported"},
                        },
                    }
                }
            }
        )
    )
    sentinel = {
        "__provenance__": True,
        "indexed_commit": "a" * 40,
        "point_set_id": hashlib.sha256(b"1").hexdigest(),
        "corpus_sha256": hashlib.sha256(b"bookkeeping.py").hexdigest(),
        "profile_fingerprint": fingerprint,
        "provider_revision": "declared",
    }
    httpx_mock.add_response(
        method="POST",
        url="http://127.0.0.1:1/collections/fixture/points/scroll",
        json={
            "result": {
                "points": [{"id": "sentinel", "payload": sentinel}, {"id": 1, "payload": {}}]
            }
        },
    )
    records = await runtime_provenance({"root": tmp_path}, "http://127.0.0.1:1")
    assert records[0]["point_count"] == 1
    assert records[0]["mapping_count"] == 2
    assert records[0]["point_ids"] == ["1"]


# ---------------------------------------------------------------------------
# Prefetched grammar cache for fixtures and sandbox workers (#127)
# ---------------------------------------------------------------------------


def _prepare_fakes(monkeypatch, pilot, calls):
    from scripts import release_smoke

    monkeypatch.setattr(pilot, "source_identity", lambda: {"source": "a" * 40})
    monkeypatch.setattr(
        pilot,
        "pmcp_distribution_identity",
        lambda path, expected=None: {"version": "3.0.3", "node_executable": None},
    )
    monkeypatch.setattr(
        pilot, "browser_node_identity", lambda path: {"path": str(path), "sha256": "node"}
    )
    monkeypatch.setattr(release_smoke, "validate_wheel_source", lambda *args: {"version": "1.6.0"})

    def run(command, directory, label, **kwargs):
        calls.append((label, command, kwargs.get("env")))
        if label == "pilot-lock-export":
            (directory / "constraints.txt").write_text("dependency==1\n")
        return '{"version": "1.6.0"}' if label == "installed-identity" else "pmcp 3.0.3"

    monkeypatch.setattr(pilot, "run_command", run)


@pytest.mark.parametrize("source", ["default", "argument", "environment"])
def test_prepare_prefetches_grammars_into_an_explicit_cache(tmp_path, monkeypatch, source):
    from scripts import v13_pmcp_pilot as pilot

    wheel = tmp_path / "index_it_mcp-1.6.0-py3-none-any.whl"
    wheel.write_bytes(b"registry wheel fixture")
    root = tmp_path / "owned"
    operator_cache = tmp_path / "operator-grammars"
    monkeypatch.delenv(pilot.GRAMMAR_CACHE_ENV, raising=False)
    if source == "environment":
        monkeypatch.setenv(pilot.GRAMMAR_CACHE_ENV, str(operator_cache))
    calls = []
    _prepare_fakes(monkeypatch, pilot, calls)

    manifest = pilot.prepare(
        root,
        wheel,
        pilot.digest_file(wheel),
        operator_cache if source == "argument" else None,
    )

    expected = root / "grammar-cache" if source == "default" else operator_cache
    assert manifest["grammar_cache"] == str(expected)
    if source == "default":
        assert expected.is_dir() and expected.stat().st_mode & 0o777 == 0o700
    ((label, command, env),) = [call for call in calls if call[0] == "grammar-prefetch"]
    assert command[: len(manifest["uvx_prefix"])] == manifest["uvx_prefix"]
    assert command[len(manifest["uvx_prefix"]) :] == [
        "mcp-index",
        "grammars",
        "prefetch",
        "--cache-dir",
        str(expected),
    ]
    # Prefetch is the one step allowed to reach the network.
    assert env[pilot.GRAMMAR_CACHE_ENV] == str(expected)
    assert pilot.GRAMMAR_MANIFEST_URL_ENV not in env
    labels = [call[0] for call in calls]
    assert labels.index("installed-identity") < labels.index("grammar-prefetch")


def test_relative_grammar_cache_is_refused(tmp_path, monkeypatch):
    from scripts import v13_pmcp_pilot as pilot

    monkeypatch.delenv(pilot.GRAMMAR_CACHE_ENV, raising=False)
    with pytest.raises(PilotRefused, match="grammar_cache_not_absolute"):
        pilot.resolve_grammar_cache(tmp_path, Path("relative/cache"))


def test_fixture_server_and_workers_get_the_prefetched_cache_with_downloads_denied(
    tmp_path, monkeypatch
):
    from mcp_server.utils.grammar_cache import CACHE_DIR_ENV, MANIFEST_URL_ENV, OFFLINE_MANIFEST_URL
    from scripts import v13_pmcp_pilot as pilot

    # The pilot runs standalone, so it mirrors these names; they must not drift.
    assert pilot.GRAMMAR_CACHE_ENV == CACHE_DIR_ENV
    assert pilot.GRAMMAR_MANIFEST_URL_ENV == MANIFEST_URL_ENV
    assert pilot.OFFLINE_GRAMMAR_MANIFEST_URL == OFFLINE_MANIFEST_URL

    cache = tmp_path / "grammar-cache"
    cache.mkdir()
    manifest = {
        "grammar_cache": str(cache),
        "pmcp_path": str(tmp_path / "pmcp"),
        "pmcp_distribution": {"console": "x", "launcher": "x"},
        "uvx_prefix": ["uvx", "--from", "index-it-mcp"],
    }
    commands = []
    monkeypatch.setattr(
        pilot,
        "run_command",
        lambda command, directory, label, **kwargs: commands.append((label, kwargs.get("env"))),
    )
    monkeypatch.setattr(pilot, "commit_fixture", lambda path, env: None)
    # create_fixture imports the pilot's sibling helper module as the pilot does
    # when run as a script; do not rely on another test having put scripts/ on
    # sys.path (failed under xdist scheduling on the #132 re-check branch).
    monkeypatch.syspath_prepend(str(pilot.REPO / "scripts"))

    fixture = pilot.create_fixture(tmp_path, manifest, label="offline")

    expected = {CACHE_DIR_ENV: str(cache), MANIFEST_URL_ENV: OFFLINE_MANIFEST_URL}
    server_env = json.loads((fixture["root"] / "project" / ".mcp.json").read_text())["mcpServers"][
        "index-it-mcp"
    ]["env"]
    for env in (fixture["env"], server_env, *(env for _, env in commands)):
        assert {key: env.get(key) for key in expected} == expected
    # The fixture's fresh XDG cache would be cold; the explicit override wins.
    assert server_env["XDG_CACHE_HOME"] != str(cache)


def _raw_stdio_call(command, env, cwd, calls):
    """Initialize, then answer each (method, params) over raw STDIO JSON-RPC."""
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "pilot-grammar-contract", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ] + [
        {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
        for index, (method, params) in enumerate(calls, start=1)
    ]
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        replies = {}
        for message in messages:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            if "id" not in message:
                continue
            while message["id"] not in replies:
                line = process.stdout.readline()
                assert line, "server closed STDIO"
                reply = json.loads(line)
                if "id" in reply:
                    replies[reply["id"]] = reply
        return [replies[index] for index in range(1, len(calls) + 1)]
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@pytest.mark.parametrize("cache_state", ["prefetched", "cold"])
def test_offline_fixture_env_reindexes_with_network_denied_only_from_a_prefetched_cache(
    tmp_path, cache_state
):
    """End to end: the pilot's fixture grammar env reaches network-denied workers.

    With grammar downloads refused (offline manifest URL) and sandbox workers
    denying network, reindex succeeds from the prefetched cache and fails
    closed with ``grammar_unavailable`` from a cold one.
    """
    from mcp_server.utils import grammar_cache
    from scripts import v13_pmcp_pilot as pilot
    from tests.fixtures.multi_repo import boot_test_server, build_temp_repo

    warm = os.environ.get(grammar_cache.CACHE_DIR_ENV)
    if cache_state == "prefetched":
        if not warm or grammar_cache.missing_grammars(["python"]):
            if os.environ.get("MCP_REQUIRE_GRAMMARS") == "1":
                pytest.fail("MCP_REQUIRE_GRAMMARS=1 requires a prefetched grammar cache")
            pytest.skip("needs TREE_SITTER_LANGUAGE_PACK_CACHE_DIR with prefetched grammars")
        cache = warm
    else:
        cold = tmp_path / "cold-grammars"
        cold.mkdir(mode=0o700)
        cache = str(cold)

    repo, _ = build_temp_repo(
        tmp_path,
        "pilot_grammar_repo",
        seed_files={"probe.py": "def pilot_probe():\n    return 1\n"},
    )
    with boot_test_server(tmp_path, [repo], seed_indexes=False):
        pass
    env = {
        **pilot.clean_env(tmp_path),
        **pilot.grammar_env({"grammar_cache": cache}),
        "PYTHONPATH": str(pilot.REPO),
        "MCP_REPO_REGISTRY": str(tmp_path / "registry.json"),
        "MCP_ALLOWED_ROOTS": str(tmp_path),
        "SEMANTIC_SEARCH_ENABLED": "false",
        "MCP_SKIP_PLUGIN_PREINDEX": "true",
        "MCP_METRICS_PORT": "0",
        # clean_env() drops the test process's PYTHON_DOTENV_DISABLED; keep the
        # checkout's tracked .env.native out of this STDIO server too.
        "PYTHON_DOTENV_DISABLED": "1",
    }
    reindex, search = _raw_stdio_call(
        [sys.executable, "-m", "mcp_server.cli.stdio_runner"],
        env,
        str(repo),
        [
            ("tools/call", {"name": "reindex", "arguments": {"repository": str(repo)}}),
            (
                "tools/call",
                {
                    "name": "search_code",
                    "arguments": {
                        "repository": str(repo),
                        "query": "pilot_probe",
                        "semantic": False,
                    },
                },
            ),
        ],
    )
    payload = reindex["result"]["structuredContent"]
    if cache_state == "prefetched":
        assert reindex["result"]["isError"] is False, payload
        assert payload["mutation_performed"] is True
        assert search["result"]["structuredContent"]["results"]
    else:
        assert reindex["result"]["isError"] is True
        # The network-denied worker could not download the grammar, so since #117
        # the rebuild refuses as grammar-unavailable instead of publishing nothing.
        assert payload["code"] == "grammar_unavailable", payload
        assert payload["mutation_performed"] is False
        assert "GrammarUnavailableRebuildError" in payload["details"]
        assert not any(Path(cache).iterdir()), "a network-denied run wrote the cold cache"
