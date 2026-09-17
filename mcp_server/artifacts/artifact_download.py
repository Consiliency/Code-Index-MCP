"""Download and install Release assets and legacy GitHub Actions index artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import select
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp_server.config.settings import get_settings
from mcp_server.core.errors import UnknownSchemaVersionError, record_handled_error

from .attestation import Attestation, verify_attestation
from .freshness import FreshnessVerdict, verify_artifact_freshness
from .integrity_gate import (
    ArtifactIntegrityGateResult,
    validate_artifact_integrity,
    validate_required_metadata_fields,
)
from .manifest_v2 import validate_semantic_profile_hash
from .semantic_profiles import extract_semantic_profile_metadata

logger = logging.getLogger(__name__)
MAX_ACTIONS_ZIP_BYTES = 2 * 1024**3
MAX_ACTIONS_PAYLOAD_BYTES = 2 * 1024**3


@dataclass
class ArtifactDownloadResult:
    artifact: Optional[Dict[str, Any]] = None
    installed_items: Optional[List[str]] = None
    validation_reasons: Optional[List[str]] = None


class IndexArtifactDownloader:
    """Restore signed index payloads from GitHub Releases or legacy Actions artifacts."""

    def __init__(
        self,
        repo: Optional[str] = None,
        token: Optional[str] = None,
        *,
        index_manager=None,
        registry=None,
        repo_path: Path | str | None = None,
    ):
        self.repo = repo or (
            self._detect_repository(repo_path)
            if repo_path is not None
            else self._detect_repository()
        )
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.api_base = f"https://api.github.com/repos/{self.repo}"
        self._index_manager = index_manager
        self._registry = registry
        if not self.token:
            print("⚠️  No GitHub token found. Using gh CLI for authentication.")

    def _detect_repository(self, repo_path: Path | str | None = None) -> str:
        from .artifact_upload import IndexArtifactUploader

        return IndexArtifactUploader._detect_repository(self, repo_path)

    def list_artifacts(self, name_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        print("🔍 Fetching available artifacts...")
        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"/repos/{self.repo}/actions/artifacts",
                    "--paginate",
                    "--jq",
                    ".artifacts[]",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("gh CLI is required for artifact download flows") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Failed to list artifacts: {exc.stderr or exc}") from exc

        artifacts = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            artifact = json.loads(line)
            if not artifact.get("expired") and artifact["name"].startswith(
                ("index-", "mcp-index-")
            ):
                if not name_filter or name_filter in artifact["name"]:
                    artifact["artifact_backend"] = "github_actions"
                    artifacts.append(artifact)

        releases = subprocess.run(
            ["gh", "api", f"/repos/{self.repo}/releases", "--paginate", "--jq", ".[]"],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in releases.stdout.splitlines():
            if not line:
                continue
            release = json.loads(line)
            tag = release["tag_name"]
            assets = release.get("assets", [])
            names = {asset["name"] for asset in assets}
            if (
                release.get("draft")
                or not tag.startswith(("index-", "mcp-index-"))
                or (name_filter and name_filter not in tag)
                or "artifact-metadata.json" not in names
                or not any(name.endswith(".tar.gz") for name in names)
            ):
                continue
            artifacts.append(
                {
                    "id": f"release:{tag}",
                    "name": tag,
                    "release_tag": tag,
                    "artifact_backend": "github_release",
                    "created_at": release.get("published_at") or release["created_at"],
                    "size_in_bytes": sum(asset["size"] for asset in assets),
                    "workflow_run": {"head_sha": release.get("target_commitish") or "HEAD"},
                }
            )

        artifacts.sort(key=lambda item: item["created_at"], reverse=True)
        return artifacts

    def download_artifact(
        self,
        artifact_id: int,
        output_dir: Path,
        *,
        repo_id: Optional[str] = None,
        tracked_branch: Optional[str] = None,
        target_commit: Optional[str] = None,
        semantic_profile_hash: Optional[str] = None,
        allow_unsafe: bool = False,
    ) -> Path:
        print(f"📥 Downloading artifact {artifact_id}...")
        temp_dir = Path(tempfile.mkdtemp())
        try:
            with (temp_dir / "artifact.zip").open("xb") as zip_file:
                command = ["gh", "api", f"/repos/{self.repo}/actions/artifacts/{artifact_id}/zip"]
                deadline = time.monotonic() + 300
                with subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                ) as process:
                    assert process.stdout is not None
                    try:
                        received = 0
                        while True:
                            remaining = deadline - time.monotonic()
                            if (
                                remaining <= 0
                                or not select.select([process.stdout], [], [], remaining)[0]
                            ):
                                raise subprocess.TimeoutExpired(command, 300)
                            chunk = os.read(process.stdout.fileno(), 1024 * 1024)
                            if not chunk:
                                break
                            received += len(chunk)
                            if received > MAX_ACTIONS_ZIP_BYTES:
                                raise ValueError("Actions artifact ZIP exceeds the download limit")
                            zip_file.write(chunk)
                        returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
                        if returncode:
                            raise subprocess.CalledProcessError(returncode, command)
                    except BaseException:
                        process.kill()
                        process.wait()
                        raise
            self._extract_actions_artifact_zip(temp_dir)
            return self._restore_downloaded_payload(
                temp_dir,
                output_dir,
                repo_id=repo_id,
                tracked_branch=tracked_branch,
                target_commit=target_commit,
                semantic_profile_hash=semantic_profile_hash,
                allow_unsafe=allow_unsafe,
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _extract_actions_artifact_zip(self, temp_dir: Path) -> None:
        archive_path = temp_dir / "artifact.zip"
        if archive_path.stat().st_size > MAX_ACTIONS_ZIP_BYTES:
            raise ValueError("Actions artifact ZIP exceeds the download limit")
        with zipfile.ZipFile(archive_path, "r") as zip_ref:
            members = zip_ref.infolist()
            names = [member.filename for member in members]
            archives = [
                name for name in names if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.tar\.gz", name)
            ]
            if (
                not 2 <= len(members) <= 4
                or len(set(names)) != len(names)
                or len(archives) != 1
                or "artifact-metadata.json" not in names
            ):
                raise ValueError("Actions artifact ZIP has an ambiguous payload")
            limits = {
                archives[0]: MAX_ACTIONS_PAYLOAD_BYTES,
                "artifact-metadata.json": 1024**2,
                "artifact-metadata.json.attestation.jsonl": 4 * 1024**2,
                archives[0] + ".sha256": 1024,
            }
            if sum(member.file_size for member in members) > MAX_ACTIONS_PAYLOAD_BYTES:
                raise ValueError("Actions artifact ZIP exceeds the expanded-size limit")
            for member in members:
                if (
                    member.filename not in limits
                    or member.file_size > limits.get(member.filename, 0)
                    or member.is_dir()
                    or member.flag_bits & 1
                    or stat.S_IFMT(member.external_attr >> 16) not in {0, stat.S_IFREG}
                    or (temp_dir / member.filename).exists()
                    or (temp_dir / member.filename).is_symlink()
                ):
                    raise ValueError("Actions artifact ZIP contains an invalid member")
            for member in members:
                with (
                    zip_ref.open(member) as source,
                    (temp_dir / member.filename).open("xb") as target,
                ):
                    shutil.copyfileobj(source, target, length=1024 * 1024)

    def download_release_artifact(
        self,
        release_tag: str,
        output_dir: Path,
        *,
        repo_id: Optional[str] = None,
        tracked_branch: Optional[str] = None,
        target_commit: Optional[str] = None,
        semantic_profile_hash: Optional[str] = None,
        allow_unsafe: bool = False,
    ) -> Path:
        print(f"📥 Downloading release artifact {release_tag}...")
        temp_dir = Path(tempfile.mkdtemp())
        try:
            subprocess.run(
                [
                    "gh",
                    "release",
                    "download",
                    release_tag,
                    "--repo",
                    self.repo,
                    "--dir",
                    str(temp_dir),
                    "--clobber",
                ],
                check=True,
            )
            return self._restore_downloaded_payload(
                temp_dir,
                output_dir,
                repo_id=repo_id,
                tracked_branch=tracked_branch,
                target_commit=target_commit,
                semantic_profile_hash=semantic_profile_hash,
                allow_unsafe=allow_unsafe,
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _locate_download_payload(
        self, payload_dir: Path
    ) -> Tuple[Path, Path, Optional[Path], Optional[Path]]:
        files = list(payload_dir.iterdir())
        archives = [file for file in files if file.name.endswith(".tar.gz")]
        checksums = [file for file in files if file.name.endswith(".sha256")]
        attestations = [file for file in files if file.name.endswith(".attestation.jsonl")]
        metadata_path = payload_dir / "artifact-metadata.json"
        if len(archives) != 1 or len(checksums) > 1 or len(attestations) > 1:
            raise ValueError("Artifact payload has missing or ambiguous archive/sidecars")
        if not metadata_path.is_file():
            raise ValueError("Artifact metadata file is required but missing")
        selected = [*archives, metadata_path, *checksums, *attestations]
        if any(file.is_symlink() or not file.is_file() for file in selected):
            raise ValueError("Artifact payload requires regular files")
        return (
            archives[0],
            metadata_path,
            next(iter(checksums), None),
            next(iter(attestations), None),
        )

    def _restore_downloaded_payload(
        self,
        payload_dir: Path,
        output_dir: Path,
        *,
        repo_id: Optional[str] = None,
        tracked_branch: Optional[str] = None,
        target_commit: Optional[str] = None,
        semantic_profile_hash: Optional[str] = None,
        allow_unsafe: bool = False,
    ) -> Path:
        archive_path, metadata_path, checksum_path, attestation_path = (
            self._locate_download_payload(payload_dir)
        )

        att = Attestation(
            bundle_url="",
            bundle_path=attestation_path,
            subject_digest="",
            signed_at=datetime.now(timezone.utc),
        )
        # The signed metadata binds identity and the archive checksum together.
        verify_attestation(metadata_path, att, expected_repo=self.repo, gh_cmd="gh")
        metadata_bytes = metadata_path.read_bytes()
        metadata = json.loads(metadata_bytes)
        delta_base = metadata.get("delta_from")
        if delta_base:
            probe = subprocess.run(
                ["gh", "release", "view", delta_base, "--repo", self.repo],
                capture_output=True,
                text=True,
            )
            if probe.returncode != 0:
                logger.warning(
                    "Delta base release %r not found in %s; treating artifact as full (delta_from cleared)",
                    delta_base,
                    self.repo,
                )
                metadata = dict(metadata)
                metadata["delta_from"] = None
        gate_result = self._run_integrity_gate(metadata, archive_path, checksum_path)
        if gate_result.manifest_v2_validated:
            print("✅ Manifest v2 verified")

        identity_reasons = self.validate_artifact_identity(
            metadata,
            repo_id=repo_id,
            tracked_branch=tracked_branch,
            target_commit=target_commit,
            semantic_profile_hash=semantic_profile_hash,
        )
        if identity_reasons and not allow_unsafe:
            raise ValueError("Artifact identity validation failed: " + "; ".join(identity_reasons))
        if identity_reasons:
            logger.warning(
                "Unsafe artifact identity override accepted: %s",
                "; ".join(identity_reasons),
            )

        compatible, issues = self.check_compatibility(metadata)
        if not compatible:
            raise ValueError("Artifact compatibility validation failed: " + "; ".join(issues))

        print("📦 Extracting index files...")
        if output_dir.is_symlink():
            raise ValueError("Artifact output must not be a symbolic link")
        output_dir.mkdir(parents=True, exist_ok=True)
        extracted = Path(tempfile.mkdtemp(prefix="verified-", dir=output_dir))
        with tarfile.open(archive_path, "r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                if not self._validate_tar_member(member, extracted) or Path(member.name).parts == (
                    "artifact-metadata.json",
                ):
                    raise ValueError(f"Unsafe archive member blocked: {member.name}")
            tar.extractall(
                extracted, members=members
            )  # nosec B202 - fresh directory, regular members only

        with (extracted / "artifact-metadata.json").open("xb") as handle:
            handle.write(metadata_bytes)
        return extracted

    def _calculate_checksum(self, file_path: Path) -> str:
        sha256 = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def check_compatibility(self, metadata: Dict[str, Any]) -> Tuple[bool, List[str]]:
        from mcp_server.storage.sqlite_store import SQLiteStore

        supported_schemas = tuple(
            str(version) for version in range(1, SQLiteStore.SCHEMA_VERSION + 1)
        )
        issues = []
        compatibility = metadata.get("compatibility", {})
        artifact_model = compatibility.get("embedding_model")
        artifact_schema = compatibility.get("schema_version")
        artifact_profiles = extract_semantic_profile_metadata(compatibility)

        required_schema = os.environ.get("INDEX_SCHEMA_VERSION")
        if not required_schema:
            local_db = Path("code_index.db")
            if local_db.exists():
                conn = None
                try:
                    conn = sqlite3.connect(str(local_db))
                    required_schema = str(
                        conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                    )
                except Exception as exc:
                    record_handled_error(__name__, exc)
                    required_schema = None
                finally:
                    if conn is not None:
                        conn.close()
        if not required_schema:
            required_schema = str(SQLiteStore.SCHEMA_VERSION)
        if artifact_schema not in supported_schemas:
            raise UnknownSchemaVersionError(
                f"Artifact schema version {artifact_schema!r} is unknown; "
                f"supported: {supported_schemas}"
            )
        if required_schema not in supported_schemas:
            raise UnknownSchemaVersionError(
                f"Required schema version {required_schema!r} is unknown"
            )
        if int(artifact_schema) > int(required_schema):
            issues.append("Artifact schema is newer than the required SQLite schema")
        # Older database versions are upgraded by SQLiteStore on admission.

        if artifact_profiles:
            try:
                requested_profiles = self._get_requested_semantic_profiles()
                if requested_profiles:
                    overlap = sorted(set(requested_profiles).intersection(artifact_profiles))
                    compatible_profiles = []
                    mismatched_profiles = []
                    for profile_id in overlap:
                        expected = requested_profiles.get(profile_id)
                        discovered = artifact_profiles[profile_id].get(
                            "compatibility_fingerprint"
                        ) or artifact_profiles[profile_id].get("compatibility_hash")
                        if expected and discovered and expected != discovered:
                            mismatched_profiles.append(profile_id)
                        else:
                            compatible_profiles.append(profile_id)

                    if not compatible_profiles:
                        if mismatched_profiles:
                            issues.append(
                                "Semantic profile fingerprint mismatch for profiles: "
                                + ", ".join(mismatched_profiles)
                            )
                        else:
                            issues.append(
                                "No compatible semantic profiles found: artifact has "
                                + ", ".join(sorted(artifact_profiles))
                                + "; local config expects "
                                + ", ".join(sorted(requested_profiles))
                            )
            except Exception as exc:
                record_handled_error(__name__, exc)
                issues.append("Local semantic profile configuration is unavailable")
        elif artifact_model:
            try:
                current_model = get_settings().semantic_embedding_model
                if artifact_model != current_model:
                    issues.append(
                        f"Embedding model mismatch: artifact={artifact_model}, current={current_model}"
                    )
            except Exception as exc:
                record_handled_error(__name__, exc)
                issues.append("Local embedding model configuration is unavailable")

        return len(issues) == 0, issues

    def _get_requested_semantic_profiles(self) -> Dict[str, Optional[str]]:
        """Return locally configured semantic profile fingerprints, if available."""
        try:
            settings = get_settings()
            profiles = settings.get_semantic_profiles_config()
            requested: Dict[str, Optional[str]] = {}
            for profile_id, payload in profiles.items():
                if not isinstance(profile_id, str) or not isinstance(payload, dict):
                    continue
                requested[profile_id] = str(payload.get("compatibility_fingerprint", "")) or None

            if any(value for value in requested.values()):
                return requested

            from .semantic_profiles import SemanticProfileRegistry

            registry = SemanticProfileRegistry.from_raw(
                profiles,
                settings.get_semantic_default_profile(),
                tool_version=settings.app_version,
            )
            return {
                profile_id: profile.compatibility_fingerprint
                for profile_id, profile in registry.list().items()
            }
        except Exception as exc:
            record_handled_error(__name__, exc)
            return {}

    def find_best_artifact(self, artifacts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        print("\n🔎 Finding best compatible artifact...")
        promoted = [artifact for artifact in artifacts if "-promoted" in artifact["name"]]
        if promoted:
            return promoted[0]
        default_branch = [
            artifact
            for artifact in artifacts
            if artifact["name"].startswith("mcp-index-")
            and not artifact["name"].startswith("mcp-index-pr-")
        ]
        if default_branch:
            return default_branch[0]
        return artifacts[0] if artifacts else None

    def find_recovery_artifact(
        self,
        artifacts: List[Dict[str, Any]],
        branch: Optional[str],
        commit: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        selected = artifacts
        if branch:
            selected = [
                artifact
                for artifact in selected
                if branch in artifact.get("name", "")
                or artifact.get("workflow_run", {}).get("head_branch") == branch
            ]
        if commit:
            short_commit = commit[:8]
            selected = [
                artifact
                for artifact in selected
                if commit in artifact.get("name", "")
                or short_commit in artifact.get("name", "")
                or artifact.get("workflow_run", {}).get("head_sha", "") == commit
            ]
        if not selected:
            return None
        promoted = [artifact for artifact in selected if "-promoted" in artifact["name"]]
        return promoted[0] if promoted else selected[0]

    def validate_artifact_identity(
        self,
        metadata: Dict[str, Any],
        *,
        repo_id: Optional[str] = None,
        tracked_branch: Optional[str] = None,
        target_commit: Optional[str] = None,
        semantic_profile_hash: Optional[str] = None,
    ) -> List[str]:
        """Validate artifact identity metadata against expected repository state."""
        from mcp_server.storage.sqlite_store import SQLiteStore

        reasons = validate_required_metadata_fields(metadata)
        actual_repo_id = metadata.get("repo_id")
        actual_branch = metadata.get("tracked_branch") or metadata.get("branch")
        actual_commit = metadata.get("commit") or metadata.get("target_commit")
        actual_schema = metadata.get("schema_version") or metadata.get("compatibility", {}).get(
            "schema_version"
        )
        actual_profile_hash = metadata.get("semantic_profile_hash")

        if repo_id and actual_repo_id != repo_id:
            reasons.append(f"repo_id mismatch: expected={repo_id}, actual={actual_repo_id}")
        if tracked_branch and actual_branch != tracked_branch:
            reasons.append(
                f"tracked_branch mismatch: expected={tracked_branch}, actual={actual_branch}"
            )
        if target_commit and actual_commit != target_commit:
            reasons.append(f"commit mismatch: expected={target_commit}, actual={actual_commit}")
        if semantic_profile_hash and actual_profile_hash != semantic_profile_hash:
            reasons.append(
                "semantic_profile_hash mismatch: "
                f"expected={semantic_profile_hash}, actual={actual_profile_hash}"
            )
        if actual_profile_hash and not validate_semantic_profile_hash(str(actual_profile_hash)):
            reasons.append(f"malformed semantic_profile_hash: {actual_profile_hash}")
        if actual_schema is not None:
            if str(actual_schema) not in {
                str(version) for version in range(1, SQLiteStore.SCHEMA_VERSION + 1)
            }:
                reasons.append(f"unknown schema_version: {actual_schema}")

        manifest = metadata.get("manifest_v2")
        if isinstance(manifest, dict):
            manifest_branch = manifest.get("tracked_branch") or manifest.get("branch")
            checks = [
                ("manifest repo_id", repo_id, manifest.get("repo_id")),
                ("manifest tracked_branch", tracked_branch, manifest_branch),
                ("manifest commit", target_commit, manifest.get("commit")),
                (
                    "manifest semantic_profile_hash",
                    semantic_profile_hash,
                    manifest.get("semantic_profile_hash"),
                ),
            ]
            for label, expected, actual in checks:
                if expected and actual != expected:
                    reasons.append(f"{label} mismatch: expected={expected}, actual={actual}")

        return reasons

    def _run_integrity_gate(
        self,
        metadata: Dict[str, Any],
        archive_path: Path,
        checksum_path: Optional[Path],
    ) -> ArtifactIntegrityGateResult:
        gate_result = validate_artifact_integrity(
            metadata=metadata,
            archive_path=archive_path,
            checksum_path=checksum_path,
        )
        if not gate_result.passed:
            raise ValueError("Artifact integrity gate failed: " + "; ".join(gate_result.reasons))
        print("✅ Integrity gate passed")
        return gate_result

    def _validate_metadata(self, metadata: Dict[str, Any]) -> Optional[str]:
        reasons = validate_required_metadata_fields(metadata)
        return reasons[0] if reasons else None

    def _is_within_directory(self, base_dir: Path, candidate_path: Path) -> bool:
        try:
            candidate_path.resolve().relative_to(base_dir.resolve())
            return True
        except ValueError:
            return False

    def _validate_tar_member(self, member: tarfile.TarInfo, extraction_dir: Path) -> bool:
        if not (member.isfile() or member.isdir()) or Path(member.name).is_absolute():
            return False
        target_path = extraction_dir / member.name
        if not self._is_within_directory(extraction_dir, target_path):
            return False
        return True

    def install_indexes(
        self,
        source_dir: Path,
        index_location: Path | str | None = None,
        index_path: Path | str | None = None,
        backup: bool = True,
    ) -> List[str]:
        """Hydrate a fresh staging destination; never replace a live generation.

        The legacy backup argument is retained for caller compatibility. Existing
        resources require generation publication, not an in-place backup/restore.
        """
        index_root = Path(index_location) if index_location is not None else Path(".mcp-index")
        target_db = Path(index_path) if index_path is not None else index_root / "current.db"
        install_map = {
            "current.db": target_db,
            "code_index.db": target_db,
            ".index_metadata.json": index_root / ".index_metadata.json",
            "artifact-metadata.json": index_root / "artifact-metadata.json",
            "vector_index.qdrant": index_root / "vector_index.qdrant",
        }
        sources = [item for item in source_dir.iterdir() if item.name in install_map]
        databases = [item for item in sources if item.name in {"current.db", "code_index.db"}]
        if len(databases) != 1 or not databases[0].is_file():
            raise ValueError("Artifact staging requires exactly one SQLite database")
        destinations = set(install_map.values()) | {
            Path(f"{target_db}-wal"),
            Path(f"{target_db}-shm"),
        }
        if any(path.exists() or path.is_symlink() for path in destinations):
            raise FileExistsError("Artifact installation requires an unused staging destination")
        for item in sources:
            if item.is_symlink() or (
                item.is_dir() and any(child.is_symlink() for child in item.rglob("*"))
            ):
                raise ValueError("Artifact staging does not accept symbolic links")

        installed_items = []
        for item in sources:
            dest = install_map[item.name]
            dest.parent.mkdir(parents=True, exist_ok=True)
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                with item.open("rb") as reader, dest.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
                    writer.flush()
                    os.fsync(writer.fileno())
            installed_items.append(str(dest))
        return installed_items

    def download_selected_artifact(
        self,
        artifact: Dict[str, Any],
        *,
        output_dir: Path,
        backup: bool = True,
        repo_id: Optional[str] = None,
        repo_path: Path | str | None = None,
        tracked_branch: Optional[str] = None,
        target_commit: Optional[str] = None,
        index_location: Path | str | None = None,
        index_path: Path | str | None = None,
        semantic_profile_hash: Optional[str] = None,
        allow_unsafe: bool = False,
    ) -> ArtifactDownloadResult:
        if allow_unsafe and repo_id is not None:
            raise ValueError(
                "Registered generations require artifact identity and freshness verification"
            )
        try:
            release = artifact.get("artifact_backend") == "github_release"
            download = self.download_release_artifact if release else self.download_artifact
            extracted_dir = download(
                artifact["release_tag"] if release else artifact["id"],
                output_dir,
                repo_id=repo_id,
                tracked_branch=tracked_branch,
                target_commit=target_commit,
                semantic_profile_hash=semantic_profile_hash,
                allow_unsafe=allow_unsafe,
            )
        except (subprocess.CalledProcessError, urllib.error.URLError, RuntimeError) as exc:
            logger.warning(
                "GitHub outage detected, keeping local index (artifact download failed: %s)", exc
            )
            return ArtifactDownloadResult(artifact=artifact, installed_items=[])

        # Freshness gate — between metadata-load and install_indexes.
        meta_path = extracted_dir / "artifact-metadata.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception as exc:
                record_handled_error(__name__, exc)
                meta = {}
        else:
            meta = {}

        max_age_days = int(os.environ.get("MCP_ARTIFACT_MAX_AGE_DAYS", "14"))
        head_commit = artifact.get("workflow_run", {}).get("head_sha", "HEAD")
        if target_commit:
            head_commit = target_commit
        verdict = verify_artifact_freshness(meta, head_commit, max_age_days, repo_path=repo_path)
        rejected_reasons: List[str] = []
        if verdict is not FreshnessVerdict.FRESH:
            rejected_reasons.append(f"freshness verdict: {verdict.value}")
            if not allow_unsafe:
                raise ValueError(
                    "Artifact freshness validation failed: " + "; ".join(rejected_reasons)
                )
            logger.warning(
                "Unsafe artifact freshness override accepted: %s",
                "; ".join(rejected_reasons),
            )

        if repo_id is not None:
            if rejected_reasons:
                raise ValueError(
                    "Mismatched artifacts cannot be admitted as registered generations"
                )
            installed_items = self._install_verified_generation(
                repo_id, extracted_dir, head_commit, repo_path
            )
        else:
            installed_items = self.install_indexes(
                extracted_dir,
                index_location=index_location,
                index_path=index_path,
                backup=backup,
            )
        return ArtifactDownloadResult(
            artifact=artifact,
            installed_items=installed_items,
            validation_reasons=rejected_reasons,
        )

    def _install_verified_generation(
        self, repo_id: str, extracted: Path, commit: str, repo_path
    ) -> List[str]:
        from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher
        from mcp_server.storage.git_index_manager import GitAwareIndexManager
        from mcp_server.storage.repository_registry import RepositoryRegistry

        manager = self._index_manager
        owned = manager is None
        dispatcher = None
        try:
            if owned:
                registry = self._registry if self._registry is not None else RepositoryRegistry()
                dispatcher = EnhancedDispatcher(
                    enable_advanced_features=False,
                    use_plugin_factory=True,
                    semantic_search_enabled=get_settings().semantic_search_enabled,
                    memory_aware=False,
                    multi_repo_enabled=False,
                )
                manager = GitAwareIndexManager(registry, dispatcher)
            info = manager.registry.get(repo_id)
            if info is None or (
                repo_path is not None and Path(info.path).resolve() != Path(repo_path).resolve()
            ):
                raise ValueError("Artifact destination is not the registered repository")
            result = manager.restore_verified_artifact(repo_id, extracted, expected_commit=commit)
            if result.action != "full_index":
                raise ValueError(result.error or "Artifact generation was not admitted")
            return [str(manager.registry.get(repo_id).index_path)]
        finally:
            if owned:
                try:
                    if dispatcher is not None:
                        dispatcher.shutdown()
                finally:
                    if manager is not None and manager.store_registry is not None:
                        manager.store_registry.shutdown()

    def download_latest(
        self,
        *,
        output_dir: Path,
        backup: bool = True,
        full_only: bool = False,
        **kwargs: Any,
    ) -> ArtifactDownloadResult:
        artifacts = self.list_artifacts()
        if full_only:
            artifacts = [a for a in artifacts if not re.search(r"-delta(?:-|$)", a.get("name", ""))]
        best = self.find_best_artifact(artifacts)
        if not best:
            raise RuntimeError("No compatible artifacts found")
        print(f"\n✅ Selected: {best['name']}")
        return self.download_selected_artifact(best, output_dir=output_dir, backup=backup, **kwargs)

    def recover(
        self,
        *,
        branch: Optional[str],
        commit: Optional[str],
        output_dir: Path,
        backup: bool = True,
        **kwargs: Any,
    ) -> ArtifactDownloadResult:
        artifacts = self.list_artifacts()
        selected = self.find_recovery_artifact(artifacts, branch=branch, commit=commit)
        if not selected:
            details = []
            if branch:
                details.append(f"branch={branch}")
            if commit:
                details.append(f"commit={commit}")
            raise RuntimeError(
                "No matching artifact found for recovery criteria"
                + (f" ({', '.join(details)})" if details else "")
            )
        print(f"\n✅ Recovery artifact selected: {selected['name']}")
        return self.download_selected_artifact(
            selected, output_dir=output_dir, backup=backup, **kwargs
        )


def format_artifact_table(artifacts: List[Dict[str, Any]]) -> None:
    if not artifacts:
        print("No artifacts found.")
        return
    print("\n📦 Available Index Artifacts:")
    print("=" * 80)
    print(f"{'Name':<40} {'Size':>10} {'Created':<20} {'Promoted'}")
    print("-" * 80)
    for artifact in artifacts[:10]:
        name = artifact["name"]
        if len(name) > 40:
            name = name[:37] + "..."
        size_mb = artifact["size_in_bytes"] / 1024 / 1024
        created = datetime.fromisoformat(artifact["created_at"].replace("Z", "+00:00"))
        created_str = created.strftime("%Y-%m-%d %H:%M")
        promoted = "✓" if "-promoted" in artifact["name"] else ""
        print(f"{name:<40} {size_mb:>8.1f}MB {created_str:<20} {promoted}")
    if len(artifacts) > 10:
        print(f"\n... and {len(artifacts) - 10} more artifacts")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download index files from GitHub Actions Artifacts"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    list_parser = subparsers.add_parser("list", help="List available artifacts")
    list_parser.add_argument("--filter", help="Filter artifact names")

    download_parser = subparsers.add_parser("download", help="Download and install indexes")
    download_parser.add_argument("--artifact-id", type=int, help="Specific artifact ID to download")
    download_parser.add_argument(
        "--latest", action="store_true", help="Download latest compatible artifact"
    )
    download_parser.add_argument(
        "--full-only",
        action="store_true",
        help="Only allow full artifacts (skip delta artifacts)",
    )
    download_parser.add_argument(
        "--no-backup", action="store_true", help="Skip backup of existing indexes"
    )
    download_parser.add_argument("--output-dir", default=".", help="Output directory")

    info_parser = subparsers.add_parser("info", help="Show artifact information")
    info_parser.add_argument("artifact_id", type=int, help="Artifact ID")

    recover_parser = subparsers.add_parser(
        "recover", help="Recover index from artifact matching branch/commit"
    )
    recover_parser.add_argument("--branch", help="Target branch name")
    recover_parser.add_argument("--commit", help="Target commit SHA")
    recover_parser.add_argument(
        "--no-backup", action="store_true", help="Skip backup of existing indexes"
    )
    recover_parser.add_argument("--output-dir", default=".", help="Output directory")

    parser.add_argument("--repo", help="GitHub repository (owner/name)")
    return parser


def run_cli(args: argparse.Namespace) -> int:
    downloader = IndexArtifactDownloader(repo=args.repo)
    if args.command == "list":
        artifacts = downloader.list_artifacts(name_filter=args.filter)
        format_artifact_table(artifacts)
        if artifacts:
            print(f"\nTotal: {len(artifacts)} artifacts")
        return 0

    if args.command == "download":
        output_dir = Path(args.output_dir) / "artifact_download"
        output_dir.mkdir(exist_ok=True)
        try:
            if args.artifact_id:
                artifact = next(
                    (
                        item
                        for item in downloader.list_artifacts()
                        if item["id"] == args.artifact_id
                    ),
                    {"id": args.artifact_id, "name": str(args.artifact_id)},
                )
                downloader.download_selected_artifact(
                    artifact, output_dir=output_dir, backup=not args.no_backup
                )
            elif args.latest:
                downloader.download_latest(
                    output_dir=output_dir,
                    backup=not args.no_backup,
                    full_only=args.full_only,
                )
            else:
                print("❌ Specify --artifact-id or --latest")
                return 1
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)
        return 0

    if args.command == "info":
        artifacts = downloader.list_artifacts()
        artifact = next((item for item in artifacts if item["id"] == args.artifact_id), {})
        if not artifact:
            print(f"❌ Artifact {args.artifact_id} not found")
            return 1
        print("\n📋 Artifact Information:")
        print(f"   Name: {artifact['name']}")
        print(f"   ID: {artifact['id']}")
        print(f"   Size: {artifact['size_in_bytes'] / 1024 / 1024:.1f} MB")
        print(f"   Created: {artifact['created_at']}")
        print(f"   Expires: {artifact['expires_at']}")
        return 0

    if args.command == "recover":
        if not args.branch and not args.commit:
            print("❌ Specify at least one of --branch or --commit")
            return 1
        output_dir = Path(args.output_dir) / "artifact_recovery"
        output_dir.mkdir(exist_ok=True)
        try:
            downloader.recover(
                branch=args.branch,
                commit=args.commit,
                output_dir=output_dir,
                backup=not args.no_backup,
            )
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)
        return 0

    print(build_parser().format_help())
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    try:
        return run_cli(args)
    except Exception as exc:
        print(f"\n❌ Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
