---
phase_loop_plan_version: 1
phase: FREEZE
roadmap: specs/phase-plans-v13.md
roadmap_sha256: 178b8328d8e7dc76ddc0804d7b72d3ccddb55e23577a3d52cb7cbd70d5fd5308
automation:
  suite_command: "uv run --locked --extra dev pytest tests/docs/test_v13_freeze_contract.py -q --no-cov"
---

# FREEZE: Contract And Evidence Freeze

## Context

Execute the reviewed v13 preamble manually in the isolated feature worktree.
Preserve the primary checkout and its staged/untracked files. The user's
manual-execution request authorizes phase work, not fabricated approval of the
explicit budget/signer owner decisions. Do not run inference or dispatch signing
while those decisions are pending. All production repairs remain downstream.

The primary roadmap is immutable during this phase; record achieved goals in
FREEZE evidence instead of changing the reviewed bytes. Read
`specs/phase-plans-v13_reviews.md`, including its round-two planning notes.
There are no open Code-Index-MCP issues/PRs as of kickoff. Every finding is
explicitly roadmap-only tracked until an actual downstream issue is enrolled;
do not close historical issues as a substitute for proof.

## Interface Freeze Gates

- [ ] IF-0-FREEZE-1 - `docs/contracts/v13-freeze.json` defines findings, table ownership, admission/publication invariants, support/provisioning and evidence policies; both owner decisions must have attributable approval before the gate is emitted.

## Lane Index & Dependencies

SL-0 — Verification contract
  Depends on: (none)
  Blocks: SL-1, SL-2
  Parallel-safe: no

SL-1 — Findings and policy contract
  Depends on: SL-0
  Blocks: SL-2
  Parallel-safe: no

SL-2 — Documentation and acceptance reducer
  Depends on: SL-0, SL-1
  Blocks: (none)
  Parallel-safe: no

## Lanes

### SL-0 - Verification contract

- **Scope**: Add bounded offline validation and negative controls for the freeze artifact.
- **Owned files**: `scripts/validate_v13_freeze.py`, `tests/docs/test_v13_freeze_contract.py`
- **Depends on**: (none)
- **Interfaces provided**: freeze-validator, freeze-test-results
- **Interfaces consumed**: audit-and-source (pre-existing), roadmap-panel (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - test: Write controls rejecting missing/duplicate findings, missing tables, invalid source/probe paths, roadmap digest drift, and absent approval.
  - impl: Implement standard-library validator with separate structural and acceptance modes; no product imports, provider access, registry or index mutation. Structural success must not mean owner approval or bug remediation.
  - verify: Run the named pytest file; inspect explicit subprocess output and exit status for both CLI modes.

### SL-1 - Findings and policy contract

- **Scope**: Map every audited item and freeze the support, storage and operational choices supported by evidence.
- **Owned files**: `docs/contracts/v13-freeze.json`
- **Depends on**: SL-0
- **Interfaces provided**: freeze-contract, owner-decision-records
- **Interfaces consumed**: freeze-validator, support-and-localci (pre-existing), owner-reply (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - test: Run structural validation before declaring the contract ready.
  - impl: Record all 33 item owners, exact source references, retained reproducer or static-risk probe, downstream command and explicit roadmap-only disposition.
  - impl: Define all SQLite application-table ownership, retained/imported/derived rows, cleanup debt, generation binding, writer/read admission, rollback and registry visibility. Serialize shared paths, especially multi_repo_manager.py.
  - impl: Preserve published support tiers; distinguish initial Linux/Python 3.12 proof from untested platforms. Keep default-branch-only automatic indexing, exclude ignored/untracked content from automatic ingestion, and reject dirty/stale admission instead of claiming working-tree edit support.
  - impl: Record PMCP pinned STDIO invocation, common registry/root configuration, optional secret delivery names only, both real Qdrant modes, exact requested reviewer routes, bounded proof nodes and pre-approved recovery handoff requirement.
  - impl: Record proposed synthetic-only 100,000-token/15-minute local inference budget and manual signing-only five-minute OIDC job. Approval starts pending; only update from the owner's explicit reply, never from silence or roadmap review.
  - verify: `python3 scripts/validate_v13_freeze.py`; then `python3 scripts/validate_v13_freeze.py --acceptance`. A pending decision is a real blocker for this phase, not permission to skip FREEZE.

### SL-2 - Documentation and acceptance reducer

- **Scope**: Record verified scope, partial progress and the exact next action without starting dependent phases prematurely.
- **Owned files**: `docs/validation/v13/FREEZE.json`, `docs/status/V13_EXECUTION.md`
- **Depends on**: SL-0, SL-1
- **Interfaces provided**: freeze-receipt
- **Interfaces consumed**: freeze-test-results, freeze-contract, owner-decision-records
- **Parallel-safe**: no
- **Tasks**:
  - test: Confirm every required command's exit status and all 33 dispositions; distinguish tests of the contract from fixes to the product.
  - impl: Reduce per-goal evidence. Emit IF-0-FREEZE-1 only if all three ECs pass and owner decisions are approved. Otherwise record blocked/product_decision_missing with the exact missing inputs; leave DIST unstarted.
  - impl: Preserve explicit empty issue inventory/dispositions if no issues were enrolled. Track agent-harness#819 only as an upstream runtime observation, not a Code-Index-MCP phase issue.
  - verify: Check staged whitespace, plan validity, artifact hashes and original-checkout preservation.

## Execution Notes

Run sequentially in the assigned feature worktree. Lane-index em dashes are
required by the installed validator's stanza parser; no parallel work is implied.
An unanswered owner decision leaves FREEZE incomplete and dependent phases unstarted.
Documentation impact: no_doc_delta for README/CHANGELOG/release notes, because
FREEZE changes only internal planning contracts and their checks, not product
behavior or published support. Its status/contract documentation is updated.

## Read Allowlist

Explicit evidence inputs: `docs/status/COMPREHENSIVE_CODE_REVIEW_2026-09-09.md`,
`docs/status/review-evidence-2026-09-09/test_counterexamples.py`,
`docs/validation/v13/roadmap-review-round1.json`,
`docs/validation/v13/roadmap-review-round2.json`,
`specs/phase-plans-v13_reviews.md`,
`specs/phase-plans-v13_panel_request.md`.
Preserve these exact bytes in the isolated worktree; do not modify historical evidence.
Committed source, migrations, support/CI docs, example configuration, and PMCP's
committed README/manifest are read-only reference inputs.
No ignored live indexes, local env values, credentials, private corpora, or
unrelated evidence are permitted reads. Operational handoffs under
`.dev-skills/handoffs/codex-plan-phase/` and
`.dev-skills/handoffs/codex-execute-phase/` are permitted metadata-only writes
and stay ignored. `plans/manifest.json` is a planning-control path; use the
runtime manifest API best-effort and do not migrate a legacy schema in place.
The standalone verification helper may read/write metadata and test output under
`.phase-loop/runs/v13-FREEZE-20260911/`, including its interpreter shim. This is
local verification evidence, not an outer-loop run; keep the directory ignored.
SL-2 owns its evidence reduction. No older canonical runtime state is imported
or overwritten in the isolated worktree.

## Verification

IF-0-VC-2: validate the emitted plan with the installed
`codex-plan-phase/scripts/validate_plan_doc.py` and literal validator.
Source-backed commands:
`uv sync --locked --python 3.12 --extra dev`;
`uv run --locked --extra dev pytest tests/docs/test_v13_freeze_contract.py -q --no-cov`;
`python3 scripts/validate_v13_freeze.py`;
`python3 scripts/validate_v13_freeze.py --acceptance`;
`git diff --cached --check`.

This preamble changes no production behavior. Full product tests, installed
smoke, live services and browser acceptance belong to the downstream phases;
they are not reported as run or passed here. Retained counterexamples are
read-only evidence, not rerun merely to re-establish an unchanged source baseline.
Contract tests have a 60-second process bound. No provider call or hosted CI
dispatch belongs to this phase.

## Acceptance Criteria

- [ ] EC-FREEZE-1 - proven by `python3 scripts/validate_v13_freeze.py` and contract tests; falsified by missing finding/source/probe/owner/command/tracking entries.
- [ ] EC-FREEZE-2 - proven by `python3 scripts/validate_v13_freeze.py` against production SQLite DDL and the table/invariant contract; falsified by an unowned table or missing retained-data/admission/rollback rule.
- [ ] EC-FREEZE-3 - proven by `python3 scripts/validate_v13_freeze.py --acceptance` plus attributable owner decisions in FREEZE.json; falsified by a pending budget/signer decision or incomplete provisioning/evidence policy.

## Spec Closeout Plan

- schema: `spec_delta_closeout.v1`
- decision: `canonical_spec_update`
- target surfaces: `docs/contracts/v13-freeze.json`, `docs/status/V13_EXECUTION.md`
- evidence paths: `docs/validation/v13/FREEZE.json`
- redaction posture: `metadata_only`
- downstream handling: none; any incompatible implementation discovery returns to contract/roadmap review before acceptance.
