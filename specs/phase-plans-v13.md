# Phase roadmap v13

## Context

Fleet-readiness remediation from `docs/status/COMPREHENSIVE_CODE_REVIEW_2026-09-09.md` and its standalone `review-evidence-2026-09-09/test_counterexamples.py`: 21 findings and 12 additional refinements. This is a new initiative; preserve v1-v12 and their historical completion records. An earlier phase marked complete does not override a newly demonstrated counterexample.

This artifact authorizes planning, not current implementation, fleet indexing, or release dispatch. Planning review and reconciliation must complete before execution. Current rollout posture remains controlled beta, not fleet-ready. Revalidate findings against implementation-time source; record obsolete findings with evidence instead of fixing historical behavior that no longer exists.

## Architecture North Star

One durable repository identity and generation binding governs registration, readiness, SQLite handles, vectors, imported content, caches, and publication. Every public query either uses that admitted generation or returns the existing unavailable/native-search contract. Installed artifacts and PMCP-managed processes must exercise the same contract as source tests.

## Assumptions

- STDIO is primary; HTTP remains a secondary local administration surface. One registered worktree per Git common directory, tracked/default branch only, remains the supported repository model.
- Existing package, profile, chunk identity, and readiness vocabulary is reused; no new state taxonomy without an explicit contract amendment.
- Linux/Python 3.12 is the initial acceptance host; owner approval is required to narrow published support, not merely missing evidence. File-backed and client/server Qdrant both require acceptance; in-memory tests cannot substitute for either.
- No commercial inference egress. Live local inference requires a bounded corpus, dry-run estimate, and approved resource budget before any request.

## Non-Goals

- No repository-wide rewrite, default semantic/reranker enablement, hostile-plugin support claim, multi-tenant HTTP service, or automatic fleet indexing.
- No edits to PMCP or agent-harness implementation here. Record substantive upstream defects separately with qualified repository references.
- No rewriting existing roadmap history, unrelated dirty files, live indexes, or credentials.

## Cross-Cutting Principles

- Retain red-at-base counterexamples; migrate them into normal discovery with real process/storage fixtures. A mock must not manufacture the behavior being proved.
- Default to `make agent-fast`, `make agent-gate`, and `make agent-full`; preserve explicit local offload and no hosted fallback. No new routine hosted jobs or weaker coverage/pin policy.
- Bind receipts to actual candidate/artifact digests and observed behavior, not predicted commits or future branch topology. Promotion never follows merely from green unit tests.
- Every downstream plan supplies `automation.suite_command`, per-goal checks, explicit exclusions, and metadata-only operational receipts. Proxy evidence needs a roadmap amendment before gate acceptance.
- Route newly discovered gaps to the nearest unstarted phase; no silent deferral. Distinguish advisory completion, code acceptance, merge, publication, and deployment.

## Finding Coverage

R01-R12 identify the audit's additional-refinement rows in their original order. Every item has one primary owner; integration checks can span phases.

| Items | Primary owner | Required disposition |
| --- | --- | --- |
| C01-C04, C16 | STATE | Transactional registration/deletion/provenance, generation handles, shutdown-aware pool |
| C05-C08, C12, C17-C18 | DATA | Preserved imported state, coherent vector publication, SDK/provenance fixes, query admission/filtering |
| C09, C13-C15, C19-C20 | SAFETY | Enforced artifact trust, supported signer, accurate sandbox boundary, termination, redacted logs, metrics ownership |
| C10-C11, C21 | DIST | Installed migrations/runtime executables, real release smoke, repaired baseline and coverage selection |
| R01 ignore fidelity; R02 missed modifications | DATA | Git-compatible admission/exclusions; lost-event reconciliation consistent with supported edit policy |
| R03 generation-aware query/rerank caches | STATE | Generation invalidation and complete candidate/profile identity; DATA validates final retrieval |
| R04 admin auth; R05 proxy trust | SAFETY | Explicit local/ephemeral support limit; no shared HTTP promotion without durable auth and trusted-proxy tests |
| R06 migration idempotence | DIST | Transactional fresh/upgrade schema parity, including partially applied scripts |
| R07 readiness cost | PILOT | Measured indexing/query contention and frozen latency/resource thresholds |
| R08 platform claims; R09 BAML parity | DIST | Evidence-backed matrix; generator/runtime compatibility and regeneration checks |
| R10 ownership/maintainability | STATE | Consolidate state ownership within touched boundaries; defer cosmetic extraction explicitly |
| R11 quality/support documentation; R12 local environment | DIST | Truthful coverage/support commands; lock-based provisioning and canonical guidance |

## Top Interface-Freeze Gates

- IF-0-FREEZE-1 - Owned data, generation/admission invariants, support limits, and evidence matrix agreed.
- IF-0-DIST-1 - Installed schema/resources and local verification contract reliable.
- IF-0-STATE-1 - Durable registry mutation and generation/handle lifetime contract implemented.
- IF-0-SAFETY-1 - Artifact trust, process shutdown, and exposure policy implemented.
- IF-0-DATA-1 - Coherent rebuild/semantic/query contract implemented.
- IF-0-PILOT-1 - PMCP-provisioned acceptance and bounded performance evidence complete.
- IF-0-PREP-1 - Versioned release candidate and exact-candidate review accepted.
- IF-0-SHIP-1 - Published artifacts independently installed and accepted; rollout disposition recorded.

## Phases

### Phase 0 - Contract And Evidence Freeze (FREEZE)

**Objective**
Freeze the shared invariants and acceptance ownership before parallel implementation.

**Exit criteria**
- [ ] EC-FREEZE-1 - All C01-C21/R01-R12 map to source, reproducer/static-risk probe, owner, command, and qualified issue or explicit roadmap-only tracking; dismissed/deferred items have evidence and rollout consequences.
- [ ] EC-FREEZE-2 - Table-by-table ownership defines retained imported data, derived data, cleanup debt, generation identity, writer/read admission, rollback, registry visibility, and metadata persistence failures.
- [ ] EC-FREEZE-3 - Freeze owner-approved support/ignore policies, PMCP inputs, explicit artifact signer/verifier, Qdrant modes, pilot workload/thresholds/budget, four reviewer identities, and evidence schemas. An unresolved signer blocks sharing acceptance; unsupported/deferred sharing requires an owner-approved amendment, never relaxed enforce mode.

**Scope notes**
Preamble/interface-only. Preserve audit inputs explicitly, never sweep unrelated dirty files. DATA owns dry-run byte/chunk/token estimates; PILOT owns budget enforcement. Freeze serial owners for shared storage/gateway/watcher files.

**Non-goals**
Implementation, measured-performance claims, upstream mutation, and inference requests.

**Key files**
- docs/status/COMPREHENSIVE_CODE_REVIEW_2026-09-09.md
- docs/status/review-evidence-2026-09-09/test_counterexamples.py
- docs/SUPPORT_MATRIX.md
- mcp_server/core/repo_context.py
- mcp_server/artifacts/semantic_profiles.py

**Depends on**
- (none)

**Produces**
- IF-0-FREEZE-1

**Spec closeout policy**
schema: spec_delta_closeout.v1; decision: canonical_spec_update; targets: v13 contract/evidence definitions and support matrix; evidence: docs/validation/v13/FREEZE.json; redaction_posture: metadata_only; missing/malformed evidence: blocker_class=contract_bug.

### Phase 1 - Distribution And Verification Fidelity (DIST)

**Objective**
Make shipped artifacts, migrations, environment setup, and local gates represent the real runtime.

**Exit criteria**
- [ ] EC-DIST-1 - Rewrite installed smoke to launch actual entrypoints: no checkout/PYTHONPATH imports or fake resolver/dispatcher. Wheel and final non-root image, using its configured startup, pass fresh/upgrade schema and mounted-Git register/index/query/restart; health/help alone cannot pass.
- [ ] EC-DIST-2 - Baseline documentation/workflow failures are fixed without relaxing policy; required regressions enter normal discovery and gate selection; every excluded suite is explicit.
- [ ] EC-DIST-3 - Supported Python/platform/language claims, BAML generator/runtime, lock setup, and agent guidance match exercised behavior; unsupported cases are restricted, not silently advertised.

**Scope notes**
Decompose into 2 lanes: packaging/migrations/container ownership; local validation/support/documentation ownership. Merge shared dependency/Makefile edits serially. Restore full schema invariants before STATE depends on them.

**Non-goals**
Version bump, publication, coverage-threshold reductions, or routine hosted CI expansion.

**Key files**
- pyproject.toml
- uv.lock
- mcp_server/storage/sqlite_store.py
- mcp_server/storage/migrations/
- docker/dockerfiles/Dockerfile.production
- scripts/release_smoke.py
- scripts/agent_validation.py
- Makefile
- baml_src/generators.baml
- tests/docs/test_p8_historical_sweep.py
- tests/test_workflow_action_pins.py
- docs/SUPPORT_MATRIX.md

**Depends on**
- FREEZE

**Produces**
- IF-0-DIST-1

**Spec closeout policy**
schema: spec_delta_closeout.v1; decision: canonical_spec_update; targets: support matrix, LOCALCI contract, installed-schema contract; evidence: docs/validation/v13/DIST.json; redaction_posture: metadata_only; missing/malformed evidence: blocker_class=contract_bug.

### Phase 2 - Durable State And Generation Ownership (STATE)

**Objective**
Make registry visibility, publication, pooled readers, watcher contexts, and caches correct across processes.

**Exit criteria**
- [ ] EC-STATE-1 - Two real processes preserve unrelated mutations, including query-triggered registry writes; observe register/unregister changes, retain publication fences, and reject failed persistence acknowledgments after restart.
- [ ] EC-STATE-2 - Cross-process writer admission and generation-aware readers prevent stale-handle queries after replacement; outstanding borrowers drain/wake; active sidecars are never unlinked beneath another holder.
- [ ] EC-STATE-3 - Watchers and all caches use current admitted generations; changed content/candidates/profile identities invalidate results; crash-boundary tests leave an old coherent or explicitly unavailable state.

**Scope notes**
Decompose into 2 lanes: registry mutation/visibility; store/pool/publication/cache ownership. The phase plan freezes interfaces first and serializes any shared-file edits. Consolidate existing state authorities, not unrelated abstractions.

**Non-goals**
Vector publication implementation, general module splitting, or destructive live-index migration.

**Key files**
- mcp_server/storage/repository_registry.py
- mcp_server/storage/store_registry.py
- mcp_server/storage/connection_pool.py
- mcp_server/storage/git_index_manager.py
- mcp_server/indexing/lock_registry.py
- mcp_server/watcher_multi_repo.py
- mcp_server/core/repo_resolver.py
- mcp_server/client.py
- mcp_server/gateway.py
- mcp_server/indexer/reranker.py

**Depends on**
- DIST

**Produces**
- IF-0-STATE-1

**Spec closeout policy**
schema: spec_delta_closeout.v1; decision: canonical_spec_update; targets: registry/publication/generation contracts; evidence: docs/validation/v13/STATE.json; redaction_posture: metadata_only; missing/malformed evidence: blocker_class=contract_bug.

### Phase 3 - Trust And Process Lifecycle (SAFETY)

**Objective**
Close artifact trust, sandbox-contract, process termination, logging, metrics, and admin-exposure gaps.

**Exit criteria**
- [ ] EC-SAFETY-1 - Enforce mode rejects absent/invalid/wrong-producer attestations before extraction; a supported signer/verifier roundtrip is proved without token display or weakening trust.
- [ ] EC-SAFETY-2 - Supported filesystem/SQLite forms obey capabilities; documentation explicitly rejects hostile-plugin containment claims unless OS-enforced evidence is supplied.
- [ ] EC-SAFETY-3 - Actual STDIO SIGTERM/SIGINT/EOF/repeated-signal and in-flight-request tests prove bounded exit and child cleanup; normal logs omit content; metrics bind/ownership is explicit across concurrent instances.
- [ ] EC-SAFETY-4 - HTTP remains local/ephemeral by default with explicit restart semantics and trusted-proxy rules; shared exposure cannot be promoted without durable auth/session acceptance.

**Scope notes**
Decompose into 2 lanes: artifact trust/sandbox; process/privacy/admin exposure. STATE/DIST must close first: installed lifecycle acceptance depends on correct artifacts, pool draining and generation handles.

**Non-goals**
Gateway/API substitution for subscription panel models, new multi-tenant auth features, or default remote metrics exposure.

**Key files**
- mcp_server/artifacts/attestation.py
- mcp_server/artifacts/artifact_download.py
- mcp_server/artifacts/publisher.py
- mcp_server/sandbox/caps_apply.py
- mcp_server/cli/stdio_runner.py
- mcp_server/metrics/prometheus_exporter.py
- mcp_server/security/auth_manager.py
- mcp_server/security/security_middleware.py
- docs/security/
- tests/integration/test_sigterm_shutdown.py

**Depends on**
- STATE

**Produces**
- IF-0-SAFETY-1

**Spec closeout policy**
schema: spec_delta_closeout.v1; decision: canonical_spec_update; targets: security threat model, attestation/lifecycle/exposure contract; evidence: docs/validation/v13/SAFETY.json; redaction_posture: metadata_only; missing/malformed evidence: blocker_class=contract_bug.

### Phase 4 - Rebuild And Retrieval Correctness (DATA)

**Objective**
Preserve owned data and publish one coherent SQLite/vector/profile generation with consistent query admission.

**Exit criteria**
- [ ] EC-DATA-1 - Real rebuilds retain imported history/documents and cleanup debt; staged semantic effects cannot alter the admitted generation before publication; failure/retry tests cover every publication boundary.
- [ ] EC-DATA-2 - File-backed and disposable real-server Qdrant pass deletion/move/scroll/cleanup at 1,000 and 1,001 chunks, interruption and retry. Never break a live file lock or silently switch backend. Every batch validates provenance before upsert, including same-dimensional model drift and restart.
- [ ] EC-DATA-3 - STDIO/HTTP/Python/cross-repository paths uniformly refuse wrong branch, stale generation, SHA-256 Git and sibling-worktree violations; source filters apply query/rank before limit; no-match remains distinct from unavailable.
- [ ] EC-DATA-4 - Git-compatible ignore rules prevent excluded inputs reaching embedding admission; missed create/modify/delete/rename events converge under the frozen edit policy; caches follow the STATE contract.

**Scope notes**
Decompose into 3 lanes: retained-data/staged publication; Qdrant/provider contracts; query/filter/ignore/reconciliation tests. Shared dispatcher/semantic-indexer edits require explicit serial ownership in the phase plan.

**Non-goals**
Broad embedding runs, changing upstream chunk IDs, or enabling experimental retrieval modes by default.

**Key files**
- mcp_server/storage/git_index_manager.py
- mcp_server/storage/sqlite_store.py
- mcp_server/utils/semantic_indexer.py
- mcp_server/utils/semantic_indexer_registry.py
- mcp_server/dispatcher/dispatcher_enhanced.py
- mcp_server/dispatcher/cross_repo_coordinator.py
- mcp_server/storage/multi_repo_manager.py
- mcp_server/health/repository_readiness.py
- mcp_server/core/ignore_patterns.py
- mcp_server/watcher/sweeper.py

**Depends on**
- STATE

**Produces**
- IF-0-DATA-1

**Spec closeout policy**
schema: spec_delta_closeout.v1; decision: canonical_spec_update; targets: rebuild, semantic admission, ignore and query contracts; evidence: docs/validation/v13/DATA.json; redaction_posture: metadata_only; missing/malformed evidence: blocker_class=contract_bug.

### Phase 5 - PMCP Pilot And Performance Acceptance (PILOT)

**Objective**
Prove actual PMCP provisioning and intended UI workflows before spending on fleet indexes.

**Exit criteria**
- [ ] EC-PILOT-1 - PMCP provisions the candidate in a clean environment with exact entrypoint, resources, registry, roots, optional handshake, and profile configuration; two unrelated temporary repos plus a sibling worktree pass lifecycle/query/refusal cases.
- [ ] EC-PILOT-2 - A bounded local-endpoint corpus stays within the approved budget and proves provenance, delete/rename/rebuild/restart behavior, latency and resource limits under query/index contention; no commercial egress occurs.
- [ ] EC-PILOT-3 - Actual PMCP/app and intended HTTP admin UI flows pass interactive browser checks, including refusal/error states and console errors; evidence identifies tested versus unshipped surfaces.
- [ ] EC-PILOT-4 - PMCP-installed runtime passes SIGTERM/SIGINT/EOF/repeated-signal, in-flight requests, metrics-port contention, reconnect and repeat provisioning without stale processes/handles. Interrupted publication remains safe; substantive upstream blockers have owner-qualified issues, not readiness waivers.

**Scope notes**
Decompose into 2 lanes: installed-runtime/PMCP/browser acceptance; performance/provenance/resource evidence. Acceptance uses disposable datasets, with workload and thresholds frozen before measurement. Genuine missing access blocks promotion, not substitutes fabricated receipts.

**Non-goals**
Indexing all repositories, modifying sister-repo code without authorization, or treating startup/health-only probes as acceptance.

**Key files**
- scripts/release_smoke.py
- scripts/agent_validation.py
- mcp_server/benchmarks/
- docs/SUPPORT_MATRIX.md
- docs/validation/v13/ (new evidence)

**Depends on**
- DATA
- SAFETY

**Produces**
- IF-0-PILOT-1

**Spec closeout policy**
schema: spec_delta_closeout.v1; decision: canonical_spec_update; targets: operator provisioning guide and rollout verdict; evidence: docs/validation/v13/PILOT.json; redaction_posture: metadata_only; missing/malformed evidence: blocker_class=contract_bug.

### Phase 6 - Release Preparation And Code Review (PREP)

**Objective**
Prepare an appropriately version-bumped candidate and reconcile the required four-agent code review without publishing.

**Exit criteria**
- [ ] EC-PREP-1 - Version, lock metadata, changelog, release notes and workflow inputs agree; all findings have evidence-backed dispositions and local full/release gates pass on the candidate.
- [ ] EC-PREP-2 - Open the scoped PR; four requested model seats review the actual final candidate with cross-model reconciliation. Fixes trigger affected checks and renewed exact-candidate review; missing/degraded seats never count as approvals.
- [ ] EC-PREP-3 - Accepted review/check receipts, artifact digests, release inputs, remaining support limits, and explicit clean publication-worktree instructions are handed to SHIP; no package/tag is published here.

**Scope notes**
Decompose into 2 lanes: version/docs/preparation; independent review/evidence reconciliation. Reconfirm model identities and current upstream release state at execution time; never substitute models silently.

**Non-goals**
External release dispatch, auto-merging before reconciliation, or widening support claims beyond PILOT evidence.

**Key files**
- pyproject.toml
- uv.lock
- mcp_server/__init__.py
- CHANGELOG.md
- .github/workflows/release-automation.yml
- specs/phase-plans-v13_reviews.md

**Depends on**
- PILOT

**Produces**
- IF-0-PREP-1

**Spec closeout policy**
schema: spec_delta_closeout.v1; decision: canonical_spec_update; targets: release notes and support disposition; evidence: docs/validation/v13/PREP.json; redaction_posture: metadata_only; missing/malformed evidence: blocker_class=contract_bug.

### Phase 7 - Publish And Verify Distribution (SHIP)

**Objective**
Merge the accepted candidate, publish through the existing controlled workflow, and verify the actual delivered artifacts.

**Exit criteria**
- [ ] EC-SHIP-1 - Revalidate authorization and accepted checks, then merge the exact reviewed candidate; record the merge result before any publish effect.
- [ ] EC-SHIP-4 - Post-merge/pre-publication gate verifies default-branch source and release inputs match the accepted content/artifact identities, with isolated clean-worktree preflight. Drift routes to PREP and renewed review, not publication.
- [ ] EC-SHIP-5 - Independently record publication authorization and dispatch only after EC-SHIP-4 passes; interrupted publication follows documented recovery without blind retries.
- [ ] EC-SHIP-2 - Registry-delivered wheel/image versions, digests and attestations match the accepted release; fresh installation repeats critical PMCP/query/migration/lifecycle acceptance outside the source tree.
- [ ] EC-SHIP-6 - If published artifacts fail acceptance, record published=true/accepted=false and blocked verification; stop cohort promotion and issue closure, preserve evidence, and follow owner-approved corrective-release/rollback policy. Never silently retry, yank, or delete artifacts.
- [ ] EC-SHIP-3 - Close corresponding qualified issues only after acceptance; record published versus deployed/cohort-ready state, residual restrictions and rollback instructions. Fleet expansion requires a separate bounded cohort decision.

**Scope notes**
Single lane because external publication effects must serialize. Downstream plan declares `phase_loop_mutation: release_dispatch`. No source/version/workflow edits here; route any required edit back to PREP for renewed review. Metadata-only receipts use an approved evidence location outside the clean dispatch tree.

**Non-goals**
Force pushes, destructive synchronization, rerunning a partially completed publication without recovery, or automatic fleet onboarding.

**Key files**
- .github/workflows/release-automation.yml (read-only)
- docs/validation/v13/PREP.json (read-only evidence)
- scripts/release_smoke.py (read-only acceptance surface)

**Depends on**
- PREP

**Produces**
- IF-0-SHIP-1

**Spec closeout policy**
schema: spec_delta_closeout.v1; decision: no_spec_delta; targets: none; evidence: runner-owned publication receipt outside dispatch tree; redaction_posture: metadata_only; missing/malformed evidence: blocker_class=contract_bug.

## Phase Dependency DAG

```text
FREEZE -> DIST -> STATE -> DATA -> PILOT -> PREP -> SHIP
                  STATE -> SAFETY --^
```

## Execution Notes

- Begin with `codex-plan-phase specs/phase-plans-v13.md FREEZE` only after roadmap panel reconciliation. Implement nothing from an unreviewed draft.
- DIST precedes STATE because missing/partial migrations invalidate multi-process schema proofs. STATE precedes SAFETY for installed shutdown/handle acceptance. DATA and SAFETY join before PILOT.
- SAFETY/DATA serialize by default where gateway, watcher, STDIO, dispatcher, registry or documentation ownership overlaps. Concurrency requires a machine-verified read/write conflict graph; the dependency DAG alone grants none.
- Each implementation phase is planned with `codex-plan-phase`; validate lane ownership and scheduler worktree assignments before parallel edits. Prompt-only claims of disjointness do not authorize concurrent writes.
- Preserve existing canonical `.phase-loop` history. Explicitly select v13 on execution rather than resuming stale v9 state. New worktrees belong under `/mnt/workspace/worktrees` on this host.
- All implementation gates remain unchecked at roadmap closeout. Roadmap review does not approve code or authorize SHIP prematurely.

## Verification

Planning runs only `phase-loop validate-roadmap specs/phase-plans-v13.md` and artifact/coverage inspection. No tests, builds, generators, migrations, browser workflows, or inference are executed while planning.

Downstream plans must bind every EC to commands and machine-readable results. Extend existing tests before selecting final node IDs; below are existing entrypoints, not claims that they already prove all v13 behavior:

```sh
make agent-fast
make agent-gate
make agent-full
make release-smoke
make release-smoke-container
uv run --locked --extra dev pytest tests/test_registry_concurrency.py tests/test_store_registry.py -q --no-cov
uv run --locked --extra dev pytest tests/test_embedding_provenance.py tests/test_tool_readiness_fail_closed.py -q --no-cov
uv run --locked --extra dev pytest tests/integration/test_sigterm_shutdown.py -m 'not requires_network and not benchmark' -q --no-cov
```

Promote the retained counterexamples into named tests and local gate selection; the historical standalone corpus is evidence, not a forever-failing release gate. Real installed-wheel/container and multi-process cases must not import checkout modules or substitute fake dispatchers. Run groups so multiple failures are reported; flag and partition proof nodes exceeding roughly five minutes instead of hiding them behind one opaque long-running command. Record skipped/network/provider/platform coverage explicitly. PILOT/PREP require runner-stamped operational receipts; lack of access is blocked evidence, not a waiver.

Container, Qdrant-service, provisioning, browser and contention proofs require separate named nodes with individual timeouts and partial machine-readable results.

## External Inputs

Use the lockfile and supported provider contracts. Resolve candidate, endpoint/profile, PMCP runtime, Qdrant server and signer identities at the relevant planning boundary. Requested review seats are Fable, GPT-5.6 Sol, Grok 4.5 and Gemini 3.1 Pro. Unavailability requires explicit owner approval for replacement and a new affected review round; no silent substitutions. Invocation receipts, not model self-description, establish requested routing. No speculative infrastructure or future-commit pins.
