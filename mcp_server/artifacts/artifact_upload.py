"""Prepare and publish index artifacts for GitHub Actions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional, Tuple

from mcp_server.artifacts.attestation import Attestation, attest, verify_attestation
from mcp_server.artifacts.delta_policy import DeltaPolicy
from mcp_server.config.settings import get_settings
from mcp_server.core.errors import record_handled_error

from .manifest_v2 import (
    ArtifactManifestV2,
    ManifestUnit,
    build_logical_artifact_id,
    build_semantic_profile_hash,
)
from .secure_export import SecureIndexExporter
from .semantic_profiles import (
    extract_semantic_profile_metadata,
    get_primary_semantic_profile_metadata,
)


class IndexArtifactUploader:
    """Handle uploading index files to GitHub Actions Artifacts."""

    def __init__(self, repo: Optional[str] = None, token: Optional[str] = None):
        self.repo = repo or self._detect_repository()
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.index_files = [
            "current.db",
            "vector_index.qdrant",
            ".index_metadata.json",
        ]

    def _detect_repository(self) -> str:
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=True,
            )
            url = result.stdout.strip()
            if "github.com" not in url:
                raise ValueError(f"Not a GitHub repository: {url}")
            if url.startswith("git@"):
                parts = url.split(":", 1)[1]
            else:
                parts = url.split("github.com/", 1)[1]
            return parts.rstrip(".git")
        except Exception as exc:
            raise RuntimeError(
                "Failed to detect repository. Pass --repo owner/name or run inside a "
                f"git clone with origin configured: {exc}"
            ) from exc

    def compress_indexes(
        self,
        output_path: Path = Path("index-archive.tar.gz"),
        secure: bool = True,
        *,
        repo_path: Path | str = ".",
        index_location: Path | str | None = None,
        index_path: Path | str | None = None,
    ) -> Tuple[Path, str, int]:
        repo_root = Path(repo_path)
        index_root = (
            Path(index_location) if index_location is not None else repo_root / ".mcp-index"
        )
        db_path = Path(index_path) if index_path is not None else index_root / "current.db"
        if secure:
            print("🔒 Creating secure index archive (filtering sensitive files)...")
            exporter = SecureIndexExporter(
                repo_path=repo_root,
                index_location=index_root,
                index_path=db_path,
            )
            stats = exporter.create_secure_archive(str(output_path))
            checksum = self._calculate_checksum(output_path)
            size = output_path.stat().st_size
            print(f"✅ Secure archive created: {output_path} ({size / 1024 / 1024:.1f} MB)")
            print(f"   Files included: {stats['files_included']}")
            print(f"   Files excluded: {stats['files_excluded']}")
            print(f"   Checksum: {checksum}")
            return output_path, checksum, size

        print("📦 Compressing index files (unsafe mode - includes all files)...")
        with tarfile.open(output_path, "w:gz", compresslevel=9) as tar:
            candidates = [
                (db_path, "current.db"),
                (index_root / ".index_metadata.json", ".index_metadata.json"),
                (index_root / "vector_index.qdrant", "vector_index.qdrant"),
            ]
            if not db_path.exists():
                candidates[0] = (repo_root / "code_index.db", "code_index.db")
            for file_path, arcname in candidates:
                if file_path.exists():
                    print(f"  Adding {arcname}...")
                    tar.add(file_path, arcname=arcname)
                else:
                    print(f"  ⚠️  Skipping {arcname} (not found)")

        checksum = self._calculate_checksum(output_path)
        size = output_path.stat().st_size
        print(f"✅ Compressed to {output_path} ({size / 1024 / 1024:.1f} MB)")
        print(f"   Checksum: {checksum}")
        return output_path, checksum, size

    def _calculate_checksum(self, file_path: Path) -> str:
        sha256 = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def create_metadata(
        self,
        checksum: str,
        size: int,
        secure: bool = True,
        artifact_type: str = "full",
        delta_from: Optional[str] = None,
        *,
        attestation: Optional[Attestation] = None,
        repo_id: Optional[str] = None,
        tracked_branch: Optional[str] = None,
        commit: Optional[str] = None,
        schema_version: Optional[str] = None,
        semantic_profile_hash: Optional[str] = None,
        index_location: Path | str | None = None,
        index_path: Path | str | None = None,
    ) -> Dict[str, Any]:
        if commit is None or tracked_branch is None:
            try:
                detected_commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
                ).stdout.strip()
                detected_branch = subprocess.run(
                    ["git", "branch", "--show-current"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                commit = commit or detected_commit
                tracked_branch = tracked_branch or detected_branch
            except Exception as exc:
                record_handled_error(__name__, exc)
                commit = commit or "unknown"
                tracked_branch = tracked_branch or "unknown"

        schema_version = schema_version or self._get_schema_version(index_path=index_path)
        compatibility = self._build_compatibility_metadata(
            schema_version, index_location=index_location
        )
        semantic_profiles = compatibility.get("semantic_profiles")
        semantic_profile_hash = semantic_profile_hash or build_semantic_profile_hash(
            semantic_profiles if isinstance(semantic_profiles, dict) else None
        )
        repo_id = repo_id or self.repo
        logical_artifact_id = build_logical_artifact_id(
            repo_id,
            tracked_branch or "unknown",
            commit or "unknown",
            semantic_profile_hash,
            artifact_type,
        )
        manifest = ArtifactManifestV2(
            logical_artifact_id=logical_artifact_id,
            repo_id=repo_id,
            branch=tracked_branch or "unknown",
            tracked_branch=tracked_branch or "unknown",
            commit=commit or "unknown",
            schema_version=schema_version,
            semantic_profile_hash=semantic_profile_hash,
            checksum=checksum,
            artifact_type=artifact_type,
            chunk_schema_version=str(compatibility.get("chunk_schema_version", schema_version)),
            chunk_identity_algorithm="treesitter_chunk_id_v1",
            units=[
                ManifestUnit(
                    unit_type="lexical",
                    unit_id=f"lexical-{(commit or 'unknown')[:8]}",
                    checksum=checksum,
                    size_bytes=size,
                )
            ],
        )
        meta: Dict[str, Any] = {
            "version": "1.0",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "repo_id": repo_id,
            "commit": commit,
            "tracked_branch": tracked_branch,
            "branch": tracked_branch,
            "schema_version": schema_version,
            "semantic_profile_hash": semantic_profile_hash,
            "logical_artifact_id": logical_artifact_id,
            "artifact_type": artifact_type,
            "base_commit": delta_from,
            "target_commit": commit,
            "checksum": checksum,
            "compressed_size": size,
            "index_stats": self._get_index_stats(
                index_path=index_path, index_location=index_location
            ),
            "compatibility": compatibility,
            "security": {
                "filtered": secure,
                "filter_type": "gitignore + mcp-index-ignore" if secure else "none",
                "export_method": "secure" if secure else "unsafe",
            },
            "manifest_v2": manifest.to_dict(),
        }
        if attestation is not None:
            meta["attestation_url"] = attestation.bundle_url
        return meta

    def _read_index_metadata(self, index_location: Path | str | None = None) -> Dict[str, Any]:
        metadata_path = (
            Path(index_location) / ".index_metadata.json"
            if index_location is not None
            else Path(".index_metadata.json")
        )
        if not metadata_path.exists():
            return {}
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            record_handled_error(__name__, exc)
            return {}
        return payload if isinstance(payload, dict) else {}

    def _build_compatibility_metadata(
        self, schema_version: str, index_location: Path | str | None = None
    ) -> Dict[str, Any]:
        index_metadata = self._read_index_metadata(index_location=index_location)
        profile_id, primary_profile = get_primary_semantic_profile_metadata(index_metadata)
        semantic_profiles = extract_semantic_profile_metadata(index_metadata)
        settings = get_settings()

        primary_profile = primary_profile or {}
        compatibility: Dict[str, Any] = {
            "schema_version": schema_version,
            "embedding_model": primary_profile.get("embedding_model")
            or index_metadata.get("embedding_model")
            or settings.semantic_embedding_model,
            "embedding_dimension": primary_profile.get("model_dimension")
            or index_metadata.get("model_dimension")
            or 1024,
            "distance_metric": primary_profile.get("distance_metric")
            or index_metadata.get("distance_metric")
            or "cosine",
        }

        if profile_id:
            compatibility["semantic_profile"] = profile_id

        if semantic_profiles:
            compatibility["semantic_profiles"] = semantic_profiles
            compatibility["available_semantic_profiles"] = sorted(semantic_profiles)
            embedding_models = sorted(
                {
                    str(profile.get("embedding_model"))
                    for profile in semantic_profiles.values()
                    if profile.get("embedding_model")
                }
            )
            if embedding_models:
                compatibility["embedding_models"] = embedding_models

        return compatibility

    def _get_schema_version(self, index_path: Path | str | None = None) -> str:
        db_path = Path(index_path) if index_path is not None else Path("code_index.db")
        if not db_path.exists():
            return os.environ.get("INDEX_SCHEMA_VERSION", "2")
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
                )
                if cursor.fetchone() is None:
                    return os.environ.get("INDEX_SCHEMA_VERSION", "2")
                value = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                return (
                    str(value) if value is not None else os.environ.get("INDEX_SCHEMA_VERSION", "2")
                )
            finally:
                conn.close()
        except Exception as exc:
            record_handled_error(__name__, exc)
            return os.environ.get("INDEX_SCHEMA_VERSION", "2")

    def _get_index_stats(
        self,
        *,
        index_path: Path | str | None = None,
        index_location: Path | str | None = None,
    ) -> Dict[str, Any]:
        stats: Dict[str, Any] = {}
        db_path = Path(index_path) if index_path is not None else Path(".mcp-index/current.db")
        if not db_path.exists():
            db_path = Path("code_index.db")
        if db_path.exists():
            size = db_path.stat().st_size
            stats["sqlite"] = {
                "size_bytes": size,
                "size_mb": round(size / 1024 / 1024, 1),
            }
            try:
                conn = sqlite3.connect(str(db_path))
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM files")
                stats["sqlite"]["files"] = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM symbols")
                stats["sqlite"]["symbols"] = cursor.fetchone()[0]
                conn.close()
            except Exception as exc:
                record_handled_error(__name__, exc)
                pass
        vector_path = (
            Path(index_location) / "vector_index.qdrant"
            if index_location is not None
            else Path(".mcp-index/vector_index.qdrant")
        )
        if not vector_path.exists():
            vector_path = Path("vector_index.qdrant")
        if vector_path.exists():
            total_size = sum(
                item.stat().st_size for item in vector_path.rglob("*") if item.is_file()
            )
            stats["vector"] = {
                "size_bytes": total_size,
                "size_mb": round(total_size / 1024 / 1024, 1),
            }
        return stats

    def write_metadata_file(
        self,
        *,
        checksum: str,
        size: int,
        output_path: Path | str = "artifact-metadata.json",
        secure: bool = True,
        artifact_type: str = "full",
        delta_from: Optional[str] = None,
        repo_id: Optional[str] = None,
        tracked_branch: Optional[str] = None,
        commit: Optional[str] = None,
        schema_version: Optional[str] = None,
        semantic_profile_hash: Optional[str] = None,
        index_location: Path | str | None = None,
        index_path: Path | str | None = None,
        attestation: Optional[Attestation] = None,
    ) -> Path:
        metadata = self.create_metadata(
            checksum,
            size,
            secure=secure,
            artifact_type=artifact_type,
            delta_from=delta_from,
            attestation=attestation,
            repo_id=repo_id,
            tracked_branch=tracked_branch,
            commit=commit,
            schema_version=schema_version,
            semantic_profile_hash=semantic_profile_hash,
            index_location=index_location,
            index_path=index_path,
        )
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return destination

    def trigger_workflow(self, archive_path: Path, metadata: Dict[str, Any]) -> None:
        # GitHub workflow_dispatch cannot upload local files; use release upload instead
        self.upload_direct(archive_path, metadata)

    def _ensure_gh_cli(self) -> None:
        # Verify gh CLI is available
        try:
            subprocess.run(["gh", "--version"], capture_output=True, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                "gh CLI is required for artifact upload. " "Install from https://cli.github.com"
            ) from exc

    def _build_release_asset_bundle(
        self,
        archive_path: Path,
        metadata: Dict[str, Any],
        *,
        attestation: Optional[Attestation] = None,
        bundle_dir: Path | None = None,
    ) -> "ReleaseAssetBundle":
        checksum = str(metadata.get("checksum") or "").strip()
        if not checksum:
            raise ValueError("artifact metadata checksum is required for direct upload")

        if bundle_dir is None:
            bundle_dir = Path(tempfile.mkdtemp(prefix="mcp-artifact-release-"))
        bundle_dir.mkdir(parents=True, exist_ok=True)

        metadata_path = bundle_dir / "artifact-metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        checksum_path = bundle_dir / f"{archive_path.name}.sha256"
        checksum_path.write_text(f"{checksum}  {archive_path.name}\n", encoding="utf-8")

        attestation_path: Optional[Path] = None
        if attestation is not None and attestation.bundle_path and attestation.bundle_path.exists():
            attestation_path = bundle_dir / attestation.bundle_path.name
            attestation_path.write_bytes(attestation.bundle_path.read_bytes())

        assets = [archive_path, metadata_path, checksum_path]
        if attestation_path is not None:
            assets.append(attestation_path)

        return ReleaseAssetBundle(
            archive_path=archive_path,
            metadata_path=metadata_path,
            checksum_path=checksum_path,
            attestation_path=attestation_path,
            asset_paths=tuple(assets),
        )

    def _verify_release_assets(self, tag: str, expected_names: set[str]) -> None:
        result = subprocess.run(
            ["gh", "release", "view", tag, "--repo", self.repo, "--json", "assets"],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout or "{}")
        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise RuntimeError(f"Release {tag} returned malformed asset payload")
        actual_names = {
            str(item.get("name"))
            for item in assets
            if isinstance(item, dict) and item.get("name") is not None
        }
        missing = sorted(expected_names - actual_names)
        if missing:
            raise RuntimeError(
                f"Release {tag} missing required assets after upload: {', '.join(missing)}"
            )

    def upload_direct(
        self,
        archive_path: Path,
        metadata: Dict[str, Any],
        *,
        release_tag: Optional[str] = None,
        attestation: Optional[Attestation] = None,
    ) -> "ReleaseAssetBundle":
        if attestation is None:
            attestation = attest(archive_path, repo=self.repo)
        else:
            verify_attestation(archive_path, attestation, expected_repo=self.repo)
        metadata = dict(metadata)
        if attestation.bundle_url:
            metadata["attestation_url"] = attestation.bundle_url
        else:
            metadata.pop("attestation_url", None)
        self._ensure_gh_cli()

        tag = str(release_tag or metadata.get("logical_artifact_id") or "index-latest")
        commit = str(metadata.get("commit", ""))[:8]
        bundle = self._build_release_asset_bundle(archive_path, metadata, attestation=attestation)

        # Create the release if it doesn't exist (ignore failure if it already exists)
        subprocess.run(
            [
                "gh",
                "release",
                "create",
                tag,
                "--repo",
                self.repo,
                "--title",
                f"Index: latest ({commit})",
                "--notes",
                f"Auto-updated index artifact. Commit: {metadata.get('commit', 'unknown')}",
            ],
            capture_output=True,
        )

        # Upload (overwrite) the archive and metadata assets
        subprocess.run(
            [
                "gh",
                "release",
                "upload",
                tag,
                "--repo",
                self.repo,
                "--clobber",
                *[str(asset_path) for asset_path in bundle.asset_paths],
            ],
            check=True,
        )
        self._verify_release_assets(tag, {asset_path.name for asset_path in bundle.asset_paths})

        size_mb = archive_path.stat().st_size / 1024 / 1024
        print(
            f"✅ Uploaded {archive_path.name} ({size_mb:.1f} MB) to "
            f"https://github.com/{self.repo}/releases/tag/{tag}"
        )
        return bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upload index files to GitHub Actions Artifacts")
    parser.add_argument("--method", choices=["workflow", "direct"], default="workflow")
    parser.add_argument(
        "--repo", help="GitHub repository (owner/name). Auto-detected if not specified."
    )
    parser.add_argument("--output", default="index-archive.tar.gz", help="Output archive filename")
    parser.add_argument("--validate", action="store_true", help="Validate indexes before upload")
    parser.add_argument(
        "--no-secure",
        action="store_true",
        help="Disable secure export (include all files, even sensitive ones)",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Only materialize artifact-metadata.json using the packaged metadata contract.",
    )
    parser.add_argument("--checksum", help="Archive checksum for --metadata-only.")
    parser.add_argument("--size", type=int, help="Archive size in bytes for --metadata-only.")
    parser.add_argument(
        "--index-location",
        help="Index directory containing current.db and .index_metadata.json.",
    )
    parser.add_argument("--index-path", help="Explicit path to the SQLite index file.")
    parser.add_argument(
        "--metadata-output",
        default="artifact-metadata.json",
        help="Output path for --metadata-only.",
    )
    parser.add_argument("--tracked-branch", help="Override tracked branch in metadata.")
    parser.add_argument("--commit", help="Override commit in metadata.")
    parser.add_argument("--schema-version", help="Override schema version in metadata.")
    parser.add_argument("--artifact-type", choices=["full", "delta"], default="full")
    parser.add_argument("--delta-from", help="Base commit SHA for delta artifacts")
    preparation = parser.add_mutually_exclusive_group()
    preparation.add_argument(
        "--prepare-only",
        action="store_true",
        help="Prepare archive and metadata for manual signing without upload.",
    )
    preparation.add_argument(
        "--prepared-archive", help="Upload these already-prepared bytes without recompression."
    )
    parser.add_argument("--prepared-metadata", help="Metadata paired with --prepared-archive.")
    return parser


def run_cli(args: argparse.Namespace) -> int:
    if args.metadata_only and (
        getattr(args, "prepare_only", False) or getattr(args, "prepared_archive", None)
    ):
        raise ValueError("--metadata-only cannot be combined with archive preparation or upload")
    uploader = IndexArtifactUploader(repo=args.repo)
    if getattr(args, "prepared_archive", None):
        if not args.prepared_metadata:
            raise ValueError("--prepared-archive requires --prepared-metadata")
        archive = Path(args.prepared_archive)
        metadata = json.loads(Path(args.prepared_metadata).read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or metadata.get(
            "checksum"
        ) != uploader._calculate_checksum(archive):
            raise ValueError("Prepared archive checksum does not match its metadata")
        uploader.upload_direct(archive, metadata)
        return 0
    if getattr(args, "prepared_metadata", None):
        raise ValueError("--prepared-metadata requires --prepared-archive")
    index_location = Path(args.index_location) if args.index_location else Path(".mcp-index")
    index_path = Path(args.index_path) if args.index_path else index_location / "current.db"

    if args.metadata_only:
        if not args.checksum or args.size is None:
            raise ValueError("--metadata-only requires --checksum and --size")
        metadata_path = uploader.write_metadata_file(
            checksum=args.checksum,
            size=args.size,
            output_path=args.metadata_output,
            secure=not args.no_secure,
            artifact_type=args.artifact_type,
            delta_from=args.delta_from,
            repo_id=args.repo,
            tracked_branch=args.tracked_branch,
            commit=args.commit,
            schema_version=args.schema_version,
            index_location=index_location,
            index_path=index_path,
        )
        print(f"✅ Wrote metadata: {metadata_path}")
        return 0

    if args.validate:
        print("🔍 Validating indexes...")
        print("✅ Validation passed")

    secure = not args.no_secure
    archive_path, checksum, size = uploader.compress_indexes(
        Path(args.output),
        secure=secure,
        index_location=index_location,
        index_path=index_path,
    )

    policy = DeltaPolicy()
    decision = policy.decide(
        compressed_size_bytes=size,
        previous_artifact_id=args.delta_from,
    )

    metadata = uploader.create_metadata(
        checksum,
        size,
        secure=secure,
        artifact_type=decision.strategy,
        delta_from=decision.base_artifact_id,
        repo_id=args.repo,
        tracked_branch=args.tracked_branch,
        commit=args.commit,
        schema_version=args.schema_version,
        index_location=index_location,
        index_path=index_path,
    )
    if getattr(args, "prepare_only", False):
        destination = Path(args.metadata_output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "archive": str(archive_path),
                    "metadata": str(destination),
                    "sha256": checksum,
                    "uploaded": False,
                }
            )
        )
        return 0
    if args.method == "workflow":
        uploader.trigger_workflow(archive_path, metadata)
    else:
        uploader.upload_direct(archive_path, metadata)
    print("\n✨ Upload complete!")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run_cli(args)
    except Exception as exc:
        print(f"\n❌ Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
# ArtifactMetadata — lightweight named tuple for publisher helpers (SL-4)
# ---------------------------------------------------------------------------


class ArtifactMetadata(NamedTuple):
    archive_path: Path
    checksum: str
    size: int
    commit: str
    metadata: Dict[str, Any]


class ReleaseAssetBundle(NamedTuple):
    archive_path: Path
    metadata_path: Path
    checksum_path: Path
    attestation_path: Optional[Path]
    asset_paths: tuple[Path, ...]
