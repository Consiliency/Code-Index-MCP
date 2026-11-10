# Changelog

All notable changes to Code-Index-MCP will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

[Unreleased]: # placeholder for post-rc5 work

### Changed

- **Per-language degradation when a tree-sitter grammar is unavailable**
  (Consiliency/Code-Index-MCP#117). Staged full and incremental builds leave a
  language whose grammar cannot load offline out of the generation whole and
  publish the other languages, instead of failing the whole repository.
  Readiness stays `ready` and gains an additive `coverage` object; while it is
  `degraded`, `search_code` returns its envelope with `coverage`, and
  `symbol_lookup` not-found responses carry it. A degraded generation is never
  uploaded as an artifact. Once the grammars load, the next sync or ref-poller
  tick indexes only the left-out languages' files, leaving every other
  language's chunk ids unchanged. Hosts with every grammar prefetched are
  unaffected.
- A missing `tsx` grammar no longer lets `.tsx` files parse with the
  TypeScript grammar, and a missing `javascript` grammar is reported as
  `grammar_unavailable` for `.ts` files instead of an unrelated plugin error.
- **Rollback note:** the left-out languages are recorded in the generation's
  `grammar_degradation` index-config marker. Binaries older than this change
  ignore the marker and serve a degraded generation as complete until its next
  rebuild.

## [1.6.0] - 2026-11-09

Prepared minor release: the port to the `mcp` 2.x SDK (Code-Index-MCP#124,
with #128, #131 and #127 in #132 and its re-check fixes in #135), so
`index-it-mcp` co-installs with PMCP 3.x; the watcher sweep, incremental sync
and generation-retention fixes (Code-Index-MCP#114 and #111 in #129, follow-ups
in #130). This section does not imply publication; protected-main dispatch and
independent delivered-artifact acceptance are separate gates.

### Changed

**Port to the `mcp` 2.x SDK** (Consiliency/Code-Index-MCP#124)

- The STDIO server runs on the `mcp` 2.x SDK. The requirement is now
  `mcp>=2.2,<2.4` (locked at 2.3.0), so `index-it-mcp` installs in the same
  environment as PMCP 3.x (`mcp>=2.2,<3`) and `phase-loop-runtime==0.7.30`
  with a clean `uv pip check`. `jsonschema` is now a direct dependency.
- The cap below 2.4 is deliberate. The Tasks runtime depends on the SDK's
  `Server.middleware`, which mcp marks provisional ("may change in a 2.x minor
  release"). Each new mcp minor must be re-tested before the cap is widened.
  At startup the server also checks the middleware contract it needs and fails
  with an actionable message if an installed mcp lacks it.
- Clients see the same tools, input and output schemas, results, error codes
  (except for methods the server does not serve, below) and readiness-aware
  `index_unavailable` responses. The port keeps two
  contracts the 1.x SDK's `call_tool` decorator provided and the 2.x lowlevel
  server dropped: arguments are validated against each tool's `inputSchema`
  (`Input validation error: ...` results), and a failing tool returns an
  `isError` result instead of a JSON-RPC error.
- The 2.x SDK removed its experimental Tasks runtime. A repo-owned runtime keeps
  task-augmented `reindex` and `write_summaries`, the `tasks` capability, and
  `tasks/get`, `tasks/result`, `tasks/list` and `tasks/cancel`, with the 1.x
  results and error codes, on connections negotiated at a handshake-era
  protocol revision (2025-11-25 and earlier).
- Clients that probe `server/discover` now negotiate protocol revision
  2026-07-28 instead of falling back to `initialize` at 2025-11-25. Claude
  Code 2.1.x does this. On that revision, which has no Tasks, `tools/list`
  omits `execution`, task metadata on `tools/call` is ignored (the call runs
  synchronously), `tasks/*` answer `-32601`, and results add `resultType` and
  a `serverInfo` `_meta` stamp. The official Python SDK's `ClientSession`, the
  1.x SDK client and the PMCP 3.x gateway negotiate 2025-11-25.
- Every tool's `outputSchema` now carries the top-level `"type": "object"` the
  MCP schema requires and the 2.x SDK enforces. Each schema's `oneOf` branches
  were already objects, so the same results validate.
- Requests for methods the server does not serve now answer `-32601` (Method
  not found) with the method name in `data`, per JSON-RPC; the 1.x SDK
  answered unknown methods with `-32602`.
- The PMCP pilot's client uses `streamable_http_client` with an `httpx2`
  client, because the 2.x SDK removed `streamablehttp_client`.

- Task retention is bounded. A task's `ttl` is clamped to between one minute
  and one hour, a task without one gets one hour, and the effective value is
  returned. The one-minute floor means a `tasks/result` woken by the completion
  always finds the result, even for `ttl: 0`. Expiry starts when a task
  finishes, so a running task is never purged. At most 256 tasks are kept: the
  oldest finished tasks are evicted first, but never a task that still has a
  `tasks/result` waiter or that finished within the last minute. When every
  slot holds a running, awaited or just-finished task, a new task is refused,
  and the client gets that reason ("Too many tasks (256 running or awaiting
  retrieval); retry after one finishes") rather than an opaque
  `"details": "ValueError"`. (Consiliency/Code-Index-MCP#132 re-check)
- Concurrent `tasks/result` requests for the same task are all answered when
  the task finishes. Previously, as in the 1.x SDK runtime, a second request
  left the first one waiting forever. Deleting or expiring a task now wakes its
  waiters, which get `-32602`.
- On the POSIX STDIO transport, a line that is not a valid JSON-RPC message
  gets a JSON-RPC error instead of no reply (the 2.x transport drops it
  silently): `-32700` (Parse error, `id` null) for invalid JSON, and `-32600`
  (Invalid Request) for other JSON, echoing a string or integer `id`. A
  malformed *response* (`result` or `error` with no `method`) is dropped with a
  debug log instead, because responses are never answered and its `id` belongs
  to the server's own requests. Blank lines are ignored. A warning logs only the
  size and the error code. On Windows the SDK's own STDIO reader is used, and
  malformed lines are still dropped silently.
- On protocol 2026-07-28, which has no server-to-client requests, summarization
  no longer attempts MCP sampling for every chunk. It logs once and uses the
  next summarization path.

### Fixed

**Watcher sweep and generation retention** (Consiliency/Code-Index-MCP#114)

- The watcher sweep counted files the indexer never stores (disabled-by-default
  languages such as YAML, shell and JSON, and unsupported extensions) as
  "created" drift. On this repository that was 112 files, so every sweep
  (default 60 minutes) forced a full rebuild that could not clear the drift,
  left readiness at `missing_provenance` during each rebuild, and published a
  new generation each time. The sweep, the full index walk and single-file
  indexing now share one admission predicate
  (`mcp_server/indexing/admission.py`, `EnhancedDispatcher.index_admission_reason`):
  the same extension, plugin-enablement and exclusion rules. Right after a full
  rebuild the sweep reports no drift. Rows a rebuild deliberately retains
  (imported documents and history) are no longer reported as deleted.
- Drift that survives a *successful* reconciliation is not reconciled again
  until the tracked tree or the drift itself changes; the sweep logs it once.
  A reconciliation that failed, was refused or raised is retried on the next
  sweep, then with exponential backoff capped at 16 sweeps.
- Bounded generation retention. Each publication and each failed rebuild is
  recorded in `generations/generation-history.json`, and only recorded
  generations can be pruned: after a publication, recorded generations beyond
  the live one plus the `MCP_INDEX_GENERATION_RETENTION` (default 2, minimum 1)
  most recently live previous generations, and recorded failed generations
  beyond the newest one. Never removed: generations without a record
  (everything from before the upgrade, or after a lost history file), any
  generation a registry entry references, generations pinned with a
  `generations/<id>.keep` file, and any generation a reader may still hold (its
  SQLite sidecars exist, or a process has it open). The semantic directory and
  sidecars are removed before the database, and the history file is fsynced.
  Quarantine copies and server-side Qdrant collections are not touched.
- Sandboxed plugin workers kept per-file in-memory state keyed by each
  rebuild's staging path, which grew by about 11 MB per rebuild of a 29-file
  fixture. A staged build now releases the repository's plugin workers while
  queries are still fenced; they restart on next use.

**Incremental sync of non-indexable files** (Consiliency/Code-Index-MCP#111)

- Incremental sync failed with `Staged rebuild failed (RuntimeError)` when a
  commit touched a file in a language with no enabled plugin, such as a
  workflow `.yml`. Incremental sync now asks the shared admission predicate,
  as the full walk does: files it rejects are ignored, an indexed file that is
  no longer admitted is deleted, and a rename to a non-indexable path (for
  example `notes.md` to `notes.txt`) deletes the old row. The result matches a
  full rebuild. A file the predicate admits whose plugin fails to resolve (for
  example a sandbox worker that does not start) is a failure in both the
  incremental and the full path, so the previous generation is kept.
- A failed staged rebuild's stored `last_sync_error` and the `repository sync`
  output now include the message of the rebuild's own refusal and a bounded
  summary of the per-file errors (at most five, checkout and staging paths
  removed). Text from other exceptions is still reported by type only.
- The shared secret redaction used for these errors and rerank diagnostics now
  also covers GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`,
  `github_pat_`), AWS access key ids, Slack tokens, PEM private keys, URL
  credentials and `*secret*`/`*token*`/`*password*` key-value pairs.

**`serverInfo.version`** (Consiliency/Code-Index-MCP#128)

- The STDIO `initialize` reply's `serverInfo.version` is the `index-it-mcp`
  version (`mcp_server.__version__`), not the `mcp` library version. The same
  version is stamped on 2026-07-28 results.

**Grammar prefetch retries** (Consiliency/Code-Index-MCP#131)

- `mcp-index grammars prefetch` retries transient download failures from the
  language pack's GitHub release assets: HTTP 5xx and 429, connection resets
  and timeouts. It makes at most 5 attempts, with equal-jitter exponential
  backoff (4 s doubling, capped at 60 s); no retry starts that would sleep past
  a 120 s budget. Refused connections, DNS lookup failures and HTTP 408 are also
  retried. Other HTTP 4xx such as 404, checksum mismatches and unrecognized
  failures fail on the first attempt. Retries and the final error name only the URL (without credentials,
  query or fragment) and the status. CI, Release Automation and the production
  Dockerfile all prefetch through this command.

**PMCP pilot grammar cache** (Consiliency/Code-Index-MCP#127)

- `scripts/v13_pmcp_pilot.py` now gives its fixtures and network-denied sandbox
  workers a prefetched grammar cache. `prepare` prefetches into an explicit
  cache, then binds it in the manifest: `--grammar-cache`, else the caller's
  `TREE_SITTER_LANGUAGE_PACK_CACHE_DIR`, else `<root>/grammar-cache`. Every
  mode checks the cache offline before it starts. The fixture, server,
  registration and lifecycle environments get
  `TREE_SITTER_LANGUAGE_PACK_CACHE_DIR`, plus a manifest URL that refuses
  downloads, so offline mode's `reindex` no longer fails with
  `grammar_unavailable`.

### Upgrade notes
- **`mcp` 2.x is required.** This release requires `mcp>=2.2,<2.4` (locked at
  2.3.0). Drop any `mcp<2` pin installed alongside `index-it-mcp` (the
  workaround for `1.4.1` and `1.5.0`, and what `1.5.1` itself requires): it
  makes the install unresolvable. Code that imports the `mcp` SDK in the same
  environment must use the 2.x API.
- **Co-install with PMCP 3.x.** `index-it-mcp` now installs in the same
  environment as PMCP 3.x (`mcp>=2.2,<3`) and `phase-loop-runtime==0.7.30`
  with a clean `uv pip check`. `1.5.1` could not share an environment with
  PMCP 3.x.
- **Claude Code negotiates protocol 2026-07-28.** Claude Code 2.1.x probes
  `server/discover` and negotiates 2026-07-28, which has no Tasks: task metadata
  on `tools/call` is ignored, the call runs synchronously and `tasks/*` answer
  `-32601`. The tools, their schemas and their results do not change.
  Task-augmented `reindex` and `write_summaries` remain available to clients
  that negotiate 2025-11-25 or earlier (the official Python SDK `ClientSession`,
  the 1.x SDK client and the PMCP 3.x gateway). The official Python SDK 2.x has
  no Tasks client; its `ClientSession` validates every `tools/call` result as a
  `CallToolResult`, so task clients must speak the 2025-11-25 wire directly.
- **The `mcp` cap moves one minor at a time.** The cap below 2.4 is deliberate:
  the Tasks runtime depends on the SDK's provisional `Server.middleware`. Each
  new `mcp` minor will be re-tested before the cap is widened to it.
- **Generation retention is automatic.** After each publication only the live
  generation, the `MCP_INDEX_GENERATION_RETENTION` (default 2) most recent
  previous ones and the newest failed one are kept. Generations that exist
  before the upgrade are never pruned automatically; remove them by hand once
  they are no longer needed. Only generations published or failed after the
  upgrade count towards retention.
- After deploying, hosts that worked around Consiliency/Code-Index-MCP#114 with
  a long `MCP_WATCHER_SWEEP_MINUTES` (for example 1440) can return to the
  default.
- PMCP pilot manifests prepared before this change lack `grammar_cache`. Run
  `prepare` again.

### Known follow-ups
- Consiliency/Code-Index-MCP#133: move the PMCP pilot from pmcp 2.7.3 to 3.x and
  re-run offline and rehearsal acceptance.
- Consiliency/Code-Index-MCP#134: delete the unported legacy scripts
  (`patch_mcp_server.py`, `scripts/cli` servers).
- Consiliency/Code-Index-MCP#121: plan a first semantic build and retire legacy
  Qdrant collections.
- Consiliency/Code-Index-MCP#117: per-language degradation when a tree-sitter
  grammar is unavailable.

## [1.5.1] - 2026-11-08

Prepared corrective release for published `1.5.0` and `1.4.1`: cap `mcp` below
2 so a plain install, with no separate `mcp` constraint, can start STDIO.
This section does not imply publication; protected-main dispatch and
independent delivered-artifact acceptance are separate gates.

### Fixed
- **Plain installs could not start STDIO.** `1.5.0` and `1.4.1` declare
  `mcp>=1.0.0` with no upper bound, so `pip install index-it-mcp==1.5.0`
  resolves `mcp` 2.x, and `index-it-mcp stdio` fails at import with
  `ImportError: cannot import name 'McpError' from 'mcp.shared.exceptions'`.
  Release smoke and the PMCP pilot install under `uv.lock` (`mcp` 1.27.0), so
  neither caught it. The requirement is now `mcp>=1.27,<2`; the locked version
  stays 1.27.0.

### Added
- `scripts/release_smoke.py --unconstrained`, run by `make release-smoke` and
  so by the CI agent gate and Release Automation. It installs the built (or
  delivered) wheel into a fresh Python 3.12 venv with no lock constraints, so
  the newest versions the wheel allows resolve. It then checks
  `index-it-mcp --version`, prefetches grammars into an explicit
  `TREE_SITTER_LANGUAGE_PACK_CACHE_DIR`, and runs an MCP initialize and
  tools/list exchange over raw JSON-RPC, then the lexical STDIO queries. It
  fails on `1.5.0`'s requirements (`mcp` 2.3.0) and passes with the cap
  (`mcp` 1.30.0).

### Upgrade notes
- **Do not install `1.5.0` or `1.4.1` without constraining `mcp` below 2**,
  even as `index-it-mcp==1.5.0`. Install `1.5.1`. Hosts that
  must stay on `1.4.1` (for example glibc older than 2.34) add `"mcp<2"` to the
  same install command. After `1.5.1` is published and accepted, the owner plans
  to yank `1.5.0` and `1.4.1`.
- **No co-install with `mcp` 2.x.** Environments that need `mcp` 2.x, such as
  PMCP 3.x, cannot install `1.5.1` in the same environment. Run it in its own
  environment until the port lands.
- No index, schema or configuration changes from `1.5.0`; its upgrade notes
  still apply when upgrading from `1.4.x`.

### Known follow-ups
- Consiliency/Code-Index-MCP#124: port the STDIO server to `mcp` 2.x and lift
  the cap.

## [1.5.0] - 2026-11-08

Prepared minor release for Code-Index-MCP#115 and Code-Index-MCP#119: the
treesitter-chunker 6 / tree-sitter 0.26 parser-stack upgrade and the
baml-bridge 0.23.0 alignment, so `index-it-mcp` can share one environment with
`phase-loop-runtime` 0.7.30. This section does not imply publication;
protected-main dispatch and independent delivered-artifact acceptance are
separate gates.

### Changed
- `baml-bridge` and the BAML toolchain move from 0.20.1 to 0.23.0, matching
  `phase-loop-runtime` 0.7.29+ so both can share one environment. `baml_sdk/` is
  regenerated (Consiliency/Code-Index-MCP#119).
- baml-bridge 0.21+ records every BAML call under `~/.baml/btel` and uploads the
  recordings to Boundary's cloud when `BOUNDARY_API_KEY` or a saved Boundary
  login exists. Recording and upload stay **off by default**: the generated SDK
  bakes `off`, so unset, blank and whitespace values of the new
  `MCP_BAML_TELEMETRY` variable mean off, whatever imports `baml_sdk` first.
  `MCP_BAML_TELEMETRY=low|medium|high` opts in. That uploads prompts containing
  source code whenever a Boundary key or login is present. Upstream's
  `BAML_TELEMETRY` is ignored. See `docs/SUPPORT_MATRIX.md`.
- LLM calls now time out after 5 minutes in total, or 10 seconds to connect; they
  had no limit before. On the single-file summarization path, a stalled provider
  can take up to 5 minutes for the batch call and up to 5 minutes for each chunk
  in the fallback.
- Generated `FunctionSpec.client_id()` is now `requested_model()`, returning the
  bare model name.
- `scripts/generate_baml_sdk.py` clears `baml_sdk/` before generating, so modules
  a newer toolchain dropped don't survive. It and CI set `DO_NOT_TRACK=1`, which
  stops `baml-cli` usage pings.
- Parser stack moves to treesitter-chunker 6.x, tree-sitter 0.26.x and
  tree-sitter-language-pack 1.20.x, matching the other Consiliency repositories
  (Consiliency/Code-Index-MCP#115).
- The required agent gate runs on Python 3.12, 3.13 and 3.14.

### Added
- `mcp-index grammars prefetch` and `mcp-index grammars check` manage the
  language-pack grammar cache. Language-pack 1.20 downloads grammars on first
  use, so CI, container images and the devcontainer prefetch them.
  `--export-libs DIR` writes a shared library directory for
  `TREE_SITTER_LANGUAGE_PACK_LIBS_DIR`, which images use because the pack only
  accepts a per-user cache. See `docs/SUPPORT_MATRIX.md` for the cache location
  and offline use.
- `get_status` reports a `grammars` block (`ready` or `grammar_unavailable`,
  with the missing grammars and affected languages), and the server logs an
  error at startup when registry grammars cannot load offline.

### Fixed
- Pack grammar lookups use `csharp` for the `c_sharp` language; the public
  language code is unchanged.
- Sandboxed plugin workers keep the host grammar cache root and libs
  directory after their environment is scrubbed, instead of starting from a
  cold cache.
- Sandboxed plugin workers warm the chunker parser for their language before
  installing the filesystem guard. Chunker 6 reads installed package metadata
  on the first python/javascript parser load, and the guard made `chunk_text`
  fail, so the sandboxed Python plugin stored no chunks.
- Network-denied sandbox workers never download grammars. The pack downloads
  from native code the Python socket guard cannot see, so a cold or
  rolled-over cache made workers fetch from GitHub and write into the host
  cache. Workers now load only already-extracted grammars and fail the plugin
  load closed otherwise.
- A file whose grammar cannot load offline fails indexing with
  `grammar_unavailable` instead of being stored without chunks, so a reindex no
  longer publishes a silently degraded generation.
- Shutdown asks an in-flight staged rebuild to stop before it publishes, so the
  shutdown watchdog no longer force-exits (exit code 1) a service whose
  rebuild was still running when the signal arrived.
- `grammars prefetch --export-libs` replaces each library atomically, refuses
  symlinked or other-writable targets and directories that hold other files,
  and never changes the mode of an existing directory. `grammars check` fails
  on an unsafe `TREE_SITTER_LANGUAGE_PACK_LIBS_DIR`.
- The release smoke harness passes the grammar location through and keeps the
  private grammar cache out of its world-writable cleanup sweep.
- The devcontainer moves to Debian bookworm because the language-pack wheels
  need glibc 2.34 or newer.

### Upgrade notes
- **Full rebuild required.** Chunk ids move to the `treesitter_chunk_id_v6`
  scheme. Indexes built with chunker 4.x report `scheme_mismatch` and fail
  closed until a full `reindex`; they are never mixed with 6.x ids. Semantic
  profiles fingerprint the chunker version, so existing vectors read as
  profile-incompatible until they are rebuilt or re-embedded.
- 6.0 drops erroneous keyword-only `lambda` fragment chunks from Python and
  changes CRLF handling in `chunk_file`. LF chunk ids are otherwise unchanged.
- **Grammar prefetch is mandatory.** Run `mcp-index grammars prefetch` after
  installing, and again after every language-pack upgrade, while network
  access is available. Sandboxed workers do not download a missing grammar:
  without a prefetch every file whose plugin needs a grammar fails with
  `grammar_unavailable`, and a reindex does not publish its generation.
- `mcp-index grammars check` now fails when `TREE_SITTER_LANGUAGE_PACK_LIBS_DIR`
  is unsafe (a symlink, not owned by root or the running user, or group- or
  other-writable); `get_status` reports that case as `libs_dir_unsafe`.
- **Semantic vectors need re-embedding.** After the full reindex, rebuild the
  semantic profile's summaries and vectors; 4.x vectors are never reused for
  6.x chunk ids. Budget the inference run before rebuilding large corpora.
- **glibc 2.34 or newer.** tree-sitter-language-pack 1.20 Linux wheels require
  glibc 2.34+ (for example Debian 12, Ubuntu 22.04, RHEL 9). Hosts on older
  glibc cannot install this release; stay on `1.4.1` there.
- **Dependency floor.** `tree-sitter>=0.26,<0.27`, `treesitter-chunker>=6,<7`
  and `tree-sitter-language-pack>=1.20,<1.21`. This lets `index-it-mcp` share
  an environment with other tools that pin tree-sitter 0.26.

- **baml-bridge 0.23.0.** `baml-bridge==0.23.0` replaces 0.20.1. Environments
  that pin `baml-bridge==0.20.1` cannot install this release; tools that pin
  0.23.0, such as `phase-loop-runtime` 0.7.30, now co-install. Code that imports
  the checked-in `baml_sdk/` gets the regenerated 0.23.0 SDK.
- **BAML call recording and upload are off by default.** Leave
  `MCP_BAML_TELEMETRY` unset (or `off`) to keep it that way. Setting
  `MCP_BAML_TELEMETRY=low|medium|high` records calls under `~/.baml` and, when
  `BOUNDARY_API_KEY` or a saved Boundary login exists, **uploads prompts that
  contain your source code** to Boundary. The level is read at the first
  `import baml_sdk`, so restart the service after changing it.
- **Upstream `BAML_TELEMETRY` is ignored.** Only `MCP_BAML_TELEMETRY` controls
  recording; existing `BAML_TELEMETRY` settings have no effect.
- **New LLM timeouts.** Summarization calls now stop after 5 minutes in total or
  10 seconds to connect. Size batch jobs for a worst case of 5 minutes for the
  batch call plus 5 minutes per chunk on the single-file fallback path.
- **`client_id()` renamed to `requested_model()`.** Callers of the generated
  `FunctionSpec.client_id()` must switch to `requested_model()`, which returns
  the bare model name.

### Known follow-ups
- Consiliency/Code-Index-MCP#117: a missing grammar currently blocks the whole
  repository's generation in every language. Per-language degradation is
  planned; until then, prefetch on every host.

## [1.4.1] - 2026-09-12

Prepared corrective release for Code-Index-MCP#97. This section does not imply publication;
protected-main dispatch and independent delivered-artifact acceptance are separate gates.

### Fixed
- Installed wheel and non-root container startup, SQLite upgrade and MCP lifecycle checks.
- Repository state, query refusal and generation fencing across interrupted indexing.
- Path, credential, artifact and local-inference policy boundaries.
- SQLite/vector reconciliation, staged corpus provenance and duplicate vector mapping counts.
- Disabled watcher admission, bounded shutdown and repository selectors in the admin UI.
- Signed artifact metadata binds repository identity and archive checksum; restores use fresh output directories.
- Staged reindex cancellation and durable row counts, independent ignore rules, cleanup failures and multi-language search scope.

### Verified
- Original pre-review pilot: installed PMCP 2.7.3, Inspector 2.6.0 and admin workflows on Linux/Python 3.12.
- Original bounded synthetic local-inference pilot with durable accounting and query/index contention; runtime repairs require fresh candidate-bound acceptance.
- Version-specific release checks and final review are tracked in `docs/operations/v13-release.md`.

### Limits
- Controlled rollout only; multi-repo and STDIO remain beta. No automatic fleet indexing.
- Other operating systems, Python 3.13+, broad retrieval quality and default reranking
  require separate evidence. Provider immutable revisions were unreported in the pilot.
- Archive-only signatures from earlier versions are rejected in enforce mode; prepare and sign canonical metadata as documented in `docs/security/attestation.md`.

## [1.4.0] — 2026-07-19

Minor feature release that lands the provider-neutral inference stack, the
tree-sitter chunker v4, and safer index rebuild/rollout gating on top of the
`1.3.1` hardening surface.

### Added
- Provider-neutral inference boundary so semantic/inference backends can be
  swapped behind a stable interface without changing caller contracts (#74).
- Tree-sitter chunker v4 adoption for more accurate and stable code chunking
  across supported languages (#79).
- Collection-resident, provenance-bound inference rollout gate (INFERLIVEGATE)
  that only activates inference for verified, provenance-tracked collections
  (#83).
- Roadmap v12 charting the next feature and hardening milestones (#77) and an
  inference-rollout operations runbook (#86).

### Changed
- Chunk-identity hardening via the CHUNKERSAFE guard, atomic index rebuild, and
  crash-ledger drain so interrupted rebuilds recover without corrupt or
  duplicated chunks (#84).
- Inference hygiene tightened end to end (INFERPOLISH) (#82).
- Four Lane A cleanups, including an explicit INDEX_LOCKED readiness state for
  concurrent index operations (#85).
- CI now derives the GHCR image namespace from the repository owner so release
  images publish correctly after the organization transfer (#89).
- Author standardization and CODEOWNERS added for deterministic review routing
  (#87, #88).

### Fixed
- Release container images are signed without crane, and the ownerless install
  URLs in the release automation and install scripts were corrected (#92).

## [1.3.1] — 2026-07-10 (Unpublished)

### Fixed
- Enforced the FastAPI JWT boundary independently from the optional local
  STDIO handshake and removed ambiguous repository-selector fallback.
- Made staged index recovery atomic and fail closed, unified summarization
  results, and bounded sandbox worker lifecycle and request handling.
- Restored deterministic local quality gates, release-critical typing, and
  hermetic test execution across the supported release matrix.
- Split release preparation from protected-main publication and pinned every
  external GitHub Action to an immutable upstream commit.

### Release status
- Package, runtime, active documentation, installers, and local build artifacts
  are prepared as `1.3.1` / `v1.3.1`.
- No tag, GitHub release, container image, attestation, or PyPI package was
  published by this hardening phase.

## [1.3.0] — 2026-07-06

### Added
- Added the public `mcp_server.client` Python API with typed local search,
  symbol lookup, reindex, and status operations for programmatic use without
  starting MCP STDIO or FastAPI.
- Added source metadata filters for ordinary code, friction signals, and
  historical GitHub issue context across the direct client, MCP tool handlers,
  and FastAPI `/search`.

### Changed
- Advanced the repo-owned package, runtime, release automation, installer, and
  active public documentation surface from `1.2.0` / `v1.2.0` to `1.3.0` /
  `v1.3.0`.
- Preserved `index-it-mcp` as the canonical Python distribution while keeping
  `code-index-mcp` limited to repository, client-local server, and container
  naming surfaces.

## [1.2.0] — 2026-04-25

### Changed (GAREL — stable surface preparation)
- Prepared the stable `1.2.0` / `v1.2.0` repo-owned release surface across
  package metadata, runtime metadata, release automation defaults, active
  install docs, support docs, and Docker installer helpers.
- Preserved `v1.2.0-rc8` as historical prerelease soak evidence only while
  routing the actual stable dispatch and GA release evidence to downstream
  `GADISP`.

### Known limitations
- This phase did not dispatch the stable release; GitHub Latest and Docker
  `latest` remain stable-only outputs that are still verified in `GADISP`.
- Beta, experimental, unsupported, and disabled-by-default support-tier limits
  remain governed by `docs/SUPPORT_MATRIX.md`; stable surface prep does not
  broaden product topology or runtime claims.

## [1.2.0-rc8] — 2026-04-25

### Changed (GARECUT — post-remediation RC recut freeze)
- Advanced the active remediated RC recut contract to `1.2.0-rc8` /
  `v1.2.0-rc8` across package metadata, runtime metadata, release automation
  defaults, and installer helpers without widening into GA channel changes.
- Froze the rc8 recut evidence and final-decision handoff expectations so a
  blocked or successful prerelease proof can route the next work either back
  through renewed GAREL or into a GARECUT rerun without silently authorizing
  GA.

### Known limitations
- `v1.2.0-rc8` remains a prerelease RC/public-alpha or beta channel artifact;
- it does not authorize GA wording, GitHub Latest promotion, or stable Docker
  `latest`; GitHub Latest is not the RC policy source.
- GARECUT dispatch still requires a clean enough release worktree, `HEAD ==
  origin/main`, and unused local plus remote `v1.2.0-rc8` tags before
  `gh workflow run` may execute.

## [1.2.0-rc7] — 2026-04-24

## [1.2.0-rc6] — 2026-04-24

### Changed (GARC — follow-up RC soak freeze)
- Advanced the active follow-up RC contract to `1.2.0-rc6` / `v1.2.0-rc6`
  across package metadata, runtime metadata, release automation defaults,
  installer helpers, README status, and the active Docker/MCP configuration
  docs.
- Added explicit follow-up RC soak operator guidance and evidence reduction for
  GARC so dispatch prerequisites, workflow observation fields, and blocked-stop
  handling are frozen before any new release mutation.

### Known limitations
- `v1.2.0-rc6` remains a prerelease RC/public-alpha or beta channel artifact;
  it does not authorize GA wording, GitHub Latest promotion, or stable Docker
  `latest`; GitHub Latest is not the RC policy source.
- GARC dispatch still requires a clean worktree plus fresh upstream
  governance/E2E/operations evidence before `gh workflow run` may execute.

## [1.2.0-rc5] — 2026-04-23

### Fixed (P27-P33 — v3 multi-repo blocker closeout)
- Added fail-closed readiness handling for missing, empty, stale, building,
  wrong-branch, and unsupported-worktree indexes. Query tools now return
  `index_unavailable` with `safe_fallback: "native_search"` instead of
  dispatching against an unsafe index.
- Hardened the v3 repository model around many unrelated repositories on one
  machine, one registered worktree per git common directory, and tracked/default
  branch indexing only.
- Added the P33 production multi-repo matrix for unrelated repo isolation,
  same-repo worktree rejection, wrong-branch safety, stale/missing index
  fallback, incremental repair, rename/delete/revert, force-push recovery, and
  wrong-artifact rejection.

### Changed (P34 — public alpha recut)
- Recut the public-alpha release contract to `1.2.0-rc5` / `v1.2.0-rc5` across
  package metadata, runtime metadata, release automation, README status,
  Docker/MCP setup docs, and `docs/SUPPORT_MATRIX.md`.
- Aligned outside-developer docs, operator runbooks, and agent handoffs on the
  readiness contract: indexed MCP results are authoritative only when readiness
  is `ready`; otherwise users should use native search or follow the returned
  remediation.
- Updated `docs/operations/deployment-runbook.md` to require P27-P33 gates,
  `make alpha-production-matrix`, docs truth tests, release metadata tests, and
  clean-checkout wheel/container smoke before public-alpha promotion.

### Known limitations
- Same-repo sibling worktrees and non-default branch queries are not independent
  indexed-search contexts in v3; they return `index_unavailable` with
  `safe_fallback: "native_search"` until a future release expands the topology
  contract.
- Language/runtime support remains scoped by `docs/SUPPORT_MATRIX.md`, release
  promotion remains gated by `docs/operations/deployment-runbook.md`, and the
  public-alpha go/no-go evidence remains linked from
  `docs/validation/private-alpha-decision.md`.

## [1.2.0-rc4] — 2026-04-22

### Added (P26 — private alpha evidence and public alpha decision)
- Added the P26 private-alpha evidence contract:
  `docs/validation/private-alpha-decision.md`,
  `docs/validation/private-alpha-evidence.schema.json`, and the ignored
  `private-alpha-evidence/` raw-output boundary.
- Added `scripts/private_alpha_evidence.py` for local private fixture runs across
  `python_repo`, `typescript_js_repo`, `mixed_docs_code_repo`,
  `multi_repo_workspace`, and `large_ignored_vendor_repo` before public alpha.
- Documented public-alpha release truth: supported install paths are
  `uv sync --locked`/STDIO and `ghcr.io/viperjuice/code-index-mcp`; language
  support is scoped by `docs/SUPPORT_MATRIX.md`; beta warnings and rollback
  instructions remain in `docs/operations/deployment-runbook.md`.

## [1.2.0-rc3] — 2026-04-21

### Changed (P21 — release contract and dependency unification)
- Unified the P21 release contract across runtime metadata, package metadata,
  README status, changelog, and release automation around `1.2.0-rc3`.
- Removed active workflow and Docker dependency references to retired
  `requirements*.txt` inputs in favor of the locked `pyproject.toml`/`uv.lock`
  dependency source.
- Expanded release metadata and requirements-consolidation tests to guard the
  P21 contract surfaces consumed by P22.

### Added (P25 — blocking release gates and automation)
- Added explicit public alpha gate jobs for dependency sync, focused format/lint,
  unit plus release smoke, integration smoke, Docker build/smoke, docs truth, and
  required-gate aggregation.
- Added release automation preflight refusal before version mutation, release
  branch creation, artifact build, tag creation, GitHub release creation, PyPI
  publish, or container publish.
- Documented the operator gate checklist and private-alpha authenticated
  attestation fallback for missing `ATTESTATION_GH_TOKEN` or repository
  attestation settings.

## [1.2.0-rc2] — 2026-04-20

### Fixed (P20 — sandbox/OpenBLAS hotfix, observability bugs)
- **(P20 SL-0)** Raised `CapabilitySet.mem_mb` default from 512 to 2048
  so sandbox-default-on plugins that transitively import numpy/OpenBLAS
  no longer abort on `RLIMIT_AS`. Closes IF-0-P20-3.
- **(P20 SL-2b)** Prometheus `/metrics` endpoint now exposes
  `mcp_tool_calls_total`, `mcp_rate_limit_sleeps_total`, and
  `mcp_artifact_errors_by_class_total`. Previously a registry split
  meant the counters lived on the default REGISTRY while the exporter
  served a private one.
- **(P20 SL-2b)** `SecretRedactionResponseMiddleware` is now applied
  on subprocess uvicorn boot. Previously it was registered in a startup
  hook that ran after FastAPI sealed its middleware stack, so subprocess
  gateways never redacted `Bearer ...` tokens in HTTP responses.

### Added (P20 — validation harness, runbook, observability smoke)
- **(P20 SL-1)** New `tests/integration/multi_repo/` harness — pytest
  fixture spawns ≥2 live uvicorn gateway subprocesses sharing one
  registry; tests cover concurrency (200 entries), cross-repo delta
  resolution, and dispatcher repo_id isolation. Closes IF-0-P20-1.
- **(P20 SL-2)** New `tests/integration/obs/` smoke — docker-compose
  Prom+Loki stack proves JSON log parse rate, counter scrape, and
  secret redaction over HTTP. Skips cleanly without docker.
- **(P20 SL-3)** New `docs/operations/deployment-runbook.md` with
  measurable bake-pass criteria, bake windows, and rollback procedures
  per stage (dev/staging/canary/full-prod). Closes IF-0-P20-2.

## [1.2.0-rc1] — 2026-04-20

### Added (P18 — Enforcement, Artifact Resilience & Ops)
- **Route auth enforcement** (IF-0-P18-4): `validate_production_config()` body fills weak-JWT, CORS-wildcard, permissive rate-limit, and weak-admin-password checks. `render_validation_errors_to_stderr()` emits `[FATAL]`/`[WARN]`-prefixed listings. Gateway startup exits non-zero on any fatal validation error in production; WARN-continues in dev/test. Auth added to `/search/capabilities`, `/graph/context`, `/graph/search`. (`mcp_server/gateway.py`, `mcp_server/config/validation.py`, `tests/security/test_route_auth_coverage.py`, `tests/security/test_startup_config_validation.py`)
- **Artifact pipeline resilience**: publisher try/finally rollback via `gh release delete` on downstream failure; `artifact_download` delta-base-missing fallback to full artifact; `_respect_rate_limit` parses `Retry-After` (int-seconds or HTTP-date, capped 300s) and distinguishes 403 (`TerminalArtifactError`) from 429 (backoff + retry); attestation preflight checks `attestations:write` scope. (`mcp_server/artifacts/publisher.py`, `artifact_download.py`, `providers/github_actions.py`, `attestation.py`)
- **Retention janitor CLI** (IF-0-P18-2): `python -m mcp_server.cli retention prune --repo OWNER/REPO [--dry-run] [--older-than-days N] [--keep-latest-n N]`. Protects `index-latest` and `isLatest=true` releases. New `mcp_server/artifacts/retention.py` policy module; new `mcp_server/cli/retention_commands.py` click group.
- **JSON logging**: hand-rolled `JSONFormatter` in `mcp_server/core/logging.py`; emits `{"timestamp","level","name","message", ...extras}`. Gated on `MCP_ENVIRONMENT=production` or `MCP_LOG_FORMAT=json`.
- **Secret redaction middleware** (IF-0-P18-1): `SecretRedactionResponseMiddleware` rewrites `Bearer \S+`, `JWT_SECRET_KEY=\S+`, `GITHUB_TOKEN=\S+` to `[REDACTED]` in 4xx/5xx response bodies. Streaming bodies pass through unmodified (documented limitation). (`mcp_server/security/security_middleware.py`)
- **Prometheus counters**: `mcp_rate_limit_sleeps_total` (no labels) and `mcp_artifact_errors_by_class_total{error_class}`. Scrapeable on `/metrics`. (`mcp_server/metrics/prometheus_exporter.py`)
- **Attestation boot probe**: `probe_gh_attestation_support()` (lru_cache'd) runs `gh attestation --help`. Boot banner emits `ATTESTATION_PREREQ` WARN if the subcommand is unavailable and `MCP_ATTESTATION_MODE=enforce`. (`mcp_server/artifacts/attestation.py`, `mcp_server/cli/stdio_runner.py`)
- **stdio_runner module surface** (IF-0-P18-3): `initialize_services()` and `call_tool(name, arguments)` hoisted from `_serve()` closures to top-level async functions. Seven runtime state variables (`dispatcher`, `plugin_manager`, `sqlite_store`, `initialization_error`, `_file_watcher`, `_indexing_thread`, `_fts_rebuild_thread`) promoted to module globals. Restores the pre-P2B test surface on the post-P2B file layout. (`mcp_server/cli/stdio_runner.py`)

### Changed (P18)
- **Plugin sandbox default** (behavioural change): `MCP_PLUGIN_SANDBOX_ENABLED=1` is no longer required — sandboxing is now ON by default. Set `MCP_PLUGIN_SANDBOX_DISABLE=1` to opt out. See `docs/security/sandbox.md` for migration notes. (`mcp_server/plugins/plugin_factory.py`)

### Fixed (P18 — P17 straggler burn-down)
- Full-suite failure count closed from 19 (P17 SL-4b close) to 4. The 17 `tests/test_mcp_server_cli.py` failures and 2 `tests/test_benchmarks.py` SLO failures now pass; residual 4 are pre-existing integration tests gated on `gh` CLI `attestations:write` scope and an unrelated P16 vocabulary signature mismatch. (`mcp_server/cli/stdio_runner.py`, `mcp_server/benchmarks/benchmark_suite.py`, `mcp_server/plugins/sandboxed_plugin.py`)

### Added (P17 — Durability & Multi-Instance Safety)
- **Registry flock protocol** (IF-0-P17-1): `RepositoryRegistry.save()` acquires
  `fcntl.flock(LOCK_EX)` on a sibling `.lock` file, then performs a read-merge-write
  with an atomic `rename()`.  Two concurrent processes calling `save()` against the same
  file no longer lose each other's writes. (`mcp_server/storage/repository_registry.py`,
  `tests/test_registry_concurrency.py`)
- **Singleton reset wired**: `reset_process_singletons()` (P16 IF-0-P16-4 stub) is now
  called at the top of `initialize_stateless_services()`.  Repeat-init in the same
  Python process yields fresh managers. (`mcp_server/cli/bootstrap.py`,
  `tests/test_singleton_reset.py`)
- **Ref-poller edge-case handling** (SL-2): `RefPoller` detects detached HEAD,
  force-push, and branch-rename; each triggers `enqueue_full_rescan(repo_id)` to prevent
  incremental indexing on a rewritten history. (`mcp_server/watcher/ref_poller.py`,
  `tests/test_ref_poller_edges.py`)
- **Sweeper observability** (SL-2): `WatcherSweeper` exceptions emit `WARNING` log and
  increment `mcp_watcher_sweep_errors_total` Prometheus counter.
  (`mcp_server/watcher/sweeper.py`, `tests/test_sweeper_observability.py`)
- **MCP_MAX_FILE_SIZE_BYTES enforced** (SL-2): Dispatcher walker (`index_directory`)
  now skips files larger than `MCP_MAX_FILE_SIZE_BYTES` (default 10 MiB); oversize
  files are logged and skipped without stalling the indexer.
  (`mcp_server/dispatcher/dispatcher_enhanced.py`)
- **Checkpoint clears on clean exit** (SL-2): `IncrementalIndexer` clears the
  checkpoint file even when `errors` is non-empty on clean exit; a re-run starts fresh
  rather than resuming from a stale error state.
  (`mcp_server/indexing/incremental_indexer.py`)
- **ENOSPC → read-only store** (SL-3): `SQLiteStore` catches `OSError(ENOSPC)` on
  `commit()`, transitions to read-only mode, and increments
  `mcp_storage_readonly_total`.  The server keeps serving reads without crashing.
  (`mcp_server/storage/sqlite_store.py`, `tests/test_disk_full.py`)
- **Schema migration backup** (SL-3): `SchemaMigrator.migrate_artifact()` writes a
  timestamped `.backup` copy of the DB before any migration; rollback-on-failure
  restores from backup. (`mcp_server/storage/schema_migrator.py`,
  `tests/test_schema_migration_backup.py`)
- **UnknownSchemaVersionError in core/errors.py** (SL-3): `UnknownSchemaVersionError`
  promoted from `schema_migrator.py` to `mcp_server/core/errors.py`, extending the P16
  IF-0-P16-1 taxonomy by one class.
- **RerankerFactory.create_default()** (SL-4): `RerankerFactory.create_default()`
  returns a functional reranker; resolves the P14 carry-over.
  (`mcp_server/plugins/reranker_factory.py`)
- **Ruff F401 clean** (SL-4): `ruff F401` count across `mcp_server/dispatcher`,
  `mcp_server/storage`, `mcp_server/watcher` is 0.
- **Multi-instance runbook** (SL-docs): `docs/operations/multi-instance.md` — flock
  protocol, singleton-reset semantics, DR/recovery procedure.
- **Vocabulary freeze** (IF-0-P17-1): Registry flock + read-merge-write pattern is
  frozen as a named interface for cross-phase reference.
- **Test-debt ledger** (SL-docs): `docs/operations/known-test-debt.md` enumerates
  residual failures after P17 close (56 total; 9 cross-repo coordinator carry-over +
  47 pre-existing in other modules).

### Added (P16 — Shared Vocabulary Preamble)
- **Frozen error taxonomy** (IF-0-P16-1): New error classes `TransientArtifactError`, `TerminalArtifactError`, and `SchemaMigrationError` in `mcp_server/core/errors.py` for structured error handling in artifact operations. Stubs only — no consumer wiring.
- **Lazy env-var getters** (IF-0-P16-2): Five new environment variable getters in `mcp_server/config/env_vars.py` — `get_max_file_size_bytes()`, `get_artifact_retention_count()`, `get_artifact_retention_days()`, `get_disk_readonly_threshold_mb()`, and `get_publish_rollback_enabled()` — with sensible defaults. Stubs only — no consumer wiring.
- **ValidationError dataclass + validate_production_config** (IF-0-P16-3): New `ValidationError` dataclass and keyword-only `validate_production_config(environment: str = "production")` function in `mcp_server/config/validation.py`. Stubs only — no consumer wiring.
- **reset_process_singletons** (IF-0-P16-4): New utility function in `mcp_server/cli/bootstrap.py` for test isolation and graceful restarts. Stubs only — no consumer wiring.

### Added (P15 — Security Hardening)
- **Plugin sandboxing** (IF-0-P15-1): Plugins execute in isolated worker processes with JSON-line IPC, 30s timeout, and capability-based restrictions. New `mcp_server/sandbox/` package with supervisor and adapter; `SandboxedPlugin` wraps instances with `CapabilitySet` (filesystem, network, subprocess, env_read). (`mcp_server/sandbox/supervisor.py`, `mcp_server/plugins/sandboxed_plugin.py`, `tests/security/test_plugin_sandbox.py`)
- **Metrics endpoint auth** (IF-0-P15-2): `/metrics` requires `Authorization: Bearer <token>` (app-level) or NetworkPolicy restriction (k8s-only). Wired via `require_auth("metrics")` middleware. (`mcp_server/security/security_middleware.py`, `mcp_server/api/gateway.py`)
- **Artifact attestation** (IF-0-P15-3): Published artifacts are signed with `gh attestation sign` and verified with `gh attestation verify` (requires `GITHUB_TOKEN` with `attestations:write` scope). Mode controlled by `MCP_ATTESTATION_MODE` (enforce/warn/skip). (`mcp_server/artifacts/attestation.py`, `mcp_server/artifacts/publisher.py`, `mcp_server/artifacts/artifact_download.py`)
- **Path traversal guard + token validator** (IF-0-P15-4): Search results filtered through `PathTraversalGuard.normalize_and_check(path, allowed_roots)` (controlled by `MCP_ALLOWED_ROOTS` env). Startup validates `GITHUB_TOKEN` for all five required scopes via `TokenValidator.validate_scopes()` (soft-fail default; hard-fail with `MCP_REQUIRE_TOKEN_SCOPES=1`). Rate-limit backoff on cross-repo artifact fetches. (`mcp_server/security/path_guard.py`, `mcp_server/security/token_validator.py`)

### Added (P14 — Multi-Repo Completeness + Schema Evolution)
- **Reranker wiring** (IF-0-P14-1): `CrossRepoCoordinator.__init__` now accepts an injected `IReranker` (`mcp_server/indexer/reranker.py`); falls back to `RerankerFactory.create_default()` with graceful degradation to `None`. (`mcp_server/dispatcher/cross_repo_coordinator.py`, `tests/test_cross_repo_reranker.py`)
- **Dependency graph** (IF-0-P14-2): New `mcp_server/dependency_graph/` package with `parsers.py`, `aggregator.py`, and ecosystem parsers (Python/npm/Go/Cargo). `_get_repository_dependencies` returns resolved repo IDs. (`mcp_server/dispatcher/cross_repo_coordinator.py`)
- **Schema migrator** (IF-0-P14-3): New `mcp_server/storage/schema_migrator.py` with `SchemaMigrator`, `UnknownSchemaVersionError`, and `SchemaMigrationError`. Artifact manifests carry `schema_version`; `check_compatibility` gates downloads by version. (`mcp_server/artifacts/artifact_download.py`)
- **Auto-delta artifacts** (IF-0-P14-4): New `mcp_server/artifacts/delta_policy.py` with `DeltaPolicy`/`DeltaDecision`. Publisher switches to delta mode when artifact exceeds `MCP_ARTIFACT_FULL_SIZE_LIMIT` (default 500 MB). `_get_chunk_ids_for_path` accepts `limit`/`offset` pagination. (`mcp_server/artifacts/publisher.py`, `mcp_server/artifacts/artifact_upload.py`)
- **Watcher sweep + rename atomicity** (IF-0-P14-5): New `mcp_server/watcher/sweeper.py::WatcherSweeper` runs full-tree scans every `MCP_WATCHER_SWEEP_MINUTES` (default 60) to recover dropped inotify/FSEvents events. `move_file` wrapped in `two_phase_commit`; raises `IndexingError` on semantic failure. (`mcp_server/dispatcher/dispatcher_enhanced.py`, `mcp_server/watcher_multi_repo.py`)

## [1.1.0] - 2026-04-14

### Security
- **#40** Removed hardcoded `JWT_SECRET_KEY` and `DEFAULT_ADMIN_PASSWORD` defaults from
  `gateway.py`. Server now raises `RuntimeError` at startup if either env var is unset,
  preventing silent production deployments with known-weak credentials.
  `docker-compose.yml` and `.env.example` updated to use env var substitution with no
  insecure literal fallbacks. (`mcp_server/gateway.py`, `docker-compose.yml`, `.env.example`)

### Fixed
- **#41** `get_index_status()` `symbols_count` field now returns the actual number of
  symbols stored for the file (was always `0`). Added `SQLiteStore.count_symbols_for_file()`
  and wired it into `IndexEngine.get_index_status()`.
  (`mcp_server/storage/sqlite_store.py`, `mcp_server/indexer/index_engine.py`)
- **#41** Reference storage in `_store_parse_result()` now calls `get_symbol()` +
  `store_reference()` instead of silently passing; references are persisted in
  `symbol_references` and queryable via `get_references()`.
  (`mcp_server/indexer/index_engine.py`)
- **#42** Removed broken `curl .../install-mcp.sh` line from README (script does not
  exist). Replaced silent TODO in `watcher_multi_repo._create_and_upload_artifact()` with
  an explicit `logger.warning` directing users to the CI workflow for artifact upload.
  (`README.md`, `mcp_server/watcher_multi_repo.py`)
- Windows CI compatibility: forward-slash path normalization in `SQLiteStore.store_file()`,
  `signal.SIGALRM` guard in `mcp_server_cli.py`, double-quoted git commit messages in
  tests, `>= 0` timing assertions for low-resolution Windows clocks.
  (`mcp_server/storage/sqlite_store.py`, `scripts/cli/mcp_server_cli.py`,
  `tests/test_utilities.py`, `tests/test_repository_management.py`,
  `tests/test_multi_repo_search.py`, `tests/test_dispatcher_advanced.py`)
- Benchmark scripts updated to current SQLiteStore/plugin API: replaced removed
  `store.add_symbol()` with `create_repository` + `store_file` + `store_symbol`, and
  replaced obsolete `plugin.extract_symbols()` with `plugin.indexFile()` with
  `IndexShard` dict-style access.
  (`benchmarks/symbol_lookup_benchmark.py`, `benchmarks/semantic_search_benchmark.py`,
  `benchmarks/indexing_speed_benchmark.py`)

### Added
- Backstage catalog registration (`catalog-info.yaml`) and standard repo layout
  (`docs/`, `mkdocs.yml`). (`04ea8ff`)
- Consiliency maintenance worker trigger workflow for scheduled index health checks.
  (`.github/workflows/`, `72f15a5`)

### Added
- Query-intent routing: symbol-pattern queries (`class Foo`, `def bar`, CamelCase,
  `snake_case`) route directly to the symbols table, bypassing BM25. Fixes retrieval for
  class/function lookups that previously returned wrong files.
  (`mcp_server/dispatcher/query_intent.py`, `dispatcher_enhanced.py`)
- `FlashRankReranker` and `CrossEncoderReranker` (OSS, lazy-loaded); all three rerankers
  share a unified sync interface. Configured via `RERANKER_TYPE` env var.
  (`mcp_server/dispatcher/reranker.py`)
- Matrix benchmark: 17-query suite covering all mode × embedding × reranker combinations.
  (`scripts/run_matrix_benchmark.py`)
- LLM chunk summarization: `ChunkWriter` / `LazyChunkWriter` / `ComprehensiveChunkWriter`;
  per-profile config via `summarization:` block in `code-index-mcp.profiles.yaml`.
  (`mcp_server/indexing/summarization.py`)
- Initial project structure with plugin-based architecture
- FastAPI-based MCP server implementation
- Python plugin with Jedi integration for advanced code analysis
- Tree-sitter wrapper for language parsing
- Semantic indexer with Voyage AI integration
- C4 architecture diagrams using Structurizr DSL
- Comprehensive documentation structure
- Plugin base class for language extensions
- File watcher skeleton for real-time updates
- Cloud sync stub implementation

### Changed
- Enhanced Python plugin with proper AST analysis
- Improved error handling in gateway endpoints
- Updated project documentation and README

### Added
- MCP `InitializeResult.instructions` field populated: every connecting client receives
  search-first guidance at handshake time, over any transport (stdio, SSE, HTTP).
  (`scripts/cli/mcp_server_cli.py`)
- `last_indexed` field added to `search_code` results, surfacing the index timestamp so
  agents can assess line-number freshness. (`scripts/cli/mcp_server_cli.py`,
  `mcp_server/storage/sqlite_store.py`)
- `FileWatcher` now auto-starts inside `initialize_services()` after the dispatcher is
  ready, so the index stays current without any manual setup. Stopped automatically in
  `main()` `finally` block on server exit. Only active when using `EnhancedDispatcher`
  (not BM25-only `SimpleDispatcher` mode). (`scripts/cli/mcp_server_cli.py`)
- Auto-index on first run: when no index exists, `initialize_services()` creates
  `.mcp-index/code_index.db` and indexes the repository in a background thread. The MCP
  server is immediately responsive; search results populate as indexing progresses.
  Set `MCP_AUTO_INDEX=false` to disable for large repos and run the `reindex` tool
  manually. (`scripts/cli/mcp_server_cli.py`)
- `search_code` responses now include `"indexing_in_progress": true` when the initial
  background index is still running, preventing agents from concluding "no results"
  prematurely while the index is being built. (`scripts/cli/mcp_server_cli.py`)
- `FileWatcher` start deferred until after background initial index completes, eliminating
  concurrent SQLite write contention between the bulk `index_directory()` pass and the
  watchdog event handler on first-run repos. (`scripts/cli/mcp_server_cli.py`)
- `.gitignore` entries for `.mcp-index/code_index.db-shm` and `.mcp-index/code_index.db-wal`
  (SQLite WAL mode sidecar files) added to `install.sh` and written automatically during
  auto-init so WAL files never appear as untracked. (`mcp-index-kit/install.sh`,
  `scripts/cli/mcp_server_cli.py`)
- `code-index-mcp.profiles.yaml` discovery now searches `MCP_PROFILES_PATH` env var →
  CWD → server package directory in that order. Previously the file was only found at CWD,
  so semantic profiles silently vanished when `cwd` was set to another repo. Fallback to the
  server's own installation means semantic search and summarization work out-of-the-box in
  any repo. (`mcp_server/config/settings.py`)
- `${VAR:default}` env-var substitution supported in all `base_url` fields of
  `code-index-mcp.profiles.yaml`. Set `VLLM_EMBEDDING_BASE_URL` or
  `VLLM_SUMMARIZATION_BASE_URL` to point at any server without editing the YAML.
  (`mcp_server/config/settings.py`, `code-index-mcp.profiles.yaml`)
- File-count guard before auto-index: if the target repo exceeds `MCP_AUTO_INDEX_MAX_FILES`
  (default 100 000), auto-indexing is skipped and a warning is logged. Prevents indefinite
  startup I/O on very large monorepos. (`scripts/cli/mcp_server_cli.py`)
- BM25 FTS auto-heal: on startup, if an existing index has tracked files but an empty
  `fts_code` table (e.g. after a migration or partial corruption), a background thread
  calls `rebuild_fts_code()` automatically. Previously the empty state persisted until
  a manual `reindex` tool call. (`scripts/cli/mcp_server_cli.py`)

### Fixed
- `scripts/cli/mcp_server_cli.py` was calling undefined `get_settings()` on every tool
  invocation, crashing the server before any tool could execute. Replaced with
  `Settings.from_environment()`. (`scripts/cli/mcp_server_cli.py`)
- `LazyChunkWriter.__init__` was missing the `summarization_config` parameter despite
  the base class `ChunkWriter` accepting it, causing a `TypeError` on every server
  startup. (`mcp_server/indexing/summarization.py`)
- File watcher `trigger_reindex()` now calls `remove_file()` before `index_file()`,
  preventing stale chunks from persisting when a file edit changes chunk boundaries.
  (`mcp_server/watcher.py`)
- `EnhancedDispatcher.remove_file()` now evicts `_file_cache` before SQLite deletion,
  preventing a skip-on-reindex race where an unchanged-hash file would be left absent
  from the index after a forced removal. (`mcp_server/dispatcher/dispatcher_enhanced.py`)
- `SQLiteStore.remove_file()` now deletes `symbol_trigrams` before `symbols`, fixing a
  FK constraint failure that silently left file rows in the DB on removal.
  (`mcp_server/storage/sqlite_store.py`)
- `SQLiteStore.remove_file()` now clears `fts_code` entries regardless of whether
  `file_id` was stored as an integer, absolute path, or relative path string.
  (`mcp_server/storage/sqlite_store.py`)
- `SQLiteStore.search_bm25()` now includes `last_modified` in result rows so the
  `last_indexed` field is non-null in search results. (`mcp_server/storage/sqlite_store.py`)
- Semantic rebuild (`_build_semantic_baseline`) now uses `IgnorePatternManager`, respecting
  `.gitignore` and `.mcp-index-ignore`. Previously indexed `test_workspace/` (20,803 Django
  fixture files), causing 7-hour rebuilds. (`mcp_server/cli/index_management.py`)
- Default ignore patterns expanded: `test_workspace/`, `test_repos/`, `vendor/`,
  `third_party/`, `baml_client/`, `*_pb2.py`, `*_pb2_grpc.py`.
  (`mcp_server/core/ignore_patterns.py`)
- `code-index-mcp.profiles.yaml` `base_url` now takes precedence over `OPENAI_API_BASE`
  env default (was reversed, routing oss_high embeddings to wrong endpoint).
  (`mcp_server/config/settings.py`)
- Corrected `oss_high` vLLM `base_url` from `127.0.0.1:8001` to `ai:8001`.
  (`code-index-mcp.profiles.yaml`)
- Resolved merge conflicts in treesitter_wrapper.py
- Fixed Python plugin implementation issues
- Tree-sitter byte-offset/char-offset mismatch in Python plugin: symbol names were corrupted in files
  containing multi-byte characters (e.g. em-dashes in docstrings) before the first class/function
  definition. Fixed by using `node.text.decode("utf-8")` in `plugin.py` and `plugin_semantic.py`.
- BM25/FTS path was bypassed when `semantic=True` but no Qdrant indexer was initialized. Fixed in
  `dispatcher_enhanced.py` so the FTS path runs whenever the semantic indexer is absent.
- Python plugin `_preindex()` now excludes `htmlcov`, `.venv`, `venv`, `node_modules`, `__pycache__`,
  `.git`, `dist`, `build` directories to prevent junk results from polluting in-memory indexes.

### Security
- Added input validation for file paths
- Implemented basic security checks in API endpoints
- Added secret detection patterns

## [0.1.0] - TBD

### Planned Features
- Complete implementation of C, C++, JavaScript, Dart, and HTML/CSS plugins
- SQLite-based local storage with FTS5
- Full file system watcher implementation
- Performance optimizations and caching
- Authentication and authorization system
- Comprehensive test suite
- Production deployment guide

---

## Version History

### Pre-release Development
- Project inception and architecture design
- Core infrastructure setup
- Initial plugin system implementation
