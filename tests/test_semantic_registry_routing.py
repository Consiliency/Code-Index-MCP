"""Standalone SemanticIndexer paths stay behind SemanticIndexerRegistry.

The HTTP gateway and the legacy ``index`` CLI must not build a standalone
``SemanticIndexer`` on a profile collection name. Semantic vectors are owned by
the generation-scoped ``SemanticIndexerRegistry`` (Consiliency/Code-Index-MCP#142).
No test here contacts Qdrant.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

import mcp_server.cli.index_management as index_management
from mcp_server.config.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
STANDALONE_ENTRY_POINTS = (
    REPO_ROOT / "mcp_server" / "gateway.py",
    REPO_ROOT / "mcp_server" / "cli" / "index_management.py",
)


def _forbidden_semantic_references(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name in {"SemanticIndexer", "get_semantic_indexer"}:
                    found.append(f"import {alias.name}")
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                "utils.semantic_indexer"
            ):
                found.append(f"from {node.module}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name in {"SemanticIndexer", "get_semantic_indexer", "_get_profile_collection_name"}:
                found.append(f"call {name}")
    return found


@pytest.mark.parametrize("path", STANDALONE_ENTRY_POINTS, ids=lambda p: p.name)
def test_entry_point_builds_no_standalone_semantic_indexer(path):
    assert _forbidden_semantic_references(path) == []


# --------------------------------------------------------------------------
# Legacy CLI
# --------------------------------------------------------------------------


@pytest.fixture
def no_semantic_indexer(monkeypatch):
    """Fail loudly if anything constructs a standalone SemanticIndexer."""
    import mcp_server.utils.semantic_indexer as semantic_module

    def _boom(*_args, **_kwargs):
        raise AssertionError("standalone SemanticIndexer constructed")

    monkeypatch.setattr(semantic_module, "SemanticIndexer", _boom)


@pytest.mark.parametrize(
    "args",
    [
        ["rebuild", "--vector-only"],
        ["rebuild", "--semantic-profile", "oss_high"],
        ["rebuild", "--force", "--vector-only"],
    ],
)
def test_legacy_vector_rebuild_refuses_with_pointer(monkeypatch, no_semantic_indexer, args):
    sqlite_build = Mock()
    monkeypatch.setattr(index_management, "_build_sqlite_baseline", sqlite_build)

    result = CliRunner().invoke(index_management.index, args)

    assert result.exit_code != 0
    assert "legacy semantic (vector) rebuild is disabled" in result.output
    assert "repository sync --force-full" in result.output
    sqlite_build.assert_not_called()


def test_rebuild_rebuilds_sqlite_only_and_points_at_registry(
    monkeypatch, tmp_path, no_semantic_indexer
):
    monkeypatch.chdir(tmp_path)
    sqlite_build = Mock(return_value={"indexed_files": 3})
    monkeypatch.setattr(index_management, "_build_sqlite_baseline", sqlite_build)

    result = CliRunner().invoke(index_management.index, ["rebuild"])

    assert result.exit_code == 0, result.output
    sqlite_build.assert_called_once_with()
    assert "Skipping vector index" in result.output
    assert "repository sync --force-full" in result.output
    assert "Files indexed: 3" in result.output
    assert not (tmp_path / "vector_index.qdrant").exists()


def test_rebuild_sqlite_only_skips_vector_notice(monkeypatch, tmp_path, no_semantic_indexer):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(index_management, "_build_sqlite_baseline", Mock(return_value={}))

    result = CliRunner().invoke(index_management.index, ["rebuild", "--sqlite-only"])

    assert result.exit_code == 0, result.output
    assert "Skipping vector index" not in result.output


def test_check_compatibility_does_not_probe_vectors(monkeypatch, tmp_path, no_semantic_indexer):
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(index_management.index, ["check-compatibility"])

    assert "generation-scoped, not checked here" in result.output
    assert "Not available (semantic deps not installed)" not in result.output


# --------------------------------------------------------------------------
# HTTP gateway
# --------------------------------------------------------------------------


def test_gateway_has_no_standalone_semantic_indexer_global():
    import mcp_server.gateway as gateway

    assert not hasattr(gateway, "semantic_indexer")
    assert hasattr(gateway, "semantic_generation_scoped_ready")


def _admin():
    return SimpleNamespace(username="admin")


def test_enabling_hybrid_semantic_branch_is_refused(monkeypatch):
    from fastapi import HTTPException

    import mcp_server.gateway as gateway

    hybrid = Mock()
    monkeypatch.setattr(gateway, "hybrid_search", hybrid)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(gateway.toggle_search_methods(semantic=True, current_user=_admin()))

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "hybrid_semantic_disabled"
    assert "mode=semantic" in excinfo.value.detail["message"]
    hybrid.enable_methods.assert_not_called()


def test_disabling_hybrid_semantic_branch_still_allowed(monkeypatch):
    import mcp_server.gateway as gateway

    hybrid = Mock()
    hybrid.config = SimpleNamespace(enable_bm25=True, enable_semantic=False, enable_fuzzy=True)
    monkeypatch.setattr(gateway, "hybrid_search", hybrid)

    result = asyncio.run(
        gateway.toggle_search_methods(semantic=False, fuzzy=True, current_user=_admin())
    )

    assert result["status"] == "success"
    hybrid.enable_methods.assert_called_once_with(bm25=None, semantic=False, fuzzy=True)


def test_search_rebuild_reports_semantic_as_generation_scoped(monkeypatch):
    import mcp_server.gateway as gateway

    monkeypatch.setattr(gateway, "hybrid_search", Mock())

    result = asyncio.run(
        gateway.rebuild_search_indexes(index_type="semantic", current_user=_admin())
    )

    assert result["results"]["semantic"] == "generation_scoped"
    assert "repository sync --force-full" in result["results"]["semantic_remediation"]


def test_shutdown_needs_no_semantic_client(monkeypatch):
    import mcp_server.gateway as gateway

    for owner in (
        "ref_poller",
        "multi_watcher",
        "dispatcher",
        "plugin_manager",
        "cache_manager",
        "_store_registry",
        "sqlite_store",
    ):
        monkeypatch.setattr(gateway, owner, None)

    asyncio.run(gateway.shutdown_event())


@pytest.mark.parametrize("registry_available", [True, False])
def test_startup_routes_semantic_through_registry_only(
    monkeypatch, no_semantic_indexer, registry_available
):
    from fastapi.testclient import TestClient

    import mcp_server.gateway as gateway

    settings = Settings.from_environment().model_copy(
        update={
            "semantic_search_enabled": True,
            "semantic_strict_mode": False,
            "semantic_autostart_qdrant": False,
        }
    )
    monkeypatch.setattr(gateway, "get_settings", lambda: settings)

    report = Mock()
    report.overall_ready = True
    report.qdrant.ok = True
    report.warnings = []
    report.to_dict.return_value = {"overall_ready": True}
    monkeypatch.setattr(gateway, "run_semantic_preflight", Mock(return_value=report))
    monkeypatch.setattr(
        gateway,
        "ensure_qdrant_running",
        Mock(side_effect=AssertionError("Qdrant autostart attempted")),
    )

    registry_cls = Mock(name="SemanticIndexerRegistry")
    if not registry_available:
        registry_cls.side_effect = RuntimeError("registry unavailable")

    preflight = type("PreflightResult", (), {"status": "warning", "checks": []})()
    with (
        patch("mcp_server.gateway.format_preflight_report", return_value=[]),
        patch("mcp_server.gateway.run_startup_preflight", return_value=preflight),
        patch("mcp_server.gateway.SQLiteStore"),
        patch("mcp_server.gateway.EnhancedDispatcher") as dispatcher_cls,
        patch("mcp_server.gateway.MultiRepositoryWatcher"),
        patch("mcp_server.storage.repository_registry.RepositoryRegistry"),
        patch("mcp_server.utils.semantic_indexer_registry.SemanticIndexerRegistry", registry_cls),
    ):
        with TestClient(gateway.app):
            hybrid = gateway.hybrid_search
            ready = gateway.semantic_generation_scoped_ready
            dispatcher_kwargs = dispatcher_cls.call_args.kwargs

    assert hybrid is not None
    assert hybrid.semantic_indexer is None
    assert hybrid.config.enable_semantic is False
    assert ready is registry_available
    if registry_available:
        assert dispatcher_kwargs["semantic_indexer_registry"] is registry_cls.return_value
    else:
        assert dispatcher_kwargs["semantic_indexer_registry"] is None
