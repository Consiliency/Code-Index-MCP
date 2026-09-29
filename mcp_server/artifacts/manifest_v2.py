"""Manifest v2 schema for lexical + semantic profile artifact units."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

LEXICAL_ONLY_SEMANTIC_PROFILE_HASH = "lexical-only"
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def build_semantic_profile_hash(
    profiles: Optional[Dict[str, Any]] = None,
) -> str:
    """Build deterministic hash over semantic profile compatibility data."""
    if not profiles:
        return LEXICAL_ONLY_SEMANTIC_PROFILE_HASH

    normalized = []
    for profile_id, payload in sorted(profiles.items()):
        if isinstance(payload, dict):
            fingerprint = (
                payload.get("compatibility_fingerprint") or payload.get("compatibility_hash") or ""
            )
        else:
            fingerprint = str(payload or "")
        normalized.append(f"{profile_id}:{fingerprint}")

    if not normalized:
        return LEXICAL_ONLY_SEMANTIC_PROFILE_HASH

    raw = "\n".join(normalized).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_semantic_profile_hash(value: str) -> bool:
    """Return whether *value* is a valid canonical semantic profile hash."""
    return value == LEXICAL_ONLY_SEMANTIC_PROFILE_HASH or bool(_SHA256_HEX_RE.match(value))


def build_logical_artifact_id(
    repo_id: str,
    tracked_branch: str,
    commit: str,
    semantic_profile_hash: str,
    artifact_type: str = "full",
) -> str:
    """Build the canonical P31 logical artifact id."""
    safe_repo = repo_id.replace("/", "_").replace(":", "_")
    safe_branch = tracked_branch.replace("/", "_").replace(":", "_")
    short_commit = commit[:8] if commit else "unknown"
    short_profile = (
        semantic_profile_hash
        if semantic_profile_hash == LEXICAL_ONLY_SEMANTIC_PROFILE_HASH
        else semantic_profile_hash[:12]
    )
    return f"mcp-index-{safe_repo}-{safe_branch}-{short_commit}-{short_profile}-{artifact_type}"


@dataclass(frozen=True)
class ManifestUnit:
    """Single logical artifact unit reference."""

    unit_type: str
    unit_id: str
    checksum: str
    size_bytes: int
    profile_id: Optional[str] = None
    compatibility_fingerprint: Optional[str] = None
    transport_ref: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize manifest unit."""
        return {
            "unit_type": self.unit_type,
            "unit_id": self.unit_id,
            "checksum": self.checksum,
            "size_bytes": self.size_bytes,
            "profile_id": self.profile_id,
            "compatibility_fingerprint": self.compatibility_fingerprint,
            "transport_ref": self.transport_ref,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class ArtifactManifestV2:
    """Top-level manifest contract for composable index artifacts."""

    logical_artifact_id: str
    repo_id: str
    branch: str
    commit: str
    schema_version: str
    chunk_schema_version: str
    chunk_identity_algorithm: str
    units: List[ManifestUnit]
    semantic_profile_hash: str = LEXICAL_ONLY_SEMANTIC_PROFILE_HASH
    checksum: str = ""
    artifact_type: str = "full"
    tracked_branch: Optional[str] = None
    manifest_version: str = "2"

    def validate(self) -> None:
        """Validate manifest invariants required for reliable consumption."""
        if self.manifest_version != "2":
            raise ValueError(f"Unsupported manifest version: {self.manifest_version}")
        from mcp_server.storage.sqlite_store import LEGACY_CHUNK_ID_SCHEME, current_chunk_id_scheme

        if self.chunk_identity_algorithm not in {
            LEGACY_CHUNK_ID_SCHEME,
            current_chunk_id_scheme(),
        }:
            raise ValueError(
                f"Unsupported chunk identity algorithm: {self.chunk_identity_algorithm}"
            )
        if not self.repo_id:
            raise ValueError("Manifest v2 requires repo_id")
        if not self.canonical_tracked_branch:
            raise ValueError("Manifest v2 requires tracked_branch")
        if self.tracked_branch and self.branch and self.tracked_branch != self.branch:
            raise ValueError("Manifest branch aliases disagree")
        if not self.commit:
            raise ValueError("Manifest v2 requires commit")
        if not self.schema_version:
            raise ValueError("Manifest v2 requires schema_version")
        if not self.resolved_checksum:
            raise ValueError("Manifest v2 requires checksum")
        if self.artifact_type not in {"full", "delta"}:
            raise ValueError(f"Invalid artifact_type: {self.artifact_type}")
        if not validate_semantic_profile_hash(self.semantic_profile_hash):
            raise ValueError(f"Malformed semantic_profile_hash: {self.semantic_profile_hash}")
        if not self.units:
            raise ValueError("Manifest v2 requires at least one unit")

        lexical_units = [u for u in self.units if u.unit_type == "lexical"]
        if len(lexical_units) != 1:
            raise ValueError("Manifest v2 requires exactly one lexical unit")
        if lexical_units[0].checksum != self.resolved_checksum:
            raise ValueError("Manifest lexical checksum disagrees with archive checksum")

        seen_ids = set()
        for unit in self.units:
            if unit.unit_id in seen_ids:
                raise ValueError(f"Duplicate manifest unit id: {unit.unit_id}")
            seen_ids.add(unit.unit_id)

            if unit.size_bytes < 0:
                raise ValueError(f"Invalid unit size for {unit.unit_id}: {unit.size_bytes}")

            if unit.unit_type == "semantic_profile":
                if not unit.profile_id:
                    raise ValueError(f"semantic_profile unit missing profile_id: {unit.unit_id}")
                if not unit.compatibility_fingerprint:
                    raise ValueError(
                        f"semantic_profile unit missing compatibility_fingerprint: {unit.unit_id}"
                    )

    @property
    def canonical_tracked_branch(self) -> str:
        """Return canonical tracked branch while accepting legacy branch input."""
        return self.tracked_branch or self.branch

    @property
    def resolved_checksum(self) -> str:
        """Return manifest checksum, falling back to the lexical unit checksum."""
        if self.checksum:
            return self.checksum
        for unit in self.units:
            if unit.unit_type == "lexical" and unit.checksum:
                return unit.checksum
        return ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize manifest to JSON-compatible dictionary."""
        self.validate()
        return {
            "manifest_version": self.manifest_version,
            "logical_artifact_id": self.logical_artifact_id,
            "repo_id": self.repo_id,
            "tracked_branch": self.canonical_tracked_branch,
            "branch": self.canonical_tracked_branch,
            "commit": self.commit,
            "schema_version": self.schema_version,
            "semantic_profile_hash": self.semantic_profile_hash,
            "checksum": self.resolved_checksum,
            "artifact_type": self.artifact_type,
            "chunk_schema_version": self.chunk_schema_version,
            "chunk_identity_algorithm": self.chunk_identity_algorithm,
            "units": [unit.to_dict() for unit in self.units],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ArtifactManifestV2":
        """Create and validate manifest from JSON payload."""
        if (
            "tracked_branch" in payload
            and "branch" in payload
            and (payload["tracked_branch"] != payload["branch"])
        ):
            raise ValueError("Manifest branch aliases disagree")
        units = [ManifestUnit(**unit) for unit in payload.get("units", [])]
        manifest = cls(
            manifest_version=str(payload.get("manifest_version", "2")),
            logical_artifact_id=payload["logical_artifact_id"],
            repo_id=payload["repo_id"],
            branch=payload.get("tracked_branch") or payload["branch"],
            tracked_branch=payload.get("tracked_branch") or payload.get("branch"),
            commit=payload["commit"],
            schema_version=payload["schema_version"],
            semantic_profile_hash=payload["semantic_profile_hash"],
            checksum=payload["checksum"],
            artifact_type=payload["artifact_type"],
            chunk_schema_version=payload["chunk_schema_version"],
            chunk_identity_algorithm=payload["chunk_identity_algorithm"],
            units=units,
        )
        manifest.validate()
        return manifest


@dataclass(frozen=True)
class WorkspaceArtifactManifest:
    """Manifest describing a workspace composed of multiple repository artifacts."""

    workspace_id: str
    workspace_commit: str
    repositories: List[Dict[str, Any]]
    manifest_version: str = "1"

    def validate(self) -> None:
        """Validate workspace manifest invariants."""
        if not self.repositories:
            raise ValueError("Workspace manifest requires at least one repository")

        seen_ids = set()
        for repo in self.repositories:
            repo_id = repo.get("repo_id")
            if not repo_id:
                raise ValueError("Workspace manifest repository entry missing repo_id")
            if repo_id in seen_ids:
                raise ValueError(f"Duplicate workspace repository id: {repo_id}")
            seen_ids.add(repo_id)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize workspace manifest."""
        self.validate()
        return {
            "manifest_version": self.manifest_version,
            "workspace_id": self.workspace_id,
            "workspace_commit": self.workspace_commit,
            "repositories": self.repositories,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "WorkspaceArtifactManifest":
        """Create workspace manifest from JSON payload."""
        manifest = cls(
            manifest_version=str(payload.get("manifest_version", "1")),
            workspace_id=payload["workspace_id"],
            workspace_commit=payload["workspace_commit"],
            repositories=list(payload.get("repositories", [])),
        )
        manifest.validate()
        return manifest
