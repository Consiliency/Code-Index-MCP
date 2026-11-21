"""
CLI commands for index management.

Provides convenient commands for managing SQLite and vector indexes.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

import click

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from mcp_server.config.env_vars import resolve_qdrant_server_api_key  # noqa: E402
from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher  # noqa: E402
from mcp_server.setup.semantic_preflight import run_semantic_preflight  # noqa: E402
from mcp_server.storage.sqlite_store import SQLiteStore  # noqa: E402

# Semantic vectors are owned by the generation-scoped SemanticIndexerRegistry
# (``ci__`` collections with provenance). The commands here build no standalone
# SemanticIndexer, so they neither read nor write a profile collection
# (Consiliency/Code-Index-MCP#142).
_SEMANTIC_REBUILD_POINTER = (
    "Build semantic vectors with `mcp-index repository sync --force-full` "
    "(register the checkout first with `mcp-index repository register <path>`)."
)
_LEGACY_SEMANTIC_REBUILD_REFUSAL = (
    "The legacy semantic (vector) rebuild is disabled: it wrote standalone profile "
    "collections that readiness and the generation-scoped semantic index cannot see. "
    + _SEMANTIC_REBUILD_POINTER
)
_LEGACY_VECTOR_DIR = "vector_index.qdrant"
_LEGACY_VECTOR_RESTORE_NOTE = (
    "Not restoring the legacy vector index: semantic vectors are generation-scoped. "
    + _SEMANTIC_REBUILD_POINTER
)
_VECTOR_COMPATIBILITY_NOTE = (
    "generation-scoped, not checked here (see `mcp-index repository status`)"
)


def _get_vector_backend_status() -> Dict[str, Any]:
    """Inspect active vector backend, preferring the live Qdrant server."""

    def _directory_size_mb(path: str) -> float:
        total_size = 0
        if os.path.exists(path):
            for dirpath, _, filenames in os.walk(path):
                for filename in filenames:
                    total_size += os.path.getsize(os.path.join(dirpath, filename))
        return total_size / (1024 * 1024)

    vector_path = os.getenv("QDRANT_PATH", _LEGACY_VECTOR_DIR)
    if os.path.exists(vector_path):
        # A file-backed store is the legacy standalone layout. Opening it with
        # QdrantClient takes (and can rewrite) its lock, so status reports it
        # from the filesystem only and never opens it (#142).
        return {
            "backend": "file",
            "location": vector_path,
            "collections": [],
            "size_mb": _directory_size_mb(vector_path),
            "legacy_read_only": True,
        }

    server_url = os.getenv("QDRANT_URL", "http://localhost:6333")
    if os.environ.get("QDRANT_USE_SERVER", "true").lower() == "true":
        # Resolved outside the broad except so a missing required key is reported.
        api_key = resolve_qdrant_server_api_key()
        try:
            from qdrant_client import QdrantClient

            client = QdrantClient(url=server_url, api_key=api_key, timeout=5)
            collection_descriptions = list(client.get_collections().collections)
            details = []
            for collection_meta in collection_descriptions:
                info = client.get_collection(collection_meta.name)
                vectors = info.config.params.vectors
                size = getattr(vectors, "size", None)
                points = getattr(info, "points_count", None)
                details.append(
                    {
                        "name": collection_meta.name,
                        "size": size,
                        "points": points,
                    }
                )
            storage_hint = "qdrant_storage"
            return {
                "backend": "server",
                "location": server_url,
                "collections": details,
                "size_mb": _directory_size_mb(storage_hint),
            }
        except Exception:
            pass

    return {}


def _build_sqlite_baseline() -> Dict[str, Any]:
    """Build the local lexical SQLite baseline using dispatcher indexing."""
    original_skip_preindex = os.environ.get("MCP_SKIP_PLUGIN_PREINDEX")
    original_lightweight_docs = os.environ.get("MCP_LIGHTWEIGHT_DOC_INDEX")
    os.environ["MCP_SKIP_PLUGIN_PREINDEX"] = "true"
    os.environ["MCP_LIGHTWEIGHT_DOC_INDEX"] = "true"
    try:
        store = SQLiteStore("code_index.db")
        dispatcher = EnhancedDispatcher(
            sqlite_store=store,
            enable_advanced_features=True,
            use_plugin_factory=True,
            lazy_load=False,
            semantic_search_enabled=False,
            memory_aware=False,
            multi_repo_enabled=False,
        )
        stats: Dict[str, Any] = dispatcher.index_directory(Path.cwd(), recursive=True)
        stats["lexical_rows"] = store.rebuild_fts_code()
        return stats
    finally:
        if original_skip_preindex is None:
            os.environ.pop("MCP_SKIP_PLUGIN_PREINDEX", None)
        else:
            os.environ["MCP_SKIP_PLUGIN_PREINDEX"] = original_skip_preindex

        if original_lightweight_docs is None:
            os.environ.pop("MCP_LIGHTWEIGHT_DOC_INDEX", None)
        else:
            os.environ["MCP_LIGHTWEIGHT_DOC_INDEX"] = original_lightweight_docs


@click.group()
def index():
    """Index management commands."""


@index.command()
@click.option("--detailed", "-d", is_flag=True, help="Show detailed compatibility information")
def check_compatibility(detailed: bool):
    """Check if current configuration is compatible with existing indexes."""
    try:
        # Check SQLite index
        sqlite_exists = os.path.exists("code_index.db")
        sqlite_compatible = True
        symbol_count = 0

        if sqlite_exists:
            try:
                store = SQLiteStore("code_index.db")
                with store._get_connection() as conn:
                    cursor = conn.execute("SELECT COUNT(*) FROM symbols")
                    symbol_count = cursor.fetchone()[0]
            except Exception as e:
                sqlite_compatible = False
                if detailed:
                    click.echo(f"SQLite error: {e}", err=True)

        # Check metadata
        metadata_exists = os.path.exists(".index_metadata.json")
        metadata = {}
        if metadata_exists:
            try:
                with open(".index_metadata.json", "r") as f:
                    metadata = json.load(f)
            except Exception as e:
                if detailed:
                    click.echo(f"Metadata error: {e}", err=True)

        # Display results
        click.echo("Index Compatibility Status:")
        click.echo(f"  SQLite index: {'✅ Compatible' if sqlite_compatible else '❌ Incompatible'}")
        click.echo(f"  Vector index: ℹ️ {_VECTOR_COMPATIBILITY_NOTE}")
        click.echo(f"  Metadata: {'✅ Present' if metadata_exists else '❌ Missing'}")

        if sqlite_exists:
            click.echo(f"  Symbol count: {symbol_count:,}")

        if detailed and metadata:
            click.echo("\nMetadata details:")
            click.echo(f"  Embedding model: {metadata.get('embedding_model', 'unknown')}")
            click.echo(f"  Created: {metadata.get('created_at', 'unknown')}")
            click.echo(f"  Git commit: {metadata.get('git_commit', 'unknown')}")

        # Vector indexes are generation-scoped and attested by repository
        # readiness, so the verdict here covers SQLite and metadata only.
        overall_compatible = sqlite_compatible and metadata_exists
        click.echo(f"\nOverall: {'✅ All compatible' if overall_compatible else '❌ Issues found'}")

        sys.exit(0 if overall_compatible else 1)

    except Exception as e:
        click.echo(f"Error checking compatibility: {e}", err=True)
        sys.exit(1)


@index.command()
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Accepted for compatibility; the SQLite index is always rebuilt",
)
@click.option("--sqlite-only", is_flag=True, help="Rebuild SQLite index only")
@click.option(
    "--vector-only",
    is_flag=True,
    help="Disabled: semantic vectors are built by `repository sync --force-full`",
)
@click.option(
    "--semantic-profile",
    "semantic_profiles",
    multiple=True,
    help="Disabled: semantic vectors are built by `repository sync --force-full`",
)
@click.option(
    "--sample-size",
    default=0,
    help="Ignored: it only limited the disabled legacy vector rebuild",
)
def rebuild(
    force: bool,
    sqlite_only: bool,
    vector_only: bool,
    semantic_profiles: tuple[str, ...],
    sample_size: int,
):
    """Rebuild the local SQLite index artifact."""

    if vector_only or semantic_profiles:
        raise click.ClickException(_LEGACY_SEMANTIC_REBUILD_REFUSAL)

    click.echo("Starting index rebuild...")
    click.echo(
        "This rebuild regenerates local runtime/artifact state. Publish the refreshed baseline via GitHub artifacts instead of committing generated index files."
    )

    if not sqlite_only:
        click.echo("⚠️ Skipping vector index: this command no longer rebuilds it.")
        click.echo(f"   {_SEMANTIC_REBUILD_POINTER}")

    click.echo("Rebuilding SQLite index...")
    try:
        # Remove old SQLite index (auto-backup first so users can restore if needed)
        if os.path.exists("code_index.db"):
            import shutil as _shutil
            from datetime import datetime as _datetime

            _bak = f"code_index.db.{_datetime.now().strftime('%Y%m%dT%H%M%S')}.bak"
            _shutil.copy2("code_index.db", _bak)
            click.echo(f"📦 Backed up existing index to {_bak}")
            os.remove("code_index.db")

        sqlite_stats = _build_sqlite_baseline()
        click.echo("✅ SQLite index rebuilt")
        click.echo(f"   Files indexed: {sqlite_stats.get('indexed_files', 0)}")
        click.echo(f"   Files ignored: {sqlite_stats.get('ignored_files', 0)}")
        click.echo(f"   Files failed: {sqlite_stats.get('failed_files', 0)}")
        click.echo(f"   Lexical rows: {sqlite_stats.get('lexical_rows', 0)}")

    except Exception as e:
        raise click.ClickException(f"SQLite index rebuild failed: {e}") from e

    click.echo("🎉 Index rebuild completed!")


@index.command()
def status():
    """Show current index status."""
    click.echo("Index Status Report")
    click.echo("=" * 30)
    click.echo(
        "Local runtime index state is restored/generated on disk for MCP use; the shared baseline is published via GitHub artifacts, not normal git history."
    )

    # SQLite index
    sqlite_path = "code_index.db"
    if os.path.exists(sqlite_path):
        try:
            store = SQLiteStore(sqlite_path)
            with store._get_connection() as conn:
                # Get symbol count
                cursor = conn.execute("SELECT COUNT(*) FROM symbols")
                symbol_count = cursor.fetchone()[0]

                # Get file count
                cursor = conn.execute("SELECT COUNT(*) FROM files")
                file_count = cursor.fetchone()[0]

                # Get database size
                db_size = os.path.getsize(sqlite_path) / (1024 * 1024)  # MB

                cursor = conn.execute("SELECT COUNT(*) FROM fts_code")
                lexical_count = cursor.fetchone()[0]

                click.echo("SQLite Index:")
                click.echo(f"  📁 Files indexed: {file_count:,}")
                click.echo(f"  🔍 Symbols found: {symbol_count:,}")
                click.echo(f"  📝 Lexical rows: {lexical_count:,}")
                click.echo(f"  💾 Database size: {db_size:.1f} MB")

        except Exception as e:
            click.echo(f"SQLite Index: ❌ Error reading ({e})")
    else:
        click.echo("SQLite Index: ❌ Not found")

    # Vector index
    vector_status = _get_vector_backend_status()
    if vector_status:
        try:
            click.echo("Vector Index:")
            click.echo(f"  🔌 Backend: {vector_status['backend']} ({vector_status['location']})")
            click.echo(f"  💾 Storage size: {vector_status['size_mb']:.1f} MB")
            collections = vector_status.get("collections") or []
            if collections:
                click.echo(f"  🧠 Collections: {len(collections)}")
                for collection in collections:
                    click.echo(
                        f"     - {collection['name']}"
                        + (
                            f" ({collection['points']} pts, dim={collection['size']})"
                            if collection.get("size")
                            else ""
                        )
                    )
            elif vector_status.get("legacy_read_only"):
                click.echo(
                    "  ⚠️ Legacy file-backed store: not opened (read-only report). "
                    "Semantic vectors are generation-scoped; see `mcp-index repository status`."
                )
            elif vector_status["backend"] == "file":
                click.echo("  ⚠️ Could not read collection info")

        except Exception as e:
            click.echo(f"Vector Index: ❌ Error reading ({e})")
    else:
        click.echo("Vector Index: ❌ Not found (optional - requires semantic deps)")

    # Metadata
    metadata_path = ".index_metadata.json"
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, "r") as f:
                metadata = json.load(f)

            click.echo("Metadata:")
            click.echo(f"  🤖 Embedding model: {metadata.get('embedding_model', 'unknown')}")
            click.echo(f"  📅 Created: {metadata.get('created_at', 'unknown')}")
            click.echo(f"  🔗 Git commit: {metadata.get('git_commit', 'unknown')[:8]}...")
            semantic_profiles = metadata.get("semantic_profiles") or {}
            if isinstance(semantic_profiles, dict) and semantic_profiles:
                click.echo("  🧩 Semantic profiles: " + ", ".join(sorted(semantic_profiles.keys())))
                for profile_id in sorted(semantic_profiles.keys()):
                    profile_meta = semantic_profiles.get(profile_id) or {}
                    collection = profile_meta.get("collection_name", "unknown")
                    model = profile_meta.get("embedding_model", "unknown")
                    click.echo(f"     - {profile_id}: {model} @ {collection}")

        except Exception as e:
            click.echo(f"Metadata: ❌ Error reading ({e})")
    else:
        click.echo("Metadata: ❌ Not found")


@index.command()
@click.argument("backup_dir")
def backup(backup_dir: str):
    """Create backup of current indexes."""
    import shutil
    from datetime import datetime

    if not backup_dir:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = f"index_backup_{timestamp}"

    os.makedirs(backup_dir, exist_ok=True)

    files_backed_up = 0

    # Backup SQLite index
    if os.path.exists("code_index.db"):
        shutil.copy2("code_index.db", f"{backup_dir}/code_index.db")
        files_backed_up += 1
        click.echo("📦 Backed up SQLite index")

    # The legacy vector directory is not backed up: restore never brings it
    # back, and semantic vectors are generation-scoped (#142).
    if os.path.exists(_LEGACY_VECTOR_DIR):
        click.echo("⚠️ Skipping legacy vector index (generation-scoped vectors are not backed up)")

    # Backup metadata
    if os.path.exists(".index_metadata.json"):
        shutil.copy2(".index_metadata.json", f"{backup_dir}/.index_metadata.json")
        files_backed_up += 1
        click.echo("📦 Backed up metadata")

    if files_backed_up > 0:
        click.echo(f"✅ Backup completed: {backup_dir} ({files_backed_up} items)")
    else:
        click.echo("❌ No index files found to backup")


@index.command()
@click.argument("backup_dir")
def restore(backup_dir: str):
    """Restore indexes from backup."""
    import shutil

    if not os.path.exists(backup_dir):
        click.echo(f"❌ Backup directory not found: {backup_dir}")
        return

    click.echo(f"Restoring from backup: {backup_dir}")

    # Remove current indexes
    if os.path.exists("code_index.db"):
        os.remove("code_index.db")
        click.echo("🗑️ Removed current SQLite index")

    if os.path.exists(".index_metadata.json"):
        os.remove(".index_metadata.json")
        click.echo("🗑️ Removed current metadata")

    files_restored = 0

    # Restore SQLite index
    backup_sqlite = f"{backup_dir}/code_index.db"
    if os.path.exists(backup_sqlite):
        shutil.copy2(backup_sqlite, "code_index.db")
        files_restored += 1
        click.echo("♻️ Restored SQLite index")

    # The legacy vector directory is neither removed nor recreated: semantic
    # vectors are generation-scoped and rebuilt by repository sync (#142).
    if os.path.exists(os.path.join(backup_dir, _LEGACY_VECTOR_DIR)):
        click.echo(f"⚠️ {_LEGACY_VECTOR_RESTORE_NOTE}")

    # Restore metadata
    backup_metadata = f"{backup_dir}/.index_metadata.json"
    if os.path.exists(backup_metadata):
        shutil.copy2(backup_metadata, ".index_metadata.json")
        files_restored += 1
        click.echo("♻️ Restored metadata")

    if files_restored > 0:
        click.echo(f"✅ Restore completed ({files_restored} items)")
    else:
        click.echo("❌ No backup files found to restore")


@index.command()
def check_semantic():
    """Check semantic search configuration and status."""
    from mcp_server.config.settings import reload_settings

    settings = reload_settings()
    report = run_semantic_preflight(
        settings=settings,
        profile=settings.get_semantic_default_profile(),
        strict=settings.semantic_strict_mode,
    )

    click.echo("Semantic Search Configuration Check")
    click.echo("=" * 40)
    click.echo(f"Overall ready: {'✅' if report.overall_ready else '❌'}")
    click.echo(
        "Can write semantic vectors: " + ("✅" if report.can_write_semantic_vectors else "❌")
    )
    click.echo(f"Profiles: {report.profiles.status.value} - {report.profiles.message}")
    click.echo(f"Enrichment: {report.enrichment.status.value} - {report.enrichment.message}")
    click.echo(f"Embedding: {report.embedding.status.value} - {report.embedding.message}")
    click.echo(f"Qdrant: {report.qdrant.status.value} - {report.qdrant.message}")
    click.echo(f"Collection: {report.collection.status.value} - {report.collection.message}")

    if report.warnings:
        click.echo("\nWarnings:")
        for warning in report.warnings:
            click.echo(f"- {warning}")

    if report.blocker is not None:
        click.echo("\nSemantic write blocker:")
        click.echo(f"  Code: {report.blocker.code}")
        click.echo(f"  Reason: {report.blocker.message}")
        if report.blocker.remediation:
            click.echo("  Remediation:")
            for fix in report.blocker.remediation:
                click.echo(f"  - {fix}")

    click.echo("\nEffective config:")
    click.echo(json.dumps(report.to_dict()["effective_config"], indent=2))

    if report.strict_mode and not report.can_write_semantic_vectors:
        raise click.ClickException("Strict semantic preflight failed")


if __name__ == "__main__":
    index()
