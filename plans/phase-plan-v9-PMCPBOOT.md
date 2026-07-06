---
phase_loop_plan_version: 1
phase: PMCPBOOT
roadmap: specs/phase-plans-v9.md
roadmap_sha256: 9616972b5b753251691b1c95955072d37dfd9cbc90294dd1416a74b204026b3d
---
# PMCPBOOT: Fleet Bootstrap And Semantic Preflight

## Context

PMCPBOOT is Phase 2 of `specs/phase-plans-v9.md`. It makes the local semantic
stack, Qdrant endpoint, allowed roots, and repository registration bootstrap
predictable before PMCP-mediated pilot calls begin.

Planning state gathered for this artifact:

- The roadmap hash was checked locally and matches
  `9616972b5b753251691b1c95955072d37dfd9cbc90294dd1416a74b204026b3d`.
- Canonical `.phase-loop/state.json` and `.phase-loop/tui-handoff.md` still
  describe `PMCPENTRY` as planned, but the newer `.phase-loop/events.jsonl`
  records `PMCPENTRY` complete with closeout commit
  `152761f313fc6833922c91772c807664adc6a868`. Live git topology is clean at
  that commit, so this plan reconciles current phase state from the newer
  ledger plus `git status --short`/HEAD and does not use legacy
  `.codex/phase-loop/` state.
- PMCP gateway catalog discovery shows `index-it-mcp` as a provisionable local
  manifest server, but it is not currently running through PMCP and no
  downstream Code-Index-MCP tools are callable in this session. This phase
  therefore freezes deterministic preflight and readiness bootstrap surfaces;
  live PMCP-mediated query evidence remains PMCPPILOT scope.
- Existing repo surfaces already cover most of the needed contract:
  `mcp-index setup semantic --dry-run` reports Qdrant, embedding, profile,
  collection, and metadata-only API key status; `mcp-index repository status`,
  `mcp-index repository list -v`, and `mcp-index artifact workspace-status`
  surface readiness and fail-closed query guidance.
- `docs/guides/pmcp-fleet-integration.md` currently freezes the PMCP stdio
  override from PMCPENTRY. PMCPBOOT should extend that guide instead of adding
  a second operator document unless execution discovers a real gap.

This phase must not add semantic architecture changes, automatic fleet
registration, destructive Qdrant or index cleanup, or long-running indexing as
part of preflight.

## Interface Freeze Gates

- [ ] IF-0-PMCPBOOT-1 - Environment contract: repo docs and tests freeze the
      non-secret PMCP-managed stdio environment for local semantic pilot use,
      including `MCP_ALLOWED_ROOTS`, `SEMANTIC_SEARCH_ENABLED=true`,
      `SEMANTIC_DEFAULT_PROFILE=oss_high`,
      `SEMANTIC_EMBEDDING_BASE_URL=http://ai:8001/v1`,
      `QDRANT_URL=http://localhost:6333`, `SEMANTIC_AUTOSTART_QDRANT=false`,
      and `MCP_AUTO_INDEX=false`; examples must use canonical env names and
      must not use the non-canonical `MCP_QDRANT_URL` spelling.
- [ ] IF-0-PMCPBOOT-2 - Bootstrap contract: repo docs and tests freeze a
      deterministic, no-surprise bootstrap sequence using `mcp-index setup
      semantic --dry-run`, `mcp-index repository register`, `mcp-index
      repository list -v`, `mcp-index repository status`, and `mcp-index
      artifact workspace-status`, and they explain how to interpret `ready`,
      `stale_commit`, `wrong_branch`, `missing_index`,
      `path_outside_allowed_roots`, and `index_unavailable` with
      `safe_fallback: "native_search"` before trusting PMCP-mediated indexed
      results.

## Lane Index & Dependencies

SL-0 — Semantic/Qdrant preflight contract tests
  Depends on: (none)
  Blocks: SL-2
  Parallel-safe: yes

SL-1 — Repository readiness/bootstrap contract tests
  Depends on: (none)
  Blocks: SL-2
  Parallel-safe: yes

SL-2 — PMCPBOOT documentation reducer
  Depends on: SL-0, SL-1
  Blocks: (none)
  Parallel-safe: no

Lane DAG:

```text
SL-0 ----\
          -> SL-2 -> PMCPBOOT acceptance
SL-1 ----/
```

## Lanes

### SL-0 — Semantic/Qdrant Preflight Contract Tests

- **Scope**: Add PMCPBOOT-focused tests that prove the existing semantic setup
  command can express the local Qdrant and `oss_high` embedding preflight
  without mutating indexes or starting long-running work.
- **Owned files**: `tests/test_pmcp_fleet_preflight.py`
- **Interfaces provided**: `tests/test_pmcp_fleet_preflight.py`,
  `PMCPBOOT semantic preflight test contract`, `IF-0-PMCPBOOT-1`
- **Interfaces consumed**: `mcp_server.cli.setup_commands.setup` (pre-existing),
  `mcp_server.setup.semantic_preflight.run_semantic_preflight` (pre-existing),
  `tests/test_setup_cli.py` patterns (pre-existing),
  `docs/guides/pmcp-fleet-integration.md` (pre-existing)
- **Parallel-safe**: yes
- **Tasks**:
  - test: Create `tests/test_pmcp_fleet_preflight.py` with deterministic tests
    that invoke `setup semantic --dry-run --profile oss_high --qdrant-url
    http://localhost:6333 --openai-api-base http://ai:8001/v1` through
    `CliRunner` with monkeypatched preflight data, proving dry-run output names
    the active profile, collection bootstrap state, Qdrant endpoint, embedding
    endpoint, blocker code, and `can_write_semantic_vectors` result.
  - test: Add content assertions that the PMCP fleet guide names
    `MCP_ALLOWED_ROOTS`, `SEMANTIC_SEARCH_ENABLED`,
    `SEMANTIC_DEFAULT_PROFILE`, `SEMANTIC_EMBEDDING_BASE_URL`, `QDRANT_URL`,
    `SEMANTIC_AUTOSTART_QDRANT=false`, and `MCP_AUTO_INDEX=false`, and rejects
    `MCP_QDRANT_URL` in the PMCP override.
  - impl: Reuse existing setup command and semantic preflight dataclasses; do
    not add a new CLI helper unless these tests expose an actual inability to
    express the contract.
  - impl: Keep all endpoint and credential handling metadata-only; assert env
    variable names and presence flags, never secret values.
  - verify: `uv run pytest tests/test_pmcp_fleet_preflight.py -q --no-cov`

### SL-1 — Repository Readiness/Bootstrap Contract Tests

- **Scope**: Extend PMCP docs tests so the guide freezes the repository
  registration, readiness, and fail-closed fallback vocabulary needed before
  PMCP-mediated pilot queries.
- **Owned files**: `tests/docs/test_pmcp_fleet_integration_docs.py`
- **Interfaces provided**: `tests/docs/test_pmcp_fleet_integration_docs.py`,
  `PMCPBOOT repository readiness docs contract`, `IF-0-PMCPBOOT-2`
- **Interfaces consumed**: `mcp_server.cli.repository_commands.repository`
  (pre-existing), `tests/test_repository_commands.py::test_status_reports_rollout_and_query_surfaces`
  (pre-existing coverage), `docs/guides/pmcp-fleet-integration.md`
- **Parallel-safe**: yes
- **Tasks**:
  - test: Add focused docs assertions that the PMCP guide documents one
    registered worktree per git common directory, `mcp-index repository
    register`, `mcp-index repository list -v`, `mcp-index repository status`,
    and `mcp-index artifact workspace-status` as the bootstrap/status command
    set before PMCP-mediated queries.
  - test: Assert the guide explains `ready`, `stale_commit`, `wrong_branch`,
    `missing_index`, `path_outside_allowed_roots`, `index_unavailable`, and
    `safe_fallback: "native_search"` in the PMCP readiness/fallback section.
  - test: Assert the preflight section marks `reindex`, `repository sync`, and
    long-running indexing commands as remediation after non-ready evidence, not
    as surprise preflight steps.
  - impl: Reuse existing repository status/list/workspace-status vocabulary;
    do not change repository runtime behavior in this lane.
  - verify: `uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py -q --no-cov`

### SL-2 — PMCPBOOT Documentation Reducer

- **Scope**: Extend the PMCP fleet integration guide with the semantic
  preflight and repository bootstrap workflow that satisfies both upstream
  test contracts.
- **Owned files**: `docs/guides/pmcp-fleet-integration.md`
- **Interfaces provided**: `IF-0-PMCPBOOT-1`, `IF-0-PMCPBOOT-2`,
  `PMCPBOOT operator bootstrap workflow`
- **Interfaces consumed**: `tests/test_pmcp_fleet_preflight.py`,
  `tests/docs/test_pmcp_fleet_integration_docs.py`, existing semantic setup
  docs, existing MRREADY rollout vocabulary, PMCP gateway catalog observation
- **Parallel-safe**: no
- **Tasks**:
  - test: Run the SL-0 and SL-1 tests first and confirm they fail on missing
    PMCPBOOT guide content before editing the guide.
  - impl: Update the local PMCP override env block to use canonical non-secret
    env names for the pilot semantic stack, replacing `MCP_QDRANT_URL` with
    `QDRANT_URL`, adding `SEMANTIC_DEFAULT_PROFILE=oss_high`,
    `SEMANTIC_EMBEDDING_BASE_URL=http://ai:8001/v1`,
    `SEMANTIC_AUTOSTART_QDRANT=false`, and `MCP_AUTO_INDEX=false`, and keeping
    `MCP_ALLOWED_ROOTS` as an absolute-path placeholder.
  - impl: Add a semantic preflight section with copyable dry-run commands for
    Qdrant and the `oss_high` embedding endpoint, explicitly stating that
    dry-run preflight does not create collections, write vectors, or start
    long-running indexing.
  - impl: Add a repository bootstrap/readiness section that documents
    registration, status/list/workspace-status checks, one-worktree identity,
    readiness interpretation, native-search fallback, and remediation routing.
  - impl: Record the no doc change decision for README, CHANGELOG, and release
    notes: PMCPENTRY already added the README guide pointer, and PMCPBOOT is a
    guide-local bootstrap/preflight contract, not a release/package phase.
  - impl: Keep PMCPPILOT live PMCP calls, query evidence, and multi-repo pilot
    reporting out of this phase.
  - verify: `uv run pytest tests/test_pmcp_fleet_preflight.py tests/docs/test_pmcp_fleet_integration_docs.py -q --no-cov`
  - verify: `git diff --check -- docs/guides/pmcp-fleet-integration.md tests/test_pmcp_fleet_preflight.py tests/docs/test_pmcp_fleet_integration_docs.py`

## Execution Notes

- SL-0 and SL-1 are writer-disjoint and may run in parallel if the scheduler
  assigns separate worktrees. SL-2 is a reducer and must run after both test
  contracts are present.
- No lane owns PMCP gateway code, PMCP manifests, `pmcp-code-mode-mcp`, runtime
  semantic indexing architecture, destructive Qdrant cleanup, release metadata,
  or broad repository registration automation.
- No `## Execution Policy` or `## Dispatch Hints` override is needed. The
  runner should use CLI/operator policy first, then roadmap policy if present,
  then registry defaults, with no silent downgrade.
- Operational examples must remain non-secret and metadata-only. They may name
  env variables and placeholder endpoint URLs, but must not record tokens,
  API-key values, private repo data, or raw credential payloads.

## Verification

Effective automation suite command:
`uv run pytest tests/test_pmcp_fleet_preflight.py tests/docs/test_pmcp_fleet_integration_docs.py -q --no-cov`

Whole-phase verification commands:

```bash
phase-loop validate-roadmap specs/phase-plans-v9.md
uv run pytest tests/test_pmcp_fleet_preflight.py tests/docs/test_pmcp_fleet_integration_docs.py -q --no-cov
uv run pytest tests/test_setup_cli.py tests/test_repository_commands.py -q --no-cov -k "setup_semantic or status_reports_rollout_and_query_surfaces"
git diff --check -- docs/guides/pmcp-fleet-integration.md tests/test_pmcp_fleet_preflight.py tests/docs/test_pmcp_fleet_integration_docs.py
rg -n "MCP_ALLOWED_ROOTS|SEMANTIC_DEFAULT_PROFILE|SEMANTIC_EMBEDDING_BASE_URL|QDRANT_URL|MCP_AUTO_INDEX=false|setup semantic --dry-run|repository status|workspace-status|path_outside_allowed_roots|index_unavailable|native_search" docs/guides/pmcp-fleet-integration.md tests/test_pmcp_fleet_preflight.py tests/docs/test_pmcp_fleet_integration_docs.py
```

Acceptance evidence expected from execution:

- The focused PMCPBOOT preflight tests pass and prove the documented semantic
  dry-run and readiness workflow are expressed through existing command
  surfaces.
- The docs tests pass and prove the guide includes the frozen environment,
  bootstrap, readiness, and fallback vocabulary.
- `git diff --check` passes for every phase-owned file.
- The final diff touches only `docs/guides/pmcp-fleet-integration.md`,
  `tests/test_pmcp_fleet_preflight.py`, and
  `tests/docs/test_pmcp_fleet_integration_docs.py` for phase implementation
  work, plus phase-loop plan/manifest artifacts from planning.

## Acceptance Criteria

- [ ] `docs/guides/pmcp-fleet-integration.md` documents a read-only semantic
      preflight for Qdrant, the local embedding endpoint, `oss_high` embedding
      dimension/profile, allowed roots, and repo registration readiness.
- [ ] Existing setup/status surfaces are reused: `mcp-index setup semantic
      --dry-run`, `mcp-index repository register`, `mcp-index repository list
      -v`, `mcp-index repository status`, and `mcp-index artifact
      workspace-status`.
- [ ] The PMCP override examples use canonical non-secret env names including
      `QDRANT_URL`, not `MCP_QDRANT_URL`, and include explicit no-surprise
      indexing/autostart posture.
- [ ] Readiness docs explain how to interpret `ready`, `stale_commit`,
      `wrong_branch`, `missing_index`, `path_outside_allowed_roots`, and
      `index_unavailable` with `safe_fallback: "native_search"`.
- [ ] `uv run pytest tests/test_pmcp_fleet_preflight.py tests/docs/test_pmcp_fleet_integration_docs.py -q --no-cov`
      covers the documented preflight and readiness/fallback guidance.
- [ ] `tests/docs/test_pmcp_fleet_integration_docs.py` plus the final `rg`
      verification prove no command documented as part of PMCPBOOT preflight
      starts long-running indexing, writes semantic vectors, creates Qdrant
      collections, or registers the whole fleet by surprise.

## Spec Closeout Plan

- schema: `spec_delta_closeout.v1`
- decision: `no_spec_delta`
- target surfaces: `docs/guides/pmcp-fleet-integration.md`, `tests/test_pmcp_fleet_preflight.py`, `tests/docs/test_pmcp_fleet_integration_docs.py`
- evidence paths: `tests/test_pmcp_fleet_preflight.py`, `tests/docs/test_pmcp_fleet_integration_docs.py`, focused test output
- redaction posture: `metadata_only`
- downstream handling: `none`
