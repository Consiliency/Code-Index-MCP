import asyncio
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from ..core.path_resolver import PathResolver
from ..core.repo_context import RepoContext
from ..dispatcher.dispatcher_enhanced import EnhancedDispatcher
from ..plugins.language_registry import get_all_extensions

if TYPE_CHECKING:
    from ..storage.git_index_manager import GitAwareIndexManager

logger = logging.getLogger(__name__)

# Directory names whose contents we never want to reindex. Matching is done
# against the path parts, so a nested ".git/objects/…" is skipped just as
# reliably as a top-level ".git/…".
_EXCLUDED_DIR_PARTS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mcp-index",
        ".indexes",
        ".tox",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "target",  # rust
        ".gradle",
        ".idea",
        ".vscode",
        "coverage",
        "htmlcov",
    }
)


def _is_excluded(path: Path) -> bool:
    """Return True when `path` lies under a directory we should ignore."""
    try:
        parts = path.parts
    except Exception:
        return True
    return any(part in _EXCLUDED_DIR_PARTS for part in parts)


def _swallow_task_exception(task: asyncio.Task) -> None:
    """Surface asyncio.create_task failures instead of silently dropping them."""
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    except Exception:
        return
    if exc is not None:
        logger.error("Background cache-invalidation task failed (%s)", type(exc).__name__)


class _Handler(FileSystemEventHandler):
    """Watchdog handler with path-level debouncing.

    Every file event is coalesced into a per-path "pending action" that fires
    after a quiet window. This prevents rebase/checkout/merge storms from
    generating thousands of dispatcher calls.
    """

    DEBOUNCE_SECONDS = 0.5
    DRAIN_TICK_SECONDS = 0.1

    def __init__(
        self,
        dispatcher: EnhancedDispatcher,
        query_cache=None,
        path_resolver: Optional[PathResolver] = None,
        ctx: Optional[RepoContext] = None,
        index_manager: Optional["GitAwareIndexManager"] = None,
    ):
        self.dispatcher = dispatcher
        self.index_manager = index_manager
        self.query_cache = query_cache
        self.path_resolver = path_resolver or PathResolver()
        workspace_root = getattr(self.path_resolver, "repository_root", None) or Path.cwd()
        self.ctx = ctx or RepoContext(
            repo_id=str(Path(workspace_root).resolve()),
            sqlite_store=getattr(dispatcher, "sqlite_store", None),
            workspace_root=Path(workspace_root),
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                repository_id=str(Path(workspace_root).resolve()),
                path=Path(workspace_root),
                tracked_branch="main",
                name=Path(workspace_root).name,
            ),
        )
        self.code_extensions = get_all_extensions()

        # Pending per-path action: path -> (due_monotonic, action, extra)
        # actions: "reindex", "remove", "move" (extra = destination Path)
        self._pending: dict[Path, tuple[float, str, Optional[Path]]] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._drain_loop, daemon=True, name="mcp-watcher-debounce"
        )
        self._worker.start()

    # ------------------------------------------------------------------
    # Debounce queue

    def _enqueue(self, path: Path, action: str, extra: Optional[Path] = None) -> None:
        if _is_excluded(path) or (extra is not None and _is_excluded(extra)):
            return
        with self._lock:
            self._pending[path] = (
                time.monotonic() + self.DEBOUNCE_SECONDS,
                action,
                extra,
            )

    def _drain_loop(self) -> None:
        while not self._stop_event.is_set():
            now = time.monotonic()
            ready: list[tuple[Path, str, Optional[Path]]] = []
            with self._lock:
                # Iterate over a copy so we can delete as we go.
                for path, (due_at, action, extra) in list(self._pending.items()):
                    if due_at <= now:
                        ready.append((path, action, extra))
                        del self._pending[path]

            for path, action, extra in ready:
                try:
                    if action == "reindex":
                        self._trigger_reindex(path)
                    elif action == "remove":
                        self._remove_file_from_index(path)
                    elif action == "move" and extra is not None:
                        self._handle_file_move(path, extra)
                except Exception as exc:
                    logger.error(
                        "watcher drain failed for %s (action=%s, error=%s)",
                        path,
                        action,
                        type(exc).__name__,
                    )
            self._stop_event.wait(self.DRAIN_TICK_SECONDS)

    def stop(self) -> None:
        """Stop the debounce worker. Idempotent."""
        self._stop_event.set()
        self._worker.join()

    def flush(self) -> None:
        """Force-drain all pending events immediately. For use in tests only."""
        with self._lock:
            pending = dict(self._pending)
            self._pending.clear()
        for path, (_, action, extra) in pending.items():
            try:
                if action == "reindex":
                    self._trigger_reindex(path)
                elif action == "remove":
                    self._remove_file_from_index(path)
                elif action == "move" and extra is not None:
                    self._handle_file_move(path, extra)
            except Exception as exc:
                logger.error(
                    "flush drain failed for %s (action=%s, error=%s)",
                    path,
                    action,
                    type(exc).__name__,
                )

    # ------------------------------------------------------------------
    # Dispatcher-side actions (run on the worker thread)

    async def _invalidate_cache_for_file(self, path: Path) -> None:
        if not self.query_cache:
            return
        try:
            count = await self.query_cache.invalidate_file_queries(str(path))
            if count > 0:
                logger.debug("Invalidated %d cache entries for %s", count, path)
        except Exception as exc:
            logger.error("Cache invalidation failed for %s (%s)", path, type(exc).__name__)

    def _kick_cache_invalidation(self, path: Path) -> None:
        if not self.query_cache:
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        try:
            if loop is not None and loop.is_running():
                task = asyncio.ensure_future(self._invalidate_cache_for_file(path), loop=loop)
                task.add_done_callback(_swallow_task_exception)
            else:
                asyncio.run(self._invalidate_cache_for_file(path))
        except Exception as exc:
            logger.error(
                "Failed to schedule cache invalidation for %s (%s)", path, type(exc).__name__
            )

    def _reconcile_if_managed(self, path: Path) -> bool:
        if not isinstance(self.dispatcher, EnhancedDispatcher):
            return False
        if self.index_manager is None:
            logger.warning("Watcher mutation unavailable without a generation owner")
            return True
        ctx = self.index_manager._resolve_ctx(self.ctx.repo_id)
        if ctx is not None:
            self.ctx = ctx
            result = self.index_manager.sync_repository_index(ctx.repo_id)
            if result.action in {"full_index", "incremental_update"}:
                self._kick_cache_invalidation(path)
        return True

    def _trigger_reindex(self, path: Path) -> None:
        if self._reconcile_if_managed(path):
            return
        if path.suffix not in self.code_extensions:
            return
        if not path.exists():
            return
        logger.info("Re-indexing %s", path)
        if self.ctx is None:
            return
        guarded_index = getattr(type(self.dispatcher), "index_file_guarded", None)
        if guarded_index is not None:
            observed_hash = self.path_resolver.compute_content_hash(path)
            self.dispatcher.remove_file(self.ctx, path)
            self.dispatcher.index_file_guarded(self.ctx, path, observed_hash)
        else:
            self.dispatcher.remove_file(path)
            self.dispatcher.index_file(path)
        self._kick_cache_invalidation(path)

    def trigger_reindex(self, path: Path) -> None:
        """Public test/integration entrypoint — skips the existence check."""
        if self._reconcile_if_managed(path):
            return
        if path.suffix not in self.code_extensions:
            return
        logger.info("Re-indexing %s", path)
        if self.ctx is None:
            return
        try:
            guarded_index = getattr(type(self.dispatcher), "index_file_guarded", None)
            if guarded_index is not None:
                observed_hash = self.path_resolver.compute_content_hash(path)
                self.dispatcher.remove_file(self.ctx, path)
                self.dispatcher.index_file_guarded(self.ctx, path, observed_hash)
            else:
                self.dispatcher.remove_file(path)
                self.dispatcher.index_file(path)
            self._kick_cache_invalidation(path)
        except Exception as exc:
            logger.error("trigger_reindex failed for %s (%s)", path, type(exc).__name__)

    def _remove_file_from_index(self, path: Path) -> None:
        if self._reconcile_if_managed(path):
            return
        if path.suffix not in self.code_extensions:
            return
        logger.info("Removing from index: %s", path)
        if self.ctx is None:
            return
        if getattr(type(self.dispatcher), "index_file_guarded", None) is not None:
            self.dispatcher.remove_file(self.ctx, path)
        else:
            self.dispatcher.remove_file(path)
        self._kick_cache_invalidation(path)

    def remove_file_from_index(self, path: Path) -> None:
        self._remove_file_from_index(path)

    def _handle_file_move(self, old_path: Path, new_path: Path) -> None:
        if self._reconcile_if_managed(new_path):
            return
        if (
            old_path.suffix not in self.code_extensions
            or new_path.suffix not in self.code_extensions
        ):
            return
        if not new_path.exists():
            self._remove_file_from_index(old_path)
            return
        content_hash = self.path_resolver.compute_content_hash(new_path)
        logger.info("Moving in index: %s -> %s", old_path, new_path)
        if self.ctx is None:
            return
        if getattr(type(self.dispatcher), "index_file_guarded", None) is not None:
            self.dispatcher.move_file(self.ctx, old_path, new_path, content_hash)
        else:
            self.dispatcher.move_file(old_path, new_path, content_hash)
        self._kick_cache_invalidation(new_path)

    # ------------------------------------------------------------------
    # Watchdog event entry point

    def on_any_event(self, event) -> None:
        if event.is_directory:
            return

        src = Path(event.src_path)
        if _is_excluded(src):
            return

        etype = event.event_type
        if etype in ("created", "modified"):
            if src.suffix in self.code_extensions:
                self._enqueue(src, "reindex")
        elif etype == "moved":
            dest = Path(event.dest_path)
            if src.suffix in self.code_extensions or dest.suffix in self.code_extensions:
                self._enqueue(src, "move", extra=dest)
        elif etype == "deleted":
            if src.suffix in self.code_extensions:
                self._enqueue(src, "remove")


class FileWatcher:
    def __init__(
        self,
        root: Path,
        dispatcher: EnhancedDispatcher,
        query_cache=None,
        path_resolver: Optional[PathResolver] = None,
        ctx: Optional[RepoContext] = None,
        index_manager: Optional["GitAwareIndexManager"] = None,
    ):
        self._observer = Observer()
        handler_ctx = ctx or RepoContext(
            repo_id=str(root.resolve()),
            sqlite_store=getattr(dispatcher, "sqlite_store", None),
            workspace_root=root,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                repository_id=str(root.resolve()),
                path=root,
                tracked_branch="main",
                name=root.name,
            ),
        )
        self._handler = _Handler(
            dispatcher, query_cache, path_resolver, ctx=handler_ctx, index_manager=index_manager
        )
        self._observer.schedule(self._handler, str(root), recursive=True)

    def start(self) -> None:
        self._observer.start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()
        self._handler.stop()
