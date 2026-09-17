"""Multi-repository file watcher with git synchronization.

This module extends the basic file watcher to support multiple repositories
and synchronize with git commits.
"""

import hashlib
import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .artifacts.commit_artifacts import CommitArtifactManager
from .core.ignore_patterns import build_walker_filter
from .core.repo_context import RepoContext
from .core.repo_resolver import RepoResolver, run_repository_mutation
from .dispatcher.dispatcher_enhanced import EnhancedDispatcher, IndexResult, IndexResultStatus
from .indexing.lock_registry import lock_registry
from .plugins.language_registry import get_all_extensions
from .storage.git_index_manager import GitAwareIndexManager, should_reindex_for_branch
from .storage.repository_registry import RepositoryRegistry
from .utils.subprocess_env import get_full_env
from .watcher.sweeper import WatcherSweeper

logger = logging.getLogger(__name__)


class GitMonitor:
    """Monitors git state changes in repositories."""

    def __init__(self, registry: RepositoryRegistry, callback, registry_callback=None):
        self.registry = registry
        self.callback = callback
        self.registry_callback = registry_callback
        self.running = False
        self.monitor_thread = None
        self.check_interval = 30  # seconds
        self.last_commits = {}  # repo_id -> commit

    def start(self):
        """Start monitoring git repositories."""
        if self.running:
            return

        self.running = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        logger.info("Git monitor started")

    def stop(self):
        """Stop monitoring."""
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join()
        logger.info("Git monitor stopped")

    def _monitor_loop(self):
        """Main monitoring loop."""
        while self.running:
            try:
                self._check_repositories()
            except Exception as e:
                logger.error(f"Error in git monitor: {type(e).__name__}")

            # Sleep with interruption support
            for _ in range(self.check_interval):
                if not self.running:
                    break
                threading.Event().wait(1)

    def _check_repositories(self):
        """Check all repositories for git state changes."""
        repositories = self.registry.get_all_repositories()
        if self.registry_callback is not None:
            self.registry_callback(repositories)
        self.last_commits = {
            key: value for key, value in self.last_commits.items() if key in repositories
        }
        for repo_id, repo_info in repositories.items():
            if not repo_info.auto_sync or not getattr(repo_info, "active", True):
                continue

            try:
                current_commit = self._get_current_commit(repo_info.path)
                if not current_commit:
                    continue

                last_commit = self.last_commits.get(repo_id)

                if last_commit and current_commit != last_commit:
                    # Commit changed
                    logger.info(f"New commit detected in {repo_info.name}: {current_commit[:8]}")
                    self.callback(repo_id, current_commit)

                self.last_commits[repo_id] = current_commit

            except Exception as e:
                logger.error(f"Error checking repository {repo_id}: {type(e).__name__}")

    def _get_current_commit(self, repo_path: str) -> Optional[str]:
        """Get current git commit for a repository."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
                env=get_full_env(),
            )
            return result.stdout.strip()
        except Exception:
            return None


class MultiRepositoryHandler(FileSystemEventHandler):
    """File system event handler for a specific repository.

    Filters events by branch (drops events on non-tracked branches) and by
    .gitignore before forwarding to the dispatcher with the resolved RepoContext.
    """

    def __init__(self, repo_id: str, repo_path: Path, parent_watcher, ctx: RepoContext):
        self.repo_id = repo_id
        self.repo_path = repo_path
        self.parent_watcher = parent_watcher
        self.ctx = ctx
        self.code_extensions = get_all_extensions()
        self._gitignore_filter = build_walker_filter(repo_path)

    def _refresh_context(self) -> bool:
        resolver = getattr(self.parent_watcher, "repo_resolver", None)
        if not isinstance(resolver, RepoResolver):
            return True
        try:
            ctx = resolver.resolve(self.repo_path)
            if (
                ctx is None
                or ctx.repo_id != self.repo_id
                or ctx.registry_entry.staleness_reason
                in {
                    "index_publication_pending",
                    "partial_index_failure",
                }
            ):
                return False
            self.ctx = ctx
            return True
        except Exception as exc:
            logger.warning(
                "Watcher context unavailable for %s: %s", self.repo_id, type(exc).__name__
            )
            return False

    def _get_current_branch(self) -> Optional[str]:
        """Return the current branch name for this repo, or None on failure."""
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repo_path), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                env=get_full_env(),
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def _mutate(self, operation):
        with lock_registry.acquire(self.repo_id, repo_path=self.repo_path):
            if not self._refresh_context() or not should_reindex_for_branch(
                self._get_current_branch(), self.ctx.tracked_branch
            ):
                return None
            try:
                return run_repository_mutation(
                    getattr(self.parent_watcher, "repo_resolver", None), self.ctx, operation
                )
            except Exception as exc:
                logger.warning(
                    "Watcher mutation unavailable for %s: %s", self.repo_id, type(exc).__name__
                )
                return None

    def _landed_mutation(
        self,
        result: object,
        *,
        success_status: IndexResultStatus,
        path: Path,
        action: str,
    ) -> bool:
        if not isinstance(result, IndexResult):
            logger.warning(
                "Unexpected %s mutation result for %s in repo %s: %r",
                action,
                path,
                self.repo_id,
                type(result).__name__,
            )
            return False
        if result.status == success_status:
            return True
        if result.status == IndexResultStatus.ERROR:
            logger.warning(
                "%s mutation failed for %s in repo %s: %s",
                action,
                path,
                self.repo_id,
                result.status.value,
            )
        return False

    def _trigger_reindex_with_ctx(self, path: Path) -> bool:
        """Branch + gitignore guarded reindex via ctx-aware dispatcher."""
        if isinstance(getattr(self.parent_watcher, "repo_resolver", None), RepoResolver):
            return self._reconcile_committed_event(path)
        if not self._refresh_context():
            return False
        current_branch = self._get_current_branch()
        if not should_reindex_for_branch(current_branch, self.ctx.tracked_branch):
            logger.debug(
                "Dropping reindex event for %s: branch %s != tracked %s",
                path,
                current_branch,
                self.ctx.tracked_branch,
            )
            return False

        if self._gitignore_filter(path):
            logger.debug("Dropping reindex event for %s: matched gitignore filter", path)
            return False

        if path.suffix not in self.code_extensions:
            return False
        if not path.exists():
            return False

        logger.info("Re-indexing %s (repo=%s)", path, self.repo_id)
        try:
            observed_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            logger.warning("Could not read %s for hash; skipping reindex", path)
            return False

        def reindex(current):
            remove_result = self.parent_watcher.dispatcher.remove_file(current, path)
            if (
                isinstance(remove_result, IndexResult)
                and remove_result.status == IndexResultStatus.ERROR
            ):
                logger.warning(
                    "Pre-index remove failed for %s in repo %s: %s",
                    path,
                    self.repo_id,
                    remove_result.status.value,
                )
                return remove_result
            return self.parent_watcher.dispatcher.index_file_guarded(
                current,
                path,
                observed_hash,
            )

        index_result = self._mutate(reindex)
        return self._landed_mutation(
            index_result,
            success_status=IndexResultStatus.INDEXED,
            path=path,
            action="reindex",
        )

    def _remove_with_ctx(self, path: Path) -> bool:
        """Branch + gitignore guarded remove via ctx-aware dispatcher."""
        if isinstance(getattr(self.parent_watcher, "repo_resolver", None), RepoResolver):
            return self._reconcile_committed_event(path)
        if not self._refresh_context():
            return False
        current_branch = self._get_current_branch()
        if not should_reindex_for_branch(current_branch, self.ctx.tracked_branch):
            logger.debug(
                "Dropping remove event for %s: branch %s != tracked %s",
                path,
                current_branch,
                self.ctx.tracked_branch,
            )
            return False

        if self._gitignore_filter(path):
            return False

        if path.suffix not in self.code_extensions:
            return False

        logger.info("Removing from index: %s (repo=%s)", path, self.repo_id)
        result = self._mutate(
            lambda current: self.parent_watcher.dispatcher.remove_file(current, path)
        )
        return self._landed_mutation(
            result,
            success_status=IndexResultStatus.DELETED,
            path=path,
            action="remove",
        )

    def _move_with_ctx(self, old_path: Path, new_path: Path) -> bool:
        """Branch + gitignore guarded move via ctx-aware dispatcher."""
        if isinstance(getattr(self.parent_watcher, "repo_resolver", None), RepoResolver):
            return self._reconcile_committed_event(new_path)
        if not self._refresh_context():
            return False
        current_branch = self._get_current_branch()
        if not should_reindex_for_branch(current_branch, self.ctx.tracked_branch):
            return False

        if self._gitignore_filter(new_path):
            return False

        exts = self.code_extensions
        if old_path.suffix not in exts and new_path.suffix not in exts:
            return False

        logger.info("Moving in index: %s -> %s (repo=%s)", old_path, new_path, self.repo_id)

        def move(current):
            if new_path.exists():
                return self.parent_watcher.dispatcher.move_file(current, old_path, new_path)
            return self.parent_watcher.dispatcher.remove_file(current, old_path)

        result = self._mutate(move)
        if isinstance(result, IndexResult) and result.status == IndexResultStatus.MOVED:
            return True
        return self._landed_mutation(
            result,
            success_status=IndexResultStatus.DELETED,
            path=old_path,
            action="remove_stale_source",
        )

    def _reconcile_committed_event(self, path: Path) -> bool:
        """Watchdog is a hint to reconcile Git, never authority for working-tree bytes."""
        if not self._refresh_context() or build_walker_filter(self.repo_path)(path):
            return False
        try:
            result = self.parent_watcher.index_manager.sync_repository_index(self.repo_id)
        except Exception as exc:
            logger.warning("Committed event reconciliation deferred (%s)", type(exc).__name__)
            return False
        return result.action in {"full_index", "incremental_update"}

    def on_any_event(self, event):
        """Route watchdog events through branch + gitignore guards."""
        if event.is_directory:
            return

        logger.debug("Event in %s: %s", self.repo_id, event)

        src = Path(event.src_path)
        etype = event.event_type

        if etype in ("created", "modified"):
            mutated = self._trigger_reindex_with_ctx(src)
        elif etype == "moved":
            dest = Path(event.dest_path)
            mutated = self._move_with_ctx(src, dest)
        elif etype == "deleted":
            mutated = self._remove_with_ctx(src)
        else:
            mutated = False

        if mutated:
            self.parent_watcher.mark_repository_changed(self.repo_id)


class MultiRepositoryWatcher:
    """Watches multiple repositories and syncs with git."""

    def __init__(
        self,
        registry: RepositoryRegistry,
        dispatcher: EnhancedDispatcher,
        index_manager: GitAwareIndexManager,
        artifact_manager: Optional[CommitArtifactManager] = None,
        repo_resolver: Optional[RepoResolver] = None,
        sweeper: Optional[WatcherSweeper] = None,
        store_registry: Optional[object] = None,
        plugin_set_registry: Optional[object] = None,
        semantic_indexer_registry: Optional[object] = None,
    ):
        self.registry = registry
        self.dispatcher = dispatcher
        self.index_manager = index_manager
        self.artifact_manager = artifact_manager or CommitArtifactManager()
        self.repo_resolver = repo_resolver
        self.store_registry = store_registry or getattr(index_manager, "store_registry", None)
        self.plugin_set_registry = plugin_set_registry
        self.semantic_indexer_registry = semantic_indexer_registry
        self.sweeper: Optional[WatcherSweeper] = sweeper or self._build_default_sweeper()

        self.watchers = {}  # repo_id -> MultiRepositoryHandler
        self.observers = {}  # repo_id -> Observer instance
        self.changed_repos = set()  # Repos with uncommitted changes
        self.git_monitor = GitMonitor(registry, self.on_git_commit, self._reconcile_registry)
        self._watch_lock = threading.RLock()
        self._pending_syncs = {}

        self.query_cache = None
        self.path_resolver = None

        self.executor = ThreadPoolExecutor(max_workers=4)
        self.running = False

        # Injected by caller; never instantiated here (circular-import guard).
        self._artifact_publisher = None

        # Keep the diagnostic hook available; wrong-branch sync is non-mutating.
        self.index_manager.on_branch_drift = self._on_branch_drift

    def _on_branch_drift(self, repo_id: str, current_branch: str, tracked_branch: str) -> None:
        """Called by GitAwareIndexManager when branch drift is detected."""
        logger.warning(
            "branch drift observed for %s: current=%s tracked=%s",
            repo_id,
            current_branch,
            tracked_branch,
        )

    def _reconcile_registry(self, repositories) -> None:
        """Observe external registration changes without modifying registry authority."""
        if not self.running:
            return
        desired = {
            repo_id: info
            for repo_id, info in repositories.items()
            if info.auto_sync and info.active
        }
        with self._watch_lock:
            for repo_id, handler in list(self.watchers.items()):
                info = desired.get(repo_id)
                if (
                    info is None
                    or Path(info.path) != handler.repo_path
                    or (
                        getattr(info, "registration_id", None)
                        != getattr(handler.ctx.registry_entry, "registration_id", None)
                    )
                ):
                    self._stop_repo_watcher(repo_id)
            for repo_id, info in desired.items():
                self._start_repo_watcher(repo_id, info.path)

    def _build_default_sweeper(self) -> Optional[WatcherSweeper]:
        store_registry = getattr(self.index_manager, "store_registry", None)
        if store_registry is None:
            logger.warning("WatcherSweeper default wiring unavailable: no store registry")
            return None

        def _repo_roots() -> Dict[str, Path]:
            return {
                repo_id: Path(repo.path)
                for repo_id, repo in self.registry.get_all_repositories().items()
                if repo.auto_sync and getattr(repo, "active", True)
            }

        if not self.registry.get_all_repositories():
            return None
        return WatcherSweeper(
            on_missed_path=None,
            repo_roots_provider=_repo_roots,
            store=None,
            store_provider=store_registry.get,
            on_missed_create=self._on_missed_create,
            on_missed_delete=self._on_missed_delete,
            on_missed_rename=self._on_missed_rename,
            on_repository_drift=self._reconcile_repository_drift,
        )

    def _reconcile_repository_drift(self, repo_id: str) -> None:
        repo = self.registry.get_repository(repo_id)
        if repo is None or not repo.auto_sync or not getattr(repo, "active", True):
            return
        result = self.index_manager.sync_repository_index(repo_id, force_full=True)
        if result.action in {"full_index", "incremental_update"}:
            self.mark_repository_changed(repo_id)

    def enqueue_full_rescan(self, repo_id: str) -> None:
        """Submit a force-full reindex to the thread pool; returns immediately."""

        def _rescan():
            self.index_manager.sync_repository_index(repo_id, force_full=True)

        self.executor.submit(_rescan)

    def _handler_for_missed_event(self, repo_id: str) -> Optional[MultiRepositoryHandler]:
        handler = self.watchers.get(repo_id)
        if handler is not None:
            return handler
        repo_info = self.registry.get_repository(repo_id)
        if repo_info is None:
            return None
        repo_root = Path(repo_info.path)
        ctx = self.repo_resolver.resolve(repo_root) if self.repo_resolver is not None else None
        if ctx is None:
            tracked = getattr(repo_info, "tracked_branch", None) or ""
            ctx = RepoContext(
                repo_id=repo_id,
                sqlite_store=None,  # type: ignore[arg-type]
                workspace_root=repo_root,
                tracked_branch=tracked,
                registry_entry=repo_info,
                requested_path=repo_root,
            )
        return MultiRepositoryHandler(repo_id, repo_root, self, ctx=ctx)

    def _on_missed_create(self, repo_id: str, relative_path: str) -> None:
        handler = self._handler_for_missed_event(repo_id)
        if handler and handler._trigger_reindex_with_ctx(handler.repo_path / relative_path):
            self.mark_repository_changed(repo_id)

    def _on_missed_delete(self, repo_id: str, relative_path: str) -> None:
        handler = self._handler_for_missed_event(repo_id)
        if handler and handler._remove_with_ctx(handler.repo_path / relative_path):
            self.mark_repository_changed(repo_id)

    def _on_missed_rename(
        self, repo_id: str, old_relative_path: str, new_relative_path: str
    ) -> None:
        handler = self._handler_for_missed_event(repo_id)
        if handler and handler._move_with_ctx(
            handler.repo_path / old_relative_path,
            handler.repo_path / new_relative_path,
        ):
            self.mark_repository_changed(repo_id)

    def start_watching_all(self):
        """Start watching all registered repositories."""
        self.running = True

        self._reconcile_registry(self.registry.get_all_repositories())

        # Start git monitor
        self.git_monitor.start()

        # Start sweeper if configured
        if self.sweeper is not None:
            self.sweeper.start()

        logger.info(f"Started watching {len(self.watchers)} repositories")

    def stop_watching_all(self):
        """Stop all watchers."""
        self.running = False

        # Stop git monitor
        self.git_monitor.stop()

        # Stop sweeper if configured
        if self.sweeper is not None:
            self.sweeper.stop()

        # Stop all file watchers
        with self._watch_lock:
            observers = list(self.observers.values())
        for observer in observers:
            observer.stop()
        for observer in observers:
            observer.join()

        self.executor.shutdown(wait=True, cancel_futures=True)

        self.watchers.clear()
        self.observers.clear()

        logger.info("Stopped all repository watchers")

    stop = stop_watching_all

    def add_repository(self, repo_path: str) -> str:
        """Add a new repository to watch.

        Args:
            repo_path: Repository path

        Returns:
            Repository ID
        """
        repo_id = self.registry.register_repository(repo_path)

        if self.running:
            self._start_repo_watcher(repo_id, repo_path)

        return repo_id

    def remove_repository(self, repo_id: str):
        """Remove a repository from watching.

        Args:
            repo_id: Repository ID
        """
        repo_info = self.registry.get_repository(repo_id)
        self._stop_repo_watcher(repo_id, repo_info)
        if repo_info is not None:
            self.registry.unregister_repository(repo_id, expected_owner=repo_info)

    def _stop_repo_watcher(self, repo_id: str, repo_info=None) -> None:
        with self._watch_lock:
            observer = self.observers.pop(repo_id, None)
            handler = self.watchers.pop(repo_id, None)
            pending = self._pending_syncs.pop(repo_id, [])
            for future, retired in pending:
                retired.set()
                future.cancel()
            if observer is not None:
                observer.stop()
                observer.join()
        for future, _retired in pending:
            if not future.cancelled():
                future.result()
        repo_root = (
            Path(repo_info.path)
            if repo_info is not None
            else (handler.repo_path if handler else None)
        )
        owner = handler.ctx.registry_entry if handler is not None else repo_info
        if owner is None:
            return
        if self.store_registry is not None and hasattr(self.store_registry, "close"):
            self.store_registry.close(repo_id, expected_owner=owner)
        # Plugins are shared by repo ID, not registration; their lifecycle manager owns eviction.
        if self.semantic_indexer_registry is not None and hasattr(
            self.semantic_indexer_registry, "evict"
        ):
            self.semantic_indexer_registry.evict(repo_id, expected_owner=owner)
        if hasattr(self.dispatcher, "evict_repository_state"):
            self.dispatcher.evict_repository_state(
                repo_id, repo_root=repo_root, expected_owner=owner
            )

    def _start_repo_watcher(self, repo_id: str, repo_path: str):
        """Start watching a specific repository."""
        with self._watch_lock:
            if repo_id in self.observers:
                return

            try:
                repo_root = Path(repo_path)
                if not repo_root.exists():
                    logger.error("Repository path does not exist: %s", repo_path)
                    return

                # Resolve RepoContext for per-repo dispatcher routing.
                ctx: Optional[RepoContext] = None
                if self.repo_resolver is not None:
                    ctx = self.repo_resolver.resolve(repo_root)
                if ctx is None:
                    # Fallback: minimal context so the handler can still filter by branch.
                    repo_info = self.registry.get_repository(repo_id)
                    tracked = (repo_info.tracked_branch if repo_info else None) or ""
                    # Build a bare RepoContext without a live sqlite_store.
                    ctx = RepoContext(
                        repo_id=repo_id,
                        sqlite_store=None,  # type: ignore[arg-type]
                        workspace_root=repo_root,
                        tracked_branch=tracked,
                        registry_entry=repo_info,
                        requested_path=repo_root,
                    )

                handler = MultiRepositoryHandler(repo_id, repo_root, self, ctx=ctx)

                observer = Observer()
                observer.schedule(handler, str(repo_root), recursive=True)
                observer.start()

                self.observers[repo_id] = observer
                self.watchers[repo_id] = handler

                logger.info("Started watching repository: %s at %s", repo_id, repo_path)

            except Exception as e:
                logger.error("Failed to start watcher for %s: %s", repo_id, type(e).__name__)

    def mark_repository_changed(self, repo_id: str):
        """Mark a repository as having uncommitted changes.

        Args:
            repo_id: Repository ID
        """
        self.changed_repos.add(repo_id)

    def on_git_commit(self, repo_id: str, commit: str):
        """Handle new git commit in repository.

        Args:
            repo_id: Repository ID
            commit: New commit SHA
        """
        logger.info(f"Processing new commit in {repo_id}: {commit[:8]}")

        # Remove from changed set (changes are now committed)
        self.changed_repos.discard(repo_id)

        with self._watch_lock:
            if not self.running or repo_id not in self.watchers:
                return
            owner = self.watchers[repo_id].ctx.registry_entry
            retired = threading.Event()
            pending = self._pending_syncs.setdefault(repo_id, [])
            pending[:] = [(future, token) for future, token in pending if not future.done()]
            pending.append(
                (
                    self.executor.submit(
                        self._sync_admitted_repository, repo_id, commit, owner, retired
                    ),
                    retired,
                )
            )

    def _sync_admitted_repository(self, repo_id, commit, owner, retired):
        with lock_registry.acquire(repo_id, repo_path=Path(owner.path)):
            if not retired.is_set():
                self._sync_repository(repo_id, commit, owner=owner)

    def _sync_repository(self, repo_id: str, commit: str, *, owner=None):
        """Sync repository index with new commit.

        Args:
            repo_id: Repository ID
            commit: Git commit SHA
        """
        try:
            # Sync the index
            kwargs = (
                {"expected_registration_id": owner.registration_id} if owner is not None else {}
            )
            result = self.index_manager.sync_repository_index(repo_id, **kwargs)

            successful_mutation = result.action in {"full_index", "incremental_update"}
            if successful_mutation:
                logger.info(
                    f"Repository {repo_id} synced: "
                    f"{result.files_processed} files in {result.duration_seconds:.2f}s"
                )

                repo_info = self.registry.get_repository(repo_id)
                if owner is not None and (
                    repo_info is None or repo_info.registration_id != owner.registration_id
                ):
                    return
                synced_commit = getattr(result, "commit", None) or commit

                if (
                    repo_info
                    and repo_info.artifact_enabled
                    and self._artifact_publisher is not None
                ):
                    try:
                        from contextlib import nullcontext

                        semantic = getattr(self.dispatcher, "_semantic_registry", None)
                        lease = (
                            semantic.lease(repo_id) if semantic is not None else nullcontext(None)
                        )
                        with lease as indexer:
                            self._artifact_publisher.publish_on_reindex(
                                repo_id,
                                synced_commit,
                                tracked_branch=getattr(repo_info, "tracked_branch", None) or "main",
                                index_location=getattr(repo_info, "index_location", None),
                                index_path=repo_info.index_path,
                                repo_path=repo_info.path,
                                semantic_indexer=indexer,
                            )
                        if hasattr(self.registry, "update_artifact_state"):
                            self.registry.update_artifact_state(
                                repo_id,
                                expected_owner=repo_info,
                                last_published_commit=synced_commit,
                                artifact_health="published",
                            )
                    except Exception as pub_exc:
                        logger.error(
                            "ArtifactPublisher.publish_on_reindex failed for %s: %s",
                            repo_id,
                            type(pub_exc).__name__,
                        )
                        if repo_info and hasattr(self.registry, "update_artifact_state"):
                            self.registry.update_artifact_state(
                                repo_id,
                                expected_owner=repo_info,
                                artifact_health="publish_failed",
                            )
                elif (
                    repo_info
                    and repo_info.artifact_enabled
                    and hasattr(self.registry, "update_artifact_state")
                ):
                    self.registry.update_artifact_state(
                        repo_id,
                        expected_owner=repo_info,
                        artifact_health="local_only",
                    )

        except Exception as e:
            logger.error(f"Failed to sync repository {repo_id}: {type(e).__name__}")

    def sync_all_repositories(self):
        """Manually trigger sync for all repositories."""
        futures = []

        for repo_id, repo_info in self.registry.get_all_repositories().items():
            if repo_info.auto_sync and repo_info.active:

                def _locked_sync(rid=repo_id):
                    return self.index_manager.sync_repository_index(rid)

                future = self.executor.submit(_locked_sync)
                futures.append((repo_id, future))

        # Wait for all to complete
        for repo_id, future in futures:
            try:
                result = future.result(timeout=300)  # 5 minute timeout
                logger.info(f"Synced {repo_id}: {result.action}")
            except Exception as e:
                logger.error(f"Failed to sync {repo_id}: {type(e).__name__}")

    def get_status(self) -> Dict[str, Any]:
        """Get status of all watched repositories.

        Returns:
            Status dictionary
        """
        with self._watch_lock:
            repo_ids = list(self.watchers)
            changed_repos = set(self.changed_repos)
        status = {"watching": len(repo_ids), "repositories": {}}

        for repo_id in repo_ids:
            repo_info = self.registry.get_repository(repo_id)
            if repo_info:
                repo_status = self.index_manager.get_repository_status(repo_id)
                repo_status["has_uncommitted_changes"] = repo_id in changed_repos
                status["repositories"][repo_id] = repo_status

        return status
