"""LOCALCI agent validation helper tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import agent_validation

REPO = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


def test_makefile_exposes_required_agent_targets() -> None:
    makefile = _read("Makefile")
    for target in (
        "agent-doctor:",
        "agent-fast:",
        "agent-gate:",
        "agent-full:",
        "agent-fix:",
        "agent-affected:",
    ):
        assert target in makefile


def test_docs_only_changes_route_to_agent_fast() -> None:
    target = agent_validation.classify_changed_paths(
        [
            "docs/development/agent-validation.md",
            "docs/status/localci-validation-contract.md",
            "README.md",
        ]
    )
    assert target == "fast"


def test_source_and_workflow_changes_route_to_agent_gate() -> None:
    assert agent_validation.classify_changed_paths(["mcp_server/cli/__init__.py"]) == "gate"
    assert (
        agent_validation.classify_changed_paths([".github/workflows/ci-cd-pipeline.yml"]) == "gate"
    )
    assert agent_validation.classify_changed_paths(["pyproject.toml"]) == "gate"


def test_agent_fast_stays_offline_by_default() -> None:
    plan = agent_validation.build_offload_plan("fast")
    assert plan.mode == "local"
    assert plan.command == ["make", "agent-fast-local"]


def test_remote_host_offload_fails_closed_without_ssh(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_REMOTE_HOST", "tailnet-host")
    monkeypatch.setattr(agent_validation, "_command_available", lambda name: False)
    try:
        agent_validation.build_offload_plan("gate")
    except RuntimeError as exc:
        assert "ssh" in str(exc)
    else:
        raise AssertionError("expected offload probe to fail closed")


def test_dagger_offload_requires_explicit_command(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_USE_DAGGER", "1")
    monkeypatch.delenv("AGENT_DAGGER_COMMAND", raising=False)
    monkeypatch.setattr(agent_validation, "_command_available", lambda name: name == "dagger")
    try:
        agent_validation.build_offload_plan("full")
    except RuntimeError as exc:
        assert "AGENT_DAGGER_COMMAND" in str(exc)
    else:
        raise AssertionError("expected dagger offload to fail closed without a command")


def test_changed_paths_includes_deleted_and_untracked_names(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_validation, "REPO", tmp_path)
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    tracked = tmp_path / "deleted source.py"
    tracked.write_text("pass\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    tracked.unlink()
    (tmp_path / "untracked source.py").write_text("pass\n")
    assert agent_validation._changed_paths() == ["deleted source.py", "untracked source.py"]
    assert agent_validation.classify_changed_paths(agent_validation._changed_paths()) == "gate"


def test_changed_paths_fails_closed_outside_git(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_validation, "REPO", tmp_path)
    with pytest.raises(RuntimeError, match="Cannot determine changed paths"):
        agent_validation._changed_paths()
