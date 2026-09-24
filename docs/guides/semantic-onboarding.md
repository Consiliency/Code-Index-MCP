# Semantic Onboarding

Semantic search readiness is stricter than ordinary indexed search readiness.
A repository can be lexically ready for `search_code` and `symbol_lookup` while
still being semantically not ready. Semantic readiness is fail-closed and is
reported separately from lexical readiness.

Semantic readiness requires all of the following durable local evidence for the
active semantic profile:

- `code_chunks` exist for the indexed repository.
- `chunk_summaries` exist for those chunks.
- `semantic_points` link those chunks to vectors for the active profile.
- the stored vector metadata matches the current profile fingerprint, vector
  dimension, and collection identity.

When any of those checks fail, semantic readiness reports a specific not-ready
state such as `summaries_missing`, `vectors_missing`,
`vector_dimension_mismatch`, `profile_mismatch`, or `semantic_stale`. Lexical
readiness does not imply semantic readiness.

This guide shows how to set up semantic search for Code-Index-MCP with:

- local Qdrant (auto-started via Docker)
- a profile-selected enrichment endpoint for chunk summaries
- a profile-selected embedding endpoint for vectorization

## Quick Start

```bash
python scripts/cli/mcp_cli.py setup semantic
```

The command performs preflight checks and, by default, starts local Qdrant if it is not reachable.
For the active profile, the preflight is fail-closed: it can block semantic
vector writes before `SEMPIPE` if chat reachability, embedding dimension, or
collection shape do not match the configured contract.
When the only blocker is `collection_missing`, the non-dry-run setup path now
reports a `collection bootstrap` outcome and creates the active profile
collection before re-running semantic preflight.

## Where Settings Live

Configuration precedence:

1. CLI flags (`setup semantic --qdrant-url ...`)
2. Environment variables (`.env`)
3. `code-index-mcp.profiles.yaml`
4. `SEMANTIC_PROFILES_JSON`
5. Built-in defaults

> **Tip**: Keep profile-local endpoints in `code-index-mcp.profiles.yaml`. Global
> `OPENAI_API_BASE` remains only a fallback for older flows and should not be the
> primary way to steer `oss_high`.

## Key Environment Variables

- `SEMANTIC_SEARCH_ENABLED=true`
- `SEMANTIC_AUTOSTART_QDRANT=true`
- `SEMANTIC_STRICT_MODE=false`
- `SEMANTIC_PREFLIGHT_TIMEOUT_SECONDS=10`
- `QDRANT_URL=http://localhost:6333`
- `QDRANT_API_KEY=`  # only when the server sets `QDRANT__SERVICE__API_KEY`; unset sends no key
- `QDRANT_COMPOSE_FILE=docker-compose.qdrant.yml`
- `SEMANTIC_ENRICHMENT_BASE_URL=http://ai:8002/v1`
- `SEMANTIC_ENRICHMENT_MODEL=chat`
- `SEMANTIC_EMBEDDING_BASE_URL=http://ai:8001/v1`
- `VLLM_SUMMARIZATION_BASE_URL=http://ai:8002/v1`  # compatibility shim only
- `VLLM_EMBEDDING_BASE_URL=http://ai:8001/v1`  # compatibility shim only
- `OPENAI_API_BASE=http://localhost:8001/v1`  # legacy fallback only
- `OPENAI_API_KEY=vllm-local`
- `VOYAGE_API_KEY`

## CLI Examples

```bash
# Validate only
python scripts/cli/mcp_cli.py setup semantic --dry-run

# Strict mode
python scripts/cli/mcp_cli.py setup semantic --strict

# JSON output for CI
python scripts/cli/mcp_cli.py setup semantic --json

# Select profile
python scripts/cli/mcp_cli.py setup semantic --profile commercial_high
```

## Verify Services

```bash
curl http://localhost:6333/collections
curl http://ai:8002/v1/models   # oss_high enrichment proxy
curl http://ai:8001/v1/models   # oss_high embedding endpoint
```

## What Preflight Proves

`setup semantic` and `mcp-index index check-semantic` now perform a semantic
preflight for the active profile that stays read-only with respect to summaries
and vectors:

- an enrichment chat smoke sends a redacted OpenAI-compatible chat request to
  the selected enrichment endpoint using the configured chat model
- an embedding smoke verifies the returned vector dimension matches the active
  profile
- a Qdrant collection check verifies collection existence, namespace identity,
  vector dimension, and distance metric without creating or mutating the
  collection
- API-key reporting remains metadata-only: env-var name plus presence boolean,
  never the secret value
- when any check fails, the command emits a structured blocker, semantic vector
  writes remain fail-closed until remediation is complete, and the phase stays
  read-only with respect to summaries and vectors
- for `oss_high`, the preflight also distinguishes the configured enrichment
  model identifier from the effective served model actually used for the chat
  smoke; this is how a configured `chat` alias can resolve to a concrete local
  Gemma model without hiding the operator-facing contract
- `setup semantic` also reports a `collection bootstrap` state. Typical values
  are `dry_run`, `created`, `reused`, or `blocked`.

## Default `oss_high` Layout

- Enrichment uses `${SEMANTIC_ENRICHMENT_BASE_URL:http://ai:8002/v1}` with
  `${SEMANTIC_ENRICHMENT_MODEL:chat}`.
- Embeddings use `${SEMANTIC_EMBEDDING_BASE_URL:http://ai:8001/v1}` with
  `Qwen/Qwen3-Embedding-8B`.
- Legacy `VLLM_SUMMARIZATION_BASE_URL` and `VLLM_EMBEDDING_BASE_URL` remain
  accepted as compatibility shims when the newer `SEMANTIC_*` vars are unset.
- `uv sync --locked` installs the default OpenAI-compatible client stack; no extra
  semantic install is required just to reach the local enrichment or embedding
  endpoints.
- When the enrichment endpoint does not literally serve `chat` but advertises a
  single compatible local model, semantic preflight reports both the configured
  model and the effective served model it used.

## Readiness Notes

- `get_status` exposes lexical readiness separately from semantic readiness.
- `uv run mcp-index repository status` is the operator-facing status surface:
  it now prints lexical readiness, semantic readiness, and active-profile
  preflight separately, plus the active profile, active collection, collection
  bootstrap state, and durable semantic evidence counts for summaries, vectors,
  and collection linkage.
- `search_code(semantic=true)` refuses with semantic readiness metadata when
  summaries, vectors, or profile compatibility are missing.
- For a semantically ready registered repository, `search_code(semantic=true)`
  stays on the semantic path end to end. Ready responses expose
  `semantic_source`, `semantic_profile_id`, and `semantic_collection_name`.
- Semantic runtime failures are explicit: `semantic=true` returns
  `code="semantic_search_failed"` with `semantic_fallback_status=failed_runtime`
  instead of silently degrading to lexical or plugin results.
- Explicit lexical queries keep the existing lexical response shape.
- Semantic search remains experimental and provider-aware; do not treat a
  lexically ready repository as semantically query-ready unless the semantic
  readiness surface reports `ready`.
- `docs/status/SEMANTIC_DOGFOOD_REBUILD.md` is the stable rebuild artifact for
- Normalized path reference for contract checks:
  `docs/status/semantic_dogfood_rebuild.md`.
  local dogfood evidence. Use it alongside `repository status` and preflight
  output when you need to confirm semantic readiness after a clean rebuild.
- The current SEMREADYFIX evidence shows the enrichment compatibility repair can
  succeed while semantic dogfood still remains blocked on `collection_missing`;
  the SEMCOLLECT evidence then shows collection bootstrap can succeed while the
  repo is still blocked downstream on summary generation; the SEMSUMFIX
  evidence then shows the direct authoritative summary runtime can recover;
  the SEMSYNCFIX evidence then shows the full-sync path can recover its scoped
  summary selection and retry behavior while still remaining blocked on
  summary throughput; the SEMTHROUGHPUT evidence then shows oversized-file
  profile batching can increase summary coverage while the live force-full
  rebuild still stalls short of a fresh indexed commit; the SEMSTALLFIX
  evidence then shows bounded semantic-stage blocker vocabulary and fail-closed
  full-index closeout without fully clearing the live repo-local stall; the
  SEMIOWAIT evidence then narrows that residual state to a lexical/storage
  blocker, `blocked_file_timeout` on `CHANGELOG.md`, with storage diagnostics
  attached; and the SEMCHANGELOG evidence then shows the bounded changelog
  repair clears that exact file path while exposing `ROADMAP.md` as the next
  exact lexical/storage blocker; the SEMROADMAP evidence then shows the
  bounded roadmap repair clears that exact file path while exposing
  `FINAL_COMPREHENSIVE_MCP_ANALYSIS.md` as the next exact lexical/storage
  blocker; the SEMANALYSIS evidence then shows the bounded final-analysis
  repair clears that exact file path while exposing `AGENTS.md` as the next
  exact lexical/storage blocker; and the SEMAGENTS evidence then shows the
  bounded AGENTS repair clears that exact file path while exposing
  `README.md` as the next exact lexical/storage blocker; and the SEMREADME
  evidence then shows the bounded README repair clears that final lexical
  blocker, restores indexed-commit freshness, and leaves the repo blocked
  downstream on semantic readiness `summaries_missing`. The SEMCLOSEOUT
  evidence then shows the registered-context semantic stage starts persisting
  authoritative summaries on the live repo-local index, and the current
  SEMTIMEOUT evidence shows the dispatcher now uses bounded one-batch passes,
  deeper timeout backoff, deterministic profile-client closure, and smaller
  doc-style fallback prompts. Even with those repairs, the repo still remains
  blocked on repo-wide summary drain before strict vector linkage can start.
  That leaves the repo at `stale_commit` with non-zero `chunk_summaries`,
  zero `semantic_points`, and a hot unsummarized backlog rooted in
  `.claude/agents/lane-executer.md` plus related `.claude/commands/*.md`
  documents. If that later blocker remains live, keep routing execution
  through `SEMTIMEOUT` instead of reusing the older SEMCLOSEOUT handoff. Use
  the report to separate those states.

## Full Reindex Pipeline

In semantic mode, a full `reindex` now runs the build in a fixed order:

1. lexical/chunk persistence
2. authoritative summary generation for the targeted chunks/files
3. semantic vector writes

The semantic vector stage is strict. If authoritative summaries are missing for
targeted chunks, or if semantic preflight says the active profile cannot write
vectors, semantic vector writes are skipped and semantic readiness stays
not-ready even when lexical indexing completed successfully.

`reindex` reports the semantic stage additively. Operators should expect to see
whether summaries were written, whether semantic vectors were written, and
whether the semantic stage was blocked before any vector write or failed after
lexical persistence.

The bounded semantic-stage vocabulary now includes:

- `blocked_missing_summaries`
- `blocked_summary_plateau`
- `blocked_summary_timeout`
- `blocked_preflight`
- `blocked_semantic_batch`
- `blocked_semantic_batch_timeout`

If a live force-full rerun still hangs without surfacing one of those stages,
the remaining blocker is below the semantic-stage accounting layer.

Below-semantic lexical/storage blockers are now reported separately. Operators
should look for:

- `lexical_stage`
- `lexical_files_attempted`
- `lexical_files_completed`
- `last_progress_path`
- `in_flight_path`

When the lower path blocks, storage diagnostics are also reported from
`SQLiteStore.health_check()`:

- `journal_mode`
- `busy_timeout_ms`
- `wal_checkpoint`
- SQLite/WAL/SHM file sizes

Manual summary tools stay separate from the full-sync contract:

- `write_summaries` is an explicit summary-only operation. It can populate
  authoritative summaries, but it does not claim semantic vector success.
- `summarize_sample` is a diagnostic/sample tool for inspecting summary output
  and does not satisfy the full-sync semantic build contract by itself.

## Incremental Mutation Contract

Incremental mutation follows the same ordered semantic contract as full sync:

1. lexical/chunk persistence
2. authoritative summary generation or summary reuse when the stored semantic
   fingerprint still matches
3. strict semantic vector writes

Changed chunks invalidate stale semantic evidence before re-embedding. That
includes authoritative summaries when chunk content or the summary contract
changes, plus vector linkages for stale chunk, subchunk, and file-summary
entries.

Deletes remove both authoritative summaries and semantic vectors for the
affected file. Renames may preserve authoritative chunk summaries only when the
stored profile and prompt fingerprints still match, but the file-summary vector
is rotated through the same summary-first rebuild path so path-sensitive
semantic context does not go stale.

Watcher-triggered repair uses the same summary-first mutation contract as
direct incremental sync. This phase is limited to mutation durability and
watcher repair; it does not change semantic query routing and does not claim
dogfood rebuild evidence.

## Troubleshooting

- Qdrant unreachable
  - run `docker compose -f docker-compose.qdrant.yml up -d qdrant`
  - ensure port `6333` is free
- OpenAI-compatible endpoint unreachable
  - check the selected profile's enrichment or embedding host/port
  - verify `SEMANTIC_ENRICHMENT_BASE_URL` and `SEMANTIC_EMBEDDING_BASE_URL`
- Wrong chat model or blocked semantic writes
  - verify `SEMANTIC_ENRICHMENT_MODEL` matches a model served by the enrichment endpoint
  - rerun `setup semantic --json` to inspect the structured blocker and remediation
  - if the configured `chat` alias resolves to a concrete served model, confirm
    the report shows both the configured model and the effective model
- Collection bootstrap succeeded but summaries are still zero
  - run `uv run mcp-index repository status` and confirm active-profile preflight is `ready`
  - probe `ComprehensiveChunkWriter.process_scope(...)` or the summary path directly
    if semantic readiness remains `summaries_missing`
  - if the batch runtime reports a BAML generator/runtime mismatch, confirm the
    direct authoritative fallback still writes `is_authoritative=1` rows with
    the expected configured model, effective model, and profile metadata
  - if direct probes succeed but `repository sync --force-full` still leaves
    semantic readiness at `summaries_missing`, treat that as a full-sync
    summary throughput blocker rather than a preflight or collection bootstrap
    blocker; the current roadmap follow-up for that state is `SEMTHROUGHPUT`
- Repository status stays `stale_commit` after a long force-full rebuild
  - compare `Current commit` and `Indexed commit` in `uv run mcp-index repository status`
  - if summary counts improved but the indexed commit did not refresh, treat
    that as a force-full completion blocker rather than only a semantic query
    blocker
  - if `search_code(semantic=true)` returns `index_unavailable` with
    `safe_fallback: "native_search"`, the indexed query surface is still
    blocked before semantic-path acceptance can be re-checked
  - if the bounded semantic-stage strings above never appear, read
    `lexical_stage`, `last_progress_path`, and `in_flight_path` from the
    SEMDOGFOOD report before assuming the semantic stage is at fault
  - if storage posture might be involved, inspect `journal_mode`,
    `busy_timeout_ms`, and `wal_checkpoint` from the same report
  - the current SEMREADME evidence shows lexical readiness `ready` with
    semantic readiness `summaries_missing`, so the remaining blocker is in
    semantic closeout rather than a lower-level README timeout
- Voyage provider failing
  - verify `VOYAGE_API_KEY`
- Preflight output
  - `setup semantic --json` reports resolved enrichment and embedding endpoints separately
  - preflight reports collection validation, vector dimension checks, whether it can write semantic vectors, and the collection bootstrap outcome
  - API-key reporting is metadata-only: env-var name plus presence boolean, never the secret value
