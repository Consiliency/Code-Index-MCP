"""Tests for P31 fail-closed artifact download and repo-scoped hydration."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_server.artifacts.artifact_download import IndexArtifactDownloader
from mcp_server.artifacts.attestation import AttestationError
from mcp_server.artifacts.freshness import FreshnessVerdict


def _metadata(**overrides) -> dict:
    payload = {
        "repo_id": "repo-id",
        "tracked_branch": "main",
        "branch": "main",
        "commit": "abcdef123456",
        "schema_version": "2",
        "semantic_profile_hash": "lexical-only",
        "checksum": "deadbeef",
        "artifact_type": "full",
        "timestamp": "2026-04-23T00:00:00Z",
        "compatibility": {
            "schema_version": "2",
            "embedding_model": "lexical-only",
        },
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("advertised_url", [None, "", "https://example.invalid/bundle"])
def test_enforce_requires_attestation_before_extraction(tmp_path, monkeypatch, advertised_url):
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
    payload = tmp_path / "payload"
    payload.mkdir()
    archive = payload / "index.tar.gz"
    source = tmp_path / "current.db"
    source.write_bytes(b"synthetic-index")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source, arcname="current.db")
    metadata = _metadata(attestation_url=advertised_url)
    (payload / "artifact-metadata.json").write_text(json.dumps(metadata))
    output = tmp_path / "output"
    output.mkdir()
    downloader = IndexArtifactDownloader(repo="owner/repo")
    with (
        patch.object(downloader, "_run_integrity_gate"),
        patch.object(downloader, "check_compatibility", return_value=(True, [])),
        pytest.raises(AttestationError),
    ):
        downloader._restore_downloaded_payload(payload, output, allow_unsafe=True)
    assert list(output.iterdir()) == []


def test_validate_artifact_identity_rejects_wrong_repo_branch_commit_and_profile():
    downloader = IndexArtifactDownloader(repo="owner/repo")

    reasons = downloader.validate_artifact_identity(
        _metadata(
            repo_id="other",
            tracked_branch="feature",
            commit="old",
            semantic_profile_hash="bad",
        ),
        repo_id="repo-id",
        tracked_branch="main",
        target_commit="abcdef123456",
        semantic_profile_hash="lexical-only",
    )

    assert any("repo_id mismatch" in reason for reason in reasons)
    assert any("tracked_branch mismatch" in reason for reason in reasons)
    assert any("commit mismatch" in reason for reason in reasons)
    assert any("semantic_profile_hash mismatch" in reason for reason in reasons)


def test_install_indexes_hydrates_repo_scoped_current_db(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "current.db").write_text("db", encoding="utf-8")
    (source / ".index_metadata.json").write_text("{}", encoding="utf-8")
    (source / "artifact-metadata.json").write_text(json.dumps(_metadata()), encoding="utf-8")

    repo_root = tmp_path / "repo"
    index_location = repo_root / ".mcp-index"
    index_path = index_location / "current.db"

    installed = IndexArtifactDownloader(repo="owner/repo").install_indexes(
        source,
        index_location=index_location,
        index_path=index_path,
        backup=False,
    )

    assert str(index_path) in installed
    assert index_path.read_text(encoding="utf-8") == "db"
    assert (index_location / ".index_metadata.json").exists()
    assert (index_location / "artifact-metadata.json").exists()
    assert not (repo_root / "code_index.db").exists()


def test_install_indexes_accepts_legacy_code_index_after_validation(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "code_index.db").write_text("legacy-db", encoding="utf-8")
    (source / "artifact-metadata.json").write_text(json.dumps(_metadata()), encoding="utf-8")

    index_location = tmp_path / "repo" / ".mcp-index"
    index_path = index_location / "current.db"

    IndexArtifactDownloader(repo="owner/repo").install_indexes(
        source,
        index_location=index_location,
        index_path=index_path,
        backup=False,
    )

    assert index_path.read_text(encoding="utf-8") == "legacy-db"


@pytest.mark.parametrize("collision", ["database", "sidecar", "metadata", "vectors"])
def test_install_never_replaces_existing_generation_resources(tmp_path, collision):
    source, destination = tmp_path / "source", tmp_path / "active"
    source.mkdir()
    destination.mkdir()
    (source / "current.db").write_bytes(b"replacement")
    (source / ".index_metadata.json").write_text("{}")
    target = {
        "database": destination / "current.db",
        "sidecar": destination / "current.db-wal",
        "metadata": destination / ".index_metadata.json",
        "vectors": destination / "vector_index.qdrant",
    }[collision]
    if collision == "vectors":
        target.mkdir()
        target = target / "marker"
    target.write_bytes(b"active")
    downloader = IndexArtifactDownloader(repo="owner/repo")
    with pytest.raises(FileExistsError, match="staging"):
        downloader.install_indexes(source, index_location=destination, backup=False)
    assert target.read_bytes() == b"active"
    if collision != "database":
        assert not (destination / "current.db").exists()


def test_install_rejects_ambiguous_database_before_copying(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "current.db").write_bytes(b"current")
    (source / "code_index.db").write_bytes(b"legacy")
    with pytest.raises(ValueError, match="exactly one"):
        IndexArtifactDownloader(repo="owner/repo").install_indexes(
            source, index_location=tmp_path / "stage"
        )
    assert not (tmp_path / "stage" / "current.db").exists()


@pytest.mark.parametrize(
    "verdict",
    [FreshnessVerdict.STALE_COMMIT, FreshnessVerdict.STALE_AGE, FreshnessVerdict.INVALID],
)
def test_download_selected_artifact_blocks_stale_or_invalid_by_default(
    tmp_path: Path, verdict: FreshnessVerdict
):
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    (extracted / "artifact-metadata.json").write_text(json.dumps(_metadata()), encoding="utf-8")
    downloader = IndexArtifactDownloader(repo="owner/repo")

    with (
        patch.object(downloader, "download_artifact", return_value=extracted),
        patch(
            "mcp_server.artifacts.artifact_download.verify_artifact_freshness",
            return_value=verdict,
        ),
        patch.object(downloader, "install_indexes") as install,
        pytest.raises(ValueError, match="freshness validation failed"),
    ):
        downloader.download_selected_artifact(
            {"id": 1, "name": "mcp-index-repo-id-main-abcdef12"},
            output_dir=tmp_path,
            backup=False,
        )

    install.assert_not_called()


def test_download_selected_artifact_unsafe_override_reports_reasons(tmp_path: Path):
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    (extracted / "artifact-metadata.json").write_text(json.dumps(_metadata()), encoding="utf-8")
    downloader = IndexArtifactDownloader(repo="owner/repo")

    with (
        patch.object(downloader, "download_artifact", return_value=extracted),
        patch(
            "mcp_server.artifacts.artifact_download.verify_artifact_freshness",
            return_value=FreshnessVerdict.STALE_COMMIT,
        ),
        patch.object(downloader, "install_indexes", return_value=[".mcp-index/current.db"]),
    ):
        result = downloader.download_selected_artifact(
            {"id": 1, "name": "mcp-index-repo-id-main-abcdef12"},
            output_dir=tmp_path,
            backup=False,
            allow_unsafe=True,
        )

    assert result.installed_items == [".mcp-index/current.db"]
    assert result.validation_reasons == ["freshness verdict: stale_commit"]


def test_download_release_artifact_restores_direct_publish_payload(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "skip")
    payload_dir = tmp_path / "release-assets"
    payload_dir.mkdir()
    archive_path = payload_dir / "index-archive.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        current_db = tmp_path / "current.db"
        current_db.write_text("db", encoding="utf-8")
        tar.add(current_db, arcname="current.db")
    checksum = IndexArtifactDownloader(repo="owner/repo")._calculate_checksum(archive_path)
    (payload_dir / "artifact-metadata.json").write_text(
        json.dumps(
            _metadata(
                checksum=checksum,
                semantic_profile_hash="a" * 64,
                manifest_v2={
                    "logical_artifact_id": "logical-id",
                    "repo_id": "repo-id",
                    "tracked_branch": "main",
                    "branch": "main",
                    "commit": "abcdef123456",
                    "schema_version": "2",
                    "semantic_profile_hash": "a" * 64,
                    "checksum": checksum,
                    "artifact_type": "full",
                    "chunk_schema_version": "2",
                    "chunk_identity_algorithm": "treesitter_chunk_id_v1",
                    "units": [
                        {
                            "unit_type": "lexical",
                            "unit_id": "lexical-abcdef12",
                            "checksum": checksum,
                            "size_bytes": archive_path.stat().st_size,
                        }
                    ],
                },
            )
        ),
        encoding="utf-8",
    )
    (payload_dir / "index-archive.tar.gz.sha256").write_text(
        f"{checksum}  index-archive.tar.gz\n", encoding="utf-8"
    )

    downloader = IndexArtifactDownloader(repo="owner/repo")

    def side_effect(args, **kwargs):
        if args[:4] == ["gh", "release", "download", "index-sha-tag"]:
            dest = Path(args[args.index("--dir") + 1])
            for file in payload_dir.iterdir():
                if file.is_file():
                    (dest / file.name).write_bytes(file.read_bytes())
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    with (
        patch("subprocess.run", side_effect=side_effect),
        patch.object(downloader, "check_compatibility", return_value=(True, [])),
    ):
        restored = downloader.download_release_artifact(
            "index-sha-tag",
            output_dir,
            repo_id="repo-id",
            tracked_branch="main",
            target_commit="abcdef123456",
        )

    assert restored.parent == output_dir
    assert restored.name.startswith("verified-")
    assert (restored / "current.db").read_text(encoding="utf-8") == "db"
    assert (restored / "artifact-metadata.json").exists()
