"""CHUNKERSAFE Lane A - scheme-mismatch recovery is NOT a dead-end.

The Lane A guard makes a chunker-scheme-mismatched (or mid-rebuild) index report
readiness ``SCHEME_MISMATCH`` / ``INDEX_REBUILDING`` and fail closed on
reads/writes.  The review panel found the recovery path was broken: readiness
told the operator to "run reindex", but reindex/rebuild REFUSED those exact
states, so a tripped index was a permanent dead-end.

These tests pin the recovery contract that was missing when the dead-end shipped:

* the two tripped states are recoverable through both the ``handle_reindex``
  guard set and the staged full-rebuild guard set (regression guard on the fix);
* the REAL staged full-rebuild tool path (``git_index_manager.rebuild_repository_index``
  -- what ``handle_reindex`` invokes) actually runs for a ``SCHEME_MISMATCH``
  index, quarantines the old bytes, and republishes a clean single-scheme index
  stamped with the CURRENT scheme, ready, with no dangling summaries/points; and
* the in-place ``begin_chunk_scheme_rebuild`` -> reindex -> ``finalize`` path
  preserves synthetic history/document rows, drops the chunker-derived
  summaries/semantic points (no dangling rows), and returns the stale Qdrant
  point ids a caller feeds to ``delete_stale_vectors`` /
  ``cleanup_stale_semantic_artifacts``.
"""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mcp_server.cli import tool_handlers as tool_handlers_module
from mcp_server.gateway import _RECOVERABLE_REINDEX_STATES as GATEWAY_RECOVERABLE
from mcp_server.health import repository_readiness
from mcp_server.health.repository_readiness import (
    ReadinessClassifier,
    RepositoryReadinessState,
)
from mcp_server.storage.git_index_manager import (
    _QUARANTINE_REBUILD_STATES,
    _RECOVERABLE_REBUILD_STATES,
    GitAwareIndexManager,
)
from mcp_server.storage.multi_repo_manager import RepositoryInfo
from mcp_server.storage.sqlite_store import (
    CHUNK_SCHEME_MARKER_KEY,
    ChunkSchemeMismatchError,
    SQLiteStore,
    assert_chunk_scheme_readable,
    current_chunk_id_scheme,
    evaluate_chunk_scheme,
)

TOOL_RECOVERABLE = tool_handlers_module._RECOVERABLE_REINDEX_STATES

# A scheme value the installed chunker will never emit -> forces a mismatch.
FOREIGN_SCHEME = "treesitter_chunk_id_recovery_foreign"


# --------------------------------------------------------------------------- #
# Seed helpers (active index built under one scheme, with dependent artifacts)
# --------------------------------------------------------------------------- #
def _store_code_chunk(store: SQLiteStore, file_id: int, chunk_id: str, index: int = 0) -> None:
    store.store_chunk(
        file_id=file_id,
        content=f"def f_{index}():\n    return {index}\n",
        content_start=0,
        content_end=20,
        line_start=1,
        line_end=2,
        chunk_id=chunk_id,
        node_id=f"node-{chunk_id}",
        treesitter_file_id="tsfile",
        language="python",
        chunk_index=index,
    )


def _store_document_chunk(store: SQLiteStore, file_id: int, chunk_id: str) -> None:
    """Synthetic (non-chunker) history/document row that must survive a rebuild."""
    store.store_chunk(
        file_id=file_id,
        content="# history document\n",
        content_start=0,
        content_end=18,
        line_start=1,
        line_end=1,
        chunk_id=chunk_id,
        node_id=chunk_id,
        treesitter_file_id="history",
        chunk_type="document",
        language="history",
    )


def _insert_summary(store: SQLiteStore, chunk_hash: str, file_id: int) -> None:
    with store._get_connection() as conn:
        conn.execute(
            "INSERT INTO chunk_summaries (chunk_hash, file_id, chunk_start, chunk_end, "
            "summary_text) VALUES (?, ?, 0, 1, ?)",
            (chunk_hash, file_id, f"summary of {chunk_hash}"),
        )


def _seed_active_index(index_path: Path, repo_path: Path) -> None:
    """Build a code_chunks index (marker stamped to the current scheme) with
    chunk summaries and semantic point mappings, plus a preserved synthetic row.
    """
    store = SQLiteStore(str(index_path))
    repo_row = store.ensure_repository_row(repo_path, name="test-repo")
    code_file = store.store_file(
        repo_row, path=repo_path / "hello.py", relative_path="hello.py", language="python"
    )
    hist_file = store.store_file(
        repo_row, path=repo_path / "history.md", relative_path="history.md", language="history"
    )
    _store_code_chunk(store, code_file, "code-chunk-1", index=0)  # stamps current scheme
    _store_code_chunk(store, code_file, "code-chunk-2", index=1)
    _store_document_chunk(store, hist_file, "history:issue")

    _insert_summary(store, "code-chunk-1", code_file)
    _insert_summary(store, "code-chunk-2", code_file)
    _insert_summary(store, "history:issue", hist_file)

    store.upsert_semantic_point("profile-a", "code-chunk-1", 101, "col-a")
    store.upsert_semantic_point("profile-a", "code-chunk-2", 102, "col-a")
    store.upsert_semantic_point("profile-a", "history:issue", 900, "col-a")

    assert store.get_chunk_scheme_marker() == current_chunk_id_scheme()
    store.close()


def _force_scheme_mismatch(index_path: Path) -> None:
    """Rewrite the persisted marker to a foreign scheme so the index reads as a
    genuine cross-scheme mismatch against the installed chunker."""
    with sqlite3.connect(str(index_path)) as conn:
        conn.execute(
            "UPDATE index_config SET config_value = ? WHERE config_key = ?",
            (FOREIGN_SCHEME, CHUNK_SCHEME_MARKER_KEY),
        )
    ReadinessClassifier.clear_index_inspection_cache()


# --------------------------------------------------------------------------- #
# Git + registry harness for the REAL staged full-rebuild tool path
# --------------------------------------------------------------------------- #
def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True
    )
    (repo / "hello.py").write_text("def hello():\n    return 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def _head_commit(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_repo_info(repo: Path, commit: str) -> RepositoryInfo:
    index_dir = repo.parent / "index"
    index_dir.mkdir(exist_ok=True)
    return RepositoryInfo(
        repository_id="recovery-repo",
        name="test-repo",
        path=repo,
        index_path=index_dir / "current.db",
        language_stats={},
        total_files=1,
        total_symbols=0,
        indexed_at=datetime.now(),
        current_commit=commit,
        last_indexed_commit=commit,
        tracked_branch="main",
        current_branch="main",
    )


class _RecoveryDispatcher:
    """Minimal dispatcher for the staged full rebuild.

    ``_full_index`` only calls ``index_directory``.  It writes a real chunker row
    into the FRESH stage DB (stamping the CURRENT scheme on first write) plus a
    synthetic history/document row, mirroring a full reindex that re-materializes
    both the code chunks and the history/doc stage.
    """

    def index_directory(self, ctx, path, recursive=True):
        store = ctx.sqlite_store
        row_id = store.ensure_repository_row(Path(path), name="test-repo")
        file_id = store.store_file(
            row_id, path=Path(path) / "hello.py", relative_path="hello.py", language="python"
        )
        store.store_chunk(
            file_id=file_id,
            content="def hello():\n    return 1\n",
            content_start=0,
            content_end=24,
            line_start=1,
            line_end=2,
            chunk_id="rebuilt-chunk-1",
            node_id="node-rebuilt-1",
            treesitter_file_id="tsfile",
            language="python",
            chunk_index=0,
        )
        store.store_chunk(
            file_id=file_id,
            content="# history\n",
            content_start=0,
            content_end=10,
            line_start=1,
            line_end=1,
            chunk_id="history:issue",
            node_id="history:issue",
            treesitter_file_id="history",
            chunk_type="document",
            language="history",
        )
        return {"indexed_files": 1, "failed_files": 0, "errors": []}


def _make_manager(repo_info: RepositoryInfo, commit: str):
    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    registry.update_git_state.return_value = {"commit": commit, "branch": "main"}
    registry.update_indexed_commit.return_value = commit

    def _update_staleness(_repo_id, reason):
        repo_info.staleness_reason = reason
        return True

    registry.update_staleness_reason.side_effect = _update_staleness
    manager = GitAwareIndexManager(registry, _RecoveryDispatcher())
    manager._resolve_ctx = MagicMock(return_value=None)
    return manager, registry


# --------------------------------------------------------------------------- #
# 1. Regression guard: the tripped states are recoverable in every guard set
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "state",
    [
        RepositoryReadinessState.SCHEME_MISMATCH,
        RepositoryReadinessState.INDEX_REBUILDING,
    ],
)
def test_tripped_states_are_recoverable_everywhere(state):
    # handle_reindex (CLI) and the HTTP gateway both admit these for recovery.
    assert state in TOOL_RECOVERABLE
    assert state in GATEWAY_RECOVERABLE
    # The staged full rebuild runs for them and preserves the old bytes first.
    assert state in _RECOVERABLE_REBUILD_STATES
    assert state in _QUARANTINE_REBUILD_STATES


# --------------------------------------------------------------------------- #
# 2. E2E: a SCHEME_MISMATCH index is refused, then recovered by the REAL tool
# --------------------------------------------------------------------------- #
def test_scheme_mismatch_is_refused_then_recovered_by_staged_rebuild(tmp_path):
    repo = _make_git_repo(tmp_path)
    commit = _head_commit(repo)
    repo_info = _make_repo_info(repo, commit)
    index_path = repo_info.index_path

    _seed_active_index(index_path, repo)
    _force_scheme_mismatch(index_path)

    # --- readiness reports the tripped state (not a healthy 'ready') ---
    readiness = ReadinessClassifier.classify_registered(repo_info)
    assert readiness.state == RepositoryReadinessState.SCHEME_MISMATCH
    assert not readiness.ready
    assert "reindex" in (readiness.remediation or "").lower()

    # --- reads AND writes fail closed under the mismatch ---
    with sqlite3.connect(str(index_path)) as raw:
        with pytest.raises(ChunkSchemeMismatchError):
            assert_chunk_scheme_readable(raw)
    guarded = SQLiteStore(str(index_path))
    with pytest.raises(ChunkSchemeMismatchError):
        _store_code_chunk(guarded, 1, "should-not-write")
    guarded.close()

    # --- the readiness remediation is now actionable (the dead-end fix) ---
    assert readiness.state in _RECOVERABLE_REBUILD_STATES

    # --- invoke the REAL reindex/rebuild tool path ---
    manager, registry = _make_manager(repo_info, commit)
    result = manager.rebuild_repository_index(repo_info.repository_id)

    assert result.action == "full_index", result.error
    # It ran *because* of the mismatch, and preserved the old bytes for forensics.
    assert result.readiness["previous_state"] == "scheme_mismatch"
    quarantine_path = Path(result.readiness["quarantine_path"])
    assert quarantine_path.exists()

    # --- the republished index is clean, ready, and single-scheme (current) ---
    ReadinessClassifier.clear_index_inspection_cache()
    assert ReadinessClassifier.classify_registered(repo_info).ready is True

    assert repo_info.index_path != index_path
    index_path = repo_info.index_path
    recovered = SQLiteStore(str(index_path))
    assert recovered.get_chunk_scheme_marker() == current_chunk_id_scheme()
    status, marker, target = recovered.get_chunk_scheme_status()
    assert status == "compatible"
    assert marker == target == current_chunk_id_scheme()
    recovered.close()

    with sqlite3.connect(str(index_path)) as conn:
        chunk_ids = {r[0] for r in conn.execute("SELECT chunk_id FROM code_chunks")}
        summary_hashes = {r[0] for r in conn.execute("SELECT chunk_hash FROM chunk_summaries")}
        point_chunk_ids = {r[0] for r in conn.execute("SELECT chunk_id FROM semantic_points")}

    # Fresh single-scheme rows only; the stale old-scheme chunk ids are gone.
    assert "rebuilt-chunk-1" in chunk_ids
    assert "code-chunk-1" not in chunk_ids and "code-chunk-2" not in chunk_ids
    # Synthetic history/document row is present after recovery.
    assert "history:issue" in chunk_ids
    # No dangling summaries / semantic points survived the republish.
    assert summary_hashes <= chunk_ids
    assert point_chunk_ids <= chunk_ids
    assert "code-chunk-1" not in summary_hashes and "code-chunk-1" not in point_chunk_ids

    # The recovered index reads without tripping the guard.
    with sqlite3.connect(str(index_path)) as raw:
        assert_chunk_scheme_readable(raw)  # does not raise

    # Provenance was recorded for the rebuilt commit.
    registry.publish_generation.assert_called_once()
    assert registry.publish_generation.call_args.kwargs["commit"] == commit
    assert registry.publish_generation.call_args.kwargs["index_path"] == index_path


# --------------------------------------------------------------------------- #
# 3. In-place rebuild: preserve synthetic rows, drop chunker artifacts, return
#    the stale Qdrant point ids for delete_stale_vectors/cleanup.
# --------------------------------------------------------------------------- #
def test_inplace_rebuild_preserves_synthetic_and_returns_stale_vector_ids(tmp_path):
    index_path = tmp_path / "code_index.db"
    store = SQLiteStore(str(index_path))
    repo_row = store.ensure_repository_row(tmp_path, name="test-repo")
    code_file = store.store_file(
        repo_row, path=tmp_path / "a.py", relative_path="a.py", language="python"
    )
    hist_file = store.store_file(
        repo_row, path=tmp_path / "h.md", relative_path="h.md", language="history"
    )
    _store_code_chunk(store, code_file, "code-chunk-1", index=0)
    _store_code_chunk(store, code_file, "code-chunk-2", index=1)
    _store_document_chunk(store, hist_file, "history:issue")
    _insert_summary(store, "code-chunk-1", code_file)
    _insert_summary(store, "code-chunk-2", code_file)
    _insert_summary(store, "history:issue", hist_file)
    store.upsert_semantic_point("profile-a", "code-chunk-1", 101, "col-a")
    store.upsert_semantic_point("profile-a", "code-chunk-2", 102, "col-a")
    store.upsert_semantic_point("profile-a", "history:issue", 900, "col-a")

    # Phase 1: invalidate + enter the blocked, resumable 'rebuilding' state.
    stale = store.begin_chunk_scheme_rebuild(profile_id="profile-a")

    # The stale remote-vector ids handed to delete_stale_vectors /
    # cleanup_stale_semantic_artifacts cover the chunker points only.
    assert set(stale["chunk_ids"]) == {"code-chunk-1", "code-chunk-2"}
    assert {p["point_id"] for p in stale["points"]} == {101, 102}
    assert all(p["chunk_id"].startswith("code-chunk") for p in stale["points"])

    # While rebuilding, readiness is non-ready and reads fail closed.
    state = repository_readiness._inspect_index_uncached(index_path)
    assert state == RepositoryReadinessState.INDEX_REBUILDING
    with sqlite3.connect(str(index_path)) as raw:
        with pytest.raises(ChunkSchemeMismatchError):
            assert_chunk_scheme_readable(raw)

    # Phase 2: repopulate under the rebuild target, then finalize.
    _store_code_chunk(store, code_file, "code-chunk-1-v2", index=0)
    store.finalize_chunk_scheme_rebuild()

    # Recovered: ready, current marker, chunker artifacts gone, synthetic kept.
    assert repository_readiness._inspect_index_uncached(index_path) is None
    assert store.get_chunk_scheme_marker() == current_chunk_id_scheme()
    with store._get_connection() as conn:
        chunk_ids = {r[0] for r in conn.execute("SELECT chunk_id FROM code_chunks")}
        summary_hashes = {r[0] for r in conn.execute("SELECT chunk_hash FROM chunk_summaries")}
        point_ids = {r[0] for r in conn.execute("SELECT point_id FROM semantic_points")}
        status, _marker, _target = evaluate_chunk_scheme(conn)

    assert status == "compatible"
    assert "code-chunk-1-v2" in chunk_ids
    assert "code-chunk-1" not in chunk_ids and "code-chunk-2" not in chunk_ids
    # Synthetic history/document row + its dependent artifacts survive untouched.
    assert "history:issue" in chunk_ids
    assert summary_hashes == {"history:issue"}  # chunker summaries dropped, no dangling
    assert point_ids == {900}  # chunker point mappings dropped, no dangling
    store.close()


# --------------------------------------------------------------------------- #
# Production drain of the pending_vector_deletions crash-ledger (I4)
#
# A crashed chunk-scheme rebuild deletes local semantic_points but leaves the
# orphaned REMOTE (Qdrant) point ids only in the durable ledger. The reindex/
# startup drain finishes that remote cleanup and clears the drained rows.
# --------------------------------------------------------------------------- #
class _FakeVectorClient:
    """Records remote-delete calls; can fail selected collections (per-profile)."""

    def __init__(self, fail_collections=frozenset()):
        self.calls = []  # list of (collection, sorted point_ids)
        self._fail = set(fail_collections)

    def delete_remote_points(self, point_ids, *, collection=None):
        self.calls.append((collection, sorted(point_ids)))
        if collection in self._fail:
            raise RuntimeError(f"Qdrant unavailable for collection {collection!r}")
        return len(point_ids)


def _drain_callback(client):
    return lambda collection, point_ids: client.delete_remote_points(
        point_ids, collection=collection
    )


def _seed_ledger(store, points):
    """Insert crash-ledger rows exactly as a mid-rebuild crash would leave them."""
    with store._get_connection() as conn:
        store._record_pending_vector_deletions(conn, points)


def test_drain_deletes_recorded_orphans_and_clears_rows(tmp_path):
    """(a) A ledger of recorded orphans -> drain deletes exactly those remote point
    ids (per collection) and clears the ledger."""
    store = SQLiteStore(str(tmp_path / "code_index.db"))
    _seed_ledger(
        store,
        [
            {"profile_id": "profile-a", "chunk_id": "c1", "point_id": 101, "collection": "col-a"},
            {"profile_id": "profile-a", "chunk_id": "c2", "point_id": 102, "collection": "col-a"},
        ],
    )

    client = _FakeVectorClient()
    result = store.drain_pending_vector_deletions(_drain_callback(client))

    assert client.calls == [("col-a", [101, 102])]
    assert result == {"rows_drained": 2, "rows_remaining": 0, "groups_failed": 0}
    assert store.get_pending_vector_deletions() == []
    store.close()


def test_drain_is_fail_safe_per_profile(tmp_path):
    """(b) A remote-delete failure for one profile/collection leaves ITS rows in the
    ledger for a later attempt, still clears the healthy profile's rows, and the
    drain call itself never raises."""
    store = SQLiteStore(str(tmp_path / "code_index.db"))
    _seed_ledger(
        store,
        [
            {"profile_id": "profile-a", "chunk_id": "a1", "point_id": 101, "collection": "col-a"},
            {"profile_id": "profile-a", "chunk_id": "a2", "point_id": 102, "collection": "col-a"},
            {"profile_id": "profile-b", "chunk_id": "b1", "point_id": 201, "collection": "col-b"},
        ],
    )

    client = _FakeVectorClient(fail_collections={"col-b"})
    # Must not raise despite the remote failure for col-b.
    result = store.drain_pending_vector_deletions(_drain_callback(client))

    assert result["groups_failed"] == 1
    assert result["rows_drained"] == 2
    assert result["rows_remaining"] == 1

    # Healthy profile drained; failed profile's rows REMAIN for the next attempt.
    remaining = store.get_pending_vector_deletions()
    assert {row["point_id"] for row in remaining} == {201}
    assert {row["profile_id"] for row in remaining} == {"profile-b"}

    # Second attempt with a now-healthy client finishes the deferred delete.
    healthy = _FakeVectorClient()
    result2 = store.drain_pending_vector_deletions(_drain_callback(healthy))
    assert healthy.calls == [("col-b", [201])]
    assert result2["rows_drained"] == 1
    assert store.get_pending_vector_deletions() == []
    store.close()


def test_drain_empty_ledger_is_noop(tmp_path):
    """(c) An empty ledger is a no-op: no remote calls, nothing cleared."""
    store = SQLiteStore(str(tmp_path / "code_index.db"))
    client = _FakeVectorClient()
    result = store.drain_pending_vector_deletions(_drain_callback(client))
    assert client.calls == []
    assert result == {"rows_drained": 0, "rows_remaining": 0, "groups_failed": 0}
    store.close()


def test_drain_is_idempotent_on_second_run(tmp_path):
    """(d) A successful drain followed by a re-run is a no-op (idempotent)."""
    store = SQLiteStore(str(tmp_path / "code_index.db"))
    _seed_ledger(
        store,
        [{"profile_id": "profile-a", "chunk_id": "c1", "point_id": 101, "collection": "col-a"}],
    )
    client = _FakeVectorClient()
    store.drain_pending_vector_deletions(_drain_callback(client))
    assert store.get_pending_vector_deletions() == []

    # Re-run: ledger already empty, so no further remote calls happen.
    client2 = _FakeVectorClient()
    result2 = store.drain_pending_vector_deletions(_drain_callback(client2))
    assert client2.calls == []
    assert result2 == {"rows_drained": 0, "rows_remaining": 0, "groups_failed": 0}
    store.close()


def test_dispatcher_drain_wiring_noops_without_semantic(tmp_path):
    """The reindex hook (EnhancedDispatcher._drain_pending_vector_deletions) drains
    when a semantic client is present and no-ops cleanly when it is not - without
    constructing a full dispatcher."""
    from contextlib import nullcontext

    from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher

    store = SQLiteStore(str(tmp_path / "code_index.db"))
    _seed_ledger(
        store,
        [{"profile_id": "profile-a", "chunk_id": "c1", "point_id": 101, "collection": "col-a"}],
    )

    class _Ctx:
        sqlite_store = store
        staging = False

    ctx = _Ctx()

    # Semantic disabled -> no client -> clean no-op, ledger untouched.
    class _NoSem:
        def _semantic_lease(self, ctx):
            return nullcontext()

        def _get_semantic_indexer(self, ctx):
            return None

    EnhancedDispatcher._drain_pending_vector_deletions(_NoSem(), ctx)
    assert {r["point_id"] for r in store.get_pending_vector_deletions()} == {101}

    # Semantic present -> drains through delete_remote_points, clears the ledger.
    client = _FakeVectorClient()

    class _WithSem(_NoSem):
        def _get_semantic_indexer(self, ctx):
            return client

    EnhancedDispatcher._drain_pending_vector_deletions(_WithSem(), ctx)
    assert client.calls == [("col-a", [101])]
    assert store.get_pending_vector_deletions() == []
    store.close()


def test_drain_skips_revived_point_ids_but_clears_their_ledger_rows(tmp_path):
    """(e) Finding 1: point ids are deterministic and content-derived, so an
    incremental index pass can legitimately RE-CREATE a ledger point id (fresh
    ``semantic_points`` row + remote vector) BEFORE the drain runs.  Such a revived
    point id is in active use and must NOT be remote deleted - deleting it would
    silently drop a live vector and leave its fresh mapping dangling.  Its ledger
    debt is still cleared (resolved).  A true-orphan point id in the SAME group
    (no live mapping) IS remote deleted."""
    store = SQLiteStore(str(tmp_path / "code_index.db"))
    _seed_ledger(
        store,
        [
            {"profile_id": "profile-a", "chunk_id": "c1", "point_id": 101, "collection": "col-a"},
            {"profile_id": "profile-a", "chunk_id": "c2", "point_id": 102, "collection": "col-a"},
        ],
    )
    # Point 101 was revived by an incremental pass: it has a LIVE semantic_points
    # mapping in the same profile/collection.  Point 102 stays a true orphan.
    store.upsert_semantic_point("profile-a", "c1-revived", 101, "col-a")

    client = _FakeVectorClient()
    result = store.drain_pending_vector_deletions(_drain_callback(client))

    # Only the true orphan (102) is remote deleted; the revived point (101) is
    # SKIPPED so its live remote vector survives.
    assert client.calls == [("col-a", [102])]
    # ... but BOTH ledger rows are cleared (debt resolved either way).
    assert result == {"rows_drained": 2, "rows_remaining": 0, "groups_failed": 0}
    assert store.get_pending_vector_deletions() == []
    # The live mapping is untouched.
    assert store.get_semantic_point_ids("profile-a", ["c1-revived"]) == [101]
    store.close()


def test_delete_remote_points_error_does_not_trip_upsert_circuit_breaker(tmp_path):
    """(f) Finding 2: a remote-delete failure in ``delete_remote_points`` (the
    ledger-drain remote hook) must RAISE (so the ledger row survives) WITHOUT
    setting ``_qdrant_available = False``.  That flag is the UPSERT path's circuit
    breaker; because the drain runs at the FRONT of every reindex against the
    shared cached indexer, a transient delete blip must not degrade indexing for
    the rest of the run."""
    from types import SimpleNamespace  # noqa: F401

    from mcp_server.core.path_resolver import PathResolver
    from mcp_server.utils.semantic_indexer import SemanticIndexer

    class _FailingDeleteQdrant:
        def __init__(self):
            self.upserted = []

        def delete(self, collection_name, points_selector):
            raise RuntimeError("transient Qdrant blip during delete")

        def upsert(self, collection_name, points):
            self.upserted.append((collection_name, points))

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    qdrant = _FailingDeleteQdrant()
    indexer = SemanticIndexer.__new__(SemanticIndexer)
    indexer.qdrant = qdrant
    indexer.collection = "code-index"
    indexer._qdrant_available = True
    indexer.path_resolver = PathResolver(repo_path)

    with pytest.raises(RuntimeError, match="Failed to delete remote points"):
        indexer.delete_remote_points([101, 102], collection="col-a")

    # The DELETE failure must NOT flip the upsert circuit breaker.
    assert indexer._qdrant_available is True

    # Upserts remain available after the delete failure (the run is not degraded).
    indexer.qdrant.upsert(collection_name="col-a", points=["p"])
    assert qdrant.upserted == [("col-a", ["p"])]


def test_delete_during_rebuild_is_symmetric_with_insert(tmp_path):
    """Cleanup item 3: a cross-scheme DELETE during the rebuild window is admitted
    or refused by exactly the same rule as the corresponding INSERT.

    Before the fix ``_assert_chunk_scheme_deletable`` had no ``rebuilding`` branch,
    so an ordinary cross-scheme writer's INSERT was refused while its DELETE was
    silently admitted -- an asymmetry that let a non-rebuild actor erase old-scheme
    rows the finalize step would then restamp (mixed-scheme corruption). Both choke
    points must now make the SAME admit/refuse decision."""
    store = SQLiteStore(str(tmp_path / "code_index.db"))
    store.ensure_repository_row(tmp_path, name="test-repo")

    rebuild_target = current_chunk_id_scheme()
    other_scheme = FOREIGN_SCHEME  # a scheme this rebuild is NOT building

    # Enter the blocked 'rebuilding' state scoped to rebuild_target.
    store.begin_chunk_scheme_rebuild(target_scheme=rebuild_target)

    with store._get_connection() as conn:
        status, marker, _t = evaluate_chunk_scheme(conn, rebuild_target)
        assert status == "rebuilding" and marker == rebuild_target

        # The rebuild writer (target == marker): both insert and delete admitted.
        store._assert_chunk_scheme_writable(conn, chunk_type="code", target=rebuild_target)
        store._assert_chunk_scheme_deletable(conn, target=rebuild_target)

        # An ordinary cross-scheme actor (target != marker): the insert is refused,
        # and the delete must now be refused symmetrically (previously admitted).
        with pytest.raises(ChunkSchemeMismatchError) as insert_exc:
            store._assert_chunk_scheme_writable(conn, chunk_type="code", target=other_scheme)
        with pytest.raises(ChunkSchemeMismatchError) as delete_exc:
            store._assert_chunk_scheme_deletable(conn, target=other_scheme)

        # Same admit/refuse decision AND same state classification on both paths.
        assert insert_exc.value.state == delete_exc.value.state == "rebuilding"

    store.close()
