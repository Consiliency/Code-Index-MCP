# Getting Started with Code-Index-MCP

This guide walks you through installing and using Code-Index-MCP to index and search your codebase.

> **Release status**: This guide targets the `1.4.1` prepared candidate.
> Published `1.4.0` does not include the v13 repairs. Use source or a locally
> built wheel until protected-main publication and delivered-artifact acceptance.
> See [release preparation and acceptance](operations/v13-release.md).
> MCP STDIO is the primary LLM surface; FastAPI is secondary admin only.
> Install-surface and language/runtime support tiers are defined in
> [SUPPORT_MATRIX.md](SUPPORT_MATRIX.md); do not treat every install path or
> language row as equivalent support.
>
> **Public alpha repository model**: one server can serve many unrelated
> repositories, with one registered worktree per git common directory. Only the
> tracked/default branch is indexed automatically. Indexed MCP results are
> authoritative only when readiness is `ready`; unavailable indexes return
> `index_unavailable` with `safe_fallback: "native_search"`.

## Prerequisites

- Python 3.12 or higher
- Git (for version control features)
- A code repository to index
- `gh` CLI authenticated with access to the repository artifacts (`gh auth login`)

## Installation

### Option 1: Install from source (Recommended)

```bash
# Clone the repository
git clone https://github.com/ViperJuice/Code-Index-MCP.git
cd Code-Index-MCP

# Install locked project dependencies
uv sync --locked

# Verify the canonical CLI entrypoint
uv run mcp-index --version
```

### Option 2: Build and install the local wheel

```bash
# From the repo root
uv run --extra dev python -m build --wheel
python -m pip install dist/index_it_mcp-1.4.1-py3-none-any.whl
index-it-mcp --version
```

If a Windows checkout still hits path-length limits after you use the cleaned
source tree, note that tracked repo paths are capped by the 160-character tracked-path
limit and the release audit also checks wheel member depth. Use
`git config --global core.longpaths true` only as a fallback for unusually deep
clone locations or third-party tooling.

## Quick Start

### 1. Pull Your Project Baseline Index

Navigate to your project directory and restore the latest published index:

```bash
cd /path/to/your/project

# Check repo/artifact readiness first
mcp-index preflight

# Pull the latest shared artifact baseline
mcp-index artifact pull --latest

# Reconcile only your local changes after restore
mcp-index artifact sync

# Check the index status
mcp-index index status
```

If there is no published artifact yet, fall back to a local rebuild:

```bash
mcp-index index rebuild --force
```

### 2. Start the MCP Server

```bash
# Start the primary MCP STDIO server used by LLM clients
mcp-index stdio

# Or start the secondary FastAPI admin/debug gateway on a less crowded port
mcp-index serve --port 9123
```

The startup surfaces are:
- MCP STDIO on stdin/stdout for LLM client integration
- FastAPI admin/debug HTTP at `http://127.0.0.1:9123` when you start `mcp-index serve`

`mcp-index serve` is an admin/debug gateway, not the repo's MCP Streamable HTTP
transport.
`MCP_CLIENT_SECRET` is a local STDIO handshake guard for `mcp-index stdio`.
The FastAPI gateway uses separate admin/debug bearer token authentication, and
no remote MCP authorization is implemented while remote MCP transport remains
deferred.

### Same-Machine Multi-Repo Setup

If you work across multiple local repositories on one machine, register each
checkout and inspect readiness before starting work. Use one registered
worktree per git common directory; same-repo sibling worktrees and non-default
branch queries are outside the v3 indexed-routing contract:

```bash
export MCP_ALLOWED_ROOTS="/path/to/repo-a:/path/to/repo-b"  # use ; instead of : on Windows
mcp-index repository register /path/to/repo-a
mcp-index repository register /path/to/repo-b
mcp-index repository list -v
mcp-index artifact workspace-status
mcp-index artifact reconcile-workspace
```

Newly registered repos usually start with `artifact_health: missing` until you
restore local runtime files with `mcp-index artifact pull --latest` or build a
local index with `mcp-index repository sync`.
Use `mcp-index repository list -v` or the MCP `get_status` tool before trusting
indexed results. If `search_code` or `symbol_lookup` returns
`index_unavailable` with `safe_fallback: "native_search"`, use native search
while following the returned readiness remediation.

### 3. Search Your Code

The primary interface is the Model Context Protocol: your LLM (Claude Code,
Cursor, etc.) calls `search_code` and `symbol_lookup` as MCP tools. A FastAPI
admin surface is also available for manual debugging (see below).

For the current named-client posture, see
`docs/status/MCP_COMPATIBILITY_EVALUATION.md`. MCPEVAL's direct client proof is
the official Python SDK smoke over STDIO; other STDIO launchers are expected to
work only insofar as they honor that same MCP contract.

**Via MCP Protocol (primary):**

First, register the server in your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "code-index": {
      "command": "mcp-index",
      "args": ["stdio"],
      "cwd": "/path/to/your/project"
    }
  }
}
```

Then the LLM (or any MCP client) invokes the indexer via tool calls. Example
JSON for a pattern/keyword search:

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

Example JSON for an exact-name symbol lookup:

```json
{
  "tool": "symbol_lookup",
  "arguments": {
    "symbol": "parse_file"
  }
}
```

Both tools accept an optional `repository` argument (a registered repo name or
an absolute path inside `MCP_ALLOWED_ROOTS`) to scope the query in a multi-repo
setup. See the multi-repo section above for registration.

If you set `MCP_CLIENT_SECRET`, clients must call the `handshake` tool first on
the local STDIO session. That local STDIO handshake guard does not replace the
gateway's admin/debug bearer token authentication, and no remote MCP
authorization is implemented while `mcp-index serve` remains a non-MCP admin
surface.

**Via REST API (admin/debug):**

The FastAPI gateway exposes the same operations as an HTTP admin surface for
diagnostics and scripts that cannot speak MCP. It is a secondary surface; use
the MCP tool calls above for normal LLM-driven workflows. See the
[API Reference in the README](../README.md#-admin-rest-interface-secondary)
for endpoint details.

## Configuration

### Environment Variables

Create a `.env` file in your project root:

```env
# Server settings
MCP_SERVER_HOST=127.0.0.1
MCP_SERVER_PORT=8000
MCP_LOG_LEVEL=INFO

# Index settings
MCP_WORKSPACE_ROOT=.
MCP_MAX_FILE_SIZE=10485760
```

### Ignore Patterns

Create `.mcp-index-ignore` to exclude files from indexing:

```
# Ignore patterns (gitignore syntax)
node_modules/
*.min.js
dist/
build/
.venv/
```

## Optional: Semantic Search

For AI-powered semantic search, install with semantic support:

```bash
uv sync --locked --extra semantic
```

Then configure Voyage AI:

```env
# Add to .env
VOYAGE_API_KEY=your_api_key_here
SEMANTIC_SEARCH_ENABLED=true
```

Get a free API key at [voyageai.com](https://www.voyageai.com/).

## CLI Commands Reference

```bash
# Index management
mcp-index index status          # Show index status
mcp-index index rebuild         # Rebuild the index
mcp-index index check-compatibility  # Check index compatibility

# Artifact sync (recommended)
mcp-index artifact pull --latest    # Download team index baseline
mcp-index artifact sync             # Reconcile local drift after restore
mcp-index artifact push             # Publish a refreshed index baseline

# Repository
mcp-index repository status     # Show repository info
```

## Troubleshooting

### Index not found

```bash
# Pull the team baseline first, then rebuild only if needed
mcp-index artifact pull --latest
mcp-index artifact sync

# Fallback if no artifact is available or drift is too large
mcp-index index rebuild --force
```

### Artifact pull fails

Check GitHub authentication and repository remote:

```bash
gh auth status
git remote get-url origin
```

### Preflight warns that you are behind remote

```bash
git pull --rebase
mcp-index artifact sync
```

### Preflight warns that local runtime files are missing

```bash
mcp-index artifact pull --latest
```

### Server won't start

Check if the port is in use:
```bash
lsof -i :8000
```

### Slow indexing

For large repositories, try:
```bash
# Use SQL-only mode (faster, no semantic search)
mcp-index index rebuild --mode sql
```

## Next Steps

- Read the [full documentation](https://github.com/ViperJuice/Code-Index-MCP#readme)
- Configure [semantic search](https://github.com/ViperJuice/Code-Index-MCP#semantic-search-configuration)
- Set up [team sync](https://github.com/ViperJuice/Code-Index-MCP#github-actions-integration)

## Support

- **Issues**: [GitHub Issues](https://github.com/ViperJuice/Code-Index-MCP/issues)
- **Discussions**: [GitHub Discussions](https://github.com/ViperJuice/Code-Index-MCP/discussions)
