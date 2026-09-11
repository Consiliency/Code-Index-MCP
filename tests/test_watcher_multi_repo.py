"""Tests for MultiRepositoryWatcher and handler-level branch/gitignore filters."""

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from watchdog.events import FileCreatedEvent, FileDeletedEvent, FileMovedEvent
from watchdog.observers import Observer

from mcp_server.core.repo_context import RepoContext
from mcp_server.dispatcher.dispatcher_enhanced import IndexResult, IndexResultStatus
from mcp_server.watcher.sweeper import WatcherSweeper
from mcp_server.watcher_multi_repo import MultiRepositoryHandler, MultiRepositoryWatcher
from tests.fixtures.multi_repo import boot_test_server, build_temp_repo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo_context(workspace_root: Path, tracked_branch: str = "main") -> RepoContext:
    store = Mock()
    info = Mock()
    info.tracked_branch = tracked_branch
    info.registration_id = None
    return RepoContext(
        repo_id="deadbeef" * 4,
        sqlite_store=store,
        workspace_root=workspace_root,
        tracked_branch=tracked_branch,
        registry_entry=info,
    )


def _make_registry(repos=None):
    """Return a mock RepositoryRegistry whose get_all_repositories returns repos."""
    registry = Mock()
    repos = repos or {}
    registry.get_all_repositories.return_value = repos
    registry.register_repository.side_effect = lambda path: f"repo-{path}"
    registry.unregister_repository = Mock()
    registry.get_repository.side_effect = lambda repo_id: repos.get(repo_id)
    return registry


def _make_repo_info(path: str, auto_sync: bool = True):
    info = Mock()
    info.path = path
    info.auto_sync = auto_sync
    info.tracked_branch = "main"
    info.registration_id = None
    info.active = True
    return info


def _make_dispatcher():
    from mcp_server.dispatcher.dispatcher_enhanced import IndexResult, IndexResultStatus

    d = Mock()
    d.index_file = Mock()
    d.index_file_guarded = Mock(
        return_value=IndexResult(
            status=IndexResultStatus.INDEXED,
            path=Path("/mock"),
            observed_hash="abc",
            actual_hash="abc",
        )
    )
    d.remove_file = Mock(
        return_value=IndexResult(
            status=IndexResultStatus.DELETED,
            path=Path("/mock"),
            observed_hash=None,
            actual_hash=None,
        )
    )
    d.move_file = Mock(
        return_value=IndexResult(
            status=IndexResultStatus.MOVED,
            path=Path("/mock"),
            observed_hash=None,
            actual_hash=None,
        )
    )
    d.evict_repository_state = Mock()
    return d


def _make_repo_resolver(ctx_map=None):
    """Return a mock RepoResolver whose resolve() returns ctx based on path."""
    resolver = Mock()
    ctx_map = ctx_map or {}

    def _resolve(path):
        for root, ctx in ctx_map.items():
            if str(path).startswith(str(root)):
                return ctx
        return None

    resolver.resolve.side_effect = _resolve
    return resolver


def test_watcher_uses_current_generation_and_external_registry_membership(tmp_path, monkeypatch):
    from mcp_server.storage.repository_registry import RepositoryRegistry

    first, first_id = build_temp_repo(tmp_path, "first", seed_files={"seed.py": "first = 1\n"})
    second, second_id = build_temp_repo(tmp_path, "second", seed_files={"seed.py": "second = 2\n"})
    with boot_test_server(tmp_path, [first]) as server:
        dispatcher = _make_dispatcher()
        resolver = server.repo_resolver
        watcher = MultiRepositoryWatcher(
            server.registry,
            dispatcher,
            Mock(store_registry=server.store_registry),
            repo_resolver=resolver,
            store_registry=server.store_registry,
            sweeper=Mock(),
        )
        monkeypatch.setattr("mcp_server.watcher_multi_repo.Observer", Mock())
        watcher.running = True
        try:
            watcher._reconcile_registry(server.registry.get_all_repositories())
            handler = watcher.watchers[first_id]
            old_store = handler.ctx.sqlite_store
            external = RepositoryRegistry(server.registry.registry_path)
            info = external.get(first_id)
            external.update_indexed_commit(first_id, info.last_indexed_commit, branch="main")
            assert handler._trigger_reindex_with_ctx(first / "seed.py")
            assert dispatcher.index_file_guarded.call_args.args[0].sqlite_store is not old_store
            external.unregister(first_id)
            assert not handler._trigger_reindex_with_ctx(first / "seed.py")
            external.register_repository(str(second))
            watcher.git_monitor._check_repositories()
            assert set(watcher.watchers) == {second_id}
            assert external.get(first_id) is None
            assert external.get(second_id) is not None
        finally:
            watcher.stop_watching_all()
            watcher.executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# SL-1.1 tests: handler branch + gitignore filtering
# ---------------------------------------------------------------------------


class TestHandlerBranchFilter:
    """Handler drops event when current_branch != ctx.tracked_branch."""

    def test_drops_event_on_non_tracked_branch(self, tmp_path):
        dispatcher = _make_dispatcher()
        ctx = _make_repo_context(tmp_path, tracked_branch="main")

        # current branch = "feature", tracked = "main" → event should be dropped
        with patch(
            "mcp_server.watcher_multi_repo.subprocess.run",
            return_value=Mock(stdout="feature\n", returncode=0),
        ):
            handler = MultiRepositoryHandler(
                repo_id="repo-1",
                repo_path=tmp_path,
                parent_watcher=Mock(dispatcher=dispatcher, query_cache=None, path_resolver=None),
                ctx=ctx,
            )
            # Directly call _trigger_reindex (bypasses debounce) to test branch guard
            test_file = tmp_path / "test.py"
            test_file.write_text("x = 1")
            handler._trigger_reindex_with_ctx(test_file)

        dispatcher.index_file.assert_not_called()
        dispatcher.remove_file.assert_not_called()

    def test_allows_event_on_tracked_branch(self, tmp_path):
        dispatcher = _make_dispatcher()
        ctx = _make_repo_context(tmp_path, tracked_branch="main")

        with patch(
            "mcp_server.watcher_multi_repo.subprocess.run",
            return_value=Mock(stdout="main\n", returncode=0),
        ):
            handler = MultiRepositoryHandler(
                repo_id="repo-1",
                repo_path=tmp_path,
                parent_watcher=Mock(dispatcher=dispatcher, query_cache=None, path_resolver=None),
                ctx=ctx,
            )
            test_file = tmp_path / "test.py"
            test_file.write_text("x = 1")
            handler._trigger_reindex_with_ctx(test_file)

        dispatcher.remove_file.assert_called_once_with(ctx, test_file)
        dispatcher.index_file_guarded.assert_called_once()
        call_args = dispatcher.index_file_guarded.call_args[0]
        assert call_args[0] is ctx
        assert call_args[1] == test_file
        assert isinstance(call_args[2], str) and len(call_args[2]) == 64  # sha256 hex


class TestHandlerGitignoreFilter:
    """Handler drops event when path matches repo .gitignore."""

    def test_drops_event_matching_gitignore(self, tmp_path):
        dispatcher = _make_dispatcher()
        ctx = _make_repo_context(tmp_path, tracked_branch="main")

        # Write a .gitignore that ignores *.log files
        (tmp_path / ".gitignore").write_text("*.log\n")

        with patch(
            "mcp_server.watcher_multi_repo.subprocess.run",
            return_value=Mock(stdout="main\n", returncode=0),
        ):
            handler = MultiRepositoryHandler(
                repo_id="repo-1",
                repo_path=tmp_path,
                parent_watcher=Mock(dispatcher=dispatcher, query_cache=None, path_resolver=None),
                ctx=ctx,
            )
            ignored_file = tmp_path / "debug.log"
            ignored_file.write_text("log content")
            # Treat .log as code extension by patching code_extensions
            handler._inner_handler.code_extensions = {".log", ".py"}
            handler._trigger_reindex_with_ctx(ignored_file)

        dispatcher.index_file.assert_not_called()

    def test_allows_event_not_matching_gitignore(self, tmp_path):
        dispatcher = _make_dispatcher()
        ctx = _make_repo_context(tmp_path, tracked_branch="main")

        (tmp_path / ".gitignore").write_text("*.log\n")

        with patch(
            "mcp_server.watcher_multi_repo.subprocess.run",
            return_value=Mock(stdout="main\n", returncode=0),
        ):
            handler = MultiRepositoryHandler(
                repo_id="repo-1",
                repo_path=tmp_path,
                parent_watcher=Mock(dispatcher=dispatcher, query_cache=None, path_resolver=None),
                ctx=ctx,
            )
            allowed_file = tmp_path / "app.py"
            allowed_file.write_text("x = 1")
            handler._trigger_reindex_with_ctx(allowed_file)

        dispatcher.remove_file.assert_called_once_with(ctx, allowed_file)
        dispatcher.index_file_guarded.assert_called_once()
        call_args = dispatcher.index_file_guarded.call_args[0]
        assert call_args[0] is ctx
        assert call_args[1] == allowed_file
        assert isinstance(call_args[2], str) and len(call_args[2]) == 64


class TestWatcherMutationTruth:
    def test_created_event_skipped_guarded_index_does_not_mark_repo_changed(self, tmp_path):
        dispatcher = _make_dispatcher()
        dispatcher.index_file_guarded.return_value = IndexResult(
            status=IndexResultStatus.NOT_FOUND,
            path=tmp_path / "test.py",
            observed_hash="abc",
            actual_hash=None,
            error="skip",
        )
        parent = Mock(dispatcher=dispatcher, query_cache=None, path_resolver=None)
        ctx = _make_repo_context(tmp_path, tracked_branch="main")

        with patch(
            "mcp_server.watcher_multi_repo.subprocess.run",
            return_value=Mock(stdout="main\n", returncode=0),
        ):
            handler = MultiRepositoryHandler("repo-1", tmp_path, parent, ctx=ctx)
            test_file = tmp_path / "test.py"
            test_file.write_text("x = 1")
            handler.on_any_event(FileCreatedEvent(str(test_file)))

        parent.mark_repository_changed.assert_not_called()

    def test_deleted_event_not_found_does_not_mark_repo_changed(self, tmp_path):
        dispatcher = _make_dispatcher()
        dispatcher.remove_file.return_value = IndexResult(
            status=IndexResultStatus.NOT_FOUND,
            path=tmp_path / "gone.py",
            observed_hash=None,
            actual_hash=None,
            error="already absent",
        )
        parent = Mock(dispatcher=dispatcher, query_cache=None, path_resolver=None)
        ctx = _make_repo_context(tmp_path, tracked_branch="main")

        with patch(
            "mcp_server.watcher_multi_repo.subprocess.run",
            return_value=Mock(stdout="main\n", returncode=0),
        ):
            handler = MultiRepositoryHandler("repo-1", tmp_path, parent, ctx=ctx)
            deleted_file = tmp_path / "gone.py"
            handler.on_any_event(FileDeletedEvent(str(deleted_file)))

        parent.mark_repository_changed.assert_not_called()

    def test_move_with_missing_destination_uses_delete_recovery(self, tmp_path):
        dispatcher = _make_dispatcher()
        old_path = tmp_path / "old.py"
        old_path.write_text("x = 1")
        new_path = tmp_path / "new.py"
        parent = Mock(dispatcher=dispatcher, query_cache=None, path_resolver=None)
        ctx = _make_repo_context(tmp_path, tracked_branch="main")

        with patch(
            "mcp_server.watcher_multi_repo.subprocess.run",
            return_value=Mock(stdout="main\n", returncode=0),
        ):
            handler = MultiRepositoryHandler("repo-1", tmp_path, parent, ctx=ctx)
            handler.on_any_event(FileMovedEvent(str(old_path), str(new_path)))

        dispatcher.move_file.assert_not_called()
        dispatcher.remove_file.assert_called_once_with(ctx, old_path)
        parent.mark_repository_changed.assert_called_once_with("repo-1")

    def test_move_with_missing_destination_only_marks_changed_on_successful_delete(self, tmp_path):
        dispatcher = _make_dispatcher()
        dispatcher.remove_file.return_value = IndexResult(
            status=IndexResultStatus.NOT_FOUND,
            path=tmp_path / "old.py",
            observed_hash=None,
            actual_hash=None,
            error="already absent",
        )
        old_path = tmp_path / "old.py"
        old_path.write_text("x = 1")
        new_path = tmp_path / "new.py"
        parent = Mock(dispatcher=dispatcher, query_cache=None, path_resolver=None)
        ctx = _make_repo_context(tmp_path, tracked_branch="main")

        with patch(
            "mcp_server.watcher_multi_repo.subprocess.run",
            return_value=Mock(stdout="main\n", returncode=0),
        ):
            handler = MultiRepositoryHandler("repo-1", tmp_path, parent, ctx=ctx)
            handler.on_any_event(FileMovedEvent(str(old_path), str(new_path)))

        dispatcher.move_file.assert_not_called()
        dispatcher.remove_file.assert_called_once_with(ctx, old_path)
        parent.mark_repository_changed.assert_not_called()


# ---------------------------------------------------------------------------
# SL-1.1 tests: MultiRepositoryWatcher lifecycle
# ---------------------------------------------------------------------------


class TestMultiRepositoryWatcherLifecycle:
    """MultiRepositoryWatcher observer management."""

    def test_default_sweeper_is_created_when_store_registry_available(self, tmp_path):
        repo1 = tmp_path / "repo1"
        repo1.mkdir()
        repos = {"repo-1": _make_repo_info(str(repo1))}
        registry = _make_registry(repos)
        index_manager = Mock()
        index_manager.store_registry = Mock()
        index_manager.store_registry.get.return_value = Mock()

        watcher = MultiRepositoryWatcher(registry, _make_dispatcher(), index_manager)

        assert isinstance(watcher.sweeper, WatcherSweeper)

    def test_explicit_sweeper_is_honored(self, tmp_path):
        registry = _make_registry({})
        index_manager = Mock()
        explicit_sweeper = Mock()

        watcher = MultiRepositoryWatcher(
            registry,
            _make_dispatcher(),
            index_manager,
            sweeper=explicit_sweeper,
        )

        assert watcher.sweeper is explicit_sweeper

    def test_default_sweeper_starts_and_stops_with_watcher(self, tmp_path):
        registry = _make_registry({})
        index_manager = Mock()
        explicit_sweeper = Mock()
        watcher = MultiRepositoryWatcher(
            registry,
            _make_dispatcher(),
            index_manager,
            sweeper=explicit_sweeper,
        )

        watcher.start_watching_all()
        watcher.stop_watching_all()

        explicit_sweeper.start.assert_called_once()
        explicit_sweeper.stop.assert_called_once()

    def test_start_watching_all_spawns_one_observer_per_repo(self, tmp_path):
        repo1 = tmp_path / "repo1"
        repo2 = tmp_path / "repo2"
        repo1.mkdir()
        repo2.mkdir()

        repos = {
            "repo-1": _make_repo_info(str(repo1)),
            "repo-2": _make_repo_info(str(repo2)),
        }
        registry = _make_registry(repos)
        dispatcher = _make_dispatcher()
        index_manager = Mock()

        ctx1 = _make_repo_context(repo1)
        ctx2 = _make_repo_context(repo2)
        resolver = _make_repo_resolver({repo1: ctx1, repo2: ctx2})

        store_registry = Mock()
        plugin_registry = Mock()
        semantic_registry = Mock()
        watcher = MultiRepositoryWatcher(
            registry,
            dispatcher,
            index_manager,
            repo_resolver=resolver,
            store_registry=store_registry,
            plugin_set_registry=plugin_registry,
            semantic_indexer_registry=semantic_registry,
        )

        try:
            watcher.start_watching_all()
            time.sleep(0.1)
            assert len(watcher.observers) == 2
            for obs in watcher.observers.values():
                assert isinstance(obs, Observer)
                assert obs.is_alive()
        finally:
            watcher.stop_watching_all()


class TestArtifactPublishTriggers:
    """Watcher publishes only after successful mutating sync actions."""

    def _watcher_for_sync(self, tmp_path, action, files_processed=1):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        repo_info = _make_repo_info(str(repo_path))
        repo_info.artifact_enabled = True
        repo_info.index_location = str(repo_path / ".mcp-index")
        repo_info.index_path = repo_path / ".mcp-index" / "current.db"
        repos = {"repo-1": repo_info}
        registry = _make_registry(repos)
        registry.update_artifact_state = Mock()
        index_manager = Mock()
        index_manager.sync_repository_index.return_value = type(
            "Result",
            (),
            {
                "action": action,
                "files_processed": files_processed,
                "duration_seconds": 0.1,
                "commit": "synced123",
            },
        )()
        artifact_manager = Mock()
        artifact_manager.create_commit_artifact.return_value = repo_path / "artifact.tar.gz"
        artifact_manager.cleanup_old_artifacts.return_value = 0
        watcher = MultiRepositoryWatcher(
            registry,
            _make_dispatcher(),
            index_manager,
            artifact_manager=artifact_manager,
            sweeper=Mock(),
        )
        watcher._artifact_publisher = Mock()
        return watcher, registry, artifact_manager

    @pytest.mark.parametrize("action", ["full_index", "incremental_update"])
    def test_mutating_sync_actions_publish_once(self, tmp_path, action):
        watcher, registry, artifact_manager = self._watcher_for_sync(tmp_path, action)

        watcher._sync_repository("repo-1", "callback123")

        artifact_manager.create_commit_artifact.assert_called_once()
        watcher._artifact_publisher.publish_on_reindex.assert_called_once_with(
            "repo-1",
            "synced123",
            tracked_branch="main",
            index_location=str(tmp_path / "repo" / ".mcp-index"),
        )
        registry.update_artifact_state.assert_any_call(
            "repo-1",
            last_published_commit="synced123",
            artifact_health="published",
        )

    @pytest.mark.parametrize("action", ["wrong_branch", "up_to_date", "downloaded", "failed"])
    def test_non_mutating_sync_actions_do_not_publish(self, tmp_path, action):
        watcher, _registry, artifact_manager = self._watcher_for_sync(tmp_path, action)

        watcher._sync_repository("repo-1", "callback123")

        artifact_manager.create_commit_artifact.assert_not_called()
        watcher._artifact_publisher.publish_on_reindex.assert_not_called()

    def test_remote_publish_failure_records_publish_failed(self, tmp_path):
        watcher, registry, artifact_manager = self._watcher_for_sync(tmp_path, "full_index")
        watcher._artifact_publisher.publish_on_reindex.side_effect = RuntimeError("boom")

        watcher._sync_repository("repo-1", "callback123")

        artifact_manager.create_commit_artifact.assert_called_once()
        registry.update_artifact_state.assert_any_call("repo-1", artifact_health="publish_failed")

    def test_local_only_health_is_reserved_for_no_remote_publisher(self, tmp_path):
        watcher, registry, artifact_manager = self._watcher_for_sync(tmp_path, "full_index")
        watcher._artifact_publisher = None

        watcher._sync_repository("repo-1", "callback123")

        artifact_manager.create_commit_artifact.assert_called_once()
        registry.update_artifact_state.assert_any_call(
            "repo-1",
            last_published_commit="synced123",
            artifact_health="local_only",
        )

    def test_add_repository_after_start_begins_watching(self, tmp_path):
        repo1 = tmp_path / "repo1"
        repo1.mkdir()
        new_repo = tmp_path / "new_repo"
        new_repo.mkdir()

        repos = {"repo-1": _make_repo_info(str(repo1))}
        registry = _make_registry(repos)
        registry.register_repository.side_effect = None
        registry.register_repository.return_value = "repo-new"

        dispatcher = _make_dispatcher()
        index_manager = Mock()

        ctx1 = _make_repo_context(repo1)
        ctx_new = _make_repo_context(new_repo)
        resolver = _make_repo_resolver({repo1: ctx1, new_repo: ctx_new})

        watcher = MultiRepositoryWatcher(
            registry, dispatcher, index_manager, repo_resolver=resolver
        )

        try:
            watcher.start_watching_all()
            time.sleep(0.1)
            assert len(watcher.observers) == 1

            watcher.add_repository(str(new_repo))
            time.sleep(0.1)
            assert len(watcher.observers) == 2
            assert "repo-new" in watcher.observers
            assert watcher.observers["repo-new"].is_alive()
        finally:
            watcher.stop_watching_all()

    def test_remove_repository_stops_only_its_observer(self, tmp_path):
        repo1 = tmp_path / "repo1"
        repo2 = tmp_path / "repo2"
        repo1.mkdir()
        repo2.mkdir()

        repos = {
            "repo-1": _make_repo_info(str(repo1)),
            "repo-2": _make_repo_info(str(repo2)),
        }
        registry = _make_registry(repos)
        dispatcher = _make_dispatcher()
        index_manager = Mock()

        ctx1 = _make_repo_context(repo1)
        ctx2 = _make_repo_context(repo2)
        resolver = _make_repo_resolver({repo1: ctx1, repo2: ctx2})

        store_registry = Mock()
        plugin_registry = Mock()
        semantic_registry = Mock()
        watcher = MultiRepositoryWatcher(
            registry,
            dispatcher,
            index_manager,
            repo_resolver=resolver,
            store_registry=store_registry,
            plugin_set_registry=plugin_registry,
            semantic_indexer_registry=semantic_registry,
        )

        try:
            watcher.start_watching_all()
            time.sleep(0.1)
            assert len(watcher.observers) == 2

            obs2 = watcher.observers["repo-2"]
            watcher.remove_repository("repo-2")
            time.sleep(0.2)

            assert "repo-2" not in watcher.observers
            assert not obs2.is_alive()
            # repo-1 observer still running
            assert "repo-1" in watcher.observers
            assert watcher.observers["repo-1"].is_alive()
            store_registry.close.assert_called_once_with("repo-2")
            plugin_registry.evict.assert_called_once_with("repo-2")
            semantic_registry.evict.assert_called_once_with("repo-2")
            dispatcher.evict_repository_state.assert_called_once_with(
                "repo-2", repo_root=Path(repos["repo-2"].path)
            )
        finally:
            watcher.stop_watching_all()

    def test_stop_watching_all_joins_all_observers(self, tmp_path):
        repo1 = tmp_path / "repo1"
        repo1.mkdir()

        repos = {"repo-1": _make_repo_info(str(repo1))}
        registry = _make_registry(repos)
        dispatcher = _make_dispatcher()
        index_manager = Mock()

        ctx1 = _make_repo_context(repo1)
        resolver = _make_repo_resolver({repo1: ctx1})

        watcher = MultiRepositoryWatcher(
            registry, dispatcher, index_manager, repo_resolver=resolver
        )
        watcher.start_watching_all()
        time.sleep(0.1)

        obs = list(watcher.observers.values())
        assert all(o.is_alive() for o in obs)

        watcher.stop_watching_all()
        time.sleep(0.3)

        assert all(not o.is_alive() for o in obs), "Observers still alive after stop_watching_all"

    def test_only_auto_sync_repos_are_watched(self, tmp_path):
        repo1 = tmp_path / "repo1"
        repo2 = tmp_path / "repo2"
        repo1.mkdir()
        repo2.mkdir()

        repos = {
            "repo-1": _make_repo_info(str(repo1), auto_sync=True),
            "repo-2": _make_repo_info(str(repo2), auto_sync=False),
        }
        registry = _make_registry(repos)
        dispatcher = _make_dispatcher()
        index_manager = Mock()

        ctx1 = _make_repo_context(repo1)
        resolver = _make_repo_resolver({repo1: ctx1})

        watcher = MultiRepositoryWatcher(
            registry, dispatcher, index_manager, repo_resolver=resolver
        )

        try:
            watcher.start_watching_all()
            time.sleep(0.1)
            assert len(watcher.observers) == 1
            assert "repo-1" in watcher.observers
            assert "repo-2" not in watcher.observers
        finally:
            watcher.stop_watching_all()
