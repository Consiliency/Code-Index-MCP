# MCP Server Agent Configuration

This file defines the capabilities and constraints for AI agents working with this codebase.

## Current State

**System Complexity**: 5/5 (High — SQLite FTS5 + Qdrant vector index, 48 language plugins, rerankers, query-intent routing)
**MCP Status**: Use MCP indexed search when repository readiness is `ready`; STDIO is the primary surface
**Last Updated**: 2026-04-23
**Support matrix**: Customer-facing language/runtime support claims live in `docs/SUPPORT_MATRIX.md`.
**Dependency truth**: Use `uv sync --locked`; `pyproject.toml` and `uv.lock` are canonical.

**Current verification boundary**: `docs/status/V13_EXECUTION.md` tracks active
hardening. Language registry inventory and earlier implementation summaries
are not installed-runtime, platform, or fleet acceptance. Keep the existing
support tiers in `docs/SUPPORT_MATRIX.md` and record untested combinations.

> **Beta status**: Multi-repo support and the STDIO interface are in beta. STDIO is the primary surface for LLM tool calls; FastAPI is a secondary admin surface for diagnostics and manual operations. Expect API surface changes before stable release.
>
> **Public alpha repository model**: v3 supports many unrelated repositories on
> one machine, with one registered worktree per git common directory. Only the
> tracked/default branch is indexed automatically. Indexed results are
> authoritative only when readiness is `ready`; unavailable indexes return
> `index_unavailable` with `safe_fallback: "native_search"`.

### What's Actually Implemented
- ✅ STDIO transport (`search_code`, `symbol_lookup`, `summarize_sample`, `reindex` MCP tools)
- ✅ FastAPI admin surface with endpoints: `/symbol`, `/search`, `/status`, `/plugins`, `/reindex`
- ✅ Dispatcher with caching and auto-initialization
- ✅ Python plugin fully functional with Tree-sitter + Jedi
- ✅ JavaScript/TypeScript plugin fully functional with Tree-sitter
- ✅ C plugin fully functional with Tree-sitter
- ✅ SQLite persistence layer with FTS5 search
- ✅ File watcher auto-starts in `initialize_services()` after dispatcher is ready; stopped in `main()` finally block on exit (EnhancedDispatcher mode only)
- ✅ Error handling and logging framework
- ✅ Comprehensive testing framework (pytest with fixtures)
- ✅ CI/CD pipeline with GitHub Actions
- ✅ Docker support and build system

### What's Recently Implemented
- ✅ C++, HTML/CSS, and Dart plugins fully functional with Tree-sitter
- ✅ Advanced metrics collection with Prometheus
- ✅ Security layer with JWT authentication
- ✅ Comprehensive testing framework with parallel execution
- ✅ Docker and Kubernetes configurations (beta hardening in progress)
- ✅ Cache management and query optimization
- ✅ Real-world repository testing validation

## Agent Capabilities

### Code Understanding
- Parse and understand the intended architecture
- Navigate plugin structure (though most are stubs)
- Interpret C4 architecture diagrams
- Understand the gap between design and implementation

### Code Modification
- Add new language plugin stubs
- Extend API endpoint definitions
- Update architecture diagrams
- Implement missing functionality

### Testing & Validation
- Run basic test files (`test_python_plugin.py`, `test_tree_sitter.py`)
- Validate TreeSitter functionality
- Check architecture consistency
- Identify implementation gaps

## MCP SEARCH STRATEGY (CRITICAL)

### Use Indexed Search When Readiness Is Ready
The codebase can use a pre-built index across 48 languages. Before treating
indexed results as authoritative, check `mcp__code-index-mcp__get_status()` for
repository readiness `ready` or honor the query tool response. If `search_code`
or `symbol_lookup` returns `code: "index_unavailable"` with
`safe_fallback: "native_search"`, use native `rg`/file tools and follow the
readiness remediation, usually `reindex`.

### Tool Priority Order:
1. **mcp__code-index-mcp__symbol_lookup** - For finding definitions when readiness is `ready`
   - Use for: Classes, functions, methods, variables
   - Returns: Exact location, signature, documentation
   - Speed: <100ms
   - `result: "not_found"` from a ready index means no symbol match; `index_unavailable` means use `native_search`
   - Example: `mcp__code-index-mcp__symbol_lookup(symbol="PluginManager")`

2. **mcp__code-index-mcp__search_code** - For pattern/content search when readiness is `ready`
   - Use for: Code patterns, text search, semantic queries
   - Supports: Regex, semantic search with semantic=true
   - Speed: <500ms, returns ranked results with line numbers and `last_indexed` timestamp
   - `results: []` from a ready index means no code match; `index_unavailable` means use `native_search`
   - Example: `mcp__code-index-mcp__search_code(query="def.*process", limit=10)`
   - Semantic: `mcp__code-index-mcp__search_code(query="authentication flow", semantic=true)`

3. **Native tools (`rg`, file reads)** - Safe fallback for non-ready indexes
   - Use when MCP tools are unavailable or return `safe_fallback: "native_search"`
   - Use for reading specific files after indexed search identifies candidates
   - Use while `reindex` or other readiness remediation is pending

### Examples:
Check readiness first:
`mcp__code-index-mcp__get_status()`

When readiness is `ready`:
`mcp__code-index-mcp__search_code(query="class.*Plugin")`

When `index_unavailable` is returned:
use `rg` or file tools and follow the returned remediation.

### Performance Impact:
- Traditional grep through 312 files: ~45 seconds
- MCP indexed search: <0.5 seconds
- Speedup: 100x faster minimum

### Additional MCP Tools:
- **mcp__code-index-mcp__get_status** - Check index health
- **mcp__code-index-mcp__list_plugins** - See all 48 supported languages
- **mcp__code-index-mcp__reindex** - Update index after changes

### Custom Slash Commands Available:
- **/find-symbol** - Quick symbol lookup using MCP
- **/search-code** - Pattern search using MCP index
- **/mcp-tools** - Complete MCP tools reference

These commands are readiness-aware quick references and are available in `.claude/commands/`

## Agent Constraints

1. **Implementation Gaps**
   - Most core components are operational (SQLite, FTS5, dispatcher, file watcher, 48-language plugins)
   - ~~The dispatcher doesn't route to plugins properly~~ (FIXED - Enhanced dispatcher working)
   - ~~No actual indexing or storage occurs~~ (FIXED - SQLite + optional Qdrant storage)
   - ~~Search functionality returns empty results~~ (FIXED - Full MCP search operational)

2. **Local-First Priority**
   - Design for local indexing (when implemented)
   - Maintain offline functionality goals
   - Minimize external dependencies

3. **Plugin Architecture**
   - Follow plugin base class requirements
   - Maintain language-specific conventions
   - Preserve plugin isolation design

4. **Security**
   - No hardcoded credentials
   - Respect file system permissions
   - Validate all external inputs

5. **Performance**
   - Consider indexing speed in future implementations
   - Plan for efficient memory usage
   - Design efficient file watching

## Multi-Repo Operation

### Repo Identity

`compute_repo_id()` (`mcp_server/storage/repo_identity.py`) uses `git rev-parse --git-common-dir` (Tier 1) to derive a stable `repo_id`. All worktrees of the same repository share a single `repo_id`; switching branches does NOT change `repo_id`. Query tools classify sibling worktrees with the P27 unsupported-worktree readiness contract and return `index_unavailable` with `safe_fallback: "native_search"` instead of dispatching against the registered checkout's index. The result is stored in a `RepoContext` frozen dataclass (`mcp_server/core/repo_context.py`) that captures all per-repo runtime state.

### Default-Branch Policy

`RepositoryRegistry.register_repository()` (`mcp_server/storage/repository_registry.py`) infers the default branch from `origin/HEAD` or falls back to `main`. Non-default branches are NOT indexed automatically — `MultiRepositoryWatcher` (`mcp_server/watcher_multi_repo.py`) runs a `RefPoller` (`mcp_server/watcher/ref_poller.py`) per registered repo at a 30-second cadence that only tracks the recorded default branch.

### MRREADY Rollout Gate

Treat repository and workspace status surfaces as rollout guidance, not query
results:

- `mcp-index repository list -v`, `mcp-index repository status`, and
  `mcp-index artifact workspace-status` surface rollout status values such as
  `ready`, `local_only`, `publish_failed`, `wrong_branch`,
  `partial_index_failure`, `stale_commit`, and `missing_index`.
- Query tools stay fail-closed and separate. Non-ready query paths still return
  `index_unavailable` with `safe_fallback: "native_search"`.
- The current operator verdict is `controlled rollout only` because multi-repo
  and STDIO remain beta even when a repository reports `ready`.

### Path Sandbox

Set `MCP_ALLOWED_ROOTS=/path/a:/path/b` using the OS path separator (`:` on Unix, `;` on Windows) to restrict which paths the server may index or read. Tools `search_code`, `symbol_lookup`, `summarize_sample`, and `reindex` reject any path outside the allowlist with uniform error code `path_outside_allowed_roots`. Registered repo *names* (not paths) bypass the path check.

### Client Auth (Optional)

Set `MCP_CLIENT_SECRET=<shared-secret>` to require a `handshake` tool call before any other tool. `HandshakeGate` (`mcp_server/cli/handshake.py`) uses `hmac.compare_digest` for constant-time comparison. When `MCP_CLIENT_SECRET` is unset the server logs `running unauthenticated — MCP_CLIENT_SECRET not set` at startup.

### Security Hardening (P15)

Phase 15 introduced defense-in-depth hardening: plugin sandboxing (isolated worker processes with capability-based restrictions), artifact attestation (SLSA signatures on GitHub), metrics endpoint authentication (bearer token), path traversal guard validation (search results respect `MCP_ALLOWED_ROOTS`), and token scope validation at startup (five required GitHub scopes). For details on operator knobs and threat model, see `docs/security/` (sandbox.md, attestation.md, path-guard.md, token-scopes.md) and `docs/operations/user-action-runbook.md` §3.4. Agents should respect the documented security posture when operating this server — plugin sandboxing is always-on by default, and attestation mode defaults to `enforce` in production.

### Components

| Component | Path | Role |
|---|---|---|
| `RepoContext` | `mcp_server/core/repo_context.py` | Frozen per-repo runtime state |
| `StoreRegistry` | `mcp_server/storage/store_registry.py` | Thread-safe per-repo SQLite store cache |
| `MultiRepositoryWatcher` | `mcp_server/watcher_multi_repo.py` | Orchestrates `RefPoller` per registered repo |
| `initialize_stateless_services()` | `mcp_server/cli/bootstrap.py:25-64` | In-process boot helper; returns `(StoreRegistry, RepoResolver, dispatcher, RepositoryRegistry, GitAwareIndexManager)` |

### Cross-Repo Search Architecture (P14)

`CrossRepoCoordinator` (`mcp_server/dispatcher/cross_repo_coordinator.py`) drives all multi-repo search. Key P14 additions:

- **Reranker wiring** (IF-0-P14-1): `__init__` accepts an injected `IReranker` (defined in `mcp_server/indexer/reranker.py`). When none is injected and `enable_reranking=True`, it falls back to `RerankerFactory.create_default()` wrapped in a try/except so a missing factory degrades gracefully to `reranker=None`.
- **Dependency graph** (IF-0-P14-2): `_get_repository_dependencies(repo_id)` now returns resolved repo IDs via the `mcp_server/dependency_graph/` package (`parsers.py`, `aggregator.py`, ecosystem parsers for Python/npm/Go/Cargo).
- **Schema migration** (IF-0-P14-3): Every artifact manifest carries a `schema_version` field. `mcp_server/storage/schema_migrator.py::SchemaMigrator.apply(from_version, to_version, db_path)` runs upgrade migrations; `mcp_server/artifacts/artifact_download.py::check_compatibility` raises `UnknownSchemaVersionError` for unrecognised versions.
- **Auto-delta artifacts** (IF-0-P14-4): `mcp_server/artifacts/delta_policy.py::DeltaPolicy.should_publish_delta(full_size_bytes, last_artifact)` gates delta mode in `ArtifactPublisher.publish_on_reindex`. Threshold controlled by `MCP_ARTIFACT_FULL_SIZE_LIMIT` (default `524288000` bytes / 500 MB).
- **Watcher sweep** (IF-0-P14-5): `mcp_server/watcher/sweeper.py::WatcherSweeper` runs a full-tree scan every `MCP_WATCHER_SWEEP_MINUTES` (default 60) to recover inotify/FSEvents drops. `mcp_server/watcher_multi_repo.py` starts/stops the sweeper. `move_file` in `dispatcher_enhanced.py` is wrapped in `two_phase_commit` and now raises `IndexingError` on semantic failure (previously a silent log).

### Setup Steps

1. Set `MCP_ALLOWED_ROOTS` to include all repo parent directories.
2. Optionally set `MCP_CLIENT_SECRET` for authentication.
3. Start the server: `op run --env-file=.mcp.env -- python -m mcp_server.cli.server_commands`
4. Register each repo: `mcp-index repository register <path>`
5. Pass `repository=<name>` to `search_code` / `symbol_lookup` to scope queries per repo.

## ESSENTIAL_COMMANDS

```bash
# Build & Install
uv sync --locked                # Install locked dependencies
make install                    # Project install helper

# Testing
make test                       # Run unit tests
make test-all                   # Run all tests with coverage
make coverage                   # Generate coverage report
make benchmark                  # Run performance benchmarks

# Code Quality
make lint                       # Run linters (black, isort, flake8, mypy, pylint)
make format                     # Format code (black, isort)
make security                   # Run security checks (safety, bandit)

# Development
uvicorn mcp_server.gateway:app --reload --host 0.0.0.0 --port 8000
make clean                      # Clean up temporary files

# Docker
make docker                     # Build Docker image

# Architecture
docker run --rm -p 8080:8080 -v "$(pwd)/architecture":/usr/local/structurizr structurizr/lite

# MCP Search Commands (check readiness first)
# Find symbol definition
mcp__code-index-mcp__symbol_lookup(symbol="ClassName")

# Search code patterns
mcp__code-index-mcp__search_code(query="def process_.*", limit=10)

# Semantic search
mcp__code-index-mcp__search_code(query="error handling logic", semantic=true)

# Check index status
mcp__code-index-mcp__get_status()

# List all language plugins
mcp__code-index-mcp__list_plugins()
```

## Development Priorities

### 🔴 CRITICAL_PRIORITIES (Immediate, Complexity 5)

### IMMEDIATE_PRIORITIES (This Week, Complexity 3-4)
1. **Document processing validation** - Complete testing and documentation (BLOCKING: production claims)
2. **Performance benchmarks** - Publish existing results (SUPPORTING: production readiness)
3. **Documentation cleanup** - Move status reports to docs/status/ (IMPACT: professional presentation)
4. **Legal compliance** - Verify LICENSE and CODE_OF_CONDUCT.md are properly referenced (BLOCKING: distribution)

### SHORT_TERM_PRIORITIES (Next Sprint, Complexity 2-3)
1. **Production deployment automation** - Complete deployment scripts (COMPLETING: Phase 4)
2. **Architecture diagram updates** - Align with current implementation (MAINTAINING: documentation quality)
3. **Monitoring framework** - Implement production monitoring (ENABLING: operations)
4. **User documentation** - Create comprehensive user guides (SUPPORTING: adoption)

### ARCHITECTURAL_DECISIONS_NEEDED
- Container orchestration platform selection (Kubernetes vs Docker Swarm vs other)
- Monitoring and alerting framework (Prometheus vs other)
- User interface approach (Web UI vs CLI-only vs API-first)

### INTERFACE-FIRST_DEVELOPMENT_SEQUENCE
**Follow ROADMAP.md Next Steps hierarchy**:
1. Container Interface Definition (Priority: HIGHEST, Complexity: 4)
2. External Module Interfaces (Priority: HIGH, Complexity: 3)
3. Intra-Container Module Interfaces (Priority: MEDIUM, Complexity: 2)
4. Implementation details (Priority: FINAL, Complexity varies)

## Architecture Context

The codebase follows C4 architecture model with comprehensive diagrams:
- **Structurizr DSL files**: Define system context, containers, and components (85% implemented)
- **PlantUML files**: Detailed component designs in architecture/level4/ (22 diagrams, 90% coverage)
- **Architecture vs Implementation**: 85% alignment (strong improvement from previous 20%)
- **Implementation Status**: Core functionality operational with production infrastructure

**ARCHITECTURE_IMPLEMENTATION_ALIGNMENT**: STRONG (85%)
✅ **ALIGNED_COMPONENTS**:
- Plugin Factory pattern → GenericTreeSitterPlugin + 48 languages implemented
- Enhanced Dispatcher → Caching, routing, error handling operational  
- Storage abstraction → SQLite + FTS5 with optional Qdrant integration
- API Gateway → FastAPI with all documented endpoints functional
- File Watcher → Real-time monitoring with Watchdog implemented

⚠️ **RECENTLY_IMPLEMENTED** (validation needed):
- Document Processing plugins → Markdown/PlainText created, testing in progress
- Specialized Language plugins → 7 plugins implemented, production validation needed
- Semantic Search integration → Voyage AI integrated with graceful fallback

❌ **GAPS_IDENTIFIED**:
- Performance benchmarks → Framework exists but results unpublished
- Production deployment → Docker configs exist but automation incomplete
- Some PlantUML diagrams → Need updates to match latest implementations

## CODE_STYLE_PREFERENCES

```python
# Discovered from pyproject.toml and Makefile
# Formatting: black + isort
# Linting: flake8 + mypy + pylint
# Type hints: Required for all functions
# Docstrings: Required for public APIs

# Function naming (discovered patterns)
def get_current_user(request: Request) -> TokenData:
def cache_symbol_lookup(query_cache: QueryResultCache):
def require_permission(permission: Permission):

# Class naming (discovered patterns)  
class FileWatcher:
class AuthenticationError(Exception):
class SecurityError(Exception):

# File naming patterns
# test_*.py for tests
# *_manager.py for managers
# *_middleware.py for middleware
```

## ARCHITECTURAL_PATTERNS

```python
# MCP Search Pattern: use indexed MCP search when readiness is ready
# Step 1: Check get_status readiness or handle index_unavailable/native_search
# Step 2: Search with MCP when ready
results = mcp__code-index-mcp__search_code(query="pattern")
# Step 3: Read specific files from results
for result in results:
    content = read_file(result['file_path'])

# Plugin Pattern: All language plugins inherit from PluginBase
class LanguagePlugin(PluginBase):
    def index(self, file_path: str) -> Dict
    def getDefinition(self, symbol: str, context: Dict) -> Dict
    def getReferences(self, symbol: str, context: Dict) -> List[Dict]

# MCP STDIO tools — primary surface for LLM tool calls
# search_code(query, repository=None) -> list[SearchResult]
# symbol_lookup(symbol, repository=None) -> SymbolResult
# summarize_sample(path) -> str
# reindex(repository=None) -> ReindexResult

# Tree-sitter Integration: Use TreeSitterWrapper for parsing
from mcp_server.utils.treesitter_wrapper import TreeSitterWrapper

# Error Handling: All functions return structured responses
{"status": "success|error", "data": {...}, "timestamp": "..."}

# Testing: pytest with fixtures; normal discovery enforces the current 35% floor.
# 80% is a target, not measured or enforced by the bounded reporting commands.
def test_plugin_functionality(plugin_fixture):
```

## NAMING_CONVENTIONS

```bash
# Functions: snake_case
get_current_user, cache_symbol_lookup, require_permission

# Classes: PascalCase
FileWatcher, AuthenticationError, PluginBase

# Files: snake_case.py
gateway.py, plugin_manager.py, security_middleware.py

# Tests: test_*.py
test_python_plugin.py, test_dispatcher.py, test_gateway.py

# Directories: snake_case
mcp_server/, plugin_system/, tree_sitter_wrapper/
```

## DEVELOPMENT_ENVIRONMENT

```bash
# Python Version: 3.12+ (from pyproject.toml requires-python = ">=3.12")
# Virtual Environment: Required (managed by uv)
uv sync --locked --extra dev  # install locked core + development dependencies
# Or for a specific extras set:
uv sync --locked --extra dev --extra semantic

# Pre-commit: Configured for linting and formatting
make lint     # Verify before committing
make format   # Auto-format code

# IDE Setup: VS Code recommended (if .vscode/ exists)
# Extensions: Python, Pylance, Black Formatter
```

## TEAM_SHARED_PRACTICES

```bash
# Testing: Always run tests before committing
make test-all

# Documentation: Update AGENTS.md when adding new patterns
# Plugin Development: Follow established PluginBase interface  
# Error Messages: Include context and suggested fixes
# Performance: Target <100ms symbol lookup, <500ms search

# Code Review: Focus on
# - Type hints for all functions
# - Comprehensive error handling  
# - Coverage scope and exclusions reported honestly; do not lower existing gates
# - Documentation updates
``` 
## DOCUMENTATION_MAINTENANCE_COMMANDS
Custom commands for documentation maintenance:
- `/project:analyze-docs` - Analyze documentation and architecture state
- `/project:update-docs` - Update documentation per analysis recommendations

**Documentation**:
- Implementation details: `/docs/tools/documentation-commands.md`
- Workflow guide: `/docs/guides/documentation-workflow.md`
- Roadmap template: `/docs/templates/roadmap-next-steps-template.md`

**Usage**: Run these commands at the start of each development iteration to ensure documentation alignment.
