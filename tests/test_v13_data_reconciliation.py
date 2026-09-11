"""Production committed-source and watcher reconciliation controls."""

import subprocess

from mcp_server.core.repo_resolver import RepoResolver
from mcp_server.storage.store_registry import StoreRegistry
from mcp_server.watcher_multi_repo import MultiRepositoryWatcher
from tests.test_v13_data_storage import runtime


def test_sweep_repairs_existing_path_drift_once_and_ignores_untracked(runtime):
    repo, registry, repo_id, _original, manager = runtime
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    stores = StoreRegistry.for_registry(registry)
    manager.store_registry = stores
    resolver = RepoResolver(registry, stores)
    manager.repo_resolver = resolver
    watcher = MultiRepositoryWatcher(registry, manager.dispatcher, manager, repo_resolver=resolver)
    try:
        first = registry.get(repo_id).index_generation
        active = stores.get(repo_id)
        with active._get_connection() as connection:
            connection.execute("UPDATE files SET content_hash='missed-update'")
        assert watcher.sweeper.sweep_once() == [repo_id]
        assert registry.get(repo_id).index_generation != first
        assert watcher.sweeper.sweep_once() == []
        (repo / "untracked.py").write_text("untracked = 1\n")
        assert watcher.sweeper.sweep_once() == []
    finally:
        watcher.executor.shutdown(wait=True)


def test_sweep_refuses_dirty_source_then_reconciles_committed_change(runtime):
    repo, registry, repo_id, _original, manager = runtime
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    manager.store_registry = StoreRegistry.for_registry(registry)
    resolver = RepoResolver(registry, manager.store_registry)
    manager.repo_resolver = resolver
    watcher = MultiRepositoryWatcher(registry, manager.dispatcher, manager, repo_resolver=resolver)
    try:
        original = registry.get(repo_id).index_generation
        (repo / "hello.py").write_text("committedchange = 42\n")
        watcher.sweeper.sweep_once()
        assert registry.get(repo_id).index_generation == original
        subprocess.run(["git", "add", "hello.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "Synthetic change"], cwd=repo, check=True)
        watcher.sweeper.sweep_once()
        assert registry.get(repo_id).index_generation != original
        assert manager.store_registry.get(repo_id).search_code_fts("committedchange")
    finally:
        watcher.executor.shutdown(wait=True)


def test_single_repository_watcher_uses_generation_owner(runtime, monkeypatch):
    from mcp_server.watcher.file_watcher import _Handler

    repo, registry, repo_id, _original, manager = runtime
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    ctx = manager._resolve_ctx(repo_id)
    handler = _Handler(manager.dispatcher, ctx=ctx, index_manager=manager)
    try:
        before = registry.get(repo_id).index_generation
        (repo / "hello.py").write_text("updatedvalue = 2\n")
        handler.trigger_reindex(repo / "hello.py")
        assert registry.get(repo_id).index_generation == before
        subprocess.run(["git", "add", "hello.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "Synthetic watcher change"], cwd=repo, check=True)
        handler.trigger_reindex(repo / "hello.py")
        assert registry.get(repo_id).index_generation != before
        assert manager._resolve_ctx(repo_id).sqlite_store.search_code_fts("updatedvalue")
    finally:
        handler.stop()
