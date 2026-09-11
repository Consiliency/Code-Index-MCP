"""
Cross-Repository Search Coordinator

This module coordinates search operations across multiple repositories,
providing intelligent result ranking, deduplication, and aggregation.
"""

import hashlib
import logging
import re
import time
from collections import defaultdict
from concurrent.futures import (  # noqa: F401  (patched by tests; looked up via globals())
    ThreadPoolExecutor,
    as_completed,
)
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from mcp_server.core.errors import MCPError, record_handled_error
from mcp_server.dependency_graph.aggregator import DependencyGraphAnalyzer
from mcp_server.health.repository_readiness import ReadinessClassifier
from mcp_server.indexer.reranker import IReranker as Reranker
from mcp_server.indexer.reranker import RerankerFactory
from mcp_server.plugins.repository_plugin_loader import get_repository_plugin_loader
from mcp_server.storage.multi_repo_manager import (
    CrossRepoSearchResult,
    MultiRepositoryManager,
    get_multi_repo_manager,
)
from mcp_server.storage.sqlite_store import (  # noqa: F401  (patched by tests; looked up via globals())
    SQLiteStore,
)
from mcp_server.storage.store_registry import StoreRegistry

logger = logging.getLogger(__name__)


# Redact bearer/api-key/token-like substrings from exception text before it is
# stored in rerank diagnostics or written to logs. Kept small and local to avoid
# a circular import back into the dispatcher module.
_SECRET_REDACTION_PATTERN = re.compile(
    r"(?i)"
    r"(?:bearer\s+[A-Za-z0-9._\-]+)"
    r"|(?:(?:api[_-]?key|apikey|token|secret|password|authorization)\s*[=:]\s*[^\s,;'\"]+)"
    r"|(?:sk-[A-Za-z0-9]{6,})"
)


def _redact_secrets(text: Optional[str]) -> Optional[str]:
    """Strip credential-like substrings from *text* before diagnostics/logging."""
    if not text:
        return text
    return _SECRET_REDACTION_PATTERN.sub("[REDACTED]", str(text))


@dataclass
class SearchContext:
    """Context for cross-repository search operations."""

    query: str
    search_type: str  # 'symbol', 'code', 'semantic'
    repositories: Optional[List[str]] = None
    languages: Optional[List[str]] = None
    file_patterns: Optional[List[str]] = None
    max_results: int = 100
    deduplicate: bool = True
    rerank: bool = True
    include_dependencies: bool = False
    search_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AggregatedResult:
    """Aggregated search result across repositories."""

    content: Dict[str, Any]
    score: float
    repositories: List[str]  # Repos where this result appears
    primary_repository: str  # Best match repository
    occurrences: int
    metadata: Dict[str, Any] = field(default_factory=dict)


_RankedRepositoryResult = AggregatedResult


@dataclass
class SearchStrategy:
    """Strategy for cross-repository search."""

    name: str
    scorer: Callable[[Dict[str, Any], SearchContext], float]
    filter: Optional[Callable[[Dict[str, Any], SearchContext], bool]] = None
    post_processor: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None


class CrossRepositoryCoordinator:
    """
    Coordinates cross-repository search with intelligent result processing.

    Features:
    - Multiple search strategies (symbol, code, semantic)
    - Result deduplication across repositories
    - Intelligent ranking based on relevance and repository priority
    - Dependency-aware search
    - Result aggregation and grouping
    """

    def __init__(
        self,
        multi_repo_manager: Optional[MultiRepositoryManager] = None,
        enable_semantic: bool = True,
        enable_reranking: bool = True,
        reranker: Optional[Reranker] = None,
    ):
        """
        Initialize the cross-repository coordinator.

        Args:
            multi_repo_manager: Multi-repository manager instance
            enable_semantic: Enable semantic search features
            enable_reranking: Enable result reranking
            reranker: Optional IReranker to inject; if None and enable_reranking is True,
                      RerankerFactory.create_default() is attempted.
        """
        self.multi_repo_manager = multi_repo_manager or get_multi_repo_manager()
        self.plugin_loader = get_repository_plugin_loader()
        self.enable_semantic = enable_semantic
        self.enable_reranking = enable_reranking

        # Structured outcome of the most recent rerank attempt (RERANKEND).
        self.last_rerank_diagnostics = None

        # Initialize reranker: injected > factory > None
        if reranker is not None:
            self.reranker: Optional[Reranker] = reranker
        elif enable_reranking:
            try:
                self.reranker = RerankerFactory.create_default()
            except Exception:
                self.reranker = None
        else:
            self.reranker = None

        # Search strategies
        self._strategies: Dict[str, SearchStrategy] = self._init_strategies()

        # Cache for repository relationships
        self._repo_dependencies: Dict[str, Set[str]] = {}

        logger.info(
            f"Cross-repository coordinator initialized "
            f"(semantic={enable_semantic}, reranking={enable_reranking})"
        )

    def _init_strategies(self) -> Dict[str, SearchStrategy]:
        """Initialize search strategies."""
        return {
            "symbol": SearchStrategy(
                name="symbol", scorer=self._score_symbol_result, filter=self._filter_symbol_result
            ),
            "code": SearchStrategy(
                name="code", scorer=self._score_code_result, filter=self._filter_code_result
            ),
            "semantic": SearchStrategy(
                name="semantic",
                scorer=self._score_semantic_result,
                post_processor=self._post_process_semantic,
            ),
        }

    async def search(self, context: SearchContext) -> List[AggregatedResult]:
        """
        Perform coordinated search across repositories.

        Args:
            context: Search context with query and parameters

        Returns:
            List of aggregated results
        """
        start_time = datetime.now()
        bindings = {
            repo.repository_id: StoreRegistry.binding(repo)
            for repo in self.multi_repo_manager.list_repositories(active_only=True)
            if context.repositories is None or repo.repository_id in context.repositories
        }
        logger.info(
            "Starting cross-repository search (query_chars=%d, type=%s)",
            len(context.query or ""),
            context.search_type,
        )

        # Get search strategy
        strategy = self._strategies.get(context.search_type, self._strategies["symbol"])

        # Perform base search
        raw_results = await self._execute_search(context)
        if any(result.error for result in raw_results) or (
            context.repositories and set(context.repositories) - bindings.keys()
        ):
            raise MCPError(
                "Index unavailable", {"code": "index_unavailable", "safe_fallback": "native_search"}
            )

        # Apply strategy filter
        if strategy.filter:
            filtered_results = self._apply_filter(raw_results, strategy.filter, context)
        else:
            filtered_results = raw_results

        # Aggregate and deduplicate
        if context.deduplicate:
            aggregated = self._aggregate_results(filtered_results, context)
        else:
            aggregated = self._convert_to_aggregated(filtered_results)

        # Score results
        scored = self._score_results(aggregated, strategy.scorer, context)

        # Rerank if enabled
        if context.rerank and self.reranker:
            reranked = await self._rerank_results(scored, context)
        else:
            reranked = scored

        # Apply post-processing
        if strategy.post_processor:
            final_results = strategy.post_processor(reranked)
        else:
            final_results = reranked

        # Limit results
        limited = final_results[: context.max_results]

        # Log statistics
        search_time = (datetime.now() - start_time).total_seconds()
        logger.info(
            f"Search completed in {search_time:.2f}s: "
            f"{len(raw_results)} raw -> {len(limited)} final results"
        )

        if any(
            not self.multi_repo_manager._query_ready(repo_id, binding)
            for repo_id, binding in bindings.items()
        ):
            raise MCPError(
                "Index unavailable", {"code": "index_unavailable", "safe_fallback": "native_search"}
            )
        return limited

    async def _execute_search(self, context: SearchContext) -> List[CrossRepoSearchResult]:
        """Execute search across repositories."""
        if context.search_type == "symbol":
            return await self.multi_repo_manager.search_symbol(
                query=context.query,
                repository_ids=context.repositories,
                language=context.languages[0] if context.languages else None,
                limit=context.max_results * 2,  # Get extra for filtering
            )
        elif context.search_type == "code":
            return await self.multi_repo_manager.search_code(
                query=context.query,
                repository_ids=context.repositories,
                file_pattern=context.file_patterns[0] if context.file_patterns else None,
                limit=context.max_results * 2,
            )
        else:
            raise MCPError(
                "Cross-repository semantic backend is unavailable",
                {"code": "index_unavailable", "safe_fallback": "native_search"},
            )

    def _apply_filter(
        self, results: List[CrossRepoSearchResult], filter_func: Callable, context: SearchContext
    ) -> List[CrossRepoSearchResult]:
        """Apply filter to search results."""
        filtered_results = []

        for repo_result in results:
            if repo_result.error:
                continue

            filtered_items = []
            for item in repo_result.results:
                if filter_func(item, context):
                    filtered_items.append(item)

            if filtered_items:
                filtered_result = CrossRepoSearchResult(
                    repository_id=repo_result.repository_id,
                    repository_name=repo_result.repository_name,
                    results=filtered_items,
                    search_time=repo_result.search_time,
                )
                filtered_results.append(filtered_result)

        return filtered_results

    def _aggregate_results(
        self, results: List[CrossRepoSearchResult], context: SearchContext
    ) -> List[AggregatedResult]:
        """Aggregate and deduplicate results across repositories."""
        # Group by content hash
        grouped: Dict[str, List[Tuple[Dict[str, Any], str, str]]] = defaultdict(list)

        for repo_result in results:
            if repo_result.error:
                continue

            for item in repo_result.results:
                # Create content hash for deduplication
                content_hash = self._hash_result(item, context.search_type)
                grouped[content_hash].append(
                    (item, repo_result.repository_id, repo_result.repository_name)
                )

        # Create aggregated results
        aggregated = []
        for content_hash, occurrences in grouped.items():
            # Choose primary result (from highest priority repo)
            primary_item, primary_repo_id, primary_repo_name = occurrences[0]

            # Get all repository IDs
            repo_ids = [repo_id for _, repo_id, _ in occurrences]

            aggregated_result = _RankedRepositoryResult(
                content=primary_item,
                score=0.0,  # Will be set by scorer
                repositories=repo_ids,
                primary_repository=primary_repo_id,
                occurrences=len(occurrences),
                metadata={"content_hash": content_hash, "primary_repo_name": primary_repo_name},
            )
            aggregated.append(aggregated_result)

        return aggregated

    def _convert_to_aggregated(
        self, results: List[CrossRepoSearchResult]
    ) -> List[AggregatedResult]:
        """Convert raw results to aggregated format without deduplication."""
        aggregated = []

        for repo_result in results:
            if repo_result.error:
                continue

            for item in repo_result.results:
                aggregated_result = _RankedRepositoryResult(
                    content=item,
                    score=0.0,
                    repositories=[repo_result.repository_id],
                    primary_repository=repo_result.repository_id,
                    occurrences=1,
                    metadata={"repository_name": repo_result.repository_name},
                )
                aggregated.append(aggregated_result)

        return aggregated

    def _hash_result(self, result: Dict[str, Any], search_type: str) -> str:
        """Create hash for result deduplication."""
        if search_type == "symbol":
            # Hash based on symbol name and type
            key = f"{result.get('symbol', '')}:{result.get('type', '')}"
        elif search_type == "code":
            # Hash based on file and line range
            key = f"{result.get('file', '')}:{result.get('line', '')}"
        else:
            # Generic hash
            key = str(result)

        return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()

    def _score_results(
        self, results: List[AggregatedResult], scorer: Callable, context: SearchContext
    ) -> List[AggregatedResult]:
        """Score and sort results."""
        for result in results:
            result.score = scorer(result.content, context)

            # Boost score based on occurrences
            if result.occurrences > 1:
                result.score *= 1 + 0.1 * (result.occurrences - 1)

        # Sort by score descending
        results.sort(key=lambda r: r.score, reverse=True)

        return results

    async def _rerank_results(
        self, results: List[AggregatedResult], context: SearchContext
    ) -> List[AggregatedResult]:
        """Rerank results using the canonical :class:`IReranker` interface.

        Drives ``IReranker.rerank(query, results, top_k)`` (positional canonical
        signature) which returns a ``Result[RerankResult]``. Results are
        correlated back to their :class:`AggregatedResult` by each
        ``RerankItem.original_rank`` — NOT by a bare index list (the pre-migration
        code incorrectly passed ``documents=`` and expected an index list, which
        no concrete canonical reranker returns). A truthful structured outcome is
        recorded on ``last_rerank_diagnostics``; it carries ids/counts/providers
        only, never query or document text.
        """
        from mcp_server.indexer.reranker import (
            RerankDiagnostics,
            RerankOutcome,
        )
        from mcp_server.indexer.reranker import SearchResult as RerankSearchResult

        if self.reranker is None:
            # NOT_CONFIGURED is reserved for the absent-reranker case only.
            self.last_rerank_diagnostics = RerankDiagnostics(
                outcome=RerankOutcome.NOT_CONFIGURED,
                candidate_count=len(results),
                returned_count=len(results),
            )
            return results

        if not results:
            # A reranker IS configured; there is nothing to rerank. That is an
            # ATTEMPTED (empty) outcome, never NOT_CONFIGURED.
            self.last_rerank_diagnostics = RerankDiagnostics(
                outcome=RerankOutcome.ATTEMPTED,
                candidate_count=0,
                returned_count=0,
            )
            return results

        # Build canonical SearchResult candidates; the document representation is
        # carried as the reranker snippet text.
        candidates = [
            RerankSearchResult(
                file_path=str(
                    result.content.get("file", "") if isinstance(result.content, dict) else ""
                ),
                start_line=0,
                end_line=0,
                column=0,
                snippet=self._create_document_repr(result.content, context.search_type),
                match_type=context.search_type,
                score=float(getattr(result, "score", 0.0) or 0.0),
            )
            for result in results
        ]
        top_k = min(len(results), context.max_results)

        try:
            # `initialize` is an optional BaseReranker detail, not part of the
            # canonical IReranker contract; call it once if present.
            if not getattr(self, "_reranker_initialized", False) and hasattr(
                self.reranker, "initialize"
            ):
                init_result = await self.reranker.initialize({})
                if not init_result.is_success:
                    _err = _redact_secrets(str(init_result.error))
                    logger.warning("Reranker initialization failed: %s", _err)
                    self.last_rerank_diagnostics = RerankDiagnostics(
                        outcome=RerankOutcome.FAILED,
                        error=_err,
                        candidate_count=len(results),
                        returned_count=len(results),
                    )
                    return results
            self._reranker_initialized = True

            rerank_result = await self.reranker.rerank(context.query, candidates, top_k)
        except Exception as exc:
            record_handled_error(__name__, exc)
            _err = _redact_secrets(str(exc))
            logger.warning("Reranking failed, returning scored order: %s", _err)
            self.last_rerank_diagnostics = RerankDiagnostics(
                outcome=RerankOutcome.FAILED,
                error=_err,
                candidate_count=len(results),
                returned_count=len(results),
            )
            return results

        if not getattr(rerank_result, "is_success", False) or not getattr(
            rerank_result, "data", None
        ):
            _err = _redact_secrets(str(getattr(rerank_result, "error", "no data")))
            logger.warning("Reranking returned no data: %s", _err)
            self.last_rerank_diagnostics = RerankDiagnostics(
                outcome=RerankOutcome.FAILED,
                error=_err,
                candidate_count=len(results),
                returned_count=len(results),
            )
            return results

        # Reorder original AggregatedResults by each item's original_rank.
        reranked_results: List[AggregatedResult] = []
        for item in rerank_result.data.results:
            idx = item.original_rank
            if 0 <= idx < len(results):
                reranked_results.append(results[idx])

        reordered = [id(r) for r in reranked_results] != [
            id(r) for r in results[: len(reranked_results)]
        ]
        provider = None
        metadata = getattr(rerank_result.data, "metadata", None) or {}
        provider = metadata.get("provider") or metadata.get("reranker")
        self.last_rerank_diagnostics = RerankDiagnostics(
            outcome=RerankOutcome.SUCCEEDED if reordered else RerankOutcome.ATTEMPTED,
            provider=provider,
            candidate_count=len(results),
            returned_count=len(reranked_results),
            reordered=reordered,
        )

        return reranked_results

    def _create_document_repr(self, result: Dict[str, Any], search_type: str) -> str:
        """Create document representation for reranking."""
        if search_type == "symbol":
            return (
                f"{result.get('symbol', '')} "
                f"{result.get('type', '')} "
                f"in {result.get('file', '')}"
            )
        elif search_type == "code":
            return (
                f"{result.get('code', '')} "
                f"in {result.get('file', '')} "
                f"line {result.get('line', '')}"
            )
        else:
            return str(result)

    # Scoring functions
    def _score_symbol_result(self, result: Dict[str, Any], context: SearchContext) -> float:
        """Score symbol search result."""
        score = 0.0
        query_lower = context.query.lower()
        symbol_lower = result.get("symbol", "").lower()

        # Exact match
        if symbol_lower == query_lower:
            score += 10.0
        # Prefix match
        elif symbol_lower.startswith(query_lower):
            score += 7.0
        # Contains match
        elif query_lower in symbol_lower:
            score += 5.0

        # Type boost
        symbol_type = result.get("type", "").lower()
        if symbol_type in ["class", "interface"]:
            score += 2.0
        elif symbol_type in ["function", "method"]:
            score += 1.5

        # Language preference
        if context.languages and result.get("language") in context.languages:
            score += 1.0

        return score

    def _score_code_result(self, result: Dict[str, Any], context: SearchContext) -> float:
        """Score code search result."""
        score = 0.0
        query_lower = context.query.lower()

        # Code content matching
        code = result.get("code", "").lower()
        matches = code.count(query_lower)
        score += matches * 2.0

        # File pattern matching
        if context.file_patterns:
            file_path = result.get("file", "")
            for pattern in context.file_patterns:
                if self._match_pattern(file_path, pattern):
                    score += 3.0
                    break

        return score

    def _score_semantic_result(self, result: Dict[str, Any], context: SearchContext) -> float:
        """Score semantic search result."""
        # Semantic scoring would use embeddings
        # For now, fall back to symbol scoring
        return self._score_symbol_result(result, context)

    # Filter functions
    def _filter_symbol_result(self, result: Dict[str, Any], context: SearchContext) -> bool:
        """Filter symbol results."""
        # Language filter
        if context.languages and result.get("language") not in context.languages:
            return False

        # File pattern filter
        if context.file_patterns:
            file_path = result.get("file", "")
            if not any(self._match_pattern(file_path, p) for p in context.file_patterns):
                return False

        return True

    def _filter_code_result(self, result: Dict[str, Any], context: SearchContext) -> bool:
        """Filter code results."""
        return self._filter_symbol_result(result, context)

    def _match_pattern(self, file_path: str, pattern: str) -> bool:
        """Match file path against pattern."""
        # Convert glob to regex
        pattern = pattern.replace("*", ".*").replace("?", ".")
        return bool(re.match(pattern, file_path))

    def _post_process_semantic(self, results: List[AggregatedResult]) -> List[AggregatedResult]:
        """Post-process semantic search results."""
        # Group related results
        # This would use semantic similarity
        return results

    async def search_with_dependencies(self, context: SearchContext) -> List[AggregatedResult]:
        """
        Search including dependency repositories.

        Args:
            context: Search context

        Returns:
            Results including dependencies
        """
        if not context.include_dependencies:
            return await self.search(context)

        # Get dependency repositories
        all_repos = set(context.repositories or [])

        for repo_id in list(all_repos):
            deps = await self._get_repository_dependencies(repo_id)
            all_repos.update(deps)

        # Update context with all repositories
        context.repositories = list(all_repos)

        # Perform search
        results = await self.search(context)

        # Mark results from dependencies
        for result in results:
            if result.primary_repository not in (context.repositories or []):
                result.metadata["from_dependency"] = True

        return results

    async def _get_repository_dependencies(self, repository_id: str) -> Set[str]:
        """Get dependency repositories by analyzing manifests via DependencyGraphAnalyzer."""
        if repository_id in self._repo_dependencies:
            return self._repo_dependencies[repository_id]

        if self._multi_repo_manager is None:
            self._repo_dependencies[repository_id] = set()
            return set()

        if not hasattr(self, "_dep_analyzer"):
            self._dep_analyzer = DependencyGraphAnalyzer(self._multi_repo_manager)

        deps = await self._dep_analyzer.analyze(repository_id)
        self._repo_dependencies[repository_id] = deps
        return deps

    def get_search_strategies(self) -> List[str]:
        """Get available search strategies."""
        return list(self._strategies.keys())

    def register_strategy(self, name: str, strategy: SearchStrategy):
        """Register a custom search strategy."""
        self._strategies[name] = strategy
        logger.info(f"Registered search strategy: {name}")

    async def analyze_query(self, query: str) -> Dict[str, Any]:
        """
        Analyze query to determine best search strategy.

        Args:
            query: Search query

        Returns:
            Analysis results with recommended strategy
        """
        analysis = {
            "query": query,
            "recommended_strategy": "symbol",
            "detected_patterns": [],
            "suggested_filters": {},
        }

        # Check for code patterns
        if any(char in query for char in ["{", "}", "(", ")", ";"]):
            analysis["recommended_strategy"] = "code"
            analysis["detected_patterns"].append("code_syntax")

        # Check for semantic indicators
        semantic_keywords = ["how to", "example", "implement", "usage"]
        if any(keyword in query.lower() for keyword in semantic_keywords):
            analysis["recommended_strategy"] = "semantic"
            analysis["detected_patterns"].append("natural_language")

        # Check for language hints
        language_pattern = r"\b(python|java|javascript|go|rust)\b"
        lang_match = re.search(language_pattern, query.lower())
        if lang_match:
            analysis["suggested_filters"]["languages"] = [lang_match.group()]
            analysis["detected_patterns"].append("language_specific")

        # Check for file patterns
        file_pattern = r"\*\.\w+|\w+\.\w+"
        if re.search(file_pattern, query):
            analysis["detected_patterns"].append("file_pattern")

        return analysis


# Singleton instance (internal — CrossRepositoryCoordinator)
_coordinator_instance: Optional[CrossRepositoryCoordinator] = None

# Singleton for the public wrapper
_search_coordinator_instance: Optional["CrossRepositorySearchCoordinator"] = None


def get_cross_repo_coordinator(
    enable_semantic: bool = True, enable_reranking: bool = True
) -> "CrossRepositorySearchCoordinator":
    """Return the singleton CrossRepositorySearchCoordinator."""
    global _search_coordinator_instance

    if _search_coordinator_instance is None:
        _search_coordinator_instance = CrossRepositorySearchCoordinator()

    return _search_coordinator_instance


# ---------------------------------------------------------------------------
# Compatibility layer: absorb storage/cross_repo_coordinator shapes
# (storage variant deleted in SL-1; these names were previously exported there)
# ---------------------------------------------------------------------------


@dataclass
class SearchScope:
    """Defines the scope for cross-repository search (absorbed from storage variant)."""

    repositories: Optional[List[str]] = None
    languages: Optional[List[str]] = None
    file_types: Optional[List[str]] = None
    max_repositories: int = 10
    priority_order: bool = True


@dataclass
class _CrossRepoAggregatedResult:
    """Aggregated result shape used by CrossRepositorySearchCoordinator."""

    query: str
    total_results: int
    repositories_searched: int
    search_time: float
    results: List[Dict[str, Any]]
    repository_stats: Dict[str, int]
    deduplication_stats: Dict[str, int]
    code: Optional[str] = None
    safe_fallback: Optional[str] = None
    repository_errors: Dict[str, str] = field(default_factory=dict)


AggregatedResult = _CrossRepoAggregatedResult


class CrossRepositorySearchCoordinator:
    """Thin wrapper around CrossRepositoryCoordinator exposing the legacy storage-variant API.

    Callers using the old ``mcp_server.storage.cross_repo_coordinator`` import path
    should migrate to ``mcp_server.dispatcher.cross_repo_coordinator``.
    """

    def __init__(
        self,
        multi_repo_manager: Optional[MultiRepositoryManager] = None,
        max_workers: int = 4,
        default_result_limit: int = 100,
    ) -> None:
        self._inner = CrossRepositoryCoordinator(
            multi_repo_manager=multi_repo_manager,
            enable_semantic=True,
            enable_reranking=True,
        )
        self.max_workers = max_workers
        self.default_result_limit = default_result_limit
        self.multi_repo_manager = self._inner.multi_repo_manager
        self.plugin_loader = self._inner.plugin_loader
        self.memory_manager = self._inner.plugin_loader.memory_manager

    async def _get_target_repositories(self, scope: SearchScope) -> List:
        """Return RepositoryInfo list filtered and ordered per scope."""
        all_repos = list(self.multi_repo_manager.list_repositories())

        if scope.repositories is not None:
            id_set = set(scope.repositories)
            all_repos = [r for r in all_repos if r.repository_id in id_set]

        if scope.languages is not None:
            lang_set = set(scope.languages)
            all_repos = [r for r in all_repos if lang_set & set(r.language_stats.keys())]

        if scope.priority_order:
            all_repos.sort(key=lambda r: r.priority, reverse=True)

        return all_repos[: scope.max_repositories]

    @staticmethod
    def _relative_suffix(file_path: str) -> str:
        """Drop the first path component (repo-root directory) so results from
        different repos collide when the within-repo relative path matches.
        """
        parts = [p for p in file_path.replace("\\", "/").split("/") if p]
        return "/".join(parts[1:]) if len(parts) > 1 else "/".join(parts)

    def _create_symbol_signature(self, result: Dict[str, Any]) -> str:
        """Content-agnostic signature for symbol dedup across repos."""
        key = (
            f"{result.get('symbol', '')}:"
            f"{self._relative_suffix(result.get('file_path', ''))}:"
            f"{result.get('line_number', '')}"
        )
        return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()

    def _create_content_hash(self, result: Dict[str, Any]) -> str:
        """Content hash for code-result dedup across repos."""
        key = (
            f"{result.get('content', '')}:" f"{self._relative_suffix(result.get('file_path', ''))}"
        )
        return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()

    def _search_symbol_in_repository(
        self, query: str, repo: Any, scope: SearchScope
    ) -> CrossRepoSearchResult:
        return self.multi_repo_manager._search_repository(
            repo.repository_id,
            query,
            scope.languages[0] if scope.languages else None,
            self.default_result_limit,
        )

    def _search_code_in_repository(
        self,
        query: str,
        repo: Any,
        scope: SearchScope,
        semantic: bool = False,
        limit: Optional[int] = None,
    ) -> CrossRepoSearchResult:
        started = time.time()
        if semantic:
            return CrossRepoSearchResult(
                repo.repository_id,
                repo.name,
                [],
                0.0,
                "Cross-repository semantic backend is unavailable",
                "index_unavailable",
                "native_search",
            )
        results = []
        for extension in scope.file_types or [None]:
            result = self.multi_repo_manager._search_code_in_repository(
                repo.repository_id,
                query,
                f"*{extension}" if extension else None,
                limit or self.default_result_limit,
            )
            if result is None or result.error:
                return result or self.multi_repo_manager._query_unavailable(
                    repo.repository_id, repo
                )
            results.extend(result.results)
        if not self.multi_repo_manager._query_ready(
            repo.repository_id, StoreRegistry.binding(repo)
        ):
            return self.multi_repo_manager._query_unavailable(repo.repository_id, repo)
        unique = {(row.get("file_path"), row.get("snippet")): row for row in results}
        ordered = sorted(unique.values(), key=lambda row: row.get("score", 0), reverse=True)
        return CrossRepoSearchResult(
            repo.repository_id,
            repo.name,
            ordered[: limit or self.default_result_limit],
            time.time() - started,
        )

    async def _aggregate_symbol_results(
        self,
        query: str,
        search_results: List[CrossRepoSearchResult],
        start_time: float,
    ) -> "_CrossRepoAggregatedResult":
        """Dedup + aggregate per-repo symbol search results."""
        failures = {
            r.repository_id: r.code or "index_unavailable" for r in search_results if r.error
        }
        if failures:
            return _CrossRepoAggregatedResult(
                query,
                0,
                len(search_results),
                time.time() - start_time,
                [],
                {},
                {},
                "index_unavailable",
                "native_search",
                failures,
            )
        seen: Dict[str, Dict[str, Any]] = {}
        repo_stats: Dict[str, int] = {}
        original_count = 0
        for repo_result in search_results:
            if repo_result.error:
                continue
            repo_stats[repo_result.repository_id] = len(repo_result.results)
            for item in repo_result.results:
                original_count += 1
                enriched = dict(item)
                enriched.setdefault("repository_id", repo_result.repository_id)
                enriched.setdefault("repository_name", repo_result.repository_name)
                sig = self._create_symbol_signature(enriched)
                if sig not in seen:
                    seen[sig] = enriched
        merged = list(seen.values())
        duplicates_removed = original_count - len(merged)
        return _CrossRepoAggregatedResult(
            query=query,
            total_results=len(merged),
            repositories_searched=len(search_results),
            search_time=time.time() - start_time,
            results=merged,
            repository_stats=repo_stats,
            deduplication_stats={
                "original_count": original_count,
                "duplicates_removed": duplicates_removed,
            },
        )

    async def _aggregate_code_results(
        self,
        query: str,
        search_results: List[CrossRepoSearchResult],
        start_time: float,
        limit: Optional[int] = None,
    ) -> "_CrossRepoAggregatedResult":
        """Dedup + score-sort + limit per-repo code search results."""
        failures = {
            r.repository_id: r.code or "index_unavailable" for r in search_results if r.error
        }
        if failures:
            return _CrossRepoAggregatedResult(
                query,
                0,
                len(search_results),
                time.time() - start_time,
                [],
                {},
                {},
                "index_unavailable",
                "native_search",
                failures,
            )
        seen: Dict[str, Dict[str, Any]] = {}
        repo_stats: Dict[str, int] = {}
        original_count = 0
        for repo_result in search_results:
            if repo_result.error:
                continue
            repo_stats[repo_result.repository_id] = len(repo_result.results)
            for item in repo_result.results:
                original_count += 1
                enriched = dict(item)
                enriched.setdefault("repository_id", repo_result.repository_id)
                enriched.setdefault("repository_name", repo_result.repository_name)
                h = self._create_content_hash(enriched)
                if h not in seen:
                    seen[h] = enriched
        merged = list(seen.values())
        merged.sort(key=lambda r: r.get("score", 0.0), reverse=True)
        if limit is not None:
            merged = merged[:limit]
        duplicates_removed = original_count - len(merged)
        return _CrossRepoAggregatedResult(
            query=query,
            total_results=len(merged),
            repositories_searched=len(search_results),
            search_time=time.time() - start_time,
            results=merged,
            repository_stats=repo_stats,
            deduplication_stats={
                "original_count": original_count,
                "duplicates_removed": duplicates_removed,
            },
        )

    def _run_parallel(
        self,
        repos: List[Any],
        worker: Callable[[Any], CrossRepoSearchResult],
    ) -> List[CrossRepoSearchResult]:
        """Execute ``worker`` across ``repos`` via ThreadPoolExecutor.

        Resolves ThreadPoolExecutor/as_completed from this module's globals so
        tests that patch ``mcp_server.dispatcher.cross_repo_coordinator.X``
        take effect.
        """
        executor_cls = globals()["ThreadPoolExecutor"]
        as_completed_fn = globals()["as_completed"]
        results: List[CrossRepoSearchResult] = []
        with executor_cls(max_workers=self.max_workers) as executor:
            futures = {executor.submit(worker, repo): repo for repo in repos}
            for fut in as_completed_fn(futures):
                try:
                    results.append(fut.result())
                except Exception as exc:
                    record_handled_error(__name__, exc)
                    repo = futures[fut]
                    results.append(
                        self.multi_repo_manager._query_unavailable(repo.repository_id, repo)
                    )
        stale = {
            repo.repository_id
            for repo in repos
            if not self.multi_repo_manager._query_ready(
                repo.repository_id, StoreRegistry.binding(repo)
            )
        }
        results = [
            result for result in results if result is not None and result.repository_id not in stale
        ]
        results.extend(
            self.multi_repo_manager._query_unavailable(repo.repository_id, repo)
            for repo in repos
            if repo.repository_id in stale
        )
        return results

    async def search_symbol(
        self, symbol: str, scope: Optional[SearchScope] = None
    ) -> "_CrossRepoAggregatedResult":
        scope = scope or SearchScope()
        start = time.time()
        repos = await self._get_target_repositories(scope)
        if not repos or (
            scope.repositories and set(scope.repositories) - {repo.repository_id for repo in repos}
        ):
            return _CrossRepoAggregatedResult(
                query=symbol,
                total_results=0,
                repositories_searched=0,
                search_time=time.time() - start,
                results=[],
                repository_stats={},
                deduplication_stats={},
                code="index_unavailable" if scope.repositories else None,
                safe_fallback="native_search" if scope.repositories else None,
            )
        per_repo = self._run_parallel(
            repos, lambda r: self._search_symbol_in_repository(symbol, r, scope)
        )
        return await self._aggregate_symbol_results(symbol, per_repo, start)

    async def search_code(
        self,
        query: str,
        scope: Optional[SearchScope] = None,
        semantic: bool = False,
        limit: Optional[int] = None,
    ) -> "_CrossRepoAggregatedResult":
        scope = scope or SearchScope()
        limit = limit or self.default_result_limit
        start = time.time()
        repos = await self._get_target_repositories(scope)
        if not repos or (
            scope.repositories and set(scope.repositories) - {repo.repository_id for repo in repos}
        ):
            return _CrossRepoAggregatedResult(
                query=query,
                total_results=0,
                repositories_searched=0,
                search_time=time.time() - start,
                results=[],
                repository_stats={},
                deduplication_stats={},
                code="index_unavailable" if scope.repositories else None,
                safe_fallback="native_search" if scope.repositories else None,
            )
        per_repo = self._run_parallel(
            repos,
            lambda r: self._search_code_in_repository(
                query, r, scope, semantic=semantic, limit=limit
            ),
        )
        return await self._aggregate_code_results(query, per_repo, start, limit=limit)

    async def get_search_statistics(self) -> Dict[str, Any]:
        repos = list(self.multi_repo_manager.list_repositories())
        total_files = sum(getattr(r, "total_files", 0) or 0 for r in repos)
        total_symbols = sum(getattr(r, "total_symbols", 0) or 0 for r in repos)
        languages: Set[str] = set()
        for r in repos:
            for lang in (getattr(r, "language_stats", None) or {}).keys():
                languages.add(lang)
        details = [
            {
                "repository_id": r.repository_id,
                "name": getattr(r, "name", ""),
                "total_files": getattr(r, "total_files", 0),
                "total_symbols": getattr(r, "total_symbols", 0),
                "language_stats": getattr(r, "language_stats", {}),
                "readiness": ReadinessClassifier.classify_registered(r).to_dict(),
                "feature_availability": {
                    "lexical": {
                        "status": (
                            "available"
                            if ReadinessClassifier.classify_registered(r).ready
                            else "unavailable"
                        )
                    },
                    "semantic": {"status": "unavailable", "reason": "runtime_status_unavailable"},
                    "graph": {"status": "unavailable", "reason": "runtime_status_unavailable"},
                    "plugins": {"status": "unavailable", "reason": "runtime_status_unavailable"},
                    "cross_repo": {"status": "available"},
                },
            }
            for r in repos
        ]
        return {
            "enabled": True,
            "total_repositories": len(repos),
            "total_files": total_files,
            "total_symbols": total_symbols,
            "languages": sorted(languages),
            "repository_details": details,
        }
