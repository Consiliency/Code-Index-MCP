# Code-Index-MCP

**Give your AI coding assistant instant, precise search across your whole codebase — so it finds the exact code it needs in milliseconds instead of burning time and tokens reading whole files.**

Code-Index-MCP is a fast, **local-first** search index for your code. It plugs into Claude Code and other AI assistants (through the Model Context Protocol, "MCP") and lets them look up any symbol or search any text in your repository almost instantly — without your code ever leaving your machine.

> **New to Code-Index-MCP?** Start with the [Getting Started Guide](docs/GETTING_STARTED.md).
>
> **Status:** v1.4.1 stable surface prepared — MCP tools (`search_code`, `symbol_lookup`) are the primary interface; a FastAPI admin gateway is available for diagnostics.

> **Stable-surface prep status**: This guide targets the `1.4.1` prepared candidate.
> Published `1.4.0` (2026-07-19) does not include these v13 repairs.
> MCP STDIO is primary; FastAPI is secondary admin only.
> See [release preparation and acceptance](docs/operations/v13-release.md).
> Publication and fleet rollout remain separate acceptance gates.

## Project Status
**Version**: 1.4.1 (prepared candidate; publication tracked separately)
**Python distribution**: `index-it-mcp`
**Container image**: `ghcr.io/consiliency/code-index-mcp`
**Primary surface**: MCP tools (`search_code`, `symbol_lookup`) via the STDIO runner when repository readiness is `ready`
**Secondary surface**: FastAPI admin REST gateway for diagnostics and scripting — see "Admin REST Interface (secondary)" below
**Core features**: local indexing, symbol/text search, registry-based language coverage; see [docs/SUPPORT_MATRIX.md](docs/SUPPORT_MATRIX.md)
**Optional features**: semantic search (requires Voyage AI or a local vLLM endpoint), GitHub Artifacts index sync
**Performance**: sub-100ms symbol lookup and sub-500ms search on indexed repos (benchmarked on this codebase; results vary by repo size and language mix)
**GA decision**: see [docs/validation/ga-final-decision.md](docs/validation/ga-final-decision.md); the current product decision is `ship GA`, while install-surface claims remain bounded by [docs/status/public-package-identity.md](docs/status/public-package-identity.md).
**GA readiness contract**: see [docs/validation/ga-readiness-checklist.md](docs/validation/ga-readiness-checklist.md) for the frozen release boundary, support-tier labels, evidence ownership, and rollback expectations that apply before dispatch.
**Release dispatch**: the historical governance record names this boundary `GADISP`; the current workflow implements it as a separate `publish` mode restricted to protected `main`.
**Repository model**: one server can serve many unrelated repositories, with one registered worktree per git common directory. Only the tracked/default branch is indexed automatically. Indexed MCP results are authoritative only when readiness is `ready`; unavailable indexes return `index_unavailable` with `safe_fallback: "native_search"`.

`MCP_CLIENT_SECRET` is a local STDIO handshake guard for `mcp-index stdio`.
The FastAPI gateway uses separate admin/debug bearer token authentication, and
no remote MCP authorization is implemented while remote MCP transport remains
deferred.

## Why it exists

When an AI assistant works in a large codebase, it often reads big chunks of files just to find what it needs. That's slow, and every file it reads costs tokens (money). Code-Index-MCP builds a local index so the assistant can jump straight to the right function, class, or line — **cutting token cost and making answers faster and more accurate**.

## Who it's for

Developers and teams using AI coding assistants on real, sizable codebases who want **faster, cheaper, more accurate** results — and who want their code to stay **private, on their own machine**.

## What you get

- **⚡ Instant lookups** — sub-100ms symbol lookup, sub-500ms search on indexed repos.
- **🔒 Local-first & private** — indexing runs on your machine; your code isn't shipped to a cloud.
- **💸 Lower token cost** — the assistant *searches* instead of reading whole files (see the charts below).
- **🧠 Semantic search (optional)** — natural-language code search via embeddings.
- **🌐 Many languages, many repos** — one server can index multiple repositories with mixed languages.
- **🔌 Plugin-based & extensible** — add language support without touching the core.

## See it in action

Benchmarks in this repository show large token/cost reductions when an assistant searches with Code-Index-MCP instead of reading files directly:

![Token and cost savings — summary](reports/performance_charts/summary_infographic.png)

![Cost savings](reports/performance_charts/cost_savings.png)

![Token reduction by language](reports/performance_charts/token_reduction_by_language.png)

*(Charts generated from the benchmarks in `reports/`; numbers vary by repo size and language mix.)*

## 🎯 Key Features

- **🚀 Local-First Architecture**: All indexing happens locally for speed and privacy
- **📂 Local Index Storage**: All indexes stored at `.indexes/` (relative to MCP server)
- **🔌 Plugin-Based Design**: Easily extensible with language-specific plugins
- **🔍 Language support**: Tiered language/runtime support is documented in [docs/SUPPORT_MATRIX.md](docs/SUPPORT_MATRIX.md)
- **⚡ Real-Time Updates**: File system monitoring for instant index updates
- **🧠 Semantic Search**: AI-powered code search with Voyage AI embeddings
- **📊 Rich Code Intelligence**: Symbol resolution, type inference, dependency tracking
- **🚀 Enhanced Performance**: Sub-100ms queries with timeout protection and BM25 bypass
- **🔄 Git Synchronization**: Automatic index updates tracking repository changes
- **📦 Portable Index Management**: Zero-cost index sharing via GitHub Artifacts
- **🔄 Automatic Index Sync**: Pull indexes on clone, push on changes
- **🎯 Smart Result Reranking**: Multi-strategy reranking for improved relevance
- **🎯 Query-Intent Routing**: Symbol-pattern queries (`class Foo`, `def bar`, CamelCase) bypass BM25 and hit the symbols table directly for sub-5ms lookups
- **🔒 Security-Aware Export**: Automatic filtering of sensitive files from shared indexes
- **🔍 Hybrid Search**: BM25 + semantic search with configurable fusion
- **🔐 Index Everything Locally**: Search .env files and secrets on your machine
- **🚫 Smart Filtering on Share**: .gitignore and .mcp-index-ignore patterns applied only during export
- **🌐 Multi-Language Indexing**: Index entire repositories with mixed languages

## 🏗️ Architecture

The Code-Index-MCP follows a modular, plugin-based architecture designed for extensibility and performance:

### System Layers

1. **🌐 System Context (Level 1)**
   - Developer interacts with Claude Code or other LLMs
   - MCP protocol provides standardized tool interface
   - Local-first processing with optional cloud features
   - Performance SLAs: <100ms symbol lookup, <500ms search

2. **📦 Container Architecture (Level 2)**
   ```
   ┌─────────────────┐     ┌──────────────┐     ┌─────────────┐
   │   API Gateway   │────▶│  Dispatcher  │────▶│   Plugins   │
   │   (FastAPI)     │     │              │     │ (Language)  │
   └─────────────────┘     └──────────────┘     └─────────────┘
          │                        │                     │
          ▼                        ▼                     ▼
   ┌─────────────────┐     ┌──────────────┐     ┌─────────────┐
   │  Local Index    │     │ File Watcher │     │  Embedding  │
   │  (SQLite+FTS5)  │     │  (Watchdog)  │     │   Service   │
   └─────────────────┘     └──────────────┘     └─────────────┘
   ```

3. **🔧 Component Details (Level 3)**
   - **Gateway Controller**: RESTful API endpoints
   - **Dispatcher Core**: Plugin routing and lifecycle
   - **Plugin Base**: Standard interface for all plugins
   - **Language Plugins**: Specialized parsers and analyzers
   - **Index Manager**: SQLite with FTS5 for fast searches
   - **Watcher Service**: Real-time file monitoring

## 🔐 Security

Code-Index-MCP implements defense-in-depth security hardening (Phase 15):

- **Plugin Sandboxing**: Plugins execute in isolated worker processes with capability-based restrictions. See [docs/security/sandbox.md](docs/security/sandbox.md).
- **Artifact Attestation**: Published indexes are signed with GitHub SLSA attestations and verified at download. See [docs/security/attestation.md](docs/security/attestation.md).
- **Path Traversal Guard**: Search results are validated to prevent escaping configured repository roots. See [docs/security/path-guard.md](docs/security/path-guard.md).
- **Token Validation**: GitHub tokens are validated for required scopes at startup (`contents:read`, `metadata:read`, `actions:read`, `actions:write`, `attestations:write`). See [docs/security/token-scopes.md](docs/security/token-scopes.md).
- **Metrics Authentication**: The `/metrics` endpoint requires bearer token authentication.

`MCP_CLIENT_SECRET` is a local STDIO handshake guard only. It is not the
gateway's admin/debug bearer token authentication, and no remote MCP
authorization is implemented while the repository continues to defer remote
MCP transport.

For a comprehensive operator runbook, see [docs/operations/user-action-runbook.md](docs/operations/user-action-runbook.md).

## 📁 Project Structure

The project follows a clean, organized structure. See [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md) for detailed layout.

Key directories:
- `mcp_server/` - Core MCP server implementation
- `scripts/` - Development and utility scripts
- `tests/` - Comprehensive test suite with fixtures
- `docs/` - Documentation and guides
- `architecture/` - System design and diagrams
- `docker/` - Docker configurations and compose files
- `mcp-index-kit/` - Shared MCP indexing toolkit and examples
- `docs/status/` - Durable evidence notes kept under version control

## 🛠️ Language Support

The current stable-surface support contract is centralized in [docs/SUPPORT_MATRIX.md](docs/SUPPORT_MATRIX.md).
It distinguishes specialized plugins, generic Tree-sitter registry coverage,
default sandbox support, optional extras, semantic/rerank setup, and known
alpha limitations. Do not assume every registry language has the same symbol
quality or default sandbox behavior.

## 🚀 Quick Start

Supported install paths are the versioned container image
`ghcr.io/consiliency/code-index-mcp:v1.4.1` (or `:latest`), native Python/STDIO
with `uv sync --locked`, and a locally built `index-it-mcp` wheel. Use the
`v1.4.1` registry image only after protected-main publication and delivered
artifact acceptance; until then use the candidate source or local wheel. The
`ghcr.io/consiliency/code-index-mcp:local-smoke` image remains an optional dev
path built from this checkout with `make release-smoke-container`.
Language coverage is bounded by [docs/SUPPORT_MATRIX.md](docs/SUPPORT_MATRIX.md),
GA-hardening evidence ownership is frozen in
[docs/validation/ga-readiness-checklist.md](docs/validation/ga-readiness-checklist.md),
and rollback procedures live in
[docs/operations/deployment-runbook.md](docs/operations/deployment-runbook.md).
Do not treat this prepared surface as a universal language support
claim; row-level support tiers still live in the support matrix.

### 🎯 Automatic Setup for Claude Code/Desktop (Recommended)
```bash
# Auto-configures MCP for your environment
./scripts/setup-mcp-json.sh

# Or interactive mode
./scripts/setup-mcp-json.sh --interactive
```

This automatically detects your environment and creates the appropriate `.mcp.json` configuration.

### 🐳 Docker Setup

The installer targets `v1.4.1`; pulling it requires matching GHCR publication.
Before that gate, the `local-smoke` tag is an optional dev image you can
build from this checkout with `make release-smoke-container`.

#### Option 1: Basic Search (No API Keys) - 2 Minutes
```bash
# After release acceptance, index your current directory
docker run -it -v $(pwd):/workspace ghcr.io/consiliency/code-index-mcp:v1.4.1
```

#### Option 2: AI-Powered Search
```bash
# Set your API key (get one at https://www.voyageai.com — free tier available)
export VOYAGE_API_KEY=your-key

# Run with semantic search enabled explicitly
docker run -it -v $(pwd):/workspace -e SEMANTIC_SEARCH_ENABLED=true -e VOYAGE_API_KEY ghcr.io/consiliency/code-index-mcp:v1.4.1
```

### 💻 Environment-Specific Setup

#### 🪟 Windows (Native)
```powershell
# PowerShell
.\scripts\setup-mcp-json.ps1

# Or manually with Docker Desktop
docker run -it -v ${PWD}:/workspace ghcr.io/consiliency/code-index-mcp:v1.4.1
```

#### 🍎 macOS
```bash
# Install Docker Desktop or use Homebrew
brew install --cask docker

# Run setup
./scripts/setup-mcp-json.sh
```

#### 🐧 Linux
```bash
# Install Docker (no Desktop needed)
curl -fsSL https://get.docker.com | sh

# Run setup
./scripts/setup-mcp-json.sh
```

#### 🔄 WSL2 (Windows Subsystem for Linux)
```bash
# With Docker Desktop integration
./scripts/setup-mcp-json.sh  # Auto-detects WSL+Docker

# Without Docker Desktop
cp .mcp.json.templates/native.json .mcp.json
uv sync --locked
```

#### 📦 Nested Containers (Dev Containers)
```bash
# For VS Code/Cursor dev containers
# Option 1: Use native Python (already in container)
cp .mcp.json.templates/native.json .mcp.json

# Option 2: Use Docker sidecar (avoids dependency conflicts)
docker-compose -f docker/compose/development/docker-compose.mcp-sidecar.yml up -d
cp .mcp.json.templates/docker-sidecar.json .mcp.json
```

### Windows Path-Length Fallback

Tracked repository paths are kept at or below the 160-character tracked-path
limit, and the REPOCLEAN audit also checks wheel-content path depth for
installed `site-packages` members. If a Windows checkout still hits a
path-length edge because of a deeply nested clone location or third-party
tooling, use `git config --global core.longpaths true` as a fallback rather
than as the primary mitigation.

### 📋 MCP.json Configuration Examples

The setup script creates the appropriate `.mcp.json` for your environment. Manual examples:

#### Native Python (Dev Container/Local)
```json
{
  "mcpServers": {
    "code-index-native": {
      "command": "python",
      "args": ["-m", "mcp_server.cli.stdio_runner"],
      "cwd": "${workspace}"
    }
  }
}
```

#### Docker (Windows/Mac/Linux)
```json
{
  "mcpServers": {
    "code-index-docker": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "${workspace}:/workspace",
        "ghcr.io/consiliency/code-index-mcp:v1.4.1"
      ]
    }
  }
}
```

### Release Smoke

```bash
make release-smoke
make release-smoke-container
```

## Using Against Many Repos

Index many unrelated repositories from a single running server instance.

**Prerequisites**: set `MCP_ALLOWED_ROOTS` to an OS-path-separator-separated list of absolute directory paths (`:` on Unix, `;` on Windows) that the server is allowed to index before starting the server:

```bash
export MCP_ALLOWED_ROOTS=/abs/a:/abs/b
```

**Start the server** (with secrets via `op run` or plain `mcp-index stdio`):

```bash
op run --env-file=.mcp.env -- mcp-index stdio
```

**Register each repo** — register one worktree per git common directory. The
stable `repo_id` comes from Tier 1 `git rev-parse --git-common-dir`, so sibling
worktrees of the same repository share identity and are not independently
indexed in v3:

```bash
mcp-index repository register /abs/a
mcp-index repository register /abs/b
```

**Scope queries per repo** — pass `repository=<name>` (registered name or path) to `search_code` / `symbol_lookup`:

```
search_code(query="def parse", repository="my-repo")
symbol_lookup(symbol="Parser", repository="my-repo")
```

**Python client (beta local API)**: for local scripts and applications on the
same machine as the registered repo, use `mcp_server.client` instead of
starting STDIO or calling FastAPI. MCP tools remain the preferred LLM surface.

```python
from mcp_server.client import open_client
from mcp_server.client_types import ClientSearchOptions

with open_client(workspace_root="/path/to/repo") as client:
    result = client.search_code(
        ClientSearchOptions(
            query="TODO",
            source_type="friction",
            friction_categories=("todo",),
            include_source_metadata=True,
        )
    )
    if result.index_unavailable:
        print(result.index_unavailable.safe_fallback)
    else:
        print(result.results[0].file)
```

The supported Python client surface is local-only: `search_code`,
`symbol_lookup`, `reindex`, and `get_status` share the same readiness-aware
service as the MCP `search_code` tool. It is a local programmatic API, not a
remote service client.

Friction pattern metadata is available as an additive filter on `search_code`.
Use `source_type="friction"` with optional `friction_categories=["todo",
"fixme", "hack", "workaround", "wish", "extraction_hint"]`. Set
`include_source_metadata=true` to attach the stored
`search_source_metadata.v1` envelope to returned results. Ordinary unfiltered
lexical calls keep the legacy result shape; invalid friction categories return
a metadata-only validation error instead of a silent empty result.

Historical GitHub issue context uses the same metadata envelope and search
surface. Run `mcp-index history ingest --repo owner/repo` to ingest
metadata-only issue documents, then filter them with `source_type="history"`
plus optional `history_labels=["reflection"]` or
`history_repos=["owner/repo"]`. The HISTORY contract is fixture-backed in
tests, requires no live GitHub credentials, and does not persist raw issue
bodies by default.

**Index tracking**: each repo's tracked/default branch is followed by
`MultiRepositoryWatcher` (`RefPoller` every 30 s). Same-repo multiple worktrees
and non-default branch queries are unsupported in v3 routing: they return
`index_unavailable` with `safe_fallback: "native_search"` and readiness
remediation instead of reusing another checkout's index. Check `get_status` or
`mcp-index repository list -v` and trust indexed MCP results only when readiness
is `ready`.

**Path sandbox**: tools `search_code`, `symbol_lookup`, `summarize_sample`, and `reindex` reject paths outside `MCP_ALLOWED_ROOTS` with error code `path_outside_allowed_roots`. Registered repo names bypass the check.

**Options:**
- Set `MCP_AUTO_INDEX=false` in the server environment to skip background auto-indexing and call the `reindex` MCP tool manually (recommended for very large repos).
- Add `{"enabled": false}` to `.mcp-index.json` in the target repo to disable indexing for that repo entirely.
- After a full reindex or code changes, call the `reindex` MCP tool to rebuild the index on demand.

**Semantic profiles:** BM25 search requires no extra config. For semantic (vector) search, the server automatically loads `code-index-mcp.profiles.yaml` from its own installation directory — no need to copy it to each repo. To override with a custom profile file, set `MCP_PROFILES_PATH=/abs/path/to/your-profiles.yaml` in the server environment. To override individual endpoint URLs without editing the YAML, use the env vars referenced in the file (e.g. `VLLM_EMBEDDING_BASE_URL`, `VLLM_SUMMARIZATION_BASE_URL`).

### ⚡ Enable Semantic Search

BM25 keyword search works with zero configuration. To add vector (semantic) search, choose one path:

**Option A — Voyage AI (recommended):**
```bash
export VOYAGE_API_KEY=your-key   # free tier available at voyageai.com
```
The `commercial_high` profile activates automatically. Restart the MCP server — the startup log will confirm semantic search is active.

**Option B — Local OSS (Qwen3-Embedding-8B via vLLM, no API key needed):**
```bash
export VLLM_EMBEDDING_BASE_URL=http://localhost:8000/v1
# Start vLLM (requires ~20GB VRAM or shared CPU with --dtype float32):
docker run -p 8000:8000 vllm/vllm-openai --model Qwen/Qwen3-Embedding-8B
```

Both profiles and their collection names are defined in `code-index-mcp.profiles.yaml` and can be customized.

`code-index-mcp.profiles.yaml` is a repo-shipped profile filename, not a pip
install target.

### Costs & Optional Features

The documented container package is `ghcr.io/consiliency/code-index-mcp`.
BM25 code search works without provider credentials. Semantic search, reranking,
artifact sync, and monitoring depend on extras, environment variables, and
service configuration. See [docs/SUPPORT_MATRIX.md](docs/SUPPORT_MATRIX.md)
for language/runtime support details.

## 🚀 Quickstart (Python)

### Prerequisites
- Python 3.12+
- Git

### Installation

#### Option 1: Use the repo-owned source install (Recommended)
```bash
# Clone the repository
git clone https://github.com/ViperJuice/Code-Index-MCP.git
cd Code-Index-MCP

# Install locked project dependencies
uv sync --locked

# Verify the canonical CLI entrypoint
uv run mcp-index --version
```

#### Option 2: Build the local wheel
```bash
# From the repo root
uv run --extra dev python -m build --wheel
python -m pip install dist/index_it_mcp-1.4.1-py3-none-any.whl
index-it-mcp --version
```

The canonical Python distribution remains `index-it-mcp`. The commands above
install this prepared candidate, not the older published release. Use
`pip install index-it-mcp==1.4.1` only after the protected-main publication and
independent delivered-package checks recorded in the release receipt.

### Quick Start After Installation

```bash
# Authenticate GitHub artifact access once
gh auth login

# Check repo/artifact readiness before starting work
mcp-index preflight

# Pull the latest published index baseline for this repo
mcp-index artifact pull --latest

# Reconcile only your local drift after restore
mcp-index artifact sync

# The restored files live locally for MCP runtime use:
# - code_index.db
# - .index_metadata.json
# - vector_index.qdrant/

# Check index status
mcp-index index status

# Start the MCP STDIO runner (primary surface used by LLMs via .mcp.json)
mcp-index stdio

# Or start the FastAPI admin REST gateway (secondary, for diagnostics only;
# this is not the repo's MCP Streamable HTTP transport)
mcp-index serve
mcp-index serve --port 9123   # alternate port
```

From an LLM (Claude Code, Cursor, …) register the STDIO runner in
`.mcp.json` and invoke the indexer as MCP tool calls. The two primary tools
are `search_code` (pattern / keyword / semantic search, <500 ms) and
`symbol_lookup` (exact class/function lookup, <100 ms). Call `get_status` to
confirm repository readiness is `ready`, or handle a query response with
`code: "index_unavailable"` and `safe_fallback: "native_search"` by using native
search while following the returned remediation, such as `reindex`:

```json
{
  "tool": "search_code",
  "arguments": {
    "query": "def parse",
    "limit": 20,
    "semantic": false
  }
}
```

```json
{
  "tool": "symbol_lookup",
  "arguments": {
    "symbol": "parse_file"
  }
}
```

Both tools accept an optional `"repository"` argument (registered repo name or
an absolute path inside `MCP_ALLOWED_ROOTS`) for multi-repo scoping. See the
"Using Against Many Repos" section above. A ready index with no matches returns
ordinary no-match payloads (`results: []` for `search_code` or
`result: "not_found"` for `symbol_lookup`) with readiness metadata; unavailable
indexes return `index_unavailable` instead.

The STDIO `tools/list` surface is deterministic and now advertises richer MCP
metadata for every public tool: stable `title` values, explicit JSON Schema
input contracts (`required`, defaults, and `additionalProperties` posture),
annotations for read-only versus mutating behavior, and implementation-owned
`outputSchema` drafts. The STDIO `tools/call` surface now returns SDK-native
`CallToolResult` objects with object-shaped `structuredContent`, preserved JSON
text fallback content in `content` for older clients, and `isError` on refusal
and error branches. Legacy array-like payloads such as plain lexical search
hits are wrapped under `structuredContent.results`, while readiness failures
still carry `index_unavailable` with `safe_fallback: "native_search"` where
that contract already applied.

`reindex` and `write_summaries` also support task-augmented execution through
the SDK-native MCP tasks surface. Both tools advertise
`execution.taskSupport = "optional"`, so clients can either keep the current
synchronous path or include a `task` object in `tools/call` and then use
`tasks/get`, `tasks/list`, `tasks/result`, and `tasks/cancel` for progress,
terminal payload retrieval, and best-effort cancellation. Readiness refusals,
path sandbox failures, conflicting scope errors, and
summarizer-unavailable preflights still fail synchronously before any task is created.
Whole-repository generation rebuilds, including readiness recovery, require the
synchronous path. A task request for that scope returns `task_scope_unsupported`
without mutation; it never silently runs a long synchronous rebuild instead.

Current verified MCP client posture is summarized in the
[MCP compatibility matrix](docs/status/MCP_COMPATIBILITY_EVALUATION.md). The
phase-owned direct smoke is the official Python SDK over STDIO; Claude Code and
other STDIO launchers are documented against that same server contract, while
remote Streamable HTTP MCP remains deferred.

### 🔧 Configuration

Create a `.env` file for configuration:

```env
# Semantic profile setup — set VOYAGE_API_KEY (free tier at voyageai.com) to enable vector search
VOYAGE_API_KEY=your_api_key_here
# Use 127.0.0.1 for local inference, or a Tailscale/SSH tunnel IP for remote GPUs
OPENAI_API_BASE=http://127.0.0.1:8001/v1
QDRANT_PATH=vector_index.qdrant

# Server settings
MCP_SERVER_HOST=0.0.0.0
MCP_SERVER_PORT=8000
MCP_LOG_LEVEL=INFO

# Workspace settings
MCP_WORKSPACE_ROOT=.
MCP_MAX_FILE_SIZE=10485760  # 10MB

# GitHub Artifact Sync (privacy settings)
MCP_ARTIFACT_SYNC=false  # Set to true to enable
AUTO_UPLOAD=false        # Auto-upload on changes
AUTO_DOWNLOAD=true       # Auto-download on clone
```

Published artifacts now carry the full lexical baseline plus two semantic
profiles:

- `commercial_high` using `voyage-code-3`
- `oss_high` using `Qwen/Qwen3-Embedding-8B`

Those profiles are stored in separate Qdrant collections inside the artifact so
consumers can pull one baseline and use either profile locally.

**Pro Tip: Remote Inference for the Open-Source Profile**
If your local machine lacks the GPU power to run the `oss_high` embedding model locally (e.g., via vLLM or Ollama), you can run inference on a remote machine and point the MCP server to it:
- **Tailscale/VPN:** Set `OPENAI_API_BASE=http://<tailnet-ip>:8001/v1`
- **SSH Tunnel:** Run `ssh -L 8001:localhost:8001 user@remote-gpu-machine`, and the default `127.0.0.1:8001` configuration will tunnel directly to your inference server.

The generated index files are not meant to live in git history. The repo tracks
the code, workflow, and configuration needed to build/publish them; GitHub
artifacts distribute the actual runtime baseline that MCP restores locally.

### Local Workspace Management

```bash
# Inspect all registered repositories and their readiness
mcp-index repository list -v

# Check all registered repos and their local artifact/runtime readiness
mcp-index artifact workspace-status

# Refresh readiness after restoring or rebuilding local indexes
mcp-index artifact reconcile-workspace

# Prepare per-repo local artifact payloads without requiring remote publication
mcp-index artifact publish-workspace
```

MRREADY freezes the rollout-facing interpretation of those commands:

- `mcp-index repository list -v`, `mcp-index repository status`, and
  `mcp-index artifact workspace-status` now surface one rollout status per
  repository: `ready`, `local_only`, `publish_failed`, `wrong_branch`,
  `stale_commit`, `missing_index`, or `partial_index_failure`.
- Query tools remain a separate fail-closed surface. If readiness is not
  `ready`, MCP search returns `index_unavailable` with
  `safe_fallback: "native_search"` instead of treating a status row as a query
  success.
- The current multi-repo verdict remains `controlled rollout only` while the
  multi-repo and STDIO surfaces are still beta.

### 🔐 Privacy & GitHub Artifact Sync

Control how your code index is shared:

```json
// .mcp-index.json
{
  "github_artifacts": {
    "enabled": false,        // Disable sync entirely
    "auto_upload": false,    // Manual upload only
    "auto_download": true,   // Still get team indexes
    "exclude_patterns": [    // Additional exclusions
      "internal/*",
      "proprietary/*"
    ]
  }
}
```

**Privacy Features:**
- Indexes filtered by .gitignore automatically
- Additional patterns via .mcp-index-ignore
- Audit logs show what was excluded
- Sync disabled by default in Docker minimal version

## 🆕 Advanced Features

### Search Result Reranking

Three rerankers are available, configured via the `RERANKER_TYPE` environment variable:

| Value | Reranker | Notes |
|---|---|---|
| `flashrank` | FlashRank | OSS, local, fast (~1–5 ms overhead) |
| `cross-encoder` | Cross-Encoder | OSS, local, highest quality |
| `voyage` | Voyage Reranker | Cloud API, requires `VOYAGE_API_KEY` |
| `none` | Disabled | Default |

```bash
export RERANKER_TYPE=flashrank   # or cross-encoder, voyage, none
```

Reranking applies only to the semantic retrieval path. BM25/FTS results are not reranked.
Implementation: `mcp_server/dispatcher/reranker.py`.

### LLM Chunk Summarization

Semantic chunks can be augmented with LLM-generated summaries before embedding, improving
retrieval of intent-based queries. Configured per-profile in `code-index-mcp.profiles.yaml`:

```yaml
summarization:
  enabled: true
  mode: lazy           # lazy (on first query) | comprehensive (at index time)
  provider: openai_compatible
  model_name: gpt-4o-mini
  base_url: "https://api.openai.com/v1"
  api_key_env: OPENAI_API_KEY
  prompt_template: "Describe this code chunk's inputs, outputs, and purpose in 2 concise sentences."
```

> ⚠️ **Security**: Do not summarize untrusted code with cloud LLMs. Hidden instructions in
> comments can be executed by the summarizer. See the [Security Notes](#-security-notes) section.

Implementation: `mcp_server/indexing/summarization.py`

### Security-Aware Index Sharing

Prevent accidental sharing of sensitive files:

```bash
# Analyze current index for security issues
python scripts/utilities/analyze_gitignore_security.py

# Create secure index export (filters gitignored files)
python scripts/utilities/secure_index_export.py

# The secure export will:
# - Exclude all gitignored files
# - Remove sensitive patterns (*.env, *.key, etc.)
# - Create audit logs of excluded files
```

### BM25 Hybrid Search

Combines traditional full-text search with semantic search:

```python
# The system automatically uses hybrid search when available
# Configure weights in settings:
HYBRID_SEARCH_BM25_WEIGHT=0.3
HYBRID_SEARCH_SEMANTIC_WEIGHT=0.5
HYBRID_SEARCH_FUZZY_WEIGHT=0.2
```

## 🔧 Dispatcher Configuration

### Enhanced Dispatcher (Default)
The enhanced dispatcher includes timeout protection and automatic fallback:

```python
from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher
from mcp_server.storage.sqlite_store import SQLiteStore

store = SQLiteStore(".indexes/YOUR_REPO_ID/current.db")
dispatcher = EnhancedDispatcher(
    sqlite_store=store,
    semantic_search_enabled=True,  # Enable if Qdrant available
    lazy_load=True,               # Load plugins on-demand
    use_plugin_factory=True       # Use dynamic plugin loading
)

# Search with automatic optimization
results = list(dispatcher.search("your query", limit=10))
```

### Simple Dispatcher (Lightweight Alternative)
For maximum performance with BM25-only search:

```python
from mcp_server.dispatcher.simple_dispatcher import create_simple_dispatcher

# Ultra-fast BM25 search without plugin overhead
dispatcher = create_simple_dispatcher(".indexes/YOUR_REPO_ID/current.db")
results = list(dispatcher.search("your query", limit=10))
```

### Configuration Options
Configure dispatcher behavior via environment variables:

```env
# Dispatcher settings
MCP_DISPATCHER_TIMEOUT=5          # Plugin loading timeout (seconds)
MCP_USE_SIMPLE_DISPATCHER=false   # Use simple dispatcher
MCP_PLUGIN_LAZY_LOAD=true        # Load plugins on-demand

# Performance tuning
MCP_BM25_BYPASS_ENABLED=true     # Enable direct BM25 bypass
MCP_MAX_PLUGIN_MEMORY=1024       # Max memory for plugins (MB)

# Auto-indexing (cross-repo use)
MCP_AUTO_INDEX=true               # Set false to skip background auto-index on first run
MCP_AUTO_INDEX_MAX_FILES=100000   # Skip auto-index if repo exceeds this file count
MCP_PROFILES_PATH=                # Absolute path to a custom profiles YAML (overrides built-in)

# Endpoint overrides (no need to edit profiles.yaml)
VLLM_EMBEDDING_BASE_URL=          # Override vLLM embedding endpoint (default: http://ai:8001/v1)
VLLM_SUMMARIZATION_BASE_URL=      # Override summarization endpoint (default: http://win:8002/v1)
```

## 🗂️ Index Management

### Centralized Index Storage

All indexes are now stored centrally at `.indexes/` (relative to the MCP project) for better organization and to prevent accidental commits:

```
.indexes/
├── {repo_hash}/              # Unique hash for each repository
│   ├── main_abc123.db        # Index for main branch at commit abc123
│   ├── main_abc123.metadata.json
│   └── current.db -> main_abc123.db  # Symlink to active index
├── qdrant/                   # Semantic search embeddings
│   └── main.qdrant/          # Centralized Qdrant database
```

**Benefits:**
- Indexes never accidentally committed to git
- Reusable across multiple clones of same repository
- Clear separation between code and indexes
- Automatic discovery based on git remote

**Migration:**
For existing repositories with local indexes:
```bash
python scripts/move_indexes_to_central.py
```

### For This Repository

This project uses GitHub Actions Artifacts for efficient index sharing, so most users start from a published index baseline instead of rebuilding locally.

```bash
# First time setup - pull latest indexes
mcp-index artifact pull --latest

# After pull, reconcile only your branch/worktree drift
mcp-index artifact sync

# Share your indexes with the team
mcp-index artifact push

# Check sync status
mcp-index artifact sync

# Optional: Install git hooks for automatic sync
mcp-index hooks install
# Now indexes upload automatically on git push
# and download automatically on git pull
```

### For ANY Repository (MCP Index Kit)

Enable portable index management in any repository with zero GitHub compute costs:

#### Quick Install

```bash
npm install -g mcp-index-kit
mcp-index init
```

#### How It Works

1. **Zero-Cost Architecture**:
   - All indexing happens on developer machines
   - Indexes stored as GitHub Artifacts (free for public repos)
   - Automatic download on clone, upload on push
   - No GitHub Actions compute required

2. **Portable Design**:
   - Single command setup for any repository
   - Auto-detected by MCP servers and tools
   - Language/runtime behavior follows the explicit support tiers in `docs/SUPPORT_MATRIX.md`
   - Enable/disable per repository

3. **Usage**:
   ```bash
   # Initialize in your repo
   cd your-repo
   mcp-index init

   # Build index locally
   mcp-index build

   # Push to GitHub Artifacts
   mcp-index push

   # Pull latest index
   mcp-index pull

   # Auto sync
   mcp-index sync
   ```

#### Configuration

##### Semantic Search Configuration

To enable semantic search capabilities, you need a Voyage AI API key. Get one from [https://www.voyageai.com/](https://www.voyageai.com/).

**Method 1: Claude Code Configuration (Recommended)**

Create or edit `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "code-index-mcp": {
      "command": "mcp-index",
      "args": ["stdio"],
      "env": {
        "VOYAGE_API_KEY": "your-voyage-ai-api-key-here",
        "SEMANTIC_SEARCH_ENABLED": "true"
      }
    }
  }
}
```

The server label `code-index-mcp` in these examples is a client-local MCP
server ID, not the Python distribution name.

**Method 2: Claude Code CLI**

```bash
claude mcp add code-index-mcp -e VOYAGE_API_KEY=your_key -e SEMANTIC_SEARCH_ENABLED=true -- mcp-index stdio
```

**Method 3: Environment Variables**

```bash
export VOYAGE_API_KEY=your_key
export SEMANTIC_SEARCH_ENABLED=true
```

**Method 4: .env File**

Create a `.env` file in your project root:

```
VOYAGE_API_KEY=your_key
SEMANTIC_SEARCH_ENABLED=true
```

**Check Configuration**

Verify your semantic search setup:

```bash
mcp-index index check-semantic
```

##### Index Configuration

Edit `.mcp-index.json` in your repository:

```json
{
  "enabled": true,
  "auto_download": true,
  "artifact_retention_days": 30,
  "github_artifacts": {
    "enabled": true,
    "max_size_mb": 100
  }
}
```

See [mcp-index-kit](./mcp-index-kit/) for full documentation


# View artifact details
mcp-index artifact info 12345
```

#### Index Management
```bash
# Check index status
mcp-index index status

# Check compatibility
mcp-index index check-compatibility

# Rebuild indexes locally only if artifact sync cannot catch up
mcp-index index rebuild

# Create backup
mcp-index index backup my_backup

# Restore from backup
mcp-index index restore my_backup
```

### GitHub Actions Integration

- **Pull Requests**: Validates developer-provided indexes (no rebuilding)
- **Merges to Main**: Promotes validated indexes to artifacts
- **Cost-Efficient**: Uses free GitHub Actions Artifacts storage
- **Auto-Cleanup**: Old artifacts cleaned up after 30 days

### Storage & Cost

- **GitHub Actions Artifacts**: FREE for public repos, included in private repo quotas
- **Retention**: 7 days for PR artifacts, 30 days for main branch
- **Size Limits**: 500MB per artifact (compressed)
- **Automatic Compression**: ~70% size reduction with tar.gz

### Developer Workflow

1. **Clone Repository**
   ```bash
   git clone https://github.com/yourusername/Code-Index-MCP.git
   cd Code-Index-MCP
   ```

2. **Get Latest Indexes**
   ```bash
    gh auth login
    mcp-index artifact pull --latest
   ```
    - This downloads the current full GitHub artifact snapshot.
    - `mcp-index artifact sync` then reconciles only your local branch/worktree drift when incremental catch-up is appropriate.

3. **Make Your Changes**
   - Edit code as normal
    - Indexes update automatically via file watcher

4. **Share Updates**
   ```bash
   # Your indexes are already updated locally
    mcp-index artifact push
   ```

### Embedding Model Compatibility

The system tracks embedding model versions to ensure compatibility:
- **`commercial_high`**: `voyage-code-3` — 2048 dimensions, dot product, float32
- **`oss_high`**: `Qwen/Qwen3-Embedding-8B` — 4096 dimensions, dot product, l2-normalized
- **Auto-detection**: System checks profile compatibility before download

Multi-profile semantic config can be provided in either:
- `SEMANTIC_PROFILES_JSON` (environment variable), or
- `code-index-mcp.profiles.yaml` (repository root).

### Artifact Strategy

- GitHub artifact pulls are full snapshot downloads, not partial remote patch fetches.
- The current compressed artifact is modest enough that full downloads stay simpler
  than a remote delta protocol.
- Efficiency comes from local incremental indexing after restore:
  - pull the latest full artifact
  - compare the restored artifact commit to local `HEAD`
  - let the watcher or local incremental reindexing reconcile added, modified,
    deleted, and renamed files
- Branch-specific remote artifacts are optional. The default strategy is to use
  the latest `main` artifact as the base and reconcile branch drift locally.

### Easy Semantic Setup (Docker-First)

Run onboarding with automatic local Qdrant startup:

```bash
mcp-index setup semantic
```

Settings precedence (highest to lowest):
1. CLI flags (for one command run)
2. Environment variables / `.env`
3. `code-index-mcp.profiles.yaml`
4. `SEMANTIC_PROFILES_JSON`
5. Built-in defaults

Common controls:

```bash
# Preflight checks only
mcp-index setup semantic --dry-run

# Strict mode: fail command if semantic stack isn't ready
mcp-index setup semantic --strict

# Override local embedding endpoint
mcp-index setup semantic --openai-api-base http://127.0.0.1:8001/v1
```

Plugin loading is auto-optimized by default using fast repository language detection:
- `MCP_AUTO_DETECT_LANGUAGES=true`
- `MCP_LANGUAGE_DETECT_MAX_FILES=5000`
- `MCP_LANGUAGE_DETECT_MIN_FILES=2`

For startup-sensitive environments, enable:
- `MCP_FAST_STARTUP=true` (uses lazy plugin loading and skips file watcher startup)

When `MCP_AUTO_DETECT_LANGUAGES=true`, auto-detection takes precedence over `plugins.yaml`.
Set `MCP_AUTO_DETECT_LANGUAGES=false` to force `plugins.yaml` language selection.

For a dual-profile setup (Voyage + local vLLM/Qwen), set:
- `VOYAGE_API_KEY`
- `OPENAI_API_BASE` (for example `http://127.0.0.1:8000/v1`)
- `OPENAI_API_KEY` (placeholder accepted for local vLLM setups)

If you use a different embedding model, the system will detect incompatibility and rebuild locally with your configuration.

## 💻 Development

### Creating a New Language Plugin

1. **Create plugin structure**
   ```bash
   mkdir -p mcp_server/plugins/my_language_plugin
   cd mcp_server/plugins/my_language_plugin
   touch __init__.py plugin.py
   ```

2. **Implement the plugin interface**
   ```python
   from mcp_server.plugin_base import PluginBase
   
   class MyLanguagePlugin(PluginBase):
       def __init__(self):
           self.tree_sitter_language = "my_language"
       
       def index(self, file_path: str) -> Dict:
           # Parse and index the file
           pass
       
       def getDefinition(self, symbol: str, context: Dict) -> Dict:
           # Find symbol definition
           pass
       
       def getReferences(self, symbol: str, context: Dict) -> List[Dict]:
           # Find symbol references
           pass
   ```

3. **Register the plugin**
   ```python
   # In dispatcher.py
   from .plugins.my_language_plugin import MyLanguagePlugin
   
   self.plugins['my_language'] = MyLanguagePlugin()
   ```

### Running Tests

```bash
# Run all tests
pytest

# Run specific test
pytest test_python_plugin.py

# Measure the current local/offloaded coverage baseline
make coverage-baseline

# Generate the local/offloaded coverage report
make coverage

# Reject tracked or staged generated coverage outputs
make coverage-artifact-guard
```

The COVERAGE contract is local/offloaded first: `make coverage` emits terminal
missing-line output plus `coverage.xml`, and `make agent-full` owns routine
coverage generation. The README badge remains deferred until a trusted event
produces real uploaded evidence.

### Architecture Visualization

```bash
# View C4 architecture diagrams
docker run --rm -p 8080:8080 \
  -v "$(pwd)/architecture":/usr/local/structurizr \
  structurizr/lite

# Open http://localhost:8080 in your browser
```

## Admin REST Interface (secondary)

> The canonical surface is MCP tool calls (`search_code`, `symbol_lookup`,
> etc.) via the STDIO runner — see the "Quick Start" sections above. The
> FastAPI REST gateway documented here is a secondary admin interface for
> diagnostics, scripting, and clients that cannot speak MCP. Its endpoints
> are not the recommended path for LLM-driven workflows.

### Admin REST Endpoints

#### `GET /symbol`
Get symbol definition (admin/debug surface — prefer the `symbol_lookup` MCP tool):
```
GET /symbol?symbol_name=parseFile&file_path=/path/to/file.py
```
Query parameters:
- `symbol_name` (required): Name of the symbol to find
- `file_path` (optional): Specific file to search in

#### `GET /search`
Search for code patterns (admin/debug surface — prefer the `search_code` MCP tool):
```
GET /search?query=async+def.*parse&file_extensions=.py,.js
```
Query parameters:
- `query` (required): Search pattern (regex supported)
- `file_extensions` (optional): Comma-separated list of extensions
- `source_type` (optional): `friction` or `history`
- `friction_categories` (optional): comma-separated friction categories
- `history_labels` (optional): comma-separated history issue labels
- `history_repos` (optional): comma-separated owner/repo filters for history
  issue documents
- `include_source_metadata` (optional): include `search_source_metadata.v1`
  records on matching results

### Python Client API (beta local API)

Use the Python client when you need local programmatic access from the same
machine and registered checkout. Use MCP tools when an assistant needs the
primary LLM tool surface.

```python
from mcp_server.client import open_client
from mcp_server.client_types import ClientSearchOptions

with open_client(workspace_root="/path/to/repo") as client:
    search = client.search_code(ClientSearchOptions(query="Reflection issue"))
    symbol = client.symbol_lookup("IndexItClient")
    status = client.get_status()
```

Readiness remains fail-closed. Non-ready repositories return typed
`index_unavailable` data with `safe_fallback="native_search"` instead of
dispatching against a stale index. The beta Python client intentionally has no
remote service client.

### Response Format

All API responses follow a consistent JSON structure:

**Success Response:**
```json
{
  "status": "success",
  "data": { ... },
  "timestamp": "2024-01-01T00:00:00Z"
}
```

**Error Response:**
```json
{
  "status": "error",
  "error": "Error message",
  "code": "ERROR_CODE",
  "timestamp": "2024-01-01T00:00:00Z"
}
```

## 🚢 Deployment

### Docker Deployment Options

The project includes multiple Docker configurations for different environments:

**Development (Default):**
```bash
# Uses docker-compose.yml + Dockerfile
docker-compose up -d
# - SQLite database
# - Uvicorn development server  
# - Volume mounts for code changes
# - Debug logging enabled
```

**Production:**
```bash
# Uses docker-compose.production.yml + Dockerfile.production
docker-compose -f docker-compose.production.yml up -d
# - PostgreSQL database
# - Gunicorn + Uvicorn workers
# - Multi-stage optimized builds
# - Security hardening (non-root user)
# - Production logging
```

**Enhanced Development:**
```bash
# Uses both compose files with development overrides
docker-compose -f docker-compose.yml -f docker-compose.dev.yml up -d
# - Development base + enhanced debugging
# - Source code volume mounting
# - Read-write code access
```

### Container Restart Behavior

**Important**: By default, `docker-compose restart` uses the **DEVELOPMENT** configuration:
- `docker-compose restart` → Uses `docker-compose.yml` (Development)
- `docker-compose -f docker-compose.production.yml restart` → Uses Production

### Production Deployment

For production environments, we provide:

1. **Multi-stage Docker builds** with security hardening
2. **PostgreSQL database** with async support
3. **Redis caching** for performance optimization
4. **Qdrant vector database** for semantic search
5. **Prometheus + Grafana** monitoring stack
6. **Kubernetes manifests** in `k8s/` directory
7. **nginx reverse proxy** configuration

See our [Deployment Guide](docs/DEPLOYMENT-GUIDE.md) for detailed instructions including:
- Kubernetes deployment configurations
- Auto-scaling setup
- Database optimization
- Security best practices
- Monitoring and observability

### System Requirements

- **Minimum**: 2GB RAM, 2 CPU cores, 10GB storage
- **Recommended**: 8GB RAM, 4 CPU cores, 50GB SSD storage
- **Large codebases**: 16GB+ RAM, 8+ CPU cores, 100GB+ SSD storage

## 📦 Releases & Pre-built Indexes

### Using Pre-built Indexes

For quick setup, download pre-built indexes from our GitHub releases:

```bash
# List available releases
python scripts/download-release.py --list

# Download the current pre-built index artifact
python scripts/download-release.py --latest

# Download specific version
python scripts/download-release.py --tag v2024.01.15 --output ./my-index
```

### Creating Releases

Maintainers can create new releases with pre-built indexes:

```bash
# Prepare locally and reconcile Code-Index-MCP#97 before merging
make agent-gate

# Only after accepted review, merge identity and publication authorization
gh workflow run "Release Automation" --ref main -f mode=publish -f version=v1.4.1 -f auto_merge=false
```

### Automatic Index Synchronization

The project includes Git hooks for automatic index synchronization:
- **Pre-push**: Uploads index changes to GitHub artifacts
- **Post-merge**: Downloads compatible indexes after pulling

Install hooks with: `mcp-index hooks install`

## 🤝 Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details.

### Development Process

1. **Fork the repository**
2. **Create a feature branch** (`git checkout -b feature/amazing-feature`)
3. **Make your changes**
4. **Add tests** (aim for 90%+ coverage)
5. **Update documentation**
6. **Submit a pull request**

### Code Style

- Follow PEP 8 for Python code
- Use type hints for all functions
- Write descriptive docstrings
- Keep functions small and focused

## 📈 Performance

### Benchmarks

| Operation | Performance Target | Current Status |
|-----------|-------------------|----------------|
| Symbol Lookup | <100ms (p95) | ✅ Achieved - All queries < 100ms |
| Code Search | <500ms (p95) | ✅ Achieved - BM25 search < 50ms |
| File Indexing | 10K files/min | ✅ Achieved - 152K files indexed |

### Matrix Benchmark (2026-04-01)

| Metric | BM25-only | voyage-code-3 | Qwen3-Embedding-8B |
|---|:---:|:---:|:---:|
| Top-1 (no reranker) | 12/17 (70.6%) | **17/17 (100%)** | **17/17 (100%)** |
| Top-1 (flashrank)   | 13/17 (76.5%) | **17/17 (100%)** | **17/17 (100%)** |
| Top-1 (cross-encoder) | — | **17/17 (100%)** | **17/17 (100%)** |
| Top-1 (voyage-reranker) | — | 15/17 (88.2%) | — |
| BM25 symbol query p50 | ~1–5 ms | — | — |
| Semantic query p50 (hybrid) | — | ~50–400 ms | ~50–280 ms |

Full results: `docs/benchmarks/matrix_benchmark.md` / `.json`

## 🏗️ Architecture Overview

The system follows C4 model architecture patterns:

- **Workspace Definition**: defined in `architecture/workspace.dsl` and validated with Structurizr CLI
- **System Context (L1)**: Claude Code integrates via MCP sub-agents against the STDIO primary surface
- **Container Level (L2)**: 8 main containers including enhanced MCP server and user documentation
- **Component Level (L3)**: Plugin system, memory management, and cross-repo coordination
- **Code Level (L4)**: 43 PlantUML diagrams documenting all system components and flows

For detailed architectural documentation, see the [architecture/](architecture/) directory.

## 🗺️ Development Roadmap

See [ROADMAP.md](ROADMAP.md) for detailed development plans and current progress.

**Current Status**: 1.4.1 hardening surface prepared; protected-main publication is still pending
- ✅ **Core Indexing**: SQLite + FTS5 for fast local search
- ✅ **Multi-Language**: Specialized and registry-backed language coverage; see `docs/SUPPORT_MATRIX.md`
- ✅ **MCP Protocol**: Verified official Python SDK compatibility over STDIO; see `docs/status/MCP_COMPATIBILITY_EVALUATION.md` for named client posture
- ✅ **Performance**: Sub-100ms queries with BM25 optimization
- 🔄 **Index Sync**: Beta support via GitHub Artifacts
- 🔄 **Semantic Search**: Optional feature requiring Voyage AI API

**Recent Improvements**:
- **⚡ Dispatcher Optimization**: Timeout protection and BM25 bypass for reliability
- **🔄 Hybrid Search**: BM25 + semantic search with graceful degradation
- **📊 Result Ranking**: Improved relevance with score normalization
- **🔧 CLI Tools**: Full-featured `mcp-index` command for index management

### Optimization Tips

Performance optimization features are implemented and available:

1. **Enable caching**: Redis caching is implemented and configurable via environment variables
2. **Adjust batch size**: Configurable via `INDEXING_BATCH_SIZE` environment variable
3. **Use SSD storage**: Improves indexing speed significantly
4. **Limit file size**: Configurable via `INDEXING_MAX_FILE_SIZE` environment variable
5. **Parallel processing**: Multi-worker indexing configurable via `INDEXING_MAX_WORKERS`

## 🔒 Security

- **Local-first**: All processing happens locally by default
- **Path validation**: Prevents directory traversal attacks
- **Input sanitization**: All queries are sanitized
- **Secret detection**: Automatic redaction of detected secrets
- **Plugin isolation**: Plugins run in restricted environments
- **⚠️ Semantic Summary Risks**: If you enable LLM-generated semantic summaries (lazy or comprehensive), be aware of **prompt injection vulnerabilities**. Malicious actors could place hidden instructions in code comments (e.g., in an open-source dependency) that the summarizer LLM might execute. Always review generated index metadata if summarizing untrusted code.

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- [Tree-sitter](https://tree-sitter.github.io/) for language parsing
- [Jedi](https://jedi.readthedocs.io/) for Python analysis
- [FastAPI](https://fastapi.tiangolo.com/) for the API framework
- [Voyage AI](https://www.voyageai.com/) for embeddings
- [Anthropic](https://www.anthropic.com/) for the MCP protocol

## 📬 Contact

- **Issues**: [GitHub Issues](https://github.com/ViperJuice/Code-Index-MCP/issues)
- **Discussions**: [GitHub Discussions](https://github.com/ViperJuice/Code-Index-MCP/discussions)

---

<p align="center">Built with ❤️ for the developer community</p>
