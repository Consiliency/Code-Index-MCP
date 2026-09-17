"""Generation-scoped semantic resources with draining borrower leases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterator

from ..config.settings import get_settings

if TYPE_CHECKING:
    from ..core.repo_context import RepoContext
    from ..storage.multi_repo_manager import RepositoryInfo
    from ..storage.repository_registry import RepositoryRegistry
    from .semantic_indexer import SemanticIndexer


@dataclass
class _Entry:
    indexer: "SemanticIndexer"
    borrowers: int = 0
    retired: bool = False
    unscoped: bool = False


class SemanticIndexerRegistry:
    """Thread-safe registry that constructs and caches per-repo SemanticIndexer instances."""

    def __init__(self, repository_registry: "RepositoryRegistry") -> None:
        self._repo_registry = repository_registry
        self._cache: Dict[str, "SemanticIndexer"] = {}
        self._bindings: Dict[str, tuple] = {}
        self._entries: Dict[tuple, _Entry] = {}
        self._lock = threading.Condition(threading.RLock())
        self._closed = False

    @staticmethod
    def _binding(info) -> tuple:
        from ..storage.store_registry import StoreRegistry

        settings = get_settings()
        profile_key = hashlib.sha256(
            json.dumps(
                [
                    settings.get_semantic_default_profile(),
                    settings.get_semantic_profiles_config(),
                    os.environ.get("QDRANT_URL"),
                    os.environ.get("QDRANT_USE_SERVER"),
                ],
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        return (
            StoreRegistry.binding(info),
            info.last_indexed_commit or info.current_commit,
            info.tracked_branch,
            profile_key,
        )

    def _resolve(
        self, repo_id: str, ctx: "RepoContext | None"
    ) -> tuple["RepositoryInfo", tuple, bool]:
        if self._closed:
            raise RuntimeError("Semantic registry is shutting down")
        active = self._repo_registry.get(repo_id)
        if active is None:
            raise KeyError(repo_id)
        info = ctx.registry_entry if ctx is not None else active
        binding = self._binding(info)
        staged = bool(ctx is not None and ctx.staging)
        if ctx is not None:
            if (
                ctx.repo_id != repo_id
                or info.registration_id != active.registration_id
                or Path(ctx.sqlite_store.db_path).resolve() != Path(info.index_path).resolve()
            ):
                raise RuntimeError("Semantic context does not own this generation")
            if staged:
                if not info.index_generation or info.index_generation == active.index_generation:
                    raise RuntimeError("Staging requires an unpublished generation")
            elif binding != self._binding(active):
                raise RuntimeError("Semantic context is no longer current")
        return info, binding, staged

    @classmethod
    def generation_root(cls, info: "RepositoryInfo", binding: tuple | None = None) -> Path:
        """Locate this configured generation without creating files or opening Qdrant."""
        binding = cls._binding(info) if binding is None else binding
        database = Path(info.index_path).resolve()
        generation = info.index_generation or "legacy"
        namespace = hashlib.sha256(
            json.dumps([info.registration_id, generation, info.index_profile, binding[-1]]).encode()
        ).hexdigest()[:32]
        return database.parent / (database.stem + ".semantic") / generation / namespace

    def _construct(
        self, repo_id: str, info: "RepositoryInfo", binding: tuple, ctx: "RepoContext | None"
    ) -> "SemanticIndexer":
        from ..artifacts.semantic_profiles import SemanticProfileRegistry
        from ..core.path_resolver import PathResolver
        from .semantic_indexer import SemanticIndexer

        settings = get_settings()
        profiles = SemanticProfileRegistry.from_raw(
            settings.get_semantic_profiles_config(), settings.get_semantic_default_profile()
        )
        branch = info.tracked_branch or info.current_branch or "unknown"
        commit = info.last_indexed_commit or info.current_commit
        root = self.generation_root(info, binding)
        root.mkdir(parents=True, exist_ok=True)
        server = os.environ.get("QDRANT_URL")
        use_server = os.environ.get("QDRANT_USE_SERVER", "true").lower() not in {"0", "false", "no"}
        indexer = SemanticIndexer(
            qdrant_path=server if server and use_server else str(root / "vectors"),
            metadata_file=str(root / ".index_metadata.json"),
            repo_identifier=repo_id,
            branch=branch,
            commit=commit,
            lineage_id=root.name,
            collection=self._collection_name(repo_id, branch, commit),
            profile_registry=profiles,
            semantic_profile=profiles.default_profile,
            path_resolver=PathResolver(
                info.path,
                source_root=ctx.workspace_root if ctx is not None and ctx.staging else None,
            ),
            sqlite_store=ctx.sqlite_store if ctx is not None else None,
            staging=bool(ctx is not None and ctx.staging),
        )
        try:
            _, current, _ = self._resolve(repo_id, ctx)
            if current != binding:
                raise RuntimeError("Semantic generation changed during construction")
        except Exception:
            self._close_indexer(indexer)
            raise
        return indexer

    def _finish_retirement(self, key: tuple, entry: _Entry) -> None:
        if entry.borrowers:
            return
        self._close_indexer(entry.indexer)
        self._entries.pop(key)
        repo_id = key[0]
        if self._cache.get(repo_id) is entry.indexer:
            self._cache.pop(repo_id, None)
            self._bindings.pop(repo_id, None)

    def get(self, repo_id: str) -> "SemanticIndexer":
        """Legacy unscoped access; runtime queries and writes must use ``lease``."""
        with self._lock:
            info, binding, staged = self._resolve(repo_id, None)
            key = (repo_id, binding, staged)
            entry = self._entries.get(key)
            if entry is not None and entry.retired:
                raise RuntimeError("Semantic generation is draining")
            if repo_id in self._cache:
                if self._bindings[repo_id] != binding:
                    raise RuntimeError(
                        "Semantic generation changed; retire the owning runtime before reopening"
                    )
                return self._cache[repo_id]

            if entry is None:
                entry = _Entry(self._construct(repo_id, info, binding, None))
                self._entries[key] = entry
            entry.unscoped = True
            self._cache[repo_id] = entry.indexer
            self._bindings[repo_id] = binding
            return entry.indexer

    @contextmanager
    def lease(
        self, repo_id: str, *, ctx: "RepoContext | None" = None
    ) -> Iterator["SemanticIndexer"]:
        """Borrow exactly the supplied generation until the context exits."""
        with self._lock:
            info, binding, staged = self._resolve(repo_id, ctx)
            key = (repo_id, binding, staged)
            for old_key, old in list(self._entries.items()):
                if old_key[0] == repo_id and old_key[2] == staged and old_key != key:
                    if old.unscoped:
                        raise RuntimeError("Unscoped semantic owner must retire before refresh")
                    old.retired = True
                    self._finish_retirement(old_key, old)
            entry = self._entries.get(key)
            if entry is not None and entry.retired:
                raise RuntimeError("Semantic generation is draining")
            if entry is None:
                entry = _Entry(self._construct(repo_id, info, binding, ctx))
                self._entries[key] = entry
            elif ctx is not None and entry.indexer.sqlite_store is not ctx.sqlite_store:
                raise RuntimeError("Semantic owner is bound to a different SQLite handle")
            entry.borrowers += 1
        try:
            yield entry.indexer
        finally:
            with self._lock:
                entry.borrowers -= 1
                try:
                    if entry.retired:
                        self._finish_retirement(key, entry)
                finally:
                    self._lock.notify_all()

    @staticmethod
    def _collection_name(repo_id: str, branch: str, commit: str | None) -> str:
        repo_part = hashlib.sha256(repo_id.encode()).hexdigest()[:12]
        branch_part = re.sub(r"[^0-9a-zA-Z]+", "_", branch.lower()).strip("_") or "branch"
        commit_part = re.sub(r"[^0-9a-zA-Z]+", "_", (commit or "unknown").lower()).strip("_")
        return f"ci__{repo_part}__{branch_part}__{commit_part[:12] or 'unknown'}"

    def evict(self, repo_id: str, *, expected_owner=None) -> bool:
        """Deny new leases; close retired resources after their last borrower."""
        from ..storage.store_registry import StoreRegistry

        expected = StoreRegistry.binding(expected_owner)[:5] if expected_owner is not None else None
        with self._lock:
            entries = [
                (key, entry)
                for key, entry in self._entries.items()
                if key[0] == repo_id and (expected is None or key[1][0][:5] == expected)
            ]
            for _, entry in entries:
                entry.retired = True
            for key, entry in entries:
                self._finish_retirement(key, entry)
            return bool(entries)

    @staticmethod
    def _close_indexer(indexer: "SemanticIndexer") -> None:
        try:
            indexer.qdrant.close()
        except Exception:
            raise RuntimeError("Semantic resource close failed") from None

    def shutdown(self) -> None:
        """Stop admission, drain borrowers and close all owned resources."""
        with self._lock:
            self._closed = True
            for entry in self._entries.values():
                entry.retired = True
            while any(entry.borrowers for entry in self._entries.values()):
                self._lock.wait()
            for key, entry in list(self._entries.items()):
                self._finish_retirement(key, entry)
