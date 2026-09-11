"""Live Git and generation admission controls for public query surfaces."""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mcp_server.health.repository_readiness import ReadinessClassifier
from mcp_server.storage.repository_registry import RepositoryRegistry
from mcp_server.storage.sqlite_store import SQLiteStore


def test_master_branch_identity_is_not_rewritten(tmp_path):
    repo = tmp_path / "master-repo"
    repo.mkdir()
    for args in (
        ["init", "-b", "master"],
        ["config", "user.name", "Synthetic"],
        ["config", "user.email", "fixture@example.invalid"],
        ["commit", "--allow-empty", "-qm", "Synthetic branch"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    registry = RepositoryRegistry(tmp_path / "registry.json")
    repo_id = registry.register_repository(str(repo))
    assert registry.get(repo_id).tracked_branch == "master"
    assert registry.update_git_state(repo_id)["branch"] == "master"


@pytest.mark.asyncio
@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("surface", ["stdio", "http", "python"])
@pytest.mark.parametrize(
    "state", ["match", "no_match", "dirty", "branch", "commit", "sibling", "generation_race"]
)
async def test_public_queries_admit_only_current_production_generations(
    tmp_path, monkeypatch, object_format, surface, state
):
    from fastapi import HTTPException
    from starlette.requests import Request

    import mcp_server.gateway as gateway
    from mcp_server import ClientSearchOptions, open_client
    from mcp_server.cli.bootstrap import initialize_stateless_services
    from mcp_server.cli.tool_handlers import handle_search_code

    repo = tmp_path / "admission"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-b", "main", f"--object-format={object_format}")
    git("config", "user.name", "Synthetic")
    git("config", "user.email", "fixture@example.invalid")
    (repo / "seed.py").write_text("def data_admission_token():\n    return 7\n")
    (repo / ".gitignore").write_text(".mcp-index/\n")
    git("add", ".")
    git("commit", "-qm", "Committed input")
    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("MCP_ENABLE_MULTI_REPO", "false")
    registry_path = tmp_path / "registry.json"
    stores, resolver, dispatcher, registry, manager = initialize_stateless_services(registry_path)
    repo_id = registry.register_repository(str(repo))
    direct = None
    try:
        result = manager.rebuild_repository_index(repo_id)
        assert result.action == "full_index", result.error
        selected = repo
        if state == "dirty":
            (repo / "seed.py").write_text("changed = 1\n")
        elif state == "branch":
            git("switch", "-c", "feature")
        elif state == "commit":
            (repo / "seed.py").write_text("changed = 1\n")
            git("add", "seed.py")
            git("commit", "-qm", "New input")
        elif state == "sibling":
            selected = tmp_path / "sibling"
            git("worktree", "add", "-b", "sibling", str(selected))
        if surface == "python":
            direct = open_client(workspace_root=selected, registry_path=registry_path)
            query_dispatcher = direct.dispatcher
        else:
            query_dispatcher = dispatcher
        if state == "generation_race":
            original = query_dispatcher.search

            def changed(*args, **kwargs):
                rows = list(original(*args, **kwargs))
                assert rows, "The interrupted query must first find a real result"
                current = registry.get(repo_id)
                registry.update_indexed_commit(repo_id, current.last_indexed_commit, branch="main")
                return rows

            monkeypatch.setattr(query_dispatcher, "search", changed)
        query = "missing_token_584693" if state == "no_match" else "data_admission_token"
        if surface == "python":
            response = direct.search_code(ClientSearchOptions(query=query))
            refusal = response.index_unavailable
            rows = response.results
            fallback = refusal.safe_fallback if refusal else None
        elif surface == "stdio":
            blocks = await handle_search_code(
                arguments={"query": query, "repository": str(selected), "semantic": False},
                dispatcher=dispatcher,
                repo_resolver=resolver,
            )
            response = json.loads(blocks[0].text)
            refusal = isinstance(response, dict) and response.get("code") == "index_unavailable"
            rows = response if isinstance(response, list) else response.get("results", [])
            fallback = response.get("safe_fallback") if isinstance(response, dict) else None
        else:
            monkeypatch.setattr(gateway, "repo_resolver", resolver)
            monkeypatch.setattr(gateway, "dispatcher", dispatcher)
            monkeypatch.setattr(gateway, "query_cache", None)
            monkeypatch.setattr(gateway, "metrics_collector", MagicMock())
            monkeypatch.setattr(gateway, "business_metrics", MagicMock())
            from urllib.parse import urlencode

            request = Request(
                {
                    "type": "http",
                    "headers": [],
                    "query_string": urlencode({"repository": str(selected)}).encode(),
                }
            )
            try:
                rows = await gateway.search(
                    request, q=query, current_user=SimpleNamespace(username="synthetic")
                )
                refusal, fallback = False, None
            except HTTPException as exc:
                assert exc.status_code == 503, exc.detail
                refusal, rows = True, []
                fallback = exc.detail.get("safe_fallback")
        if state in {"match", "no_match"}:
            assert not refusal
            assert bool(rows) == (state == "match")
        else:
            assert refusal and not rows
            assert fallback == "native_search"
    finally:
        if direct is not None:
            direct.close()
        dispatcher.shutdown()
        stores.shutdown()


def test_production_bootstrap_rebuild_uses_supplied_registry(tmp_path, monkeypatch):
    from mcp_server.cli.bootstrap import initialize_stateless_services
    from tests.fixtures.multi_repo import build_temp_repo

    repo, repo_id = build_temp_repo(
        tmp_path, "production", seed_files={"seed.py": "def data_token():\n    return 7\n"}
    )
    monkeypatch.setenv("MCP_ENABLE_MULTI_REPO", "true")
    monkeypatch.delenv("MCP_TEST_MODE", raising=False)
    registry_path = tmp_path / "selected-registry.json"
    stores, resolver, dispatcher, registry, manager = initialize_stateless_services(registry_path)
    try:
        registry.register_repository(str(repo))
        assert dispatcher._multi_repo_manager.registry.registry_path == registry_path
        result = manager.rebuild_repository_index(repo_id)
        assert result.action == "full_index", result.error
        ctx = resolver.resolve_ready(repo)
        assert list(dispatcher.search(ctx, "data_token"))
        import asyncio

        assert asyncio.run(dispatcher.cross_repo_code_search([ctx], "data_token"))["results"]
        refused = asyncio.run(dispatcher.cross_repo_code_search([ctx], "data_token", semantic=True))
        assert refused["code"] == "index_unavailable"
        assert refused["safe_fallback"] == "native_search"
        info = registry.get(repo_id)
        registry.update_indexed_commit(repo_id, info.last_indexed_commit, branch="main")
        for method in (dispatcher.cross_repo_code_search, dispatcher.cross_repo_symbol_search):
            refused = asyncio.run(method([ctx], "data_token"))
            assert refused["code"] == "index_unavailable" and not refused["results"]
    finally:
        dispatcher.shutdown()
        stores.shutdown()


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("change", ["dirty", "staged", "branch", "commit"])
@pytest.mark.parametrize("cached_identity", ["valid", "malformed", "no_common_dir"])
def test_live_git_state_refuses_ready_for_both_object_formats(
    tmp_path, monkeypatch, object_format, change, cached_identity
):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-b", "main", f"--object-format={object_format}")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    file = repo / "hello.py"
    file.write_text("hello = 1\n")
    git("add", ".")
    git("commit", "-m", "fixture")
    commit = git("rev-parse", "HEAD")
    monkeypatch.setenv("MCP_INDEX_STORAGE_PATH", str(tmp_path / "indexes"))
    registry = RepositoryRegistry(tmp_path / "registry.json")
    repo_id = registry.register_repository(str(repo))
    info = registry.get(repo_id)
    info.index_path.parent.mkdir(parents=True)
    store = SQLiteStore(str(info.index_path))
    try:
        row = store.ensure_repository_row(repo)
        store.store_file(row, file, "hello.py")
        registry.update_indexed_commit(repo_id, commit, branch="main")
        info = registry.get(repo_id)
        if cached_identity == "malformed":
            info.current_commit = "not-a-git-object"
        elif cached_identity == "no_common_dir":
            info.git_common_dir = None
        assert ReadinessClassifier.classify_registered(info).ready
        if change == "branch":
            git("switch", "-c", "feature")
        else:
            file.write_text("hello = 2\n")
            if change in {"staged", "commit"}:
                git("add", ".")
            if change == "commit":
                git("commit", "-m", "changed")
        readiness = ReadinessClassifier.classify_registered(info)
        assert not readiness.ready
        assert readiness.code == ("wrong_branch" if change == "branch" else "stale_commit")
    finally:
        store.close()
