"""Focused tests for profile-aware semantic indexer behavior."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mcp_server.artifacts.semantic_profiles import SemanticProfileRegistry
from mcp_server.utils import semantic_indexer as semantic_indexer_module
from mcp_server.utils.semantic_indexer import SemanticIndexer, ensure_qdrant_collection


class _FakeEmbeddingProvider:
    def __init__(self, provider_name: str, dimension: int) -> None:
        self._provider_name = provider_name
        self._dimension = dimension

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def embed(self, texts, input_type="document"):
        del input_type
        return [[0.0] * self._dimension for _ in texts]


class _FakeQdrantClient:
    def __init__(self) -> None:
        self.upserts = []
        self.collections = {}

    def upsert(self, *, collection_name, points, wait=False):
        self.upserts.append((collection_name, list(points)))
        return SimpleNamespace(status="completed")

    def get_collections(self):
        return SimpleNamespace(
            collections=[SimpleNamespace(name=name) for name in self.collections.keys()]
        )

    def get_collection(self, collection_name):
        size, distance = self.collections[collection_name]
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors=SimpleNamespace(size=size, distance=distance))
            )
        )

    def create_collection(self, *, collection_name, vectors_config):
        self.collections[collection_name] = (vectors_config.size, vectors_config.distance)

    def recreate_collection(self, *, collection_name, vectors_config):
        self.collections[collection_name] = (vectors_config.size, vectors_config.distance)


def _sample_profiles() -> dict[str, dict[str, object]]:
    return {
        "commercial-high": {
            "provider": "voyage",
            "model_name": "voyage-code-3",
            "model_version": "2025-01",
            "vector_dimension": 1024,
            "distance_metric": "cosine",
            "normalization_policy": "provider-default",
            "chunk_schema_version": "2.0",
            "chunker_version": "treesitter-v2",
            "build_metadata": {"collection_name": "semantic-commercial-high"},
        },
        "oss-high": {
            "provider": "openai_compatible",
            "model_name": "Qwen/Qwen3-Embedding-8B",
            "model_version": "vllm-local",
            "vector_dimension": 4096,
            "distance_metric": "cosine",
            "normalization_policy": "none",
            "chunk_schema_version": "2.1",
            "chunker_version": "treesitter-v3",
            "build_metadata": {"collection_name": "semantic-oss-high"},
        },
    }


def _patch_indexer_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)

    # The default PathResolver auto-detects the repository root by walking up to
    # the nearest .git, which under pytest's tmp_path lands on /tmp and prefixes
    # every repo-relative chunk id with the pytest tmp directory. Pin detection to
    # tmp_path so relative paths (and the chunk ids derived from them) are stable.
    from mcp_server.core.path_resolver import PathResolver

    monkeypatch.setattr(PathResolver, "_detect_repository_root", lambda self: Path(tmp_path))

    def _fake_init_qdrant(self, qdrant_path):
        self._qdrant_available = True
        self._connection_mode = "memory"
        return _FakeQdrantClient()

    monkeypatch.setattr(SemanticIndexer, "_init_qdrant_client", _fake_init_qdrant)
    monkeypatch.setattr(SemanticIndexer, "_ensure_collection", lambda self: None)
    monkeypatch.setattr(SemanticIndexer, "_get_git_commit_hash", lambda self: "deadbeef")

    def _fake_provider(
        provider_name: str,
        model_name: str,
        vector_dimension: int,
        api_key=None,
        base_url=None,
    ):
        del model_name, api_key, base_url
        normalized = provider_name.strip().lower()
        if normalized in {"voyage", "voyageai"}:
            return _FakeEmbeddingProvider("voyage", vector_dimension)
        return _FakeEmbeddingProvider("openai_compatible", vector_dimension)

    monkeypatch.setattr(
        semantic_indexer_module,
        "create_embedding_provider",
        _fake_provider,
    )


class _FakeSQLiteStore:
    def __init__(self, summary_text: str | None) -> None:
        self.summary_text = summary_text
        self.semantic_points = []

    def get_chunk_summary(self, _chunk_id: str):
        if self.summary_text is None:
            return None
        return {"summary_text": self.summary_text}

    def upsert_semantic_point(self, **kwargs):
        self.semantic_points.append(kwargs)


def _patch_chunk_file(monkeypatch, chunk_id: str = "chunk-1") -> None:
    def _fake_chunk_file(*_args, **_kwargs):
        return [
            SimpleNamespace(
                content="def alpha(x):\n    return x + 1\n",
                metadata={"symbol": "alpha", "kind": "function", "signature_text": "alpha(x)"},
                start_line=1,
                end_line=2,
                node_type="function_definition",
                chunk_id=chunk_id,
                node_id=chunk_id,
            )
        ]

    monkeypatch.setattr(semantic_indexer_module, "chunk_file", _fake_chunk_file)


def test_profile_configuration_is_selected_from_registry(monkeypatch, tmp_path):
    _patch_indexer_runtime(monkeypatch, tmp_path)
    registry = SemanticProfileRegistry.from_raw(_sample_profiles(), "commercial-high")

    indexer = SemanticIndexer(
        collection="code-index",
        qdrant_path=":memory:",
        profile_registry=registry,
        semantic_profile="oss-high",
        repo_identifier="https://github.com/acme/repo.git",
        branch="main",
        commit="abcdef123456",
    )

    assert indexer.embedding_provider == "openai_compatible"
    assert indexer.embedding_model == "Qwen/Qwen3-Embedding-8B"
    assert indexer.embedding_model_version == "vllm-local"
    assert indexer.embedding_dimension == 4096
    assert indexer.distance_metric == "cosine"
    assert indexer.normalization_policy == "none"
    assert indexer.chunk_schema_version == "2.1"
    assert "__oss-high__" in indexer.collection


def test_legacy_fallback_remains_compatible(monkeypatch, tmp_path):
    _patch_indexer_runtime(monkeypatch, tmp_path)

    indexer = SemanticIndexer(collection="legacy-collection", qdrant_path=":memory:")

    assert indexer.collection == "legacy-collection"
    assert indexer.embedding_model == "voyage-code-3"
    assert indexer.embedding_dimension == 1024
    assert indexer.distance_metric == "cosine"

    expected = hashlib.sha256("voyage-code-3:1024:cosine".encode("utf-8")).hexdigest()[:16]
    assert indexer._generate_compatibility_hash() == expected

    metadata = indexer._build_metadata()
    assert "semantic_profile" not in metadata
    assert "compatibility_fingerprint" not in metadata


def test_compatibility_metadata_differs_by_profile(monkeypatch, tmp_path):
    _patch_indexer_runtime(monkeypatch, tmp_path)
    registry = SemanticProfileRegistry.from_raw(_sample_profiles(), "commercial-high")

    commercial = SemanticIndexer(
        collection="code-index",
        qdrant_path=":memory:",
        profile_registry=registry,
        semantic_profile="commercial-high",
    )
    oss = SemanticIndexer(
        collection="code-index",
        qdrant_path=":memory:",
        profile_registry=registry,
        semantic_profile="oss-high",
    )

    commercial_metadata = commercial._build_metadata()
    oss_metadata = oss._build_metadata()

    assert commercial_metadata["compatibility_hash"] != oss_metadata["compatibility_hash"]
    assert (
        commercial_metadata["compatibility_fingerprint"]
        != oss_metadata["compatibility_fingerprint"]
    )
    assert commercial.collection == "semantic-commercial-high"
    assert oss.collection == "semantic-oss-high"


def test_metadata_file_accumulates_multiple_semantic_profiles(monkeypatch, tmp_path):
    _patch_indexer_runtime(monkeypatch, tmp_path)
    registry = SemanticProfileRegistry.from_raw(_sample_profiles(), "commercial-high")

    SemanticIndexer(
        collection="code-index",
        qdrant_path=":memory:",
        profile_registry=registry,
        semantic_profile="commercial-high",
    )
    SemanticIndexer(
        collection="code-index",
        qdrant_path=":memory:",
        profile_registry=registry,
        semantic_profile="oss-high",
    )

    metadata = json.loads((tmp_path / ".index_metadata.json").read_text(encoding="utf-8"))

    assert metadata["semantic_profile"] == "oss-high"
    assert sorted(metadata["semantic_profiles"].keys()) == [
        "commercial-high",
        "oss-high",
    ]
    assert (
        metadata["semantic_profiles"]["commercial-high"]["compatibility_fingerprint"]
        == registry.get("commercial-high").compatibility_fingerprint
    )
    assert (
        metadata["semantic_profiles"]["oss-high"]["compatibility_fingerprint"]
        == registry.get("oss-high").compatibility_fingerprint
    )
    assert (
        metadata["semantic_profiles"]["commercial-high"]["collection_name"]
        == "semantic-commercial-high"
    )
    assert metadata["semantic_profiles"]["oss-high"]["collection_name"] == "semantic-oss-high"


def test_strict_batch_indexing_refuses_writes_without_authoritative_summary(monkeypatch, tmp_path):
    _patch_indexer_runtime(monkeypatch, tmp_path)
    _patch_chunk_file(monkeypatch)
    registry = SemanticProfileRegistry.from_raw(_sample_profiles(), "oss-high")
    sqlite_store = _FakeSQLiteStore(summary_text=None)
    source = tmp_path / "sample.py"
    source.write_text("def alpha(x):\n    return x + 1\n", encoding="utf-8")

    indexer = SemanticIndexer(
        collection="code-index",
        qdrant_path=":memory:",
        profile_registry=registry,
        semantic_profile="oss-high",
        sqlite_store=sqlite_store,
    )

    result = indexer.index_files_batch([source], require_summaries=True)

    assert result["files_indexed"] == 0
    assert result["files_blocked"] == 1
    assert result["missing_summary_chunk_ids"] == ["chunk-1"]
    assert indexer.qdrant.upserts == []


@pytest.mark.parametrize("include_success", [False, True])
def test_batch_preparation_errors_are_failures_not_skips(monkeypatch, tmp_path, include_success):
    _patch_indexer_runtime(monkeypatch, tmp_path)
    _patch_chunk_file(monkeypatch)
    registry = SemanticProfileRegistry.from_raw(_sample_profiles(), "oss-high")
    source = tmp_path / "sample.py"
    source.write_text("def alpha(x):\n    return x + 1\n")
    indexer = SemanticIndexer(
        qdrant_path=":memory:",
        profile_registry=registry,
        semantic_profile="oss-high",
        sqlite_store=_FakeSQLiteStore(summary_text="Synthetic summary"),
    )
    prepare = indexer._prepare_file_for_indexing
    failed = tmp_path / "bad.py"

    def prepare_with_failure(path):
        if path == failed:
            raise OSError("private input")
        return prepare(path)

    monkeypatch.setattr(indexer, "_prepare_file_for_indexing", prepare_with_failure)
    provenance = Mock()
    monkeypatch.setattr(indexer, "_write_collection_provenance_best_effort", provenance)
    result = indexer.index_files_batch([failed, source] if include_success else [failed])
    assert result["files_failed"] == 1
    assert result["files_skipped"] == 0
    assert result["files_indexed"] == int(include_success)
    provenance.assert_not_called()


def test_strict_preparation_includes_summary_text_in_embedding_input(monkeypatch, tmp_path):
    _patch_indexer_runtime(monkeypatch, tmp_path)
    _patch_chunk_file(monkeypatch)
    registry = SemanticProfileRegistry.from_raw(_sample_profiles(), "oss-high")
    sqlite_store = _FakeSQLiteStore(summary_text="Summarizes alpha and its input")
    source = tmp_path / "sample.py"
    source.write_text("def alpha(x):\n    return x + 1\n", encoding="utf-8")

    indexer = SemanticIndexer(
        collection="code-index",
        qdrant_path=":memory:",
        profile_registry=registry,
        semantic_profile="oss-high",
        sqlite_store=sqlite_store,
    )

    prep = indexer._prepare_file_for_indexing(Path(source))

    assert prep is not None
    assert any("Summarizes alpha and its input" in text for text in prep["embedding_inputs"])
    assert any("return x + 1" in text for text in prep["embedding_inputs"])


def test_successful_strict_batch_indexing_persists_chunk_point_links(monkeypatch, tmp_path):
    _patch_indexer_runtime(monkeypatch, tmp_path)
    _patch_chunk_file(monkeypatch, chunk_id="chunk-link")
    registry = SemanticProfileRegistry.from_raw(_sample_profiles(), "oss-high")
    sqlite_store = _FakeSQLiteStore(summary_text="Summarizes alpha and its input")
    source = tmp_path / "sample.py"
    source.write_text("def alpha(x):\n    return x + 1\n", encoding="utf-8")

    indexer = SemanticIndexer(
        collection="code-index",
        qdrant_path=":memory:",
        profile_registry=registry,
        semantic_profile="oss-high",
        sqlite_store=sqlite_store,
    )

    result = indexer.index_files_batch([source], require_summaries=True)

    assert result["files_indexed"] == 1
    assert {point["chunk_id"] for point in sqlite_store.semantic_points} == {
        "chunk-link:part:1:1",
        "sample.py:file-summary",
        "chunk-link",
    }
    assert all(point["profile_id"] == "oss-high" for point in sqlite_store.semantic_points)


def test_preflight_blocker_prevents_any_qdrant_upsert(monkeypatch, tmp_path):
    _patch_indexer_runtime(monkeypatch, tmp_path)
    _patch_chunk_file(monkeypatch)
    registry = SemanticProfileRegistry.from_raw(_sample_profiles(), "oss-high")
    sqlite_store = _FakeSQLiteStore(summary_text="Summarizes alpha and its input")
    source = tmp_path / "sample.py"
    source.write_text("def alpha(x):\n    return x + 1\n", encoding="utf-8")

    indexer = SemanticIndexer(
        collection="code-index",
        qdrant_path=":memory:",
        profile_registry=registry,
        semantic_profile="oss-high",
        sqlite_store=sqlite_store,
    )

    result = indexer.index_files_batch(
        [source],
        require_summaries=True,
        semantic_preflight={
            "can_write_semantic_vectors": False,
            "blocker": {"code": "collection_missing", "message": "Collection missing"},
        },
    )

    assert result["files_blocked"] == 1
    assert indexer.qdrant.upserts == []


def test_ensure_qdrant_collection_creates_missing_collection():
    client = _FakeQdrantClient()

    result = ensure_qdrant_collection(
        client,
        collection_name="semantic-oss-high",
        expected_dimension=4096,
        distance_metric="cosine",
        allow_recreate=False,
    )

    assert result.status == "created"
    assert "semantic-oss-high" in client.collections


def test_ensure_qdrant_collection_blocks_shape_mismatch_when_recreate_disallowed():
    client = _FakeQdrantClient()
    distance = SemanticIndexer.resolve_qdrant_distance("dot")
    client.collections["semantic-oss-high"] = (1024, distance)

    result = ensure_qdrant_collection(
        client,
        collection_name="semantic-oss-high",
        expected_dimension=4096,
        distance_metric="cosine",
        allow_recreate=False,
    )

    assert result.status == "blocked"
    assert result.actual_dimension == 1024


def _bare_reconcile_indexer(client, *, active_dimension: int):
    """A SemanticIndexer wired for the reconcile path without the heavy __init__.

    Attestation is stubbed to succeed (compatible probe); the collection-shape
    validation and recreate/persist behavior under test are the REAL methods.
    """
    indexer = SemanticIndexer.__new__(SemanticIndexer)
    indexer._qdrant_available = True
    indexer.qdrant = client
    indexer.collection = "semantic-oss-high"
    indexer.embedding_dimension = active_dimension
    indexer.distance_metric = "cosine"
    indexer._writes_prepared = False
    indexer._attestation = None

    writes: list = []
    attestation = SimpleNamespace(ok=True, to_dict=lambda: {"ok": True})
    indexer._provider_supports_provenance = lambda: True
    indexer._attest_active_profile = lambda **_kw: attestation
    indexer._attested_metadata = lambda _att: {
        "attested": True,
        "model_dimension": indexer.embedding_dimension,
    }
    indexer._atomic_write_metadata = lambda meta: writes.append(meta)
    return indexer, writes


def test_reconcile_compatible_probe_over_profile_migrated_shape_requires_reindex():
    """N1: a COMPATIBLE probe over a profile-migrated (dimension-changed) collection
    must NOT persist attested metadata over the stale shape.

    Repro: active profile is now 8-d and the provider attests OK, but the existing
    collection is the stale 16-d v9 one. Without ``allow_reindex`` the reconcile must
    return ``reindex_required`` and mutate/persist nothing; with ``allow_reindex`` it
    recreates to the active shape FIRST, then persists under the verified attestation.
    """
    cosine = SemanticIndexer.resolve_qdrant_distance("cosine")

    # ---- allow_reindex=False: reindex_required, nothing persisted / mutated ----
    client = _FakeQdrantClient()
    client.collections["semantic-oss-high"] = (16, cosine)  # stale v9 16-d shape
    indexer, writes = _bare_reconcile_indexer(client, active_dimension=8)

    result = indexer.reconcile_existing_collection()

    assert result["status"] == "reindex_required"
    assert "reindex" in result["message"].lower()
    # The bug was persisting attested=True (model_dimension=8) over the 16-d shape.
    assert writes == []
    assert client.collections["semantic-oss-high"] == (16, cosine)  # untouched
    assert indexer._writes_prepared is False

    # ---- allow_reindex=True: recreate to active shape, THEN persist attested ----
    client = _FakeQdrantClient()
    client.collections["semantic-oss-high"] = (16, cosine)
    indexer, writes = _bare_reconcile_indexer(client, active_dimension=8)

    result = indexer.reconcile_existing_collection(allow_reindex=True)

    assert result["status"] == "reindexed"
    assert client.collections["semantic-oss-high"] == (8, cosine)  # rebuilt to active dim
    # Persisted exactly once, AFTER the recreate (so never over the stale shape).
    assert writes == [{"attested": True, "model_dimension": 8}]
    assert indexer._writes_prepared is True


def test_reconcile_compatible_probe_matching_shape_still_reconciles():
    """Guard: the N1 shape gate does not disturb the normal compatible path when the
    live collection already matches the active shape."""
    cosine = SemanticIndexer.resolve_qdrant_distance("cosine")
    client = _FakeQdrantClient()
    client.collections["semantic-oss-high"] = (8, cosine)  # already at active shape
    indexer, writes = _bare_reconcile_indexer(client, active_dimension=8)

    result = indexer.reconcile_existing_collection()

    assert result["status"] == "reconciled"
    assert writes == [{"attested": True, "model_dimension": 8}]
    assert indexer._writes_prepared is True
