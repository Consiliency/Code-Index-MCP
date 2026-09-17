# V13 State Contract

Status: accepted IF-0-STATE-1 on candidate `645421f`; see the STATE receipt.

## Registry Authority

The JSON registry remains authoritative. Public readers reload under a shared
process lock and return independent snapshots. Writers hold the exclusive lock
across reload, field mutation/deletion, temporary-file flush/fsync, atomic replace
and parent-directory fsync. Failed persistence propagates. A failed directory
fsync may leave visible but unacknowledged bytes; restart reads the actual revision.
Malformed JSON is an error, never an empty successful registry.

Registration adds an opaque `registration_id`. A new registration of a removed
repository gets a different identity. `save()` compatibility applies only local
field deltas to the same registration, preserving externally changed fields and
deletions. Public mutators are preferred over editing internal dictionaries.

`begin_generation_mutation` fences readers using registration/generation compare
and swap. `publish_generation` atomically records physical path, generation,
commit, branch and profile, then clears pending/error state. Git refresh does not
clear publication fences. Legacy provenance stamping rotates the generation and
supports expected-registration checks for admitted writers.

## Writers And Readers

Production manager, scoped API/task reindex, watcher and incremental entrypoints
use the registered checkout's `.mcp-index/writer.lock`. The lock combines a
thread-reentrant lock with process-shared flock and bounded admission waits; it
is never removed to break contention. Calls without a checkout retain the
legacy thread-only utility contract.

Rebuilds write a sibling staging database, validate durable rows, and publish a
unique `generations/<generation>.db` path. They do not replace an active database
or unlink its WAL/SHM. Old files and held reader transactions survive. No eager
generation reclamation is promised. Publication interruption leaves the old
registration fenced until a validated rebuild succeeds. Failed in-place updates
remain unavailable; recovery never deletes live SQLite/Qdrant data to simulate
rollback.

StoreRegistry binds stores to registration, generation, profile, checkout,
physical path and file identity. A changed binding retires the cached pool.
Outstanding connections finish; returned connections close, new borrowers fail,
and waiting borrowers wake on shutdown. In-flight constructors cannot repopulate
a shutdown epoch. Nested store calls use the same connection and savepoints, so
they neither exhaust a one-connection pool nor commit an outer transaction.

Replacing SQLite bytes externally under a live filename is unsupported. A cached
physical-identity mismatch fences the repository instead of adopting potentially
incoherent WAL state. A fresh runtime cannot certify such manual replacement;
operators must rebuild, not transplant active database files.

## Admission And Caches

RepoContext is a snapshot, not a live registry reference. Query surfaces recheck
current registration/generation before returning results. A transition returns
`index_unavailable` with `safe_fallback: native_search`, including when a retired
pool interrupts a query. Scoped mutations fence reads, retain existing commit
provenance, and publish a new logical generation only on clean completion.

Watchers refresh contexts and recheck branches inside writer admission. Registry
polling adds/removes observers without resurrecting external deletions. Query and
graph cache identities include the admitted generation. Semantic cache identities
also include profile configuration; reranker keys cover the complete query,
configuration, all candidate content and requested result count.
Rerank callers that cannot supply a generation binding run without cache reuse;
this includes legacy cross-repository candidates until DATA supplies admission
bindings. Missing identity never becomes a reusable cross-generation cache key.

## Downstream Requirements

STATE does not implement vector generation publication, artifact staging, full
query-surface policy parity or changed-file policy. Those remain DATA acceptance
requirements. Semantic cache binding changes are unavailable until the owning
runtime is retired; they do not close potentially borrowed Qdrant handles.
Legacy artifact extraction into an active directory is disabled, with local
rebuild retained, until DATA supplies staged restore. SAFETY owns termination of
timed-out workers before cancellation can be treated as complete.

No live registry, index or private repository was migrated by these changes.
Evidence comes from synthetic local fixtures. Exact-candidate acceptance is
recorded in `docs/validation/v13/STATE.json`: all seven stamped commands, locked
environment refresh and the 433-test expanded phase suite passed. The broad
offline suite passed 3,025 tests, with 153 skips and 31 deselections; those
exclusions are not passing coverage. The installed wheel passed two SDK sessions.
