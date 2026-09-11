# V13 Manual Execution

Updated 2026-09-11. Branch: `codex/v13-audit-remediation`.
Worktree: `/mnt/workspace/worktrees/Code-Index-MCP-v13-audit-remediation`.

## Progress

| Phase | Status | Evidence |
| --- | --- | --- |
| FREEZE | Accepted; owner decisions approved | `docs/validation/v13/FREEZE.json` |
| DIST | Implementation checkpoint; final acceptance rerun pending | Installed wheel/image checks passed; no phase gate emitted yet |
| STATE | Not started | No implementation acceptance claimed |
| SAFETY | Not started | No implementation acceptance claimed |
| DATA | Not started | No implementation acceptance claimed |
| PILOT | Not started | No inference or browser acceptance claimed |
| PREP | Not started | No version bump or implementation code review claimed |
| SHIP | Not started | No merge, publication or issue closure claimed |

The four-seat reconciled roadmap remains unchanged at SHA-256
`178b8328d8e7dc76ddc0804d7b72d3ccddb55e23577a3d52cb7cbd70d5fd5308`.
Its planning acceptance and the Grok requested-route/self-description caveat
remain in `specs/phase-plans-v13_reviews.md`.

## FREEZE Work

- Preserved the seven original roadmap/review/audit inputs byte-for-byte in this isolated worktree.
- Added `plans/phase-plan-v13-FREEZE.md`, with three serial lanes and explicit ownership.
- Added `docs/contracts/v13-freeze.json`: 33 finding mappings, all 19 application-table policies, shared ownership/admission and operational evidence contracts.
- Added offline structural and acceptance checks plus 13 passing contract tests.
- Installed locked dependencies in this worktree's own Python 3.12 environment.
- After the owner's explicit approval, reran acceptance successfully and emitted IF-0-FREEZE-1.
- Original main checkout, its index, unrelated worktrees and original v9 phase-loop history were not modified.

The independent verification helper (not the outer phase-loop runner) produced
`.phase-loop/runs/v13-FREEZE-20260911/verification.json` and its sealed log.
Summary: **13 passed in 1.98s; structural exit 0; acceptance exit 0**.
The earlier owner-decision blocker is resolved, not bypassed.

## Approved Owner Decisions

1. Synthetic-only local inference pilot: 100,000 total embedding input tokens including retries; 15 minutes maximum; no commercial egress or real fleet indexes.
2. Manual signing-only GitHub OIDC attestation job: five-minute cap; no new routine hosted CI and no private source/index uploads. Keep enforce-mode verification. Proposed mechanism follows the [GitHub attestation contract](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations); do not claim the signing workflow performed a local build.

The owner's reply "Approved" on 2026-09-11 explicitly accepted both proposals.
Approval is recorded in the contract and receipt; the original acceptance
command and standalone verification helper passed. Further publication/recovery
decisions remain in their phase's explicit gates.

The upstream startup warning remains tracked by [agent-harness#819](https://github.com/Consiliency/agent-harness/issues/819).
Normal closeout recovered and blocked synthetic invalid inputs in both tested
import orders earlier; this is not a reason to bypass candidate-specific checks.

## DIST Checkpoint

- SQL migrations are packaged, loaded as installed resources, reconciled transactionally, and checked for fresh/partial-upgrade/reopen parity.
- Preopened pooled connections refresh schema visibility; in-memory stores retain their database across connection boundaries.
- Real isolated wheel STDIO and configured non-root container HTTP workflows cover registration, indexing, queries, no match, refusal and restart.
- Corrected staged reindex output schemas and HTTP symbol line bounds exposed by those installed workflows.
- BAML generator/runtime/client are pinned and reproducible; historical tests, workflow census, changed-path routing and support/coverage documentation are aligned.
- The first stamped run recorded 2,975 broad-suite passes and 313 phase-suite passes, but rejected acceptance because the separately invoked Git integration command selected zero tests. Its evidence remains intact under `.phase-loop/runs/v13-DIST-20260911-final/`.
- Explicit integration selection exposed a pooled-schema regression; the focused reproducer and all nine Git integration tests now pass (16 combined tests). A new full stamped run must accept the final candidate, including wheel/image hashes, before IF-0-DIST-1 is produced.

## Resume

Read `docs/validation/v13/FREEZE.json` and
`.dev-skills/handoffs/codex-execute-phase/latest.md` in this worktree.
Next phase: DIST.
Command: `codex-execute-phase plans/phase-plan-v13-DIST.md`.
Do not restart v9 or infer v13 completion from old primary-checkout state.
