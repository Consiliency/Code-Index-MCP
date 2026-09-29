"""Fail-closed integrity validation for downloaded index artifacts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp_server.artifacts.manifest_v2 import ArtifactManifestV2


@dataclass(frozen=True)
class ArtifactIntegrityGateResult:
    """Result payload produced by artifact integrity validation."""

    passed: bool
    reasons: List[str] = field(default_factory=list)
    expected_checksum: Optional[str] = None
    actual_checksum: Optional[str] = None
    manifest_v2_validated: bool = False


def validate_required_metadata_fields(metadata: Dict[str, Any]) -> List[str]:
    """Validate mandatory metadata structure and fields."""
    reasons: List[str] = []

    if "tracked_branch" not in metadata and "branch" in metadata:
        metadata = dict(metadata)
        metadata["tracked_branch"] = metadata["branch"]

    required_keys = [
        "repo_id",
        "tracked_branch",
        "commit",
        "schema_version",
        "semantic_profile_hash",
        "checksum",
        "artifact_type",
        "timestamp",
        "compatibility",
    ]
    for key in required_keys:
        if key not in metadata:
            reasons.append(f"missing key: {key}")

    semantic_profile_hash = metadata.get("semantic_profile_hash")
    if semantic_profile_hash is not None:
        from mcp_server.artifacts.manifest_v2 import validate_semantic_profile_hash

        if not validate_semantic_profile_hash(str(semantic_profile_hash)):
            reasons.append(f"malformed semantic_profile_hash: {semantic_profile_hash}")

    compatibility = metadata.get("compatibility")
    if compatibility is None:
        return reasons

    if not isinstance(compatibility, dict):
        reasons.append("compatibility must be an object")
        return reasons

    for key in ["schema_version", "embedding_model"]:
        if key not in compatibility:
            reasons.append(f"missing compatibility key: {key}")

    artifact_type = metadata.get("artifact_type", "full")
    if artifact_type not in {"full", "delta"}:
        reasons.append(f"invalid artifact_type: {artifact_type}")
    elif artifact_type == "delta":
        for key in ["base_commit", "target_commit"]:
            if key not in metadata or not metadata.get(key):
                reasons.append(f"missing delta metadata key: {key}")

    return reasons


def _calculate_checksum(file_path: Path) -> str:
    """Calculate SHA256 checksum of a local file."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _extract_manifest_v2_payload(metadata: Dict[str, Any]) -> Optional[Any]:
    """Extract optional manifest v2 payload from known metadata keys."""
    for key in ["manifest_v2", "artifact_manifest_v2"]:
        if key in metadata:
            return metadata[key]
    return None


def validate_artifact_integrity(
    metadata: Dict[str, Any],
    archive_path: Path,
    checksum_path: Optional[Path] = None,
) -> ArtifactIntegrityGateResult:
    """Validate metadata, checksum, and optional manifest v2 payload."""
    reasons = validate_required_metadata_fields(metadata)

    expected_checksum = str(metadata.get("checksum") or "") or None
    if checksum_path and checksum_path.exists():
        fields = checksum_path.read_text().split()
        sidecar_checksum = fields[0] if fields else None
        if sidecar_checksum != expected_checksum:
            reasons.append("checksum sidecar disagrees with signed metadata")
    actual_checksum: Optional[str] = None
    if not expected_checksum:
        reasons.append("artifact checksum is required but missing")
    else:
        actual_checksum = _calculate_checksum(archive_path)
        if actual_checksum != expected_checksum:
            reasons.append(
                f"checksum mismatch: expected={expected_checksum}, actual={actual_checksum}"
            )

    manifest_v2_validated = False
    manifest_v2_payload = _extract_manifest_v2_payload(metadata)
    if "manifest_v2" in metadata or "artifact_manifest_v2" in metadata:
        if (
            "manifest_v2" in metadata
            and "artifact_manifest_v2" in metadata
            and (metadata["manifest_v2"] != metadata["artifact_manifest_v2"])
        ):
            reasons.append("manifest_v2 aliases disagree")
        if not isinstance(manifest_v2_payload, dict):
            reasons.append("manifest_v2 must be an object")
        else:
            try:
                manifest = ArtifactManifestV2.from_dict(manifest_v2_payload)
                bound_fields = {
                    "repo_id": manifest.repo_id,
                    "tracked_branch": manifest.canonical_tracked_branch,
                    "commit": manifest.commit,
                    "schema_version": manifest.schema_version,
                    "semantic_profile_hash": manifest.semantic_profile_hash,
                    "checksum": manifest.resolved_checksum,
                    "artifact_type": manifest.artifact_type,
                }
                manifest_reasons = []
                for key, value in bound_fields.items():
                    outer = metadata.get(key)
                    if key == "tracked_branch":
                        outer = outer or metadata.get("branch")
                    if str(outer) != str(value):
                        manifest_reasons.append(f"manifest_v2 {key} disagrees with metadata")
                if "logical_artifact_id" in metadata and (
                    manifest.logical_artifact_id != metadata["logical_artifact_id"]
                ):
                    manifest_reasons.append(
                        "manifest_v2 logical_artifact_id disagrees with metadata"
                    )
                compatibility = metadata.get("compatibility")
                if isinstance(compatibility, dict):
                    if str(compatibility.get("schema_version")) != str(manifest.schema_version):
                        manifest_reasons.append("manifest_v2 schema disagrees with compatibility")
                    if "chunk_schema_version" in compatibility and str(
                        compatibility["chunk_schema_version"]
                    ) != str(manifest.chunk_schema_version):
                        manifest_reasons.append(
                            "manifest_v2 chunk schema disagrees with compatibility"
                        )
                lexical = next(unit for unit in manifest.units if unit.unit_type == "lexical")
                if lexical.size_bytes != archive_path.stat().st_size:
                    manifest_reasons.append("manifest_v2 lexical size disagrees with archive")
                if (
                    "compressed_size" in metadata
                    and metadata["compressed_size"] != lexical.size_bytes
                ):
                    manifest_reasons.append("manifest_v2 compressed size disagrees with metadata")
                reasons.extend(manifest_reasons)
                manifest_v2_validated = not manifest_reasons
            except (KeyError, TypeError, ValueError) as exc:
                reasons.append(f"invalid manifest_v2: {exc}")

    return ArtifactIntegrityGateResult(
        passed=not reasons,
        reasons=reasons,
        expected_checksum=expected_checksum,
        actual_checksum=actual_checksum,
        manifest_v2_validated=manifest_v2_validated,
    )
