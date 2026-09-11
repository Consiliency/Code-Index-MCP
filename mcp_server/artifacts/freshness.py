"""Artifact freshness verification (IF-0-P12-5)."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path


class FreshnessVerdict(str, Enum):
    FRESH = "fresh"
    STALE_COMMIT = "stale_commit"
    STALE_AGE = "stale_age"
    INVALID = "invalid"


def verify_artifact_freshness(
    meta: dict, head_commit: str, max_age_days: int, *, repo_path: Path | str | None = None
) -> FreshnessVerdict:
    """Return a verdict for the artifact described by *meta*.

    Args:
        meta: Artifact metadata dict (must have 'commit' and 'timestamp').
        head_commit: The current HEAD commit ref to check ancestry against.
        max_age_days: Maximum acceptable artifact age in days.
    """
    commit = meta.get("commit")
    timestamp_str = meta.get("timestamp")

    if not commit or not timestamp_str:
        return FreshnessVerdict.INVALID

    try:
        ts = datetime.fromisoformat(timestamp_str.rstrip("Z")).replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return FreshnessVerdict.INVALID

    # Check commit ancestry first — if not an ancestor, report STALE_COMMIT.
    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, head_commit],
            check=True,
            capture_output=True,
            cwd=repo_path,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return FreshnessVerdict.STALE_COMMIT

    # Check age.
    age = datetime.now(timezone.utc) - ts
    if age > timedelta(days=max_age_days):
        return FreshnessVerdict.STALE_AGE

    return FreshnessVerdict.FRESH
