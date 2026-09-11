"""P2B bootstrap entry — IF-0-P2B-4 implementation.

Constructs the process-wide service pool: StoreRegistry + RepoResolver + dispatcher +
RepositoryRegistry + GitAwareIndexManager.
No cwd capture, no preloaded sqlite_store.

Every MCP tool call resolves a RepoContext per-request via RepoResolver.
"""

from __future__ import annotations

import os
import signal
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Tuple

from mcp_server.core.repo_resolver import RepoResolver
from mcp_server.dispatcher.protocol import DispatcherProtocol
from mcp_server.storage.git_index_manager import GitAwareIndexManager
from mcp_server.storage.repository_registry import RepositoryRegistry
from mcp_server.storage.store_registry import StoreRegistry


def initialize_stateless_services(
    registry_path: Optional[Path] = None,
) -> Tuple[
    StoreRegistry, RepoResolver, DispatcherProtocol, RepositoryRegistry, GitAwareIndexManager
]:
    """Construct the process-wide service pool.

    Returns (StoreRegistry, RepoResolver, dispatcher, RepositoryRegistry, GitAwareIndexManager).
    The dispatcher holds no per-repo state; every public method takes ``ctx: RepoContext``.
    ``registry_path`` overrides the default registry location (mainly for tests).
    """
    reset_process_singletons()

    import os

    from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher
    from mcp_server.dispatcher.simple_dispatcher import SimpleDispatcher
    from mcp_server.utils.semantic_indexer_registry import SemanticIndexerRegistry

    # Build registry + store registry
    resolved_registry_path = registry_path or _default_registry_path()
    repo_registry = RepositoryRegistry(registry_path=resolved_registry_path)
    store_registry = StoreRegistry.for_registry(repo_registry)
    repo_resolver = RepoResolver(registry=repo_registry, store_registry=store_registry)

    # Construct dispatcher — no sqlite_store, no cwd
    use_simple = os.getenv("MCP_USE_SIMPLE_DISPATCHER", "false").lower() == "true"
    if use_simple:
        dispatcher: DispatcherProtocol = SimpleDispatcher()
    else:
        _explicit = os.getenv("RERANKER_TYPE", "").strip().lower()
        reranker_type = _explicit if _explicit else "none"
        semantic_enabled = os.getenv("SEMANTIC_SEARCH_ENABLED", "true").lower() == "true"
        semantic_registry = SemanticIndexerRegistry(repo_registry) if semantic_enabled else None
        dispatcher = EnhancedDispatcher(
            enable_advanced_features=True,
            use_plugin_factory=True,
            lazy_load=True,
            semantic_search_enabled=semantic_enabled,
            memory_aware=True,
            multi_repo_enabled=None,
            reranker_type=reranker_type,
            semantic_indexer_registry=semantic_registry,
        )

    git_index_manager = GitAwareIndexManager(
        registry=repo_registry,
        dispatcher=dispatcher,
        repo_resolver=repo_resolver,
        store_registry=store_registry,
    )

    return store_registry, repo_resolver, dispatcher, repo_registry, git_index_manager


def reset_process_singletons() -> None:
    """Null all module-level process singletons; tolerates pruned installs."""
    # Metrics have process lifetime, not bootstrap/repository lifetime. Resetting
    # their owner leaks listeners and re-registers duplicate shared collectors.
    try:
        import mcp_server.gateway as _m

        setattr(_m, "_repo_registry", None)
    except ImportError:
        pass

    try:
        import mcp_server.plugin_system.loader as _m

        setattr(_m, "_loader", None)
    except ImportError:
        pass

    try:
        import mcp_server.plugin_system.discovery as _m

        setattr(_m, "_discovery", None)
    except ImportError:
        pass

    try:
        import mcp_server.plugin_system.config as _m

        setattr(_m, "_config_manager", None)
    except ImportError:
        pass

    try:
        import mcp_server.plugins.memory_aware_manager as _m

        setattr(_m, "_manager_instance", None)
    except ImportError:
        pass

    try:
        import mcp_server.storage.multi_repo_manager as _m

        setattr(_m, "_manager_instance", None)
    except ImportError:
        pass

    try:
        import mcp_server.dispatcher.cross_repo_coordinator as _m

        setattr(_m, "_coordinator_instance", None)
    except ImportError:
        pass

    try:
        import mcp_server.plugins.repository_plugin_loader as _m

        setattr(_m, "_loader_instance", None)
    except ImportError:
        pass


def _default_registry_path() -> Path:
    env_path = os.environ.get("MCP_REPO_REGISTRY")
    if env_path:
        return Path(env_path)
    return Path.home() / ".mcp" / "repository_registry.json"


def _allowed_roots() -> list[Path]:
    """Resolve the list of directories that reindex/read tools may touch.

    Precedence: MCP_ALLOWED_ROOTS (os.pathsep-separated) > MCP_WORKSPACE_ROOT > cwd.
    """
    from mcp_server.security.path_allowlist import parse_allowed_roots

    raw = os.environ.get("MCP_ALLOWED_ROOTS", "").strip()
    if raw:
        roots = list(parse_allowed_roots(raw))
        if roots:
            return roots
    ws = os.environ.get("MCP_WORKSPACE_ROOT", "").strip()
    if ws:
        try:
            return [
                Path(ws).expanduser().resolve()
            ]  # resolve() exempt: boot-time root canonicalization
        except Exception:
            pass
    return [Path.cwd().resolve()]


def _path_within_allowed(target: Path, roots: list[Path]) -> bool:
    from mcp_server.security.path_allowlist import path_within_allowed

    return path_within_allowed(target, tuple(roots))


def validate_index(store, repo_path: Path) -> dict:
    """Validate that the index is up-to-date and accessible."""
    issues = []
    stats = {}

    try:
        conn = sqlite3.connect(store.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM files")
        total_files = cursor.fetchone()[0]
        stats["total_files"] = total_files

        cursor.execute("SELECT COUNT(*) FROM bm25_content")
        bm25_count = cursor.fetchone()[0]
        stats["bm25_documents"] = bm25_count

        cursor.execute(
            "SELECT COUNT(*) FROM bm25_content WHERE filepath LIKE ?",
            (f"{repo_path}%",),
        )
        current_repo_files = cursor.fetchone()[0]
        stats["current_repo_files"] = current_repo_files

        cursor.execute(
            "SELECT COUNT(*) FROM bm25_content WHERE filepath LIKE 'PathUtils.get_workspace_root() / %'"
        )
        docker_files = cursor.fetchone()[0]
        stats["docker_files"] = docker_files

        if total_files == 0:
            issues.append("Index is empty - no files indexed")
        if bm25_count == 0:
            issues.append("No BM25 documents in index")
        if current_repo_files == 0 and docker_files > 0:
            issues.append(
                f"Index contains {docker_files} Docker paths but no files from current repo"
            )

        cursor.execute("SELECT filepath FROM bm25_content LIMIT 10")
        sample_paths = cursor.fetchall()
        inaccessible_count = 0
        for (path,) in sample_paths:
            if not Path(path).exists():
                inaccessible_count += 1

        if inaccessible_count > len(sample_paths) * 0.5:
            issues.append(
                f"{inaccessible_count}/{len(sample_paths)} sampled paths are inaccessible"
            )

        conn.close()

    except Exception as e:
        issues.append(f"Database error: {str(e)}")

    return {"valid": len(issues) == 0, "issues": issues, "stats": stats}


@contextmanager
def timeout(seconds):
    """Context manager for timeout (Unix only; no-op on Windows)."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def timeout_handler(signum, frame):
        raise TimeoutError(f"Operation timed out after {seconds} seconds")

    original_handler = signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, original_handler)
