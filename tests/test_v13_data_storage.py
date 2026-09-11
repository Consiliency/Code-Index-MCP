"""Retained-data and staged-publication regression controls."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mcp_server.core.path_resolver import PathResolver
from mcp_server.core.repo_context import RepoContext
from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher
from mcp_server.storage.git_index_manager import GitAwareIndexManager
from mcp_server.storage.repository_registry import RepositoryRegistry
from mcp_server.storage.sqlite_store import SQLiteStore
from tests.test_git_index_manager import _get_head_commit, _make_git_repo
from tests.test_history_issue_storage import _history_record


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    monkeypatch.setenv("MCP_INDEX_STORAGE_PATH", str(tmp_path / "indexes"))
    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("MCP_ENABLE_MULTI_REPO", "false")
    monkeypatch.chdir(tmp_path)
    registry = RepositoryRegistry(tmp_path / "registry.json")
    repo_id = registry.register_repository(str(repo))
    info = registry.get(repo_id)
    info.index_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(str(info.index_path), path_resolver=PathResolver(repo))
    dispatcher = EnhancedDispatcher(
        plugins=[],
        enable_advanced_features=False,
        use_plugin_factory=False,
        semantic_search_enabled=False,
        memory_aware=False,
        multi_repo_enabled=False,
    )
    manager = GitAwareIndexManager(registry, dispatcher)
    try:
        yield repo, registry, repo_id, store, manager
    finally:
        dispatcher.shutdown()
        store.close()
        if manager.store_registry is not None:
            manager.store_registry.shutdown()


def _seed_retained(store, repo):
    repository_id = store.ensure_repository_row(repo, name="fixture")
    store.upsert_history_issue_documents(
        repository_id, [_history_record(91, "reflection", "2026-07-05T00:00:00Z")]
    )
    file_id = store.store_file(repository_id, repo / "imported-note.md", "imported-note.md")
    symbol_id = store.store_symbol(file_id, "RetainedDocument", "document", 1, 1)
    store.store_chunk(
        file_id=file_id,
        symbol_id=symbol_id,
        content="retained document sentinel",
        content_start=0,
        content_end=26,
        line_start=1,
        line_end=1,
        chunk_id="document:fixture",
        node_id="document:fixture",
        treesitter_file_id="imported",
        chunk_type="document",
    )
    store.store_reference(symbol_id, file_id, 1)
    store.store_chunk_summary(
        "document:fixture",
        file_id,
        0,
        26,
        "retained summary",
        llm_model="fixture",
        profile_id="fixture",
        is_authoritative=True,
    )
    store.upsert_semantic_point("fixture", "document:fixture", 101, "retained-collection")
    with store._get_connection() as connection:
        connection.execute(
            "INSERT INTO imports (file_id, imported_path, imported_name) VALUES (?, ?, ?)",
            (file_id, "retained-module", "RetainedDocument"),
        )
        store._record_pending_vector_deletions(
            connection,
            [
                {
                    "profile_id": "fixture",
                    "chunk_id": "old-code",
                    "point_id": 202,
                    "collection": "old-collection",
                }
            ],
        )
        connection.execute(
            "INSERT INTO file_moves (repository_id, old_relative_path, new_relative_path, "
            "content_hash, move_type) VALUES (?, 'before.md', 'imported-note.md', 'old', 'rename')",
            (repository_id,),
        )
        connection.execute(
            "INSERT INTO index_config (config_key, config_value) VALUES ('operator-fixture', 'keep')"
        )
    return file_id, symbol_id


def test_standalone_manager_reuses_generation_bound_store(runtime):
    _repo, _registry, repo_id, _store, manager = runtime
    first = manager._resolve_ctx(repo_id)
    second = manager._resolve_ctx(repo_id)
    assert manager.store_registry is not None
    assert first.sqlite_store is second.sqlite_store
    assert manager.store_registry.is_current(repo_id, first.sqlite_store)


def test_production_rebuild_preserves_imported_data_links_and_cleanup_debt(runtime):
    repo, registry, repo_id, original, manager = runtime
    file_id, symbol_id = _seed_retained(original, repo)
    registry.update_indexed_commit(repo_id, _get_head_commit(repo), branch="main")
    original_path = Path(original.db_path)
    before = original_path.read_bytes()
    result = manager.rebuild_repository_index(repo_id)
    assert result.action == "full_index", result.error
    active = registry.get(repo_id)
    assert active.index_path != original_path
    assert original_path.read_bytes() == before
    rebuilt = SQLiteStore(str(active.index_path), path_resolver=PathResolver(repo))
    try:
        assert len(rebuilt.search_chunks_by_source_metadata(source_type="history")) == 1
        assert rebuilt.get_chunk_summary("document:fixture")["summary_text"] == "retained summary"
        assert rebuilt.get_semantic_point_ids("fixture", ["document:fixture"]) == [101]
        assert rebuilt.get_pending_vector_deletions()[0]["collection"] == "old-collection"
        assert rebuilt.search_code_fts("retained")
        assert rebuilt.search_code_fts("hello")
        with rebuilt._get_connection() as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert (
                connection.execute(
                    "SELECT symbol_id FROM code_chunks WHERE chunk_id='document:fixture'"
                ).fetchone()[0]
                == symbol_id
            )
            assert (
                connection.execute(
                    "SELECT file_id FROM symbol_references WHERE symbol_id=?", (symbol_id,)
                ).fetchone()[0]
                == file_id
            )
            assert connection.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM file_moves").fetchone()[0] == 1
            assert (
                connection.execute(
                    "SELECT config_value FROM index_config WHERE config_key='operator-fixture'"
                ).fetchone()[0]
                == "keep"
            )
    finally:
        rebuilt.close()


def test_stage_has_early_fence_own_context_and_immutable_source(runtime, monkeypatch):
    repo, registry, repo_id, store, manager = runtime
    _seed_retained(store, repo)
    original = registry.get(repo_id)
    captured = _get_head_commit(repo)
    full_index = manager._full_index

    def inspect_stage(repo_id, ctx, **kwargs):
        assert registry.get(repo_id).staleness_reason == "index_publication_pending"
        assert ctx.staging is True
        assert ctx.registry_entry.index_generation != original.index_generation
        assert ctx.registry_entry.index_path == Path(ctx.sqlite_store.db_path)
        assert ctx.registry_entry.last_indexed_commit == captured
        assert ctx.workspace_root != repo
        assert (ctx.workspace_root / "hello.py").read_text() == "print('hello')\n"
        (repo / "hello.py").write_text("uncommitted private replacement\n")
        assert (ctx.workspace_root / "hello.py").read_text() == "print('hello')\n"
        return full_index(repo_id, ctx, **kwargs)

    monkeypatch.setattr(manager, "_full_index", inspect_stage)
    result = manager.rebuild_repository_index(repo_id)
    assert result.action == "failed"
    assert registry.get(repo_id).index_path == original.index_path
    assert registry.get(repo_id).staleness_reason == "partial_index_failure"


@pytest.mark.parametrize(
    "stage", ["stage_created", "before_replacement", "after_replacement", "before_provenance"]
)
def test_failed_generation_retains_old_bytes_and_pending_fence(runtime, monkeypatch, stage):
    repo, registry, repo_id, store, manager = runtime
    _seed_retained(store, repo)
    registry.update_indexed_commit(repo_id, _get_head_commit(repo), branch="main")
    original = registry.get(repo_id)
    before = original.index_path.read_bytes()

    def fail(point):
        if point == stage:
            raise OSError("private staging fault")

    monkeypatch.setattr(manager, "_rebuild_checkpoint", fail)
    result = manager.rebuild_repository_index(repo_id)
    assert result.action == "failed"
    assert "private" not in result.error
    current = RepositoryRegistry(registry.registry_path).get(repo_id)
    assert current.index_path == original.index_path
    assert original.index_path.read_bytes() == before
    assert current.staleness_reason == "partial_index_failure"


def test_storing_identical_files_does_not_invent_a_rename(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    first, second = repo / "one.py", repo / "two.py"
    first.write_text("same = 1\n")
    second.write_text(first.read_text())
    store = SQLiteStore(str(tmp_path / "index.db"), path_resolver=PathResolver(repo))
    try:
        repo_id = store.ensure_repository_row(repo)
        one = store.store_file(repo_id, first, "one.py")
        two = store.store_file(repo_id, second, "two.py")
        assert one != two
        assert {row["relative_path"] for row in store.get_all_files()} == {"one.py", "two.py"}
        with store._get_connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM file_moves").fetchone()[0] == 0
    finally:
        store.close()


def test_semantic_close_failure_prevents_generation_publication(runtime, monkeypatch):
    repo, registry, repo_id, store, manager = runtime
    _seed_retained(store, repo)
    original = registry.get(repo_id)
    semantic_registry = MagicMock()
    semantic_registry.evict.side_effect = OSError("synthetic flush failure")
    monkeypatch.setattr(manager.dispatcher, "_semantic_registry", semantic_registry)
    result = manager.rebuild_repository_index(repo_id)
    assert result.action == "failed"
    assert registry.get(repo_id).index_path == original.index_path
    assert registry.get(repo_id).staleness_reason == "partial_index_failure"


def test_staged_fts_rebuild_reads_snapshot_and_retains_imported_documents(runtime, tmp_path):
    repo, _registry, _repo_id, original, _manager = runtime
    _seed_retained(original, repo)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "hello.py").write_text("committedsentinel = 1\n")
    repository_id = original.ensure_repository_row(repo)
    original.store_file(repository_id, repo / "hello.py", "hello.py")
    (repo / "hello.py").write_text("privatesentinel = 1\n")
    destination = tmp_path / "staged.db"
    SQLiteStore.snapshot_database(Path(original.db_path), destination)
    staged = SQLiteStore(str(destination), path_resolver=PathResolver(repo, source_root=snapshot))
    try:
        staged.rebuild_fts_code()
        assert staged.search_code_fts("committedsentinel")
        assert not staged.search_code_fts("privatesentinel")
        assert staged.search_code_fts("retained")
    finally:
        staged.close()


def test_staged_summary_scope_matches_snapshot_input(runtime, tmp_path):
    repo, registry, repo_id, _original, manager = runtime
    snapshot = tmp_path / "summary-source"
    snapshot.mkdir()
    source = snapshot / "hello.py"
    source.write_text("def hello():\n    return 1\n")
    store = SQLiteStore(
        str(tmp_path / "summary-stage.db"), path_resolver=PathResolver(repo, source_root=snapshot)
    )
    ctx = RepoContext(
        repo_id=repo_id,
        sqlite_store=store,
        workspace_root=snapshot,
        tracked_branch="main",
        registry_entry=registry.get(repo_id),
        staging=True,
    )
    try:
        manager.dispatcher._persist_index_shard(
            ctx,
            source,
            source.read_text(),
            "python",
            {
                "chunks": [{"chunk_id": "summary-scope", "content": source.read_text()}],
            },
        )
        assert manager.dispatcher._count_missing_summaries_for_paths(ctx, [source]) == 1
        with store._get_connection() as connection:
            assert connection.execute("SELECT path FROM files").fetchone()[0] == str(source)
    finally:
        store.close()


def test_code_refresh_retains_document_rows_and_records_all_profile_vector_debt(runtime):
    repo, registry, repo_id, store, manager = runtime
    file_id, document_symbol = _seed_retained(store, repo)
    store.store_chunk(
        file_id=file_id,
        content="old code",
        content_start=0,
        content_end=8,
        line_start=1,
        line_end=1,
        chunk_id="old-code",
        node_id="old-code",
        treesitter_file_id="fixture",
    )
    store.store_chunk_summary("old-code", file_id, 0, 8, "old summary", "fixture")
    store.upsert_semantic_point("secondary", "old-code:part:0", 303, "secondary-old")
    ctx = RepoContext(
        repo_id=repo_id,
        sqlite_store=store,
        workspace_root=repo,
        tracked_branch="main",
        registry_entry=registry.get(repo_id),
    )
    path = repo / "imported-note.md"
    path.write_text("new code")
    manager.dispatcher._persist_index_shard(
        ctx,
        path,
        "new code",
        "markdown",
        {
            "chunks": [{"chunk_id": "new-code", "content": "new code"}],
        },
    )
    assert store.get_chunk_by_chunk_id("document:fixture")["symbol_id"] == document_symbol
    assert store.get_chunk_summary("document:fixture")["summary_text"] == "retained summary"
    assert store.get_chunk_summary("old-code") is None
    assert store.get_semantic_point_ids("fixture", ["document:fixture"]) == [101]
    assert store.get_semantic_point_ids("secondary", ["old-code:part:0"]) == []
    assert any(
        row["point_id"] == 303 and row["collection"] == "secondary-old"
        for row in store.get_pending_vector_deletions()
    )


@pytest.mark.parametrize(
    "restore_fault", [None, "missing", "duplicate", "path", "dimension", "provenance", "commit"]
)
def test_real_semantic_generation_uses_its_own_backend_and_matching_summary_ids(
    runtime, monkeypatch, restore_fault
):
    import json
    from types import SimpleNamespace

    from mcp_server.config.settings import Settings
    from mcp_server.utils.semantic_indexer_registry import SemanticIndexerRegistry
    from tests.test_embedding_provenance import _FakeProvenanceProvider, _openai_response, _profile

    repo, registry, repo_id, _store, manager = runtime
    (repo / "hello.py").write_text(
        "def hello():\n    # TODO: maintain synthetic observation\n    return 'semantic sentinel'\n"
    )
    (repo / "same.py").write_text((repo / "hello.py").read_text())
    subprocess.run(["git", "add", "hello.py", "same.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic semantic fixture"], cwd=repo, check=True)
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.setenv("SEMANTIC_SEARCH_ENABLED", "true")
    settings = Settings(
        semantic_search_enabled=True,
        semantic_default_profile="fixture",
        semantic_profiles_json=json.dumps({"fixture": _profile().to_dict()}),
    )
    monkeypatch.setattr("mcp_server.config.settings.get_settings", lambda: settings)
    monkeypatch.setattr("mcp_server.utils.semantic_indexer_registry.get_settings", lambda: settings)
    monkeypatch.setattr(
        "mcp_server.dispatcher.dispatcher_enhanced.reload_settings", lambda: settings
    )
    provider = _FakeProvenanceProvider(_openai_response)
    monkeypatch.setattr(
        "mcp_server.utils.semantic_indexer.create_embedding_provider", lambda **kw: provider
    )
    global_probe = MagicMock(side_effect=AssertionError("Unowned global backend probe"))
    monkeypatch.setattr("mcp_server.setup.semantic_preflight.run_semantic_preflight", global_probe)

    class FixtureWriter:
        def __init__(self, db_path, **kwargs):
            self.db_path = db_path

        async def process_scope(self, **kwargs):
            store = SQLiteStore(self.db_path)
            try:
                rows = store.get_missing_summaries(limit=kwargs["limit"])
                for row in rows:
                    store.store_chunk_summary(
                        row["chunk_id"],
                        row["file_id"],
                        row["content_start"],
                        row["content_end"],
                        "Synthetic summary",
                        "fixture",
                        profile_id="fixture",
                        is_authoritative=True,
                    )
                return SimpleNamespace(
                    summaries_written=len(rows),
                    chunks_attempted=len(rows),
                    authoritative_chunks=len(rows),
                    missing_chunk_ids=[],
                    files_attempted=1,
                    files_summarized=1,
                )
            finally:
                store.close()

    monkeypatch.setattr("mcp_server.indexing.summarization.ComprehensiveChunkWriter", FixtureWriter)
    manager.dispatcher._semantic_enabled = True
    manager.dispatcher._semantic_registry = SemanticIndexerRegistry(registry)
    result = manager.rebuild_repository_index(repo_id)
    assert result.action == "full_index", (result.error, result.semantic)
    global_probe.assert_not_called()
    assert provider.calls
    ctx = manager._resolve_ctx(repo_id)
    from mcp_server.health.repository_readiness import ReadinessClassifier

    semantic_readiness = ReadinessClassifier.classify_semantic_registered(
        ctx.registry_entry, ctx.sqlite_store
    )
    assert semantic_readiness.ready, semantic_readiness.to_dict()

    def assert_public_semantic_queries():
        import asyncio
        from urllib.parse import urlencode

        from starlette.requests import Request

        import mcp_server.gateway as gateway
        from mcp_server import ClientSearchOptions, open_client
        from mcp_server.cli.tool_handlers import handle_search_code
        from mcp_server.core.repo_resolver import RepoResolver

        manager.dispatcher._semantic_registry.evict(repo_id)
        with open_client(workspace_root=repo, registry_path=registry.registry_path) as client:
            response = client.search_code(
                ClientSearchOptions(
                    query="concept",
                    semantic=True,
                    source_type="friction",
                    friction_categories=("todo",),
                )
            )
            assert response.code is None, response
            assert len(response.results) == 2
            assert all(row.source_metadata for row in response.results)
        resolver = RepoResolver(registry, manager.store_registry)
        monkeypatch.setattr(gateway, "repo_resolver", resolver)
        monkeypatch.setattr(gateway, "dispatcher", manager.dispatcher)
        monkeypatch.setattr(gateway, "query_cache", None)
        monkeypatch.setattr(gateway, "get_settings", lambda: settings)
        monkeypatch.setattr(gateway, "metrics_collector", MagicMock())
        monkeypatch.setattr(gateway, "business_metrics", MagicMock())

        async def query_transports():
            blocks = await handle_search_code(
                arguments={
                    "query": "concept",
                    "repository": str(repo),
                    "semantic": True,
                    "source_type": "friction",
                    "friction_categories": ["todo"],
                },
                dispatcher=manager.dispatcher,
                repo_resolver=resolver,
            )
            payload = json.loads(blocks[0].text)
            assert len(payload.get("results", [])) == 2, payload
            request = Request(
                {
                    "type": "http",
                    "headers": [],
                    "query_string": urlencode({"repository": str(repo)}).encode(),
                }
            )
            rows = await gateway.search(
                request,
                q="concept",
                semantic=True,
                source_type="friction",
                friction_categories="todo",
                current_user=SimpleNamespace(username="synthetic"),
            )
            assert len(rows) == 2, rows
            assert all(row.get("source_metadata") for row in rows)

        asyncio.run(query_transports())

    if restore_fault is None:
        assert_public_semantic_queries()
    with manager.dispatcher._semantic_registry.lease(repo_id) as indexer:
        points, offset = indexer.qdrant.scroll(indexer.collection, with_payload=True, limit=100)
        assert points and offset is None
        points = [point for point in points if point.id != indexer.PROVENANCE_POINT_ID]
        assert {point.payload["file"] for point in points} == {
            str(repo / "hello.py"),
            str(repo / "same.py"),
        }
        with ctx.sqlite_store._get_connection() as connection:
            first_ids = {row[0] for row in connection.execute("SELECT chunk_id FROM code_chunks")}
            assert len(first_ids) == 2
            assert connection.execute("SELECT COUNT(*) FROM semantic_points").fetchone()[0] > 0
            assert {
                row[0] for row in connection.execute("SELECT collection FROM semantic_points")
            } == {indexer.collection}
        import tarfile

        from mcp_server.artifacts.secure_export import SecureIndexExporter

        archive = repo.parent / "semantic-export.tar.gz"
        SecureIndexExporter(
            repo_path=repo, index_path=ctx.sqlite_store.db_path, semantic_indexer=indexer
        ).create_secure_archive(str(archive))
        extracted = repo.parent / "semantic-extracted"
        extracted.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(extracted, filter="data")
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    with manager._resolve_ctx(repo_id).sqlite_store._get_connection() as connection:
        assert {
            row[0] for row in connection.execute("SELECT chunk_id FROM code_chunks")
        } == first_ids
    before_path = registry.get(repo_id).index_path
    before_bytes = before_path.read_bytes()
    vectors_path = extracted / "semantic-vectors.jsonl"
    vectors = [json.loads(line) for line in vectors_path.read_text().splitlines()]
    if restore_fault == "missing":
        vectors.pop()
    elif restore_fault == "duplicate":
        vectors.append(vectors[0])
    elif restore_fault == "path":
        vectors[0]["payload"]["relative_path"] = "../outside.py"
    elif restore_fault == "dimension":
        vectors[0]["vector"].pop()
    elif restore_fault == "provenance":
        metadata_path = extracted / ".index_metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["semantic_profiles"]["fixture"]["model_version"] = "wrong-revision"
        metadata_path.write_text(json.dumps(metadata))
    vectors_path.write_text("\n".join(json.dumps(point) for point in vectors) + "\n")
    result = manager.restore_verified_artifact(
        repo_id,
        extracted,
        expected_commit="0" * 40 if restore_fault == "commit" else _get_head_commit(repo),
    )
    assert before_path.read_bytes() == before_bytes
    if restore_fault:
        assert result.action == "failed"
        assert registry.get(repo_id).index_path == before_path
        assert registry.get(repo_id).staleness_reason == "partial_index_failure"
        return
    assert result.action == "full_index", result.error
    with manager.dispatcher._semantic_registry.lease(repo_id) as indexer:
        points, _ = indexer.qdrant.scroll(indexer.collection, with_vectors=True, limit=100)
        assert len([point for point in points if point.id != indexer.PROVENANCE_POINT_ID]) == 4
    assert_public_semantic_queries()

    current = manager._resolve_ctx(repo_id)
    metadata_file = (
        SemanticIndexerRegistry.generation_root(current.registry_entry) / ".index_metadata.json"
    )
    original_metadata = metadata_file.read_text()
    (repo / ".index_metadata.json").write_text(original_metadata)
    metadata_file.unlink()
    assert not ReadinessClassifier.classify_semantic_registered(
        current.registry_entry, current.sqlite_store
    ).ready
    incomplete = json.loads(original_metadata)
    incomplete["semantic_profiles"]["fixture"].pop("compatibility_fingerprint")
    incomplete["semantic_profiles"]["fixture"].pop("compatibility_hash")
    metadata_file.write_text(json.dumps(incomplete))
    assert not ReadinessClassifier.classify_semantic_registered(
        current.registry_entry, current.sqlite_store
    ).ready
    metadata_file.write_text(original_metadata)
    assert ReadinessClassifier.classify_semantic_registered(
        current.registry_entry, current.sqlite_store
    ).ready


def test_artifact_restore_preserves_local_imports_and_cleanup_debt(runtime, monkeypatch):
    import json
    from datetime import datetime, timezone

    from mcp_server.artifacts.artifact_download import IndexArtifactDownloader
    from mcp_server.artifacts.secure_export import SecureIndexExporter

    repo, registry, repo_id, _original, manager = runtime
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    active = manager._resolve_ctx(repo_id)
    extracted = repo.parent / "artifact-fixture"
    extracted.mkdir()
    SecureIndexExporter(repo_path=repo).create_filtered_database(
        active.sqlite_store.db_path, str(extracted / "current.db")
    )
    _seed_retained(active.sqlite_store, repo)
    # Hash the complete committed fixture, not the pre-checkpoint main DB alone.
    with active.sqlite_store._get_connection() as connection:
        assert connection.execute("PRAGMA wal_checkpoint(FULL)").fetchone()[0] == 0
    old_path = registry.get(repo_id).index_path
    before = old_path.read_bytes()
    commit = _get_head_commit(repo)
    (extracted / "artifact-metadata.json").write_text(
        json.dumps({"commit": commit, "timestamp": datetime.now(timezone.utc).isoformat()})
    )
    downloader = IndexArtifactDownloader(repo="fixture/repository", index_manager=manager)
    # The accepted SAFETY suite covers signature/integrity verification before this boundary.
    monkeypatch.setattr(downloader, "download_artifact", lambda *args, **kwargs: extracted)
    result = downloader.download_selected_artifact(
        {"id": 1},
        output_dir=extracted,
        repo_id=repo_id,
        repo_path=repo,
        target_commit=commit,
        tracked_branch="main",
        index_path=old_path,
        index_location=registry.get(repo_id).index_location,
    )
    assert result.installed_items == [str(registry.get(repo_id).index_path)]
    assert registry.get(repo_id).index_path != old_path
    assert old_path.read_bytes() == before
    restored = manager._resolve_ctx(repo_id).sqlite_store
    assert restored.get_chunk_summary("document:fixture")["summary_text"] == "retained summary"
    assert restored.get_semantic_point_ids("fixture", ["document:fixture"]) == [101]
    assert restored.get_pending_vector_deletions()
    assert restored.search_code_fts("hello")


@pytest.mark.parametrize("surface", ["symbol", "code"])
@pytest.mark.parametrize("state", ["match", "no_match", "dirty", "pending", "removed"])
def test_compatibility_coordinator_uses_real_admitted_generations(runtime, surface, state):
    import asyncio

    from mcp_server.dispatcher.cross_repo_coordinator import (
        CrossRepositorySearchCoordinator,
        SearchScope,
    )
    from mcp_server.storage.multi_repo_manager import MultiRepositoryManager

    repo, registry, repo_id, _store, manager = runtime
    (repo / "hello.py").write_text("def indexed_sentinel():\n    return 'indexed_sentinel'\n")
    subprocess.run(["git", "add", "hello.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic query fixture"], cwd=repo, check=True)
    manager.dispatcher._use_factory = True
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    info = registry.get(repo_id)
    owner = MultiRepositoryManager(central_index_path=registry.registry_path)
    coordinator = CrossRepositorySearchCoordinator(owner)
    query = "absent_sentinel" if state == "no_match" else "indexed_sentinel"
    if state == "dirty":
        (repo / "hello.py").write_text("dirty = 1\n")
    elif state == "pending":
        registry.update_staleness_reason(repo_id, "index_publication_pending")
    elif state == "removed":
        registry.unregister_repository(repo_id)
    try:
        scope = SearchScope(repositories=[repo_id])
        method = (
            coordinator._search_symbol_in_repository
            if surface == "symbol"
            else coordinator._search_code_in_repository
        )
        result = method(query, info, scope)
        if state in {"match", "no_match"}:
            assert result.error is None
            assert bool(result.results) == (state == "match")
        else:
            assert result.results == []
            assert result.code == "index_unavailable"
            assert result.safe_fallback == "native_search"
        public = asyncio.run(getattr(coordinator, f"search_{surface}")(query, scope=scope))
        if state in {"match", "no_match"}:
            assert bool(public.results) == (state == "match")
        else:
            assert public.results == []
            assert public.code == "index_unavailable"
            assert public.safe_fallback == "native_search"
    finally:
        owner.close()


def test_internal_cross_repo_coordinator_does_not_confuse_result_shapes(runtime):
    import asyncio

    from mcp_server.core.errors import MCPError
    from mcp_server.dispatcher.cross_repo_coordinator import (
        CrossRepositoryCoordinator,
        SearchContext,
    )
    from mcp_server.storage.multi_repo_manager import MultiRepositoryManager

    _repo, registry, repo_id, _store, manager = runtime
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    owner = MultiRepositoryManager(central_index_path=registry.registry_path)
    try:
        coordinator = CrossRepositoryCoordinator(
            owner, enable_semantic=False, enable_reranking=False
        )
        context = SearchContext("hello", "code", repositories=[repo_id], rerank=False)
        results = asyncio.run(coordinator.search(context))
        assert results and results[0].primary_repository == repo_id
        registry.update_staleness_reason(repo_id, "index_publication_pending")
        with pytest.raises(MCPError) as failure:
            asyncio.run(coordinator.search(context))
        assert failure.value.details["code"] == "index_unavailable"
    finally:
        owner.close()


def test_export_snapshots_wal_and_filters_owned_rows_without_leaking_paths(runtime):
    from mcp_server.artifacts.secure_export import SecureIndexExporter

    repo, _registry, _repo_id, store, _manager = runtime
    (repo / "nested").mkdir()
    (repo / "nested" / ".gitignore").write_text("*.py\n!public.py\n")
    repository_id = store.ensure_repository_row(repo)
    for name in ("nested/hidden.py", "nested/public.py"):
        file_id = store.store_file(repository_id, repo / name, name)
        symbol_id = store.store_symbol(file_id, name, "function", 1, 1)
        store.store_chunk(
            file_id=file_id,
            symbol_id=symbol_id,
            content=name,
            content_start=0,
            content_end=len(name),
            line_start=1,
            line_end=1,
            chunk_id=name,
            node_id=name,
            treesitter_file_id=name,
        )
        store.store_chunk_summary(name, file_id, 0, len(name), name, "fixture")
        store.upsert_semantic_point("fixture", name, file_id, "fixture")
    target = repo.parent / "export.db"
    exporter = SecureIndexExporter(repo_path=repo, index_path=store.db_path)
    assert exporter.create_filtered_database(store.db_path, str(target)) == (1, 1)
    with sqlite3.connect(target) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT relative_path FROM files").fetchall() == [
            ("nested/public.py",)
        ]
        assert connection.execute("SELECT chunk_hash FROM chunk_summaries").fetchall() == [
            ("nested/public.py",)
        ]
        assert (
            connection.execute("SELECT COUNT(*) FROM pending_vector_deletions").fetchone()[0] == 0
        )
    assert not (Path.cwd() / "excluded_files.log").exists()
    assert (
        len(store.get_semantic_point_ids("fixture", ["nested/hidden.py", "nested/public.py"])) == 2
    )


def test_export_explicit_missing_generation_never_falls_back_to_legacy(runtime):
    from mcp_server.artifacts.secure_export import SecureIndexExporter

    repo, _registry, _repo_id, store, _manager = runtime
    SQLiteStore.snapshot_database(Path(store.db_path), repo / "code_index.db")
    with pytest.raises(FileNotFoundError):
        SecureIndexExporter(repo_path=repo, index_path=repo / "absent.db").create_secure_archive(
            str(repo.parent / "archive.tar.gz")
        )


@pytest.mark.parametrize("escape", ["symlink", "parent"])
def test_export_rejects_vector_backend_escaping_generation(runtime, monkeypatch, escape):
    from mcp_server.artifacts.secure_export import SecureIndexExporter

    repo, _registry, _repo_id, store, _manager = runtime
    repository_id = store.ensure_repository_row(repo)
    file_id = store.store_file(repository_id, repo / "hello.py", "hello.py")
    store.store_chunk(
        file_id=file_id,
        content="hello",
        content_start=0,
        content_end=5,
        line_start=1,
        line_end=1,
        chunk_id="hello",
        node_id="hello",
        treesitter_file_id="fixture",
    )
    store.upsert_semantic_point("fixture", "hello", 901, "fixture")
    generation = Path(store.db_path).parent
    outside = generation.parent / "unowned-vectors"
    outside.mkdir()
    backend = generation / "vectors"
    if escape == "symlink":
        backend.symlink_to(outside, target_is_directory=True)
    else:
        backend = generation / ".." / "unowned-vectors"
    exporter = SecureIndexExporter(repo_path=repo, index_path=store.db_path)
    monkeypatch.setattr(
        exporter,
        "read_generation_metadata",
        lambda *args: {
            "semantic_profile": "fixture",
            "collection_name": "fixture",
            "semantic_profiles": {"fixture": {"attested": True}},
            "qdrant_path": str(backend),
        },
    )
    connect = MagicMock(side_effect=AssertionError("Unowned backend opened"))
    monkeypatch.setattr("qdrant_client.QdrantClient", connect)
    with pytest.raises(RuntimeError, match="not owned by this generation"):
        exporter._export_vectors(Path(store.db_path), repo.parent / "vectors.jsonl")
    connect.assert_not_called()


def test_hard_delete_records_vector_debt_and_clears_inbound_references(runtime):
    repo, _registry, _repo_id, store, _manager = runtime
    repository_id = store.ensure_repository_row(repo)
    removed = store.store_file(repository_id, repo / "hello.py", "hello.py")
    symbol = store.store_symbol(removed, "removed", "function", 1, 1)
    retained = store.store_file(repository_id, repo / "other.py", "other.py")
    store.store_reference(symbol, retained, 1)
    store.store_chunk(
        file_id=removed,
        content="removed",
        content_start=0,
        content_end=7,
        line_start=1,
        line_end=1,
        chunk_id="removed",
        node_id="removed",
        treesitter_file_id="fixture",
    )
    store.upsert_semantic_point("profile", "removed", 909, "original-owner")
    assert store.remove_file("hello.py", repository_id)
    assert store.get_semantic_point_ids("profile", ["removed"]) == []
    assert store.get_pending_vector_deletions()[0]["collection"] == "original-owner"
    with store._get_connection() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("drift", ["none", "content", "profile"])
def test_snapshot_retains_documents_and_only_valid_code_summaries(runtime, tmp_path, drift):
    repo, registry, repo_id, original, _manager = runtime
    document_id, _ = _seed_retained(original, repo)
    repo_row = original.ensure_repository_row(repo)
    code_id = original.store_file(repo_row, repo / "hello.py", "hello.py")
    original.store_chunk(
        file_id=code_id,
        content="hello",
        content_start=0,
        content_end=5,
        line_start=1,
        line_end=1,
        chunk_id="code:fixture",
        node_id="node:fixture",
        treesitter_file_id="ts:fixture",
    )
    original.store_chunk_summary(
        "code:fixture",
        code_id,
        0,
        5,
        "unchanged summary",
        llm_model="fixture",
        profile_id="fixture",
        is_authoritative=True,
    )
    original.upsert_semantic_point("fixture", "code:fixture", 303, "old-collection")
    before = Path(original.db_path).read_bytes()
    destination = tmp_path / "stage.db"
    SQLiteStore.snapshot_database(Path(original.db_path), destination)
    stage = SQLiteStore(str(destination), path_resolver=PathResolver(repo))
    try:
        stage.prepare_generation(
            {
                "hello.py": (
                    "changed"
                    if drift == "content"
                    else hashlib.sha256((repo / "hello.py").read_bytes()).hexdigest()
                )
            },
            "other" if drift == "profile" else "fixture",
        )
        assert Path(original.db_path).read_bytes() == before
        assert bool(stage.get_chunk_summary("code:fixture")) is (drift == "none")
        assert stage.get_chunk_summary("document:fixture")["file_id"] == document_id
        assert stage.search_code_fts("retained")
        assert stage.get_semantic_point_ids("fixture", ["document:fixture"]) == [101]
        assert stage.get_semantic_point_ids("fixture", ["code:fixture"]) == []
        assert {row["point_id"] for row in stage.get_pending_vector_deletions()} == {202, 303}
        with stage._get_connection() as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        stage.close()


def test_generation_removes_stale_auxiliary_bm25_rows_before_file_deletion(runtime, tmp_path):
    from mcp_server.indexer.bm25_indexer import BM25Indexer

    repo, _registry, _repo_id, original, _manager = runtime
    _seed_retained(original, repo)
    row = original.ensure_repository_row(repo)
    original.store_file(row, repo / "obsolete.py", "obsolete.py")
    bm25 = BM25Indexer(original)
    bm25.add_document(str(repo / "obsolete.py"), "obsolete sentinel", {})
    bm25.add_document(str(repo / "imported-note.md"), "retained sentinel", {})
    destination = tmp_path / "stage.db"
    SQLiteStore.snapshot_database(Path(original.db_path), destination)
    stage = SQLiteStore(str(destination), path_resolver=PathResolver(repo))
    try:
        stage.prepare_generation({}, "fixture")
        with stage._get_connection() as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert [row[0] for row in connection.execute("SELECT filepath FROM bm25_content")] == [
                str(repo / "imported-note.md")
            ]
            assert connection.execute("SELECT COUNT(*) FROM bm25_index_status").fetchone()[0] == 1
    finally:
        stage.close()


@pytest.mark.parametrize("surface", ["symbol", "code"])
@pytest.mark.parametrize("state", ["pending", "generation_race", "ready_no_match"])
def test_legacy_query_entrypoints_recheck_admission(runtime, monkeypatch, surface, state):
    from mcp_server.storage.multi_repo_manager import MultiRepositoryManager

    repo, registry, repo_id, store, _manager = runtime
    _seed_retained(store, repo)
    registry.update_indexed_commit(repo_id, _get_head_commit(repo), branch="main")
    manager = MultiRepositoryManager(registry.registry_path)
    query = (
        manager._search_repository if surface == "symbol" else manager._search_code_in_repository
    )
    method = "search_symbols" if surface == "symbol" else "search_bm25"
    active = registry.get(repo_id)

    def search(*args, **kwargs):
        if state == "generation_race":
            registry.publish_generation(
                repo_id,
                generation="replacement",
                index_path=active.index_path,
                commit=active.current_commit,
                branch="main",
                profile=active.index_profile,
                expected_registration_id=active.registration_id,
                expected_generation=active.index_generation,
            )
        return []

    spy = MagicMock(side_effect=search)
    monkeypatch.setattr(store, method, spy)
    monkeypatch.setattr(manager, "_get_connection", lambda _repo_id: store)
    if state == "pending":
        registry.begin_generation_mutation(
            repo_id,
            expected_registration_id=active.registration_id,
            expected_generation=active.index_generation,
        )
    try:
        result = query(repo_id, "absent", None, 5)
        assert result.results == []
        if state == "ready_no_match":
            assert result.error is None
        else:
            assert result.code == "index_unavailable"
            assert result.safe_fallback == "native_search"
        assert spy.call_count == (0 if state == "pending" else 1)
    finally:
        manager._store_registry.shutdown()


@pytest.mark.parametrize(
    "failure", ["none", "missing_point", "upsert", "unacknowledged", "cleanup"]
)
def test_retained_vector_copy_uses_real_generation_clients_without_active_mutation(
    runtime, tmp_path, monkeypatch, failure
):
    from qdrant_client import models

    from mcp_server.utils.semantic_indexer_registry import SemanticIndexerRegistry
    from tests.test_embedding_provenance import _FakeProvenanceProvider, _openai_response, _profile

    repo, repos, repo_id, store, manager = runtime
    monkeypatch.delenv("QDRANT_URL", raising=False)
    settings = MagicMock()
    settings.get_semantic_profiles_config.return_value = {"fixture": _profile().to_dict()}
    settings.get_semantic_default_profile.return_value = "fixture"
    monkeypatch.setattr("mcp_server.utils.semantic_indexer_registry.get_settings", lambda: settings)
    monkeypatch.setattr("mcp_server.config.settings.get_settings", lambda: settings)
    monkeypatch.setattr(
        "mcp_server.utils.semantic_indexer.create_embedding_provider",
        lambda **kwargs: _FakeProvenanceProvider(_openai_response),
    )
    registry = SemanticIndexerRegistry(repos)
    manager.dispatcher._semantic_registry = registry
    active = repos.get(repo_id)
    stage = None
    try:
        with registry.lease(repo_id) as original:
            original._prepare_for_writes()
            points = [
                models.PointStruct(id=i, vector=[float(i + 1)] + [1.0] * 7, payload={"sentinel": i})
                for i in range(257)
            ]
            original.qdrant.upsert(original.collection, points, wait=True)
            for i in range(257):
                store.upsert_semantic_point("fixture", f"document:{i}", i, original.collection)
            original_metadata = Path(original.metadata_file).read_bytes()
            original_db = Path(store.db_path).read_bytes()
            stage_path = tmp_path / "stage.db"
            SQLiteStore.snapshot_database(Path(store.db_path), stage_path)
            stage = SQLiteStore(str(stage_path), path_resolver=PathResolver(repo))
            staged_info = replace(active, index_path=stage_path, index_generation="unpublished")
            ctx = RepoContext(repo_id, stage, repo, "main", staged_info, staging=True)
            with registry.lease(repo_id, ctx=ctx) as staged:
                if failure == "missing_point":
                    original.qdrant.delete(
                        original.collection, models.PointIdsList(points=[256]), wait=True
                    )
                elif failure == "upsert":
                    monkeypatch.setattr(
                        staged.qdrant, "upsert", MagicMock(side_effect=OSError("write fault"))
                    )
                elif failure == "unacknowledged":
                    from types import SimpleNamespace

                    monkeypatch.setattr(
                        staged.qdrant,
                        "upsert",
                        MagicMock(return_value=SimpleNamespace(status="acknowledged")),
                    )
                if failure in {"none", "cleanup"}:
                    manager._copy_retained_vectors(repo_id, ctx)
                    assert staged.qdrant.count(staged.collection).count == 257
                    for point in staged.qdrant.retrieve(
                        staged.collection, [0, 256], with_vectors=True
                    ):
                        assert point.payload["sentinel"] == point.id
                        source = original.qdrant.retrieve(
                            original.collection, [point.id], with_vectors=True
                        )[0]
                        assert point.vector == pytest.approx(source.vector)
                    with stage._get_connection() as connection:
                        assert {
                            row[0]
                            for row in connection.execute("SELECT collection FROM semantic_points")
                        } == {staged.collection}
                        stage._record_pending_vector_deletions(
                            connection,
                            [
                                {
                                    "profile_id": "fixture",
                                    "chunk_id": "document:0",
                                    "point_id": 0,
                                    "collection": staged.collection,
                                },
                                {
                                    "profile_id": "fixture",
                                    "chunk_id": "old-owner",
                                    "point_id": 0,
                                    "collection": original.collection,
                                },
                            ],
                        )
                        connection.execute(
                            "DELETE FROM semantic_points WHERE chunk_id='document:0'"
                        )
                    if failure == "cleanup":
                        monkeypatch.setattr(
                            staged.qdrant, "delete", MagicMock(side_effect=OSError("delete fault"))
                        )
                        with pytest.raises(RuntimeError):
                            manager._finalize_staged_vectors(repo_id, ctx)
                        assert len(stage.get_pending_vector_deletions()) == 2
                    else:
                        manager._finalize_staged_vectors(repo_id, ctx)
                        assert (
                            staged.qdrant.count(staged.collection).count == 257
                        )  # Includes provenance sentinel.
                        assert {
                            row["collection"] for row in stage.get_pending_vector_deletions()
                        } == {original.collection}
                else:
                    with pytest.raises((RuntimeError, OSError)):
                        manager._copy_retained_vectors(repo_id, ctx)
                assert Path(original.metadata_file).read_bytes() == original_metadata
                assert Path(store.db_path).read_bytes() == original_db
                assert original.qdrant.count(original.collection).count == (
                    256 if failure == "missing_point" else 257
                )
                assert repos.get(repo_id).index_generation == active.index_generation
    finally:
        registry.shutdown()
        manager.dispatcher._semantic_registry = None
        if stage is not None:
            stage.close()
