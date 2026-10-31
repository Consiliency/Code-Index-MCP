"""GITHUB_TOKEN least privilege: no workflow inherits the repository default.

Every workflow declares top-level permissions that never grant a write scope. Write
scopes appear only on the jobs that publish, sign, attest, upload SARIF or mutate
PRs and releases, and each one is listed here so that a new grant needs a review.
Refs Consiliency/Code-Index-MCP#106.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# (workflow, job) -> the write scopes that job needs; anything else must be read/none.
ALLOWED_JOB_WRITES = {
    ("ci-cd-pipeline.yml", "informational-security-scan"): {"security-events"},
    ("container-registry.yml", "informational-container-security-scan"): {"security-events"},
    ("container-registry.yml", "informational-push-manifests"): {"packages"},
    ("container-registry.yml", "informational-sign-images"): {"packages", "id-token"},
    ("release-automation.yml", "create-release-pr"): {"pull-requests"},
    ("release-automation.yml", "enable-release-auto-merge"): {"contents", "pull-requests"},
    ("release-automation.yml", "claim-release"): {"contents"},
    ("release-automation.yml", "build-release"): {"packages", "id-token"},
    ("release-automation.yml", "publish-release"): {"contents", "id-token"},
    ("sign-published-image.yml", "sign-published-image"): {"packages", "id-token"},
    ("sign-published-image.yml", "attest-local-index"): {"id-token", "attestations"},
}


def _workflows():
    paths = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert paths
    for path in paths:
        yield path.name, yaml.safe_load(path.read_text(encoding="utf-8"))


def _writes(permissions) -> set[str]:
    if isinstance(permissions, str):
        # "write-all" or "read-all"; only the latter is acceptable anywhere.
        return {"*"} if permissions != "read-all" else set()
    return {scope for scope, level in (permissions or {}).items() if level == "write"}


def test_every_workflow_declares_read_only_top_level_permissions() -> None:
    for name, workflow in _workflows():
        assert "permissions" in workflow, f"{name} inherits the repository default token"
        assert _writes(workflow["permissions"]) == set(), f"{name} grants write at top level"


def test_job_write_scopes_are_explicitly_allowed() -> None:
    seen = set()
    for name, workflow in _workflows():
        for job_id, job in workflow["jobs"].items():
            writes = _writes(job.get("permissions"))
            allowed = ALLOWED_JOB_WRITES.get((name, job_id), set())
            assert writes <= allowed, f"{name}:{job_id} has unreviewed write scopes {writes}"
            if writes:
                seen.add((name, job_id))
    assert seen == set(ALLOWED_JOB_WRITES), "allowlist names a job that no longer writes"
