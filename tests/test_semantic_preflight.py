"""Tests for semantic preflight readiness checks."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mcp_server.config.settings import Settings
from mcp_server.setup.semantic_preflight import (
    ServiceStatus,
    bootstrap_active_profile_collection,
    check_embedding_smoke,
    check_enrichment_chat,
    check_openai_compatible,
    check_qdrant,
    check_qdrant_collection,
    run_semantic_preflight,
    summarize_collection_bootstrap,
)


@pytest.fixture(autouse=True)
def _qdrant_api_key(monkeypatch):
    """Server-mode Qdrant fails closed without a key; tests/test_qdrant_api_key.py covers that."""
    monkeypatch.setenv("QDRANT_API_KEY", "test-qdrant-key")
    monkeypatch.delenv("QDRANT_REQUIRE_API_KEY", raising=False)


def _ready_check(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        ok=True,
        name=name,
        message="ok",
        status=ServiceStatus.READY,
        details={},
        fixes=[],
    )


def _semantic_settings() -> Settings:
    return Settings(
        semantic_search_enabled=True,
        semantic_profiles_json="""
        {
          "oss_high": {
            "provider": "openai_compatible",
            "model_name": "Qwen/Qwen3-Embedding-8B",
            "model_version": "1",
            "vector_dimension": 4096,
            "distance_metric": "cosine",
            "normalization_policy": "provider-default",
            "chunk_schema_version": "v1",
            "chunker_version": "chunker@1",
            "build_metadata": {
              "embedding_api_base": "http://profile-embed:8001/v1",
              "embedding_model_name": "Qwen/Qwen3-Embedding-8B",
              "embedding_api_key_env": "EMBEDDING_KEY",
              "enrichment_api_base": "http://profile-enrich:8002/v1",
              "enrichment_model_name": "chat",
              "enrichment_api_key_env": "ENRICHMENT_KEY",
              "collection_name": "ci__repo__oss-high__workspace"
            }
          }
        }
        """,
        semantic_default_profile="oss_high",
    )


def test_check_qdrant_unreachable_returns_unreachable():
    result = check_qdrant("http://127.0.0.1:65531", timeout_s=0.1)
    assert result.status == ServiceStatus.UNREACHABLE


def test_check_openai_compatible_unreachable_returns_unreachable():
    result = check_openai_compatible("http://127.0.0.1:65532/v1", timeout_s=0.1)
    assert result.status == ServiceStatus.UNREACHABLE


def test_run_semantic_preflight_disabled_returns_disabled_report():
    settings = Settings(semantic_search_enabled=False)
    report = run_semantic_preflight(settings=settings, strict=False)
    assert report.overall_ready is False
    assert report.can_write_semantic_vectors is False
    assert report.qdrant.status == ServiceStatus.DISABLED
    assert report.embedding.status == ServiceStatus.DISABLED
    assert report.enrichment.status == ServiceStatus.DISABLED
    assert report.collection.status == ServiceStatus.DISABLED
    assert report.blocker is not None
    assert report.blocker.code == "semantic_disabled"


def test_run_semantic_preflight_prefers_profile_embedding_endpoint_over_global_fallback():
    settings = Settings(
        semantic_search_enabled=True,
        openai_api_base="http://global-fallback:9000/v1",
        semantic_profiles_json="""
        {
          "oss_high": {
            "provider": "openai_compatible",
            "model_name": "Qwen/Qwen3-Embedding-8B",
            "model_version": "1",
            "vector_dimension": 4096,
            "distance_metric": "cosine",
            "normalization_policy": "provider-default",
            "chunk_schema_version": "v1",
            "chunker_version": "chunker@1",
            "build_metadata": {
              "embedding_api_base": "http://profile-embed:8001/v1",
              "embedding_model_name": "Qwen/Qwen3-Embedding-8B",
              "embedding_api_key_env": "EMBEDDING_KEY",
              "enrichment_api_base": "http://profile-enrich:8002/v1",
              "enrichment_model_name": "chat",
              "enrichment_api_key_env": "ENRICHMENT_KEY",
              "collection_name": "ci__repo__oss-high__workspace"
            }
          }
        }
        """,
        semantic_default_profile="oss_high",
    )

    with (
        patch("mcp_server.setup.semantic_preflight.check_embedding_smoke") as check_embedding,
        patch("mcp_server.setup.semantic_preflight.check_enrichment_chat") as check_enrichment,
        patch("mcp_server.setup.semantic_preflight.check_qdrant") as check_qdrant_probe,
        patch("mcp_server.setup.semantic_preflight.check_qdrant_collection") as check_collection,
    ):
        check_embedding.return_value = _ready_check("embedding_vector")
        check_enrichment.return_value = _ready_check("enrichment_chat")
        check_qdrant_probe.return_value = _ready_check("qdrant")
        check_collection.return_value = _ready_check("qdrant_collection")

        report = run_semantic_preflight(settings=settings, strict=False)

    check_embedding.assert_called_once_with(
        base_url="http://profile-embed:8001/v1",
        model="Qwen/Qwen3-Embedding-8B",
        api_key_env="EMBEDDING_KEY",
        expected_dimension=4096,
        timeout_s=10.0,
    )
    check_enrichment.assert_called_once_with(
        base_url="http://profile-enrich:8002/v1",
        model="chat",
        api_key_env="ENRICHMENT_KEY",
        timeout_s=10.0,
    )
    assert report.effective_config["embedding"]["base_url"] == "http://profile-embed:8001/v1"
    assert report.effective_config["enrichment"]["base_url"] == "http://profile-enrich:8002/v1"


def test_run_semantic_preflight_reports_redacted_config_only(monkeypatch):
    monkeypatch.setenv("EMBEDDING_KEY", "embed-secret")
    monkeypatch.setenv("ENRICHMENT_KEY", "enrich-secret")
    settings = _semantic_settings()

    with (
        patch("mcp_server.setup.semantic_preflight.check_embedding_smoke") as check_embedding,
        patch("mcp_server.setup.semantic_preflight.check_enrichment_chat") as check_enrichment,
        patch("mcp_server.setup.semantic_preflight.check_qdrant") as check_qdrant_probe,
        patch("mcp_server.setup.semantic_preflight.check_qdrant_collection") as check_collection,
    ):
        check_embedding.return_value = _ready_check("embedding_vector")
        check_enrichment.return_value = _ready_check("enrichment_chat")
        check_qdrant_probe.return_value = _ready_check("qdrant")
        check_collection.return_value = _ready_check("qdrant_collection")

        report = run_semantic_preflight(settings=settings, strict=False)

    assert report.effective_config["embedding"]["api_key_env"] == "EMBEDDING_KEY"
    assert report.effective_config["embedding"]["api_key_present"] is True
    assert report.effective_config["enrichment"]["api_key_env"] == "ENRICHMENT_KEY"
    assert report.effective_config["enrichment"]["api_key_present"] is True
    payload = str(report.to_dict())
    assert "embed-secret" not in payload
    assert "enrich-secret" not in payload


def test_run_semantic_preflight_effective_config_reports_configured_and_effective_enrichment_model(
    monkeypatch,
):
    monkeypatch.setenv("EMBEDDING_KEY", "embed-secret")
    monkeypatch.setenv("ENRICHMENT_KEY", "enrich-secret")
    settings = _semantic_settings()

    with (
        patch("mcp_server.setup.semantic_preflight.check_embedding_smoke") as check_embedding,
        patch("mcp_server.setup.semantic_preflight.check_qdrant") as check_qdrant_probe,
        patch("mcp_server.setup.semantic_preflight.check_qdrant_collection") as check_collection,
        patch("mcp_server.setup.semantic_preflight.check_enrichment_chat") as check_enrichment,
    ):
        check_embedding.return_value = _ready_check("embedding_vector")
        check_qdrant_probe.return_value = _ready_check("qdrant")
        check_collection.return_value = _ready_check("qdrant_collection")
        check_enrichment.return_value = SimpleNamespace(
            ok=True,
            name="enrichment_chat",
            message="ok",
            status=ServiceStatus.READY,
            details={
                "configured_model": "chat",
                "effective_model": "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit",
                "resolution_strategy": "single_served_model_for_chat_alias",
                "considered_model_ids": ["cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"],
                "models_probe_verified": True,
                "failure_class": None,
            },
            fixes=[],
        )

        report = run_semantic_preflight(settings=settings, strict=False)

    assert report.effective_config["enrichment"]["model"] == "chat"
    assert report.effective_config["enrichment"]["configured_model"] == "chat"
    assert (
        report.effective_config["enrichment"]["effective_model"]
        == "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"
    )
    assert (
        report.effective_config["enrichment"]["resolution_strategy"]
        == "single_served_model_for_chat_alias"
    )


def test_enrichment_chat_reports_missing_api_key_env():
    result = check_enrichment_chat(
        base_url="http://profile-enrich:8002/v1",
        model="chat",
        api_key_env="MISSING_CHAT_KEY",
        timeout_s=0.1,
    )
    assert result.status == ServiceStatus.MISCONFIGURED
    assert result.details["failure_class"] == "missing_api_key_env"


def test_embedding_smoke_rejects_dimension_mismatch(monkeypatch):
    monkeypatch.setenv("EMBEDDING_KEY", "present")
    with patch(
        "mcp_server.setup.semantic_preflight._http_post_json",
        return_value={"data": [{"embedding": [0.0, 1.0, 2.0]}]},
    ):
        result = check_embedding_smoke(
            base_url="http://profile-embed:8001/v1",
            model="Qwen/Qwen3-Embedding-8B",
            api_key_env="EMBEDDING_KEY",
            expected_dimension=4096,
            timeout_s=0.1,
        )
    assert result.status == ServiceStatus.MISCONFIGURED
    assert result.details["failure_class"] == "embedding_dimension_mismatch"
    assert result.details["actual_dimension"] == 3


def test_enrichment_chat_rejects_wrong_chat_model(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_KEY", "present")

    class _FakeHttpError(Exception):
        pass

    from urllib import error as urllib_error

    def _raise_http_error(*args, **kwargs):
        raise urllib_error.HTTPError(
            url="http://profile-enrich:8002/v1/chat/completions",
            code=400,
            msg="bad request",
            hdrs=None,
            fp=None,
        )

    with (
        patch("mcp_server.setup.semantic_preflight._http_post_json", side_effect=_raise_http_error),
        patch(
            "mcp_server.setup.semantic_preflight._read_http_error",
            return_value=(400, '{"error":{"message":"model not found"}}'),
        ),
    ):
        result = check_enrichment_chat(
            base_url="http://profile-enrich:8002/v1",
            model="chat",
            api_key_env="ENRICHMENT_KEY",
            timeout_s=0.1,
        )
    assert result.status == ServiceStatus.MISCONFIGURED
    assert result.details["failure_class"] == "wrong_chat_model"


def test_enrichment_chat_uses_configured_model_when_served(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_KEY", "present")
    seen_payload = {}

    def _fake_post(url, payload, *, timeout_s, headers=None):
        del url, timeout_s, headers
        seen_payload.update(payload)
        return {"choices": [{"message": {"content": "ok"}}], "model": payload["model"]}

    with (
        patch(
            "mcp_server.setup.semantic_preflight._http_request_json",
            return_value={"data": [{"id": "chat"}]},
        ),
        patch("mcp_server.setup.semantic_preflight._http_post_json", side_effect=_fake_post),
    ):
        result = check_enrichment_chat(
            base_url="http://profile-enrich:8002/v1",
            model="chat",
            api_key_env="ENRICHMENT_KEY",
            timeout_s=0.1,
        )

    assert result.status == ServiceStatus.READY
    assert seen_payload["model"] == "chat"
    assert result.details["configured_model"] == "chat"
    assert result.details["effective_model"] == "chat"
    assert result.details["resolution_strategy"] == "configured_model_served"
    assert result.details["considered_model_ids"] == ["chat"]


def test_enrichment_chat_resolves_default_chat_alias_to_single_served_model(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_KEY", "present")
    seen_payload = {}
    served_model = "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"

    def _fake_post(url, payload, *, timeout_s, headers=None):
        del url, timeout_s, headers
        seen_payload.update(payload)
        return {"choices": [{"message": {"content": "ok"}}], "model": payload["model"]}

    with (
        patch(
            "mcp_server.setup.semantic_preflight._http_request_json",
            return_value={"data": [{"id": served_model}]},
        ),
        patch("mcp_server.setup.semantic_preflight._http_post_json", side_effect=_fake_post),
    ):
        result = check_enrichment_chat(
            base_url="http://profile-enrich:8002/v1",
            model="chat",
            api_key_env="ENRICHMENT_KEY",
            timeout_s=0.1,
        )

    assert result.status == ServiceStatus.READY
    assert seen_payload["model"] == served_model
    assert result.details["configured_model"] == "chat"
    assert result.details["effective_model"] == served_model
    assert result.details["resolution_strategy"] == "single_served_model_for_chat_alias"
    assert result.details["considered_model_ids"] == [served_model]


def test_enrichment_chat_reports_wrong_model_after_compatibility_path_exhausted(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_KEY", "present")

    from urllib import error as urllib_error

    def _raise_http_error(*args, **kwargs):
        raise urllib_error.HTTPError(
            url="http://profile-enrich:8002/v1/chat/completions",
            code=400,
            msg="bad request",
            hdrs=None,
            fp=None,
        )

    with (
        patch(
            "mcp_server.setup.semantic_preflight._http_request_json",
            return_value={"data": [{"id": "model-a"}, {"id": "model-b"}]},
        ),
        patch("mcp_server.setup.semantic_preflight._http_post_json", side_effect=_raise_http_error),
        patch(
            "mcp_server.setup.semantic_preflight._read_http_error",
            return_value=(400, '{"error":{"message":"model not found"}}'),
        ),
    ):
        result = check_enrichment_chat(
            base_url="http://profile-enrich:8002/v1",
            model="chat",
            api_key_env="ENRICHMENT_KEY",
            timeout_s=0.1,
        )

    assert result.status == ServiceStatus.MISCONFIGURED
    assert result.details["failure_class"] == "wrong_chat_model"
    assert result.details["configured_model"] == "chat"
    assert result.details["effective_model"] == "chat"
    assert result.details["resolution_strategy"] == "no_compatible_model_found"
    assert result.details["considered_model_ids"] == ["model-a", "model-b"]


def test_qdrant_collection_reports_missing_collection():
    from urllib import error as urllib_error

    def _raise_http_error(*args, **kwargs):
        raise urllib_error.HTTPError(
            url="http://localhost:6333/collections/ci__repo__oss-high__workspace",
            code=404,
            msg="not found",
            hdrs=None,
            fp=None,
        )

    with (
        patch("mcp_server.setup.semantic_preflight._http_get_json", side_effect=_raise_http_error),
        patch(
            "mcp_server.setup.semantic_preflight._read_http_error",
            return_value=(404, '{"status":"not found"}'),
        ),
    ):
        result = check_qdrant_collection(
            qdrant_url="http://localhost:6333",
            collection_name="ci__repo__oss-high__workspace",
            expected_dimension=4096,
            expected_distance="cosine",
            timeout_s=0.1,
        )
    assert result.status == ServiceStatus.MISCONFIGURED
    assert result.details["failure_class"] == "collection_missing"


def test_qdrant_collection_validates_dimension_and_distance_without_mutation():
    with patch(
        "mcp_server.setup.semantic_preflight._http_get_json",
        return_value={
            "result": {
                "config": {
                    "params": {
                        "vectors": {
                            "size": 4096,
                            "distance": "Cosine",
                        }
                    }
                }
            }
        },
    ) as http_get:
        result = check_qdrant_collection(
            qdrant_url="http://localhost:6333",
            collection_name="ci__repo__oss-high__workspace",
            expected_dimension=4096,
            expected_distance="cosine",
            timeout_s=0.1,
        )

    assert result.status == ServiceStatus.READY
    assert result.details["actual_dimension"] == 4096
    assert result.details["actual_distance_metric"] == "cosine"


def test_summarize_collection_bootstrap_reports_reused_for_ready_preflight():
    result = summarize_collection_bootstrap(
        {
            "can_write_semantic_vectors": True,
            "effective_config": {
                "selected_profile": "oss_high",
                "collection_name": "code_index__oss_high__v1",
                "normalized_collection_name": "code_index__oss_high__v1",
                "qdrant_url": "http://localhost:6333",
                "vector_dimension": 4096,
                "distance_metric": "cosine",
            },
        }
    )

    assert result.status == "reused"
    assert result.collection_name == "code_index__oss_high__v1"


def test_bootstrap_active_profile_collection_creates_missing_collection(monkeypatch):
    settings = _semantic_settings()

    monkeypatch.setattr(
        "mcp_server.setup.semantic_preflight.check_qdrant",
        lambda *args, **kwargs: _ready_check("qdrant"),
    )

    class _FakeQdrantClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    monkeypatch.setattr("qdrant_client.QdrantClient", _FakeQdrantClient)
    monkeypatch.setattr(
        "mcp_server.utils.semantic_indexer.ensure_qdrant_collection",
        lambda *args, **kwargs: SimpleNamespace(
            status="created",
            actual_dimension=4096,
            actual_distance_metric="cosine",
        ),
    )

    result = bootstrap_active_profile_collection(settings=settings, profile="oss_high")

    assert result.status == "created"
    assert result.profile_id == "oss_high"
    assert result.collection_name == "ci__repo__oss-high__workspace"


def test_bootstrap_active_profile_collection_preserves_shape_mismatch_blocker(monkeypatch):
    settings = _semantic_settings()

    monkeypatch.setattr(
        "mcp_server.setup.semantic_preflight.check_qdrant",
        lambda *args, **kwargs: _ready_check("qdrant"),
    )

    class _FakeQdrantClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    monkeypatch.setattr("qdrant_client.QdrantClient", _FakeQdrantClient)
    monkeypatch.setattr(
        "mcp_server.utils.semantic_indexer.ensure_qdrant_collection",
        lambda *args, **kwargs: SimpleNamespace(
            status="blocked",
            actual_dimension=1024,
            actual_distance_metric="dot",
        ),
    )

    result = bootstrap_active_profile_collection(settings=settings, profile="oss_high")

    assert result.status == "blocked"
    assert result.blocker["code"] == "collection_shape_mismatch"
    assert result.blocker["actual_dimension"] == 1024


def test_run_semantic_preflight_emits_structured_blocker_for_fail_closed_contract(monkeypatch):
    monkeypatch.setenv("EMBEDDING_KEY", "present")
    settings = _semantic_settings()

    with (
        patch("mcp_server.setup.semantic_preflight.check_enrichment_chat") as check_enrichment,
        patch("mcp_server.setup.semantic_preflight.check_embedding_smoke") as check_embedding,
        patch("mcp_server.setup.semantic_preflight.check_qdrant") as check_qdrant_probe,
        patch("mcp_server.setup.semantic_preflight.check_qdrant_collection") as check_collection,
    ):
        check_enrichment.return_value = _ready_check("enrichment_chat")
        check_qdrant_probe.return_value = _ready_check("qdrant")
        check_collection.return_value = _ready_check("qdrant_collection")
        check_embedding.return_value = SimpleNamespace(
            ok=False,
            name="embedding_vector",
            message="Embedding dimension mismatch: expected 4096, got 3",
            status=ServiceStatus.MISCONFIGURED,
            details={"failure_class": "embedding_dimension_mismatch"},
            fixes=["Repair embedding model"],
        )

        report = run_semantic_preflight(settings=settings, strict=True)

    assert report.overall_ready is False
    assert report.can_write_semantic_vectors is False
    assert report.blocker is not None
    assert report.blocker.code == "embedding_dimension_mismatch"
    assert report.blocker.can_write_semantic_vectors is False
    assert report.blocker.failing_checks[0]["name"] == "embedding_vector"
    assert "Strict semantic mode enabled" in " ".join(report.warnings)
