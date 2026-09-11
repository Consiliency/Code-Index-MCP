"""Production committed-source and watcher reconciliation controls."""

import subprocess

import pytest

from mcp_server.core.repo_resolver import RepoResolver
from mcp_server.storage.store_registry import StoreRegistry
from mcp_server.watcher_multi_repo import MultiRepositoryWatcher
from tests.test_v13_data_storage import runtime


@pytest.mark.parametrize("operation", ["modify", "rename", "delete"])
def test_incremental_committed_mutation_updates_content_and_keeps_move_identity(runtime, operation):
    repo, _registry, repo_id, _original, manager = runtime
    source = repo / "hello.py"
    content = "\n".join(f"number_{i} = {i}" for i in range(40)) + "\noldsentinel = 1\n"
    source.write_text(content)
    for i in range(8):
        (repo / f"unchanged{i}.py").write_text(f"unchanged_{i} = {i}\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic incremental base"], cwd=repo, check=True)
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    old_id = manager._resolve_ctx(repo_id).sqlite_store.get_file_id_by_path("hello.py")
    if operation == "delete":
        source.unlink()
    else:
        if operation == "rename":
            target = repo / "renamed.py"
            source.rename(target)
            source = target
        source.write_text(content.replace("oldsentinel", "newsentinel"))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic incremental change"], cwd=repo, check=True)
    result = manager.sync_repository_index(repo_id)
    assert result.action == "incremental_update", result.error
    store = manager._resolve_ctx(repo_id).sqlite_store
    assert not store.search_code_fts("oldsentinel")
    if operation != "delete":
        assert store.search_code_fts("newsentinel")
        assert store.get_file_id_by_path(source.name) == old_id
    if operation == "rename":
        assert store.get_file_id_by_path("hello.py") is None
        with store._get_connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM file_moves").fetchone()[0] == 1


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
