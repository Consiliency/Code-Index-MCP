"""Delta-base discovery uses authenticated metadata and a real restore."""

import hashlib
import json
import subprocess

import pytest

from mcp_server.artifacts.artifact_upload import _metadata_bytes
from tests.test_v13_prep_repairs import artifact_payload


@pytest.mark.parametrize("base", ["missing", "present", "none"])
def test_delta_base_probe_preserves_signed_metadata(artifact_payload, tmp_path, monkeypatch, base):
    downloader, payload, metadata, _verified = artifact_payload
    if base != "none":
        metadata["delta_from"] = "index-base"
    subject = payload / "artifact-metadata.json"
    subject.write_bytes(_metadata_bytes(metadata))
    signed_digest = hashlib.sha256(subject.read_bytes()).hexdigest()
    probes = []
    verified = []

    def gh(args, **kwargs):
        if args[:3] == ["gh", "attestation", "verify"]:
            assert args[3] == str(subject)
            assert hashlib.sha256(subject.read_bytes()).hexdigest() == signed_digest
            verified.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        assert args == ["gh", "release", "view", "index-base", "--repo", "synthetic/example"]
        assert kwargs["timeout"] == 30
        probes.append(args)
        return subprocess.CompletedProcess(args, 1 if base == "missing" else 0, "", "")

    captured = []
    gate = downloader._run_integrity_gate

    def observe(value, archive, checksum):
        captured.append(dict(value))
        return gate(value, archive, checksum)

    monkeypatch.setattr(subprocess, "run", gh)
    monkeypatch.setattr(downloader, "_run_integrity_gate", observe)
    restored = downloader._restore_downloaded_payload(payload, tmp_path / "output")
    assert verified and len(captured) == 1
    assert len(probes) == (0 if base == "none" else 1)
    assert captured[0].get("delta_from") == ("index-base" if base == "present" else None)
    assert (restored / "current.db").is_file()
    assert json.loads((restored / "artifact-metadata.json").read_text()) == metadata
