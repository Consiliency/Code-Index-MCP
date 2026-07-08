"""Docs contract for the PMCP fleet integration pilot guide."""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
README_MD = REPO_ROOT / "README.md"
GUIDE_MD = REPO_ROOT / "docs" / "guides" / "pmcp-fleet-integration.md"

GUIDE_LINK = "docs/guides/pmcp-fleet-integration.md"
ISSUE_URL = "https://github.com/ViperJuice/pmcp/issues/89"
PINNED_COMMAND = "uvx --from index-it-mcp==1.2.0 index-it-mcp stdio"


def _readme_text() -> str:
    return README_MD.read_text(encoding="utf-8")


def _guide_text() -> str:
    assert GUIDE_MD.exists(), "Expected docs/guides/pmcp-fleet-integration.md to exist"
    return GUIDE_MD.read_text(encoding="utf-8")


def test_guide_freezes_local_override_and_upstream_tracker():
    text = _guide_text()
    for expected in (
        ISSUE_URL,
        PINNED_COMMAND,
        '"command": "uvx"',
        '"--from"',
        '"index-it-mcp==1.2.0"',
        '"index-it-mcp"',
        '"stdio"',
        ".mcp.json",
    ):
        assert expected in text


def test_guide_explains_stdio_vs_serve_and_secret_posture():
    text = _guide_text()
    for expected in (
        "`index-it-mcp serve`",
        "FastAPI admin/debug surface",
        "child-process MCP transport",
        "MCP_CLIENT_SECRET",
        "unset",
        "handshake",
        "running unauthenticated",
    ):
        assert expected in text


def test_guide_freezes_pin_and_rejects_floating_examples():
    text = _guide_text()
    for expected in (
        "index-it-mcp==1.2.0",
        "multiple `index-it-mcp` release lines",
        "Do not use `uvx index-it-mcp`",
    ):
        assert expected in text

    forbidden = (
        "uvx index-it-mcp stdio",
        "uvx index-it-mcp serve",
        '"args": ["--from", "index-it-mcp", "index-it-mcp", "stdio"]',
    )
    for snippet in forbidden:
        assert snippet not in text


def test_guide_freezes_pmcp_bootstrap_env_and_preflight_commands():
    text = _guide_text()
    for expected in (
        "MCP_ALLOWED_ROOTS",
        "MCP_REPO_REGISTRY",
        "SEMANTIC_SEARCH_ENABLED",
        "SEMANTIC_DEFAULT_PROFILE",
        "SEMANTIC_EMBEDDING_BASE_URL",
        "QDRANT_URL",
        "SEMANTIC_AUTOSTART_QDRANT",
        "MCP_AUTO_INDEX",
        "mcp-index setup semantic --dry-run",
        "--profile oss_high",
        "--qdrant-url http://localhost:6333",
        "--openai-api-base http://ai:8001/v1",
        "does not create collections",
        "does not write semantic vectors",
        "does not start long-running indexing",
    ):
        assert expected in text

    assert '"MCP_QDRANT_URL":' not in text


def test_guide_freezes_repository_bootstrap_and_readiness_fallback_vocabulary():
    text = _guide_text()
    for expected in (
        "one registered worktree per git common directory",
        "mcp-index repository register",
        "same `MCP_INDEX_STORAGE_PATH` and `MCP_REPO_REGISTRY`",
        "mcp-index repository list -v",
        "mcp-index repository status",
        "mcp-index artifact workspace-status",
        "`ready`",
        "`stale_commit`",
        "`wrong_branch`",
        "`missing_index`",
        "`path_outside_allowed_roots`",
        "`index_unavailable`",
        '`safe_fallback: "native_search"`',
        "`reindex`",
        "`repository sync`",
    ):
        assert expected in text


def test_guide_defines_fleet_rollout_policy_and_enablement_thresholds():
    text = _guide_text()
    for expected in (
        "## Fleet Rollout Policy",
        "Pilot repos",
        "Core engineering repos",
        "Long-tail repos",
        "Opt-out repos",
        "Repos requiring manual constraints",
        "lazy-started",
        "fleet-wide auto-start is not allowed",
        "broad auto-registration is not allowed",
        "must use native search whenever PMCP-mediated Code-Index-MCP readiness is not `ready`",
        "Semantic indexing is enabled only when",
        "Lexical-only PMCP use is acceptable only after lexical readiness is `ready`",
        "Qdrant",
        "local embedding endpoint",
        "`oss_high` profile",
        "PMCP `1.19.1`",
        "configured registry",
        "repository readiness",
    ):
        assert expected in text


def test_guide_covers_rollout_troubleshooting_matrix():
    text = _guide_text()
    for expected in (
        "## Troubleshooting Matrix",
        "wrong transport",
        "wrong package version",
        "missing allowed roots",
        "missing Qdrant",
        "missing embedding endpoint",
        "wrong branch",
        "stale commit",
        "missing index",
        "`path_outside_allowed_roots`",
        "system PMCP service",
        "command mode",
        "remote URL",
        "repositories: []",
        "unregistered_repository",
        "MCP_REPO_REGISTRY",
        "CPython 3.13",
    ):
        assert expected in text


def test_readme_links_to_pmcp_guide_without_duplicating_pilot_command():
    readme_text = _readme_text()
    assert GUIDE_LINK in readme_text
    assert "PMCP fleet integration" in readme_text
    assert "docs/status/PMCP_FLEET_PILOT.md" in readme_text
    assert "controlled lexical PMCP use only for PMCP-ready pilot repos" in readme_text
    assert "no semantic or fleet-wide indexing rollout" in readme_text
    assert PINNED_COMMAND not in readme_text
