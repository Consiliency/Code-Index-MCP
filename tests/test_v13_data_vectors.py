"""Real Qdrant maintenance and embedding-admission counterexamples."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from qdrant_client import QdrantClient, models

from mcp_server.core.path_resolver import PathResolver
from mcp_server.interfaces.inference_contracts import ProvenanceField
from mcp_server.utils.semantic_indexer import SemanticIndexer
from tests.test_embedding_provenance import (
    _FakeProvenanceProvider,
    _items,
    _make_indexer,
    _openai_response,
    _profile,
)

pytestmark = [pytest.mark.requires_network] if os.environ.get("V13_TEST_QDRANT_URL") else []


@pytest.fixture
def real_indexer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider = _FakeProvenanceProvider(_openai_response)
    monkeypatch.setattr(
        "mcp_server.utils.semantic_indexer.create_embedding_provider", lambda **kwargs: provider
    )
    endpoint = os.environ.get("V13_TEST_QDRANT_URL", str(tmp_path / "vectors"))
    if endpoint.startswith("http"):
        container = os.environ["V13_TEST_QDRANT_CONTAINER"]
        state = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "inspect",
                    "--format",
                    '{"name":{{json .Name}},"ports":{{json .NetworkSettings.Ports}}}',
                    container,
                ],
                text=True,
                timeout=10,
            )
        )
        assert state["name"].startswith("/v13-qdrant-")
        assert endpoint == "http://127.0.0.1:" + state["ports"]["6333/tcp"][0]["HostPort"]
    profile = replace(
        _profile(), build_metadata={"collection_name": "v13-proof-" + uuid.uuid4().hex}
    )
    indexer = SemanticIndexer(
        qdrant_path=endpoint,
        path_resolver=PathResolver(tmp_path),
        profile=profile,
        collection="v13-" + tmp_path.name.replace("_", "-"),
    )
    indexer._prepare_for_writes()
    try:
        yield indexer
    finally:
        if endpoint.startswith("http"):
            indexer.qdrant.delete_collection(indexer.collection)
        indexer.qdrant.close()


def _seed(indexer, count):
    indexer.qdrant.upsert(
        collection_name=indexer.collection,
        points=[
            models.PointStruct(
                id=i,
                vector=[float(i % 7 + 1), 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                payload={
                    "relative_path": "source.py",
                    "file": "source.py",
                    "content_hash": "synthetic-content",
                    "original_id": i,
                },
            )
            for i in range(1, count + 1)
        ],
        wait=True,
    )


def _points(indexer):
    points, offset = [], None
    while True:
        batch, offset = indexer.qdrant.scroll(
            collection_name=indexer.collection,
            limit=137,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        points.extend(batch)
        if offset is None:
            return {point.id: point for point in points}


@pytest.mark.parametrize("count", [1000, 1001])
@pytest.mark.parametrize("operation", ["remove", "move", "lookup", "mark"])
def test_maintenance_crosses_page_boundary(real_indexer, count, operation):
    indexer = real_indexer
    _seed(indexer, count)
    before = _points(indexer)
    assert len(before) == count
    if operation == "remove":
        assert indexer.remove_file("source.py") == count
        assert not _points(indexer)
        assert indexer.remove_file("source.py") == 0
    elif operation == "lookup":
        found = indexer.get_embeddings_by_content_hash("synthetic-content")
        assert {point["id"] for point in found} == set(before)
    else:
        if operation == "move":
            assert indexer.move_file("source.py", "moved.py", "synthetic-content") == count
            assert indexer.move_file("source.py", "moved.py", "synthetic-content") == 0
        else:
            assert indexer.mark_file_deleted("source.py") == count
        after = _points(indexer)
        assert after.keys() == before.keys()
        for point_id, point in after.items():
            assert point.vector == before[point_id].vector
            assert point.payload["original_id"] == point_id
            if operation == "move":
                assert point.payload["relative_path"] == "moved.py"
            else:
                assert point.payload["is_deleted"] is True


@pytest.mark.parametrize("count", [1000, 1001])
@pytest.mark.parametrize("identity_key", ["chunk_id", "source_chunk_id"])
def test_maintenance_filtered_ranking_precedes_limit(real_indexer, count, identity_key):
    indexer = real_indexer
    indexer.embedding_client = _FakeProvenanceProvider(
        lambda texts, input_type: _openai_response(
            texts, input_type, items=_items([[1.0] * 8 for _ in texts])
        )
    )
    points = [
        models.PointStruct(
            id=i,
            vector=([1.0] * 8 if i == count else [1.0] + [0.0] * 7),
            payload={identity_key: f"source-{i}", "file": f"source-{i}.py"},
        )
        for i in range(1, count + 1)
    ]
    for offset, payload in enumerate(
        [
            {"chunk_id": "excluded"},
            {identity_key: "source-1", "is_deleted": True},
            {identity_key: "source-1", indexer.PROVENANCE_TAG: True},
        ],
        start=1,
    ):
        points.append(models.PointStruct(id=count + offset, vector=[1.0] * 8, payload=payload))
    indexer.qdrant.upsert(collection_name=indexer.collection, points=points, wait=True)

    results = indexer.search(
        "concept", limit=1, source_chunk_ids=[f"source-{i}" for i in range(1, count + 1)]
    )

    assert len(results) == 1
    assert results[0][identity_key] == f"source-{count}"
    assert results[0]["score"] > 0.9
    assert indexer.embedding_client.calls == [(["concept"], "query")]


def test_empty_semantic_candidates_do_not_embed_or_search(real_indexer):
    provider = real_indexer.embedding_client
    before = list(provider.calls)
    assert real_indexer.search("concept", source_chunk_ids=[]) == []
    assert provider.calls == before


@pytest.mark.parametrize("remote_only", [False, True])
def test_staged_publication_requires_exact_remote_ownership(real_indexer, tmp_path, remote_only):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from mcp_server.core.repo_context import RepoContext
    from mcp_server.storage.git_index_manager import GitAwareIndexManager
    from mcp_server.storage.sqlite_store import SQLiteStore

    _seed(real_indexer, 2 if remote_only else 1)
    real_indexer.write_collection_provenance([1])
    store = SQLiteStore(str(tmp_path / "staged.db"))
    try:
        store.upsert_semantic_point(
            real_indexer.semantic_profile.profile_id, "chunk", 1, real_indexer.collection
        )
        registry = SimpleNamespace(lease=lambda repo_id, ctx: nullcontext(real_indexer))
        manager = GitAwareIndexManager.__new__(GitAwareIndexManager)
        manager.dispatcher = SimpleNamespace(_semantic_registry=registry)
        ctx = RepoContext("repo", store, tmp_path, "main", SimpleNamespace(), staging=True)
        if remote_only:
            with pytest.raises(RuntimeError, match="ownership is incomplete"):
                manager._finalize_staged_vectors("repo", ctx)
        else:
            manager._finalize_staged_vectors("repo", ctx)
    finally:
        store.close()


@pytest.mark.parametrize("derived", [0, 2**63 - 1, 2**63, 2**64 - 1])
def test_generated_point_ids_fit_both_sqlite_and_qdrant(real_indexer, tmp_path, derived):
    from mcp_server.storage.sqlite_store import SQLiteStore

    point_id = real_indexer._reserve_safe_id(derived)
    assert 0 < point_id < 2**63
    store = SQLiteStore(str(tmp_path / "point-ids.db"))
    try:
        store.upsert_semantic_point("fixture", "source", point_id, real_indexer.collection)
        real_indexer.qdrant.upsert(
            collection_name=real_indexer.collection,
            points=[models.PointStruct(id=point_id, vector=[1.0] * 8)],
            wait=True,
        )
        ids = store.get_semantic_point_ids("fixture", ["source"])
        assert ids == [point_id]
        assert real_indexer.qdrant.retrieve(real_indexer.collection, ids=ids)[0].id == point_id
    finally:
        store.close()


def test_explicit_memory_backend_never_contacts_default_server(monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr("mcp_server.utils.semantic_indexer.QdrantClient", factory)
    monkeypatch.setenv("QDRANT_USE_SERVER", "true")
    indexer = SemanticIndexer.__new__(SemanticIndexer)
    indexer._init_qdrant_client(":memory:")
    factory.assert_called_once_with(location=":memory:")
    assert indexer._connection_mode == "memory"


def test_explicit_server_backend_never_contacts_default_server(monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr("mcp_server.utils.semantic_indexer.QdrantClient", factory)
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("QDRANT_USE_SERVER", "true")
    indexer = SemanticIndexer.__new__(SemanticIndexer)
    indexer._init_qdrant_client("http://127.0.0.1:6339")
    factory.assert_called_once_with(url="http://127.0.0.1:6339", timeout=5)


def test_file_backend_refuses_live_lock_without_unlink_or_fallback(tmp_path, monkeypatch):
    path = tmp_path / "held-vectors"
    first = QdrantClient(path=str(path))
    lock = path / ".lock"
    identity = lock.stat().st_ino
    indexer = SemanticIndexer.__new__(SemanticIndexer)
    calls = []

    def construct(**kwargs):
        calls.append(kwargs)
        assert kwargs == {"path": str(path)}, "backend fallback attempted"
        return QdrantClient(**kwargs)

    monkeypatch.setattr("mcp_server.utils.semantic_indexer.QdrantClient", construct)
    second = None
    try:
        try:
            second = indexer._init_qdrant_client(str(path))
        except RuntimeError:
            pass
        assert second is None, "a live file lock was bypassed"
        assert lock.stat().st_ino == identity
        assert calls == [{"path": str(path)}]
    finally:
        if second is not None:
            second.close()
        first.close()


@pytest.mark.parametrize("drift", ["model", "revision", "normalization", "dimension"])
@pytest.mark.parametrize("restart", [False, True])
def test_every_document_batch_revalidates_provenance(tmp_path, drift, restart):
    indexer = _make_indexer(tmp_path, _FakeProvenanceProvider(_openai_response))
    indexer._prepare_for_writes()
    if restart:
        indexer = _make_indexer(tmp_path, _FakeProvenanceProvider(_openai_response))
    original_metadata = Path(indexer.metadata_file).read_bytes()

    def drifted(texts, input_type):
        response = _openai_response(texts, input_type)
        if drift == "model":
            return replace(response, served_model_id=ProvenanceField.reported("other-model"))
        if drift == "revision":
            return replace(response, model_revision=ProvenanceField.reported("v2"))
        if drift == "normalization":
            return replace(response, normalization=ProvenanceField.reported("unit-l2"))
        return replace(response, dimension=ProvenanceField.reported(16))

    indexer.embedding_client = _FakeProvenanceProvider(drifted)
    with pytest.raises(RuntimeError):
        indexer._embed_texts(["synthetic batch"], input_type="document")
    assert not indexer.qdrant.upserts
    assert Path(indexer.metadata_file).read_bytes() == original_metadata


def test_unavailable_remote_cleanup_keeps_local_mappings(tmp_path):
    from mcp_server.storage.sqlite_store import SQLiteStore

    store = SQLiteStore(str(tmp_path / "index.db"))
    try:
        store.upsert_semantic_point("fixture", "chunk", 1, "fixture-collection")
        indexer = SemanticIndexer.__new__(SemanticIndexer)
        indexer._qdrant_available = False
        indexer.collection = "fixture-collection"
        with pytest.raises(RuntimeError):
            indexer.delete_stale_vectors("fixture", ["chunk"], sqlite_store=store)
        assert store.get_semantic_point_ids("fixture", ["chunk"]) == [1]
    finally:
        store.close()


def test_restarted_write_cannot_restamp_old_collection_with_new_model(tmp_path):
    first = _make_indexer(tmp_path, _FakeProvenanceProvider(_openai_response))
    first._prepare_for_writes()
    original = Path(first.metadata_file).read_bytes()
    changed = _make_indexer(
        tmp_path,
        _FakeProvenanceProvider(
            lambda texts, input_type: _openai_response(texts, input_type, served_model="changed")
        ),
        profile=_profile(model="changed"),
    )
    changed.qdrant = first.qdrant
    with pytest.raises(RuntimeError):
        changed._prepare_for_writes()
    assert Path(first.metadata_file).read_bytes() == original
    assert not changed.qdrant.upserts


@pytest.mark.parametrize("payload", [b"{corrupt", b"[]"])
def test_corrupt_metadata_refuses_write_without_replacing_evidence(tmp_path, payload):
    indexer = _make_indexer(tmp_path, _FakeProvenanceProvider(_openai_response))
    Path(indexer.metadata_file).write_bytes(payload)
    with pytest.raises(RuntimeError):
        indexer._prepare_for_writes()
    assert Path(indexer.metadata_file).read_bytes() == payload
    assert not indexer.qdrant.upserts


def test_metadata_unreadable_refuses_query_and_write(tmp_path, monkeypatch):
    indexer = _make_indexer(tmp_path, _FakeProvenanceProvider(_openai_response))
    import builtins

    original = builtins.open

    def unreadable(file, *args, **kwargs):
        if str(file) == indexer.metadata_file:
            raise PermissionError("synthetic secret permission payload")
        return original(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", unreadable)
    with pytest.raises(RuntimeError, match="metadata") as error:
        indexer._embed_texts(["query"], input_type="query")
    assert "secret" not in str(error.value)
    assert not indexer.qdrant.upserts


def test_indexed_commit_is_supplied_context_not_cwd(real_indexer):
    real_indexer.commit = "a" * 64
    assert real_indexer._get_git_commit_hash() == "a" * 64


def test_metadata_fsync_failure_propagates_without_replacing_old_bytes(tmp_path, monkeypatch):
    indexer = _make_indexer(tmp_path, _FakeProvenanceProvider(_openai_response))
    target = Path(indexer.metadata_file)
    target.write_text('{"retained": true}')
    original = target.read_bytes()
    monkeypatch.setattr(os, "fsync", MagicMock(side_effect=OSError("synthetic durability failure")))
    with pytest.raises(OSError):
        indexer._atomic_write_metadata({"retained": False})
    assert target.read_bytes() == original
    assert not list(tmp_path.glob(".*.tmp"))


def test_another_process_owns_file_lock_until_it_closes(tmp_path):
    path = str(tmp_path / "held-vectors")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from qdrant_client import QdrantClient; import sys; "
            "c = QdrantClient(path=sys.argv[1]); print('ready', flush=True); "
            "sys.stdin.readline(); c.close()",
            path,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        import select

        assert select.select([child.stdout], [], [], 15)[0]
        assert child.stdout.readline().strip() == "ready"
        inode = (Path(path) / ".lock").stat().st_ino
        indexer = SemanticIndexer.__new__(SemanticIndexer)
        with pytest.raises(RuntimeError):
            indexer._init_qdrant_client(path)
        assert (Path(path) / ".lock").stat().st_ino == inode
        assert child.poll() is None
    finally:
        child.communicate("\n", timeout=15)
        assert child.returncode == 0
    client = SemanticIndexer.__new__(SemanticIndexer)._init_qdrant_client(path)
    client.close()


def test_failed_explicit_server_has_no_backend_fallback(monkeypatch):
    factory = MagicMock(side_effect=ConnectionError("private server failure"))
    monkeypatch.setattr("mcp_server.utils.semantic_indexer.QdrantClient", factory)
    indexer = SemanticIndexer.__new__(SemanticIndexer)
    with pytest.raises(RuntimeError) as error:
        indexer._init_qdrant_client("http://127.0.0.1:1")
    assert factory.call_count == 1
    assert factory.call_args.kwargs["url"] == "http://127.0.0.1:1"
    assert "private" not in str(error.value)


def test_concurrent_batches_cannot_share_another_batches_attestation(real_indexer):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)

    def respond(texts, input_type):
        barrier.wait(timeout=10)
        return _openai_response(
            texts, input_type, served_model="changed" if texts == ["bad"] else "srv-model"
        )

    real_indexer.embedding_client = _FakeProvenanceProvider(respond)

    def index(content):
        return real_indexer.index_symbol(
            file="source.py",
            name=content,
            kind="chunk",
            signature=content,
            line=1,
            span=(1, 1),
            content=content,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        good = pool.submit(index, "good")
        bad = pool.submit(index, "bad")
        good.result(timeout=15)
        with pytest.raises(RuntimeError):
            bad.result(timeout=15)
    points = _points(real_indexer)
    assert len(points) == 1
    assert next(iter(points.values())).payload["symbol"] == "good"


@pytest.mark.parametrize("operation", ["remove", "move", "mark"])
def test_maintenance_interruption_retries_without_payload_corruption(
    real_indexer, monkeypatch, operation
):
    indexer = real_indexer
    _seed(indexer, 1001)
    before = _points(indexer)
    method = "delete" if operation == "remove" else "set_payload"
    original = getattr(indexer.qdrant, method)
    calls = 0

    def interrupted(**kwargs):
        nonlocal calls
        if method != "delete" or kwargs["points_selector"].points != [0]:
            calls += 1
            if calls == 2:
                raise OSError("synthetic interruption")
        return original(**kwargs)

    def mutate():
        if operation == "remove":
            return indexer.remove_file("source.py")
        if operation == "move":
            return indexer.move_file("source.py", "moved.py")
        return indexer.mark_file_deleted("source.py")

    monkeypatch.setattr(indexer.qdrant, method, interrupted)
    with pytest.raises(RuntimeError):
        mutate()
    monkeypatch.setattr(indexer.qdrant, method, original)
    assert mutate() > 0
    after = _points(indexer)
    if operation == "remove":
        assert not after
    else:
        assert after.keys() == before.keys()
        for point_id, point in after.items():
            assert point.vector == before[point_id].vector
            assert point.payload["original_id"] == point_id
            assert (
                point.payload["relative_path"] == "moved.py"
                if operation == "move"
                else point.payload["is_deleted"] is True
            )
