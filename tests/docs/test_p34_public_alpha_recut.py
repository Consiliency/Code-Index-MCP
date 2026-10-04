"""P34 public-alpha recut documentation contract checks."""

from __future__ import annotations

from pathlib import Path

from tests.test_release_metadata import EXPECTED_TAG, EXPECTED_VERSION

REPO = Path(__file__).parent.parent.parent

PREPARED_STABLE_VERSION = "1.4.0"
PREPARED_STABLE_TAG = "v1.4.0"
PUBLIC_ALPHA_VERSION = "1.2.0-rc8"
PUBLIC_ALPHA_TAG = "v1.2.0-rc8"
CURRENT_RECUT_VERSION = "1.2.0-rc8"
CURRENT_RECUT_TAG = "v1.2.0-rc8"

PUBLIC_SURFACES = [
    "README.md",
    "docs/GETTING_STARTED.md",
    "docs/MCP_CONFIGURATION.md",
    "docs/DOCKER_GUIDE.md",
    "docs/operations/deployment-runbook.md",
    "docs/operations/user-action-runbook.md",
    "docs/SUPPORT_MATRIX.md",
    "AGENTS.md",
    "CLAUDE.md",
]

ACTIVE_RC4_DRIFT_SURFACES = [
    "docs/DEPLOYMENT-GUIDE.md",
    "docs/PRODUCTION_DEPLOYMENT_GUIDE.md",
    "docs/DYNAMIC_PLUGIN_LOADING.md",
    "docs/api/API-REFERENCE.md",
    "scripts/install-mcp-docker.sh",
    "scripts/install-mcp-docker.ps1",
]

MODEL_TERMS = [
    "many unrelated repositories",
    "one registered worktree",
    "git common directory",
    "tracked/default branch",
    "readiness",
    "ready",
    "index_unavailable",
    "safe_fallback",
    "native_search",
]

FORBIDDEN_UNCONDITIONAL_AGENT_PHRASES = [
    "Always search with MCP tools",
    "before using Grep or Glob",
    "ONLY if MCP search returns 0 results",
    "MCP-FIRST",
]

FORBIDDEN_UNRESTRICTED_CLAIMS = [
    "unrestricted multi-worktree",
    "unrestricted multi-branch",
    "indexes every branch",
    "all worktrees are supported",
]


def _read(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


def _normalized(relative: str) -> str:
    return " ".join(_read(relative).replace("\n> ", " ").split())


def test_public_surfaces_share_v3_operating_model():
    for relative in PUBLIC_SURFACES:
        text = _normalized(relative)
        missing = [term for term in MODEL_TERMS if term not in text]
        assert missing == [], f"{relative} missing {missing}"


def test_release_surfaces_use_prepared_stable_identifier_and_preserve_rc_history():
    surfaces = [
        "README.md",
        "docs/GETTING_STARTED.md",
        "docs/MCP_CONFIGURATION.md",
        "docs/DOCKER_GUIDE.md",
        "docs/operations/deployment-runbook.md",
        "docs/SUPPORT_MATRIX.md",
        "CHANGELOG.md",
    ]
    for relative in surfaces:
        text = _read(relative)
        assert PREPARED_STABLE_VERSION in text, relative
        if relative != "CHANGELOG.md":
            assert "1.2.0-rc4" not in text, relative
    assert PUBLIC_ALPHA_VERSION in _read("CHANGELOG.md")
    assert EXPECTED_TAG in _read(".github/workflows/release-automation.yml")


def test_active_release_instructions_do_not_reference_rc4_or_stale_recut_target():
    for relative in ACTIVE_RC4_DRIFT_SURFACES:
        text = _read(relative)
        if relative in {"scripts/install-mcp-docker.sh", "scripts/install-mcp-docker.ps1"}:
            assert EXPECTED_TAG in text, relative
        assert (
            EXPECTED_VERSION in text
            or PREPARED_STABLE_VERSION in text
            or PREPARED_STABLE_TAG in text
            or PUBLIC_ALPHA_VERSION in text
            or PUBLIC_ALPHA_TAG in text
            or CURRENT_RECUT_VERSION in text
            or CURRENT_RECUT_TAG in text
        ), relative
        assert "1.2.0-rc4" not in text, relative
        assert "v1.2.0-rc4" not in text, relative


def test_agent_docs_are_readiness_gated():
    for relative in ("AGENTS.md", "CLAUDE.md"):
        text = _read(relative)
        for expected in ("get_status", "index_unavailable", "safe_fallback", "native_search"):
            assert expected in text, f"{relative} missing {expected}"
        for forbidden in FORBIDDEN_UNCONDITIONAL_AGENT_PHRASES:
            assert forbidden not in text, f"{relative} still contains {forbidden!r}"


def test_public_alpha_checklist_names_required_p34_gates():
    text = _read("docs/operations/deployment-runbook.md")
    for expected in (
        "P27-P33",
        "make alpha-production-matrix",
        "uv run pytest tests/smoke tests/docs tests/test_release_metadata.py",
        "make release-smoke release-smoke-container",
        "tests/docs/test_p34_public_alpha_recut.py",
        "git tag -l v1.2.0-rc8",
    ):
        assert expected in text


def test_changelog_records_recut_and_remaining_limitations():
    text = _normalized("CHANGELOG.md")
    for expected in (
        "## [1.2.0-rc5] — 2026-04-23",
        "P27-P33",
        "P34",
        "many unrelated repositories",
        "one registered worktree",
        "tracked/default branch",
        "index_unavailable",
        "safe_fallback",
        "native_search",
        "docs/SUPPORT_MATRIX.md",
        "docs/operations/deployment-runbook.md",
        "docs/validation/private-alpha-decision.md",
    ):
        assert expected in text
    for forbidden in FORBIDDEN_UNRESTRICTED_CLAIMS:
        assert forbidden not in text
