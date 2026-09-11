"""Failed publication preserves prepared bytes and remote diagnostic evidence."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from mcp_server.artifacts.artifact_upload import IndexArtifactUploader, ReleaseAssetBundle
from mcp_server.artifacts.attestation import Attestation
from mcp_server.artifacts.publisher import ArtifactError, ArtifactPublisher

REPO = "owner/repo"
COMMIT = "abcdef1234567890abcdef1234567890abcdef12"
SHORT_SHA = COMMIT[:7]
SHA_TAG = f"index-repo-main-{SHORT_SHA}"

_SYNTHETIC_ATTESTATION = Attestation(
    bundle_url="https://github.com/owner/repo/attestations/1",
    bundle_path=Path("/tmp/_rollback_archive.tar.gz.attestation.jsonl"),
    subject_digest="sha256stub",
    signed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
)


@pytest.fixture(autouse=True)
def _stub_attest(monkeypatch):
    monkeypatch.setattr(
        "mcp_server.artifacts.publisher.attest",
        MagicMock(return_value=_SYNTHETIC_ATTESTATION),
    )


def _make_uploader() -> IndexArtifactUploader:
    uploader = IndexArtifactUploader.__new__(IndexArtifactUploader)
    uploader.repo = REPO
    uploader.token = ""
    uploader.index_files = []
    uploader.compress_indexes = MagicMock(  # type: ignore[method-assign]
        return_value=(Path("/tmp/_rollback_archive.tar.gz"), "sha256stub", 100)
    )
    uploader.create_metadata = MagicMock(  # type: ignore[method-assign]
        return_value={
            "artifact_type": "full",
            "delta_from": None,
            "checksum": "sha256stub",
        }
    )
    uploader.upload_direct = MagicMock(  # type: ignore[method-assign]
        return_value=ReleaseAssetBundle(
            archive_path=Path("/tmp/_rollback_archive.tar.gz"),
            metadata_path=Path("/tmp/artifact-metadata.json"),
            checksum_path=Path("/tmp/_rollback_archive.tar.gz.sha256"),
            attestation_path=Path("/tmp/_rollback_archive.tar.gz.attestation.jsonl"),
            asset_paths=(
                Path("/tmp/_rollback_archive.tar.gz"),
                Path("/tmp/artifact-metadata.json"),
                Path("/tmp/_rollback_archive.tar.gz.sha256"),
                Path("/tmp/_rollback_archive.tar.gz.attestation.jsonl"),
            ),
        )
    )
    return uploader


class TestPublishRollback:
    """Publisher must retain the SHA-keyed release if a later operation fails."""

    def test_sha_release_retained_on_move_latest_failure(self):
        """A pointer failure must not remove diagnostic release evidence."""
        uploader = _make_uploader()
        publisher = ArtifactPublisher(uploader, gh_cmd="gh")
        delete_calls: list[list[str]] = []

        def side_effect(args, **kwargs):
            subcmd = args[1] if len(args) > 1 else ""
            subsubcmd = args[2] if len(args) > 2 else ""
            tag_arg = args[3] if len(args) > 3 else ""

            # Record delete calls
            if subsubcmd == "delete":
                delete_calls.append(list(args))
                return MagicMock(returncode=0, stdout="", stderr="")

            # _get_latest_commit: view index-latest json → no latest yet
            if subsubcmd == "view" and "index-latest" in args and "--json" in args:
                return MagicMock(returncode=1, stdout="", stderr="not found")

            # _ensure_sha_release: view sha-tag → not found → create succeeds
            if subsubcmd == "view" and SHA_TAG in args:
                return MagicMock(returncode=1, stdout="", stderr="not found")

            # _move_latest_pointer: view index-latest → not found → create fails
            if subsubcmd == "view" and "index-latest" in args:
                return MagicMock(returncode=1, stdout="", stderr="not found")

            if subsubcmd == "create" and "index-latest" in args:
                raise subprocess.CalledProcessError(1, args, b"", b"permission denied")

            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises((ArtifactError, subprocess.CalledProcessError)):
                publisher.publish_on_reindex("repo", COMMIT)

        assert not delete_calls

    def test_pointer_failure_does_not_delete_any_release(self):
        """Cleanup must not mutate either the candidate or latest release."""
        uploader = _make_uploader()
        publisher = ArtifactPublisher(uploader, gh_cmd="gh")
        delete_calls: list[list[str]] = []

        def side_effect(args, **kwargs):
            subsubcmd = args[2] if len(args) > 2 else ""
            if subsubcmd == "delete":
                delete_calls.append(list(args))
                return MagicMock(returncode=0, stdout="", stderr="")
            if subsubcmd == "view" and "--json" in args:
                return MagicMock(returncode=1, stdout="", stderr="not found")
            if subsubcmd == "view":
                return MagicMock(returncode=1, stdout="", stderr="not found")
            if subsubcmd == "create" and "index-latest" in args:
                raise subprocess.CalledProcessError(1, args, b"", b"fail")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises((ArtifactError, subprocess.CalledProcessError)):
                publisher.publish_on_reindex("repo", COMMIT)

        assert not delete_calls

    def test_original_error_reraised_after_rollback(self):
        """The original exception (not the delete exception) must propagate."""
        uploader = _make_uploader()
        publisher = ArtifactPublisher(uploader, gh_cmd="gh")

        def side_effect(args, **kwargs):
            subsubcmd = args[2] if len(args) > 2 else ""
            if subsubcmd == "delete":
                return MagicMock(returncode=0, stdout="", stderr="")
            if subsubcmd == "view" and "--json" in args:
                return MagicMock(returncode=1, stdout="", stderr="not found")
            if subsubcmd == "view":
                return MagicMock(returncode=1, stdout="", stderr="not found")
            if subsubcmd == "create" and "index-latest" in args:
                raise subprocess.CalledProcessError(1, args, b"", b"original error")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises((ArtifactError, subprocess.CalledProcessError)):
                publisher.publish_on_reindex("repo", COMMIT)

    def test_no_rollback_when_sha_release_never_created(self):
        """If _ensure_sha_release itself fails, no delete should be attempted."""
        uploader = _make_uploader()
        publisher = ArtifactPublisher(uploader, gh_cmd="gh")
        delete_calls: list[list[str]] = []

        def side_effect(args, **kwargs):
            subsubcmd = args[2] if len(args) > 2 else ""
            if subsubcmd == "delete":
                delete_calls.append(list(args))
                return MagicMock(returncode=0, stdout="", stderr="")
            if subsubcmd == "view" and "--json" in args:
                return MagicMock(returncode=1, stdout="", stderr="not found")
            if subsubcmd == "view":
                return MagicMock(returncode=1, stdout="", stderr="not found")
            # Fail on create of SHA release
            if subsubcmd == "create" and SHA_TAG in args:
                raise subprocess.CalledProcessError(1, args, b"", b"sha-create-fail")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises((ArtifactError, subprocess.CalledProcessError)):
                publisher.publish_on_reindex("repo", COMMIT)

        sha_deletes = [c for c in delete_calls if SHA_TAG in c]
        assert (
            not sha_deletes
        ), f"Should not delete SHA release we never created; got: {sha_deletes}"

    def test_sha_release_retained_on_asset_upload_failure(self):
        """An interrupted upload retains the SHA release for explicit recovery."""
        uploader = _make_uploader()
        uploader.upload_direct.side_effect = RuntimeError("upload failed")
        publisher = ArtifactPublisher(uploader, gh_cmd="gh")
        delete_calls: list[list[str]] = []

        def side_effect(args, **kwargs):
            subsubcmd = args[2] if len(args) > 2 else ""
            if subsubcmd == "delete":
                delete_calls.append(list(args))
                return MagicMock(returncode=0, stdout="", stderr="")
            if subsubcmd == "view" and "--json" in args:
                return MagicMock(returncode=1, stdout="", stderr="not found")
            if subsubcmd == "view":
                return MagicMock(returncode=1, stdout="", stderr="not found")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=side_effect):
            with pytest.raises(ArtifactError):
                publisher.publish_on_reindex("repo", COMMIT)

        assert not delete_calls
