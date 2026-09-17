import hashlib
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .artifacts.semantic_profiles import SemanticProfileRegistry
from .cache import (
    CacheManagerFactory,
    QueryCacheConfig,
    QueryResultCache,
    QueryType,
)
from .cli.index_management import _get_profile_collection_name
from .cli.preflight_commands import format_preflight_report, run_startup_preflight
from .client import (
    ClientValidationError,
    build_search_options,
    execute_search_service,
    search_result_for_gateway,
)
from .config.environment import is_production as _is_production
from .config.settings import get_settings
from .config.validation import (
    WEAK_CREDENTIAL_BLOCKLIST,
    render_validation_errors_to_stderr,
    validate_production_config,
)
from .core import RepoContext, RepoResolver
from .core.logging import setup_logging
from .core.repo_resolver import run_repository_mutation
from .dispatcher.dispatcher_enhanced import EnhancedDispatcher
from .health.repository_readiness import RepositoryReadiness, RepositoryReadinessState
from .indexer.bm25_indexer import BM25Indexer
from .indexer.hybrid_search import HybridSearch, HybridSearchConfig
from .indexing.source_metadata import normalize_friction_category
from .metrics import get_health_checker, get_metrics_collector
from .metrics.middleware import get_business_metrics, setup_metrics_middleware
from .metrics.prometheus_exporter import get_prometheus_exporter
from .plugin_base import SymbolDef
from .plugin_system import PluginManager
from .security import (
    AuthCredentials,
    AuthenticationMiddleware,
    AuthManager,
    AuthorizationMiddleware,
    Permission,
    RateLimitMiddleware,
    RequestValidationMiddleware,
    SecurityConfig,
    SecurityHeadersMiddleware,
    User,
    UserRole,
    get_current_active_user,
    require_auth,
    require_permission,
    require_role,
)
from .security.path_guard import PathTraversalError, PathTraversalGuard
from .security.security_middleware import SecretRedactionResponseMiddleware
from .security.token_validator import TokenValidator
from .setup.qdrant_autostart import ensure_qdrant_running
from .setup.semantic_preflight import run_semantic_preflight
from .storage.sqlite_store import SQLiteStore
from .storage.store_registry import StoreRegistry
from .utils.fuzzy_indexer import FuzzyIndexer
from .utils.index_discovery import IndexDiscovery
from .utils.language_detector import detect_repository_languages
from .watcher.ref_poller import RefPoller
from .watcher_multi_repo import MultiRepositoryWatcher

# Set up logging
setup_logging(log_level="INFO")
logger = logging.getLogger(__name__)


def _parse_cors_origins_from_env() -> list[str]:
    """Parse CORS_ORIGINS env var into a cleaned list of origins.

    No default — an unset or empty value means "no cross-origin requests
    allowed" and emits a warning. Accepts comma-separated origins. Explicitly
    refuses "*" which would reintroduce the old footgun.
    """
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if not raw:
        logger.warning(
            "CORS_ORIGINS is unset — no cross-origin requests will be allowed. "
            "Set CORS_ORIGINS to a comma-separated allowlist (never '*' in production)."
        )
        return []
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if "*" in origins:
        logger.warning(
            "CORS_ORIGINS contains '*' — refusing wildcard origin. "
            "Replace with an explicit allowlist."
        )
        return [o for o in origins if o != "*"]
    return origins


def _build_security_config_from_env() -> SecurityConfig:
    """Build SecurityConfig from environment and fail closed on invalid input."""
    jwt_secret_key = os.getenv("JWT_SECRET_KEY")
    if not jwt_secret_key:
        raise RuntimeError(
            "JWT_SECRET_KEY env var must be set. Generate one with: openssl rand -base64 32"
        )

    try:
        return SecurityConfig(
            jwt_secret_key=jwt_secret_key,
            jwt_algorithm="HS256",
            access_token_expire_minutes=int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30")),
            refresh_token_expire_days=int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7")),
            password_min_length=int(os.getenv("PASSWORD_MIN_LENGTH", "8")),
            max_login_attempts=int(os.getenv("MAX_LOGIN_ATTEMPTS", "5")),
            lockout_duration_minutes=int(os.getenv("LOCKOUT_DURATION_MINUTES", "15")),
            rate_limit_requests=int(os.getenv("RATE_LIMIT_REQUESTS", "100")),
            rate_limit_window_minutes=int(os.getenv("RATE_LIMIT_WINDOW_MINUTES", "1")),
            cors_origins=_parse_cors_origins_from_env(),
            cors_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            cors_headers=["*"],
        )
    except Exception as exc:
        raise RuntimeError(f"Invalid JWT security configuration: {exc}") from exc


def _require_default_admin_password() -> str:
    """Return the configured default admin password or raise fail-closed."""
    admin_password = os.getenv("DEFAULT_ADMIN_PASSWORD")
    if not admin_password:
        raise RuntimeError(
            "DEFAULT_ADMIN_PASSWORD env var must be set. Choose a strong password for the admin account."
        )
    if admin_password.strip().lower() in WEAK_CREDENTIAL_BLOCKLIST:
        raise RuntimeError(
            "DEFAULT_ADMIN_PASSWORD is on the well-known-weak list. "
            "Pick a strong password (see config/validation.py blocklist)."
        )
    return admin_password


def _build_boot_auth_manager() -> tuple[SecurityConfig | None, AuthManager | None]:
    """Best-effort import-time auth bootstrap for pre-startup route protection."""
    try:
        config = _build_security_config_from_env()
    except RuntimeError:
        return None, None
    return config, AuthManager(config)


def _register_security_middleware(
    target_app: FastAPI,
    config: SecurityConfig | None,
    active_auth_manager: AuthManager | None,
) -> None:
    """Register the HTTP security stack before startup."""
    cors_origins = config.cors_origins if config is not None else _parse_cors_origins_from_env()
    security_headers = (
        config.security_headers
        if config is not None
        else SecurityConfig(jwt_secret_key="x" * 32).security_headers
    )

    target_app.add_middleware(SecurityHeadersMiddleware, security_headers=security_headers)
    target_app.add_middleware(AuthorizationMiddleware, auth_manager=active_auth_manager)
    target_app.add_middleware(AuthenticationMiddleware, auth_manager=active_auth_manager)
    target_app.add_middleware(RateLimitMiddleware, auth_manager=active_auth_manager)
    target_app.add_middleware(RequestValidationMiddleware)
    target_app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )


app = FastAPI(
    title="MCP Server",
    description="Code Index MCP Server with Security, Metrics, and Health Checks",
    version=__version__,
)
dispatcher: EnhancedDispatcher | None = None
repo_resolver: RepoResolver | None = None
sqlite_store: SQLiteStore | None = None
multi_watcher: MultiRepositoryWatcher | None = None
ref_poller: RefPoller | None = None
plugin_manager: PluginManager | None = None
plugin_loader = None  # Dynamic plugin loader
auth_manager: AuthManager | None = None
security_config: SecurityConfig | None = None
cache_manager = None
query_cache: QueryResultCache | None = None
bm25_indexer: BM25Indexer | None = None
hybrid_search: HybridSearch | None = None
fuzzy_indexer: FuzzyIndexer | None = None
git_index_manager: Any = None
semantic_indexer = None
profile_hydration_status: Dict[str, Any] | None = None
semantic_setup_status: Dict[str, Any] | None = None
_repo_registry = None
_store_registry: StoreRegistry | None = None
security_config, auth_manager = _build_boot_auth_manager()


def _get_path_guard() -> Optional[PathTraversalGuard]:
    from mcp_server.security.path_allowlist import parse_allowed_roots

    # Read env lazily so tests can monkeypatch MCP_ALLOWED_ROOTS before calling.
    raw = os.environ.get("MCP_ALLOWED_ROOTS", "")
    roots = parse_allowed_roots(raw)
    if not roots:
        # No roots configured — guard short-circuits (allow-all for dev/test).
        return None
    return PathTraversalGuard(roots)


def _normalize_search_result(raw_result: Any) -> Optional[dict[str, Any]]:
    """Normalize internal search payloads to the public SearchResult schema.

    Returns None if the result's file path fails the path traversal guard.
    """

    def _prefer_path(candidate: Any, fallback: Any) -> str:
        candidate_str = str(candidate or "")
        fallback_str = str(fallback or "")
        if candidate_str and not candidate_str.isdigit():
            return candidate_str
        return fallback_str or candidate_str

    if isinstance(raw_result, dict):
        file_value = _prefer_path(
            raw_result.get("file")
            or raw_result.get("file_path")
            or raw_result.get("filepath")
            or raw_result.get("defined_in"),
            raw_result.get("relative_path") or raw_result.get("path"),
        )

        guard = _get_path_guard()
        if guard is not None:
            try:
                guard.normalize_and_check(file_value)
            except PathTraversalError:
                logger.warning("path traversal attempt blocked: %r", file_value)
                return None

        start_line = raw_result.get("start_line")
        end_line = raw_result.get("end_line")

        if start_line is None:
            start_line = raw_result.get("line_start")
        if end_line is None:
            end_line = raw_result.get("line_end")

        if start_line is None:
            start_line = raw_result.get("line")

        if start_line is None:
            span = raw_result.get("span")
            if isinstance(span, (list, tuple)) and len(span) >= 2:
                start_line = span[0]
                if end_line is None:
                    end_line = span[1]
            elif isinstance(span, (list, tuple)) and len(span) >= 1:
                start_line = span[0]

        start_line = int(start_line) if start_line is not None else 1
        end_line = int(end_line) if end_line is not None else start_line

        snippet_value = (
            raw_result.get("snippet")
            or raw_result.get("context")
            or raw_result.get("content")
            or raw_result.get("doc")
            or raw_result.get("signature")
            or ""
        )

        # Prepend line numbers to snippet if start_line is known
        snippet_lines = str(snippet_value).split("\n")
        numbered_snippet = "\n".join(
            f"{start_line + i}: {line}" for i, line in enumerate(snippet_lines)
        )

        normalized: dict[str, Any] = {
            "file": str(file_value),
            "start_line": start_line,
            "end_line": end_line,
            "snippet": numbered_snippet,
        }
        if raw_result.get("source_metadata") is not None:
            normalized["source_metadata"] = raw_result.get("source_metadata")
        return normalized

    file_value = _prefer_path(
        getattr(raw_result, "file", None)
        or getattr(raw_result, "file_path", None)
        or getattr(raw_result, "filepath", None),
        getattr(raw_result, "relative_path", None) or getattr(raw_result, "path", None),
    )

    guard = _get_path_guard()
    if guard is not None:
        try:
            guard.normalize_and_check(file_value)
        except PathTraversalError:
            logger.warning("path traversal attempt blocked: %r", file_value)
            return None

    start_line = (
        getattr(raw_result, "start_line", None)
        or getattr(raw_result, "line_start", None)
        or getattr(raw_result, "line", None)
        or 1
    )
    end_line = (
        getattr(raw_result, "end_line", None) or getattr(raw_result, "line_end", None) or start_line
    )
    start_line = int(start_line)
    end_line = int(end_line)

    snippet_value = (
        getattr(raw_result, "snippet", None) or getattr(raw_result, "context", None) or ""
    )

    snippet_lines = str(snippet_value).split("\n")
    numbered_snippet = "\n".join(
        f"{start_line + i}: {line}" for i, line in enumerate(snippet_lines)
    )

    normalized = {
        "file": str(file_value),
        "start_line": start_line,
        "end_line": end_line,
        "snippet": numbered_snippet,
    }
    source_metadata = getattr(raw_result, "source_metadata", None)
    if source_metadata is not None:
        normalized["source_metadata"] = source_metadata
    return normalized


def _normalize_source_metadata_categories(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [normalize_friction_category(item) for item in value.split(",") if item.strip()]


def _normalize_csv_filters(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return sorted({item.strip() for item in value.split(",") if item.strip()}, key=str.lower)


language_detection_status: Dict[str, Any] | None = None

_FALLBACK_REPO_ID = "default"
_repo_search_backends: dict[str, tuple[SQLiteStore, BM25Indexer, FuzzyIndexer, HybridSearch]] = {}
_repo_search_backends_lock = threading.Lock()
_RECOVERABLE_REINDEX_STATES = frozenset(
    {
        RepositoryReadinessState.MISSING_INDEX,
        RepositoryReadinessState.INDEX_EMPTY,
        RepositoryReadinessState.STALE_COMMIT,
        RepositoryReadinessState.CORRUPT_SQLITE,
        RepositoryReadinessState.MISSING_SCHEMA,
        RepositoryReadinessState.MISSING_PROVENANCE,
        # CHUNKERSAFE Lane A: scheme-mismatched / mid-rebuild indexes must be
        # reindexable so a tripped guard is not a permanent dead-end.
        RepositoryReadinessState.SCHEME_MISMATCH,
        RepositoryReadinessState.INDEX_REBUILDING,
    }
)


def _repository_unavailable_detail(selector: str) -> dict[str, Any]:
    try:
        readiness = repo_resolver.classify(selector) if repo_resolver is not None else None
    except Exception:
        readiness = None
    return {
        "error": "Index unavailable",
        "code": getattr(getattr(readiness, "state", None), "value", "repository_unavailable"),
        "safe_fallback": "native_search",
        "repository": selector,
        "readiness": readiness.to_dict() if readiness is not None else None,
    }


def _search_backends_for_repo(
    ctx: RepoContext,
) -> tuple[Optional[BM25Indexer], Optional[FuzzyIndexer], Optional[HybridSearch]]:
    if ctx.repo_id == _FALLBACK_REPO_ID:
        return bm25_indexer, fuzzy_indexer, hybrid_search

    with _repo_search_backends_lock:
        cached = _repo_search_backends.get(ctx.repo_id)
        if cached is not None and cached[0] is ctx.sqlite_store:
            return cached[1], cached[2], cached[3]

        repo_bm25 = BM25Indexer(ctx.sqlite_store)
        repo_fuzzy = FuzzyIndexer(ctx.sqlite_store)
        source_config = hybrid_search.config if hybrid_search is not None else HybridSearchConfig()
        repo_hybrid = HybridSearch(
            storage=ctx.sqlite_store,
            bm25_indexer=repo_bm25,
            semantic_indexer=None,
            fuzzy_indexer=repo_fuzzy,
            config=HybridSearchConfig(
                bm25_weight=source_config.bm25_weight,
                semantic_weight=0.0,
                fuzzy_weight=source_config.fuzzy_weight,
                rrf_k=source_config.rrf_k,
                enable_bm25=source_config.enable_bm25,
                enable_semantic=False,
                enable_fuzzy=source_config.enable_fuzzy,
                individual_limit=source_config.individual_limit,
                final_limit=source_config.final_limit,
                parallel_execution=source_config.parallel_execution,
                parallel_timeout_seconds=source_config.parallel_timeout_seconds,
                cache_results=source_config.cache_results,
                min_bm25_score=source_config.min_bm25_score,
                min_semantic_score=source_config.min_semantic_score,
                min_fuzzy_score=source_config.min_fuzzy_score,
            ),
        )
        _repo_search_backends[ctx.repo_id] = (
            ctx.sqlite_store,
            repo_bm25,
            repo_fuzzy,
            repo_hybrid,
        )
        return repo_bm25, repo_fuzzy, repo_hybrid


def get_repo_ctx(request: Request) -> RepoContext:
    """Resolve a RepoContext for the current request.

    Resolution order:
    1. ``X-Repo-Id`` header value treated as an exact repository selector.
    2. ``?repository=<selector>`` query parameter.
    3. ``repo_resolver.resolve(cwd)`` fallback when a resolver is available.
    4. Minimal context built from the module-global ``sqlite_store`` as last resort.
    """
    candidate_selector: Optional[str] = None

    repo_id_header = request.headers.get("X-Repo-Id")
    if repo_id_header:
        candidate_selector = repo_id_header

    if candidate_selector is None:
        candidate_selector = request.query_params.get("repository")

    if candidate_selector is not None:
        if repo_resolver is None:
            raise HTTPException(503, detail=_repository_unavailable_detail(candidate_selector))
        try:
            readiness = repo_resolver.classify(candidate_selector)
        except Exception as exc:
            raise HTTPException(
                503, detail=_repository_unavailable_detail(candidate_selector)
            ) from exc
        if isinstance(readiness, RepositoryReadiness) and not readiness.ready:
            raise HTTPException(
                503,
                detail={
                    "error": "Index unavailable",
                    "code": readiness.code,
                    "safe_fallback": "native_search",
                    "repository": candidate_selector,
                    "readiness": readiness.to_dict(),
                },
            )
        resolved = (
            repo_resolver.resolve_ready(candidate_selector)
            if isinstance(repo_resolver, RepoResolver)
            else repo_resolver.resolve(candidate_selector)
        )
        if resolved is not None:
            return resolved
        raise HTTPException(503, detail=_repository_unavailable_detail(candidate_selector))

    if repo_resolver is not None:
        cwd_selector = str(Path.cwd())
        try:
            readiness = repo_resolver.classify(cwd_selector)
        except Exception as exc:
            raise HTTPException(503, detail=_repository_unavailable_detail(cwd_selector)) from exc
        if isinstance(readiness, RepositoryReadiness) and not readiness.ready:
            raise HTTPException(
                503,
                detail={
                    "error": "Index unavailable",
                    "code": readiness.code,
                    "safe_fallback": "native_search",
                    "readiness": readiness.to_dict(),
                },
            )
        resolved = (
            repo_resolver.resolve_ready(cwd_selector)
            if isinstance(repo_resolver, RepoResolver)
            else repo_resolver.resolve(cwd_selector)
        )
        if resolved is not None:
            return resolved
        if isinstance(repo_resolver, RepoResolver):
            raise HTTPException(503, detail=_repository_unavailable_detail(cwd_selector))

    if sqlite_store is None:
        raise HTTPException(503, detail="Repository resolver and fallback store are unavailable")

    # Final fallback: wrap the gateway-global sqlite_store in a minimal context.
    from datetime import datetime

    from .storage.multi_repo_manager import RepositoryInfo  # local import to avoid cycles

    cwd = Path.cwd()
    dummy_entry = RepositoryInfo(
        repository_id=_FALLBACK_REPO_ID,
        name="default",
        path=cwd,
        index_path=cwd,
        language_stats={},
        total_files=0,
        total_symbols=0,
        indexed_at=datetime.utcnow(),
    )
    return RepoContext(
        repo_id=_FALLBACK_REPO_ID,
        sqlite_store=sqlite_store,  # type: ignore[arg-type]
        workspace_root=cwd,
        tracked_branch="",
        registry_entry=dummy_entry,
    )


# Initialize metrics and health checking
metrics_collector = get_metrics_collector()
health_checker = get_health_checker()
business_metrics = get_business_metrics()

# Setup metrics middleware
setup_metrics_middleware(app, enable_detailed_metrics=True)

# Register SecretRedactionResponseMiddleware at import time so it applies
# regardless of how the app is booted.
app.add_middleware(SecretRedactionResponseMiddleware)
_register_security_middleware(app, security_config, auth_manager)
app.state.auth_manager = auth_manager
app.state.security_config = security_config


@app.on_event("startup")
async def startup_event():
    """Initialize the dispatcher and register plugins on startup."""
    global dispatcher, repo_resolver, sqlite_store, multi_watcher, ref_poller, plugin_manager, plugin_loader, auth_manager, security_config, cache_manager, query_cache, bm25_indexer, hybrid_search, fuzzy_indexer, git_index_manager, semantic_indexer, profile_hydration_status, semantic_setup_status, language_detection_status, _store_registry

    app.state.startup_time = time.monotonic()

    try:
        preflight_result = run_startup_preflight()
        for line in format_preflight_report(preflight_result):
            if preflight_result.status == "warning":
                logger.warning(line)
            else:
                logger.info(line)

        # Initialize security configuration
        logger.info("Initializing security configuration...")
        security_config = _build_security_config_from_env()

        # Validate security config before constructing auth manager
        _validation_errors = validate_production_config(
            security_config, environment=str(os.getenv("MCP_ENVIRONMENT", "development"))
        )
        if _validation_errors:
            render_validation_errors_to_stderr(_validation_errors)
            if any(e.severity == "fatal" for e in _validation_errors) and _is_production():
                sys.exit(1)

        # Initialize authentication manager
        logger.info("Initializing authentication manager...")
        auth_manager = AuthManager(security_config)
        app.state.auth_manager = auth_manager
        app.state.security_config = security_config

        # Validate GitHub token scopes (warns and continues if token absent; raises only if MCP_REQUIRE_TOKEN_SCOPES=1)
        TokenValidator.validate_scopes(
            required=[
                "contents:read",
                "metadata:read",
                "actions:read",
                "actions:write",
                "attestations:write",
            ]
        )

        # Create default admin user if it doesn't exist
        admin_user = await auth_manager.get_user_by_username("admin")
        if not admin_user:
            admin_password = _require_default_admin_password()
            logger.info("Creating default admin user...")
            await auth_manager.create_user(
                username="admin",
                password=admin_password,
                email=os.getenv("DEFAULT_ADMIN_EMAIL", "admin@localhost"),
                role=UserRole.ADMIN,
            )
            logger.info("Default admin user created")

        # Initialize cache system
        logger.info("Initializing cache system...")
        cache_backend_type = os.getenv("CACHE_BACKEND", "memory").lower()
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

        if cache_backend_type == "redis":
            try:
                cache_manager = CacheManagerFactory.create_redis_cache(
                    redis_url=redis_url,
                    default_ttl=int(os.getenv("CACHE_DEFAULT_TTL", "3600")),
                )
                logger.info("Using Redis cache backend")
            except Exception as e:
                logger.warning(
                    f"Failed to initialize Redis cache, falling back to memory: {type(e).__name__}"
                )
                cache_manager = CacheManagerFactory.create_memory_cache()
        elif cache_backend_type == "hybrid":
            try:
                cache_manager = CacheManagerFactory.create_hybrid_cache(
                    redis_url=redis_url,
                    max_entries=int(os.getenv("CACHE_MAX_ENTRIES", "1000")),
                    max_memory_mb=int(os.getenv("CACHE_MAX_MEMORY_MB", "100")),
                    default_ttl=int(os.getenv("CACHE_DEFAULT_TTL", "3600")),
                )
                logger.info("Using hybrid cache backend")
            except Exception as e:
                logger.warning(
                    f"Failed to initialize hybrid cache, falling back to memory: {type(e).__name__}"
                )
                cache_manager = CacheManagerFactory.create_memory_cache()
        else:
            cache_manager = CacheManagerFactory.create_memory_cache(
                max_entries=int(os.getenv("CACHE_MAX_ENTRIES", "1000")),
                max_memory_mb=int(os.getenv("CACHE_MAX_MEMORY_MB", "100")),
                default_ttl=int(os.getenv("CACHE_DEFAULT_TTL", "3600")),
            )
            logger.info("Using memory cache backend")

        await cache_manager.initialize()

        # Initialize query result cache
        query_cache_config = QueryCacheConfig(
            enabled=os.getenv("QUERY_CACHE_ENABLED", "true").lower() == "true",
            default_ttl=int(os.getenv("QUERY_CACHE_DEFAULT_TTL", "300")),
            symbol_lookup_ttl=int(os.getenv("QUERY_CACHE_SYMBOL_TTL", "1800")),
            search_ttl=int(os.getenv("QUERY_CACHE_SEARCH_TTL", "600")),
            semantic_search_ttl=int(os.getenv("QUERY_CACHE_SEMANTIC_TTL", "3600")),
        )
        query_cache = QueryResultCache(cache_manager, query_cache_config)
        logger.info("Query result cache initialized successfully")
        settings = get_settings()

        # Check for portable index first
        workspace_root = Path(".")
        discovery = IndexDiscovery(workspace_root)

        if discovery.is_index_enabled():
            logger.info("MCP portable index detected")

            required_schema = settings.index_schema_version
            required_model = settings.semantic_embedding_model
            strict_compatibility = settings.strict_index_compatibility
            recovery_branch = os.getenv("MCP_INDEX_RECOVERY_BRANCH")
            recovery_commit = os.getenv("MCP_INDEX_RECOVERY_COMMIT")

            # Try to use existing compatible index
            index_path = discovery.get_local_index_path(
                requested_schema_version=required_schema,
                requested_embedding_model=required_model,
                strict_compatibility=strict_compatibility,
            )

            # Optional branch/commit-targeted recovery path.
            if not index_path and (recovery_branch or recovery_commit):
                logger.info(
                    "Attempting branch/commit index recovery (branch=%s, commit=%s)",
                    recovery_branch,
                    recovery_commit,
                )
                if discovery.download_recovery_index(
                    branch=recovery_branch,
                    commit=recovery_commit,
                    requested_schema_version=required_schema,
                    requested_embedding_model=required_model,
                    strict_compatibility=strict_compatibility,
                ):
                    index_path = discovery.get_local_index_path(
                        requested_schema_version=required_schema,
                        requested_embedding_model=required_model,
                        strict_compatibility=strict_compatibility,
                    )

            if not index_path and discovery.should_download_index(
                requested_schema_version=required_schema,
                requested_embedding_model=required_model,
                strict_compatibility=strict_compatibility,
            ):
                logger.info("Attempting to download index from GitHub artifacts...")
                if discovery.download_latest_index_for_runtime(
                    requested_schema_version=required_schema,
                    requested_embedding_model=required_model,
                    strict_compatibility=strict_compatibility,
                ):
                    index_path = discovery.get_local_index_path(
                        requested_schema_version=required_schema,
                        requested_embedding_model=required_model,
                        strict_compatibility=strict_compatibility,
                    )
                    logger.info("Successfully downloaded index from artifacts")
                else:
                    logger.info("Could not download index, will use default")

            if index_path:
                logger.info(f"Using portable index: {index_path}")
                sqlite_store = SQLiteStore(str(index_path))

                # Log index info
                info = discovery.get_index_info()
                if info["metadata"]:
                    meta = info["metadata"]
                    logger.info(f"Index created: {meta.get('created_at', 'unknown')}")
                    logger.info(f"Index commit: {meta.get('commit', 'unknown')[:8]}")
            else:
                logger.info("No portable index found, using default")
                sqlite_store = SQLiteStore("code_index.db")
        else:
            # Initialize SQLite store with default
            logger.info("Initializing SQLite store with default path...")
            sqlite_store = SQLiteStore("code_index.db")

        # Initialize RepoResolver for per-request repo context resolution.
        _local_repo_registry = None
        try:
            from .storage.repository_registry import RepositoryRegistry

            _local_repo_registry = RepositoryRegistry()
            _store_registry = StoreRegistry.for_registry(_local_repo_registry)
            repo_resolver = RepoResolver(_local_repo_registry, _store_registry)
            import mcp_server.gateway as _gw_self

            _gw_self._repo_registry = _local_repo_registry
            logger.info("RepoResolver initialized")
        except Exception as _e:
            logger.warning(
                "RepoResolver init failed; falling back to global sqlite_store: %s",
                type(_e).__name__,
            )
            repo_resolver = None

        profile_registry = None
        requested_profiles: Dict[str, Optional[str]] = {}
        try:
            profile_registry = SemanticProfileRegistry.from_raw(
                settings.get_semantic_profiles_config(),
                settings.get_semantic_default_profile(),
                tool_version=settings.app_version,
            )
            requested_profiles = {
                profile_id: profile.compatibility_fingerprint
                for profile_id, profile in profile_registry.list().items()
            }
        except Exception as exc:
            logger.warning(
                "Failed to load semantic profile registry for hydration: %s", type(exc).__name__
            )

        selected_index_path = (
            discovery.get_local_index_path() if discovery.is_index_enabled() else None
        )
        profile_hydration_status = discovery.get_profile_hydration_status(
            requested_profiles=requested_profiles,
            index_path=selected_index_path,
            lexical_available=sqlite_store is not None,
            branch=os.getenv("MCP_INDEX_RECOVERY_BRANCH"),
            commit=os.getenv("MCP_INDEX_RECOVERY_COMMIT"),
        )
        logger.info(
            "Profile hydration fallback strategy: %s",
            profile_hydration_status.get("fallback_strategy", "lexical_only"),
        )

        logger.info("SQLite store initialized successfully")

        # Initialize plugin system with dynamic discovery
        logger.info("Initializing plugin system with dynamic discovery...")
        from .plugin_system.discovery import get_plugin_discovery
        from .plugin_system.loader import get_plugin_loader

        # Discover all available plugins
        plugin_discovery = get_plugin_discovery()
        discovered = plugin_discovery.discover_plugins()
        logger.info(f"Discovered {len(discovered)} plugins: {list(discovered.keys())}")

        # Initialize plugin loader
        plugin_loader = get_plugin_loader()

        # Load plugins based on configuration or auto-detected repo languages
        language_detection_status = None
        config_path = Path("plugins.yaml")
        enabled_languages = list(discovered.keys())

        if settings.mcp_auto_detect_languages:
            detection = detect_repository_languages(
                repo_path=Path("."),
                max_files=settings.mcp_language_detect_max_files,
                min_files=settings.mcp_language_detect_min_files,
            )
            language_detection_status = detection.to_dict()

            detected = set(detection.detected_languages)
            discovered_set = set(discovered.keys())

            alias_map = {
                "c_sharp": "csharp",
                "js": "javascript",
            }
            normalized_detected = {alias_map.get(lang, lang) for lang in detected}
            auto_enabled = sorted(normalized_detected.intersection(discovered_set))

            for helper_lang in ["plaintext", "markdown"]:
                if helper_lang in discovered_set and helper_lang not in auto_enabled:
                    auto_enabled.append(helper_lang)

            if auto_enabled:
                enabled_languages = auto_enabled
                logger.info(
                    "Auto-detected plugin allowlist (%d/%d discovered): %s",
                    len(enabled_languages),
                    len(discovered_set),
                    enabled_languages,
                )
            else:
                logger.warning(
                    "Language detection found no matching plugin keys; falling back to all discovered plugins"
                )
        elif config_path.exists():
            import yaml

            with open(config_path, "r") as f:
                plugin_config = yaml.safe_load(f)

            enabled_languages = plugin_config.get("enabled_languages", list(discovered.keys()))
            logger.info(
                "Auto-detect disabled; loading plugins from plugins.yaml: %s",
                enabled_languages,
            )
        else:
            logger.info(
                "Auto-detect disabled and no plugins.yaml found; loading all discovered plugins"
            )

        # Load plugins
        plugin_instances = []
        for language in enabled_languages:
            try:
                plugin = plugin_loader.load_plugin(language)
                if plugin:
                    plugin_instances.append(plugin)
                    logger.info(f"Successfully loaded plugin for {language}")
            except Exception as e:
                logger.error(f"Failed to load plugin for {language}: {type(e).__name__}")

        logger.info(f"Loaded {len(plugin_instances)} plugins")

        # Create plugin manager for compatibility
        plugin_manager = PluginManager(sqlite_store=sqlite_store)
        # Register loaded plugins with manager
        for plugin in plugin_instances:
            if hasattr(plugin, "get_language"):
                lang = plugin.get_language()
                plugin_manager._plugins[lang] = plugin

        logger.info(f"Loaded {len(plugin_instances)} active plugins")

        semantic_indexer_registry = None
        if settings.semantic_search_enabled and _local_repo_registry is not None:
            try:
                from .utils.semantic_indexer_registry import SemanticIndexerRegistry

                semantic_indexer_registry = SemanticIndexerRegistry(_local_repo_registry)
            except Exception as _sem_reg_err:
                logger.warning("Semantic registry unavailable: %s", type(_sem_reg_err).__name__)

        # Create a new EnhancedDispatcher instance with the loaded plugins
        logger.info("Creating dispatcher...")
        dispatcher = EnhancedDispatcher(
            semantic_search_enabled=settings.semantic_search_enabled,
            lazy_load=settings.mcp_fast_startup,
            semantic_indexer_registry=semantic_indexer_registry,
        )
        logger.info(
            "EnhancedDispatcher created with semantic search enabled: %s",
            settings.semantic_search_enabled,
        )

        # Initialize BM25 indexer
        logger.info("Initializing BM25 indexer...")
        bm25_indexer = BM25Indexer(sqlite_store)
        logger.info("BM25 indexer initialized successfully")

        # Initialize Fuzzy indexer
        logger.info("Initializing Fuzzy indexer...")
        fuzzy_indexer = FuzzyIndexer(sqlite_store)
        logger.info("Fuzzy indexer initialized successfully")

        semantic_runtime_enabled = settings.semantic_search_enabled
        semantic_setup_report = run_semantic_preflight(
            settings=settings,
            profile=settings.get_semantic_default_profile(),
            strict=settings.semantic_strict_mode,
        )

        if (
            semantic_runtime_enabled
            and settings.semantic_autostart_qdrant
            and not semantic_setup_report.qdrant.ok
        ):
            autostart_result = ensure_qdrant_running(settings)
            logger.info("Qdrant autostart result: %s", autostart_result.message)
            semantic_setup_report = run_semantic_preflight(
                settings=settings,
                profile=settings.get_semantic_default_profile(),
                strict=settings.semantic_strict_mode,
            )

        semantic_setup_status = semantic_setup_report.to_dict()
        if semantic_setup_report.overall_ready:
            logger.info("Semantic preflight passed")
        else:
            logger.warning("Semantic preflight warnings: %s", semantic_setup_report.warnings)

        if settings.semantic_strict_mode and not semantic_setup_report.overall_ready:
            raise RuntimeError(
                "Semantic strict mode is enabled and preflight failed: "
                + "; ".join(semantic_setup_report.warnings)
            )

        if not semantic_setup_report.overall_ready:
            semantic_runtime_enabled = False

        # Check if semantic indexer is available and enabled
        semantic_indexer = None
        if semantic_runtime_enabled:
            try:
                from .utils.semantic_indexer import SemanticIndexer

                # Use the canonical local file-backed semantic baseline by default.
                qdrant_path = os.getenv("QDRANT_PATH", "vector_index.qdrant")
                semantic_profile_id = settings.get_semantic_default_profile()
                semantic_profile = profile_registry.get(semantic_profile_id)
                if semantic_profile is None:
                    raise RuntimeError(
                        f"Semantic default profile '{semantic_profile_id}' is not available"
                    )
                semantic_indexer = SemanticIndexer(
                    collection=_get_profile_collection_name(
                        semantic_profile, settings.semantic_collection_name
                    ),
                    qdrant_path=qdrant_path,
                    profile_registry=profile_registry,
                    semantic_profile=semantic_profile_id,
                )
                logger.info(
                    "Semantic indexer initialized successfully with Qdrant at %s "
                    "(profile=%s, collection=%s)",
                    qdrant_path,
                    semantic_profile_id,
                    semantic_indexer.collection,
                )
            except ImportError:
                logger.warning("Semantic indexer not available (missing dependencies)")
            except Exception as e:
                logger.error(f"Failed to initialize semantic indexer: {type(e).__name__}")
        else:
            logger.info(
                "Semantic indexer disabled after preflight; lexical/bm25/fuzzy remain active"
            )

        # Initialize Hybrid Search
        logger.info("Initializing Hybrid Search...")
        hybrid_config = HybridSearchConfig(
            bm25_weight=float(os.getenv("HYBRID_BM25_WEIGHT", "0.5")),
            semantic_weight=float(os.getenv("HYBRID_SEMANTIC_WEIGHT", "0.3")),
            fuzzy_weight=float(os.getenv("HYBRID_FUZZY_WEIGHT", "0.2")),
            enable_bm25=True,
            enable_semantic=semantic_indexer is not None,
            enable_fuzzy=True,
            parallel_execution=True,
            cache_results=True,
        )
        hybrid_search = HybridSearch(
            storage=sqlite_store,
            bm25_indexer=bm25_indexer,
            semantic_indexer=semantic_indexer,
            fuzzy_indexer=fuzzy_indexer,
            config=hybrid_config,
        )
        logger.info(
            f"Hybrid Search initialized (BM25: {hybrid_config.enable_bm25}, Semantic: {hybrid_config.enable_semantic}, Fuzzy: {hybrid_config.enable_fuzzy})"
        )

        from .storage.git_index_manager import GitAwareIndexManager

        git_index_manager = GitAwareIndexManager(
            registry=_repo_registry,
            dispatcher=dispatcher,
            repo_resolver=repo_resolver,
            store_registry=_store_registry,
        )

        # Initialize multi-repo watcher + ref poller
        if settings.mcp_fast_startup:
            logger.info("MCP fast startup enabled: skipping watcher start")
            multi_watcher = None
            ref_poller = None
        else:
            logger.info("Starting MultiRepositoryWatcher and RefPoller...")
            starting_watcher = None
            starting_poller = None
            try:
                starting_watcher = MultiRepositoryWatcher(
                    registry=_repo_registry,
                    dispatcher=dispatcher,
                    index_manager=git_index_manager,
                    repo_resolver=repo_resolver,
                    store_registry=_store_registry,
                    semantic_indexer_registry=semantic_indexer_registry,
                    plugin_set_registry=getattr(dispatcher, "_plugin_set_registry", None),
                )
                starting_poller = RefPoller(
                    registry=_repo_registry,
                    git_index_manager=git_index_manager,
                    dispatcher=dispatcher,
                    repo_resolver=repo_resolver,
                )
                starting_watcher.start_watching_all()
                starting_poller.start()
            except Exception as _watcher_err:
                cleanup_errors = []
                for owner, stop in (
                    (starting_poller, "stop"),
                    (starting_watcher, "stop_watching_all"),
                ):
                    if owner is not None:
                        try:
                            getattr(owner, stop)()
                        except Exception as cleanup_error:
                            cleanup_errors.append(cleanup_error)
                if cleanup_errors:
                    # Retain owners for shutdown retry and fail startup rather than leak silently.
                    multi_watcher = starting_watcher
                    ref_poller = starting_poller
                    raise RuntimeError("Watcher startup cleanup failed") from cleanup_errors[0]
                logger.warning(
                    "MultiRepositoryWatcher failed to start: %s", type(_watcher_err).__name__
                )
                multi_watcher = None
                ref_poller = None
            else:
                multi_watcher = starting_watcher
                ref_poller = starting_poller
                logger.info("MultiRepositoryWatcher and RefPoller started")

        # Store in app.state for potential future use
        app.state.dispatcher = dispatcher
        app.state.sqlite_store = sqlite_store
        app.state.file_watcher = multi_watcher
        app.state.plugin_manager = plugin_manager
        app.state.auth_manager = auth_manager
        app.state.security_config = security_config
        app.state.cache_manager = cache_manager
        app.state.query_cache = query_cache
        app.state.metrics_collector = metrics_collector
        app.state.health_checker = health_checker
        app.state.business_metrics = business_metrics
        app.state.bm25_indexer = bm25_indexer
        app.state.hybrid_search = hybrid_search
        app.state.fuzzy_indexer = fuzzy_indexer
        app.state.git_index_manager = git_index_manager
        app.state.semantic_indexer = semantic_indexer
        app.state.profile_hydration = profile_hydration_status
        app.state.semantic_setup = semantic_setup_status
        app.state.language_detection = language_detection_status
        app.state.preflight = {
            "status": preflight_result.status,
            "checks": [check.__dict__ for check in preflight_result.checks],
        }

        # Update status to include search capabilities
        search_capabilities = []
        if bm25_indexer:
            search_capabilities.append("bm25")
        if fuzzy_indexer:
            search_capabilities.append("fuzzy")
        if semantic_indexer:
            search_capabilities.append("semantic")
        if hybrid_search:
            search_capabilities.append("hybrid")

        logger.info(f"Search capabilities: {', '.join(search_capabilities)}")

        # Register health checks for system components
        logger.info("Registering component health checks...")
        health_checker.register_health_check(
            "database", health_checker.create_database_health_check("code_index.db")
        )
        health_checker.register_health_check(
            "plugins", health_checker.create_plugin_health_check(plugin_manager)
        )

        # Update system metrics
        business_metrics.update_system_metrics(
            active_plugins=len(plugin_instances),
            indexed_files=0,  # Will be updated as files are indexed
            database_size=0,  # Will be updated periodically
            memory_usage=0,  # Will be updated by middleware
        )

        # Log loaded plugins with detailed status
        plugin_status = plugin_manager.get_detailed_plugin_status()
        for name, status in plugin_status.items():
            basic_info = status["basic_info"]
            runtime_info = status["runtime_info"]
            logger.info(
                f"Plugin '{name}': {runtime_info['state']} (v{basic_info['version']}, language: {basic_info['language']}, enabled: {runtime_info['enabled']})"
            )
            if runtime_info.get("error"):
                logger.warning(f"Plugin '{name}' has error: {runtime_info['error']}")

        logger.info(
            "MCP Server initialized successfully with dynamic plugin system, SQLite persistence, and file watcher"
        )
    except Exception as e:
        logger.error(f"Failed to initialize MCP Server: {type(e).__name__}")
        raise


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up resources on shutdown."""
    # These globals are only read, not assigned, so no 'global' declaration needed

    if multi_watcher:
        try:
            multi_watcher.stop_watching_all()
            logger.info("MultiRepositoryWatcher stopped successfully")
        except Exception as e:
            logger.error(f"Error stopping MultiRepositoryWatcher: {type(e).__name__}")

    if ref_poller:
        try:
            ref_poller.stop()
            logger.info("RefPoller stopped successfully")
        except Exception as e:
            logger.error(f"Error stopping RefPoller: {type(e).__name__}")

    if dispatcher:
        try:
            dispatcher.shutdown()
            logger.info("Dispatcher plugin workers stopped successfully")
        except Exception as e:
            logger.error(f"Error stopping dispatcher plugin workers: {type(e).__name__}")

    if plugin_manager:
        try:
            shutdown_result = plugin_manager.shutdown_safe()
            if shutdown_result.success:
                logger.info("Plugin manager shutdown successfully")
            else:
                logger.error(f"Plugin manager shutdown failed: {shutdown_result.error.message}")
                logger.error(f"Shutdown error details: {shutdown_result.error.details}")
        except Exception as e:
            logger.error(f"Error shutting down plugin manager: {type(e).__name__}")

    if cache_manager:
        try:
            await cache_manager.shutdown()
            logger.info("Cache manager shutdown successfully")
        except Exception as e:
            logger.error(f"Error shutting down cache manager: {type(e).__name__}")


# Authentication endpoints


@app.post("/api/v1/auth/login")
async def login(credentials: AuthCredentials) -> Dict[str, Any]:
    """User login endpoint."""
    if auth_manager is None:
        raise HTTPException(503, "Authentication service not ready")

    try:
        user = await auth_manager.authenticate_user(credentials)
        if not user:
            raise HTTPException(401, "Invalid credentials")

        access_token = await auth_manager.create_access_token(user)
        refresh_token = await auth_manager.create_refresh_token(user)

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": security_config.access_token_expire_minutes * 60,
            "user": {
                "id": user.id,
                "username": user.username,
                "role": user.role.value,
                "permissions": [p.value for p in user.permissions],
            },
        }
    except Exception as e:
        logger.error(f"Login failed for user '{credentials.username}': {type(e).__name__}")
        raise HTTPException(401, "Authentication failed")


@app.post("/api/v1/auth/refresh")
async def refresh_token(refresh_token: str) -> Dict[str, Any]:
    """Refresh access token."""
    if auth_manager is None:
        raise HTTPException(503, "Authentication service not ready")

    try:
        new_access_token = await auth_manager.refresh_access_token(refresh_token)
        if not new_access_token:
            raise HTTPException(401, "Invalid refresh token")

        return {
            "access_token": new_access_token,
            "token_type": "bearer",
            "expires_in": security_config.access_token_expire_minutes * 60,
        }
    except Exception as e:
        logger.error(f"Token refresh failed: {type(e).__name__}")
        raise HTTPException(401, "Token refresh failed")


@app.post("/api/v1/auth/logout")
async def logout(
    refresh_token: Optional[str] = None,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, str]:
    """User logout endpoint."""
    if auth_manager is None:
        raise HTTPException(503, "Authentication service not ready")

    try:
        if refresh_token:
            await auth_manager.revoke_refresh_token(refresh_token)

        await auth_manager._log_security_event(
            "user_logout", user_id=current_user.id, username=current_user.username
        )

        return {"message": "Successfully logged out"}
    except Exception as e:
        logger.error(f"Logout failed: {type(e).__name__}")
        raise HTTPException(500, "Logout failed")


@app.get("/api/v1/auth/me")
async def get_current_user_info(
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Get current user information."""
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "role": current_user.role.value,
        "permissions": [p.value for p in current_user.permissions],
        "is_active": current_user.is_active,
        "created_at": current_user.created_at.isoformat(),
        "last_login": (current_user.last_login.isoformat() if current_user.last_login else None),
    }


@app.post("/api/v1/auth/register")
async def register(
    credentials: AuthCredentials,
    email: Optional[str] = None,
    admin_user: User = Depends(require_role(UserRole.ADMIN)),
) -> Dict[str, Any]:
    """Register new user (admin only)."""
    if auth_manager is None:
        raise HTTPException(503, "Authentication service not ready")

    try:
        user = await auth_manager.create_user(
            username=credentials.username,
            password=credentials.password,
            email=email,
            role=UserRole.USER,
        )

        return {
            "message": "User created successfully",
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user.role.value,
            },
        }
    except Exception as e:
        logger.error(f"User registration failed: {type(e).__name__}")
        raise HTTPException(400, "User registration failed")


# Security management endpoints


@app.get("/api/v1/security/events")
async def get_security_events(
    limit: int = 100, admin_user: User = Depends(require_role(UserRole.ADMIN))
) -> Dict[str, Any]:
    """Get security events (admin only)."""
    if auth_manager is None:
        raise HTTPException(503, "Authentication service not ready")

    try:
        events = await auth_manager.get_security_events(limit)
        return {
            "events": [
                {
                    "id": event.id,
                    "event_type": event.event_type,
                    "user_id": event.user_id,
                    "username": event.username,
                    "ip_address": event.ip_address,
                    "timestamp": event.timestamp.isoformat(),
                    "details": event.details,
                    "severity": event.severity,
                }
                for event in events
            ]
        }
    except Exception as e:
        logger.error(f"Failed to get security events: {type(e).__name__}")
        raise HTTPException(500, "Failed to retrieve security events")


# Health check endpoints (public)
@app.get("/health")
async def health_check() -> Dict[str, Any]:
    """Basic health check endpoint."""
    return {"status": "healthy", "service": "mcp-server", "timestamp": time.time()}


@app.get("/health/detailed")
async def detailed_health_check() -> Dict[str, Any]:
    """Detailed health check endpoint."""
    try:
        overall_health = await health_checker.get_overall_health()
        component_results = await health_checker.check_all_components()

        return {
            "status": overall_health.status.value,
            "message": overall_health.message,
            "timestamp": overall_health.timestamp,
            "details": overall_health.details,
            "components": [
                {
                    "component": result.component,
                    "status": result.status.value,
                    "message": result.message,
                    "details": result.details,
                }
                for result in component_results
            ],
        }
    except Exception as e:
        logger.error(f"Health check failed: {type(e).__name__}")
        return {
            "status": "unhealthy",
            "message": "Health check failed",
            "timestamp": time.time(),
        }


@app.get("/health/{component}")
async def component_health_check(component: str) -> Dict[str, Any]:
    """Health check for a specific component."""
    try:
        result = await health_checker.check_component(component)
        return {
            "component": result.component,
            "status": result.status.value,
            "message": result.message,
            "timestamp": result.timestamp,
            "details": result.details,
        }
    except Exception as e:
        logger.error(f"Component health check failed for {component}: {type(e).__name__}")
        raise HTTPException(500, "Health check failed")


@app.get("/ready")
async def readiness_probe() -> Dict[str, Any]:
    """Readiness probe: 200 when dispatcher, sqlite_store, and _repo_registry are all initialised."""
    from fastapi.responses import JSONResponse as _JSONResponse

    from .health.probes import HealthView

    hv = HealthView(
        dispatcher=dispatcher,
        sqlite_store=sqlite_store,
        registry=_repo_registry,
        startup_time=getattr(app.state, "startup_time", None),
    )
    snap = hv.snapshot()
    code = 200 if (snap["sqlite"] and snap["registry"] and snap["dispatcher"]) else 503
    return _JSONResponse(content=snap, status_code=code)


@app.get("/liveness")
async def liveness_probe() -> Dict[str, Any]:
    """Liveness probe: returns 200 if the asyncio event loop is responsive."""
    import asyncio

    from fastapi.responses import JSONResponse as _JSONResponse

    await asyncio.sleep(0)
    return _JSONResponse(content={"status": "ok"}, status_code=200)


# Metrics endpoints
@app.get("/metrics", dependencies=[Depends(require_auth("metrics"))])
def get_prometheus_metrics() -> Response:
    """Prometheus metrics endpoint."""
    try:
        prometheus_exporter = get_prometheus_exporter()

        # Update build info
        prometheus_exporter.set_build_info(
            version="1.0.0",
            commit=os.getenv("GIT_COMMIT", "unknown"),
            build_time=os.getenv("BUILD_TIME", "unknown"),
        )

        # Update system metrics
        import psutil

        process = psutil.Process()
        memory_info = process.memory_info()
        prometheus_exporter.set_memory_usage(memory_info.rss, memory_info.vms)
        prometheus_exporter.set_cpu_usage(process.cpu_percent())
        prometheus_exporter.set_active_threads(threading.active_count())

        # Update plugin metrics
        if plugin_loader:
            _ = plugin_loader.get_statistics()
            for lang, plugin in plugin_loader.get_active_plugins().items():
                prometheus_exporter.plugin_status.labels(
                    plugin=plugin.__class__.__name__, language=lang
                ).set(1)

        # Update watcher metrics (placeholder — MultiRepositoryWatcher has no get_watched_count yet)
        if multi_watcher:
            pass

        # Generate metrics
        metrics = prometheus_exporter.generate_metrics()
        return Response(content=metrics, media_type=prometheus_exporter.get_content_type())
    except Exception as e:
        logger.error(f"Failed to generate Prometheus metrics: {type(e).__name__}")
        raise HTTPException(500, "Failed to generate metrics")


@app.get("/metrics/json")
def get_metrics_json(
    current_user: User = Depends(require_permission(Permission.READ)),
) -> Dict[str, Any]:
    """JSON metrics endpoint for programmatic access."""
    try:
        families = metrics_collector.get_metric_families()
        stats = metrics_collector.get_stats()

        return {
            "timestamp": time.time(),
            "collector_stats": stats,
            "metric_families": families,
        }
    except Exception as e:
        logger.error(f"Failed to get JSON metrics: {type(e).__name__}")
        raise HTTPException(500, "Failed to get metrics")


def _require_current_generation(ctx: RepoContext) -> None:
    if isinstance(repo_resolver, RepoResolver) and ctx.repo_id != _FALLBACK_REPO_ID:
        if not repo_resolver.is_current(ctx):
            raise HTTPException(
                503,
                detail={
                    "code": "index_unavailable",
                    "safe_fallback": "native_search",
                    "remediation": "Repository generation changed; re-check readiness and retry.",
                },
            )


@app.get("/symbol", response_model=SymbolDef | None)
async def symbol(
    request: Request,
    symbol: str,
    current_user: User = Depends(require_permission(Permission.READ)),
    repository: Optional[str] = None,
):
    if dispatcher is None:
        logger.error("Symbol lookup attempted but dispatcher not ready")
        raise HTTPException(503, "Dispatcher not ready")
    ctx = get_repo_ctx(request)

    start_time = time.time()
    try:
        logger.debug("Looking up symbol")

        # Try cache first if query cache is available
        cached_result = None
        if query_cache and query_cache.config.enabled:
            cached_result = await query_cache.get_cached_result(
                QueryType.SYMBOL_LOOKUP,
                symbol=symbol,
                repo_id=ctx.repo_id,
                generation=ctx.generation_key,
            )

        if cached_result is not None:
            _require_current_generation(ctx)
            logger.debug("Found cached symbol")
            duration = time.time() - start_time
            business_metrics.record_search_performed(
                query=symbol, semantic=False, results_count=1, duration=duration
            )
            return cached_result

        # Record symbol lookup metrics
        with metrics_collector.time_function("symbol_lookup"):
            result = dispatcher.lookup(ctx, symbol)
        _require_current_generation(ctx)

        # Cache the result if available
        if query_cache and query_cache.config.enabled and result:
            await query_cache.cache_result(
                QueryType.SYMBOL_LOOKUP,
                result,
                symbol=symbol,
                repo_id=ctx.repo_id,
                generation=ctx.generation_key,
            )

        # Record business metrics
        duration = time.time() - start_time
        business_metrics.record_search_performed(
            query=symbol,
            semantic=False,
            results_count=1 if result else 0,
            duration=duration,
        )

        if result:
            logger.debug("Found symbol")
        else:
            logger.debug("Symbol not found")
        _require_current_generation(ctx)
        return result
    except HTTPException:
        raise
    except Exception as e:
        _require_current_generation(ctx)
        duration = time.time() - start_time
        business_metrics.record_search_performed(
            query=symbol, semantic=False, results_count=0, duration=duration
        )
        logger.error("Symbol lookup failed (%s)", type(e).__name__)
        raise HTTPException(500, "Internal error during symbol lookup")


@app.get("/search", response_model=list[dict[str, Any]])
async def search(
    request: Request,
    q: str,
    semantic: bool = False,
    limit: int = 20,
    mode: str = "auto",  # "auto", "hybrid", "bm25", "semantic", "fuzzy", "classic"
    language: Optional[str] = None,
    file_filter: Optional[str] = None,
    source_type: Optional[str] = None,
    friction_categories: Optional[str] = None,
    history_labels: Optional[str] = None,
    history_repos: Optional[str] = None,
    include_source_metadata: bool = False,
    current_user: User = Depends(require_permission(Permission.READ)),
    repository: Optional[str] = None,
):
    """Search with support for multiple modes including hybrid search.

    Args:
        q: Search query
        semantic: Whether to use semantic search (for backward compatibility)
        limit: Maximum number of results
        mode: Search mode - "auto" (default), "hybrid", "bm25", "semantic", "fuzzy", or "classic"
        language: Filter by programming language
        file_filter: Filter by file path pattern
        current_user: Authenticated user
    """
    if dispatcher is None and mode == "classic":
        logger.error("Search attempted but dispatcher not ready")
        raise HTTPException(503, "Dispatcher not ready")

    try:
        options = build_search_options(
            query=q,
            repository=request.query_params.get("repository"),
            semantic=semantic,
            limit=limit,
            source_type=source_type,
            friction_categories=friction_categories,
            history_labels=history_labels,
            history_repos=history_repos,
            include_source_metadata=include_source_metadata,
        )
    except ClientValidationError as exc:
        detail: dict[str, Any] = {"error": exc.error, "details": exc.details}
        if exc.allowed_categories:
            detail["allowed_categories"] = exc.allowed_categories
        raise HTTPException(400, detail) from exc
    except ValueError as exc:
        raise HTTPException(
            400, {"error": "Invalid search source filters", "details": str(exc)}
        ) from exc

    ctx = get_repo_ctx(request)
    repo_bm25: Optional[BM25Indexer] = None
    repo_fuzzy: Optional[FuzzyIndexer] = None
    repo_hybrid: Optional[HybridSearch] = None
    if mode in {"hybrid", "bm25", "fuzzy"} or ctx.repo_id == _FALLBACK_REPO_ID:
        repo_bm25, repo_fuzzy, repo_hybrid = _search_backends_for_repo(ctx)
    explicit_repository = request.query_params.get("repository")
    service_repo_resolver = repo_resolver
    if explicit_repository is None and ctx.repo_id == _FALLBACK_REPO_ID:
        service_repo_resolver = None
    start_time = time.time()
    try:
        # Determine effective search mode
        effective_mode = mode
        filtered_search_requested = bool(
            options.source_type
            or options.friction_categories
            or options.history_labels
            or options.history_repos
            or options.include_source_metadata
        )
        if mode == "auto":
            if filtered_search_requested and dispatcher and hasattr(dispatcher, "search"):
                effective_mode = "semantic" if semantic else "classic"
            elif semantic and dispatcher and hasattr(dispatcher, "search"):
                effective_mode = "semantic"
            elif ctx.repo_id != _FALLBACK_REPO_ID:
                effective_mode = "classic"
            # Non-semantic requests use hybrid if available, otherwise fall back.
            elif repo_hybrid is not None:
                effective_mode = "hybrid"
            else:
                effective_mode = "bm25" if repo_bm25 else "classic"

        logger.debug(
            f"Searching (query_chars={len(q)}, mode={effective_mode}, limit={limit}, language={language})"
        )

        # Build filters
        filters = {}
        if language:
            filters["language"] = language
        if file_filter:
            filters["file_filter"] = file_filter

        cache_params = {
            "repo_id": ctx.repo_id,
            "generation": ctx.generation_key,
            "mode": effective_mode,
            "q": q,
            "semantic": effective_mode == "semantic",
            "limit": limit,
            "language": language,
            "file_filter": file_filter,
            "source_type": options.source_type.value if options.source_type else None,
            "friction_categories": options.friction_categories,
            "history_labels": options.history_labels,
            "history_repos": options.history_repos,
            "include_source_metadata": options.include_source_metadata,
        }
        if effective_mode in {"semantic", "hybrid"}:
            settings = get_settings()
            profile_registry = SemanticProfileRegistry.from_raw(
                settings.get_semantic_profiles_config(),
                settings.get_semantic_default_profile(),
                tool_version=settings.app_version,
            )
            cache_params["profile"] = hashlib.sha256(
                json.dumps(profile_registry.to_dict(), sort_keys=True, default=str).encode()
            ).hexdigest()

        cached_results = None
        if query_cache and query_cache.config.enabled:
            query_type = (
                QueryType.SEMANTIC_SEARCH if effective_mode == "semantic" else QueryType.SEARCH
            )
            cached_results = await query_cache.get_cached_result(query_type, **cache_params)

        if cached_results is not None:
            _require_current_generation(ctx)
            cached_results = [
                r for r in (_normalize_search_result(x) for x in cached_results) if r is not None
            ]
            logger.debug("Found cached search results (count=%d)", len(cached_results))
            duration = time.time() - start_time
            business_metrics.record_search_performed(
                query=q,
                semantic=(effective_mode == "semantic"),
                results_count=len(cached_results),
                duration=duration,
            )
            return cached_results

        # Perform search based on mode
        results = []

        if effective_mode == "hybrid" and repo_hybrid:
            # Use hybrid search
            with metrics_collector.time_function("search", labels={"mode": "hybrid"}):
                hybrid_results = await repo_hybrid.search(query=q, filters=filters, limit=limit)
                results = [
                    r
                    for r in (_normalize_search_result(x) for x in hybrid_results)
                    if r is not None
                ]

        elif effective_mode == "bm25" and repo_bm25:
            # Direct BM25 search
            with metrics_collector.time_function("search", labels={"mode": "bm25"}):
                bm25_results = repo_bm25.search(q, limit=limit, **filters)
                if not bm25_results:
                    bm25_results = ctx.sqlite_store.search_bm25(q, table="fts_code", limit=limit)
                results = [
                    r for r in (_normalize_search_result(x) for x in bm25_results) if r is not None
                ]

        elif effective_mode == "fuzzy" and repo_fuzzy:
            # Direct fuzzy search
            with metrics_collector.time_function("search", labels={"mode": "fuzzy"}):
                if hasattr(repo_fuzzy, "search_fuzzy"):
                    fuzzy_results = repo_fuzzy.search_fuzzy(q, max_results=limit)
                else:
                    fuzzy_results = repo_fuzzy.search(q, limit=limit)
                results = [
                    r for r in (_normalize_search_result(x) for x in fuzzy_results) if r is not None
                ]

        elif effective_mode == "semantic":
            # Use classic dispatcher with semantic=True
            if dispatcher:
                with metrics_collector.time_function("search", labels={"mode": "semantic"}):
                    result = execute_search_service(
                        dispatcher=dispatcher,
                        repo_resolver=service_repo_resolver,
                        options=options,
                        workspace_root=ctx.workspace_root,
                        ctx=ctx,
                    )
                    if result.index_unavailable is not None:
                        raise HTTPException(
                            503,
                            detail={
                                "error": "Index unavailable",
                                "code": result.index_unavailable.code,
                                "safe_fallback": result.index_unavailable.safe_fallback,
                                "readiness": result.index_unavailable.readiness,
                                "message": result.index_unavailable.message,
                                "remediation": result.index_unavailable.remediation,
                            },
                        )
                    if result.code == "semantic_not_ready":
                        raise HTTPException(
                            503,
                            detail={
                                "error": "Semantic search not ready",
                                "code": result.code,
                                "semantic_readiness": result.semantic_readiness,
                                "message": result.message,
                            },
                        )
                    if result.code == "semantic_search_failed":
                        raise HTTPException(
                            500,
                            detail={
                                "error": "Semantic search failed",
                                "code": result.code,
                                "semantic_readiness": result.semantic_readiness,
                                "message": result.message,
                            },
                        )
                    results = search_result_for_gateway(result)
            else:
                raise HTTPException(
                    503,
                    detail={
                        "error": "Semantic search not available",
                        "reason": "Semantic preflight failed (embedding endpoint or credentials unavailable)",
                        "setup": {
                            "recommended": "Run: python scripts/cli/mcp_cli.py setup semantic",
                            "method_1_mcp_json": [
                                "Configure in .mcp.json (recommended for Claude Code):",
                                "{",
                                '  "mcpServers": {',
                                '    "code-index-mcp": {',
                                '      "command": "uvicorn",',
                                '      "args": ["mcp_server.gateway:app"],',
                                '      "env": {',
                                '        "VOYAGE_API_KEY": "your-key-here",',
                                '        "OPENAI_API_BASE": "http://localhost:8001/v1",',
                                '        "SEMANTIC_SEARCH_ENABLED": "true"',
                                "      }",
                                "    }",
                                "  }",
                                "}",
                            ],
                            "method_2_cli": [
                                "Or use Claude Code CLI:",
                                "claude mcp add code-index-mcp -e VOYAGE_API_KEY=your_key -e SEMANTIC_SEARCH_ENABLED=true -- uvicorn mcp_server.gateway:app",
                            ],
                            "method_3_env": [
                                "Or set environment variables:",
                                "export VOYAGE_API_KEY=your_key",
                                "export SEMANTIC_SEARCH_ENABLED=true",
                            ],
                            "method_4_dotenv": [
                                "Or add to .env file:",
                                "VOYAGE_API_KEY=your_key",
                                "SEMANTIC_SEARCH_ENABLED=true",
                            ],
                            "get_api_key": "Get your API key from: https://www.voyageai.com/",
                            "alternative": "Use mode='hybrid' or mode='bm25' for keyword-based search",
                        },
                    },
                )

        else:
            # Classic search through dispatcher
            if dispatcher:
                with metrics_collector.time_function("search", labels={"mode": "classic"}):
                    result = execute_search_service(
                        dispatcher=dispatcher,
                        repo_resolver=service_repo_resolver,
                        options=options,
                        workspace_root=ctx.workspace_root,
                        ctx=ctx,
                    )
                    if result.index_unavailable is not None:
                        raise HTTPException(
                            503,
                            detail={
                                "error": "Index unavailable",
                                "code": result.index_unavailable.code,
                                "safe_fallback": result.index_unavailable.safe_fallback,
                                "readiness": result.index_unavailable.readiness,
                                "message": result.index_unavailable.message,
                                "remediation": result.index_unavailable.remediation,
                            },
                        )
                    results = search_result_for_gateway(result)
            else:
                raise HTTPException(503, "Classic search not available")

        # Cache the results if available
        results = [r for r in (_normalize_search_result(x) for x in results) if r is not None]
        _require_current_generation(ctx)
        if query_cache and query_cache.config.enabled and results:
            query_type = (
                QueryType.SEMANTIC_SEARCH if effective_mode == "semantic" else QueryType.SEARCH
            )
            await query_cache.cache_result(
                query_type,
                results,
                **cache_params,
            )

        # Record business metrics
        duration = time.time() - start_time
        business_metrics.record_search_performed(
            query=q,
            semantic=(effective_mode == "semantic"),
            results_count=len(results),
            duration=duration,
        )

        logger.debug(f"Search returned {len(results)} results using {effective_mode} mode")
        _require_current_generation(ctx)
        return results
    except HTTPException:
        raise
    except Exception as e:
        duration = time.time() - start_time
        business_metrics.record_search_performed(
            query=q, semantic=semantic, results_count=0, duration=duration
        )
        logger.error("Search failed (%s)", type(e).__name__)
        raise HTTPException(500, "Internal error during search")


@app.get("/search/capabilities")
async def get_search_capabilities(
    current_user: User = Depends(require_permission(Permission.READ)),
) -> Dict[str, Any]:
    """Get available search capabilities and configuration guidance."""
    voyage_key = os.environ.get("VOYAGE_API_KEY")
    openai_base = os.environ.get("OPENAI_API_BASE")
    semantic_enabled = os.environ.get("SEMANTIC_SEARCH_ENABLED", "false").lower() == "true"

    return {
        "available_modes": {
            "bm25": bm25_indexer is not None,
            "fuzzy": fuzzy_indexer is not None,
            "semantic": semantic_indexer is not None,
            "hybrid": hybrid_search is not None,
            "classic": dispatcher is not None,
        },
        "semantic_config": {
            "enabled": semantic_indexer is not None,
            "api_key_configured": bool(voyage_key),
            "openai_api_base": openai_base,
            "semantic_enabled_flag": semantic_enabled,
            "status": "ready" if semantic_indexer else "not_configured",
            "setup": semantic_setup_status,
        },
        "language_detection": language_detection_status,
        "configuration_guide": {
            "mcp_json_example": {
                "description": "Add to .mcp.json for Claude Code (recommended)",
                "config": {
                    "mcpServers": {
                        "code-index-mcp": {
                            "command": "uvicorn",
                            "args": ["mcp_server.gateway:app"],
                            "env": {
                                "VOYAGE_API_KEY": "your-key-here",
                                "OPENAI_API_BASE": "http://localhost:8001/v1",
                                "SEMANTIC_SEARCH_ENABLED": "true",
                            },
                        }
                    }
                },
            },
            "cli_command": "claude mcp add code-index-mcp -e VOYAGE_API_KEY=key -e SEMANTIC_SEARCH_ENABLED=true -- uvicorn mcp_server.gateway:app",
            "env_file": "Add to .env: VOYAGE_API_KEY=key and SEMANTIC_SEARCH_ENABLED=true",
            "get_api_key": "https://www.voyageai.com/",
        },
    }


@app.get("/status")
async def get_status(
    request: Request,
    current_user: User = Depends(require_permission(Permission.READ)),
) -> Dict[str, Any]:
    """Returns server status including plugin information and statistics."""
    if dispatcher is None:
        return {
            "status": "error",
            "plugins": 0,
            "indexed_files": {"total": 0, "by_language": {}},
            "profile_hydration": profile_hydration_status,
            "version": "0.1.0",
            "message": "Dispatcher not initialized",
        }

    explicit_selector = request.headers.get("X-Repo-Id") or request.query_params.get("repository")
    try:
        ctx: Optional[RepoContext] = get_repo_ctx(request)
    except HTTPException:
        if explicit_selector is not None:
            raise
        ctx = None
    cache_repo_id = ctx.repo_id if ctx is not None else "aggregate"
    try:
        # Try cache first if query cache is available
        cached_status = None
        if query_cache and query_cache.config.enabled:
            cached_status = await query_cache.get_cached_result(
                QueryType.PROJECT_STATUS, repo_id=cache_repo_id
            )

        if cached_status is not None:
            return cached_status

        # Get plugin count via public Protocol method
        loaded_plugins = dispatcher.plugins()
        plugin_count = len(loaded_plugins)

        # Get indexed files statistics
        indexed_stats = {"total": 0, "by_language": {}}
        if ctx is not None and hasattr(dispatcher, "get_statistics"):
            indexed_stats = dispatcher.get_statistics(ctx)
        else:
            # Calculate basic statistics from plugins
            for plugin in loaded_plugins:
                if hasattr(plugin, "get_indexed_count"):
                    count = plugin.get_indexed_count()
                    indexed_stats["total"] += count
                    lang = getattr(plugin, "language", getattr(plugin, "lang", "unknown"))
                    indexed_stats["by_language"][lang] = count

        # Add database statistics if available
        db_stats = {}
        if ctx is not None and ctx.sqlite_store:
            db_stats = ctx.sqlite_store.get_statistics()

        # Add cache statistics if available
        cache_stats = {}
        if cache_manager:
            try:
                cache_metrics = await cache_manager.get_metrics()
                cache_stats = {
                    "hit_rate": cache_metrics.hit_rate,
                    "entries": cache_metrics.entries_count,
                    "memory_usage_mb": cache_metrics.memory_usage_mb,
                }
            except Exception as e:
                logger.warning(f"Failed to get cache stats: {type(e).__name__}")

        from mcp_server.health.repo_status import build_health_row as _build_health_row

        _repositories = []
        if _repo_registry is not None:
            try:
                _repositories = []
                for info in _repo_registry.get_all_repositories().values():
                    _features = None
                    if repo_resolver is not None and hasattr(
                        dispatcher, "get_runtime_feature_status"
                    ):
                        try:
                            _readiness = repo_resolver.classify(info.path)
                            if isinstance(_readiness, RepositoryReadiness) and _readiness.ready:
                                _ctx = (
                                    repo_resolver.resolve_ready(info.path)
                                    if isinstance(repo_resolver, RepoResolver)
                                    else repo_resolver.resolve(info.path)
                                )
                                if _ctx is not None:
                                    _features = dispatcher.get_runtime_feature_status(_ctx)
                        except Exception:
                            _features = None
                    _repositories.append(_build_health_row(info, features=_features))
            except Exception as _repo_err:
                logger.warning(
                    "Failed to build repository health rows: %s", type(_repo_err).__name__
                )

        status_data = {
            "status": "operational",
            "plugins": plugin_count,
            "indexed_files": indexed_stats,
            "database": db_stats,
            "cache": cache_stats,
            "search_capabilities": [],
            "profile_hydration": profile_hydration_status,
            "semantic_setup": semantic_setup_status,
            "language_detection": language_detection_status,
            "version": "0.1.0",
            "repositories": _repositories,
        }

        # Add search capabilities
        if bm25_indexer:
            status_data["search_capabilities"].append("bm25")
        if fuzzy_indexer:
            status_data["search_capabilities"].append("fuzzy")
        if hasattr(app.state, "semantic_indexer") and app.state.semantic_indexer:
            status_data["search_capabilities"].append("semantic")
        if hybrid_search:
            status_data["search_capabilities"].append("hybrid")

        # Cache the status
        if query_cache and query_cache.config.enabled:
            await query_cache.cache_result(
                QueryType.PROJECT_STATUS, status_data, repo_id=cache_repo_id
            )

        return status_data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting server status: {type(e).__name__}")
        return {
            "status": "error",
            "plugins": 0,
            "indexed_files": {"total": 0, "by_language": {}},
            "profile_hydration": profile_hydration_status,
            "version": "0.1.0",
            "message": "Failed to get server status",
        }


@app.get("/plugins")
def plugins(
    current_user: User = Depends(require_permission(Permission.READ)),
) -> List[Dict[str, Any]]:
    """Returns list of loaded plugins with their information."""
    if plugin_manager is None:
        logger.error("Plugin list requested but plugin manager not ready")
        raise HTTPException(503, "Plugin manager not ready")

    try:
        plugin_list = []
        plugin_infos = plugin_manager._registry.list_plugins()
        plugin_status = plugin_manager.get_plugin_status()

        for info in plugin_infos:
            plugin_state = plugin_status.get(info.name, {})
            plugin_data = {
                "name": info.name,
                "version": info.version,
                "description": info.description,
                "author": info.author,
                "language": info.language,
                "file_extensions": info.file_extensions,
                "state": plugin_state.get("state", "unknown"),
                "enabled": plugin_state.get("enabled", False),
            }
            plugin_list.append(plugin_data)

        logger.debug(f"Returning {len(plugin_list)} plugins")
        return plugin_list
    except Exception as e:
        logger.error(f"Error getting plugin list: {type(e).__name__}")
        raise HTTPException(500, "Internal error getting plugins")


@app.post("/reindex")
async def reindex(
    request: Request,
    path: Optional[str] = None,
    current_user: User = Depends(require_permission(Permission.EXECUTE)),
    repository: Optional[str] = None,
) -> Dict[str, Any]:
    """Triggers manual reindexing of files.

    Args:
        path: Optional specific directory path to reindex. If not provided,
              reindexes all configured paths.

    Returns:
        Task status information.
    """
    if dispatcher is None:
        logger.error("Reindex requested but dispatcher not ready")
        raise HTTPException(503, "Dispatcher not ready")

    selector = request.headers.get("X-Repo-Id") or request.query_params.get("repository")
    readiness = None
    if repo_resolver is not None:
        target = selector if selector is not None else str(Path.cwd())
        try:
            readiness = repo_resolver.classify(target)
        except Exception as exc:
            raise HTTPException(503, detail=_repository_unavailable_detail(str(target))) from exc

    if readiness is not None and not readiness.ready:
        if path and readiness.state in _RECOVERABLE_REINDEX_STATES:
            raise HTTPException(
                409,
                detail={
                    "error": "Full repository rebuild required",
                    "code": "full_rebuild_required",
                    "readiness": readiness.to_dict(),
                    "mutation_performed": False,
                },
            )
        if not path and readiness.state in _RECOVERABLE_REINDEX_STATES:
            if git_index_manager is None or readiness.repository_id is None:
                raise HTTPException(503, detail="Staged repository reindex is unavailable")
            sync_result = await anyio.to_thread.run_sync(
                git_index_manager.rebuild_repository_index,
                readiness.repository_id,
                abandon_on_cancel=False,
            )
            if sync_result.action != "full_index":
                raise HTTPException(
                    409,
                    detail={
                        "error": "Reindex failed",
                        "code": sync_result.code or "reindex_failed",
                        "mutation_performed": False,
                    },
                )
            return {
                "status": "completed",
                "mode": "staged_full",
                "indexed_files": sync_result.files_processed,
                "commit": sync_result.commit,
                "readiness": sync_result.readiness,
            }
        raise HTTPException(503, detail=_repository_unavailable_detail(str(target)))

    ctx = get_repo_ctx(request)
    try:
        logger.info(f"Manual reindex requested for path: {path or 'all'}")
        if path:
            workspace_root = ctx.workspace_root.expanduser().resolve(strict=True)
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = workspace_root / candidate
            try:
                target_path = candidate.resolve(strict=True)
            except FileNotFoundError as exc:
                raise HTTPException(404, f"Path not found: {path}") from exc

            try:
                target_path.relative_to(workspace_root)
            except ValueError as exc:
                raise HTTPException(
                    403,
                    detail={
                        "error": "Reindex path is outside the selected repository",
                        "code": "path_outside_selected_repository",
                        "repository_id": ctx.repo_id,
                    },
                ) from exc

            guard = _get_path_guard()
            if guard is not None:
                try:
                    guard.normalize_and_check(target_path)
                except PathTraversalError as exc:
                    raise HTTPException(
                        403,
                        detail={
                            "error": "Reindex path is outside MCP_ALLOWED_ROOTS",
                            "code": "path_outside_allowed_roots",
                        },
                    ) from exc

            def index_target(current):
                source_target = target_path
                if current.staging:
                    source_target = current.workspace_root / target_path.relative_to(workspace_root)
                if target_path.is_file():
                    result = dispatcher.index_file(current, source_target)
                    failed = getattr(result, "status", None) in {
                        "error",
                        "not_found",
                        "skipped_toctou",
                    }
                    return {"indexed_files": int(not failed), "failed_files": int(failed)}
                return dispatcher.index_directory(current, source_target, recursive=True)

            stats = await anyio.to_thread.run_sync(
                run_repository_mutation, repo_resolver, ctx, index_target, abandon_on_cancel=False
            )
            if stats["failed_files"]:
                raise RuntimeError("Reindex did not complete cleanly")
            indexed_count = stats["indexed_files"]

            logger.info(f"Successfully reindexed {indexed_count} files in {path}")
            return {
                "status": "completed",
                "message": f"Reindexed {indexed_count} files in {path}",
            }
        else:
            if ctx.repo_id == _FALLBACK_REPO_ID or git_index_manager is None:
                raise HTTPException(
                    503,
                    detail={
                        "error": "Staged repository reindex is unavailable",
                        "code": "staged_reindex_unavailable",
                        "repository_id": ctx.repo_id,
                    },
                )
            sync_result = await anyio.to_thread.run_sync(
                git_index_manager.rebuild_repository_index, ctx.repo_id, abandon_on_cancel=False
            )
            if sync_result.action != "full_index":
                raise HTTPException(
                    409,
                    detail={
                        "error": "Staged repository reindex was refused",
                        "code": sync_result.code or "reindex_failed",
                        "readiness": sync_result.readiness,
                        "message": sync_result.error,
                    },
                )
            return {
                "status": "completed",
                "mode": "staged_full",
                "message": f"Reindexed {sync_result.files_processed} files",
                "indexed_files": sync_result.files_processed,
                "commit": sync_result.commit,
                "recovery": sync_result.readiness,
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Reindexing failed: {type(e).__name__}")
        raise HTTPException(500, "Reindexing failed")


@app.post("/plugins/{plugin_name}/reload")
async def reload_plugin(
    plugin_name: str, current_user: User = Depends(require_role(UserRole.ADMIN))
) -> Dict[str, str]:
    """Reload a specific plugin.

    Args:
        plugin_name: Name of the plugin to reload

    Returns:
        Status message
    """
    if plugin_manager is None:
        logger.error("Plugin reload requested but plugin manager not ready")
        raise HTTPException(503, "Plugin manager not ready")

    try:
        plugin_manager.reload_plugin(plugin_name)
        return {
            "status": "success",
            "message": f"Plugin '{plugin_name}' reloaded successfully",
        }
    except Exception as e:
        logger.error(f"Failed to reload plugin '{plugin_name}': {type(e).__name__}")
        raise HTTPException(500, "Failed to reload plugin")


@app.post("/plugins/{plugin_name}/enable")
async def enable_plugin(
    plugin_name: str, current_user: User = Depends(require_role(UserRole.ADMIN))
) -> Dict[str, str]:
    """Enable a disabled plugin.

    Args:
        plugin_name: Name of the plugin to enable

    Returns:
        Status message
    """
    if plugin_manager is None:
        logger.error("Plugin enable requested but plugin manager not ready")
        raise HTTPException(503, "Plugin manager not ready")

    try:
        plugin_manager.enable_plugin(plugin_name)
        # Recreate dispatcher with updated plugins
        active_plugins = plugin_manager.get_active_plugins()
        global dispatcher
        dispatcher = EnhancedDispatcher(list(active_plugins.values()))

        return {
            "status": "success",
            "message": f"Plugin '{plugin_name}' enabled successfully",
        }
    except Exception as e:
        logger.error(f"Failed to enable plugin '{plugin_name}': {type(e).__name__}")
        raise HTTPException(500, "Failed to enable plugin")


@app.post("/plugins/{plugin_name}/disable")
async def disable_plugin(
    plugin_name: str, current_user: User = Depends(require_role(UserRole.ADMIN))
) -> Dict[str, str]:
    """Disable an active plugin.

    Args:
        plugin_name: Name of the plugin to disable

    Returns:
        Status message
    """
    if plugin_manager is None:
        logger.error("Plugin disable requested but plugin manager not ready")
        raise HTTPException(503, "Plugin manager not ready")

    try:
        plugin_manager.disable_plugin(plugin_name)
        # Recreate dispatcher with updated plugins
        active_plugins = plugin_manager.get_active_plugins()
        global dispatcher
        dispatcher = EnhancedDispatcher(list(active_plugins.values()))

        return {
            "status": "success",
            "message": f"Plugin '{plugin_name}' disabled successfully",
        }
    except Exception as e:
        logger.error(f"Failed to disable plugin '{plugin_name}': {type(e).__name__}")
        raise HTTPException(500, "Failed to disable plugin")


# Cache management endpoints


@app.get("/cache/stats")
async def get_cache_stats(
    current_user: User = Depends(require_permission(Permission.READ)),
) -> Dict[str, Any]:
    """Get cache statistics and performance metrics."""
    if not cache_manager:
        raise HTTPException(503, "Cache manager not ready")

    try:
        cache_metrics = await cache_manager.get_metrics()
        backend_stats = await cache_manager.get_backend_stats()

        stats = {
            "cache_metrics": {
                "hits": cache_metrics.hits,
                "misses": cache_metrics.misses,
                "sets": cache_metrics.sets,
                "deletes": cache_metrics.deletes,
                "hit_rate": cache_metrics.hit_rate,
                "avg_response_time_ms": cache_metrics.avg_response_time_ms,
                "entries_count": cache_metrics.entries_count,
                "memory_usage_mb": cache_metrics.memory_usage_mb,
            },
            "backend_stats": backend_stats,
        }

        # Add query cache stats if available
        if query_cache:
            query_stats = await query_cache.get_cache_stats()
            stats["query_cache"] = query_stats

        return stats
    except Exception as e:
        logger.error(f"Failed to get cache stats: {type(e).__name__}")
        raise HTTPException(500, "Failed to get cache statistics")


@app.post("/cache/clear")
async def clear_cache(
    current_user: User = Depends(require_role(UserRole.ADMIN)),
) -> Dict[str, Any]:
    """Clear all cache entries (admin only)."""
    if not cache_manager:
        raise HTTPException(503, "Cache manager not ready")

    try:
        count = await cache_manager.clear()
        logger.info(f"Cache cleared by admin user {current_user.username}: {count} entries")

        return {
            "status": "success",
            "message": f"Cleared {count} cache entries",
            "cleared_entries": count,
        }
    except Exception as e:
        logger.error(f"Failed to clear cache: {type(e).__name__}")
        raise HTTPException(500, "Failed to clear cache")


@app.post("/cache/invalidate")
async def invalidate_cache_by_tags(
    tags: List[str], current_user: User = Depends(require_role(UserRole.ADMIN))
) -> Dict[str, Any]:
    """Invalidate cache entries by tags (admin only)."""
    if not cache_manager:
        raise HTTPException(503, "Cache manager not ready")

    try:
        tag_set = set(tags)
        count = await cache_manager.invalidate_by_tags(tag_set)
        logger.info(
            f"Cache invalidated by admin user {current_user.username}: {count} entries with tags {tags}"
        )

        return {
            "status": "success",
            "message": f"Invalidated {count} cache entries with tags {tags}",
            "invalidated_entries": count,
            "tags": tags,
        }
    except Exception as e:
        logger.error(f"Failed to invalidate cache by tags: {type(e).__name__}")
        raise HTTPException(500, "Failed to invalidate cache")


@app.post("/cache/invalidate/files")
async def invalidate_cache_by_files(
    file_paths: List[str], current_user: User = Depends(require_role(UserRole.ADMIN))
) -> Dict[str, Any]:
    """Invalidate cache entries that depend on specific files (admin only)."""
    if not query_cache:
        raise HTTPException(503, "Query cache not ready")

    try:
        total_count = 0
        for file_path in file_paths:
            count = await query_cache.invalidate_file_queries(file_path)
            total_count += count

        logger.info(
            f"File-based cache invalidation by admin user {current_user.username}: {total_count} entries for {len(file_paths)} files"
        )

        return {
            "status": "success",
            "message": f"Invalidated {total_count} cache entries for {len(file_paths)} files",
            "invalidated_entries": total_count,
            "files": file_paths,
        }
    except Exception as e:
        logger.error(f"Failed to invalidate cache by files: {type(e).__name__}")
        raise HTTPException(500, "Failed to invalidate cache by files")


@app.post("/cache/invalidate/semantic")
async def invalidate_semantic_cache(
    current_user: User = Depends(require_role(UserRole.ADMIN)),
) -> Dict[str, Any]:
    """Invalidate all semantic search cache entries (admin only)."""
    if not query_cache:
        raise HTTPException(503, "Query cache not ready")

    try:
        count = await query_cache.invalidate_semantic_queries()
        logger.info(
            f"Semantic cache invalidated by admin user {current_user.username}: {count} entries"
        )

        return {
            "status": "success",
            "message": f"Invalidated {count} semantic search cache entries",
            "invalidated_entries": count,
        }
    except Exception as e:
        logger.error(f"Failed to invalidate semantic cache: {type(e).__name__}")
        raise HTTPException(500, "Failed to invalidate semantic cache")


@app.post("/cache/warm")
async def warm_cache(
    keys: List[str], current_user: User = Depends(require_role(UserRole.ADMIN))
) -> Dict[str, Any]:
    """Warm cache with predefined keys (admin only)."""
    if not cache_manager:
        raise HTTPException(503, "Cache manager not ready")

    try:
        # Simple factory function for warming - would need more sophisticated logic in production
        async def factory(key: str):
            # This is a placeholder - in real implementation would depend on key type
            return f"warmed_value_for_{key}"

        count = await cache_manager.warm_cache(keys, factory)
        logger.info(f"Cache warmed by admin user {current_user.username}: {count} entries")

        return {
            "status": "success",
            "message": f"Warmed {count} cache entries",
            "warmed_entries": count,
            "requested_keys": len(keys),
        }
    except Exception as e:
        logger.error(f"Failed to warm cache: {type(e).__name__}")
        raise HTTPException(500, "Failed to warm cache")


@app.post("/cache/cleanup")
async def cleanup_cache(
    current_user: User = Depends(require_role(UserRole.ADMIN)),
) -> Dict[str, Any]:
    """Manually trigger cache cleanup (admin only)."""
    if not cache_manager:
        raise HTTPException(503, "Cache manager not ready")

    try:
        count = await cache_manager.cleanup()
        logger.info(
            f"Cache cleanup triggered by admin user {current_user.username}: {count} entries cleaned"
        )

        return {
            "status": "success",
            "message": f"Cleaned up {count} expired cache entries",
            "cleaned_entries": count,
        }
    except Exception as e:
        logger.error(f"Failed to cleanup cache: {type(e).__name__}")
        raise HTTPException(500, "Failed to cleanup cache")


# Hybrid Search endpoints


@app.get("/search/config")
async def get_search_config(
    current_user: User = Depends(require_permission(Permission.READ)),
) -> Dict[str, Any]:
    """Get current hybrid search configuration."""
    if not hybrid_search:
        raise HTTPException(503, "Hybrid search not available")

    config = hybrid_search.config
    return {
        "weights": {
            "bm25": config.bm25_weight,
            "semantic": config.semantic_weight,
            "fuzzy": config.fuzzy_weight,
        },
        "enabled_methods": {
            "bm25": config.enable_bm25,
            "semantic": config.enable_semantic,
            "fuzzy": config.enable_fuzzy,
        },
        "rrf_k": config.rrf_k,
        "parallel_execution": config.parallel_execution,
        "cache_results": config.cache_results,
        "limits": {
            "individual_limit": config.individual_limit,
            "final_limit": config.final_limit,
        },
    }


@app.put("/search/config/weights")
async def update_search_weights(
    bm25: Optional[float] = None,
    semantic: Optional[float] = None,
    fuzzy: Optional[float] = None,
    current_user: User = Depends(require_role(UserRole.ADMIN)),
) -> Dict[str, Any]:
    """Update hybrid search weights (admin only).

    Weights will be normalized to sum to 1.0.
    """
    if not hybrid_search:
        raise HTTPException(503, "Hybrid search not available")

    try:
        hybrid_search.set_weights(bm25=bm25, semantic=semantic, fuzzy=fuzzy)

        # Get updated config
        config = hybrid_search.config

        logger.info(
            f"Search weights updated by {current_user.username}: "
            f"BM25={config.bm25_weight:.3f}, "
            f"Semantic={config.semantic_weight:.3f}, "
            f"Fuzzy={config.fuzzy_weight:.3f}"
        )

        return {
            "status": "success",
            "weights": {
                "bm25": config.bm25_weight,
                "semantic": config.semantic_weight,
                "fuzzy": config.fuzzy_weight,
            },
        }
    except Exception as e:
        logger.error(f"Failed to update search weights: {type(e).__name__}")
        raise HTTPException(500, "Failed to update weights")


@app.put("/search/config/methods")
async def toggle_search_methods(
    bm25: Optional[bool] = None,
    semantic: Optional[bool] = None,
    fuzzy: Optional[bool] = None,
    current_user: User = Depends(require_role(UserRole.ADMIN)),
) -> Dict[str, Any]:
    """Enable or disable search methods (admin only)."""
    if not hybrid_search:
        raise HTTPException(503, "Hybrid search not available")

    try:
        hybrid_search.enable_methods(bm25=bm25, semantic=semantic, fuzzy=fuzzy)

        # Get updated config
        config = hybrid_search.config

        logger.info(
            f"Search methods updated by {current_user.username}: "
            f"BM25={config.enable_bm25}, "
            f"Semantic={config.enable_semantic}, "
            f"Fuzzy={config.enable_fuzzy}"
        )

        return {
            "status": "success",
            "enabled_methods": {
                "bm25": config.enable_bm25,
                "semantic": config.enable_semantic,
                "fuzzy": config.enable_fuzzy,
            },
        }
    except Exception as e:
        logger.error(f"Failed to update search methods: {type(e).__name__}")
        raise HTTPException(500, "Failed to update methods")


@app.get("/search/statistics")
async def get_search_statistics(
    current_user: User = Depends(require_permission(Permission.READ)),
) -> Dict[str, Any]:
    """Get search statistics and performance metrics."""
    stats = {}

    # Hybrid search statistics
    if hybrid_search:
        stats["hybrid_search"] = hybrid_search.get_statistics()

    # BM25 statistics
    if bm25_indexer:
        stats["bm25"] = bm25_indexer.get_statistics()

    # Fuzzy search statistics
    if fuzzy_indexer and hasattr(fuzzy_indexer, "get_statistics"):
        stats["fuzzy"] = fuzzy_indexer.get_statistics()

    # Add general search metrics from business metrics
    if business_metrics:
        search_metrics = business_metrics.get_search_metrics()
        stats["general"] = {
            "total_searches": search_metrics.get("total_searches", 0),
            "average_response_time_ms": search_metrics.get("avg_response_time", 0),
            "search_success_rate": search_metrics.get("success_rate", 0),
        }

    return stats


@app.post("/search/optimize")
async def optimize_search_indexes(
    current_user: User = Depends(require_role(UserRole.ADMIN)),
) -> Dict[str, Any]:
    """Optimize search indexes for better performance (admin only)."""
    results = {}

    try:
        # Optimize BM25 indexes
        if bm25_indexer:
            bm25_indexer.optimize()
            results["bm25"] = "optimized"
            logger.info("BM25 indexes optimized")

        # Optimize FTS5 tables in SQLite
        if sqlite_store:
            sqlite_store.optimize_fts_tables()
            results["fts5"] = "optimized"
            logger.info("FTS5 tables optimized")

        # Clear hybrid search cache
        if hybrid_search:
            hybrid_search.clear_cache()
            results["hybrid_cache"] = "cleared"
            logger.info("Hybrid search cache cleared")

        logger.info(f"Search indexes optimized by {current_user.username}")

        return {
            "status": "success",
            "message": "Search indexes optimized successfully",
            "results": results,
        }
    except Exception as e:
        logger.error(f"Failed to optimize search indexes: {type(e).__name__}")
        raise HTTPException(500, "Failed to optimize indexes")


@app.get("/search/term/{term}/stats")
async def get_term_statistics(
    term: str, current_user: User = Depends(require_permission(Permission.READ))
) -> Dict[str, Any]:
    """Get statistics for a specific search term."""
    stats = {}

    try:
        # BM25 term statistics
        if bm25_indexer:
            stats["bm25"] = bm25_indexer.get_term_statistics(term)

        # SQLite FTS5 statistics
        if sqlite_store:
            stats["fts5"] = sqlite_store.get_bm25_term_statistics(term)

        return {"term": term, "statistics": stats}
    except Exception as e:
        logger.error(f"Failed to get term statistics: {type(e).__name__}")
        raise HTTPException(500, "Failed to get term statistics")


@app.post("/search/rebuild")
async def rebuild_search_indexes(
    index_type: str = "all",  # "all", "bm25", "fuzzy", "semantic"
    current_user: User = Depends(require_role(UserRole.ADMIN)),
) -> Dict[str, Any]:
    """Rebuild search indexes (admin only)."""
    if index_type not in ["all", "bm25", "fuzzy", "semantic"]:
        raise HTTPException(
            400, "Invalid index_type. Must be 'all', 'bm25', 'fuzzy', or 'semantic'"
        )

    results = {}

    try:
        if index_type in ["all", "bm25"] and bm25_indexer:
            bm25_indexer.rebuild()
            results["bm25"] = "rebuilt"
            logger.info("BM25 index rebuilt")

        if index_type in ["all", "fuzzy"] and fuzzy_indexer:
            fuzzy_indexer.clear()
            # Re-index all files
            if sqlite_store:
                files = sqlite_store.get_all_files()
                for file_info in files:
                    try:
                        with open(file_info["path"], "r", encoding="utf-8") as f:
                            content = f.read()
                        fuzzy_indexer.add_file(file_info["path"], content)
                    except Exception as e:
                        logger.warning(
                            f"Failed to re-index {file_info['path']}: {type(e).__name__}"
                        )
            results["fuzzy"] = "rebuilt"
            logger.info("Fuzzy index rebuilt")

        if index_type in ["all", "semantic"]:
            # Semantic index rebuild would go here if available
            if hasattr(hybrid_search, "semantic_indexer") and hybrid_search.semantic_indexer:
                results["semantic"] = "rebuild_not_implemented"
            else:
                results["semantic"] = "not_available"

        logger.info(f"Search indexes rebuilt by {current_user.username}: {index_type}")

        return {
            "status": "success",
            "message": "Search indexes rebuilt successfully",
            "index_type": index_type,
            "results": results,
        }
    except Exception as e:
        logger.error(f"Failed to rebuild search indexes: {type(e).__name__}")
        raise HTTPException(500, "Failed to rebuild indexes")


# Graph Analysis Endpoints


@app.get("/graph/dependencies/{symbol}")
async def get_symbol_dependencies(
    request: Request,
    symbol: str,
    max_depth: int = 3,
    current_user: User = Depends(require_permission(Permission.READ)),
) -> Dict[str, Any]:
    """Get dependencies of a symbol."""
    if dispatcher is None:
        raise HTTPException(503, "Dispatcher not ready")

    ctx = get_repo_ctx(request)
    try:
        dependencies = dispatcher.find_symbol_dependencies(ctx, symbol, max_depth)

        return {
            "symbol": symbol,
            "dependencies": dependencies,
            "count": len(dependencies),
            "max_depth": max_depth,
        }
    except Exception as e:
        logger.error("Dependency lookup failed (%s)", type(e).__name__)
        raise HTTPException(500, "Failed to get dependencies")


@app.get("/graph/dependents/{symbol}")
async def get_symbol_dependents(
    request: Request,
    symbol: str,
    max_depth: int = 3,
    current_user: User = Depends(require_permission(Permission.READ)),
) -> Dict[str, Any]:
    """Get dependents of a symbol (what depends on it)."""
    if dispatcher is None:
        raise HTTPException(503, "Dispatcher not ready")

    ctx = get_repo_ctx(request)
    try:
        dependents = dispatcher.find_symbol_dependents(ctx, symbol, max_depth)

        return {
            "symbol": symbol,
            "dependents": dependents,
            "count": len(dependents),
            "max_depth": max_depth,
        }
    except Exception as e:
        logger.error("Dependent lookup failed (%s)", type(e).__name__)
        raise HTTPException(500, "Failed to get dependents")


@app.get("/graph/hotspots")
async def get_code_hotspots(
    request: Request,
    top_n: int = 10,
    current_user: User = Depends(require_permission(Permission.READ)),
) -> Dict[str, Any]:
    """Get code hotspots (highly connected nodes)."""
    if dispatcher is None:
        raise HTTPException(503, "Dispatcher not ready")

    ctx = get_repo_ctx(request)
    try:
        hotspots = dispatcher.get_code_hotspots(ctx, top_n)

        return {
            "hotspots": hotspots,
            "count": len(hotspots),
            "top_n": top_n,
        }
    except Exception as e:
        logger.error(f"Error getting hotspots: {type(e).__name__}")
        raise HTTPException(500, "Failed to get hotspots")


@app.post("/graph/context")
async def get_context_for_symbols(
    request: Request,
    symbols: List[str],
    radius: int = 2,
    budget: int = 200,
    weights: Optional[Dict[str, float]] = None,
    current_user: User = Depends(require_permission(Permission.READ)),
) -> Dict[str, Any]:
    """Get optimal context for symbols using graph cut."""
    if dispatcher is None:
        raise HTTPException(503, "Dispatcher not ready")

    ctx = get_repo_ctx(request)
    try:
        result = dispatcher.get_context_for_symbols(ctx, symbols, radius, budget, weights)

        if result is None:
            return {
                "available": False,
                "message": "Graph features not available. Install treesitter-chunker.",
            }

        return {
            "available": True,
            "symbols": symbols,
            "selected_nodes": [
                {
                    "id": node.id,
                    "symbol": node.symbol,
                    "file": node.file_path,
                    "kind": node.kind,
                    "language": node.language,
                    "line": node.line_start,
                    "score": node.score,
                }
                for node in result.selected_nodes
            ],
            "edges": [
                {
                    "source": edge.source_id,
                    "target": edge.target_id,
                    "type": edge.edge_type.value,
                    "weight": edge.weight,
                }
                for edge in result.induced_edges
            ],
            "statistics": {
                "selected_nodes": len(result.selected_nodes),
                "induced_edges": len(result.induced_edges),
                "seed_nodes": len(result.seed_nodes),
                "radius": result.radius,
                "budget": result.budget,
                "total_candidates": result.total_candidates,
                "execution_time_ms": result.execution_time_ms,
            },
        }
    except Exception as e:
        logger.error(f"Error getting context for symbols: {type(e).__name__}")
        raise HTTPException(500, "Failed to get context")


@app.get("/graph/search")
async def graph_search(
    request: Request,
    q: str,
    expansion_radius: int = 1,
    max_context_nodes: int = 50,
    semantic: bool = False,
    limit: int = 20,
    current_user: User = Depends(require_permission(Permission.READ)),
) -> Dict[str, Any]:
    """Search with graph-based context expansion."""
    if dispatcher is None:
        raise HTTPException(503, "Dispatcher not ready")

    ctx = get_repo_ctx(request)
    try:
        results = list(
            dispatcher.graph_search(
                ctx,
                query=q,
                expansion_radius=expansion_radius,
                max_context_nodes=max_context_nodes,
                semantic=semantic,
                limit=limit,
            )
        )

        # Separate context results from search results
        search_results = [r for r in results if not r.get("context", False)]
        context_results = [r for r in results if r.get("context", False)]

        return {
            "query": q,
            "search_results": search_results,
            "context_results": context_results,
            "statistics": {
                "total_results": len(results),
                "search_results": len(search_results),
                "context_results": len(context_results),
                "expansion_radius": expansion_radius,
                "max_context_nodes": max_context_nodes,
            },
        }
    except Exception as e:
        logger.error(f"Error in graph search: {type(e).__name__}")
        raise HTTPException(500, "Graph search failed")


@app.get("/graph/status")
async def get_graph_status(
    request: Request,
    current_user: User = Depends(require_permission(Permission.READ)),
) -> Dict[str, Any]:
    """Get graph analysis system status."""
    if dispatcher is None:
        raise HTTPException(503, "Dispatcher not ready")

    ctx = get_repo_ctx(request)
    try:
        from .graph import CHUNKER_AVAILABLE

        status: Dict[str, Any] = {"available": CHUNKER_AVAILABLE}
        if hasattr(dispatcher, "get_runtime_feature_status"):
            feature_status = dispatcher.get_runtime_feature_status(ctx).get("graph", {})
            status.update(feature_status)

        if not CHUNKER_AVAILABLE:
            status["initialized"] = False
            status["message"] = (
                "Install treesitter-chunker for graph features: pip install treesitter-chunker"
            )
        else:
            # Probe availability via a zero-cost health check; graph init is lazy.
            health = dispatcher.health_check(ctx)
            status["initialized"] = health.get("graph_initialized", False)
            if not status["initialized"]:
                status.setdefault("status", "unavailable")
                status.setdefault("reason", "graph_not_initialized")

        return status
    except Exception as e:
        logger.error(f"Error getting graph status: {type(e).__name__}")
        raise HTTPException(500, "Failed to get graph status")


@app.post("/graph/initialize")
async def initialize_graph(
    request: Request,
    file_paths: Optional[List[str]] = None,
    current_user: User = Depends(require_role(UserRole.ADMIN)),
) -> Dict[str, Any]:
    """Initialize or rebuild the graph from files (admin only)."""
    if dispatcher is None:
        raise HTTPException(503, "Dispatcher not ready")

    ctx = get_repo_ctx(request)
    try:
        # If no file paths provided, scan all indexed files
        if file_paths is None:
            if ctx.sqlite_store:
                files = ctx.sqlite_store.get_all_files()
                file_paths = [f["path"] for f in files]
            else:
                raise HTTPException(400, "No file paths provided and no store available")

        # Trigger graph initialization via a bulk index_directory call as a proxy.
        # The dispatcher's graph is built lazily; priming it via index_directory
        # ensures graph structures are populated for the listed paths.
        if hasattr(dispatcher, "_ensure_graph_initialized"):
            success = dispatcher._ensure_graph_initialized(file_paths, ctx=ctx)
            if not success:
                return {
                    "status": "failed",
                    "message": "Graph initialization failed. Check that treesitter-chunker is installed.",
                }
        else:
            return {
                "status": "unavailable",
                "message": "Graph initialization not supported by the current dispatcher.",
            }

        health = dispatcher.health_check(ctx)
        return {
            "status": "success",
            "message": f"Graph initialized from {len(file_paths)} files",
            "graph_initialized": health.get("graph_initialized", False),
        }
    except Exception as e:
        logger.error(f"Error initializing graph: {type(e).__name__}")
        raise HTTPException(500, "Failed to initialize graph")
