"""Tests for DeltaPolicy integration in ArtifactPublisher and IndexArtifactUploader."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_server.artifacts.publisher import ArtifactPublisher, ArtifactRef


def _make_uploader(repo: str = "owner/repo") -> MagicMock:
    uploader = MagicMock()
    uploader.repo = repo
    uploader.compress_indexes.return_value = (Path("/tmp/archive.tar.gz"), "sha256abc", 200)
    uploader.create_metadata.return_value = {
        "artifact_type": "full",
        "delta_from": None,
        "checksum": "sha256abc",
    }
    return uploader


def _make_publisher(uploader: MagicMock, *, gh_cmd: str = "gh") -> ArtifactPublisher:
    return ArtifactPublisher(uploader, gh_cmd=gh_cmd)


class TestPublisherDeltaIntegration:
    """Publisher calls DeltaPolicy and wires result into create_metadata."""

    def _mock_gh(self, publisher: ArtifactPublisher, *, prev_commit: str | None = None) -> None:
        """Patch publisher._run and subprocess.run to avoid real gh calls."""

        def fake_run(args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            # view index-latest: return prev_commit if given
            if "release" in args and "view" in args and "index-latest" in args:
                if prev_commit is None:
                    result.returncode = 1
                else:
                    result.returncode = 0
                    result.stdout = json.dumps({"targetCommitish": prev_commit})
            return result

        import subprocess as _sp

        patcher = patch.object(_sp, "run", side_effect=fake_run)
        patcher.start()
        publisher._patcher = patcher  # type: ignore[attr-defined]
        publisher._run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))

        attest_patcher = patch(
            "mcp_server.artifacts.publisher._attestation_mode",
            return_value="skip",
        )
        attest_patcher.start()
        publisher._attest_patcher = attest_patcher  # type: ignore[attr-defined]

    def test_publisher_switches_to_delta_when_env_low(self, monkeypatch):
        """When MCP_ARTIFACT_FULL_SIZE_LIMIT=1 and prev artifact exists, metadata must be delta."""
        monkeypatch.setenv("MCP_ARTIFACT_FULL_SIZE_LIMIT", "1")

        prev_artifact_id = "deadbeef"
        uploader = _make_uploader()
        uploader.compress_indexes.return_value = (Path("/tmp/archive.tar.gz"), "sha256abc", 200)

        publisher = _make_publisher(uploader)
        self._mock_gh(publisher, prev_commit=prev_artifact_id)

        ref = publisher.publish_on_reindex(repo_id="owner/repo", commit="abc1234567890")

        # create_metadata must have been called with delta strategy
        uploader.create_metadata.assert_called_once()
        call_kwargs = uploader.create_metadata.call_args
        assert call_kwargs.kwargs.get("artifact_type") == "delta"
        assert call_kwargs.kwargs.get("delta_from") == prev_artifact_id

        if hasattr(publisher, "_patcher"):
            publisher._patcher.stop()
        if hasattr(publisher, "_attest_patcher"):
            publisher._attest_patcher.stop()

    def test_publisher_full_when_no_previous(self, monkeypatch):
        """When no previous artifact, strategy must be full regardless of size."""
        monkeypatch.setenv("MCP_ARTIFACT_FULL_SIZE_LIMIT", "1")

        uploader = _make_uploader()
        publisher = _make_publisher(uploader)
        self._mock_gh(publisher, prev_commit=None)

        publisher.publish_on_reindex(repo_id="owner/repo", commit="abc1234567890")

        call_kwargs = uploader.create_metadata.call_args
        assert call_kwargs.kwargs.get("artifact_type") == "full"
        assert call_kwargs.kwargs.get("delta_from") is None

        if hasattr(publisher, "_patcher"):
            publisher._patcher.stop()
        if hasattr(publisher, "_attest_patcher"):
            publisher._attest_patcher.stop()
