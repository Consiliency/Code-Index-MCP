# PMCP Fleet Pilot

Phase: `PMCPPILOT`
Plan: `plans/phase-plan-v9-PMCPPILOT.md`
Roadmap: `specs/phase-plans-v9.md`
Evidence timestamp: `2026-07-06T10:44:59Z`
Observed commit: `2d8c241a5f88fc185b33152681b31f482b0f525f`

## Summary

This pilot reached a working PMCP-managed `index-it-mcp` server and exercised
the required tool surface, but the PMCP-managed runtime still reports
`repositories: []`. The bounded local bootstrap registered all four pilot
repos for the CLI surfaces, yet PMCP-mediated query calls still fail closed
with `index_unavailable` and `safe_fallback: "native_search"` because the
downstream runtime sees each repo as `unregistered_repository`.

## PMCP health

- The active system PMCP service is reachable on the local HTTP transport.
- `pmcp config status` still sees a repo-local `code-index-mcp` project entry.
- `pmcp doctor` reports that the repo-local command-mode entry conflicts with
  the active system PMCP service and should use the remote PMCP gateway URL
  instead of command mode while the service stays active.
- A separate user-level `index-it-mcp` PMCP entry is online and answered the
  pilot tool invocations.

## Server status

- PMCP manifest candidate: `index-it-mcp`
- Startup path used for evidence: `local_override`
- `pmcp status --json --server index-it-mcp --probe`: reports
  `index-it-mcp` online with 8 tools
- PMCP-mediated `get_status`: returns `version: 1.2.0`, `status: unknown`, and
  `repositories: []`

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
| `pmcp` | `index_empty` | `index_empty` | `native_search` |
| `pmcp-code-mode-mcp` | `index_empty` | `index_empty` | `native_search` |
| `agent-harness` | `index_empty` | `index_empty` | `native_search` |

## PMCP-mediated query matrix

| Check | Result |
| --- | --- |
| `get_status` | pass: PMCP reached `index-it-mcp 1.2.0` and reported `repositories: []` |
| `list_plugins` | pass: PMCP returned the loaded tool surface and availability counts |
| lexical `search_code` | pass: invoked against `Code-Index-MCP` and failed closed with `index_unavailable` / `unregistered_repository` |
| `symbol_lookup` | pass: invoked against `Code-Index-MCP` and failed closed with `index_unavailable` / `unregistered_repository` |
| semantic `search_code` | pass: invoked against `Code-Index-MCP` with `semantic=true` and failed closed with `index_unavailable` / `unregistered_repository` |

## Reindex/fallback evidence

- `pmcp status`, PMCP gateway health, and PMCP-mediated `get_status` agree
  that `index-it-mcp` is online for this pilot.
- The bounded `mcp-index repository register`, `repository list -v`, and
  `artifact workspace-status` checks still leave `Code-Index-MCP` on
  `wrong_branch` and the other three pilot repos on `index_empty`.
- PMCP-managed query calls still do not see the bounded local bootstrap. The
  live runtime returns `repositories: []`, so each query target resolves to
  `unregistered_repository`.
- Readiness fallback remains truthful: every registered pilot repo currently
  requires `safe_fallback: "native_search"` through PMCP.
- Bounded `reindex` behavior was invoked against `pmcp` through PMCP and
  returned `mutation_performed: false` with `code: "unregistered_repository"`.

## PMCPROLL rollout verdict

PMCP-blocked for indexed fleet adoption at this checkpoint.

- All four pilot repos must continue to use native search.
- no fleet-wide auto-start is allowed.
- no semantic rollout until PMCP issue #89 clears runtime registry alignment
  and at least one pilot repo reaches readiness `ready` through the
  PMCP-managed runtime.
- The blocking runtime evidence remains `repositories: []` on the PMCP-managed
  server and `unregistered_repository` on PMCP-mediated query calls.
- The active system PMCP service versus repo-local command mode conflict
  remains an operator-visible rollout hazard and must stay in the guide's
  troubleshooting matrix.
- Host `CPython 3.13` provisioning incompatibilities remain a fleet rollout
  constraint to check during operator bootstrap, even though this reducer did
  not need a new provisioning attempt to confirm the PMCP-blocked verdict.

## Safe indexed-search verdict

At this checkpoint, none are safe for indexed search through PMCP.

## Native-search fallback verdict

At this checkpoint, all four pilot repos must continue to use native search
until PMCP aligns its managed runtime with the intended repository registry and
at least one pilot repository becomes `ready`.

## Failures and limitations

- The active system PMCP service and the repo-local command-mode project entry
  are currently incompatible; PMCP doctor points to the remote URL path as the
  service-compatible configuration.
- The PMCP-managed `index-it-mcp` runtime is online but still reports
  `repositories: []`, so the bounded local bootstrap does not yet provide a
  usable PMCP query registry.
- PMCP issue #89 remains the upstream dependency for truthful PMCP bootstrap
  and rollout proof.

## Verification

- `uv run pytest tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov`
- `uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py tests/test_pmcp_fleet_preflight.py -q --no-cov`
- `python -m json.tool docs/status/pmcp-pilot/startup-evidence.json >/dev/null`
- `python -m json.tool docs/status/pmcp-pilot/repository-readiness-evidence.json >/dev/null`
- `python -m json.tool docs/status/pmcp-pilot/query-evidence.json >/dev/null`
