"""GAGOV governance contract checks."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parent.parent.parent

CHECKLIST = REPO / "docs" / "validation" / "ga-readiness-checklist.md"
DEPLOYMENT_RUNBOOK = REPO / "docs" / "operations" / "deployment-runbook.md"
USER_ACTION_RUNBOOK = REPO / "docs" / "operations" / "user-action-runbook.md"
GA_GOVERNANCE_EVIDENCE = REPO / "docs" / "validation" / "ga-governance-evidence.md"
RELEASE_GOVERNANCE_EVIDENCE = REPO / "docs" / "validation" / "release-governance-evidence.md"
RELEASE_AUTOMATION = REPO / ".github" / "workflows" / "release-automation.yml"
CI_PIPELINE = REPO / ".github" / "workflows" / "ci-cd-pipeline.yml"
CONTAINER_WORKFLOW = REPO / ".github" / "workflows" / "container-registry.yml"
LOCKFILE_WORKFLOW = REPO / ".github" / "workflows" / "lockfile-check.yml"

REQUIRED_GATES = [
    "Alpha Gate - Dependency Sync",
    "Alpha Gate - Format And Lint",
    "Alpha Gate - Unit And Release Smoke",
    "Alpha Gate - Integration Smoke",
    "Alpha Gate - Production Multi-Repo Matrix",
    "Alpha Gate - Docker Build And Smoke",
    "Alpha Gate - Docs Truth",
    "Alpha Gate - Required Gates Passed",
]

REQUIRED_POLICY_TERMS = [
    "v1.2.0-rc5",
    "v2.15.0-alpha.1",
    "GitHub Latest",
    "auto_merge=false",
    "Docker latest",
]
RUNBOOK_POLICY_TERMS = [
    "v1.2.0-rc8",
    "v2.15.0-alpha.1",
    "GitHub Latest",
    "auto_merge=false",
    "Docker latest",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalized(path: Path) -> str:
    return " ".join(_read(path).split())


def test_gagov_artifact_exists_and_references_frozen_gate_source():
    checklist = _read(CHECKLIST)
    evidence = _read(GA_GOVERNANCE_EVIDENCE)

    assert "## Required gates" in checklist
    assert "docs/validation/ga-readiness-checklist.md" in evidence
    assert "Enforcement disposition: `enforced via branch protection`" in evidence

    for gate in REQUIRED_GATES:
        assert gate in checklist
        assert gate in evidence


def test_runbooks_record_enforced_governance_and_blocker_response():
    for path, required_heading in (
        (DEPLOYMENT_RUNBOOK, "## GAGOV Governance Status"),
        (USER_ACTION_RUNBOOK, "### 3.5A Phase GAGOV - Enforced Release Governance"),
    ):
        text = _normalized(path)

        assert required_heading in text, path
        assert "enforced via branch protection" in text, path
        assert "Earlier RELGOV/GACLOSE evidence captured" in text, path
        assert "manual enforcement" in text, path
        assert "If a later probe shows branch protection drift" in text, path
        assert "docs/validation/ga-governance-evidence.md" in text, path

        for gate in REQUIRED_GATES:
            assert gate in text, f"{path} missing {gate!r}"

        for term in RUNBOOK_POLICY_TERMS:
            assert term in text, f"{path} missing {term!r}"


def test_ga_governance_evidence_records_redacted_settings_and_policy_metadata():
    text = _read(GA_GOVERNANCE_EVIDENCE)

    for expected in (
        "Repository: `ViperJuice/Code-Index-MCP`",
        "Default branch: `main`",
        "Visibility: `PUBLIC`",
        "Branch protection: `enabled`",
        "Repository rulesets: `none`",
        "Required status checks: `strict`",
        "Required pull request reviews: `1 approving review`",
        "Dismiss stale reviews: `true`",
        "Require conversation resolution: `true`",
        "Require linear history: `true`",
        "Enforce for administrators: `true`",
        "Policy accepted by: `repository operator`",
        "manual enforcement",
    ):
        assert expected in text

    for gate in REQUIRED_GATES:
        assert gate in text

    for term in REQUIRED_POLICY_TERMS:
        assert term in text

    for forbidden in ("gho_", "Authorization: Bearer", '"token":', '"password":'):
        assert forbidden not in text


def test_workflows_and_evidence_share_release_channel_policy():
    surfaces = "\n".join(
        [
            _read(RELEASE_AUTOMATION),
            _read(CI_PIPELINE),
            _read(CONTAINER_WORKFLOW),
            _read(LOCKFILE_WORKFLOW),
            _read(GA_GOVERNANCE_EVIDENCE),
            _read(RELEASE_GOVERNANCE_EVIDENCE),
        ]
    )

    for gate in REQUIRED_GATES:
        assert gate in surfaces

    for expected in (
        "release_type=custom",
        'tags+=(--tag "${IMAGE_REF}:latest")',
        "Alpha Gate - Docker Build And Smoke",
        "Alpha Gate - Dependency Sync",
    ):
        assert expected in surfaces
