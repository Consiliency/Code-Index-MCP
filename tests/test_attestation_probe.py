"""SL-5 tests — probe_gh_attestation_support + boot warning wiring."""

from __future__ import annotations

import logging
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from mcp_server.artifacts.attestation import probe_gh_attestation_support


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """Reset lru_cache between tests so mocked subprocess doesn't leak."""
    probe_gh_attestation_support.cache_clear()
    yield
    probe_gh_attestation_support.cache_clear()


def test_probe_returns_true_when_gh_available():
    """probe returns True when gh attestation --help exits 0."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        result = probe_gh_attestation_support()
        assert result is True
        mock_run.assert_called_once_with(
            ["gh", "attestation", "--help"],
            capture_output=True,
            timeout=5,
        )


def test_probe_returns_false_when_gh_unavailable():
    """probe returns False when gh exits non-zero."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    with patch("subprocess.run", return_value=mock_result):
        result = probe_gh_attestation_support()
        assert result is False


def test_probe_returns_false_on_exception():
    """probe returns False when subprocess raises (gh not installed)."""
    with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
        result = probe_gh_attestation_support()
        assert result is False


def test_probe_returns_false_on_timeout():
    """probe returns False when subprocess times out."""
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=5)):
        result = probe_gh_attestation_support()
        assert result is False


def test_probe_is_cached():
    """probe is called only once per process (cached)."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        r1 = probe_gh_attestation_support()
        r2 = probe_gh_attestation_support()
        assert r1 is True
        assert r2 is True
        assert mock_run.call_count == 1, "subprocess.run should only be called once (cached)"


def test_boot_warn_when_probe_false_and_enforce_mode(caplog, monkeypatch):
    """warn_if_gh_attestation_missing logs WARNING when probe=False + MCP_ATTESTATION_MODE=enforce."""
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
    mock_result = MagicMock()
    mock_result.returncode = 1
    with patch("subprocess.run", return_value=mock_result):
        from mcp_server.artifacts.attestation import warn_if_gh_attestation_missing

        with caplog.at_level(logging.WARNING, logger="mcp_server.artifacts.attestation"):
            warn_if_gh_attestation_missing()
        assert any(
            "ATTESTATION_PREREQ" in r.message for r in caplog.records
        ), "Expected ATTESTATION_PREREQ warning when probe=False and enforce mode"


def test_no_boot_warn_when_probe_true(caplog, monkeypatch):
    """warn_if_gh_attestation_missing does not log when probe=True."""
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
    mock_result = MagicMock()
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result):
        from mcp_server.artifacts.attestation import warn_if_gh_attestation_missing

        with caplog.at_level(logging.WARNING, logger="mcp_server.artifacts.attestation"):
            warn_if_gh_attestation_missing()
        assert not any("ATTESTATION_PREREQ" in r.message for r in caplog.records)


def test_no_boot_warn_when_not_enforce_mode(caplog, monkeypatch):
    """warn_if_gh_attestation_missing does not warn when mode != enforce."""
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "warn")
    mock_result = MagicMock()
    mock_result.returncode = 1
    with patch("subprocess.run", return_value=mock_result):
        from mcp_server.artifacts.attestation import warn_if_gh_attestation_missing

        with caplog.at_level(logging.WARNING, logger="mcp_server.artifacts.attestation"):
            warn_if_gh_attestation_missing()
        assert not any("ATTESTATION_PREREQ" in r.message for r in caplog.records)
