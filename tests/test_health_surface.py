"""Tests for IF-0-P10-4 and IF-0-P10-7: multi-repo health surface (SL-4)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tests.fixtures.health_repo import make_repo_info

EXPECTED_KEYS = {
    "repo_id",
    "tracked_branch",
    "index_path_exists",
    "git_dir_exists",
    "last_indexed_commit",
    "artifact_health",
    "staleness_reason",
    "readiness",
    "ready",
    "readiness_code",
    "remediation",
    "semantic_readiness",
    "semantic_ready",
    "semantic_readiness_code",
    "semantic_remediation",
    "rollout_status",
    "rollout_remediation",
    "query_status",
    "query_remediation",
    "features",
}


# ---------------------------------------------------------------------------
# build_health_row unit tests
# ---------------------------------------------------------------------------


class TestBuildHealthRow:
    def test_returns_expected_keys(self, tmp_path):
        from mcp_server.health.repo_status import build_health_row

        info = make_repo_info(tmp_path)
        row = build_health_row(info)
        assert set(row.keys()) == EXPECTED_KEYS

    def test_healthy_repo(self, tmp_path):
        from mcp_server.health.repo_status import build_health_row

        info = make_repo_info(tmp_path)
        row = build_health_row(info)
        assert row["staleness_reason"] is None
        assert row["readiness"] == "ready"
        assert row["ready"] is True
        assert row["semantic_readiness"] == "enrichment_unavailable"
        assert row["semantic_ready"] is False
        assert row["rollout_status"] == "local_only"
        assert row["query_status"] == "ready"
        assert row["index_path_exists"] is True
        assert row["git_dir_exists"] is True

    def test_missing_git_dir(self, tmp_path):
        from mcp_server.health.repo_status import build_health_row

        info = make_repo_info(tmp_path, missing_git_dir=True)
        row = build_health_row(info)
        assert row["staleness_reason"] == "missing_git_dir"
        assert row["git_dir_exists"] is False

    def test_missing_index(self, tmp_path):
        from mcp_server.health.repo_status import build_health_row

        info = make_repo_info(tmp_path, missing_index=True)
        row = build_health_row(info)
        assert row["staleness_reason"] == "missing_index"
        assert row["readiness"] == "missing_index"
        assert row["rollout_status"] == "missing_index"
        assert row["query_status"] == "index_unavailable"
        assert row["index_path_exists"] is False

    def test_empty_index(self, tmp_path):
        from mcp_server.health.repo_status import build_health_row

        info = make_repo_info(tmp_path, empty_index=True)
        row = build_health_row(info)
        assert row["staleness_reason"] == "index_empty"
        assert row["readiness"] == "index_empty"

    def test_stale_commit(self, tmp_path):
        from mcp_server.health.repo_status import build_health_row

        info = make_repo_info(tmp_path, current_commit="bbbb", last_indexed_commit="aaaa")
        row = build_health_row(info)
        assert row["staleness_reason"] == "stale_commit"
        assert row["readiness"] == "stale_commit"

    def test_wrong_branch(self, tmp_path):
        from mcp_server.health.repo_status import build_health_row

        info = make_repo_info(tmp_path, tracked_branch="main", current_branch="feature")
        row = build_health_row(info)
        assert row["staleness_reason"] == "wrong_branch"
        assert row["readiness"] == "wrong_branch"
        assert row["rollout_status"] == "wrong_branch"

    def test_partial_index_failure_overrides_other_staleness(self, tmp_path):
        from mcp_server.health.repo_status import build_health_row

        info = make_repo_info(
            tmp_path,
            current_commit="bbbb",
            last_indexed_commit="aaaa",
        )
        info.staleness_reason = "partial_index_failure"
        row = build_health_row(info)
        assert row["staleness_reason"] == "partial_index_failure"
        assert row["rollout_status"] == "partial_index_failure"
        assert row["readiness"] == "missing_provenance"

    def test_index_building(self, tmp_path):
        from mcp_server.health.repo_status import build_health_row
        from mcp_server.health.repository_readiness import ReadinessClassifier

        info = make_repo_info(tmp_path)
        readiness = ReadinessClassifier.classify_registered(info, indexing_active=True)
        row = build_health_row(info, readiness=readiness)
        assert row["staleness_reason"] == "index_building"
        assert row["readiness"] == "index_building"

    def test_missing_git_dir_takes_priority_over_missing_index(self, tmp_path):
        from mcp_server.health.repo_status import build_health_row

        info = make_repo_info(tmp_path, missing_git_dir=True, missing_index=True)
        row = build_health_row(info)
        assert row["staleness_reason"] == "missing_git_dir"

    def test_repo_id_matches(self, tmp_path):
        from mcp_server.health.repo_status import build_health_row

        info = make_repo_info(tmp_path, repo_id="my-repo")
        row = build_health_row(info)
        assert row["repo_id"] == "my-repo"

    def test_tracked_branch(self, tmp_path):
        from mcp_server.health.repo_status import build_health_row

        info = make_repo_info(tmp_path, tracked_branch="dev")
        row = build_health_row(info)
        assert row["tracked_branch"] == "dev"

    def test_last_indexed_commit_none(self, tmp_path):
        from mcp_server.health.repo_status import build_health_row

        info = make_repo_info(tmp_path, last_indexed_commit=None)
        row = build_health_row(info)
        assert row["last_indexed_commit"] is None

    def test_semantic_preflight_is_additive_runtime_metadata(self, tmp_path):
        from mcp_server.health.repo_status import build_health_row

        info = make_repo_info(tmp_path)
        row = build_health_row(
            info,
            semantic_preflight={
                "overall_ready": False,
                "can_write_semantic_vectors": False,
                "blocker": {
                    "code": "collection_missing",
                    "message": "Qdrant collection is missing for the active semantic profile",
                    "failing_checks": [],
                    "remediation": ["Create the expected collection"],
                    "can_write_semantic_vectors": False,
                },
            },
        )
        semantic_feature = row["features"]["semantic"]
        assert semantic_feature["preflight"]["overall_ready"] is False
        assert semantic_feature["preflight"]["blocker"]["code"] == "collection_missing"


# ---------------------------------------------------------------------------
# stdio handle_get_status includes repositories
# ---------------------------------------------------------------------------


class TestStdioHandleGetStatusRepositories:
    def _make_registry_mock(self, tmp_path):
        info = make_repo_info(tmp_path, repo_id="repo-a")
        registry = Mock()
        registry.get_all_repositories.return_value = {"repo-a": info}
        return registry

    def _make_resolver_mock(self, tmp_path):
        registry = self._make_registry_mock(tmp_path)
        resolver = Mock()
        resolver._registry = registry
        return resolver

    def test_repositories_key_present(self, tmp_path):
        from mcp_server.cli.tool_handlers import handle_get_status
        from mcp_server.dispatcher.simple_dispatcher import SimpleDispatcher

        dispatcher = SimpleDispatcher()
        resolver = self._make_resolver_mock(tmp_path)

        result = asyncio.new_event_loop().run_until_complete(
            handle_get_status(
                arguments={},
                dispatcher=dispatcher,
                repo_resolver=resolver,
            )
        )
        data = json.loads(result[0].text)
        assert "repositories" in data

    def test_repositories_entries_match_registry(self, tmp_path):
        from mcp_server.cli.tool_handlers import handle_get_status
        from mcp_server.dispatcher.simple_dispatcher import SimpleDispatcher

        dispatcher = SimpleDispatcher()
        resolver = self._make_resolver_mock(tmp_path)

        result = asyncio.new_event_loop().run_until_complete(
            handle_get_status(
                arguments={},
                dispatcher=dispatcher,
                repo_resolver=resolver,
            )
        )
        data = json.loads(result[0].text)
        repos = data["repositories"]
        assert len(repos) == 1
        assert set(repos[0].keys()) == EXPECTED_KEYS
        assert repos[0]["repo_id"] == "repo-a"

    def test_repositories_empty_when_no_registry(self, tmp_path):
        from mcp_server.cli.tool_handlers import handle_get_status
        from mcp_server.dispatcher.simple_dispatcher import SimpleDispatcher

        dispatcher = SimpleDispatcher()

        result = asyncio.new_event_loop().run_until_complete(
            handle_get_status(
                arguments={},
                dispatcher=dispatcher,
                repo_resolver=None,
            )
        )
        data = json.loads(result[0].text)
        assert data["repositories"] == []


def test_stdio_health_omits_plugin_exception_text(monkeypatch):
    from types import SimpleNamespace

    from mcp_server.cli.tool_handlers import handle_get_status
    from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher
    from mcp_server.dispatcher.simple_dispatcher import SimpleDispatcher

    marker = "private-plugin-exception-marker"
    plugin = SimpleNamespace(get_indexed_count=lambda: (_ for _ in ()).throw(RuntimeError(marker)))
    enhanced = EnhancedDispatcher.__new__(EnhancedDispatcher)
    enhanced._lang_cache = {"python": plugin}
    enhanced._use_factory = False
    enhanced._lazy_load = False
    monkeypatch.setattr(enhanced, "_managed_plugin_count", lambda repo_id: 1)
    monkeypatch.setattr(enhanced, "supported_languages", lambda: ["python"])
    monkeypatch.setattr(
        enhanced,
        "get_runtime_feature_status",
        lambda ctx: {"graph": {"status": "unavailable"}},
    )
    health = enhanced.health_check(SimpleNamespace(repo_id="test-repo"))
    assert health["plugins"]["python"]["error"] == "plugin_health_failed"

    dispatcher = SimpleDispatcher()
    monkeypatch.setattr(dispatcher, "get_statistics", lambda: {})
    monkeypatch.setattr(dispatcher, "health_check", lambda: health)
    result = asyncio.run(handle_get_status(arguments={}, dispatcher=dispatcher, repo_resolver=None))
    assert marker not in result[0].text
    assert "plugin_health_failed" in result[0].text


# ---------------------------------------------------------------------------
# HTTP gateway.get_status includes repositories
# ---------------------------------------------------------------------------


class TestGatewayGetStatusRepositories:
    def _make_gateway_registry(self, tmp_path):
        info = make_repo_info(tmp_path, repo_id="repo-b")
        registry = Mock()
        registry.get_all_repositories.return_value = {"repo-b": info}
        return registry

    def _authenticated_client(self):
        from fastapi.testclient import TestClient

        import mcp_server.gateway as gateway
        from mcp_server.security import AuthManager, SecurityConfig, UserRole

        auth_manager = getattr(gateway.app.state, "auth_manager", None)
        if auth_manager is None:
            auth_manager = AuthManager(
                SecurityConfig(
                    jwt_secret_key=os.environ["JWT_SECRET_KEY"],
                    jwt_algorithm="HS256",
                    access_token_expire_minutes=30,
                )
            )
            gateway.app.state.auth_manager = auth_manager
        user = asyncio.run(auth_manager.get_user_by_username("health_surface_admin"))
        if user is None:
            user = asyncio.run(
                auth_manager.create_user(
                    username="health_surface_admin",
                    password="HealthSurfaceAdmin123!",
                    email="health_surface_admin@test.local",
                    role=UserRole.ADMIN,
                )
            )
        token = asyncio.run(auth_manager.create_access_token(user))
        return TestClient(
            gateway.app,
            headers={"Authorization": f"Bearer {token}"},
            raise_server_exceptions=True,
        )

    def test_gateway_repositories_key_present(self, tmp_path, monkeypatch):
        import mcp_server.gateway as gateway

        registry = self._make_gateway_registry(tmp_path)
        monkeypatch.setattr(gateway, "_repo_registry", registry, raising=False)

        from mcp_server.dispatcher.simple_dispatcher import SimpleDispatcher

        monkeypatch.setattr(gateway, "dispatcher", SimpleDispatcher())
        for attr in (
            "bm25_indexer",
            "hybrid_search",
            "fuzzy_indexer",
            "semantic_indexer",
            "query_cache",
        ):
            if hasattr(gateway, attr):
                monkeypatch.setattr(gateway, attr, None)

        client = self._authenticated_client()
        response = client.get("/status")
        client.close()

        assert response.status_code == 200
        data = response.json()
        assert "repositories" in data

    def test_gateway_repositories_entries_match_registry(self, tmp_path, monkeypatch):
        import mcp_server.gateway as gateway

        registry = self._make_gateway_registry(tmp_path)
        monkeypatch.setattr(gateway, "_repo_registry", registry, raising=False)

        from mcp_server.dispatcher.simple_dispatcher import SimpleDispatcher

        monkeypatch.setattr(gateway, "dispatcher", SimpleDispatcher())
        for attr in (
            "bm25_indexer",
            "hybrid_search",
            "fuzzy_indexer",
            "semantic_indexer",
            "query_cache",
        ):
            if hasattr(gateway, attr):
                monkeypatch.setattr(gateway, attr, None)

        client = self._authenticated_client()
        response = client.get("/status")
        client.close()

        assert response.status_code == 200
        data = response.json()
        repos = data["repositories"]
        assert isinstance(repos, list)
        assert len(repos) == 1
        assert set(repos[0].keys()) == EXPECTED_KEYS
        assert repos[0]["repo_id"] == "repo-b"
