"""GARC follow-up RC soak contract checks."""

from __future__ import annotations

from pathlib import Path

from tests.test_release_metadata import EXPECTED_TAG

REPO = Path(__file__).parent.parent.parent
ACTIVE_STABLE_TAG = EXPECTED_TAG

GA_CHECKLIST = REPO / "docs" / "validation" / "ga-readiness-checklist.md"
GA_GOVERNANCE = REPO / "docs" / "validation" / "ga-governance-evidence.md"
GA_E2E = REPO / "docs" / "validation" / "ga-e2e-evidence.md"
GA_OPS = REPO / "docs" / "validation" / "ga-operations-evidence.md"
GA_RC = REPO / "docs" / "validation" / "ga-rc-evidence.md"
RELEASE_WORKFLOW = REPO / ".github" / "workflows" / "release-automation.yml"
PYPROJECT = REPO / "pyproject.toml"
PACKAGE = REPO / "mcp_server" / "__init__.py"
CHANGELOG = REPO / "CHANGELOG.md"
README = REPO / "README.md"
GETTING_STARTED = REPO / "docs" / "GETTING_STARTED.md"
MCP_CONFIGURATION = REPO / "docs" / "MCP_CONFIGURATION.md"
DOCKER_GUIDE = REPO / "docs" / "DOCKER_GUIDE.md"
SUPPORT_MATRIX = REPO / "docs" / "SUPPORT_MATRIX.md"
DEPLOYMENT_RUNBOOK = REPO / "docs" / "operations" / "deployment-runbook.md"
USER_ACTION_RUNBOOK = REPO / "docs" / "operations" / "user-action-runbook.md"
RELEASE_METADATA = REPO / "tests" / "test_release_metadata.py"

SURFACES = [
    RELEASE_WORKFLOW,
    PYPROJECT,
    PACKAGE,
    CHANGELOG,
    README,
    GETTING_STARTED,
    MCP_CONFIGURATION,
    DOCKER_GUIDE,
    SUPPORT_MATRIX,
    DEPLOYMENT_RUNBOOK,
    USER_ACTION_RUNBOOK,
    RELEASE_METADATA,
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_rc8_contract_surfaces_are_frozen():
    for path in (GA_RC, CHANGELOG, REPO / "docs" / "validation" / "ga-final-decision.md"):
        text = _read(path)
        assert "1.2.0-rc8" in text or "v1.2.0-rc8" in text, path

    workflow = _read(RELEASE_WORKFLOW)
    assert f"default: '{ACTIVE_STABLE_TAG}'" in workflow
    assert "default: 'prepare'" in workflow
    assert "inputs.mode == 'publish' && github.ref == 'refs/heads/main'" in workflow
    assert "auto_merge=false" in "\n".join([_read(DEPLOYMENT_RUNBOOK), _read(USER_ACTION_RUNBOOK)])


def test_public_surfaces_preserve_rc_only_channel_posture():
    combined = "\n".join(
        [
            _read(README),
            _read(GETTING_STARTED),
            _read(MCP_CONFIGURATION),
            _read(DOCKER_GUIDE),
            _read(SUPPORT_MATRIX),
            _read(DEPLOYMENT_RUNBOOK),
            _read(USER_ACTION_RUNBOOK),
        ]
    ).lower()

    for expected in ("public-alpha", "beta", "docker `latest`", "github latest"):
        assert expected in combined

    for forbidden in ("active stable surface", "v1.2.0 is the active public version"):
        assert forbidden not in combined


def test_runbooks_freeze_pre_dispatch_and_observation_commands():
    combined = "\n".join([_read(DEPLOYMENT_RUNBOOK), _read(USER_ACTION_RUNBOOK)])

    for expected in (
        "git status --short --branch",
        "git fetch origin main --tags --prune",
        "git rev-parse HEAD origin/main",
        "git tag -l v1.2.0-rc8",
        "git ls-remote --tags origin refs/tags/v1.2.0-rc8",
        'gh workflow view "Release Automation"',
        'gh workflow run "Release Automation" -f version=v1.2.0-rc8 -f release_type=custom -f auto_merge=false',
        'gh run list --workflow "Release Automation" --limit 10',
        "gh run watch <run-id> --exit-status",
        "gh run view <run-id> --json url,headSha,status,conclusion,jobs",
        "gh release view v1.2.0-rc8 --repo ViperJuice/Code-Index-MCP --json tagName,isPrerelease,isDraft,publishedAt,targetCommitish,url,assets",
        "recut succeeded",
        "ga-rc-evidence.md",
    ):
        assert expected in combined


def test_ga_rc_evidence_exists_and_records_blocked_or_observed_state():
    checklist = _read(GA_CHECKLIST)
    governance = _read(GA_GOVERNANCE)
    e2e = _read(GA_E2E)
    ops = _read(GA_OPS)
    evidence = _read(GA_RC)

    assert evidence.startswith("> **Historical artifact")
    assert "docs/validation/ga-rc-evidence.md" in checklist
    assert "enforced via branch protection" in governance
    assert "ready" in e2e
    assert "plans/phase-plan-v5-gaops.md" in ops

    for expected in (
        "# GA RC Evidence",
        "## Summary",
        "## Pre-dispatch Qualification",
        "## Intended Dispatch Inputs",
        "## Workflow Observation",
        "## Release-Channel Policy",
        "## Rollback And Next-Step Disposition",
        "## Verification",
        "plans/phase-plan-v5-garc.md",
        "v1.2.0-rc8",
        "git status --short --branch",
        'gh workflow view "Release Automation"',
        "auto_merge=false",
        "stable-only",
        "Release Automation",
    ):
        assert expected in evidence

    assert (
        "blocked before dispatch" in evidence
        or "follow-up RC soak succeeded" in evidence
        or "recut succeeded" in evidence
        or "workflow failed after dispatch" in evidence
    )

    for forbidden in ("gho_", "Authorization: Bearer", '"password":'):
        assert forbidden not in evidence
