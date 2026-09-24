# V13 Roadmap Review And Reconciliation

## Scope And Status

Planning review only. No implementation, product test run, merge, or publication has been performed for v13. All implementation exit criteria remain unchecked. The comprehensive audit and counterexamples are pre-existing inputs, not newly executed evidence.

- Roadmap: `specs/phase-plans-v13.md`.
- First-round input SHA-256: `5f0bcdc6637c8d4c15a091ff34408a9a9e9f88a3d909a82e4da82fdacfd4000a`.
- Audit SHA-256: `de864227b7fa444e4407ec7b9428409e7abb3903a911f9a1bb3a640f5e7d4a47`.
- Counterexample SHA-256: `72aa3d3f63d828305123dd4f5dba9117206337a4113dda7e0e59e66881efe335`.
- Review brief: `specs/phase-plans-v13_panel_request.md`.
- Raw round-one results: `docs/validation/v13/roadmap-review-round1.json`.
- Raw round-two results: `docs/validation/v13/roadmap-review-round2.json`.
- Final reviewed roadmap SHA-256: `178b8328d8e7dc76ddc0804d7b72d3ccddb55e23577a3d52cb7cbd70d5fd5308`.
- Status: first-round recommendations reconciled; all four second-round responses are OK/AGREE with no remaining roadmap-level blockers. This is planning acceptance only, subject to the model-provenance limitation below. Earlier votes were not transferred to the revised bytes.

## Requested Panel

| Seat | Requested model and effort | First-round runtime status | First-round verdict |
| --- | --- | --- | --- |
| Correctness/data durability | Claude Fable 5, max, subscription TUI | OK | PARTIALLY AGREE |
| Architecture/dependencies | GPT-5.6 Sol, max, Codex subscription | OK | PARTIALLY AGREE |
| Adversarial/security | Grok 4.5, max mapped to CLI high | OK | PARTIALLY AGREE |
| Coverage/alternatives/proof cost | Gemini 3.1 Pro, High, Antigravity | OK | DISAGREE |

Four independent first-round reviews were received; none read peer votes before their review. All four found complete primary ownership for the 21 findings and 12 refinements. They identified nine blocking recommendations, with overlapping underlying concerns, plus non-blocking refinements below. First-round independence supplies a review-content ablation baseline: the Fable, Gemini, and Grok analyses did not depend on Sol's reasoning. It does not attest exact served-model identity. A later agreement after exposure to peer reviews is not counted as an additional independent reviewer.

### Model Provenance Caveat

The explicit runtime board requested `claude-fable-5`, `gpt-5.6-sol`, `grok-4.5`, and `Gemini 3.1 Pro`. The registered routes resolve these without rewriting model identity; Gemini renders as `Gemini 3.1 Pro (High)`. The live Grok CLI lists both 4.5 and 4.6, with 4.6 only as its default; the invocation passes `-m grok-4.5` explicitly.

Grok's responses nevertheless call themselves 4.6; Sol's responses use the generic label GPT-5. These self-descriptions are not serving-model attestations and must not silently replace the requested identities. The first-round serializer retained the board specification but not the runtime's separate launch-evidence property. Round two preserves the actual broker provider-model and argument/prompt digests plus fallback flags. Its explicit launch routes match the requested models, with no fallback. This establishes invocation provenance, not an independent attestation of the server-side model that answered; the Grok self-label discrepancy remains recorded, not resolved by assertion. Do not cite round one as confirmed Grok 4.5 or this run as strict serving-model ablation evidence.

## Dispositions

| Review IDs | Disposition | Roadmap correction and reasoning |
| --- | --- | --- |
| ANTIGRAVITY-BLK-1, ANTIGRAVITY-BLK-2, GROK-B2 | Accepted as explicit acceptance strengthening | EC-DIST-1 now requires rewriting installed smoke to use actual entrypoints, no checkout/PYTHONPATH imports or fake dispatcher/resolver, final non-root configured startup and mounted Git; health/help alone cannot pass. These are known defects DIST must fix, not proof that a future implementation has already failed. Do not replace the configured production startup with a different convenient command. |
| ANT-V13-B1, GROK-B1 | Accepted | EC-PILOT-4 explicitly exercises PMCP-installed SIGTERM/SIGINT/EOF/repeated signals, in-flight requests, metrics-port contention, reconnect and reprovisioning. SAFETY now depends on STATE and therefore DIST, so installed lifecycle acceptance cannot precede artifact/handle correctness. |
| ANTIGRAVITY-BLK-3 | Accepted narrowly | Added STATE -> SAFETY for real schema/handle and lifecycle dependencies. Gateway/watcher/STDIO/dispatcher/shared-document edits serialize by default; any SAFETY/DATA concurrency needs a machine-verified read/write conflict graph. Rejected the implication that the original conditional scheduling rule authorized uncontrolled parallel writes. |
| OPENAI-B01 | Accepted | File-backed and client/server Qdrant both require acceptance; in-memory SDK tests are not substitutes. EC-DATA-2 requires a disposable real service, 1,000/1,001-point boundaries, interruption and retry. Source inspection also found live-lock removal and implicit backend fallback paths; DATA now explicitly prohibits both, as part of coherent semantic ownership. |
| OPENAI-B02 | Accepted | SHIP retains existing goal IDs and adds separate post-merge identity, authorization/dispatch, and failed-published-acceptance goals. Source/release-input drift returns to PREP. published=true/accepted=false is receipt metadata, not a new phase status; verification/closure/cohort promotion remain blocked. Recovery requires owner-approved corrective release/rollback, never blind retry/yank/delete. |
| GROK-B3 | Accepted | EC-FREEZE-3 freezes an explicit signer/verifier decision. Missing signer support blocks sharing acceptance; deferring that surface requires an owner-approved amendment without weakening enforce mode. |
| ANT-V13-R1, ANTIGRAVITY-REF-1 | Accepted | Freeze the four requested seats; the external-input section records Fable, Sol, Grok 4.5 and Gemini 3.1 Pro. Unavailable models require explicit owner-approved replacement and a new affected review round, never automatic substitutions. |
| ANT-V13-R2 | Accepted | EC-STATE-1 explicitly includes query-triggered registry writes, not only indexing writers. |
| ANT-V13-R3, ANT-V13-R4, GROK-R1, GROK-R2 | Accepted | DIST precedes STATE to establish schema parity; STATE precedes SAFETY for installed lifecycle/handle semantics. FREEZE owns shared-file serialization decisions; overlapping SAFETY/DATA plans serialize by default. |
| ANTIGRAVITY-REF-2 | Accepted | EC-DATA-2 names both 1,000 and 1,001 chunks to exercise pagination boundaries. |
| OPENAI-N01 | Accepted | EC-FREEZE-1 requires qualified issue linkage or explicit roadmap-only tracking for every finding. No issues were filed during this planning turn. |
| OPENAI-N02, GROK-R4 | Accepted | Named container, Qdrant-service, provisioning, browser and contention proof nodes require individual timeouts and partial machine-readable results. |
| OPENAI-N03 | Accepted | Missing platform evidence alone does not authorize narrowing published support; owner approval and explicit matrix disposition are required. |
| GROK-R3 | Accepted | DATA owns byte/chunk/token dry-run estimates; PILOT owns budget enforcement, frozen before requests. |
| GROK-R5 | Accepted | Preserve red-at-base intent while migrating counterexamples into ordinary tests; installed-artifact acceptance forbids checkout/fake-dispatcher substitutes. Unit-level fixtures remain legitimate when they do not stand in for the boundary being claimed. |
| AUTHOR-01 | Corrected | The proposed signal-test command used `-m integration`, but that file currently has no integration marker. Replaced it with an explicit network/benchmark exclusion override so the named tests cannot all be deselected. Identified through source inspection; no product test was run. |

## Cross-Model Reconciliation

Round two reviewed the revised roadmap together with the disposition log and all four raw first-round responses. The brief required checking each accepted recommendation in the actual revised text, identifying remaining blockers, preserving disagreement, and treating neither Sol nor earlier votes as authority. Final AGREE means ready for phase planning only.

| Seat | Recorded provider launch model | Fallback | Status | Verdict |
| --- | --- | --- | --- | --- |
| Fable | `claude-fable-5` | false | OK | AGREE |
| Sol | `gpt-5.6-sol` | false | OK | AGREE |
| Grok | `grok-4.5` | false | OK | AGREE |
| Gemini | `Gemini 3.1 Pro (High)` | false | OK | AGREE |

All seven input-file digests matched the live files after the panel completed, including the primary roadmap. The raw result retains the pre-closeout review-log digest; this log was subsequently updated only to record results and downstream notes. The reviewed primary roadmap has not changed since round two.

### Downstream Planning Notes

These accepted non-blocking details operationalize existing criteria; they do not introduce new roadmap goals or transfer implementation approval. Each phase planner must read this log with the roadmap.

| Round-two recommendation | Required phase-plan treatment |
| --- | --- |
| ANT-V13-R5, OPENAI-R2-N03, GROK-R4 | Final roadmap validation and goal inspection completed: eight phases, 30 unique unchecked goals, including EC-SHIP-4/5/6. Verify required runtime gates before governed execution. |
| ANT-V13-R6 | Preserve both raw rounds and second-round launch evidence; retain the served-model limitation above. No seat substitution or degraded response was accepted. |
| ANT-V13-R7, GROK-R3 | FREEZE explicitly assigns serial ownership for `multi_repo_manager.py` as well as other overlapping DATA/SAFETY paths. No concurrent lane scheduling without machine-checked read/write ownership. |
| ANT-V13-R8, GROK-R1 | PREP hands SHIP an owner-approved corrective-release/rollback policy before dispatch, avoiding recovery-process invention after a failed publication. |
| ANT-V13-R9 | FREEZE pins exact requested reviewer model IDs and approval/replacement policy; display names alone are insufficient. |
| GROK-R2 | FREEZE explicitly distinguishes working-tree edits from committed/default-branch watcher recovery. DATA implements only the frozen policy. |
| OPENAI-R2-N01 | DATA enumerates partial embedding responses and concurrent writes alongside model drift, restart, interruption, and retry. |
| OPENAI-R2-N02 | SAFETY checks sentinel content is absent from errors and ordinary logs; sandbox evidence covers supported path-like values, SQLite URIs, inherited descriptors, and subprocess cases without claiming hostile-plugin containment. |
| Gemini metrics refinement | SAFETY defaults metrics to loopback or disabled unless durable authentication is supplied; test concurrent instance ownership and unauthorized exposure, not only successful bind. |
| Gemini SHIP sequencing refinement | Preserve goal IDs but execute dependency order, not numeric sorting: merge (1), post-merge identity (4), authorization/dispatch (5), delivered acceptance (2), then closure (3). Goal 6 is the conditional failure branch, which blocks closure/promotion; never deliberately publish a broken release to exercise it. |

## Verification And Operational Limits

On 2026-09-11, `phase-loop validate-roadmap specs/phase-plans-v13.md` passed for eight phases on the final reviewed bytes. Structured inspection verified 30 unique unchecked goal IDs (FREEZE 3, DIST 3, STATE 3, SAFETY 4, DATA 4, PILOT 4, PREP 3, SHIP 6), all declared existing key-file paths, and the 2,978-word budget. No product tests, builds, migrations, generators, browsers, or local embedding endpoint were run during this planning task.

The installed phase-loop runtime emits a circular-import warning that `fab_gate` was not registered at startup. This is the existing open [Consiliency/agent-harness#819](https://github.com/Consiliency/agent-harness/issues/819), not a Code-Index-MCP defect. Follow-up verification on 2026-09-11 used Python 3.13.12 and runtime 0.7.14 git-pinned at `c98573ebd45452451ce719406f5b28379284b4c9`. Both fresh CLI-first and panel-invoker-first processes started with four registered validators and unavailable `fab_gate`; normal `run_closeout_validators` recovered all five and returned `fab_delta_review_gate_block` at block severity for synthetic FAB inputs missing repo_root, with `PHASE_LOOP_REVIEW=block`. Both assertions passed without preloading, monkeypatching, upgrading, or changing phase/publication state. [Consumer evidence](https://github.com/Consiliency/agent-harness/issues/819#issuecomment-5629853225) extends the existing issue rather than duplicating it.

This narrows the earlier warning: startup is defective, but permanent gate absence is not demonstrated on these paths. These negative controls are not real-candidate or end-to-end execution acceptance. Preserve blocking review configuration and verify trusted gate inputs on the actual execution path; do not bypass gates or modify agent-harness from this repository. The reviewed roadmap bytes remain unchanged.

Existing `.phase-loop` state and its ledger describe completed v9 work and older Git topology. Current source remains on main at the audit baseline. No old runner state was rewritten or resumed; future execution must explicitly select v13.
