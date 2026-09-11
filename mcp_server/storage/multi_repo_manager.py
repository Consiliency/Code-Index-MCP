"""
Multi-Repository Search Support System

This module manages multiple repository indexes and enables efficient
cross-repository code search operations.
"""

import hashlib
import logging
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp_server.storage.repo_identity import compute_repo_id
from mcp_server.storage.repository_registry import RepositoryRegistry
from mcp_server.storage.sqlite_store import SQLiteStore
from mcp_server.storage.store_registry import StoreRegistry
from mcp_server.utils.index_discovery import IndexDiscovery

logger = logging.getLogger(__name__)


@dataclass
class RepositoryInfo:
    """Information about a registered repository."""

    repository_id: str
    name: str
    path: Path
    index_path: Path
    language_stats: Dict[str, int]
    total_files: int
    total_symbols: int
    indexed_at: datetime
    current_commit: Optional[str] = None
    last_indexed_commit: Optional[str] = None
    last_indexed_branch: Optional[str] = None
    last_indexed: Optional[datetime] = None
    current_branch: Optional[str] = None
    url: Optional[str] = None
    auto_sync: bool = True
    artifact_enabled: bool = False
    active: bool = True
    priority: int = 0  # Higher priority repos searched first
    index_location: Optional[str] = None
    last_published_commit: Optional[str] = None
    last_recovered_commit: Optional[str] = None
    available_semantic_profiles: Optional[List[str]] = None
    artifact_backend: Optional[str] = None
    artifact_health: Optional[str] = None
    tracked_branch: Optional[str] = None  # pinned default branch; never auto-mutates
    git_common_dir: Optional[str] = None  # str for JSON-safety; __post_init__ coerces Path→str
    staleness_reason: Optional[str] = None
    last_sync_error: Optional[str] = None
    registration_id: Optional[str] = None
    index_generation: Optional[str] = None
    index_profile: Optional[str] = None

    def __post_init__(self) -> None:
        """Normalize paths and derived fields."""
        if isinstance(self.path, str):
            self.path = Path(self.path)

        if isinstance(self.index_path, str):
            self.index_path = Path(self.index_path)

        if isinstance(self.git_common_dir, Path):
            self.git_common_dir = str(self.git_common_dir)

        if self.index_location is None:
            # If index_path points to a file, use its parent directory as index location.
            index_base = self.index_path.parent if self.index_path.suffix else self.index_path
            self.index_location = str(index_base)

    @property
    def repo_id(self) -> str:
        """Alias for repository_id to match legacy callers."""
        return self.repository_id

    def needs_update(self) -> bool:
        """Return True if the current commit differs from the last indexed commit."""
        return bool(self.current_commit and self.current_commit != self.last_indexed_commit)


@dataclass
class CrossRepoSearchResult:
    """Result from cross-repository search."""

    repository_id: str
    repository_name: str
    results: List[Dict[str, Any]]
    search_time: float
    error: Optional[str] = None
    code: Optional[str] = None
    safe_fallback: Optional[str] = None


class MultiRepositoryManager:
    """
    Manages multiple repository indexes for cross-repository search.

    Features:
    - Repository registration and discovery
    - Priority-based search ordering
    - Parallel search execution
    - Result aggregation and ranking
    - Repository health monitoring
    """

    def __init__(self, central_index_path: Optional[Path] = None, max_workers: int = 4):
        """
        Initialize the multi-repository manager.

        Args:
            central_index_path: Path to central repository registry
            max_workers: Maximum parallel search workers
        """
        self.central_index_path = (
            Path(central_index_path) if central_index_path else self._get_default_registry_path()
        )
        self.max_workers = max_workers

        # Repository registry
        self.registry = RepositoryRegistry(self.central_index_path)

        # Cached repository connections (delegated to StoreRegistry)
        self._store_registry = StoreRegistry.for_registry(self.registry)

        # Search statistics
        self._search_stats = {
            "total_searches": 0,
            "total_repositories_searched": 0,
            "average_search_time": 0.0,
            "cache_hits": 0,
        }

        logger.info(
            f"Multi-repository manager initialized with " f"registry at {self.central_index_path}"
        )

    def _get_default_registry_path(self) -> Path:
        """Get default path for repository registry."""
        # Check environment variable
        env_path = os.environ.get("MCP_REPO_REGISTRY")
        if env_path:
            return Path(env_path)

        # Default to ~/.mcp/repository_registry.json
        home = Path.home()
        mcp_dir = home / ".mcp"
        mcp_dir.mkdir(exist_ok=True)

        return mcp_dir / "repository_registry.json"

    def register_repository(
        self, repository_path: Path, name: Optional[str] = None, priority: int = 0
    ) -> RepositoryInfo:
        """
        Register a repository for multi-repository search.

        Args:
            repository_path: Path to repository
            name: Optional display name
            priority: Search priority (higher = searched first)

        Returns:
            RepositoryInfo for the registered repository
        """
        # Discover index
        discovery = IndexDiscovery(repository_path)
        index_path = discovery.get_local_index_path()

        if not index_path:
            raise ValueError(f"No index found for repository at {repository_path}")

        # Generate repository ID
        repo_id = self._generate_repository_id(repository_path)

        # Get repository statistics
        stats = self._analyze_repository(index_path)

        # Create repository info
        repo_info = RepositoryInfo(
            repository_id=repo_id,
            name=name or repository_path.name,
            path=repository_path,
            index_path=index_path,
            language_stats=stats["languages"],
            total_files=stats["total_files"],
            total_symbols=stats["total_symbols"],
            indexed_at=datetime.now(),
            priority=priority,
        )

        # Register with registry
        self.registry.register(repo_info)

        logger.info(
            f"Registered repository '{repo_info.name}' "
            f"({repo_info.total_files} files, {repo_info.total_symbols} symbols)"
        )

        return repo_info

    def _generate_repository_id(self, repository_path: Path) -> str:
        """Generate unique ID for repository."""
        return compute_repo_id(repository_path).repo_id

    def _analyze_repository(self, index_path: Path) -> Dict[str, Any]:
        """Analyze repository index for statistics."""
        stats = {"languages": {}, "total_files": 0, "total_symbols": 0}

        try:
            conn = sqlite3.connect(str(index_path))
            cursor = conn.cursor()

            # Count files by language
            cursor.execute("""
                SELECT language, COUNT(*) 
                FROM files 
                WHERE is_deleted = 0 OR is_deleted IS NULL
                GROUP BY language
            """)

            for language, count in cursor:
                if language:
                    stats["languages"][language] = count
                    stats["total_files"] += count

            # Count symbols
            cursor.execute("""
                SELECT COUNT(*) FROM symbols
                WHERE is_deleted = 0 OR is_deleted IS NULL
            """)
            stats["total_symbols"] = cursor.fetchone()[0]

            conn.close()

        except Exception as e:
            logger.error(f"Error analyzing repository: {type(e).__name__}")

        return stats

    @classmethod
    def resolve_repo_id(cls, repository: str) -> str:
        """
        Canonicalize a repository identifier (id / URL / filesystem path) to a repo_id.

        Accepts any of:
          - a 16-char lowercase hex repo_id (returned unchanged)
          - a git remote URL (hashed via compute_repo_id abs-path fallback)
          - a filesystem path (delegated to compute_repo_id)
        """
        if not repository:
            raise ValueError("repository must be a non-empty string")

        candidate = repository.strip()

        # Already a repo_id?
        if len(candidate) == 16 and all(c in "0123456789abcdef" for c in candidate.lower()):
            return candidate.lower()

        # Git URL fast-path (no local path to resolve)
        url_prefixes = ("git@", "http://", "https://", "ssh://", "git://")
        if candidate.startswith(url_prefixes):
            return hashlib.sha256(candidate.encode()).hexdigest()[:16]

        # Filesystem path: delegate to compute_repo_id
        path = Path(candidate).expanduser()
        return compute_repo_id(path).repo_id

    def is_repo_authorized(self, repository_id: str) -> bool:
        """
        Check whether a repository may be queried.

        Policy:
          - Repo must be present in the registry (and active).
          - If MCP_REFERENCE_REPOS is set, the repo_id must also appear in that
            comma-separated allowlist (entries may be repo_ids, paths, or URLs;
            each is normalized via resolve_repo_id).
          - If MCP_REFERENCE_REPOS is unset or empty, any registered repo is allowed.
        """
        repo = self.registry.get(repository_id)
        if not repo or not repo.active:
            return False

        allowlist_raw = os.environ.get("MCP_REFERENCE_REPOS", "").strip()
        if not allowlist_raw:
            return True

        allowed_ids = set()
        for entry in allowlist_raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                allowed_ids.add(self.resolve_repo_id(entry))
            except Exception:
                continue

        return repository_id in allowed_ids

    def unregister_repository(self, repository_id: str):
        """
        Unregister a repository from multi-repository search.

        Args:
            repository_id: ID of repository to unregister
        """
        # Remove from registry
        self.registry.unregister(repository_id)

        # Close any cached connection
        self._store_registry.close(repository_id)

        logger.info(f"Unregistered repository {repository_id}")

    def list_repositories(self, active_only: bool = True) -> List[RepositoryInfo]:
        """
        List all registered repositories.

        Args:
            active_only: Only return active repositories

        Returns:
            List of repository information
        """
        repos = self.registry.list_all()

        if active_only:
            repos = [r for r in repos if r.active]

        # Sort by priority descending, then by name
        repos.sort(key=lambda r: (-r.priority, r.name))

        return repos

    def get_repository_info(self, repository_id: str) -> Optional[RepositoryInfo]:
        """Get information about a specific repository."""
        return self.registry.get(repository_id)

    def has_local_runtime_state(self, repository_id: str) -> bool:
        """Return True when the repository has a local MCP runtime baseline."""
        repo = self.registry.get(repository_id)
        if not repo:
            return False
        base = Path(repo.index_location) if repo.index_location else repo.path
        return (repo.path / "code_index.db").exists() or (base / "current.db").exists()

    def get_ready_repositories(self) -> List[RepositoryInfo]:
        """Return repositories ready for local search/reconcile use."""
        return [
            repo
            for repo in self.list_repositories(active_only=True)
            if self._query_ready(repo.repository_id)
        ]

    def get_stale_repositories(self) -> List[RepositoryInfo]:
        """Return repositories whose local/runtime state appears stale or missing."""
        stale = []
        for repo in self.list_repositories(active_only=True):
            if repo.current_commit and repo.last_recovered_commit:
                if repo.current_commit != repo.last_recovered_commit:
                    stale.append(repo)
                    continue
            if repo.current_commit and repo.last_published_commit:
                if repo.current_commit != repo.last_published_commit:
                    stale.append(repo)
                    continue
            if not self.has_local_runtime_state(repo.repository_id):
                stale.append(repo)
        return stale

    def _get_connection(self, repository_id: str) -> Optional[SQLiteStore]:
        """Get cached connection to repository index."""
        repo_info = self.registry.get(repository_id)
        if not repo_info or not repo_info.active:
            return None
        try:
            return self._store_registry.get(repository_id)
        except (KeyError, Exception) as e:
            logger.error(f"Failed to connect to repository {repository_id}: {type(e).__name__}")
            return None

    def _normalize_symbol_result(self, result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize symbol search result fields for aggregation."""
        symbol_name = result.get("symbol") or result.get("name")
        if not symbol_name:
            return None

        file_path = result.get("file_path") or result.get("file") or result.get("path")
        if not file_path and result.get("relative_path"):
            file_path = result["relative_path"]

        line_number = result.get("line") or result.get("line_start") or result.get("line_number")

        return {
            "symbol": symbol_name,
            "type": result.get("type") or result.get("kind"),
            "language": result.get("language"),
            "file": file_path,
            "line": line_number,
        }

    def _query_ready(self, repository_id: str, binding: Optional[tuple] = None) -> bool:
        from mcp_server.health.repository_readiness import ReadinessClassifier

        repo = self.registry.get(repository_id)
        return bool(
            repo
            and self.is_repo_authorized(repository_id)
            and (binding is None or StoreRegistry.binding(repo) == binding)
            and ReadinessClassifier.classify_registered(repo).ready
        )

    @staticmethod
    def _query_unavailable(
        repository_id: str, repo: Optional[RepositoryInfo]
    ) -> CrossRepoSearchResult:
        return CrossRepoSearchResult(
            repository_id=repository_id,
            repository_name=repo.name if repo else repository_id,
            results=[],
            search_time=0.0,
            error="index_unavailable",
            code="index_unavailable",
            safe_fallback="native_search",
        )

    async def search_symbol(
        self,
        query: str,
        repository_ids: Optional[List[str]] = None,
        language: Optional[str] = None,
        limit: int = 50,
    ) -> List[CrossRepoSearchResult]:
        """
        Search for symbols across multiple repositories.

        Args:
            query: Symbol name or pattern to search
            repository_ids: Specific repositories to search (None = all)
            language: Filter by programming language
            limit: Maximum results per repository

        Returns:
            List of cross-repository search results
        """
        start_time = datetime.now()

        # Get repositories to search
        if repository_ids:
            repos = [self.registry.get(rid) for rid in repository_ids]
            repos = [r for r in repos if r and r.active]
        else:
            repos = self.list_repositories(active_only=True)

        if not repos:
            logger.warning("No repositories to search")
            return []

        # Update statistics
        self._search_stats["total_searches"] += 1
        self._search_stats["total_repositories_searched"] += len(repos)

        # Execute searches in parallel
        results = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit search tasks
            future_to_repo = {
                executor.submit(
                    self._search_repository, repo.repository_id, query, language, limit
                ): repo
                for repo in repos
            }

            # Collect results
            for future in as_completed(future_to_repo):
                repo = future_to_repo[future]
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                except Exception as e:
                    logger.error(f"Search failed for {repo.name}: {type(e).__name__}")
                    results.append(
                        CrossRepoSearchResult(
                            repository_id=repo.repository_id,
                            repository_name=repo.name,
                            results=[],
                            search_time=0.0,
                            error=type(e).__name__,
                        )
                    )

        # Update average search time
        total_time = (datetime.now() - start_time).total_seconds()
        self._update_search_stats(total_time)

        # Sort by repository priority
        repo_priority = {r.repository_id: r.priority for r in repos}
        results.sort(key=lambda r: -repo_priority.get(r.repository_id, 0))

        logger.info(
            f"Searched {len(repos)} repositories (query_chars={len(query)}) "
            f"in {total_time:.2f}s, found {sum(len(r.results) for r in results)} results"
        )

        return results

    def _search_repository(
        self, repository_id: str, query: str, language: Optional[str], limit: int
    ) -> Optional[CrossRepoSearchResult]:
        """Search a single repository."""
        start_time = datetime.now()
        repo_info = self.registry.get(repository_id)
        if not self._query_ready(repository_id):
            return self._query_unavailable(repository_id, repo_info)
        binding = StoreRegistry.binding(repo_info)

        # Get connection
        try:
            store = self._get_connection(repository_id)
        except Exception as e:
            logger.error(f"Failed to get connection for {repository_id}: {type(e).__name__}")
            return None
        if not store:
            return None

        try:
            # Build search query
            conditions = ["s.name LIKE ?"]
            params = [f"%{query}%"]

            if language:
                conditions.append("f.language = ?")
                params.append(language)

            where_clause = " AND ".join(conditions)

            # Execute search
            results = store.search_symbols(
                query=query, where_clause=where_clause, params=params, limit=limit
            )

            # Convert and normalize results
            formatted_results = []
            for result in results:
                normalized = self._normalize_symbol_result(result)
                if not normalized:
                    continue
                normalized["repository"] = repo_info.name
                normalized["repository_id"] = repository_id
                formatted_results.append(normalized)

            search_time = (datetime.now() - start_time).total_seconds()

            if not self._query_ready(repository_id, binding):
                return self._query_unavailable(repository_id, repo_info)
            return CrossRepoSearchResult(
                repository_id=repository_id,
                repository_name=repo_info.name,
                results=formatted_results,
                search_time=search_time,
            )

        except Exception as e:
            logger.error(f"Error searching repository {repository_id}: {type(e).__name__}")
            return CrossRepoSearchResult(
                repository_id=repository_id,
                repository_name=repo_info.name,
                results=[],
                search_time=0.0,
                error=type(e).__name__,
            )

    async def search_code(
        self,
        query: str,
        repository_ids: Optional[List[str]] = None,
        file_pattern: Optional[str] = None,
        limit: int = 50,
    ) -> List[CrossRepoSearchResult]:
        """
        Search for code content across repositories using BM25.

        Args:
            query: Code pattern to search
            repository_ids: Specific repositories to search
            file_pattern: File pattern filter (e.g., "*.py")
            limit: Maximum results per repository

        Returns:
            List of cross-repository search results
        """
        start_time = datetime.now()

        # Get repositories to search
        if repository_ids:
            repos = [self.registry.get(rid) for rid in repository_ids]
            repos = [r for r in repos if r and r.active]
        else:
            repos = self.list_repositories(active_only=True)

        if not repos:
            logger.warning("No repositories to search")
            return []

        logger.info("Code search across %d repositories (query_chars=%d)", len(repos), len(query))

        # Update statistics
        self._search_stats["total_searches"] += 1
        self._search_stats["total_repositories_searched"] += len(repos)

        # Search in parallel using thread pool
        results = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_repo = {
                executor.submit(
                    self._search_code_in_repository,
                    repo.repository_id,
                    query,
                    file_pattern,
                    limit,
                ): repo
                for repo in repos
            }

            # Collect results
            for future in as_completed(future_to_repo):
                repo = future_to_repo[future]
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                except Exception as e:
                    logger.error(f"Search failed in {repo.name}: {type(e).__name__}")
                    results.append(
                        CrossRepoSearchResult(
                            repository_id=repo.repository_id,
                            repository_name=repo.name,
                            results=[],
                            search_time=0.0,
                            error=type(e).__name__,
                        )
                    )

        # Sort by priority
        repo_priority = {r.repository_id: r.priority for r in repos}
        results.sort(key=lambda r: -repo_priority.get(r.repository_id, 0))

        total_time = (datetime.now() - start_time).total_seconds()
        self._update_search_stats(total_time)

        logger.info(
            f"Code search completed in {total_time:.2f}s, "
            f"found {sum(len(r.results) for r in results)} results"
        )

        return results

    def _search_code_in_repository(
        self, repository_id: str, query: str, file_pattern: Optional[str], limit: int
    ) -> Optional[CrossRepoSearchResult]:
        """Search code content in a single repository using BM25."""
        start_time = datetime.now()
        repo_info = self.registry.get(repository_id)
        if not self._query_ready(repository_id):
            return self._query_unavailable(repository_id, repo_info)
        binding = StoreRegistry.binding(repo_info)

        store = self._get_connection(repository_id)
        if not store:
            return None

        try:
            # Use BM25 search on the appropriate table
            # Try both bm25_content and fts_code tables
            bm25_results = []

            # First try bm25_content table
            try:
                bm25_results = store.search_bm25(query, table="bm25_content", limit=limit)
            except Exception as e:
                logger.debug(f"bm25_content search failed, trying fts_code: {type(e).__name__}")
                # Fall back to fts_code table
                try:
                    bm25_results = store.search_bm25(query, table="fts_code", limit=limit)
                except Exception as e2:
                    logger.warning(
                        f"Both BM25 tables failed for {repository_id}: {type(e2).__name__}"
                    )

            # Format results
            formatted_results = []
            for result in bm25_results:
                formatted_results.append(
                    {
                        "file": result.get("filepath", result.get("file_path", "")),
                        "line": result.get("line", 0),
                        "snippet": result.get("snippet", ""),
                        "score": result.get("score", 0.0),
                        "repository": repo_info.name,
                        "repository_id": repository_id,
                    }
                )

            search_time = (datetime.now() - start_time).total_seconds()

            if not self._query_ready(repository_id, binding):
                return self._query_unavailable(repository_id, repo_info)
            return CrossRepoSearchResult(
                repository_id=repository_id,
                repository_name=repo_info.name,
                results=formatted_results,
                search_time=search_time,
            )

        except Exception as e:
            logger.error(f"BM25 search failed in {repository_id}: {type(e).__name__}")
            return CrossRepoSearchResult(
                repository_id=repository_id,
                repository_name=repo_info.name,
                results=[],
                search_time=(datetime.now() - start_time).total_seconds(),
                error=type(e).__name__,
            )

    def _update_search_stats(self, search_time: float):
        """Update search statistics."""
        stats = self._search_stats

        # Update average search time
        total_searches = stats["total_searches"]
        current_avg = stats["average_search_time"]

        # Incremental average calculation
        new_avg = ((current_avg * (total_searches - 1)) + search_time) / total_searches
        stats["average_search_time"] = new_avg

    def get_statistics(self) -> Dict[str, Any]:
        """Get multi-repository search statistics."""
        repos = self.list_repositories()

        # Aggregate repository statistics
        total_files = sum(r.total_files for r in repos)
        total_symbols = sum(r.total_symbols for r in repos)

        # Language distribution
        all_languages = {}
        for repo in repos:
            for lang, count in repo.language_stats.items():
                all_languages[lang] = all_languages.get(lang, 0) + count

        return {
            "repositories": {
                "total": len(repos),
                "active": len([r for r in repos if r.active]),
                "total_files": total_files,
                "total_symbols": total_symbols,
            },
            "languages": all_languages,
            "search_stats": self._search_stats.copy(),
            "cache": {"connections": len(self._store_registry._cache)},
        }

    def health_check(self) -> Dict[str, Any]:
        """Check health of all registered repositories."""
        health_status = {"healthy": 0, "unhealthy": 0, "repositories": []}

        for repo in self.list_repositories():
            repo_health = {
                "repository_id": repo.repository_id,
                "name": repo.name,
                "status": "healthy",
                "issues": [],
            }

            # Check index file exists
            if not repo.index_path.exists():
                repo_health["status"] = "unhealthy"
                repo_health["issues"].append("Index file not found")

            # Check repository path exists
            if not repo.path.exists():
                repo_health["status"] = "unhealthy"
                repo_health["issues"].append("Repository path not found")

            # Try to connect
            try:
                conn = self._get_connection(repo.repository_id)
                if not conn:
                    repo_health["status"] = "unhealthy"
                    repo_health["issues"].append("Cannot connect to index")
            except Exception as e:
                repo_health["status"] = "unhealthy"
                repo_health["issues"].append(f"Connection error: {str(e)}")

            # Update counters
            if repo_health["status"] == "healthy":
                health_status["healthy"] += 1
            else:
                health_status["unhealthy"] += 1

            health_status["repositories"].append(repo_health)

        return health_status

    def optimize_indexes(self):
        """Optimize all repository indexes for better performance."""
        optimized = 0

        for repo in self.list_repositories():
            try:
                conn = sqlite3.connect(str(repo.index_path))
                cursor = conn.cursor()

                # Run VACUUM to optimize
                cursor.execute("VACUUM")

                # Analyze for query optimization
                cursor.execute("ANALYZE")

                conn.close()
                optimized += 1

                logger.info(f"Optimized index for {repo.name}")

            except Exception as e:
                logger.error(f"Failed to optimize {repo.name}: {type(e).__name__}")

        logger.info(f"Optimized {optimized} repository indexes")

    def close(self):
        """Close all connections and save registry."""
        self._store_registry.shutdown()

        # Save registry
        self.registry.save()

        logger.info("Multi-repository manager closed")


# Singleton instance
_manager_instance: Optional[MultiRepositoryManager] = None


def get_multi_repo_manager(
    central_index_path: Optional[Path] = None,
) -> MultiRepositoryManager:
    """
    Get the singleton multi-repository manager.

    Args:
        central_index_path: Optional path to registry

    Returns:
        MultiRepositoryManager instance
    """
    global _manager_instance

    if _manager_instance is None:
        _manager_instance = MultiRepositoryManager(central_index_path)

    return _manager_instance
