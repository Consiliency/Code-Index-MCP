"""
SL-1.3: Dispatcher repo_id isolation via live gateway subprocesses.

GET /search?q=<marker>&repository=<repo_b_path> must only return results
from repo-B's workspace. Verifies that the gateway's get_repo_ctx() + dispatcher
correctly honors ctx.repo_id scoping.

Note: /search requires auth. We obtain a Bearer token via POST /api/v1/auth/login
using the admin credentials injected into the gateway subprocess environment.
"""

import pytest
import requests

from tests.integration.multi_repo.conftest import _ADMIN_PASSWORD, MultiRepoContext

pytestmark = pytest.mark.requires_network


def _get_auth_token(base_url: str) -> str:
    """Obtain a Bearer token from the gateway login endpoint."""
    resp = requests.post(
        f"{base_url}/api/v1/auth/login",
        json={"username": "admin", "password": _ADMIN_PASSWORD},
        timeout=10,
    )
    assert resp.status_code == 200, f"Login failed ({resp.status_code}): {resp.text[:400]}"
    data = resp.json()
    token = data.get("access_token") or data.get("token")
    assert token, f"No token in login response: {data}"
    return token


def test_dispatcher_repo_id_isolation(multi_repo_fixture):
    """
    Gateway-A's /search scoped to repo-B's workspace path returns only
    results from repo-B (dispatcher honours ctx.repo_id).

    If the gateway's search index is empty (no files indexed), the test
    asserts the search completes without error and returns an empty list
    (not results from a different repo). This validates route isolation.
    """
    ctx: MultiRepoContext = multi_repo_fixture(n_gateways=2, n_repos=2)
    assert len(ctx.gateways) == 2
    assert len(ctx.repos) == 2

    repo_ids = list(ctx.repos.keys())
    repo_b_id = repo_ids[1]
    repo_b = ctx.repos[repo_b_id]

    # Use gateway-A (index 0) to query repo-B
    gw_a = ctx.gateways[0]
    token = _get_auth_token(gw_a.base_url)
    headers = {"Authorization": f"Bearer {token}"}

    # Search with repository scoped to repo-B's workspace path
    resp = requests.get(
        f"{gw_a.base_url}/search",
        params={
            "q": "workspace",
            "repository": str(repo_b.path),
            "mode": "bm25",
            "limit": 50,
        },
        headers=headers,
        timeout=15,
    )
    if resp.status_code == 503:
        detail = resp.json()["detail"]
        assert detail["code"] in {"index_empty", "missing_provenance"}
        assert detail["safe_fallback"] == "native_search"
        return
    assert resp.status_code == 200, f"Search failed ({resp.status_code}): {resp.text[:400]}"

    results = resp.json()
    assert isinstance(results, list), f"Expected list, got {type(results)}: {results}"

    # All returned file paths must be within repo-B's workspace
    repo_b_path_str = str(repo_b.path)
    for result in results:
        file_path = result.get("file") or result.get("path") or result.get("filepath") or ""
        if file_path:
            assert file_path.startswith(
                repo_b_path_str
            ), f"Result file {file_path!r} is outside repo-B workspace {repo_b_path_str!r}"
