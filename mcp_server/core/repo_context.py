"""Per-tool-call repository context (frozen outer, mutable inner reference)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mcp_server.storage.multi_repo_manager import RepositoryInfo

# Real imports so get_type_hints() resolves forward references at runtime.
# Neither class imports repo_context, so there is no circular dependency.
from mcp_server.storage.sqlite_store import SQLiteStore


def index_generation_key(store: SQLiteStore, entry: RepositoryInfo) -> tuple:
    """Cache identity shared by full and lightweight repository contexts."""
    return (
        getattr(store, "registry_binding", None) or (str(getattr(store, "db_path", "")),),
        getattr(entry, "last_indexed_commit", None),
        getattr(entry, "last_indexed_branch", None),
    )


@dataclass(frozen=True)
class RepoContext:
    """Per-tool-call repository context bound to a registry snapshot.

    Fields:
      repo_id:         canonical 16-hex sha256 from compute_repo_id
      sqlite_store:    hydrated from StoreRegistry; non-Optional by contract
      workspace_root:  registered repository root
      tracked_branch:  pinned default branch from RepositoryInfo.tracked_branch
      registry_entry:  copied metadata for this admission; recheck before results
      requested_path:  original caller path resolved for diagnostics
    """

    repo_id: str
    sqlite_store: SQLiteStore
    workspace_root: Path
    tracked_branch: str
    registry_entry: RepositoryInfo
    requested_path: Path | None = None

    @property
    def generation_key(self) -> tuple:
        """Cache identity for the admitted generation and its provenance."""
        return index_generation_key(self.sqlite_store, self.registry_entry)
