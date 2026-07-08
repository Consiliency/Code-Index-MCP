# PMCP Fleet Pilot

Phase: `PMCPPILOT`
Plan: `plans/phase-plan-v9-PMCPPILOT.md`
Roadmap: `specs/phase-plans-v9.md`
Evidence timestamp: `2026-07-08T05:24:29Z`
Observed commit: `3c2301f0f1763e8f64582da61648a06d1e7d816b`

## Summary

PMCP issue #89 is closed on the PMCP side, and live PMCP `1.19.1` gateway
checks now reach a PMCP-managed `index-it-mcp 1.2.0` runtime with four
registered pilot repos. The old `repositories: []` blocker was a registry env
bootstrap mismatch, not a remaining PMCP transport bug.

The current adoption verdict is controlled lexical PMCP pilot only:
`Code-Index-MCP` is PMCP-ready for lexical query use, while `pmcp`,
`pmcp-code-mode-mcp`, and `agent-harness` still fail closed as non-ready and
must use native search. Semantic rollout remains gated because the PMCP runtime
reports `semantic_indexer_unavailable`; do not start broad fleet indexing.

## PMCP health

- `pmcp 1.19.1` is installed.
- `pmcp status --probe --server index-it-mcp --json` reports `index-it-mcp`
  online with 8 tools from user-scoped PMCP configuration.
- PMCP-mediated `get_status` returns `status: healthy`, `version: 1.2.0`,
  `dispatcher_type: EnhancedDispatcher`, and four registered repositories.
- PMCP issue #89 is closed. PMCP's remaining note is that registration must
  use the same `MCP_INDEX_STORAGE_PATH`/`MCP_REPO_REGISTRY` as the
  PMCP-spawned server.

## Server status

- PMCP manifest candidate: `index-it-mcp`
- Startup path used for evidence: `local_override`
- PMCP-mediated `get_status`: four registered repos; one `ready`, three
  `index_empty`
- Runtime lexical feature: `available`
- Runtime semantic feature: `unavailable` with reason
  `semantic_indexer_unavailable`
- Runtime graph feature: `unavailable` with reason `graph_not_initialized`

## Package/version/env metadata

- Package version observed through PMCP: `index-it-mcp 1.2.0`
- PMCP gateway version observed locally: `pmcp 1.19.1`
- Configured env variable names for the pilot contract:
  `MCP_ALLOWED_ROOTS`, `MCP_INDEX_STORAGE_PATH`, `MCP_REPO_REGISTRY`,
  `SEMANTIC_SEARCH_ENABLED`, `SEMANTIC_DEFAULT_PROFILE`,
  `SEMANTIC_EMBEDDING_BASE_URL`, `QDRANT_URL`,
  `SEMANTIC_AUTOSTART_QDRANT`, `MCP_AUTO_INDEX`
- Evidence files:
  `docs/status/pmcp-pilot/startup-evidence.json`,
  `docs/status/pmcp-pilot/repository-readiness-evidence.json`,
  `docs/status/pmcp-pilot/query-evidence.json`

## Pilot repository registration

The PMCP-managed runtime sees all four pilot repos:

- `Code-Index-MCP`
- `pmcp`
- `pmcp-code-mode-mcp`
- `agent-harness`

Plain local CLI commands without the PMCP storage env can still read the old
default registry and report stale readiness. Treat PMCP-mediated status as the
pilot truth for this checkpoint, and run registration with the same
`MCP_INDEX_STORAGE_PATH`/`MCP_REPO_REGISTRY` that PMCP passes to the server.

## Readiness table

| Repository | PMCP readiness | Query surface | Safe fallback |
| --- | --- | --- | --- |
| `Code-Index-MCP` | `ready` | lexical indexed PMCP search allowed | not needed while ready |
| `pmcp` | `index_empty` | `index_unavailable` | `native_search` |
| `pmcp-code-mode-mcp` | `index_empty` | `index_unavailable` | `native_search` |
| `agent-harness` | `index_empty` | `index_unavailable` | `native_search` |

## PMCP-mediated query matrix

| Check | Result |
| --- | --- |
| `get_status` | pass: PMCP reached `index-it-mcp 1.2.0`, reported `healthy`, and returned four repositories |
| `list_plugins` | pass: PMCP returned 48 supported languages with availability counts `enabled=15`, `unsupported=32`, `missing_extra=1` |
| lexical `search_code` | pass: invoked against ready `Code-Index-MCP` and returned indexed results |
| `symbol_lookup` | pass: invoked against ready `Code-Index-MCP` and returned an indexed symbol result |
| semantic `search_code` | pass with limitation: `semantic=true` returns results while runtime semantic status remains `semantic_indexer_unavailable`, so do not treat semantic coverage as proven |
| Readiness fallback | pass: invoked lexical `search_code` against non-ready `pmcp` and received `index_unavailable` with `safe_fallback: "native_search"` and readiness `index_empty` |

## Reindex/fallback evidence

- The PMCP-managed runtime is online and registered; PMCP issue #89's
  `repositories: []` symptom is cleared when the configured registry is used.
- `Code-Index-MCP` can answer lexical PMCP-mediated queries at the observed
  commit.
- Non-ready repos still fail closed. `pmcp` returns `index_unavailable`,
  readiness `index_empty`, and `safe_fallback: "native_search"`.
- This checkpoint intentionally did not run `reindex` for the remaining pilot
  repos. Broad indexing has token and compute cost, so it stays behind an
  explicit operator decision.

## PMCPROLL rollout verdict

Controlled lexical PMCP pilot is allowed for PMCP-ready repos only.

- `Code-Index-MCP` may use PMCP-mediated lexical indexed search while its
  PMCP readiness remains `ready`.
- `pmcp`, `pmcp-code-mode-mcp`, and `agent-harness` must continue to use
  native search until they are intentionally indexed and PMCP reports
  readiness `ready`.
- no fleet-wide auto-start is allowed.
- no semantic rollout is allowed while runtime semantic status is
  `semantic_indexer_unavailable`.
- PMCP issue #89 dependency is cleared for PMCP's side; remaining work is
  Code-Index-MCP/operator bootstrap discipline and explicit repo indexing.
- Host `CPython 3.13` provisioning incompatibilities remain a fleet rollout
  constraint to check during operator bootstrap.

## Safe indexed-search verdict

Safe only for PMCP-ready repositories. At this checkpoint, that means
lexical PMCP-mediated indexed search for `Code-Index-MCP` only.

## Native-search fallback verdict

Native search remains mandatory for every non-ready pilot repo and any query
path returning `index_unavailable` with `safe_fallback: "native_search"`.

## Failures and limitations

- Runtime semantic status is unavailable, so semantic search and summarization
  are not accepted as fleet-ready proof.
- Three pilot repos are registered but `index_empty`; indexing them has not
  been started.
- Plain local CLI commands without PMCP's storage env can still show stale
  default-registry state. This is a bootstrap hygiene issue, not PMCP gateway
  incompatibility.

## Verification

- `uv run pytest tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov`
- `uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py tests/test_pmcp_fleet_preflight.py -q --no-cov`
- `python -m json.tool docs/status/pmcp-pilot/startup-evidence.json >/dev/null`
- `python -m json.tool docs/status/pmcp-pilot/repository-readiness-evidence.json >/dev/null`
- `python -m json.tool docs/status/pmcp-pilot/query-evidence.json >/dev/null`
- PMCP gateway checks: `get_status`, `list_plugins`, lexical `search_code`,
  `symbol_lookup`, semantic-limited `search_code`, and non-ready fallback
