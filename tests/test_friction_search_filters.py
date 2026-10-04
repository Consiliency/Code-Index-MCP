from types import SimpleNamespace

import pytest
from qdrant_client import models

from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher, SemanticSearchFailure
from mcp_server.indexing.source_metadata import merge_source_metadata
from tests.test_embedding_provenance import _FakeProvenanceProvider, _items, _openai_response
from tests.test_history_issue_storage import _history_record
from tests.test_v13_data_vectors import real_indexer


def test_unfiltered_lexical_search_keeps_legacy_shape(sqlite_store, tmp_path):
    repo_id = sqlite_store.create_repository(str(tmp_path), "repo")
    file_id = sqlite_store.store_file(
        repo_id, str(tmp_path / "main.py"), "main.py", language="python"
    )
    sqlite_store.store_chunk(
        file_id=file_id,
        content="def demo():\n    return 'demo'",
        content_start=0,
        content_end=29,
        line_start=1,
        line_end=2,
        chunk_id="chunk-1",
        node_id="node-1",
        treesitter_file_id="ts-1",
    )
    with sqlite_store._get_connection() as conn:
        conn.execute("INSERT INTO fts_code (content, file_id) VALUES (?, ?)", ("demo", file_id))

    ctx = SimpleNamespace(
        sqlite_store=sqlite_store,
        repo_id="repo-id",
        registry_entry=SimpleNamespace(path=tmp_path, name="repo"),
        workspace_root=tmp_path,
    )
    dispatcher = EnhancedDispatcher()

    results = list(dispatcher.search(ctx, "demo", semantic=False, limit=5))

    assert results
    assert "source_metadata" not in results[0]


def test_friction_filtered_search_returns_only_matching_chunks(sqlite_store, tmp_path):
    repo_id = sqlite_store.create_repository(str(tmp_path), "repo")
    file_id = sqlite_store.store_file(
        repo_id, str(tmp_path / "main.py"), "main.py", language="python"
    )
    sqlite_store.store_chunk(
        file_id=file_id,
        content="# TODO: first",
        content_start=0,
        content_end=13,
        line_start=1,
        line_end=1,
        chunk_id="chunk-1",
        node_id="node-1",
        treesitter_file_id="ts-1",
    )
    sqlite_store.store_chunk(
        file_id=file_id,
        content="# FIXME: second",
        content_start=14,
        content_end=29,
        line_start=2,
        line_end=2,
        chunk_id="chunk-2",
        node_id="node-2",
        treesitter_file_id="ts-2",
    )
    ctx = SimpleNamespace(
        sqlite_store=sqlite_store,
        repo_id="repo-id",
        registry_entry=SimpleNamespace(path=tmp_path, name="repo"),
        workspace_root=tmp_path,
    )
    dispatcher = EnhancedDispatcher()

    results = list(
        dispatcher.search(
            ctx,
            "second",
            source_type="friction",
            friction_categories=["fixme"],
            limit=5,
        )
    )

    assert len(results) == 1
    assert results[0]["source_metadata"]["records"][0]["category"] == "fixme"


def test_friction_filtered_search_finds_matches_after_many_plain_chunks(sqlite_store, tmp_path):
    repo_id = sqlite_store.create_repository(str(tmp_path), "repo")
    file_id = sqlite_store.store_file(
        repo_id, str(tmp_path / "main.py"), "main.py", language="python"
    )
    for index in range(120):
        sqlite_store.store_chunk(
            file_id=file_id,
            content=f"def filler_{index}():\n    return {index}",
            content_start=index * 30,
            content_end=(index + 1) * 30,
            line_start=index + 1,
            line_end=index + 1,
            chunk_id=f"plain-{index}",
            node_id=f"plain-{index}",
            treesitter_file_id=f"plain-{index}",
        )
    sqlite_store.store_chunk(
        file_id=file_id,
        content="# TODO: late marker",
        content_start=4000,
        content_end=4019,
        line_start=200,
        line_end=200,
        chunk_id="late-marker",
        node_id="late-marker",
        treesitter_file_id="late-marker",
    )
    dispatcher = EnhancedDispatcher()

    results = list(
        dispatcher.search(
            SimpleNamespace(
                sqlite_store=sqlite_store,
                repo_id="repo-id",
                registry_entry=SimpleNamespace(path=tmp_path, name="repo"),
                workspace_root=tmp_path,
            ),
            "late marker",
            source_type="friction",
            friction_categories=["todo"],
            limit=5,
        )
    )

    assert len(results) == 1
    assert results[0]["line"] == 200
    assert results[0]["source_metadata"]["records"][0]["category"] == "todo"


def test_include_source_metadata_enriches_unfiltered_results(sqlite_store, tmp_path):
    repo_id = sqlite_store.create_repository(str(tmp_path), "repo")
    file_id = sqlite_store.store_file(
        repo_id, str(tmp_path / "main.py"), "main.py", language="python"
    )
    sqlite_store.store_chunk(
        file_id=file_id,
        content="# TODO: first",
        content_start=0,
        content_end=13,
        line_start=1,
        line_end=1,
        chunk_id="chunk-1",
        node_id="node-1",
        treesitter_file_id="ts-1",
    )
    with sqlite_store._get_connection() as conn:
        conn.execute("INSERT INTO fts_code (content, file_id) VALUES (?, ?)", ("TODO", file_id))
    ctx = SimpleNamespace(
        sqlite_store=sqlite_store,
        repo_id="repo-id",
        registry_entry=SimpleNamespace(path=tmp_path, name="repo"),
        workspace_root=tmp_path,
    )
    dispatcher = EnhancedDispatcher()

    results = list(dispatcher.search(ctx, "TODO", include_source_metadata=True, limit=5))

    assert results[0]["source_metadata"]["records"][0]["category"] == "todo"


@pytest.mark.parametrize("source_type", ["friction", "history"])
def test_source_filtered_semantic_query_ranks_complete_candidates(
    sqlite_store, tmp_path, monkeypatch, real_indexer, source_type
):
    repo_id = sqlite_store.create_repository(str(tmp_path), "repo")
    file_id = sqlite_store.store_file(repo_id, str(tmp_path / "notes.py"), "notes.py")
    points = []
    for index in range(1, 1003):
        excluded = index == 1002
        metadata = None
        if source_type == "history":
            record = _history_record(
                index,
                "reflection",
                "2026-07-05T00:00:00Z",
                repo="other/repo" if excluded else "owner/repo",
            )
            metadata = merge_source_metadata({}, [record])
            content = f"Historical observation {index}"
        else:
            content = f"# {'FIXME' if excluded else 'TODO'}: ordinary observation {index}"
        sqlite_store.store_chunk(
            file_id=file_id,
            content=content,
            content_start=index * 100,
            content_end=index * 100 + len(content),
            line_start=index,
            line_end=index,
            chunk_id=f"source-{index}",
            node_id=f"source-{index}",
            treesitter_file_id="notes",
            metadata=metadata,
        )
        points.append(
            models.PointStruct(
                id=index,
                vector=([1.0] * 8 if index >= 1001 else [1.0] + [0.0] * 7),
                payload={
                    "source_chunk_id": f"source-{index}",
                    "chunk_id": f"source-{index}:part:1:2",
                    "file": "notes.py",
                    "line": index,
                },
            )
        )
    real_indexer.qdrant.upsert(collection_name=real_indexer.collection, points=points, wait=True)
    real_indexer.embedding_client = _FakeProvenanceProvider(
        lambda texts, input_type: _openai_response(
            texts, input_type, items=_items([[1.0] * 8 for _ in texts])
        )
    )
    dispatcher = EnhancedDispatcher()
    monkeypatch.setattr(dispatcher, "_get_semantic_indexer", lambda ctx: real_indexer)
    ctx = SimpleNamespace(
        sqlite_store=sqlite_store,
        repo_id="repo-id",
        workspace_root=tmp_path,
        registry_entry=SimpleNamespace(path=tmp_path, name="repo"),
    )
    filters = (
        {"friction_categories": ["todo"]}
        if source_type == "friction"
        else {"history_labels": ["reflection"], "history_repos": ["owner/repo"]}
    )
    try:
        assert not list(dispatcher.search(ctx, "concept", source_type=source_type, **filters))
        results = list(
            dispatcher.search(
                ctx, "concept", semantic=True, source_type=source_type, limit=1, **filters
            )
        )
        assert len(results) == 1
        assert results[0]["line"] == 1001
        assert results[0]["score"] > 0.9
        assert results[0]["source_metadata"]["records"][0]["source_type"] == source_type
        assert results[0]["semantic_profile_id"] == real_indexer.semantic_profile.profile_id
        assert "chunk_id" not in results[0]
        calls = list(real_indexer.embedding_client.calls)
        assert not list(
            dispatcher.search(
                ctx, "concept", semantic=True, source_type="history", history_repos=["missing/repo"]
            )
        )
        assert real_indexer.embedding_client.calls == calls
    finally:
        dispatcher.shutdown()


@pytest.mark.parametrize("failure", ["unavailable", "query_failed"])
def test_source_filtered_semantic_failure_never_returns_lexical_results(
    sqlite_store, tmp_path, monkeypatch, failure
):
    repo_id = sqlite_store.create_repository(str(tmp_path), "repo")
    file_id = sqlite_store.store_file(repo_id, str(tmp_path / "note.py"), "note.py")
    sqlite_store.store_chunk(
        file_id=file_id,
        content="# TODO: concept",
        content_start=0,
        content_end=15,
        line_start=1,
        line_end=1,
        chunk_id="source",
        node_id="source",
        treesitter_file_id="note",
    )
    dispatcher = EnhancedDispatcher()

    def fail_query(**kwargs):
        raise RuntimeError("synthetic provider failure")

    indexer = None if failure == "unavailable" else SimpleNamespace(search=fail_query)
    monkeypatch.setattr(dispatcher, "_get_semantic_indexer", lambda ctx: indexer)
    ctx = SimpleNamespace(
        sqlite_store=sqlite_store,
        repo_id="repo-id",
        workspace_root=tmp_path,
        registry_entry=SimpleNamespace(path=tmp_path, name="repo"),
    )
    try:
        with pytest.raises(SemanticSearchFailure):
            list(dispatcher.search(ctx, "concept", semantic=True, source_type="friction"))
    finally:
        dispatcher.shutdown()
