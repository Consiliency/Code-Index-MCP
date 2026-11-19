"""
Portable Index Discovery for MCP
Automatically detects and uses indexes created by mcp-index-kit
Enhanced with multi-path discovery to fix test environment issues
"""

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from mcp_server.artifacts.profile_hydration import ProfileHydrationCoordinator
from mcp_server.artifacts.provider_factory import ArtifactProviderFactory
from mcp_server.storage.schema_errors import SchemaMismatchError

if TYPE_CHECKING:
    from mcp_server.storage.index_manager import IndexManifest

logger = logging.getLogger(__name__)


def _is_within_directory(base_dir: Path, candidate_path: Path) -> bool:
    """Return whether candidate_path resolves under base_dir."""
    try:
        candidate_path.resolve().relative_to(base_dir.resolve())
        return True
    except ValueError:
        return False


def _validate_tar_member(member: tarfile.TarInfo, extraction_dir: Path) -> bool:
    """Return whether a tar member is safe to extract into extraction_dir."""
    target_path = extraction_dir / member.name
    if not _is_within_directory(extraction_dir, target_path):
        logger.error("Blocked unsafe tar member path: %s", member.name)
        return False

    if member.issym():
        if not member.linkname:
            logger.error("Blocked symlink member without link target: %s", member.name)
            return False

        link_target = target_path.parent / member.linkname
        if not _is_within_directory(extraction_dir, link_target):
            logger.error(
                "Blocked unsafe symlink target in tar member: %s -> %s",
                member.name,
                member.linkname,
            )
            return False

    if member.islnk():
        if not member.linkname:
            logger.error("Blocked hardlink member without link target: %s", member.name)
            return False

        # Hardlink targets are interpreted relative to archive root.
        link_target = extraction_dir / member.linkname
        if not _is_within_directory(extraction_dir, link_target):
            logger.error(
                "Blocked unsafe hardlink target in tar member: %s -> %s",
                member.name,
                member.linkname,
            )
            return False

    if member.isdev():
        logger.error("Blocked device node in tar member: %s", member.name)
        return False

    return True


class IndexDiscovery:
    """Discovers and manages portable MCP indexes in repositories"""

    def __init__(
        self,
        workspace_root: Path,
        storage_strategy: Optional[str] = None,
        enable_multi_path: bool = True,
    ):
        self.workspace_root = Path(workspace_root)
        self.index_dir = self.workspace_root / ".mcp-index"
        self.config_file = self.workspace_root / ".mcp-index.json"
        self.metadata_file = self.index_dir / ".index_metadata.json"
        self.enable_multi_path = enable_multi_path

        # Import modules for multi-path support
        from mcp_server.config.index_paths import IndexPathConfig
        from mcp_server.storage.index_manager import IndexManager

        # Initialize multi-path configuration
        self.path_config = IndexPathConfig() if enable_multi_path else None

        # Determine storage strategy
        if storage_strategy is None:
            config = self.get_index_config()
            storage_strategy = config.get("storage_strategy", "inline") if config else "inline"

        self.storage_strategy = storage_strategy
        self.index_manager = IndexManager(storage_strategy=storage_strategy)

    def is_index_enabled(self) -> bool:
        """Check if MCP indexing is enabled for this repository"""
        # Check environment variable first
        if os.getenv("MCP_INDEX_ENABLED", "").lower() == "false":
            return False

        # Check config file
        if self.config_file.exists():
            try:
                with open(self.config_file) as f:
                    config = json.load(f)
                    return config.get("enabled", True)
            except Exception as e:
                logger.warning(f"Failed to read MCP config: {e}")

        # Check if .mcp-index directory exists
        return self.index_dir.exists()

    def get_index_config(self) -> Optional[Dict[str, Any]]:
        """Get the MCP index configuration"""
        if not self.config_file.exists():
            return None

        try:
            with open(self.config_file) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load index config: {e}")
            return None

    def get_index_metadata(self) -> Optional[Dict[str, Any]]:
        """Get metadata about the current index"""
        if not self.metadata_file.exists():
            return None

        try:
            with open(self.metadata_file) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load index metadata: {e}")
            return None

    def _read_index_metadata_for_path(self, index_path: Optional[Path]) -> Optional[Dict[str, Any]]:
        """Read index metadata colocated with a discovered SQLite index."""
        if index_path:
            metadata_path = index_path.parent / ".index_metadata.json"
            if metadata_path.exists():
                try:
                    return json.loads(metadata_path.read_text())
                except Exception as exc:
                    logger.warning("Failed to load index metadata at %s: %s", metadata_path, exc)

        return self.get_index_metadata()

    def get_profile_hydration_status(
        self,
        requested_profiles: Optional[Dict[str, Optional[str]]] = None,
        *,
        index_path: Optional[Path] = None,
        branch: Optional[str] = None,
        commit: Optional[str] = None,
        lexical_available: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Return profile hydration state using lexical-first fallback semantics."""
        effective_index_path = index_path if index_path is not None else self.get_local_index_path()
        effective_lexical = (
            lexical_available if lexical_available is not None else effective_index_path is not None
        )

        metadata = self._read_index_metadata_for_path(effective_index_path)
        coordinator = ProfileHydrationCoordinator()
        report = coordinator.from_index_metadata(
            requested_profiles=requested_profiles or {},
            index_metadata=metadata,
            lexical_available=effective_lexical,
            branch=branch,
            commit=commit,
        )
        return report.to_dict()

    def read_index_manifest(self, index_path: Path) -> Optional["IndexManifest"]:
        """Load manifest for a discovered index if it exists."""
        return self.index_manager.read_index_manifest(index_path)

    def write_index_manifest(
        self,
        index_path: Path,
        schema_version: str,
        embedding_model: str,
        creation_commit: Optional[str] = None,
    ) -> Path:
        """Persist manifest metadata next to a SQLite index."""
        commit = creation_commit or self._get_current_commit()
        return self.index_manager.write_index_manifest(
            index_path=index_path,
            schema_version=schema_version,
            embedding_model=embedding_model,
            creation_commit=commit,
        )

    def get_local_index_path(
        self,
        requested_schema_version: Optional[str] = None,
        requested_embedding_model: Optional[str] = None,
        strict_compatibility: bool = False,
    ) -> Optional[Path]:
        """Get path to local SQLite index if it exists."""
        if not self.is_index_enabled():
            return None

        require_selection = bool(
            requested_schema_version is not None or requested_embedding_model is not None
        )
        candidates: List[Dict[str, Any]] = []
        search_paths: List[Path] = []

        def _record_candidate(db_path: Optional[Path]) -> Optional[Path]:
            if not db_path or not db_path.exists():
                return None

            if not self._validate_sqlite_index(db_path):
                return None

            if require_selection:
                candidates.append({"path": db_path, "manifest": self.read_index_manifest(db_path)})
                return None

            return db_path

        # Try centralized storage first if enabled
        if self.storage_strategy == "centralized":
            centralized_path = self.index_manager.get_current_index_path(self.workspace_root)
            candidate = _record_candidate(centralized_path)
            if candidate:
                return candidate

        # Use multi-path discovery if enabled
        if self.enable_multi_path and self.path_config:
            # Try to determine repository identifier
            repo_id = self._get_repository_identifier()
            search_paths = self.path_config.get_search_paths(repo_id)

            logger.info(f"Searching for index in {len(search_paths)} locations")

            for search_path in search_paths:
                # Look for code_index.db in each path
                db_candidates = [
                    search_path / "code_index.db",
                    search_path / "current.db",
                    search_path / f"{repo_id}.db" if repo_id else None,
                ]

                for db_path in db_candidates:
                    candidate = _record_candidate(db_path)
                    if candidate:
                        logger.info(f"Found valid index at: {db_path}")
                        return candidate

        # Fall back to legacy local storage
        db_path = self.index_dir / "code_index.db"
        candidate = _record_candidate(db_path)
        if candidate:
            return candidate

        # Log detailed information about search failure
        if self.enable_multi_path:
            logger.warning(f"No valid index found after searching {len(search_paths)} paths")
            validation = self.path_config.validate_paths(self._get_repository_identifier())
            existing_paths = [str(p) for p, exists in validation.items() if exists]
            if existing_paths:
                logger.info(f"Existing search paths: {existing_paths}")

        if require_selection and candidates:
            if strict_compatibility:
                compatible_candidates = []
                for candidate in candidates:
                    if self._validate_runtime_compatibility(
                        candidate["path"],
                        requested_schema_version=requested_schema_version,
                        requested_embedding_model=requested_embedding_model,
                        strict=True,
                    ):
                        compatible_candidates.append(candidate)

                if not compatible_candidates:
                    logger.warning("No compatible local index candidates found in strict mode")
                    return None

                candidates = compatible_candidates

            try:
                return self.index_manager.select_best_index(
                    candidates,
                    requested_schema_version=requested_schema_version,
                    requested_embedding_model=requested_embedding_model,
                    strict_compatibility=strict_compatibility,
                )
            except SchemaMismatchError:
                if os.environ.get("MCP_REBUILD_ON_SCHEMA_MISMATCH") == "1":
                    self._trigger_rebuild()
                    return self.index_manager.select_best_index(
                        candidates,
                        requested_schema_version=requested_schema_version,
                        requested_embedding_model=requested_embedding_model,
                        strict_compatibility=strict_compatibility,
                    )
                raise

        return None

    def _trigger_rebuild(self) -> None:
        """Invoke the indexer rebuild path when a schema mismatch is detected."""
        import asyncio

        from mcp_server.indexer.index_engine import IndexEngine

        engine = IndexEngine()
        asyncio.get_event_loop().run_until_complete(engine.rebuild_index())

    def _validate_sqlite_index(self, db_path: Path) -> bool:
        """Validate that a file is a valid SQLite database with expected schema."""
        try:
            conn = sqlite3.connect(str(db_path))
            # Check for expected tables
            cursor = conn.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name IN ('files', 'symbols', 'repositories')
            """)
            tables = {row[0] for row in cursor.fetchall()}
            conn.close()

            # Must have at least files table
            if "files" in tables:
                return True
            else:
                logger.debug(f"Index at {db_path} missing required tables")
                return False
        except Exception as e:
            logger.debug(f"Invalid SQLite index at {db_path}: {e}")
            return False

    def _get_repository_identifier(self) -> Optional[str]:
        """Get repository identifier for the current workspace."""
        # Try to get from git remote
        try:
            result = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                capture_output=True,
                text=True,
                cwd=str(self.workspace_root),
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass

        # Fall back to directory name
        return self.workspace_root.name

    def _get_current_commit(self) -> Optional[str]:
        """Get the current git commit hash for the workspace."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(self.workspace_root),
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            logger.debug("Unable to resolve current git commit for manifest creation")
        return None

    def get_vector_index_path(self) -> Optional[Path]:
        """Get path to local vector index if it exists"""
        if not self.is_index_enabled():
            return None

        vector_path = self.index_dir / "vector_index"
        if vector_path.exists() and vector_path.is_dir():
            return vector_path

        return None

    def should_download_index(
        self,
        requested_schema_version: Optional[str] = None,
        requested_embedding_model: Optional[str] = None,
        strict_compatibility: bool = True,
    ) -> bool:
        """Check if we should attempt to download an index from GitHub"""
        config = self.get_index_config()
        if not config:
            return False

        # Check if auto-download is enabled
        if not config.get("auto_download", True):
            return False

        # Check if GitHub artifacts are enabled
        if not config.get("github_artifacts", {}).get("enabled", True):
            return False

        # Skip download if we already have a compatible index
        existing_index = self.get_local_index_path(
            requested_schema_version=requested_schema_version,
            requested_embedding_model=requested_embedding_model,
            strict_compatibility=strict_compatibility,
        )
        if existing_index:
            return False

        return True

    def download_latest_index(self) -> bool:
        """Attempt to download the latest index from GitHub artifacts"""
        return self.download_latest_index_for_runtime()

    def download_latest_index_for_runtime(
        self,
        requested_schema_version: Optional[str] = None,
        requested_embedding_model: Optional[str] = None,
        strict_compatibility: bool = True,
    ) -> bool:
        """Attempt to download a compatible index from GitHub artifacts."""
        if not self.should_download_index(
            requested_schema_version=requested_schema_version,
            requested_embedding_model=requested_embedding_model,
            strict_compatibility=strict_compatibility,
        ):
            return False

        # Check if gh CLI is available
        if not self._is_gh_cli_available():
            logger.info("GitHub CLI not available, skipping index download")
            return False

        try:
            # Get repository info
            repo = self._get_repository_info()
            if not repo:
                return False

            required_schema, required_model = self._load_required_compatibility(
                requested_schema_version=requested_schema_version,
                requested_embedding_model=requested_embedding_model,
            )

            # Find latest artifact
            artifact = self._find_latest_artifact(repo)
            if not artifact:
                logger.info("No index artifacts found")
                return False

            # Download and extract
            logger.info(f"Downloading index artifact: {artifact['name']}")
            if self._download_and_extract_artifact(
                repo,
                artifact["id"],
                requested_schema_version=required_schema,
                requested_embedding_model=required_model,
                strict_compatibility=strict_compatibility,
            ):
                logger.info("Successfully downloaded and extracted index")
                return True

        except Exception as e:
            logger.error(f"Failed to download index: {e}")

        return False

    def _is_gh_cli_available(self) -> bool:
        """Check if GitHub CLI is available"""
        try:
            result = subprocess.run(["gh", "--version"], capture_output=True, check=False)
            return result.returncode == 0
        except FileNotFoundError:
            return False

    def _get_repository_info(self) -> Optional[str]:
        """Get the repository name in owner/repo format"""
        try:
            result = subprocess.run(
                [
                    "gh",
                    "repo",
                    "view",
                    "--json",
                    "nameWithOwner",
                    "-q",
                    ".nameWithOwner",
                ],
                capture_output=True,
                text=True,
                cwd=str(self.workspace_root),
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return None

    def _find_latest_artifact(self, repo: str) -> Optional[Dict[str, Any]]:
        """Find the most recent index artifact"""
        try:
            provider = ArtifactProviderFactory.create(repo)
            records = provider.list_artifacts(("mcp-index-", "index-"))
            if records:
                return records[0].to_dict()
        except Exception as exc:
            logger.debug("Artifact provider list failed, using gh fallback: %s", exc)

        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    "-H",
                    "Accept: application/vnd.github+json",
                    f"/repos/{repo}/actions/artifacts",
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            if not result.stdout:
                return None

            payload = json.loads(result.stdout)
            raw_artifacts = payload.get("artifacts", [])
            artifacts = [
                artifact
                for artifact in raw_artifacts
                if artifact.get("name", "").startswith(("mcp-index-", "index-"))
            ]

            if not artifacts:
                return None

            # Sort by creation date and return most recent
            artifacts.sort(key=lambda x: x["created_at"], reverse=True)
            return artifacts[0]

        except subprocess.CalledProcessError, json.JSONDecodeError:
            return None

    def _find_recovery_artifact(
        self,
        repo: str,
        branch: Optional[str],
        commit: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Find artifact by branch/commit criteria for recovery."""
        try:
            provider = ArtifactProviderFactory.create(repo)
            artifacts = [r.to_dict() for r in provider.list_artifacts(("mcp-index-", "index-"))]

            if branch:
                artifacts = [a for a in artifacts if branch in a.get("name", "")]

            if commit:
                short_commit = commit[:8]
                artifacts = [
                    a
                    for a in artifacts
                    if commit in a.get("name", "") or short_commit in a.get("name", "")
                ]

            if artifacts:
                promoted = [a for a in artifacts if "-promoted" in a.get("name", "")]
                return promoted[0] if promoted else artifacts[0]
        except Exception as exc:
            logger.debug("Artifact provider recovery lookup failed, using gh fallback: %s", exc)

        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    "-H",
                    "Accept: application/vnd.github+json",
                    f"/repos/{repo}/actions/artifacts",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            return None

        if not result.stdout:
            return None

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

        artifacts = [
            artifact
            for artifact in payload.get("artifacts", [])
            if artifact.get("name", "").startswith(("mcp-index-", "index-"))
        ]

        if branch:
            artifacts = [a for a in artifacts if branch in a.get("name", "")]

        if commit:
            short_commit = commit[:8]
            artifacts = [
                a
                for a in artifacts
                if commit in a.get("name", "") or short_commit in a.get("name", "")
            ]

        if not artifacts:
            return None

        artifacts.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        promoted = [a for a in artifacts if "-promoted" in a.get("name", "")]
        if promoted:
            return promoted[0]

        return artifacts[0]

    def download_recovery_index(
        self,
        branch: Optional[str] = None,
        commit: Optional[str] = None,
        requested_schema_version: Optional[str] = None,
        requested_embedding_model: Optional[str] = None,
        strict_compatibility: bool = True,
    ) -> bool:
        """Download and restore artifact matching branch/commit criteria."""
        if not branch and not commit:
            logger.error("Recovery download requires branch or commit")
            return False

        if not self._is_gh_cli_available():
            logger.info("GitHub CLI not available, skipping recovery download")
            return False

        repo = self._get_repository_info()
        if not repo:
            return False

        required_schema, required_model = self._load_required_compatibility(
            requested_schema_version=requested_schema_version,
            requested_embedding_model=requested_embedding_model,
        )

        artifact = self._find_recovery_artifact(repo, branch, commit)
        if not artifact:
            logger.error("No recovery artifact found for branch=%s commit=%s", branch, commit)
            return False

        logger.info("Recovering index from artifact: %s", artifact.get("name"))
        return self._download_and_extract_artifact(
            repo,
            artifact["id"],
            requested_schema_version=required_schema,
            requested_embedding_model=required_model,
            strict_compatibility=strict_compatibility,
        )

    def _download_and_extract_artifact(
        self,
        repo: str,
        artifact_id: Any,
        requested_schema_version: Optional[str] = None,
        requested_embedding_model: Optional[str] = None,
        strict_compatibility: bool = True,
    ) -> bool:
        """Download and extract an artifact"""
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                # Download artifact
                zip_path = Path(tmpdir) / "artifact.zip"
                downloaded = False
                try:
                    provider = ArtifactProviderFactory.create(repo)
                    provider_zip_path = provider.download_artifact(str(artifact_id), Path(tmpdir))
                    if provider_zip_path != zip_path:
                        shutil.copy2(provider_zip_path, zip_path)
                    downloaded = True
                except Exception as exc:
                    logger.debug("Artifact provider download failed, using gh fallback: %s", exc)

                if not downloaded:
                    result = subprocess.run(
                        [
                            "gh",
                            "api",
                            "-H",
                            "Accept: application/vnd.github+json",
                            f"/repos/{repo}/actions/artifacts/{artifact_id}/zip",
                        ],
                        capture_output=True,
                        check=True,
                    )

                    with open(zip_path, "wb") as f:
                        f.write(result.stdout)

                # Extract zip
                subprocess.run(["unzip", "-q", str(zip_path)], cwd=tmpdir, check=True)

                # Find and extract tar.gz
                tar_path = Path(tmpdir) / "mcp-index-archive.tar.gz"
                if not tar_path.exists():
                    logger.error("Archive not found in artifact")
                    return False

                metadata_path = Path(tmpdir) / "artifact-metadata.json"
                if not metadata_path.exists():
                    logger.error("Artifact metadata not found in artifact")
                    return False

                try:
                    metadata = json.loads(metadata_path.read_text())
                except json.JSONDecodeError as exc:
                    logger.error("Artifact metadata is invalid JSON: %s", exc)
                    return False

                metadata_error = self._validate_artifact_metadata(metadata)
                if metadata_error:
                    logger.error("Artifact metadata validation failed: %s", metadata_error)
                    return False

                # Verify checksum (required)
                checksum_path = Path(tmpdir) / "mcp-index-archive.tar.gz.sha256"
                if checksum_path.exists():
                    if not self._verify_checksum(tar_path, checksum_path):
                        logger.error("Checksum verification failed")
                        return False
                else:
                    expected_checksum = metadata.get("checksum")
                    if not expected_checksum:
                        logger.error("Artifact checksum is required but missing")
                        return False
                    if not self._verify_checksum_value(tar_path, expected_checksum):
                        logger.error("Checksum verification failed")
                        return False

                compatibility_error = self._validate_artifact_compatibility_metadata(
                    metadata,
                    requested_schema_version=requested_schema_version,
                    requested_embedding_model=requested_embedding_model,
                    strict=strict_compatibility,
                )
                if compatibility_error:
                    logger.error(
                        "Artifact compatibility validation failed: %s",
                        compatibility_error,
                    )
                    return False

                staging_dir = Path(tmpdir) / "staging_extract"
                staging_dir.mkdir(parents=True, exist_ok=True)

                # Extract to staging directory
                with tarfile.open(tar_path, "r:gz") as tar:
                    members = tar.getmembers()
                    for member in members:
                        if not _validate_tar_member(member, staging_dir):
                            return False

                    tar.extractall(staging_dir, members=members)

                staged_index_path = staging_dir / "code_index.db"
                if not staged_index_path.exists():
                    logger.error("Extracted artifact missing code_index.db")
                    return False

                if not self._validate_runtime_compatibility(
                    staged_index_path,
                    requested_schema_version=requested_schema_version,
                    requested_embedding_model=requested_embedding_model,
                    strict=strict_compatibility,
                ):
                    logger.error("Extracted artifact index failed runtime compatibility checks")
                    return False

                # Replace index directory atomically from staged files
                self.index_dir.mkdir(parents=True, exist_ok=True)

                for item in self.index_dir.iterdir():
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()

                for item in staging_dir.iterdir():
                    destination = self.index_dir / item.name
                    if item.is_dir():
                        shutil.copytree(item, destination)
                    else:
                        shutil.copy2(item, destination)

                return True

            except Exception as e:
                logger.error(f"Failed to download/extract artifact: {e}")
                return False

    def _verify_checksum(self, file_path: Path, checksum_path: Path) -> bool:
        """Verify SHA256 checksum"""
        try:
            with open(checksum_path) as f:
                expected_checksum = f.read().split()[0]

            sha256 = hashlib.sha256()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    sha256.update(chunk)

            actual_checksum = sha256.hexdigest()
            return actual_checksum == expected_checksum

        except Exception as e:
            logger.warning(f"Checksum verification failed: {e}")
            return False

    def _verify_checksum_value(self, file_path: Path, expected_checksum: str) -> bool:
        """Verify SHA256 checksum against an expected checksum value."""
        try:
            sha256 = hashlib.sha256()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    sha256.update(chunk)

            actual_checksum = sha256.hexdigest()
            return actual_checksum == expected_checksum
        except Exception as e:
            logger.warning(f"Checksum verification failed: {e}")
            return False

    def _load_required_compatibility(
        self,
        requested_schema_version: Optional[str] = None,
        requested_embedding_model: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Resolve required schema/model compatibility from args + config + env."""
        if requested_schema_version and requested_embedding_model:
            return requested_schema_version, requested_embedding_model

        config = self.get_index_config() or {}
        github_artifacts = config.get("github_artifacts", {})

        resolved_schema = requested_schema_version or github_artifacts.get(
            "required_schema_version"
        )
        resolved_schema = resolved_schema or os.getenv("INDEX_SCHEMA_VERSION")

        resolved_model = requested_embedding_model or github_artifacts.get(
            "required_embedding_model"
        )
        resolved_model = resolved_model or os.getenv("SEMANTIC_EMBEDDING_MODEL")

        return resolved_schema, resolved_model

    def _validate_artifact_metadata(self, metadata: Dict[str, Any]) -> Optional[str]:
        """Validate required artifact metadata fields."""
        required_keys = ["checksum", "commit", "branch", "timestamp", "compatibility"]
        for key in required_keys:
            if key not in metadata:
                return f"Missing required metadata key: {key}"

        compatibility = metadata.get("compatibility")
        if not isinstance(compatibility, dict):
            return "Metadata compatibility field must be an object"

        for key in ["schema_version", "embedding_model"]:
            if key not in compatibility:
                return f"Missing compatibility metadata field: {key}"

        return None

    def _validate_artifact_compatibility_metadata(
        self,
        metadata: Dict[str, Any],
        requested_schema_version: Optional[str],
        requested_embedding_model: Optional[str],
        strict: bool,
    ) -> Optional[str]:
        """Validate artifact metadata compatibility against required schema/model."""
        if not strict:
            return None

        compatibility = metadata.get("compatibility", {})
        artifact_schema = compatibility.get("schema_version")
        artifact_model = compatibility.get("embedding_model")

        if requested_schema_version and artifact_schema != requested_schema_version:
            return (
                "schema mismatch: "
                f"required={requested_schema_version}, artifact={artifact_schema}"
            )

        if requested_embedding_model and artifact_model != requested_embedding_model:
            return (
                "embedding model mismatch: "
                f"required={requested_embedding_model}, artifact={artifact_model}"
            )

        return None

    def _validate_runtime_compatibility(
        self,
        index_path: Path,
        requested_schema_version: Optional[str],
        requested_embedding_model: Optional[str],
        strict: bool,
    ) -> bool:
        """Validate runtime index compatibility against required schema/model."""
        if not strict:
            return True

        manifest = self.read_index_manifest(index_path)
        actual_schema = (
            manifest.schema_version if manifest else self._read_schema_version(index_path)
        )
        actual_model = (
            manifest.embedding_model if manifest else self._read_embedding_model(index_path)
        )

        if requested_schema_version:
            if not actual_schema:
                logger.error("Schema version unavailable for compatibility check")
                return False
            if actual_schema != requested_schema_version:
                logger.error(
                    "Index schema mismatch: required=%s, found=%s",
                    requested_schema_version,
                    actual_schema,
                )
                return False

        if requested_embedding_model:
            if not actual_model:
                logger.error("Embedding model unavailable for compatibility check")
                return False
            if actual_model != requested_embedding_model:
                logger.error(
                    "Index embedding model mismatch: required=%s, found=%s",
                    requested_embedding_model,
                    actual_model,
                )
                return False

        return True

    def _read_schema_version(self, index_path: Path) -> Optional[str]:
        """Read schema version from SQLite index."""
        try:
            conn = sqlite3.connect(str(index_path))
            try:
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
                )
                if cursor.fetchone() is None:
                    return None

                cursor = conn.execute("SELECT MAX(version) FROM schema_version")
                value = cursor.fetchone()[0]
                return str(value) if value is not None else None
            finally:
                conn.close()
        except Exception as e:
            logger.warning("Failed to read index schema version: %s", e)
            return None

    def _read_embedding_model(self, index_path: Path) -> Optional[str]:
        """Read embedding model from index config or metadata."""
        try:
            conn = sqlite3.connect(str(index_path))
            try:
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='index_config'"
                )
                if cursor.fetchone() is not None:
                    cursor = conn.execute(
                        "SELECT config_value FROM index_config WHERE config_key='embedding_model'"
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        return str(row[0])
            finally:
                conn.close()
        except Exception as e:
            logger.warning("Failed to read embedding model from index DB: %s", e)

        metadata_path = index_path.parent / ".index_metadata.json"
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text())
                model = metadata.get("embedding_model")
                if model:
                    return str(model)
            except Exception as e:
                logger.warning("Failed to read index metadata: %s", e)

        return None

    def get_index_info(self) -> Dict[str, Any]:
        """Get comprehensive information about the index"""
        info = {
            "enabled": self.is_index_enabled(),
            "has_local_index": False,
            "has_vector_index": False,
            "auto_download": False,
            "github_artifacts": False,
            "metadata": None,
            "config": None,
            "search_paths": [],
            "found_at": None,
            "profile_hydration": None,
        }

        if info["enabled"]:
            index_path = self.get_local_index_path()
            info["has_local_index"] = index_path is not None
            info["found_at"] = str(index_path) if index_path else None
            info["has_vector_index"] = self.get_vector_index_path() is not None
            info["metadata"] = self._read_index_metadata_for_path(index_path)
            info["config"] = self.get_index_config()
            info["profile_hydration"] = self.get_profile_hydration_status(
                index_path=index_path,
                lexical_available=index_path is not None,
            )

            if info["config"]:
                info["auto_download"] = info["config"].get("auto_download", True)
                info["github_artifacts"] = (
                    info["config"].get("github_artifacts", {}).get("enabled", True)
                )

            # Include search paths if multi-path is enabled
            if self.enable_multi_path and self.path_config:
                repo_id = self._get_repository_identifier()
                info["search_paths"] = [str(p) for p in self.path_config.get_search_paths(repo_id)]

        return info

    def find_all_indexes(self) -> List[Dict[str, Any]]:
        """Find all available indexes across all search paths."""
        if not self.enable_multi_path or not self.path_config:
            # Just check the default location
            index_path = self.get_local_index_path()
            if index_path:
                return [
                    {
                        "path": str(index_path),
                        "type": "sqlite",
                        "valid": True,
                        "location_type": "default",
                    }
                ]
            return []

        found_indexes = []
        repo_id = self._get_repository_identifier()
        search_paths = self.path_config.get_search_paths(repo_id)

        for search_path in search_paths:
            # Look for SQLite indexes
            for pattern in ["code_index.db", "current.db", "*.db"]:
                for db_path in search_path.glob(pattern):
                    if self._validate_sqlite_index(db_path):
                        manifest = self.read_index_manifest(db_path)
                        found_indexes.append(
                            {
                                "path": str(db_path),
                                "type": "sqlite",
                                "valid": True,
                                "location_type": self._classify_location(search_path),
                                "size_mb": db_path.stat().st_size / (1024 * 1024),
                                "manifest": manifest.to_dict() if manifest else None,
                            }
                        )

            # Look for vector indexes
            vector_path = search_path / "vector_index"
            if vector_path.exists() and vector_path.is_dir():
                found_indexes.append(
                    {
                        "path": str(vector_path),
                        "type": "vector",
                        "valid": True,
                        "location_type": self._classify_location(search_path),
                    }
                )

        return found_indexes

    def _classify_location(self, path: Path) -> str:
        """Classify the type of location for an index."""
        # Normalize to forward slashes for cross-platform string matching
        path_str = str(path).replace("\\", "/")
        if "test_indexes" in path_str:
            return "test"
        elif "/.indexes/" in path_str or path_str.endswith(".indexes"):
            return "centralized"
        elif "/.mcp-index" in path_str:
            return "legacy"
        elif "/tmp/" in path_str:
            return "temporary"
        elif path_str.startswith(str(Path.home()).replace("\\", "/")):
            return "user"
        elif "/workspaces/" in path_str:
            return "docker"
        else:
            return "other"

    @staticmethod
    def discover_indexes(search_paths: List[Path]) -> List[Tuple[Path, Dict[str, Any]]]:
        """Discover all MCP indexes in the given search paths"""
        discovered = []

        for search_path in search_paths:
            if not search_path.exists():
                continue

            # Look for .mcp-index.json files
            for config_file in search_path.rglob(".mcp-index.json"):
                workspace = config_file.parent
                discovery = IndexDiscovery(workspace)
                info = discovery.get_index_info()

                if info["enabled"] and info["has_local_index"]:
                    discovered.append((workspace, info))

        return discovered
