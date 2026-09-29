"""QDRANT_API_KEY is sent to every server-mode Qdrant connection.

The qdrant client and HTTP probes are mocked - no live server is used.
"""

import io
import sqlite3
from types import SimpleNamespace
from urllib import error

import pytest

from mcp_server.artifacts.secure_export import SecureIndexExporter
from mcp_server.cli.index_management import _get_vector_backend_status
from mcp_server.config.env_vars import get_qdrant_api_key
from mcp_server.setup import semantic_preflight
from mcp_server.setup.semantic_preflight import (
    bootstrap_active_profile_collection,
    check_qdrant,
    check_qdrant_collection,
)
from mcp_server.utils.semantic_indexer import SemanticIndexer
from tests.test_semantic_preflight import _ready_check, _semantic_settings

API_KEY = "test-qdrant-key"


class RecordingQdrantClient:
    """Qdrant client double that records constructor kwargs."""

    instances: list = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        RecordingQdrantClient.instances.append(self)

    def get_collections(self):
        return SimpleNamespace(collections=[])


@pytest.fixture(autouse=True)
def _reset_recorder():
    RecordingQdrantClient.instances = []


@pytest.fixture(params=[API_KEY, None], ids=["key-set", "key-unset"])
def api_key(request, monkeypatch):
    if request.param is None:
        monkeypatch.delenv("QDRANT_API_KEY", raising=False)
    else:
        monkeypatch.setenv("QDRANT_API_KEY", request.param)
    return request.param


def test_get_qdrant_api_key_reads_env(monkeypatch):
    monkeypatch.setenv("QDRANT_API_KEY", API_KEY)
    assert get_qdrant_api_key() == API_KEY


@pytest.mark.parametrize("value", [None, "", "   "])
def test_get_qdrant_api_key_unset_or_blank_is_none(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("QDRANT_API_KEY", raising=False)
    else:
        monkeypatch.setenv("QDRANT_API_KEY", value)
    assert get_qdrant_api_key() is None


def test_semantic_indexer_server_mode_passes_api_key(monkeypatch, api_key):
    monkeypatch.setenv("QDRANT_USE_SERVER", "true")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.test:6333")
    monkeypatch.setattr("mcp_server.utils.semantic_indexer.QdrantClient", RecordingQdrantClient)

    indexer = SemanticIndexer.__new__(SemanticIndexer)
    client = indexer._init_qdrant_client("http://qdrant.test:6333")

    assert client.kwargs["url"] == "http://qdrant.test:6333"
    assert client.kwargs["api_key"] == api_key
    assert indexer._connection_mode == "server"


def test_semantic_indexer_explicit_url_passes_api_key(monkeypatch, api_key):
    monkeypatch.setenv("QDRANT_USE_SERVER", "false")
    monkeypatch.setattr("mcp_server.utils.semantic_indexer.QdrantClient", RecordingQdrantClient)

    indexer = SemanticIndexer.__new__(SemanticIndexer)
    client = indexer._init_qdrant_client("http://explicit.test:6333")

    assert client.kwargs["url"] == "http://explicit.test:6333"
    assert client.kwargs["api_key"] == api_key


def test_semantic_indexer_memory_mode_sends_no_api_key(monkeypatch):
    monkeypatch.setenv("QDRANT_USE_SERVER", "false")
    monkeypatch.setenv("QDRANT_API_KEY", API_KEY)
    monkeypatch.setattr("mcp_server.utils.semantic_indexer.QdrantClient", RecordingQdrantClient)

    indexer = SemanticIndexer.__new__(SemanticIndexer)
    client = indexer._init_qdrant_client(":memory:")

    assert client.kwargs == {"location": ":memory:"}


def test_index_management_server_backend_passes_api_key(monkeypatch, tmp_path, api_key):
    monkeypatch.setenv("QDRANT_PATH", str(tmp_path / "absent.qdrant"))
    monkeypatch.setenv("QDRANT_USE_SERVER", "true")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.test:6333")
    monkeypatch.setattr("qdrant_client.QdrantClient", RecordingQdrantClient)

    status = _get_vector_backend_status()

    assert status["backend"] == "server"
    (client,) = RecordingQdrantClient.instances
    assert client.kwargs["url"] == "http://qdrant.test:6333"
    assert client.kwargs["api_key"] == api_key


def test_secure_export_server_backend_passes_api_key(monkeypatch, tmp_path, api_key):
    backend = "http://qdrant.test:6333"
    monkeypatch.setenv("QDRANT_URL", backend)
    monkeypatch.setattr("qdrant_client.QdrantClient", RecordingQdrantClient)
    monkeypatch.setattr(
        SecureIndexExporter,
        "read_generation_metadata",
        lambda *args: {
            "qdrant_path": backend,
            "semantic_profile": "profile",
            "semantic_profiles": {"profile": {"attested": True}},
            "collection_name": "collection",
        },
    )
    monkeypatch.setattr(
        RecordingQdrantClient,
        "retrieve",
        lambda self, *args, **kwargs: [
            SimpleNamespace(id="point", payload={"relative_path": "sample.py"}, vector=[0.1])
        ],
        raising=False,
    )
    monkeypatch.setattr(RecordingQdrantClient, "close", lambda self: None, raising=False)
    database = tmp_path / "current.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE semantic_points (profile_id TEXT, point_id TEXT, collection TEXT)")
        db.execute("INSERT INTO semantic_points VALUES ('profile', 'point', 'collection')")
    exporter = SecureIndexExporter(repo_path=tmp_path, index_location=tmp_path, index_path=database)
    exporter._export_vectors(database, tmp_path / "vectors.jsonl")
    assert RecordingQdrantClient.instances[0].kwargs["api_key"] == api_key


def test_bootstrap_collection_passes_api_key(monkeypatch, api_key):
    monkeypatch.setattr(
        "mcp_server.setup.semantic_preflight.check_qdrant",
        lambda *args, **kwargs: _ready_check("qdrant"),
    )
    monkeypatch.setattr("qdrant_client.QdrantClient", RecordingQdrantClient)
    monkeypatch.setattr(
        "mcp_server.utils.semantic_indexer.ensure_qdrant_collection",
        lambda *args, **kwargs: SimpleNamespace(
            status="created",
            actual_dimension=4096,
            actual_distance_metric="cosine",
        ),
    )

    result = bootstrap_active_profile_collection(settings=_semantic_settings(), profile="oss_high")

    assert result.status == "created"
    (client,) = RecordingQdrantClient.instances
    assert client.kwargs["api_key"] == api_key


def _capture_get_json(monkeypatch, payload):
    calls = []

    def _fake_get_json(url, timeout_s, headers=None):
        calls.append({"url": url, "headers": headers})
        return payload

    monkeypatch.setattr(semantic_preflight, "_http_get_json", _fake_get_json)
    return calls


def test_check_qdrant_probe_sends_api_key_header(monkeypatch, api_key):
    calls = _capture_get_json(monkeypatch, {"result": {"collections": [{"name": "a"}]}})

    result = check_qdrant("http://qdrant.test:6333", timeout_s=0.1)

    assert result.ok
    expected = {"api-key": api_key} if api_key else {}
    assert calls == [{"url": "http://qdrant.test:6333/collections", "headers": expected}]


def test_check_qdrant_collection_probe_sends_api_key_header(monkeypatch, api_key):
    calls = _capture_get_json(
        monkeypatch,
        {"result": {"config": {"params": {"vectors": {"size": 4, "distance": "Cosine"}}}}},
    )

    check_qdrant_collection(
        qdrant_url="http://qdrant.test:6333",
        collection_name="code",
        expected_dimension=4,
        expected_distance="cosine",
        timeout_s=0.1,
    )

    expected = {"api-key": api_key} if api_key else {}
    assert calls[0]["headers"] == expected


def test_check_qdrant_unauthorized_suggests_api_key(monkeypatch):
    monkeypatch.delenv("QDRANT_API_KEY", raising=False)

    def _unauthorized(url, timeout_s, headers=None):
        raise error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b"Must provide an API key"))

    monkeypatch.setattr(semantic_preflight, "_http_get_json", _unauthorized)

    result = check_qdrant("http://qdrant.test:6333", timeout_s=0.1)

    assert not result.ok
    assert result.details["http_status"] == 401
    assert any("QDRANT_API_KEY" in fix for fix in result.fixes)
