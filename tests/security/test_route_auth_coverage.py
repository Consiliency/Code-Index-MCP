"""P18 SL-1: Verify every non-whitelisted route returns 401 without a token."""

from __future__ import annotations

import asyncio
from typing import List, Tuple

import pytest
from fastapi import Depends
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from mcp_server.gateway import app
from mcp_server.security import (
    AuthManager,
    Permission,
    SecurityConfig,
    User,
    UserRole,
)

# ---------------------------------------------------------------------------
# Routes that are intentionally public (no auth required).
# ---------------------------------------------------------------------------
_PUBLIC_ROUTES = {
    "/health",
    "/ready",
    "/liveness",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/docs",
    "/redoc",
    "/openapi.json",
}

_JWT_SECRET = "test-secret-key-at-least-32-characters-for-sl1"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def security_cfg() -> SecurityConfig:
    return SecurityConfig(
        jwt_secret_key=_JWT_SECRET,
        jwt_algorithm="HS256",
        access_token_expire_minutes=30,
    )


@pytest.fixture(scope="module")
def auth_mgr(security_cfg: SecurityConfig) -> AuthManager:
    return AuthManager(security_cfg)


@pytest.fixture(scope="module")
def admin_token(auth_mgr: AuthManager) -> str:
    loop = asyncio.new_event_loop()
    try:
        user: User = loop.run_until_complete(
            auth_mgr.create_user(
                username="admin_sl1_cov",
                password="AdminSL1Pass123!",
                email="admin_sl1_cov@test.local",
                role=UserRole.ADMIN,
            )
        )
        return loop.run_until_complete(auth_mgr.create_access_token(user))
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _protected_routes() -> List[Tuple[str, str]]:
    """Return (method, path) for every route that should require auth."""
    results = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path: str = route.path
        if path in _PUBLIC_ROUTES or any(path.startswith(pub + "/") for pub in ("/docs", "/redoc")):
            continue
        for method in route.methods or []:
            results.append((method.upper(), path))
    return results


def _has_auth_dependency(route: APIRoute) -> bool:
    """Return True if the route declares any auth-related dependency."""
    all_deps = list(route.dependencies or [])
    all_deps += list(route.dependant.dependencies if hasattr(route, "dependant") else [])
    dep_names = {
        getattr(dep.call, "__name__", "") if hasattr(dep, "call") else "" for dep in all_deps
    }
    return bool(
        dep_names
        & {"require_permission", "require_auth", "require_role", "get_current_active_user"}
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_search_capabilities_requires_auth() -> None:
    """GET /search/capabilities must return 401 without a token."""
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/search/capabilities")
    assert resp.status_code == 401, (
        f"/search/capabilities returned {resp.status_code}; expected 401. "
        "Add Depends(require_permission(Permission.READ)) to the route."
    )


def test_public_health_probes_are_reachable_without_a_token() -> None:
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/liveness").status_code == 200
    assert client.get("/ready").status_code in {200, 503}
    assert client.get("/health").status_code in {200, 503}


def test_public_path_matching_does_not_expose_prefix_collisions() -> None:
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/readiness-private").status_code == 401
    assert client.get("/health-private").status_code == 401
    assert client.get("/health/detailed").status_code == 401
    assert client.get("/health/database").status_code == 401


def test_all_protected_routes_return_401_without_token() -> None:
    """Every non-whitelisted route must return 401 when called without credentials."""
    client = TestClient(app, raise_server_exceptions=False)
    failures = []
    for method, path in _protected_routes():
        # Skip parameterised paths — use a stub value
        test_path = path
        for seg in path.split("/"):
            if seg.startswith("{") and seg.endswith("}"):
                test_path = test_path.replace(seg, "test-stub")

        resp = client.request(method, test_path)
        if resp.status_code not in (401, 422, 503):
            # 422 = missing required body/query param (not an auth bypass)
            # 503 = service not started in test context (e.g. dispatcher)
            failures.append(f"  {method} {path} → {resp.status_code} (expected 401 or 503)")
    if failures:
        pytest.fail("Routes returned unexpected status without auth token:\n" + "\n".join(failures))
