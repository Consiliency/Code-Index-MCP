"""Local env file loading in mcp_server.config.environment.

Regression coverage for Consiliency/Code-Index-MCP#141: the first get_env_var()
call loaded the tracked .env.native into os.environ for the rest of the test
process.
"""

import os
from unittest.mock import patch

import pytest

from mcp_server.config import environment

SENTINEL = "MCP_DOTENV_REGRESSION_SENTINEL"


@pytest.fixture
def fake_checkout(tmp_path, monkeypatch):
    """A stand-in checkout root holding a .env.native, with the load not yet done."""
    (tmp_path / ".env.native").write_text(f"{SENTINEL}=from-dotenv\n")
    module_path = tmp_path / "mcp_server" / "config" / "environment.py"
    # load_local_env_files() derives the checkout root from the module's own path.
    monkeypatch.setattr(environment, "__file__", str(module_path))
    monkeypatch.setattr(environment, "_DOTENV_LOADED", False)
    # Restore os.environ exactly, including keys a load adds.
    with patch.dict(os.environ):
        os.environ.pop(SENTINEL, None)
        yield tmp_path


def test_test_process_does_not_load_checkout_env_files(monkeypatch):
    """tests/conftest.py keeps the real checkout's env files out of os.environ."""
    monkeypatch.setattr(environment, "_DOTENV_LOADED", False)
    before = dict(os.environ)

    environment.get_env_var("MCP_ENABLE_MULTI_REPO")

    assert os.environ["PYTHON_DOTENV_DISABLED"] == "1"
    assert dict(os.environ) == before
    assert environment._DOTENV_LOADED is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "t", "yes", "Y", " 1 "])
def test_get_env_var_leaves_environ_unchanged_when_dotenv_disabled(
    fake_checkout, monkeypatch, value
):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", value)
    before = dict(os.environ)

    assert environment.get_env_var(SENTINEL, "fallback") == "fallback"

    assert dict(os.environ) == before
    # Not latched: a later call with the switch cleared may still load.
    assert environment._DOTENV_LOADED is False


@pytest.mark.parametrize("value", [None, "", "0", "false", "no"])
def test_get_env_var_loads_checkout_env_files_when_not_disabled(fake_checkout, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    else:
        monkeypatch.setenv("PYTHON_DOTENV_DISABLED", value)

    assert environment.get_env_var(SENTINEL, "fallback") == "from-dotenv"

    assert os.environ[SENTINEL] == "from-dotenv"
    assert environment._DOTENV_LOADED is True


def test_disabled_load_can_be_enabled_later_in_the_process(fake_checkout, monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    assert environment.get_env_var(SENTINEL) is None

    monkeypatch.delenv("PYTHON_DOTENV_DISABLED")

    assert environment.get_env_var(SENTINEL) == "from-dotenv"


def test_process_environment_wins_over_checkout_env_files(fake_checkout, monkeypatch):
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.setenv(SENTINEL, "from-process")

    assert environment.get_env_var(SENTINEL) == "from-process"
