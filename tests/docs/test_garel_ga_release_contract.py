"""GAREL GA decision and release contract checks."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parent.parent.parent

GA_CHECKLIST = REPO / "docs" / "validation" / "ga-readiness-checklist.md"
GA_GOVERNANCE = REPO / "docs" / "validation" / "ga-governance-evidence.md"
GA_E2E = REPO / "docs" / "validation" / "ga-e2e-evidence.md"
GA_OPS = REPO / "docs" / "validation" / "ga-operations-evidence.md"
GA_RC = REPO / "docs" / "validation" / "ga-rc-evidence.md"
GA_FINAL = REPO / "docs" / "validation" / "ga-final-decision.md"
GA_RELEASE = REPO / "docs" / "validation" / "ga-release-evidence.md"
ROADMAP = REPO / "specs" / "phase-plans-v5.md"
WORKFLOW = REPO / ".github" / "workflows" / "release-automation.yml"
README = REPO / "README.md"
GETTING_STARTED = REPO / "docs" / "GETTING_STARTED.md"
MCP_CONFIGURATION = REPO / "docs" / "MCP_CONFIGURATION.md"
DOCKER_GUIDE = REPO / "docs" / "DOCKER_GUIDE.md"
SUPPORT_MATRIX = REPO / "docs" / "SUPPORT_MATRIX.md"
DEPLOYMENT_RUNBOOK = REPO / "docs" / "operations" / "deployment-runbook.md"
USER_ACTION_RUNBOOK = REPO / "docs" / "operations" / "user-action-runbook.md"

PUBLIC_SURFACES = (
    README,
    GETTING_STARTED,
    MCP_CONFIGURATION,
    DOCKER_GUIDE,
    SUPPORT_MATRIX,
    DEPLOYMENT_RUNBOOK,
    USER_ACTION_RUNBOOK,
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_final_decision_exists_and_cites_all_ga_inputs():
    checklist = _read(GA_CHECKLIST)
    governance = _read(GA_GOVERNANCE)
    e2e = _read(GA_E2E)
    ops = _read(GA_OPS)
    rc = _read(GA_RC)
    decision = _read(GA_FINAL)

    assert decision.startswith("> **Historical artifact")
    assert "docs/validation/ga-final-decision.md" in checklist
    assert "enforced via branch protection" in governance
    assert "ready" in e2e
    assert "plans/phase-plan-v5-gaops.md" in ops
    assert "recut succeeded" in rc

    for expected in (
        "# GA Final Decision",
        "## Summary",
        "## Decision Inputs",
        "## Workflow Runtime Disposition",
        "## Final Decision",
        "## Stable Surface Preparation",
        "## Historical RC Evidence",
        "## Next Scope",
        "## Verification",
        "ship GA",
        "docs/validation/ga-readiness-checklist.md",
        "docs/validation/ga-governance-evidence.md",
        "docs/validation/ga-e2e-evidence.md",
        "docs/validation/ga-operations-evidence.md",
        "docs/validation/ga-rc-evidence.md",
        "v1.2.0",
        "v1.2.0-rc8",
        "softprops/action-gh-release@v3",
        "actions/download-artifact@v8",
        "v8.0.1",
        "Buffer()",
        "specs/phase-plans-v5.md",
        "GARECUT",
        "GAREL",
    ):
        assert expected in decision

    assert "- Final decision: `defer GA`." not in decision
    assert "- Final decision: `cut another RC`." not in decision


def test_ship_decision_defers_release_evidence_to_gadisp_and_keeps_public_surfaces_pre_dispatch():
    decision = _read(GA_FINAL)

    assert "- Final decision: `ship GA`." in decision
    assert "Stable GA dispatch: `authorized for downstream GADISP`" in decision
    assert "ga-release-evidence.md`" in decision
    assert "remains intentionally absent until downstream GADISP" in decision
    if GA_RELEASE.exists():
        release = _read(GA_RELEASE)
        assert "GADISP" in release
        assert "stable dispatch succeeded" in release
        assert "v1.2.0" in release
    else:
        assert "remains intentionally absent until downstream GADISP" in decision

    combined = "\n".join(_read(path) for path in PUBLIC_SURFACES).lower()
    for expected in ("stable", "github latest", "docker `latest`"):
        assert expected in combined
    for path in (README, GETTING_STARTED, MCP_CONFIGURATION, DOCKER_GUIDE, SUPPORT_MATRIX):
        text = _read(path)
        assert "1.4.0" in text, f"{path.relative_to(REPO)} missing stable surface version"


def test_workflow_runtime_warning_is_remediated_before_any_future_ga_dispatch():
    workflow = _read(WORKFLOW)
    decision = _read(GA_FINAL)
    roadmap = _read(ROADMAP)

    assert "prepare-release-pr:" in workflow
    assert "inputs.auto_merge == 'true'" in workflow
    assert 'gh release create "$RELEASE_VERSION"' in workflow
    assert "--verify-tag" in workflow
    assert "softprops/action-gh-release@v2" not in workflow
    assert "GitHub's latest published release is still" in decision
    assert "`v8.0.1`" in decision
    assert "Stable surface status: `prepared for downstream GADISP`" in decision
    assert "actions/download-artifact@v8" in decision
    assert "Buffer()" in decision
    assert "accepted as non-blocking for GA" in decision
    assert "GADISP planning/execution authorized" in decision
    assert "### Phase 8 — GA Stable Dispatch And Release Evidence (GADISP)" in roadmap
    assert "accepted as non-blocking for GA dispatch" in roadmap.replace("\n", " ")
    assert "softprops/action-gh-release@v3" in roadmap
