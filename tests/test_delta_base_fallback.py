"""Never reinterpret authenticated delta metadata as a standalone full snapshot."""

import hashlib
import json
import subprocess

import pytest

from mcp_server.artifacts.artifact_upload import _metadata_bytes
from mcp_server.artifacts.delta_artifacts import DeltaManifest, build_delta_archive
from mcp_server.artifacts.manifest_v2 import ArtifactManifestV2, ManifestUnit
from tests.test_v13_prep_repairs import artifact_payload


@pytest.mark.parametrize("kind", ["delta", "legacy_delta", "full"])
def test_delta_restore_refuses_without_rewriting_signed_metadata(
    artifact_payload, tmp_path, monkeypatch, kind
):
    downloader, payload, metadata, _verified = artifact_payload
    if kind == "delta":
        metadata.update(artifact_type="delta", base_commit="b" * 40, target_commit="a" * 40)
        archive = payload / "index.tar.gz"
        build_delta_archive(DeltaManifest("b" * 40, "a" * 40, [], {}), tmp_path, archive)
        metadata["checksum"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        metadata["manifest_v2"] = ArtifactManifestV2(
            logical_artifact_id="synthetic-delta",
            repo_id="fixture",
            branch="main",
            commit="a" * 40,
            schema_version="2",
            chunk_schema_version="2",
            chunk_identity_algorithm="treesitter_chunk_id_v1",
            artifact_type="delta",
            checksum=metadata["checksum"],
            units=[
                ManifestUnit("lexical", "fixture", metadata["checksum"], archive.stat().st_size)
            ],
        ).to_dict()
    elif kind == "legacy_delta":
        metadata["delta_from"] = "index-base"
    subject = payload / "artifact-metadata.json"
    subject.write_bytes(_metadata_bytes(metadata))
    signed_digest = hashlib.sha256(subject.read_bytes()).hexdigest()
    verified = []

    def gh(args, **kwargs):
        assert args[:3] == ["gh", "attestation", "verify"]
        assert args[3] == str(subject)
        assert hashlib.sha256(subject.read_bytes()).hexdigest() == signed_digest
        verified.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    captured = []
    gate = downloader._run_integrity_gate

    def observe(value, archive, checksum):
        captured.append(dict(value))
        return gate(value, archive, checksum)

    monkeypatch.setattr(subprocess, "run", gh)
    monkeypatch.setattr(downloader, "_run_integrity_gate", observe)
    output = tmp_path / "output"
    if kind != "full":
        with pytest.raises(ValueError, match="authenticated base chain"):
            downloader._restore_downloaded_payload(payload, output)
        assert not output.exists()
    else:
        restored = downloader._restore_downloaded_payload(payload, output)
        assert (restored / "current.db").is_file()
        assert json.loads((restored / "artifact-metadata.json").read_text()) == metadata
    assert verified and len(captured) == 1
    assert captured[0] == metadata
    assert hashlib.sha256(subject.read_bytes()).hexdigest() == signed_digest
