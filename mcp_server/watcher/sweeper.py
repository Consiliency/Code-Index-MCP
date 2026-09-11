"""Periodic full-tree sweeper that recovers inotify/FSEvents drop events (IF-0-P14-5)."""

import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..core.ignore_patterns import build_walker_filter
from ..metrics.prometheus_exporter import mcp_watcher_sweep_errors_total
from ..storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

# Supported code-file extensions (mirrors _Handler.code_extensions)
_CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".java",
    ".c",
    ".cpp",
    ".cc",
    ".cxx",
    ".h",
    ".hpp",
    ".cs",
    ".go",
    ".rb",
    ".rs",
    ".swift",
    ".kt",
    ".scala",
    ".php",
    ".r",
    ".m",
    ".mm",
    ".dart",
    ".lua",
    ".pl",
    ".sh",
    ".sql",
    ".html",
    ".css",
    ".scss",
    ".vue",
    ".elm",
    ".ex",
    ".exs",
    ".erl",
    ".clj",
    ".cljs",
    ".hs",
    ".ml",
    ".mli",
    ".f90",
    ".f95",
}

ENV_SWEEP_MINUTES: str = "MCP_WATCHER_SWEEP_MINUTES"
DEFAULT_SWEEP_MINUTES: int = 60


class WatcherSweeper:
    """Periodically diffs filesystem vs SQLite to recover missed watchdog events.

    Calls on_missed_path(repo_id, relative_path) for each code file present on disk
    but absent from the SQLite index. The caller is responsible for re-indexing the file.

    Thread model: single daemon thread using Event.wait() for clean stop.
    """

    def __init__(
        self,
        on_missed_path: Optional[Callable[[str, str], None]],
        repo_roots_provider: Callable[[], Dict[str, Path]],
        store: Optional[SQLiteStore],
        on_missed_create: Optional[Callable[[str, str], None]] = None,
        on_missed_delete: Optional[Callable[[str, str], None]] = None,
        on_missed_rename: Optional[Callable[[str, str, str], None]] = None,
        store_provider: Optional[Callable[[str], SQLiteStore]] = None,
        interval_minutes: int = DEFAULT_SWEEP_MINUTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_missed_create = on_missed_create or on_missed_path or (lambda _r, _p: None)
        self._on_missed_delete = on_missed_delete or (lambda _r, _p: None)
        self._on_missed_rename = on_missed_rename or (lambda _r, _o, _n: None)
        self._repo_roots_provider = repo_roots_provider
        self._store = store
        self._store_provider = store_provider
        self._clock = clock

        # Env var overrides constructor arg when set
        env_val = os.environ.get(ENV_SWEEP_MINUTES)
        if env_val is not None:
            try:
                interval_minutes = int(env_val)
            except ValueError:
                pass

        self.interval_minutes = interval_minutes
        self._stop_event = threading.Event()
        self._thread: threading.Thread = None  # type: ignore[assignment]

    def start(self) -> None:
        """Start the background sweep thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="watcher-sweeper")
        self._thread.start()

    def stop(self) -> None:
        """Stop and drain the sweep owner before its storage can be released."""
        self._stop_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()

    def _loop(self) -> None:
        """Daemon loop: wait for interval or stop, then sweep."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self.interval_minutes * 60)
            if self._stop_event.is_set():
                break
            try:
                self.sweep_once()
            except Exception as e:
                logger.warning("watcher sweep error: %s", e)
                mcp_watcher_sweep_errors_total.inc()

    def sweep_once(self) -> List[str]:
        """Diff filesystem vs SQLite; call on_missed_path for each absent file.

        Returns list of repo_ids that had drift.
        """
        drifted: List[str] = []
        repos = self._repo_roots_provider()

        for repo_id, repo_root in repos.items():
            repo_root = Path(repo_root)
            if not repo_root.exists():
                continue
            store = self._store_provider(repo_id) if self._store_provider else self._store
            if store is None:
                continue

            # Resolve SQLite integer repo id
            repo_record = store.get_repository(str(repo_root))
            if repo_record is None:
                continue
            sqlite_repo_id: int = repo_record["id"]

            known_files = store.get_all_files(repository_id=sqlite_repo_id)
            known_by_path = {f["relative_path"]: f for f in known_files if f.get("relative_path")}

            gitignore_filter = build_walker_filter(repo_root)
            repo_has_drift = False

            fs_by_path: Dict[str, str] = {}
            for fs_path in repo_root.rglob("*"):
                if not fs_path.is_file():
                    continue
                if fs_path.suffix not in _CODE_EXTENSIONS:
                    continue
                if gitignore_filter(fs_path):
                    continue

                try:
                    rel = fs_path.relative_to(repo_root)
                except ValueError:
                    continue

                rel_str = str(rel).replace("\\", "/")
                fs_by_path[rel_str] = self._hash_file(fs_path)

            created = set(fs_by_path) - set(known_by_path)
            deleted = {
                rel
                for rel in set(known_by_path) - set(fs_by_path)
                if self._indexed_path_should_report_delete(repo_root, rel, gitignore_filter)
            }
            renamed = self._match_renames(created, deleted, fs_by_path, known_by_path)
            renamed_created = {new for _old, new in renamed}
            renamed_deleted = {old for old, _new in renamed}

            for old_rel, new_rel in sorted(renamed):
                self._on_missed_rename(repo_id, old_rel, new_rel)
                repo_has_drift = True

            for rel_str in sorted(created - renamed_created):
                self._on_missed_create(repo_id, rel_str)
                repo_has_drift = True

            for rel_str in sorted(deleted - renamed_deleted):
                self._on_missed_delete(repo_id, rel_str)
                repo_has_drift = True

            if repo_has_drift:
                drifted.append(repo_id)

        return drifted

    def _hash_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _indexed_path_should_report_delete(
        self, repo_root: Path, rel: str, gitignore_filter
    ) -> bool:
        path = repo_root / rel
        if path.suffix not in _CODE_EXTENSIONS:
            return False
        return not gitignore_filter(path)

    def _match_renames(
        self,
        created: set[str],
        deleted: set[str],
        fs_by_path: Dict[str, str],
        known_by_path: Dict[str, Dict],
    ) -> List[tuple[str, str]]:
        created_by_hash: Dict[str, List[str]] = {}
        deleted_by_hash: Dict[str, List[str]] = {}

        for rel in created:
            content_hash = fs_by_path.get(rel)
            if content_hash:
                created_by_hash.setdefault(content_hash, []).append(rel)
        for rel in deleted:
            content_hash = known_by_path[rel].get("content_hash") or known_by_path[rel].get("hash")
            if content_hash:
                deleted_by_hash.setdefault(content_hash, []).append(rel)

        renames: List[tuple[str, str]] = []
        for content_hash, old_paths in deleted_by_hash.items():
            new_paths = created_by_hash.get(content_hash, [])
            if len(old_paths) == 1 and len(new_paths) == 1:
                renames.append((old_paths[0], new_paths[0]))
        return renames
