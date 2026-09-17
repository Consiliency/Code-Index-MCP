# Code-Index-MCP Support Matrix

This matrix is the canonical support statement for the `1.4.1` prepared
candidate. MCP STDIO is the primary LLM surface and FastAPI is secondary admin.
Published `1.4.0` is historical distribution evidence, not proof of these v13
repairs. See [release preparation and acceptance](operations/v13-release.md)
for the separate PREP and SHIP gates. Support tiers below are unchanged.

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
| Python distribution name on PyPI (`index-it-mcp`) | beta | Canonical distribution name; `1.4.0` is published on PyPI | package naming note, install docs, `docs/status/public-package-identity.md` | `pip install index-it-mcp` resolves to the published `1.4.0`; source install and locally built wheels remain supported development paths |
| Docker image `ghcr.io/consiliency/code-index-mcp:v1.4.0` | beta | Published container path; `:v1.4.0` and `:latest` are live on GHCR | Docker guide, release evidence, image naming tests | Container docs must keep the same topology and readiness limits as native docs |
| Semantic search (`uv sync --locked --extra semantic` plus provider config) | experimental | Optional extra and provider-dependent | extras docs, config env vars, support-fact notes; INFERGATE rollout gate (`docs/status/INFERENCE_ROLLOUT.md`) | Requires semantic readiness `ready` for the active profile; ready `semantic: true` responses expose semantic source/profile/collection metadata, while `summaries_missing`, `vectors_missing`, `profile_mismatch`, or `semantic_stale` return explicit semantic refusal or failure instead of lexical fallback. Retrieval-rollout gate verdict is `dark_opt_in` (gate not yet passed — dense/hybrid arms `not_run` for lack of live provider evidence), so this remains experimental, not default-enabled. A live provider run's numbers count only after the queried collection's resident `collection-provenance.v1` binding verifies against the frozen corpus (matching `indexed_commit`/`corpus_sha256`, recorded profile/provider revision, matching `point_set_id`); an unverified collection is out of contract |
| Reranking (`uv sync --locked --extra rerank` plus provider/model config) | experimental | Optional extra and provider-dependent | extras docs and support-fact notes; INFERGATE rollout gate (`docs/status/INFERENCE_ROLLOUT.md`) | Treat as opt-in ranking improvement, not baseline query behavior. Learned reranking stays OPT-IN / not default-enabled: the predeclared rollout gate has NOT passed (`hybrid_rerank` verdict `dark_opt_in` — insufficient provider evidence; holdout not used for tuning). A default flip requires a live run whose queried collection's resident `collection-provenance.v1` binding verifies against the frozen corpus before `hybrid_rerank` metrics are counted |
| Default sandboxed plugin execution | beta | Default security posture | `sandbox_supported`, `activation_mode`, sandbox docs | Coverage varies by language and is authoritative through `plugin_availability` |
| Unsandboxed or sandbox-disabled plugin path | disabled-by-default | Explicit operator opt-in only | `activation_mode=disabled_by_default`, sandbox docs | Enabling unsandboxed fallback does not widen documented default support |
| Same-repo sibling worktrees and non-default indexed routing | unsupported | Outside the indexed-routing contract | readiness classifier, v3 topology docs, failure-matrix tests | Use native search or readiness remediation instead of treating this as indexed support |

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
BAML generation and runtime are pinned together at 0.221.0; regeneration is
offline and does not call the inference client declared in the source contract.
V13 has historical accepted DIST, STATE, SAFETY, DATA and PILOT receipts bound
to their recorded source commits, not to subsequent PREP repairs. The historical
PILOT proves its independently installed candidate through PMCP 2.7.3, MCP
Inspector 2.6.0 and the HTTP admin UI on Linux/Python 3.12.12, including bounded
local inference and query/index contention. See `docs/validation/v13/PILOT.json`
and `docs/operations/v13-pmcp-pilot.md` for exact source/artifact identities.
The current 1.4.1 candidate still requires fresh installed, browser and bounded
live validation plus four-seat reconciliation. Historical PILOT acceptance cannot
substitute for that proof. This does not establish fleet readiness, general retrieval quality, immutable
provider-build identity, or support on untested platforms. PREP code review and
SHIP publication remain required; the published 1.4.0 package is not this v13
candidate. Existing support tiers and opt-in retrieval gates are unchanged.

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
