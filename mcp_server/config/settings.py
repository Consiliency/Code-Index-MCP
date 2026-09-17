"""
Comprehensive settings management for production deployments.
"""

import json
import os
import re
import secrets
from dataclasses import dataclass
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, model_validator, validator

from .environment import Environment, get_env_var, get_environment, is_production


def _find_profiles_yaml() -> Optional[str]:
    """Return the first existing code-index-mcp.profiles.yaml path.

    Search order:
    1. MCP_PROFILES_PATH env var (explicit override)
    2. CWD/code-index-mcp.profiles.yaml (per-repo override)
    3. <package-root>/code-index-mcp.profiles.yaml (server installation fallback)
    """
    env_path = os.getenv("MCP_PROFILES_PATH")
    if env_path:
        if not Path(env_path).is_file():
            raise FileNotFoundError("MCP_PROFILES_PATH does not name an existing file")
        return env_path
    cwd_path = Path.cwd() / "code-index-mcp.profiles.yaml"
    if cwd_path.exists():
        return str(cwd_path)
    pkg_path = Path(__file__).parent.parent.parent / "code-index-mcp.profiles.yaml"
    if pkg_path.exists():
        return str(pkg_path)
    return None


def _expand_env_vars(value: str) -> str:
    """Expand ${VAR} and ${VAR:default} patterns in *value* using os.environ."""

    def _replace(m: re.Match) -> str:
        var, _, default = m.group(1).partition(":")
        return os.environ.get(var, default)

    return re.sub(r"\$\{([^}]+)\}", _replace, value)


def _resolve_semantic_base_url(
    configured_value: Any,
    *,
    primary_env: str,
    legacy_env: Optional[str] = None,
    fallback: Optional[str] = None,
) -> str:
    """Resolve a semantic base URL with primary and legacy env-var shims."""
    primary_value = os.environ.get(primary_env)
    if primary_value:
        return primary_value
    if legacy_env:
        legacy_value = os.environ.get(legacy_env)
        if legacy_value:
            return legacy_value
    if configured_value not in (None, ""):
        return _expand_env_vars(str(configured_value))
    return str(fallback or "")


class DatabaseSettings(BaseModel):
    """Database configuration settings."""

    # Connection settings
    url: str = Field(default="sqlite:///./code_index.db", description="Database connection URL")
    pool_size: int = Field(default=10, ge=1, le=100)
    max_overflow: int = Field(default=20, ge=0, le=100)
    pool_timeout: int = Field(default=30, ge=1, le=300)
    pool_recycle: int = Field(default=3600, ge=300, le=86400)

    # SQLite specific settings
    sqlite_timeout: int = Field(default=20, ge=1, le=60)
    sqlite_check_same_thread: bool = Field(default=False)

    # Migration settings
    auto_migrate: bool = Field(default=True)
    migration_timeout: int = Field(default=300, ge=30, le=1800)

    @classmethod
    def from_environment(cls) -> "DatabaseSettings":
        """Create database settings from environment variables."""
        env = get_environment()

        if env == Environment.PRODUCTION:
            # Production requires explicit database URL
            db_url = get_env_var("DATABASE_URL", required=True)
            return cls(
                url=db_url,
                pool_size=int(get_env_var("DB_POOL_SIZE", "20")),
                max_overflow=int(get_env_var("DB_MAX_OVERFLOW", "40")),
                pool_timeout=int(get_env_var("DB_POOL_TIMEOUT", "30")),
                pool_recycle=int(get_env_var("DB_POOL_RECYCLE", "3600")),
                auto_migrate=get_env_var("DB_AUTO_MIGRATE", "false").lower() == "true",
            )

        elif env == Environment.STAGING:
            # Staging can use PostgreSQL or SQLite
            db_url = get_env_var(
                "DATABASE_URL", "postgresql://mcp_user:mcp_pass@localhost/mcp_staging"
            )
            return cls(
                url=db_url,
                pool_size=int(get_env_var("DB_POOL_SIZE", "10")),
                auto_migrate=True,
            )

        elif env == Environment.TESTING:
            # Testing uses in-memory SQLite
            return cls(
                url="sqlite:///:memory:",
                auto_migrate=True,
                sqlite_check_same_thread=False,
            )

        else:  # Development
            # Development uses local SQLite file
            return cls(
                url=get_env_var("DATABASE_URL", "sqlite:///./dev_code_index.db"),
                auto_migrate=True,
            )

    @validator("url")
    def validate_database_url(cls, v):
        """Validate database URL format."""
        try:
            parsed = urlparse(v)
            if not parsed.scheme:
                raise ValueError(
                    "Database URL must include scheme (sqlite://, postgresql://, etc.)"
                )
            return v
        except Exception as e:
            raise ValueError(f"Invalid database URL: {e}")


class SecuritySettings(BaseModel):
    """Security configuration settings."""

    # JWT Configuration
    jwt_secret_key: str = Field(min_length=32, description="JWT signing secret key")
    jwt_algorithm: str = Field(default="HS256", description="JWT signing algorithm")
    access_token_expire_minutes: int = Field(default=30, ge=1, le=1440)
    refresh_token_expire_days: int = Field(default=7, ge=1, le=90)

    # Password Policy
    password_min_length: int = Field(default=8, ge=6, le=128)
    password_require_uppercase: bool = Field(default=True)
    password_require_lowercase: bool = Field(default=True)
    password_require_numbers: bool = Field(default=True)
    password_require_special: bool = Field(default=True)

    # Rate Limiting
    rate_limit_enabled: bool = Field(default=True)
    rate_limit_requests: int = Field(default=100, ge=1, le=10000)
    rate_limit_window: int = Field(default=3600, ge=60, le=86400)

    # CORS Settings
    cors_enabled: bool = Field(default=True)
    cors_allowed_origins: List[str] = Field(default_factory=lambda: ["*"])
    cors_allowed_methods: List[str] = Field(
        default_factory=lambda: ["GET", "POST", "PUT", "DELETE"]
    )
    cors_allowed_headers: List[str] = Field(default_factory=lambda: ["*"])

    # Security Headers
    security_headers_enabled: bool = Field(default=True)
    hsts_max_age: int = Field(default=31536000, ge=0)

    # Admin User Settings
    default_admin_username: str = Field(default="admin")
    default_admin_email: str = Field(default="admin@localhost")

    @classmethod
    def from_environment(cls) -> "SecuritySettings":
        """Create security settings from environment variables."""
        env = get_environment()

        # Generate secure JWT secret if not provided
        jwt_secret = get_env_var("JWT_SECRET_KEY")
        if not jwt_secret:
            if is_production():
                raise ValueError("JWT_SECRET_KEY must be explicitly set in production environment")
            # Generate a secure secret for non-production
            jwt_secret = secrets.token_urlsafe(32)

        if env == Environment.PRODUCTION:
            return cls(
                jwt_secret_key=jwt_secret,
                access_token_expire_minutes=int(get_env_var("JWT_ACCESS_EXPIRE_MINUTES", "15")),
                refresh_token_expire_days=int(get_env_var("JWT_REFRESH_EXPIRE_DAYS", "7")),
                cors_allowed_origins=get_env_var("CORS_ALLOWED_ORIGINS", "").split(","),
                rate_limit_requests=int(get_env_var("RATE_LIMIT_REQUESTS", "60")),
                rate_limit_window=int(get_env_var("RATE_LIMIT_WINDOW", "3600")),
                default_admin_username=get_env_var("DEFAULT_ADMIN_USERNAME", "admin"),
                default_admin_email=get_env_var("DEFAULT_ADMIN_EMAIL", required=True),
            )

        elif env == Environment.STAGING:
            return cls(
                jwt_secret_key=jwt_secret,
                access_token_expire_minutes=int(get_env_var("JWT_ACCESS_EXPIRE_MINUTES", "30")),
                cors_allowed_origins=get_env_var(
                    "CORS_ALLOWED_ORIGINS", "http://localhost:3000"
                ).split(","),
                rate_limit_requests=int(get_env_var("RATE_LIMIT_REQUESTS", "100")),
            )

        elif env == Environment.TESTING:
            return cls(
                jwt_secret_key="test-secret-key-32-characters-long",
                access_token_expire_minutes=5,
                rate_limit_enabled=False,
                cors_enabled=True,
            )

        else:  # Development
            return cls(
                jwt_secret_key=jwt_secret,
                access_token_expire_minutes=int(get_env_var("JWT_ACCESS_EXPIRE_MINUTES", "60")),
                rate_limit_requests=int(get_env_var("RATE_LIMIT_REQUESTS", "1000")),
                cors_allowed_origins=["*"],
            )


class CacheSettings(BaseModel):
    """Cache configuration settings."""

    # Redis Configuration
    redis_url: Optional[str] = Field(default=None, description="Redis connection URL")
    redis_pool_size: int = Field(default=10, ge=1, le=100)
    redis_timeout: int = Field(default=30, ge=1, le=300)

    # Cache TTL Settings
    default_ttl: int = Field(default=3600, ge=60, le=86400)
    symbol_cache_ttl: int = Field(default=1800, ge=60, le=86400)
    search_cache_ttl: int = Field(default=600, ge=60, le=3600)

    # Memory Cache Settings
    memory_cache_size: int = Field(default=1000, ge=100, le=100000)

    @classmethod
    def from_environment(cls) -> "CacheSettings":
        """Create cache settings from environment variables."""
        env = get_environment()

        if env == Environment.PRODUCTION:
            # Production requires Redis
            redis_url = get_env_var("REDIS_URL", required=True)
            return cls(
                redis_url=redis_url,
                redis_pool_size=int(get_env_var("REDIS_POOL_SIZE", "20")),
                default_ttl=int(get_env_var("CACHE_DEFAULT_TTL", "3600")),
                symbol_cache_ttl=int(get_env_var("CACHE_SYMBOL_TTL", "1800")),
                search_cache_ttl=int(get_env_var("CACHE_SEARCH_TTL", "600")),
            )

        elif env == Environment.STAGING:
            # Staging can use Redis if available
            redis_url = get_env_var("REDIS_URL", "redis://localhost:6379")
            return cls(
                redis_url=redis_url,
                default_ttl=int(get_env_var("CACHE_DEFAULT_TTL", "1800")),
            )

        else:  # Development and Testing
            # Use memory cache for simplicity
            return cls(
                redis_url=get_env_var("REDIS_URL"),  # Optional in dev
                memory_cache_size=int(get_env_var("MEMORY_CACHE_SIZE", "1000")),
            )


class MetricsSettings(BaseModel):
    """Metrics and monitoring configuration."""

    # Prometheus Settings
    prometheus_enabled: bool = Field(default=True)
    prometheus_port: int = Field(default=8001, ge=1024, le=65535)
    prometheus_path: str = Field(default="/metrics")

    # Health Check Settings
    health_check_enabled: bool = Field(default=True)
    health_check_interval: int = Field(default=30, ge=5, le=300)

    # Performance Monitoring
    performance_monitoring_enabled: bool = Field(default=True)
    slow_query_threshold: float = Field(default=1.0, ge=0.1, le=60.0)

    @classmethod
    def from_environment(cls) -> "MetricsSettings":
        """Create metrics settings from environment variables."""
        return cls(
            prometheus_enabled=get_env_var("PROMETHEUS_ENABLED", "true").lower() == "true",
            prometheus_port=int(get_env_var("PROMETHEUS_PORT", "8001")),
            health_check_enabled=get_env_var("HEALTH_CHECK_ENABLED", "true").lower() == "true",
            performance_monitoring_enabled=get_env_var(
                "PERFORMANCE_MONITORING_ENABLED", "true"
            ).lower()
            == "true",
            slow_query_threshold=float(get_env_var("SLOW_QUERY_THRESHOLD", "1.0")),
        )


class LoggingSettings(BaseModel):
    """Logging configuration settings."""

    # Log Level
    level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")

    # Log Format
    format: str = Field(default="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    json_format: bool = Field(default=False, description="Use JSON structured logging")

    # Log Output
    log_file: Optional[str] = Field(default=None, description="Log file path")
    log_rotation: bool = Field(default=True)
    log_max_size: str = Field(default="100MB")
    log_backup_count: int = Field(default=5, ge=1, le=20)

    # Request Logging
    request_logging_enabled: bool = Field(default=True)
    log_request_body: bool = Field(default=False)
    log_response_body: bool = Field(default=False)

    @classmethod
    def from_environment(cls) -> "LoggingSettings":
        """Create logging settings from environment variables."""
        env = get_environment()

        if env == Environment.PRODUCTION:
            return cls(
                level=get_env_var("LOG_LEVEL", "INFO"),
                json_format=True,  # Production uses structured logging
                log_file=get_env_var("LOG_FILE", "PathUtils.get_log_path()/app.log"),
                log_request_body=False,  # Don't log bodies in production
                log_response_body=False,
            )

        elif env == Environment.TESTING:
            return cls(
                level=get_env_var("LOG_LEVEL", "WARNING"),
                json_format=False,
                log_file=None,  # No file logging in tests
                request_logging_enabled=False,
            )

        else:  # Development and Staging
            return cls(
                level=get_env_var("LOG_LEVEL", "DEBUG"),
                json_format=get_env_var("LOG_JSON_FORMAT", "false").lower() == "true",
                log_file=get_env_var("LOG_FILE"),
                log_request_body=get_env_var("LOG_REQUEST_BODY", "false").lower() == "true",
            )


class SummarizationSettings(BaseModel):
    """LLM model defaults for chunk summarization fallback chain."""

    # Cerebras OSS fallback — Qwen 3 235B (preview) is the most modern model available on Cerebras
    cerebras_model: str = Field(
        default="qwen-3-235b-a22b-instruct-2507", description="Cerebras model for OSS fallback"
    )

    # Anthropic fallback — claude-haiku-4-5 is the current lightweight model
    anthropic_model: str = Field(
        default="claude-haiku-4-5-20251001", description="Anthropic model for commercial fallback"
    )

    # OpenAI fallback — gpt-5.4-nano is cheapest/fastest for short summaries
    openai_model: str = Field(
        default="gpt-5.4-nano", description="OpenAI model for commercial fallback"
    )

    @classmethod
    def from_environment(cls) -> "SummarizationSettings":
        """Create summarization settings from environment variables."""
        return cls(
            cerebras_model=get_env_var("CEREBRAS_MODEL", "qwen-3-235b-a22b-instruct-2507"),
            anthropic_model=get_env_var("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            openai_model=get_env_var("OPENAI_CHAT_MODEL", "gpt-5.4-nano"),
        )


class RerankingSettings(BaseModel):
    """Reranking configuration for search result optimization."""

    # Enable/disable reranking
    enabled: bool = Field(default=False, description="Enable search result reranking")

    # Reranker type selection
    reranker_type: str = Field(
        default="hybrid",
        pattern="^(cohere|cross-encoder|tfidf|hybrid|voyage|flashrank|none)$",
        description="Type of reranker to use",
    )

    # Voyage AI settings
    voyage_model: str = Field(
        default="rerank-2",
        description="Voyage AI reranking model (rerank-2 or rerank-2-lite)",
    )

    # Cohere settings
    cohere_api_key: Optional[str] = Field(default=None, description="Cohere API key")
    cohere_model: str = Field(default="rerank-english-v2.0", description="Cohere reranking model")

    # Cross-encoder settings
    cross_encoder_model: str = Field(
        default="cross-encoder/ms-marco-MiniLM-L-6-v2",
        description="Cross-encoder model for local reranking",
    )
    cross_encoder_device: str = Field(default="cpu", pattern="^(cpu|cuda|mps)$")

    # Hybrid reranker settings
    hybrid_primary_type: str = Field(
        default="cohere", description="Primary reranker for hybrid mode"
    )
    hybrid_fallback_type: str = Field(
        default="tfidf", description="Fallback reranker for hybrid mode"
    )
    hybrid_primary_weight: float = Field(default=0.7, ge=0.0, le=1.0)
    hybrid_fallback_weight: float = Field(default=0.3, ge=0.0, le=1.0)

    # General settings
    top_k: Optional[int] = Field(
        default=None, ge=1, le=100, description="Number of top results to rerank"
    )
    cache_ttl: int = Field(default=3600, ge=0, le=86400, description="Cache TTL in seconds")

    # Performance settings
    batch_size: int = Field(default=32, ge=1, le=128, description="Batch size for reranking")
    timeout: float = Field(default=5.0, ge=0.1, le=30.0, description="Reranking timeout in seconds")

    @validator("hybrid_primary_weight", "hybrid_fallback_weight")
    def validate_weights(cls, v, values):
        """Ensure weights sum to 1.0 for hybrid mode."""
        if "hybrid_primary_weight" in values and "hybrid_fallback_weight" in values:
            total = values["hybrid_primary_weight"] + values["hybrid_fallback_weight"]
            if abs(total - 1.0) > 0.001:  # Allow small floating point errors
                raise ValueError(f"Hybrid weights must sum to 1.0, got {total}")
        return v

    @classmethod
    def from_environment(cls) -> "RerankingSettings":
        """Create reranking settings from environment variables."""
        return cls(
            enabled=get_env_var("RERANKING_ENABLED", "false").lower() == "true",
            reranker_type=get_env_var("RERANKER_TYPE", "hybrid"),
            cohere_api_key=get_env_var("COHERE_API_KEY"),
            cohere_model=get_env_var("COHERE_MODEL", "rerank-english-v2.0"),
            cross_encoder_model=get_env_var(
                "CROSS_ENCODER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2"
            ),
            voyage_model=get_env_var("VOYAGE_MODEL", "rerank-2"),
            cross_encoder_device=get_env_var("CROSS_ENCODER_DEVICE", "cpu"),
            hybrid_primary_type=get_env_var("HYBRID_PRIMARY_TYPE", "cohere"),
            hybrid_fallback_type=get_env_var("HYBRID_FALLBACK_TYPE", "tfidf"),
            top_k=int(get_env_var("RERANK_TOP_K", "0")) or None,
            cache_ttl=int(get_env_var("RERANK_CACHE_TTL", "3600")),
        )


class Settings(BaseModel):
    """Main application settings."""

    # Application Info
    app_name: str = Field(default="MCP Server")
    app_version: str = Field(default="1.0.0")
    app_description: str = Field(default="Code Index MCP Server")

    # Server Settings
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000, ge=1024, le=65535)
    workers: int = Field(default=1, ge=1, le=16)

    # Environment
    environment: Environment = Field(default_factory=get_environment)
    debug: bool = Field(default=False)

    # Component Settings
    database: DatabaseSettings = Field(default_factory=DatabaseSettings.from_environment)
    security: SecuritySettings = Field(default_factory=SecuritySettings.from_environment)
    cache: CacheSettings = Field(default_factory=CacheSettings.from_environment)
    metrics: MetricsSettings = Field(default_factory=MetricsSettings.from_environment)
    logging: LoggingSettings = Field(default_factory=LoggingSettings.from_environment)
    reranking: RerankingSettings = Field(default_factory=RerankingSettings.from_environment)
    summarization: SummarizationSettings = Field(
        default_factory=SummarizationSettings.from_environment
    )

    # Feature Flags
    dynamic_plugin_loading: bool = Field(default=True)
    mcp_auto_detect_languages: bool = Field(default=True)
    mcp_language_detect_max_files: int = Field(default=5000)
    mcp_language_detect_min_files: int = Field(default=2)
    mcp_fast_startup: bool = Field(default=False)
    semantic_search_enabled: bool = Field(default=False)
    strict_index_compatibility: bool = Field(default=True)
    index_schema_version: str = Field(default="2")
    artifact_provider: str = Field(default="github_actions")
    artifact_local_cache_dir: str = Field(default=".indexes/artifacts")
    artifact_s3_bucket: Optional[str] = Field(default=None)
    artifact_s3_prefix: str = Field(default="mcp-index")
    artifact_routing_default_repo_visibility: str = Field(default="private")
    artifact_routing_default_artifact_size_bytes: int = Field(default=0)
    artifact_routing_default_profile_type: str = Field(default="standard")
    artifact_routing_large_threshold_bytes: int = Field(default=50 * 1024 * 1024)
    artifact_routing_fallback_order: str = Field(default="local_fs,github_actions")
    artifact_delta_enabled: bool = Field(default=False)

    # Semantic Search Configuration
    voyage_api_key: Optional[str] = Field(default=None)
    openai_api_key: Optional[str] = Field(default=None)
    openai_api_base: str = Field(default="http://localhost:8001/v1")
    qdrant_host: str = Field(default="localhost")
    qdrant_port: int = Field(default=6333)
    qdrant_grpc_port: int = Field(default=6334)
    semantic_embedding_model: str = Field(default="voyage-code-3")
    semantic_collection_name: str = Field(default="code-embeddings")
    semantic_profiles_json: Optional[str] = Field(default=None)
    semantic_default_profile: str = Field(default="oss_high")
    semantic_autostart_qdrant: bool = Field(default=True)
    semantic_strict_mode: bool = Field(default=False)
    semantic_preflight_timeout_seconds: int = Field(default=10)
    qdrant_compose_file: str = Field(default="docker-compose.qdrant.yml")

    @classmethod
    def from_environment(cls) -> "Settings":
        """Create settings from environment variables."""
        env = get_environment()

        # Base settings
        settings = cls(
            app_name=get_env_var("APP_NAME", "MCP Server"),
            app_version=get_env_var("APP_VERSION", "1.0.0"),
            host=get_env_var("HOST", "0.0.0.0"),
            port=int(get_env_var("PORT", "8000")),
            environment=env,
            debug=get_env_var("DEBUG", "false").lower() == "true",
            dynamic_plugin_loading=get_env_var("DYNAMIC_PLUGIN_LOADING", "true").lower() == "true",
            mcp_auto_detect_languages=get_env_var("MCP_AUTO_DETECT_LANGUAGES", "true").lower()
            == "true",
            mcp_language_detect_max_files=int(get_env_var("MCP_LANGUAGE_DETECT_MAX_FILES", "5000")),
            mcp_language_detect_min_files=int(get_env_var("MCP_LANGUAGE_DETECT_MIN_FILES", "2")),
            mcp_fast_startup=get_env_var("MCP_FAST_STARTUP", "false").lower() == "true",
            semantic_search_enabled=get_env_var("SEMANTIC_SEARCH_ENABLED", "true").lower()
            == "true",
            strict_index_compatibility=get_env_var("STRICT_INDEX_COMPATIBILITY", "true").lower()
            == "true",
            index_schema_version=get_env_var("INDEX_SCHEMA_VERSION", "2"),
            artifact_provider=get_env_var("ARTIFACT_PROVIDER", "github_actions"),
            artifact_local_cache_dir=get_env_var("ARTIFACT_LOCAL_CACHE_DIR", ".indexes/artifacts"),
            artifact_s3_bucket=get_env_var("ARTIFACT_S3_BUCKET"),
            artifact_s3_prefix=get_env_var("ARTIFACT_S3_PREFIX", "mcp-index"),
            artifact_routing_default_repo_visibility=get_env_var(
                "ARTIFACT_ROUTING_DEFAULT_REPO_VISIBILITY", "private"
            ),
            artifact_routing_default_artifact_size_bytes=int(
                get_env_var("ARTIFACT_ROUTING_DEFAULT_ARTIFACT_SIZE_BYTES", "0")
            ),
            artifact_routing_default_profile_type=get_env_var(
                "ARTIFACT_ROUTING_DEFAULT_PROFILE_TYPE", "standard"
            ),
            artifact_routing_large_threshold_bytes=int(
                get_env_var("ARTIFACT_ROUTING_LARGE_THRESHOLD_BYTES", str(50 * 1024 * 1024))
            ),
            artifact_routing_fallback_order=get_env_var(
                "ARTIFACT_ROUTING_FALLBACK_ORDER", "local_fs,github_actions"
            ),
            artifact_delta_enabled=get_env_var("ARTIFACT_DELTA_ENABLED", "false").lower() == "true",
            voyage_api_key=get_env_var("VOYAGE_API_KEY"),
            openai_api_key=get_env_var("OPENAI_API_KEY"),
            openai_api_base=get_env_var("OPENAI_API_BASE", "http://localhost:8001/v1"),
            qdrant_host=get_env_var("QDRANT_HOST", "localhost"),
            qdrant_port=int(get_env_var("QDRANT_PORT", "6333")),
            qdrant_grpc_port=int(get_env_var("QDRANT_GRPC_PORT", "6334")),
            semantic_embedding_model=get_env_var("SEMANTIC_EMBEDDING_MODEL", "voyage-code-3"),
            semantic_collection_name=get_env_var("SEMANTIC_COLLECTION_NAME", "code-embeddings"),
            semantic_profiles_json=get_env_var("SEMANTIC_PROFILES_JSON"),
            semantic_default_profile=get_env_var("SEMANTIC_DEFAULT_PROFILE", "oss_high"),
            semantic_autostart_qdrant=get_env_var("SEMANTIC_AUTOSTART_QDRANT", "true").lower()
            == "true",
            semantic_strict_mode=get_env_var("SEMANTIC_STRICT_MODE", "false").lower() == "true",
            semantic_preflight_timeout_seconds=int(
                get_env_var("SEMANTIC_PREFLIGHT_TIMEOUT_SECONDS", "10")
            ),
            qdrant_compose_file=get_env_var("QDRANT_COMPOSE_FILE", "docker-compose.qdrant.yml"),
        )

        # Environment-specific overrides
        if env == Environment.PRODUCTION:
            settings.debug = False
            settings.workers = int(get_env_var("WORKERS", "4"))
        elif env == Environment.DEVELOPMENT:
            settings.debug = get_env_var("DEBUG", "true").lower() == "true"
            settings.workers = 1

        return settings

    @model_validator(mode="after")
    def validate_production_settings(self):
        """Validate settings for production environment."""
        if self.environment == Environment.PRODUCTION:
            # Production validation
            if self.debug:
                raise ValueError("Debug mode must be disabled in production")

            if self.security and len(self.security.jwt_secret_key) < 32:
                raise ValueError("JWT secret key must be at least 32 characters in production")

        return self

    def get_database_url(self) -> str:
        """Get the database connection URL."""
        return self.database.url

    def is_redis_enabled(self) -> bool:
        """Check if Redis caching is enabled."""
        return self.cache.redis_url is not None

    def is_prometheus_enabled(self) -> bool:
        """Check if Prometheus metrics are enabled."""
        return self.metrics.prometheus_enabled

    def _load_profiles_yaml(self) -> Dict[str, Any]:
        """Load semantic profile YAML payload when available."""
        yaml_path = _find_profiles_yaml()
        if not yaml_path:
            return {}
        with open(yaml_path, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    def get_semantic_profiles_config(self) -> Dict[str, Dict[str, Any]]:
        """Get configured semantic profile definitions.

        If no explicit profile JSON is configured, returns a legacy-default profile
        derived from existing semantic settings for backward compatibility.
        """
        if self.semantic_profiles_json:
            try:
                parsed = json.loads(self.semantic_profiles_json)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid SEMANTIC_PROFILES_JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError("SEMANTIC_PROFILES_JSON must decode to an object")
            return parsed

        chunker_version = self._detect_treesitter_chunker_version()
        payload = self._load_profiles_yaml()
        profile_map = payload.get("profiles") or {}
        if isinstance(profile_map, dict) and profile_map:
            converted: Dict[str, Dict[str, Any]] = {}
            for profile_id, config in profile_map.items():
                model = config.get("model") or {}
                auth = config.get("auth") or {}
                serving = config.get("serving") or {}
                vllm = serving.get("vllm") or {}
                vector_store = config.get("vector_store") or {}
                metadata = config.get("metadata") or {}
                normalization = config.get("normalization") or {}
                summarization = config.get("summarization") or {}

                provider = str(config.get("provider", "voyage"))
                embedding_base = _resolve_semantic_base_url(
                    vllm.get("base_url") or self.openai_api_base,
                    primary_env="SEMANTIC_EMBEDDING_BASE_URL",
                    legacy_env="VLLM_EMBEDDING_BASE_URL",
                    fallback=self.openai_api_base,
                )
                embedding_api_key_env = str(auth.get("api_key_env", "OPENAI_API_KEY"))
                enrichment_base = _resolve_semantic_base_url(
                    summarization.get("base_url"),
                    primary_env="SEMANTIC_ENRICHMENT_BASE_URL",
                    legacy_env="VLLM_SUMMARIZATION_BASE_URL",
                    fallback="http://ai:8002/v1",
                )
                enrichment_api_key_env = str(
                    summarization.get("api_key_env", embedding_api_key_env)
                )
                enrichment_model = _expand_env_vars(
                    str(
                        summarization.get(
                            "model_name", os.environ.get("SEMANTIC_ENRICHMENT_MODEL", "chat")
                        )
                    )
                )
                embedding_model = str(model.get("name", self.semantic_embedding_model))

                converted[str(profile_id)] = {
                    "provider": provider,
                    "model_name": embedding_model,
                    "model_version": str(
                        model.get("version") or metadata.get("embed_schema_version") or "1"
                    ),
                    "vector_dimension": int(
                        model.get("output_dimension") or vector_store.get("vector_size") or 1024
                    ),
                    "distance_metric": str(vector_store.get("distance", "cosine")),
                    "normalization_policy": (
                        "l2" if normalization.get("l2_normalize_vectors") else "provider-default"
                    ),
                    "chunk_schema_version": str(
                        metadata.get("chunk_schema_version", self.index_schema_version)
                    ),
                    "chunker_version": chunker_version,
                    "build_metadata": {
                        "openai_api_base": embedding_base,
                        "openai_api_key_env": embedding_api_key_env,
                        "embedding_api_base": embedding_base,
                        "embedding_model_name": embedding_model,
                        "embedding_api_key_env": embedding_api_key_env,
                        "enrichment_api_base": enrichment_base,
                        "enrichment_model_name": enrichment_model,
                        "enrichment_api_key_env": enrichment_api_key_env,
                        "collection_name": str(
                            vector_store.get("collection", self.semantic_collection_name)
                        ),
                    },
                }

            return converted

        return {
            "legacy-default": {
                "provider": "voyage",
                "model_name": self.semantic_embedding_model,
                "model_version": "legacy",
                "vector_dimension": 1024,
                "distance_metric": "cosine",
                "normalization_policy": "provider-default",
                "chunk_schema_version": self.index_schema_version,
                "chunker_version": chunker_version,
            }
        }

    def get_semantic_default_profile(self) -> str:
        """Resolve semantic default profile with safe fallback behavior."""
        configured = (self.semantic_default_profile or "").strip()
        profiles = self.get_semantic_profiles_config()
        if configured and configured in profiles:
            return configured
        if configured and configured != "legacy-default" and configured not in profiles:
            raise ValueError(
                f"Configured SEMANTIC_DEFAULT_PROFILE '{configured}' not found in profile set"
            )
        return next(iter(profiles.keys()))

    def get_profile_summarization_config(self, profile_id: str) -> Dict[str, Any]:
        """Return summarization config for a profile merged with settings-level fallback models.

        Always returns a dict. The ``base_url`` key is only present when the profile
        defines a primary summarization endpoint. Fallback model names are always
        included so ``ChunkWriter`` can use them without reading env vars directly.
        """
        profile_cfg: Dict[str, Any] = {}
        if self.semantic_profiles_json:
            metadata = (
                self.get_semantic_profiles_config().get(profile_id, {}).get("build_metadata") or {}
            )
            if metadata.get("enrichment_api_base"):
                profile_cfg = {
                    "base_url": metadata["enrichment_api_base"],
                    "model_name": metadata.get("enrichment_model_name") or "chat",
                    "api_key_env": metadata.get("enrichment_api_key_env") or "OPENAI_API_KEY",
                }
        else:
            payload = self._load_profiles_yaml()
            profile_map = payload.get("profiles") or {}
            profile_cfg = dict((profile_map.get(profile_id) or {}).get("summarization") or {})
        if profile_cfg and not self.semantic_profiles_json:
            profile_cfg["base_url"] = _resolve_semantic_base_url(
                profile_cfg.get("base_url"),
                primary_env="SEMANTIC_ENRICHMENT_BASE_URL",
                legacy_env="VLLM_SUMMARIZATION_BASE_URL",
                fallback=profile_cfg.get("base_url") or "http://ai:8002/v1",
            )
            if "model_name" in profile_cfg:
                profile_cfg["model_name"] = _expand_env_vars(str(profile_cfg["model_name"]))

        return {
            **profile_cfg,
            # Settings-controlled fallback models (env vars override defaults)
            "cerebras_model": self.summarization.cerebras_model,
            "anthropic_model": self.summarization.anthropic_model,
            "openai_model": self.summarization.openai_model,
        }

    def _detect_treesitter_chunker_version(self) -> str:
        """Return installed treesitter-chunker version label for metadata."""
        try:
            return f"treesitter-chunker@{version('treesitter-chunker')}"
        except PackageNotFoundError:
            return "treesitter-chunker@not-installed"


# ---------------------------------------------------------------------------
# Deployment profiles (IF-0-INFERPROFILES-1)
# ---------------------------------------------------------------------------
#
# A profile is an explicit, declarative statement of an inference posture: which
# embedding provider is used, which reranker path is taken, whether commercial
# source-code egress is involved (with a human-readable disclosure), and whether
# the profile depends on learned models at all.
#
# This registry is intentionally PURE DATA (identifier strings + enums). It does
# NOT import ``embedding_providers`` or ``reranker`` at module scope: those pull
# heavy optional dependencies (voyageai, torch) and settings.py is imported
# broadly. Live construction of a provider/reranker is a caller concern; the
# registry only declares the wiring so that resolution stays network-free and
# side-effect-free. In particular, resolving/degrading a profile NEVER touches a
# Qdrant collection — collection lifecycle stays fail-closed in INFERSAFE's
# ``ensure_qdrant_collection`` (default ``allow_recreate=False``), and this
# module never calls it.


class DeploymentProfile(str, Enum):
    """The four supported inference deployment postures."""

    #: Optional local/in-process inference. In-process rerank (FlashRank /
    #: cross-encoder) via the RERANKEND standalone-profile gate; optional local
    #: embedding if configured. No commercial egress. Learned models opt-in.
    STANDALONE_LOCAL = "standalone_local"

    #: Endpoint inference against a private OpenAI-compatible / ai-stack endpoint
    #: (OpenAI-compatible embeddings + ``EndpointReranker``) plus Qdrant. No
    #: commercial egress.
    FLEET_LOCAL = "fleet_local"

    #: Commercial endpoint providers (Voyage embeddings / Cohere reranking).
    #: MUST be opt-in and is visibly marked as source-code egress.
    COMMERCIAL = "commercial"

    #: No learned-model dependency at all: BM25 / symbol search only. This is the
    #: dependency-free path and the safe degradation target.
    LEXICAL_ONLY = "lexical_only"


class RerankerPath(str, Enum):
    """Which reranker construction path a profile declares."""

    #: No reranking (lexical/symbol ordering only).
    NONE = "none"

    #: In-process reranker behind ``RerankerFactory.create_standalone_reranker``
    #: (requires ``standalone_profile=True``).
    STANDALONE_IN_PROCESS = "standalone_in_process"

    #: ``EndpointReranker`` over the ``rerank.v1`` wire contract (private stack).
    ENDPOINT = "endpoint"

    #: Commercial endpoint reranker (Cohere ``/v2/rerank`` adapter). Egress.
    COMMERCIAL_ENDPOINT = "commercial_endpoint"


#: Safe default posture. Deliberately the dependency-free, no-egress profile so
#: that neither commercial egress nor learned-model dependency is ever selected
#: implicitly (INFERGATE owns any default-enablement decision).
DEFAULT_DEPLOYMENT_PROFILE = DeploymentProfile.LEXICAL_ONLY

_NO_EGRESS_DISCLOSURE = (
    "No source-code egress: inference stays on operator-controlled infrastructure."
)


class CommercialEgressNotOptedIn(ValueError):
    """Raised when a commercial (source-code egress) profile is selected without
    an explicit opt-in. Commercial egress is never implicit."""


@dataclass(frozen=True)
class DeploymentProfileContract:
    """Declarative contract for one deployment profile.

    Attributes:
        profile: The :class:`DeploymentProfile` this contract describes.
        embedding_provider: Embedding provider *name string* (as understood by
            ``create_embedding_provider``), or ``None`` when the profile uses no
            embedding provider (``lexical_only``).
        embedding_provider_locality: Human label for where embeddings run
            (``"local"``, ``"private_endpoint"``, ``"commercial"``, ``"none"``).
        reranker_path: Which :class:`RerankerPath` the profile declares.
        requires_learned_models: Whether the profile depends on any learned model
            (embedding or reranker). ``lexical_only`` is the only ``False``.
        commercial_egress: Whether resolving this profile sends source code to a
            third-party commercial API.
        egress_disclosure: Human-readable disclosure string (always present).
        fallback_profile: Profile to degrade to on provider unavailability, or
            ``None`` if the profile is already the terminal fallback. Fallbacks
            never escalate into commercial egress.
        description: Operator-facing one-line description.
    """

    profile: DeploymentProfile
    embedding_provider: Optional[str]
    embedding_provider_locality: str
    reranker_path: RerankerPath
    requires_learned_models: bool
    commercial_egress: bool
    egress_disclosure: str
    fallback_profile: Optional[DeploymentProfile]
    description: str

    @property
    def is_lexical_only(self) -> bool:
        return self.profile is DeploymentProfile.LEXICAL_ONLY


#: The frozen profile registry. Keep this declarative; add construction logic in
#: callers, not here.
DEPLOYMENT_PROFILES: Dict[DeploymentProfile, DeploymentProfileContract] = {
    DeploymentProfile.STANDALONE_LOCAL: DeploymentProfileContract(
        profile=DeploymentProfile.STANDALONE_LOCAL,
        embedding_provider="openai_compatible",
        embedding_provider_locality="local",
        reranker_path=RerankerPath.STANDALONE_IN_PROCESS,
        requires_learned_models=True,
        commercial_egress=False,
        egress_disclosure=_NO_EGRESS_DISCLOSURE,
        fallback_profile=DeploymentProfile.LEXICAL_ONLY,
        description=(
            "Optional local/in-process inference (in-process FlashRank / "
            "cross-encoder rerank via the standalone profile flag; local "
            "embedding if configured). Learned models are opt-in."
        ),
    ),
    DeploymentProfile.FLEET_LOCAL: DeploymentProfileContract(
        profile=DeploymentProfile.FLEET_LOCAL,
        embedding_provider="openai_compatible",
        embedding_provider_locality="private_endpoint",
        reranker_path=RerankerPath.ENDPOINT,
        requires_learned_models=True,
        commercial_egress=False,
        egress_disclosure=_NO_EGRESS_DISCLOSURE,
        fallback_profile=DeploymentProfile.LEXICAL_ONLY,
        description=(
            "Endpoint inference via a private OpenAI-compatible / ai-stack "
            "endpoint (OpenAI-compatible embeddings + EndpointReranker) plus "
            "Qdrant. No commercial egress."
        ),
    ),
    DeploymentProfile.COMMERCIAL: DeploymentProfileContract(
        profile=DeploymentProfile.COMMERCIAL,
        embedding_provider="voyage",
        embedding_provider_locality="commercial",
        reranker_path=RerankerPath.COMMERCIAL_ENDPOINT,
        requires_learned_models=True,
        commercial_egress=True,
        egress_disclosure=(
            "SOURCE-CODE EGRESS: source code is sent to third-party commercial "
            "APIs (Voyage AI embeddings and/or Cohere reranking). This profile "
            "must be explicitly opted into and is never selected implicitly."
        ),
        fallback_profile=DeploymentProfile.LEXICAL_ONLY,
        description=(
            "Commercial endpoint providers (Voyage / Cohere). Opt-in only and "
            "visibly marked as source-code egress."
        ),
    ),
    DeploymentProfile.LEXICAL_ONLY: DeploymentProfileContract(
        profile=DeploymentProfile.LEXICAL_ONLY,
        embedding_provider=None,
        embedding_provider_locality="none",
        reranker_path=RerankerPath.NONE,
        requires_learned_models=False,
        commercial_egress=False,
        egress_disclosure=_NO_EGRESS_DISCLOSURE,
        fallback_profile=None,
        description=(
            "No learned-model dependency at all (BM25 / symbol search only). "
            "Dependency-free path and the safe degradation target."
        ),
    ),
}


def _coerce_profile(profile: "DeploymentProfile | str") -> DeploymentProfile:
    """Coerce a string or enum into a :class:`DeploymentProfile`."""
    if isinstance(profile, DeploymentProfile):
        return profile
    try:
        return DeploymentProfile(str(profile).strip().lower())
    except ValueError as exc:
        valid = ", ".join(p.value for p in DeploymentProfile)
        raise ValueError(
            f"Unknown deployment profile {profile!r}. Valid profiles: {valid}."
        ) from exc


def resolve_deployment_profile(
    profile: "DeploymentProfile | str" = DEFAULT_DEPLOYMENT_PROFILE,
    *,
    allow_commercial: bool = False,
) -> DeploymentProfileContract:
    """Resolve a deployment profile name/enum into its declarative contract.

    Commercial egress is never implicit: selecting the ``commercial`` profile
    requires ``allow_commercial=True`` (the operator's explicit opt-in). Any
    other profile resolves without egress.

    Args:
        profile: Profile name string or :class:`DeploymentProfile`. Defaults to
            the safe, dependency-free :data:`DEFAULT_DEPLOYMENT_PROFILE`.
        allow_commercial: Must be ``True`` to resolve the ``commercial`` profile.

    Returns:
        The frozen :class:`DeploymentProfileContract` for the profile.

    Raises:
        ValueError: on an unknown profile name.
        CommercialEgressNotOptedIn: when a commercial-egress profile is selected
            without ``allow_commercial=True``.
    """
    resolved = _coerce_profile(profile)
    contract = DEPLOYMENT_PROFILES[resolved]
    if contract.commercial_egress and not allow_commercial:
        raise CommercialEgressNotOptedIn(
            f"Profile '{resolved.value}' involves source-code egress and must be "
            "opted into explicitly (allow_commercial=True). "
            f"{contract.egress_disclosure}"
        )
    return contract


#: Env var naming the active deployment profile (value is a ``DeploymentProfile``
#: string, e.g. ``"fleet_local"``). Absent -> the safe ``lexical_only`` default.
ACTIVE_PROFILE_ENV = "MCP_DEPLOYMENT_PROFILE"

#: Env var that operators must set truthy (e.g. ``1``) to opt into commercial
#: source-code egress. Absent/falsey -> commercial resolution is refused.
ALLOW_COMMERCIAL_EGRESS_ENV = "MCP_ALLOW_COMMERCIAL_EGRESS"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_truthy(value: Optional[str]) -> bool:
    """Return ``True`` when *value* is an explicit truthy opt-in token."""
    return value is not None and value.strip().lower() in _TRUTHY


def resolve_active_deployment_profile(
    env: Optional[Mapping[str, str]] = None,
) -> DeploymentProfileContract:
    """Resolve the *active* deployment profile from the environment.

    This is the stable entry point that the semantic-indexer and dispatcher
    lanes call to gate live provider/reranker construction. It reads the active
    profile name from :data:`ACTIVE_PROFILE_ENV` (``MCP_DEPLOYMENT_PROFILE``),
    defaulting to the safe, dependency-free :data:`DEFAULT_DEPLOYMENT_PROFILE`
    (``lexical_only``) when unset.

    Commercial egress is never implicit: selecting the ``commercial`` profile
    requires the operator to have explicitly opted in via
    :data:`ALLOW_COMMERCIAL_EGRESS_ENV` (``MCP_ALLOW_COMMERCIAL_EGRESS=1``).
    Without that opt-in, resolving ``commercial`` raises
    :class:`CommercialEgressNotOptedIn`.

    Args:
        env: Optional environment mapping to read from (defaults to
            ``os.environ``). Injectable so callers/tests need not mutate the
            process environment.

    Returns:
        The frozen :class:`DeploymentProfileContract` for the active profile.

    Raises:
        ValueError: when ``MCP_DEPLOYMENT_PROFILE`` names an unknown profile.
        CommercialEgressNotOptedIn: when ``commercial`` is selected without the
            explicit ``MCP_ALLOW_COMMERCIAL_EGRESS`` opt-in.
    """
    environ = os.environ if env is None else env
    profile_name = environ.get(ACTIVE_PROFILE_ENV, DEFAULT_DEPLOYMENT_PROFILE.value)
    allow_commercial = _env_truthy(environ.get(ALLOW_COMMERCIAL_EGRESS_ENV))
    return resolve_deployment_profile(profile_name, allow_commercial=allow_commercial)


def deployment_profile_is_explicitly_set(
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Return ``True`` only when the operator explicitly selected a profile.

    This is the LEGACY discriminator: :func:`resolve_active_deployment_profile`
    defaults an unset environment to ``lexical_only`` (the safe *contract*
    default), but enforcement lanes must distinguish "operator never opted into
    the profile system" (env var absent) from "operator explicitly chose
    lexical_only". Only the former preserves pre-existing, unrestricted behavior.
    """
    environ = os.environ if env is None else env
    return ACTIVE_PROFILE_ENV in environ


def commercial_egress_allowed(env: Optional[Mapping[str, str]] = None) -> bool:
    """Return ``True`` when constructing a commercial (egress) provider is allowed.

    Backward-compatible policy:

    * Profile **unset** (env var absent) -> ``True`` (LEGACY: the operator never
      opted into the profile system, so preserve pre-existing behavior where a
      configured commercial provider builds without restriction).
    * Profile **explicitly** set to ``commercial`` **with**
      ``MCP_ALLOW_COMMERCIAL_EGRESS=1`` -> ``True`` (explicit opt-in).
    * Profile explicitly set to any other value, or ``commercial`` without the
      opt-in flag -> ``False``.

    Never raises: an unknown explicit profile name is treated as not permitting
    commercial egress (fail-closed). Use :func:`resolve_active_deployment_profile`
    when a raising contract check is desired.
    """
    if not deployment_profile_is_explicitly_set(env):
        return True  # LEGACY: unset -> allow (pre-existing behavior).
    environ = os.environ if env is None else env
    try:
        profile = _coerce_profile(environ.get(ACTIVE_PROFILE_ENV, ""))
    except ValueError:
        return False  # Unknown explicit profile: fail closed.
    contract = DEPLOYMENT_PROFILES[profile]
    if not contract.commercial_egress:
        return False
    return _env_truthy(environ.get(ALLOW_COMMERCIAL_EGRESS_ENV))


def learned_models_allowed(env: Optional[Mapping[str, str]] = None) -> bool:
    """Return ``True`` unless the profile is EXPLICITLY ``lexical_only``.

    ``lexical_only`` is the only *known* profile that forbids ALL learned
    providers and rerankers (BM25 / symbol search only). When the profile is
    unset (legacy) or set to any learned-model profile, learned providers are
    permitted. An unknown explicit profile name is treated as forbidding learned
    models (fail-closed), symmetric with :func:`commercial_egress_allowed`; use
    :func:`resolve_active_deployment_profile` when a raising contract check is
    desired.
    """
    if not deployment_profile_is_explicitly_set(env):
        return True  # LEGACY: unset -> allow.
    environ = os.environ if env is None else env
    try:
        profile = _coerce_profile(environ.get(ACTIVE_PROFILE_ENV, ""))
    except ValueError:
        return False  # Unknown explicit profile: fail closed.
    return profile is not DeploymentProfile.LEXICAL_ONLY


@dataclass(frozen=True)
class DegradationDiagnostic:
    """Structured diagnostic describing the ACTUAL path a resolution took.

    ``degraded`` is ``True`` when the requested profile's provider was
    unavailable and the resolver fell back. The diagnostic always names both the
    failed component and the requested/active profiles so an operator can see the
    real path taken. ``collection_mutated`` documents the invariant that a
    provider outage must never mutate collection metadata (this resolver never
    touches a collection; INFERSAFE's fail-closed ``ensure_qdrant_collection``
    enforces the same on the write path).
    """

    requested_profile: DeploymentProfile
    active_profile: DeploymentProfile
    degraded: bool
    failed_component: Optional[str]
    reason: Optional[str]
    actual_path: str
    collection_mutated: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "requested_profile": self.requested_profile.value,
            "active_profile": self.active_profile.value,
            "degraded": self.degraded,
            "failed_component": self.failed_component,
            "reason": self.reason,
            "actual_path": self.actual_path,
            "collection_mutated": self.collection_mutated,
        }


def apply_degradation_policy(
    contract: DeploymentProfileContract,
    *,
    probe: Optional[Callable[[], None]] = None,
) -> Tuple[DeploymentProfileContract, DegradationDiagnostic]:
    """Apply the documented degradation policy for a resolved profile.

    The policy: probe the profile's inference provider; if it is unavailable,
    degrade to the profile's declared ``fallback_profile`` (always the
    dependency-free ``lexical_only`` for learned-model profiles) and emit a
    diagnostic naming the failed component and the actual path taken. A provider
    outage NEVER escalates into a commercial-egress profile, and this function
    performs NO collection mutation (no create/recreate) — it is pure config
    resolution, composing with INFERSAFE's fail-closed collection lifecycle.

    Args:
        contract: The already-resolved profile contract (from
            :func:`resolve_deployment_profile`).
        probe: Optional zero-arg callable that raises when the profile's provider
            is unavailable. When ``None``, the profile is assumed healthy.

    Returns:
        ``(active_contract, diagnostic)``. On success ``active_contract`` is the
        input contract; on degradation it is the fallback contract.
    """
    component = contract.embedding_provider or contract.reranker_path.value

    if probe is None:
        return contract, DegradationDiagnostic(
            requested_profile=contract.profile,
            active_profile=contract.profile,
            degraded=False,
            failed_component=None,
            reason=None,
            actual_path=f"{contract.profile.value} (provider assumed available)",
            collection_mutated=False,
        )

    try:
        probe()
    except Exception as exc:  # noqa: BLE001 - policy degrades on any provider fault
        fallback = contract.fallback_profile or DeploymentProfile.LEXICAL_ONLY
        fallback_contract = DEPLOYMENT_PROFILES[fallback]
        # Invariant: degradation must never escalate into commercial egress.
        if fallback_contract.commercial_egress:
            raise AssertionError("degradation fallback must never be a commercial-egress profile")
        actual_path = (
            f"{contract.profile.value} -> {fallback.value} "
            f"(component '{component}' unavailable)"
        )
        return fallback_contract, DegradationDiagnostic(
            requested_profile=contract.profile,
            active_profile=fallback,
            degraded=True,
            failed_component=component,
            reason=f"{type(exc).__name__}: {exc}",
            actual_path=actual_path,
            collection_mutated=False,
        )

    return contract, DegradationDiagnostic(
        requested_profile=contract.profile,
        active_profile=contract.profile,
        degraded=False,
        failed_component=None,
        reason=None,
        actual_path=f"{contract.profile.value} (provider available)",
        collection_mutated=False,
    )


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """
    Get application settings singleton.

    Returns:
        Settings: Application settings instance
    """
    global _settings
    if _settings is None:
        _settings = Settings.from_environment()
    return _settings


def reload_settings() -> Settings:
    """
    Reload settings from environment variables.

    Returns:
        Settings: New settings instance
    """
    global _settings
    _settings = Settings.from_environment()
    return _settings
