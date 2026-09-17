"""Git-aware index manager for commit-synchronized indexing.

This module provides index management that's synchronized with git commits,
supporting incremental updates and artifact management.
"""

import hashlib
import json
import logging
import math
import os
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..artifacts.commit_artifacts import CommitArtifactManager
from ..config.env_vars import get_max_file_size_bytes
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
        if isinstance(repo_resolver, RepoResolver):
            repo_resolver._index_manager = self
        self.store_registry = store_registry
        if isinstance(dispatcher, EnhancedDispatcher) and dispatcher._semantic_registry is None:
            from ..utils.semantic_indexer_registry import SemanticIndexerRegistry

            dispatcher._semantic_registry = SemanticIndexerRegistry(registry)
        self.artifact_manager = CommitArtifactManager()
        # Wired post-construction by MultiRepositoryWatcher to avoid circular import.
        # Signature: (repo_id: str, current_branch: str, tracked_branch: str) -> None
        self.on_branch_drift: _DriftCallback = None

    def sync_repository_index(
        self,
        repo_id: str,
        force_full: bool = False,
        bypass_branch_guard: bool = False,
        *,
        expected_registration_id: Optional[str] = None,
    ) -> IndexSyncResult:
        """Serialize repository synchronization through the shared per-repo lock."""
        repo = self.registry.get_repository(repo_id)
        with lock_registry.acquire(repo_id, repo_path=repo.path if repo else None):
            return self._sync_repository_index_locked(
                repo_id,
                force_full=force_full,
                bypass_branch_guard=bypass_branch_guard,
                expected_registration_id=expected_registration_id,
            )

    def _sync_repository_index_locked(
        self,
        repo_id: str,
        force_full: bool = False,
        bypass_branch_guard: bool = False,
        *,
        expected_registration_id: Optional[str] = None,
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
        if (
            expected_registration_id is not None
            and repo_info.registration_id != expected_registration_id
        ):
            return IndexSyncResult(
                action="refused", commit="", error="Repository registration changed"
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
            and self._git_source_unchanged(repo_path, current_commit, current_branch)
        ):
            return IndexSyncResult(
                action="up_to_date",
                commit=current_commit,
                duration_seconds=(datetime.now() - start_time).total_seconds(),
            )

        if repo_info.artifact_enabled and not force_full:
            if self._has_remote_artifact(repo_id, current_commit):
                if self._download_commit_index(repo_id, current_commit):
                    return IndexSyncResult(
                        action="downloaded",
                        commit=current_commit,
                        duration_seconds=(datetime.now() - start_time).total_seconds(),
                    )
        changes = None
        if (
            last_indexed_commit
            and not force_full
            and index_exists_before_mutation
            and repo_info.staleness_reason
            not in {"index_publication_pending", "partial_index_failure"}
        ):
            try:
                candidate = self._get_changed_files(repo_path, last_indexed_commit, current_commit)
                policy_paths = candidate.added + candidate.modified + candidate.deleted
                policy_paths += [path for pair in candidate.renamed for path in pair]
                if not any(
                    Path(path).name in {".gitignore", ".mcp-index-ignore"} for path in policy_paths
                ):
                    if not self._should_full_reindex(repo_path, candidate):
                        changes = candidate
            except (OSError, subprocess.SubprocessError):
                logger.info("Change inventory unavailable; staging a full rebuild")
        return self._rebuild_repository_index_locked(
            repo_id, changes=changes, force_full=force_full
        )

    def rebuild_repository_index(self, repo_id: str) -> IndexSyncResult:
        """Build a full sibling index and publish it atomically for one repository."""
        repo = self.registry.get_repository(repo_id)
        with lock_registry.acquire(repo_id, repo_path=repo.path if repo else None):
            return self._rebuild_repository_index_locked(repo_id)

    def restore_verified_artifact(
        self, repo_id: str, extracted: Path, *, expected_commit: str
    ) -> IndexSyncResult:
        """Admit an integrity/identity/signature-verified archive through generation publication."""
        repo = self.registry.get_repository(repo_id)
        if repo is None:
            raise KeyError(repo_id)
        database = extracted / "current.db"
        if not database.is_file():
            raise ValueError("Artifact has no portable current.db generation")

        def restore(ctx):
            if ctx.registry_entry.current_commit != expected_commit:
                raise ValueError("Artifact commit changed before staging")
            mappings = ctx.sqlite_store.import_artifact_rows(database, ctx.workspace_root)
            if mappings:
                self._restore_artifact_vectors(ctx, extracted, mappings)
            with ctx.sqlite_store._get_connection() as connection:
                count = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            return UpdateResult(indexed=count)

        with lock_registry.acquire(repo_id, repo_path=repo.path):
            return self._rebuild_repository_index_locked(
                repo_id, stage_operation=restore, replace_derived=True
            )

    def _restore_artifact_vectors(
        self, ctx: RepoContext, extracted: Path, mappings: List[Dict]
    ) -> None:
        from qdrant_client import models

        if getattr(self.dispatcher, "_semantic_enabled", False) is not True:
            raise RuntimeError("Semantic artifact restore requires its configured profile")
        metadata = json.loads((extracted / ".index_metadata.json").read_text(encoding="utf-8"))
        manifest = metadata.get("vector_export", {})
        if (
            manifest.get("format") != "semantic-vectors.v1"
            or manifest.get("file") != "semantic-vectors.jsonl"
        ):
            raise ValueError("Artifact vector format requires a new portable export")
        ids = {row["point_id"] for row in mappings}
        with ctx.sqlite_store._get_connection() as connection:
            chunk_paths = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT c.chunk_id, f.relative_path FROM code_chunks c JOIN files f ON f.id=c.file_id"
                )
            }
        mapped_chunks, mapped_paths = {}, {}
        for row in mappings:
            chunk_id = row["chunk_id"]
            relative = (
                chunk_id[: -len(":file-summary")]
                if chunk_id.endswith(":file-summary")
                else chunk_paths.get(chunk_id.split(":part:")[0])
            )
            if relative is None:
                raise ValueError("Artifact mapping has no source record")
            mapped_chunks.setdefault(row["point_id"], set()).add(chunk_id)
            mapped_paths.setdefault(row["point_id"], set()).add(relative)
        seen = set()
        with self.dispatcher._semantic_registry.lease(ctx.repo_id, ctx=ctx) as indexer:
            profile = indexer.semantic_profile.profile_id
            record = metadata.get("semantic_profiles", {}).get(profile, {})
            if not record.get("attested") or any(
                row["profile_id"] != profile or row["collection"] != manifest.get("collection")
                for row in mappings
            ):
                raise ValueError("Artifact vectors lack an attested profile owner")
            indexer._prepare_for_writes()
            indexer._check_indexed_profile(indexer._attestation, record=record)
            batch = []

            def flush():
                if not batch:
                    return
                result = indexer.qdrant.upsert(indexer.collection, batch, wait=True)
                if getattr(result, "status", None) not in {
                    "completed",
                    models.UpdateStatus.COMPLETED,
                }:
                    raise RuntimeError("Artifact vector upsert was not acknowledged")
                batch.clear()

            with (extracted / "semantic-vectors.jsonl").open(encoding="utf-8") as source:
                for line in source:
                    point = models.PointStruct.model_validate_json(line)
                    if point.id not in ids:
                        continue  # Locally retained imported rows take precedence.
                    if point.id in seen:
                        raise ValueError("Artifact contains duplicate vector identities")
                    if (
                        not isinstance(point.vector, list)
                        or len(point.vector) != indexer.embedding_dimension
                        or not all(math.isfinite(value) for value in point.vector)
                    ):
                        raise ValueError("Artifact vector dimensions do not match provenance")
                    payload = dict(point.payload or {})
                    relative = payload.get("relative_path")
                    if (
                        not isinstance(relative, str)
                        or Path(relative).is_absolute()
                        or ".." in Path(relative).parts
                    ):
                        raise ValueError("Artifact vector source path is invalid")
                    if relative not in mapped_paths[point.id]:
                        raise ValueError("Artifact vector points at a different source file")
                    expected_chunks = mapped_chunks[point.id]
                    if (
                        payload.get("chunk_id") not in expected_chunks
                        and payload.get("source_chunk_id") not in expected_chunks
                    ):
                        raise ValueError(
                            "Artifact vector payload does not match its storage mapping"
                        )
                    payload["file"] = str(indexer.path_resolver.resolve_path(relative))
                    batch.append(
                        models.PointStruct(id=point.id, vector=point.vector, payload=payload)
                    )
                    seen.add(point.id)
                    if len(batch) == 256:
                        flush()
                flush()
            if seen != ids:
                raise ValueError("Artifact vector mappings are incomplete")
            with ctx.sqlite_store._get_connection() as connection:
                for row in mappings:
                    connection.execute(
                        "UPDATE semantic_points SET collection=? WHERE profile_id=? AND chunk_id=?",
                        (indexer.collection, profile, row["chunk_id"]),
                    )
                all_ids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT point_id FROM semantic_points WHERE profile_id=?",
                        (profile,),
                    )
                ]
            indexer.write_collection_provenance(all_ids)

    def _rebuild_repository_index_locked(
        self,
        repo_id: str,
        *,
        changes: Optional[ChangeSet] = None,
        force_full: bool = False,
        stage_operation=None,
        replace_derived: bool = False,
    ) -> IndexSyncResult:
        start_time = datetime.now()
        repo_info = self.registry.get_repository(repo_id)
        if repo_info is None:
            return IndexSyncResult(action="failed", commit="", error="Repository not found")

        git_state = self.registry.update_git_state(repo_id)
        current_commit = git_state.get("commit") if git_state else None
        current_branch = git_state.get("branch") if git_state else None
        if not current_commit:
            return IndexSyncResult(action="failed", commit="", error="Failed to get current commit")
        repo_info.current_commit = current_commit
        if current_branch:
            repo_info.current_branch = current_branch
        readiness = ReadinessClassifier.classify_registered(repo_info)
        repo_path = Path(repo_info.path).resolve()
        if readiness.state in _RECOVERABLE_REBUILD_STATES and not self._git_source_unchanged(
            repo_path, current_commit, current_branch
        ):
            readiness = replace(
                readiness,
                state=RepositoryReadinessState.STALE_COMMIT,
                remediation="Commit or discard tracked edits before rebuilding the index.",
            )
            return IndexSyncResult(
                action="refused",
                commit=current_commit,
                code=readiness.state.value,
                readiness=readiness.to_dict(),
                error=readiness.remediation,
            )
        if readiness.state not in _RECOVERABLE_REBUILD_STATES:
            return IndexSyncResult(
                action="refused",
                commit=current_commit,
                code=readiness.state.value,
                readiness=readiness.to_dict(),
                error=readiness.remediation,
                duration_seconds=(datetime.now() - start_time).total_seconds(),
            )

        active_path = Path(repo_info.index_path).resolve()
        index_root = Path(repo_info.index_location).resolve()
        generation = uuid.uuid4().hex
        generation_dir = index_root / "generations"
        generation_path = generation_dir / f"{generation}.db"
        stage_dir = None
        stage_store = None
        result = UpdateResult()
        indexed_before = repo_info.last_indexed_commit
        full_call_started = False
        full_call_completed = False

        try:
            self.registry.begin_generation_mutation(
                repo_id,
                expected_registration_id=repo_info.registration_id,
                expected_generation=repo_info.index_generation,
            )
            if force_full:
                self._write_force_full_exit_trace(
                    repo_info,
                    {
                        "status": "running",
                        "stage": "force_full_started",
                        "stage_family": "lexical",
                        "current_commit": current_commit,
                        "indexed_commit_before": indexed_before,
                        "last_progress_path": None,
                        "in_flight_path": None,
                        "summary_call_timed_out": False,
                        "summary_call_file_path": None,
                        "summary_call_chunk_ids": [],
                        "summary_call_timeout_seconds": None,
                        "blocker_source": "lexical_mutation",
                        "process_id": os.getpid(),
                    },
                )
            generation_dir.mkdir(parents=True, exist_ok=True)
            stage_dir = Path(tempfile.mkdtemp(prefix=".current.db.staging-", dir=index_root))
            source_root = stage_dir / "source"
            source_root.mkdir()
            hashes = self._snapshot_committed_inputs(repo_path, current_commit, source_root)
            if active_path.exists() and readiness.state != RepositoryReadinessState.CORRUPT_SQLITE:
                SQLiteStore.snapshot_database(active_path, generation_path)
            stage_store = SQLiteStore(
                str(generation_path), path_resolver=PathResolver(repo_path, source_root=source_root)
            )
            from ..config.settings import get_settings

            settings = get_settings()
            profile = (
                settings.get_semantic_default_profile()
                if getattr(self.dispatcher, "_semantic_enabled", False) is True
                else settings.semantic_default_profile
            )
            if replace_derived or (changes is None and stage_operation is None):
                stage_store.prepare_generation(hashes, profile)
            else:
                with stage_store._get_connection() as connection:
                    connection.execute("DELETE FROM query_cache")
                    connection.execute("DELETE FROM parse_cache")
            stage_info = replace(
                repo_info,
                index_path=generation_path,
                index_generation=generation,
                index_profile=profile,
                current_commit=current_commit,
                last_indexed_commit=current_commit,
                current_branch=current_branch,
                last_indexed_branch=current_branch,
                staleness_reason=None,
            )
            stage_ctx = RepoContext(
                repo_id=repo_id,
                sqlite_store=stage_store,
                workspace_root=source_root,
                tracked_branch=repo_info.tracked_branch or "",
                registry_entry=stage_info,
                staging=True,
            )
            self._rebuild_checkpoint("stage_created")
            if getattr(self.dispatcher, "_semantic_enabled", False) is True:
                if not callable(getattr(self.dispatcher, "_semantic_lease", None)):
                    raise RuntimeError("Staged semantic integration unavailable")
                self._copy_retained_vectors(repo_id, stage_ctx)
            if stage_operation is not None:
                result = stage_operation(stage_ctx)
            elif changes is None:
                full_call_started = True
                if force_full:
                    callback = self._make_force_full_progress_callback(
                        repo_info=repo_info,
                        current_commit=current_commit,
                        indexed_commit_before=indexed_before,
                    )
                    try:
                        value = self._full_index(repo_id, stage_ctx, progress_callback=callback)
                    except TypeError as exc:
                        if "progress_callback" not in str(exc):
                            raise
                        value = self._full_index(repo_id, stage_ctx)
                    result = self._normalize_update_result(value)
                else:
                    result = self._normalize_update_result(self._full_index(repo_id, stage_ctx))
                full_call_completed = True
            else:
                # Project Git changes onto the admitted snapshot and previously indexed rows.
                with stage_store._get_connection() as connection:
                    indexed = {
                        row[0] for row in connection.execute("SELECT relative_path FROM files")
                    }
                admitted = ChangeSet(added=[], modified=[], deleted=[], renamed=[])
                admitted.deleted = [path for path in changes.deleted if path in indexed]
                for path in changes.added + changes.modified:
                    if path in hashes:
                        (admitted.modified if path in indexed else admitted.added).append(path)
                    elif path in indexed:
                        admitted.deleted.append(path)
                for old_path, new_path in changes.renamed:
                    if old_path in indexed and new_path in hashes:
                        admitted.renamed.append((old_path, new_path))
                    elif old_path in indexed:
                        admitted.deleted.append(old_path)
                    elif new_path in hashes:
                        admitted.added.append(new_path)
                result = self._incremental_index_update(repo_id, stage_ctx, admitted)
            if not result.clean:
                raise RuntimeError("Staged full index did not complete cleanly")
            if getattr(self.dispatcher, "_semantic_enabled", False) is True:
                self._finalize_staged_vectors(repo_id, stage_ctx)
            stage_store.rebuild_generation_indexes()
            # Parser inputs are disposable; all published paths remain canonical.
            with stage_store._get_connection() as connection:
                for file_id, path in connection.execute("SELECT id, path FROM files").fetchall():
                    source = Path(path)
                    if source.is_relative_to(source_root):
                        canonical = str(repo_path / source.relative_to(source_root))
                        connection.execute(
                            "UPDATE files SET path=? WHERE id=?", (canonical, file_id)
                        )
                stage_store._set_config(
                    connection, "index_generation", generation, "Durable generation"
                )
            if not self._index_path_has_durable_rows(generation_path):
                raise RuntimeError("Staged index has no durable rows")

            self._rebuild_checkpoint("before_replacement")
            self._release_runtime_handles(repo_id, repo_info, stage_ctx)
            stage_store = None
            quarantine_path = None
            if readiness.state in _QUARANTINE_REBUILD_STATES:
                quarantine_path = self._quarantine_active_index(active_path, readiness.state)
            self._fsync_generation(generation_path)
            self._rebuild_checkpoint("after_replacement")
            self._rebuild_checkpoint("before_provenance")
            if not self._git_source_unchanged(repo_path, current_commit, current_branch):
                raise RuntimeError("Repository changed while the generation was building")
            self.registry.publish_generation(
                repo_id,
                generation=generation,
                index_path=generation_path,
                commit=current_commit,
                branch=current_branch or repo_info.tracked_branch,
                profile=profile,
                expected_registration_id=repo_info.registration_id,
                expected_generation=repo_info.index_generation,
            )
            repo_info.index_path = generation_path
            repo_info.index_generation = generation
            repo_info.index_profile = profile
            repo_info.last_indexed_commit = current_commit
            repo_info.last_indexed_branch = current_branch
            repo_info.staleness_reason = None
            ReadinessClassifier.clear_index_inspection_cache()
            if force_full:
                try:
                    self._write_force_full_exit_trace(
                        repo_info,
                        {
                            **(result.semantic or {}),
                            "status": "completed",
                            "stage": "force_full_completed",
                            "stage_family": "final_closeout",
                            "in_flight_path": None,
                            "blocker_source": "final_closeout",
                        },
                    )
                except OSError as exc:
                    logger.warning(
                        "Published generation trace unavailable (%s)", type(exc).__name__
                    )
            return IndexSyncResult(
                action="full_index" if changes is None else "incremental_update",
                commit=current_commit,
                files_processed=result.files_processed,
                readiness={
                    "previous_state": readiness.state.value,
                    "quarantine_path": str(quarantine_path) if quarantine_path else None,
                },
                semantic=result.semantic,
                duration_seconds=(datetime.now() - start_time).total_seconds(),
            )
        except Exception as exc:
            error = f"Staged rebuild failed ({type(exc).__name__})"
            self.registry.fail_generation_mutation(
                repo_id,
                error=error,
                expected_registration_id=repo_info.registration_id,
                expected_generation=repo_info.index_generation,
            )
            if force_full and full_call_started and not full_call_completed:
                raise
            if force_full:
                self._write_force_full_exit_trace(
                    repo_info,
                    {
                        **(result.semantic or {}),
                        **(result.low_level or {}),
                        "status": "completed",
                        "stage": "force_full_failed",
                        "stage_family": "final_closeout",
                        "blocker_source": self._trace_blocker_source(result),
                        "runtime_restore_performed": False,
                        "runtime_restore_declined_reason": "old_generation_retained",
                    },
                )
            return IndexSyncResult(
                action="failed",
                commit=current_commit,
                error=error,
                files_processed=result.files_processed,
                semantic=result.semantic,
                duration_seconds=(datetime.now() - start_time).total_seconds(),
            )
        finally:
            if force_full:
                self._finalize_running_force_full_trace_as_interrupted(
                    repo_info=repo_info,
                    current_commit=current_commit,
                    indexed_commit_before=indexed_before,
                )
            if stage_store is not None:
                try:
                    self._release_runtime_handles(repo_id, repo_info, None)
                except Exception as exc:
                    logger.warning(
                        "Failed-stage retirement remains pending: %s", type(exc).__name__
                    )
                finally:
                    stage_store.close()
            if stage_dir is not None:
                shutil.rmtree(stage_dir, ignore_errors=True)

    @staticmethod
    def _git_source_unchanged(repo_path: Path, commit: str, branch: Optional[str]) -> bool:
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
            ref = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
            dirty = subprocess.run(
                ["git", "diff", "--quiet", "--no-ext-diff", "HEAD", "--"],
                cwd=repo_path,
                capture_output=True,
                timeout=10,
            ).returncode
            return head == commit and ref == branch and dirty == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _finalize_staged_vectors(self, repo_id: str, ctx: RepoContext) -> None:
        """Drain only this unpublished owner and verify its live point mappings."""
        from qdrant_client import models

        if not ctx.staging:
            raise RuntimeError("Vector publication requires an unpublished generation")
        with self.dispatcher._semantic_registry.lease(repo_id, ctx=ctx) as staged:
            drained = ctx.sqlite_store.drain_pending_vector_deletions(
                lambda collection, ids: staged.delete_remote_points(ids, collection=collection),
                only_collection=staged.collection,
            )
            if drained["groups_failed"]:
                raise RuntimeError("Staged vector cleanup did not complete")
            with ctx.sqlite_store._get_connection() as connection:
                records = connection.execute(
                    "SELECT point_id, collection FROM semantic_points WHERE profile_id=?",
                    (staged.semantic_profile.profile_id,),
                ).fetchall()
            if records:
                record = staged._indexed_profile_record()
                if not record or not record.get("attested"):
                    raise RuntimeError("Staged vectors lack attested provenance")
            if any(row["collection"] != staged.collection for row in records):
                raise RuntimeError("Staged vectors belong to another generation")
            ids = list(dict.fromkeys(row["point_id"] for row in records))
            remote_count = staged.qdrant.count(
                collection_name=staged.collection,
                exact=True,
                count_filter=models.Filter(
                    must_not=[models.HasIdCondition(has_id=[staged.PROVENANCE_POINT_ID])]
                ),
            ).count
            if remote_count != len(ids):
                raise RuntimeError("Staged vector ownership is incomplete")
            relative_paths = set()
            complete_corpus = True
            for start in range(0, len(ids), 256):
                batch = ids[start : start + 256]
                points = staged.qdrant.retrieve(
                    staged.collection,
                    batch,
                    with_payload=True,
                    with_vectors=False,
                )
                if {point.id for point in points} != set(batch):
                    raise RuntimeError("Staged vector mappings are incomplete")
                for point in points:
                    relative_path = (point.payload or {}).get("relative_path")
                    if isinstance(relative_path, str) and relative_path:
                        relative_paths.add(relative_path)
                    else:
                        complete_corpus = False
            if ids:
                staged.write_collection_provenance(
                    ids,
                    corpus_sha256=(
                        staged._compute_corpus_sha256(relative_paths) if complete_corpus else None
                    ),
                )

    def _copy_retained_vectors(self, repo_id: str, ctx: RepoContext) -> None:
        """Copy attested retained points into the stage without mutating their owner."""
        from qdrant_client import models

        from ..config.settings import get_settings

        profile = get_settings().get_semantic_default_profile()
        with ctx.sqlite_store._get_connection() as connection:
            records = connection.execute(
                "SELECT chunk_id, point_id, collection FROM semantic_points WHERE profile_id=?",
                (profile,),
            ).fetchall()
        if not records:
            return
        registry = self.dispatcher._semantic_registry
        if registry is None:
            raise RuntimeError("Semantic generation owner is unavailable")
        with registry.lease(repo_id) as original:
            with registry.lease(repo_id, ctx=ctx) as staged:
                if any(row["collection"] != original.collection for row in records):
                    raise RuntimeError("Retained vectors have no matching generation owner")
                attested = original._indexed_profile_record()
                if not attested or not attested.get("attested"):
                    raise RuntimeError("Retained vectors lack attested provenance")
                staged._prepare_for_writes()
                original._check_indexed_profile(staged._attestation)
                for start in range(0, len(records), 256):
                    batch = records[start : start + 256]
                    ids = list(dict.fromkeys(row["point_id"] for row in batch))
                    points = original.qdrant.retrieve(
                        collection_name=original.collection,
                        ids=ids,
                        with_payload=True,
                        with_vectors=True,
                    )
                    if {point.id for point in points} != set(ids):
                        raise RuntimeError("Retained vector mappings are incomplete")
                    acknowledged = staged.qdrant.upsert(
                        collection_name=staged.collection,
                        points=[
                            models.PointStruct(
                                id=point.id,
                                vector=point.vector,
                                payload=point.payload,
                            )
                            for point in points
                        ],
                        wait=True,
                    )
                    if acknowledged.status not in {"completed", models.UpdateStatus.COMPLETED}:
                        raise RuntimeError("Retained vector copy was not acknowledged")
                    with ctx.sqlite_store._get_connection() as connection:
                        connection.executemany(
                            "UPDATE semantic_points SET collection=? WHERE profile_id=? AND chunk_id=?",
                            [(staged.collection, profile, row["chunk_id"]) for row in batch],
                        )

    @staticmethod
    def _snapshot_committed_inputs(
        repo_path: Path, commit: str, destination: Path
    ) -> Dict[str, str]:
        from ..core.ignore_patterns import build_walker_filter
        from ..plugins.generic_treesitter_plugin import GenericTreeSitterPlugin

        listing = subprocess.run(
            ["git", "ls-tree", "-r", "-l", "-z", commit],
            cwd=repo_path,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
        entries = []
        for record in listing.split(b"\0"):
            if not record:
                continue
            metadata, encoded_path = record.split(b"\t", 1)
            mode, kind, oid, size = metadata.split()
            relative = Path(os.fsdecode(encoded_path))
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("Invalid committed source path")
            policy = relative.name == ".gitignore" or relative == Path(".mcp-index-ignore")
            if policy and mode not in {b"100644", b"100755"}:
                raise ValueError("Committed ignore policy must be a regular file")
            if kind != b"blob" or mode not in {b"100644", b"100755"}:
                continue
            bounded = GenericTreeSitterPlugin.uses_exact_bounded_json_path(
                relative
            ) or GenericTreeSitterPlugin.uses_exact_bounded_jsonl_path(relative)
            if int(size) > get_max_file_size_bytes() and not bounded:
                if policy:
                    raise ValueError("Committed ignore policy exceeds the input limit")
                continue
            entries.append((relative, oid.decode("ascii"), policy))

        hashes = {}
        resolver = PathResolver(repo_path)

        def copy_blob(entry):
            relative, oid, _policy = entry
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                subprocess.run(
                    ["git", "cat-file", "blob", oid],
                    cwd=repo_path,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    check=True,
                    timeout=30,
                )
            hashes[relative.as_posix()] = resolver.compute_content_hash(target)

        # Load committed policies from parents first, before reading ordinary blobs.
        policies = sorted(
            (entry for entry in entries if entry[2]), key=lambda entry: len(entry[0].parts)
        )
        for entry in policies:
            if len(entry[0].parts) == 1 or not build_walker_filter(destination)(
                destination / entry[0]
            ):
                copy_blob(entry)
        excluded = build_walker_filter(destination)
        for entry in entries:
            relative = entry[0]
            if relative.as_posix() in hashes or entry[2] or excluded(destination / relative):
                continue
            copy_blob(entry)
        return hashes

    @staticmethod
    def _fsync_generation(database: Path) -> None:
        paths = [database]
        semantic = database.parent / (database.stem + ".semantic")
        directories = [database.parent, database.parent.parent]
        if semantic.exists():
            for root, _dirs, files in os.walk(semantic):
                directory = Path(root)
                directories.append(directory)
                paths.extend(directory / name for name in files)
        for path in paths:
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        if os.name != "nt":
            for directory in sorted(
                set(directories), key=lambda path: len(path.parts), reverse=True
            ):
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

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
                if isinstance(self.registry, RepositoryRegistry):
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
            repo_path = ctx.workspace_root

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
        if mutation.semantic and (
            mutation.semantic.get("semantic_failed") or mutation.semantic.get("semantic_blocked")
        ):
            result.failed += 1
            result.errors.append("Required semantic mutation did not complete")
            return
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
            if semantic.get("semantic_failed", 0) or semantic.get("semantic_blocked", 0):
                return "Semantic stage has failed or blocked files"
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

        repo_path = ctx.workspace_root

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
            sqlite_store.close()
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
