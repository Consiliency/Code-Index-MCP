"""
CLI commands for managing signed index artifacts in GitHub Releases.

This module provides commands for uploading, downloading, and managing
index artifacts with legacy GitHub Actions compatibility.
"""

import hashlib
import json
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path
from typing import List, Optional, cast
from uuid import uuid4

import click

from mcp_server.artifacts.artifact_download import (
    IndexArtifactDownloader,
    format_artifact_table,
)
from mcp_server.artifacts.artifact_upload import IndexArtifactUploader
from mcp_server.artifacts.attestation import _attestation_mode
from mcp_server.artifacts.multi_repo_artifact_coordinator import (
    MultiRepoArtifactCoordinator,
)
from mcp_server.artifacts.semantic_profiles import extract_semantic_profile_metadata
from mcp_server.config.settings import get_settings
from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher
from mcp_server.indexing.change_detector import ChangeDetector, FileChange
from mcp_server.indexing.incremental_indexer import IncrementalIndexer
from mcp_server.plugins.language_registry import get_language_by_extension
from mcp_server.plugins.plugin_factory import PluginFactory
from mcp_server.plugins.python_plugin.plugin import Plugin as PythonPlugin
from mcp_server.storage.multi_repo_manager import MultiRepositoryManager, RepositoryInfo
from mcp_server.storage.sqlite_store import SQLiteStore
from mcp_server.utils.subprocess_env import get_full_env


def _canonical_index_location(repo_info: RepositoryInfo | None = None) -> Path:
    if repo_info is not None:
        return Path(repo_info.index_location or repo_info.index_path.parent)
    return Path(".mcp-index")


def _canonical_index_path(repo_info: RepositoryInfo | None = None) -> Path:
    if repo_info is not None:
        return Path(repo_info.index_path)
    return Path(".mcp-index") / "current.db"


def _resolve_repository(repository: Optional[str]) -> RepositoryInfo | None:
    manager = MultiRepositoryManager()
    try:
        for repo in manager.list_repositories(active_only=True):
            if repository:
                if repo.repository_id == repository or repo.name == repository:
                    return repo
            elif repo.path.resolve() == Path.cwd().resolve():
                return repo
    finally:
        manager.close()
    if not repository:
        return None
    raise click.ClickException(f"Registered repository not found: {repository}")


def _get_restored_index_paths() -> List[Path]:
    """Return restored index artifacts present in the working directory."""
    index_root = Path(".mcp-index")
    expected_paths = [
        index_root / "current.db",
        index_root / ".index_metadata.json",
        index_root / "artifact-metadata.json",
        index_root / "semantic_index_metadata.json",
        index_root / "vector_index.qdrant",
    ]
    return [path for path in expected_paths if path.exists()]


def _verify_local_index_restored() -> bool:
    """Check whether artifact retrieval restored a usable local index."""
    return bool(_get_restored_index_paths())


def _print_runtime_restore_note() -> None:
    """Explain that restored index files are local runtime state for MCP use."""
    click.echo(
        "ℹ️  These restored index files are local runtime state for the MCP "
        "(`.mcp-index/current.db`, `.mcp-index/.index_metadata.json`, `.mcp-index/vector_index.qdrant`) and are "
        "normally distributed via GitHub artifacts rather than git history."
    )


def _load_json_file(path: Path) -> dict | None:
    """Load JSON data if the file exists and is valid."""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _get_artifact_identity() -> dict[str, str | None]:
    """Return restored artifact commit and branch metadata."""
    artifact_metadata = _load_json_file(Path(".mcp-index") / "artifact-metadata.json") or {}
    index_metadata = _load_json_file(Path(".mcp-index") / ".index_metadata.json") or {}
    compatibility = artifact_metadata.get("compatibility", {})
    profile_source = compatibility if compatibility else index_metadata
    semantic_profiles = extract_semantic_profile_metadata(profile_source)
    profile_names = ", ".join(sorted(semantic_profiles)) if semantic_profiles else None

    return {
        "commit": artifact_metadata.get("commit") or index_metadata.get("git_commit"),
        "branch": artifact_metadata.get("tracked_branch") or artifact_metadata.get("branch"),
        "embedding_model": compatibility.get("embedding_model")
        or index_metadata.get("embedding_model"),
        "schema_version": compatibility.get("schema_version")
        or index_metadata.get("chunk_schema_version"),
        "semantic_profiles": profile_names,
    }


def _get_git_ref_info() -> dict[str, str | None]:
    """Return current repository HEAD and branch information."""
    info: dict[str, str | None] = {"head": None, "branch": None}
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            env=get_full_env(),
        )
        info["head"] = head.stdout.strip()
    except Exception:
        return info

    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            env=get_full_env(),
        )
        info["branch"] = branch.stdout.strip()
    except Exception:
        pass

    return info


def _merge_changes(*change_groups: List[FileChange]) -> List[FileChange]:
    """Combine file changes while preserving unique path/type pairs."""
    merged = []
    seen: set[tuple[str, str, str | None]] = set()
    for changes in change_groups:
        for change in changes:
            key = (change.path, change.change_type, change.old_path)
            if key in seen:
                continue
            seen.add(key)
            merged.append(change)
    return merged


def _is_reconcile_candidate(change: FileChange) -> bool:
    """Ignore generated artifact state that should not trigger code reindex."""
    ignored_prefixes = (
        "index_backup_",
        "vector_index.qdrant/",
    )
    ignored_paths = {
        ".index_metadata.json",
        "artifact-metadata.json",
    }

    if change.path in ignored_paths:
        return False
    return not any(change.path.startswith(prefix) for prefix in ignored_prefixes)


def _get_local_drift() -> tuple[ChangeDetector, List[FileChange]]:
    """Return the detector and merged committed/uncommitted drift."""
    detector = ChangeDetector(Path.cwd())
    artifact_identity = _get_artifact_identity()
    git_info = _get_git_ref_info()

    committed_changes = []
    artifact_commit = artifact_identity.get("commit")
    if isinstance(artifact_commit, str) and git_info.get("head"):
        if artifact_commit != git_info["head"]:
            committed_changes = detector.get_changes_since_commit(artifact_commit, "HEAD")

    uncommitted_changes = detector.get_uncommitted_changes()
    merged_changes = _merge_changes(committed_changes, uncommitted_changes)
    filtered_changes = [change for change in merged_changes if _is_reconcile_candidate(change)]
    return detector, filtered_changes


def _run_incremental_reconcile(
    changes: List[FileChange], repo_info: RepositoryInfo | None = None
) -> bool:
    """Reconcile committed changes through the registered generation writer."""
    if not changes and repo_info is None:
        return True
    from mcp_server.storage.git_index_manager import GitAwareIndexManager

    with closing(MultiRepositoryManager()) as owner:
        if repo_info is None:
            repo_info = next(
                (
                    repo
                    for repo in owner.list_repositories(active_only=True)
                    if repo.path.resolve() == Path.cwd().resolve()
                ),
                None,
            )
        if repo_info is None:
            raise click.ClickException(
                "Register this repository before reconciling its committed index"
            )
        dispatcher = EnhancedDispatcher(
            enable_advanced_features=False,
            use_plugin_factory=True,
            semantic_search_enabled=get_settings().semantic_search_enabled,
            memory_aware=False,
            multi_repo_enabled=False,
        )
        manager = None
        try:
            manager = GitAwareIndexManager(owner.registry, dispatcher)
            result = manager.sync_repository_index(repo_info.repository_id)
            click.echo(f"Committed reconcile: {result.action}; files={result.files_processed}")
            return result.action in {"full_index", "incremental_update", "up_to_date"}
        finally:
            try:
                dispatcher.shutdown()
            finally:
                if manager is not None and manager.store_registry is not None:
                    manager.store_registry.shutdown()


def _print_reconcile_guidance() -> None:
    """Print reconcile guidance after restore or sync."""
    artifact_identity = _get_artifact_identity()
    git_info = _get_git_ref_info()

    if artifact_identity.get("commit"):
        click.echo(
            f"📦 Restored artifact commit: {artifact_identity['commit']}"
            + (f" ({artifact_identity['branch']})" if artifact_identity.get("branch") else "")
        )
    if artifact_identity.get("embedding_model"):
        click.echo(f"🧠 Artifact embedding model: {artifact_identity['embedding_model']}")
    if artifact_identity.get("semantic_profiles"):
        click.echo(f"🧩 Artifact semantic profiles: {artifact_identity['semantic_profiles']}")

    if not artifact_identity.get("commit") or not git_info.get("head"):
        click.echo("ℹ️  Artifact restore complete. Git drift could not be determined.")
        return

    if artifact_identity["commit"] == git_info["head"]:
        click.echo("✅ Local HEAD matches the restored artifact commit.")
    detector, all_changes = _get_local_drift()

    if not all_changes:
        click.echo("✅ No local drift detected. The restored artifact is ready to use.")
        return

    cost = detector.estimate_reindex_cost(all_changes)
    change_summary = (
        f"added/modified={cost['files_to_index']}, "
        f"deleted={cost['files_to_remove']}, moved={cost['files_to_move']}"
    )
    click.echo(f"🔄 Local drift detected relative to the restored artifact: {change_summary}")
    if detector.should_use_incremental(all_changes):
        click.echo("💡 Recommended: run or continue local incremental reconcile for these changes.")
    else:
        click.echo(
            "⚠️  Change volume is large; a local rebuild may be simpler than incremental catch-up."
        )


@click.group()
def artifact():
    """Manage signed Release index artifacts and legacy Actions downloads."""


@artifact.command()
@click.option("--validate", is_flag=True, help="Validate indexes before upload")
@click.option("--compress-only", is_flag=True, help="Only compress, do not upload")
@click.option("--no-secure", is_flag=True, help="Disable secure export (include all files)")
@click.option("--repository", help="Registered repository id or name")
@click.option("--prepare-only", is_flag=True, help="Prepare archive and metadata without uploading")
@click.option(
    "--metadata-output", type=click.Path(path_type=Path), default="artifact-metadata.json"
)
@click.option("--prepared-archive", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--prepared-metadata", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--skip-if-current",
    is_flag=True,
    default=False,
    help="No-op if this registered commit was already published",
)
def push(
    validate: bool,
    compress_only: bool,
    no_secure: bool,
    skip_if_current: bool,
    repository: Optional[str],
    prepare_only: bool,
    metadata_output: Path,
    prepared_archive: Optional[Path],
    prepared_metadata: Optional[Path],
):
    """Prepare or upload signed local index Release assets."""
    try:
        if bool(prepared_archive) != bool(prepared_metadata):
            raise click.ClickException(
                "Both --prepared-archive and --prepared-metadata are required"
            )
        if (prepared_archive and (prepare_only or compress_only or no_secure)) or (
            prepare_only and compress_only
        ):
            raise click.ClickException(
                "Preparation, compression and prepared upload are separate modes"
            )
        if prepare_only and (metadata_output.exists() or metadata_output.is_symlink()):
            raise click.ClickException("Prepared metadata already exists; use a fresh output path")
        repo_info = _resolve_repository(repository)
        index_path = _canonical_index_path(repo_info)
        index_location = _canonical_index_location(repo_info)
        if not index_path.exists():
            raise click.ClickException("No index generation found. Run indexing first.")
        if repo_info is not None:
            from mcp_server.health.repository_readiness import ReadinessClassifier

            readiness = ReadinessClassifier.classify_registered(repo_info)
            if not readiness.ready:
                raise click.ClickException(f"Index is unavailable: {readiness.code}")
        if (
            skip_if_current
            and repo_info is not None
            and repo_info.artifact_health == "published"
            and repo_info.last_published_commit == repo_info.last_indexed_commit
        ):
            click.echo("This indexed commit was already published. Skipping upload.")
            return

        if (
            not (prepare_only or compress_only or prepared_archive)
            and _attestation_mode() == "enforce"
        ):
            raise click.ClickException(
                "Use --prepare-only, sign the metadata, then --prepared-archive with --prepared-metadata"
            )
        uploader = IndexArtifactUploader(repo_path=repo_info.path if repo_info else None)

        if validate:
            import sqlite3

            with closing(
                sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True)
            ) as conn:
                if (
                    conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]
                    or conn.execute("PRAGMA foreign_key_check").fetchone()
                ):
                    raise click.ClickException("Index integrity validation failed")
            click.echo("✅ Validation passed")

        identity = {
            "repo_id": repo_info.repository_id if repo_info else None,
            "commit": repo_info.last_indexed_commit if repo_info else None,
            "tracked_branch": repo_info.tracked_branch if repo_info else None,
        }
        if prepared_archive:
            assert prepared_metadata is not None
            uploader.upload_prepared(prepared_archive, prepared_metadata, **identity)
        else:
            secure = not no_secure
            archive_path, checksum, size = uploader.compress_indexes(
                index_location / f"index-archive-{uuid4().hex}.tar.gz",
                secure=secure,
                repo_path=repo_info.path if repo_info else Path.cwd(),
                index_location=index_location,
                index_path=index_path,
            )
            if compress_only:
                click.echo(f"Prepared archive: {archive_path}; no upload requested")
                return
            if prepare_only:
                uploader.write_metadata_file(
                    checksum=checksum,
                    size=size,
                    output_path=metadata_output,
                    secure=secure,
                    index_location=index_location,
                    index_path=index_path,
                    **identity,
                )
                click.echo(
                    json.dumps(
                        {
                            "archive": str(archive_path),
                            "metadata": str(metadata_output),
                            "sha256": hashlib.sha256(metadata_output.read_bytes()).hexdigest(),
                            "archive_sha256": checksum,
                            "uploaded": False,
                        }
                    )
                )
                return
            metadata = uploader.create_metadata(
                checksum,
                size,
                secure=secure,
                index_location=index_location,
                index_path=index_path,
                **identity,
            )
            uploader.upload_direct(archive_path, metadata)
        if repo_info is not None:
            with closing(MultiRepositoryManager()) as owner:
                recorded = owner.registry.mark_artifact_published(
                    repo_info.repository_id,
                    expected_registration_id=repo_info.registration_id,
                    expected_generation=repo_info.index_generation,
                    expected_commit=repo_info.last_indexed_commit,
                )
                if not recorded:
                    click.echo("Upload completed for an older generation; current state unchanged.")

    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()


@artifact.command()
@click.option("--latest", is_flag=True, help="Download latest compatible artifact")
@click.option("--artifact-id", type=int, help="Download specific artifact by ID")
@click.option("--repository", help="Registered repository id or name")
@click.option(
    "--unsafe-allow-mismatched-artifact",
    is_flag=True,
    help="Allow install after identity/freshness rejection and print rejected reasons",
)
@click.option("--no-backup", is_flag=True, help="Skip backup of existing indexes")
def pull(
    latest: bool,
    artifact_id: Optional[int],
    repository: Optional[str],
    unsafe_allow_mismatched_artifact: bool,
    no_backup: bool,
):
    """Download indexes from GitHub Actions Artifacts."""
    try:
        if not latest and not artifact_id:
            click.echo("❌ Specify --latest or --artifact-id")
            return

        repo_info = _resolve_repository(repository)
        downloader = IndexArtifactDownloader(repo_path=repo_info.path if repo_info else None)
        download_workspace = tempfile.TemporaryDirectory(prefix="mcp-artifact-")
        output_dir = Path(download_workspace.name)
        try:
            if latest:
                result = downloader.download_latest(
                    output_dir=output_dir,
                    backup=not no_backup,
                    allow_unsafe=unsafe_allow_mismatched_artifact,
                    repo_id=repo_info.repository_id if repo_info else None,
                    expected_owner=repo_info,
                    repo_path=repo_info.path if repo_info else None,
                    tracked_branch=repo_info.tracked_branch if repo_info else None,
                    target_commit=repo_info.current_commit if repo_info else None,
                    index_location=_canonical_index_location(repo_info),
                    index_path=_canonical_index_path(repo_info),
                )
            else:
                artifacts = downloader.list_artifacts()
                artifact = next(
                    (item for item in artifacts if item["id"] == artifact_id),
                    {"id": artifact_id, "name": str(artifact_id)},
                )
                result = downloader.download_selected_artifact(
                    artifact,
                    output_dir=output_dir,
                    backup=not no_backup,
                    allow_unsafe=unsafe_allow_mismatched_artifact,
                    repo_id=repo_info.repository_id if repo_info else None,
                    expected_owner=repo_info,
                    repo_path=repo_info.path if repo_info else None,
                    tracked_branch=repo_info.tracked_branch if repo_info else None,
                    target_commit=repo_info.current_commit if repo_info else None,
                    index_location=_canonical_index_location(repo_info),
                    index_path=_canonical_index_path(repo_info),
                )
        finally:
            download_workspace.cleanup()

        installed = getattr(result, "installed_items", None)
        if repo_info is not None and not installed:
            raise click.ClickException("No registered artifact generation was restored")
        if repo_info is None and not _verify_local_index_restored():
            click.echo("❌ Download completed but no local index files were restored", err=True)
            raise click.Abort()

        restored = (
            ", ".join(Path(path).name for path in installed)
            if installed
            else ", ".join(path.name for path in _get_restored_index_paths())
        )
        click.echo(f"✅ Local index files restored: {restored}")
        if result.validation_reasons:
            click.echo("⚠️  Unsafe artifact install accepted:")
            for reason in result.validation_reasons:
                click.echo(f"   {reason}")
        _print_runtime_restore_note()
        _print_reconcile_guidance()

    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()


@artifact.command(name="list")
@click.option("--filter", help="Filter artifact names")
def list_artifacts(filter: Optional[str]):
    """List available index artifacts."""
    try:
        downloader = IndexArtifactDownloader()
        artifacts = downloader.list_artifacts(name_filter=filter)
        format_artifact_table(artifacts)
        if artifacts:
            click.echo(f"\nTotal: {len(artifacts)} artifacts")

    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()


@artifact.command()
@click.option("--repository", help="Registered repository id or name")
def sync(repository: Optional[str]):
    """Sync indexes with GitHub (pull if needed, push if local is newer)."""
    try:
        click.echo("🔄 Checking index synchronization status...")
        repo_info = _resolve_repository(repository)
        if repo_info is not None:
            if not repo_info.index_path.exists():
                click.get_current_context().invoke(
                    pull,
                    latest=True,
                    artifact_id=None,
                    repository=repo_info.repository_id,
                    unsafe_allow_mismatched_artifact=False,
                    no_backup=False,
                )
            if not _run_incremental_reconcile([], repo_info):
                raise click.ClickException("Registered generation could not be synchronized")
            click.echo("Registered committed generation synchronized.")
            return

        # Check if we have local indexes
        has_local = _canonical_index_path().exists()

        if not has_local:
            click.echo("📥 No local indexes found. Pulling latest...")
            downloader = IndexArtifactDownloader()
            download_workspace = tempfile.TemporaryDirectory(prefix="mcp-artifact-")
            output_dir = Path(download_workspace.name)
            try:
                downloader.download_latest(
                    output_dir=output_dir,
                    backup=True,
                    index_location=_canonical_index_location(),
                    index_path=_canonical_index_path(),
                )
            finally:
                download_workspace.cleanup()

            if not _verify_local_index_restored():
                click.echo(
                    "❌ Sync download completed but no local index files were restored",
                    err=True,
                )
                raise click.Abort()
            restored = ", ".join(path.name for path in _get_restored_index_paths())
            click.echo(f"✅ Indexes synchronized! Restored: {restored}")
            _print_runtime_restore_note()
            _print_reconcile_guidance()

            _detector, changes = _get_local_drift()
            if changes:
                raise click.ClickException(
                    "Register this repository with `mcp-index repository register <path>` "
                    "and retry `mcp-index artifact sync --repository <name>` to reconcile drift."
                )

        else:
            click.echo("📊 Local indexes found:")
            click.echo(
                "   These files are local artifact/runtime state used by the MCP, not source-controlled code."
            )

            # Get local stats
            import sqlite3

            conn = sqlite3.connect(str(_canonical_index_path()))
            cursor = conn.cursor()

            try:
                cursor.execute("SELECT COUNT(*) FROM files")
                file_count = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM symbols")
                symbol_count = cursor.fetchone()[0]

                click.echo(f"   Files: {file_count}")
                click.echo(f"   Symbols: {symbol_count}")

            except Exception:
                pass
            finally:
                conn.close()

            _print_reconcile_guidance()

            _detector, changes = _get_local_drift()
            if changes:
                raise click.ClickException(
                    "Register this repository with `mcp-index repository register <path>` "
                    "and retry `mcp-index artifact sync --repository <name>` to reconcile drift."
                )
            else:
                click.echo("\n✅ Local artifact baseline is already in sync.")

            click.echo("\n✅ Sync check complete!")

    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()


@artifact.command()
@click.option("--older-than", type=int, default=30, help="Delete artifacts older than N days")
@click.option("--keep-latest", type=int, default=5, help="Keep at least N latest artifacts")
@click.option("--dry-run", is_flag=True, help="Show what would be deleted without deleting")
def cleanup(older_than: int, keep_latest: int, dry_run: bool):
    """Clean up old artifacts to save storage."""
    try:
        click.echo(f"🧹 Cleaning up artifacts older than {older_than} days...")
        click.echo(f"   Keeping at least {keep_latest} latest artifacts")

        if dry_run:
            click.echo("   🔍 DRY RUN - no changes will be made")

        # This would trigger the GitHub Actions workflow
        # For now, just show instructions
        click.echo("\n📝 To clean up artifacts, run:")
        click.echo("   gh workflow run index-artifact-management.yml -f action=cleanup")

        click.echo("\nOr use the GitHub Actions UI to trigger the cleanup workflow.")

    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()


@artifact.command()
@click.argument("artifact_id", type=int)
def info(artifact_id: int):
    """Show detailed information about a specific artifact."""
    try:
        downloader = IndexArtifactDownloader()
        artifacts = downloader.list_artifacts()
        artifact_info = next((item for item in artifacts if item["id"] == artifact_id), None)
        if artifact_info is None:
            click.echo(f"❌ Artifact {artifact_id} not found", err=True)
            raise click.Abort()
        artifact_item = cast(dict, artifact_info)

        click.echo("\n📋 Artifact Information:")
        click.echo(f"   Name: {artifact_item['name']}")
        click.echo(f"   ID: {artifact_item['id']}")
        click.echo(f"   Size: {artifact_item['size_in_bytes'] / 1024 / 1024:.1f} MB")
        click.echo(f"   Created: {artifact_item['created_at']}")
        click.echo(f"   Expires: {artifact_item['expires_at']}")

    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()


@artifact.command()
@click.option("--branch", help="Recover artifact for a branch")
@click.option("--commit", help="Recover artifact for a commit SHA")
@click.option("--repository", help="Registered repository id or name")
@click.option(
    "--unsafe-allow-mismatched-artifact",
    is_flag=True,
    help="Allow install after identity/freshness rejection and print rejected reasons",
)
@click.option("--no-backup", is_flag=True, help="Skip backup of existing indexes")
def recover(
    branch: Optional[str],
    commit: Optional[str],
    repository: Optional[str],
    unsafe_allow_mismatched_artifact: bool,
    no_backup: bool,
):
    """Recover indexes from artifact matching branch/commit."""
    try:
        if not branch and not commit:
            click.echo("❌ Specify at least one of --branch or --commit", err=True)
            raise click.Abort()

        repo_info = _resolve_repository(repository)
        downloader = IndexArtifactDownloader(repo_path=repo_info.path if repo_info else None)
        download_workspace = tempfile.TemporaryDirectory(prefix="mcp-artifact-")
        output_dir = Path(download_workspace.name)
        try:
            result = downloader.recover(
                branch=branch,
                commit=commit,
                output_dir=output_dir,
                backup=not no_backup,
                allow_unsafe=unsafe_allow_mismatched_artifact,
                repo_id=repo_info.repository_id if repo_info else None,
                expected_owner=repo_info,
                repo_path=repo_info.path if repo_info else None,
                tracked_branch=repo_info.tracked_branch if repo_info else branch,
                target_commit=repo_info.current_commit if repo_info else commit,
                index_location=_canonical_index_location(repo_info),
                index_path=_canonical_index_path(repo_info),
            )
        finally:
            download_workspace.cleanup()

        installed = getattr(result, "installed_items", None)
        if repo_info is not None and not installed:
            raise click.ClickException("No registered artifact generation was restored")
        if repo_info is None and not _verify_local_index_restored():
            click.echo("❌ Recovery completed but no local index files were restored", err=True)
            raise click.Abort()

        restored = (
            ", ".join(Path(path).name for path in installed)
            if installed
            else ", ".join(path.name for path in _get_restored_index_paths())
        )
        click.echo(f"✅ Local index files restored: {restored}")
        if result.validation_reasons:
            click.echo("⚠️  Unsafe artifact install accepted:")
            for reason in result.validation_reasons:
                click.echo(f"   {reason}")
        _print_reconcile_guidance()

    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        raise click.Abort()


def _format_workspace_results(results: List) -> None:
    for result in results:
        status = "✅" if result.success else "❌"
        click.echo(f"{status} {result.repository_name} ({result.repository_id})")
        if result.details:
            for key, value in sorted(result.details.items()):
                click.echo(f"   {key}: {value}")
        if result.error:
            click.echo(f"   error: {result.error}")


@artifact.command("workspace-status")
@click.option("--repository", "repositories", multiple=True, help="Repository ID to inspect")
def workspace_status(repositories: tuple[str, ...]):
    """Show local-first artifact/runtime lifecycle state for registered repositories."""
    coordinator = MultiRepoArtifactCoordinator()
    manifest = coordinator.build_workspace_manifest(repositories or None)
    click.echo("🗂️  Workspace manifest")
    click.echo(f"   workspace_id: {manifest.workspace_id}")
    click.echo(f"   repositories: {len(manifest.repositories)}")
    _format_workspace_results(coordinator.get_workspace_status(repositories or None))


@artifact.command("publish-workspace")
@click.option("--repository", "repositories", multiple=True, help="Repository ID to publish")
def publish_workspace(repositories: tuple[str, ...]):
    """Prepare local artifact payloads for registered repositories."""
    coordinator = MultiRepoArtifactCoordinator()
    click.echo(
        "ℹ️  Local-first mode: this prepares per-repo artifact payloads and records workspace state without requiring paid remote publication."
    )
    results = coordinator.publish_workspace(repositories or None)
    _format_workspace_results(results)
    if any(not result.success for result in results):
        raise click.Abort()


@artifact.command("fetch-workspace")
@click.option("--repository", "repositories", multiple=True, help="Repository ID to fetch")
def fetch_workspace(repositories: tuple[str, ...]):
    """Fetch available artifacts for registered repositories where configured."""
    coordinator = MultiRepoArtifactCoordinator()
    results = coordinator.fetch_workspace(repositories or None)
    _format_workspace_results(results)
    if any(not result.success for result in results):
        raise click.Abort()


@artifact.command("reconcile-workspace")
@click.option("--repository", "repositories", multiple=True, help="Repository ID to reconcile")
def reconcile_workspace(repositories: tuple[str, ...]):
    """Refresh local readiness state for registered repositories."""
    coordinator = MultiRepoArtifactCoordinator()
    results = coordinator.reconcile_workspace(repositories or None)
    _format_workspace_results(results)
