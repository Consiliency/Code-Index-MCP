"""Git-aware index manager for commit-synchronized indexing.

This module provides index management that's synchronized with git commits,
supporting incremental updates and artifact management.
"""

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..artifacts.commit_artifacts import CommitArtifactManager
from ..core.ignore_patterns import EXCLUDED_DIR_PARTS
from ..core.path_resolver import PathResolver
from ..core.repo_context import RepoContext
from ..core.repo_resolver import RepoResolver
from ..dispatcher.dispatcher_enhanced import (
    EnhancedDispatcher,
    IndexResult,
    IndexResultStatus,
)
from ..health.repo_status import build_health_row
from ..health.repository_readiness import (
    ReadinessClassifier,
    RepositoryReadiness,
    RepositoryReadinessState,
)
from ..indexing.change_detector import ChangeDetector
from ..indexing.lock_registry import lock_registry
from .repository_registry import RepositoryRegistry
from .sqlite_store import SQLiteStore
from .store_registry import StoreRegistry


def should_reindex_for_branch(current: Optional[str], tracked: Optional[str]) -> bool:
    """True iff both branches are non-None and equal."""
    if not current or not tracked:
        return False
    return current == tracked


logger = logging.getLogger(__name__)
_FORCE_FULL_EXIT_TRACE = "force_full_exit_trace.json"
_RECOVERABLE_REBUILD_STATES = frozenset(
    {
        RepositoryReadinessState.READY,
        RepositoryReadinessState.MISSING_INDEX,
        RepositoryReadinessState.INDEX_EMPTY,
        RepositoryReadinessState.STALE_COMMIT,
        RepositoryReadinessState.CORRUPT_SQLITE,
        RepositoryReadinessState.MISSING_SCHEMA,
        RepositoryReadinessState.MISSING_PROVENANCE,
        # CHUNKERSAFE Lane A: the staged full rebuild builds a fresh stage DB
        # (empty -> current scheme stamped on first write -> os.replace), which
        # naturally yields a clean single-scheme index. Allow it to run for a
        # scheme-mismatched or interrupted-rebuild index so recovery is possible.
        RepositoryReadinessState.SCHEME_MISMATCH,
        RepositoryReadinessState.INDEX_REBUILDING,
    }
)
_QUARANTINE_REBUILD_STATES = frozenset(
    {
        RepositoryReadinessState.CORRUPT_SQLITE,
        RepositoryReadinessState.MISSING_SCHEMA,
        RepositoryReadinessState.MISSING_PROVENANCE,
        # Preserve the pre-rebuild bytes of a scheme-mismatched / half-rebuilt
        # index for forensics before the atomic os.replace overwrites it.
        RepositoryReadinessState.SCHEME_MISMATCH,
        RepositoryReadinessState.INDEX_REBUILDING,
    }
)

# Callback type: (repo_id, current_branch, tracked_branch) -> None
_DriftCallback = Optional[Any]


@dataclass
class ChangeSet:
    """Represents file changes between commits."""

    added: List[str]
    modified: List[str]
    deleted: List[str]
    renamed: List[Tuple[str, str]]  # List of (old_path, new_path)

    def is_empty(self) -> bool:
        """Check if there are no changes."""
        return not (self.added or self.modified or self.deleted or self.renamed)

    def total_changes(self) -> int:
        """Get total number of changed files."""
        return len(self.added) + len(self.modified) + len(self.deleted) + len(self.renamed)


@dataclass
class IndexSyncResult:
    """Result of index synchronization operation."""

    action: str
    commit: str
    files_processed: int = 0
    error: Optional[str] = None
    duration_seconds: float = 0.0
    code: Optional[str] = None
    readiness: Optional[Dict[str, Any]] = None
    semantic: Optional[Dict[str, Any]] = None


@dataclass
class UpdateResult:
    """Result of incremental index update."""

    indexed: int = 0
    deleted: int = 0
    moved: int = 0
    failed: int = 0
    skipped: int = 0
    errors: List[str] = None
    semantic: Optional[Dict[str, Any]] = None
    low_level: Optional[Dict[str, Any]] = None
    duration_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    @property
    def files_processed(self) -> int:
        return self.indexed + self.deleted + self.moved

    @property
    def clean(self) -> bool:
        return self.failed == 0 and self.skipped == 0 and not self.errors


@dataclass
class RuntimeSnapshot:
    """Temporary backup of the active runtime before a force-full mutation."""

    backup_dir: Path
    db_path: Path
    qdrant_path: Path
    db_existed: bool
    qdrant_existed: bool
    counts_before: Dict[str, int]
    sqlite_sidecars: List[str]


@dataclass
class RuntimeRestoreResult:
    """Outcome of restoring or preserving the active runtime after a blocked run."""

    restored: bool
    mode: str
    counts_before: Dict[str, int]
    counts_after: Dict[str, int]


class GitAwareIndexManager:
    """Manages indexes synchronized with git commits."""

    def __init__(
        self,
        registry: RepositoryRegistry,
        dispatcher: Optional[EnhancedDispatcher] = None,
        repo_resolver: Optional[RepoResolver] = None,
        store_registry: Optional[StoreRegistry] = None,
    ):
        self.registry = registry
        self.dispatcher = dispatcher
        self.repo_resolver = repo_resolver
        self.store_registry = store_registry
        self.artifact_manager = CommitArtifactManager()
        # Wired post-construction by MultiRepositoryWatcher to avoid circular import.
        # Signature: (repo_id: str, current_branch: str, tracked_branch: str) -> None
        self.on_branch_drift: _DriftCallback = None

    def sync_repository_index(
        self, repo_id: str, force_full: bool = False, bypass_branch_guard: bool = False
    ) -> IndexSyncResult:
        """Serialize repository synchronization through the shared per-repo lock."""
        repo = self.registry.get_repository(repo_id)
        with lock_registry.acquire(repo_id, repo_path=repo.path if repo else None):
            return self._sync_repository_index_locked(
                repo_id,
                force_full=force_full,
                bypass_branch_guard=bypass_branch_guard,
            )

    def _sync_repository_index_locked(
        self, repo_id: str, force_full: bool = False, bypass_branch_guard: bool = False
    ) -> IndexSyncResult:
        """Sync index with repository's current git state.

        Args:
            repo_id: Repository ID
            force_full: Force full reindex instead of incremental
            bypass_branch_guard: Compatibility parameter; production sync paths always
                respect the tracked-branch guard.

        Returns:
            IndexSyncResult
        """
        start_time = datetime.now()

        repo_info = self.registry.get_repository(repo_id)
        if not repo_info:
            return IndexSyncResult(
                action="failed", commit="", error=f"Repository not found: {repo_id}"
            )

        repo_path = Path(repo_info.path)
        index_exists_before_mutation = self._index_exists(repo_info)

        # Update current git state
        git_state = self.registry.update_git_state(repo_id)
        current_commit = git_state.get("commit") if git_state else None
        if not current_commit:
            return IndexSyncResult(action="failed", commit="", error="Failed to get current commit")

        repo_info.current_commit = current_commit
        if git_state and git_state.get("branch"):
            repo_info.current_branch = git_state["branch"]
        last_indexed_commit = repo_info.last_indexed_commit
        current_branch = getattr(repo_info, "current_branch", None)

        if not should_reindex_for_branch(current_branch, repo_info.tracked_branch):
            # Distinguish true drift (both non-None, different) from unconfigured (tracked is None)
            if (
                current_branch
                and repo_info.tracked_branch
                and current_branch != repo_info.tracked_branch
            ):
                logger.warning(
                    "branch.drift.detected",
                    extra={
                        "repo_id": repo_id,
                        "current_branch": current_branch,
                        "tracked_branch": repo_info.tracked_branch,
                    },
                )
                readiness = RepositoryReadiness(
                    state=RepositoryReadinessState.WRONG_BRANCH,
                    repository_id=repo_id,
                    repository_name=getattr(repo_info, "name", None),
                    registered_path=str(Path(repo_info.path).resolve(strict=False)),
                    tracked_branch=repo_info.tracked_branch,
                    current_branch=current_branch,
                    current_commit=current_commit,
                    last_indexed_commit=last_indexed_commit,
                    index_path=(
                        str(Path(repo_info.index_path).resolve(strict=False))
                        if getattr(repo_info, "index_path", None)
                        else None
                    ),
                    remediation=(
                        f"Switch to the tracked branch '{repo_info.tracked_branch}' "
                        "or register the intended repository path."
                    ),
                )
                return IndexSyncResult(
                    action="wrong_branch",
                    commit=current_commit,
                    duration_seconds=(datetime.now() - start_time).total_seconds(),
                    code=RepositoryReadinessState.WRONG_BRANCH.value,
                    readiness=readiness.to_dict(),
                )
            else:
                logger.info(
                    "Skipping reindex for %s: current branch %r != tracked branch %r",
                    repo_id,
                    current_branch,
                    repo_info.tracked_branch,
                )
            return IndexSyncResult(
                action="up_to_date",
                commit=current_commit,
                duration_seconds=(datetime.now() - start_time).total_seconds(),
            )

        # Check if already up to date
        if (
            current_commit == last_indexed_commit
            and not force_full
            and not repo_info.staleness_reason
            and index_exists_before_mutation
        ):
            return IndexSyncResult(
                action="up_to_date",
                commit=current_commit,
                duration_seconds=(datetime.now() - start_time).total_seconds(),
            )

        if repo_info.staleness_reason in {"index_publication_pending", "partial_index_failure"}:
            return self._rebuild_repository_index_locked(repo_id)

        try:
            self.registry.begin_generation_mutation(
                repo_id,
                expected_registration_id=repo_info.registration_id,
                expected_generation=repo_info.index_generation,
            )
        except (OSError, ValueError):
            return IndexSyncResult(
                action="failed", commit=current_commit, error="Could not fence index mutation"
            )

        # Check for remote index artifact
        if repo_info.artifact_enabled and not force_full:
            if self._has_remote_artifact(repo_id, current_commit):
                # Download existing index for this commit
                if self._download_commit_index(repo_id, current_commit):
                    if self._index_exists(repo_info) and self.registry.update_indexed_commit(
                        repo_id,
                        current_commit,
                        branch=current_branch,
                        expected_registration_id=repo_info.registration_id,
                        expected_generation=repo_info.index_generation,
                    ):
                        repo_info.last_indexed_commit = current_commit
                        return IndexSyncResult(
                            action="downloaded",
                            commit=current_commit,
                            duration_seconds=(datetime.now() - start_time).total_seconds(),
                        )

        ctx = self._resolve_ctx(repo_id)
        if ctx is None:
            return IndexSyncResult(
                action="failed",
                commit=current_commit,
                error=f"Failed to resolve repository context: {repo_id}",
                duration_seconds=(datetime.now() - start_time).total_seconds(),
            )

        # Determine what changed since last index
        if last_indexed_commit and not force_full:
            try:
                changed_files = self._get_changed_files(
                    repo_path, last_indexed_commit, current_commit
                )
                if not changed_files.is_empty():
                    if self._should_full_reindex(repo_path, changed_files):
                        logger.info(
                            "Large change set detected for %s (%d files), using full reindex",
                            repo_id,
                            changed_files.total_changes(),
                        )
                    elif not index_exists_before_mutation:
                        logger.warning(
                            "No existing durable index for %s; using full reindex",
                            repo_id,
                        )
                    else:
                        # Incremental update - only reindex changed files
                        result = self._incremental_index_update(repo_id, ctx, changed_files)
                        if not result.clean:
                            self.registry.update_staleness_reason(repo_id, "partial_index_failure")
                            self.registry.update_last_sync_error(
                                repo_id,
                                "; ".join(result.errors) or "Incremental index update failed",
                            )
                            return IndexSyncResult(
                                action="failed",
                                commit=current_commit,
                                files_processed=result.files_processed,
                                error="; ".join(result.errors) or "Incremental index update failed",
                                duration_seconds=(datetime.now() - start_time).total_seconds(),
                                semantic=result.semantic,
                            )
                        if self._index_exists(repo_info) and self.registry.update_indexed_commit(
                            repo_id,
                            current_commit,
                            branch=current_branch,
                            expected_registration_id=repo_info.registration_id,
                            expected_generation=repo_info.index_generation,
                        ):
                            repo_info.last_indexed_commit = current_commit
                            return IndexSyncResult(
                                action="incremental_update",
                                commit=current_commit,
                                files_processed=result.files_processed,
                                duration_seconds=(datetime.now() - start_time).total_seconds(),
                            )
            except Exception as e:
                logger.warning(f"Incremental update failed, falling back to full index: {e}")

        # Full index needed
        runtime_snapshot = self._snapshot_active_runtime(repo_info)
        if force_full:
            self._write_force_full_exit_trace(
                repo_info,
                {
                    "status": "running",
                    "stage": "force_full_started",
                    "stage_family": "lexical",
                    "current_commit": current_commit,
                    "indexed_commit_before": last_indexed_commit,
                    "last_progress_path": None,
                    "in_flight_path": None,
                    "summary_call_timed_out": False,
                    "summary_call_file_path": None,
                    "summary_call_chunk_ids": [],
                    "summary_call_timeout_seconds": None,
                    "blocker_source": "lexical_mutation",
                },
            )
        progress_callback = None
        if force_full:
            progress_callback = self._make_force_full_progress_callback(
                repo_info=repo_info,
                current_commit=current_commit,
                indexed_commit_before=last_indexed_commit,
            )
        try:
            full_index_value = self._full_index(
                repo_id,
                ctx,
                progress_callback=progress_callback,
            )
        except TypeError as exc:
            if progress_callback is not None and "progress_callback" in str(exc):
                try:
                    full_index_value = self._full_index(repo_id, ctx)
                except BaseException:
                    if force_full:
                        self._finalize_running_force_full_trace_as_interrupted(
                            repo_info=repo_info,
                            current_commit=current_commit,
                            indexed_commit_before=last_indexed_commit,
                        )
                    raise
            else:
                raise
        except BaseException:
            if force_full:
                self._finalize_running_force_full_trace_as_interrupted(
                    repo_info=repo_info,
                    current_commit=current_commit,
                    indexed_commit_before=last_indexed_commit,
                )
            raise
        result = self._normalize_update_result(full_index_value)
        restore_result = self._restore_zero_summary_runtime_if_needed(
            repo_id,
            repo_info,
            ctx,
            result,
            runtime_snapshot,
        )
        if restore_result is not None:
            self._write_force_full_exit_trace(
                repo_info,
                {
                    "status": "completed",
                    "stage": "runtime_restore_completed",
                    "stage_family": "final_closeout",
                    "current_commit": current_commit,
                    "indexed_commit_before": last_indexed_commit,
                    "last_progress_path": (result.low_level or {}).get("last_progress_path")
                    or (result.semantic or {}).get("summary_call_file_path"),
                    "in_flight_path": (result.low_level or {}).get("in_flight_path"),
                    "summary_call_timed_out": (result.semantic or {}).get(
                        "summary_call_timed_out", False
                    ),
                    "summary_call_file_path": (result.semantic or {}).get("summary_call_file_path"),
                    "summary_call_chunk_ids": (result.semantic or {}).get(
                        "summary_call_chunk_ids", []
                    ),
                    "summary_call_timeout_seconds": (result.semantic or {}).get(
                        "summary_call_timeout_seconds"
                    ),
                    "blocker_source": self._trace_blocker_source(result),
                    "storage_failure_family": (result.semantic or {}).get("storage_failure_family"),
                    "storage_failure_reason": (result.semantic or {}).get("storage_failure_reason"),
                    "storage_failure_message": (result.semantic or {}).get(
                        "storage_failure_message"
                    ),
                    "storage_diagnostics": (result.semantic or {}).get("storage_diagnostics"),
                    "runtime_restore_performed": True,
                    "runtime_restore_mode": restore_result.mode,
                    "runtime_restore_declined_reason": None,
                },
            )
        if not result.clean:
            self.registry.update_staleness_reason(repo_id, "partial_index_failure")
            error_detail = self._format_sync_error_with_restore_context(
                "; ".join(result.errors) or "Full index failed",
                restore_result,
            )
            if force_full and restore_result is None:
                self._write_force_full_exit_trace(
                    repo_info,
                    {
                        "status": "completed",
                        "stage": "force_full_failed",
                        "stage_family": "final_closeout",
                        "current_commit": current_commit,
                        "indexed_commit_before": last_indexed_commit,
                        "last_progress_path": (result.low_level or {}).get("last_progress_path")
                        or (result.semantic or {}).get("summary_call_file_path"),
                        "in_flight_path": (result.low_level or {}).get("in_flight_path"),
                        "summary_call_timed_out": (result.semantic or {}).get(
                            "summary_call_timed_out", False
                        ),
                        "summary_call_file_path": (result.semantic or {}).get(
                            "summary_call_file_path"
                        ),
                        "summary_call_chunk_ids": (result.semantic or {}).get(
                            "summary_call_chunk_ids", []
                        ),
                        "summary_call_timeout_seconds": (result.semantic or {}).get(
                            "summary_call_timeout_seconds"
                        ),
                        "blocker_source": self._trace_blocker_source(result),
                        "storage_failure_family": (result.semantic or {}).get(
                            "storage_failure_family"
                        ),
                        "storage_failure_reason": (result.semantic or {}).get(
                            "storage_failure_reason"
                        ),
                        "storage_failure_message": (result.semantic or {}).get(
                            "storage_failure_message"
                        ),
                        "storage_diagnostics": (result.semantic or {}).get("storage_diagnostics"),
                        "runtime_restore_performed": False,
                        "runtime_restore_mode": None,
                        "runtime_restore_declined_reason": (result.semantic or {}).get(
                            "runtime_restore_declined_reason"
                        ),
                    },
                )
            self.registry.update_last_sync_error(
                repo_id,
                error_detail,
            )
            return IndexSyncResult(
                action="failed",
                commit=current_commit,
                files_processed=result.files_processed,
                error=error_detail,
                duration_seconds=(datetime.now() - start_time).total_seconds(),
                semantic=result.semantic,
            )
        if not self._index_has_durable_rows(repo_info):
            self.registry.update_staleness_reason(repo_id, "index_empty")
            self.registry.update_last_sync_error(
                repo_id,
                "Full index completed without durable SQLite file rows",
            )
            return IndexSyncResult(
                action="failed",
                commit=current_commit,
                files_processed=result.files_processed,
                error="Full index completed without durable SQLite file rows",
                duration_seconds=(datetime.now() - start_time).total_seconds(),
                semantic=result.semantic,
            )
        if self._index_exists(repo_info) and self.registry.update_indexed_commit(
            repo_id,
            current_commit,
            branch=current_branch,
            expected_registration_id=repo_info.registration_id,
            expected_generation=repo_info.index_generation,
        ):
            repo_info.last_indexed_commit = current_commit
            self.registry.update_last_sync_error(repo_id, None)
            if force_full:
                self._write_force_full_exit_trace(
                    repo_info,
                    {
                        "status": "completed",
                        "stage": "force_full_completed",
                        "stage_family": "final_closeout",
                        "current_commit": current_commit,
                        "indexed_commit_before": last_indexed_commit,
                        "last_progress_path": (result.low_level or {}).get("last_progress_path")
                        or (result.semantic or {}).get("summary_call_file_path"),
                        "in_flight_path": None,
                        "summary_call_timed_out": (result.semantic or {}).get(
                            "summary_call_timed_out", False
                        ),
                        "summary_call_file_path": (result.semantic or {}).get(
                            "summary_call_file_path"
                        ),
                        "summary_call_chunk_ids": (result.semantic or {}).get(
                            "summary_call_chunk_ids", []
                        ),
                        "summary_call_timeout_seconds": (result.semantic or {}).get(
                            "summary_call_timeout_seconds"
                        ),
                        "blocker_source": "final_closeout",
                    },
                )
        elif not self._index_exists(repo_info):
            self.registry.update_last_sync_error(
                repo_id,
                "Full index did not create a durable SQLite index",
            )
            return IndexSyncResult(
                action="failed",
                commit=current_commit,
                files_processed=result.files_processed,
                error="Full index did not create a durable SQLite index",
                duration_seconds=(datetime.now() - start_time).total_seconds(),
                semantic=result.semantic,
            )

        return IndexSyncResult(
            action="full_index",
            commit=current_commit,
            files_processed=result.indexed,
            duration_seconds=(datetime.now() - start_time).total_seconds(),
            semantic=result.semantic,
        )

    def rebuild_repository_index(self, repo_id: str) -> IndexSyncResult:
        """Build a full sibling index and publish it atomically for one repository."""
        repo = self.registry.get_repository(repo_id)
        with lock_registry.acquire(repo_id, repo_path=repo.path if repo else None):
            return self._rebuild_repository_index_locked(repo_id)

    def _rebuild_repository_index_locked(self, repo_id: str) -> IndexSyncResult:
        start_time = datetime.now()
        repo_info = self.registry.get_repository(repo_id)
        if repo_info is None:
            return IndexSyncResult(
                action="failed", commit="", error=f"Repository not found: {repo_id}"
            )

        git_state = self.registry.update_git_state(repo_id)
        current_commit = git_state.get("commit") if git_state else None
        current_branch = git_state.get("branch") if git_state else None
        if not current_commit:
            return IndexSyncResult(action="failed", commit="", error="Failed to get current commit")
        repo_info.current_commit = current_commit
        if current_branch:
            repo_info.current_branch = current_branch

        readiness = ReadinessClassifier.classify_registered(repo_info)
        if readiness.state not in _RECOVERABLE_REBUILD_STATES:
            return IndexSyncResult(
                action="refused",
                commit=current_commit,
                code=readiness.state.value,
                readiness=readiness.to_dict(),
                error=readiness.remediation,
                duration_seconds=(datetime.now() - start_time).total_seconds(),
            )

        repo_path = Path(repo_info.path).resolve(strict=False)
        active_path = Path(repo_info.index_path).resolve(strict=False)
        index_root = Path(repo_info.index_location).resolve(strict=False)
        index_root.mkdir(parents=True, exist_ok=True)
        generation = uuid.uuid4().hex
        generation_dir = index_root / "generations"
        generation_dir.mkdir(exist_ok=True)
        generation_path = generation_dir / f"{generation}.db"
        stage_dir = Path(tempfile.mkdtemp(prefix=".current.db.staging-", dir=index_root))
        stage_path = stage_dir / "current.db"
        stage_store: Optional[SQLiteStore] = None
        publication_started = False

        try:
            stage_store = SQLiteStore(
                str(stage_path),
                path_resolver=PathResolver(repo_path),
            )
            stage_ctx = RepoContext(
                repo_id=repo_id,
                sqlite_store=stage_store,
                workspace_root=repo_path,
                tracked_branch=getattr(repo_info, "tracked_branch", "") or "",
                registry_entry=repo_info,
            )
            result = self._normalize_update_result(self._full_index(repo_id, stage_ctx))
            if not result.clean:
                error = "; ".join(result.errors) or "Staged full index failed"
                self.registry.update_last_sync_error(repo_id, error)
                return IndexSyncResult(
                    action="failed",
                    commit=current_commit,
                    files_processed=result.files_processed,
                    error=error,
                    semantic=result.semantic,
                    duration_seconds=(datetime.now() - start_time).total_seconds(),
                )
            if not self._index_path_has_durable_rows(stage_path):
                error = "Staged full index completed without durable SQLite file rows"
                self.registry.update_last_sync_error(repo_id, error)
                return IndexSyncResult(
                    action="failed",
                    commit=current_commit,
                    files_processed=result.files_processed,
                    error=error,
                    semantic=result.semantic,
                    duration_seconds=(datetime.now() - start_time).total_seconds(),
                )

            self._rebuild_checkpoint("before_replacement")
            stage_store.close()
            stage_store = None
            self._release_runtime_handles(repo_id, repo_info, None)
            quarantine_path = None
            if readiness.state in _QUARANTINE_REBUILD_STATES:
                quarantine_path = self._quarantine_active_index(active_path, readiness.state)

            self.registry.begin_generation_mutation(
                repo_id,
                expected_registration_id=repo_info.registration_id,
                expected_generation=repo_info.index_generation,
            )
            publication_started = True
            with stage_path.open("rb") as source:
                os.fsync(source.fileno())
            os.replace(stage_path, generation_path)
            for directory in (generation_dir, index_root):
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            ReadinessClassifier.clear_index_inspection_cache()
            self._rebuild_checkpoint("after_replacement")
            if not self._index_path_has_durable_rows(generation_path):
                raise RuntimeError("Published index failed durable-row validation")
            self._rebuild_checkpoint("before_provenance")
            self.registry.publish_generation(
                repo_id,
                generation=generation,
                index_path=generation_path,
                commit=current_commit,
                branch=current_branch or repo_info.tracked_branch,
                profile=getattr(repo_info, "index_profile", None),
                expected_registration_id=getattr(repo_info, "registration_id", None),
                expected_generation=getattr(repo_info, "index_generation", None),
            )
            repo_info.index_path = generation_path
            repo_info.index_generation = generation
            repo_info.last_indexed_commit = current_commit
            repo_info.last_indexed_branch = current_branch
            repo_info.staleness_reason = None
            return IndexSyncResult(
                action="full_index",
                commit=current_commit,
                files_processed=result.indexed,
                readiness={
                    "previous_state": readiness.state.value,
                    "quarantine_path": str(quarantine_path) if quarantine_path else None,
                },
                semantic=result.semantic,
                duration_seconds=(datetime.now() - start_time).total_seconds(),
            )
        except Exception as exc:
            error = f"Staged rebuild failed: {exc}"
            if publication_started:
                self.registry.update_staleness_reason(repo_id, "partial_index_failure")
            self.registry.update_last_sync_error(repo_id, error)
            return IndexSyncResult(
                action="failed",
                commit=current_commit,
                error=error,
                duration_seconds=(datetime.now() - start_time).total_seconds(),
            )
        finally:
            if stage_store is not None:
                stage_store.close()
            shutil.rmtree(stage_dir, ignore_errors=True)

    def _rebuild_checkpoint(self, stage: str) -> None:
        """Failure-injection seam used by publication boundary tests."""

    def _quarantine_active_index(
        self,
        active_path: Path,
        state: RepositoryReadinessState,
    ) -> Optional[Path]:
        if not active_path.exists():
            return None
        quarantine_dir = active_path.parent / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        quarantine_path = quarantine_dir / f"{active_path.name}.{state.value}.{timestamp}"
        shutil.copy2(active_path, quarantine_path)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{active_path}{suffix}")
            if sidecar.exists():
                shutil.copy2(sidecar, Path(f"{quarantine_path}{suffix}"))
        return quarantine_path

    def _index_path_has_durable_rows(self, path: Path) -> bool:
        return path.exists() and ReadinessClassifier._inspect_index(path) is None

    def _resolve_ctx(self, repo_id: str) -> Optional[RepoContext]:
        """Resolve a RepoContext for the registered repository before mutation."""
        repo_info = self.registry.get_repository(repo_id)
        if not repo_info:
            return None

        repo_path = Path(repo_info.path)
        if self.repo_resolver is not None:
            ctx = self.repo_resolver.resolve(repo_path)
            if ctx is not None and ctx.repo_id == repo_id:
                return ctx
            return None

        try:
            if self.store_registry is not None:
                store = self.store_registry.get(repo_id)
            else:
                registry_get = getattr(self.registry, "get", None)
                registered_info = registry_get(repo_id) if callable(registry_get) else None
                if registered_info is repo_info:
                    self.store_registry = StoreRegistry.for_registry(self.registry)
                    store = self.store_registry.get(repo_id)
                else:
                    if not isinstance(repo_info.index_path, (str, Path)):
                        return None
                    store = SQLiteStore(
                        str(repo_info.index_path),
                        path_resolver=PathResolver(repo_path),
                    )
        except Exception as exc:
            logger.error("Failed to resolve store for %s: %s", repo_id, exc)
            return None

        return RepoContext(
            repo_id=repo_id,
            sqlite_store=store,
            workspace_root=repo_path,
            tracked_branch=getattr(repo_info, "tracked_branch", "") or "",
            registry_entry=repo_info,
        )

    def _index_exists(self, repo_info: Any) -> bool:
        index_path = getattr(repo_info, "index_path", None)
        if index_path is not None:
            return Path(index_path).exists()
        index_location = getattr(repo_info, "index_location", None)
        if index_location is None:
            return False
        return (Path(index_location) / "current.db").exists()

    def _index_has_durable_rows(self, repo_info: Any) -> bool:
        index_path = getattr(repo_info, "index_path", None)
        if index_path is None:
            return False
        path = Path(index_path)
        if not path.exists():
            return False
        try:
            import sqlite3

            conn = sqlite3.connect(str(path))
            try:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM files WHERE is_deleted = 0 OR is_deleted IS NULL"
                )
                return int(cursor.fetchone()[0]) > 0
            finally:
                conn.close()
        except Exception:
            return False

    def _normalize_update_result(self, value: Any) -> UpdateResult:
        if isinstance(value, UpdateResult):
            return value
        if isinstance(value, int):
            return UpdateResult(indexed=value)
        return UpdateResult(failed=1, errors=[f"Unexpected update result: {value!r}"])

    def _get_changed_files(self, repo_path: Path, from_commit: str, to_commit: str) -> ChangeSet:
        """Get files changed between two commits.

        Args:
            repo_path: Repository path
            from_commit: Starting commit
            to_commit: Ending commit

        Returns:
            ChangeSet
        """
        changes = ChangeSet(added=[], modified=[], deleted=[], renamed=[])
        for change in ChangeDetector(repo_path).get_changes_since_commit(from_commit, to_commit):
            path_excluded = any(part in EXCLUDED_DIR_PARTS for part in Path(change.path).parts)
            if change.change_type == "added":
                if not path_excluded:
                    changes.added.append(change.path)
            elif change.change_type == "modified":
                if not path_excluded:
                    changes.modified.append(change.path)
            elif change.change_type == "deleted":
                if not path_excluded:
                    changes.deleted.append(change.path)
            elif change.change_type == "renamed" and change.old_path:
                old_excluded = any(
                    part in EXCLUDED_DIR_PARTS for part in Path(change.old_path).parts
                )
                if old_excluded and not path_excluded:
                    changes.added.append(change.path)
                elif not old_excluded and path_excluded:
                    changes.deleted.append(change.old_path)
                elif not old_excluded and not path_excluded:
                    changes.renamed.append((change.old_path, change.path))

        return changes

    def _incremental_index_update(
        self, repo_id: str, ctx: RepoContext, changes: ChangeSet
    ) -> UpdateResult:
        """Update index incrementally based on file changes.

        Args:
            repo_id: Repository ID
            changes: Set of file changes

        Returns:
            UpdateResult
        """
        start_time = datetime.now()
        result = UpdateResult()

        repo_info = self.registry.get_repository(repo_id)
        if not repo_info:
            result.failed += 1
            result.errors.append(f"Repository not found: {repo_id}")
            return result

        if not self._index_exists(repo_info):
            result.failed += 1
            result.errors.append(f"Missing durable index for incremental update: {repo_id}")
            result.duration_seconds = (datetime.now() - start_time).total_seconds()
            return result

        # Use dispatcher if available, otherwise direct SQLite operations
        if self.dispatcher:
            repo_path = Path(repo_info.path)

            # Handle deletions first
            for path in changes.deleted:
                try:
                    mutation = self._coerce_index_result(
                        self.dispatcher.remove_file(ctx, repo_path / path),
                        path=repo_path / path,
                    )
                    self._record_required_mutation_result(
                        result,
                        mutation,
                        success_status=IndexResultStatus.DELETED,
                        success_counter="deleted",
                        action_label=f"Failed to remove {path}",
                    )
                except Exception as e:
                    logger.error(f"Failed to remove {path}: {e}")
                    result.failed += 1
                    result.errors.append(f"Failed to remove {path}: {e}")

            # Handle renames
            for old_path, new_path in changes.renamed:
                try:
                    old_full = repo_path / old_path
                    new_full = repo_path / new_path
                    if new_full.exists():
                        mutation = self._coerce_index_result(
                            self.dispatcher.move_file(ctx, old_full, new_full),
                            path=new_full,
                        )
                        self._record_required_mutation_result(
                            result,
                            mutation,
                            success_status=IndexResultStatus.MOVED,
                            success_counter="moved",
                            action_label=f"Failed to move {old_path} -> {new_path}",
                        )
                    else:
                        # New path doesn't exist, just remove old
                        mutation = self._coerce_index_result(
                            self.dispatcher.remove_file(ctx, old_full),
                            path=old_full,
                        )
                        self._record_required_mutation_result(
                            result,
                            mutation,
                            success_status=IndexResultStatus.DELETED,
                            success_counter="deleted",
                            action_label=f"Failed to remove stale rename source {old_path}",
                        )
                except Exception as e:
                    logger.error(f"Failed to move {old_path} -> {new_path}: {e}")
                    result.failed += 1
                    result.errors.append(f"Failed to move {old_path} -> {new_path}: {e}")

            # Handle modifications and additions
            for path in changes.modified + changes.added:
                try:
                    full_path = repo_path / path
                    if full_path.exists() and full_path.is_file():
                        mutation = self._coerce_index_result(
                            self.dispatcher.index_file(ctx, full_path),
                            path=full_path,
                        )
                        self._record_required_mutation_result(
                            result,
                            mutation,
                            success_status=IndexResultStatus.INDEXED,
                            success_counter="indexed",
                            action_label=f"Failed to index {path}",
                        )
                    else:
                        result.skipped += 1
                        result.errors.append(f"Failed to index {path}: file missing at dispatch")
                except Exception as e:
                    logger.error(f"Failed to index {path}: {e}")
                    result.failed += 1
                    result.errors.append(f"Failed to index {path}: {e}")
        else:
            result.failed += 1
            result.errors.append("No dispatcher available for incremental update")

        result.duration_seconds = (datetime.now() - start_time).total_seconds()
        return result

    def _coerce_index_result(self, value: object, *, path: Path) -> IndexResult:
        if isinstance(value, IndexResult):
            return value
        return IndexResult(
            status=IndexResultStatus.ERROR,
            path=path,
            observed_hash=None,
            actual_hash=None,
            error=f"Unexpected mutation result: {value!r}",
        )

    def _record_required_mutation_result(
        self,
        result: UpdateResult,
        mutation: IndexResult,
        *,
        success_status: IndexResultStatus,
        success_counter: str,
        action_label: str,
    ) -> None:
        self._merge_semantic_result(result, mutation.semantic)
        if mutation.status == success_status:
            setattr(result, success_counter, getattr(result, success_counter) + 1)
            return

        detail = mutation.error or mutation.status.value
        if mutation.status in {
            IndexResultStatus.SKIPPED_UNCHANGED,
            IndexResultStatus.SKIPPED_TOCTOU,
        }:
            result.skipped += 1
            result.errors.append(f"{action_label}: skipped required mutation ({detail})")
            return

        result.failed += 1
        result.errors.append(f"{action_label}: {detail}")

    def _merge_semantic_result(
        self, result: UpdateResult, semantic: Optional[Dict[str, Any]]
    ) -> None:
        if not semantic:
            return
        if result.semantic is None:
            result.semantic = {
                "summaries_written": 0,
                "summary_chunks_attempted": 0,
                "summary_missing_chunks": 0,
                "semantic_indexed": 0,
                "semantic_failed": 0,
                "semantic_skipped": 0,
                "semantic_blocked": 0,
                "vectors_deleted": 0,
                "mappings_deleted": 0,
                "summaries_deleted": 0,
                "summaries_preserved": 0,
                "semantic_stage": "not_run",
                "semantic_error": None,
            }
        for key in [
            "summaries_written",
            "summary_chunks_attempted",
            "summary_missing_chunks",
            "semantic_indexed",
            "semantic_failed",
            "semantic_skipped",
            "semantic_blocked",
            "vectors_deleted",
            "mappings_deleted",
            "summaries_deleted",
            "summaries_preserved",
        ]:
            result.semantic[key] = result.semantic.get(key, 0) + int(semantic.get(key, 0) or 0)

        stage = semantic.get("semantic_stage")
        if stage:
            result.semantic["semantic_stage"] = stage
        if semantic.get("semantic_error"):
            result.semantic["semantic_error"] = semantic.get("semantic_error")
        for key in [
            "summary_call_timed_out",
            "summary_call_file_path",
            "summary_call_chunk_ids",
            "summary_call_timeout_seconds",
            "storage_failure_family",
            "storage_failure_reason",
            "storage_failure_message",
            "storage_diagnostics",
            "runtime_restore_declined_reason",
        ]:
            if key in semantic:
                result.semantic[key] = semantic.get(key)

    def _should_full_reindex(self, repo_path: Path, changes: ChangeSet) -> bool:
        """Decide whether change volume warrants a full reindex."""
        try:
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
            )
            tracked_files = [line for line in result.stdout.splitlines() if line.strip()]
            total = len(tracked_files)
            if total == 0:
                return False

            ratio = changes.total_changes() / total
            return ratio >= 0.5
        except subprocess.CalledProcessError:
            return False

    def _semantic_stage_error(self, semantic: Optional[Dict[str, Any]]) -> Optional[str]:
        """Return an exact force-full blocker when the semantic stage did not finish cleanly."""
        if not semantic:
            return None

        stage = semantic.get("semantic_stage")
        if stage in {None, "not_run", "skipped", "indexed"}:
            return None

        error = semantic.get("semantic_error")
        if isinstance(error, str) and error.strip():
            return error.strip()

        blocker = semantic.get("semantic_blocker")
        if isinstance(blocker, dict):
            message = blocker.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()

        return f"Semantic stage ended with {stage}"

    def _low_level_stage_error(self, low_level: Optional[Dict[str, Any]]) -> Optional[str]:
        """Return an exact lexical/storage blocker before semantic-stage accounting starts."""
        if not low_level:
            return None

        blocker = low_level.get("low_level_blocker")
        if isinstance(blocker, dict):
            message = blocker.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()

        stage = low_level.get("lexical_stage")
        if stage in {None, "not_run", "completed"}:
            return None
        return f"Lexical stage ended with {stage}"

    def _full_index(
        self,
        repo_id: str,
        ctx: RepoContext,
        progress_callback: Optional[Any] = None,
    ) -> UpdateResult:
        """Perform full repository indexing.

        Args:
            repo_id: Repository ID

        Returns:
            UpdateResult with durability/failure details
        """
        start_time = datetime.now()
        result = UpdateResult()
        repo_info = self.registry.get_repository(repo_id)
        if not repo_info:
            result.failed += 1
            result.errors.append(f"Repository not found: {repo_id}")
            return result

        if not self.dispatcher:
            logger.error("No dispatcher available for indexing")
            result.failed += 1
            result.errors.append("No dispatcher available for indexing")
            return result

        repo_path = Path(repo_info.path)

        # Ensure index directory exists
        index_dir = Path(repo_info.index_location)
        index_dir.mkdir(parents=True, exist_ok=True)

        # Index the directory
        logger.info(f"Starting full index of {repo_info.name}")
        try:
            if progress_callback is None:
                stats = self.dispatcher.index_directory(ctx, repo_path, recursive=True)
            else:
                try:
                    stats = self.dispatcher.index_directory(
                        ctx,
                        repo_path,
                        recursive=True,
                        progress_callback=progress_callback,
                    )
                except TypeError as exc:
                    if "progress_callback" not in str(exc):
                        raise
                    stats = self.dispatcher.index_directory(ctx, repo_path, recursive=True)
        except Exception as exc:
            result.failed += 1
            result.errors.append(f"Full index failed: {exc}")
            result.duration_seconds = (datetime.now() - start_time).total_seconds()
            return result

        total_indexed = stats.get("indexed_files", 0) if isinstance(stats, dict) else 0
        failed_files = stats.get("failed_files", 0) if isinstance(stats, dict) else 0
        errors = stats.get("errors", []) if isinstance(stats, dict) else []
        result.indexed = total_indexed
        result.failed = failed_files
        if isinstance(stats, dict):
            result.semantic = {
                "summaries_written": stats.get("summaries_written", 0),
                "summary_chunks_attempted": stats.get("summary_chunks_attempted", 0),
                "summary_missing_chunks": stats.get("summary_missing_chunks", 0),
                "summary_passes": stats.get("summary_passes", 0),
                "summary_remaining_chunks": stats.get("summary_remaining_chunks", 0),
                "summary_scope_drained": stats.get("summary_scope_drained", True),
                "summary_continuation_required": stats.get("summary_continuation_required", False),
                "summary_call_timed_out": stats.get("summary_call_timed_out", False),
                "summary_call_file_path": stats.get("summary_call_file_path"),
                "summary_call_chunk_ids": stats.get("summary_call_chunk_ids", []),
                "summary_call_timeout_seconds": stats.get("summary_call_timeout_seconds"),
                "semantic_indexed": stats.get("semantic_indexed", 0),
                "semantic_failed": stats.get("semantic_failed", 0),
                "semantic_skipped": stats.get("semantic_skipped", 0),
                "semantic_blocked": stats.get("semantic_blocked", 0),
                "semantic_stage": stats.get("semantic_stage"),
                "semantic_error": stats.get("semantic_error"),
                "storage_failure_family": stats.get("storage_failure_family"),
                "storage_failure_reason": stats.get("storage_failure_reason"),
                "storage_failure_message": stats.get("storage_failure_message"),
                "storage_diagnostics": stats.get("storage_diagnostics"),
                "runtime_restore_declined_reason": stats.get("runtime_restore_declined_reason"),
            }
            if stats.get("low_level_blocker") is not None or stats.get("lexical_stage") not in {
                None,
                "not_run",
                "completed",
            }:
                result.low_level = {
                    "lexical_stage": stats.get("lexical_stage"),
                    "lexical_files_attempted": stats.get("lexical_files_attempted", 0),
                    "lexical_files_completed": stats.get("lexical_files_completed", 0),
                    "last_progress_path": stats.get("last_progress_path"),
                    "in_flight_path": stats.get("in_flight_path"),
                    "low_level_blocker": stats.get("low_level_blocker"),
                    "storage_diagnostics": stats.get("storage_diagnostics"),
                }
        result.errors.extend(str(error) for error in errors)
        low_level_error = self._low_level_stage_error(result.low_level)
        if low_level_error:
            result.errors.append(low_level_error)
        semantic_error = self._semantic_stage_error(result.semantic)
        if semantic_error:
            result.errors.append(semantic_error)
        result.duration_seconds = (datetime.now() - start_time).total_seconds()
        logger.info(f"Indexed {total_indexed} files in {repo_info.name}")

        return result

    def _has_remote_artifact(self, repo_id: str, commit: str) -> bool:
        """Check if remote artifact exists for commit.

        Args:
            repo_id: Repository ID
            commit: Git commit SHA

        Returns:
            True if artifact exists
        """
        # Local artifact store fallback (can be extended to GitHub/cloud providers).
        return self.artifact_manager.has_artifact(repo_id, commit)

    def _download_commit_index(self, repo_id: str, commit: str) -> bool:
        """Download index artifact for specific commit.

        Args:
            repo_id: Repository ID
            commit: Git commit SHA

        Returns:
            True if successful
        """
        # Legacy extraction replaces live SQLite/Qdrant files. Until artifacts use
        # staged generation publication, build locally instead of admitting it.
        logger.info("Artifact restore requires staged generation publication for %s", repo_id)
        return False

    def create_commit_artifact(self, repo_id: str) -> Optional[Path]:
        """Create index artifact for current commit.

        Args:
            repo_id: Repository ID

        Returns:
            Path to created artifact or None
        """
        repo_info = self.registry.get_repository(repo_id)
        if not repo_info:
            return None

        commit = repo_info.current_commit
        if not commit:
            return None

        index_path = Path(repo_info.index_location)
        return self.artifact_manager.create_commit_artifact(repo_id, commit, index_path)

    def enqueue_full_rescan(self, repo_id: str) -> IndexSyncResult:
        """Trigger a guarded full rescan without bypassing the tracked-branch check."""
        return self.sync_repository_index(repo_id, force_full=True)

    def _runtime_paths(self, repo_info: Any) -> Tuple[Path, Path]:
        index_location = Path(repo_info.index_location)
        return Path(repo_info.index_path), index_location / "semantic_qdrant"

    def _snapshot_active_runtime(self, repo_info: Any) -> RuntimeSnapshot:
        db_path, qdrant_path = self._runtime_paths(repo_info)
        backup_dir = Path(tempfile.mkdtemp(prefix="mcp-index-runtime-"))
        backup_db = backup_dir / "current.db"
        backup_qdrant = backup_dir / "semantic_qdrant"
        counts_before = self._read_runtime_counts(db_path)
        if db_path.exists():
            shutil.copy2(db_path, backup_db)
        sqlite_sidecars: List[str] = []
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{db_path}{suffix}")
            if sidecar.exists():
                try:
                    shutil.copy2(sidecar, backup_dir / sidecar.name)
                except FileNotFoundError:
                    continue
                sqlite_sidecars.append(suffix)
        if qdrant_path.exists():
            shutil.copytree(qdrant_path, backup_qdrant)
        return RuntimeSnapshot(
            backup_dir=backup_dir,
            db_path=db_path,
            qdrant_path=qdrant_path,
            db_existed=db_path.exists(),
            qdrant_existed=qdrant_path.exists(),
            counts_before=counts_before,
            sqlite_sidecars=sqlite_sidecars,
        )

    def _restore_zero_summary_runtime_if_needed(
        self,
        repo_id: str,
        repo_info: Any,
        ctx: RepoContext,
        result: UpdateResult,
        snapshot: RuntimeSnapshot,
    ) -> Optional[RuntimeRestoreResult]:
        semantic = result.semantic or {}
        timed_out = semantic.get("semantic_stage") == "blocked_summary_call_timeout"
        storage_closeout = self._semantic_storage_closeout(semantic)
        zero_summary = int(semantic.get("summaries_written", 0) or 0) == 0
        zero_vectors = self._read_runtime_counts(snapshot.db_path).get("semantic_points", 0) == 0
        if (timed_out or storage_closeout) and (not zero_summary or not zero_vectors):
            semantic["runtime_restore_declined_reason"] = (
                "authoritative summaries or semantic vectors were already written"
            )
            result.semantic = semantic
        if not (timed_out or storage_closeout) or not zero_summary or not zero_vectors:
            self._cleanup_runtime_snapshot(snapshot)
            return None

        # Other processes can still hold this database or vector store. Copying
        # over their files cannot provide a coherent rollback; retain the fence.
        semantic["runtime_restore_performed"] = False
        semantic["runtime_restore_mode"] = None
        semantic["runtime_restore_declined_reason"] = (
            "Live runtime handles may exist; staged generation rebuild required"
        )
        semantic["runtime_counts_before"] = dict(snapshot.counts_before)
        semantic["runtime_counts_after"] = self._read_runtime_counts(snapshot.db_path)
        result.semantic = semantic
        self.registry.update_staleness_reason(repo_id, "partial_index_failure")
        self._cleanup_runtime_snapshot(snapshot)
        return None

    def _release_runtime_handles(
        self,
        repo_id: str,
        repo_info: Any,
        ctx: Optional[RepoContext],
    ) -> None:
        if self.store_registry is not None:
            self.store_registry.close(repo_id)
        sqlite_store = getattr(ctx, "sqlite_store", None) if ctx is not None else None
        if sqlite_store is not None:
            try:
                sqlite_store.close()
            except Exception:
                pass
        if self.dispatcher is not None and hasattr(self.dispatcher, "evict_repository_state"):
            self.dispatcher.evict_repository_state(repo_id, repo_info.path)

    def _cleanup_runtime_snapshot(self, snapshot: RuntimeSnapshot) -> None:
        shutil.rmtree(snapshot.backup_dir, ignore_errors=True)

    def _read_runtime_counts(self, db_path: Path) -> Dict[str, int]:
        counts = {
            "files": 0,
            "code_chunks": 0,
            "chunk_summaries": 0,
            "semantic_points": 0,
        }
        if not db_path.exists():
            return counts
        try:
            with sqlite3.connect(db_path) as conn:
                for table in counts:
                    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    counts[table] = int(row[0]) if row else 0
        except sqlite3.Error:
            return counts
        return counts

    def _format_sync_error_with_restore_context(
        self,
        error: str,
        restore_result: Optional[RuntimeRestoreResult],
    ) -> str:
        if restore_result is None or not restore_result.restored:
            return error
        return (
            f"{error} [runtime restored via {restore_result.mode}; "
            f"counts {restore_result.counts_before} -> {restore_result.counts_after}]"
        )

    def _force_full_exit_trace_path(self, repo_info: Any) -> Path:
        return Path(repo_info.index_location) / _FORCE_FULL_EXIT_TRACE

    def _trace_timestamp(self) -> str:
        return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    def _read_force_full_exit_trace(self, repo_info: Any) -> Optional[Dict[str, Any]]:
        path = self._force_full_exit_trace_path(repo_info)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_force_full_exit_trace(self, repo_info: Any, update: Dict[str, Any]) -> None:
        path = self._force_full_exit_trace_path(repo_info)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._read_force_full_exit_trace(repo_info) or {}
        payload.update(update)
        payload["trace_timestamp"] = self._trace_timestamp()
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        temp_path.replace(path)

    def _force_full_trace_process_alive(self, trace: Dict[str, Any]) -> Optional[bool]:
        process_id = trace.get("process_id")
        if not isinstance(process_id, int) or process_id <= 0:
            return None
        try:
            os.kill(process_id, 0)
        except OSError:
            return False
        return True

    def _make_force_full_progress_callback(
        self,
        *,
        repo_info: Any,
        current_commit: Optional[str],
        indexed_commit_before: Optional[str],
    ) -> Any:
        def callback(snapshot: Dict[str, Any]) -> None:
            previous_trace = self._read_force_full_exit_trace(repo_info) or {}
            last_progress_path = snapshot.get("last_progress_path")
            if last_progress_path is None:
                last_progress_path = previous_trace.get("last_progress_path")
            self._write_force_full_exit_trace(
                repo_info,
                {
                    "status": "running",
                    "stage": snapshot.get("stage"),
                    "stage_family": snapshot.get("stage_family"),
                    "current_commit": current_commit,
                    "indexed_commit_before": indexed_commit_before,
                    "last_progress_path": last_progress_path,
                    "in_flight_path": snapshot.get("in_flight_path"),
                    "summary_call_timed_out": snapshot.get("summary_call_timed_out", False),
                    "summary_call_file_path": snapshot.get("summary_call_file_path"),
                    "summary_call_chunk_ids": snapshot.get("summary_call_chunk_ids", []),
                    "summary_call_timeout_seconds": snapshot.get("summary_call_timeout_seconds"),
                    "process_id": os.getpid(),
                    "blocker_source": snapshot.get("blocker_source"),
                    "semantic_stage": snapshot.get("semantic_stage"),
                    "lexical_stage": snapshot.get("lexical_stage"),
                    "storage_failure_family": snapshot.get("storage_failure_family"),
                    "storage_failure_reason": snapshot.get("storage_failure_reason"),
                    "storage_failure_message": snapshot.get("storage_failure_message"),
                    "storage_diagnostics": snapshot.get("storage_diagnostics"),
                    "runtime_restore_performed": snapshot.get("runtime_restore_performed"),
                    "runtime_restore_mode": snapshot.get("runtime_restore_mode"),
                    "runtime_restore_declined_reason": snapshot.get(
                        "runtime_restore_declined_reason"
                    ),
                },
            )

        return callback

    def _trace_blocker_source(self, result: UpdateResult) -> str:
        if result.low_level is not None:
            return "lexical_mutation"
        semantic = result.semantic or {}
        blocker = semantic.get("semantic_blocker")
        if isinstance(blocker, dict) and blocker.get("code") == "storage_closeout":
            return "storage_closeout"
        if semantic.get("storage_failure_family"):
            return "storage_closeout"
        if semantic.get("summary_call_timed_out"):
            return "summary_call_shutdown"
        return "final_closeout"

    def _finalize_running_force_full_trace_as_interrupted(
        self,
        *,
        repo_info: Any,
        current_commit: Optional[str],
        indexed_commit_before: Optional[str],
    ) -> None:
        previous_trace = self._read_force_full_exit_trace(repo_info) or {}
        if previous_trace.get("status") != "running":
            return
        self._write_force_full_exit_trace(
            repo_info,
            {
                "status": "interrupted",
                "stage": previous_trace.get("stage") or "force_full_interrupted",
                "stage_family": previous_trace.get("stage_family") or "final_closeout",
                "current_commit": current_commit,
                "indexed_commit_before": indexed_commit_before,
                "last_progress_path": previous_trace.get("last_progress_path"),
                "in_flight_path": previous_trace.get("in_flight_path"),
                "summary_call_timed_out": previous_trace.get("summary_call_timed_out", False),
                "summary_call_file_path": previous_trace.get("summary_call_file_path"),
                "summary_call_chunk_ids": previous_trace.get("summary_call_chunk_ids", []),
                "summary_call_timeout_seconds": previous_trace.get("summary_call_timeout_seconds"),
                "process_id": previous_trace.get("process_id"),
                "blocker_source": previous_trace.get("blocker_source") or "process_interrupt",
                "semantic_stage": previous_trace.get("semantic_stage"),
                "lexical_stage": previous_trace.get("lexical_stage"),
                "storage_failure_family": previous_trace.get("storage_failure_family"),
                "storage_failure_reason": previous_trace.get("storage_failure_reason"),
                "storage_failure_message": previous_trace.get("storage_failure_message"),
                "storage_diagnostics": previous_trace.get("storage_diagnostics"),
                "runtime_restore_performed": previous_trace.get("runtime_restore_performed"),
                "runtime_restore_mode": previous_trace.get("runtime_restore_mode"),
                "runtime_restore_declined_reason": previous_trace.get(
                    "runtime_restore_declined_reason"
                ),
            },
        )

    def _semantic_storage_closeout(self, semantic: Dict[str, Any]) -> bool:
        blocker = semantic.get("semantic_blocker")
        if isinstance(blocker, dict) and blocker.get("code") == "storage_closeout":
            return True
        return bool(semantic.get("storage_failure_family"))

    def sync_all_repositories(self) -> Dict[str, IndexSyncResult]:
        """Sync all repositories that need updates sequentially."""
        results = {}

        # Get repositories needing update
        stale_repos = self.registry.get_repositories_needing_update()

        if not stale_repos:
            logger.info("All repositories are up to date")
            return results

        logger.info(f"Found {len(stale_repos)} repositories needing update")

        for repo_id, repo_info in stale_repos:
            if repo_info.auto_sync:
                logger.info(f"Syncing {repo_info.name}...")
                results[repo_id] = self.sync_repository_index(repo_id)
            else:
                logger.info(f"Skipping {repo_info.name} (auto-sync disabled)")

        return results

    def get_repository_status(self, repo_id: str) -> Dict[str, Any]:
        """Get detailed status of a repository's index.

        Args:
            repo_id: Repository ID

        Returns:
            Status dictionary
        """
        repo_info = self.registry.get_repository(repo_id)
        if not repo_info:
            return {"error": "Repository not found"}

        status = {
            "repo_id": repo_id,
            "name": repo_info.name,
            "path": repo_info.path,
            "current_commit": repo_info.current_commit,
            "last_indexed_commit": repo_info.last_indexed_commit,
            "last_indexed": repo_info.last_indexed,
            "needs_update": repo_info.needs_update(),
            "auto_sync": repo_info.auto_sync,
            "artifact_enabled": repo_info.artifact_enabled,
            "artifact_backend": repo_info.artifact_backend,
            "artifact_health": repo_info.artifact_health,
            "last_sync_error": getattr(repo_info, "last_sync_error", None),
        }

        # Check index file
        index_path = Path(repo_info.index_location) / "current.db"
        if index_path.exists():
            status["index_exists"] = True
            status["index_size_mb"] = index_path.stat().st_size / (1024 * 1024)
        else:
            status["index_exists"] = False
            status["index_size_mb"] = 0
        trace = self._read_force_full_exit_trace(repo_info)
        if trace is not None and trace.get("status") == "running":
            is_alive = self._force_full_trace_process_alive(trace)
            if is_alive is False:
                self._write_force_full_exit_trace(
                    repo_info,
                    {
                        "status": "interrupted",
                        "stage": trace.get("stage") or "force_full_interrupted",
                        "stage_family": trace.get("stage_family") or "final_closeout",
                        "current_commit": trace.get("current_commit") or repo_info.current_commit,
                        "indexed_commit_before": trace.get("indexed_commit_before")
                        or repo_info.last_indexed_commit,
                        "last_progress_path": trace.get("last_progress_path"),
                        "in_flight_path": trace.get("in_flight_path"),
                        "summary_call_timed_out": trace.get("summary_call_timed_out", False),
                        "summary_call_file_path": trace.get("summary_call_file_path"),
                        "summary_call_chunk_ids": trace.get("summary_call_chunk_ids", []),
                        "summary_call_timeout_seconds": trace.get("summary_call_timeout_seconds"),
                        "process_id": trace.get("process_id"),
                        "blocker_source": trace.get("blocker_source") or "process_interrupt",
                        "semantic_stage": trace.get("semantic_stage"),
                        "lexical_stage": trace.get("lexical_stage"),
                        "storage_failure_family": trace.get("storage_failure_family"),
                        "storage_failure_reason": trace.get("storage_failure_reason"),
                        "storage_failure_message": trace.get("storage_failure_message"),
                        "storage_diagnostics": trace.get("storage_diagnostics"),
                        "runtime_restore_performed": trace.get("runtime_restore_performed"),
                        "runtime_restore_mode": trace.get("runtime_restore_mode"),
                        "runtime_restore_declined_reason": trace.get(
                            "runtime_restore_declined_reason"
                        ),
                    },
                )
                trace = self._read_force_full_exit_trace(repo_info)
        if trace is not None:
            status["force_full_exit_trace"] = trace

        status.update(build_health_row(repo_info))

        return status
