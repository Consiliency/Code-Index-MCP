"""Lazy env-var getters — each reads os.getenv on every call; no module-scope caching."""

import os
from typing import Optional


def get_max_file_size_bytes() -> int:
    return int(os.getenv("MCP_MAX_FILE_SIZE_BYTES", str(10 * 1024 * 1024)))


def get_artifact_retention_count() -> int:
    return int(os.getenv("MCP_ARTIFACT_RETENTION_COUNT", "10"))


def get_artifact_retention_days() -> int:
    return int(os.getenv("MCP_ARTIFACT_RETENTION_DAYS", "30"))


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
