---
phase_loop_plan_version: 1
phase: PREP
roadmap: specs/phase-plans-v13.md
roadmap_sha256: 178b8328d8e7dc76ddc0804d7b72d3ccddb55e23577a3d52cb7cbd70d5fd5308
automation:
  suite_command: "env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests/test_release_metadata.py tests/test_v13_release_candidate.py tests/test_v13_pmcp_pilot.py tests/test_v13_pilot_budget.py tests/test_gateway_transport_boundary.py tests/test_v13_prep_repairs.py -q --no-cov -o log_cli=false"
---

# PREP: Versioned Candidate And Four-Agent Review

## Context

Execute manually after accepted PILOT. Prepare 1.4.1 as a corrective patch;
public distribution and entrypoint identities do not change. Published 1.4.0
and this checkout's prepared candidate are distinct. Code-Index-MCP#97 is the
existing scoped draft PR. No publish or merge in PREP; no routine hosted CI.

The immutable roadmap and accepted phase receipts remain read-only. The
phase-loop CLI entrypoint remains disabled under Consiliency/agent-harness#819.
Use standalone runner-stamped verification. Two serial lanes, no implementation
subagents. Read-only four-model reviews are explicitly authorized.

## Interface Freeze Gates

- [ ] IF-0-PREP-1 - Versioned content, independent exact-candidate review/reconciliation, local gates and immutable evidence accepted for SHIP.

## Evidence And Review Contract

Version changes are metadata, not fresh inference proof. Preserve original PILOT
source, wheel and manifest bindings. A fail-closed comparison must establish
identical runtime package bytes except the sole __version__ assignment, identical
structured project/lock contents except this distribution's version, and no
unreviewed runtime/configuration changes outside that package. Validate original
live records and archived ledger read-only against their original manifest.
Record this as consumption of phase-bound evidence, never a live run of 1.4.1.
Fresh candidate installed-wheel, full, Qdrant, container, browser and loopback
rehearsal checks establish release-specific behavior. Runtime/dependency drift
invalidates this consumption and routes back to PILOT with an owner decision
if the original cumulative allowance has expired. Never create/reset a ledger,
repeat real inference automatically, relax thresholds or rewrite accepted proof.

Freeze final code, full base-to-candidate diff, relevant source bundle, tests,
artifact digests and support restrictions before review. Invoke exactly:
Fable claude-fable-5/max via first-party Claude subscription TUI/self-PTY;
Sol gpt-5.6-sol/max via Codex; Grok grok-4.5/max via Grok; and Gemini 3.1 Pro/high
via Gemini (display Gemini 3.1 Pro (High)). No API-key fallback, backfill or
silent replacement. Preserve configured-versus-reported model differences.
Explicit refusal is terminal for that seat and requires owner replacement.
All four independent substantive reports and all four cross-model reconciliation
responses must bind the frozen candidate. Missing/degraded seats block approval.

Keep final panel/check receipts in owned ignored PREP scratch and qualified PR
comments so their recording cannot alter reviewed source. Commit candidate
documentation/receipt schema before the final panel with honest pending status;
after approval emit the accepted metadata-only PREP.json outside the dispatch
tree and identify that canonical receipt location in the SHIP handoff. Do not
claim a pre-review placeholder is accepted. Any tracked change after review
requires new exact-candidate review; repairs first amend bounded ownership and
repeat affected verification. Support tiers never widen from review alone.

## Review Repair Amendment

Initial candidate 6310af8 failed its full gate (ten version/document assertions)
and its first three diagnostic review batches found reproduced runtime gaps.
Those reports and checks remain historical, not final-candidate acceptance.
Pause further review of rejected bytes. SL-0 now owns the enumerated bounded
repairs; SL-1 repeats the full four-seat code coverage and reconciliation after
the repaired source is frozen. Do not count incomplete Gemini batch01 coverage.

- test: Preserve counterexamples, then prove metadata identity tampering,
  reused extraction outputs/symlinks, ignored task files, cancellation timing,
  cleanup exceptions, schema mismatches, profile reconciliation, future/offset
  timestamps, client repeat-close and complete multi-language search scope.
- impl: Authenticate metadata plus archive checksum before trusting identity or
  extracting. Preserve manual digest-only signing, fail closed for old unbound
  artifacts, never silently downgrade enforce policy. Use fresh owned extraction
  output and exclusive sidecar writes; do not overlay stale files.
- impl: Make task bookkeeping count-only, reflect actual staged publication in
  cancellation responses, drain all shutdown owners, align tool output schemas,
  use generation metadata, and keep Git exclusions independent of MCP negations.
- impl: Fix confirmed release assertions and restore checklist links without
  editing historical release evidence. Clarify metrics and container bind limits.
- verify: New focused tests first, then all affected suites, the full local gate,
  installed wheel/container, both Qdrant modes and actual browser workflows.
  Candidate comparator MUST reject runtime drift; never loosen it to accept
  these repairs as version-only. An explicit fresh PILOT and signing allowance
  is requested but PENDING. No live inference, new ledger or hosted signing
  until separately approved. Preserve the expired original allowance and all
  original signatures. Later approval requires a bounded operational amendment
  with exact new allowance identity; this amendment grants no such effect.

## Renewed Operational Approval - 2026-09-15

The owner's "Approved for both" accepts exactly one additional synthetic local
pilot (100000 input units including retries, 900 seconds from first admission,
concurrency one) and one additional signing-only validation job (five minutes).
No fleet inputs, commercial inference, reset, refund or automatic retry is approved.
This supersedes the pending-effect restriction above, not historical receipts.

- The renewed approval ID is `v13-prep-178b8328-20260915-synthetic-local`.
  The sole new writable ledger is
  `.phase-loop/runs/v13-PILOT-allowance-20260915/`.
  Original `.phase-loop/runs/v13-PILOT-allowance/` stays read-only.
- SL-0 adds only the named renewal ID to the budget guard; unknown approval IDs,
  mismatched old/new ledgers, reinitialization and exhausted/deadline requests
  must refuse. No CLI override for budgets, providers or allowance roots.
  The driver selects the fixed renewed identity for live mode and the historical
  synthetic-only identity for rehearsal. Archived old evidence remains readable.
- The existing version-only validator remains unchanged in behavior. A distinct
  renewed-pilot path verifies clean exact source/tree/lock, wheel/constraints,
  offline/browser/live receipts, canonical versus archived renewed ledger and
  actual admission/provenance/contention evidence. Reject rehearsal, old approval,
  path escape, source drift and tampered receipts. It is not a version-only waiver.
- Mandatory order: freeze and sync candidate source/artifacts, pass local/full/
  installed/Qdrant/container/browser/rehearsal gates and close all code-review
  blockers before either one-shot effect. Final four-seat reconciliation includes
  operational evidence. Any later tracked or artifact change invalidates both
  fresh proofs and blocks progression pending a new owner decision.
- Prepare synthetic index metadata locally; commit/sync all source first, then
  dispatch exactly one `sign-published-image.yml` job in `index-attestation`
  mode. Pass only canonical metadata SHA-256, enforce its five-minute job limit,
  retain workflow/run identity and verify the downloaded attestation using the
  production verifier. Prove metadata/archive tampering rejects. No index/source
  upload, image-signing mode, registry publication or dispatch retry in PREP.
- Pilot permission is consumed at first request admission; signing permission
  at accepted dispatch. Uncertain dispatch/admission outcomes block any retry
  until read-only reconciliation; a proven acceptance remains consumed on failure,
  cancellation or crash. Only proven pre-effect failure leaves permission unused,
  never authorizing reset, reinitialization or an automatic retry. Record intent
  before dispatch and retain run/attempt identity; no second dispatch is allowed.
- Signing, inference and final review receipts stay in external PREP scratch,
  bound to exact source/artifacts. Preserve all failures and original evidence.
  A read-only positive legacy control must verify the original manifest, live
  records and archived ledger unchanged, with before/after hashes, alongside
  the negative control rejecting repaired runtime as version-only.

## Owner-Authorized Manual Review Route - 2026-09-17

The owner's "go with your recomendation. You have my auth" approves the
recommended manual, pointer-based four-agent panel as this release's accepted
review route, conditional on verified read-only boundaries for each reviewer.
This supersedes the supported-broker-only launch requirement below for this
release, not the required seat roster, coverage, reconciliation or acceptance
criteria. It does not change the harness's general authorization policy.

Use existing subscription CLI drivers with private, hash-bound snapshots and
short instruction/path manifests, never injected source bundles. Verify each
seat's actual file access and inability to mutate source or use unapproved tools
before substantive review. Keep provider authentication outside model-readable
inputs, preserve exact-model and tool-access evidence, and refuse unsupported
confinement instead of bypassing permissions or silently substituting a model.
Retain each independent report and subsequent cross-model disposition on the
same frozen candidate. Legitimate fixes require new exact-candidate verification
and review. Metadata-only receipts explicitly name this owner-authorized manual
route; they must not falsely claim successful governed-broker execution.

The prior Fable-only diagnostic is historical amendment evidence, not final
code approval. The operational limits and sequencing above remain unchanged.
No additional approval is needed for the already-authorized pilot/signing
effects, and no fleet expansion or unbounded inference is authorized.

## Lane Index & Dependencies

SL-0 — Version, release docs and candidate proof
  Depends on: (none)
  Blocks: SL-1
  Parallel-safe: no

SL-1 — Independent review and evidence reducer
  Depends on: SL-0
  Blocks: (none)
  Parallel-safe: no

## Lanes

### SL-0 - Version, release docs and candidate proof

- **Scope**: Prepare consistent patch metadata and prove current candidate behavior with the explicitly renewed operational evidence.
- **Depends on**: (none)
- **Owned files**: `pyproject.toml`, `uv.lock`, `mcp_server/__init__.py`, `.github/workflows/release-automation.yml`, `CHANGELOG.md`, `README.md`, `scripts/install-mcp-docker.sh`, `scripts/install-mcp-docker.ps1`, `docs/GETTING_STARTED.md`, `docs/MCP_CONFIGURATION.md`, `docs/SUPPORT_MATRIX.md`, `tests/test_release_metadata.py`, `scripts/v13_release_candidate.py`, `tests/test_v13_release_candidate.py`, `scripts/v13_pmcp_pilot.py`, `tests/test_v13_pmcp_pilot.py`, `scripts/v13_pilot_budget.py`, `tests/test_v13_pilot_budget.py`, `docs/operations/v13-release.md`, `mcp_server/artifacts/artifact_download.py`, `mcp_server/artifacts/integrity_gate.py`, `tests/test_artifact_integrity_gate.py`, `mcp_server/artifacts/artifact_upload.py`, `mcp_server/artifacts/freshness.py`, `mcp_server/artifacts/multi_repo_artifact_coordinator.py`, `mcp_server/artifacts/publisher.py`, `mcp_server/cli/artifact_commands.py`, `mcp_server/cli/task_reindex.py`, `mcp_server/cli/tool_handlers.py`, `mcp_server/cli/stdio_runner.py`, `mcp_server/client.py`, `mcp_server/core/ignore_patterns.py`, `mcp_server/core/repo_resolver.py`, `mcp_server/dispatcher/cross_repo_coordinator.py`, `mcp_server/dispatcher/dispatcher_enhanced.py`, `mcp_server/storage/sqlite_store.py`, `mcp_server/storage/multi_repo_manager.py`, `docs/operations/v13-pmcp-pilot.md`, `docs/security/attestation.md`, `docs/security/auth-boundary.md`, `.github/workflows/sign-published-image.yml`, `tests/test_v13_prep_repairs.py`, `tests/test_artifact_download.py`, `tests/test_artifact_upload.py`, `tests/test_artifact_attestation.py`, `tests/security/test_artifact_attestation.py`, `tests/test_artifact_publish_race.py`, `tests/test_artifact_publish_rollback.py`, `tests/test_artifact_auto_delta.py`, `tests/test_artifact_commands.py`, `tests/test_multi_repo_artifact_coordinator.py`, `tests/test_mcptasks_reindex.py`, `tests/test_artifact_freshness.py`, `tests/test_ignore_patterns.py`, `tests/test_python_client_contract.py`, `tests/test_cross_repo_coordinator.py`, `tests/docs/test_gabase_ga_readiness_contract.py`, `tests/docs/test_garc_rc_soak_contract.py`, `tests/docs/test_garecut_rc_recut_contract.py`, `tests/docs/test_p34_public_alpha_recut.py`, `tests/docs/test_pubname_public_docs.py`, `tests/smoke/test_mcpbase_stdio_smoke.py`, `tests/smoke/test_mcpeval_sdk_surface.py`
- **Interfaces provided**: prepared-version-contract, version-only-evidence-binding, fresh-release-checks
- **Interfaces consumed**: accepted-pilot-contract (pre-existing), accepted-freeze-contract (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - test: Release metadata tests fail for old version, lock mismatch, contradictory publication claims and incorrect installer/workflow defaults.
  - impl: Bump to 1.4.1, regenerate only root package lock metadata, add dated changelog and correct active install/support documentation without rewriting historical evidence.
  - test: Candidate evidence reducer refuses runtime/config/dependency changes, altered version assignment structure, dirty source, missing/tampered original proof and mismatched records; positive version-only control uses real temporary Git commits.
  - impl: Add bounded read-only candidate evidence validator using Git, TOML and existing strict live reducer; retain source versus delivered-version distinction.
  - test: Reject unknown approval IDs, cross-identity ledgers, alternate/symlinked allowance roots, reinitialization, CLI overrides, cumulative units above 100000 including retries, admission after 900 seconds, concurrent admission, and original-ledger writes. Preserve charged failures across crash/cancellation/restart.
  - impl: Add only the fixed renewal identity/path and a distinct renewed-pilot validator; preserve all legacy validator behavior.
  - test: Signing acceptance rejects missing/failed proof, wrong ref/mode/digest, second dispatch, production-verifier failure, metadata tampering and archive tampering.
  - verify: Perform the single digest-only signing exercise after all prerequisite gates; retain production-verifier and negative-control results.
  - verify: Focused tests, locked refresh, full local gate, independent installed wheel/PMCP, Qdrant both modes, non-root container, synthetic-provider rehearsal and actual admin/Inspector browser interactions.
  - impl: Document publication inputs, clean worktree and recovery policy: on partial effect or delivered acceptance failure stop, preserve evidence, no blind redispatch/yank/delete; corrective release or rollback requires a fresh bounded decision.
  - verify: Live GitHub/tag/PyPI collision checks and exact release inputs. Only the separately approved digest-only signing validation is permitted; no release publication.

### SL-1 - Independent review and evidence reducer

- **Scope**: Reconcile the four exact review seats and package verified release authority for SHIP.
- **Depends on**: SL-0
- **Owned files**: `docs/validation/v13/PREP.json`, `docs/status/V13_EXECUTION.md`, `specs/phase-plans-v13_reviews.md`
- **Interfaces provided**: accepted-prep-evidence, reviewed-candidate-identity, ship-publication-contract
- **Interfaces consumed**: prepared-version-contract, version-only-evidence-binding, fresh-release-checks, accepted-pilot-contract (pre-existing), accepted-freeze-contract (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - test: Evidence checklist rejects missing exact seats, source drift, unresolved substantive findings, failed/skipped required operational goals or a published-before-acceptance claim.
  - impl: Freeze the candidate and stage full review materials through the supported advisor-panel runtime; no custom spawn or authorization bypass.
  - verify: Each seat independently reviews full changes and relevant production construction sites; then reads all peer reports and supplies its reconciliation disposition on the same candidate.
  - impl: Adjudicate findings with reproductions, fix legitimate bugs through amended SL-0 ownership, rerun affected/full checks and renewed panel as required. No unresolved release blocker.
  - verify: Recheck PR base/head/mergeability and qualified open issues; retain old finding dispositions without closing unproved work.
  - impl: Emit accepted metadata-only external PREP receipt with exact source/diff/tree/artifact/check/panel hashes, all goal dispositions, explicit limits and clean SHIP instructions. Pending tracked receipt points to canonical external receipt without claiming prior approval.

## Execution Notes

Scratch read/write: `.phase-loop/runs/v13-PREP-*/**` and newly created
`.phase-loop/runs/v13-PILOT-PREP-*/**`, `build/**`,
`index_it_mcp.egg-info/**`, and the sole new canonical
`.phase-loop/runs/v13-PILOT-allowance-20260915/**`. Only new PREP-created Qdrant proof outputs under
`.phase-loop/runs/v13-DATA-qdrant-*/pytest.log` and `junit.xml` are readable.
Read-only original PILOT inputs: the accepted receipt's exact manifest and
referenced metadata/verification/browser/live records beneath
`.phase-loop/runs/v13-PILOT-366f6bca7765/`, plus its verification helper and
browser sealing helper. Read canonical allowance only via read-only ledger API.
Never read private fixture auth/config from older runs. Public Inspector launcher
from the explicitly retained PILOT-3319125434ce npm-cache package is allowed.
Only newly generated PREP browser screenshots under the primary checkout's
`.playwright-mcp/v13-prep-*.png` may be read/archived; primary source and private
indexes remain protected. Fresh synthetic credentials remain private and redacted.

Signing authority: inspect authenticated account/permission metadata without
secrets. The job's token permits only contents read, id-token write and
attestations write; assert the exact workflow, ref, mode, inputs and job policy
before dispatch. GitHub Actions-write dispatch permission is repository-scoped,
not proof of a workflow-specific credential. Do not claim a broad operator
credential cannot mutate releases, rotate credentials, or test denial by attempting
unauthorized mutations. Enforce the bounded operation through exact dispatch
validation, recorded intent and read-only run/attestation verification.

Control outputs: active PREP plan, `plans/manifest.json`, resolver-owned
planner/executor handoffs and skill reflections. Final acceptance control
updates remain outside the frozen candidate and never change its identity.
No upstream roadmap, contract, audit input or accepted receipt edits.

## Verification

- Structural plan validator and frontmatter suite.
- `uv sync --locked --python 3.12 --extra dev`
- `uv run --locked --extra dev python scripts/v13_release_candidate.py`
- `make agent-gate` and `make release-smoke-container`
- `uv run --locked --extra dev python scripts/v13_qdrant_smoke.py --mode file` and `--mode server`.
- Separate Git-manager and broad offline pytest suites exclude only requires_network/benchmark markers, with all skips recorded.
- Candidate PILOT driver prepare/offline/rehearsal/browser/verify-browser, then exactly one approved live mode and read-only verify-live on its fixed renewed ledger.
- `uv run --locked --extra dev python scripts/v13_release_candidate.py --renewed-pilot-root <owned-candidate-root>`; original mode must still reject changed runtime.
- Changed executable Python isort/black; immutable audit input hash and `git diff --check`.
- Runner-stamped named nodes bind source/tree, environment, commands/exits and operational receipts. Whole-suite nodes may exceed five minutes; stream heartbeat and preserve every failure, no opaque silent timeout retries.

## Acceptance Criteria

- [ ] EC-PREP-1 - proven by `scripts/v13_release_candidate.py` and `.phase-loop/runs/v13-PREP-*/verification/verification.json`; falsified by metadata/runtime drift, failed installed construction-site controls or stale receipt bindings. After renewal, require the distinct renewed-pilot validator, all budget negatives and unchanged positive legacy proof. Signing proof must bind frozen commit/ref/workflow revision, run ID/attempt, index-attestation mode, canonical metadata digest and subject; any missing/failed check, second dispatch or accepted tampering falsifies acceptance. Original version-only rejection remains a negative control.
- [ ] EC-PREP-2 - proven by `.phase-loop/runs/v13-PREP-*/panel/` reports with all eight seat dispositions bound to the candidate; falsified by missing/refused/degraded seats, unsupported model substitution or unresolved blocking findings.
- [ ] EC-PREP-3 - proven by `.phase-loop/runs/v13-PREP-*/PREP.json` and resolver-owned SHIP handoff; falsified by missing digests, unsupported support claims, dirty publication instructions or an effect before review acceptance.

## Spec Closeout Plan

- schema: `spec_delta_closeout.v1`
- decision: `canonical_spec_update`
- target surfaces: `docs/operations/v13-release.md`, `docs/status/V13_EXECUTION.md`, `specs/phase-plans-v13_reviews.md`
- evidence paths: `docs/validation/v13/PREP.json`, `.phase-loop/runs/v13-PREP-*/PREP.json`
- redaction posture: `metadata_only`
- downstream handling: SHIP only after exact review and verification; no automatic cohort promotion.

## External Inputs

Python 3.12, PMCP 2.7.3, MCP Inspector 2.6.0 and unchanged locked dependencies.
Qdrant image qdrant/qdrant@sha256:f1c7272cdac52b38c1a0e89313922d940ba50afd90d593a1605dbbc214e66ffb.
Four explicit review seats above; no changed runtime defaults.
