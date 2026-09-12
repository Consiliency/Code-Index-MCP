"""GARECUT post-remediation RC recut contract checks."""

from __future__ import annotations

from pathlib import Path

from tests.test_release_metadata import EXPECTED_TAG, EXPECTED_VERSION

REPO = Path(__file__).parent.parent.parent

GA_CHECKLIST = REPO / "docs" / "validation" / "ga-readiness-checklist.md"
GA_RC = REPO / "docs" / "validation" / "ga-rc-evidence.md"
GA_FINAL = REPO / "docs" / "validation" / "ga-final-decision.md"
ROADMAP = REPO / "specs" / "phase-plans-v5.md"
WORKFLOW = REPO / ".github" / "workflows" / "release-automation.yml"
RELEASE_METADATA = REPO / "tests" / "test_release_metadata.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_rc8_contract_surfaces_and_workflow_path_are_frozen():
    workflow = _read(WORKFLOW)
    release_metadata = _read(RELEASE_METADATA)
    evidence = _read(GA_RC)

    for expected in (
        EXPECTED_TAG,
        EXPECTED_VERSION,
        "softprops/action-gh-release@718ea10b132b3b2eba29c1007bb80653f286566b",
    ):
        assert expected in workflow

    for expected in (EXPECTED_TAG, EXPECTED_VERSION, "protected-main"):
        assert expected in release_metadata

    for expected in ("v1.2.0-rc8", "1.2.0-rc8", "recut succeeded"):
        assert expected in evidence

    assert "softprops/action-gh-release@v2" not in workflow
    assert "latest" in workflow


def test_ga_rc_evidence_is_the_canonical_recut_artifact():
    checklist = _read(GA_CHECKLIST)
    evidence = _read(GA_RC)

    assert "docs/validation/ga-rc-evidence.md" in checklist
    assert evidence.startswith("> **Historical artifact")

    for expected in (
        "# GA RC Evidence",
        "plans/phase-plan-v5-garecut.md",
        "v1.2.0-rc8",
        "Release Automation",
        "PyPI",
        "GHCR",
        "prerelease",
        "latest",
        "softprops/action-gh-release@v3",
    ):
        assert expected in evidence

    assert (
        "blocked before dispatch" in evidence
        or "workflow failed after dispatch" in evidence
        or "recut succeeded" in evidence
    )


def test_final_decision_stays_historical_and_routes_to_the_next_phase_explicitly():
    decision = _read(GA_FINAL)
    roadmap = _read(ROADMAP)

    assert decision.startswith("> **Historical artifact")
    for expected in (
        "# GA Final Decision",
        "ship GA",
        "GAREL",
        "v1.2.0",
        "v1.2.0-rc8",
        "ready for GADISP planning",
        "authorized for downstream GADISP",
        "actions/download-artifact@v8",
        "accepted as non-blocking for GA",
        "softprops/action-gh-release@v3",
    ):
        assert expected in decision

    assert "- Final decision: `defer GA`." not in decision
    assert "GADISP planning/execution authorized" in decision
    assert "### Phase 9 — Post-Remediation RC Recut (GARECUT)" in roadmap
    assert "softprops/action-gh-release@v3" in roadmap
