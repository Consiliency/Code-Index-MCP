---
phase_loop_plan_version: 1
phase: DATA
roadmap: specs/phase-plans-v13.md
roadmap_sha256: 178b8328d8e7dc76ddc0804d7b72d3ccddb55e23577a3d52cb7cbd70d5fd5308
automation:
  suite_command: "env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests/test_v13_data_storage.py tests/test_v13_data_vectors.py tests/test_v13_data_queries.py tests/test_v13_data_reconciliation.py tests/test_semantic_indexer_registry.py tests/test_embedding_provenance.py tests/test_history_search_filters.py tests/test_history_issue_storage.py tests/test_ignore_patterns.py tests/test_watcher_sweep.py tests/test_repository_readiness.py tests/test_tool_readiness_fail_closed.py -q --no-cov"
---

# DATA: Coherent Rebuild And Retrieval

## Context

Execute manually after accepted STATE and SAFETY receipts. Address C05-C08,
C12, C17-C18 and R01-R02, validating STATE cache identities at final retrieval.
The frozen 19-table ownership matrix is authoritative; original audit,
counterexamples, roadmap and upstream contracts remain immutable. All three
lanes execute serially in the existing isolated v13 feature worktree.

No real fleet registry/index is an input. This phase uses synthetic repositories
and deterministic embedding response fixtures only. A disposable loopback
Qdrant server is allowed, not the fleet server or any inference endpoint.
PILOT retains the approved live-inference budget and browser acceptance.

## Interface Freeze Gates

- [ ] IF-0-DATA-1 - Retained owned rows, generation-scoped SQLite/vector/profile publication, exact backend ownership, per-batch provenance, uniform query admission and committed-source reconciliation.

## Frozen Interfaces

A staged RepoContext explicitly identifies unpublished work and its SQLite
generation path. Semantic construction consumes that context, not a fresh
registry lookup that returns the active generation. The generation's durable
directory owns SQLite, file-backed vectors and profile metadata; server
collections use the same opaque generation identity. Existing public readiness
and semantic-profile vocabulary remains unchanged.

SemanticIndexerRegistry provides context-aware construction and scoped leases.
Retirement denies new leases but drains existing borrowers before close.
A generation/profile change opens its own resource namespace; no live .lock
unlink, old-collection mutation or silent file/server fallback is allowed.
Explicit URL, memory and filesystem selections are mutually exclusive.

Each provenance-capable embedding response is validated before vector writes,
including item arity/order/status/dimensions and complete profile attestation
against persisted model/revision/normalization/fingerprint. A matching probe
never authorizes later drift. Persisted metadata failures propagate, with
fsync/atomic replace. Legacy test doubles cannot count as provenance evidence.

Use SQLite's backup/transaction APIs for retained data, not manual FTS shadow
copies. Regenerate code rows while preserving imported document/history IDs,
relationships, valid unchanged summaries, operator configuration and cleanup
debt. Rebuild FTS/trigrams from authoritative staged rows. Pending deletes name
their original collection and are cleared only after acknowledged deletion;
staging cannot drain debt against an admitted old generation.

Full and incremental publication share process writer admission, an initial
pending fence, immutable committed input, staged storage, post-build Git/CAS
validation and durable registry publication. Failed stages stay unavailable or
leave the old coherent generation; never publish partial semantic effects.
Artifact restore uses the same staged boundary and accepted SAFETY verification
before admission. Never re-enable direct active-path extraction.

Automatic indexing admits tracked/default-branch committed content, with nested
Git ignore and negation rules plus existing index-specific exclusions applied
before parse/embedding. Use Git or a proven Git-wildmatch parser, not a new
fnmatch approximation. Snapshot committed blobs so dirty/racing working-tree
content cannot be silently stamped with a commit. Preserve canonical result
paths when parsing a disposable snapshot. Dirty tracked edits refuse query
admission through existing unavailable/native-search semantics.

All query surfaces resolve current registration, supported worktree, Git object
format and generation, then recheck before returning results. SHA-1 and SHA-256
Git commits receive identical live checks. Filtered history/friction search
applies the query and ranking to matching source records before limiting;
available no-match remains distinct from unavailable.

## Lane Index & Dependencies

SL-0 — Vector SDK, provenance and generation lease foundation
  Depends on: (none)
  Blocks: SL-1, SL-2
  Parallel-safe: no

SL-1 — Retained storage and staged publication
  Depends on: SL-0
  Blocks: SL-2
  Parallel-safe: no

SL-2 — Query admission, ignore reconciliation and documentation reducer
  Depends on: SL-0, SL-1
  Blocks: (none)
  Parallel-safe: no

## Lanes

### SL-0 - Vector SDK, provenance and generation lease foundation

- **Scope**: Establish exact Qdrant backend ownership and generation-aware semantic operations.
- **Owned files**: `mcp_server/utils/semantic_indexer.py`, `mcp_server/utils/semantic_indexer_registry.py`, `mcp_server/core/repo_context.py`, `mcp_server/utils/embedding_providers.py`, `mcp_server/artifacts/semantic_profiles.py`, `tests/test_v13_data_vectors.py`, `tests/test_semantic_indexer_registry.py`, `tests/test_semantic_stale_vector_cleanup.py`, `tests/test_semantic_indexer_collection_lifecycle.py`, `tests/test_embedding_provenance.py`, `tests/test_profile_aware_semantic_indexer.py`, `tests/test_semantic_namespace_resolver.py`, `tests/test_semantic_profiles.py`, `scripts/v13_qdrant_smoke.py`
- **Interfaces provided**: staged-context, semantic-leases, exact-backend, batch-provenance, vector-maintenance
- **Interfaces consumed**: freeze-contract (pre-existing), state-contract (pre-existing), safety-contract (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - test: Promote locked SDK removal and batch-drift counterexamples. Add 1000/1001 point deletion/move/hash lookup/soft-delete/scroll checks, preserving each vector-to-payload association.
  - impl: Use supported scroll, filtered delete and payload-only update APIs with acknowledged writes and bounded pages; make interruption/retry idempotent without forgetting debt.
  - impl: Select exactly the requested backend; report lock/availability errors without unlinking locks, connecting to another server or exposing raw provider payloads.
  - impl: Bind indexer paths, collections, SQLite mappings and metadata to explicit context generations; add scoped borrower leases and safe retirement.
  - impl: Validate each embedding batch and persisted provenance on restart; reject same-dimensional drift and partial responses before upsert. Propagate durable metadata failures.
  - test: Real file-backed client and disposable pinned server, including a second process holding the live local lock and a failed server connection with no fallback.
  - verify: Run focused vector/provenance tests and separate named file/server smoke nodes, each capped at 300 seconds. Deterministic synthetic vectors only.

### SL-1 - Retained storage and staged publication

- **Scope**: Preserve owned data while admitting one coherent durable generation.
- **Owned files**: `mcp_server/storage/sqlite_store.py`, `mcp_server/storage/git_index_manager.py`, `mcp_server/storage/repository_registry.py`, `mcp_server/storage/store_registry.py`, `mcp_server/storage/multi_repo_manager.py`, `mcp_server/artifacts/artifact_download.py`, `mcp_server/artifacts/publisher.py`, `tests/test_v13_data_storage.py`, `tests/test_git_index_manager.py`, `tests/test_store_registry.py`, `tests/test_registry_concurrency.py`, `tests/test_sqlite_store.py`, `tests/test_history_issue_storage.py`, `tests/test_history_source_metadata.py`, `tests/test_chunker_scheme_recovery.py`, `tests/test_artifact_download.py`, `tests/test_artifact_lifecycle.py`, `tests/test_multi_repo_manager.py`, `tests/test_multi_repo_search.py`
- **Depends on**: SL-0
- **Interfaces provided**: retained-stage, coherent-publication, source-filter-query, legacy-query-admission
- **Interfaces consumed**: staged-context, semantic-leases, exact-backend, batch-provenance, vector-maintenance, freeze-contract (pre-existing), state-contract (pre-existing), safety-contract (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - test: Seed real history/documents, imported vectors, summaries, relationships and outstanding deletion intents; run production rebuild rather than a mock that copies rows.
  - impl: Copy retained state through SQLite snapshot APIs, regenerate derived rows without dropping retained data, preserve valid unchanged summaries and reconstruct FTS/trigrams.
  - impl: Stage full/incremental SQLite and vectors under one generation; hold writer ownership, fence before mutation and publish only after durable validation. Keep old resources for active borrowers.
  - impl: Route artifact restore through verified isolated staging and the same CAS admission; no active SQLite sidecar deletion.
  - test: Inject failures at stage creation, vector writes, metadata persistence, SQLite durability and registry publication, then restart/retry; assert old bytes/vectors unchanged or queries unavailable.
  - impl: Extend source-metadata search to apply query/rank before limit and make legacy cross-repository storage entrypoints obey readiness/generation checks.
  - verify: Run storage, history, registry and artifact tests; leave integration-dependent ECs unchecked until SL-2 enters real dispatcher construction sites.

### SL-2 - Query admission, ignore reconciliation and documentation reducer

- **Scope**: Integrate the staged generation interfaces across actual entrypoints and reconcile all DATA evidence.
- **Owned files**: `mcp_server/dispatcher/dispatcher_enhanced.py`, `mcp_server/dispatcher/cross_repo_coordinator.py`, `mcp_server/core/repo_resolver.py`, `mcp_server/core/ignore_patterns.py`, `mcp_server/core/path_resolver.py`, `mcp_server/health/repository_readiness.py`, `mcp_server/client.py`, `mcp_server/gateway.py`, `mcp_server/cli/tool_handlers.py`, `mcp_server/cli/task_reindex.py`, `mcp_server/cli/bootstrap.py`, `mcp_server/watcher_multi_repo.py`, `mcp_server/watcher/ref_poller.py`, `mcp_server/watcher/sweeper.py`, `mcp_server/watcher/file_watcher.py`, `tests/test_v13_data_queries.py`, `tests/test_v13_data_reconciliation.py`, `tests/test_dispatcher.py`, `tests/test_cross_repo_coordinator.py`, `tests/test_history_search_filters.py`, `tests/test_tool_readiness_fail_closed.py`, `tests/test_tool_handlers_readiness.py`, `tests/test_repository_readiness.py`, `tests/test_ignore_patterns.py`, `tests/test_watcher_sweep.py`, `tests/test_watcher_multi_repo.py`, `tests/test_watcher.py`, `tests/test_sweeper_observability.py`, `tests/test_client.py`, `tests/test_python_client_search.py`, `tests/test_python_client_contract.py`, `tests/test_python_client_indexing.py`, `tests/test_python_client_sources.py`, `tests/test_friction_search_filters.py`, `tests/test_gateway.py`, `tests/test_v13_state.py`, `tests/test_v13_safety.py`, `scripts/installed_runtime_smoke.py`, `scripts/safety_runtime_smoke.py`, `scripts/release_smoke.py`, `scripts/v13_pilot_estimate.py`, `scripts/agent_validation.py`, `pyproject.toml`, `uv.lock`, `docs/contracts/v13-data.md`, `docs/validation/v13/DATA.json`, `docs/status/V13_EXECUTION.md`, `docs/operations/v13-data-generation.md`
- **Depends on**: SL-0, SL-1
- **Interfaces provided**: uniform-query-admission, committed-input-reconciliation, pilot-estimate, data-receipt
- **Interfaces consumed**: staged-context, semantic-leases, exact-backend, batch-provenance, vector-maintenance, retained-stage, coherent-publication, source-filter-query, legacy-query-admission, freeze-contract (pre-existing), state-contract (pre-existing), safety-contract (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - impl: Wire dispatcher staging/leases to the supplied context and retained SQLite mappings; preserve canonical file paths and complete generation cache identity.
  - test: Real STDIO/HTTP/Python/cross-repository no-match and refusal matrix, including branch switches, stale commits, sibling worktrees, SHA-256 Git and generation changes during queries.
  - impl: Use the shared admission contract at every public surface; filtered queries must actually match/rank, not return the first metadata rows.
  - test: Nested ignores, rooted patterns, negations, tracked/excluded secrets, sibling roots and symlinks are filtered before parse/embed; dirty tracked contents never enter committed snapshots.
  - impl: Reconcile committed create/modify/delete/rename with fresh registry state; prune excluded directories and detect overlapping-path hash changes. Watchers do not admit dirty edits.
  - impl: Produce a pure synthetic corpus byte/chunk/token upper-bound estimate for PILOT; no inference requests or claims of measured quality.
  - verify: Update installed fixture commits as required by committed-input policy. Run local gates, installed wheel/container, broad offline suite and all DATA operational controls.
  - impl: Reduce all producer evidence into the DATA contract, per-EC receipt and status; retain controlled-rollout posture and explicit exclusions.

## Execution Notes

Read allowlist: committed source/config/docs/tests, accepted receipts, immutable
audit counterexamples, and synthetic fixtures/process logs created by v13.
Control outputs: active plan, `plans/manifest.json`, and
`.dev-skills/handoffs/codex-plan-phase/**` /
`.dev-skills/handoffs/codex-execute-phase/**`.
Ignored scratch allowlist: `.phase-loop/runs/v13-DATA-*/**`, `build/**`,
`index_it_mcp.egg-info/**`; raw operational logs stay ignored and receipts are
metadata-only. No existing private index, registry, source corpus or env file.
The main thread owns all implementation; no implementation-agent fanout.
The outer phase-loop remains disabled under agent-harness#819.

## Verification

- `uv sync --locked --python 3.12 --extra dev`
- `env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests/test_v13_data_vectors.py tests/test_embedding_provenance.py -q --no-cov`
- `uv run --locked --extra dev python scripts/v13_qdrant_smoke.py --mode file`
- `uv run --locked --extra dev python scripts/v13_qdrant_smoke.py --mode server`
- `uv run --locked --extra dev python scripts/v13_pilot_estimate.py`
- `make agent-gate`
- `make release-smoke-container`
- `env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests/test_git_index_manager.py -m 'not requires_network and not benchmark' -q --no-cov`
- `env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests --ignore=tests/test_git_index_manager.py -m 'not requires_network and not benchmark' -q --no-cov`
- Frontmatter phase suite, formatting on changed Python files and `git diff --check`.

Every operational node has a named timeout and metadata result in the exact-head
runner verification. No in-memory-only substitute for file/server Qdrant, no
provider fixture substitute for later PILOT live provenance, and no health-only
substitute for installed query workflows. Record all skips and failed attempts.

## Acceptance Criteria

- [ ] EC-DATA-1 - proven by `tests/test_v13_data_storage.py` and installed rebuild probes; falsified by lost retained rows/debt or active vector mutation before publication.
- [ ] EC-DATA-2 - proven by `tests/test_v13_data_vectors.py` and both `scripts/v13_qdrant_smoke.py` modes; falsified by truncation at 1000, payload misassociation, a broken live lock, backend fallback or an upsert after provenance drift.
- [ ] EC-DATA-3 - proven by `tests/test_v13_data_queries.py` and installed entrypoint probes; falsified by ready results from an unsupported/stale generation or filtered results that ignore the query.
- [ ] EC-DATA-4 - proven by `tests/test_v13_data_reconciliation.py` and `tests/test_ignore_patterns.py`; falsified by excluded/dirty input reaching embedding admission or missed committed modifications remaining stale after reconciliation.

## Spec Closeout Plan

- schema: `spec_delta_closeout.v1`
- decision: `canonical_spec_update`
- target surfaces: `docs/contracts/v13-data.md`, `docs/operations/v13-data-generation.md`
- evidence paths: `docs/validation/v13/DATA.json`
- redaction posture: `metadata_only`
- downstream handling: PILOT consumes accepted DATA contracts and dry-run estimate; no fleet expansion or release before its operational acceptance.

## External Inputs

Locked Qdrant client is the SDK contract. The disposable server image resolves to
`qdrant/qdrant@sha256:f1c7272cdac52b38c1a0e89313922d940ba50afd90d593a1605dbbc214e66ffb`;
record its reported server version when starting the isolated fixture.
Primary API documentation checked through PMCP Context7:
https://github.com/qdrant/qdrant-client/blob/master/qdrant_client/qdrant_client.py.
GitIgnoreSpec behavior checked through PMCP Context7 at
https://github.com/cpburnz/python-pathspec/blob/master/README.rst.
The lock already contains pathspec 1.0.4; promote it to a direct runtime
dependency if used, without unrelated dependency upgrades.
