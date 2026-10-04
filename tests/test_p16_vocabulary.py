"""P16 SL-1 vocabulary freeze tests — pins IF-0-P16-1..4 contracts."""

import importlib
import inspect
import sys
from dataclasses import fields, is_dataclass

import pytest

# ---------------------------------------------------------------------------
# IF-0-P16-1: Error taxonomy
# ---------------------------------------------------------------------------


def test_error_taxonomy():
    from mcp_server.core.errors import (
        ArtifactError,
        MCPError,
        SchemaMigrationError,
        TerminalArtifactError,
        TransientArtifactError,
    )

    for cls in (TransientArtifactError, TerminalArtifactError, SchemaMigrationError):
        assert issubclass(cls, ArtifactError)
        assert issubclass(cls, MCPError)
        exc = cls("msg", {"key": "val"})
        assert exc.message == "msg"
        assert exc.details == {"key": "val"}


# ---------------------------------------------------------------------------
# IF-0-P16-2: Lazy-read env-var module
# ---------------------------------------------------------------------------


def test_env_vars_lazy_no_import_side_effect(monkeypatch):
    """Module must not call os.getenv at import time."""
    sys.modules.pop("mcp_server.config.env_vars", None)

    calls = []

    def recording_getenv(name, default=None):
        calls.append(name)
        return default

    monkeypatch.setattr("os.getenv", recording_getenv)
    monkeypatch.setenv("MCP_MAX_FILE_SIZE_BYTES", "1")
    monkeypatch.setenv("MCP_ARTIFACT_RETENTION_COUNT", "1")
    monkeypatch.setenv("MCP_ARTIFACT_RETENTION_DAYS", "1")
    monkeypatch.setenv("MCP_DISK_READONLY_THRESHOLD_MB", "1")
    monkeypatch.setenv("MCP_PUBLISH_ROLLBACK_ENABLED", "true")

    import mcp_server.config.env_vars  # noqa: F401

    assert calls == [], f"os.getenv called at import time: {calls}"


def test_env_vars_lazy_getters_read_on_each_call(monkeypatch):
    """Each getter must re-read from env on every call (no caching)."""
    sys.modules.pop("mcp_server.config.env_vars", None)
    import mcp_server.config.env_vars as ev

    monkeypatch.setenv("MCP_MAX_FILE_SIZE_BYTES", "111")
    assert ev.get_max_file_size_bytes() == 111
    monkeypatch.setenv("MCP_MAX_FILE_SIZE_BYTES", "222")
    assert ev.get_max_file_size_bytes() == 222

    monkeypatch.setenv("MCP_ARTIFACT_RETENTION_COUNT", "5")
    assert ev.get_artifact_retention_count() == 5
    monkeypatch.setenv("MCP_ARTIFACT_RETENTION_COUNT", "7")
    assert ev.get_artifact_retention_count() == 7

    monkeypatch.setenv("MCP_ARTIFACT_RETENTION_DAYS", "10")
    assert ev.get_artifact_retention_days() == 10
    monkeypatch.setenv("MCP_ARTIFACT_RETENTION_DAYS", "20")
    assert ev.get_artifact_retention_days() == 20

    monkeypatch.setenv("MCP_DISK_READONLY_THRESHOLD_MB", "50")
    assert ev.get_disk_readonly_threshold_mb() == 50
    monkeypatch.setenv("MCP_DISK_READONLY_THRESHOLD_MB", "75")
    assert ev.get_disk_readonly_threshold_mb() == 75

    monkeypatch.setenv("MCP_PUBLISH_ROLLBACK_ENABLED", "true")
    assert ev.get_publish_rollback_enabled() is True
    monkeypatch.setenv("MCP_PUBLISH_ROLLBACK_ENABLED", "false")
    assert ev.get_publish_rollback_enabled() is False


def test_env_vars_defaults(monkeypatch):
    """Each getter must return the declared default when var is unset."""
    sys.modules.pop("mcp_server.config.env_vars", None)
    import mcp_server.config.env_vars as ev

    monkeypatch.delenv("MCP_MAX_FILE_SIZE_BYTES", raising=False)
    assert ev.get_max_file_size_bytes() == 10 * 1024 * 1024

    monkeypatch.delenv("MCP_ARTIFACT_RETENTION_COUNT", raising=False)
    assert ev.get_artifact_retention_count() == 10

    monkeypatch.delenv("MCP_ARTIFACT_RETENTION_DAYS", raising=False)
    assert ev.get_artifact_retention_days() == 30

    monkeypatch.delenv("MCP_DISK_READONLY_THRESHOLD_MB", raising=False)
    assert ev.get_disk_readonly_threshold_mb() == 100

    monkeypatch.delenv("MCP_PUBLISH_ROLLBACK_ENABLED", raising=False)
    assert ev.get_publish_rollback_enabled() is True


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("", False),
    ],
)
def test_env_vars_boolean_parsing(monkeypatch, value, expected):
    """get_publish_rollback_enabled must parse truthy/falsy strings correctly."""
    sys.modules.pop("mcp_server.config.env_vars", None)
    import mcp_server.config.env_vars as ev

    monkeypatch.setenv("MCP_PUBLISH_ROLLBACK_ENABLED", value)
    assert ev.get_publish_rollback_enabled() is expected


# ---------------------------------------------------------------------------
# IF-0-P16-3: ValidationError dataclass + validate_production_config
# ---------------------------------------------------------------------------


def test_validation_error_dataclass():
    from dataclasses import FrozenInstanceError

    from mcp_server.config.validation import ValidationError

    assert is_dataclass(ValidationError)

    field_names = [f.name for f in fields(ValidationError)]
    assert field_names == ["code", "message", "severity"]

    ve = ValidationError(code="X", message="msg", severity="warn")
    with pytest.raises(FrozenInstanceError):
        ve.code = "Y"  # type: ignore[misc]

    ve2 = ValidationError(code="A", message="b", severity="fatal")
    assert ve2.severity == "fatal"


def test_validate_production_config_signature(monkeypatch):
    import types

    from mcp_server.config import validate_production_config
    from mcp_server.config.validation import ValidationError

    # Use a minimal real-shaped config with a strong JWT secret and no CORS wildcard.
    strong_secret = "a" * 40  # 40 hex chars — not in weak-credential blocklist
    cfg = types.SimpleNamespace(
        jwt_secret_key=strong_secret, cors_origins=[], rate_limit_requests=100
    )

    # Ensure DEFAULT_ADMIN_PASSWORD is strong (required in production env check)
    monkeypatch.setenv("DEFAULT_ADMIN_PASSWORD", "Strong-Admin-Password-xyz!")

    result = validate_production_config(cfg, environment="production")
    assert isinstance(result, list)
    assert all(isinstance(e, ValidationError) for e in result)

    result2 = validate_production_config(cfg, environment="dev")
    assert isinstance(result2, list)

    sig = inspect.signature(validate_production_config)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == ["config", "environment"]
    assert params[1].kind == inspect.Parameter.KEYWORD_ONLY
    assert params[1].default == "production"


def test_validate_production_config_reexported():
    import mcp_server.config as config_mod
    from mcp_server.config import ValidationError, validate_production_config  # noqa: F401

    assert "ValidationError" in config_mod.__all__
    assert "validate_production_config" in config_mod.__all__


# ---------------------------------------------------------------------------
# IF-0-P16-4: reset_process_singletons
# ---------------------------------------------------------------------------

SINGLETON_TABLE = [
    ("mcp_server.gateway", "_repo_registry"),
    ("mcp_server.plugin_system.loader", "_loader"),
    ("mcp_server.plugin_system.discovery", "_discovery"),
    ("mcp_server.plugin_system.config", "_config_manager"),
    ("mcp_server.plugins.memory_aware_manager", "_manager_instance"),
]


@pytest.mark.parametrize("module_path,attr_name", SINGLETON_TABLE)
def test_reset_process_singletons_nulls_all(module_path, attr_name, monkeypatch):
    """reset_process_singletons must null each registered singleton attr."""
    from mcp_server.cli.bootstrap import reset_process_singletons

    mod = importlib.import_module(module_path)
    monkeypatch.setattr(mod, attr_name, object())

    reset_process_singletons()

    assert getattr(mod, attr_name) is None


def test_reset_process_singletons_leaves_lock_alone():
    """reset_process_singletons must NOT touch _manager_lock."""
    import mcp_server.plugins.memory_aware_manager as mam_mod
    from mcp_server.cli.bootstrap import reset_process_singletons

    original_lock = getattr(mam_mod, "_manager_lock", None)
    sentinel = object()
    mam_mod._manager_lock = sentinel  # type: ignore[attr-defined]

    reset_process_singletons()

    assert mam_mod._manager_lock is sentinel
    mam_mod._manager_lock = original_lock  # type: ignore[attr-defined]


def test_reset_process_singletons_tolerates_missing_module(monkeypatch):
    """reset_process_singletons must not raise when a module is absent (pruned install)."""
    from mcp_server.cli.bootstrap import reset_process_singletons

    monkeypatch.setitem(sys.modules, "mcp_server.metrics.prometheus_exporter", None)

    reset_process_singletons()  # must not raise
