"""Provider interfaces for artifact storage backends."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple


@dataclass
class ArtifactRecord:
    """Normalized artifact metadata record."""

    artifact_id: str
    name: str
    created_at: str
    size_bytes: int = 0
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert record to dictionary compatible with existing callers."""
        # Preserve numeric IDs as integers for API compatibility
        try:
            artifact_id: Any = int(self.artifact_id)
        except ValueError, TypeError:
            artifact_id = self.artifact_id
        return {
            "id": artifact_id,
            "name": self.name,
            "created_at": self.created_at,
            "size_in_bytes": self.size_bytes,
            **(self.metadata or {}),
        }


class ArtifactProvider(Protocol):
    """Storage provider protocol for index artifacts."""

    def list_artifacts(self, prefixes: Tuple[str, ...]) -> List[ArtifactRecord]:
        """List artifacts filtered by name prefix."""

    def download_artifact(self, artifact_id: str, destination: Path) -> Path:
        """Download artifact payload to destination and return payload path."""

    def upload_artifact(
        self,
        artifact_name: str,
        source_paths: List[Path],
        retention_days: int,
    ) -> str:
        """Upload artifact and return provider artifact ID."""

    def delete_artifact(self, artifact_id: str) -> bool:
        """Delete artifact by ID."""
