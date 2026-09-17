"""Resolves filesystem paths to RepoContext via the RepositoryRegistry."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional, TypeVar

from mcp_server.core.repo_context import RepoContext
from mcp_server.health.repository_readiness import (
    ReadinessClassifier,
    RepositoryReadiness,
    RepositoryReadinessState,
)
from mcp_server.indexing.lock_registry import lock_registry
from mcp_server.storage.repository_registry import RepositoryRegistry
from mcp_server.storage.store_registry import StoreRegistry

logger = logging.getLogger(__name__)
_MutationResult = TypeVar("_MutationResult")


class RepositoryMutationCancelled(RuntimeError):
    """A cancelled staged mutation that was not published."""

    def __init__(self, outcome: dict):
        super().__init__("Staged mutation cancelled without publication")
        self.outcome = {**outcome, "mutation_performed": False}


def run_repository_mutation(resolver, ctx, operation: Callable):
    """Share writer admission across synchronous and task-backed entrypoints."""
    if ctx is None:
        return operation(ctx)
    with lock_registry.acquire(ctx.repo_id, repo_path=ctx.workspace_root):
        if isinstance(resolver, RepoResolver):
            return resolver.mutate(ctx, operation)
        return operation(ctx)


def _find_git_root(start: Path) -> Optional[Path]:
    """Walk up from `start` to find the nearest directory containing `.git`
    (file for worktrees, dir for main clones). Returns None if none found."""
    current = start.expanduser().resolve()
    if current.is_file():
        current = current.parent
    while True:
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


class RepoResolver:
    """Resolves arbitrary selectors to a RepoContext, or None when
    the selector is outside any registered repository. Does NOT auto-register."""

    def __init__(self, registry: RepositoryRegistry, store_registry: StoreRegistry):
        self._registry = registry
        self._store_registry = store_registry
        self._index_manager = None

    def classify(self, selector: str | Path) -> RepositoryReadiness:
        """Classify repository readiness for a selector without registering it."""
        return ReadinessClassifier.classify_path(self._registry, selector)

    def resolve(self, selector: str | Path) -> Optional[RepoContext]:
        readiness = self.classify(selector)
        if readiness.state in {
            RepositoryReadinessState.UNREGISTERED_REPOSITORY,
            RepositoryReadinessState.UNSUPPORTED_WORKTREE,
            RepositoryReadinessState.AMBIGUOUS_SELECTOR,
        }:
            return None

        return self._context_from_readiness(readiness)

    def resolve_ready(self, selector: str | Path) -> Optional[RepoContext]:
        """Resolve only when this operation's own classification is ready."""
        readiness = self.classify(selector)
        if not readiness.ready:
            return None
        ctx = self._context_from_readiness(readiness)
        return ctx if ctx is not None and self.is_current(ctx) else None

    def is_current(self, ctx: RepoContext) -> bool:
        """Fail closed when a query crosses a registration or generation transition."""
        try:
            info = self._registry.get(ctx.repo_id)
            if info is None or not self._store_registry.is_current(ctx.repo_id, ctx.sqlite_store):
                return False
            if (
                info.last_indexed_commit != ctx.registry_entry.last_indexed_commit
                or info.last_indexed_branch != ctx.registry_entry.last_indexed_branch
                or self._store_registry.binding(info)
                != self._store_registry.binding(ctx.registry_entry)
            ):
                return False
            return ReadinessClassifier.classify_registered(info).ready
        except Exception:
            return False

    def mutate(
        self, ctx: RepoContext, operation: Callable[[RepoContext], _MutationResult]
    ) -> _MutationResult:
        """Run a scoped mutation in the same staged boundary as full rebuilds."""
        with lock_registry.acquire(ctx.repo_id, repo_path=ctx.workspace_root):
            if not self.is_current(ctx):
                raise RuntimeError("Repository generation changed before mutation")
            current = self.resolve_ready(ctx.workspace_root)
            if current is None or current.generation_key != ctx.generation_key:
                raise RuntimeError("Repository became unavailable before mutation")
            if self._index_manager is None:
                raise RuntimeError("Staged repository mutation owner is unavailable")
            from mcp_server.storage.git_index_manager import UpdateResult

            outcome = []

            def staged_operation(stage):
                result = operation(stage)
                outcome.append(result)
                if isinstance(result, dict):
                    failed = any(
                        result.get(key)
                        for key in (
                            "failed_files",
                            "semantic_failed",
                            "semantic_blocked",
                            "cancelled",
                            "error",
                            "semantic_error",
                            "low_level_blocker",
                        )
                    )
                    indexed = result.get("indexed_files", 0)
                else:
                    status = getattr(result, "status", None)
                    failed = status not in {"indexed", "deleted", "moved", "skipped_unchanged"}
                    indexed = int(status == "indexed")
                    semantic = getattr(result, "semantic", None) or {}
                    failed = failed or bool(
                        semantic.get("semantic_failed") or semantic.get("semantic_blocked")
                    )
                return UpdateResult(indexed=indexed, failed=int(bool(failed)))

            sync = self._index_manager._rebuild_repository_index_locked(
                ctx.repo_id, stage_operation=staged_operation
            )
            if sync.action != "full_index":
                if outcome and isinstance(outcome[0], dict) and outcome[0].get("cancelled"):
                    raise RepositoryMutationCancelled(outcome[0])
                raise RuntimeError("Staged mutation did not publish; rebuild required")
            if isinstance(outcome[0], dict):
                outcome[0]["mutation_performed"] = True
            return outcome[0]

    def _context_from_readiness(self, readiness: RepositoryReadiness) -> Optional[RepoContext]:
        repo_id = readiness.repository_id
        if repo_id is None:
            return None

        info = self._registry.get(repo_id)
        if info is None:
            return None

        tracked_branch = info.tracked_branch or ""

        store = self._store_registry.get(repo_id)
        if store.registry_binding != self._store_registry.binding(info):
            return None
        if readiness.requested_path:
            requested_path = Path(readiness.requested_path)
        else:
            requested_path = Path(info.path).expanduser().resolve()

        workspace_root = Path(info.path).expanduser().resolve()
        return RepoContext(
            repo_id=repo_id,
            sqlite_store=store,
            workspace_root=workspace_root,
            tracked_branch=tracked_branch,
            registry_entry=info,
            requested_path=requested_path,
        )
