"""ArtifactPublisher — commit-SHA-keyed release creation with atomic latest pointer (IF-0-P13-4)."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from uuid import uuid4

from mcp_server.artifacts.attestation import attest
from mcp_server.artifacts.delta_policy import DeltaPolicy
from mcp_server.core.errors import MCPError

try:
    from mcp_server.metrics.prometheus_exporter import (
        mcp_artifact_errors_by_class_total,
    )
except ImportError:
    mcp_artifact_errors_by_class_total = None

if TYPE_CHECKING:
    from .artifact_upload import IndexArtifactUploader

logger = logging.getLogger(__name__)


class ArtifactError(MCPError):
    """Raised when a gh CLI operation fails during artifact publishing."""


@dataclass(frozen=True)
class ArtifactRef:
    repo_id: str
    commit: str
    tag: str
    release_url: str
    is_latest: bool
    tracked_branch: str = ""


class ArtifactPublisher:
    """Publish commit-SHA-keyed GitHub releases and atomically update index-latest."""

    def __init__(self, uploader: "IndexArtifactUploader", *, gh_cmd: str = "gh") -> None:
        self._uploader = uploader
        self._gh_cmd = gh_cmd

    # ------------------------------------------------------------------
    # Public API (frozen by IF-0-P13-4)
    # ------------------------------------------------------------------

    def publish_on_reindex(
        self,
        repo_id: str,
        commit: str,
        *,
        tracked_branch: str = "main",
        index_location: Path | str | None = None,
        index_path: Path | str | None = None,
        repo_path: Path | str | None = None,
        semantic_indexer=None,
    ) -> ArtifactRef:
        """Idempotent publish: creates a SHA-keyed release and atomically moves index-latest.

        Calling twice with the same (repo_id, commit) returns the same ArtifactRef.
        The losing side of a concurrent race still has its SHA-keyed release reachable;
        only is_latest differs.
        """
        short_sha = commit[:7]
        safe_repo = repo_id.replace("/", "_").replace(":", "_")
        safe_branch = tracked_branch.replace("/", "_").replace(":", "_")
        sha_tag = f"index-{safe_repo}-{safe_branch}-{short_sha}"
        repo = self._uploader.repo
        release_url = f"https://github.com/{repo}/releases/tag/{sha_tag}"

        try:
            if repo_path is not None:
                from .artifact_upload import IndexArtifactUploader

                selected_repo = IndexArtifactUploader._detect_repository(self._uploader, repo_path)
                if selected_repo.casefold() != repo.casefold():
                    raise ArtifactError(
                        "Publisher destination does not match the selected repository"
                    )
            previous_artifact_id = self._get_latest_commit(repo)
            archive_path, checksum, size = self._uploader.compress_indexes(
                Path(f"index-archive-{safe_repo}-{safe_branch}-{short_sha}-{uuid4().hex}.tar.gz"),
                index_location=index_location,
                index_path=index_path,
                repo_path=repo_path or ".",
                semantic_indexer=semantic_indexer,
            )
            attestation = attest(archive_path, repo=repo, gh_cmd=self._gh_cmd)
            policy = DeltaPolicy()
            decision = policy.decide(
                compressed_size_bytes=size,
                previous_artifact_id=previous_artifact_id,
            )
            metadata = self._uploader.create_metadata(
                checksum,
                size,
                artifact_type=decision.strategy,
                delta_from=decision.base_artifact_id,
                attestation=attestation,
                repo_id=repo_id,
                tracked_branch=tracked_branch,
                commit=commit,
                index_location=index_location,
                index_path=index_path,
            )
            self._ensure_sha_release(sha_tag, commit, repo)
            # Keep prepared bytes and partial releases available for diagnosis/resume.
            self._uploader.upload_direct(
                archive_path,
                metadata,
                release_tag=sha_tag,
                attestation=attestation,
            )
            self._move_latest_pointer(sha_tag, commit, repo)
            is_latest = self._check_is_latest(commit, repo)
        except ArtifactError:
            raise
        except subprocess.CalledProcessError as exc:
            raise ArtifactError(
                f"gh CLI returned non-zero exit {exc.returncode}; publication evidence retained"
            ) from exc
        except Exception as exc:
            raise ArtifactError(
                f"publish_on_reindex failed ({type(exc).__name__}); prepared archive retained"
            ) from exc

        return ArtifactRef(
            repo_id=repo_id,
            commit=commit,
            tag=sha_tag,
            release_url=release_url,
            is_latest=is_latest,
            tracked_branch=tracked_branch,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_latest_commit(self, repo: str) -> Optional[str]:
        """Return the target commit of index-latest, or None if it doesn't exist."""
        result = subprocess.run(
            [
                self._gh_cmd,
                "release",
                "view",
                "index-latest",
                "--repo",
                repo,
                "--json",
                "targetCommitish",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
            return data.get("targetCommitish") or None
        except (json.JSONDecodeError, AttributeError):
            return None

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        """Run a gh sub-command, wrapping CalledProcessError in ArtifactError."""
        try:
            return subprocess.run(
                [self._gh_cmd] + args,
                capture_output=True,
                text=True,
                check=check,
            )
        except subprocess.CalledProcessError as exc:
            raise ArtifactError(
                f"gh CLI returned non-zero exit {exc.returncode}; publication evidence retained"
            ) from exc

    def _ensure_sha_release(self, sha_tag: str, commit: str, repo: str) -> bool:
        """Create the SHA-keyed release if it doesn't already exist (idempotent)."""
        result = subprocess.run(
            [self._gh_cmd, "release", "view", sha_tag, "--repo", repo],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return False  # Already exists — idempotent short-circuit

        # Create new SHA-keyed release
        self._run(
            [
                "release",
                "create",
                sha_tag,
                "--repo",
                repo,
                "--target",
                commit,
                "--title",
                f"Index: {sha_tag}",
                "--notes",
                f"Auto-published index artifact for commit {commit}",
            ]
        )
        return True

    def _move_latest_pointer(self, sha_tag: str, commit: str, repo: str) -> None:
        """Atomically move index-latest to point at commit via gh release edit (or create)."""
        result = subprocess.run(
            [self._gh_cmd, "release", "view", "index-latest", "--repo", repo],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # First-ever publish: create index-latest
            self._run(
                [
                    "release",
                    "create",
                    "index-latest",
                    "--repo",
                    repo,
                    "--target",
                    commit,
                    "--title",
                    "Index: latest",
                    "--notes",
                    f"Auto-updated index artifact. Commit: {commit}",
                ]
            )
        else:
            self._run(
                [
                    "release",
                    "edit",
                    "index-latest",
                    "--repo",
                    repo,
                    "--target",
                    commit,
                    "--title",
                    f"Index: latest ({commit[:8]})",
                ]
            )

    def _check_is_latest(self, commit: str, repo: str) -> bool:
        """Return True iff index-latest currently points at our commit."""
        result = subprocess.run(
            [
                self._gh_cmd,
                "release",
                "view",
                "index-latest",
                "--repo",
                repo,
                "--json",
                "targetCommitish",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False
        try:
            data = json.loads(result.stdout)
            return bool(data.get("targetCommitish", "") == commit)
        except (json.JSONDecodeError, AttributeError):
            return False
