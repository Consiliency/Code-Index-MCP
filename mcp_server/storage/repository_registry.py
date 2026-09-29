"""
Repository Registry for Multi-Repository Management

This module handles persistent storage and management of repository
registration information for cross-repository search.
"""

import fcntl
import json
import logging
import os
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp_server.storage.repo_identity import compute_repo_id, resolve_tracked_branch

logger = logging.getLogger(__name__)


class MultipleWorktreesUnsupportedError(ValueError):
    """Raised when a second filesystem path targets an already registered git common dir."""

    code = "multiple_worktrees_unsupported"

    def __init__(
        self,
        *,
        registered_path: Path,
        requested_path: Path,
        git_common_dir: Path,
    ) -> None:
        self.registered_path = str(Path(registered_path).resolve(strict=False))
        self.requested_path = str(Path(requested_path).resolve(strict=False))
        self.git_common_dir = str(Path(git_common_dir).resolve(strict=False))
        self.remediation = (
            "Use the registered path or unregister it before registering another worktree."
        )
        super().__init__(
            f"{self.code}: {self.requested_path} shares git common dir "
            f"{self.git_common_dir} with registered path {self.registered_path}"
        )

    def to_dict(self) -> Dict[str, str]:
        return {
            "code": self.code,
            "registered_path": self.registered_path,
            "requested_path": self.requested_path,
            "git_common_dir": self.git_common_dir,
            "remediation": self.remediation,
        }


class RepositoryRegistry:
    """
    Manages persistent registry of repositories for multi-repository search.

    Features:
    - JSON-based persistent storage
    - Thread-safe operations
    - Repository metadata management
    - Active/inactive status tracking
    """

    def __init__(self, registry_path: Optional[Path] = None):
        """
        Initialize the repository registry.

        Args:
            registry_path: Path to registry JSON file. Defaults to ~/.mcp/repository_registry.json
        """
        self.registry_path = registry_path or self._get_default_registry_path()
        self._lock = threading.RLock()
        self._registry: Dict[str, Dict[str, Any]] = {}
        self._baseline: Dict[str, Dict[str, Any]] = {}

        # Ensure parent directory exists
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)

        # Load existing registry
        self._load()

        logger.info(f"Repository registry initialized at {registry_path}")

    def _get_default_registry_path(self) -> Path:
        """Return the default registry path under the user home directory."""
        env_path = os.environ.get("MCP_REPO_REGISTRY")
        if env_path:
            return Path(env_path)

        home = Path.home()
        return home / ".mcp" / "repository_registry.json"

    def _load(self):
        """Load registry from disk, migrating legacy entries to new id scheme."""
        with self._transaction(write=True):
            self._migrate_registry(self._registry)

    def _read_registry(self) -> Dict[str, Dict[str, Any]]:
        """Read one complete on-disk revision; malformed state is not an empty registry."""
        try:
            with self.registry_path.open() as source:
                data = json.load(source)
        except FileNotFoundError:
            return {}
        for repo_data in data.values():
            repo_data["path"] = Path(repo_data["path"])
            repo_data["index_path"] = Path(repo_data["index_path"])
            for field in ("indexed_at", "last_indexed"):
                if repo_data.get(field):
                    repo_data[field] = datetime.fromisoformat(repo_data[field])
        return data

    @contextmanager
    def _transaction(self, *, write: bool = False):
        """Hold thread and process locks from reload through durable publication."""
        with self._lock:
            fd = os.open(self.registry_path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
                fresh = self._read_registry()
                self._registry = deepcopy(fresh)
                self._baseline = deepcopy(fresh)
                try:
                    yield
                    if write and self._registry != fresh:
                        self._persist()
                    self._baseline = deepcopy(self._registry)
                except BaseException:
                    self._registry = fresh
                    self._baseline = deepcopy(fresh)
                    raise
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _persist(self) -> None:
        """Publish while holding the registry lock; never acknowledge a failed fsync."""
        fd, name = tempfile.mkstemp(
            prefix=self.registry_path.name + ".", dir=self.registry_path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as target:
                json.dump(self._serialize_registry(), target, indent=2)
                target.flush()
                os.fsync(target.fileno())
            temporary.replace(self.registry_path)
            parent_fd = os.open(self.registry_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _migrate_registry(self, raw: Dict[str, Any]) -> bool:
        """Re-key any legacy entries and back-fill new fields. Returns True if changes made."""
        changed = False
        renames: Dict[str, str] = {}

        for old_id, repo_dict in list(raw.items()):
            if not repo_dict.get("registration_id"):
                repo_dict["registration_id"] = uuid.uuid4().hex
                changed = True
            repo_path = Path(repo_dict.get("path", ""))
            if not repo_path.exists():
                continue
            try:
                identity = compute_repo_id(repo_path)
            except Exception as exc:
                logger.warning(f"compute_repo_id failed for {repo_path}: {exc}")
                continue

            new_id = identity.repo_id

            # Back-fill tracked_branch and git_common_dir if missing
            if not repo_dict.get("tracked_branch"):
                try:
                    branch = resolve_tracked_branch(identity.git_common_dir)
                    repo_dict["tracked_branch"] = branch if branch else None
                    changed = True
                except Exception as exc:
                    logger.warning(f"resolve_tracked_branch failed for {repo_path}: {exc}")

            if not repo_dict.get("git_common_dir") and identity.git_common_dir:
                repo_dict["git_common_dir"] = str(identity.git_common_dir)
                changed = True

            if new_id != old_id:
                renames[old_id] = new_id
                changed = True

        for old_id, new_id in renames.items():
            repo_dict = raw.pop(old_id)
            repo_dict["repository_id"] = new_id
            raw[new_id] = repo_dict
            self._migrate_sqlite(repo_dict, old_id, new_id)

        return changed

    def _migrate_sqlite(self, repo_dict: Dict[str, Any], old_id: str, new_id: str) -> None:
        """Update repository_id text FK columns in the per-repo SQLite index."""
        index_path = repo_dict.get("index_path")
        if index_path is None:
            return
        db_path = str(index_path)
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA foreign_keys=OFF")
            cursor = conn.cursor()
            for table in ("files", "symbols", "bm25_content", "imports"):
                cursor.execute(f"PRAGMA table_info({table})")
                cols = [row[1] for row in cursor.fetchall()]
                if "repository_id" not in cols:
                    logger.debug(
                        f"Table {table} in {db_path} has no repository_id column, skipping"
                    )
                    continue
                cursor.execute(
                    f"UPDATE {table} SET repository_id=? WHERE repository_id=?",
                    (new_id, old_id),
                )
                logger.debug(
                    f"Migrated {cursor.rowcount} rows in {table} " f"({old_id} → {new_id})"
                )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning(f"SQLite migration failed for {db_path} ({old_id} → {new_id}): {exc}")

    def _serialize_registry(self) -> Dict[str, Any]:
        """Return a JSON-serializable snapshot of self._registry."""
        data: Dict[str, Any] = {}
        for repo_id, repo_info in self._registry.items():
            repo_data = repo_info.copy()
            repo_data["path"] = str(repo_data["path"])
            repo_data["index_path"] = str(repo_data["index_path"])
            if "indexed_at" in repo_data and hasattr(repo_data["indexed_at"], "isoformat"):
                repo_data["indexed_at"] = repo_data["indexed_at"].isoformat()
            if "last_indexed" in repo_data and hasattr(repo_data["last_indexed"], "isoformat"):
                repo_data["last_indexed"] = repo_data["last_indexed"].isoformat()
            if "index_location" in repo_data:
                repo_data["index_location"] = str(repo_data["index_location"])
            data[repo_id] = repo_data
        return data

    def save(self):
        """Apply legacy in-memory edits as field deltas, not stale whole-row snapshots."""
        with self._lock:
            local, baseline = deepcopy(self._registry), self._baseline
            with self._transaction(write=True):
                for repo_id in baseline.keys() - local.keys():
                    current = self._registry.get(repo_id)
                    if current and current.get("registration_id") == baseline[repo_id].get(
                        "registration_id"
                    ):
                        self._registry.pop(repo_id, None)
                for repo_id, entry in local.items():
                    previous = baseline.get(repo_id)
                    if previous is None:
                        self._registry.setdefault(repo_id, entry)
                    elif repo_id in self._registry:
                        current = self._registry[repo_id]
                        if current.get("registration_id") != previous.get("registration_id"):
                            continue
                        for key in previous.keys() - entry.keys():
                            current.pop(key, None)
                        for key, value in entry.items():
                            if key not in previous or previous[key] != value:
                                current[key] = value

    def register(self, repo_info):
        """
        Register a repository.

        Args:
            repo_info: RepositoryInfo dataclass instance
        """
        with self._transaction(write=True):
            # Convert dataclass to dict
            repo_data = asdict(repo_info)
            repo_data["registration_id"] = uuid.uuid4().hex
            for existing in self._registry.values():
                same_id = existing["repository_id"] == repo_info.repository_id
                common = repo_data.get("git_common_dir")
                same_common = (
                    common
                    and existing.get("git_common_dir")
                    and (Path(common).resolve() == Path(existing["git_common_dir"]).resolve())
                )
                if same_id or same_common:
                    if Path(existing["path"]).resolve() != Path(repo_info.path).resolve():
                        raise MultipleWorktreesUnsupportedError(
                            registered_path=existing["path"],
                            requested_path=repo_info.path,
                            git_common_dir=Path(common or repo_info.path),
                        )
                    return
            self._registry[repo_info.repository_id] = repo_data

            logger.info(f"Registered repository: {repo_info.name} ({repo_info.repository_id})")

    def unregister(self, repository_id: str, *, expected_owner=None):
        """
        Unregister a repository.

        Args:
            repository_id: ID of repository to unregister
        """
        with self._transaction(write=True):
            if repository_id in self._registry:
                if (
                    expected_owner is not None
                    and self._registry[repository_id].get("registration_id")
                    != expected_owner.registration_id
                ):
                    return False
                repo_name = self._registry[repository_id].get("name", "Unknown")
                del self._registry[repository_id]
                logger.info(f"Unregistered repository: {repo_name} ({repository_id})")
                return True
            else:
                logger.warning(f"Repository {repository_id} not found in registry")
                return False

    def register_repository(
        self,
        repo_path: str,
        auto_sync: bool = True,
        artifact_enabled: bool = True,
        priority: int = 0,
    ) -> str:
        """Register a repository by filesystem path and return its repository ID."""
        path = Path(repo_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Repository path does not exist: {repo_path}")

        existing = self.find_by_path(path)
        if existing:
            return existing

        # Import locally to avoid circular imports
        from mcp_server.storage.multi_repo_manager import RepositoryInfo

        identity = compute_repo_id(path)
        if identity.git_common_dir is not None:
            existing_common = self.find_by_git_common_dir(identity.git_common_dir)
            if existing_common:
                existing_info = self.get(existing_common)
                if existing_info is not None and Path(existing_info.path).resolve() != path:
                    raise MultipleWorktreesUnsupportedError(
                        registered_path=existing_info.path,
                        requested_path=path,
                        git_common_dir=identity.git_common_dir,
                    )

        repo_id = identity.repo_id
        tracked = resolve_tracked_branch(identity.git_common_dir)
        index_base = path / ".mcp-index"
        index_db = index_base / "current.db"

        repo_info = RepositoryInfo(
            repository_id=repo_id,
            name=path.name,
            path=path,
            index_path=index_db,
            language_stats={},
            total_files=0,
            total_symbols=0,
            indexed_at=datetime.now(),
            current_commit=self._get_git_commit(path),
            last_indexed_commit=None,
            last_indexed=None,
            current_branch=self._get_preferred_branch(path),
            url=self._get_git_remote(path),
            auto_sync=auto_sync,
            artifact_enabled=artifact_enabled,
            active=True,
            priority=priority,
            index_location=str(index_base),
            artifact_backend="local_workspace",
            artifact_health="missing",
            available_semantic_profiles=[],
            tracked_branch=tracked if tracked else None,
            git_common_dir=str(identity.git_common_dir) if identity.git_common_dir else None,
        )

        self.register(repo_info)
        return repo_id

    def unregister_repository(self, repository_id: str, *, expected_owner=None) -> bool:
        """Unregister a repository and return whether it existed."""
        return self.unregister(repository_id, expected_owner=expected_owner)

    def get_all_repositories(self) -> Dict[str, Any]:
        """Return all repositories keyed by repository ID."""
        with self._transaction():
            return {
                repo_id: self._dict_to_repo_info(deepcopy(repo_data))
                for repo_id, repo_data in self._registry.items()
            }

    def get_repository_by_path(self, repo_path: str) -> Optional[Any]:
        """Return repository info for a registered path or any subdirectory of one."""
        search_path = Path(repo_path.rstrip("/")).resolve()
        # First try exact match
        repo_id = self.find_by_path(search_path)
        if repo_id:
            return self.get(repo_id)
        # Then try parent directories (subdirectory lookup)
        for parent in search_path.parents:
            repo_id = self.find_by_path(parent)
            if repo_id:
                return self.get(repo_id)
        return None

    def set_artifact_enabled(self, repository_id: str, enabled: bool) -> bool:
        """Enable or disable artifact support for a repository."""
        with self._transaction(write=True):
            repo = self._registry.get(repository_id)
            if not repo:
                return False
            repo["artifact_enabled"] = enabled
            return True

    def update_artifact_state(
        self, repository_id: str, *, expected_owner: Any = None, **artifact_state: Any
    ) -> bool:
        """Update artifact lifecycle metadata for a repository."""
        with self._transaction(write=True):
            repo = self._registry.get(repository_id)
            if not repo:
                logger.warning(f"Repository {repository_id} not found in registry")
                return False

            if expected_owner is not None and any(
                repo.get(key) != getattr(expected_owner, key, None)
                for key in ("registration_id", "index_generation", "last_indexed_commit")
            ):
                return False

            for key, value in artifact_state.items():
                repo[key] = value
            return True

    def mark_artifact_published(
        self,
        repository_id: str,
        *,
        expected_registration_id: Optional[str],
        expected_generation: Optional[str],
        expected_commit: Optional[str],
    ) -> bool:
        """Record upload completion only for the generation that was uploaded."""
        with self._transaction(write=True):
            repo = self._registry.get(repository_id)
            if repo is None or (
                repo.get("registration_id") != expected_registration_id
                or repo.get("index_generation") != expected_generation
                or repo.get("last_indexed_commit") != expected_commit
            ):
                return False
            repo.update(
                last_published_commit=expected_commit,
                artifact_backend="github_release",
                artifact_health="published",
            )
            return True

    def update_staleness_reason(
        self, repository_id: str, reason: Optional[str], *, expected_owner: Any = None
    ) -> bool:
        """Persist a repo-local staleness marker for status/reporting surfaces."""
        with self._transaction(write=True):
            repo = self._registry.get(repository_id)
            if not repo:
                logger.warning(f"Repository {repository_id} not found in registry")
                return False

            if expected_owner is not None and any(
                repo.get(key) != getattr(expected_owner, key, None)
                for key in ("registration_id", "index_generation", "last_indexed_commit")
            ):
                return False
            repo["staleness_reason"] = reason
            return True

    def update_last_sync_error(self, repository_id: str, error: Optional[str]) -> bool:
        """Persist the latest exact sync blocker for status/reporting surfaces."""
        with self._transaction(write=True):
            repo = self._registry.get(repository_id)
            if not repo:
                logger.warning(f"Repository {repository_id} not found in registry")
                return False

            repo["last_sync_error"] = error
            return True

    def get(self, repository_id: str) -> Optional[Any]:
        """
        Get repository information.

        Args:
            repository_id: ID of repository

        Returns:
            RepositoryInfo-like dict or None if not found
        """
        with self._transaction():
            repo_data = self._registry.get(repository_id)
            if repo_data:
                # Return a copy to prevent external modifications
                return self._dict_to_repo_info(deepcopy(repo_data))
            return None

    def get_repository(self, repository_id: str) -> Optional[Any]:
        """
        Get repository information (alias for get).

        Args:
            repository_id: Repository identifier.

        Returns:
            RepositoryInfo or None.
        """
        return self.get(repository_id)

    def list_all(self) -> List[Any]:
        """
        List all registered repositories.

        Returns:
            List of RepositoryInfo-like objects
        """
        with self._transaction():
            repos = []
            for repo_data in self._registry.values():
                repos.append(self._dict_to_repo_info(deepcopy(repo_data)))
            return repos

    def _dict_to_repo_info(self, repo_dict: Dict[str, Any]) -> Any:
        """Convert dictionary back to RepositoryInfo-like object."""
        # Import here to avoid circular imports
        from mcp_server.storage.multi_repo_manager import RepositoryInfo

        # Ensure paths are Path objects
        if isinstance(repo_dict.get("path"), str):
            repo_dict["path"] = Path(repo_dict["path"])
        if isinstance(repo_dict.get("index_path"), str):
            repo_dict["index_path"] = Path(repo_dict["index_path"])

        # Ensure datetime is parsed
        if isinstance(repo_dict.get("indexed_at"), str):
            repo_dict["indexed_at"] = datetime.fromisoformat(repo_dict["indexed_at"])
        if isinstance(repo_dict.get("last_indexed"), str):
            repo_dict["last_indexed"] = datetime.fromisoformat(repo_dict["last_indexed"])

        return RepositoryInfo(**repo_dict)

    def update_status(self, repository_id: str, active: bool):
        """
        Update repository active status.

        Args:
            repository_id: ID of repository
            active: New active status
        """
        with self._transaction(write=True):
            if repository_id in self._registry:
                self._registry[repository_id]["active"] = active
                status = "activated" if active else "deactivated"
                logger.info(f"Repository {repository_id} {status}")
            else:
                logger.warning(f"Repository {repository_id} not found in registry")

    def update_priority(self, repository_id: str, priority: int):
        """
        Update repository search priority.

        Args:
            repository_id: ID of repository
            priority: New priority (higher = searched first)
        """
        with self._transaction(write=True):
            if repository_id in self._registry:
                self._registry[repository_id]["priority"] = priority
                logger.info(f"Repository {repository_id} priority set to {priority}")
            else:
                logger.warning(f"Repository {repository_id} not found in registry")

    def update_statistics(self, repository_id: str, stats: Dict[str, Any]):
        """
        Update repository statistics.

        Args:
            repository_id: ID of repository
            stats: New statistics (language_stats, total_files, total_symbols)
        """
        with self._transaction(write=True):
            if repository_id in self._registry:
                repo = self._registry[repository_id]

                # Update statistics
                if "language_stats" in stats:
                    repo["language_stats"] = stats["language_stats"]
                if "total_files" in stats:
                    repo["total_files"] = stats["total_files"]
                if "total_symbols" in stats:
                    repo["total_symbols"] = stats["total_symbols"]

                # Update indexed timestamp
                repo["indexed_at"] = datetime.now()
                logger.info(f"Updated statistics for repository {repository_id}")
            else:
                logger.warning(f"Repository {repository_id} not found in registry")

    def update_current_commit(self, repository_id: str) -> Optional[str]:
        """
        Refresh the current commit for a repository by reading its git HEAD.

        Args:
            repository_id: Repository identifier.

        Returns:
            The commit SHA if updated, otherwise None.
        """
        state = self.update_git_state(repository_id)
        return state.get("commit") or None if state else None

    def update_indexed_commit(
        self,
        repository_id: str,
        commit: str,
        branch: Optional[str] = None,
        *,
        expected_registration_id: Optional[str] = None,
        expected_generation: Optional[str] = None,
    ) -> Optional[str]:
        """
        Persist the last indexed commit for a repository.

        Args:
            repository_id: Repository identifier.
            commit: Commit SHA that was indexed.

        Returns:
            The stored commit SHA, or None if the repository was not found.
        """
        with self._transaction(write=True):
            repo = self._registry.get(repository_id)
            if not repo:
                logger.warning(f"Repository {repository_id} not found in registry")
                return None

            if expected_registration_id is not None and (
                repo.get("registration_id") != expected_registration_id
                or repo.get("index_generation") != expected_generation
            ):
                raise ValueError("Repository registration or generation changed during mutation")

            repo["last_indexed_commit"] = commit
            repo["index_generation"] = uuid.uuid4().hex
            if branch is None:
                branch = repo.get("current_branch")
            if branch:
                repo["last_indexed_branch"] = branch
            repo["last_indexed"] = datetime.now()
            repo["staleness_reason"] = None
            repo["last_sync_error"] = None
            return commit

    def begin_generation_mutation(
        self,
        repository_id: str,
        *,
        expected_registration_id: Optional[str],
        expected_generation: Optional[str],
        require_auto_sync: bool = False,
    ) -> None:
        """Fence reads only if the writer still owns its admitted registration."""
        with self._transaction(write=True):
            repo = self._registry.get(repository_id)
            if repo is None or (
                repo.get("registration_id") != expected_registration_id
                or repo.get("index_generation") != expected_generation
            ):
                raise ValueError("Repository registration or generation changed before mutation")
            if require_auto_sync and (
                not repo.get("auto_sync", True) or not repo.get("active", True)
            ):
                raise ValueError("Automatic sync disabled before mutation")
            repo["staleness_reason"] = "index_publication_pending"

    def fail_generation_mutation(
        self,
        repository_id: str,
        *,
        error: str,
        expected_registration_id: Optional[str],
        expected_generation: Optional[str],
    ) -> bool:
        """Record a failed generation only while its admitted owner is current."""
        with self._transaction(write=True):
            repo = self._registry.get(repository_id)
            if repo is None or (
                repo.get("registration_id") != expected_registration_id
                or repo.get("index_generation") != expected_generation
            ):
                return False
            repo.update(staleness_reason="partial_index_failure", last_sync_error=error)
            return True

    def publish_generation(
        self,
        repository_id: str,
        *,
        generation: str,
        index_path: Path,
        commit: str,
        branch: str,
        profile: Optional[str],
        expected_registration_id: Optional[str],
        expected_generation: Optional[str],
    ) -> None:
        """Atomically bind a validated physical index and its provenance."""
        with self._transaction(write=True):
            repo = self._registry.get(repository_id)
            if repo is None or (
                repo.get("registration_id") != expected_registration_id
                or repo.get("index_generation") != expected_generation
            ):
                raise ValueError("Repository registration or generation changed during publication")
            repo.update(
                index_generation=generation,
                index_path=Path(index_path),
                index_profile=profile,
                last_indexed_commit=commit,
                last_indexed_branch=branch,
                last_indexed=datetime.now(),
                staleness_reason=None,
                last_sync_error=None,
            )

    def get_repositories_needing_update(self) -> List[Tuple[str, Any]]:
        """
        Return repositories where the current commit differs from the last indexed commit.

        Returns:
            List of tuples containing repository ID and RepositoryInfo.
        """
        stale: List[Tuple[str, Any]] = []
        with self._transaction():
            for repo_id, repo_data in self._registry.items():
                repo_info = self._dict_to_repo_info(deepcopy(repo_data))
                if repo_info.needs_update():
                    stale.append((repo_id, repo_info))
        return stale

    def _get_git_commit(self, repo_path: Path) -> Optional[str]:
        """Return the HEAD commit SHA for a repository path."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as exc:
            logger.error(f"Failed to read git commit for {repo_path}: {exc}")
        except FileNotFoundError:
            logger.error("Git is not installed or not available in PATH")
        return None

    def _get_git_branch(self, repo_path: Path) -> Optional[str]:
        """Return the current branch name for a repository path."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return None
        except FileNotFoundError:
            return None

    def _get_git_remote(self, repo_path: Path) -> Optional[str]:
        """Return origin remote URL for a repository path."""
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return None
        except FileNotFoundError:
            return None

    def _list_git_branches(self, repo_path: Path) -> List[str]:
        """Return local git branch names."""
        try:
            result = subprocess.run(
                ["git", "branch", "--format=%(refname:short)"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
            )
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except Exception:
            return []

    def _get_preferred_branch(self, repo_path: Path) -> Optional[str]:
        """Return preferred baseline branch when available."""
        identity = compute_repo_id(repo_path)
        branch = resolve_tracked_branch(identity.git_common_dir)
        return branch if branch else None

    def discover_repositories(self, search_paths: List[str]) -> List[str]:
        """Discover git repositories under the given search paths.

        Returns a list of repository root paths (as strings).
        """
        found = []
        for search_root in search_paths:
            root = Path(search_root)
            if not root.exists():
                continue
            for git_dir in root.rglob(".git"):
                if git_dir.is_dir():
                    found.append(str(git_dir.parent))
        return found

    def update_git_state(self, repository_id: str) -> Optional[Dict[str, str]]:
        """Refresh both current commit and branch for a repository."""
        with self._transaction():
            repo = self._registry.get(repository_id)
            if not repo:
                return None
            repo_path = Path(repo["path"])

            registration_id = repo.get("registration_id")

        commit = self._get_git_commit(repo_path)
        branch = self._get_git_branch(repo_path)

        if not commit and not branch:
            return None

        with self._transaction(write=True):
            repo = self._registry.get(repository_id)
            if (
                not repo
                or Path(repo["path"]) != repo_path
                or repo.get("registration_id") != registration_id
            ):
                return None
            if commit:
                repo["current_commit"] = commit
            if branch:
                repo["current_branch"] = branch

        return {
            "commit": commit or "",
            "branch": branch or "",
        }

    def find_by_path(self, path: Path) -> Optional[str]:
        """
        Find repository ID by path.

        Args:
            path: Repository path

        Returns:
            Repository ID or None if not found
        """
        with self._transaction():
            path_str = str(path.resolve())

            for repo_id, repo_data in self._registry.items():
                repo_path = repo_data.get("path")
                if repo_path is None:
                    continue
                if isinstance(repo_path, str):
                    repo_path = Path(repo_path)

                if str(Path(repo_path).resolve()) == path_str:
                    return repo_id

            return None

    def find_by_git_common_dir(self, git_common_dir: Path) -> Optional[str]:
        """Find a repository ID by normalized git common directory."""
        target = Path(git_common_dir).resolve(strict=False)
        with self._transaction():
            for repo_id, repo_data in self._registry.items():
                stored = repo_data.get("git_common_dir")
                if not stored:
                    continue
                if Path(stored).resolve(strict=False) == target:
                    return repo_id
        return None

    def find_unsupported_worktree(self, path: Path) -> Optional[Any]:
        """Return registered repo info when *path* is another worktree of it."""
        try:
            requested = Path(path).resolve(strict=False)
            identity = compute_repo_id(requested)
        except Exception:
            return None
        if identity.git_common_dir is None:
            return None
        repo_id = self.find_by_git_common_dir(identity.git_common_dir)
        if repo_id is None:
            return None
        repo_info = self.get(repo_id)
        if repo_info is None:
            return None
        if Path(repo_info.path).resolve(strict=False) == requested:
            return None
        return repo_info

    def get_statistics(self) -> Dict[str, Any]:
        """Get registry statistics."""
        with self._transaction():
            total = len(self._registry)
            active = sum(1 for r in self._registry.values() if r.get("active", True))

            # Language distribution
            all_languages = {}
            total_files = 0
            total_symbols = 0

            for repo in self._registry.values():
                lang_stats = repo.get("language_stats", {})
                for lang, count in lang_stats.items():
                    all_languages[lang] = all_languages.get(lang, 0) + count

                total_files += repo.get("total_files", 0)
                total_symbols += repo.get("total_symbols", 0)

            return {
                "total_repositories": total,
                "active_repositories": active,
                "inactive_repositories": total - active,
                "total_files": total_files,
                "total_symbols": total_symbols,
                "languages": all_languages,
                "registry_size_bytes": (
                    self.registry_path.stat().st_size if self.registry_path.exists() else 0
                ),
            }

    def cleanup(self):
        """Clean up invalid or missing repositories."""
        with self._transaction(write=True):
            to_remove = []

            for repo_id, repo_data in self._registry.items():
                # Check if paths still exist
                index_path = repo_data.get("index_path")
                if isinstance(index_path, str):
                    index_path = Path(index_path)

                if not index_path or not index_path.exists():
                    to_remove.append(repo_id)
                    logger.warning(f"Repository {repo_id} has missing index, marking for removal")

            # Remove invalid entries
            for repo_id in to_remove:
                del self._registry[repo_id]

            if to_remove:
                logger.info(f"Cleaned up {len(to_remove)} invalid repository entries")

            return len(to_remove)
