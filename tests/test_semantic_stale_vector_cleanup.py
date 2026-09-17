"""Tests for profile-scoped stale vector cleanup and mapping writes."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_server.core.path_resolver import PathResolver
from mcp_server.storage.sqlite_store import SQLiteStore
from mcp_server.utils.semantic_indexer import SemanticIndexer


class _FakeEmbeddingProvider:
    provider_name = "voyage"

    def __init__(self, dimension: int = 8) -> None:
        self.dimension = dimension

    def embed(self, texts, input_type="document"):
        del input_type
        return [[0.0] * self.dimension for _ in texts]


class _FakeQdrantClient:
    def __init__(self) -> None:
        self.deleted = []
        self.upserted = []

    def delete(self, collection_name: str, points_selector, *, wait=False) -> None:
        self.deleted.append(
            {
                "collection": collection_name,
                "points": list(points_selector.points),
                "wait": wait,
            }
        )

    def upsert(self, collection_name: str, points) -> None:
        self.upserted.append(
            {
                "collection": collection_name,
                "points": points,
            }
        )


def _build_indexer(repo_path: Path, qdrant: _FakeQdrantClient) -> SemanticIndexer:
    indexer = SemanticIndexer.__new__(SemanticIndexer)
    indexer.embedding_client = _FakeEmbeddingProvider(8)
    indexer.embedding_provider = "voyage"
    indexer.embedding_model = "voyage-code-3"
    indexer.embedding_dimension = 8
    indexer.path_resolver = PathResolver(repo_path)
    indexer.qdrant = qdrant
    indexer.collection = "code-index"
    indexer._qdrant_available = True
    indexer.semantic_profile = SimpleNamespace(profile_id="test-profile")
    return indexer


def test_delete_stale_vectors_deletes_qdrant_points_and_mappings(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    store = SQLiteStore(str(tmp_path / "index.db"), path_resolver=PathResolver(repo_path))
    qdrant = _FakeQdrantClient()
    indexer = _build_indexer(repo_path, qdrant)

    store.upsert_semantic_point("test-profile", "chunk-1", 101, "code-index")
    store.upsert_semantic_point("test-profile", "chunk-2", 202, "code-index")

    deleted = indexer.delete_stale_vectors(
        profile_id="test-profile",
        chunk_ids=["chunk-1", "chunk-2"],
        sqlite_store=store,
    )

    assert deleted == 2
    assert qdrant.deleted[-1]["points"] == [101, 202]
    assert store.get_semantic_point_ids("test-profile", ["chunk-1", "chunk-2"]) == []


def test_index_symbol_writes_mapping_for_chunk_metadata(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    source_file = repo_path / "doc.md"
    source_file.write_text("# title\nhello")

    store = SQLiteStore(str(tmp_path / "index.db"), path_resolver=PathResolver(repo_path))
    qdrant = _FakeQdrantClient()
    indexer = _build_indexer(repo_path, qdrant)

    content = "chunk content"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    expected_point_id = indexer._symbol_id(
        str(source_file),
        "chunk-1",
        1,
        content_hash,
    )

    indexer.index_symbol(
        file=str(source_file),
        name="chunk-1",
        kind="chunk",
        signature="Chunk 1",
        line=1,
        span=(1, 2),
        content=content,
        metadata={"chunk_id": "chunk-1"},
        sqlite_store=store,
    )

    assert qdrant.upserted
    assert store.get_semantic_point_ids("test-profile", ["chunk-1"]) == [expected_point_id]


def test_cleanup_stale_semantic_artifacts_deletes_mappings_and_invalidated_summaries(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    store = SQLiteStore(str(tmp_path / "index.db"), path_resolver=PathResolver(repo_path))
    qdrant = _FakeQdrantClient()
    indexer = _build_indexer(repo_path, qdrant)

    repo_id = store.create_repository(str(repo_path), "repo")
    file_path = repo_path / "file.py"
    file_path.write_text("value = 1\n")
    file_id = store.store_file(repo_id, file_path, relative_path="file.py", language="python")
    store.store_chunk(
        file_id=file_id,
        content="value = 1",
        content_start=0,
        content_end=9,
        line_start=1,
        line_end=1,
        chunk_id="chunk-1",
        node_id="node-1",
        treesitter_file_id="ts-1",
    )
    store.store_chunk_summary(
        chunk_hash="chunk-1",
        file_id=file_id,
        chunk_start=0,
        chunk_end=9,
        summary_text="stale summary",
        llm_model="chat",
        is_authoritative=True,
        profile_id="test-profile",
        prompt_fingerprint="prompt-old",
        audit_metadata={"profile_id": "test-profile", "prompt_fingerprint": "prompt-old"},
    )
    store.upsert_semantic_point("test-profile", "chunk-1", 101, "code-index")
    store.upsert_semantic_point("test-profile", "chunk-1:part:1:1", 102, "code-index")
    store.upsert_semantic_point("test-profile", "file.py:file-summary", 103, "code-index")

    cleanup = indexer.cleanup_stale_semantic_artifacts(
        profile_id="test-profile",
        invalidation={
            "vector_chunk_ids": ["chunk-1", "chunk-1:part:1:1", "file.py:file-summary"],
            "summary_chunk_ids_to_delete": ["chunk-1"],
            "summary_chunk_ids_preserved": [],
            "requires_summary_regeneration": True,
        },
        sqlite_store=store,
    )

    assert cleanup["vectors_deleted"] == 3
    assert cleanup["mappings_deleted"] == 3
    assert cleanup["summaries_deleted"] == 1
    assert qdrant.deleted[-1]["points"] == [101, 102, 103]
    assert (
        store.get_semantic_point_ids(
            "test-profile",
            ["chunk-1", "chunk-1:part:1:1", "file.py:file-summary"],
        )
        == []
    )
    assert store.get_chunk_summary("chunk-1") is None


def test_cleanup_targets_recorded_collection_with_acknowledged_delete(tmp_path):
    store = SQLiteStore(str(tmp_path / "index.db"))
    client = _FakeQdrantClient()
    indexer = _build_indexer(tmp_path, client)
    try:
        store.upsert_semantic_point("test-profile", "chunk", 101, "original-collection")
        assert indexer.delete_stale_vectors("test-profile", ["chunk"], store) == 1
        assert client.deleted[-1] == {
            "collection": "original-collection",
            "points": [101],
            "wait": True,
        }
    finally:
        store.close()


def test_staged_cleanup_cannot_mutate_old_collection_or_clear_mapping(tmp_path):
    store = SQLiteStore(str(tmp_path / "index.db"))
    client = _FakeQdrantClient()
    indexer = _build_indexer(tmp_path, client)
    indexer.staging = True
    try:
        store.upsert_semantic_point("test-profile", "chunk", 101, "old-generation")
        with pytest.raises(RuntimeError):
            indexer.delete_stale_vectors("test-profile", ["chunk"], store)
        with pytest.raises(RuntimeError):
            indexer.delete_remote_points([101], collection="old-generation")
        assert store.get_semantic_point_ids("test-profile", ["chunk"]) == [101]
        assert not client.deleted
    finally:
        store.close()


def test_failed_remote_delete_preserves_mapping_and_redacts_error(tmp_path, monkeypatch):
    store = SQLiteStore(str(tmp_path / "index.db"))
    client = _FakeQdrantClient()
    indexer = _build_indexer(tmp_path, client)

    def fail(**kwargs):
        raise OSError("private response body")

    monkeypatch.setattr(client, "delete", fail)
    try:
        store.upsert_semantic_point("test-profile", "chunk", 101, "code-index")
        with pytest.raises(RuntimeError) as error:
            indexer.delete_stale_vectors("test-profile", ["chunk"], store)
        assert "private" not in str(error.value)
        assert store.get_semantic_point_ids("test-profile", ["chunk"]) == [101]
    finally:
        store.close()
