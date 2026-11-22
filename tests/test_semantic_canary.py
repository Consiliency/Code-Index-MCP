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


def test_queries_share_no_lexical_term_with_any_fixture_file():
    texts = [*canary.CORPUS, *canary.CORPUS.values()]
    for query, expected in canary.QUERIES:
        assert expected in canary.CORPUS
        assert canary.overlap_terms(query, texts) == set(), query
    # The helper does catch overlap (the old, lexically matching queries).
    assert canary.overlap_terms("find a product by its code", texts) == {
        "find",
        "product",
        "code",
        "its",
    }


def test_owned_collections_are_new_and_in_the_exact_namespace():
    prefix = canary.namespace_prefix("repo-a", "canary-" + "a" * 32)
    other_repo = canary.namespace_prefix("repo-b", "canary-" + "a" * 32)
    assert prefix.startswith("ci__") and prefix.endswith("__canary-" + "a" * 32 + "__")
    before = {prefix + "old", "unrelated"}
    after = before | {prefix + "gen1", other_repo + "gen1", "ci__x__canary-b__gen", "new-other"}
    assert canary.owned_collections(before, after, prefix) == [prefix + "gen1"]
    assert canary.RUN_ID_BYTES >= 16


def test_anonymous_verdict():
    assert canary.anonymous_verdict([401, 401], 401) is None
    assert canary.anonymous_verdict(None, 401) is None
    assert canary.anonymous_verdict([401, 200], 401) == "anonymous_access_during"
    assert canary.anonymous_verdict([], 401) == "anonymous_access_during"
    assert canary.anonymous_verdict([401], 0) == "anonymous_access_after"
    assert canary.anonymous_verdict(None, 200) == "anonymous_access_after"


@pytest.fixture
def fake_flow(monkeypatch):
    """Stub every external step so run_canary's bookkeeping can be checked offline."""
    state = {"anon": [], "session": None, "rmtree_fails": False, "deleted": []}
    monkeypatch.setenv("QDRANT_API_KEY", "q-secret")

    def anonymous_status(_url):
        return state["anon"].pop(0) if len(state["anon"]) > 1 else state["anon"][0]

    async def run_session(*_args, **_kwargs):
        if isinstance(state["session"], Exception):
            raise state["session"]
        return state["session"]

    real_rmtree = canary.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        real_rmtree(path, ignore_errors=True)
        if state["rmtree_fails"]:
            raise OSError("synthetic")

    monkeypatch.setattr(canary, "anonymous_status", anonymous_status)
    monkeypatch.setattr(canary, "ANONYMOUS_PROBE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(canary, "http_json", lambda *a, **k: (200, None))
    monkeypatch.setattr(canary, "probe_dimension", lambda *a: 8)
    monkeypatch.setattr(canary, "create_repository", lambda root: root / "repo")
    monkeypatch.setattr(canary, "server_env", lambda *a, **k: {})
    monkeypatch.setattr(canary, "run_quiet", lambda *a, **k: None)
    monkeypatch.setattr(canary, "read_repository_id", lambda root: "repo-id")
    monkeypatch.setattr(canary, "list_collections", lambda *a: set())
    monkeypatch.setattr(canary, "run_session", run_session)
    monkeypatch.setattr(canary, "collection_shape", lambda *a: (8, 3))
    monkeypatch.setattr(canary, "require_file_vectors", lambda *a: None)
    monkeypatch.setattr(
        canary,
        "delete_owned_collections",
        lambda url, key, before, prefix: state["deleted"].append(prefix) or True,
    )
    monkeypatch.setattr(canary.shutil, "rmtree", rmtree)
    state["prefix"] = lambda profile: canary.namespace_prefix("repo-id", profile)
    return state


def _session_collection(monkeypatch, state):
    """Make the fake session return a collection in the run's own namespace."""
    real = canary.namespace_prefix

    def capture(repo_id, profile_id):
        prefix = real(repo_id, profile_id)
        if state["session"] is None:
            state["session"] = prefix + "gen"
        return prefix

    monkeypatch.setattr(canary, "namespace_prefix", capture)


def test_fake_flow_passes_and_cleans_up(fake_flow, monkeypatch):
    fake_flow["anon"] = [401]
    _session_collection(monkeypatch, fake_flow)
    receipt = canary.run_canary(_args())
    assert receipt["result"] == "pass", receipt["failed_check"]
    assert len(fake_flow["deleted"]) == 1


def test_anonymous_failure_during_a_failed_session_outranks_it(fake_flow):
    fake_flow["anon"] = [401, 200]
    fake_flow["session"] = canary.CanaryFailure("semantic_top_hit_mismatch")
    receipt = canary.run_canary(_args())
    assert receipt["result"] == "fail"
    assert receipt["failed_check"] == "anonymous_access_during"
    assert len(fake_flow["deleted"]) == 1  # cleanup still ran


def test_anonymous_failure_after_an_earlier_failure_outranks_it(fake_flow, monkeypatch):
    fake_flow["anon"] = [401, 0]

    def fail(*_args):
        raise canary.CanaryFailure("embedder_dimension_probe_failed")

    monkeypatch.setattr(canary, "probe_dimension", fail)
    receipt = canary.run_canary(_args())
    assert receipt["failed_check"] == "anonymous_access_after"
    assert fake_flow["deleted"] == []  # nothing was created, nothing is deleted


def test_temporary_cleanup_failure_fails_a_passing_run(fake_flow, monkeypatch):
    fake_flow["anon"] = [401]
    fake_flow["rmtree_fails"] = True
    _session_collection(monkeypatch, fake_flow)
    receipt = canary.run_canary(_args())
    assert receipt["result"] == "fail"
    assert receipt["failed_check"] == "temporary_cleanup_failed"
