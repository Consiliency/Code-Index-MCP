"""Tracked-ref poller: triggers incremental reindex on branch tip advance."""

import logging
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict

from ..health.repository_readiness import RepositoryReadinessState

logger = logging.getLogger(__name__)

# Per-repo attachment state: "attached" | "detached"
# Keyed by repository_id.  Module-level to survive poller restarts, but the
# poller is single-writer per loop iteration so no lock is needed.
_FALLBACK_BRANCHES = ("main", "master", "HEAD")


class RefPoller:
    """Poll refs/heads/<tracked_branch> per repo and trigger reindex on advance."""

    def __init__(
        self,
        registry: Any,
        git_index_manager: Any,
        dispatcher: Any,
        repo_resolver: Any,
        *,
        interval_seconds: int = 30,
    ) -> None:
        self._registry = registry
        self._git_index_manager = git_index_manager
        self._repo_resolver = repo_resolver
        self._interval = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # "attached" | "detached" per repo_id; unknown treated as "attached"
        self._last_branch_state: Dict[str, str] = {}

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="RefPoller")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            for repo in self._registry.list_all():
                try:
                    self._poll_one(repo)
                except Exception:
                    logger.exception(
                        "Unhandled error polling repo %s", getattr(repo, "repository_id", repo)
                    )
            self._stop_event.wait(self._interval)

    def _poll_one(self, repo_info: Any) -> None:
        tracked_branch = getattr(repo_info, "tracked_branch", None)
        if not tracked_branch:
            return

        repo_id = repo_info.repository_id
        repo_path = Path(repo_info.path)
        if self._is_unsupported_worktree(repo_path):
            logger.warning("ref_poller: unsupported worktree for %s; skipping", repo_id)
            return

        current_branch = self._current_branch(repo_path)
        if current_branch is not None and current_branch != tracked_branch:
            if self._read_ref(repo_path, tracked_branch) is None:
                logger.warning(
                    "ref_poller: tracked ref %s missing for %s; skipping",
                    tracked_branch,
                    repo_id,
                )
            else:
                logger.info(
                    "ref_poller: skipping %s because current branch %r != tracked branch %r",
                    repo_id,
                    current_branch,
                    tracked_branch,
                )
            self._last_branch_state[repo_id] = "attached"
            return

        if current_branch != tracked_branch:
            self._last_branch_state[repo_id] = "detached" if current_branch is None else "attached"
            logger.info(
                "ref_poller: skipping %s because current branch %r != tracked branch %r",
                repo_id,
                current_branch,
                tracked_branch,
            )
            return

        tip_sha = self._read_ref(repo_path, tracked_branch)

        # --- Detached HEAD / branch-rename detection ---
        if tip_sha is None:
            self._last_branch_state[repo_id] = "detached"
            logger.warning(
                "ref_poller: tracked ref %s missing for %s; skipping",
                tracked_branch,
                repo_id,
            )
            return

        # Tip resolved — mark as attached
        self._last_branch_state[repo_id] = "attached"

        last = getattr(repo_info, "last_indexed_commit", None)
        if tip_sha == last:
            return  # nothing changed

        # --- Force-push detection ---
        if last:
            is_ancestor = self._is_ancestor(repo_path, last, tip_sha)
            if not is_ancestor:
                logger.warning(
                    "ref_poller: force-push detected for %s (old=%s new=%s); enqueueing full rescan",
                    repo_id,
                    last,
                    tip_sha,
                )
                self._git_index_manager.enqueue_full_rescan(repo_id)
                return

        # Normal fast-forward advance
        self._git_index_manager.sync_repository_index(repo_id)

    def _is_ancestor(self, repo_path: Path, old_sha: str, new_sha: str) -> bool:
        """Return True if old_sha is an ancestor of new_sha (normal advance)."""
        result = subprocess.run(
            ["git", "-C", str(repo_path), "merge-base", "--is-ancestor", old_sha, new_sha],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    def _read_ref(self, repo_path: Path, branch: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "rev-parse", "--verify", f"refs/heads/{branch}"],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            logger.warning(
                "Could not resolve ref refs/heads/%s for repo at %s; skipping",
                branch,
                repo_path,
            )
            return None

    def _current_branch(self, repo_path: Path) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            return None
        value = result.stdout.strip()
        return value if value and value != "HEAD" else None

    def _is_unsupported_worktree(self, repo_path: Path) -> bool:
        classify = getattr(self._repo_resolver, "classify", None)
        if not callable(classify):
            return False
        try:
            readiness = classify(repo_path)
        except Exception:
            return False
        return getattr(readiness, "state", None) == RepositoryReadinessState.UNSUPPORTED_WORKTREE
