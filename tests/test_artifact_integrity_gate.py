"""Tests for artifact integrity gate validation and downloader integration."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import mcp_server.artifacts.artifact_download as artifact_download_module
from mcp_server.artifacts.artifact_download import IndexArtifactDownloader
from mcp_server.artifacts.integrity_gate import validate_artifact_integrity
from mcp_server.artifacts.manifest_v2 import ArtifactManifestV2, ManifestUnit
from mcp_server.artifacts.semantic_profiles import SemanticProfileRegistry


def _configured_profiles() -> dict:
    return {
        "oss_high": {
            "provider": "openai_compatible",
            "model_name": "Qwen/Qwen3-Embedding-8B",
            "model_version": "e2e",
            "vector_dimension": 4096,
            "distance_metric": "dot",
            "normalization_policy": "l2",
            "chunk_schema_version": "1",
            "chunker_version": "treesitter-v2",
            "build_metadata": {
                "collection_name": "code_index__oss_high__v1",
                "enrichment_model": "chat",
                "enrichment_base_url": "http://ai:8002/v1",
                "prompt_template": "Summarize this chunk.",
            },
        },
        "commercial_high": {
            "provider": "voyage",
            "model_name": "voyage-code-3",
            "model_version": "3",
            "vector_dimension": 2048,
            "distance_metric": "dot",
            "normalization_policy": "provider-default",
            "chunk_schema_version": "1",
            "chunker_version": "treesitter-v2",
            "build_metadata": {
                "collection_name": "code_index__commercial_high__v1",
                "enrichment_model": "gpt-5.4-mini",
                "enrichment_base_url": "https://api.openai.com/v1",
                "prompt_template": "Summarize this chunk.",
            },
        },
    }


def _write_archive(tmp_path: Path, payload: bytes = b"index-archive") -> Path:
    archive_path = tmp_path / "artifact.tar.gz"
    archive_path.write_bytes(payload)
    return archive_path


def _base_metadata(checksum: str) -> dict:
    return {
        "repo_id": "owner/repo",
        "checksum": checksum,
        "commit": "0123456789abcdef",
        "tracked_branch": "main",
        "branch": "main",
        "schema_version": "2",
        "semantic_profile_hash": "lexical-only",
        "artifact_type": "full",
        "timestamp": "2026-03-03T00:00:00Z",
        "compatibility": {
            "schema_version": "2",
            "embedding_model": "voyage-code-3",
        },
    }


def test_integrity_gate_passes_valid_metadata_and_checksum(tmp_path: Path):
    archive_path = _write_archive(tmp_path)
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    metadata = _base_metadata(checksum)

    result = validate_artifact_integrity(metadata=metadata, archive_path=archive_path)

    assert result.passed is True
    assert result.reasons == []
    assert result.expected_checksum == checksum
    assert result.actual_checksum == checksum


def test_integrity_gate_fails_on_missing_required_metadata_key(tmp_path: Path):
    archive_path = _write_archive(tmp_path)
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    metadata = _base_metadata(checksum)
    metadata.pop("commit")

    result = validate_artifact_integrity(metadata=metadata, archive_path=archive_path)

    assert result.passed is False
    assert "missing key: commit" in result.reasons


def test_integrity_gate_rejects_sidecar_disagreeing_with_metadata(tmp_path: Path):
    archive_path = _write_archive(tmp_path)
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    metadata = _base_metadata("wrong-checksum")
    checksum_path = tmp_path / "artifact.sha256"
    checksum_path.write_text(f"{checksum} artifact.tar.gz\n")

    result = validate_artifact_integrity(
        metadata=metadata,
        archive_path=archive_path,
        checksum_path=checksum_path,
    )

    assert result.passed is False
    assert result.expected_checksum == "wrong-checksum"
    assert "checksum sidecar disagrees with signed metadata" in result.reasons


def test_integrity_gate_validates_optional_manifest_v2_payload(tmp_path: Path):
    archive_path = _write_archive(tmp_path)
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    manifest = ArtifactManifestV2(
        logical_artifact_id="repo-main-abc123",
        repo_id="owner/repo",
        branch="main",
        tracked_branch="main",
        commit="abc123",
        schema_version="2",
        semantic_profile_hash="lexical-only",
        checksum="deadbeef",
        artifact_type="full",
        chunk_schema_version="2.0",
        chunk_identity_algorithm="treesitter_chunk_id_v1",
        units=[
            ManifestUnit(
                unit_type="lexical",
                unit_id="lexical-main-abc123",
                checksum="deadbeef",
                size_bytes=1024,
            )
        ],
    )
    metadata = _base_metadata(checksum)
    metadata["manifest_v2"] = manifest.to_dict()

    result = validate_artifact_integrity(metadata=metadata, archive_path=archive_path)

    assert result.passed is True
    assert result.manifest_v2_validated is True


def test_integrity_gate_fails_for_invalid_manifest_v2_payload(tmp_path: Path):
    archive_path = _write_archive(tmp_path)
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    metadata = _base_metadata(checksum)
    metadata["manifest_v2"] = {"repo_id": "owner/repo"}

    result = validate_artifact_integrity(metadata=metadata, archive_path=archive_path)

    assert result.passed is False
    assert any(reason.startswith("invalid manifest_v2:") for reason in result.reasons)


def test_downloader_run_integrity_gate_reuses_shared_gate(tmp_path: Path):
    downloader = IndexArtifactDownloader(repo="owner/repo")

    archive_path = _write_archive(tmp_path)
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    metadata = _base_metadata(checksum)

    result = downloader._run_integrity_gate(
        metadata=metadata,
        archive_path=archive_path,
        checksum_path=None,
    )

    assert result.passed is True


def test_downloader_run_integrity_gate_fails_closed(tmp_path: Path):
    downloader = IndexArtifactDownloader(repo="owner/repo")

    archive_path = _write_archive(tmp_path)
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    metadata = _base_metadata(checksum)
    metadata.pop("tracked_branch")
    metadata.pop("branch")

    with pytest.raises(ValueError, match="Artifact integrity gate failed"):
        downloader._run_integrity_gate(
            metadata=metadata,
            archive_path=archive_path,
            checksum_path=None,
        )


def test_downloader_accepts_artifact_when_any_profile_matches(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    configured_profiles = _configured_profiles()
    registry = SemanticProfileRegistry.from_raw(configured_profiles, "oss_high")
    monkeypatch.setattr(
        artifact_download_module,
        "get_settings",
        lambda: SimpleNamespace(
            app_version="test",
            semantic_embedding_model="voyage-code-3",
            get_semantic_profiles_config=lambda: configured_profiles,
            get_semantic_default_profile=lambda: "oss_high",
        ),
    )

    downloader = IndexArtifactDownloader(repo="owner/repo")
    metadata = {
        "compatibility": {
            "schema_version": "2",
            "semantic_profiles": {
                "oss_high": {
                    "compatibility_fingerprint": registry.get("oss_high").compatibility_fingerprint
                }
            },
        }
    }

    compatible, issues = downloader.check_compatibility(metadata)

    assert compatible is True
    assert issues == []


def test_downloader_rejects_artifact_when_profile_fingerprints_mismatch(
    monkeypatch, tmp_path: Path
):
    monkeypatch.chdir(tmp_path)
    configured_profiles = _configured_profiles()
    monkeypatch.setattr(
        artifact_download_module,
        "get_settings",
        lambda: SimpleNamespace(
            app_version="test",
            semantic_embedding_model="voyage-code-3",
            get_semantic_profiles_config=lambda: {"oss_high": configured_profiles["oss_high"]},
            get_semantic_default_profile=lambda: "oss_high",
        ),
    )

    downloader = IndexArtifactDownloader(repo="owner/repo")
    metadata = {
        "compatibility": {
            "schema_version": "2",
            "semantic_profiles": {
                "oss_high": {"compatibility_fingerprint": "different-fingerprint"}
            },
        }
    }

    compatible, issues = downloader.check_compatibility(metadata)

    assert compatible is False
    assert issues == ["Semantic profile fingerprint mismatch for profiles: oss_high"]
