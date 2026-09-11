# V13 Data Contract

Status: implementation candidate; acceptance requires the DATA receipt and its
exact-commit verification artifact. This extends, and does not relax, the frozen
table ownership, STATE admission and SAFETY trust contracts.

## Generation Publication

Full rebuild, committed incremental reconciliation, scoped repair and verified
artifact restore use one registered writer. The writer fences readers, snapshots
the active SQLite database with SQLite backup, and works in an unpublished
generation. It reads regular committed Git blobs, not mutable working-tree bytes.
Nested Git ignore rules, rooted patterns, negations, index exclusions and file
size policy apply before ordinary blobs are copied, parsed or embedded. Dirty
tracked files, wrong branches and unsupported worktrees refuse admission.

Imported history/documents and their relationships, operator configuration and
collection-qualified cleanup debt survive rebuild. Code-derived rows are
regenerated; unchanged content/profile summaries can be retained, with the strict
semantic path checking its summary fingerprint contract. FTS and trigrams are
rebuilt from authoritative rows. No active SQLite WAL/SHM is unlinked.

Semantic state belongs to the staged context's generation and profile, not an
independent lookup of the active registry. File-backed vectors and profile
metadata live alongside that generation; server collections have generation
identity. Retained vectors are copied to the new collection. Cleanup only drains
debt owned by that stage after an acknowledged deletion, never debt targeting an
admitted old collection. Finalization validates mappings and provenance before
the durable registry compare-and-swap publishes the SQLite/vector generation.

Failures before publication leave old bytes untouched and queries fenced or
still coherently admitted. Commit/registration/generation drift cannot publish
the stage. A successful retry creates a new stage; it does not repair correctness
by deleting active files. Old generations are retained; automatic reclamation is
not part of this contract.

## Vector Ownership And Provenance

Exactly one explicit memory, filesystem or server backend is selected. A live
file lock is never removed and server failure never selects another backend.
Generation-aware leases deny new borrowers during retirement and drain existing
borrowers before close. Metadata persistence errors prevent success.

Provenance-capable responses are checked per batch, including arity, ordering,
status, dimensions, model/revision, normalization and fingerprint. Same-dimensional
drift after a matching probe is rejected before upsert, including after restart.
Maintenance uses paginated scroll and acknowledged SDK writes, preserving
vector/payload association across deletion, move, interruption and retry.
New hash-derived vector point IDs fit SQLite's positive signed 63-bit integer
range and cannot use the provenance sentinel. This does not alter upstream
source chunk IDs or rewrite retained vector mappings.

## Portable Artifacts

Export snapshots SQLite including committed WAL contents, filters excluded rows,
checks foreign keys and rebuilds search structures. Host-local caches, cleanup
debt and absolute repository/backend paths are not portable artifact content.
Mapped vectors stream in bounded batches into `semantic-vectors.v1` JSONL; live
Qdrant files are not copied. A backend path escaping its owning directory through
a symlink or parent traversal is refused. Export requires quiescent file ownership
or an explicitly borrowed semantic indexer; it cannot break another process's lock.

Enforce-mode verification precedes extraction. Registered restore cannot bypass
identity/freshness checks with an unsafe flag or extract into the active index.
It merges into a staged committed snapshot, retains local imported data, validates
all mapped vectors and provenance, and uses the same publication admission.
Artifact header SHA alone is not recovery provenance; the admitted registry is.

Publisher and workspace/CLI callers select the registered generation and its
actual GitHub origin. Prepared archives remain available after failed publication.
Successful uploads record `published`; failures do not stamp a new published
commit. Local indexing without a configured publisher records `local_only`.
No routine hosted CI, new signing job or private index upload is implied.

## Query Contract

STDIO, HTTP, Python and legacy cross-repository paths check current registration,
worktree, branch, live SHA-1/SHA-256 commit and generation before returning data.
`master` remains `master`; branch identity is not an alias for `main`. A generation
change during a query refuses the result. Non-ready paths return
`index_unavailable` and `safe_fallback: native_search`; HTTP uses its documented
503 refusal. A ready empty result means no match, not unavailable.

Source metadata filters apply the actual query and ranking before the limit.
Semantic readiness resolves the same generation-specific metadata directory and
collection as the resource owner. Missing or incomplete generation metadata
cannot be satisfied by an unrelated legacy repo-root metadata file.
Lexical queries rank the complete matching metadata set with FTS. Semantic
queries restrict Qdrant to those source chunk identities before vector ranking,
including all derived subchunks. Deleted vectors and provenance sentinel points
are excluded before top-k. Empty candidate sets make no embedding request;
unavailable or failed semantic queries cannot substitute lexical results.
Cross-repository aggregate failures remain visible and do not become a successful
partial or empty response. The legacy coordinator's unimplemented semantic mode
returns unavailable, never an unlabeled lexical fallback. Python clients close
only the runtime resources they own. Reindex recovery runs off the async event
loop while retaining mutation ownership through cancellation.

## Evidence Limits

DATA uses synthetic repositories and deterministic embedding responses, including
real file-backed Qdrant and a pinned disposable server. It does not prove local
inference quality, PMCP provisioning, browser workflows or fleet readiness.
The pure PILOT estimate makes zero inference calls. Installed lifecycle probes
still allow the existing 15-second in-flight fail-stop; PILOT must meet the frozen
five-second threshold, not inherit a waiver. Controlled rollout remains the limit.
