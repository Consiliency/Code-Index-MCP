"""Per-repo SemanticIndexer cache — replaces SL-0 stub."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Dict

from ..config.settings import get_settings

if TYPE_CHECKING:
    from ..storage.repository_registry import RepositoryRegistry
    from .semantic_indexer import SemanticIndexer


class SemanticIndexerRegistry:
    """Thread-safe registry that constructs and caches per-repo SemanticIndexer instances."""

    def __init__(self, repository_registry: "RepositoryRegistry") -> None:
        self._repo_registry = repository_registry
        self._cache: Dict[str, "SemanticIndexer"] = {}
        self._bindings: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _binding(info) -> tuple:
        from ..storage.store_registry import StoreRegistry

        settings = get_settings()
        profile_key = hashlib.sha256(
            json.dumps(
                [settings.get_semantic_default_profile(), settings.get_semantic_profiles_config()],
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        return (StoreRegistry.binding(info), info.current_commit, info.tracked_branch, profile_key)

    def get(self, repo_id: str) -> "SemanticIndexer":
        """Return the SemanticIndexer for *repo_id*, constructing it on first access.

        Raises KeyError if repo_id is not registered.
        """
        with self._lock:
            repo_info = self._repo_registry.get(repo_id)
            if repo_info is None:
                raise KeyError(repo_id)
            binding = self._binding(repo_info)
            if repo_id in self._cache:
                if self._bindings[repo_id] != binding:
                    raise RuntimeError(
                        "Semantic generation changed; retire the owning runtime before reopening"
                    )
                return self._cache[repo_id]

            from .semantic_indexer import SemanticIndexer

            branch = repo_info.tracked_branch or repo_info.current_branch or "unknown"
            qdrant_path = Path(repo_info.index_location or Path(repo_info.index_path).parent)
            indexer = SemanticIndexer(
                qdrant_path=str(qdrant_path / "semantic_qdrant"),
                repo_identifier=repo_id,
                branch=branch,
                commit=repo_info.current_commit,
                collection=self._collection_name(repo_id, branch, repo_info.current_commit),
            )
            current = self._repo_registry.get(repo_id)
            if current is None or self._binding(current) != binding:
                self._close_indexer(indexer)
                raise RuntimeError("Semantic generation changed during construction")
            self._cache[repo_id] = indexer
            self._bindings[repo_id] = binding
            return indexer

    @staticmethod
    def _collection_name(repo_id: str, branch: str, commit: str | None) -> str:
        repo_part = hashlib.sha256(repo_id.encode()).hexdigest()[:12]
        branch_part = re.sub(r"[^0-9a-zA-Z]+", "_", branch.lower()).strip("_") or "branch"
        commit_part = re.sub(r"[^0-9a-zA-Z]+", "_", (commit or "unknown").lower()).strip("_")
        return f"ci__{repo_part}__{branch_part}__{commit_part[:12] or 'unknown'}"

    def evict(self, repo_id: str) -> bool:
        """Close and remove one cached indexer. Returns True when one existed."""
        with self._lock:
            indexer = self._cache.pop(repo_id, None)
            self._bindings.pop(repo_id, None)
        if indexer is None:
            return False
        self._close_indexer(indexer)
        return True

    @staticmethod
    def _close_indexer(indexer: "SemanticIndexer") -> None:
        try:
            indexer.qdrant.close()
        except Exception:
            pass

    def shutdown(self) -> None:
        """Close all cached indexers."""
        with self._lock:
            indexers = list(self._cache.values())
            self._cache.clear()
            self._bindings.clear()

        for indexer in indexers:
            self._close_indexer(indexer)
