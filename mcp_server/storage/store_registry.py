"""
Thread-safe registry of SQLiteStore instances keyed by repo_id.
"""

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Dict

from mcp_server.core.path_resolver import PathResolver
from mcp_server.storage.connection_pool import ConnectionPool
from mcp_server.storage.repository_registry import RepositoryRegistry
from mcp_server.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class StoreRegistry:
    """Thread-safe registry of SQLiteStore instances keyed by repo_id.

    Idempotent: get(repo_id) returns the same instance for repeated calls.
    Cache dict is protected by threading.Lock (SQLite WAL handles per-
    connection thread-safety separately). Construction of a new SQLiteStore
    happens OUTSIDE the global lock to avoid serializing all cache misses.

    Per-key construction lock: a second dict of per-repo_id locks serializes
    concurrent construction for the SAME key (preventing SQLite migration
    races), while different keys can still construct in parallel.

    Double-check pattern: after acquiring the per-key lock, re-check the
    cache — the first thread inserts and the rest return the cached instance.

    Use for_registry() to construct. __init__ is treated as private.
    """

    def __init__(self, registry: RepositoryRegistry):
        self._registry = registry
        self._cache: Dict[str, SQLiteStore] = {}
        self._lock = threading.Lock()
        self._build_locks: Dict[str, threading.Lock] = {}
        self._epoch = 0

    @classmethod
    def for_registry(cls, registry: RepositoryRegistry) -> "StoreRegistry":
        return cls(registry)

    def _get_build_lock(self, repo_id: str) -> threading.Lock:
        """Return (creating if needed) a per-repo_id construction lock."""
        with self._lock:
            if repo_id not in self._build_locks:
                self._build_locks[repo_id] = threading.Lock()
            return self._build_locks[repo_id]

    @staticmethod
    def binding(info) -> tuple:
        """Logical generation plus physical identity; content writes do not change it."""
        path = Path(info.index_path).resolve()
        try:
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
        except FileNotFoundError:
            identity = None
        return (
            getattr(info, "registration_id", None),
            getattr(info, "index_generation", None),
            getattr(info, "index_profile", None),
            str(Path(info.path).resolve()),
            str(path),
            identity,
        )

    def is_current(self, repo_id: str, store: SQLiteStore) -> bool:
        """Recheck a borrowed generation before returning authoritative results."""
        info = self._registry.get(repo_id)
        return (
            info is not None
            and not info.staleness_reason
            and store.registry_binding == self.binding(info)
        )

    def get(self, repo_id: str) -> SQLiteStore:
        """Return a store bound to the current registration and physical generation."""
        with self._get_build_lock(repo_id):
            info = self._registry.get(repo_id)
            expected = self.binding(info) if info is not None else None
            with self._lock:
                epoch = self._epoch
                cached = self._cache.get(repo_id)
                if cached is not None and cached.registry_binding == expected:
                    return cached
                retired = self._cache.pop(repo_id, None)
            if retired is not None:
                binding = retired.registry_binding
                replaced = (
                    expected is not None
                    and binding[:-1] == expected[:-1]
                    and (binding[-1] != expected[-1])
                )
                try:
                    if replaced:
                        self._registry.update_staleness_reason(repo_id, "partial_index_failure")
                finally:
                    retired.close()
                if replaced:
                    raise RuntimeError(
                        "Index file replaced outside generation publication; rebuild required"
                    )
            if info is None:
                raise KeyError(f"repo_id {repo_id!r} is not registered")
            index_path = str(info.index_path)
            Path(index_path).parent.mkdir(parents=True, exist_ok=True)
            pool = ConnectionPool(
                factory=lambda p=index_path: sqlite3.connect(p, check_same_thread=False),
                size=4,
            )
            try:
                store = SQLiteStore(index_path, path_resolver=PathResolver(info.path), pool=pool)
                current = self._registry.get(repo_id)
                actual = self.binding(current) if current is not None else None
                if (
                    actual is None
                    or actual[:-1] != expected[:-1]
                    or (expected[-1] is not None and expected[-1] != actual[-1])
                ):
                    raise RuntimeError("Repository generation changed while opening its index")
                store.registry_binding = actual
            except BaseException:
                pool.close_all()
                raise
            with self._lock:
                if self._epoch == epoch:
                    self._cache[repo_id] = store
                else:
                    store.close()
                    raise RuntimeError("StoreRegistry shut down while opening an index")
            return store

    def close(self, repo_id: str) -> None:
        """Close and evict the cached store for repo_id. No-op if absent."""
        # Keep the per-key lock identity stable for concurrent constructors.
        with self._get_build_lock(repo_id):
            with self._lock:
                store = self._cache.pop(repo_id, None)
            if store is not None:
                store.close()

    def shutdown(self) -> None:
        """Close all cached stores and clear the cache."""
        with self._lock:
            self._epoch += 1
            items = list(self._cache.items())
            self._cache.clear()
        for repo_id, store in items:
            try:
                store.close()
            except Exception as exc:
                logger.warning("SQLiteStore.close failed for %s: %s", repo_id, exc)
