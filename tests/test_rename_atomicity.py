"""Tests for atomic rename via two_phase_commit wrapper in dispatcher_enhanced.move_file."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from mcp_server.core.errors import IndexingError
from mcp_server.core.repo_context import RepoContext
from mcp_server.dispatcher.dispatcher_enhanced import IndexResultStatus
from mcp_server.storage.sqlite_store import SQLiteStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path) -> SQLiteStore:
    db_path = tmp_path / "idx.db"
    return SQLiteStore(str(db_path))


def _make_ctx(store: SQLiteStore, workspace_root: Path, repo_id: str = "repo-1") -> RepoContext:
    info = Mock()
    info.tracked_branch = "main"
    info.path = workspace_root
    info.name = workspace_root.name
    return RepoContext(
        repo_id=repo_id,
        sqlite_store=store,
        workspace_root=workspace_root,
        tracked_branch="main",
        registry_entry=info,
    )


def _make_dispatcher(store: SQLiteStore):
    """Return a minimal EnhancedDispatcher with SQLite store and no semantic indexer."""
    from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher

    dispatcher = EnhancedDispatcher.__new__(EnhancedDispatcher)
    dispatcher._operation_stats = {}
    dispatcher._legacy_plugins = []
    dispatcher._lazy_load = False
    dispatcher._use_factory = False
    dispatcher._enable_advanced = False
    dispatcher._router = None
    dispatcher._semantic_registry = None
    dispatcher._semantic_indexer_fallback = None
    # Attributes the registered-indexer lazy-build path (added in the roadmap-v8
    # dispatcher refactor) reads via _get_or_build_registered_semantic_indexer().
    # _make_ctx() sets a real registry_entry.path, so _is_registered_context() is
    # True and move_file()/remove_file() reach this path; without these the lookup
    # raises AttributeError before any rename logic runs.
    dispatcher._registered_semantic_indexers = {}
    dispatcher._registered_semantic_indexer_lock = __import__("threading").RLock()
    dispatcher._plugin_set_registry = Mock()
    dispatcher._plugin_set_registry.plugins_for.return_value = []
    dispatcher._file_cache = {}
    dispatcher._file_cache_lock = __import__("threading").RLock()
    return dispatcher


# ---------------------------------------------------------------------------
# test_rename_single_remove_single_add (acceptance #6)
# ---------------------------------------------------------------------------


class TestRenameSingleRemoveSingleAdd:
    """After move_file(foo.py → bar.py) SQLite shows only bar.py."""

    def test_rename_single_remove_single_add(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()

        store = _make_store(tmp_path)
        repo_int_id = store.create_repository(path=str(repo_root), name="testrepo")

        # Seed foo.py in the store using its absolute path inside repo_root
        foo_abs = repo_root / "foo.py"
        foo_abs.write_text("x = 1\n")
        store.store_file(
            path=str(foo_abs),
            relative_path="foo.py",
            language="python",
            repository_id=repo_int_id,
        )

        ctx = _make_ctx(store, workspace_root=repo_root)
        dispatcher = _make_dispatcher(store)

        bar_abs = repo_root / "bar.py"
        bar_abs.write_text("x = 1\n")

        # No semantic indexer — no plugin with _indexer
        result = dispatcher.move_file(ctx, foo_abs, bar_abs, content_hash="abc123")
        assert result.status == IndexResultStatus.MOVED

        with store._get_connection() as conn:
            rows = conn.execute(
                "SELECT relative_path FROM files "
                "WHERE repository_id = ? AND relative_path IN ('foo.py', 'bar.py') "
                "AND (is_deleted = 0 OR is_deleted IS NULL)",
                (repo_int_id,),
            ).fetchall()

        paths = [r[0] for r in rows]
        assert paths == ["bar.py"], f"Expected only bar.py but got {paths}"


# ---------------------------------------------------------------------------
# test_rename_rollback_on_semantic_failure (acceptance #6 + metric check)
# ---------------------------------------------------------------------------


class TestRenameRollbackOnSemanticFailure:
    """When semantic indexer raises, SQLite must be rolled back to old path."""

    def test_rename_rollback_on_semantic_failure(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()

        store = _make_store(tmp_path)
        repo_int_id = store.create_repository(path=str(repo_root), name="testrepo")

        foo_abs = repo_root / "foo.py"
        foo_abs.write_text("x = 1\n")
        store.store_file(
            path=str(foo_abs),
            relative_path="foo.py",
            language="python",
            repository_id=repo_int_id,
        )

        ctx = _make_ctx(store, workspace_root=repo_root)
        dispatcher = _make_dispatcher(store)

        # move_file wraps the SQLite path-update (primary) and the semantic re-embed
        # (shadow) in two_phase_commit; a shadow failure must roll the SQLite move
        # back. Drive that by failing the semantic indexer's shadow op.
        failing_sem = Mock()
        failing_sem.semantic_profile.profile_id = "oss-high"
        failing_sem.cleanup_stale_semantic_artifacts = Mock(
            side_effect=RuntimeError("semantic failure")
        )

        bar_abs = repo_root / "bar.py"

        with patch.object(dispatcher, "_get_semantic_indexer", return_value=failing_sem):
            with pytest.raises(Exception):
                dispatcher.move_file(ctx, foo_abs, bar_abs, content_hash="abc123")

        # SQLite must still show OLD path after rollback
        with store._get_connection() as conn:
            rows = conn.execute(
                "SELECT relative_path FROM files "
                "WHERE repository_id = ? AND relative_path IN ('foo.py', 'bar.py') "
                "AND (is_deleted = 0 OR is_deleted IS NULL)",
                (repo_int_id,),
            ).fetchall()

        paths = [r[0] for r in rows]
        assert paths == ["foo.py"], f"Expected rollback to foo.py but got {paths}"

    def test_move_reports_semantic_counter_failure(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        store = _make_store(tmp_path)
        repo_int_id = store.create_repository(path=str(repo_root), name="testrepo")
        old_path = repo_root / "foo.py"
        new_path = repo_root / "bar.py"
        new_path.write_text("x = 1\n")
        store.store_file(
            path=old_path, relative_path="foo.py", language="python", repository_id=repo_int_id
        )
        ctx = _make_ctx(store, workspace_root=repo_root)
        dispatcher = _make_dispatcher(store)
        sem = Mock()
        sem.semantic_profile.profile_id = "oss-high"
        with (
            patch.object(dispatcher, "_get_semantic_indexer", return_value=sem),
            patch.object(
                dispatcher,
                "rebuild_semantic_for_paths",
                return_value={"semantic_failed": 1, "semantic_blocked": 0},
            ),
        ):
            result = dispatcher.move_file(ctx, old_path, new_path, content_hash="abc123")
        assert result.status == IndexResultStatus.ERROR
        assert result.path == new_path
        assert result.semantic["semantic_failed"] == 1
        assert dispatcher._operation_stats.get("moves", 0) == 0

    def test_move_returns_not_found_when_primary_row_never_existed(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()

        store = _make_store(tmp_path)
        store.create_repository(path=str(repo_root), name="testrepo")
        ctx = _make_ctx(store, workspace_root=repo_root)
        dispatcher = _make_dispatcher(store)

        foo_abs = repo_root / "foo.py"
        bar_abs = repo_root / "bar.py"
        foo_abs.write_text("x = 1\n")
        bar_abs.write_text("x = 1\n")

        result = dispatcher.move_file(ctx, foo_abs, bar_abs, content_hash="abc123")

        assert result.status == IndexResultStatus.NOT_FOUND

    def test_rename_rollback_increments_error_metric(self, tmp_path):
        """record_handled_error is called with module=dispatcher_enhanced and exception=IndexingError."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()

        store = _make_store(tmp_path)
        repo_int_id = store.create_repository(path=str(repo_root), name="testrepo")

        foo_abs = repo_root / "foo.py"
        foo_abs.write_text("x = 1\n")
        store.store_file(
            path=str(foo_abs),
            relative_path="foo.py",
            language="python",
            repository_id=repo_int_id,
        )

        ctx = _make_ctx(store, workspace_root=repo_root)
        dispatcher = _make_dispatcher(store)

        # A shadow-op (semantic) failure inside two_phase_commit surfaces as a
        # TwoPhaseCommitError, which move_file wraps in IndexingError and reports
        # via record_handled_error before re-raising.
        failing_sem = Mock()
        failing_sem.semantic_profile.profile_id = "oss-high"
        failing_sem.cleanup_stale_semantic_artifacts = Mock(side_effect=RuntimeError("boom"))

        bar_abs = repo_root / "bar.py"

        call_count = []

        def fake_record_handled_error(module, exc):
            call_count.append((module, type(exc).__name__))

        with (
            patch(
                "mcp_server.dispatcher.dispatcher_enhanced.record_handled_error",
                side_effect=fake_record_handled_error,
            ),
            patch.object(dispatcher, "_get_semantic_indexer", return_value=failing_sem),
        ):
            with pytest.raises(Exception):
                dispatcher.move_file(ctx, foo_abs, bar_abs, content_hash="abc123")

        assert len(call_count) >= 1
        modules = [c[0] for c in call_count]
        exc_names = [c[1] for c in call_count]
        assert "mcp_server.dispatcher.dispatcher_enhanced" in modules
        assert "IndexingError" in exc_names
