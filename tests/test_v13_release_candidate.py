"""Release evidence consumption must not disguise changed runtime behavior."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.v13_release_candidate import CandidateRefused, compare_candidate, verify_pilot


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo: Path) -> str:
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "synthetic candidate")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def candidate(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Synthetic")
    git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    files = {
        "pyproject.toml": '[project]\nname="index-it-mcp"\nversion="1.4.0"\ndependencies=["x==1"]\n',
        "uv.lock": 'version=1\n[[package]]\nname="index-it-mcp"\nversion="1.4.0"\nsource={editable="."}\n[[package]]\nname="x"\nversion="1"\n',
        "mcp_server/__init__.py": '__version__ = "1.4.0"\n',
        "mcp_server/gateway.py": "VALUE = 1\n",
        "Dockerfile.production": "FROM python:3.12\n",
        ".github/workflows/release-automation.yml": "default: 'v1.4.0'\n",
        "scripts/install-mcp-docker.sh": 'MCP_VERSION="v1.4.0"\n',
        "README.md": "Published history\n",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    base = commit(tmp_path)
    for name in ("pyproject.toml", "uv.lock", "mcp_server/__init__.py"):
        path = tmp_path / name
        path.write_text(path.read_text().replace("1.4.0", "1.4.1"))
    commit(tmp_path)
    return tmp_path, base


def test_version_only_candidate_preserves_runtime(candidate):
    repo, base = candidate
    result = compare_candidate(repo, base)
    assert result["old_version"] == "1.4.0"
    assert result["new_version"] == "1.4.1"
    assert result["source"] == git(repo, "rev-parse", "HEAD")
    assert result["runtime_package_files"] == 2


@pytest.mark.parametrize(
    "path,content",
    [
        ("mcp_server/gateway.py", "VALUE = 2\n"),
        ("mcp_server/new.py", "VALUE = 1\n"),
        ("mcp_server/__init__.py", '__version__ = "1.4.1"\nraise RuntimeError()\n'),
        ("mcp_server/__init__.py", '__version__ = "1.4.1" # changed structure\n'),
        ("Dockerfile.production", "FROM python:3.13\n"),
        ("config.yaml", "endpoint: changed\n"),
        ("scripts/v13_pmcp_pilot.py", "pass\n"),
        (".github/workflows/release-automation.yml", "default: 'v1.4.1'\npermissions: write-all\n"),
        ("scripts/install-mcp-docker.sh", 'MCP_VERSION="v1.4.1"\necho unsafe\n'),
    ],
)
def test_runtime_changes_cannot_reuse_live_proof(candidate, path, content):
    repo, base = candidate
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    commit(repo)
    with pytest.raises(CandidateRefused):
        compare_candidate(repo, base)


@pytest.mark.parametrize("path", ["pyproject.toml", "uv.lock"])
def test_dependency_changes_cannot_reuse_live_proof(candidate, path):
    repo, base = candidate
    target = repo / path
    target.write_text(
        target.read_text().replace("x==1", "x==2").replace('version="1"', 'version="2"')
    )
    commit(repo)
    with pytest.raises(CandidateRefused):
        compare_candidate(repo, base)


def test_deleted_runtime_file_cannot_reuse_live_proof(candidate):
    repo, base = candidate
    (repo / "mcp_server/gateway.py").unlink()
    commit(repo)
    with pytest.raises(CandidateRefused):
        compare_candidate(repo, base)


@pytest.mark.parametrize("path", ["README.md", "mcp_server/gateway.py", "new.py"])
def test_uncommitted_candidate_is_refused(candidate, path):
    repo, base = candidate
    (repo / path).write_text("dirty\n")
    with pytest.raises(CandidateRefused, match="dirty_candidate"):
        compare_candidate(repo, base)


def test_metadata_only_changes_are_reported(candidate):
    repo, base = candidate
    (repo / "README.md").write_text("Prepared candidate, not publication\n")
    workflow = repo / ".github/workflows/release-automation.yml"
    workflow.write_text(workflow.read_text().replace("1.4.0", "1.4.1"))
    commit(repo)
    result = compare_candidate(repo, base)
    assert "README.md" in result["changed_files"]


def test_missing_pilot_receipt_is_refused(tmp_path):
    with pytest.raises(CandidateRefused, match="pilot_evidence_missing_or_invalid"):
        verify_pilot(tmp_path)


def test_invalid_commit_input_is_refused(candidate):
    repo, _ = candidate
    with pytest.raises(CandidateRefused, match="invalid_source_identity"):
        compare_candidate(repo, "--help")


@pytest.mark.parametrize("damage", ["status", "path", "hash", "binding"])
def test_tampered_pilot_metadata_is_refused(tmp_path, damage):
    root = tmp_path / ".phase-loop/runs/v13-PILOT-366f6bca7765"
    root.mkdir(parents=True)
    manifest = root / "manifest.json"
    manifest.write_text("{}")
    receipt = {
        "terminal_status": "complete",
        "verification_status": "passed",
        "manifest_path": str(manifest.relative_to(tmp_path)),
        "manifest_file_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }
    if damage == "status":
        receipt["verification_status"] = "failed"
    elif damage == "path":
        receipt["manifest_path"] = "outside.json"
    elif damage == "hash":
        receipt["manifest_file_sha256"] = "0" * 64
    path = tmp_path / "docs/validation/v13/PILOT.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(receipt))
    with pytest.raises(CandidateRefused):
        verify_pilot(tmp_path)
