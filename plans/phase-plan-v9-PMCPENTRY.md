---
phase_loop_plan_version: 1
phase: PMCPENTRY
roadmap: specs/phase-plans-v9.md
roadmap_sha256: 9616972b5b753251691b1c95955072d37dfd9cbc90294dd1416a74b204026b3d
---
# PMCPENTRY: PMCP Entry Contract And Local Override

## Context

PMCPENTRY is Phase 1 of `specs/phase-plans-v9.md`. The phase freezes the
Code-Index-MCP side of the PMCP entry contract so internal dogfooding can use
PMCP as the agent-facing entry point before PMCP's shipped manifest is
corrected.

Planning state gathered for this artifact:

- The roadmap hash was checked locally and matches
  `9616972b5b753251691b1c95955072d37dfd9cbc90294dd1416a74b204026b3d`.
- Canonical `.phase-loop/state.json` records `PMCPENTRY` as the current phase
  with no active plan artifact; `.phase-loop/tui-handoff.md` records the last
  launch as a dry-run-only planning attempt. Legacy `.codex/phase-loop/` state
  was not used.
- The worktree is on `main` at
  `fa77ba19750e99e6f9a77fc376e6b3d1bbb64d33`, with `main...origin/main`
  ahead 192 and behind 2. `specs/phase-plans-v9.md` is staged as a new
  roadmap, and `.dev-skills/` contains existing untracked handoff artifacts.
- The current console entrypoint surface in `pyproject.toml` maps
  `index-it-mcp` to `mcp_server.cli:cli`; `mcp_server/cli/server_commands.py`
  exposes separate `stdio` and `serve` commands. The PMCP local process-backed
  child contract must therefore launch `index-it-mcp stdio`, not
  `index-it-mcp serve`.
- The repository stable surface is `index-it-mcp` version `1.2.0`. A
  planning-time PyPI JSON metadata check showed `1.2.0` is present while newer
  unrelated release lines also exist, so this phase should pin the PMCP local
  override to `index-it-mcp==1.2.0` rather than use unpinned provisioning.
- PMCP gateway tooling was probed through the available PMCP namespace, but
  only gateway maintenance/task tools are exposed in this session. This plan
  therefore uses repo-local contract docs/tests and leaves PMCP manifest
  implementation to PMCP issue #89.

This phase is documentation and contract setup only. It must not change PMCP
code, start fleet indexing, add a remote MCP transport, or modify
`pmcp-code-mode-mcp`.

## Interface Freeze Gates

- [ ] IF-0-PMCPENTRY-1 - PMCP launch contract: repo docs and docs tests
      freeze a copyable local PMCP `.mcp.json` override whose child process
      launches `uvx --from index-it-mcp==1.2.0 index-it-mcp stdio`, explains
      that `index-it-mcp serve` starts the FastAPI admin/debug surface rather
      than the local PMCP child-process MCP transport, links PMCP issue #89 as
      the upstream manifest tracker, and states the local `MCP_CLIENT_SECRET`
      posture for PMCP-managed stdio.
- [ ] IF-0-PMCPENTRY-2 - Version contract: repo docs and docs tests freeze
      `index-it-mcp==1.2.0` as the approved pilot package pin, describe the
      PyPI multi-line drift risk, and reject unpinned `uvx index-it-mcp` or
      floating-channel PMCP examples for this internal rollout.

## Lane Index & Dependencies

SL-0 — PMCP contract tests
  Depends on: (none)
  Blocks: SL-1
  Parallel-safe: no

SL-1 — PMCP documentation reducer
  Depends on: SL-0
  Blocks: (none)
  Parallel-safe: no

Lane DAG:

```text
SL-0 -> SL-1 -> PMCPENTRY acceptance
```

## Lanes

### SL-0 - PMCP Contract Tests

- **Scope**: Add focused docs tests that freeze the local override,
  stdio-vs-serve distinction, issue link, client-secret posture, README link,
  and version-pinning risk before the guide is written.
- **Owned files**: `tests/docs/test_pmcp_fleet_integration_docs.py`
- **Interfaces provided**: `tests/docs/test_pmcp_fleet_integration_docs.py`
- **Interfaces consumed**: `roadmap Phase 1 exit criteria`, `pyproject.toml`, `mcp_server/cli/server_commands.py`, `tests/docs/`, `PMCP issue #89` (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - test: Create `tests/docs/test_pmcp_fleet_integration_docs.py` with
    deterministic file-content assertions that fail until the guide names PMCP
    issue #89, includes a copyable `.mcp.json` override, pins
    `index-it-mcp==1.2.0`, uses `index-it-mcp stdio`, explains why `serve` is
    the FastAPI/admin surface, and records the `MCP_CLIENT_SECRET` local-stdio
    posture.
  - test: Include a negative assertion that the guide does not present an
    unpinned `uvx index-it-mcp` or `index-it-mcp serve` PMCP child-process
    override as the pilot command.
  - impl: Keep the test deterministic and content-based, using existing
    `tests/docs/` helper style with repo-relative `Path` constants.
  - impl: Do not write the guide or README in this lane; leave those expected
    failures for SL-1.
  - verify: `uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py -q --no-cov`

### SL-1 - PMCP Documentation Reducer

- **Scope**: Add the PMCP fleet integration guide and root README pointer that
  satisfy the SL-0 docs contract without changing runtime behavior.
- **Owned files**: `docs/guides/pmcp-fleet-integration.md`, `README.md`
- **Interfaces provided**: `IF-0-PMCPENTRY-1`, `IF-0-PMCPENTRY-2`,
  `README PMCP guide link`
- **Interfaces consumed**: `tests/docs/test_pmcp_fleet_integration_docs.py`
- **Parallel-safe**: no
- **Tasks**:
  - test: Run the SL-0 docs test first and confirm it fails on the missing
    guide or README pointer before writing documentation.
  - impl: Add `docs/guides/pmcp-fleet-integration.md` with sections for PMCP
    ownership boundary, local override JSON, stdio-vs-serve rationale,
    version/channel risk, local stdio auth posture, and explicit non-goals.
  - impl: Add a concise PMCP fleet integration pointer near the README setup
    or MCP configuration sections, preserving existing native STDIO and
    FastAPI-secondary language.
  - impl: Do not duplicate the JSON override in README; keep the guide as the
    single detailed operator surface.
  - impl: Keep all operational values non-secret and metadata-only; use env
    variable names and placeholders only.
  - verify: `uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py -q --no-cov`
  - verify: `git diff --check -- README.md docs/guides/pmcp-fleet-integration.md tests/docs/test_pmcp_fleet_integration_docs.py`

## Execution Notes

- Run lanes serially: SL-0 writes the failing docs test contract, then SL-1
  consumes that contract to write the guide and README pointer.
- No phase-owned lane writes PMCP gateway code, PMCP manifests, runtime CLI
  behavior, release metadata, or `pmcp-code-mode-mcp`.
- No `## Execution Policy` or `## Dispatch Hints` override is needed for this
  plan. The runner should use CLI/operator policy first, then registry
  defaults, with no silent model or executor downgrade.
- Keep operational examples metadata-only. The implementation may name
  environment variables and placeholder values, but must not record secret
  values.

## Verification

Effective automation suite command:
`uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py -q --no-cov`

Whole-phase verification commands:

```bash
uv run pytest tests/docs/test_pmcp_fleet_integration_docs.py -q --no-cov
git diff --check -- README.md docs/guides/pmcp-fleet-integration.md tests/docs/test_pmcp_fleet_integration_docs.py
rg -n "PMCP|pmcp-fleet-integration|index-it-mcp==1\\.2\\.0|index-it-mcp stdio|MCP_CLIENT_SECRET" README.md docs/guides/pmcp-fleet-integration.md tests/docs/test_pmcp_fleet_integration_docs.py
```

Acceptance evidence expected from execution:

- The focused docs test passes and proves the PMCP issue link, command pin,
  stdio transport, package drift warning, README pointer, and client-secret
  posture are present.
- `git diff --check` passes for every phase-owned file.
- The final diff touches only `docs/guides/pmcp-fleet-integration.md`,
  `tests/docs/test_pmcp_fleet_integration_docs.py`, and `README.md` for this
  phase.

## Acceptance Criteria

- [ ] `docs/guides/pmcp-fleet-integration.md` links PMCP issue #89 and
      identifies PMCP as the owner of shipped gateway manifest/provisioning
      fixes.
- [ ] The guide includes a copyable PMCP `.mcp.json` local override that
      launches `uvx --from index-it-mcp==1.2.0 index-it-mcp stdio`.
- [ ] The guide explains that `index-it-mcp serve` starts the FastAPI
      admin/debug surface and is not the local PMCP child-process MCP
      transport.
- [ ] The guide describes package version/channel drift risk and states that
      the pilot pin is `index-it-mcp==1.2.0`.
- [ ] The guide states whether `MCP_CLIENT_SECRET` is disabled/unset for local
      PMCP-managed stdio or blocked pending PMCP handshake support, without
      recording any secret value.
- [ ] `README.md` links to `docs/guides/pmcp-fleet-integration.md` without
      duplicating a floating or unpinned PMCP command.
- [ ] `tests/docs/test_pmcp_fleet_integration_docs.py` enforces the PMCP issue,
      stdio command, version pin, stdio-vs-serve distinction, README link,
      package drift warning, and client-secret posture.

## Spec Closeout Plan

- schema: `spec_delta_closeout.v1`
- decision: `no_spec_delta`
- target surfaces: `docs/guides/pmcp-fleet-integration.md`, `README.md`
- evidence paths: `tests/docs/test_pmcp_fleet_integration_docs.py`, focused docs test output
- redaction posture: `metadata_only`
- downstream handling: `none`
