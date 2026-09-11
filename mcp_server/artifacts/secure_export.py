"""Consistent, filtered generation export without copying live vector storage."""

from __future__ import annotations

import json
import os
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

from mcp_server.core.ignore_patterns import build_walker_filter
from mcp_server.core.path_resolver import PathResolver


class SecureIndexExporter:
    """Export a SQLite snapshot and the exact vectors referenced by its mappings."""

    def __init__(
        self,
        *,
        repo_path: Path | str = ".",
        index_location: Path | str | None = None,
        index_path: Path | str | None = None,
        semantic_indexer=None,
        secure: bool = True,
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.index_location = Path(index_location) if index_location is not None else self.repo_path
        self.index_path = (
            Path(index_path) if index_path is not None else self.index_location / "current.db"
        )
        if index_path is None and not self.index_path.exists():
            self.index_path = self.repo_path / "code_index.db"
        self.semantic_indexer = semantic_indexer
        self._excluded = build_walker_filter(self.repo_path)
        self.secure = secure

    def _should_exclude(self, file_path: str) -> bool:
        path = Path(file_path)
        path = path if path.is_absolute() else self.repo_path / path
        if not path.resolve().is_relative_to(self.repo_path):
            return True
        return self._excluded(path) if self.secure else False

    def create_filtered_database(self, source_db: str, target_db: str) -> Tuple[int, int]:
        from mcp_server.storage.sqlite_store import SQLiteStore

        SQLiteStore.snapshot_database(Path(source_db), Path(target_db))
        store = SQLiteStore(target_db, path_resolver=PathResolver(self.repo_path))
        excluded = 0
        try:
            with store._get_connection() as connection:
                files = connection.execute(
                    "SELECT id, repository_id, relative_path, path FROM files"
                ).fetchall()
            for row in files:
                if self._should_exclude(row["relative_path"] or row["path"]):
                    store.remove_file(row["relative_path"], row["repository_id"])
                    excluded += 1
            with store._get_connection() as connection:
                # These records belong to the exporting host, not the portable index.
                for table in (
                    "pending_vector_deletions",
                    "file_moves",
                    "query_cache",
                    "parse_cache",
                ):
                    connection.execute(f"DELETE FROM {table}")
                connection.execute(
                    "DELETE FROM index_config WHERE config_key NOT IN "
                    "('chunk_identity_scheme', 'chunker_version', 'index_generation')"
                )
                connection.execute(
                    "UPDATE files SET path=relative_path WHERE relative_path IS NOT NULL"
                )
                connection.execute("UPDATE repositories SET path='.'")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("Export contains dangling storage relationships")
            store.rebuild_generation_indexes()
        finally:
            store.close()
        with closing(sqlite3.connect(target_db)) as connection:
            connection.execute("VACUUM")
        return len(files) - excluded, excluded

    @staticmethod
    def read_generation_metadata(index_path: Path, index_location: Path) -> dict:
        root = index_path.parent / (index_path.stem + ".semantic")
        candidates = list(root.glob("*/*/.index_metadata.json")) if root.exists() else []
        if not candidates and not root.exists():
            legacy = index_location / ".index_metadata.json"
            if legacy.exists():
                candidates = [legacy]
        if not candidates:
            return {}
        if len(candidates) != 1:
            raise RuntimeError("Generation has ambiguous semantic ownership")
        metadata = json.loads(candidates[0].read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError("Generation metadata is not an object")
        return metadata

    def _export_vectors(self, database: Path, destination: Path) -> dict:
        from qdrant_client import QdrantClient

        with closing(sqlite3.connect(database)) as connection:
            records = connection.execute(
                "SELECT DISTINCT profile_id, point_id, collection FROM semantic_points"
            ).fetchall()
        if not records:
            return {}
        indexer = self.semantic_indexer
        metadata = (
            indexer._load_existing_metadata()
            if indexer is not None
            else self.read_generation_metadata(self.index_path, self.index_location)
        )
        profile = metadata.get("semantic_profile")
        attested = metadata.get("semantic_profiles", {}).get(profile, {})
        collection = metadata.get("collection_name")
        if not attested.get("attested") or any(
            row[0] != profile or row[2] != collection for row in records
        ):
            raise RuntimeError("Export vectors lack one attested generation owner")
        client = indexer.qdrant if indexer is not None else None
        owns_client = client is None
        try:
            if owns_client:
                # File owners must be quiescent. A live lock is an error, never removed.
                backend = str(metadata.get("qdrant_path") or "")
                if backend.startswith(("http://", "https://")):
                    if backend != os.environ.get("QDRANT_URL"):
                        raise RuntimeError("Export backend is not the configured server")
                    client = QdrantClient(url=backend, timeout=30)
                elif backend and Path(backend).resolve().is_relative_to(
                    self.index_path.parent.resolve()
                ):
                    client = QdrantClient(path=str(Path(backend).resolve()))
                else:
                    raise RuntimeError("Export backend is not owned by this generation")
            ids = sorted({row[1] for row in records})
            with destination.open("x", encoding="utf-8") as handle:
                for start in range(0, len(ids), 256):
                    batch = ids[start : start + 256]
                    points = client.retrieve(
                        collection, batch, with_vectors=True, with_payload=True
                    )
                    if {point.id for point in points} != set(batch):
                        raise RuntimeError("Export vector mappings are incomplete")
                    for point in points:
                        payload = dict(point.payload or {})
                        relative = payload.get("relative_path")
                        if not relative or self._should_exclude(relative):
                            raise RuntimeError("Export vector has no admitted source path")
                        payload["file"] = relative
                        handle.write(
                            json.dumps({"id": point.id, "vector": point.vector, "payload": payload})
                            + "\n"
                        )
        finally:
            if owns_client and client is not None:
                client.close()
        metadata = dict(metadata)
        metadata.pop("qdrant_path", None)
        metadata["semantic_profiles"] = {
            profile: {key: value for key, value in attested.items() if key != "qdrant_path"}
        }
        metadata["vector_export"] = {
            "format": "semantic-vectors.v1",
            "file": destination.name,
            "points": len(ids),
            "profile_id": profile,
            "collection": collection,
        }
        return metadata

    def create_secure_archive(
        self, output_path: str = "secure_index_archive.tar.gz"
    ) -> Dict[str, Any]:
        if not self.index_path.is_file():
            raise FileNotFoundError("Requested index generation is absent")
        stats: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "components": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "current.db"
            included, excluded = self.create_filtered_database(str(self.index_path), str(database))
            stats.update(files_included=included, files_excluded=excluded)
            stats["components"].append("current.db")
            metadata = self._export_vectors(database, root / "semantic-vectors.jsonl")
            if metadata:
                (root / ".index_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
                stats["components"].extend(["semantic-vectors.jsonl", ".index_metadata.json"])
                stats["vector_points_included"] = metadata["vector_export"]["points"]
            with tarfile.open(output_path, "x:gz") as archive:
                for name in stats["components"]:
                    archive.add(root / name, arcname=name)
        stats["archive_size"] = Path(output_path).stat().st_size
        return stats
