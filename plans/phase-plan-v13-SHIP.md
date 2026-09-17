---
phase_loop_plan_version: 1
phase: SHIP
roadmap: specs/phase-plans-v13.md
roadmap_sha256: 178b8328d8e7dc76ddc0804d7b72d3ccddb55e23577a3d52cb7cbd70d5fd5308
phase_loop_mutation: release_dispatch
release_base_ref: origin/main
---

# SHIP: Publish And Verify 1.4.1

## Context

Prepared before PREP freezes its candidate. This plan is not PREP acceptance
or permission to skip any review, pilot, signing or delivered-artifact gate.
Consume the accepted external PREP receipt and executor handoff only after all
three PREP exit criteria pass. The tracked pending PREP.json is not authority.
The owner's standing request authorizes merge and publication after these gates;
record that authorization independently from review acceptance before dispatch.

Execute manually while the outer phase-loop command remains disabled. Use one
serial lane, as the roadmap requires. No implementation workers, source edits,
version changes, routine hosted CI, fleet indexing or automatic cohort rollout.
If source or release inputs change, return to PREP and renew review/evidence.

## Interface Freeze Gates

- IF-0-PREP-1: Read-only accepted external receipt binds the clean source/tree,
  base-to-candidate diff, exact four independent reviews and four reconciliations,
  local/full/installed/browser checks, artifact hashes, renewed synthetic pilot
  and the single digest-only signing run. Verify actual bytes and all identities.
- IF-0-SHIP-1: External metadata-only publication receipt records merge, exact
  dispatch/run/attempt, delivered versions/digests/attestations, fresh installed
  acceptance, issue dispositions and separate rollout/deployment status.

## Lane Index & Dependencies

SL-0 — Publication And Post-Dispatch Documentation Sweep
  Depends on: (none)
  Blocks: (none)
  Parallel-safe: no

## Lanes

### SL-0 - Publication And Post-Dispatch Documentation Sweep

- **Scope**: Merge and dispatch once after acceptance, then reduce actual remote
  outcomes and delivered-artifact checks into an external release receipt.
- **Depends on**: (none)
- **Owned files**: `.phase-loop/runs/v13-SHIP-*/**`
- **Interfaces consumed**: IF-0-PREP-1 (pre-existing), accepted-prep-evidence (pre-existing), reviewed-candidate-identity (pre-existing), ship-publication-contract (pre-existing), protected-main policy (pre-existing).
- **Interfaces provided**: IF-0-SHIP-1, delivered-release-acceptance.
- **Parallel-safe**: no
- **Tasks**:
  - test: Verify every PREP receipt hash and exact seat/model disposition. Missing,
    stale, partial, refused or degraded evidence blocks before any merge/effect.
    Revalidate the renewed pilot reducer read-only on its original clean source;
    never reinitialize or spend either allowance again.
  - test: Read Code-Index-MCP#97, current main, mergeability, current remote head,
    GitHub releases/tags, PyPI and GHCR metadata. Require the PR head to equal
    the accepted candidate and the base to match reviewed assumptions. Preserve
    any unexpected open work; do not retarget or overwrite it silently.
  - impl: Record merge intent and the accepted head before invoking the existing
    GitHub merge path with an exact head guard. Read back the merge SHA/state.
    Ambiguous responses require read-only reconciliation, never a second merge
    attempt without identifying the first result.
  - test: Create an isolated clean publication worktree under
    `/mnt/workspace/worktrees` when available. Fetch protected main, verify its
    ancestry includes the accepted candidate or an explicitly validated
    squash-equivalent tree, and require full tree equality with the accepted
    candidate. Verify 1.4.1 package/lock/workflow inputs and clean synced state.
    Any content drift routes to PREP before dispatch.
  - test: Immediately recheck `v1.4.1`, PyPI `index-it-mcp==1.4.1`, GitHub release
    and `ghcr.io/consiliency/code-index-mcp:v1.4.1`, including partial prior runs.
    Record collision/partial-effect state. Do not overwrite existing artifacts.
  - impl: Write a durable dispatch-intent record outside the publication worktree,
    with accepted source/tree, default-branch SHA, version, exact workflow hash,
    owner authorization, expected inputs and `dispatch_attempted=false`. Then
    dispatch `release-automation.yml` exactly once from `main` with
    `mode=publish`, `version=v1.4.1`, `auto_merge=false`. Record request time and
    exact run identity before monitoring. Ambiguous acceptance permits only
    read-only enumeration/reconciliation; no automatic redispatch.
  - verify: Read all workflow jobs to terminal states, retain failed/partial
    outcomes, and verify the protected source and final tag/asset identities.
    Stable image promotion must follow signed candidate-image verification and
    successful Python/GitHub publication. Never substitute a local image for a
    registry-delivered image or infer publication from a green prepare job.
  - verify: Download the actual registry-delivered wheel/sdist and pull the exact
    published image digest into owned disposable validation locations. Record
    registry checksums, image digest and attestations. Compare package contents,
    version, entrypoints and dependency/lock expectations with the accepted
    artifacts; explain archive-container metadata differences explicitly rather
    than claiming unequal bytes have equal digests.
  - verify: Install the delivered wheel outside the source tree and repeat real
    PMCP provisioning/tool invocation, lexical/no-match/readiness refusal,
    migration/reopen and signal/EOF/in-flight lifecycle checks. Exercise the
    published non-root image and admin/Inspector UI paths. Use synthetic inputs
    and offline/rehearsal providers only; no additional live inference allowance
    is created by release acceptance. Record module paths and no-survivor checks.
  - impl: Post-dispatch evidence reduction reads all preceding outputs and records
    exact merge/run/tag, source/tree/workflow and delivered digests, verification
    results, timestamps and failure dispositions. No placeholder source or
    planned dispatch can satisfy this step. Outputs remain outside the clean
    publication tree; do not edit frozen status documents to manufacture proof.
  - verify: Validate all six EC dispositions against actual evidence before
    emitting IF-0-SHIP-1. Refresh qualified issues and close only proven resolved
    items with a substantive evidence comment. Keep agent-harness#848 open unless
    its upstream defect is independently fixed; the manual route is no fix.

## Verification

Read-only preflight uses `git status --porcelain`, `git rev-parse`, `git diff`,
`gh pr view 97 --repo Consiliency/Code-Index-MCP`, `gh release view v1.4.1`,
`gh run view`, registry metadata and receipt hash/reducer checks. Release-smoke
and PMCP installed checks must consume registry artifacts, not rebuild/import
the checkout. Use existing `scripts/release_smoke.py` and
`scripts/v13_pmcp_pilot.py` acceptance implementations through owned verification
drivers. Capture exact commands, exit statuses and hashes with the standalone
verification helper; do not convert missing prerequisites or skipped tests into
passing acceptance. All operational processes must be stopped and reaped.

## Acceptance Criteria

- [ ] EC-SHIP-1 - Proven by `gh pr view 97 --repo Consiliency/Code-Index-MCP`, accepted PREP hashes and external SHIP merge-intent/readback JSON assertions; falsified by candidate/base drift or missing terminal merge evidence.
- [ ] EC-SHIP-4 - Proven by `git status --porcelain`, `git rev-parse HEAD^{tree}` and accepted full-tree/release-input comparison in external SHIP preflight JSON before dispatch; falsified by dirty/unsynced source or any content drift.
- [ ] EC-SHIP-5 - Proven by external SHIP authorization/dispatch-intent JSON and `gh run view` assertions for one unique run/attempt and exact inputs; falsified by duplicate or ambiguous effects.
- [ ] EC-SHIP-2 - Proven by external SHIP registry-digest/attestation JSON and runner-stamped installed PMCP/query/migration/UI/lifecycle `verification.json`; falsified by source-only tests, a rebuilt local substitute or any failed delivered acceptance case.
- [ ] EC-SHIP-6 - Proven by external SHIP failure-disposition JSON asserting `published=true/accepted=false` and no promotion/retry for every failed delivered case; successful delivery records not-triggered; falsified by an unrecorded failure or automatic retry, rollback or promotion.
- [ ] EC-SHIP-3 - Proven by `gh issue list --repo Consiliency/Code-Index-MCP` and external SHIP receipt assertions for qualified dispositions and explicit deployed/cohort-ready restrictions; falsified by premature issue closure, implied deployment or automatic fleet expansion.

## Execution Notes

Read approved PREP/PILOT evidence and immutable original/renewed ledgers under
the existing `.phase-loop/runs/v13-PREP-*`, `v13-PILOT-*` allowlists, current
Git/registry metadata and the frozen source/scripts. Never print credentials,
raw environment values or provider payloads. Only new SHIP evidence and
resolver-owned handoff/reflection files may be written after the freeze. No
tracked manifest/status/roadmap updates in the dispatch tree; final state is
recorded in the external receipt and qualified PR/issue comments.

## Recovery And Closeout

If an artifact is published but acceptance fails, record `published=true`,
`accepted=false`, `verification=blocked`; preserve all evidence and stop issue
closure and cohort promotion. No automatic retry, yank, delete, retag, rollback
or corrective version. Those require a new bounded owner decision. A workflow
still running is not a failed or completed release. Keep process/run identities
for recovery and distinguish published, accepted, deployed and cohort-ready.

Spec closeout: `no_spec_delta`; no changes to the immutable reviewed roadmap.
Controlled rollout only, preserving the PREP support and budget restrictions.

## Spec Closeout Plan

- schema: `spec_delta_closeout.v1`
- decision: `no_spec_delta`
- target surfaces: none; metadata-only external SHIP receipt
- evidence paths: `.phase-loop/runs/v13-SHIP-*/SHIP.json`, sibling verification and dispatch-intent records; exact dispatch/run/tag/source/artifact identities and delivered checks
- redaction posture: metadata_only; never credentials or private provider payloads
- downstream handling: controlled rollout only; separate bounded fleet decision
- README.md: no-doc-change; PREP already owns versioned install and support claims.
- CHANGELOG.md: no-doc-change; PREP already owns the 1.4.1 release description.
- docs/operations/v13-release.md: no-doc-change; frozen gates and recovery policy
  remain accurate. Actual publication/acceptance lives in the external receipt
  and GitHub release, not a tracked-file mutation after one-shot evidence.
- docs/status/V13_EXECUTION.md: no-doc-change; a dated historical checkpoint is
  not final release authority. The external receipt and qualified PR comment
  supply the terminal status without invalidating the accepted candidate.
- Missing or malformed evidence: blocker_class=contract_bug; no IF-0-SHIP-1.
