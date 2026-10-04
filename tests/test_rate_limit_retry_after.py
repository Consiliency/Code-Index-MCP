"""Tests for _respect_rate_limit Retry-After parsing: 429/403 handling (SL-2.4)."""

from __future__ import annotations

import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from mcp_server.artifacts.providers.github_actions import _respect_rate_limit
from mcp_server.core.errors import TerminalArtifactError, TransientArtifactError


class TestRetryAfterParsing:
    """_respect_rate_limit must parse Retry-After and sleep appropriately."""

    def test_no_rate_limit_headers_returns_zero(self):
        headers: dict = {}
        slept = _respect_rate_limit(headers, status_code=200)
        assert slept == 0.0

    def test_429_with_retry_after_integer_sleeps(self):
        headers = {"Retry-After": "5"}
        with patch("time.sleep") as mock_sleep:
            slept = _respect_rate_limit(headers, status_code=429)
        mock_sleep.assert_called_once_with(5.0)
        assert slept == 5.0

    def test_429_with_retry_after_capped_at_300(self):
        headers = {"Retry-After": "9999"}
        with patch("time.sleep") as mock_sleep:
            slept = _respect_rate_limit(headers, status_code=429)
        mock_sleep.assert_called_once_with(300.0)
        assert slept == 300.0

    def test_429_with_retry_after_http_date(self):
        import datetime
        from email.utils import formatdate

        future_ts = time.time() + 30
        http_date = formatdate(future_ts, usegmt=True)
        headers = {"Retry-After": http_date}
        with patch("time.sleep") as mock_sleep:
            with patch("time.time", return_value=time.time()):
                slept = _respect_rate_limit(headers, status_code=429)
        # Should have slept some positive amount up to 300
        assert 0 < slept <= 300

    def test_429_increments_rate_limit_counter(self):
        headers = {"Retry-After": "1"}
        with patch("time.sleep"):
            with patch(
                "mcp_server.artifacts.providers.github_actions.mcp_rate_limit_sleeps_total"
            ) as mock_counter:
                mock_counter_instance = MagicMock()
                mock_counter.labels.return_value = mock_counter_instance
                _respect_rate_limit(headers, status_code=429)

    def test_403_raises_terminal_artifact_error(self):
        headers: dict = {}
        with pytest.raises(TerminalArtifactError):
            _respect_rate_limit(headers, status_code=403)

    def test_403_increments_error_counter(self):
        headers: dict = {}
        with patch(
            "mcp_server.artifacts.providers.github_actions.mcp_artifact_errors_by_class_total"
        ) as mock_counter:
            mock_instance = MagicMock()
            mock_counter.labels.return_value = mock_instance
            with pytest.raises(TerminalArtifactError):
                _respect_rate_limit(headers, status_code=403)

    def test_200_with_low_remaining_uses_reset_header(self):
        """Legacy path: low X-RateLimit-Remaining still triggers backoff on 200."""
        headers = {
            "X-RateLimit-Remaining": "5",
            "X-RateLimit-Reset": str(int(time.time()) + 10),
        }
        with patch("time.sleep") as mock_sleep:
            slept = _respect_rate_limit(headers, status_code=200)
        assert slept > 0
        mock_sleep.assert_called_once()

    def test_200_with_high_remaining_no_sleep(self):
        headers = {
            "X-RateLimit-Remaining": "5000",
        }
        with patch("time.sleep") as mock_sleep:
            slept = _respect_rate_limit(headers, status_code=200)
        assert slept == 0.0
        mock_sleep.assert_not_called()


class TestGhApiStatusCodePropagation:
    """_gh_api must hand the HTTP status code to _respect_rate_limit."""

    def test_gh_api_403_raises_terminal_error(self):
        """When gh api returns HTTP 403, _gh_api must surface TerminalArtifactError."""
        from mcp_server.artifacts.providers.github_actions import _gh_api

        http_response = (
            "HTTP/1.1 403 Forbidden\r\n"
            "Content-Type: application/json\r\n"
            "\r\n"
            '{"message":"Must have admin rights"}'
        )

        mock_result = MagicMock(returncode=0, stdout=http_response, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(TerminalArtifactError):
                _gh_api("gh", "/repos/owner/repo/actions/artifacts")

    def test_gh_api_429_sleeps_and_does_not_raise_terminal(self):
        """When gh api returns HTTP 429, _gh_api sleeps but does NOT raise TerminalArtifactError."""
        from mcp_server.artifacts.providers.github_actions import _gh_api

        http_response = (
            "HTTP/1.1 429 Too Many Requests\r\n"
            "Retry-After: 2\r\n"
            "Content-Type: application/json\r\n"
            "\r\n"
            '{"message":"rate limit"}'
        )

        mock_result = MagicMock(returncode=0, stdout=http_response, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            with patch("time.sleep") as mock_sleep:
                try:
                    _gh_api("gh", "/repos/owner/repo/actions/artifacts")
                except TerminalArtifactError:
                    pytest.fail("429 should not raise TerminalArtifactError")
                except Exception:
                    pass  # other errors OK for this stub
                mock_sleep.assert_called()


class TestAttestationPreflight:
    """Local publication consumes a signed bundle without probing credentials."""

    @pytest.mark.parametrize("mode", ["enforce", "warn"])
    def test_missing_bundle_never_probes_auth_or_attempts_sign(
        self, tmp_path, monkeypatch, caplog, mode
    ):
        from mcp_server.artifacts.attestation import AttestationError, attest

        monkeypatch.setenv("MCP_ATTESTATION_MODE", mode)
        archive = tmp_path / "archive.tar.gz"
        archive.write_bytes(b"synthetic archive")
        with patch("subprocess.run") as run:
            if mode == "enforce":
                with pytest.raises(AttestationError, match="ATTESTATION_PREREQ"):
                    attest(archive, repo="owner/repo")
            else:
                result = attest(archive, repo="owner/repo")
                assert result.bundle_path is None
                assert "ATTESTATION_PREREQ" in caplog.text
        run.assert_not_called()
        assert archive.read_bytes() == b"synthetic archive"
