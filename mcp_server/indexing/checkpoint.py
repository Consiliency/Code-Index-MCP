"""Checkpoint schema and helpers for durable reindex resume (IF-0-P13-1)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

REINDEX_STATE_VERSION: int = 1

_STATE_FILE = ".reindex-state"
_TMP_FILE = ".reindex-state.tmp"


@dataclass
class ReindexCheckpoint:
    repo_id: str
    started_at: str  # ISO-8601 UTC
    last_completed_path: str  # relative path; "" if none yet
    remaining_paths: List[str]  # relative paths still to process
    errors: List[dict] = field(default_factory=list)  # [{"path": ..., "error": ...}]
    schema_version: int = REINDEX_STATE_VERSION


def save(ckpt: ReindexCheckpoint, repo_root: Path) -> None:
    """Write checkpoint atomically via tmp+rename."""
    data = {
        "repo_id": ckpt.repo_id,
        "started_at": ckpt.started_at,
        "last_completed_path": ckpt.last_completed_path,
        "remaining_paths": ckpt.remaining_paths,
        "errors": ckpt.errors,
        "schema_version": ckpt.schema_version,
    }
    tmp = repo_root / _TMP_FILE
    final = repo_root / _STATE_FILE
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, final)  # atomic on POSIX


def load(repo_root: Path) -> Optional[ReindexCheckpoint]:
    """Return checkpoint or None if absent or schema mismatch."""
    state_file = repo_root / _STATE_FILE
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    if data.get("schema_version") != REINDEX_STATE_VERSION:
        return None
    return ReindexCheckpoint(
        repo_id=data["repo_id"],
        started_at=data["started_at"],
        last_completed_path=data["last_completed_path"],
        remaining_paths=data["remaining_paths"],
        errors=data.get("errors", []),
        schema_version=data["schema_version"],
    )


def clear(repo_root: Path) -> None:
    """Remove checkpoint file; no-op if absent."""
    state_file = repo_root / _STATE_FILE
    try:
        state_file.unlink()
    except FileNotFoundError:
        pass
