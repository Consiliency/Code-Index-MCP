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


def test_readme_links_to_pmcp_guide_without_duplicating_pilot_command():
    readme_text = _readme_text()
    assert GUIDE_LINK in readme_text
    assert "PMCP fleet integration" in readme_text
    assert PINNED_COMMAND not in readme_text
