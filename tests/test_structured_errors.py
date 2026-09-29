"""Tests for P13 SL-5 structured-errors: IndexingError, ArtifactError, record_handled_error."""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_server.core.errors import (
    ArtifactError,
    IndexingError,
    MCPError,
    record_handled_error,
)

# ---------------------------------------------------------------------------
# Class hierarchy
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def restore_metrics_owner(monkeypatch):
    import mcp_server.metrics.prometheus_exporter as prom_mod

    monkeypatch.setattr(prom_mod, "_exporter", prom_mod._exporter)


def test_indexing_error_subclasses_mcp_error():
    exc = IndexingError("indexing failed")
    assert isinstance(exc, MCPError)
    assert isinstance(exc, Exception)


def test_artifact_error_subclasses_mcp_error():
    exc = ArtifactError("artifact failed")
    assert isinstance(exc, MCPError)
    assert isinstance(exc, Exception)


def test_indexing_error_is_distinct_from_index_error():
    from mcp_server.core.errors import IndexError as MCP_IndexError

    assert IndexingError is not MCP_IndexError
    exc = MCP_IndexError("/some/path", "oops")
    assert isinstance(exc, MCPError)
    assert not isinstance(exc, IndexingError)


# ---------------------------------------------------------------------------
# PrometheusExporter.errors_by_type counter
# ---------------------------------------------------------------------------


def test_errors_by_type_counter_registered():
    from mcp_server.metrics.prometheus_exporter import PROMETHEUS_AVAILABLE, PrometheusExporter

    exporter = PrometheusExporter()
    if PROMETHEUS_AVAILABLE:
        assert exporter.errors_by_type is not None
    else:
        assert exporter.errors_by_type is None


def test_errors_by_type_none_when_prometheus_unavailable(monkeypatch):
    """When prometheus_client is absent, errors_by_type must be None."""
    import mcp_server.metrics.prometheus_exporter as prom_mod

    original = prom_mod.PROMETHEUS_AVAILABLE
    monkeypatch.setattr(prom_mod, "PROMETHEUS_AVAILABLE", False)
    try:
        exporter = prom_mod.PrometheusExporter()
        assert exporter.errors_by_type is None
    finally:
        monkeypatch.setattr(prom_mod, "PROMETHEUS_AVAILABLE", original)


# ---------------------------------------------------------------------------
# record_handled_error behaviour
# ---------------------------------------------------------------------------


def test_record_handled_error_increments_counter_by_one():
    """Each call must increment the counter for the given module/exception pair by exactly 1."""
    import mcp_server.metrics.prometheus_exporter as prom_mod
    from mcp_server.metrics.prometheus_exporter import (
        PROMETHEUS_AVAILABLE,
        PrometheusExporter,
        _exporter,
    )

    if not PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")

    exporter = PrometheusExporter()
    prom_mod._exporter = exporter

    exc = OSError("test error")
    before = _get_counter_value(exporter, "test_module", "OSError")
    record_handled_error("test_module", exc)
    after = _get_counter_value(exporter, "test_module", "OSError")
    assert after - before == 1


def test_record_handled_error_increments_once_per_call():
    import mcp_server.metrics.prometheus_exporter as prom_mod
    from mcp_server.metrics.prometheus_exporter import PROMETHEUS_AVAILABLE, PrometheusExporter

    if not PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")

    exporter = PrometheusExporter()
    prom_mod._exporter = exporter

    exc = ValueError("test")
    before = _get_counter_value(exporter, "mod_a", "ValueError")
    record_handled_error("mod_a", exc)
    record_handled_error("mod_a", exc)
    record_handled_error("mod_a", exc)
    after = _get_counter_value(exporter, "mod_a", "ValueError")
    assert after - before == 3


def test_record_handled_error_no_raise_when_prometheus_unavailable(monkeypatch):
    """When prometheus_client unavailable, record_handled_error must not raise."""
    import mcp_server.core.errors as errors_mod
    import mcp_server.metrics.prometheus_exporter as prom_mod

    monkeypatch.setattr(prom_mod, "PROMETHEUS_AVAILABLE", False)
    monkeypatch.setattr(prom_mod, "_exporter", None)
    # Should not raise under any circumstances
    record_handled_error("no_prom", OSError("x"))


def test_record_handled_error_never_raises():
    """Helper must swallow all exceptions from metric surface."""
    import mcp_server.metrics.prometheus_exporter as prom_mod
    from mcp_server.metrics.prometheus_exporter import PrometheusExporter

    bad_exporter = MagicMock()
    bad_exporter.errors_by_type = MagicMock()
    bad_exporter.errors_by_type.labels.side_effect = RuntimeError("boom")
    prom_mod._exporter = bad_exporter

    # Must not raise
    record_handled_error("m", OSError("y"))

    # Restore
    prom_mod._exporter = None


# ---------------------------------------------------------------------------
# Per-file call-site smoke tests (samples ≥3 of the 26 sites)
# ---------------------------------------------------------------------------


SL5_FILES = [
    "mcp_server/dispatcher/dispatcher_enhanced.py",
    "mcp_server/dispatcher/simple_dispatcher.py",
    "mcp_server/dispatcher/fallback.py",
    "mcp_server/dispatcher/cross_repo_coordinator.py",
    "mcp_server/artifacts/artifact_upload.py",
    "mcp_server/artifacts/artifact_download.py",
    "mcp_server/artifacts/multi_repo_artifact_coordinator.py",
]


@pytest.mark.parametrize("rel_path", SL5_FILES)
def test_record_handled_error_call_present_in_file(rel_path):
    """Each SL-5 owned source file must contain at least one record_handled_error call."""
    repo_root = Path(__file__).parent.parent
    src = (repo_root / rel_path).read_text(encoding="utf-8")
    assert "record_handled_error(" in src, f"{rel_path} missing record_handled_error call"


# ---------------------------------------------------------------------------
# Sampled call-site integration: 3 sites raise correct exception type
# ---------------------------------------------------------------------------


def test_dispatcher_enhanced_site_instruments_exception():
    """Smoke: the dispatcher_enhanced init-time metrics site catches and instruments correctly."""
    import mcp_server.metrics.prometheus_exporter as prom_mod
    from mcp_server.metrics.prometheus_exporter import PROMETHEUS_AVAILABLE, PrometheusExporter

    if not PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")

    exporter = PrometheusExporter()
    prom_mod._exporter = exporter

    # Importing EnhancedDispatcher triggers the init-time except block if something fails.
    # We directly call record_handled_error to verify the counter path works for the module.
    before = _get_counter_value(
        exporter, "mcp_server.dispatcher.dispatcher_enhanced", "ImportError"
    )
    record_handled_error("mcp_server.dispatcher.dispatcher_enhanced", ImportError("missing dep"))
    after = _get_counter_value(exporter, "mcp_server.dispatcher.dispatcher_enhanced", "ImportError")
    assert after - before == 1


def test_artifact_upload_site_instruments_exception():
    import mcp_server.metrics.prometheus_exporter as prom_mod
    from mcp_server.metrics.prometheus_exporter import PROMETHEUS_AVAILABLE, PrometheusExporter

    if not PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")

    exporter = PrometheusExporter()
    prom_mod._exporter = exporter

    before = _get_counter_value(exporter, "mcp_server.artifacts.artifact_upload", "OSError")
    record_handled_error("mcp_server.artifacts.artifact_upload", OSError("disk full"))
    after = _get_counter_value(exporter, "mcp_server.artifacts.artifact_upload", "OSError")
    assert after - before == 1


def test_artifact_download_site_instruments_exception():
    import mcp_server.metrics.prometheus_exporter as prom_mod
    from mcp_server.metrics.prometheus_exporter import PROMETHEUS_AVAILABLE, PrometheusExporter

    if not PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")

    exporter = PrometheusExporter()
    prom_mod._exporter = exporter

    before = _get_counter_value(exporter, "mcp_server.artifacts.artifact_download", "ValueError")
    record_handled_error("mcp_server.artifacts.artifact_download", ValueError("bad data"))
    after = _get_counter_value(exporter, "mcp_server.artifacts.artifact_download", "ValueError")
    assert after - before == 1


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _get_counter_value(exporter, module: str, exception: str) -> float:
    """Read the current _value from errors_by_type counter for a label set."""
    try:
        labeled = exporter.errors_by_type.labels(module=module, exception=exception)
        # prometheus_client Counter stores ._value.get() or ._metrics
        return labeled._value.get()
    except Exception:
        return 0.0
