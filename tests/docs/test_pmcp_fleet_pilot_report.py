"""Docs contract for the PMCP fleet pilot status report."""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
GUIDE_MD = REPO_ROOT / "docs" / "guides" / "pmcp-fleet-integration.md"
REPORT_MD = REPO_ROOT / "docs" / "status" / "PMCP_FLEET_PILOT.md"
STARTUP_JSON = REPO_ROOT / "docs" / "status" / "pmcp-pilot" / "startup-evidence.json"
READINESS_JSON = REPO_ROOT / "docs" / "status" / "pmcp-pilot" / "repository-readiness-evidence.json"
QUERY_JSON = REPO_ROOT / "docs" / "status" / "pmcp-pilot" / "query-evidence.json"

REPORT_LINK = "docs/status/PMCP_FLEET_PILOT.md"


def _read(path: Path) -> str:
    assert path.exists(), f"Expected {path.relative_to(REPO_ROOT)} to exist"
    return path.read_text(encoding="utf-8")


def test_report_contains_required_summary_and_verification_fields():
    text = _read(REPORT_MD)

    for expected in (
        "# PMCP Fleet Pilot",
        "Phase: `PMCPPILOT`",
        "Plan: `plans/phase-plan-v9-PMCPPILOT.md`",
        "Roadmap: `specs/phase-plans-v9.md`",
        "Evidence timestamp:",
        "Observed commit:",
        "PMCP health",
        "Server status",
        "Package/version/env metadata",
        "Pilot repository registration",
        "Readiness table",
        "PMCP-mediated query matrix",
        "Reindex/fallback evidence",
        "Safe indexed-search verdict",
        "Native-search fallback verdict",
        "Failures and limitations",
        "Verification",
    ):
        assert expected in text


def test_report_references_metadata_evidence_and_required_tool_checks():
    text = _read(REPORT_MD)

    for expected in (
        "docs/status/pmcp-pilot/startup-evidence.json",
        "docs/status/pmcp-pilot/repository-readiness-evidence.json",
        "docs/status/pmcp-pilot/query-evidence.json",
        "`get_status`",
        "`list_plugins`",
        "lexical `search_code`",
        "`symbol_lookup`",
        "semantic `search_code`",
        "Readiness fallback",
        "`reindex`",
        '`safe_fallback: "native_search"`',
    ):
        assert expected in text


def test_report_names_required_repos_and_truthful_verdicts():
    text = _read(REPORT_MD)

    for expected in (
        "Code-Index-MCP",
        "pmcp",
        "pmcp-code-mode-mcp",
        "agent-harness",
        "none are safe for indexed search through PMCP",
        "all four pilot repos must continue to use native search",
    ):
        assert expected in text


def test_report_records_blocking_startup_findings_without_secret_values():
    text = _read(REPORT_MD)

    for expected in (
        "system PMCP service",
        "command mode",
        "remote URL",
        "repositories: []",
        "unregistered_repository",
        "PMCP issue #89",
    ):
        assert expected in text

    forbidden = (
        "OPENAI_API_KEY=",
        "SUPABASE_ACCESS_TOKEN=",
        "MCP_CLIENT_SECRET=",
        "Authorization: Bearer",
    )
    for snippet in forbidden:
        assert snippet not in text


def test_guide_links_to_pilot_report_without_duplication():
    text = _read(GUIDE_MD)
    assert REPORT_LINK in text
    assert "PMCP Fleet Pilot" in text


def test_json_evidence_artifacts_exist_and_include_required_fields():
    startup_text = _read(STARTUP_JSON)
    readiness_text = _read(READINESS_JSON)
    query_text = _read(QUERY_JSON)

    for expected in (
        '"selected_startup_path": "local_override"',
        '"index-it-mcp"',
        '"configured_env_names"',
        '"metadata_only"',
    ):
        assert expected in startup_text

    for expected in (
        "Code-Index-MCP",
        "pmcp",
        "pmcp-code-mode-mcp",
        "agent-harness",
        '"server_registry_state": "empty"',
        '"safe_fallback": "native_search"',
    ):
        assert expected in readiness_text

    for expected in (
        '"tool_name": "get_status"',
        '"tool_name": "list_plugins"',
        '"tool_name": "search_code"',
        '"tool_name": "symbol_lookup"',
        '"tool_name": "reindex"',
        '"code": "index_unavailable"',
        '"code": "unregistered_repository"',
        '"verdict": "pass"',
    ):
        assert expected in query_text
