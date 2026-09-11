---
phase_loop_plan_version: 1
phase: STATE
roadmap: specs/phase-plans-v13.md
roadmap_sha256: 178b8328d8e7dc76ddc0804d7b72d3ccddb55e23577a3d52cb7cbd70d5fd5308
automation:
  suite_command: "env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests/test_v13_state.py tests/test_repository_registry.py tests/test_repository_registry_commits.py tests/test_registry_concurrency.py tests/test_store_registry.py tests/test_sqlite_pool.py tests/test_git_index_manager.py tests/test_watcher_multi_repo.py tests/test_repository_readiness.py tests/test_python_client_search.py tests/test_python_client_contract.py tests/test_gateway.py tests/test_tool_handlers_readiness.py tests/test_endpoint_reranker.py tests/test_semantic_indexer_registry.py tests/test_mcptasks_reindex.py tests/test_indexing_lock.py tests/test_reindex_resume.py tests/test_chunker_scheme_recovery.py tests/test_dispatcher_toctou.py tests/test_friction_search_filters.py tests/test_history_search_filters.py -q --no-cov"
---

# STATE: Durable Registry And Index Generations

## Context

Consume accepted IF-0-DIST-1 from docs/validation/v13/DIST.json before execution.
Implement C01-C04/C16 and R03 under the immutable v13 freeze contract. All lanes
are serial in the existing isolated worktree. No live indexes, private corpora,
inference, attestation dispatch, version publication or fleet promotion.

## Interface Freeze Gates

- [ ] IF-0-STATE-1 - Durable registry mutation, cross-process writer admission, generation-aware readers and shutdown-aware pool pass real-process boundary tests.

## Frozen Interfaces

The registry remains the single JSON state authority, with existing public
method names and RepositoryInfo compatibility. Mutations hold the process-shared
registry lock through reload, field mutation or deletion, fsync, atomic replace
and directory fsync. No stale whole-row merge. Reads observe external revisions.
Malformed registry or persistence failures propagate, never acknowledge success.

One cross-process writer per repository, reentrant within a thread, covers
mutation and generation publication. A generation binds physical SQLite identity,
commit/branch and profile metadata; readers bind and recheck it before results.
Use generation-specific physical files where replacement would leave external
handles alive. Old generations are retained until safe cleanup can be proved;
never unlink active WAL/SHM files, force locks, or claim eager reclamation.

StoreRegistry validates registration and generation before returning a store;
retired pools reject new borrowers, wake waiters and close returning connections.
Nested store operations must not deadlock exhausted pools. Watchers resolve fresh
contexts; cache keys include generation/profile and full candidate content.
Query-unavailable/native-search vocabulary is unchanged. DATA will implement
vector staging and complete cross-surface readiness parity on this foundation.

## Lane Index & Dependencies

SL-0 — Registry durability
  Depends on: (none)
  Blocks: SL-1, SL-2
  Parallel-safe: no

SL-1 — Generation and reader lifetime
  Depends on: SL-0
  Blocks: SL-2
  Parallel-safe: no

SL-2 — Documentation and acceptance reducer
  Depends on: SL-0, SL-1
  Blocks: (none)
  Parallel-safe: no

## Lanes

### SL-0 - Registry durability

- **Scope**: Atomic registry changes, deletion and visibility, preserving existing registration support.
- **Owned files**: `mcp_server/storage/repository_registry.py`, `mcp_server/storage/multi_repo_manager.py`, `tests/test_repository_registry.py`, `tests/test_repository_registry_commits.py`, `tests/test_registry_concurrency.py`, `tests/test_v13_state.py`
- **Interfaces provided**: durable-registry
- **Interfaces consumed**: freeze-contract (pre-existing), dist-receipt (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - test: Reproduce deletion resurrection, field loss, stale readers and failed-write acknowledgment using real processes and synthetic repositories.
  - impl: Consolidate locking/reload/persist into existing registry ownership. Apply explicit mutations to fresh disk state; persist deletion; refresh reads; keep concurrent registration/worktree checks inside admission.
  - impl: Preserve public save compatibility without allowing stale snapshots to erase concurrent provenance. Malformed JSON must not become an empty successful registry.
  - impl: Provide one atomic generation/provenance publication method with additive RepositoryInfo generation metadata for the downstream lane.
  - impl: Flush and fsync file and parent before acknowledging success. Keep old local state on failed persistence and propagate failure; query-triggered Git updates preserve publication fences.
  - verify: Test register/unregister, stale priority/Git updates during publication, write/replace/fsync failure, restart visibility and existing legacy-id migration.

### SL-1 - Generation and reader lifetime

- **Scope**: Single-writer publication, current handles, wakeable pools, watcher/cache consistency.
- **Owned files**: `mcp_server/storage/store_registry.py`, `mcp_server/storage/connection_pool.py`, `mcp_server/storage/sqlite_store.py`, `mcp_server/storage/git_index_manager.py`, `mcp_server/indexing/lock_registry.py`, `mcp_server/indexing/__init__.py`, `mcp_server/indexing/incremental_indexer.py`, `mcp_server/core/repo_context.py`, `mcp_server/core/repo_resolver.py`, `mcp_server/health/repository_readiness.py`, `mcp_server/watcher_multi_repo.py`, `mcp_server/client.py`, `mcp_server/gateway.py`, `mcp_server/cli/stdio_runner.py`, `mcp_server/cli/tool_handlers.py`, `mcp_server/dispatcher/dispatcher_enhanced.py`, `mcp_server/indexer/reranker.py`, `mcp_server/cache/query_cache.py`, `tests/test_store_registry.py`, `tests/test_sqlite_pool.py`, `tests/test_indexing_lock.py`, `tests/test_reindex_resume.py`, `tests/test_repository_commands.py`, `tests/test_git_index_manager.py`, `tests/test_watcher_multi_repo.py`, `tests/test_repository_readiness.py`, `tests/test_python_client_search.py`, `tests/test_python_client_contract.py`, `tests/test_gateway.py`, `tests/test_tool_handlers_readiness.py`, `tests/test_endpoint_reranker.py`, `tests/test_cross_repo_reranker.py`, `mcp_server/cli/task_reindex.py`, `tests/test_mcptasks_reindex.py`, `tests/test_chunker_scheme_recovery.py`, `tests/test_dispatcher_toctou.py`, `mcp_server/utils/semantic_indexer_registry.py`, `tests/test_semantic_indexer_registry.py`
- **Depends on**: SL-0
- **Interfaces provided**: generation-admission, draining-pools, generation-caches
- **Interfaces consumed**: durable-registry, freeze-contract (pre-existing), dist-receipt (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - test: Real-process competing writers, an outstanding reader during publication, external replacement, unregister while cached, and interrupted publication leave coherent old results or explicit unavailable.
  - impl: Remove the eager indexing-package import cycle exposed by a fresh writer-lock process; keep exported names lazily compatible. Apply shared writer admission to the direct incremental entrypoint as well as manager and watcher calls.
  - impl: Extend existing indexing lock authority to cross-process admission. Preserve thread reentrancy and lock ordering; use bounded waits where appropriate. Scope direct Python/HTTP/STDIO and task reindex mutations through the same admission and pending-generation fence.
  - impl: Publish generation binding and provenance durably after validation; retain old physical files while other handles may exist. Preserve a durable pending fence when publication cannot finish.
  - impl: Compare live registration/path/file identity on store acquisition; close retired stores without stranding borrowers. Fix exhausted-pool shutdown and nested borrow behavior with deterministic events, not sleep-based assertions.
  - impl: Resolve current watcher contexts before mutation. Bind query admission/cache state to generation; recheck before emitting results after a concurrent change.
  - impl: Include all rerank candidates, their content and profile/config identity in deterministic cache keys. Test changed content at the same line and changed candidate eleven.
  - verify: Run focused real-process failure injection, current query/readiness/registry/pool tests, installed wheel workflow and broad offline baseline.

### SL-2 - Documentation and acceptance reducer

- **Scope**: Record actual state contracts and reduce all three ECs against evidence.
- **No-doc-change decision**: README and CHANGELOG release updates belong to PREP; this phase updates the internal state contract and execution status, without changing support or release claims.
- **Owned files**: `docs/contracts/v13-state.md`, `docs/validation/v13/STATE.json`, `docs/status/V13_EXECUTION.md`, `Makefile`
- **Depends on**: SL-0, SL-1
- **Interfaces provided**: state-receipt
- **Interfaces consumed**: durable-registry, generation-admission, draining-pools, generation-caches
- **Parallel-safe**: no
- **Tasks**:
  - test: Require named evidence for every owned finding and EC, including subprocess outcomes and crash boundaries.
  - impl: Document active-generation lookup, legacy compatibility, retained-resource cleanup limits, pending failures, and consumer expectations. Add focused regressions to local gate selection.
  - verify: Use standalone stamped verification, explicit exclusions, owned-file audit and a clean checkpoint before emitting IF-0-STATE-1. No dependent phase starts on partial success.

## Execution Notes

Main thread executes serially; no implementation worker fanout. Keep the original
checkout and all unrelated worktrees untouched. Read allowlist: committed source,
config, tests and docs; accepted FREEZE/DIST receipts; immutable v13 freeze and
roadmap/review/audit inputs. Historical
`docs/status/review-evidence-2026-09-09/test_counterexamples.py` is read-only:
promote controls into normal tests, do not edit the evidence to turn it green.
Only synthetic temporary databases/repositories/processes created by these tests
may be mutated or cleaned by their owning lifecycle. No live registry/index,
secret env file, private raw-data or deployment state is allowed.

Additional SL-1 ownership discovered during boundary verification:
`mcp_server/cli/task_reindex.py`, `tests/test_mcptasks_reindex.py`,
`tests/test_chunker_scheme_recovery.py`, `tests/test_dispatcher_toctou.py`,
`mcp_server/utils/semantic_indexer_registry.py`, `tests/test_semantic_indexer_registry.py`.
These cover task writer admission and existing regressions that assumed in-place
publication or constructed a watcher without its registered path.
Semantic caches reject changed bindings without breaking an existing Qdrant
handle; DATA owns lease-aware semantic generation replacement. Legacy artifact
restore into the active directory is disabled pending DATA's staged restore.

Control outputs: active plan, `plans/manifest.json`,
`.dev-skills/handoffs/codex-plan-phase/**`,
`.dev-skills/handoffs/codex-execute-phase/**`.
Ignored verification outputs under `.phase-loop/runs/v13-STATE-*/` are allowed
metadata-only. Packaging scratch `build/**` and `index_it_mcp.egg-info/**`
is allowed and never staged. Skill reflections use runtime-resolved paths.

## Verification

- `uv sync --locked --python 3.12 --extra dev`
- `env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests/test_v13_state.py tests/test_registry_concurrency.py tests/test_store_registry.py tests/test_sqlite_pool.py -q --no-cov`
- `make agent-gate`
- `env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests -m 'not requires_network and not benchmark' -q --no-cov`
- `git diff --check`

Use the frontmatter suite command as the required phase suite. Each test has the
unchanged 300-second cap; outer groups are bounded and record partial results.
Explicitly exclude network/provider and benchmarks from the offline baseline;
real local multiprocess/SQLite cases and Git integration are included.
No inference, remote Qdrant service, browser or signing acceptance is claimed.

## Acceptance Criteria

- [ ] EC-STATE-1 - proven by `tests/test_v13_state.py` and `tests/test_registry_concurrency.py`; falsified by lost fields, resurrected deletion, stale reads or acknowledged failed fsync/replace after restart.
- [ ] EC-STATE-2 - proven by `tests/test_v13_state.py`, `tests/test_store_registry.py`, `tests/test_sqlite_pool.py` and installed `make release-smoke`; falsified by concurrent writer entry, stale-generation dispatch, blocked waiters or active sidecar removal.
- [ ] EC-STATE-3 - proven by watcher/client/gateway/reranker controls in the suite and `tests/test_v13_state.py`; falsified by stale context/cache hits after generation/profile/content change or ready state after incomplete publication.

## Spec Closeout Plan

- schema: `spec_delta_closeout.v1`
- decision: `canonical_spec_update`
- target surfaces: `docs/contracts/v13-state.md`, `docs/status/V13_EXECUTION.md`
- evidence paths: `docs/validation/v13/STATE.json`
- redaction posture: `metadata_only`
- downstream handling: SAFETY next; DATA consumes generation-admission and must preserve it.
