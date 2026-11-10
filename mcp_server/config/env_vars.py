"""Lazy env-var getters — each reads os.getenv on every call; no module-scope caching."""

import math
import os
from typing import Optional


def get_max_file_size_bytes() -> int:
    return int(os.getenv("MCP_MAX_FILE_SIZE_BYTES", str(10 * 1024 * 1024)))


def get_artifact_retention_count() -> int:
    return int(os.getenv("MCP_ARTIFACT_RETENTION_COUNT", "10"))


def get_artifact_retention_days() -> int:
    return int(os.getenv("MCP_ARTIFACT_RETENTION_DAYS", "30"))


#: Superseded index generations kept for rollback besides the live one.
DEFAULT_INDEX_GENERATION_RETENTION = 2


def get_index_generation_retention() -> int:
    """Return how many superseded generations to keep for rollback (at least 1)."""
    raw = os.getenv("MCP_INDEX_GENERATION_RETENTION")
    try:
        value = int(raw) if raw is not None else DEFAULT_INDEX_GENERATION_RETENTION
    except ValueError:
        value = DEFAULT_INDEX_GENERATION_RETENTION
    return max(1, value)


#: Seconds a SQLite generation snapshot may copy no new pages before it fails.
DEFAULT_INDEX_SNAPSHOT_STALL_SECONDS = 120.0
MIN_INDEX_SNAPSHOT_STALL_SECONDS = 5.0

#: Seconds one git subprocess of a staged rebuild may run before it fails.
DEFAULT_INDEX_GIT_TIMEOUT_SECONDS = 300.0
MIN_INDEX_GIT_TIMEOUT_SECONDS = 10.0


def _float_env(name: str, default: float, floor: float) -> float:
    raw = os.getenv(name)
    try:
        value = float(raw) if raw is not None else default
    except ValueError:
        value = default
    if not math.isfinite(value):  # inf/NaN would disable the guard entirely
        value = default
    return max(floor, value)


def get_index_snapshot_stall_seconds() -> float:
    """Return the no-progress window for a generation snapshot (at least 5 s)."""
    return _float_env(
        "MCP_INDEX_SNAPSHOT_STALL_SECONDS",
        DEFAULT_INDEX_SNAPSHOT_STALL_SECONDS,
        MIN_INDEX_SNAPSHOT_STALL_SECONDS,
    )


def get_index_git_timeout_seconds() -> float:
    """Return the per-command git timeout for a staged rebuild (at least 10 s)."""
    return _float_env(
        "MCP_INDEX_GIT_TIMEOUT_SECONDS",
        DEFAULT_INDEX_GIT_TIMEOUT_SECONDS,
        MIN_INDEX_GIT_TIMEOUT_SECONDS,
    )


def get_disk_readonly_threshold_mb() -> int:
    return int(os.getenv("MCP_DISK_READONLY_THRESHOLD_MB", "100"))


def get_publish_rollback_enabled() -> bool:
    raw = os.getenv("MCP_PUBLISH_ROLLBACK_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class QdrantAuthConfigError(RuntimeError):
    """Server-mode Qdrant needs an API key, but the client has none configured."""


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def get_qdrant_api_key() -> Optional[str]:
    """API key for server-mode Qdrant from QDRANT_API_KEY; unset or blank means None."""
    return os.getenv("QDRANT_API_KEY", "").strip() or None


def qdrant_api_key_required() -> bool:
    """Whether server-mode Qdrant clients must send an API key.

    Defaults to True. ``QDRANT_REQUIRE_API_KEY=false`` is an explicit opt-out for a
    disposable, unauthenticated test server only; it is not a rollback path.
    """
    raw = os.getenv("QDRANT_REQUIRE_API_KEY", "").strip().lower()
    if not raw or raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise QdrantAuthConfigError("QDRANT_REQUIRE_API_KEY must be true or false")


def resolve_qdrant_server_api_key() -> Optional[str]:
    """API key for a server-mode (http/https) Qdrant client; fails closed when missing.

    Raises QdrantAuthConfigError when a key is required and QDRANT_API_KEY is unset or
    blank. Never includes the key value in any message.
    """
    required = qdrant_api_key_required()
    api_key = get_qdrant_api_key()
    if api_key is None and required:
        raise QdrantAuthConfigError(
            "Server-mode Qdrant requires an API key: set QDRANT_API_KEY to the server's "
            "QDRANT__SERVICE__API_KEY. Set QDRANT_REQUIRE_API_KEY=false only for a "
            "disposable, unauthenticated test server."
        )
    return api_key


#: Bounded summary passes per repo-scope semantic rebuild (each pass summarizes one file).
DEFAULT_SEMANTIC_SUMMARY_PASS_BUDGET = 8


def get_semantic_summary_pass_budget() -> Optional[int]:
    """Return the repo-scope summary pass budget, or None when unbounded.

    Reads SEMANTIC_SUMMARY_PASS_BUDGET. ``0`` means unbounded; unset, blank,
    non-integer or negative values fall back to the default.
    """
    raw = (os.getenv("SEMANTIC_SUMMARY_PASS_BUDGET") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_SEMANTIC_SUMMARY_PASS_BUDGET
    except ValueError:
        value = DEFAULT_SEMANTIC_SUMMARY_PASS_BUDGET
    if value < 0:
        value = DEFAULT_SEMANTIC_SUMMARY_PASS_BUDGET
    return value or None
