---
phase_loop_plan_version: 1
phase: PMCPROLL
roadmap: specs/phase-plans-v9.md
roadmap_sha256: d19ac240664663c67ece7b01e262d5e28e6ae33e2fef292e45db989538b2b48e
---
# PMCPROLL: Fleet Rollout Policy And Adoption Guide

## Context

PMCPROLL is Phase 4 of `specs/phase-plans-v9.md`. It converts the completed
PMCPPILOT evidence into an operator-facing rollout policy and a truthful
adoption verdict for PMCP-mediated Code-Index-MCP use across the internal fleet.

Planning state gathered for this artifact:

- The roadmap hash was checked locally and matches
  `d19ac240664663c67ece7b01e262d5e28e6ae33e2fef292e45db989538b2b48e`.
- Canonical `.phase-loop/state.json`, `.phase-loop/tui-handoff.md`, and the
  newer `.phase-loop/events.jsonl` agree that `PMCPPILOT` was manually
  repaired, verified, committed at `eef24ce64d7ce5bb530ba9c0e4b47800bbf91b9b`,
  and that the current phase is `PMCPROLL`. Live git topology is clean on
  `main` at that commit, so this plan treats `.phase-loop/` as authoritative
  and does not use legacy `.codex/phase-loop/` state.
- PMCP is exposed in this planning session. `gateway_health` reports the PMCP
  gateway and a prior successful `index-it-mcp` audit trail, while
  `gateway_describe` confirms that `search_code` and `symbol_lookup` accept an
  optional `repository` argument and must fail closed with `index_unavailable`
  plus `safe_fallback: native_search` until repository readiness is `ready`.
- `docs/status/PMCP_FLEET_PILOT.md` is the active pilot evidence. It records a
  working PMCP-managed `index-it-mcp 1.2.0` server with 8 tools, but the
  PMCP-managed runtime reports `repositories: []`. The bounded local bootstrap
  registered all four pilot repos for local CLI status surfaces, yet PMCP
  query calls still resolve to `unregistered_repository` and preserve
  `native_search` fallback.
- The pilot verdict is not broad indexed-search readiness. At this checkpoint,
  no pilot repository is safe for indexed search through PMCP; all four pilot
  repositories must continue to use native search until PMCP aligns its managed
  runtime with the intended repository registry and at least one pilot repo
  reaches `ready`.
- The existing PMCP guide already freezes the pinned local override, semantic
  preflight, repo bootstrap, readiness vocabulary, and current runtime registry
  caveat. PMCPROLL should extend that guide into fleet policy instead of
  creating a second operator guide unless execution discovers a real doc
  ownership gap.

This phase must not start full fleet indexing, edit PMCP code, hide
repo-registration automation behind PMCP, dispatch a release, or record secret
values.

## Interface Freeze Gates

- [ ] IF-0-PMCPROLL-1 - Fleet rollout contract: repo docs, docs tests, and the
      pilot status report freeze a PMCP fleet rollout policy that defines
      staged rollout groups, keeps lazy PMCP startup with no fleet-wide
      auto-start as the default, requires native-search fallback whenever
      readiness is not `ready`, states when semantic indexing may be enabled
      versus when lexical-only use is acceptable, covers the full
      troubleshooting matrix from the roadmap, and records a final adoption
      verdict that either names the remaining PMCP issue #89 dependencies or
      states that the PMCP dependency is cleared.

## Lane Index & Dependencies

SL-0 — Rollout contract tests
  Depends on: (none)
  Blocks: SL-1, SL-2
  Parallel-safe: no

SL-1 — Fleet rollout policy guide and README reducer
  Depends on: SL-0
  Blocks: SL-2
  Parallel-safe: no

SL-2 — Pilot evidence docs and spec-closeout reducer
  Depends on: SL-0, SL-1
  Blocks: (none)
  Parallel-safe: no

Lane DAG:

```text
SL-0 -> SL-1 -> SL-2 -> PMCPROLL acceptance
```

## Lanes

### SL-0 - Rollout Contract Tests

- **Scope**: Extend the existing PMCP policy tests so the rollout policy,
  troubleshooting matrix, README pointer, pilot verdict, and no-secret posture
  are machine-checked before docs are reduced.
- **Owned files**: `tests/docs/test_pmcp_fleet_integration_docs.py`, `tests/docs/test_pmcp_fleet_pilot_report.py`
- **Interfaces provided**: `PMCPROLL docs test contract`, `PMCPROLL pilot verdict test contract`
- **Interfaces consumed**: `specs/phase-plans-v9.md Phase 4 (pre-existing)`,
  `docs/guides/pmcp-fleet-integration.md (pre-existing)`,
  `docs/status/PMCP_FLEET_PILOT.md (pre-existing)`,
  `docs/status/pmcp-pilot/startup-evidence.json (pre-existing)`,
  `docs/status/pmcp-pilot/repository-readiness-evidence.json (pre-existing)`,
  `docs/status/pmcp-pilot/query-evidence.json (pre-existing)`
- **Parallel-safe**: no
- **Tasks**:
  - test: Extend `tests/docs/test_pmcp_fleet_integration_docs.py` with content
    assertions for `docs/guides/pmcp-fleet-integration.md` that require a
    `Fleet Rollout Policy` section, rollout group definitions for pilot repos,
    core engineering repos, long-tail repos, opt-out repos, and repos requiring
    manual constraints.
  - test: Assert the guide freezes the default policy: lazy PMCP startup,
    no fleet-wide auto-start, no hidden auto-registration, and native-search
    fallback whenever readiness is not `ready`.
  - test: Assert the guide defines semantic enablement criteria and
    lexical-only criteria, including Qdrant readiness, local embedding endpoint
    readiness, `oss_high` profile compatibility, PMCP runtime registry
    alignment, repository readiness, and when lexical-only PMCP use is still
    acceptable.
  - test: Assert troubleshooting coverage for wrong transport, wrong package
    version, missing allowed roots, missing Qdrant, missing embedding endpoint,
    wrong branch, stale commit, missing index, `path_outside_allowed_roots`,
    active system PMCP service versus repo-local command-mode conflicts,
    PMCP-managed server registry isolation after local CLI bootstrap, and host
    `CPython 3.13` package/provisioning incompatibilities.
  - test: Extend `tests/docs/test_pmcp_fleet_pilot_report.py` so the pilot
    report must include a PMCPROLL rollout verdict naming the current
    PMCP-blocked/native-search posture, PMCP issue #89, `repositories: []`,
    `unregistered_repository`, and the absence of any repo safe for indexed
    PMCP search.
  - test: Assert README points to the guide and pilot report without
    duplicating raw override JSON, PMCP payloads, or secret values.
  - impl: Keep the tests deterministic and content-based; do not call PMCP or
    start indexing from tests.
  - verify: `uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov`

### SL-1 - Fleet Rollout Policy Guide And README Reducer

- **Scope**: Reduce the roadmap policy and pilot evidence into the PMCP fleet
  guide and README pointer without changing runtime behavior.
- **Owned files**: `docs/guides/pmcp-fleet-integration.md`, `README.md`
- **Interfaces provided**: `PMCPROLL rollout policy guide`, `README PMCP rollout pointer`
- **Interfaces consumed**: `PMCPROLL docs test contract`,
  `docs/status/PMCP_FLEET_PILOT.md (pre-existing)`,
  `IF-0-PMCPENTRY-1 (pre-existing)`, `IF-0-PMCPENTRY-2 (pre-existing)`,
  `IF-0-PMCPBOOT-1 (pre-existing)`, `IF-0-PMCPBOOT-2 (pre-existing)`,
  `IF-0-PMCPPILOT-1 (pre-existing)`
- **Parallel-safe**: no
- **Tasks**:
  - test: Run the SL-0 docs tests before editing and confirm they fail only on
    missing or incomplete PMCPROLL rollout content.
  - impl: Add a `Fleet Rollout Policy` section to
    `docs/guides/pmcp-fleet-integration.md` with staged groups: pilot repos,
    core engineering repos, long-tail repos, opt-out repos, and repos requiring
    manual constraints.
  - impl: Add the default policy: PMCP remains lazy-started, fleet-wide
    auto-start is not allowed, broad auto-registration is not allowed, and
    agents must use native search whenever PMCP-mediated Code-Index-MCP
    readiness is not `ready` or returns `index_unavailable` with
    `safe_fallback: "native_search"`.
  - impl: Add semantic versus lexical policy. Semantic indexing is enabled only
    when Qdrant, the local embedding endpoint, the `oss_high` profile, PMCP
    env propagation, PMCP runtime registry alignment, and repository readiness
    are all proven. Lexical-only PMCP use is acceptable only after lexical
    readiness is `ready` and semantic readiness is the remaining non-critical
    blocker; otherwise use native search.
  - impl: Add a troubleshooting matrix covering every roadmap-required case:
    wrong transport, wrong package version, missing allowed roots, missing
    Qdrant, missing embedding endpoint, wrong branch, stale commit, missing
    index, `path_outside_allowed_roots`, active system PMCP service versus
    repo-local command-mode conflicts, PMCP-managed server registry isolation
    after local CLI bootstrap, and host `CPython 3.13` provisioning
    incompatibilities.
  - impl: Add a concise README update that points to the PMCP guide and pilot
    report and states the current adoption posture without duplicating the
    detailed override JSON or raw evidence.
  - impl: Do not change runtime code, PMCP manifests, package metadata,
    release notes, or the existing evidence JSON files in this lane.
  - verify: `uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py -q --no-cov`
  - verify: `git diff --check -- docs/guides/pmcp-fleet-integration.md README.md tests/docs/test_pmcp_fleet_integration_docs.py`

### SL-2 - Pilot Evidence Docs And Spec-Closeout Reducer

- **Scope**: Back-fill the completed pilot report with the final PMCPROLL
  adoption verdict and perform the roadmap/spec closeout check for any new
  blockers discovered during rollout reduction.
- **Owned files**: `docs/status/PMCP_FLEET_PILOT.md`, `specs/phase-plans-v9.md`
- **Interfaces provided**: `IF-0-PMCPROLL-1`, `PMCPROLL final rollout verdict`,
  `PMCPROLL spec closeout decision`
- **Interfaces consumed**: `PMCPROLL docs test contract`,
  `PMCPROLL rollout policy guide`, `docs/status/pmcp-pilot/startup-evidence.json`,
  `docs/status/pmcp-pilot/repository-readiness-evidence.json`,
  `docs/status/pmcp-pilot/query-evidence.json`,
  `PMCP gateway health/describe planning observations`, `PMCP issue #89`
- **Parallel-safe**: no
- **Tasks**:
  - test: Run the SL-0 pilot-report assertions before editing and confirm they
    fail only on the missing PMCPROLL rollout verdict section.
  - impl: Add a `PMCPROLL rollout verdict` section to
    `docs/status/PMCP_FLEET_PILOT.md` that states the final adoption posture.
    With the current pilot evidence, the expected truthful verdict is
    PMCP-blocked for indexed fleet adoption, native-search fallback for all
    pilot repos, no fleet-wide auto-start, and no semantic rollout until PMCP
    issue #89 clears runtime registry alignment and at least one pilot repo is
    `ready`.
  - impl: Record the concrete pilot-derived blockers: active system PMCP
    service versus repo-local command-mode conflict, PMCP-managed runtime
    `repositories: []`, `unregistered_repository` query responses, native-search
    fallback preservation, and any host `CPython 3.13` provisioning
    incompatibility observed during execution.
  - impl: Inspect `specs/phase-plans-v9.md` before editing. If the only
    rollout blockers are the PMCPPILOT findings already carried into the
    current PMCPROLL scope, leave the roadmap content unchanged and record that
    the prior PMCPPILOT roadmap amendment already covered them. If execution
    discovers a new rollout blocker not covered by the roadmap, amend
    `specs/phase-plans-v9.md` in the nearest safe downstream location; because
    PMCPROLL is the final v9 phase, prefer a concise follow-up note under the
    PMCPROLL spec closeout policy rather than inventing a broad new phase.
  - impl: Keep the verdict metadata-only. Do not copy secret values, raw PMCP
    payloads, long source excerpts, credentials, or private environment values
    into docs.
  - verify: `uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov`
  - verify: `phase-loop validate-roadmap specs/phase-plans-v9.md`
  - verify: `git diff --check -- docs/status/PMCP_FLEET_PILOT.md specs/phase-plans-v9.md tests/docs/test_pmcp_fleet_pilot_report.py`

## Execution Notes

- Run lanes serially. SL-0 writes the docs test contract, SL-1 reduces the
  operator guide and README pointer, and SL-2 reduces the pilot verdict plus
  spec closeout. All writable lane ownership is disjoint.
- No lane owns PMCP gateway source, PMCP shipped manifests, `pmcp-code-mode-mcp`
  source, runtime indexing code, package/dependency metadata, release dispatch,
  full fleet registration, broad auto-start, or secret-bearing config.
- This plan intentionally keeps the final adoption verdict honest. If execution
  observes the same PMCP runtime registry gap recorded by PMCPPILOT, the
  verdict is PMCP-blocked/native-search, not broad PMCP indexed-search
  readiness.
- Docs-sweep decision: README is owned by SL-1 and consumed by the terminal
  reducer; CHANGELOG and release notes are `no_doc_delta` / no doc change
  because PMCPROLL is an adoption-policy reducer, not a release, package, or
  public API mutation phase.
- No `## Execution Policy` or `## Dispatch Hints` override is needed. The
  runner should use CLI/operator policy first, then roadmap policy if present,
  then registry defaults, with no silent downgrade.
- Operational evidence must remain metadata-only. It may name env variables,
  documented non-secret endpoint URLs, repo paths from the roadmap, package
  versions, status codes, readiness values, PMCP issue #89, and blocker class
  names. It must not record tokens, API keys, private keys, credential payloads,
  local secret env values, or long source excerpts from private repos.

## Verification

Effective automation suite command:
`uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov`

Whole-phase verification commands:

```bash
phase-loop validate-roadmap specs/phase-plans-v9.md
uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov
uv run pytest tests/test_pmcp_fleet_preflight.py tests/test_repository_commands.py -q --no-cov -k "setup_semantic or status_reports_rollout_and_query_surfaces"
python -m json.tool docs/status/pmcp-pilot/startup-evidence.json >/dev/null
python -m json.tool docs/status/pmcp-pilot/repository-readiness-evidence.json >/dev/null
python -m json.tool docs/status/pmcp-pilot/query-evidence.json >/dev/null
git diff --check -- docs/guides/pmcp-fleet-integration.md README.md docs/status/PMCP_FLEET_PILOT.md specs/phase-plans-v9.md tests/docs/test_pmcp_fleet_integration_docs.py tests/docs/test_pmcp_fleet_pilot_report.py
rg -n "Fleet Rollout Policy|pilot repos|core engineering repos|long-tail repos|opt-out repos|manual constraints|lazy PMCP startup|native_search|semantic indexing|lexical-only|path_outside_allowed_roots|CPython 3.13|PMCPROLL rollout verdict|PMCP issue #89|unregistered_repository" docs/guides/pmcp-fleet-integration.md README.md docs/status/PMCP_FLEET_PILOT.md tests/docs/test_pmcp_fleet_integration_docs.py tests/docs/test_pmcp_fleet_pilot_report.py
```

Acceptance evidence expected from execution:

- Focused docs tests pass and prove the rollout groups, default policy,
  semantic/lexical criteria, troubleshooting matrix, README pointer, pilot
  verdict, and no-secret posture are present.
- `phase-loop validate-roadmap specs/phase-plans-v9.md` passes after any
  necessary spec closeout amendment.
- The evidence JSON files remain valid JSON and are not rewritten with secret
  values.
- `git diff --check` passes for every phase-owned file.
- The final diff touches only PMCPROLL-owned docs/tests/spec files for phase
  implementation work, plus phase-loop plan/manifest/handoff artifacts from
  planning.

## Acceptance Criteria

- [ ] `docs/guides/pmcp-fleet-integration.md` defines staged rollout groups:
      pilot repos, core engineering repos, long-tail repos, opt-out repos, and
      repos requiring manual constraints.
- [ ] The guide states the default rollout policy: lazy PMCP startup, no
      fleet-wide auto-start, no hidden fleet registration, and native-search
      fallback whenever repository readiness is not `ready`.
- [ ] The guide defines semantic enablement criteria and lexical-only criteria
      using Qdrant readiness, embedding endpoint readiness, `oss_high` profile
      compatibility, PMCP env propagation, PMCP-managed registry alignment, and
      repository readiness.
- [ ] Troubleshooting covers wrong transport, wrong package version, missing
      allowed roots, missing Qdrant, missing embedding endpoint, wrong branch,
      stale commit, missing index, `path_outside_allowed_roots`, active system
      PMCP service versus repo-local command-mode conflicts, PMCP-managed
      server registry isolation after local CLI bootstrap, and host
      `CPython 3.13` provisioning incompatibilities.
- [ ] `docs/status/PMCP_FLEET_PILOT.md` records the PMCPROLL final rollout
      verdict and truthfully states whether adoption is ready, PMCP-blocked, or
      limited to lexical-only use.
- [ ] `uv run pytest tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov`
      proves the final verdict records remaining PMCP issue #89 dependencies
      or states that the PMCP dependency is cleared.
- [ ] `uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py -q --no-cov`
      proves README links to the PMCP guide and pilot report without
      duplicating raw PMCP override JSON, raw PMCP payloads, or secret values.
- [ ] `uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov`
      passes and enforces the rollout policy, troubleshooting matrix, final
      verdict, README pointer, and metadata-only posture.

## Spec Closeout Plan

- schema: `spec_delta_closeout.v1`
- decision: `roadmap_amendment`
- target surfaces: `specs/phase-plans-v9.md`, `docs/guides/pmcp-fleet-integration.md`, `docs/status/PMCP_FLEET_PILOT.md`, `README.md`
- evidence paths: `docs/status/PMCP_FLEET_PILOT.md`, `tests/docs/test_pmcp_fleet_integration_docs.py`, `tests/docs/test_pmcp_fleet_pilot_report.py`
- redaction posture: `metadata_only`
- downstream handling: `roadmap amendment if execution discovers a new PMCP rollout blocker; otherwise cite the PMCPPILOT-carried PMCPROLL amendment and no further downstream phase`
