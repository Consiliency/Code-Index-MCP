"""Multi-repository artifact lifecycle coordination."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from uuid import uuid4

from mcp_server.artifacts.artifact_download import IndexArtifactDownloader
from mcp_server.artifacts.artifact_upload import IndexArtifactUploader
from mcp_server.artifacts.manifest_v2 import WorkspaceArtifactManifest
from mcp_server.artifacts.semantic_profiles import extract_semantic_profile_metadata
from mcp_server.core.errors import record_handled_error
from mcp_server.health.repo_status import build_health_row
from mcp_server.health.repository_readiness import ReadinessClassifier
from mcp_server.storage.multi_repo_manager import MultiRepositoryManager, RepositoryInfo


@dataclass
class RepoArtifactLifecycleResult:
    """Outcome for a single repository artifact operation."""

    repository_id: str
    repository_name: str
    action: str
    success: bool
    details: Dict[str, Any]
    error: Optional[str] = None


class MultiRepoArtifactCoordinator:
    """Coordinate artifact lifecycle across registered repositories."""

    def __init__(self, multi_repo_manager: Optional[MultiRepositoryManager] = None) -> None:
        self.multi_repo_manager = multi_repo_manager or MultiRepositoryManager()

    def _iter_repositories(
        self, repository_ids: Optional[Iterable[str]] = None
    ) -> List[RepositoryInfo]:
        repos = self.multi_repo_manager.list_repositories(active_only=True)
        if repository_ids is None:
            return [repo for repo in repos if repo.artifact_enabled]
        wanted = set(repository_ids)
        return [repo for repo in repos if repo.repository_id in wanted]

    def _read_local_profiles(
        self,
        repo_path: Path,
        index_location: Path | str | None = None,
        index_path: Path | None = None,
    ) -> List[str]:
        if index_path is not None:
            from .secure_export import SecureIndexExporter

            payload = SecureIndexExporter.read_generation_metadata(
                index_path, Path(index_location or index_path.parent)
            )
            return sorted(extract_semantic_profile_metadata(payload).keys())
        metadata_path = (
            Path(index_location) / ".index_metadata.json"
            if index_location is not None
            else repo_path / ".mcp-index" / ".index_metadata.json"
        )
        if not metadata_path.exists():
            return []
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            record_handled_error(__name__, exc)
            return []
        return sorted(extract_semantic_profile_metadata(payload).keys())

    def build_workspace_manifest(
        self, repository_ids: Optional[Iterable[str]] = None
    ) -> WorkspaceArtifactManifest:
        repos = self._iter_repositories(repository_ids)
        repositories = []
        for repo in repos:
            repositories.append(
                {
                    "repo_id": repo.repository_id,
                    "name": repo.name,
                    "path": str(repo.path),
                    "current_commit": repo.current_commit,
                    "last_published_commit": repo.last_published_commit,
                    "last_recovered_commit": repo.last_recovered_commit,
                    "artifact_backend": repo.artifact_backend,
                    "artifact_health": repo.artifact_health,
                    "available_semantic_profiles": repo.available_semantic_profiles or [],
                }
            )

        workspace_commit = "workspace"
        if repos:
            workspace_commit = "+".join(
                sorted(repo.current_commit or repo.repository_id for repo in repos)
            )
        return WorkspaceArtifactManifest(
            workspace_id="multi-repo-workspace",
            workspace_commit=workspace_commit,
            repositories=repositories,
        )

    def _read_artifact_metadata(
        self, repo: RepositoryInfo, index_location: Path | str | None = None
    ) -> Dict[str, Any]:
        base = (
            Path(index_location)
            if index_location is not None
            else Path(repo.index_location or repo.index_path.parent)
        )
        metadata_path = base / "artifact-metadata.json"
        if not metadata_path.exists():
            return {}
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception as exc:
            record_handled_error(__name__, exc)
            return {}

    def _build_validation_details(
        self,
        repo: RepositoryInfo,
        *,
        index_location: Path | str | None = None,
        validation_reasons: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        metadata_payload = metadata or self._read_artifact_metadata(repo, index_location)
        profiles = self._read_local_profiles(repo.path, index_location, repo.index_path)
        compatibility = (
            metadata_payload.get("compatibility", {}) if isinstance(metadata_payload, dict) else {}
        )
        schema_version = metadata_payload.get("schema_version") or compatibility.get(
            "schema_version"
        )

        validation_status = "missing"
        if metadata_payload:
            validation_status = "override_required" if validation_reasons else "passed"

        return {
            "validation_status": validation_status,
            "validation_reasons": validation_reasons or [],
            "validation": {
                "commit": metadata_payload.get("commit"),
                "branch": metadata_payload.get("tracked_branch") or metadata_payload.get("branch"),
                "checksum": metadata_payload.get("checksum"),
                "schema_version": schema_version,
                "semantic_profile_hash": metadata_payload.get("semantic_profile_hash"),
                "semantic_profiles": profiles
                or compatibility.get("available_semantic_profiles")
                or [],
            },
        }

    def _refuse_if_not_ready(
        self,
        repo: RepositoryInfo,
        *,
        action: str,
        allowed_states: set[str],
    ) -> RepoArtifactLifecycleResult | None:
        readiness = ReadinessClassifier.classify_registered(repo)
        if readiness.state.value in allowed_states:
            return None
        return RepoArtifactLifecycleResult(
            repository_id=repo.repository_id,
            repository_name=repo.name,
            action=action,
            success=False,
            details={
                "readiness": readiness.to_dict(),
                "artifact_backend": repo.artifact_backend,
                "artifact_health": repo.artifact_health,
                "last_published_commit": repo.last_published_commit,
                "last_recovered_commit": repo.last_recovered_commit,
            },
            error=readiness.remediation or readiness.state.value,
        )

    def get_workspace_status(
        self, repository_ids: Optional[Iterable[str]] = None
    ) -> List[RepoArtifactLifecycleResult]:
        results = []
        for repo in self._iter_repositories(repository_ids):
            readiness = ReadinessClassifier.classify_registered(repo)
            health_row = build_health_row(repo, readiness=readiness)
            details = {
                "current_commit": repo.current_commit,
                "last_published_commit": repo.last_published_commit,
                "last_recovered_commit": repo.last_recovered_commit,
                "artifact_backend": repo.artifact_backend,
                "artifact_health": repo.artifact_health,
                "available_semantic_profiles": repo.available_semantic_profiles or [],
            }
            details.update(health_row)
            details.update(self._build_validation_details(repo))
            results.append(
                RepoArtifactLifecycleResult(
                    repository_id=repo.repository_id,
                    repository_name=repo.name,
                    action="status",
                    success=True,
                    details=details,
                )
            )
        return results

    def publish_workspace(
        self, repository_ids: Optional[Iterable[str]] = None
    ) -> List[RepoArtifactLifecycleResult]:
        results = []
        for repo in self._iter_repositories(repository_ids):
            try:
                refused = self._refuse_if_not_ready(
                    repo,
                    action="publish",
                    allowed_states={"ready"},
                )
                if refused is not None:
                    results.append(refused)
                    continue

                index_location = Path(repo.index_location or repo.index_path.parent)
                archive_path = index_location / f"index-archive-{uuid4().hex}.tar.gz"
                uploader = IndexArtifactUploader()
                archive_path, checksum, size = uploader.compress_indexes(
                    archive_path,
                    secure=True,
                    repo_path=repo.path,
                    index_location=index_location,
                    index_path=repo.index_path,
                )
                metadata = uploader.create_metadata(
                    checksum,
                    size,
                    secure=True,
                    repo_id=repo.repository_id,
                    tracked_branch=repo.tracked_branch or repo.current_branch or "main",
                    commit=repo.current_commit or "unknown",
                    index_location=index_location,
                    index_path=repo.index_path,
                )
                try:
                    uploader.upload_direct(archive_path, metadata)
                    health = "prepared"
                except Exception:
                    health = "publish_failed"
                    raise
                profiles = self._read_local_profiles(repo.path, index_location, repo.index_path)
                self.multi_repo_manager.registry.update_artifact_state(
                    repo.repository_id,
                    last_published_commit=repo.current_commit,
                    artifact_backend="local_workspace",
                    artifact_health=health,
                    available_semantic_profiles=profiles,
                )
                validation_details = self._build_validation_details(
                    repo,
                    index_location=index_location,
                    metadata=metadata,
                )
                results.append(
                    RepoArtifactLifecycleResult(
                        repository_id=repo.repository_id,
                        repository_name=repo.name,
                        action="publish",
                        success=True,
                        details={
                            "profiles": profiles,
                            "published_commit": repo.current_commit,
                            "artifact_backend": "local_workspace",
                            "artifact_health": health,
                            "last_published_commit": repo.current_commit,
                            "last_recovered_commit": repo.last_recovered_commit,
                            "prepared_archive": "index-archive.tar.gz",
                            **validation_details,
                        },
                    )
                )
            except Exception as exc:
                results.append(
                    RepoArtifactLifecycleResult(
                        repository_id=repo.repository_id,
                        repository_name=repo.name,
                        action="publish",
                        success=False,
                        details={},
                        error=str(exc),
                    )
                )
        return results

    def fetch_workspace(
        self, repository_ids: Optional[Iterable[str]] = None
    ) -> List[RepoArtifactLifecycleResult]:
        results = []
        for repo in self._iter_repositories(repository_ids):
            try:
                refused = self._refuse_if_not_ready(
                    repo,
                    action="fetch",
                    allowed_states={"ready", "missing_index", "stale_commit"},
                )
                if refused is not None:
                    results.append(refused)
                    continue

                downloader = IndexArtifactDownloader(registry=self.multi_repo_manager.registry)
                index_location = Path(repo.index_location or repo.index_path.parent)
                download_workspace = tempfile.TemporaryDirectory(prefix="mcp-artifact-")
                output_dir = Path(download_workspace.name)
                try:
                    result = downloader.download_latest(
                        output_dir=output_dir,
                        backup=True,
                        repo_id=repo.repository_id,
                        repo_path=repo.path,
                        tracked_branch=repo.tracked_branch or repo.current_branch or "main",
                        target_commit=repo.current_commit,
                        index_location=index_location,
                        index_path=repo.index_path,
                    )
                finally:
                    download_workspace.cleanup()

                artifact = result.artifact or {}
                if not getattr(result, "installed_items", None):
                    raise RuntimeError("Artifact download admitted no new generation")
                repo = self.multi_repo_manager.registry.get(repo.repository_id)
                if repo is None:
                    raise RuntimeError("Repository was removed during artifact restore")
                profiles = self._read_local_profiles(
                    repo.path, repo.index_location, repo.index_path
                )
                health = ReadinessClassifier.classify_registered(repo).state.value
                if health != "ready":
                    raise RuntimeError(f"Artifact download did not hydrate {repo.index_path}")
                self.multi_repo_manager.registry.update_artifact_state(
                    repo.repository_id,
                    last_recovered_commit=artifact.get("workflow_run", {}).get("head_sha")
                    or artifact.get("head_sha")
                    or repo.current_commit,
                    artifact_backend=repo.artifact_backend or "github_actions",
                    artifact_health=health,
                    available_semantic_profiles=profiles,
                )
                recovered_commit = (
                    artifact.get("workflow_run", {}).get("head_sha")
                    or artifact.get("head_sha")
                    or repo.current_commit
                )
                validation_reasons = getattr(result, "validation_reasons", []) or []
                validation_details = self._build_validation_details(
                    repo,
                    index_location=index_location,
                    validation_reasons=validation_reasons,
                )
                results.append(
                    RepoArtifactLifecycleResult(
                        repository_id=repo.repository_id,
                        repository_name=repo.name,
                        action="fetch",
                        success=True,
                        details={
                            "profiles": profiles,
                            "artifact_name": artifact.get("name"),
                            "artifact_id": artifact.get("id"),
                            "artifact_backend": repo.artifact_backend or "github_actions",
                            "artifact_health": health,
                            "last_published_commit": repo.last_published_commit,
                            "last_recovered_commit": recovered_commit,
                            "recovered_commit": recovered_commit,
                            **validation_details,
                        },
                    )
                )
            except Exception as exc:
                results.append(
                    RepoArtifactLifecycleResult(
                        repository_id=repo.repository_id,
                        repository_name=repo.name,
                        action="fetch",
                        success=False,
                        details={},
                        error=str(exc),
                    )
                )
        return results

    def reconcile_workspace(
        self, repository_ids: Optional[Iterable[str]] = None
    ) -> List[RepoArtifactLifecycleResult]:
        results = []
        for repo in self._iter_repositories(repository_ids):
            has_local_index = Path(repo.index_path).exists()
            readiness = ReadinessClassifier.classify_registered(repo)
            if readiness.state.value == "wrong_branch":
                health = "wrong_branch"
            elif readiness.state.value == "stale_commit":
                health = "stale_commit"
            else:
                health = "ready" if has_local_index else "missing"
            self.multi_repo_manager.registry.update_artifact_state(
                repo.repository_id,
                artifact_health=health,
                available_semantic_profiles=self._read_local_profiles(
                    repo.path, repo.index_location
                ),
            )
            details = {
                "has_local_index": has_local_index,
                "artifact_health": health,
                "current_commit": repo.current_commit,
                "last_published_commit": repo.last_published_commit,
                "last_recovered_commit": repo.last_recovered_commit,
                "available_semantic_profiles": self._read_local_profiles(
                    repo.path, repo.index_location
                ),
                "readiness": readiness.to_dict(),
            }
            details.update(self._build_validation_details(repo))
            results.append(
                RepoArtifactLifecycleResult(
                    repository_id=repo.repository_id,
                    repository_name=repo.name,
                    action="reconcile",
                    success=True,
                    details=details,
                )
            )
        return results
