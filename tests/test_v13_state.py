"""Durable state boundary regressions using only synthetic local repositories."""

import json
import multiprocessing
from datetime import datetime
from pathlib import Path

import pytest

from mcp_server.storage.multi_repo_manager import RepositoryInfo
from mcp_server.storage.repository_registry import RepositoryRegistry


def _registered(tmp_path):
    registry = RepositoryRegistry(tmp_path / "registry.json")
    registry.register(
        RepositoryInfo(
            repository_id="synthetic",
            name="synthetic",
            path=tmp_path / "absent-repository",
            index_path=tmp_path / "index.db",
            language_stats={},
            total_files=0,
            total_symbols=0,
            indexed_at=datetime.now(),
        )
    )
    return registry


def _stale_priority_worker(path, ready, proceed):
    registry = RepositoryRegistry(path)
    ready.set()
    assert proceed.wait(10)
    registry.update_priority("synthetic", 7)


@pytest.mark.parametrize("delete", [False, True])
def test_stale_process_preserves_publication_or_deletion(tmp_path, delete):
    registry = _registered(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready, proceed = context.Event(), context.Event()
    worker = context.Process(
        target=_stale_priority_worker, args=(registry.registry_path, ready, proceed)
    )
    worker.start()
    try:
        assert ready.wait(10)
        if delete:
            assert registry.unregister_repository("synthetic")
        else:
            registry.update_staleness_reason("synthetic", "index_publication_pending")
            registry.update_last_sync_error("synthetic", "publication interrupted")
        proceed.set()
        worker.join(10)
        assert worker.exitcode == 0
        current = RepositoryRegistry(registry.registry_path).get("synthetic")
        if delete:
            assert current is None
            assert registry.get("synthetic") is None
        else:
            assert current.priority == 7
            assert current.staleness_reason == "index_publication_pending"
            assert current.last_sync_error == "publication interrupted"
            assert registry.get("synthetic").priority == 7
    finally:
        proceed.set()
        if worker.is_alive():
            worker.terminate()
        worker.join(10)
        worker.close()


def test_registry_read_surfaces_refresh_and_do_not_share_nested_state(tmp_path):
    registry = _registered(tmp_path)
    other = RepositoryRegistry(registry.registry_path)
    other.update_statistics("synthetic", {"total_files": 9, "language_stats": {"python": 9}})
    assert registry.get_statistics()["total_files"] == 9
    assert registry.list_all()[0].total_files == 9
    assert registry.get_all_repositories()["synthetic"].total_files == 9
    registry.get("synthetic").language_stats["python"] = 42
    assert registry.get("synthetic").language_stats == {"python": 9}
    other.unregister("synthetic")
    assert registry.find_by_path(tmp_path / "absent-repository") is None
    assert registry.list_all() == []


def test_legacy_save_applies_only_local_field_delta(tmp_path):
    registry = _registered(tmp_path)
    other = RepositoryRegistry(registry.registry_path)
    registry._registry["synthetic"]["priority"] = 8
    other.update_staleness_reason("synthetic", "index_publication_pending")
    registry.save()
    current = other.get("synthetic")
    assert current.priority == 8
    assert current.staleness_reason == "index_publication_pending"


@pytest.mark.parametrize("operation", ["write", "replace", "file_fsync", "directory_fsync"])
def test_registry_persistence_failure_is_not_acknowledged(tmp_path, monkeypatch, operation):
    import mcp_server.storage.repository_registry as module

    registry = _registered(tmp_path)
    fsync = module.os.fsync
    calls = 0

    def fail(*args, **kwargs):
        raise OSError("injected persistence failure")

    def fail_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == (1 if operation == "file_fsync" else 2):
            fail()
        return fsync(fd)

    with monkeypatch.context() as patch:
        if operation == "write":
            patch.setattr(module.json, "dump", fail)
        elif operation == "replace":
            patch.setattr(Path, "replace", fail)
        else:
            patch.setattr(module.os, "fsync", fail_fsync)
        with pytest.raises(OSError, match="injected persistence failure"):
            registry.update_priority("synthetic", 9)
        assert registry._registry["synthetic"]["priority"] == 0
    restarted = RepositoryRegistry(registry.registry_path)
    # Rename can be visible despite a failed directory fsync; the caller still
    # receives failure and must not assume a durable publication.
    assert restarted.get("synthetic").priority == (9 if operation == "directory_fsync" else 0)
    restarted.update_priority("synthetic", 3)
    assert restarted.get("synthetic").priority == 3


def test_corrupt_registry_fails_closed_on_load_and_mutation(tmp_path):
    registry = _registered(tmp_path)
    registry.registry_path.write_text("{broken")
    with pytest.raises(json.JSONDecodeError):
        RepositoryRegistry(registry.registry_path)
    with pytest.raises(json.JSONDecodeError):
        registry.update_priority("synthetic", 4)
    assert registry.registry_path.read_text() == "{broken"


def test_generation_publication_is_atomic_and_rejects_obsolete_registration(tmp_path):
    registry = _registered(tmp_path)
    before = registry.get("synthetic")
    publication = dict(
        generation="generation-one",
        index_path=tmp_path / "generation-one.db",
        commit="commit-one",
        branch="main",
        profile="synthetic-profile",
        expected_registration_id=before.registration_id,
        expected_generation=None,
    )
    registry.publish_generation("synthetic", **publication)
    current = RepositoryRegistry(registry.registry_path).get("synthetic")
    assert (
        current.index_generation,
        current.index_path,
        current.last_indexed_commit,
        current.last_indexed_branch,
        current.index_profile,
    ) == (
        "generation-one",
        tmp_path / "generation-one.db",
        "commit-one",
        "main",
        "synthetic-profile",
    )
    with pytest.raises(ValueError, match="generation changed"):
        registry.publish_generation("synthetic", **publication)
    registry.unregister("synthetic")
    registry.register(before)
    assert registry.get("synthetic").registration_id != before.registration_id
    with pytest.raises(ValueError, match="generation changed"):
        registry.publish_generation("synthetic", **publication)


def test_git_refresh_does_not_clear_publication_fence(tmp_path, monkeypatch):
    registry = _registered(tmp_path)
    writer = RepositoryRegistry(registry.registry_path)

    def commit(path):
        writer.update_staleness_reason("synthetic", "index_publication_pending")
        writer.update_last_sync_error("synthetic", "interrupted")
        return "commit-two"

    monkeypatch.setattr(registry, "_get_git_commit", commit)
    monkeypatch.setattr(registry, "_get_git_branch", lambda path: "main")
    assert registry.update_git_state("synthetic")["commit"] == "commit-two"
    current = writer.get("synthetic")
    assert current.staleness_reason == "index_publication_pending"
    assert current.last_sync_error == "interrupted"


@pytest.mark.parametrize("outcome", ["clean", "error", "cancelled", "exception", "unregister"])
def test_scoped_mutation_fences_reads_and_publishes_only_clean_outcomes(tmp_path, outcome):
    from mcp_server.core.repo_resolver import run_repository_mutation
    from tests.fixtures.multi_repo import boot_test_server, build_temp_repo

    path, repo_id = build_temp_repo(tmp_path, "mutation")
    with boot_test_server(tmp_path, [path]) as server:
        resolver = server.repo_resolver
        ctx = resolver.resolve_ready(path)
        before = server.registry.get(repo_id)

        def mutate(current):
            assert current.staging
            assert current.sqlite_store.db_path != ctx.sqlite_store.db_path
            assert not resolver.is_current(ctx)
            assert not resolver.classify(path).ready
            assert server.registry.get(repo_id).staleness_reason == "index_publication_pending"
            with current.sqlite_store._get_connection() as conn:
                conn.execute("UPDATE files SET language = 'synthetic'")
            if outcome == "exception":
                raise RuntimeError("injected mutation failure")
            if outcome == "unregister":
                server.registry.unregister(repo_id)
            return {"failed_files": int(outcome == "error"), "cancelled": outcome == "cancelled"}

        if outcome == "clean":
            run_repository_mutation(resolver, ctx, mutate)
            after = server.registry.get(repo_id)
            assert after.index_generation != before.index_generation
            assert after.last_indexed_commit == before.last_indexed_commit
            assert resolver.resolve_ready(path) is not None
            with pytest.raises(RuntimeError, match="generation changed"):
                run_repository_mutation(resolver, ctx, lambda current: pytest.fail("stale writer"))
        else:
            with pytest.raises((RuntimeError, ValueError)):
                run_repository_mutation(resolver, ctx, mutate)
            assert not resolver.classify(path).ready
            after = RepositoryRegistry(server.registry.registry_path).get(repo_id)
            if outcome == "unregister":
                assert after is None
            else:
                assert after.index_generation == before.index_generation
                assert after.staleness_reason == "partial_index_failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["search", "symbol"])
async def test_gateway_cache_does_not_reuse_a_previous_generation(tmp_path, monkeypatch, surface):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from starlette.requests import Request

    import mcp_server.gateway as gateway
    from mcp_server.cache import CacheManagerFactory, QueryResultCache
    from tests.fixtures.multi_repo import boot_test_server, build_temp_repo

    path, repo_id = build_temp_repo(
        tmp_path, "cache", seed_files={"seed.py": "def cache_token():\n    return 7\n"}
    )
    manager = CacheManagerFactory.create_memory_cache()
    await manager.initialize()
    try:
        with boot_test_server(tmp_path, [path]) as server:
            monkeypatch.setattr(gateway, "repo_resolver", server.repo_resolver)
            monkeypatch.setattr(gateway, "dispatcher", server.dispatcher)
            monkeypatch.setattr(gateway, "query_cache", QueryResultCache(manager))
            monkeypatch.setattr(gateway, "metrics_collector", MagicMock())
            monkeypatch.setattr(gateway, "business_metrics", MagicMock())
            monkeypatch.setattr(
                gateway, "get_repo_ctx", lambda request: server.repo_resolver.resolve_ready(path)
            )
            method = "search" if surface == "search" else "lookup"
            spy = MagicMock(wraps=getattr(server.dispatcher, method))
            monkeypatch.setattr(server.dispatcher, method, spy)
            request = Request({"type": "http", "headers": [], "query_string": b""})
            user = SimpleNamespace(username="synthetic")

            async def query():
                if surface == "search":
                    return await gateway.search(request, q="cache_token", current_user=user)
                return await gateway.symbol(request, symbol="cache_token", current_user=user)

            assert await query()
            assert await query()
            assert spy.call_count == 1
            info = server.registry.get(repo_id)
            server.registry.update_indexed_commit(repo_id, info.last_indexed_commit, branch="main")
            assert await query()
            assert spy.call_count == 2
    finally:
        await manager.shutdown()


def test_graph_and_query_cache_keys_cover_generation_and_typed_dict_dependencies(tmp_path):
    from mcp_server.cache import QueryResultCache, QueryType
    from tests.fixtures.multi_repo import boot_test_server, build_temp_repo

    path, repo_id = build_temp_repo(tmp_path, "graph")
    with boot_test_server(tmp_path, [path]) as server:
        ctx = server.repo_resolver.resolve_ready(path)
        before = server.dispatcher._graph_key(ctx)
        server.registry.update_indexed_commit(
            repo_id, ctx.registry_entry.last_indexed_commit, branch="main"
        )
        current = server.repo_resolver.resolve_ready(path)
        assert before != server.dispatcher._graph_key(current)
        cache = QueryResultCache(None)
        assert cache._extract_file_dependencies(
            QueryType.SYMBOL_LOOKUP, {"defined_in": "a.py"}
        ) == {"a.py"}
        assert cache._extract_file_dependencies(
            QueryType.SEARCH, [{"file": "b.py"}, {"file_path": "c.py"}]
        ) == {"b.py", "c.py"}
