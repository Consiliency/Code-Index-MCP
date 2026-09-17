# Comprehensive Code Review: 2026-09-09

## Decision

**Hold fleet-wide indexing.** The repository has substantial working functionality and a large passing test base, but its registry, rebuild, semantic mutation, and distribution boundaries still contain correctness defects. These are primarily Index It MCP responsibilities, not reasons to wait for PMCP implementation.

Use a small, disposable lexical acceptance corpus while fixing the blockers. Do not spend fleet embedding resources or treat current indexes as durable authoritative state until the publication and lifecycle gates below pass. This is a review and recommended implementation sequence, not an executed remediation roadmap or a new release approval.

## Review Baseline and Scope

- Reviewed commit: `d4e09c54232fdddd76c393c4027d0f4c01fbd5a4` on `main`, matching the observed `origin/main`.
- Current distribution version: `1.4.0`. GitHub release and PyPI latest both report this version; historical version-ordering concerns should not be carried forward as current findings.
- Live GitHub checks found no open PRs or issues in `Consiliency/Code-Index-MCP`. These findings have not been filed as issues.
- Scope: repository identity/readiness, registry persistence, SQLite storage/migrations/pooling, staged indexing, watcher behavior, lexical/semantic/cross-repository search, embeddings/reranking, artifact transfer/attestation, plugin sandboxing, STDIO and HTTP boundaries, packaging/containers, tests, release automation, configuration, and operational documentation.
- Method: targeted source and caller tracing across these areas, locked-environment tests, real temporary Git/SQLite/Qdrant counterexamples, actual STDIO process execution, wheel build and inspection, and read-only external release checks. This is not a claim that every line or every supported platform was manually reviewed.
- No production source fixes, live index rebuilds, paid inference, merge, publication, or remote issue mutations were performed. The pre-existing untracked `.mcp-index/` was preserved.

Severity: P1 should block the affected rollout surface; P2 is a material bug or hardening gap with a narrower exposure. "Reproduced" means a concrete expected-behavior check failed at this head. "Source-confirmed" means the control flow or artifact was inspected, with the stated integration limits.

## Prioritized Findings

### C01 [P1] Unregistration is resurrected on disk

**Location:** `mcp_server/storage/repository_registry.py:234`, `:269`.

`unregister()` deletes the in-memory entry, then `save()` merges that snapshot with the old on-disk dictionary. An absent in-memory key never removes its on-disk counterpart. The method reports success but a fresh registry reload restores the repository. This breaks removal and registered-worktree replacement, and can restore a removed repository to later watcher startup.

**Evidence:** `test_unregister_survives_reload` registers and unregisters a real temporary repository, then finds the repository ID still in the JSON file.

**Fix/acceptance:** represent deletion as an explicit transaction, not an omission from a snapshot. Verify removal from a second process and after restart, including watcher detachment.

### C02 [P1] Registry snapshots overwrite concurrent provenance and hide external changes

**Location:** `mcp_server/storage/repository_registry.py:217`, `:429`, `:458`; `mcp_server/client.py:100`.

The file lock serializes writes, but every writer replaces entire rows from its potentially stale in-memory snapshot. A priority update can erase another process's `index_publication_pending` fence or indexed-commit update. Existing registry readers do not reload external changes. Even query preparation can write registry state through `update_git_state()`. Consequently, a CLI and a long-lived PMCP-managed server can disagree about registrations and readiness while both appear healthy.

**Evidence:** `test_stale_writer_preserves_new_provenance` loses a pending-publication marker after a second instance updates priority. `test_server_observes_external_registry_changes` demonstrates the stale reader.

**Fix/acceptance:** use transactional field mutations and revision-aware reads; a transactional SQLite registry is one possible implementation, not a prerequisite. Exercise two real processes, unrelated field updates, registration/removal, and an in-progress publication fence.

### C03 [P1] Failed registry writes are acknowledged as durable publication

**Location:** `mcp_server/storage/repository_registry.py:240`, `:581`; `mcp_server/storage/git_index_manager.py:679`.

`save()` logs persistence exceptions without propagating failure. Callers such as `update_indexed_commit()` still return the new commit; the staged publisher interprets that return as durable success. The same pattern undermines its pre-replacement pending marker. A failed disk write can therefore be followed by a successful publication response without the corresponding durable state.

**Evidence:** `test_registry_does_not_ack_failed_persistence` injects an atomic-replace failure and observes acknowledgment of the new commit while disk retains the old one.

**Fix/acceptance:** commit metadata only after verified persistence; propagate write failures and leave the repository unavailable. Inject write, replace, and crash-boundary failures and verify restart behavior.

### C04 [P1] Database replacement leaves other readers on an old index generation

**Location:** `mcp_server/storage/store_registry.py:57`; `mcp_server/storage/git_index_manager.py:671`; `mcp_server/indexing/lock_registry.py:10`.

Cached stores are keyed only by repository ID. Staged publication closes handles in its own runtime, unlinks active SQLite sidecars, and replaces the database pathname. Another server's pooled connections still reference the old database. Readiness can inspect the new path while the query uses the old cached store. The indexing lock is a process-local `RLock`, not cross-process writer exclusion.

**Evidence:** `test_cached_store_reopens_after_replacement` warms a resolver/store, replaces the SQLite file, obtains another ready resolution, and still reads the old file row. This proves stale reads; cross-process WAL corruption was not deliberately induced.

**Fix/acceptance:** introduce an index generation identity, generation-aware handle acquisition, cross-process writer admission, and reader draining. Do not unlink WAL/SHM files while unrelated processes can hold the database. Cover concurrent query/rebuild, watcher contexts, process restart, and replacement failure.

### C05 [P1] Real staged rebuilds discard imported history and other non-file state

**Location:** `mcp_server/storage/git_index_manager.py:636`, `:1131`; `tests/test_chunker_scheme_recovery.py:190`.

A rebuild starts from an empty SQLite database and invokes directory indexing. There is no transfer of existing imported history/document rows or pending vector cleanup records. In-place migration helpers that preserve those rows are not called by this production path. The recovery test's fake dispatcher itself writes a history row into the new database, so that test does not demonstrate preservation by the real dispatcher.

**Evidence:** `test_staged_rebuild_preserves_synthetic_history` uses the real `EnhancedDispatcher`; a seeded synthetic history chunk disappears after a successful full rebuild. Loss of other imported state and cleanup debt follows the same fresh-stage path but was not independently reproduced for every table.

**Fix/acceptance:** specify the durable data ownership of every table, copy retained data into the stage, and preserve/reconcile cleanup debt. Test real code plus imported history/document rows and outstanding vector deletions across both success and interruption.

### C06 [P1] The SQLite staging boundary does not stage semantic writes

**Location:** `mcp_server/dispatcher/dispatcher_enhanced.py:1683`; `mcp_server/utils/semantic_indexer_registry.py:25`; `mcp_server/storage/git_index_manager.py:643`.

The stage has a new SQLite store but the same repository ID. Semantic lookup calls `registry.get(ctx.repo_id)`, whose cache and storage path refer to the repository's active semantic index. A rebuild can mutate that semantic state before the SQLite stage passes validation. A later SQLite failure cannot roll those semantic effects back.

**Evidence:** source-confirmed ownership mismatch. No remote Qdrant failure-injection campaign was run, so the exact partial-publication outcomes remain an integration-test requirement.

**Fix/acceptance:** stage semantic collections/metadata by generation and publish one verified generation binding for SQLite, vectors, commit, and embedding profile. Inject failure after embedding, after upsert, before SQLite replacement, and before provenance publication.

### C07 [P1] Semantic deletion and movement call an API absent from the locked Qdrant client

**Location:** `mcp_server/utils/semantic_indexer.py:3728`, `:3790`, `:3891`, `:3930`.

The locked `qdrant-client==1.17.1` no longer exposes `QdrantClient.search`. The main search path has compatibility handling, but file deletion, movement, content-hash lookup, and deletion marking still call the removed method. File deletion raises and marks Qdrant unavailable, breaking index maintenance. Some helper queries also cap matches at 1,000, which is unsuitable for complete file cleanup.

**Evidence:** `test_locked_qdrant_file_removal_api` creates an actual in-memory Qdrant collection and point, then observes `AttributeError` wrapped by `remove_file()`.

**Fix/acceptance:** use the locked SDK consistently. Prefer filter-based deletion or paginated scrolling for complete lifecycle mutations, not a similarity top-k query. Test delete, rename, cleanup retry, and files with more than 1,000 chunks against the real SDK.

### C08 [P1] Embedding batches can change model provenance after the first write probe

**Location:** `mcp_server/utils/semantic_indexer.py:558`, `:1449`, `:2657`.

`_prepare_for_writes()` performs a provenance probe once and short-circuits later calls. `_embed_texts()` validates dimensions/cardinality but discards the embedding response's provenance. A local endpoint can switch to a different same-dimensional model after the probe and write incompatible vectors into the existing collection. Query-time validation is stronger than this write path.

**Evidence:** `test_batch_embedding_revalidates_provenance` accepts a valid initial probe followed by a batch from a different same-dimensional model, without rejecting it.

**Fix/acceptance:** compare every batch's provenance against the generation's admitted embedding identity before upsert; validate the complete batch before accepting any rows. Test model/profile drift, endpoint restart, partial responses, retries, and concurrent writes.

### C09 [P1] Omitting attestation metadata bypasses enforce mode

**Location:** `mcp_server/artifacts/artifact_download.py:268`.

The downloader invokes attestation verification only if downloaded metadata contains a truthy `attestation_url`. An archive and self-consistent metadata/checksum without that field bypass verification even with `MCP_ATTESTATION_MODE=enforce`. Checksums alone do not authenticate the artifact producer.

**Evidence:** `test_enforce_rejects_unsigned_archive` constructs an otherwise accepted unsigned archive; extraction succeeds in enforce mode.

**Fix/acceptance:** require attestation from trusted local policy before extraction, independently of optional untrusted metadata. Reject absent/empty bundles and invalid modes; test unsigned, tampered, wrong-repository, and valid signed artifacts.

### C10 [P1] Published wheels omit SQL migrations and silently take a different schema path

**Location:** `pyproject.toml:162`; `mcp_server/storage/sqlite_store.py:730`; `scripts/release_smoke.py:41`.

Package data does not include `storage/migrations/*.sql`. Both a newly built wheel and the actual published `1.4.0` wheel lack those files. `_run_migrations()` returns immediately when the directory is absent, also skipping the subsequent repository-identity column migration. The release wheel smoke checks entrypoint help, not installed-artifact schema/index/query behavior.

**Evidence:** wheel ZIP inspection plus a fresh database created from extracted wheel code outside the checkout. It reports schema version 2 and lacks `repositories.tracked_branch` and `repositories.git_common_dir`. Source execution includes the extra migration path. Core table creation still occurs; this finding is schema/upgrade divergence, not a claim that every fresh-wheel query fails.

**Fix/acceptance:** package migration resources explicitly and use a tested resource loader. Install the built wheel outside the source tree and test fresh databases, supported upgrades, register/index/query, and process restart. Reconcile migration versions and validate other declared runtime resources in the same audit.

### C11 [P1] The production container does not install Git in its final stage

**Location:** `docker/dockerfiles/Dockerfile.production:29`.

Git is installed only in the build stage. The final stage installs `curl` and `libstdc++6`; copying the virtual environment does not carry Git into the runtime. Repository identity, branch/commit readiness, change detection, and reindexing depend on Git subprocesses. An HTTP health endpoint can still pass without those workflows functioning.

**Evidence:** source-confirmed container recipe defect; a new container build/run was not performed in this audit.

**Fix/acceptance:** install the runtime executables required by the declared surface. Run the final image as its non-root user against a mounted temporary Git repository and prove registration, indexing, querying, branch refusal, and restart. Include `gh` only if this image is meant to perform artifact operations that require it.

### C12 [P1] Legacy cross-repository search bypasses the readiness contract

**Location:** `mcp_server/storage/multi_repo_manager.py:409`; `mcp_server/dispatcher/cross_repo_coordinator.py:778`.

These paths filter repositories for activity and open stores directly instead of requiring the centralized readiness decision. A registered repository on a non-tracked branch can return indexed hits even though the classifier says `wrong_branch`. This affects the optional cross-repository/Python API paths; it is not a claim that the primary single-repository STDIO handler lacks its existing guard.

**Evidence:** `test_legacy_cross_repo_search_refuses_wrong_branch` switches a real repository off its tracked branch, confirms the refusal classification, and still gets the old symbol from `MultiRepositoryManager.search_symbol()`.

**Fix/acceptance:** route every public query surface through the same generation/readiness admission. Test empty matches separately from unavailable indexes across STDIO, HTTP, the Python client, and optional cross-repository routes.

### C13 [P2] Index artifact signing relies on an unsupported GitHub CLI operation

**Location:** `mcp_server/artifacts/attestation.py:32`, `:80`.

`attest()` invokes `gh attestation sign`; the installed CLI exposes only `download`, `trusted-root`, and `verify`. The prerequisite check also looks for a literal `attestations:write` string in `gh auth status --show-token`. Merely detecting the parent command does not prove signing support. This is the index-artifact signing path, distinct from the newer container-signing workflows.

**Evidence:** current CLI help plus the actual invocation in source. Live authenticated signing tests were not enabled; no token-bearing command was executed for this review.

**Fix/acceptance:** choose and test a supported signing owner/mechanism, then verify its exact output with the downloader. Remove unnecessary token-display requests. Keep expensive testing local; any necessary hosted attestation step should be narrow and explicitly owned. Treat this as a blocker before enabling artifact sharing, not a reason to weaken verification.

### C14 [P2] Sandbox capability restrictions miss ordinary filesystem and SQLite APIs

**Location:** `mcp_server/sandbox/caps_apply.py:153`, `:191`.

The filesystem guard patches `builtins.open`, but `Path.read_text()` uses a different opening path. SQLite read-only handling does not rewrite/reject an existing `file:...?mode=rw` URI. These bypasses undermine the documented capability contract. The current design is defense in depth for trusted/first-party plugins, not a sufficient hostile-code security boundary.

**Evidence:** `test_sandbox_pathlib_enforces_read_roots` first confirms `builtins.open` rejects a file outside all admitted roots, then reads it through `Path.read_text`. `test_sandbox_readonly_rejects_rw_uri` creates a table through a read-write URI under the read-only capability.

**Fix/acceptance:** enforce the intended restrictions across supported I/O forms and make the trust model explicit. For hostile plugins, use OS-level containment rather than Python monkeypatches. Cover path-like objects, URIs, inherited descriptors, and subprocess escape routes without claiming untested containment.

### C15 [P2] SIGTERM cleans up services but does not terminate the real STDIO server

**Location:** `mcp_server/cli/stdio_runner.py:1558`, `:1606`; `tests/integration/test_sigterm_shutdown.py:220`.

The installed signal handler schedules service cleanup, but never cancels/stops `server.run()` or closes its transport. With stdin still open, the process remains alive after all cleanup completes. This matters for PMCP process replacement and can leave a server accepting input after its dependencies have been closed. Existing parent-death tests exercise a miniature supervisor parent, not this real server lifecycle.

**Evidence:** `test_real_stdio_server_exits_on_sigterm` starts the actual module with isolated paths, waits for startup, sends SIGTERM, and times out waiting for exit. Logs show all cleanup completed about one second after the signal; the process was still alive at five seconds. Only the audit-owned process was then killed.

**Fix/acceptance:** stop request admission, cancel/close the serving task/transport, and drain services with one bounded shutdown contract. Test SIGTERM, SIGINT, EOF, in-flight requests, and repeated signals against the actual installed entrypoint.

### C16 [P2] Pool shutdown leaves already-waiting borrowers blocked forever

**Location:** `mcp_server/storage/connection_pool.py:33`.

`acquire()` checks `_closed` before an unbounded `Queue.get()`. `close_all()` drains the queue without waking waiters; an outstanding borrower subsequently closes its connection rather than returning it. A request already waiting for a connection can therefore never finish during eviction or shutdown.

**Evidence:** `test_pool_shutdown_unblocks_waiter` fills the pool, observes a second borrower entering the queue wait, closes the pool, and verifies that the borrower does not wake after the held connection is released.

**Fix/acceptance:** use a shutdown-aware condition/queue protocol with clear borrower lifetime rules. Verify every waiter exits and no late construction can repopulate a closed registry.

### C17 [P2] SHA-256 Git repositories reuse cached branch/commit readiness

**Location:** `mcp_server/health/repository_readiness.py:249`.

Live Git validation is conditional on a cached 40-character hexadecimal commit. Git SHA-256 object IDs are 64 characters, so those repositories take the cached-value path. A branch switch can continue to classify as ready until another component refreshes the metadata.

**Evidence:** `test_sha256_readiness_checks_live_branch` creates a real SHA-256 Git repository, indexes its tracked branch, switches branches, and still obtains ready classification.

**Fix/acceptance:** do not use SHA-1 length to decide whether live identity is necessary. Test SHA-1, SHA-256, detached HEAD, missing Git metadata, and external branch changes through each query surface.

### C18 [P2] Source metadata filters bypass the search query

**Location:** `mcp_server/dispatcher/dispatcher_enhanced.py:1871`; `mcp_server/storage/sqlite_store.py:2220`.

Supplying source/history/friction filters takes an early branch that fetches metadata-matching chunks and returns without applying `query`. An absent search term can produce apparently relevant results; limiting the metadata rows before text matching would also hide later matches.

**Evidence:** `test_source_metadata_filter_still_applies_query` runs the real dispatcher/store with a term absent from the content and still receives a matching-source row.

**Fix/acceptance:** intersect metadata predicates with the lexical/semantic query before ranking and limit. Test no-match, later matching rows, combined filters, and both search modes.

### C19 [P2] Default request logging includes raw query/tool arguments

**Location:** `mcp_server/cli/stdio_runner.py:1340`; `mcp_server/storage/multi_repo_manager.py:587`.

STDIO logs non-handshake tool arguments at INFO; cross-repository code search logs the full query. Queries can contain proprietary code, repository paths, or accidentally included secrets. The separate handshake redaction does not protect these paths.

**Evidence:** source-confirmed default logging statements; no sensitive query was used to demonstrate leakage.

**Fix/acceptance:** log tool names, request IDs, counts, and durations by default. Make any content-level debug logging explicit and redacted. Test that sentinel query content never appears in normal logs or error diagnostics.

### C20 [P2] STDIO starts a separate unauthenticated metrics listener on all interfaces

**Location:** `mcp_server/cli/stdio_runner.py:1525`; `mcp_server/metrics/prometheus_exporter.py:357`.

Each STDIO server starts the exporter, normally on port 9090. `start_http_server` receives no address/auth configuration and defaults to all interfaces. Protecting the HTTP gateway's metrics route does not protect this separate listener. Multiple per-client servers also contend for the same default port.

**Evidence:** source-confirmed startup and exporter arguments, plus the installed exporter API's bind default. No externally reachable metrics endpoint was probed.

**Fix/acceptance:** choose one explicit exporter ownership model, default to loopback or disabled, and require configured protection for remote exposure. Test bind scope and concurrent MCP instances.

### C21 [P2] Main's test contract is red and several tests do not exercise their claimed boundary

**Location:** `tests/docs/test_p8_historical_sweep.py:20`, `:90`; `tests/test_workflow_action_pins.py:16`, `:89`; `scripts/release_smoke.py:41`.

Two historical-document tests compute deleted files relative to `main..HEAD`; after merge that set is empty while the historical triage retains four deleted entries. The workflow census also omits `sign-published-image.yml`. These are real baseline failures, not runtime regressions. Separately, source-checkout smoke, mocked history reconstruction, and miniature-parent shutdown tests leave installed-artifact and process-boundary bugs undetected.

**Evidence:** the broad suite has three remaining genuine failures after two environment-specific SDK smoke failures were rerun successfully. See verification below.

**Fix/acceptance:** make historical inventory assertions independent of the current branch comparison, update the intentionally approved workflow census without weakening pin/cost policy, and add real boundary tests. Do not simply loosen assertions to obtain green CI.

## Additional Gaps and Refinements

These are bounded follow-up work, not evidence that every optional feature is broken.

| Area | Observed gap and location | Recommended treatment |
| --- | --- | --- |
| Git ignore fidelity and inference cost | `core/ignore_patterns.py:49` explicitly handles only root-level ignore files; Git negation/nested rules are incomplete. Subdirectory walks can lack repository-root rules. | Adopt a proven Git-compatible matcher or Git's own tracked/ignored-file policy. Test nested monorepos, generated trees, exclusions, and dry-run byte/chunk estimates before inference. |
| Watcher recovery | `watcher/sweeper.py:176` identifies creates/deletes/renames but does not compare hashes for paths present in both snapshots, despite hashing them. | Add a lost-modification-event test and implement content-change reconciliation if working-tree edits are in scope. Otherwise narrow the recovery claim to committed/default-branch behavior. Prune excluded directories before descent. |
| Cache generation | Gateway symbol cache at `gateway.py:1490` lacks a generation key. `indexer/reranker.py:215` keys on query plus only the first ten paths/lines. | Include repository/generation/profile and complete candidate identity where relevant. Test changed content at identical locations, changed candidates beyond ten, and replacement from another process. |
| Admin auth lifecycle | `security/auth_manager.py:244` keeps users, refresh tokens, sessions, lockouts, and rate limits in memory; the production server recycles workers. | Either clearly constrain this to a local ephemeral admin surface or persist the required state. Prove restart and multi-worker semantics before shared HTTP deployment. |
| Proxy trust | `security/security_middleware.py:85` accepts client-supplied `X-Forwarded-For` for rate limiting without a trusted-proxy boundary. | Honor forwarded identities only from trusted peers; test direct-client spoofing and deployed proxy topology. |
| Migration idempotence | `storage/sqlite_store.py:751` swallows a duplicate-column exception for an entire SQL script, potentially skipping later statements and its version record. | Make each supported migration transactional and explicitly idempotent. Compare full schemas for fresh install and every supported upgrade origin. |
| Readiness cost | `health/repository_readiness.py:162` performs `PRAGMA quick_check` when file/WAL inspection identity changes. | Benchmark concurrent queries during indexing at realistic repository sizes; avoid turning every publication/change into repeated full-database work. Retain fail-closed corruption handling. |
| Platform claims | Registry imports POSIX `fcntl` directly; this audit ran Linux/Python 3.12 only. | State supported deployment hosts explicitly and add platform tests before claiming equivalent Windows behavior. |
| BAML generation | `baml_src/generators.baml:4` declares 0.220.0; the lock selects baml-py 0.221.0 and the dependency floor permits later versions. | Align generator/runtime intentionally and add regeneration/drift checks. No provider-backed BAML generation/evaluation was run here. |
| Runtime ownership and maintainability | Dispatcher, semantic indexer, SQLite store, and gateway are large modules; optional legacy managers create independent registries/configuration paths. | Consolidate state ownership while fixing the relevant defects, then extract bounded components behind existing interfaces. Avoid a whole-repository rewrite. |
| Quality/support documentation | Nested agent guidance contains contradictory production/stub claims; configured coverage floor is 35%, not the 80% described in guidance. Default pytest selection excludes integration/slow tests. | Make `docs/SUPPORT_MATRIX.md` and actual acceptance commands authoritative. Record exclusions and measured coverage; do not substitute a target percentage or language-plugin inventory for exercised support. |
| Local environment | Existing checkout `.venv` advertised index-it-mcp 1.2.0 with older chunker/language-pack dependencies. | Use the lock and an explicit supported Python for local/PMCP provisioning. The review used a separate clean environment and did not overwrite the existing one. |

## Recommended Implementation Sequence

These are proposed stages with acceptance gates, not a replacement for the repository's canonical phased-roadmap planning process. Keep changes in reviewable PRs and use the existing local CI convention to avoid unnecessary hosted Actions cost.

### Stage 1: Restore Distribution and Test Truth

Address C10, C11, and C21. Establish installed-wheel and final-container acceptance fixtures before using them as delivery gates. Repair the migration resource/schema path and baseline tests. Reconcile supported runtimes, BAML version policy, and stale operational guidance where they affect these tests.

**Exit:** deterministic local tests are green; an isolated wheel creates and upgrades the intended schema; the final image can index/query a real mounted Git repository as its configured user. A passing `/health` or `--help` is not sufficient.

### Stage 2: Make Registry and Publication Durable Across Processes

Address C01-C04 and C16 together around one registry mutation/publication contract. Define generation identity, pending/committed visibility, writer admission, handle lifetime, and failure reporting. Do not add more independently cached registry abstractions.

**Exit:** two real server/CLI processes agree on registration, deletion, branch state, and committed index generation. Crashes and injected persistence failures cannot expose ready metadata for unpublished data or leave permanent pool waiters.

### Stage 3: Preserve Data and Make Semantic Maintenance Correct

Address C05-C08 and C12/C17/C18; include generation-aware cache behavior and ignore-policy acceptance. Make SQLite/vector publication coherent and retain imported content and cleanup debt. Exercise the actual locked Qdrant SDK and model-provenance contract.

**Exit:** rebuild, delete, rename, retry, branch switch, and interruption retain the right data and refuse the wrong generation on every query surface. Per-batch provenance drift is rejected before writing. A minimal pinned local-endpoint corpus passes before any fleet-scale inference.

### Stage 4: Close Security and Process-Lifecycle Gaps

Address C09, C13-C15, and C19-C20. This work can begin alongside the durability stages if ownership is kept separate, but the applicable gates must pass before pilot promotion. Decide whether shared HTTP/auth and arbitrary third-party plugins are actually supported deployment modes; do not silently imply they are.

**Exit:** unsigned artifacts are refused in enforce mode; the supported signer/verifier pair works; actual STDIO processes exit cleanly; normal logs omit query content; metrics exposure is explicit; sandbox claims match tested enforcement.

### Stage 5: Prove PMCP Provisioning and Then Expand Carefully

This audit did not inspect the current PMCP checkout or re-run its installed app/browser flows, so it cannot certify current cross-repository compatibility. Use PMCP as the entrypoint for the acceptance run rather than manually bypassing its provisioning.

**Index It MCP owns:** all C01-C21 fixes, package/runtime resources, durable registry/index state, readiness semantics, inference validation, lifecycle behavior, and truthful diagnostics.

**PMCP owns or jointly verifies:** exact released runtime selection, entrypoint/arguments, one agreed registry location, allowed roots, optional handshake secret delivery, embedding profile and local endpoint configuration, startup/restart/termination policy, and user-visible handling of `index_unavailable`/`native_search`. These are acceptance requirements, not newly verified PMCP defects.

**Pilot acceptance:**

1. Provision the built candidate through PMCP into a clean environment; confirm imported module paths, version, resources, and configuration without printing secrets.
2. Use two small unrelated temporary repositories, plus a sibling worktree. Register/unregister while the client is running and verify behavior after restart.
3. Exercise lexical hit, true no-match, unsupported worktree, wrong branch, stale commit, and failed reindex outcomes through actual client calls.
4. Exercise create/modify/delete/rename/commit/rebuild with queries in flight, including interrupted publication and cleanup retry.
5. Use a tiny explicitly budgeted corpus on the pinned local embedding endpoint. Record model/profile provenance and resource usage; test failure/drift without indexing the fleet.
6. Verify SIGTERM, EOF, client reconnect, and repeated provisioning do not leave duplicate processes, stale handles, or metric-port conflicts.
7. Test the PMCP/app UI and the HTTP admin workflows actually intended to ship, including errors and refusal states, using browser interactions and console checks. API unit tests alone do not certify these UIs.
8. Review the exact candidate with the requested four-model panel before merge: Fable, Sol, Grok, and the agreed fourth reviewer. Verify the live model identities; reconcile findings against the exact final head rather than carrying approvals across code changes.
9. Merge only after acceptance and reconciliation, bump the version, publish, then repeat installed-artifact acceptance against the registry-delivered artifact and expand in measured repository cohorts.

## Verification and Reproduction

### Existing Tests

- Focused readiness, fail-closed tool behavior, chunker identity/recovery/contract, gateway auth, and artifact-attestation tests: **140 passed, 3 skipped** in 11.49 seconds.
- Broad offline-oriented run: **2,943 passed, 154 skipped, 5 failed** in 91.62 seconds. It included integration/slow tests through an explicit marker override, but excluded network/benchmark cases and ran the Git integration module separately.
- Two broad-run failures were SDK smoke tests that require `<checkout>/.venv/bin/python`. Linking the disposable worktree to the clean review environment and rerunning them produced **2 passed** in 14.33 seconds. These are not counted as product defects.
- Remaining baseline failures: `tests/docs/test_p8_historical_sweep.py::test_triage_log_row_count_matches_union`, `::test_triage_log_paths_match_union`, and `tests/test_workflow_action_pins.py::test_workflow_pinning_does_not_expand_triggers_or_hosted_jobs`.
- Separate `tests/test_git_integration.py` run with isolated environment behavior and signing disabled for fixture commits: **9 passed** in 3.88 seconds.
- The entire broad suite was not rerun after the SDK environment adjustment. Coverage collection was disabled for these audit runs; no coverage percentage is claimed.

### New Counterexamples

The standalone [counterexample corpus](review-evidence-2026-09-09/test_counterexamples.py) contains **16 expected-correctness tests that all fail on the reviewed head**. This is deliberate evidence of unhandled cases, separate from the existing suite's failures. The first exploratory run of the STDIO case inherited an invalid machine path; the retained test isolates that configuration. The filesystem test also includes a positive denial control before demonstrating the alternate-API bypass.

Run explicitly from the checkout in the locked review environment:

```sh
env PYTHONPATH="$PWD" SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 \
  /tmp/index-it-review-20260909-py312/bin/python -m pytest \
  docs/status/review-evidence-2026-09-09/test_counterexamples.py \
  -q --tb=short -o addopts=''
```

The last scratch-path run completed with **16 failed** in 25.07 seconds; running the retained corpus from its documented location independently reproduced **16 failed** in 13.45 seconds. Temporary repositories, databases, provider-response fixtures, an in-memory Qdrant client, and an audit-owned STDIO subprocess were used. No actual user repository was indexed or deleted. The retained corpus is outside normal `tests/` discovery so it does not silently turn these review assertions into mandatory tests before remediation.

### Build and Release Evidence

- `uv sync --locked --extra dev --python 3.12` installed the review environment at `/tmp/index-it-review-20260909-py312`: Python 3.12.12, index-it-mcp 1.4.0, treesitter-chunker 4.0.0, qdrant-client 1.17.1, mcp 1.27.0, baml-py 0.221.0.
- Isolated `uv build --wheel` succeeded. An earlier no-isolation build attempt lacked the `wheel` build dependency; it was not treated as a product failure.
- Published GitHub wheel SHA-256: `bfd0ba83f314dc3f4e18925f1ae80d5c54e9601088792fcddfd0039c77d2f4fe`.
- Locally built wheel SHA-256: `f03940935f9ff3c8553a9b315d8d0d89b3e7e93a5ce74728d2b9a0e0b40b3e02`.
- Both wheels omit the SQL migration payload. Different build hashes are recorded for provenance, not asserted to be a reproducibility defect.
- Current release: [Consiliency/Code-Index-MCP v1.4.0](https://github.com/Consiliency/Code-Index-MCP/releases/tag/v1.4.0), published 2026-07-19 at 15:22:14 UTC, with wheel, sdist, and SBOM assets. Release existence is not proof of fleet acceptance or of a historical review panel's exact composition.

### Local Audit Artifacts and Limits

- Disposable exact-head worktree: `/mnt/workspace/worktrees/Code-Index-MCP-review-20260909`.
- Logs: `/tmp/index-it-review-20260909-{baseline,broad,sdk-rerun,git-integration,repros,retained-repros,build}.log`.
- Wheel artifacts: `/tmp/index-it-review-20260909-dist/` and `/tmp/index-it-review-20260909-published/`; extracted-wheel check under `/tmp/index-it-review-20260909-wheel/`.
- Existing checkout environment and `.mcp-index/` were left intact. Scratch artifacts were retained for inspection, not destructively cleaned.
- Not executed: full lint/type/security-tool campaign, all language/platform combinations, live authenticated artifact signing, remote Qdrant publication interruption, paid inference, a new production-container run, or live PMCP/app/browser acceptance. These remain explicit gates where applicable, not implied successes.

## Bottom Line

The appropriate path is targeted hardening, not a rewrite and not immediate fleet indexing. Restore installed-artifact fidelity, fix transactional state and generation ownership, prove semantic/rebuild correctness, close security and shutdown gaps, and then run a small PMCP-provisioned acceptance cohort. The existing passing suite is useful, but it must be extended at these real process and persistence boundaries before spending on broad indexing.
