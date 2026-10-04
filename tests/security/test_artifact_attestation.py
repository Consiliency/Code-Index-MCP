"""Tests for artifact attestation — attest + verify_attestation + publisher wiring (SL-3)."""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

import pytest

from mcp_server.artifacts.attestation import (
    Attestation,
    AttestationError,
    attest,
    verify_attestation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_archive(tmp_path: Path, content: bytes = b"fake archive content") -> Path:
    archive = tmp_path / "test-archive.tar.gz"
    archive.write_bytes(content)
    archive.with_suffix(archive.suffix + ".attestation.jsonl").write_text("mock bundle")
    (tmp_path / "bundle.attestation.jsonl").write_text("mock bundle")
    return archive


def _ok_run(*args, **kwargs):
    return CompletedProcess(
        args=args[0],
        returncode=0,
        stdout="bundle: https://github.com/owner/repo/attestations/1\n",
        stderr="",
    )


def _fail_run(*args, **kwargs):
    return CompletedProcess(args=args[0], returncode=1, stdout="", stderr="tampered")


# ---------------------------------------------------------------------------
# Test (a): attest returns Attestation with correct digest
# ---------------------------------------------------------------------------


class TestAttest:
    def test_attest_returns_attestation_with_bundle_url(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_ATTESTATION_MODE", raising=False)
        archive = _make_archive(tmp_path)
        expected_digest = _sha256(archive.read_bytes())

        with patch("mcp_server.artifacts.attestation.subprocess.run", side_effect=_ok_run):
            result = attest(archive, repo="owner/repo")

        assert isinstance(result, Attestation)
        assert result.bundle_url != ""
        assert result.subject_digest == expected_digest

    def test_attest_sidecar_path_is_set(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_ATTESTATION_MODE", raising=False)
        archive = _make_archive(tmp_path)

        with patch("mcp_server.artifacts.attestation.subprocess.run", side_effect=_ok_run):
            result = attest(archive, repo="owner/repo")

        assert result.bundle_path == archive.with_suffix(archive.suffix + ".attestation.jsonl")

    def test_attest_signed_at_is_datetime(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_ATTESTATION_MODE", raising=False)
        archive = _make_archive(tmp_path)

        with patch("mcp_server.artifacts.attestation.subprocess.run", side_effect=_ok_run):
            result = attest(archive, repo="owner/repo")

        assert isinstance(result.signed_at, datetime)


# ---------------------------------------------------------------------------
# Test (b): verify_attestation returns cleanly on success
# ---------------------------------------------------------------------------


class TestVerifyAttestation:
    def test_verify_returns_none_on_success(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_ATTESTATION_MODE", raising=False)
        archive = _make_archive(tmp_path)
        att = Attestation(
            bundle_url="https://github.com/owner/repo/attestations/1",
            bundle_path=tmp_path / "bundle.attestation.jsonl",
            subject_digest=_sha256(archive.read_bytes()),
            signed_at=datetime.now(timezone.utc),
        )

        with patch("mcp_server.artifacts.attestation.subprocess.run", side_effect=_ok_run):
            result = verify_attestation(archive, att, expected_repo="owner/repo")

        assert result is None


# ---------------------------------------------------------------------------
# Test (c): tampered archive under enforce mode raises AttestationError
# ---------------------------------------------------------------------------


class TestVerifyEnforceMode:
    def test_tampered_raises_attestation_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
        archive = _make_archive(tmp_path)
        att = Attestation(
            bundle_url="https://github.com/owner/repo/attestations/1",
            bundle_path=tmp_path / "bundle.attestation.jsonl",
            subject_digest="wrong_digest",
            signed_at=datetime.now(timezone.utc),
        )

        with patch("mcp_server.artifacts.attestation.subprocess.run", side_effect=_fail_run):
            with pytest.raises(AttestationError):
                verify_attestation(archive, att, expected_repo="owner/repo")


# ---------------------------------------------------------------------------
# Test (d): warn mode logs and continues
# ---------------------------------------------------------------------------


class TestVerifyWarnMode:
    def test_warn_mode_logs_and_returns_none(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("MCP_ATTESTATION_MODE", "warn")
        archive = _make_archive(tmp_path)
        att = Attestation(
            bundle_url="https://github.com/owner/repo/attestations/1",
            bundle_path=tmp_path / "bundle.attestation.jsonl",
            subject_digest="wrong_digest",
            signed_at=datetime.now(timezone.utc),
        )

        with patch("mcp_server.artifacts.attestation.subprocess.run", side_effect=_fail_run):
            with caplog.at_level(logging.WARNING, logger="mcp_server.artifacts.attestation"):
                result = verify_attestation(archive, att, expected_repo="owner/repo")

        assert result is None
        assert "tampered" not in caplog.text
        assert "digest" in caplog.text


# ---------------------------------------------------------------------------
# Test (e): skip mode does not call subprocess
# ---------------------------------------------------------------------------


class TestSkipMode:
    def test_skip_mode_no_subprocess(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_ATTESTATION_MODE", "skip")
        archive = _make_archive(tmp_path)
        att = Attestation(
            bundle_url="",
            bundle_path=Path(""),
            subject_digest="",
            signed_at=datetime.now(timezone.utc),
        )

        mock_run = MagicMock()
        with patch("mcp_server.artifacts.attestation.subprocess.run", mock_run):
            result = verify_attestation(archive, att, expected_repo="owner/repo")

        assert mock_run.call_count == 0
        assert result is None

    def test_attest_skip_mode_no_subprocess(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_ATTESTATION_MODE", "skip")
        archive = _make_archive(tmp_path)

        mock_run = MagicMock()
        with patch("mcp_server.artifacts.attestation.subprocess.run", mock_run):
            result = attest(archive, repo="owner/repo")

        assert mock_run.call_count == 0
        assert isinstance(result, Attestation)


# ---------------------------------------------------------------------------
# Test (f): automatic publication cannot substitute an archive-only attestation
# ---------------------------------------------------------------------------


class TestPublishOnReindexAttestationUrl:
    def test_publish_on_reindex_requires_manual_metadata_signing(self, monkeypatch):
        from unittest.mock import MagicMock as _MM

        from mcp_server.artifacts.artifact_upload import IndexArtifactUploader
        from mcp_server.artifacts.publisher import ArtifactError, ArtifactPublisher

        monkeypatch.delenv("MCP_ATTESTATION_MODE", raising=False)

        uploader = IndexArtifactUploader.__new__(IndexArtifactUploader)
        uploader.repo = "owner/repo"
        uploader.token = ""
        uploader.index_files = []

        _synthetic_attestation = Attestation(
            bundle_url="https://github.com/owner/repo/attestations/1",
            bundle_path=Path("/tmp/test-archive.tar.gz.attestation.jsonl"),
            subject_digest="sha256stub",
            signed_at=datetime.now(timezone.utc),
        )

        uploader.compress_indexes = _MM(return_value=(Path("/tmp/test-archive.tar.gz"), "sha256stub", 100))  # type: ignore[method-assign]
        metadata_return = {
            "artifact_type": "full",
            "delta_from": None,
            "checksum": "sha256stub",
            "attestation_url": "https://github.com/owner/repo/attestations/1",
        }
        uploader.create_metadata = _MM(return_value=metadata_return)  # type: ignore[method-assign]
        uploader.upload_direct = _MM()  # type: ignore[method-assign]

        publisher = ArtifactPublisher(uploader, gh_cmd="gh")

        commit = "abcdef1234567890abcdef1234567890abcdef12"

        def gh_side_effect(args, **kwargs):
            if "view" in args and "index-latest" in args:
                return MagicMock(
                    returncode=0, stdout=f'{{"targetCommitish": "{commit}"}}', stderr=""
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=gh_side_effect) as gh:
            with pytest.raises(ArtifactError, match="prepare-only"):
                publisher.publish_on_reindex("owner/repo", commit)
        uploader.compress_indexes.assert_not_called()
        uploader.create_metadata.assert_not_called()
        uploader.upload_direct.assert_not_called()
        gh.assert_not_called()
