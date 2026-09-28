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


def get_qdrant_api_key() -> Optional[str]:
    """API key for server-mode Qdrant; unset or blank means the server needs no key."""
    return os.getenv("QDRANT_API_KEY", "").strip() or None
