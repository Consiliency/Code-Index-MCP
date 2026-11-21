"""Offline checks for the semantic canary's secret handling and receipt (#113)."""

from __future__ import annotations

import json

import pytest

from scripts import semantic_canary as canary


def _args(**overrides):
    argv = [
        "--qdrant-url",
        "http://127.0.0.1:9",
        "--embedder-url",
        "http://127.0.0.1:9/v1",
        "--model",
        "synthetic-model",
    ]
    for key, value in overrides.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return canary.parse_args(argv)


def test_server_env_is_minimal_and_carries_secrets_by_name(tmp_path, monkeypatch):
    monkeypatch.setenv("CANARY_LEAK_SENTINEL", "operator-value")
    monkeypatch.setenv("OPENAI_API_KEY", "operator-openai-key")
    profile = canary.build_profile(
        model="m",
        dimension=8,
        embedder_url="http://embed/v1",
        enrichment_url="http://enrich/v1",
        enrichment_model="chat",
        key_env="CANARY_EMBEDDER_API_KEY",
    )
    env = canary.server_env(
        tmp_path,
        qdrant_url="http://qdrant",
        qdrant_key="q-secret",
        profile_id="canary-test",
        profile=profile,
        key_env="CANARY_EMBEDDER_API_KEY",
        embedder_key="e-secret",
    )
    assert "CANARY_LEAK_SENTINEL" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["PYTHON_DOTENV_DISABLED"] == "1"
    assert env["SEMANTIC_AUTOSTART_QDRANT"] == "false"
    assert env["QDRANT_API_KEY"] == "q-secret"
    assert env["CANARY_EMBEDDER_API_KEY"] == "e-secret"
    profiles = json.loads(env["SEMANTIC_PROFILES_JSON"])
    assert env["SEMANTIC_DEFAULT_PROFILE"] == "canary-test"
    assert profiles["canary-test"]["build_metadata"]["embedding_api_key_env"] == (
        "CANARY_EMBEDDER_API_KEY"
    )
    # The profile names the key's variable, never the key itself.
    assert "e-secret" not in env["SEMANTIC_PROFILES_JSON"]
    assert "q-secret" not in env["SEMANTIC_PROFILES_JSON"]


def test_profile_without_key_env_names_no_key():
    profile = canary.build_profile(
        model="m",
        dimension=8,
        embedder_url="http://embed/v1",
        enrichment_url="http://embed/v1",
        enrichment_model="chat",
        key_env=None,
    )
    assert not any(key.endswith("_key_env") for key in profile["build_metadata"])


def test_missing_qdrant_key_fails_closed_without_secrets(monkeypatch):
    monkeypatch.delenv("QDRANT_API_KEY", raising=False)
    receipt = canary.run_canary(_args())
    assert receipt["result"] == "fail"
    assert receipt["failed_check"] == "qdrant_api_key_missing"
    assert set(receipt) <= canary.RECEIPT_KEYS


def test_receipt_rejects_unexpected_keys(tmp_path):
    with pytest.raises(ValueError):
        canary.write_receipt(tmp_path / "r.json", {"schema": "x", "env": {}})
    path = tmp_path / "nested" / "r.json"
    canary.write_receipt(path, {"schema": canary.RECEIPT_SCHEMA, "result": "fail"})
    assert json.loads(path.read_text())["result"] == "fail"


def test_anonymous_checks_require_every_status_to_be_401():
    canary.require_anonymous_denied([401, 401], "x")
    for statuses in ([], [401, 200], [0]):
        with pytest.raises(canary.CanaryFailure):
            canary.require_anonymous_denied(statuses, "x")


def test_search_check_requires_top_hit_and_linkage():
    good = {
        "results": [
            {
                "file": "/r/balance.py",
                "semantic_source": "semantic",
                "semantic_profile_id": "canary-a",
                "semantic_collection_name": "ci__x__canary-a__g",
            }
        ],
        "semantic_source": "semantic",
        "semantic_profile_id": "canary-a",
        "semantic_collection_name": "ci__x__canary-a__g",
    }
    assert canary.check_search(good, "balance.py", "canary-a") == "ci__x__canary-a__g"
    with pytest.raises(canary.CanaryFailure, match="semantic_top_hit_mismatch"):
        canary.check_search(good, "catalog.py", "canary-a")
    with pytest.raises(canary.CanaryFailure, match="semantic_linkage_mismatch"):
        canary.check_search(good, "balance.py", "other")
    with pytest.raises(canary.CanaryFailure, match="semantic_query_refused"):
        canary.check_search({"code": "semantic_not_ready", "results": []}, "balance.py", "a")


def test_failure_code_unwraps_task_groups():
    nested = ExceptionGroup("outer", [ExceptionGroup("inner", [canary.CanaryFailure("x_code")])])
    assert canary.failure_code(nested) == "x_code"
    assert canary.failure_code(ExceptionGroup("g", [TimeoutError()])) == "timeout"
    assert canary.failure_code(ValueError("secret text")) == "unexpected_ValueError"
