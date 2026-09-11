---
phase_loop_plan_version: 1
phase: DIST
roadmap: specs/phase-plans-v13.md
roadmap_sha256: 178b8328d8e7dc76ddc0804d7b72d3ccddb55e23577a3d52cb7cbd70d5fd5308
automation:
  suite_command: "uv run --locked --extra dev pytest tests/test_sqlite_migrations.py tests/test_sqlite_store.py tests/test_go_plugin.py tests/test_procenv_cli_call_sites.py tests/test_tool_schema_handler_parity.py tests/test_dispatcher.py tests/smoke/test_release_smoke_contract.py tests/docs/test_p8_historical_sweep.py tests/test_workflow_action_pins.py tests/test_baml_contract.py tests/docs/test_v13_freeze_contract.py -q --no-cov"
---

# DIST: Distribution And Verification Fidelity

## Context

Consume accepted IF-0-FREEZE-1 from docs/validation/v13/FREEZE.json, verified
after the owner's explicit approval. Preserve the reviewed roadmap bytes and
historical counterexamples. Execute manually and sequentially on
codex/v13-audit-remediation; no outer phase-loop run, inference or release.
C10/C11/C21 and R06/R08/R09/R11/R12 belong here. No support-tier reduction,
coverage reduction, or routine hosted job expansion is authorized.

## Interface Freeze Gates

- [ ] IF-0-DIST-1 - Installed migration payload, transactional upgrade parity and real installed entrypoint smoke pass with unchanged local CI policy.

## Lane Index & Dependencies

SL-0 — Installed artifacts and schema
  Depends on: (none)
  Blocks: SL-1, SL-2
  Parallel-safe: no

SL-1 — Verification fidelity and support
  Depends on: SL-0
  Blocks: SL-2
  Parallel-safe: no

SL-2 — Acceptance reducer
  Depends on: SL-0, SL-1
  Blocks: (none)
  Parallel-safe: no

## Lanes

### SL-0 - Installed artifacts and schema

- **Scope**: Repair package resources and migrations, then replace help/mock-only smoke with real installed workflows.
- **Owned files**: `pyproject.toml`, `uv.lock`, `mcp_server/storage/sqlite_store.py`, `mcp_server/storage/migrations/*.sql`, `docker/dockerfiles/Dockerfile.production`, `scripts/release_smoke.py`, `scripts/installed_runtime_smoke.py`, `tests/test_sqlite_migrations.py`, `tests/smoke/test_release_smoke_contract.py`, `baml_src/generators.baml`, `mcp_server/indexing/baml_client/**`, `tests/test_baml_contract.py`, `mcp_server/cli/stdio_runner.py`, `tests/test_tool_schema_handler_parity.py`, `mcp_server/dispatcher/dispatcher_enhanced.py`, `tests/test_dispatcher.py`
- **Interfaces provided**: installed-schema, installed-smoke, baml-parity
- **Interfaces consumed**: freeze-contract (pre-existing), source-audit (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - test: Reproduce absent migration resources and partial ALTER migration failure. Add actual SQLite fresh/upgrade/reopen, retained-row, duplicate-column continuation and rollback controls.
  - impl: Ship SQL with importlib.resources loading; use SQLite complete_statement for triggers and one transaction per migration, skip only a confirmed duplicate-column statement, record completed version only after every statement succeeds. Reconcile already-marked partial installations with idempotent migrations.
  - impl: Install Git in the final non-root image and install the application non-editably; remove checkout import dependence. Audit declared resources.
  - impl: Exercise a wheel installed into an external venv with isolated HOME/config and no PYTHONPATH. Spawn canonical entrypoints, initialize MCP, register/index/query/no-match/unregistered/wrong-branch/restart with a disposable Git repository and actual SDK transport.
  - impl: Exercise configured non-root image startup plus mounted disposable Git registration/index/query/restart; never inject fake resolver/dispatcher or treat health as sufficient.
  - impl: Align BAML generator and runtime exactly at the locked version, regenerate, verify no generated drift without model calls.
  - impl: Installed smoke exposed an undeclared staged_full reindex response. Correct only the advertised reindex result schema and add schema validation controls; runtime lifecycle remains SAFETY-owned downstream. Keep actual SDK validation enabled.
  - impl: Container smoke exposed missing start_line/end_line in direct symbol responses. Repair those producer fields and verify the existing SymbolDef contract; generation/query admission remains STATE/DATA-owned downstream.
  - verify: Run migration/BAML/smoke contract tests, wheel smoke and container smoke with individual 300-second execution bounds and partial results. Build/install have separate bounded steps.

### SL-1 - Verification fidelity and support

- **Scope**: Repair historical test baselines and select the regressions in local gates without relaxing policy.
- **Owned files**: `tests/docs/test_p8_historical_sweep.py`, `tests/test_workflow_action_pins.py`, `scripts/agent_validation.py`, `tests/test_agent_validation.py`, `tests/test_procenv_cli_call_sites.py`, `Makefile`, `docs/SUPPORT_MATRIX.md`, `docs/status/localci-validation-contract.md`, `AGENTS.md`
- **Depends on**: SL-0
- **Interfaces provided**: local-gates, support-evidence
- **Interfaces consumed**: installed-schema, installed-smoke, baml-parity, freeze-contract (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - test: Preserve independent historical inventory including four deleted paths without main..HEAD dependence; assert missing/bad banners and accidentally resurrected paths fail.
  - impl: Account for the existing signing workflow census with immutable pins and exact trigger/job counts intact.
  - impl: Add DIST tests to agent gates and route untracked/deleted source changes correctly; keep explicit local offload and fail-closed no-hosted fallback.
  - impl: Clarify declared versus exercised runtime/platform/language support and actual coverage scope/thresholds in canonical docs; preserve tiers and record untested combinations.
  - verify: Run make agent-fast and make agent-gate; capture broad offline pytest baseline with integration/slow explicitly included and provider/network/benchmark exclusions recorded. Use locked Python 3.12 environment.

### SL-2 - Documentation and acceptance reducer

- **Scope**: Reduce actual installed, regression and support evidence against all DIST goals.
- **Owned files**: `docs/validation/v13/DIST.json`, `docs/status/V13_EXECUTION.md`
- **Depends on**: SL-0, SL-1
- **Interfaces provided**: dist-receipt
- **Interfaces consumed**: installed-schema, installed-smoke, baml-parity, local-gates, support-evidence
- **Parallel-safe**: no
- **Tasks**:
  - test: Bind source/artifact hashes and observed statuses to each EC; no future success claims.
  - impl: Record all owned finding dispositions and explicit empty issue arrays unless a qualified issue is enrolled. Preserve future STATE/DATA/SAFETY failures as open work, never as DIST fixes.
  - verify: Check ownership, stamped suite evidence and clean checkpoint before emitting IF-0-DIST-1; genuine upstream/contract failures stop dependent phases.

## Execution Notes

Two implementation lanes followed by one reducer, all serial. Lane-index
em dashes are required by the installed parser. Manual commit/push and a draft
PR are visibility checkpoints, not review or merge approval. The reviewed
roadmap remains immutable; achieved ECs live in phase receipts.

Read allowlist: committed source/config/docs, `docs/contracts/v13-freeze.json`,
`docs/validation/v13/FREEZE.json`, the two retained v13 panel JSON files,
`docs/status/COMPREHENSIVE_CODE_REVIEW_2026-09-09.md` and
`docs/status/review-evidence-2026-09-09/test_counterexamples.py` read-only.
No live indexes, secret env files, private corpora or unrelated raw data.
Synthetic temporary directories created by this phase may be read/written
and removed only by their owning tempfile/container lifecycle. Installed
dependencies and generated wheel/image resources may be inspected. Verification
may create ignored `build/**` and `index_it_mcp.egg-info/**` packaging scratch;
these are rebuildable output, never staged.
output under `.phase-loop/runs/v13-DIST-*/` stays ignored and metadata-only.
`.dev-skills/handoffs/codex-plan-phase/**`,
`.dev-skills/handoffs/codex-execute-phase/**` and `plans/manifest.json` are
operational control outputs; use the runtime resolvers/APIs. Existing handoff
history stays intact. No legacy canonical state is overwritten.

## Verification

- `uv sync --locked --python 3.12 --extra dev`
- `uv run --locked --extra dev pytest tests/test_sqlite_migrations.py tests/smoke/test_release_smoke_contract.py tests/docs/test_p8_historical_sweep.py tests/test_workflow_action_pins.py tests/test_baml_contract.py tests/docs/test_v13_freeze_contract.py -q --no-cov`
- `make agent-fast`
- `make agent-gate`
- `make release-smoke-container`
- `env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests/test_git_integration.py -m integration -q --no-cov`
- `env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests -m 'not requires_network and not benchmark' --ignore=tests/test_git_integration.py -q --no-cov`
- `git diff --check`

Each test node is capped at 300 seconds. Group/process/build timeouts and
partial outcomes are recorded; no hidden background verification. The broad
baseline includes real integration/slow cases but excludes network, benchmarks
and separately isolated Git integration tests. No coverage percentage claimed
from --no-cov runs; unchanged coverage target is exercised before release.
Live inference, Qdrant-service, signing, PMCP UI and fleet onboarding belong
downstream and are not claimed here.

## Acceptance Criteria

- [ ] EC-DIST-1 - proven by `make release-smoke` and `make release-smoke-container`, falsified by absent resources, wrong imported path, schema drift, missing Git, empty expected queries or restart failure.
- [ ] EC-DIST-2 - proven by `make agent-fast`, `make agent-gate` and the explicit broad pytest baseline, falsified by the retained historical/workflow failures or missing regression selection.
- [ ] EC-DIST-3 - proven by `tests/test_baml_contract.py`, regeneration and installed runtime receipts with support docs, falsified by mismatched generator/runtime, unbounded claim expansion or lock drift.

## Spec Closeout Plan

- schema: `spec_delta_closeout.v1`
- decision: `canonical_spec_update`
- target surfaces: `docs/SUPPORT_MATRIX.md`, `docs/status/localci-validation-contract.md`, `AGENTS.md`
- evidence paths: `docs/validation/v13/DIST.json`
- redaction posture: `metadata_only`
- downstream handling: none unless a contract discovery requires reviewed amendment.
