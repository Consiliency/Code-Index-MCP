#!/usr/bin/env python3
"""Repo-local validation command router for LOCALCI."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from mcp_server.utils.subprocess_env import get_command_availability, get_full_env

REPO = Path(__file__).resolve().parents[1]
DOCS_ONLY_PREFIXES = ("docs/",)
DOCS_ONLY_FILES = {
    "README.md",
    "CHANGELOG.md",
    "AGENTS.md",
    "CLAUDE.md",
    "docs/status/localci-validation-contract.md",
}
GATE_PREFIXES = (".github/workflows/", "mcp_server/", "scripts/", "tests/")
GATE_FILES = {"Makefile", "pyproject.toml", "uv.lock"}
TARGET_TO_LOCAL = {
    "fast": "agent-fast-local",
    "gate": "agent-gate-local",
    "full": "agent-full-local",
    "fix": "agent-fix-local",
}


@dataclass(frozen=True)
class OffloadPlan:
    mode: str
    command: list[str]


def _run(cmd: Sequence[str]) -> int:
    return subprocess.run(list(cmd), cwd=REPO, check=False, env=get_full_env()).returncode


def _is_docs_only(path: str) -> bool:
    return path in DOCS_ONLY_FILES or path.startswith(DOCS_ONLY_PREFIXES)


def classify_changed_paths(paths: Sequence[str]) -> str:
    cleaned = [path for path in paths if path]
    if not cleaned:
        return "fast"
    if all(_is_docs_only(path) for path in cleaned):
        return "fast"
    for path in cleaned:
        if path in GATE_FILES or path.startswith(GATE_PREFIXES):
            return "gate"
        if not _is_docs_only(path):
            return "gate"
    return "gate"


def _command_available(name: str) -> bool:
    return get_command_availability(name).available


def build_offload_plan(target: str) -> OffloadPlan:
    local_target = TARGET_TO_LOCAL[target]
    if os.environ.get("AGENT_USE_DAGGER") == "1":
        if not _command_available("dagger"):
            raise RuntimeError("Dagger offload requested but `dagger` is unavailable.")
        dagger_command = os.environ.get("AGENT_DAGGER_COMMAND")
        if not dagger_command:
            raise RuntimeError(
                "Dagger offload requested but AGENT_DAGGER_COMMAND is not configured."
            )
        return OffloadPlan("dagger", ["sh", "-lc", dagger_command])

    remote_host = os.environ.get("AGENT_REMOTE_HOST")
    if remote_host:
        if not _command_available("ssh"):
            raise RuntimeError("Remote-host offload requested but `ssh` is unavailable.")
        remote_command = os.environ.get("AGENT_REMOTE_COMMAND")
        if not remote_command:
            remote_command = f"cd {REPO} && make {local_target}"
        return OffloadPlan("remote-host", ["ssh", remote_host, remote_command])

    return OffloadPlan("local", ["make", local_target])


def _changed_paths() -> list[str]:
    commands = [
        ["git", "diff", "--name-only", "-z", "--diff-filter=ACMRD", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
    ]
    paths = set()
    for cmd in commands:
        result = subprocess.run(
            cmd,
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
            env=get_full_env(),
        )
        if result.returncode != 0:
            raise RuntimeError(
                "Cannot determine changed paths; refusing a reduced validation gate."
            )
        paths.update(path for path in result.stdout.split("\0") if path)
    return sorted(paths)


def cmd_doctor() -> int:
    for line in [
        "agent_validation_mode=doctor",
        f"repo={REPO}",
        f"uv_available={_command_available('uv')}",
        f"pytest_available={_command_available('pytest')}",
        f"docker_available={_command_available('docker')}",
        f"dagger_available={_command_available('dagger')}",
        f"ssh_available={_command_available('ssh')}",
        f"remote_host_configured={bool(os.environ.get('AGENT_REMOTE_HOST'))}",
        f"dagger_requested={os.environ.get('AGENT_USE_DAGGER') == '1'}",
    ]:
        print(line)
    return 0


def cmd_run(target: str) -> int:
    plan = build_offload_plan(target)
    print(f"selected_mode={plan.mode}")
    print(f"selected_target={target}")
    print(f"command_name={plan.command[0]}")
    print("command_redacted=true")
    return _run(plan.command)


def cmd_affected(paths: Sequence[str] | None) -> int:
    selected_paths = list(paths) if paths else _changed_paths()
    target = classify_changed_paths(selected_paths)
    print(f"classification={target}")
    for path in selected_paths:
        print(f"path={path}")
    return cmd_run(target)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("target", choices=sorted(TARGET_TO_LOCAL))

    affected_parser = subparsers.add_parser("affected")
    affected_parser.add_argument("paths", nargs="*")

    args = parser.parse_args()
    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "run":
        return cmd_run(args.target)
    return cmd_affected(args.paths)


if __name__ == "__main__":
    sys.exit(main())
