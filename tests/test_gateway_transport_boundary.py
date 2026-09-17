from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from mcp_server import __version__
from mcp_server.gateway import app


def test_gateway_exposes_admin_routes_but_no_mcp_transport_endpoint():
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}

    for expected in ("/symbol", "/search", "/status", "/plugins", "/reindex"):
        assert expected in paths

    for disallowed in ("/mcp", "/messages", "/sse"):
        assert disallowed not in paths


def test_gateway_metadata_keeps_fastapi_admin_positioning():
    assert app.title == "MCP Server"
    assert "Code Index MCP Server" in (app.description or "")


@pytest.mark.parametrize(
    "path,method", [("/symbol", "get"), ("/search", "get"), ("/reindex", "post")]
)
def test_admin_ui_exposes_existing_repository_selector(path, method):
    operation = app.openapi()["paths"][path][method]
    selectors = [item for item in operation["parameters"] if item["name"] == "repository"]
    assert len(selectors) == 1
    assert selectors[0]["in"] == "query"
    assert selectors[0]["required"] is False


def test_admin_ui_reports_installed_package_version():
    assert app.openapi()["info"]["version"] == __version__
