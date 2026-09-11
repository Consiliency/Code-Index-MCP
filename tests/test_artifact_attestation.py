"""Tests for artifact attestation — attest() and verify_attestation() (SL-2.2)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_server.artifacts.attestation import (
    Attestation,
    AttestationError,
    attest,
    verify_attestation,
)

FAKE_ARCHIVE = Path("/tmp/fake_archive.tar.gz")
REPO = "owner/repo"


def test_attest_never_signs_locally_or_displays_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
    archive = tmp_path / "prepared.tar.gz"
    archive.write_bytes(b"unchanged prepared bytes")
    with patch("subprocess.run") as run, pytest.raises(AttestationError):
        attest(archive, repo=REPO)
    run.assert_not_called()
    assert archive.read_bytes() == b"unchanged prepared bytes"


def test_verify_binds_trusted_workflow_ref_and_predicate(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
    monkeypatch.delenv("MCP_ATTESTATION_SOURCE_REF", raising=False)
    monkeypatch.setenv("MCP_ATTESTATION_SIGNER_DIGEST", "a" * 40)
    bundle = tmp_path / "bundle.jsonl"
    bundle.write_text("{}")
    att = Attestation("", bundle, "", datetime.now(timezone.utc))
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as run:
        verify_attestation(FAKE_ARCHIVE, att, expected_repo=REPO)
    args = run.call_args.args[0]
    assert args[args.index("--signer-workflow") + 1] == (
        "owner/repo/.github/workflows/sign-published-image.yml"
    )
    assert args[args.index("--source-ref") + 1] == "refs/heads/main"
    assert args[args.index("--signer-digest") + 1] == "a" * 40
    assert args[args.index("--predicate-type") + 1].endswith("local-index-digest/v1")
    assert run.call_args.kwargs["timeout"] <= 30


def test_unknown_attestation_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "typo")
    with patch("subprocess.run") as run, pytest.raises(AttestationError):
        verify_attestation(FAKE_ARCHIVE, None, expected_repo=REPO)
    run.assert_not_called()


def test_verifier_failure_diagnostics_do_not_echo_cli_payload(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
    bundle = tmp_path / "bundle.jsonl"
    bundle.write_text("{}")
    sentinel = "private-provider-payload-sentinel"
    att = Attestation("", bundle, "", datetime.now(timezone.utc))
    with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr=sentinel)):
        with pytest.raises(AttestationError) as error:
            verify_attestation(FAKE_ARCHIVE, att, expected_repo=REPO)
    assert sentinel not in str(error.value)
    assert sentinel not in caplog.text


@pytest.fixture(autouse=True)
def _stub_sha256(monkeypatch, tmp_path):
    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(b"synthetic archive")
    monkeypatch.setattr(__name__ + ".FAKE_ARCHIVE", archive)
    monkeypatch.setattr("mcp_server.artifacts.attestation._sha256_of", lambda p: "sha256stub")


class TestAttestSkipMode:
    def test_skip_mode_returns_empty_attestation(self, monkeypatch):
        monkeypatch.setenv("MCP_ATTESTATION_MODE", "skip")
        result = attest(FAKE_ARCHIVE, repo=REPO)
        assert result.bundle_url == ""
        assert result.subject_digest == ""

    def test_skip_mode_no_subprocess(self, monkeypatch):
        monkeypatch.setenv("MCP_ATTESTATION_MODE", "skip")
        with patch("subprocess.run") as mock_run:
            attest(FAKE_ARCHIVE, repo=REPO)
        mock_run.assert_not_called()


class TestAttestEnforceMode:
    def test_enforce_mode_raises_on_bundle_verification_failure(self, monkeypatch):
        monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
        FAKE_ARCHIVE.with_suffix(".gz.attestation.jsonl").write_text("{}")

        def fake_run(args, **kwargs):
            assert args[:3] == ["gh", "attestation", "verify"]
            return MagicMock(returncode=1, stdout="", stderr="verification failed")

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(AttestationError):
                attest(FAKE_ARCHIVE, repo=REPO)

    def test_enforce_mode_returns_attestation_on_success(self, monkeypatch):
        monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
        FAKE_ARCHIVE.with_suffix(".gz.attestation.jsonl").write_text("{}")

        def fake_run(args, **kwargs):
            assert args[:3] == ["gh", "attestation", "verify"]
            return MagicMock(
                returncode=0,
                stdout="https://github.com/owner/repo/attestations/abc123",
                stderr="",
            )

        with patch("subprocess.run", side_effect=fake_run):
            result = attest(FAKE_ARCHIVE, repo=REPO)

        assert "github.com" in result.bundle_url
        assert result.subject_digest == "sha256stub"


class TestAttestWarnMode:
    def test_warn_mode_reports_missing_bundle(self, monkeypatch, caplog):
        monkeypatch.setenv("MCP_ATTESTATION_MODE", "warn")
        import logging

        with patch("subprocess.run") as run:
            with caplog.at_level(logging.WARNING, logger="mcp_server.artifacts.attestation"):
                result = attest(FAKE_ARCHIVE, repo=REPO)

        run.assert_not_called()
        assert result.bundle_path is None
        assert result.bundle_url == ""
        assert "ATTESTATION_PREREQ" in caplog.text


class TestVerifyAttestation:
    def test_skip_mode_no_verify(self, monkeypatch):
        monkeypatch.setenv("MCP_ATTESTATION_MODE", "skip")
        att = Attestation(
            bundle_url="",
            bundle_path=Path("/tmp/fake.jsonl"),
            subject_digest="sha256stub",
            signed_at=datetime.now(timezone.utc),
        )
        with patch("subprocess.run") as mock_run:
            verify_attestation(FAKE_ARCHIVE, att, expected_repo=REPO)
        mock_run.assert_not_called()

    def test_enforce_mode_raises_on_verify_failure(self, monkeypatch):
        monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
        att = Attestation(
            bundle_url="https://github.com/owner/repo/attestations/1",
            bundle_path=Path("/tmp/fake.jsonl"),
            subject_digest="sha256stub",
            signed_at=datetime.now(timezone.utc),
        )
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="verify failed"),
        ):
            with pytest.raises(AttestationError):
                verify_attestation(FAKE_ARCHIVE, att, expected_repo=REPO)

    def test_warn_mode_logs_on_verify_failure(self, monkeypatch, caplog):
        monkeypatch.setenv("MCP_ATTESTATION_MODE", "warn")
        import logging

        att = Attestation(
            bundle_url="https://github.com/owner/repo/attestations/1",
            bundle_path=Path("/tmp/fake.jsonl"),
            subject_digest="sha256stub",
            signed_at=datetime.now(timezone.utc),
        )
        with patch(
            "subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="fail")
        ):
            with caplog.at_level(logging.WARNING, logger="mcp_server.artifacts.attestation"):
                verify_attestation(FAKE_ARCHIVE, att, expected_repo=REPO)

        assert any(r.levelno >= logging.WARNING for r in caplog.records)
