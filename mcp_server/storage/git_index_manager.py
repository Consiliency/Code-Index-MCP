"""Git-aware index manager for commit-synchronized indexing.

This module provides index management that's synchronized with git commits,
supporting incremental updates and artifact management.
"""

import hashlib
import inspect
import json
import logging
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..artifacts.commit_artifacts import CommitArtifactManager
from ..config.env_vars import (
    get_index_generation_retention,
    get_index_git_timeout_seconds,
    get_max_file_size_bytes,
)
from ..core.ignore_patterns import EXCLUDED_DIR_PARTS, build_walker_filter
from ..core.path_resolver import PathResolver
from ..core.repo_context import RepoContext
from ..core.repo_resolver import RepoResolver
from ..dispatcher.dispatcher_enhanced import (
    EnhancedDispatcher,
    IndexResult,
    IndexResultStatus,
    _redact_secrets,
    is_grammar_unavailable_result,
    is_unsupported_file_result,
)
from ..health.repo_status import build_health_row
from ..health.repository_readiness import (
    ReadinessClassifier,
    RepositoryReadiness,
    RepositoryReadinessState,
    coverage_from_marker,
    published_grammar_degradation,
)
from ..indexing.change_detector import ChangeDetector
from ..indexing.lock_registry import lock_registry
from ..plugins.language_registry import get_language_by_extension
from ..utils import grammar_cache
from .repository_registry import RepositoryRegistry
from .sqlite_store import SnapshotStalledError, SQLiteStore, read_grammar_degradation
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
    #: Languages the published generation left out because their grammars
    #: were unavailable, as stored in the marker (#117); None when complete.
    degraded_languages: Optional[Dict[str, Any]] = None


@dataclass
class UpdateResult:
    """Result of incremental index update."""

    indexed: int = 0
    deleted: int = 0
    moved: int = 0
    failed: int = 0
    skipped: int = 0
    #: Changed files that no plugin stores (for example a disabled-by-default
    #: language). Ignored exactly as the full walk ignores them (#111).
    ignored: int = 0
    errors: List[str] = None
    semantic: Optional[Dict[str, Any]] = None
    low_level: Optional[Dict[str, Any]] = None
    duration_seconds: float = 0.0
    #: Admitted files left out because their language's grammar was
    #: unavailable (#117). They do not make the result unclean: the whole
    #: language is dropped from the generation and recorded in its marker.
    deferred: int = 0
    degraded_languages: Optional[Dict[str, Dict[str, Any]]] = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []
        if self.degraded_languages is None:
            self.degraded_languages = {}

    def defer(self, language: Optional[str], grammars: Any = (), files: int = 1) -> None:
        self.deferred += files
        entry = self.degraded_languages.setdefault(
            language or "unknown", {"grammars": [], "files": 0}
        )
        entry["files"] += files
        entry["grammars"] = sorted(set(entry["grammars"]) | set(grammars or ()))

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


#: Bounds for the per-file error summary carried by a failed sync's stored error
#: and CLI output (#111).
_SYNC_ERROR_MAX_ITEMS = 5
_SYNC_ERROR_ITEM_CHARS = 300
_SYNC_ERROR_TOTAL_CHARS = 2000


def _safe_error_text(text: object, strip_prefixes: List[str]) -> str:
    """Redact secret-like substrings and absolute checkout/stage prefixes."""
    value = str(text)
    for prefix in sorted(strip_prefixes, key=len, reverse=True):
        if prefix:
            value = value.replace(prefix.rstrip("/\\") + os.sep, "").replace(prefix, "")
    value = _redact_secrets(value) or ""
    value = " ".join(value.split())
    if len(value) > _SYNC_ERROR_ITEM_CHARS:
        value = value[: _SYNC_ERROR_ITEM_CHARS - 3] + "..."
    return value


def _format_rebuild_error(
    exc: BaseException,
    result: "UpdateResult",
    *,
    strip_prefixes: List[str],
) -> str:
    """Describe a failed staged rebuild with its message and a bounded error summary."""
    message = (
        _safe_error_text(exc, strip_prefixes) if isinstance(exc, _DESCRIBED_REBUILD_ERRORS) else ""
    )
    error = f"Staged rebuild failed ({type(exc).__name__}" + (f": {message})" if message else ")")
    errors = [item for item in (result.errors or []) if str(item).strip()]
    if errors:
        details = [
            _safe_error_text(item, strip_prefixes) for item in errors[:_SYNC_ERROR_MAX_ITEMS]
        ]
        error += f"; {len(errors)} file error(s): " + "; ".join(details)
        if len(errors) > _SYNC_ERROR_MAX_ITEMS:
            error += f"; and {len(errors) - _SYNC_ERROR_MAX_ITEMS} more"
    if len(error) > _SYNC_ERROR_TOTAL_CHARS:
        error = error[: _SYNC_ERROR_TOTAL_CHARS - 3] + "..."
    return error


#: Generations this code published or failed, with when each was last live.
#: Only generations recorded here can ever be pruned (#114 review B1).
_GENERATION_HISTORY_FILE = "generation-history.json"
_GENERATION_HISTORY_VERSION = 2
_GENERATION_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_GENERATION_SIDECARS = ("-wal", "-shm", "-journal")
#: An operator pin: ``generations/<id>.keep`` keeps that generation forever.
_GENERATION_PIN_SUFFIX = ".keep"
#: Failed (never published) generations kept as evidence, newest first.
_FAILED_GENERATION_RETENTION = 1


def _is_generation_id(value: str) -> bool:
    return bool(_GENERATION_ID_PATTERN.fullmatch(value))


def _read_generation_history(path: Path) -> Dict[str, Dict[str, Any]]:
    """Return recorded generations; a missing or unreadable file records nothing.

    An empty history means nothing is prunable, so losing the file can only
    keep generations, never delete them.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Generation history unreadable (%s); nothing is pruned", type(exc).__name__)
        return {}
    if not isinstance(data, dict) or data.get("version") != _GENERATION_HISTORY_VERSION:
        logger.warning("Generation history has an unknown format; nothing is pruned")
        return {}
    entries: Dict[str, Dict[str, Any]] = {}
    for generation_id, entry in (data.get("generations") or {}).items():
        if (
            isinstance(generation_id, str)
            and _is_generation_id(generation_id)
            and isinstance(entry, dict)
            and entry.get("state") in {"published", "failed"}
            and isinstance(entry.get("recorded_at"), (int, float))
        ):
            entries[generation_id] = {
                "state": entry["state"],
                "recorded_at": float(entry["recorded_at"]),
                "last_live_at": (
                    float(entry["last_live_at"])
                    if isinstance(entry.get("last_live_at"), (int, float))
                    else None
                ),
            }
    return entries


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_generation_history(path: Path, entries: Dict[str, Dict[str, Any]]) -> None:
    """Durably replace the history file (fsync the file, then its directory)."""
    payload = (
        json.dumps(
            {"version": _GENERATION_HISTORY_VERSION, "generations": entries},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _generation_paths(database: Path) -> List[Path]:
    return [database, *(Path(f"{database}{suffix}") for suffix in _GENERATION_SIDECARS)]


def _generations_held_open(databases: List[Path]) -> set:
    """Return the databases an active reader may still hold.

    A database counts as held when any of its SQLite sidecars exists (an open
    WAL connection keeps ``-wal``/``-shm``; a clean last close removes them), or,
    where ``/proc`` is available, when any process this user can inspect has the
    database or a sidecar open. On POSIX an unlinked file stays readable to an
    existing descriptor, and the previous generation is always retained, so a
    reader the scan cannot see (another user's process, or a platform without
    ``/proc``) is still protected by the retention window.
    """
    held = {
        database
        for database in databases
        if any(path.exists() for path in _generation_paths(database)[1:])
    }
    remaining = {
        str(path): database
        for database in databases
        if database not in held
        for path in _generation_paths(database)
    }
    proc = Path("/proc")
    if not remaining or not proc.is_dir():
        return held
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            descriptors = os.listdir(process / "fd")
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(process / "fd" / descriptor)
            except OSError:
                continue
            database = remaining.get(target.removesuffix(" (deleted)"))
            if database is not None:
                held.add(database)
        if len(held) == len(databases):
            break
    return held


def _remove_generation_files(database: Path) -> Optional[int]:
    """Remove one generation's semantic directory, sidecars and database.

    The database goes last, so an interrupted removal leaves a recorded
    generation that the next prune retries instead of orphaned sidecars.
    Returns the bytes freed, or ``None`` when the database could not be removed
    (for example a Windows reader still holds it).
    """
    freed = 0
    semantic = database.with_suffix(".semantic")
    if semantic.is_dir() and not semantic.is_symlink():
        for root, _dirs, files in os.walk(semantic):
            for name in files:
                try:
                    freed += (Path(root) / name).stat().st_size
                except OSError:
                    pass
        shutil.rmtree(semantic, ignore_errors=True)
        if semantic.exists():
            # Keep the database and its history record so the next prune
            # retries; deleting them now would orphan the semantic directory
            # forever (#129 re-check nit).
            logger.warning("Generation semantic directory kept: %s", semantic.name)
            return None
    for path in reversed(_generation_paths(database)):
        try:
            size = path.stat().st_size
            path.unlink()
            freed += size
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Generation file kept (%s): %s", type(exc).__name__, path.name)
            if path == database:
                return None
    return freed


#: Test-only seam for the installed-wheel shutdown smoke: when both are set and
#: the hold file exists, the staged rebuild parks at that checkpoint (writing
#: ``<hold file>.held``) until the file is removed or shutdown is requested.
#: This makes "a writer is in flight when shutdown starts" deterministic without
#: relying on a fixture being slow enough. Unset in production, it is a no-op.
_REBUILD_HOLD_STAGE_ENV = "MCP_TEST_REBUILD_HOLD_STAGE"
_REBUILD_HOLD_FILE_ENV = "MCP_TEST_REBUILD_HOLD_FILE"


class RebuildShutdownRequested(RuntimeError):
    """The owning service is shutting down; abandon the unpublished generation."""


class StagedRebuildError(RuntimeError):
    """A staged rebuild refused to publish; its message is fixed, safe text."""

    code: Optional[str] = None


class GrammarUnavailableRebuildError(StagedRebuildError):
    """Every admitted file was deferred for an unavailable grammar (#117)."""

    code = grammar_cache.GRAMMAR_UNAVAILABLE


#: Code of a refused restore whose artifact does not cover the committed inputs.
ARTIFACT_INCOMPLETE = "artifact_incomplete"


class ArtifactIncompleteError(StagedRebuildError):
    """A restored artifact lacks admitted committed files (#117 review)."""

    code = ARTIFACT_INCOMPLETE


#: Exceptions whose messages are authored here and safe to store and print.
#: Other exception text (OS, SQLite, plugin errors) can carry private detail, so
#: only their type is reported; per-file detail comes from ``UpdateResult.errors``.
_DESCRIBED_REBUILD_ERRORS = (StagedRebuildError, RebuildShutdownRequested, SnapshotStalledError)

#: How often a running git subprocess is checked for shutdown and its deadline.
_GIT_POLL_SECONDS = 0.25
#: How long to wait for a killed git process group to release its pipes.
_GIT_REAP_SECONDS = 5.0
#: How long the best-effort Windows ``taskkill /T`` may take.
_GIT_TASKKILL_SECONDS = 5.0
#: Platform switch for the git process-tree handling; tests replace it.
_WINDOWS = os.name == "nt"
#: Minimum spacing of snapshot heartbeats written into the force-full trace.
_SNAPSHOT_TRACE_HEARTBEAT_SECONDS = 5.0
#: Minimum spacing of snapshot progress log lines.
_SNAPSHOT_LOG_HEARTBEAT_SECONDS = 30.0
#: Clock for git deadlines and snapshot heartbeats; tests replace it.
_monotonic = time.monotonic


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
        self._shutdown_event = threading.Event()

    @property
    def _shutdown(self) -> threading.Event:
        event = self.__dict__.get("_shutdown_event")
        if event is None:
            event = self.__dict__.setdefault("_shutdown_event", threading.Event())
        return event

    @property
    def _staged_grammar(self) -> threading.local:
        local = self.__dict__.get("_staged_grammar_local")
        if local is None:
            local = self.__dict__.setdefault("_staged_grammar_local", threading.local())
        return local

    @contextmanager
    def _staged_grammar_scope(self, availability: Optional[grammar_cache.GrammarAvailability]):
        """Opt this thread's staged walk in to per-language degradation (#117).

        The snapshot reaches ``_full_index`` / ``_incremental_index_update``
        through this scope, not their call signature, so it applies even when
        it is complete: a grammar that becomes unavailable mid-build then
        defers its language instead of failing the whole build, exactly as
        if it had been missing at the start. Overrides and test doubles of
        those methods keep their signatures.
        """
        local = self._staged_grammar
        previous = getattr(local, "availability", None)
        local.availability = availability
        try:
            yield
        finally:
            local.availability = previous

    def _scoped_grammar_availability(
        self, explicit: Optional[grammar_cache.GrammarAvailability]
    ) -> Optional[grammar_cache.GrammarAvailability]:
        if explicit is not None:
            return explicit
        return getattr(self._staged_grammar, "availability", None)

    def request_shutdown(self) -> None:
        """Ask in-flight staged rebuilds to stop before they publish.

        The owning service calls this when shutdown starts. A rebuild stops
        between files and at every pre-publication checkpoint, abandoning its
        staged generation (the active index is untouched), so the service's
        graceful cleanup finishes inside the shutdown grace window instead of
        waiting for a full rebuild and then being force-exited
        (#116 review S1).
        """
        self._shutdown.set()

    def shutdown_requested(self) -> bool:
        return self._shutdown.is_set()

    def sync_repository_index(
        self,
        repo_id: str,
        force_full: bool = False,
        bypass_branch_guard: bool = False,
        *,
        expected_registration_id: Optional[str] = None,
        require_auto_sync: bool = False,
    ) -> IndexSyncResult:
        """Serialize repository synchronization through the shared per-repo lock."""
        repo = self.registry.get_repository(repo_id)
        with lock_registry.acquire(repo_id, repo_path=repo.path if repo else None):
            return self._sync_repository_index_locked(
                repo_id,
                force_full=force_full,
                bypass_branch_guard=bypass_branch_guard,
                expected_registration_id=expected_registration_id,
                require_auto_sync=require_auto_sync,
            )

    def _sync_repository_index_locked(
        self,
        repo_id: str,
        force_full: bool = False,
        bypass_branch_guard: bool = False,
        *,
        expected_registration_id: Optional[str] = None,
        require_auto_sync: bool = False,
    ) -> IndexSyncResult:
        """Sync under the repository lock; a force-full run owns a fresh trace.

        The force-full trace is reset here, before any check that can refuse
        or fail the run, and every outcome that never reached the staged
        rebuild's own closeout is written as a terminal trace, so status never
        shows a previous run's fields for a new attempt (#112).
        """
        kwargs = {
            "force_full": force_full,
            "bypass_branch_guard": bypass_branch_guard,
            "expected_registration_id": expected_registration_id,
            "require_auto_sync": require_auto_sync,
        }
        trace_repo = self.registry.get_repository(repo_id) if force_full else None
        # A queued job from an old registration (or one whose automatic sync
        # was turned off) is refused below without running; it must not reset
        # or close the trace that belongs to the current registration.
        if trace_repo is not None and (
            (
                expected_registration_id is not None
                and trace_repo.registration_id != expected_registration_id
            )
            or (require_auto_sync and (not trace_repo.auto_sync or not trace_repo.active))
        ):
            trace_repo = None
        if trace_repo is None:
            return self._sync_repository_index_unwrapped(repo_id, **kwargs)
        indexed_before = trace_repo.last_indexed_commit
        try:
            self._start_force_full_exit_trace(
                trace_repo,
                self._force_full_started_trace(
                    trace_repo.current_commit,
                    indexed_before,
                    registration_id=self._trace_registration_id(trace_repo),
                ),
            )
        except OSError as exc:
            logger.warning("Force-full trace unavailable (%s)", type(exc).__name__)
        try:
            result = self._sync_repository_index_unwrapped(repo_id, **kwargs)
        except BaseException:
            if self._force_full_trace_owned_by(trace_repo):
                self._close_force_full_trace_safely(
                    self._finalize_running_force_full_trace_as_interrupted,
                    repo_info=trace_repo,
                    current_commit=trace_repo.current_commit,
                    indexed_commit_before=indexed_before,
                )
            raise
        self._close_force_full_trace_safely(
            self._terminalize_unadmitted_force_full_trace, trace_repo, result
        )
        return result

    @staticmethod
    def _trace_registration_id(repo_info: Any) -> Optional[str]:
        registration_id = getattr(repo_info, "registration_id", None)
        return registration_id if isinstance(registration_id, str) else None

    def _force_full_trace_owned_by(self, repo_info: Any) -> bool:
        """Whether the stored trace was started by this registration."""
        try:
            trace = self._read_force_full_exit_trace(repo_info) or {}
        except OSError:
            return False
        return trace.get("registration_id") == self._trace_registration_id(repo_info)

    @staticmethod
    def _force_full_started_trace(
        current_commit: Optional[str],
        indexed_commit_before: Optional[str],
        *,
        registration_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "registration_id": registration_id,
            "status": "running",
            "stage": "force_full_started",
            "stage_family": "lexical",
            "current_commit": current_commit,
            "indexed_commit_before": indexed_commit_before,
            "last_progress_path": None,
            "in_flight_path": None,
            "summary_call_timed_out": False,
            "summary_call_file_path": None,
            "summary_call_chunk_ids": [],
            "summary_call_timeout_seconds": None,
            "blocker_source": "lexical_mutation",
            "process_id": os.getpid(),
        }

    @staticmethod
    def _close_force_full_trace_safely(close: Any, *args: Any, **kwargs: Any) -> None:
        try:
            close(*args, **kwargs)
        except Exception as exc:
            logger.warning("Force-full trace closeout unavailable (%s)", type(exc).__name__)

    def _terminalize_unadmitted_force_full_trace(
        self, repo_info: Any, result: IndexSyncResult
    ) -> None:
        """Close a trace the staged rebuild never closed (refused or failed early)."""
        if result.action in {"full_index", "incremental_update"}:
            return
        trace = self._read_force_full_exit_trace(repo_info) or {}
        if trace.get("status") != "running":
            return
        if trace.get("registration_id") != self._trace_registration_id(repo_info):
            return  # another registration's trace: never close it
        self._write_force_full_exit_trace(
            repo_info,
            {
                "status": "completed",
                "stage": "force_full_failed" if result.action == "failed" else "force_full_refused",
                "stage_family": "final_closeout",
                "current_commit": result.commit or trace.get("current_commit"),
                "blocker_source": "admission",
                "result_action": result.action,
                "result_code": result.code,
                "runtime_restore_performed": False,
                "runtime_restore_declined_reason": "old_generation_retained",
            },
        )

    def _sync_repository_index_unwrapped(
        self,
        repo_id: str,
        force_full: bool = False,
        bypass_branch_guard: bool = False,
        *,
        expected_registration_id: Optional[str] = None,
        require_auto_sync: bool = False,
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
        if require_auto_sync and (not repo_info.auto_sync or not repo_info.active):
            return IndexSyncResult(action="refused", commit="", error="Automatic sync disabled")
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

        readiness = ReadinessClassifier.classify_registered(repo_info)
        if readiness.state not in {
            RepositoryReadinessState.READY,
            RepositoryReadinessState.STALE_COMMIT,
        }:
            return self._rebuild_repository_index_locked(
                repo_id,
                force_full=force_full,
                expected_registration_id=repo_info.registration_id,
                require_auto_sync=require_auto_sync,
            )

        # Matching provenance is insufficient when the database is not ready.
        if (
            current_commit == last_indexed_commit
            and readiness.ready
            and not force_full
            and not repo_info.staleness_reason
            and index_exists_before_mutation
            and self._git_source_unchanged(repo_path, current_commit, current_branch)
        ):
            recoverable = self.recoverable_degraded_languages(repo_info)
            if recoverable:
                # Grammars for left-out languages arrived: index only their
                # files through an empty-change-set incremental build, so no
                # other language's rows or chunk ids change (#117).
                logger.info(
                    "Repairing degraded coverage of %s: %s", repo_id, ", ".join(sorted(recoverable))
                )
                return self._rebuild_repository_index_locked(
                    repo_id,
                    changes=ChangeSet(added=[], modified=[], deleted=[], renamed=[]),
                    expected_registration_id=repo_info.registration_id,
                    require_auto_sync=require_auto_sync,
                )
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
            and not (published_grammar_degradation(Path(repo_info.index_path)) or {}).get(
                "unreadable"
            )
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
            repo_id,
            changes=changes,
            force_full=force_full,
            expected_registration_id=repo_info.registration_id,
            require_auto_sync=require_auto_sync,
        )

    def rebuild_repository_index(self, repo_id: str) -> IndexSyncResult:
        """Build a full sibling index and publish it atomically for one repository."""
        repo = self.registry.get_repository(repo_id)
        with lock_registry.acquire(repo_id, repo_path=repo.path if repo else None):
            return self._rebuild_repository_index_locked(repo_id)

    def restore_verified_artifact(
        self, repo_id: str, extracted: Path, *, expected_commit: str, expected_owner=None
    ) -> IndexSyncResult:
        """Admit an integrity/identity/signature-verified archive through generation publication."""
        repo = self.registry.get_repository(repo_id)
        if repo is None:
            raise KeyError(repo_id)
        if expected_owner is None:
            expected_owner = repo
        database = extracted / "current.db"
        if not database.is_file():
            raise ValueError("Artifact has no portable current.db generation")
        # A degraded generation must never be published from an artifact; this
        # version refuses to upload one, so a marker means another writer
        # (#117 review). Refused before any generation mutation is admitted.
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            if read_grammar_degradation(source):
                raise ValueError(
                    "Artifact records degraded grammar coverage; build the index locally"
                )
        restored: Dict[str, Any] = {}

        def restore(ctx):
            if ctx.registry_entry.current_commit != expected_commit:
                raise ValueError("Artifact commit changed before staging")
            with closing(
                sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            ) as source:
                version = source.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            if version is None or version > SQLiteStore.SCHEMA_VERSION:
                raise ValueError("Artifact SQLite schema is unsupported")
            if version < SQLiteStore.SCHEMA_VERSION:
                with tempfile.TemporaryDirectory(
                    prefix=".artifact-upgrade-", dir=Path(ctx.sqlite_store.db_path).parent
                ) as temporary:
                    upgraded = Path(temporary) / "current.db"
                    # No total ceiling (#112), so shutdown must be able to stop it.
                    SQLiteStore.snapshot_database(
                        database,
                        upgraded,
                        checkpoint=lambda: self._rebuild_checkpoint("artifact_upgrade"),
                        clock=_monotonic,
                    )
                    migrated = SQLiteStore(str(upgraded))
                    migrated.close()
                    restored["paths"] = self._artifact_file_paths(upgraded)
                    mappings = ctx.sqlite_store.import_artifact_rows(upgraded, ctx.workspace_root)
            else:
                restored["paths"] = self._artifact_file_paths(database)
                mappings = ctx.sqlite_store.import_artifact_rows(database, ctx.workspace_root)
            if mappings:
                self._restore_artifact_vectors(ctx, extracted, mappings)
            with ctx.sqlite_store._get_connection() as connection:
                count = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            return UpdateResult(indexed=count)

        def verify(hashes: Dict[str, str], stored: Any) -> None:
            # The stage keeps the live generation's file rows, so completeness
            # is judged from the artifact's own file set: every committed file
            # the full walk would store must be in it. Path sets only, no
            # re-hashing (#117 review: an older uploader strips the marker).
            paths = restored.get("paths") or frozenset()
            missing = sum(1 for path in hashes if path not in paths and stored(path))
            if missing:
                raise ArtifactIncompleteError(
                    f"Artifact lacks {missing} committed file(s) this host indexes; "
                    "build the index locally"
                )

        with lock_registry.acquire(repo_id, repo_path=repo.path):
            return self._rebuild_repository_index_locked(
                repo_id,
                stage_operation=restore,
                replace_derived=True,
                expected_owner=expected_owner,
                verify_stage=verify,
            )

    @staticmethod
    def _artifact_file_paths(database: Path) -> frozenset:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            return frozenset(
                str(row[0])
                for row in source.execute(
                    "SELECT relative_path FROM files WHERE is_deleted = 0 OR is_deleted IS NULL"
                )
                if row[0]
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
        mapped_chunks: Dict[int | str | uuid.UUID, set[str]] = {}
        mapped_paths: Dict[int | str | uuid.UUID, set[str]] = {}
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
        assert self.dispatcher is not None
        assert self.dispatcher._semantic_registry is not None
        with self.dispatcher._semantic_registry.lease(ctx.repo_id, ctx=ctx) as indexer:
            profile = indexer.semantic_profile.profile_id
            record = metadata.get("semantic_profiles", {}).get(profile, {})
            if not record.get("attested") or any(
                row["profile_id"] != profile or row["collection"] != manifest.get("collection")
                for row in mappings
            ):
                raise ValueError("Artifact vectors lack an attested profile owner")
            indexer._prepare_for_writes()
            assert indexer._attestation is not None
            indexer._check_indexed_profile(indexer._attestation, record=record)
            batch: List[models.PointStruct] = []

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
                        or not all(
                            isinstance(value, (int, float)) and math.isfinite(value)
                            for value in point.vector
                        )
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
        expected_registration_id: Optional[str] = None,
        require_auto_sync: bool = False,
        expected_owner=None,
        verify_stage=None,
    ) -> IndexSyncResult:
        start_time = datetime.now()
        repo_info = self.registry.get_repository(repo_id)
        if repo_info is None:
            return IndexSyncResult(action="failed", commit="", error="Repository not found")
        if expected_owner is not None and (
            repo_info.registration_id != expected_owner.registration_id
            or repo_info.index_generation != expected_owner.index_generation
        ):
            return IndexSyncResult(action="refused", commit="", error="Artifact owner changed")
        if (
            expected_registration_id is not None
            and repo_info.registration_id != expected_registration_id
        ):
            return IndexSyncResult(
                action="refused", commit="", error="Repository registration changed"
            )
        if require_auto_sync and (not repo_info.auto_sync or not repo_info.active):
            return IndexSyncResult(action="refused", commit="", error="Automatic sync disabled")

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
        source_unchanged = True
        if readiness.state in _RECOVERABLE_REBUILD_STATES:
            try:
                source_unchanged = self._git_source_unchanged(
                    repo_path, current_commit, current_branch, strict=True
                )
            except (subprocess.TimeoutExpired, RebuildShutdownRequested) as exc:
                # Nothing was admitted yet; report why without recording a
                # failed generation or a false STALE_COMMIT refusal (#112).
                detail = (
                    StagedRebuildError("Git source check exceeded MCP_INDEX_GIT_TIMEOUT_SECONDS")
                    if isinstance(exc, subprocess.TimeoutExpired)
                    else exc
                )
                return IndexSyncResult(
                    action="failed",
                    commit=current_commit,
                    error=_format_rebuild_error(
                        detail, UpdateResult(), strip_prefixes=[str(repo_path)]
                    ),
                    duration_seconds=(datetime.now() - start_time).total_seconds(),
                )
        if not source_unchanged:
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
        stage_ctx = None
        result = UpdateResult()
        indexed_before = repo_info.last_indexed_commit
        full_call_started = False
        full_call_completed = False
        mutation_admitted = False

        try:
            self.registry.begin_generation_mutation(
                repo_id,
                expected_registration_id=repo_info.registration_id,
                expected_generation=repo_info.index_generation,
                **({"require_auto_sync": True} if require_auto_sync else {}),
            )
            mutation_admitted = True
            if force_full:
                # Normally already fresh from the sync entry; resetting again
                # keeps a direct force-full caller from inheriting old fields.
                self._start_force_full_exit_trace(
                    repo_info,
                    self._force_full_started_trace(
                        current_commit,
                        indexed_before,
                        registration_id=self._trace_registration_id(repo_info),
                    ),
                )
            generation_dir.mkdir(parents=True, exist_ok=True)
            stage_dir = Path(tempfile.mkdtemp(prefix=".current.db.staging-", dir=index_root))
            source_root = stage_dir / "source"
            source_root.mkdir()
            try:
                hashes = self._snapshot_committed_inputs(
                    repo_path,
                    current_commit,
                    source_root,
                    on_progress=self._make_snapshot_heartbeat(
                        repo_info if force_full else None,
                        stage="snapshot_inputs",
                        unit="blobs",
                    ),
                )
            except subprocess.TimeoutExpired as exc:
                raise StagedRebuildError(
                    "Committed input snapshot exceeded MCP_INDEX_GIT_TIMEOUT_SECONDS"
                ) from exc
            if active_path.exists() and readiness.state != RepositoryReadinessState.CORRUPT_SQLITE:
                self._snapshot_active_generation(
                    active_path, generation_path, repo_info if force_full else None
                )
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
            if replace_derived or (changes is None and stage_operation is None):
                contract_getter = getattr(self.dispatcher, "get_semantic_summary_contract", None)
                contract = contract_getter(stage_ctx) if callable(contract_getter) else None
                stage_store.prepare_generation(
                    hashes,
                    profile,
                    (
                        contract.get("prompt_fingerprint")
                        if contract and contract.get("profile_id") == profile
                        else None
                    ),
                )
            else:
                with stage_store._get_connection() as connection:
                    connection.execute("DELETE FROM query_cache")
                    connection.execute("DELETE FROM parse_cache")
            self._rebuild_checkpoint("stage_created")
            if getattr(self.dispatcher, "_semantic_enabled", False) is True:
                if not callable(getattr(self.dispatcher, "_semantic_lease", None)):
                    raise StagedRebuildError("Staged semantic integration unavailable")
                self._copy_retained_vectors(repo_id, stage_ctx)
            # Per-language degradation is opt-in for staged full and incremental
            # builds only; gateway and subdirectory stage operations stay fail-
            # closed. Availability is decided once, here (#117).
            grammar_availability = self._grammar_availability() if stage_operation is None else None
            # The shared admission predicate decides which committed paths
            # are stored, exactly as the full walk does: a path it rejects
            # (no plugin for the language, disabled language) is ignored,
            # and an indexed path that is no longer admitted is deleted
            # (Consiliency/Code-Index-MCP#111).
            is_excluded = build_walker_filter(source_root)

            def stored(path: str) -> bool:
                return path in hashes and (
                    self._stage_admission_reason(repo_id, source_root, path, is_excluded) is None
                )

            def deferred(path: str) -> bool:
                return grammar_availability is not None and grammar_availability.defers(
                    get_language_by_extension(Path(path).suffix.lower())
                )

            if stage_operation is not None:
                result = stage_operation(stage_ctx)
                if verify_stage is not None:
                    verify_stage(hashes, stored)
                    # A verified complete restore replaces every derived row,
                    # so the live generation's marker must not carry over.
                    with stage_store._get_connection() as connection:
                        stage_store.set_grammar_degradation(connection, None)
            elif changes is None:
                full_call_started = True
                # The snapshot reaches the walk through the scope, also when it
                # is complete, so a grammar lost mid-build defers its language
                # (#117). With nothing lost the walk indexes exactly as before.
                if force_full:
                    callback = self._make_force_full_progress_callback(
                        repo_info=repo_info,
                        current_commit=current_commit,
                        indexed_commit_before=indexed_before,
                    )
                    with self._staged_grammar_scope(grammar_availability):
                        try:
                            value = self._full_index(repo_id, stage_ctx, progress_callback=callback)
                        except TypeError as exc:
                            if "progress_callback" not in str(exc):
                                raise
                            value = self._full_index(repo_id, stage_ctx)
                    result = self._normalize_update_result(value)
                else:
                    with self._staged_grammar_scope(grammar_availability):
                        value = self._full_index(repo_id, stage_ctx)
                    result = self._normalize_update_result(value)
                full_call_completed = True
            else:
                # Project Git changes onto the admitted snapshot and previously indexed rows.
                with stage_store._get_connection() as connection:
                    indexed = {
                        row[0] for row in connection.execute("SELECT relative_path FROM files")
                    }
                admitted = ChangeSet(added=[], modified=[], deleted=[], renamed=[])
                not_admitted = 0
                admitted.deleted = [path for path in changes.deleted if path in indexed]
                for path in changes.added + changes.modified:
                    if stored(path) and deferred(path):
                        # Its language is left out whole; any row of it is
                        # dropped before publish, as a full walk would (#117).
                        if path in indexed:
                            admitted.deleted.append(path)
                    elif stored(path):
                        (admitted.modified if path in indexed else admitted.added).append(path)
                    elif path in indexed:
                        admitted.deleted.append(path)
                    elif path in hashes:
                        not_admitted += 1
                for old_path, new_path in changes.renamed:
                    new_stored = stored(new_path)
                    if new_stored and deferred(new_path):
                        if old_path in indexed:
                            admitted.deleted.append(old_path)
                    elif old_path in indexed and new_stored:
                        admitted.renamed.append((old_path, new_path))
                    elif old_path in indexed:
                        # Renamed to a path no plugin stores: drop the old row.
                        admitted.deleted.append(old_path)
                        not_admitted += int(new_path in hashes)
                    elif new_stored:
                        admitted.added.append(new_path)
                    elif new_path in hashes:
                        not_admitted += 1
                # Languages the previous generation left out whose grammars
                # load now: index every committed file of them (#117 repair).
                repaired = self._repairable_languages(stage_store, grammar_availability)
                if repaired:
                    queued = set(admitted.added) | set(admitted.modified)
                    queued.update(new_path for _old, new_path in admitted.renamed)
                    for path in sorted(hashes):
                        if (
                            path not in queued
                            and get_language_by_extension(Path(path).suffix.lower()) in repaired
                            and stored(path)
                        ):
                            (admitted.modified if path in indexed else admitted.added).append(path)
                with self._staged_grammar_scope(grammar_availability):
                    result = self._incremental_index_update(repo_id, stage_ctx, admitted)
                result.ignored += not_admitted
            if not result.clean:
                if self._shutdown.is_set():
                    # The walk stopped because the service is shutting down:
                    # record it as abandoned, not as a real index failure
                    # (#116 re-check NIT 2).
                    raise RebuildShutdownRequested(
                        "service shutdown requested during the index walk"
                    )
                raise StagedRebuildError(
                    "Staged full index did not complete cleanly"
                    if changes is None
                    else "Staged index update did not complete cleanly"
                )
            degradation = None
            if grammar_availability is not None:
                degradation = self._apply_grammar_degradation(
                    stage_ctx,
                    stage_store,
                    hashes,
                    stored,
                    grammar_availability,
                    result,
                    incremental=changes is not None,
                )
            if degradation and not self._has_indexable_source(
                hashes, stored, set(degradation.get("languages") or ())
            ):
                # Decided from the admitted committed inputs, not from leftover
                # rows: retained history/document rows must not let a generation
                # with every source file deferred publish (#117 review).
                raise GrammarUnavailableRebuildError(
                    "Every admitted file needs a tree-sitter grammar that is "
                    "unavailable; run `mcp-index grammars prefetch`, then reindex"
                )
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
                if degradation:
                    raise GrammarUnavailableRebuildError(
                        "Every admitted file needs a tree-sitter grammar that is "
                        "unavailable; run `mcp-index grammars prefetch`, then reindex"
                    )
                raise StagedRebuildError("Staged index has no durable rows")

            self._rebuild_checkpoint("before_replacement")
            self._release_runtime_handles(repo_id, repo_info, stage_ctx)
            stage_store = None
            quarantine_path = None
            if readiness.state in _QUARANTINE_REBUILD_STATES:
                quarantine_path = self._quarantine_active_index(active_path, readiness.state)
            self._fsync_generation(generation_path)
            self._rebuild_checkpoint("after_replacement")
            self._rebuild_checkpoint("before_provenance")
            try:
                unchanged = self._git_source_unchanged(
                    repo_path, current_commit, current_branch, strict=True
                )
            except subprocess.TimeoutExpired as exc:
                raise StagedRebuildError(
                    "Git source check exceeded MCP_INDEX_GIT_TIMEOUT_SECONDS"
                ) from exc
            if not unchanged:
                raise StagedRebuildError("Repository changed while the generation was building")
            # Last shutdown check: only the registry publish write itself remains.
            self._rebuild_checkpoint("before_publish")
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
            self._prune_superseded_generations(
                generation_dir,
                live_generation=generation,
                previous_index_path=active_path,
            )
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
                except Exception as exc:
                    logger.warning(
                        "Published generation trace unavailable (%s)", type(exc).__name__
                    )
            published_marker = self._read_generation_degradation(generation_path)
            return IndexSyncResult(
                action="full_index" if changes is None else "incremental_update",
                commit=current_commit,
                files_processed=result.files_processed,
                readiness={
                    "index_generation": generation,
                    "index_path": str(generation_path),
                    "previous_state": readiness.state.value,
                    "quarantine_path": str(quarantine_path) if quarantine_path else None,
                    "coverage": coverage_from_marker(published_marker),
                },
                semantic=result.semantic,
                duration_seconds=(datetime.now() - start_time).total_seconds(),
                degraded_languages=(
                    published_marker.get("languages") if published_marker else None
                ),
            )
        except Exception as exc:
            error = _format_rebuild_error(
                exc,
                result,
                strip_prefixes=[
                    str(prefix)
                    for prefix in (stage_dir / "source" if stage_dir else None, repo_path)
                    if prefix is not None
                ],
            )
            if mutation_admitted:
                self.registry.fail_generation_mutation(
                    repo_id,
                    error=error,
                    expected_registration_id=repo_info.registration_id,
                    expected_generation=repo_info.index_generation,
                )
                if generation_path.exists():
                    # Keep the newest failed generation as evidence; older
                    # recorded failures are removed (#114 review N6).
                    self._prune_superseded_generations(generation_dir, failed_generation=generation)
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
                code=exc.code if isinstance(exc, StagedRebuildError) else None,
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
                    self._release_runtime_handles(repo_id, repo_info, stage_ctx)
                except Exception as exc:
                    logger.warning(
                        "Failed-stage retirement remains pending: %s", type(exc).__name__
                    )
                finally:
                    stage_store.close()
            if stage_dir is not None:
                shutil.rmtree(stage_dir, ignore_errors=True)

    def _grammar_availability(self) -> Optional[grammar_cache.GrammarAvailability]:
        """Snapshot which languages cannot load their grammars, once per build.

        Returns None, which keeps the fail-closed behaviour, when the check
        itself fails (Consiliency/Code-Index-MCP#117).
        """
        try:
            return grammar_cache.snapshot_availability(refresh=True)
        except Exception as exc:  # noqa: BLE001 - no opt-in without a snapshot
            logger.warning("Grammar availability check failed (%s)", type(exc).__name__)
            return None

    def _apply_grammar_degradation(
        self,
        ctx: RepoContext,
        store: SQLiteStore,
        hashes: Dict[str, str],
        stored: Any,
        availability: grammar_cache.GrammarAvailability,
        result: UpdateResult,
        *,
        incremental: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Leave degraded languages out of the stage whole and record them.

        Every stored row of a language whose grammar is unavailable (copied
        forward from the previous generation, or chunkless after
        ``prepare_generation``) is removed through ``dispatcher.remove_file``,
        so FTS rows and vectors go too. The marker lists each language with
        at least one admitted committed file, counted from the committed
        inputs exactly as a full walk would, so full and incremental builds
        record the same coverage (#111, #117). Returns the marker, or None
        when coverage is complete (the marker is then cleared).
        """
        candidates = set(availability.unavailable) | set(result.degraded_languages)
        existing = store.get_grammar_degradation()
        if not candidates and existing is None:
            return None  # complete coverage, nothing to clear
        counts: Dict[str, int] = {}
        if candidates:
            for path in hashes:
                language = get_language_by_extension(Path(path).suffix.lower())
                if language in candidates and stored(path):
                    counts[language] = counts.get(language, 0) + 1
            for language, entry in result.degraded_languages.items():
                if not counts.get(language) and entry.get("files"):
                    counts[language] = int(entry["files"])
            for relative in store.stored_paths_for_languages(candidates):
                if self.dispatcher is None:
                    raise StagedRebuildError("No dispatcher available to drop degraded rows")
                full_path = ctx.workspace_root / relative
                mutation = self._coerce_index_result(
                    self.dispatcher.remove_file(ctx, full_path), path=full_path
                )
                self._merge_semantic_result(result, mutation.semantic)
                if mutation.status not in {IndexResultStatus.DELETED, IndexResultStatus.NOT_FOUND}:
                    raise StagedRebuildError(
                        "Could not drop the stored rows of a language whose grammar is unavailable"
                    )
        languages: Dict[str, Dict[str, Any]] = {}
        for language, count in sorted(counts.items()):
            if count <= 0:
                continue
            grammars = set(availability.missing(language))
            grammars.update(result.degraded_languages.get(language, {}).get("grammars", ()))
            languages[language] = {"grammars": sorted(grammars), "deferred_files": count}
        marker = {"version": 1, "languages": languages} if languages else None
        if incremental and existing and existing.get("unreadable"):
            # Which languages an unreadable marker left out is unknown, so
            # only a full rebuild may replace it.
            return existing
        with store._get_connection() as connection:
            store.set_grammar_degradation(connection, marker)
        if marker:
            logger.warning(
                "Publishing with degraded coverage: %s left out (tree-sitter grammars "
                "unavailable: %s); run `mcp-index grammars prefetch` to index them",
                ", ".join(
                    f"{name} ({entry['deferred_files']} files)" for name, entry in languages.items()
                ),
                ", ".join(sorted({g for entry in languages.values() for g in entry["grammars"]})),
            )
        return marker

    @staticmethod
    def _has_indexable_source(hashes: Dict[str, str], stored: Any, degraded: set) -> bool:
        """Whether any committed file outside ``degraded`` would be stored."""
        for path in hashes:
            if get_language_by_extension(Path(path).suffix.lower()) in degraded:
                continue
            if stored(path):
                return True
        return False

    @staticmethod
    def _repairable_languages(
        store: SQLiteStore, availability: Optional[grammar_cache.GrammarAvailability]
    ) -> frozenset:
        """Languages the stage's marker left out whose grammars now load."""
        if availability is None:
            return frozenset()
        marker = store.get_grammar_degradation()
        if not marker or marker.get("unreadable"):
            return frozenset()
        return frozenset(
            language for language in marker["languages"] if not availability.defers(language)
        )

    def recoverable_degraded_languages(self, repo_info: Any) -> frozenset:
        """Left-out languages of the published generation whose grammars load now.

        Cheap when coverage is complete (a cached marker read); re-reads the
        grammar cache only for a degraded generation, so a prefetch after
        startup is picked up. Empty while a failed sync is pending, so a
        repair that failed is not retried on every poll (#117).
        """
        if repo_info is None or getattr(repo_info, "staleness_reason", None):
            return frozenset()
        index_path = getattr(repo_info, "index_path", None)
        marker = published_grammar_degradation(Path(index_path)) if index_path else None
        if not marker or marker.get("unreadable") or not marker.get("languages"):
            return frozenset()
        languages = list(marker["languages"])
        try:
            missing = grammar_cache.unavailable_languages(languages, refresh=True)
        except Exception as exc:  # noqa: BLE001 - never repair on a failed check
            logger.warning("Grammar availability check failed (%s)", type(exc).__name__)
            return frozenset()
        return frozenset(language for language in languages if language not in missing)

    @staticmethod
    def _read_generation_degradation(path: Path) -> Optional[Dict[str, Any]]:
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                return read_grammar_degradation(conn)
        except (sqlite3.Error, OSError):
            return None

    def _prune_superseded_generations(
        self,
        generation_dir: Path,
        *,
        live_generation: Optional[str] = None,
        previous_index_path: Optional[Path] = None,
        failed_generation: Optional[str] = None,
    ) -> List[str]:
        """Record a publication or failure and bound recorded generations (#114).

        Only generations this code recorded (in ``generation-history.json``) as
        published and since superseded, or as failed, can be removed. Unknown
        generations (pre-upgrade, or anything after a lost history file) are
        never touched. Always kept: the live generation, any generation a
        registry entry references, pinned generations (``<id>.keep``), the
        ``MCP_INDEX_GENERATION_RETENTION`` most recently live previous
        generations, the newest failed generation, and any generation an active
        reader may still hold. Quarantine copies are outside the scan. Pruning
        never fails the caller.
        """
        try:
            return self._prune_superseded_generations_unchecked(
                generation_dir,
                live_generation=live_generation,
                previous_index_path=previous_index_path,
                failed_generation=failed_generation,
            )
        except Exception as exc:
            logger.warning("Generation retention skipped: %s", type(exc).__name__)
            return []

    def _prune_superseded_generations_unchecked(
        self,
        generation_dir: Path,
        *,
        live_generation: Optional[str],
        previous_index_path: Optional[Path],
        failed_generation: Optional[str],
    ) -> List[str]:
        retention = get_index_generation_retention()
        generation_dir = generation_dir.resolve()
        history_path = generation_dir / _GENERATION_HISTORY_FILE
        history = _read_generation_history(history_path)
        now = time.time()

        if live_generation is not None:
            if previous_index_path is not None:
                previous = Path(previous_index_path)
                entry = history.get(previous.stem)
                if (
                    entry is not None
                    and entry["state"] == "published"
                    and previous.parent.resolve(strict=False) == generation_dir
                ):
                    # It was live until this publication superseded it.
                    entry["last_live_at"] = now
            history[live_generation] = {
                "state": "published",
                "recorded_at": now,
                "last_live_at": now,
            }
        if failed_generation is not None:
            history[failed_generation] = {
                "state": "failed",
                "recorded_at": now,
                "last_live_at": None,
            }

        present = {
            path.stem: path
            for path in generation_dir.glob("*.db")
            if path.is_file() and _is_generation_id(path.stem)
        }
        # Recorded generations whose files are gone no longer need an entry.
        history = {key: value for key, value in history.items() if key in present}

        protected = {live_generation} if live_generation else set()
        for repo in self.registry.get_all_repositories().values():
            index_path = getattr(repo, "index_path", None)
            if index_path and Path(index_path).parent.resolve(strict=False) == generation_dir:
                protected.add(Path(index_path).stem)
        protected |= {
            key for key in present if (generation_dir / f"{key}{_GENERATION_PIN_SUFFIX}").exists()
        }

        superseded = sorted(
            (
                key
                for key, entry in history.items()
                if entry["state"] == "published" and key not in protected
            ),
            key=lambda key: history[key]["last_live_at"] or history[key]["recorded_at"],
            reverse=True,
        )
        failed = sorted(
            (key for key, entry in history.items() if entry["state"] == "failed"),
            key=lambda key: history[key]["recorded_at"],
            reverse=True,
        )
        keep = protected | set(superseded[:retention]) | set(failed[:_FAILED_GENERATION_RETENTION])
        candidates = [present[key] for key in sorted(history) if key not in keep]
        held = _generations_held_open(candidates) if candidates else set()

        pruned: List[str] = []
        reclaimed = 0
        for database in candidates:
            if database in held:
                logger.info("Superseded generation still open; kept: %s", database.name)
                continue
            freed = _remove_generation_files(database)
            if freed is None:
                continue
            pruned.append(database.stem)
            history.pop(database.stem, None)
            reclaimed += freed

        _write_generation_history(history_path, history)
        if pruned:
            logger.info(
                "Pruned %d superseded index generation(s), reclaimed %d bytes; kept live "
                "plus up to %d previous generation(s)",
                len(pruned),
                reclaimed,
                retention,
            )
        return pruned

    def _run_git(
        self,
        args: List[str],
        cwd: Path,
        *,
        stdout: Any = None,
        check: bool = True,
    ) -> "subprocess.CompletedProcess[bytes]":
        """Run one git command of a staged rebuild, honoring shutdown and its timeout.

        The timeout is ``MCP_INDEX_GIT_TIMEOUT_SECONDS``. Output is polled every
        :data:`_GIT_POLL_SECONDS`, so a shutdown request kills git and raises
        :class:`RebuildShutdownRequested` promptly; exceeding the timeout kills
        git and raises :class:`subprocess.TimeoutExpired`. Output is bytes.

        Git runs in its own process group, and stopping it kills the whole
        group, so helpers it spawned (transport, promisor fetch) die with it.
        Reaping is bounded by :data:`_GIT_REAP_SECONDS`: a descendant that
        left the group and still holds the pipes is abandoned, not waited for.
        """
        timeout = get_index_git_timeout_seconds()
        command = ["git", *args]
        isolation: Dict[str, Any] = (
            {"start_new_session": True}
            if not _WINDOWS
            else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        )
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE if stdout is None else stdout,
            stderr=subprocess.PIPE,
            **isolation,
        )
        deadline = _monotonic() + timeout
        try:
            while True:
                try:
                    output, errors = process.communicate(timeout=_GIT_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    if self._shutdown.is_set():
                        raise RebuildShutdownRequested(
                            f"service shutdown requested during git {args[0]}"
                        ) from None
                    if _monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(command, timeout) from None
        except BaseException:
            self._stop_git(process, args[0])
            raise
        if check and process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command, output, errors)
        return subprocess.CompletedProcess(command, process.returncode, output, errors)

    @staticmethod
    def _stop_git(process: "subprocess.Popen[bytes]", name: str) -> None:
        """Kill git's process tree and reap it without waiting unboundedly.

        POSIX kills git's whole process group. Windows is best effort only:
        Windows is not a verified platform (docs/SUPPORT_MATRIX.md, "V13
        Verification Boundary"), so instead of a Job Object it tries
        ``taskkill /T /F`` for the tree and then kills git itself; a
        descendant that survives is covered by the bounded reap below.
        """
        if _WINDOWS:
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_GIT_TASKKILL_SECONDS,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                process.kill()
            except OSError:
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            except OSError:
                process.kill()
        try:
            process.communicate(timeout=_GIT_REAP_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        # A descendant outside the group still holds the pipes: close our ends
        # so nothing blocks on them, reap the direct child, and move on.
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        try:
            process.wait(timeout=_GIT_REAP_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        logger.warning(
            "Stopped git %s did not release its output within %gs; abandoned it",
            name,
            _GIT_REAP_SECONDS,
        )

    def _git_source_unchanged(
        self,
        repo_path: Path,
        commit: str,
        branch: Optional[str],
        *,
        strict: bool = False,
    ) -> bool:
        """Whether HEAD, the branch and the tracked worktree still match ``commit``.

        A git failure means "changed". A git timeout or a shutdown request says
        nothing about the repository, so with ``strict`` they propagate as
        :class:`subprocess.TimeoutExpired` / :class:`RebuildShutdownRequested`
        instead of being reported as a change (#112). Without ``strict`` (the
        "is it already up to date?" probe) they are logged and answer "no".
        """
        try:
            head = self._run_git(["rev-parse", "HEAD"], repo_path).stdout.decode().strip()
            ref = (
                self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_path)
                .stdout.decode()
                .strip()
            )
            dirty = self._run_git(
                ["diff", "--quiet", "--no-ext-diff", "HEAD", "--"], repo_path, check=False
            ).returncode
            return head == commit and ref == branch and dirty == 0
        except subprocess.TimeoutExpired:
            if strict:
                raise
            logger.warning(
                "Git source check exceeded MCP_INDEX_GIT_TIMEOUT_SECONDS (%ss)",
                get_index_git_timeout_seconds(),
            )
            return False
        except RebuildShutdownRequested:
            if strict:
                raise
            return False
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
            return False

    def _finalize_staged_vectors(self, repo_id: str, ctx: RepoContext) -> None:
        """Drain only this unpublished owner and verify its live point mappings."""
        from qdrant_client import models

        if not ctx.staging:
            raise RuntimeError("Vector publication requires an unpublished generation")
        assert self.dispatcher is not None
        assert self.dispatcher._semantic_registry is not None
        with self.dispatcher._semantic_registry.lease(repo_id, ctx=ctx) as staged:
            drained = ctx.sqlite_store.drain_pending_vector_deletions(
                lambda collection, ids: staged.delete_remote_points(ids, collection=collection),
                only_collection=staged.collection,
            )
            if drained["groups_failed"]:
                raise StagedRebuildError("Staged vector cleanup did not complete")
            with ctx.sqlite_store._get_connection() as connection:
                records = connection.execute(
                    "SELECT point_id, collection FROM semantic_points WHERE profile_id=?",
                    (staged.semantic_profile.profile_id,),
                ).fetchall()
            if records:
                record = staged._indexed_profile_record()
                if not record or not record.get("attested"):
                    raise StagedRebuildError("Staged vectors lack attested provenance")
            if any(row["collection"] != staged.collection for row in records):
                raise StagedRebuildError("Staged vectors belong to another generation")
            ids = list(dict.fromkeys(row["point_id"] for row in records))
            if not ids and not any(
                collection.name == staged.collection
                for collection in staged.qdrant.get_collections().collections
            ):
                return
            remote_count = staged.qdrant.count(
                collection_name=staged.collection,
                exact=True,
                count_filter=models.Filter(
                    must_not=[models.HasIdCondition(has_id=[staged.PROVENANCE_POINT_ID])]
                ),
            ).count
            if remote_count != len(ids):
                raise StagedRebuildError("Staged vector ownership is incomplete")
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
                    raise StagedRebuildError("Staged vector mappings are incomplete")
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
        assert self.dispatcher is not None
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
                assert staged._attestation is not None
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
                    copied_points = []
                    for point in points:
                        if point.vector is None:
                            raise RuntimeError("Retained vector payload is missing")
                        copied_points.append(
                            models.PointStruct(
                                id=point.id,
                                vector=point.vector,
                                payload=point.payload,
                            )
                        )
                    acknowledged = staged.qdrant.upsert(
                        collection_name=staged.collection,
                        points=copied_points,
                        wait=True,
                    )
                    if acknowledged.status not in {"completed", models.UpdateStatus.COMPLETED}:
                        raise RuntimeError("Retained vector copy was not acknowledged")
                    with ctx.sqlite_store._get_connection() as connection:
                        connection.executemany(
                            "UPDATE semantic_points SET collection=? WHERE profile_id=? AND chunk_id=?",
                            [(staged.collection, profile, row["chunk_id"]) for row in batch],
                        )

    def _snapshot_committed_inputs(
        self,
        repo_path: Path,
        commit: str,
        destination: Path,
        *,
        on_progress: Optional[Any] = None,
    ) -> Dict[str, str]:
        from ..core.ignore_patterns import build_walker_filter, index_path_exclusion_reason
        from ..plugins.generic_treesitter_plugin import GenericTreeSitterPlugin

        self._rebuild_checkpoint("snapshot_inputs")
        listing = self._run_git(["ls-tree", "-r", "-l", "-z", commit], repo_path).stdout
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
            if index_path_exclusion_reason(destination / relative, destination):
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
        copied = 0
        # Admission depends on the committed policies, so no total is reported
        # until they are loaded and the admitted count is exact.
        progress_total: Optional[int] = None

        def copy_blob(entry):
            nonlocal copied
            relative, oid, _policy = entry
            # Shutdown is honored between blobs, not only after the whole copy.
            self._rebuild_checkpoint("snapshot_inputs")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                self._run_git(["cat-file", "blob", oid], repo_path, stdout=output)

            def normalized_hash(encoding):
                digest = hashlib.sha256()
                with target.open("r", encoding=encoding, newline=None) as source:
                    for chunk in iter(lambda: source.read(8192), ""):
                        digest.update(chunk.encode("utf-8"))
                return digest.hexdigest()

            try:
                hashes[relative.as_posix()] = normalized_hash("utf-8")
            except UnicodeDecodeError:
                hashes[relative.as_posix()] = normalized_hash("latin-1")
            copied += 1
            if on_progress is not None and progress_total is not None:
                on_progress(copied, progress_total)

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
        ordinary = [
            entry
            for entry in entries
            if entry[0].as_posix() not in hashes
            and not entry[2]
            and not excluded(destination / entry[0])
        ]
        progress_total = copied + len(ordinary)
        if on_progress is not None:
            on_progress(copied, progress_total)
        for entry in ordinary:
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

    def _make_snapshot_heartbeat(self, trace_repo_info: Any, *, stage: str, unit: str) -> Any:
        """Return a throttled ``(done, total)`` progress callback for a snapshot phase.

        It refreshes the force-full trace (when ``trace_repo_info`` is given)
        at most every :data:`_SNAPSHOT_TRACE_HEARTBEAT_SECONDS`, so status does
        not call a healthy slow copy stale, and logs progress at most every
        :data:`_SNAPSHOT_LOG_HEARTBEAT_SECONDS`.
        """
        state: Dict[str, Any] = {"trace": None, "log": _monotonic()}

        def heartbeat(done: int, total: int) -> None:
            now = _monotonic()
            if trace_repo_info is not None and (
                state["trace"] is None
                or done >= total  # always record completion
                or now - state["trace"] >= _SNAPSHOT_TRACE_HEARTBEAT_SECONDS
            ):
                state["trace"] = now
                timestamp = self._trace_timestamp()
                try:
                    self._write_force_full_exit_trace(
                        trace_repo_info,
                        {
                            "status": "running",
                            "stage": stage,
                            "stage_family": "lexical",
                            f"snapshot_{unit}_done": done,
                            f"snapshot_{unit}_total": total,
                            "last_progress_at": timestamp,
                        },
                    )
                except (OSError, ValueError) as exc:
                    logger.debug("Snapshot heartbeat trace unavailable (%s)", type(exc).__name__)
            if now - state["log"] >= _SNAPSHOT_LOG_HEARTBEAT_SECONDS:
                state["log"] = now
                logger.info("Staged rebuild %s progress: %d/%d %s", stage, done, total, unit)

        return heartbeat

    def _snapshot_active_generation(
        self, active_path: Path, generation_path: Path, trace_repo_info: Any
    ) -> None:
        """Copy the active database into the new generation (stall-bounded, #112)."""
        try:
            SQLiteStore.snapshot_database(
                active_path,
                generation_path,
                checkpoint=lambda: self._rebuild_checkpoint("snapshot_generation"),
                on_progress=self._make_snapshot_heartbeat(
                    trace_repo_info, stage="snapshot_generation", unit="pages"
                ),
                clock=_monotonic,
            )
        except SnapshotStalledError as exc:
            logger.error(
                "Staged rebuild stage snapshot_generation stalled at %d/%d pages "
                "(no progress for %gs; see MCP_INDEX_SNAPSHOT_STALL_SECONDS)",
                exc.pages_done,
                exc.pages_total,
                exc.stall_seconds,
            )
            raise

    def _rebuild_checkpoint(self, stage: str) -> None:
        """Failure-injection seam used by publication boundary tests.

        Every checkpoint precedes ``publish_generation``, so it is also where a
        rebuild honors :meth:`request_shutdown`.
        """
        self._hold_for_test(stage)
        if self._shutdown.is_set():
            raise RebuildShutdownRequested(f"service shutdown requested before {stage}")

    def _hold_for_test(self, stage: str) -> None:
        if os.environ.get(_REBUILD_HOLD_STAGE_ENV) != stage:
            return
        hold = os.environ.get(_REBUILD_HOLD_FILE_ENV)
        if not hold or not os.path.exists(hold):
            return
        Path(f"{hold}.held").write_text(f"{os.getpid()} {stage}\n", encoding="utf-8")
        while os.path.exists(hold) and not self._shutdown.wait(0.01):
            pass

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
        self,
        repo_id: str,
        ctx: RepoContext,
        changes: ChangeSet,
        grammar_availability: Optional[grammar_cache.GrammarAvailability] = None,
    ) -> UpdateResult:
        """Update index incrementally based on file changes.

        Args:
            repo_id: Repository ID
            changes: Set of file changes
            grammar_availability: Opts in to per-language degradation (#117):
                a file that fails with ``grammar_unavailable`` is deferred
                instead of failed; the caller drops its whole language.
                None falls back to the staged build's scoped snapshot.

        Returns:
            UpdateResult
        """
        start_time = datetime.now()
        result = UpdateResult()
        grammar_availability = self._scoped_grammar_availability(grammar_availability)

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
                        if grammar_availability is not None and is_grammar_unavailable_result(
                            mutation
                        ):
                            # The grammar disappeared after the build started.
                            language = mutation.language or get_language_by_extension(
                                full_path.suffix.lower()
                            )
                            result.defer(
                                language,
                                grammar_cache.unavailable_languages(
                                    [language] if language else []
                                ).get(language, ()),
                            )
                            continue
                        if is_unsupported_file_result(mutation):
                            # No plugin stores this file; the full walk ignores
                            # it, so incremental sync must too (#111).
                            self._merge_semantic_result(result, mutation.semantic)
                            result.ignored += 1
                            if path in changes.modified:
                                # A full rebuild would drop the stale row.
                                self._record_required_mutation_result(
                                    result,
                                    self._coerce_index_result(
                                        self.dispatcher.remove_file(ctx, full_path),
                                        path=full_path,
                                    ),
                                    success_status=IndexResultStatus.DELETED,
                                    success_counter="deleted",
                                    action_label=f"Failed to remove unindexable {path}",
                                )
                            continue
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

    @staticmethod
    def _accepts_keyword(func: Any, name: str) -> bool:
        try:
            parameters = inspect.signature(func).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.name == name or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

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
        grammar_availability: Optional[grammar_cache.GrammarAvailability] = None,
    ) -> UpdateResult:
        """Perform full repository indexing.

        ``grammar_availability`` (or, when None, the staged build's scoped
        snapshot) opts the walk in to per-language degradation (#117) when the
        dispatcher supports it.

        Args:
            repo_id: Repository ID

        Returns:
            UpdateResult with durability/failure details
        """
        start_time = datetime.now()
        result = UpdateResult()
        grammar_availability = self._scoped_grammar_availability(grammar_availability)
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

        # Index the directory. A shutdown request stops the walk between files.
        logger.info(f"Starting full index of {repo_info.name}")
        cancel_kwargs: Dict[str, Any] = {}
        if self._accepts_keyword(self.dispatcher.index_directory, "cancel_check"):
            cancel_kwargs["cancel_check"] = self._shutdown.is_set
        if grammar_availability is not None and self._accepts_keyword(
            self.dispatcher.index_directory, "grammar_availability"
        ):
            cancel_kwargs["grammar_availability"] = grammar_availability
        try:
            if progress_callback is None:
                stats = self.dispatcher.index_directory(
                    ctx, repo_path, recursive=True, **cancel_kwargs
                )
            else:
                try:
                    stats = self.dispatcher.index_directory(
                        ctx,
                        repo_path,
                        recursive=True,
                        progress_callback=progress_callback,
                        **cancel_kwargs,
                    )
                except TypeError as exc:
                    if "progress_callback" not in str(exc):
                        raise
                    stats = self.dispatcher.index_directory(
                        ctx, repo_path, recursive=True, **cancel_kwargs
                    )
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
        if isinstance(stats, dict) and isinstance(stats.get("degraded_languages"), dict):
            for language, entry in stats["degraded_languages"].items():
                if isinstance(entry, dict):
                    result.defer(
                        language, entry.get("grammars") or (), int(entry.get("files") or 0)
                    )
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

        if repo_info.index_path and self._read_generation_degradation(Path(repo_info.index_path)):
            # A generation that left languages out is never shared (#117).
            logger.warning("Not creating a commit artifact: index coverage is degraded")
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
            self.store_registry.close(repo_id, expected_owner=repo_info)
        sqlite_store = getattr(ctx, "sqlite_store", None) if ctx is not None else None
        if sqlite_store is not None:
            sqlite_store.close()
        if self.dispatcher is not None and hasattr(self.dispatcher, "evict_repository_state"):
            self.dispatcher.evict_repository_state(
                repo_id, repo_info.path, expected_owner=repo_info
            )
            if ctx is not None and ctx.staging:
                self.dispatcher.evict_repository_state(
                    repo_id, repo_info.path, expected_owner=ctx.registry_entry
                )
                # Plugin workers built per-file state against the disposable
                # staging copy; drop them while queries are still fenced so that
                # state cannot accumulate across rebuilds (#114). Worker eviction
                # is repository-wide, so a stale stage whose registration or
                # generation was replaced must leave the replacement's workers.
                release_workers = getattr(self.dispatcher, "release_staged_plugin_workers", None)
                if callable(release_workers) and self._stage_owner_is_current(repo_id, repo_info):
                    release_workers(repo_id)

    def _stage_admission_reason(
        self, repo_id: str, root: Path, relative: str, is_excluded
    ) -> Optional[str]:
        """Ask the dispatcher's shared admission predicate about a staged path."""
        admission = getattr(self.dispatcher, "index_admission_reason", None)
        if not callable(admission):
            return None
        reason = admission(repo_id, root / relative, root, is_excluded)
        return reason if isinstance(reason, str) else None

    def _stage_owner_is_current(self, repo_id: str, owner: Any) -> bool:
        """Return True while the registry still names the stage's admitted owner."""
        try:
            current = self.registry.get_repository(repo_id)
        except Exception as exc:
            logger.debug("Stage owner lookup failed: %s", type(exc).__name__)
            return False
        return (
            current is not None
            and getattr(current, "registration_id", None) == getattr(owner, "registration_id", None)
            and getattr(current, "index_generation", None)
            == getattr(owner, "index_generation", None)
        )

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

    def _start_force_full_exit_trace(self, repo_info: Any, update: Dict[str, Any]) -> None:
        """Replace the trace with a fresh one for a new force-full run.

        Fields an earlier run left (``lexical_*``, ``semantic_*``,
        ``summary_*``) must not describe this run (#112).
        """
        self._store_force_full_exit_trace(repo_info, dict(update))

    def _write_force_full_exit_trace(self, repo_info: Any, update: Dict[str, Any]) -> None:
        """Merge ``update`` into the current run's trace."""
        payload = self._read_force_full_exit_trace(repo_info) or {}
        payload.update(update)
        self._store_force_full_exit_trace(repo_info, payload)

    def _store_force_full_exit_trace(self, repo_info: Any, payload: Dict[str, Any]) -> None:
        path = self._force_full_exit_trace_path(repo_info)
        path.parent.mkdir(parents=True, exist_ok=True)
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
        results: Dict[str, IndexSyncResult] = {}

        # Get repositories needing update
        stale_repos = self.registry.get_repositories_needing_update()

        if not stale_repos:
            logger.info("All repositories are up to date")
            return results

        logger.info(f"Found {len(stale_repos)} repositories needing update")

        for repo_id, repo_info in stale_repos:
            if repo_info.auto_sync and repo_info.active:
                logger.info(f"Syncing {repo_info.name}...")
                results[repo_id] = self.sync_repository_index(
                    repo_id,
                    expected_registration_id=repo_info.registration_id,
                    require_auto_sync=True,
                )
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
        index_path = Path(repo_info.index_path or Path(repo_info.index_location) / "current.db")
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
