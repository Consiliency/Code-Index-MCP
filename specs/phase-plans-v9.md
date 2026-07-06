# Phase roadmap v9

## Context

The v8 roadmap covers MCP surface modernization. This roadmap covers a
separate adoption problem: using Code-Index-MCP internally through PMCP as the
fleet entry point, instead of wiring each agent or repository directly to this
server.

Current local audit findings:

- PMCP already has an `index-it-mcp` manifest entry and reports it as a lazy
  manifest server.
- The PMCP manifest entry currently launches `index-it-mcp serve`, which
  starts the HTTP/admin surface. PMCP's local process-backed server path expects
  stdio, so PMCP needs to launch `index-it-mcp stdio` or intentionally model a
  remote MCP transport.
- PMCP manifest entries do not currently carry all non-secret operational env
  needed for a fleet rollout: allowed roots, semantic profile, embedding
  endpoint, Qdrant endpoint, and indexing policy.
- PMCP provisioning is unpinned. PyPI currently exposes multiple
  `index-it-mcp` release lines, so unpinned provisioning can pull a different
  package version than the one validated for this internal rollout.
- Local Qdrant is reachable on `localhost:6333`.
- The local OpenAI-compatible embedding endpoint
  `http://ai:8001/v1` exposes `Qwen/Qwen3-Embedding-8B`; a probe returned
  4096-dimensional embeddings matching the `oss_high` semantic profile.
- `pmcp-code-mode-mcp` is a downstream tenant code-mode server, not the place
  to put PMCP gateway/provisioning work.
- PMCP issue #89 tracks the PMCP-owned work:
  https://github.com/ViperJuice/pmcp/issues/89

Ownership split for a clean fleet rollout is approximately 60 percent PMCP and
40 percent Code-Index-MCP. PMCP owns gateway discovery, provisioning,
version/channel behavior, local-server env/config modeling, and readiness UX at
the gateway boundary. Code-Index-MCP owns the stdio server, readiness contract,
repo-registration guidance, semantic preflight, pilot evidence, and operator
docs.

## Architecture North Star

PMCP should be the agent-facing entry point for internal code search. PMCP
discovers, provisions, starts, and invokes `index-it-mcp` lazily. Code-Index-MCP
remains a local-first, stdio-primary MCP server that owns indexing,
multi-repo readiness, search, symbol lookup, semantic retrieval, and native
fallback guidance.

```text
agent -> PMCP gateway -> lazy index-it-mcp stdio server
                    -> registered repo readiness
                    -> lexical/symbol/semantic search
                    -> fail-closed native_search fallback when not ready
```

The FastAPI admin/debug surface remains non-MCP unless a later roadmap
deliberately adds a spec-compliant remote MCP transport.

## Assumptions

- PMCP issue #89 remains the upstream tracker for PMCP-owned gateway work.
- The internal pilot can use a local PMCP `.mcp.json` override while PMCP's
  shipped manifest is corrected.
- `index-it-mcp stdio` remains the supported MCP transport for the internal
  rollout.
- Indexed results are authoritative only when repository readiness is `ready`.
- Non-ready query paths must preserve `index_unavailable` with
  `safe_fallback: native_search`.
- Semantic rollout uses the local `oss_high` profile, Qdrant, and
  `http://ai:8001/v1`.
- Fleet rollout should stay lazy by default; no auto-start across the fleet.

## Non-Goals

- No PMCP gateway implementation in this repository.
- No change to `pmcp-code-mode-mcp`.
- No remote MCP transport implementation in Code-Index-MCP unless a later
  roadmap explicitly chooses it.
- No broad indexing architecture rewrite.
- No release dispatch from this roadmap.
- No secret values in docs, examples, handoffs, or status reports.

## Cross-Cutting Principles

- Keep PMCP and Code-Index-MCP ownership boundaries explicit.
- Prefer copyable local overrides until PMCP's shipped manifest is fixed.
- Keep readiness and fallback behavior visible to agents.
- Validate the local semantic stack before pilot indexing.
- Start with a small pilot repo set before broad registration.
- Preserve local-first operation and avoid background fleet-wide auto-start.

## Top Interface-Freeze Gates

- IF-0-PMCPENTRY-1 - PMCP launch contract: PMCP can start `index-it-mcp` as a
  working MCP server through stdio or an explicitly supported remote MCP
  transport.
- IF-0-PMCPENTRY-2 - Version contract: PMCP provisioning uses an approved
  version/channel and cannot silently drift to an unintended PyPI release line.
- IF-0-PMCPBOOT-1 - Environment contract: PMCP-mediated launch supplies
  non-secret allowed-root, semantic profile, embedding endpoint, Qdrant
  endpoint, and indexing policy configuration.
- IF-0-PMCPBOOT-2 - Bootstrap contract: repo registration and initial readiness
  checks are documented and deterministic, whether PMCP owns a hook or the
  operator runs explicit commands.
- IF-0-PMCPPILOT-1 - PMCP-mediated pilot contract: PMCP-invoked
  `index-it-mcp` proves status, plugin listing, lexical search, symbol lookup,
  semantic search, readiness fallback, and reindex behavior on a small repo set.
- IF-0-PMCPROLL-1 - Fleet rollout contract: operator docs define rollout
  phases, readiness policy, fallback behavior, troubleshooting, and remaining
  PMCP dependencies.

## Phases

### Phase 1 - PMCP Entry Contract And Local Override (PMCPENTRY)

**Objective**

Freeze the PMCP dependency and provide a safe local override so internal
dogfooding can start before PMCP's shipped manifest is corrected.

**Exit criteria**

- [ ] PMCP issue #89 is linked from repo docs as the upstream PMCP tracker.
- [ ] A copyable PMCP `.mcp.json` override launches
      `uvx --from index-it-mcp==<approved-version> index-it-mcp stdio`.
- [ ] Docs explain why `index-it-mcp serve` is not the local PMCP child-process
      transport.
- [ ] Docs describe package version/channel risk and how the pilot pins it.
- [ ] Docs state whether `MCP_CLIENT_SECRET` is disabled for local
      PMCP-managed stdio or blocked on PMCP handshake support.

**Scope notes**

This phase is documentation and contract setup. It may add tests that enforce
the docs name the PMCP issue, stdio command, pinning posture, and temporary
override. It is interface-first and should decompose into 2 lanes: docs
contract/tests and operator-facing override guidance.

**Non-goals**

- No PMCP code changes in this repository.
- No live fleet indexing.
- No remote MCP transport work.

**Key files**

- `docs/guides/pmcp-fleet-integration.md`
- `tests/docs/test_pmcp_fleet_integration_docs.py`
- `README.md`

**Depends on**

- (none)

**Produces**

- IF-0-PMCPENTRY-1 - PMCP launch contract.
- IF-0-PMCPENTRY-2 - Version contract.

**Spec closeout policy**

- schema: spec_delta_closeout.v1
- expected_decision: no_spec_delta
- target_surfaces: `docs/guides/pmcp-fleet-integration.md`, `README.md`
- evidence_paths: docs tests and PMCP issue link
- redaction_posture: metadata_only
- blocker_class: contract_bug if PMCP issue #89 is missing or the override
  cannot be expressed without secrets.

### Phase 2 - Fleet Bootstrap And Semantic Preflight (PMCPBOOT)

**Objective**

Make local semantic readiness, Qdrant readiness, allowed roots, and
repo-registration bootstrap deterministic before PMCP-mediated pilot calls.

**Exit criteria**

- [ ] A documented preflight checks Qdrant, the local embedding endpoint,
      `oss_high` embedding dimension, allowed roots, and repo registration.
- [ ] Existing setup/status commands are reused where possible before adding
      new CLI surface.
- [ ] Readiness docs show how to interpret `ready`, `stale_commit`,
      `wrong_branch`, `missing_index`, `path_outside_allowed_roots`, and
      `index_unavailable`.
- [ ] Tests cover the documented preflight and readiness/fallback guidance.
- [ ] No command in this phase starts long-running indexing by surprise.

**Scope notes**

This phase may add a narrow read-only helper only if existing
`mcp-index setup semantic --dry-run` and repository status surfaces cannot
express the pilot preflight cleanly. It should decompose into 2 lanes:
semantic/Qdrant endpoint preflight and repository readiness/bootstrap
preflight.

**Non-goals**

- No semantic architecture changes.
- No automatic fleet registration.
- No destructive Qdrant or index cleanup.

**Key files**

- `docs/guides/pmcp-fleet-integration.md`
- `tests/test_pmcp_fleet_preflight.py`
- `tests/test_repository_commands.py`
- existing setup/preflight modules if needed

**Depends on**

- PMCPENTRY

**Produces**

- IF-0-PMCPBOOT-1 - Environment contract.
- IF-0-PMCPBOOT-2 - Bootstrap contract.

**Spec closeout policy**

- schema: spec_delta_closeout.v1
- expected_decision: no_spec_delta
- target_surfaces: setup/preflight docs and tests
- evidence_paths: preflight test output and readiness command examples
- redaction_posture: metadata_only
- blocker_class: contract_bug if preflight evidence cannot distinguish missing
  endpoint, missing Qdrant, missing registration, and non-ready index states.

### Phase 3 - PMCP-Mediated Pilot (PMCPPILOT)

**Objective**

Prove real PMCP-mediated operation on a small internal repo set before broad
fleet adoption.

**Exit criteria**

- [ ] PMCP starts `index-it-mcp` through the local override or the fixed PMCP
      manifest.
- [ ] Pilot repos are registered one worktree per git common directory.
- [ ] PMCP-mediated calls prove `get_status`, `list_plugins`, `search_code`,
      `symbol_lookup`, semantic search, readiness fallback, and `reindex`.
- [ ] A status artifact records PMCP health, server status, package version,
      configured env names, registered repos, readiness states, query evidence,
      and failures.
- [ ] The pilot report states which repos are safe for indexed search and
      which must still use native search.

**Scope notes**

Pilot repos:

- `/home/viperjuice/code/Code-Index-MCP`
- `/home/viperjuice/code/pmcp`
- `/home/viperjuice/code/pmcp-code-mode-mcp`
- `/home/viperjuice/code/agent-harness`

This phase may use PMCP tools directly for health, describe, connect,
provision, and invoke checks, but must not record secret values.
It should decompose into 3 lanes: PMCP server startup/provision evidence,
pilot repository registration/readiness evidence, and PMCP-mediated query
evidence/reporting.

**Non-goals**

- No full fleet indexing.
- No PMCP manifest fix unless performed in the PMCP repository.
- No background auto-start policy.

**Key files**

- `docs/status/PMCP_FLEET_PILOT.md`
- `tests/docs/test_pmcp_fleet_pilot_report.py`
- `docs/guides/pmcp-fleet-integration.md`

**Depends on**

- PMCPBOOT

**Produces**

- IF-0-PMCPPILOT-1 - PMCP-mediated pilot contract.

**Spec closeout policy**

- schema: spec_delta_closeout.v1
- expected_decision: no_spec_delta
- target_surfaces: pilot status report and integration guide
- evidence_paths: `docs/status/PMCP_FLEET_PILOT.md`
- redaction_posture: metadata_only
- blocker_class: contract_bug if PMCP cannot connect to a working stdio server
  or if readiness/fallback cannot be observed through PMCP.

### Phase 4 - Fleet Rollout Policy And Adoption Guide (PMCPROLL)

**Objective**

Convert pilot evidence into an operator-facing fleet rollout policy and decide
whether adoption is ready, PMCP-blocked, or limited to lexical-only use.

**Exit criteria**

- [ ] Docs define staged rollout groups: pilot repos, core engineering repos,
      long-tail repos, opt-out repos, and repos requiring manual constraints.
- [ ] Default policy is lazy PMCP startup, no fleet-wide auto-start, and
      native-search fallback whenever readiness is not `ready`.
- [ ] Docs define when semantic indexing is enabled and when lexical-only use
      is acceptable.
- [ ] Troubleshooting covers wrong transport, wrong package version, missing
      allowed roots, missing Qdrant, missing embedding endpoint, wrong branch,
      stale commit, missing index, path-outside-allowed-roots, active system
      PMCP service versus repo-local command-mode conflicts, and host Python
      packaging incompatibilities that prevent `index-it-mcp` provisioning.
- [ ] Final rollout verdict records remaining PMCP issue #89 dependencies or
      states that the PMCP dependency is cleared.

**Scope notes**

This phase is the adoption reducer. It should not start full fleet indexing
unless the pilot evidence already proves readiness and the operator explicitly
asks to proceed. It should decompose into 2 lanes: rollout policy docs and
pilot-evidence verdict/troubleshooting docs. If PMCPPILOT stays blocked on a
system PMCP service conflict or on host `CPython 3.13` provisioning
incompatibility, this phase must reduce those findings into the rollout verdict
instead of assuming the older pilot override remains sufficient.

**Non-goals**

- No release dispatch.
- No PMCP code edits.
- No hidden auto-registration of all repos.

**Key files**

- `docs/guides/pmcp-fleet-integration.md`
- `docs/status/PMCP_FLEET_PILOT.md`
- `README.md`

**Depends on**

- PMCPPILOT

**Produces**

- IF-0-PMCPROLL-1 - Fleet rollout contract.

**Spec closeout policy**

- schema: spec_delta_closeout.v1
- expected_decision: roadmap_amendment
- target_surfaces: future roadmap if rollout exposes new blockers
- evidence_paths: `docs/status/PMCP_FLEET_PILOT.md`
- redaction_posture: metadata_only
- blocker_class: contract_bug if the final guide cannot state a truthful
  adoption verdict from pilot evidence.

## Phase Dependency DAG

```text
PMCPENTRY -> PMCPBOOT -> PMCPPILOT -> PMCPROLL
                         ^
                         |
                  PMCP issue #89
```

## Execution Notes

- PMCPENTRY can start immediately in this repository.
- PMCPBOOT can be planned after PMCPENTRY freezes the override and docs
  contract.
- PMCPPILOT depends on either PMCP issue #89 being resolved or on accepting the
  temporary PMCP local override for pilot use.
- When a shared PMCP system service is active, a repo-local command-mode PMCP
  entry is not sufficient pilot proof; rollout guidance must either use the
  remote gateway URL or document how the operator disables the conflicting
  service.
- Host `CPython 3.13` packaging compatibility for `index-it-mcp` is now a
  rollout input because manifest provisioning currently fails when the required
  wheel set is unavailable.
- PMCPROLL should not be planned until PMCPPILOT records real PMCP-mediated
  evidence.
- PMCP-owned implementation should happen in `/home/viperjuice/code/pmcp`
  against issue #89, not in this repository.

## Verification

- `phase-loop validate-roadmap specs/phase-plans-v9.md`
- `uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py -q --no-cov`
- `uv run pytest tests/test_pmcp_fleet_preflight.py tests/test_repository_commands.py -q --no-cov`
- `uv run pytest tests/docs/test_pmcp_fleet_pilot_report.py -q --no-cov`

automation:
  status: ready
  next_skill: codex-plan-phase
  next_command: codex-plan-phase specs/phase-plans-v9.md PMCPENTRY
  next_model_hint: gpt-5
  next_effort_hint: medium
  human_required: false
  verification_status: not_run
  artifact: specs/phase-plans-v9.md
  next_phase: PMCPENTRY
