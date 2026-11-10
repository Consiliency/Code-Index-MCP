# Code-Index-MCP Support Matrix

This matrix is the canonical support statement for published `1.6.0`
(2026-11-10 UTC), which runs on the `mcp` 2.x SDK (`mcp>=2.2,<2.4`). MCP STDIO
is the primary LLM surface and FastAPI is secondary admin. Its delivered
acceptance is partial (wheel, image, co-installs and PMCP prepare and offline
passed; PMCP rehearsal and browser not run). Published `1.5.1` caps `mcp<2`.
`1.2.0` through `1.5.0`, including `1.4.1` (2026-10-13), are yanked on PyPI:
their `mcp` requirement has no upper bound, so an install resolves `mcp` 2.x
and STDIO fails at import. The abandoned `2.x` line is yanked too.
See [release preparation and acceptance](operations/v13-release.md) for the
separate PREP and SHIP gates. Support tiers below are unchanged.

Repository-topology support is separate from language/runtime support. The v3
public-alpha model supports many unrelated repositories on one machine, with one
registered worktree per git common directory. Only the tracked/default branch is
indexed automatically. Same-repo sibling worktrees and non-default branch
queries return `index_unavailable` with `safe_fallback: "native_search"` until
readiness remediation completes; indexed MCP results are authoritative only when
readiness is `ready`. Lexical repository readiness does not imply semantic
readiness: `semantic: true` queries additionally require summaries, vector
linkage, and profile-compatible semantic metadata.

Runtime facts are grounded in `mcp_server/plugins/language_registry.py`,
`mcp_server/plugins/plugin_factory.py`, `mcp_server/plugins/plugin_set_registry.py`,
`mcp_server/plugins/sandboxed_plugin.py`, `mcp_server/cli/tool_handlers.py`,
and `mcp_server/cli/stdio_runner.py`. The canonical release-boundary and
evidence checklist is `docs/validation/ga-readiness-checklist.md`.

## Client and transport compatibility

The client/transport compatibility reducer for the current MCP surface is
`docs/status/MCP_COMPATIBILITY_EVALUATION.md`. The short version:

- Official Python MCP SDK over STDIO is the direct phase-owned verified client.
- Claude Code and other STDIO launchers are documented against the same STDIO
  contract, but MCPEVAL does not add a separate vendor-harness smoke for each
  editor.
- FastAPI remains the admin/debug HTTP surface, not the repository's MCP
  Streamable HTTP transport.
- Remote Streamable HTTP MCP remains deferred until a later roadmap phase adds
  a distinct endpoint, transport tests, and transport-specific auth.
- MCP SDK: `1.6.0`, source installs and wheels built from `main` run on the
  `mcp` 2.x SDK and require `mcp>=2.2,<2.4` (Consiliency/Code-Index-MCP#124). They install
  in the same environment as PMCP 3.x (`mcp>=2.2,<3`) and
  `phase-loop-runtime==0.7.30` with a clean `uv pip check`, so PMCP 3.x no
  longer needs a separate environment. Only mcp 2.2 and 2.3 are verified. The
  Tasks runtime depends on `Server.middleware`, which mcp marks provisional, so
  each new mcp minor must be re-tested before the cap is widened. The server
  refuses to start with a clear message if the installed mcp lacks the
  middleware contract it needs.
  Remove any `mcp<2` pin installed alongside them: it makes the install
  unresolvable. Published `1.5.1` still caps `mcp<2`, so it cannot share an
  environment with PMCP 3.x.
- Protocol revisions: the STDIO server negotiates 2024-11-05 through 2025-11-25
  through `initialize`, and 2026-07-28 through `server/discover`. The official
  Python SDK's `ClientSession`, the 1.x SDK client and the PMCP 3.x gateway
  negotiate 2025-11-25; Claude Code 2.1.x negotiates 2026-07-28. Both serve the
  same tools and results. Task-augmented `reindex` and `write_summaries` (the
  `tasks/*` methods) exist only on handshake-era revisions, because 2026-07-28
  has no Tasks.

## Claim tiers

- **Stable prep**: The historical `GADISP` preparation is complete and `v1.4.0`
  was published on 2026-07-19. Publication is not fleet acceptance; v13 is the
  active hardening effort, with progress in `docs/status/V13_EXECUTION.md`.
- **Beta**: Multi-repo support, STDIO, and secondary tool readiness remain beta
  surfaces. Passing TOOLRDY evidence is readiness evidence, not a GA support
  claim or a support-matrix expansion.
- **GA**: GA claims require a separate hardening roadmap with enforced release
  governance, current end-to-end production evidence, and explicit support-tier
  acceptance for every advertised language/runtime surface.

Product-level release posture continues to use the shared GABASE labels
`public-alpha`, `beta`, `GA`, `experimental`, `unsupported`, and
`disabled-by-default`. The row-level support tiers below use the frozen
GASUPPORT labels `GA-supported`, `beta`, `experimental`, `unsupported`, and
`disabled-by-default`.

## Machine-checkable support facts

`PluginFactory.get_plugin_availability()` and `list_plugins` are the canonical
machine-readable support-fact surfaces. Each `plugin_availability` row exposes:

- `state` for default availability in the current sandbox posture.
- `sandbox_supported` for whether a hardened sandbox module exists.
- `specific_plugin` and `availability_basis` for specialized-plugin versus
  registry-only behavior.
- `activation_mode` for default, extra-required, or disabled-by-default paths.
- `plugin_module`, `required_extras`, `remediation`, and `error_type` for
  exact module and optional-dependency posture.

These fields are support evidence, not marketing copy. Secondary-tool readiness
evidence in `docs/validation/secondary-tool-readiness-evidence.md` remains
readiness evidence only and must not be used as proof of broader language,
topology, or install-surface support expansion.

## Language and runtime support

| Language | Support tier | Runtime behavior | Parser status | Sandbox support | Required extras | Symbol quality | Semantic support | Known limitations |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Bash | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| C | GA-supported | Specialized plugin | Specific parser | Default sandbox module | Core | High for C symbols | Optional provider/API key | Header ownership and macro semantics are best effort |
| C# | beta | Specialized plugin when importable | Specific parser | Default sandbox module | Core | Medium to high | Optional provider/API key | Sandbox map includes `c_sharp`/`csharp`; project references are best effort |
| C++ | GA-supported | Specialized plugin | Specific parser | Default sandbox module | Core | High for common C++ symbols | Optional provider/API key | Template and macro expansion remain best effort |
| CMake | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| CSS | GA-supported | Specialized HTML/CSS plugin | Specific parser | Default sandbox module | Core | Medium selectors/rules | Optional provider/API key | CSS and HTML share one plugin implementation |
| CSV | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Low document-like rows | Optional provider/API key | Treated as text/data, not code symbols |
| Dart | beta | Specialized plugin | Specific parser | Default sandbox module | Core | Medium to high | Optional provider/API key | Flutter semantics are best effort |
| Dockerfile | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic instructions | Optional provider/API key | Available only when sandbox is explicitly disabled |
| Elixir | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| Elm | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| Erlang | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| Fortran | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| Go | beta | Specialized plugin when importable | Specific parser | Sandbox module exists; current env importability can vary | Core | Medium to high | Optional provider/API key | Plugin import may depend on the local environment |
| GraphQL | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic schema/query symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| Haskell | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| HTML | GA-supported | Specialized HTML/CSS plugin | Specific parser | Default sandbox module | Core | Medium elements/ids/classes | Optional provider/API key | HTML and CSS share one plugin implementation |
| Java | beta | Specialized plugin | Specific parser | Default sandbox module | `java` extra recommended for static analysis | Medium to high | Optional provider/API key | Import resolution depends on optional `javalang` |
| JavaScript | GA-supported | Specialized plugin | Specific parser | Default sandbox module | Core | High for common JS/JSX patterns | Optional provider/API key | Framework-specific semantics are best effort |
| JSON | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Low document/data symbols | Optional provider/API key | Treated as structured data, not executable code |
| Julia | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| Kotlin | beta | Specialized plugin | Specific parser | Default sandbox module | Core | Basic to medium | Optional provider/API key | `Plugin` export is normalized; language semantics remain best effort |
| LaTeX | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Low document sections | Optional provider/API key | Document-oriented extraction only |
| Lua | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| Makefile | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic targets | Optional provider/API key | Available only when sandbox is explicitly disabled |
| Markdown | beta | Specialized document plugin | Specific parser | Default sandbox module | Core | Headings and document chunks | Optional provider/API key | Not a programming-language symbol index |
| MATLAB | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| Objective-C | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| OCaml | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| Perl | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| PHP | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| Plain Text | disabled-by-default | Simple text plugin | Text scanner | Reported as `unsupported` in default sandbox mode | Core | Text chunks only | Optional provider/API key | Sensitive extensions are indexed only inside allowed local roots |
| Python | GA-supported | Specialized plugin | Tree-sitter plus Jedi | Default sandbox module | Core | High for Python symbols | Optional provider/API key | Dynamic runtime behavior is best effort |
| R | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| Regular Expression | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Low pattern chunks | Optional provider/API key | Treated as text/pattern data |
| reStructuredText | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Low document sections | Optional provider/API key | Document-oriented extraction only |
| Ruby | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| Rust | beta | Specialized plugin | Specific parser | Default sandbox module | Core | Basic to medium | Optional provider/API key | `Plugin` export is normalized; language semantics remain best effort |
| Scala | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| SCSS | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic selector/rule symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| SQL | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic query/table references | Optional provider/API key | Dialect semantics are best effort |
| Swift | beta | Specialized plugin | Specific parser | Default sandbox module | Core | Medium to high | Optional provider/API key | Platform SDK semantics are best effort |
| TOML | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Low key/value chunks | Optional provider/API key | Treated as structured data |
| TypeScript | GA-supported | Specialized plugin | Specific parser | Default sandbox module | Core | High for TS/TSX patterns | Optional provider/API key | Type-checker project context is best effort |
| Vim Script | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Basic structural symbols | Optional provider/API key | Available only when sandbox is explicitly disabled |
| XML | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Low element chunks | Optional provider/API key | Treated as structured data |
| YAML | disabled-by-default | Generic Tree-sitter registry path | Registry grammar | Reported as `unsupported` in default sandbox mode | Core | Low key/value chunks | Optional provider/API key | Treated as structured data |

## Install and tool surfaces

| Surface | Support tier | Default posture | Evidence basis | Notes |
| --- | --- | --- | --- | --- |
| STDIO query tools (`search_code`, `symbol_lookup`, `get_status`, `list_plugins`) | beta | Primary LLM surface | `stdio_runner._build_tool_list()`, readiness-gated tool descriptions, `plugin_availability` facts | Indexed query results are authoritative only when readiness is `ready`; `search_code` also supports additive `source_type="friction"` / `source_type="history"` filters, `friction_categories`, `history_labels`, `history_repos`, and `include_source_metadata` |
| STDIO mutation and summarization tools (`reindex`, `write_summaries`, `summarize_sample`) | beta | Secondary tool surfaces behind readiness gates | `tool_handlers.py`, TOOLRDY evidence, readiness-refusal tests | Non-ready repos fail closed; the artifact is readiness evidence, not support expansion |
| Python client (`mcp_server.client`, `IndexItClient`) | beta | Supported local programmatic API | `mcp_server/client.py`, `mcp_server/client_types.py`, PYCLIENT tests/docs | Local-only API for `search_code`, `symbol_lookup`, `reindex`, and `get_status`; it shares readiness-aware search behavior with MCP `search_code` and returns typed `index_unavailable` results instead of dispatching stale indexes |
| FastAPI admin and diagnostics surface | beta | Secondary/admin surface | `README.md`, `docs/GETTING_STARTED.md`, `docs/MCP_CONFIGURATION.md` | Do not treat FastAPI as the primary LLM interface |
| Native source install via `uv sync --locked` | beta | Canonical local development and operator path | `pyproject.toml`, `uv.lock`, install docs | This is the preferred local install path while the release remains pre-GA |
| Python distribution name on PyPI (`index-it-mcp`) | beta | Canonical distribution name; `1.6.0` is the latest release published on PyPI | package naming note, install docs, `docs/status/public-package-identity.md`, delivered acceptance in `docs/operations/v13-release.md` | Install `pip install index-it-mcp==1.6.0`: it requires `mcp>=2.2,<2.4`, co-installs with PMCP 3.x, and its delivered acceptance installed the PyPI wheel with no lock constraints. `1.5.1` (caps `mcp<2`) stays installable. `1.2.0` through `1.5.0` are yanked (unbounded `mcp`). Source install and locally built wheels remain supported development paths |
| Docker image `ghcr.io/consiliency/code-index-mcp:v1.4.0` | beta | Published container path; `:v1.4.0` and `:latest` are live on GHCR | Docker guide, release evidence, image naming tests | Container docs must keep the same topology and readiness limits as native docs |
| Semantic search (`uv sync --locked --extra semantic` plus provider config) | experimental | Optional extra and provider-dependent | extras docs, config env vars, support-fact notes; INFERGATE rollout gate (`docs/status/INFERENCE_ROLLOUT.md`) | Requires semantic readiness `ready` for the active profile; ready `semantic: true` responses expose semantic source/profile/collection metadata, while `summaries_missing`, `vectors_missing`, `profile_mismatch`, or `semantic_stale` return explicit semantic refusal or failure instead of lexical fallback. Retrieval-rollout gate verdict is `dark_opt_in` (gate not yet passed — dense/hybrid arms `not_run` for lack of live provider evidence), so this remains experimental, not default-enabled. A live provider run's numbers count only after the queried collection's resident `collection-provenance.v1` binding verifies against the frozen corpus (matching `indexed_commit`/`corpus_sha256`, recorded profile/provider revision, matching `point_set_id`); an unverified collection is out of contract |
| Reranking (`uv sync --locked --extra rerank` plus provider/model config) | experimental | Optional extra and provider-dependent | extras docs and support-fact notes; INFERGATE rollout gate (`docs/status/INFERENCE_ROLLOUT.md`) | Treat as opt-in ranking improvement, not baseline query behavior. Learned reranking stays OPT-IN / not default-enabled: the predeclared rollout gate has NOT passed (`hybrid_rerank` verdict `dark_opt_in` — insufficient provider evidence; holdout not used for tuning). A default flip requires a live run whose queried collection's resident `collection-provenance.v1` binding verifies against the frozen corpus before `hybrid_rerank` metrics are counted |
| Default sandboxed plugin execution | beta | Default security posture | `sandbox_supported`, `activation_mode`, sandbox docs | Coverage varies by language and is authoritative through `plugin_availability` |
| Unsandboxed or sandbox-disabled plugin path | disabled-by-default | Explicit operator opt-in only | `activation_mode=disabled_by_default`, sandbox docs | Enabling unsandboxed fallback does not widen documented default support |
| Same-repo sibling worktrees and non-default indexed routing | unsupported | Outside the indexed-routing contract | readiness classifier, v3 topology docs, failure-matrix tests | Use native search or readiness remediation instead of treating this as indexed support |

## Parser stack and grammar cache

The parser stack is treesitter-chunker 6.x, tree-sitter 0.26.x and
tree-sitter-language-pack 1.20.x (Consiliency/Code-Index-MCP#115). Support
tiers above are unchanged by the upgrade.

- **Grammar delivery.** Language-pack 1.20 no longer bundles parser libraries.
  It downloads them on first use into a versioned cache,
  `<root>/tree-sitter-language-pack/v1.20.0/libs` (about 93 MB for the 47
  registry grammars). The root is `TREE_SITTER_LANGUAGE_PACK_CACHE_DIR`, else
  `$XDG_CACHE_HOME`, else `$HOME/.cache`, else the running user's passwd home
  `.cache`. Run `mcp-index grammars prefetch` while
  network access is available; `mcp-index grammars check` verifies every
  grammar loads offline. A cold cache with no network fails with
  `DownloadError`/`LanguageNotFoundError`. A warm cache works offline and
  read-only. The pack refuses a cache directory that is not owned by the
  running user or is group- or other-writable, so the cache is per user. For
  shared installs, `mcp-index grammars prefetch --export-libs DIR` copies the
  libraries to a world-readable directory that any user loads through
  `TREE_SITTER_LANGUAGE_PACK_LIBS_DIR=DIR`. The pack loads that directory
  first, without an ownership check, into the server and every sandbox worker,
  so it must be owned by root or the service user and must not be group- or
  other-writable; `mcp-index grammars check` fails otherwise, and also when a
  library in it is writable by others or is a symlink. The export writes each
  library to a temporary file and renames it into place, so re-exporting on a
  live host does not disturb running processes. It refuses symlinked path
  components, an existing directory that is writable by others (such as
  `/tmp`) or holds anything but parser libraries, and only sets mode 0755 on
  directories it creates. Container images do this into
  `/opt/tree-sitter-language-pack/libs`, CI prefetches before the agent gate,
  and sandboxed plugin workers keep the host cache root and libs directory
  (resolved to absolute paths against the host's working directory) even
  though their environment is scrubbed.
- **Sandboxed workers never download grammars.** The pack downloads from
  native code that the worker's Python socket guard cannot see, so a
  network-denied worker (the default) also loads grammars from the local cache
  only. It points `TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL` at an unknown-scheme
  sentinel that fails before any socket is created, and it refuses, with
  `DownloadError`, any grammar that is not already extracted in the cache or
  present in the libs directory, as well as the pack's download, prefetch and
  manifest entry points. A cold, missing or rolled-over cache (for example
  after a 1.20.x patch release moves the versioned directory) therefore fails
  the plugin load closed, with no connection and nothing written to the host
  cache, instead of downloading. A warm cache loads read-only. Prefetch on the
  host after every pack upgrade. A plugin that calls the pack's native module or
  public loader classes directly, or changes its own environment (for example
  clearing the manifest URL), is outside this cooperative guard: it can create
  the versioned cache directory and a lock file, and with the manifest URL
  cleared it can download. This matches the sandbox's cooperative model; the
  guard does not contain malicious plugins.
- **Missing grammars degrade per language.** A file whose plugin needs a
  grammar that is not loadable offline (not in the cache or libs directory) is
  never stored without chunks. "Language" is the registry language of the
  file's extension, and `typescript` needs the `typescript`, `tsx` and
  `javascript` grammars, because its plugin loads all three. Document-plugin
  languages (`markdown`, `plaintext`) never load a grammar and are not
  affected (Consiliency/Code-Index-MCP#117).
  - *Staged full and incremental builds* (sync, the ref poller, `reindex` of a
    whole repository) decide availability once when the build starts. They
    leave every language with a missing grammar out of the generation whole:
    its files are not indexed, and any of its rows the previous generation
    held (files, symbols, chunks, FTS rows, vectors) are removed, so a stale
    or chunkless row never appears as authoritative and a full walk and an
    incremental update store the same rows. The other languages publish
    normally. If every admitted file is left out, the build refuses to publish
    with code `grammar_unavailable` and the previous generation, or
    `missing_index`, stays in place. Retained history or document rows do not
    count as indexed source for this check.
  - *Readiness stays `ready`.* Repository readiness gains an additive
    `coverage` object: `{"state": "complete"}`, or `{"state": "degraded",
    "unavailable_languages": [...], "missing_grammars": [...],
    "deferred_files": n, "recoverable": bool, "remediation": "..."}`
    (`coverage` is `null` when no index could be inspected). The
    `get_status` repository rows carry `coverage` and `coverage_status`, and
    the `grammars` block lists `repositories_degraded`. While coverage is
    degraded, `search_code` returns its envelope (`results`, `coverage`,
    `note`) instead of the bare result list, and a `symbol_lookup` not-found
    response carries `coverage`. The left-out languages have no rows, so they
    never appear in results.
  - *Repair.* Once the missing grammars load (for example after
    `mcp-index grammars prefetch` or `--export-libs`, no restart needed), the
    next sync, or the ref poller's next tick with an unchanged branch tip,
    indexes only the left-out languages' files through an incremental build
    with an empty change set. Rows, chunk ids and vectors of the other
    languages are untouched, and the chunk identity scheme does not change. An
    explicit whole-repository `reindex` also restores full coverage. Automatic
    repair starts only from a `ready` repository, so a repair that fails is not
    retried on every poll.
  - *Artifacts.* A degraded generation is never uploaded or turned into a
    commit artifact. Restore refuses an artifact that carries the marker, and
    one that lacks any committed file this host would index (an uploader older
    than this change drops the marker but archives the partial rows), so the
    caller builds locally instead.
  - *Watcher sweep.* Files of a left-out language are not drift, so they never
    force a full rebuild; the repair above indexes them.
  - *Other index paths stay fail-closed.* Path-scoped (subdirectory or
    single-file) `reindex` through the MCP tool or the gateway, the watcher
    and the stdio background index fail such files with
    `grammar_unavailable`, as before.
  - *Prefetched hosts.* With every grammar loadable there is no marker,
    coverage is `complete`, and behaviour is unchanged. An unsafe libs
    directory whose grammars all load is complete coverage too; `get_status`
    reports it as `libs_dir_unsafe`.
  - `get_status` reports a `grammars` block with `state` (`ready`,
    `grammar_unavailable`, or `libs_dir_unsafe`), the missing grammars, the
    affected language codes, libs-directory problems and the remediation, and
    the server logs an error at startup when grammars are missing.
- **Grammar names.** The public language code `c_sharp` is unchanged; the pack
  grammar is `csharp`.
- **Platform floor.** The language-pack Linux wheels need glibc 2.34 or newer.
- **Index identity.** Chunk ids move to the `treesitter_chunk_id_v6` scheme. An
  index built with treesitter-chunker 4.x (`treesitter_chunk_id_v4`) reports
  `scheme_mismatch`, fails closed and needs a full `reindex`; it is never mixed
  with 6.x ids. Semantic profiles fingerprint the chunker version, so 4.x
  vectors and summaries read as profile-incompatible until rebuilt.
- **Python.** The required agent gate runs on Python 3.12, 3.13 and 3.14 in
  hosted Linux CI. That is CI evidence, not installed-runtime acceptance on
  those versions.

## Summarization runtime (BAML)

Chunk summarization calls its LLM through the generated `baml_sdk` and the
`baml-bridge` 0.23.0 runtime. Summarization prompts contain the indexed
repository's source code.

**Run recording and Boundary upload are off by default.** baml-bridge 0.21+
records every call under `BAML_HOME` (default `~/.baml/btel`). When
`BOUNDARY_API_KEY` is set or a saved `baml auth login` exists, it also uploads
the recordings to Boundary's cloud (`api.prod.bcs.boundaryml.com`).
`scripts/generate_baml_sdk.py` bakes `off` into the generated SDK as its own
default, so the default holds no matter which code imports `baml_sdk` first.

| `MCP_BAML_TELEMETRY` | Effect |
|---|---|
| unset, blank or whitespace (for example compose's `${MCP_BAML_TELEMETRY:-}`) | off |
| `off`, `false`, `0`, or any unrecognized value | off |
| `true` | keeps the default, which is off |
| `low`, `medium` or `high` | records at that level (opt-in) |

- Only `MCP_BAML_TELEMETRY` is honored. Upstream's `BAML_TELEMETRY` variable is
  ignored by this SDK, so `BAML_TELEMETRY=off` neither disables nor enables
  anything.
- The level is read once, when `baml_sdk` is first imported. Restart the
  process to change it.
- Opting in uploads recordings, which include prompts containing source code,
  to Boundary whenever `BOUNDARY_API_KEY` or a saved Boundary login is present.
  To keep them local, set `BOUNDARY_API_KEY=local`. Before opting in, consider
  adding `[bridge] http_bodies = false` to `baml.toml` and regenerating, so
  recordings omit request and response bodies.

**LLM call timeouts.** Since baml-bridge 0.21, each LLM call has a 5 minute total
timeout and a 10 second connect timeout; 0.20.1 had no limit. Repository-scope
summarization also applies its own per-call timeout (`call_timeout_s`, default
three times `timeout_s`, at least 15 seconds). The single-file
`summarize_file_chunks` path has no such limit. There, a provider that accepts
the connection but never answers can cost up to 5 minutes for the batch call,
then up to 5 minutes for each chunk in the per-chunk fallback.

**SDK API.** `FunctionSpec.client_id()` became `requested_model()`, which returns
the bare model name (`gpt-oss-120b`, not `openai-generic/gpt-oss-120b`).
Production summaries record the configured model name and are unaffected.

## V13 Verification Boundary

Published tiers above are unchanged. The `>=3.12` package requirement and
OS-independent classifier are compatibility declarations, not proof of every
Python release or operating system. Current remediation starts with Linux and
Python 3.12; Python 3.13+, macOS and Windows need their own installed-runtime
evidence before rollout on those combinations. No blanket 48-language result
is implied by a Python fixture or a registry inventory.

The installed wheel smoke now checks fresh and upgraded SQLite databases,
registered Git indexing, real SDK queries/refusals and a second STDIO session.
The container smoke exercises the configured non-root HTTP startup and mounted
Git fixture, not only health. A failing node blocks distribution acceptance;
actual results belong in `docs/validation/v13/DIST.json`, not this description.
BAML generation and runtime are pinned together at 0.23.0; regeneration is
offline and does not call the inference client declared in the source contract.
BAML run recording (local `BAML_HOME` files and Boundary upload) is off by
default on every path; see "Summarization runtime (BAML)" for the opt-in.
V13 has historical accepted DIST, STATE, SAFETY, DATA and PILOT receipts bound
to their recorded source commits, not to subsequent PREP repairs. The historical
PILOT proves its independently installed candidate through PMCP 2.7.3, MCP
Inspector 2.6.0 and the HTTP admin UI on Linux/Python 3.12.12, including bounded
local inference and query/index contention. See `docs/validation/v13/PILOT.json`
and `docs/operations/v13-pmcp-pilot.md` for exact source/artifact identities.
The 1.4.1 candidate was accepted, published and delivered-accepted as recorded in
`docs/status/V13_EXECUTION.md`. 1.5.0 is published with partial delivered
acceptance; its corrective release 1.5.1 is published and its delivered wheel
and image acceptance passed, as recorded in `docs/operations/v13-release.md`.
1.6.0 is published with partial delivered acceptance (PMCP rehearsal and browser
not run). 1.2.0 through 1.5.0 are yanked. Historical PILOT acceptance is not
proof for 1.5.0, 1.5.1 or 1.6.0. This does not establish fleet readiness, general retrieval quality, immutable
provider-build identity, or support on untested platforms. PREP code review and
SHIP publication remain required for each candidate; the published 1.4.0 package
predates v13. Existing support tiers and opt-in retrieval gates are unchanged.

## Notes

- Optional semantic and reranking behavior depends on extras, provider
  configuration, and API keys or local model endpoints. It should not be
  treated as unconditional support. Lexical readiness alone is not enough for
  semantic query support.
- `list_plugins` is the canonical detailed capability surface. Each
  `plugin_availability` row includes `language`, `state`, `sandbox_supported`,
  `specific_plugin`, `plugin_module`, `availability_basis`,
  `activation_mode`, `required_extras`, `remediation`, and `error_type`.
- Registry-only languages remain bounded by the default sandbox posture. If a
  language is reported as `unsupported` with `activation_mode` set to
  `disabled_by_default`, that is evidence for the row above, not permission to
  advertise the language as default-on support.
- TOOLRDY evidence in
  `docs/validation/secondary-tool-readiness-evidence.md` remains readiness
  evidence only. It can justify readiness wording for existing secondary tools
  but must not be used as proof of broader support expansion.
