"""Tests for reset_process_singletons() extension and initialize_stateless_services wiring."""

from pathlib import Path
from unittest.mock import sentinel

import pytest


@pytest.mark.parametrize(
    "module_name,attr_name",
    [
        ("mcp_server.storage.multi_repo_manager", "_manager_instance"),
        ("mcp_server.dispatcher.cross_repo_coordinator", "_coordinator_instance"),
        ("mcp_server.plugins.repository_plugin_loader", "_loader_instance"),
    ],
)
def test_reset_nulls_extended_singletons(module_name: str, attr_name: str):
    """reset_process_singletons() must null the three new P17 singletons."""
    import importlib

    from mcp_server.cli.bootstrap import reset_process_singletons

    mod = importlib.import_module(module_name)
    setattr(mod, attr_name, sentinel.live_object)

    reset_process_singletons()

    assert (
        getattr(mod, attr_name) is None
    ), f"{module_name}.{attr_name} was not set to None by reset_process_singletons()"


def test_initialize_stateless_services_calls_reset(tmp_path: Path, monkeypatch):
    """Reset configuration singletons without abandoning the process metrics owner."""
    import importlib

    import mcp_server.dispatcher.cross_repo_coordinator as crc
    import mcp_server.gateway as gw
    import mcp_server.metrics.prometheus_exporter as pe
    import mcp_server.plugin_system.config as psc
    import mcp_server.plugin_system.discovery as psd
    import mcp_server.plugin_system.loader as psl
    import mcp_server.plugins.memory_aware_manager as mam
    import mcp_server.plugins.repository_plugin_loader as rpl
    import mcp_server.storage.multi_repo_manager as mrm

    metrics_owner = pe.get_prometheus_exporter()
    sentinels = [
        (mrm, "_manager_instance"),
        (crc, "_coordinator_instance"),
        (rpl, "_loader_instance"),
        (gw, "_repo_registry"),
        (psl, "_loader"),
        (psd, "_discovery"),
        (psc, "_config_manager"),
        (mam, "_manager_instance"),
    ]
    for mod, attr in sentinels:
        monkeypatch.setattr(mod, attr, sentinel.live)

    from mcp_server.cli.bootstrap import initialize_stateless_services

    result = initialize_stateless_services(registry_path=tmp_path / "r.json")

    assert result is not None
    assert len(result) == 5
    assert pe.get_prometheus_exporter() is metrics_owner

    for mod, attr in sentinels:
        val = getattr(mod, attr)
        assert (
            val is not sentinel.live
        ), f"{mod.__name__}.{attr} still has sentinel — reset not called or not covering this attr"


def test_initialize_stateless_services_idempotent_under_repeat_init(tmp_path: Path):
    """Calling initialize_stateless_services() twice must return distinct RepositoryRegistry instances."""
    from mcp_server.cli.bootstrap import initialize_stateless_services

    _, _, _, registry1, _ = initialize_stateless_services(registry_path=tmp_path / "r.json")
    _, _, _, registry2, _ = initialize_stateless_services(registry_path=tmp_path / "r.json")

    assert (
        registry1 is not registry2
    ), "Both calls returned the same RepositoryRegistry — stale singleton not cleared"
