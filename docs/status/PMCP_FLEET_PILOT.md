# PMCP Fleet Pilot

Phase: `PMCPPILOT`
Plan: `plans/phase-plan-v9-PMCPPILOT.md`
Roadmap: `specs/phase-plans-v9.md`
Evidence timestamp: `2026-07-06T10:01:48Z`
Observed commit: `1fe9529eac6f94b85d05cdd156ad75689c0c6be4`

## Summary

This pilot stayed blocked. PMCP catalog discovery still exposes
`index-it-mcp`, but PMCP could not start a working downstream stdio server for
tool invocations on the bounded repo set.

## PMCP health

- The active PMCP system service is reachable on the local HTTP transport.
- `pmcp config status` still sees a repo-local `code-index-mcp` project entry.
- `pmcp doctor` reports that the repo-local command-mode entry conflicts with
  the active system PMCP service and should use the remote PMCP gateway URL
  instead of command mode while the service stays active.

## Server status

- PMCP manifest candidate: `index-it-mcp`
- Startup path used for evidence: `fixed_manifest`
- `gateway_connect_server("index-it-mcp")`: failed with disconnected startup
- `gateway_provision("index-it-mcp")`: failed on host `CPython 3.13` because
  `tree-sitter-languages` currently has no `cp313` wheels for the resolved
  package set

## Package/version/env metadata

- Package version observed for this repository: `index-it-mcp 1.2.0`
- Configured env variable names for the pilot contract:
  `MCP_ALLOWED_ROOTS`, `MCP_INDEX_STORAGE_PATH`, `SEMANTIC_SEARCH_ENABLED`,
  `SEMANTIC_DEFAULT_PROFILE`, `SEMANTIC_EMBEDDING_BASE_URL`, `QDRANT_URL`,
  `SEMANTIC_AUTOSTART_QDRANT`, `MCP_AUTO_INDEX`
- Evidence files:
  `docs/status/pmcp-pilot/startup-evidence.json`,
  `docs/status/pmcp-pilot/repository-readiness-evidence.json`,
  `docs/status/pmcp-pilot/query-evidence.json`

## Pilot repository registration

All four pilot repos exist locally and were registered through the bounded
bootstrap path:

- `Code-Index-MCP`
- `pmcp`
- `pmcp-code-mode-mcp`
- `agent-harness`

## Readiness table

| Repository | Registration readiness | Workspace readiness | Safe fallback |
| --- | --- | --- | --- |
| `Code-Index-MCP` | `wrong_branch` | `wrong_branch` | `native_search` |
| `pmcp` | `missing_index` | `index_empty` | `native_search` |
| `pmcp-code-mode-mcp` | `missing_index` | `index_empty` | `native_search` |
| `agent-harness` | `missing_index` | `index_empty` | `native_search` |

## PMCP-mediated query matrix

| Check | Result |
| --- | --- |
| `get_status` | blocked before invocation because PMCP could not start a working server |
| `list_plugins` | blocked before invocation because PMCP could not start a working server |
| lexical `search_code` | blocked before invocation because PMCP could not start a working server |
| `symbol_lookup` | blocked before invocation because PMCP could not start a working server |
| semantic `search_code` | blocked before invocation because PMCP could not start a working server |

## Reindex/fallback evidence

- `gateway_catalog_search` still identifies `index-it-mcp` as the intended
  PMCP server.
- Readiness fallback remains truthful: every registered pilot repo currently
  resolves to `index_unavailable` and requires
  `safe_fallback: "native_search"`.
- This blocked report still records the required readiness fallback vocabulary
  even though PMCP never reached the point of issuing downstream query calls.
- Bounded `reindex` behavior through PMCP was not attempted because no working
  PMCP-managed stdio server was available to carry the request.

## Safe indexed-search verdict

At this checkpoint, none are safe for indexed search through PMCP.

## Native-search fallback verdict

At this checkpoint, all four pilot repos must continue to use native search
until PMCP can start a working downstream stdio server and a bounded reindex or
artifact hydration makes at least one pilot repository `ready`.

## Failures and limitations

- The active system PMCP service and the repo-local command-mode project entry
  are currently incompatible; PMCP doctor points to the remote URL path as the
  service-compatible configuration.
- Manifest provisioning for `index-it-mcp` is blocked on host `CPython 3.13`
  because `tree-sitter-languages` does not publish the required wheel set.
- PMCP issue #89 remains the upstream dependency for truthful PMCP startup and
  rollout proof.

## Verification

- `uv run pytest tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov`
- `uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py tests/test_pmcp_fleet_preflight.py -q --no-cov`
- `python -m json.tool docs/status/pmcp-pilot/startup-evidence.json >/dev/null`
- `python -m json.tool docs/status/pmcp-pilot/repository-readiness-evidence.json >/dev/null`
- `python -m json.tool docs/status/pmcp-pilot/query-evidence.json >/dev/null`
