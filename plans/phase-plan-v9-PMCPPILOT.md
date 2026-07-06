---
phase_loop_plan_version: 1
phase: PMCPPILOT
roadmap: specs/phase-plans-v9.md
roadmap_sha256: 9616972b5b753251691b1c95955072d37dfd9cbc90294dd1416a74b204026b3d
---
# PMCPPILOT: PMCP-Mediated Pilot

## Context

PMCPPILOT is Phase 3 of `specs/phase-plans-v9.md`. It proves that internal
agents can reach Code-Index-MCP through PMCP on a small repo set before any
broader fleet rollout.

Planning state gathered for this artifact:

- The roadmap hash was checked locally and matches
  `9616972b5b753251691b1c95955072d37dfd9cbc90294dd1416a74b204026b3d`.
- Canonical `.phase-loop/state.json` and `.phase-loop/tui-handoff.md` still
  name `PMCPBOOT` as current/planned, but the newer canonical
  `.phase-loop/events.jsonl` records `PMCPBOOT` complete with closeout commit
  `1fe9529eac6f94b85d05cdd156ad75689c0c6be4`. Live git topology is clean at
  that commit, so this plan reconciles current phase state from the newer
  ledger plus `git status --short`/HEAD and does not use legacy
  `.codex/phase-loop/` state.
- PMCP gateway catalog discovery currently shows `index-it-mcp` as a
  provisionable local manifest server, but it is not running through PMCP in
  this planning session. PMCPPILOT may therefore use either the fixed PMCP
  manifest or the temporary local override already frozen by PMCPENTRY and
  PMCPBOOT.
- `docs/guides/pmcp-fleet-integration.md` already freezes the pinned
  `index-it-mcp==1.2.0` stdio override, local semantic env names, dry-run
  semantic preflight, repository registration, readiness interpretation, and
  native-search fallback vocabulary.
- `mcp_server/cli/stdio_runner.py` exposes the pilot tool surface:
  `get_status`, `list_plugins`, `search_code`, `symbol_lookup`, and `reindex`.
  `search_code` accepts `semantic=true`, and non-ready indexed query paths
  return fail-closed readiness responses with `safe_fallback: "native_search"`.

Pilot repositories are limited to:

- `/home/viperjuice/code/Code-Index-MCP`
- `/home/viperjuice/code/pmcp`
- `/home/viperjuice/code/pmcp-code-mode-mcp`
- `/home/viperjuice/code/agent-harness`

This phase must not perform full fleet indexing, change PMCP code, edit the
PMCP shipped manifest in this repository, start background auto-indexing, or
record secret values.

## Interface Freeze Gates

- [ ] IF-0-PMCPPILOT-1 - PMCP-mediated pilot contract: metadata-only evidence
      and docs tests freeze a PMCP-invoked `index-it-mcp` pilot that records
      PMCP gateway/server health, package version, configured env variable
      names, registered pilot repos, readiness states, `get_status`,
      `list_plugins`, lexical `search_code`, `symbol_lookup`, semantic
      `search_code`, readiness fallback with `safe_fallback: "native_search"`,
      and bounded `reindex` behavior; the final status report must state which
      pilot repos are safe for indexed search and which still require native
      search.

## Lane Index & Dependencies

SL-0 — PMCP startup/provision contract and report tests
  Depends on: (none)
  Blocks: SL-2
  Parallel-safe: yes

SL-1 — Pilot repository registration/readiness evidence
  Depends on: (none)
  Blocks: SL-2
  Parallel-safe: yes

SL-2 — PMCP-mediated query evidence and docs/report reducer
  Depends on: SL-0, SL-1
  Blocks: (none)
  Parallel-safe: no

Lane DAG:

```text
SL-0 ----\
          -> SL-2 -> PMCPPILOT acceptance
SL-1 ----/
```

## Lanes

### SL-0 — PMCP Startup/Provision Contract And Report Tests

- **Scope**: Add the deterministic pilot report contract test and capture
  metadata-only PMCP gateway/startup evidence for the local override or fixed
  PMCP manifest path.
- **Owned files**: `tests/docs/test_pmcp_fleet_pilot_report.py`, `docs/status/pmcp-pilot/startup-evidence.json`
- **Interfaces provided**: `tests/docs/test_pmcp_fleet_pilot_report.py`, `docs/status/pmcp-pilot/startup-evidence.json`, `PMCPPILOT report docs contract`, `PMCP startup/provision evidence`, `PMCP manifest-or-override startup decision`
- **Interfaces consumed**: `specs/phase-plans-v9.md Phase 3 (pre-existing)`, `docs/guides/pmcp-fleet-integration.md (pre-existing)`, `mcp_server/cli/stdio_runner.py (pre-existing)`, `PMCP issue #89 (pre-existing)`, `PMCP gateway catalog/config status (pre-existing runtime surface)`
- **Parallel-safe**: yes
- **Tasks**:
  - test: Create `tests/docs/test_pmcp_fleet_pilot_report.py` with content
    assertions for `docs/status/PMCP_FLEET_PILOT.md` and the PMCP guide. The
    test should require the report title, phase-plan identity, evidence
    timestamp, observed commit, PMCP health, server status, package version,
    configured env names, pilot repo list, readiness table, query evidence
    matrix, failures/limitations, indexed-search verdict, native-search
    fallback verdict, and verification section.
  - test: Assert the report references the three metadata evidence files and
    names every required tool check: `get_status`, `list_plugins`,
    `search_code`, `symbol_lookup`, semantic `search_code`, readiness fallback,
    and `reindex`.
  - test: Assert the PMCP guide links to `docs/status/PMCP_FLEET_PILOT.md`
    without duplicating raw PMCP payloads or secret values.
  - impl: Use PMCP gateway discovery first, including
    `mcp__pmcp.gateway_catalog_search(query="index-it-mcp", include_offline=true)`
    and `mcp__pmcp.gateway_config_status()` when available, to record whether
    the fixed PMCP manifest can start `index-it-mcp` or whether the pinned
    local override was used.
  - impl: Record `docs/status/pmcp-pilot/startup-evidence.json` with only
    non-secret fields: capture timestamp, PMCP candidate name, source,
    transport, provisionable/running booleans, selected startup path
    (`fixed_manifest` or `local_override`), package/version observation,
    server instructions/tool names if obtained, configured env variable names,
    and redaction status. Do not store env values except the non-secret local
    endpoints already documented by PMCPBOOT.
  - verify: `uv run python -m py_compile tests/docs/test_pmcp_fleet_pilot_report.py`
  - verify: `python -m json.tool docs/status/pmcp-pilot/startup-evidence.json >/dev/null`

### SL-1 — Pilot Repository Registration/Readiness Evidence

- **Scope**: Register the bounded pilot repo set through the documented
  bootstrap path and record readiness/fallback state before query evidence is
  reduced into the status report.
- **Owned files**: `docs/status/pmcp-pilot/repository-readiness-evidence.json`
- **Interfaces provided**: `docs/status/pmcp-pilot/repository-readiness-evidence.json`, `pilot repo registration evidence`, `pilot readiness matrix`, `native-search fallback candidates`
- **Interfaces consumed**: `IF-0-PMCPBOOT-1 (pre-existing)`, `IF-0-PMCPBOOT-2 (pre-existing)`, `docs/guides/pmcp-fleet-integration.md (pre-existing)`, `mcp-index repository register (pre-existing)`, `mcp-index repository list -v (pre-existing)`, `mcp-index repository status (pre-existing)`, `mcp-index artifact workspace-status (pre-existing)`
- **Parallel-safe**: yes
- **Tasks**:
  - test: Confirm each configured pilot path exists before registration, and
    record any missing path as a pilot failure row rather than silently
    dropping it from evidence.
  - impl: Run the PMCPBOOT bootstrap sequence for only the four pilot repos:
    `mcp-index repository register`, `mcp-index repository list -v`,
    `mcp-index repository status`, and `mcp-index artifact workspace-status`.
  - impl: Preserve one pre-remediation non-ready observation when available so
    SL-2 can prove the PMCP-mediated readiness fallback path before any bounded
    `reindex` remediation changes state.
  - impl: Record `docs/status/pmcp-pilot/repository-readiness-evidence.json`
    with repo path, git common-directory identity, registered/default branch,
    current commit, readiness status, semantic readiness, recommended
    remediation, and safe fallback classification. Redact user/private data
    beyond repo paths already named by the roadmap.
  - impl: Do not register repos outside the pilot set and do not trigger broad
    fleet indexing or background auto-start policy.
  - verify: `python -m json.tool docs/status/pmcp-pilot/repository-readiness-evidence.json >/dev/null`
  - verify: `rg -n "Code-Index-MCP|pmcp|pmcp-code-mode-mcp|agent-harness|ready|index_unavailable|native_search" docs/status/pmcp-pilot/repository-readiness-evidence.json`

### SL-2 — PMCP-Mediated Query Evidence And Docs/Report Reducer

- **Scope**: Invoke the required Code-Index-MCP tools through PMCP, record the
  query/reindex evidence, and reduce all pilot evidence into the status report
  and guide pointer.
- **Owned files**: `docs/status/pmcp-pilot/query-evidence.json`, `docs/status/PMCP_FLEET_PILOT.md`, `docs/guides/pmcp-fleet-integration.md`
- **Interfaces provided**: `IF-0-PMCPPILOT-1`, `PMCP_FLEET_PILOT status artifact`, `pilot indexed-search verdict`, `pilot native-search fallback verdict`
- **Interfaces consumed**: `tests/docs/test_pmcp_fleet_pilot_report.py`, `docs/status/pmcp-pilot/startup-evidence.json`, `docs/status/pmcp-pilot/repository-readiness-evidence.json`, `mcp_server/cli/stdio_runner.py tool surface (pre-existing)`, `PMCP-mediated downstream invocation results (pre-existing runtime surface)`
- **Parallel-safe**: no
- **Tasks**:
  - test: Run `uv run pytest tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov` before report reduction and confirm it fails only on missing or
    incomplete PMCPPILOT report/guide content.
  - impl: Through PMCP, invoke the downstream Code-Index-MCP tools for the
    bounded pilot set or the subset that reaches `ready`: `get_status`,
    `list_plugins`, lexical `search_code`, `symbol_lookup`, semantic
    `search_code` with `semantic=true`, readiness fallback on a non-ready repo
    observation, and a bounded `reindex` remediation on one selected pilot repo
    when safe.
  - impl: Record `docs/status/pmcp-pilot/query-evidence.json` with tool name,
    repository, request shape, redacted response summary, readiness state,
    fallback/remediation when present, pass/fail verdict, and timestamp. Do not
    store full raw payloads that may contain private source excerpts; keep
    snippets short and metadata-oriented.
  - impl: Write `docs/status/PMCP_FLEET_PILOT.md` with sections for summary,
    PMCP startup path, package/version/env metadata, pilot repo registration,
    readiness table, PMCP-mediated query matrix, reindex/fallback evidence,
    safe indexed-search verdict, native-search fallback verdict, failures and
    remaining PMCP issue #89 dependencies, and verification.
  - impl: Update `docs/guides/pmcp-fleet-integration.md` with a concise link to
    the pilot report and no duplicated raw evidence.
  - impl: If PMCP cannot connect to a working stdio server, still write the
    status report with the startup failure evidence and mark IF-0-PMCPPILOT-1
    incomplete; the closeout should then block with a repairable contract
    blocker rather than inventing live PMCP query proof.
  - verify: `uv run pytest tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov`
  - verify: `python -m json.tool docs/status/pmcp-pilot/query-evidence.json >/dev/null`
  - verify: `git diff --check -- docs/status/PMCP_FLEET_PILOT.md docs/status/pmcp-pilot/startup-evidence.json docs/status/pmcp-pilot/repository-readiness-evidence.json docs/status/pmcp-pilot/query-evidence.json docs/guides/pmcp-fleet-integration.md tests/docs/test_pmcp_fleet_pilot_report.py`

## Execution Notes

- SL-0 and SL-1 are writer-disjoint and may run in parallel only when the
  scheduler assigns separate worktrees. SL-2 is a reducer and must run after
  both evidence producers complete.
- The temporary local override is an acceptable PMCPPILOT startup path if PMCP
  issue #89 is still unresolved; the report must state whether execution used
  the fixed manifest or the temporary override.
- No lane owns PMCP gateway source code, PMCP shipped manifests,
  `pmcp-code-mode-mcp` source, full fleet registration, broad background
  indexing, release metadata, or secret-bearing config.
- Docs-sweep decision: no doc change is needed for README, CHANGELOG, or
  release notes in this phase. PMCPENTRY already added the README pointer,
  PMCPPILOT's public operator surface is the guide pointer plus
  `docs/status/PMCP_FLEET_PILOT.md`, and this is not a release/package phase.
- No `## Execution Policy` or `## Dispatch Hints` override is needed. The
  runner should use CLI/operator policy first, then roadmap policy if present,
  then registry defaults, with no silent downgrade.
- Operational evidence must remain metadata-only. It may name env variables,
  documented non-secret endpoint URLs, repo paths from the roadmap, package
  versions, status codes, and readiness values. It must not record tokens, API
  keys, private key material, credential payloads, local secret env values, or
  long source excerpts from private repos.

## Verification

Effective automation suite command:
`uv run pytest tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov`

Whole-phase verification commands:

```bash
phase-loop validate-roadmap specs/phase-plans-v9.md
uv run pytest tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov
uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py tests/test_pmcp_fleet_preflight.py -q --no-cov
python -m json.tool docs/status/pmcp-pilot/startup-evidence.json >/dev/null
python -m json.tool docs/status/pmcp-pilot/repository-readiness-evidence.json >/dev/null
python -m json.tool docs/status/pmcp-pilot/query-evidence.json >/dev/null
git diff --check -- docs/status/PMCP_FLEET_PILOT.md docs/status/pmcp-pilot/startup-evidence.json docs/status/pmcp-pilot/repository-readiness-evidence.json docs/status/pmcp-pilot/query-evidence.json docs/guides/pmcp-fleet-integration.md tests/docs/test_pmcp_fleet_pilot_report.py
rg -n "PMCPPILOT|PMCP_FLEET_PILOT|get_status|list_plugins|search_code|symbol_lookup|semantic=true|reindex|native_search|safe_fallback|Code-Index-MCP|pmcp-code-mode-mcp|agent-harness" docs/status/PMCP_FLEET_PILOT.md docs/status/pmcp-pilot/*.json docs/guides/pmcp-fleet-integration.md tests/docs/test_pmcp_fleet_pilot_report.py
```

Acceptance evidence expected from execution:

- The pilot docs test passes and proves the status report records all required
  PMCP startup, repo registration, readiness, query, fallback, reindex,
  failure, and verdict fields.
- All three JSON evidence artifacts are valid JSON and contain only
  metadata-oriented, non-secret fields.
- The final report states which pilot repos are safe for indexed search and
  which must still use native search.
- If PMCP-mediated calls cannot reach a working stdio server, the report
  records the failure and the closeout blocks instead of claiming IF gate
  production.
- `git diff --check` passes for every phase-owned file.

## Acceptance Criteria

- [ ] PMCP starts `index-it-mcp` through either the fixed PMCP manifest or the
      pinned local override from `docs/guides/pmcp-fleet-integration.md`.
- [ ] The four pilot repos are evaluated with one registered worktree per git
      common directory or recorded as explicit failures if absent, as proven by
      `docs/status/pmcp-pilot/repository-readiness-evidence.json` and
      `uv run pytest tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov`.
- [ ] PMCP-mediated calls prove `get_status`, `list_plugins`, lexical
      `search_code`, `symbol_lookup`, semantic `search_code`, readiness
      fallback, and bounded `reindex` behavior.
- [ ] `docs/status/PMCP_FLEET_PILOT.md` records PMCP health, server status,
      package version, configured env names, registered repos, readiness
      states, query evidence, reindex/fallback evidence, and failures without
      secret values.
- [ ] The pilot report states which repos are safe for indexed search and which
      must still use native search, as proven by
      `uv run pytest tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov`
      and the final `rg` verification command.
- [ ] `tests/docs/test_pmcp_fleet_pilot_report.py` plus JSON validation and
      `git diff --check` verify the report, evidence artifacts, and guide
      pointer.

## Spec Closeout Plan

- schema: `spec_delta_closeout.v1`
- decision: `no_spec_delta`
- target surfaces: `docs/status/PMCP_FLEET_PILOT.md`, `docs/status/pmcp-pilot/startup-evidence.json`, `docs/status/pmcp-pilot/repository-readiness-evidence.json`, `docs/status/pmcp-pilot/query-evidence.json`, `docs/guides/pmcp-fleet-integration.md`, `tests/docs/test_pmcp_fleet_pilot_report.py`
- evidence paths: `docs/status/PMCP_FLEET_PILOT.md`, `docs/status/pmcp-pilot/startup-evidence.json`, `docs/status/pmcp-pilot/repository-readiness-evidence.json`, `docs/status/pmcp-pilot/query-evidence.json`, focused test output
- redaction posture: `metadata_only`
- downstream handling: `none`
