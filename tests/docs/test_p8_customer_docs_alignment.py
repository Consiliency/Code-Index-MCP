"""P8 / SL-1 — Customer-primary prose alignment checks.

Tests verify that README.md and docs/GETTING_STARTED.md match the post-P8
reality: beta-status admonition near the top of README, MCP tool-call JSON
as the primary usage surface (search_code + symbol_lookup), FastAPI REST
surface demoted to "secondary" admin role.

All stale-claim substrings enumerated as data so the test file itself does
not contain bare literal tokens that would self-match when someone greps
the whole repo; the checks read file contents, not ``__file__``.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
README_MD = REPO_ROOT / "README.md"
GETTING_STARTED_MD = REPO_ROOT / "docs" / "GETTING_STARTED.md"

# Stale substrings assembled from parts so this file doesn't re-match its own
# greps if someone later runs the repo-wide sweep over tests/ too.
_STALE_README_SUBSTRINGS = (
    "Production" + "-Ready",
    "Implementation Status: " + "Production",
    "100%" + " implemented",
    "fully " + "operational",
)


def _readme_text() -> str:
    return README_MD.read_text(encoding="utf-8")


def _getting_started_text() -> str:
    return GETTING_STARTED_MD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# README — stale-claim greps must return zero matches
# ---------------------------------------------------------------------------


def test_readme_no_stale_claims():
    text = _readme_text()
    hits = {s: text.count(s) for s in _STALE_README_SUBSTRINGS if s in text}
    assert hits == {}, f"README contains stale claims: {hits}"


# ---------------------------------------------------------------------------
# README — Beta-status admonition within first 80 lines
# ---------------------------------------------------------------------------


def test_readme_status_admonition_near_top():
    text = _readme_text()
    head = "\n".join(text.splitlines()[:80])
    assert (
        "> **Stable-surface release status**:" in head
    ), "README missing stable-surface status blockquote within first 80 lines"
    # Body must name MCP as primary and FastAPI/REST as secondary admin surface.
    # Search the full admonition block (contiguous blockquote lines).
    admonition_blocks = re.findall(r"(?:^>.*\n)+", text, re.MULTILINE)
    status_blocks = [b for b in admonition_blocks if "Stable-surface release status" in b]
    assert len(status_blocks) >= 1, "No stable-surface status admonition block found"
    status_body = "\n".join(status_blocks)
    assert "MCP" in status_body, "Status admonition does not name MCP as primary surface"
    assert ("FastAPI" in status_body) or (
        "REST" in status_body
    ), "Status admonition does not name FastAPI/REST as secondary admin surface"
    assert ("secondary" in status_body.lower()) or (
        "admin" in status_body.lower()
    ), "Status admonition does not mark FastAPI/REST as secondary/admin"


# ---------------------------------------------------------------------------
# README — MCP tool-call JSON examples for search_code and symbol_lookup
# ---------------------------------------------------------------------------


def test_readme_mcp_tool_names_present():
    text = _readme_text()
    assert "search_code" in text, "README missing 'search_code' MCP tool name"
    assert "symbol_lookup" in text, "README missing 'symbol_lookup' MCP tool name"


def test_readme_tool_call_json_arguments_objects():
    """Both tool names must appear in a JSON tool-call shape with an
    ``arguments`` object (``"tool": "<name>"`` plus ``"arguments": {...}``)."""
    text = _readme_text()
    for tool in ("search_code", "symbol_lookup"):
        # Look for {"tool": "<tool>"...} paired with an "arguments" object.
        # Match across lines; be permissive about whitespace ordering.
        pattern = r'"tool"\s*:\s*"' + re.escape(tool) + r'"[^{}]*?"arguments"\s*:\s*\{'
        assert re.search(
            pattern, text, re.DOTALL
        ), f"README missing tool-call JSON for '{tool}' with 'arguments' object"


# ---------------------------------------------------------------------------
# README — REST surface demoted, not primary
# ---------------------------------------------------------------------------


def test_readme_rest_demoted_to_secondary():
    text = _readme_text()
    # A heading exists that marks REST as secondary admin surface.
    assert re.search(
        r"^##\s+Admin REST Interface\s*\(secondary\)\s*$", text, re.MULTILINE
    ), "README missing '## Admin REST Interface (secondary)' heading"


# ---------------------------------------------------------------------------
# GETTING_STARTED — curl .*(search|symbol) must be zero
# ---------------------------------------------------------------------------


def test_getting_started_no_curl_rest_examples():
    text = _getting_started_text()
    matches = re.findall(r"curl .*(search|symbol)", text)
    assert matches == [], f"docs/GETTING_STARTED.md still contains curl REST examples: {matches}"


# ---------------------------------------------------------------------------
# GETTING_STARTED — MCP-primary content
# ---------------------------------------------------------------------------


def test_getting_started_has_mcp_json_registration():
    text = _getting_started_text()
    assert ".mcp.json" in text, "docs/GETTING_STARTED.md missing '.mcp.json' string"


def test_getting_started_has_tool_call_json_for_both_tools():
    text = _getting_started_text()
    for tool in ("search_code", "symbol_lookup"):
        pattern = r'"tool"\s*:\s*"' + re.escape(tool) + r'"[^{}]*?"arguments"\s*:\s*\{'
        assert re.search(
            pattern, text, re.DOTALL
        ), f"docs/GETTING_STARTED.md missing tool-call JSON for '{tool}'"


def test_getting_started_via_mcp_subsection_leads():
    """Via MCP Protocol subsection must appear before any Via REST API
    subsection in the Search Your Code section."""
    text = _getting_started_text()
    via_mcp = text.find("Via MCP Protocol")
    assert via_mcp != -1, "Missing 'Via MCP Protocol' subsection"
    via_rest = text.find("Via REST API")
    if via_rest != -1:
        assert via_mcp < via_rest, "'Via MCP Protocol' must appear before 'Via REST API' subsection"
        # REST subsection must not label itself primary.
        # Slice out just the REST subsection (up to the next H2/H3 heading).
        rest_tail = text[via_rest:]
        next_heading = re.search(r"\n##?#?\s", rest_tail[1:])
        rest_section = rest_tail[: next_heading.start() + 1] if next_heading else rest_tail
        assert (
            "primary" not in rest_section.lower()
        ), "'Via REST API' subsection must not describe itself as primary"
