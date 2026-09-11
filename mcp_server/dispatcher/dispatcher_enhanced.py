"""Enhanced dispatcher with dynamic plugin loading via PluginFactory."""

import ast
import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

from ..artifacts.semantic_profiles import SemanticProfileRegistry
from ..config.env_vars import get_max_file_size_bytes
from ..config.settings import reload_settings
from ..core.errors import IndexingError, TransientArtifactError, record_handled_error
from ..core.ignore_patterns import EXCLUDED_DIR_PARTS as _INDEX_EXCLUDED_DIRS
from ..core.ignore_patterns import (
    build_walker_filter,
)
from ..core.repo_context import RepoContext, index_generation_key
from ..graph import (
    CHUNKER_AVAILABLE,
    ContextSelector,
    GraphAnalyzer,
    GraphCutResult,
    GraphNode,
    XRefAdapter,
)
from ..indexing.source_metadata import extract_matching_source_metadata
from ..plugin_base import IPlugin, SearchResult, SymbolDef
from ..plugins.generic_treesitter_plugin import GenericTreeSitterPlugin
from ..plugins.language_registry import get_all_extensions, get_language_by_extension
from ..plugins.plugin_factory import PluginFactory, PluginUnavailableError
from ..plugins.plugin_set_registry import PluginSetRegistry
from ..storage.multi_repo_manager import MultiRepositoryManager
from ..storage.sqlite_store import (
    SQLiteStore,
    _merge_chunk_source_metadata,
    assert_chunk_scheme_readable,
    classify_sqlite_storage_failure,
)
from ..storage.two_phase import TwoPhaseCommitError, two_phase_commit
from ..utils.semantic_indexer import SemanticIndexer
from ..utils.semantic_indexer_registry import SemanticIndexerRegistry
from .cross_repo_coordinator import (
    CrossRepositorySearchCoordinator,
    SearchScope,
)
from .fallback import run_gated_fallback
from .plugin_router import FileTypeMatcher, PluginCapability, PluginRouter
from .query_intent import QueryIntent
from .query_intent import classify as classify_query_intent
from .result_aggregator import (
    AggregatedResult,
    RankingCriteria,
    ResultAggregator,
)

logger = logging.getLogger(__name__)
_semantic_borrower = ContextVar("dispatcher_semantic_borrower", default=None)


def _semantic_operation(method):
    """Keep nested operations and lazy iterators inside one generation lease."""
    if inspect.isgeneratorfunction(method):

        @wraps(method)
        def generate(self, ctx, *args, **kwargs):
            with self._semantic_lease(ctx):
                yield from method(self, ctx, *args, **kwargs)

        return generate

    @wraps(method)
    def call(self, ctx, *args, **kwargs):
        with self._semantic_lease(ctx):
            return method(self, ctx, *args, **kwargs)

    return call


class SemanticSearchFailure(RuntimeError):
    """Raised when a semantic-only query cannot safely produce semantic output."""

    def __init__(
        self,
        message: str,
        *,
        profile_id: Optional[str] = None,
        collection_name: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.profile_id = profile_id
        self.collection_name = collection_name


class IndexResultStatus(str, Enum):
    INDEXED = "indexed"
    DELETED = "deleted"
    MOVED = "moved"
    SKIPPED_UNCHANGED = "skipped_unchanged"
    SKIPPED_TOCTOU = "skipped_toctou"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass
class IndexResult:
    status: IndexResultStatus
    path: Path
    observed_hash: str | None
    actual_hash: str | None
    error: str | None = None
    semantic: Optional[Dict[str, Any]] = None


@dataclass
class _GraphState:
    analyzer: Optional[GraphAnalyzer] = None
    selector: Optional[ContextSelector] = None
    nodes: List[GraphNode] = None  # type: ignore[assignment]
    edges: List[Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.nodes is None:
            self.nodes = []
        if self.edges is None:
            self.edges = []


_INDEX_EXCLUDED_FILENAMES = {
    "full_indexing_log.txt",
    "mcp_validation_results.json",
    "mcp_indexing_status.json",
    "semantic_indexing_progress.json",
    "mcp_indexing_summary.json",
    "complete_indexing_results.json",
    "mcp_direct_test_results.json",
    "test_queries.json",
}

_EXACT_BOUNDED_PYTHON_PATHS = {
    "tests/root_tests/run_reranking_tests.py": "exact_run_reranking_tests_rebound",
    "tests/root_tests/test_swift_plugin.py": "exact_test_swift_plugin_rebound",
    "tests/root_tests/test_mcp_database_efficiency.py": (
        "exact_test_mcp_database_efficiency_rebound"
    ),
    "tests/security/test_route_auth_coverage.py": "exact_test_route_auth_coverage_rebound",
    "tests/security/test_p24_sandbox_degradation.py": (
        "exact_test_p24_sandbox_degradation_rebound"
    ),
    "tests/test_p24_plugin_availability.py": ("exact_test_p24_plugin_availability_rebound"),
    "tests/test_dispatcher_extension_gating.py": ("exact_test_dispatcher_extension_gating_rebound"),
    "scripts/validate_mcp_comprehensive.py": "exact_validate_mcp_comprehensive_rebound",
    "scripts/migrate_large_index_to_multi_repo.py": "exact_migrate_large_index_to_multi_repo_rebound",
    "scripts/check_index_languages.py": "exact_check_index_languages_rebound",
    "scripts/check_test_index_schema.py": "exact_check_test_index_schema_rebound",
    "scripts/ensure_test_repos_indexed.py": "exact_ensure_test_repos_indexed_rebound",
    "scripts/index_missing_repos_semantic.py": "exact_index_missing_repos_semantic_rebound",
    "scripts/identify_working_indexes.py": "exact_identify_working_indexes_rebound",
    "scripts/utilities/prepare_index_for_upload.py": ("exact_prepare_index_for_upload_rebound"),
    "scripts/utilities/verify_tool_usage.py": "exact_verify_tool_usage_rebound",
    "scripts/map_repos_to_qdrant.py": "exact_map_repos_to_qdrant_rebound",
    "scripts/create_claude_code_aware_report.py": ("exact_create_claude_code_aware_report_rebound"),
    "scripts/execute_optimized_analysis.py": "exact_execute_optimized_analysis_rebound",
    "scripts/index-artifact-upload-v2.py": "exact_index_artifact_upload_v2_rebound",
    "scripts/analyze_claude_code_edits.py": "exact_analyze_claude_code_edits_rebound",
    "scripts/verify_mcp_retrieval.py": "exact_verify_mcp_retrieval_rebound",
    "scripts/run_comprehensive_query_test.py": "exact_run_comprehensive_query_test_rebound",
    "scripts/index_all_repos_semantic_full.py": ("exact_index_all_repos_semantic_full_rebound"),
    "scripts/real_strategic_recommendations.py": ("exact_real_strategic_recommendations_rebound"),
    "scripts/migrate_to_centralized.py": "exact_migrate_to_centralized_rebound",
    "scripts/reindex_current_repository.py": "exact_reindex_current_repository_rebound",
    "scripts/demo_centralized_indexes.py": "exact_demo_centralized_indexes_rebound",
    "scripts/test_mcp_protocol_direct.py": "exact_test_mcp_protocol_direct_rebound",
    "scripts/verify_embeddings.py": "exact_verify_embeddings_rebound",
    "scripts/claude_code_behavior_simulator.py": "exact_claude_code_behavior_simulator_rebound",
    "scripts/create_semantic_embeddings.py": "exact_create_semantic_embeddings_rebound",
    "scripts/consolidate_real_performance_data.py": (
        "exact_consolidate_real_performance_data_rebound"
    ),
    "mcp_server/visualization/quick_charts.py": "exact_visualization_quick_charts_rebound",
    "tests/docs/test_mre2e_evidence_contract.py": "exact_mre2e_docs_contract_rebound",
    "tests/docs/test_gagov_governance_contract.py": "exact_gagov_docs_contract_rebound",
    "tests/docs/test_gaclose_evidence_closeout.py": "exact_gaclose_docs_contract_rebound",
    "tests/docs/test_p8_deployment_security.py": "exact_p8_security_docs_contract_rebound",
    "tests/docs/test_semincr_contract.py": "exact_semincr_docs_contract_rebound",
    "tests/docs/test_gabase_ga_readiness_contract.py": ("exact_gabase_docs_contract_rebound"),
    "tests/docs/test_garc_rc_soak_contract.py": "exact_garc_docs_contract_rebound",
    "tests/docs/test_garel_ga_release_contract.py": ("exact_garel_docs_contract_rebound"),
    "tests/docs/test_p23_doc_truth.py": "exact_p23_docs_truth_rebound",
    "tests/docs/test_semdogfood_evidence_contract.py": (
        "exact_semdogfood_evidence_contract_rebound"
    ),
    "tests/security/fixtures/mock_plugin/plugin.py": "exact_mock_plugin_fixture_rebound",
    "tests/security/fixtures/mock_plugin/__init__.py": "exact_mock_plugin_fixture_init_rebound",
    "tests/integration/__init__.py": "exact_integration_package_marker_rebound",
    "tests/integration/obs/test_obs_smoke.py": "exact_integration_obs_smoke_rebound",
}

_EXACT_BOUNDED_SHELL_PATHS = {
    "scripts/preflight_upgrade.sh": "exact_preflight_upgrade_rebound",
}

_REPO_SCOPE_SUMMARY_PASS_BUDGET = 8


def _get_profile_collection_name(profile: Any, fallback: str) -> str:
    """Return the configured collection name for a semantic profile."""
    build_metadata = getattr(profile, "build_metadata", None) or {}
    collection_name = build_metadata.get("collection_name")
    if isinstance(collection_name, str) and collection_name.strip():
        return collection_name.strip()
    return fallback


_INDEX_EXCLUDED_SUFFIXES = {
    ".xml",
}


def _get_lexical_timeout_seconds() -> float:
    raw = os.getenv("MCP_INDEX_LEXICAL_TIMEOUT_SECONDS", "20")
    try:
        return max(float(raw), 1.0)
    except ValueError:
        return 20.0


#: Default per-request timeout (seconds) for the endpoint reranker transport so a
#: hung transport can never block the search path forever.
_DEFAULT_RERANK_TIMEOUT_S = 30.0


def _get_endpoint_rerank_timeout_seconds() -> float:
    """Return the endpoint reranker timeout from ``MCP_RERANK_TIMEOUT_S``.

    Falls back to :data:`_DEFAULT_RERANK_TIMEOUT_S` (30s) on an unset or
    unparseable value; clamps to a >=1s floor.
    """
    raw = os.getenv("MCP_RERANK_TIMEOUT_S")
    if raw is None:
        return _DEFAULT_RERANK_TIMEOUT_S
    try:
        return max(float(raw), 1.0)
    except (ValueError, TypeError):
        return _DEFAULT_RERANK_TIMEOUT_S


#: Reranker types that route source-code text to a third-party commercial API and
#: therefore require the active deployment profile to permit commercial egress.
_COMMERCIAL_RERANKER_TYPES = frozenset({"voyage", "cohere"})


# Redact bearer tokens, api keys, and token-like substrings before any exception
# text enters rerank diagnostics or logs. Kept deliberately small and defensive.
_SECRET_REDACTION_PATTERN = re.compile(
    r"(?i)"
    r"(?:bearer\s+[A-Za-z0-9._\-]+)"
    r"|(?:(?:api[_-]?key|apikey|token|secret|password|authorization)\s*[=:]\s*[^\s,;'\"]+)"
    r"|(?:sk-[A-Za-z0-9]{6,})"
)


def _redact_secrets(text: Optional[str]) -> Optional[str]:
    """Strip bearer/api-key/token-like substrings from *text*.

    Applied to exception messages before they are stored in diagnostics or
    written to logs so a leaked credential in a provider error can't surface.
    """
    if not text:
        return text
    return _SECRET_REDACTION_PATTERN.sub("[REDACTED]", str(text))


def _active_profile_permits_commercial() -> bool:
    """Return ``True`` only when the active deployment profile permits commercial
    (source-code egress) inference.

    Fail-closed: any import/resolution error (including
    :class:`CommercialEgressNotOptedIn` when a commercial profile is selected
    without the explicit opt-in) yields ``False`` so a commercial reranker is
    never constructed by accident.
    """
    try:
        from ..config.settings import resolve_active_deployment_profile
    except Exception:  # pragma: no cover - settings contract always importable
        return False
    try:
        contract = resolve_active_deployment_profile()
    except Exception:
        return False
    return bool(getattr(contract, "commercial_egress", False))


# Path segment penalties for lexical (BM25/fuzzy) results.
# FTS5 scores are negative; adding a positive penalty degrades rank.
_PATH_PENALTY_RULES: List[Tuple[str, float]] = [
    ("htmlcov/", 10.0),  # coverage HTML — matches every string literally
    ("docs/benchmarks/", 10.0),  # benchmark JSON files contain literal query strings
    ("/tests/", 0.3),  # test files reference queries as test data
    ("makefile", 0.5),  # build files are lower signal than source code
]
_JSON_PENALTY = 0.5  # generic .json data files are lower signal than source code


def _path_score_penalty(file_path: str) -> float:
    """Return an additive BM25 score penalty for non-source paths.

    FTS5 bm25() scores are negative (more negative = better), so adding a
    positive value here degrades a result's rank without altering others.
    """
    p = file_path.replace("\\", "/").lower()
    for segment, penalty in _PATH_PENALTY_RULES:
        if segment in p:
            return penalty
    if p.endswith(".json"):
        return _JSON_PENALTY
    return 0.0


def _filename_token_boost(query: str, file_path: str) -> float:
    """Return a negative BM25 adjustment (rank improvement) when query terms
    appear in the filename stem.

    FTS5 bm25() scores are negative; a negative return value promotes the result.
    Mirrors the _fts_rank_adjustment logic in sqlite_store.py.
    """
    stem = Path(file_path).stem.lower()
    terms = set(re.findall(r"[a-z0-9_]+", query.lower()))
    stem_tokens = set(re.findall(r"[a-z0-9]+", stem))
    overlap = len(terms & stem_tokens)
    return -min(overlap, 3) * 0.6


def _run_coro_blocking(coro: Any) -> Any:
    """Run an async coroutine from sync code, even when an event loop is active."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - propagated to caller
            error["value"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "value" in error:
        raise error["value"]
    return result.get("value")


def _run_blocking_with_timeout(func: Any, *, timeout_seconds: float) -> Any:
    """Report deadline failure only after the worker releases mutation ownership."""
    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = func()
        except BaseException as exc:  # pragma: no cover - propagated to caller
            error["value"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        # Python cannot cancel a running thread. Keep the caller's writer fence
        # until it settles; STDIO's shutdown watchdog bounds service termination.
        thread.join()
        raise TimeoutError(f"Operation exceeded {timeout_seconds:.0f} seconds; worker drained")
    if "value" in error:
        raise error["value"]
    return result.get("value")


# =====================================================================
# Reranker semantic-path capability (RERANKEND / IF-0-RERANKEND-1)
# =====================================================================
#: Text/lexical rerankers degrade a pure-vector semantic ranking, so they are
#: skipped by policy on the semantic path. This is the default capability lookup
#: for reranker *types* the dispatcher builds in-process; individual rerankers
#: may override it by declaring a ``skips_semantic_path`` attribute (see
#: :func:`_reranker_skips_semantic_path`). The endpoint (``rerank.v1``) path is
#: embedding-consistent and does NOT skip.
TEXT_ONLY_RERANKER_TYPES = frozenset({"flashrank", "cross-encoder"})


def _reranker_skips_semantic_path(reranker_type: Optional[str], reranker: Any) -> bool:
    """Whether ``reranker`` must be skipped on the pure-vector semantic path.

    Explicit capability contract (replaces the historical ad-hoc type-tuple
    branch): a reranker may DECLARE its policy via a ``skips_semantic_path``
    attribute; when absent, the decision falls back to the text-only reranker
    type set. This keeps the semantic-skip decision a stated capability rather
    than an inline special case, while requiring no change to the read-only
    reranker classes in ``reranker.py``.
    """
    declared = getattr(reranker, "skips_semantic_path", None)
    if declared is not None:
        return bool(declared)
    return reranker_type in TEXT_ONLY_RERANKER_TYPES


class _SyncDictEndpointReranker:
    """Synchronous ``List[Dict]`` facade over an async canonical :class:`IReranker`.

    The dispatcher's sync search path speaks the ``List[Dict]`` candidate shape
    and the sync ``rerank(query, candidates, top_k) -> List[Dict]`` convention of
    the in-process trio (Voyage/FlashRank/CrossEncoder). This adapter lets that
    exact path drive an async endpoint reranker (e.g.
    :class:`~mcp_server.indexer.reranker.EndpointReranker`) by:

        1. Mapping each candidate dict to a canonical ``SearchResult`` (feeding
           ``_rerank_doc or snippet or file`` into ``snippet`` for parity with
           the trio's document-text preference).
        2. Driving the async reranker through
           :class:`~mcp_server.indexer.reranker.SyncRerankerAdapter` (which uses
           the loop-safe ``run_coroutine_sync`` bridge — never ``asyncio.run``
           inside a running loop).
        3. Reordering the ORIGINAL dict objects by each ``RerankItem``'s
           ``original_rank`` so downstream metadata (``_rerank_doc``,
           ``repository_id``, scores) is preserved.

    Structured outcome signals published by the underlying reranker
    (``last_outcome`` / ``last_diagnostics`` / ``last_fallback``) are mirrored
    onto this adapter after each call so the dispatcher's ``_apply_reranker``
    outcome bookkeeping keeps working unchanged.
    """

    #: Endpoint reranking is embedding-consistent; it is NOT skipped on the
    #: semantic path (unlike in-process text rerankers).
    skips_semantic_path = False

    def __init__(self, async_reranker: Any, *, timeout: Optional[float] = None):
        from ..indexer.reranker import SyncRerankerAdapter

        self._async = async_reranker
        self._sync = SyncRerankerAdapter(async_reranker, timeout=timeout)
        self.last_fallback = None
        self.last_outcome = None
        self.last_diagnostics = None

    def rerank(self, query: str, candidates: List[Dict], top_k: int) -> List[Dict]:
        from ..indexer.reranker import SearchResult as RerankSearchResult

        search_results = [
            RerankSearchResult(
                file_path=c.get("file", ""),
                start_line=c.get("line", 0) or 0,
                end_line=c.get("line", 0) or 0,
                column=0,
                snippet=c.get("_rerank_doc") or c.get("snippet") or c.get("file", ""),
                match_type=c.get("match_type", "endpoint"),
                score=float(c.get("score", 0.0) or 0.0),
                context=c.get("context"),
                generation_key=c.get("_rerank_generation"),
            )
            for c in candidates
        ]

        result = self._sync.rerank(query, search_results, top_k)

        # Mirror any structured signals the async reranker published.
        self.last_fallback = getattr(self._async, "last_fallback", None)
        self.last_outcome = getattr(self._async, "last_outcome", None)
        self.last_diagnostics = getattr(self._async, "last_diagnostics", None)

        if not getattr(result, "is_success", False):
            # Funnel a contract/provider Result failure into the dispatcher's
            # FAILED path. The error string is a contract message (no doc text).
            raise RuntimeError(str(getattr(result, "error", "rerank failed")))

        rerank_result = result.data
        ordered: List[Dict] = []
        for item in rerank_result.results:
            idx = item.original_rank
            if 0 <= idx < len(candidates):
                ordered.append(candidates[idx])
        return ordered[:top_k]


class EnhancedDispatcher:
    """Enhanced dispatcher with dynamic plugin loading and advanced routing capabilities."""

    # Document query patterns - common documentation search terms
    DOCUMENT_QUERY_PATTERNS = [
        r"\b(how\s+to|howto)\b",
        r"\b(getting\s+started|get\s+started)\b",
        r"\b(installation|install|setup)\b",
        r"\b(configuration|configure|config)\b",
        r"\b(api\s+doc|api\s+documentation|api\s+reference)\b",
        r"\b(tutorial|guide|walkthrough)\b",
        r"\b(example|sample|snippet)\b",
        r"\b(readme|documentation|docs)\b",
        r"\b(usage|use\s+case|using)\b",
        r"\b(reference|manual)\b",
        r"\b(faq|frequently\s+asked)\b",
        r"\b(troubleshoot|troubleshooting|debug|debugging|error|errors|issue|issues)\b",
        r"\b(best\s+practice|best\s+practices|convention|conventions)\b",
        r"\b(architecture|design|overview)\b",
        r"\b(changelog|release\s+notes|migration)\b",
    ]

    # Documentation file patterns
    DOCUMENTATION_FILE_PATTERNS = [
        r"readme(\.\w+)?$",
        r"changelog(\.\w+)?$",
        r"contributing(\.\w+)?$",
        r"license(\.\w+)?$",
        r"install(\.\w+)?$",
        r"setup(\.\w+)?$",
        r"guide(\.\w+)?$",
        r"tutorial(\.\w+)?$",
        r"\.md$",
        r"\.rst$",
        r"\.txt$",
        r"docs?/",
        r"documentation/",
    ]

    def __init__(
        self,
        plugins: Optional[List[IPlugin]] = None,
        enable_advanced_features: bool = True,
        use_plugin_factory: bool = True,
        lazy_load: bool = True,
        semantic_search_enabled: bool = True,
        memory_aware: bool = True,
        multi_repo_enabled: bool = None,
        reranker_type: str = "none",
        plugin_set_registry: Optional[PluginSetRegistry] = None,
        semantic_indexer_registry: Optional[SemanticIndexerRegistry] = None,
        endpoint_rerank_transport: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        """Initialize the enhanced dispatcher.

        Args:
            plugins: Optional list of pre-instantiated plugins (for backward compatibility)
            enable_advanced_features: Whether to enable advanced routing and aggregation
            use_plugin_factory: Whether to use PluginFactory for dynamic loading
            lazy_load: Whether to lazy-load plugins on demand
            semantic_search_enabled: Whether to enable semantic search in plugins
            memory_aware: Whether to use memory-aware plugin management
            multi_repo_enabled: Whether to enable multi-repository support (None = auto from env)
            reranker_type: Reranker to use — "voyage", "flashrank", "cross-encoder", or "none"
        """
        self._memory_aware = memory_aware
        self._multi_repo_enabled = multi_repo_enabled

        # Repository-aware plugin components require a per-call ctx; initialized without store.
        self._repo_plugin_loader = None
        self._memory_manager = None

        # Initialize multi-repo manager if enabled
        if multi_repo_enabled is None:
            multi_repo_enabled = os.getenv("MCP_ENABLE_MULTI_REPO", "false").lower() == "true"

        if multi_repo_enabled:
            default_storage_path = Path.home() / ".mcp" / "indexes"
            storage_path = os.getenv("MCP_INDEX_STORAGE_PATH", str(default_storage_path))
            registry_path = Path(storage_path) / "repository_registry.json"
            self._multi_repo_manager = MultiRepositoryManager(central_index_path=registry_path)
            self._cross_repo_coordinator = CrossRepositorySearchCoordinator(
                self._multi_repo_manager
            )
        else:
            self._multi_repo_manager = None
            self._cross_repo_coordinator = None
        self._enable_advanced = enable_advanced_features
        self._use_factory = use_plugin_factory
        self._lazy_load = lazy_load
        self._semantic_enabled = semantic_search_enabled

        # Per-repo plugin registry (P3)
        self._plugin_set_registry: PluginSetRegistry = (
            plugin_set_registry if plugin_set_registry is not None else PluginSetRegistry()
        )
        # Legacy process-global plugin storage for callers that inject a pre-built plugin list.
        self._legacy_plugins: List[IPlugin] = []
        self._lang_cache: Dict[str, IPlugin] = {}
        self._loaded_languages: set[str] = set()
        self._unavailable_languages: set[str] = set()

        # Cache for file hashes to avoid re-indexing unchanged files
        self._file_cache = {}  # path -> (mtime, size, content_hash)
        self._file_cache_lock = threading.RLock()

        # Advanced components
        if self._enable_advanced:
            self._file_matcher = FileTypeMatcher()
            self._router = PluginRouter(self._file_matcher)
            self._aggregator = ResultAggregator()

        # Performance tracking
        self._operation_stats = {
            "searches": 0,
            "lookups": 0,
            "indexings": 0,
            "total_time": 0.0,
            "plugins_loaded": 0,
        }

        # Per-repo semantic indexer registry (P3)
        self._semantic_registry: Optional[SemanticIndexerRegistry] = semantic_indexer_registry
        # Fallback process-global semantic indexer for repos not tracked by the registry.
        self._semantic_indexer_fallback: Optional[SemanticIndexer] = None

        # Initialize reranker
        self._reranker = None  # type: Optional[Any]
        # Retain the configured reranker type so the outcome vocabulary can name
        # the policy identity (e.g. on the semantic-skip path) truthfully.
        self._reranker_type = reranker_type
        # Structured diagnostics for the most recent _apply_reranker call.
        self._last_rerank_diagnostics = None  # type: Optional[Any]
        if reranker_type and reranker_type != "none":
            try:
                self._reranker = self._build_reranker(
                    reranker_type, endpoint_rerank_transport=endpoint_rerank_transport
                )
            except Exception as e:
                logger.warning(f"Failed to initialize reranker: {type(e).__name__}")
        # Skip text/lexical rerankers on the pure-vector semantic path. Expressed
        # as an explicit capability contract (see _reranker_skips_semantic_path):
        # a reranker may declare `skips_semantic_path`; otherwise the text-only
        # type set decides. The endpoint (rerank.v1) path is embedding-consistent
        # and does not skip; VoyageReranker ("voyage") is Voyage-consistent too.
        self._reranker_skips_semantic = _reranker_skips_semantic_path(reranker_type, self._reranker)

        # Initialize legacy plugin list (backward compatibility for callers injecting plugins)
        if plugins:
            self._legacy_plugins = plugins
            self._lang_cache = {p.lang: p for p in plugins}
            for plugin in plugins:
                self._loaded_languages.add(getattr(plugin, "lang", "unknown"))
            if self._enable_advanced:
                self._register_plugins_with_router()
        elif use_plugin_factory and not lazy_load:
            self._load_all_plugins()

        # Compile document query patterns for performance
        self._compiled_doc_patterns = [
            re.compile(pattern, re.IGNORECASE) for pattern in self.DOCUMENT_QUERY_PATTERNS
        ]
        self._compiled_file_patterns = [
            re.compile(pattern, re.IGNORECASE) for pattern in self.DOCUMENTATION_FILE_PATTERNS
        ]

        # Graph analysis components (lazy initialized)
        self._graph_builder: Optional[XRefAdapter] = None
        self._graph_state: Dict[str, _GraphState] = {}

        # Bounded fallback timeout (IF-0-P11-2)
        try:
            _ms = int(os.environ.get("MCP_DISPATCHER_FALLBACK_MS", "2000"))
            if _ms <= 0:
                raise ValueError("non-positive")
            self._fallback_timeout_ms = _ms
        except (ValueError, TypeError):
            logger.warning("Invalid MCP_DISPATCHER_FALLBACK_MS value; using default 2000ms")
            self._fallback_timeout_ms = 2000

        # Fallback histogram handle (IF-0-P11-3)
        try:
            from mcp_server.metrics.prometheus_exporter import get_prometheus_exporter

            self._fallback_histogram = get_prometheus_exporter().dispatcher_fallback_histogram
        except Exception as exc:
            record_handled_error(__name__, exc)
            self._fallback_histogram = None

        logger.info("Enhanced dispatcher initialized")

    def _load_all_plugins(self):
        """Load all available plugins using PluginFactory with timeout protection."""
        logger.info("Loading all available plugins with timeout...")

        import signal
        from contextlib import contextmanager

        @contextmanager
        def timeout(seconds):
            def timeout_handler(signum, frame):
                raise TimeoutError(f"Plugin loading timed out after {seconds}s")

            # Only use alarm on Unix-like systems
            if hasattr(signal, "SIGALRM"):
                old_handler = signal.signal(signal.SIGALRM, timeout_handler)
                signal.alarm(seconds)
                try:
                    yield
                finally:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old_handler)
            else:
                # On Windows, just yield without timeout
                yield

        try:
            with timeout(30):  # 30 second timeout for startup plugin loading
                # Use repository-aware loading if available
                if self._repo_plugin_loader and self._memory_aware:
                    languages_to_load = self._repo_plugin_loader.get_required_plugins()
                    priority_order = self._repo_plugin_loader.get_priority_languages()
                    self._repo_plugin_loader.log_loading_plan()

                    for lang in priority_order:
                        if lang in languages_to_load:
                            try:
                                if self._memory_manager:
                                    plugin = self._memory_manager.get_plugin(lang)
                                else:
                                    plugin = PluginFactory.create_plugin(
                                        lang, None, self._semantic_enabled
                                    )

                                if plugin:
                                    self._legacy_plugins.append(plugin)
                                    self._lang_cache[lang] = plugin
                                    self._loaded_languages.add(lang)
                                    self._operation_stats["plugins_loaded"] += 1
                                    self._repo_plugin_loader.mark_loaded(lang)
                            except PluginUnavailableError as e:
                                logger.info(
                                    "Skipping unavailable plugin for %s: %s",
                                    lang,
                                    e.state,
                                )
                                self._unavailable_languages.add(lang)
                            except Exception as e:
                                logger.error(f"Failed to load {lang} plugin: {type(e).__name__}")
                else:
                    all_plugins = PluginFactory.create_all_plugins(
                        sqlite_store=None,
                        enable_semantic=self._semantic_enabled,
                    )

                    for lang, plugin in all_plugins.items():
                        self._legacy_plugins.append(plugin)
                        self._lang_cache[lang] = plugin
                        self._loaded_languages.add(lang)
                        self._operation_stats["plugins_loaded"] += 1

                if self._enable_advanced:
                    self._register_plugins_with_router()

                logger.info(
                    f"Loaded {len(self._legacy_plugins)} plugins: {', '.join(sorted(self._loaded_languages))}"
                )

        except TimeoutError as e:
            logger.warning(f"Plugin loading timeout: {type(e).__name__}")
            self._legacy_plugins = []
            self._loaded_languages = set()
        except Exception as e:
            logger.error(f"Plugin loading failed: {type(e).__name__}")
            self._legacy_plugins = []
            self._loaded_languages = set()

    def _ensure_plugin_loaded(self, language: str) -> Optional[IPlugin]:
        """Ensure a plugin for the given language is loaded.

        Args:
            language: Language code (e.g., 'python', 'go')

        Returns:
            Plugin instance or None if not available
        """
        # Normalize language
        language = language.lower().replace("-", "_")

        # Check if already loaded
        if language in self._lang_cache:
            return self._lang_cache[language]

        # If not using factory or already tried to load, return None
        if (
            not self._use_factory
            or language in self._loaded_languages
            or language in self._unavailable_languages
        ):
            return None

        # Try to load the plugin
        try:
            logger.info(f"Lazy loading plugin for {language}")
            plugin = PluginFactory.create_plugin(
                language,
                sqlite_store=None,
                enable_semantic=self._semantic_enabled,
            )

            # Add to legacy collections
            self._legacy_plugins.append(plugin)
            self._lang_cache[language] = plugin
            self._loaded_languages.add(language)
            self._operation_stats["plugins_loaded"] += 1

            # Register with router if needed
            if self._enable_advanced:
                capabilities = self._detect_plugin_capabilities(plugin)
                self._router.register_plugin(plugin, capabilities)

            logger.info(f"Successfully loaded {language} plugin")
            return plugin

        except PluginUnavailableError as e:
            logger.info("No plugin available for %s: %s", language, e.state)
            self._unavailable_languages.add(language)
            return None
        except Exception as e:
            logger.error(f"Unexpected plugin load failure for {language}: {type(e).__name__}")
            self._loaded_languages.add(language)  # Mark as attempted
            return None

    def _ensure_plugin_for_file(self, path: Path) -> Optional[IPlugin]:
        """Ensure a plugin is loaded for the given file.

        Args:
            path: File path

        Returns:
            Plugin instance or None if not available
        """
        # Get language from file extension
        extension = path.suffix.lower()
        language = get_language_by_extension(extension)

        if language:
            return self._ensure_plugin_loaded(language)

        # Fallback: try all loaded legacy plugins
        for plugin in self._legacy_plugins:
            if plugin.supports(path):
                return plugin

        return None

    def _register_plugins_with_router(self):
        """Register legacy plugins with the router and assign capabilities."""
        for plugin in self._legacy_plugins:
            capabilities = self._detect_plugin_capabilities(plugin)
            self._router.register_plugin(plugin, capabilities)

    def _detect_plugin_capabilities(self, plugin: IPlugin) -> List[PluginCapability]:
        """Detect capabilities for a plugin based on its language and features."""
        capabilities = []
        lang = getattr(plugin, "lang", "unknown")

        # Base capabilities all plugins have
        capabilities.append(
            PluginCapability(
                "syntax_analysis",
                "1.0",
                f"{lang} syntax analysis",
                priority=70,
                metadata={"language": lang},
            )
        )

        capabilities.append(
            PluginCapability(
                "code_search",
                "1.0",
                f"{lang} code search",
                priority=80,
                metadata={"language": lang},
            )
        )

        # Check for semantic search capability
        if hasattr(plugin, "_enable_semantic") and plugin._enable_semantic:
            capabilities.append(
                PluginCapability(
                    "semantic_search",
                    "1.0",
                    f"{lang} semantic search",
                    priority=90,
                    metadata={"language": lang},
                )
            )

        # Language-specific capabilities
        if lang == "python":
            capabilities.extend(
                [
                    PluginCapability("refactoring", "1.0", "Python refactoring support", 75),
                    PluginCapability("type_analysis", "1.0", "Python type analysis", 85),
                ]
            )
        elif lang in ["javascript", "typescript"]:
            capabilities.extend(
                [
                    PluginCapability("linting", "1.0", "JavaScript/TypeScript linting", 85),
                    PluginCapability("bundling_analysis", "1.0", "Module bundling analysis", 70),
                    PluginCapability("framework_support", "1.0", "Framework-specific support", 75),
                ]
            )
        elif lang in ["c", "cpp"]:
            capabilities.extend(
                [
                    PluginCapability("compilation_analysis", "1.0", "Compilation analysis", 80),
                    PluginCapability("memory_analysis", "1.0", "Memory usage analysis", 70),
                    PluginCapability("performance_profiling", "1.0", "Performance profiling", 75),
                ]
            )
        elif lang in ["go", "rust"]:
            capabilities.extend(
                [
                    PluginCapability("package_analysis", "1.0", f"{lang} package analysis", 80),
                    PluginCapability(
                        "concurrency_analysis",
                        "1.0",
                        f"{lang} concurrency analysis",
                        75,
                    ),
                ]
            )
        elif lang in ["java", "kotlin", "scala"]:
            capabilities.extend(
                [
                    PluginCapability("jvm_analysis", "1.0", "JVM bytecode analysis", 75),
                    PluginCapability("build_tool_integration", "1.0", "Build tool integration", 70),
                ]
            )

        return capabilities

    def plugins(self) -> List[IPlugin]:
        """Return all loaded legacy plugins."""
        return list(self._legacy_plugins)

    def _managed_plugin_count(self, repo_id: str) -> int:
        """Read managed worker count without requiring newer test doubles."""
        loaded_count = getattr(self._plugin_set_registry, "loaded_count", None)
        if callable(loaded_count):
            try:
                count = loaded_count(repo_id)
                if isinstance(count, int):
                    return count
            except Exception as exc:
                logger.warning("Per-repo plugin count failed: %s", type(exc).__name__)
        snapshot_fn = getattr(self._plugin_set_registry, "resource_snapshot", None)
        if callable(snapshot_fn):
            try:
                reserved = getattr(snapshot_fn(), "reserved_workers", None)
                if isinstance(reserved, int):
                    return reserved
            except Exception as exc:
                logger.warning("Plugin resource snapshot failed: %s", type(exc).__name__)
        return len(self._plugin_set_registry.plugins_for(repo_id))

    def shutdown(self) -> None:
        """Close managed plugin workers and any explicitly injected adapters."""
        if self._semantic_registry is not None:
            self._semantic_registry.shutdown()
        if hasattr(self._plugin_set_registry, "shutdown"):
            self._plugin_set_registry.shutdown()
        for plugin in self._legacy_plugins:
            close = getattr(plugin, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    logger.warning("Legacy plugin close failed: %s", type(exc).__name__)
        self._legacy_plugins.clear()
        self._lang_cache.clear()

    def supported_languages(self) -> List[str]:
        """Get list of all supported languages (loaded and available)."""
        if self._use_factory:
            return PluginFactory.get_supported_languages()
        else:
            return list(self._lang_cache.keys())

    def _get_file_hash(self, content: str) -> str:
        """Compute SHA-256 hash of file content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _hash_file_bytes(path: Path) -> str:
        """Compute SHA-256 hash of raw file bytes (used for TOCTOU guard)."""
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _should_reindex(
        self, path: Path, content: str, store: Optional[SQLiteStore] = None
    ) -> bool:
        """Return True if the file needs to be (re-)indexed."""
        if isinstance(store, SQLiteStore):
            persisted = store.get_file(path)
            if not persisted or persisted.get("content_hash") != self._get_file_hash(content):
                return True
        key = str(path)
        with self._file_cache_lock:
            if key not in self._file_cache:
                return True
            cached_mtime, cached_size, cached_hash = self._file_cache[key]
        try:
            stat = path.stat()
            if stat.st_mtime == cached_mtime and stat.st_size == cached_size:
                return False
            return self._get_file_hash(content) != cached_hash
        except OSError:
            return True

    def _match_plugin(self, ctx: Union[RepoContext, Path], path: Optional[Path] = None) -> IPlugin:
        """Match a plugin for the given file path."""
        repo_ctx: Optional[RepoContext]
        if path is None:
            repo_ctx = None
            target_path = Path(ctx)  # type: ignore[arg-type]
        else:
            repo_ctx = ctx  # type: ignore[assignment]
            target_path = path

        # Check explicitly-registered legacy plugins first
        for p in self._legacy_plugins:
            if p.supports(target_path):
                return p

        if repo_ctx is not None:
            managed = self._plugin_set_registry.plugins_for_file(repo_ctx, target_path)
            if managed:
                return managed[0][0]
            language = get_language_by_extension(target_path.suffix.lower())
            if language:
                availability = PluginFactory.get_plugin_availability(language)
                if availability.get("state") != "enabled":
                    compatibility_plugin = self._ensure_plugin_loaded(language)
                    if compatibility_plugin:
                        return compatibility_plugin

        # Use advanced routing if available
        if self._enable_advanced and self._router:
            route_result = self._router.get_best_plugin(target_path)
            if route_result:
                return route_result.plugin

        raise RuntimeError(f"No plugin for {target_path}")

    def get_plugins_for_file(self, ctx: RepoContext, path: Path) -> List[Tuple[IPlugin, float]]:
        """Get all plugins that can handle a file with confidence scores."""
        if self._legacy_plugins and self._enable_advanced and self._router:
            route_results = self._router.route_file(path)
            return [(result.plugin, result.confidence) for result in route_results]
        if self._legacy_plugins:
            matching_plugins: List[Tuple[IPlugin, float]] = []
            for plugin in self._legacy_plugins:
                if plugin.supports(path):
                    matching_plugins.append((plugin, 1.0))
            return matching_plugins
        return self._plugin_set_registry.plugins_for_file(ctx, path)

    def lookup(self, ctx: RepoContext, symbol: str, limit: int = 20) -> Optional[SymbolDef]:
        """Look up symbol definition within ctx.repo_id."""
        _hp_t0 = time.perf_counter()
        start_time = time.time()

        try:
            # For symbol lookup, prefer BM25 direct lookup to avoid plugin loading delays
            # Only load plugins if explicitly needed and BM25 fails
            if ctx.sqlite_store:
                logger.debug("Using BM25 lookup directly for better performance")
                try:
                    import sqlite3

                    conn = sqlite3.connect(ctx.sqlite_store.db_path)
                    cursor = conn.cursor()

                    # First try symbols table for exact matches
                    cursor.execute(
                        """
                        SELECT s.name, s.kind, s.line_start, s.line_end,
                               s.signature, s.documentation, f.path, f.language
                        FROM symbols s
                        JOIN files f ON s.file_id = f.id
                        WHERE s.name = ? OR s.name LIKE ?
                        ORDER BY CASE WHEN s.name = ? THEN 0 ELSE 1 END
                        LIMIT 1
                    """,
                        (symbol, f"%{symbol}%", symbol),
                    )

                    row = cursor.fetchone()
                    if row:
                        name, kind, line, end_line, signature, doc, filepath, language = row
                        conn.close()

                        # Return proper SymbolDef dict
                        return {
                            "symbol": name,
                            "kind": kind,
                            "language": language or "unknown",
                            "signature": signature or f"{kind} {name}",
                            "doc": doc,
                            "defined_in": filepath,
                            "line": line or 1,
                            "start_line": line or 1,
                            "end_line": end_line or line or 1,
                            "span": (0, len(name)),
                        }

                    # Fallback to BM25 if available
                    try:
                        patterns = [
                            f"class {symbol}",
                            f"def {symbol}",
                            f"function {symbol}",
                            symbol,  # Try exact symbol match as fallback
                        ]

                        for pattern in patterns:
                            cursor.execute(
                                """
                                SELECT filepath, snippet(bm25_content, -1, '', '', '...', 20), language
                                FROM bm25_content
                                WHERE bm25_content MATCH ?
                                ORDER BY rank
                                LIMIT 1
                            """,
                                (pattern,),
                            )

                            row = cursor.fetchone()
                            if row:
                                filepath, snippet, language = row

                                # Determine kind from pattern
                                pattern_lower = pattern.lower()
                                if "class" in pattern_lower:
                                    kind = "class"
                                elif "def" in pattern_lower or "function" in pattern_lower:
                                    kind = "function"
                                else:
                                    kind = "symbol"

                                conn.close()

                                return {
                                    "symbol": symbol,
                                    "kind": kind,
                                    "language": language or "unknown",
                                    "signature": snippet,
                                    "doc": None,
                                    "defined_in": filepath,
                                    "line": 1,
                                    "start_line": 1,
                                    "end_line": 1,
                                    "span": (0, len(symbol)),
                                }
                    except sqlite3.OperationalError:
                        # BM25 table doesn't exist, that's fine
                        pass

                    conn.close()
                except Exception as e:
                    logger.error(f"Error in direct symbol lookup: {type(e).__name__}")

            repo_plugins = self._plugin_set_registry.plugins_for(ctx.repo_id)

            # Derive source_ext from a BM25 filepath hint so run_gated_fallback
            # can gate plugins by extension.
            row_filepath: Optional[str] = None
            if ctx.sqlite_store:
                try:
                    import sqlite3 as _sqlite3

                    _conn = _sqlite3.connect(ctx.sqlite_store.db_path)
                    try:
                        _cur = _conn.cursor()
                        _cur.execute(
                            "SELECT filepath FROM bm25_content WHERE bm25_content MATCH ? LIMIT 1",
                            (symbol,),
                        )
                        _hint = _cur.fetchone()
                        if _hint:
                            row_filepath = _hint[0]
                    except Exception as exc:
                        record_handled_error(__name__, exc)
                        pass
                    finally:
                        _conn.close()
                except Exception as exc:
                    record_handled_error(__name__, exc)
                    pass

            source_ext = os.path.splitext(row_filepath)[1].lower() if row_filepath else None

            result = run_gated_fallback(
                plugins=repo_plugins,
                symbol=symbol,
                source_ext=source_ext,
                timeout_ms=self._fallback_timeout_ms,
                histogram=self._fallback_histogram,
            )

            self._operation_stats["lookups"] += 1
            self._operation_stats["total_time"] += time.time() - start_time
            return result

        except Exception as e:
            logger.error("Symbol lookup failed (%s)", type(e).__name__)
            return None
        finally:
            try:
                from mcp_server.metrics.prometheus_exporter import get_prometheus_exporter

                _hist = get_prometheus_exporter().dispatcher_lookup_histogram
                if _hist is not None:
                    _hist.observe(time.perf_counter() - _hp_t0)
            except Exception as exc:
                record_handled_error(__name__, exc)
                pass

    def _is_document_query(self, query: str) -> bool:
        """Check if the query is looking for documentation.

        Args:
            query: Search query string

        Returns:
            True if this appears to be a documentation query
        """
        query_lower = query.lower()

        # Check against document query patterns
        for pattern in self._compiled_doc_patterns:
            if pattern.search(query_lower):
                return True

        # Check for question words at the beginning
        question_starters = [
            "how",
            "what",
            "where",
            "when",
            "why",
            "can",
            "is",
            "does",
            "should",
        ]
        first_word = query_lower.split()[0] if query_lower.split() else ""
        if first_word in question_starters:
            return True

        return False

    def _expand_document_query(self, query: str) -> List[str]:
        """Expand a document query with related terms for better search coverage.

        Args:
            query: Original search query

        Returns:
            List of expanded query variations
        """
        expanded_queries = [query]  # Always include original
        query_lower = query.lower()

        # Common expansions for documentation queries
        expansions = {
            "install": ["installation", "setup", "getting started", "requirements"],
            "config": [
                "configuration",
                "configure",
                "settings",
                "options",
                "parameters",
            ],
            "api": ["api documentation", "api reference", "endpoint", "method"],
            "how to": ["tutorial", "guide", "example", "usage"],
            "example": ["sample", "snippet", "demo", "code example"],
            "error": ["troubleshoot", "debug", "issue", "problem", "fix"],
            "getting started": ["quickstart", "tutorial", "introduction", "setup"],
            "guide": ["tutorial", "documentation", "walkthrough", "how to"],
            "usage": ["how to use", "example", "api", "reference"],
        }

        # Apply expansions
        for term, related_terms in expansions.items():
            if term in query_lower:
                for related in related_terms:
                    # Replace the term with related term
                    expanded = query_lower.replace(term, related)
                    if expanded != query_lower and expanded not in expanded_queries:
                        expanded_queries.append(expanded)

                # Also add queries with additional terms
                for related in related_terms[:2]:  # Limit to avoid too many queries
                    expanded = f"{query} {related}"
                    if expanded not in expanded_queries:
                        expanded_queries.append(expanded)

        # Add file-specific searches for common documentation files
        if self._is_document_query(query):
            # Extract the main topic from the query
            topic_words = []
            for word in query.lower().split():
                if word not in [
                    "how",
                    "to",
                    "the",
                    "a",
                    "an",
                    "is",
                    "are",
                    "what",
                    "where",
                    "when",
                ]:
                    topic_words.append(word)

            if topic_words:
                topic = " ".join(topic_words[:2])  # Use first two topic words
                expanded_queries.extend(
                    [
                        f"README {topic}",
                        f"{topic} documentation",
                        f"{topic} docs",
                        f"{topic} guide",
                    ]
                )

        # Remove duplicates while preserving order
        seen = set()
        unique_queries = []
        for q in expanded_queries:
            if q not in seen:
                seen.add(q)
                unique_queries.append(q)

        return unique_queries[:10]  # Limit to 10 queries max

    def _is_documentation_file(self, file_path: str) -> bool:
        """Check if a file path is likely a documentation file.

        Args:
            file_path: Path to check

        Returns:
            True if this appears to be a documentation file
        """
        path_lower = file_path.lower()

        for pattern in self._compiled_file_patterns:
            if pattern.search(path_lower):
                return True

        return False

    def _adjust_ranking_for_documents(
        self, query: str, results: List[AggregatedResult]
    ) -> List[AggregatedResult]:
        """Adjust ranking to prioritize documentation files for document queries.

        Args:
            query: Original search query
            results: List of aggregated results

        Returns:
            Re-ranked results with documentation prioritized
        """
        if not self._is_document_query(query):
            return results

        # Separate documentation and code results
        doc_results = []
        code_results = []

        for result in results:
            if self._is_documentation_file(result.primary_result.get("file", "")):
                # Boost documentation files for document queries
                result.rank_score *= 1.5
                result.metadata["doc_boost"] = True
                doc_results.append(result)
            else:
                code_results.append(result)

        # Sort each group by rank score
        doc_results.sort(key=lambda r: r.rank_score, reverse=True)
        code_results.sort(key=lambda r: r.rank_score, reverse=True)

        # Combine with documentation files first
        return doc_results + code_results

    def _get_chunk_content_for_reranking(
        self, sqlite_store: Optional[SQLiteStore], file_path: str
    ) -> str:
        """Return the best chunk content for a file to use as reranker document text."""
        if not sqlite_store or not file_path:
            return ""
        try:
            file_id = sqlite_store.get_file_id_by_path(file_path)
            if file_id is None:
                return ""
            chunk = sqlite_store.find_best_chunk_for_file(file_id, [])
            return chunk.get("content", "") if chunk else ""
        except Exception as exc:
            record_handled_error(__name__, exc)
            return ""

    def _symbol_route(
        self, sqlite_store: Optional[SQLiteStore], name: str, kind: Optional[str], limit: int
    ) -> List[Dict]:
        """Query the symbols table directly and return search-result-shaped dicts."""
        if not sqlite_store:
            return []
        try:
            rows = sqlite_store.get_symbol(name, kind=kind)
            if not rows and kind:
                # Relax kind constraint and retry
                rows = sqlite_store.get_symbol(name, kind=None)
            if not rows:
                return []

            # Sort: non-__init__ files first, then by line number ascending
            def _rank(row: Dict) -> tuple:
                fp = (row.get("file_path") or "").replace("\\", "/")
                is_init = 1 if (fp.endswith("/__init__.py") or fp == "__init__.py") else 0
                return (is_init, row.get("line_start", 0))

            rows.sort(key=_rank)
            results = []
            for row in rows[:limit]:
                file_path = row.get("file_path", "")
                snippet = row.get("signature") or row.get("documentation") or ""
                results.append(
                    {
                        "file": file_path,
                        "line": row.get("line_start", 1),
                        "line_end": row.get("line_end"),
                        "symbol": row.get("name"),
                        "snippet": snippet,
                        "score": 1.0,
                        "language": "unknown",
                    }
                )
            return results
        except Exception as e:
            logger.warning("Symbol route failed (%s)", type(e).__name__)
            return []

    @classmethod
    def wrap_endpoint_reranker(
        cls, async_reranker: Any, *, timeout: Optional[float] = None
    ) -> "_SyncDictEndpointReranker":
        """Wrap an async canonical :class:`IReranker` for the sync dispatcher path.

        Returns a :class:`_SyncDictEndpointReranker` that presents the sync
        ``rerank(query, List[Dict], top_k) -> List[Dict]`` convention while
        driving ``async_reranker`` through the loop-safe sync/async bridge. This
        is the single seam used both by ``_build_reranker`` (production wiring)
        and by tests.
        """
        return _SyncDictEndpointReranker(async_reranker, timeout=timeout)

    def _build_reranker(
        self,
        reranker_type: str,
        *,
        endpoint_rerank_transport: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> Optional[Any]:
        """Construct the reranker object for ``reranker_type`` or ``None``.

        In-process trio (Voyage/FlashRank/CrossEncoder) present the sync
        ``List[Dict]`` interface directly. The canonical ``endpoint`` type routes
        through :class:`_SyncDictEndpointReranker` over an injected ``rerank.v1``
        transport; with no transport it fails safe to ``None`` rather than
        constructing a broken reranker.
        """
        from ..indexer.reranker import (
            CrossEncoderReranker,
            EndpointReranker,
            FlashRankReranker,
            VoyageReranker,
        )

        if reranker_type in _COMMERCIAL_RERANKER_TYPES and not _active_profile_permits_commercial():
            logger.warning(
                "Commercial reranker '%s' requested but the active deployment "
                "profile does not permit commercial egress; disabling reranker",
                reranker_type,
            )
            return None

        if reranker_type == "voyage":
            voyage_model = os.getenv("VOYAGE_RERANK_MODEL", "rerank-2")
            logger.info(f"Reranker initialized: VoyageReranker model={voyage_model}")
            return VoyageReranker(model=voyage_model)
        if reranker_type == "flashrank":
            flashrank_model = os.getenv("FLASHRANK_MODEL", "ms-marco-MiniLM-L-12-v2")
            logger.info(f"Reranker initialized: FlashRankReranker model={flashrank_model}")
            return FlashRankReranker(model=flashrank_model)
        if reranker_type == "cross-encoder":
            ce_model = os.getenv("CROSS_ENCODER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
            logger.info(f"Reranker initialized: CrossEncoderReranker model={ce_model}")
            return CrossEncoderReranker(model=ce_model)
        if reranker_type == "endpoint":
            if endpoint_rerank_transport is None:
                logger.warning(
                    "reranker_type 'endpoint' requires an injected rerank.v1 "
                    "transport; disabling reranker"
                )
                return None
            provider = os.getenv("RERANK_ENDPOINT_PROVIDER", "endpoint")
            endpoint = EndpointReranker(endpoint_rerank_transport, provider=provider)
            logger.info(f"Reranker initialized: EndpointReranker provider={provider}")
            return self.wrap_endpoint_reranker(
                endpoint, timeout=_get_endpoint_rerank_timeout_seconds()
            )
        logger.warning(f"Unknown reranker_type '{reranker_type}', disabling reranker")
        return None

    def _record_rerank_diagnostics(self, diagnostics) -> None:
        """Store and log a structured rerank outcome.

        Only ids, counts, provider names, timings, and errors are recorded —
        never raw code, snippet, or document text.
        """
        self._last_rerank_diagnostics = diagnostics
        logger.info("rerank outcome: %s", diagnostics.to_dict())

    def _apply_reranker(
        self,
        sqlite_store: Optional[SQLiteStore],
        query: str,
        candidates: List[Dict],
        limit: int,
        semantic_source: bool = False,
    ) -> List[Dict]:
        """Enrich candidates with chunk text and apply the reranker if configured.

        Records a truthful :class:`RerankDiagnostics` for every exit path so a
        failed or unchanged ordering is never reported as ``succeeded``.
        """
        import time as _time

        from ..indexer.reranker import RerankDiagnostics, RerankOutcome

        provider = self._reranker_type if self._reranker is not None else None

        def _order_key(items):
            # Stable content key (file, line) — compare-only, no text leakage.
            return [(c.get("file"), c.get("line")) for c in items]

        if self._reranker is None:
            self._record_rerank_diagnostics(
                RerankDiagnostics(
                    outcome=RerankOutcome.NOT_CONFIGURED,
                    candidate_count=len(candidates),
                    returned_count=min(len(candidates), limit),
                )
            )
            return candidates[:limit]

        if semantic_source and self._reranker_skips_semantic:
            self._record_rerank_diagnostics(
                RerankDiagnostics(
                    outcome=RerankOutcome.SKIPPED_POLICY,
                    provider=provider,
                    policy=self._reranker_type,
                    candidate_count=len(candidates),
                    returned_count=min(len(candidates), limit),
                )
            )
            return candidates[:limit]

        # Exclude paths with penalty >= 1.0 (benchmark/coverage noise files).
        filtered = [c for c in candidates if _path_score_penalty(c.get("file", "")) < 1.0]
        if not filtered:
            filtered = candidates
        for c in filtered:
            if sqlite_store is not None and sqlite_store.registry_binding is not None:
                c["_rerank_generation"] = hashlib.sha256(
                    json.dumps(sqlite_store.registry_binding, sort_keys=True, default=str).encode()
                ).hexdigest()
            if not c.get("_rerank_doc"):
                c["_rerank_doc"] = self._get_chunk_content_for_reranking(
                    sqlite_store, c.get("file", "")
                )

        # Reset any stale fallback signal before the attempt.
        if hasattr(self._reranker, "last_fallback"):
            self._reranker.last_fallback = None

        _start = _time.perf_counter()
        try:
            reranked = self._reranker.rerank(query, filtered, limit)
        except Exception as e:
            duration_ms = (_time.perf_counter() - _start) * 1000.0
            logger.warning(
                "_apply_reranker failed, returning original order: %s",
                _redact_secrets(str(type(e).__name__)),
            )
            self._record_rerank_diagnostics(
                RerankDiagnostics(
                    outcome=RerankOutcome.FAILED,
                    provider=provider,
                    candidate_count=len(filtered),
                    returned_count=min(len(candidates), limit),
                    duration_ms=duration_ms,
                    error=_redact_secrets(str(e)),
                )
            )
            return candidates[:limit]

        duration_ms = (_time.perf_counter() - _start) * 1000.0
        reordered = _order_key(reranked) != _order_key(filtered[: len(reranked)])

        # A reranker MAY publish a fallback signal (provider names only).
        fallback = getattr(self._reranker, "last_fallback", None)
        if fallback is not None:
            self._record_rerank_diagnostics(
                RerankDiagnostics(
                    outcome=RerankOutcome.FALLBACK_APPLIED,
                    provider=provider,
                    failed_provider=getattr(fallback, "failed_provider", None),
                    fallback_provider=getattr(fallback, "fallback_provider", None),
                    candidate_count=len(filtered),
                    returned_count=len(reranked),
                    reordered=reordered,
                    duration_ms=duration_ms,
                )
            )
            return reranked

        # A sync reranker MAY swallow a provider failure and return the original
        # order while surfacing the reason via `last_error`. That is a FAILED
        # outcome, NOT a no-op ATTEMPTED — otherwise a masked failure masquerades
        # as a benign unchanged ordering.
        last_error = getattr(self._reranker, "last_error", None)
        if last_error is not None:
            self._record_rerank_diagnostics(
                RerankDiagnostics(
                    outcome=RerankOutcome.FAILED,
                    provider=provider,
                    candidate_count=len(filtered),
                    returned_count=len(reranked),
                    reordered=reordered,
                    duration_ms=duration_ms,
                    error=_redact_secrets(str(last_error)),
                )
            )
            return reranked

        outcome = RerankOutcome.SUCCEEDED if reordered else RerankOutcome.ATTEMPTED
        self._record_rerank_diagnostics(
            RerankDiagnostics(
                outcome=outcome,
                provider=provider,
                candidate_count=len(filtered),
                returned_count=len(reranked),
                reordered=reordered,
                duration_ms=duration_ms,
            )
        )
        return reranked

    def _is_registered_context(self, ctx: RepoContext) -> bool:
        entry = getattr(ctx, "registry_entry", None)
        if entry is None:
            return False
        return isinstance(getattr(entry, "path", None), (str, Path))

    @staticmethod
    def _input_path(ctx: RepoContext, path: Union[str, Path]) -> Path:
        path = Path(path).absolute()
        if ctx.staging:
            canonical_root = Path(ctx.registry_entry.path).resolve()
            if path.is_relative_to(canonical_root) and not path.is_relative_to(ctx.workspace_root):
                path = ctx.workspace_root / path.relative_to(canonical_root)
        return path

    def _get_semantic_indexer(self, ctx: RepoContext) -> Optional[SemanticIndexer]:
        """Return the resource borrowed by this operation, never an unscoped owner."""
        borrowed = _semantic_borrower.get()
        if borrowed is not None and borrowed[:2] == (id(self), id(ctx)):
            return borrowed[2]
        if not getattr(self, "_semantic_enabled", True):
            return None
        if self._semantic_registry is not None or self._is_registered_context(ctx):
            raise RuntimeError("Semantic access requires a scoped generation lease")
        return self._semantic_indexer_fallback

    @contextmanager
    def _semantic_lease(self, ctx: RepoContext):
        borrowed = _semantic_borrower.get()
        if borrowed is not None and borrowed[:2] == (id(self), id(ctx)):
            yield borrowed[2]
            return
        with ExitStack() as resources:
            indexer = None
            if getattr(self, "_semantic_enabled", False):
                try:
                    if self._semantic_registry is not None:
                        indexer = resources.enter_context(
                            self._semantic_registry.lease(ctx.repo_id, ctx=ctx)
                        )
                    elif self._is_registered_context(ctx):
                        raise RuntimeError("Registered semantic generation owner is unavailable")
                    else:
                        indexer = self._semantic_indexer_fallback
                except Exception as exc:
                    if getattr(ctx, "staging", False):
                        raise
                    logger.warning("Semantic generation unavailable (%s)", type(exc).__name__)
            token = _semantic_borrower.set((id(self), id(ctx), indexer))
            try:
                yield indexer
            finally:
                _semantic_borrower.reset(token)

    def _sqlite_repository_id(self, ctx: RepoContext) -> int:
        repo_info = ctx.registry_entry
        raw_path = getattr(repo_info, "path", None) if repo_info is not None else None
        if not isinstance(raw_path, (str, Path)):
            raw_path = ctx.workspace_root
        repo_path = Path(raw_path)
        repo_name = getattr(repo_info, "name", None) if repo_info is not None else None
        metadata = {"repo_id": ctx.repo_id}
        return ctx.sqlite_store.ensure_repository_row(repo_path, name=repo_name, metadata=metadata)

    def _graph_key(self, ctx: RepoContext) -> str:
        root = Path(ctx.workspace_root).expanduser().resolve()
        generation = hashlib.sha256(
            json.dumps(
                index_generation_key(ctx.sqlite_store, ctx.registry_entry),
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        return f"{ctx.repo_id}:{root}:{generation}"

    @_semantic_operation
    def get_runtime_feature_status(self, ctx: RepoContext) -> Dict[str, Dict[str, Any]]:
        semantic_reason = None
        semantic_available = False
        if not self._semantic_enabled:
            semantic_reason = "semantic_search_disabled"
        elif self._get_semantic_indexer(ctx) is None:
            semantic_reason = "semantic_indexer_unavailable"
        else:
            semantic_available = True

        graph_state = self._graph_state.get(self._graph_key(ctx))
        graph_available = bool(graph_state and graph_state.analyzer and graph_state.selector)

        return {
            "lexical": {"status": "available" if ctx.sqlite_store else "unavailable"},
            "semantic": {
                "status": "available" if semantic_available else "unavailable",
                "reason": semantic_reason,
            },
            "graph": {
                "status": "available" if graph_available else "unavailable",
                "reason": None if graph_available else "graph_not_initialized",
            },
            "plugins": {
                "status": "available",
                "loaded": self._managed_plugin_count(ctx.repo_id),
            },
            "cross_repo": {
                "status": "available" if self._cross_repo_coordinator else "unavailable",
                "reason": None if self._cross_repo_coordinator else "cross_repo_disabled",
            },
        }

    def evict_repository_state(
        self, repo_id: str, repo_root: Optional[Union[str, Path]] = None
    ) -> Dict[str, int]:
        evicted = {"file_cache": 0, "graph": 0, "plugins": 0, "semantic": 0}
        root: Optional[Path] = Path(repo_root).expanduser().resolve() if repo_root else None
        with self._file_cache_lock:
            for key in list(self._file_cache.keys()):
                try:
                    path = Path(key).expanduser().resolve()
                    if root is not None and not path.is_relative_to(root):
                        continue
                except Exception:
                    if root is not None:
                        continue
                self._file_cache.pop(key, None)
                evicted["file_cache"] += 1
        for key in list(self._graph_state.keys()):
            if key.startswith(f"{repo_id}:"):
                self._graph_state.pop(key, None)
                evicted["graph"] += 1
        if hasattr(self._plugin_set_registry, "evict"):
            try:
                self._plugin_set_registry.evict(repo_id)
                evicted["plugins"] = 1
            except Exception as exc:
                logger.warning("Plugin eviction failed for %s: %s", repo_id, type(exc).__name__)
        if self._semantic_registry is not None and hasattr(self._semantic_registry, "evict"):
            evicted["semantic"] = 1 if self._semantic_registry.evict(repo_id) else 0
        return evicted

    @_semantic_operation
    def search(
        self,
        ctx: RepoContext,
        query: str,
        semantic: bool = False,
        fuzzy: bool = False,
        limit: int = 20,
        source_type: Optional[str] = None,
        friction_categories: Optional[List[str]] = None,
        history_labels: Optional[List[str]] = None,
        history_repos: Optional[List[str]] = None,
        include_source_metadata: bool = False,
    ) -> Iterable[SearchResult]:
        """Search for code and documentation scoped to ctx.repo_id."""
        _hp_t0 = time.perf_counter()
        start_time = time.time()
        sqlite_store = ctx.sqlite_store
        _semantic_indexer = self._get_semantic_indexer(ctx)
        strict_semantic_route = semantic and self._is_registered_context(ctx)
        repo_plugins = self._plugin_set_registry.plugins_for(ctx.repo_id)

        try:
            effective_source_type = source_type
            if effective_source_type is None:
                if history_labels or history_repos:
                    effective_source_type = "history"
                elif friction_categories:
                    effective_source_type = "friction"

            if sqlite_store and (
                effective_source_type or friction_categories or history_labels or history_repos
            ):
                filtered_results = sqlite_store.search_chunks_by_source_metadata(
                    query=query,
                    source_type=effective_source_type or "friction",
                    friction_categories=friction_categories,
                    history_labels=history_labels,
                    history_repos=history_repos,
                    limit=limit,
                )
                for item in filtered_results:
                    yield item
                self._operation_stats["searches"] += 1
                self._operation_stats["total_time"] += time.time() - start_time
                return

            # Fuzzy (trigram) path for misspelled queries
            if fuzzy and sqlite_store:
                logger.info("Using fuzzy trigram search (query_chars=%d)", len(query or ""))
                try:
                    sym_results = sqlite_store.search_symbols_fuzzy(query, limit)
                    file_results = sqlite_store.search_files_fuzzy(query, limit)

                    # Merge by file_path, keeping best score
                    merged: Dict[str, Dict] = {}
                    for r in sym_results:
                        fp = r.get("file_path", "")
                        if fp and (fp not in merged or r.get("score", 0) > merged[fp]["score"]):
                            merged[fp] = {
                                "file": fp,
                                "score": r.get("score", 0),
                                "line": r.get("line_start", 1),
                                "snippet": r.get("name", ""),
                                "language": r.get("language", "unknown"),
                            }
                    for r in file_results:
                        fp = r.get("file_path", "")
                        if fp and (fp not in merged or r.get("score", 0) > merged[fp]["score"]):
                            merged[fp] = {
                                "file": fp,
                                "score": r.get("score", 0),
                                "line": 1,
                                "snippet": "",
                                "language": "unknown",
                            }

                    sorted_results = sorted(merged.values(), key=lambda x: x["score"], reverse=True)
                    for item in sorted_results[:limit]:
                        yield item
                    self._operation_stats["searches"] += 1
                    self._operation_stats["total_time"] += time.time() - start_time
                    return
                except Exception as e:
                    logger.warning(f"Fuzzy search failed: {type(e).__name__}")

            # Prefer direct SQLite lexical search for non-semantic queries so the
            # server remains useful even when plugin in-memory indexes are cold.
            if sqlite_store and (
                not semantic or (_semantic_indexer is None and not strict_semantic_route)
            ):
                # Symbol routing: bypass BM25 for explicit symbol-pattern queries.
                intent, sym_name, kind_hint = classify_query_intent(query)
                if intent == QueryIntent.SYMBOL:
                    sym_results = self._symbol_route(sqlite_store, sym_name, kind_hint, limit)
                    if sym_results:
                        logger.info(
                            "Symbol route hit (query_chars=%d, results=%d)",
                            len(query or ""),
                            len(sym_results),
                        )
                        yield from sym_results
                        self._operation_stats["searches"] += 1
                        self._operation_stats["total_time"] += time.time() - start_time
                        return

                logger.info("Using direct lexical search (query_chars=%d)", len(query or ""))
                try:
                    tables_to_try = ["bm25_content", "fts_code"]

                    for table in tables_to_try:
                        try:
                            # Fetch an oversampled set so path-based penalties can
                            # surface results that BM25 ranked below the cutoff.
                            fetch_limit = max(limit * 8, 50)
                            results = sqlite_store.search_bm25(
                                query, table=table, limit=fetch_limit
                            )
                            if len(results) < fetch_limit:
                                or_query = " OR ".join(
                                    t for t in query.split() if re.match(r"[a-zA-Z0-9_]", t)
                                )
                                if or_query != query:
                                    or_results = sqlite_store.search_bm25(
                                        or_query, table=table, limit=fetch_limit
                                    )
                                    and_paths = {
                                        r.get("filepath") or r.get("file_path", "") for r in results
                                    }
                                    for r in or_results:
                                        fp = r.get("filepath") or r.get("file_path", "")
                                        if fp not in and_paths:
                                            results.append(r)
                            if results:
                                # Apply path-based score penalty and filename token
                                # boost, re-sort, truncate.
                                # FTS5 bm25() scores are negative; adding a positive
                                # penalty degrades rank for non-source paths; a
                                # negative boost from _filename_token_boost improves it.
                                scored = []
                                for result in results:
                                    file_path = result.get("filepath") or result.get(
                                        "file_path", ""
                                    )
                                    raw = result.get("score", 0.0)
                                    adjusted = (
                                        raw
                                        + _path_score_penalty(file_path)
                                        + _filename_token_boost(query, file_path)
                                    )
                                    scored.append((adjusted, file_path, result))
                                scored.sort(key=lambda t: t[0])
                                bm25_candidates = []
                                for adjusted, file_path, result in scored[:limit]:
                                    chunk = None
                                    file_id = result.get("file_id")
                                    if file_id is not None:
                                        try:
                                            chunk = sqlite_store.find_best_chunk_for_file(
                                                int(file_id), query.split()
                                            )
                                        except Exception as exc:
                                            record_handled_error(__name__, exc)
                                            chunk = None
                                    bm25_candidates.append(
                                        {
                                            "file": file_path,
                                            "line": (
                                                chunk["line_start"]
                                                if chunk
                                                else result.get("line", 1)
                                            ),
                                            "line_end": chunk["line_end"] if chunk else None,
                                            "symbol": chunk["symbol"] if chunk else None,
                                            "snippet": result.get("snippet", ""),
                                            "score": adjusted,
                                            "language": result.get("language", "unknown"),
                                        }
                                    )
                                bm25_candidates = self._apply_reranker(
                                    sqlite_store, query, bm25_candidates, limit
                                )
                                for item in bm25_candidates:
                                    if include_source_metadata:
                                        chunk_info = sqlite_store.find_chunk_at_line(
                                            item["file"],
                                            int(item.get("line", 1) or 1),
                                        )
                                        if chunk_info:
                                            source_metadata = extract_matching_source_metadata(
                                                chunk_info.get("metadata")
                                            )
                                            if source_metadata is not None:
                                                item = {**item, "source_metadata": source_metadata}
                                    yield {
                                        k: v
                                        for k, v in item.items()
                                        if k not in {"_rerank_doc", "_rerank_generation"}
                                    }
                                self._operation_stats["searches"] += 1
                                self._operation_stats["total_time"] += time.time() - start_time
                                return
                        except Exception as e:
                            logger.debug(
                                f"Lexical search in table '{table}' failed: {type(e).__name__}"
                            )
                            continue
                except Exception as e:
                    logger.warning(f"Direct lexical search failed: {type(e).__name__}")

            # Semantic queries go directly to the vector index — plugins explicitly
            # return [] for semantic=True, so bypassing them is correct.
            if strict_semantic_route and _semantic_indexer is None:
                raise SemanticSearchFailure(
                    "Semantic search requested for a registered repository, but the semantic indexer is unavailable."
                )

            if semantic and _semantic_indexer:
                logger.info(
                    "Using semantic indexer (query_chars=%d, limit=%d)",
                    len(query or ""),
                    limit,
                )
                try:
                    semantic_results = _semantic_indexer.search(query=query, limit=limit)
                    candidates = []
                    for result in semantic_results:
                        snippet = result.get("snippet", "")
                        if not snippet and "code" in result:
                            lines = result["code"].split("\n")
                            snippet = "\n".join(lines[:5])
                        file_value = (
                            result.get("relative_path")
                            or result.get("file")
                            or result.get("path")
                            or result.get("file_path")
                            or result.get("filepath")
                            or ""
                        )
                        raw_score = result.get("score", 0.0)
                        # Apply a mild penalty for artifact support paths in semantic results.
                        # The artifacts/ directory holds secondary routing/policy helpers, not
                        # primary implementations, so demote them relative to core code.
                        fp_lower = file_value.replace("\\", "/").lower()
                        if "/artifacts/" in fp_lower:
                            raw_score *= 0.85
                        candidates.append(
                            {
                                "file": file_value,
                                "line": result.get("line", 1),
                                "snippet": snippet,
                                "score": raw_score,
                                "language": result.get("metadata", {}).get("language", "unknown"),
                                "semantic_source": result.get("semantic_source", "semantic"),
                                "semantic_profile_id": result.get("semantic_profile_id"),
                                "semantic_collection_name": result.get("semantic_collection_name"),
                            }
                        )
                    # Re-sort by adjusted score so penalties take effect before reranking.
                    candidates.sort(key=lambda c: c["score"], reverse=True)
                    candidates = self._apply_reranker(
                        sqlite_store, query, candidates, limit, semantic_source=True
                    )
                    for item in candidates:
                        yield {
                            k: v
                            for k, v in item.items()
                            if k not in {"_rerank_doc", "_rerank_generation"}
                        }
                    self._operation_stats["searches"] += 1
                    self._operation_stats["total_time"] += time.time() - start_time
                    return
                except Exception as e:
                    if strict_semantic_route:
                        raise SemanticSearchFailure(
                            f"Semantic search failed for registered repository: {e}",
                            profile_id=getattr(
                                _semantic_indexer.semantic_profile, "profile_id", None
                            ),
                            collection_name=getattr(_semantic_indexer, "collection", None),
                        ) from e
                    logger.warning(
                        f"Semantic indexer search failed, falling back to plugins: {type(e).__name__}"
                    )

            # If still no plugins, try hybrid or BM25 search directly
            if len(repo_plugins) == 0 and sqlite_store:
                # Use semantic search if available and requested
                if semantic and _semantic_indexer:
                    logger.info("No plugins loaded, using semantic search")
                    try:
                        semantic_results = _semantic_indexer.search(query=query, limit=limit)
                        for result in semantic_results:
                            snippet = result.get("snippet", "")
                            if not snippet and "code" in result:
                                lines = result["code"].split("\n")
                                snippet = "\n".join(lines[:5])
                            file_value = (
                                result.get("relative_path")
                                or result.get("file")
                                or result.get("path")
                                or result.get("file_path")
                                or result.get("filepath")
                                or ""
                            )
                            yield {
                                "file": file_value,
                                "line": result.get("line", 1),
                                "snippet": snippet,
                                "score": result.get("score", 0.0),
                                "language": result.get("metadata", {}).get("language", "unknown"),
                                "semantic_source": result.get("semantic_source", "semantic"),
                                "semantic_profile_id": result.get("semantic_profile_id"),
                                "semantic_collection_name": result.get("semantic_collection_name"),
                            }
                        self._operation_stats["searches"] += 1
                        self._operation_stats["total_time"] += time.time() - start_time
                        return
                    except Exception as e:
                        if strict_semantic_route:
                            raise SemanticSearchFailure(
                                f"Semantic search failed for registered repository: {e}",
                                profile_id=getattr(
                                    _semantic_indexer.semantic_profile, "profile_id", None
                                ),
                                collection_name=getattr(_semantic_indexer, "collection", None),
                            ) from e
                        logger.error(f"Error in semantic search: {type(e).__name__}")
                        # Fall back to BM25

                # Fall back to BM25-only search
                logger.info("Using BM25 search directly")
                try:
                    import sqlite3

                    conn = sqlite3.connect(sqlite_store.db_path)
                    cursor = conn.cursor()

                    # Check if this is a BM25 index
                    cursor.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='bm25_content'"
                    )
                    if cursor.fetchone():
                        # Use BM25 search
                        cursor.execute(
                            """
                            SELECT 
                                filepath,
                                filename,
                                snippet(bm25_content, -1, '<<', '>>', '...', 20) as snippet,
                                language,
                                rank
                            FROM bm25_content
                            WHERE bm25_content MATCH ?
                            ORDER BY rank
                            LIMIT ?
                        """,
                            (query, limit),
                        )

                        for row in cursor.fetchall():
                            filepath, filename, snippet, language, rank = row
                            yield {
                                "file": filepath,
                                "line": 1,
                                "snippet": snippet,
                                "score": abs(rank),
                                "language": language or "unknown",
                            }

                        conn.close()
                        self._operation_stats["searches"] += 1
                        self._operation_stats["total_time"] += time.time() - start_time
                        return

                    conn.close()
                except Exception as e:
                    logger.error(f"Error in direct BM25 search: {type(e).__name__}")

            # Detect if this is a document query
            is_doc_query = self._is_document_query(query)

            # Expand query if it's a document query
            queries = [query]
            if is_doc_query:
                queries = self._expand_document_query(query)
                logger.info(
                    "Expanded document query (query_chars=%d) to %d variations",
                    len(query or ""),
                    len(queries),
                )
                # Force semantic search for natural language queries
                semantic = True

            if self._enable_advanced and self._aggregator:
                # Use advanced aggregation
                all_results_by_plugin = {}
                opts = {
                    "semantic": semantic,
                    "limit": limit * 2 if is_doc_query else limit,
                }

                # Search with all query variations
                for search_query in queries:
                    for plugin in repo_plugins:
                        try:
                            results = list(plugin.search(search_query, opts))
                            if results:
                                if plugin not in all_results_by_plugin:
                                    all_results_by_plugin[plugin] = []
                                all_results_by_plugin[plugin].extend(results)
                        except Exception as e:
                            logger.warning(
                                "Plugin %s search failed (%s)", plugin.lang, type(e).__name__
                            )

                # Deduplicate results per plugin (handle both dict results and SearchResult objects)
                for plugin, results in all_results_by_plugin.items():
                    seen = set()
                    unique_results = []
                    for result in results:
                        if hasattr(result, "path"):
                            key = f"{result.path}:{getattr(result, 'line', 0)}"
                        else:
                            key = f"{result.get('file', result.get('path', ''))}:{result.get('line', 0)}"
                        if key not in seen:
                            seen.add(key)
                            unique_results.append(result)
                    all_results_by_plugin[plugin] = unique_results

                # Determine if we have plugin SearchResult dicts (have "path"/"name", not "file")
                # vs BM25 dict results (have "file" key). Plugin results bypass the dict-only aggregator.
                first_result = next(
                    (r for results in all_results_by_plugin.values() for r in results), None
                )
                is_search_result_objects = first_result is not None and (
                    hasattr(first_result, "path")  # dataclass
                    or (
                        isinstance(first_result, dict)
                        and "path" in first_result
                        and "file" not in first_result
                    )
                )

                if is_search_result_objects:
                    # Plugin SearchResult objects: flatten and sort by score, bypassing dict-only aggregator
                    all_search_results = []
                    for results in all_results_by_plugin.values():
                        all_search_results.extend(results)
                    all_search_results.sort(key=lambda r: getattr(r, "score", 0), reverse=True)
                    self._operation_stats["searches"] += 1
                    self._operation_stats["total_time"] += time.time() - start_time
                    for r in all_search_results[:limit]:
                        yield r
                else:
                    # BM25/dict results: use full aggregation pipeline
                    # Configure aggregator for document queries
                    if is_doc_query and self._enable_advanced:
                        doc_criteria = RankingCriteria(
                            relevance_weight=0.5,
                            confidence_weight=0.2,
                            frequency_weight=0.2,
                            recency_weight=0.1,
                            prefer_exact_matches=False,
                            boost_multiple_sources=True,
                            boost_common_extensions=True,
                        )
                        self._aggregator.configure(ranking_criteria=doc_criteria)

                    aggregated_results, stats = self._aggregator.aggregate_search_results(
                        all_results_by_plugin, limit=limit * 2 if is_doc_query else limit
                    )

                    # Adjust ranking for document queries
                    if is_doc_query:
                        aggregated_results = self._adjust_ranking_for_documents(
                            query, aggregated_results
                        )

                    # Apply final limit
                    if limit and len(aggregated_results) > limit:
                        aggregated_results = aggregated_results[:limit]

                    logger.debug(
                        f"Search aggregation stats: {stats.total_results} total, "
                        f"{stats.unique_results} unique, {stats.plugins_used} plugins used, "
                        f"document_query={is_doc_query}"
                    )

                    self._operation_stats["searches"] += 1
                    self._operation_stats["total_time"] += time.time() - start_time

                    # Yield primary results from aggregated results
                    for aggregated in aggregated_results:
                        yield aggregated.primary_result
            else:
                # Fallback to basic search
                # Detect if this is a document query
                is_doc_query = self._is_document_query(query)

                # Expand query if it's a document query
                queries = [query]
                if is_doc_query:
                    queries = self._expand_document_query(query)
                    semantic = True  # Force semantic search for natural language

                opts = {"semantic": semantic, "limit": limit}
                all_results = []

                # Search with all query variations
                for search_query in queries:
                    for p in repo_plugins:
                        try:
                            for result in p.search(search_query, opts):
                                all_results.append(result)
                        except Exception as e:
                            logger.warning("Plugin %s search failed (%s)", p.lang, type(e).__name__)

                # Attempt gated symbol-definition fallback for the query as a symbol name.
                # source_ext is derived from the first BM25 filepath hit, or None.
                _search_row_filepath: Optional[str] = None
                if sqlite_store:
                    try:
                        import sqlite3 as _sqlite3_s

                        _sconn = _sqlite3_s.connect(sqlite_store.db_path)
                        try:
                            _scur = _sconn.cursor()
                            _scur.execute(
                                "SELECT filepath FROM bm25_content WHERE bm25_content MATCH ? LIMIT 1",
                                (query,),
                            )
                            _shint = _scur.fetchone()
                            if _shint:
                                _search_row_filepath = _shint[0]
                        except Exception as exc:
                            record_handled_error(__name__, exc)
                            pass
                        finally:
                            _sconn.close()
                    except Exception as exc:
                        record_handled_error(__name__, exc)
                        pass
                _search_source_ext = (
                    os.path.splitext(_search_row_filepath)[1].lower()
                    if _search_row_filepath
                    else None
                )
                _sym_def = run_gated_fallback(
                    plugins=repo_plugins,
                    symbol=query,
                    source_ext=_search_source_ext,
                    timeout_ms=self._fallback_timeout_ms,
                    histogram=self._fallback_histogram,
                )
                if _sym_def is not None:
                    _sym_filepath = (
                        _sym_def.get("defined_in", "") if isinstance(_sym_def, dict) else ""
                    )
                    all_results.append(
                        {
                            "file": _sym_filepath,
                            "line": (_sym_def.get("line", 1) if isinstance(_sym_def, dict) else 1),
                            "snippet": (
                                _sym_def.get("signature", "") if isinstance(_sym_def, dict) else ""
                            ),
                            "score": 1.0,
                            "language": (
                                _sym_def.get("language", "unknown")
                                if isinstance(_sym_def, dict)
                                else "unknown"
                            ),
                        }
                    )

                # Deduplicate results
                seen = set()
                unique_results = []
                for result in all_results:
                    key = f"{result['file']}:{result['line']}"
                    if key not in seen:
                        seen.add(key)
                        unique_results.append(result)

                # Sort by score if available
                unique_results.sort(key=lambda r: r.get("score", 0.5) or 0.5, reverse=True)

                # Prioritize documentation files for document queries
                if is_doc_query:
                    doc_results = []
                    code_results = []
                    for result in unique_results:
                        if self._is_documentation_file(result.get("file", "")):
                            doc_results.append(result)
                        else:
                            code_results.append(result)
                    unique_results = doc_results + code_results

                # Apply limit
                count = 0
                for result in unique_results:
                    if limit and count >= limit:
                        break
                    yield result
                    count += 1

                self._operation_stats["searches"] += 1
                self._operation_stats["total_time"] += time.time() - start_time

        except SemanticSearchFailure:
            raise
        except Exception as e:
            logger.error(
                "Search failed (query_chars=%d, error_type=%s)", len(query or ""), type(e).__name__
            )
        finally:
            try:
                from mcp_server.metrics.prometheus_exporter import get_prometheus_exporter

                _hist = get_prometheus_exporter().dispatcher_search_histogram
                if _hist is not None:
                    _hist.observe(time.perf_counter() - _hp_t0)
            except Exception as exc:
                record_handled_error(__name__, exc)
                pass

    @_semantic_operation
    def index_file(self, ctx: RepoContext, path: Path, do_semantic: bool = True) -> IndexResult:
        """Index a single file if it has changed."""
        path = self._input_path(ctx, path)
        if build_walker_filter(ctx.workspace_root)(path):
            return IndexResult(
                IndexResultStatus.ERROR, path, None, None, "Path excluded by repository policy"
            )
        path = path.resolve()

        if not path.exists():
            return IndexResult(
                status=IndexResultStatus.NOT_FOUND,
                path=path,
                observed_hash=None,
                actual_hash=None,
                error="file not found",
            )

        try:
            # Read file content
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # Try with different encodings
                try:
                    content = path.read_text(encoding="latin-1")
                except Exception as e:
                    logger.error(f"Failed to read {path}: {type(e).__name__}")
                    return IndexResult(
                        status=IndexResultStatus.ERROR,
                        path=path,
                        observed_hash=None,
                        actual_hash=None,
                        error=str(e),
                    )

            # Skip if file hasn't changed since last index
            if not self._should_reindex(path, content, ctx.sqlite_store):
                logger.debug(f"Skipping {path} (unchanged)")
                return IndexResult(
                    status=IndexResultStatus.SKIPPED_UNCHANGED,
                    path=path,
                    observed_hash=None,
                    actual_hash=None,
                )

            bounded_python_shard = self._build_exact_bounded_python_shard(
                path, content, ctx.workspace_root
            )
            bounded_shell_shard = self._build_exact_bounded_shell_shard(
                path, content, ctx.workspace_root
            )
            bounded_json_shard = self._build_exact_bounded_json_shard(
                path, content, ctx.workspace_root
            )
            bounded_jsonl_shard = self._build_exact_bounded_jsonl_shard(
                path, content, ctx.workspace_root
            )
            if bounded_python_shard is not None:
                plugin_language = "python"
                plugin_lang = "python"
                shard = bounded_python_shard
            elif bounded_shell_shard is not None:
                plugin_language = "plaintext"
                plugin_lang = "plaintext"
                shard = bounded_shell_shard
            elif bounded_json_shard is not None:
                plugin_language = "json"
                plugin_lang = "json"
                shard = bounded_json_shard
            elif bounded_jsonl_shard is not None:
                plugin_language = "plaintext"
                plugin_lang = "plaintext"
                shard = bounded_jsonl_shard
            else:
                # Find the appropriate plugin
                plugin = self._match_plugin(ctx, path)
                plugin_language = plugin.language
                plugin_lang = plugin.lang

            # Index the file
            start_time = time.time()
            logger.info(f"Indexing {path} with {plugin_lang} plugin")
            if (
                bounded_python_shard is None
                and bounded_shell_shard is None
                and bounded_json_shard is None
                and bounded_jsonl_shard is None
            ):
                shard = plugin.indexFile(path, content)
            try:
                self._persist_index_shard(ctx, path, content, plugin_language, shard)
            except Exception as e:
                logger.error(f"Failed to persist index shard for {path}: {type(e).__name__}")
                return IndexResult(
                    status=IndexResultStatus.ERROR,
                    path=path,
                    observed_hash=None,
                    actual_hash=None,
                    error=f"failed to persist index shard: {e}",
                )

            # Update file cache after successful indexing
            try:
                stat = path.stat()
                with self._file_cache_lock:
                    self._file_cache[str(path)] = (
                        stat.st_mtime,
                        stat.st_size,
                        self._get_file_hash(content),
                    )
            except OSError:
                pass

            # Record performance if advanced features enabled
            if (
                self._enable_advanced
                and self._router
                and bounded_python_shard is None
                and bounded_shell_shard is None
                and bounded_json_shard is None
                and bounded_jsonl_shard is None
            ):
                execution_time = time.time() - start_time
                self._router.record_performance(plugin, execution_time)

            self._operation_stats["indexings"] += 1
            self._operation_stats["total_time"] += time.time() - start_time

            logger.info(
                f"Successfully indexed {path}: {len(shard.get('symbols', []))} symbols found"
            )

            # Semantic indexing for incremental single-file updates
            semantic_stats = None
            _sem = self._get_semantic_indexer(ctx)
            if do_semantic and _sem:
                try:
                    semantic_stats = self.rebuild_semantic_for_paths(ctx, [path])
                except Exception as e:
                    logger.warning(f"Semantic indexing failed for {path}: {type(e).__name__}")
                    raise RuntimeError("Semantic indexing did not complete") from e

            return IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
                semantic=semantic_stats,
            )

        except RuntimeError as e:
            # No plugin found for this file type
            logger.debug(f"No plugin for {path}: {type(e).__name__}")
            return IndexResult(
                status=IndexResultStatus.ERROR,
                path=path,
                observed_hash=None,
                actual_hash=None,
                error=str(e),
            )
        except Exception as e:
            logger.error(f"Error indexing {path}: {type(e).__name__}")
            return IndexResult(
                status=IndexResultStatus.ERROR,
                path=path,
                observed_hash=None,
                actual_hash=None,
                error=str(e),
            )

    def _build_exact_bounded_json_shard(
        self, path: Path, content: str, workspace_root: Path
    ) -> Optional[Dict[str, Any]]:
        if path.suffix.lower() != ".json":
            return None
        bounded_path_reason = GenericTreeSitterPlugin.exact_bounded_json_reason(
            path, workspace_root
        )
        if bounded_path_reason is None:
            return None
        return {
            "symbols": [],
            "chunks": [],
            "metadata": {
                "bounded_chunk_path": True,
                "bounded_path_reason": bounded_path_reason,
            },
        }

    def _build_exact_bounded_jsonl_shard(
        self, path: Path, content: str, workspace_root: Path
    ) -> Optional[Dict[str, Any]]:
        if path.suffix.lower() != ".jsonl":
            return None
        bounded_path_reason = GenericTreeSitterPlugin.exact_bounded_jsonl_reason(
            path, workspace_root
        )
        if bounded_path_reason is None:
            return None
        return {
            "symbols": [],
            "chunks": [],
            "metadata": {
                "bounded_chunk_path": True,
                "bounded_path_reason": bounded_path_reason,
            },
        }

    def _build_exact_bounded_python_shard(
        self, path: Path, content: str, workspace_root: Path
    ) -> Optional[Dict[str, Any]]:
        if path.suffix.lower() != ".py":
            return None
        try:
            relative_path = path.resolve().relative_to(workspace_root.resolve()).as_posix()
        except ValueError:
            return None
        bounded_path_reason = _EXACT_BOUNDED_PYTHON_PATHS.get(relative_path)
        if bounded_path_reason is None:
            return None

        symbols = []
        try:
            tree = ast.parse(content)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(
                        {
                            "symbol": node.name,
                            "kind": "function",
                            "signature": f"def {node.name}(...):",
                            "line": node.lineno,
                            "span": (node.lineno, getattr(node, "end_lineno", node.lineno)),
                        }
                    )
                    continue
                if not isinstance(node, ast.ClassDef):
                    continue
                symbols.append(
                    {
                        "symbol": node.name,
                        "kind": "class",
                        "signature": f"class {node.name}:",
                        "line": node.lineno,
                        "span": (node.lineno, getattr(node, "end_lineno", node.lineno)),
                    }
                )
                for child in node.body:
                    if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    symbols.append(
                        {
                            "symbol": child.name,
                            "kind": "method",
                            "signature": f"def {child.name}(...):",
                            "line": child.lineno,
                            "span": (child.lineno, getattr(child, "end_lineno", child.lineno)),
                        }
                    )

        return {
            "symbols": symbols,
            "chunks": [],
            "metadata": {
                "bounded_chunk_path": True,
                "bounded_path_reason": bounded_path_reason,
            },
        }

    def _build_exact_bounded_shell_shard(
        self, path: Path, content: str, workspace_root: Path
    ) -> Optional[Dict[str, Any]]:
        if path.suffix.lower() not in {".sh", ".bash", ".zsh"}:
            return None
        try:
            relative_path = path.resolve().relative_to(workspace_root.resolve()).as_posix()
        except ValueError:
            return None
        bounded_path_reason = _EXACT_BOUNDED_SHELL_PATHS.get(relative_path)
        if bounded_path_reason is None:
            return None

        symbols = []
        for line_number, raw_line in enumerate(content.splitlines(), start=1):
            line = raw_line.strip()
            if "preflight_env" not in line:
                continue
            symbols.append(
                {
                    "symbol": "preflight_env",
                    "kind": "command",
                    "signature": raw_line.strip(),
                    "line": line_number,
                    "span": (line_number, line_number),
                }
            )
            break

        return {
            "symbols": symbols,
            "chunks": [],
            "metadata": {
                "bounded_chunk_path": True,
                "bounded_path_reason": bounded_path_reason,
            },
        }

    @_semantic_operation
    def index_file_guarded(self, ctx: RepoContext, path: Path, expected_hash: str) -> IndexResult:
        """TOCTOU-guarded index: re-hashes immediately before plugin write.

        If the file hash at dispatch time differs from expected_hash, the file
        was modified between watcher observation and dispatch; skip indexing.
        """
        path = self._input_path(ctx, path)
        if build_walker_filter(ctx.workspace_root)(path):
            return IndexResult(
                IndexResultStatus.ERROR,
                path,
                expected_hash,
                None,
                "Path excluded by repository policy",
            )
        path = path.resolve()

        if not path.exists():
            return IndexResult(
                status=IndexResultStatus.ERROR,
                path=path,
                observed_hash=expected_hash,
                actual_hash=None,
                error="file not found",
            )

        try:
            actual_hash = self._hash_file_bytes(path)
        except OSError as e:
            return IndexResult(
                status=IndexResultStatus.ERROR,
                path=path,
                observed_hash=expected_hash,
                actual_hash=None,
                error=str(e),
            )

        if actual_hash != expected_hash:
            logger.warning(
                "dispatcher.index.toctou_skipped: file drifted before dispatch",
                extra={"path": str(path), "observed": expected_hash, "actual": actual_hash},
            )
            return IndexResult(
                status=IndexResultStatus.SKIPPED_TOCTOU,
                path=path,
                observed_hash=expected_hash,
                actual_hash=actual_hash,
            )

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                content = path.read_text(encoding="latin-1")
            except Exception as e:
                return IndexResult(
                    status=IndexResultStatus.ERROR,
                    path=path,
                    observed_hash=expected_hash,
                    actual_hash=actual_hash,
                    error=str(e),
                )

        if not self._should_reindex(path, content, ctx.sqlite_store):
            return IndexResult(
                status=IndexResultStatus.SKIPPED_UNCHANGED,
                path=path,
                observed_hash=expected_hash,
                actual_hash=actual_hash,
            )

        try:
            plugin = self._match_plugin(ctx, path)
        except RuntimeError as e:
            return IndexResult(
                status=IndexResultStatus.ERROR,
                path=path,
                observed_hash=expected_hash,
                actual_hash=actual_hash,
                error=str(e),
            )

        try:
            start_time = time.time()
            logger.info(f"Indexing {path} with {plugin.lang} plugin (guarded)")
            shard = plugin.indexFile(path, content)
            try:
                self._persist_index_shard(ctx, path, content, plugin.language, shard)
            except Exception as e:
                return IndexResult(
                    status=IndexResultStatus.ERROR,
                    path=path,
                    observed_hash=expected_hash,
                    actual_hash=actual_hash,
                    error=f"failed to persist index shard: {e}",
                )

            try:
                stat = path.stat()
                with self._file_cache_lock:
                    self._file_cache[str(path)] = (
                        stat.st_mtime,
                        stat.st_size,
                        self._get_file_hash(content),
                    )
            except OSError:
                pass

            if self._enable_advanced and self._router:
                self._router.record_performance(plugin, time.time() - start_time)

            self._operation_stats["indexings"] += 1
            self._operation_stats["total_time"] += time.time() - start_time

            semantic_stats = None
            _sem = self._get_semantic_indexer(ctx)
            if _sem:
                try:
                    semantic_stats = self.rebuild_semantic_for_paths(ctx, [path])
                except Exception as e:
                    logger.warning(f"Semantic indexing failed for {path}: {type(e).__name__}")
                    raise RuntimeError("Semantic indexing did not complete") from e

            return IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=expected_hash,
                actual_hash=actual_hash,
                semantic=semantic_stats,
            )
        except Exception as e:
            logger.error(f"Error in guarded indexing of {path}: {type(e).__name__}")
            return IndexResult(
                status=IndexResultStatus.ERROR,
                path=path,
                observed_hash=expected_hash,
                actual_hash=actual_hash,
                error=str(e),
            )

    def get_semantic_summary_contract(self, ctx: RepoContext) -> Optional[Dict[str, Any]]:
        """Return the active summary fingerprint contract for strict semantic rebuilds."""
        active_store = getattr(ctx, "sqlite_store", None)
        if active_store is None:
            return None

        from ..indexing.summarization import ComprehensiveChunkWriter

        settings = reload_settings()
        summarization_config = settings.get_profile_summarization_config(
            settings.semantic_default_profile
        )
        summarization_config.setdefault("profile_id", settings.semantic_default_profile)
        writer = ComprehensiveChunkWriter(
            db_path=active_store.db_path,
            qdrant_client=None,
            summarization_config=summarization_config,
        )
        return {
            "profile_id": summarization_config.get("profile_id"),
            "prompt_fingerprint": writer._prompt_fingerprint(),
        }

    @_semantic_operation
    def rebuild_semantic_for_paths(
        self,
        ctx: RepoContext,
        paths: List[Path],
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Run the summary-first strict semantic pipeline for selected files."""
        stats = {
            "summaries_written": 0,
            "summary_chunks_attempted": 0,
            "summary_missing_chunks": 0,
            "summary_passes": 0,
            "summary_remaining_chunks": 0,
            "summary_scope_drained": True,
            "summary_continuation_required": False,
            "summary_call_timed_out": False,
            "summary_call_file_path": None,
            "summary_call_chunk_ids": [],
            "summary_call_timeout_seconds": None,
            "semantic_indexed": 0,
            "semantic_failed": 0,
            "semantic_skipped": 0,
            "semantic_blocked": 0,
            "semantic_stage": "not_run",
        }
        normalized_paths = [Path(path).resolve() for path in paths if Path(path).exists()]
        if not normalized_paths:
            stats["semantic_stage"] = "skipped"
            return stats

        def emit_progress(stage: str, stage_family: str, blocker_source: str) -> None:
            if progress_callback is None:
                return
            progress_callback(
                {
                    "stage": stage,
                    "stage_family": stage_family,
                    "blocker_source": blocker_source,
                    "semantic_stage": stats.get("semantic_stage"),
                    "last_progress_path": None,
                    "in_flight_path": None,
                    "summary_call_timed_out": stats.get("summary_call_timed_out", False),
                    "summary_call_file_path": stats.get("summary_call_file_path"),
                    "summary_call_chunk_ids": list(stats.get("summary_call_chunk_ids", []) or []),
                    "summary_call_timeout_seconds": stats.get("summary_call_timeout_seconds"),
                }
            )

        _sem = self._get_semantic_indexer(ctx)
        active_store = getattr(ctx, "sqlite_store", None)
        if _sem is None or active_store is None:
            stats["semantic_stage"] = "skipped"
            stats["semantic_skipped"] = len(normalized_paths)
            return stats

        from ..indexing.summarization import ComprehensiveChunkWriter
        from ..setup.semantic_preflight import (
            bootstrap_active_profile_collection,
            run_semantic_preflight,
            summarize_collection_bootstrap,
        )

        settings = reload_settings()
        summarization_config = settings.get_profile_summarization_config(
            settings.semantic_default_profile
        )
        summarization_config.setdefault("profile_id", settings.semantic_default_profile)
        writer = ComprehensiveChunkWriter(
            db_path=active_store.db_path,
            qdrant_client=None,
            summarization_config=summarization_config,
        )
        semantic_stage_timeout_seconds = max(60, settings.semantic_preflight_timeout_seconds * 12)
        # Keep each summary pass bounded enough to make durable progress inside
        # the per-pass timeout instead of attempting the entire repo backlog at once.
        summary_limit = min(512, max(64, len(normalized_paths)))
        min_summary_limit = 1
        max_summary_passes = _REPO_SCOPE_SUMMARY_PASS_BUDGET if len(normalized_paths) > 1 else None
        remaining_missing = self._count_missing_summaries_for_paths(ctx, normalized_paths)
        summary_missing_ids: List[str] = []
        previous_missing = remaining_missing
        plateau_passes = 0
        if remaining_missing > 0:
            emit_progress(
                "summary_shutdown_started",
                "summary_shutdown",
                "summary_call_shutdown",
            )

        while remaining_missing > 0:
            if cancel_check is not None and cancel_check():
                stats["summary_missing_chunks"] = remaining_missing
                stats["semantic_stage"] = "cancelled"
                stats["semantic_blocked"] = len(normalized_paths)
                stats["cancelled"] = True
                emit_progress("cancelled", "semantic_closeout", "summary_call_shutdown")
                return stats
            missing_before_pass = remaining_missing
            try:
                summary_result = _run_coro_blocking(
                    asyncio.wait_for(
                        writer.process_scope(
                            limit=summary_limit,
                            target_paths=normalized_paths,
                            max_batches=1,
                            cancel_check=cancel_check,
                        ),
                        timeout=semantic_stage_timeout_seconds,
                    )
                )
            except TimeoutError:
                refreshed_missing = self._count_missing_summaries_for_paths(ctx, normalized_paths)
                if refreshed_missing < remaining_missing:
                    remaining_missing = refreshed_missing
                    previous_missing = refreshed_missing
                    plateau_passes = 0
                if summary_limit > min_summary_limit:
                    summary_limit = max(min_summary_limit, summary_limit // 2)
                    continue
                stats["summary_missing_chunks"] = remaining_missing
                stats["semantic_stage"] = "blocked_summary_timeout"
                stats["semantic_blocked"] = len(normalized_paths)
                stats["semantic_error"] = (
                    "Summary generation timed out before strict semantic indexing could start"
                )
                if summary_missing_ids:
                    stats["summary_missing_chunk_ids"] = sorted(set(summary_missing_ids))
                emit_progress(
                    "blocked_summary_timeout",
                    "semantic_closeout",
                    "summary_call_shutdown",
                )
                return stats
            if getattr(summary_result, "blocked_call_reason", None) == "timeout":
                summary_missing_ids.extend(summary_result.missing_chunk_ids)
                reported_remaining = getattr(summary_result, "remaining_chunks", None)
                if isinstance(reported_remaining, int):
                    remaining_missing = reported_remaining
                else:
                    remaining_missing = self._count_missing_summaries_for_paths(
                        ctx, normalized_paths
                    )
                stats["summary_missing_chunks"] = remaining_missing
                stats["summary_remaining_chunks"] = remaining_missing
                stats["summary_scope_drained"] = False
                stats["summary_call_timed_out"] = True
                stats["summary_call_file_path"] = getattr(
                    summary_result, "blocked_call_file_path", None
                )
                stats["summary_call_chunk_ids"] = list(
                    getattr(summary_result, "blocked_call_chunk_ids", []) or []
                )
                stats["summary_call_timeout_seconds"] = getattr(
                    summary_result, "blocked_call_timeout_seconds", None
                )
                stats["semantic_stage"] = "blocked_summary_call_timeout"
                stats["semantic_blocked"] = len(normalized_paths)
                timeout_seconds = getattr(summary_result, "blocked_call_timeout_seconds", None)
                timeout_detail = (
                    f" after {timeout_seconds:.0f} seconds"
                    if isinstance(timeout_seconds, (int, float))
                    else ""
                )
                timed_out_path = getattr(summary_result, "blocked_call_file_path", None)
                stats["semantic_error"] = (
                    "Authoritative summary call timed out"
                    f"{timeout_detail} before any summary was written for "
                    f"{timed_out_path or 'unknown file'}; "
                    f"{remaining_missing} chunks still require summaries"
                )
                if summary_missing_ids:
                    stats["summary_missing_chunk_ids"] = sorted(set(summary_missing_ids))
                emit_progress(
                    "blocked_summary_call_timeout",
                    "semantic_closeout",
                    "summary_call_shutdown",
                )
                return stats
            if getattr(summary_result, "cancelled", False):
                stats["summary_missing_chunks"] = self._count_missing_summaries_for_paths(
                    ctx, normalized_paths
                )
                stats["semantic_stage"] = "cancelled"
                stats["semantic_blocked"] = len(normalized_paths)
                stats["cancelled"] = True
                emit_progress("cancelled", "semantic_closeout", "summary_call_shutdown")
                return stats
            stats["summary_passes"] += 1
            stats["summaries_written"] += summary_result.summaries_written
            stats["summary_chunks_attempted"] += summary_result.chunks_attempted
            summary_missing_ids.extend(summary_result.missing_chunk_ids)
            reported_remaining = getattr(summary_result, "remaining_chunks", None)
            if isinstance(reported_remaining, int):
                remaining_missing = reported_remaining
            else:
                remaining_missing = self._count_missing_summaries_for_paths(ctx, normalized_paths)
            stats["summary_remaining_chunks"] = remaining_missing
            stats["summary_scope_drained"] = remaining_missing == 0
            if summary_result.summaries_written > 0 and remaining_missing > 0:
                if remaining_missing < previous_missing:
                    plateau_passes = 0
                else:
                    plateau_passes += 1
                    if plateau_passes >= 2:
                        stats["summary_missing_chunks"] = remaining_missing
                        stats["semantic_stage"] = "blocked_summary_plateau"
                        stats["semantic_blocked"] = len(normalized_paths)
                        stats["semantic_error"] = (
                            "Summary generation plateaued before strict semantic indexing could start"
                        )
                        if summary_missing_ids:
                            stats["summary_missing_chunk_ids"] = sorted(set(summary_missing_ids))
                        emit_progress(
                            "blocked_summary_plateau",
                            "semantic_closeout",
                            "semantic_closeout",
                        )
                        return stats
                if max_summary_passes is not None and stats["summary_passes"] >= max_summary_passes:
                    stats["summary_missing_chunks"] = remaining_missing
                    stats["summary_continuation_required"] = True
                    stats["semantic_stage"] = "blocked_missing_summaries"
                    stats["semantic_blocked"] = len(normalized_paths)
                    stats["semantic_error"] = (
                        "Missing authoritative summaries blocked strict semantic indexing "
                        f"after {stats['summary_passes']} bounded summary passes; "
                        f"{remaining_missing} chunks still require summaries"
                    )
                    if summary_missing_ids:
                        stats["summary_missing_chunk_ids"] = sorted(set(summary_missing_ids))
                    emit_progress(
                        "blocked_missing_summaries",
                        "semantic_closeout",
                        "semantic_closeout",
                    )
                    return stats
            if summary_result.summaries_written == 0:
                if remaining_missing >= missing_before_pass and summary_limit > min_summary_limit:
                    summary_limit = max(min_summary_limit, summary_limit // 2)
                    continue
                break
            previous_missing = remaining_missing

        stats["summary_missing_chunks"] = remaining_missing
        stats["summary_remaining_chunks"] = remaining_missing
        stats["summary_scope_drained"] = remaining_missing == 0
        if remaining_missing > 0:
            stats["semantic_stage"] = "blocked_missing_summaries"
            stats["semantic_blocked"] = len(normalized_paths)
            stats["semantic_error"] = (
                "Missing authoritative summaries blocked strict semantic indexing"
            )
            if summary_missing_ids:
                stats["summary_missing_chunk_ids"] = sorted(set(summary_missing_ids))
            emit_progress(
                "blocked_missing_summaries",
                "semantic_closeout",
                "semantic_closeout",
            )
            return stats

        semantic_preflight = run_semantic_preflight(
            settings=settings,
            strict=False,
        ).to_dict()
        collection_bootstrap = summarize_collection_bootstrap(semantic_preflight)
        if not semantic_preflight.get("can_write_semantic_vectors", True):
            blocker = semantic_preflight.get("blocker") or {}
            if blocker.get("code") == "collection_missing":
                collection_bootstrap = bootstrap_active_profile_collection(
                    settings=settings,
                    profile=settings.semantic_default_profile,
                ).to_dict()
                stats["semantic_collection_bootstrap"] = collection_bootstrap
                semantic_preflight = run_semantic_preflight(
                    settings=settings,
                    strict=False,
                ).to_dict()
            else:
                stats["semantic_collection_bootstrap"] = collection_bootstrap.to_dict()
        else:
            stats["semantic_collection_bootstrap"] = collection_bootstrap.to_dict()

        if not semantic_preflight.get("can_write_semantic_vectors", True):
            stats["semantic_stage"] = "blocked_preflight"
            stats["semantic_blocked"] = len(normalized_paths)
            stats["semantic_blocker"] = semantic_preflight.get("blocker")
            stats["semantic_error"] = (semantic_preflight.get("blocker") or {}).get("message")
            emit_progress(
                "blocked_preflight",
                "semantic_closeout",
                "semantic_closeout",
            )
            return stats
        if cancel_check is not None and cancel_check():
            stats["semantic_stage"] = "cancelled"
            stats["semantic_blocked"] = len(normalized_paths)
            stats["cancelled"] = True
            emit_progress("cancelled", "semantic_closeout", "semantic_closeout")
            return stats

        try:
            sem_stats = _run_blocking_with_timeout(
                lambda: _sem.index_files_batch(
                    normalized_paths,
                    embed_batch_size=1000,
                    require_summaries=True,
                    semantic_preflight=semantic_preflight,
                ),
                timeout_seconds=semantic_stage_timeout_seconds,
            )
        except TimeoutError:
            stats["semantic_stage"] = "blocked_semantic_batch_timeout"
            stats["semantic_blocked"] = len(normalized_paths)
            stats["semantic_error"] = (
                "Semantic batch writes timed out after summaries were generated"
            )
            emit_progress(
                "blocked_semantic_batch_timeout",
                "semantic_closeout",
                "semantic_closeout",
            )
            return stats
        stats["semantic_indexed"] = sem_stats.get("files_indexed", 0)
        stats["semantic_failed"] = sem_stats.get("files_failed", 0)
        stats["semantic_skipped"] = sem_stats.get("files_skipped", 0)
        stats["semantic_blocked"] = sem_stats.get("files_blocked", 0)
        stats["total_embedding_units"] = sem_stats.get("total_embedding_units", 0)
        if sem_stats.get("semantic_blocker") is not None:
            stats["semantic_blocker"] = sem_stats.get("semantic_blocker")
        if sem_stats.get("semantic_error"):
            stats["semantic_error"] = sem_stats.get("semantic_error")
        if stats["semantic_indexed"] > 0:
            stats["semantic_stage"] = "indexed"
        elif stats["semantic_blocked"] > 0:
            stats["semantic_stage"] = "blocked_semantic_batch"
            if "semantic_error" not in stats:
                stats["semantic_error"] = (
                    "Semantic batch writes were blocked after summaries were generated"
                )
        elif stats["semantic_failed"] > 0:
            stats["semantic_stage"] = "failed_semantic_batch"
        else:
            stats["semantic_stage"] = "skipped"
        emit_progress(stats["semantic_stage"], "semantic_closeout", "semantic_closeout")
        return stats

    def _count_missing_summaries_for_paths(self, ctx: RepoContext, paths: List[Path]) -> int:
        active_store = getattr(ctx, "sqlite_store", None)
        if active_store is None or not paths:
            return 0

        normalized_paths = sorted(Path(path).resolve(strict=False).as_posix() for path in paths)
        placeholders = ", ".join("?" for _ in normalized_paths)
        with sqlite3.connect(active_store.db_path) as conn:
            # CHUNKERSAFE Lane A: fail closed before a scheme-dependent code_chunks
            # read over an incompatible/rebuilding index.
            assert_chunk_scheme_readable(conn)
            row = conn.execute(
                f"""SELECT COUNT(*)
                    FROM code_chunks c
                    JOIN files f ON c.file_id = f.id
                    LEFT JOIN chunk_summaries cs ON c.chunk_id = cs.chunk_hash
                    WHERE cs.chunk_hash IS NULL
                      AND f.path IN ({placeholders})""",
                tuple(normalized_paths),
            ).fetchone()
        return int(row[0]) if row else 0

    def _persist_index_shard(
        self,
        ctx: RepoContext,
        path: Path,
        content: str,
        language: str,
        shard: Dict[str, Any],
    ) -> None:
        """Persist a plugin-returned shard into the host SQLite store.

        Sandboxed plugins cannot receive SQLite capabilities, so the dispatcher
        owns durable writes from their returned shard.
        """
        sqlite_store = ctx.sqlite_store
        if not isinstance(sqlite_store, SQLiteStore):
            return

        repo_path = Path(
            getattr(ctx.registry_entry, "path", ctx.workspace_root) or ctx.workspace_root
        )
        repo_name = getattr(ctx.registry_entry, "name", None) or repo_path.name
        repository_row = sqlite_store.ensure_repository_row(repo_path, name=repo_name)
        try:
            relative_path = str(path.relative_to(Path(ctx.workspace_root))).replace("\\", "/")
        except ValueError:
            relative_path = sqlite_store.path_resolver.normalize_path(path)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        file_id = sqlite_store.store_file(
            repository_id=repository_row,
            path=path,
            relative_path=relative_path,
            language=language,
            size=len(content.encode("utf-8")),
            hash=content_hash,
            content_hash=content_hash,
            metadata=shard.get("metadata") if isinstance(shard, dict) else None,
        )
        with sqlite_store._get_connection() as conn:
            # Central chunk-scheme guard (CHUNKERSAFE Lane A): the dispatcher raw
            # upsert bypasses SQLiteStore.store_chunk, so route it through the same
            # check before any delete/insert. Stamps an empty index; raises
            # ChunkSchemeMismatchError (refusing the write) on a scheme mismatch.
            sqlite_store._assert_chunk_scheme_writable(conn, chunk_type="code")
            self._clear_file_index_rows(sqlite_store, file_id, conn=conn)

            for symbol in shard.get("symbols", []) if isinstance(shard, dict) else []:
                if not isinstance(symbol, dict):
                    continue
                name = symbol.get("symbol") or symbol.get("name")
                if not name:
                    continue
                span = symbol.get("span") or ()
                line_start = symbol.get("line_start") or symbol.get("line")
                line_end = symbol.get("line_end")
                if not line_start and isinstance(span, (list, tuple)) and span:
                    line_start = span[0]
                if not line_end and isinstance(span, (list, tuple)) and len(span) > 1:
                    line_end = span[1]
                line_start = int(line_start or 1)
                line_end = int(line_end or line_start)
                cursor = conn.execute(
                    """INSERT INTO symbols
                       (file_id, name, kind, line_start, line_end, column_start,
                        column_end, signature, documentation, metadata, token_count, token_model)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        file_id,
                        str(name),
                        str(symbol.get("kind") or "symbol"),
                        line_start,
                        line_end,
                        symbol.get("column_start") or symbol.get("column"),
                        symbol.get("column_end"),
                        symbol.get("signature"),
                        symbol.get("documentation") or symbol.get("doc"),
                        json.dumps(symbol.get("metadata") or symbol),
                        None,
                        None,
                    ),
                )
                sqlite_store._store_trigrams(conn, cursor.lastrowid, str(name))

            shard_chunks = shard.get("chunks") if isinstance(shard, dict) else None
            if shard_chunks is None:
                try:
                    from chunker import chunk_text

                    shard_chunks = [chunk.__dict__ for chunk in chunk_text(content, language)]
                except Exception as exc:
                    logger.debug(
                        "Host chunk derivation failed for %s: %s", path, type(exc).__name__
                    )
                    shard_chunks = []

            for index, chunk in enumerate(shard_chunks):
                if not isinstance(chunk, dict):
                    continue
                chunk_content = str(chunk.get("content") or "")
                if not chunk_content:
                    continue
                start = int(chunk.get("content_start") or chunk.get("byte_start") or 0)
                end = int(
                    chunk.get("content_end") or chunk.get("byte_end") or start + len(chunk_content)
                )
                line_start = int(chunk.get("line_start") or chunk.get("start_line") or 1)
                line_end = int(chunk.get("line_end") or chunk.get("end_line") or line_start)
                chunk_id = str(
                    chunk.get("chunk_id")
                    or hashlib.sha256(
                        f"{relative_path}:{index}:{start}:{end}".encode("utf-8")
                    ).hexdigest()
                )
                conn.execute(
                    """INSERT INTO code_chunks
                       (file_id, symbol_id, content, content_start, content_end,
                        line_start, line_end, chunk_id, node_id, treesitter_file_id,
                        symbol_hash, definition_id, token_count, token_model,
                        chunk_type, language, node_type, parent_chunk_id, depth,
                        chunk_index, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(file_id, chunk_id) DO UPDATE SET
                       symbol_id=excluded.symbol_id,
                       content=excluded.content,
                       content_start=excluded.content_start,
                       content_end=excluded.content_end,
                       line_start=excluded.line_start,
                       line_end=excluded.line_end,
                       node_id=excluded.node_id,
                       treesitter_file_id=excluded.treesitter_file_id,
                       symbol_hash=excluded.symbol_hash,
                       definition_id=excluded.definition_id,
                       token_count=excluded.token_count,
                       token_model=excluded.token_model,
                       chunk_type=excluded.chunk_type,
                       language=excluded.language,
                       node_type=excluded.node_type,
                       parent_chunk_id=excluded.parent_chunk_id,
                       depth=excluded.depth,
                       chunk_index=excluded.chunk_index,
                       metadata=excluded.metadata,
                       updated_at=CURRENT_TIMESTAMP""",
                    (
                        file_id,
                        None,
                        chunk_content,
                        start,
                        end,
                        line_start,
                        line_end,
                        chunk_id,
                        str(chunk.get("node_id") or chunk_id),
                        str(chunk.get("file_id") or relative_path),
                        chunk.get("symbol_hash"),
                        chunk.get("definition_id"),
                        chunk.get("token_count"),
                        chunk.get("token_model"),
                        chunk.get("chunk_type") or "code",
                        language,
                        chunk.get("node_type"),
                        chunk.get("parent_chunk_id"),
                        int(chunk.get("depth") or 0),
                        int(chunk.get("chunk_index") or index),
                        json.dumps(
                            _merge_chunk_source_metadata(
                                chunk.get("metadata"),
                                chunk_content,
                                line_start,
                            )
                        ),
                    ),
                )

            conn.execute("DELETE FROM fts_code WHERE file_id = ?", (str(file_id),))
            conn.execute(
                "INSERT INTO fts_code (content, file_id) VALUES (?, ?)", (content, file_id)
            )

    def _clear_file_index_rows(
        self,
        sqlite_store: SQLiteStore,
        file_id: int,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        if conn is None:
            with sqlite_store._get_connection() as owned_conn:
                self._clear_file_index_rows(sqlite_store, file_id, conn=owned_conn)
                return
        conn.execute(
            "DELETE FROM symbol_trigrams WHERE symbol_id IN "
            "(SELECT id FROM symbols WHERE file_id = ?)",
            (file_id,),
        )
        conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM code_chunks WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM fts_code WHERE file_id = ?", (str(file_id),))

    def get_statistics(self, ctx: RepoContext) -> Dict[str, Any]:
        """Get statistics about indexed files and languages."""
        try:
            by_language: Dict[str, int] = {}
            with self._file_cache_lock:
                cached_paths = list(self._file_cache.keys())
            for file_path in cached_paths:
                for lang, plugin in self._lang_cache.items():
                    if plugin.supports(Path(file_path)):
                        by_language[lang] = by_language.get(lang, 0) + 1
                        break
            return {
                "total": len(cached_paths),
                "by_language": by_language,
            }
        except Exception as exc:
            record_handled_error(__name__, exc)
            return {"total": 0, "by_language": {}}

    @_semantic_operation
    def _drain_pending_vector_deletions(self, ctx: RepoContext) -> None:
        """Best-effort recovery drain of the remote-vector crash-ledger (I4).

        A crashed chunk-scheme rebuild durably records orphaned remote (Qdrant)
        point ids in the SQLite ``pending_vector_deletions`` ledger, but the local
        ``semantic_points`` mappings are already gone, so nothing else will ever
        clean the remote vectors. Reindex is the natural recovery point: here BOTH
        the store and a semantic (Qdrant) client are available, so finish the
        deferred remote delete and clear only the rows actually drained.

        No-ops cleanly when semantic indexing is disabled (no client) or the store
        predates the ledger, and never raises - a recovery failure must not crash
        the reindex it is hooked into.
        """
        store = getattr(ctx, "sqlite_store", None)
        if ctx.staging:
            return
        if store is None or not hasattr(store, "drain_pending_vector_deletions"):
            return
        try:
            sem = self._get_semantic_indexer(ctx)
        except Exception:  # semantic client build is best-effort here
            sem = None
        if sem is None or not hasattr(sem, "delete_remote_points"):
            return
        try:
            result = store.drain_pending_vector_deletions(
                lambda collection, point_ids: sem.delete_remote_points(
                    point_ids, collection=collection
                )
            )
        except Exception as exc:  # store method is fail-safe, but never crash reindex
            logger.warning("Pending-vector-deletion drain skipped: %s", type(exc).__name__)
            return
        if result.get("rows_drained") or result.get("groups_failed"):
            logger.info(
                "Pending-vector-deletion drain: %s drained, %s remaining, " "%s group(s) deferred",
                result.get("rows_drained"),
                result.get("rows_remaining"),
                result.get("groups_failed"),
            )

    @_semantic_operation
    def index_directory(
        self,
        ctx: RepoContext,
        directory: Path,
        recursive: bool = True,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Index all files in a directory, respecting ignore patterns."""
        directory = self._input_path(ctx, directory)
        logger.info(f"Indexing directory: {directory} (recursive={recursive})")

        # Recovery point (I4): finish any deferred remote-vector deletions left by
        # a crashed chunk-scheme rebuild before we repopulate. Reindex is where both
        # the store and a semantic (Qdrant) client are available; best-effort so a
        # recovery failure can never block the reindex.
        self._drain_pending_vector_deletions(ctx)

        # Get all supported extensions
        supported_extensions = get_all_extensions()

        stats = {
            "total_files": 0,
            "indexed_files": 0,
            "ignored_files": 0,
            "failed_files": 0,
            "by_language": {},
            "summaries_written": 0,
            "summary_chunks_attempted": 0,
            "summary_missing_chunks": 0,
            "semantic_indexed": 0,
            "semantic_failed": 0,
            "semantic_skipped": 0,
            "semantic_blocked": 0,
            "semantic_stage": "not_run",
            "lexical_stage": "not_run",
            "lexical_files_attempted": 0,
            "lexical_files_completed": 0,
            "last_progress_path": None,
            "in_flight_path": None,
            "low_level_blocker": None,
            "storage_diagnostics": None,
        }

        def emit_progress(stage: str, stage_family: str, blocker_source: str) -> None:
            if progress_callback is None:
                return
            progress_callback(
                {
                    "stage": stage,
                    "stage_family": stage_family,
                    "blocker_source": blocker_source,
                    "lexical_stage": stats.get("lexical_stage"),
                    "semantic_stage": stats.get("semantic_stage"),
                    "last_progress_path": stats.get("last_progress_path"),
                    "in_flight_path": stats.get("in_flight_path"),
                    "summary_call_timed_out": stats.get("summary_call_timed_out", False),
                    "summary_call_file_path": stats.get("summary_call_file_path"),
                    "summary_call_chunk_ids": list(stats.get("summary_call_chunk_ids", []) or []),
                    "summary_call_timeout_seconds": stats.get("summary_call_timeout_seconds"),
                }
            )

        is_excluded = build_walker_filter(directory)

        # Walk directory while pruning excluded directories early.
        if recursive:

            def iter_files() -> Iterable[Path]:
                # followlinks=False (default, made explicit) keeps us cycle-safe.
                for current_root, dirnames, filenames in os.walk(directory, followlinks=False):
                    root_path = Path(current_root)
                    dirnames[:] = [
                        dirname
                        for dirname in dirnames
                        if dirname not in _INDEX_EXCLUDED_DIRS
                        and not is_excluded(root_path / dirname)
                    ]
                    for filename in filenames:
                        yield root_path / filename

        else:

            def iter_files() -> Iterable[Path]:
                for path in directory.glob("*"):
                    if path.is_file():
                        yield path

        # Collect paths that were successfully indexed for batch semantic embedding
        semantically_indexed_paths: List[Path] = []

        for path in iter_files():
            if cancel_check is not None and cancel_check():
                stats["lexical_stage"] = "cancelled"
                stats["in_flight_path"] = None
                stats["cancelled"] = True
                emit_progress("cancelled", "lexical", "lexical_mutation")
                break
            if not path.is_file():
                continue

            stats["total_files"] += 1

            relative_parts = (
                path.relative_to(directory).parts if path.is_relative_to(directory) else path.parts
            )
            if any(part.endswith(".egg-info") for part in relative_parts):
                stats["ignored_files"] += 1
                continue

            if path.name in _INDEX_EXCLUDED_FILENAMES:
                stats["ignored_files"] += 1
                continue

            if path.suffix.lower() in _INDEX_EXCLUDED_SUFFIXES:
                stats["ignored_files"] += 1
                continue

            if is_excluded(path):
                stats["ignored_files"] += 1
                continue

            try:
                size = path.stat().st_size
            except OSError:
                continue
            exact_bounded_json = GenericTreeSitterPlugin.uses_exact_bounded_json_path(
                path, directory
            )
            exact_bounded_jsonl = GenericTreeSitterPlugin.uses_exact_bounded_jsonl_path(
                path, directory
            )
            if (
                size > get_max_file_size_bytes()
                and not exact_bounded_json
                and not exact_bounded_jsonl
            ):
                logger.warning("skipping oversized file: %s (%d bytes)", path, size)
                stats["ignored_files"] = stats.get("ignored_files", 0) + 1
                continue

            # Try to find a plugin that supports this file
            # This allows us to index ALL files, including .env, .key, etc.
            try:
                # First try to match by extension
                if path.suffix in supported_extensions or exact_bounded_jsonl:
                    mutation = self._index_file_with_lexical_timeout(
                        ctx,
                        path,
                        stats,
                        progress_callback=emit_progress,
                    )
                    if mutation.status == IndexResultStatus.INDEXED:
                        stats["indexed_files"] += 1
                        semantically_indexed_paths.append(path.resolve())
                        emit_progress("lexical_walking", "lexical", "lexical_mutation")
                    elif self._is_non_indexable_result(mutation):
                        stats["ignored_files"] += 1
                    else:
                        stats["failed_files"] += 1
                        if mutation.error:
                            stats.setdefault("errors", []).append(f"{path}: {mutation.error}")
                        if self._is_storage_lock_error(mutation.error):
                            self._record_low_level_blocker(
                                ctx,
                                stats,
                                code="sqlite_runtime_failure",
                                message=mutation.error,
                                path=path,
                                stage="blocked_storage_error",
                            )
                            break
                # For files without recognized extensions, try each plugin's supports() method
                # This allows plugins to match by filename patterns (e.g., .env, Dockerfile)
                else:
                    matched = False
                    for plugin in self._plugin_set_registry.plugins_for(ctx.repo_id):
                        if plugin.supports(path):
                            mutation = self._index_file_with_lexical_timeout(
                                ctx,
                                path,
                                stats,
                                progress_callback=emit_progress,
                            )
                            if mutation.status == IndexResultStatus.INDEXED:
                                stats["indexed_files"] += 1
                                semantically_indexed_paths.append(path.resolve())
                                emit_progress("lexical_walking", "lexical", "lexical_mutation")
                            elif self._is_non_indexable_result(mutation):
                                stats["ignored_files"] += 1
                            else:
                                stats["failed_files"] += 1
                                if mutation.error:
                                    stats.setdefault("errors", []).append(
                                        f"{path}: {mutation.error}"
                                    )
                                if self._is_storage_lock_error(mutation.error):
                                    self._record_low_level_blocker(
                                        ctx,
                                        stats,
                                        code="sqlite_runtime_failure",
                                        message=mutation.error,
                                        path=path,
                                        stage="blocked_storage_error",
                                    )
                                    break
                            matched = True
                            break

                    # If no plugin matched but we want to index everything,
                    # we could add a fallback here to index as plaintext
                    # For now, we'll skip unmatched files
                    if not matched:
                        logger.debug(f"No plugin found for {path}")

                # Track by language
                language = get_language_by_extension(path.suffix)
                if language:
                    stats["by_language"][language] = stats["by_language"].get(language, 0) + 1

            except TimeoutError as exc:
                logger.error("Lexical indexing timed out for %s: %s", path, type(exc).__name__)
                stats["failed_files"] += 1
                self._record_low_level_blocker(
                    ctx,
                    stats,
                    code="lexical_file_timeout",
                    message=(f"Lexical indexing timed out while processing {path.name}"),
                    path=path,
                    stage="blocked_file_timeout",
                )
                emit_progress("blocked_file_timeout", "lexical", "lexical_mutation")
                break
            except sqlite3.OperationalError as exc:
                logger.error(
                    "SQLite operational error while indexing %s: %s", path, type(exc).__name__
                )
                stats["failed_files"] += 1
                self._record_low_level_blocker(
                    ctx,
                    stats,
                    code="sqlite_runtime_failure",
                    message=str(exc),
                    path=path,
                    stage="blocked_storage_error",
                )
                emit_progress("blocked_storage_error", "lexical", "lexical_mutation")
                break
            except Exception as e:
                logger.error(f"Failed to index {path}: {type(e).__name__}")
                stats["failed_files"] += 1

            if stats["low_level_blocker"] is not None:
                break

        if stats["low_level_blocker"] is None:
            if stats.get("cancelled"):
                stats["lexical_stage"] = "cancelled"
                stats["in_flight_path"] = None
            else:
                stats["lexical_stage"] = "completed"
                stats["in_flight_path"] = None
                emit_progress(
                    "force_full_closeout_handoff",
                    "final_closeout",
                    "final_closeout",
                )

        # Batch semantic embedding — O(n/1000) API calls instead of O(n)
        _sem = self._get_semantic_indexer(ctx)
        stats["semantic_paths_queued"] = len(semantically_indexed_paths)
        stats["semantic_indexer_present"] = _sem is not None
        if (
            stats["low_level_blocker"] is None
            and not stats.get("cancelled")
            and _sem
            and semantically_indexed_paths
        ):
            try:
                stats.update(
                    self.rebuild_semantic_for_paths(
                        ctx,
                        semantically_indexed_paths,
                        progress_callback=progress_callback,
                        cancel_check=cancel_check,
                    )
                )
            except Exception as e:
                logger.error(f"Batch semantic indexing failed: {type(e).__name__}")
                storage_failure = self._classify_storage_closeout_failure(ctx, e)
                if storage_failure is not None:
                    stats["semantic_stage"] = "blocked_storage_error"
                    stats["semantic_blocked"] = len(semantically_indexed_paths)
                    stats["semantic_error"] = storage_failure["storage_failure_message"]
                    stats["semantic_blocker"] = {
                        "code": "storage_closeout",
                        "message": storage_failure["storage_failure_message"],
                    }
                    stats.update(storage_failure)
                    stats["storage_diagnostics"] = self._collect_storage_diagnostics(ctx)
                    emit_progress(
                        "blocked_storage_error",
                        "final_closeout",
                        "storage_closeout",
                    )
                else:
                    stats["semantic_stage"] = "failed"
                    stats["semantic_error"] = str(e)

        logger.info(
            f"Directory indexing complete: {stats['indexed_files']} indexed, "
            f"{stats['ignored_files']} ignored, {stats['failed_files']} failed"
        )

        return stats

    def _index_file_with_lexical_timeout(
        self,
        ctx: RepoContext,
        path: Path,
        stats: Dict[str, Any],
        progress_callback: Optional[Callable[[str, str, str], None]] = None,
    ) -> IndexResult:
        stats["lexical_stage"] = "walking"
        stats["lexical_files_attempted"] += 1
        stats["in_flight_path"] = str(path.resolve())
        if progress_callback is not None:
            progress_callback("lexical_walking", "lexical", "lexical_mutation")
        mutation = _run_blocking_with_timeout(
            lambda: self.index_file(ctx, path, do_semantic=False),
            timeout_seconds=_get_lexical_timeout_seconds(),
        )
        stats["lexical_files_completed"] += 1
        stats["last_progress_path"] = str(path.resolve())
        stats["in_flight_path"] = None
        return mutation

    def _record_low_level_blocker(
        self,
        ctx: RepoContext,
        stats: Dict[str, Any],
        *,
        code: str,
        message: str,
        path: Path,
        stage: str,
    ) -> None:
        stats["lexical_stage"] = stage
        stats["in_flight_path"] = str(path.resolve())
        stats["low_level_blocker"] = {
            "code": code,
            "message": message,
            "path": str(path.resolve()),
        }
        stats["storage_diagnostics"] = self._collect_storage_diagnostics(ctx)

    def _collect_storage_diagnostics(self, ctx: RepoContext) -> Optional[Dict[str, Any]]:
        sqlite_store = getattr(ctx, "sqlite_store", None)
        if sqlite_store is None or not hasattr(sqlite_store, "health_check"):
            return None
        try:
            return sqlite_store.health_check()
        except Exception as exc:
            return {
                "status": "unhealthy",
                "error": f"Storage diagnostics failed: {exc}",
            }

    def _classify_storage_closeout_failure(
        self, ctx: RepoContext, error: BaseException
    ) -> Optional[Dict[str, Any]]:
        sqlite_store = getattr(ctx, "sqlite_store", None)
        if sqlite_store is not None and hasattr(sqlite_store, "get_readonly_diagnostics"):
            diagnostics = sqlite_store.get_readonly_diagnostics()
            if diagnostics is not None:
                return diagnostics
        if isinstance(error, TransientArtifactError):
            return classify_sqlite_storage_failure(error)
        if isinstance(error, sqlite3.OperationalError):
            return classify_sqlite_storage_failure(error)
        return None

    def _is_storage_lock_error(self, error: Optional[str]) -> bool:
        if not error:
            return False
        lowered = error.lower()
        return "database is locked" in lowered or "database table is locked" in lowered

    def _is_non_indexable_result(self, mutation: IndexResult) -> bool:
        if mutation.status in {
            IndexResultStatus.SKIPPED_UNCHANGED,
            IndexResultStatus.SKIPPED_TOCTOU,
        }:
            return True
        return mutation.status == IndexResultStatus.ERROR and bool(
            mutation.error and mutation.error.startswith("No plugin for ")
        )

    def search_documentation(
        self,
        ctx: RepoContext,
        topic: str,
        doc_types: Optional[List[str]] = None,
        limit: int = 20,
    ) -> Iterable[SearchResult]:
        """Search specifically across documentation files scoped to ctx.repo_id.

        Args:
            ctx: Repository context for storage routing
            topic: Topic to search for (e.g., "installation", "configuration")
            doc_types: Optional list of document types to search (e.g., ["readme", "guide", "api"])
            limit: Maximum number of results

        Returns:
            Search results from documentation files
        """
        # Default document types if not specified
        if doc_types is None:
            doc_types = [
                "readme",
                "documentation",
                "guide",
                "tutorial",
                "api",
                "changelog",
                "contributing",
            ]

        # Build search queries for different document types
        queries = []
        for doc_type in doc_types:
            queries.extend([f"{doc_type} {topic}", f"{topic} {doc_type}", f"{topic} in {doc_type}"])

        # Also search for the topic in common doc filenames
        queries.extend(
            [
                f"README {topic}",
                f"CONTRIBUTING {topic}",
                f"docs {topic}",
                f"documentation {topic}",
            ]
        )

        # Deduplicate queries
        queries = list(dict.fromkeys(queries))

        logger.info(
            "Cross-document search (topic_chars=%d, variations=%d)",
            len(topic or ""),
            len(queries),
        )

        # Use the enhanced search with document-specific handling
        all_results = []
        seen = set()

        for query in queries[:10]:
            for result in self.search(ctx, query, semantic=True, limit=limit):
                # Only include documentation files
                if self._is_documentation_file(result.get("file", "")):
                    key = f"{result['file']}:{result['line']}"
                    if key not in seen:
                        seen.add(key)
                        all_results.append(result)

        # Sort by relevance (score) and return top results
        all_results.sort(key=lambda r: r.get("score", 0.5) or 0.5, reverse=True)

        count = 0
        for result in all_results:
            if count >= limit:
                break
            yield result
            count += 1

    def health_check(self, ctx: RepoContext) -> Dict[str, Any]:
        """Perform a health check on all components."""
        health = {
            "status": "healthy",
            "components": {
                "dispatcher": {
                    "status": "healthy",
                    "plugins_loaded": self._managed_plugin_count(ctx.repo_id),
                    "languages_supported": len(self.supported_languages()),
                    "factory_enabled": self._use_factory,
                    "lazy_loading": self._lazy_load,
                }
            },
            "plugins": {},
            "errors": [],
        }
        try:
            health.update(
                {
                    "runtime_features": self.get_runtime_feature_status(ctx),
                    "graph_initialized": self.get_runtime_feature_status(ctx)["graph"]["status"]
                    == "available",
                }
            )
        except Exception:
            health["graph_initialized"] = False

        # Check plugin health
        for lang, plugin in self._lang_cache.items():
            try:
                plugin_health = {
                    "status": "healthy",
                    "class": plugin.__class__.__name__,
                    "semantic_enabled": getattr(plugin, "_enable_semantic", False),
                }
                if hasattr(plugin, "get_indexed_count"):
                    plugin_health["indexed_files"] = plugin.get_indexed_count()
            except Exception as e:
                plugin_health = {"status": "error", "error": str(e)}
                health["errors"].append(f"Plugin {lang}: {str(e)}")

            health["plugins"][lang] = plugin_health

        # Determine overall health
        if len(health["errors"]) > 0:
            health["status"] = "degraded" if len(health["errors"]) < 3 else "unhealthy"

        return health

    @_semantic_operation
    def remove_file(self, ctx: RepoContext, path: Union[Path, str]) -> IndexResult:
        """Remove a file from all per-repo indexes."""
        path = self._input_path(ctx, path).resolve()
        logger.info(f"Removing file from index: {path}")

        # Evict from skip cache so a subsequent index_file() call is not skipped
        with self._file_cache_lock:
            self._file_cache.pop(str(path), None)

        primary_result = IndexResult(
            status=IndexResultStatus.ERROR,
            path=path,
            observed_hash=None,
            actual_hash=None,
            error="sqlite store unavailable",
        )

        try:
            semantic_stats = None
            _sem = self._get_semantic_indexer(ctx)
            if ctx.sqlite_store:
                from ..core.path_resolver import PathResolver

                path_resolver = PathResolver(repository_root=ctx.workspace_root)
                try:
                    relative_path = path_resolver.normalize_path(path)
                    invalidation = ctx.sqlite_store.plan_semantic_invalidation(
                        relative_path,
                        repository_id=self._sqlite_repository_id(ctx),
                        profile_id=(_sem.semantic_profile.profile_id if _sem is not None else None),
                    )
                    removed = ctx.sqlite_store.remove_file(
                        relative_path, repository_id=self._sqlite_repository_id(ctx)
                    )
                    if removed:
                        primary_result = IndexResult(
                            status=IndexResultStatus.DELETED,
                            path=path,
                            observed_hash=None,
                            actual_hash=None,
                        )
                    else:
                        primary_result = IndexResult(
                            status=IndexResultStatus.NOT_FOUND,
                            path=path,
                            observed_hash=None,
                            actual_hash=None,
                            error="indexed file row not found",
                        )
                    if _sem is not None:
                        semantic_stats = _sem.cleanup_stale_semantic_artifacts(
                            profile_id=_sem.semantic_profile.profile_id,
                            invalidation=invalidation,
                            sqlite_store=ctx.sqlite_store,
                        )
                except Exception as e:
                    logger.error(f"Error removing from SQLite: {type(e).__name__}")
                    primary_result = IndexResult(
                        status=IndexResultStatus.ERROR,
                        path=path,
                        observed_hash=None,
                        actual_hash=None,
                        error=str(e),
                    )

            # Remove from plugin fuzzy index if available
            try:
                plugin = self._match_plugin(ctx, path)
                if plugin and hasattr(plugin, "_indexer") and plugin._indexer:
                    plugin._indexer.remove_file(path)
            except Exception as e:
                logger.warning(f"Error removing from plugin index: {type(e).__name__}")

            # Update statistics
            if primary_result.status == IndexResultStatus.DELETED:
                self._operation_stats["deletions"] = self._operation_stats.get("deletions", 0) + 1
            primary_result.semantic = semantic_stats
            return primary_result

        except Exception as e:
            logger.error(f"Error removing file {path}: {type(e).__name__}")
            return IndexResult(
                status=IndexResultStatus.ERROR,
                path=path,
                observed_hash=None,
                actual_hash=None,
                error=str(e),
            )

    @_semantic_operation
    def move_file(
        self,
        ctx: RepoContext,
        old_path: Union[Path, str],
        new_path: Union[Path, str],
        content_hash: Optional[str] = None,
    ) -> IndexResult:
        """Relocate a file in the per-repo index.

        SQLite path-update is the primary op; semantic re-embed is the shadow op, both
        wrapped in two_phase_commit so a semantic-indexer failure rolls SQLite back.

        Lock re-entry: callers inside the watcher loop already hold
        lock_registry.acquire(repo_id); the reentrant RLock makes any re-acquire here
        a no-op.  Callers outside the watcher loop must acquire the lock themselves.
        """
        old_path = self._input_path(ctx, old_path).resolve()
        new_path = self._input_path(ctx, new_path).resolve()
        logger.info(f"Moving file in index: {old_path} -> {new_path}")

        if not ctx.sqlite_store:
            # No SQLite — nothing to do
            self._operation_stats["moves"] = self._operation_stats.get("moves", 0) + 1
            return IndexResult(
                status=IndexResultStatus.ERROR,
                path=new_path,
                observed_hash=None,
                actual_hash=None,
                error="sqlite store unavailable",
            )

        from ..core.path_resolver import PathResolver

        workspace = ctx.workspace_root if ctx.workspace_root else None
        path_resolver = PathResolver(repository_root=workspace)
        try:
            old_relative = path_resolver.normalize_path(old_path)
            new_relative = path_resolver.normalize_path(new_path)
        except Exception as e:
            logger.error(
                f"Error normalizing paths for move {old_path} -> {new_path}: {type(e).__name__}"
            )
            return IndexResult(
                status=IndexResultStatus.ERROR,
                path=new_path,
                observed_hash=None,
                actual_hash=None,
                error=str(e),
            )

        store = ctx.sqlite_store
        repository_id = self._sqlite_repository_id(ctx)
        semantic_contract = self.get_semantic_summary_contract(ctx) or {}
        _sem = self._get_semantic_indexer(ctx)
        invalidation = store.plan_semantic_invalidation(
            old_relative,
            repository_id=repository_id,
            profile_id=_sem.semantic_profile.profile_id if _sem is not None else None,
            new_relative_path=new_relative,
            preserve_matching_summaries=True,
            expected_summary_profile_id=semantic_contract.get("profile_id"),
            expected_prompt_fingerprint=semantic_contract.get("prompt_fingerprint"),
        )

        def _sqlite_primary() -> Tuple[str, str]:
            moved = store.move_file(
                old_relative,
                new_relative,
                repository_id=repository_id,
                content_hash=content_hash,
            )
            if not moved:
                raise FileNotFoundError(old_relative)
            return old_relative, new_relative

        def _semantic_shadow(_result: Tuple[str, str]) -> None:
            if _sem is not None:
                _sem.cleanup_stale_semantic_artifacts(
                    profile_id=_sem.semantic_profile.profile_id,
                    invalidation=invalidation,
                    sqlite_store=store,
                )

        def _sqlite_rollback(_result: Tuple[str, str]) -> None:
            try:
                store.move_file(
                    new_relative,
                    old_relative,
                    repository_id=repository_id,
                    content_hash=content_hash,
                )
                logger.info("Rolled back SQLite rename %s -> %s", new_relative, old_relative)
            except Exception as rb_exc:
                logger.error("Rollback failed for %s: %s", old_relative, type(rb_exc).__name__)

        try:
            two_phase_commit(
                primary_op=_sqlite_primary,
                shadow_op=_semantic_shadow,
                rollback=_sqlite_rollback,
            )
            semantic_stats = None
            if _sem is not None:
                semantic_stats = self.rebuild_semantic_for_paths(ctx, [new_path])
            self._operation_stats["moves"] = self._operation_stats.get("moves", 0) + 1
            return IndexResult(
                status=IndexResultStatus.MOVED,
                path=new_path,
                observed_hash=None,
                actual_hash=None,
                semantic=semantic_stats,
            )
        except FileNotFoundError:
            return IndexResult(
                status=IndexResultStatus.NOT_FOUND,
                path=old_path,
                observed_hash=None,
                actual_hash=None,
                error="indexed source row not found",
            )
        except TwoPhaseCommitError as exc:
            err = IndexingError(f"Atomic move failed {old_path} -> {new_path}: {exc}")
            record_handled_error(__name__, err)
            raise err from exc
        except Exception as e:
            logger.error(f"Error moving file {old_path} -> {new_path}: {type(e).__name__}")
            return IndexResult(
                status=IndexResultStatus.ERROR,
                path=new_path,
                observed_hash=None,
                actual_hash=None,
                error=str(e),
            )

    async def cross_repo_symbol_search(
        self,
        contexts: List[RepoContext],
        symbol: str,
        languages: Optional[List[str]] = None,
        max_repositories: int = 10,
    ) -> Dict[str, Any]:
        """Fan symbol-lookup across the provided repo contexts."""
        if not self._cross_repo_coordinator:
            raise RuntimeError(
                "Cross-repository search not enabled. Set MCP_ENABLE_MULTI_REPO=true"
            )

        repositories = [ctx.repo_id for ctx in contexts]
        scope = SearchScope(
            repositories=repositories,
            languages=languages,
            max_repositories=max_repositories,
            priority_order=True,
        )

        try:
            result = await self._cross_repo_coordinator.search_symbol(symbol, scope)
            return {
                "query": result.query,
                "total_results": result.total_results,
                "repositories_searched": result.repositories_searched,
                "search_time": result.search_time,
                "results": result.results,
                "repository_stats": result.repository_stats,
                "deduplication_stats": result.deduplication_stats,
            }
        except Exception as e:
            logger.error(f"Cross-repository symbol search failed: {type(e).__name__}")
            return {
                "query": symbol,
                "total_results": 0,
                "repositories_searched": 0,
                "search_time": 0.0,
                "results": [],
                "repository_stats": {},
                "deduplication_stats": {},
                "error": str(e),
            }

    async def cross_repo_code_search(
        self,
        contexts: List[RepoContext],
        query: str,
        languages: Optional[List[str]] = None,
        semantic: bool = False,
        max_repositories: int = 10,
    ) -> Dict[str, Any]:
        """Fan code-search across the provided repo contexts."""
        if not self._cross_repo_coordinator:
            raise RuntimeError(
                "Cross-repository search not enabled. Set MCP_ENABLE_MULTI_REPO=true"
            )

        repositories = [ctx.repo_id for ctx in contexts]
        scope = SearchScope(
            repositories=repositories,
            languages=languages,
            max_repositories=max_repositories,
            priority_order=True,
        )

        try:
            result = await self._cross_repo_coordinator.search_code(query, scope, semantic)

            # Convert to dictionary format for MCP tools
            return {
                "query": result.query,
                "total_results": result.total_results,
                "repositories_searched": result.repositories_searched,
                "search_time": result.search_time,
                "results": result.results,
                "repository_stats": result.repository_stats,
                "deduplication_stats": result.deduplication_stats,
            }
        except Exception as e:
            logger.error(f"Cross-repository code search failed: {type(e).__name__}")
            return {
                "query": query,
                "total_results": 0,
                "repositories_searched": 0,
                "search_time": 0.0,
                "results": [],
                "repository_stats": {},
                "deduplication_stats": {},
                "error": str(e),
            }

    async def get_cross_repo_statistics(self, contexts: List[RepoContext]) -> Dict[str, Any]:
        """Aggregate statistics across the provided repo contexts."""
        if not self._cross_repo_coordinator:
            return {
                "enabled": False,
                "message": "Cross-repository search not enabled. Set MCP_ENABLE_MULTI_REPO=true",
            }

        try:
            stats = await self._cross_repo_coordinator.get_search_statistics()
            stats["enabled"] = True
            return stats
        except Exception as e:
            logger.error(f"Failed to get cross-repository statistics: {type(e).__name__}")
            return {
                "enabled": True,
                "error": str(e),
                "total_repositories": 0,
                "total_files": 0,
                "total_symbols": 0,
                "languages": [],
                "repository_details": [],
            }

    def _ensure_graph_initialized(
        self, file_paths: Optional[List[str]] = None, ctx: Optional[RepoContext] = None
    ) -> bool:
        """
        Ensure graph components are initialized.

        Args:
            file_paths: Optional list of files to build graph from

        Returns:
            True if graph is initialized, False otherwise
        """
        if not CHUNKER_AVAILABLE:
            logger.warning("Graph features not available: TreeSitter Chunker not installed")
            return False

        if ctx is None:
            logger.warning("Graph features require a repository context")
            return False

        key = self._graph_key(ctx)
        state = self._graph_state.get(key)

        if state is not None and state.analyzer is not None and file_paths is None:
            return True

        try:
            # Initialize graph builder
            if self._graph_builder is None:
                self._graph_builder = XRefAdapter()

            # Build graph from files
            if file_paths:
                nodes, edges = self._graph_builder.build_graph(file_paths)
                self._graph_state[key] = _GraphState(
                    analyzer=GraphAnalyzer(nodes, edges),
                    selector=ContextSelector(nodes, edges),
                    nodes=nodes,
                    edges=edges,
                )

                logger.info(f"Graph initialized: {len(nodes)} nodes, {len(edges)} edges")
                return True
            else:
                # No files provided and not initialized
                return False

        except Exception as e:
            logger.error(f"Failed to initialize graph: {type(e).__name__}")
            return False

    def graph_search(
        self,
        ctx: RepoContext,
        query: str,
        expansion_radius: int = 1,
        max_context_nodes: int = 50,
        semantic: bool = False,
        limit: int = 20,
    ) -> Iterable[SearchResult]:
        """Search with graph-based context expansion within ctx.repo_id."""
        # First, perform regular search
        search_results = list(self.search(ctx, query, semantic=semantic, limit=limit))

        if not search_results:
            return

        # Try to expand with graph context
        graph_state = self._graph_state.get(self._graph_key(ctx))
        if graph_state and graph_state.selector:
            try:
                context_nodes = graph_state.selector.expand_search_results(
                    search_results, expansion_radius, max_context_nodes
                )

                # Add context nodes as additional results
                for node in context_nodes:
                    # Check if already in results
                    already_included = any(r.get("file") == node.file_path for r in search_results)
                    if not already_included:
                        yield {
                            "file": node.file_path,
                            "line": node.line_start or 1,
                            "snippet": f"Context: {node.symbol or node.kind}",
                            "score": node.score,
                            "language": node.language,
                            "context": True,
                        }
            except Exception as e:
                logger.error(f"Error expanding search with graph: {type(e).__name__}")

        # Yield original results
        for result in search_results:
            yield result

    def get_context_for_symbols(
        self,
        ctx: RepoContext,
        symbols: List[str],
        radius: int = 2,
        budget: int = 200,
        weights: Optional[Dict[str, float]] = None,
    ) -> Optional[GraphCutResult]:
        """Budgeted graph-cut over the symbols' neighborhood within ctx.repo_id.

        Args:
            ctx: Repository context
            symbols: Symbol names to find context for
            radius: Maximum distance from symbols
            budget: Maximum number of nodes in context
            weights: Scoring weights

        Returns:
            GraphCutResult or None if graph not available
        """
        graph_state = self._graph_state.get(self._graph_key(ctx))
        if not graph_state or not graph_state.selector:
            logger.warning("Context selector not initialized")
            return None

        try:
            # Find nodes matching symbols
            seed_nodes = []
            for node in graph_state.nodes:
                if node.symbol in symbols:
                    seed_nodes.append(node.id)

            if not seed_nodes:
                logger.warning("No graph nodes found for requested symbols")
                return None

            # Select context
            result = graph_state.selector.select_context(
                seeds=seed_nodes, radius=radius, budget=budget, weights=weights
            )

            return result

        except Exception as e:
            logger.error(f"Error getting context for symbols: {type(e).__name__}")
            return None

    def find_symbol_dependencies(
        self, ctx: RepoContext, symbol: str, max_depth: int = 3
    ) -> List[Dict[str, Any]]:
        """Outgoing dependency walk from symbol within ctx.repo_id.

        Returns:
            List of dependent symbols with metadata
        """
        graph_state = self._graph_state.get(self._graph_key(ctx))
        if not graph_state or not graph_state.analyzer:
            logger.warning("Graph analyzer not initialized")
            return []

        try:
            # Find node with this symbol
            node_id = None
            for node in graph_state.nodes:
                if node.symbol == symbol:
                    node_id = node.id
                    break

            if not node_id:
                logger.warning("Requested symbol not found in graph")
                return []

            # Get dependencies
            deps = graph_state.analyzer.find_dependencies(node_id, max_depth)

            # Convert to dict format
            return [
                {
                    "symbol": dep.symbol,
                    "file": dep.file_path,
                    "kind": dep.kind,
                    "language": dep.language,
                    "line": dep.line_start,
                }
                for dep in deps
            ]

        except Exception as e:
            logger.error("Dependency lookup failed (%s)", type(e).__name__)
            return []

    def find_symbol_dependents(
        self, ctx: RepoContext, symbol: str, max_depth: int = 3
    ) -> List[Dict[str, Any]]:
        """Incoming dependent walk into symbol within ctx.repo_id."""
        graph_state = self._graph_state.get(self._graph_key(ctx))
        if not graph_state or not graph_state.analyzer:
            logger.warning("Graph analyzer not initialized")
            return []

        try:
            # Find node with this symbol
            node_id = None
            for node in graph_state.nodes:
                if node.symbol == symbol:
                    node_id = node.id
                    break

            if not node_id:
                logger.warning("Requested symbol not found in graph")
                return []

            # Get dependents
            dependents = graph_state.analyzer.find_dependents(node_id, max_depth)

            # Convert to dict format
            return [
                {
                    "symbol": dep.symbol,
                    "file": dep.file_path,
                    "kind": dep.kind,
                    "language": dep.language,
                    "line": dep.line_start,
                }
                for dep in dependents
            ]

        except Exception as e:
            logger.error("Dependent lookup failed (%s)", type(e).__name__)
            return []

    def get_code_hotspots(self, ctx: RepoContext, top_n: int = 10) -> List[Dict[str, Any]]:
        """Highest-centrality symbols in the per-repo call graph.

        Returns:
            List of hotspot information
        """
        graph_state = self._graph_state.get(self._graph_key(ctx))
        if not graph_state or not graph_state.analyzer:
            logger.warning("Graph analyzer not initialized")
            return []

        try:
            hotspots = graph_state.analyzer.get_hotspots(top_n)

            return [
                {
                    "symbol": node.symbol,
                    "file": node.file_path,
                    "kind": node.kind,
                    "language": node.language,
                    "line": node.line_start,
                    "score": node.score,
                }
                for node in hotspots
            ]

        except Exception as e:
            logger.error(f"Error getting hotspots: {type(e).__name__}")
            return []
