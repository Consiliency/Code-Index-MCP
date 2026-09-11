"""
SQLite-based persistence layer for the MCP Server.

This module provides a local storage implementation using SQLite with FTS5
for efficient full-text search capabilities.
"""

import functools
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from ..core.errors import TransientArtifactError
from ..core.path_resolver import PathResolver
from ..indexing.friction import extract_friction_markers
from ..indexing.github_issues import issue_history_dedupe_key
from ..indexing.source_metadata import (
    HISTORY_SOURCE_TYPE,
    HistoryIssueRecord,
    extract_matching_source_metadata,
    merge_source_metadata,
)
from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)

_LEXICAL_EXCLUDED_FILENAMES = {
    "full_indexing_log.txt",
    "mcp_validation_results.json",
    "mcp_indexing_status.json",
    "semantic_indexing_progress.json",
    "mcp_indexing_summary.json",
    "complete_indexing_results.json",
    "mcp_direct_test_results.json",
    "test_queries.json",
}

_SQLITE_STORAGE_FAILURE_PATTERNS = (
    ("disk I/O error", "disk_io_error"),
    ("database or disk is full", "disk_full"),
)


def _merge_chunk_source_metadata(
    metadata: Optional[Dict[str, Any]],
    content: str,
    line_start: int,
) -> Dict[str, Any]:
    records = []
    for marker in extract_friction_markers(content):
        adjusted = dict(marker)
        adjusted["line"] = int(marker["line"]) + max(int(line_start) - 1, 0)
        records.append(adjusted)
    if not records:
        return dict(metadata or {})
    return merge_source_metadata(metadata, records)


def _history_issue_document_content(record: HistoryIssueRecord) -> str:
    labels = ", ".join(record["labels"]) if record["labels"] else "(none)"
    lines = [
        f"# Issue #{record['number']}: {record['title']}",
        f"Repository: {record['repo']}",
        f"Type: {record['type']}",
        f"State: {record['state']}",
        f"Labels: {labels}",
        f"Created: {record['created_at']}",
        f"Updated: {record['updated_at']}",
        f"URL: {record['url']}",
        "",
        f"Summary: {record['summary']}",
    ]
    closed_at = record.get("closed_at")
    if closed_at:
        lines.insert(7, f"Closed: {closed_at}")
    if record["learnings"]:
        lines.extend(["", "Learnings:"])
        lines.extend(f"- {learning}" for learning in record["learnings"])
    return "\n".join(lines)


def _normalized_query_terms(query: str) -> List[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", query)
    return [token for token in re.findall(r"[a-z0-9_]+", expanded.lower()) if len(token) >= 3]


def _looks_like_symbol_precise_query(query: str) -> bool:
    normalized = query.strip()
    if not normalized:
        return False
    if re.search(r"\b(class|def|function|method|symbol)\s+[A-Za-z_][A-Za-z0-9_]*", normalized):
        return True
    if re.search(r"\b[A-Z][A-Za-z0-9_]{2,}\b", normalized):
        return True
    return bool(re.search(r"\b[a-z]+_[a-z0-9_]+\b", normalized))


def _fts_rank_adjustment(query: str, file_path: str) -> float:
    normalized_path = file_path.replace("\\", "/").lower().lstrip("./")
    filename_stem = Path(normalized_path).stem
    path_tokens = set(re.findall(r"[a-z0-9_]+", normalized_path))
    terms = _normalized_query_terms(query)

    adjustment = 0.0
    if normalized_path.endswith("/__init__.py") or normalized_path == "__init__.py":
        adjustment += 0.45
    if _looks_like_symbol_precise_query(query) and (
        normalized_path.startswith("tests/") or "/tests/" in normalized_path
    ):
        adjustment += 0.7

    filename_matches = sum(1 for term in terms if term.replace("-", "_") in filename_stem)
    path_matches = sum(1 for term in terms if term.replace("-", "_") in path_tokens)
    adjustment -= min(filename_matches, 3) * 0.22
    adjustment -= min(path_matches, 4) * 0.08
    return adjustment


def classify_sqlite_storage_failure(error: BaseException) -> Optional[Dict[str, Any]]:
    """Normalize SQLite operational storage failures into a stable diagnostics shape."""
    message = str(error)
    lowered = message.lower()
    for needle, reason in _SQLITE_STORAGE_FAILURE_PATTERNS:
        if needle.lower() not in lowered:
            continue
        return {
            "storage_failure_family": "sqlite_operational",
            "storage_failure_reason": reason,
            "storage_failure_message": message,
            "readonly": True,
        }
    return None


# ---------------------------------------------------------------------------
# Chunk identity-scheme guard (CHUNKERSAFE Lane A)
#
# ``code_chunks`` rows (and the ``chunk_summaries`` / ``semantic_points`` /
# remote Qdrant vectors keyed off their ids) are only coherent within a single
# chunk-identity scheme.  Upstream ``treesitter-chunker`` may change the id
# algorithm across a major (issue #76 / v4), which silently corrupts a mixed
# index if un-reindexed rows from an older scheme survive.  These helpers persist
# a per-DB scheme marker in ``index_config`` and provide the single fail-closed
# check that every writer, deleter, and scheme-dependent reader routes through.
# ---------------------------------------------------------------------------

#: Legacy stable-id algorithm name, kept coherent with the hardcoded
#: ``chunk_identity_algorithm`` in ``manifest_v2`` / ``artifact_upload`` and with
#: the semantic-profile ``chunker_version`` contract.  The chunk-id algorithm was
#: stable across chunker package majors 1-3, so pre-v4 chunkers map to this.
LEGACY_CHUNK_ID_SCHEME = "treesitter_chunk_id_v1"

#: ``index_config`` keys for the persisted scheme markers.
CHUNK_SCHEME_MARKER_KEY = "chunk_identity_scheme"
CHUNK_IDENTITY_ALGORITHM_KEY = "chunk_identity_algorithm"
CHUNK_SCHEME_REBUILD_KEY = "chunk_scheme_rebuild_target"

#: ``code_chunks.chunk_type`` value used for synthetic history/document rows that
#: are NOT produced by the tree-sitter chunker and must survive a rebuild.
PRESERVED_CHUNK_TYPE = "document"

_SCHEME_READ_BLOCKING = frozenset({"mismatch", "missing_marker", "rebuilding"})
_SCHEME_WRITE_BLOCKING = frozenset({"mismatch", "missing_marker"})


def _escape_like(text: str) -> str:
    """Escape SQL LIKE metacharacters so a literal id matches only itself.

    Canonical home for the helper (this module is imported by the indexing layer,
    not vice-versa).  Backslash is the ``ESCAPE`` char (see ``LIKE ? ESCAPE '\\'``),
    so it must be escaped first; then the ``%``/``_`` wildcards are neutralised.
    Under collision-free (v4) ids a ``chunk_id`` may legitimately contain ``%``/``_``,
    which would otherwise make a ``chunk_id LIKE '<id>:part:%'`` query mis-match
    unrelated rows.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ChunkSchemeMismatchError(RuntimeError):
    """Raised when a ``code_chunks`` operation would cross an id-scheme boundary.

    A typed, actionable error: refusing the operation is the fail-closed behavior
    that prevents a mixed-scheme (silently corrupt) index.
    """

    def __init__(
        self,
        message: str,
        *,
        expected: Optional[str] = None,
        found: Optional[str] = None,
        state: Optional[str] = None,
        remediation: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.expected = expected
        self.found = found
        self.state = state
        self.remediation = remediation or (
            "Run an atomic chunk-scheme rebuild (reindex) before reading or writing "
            "code chunks for this repository."
        )


def _chunker_major_version(chunker_module: Any) -> Optional[int]:
    version = getattr(chunker_module, "__version__", None)
    if not version:
        try:
            import importlib.metadata as _md

            version = _md.version("treesitter-chunker")
        except Exception:
            version = None
    if not version:
        return None
    match = re.match(r"\s*v?(\d+)", str(version))
    return int(match.group(1)) if match else None


@functools.lru_cache(maxsize=1)
def current_chunk_id_scheme() -> str:
    """Return the id-scheme of the installed chunker.

    Reuses the upstream identity contract: an explicit ``CHUNK_ID_SCHEME``
    attribute wins; otherwise derive from the package major, staying coherent
    with the legacy ``treesitter_chunk_id_v1`` algorithm for pre-v4 chunkers and
    only diverging to ``treesitter_chunk_id_v{major}`` once a new major (>=4)
    ships without advertising the attribute.  When the chunker cannot be imported
    at all, default to the legacy scheme.

    Result is cached for the process (``lru_cache``): the installed chunker
    version cannot change without a restart, so this keeps the per-write hot path
    off the ``importlib.metadata`` dist-info scan (I6).
    """
    try:
        import chunker
    except Exception:
        return LEGACY_CHUNK_ID_SCHEME
    scheme = getattr(chunker, "CHUNK_ID_SCHEME", None)
    if isinstance(scheme, str) and scheme.strip():
        return scheme.strip()
    major = _chunker_major_version(chunker)
    if major is None or major < 4:
        return LEGACY_CHUNK_ID_SCHEME
    return f"treesitter_chunk_id_v{major}"


def _is_missing_schema_error(error: sqlite3.OperationalError) -> bool:
    """True only for a structural "schema not present" error - a missing table or
    column (a not-yet-created / older-schema index).

    Every OTHER ``OperationalError`` - notably "database is locked"/"is busy"
    under write contention - must NOT be swallowed: misreading a locked, *marked*
    DB as absent would let the caller reclassify it as ``empty`` and silently
    re-stamp the marker with the new scheme (I1, silent mixed-scheme corruption).
    Those propagate so the caller fails loud instead of corrupting the marker.
    """
    message = str(error).lower()
    return "no such table" in message or "no such column" in message


def _scheme_read_config_value(conn: sqlite3.Connection, key: str) -> Optional[str]:
    try:
        row = conn.execute(
            "SELECT config_value FROM index_config WHERE config_key = ?", (key,)
        ).fetchone()
    except sqlite3.OperationalError as error:
        if _is_missing_schema_error(error):
            return None
        raise
    if row is None:
        return None
    return row[0]


def _has_chunker_rows(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM code_chunks WHERE chunk_type != ? LIMIT 1",
            (PRESERVED_CHUNK_TYPE,),
        ).fetchone()
    except sqlite3.OperationalError as error:
        if _is_missing_schema_error(error):
            return False
        raise
    return row is not None


def evaluate_chunk_scheme(
    conn: sqlite3.Connection, target_scheme: Optional[str] = None
) -> Tuple[str, Optional[str], str]:
    """Classify the chunk-scheme compatibility of ``conn`` against ``target_scheme``.

    Returns ``(status, marker, target)`` where ``status`` is one of:

    - ``"compatible"`` - marker present and equal to target.
    - ``"compatible_legacy"`` - no marker but chunker rows present AND the current
      target is the legacy ``treesitter_chunk_id_v1`` scheme.  The chunk-id
      algorithm was stable across chunker majors 1-3, so an existing unmarked
      (v3-built) index genuinely IS v1: treat it as compatible and auto-stamp v1
      on the next write, rather than bricking every legacy index (B1).
    - ``"empty"``       - no marker and no chunker rows; safe to stamp on first build.
    - ``"missing_marker"`` - no marker but chunker rows present AND the current
      target is a *different* (>=v4) scheme: a real scheme change over an unmarked
      index, treated as incompatible - fail closed.
    - ``"mismatch"``    - marker present but different from target.
    - ``"rebuilding"``  - an atomic rebuild is in progress and unfinalized.
    """
    target = target_scheme or current_chunk_id_scheme()
    rebuild_target = _scheme_read_config_value(conn, CHUNK_SCHEME_REBUILD_KEY)
    if rebuild_target is not None:
        return ("rebuilding", rebuild_target, target)
    marker = _scheme_read_config_value(conn, CHUNK_SCHEME_MARKER_KEY)
    if marker is not None:
        return ("compatible" if marker == target else "mismatch", marker, target)
    if _has_chunker_rows(conn):
        if target == LEGACY_CHUNK_ID_SCHEME:
            return ("compatible_legacy", None, target)
        return ("missing_marker", None, target)
    return ("empty", None, target)


def assert_chunk_scheme_readable(
    conn: sqlite3.Connection, target_scheme: Optional[str] = None
) -> None:
    """Fail closed on scheme-dependent reads over an incompatible/rebuilding index."""
    status, marker, target = evaluate_chunk_scheme(conn, target_scheme)
    if status in _SCHEME_READ_BLOCKING:
        raise ChunkSchemeMismatchError(
            f"code_chunks scheme is {status!r} (index marker={marker!r}, current "
            f"scheme={target!r}); refusing scheme-dependent read until an atomic "
            "rebuild completes.",
            expected=target,
            found=marker,
            state=status,
        )


class SQLiteStore:
    """SQLite-based storage implementation with FTS5 support."""

    def __init__(
        self,
        db_path: str = "code_index.db",
        path_resolver: Optional[PathResolver] = None,
        pool: Optional[ConnectionPool] = None,
    ):
        """
        Initialize the SQLite store.

        Args:
            db_path: Path to the SQLite database file
            path_resolver: PathResolver instance for path management
            pool: Optional ConnectionPool for read operations; factory must use
                  check_same_thread=False.
        """
        self.db_path = db_path
        self.path_resolver = path_resolver or PathResolver()
        self._pool = pool
        self._connection_state = threading.local()
        self.registry_binding = None
        self._memory_uri = None
        self._memory_anchor = None
        if db_path == ":memory:" and pool is None:
            # Keep the database alive while allowing the existing nested read connections.
            self._memory_uri = f"file:index-it-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._memory_anchor = sqlite3.connect(
                self._memory_uri, uri=True, check_same_thread=False
            )
        self._readonly = False
        self._readonly_diagnostics: Optional[Dict[str, Any]] = None

        self._init_database()
        self._run_migrations()
        self._ensure_semantic_points_table()
        self._ensure_pending_vector_deletions_table()
        self._ensure_chunk_summary_audit_columns()

    def _require_writable(self) -> None:
        if self._readonly:
            raise TransientArtifactError("store is read-only after ENOSPC")

    def close(self) -> None:
        """Close the store.  If a connection pool is attached, drain it."""
        if self._pool is not None:
            self._pool.close_all()
        if self._memory_anchor is not None:
            self._memory_anchor.close()
            self._memory_anchor = None

    def get_readonly_diagnostics(self) -> Optional[Dict[str, Any]]:
        """Return the most recent storage-failure provenance that forced read-only mode."""
        if self._readonly_diagnostics is None:
            return None
        return dict(self._readonly_diagnostics)

    @staticmethod
    def snapshot_database(source: Path, destination: Path) -> None:
        """Copy a committed SQLite snapshot without modifying the active database."""
        deadline = time.monotonic() + 30

        def progress(status: int, remaining: int, total: int) -> None:
            if time.monotonic() >= deadline:
                raise TimeoutError("SQLite generation snapshot deadline exceeded")

        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(destination)) as writer:
                reader.backup(writer, pages=256, progress=progress)

    def prepare_generation(self, committed_hashes: Dict[str, str], profile_id: str) -> None:
        """Regenerate code-owned rows in an unpublished copy, retaining owned data."""
        self._require_writable()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            retained_files = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT file_id FROM code_chunks "
                    "WHERE chunk_type = ? OR chunk_id LIKE 'history:%'",
                    (PRESERVED_CHUNK_TYPE,),
                )
            }
            files_by_id = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM files")}
            readable_scheme = evaluate_chunk_scheme(conn, self._current_scheme())[0] in {
                "compatible",
                "compatible_legacy",
            }
            valid_summaries = {
                row["chunk_hash"]
                for row in conn.execute("SELECT * FROM chunk_summaries")
                if row["file_id"] in retained_files
                or (
                    readable_scheme
                    and row["profile_id"] == profile_id
                    and (file := files_by_id.get(row["file_id"])) is not None
                    and file["content_hash"] is not None
                    and committed_hashes.get(file["relative_path"]) == file["content_hash"]
                )
            }
            stale = self._collect_stale_chunk_artifacts(conn, profile_id)
            stale["summary_hashes"] = [
                value for value in stale["summary_hashes"] if value not in valid_summaries
            ]
            self._delete_chunker_rows(conn, stale)
            auxiliary_tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for file_id, file in files_by_id.items():
                if file_id in retained_files:
                    continue
                for table in ("bm25_content", "bm25_documents", "bm25_index_status"):
                    if table in auxiliary_tables:
                        conn.execute(f"DELETE FROM {table} WHERE file_id = ?", (file_id,))
                if "bm25_symbols" in auxiliary_tables:
                    conn.execute(
                        "DELETE FROM bm25_symbols WHERE symbol_id IN "
                        "(SELECT id FROM symbols WHERE file_id = ?)",
                        (file_id,),
                    )
                conn.execute(
                    "DELETE FROM symbol_references WHERE file_id = ? OR symbol_id IN "
                    "(SELECT id FROM symbols WHERE file_id = ?)",
                    (file_id, file_id),
                )
                conn.execute("DELETE FROM imports WHERE file_id = ?", (file_id,))
                conn.execute("DELETE FROM embeddings WHERE file_id = ?", (file_id,))
                conn.execute(
                    "DELETE FROM symbol_trigrams WHERE symbol_id IN "
                    "(SELECT id FROM symbols WHERE file_id = ?)",
                    (file_id,),
                )
                conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
                if file["relative_path"] not in committed_hashes:
                    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
            conn.execute("DELETE FROM query_cache")
            conn.execute("DELETE FROM parse_cache")
            conn.execute("DELETE FROM fts_code")
            self._stamp_scheme(conn, self._current_scheme())
            self._delete_config(conn, CHUNK_SCHEME_REBUILD_KEY)
            self.rebuild_generation_indexes()

    def import_artifact_rows(self, source_db: Path, source_root: Path) -> List[Dict]:
        """Merge portable rows into a prepared stage, retaining local imported records."""
        from ..core.ignore_patterns import build_walker_filter

        excluded = build_walker_filter(source_root)
        repository_id = self.ensure_repository_row(self.path_resolver.repository_root)
        with closing(
            sqlite3.connect(source_db.resolve().as_uri() + "?mode=ro", uri=True)
        ) as source:
            source.row_factory = sqlite3.Row
            assert_chunk_scheme_readable(source)
            if source.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Artifact contains dangling storage relationships")
            with self._get_connection() as target:
                file_ids, symbol_ids = {}, {}
                imported_chunks = set()
                code_paths = set()

                def insert(table, row, **overrides):
                    columns = {item[1] for item in target.execute(f"PRAGMA table_info({table})")}
                    values = {
                        key: value
                        for key, value in dict(row).items()
                        if key in columns and key != "id"
                    }
                    values.update(overrides)
                    names = list(values)
                    return target.execute(
                        f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                        [values[name] for name in names],
                    ).lastrowid

                for row in source.execute("SELECT * FROM files WHERE is_deleted=0"):
                    relative = row["relative_path"]
                    path = source_root / relative
                    if (
                        Path(relative).is_absolute()
                        or ".." in Path(relative).parts
                        or excluded(path)
                    ):
                        raise ValueError("Artifact contains an excluded source path")
                    has_code = (
                        source.execute(
                            "SELECT 1 FROM code_chunks WHERE file_id=? AND chunk_type!='document' "
                            "AND chunk_id NOT LIKE 'history:%' LIMIT 1",
                            (row["id"],),
                        ).fetchone()
                        or not source.execute(
                            "SELECT 1 FROM code_chunks WHERE file_id=?", (row["id"],)
                        ).fetchone()
                    )
                    if has_code:
                        if (
                            not path.is_file()
                            or hashlib.sha256(path.read_bytes()).hexdigest() != row["content_hash"]
                        ):
                            raise ValueError("Artifact source differs from committed input")
                        code_paths.add(relative)
                    existing = target.execute(
                        "SELECT id FROM files WHERE repository_id=? AND relative_path=?",
                        (repository_id, relative),
                    ).fetchone()
                    if existing and not has_code:
                        continue
                    canonical = str(self.path_resolver.resolve_path(relative))
                    if existing:
                        file_id = existing[0]
                        target.execute(
                            "UPDATE files SET path=?, hash=?, content_hash=?, language=?, is_deleted=0 WHERE id=?",
                            (canonical, row["hash"], row["content_hash"], row["language"], file_id),
                        )
                    else:
                        file_id = insert("files", row, repository_id=repository_id, path=canonical)
                    file_ids[row["id"]] = file_id
                    if has_code:
                        target.execute(
                            "DELETE FROM fts_code WHERE CAST(file_id AS TEXT)=?", (str(file_id),)
                        )
                        target.execute(
                            "INSERT INTO fts_code (content, file_id) VALUES (?, ?)",
                            (path.read_text(encoding="utf-8", errors="replace"), file_id),
                        )
                for row in source.execute("SELECT * FROM symbols"):
                    if row["file_id"] in file_ids:
                        symbol_ids[row["id"]] = insert(
                            "symbols", row, file_id=file_ids[row["file_id"]]
                        )
                for row in source.execute("SELECT * FROM code_chunks"):
                    file_id = file_ids.get(row["file_id"])
                    if file_id is None:
                        continue
                    present = target.execute(
                        "SELECT file_id, content FROM code_chunks WHERE chunk_id=?",
                        (row["chunk_id"],),
                    ).fetchone()
                    if present:
                        if present[0] != file_id or present[1] != row["content"]:
                            raise ValueError("Artifact chunk identity collides with retained data")
                        continue
                    insert(
                        "code_chunks",
                        row,
                        file_id=file_id,
                        symbol_id=symbol_ids.get(row["symbol_id"]),
                    )
                    imported_chunks.add(row["chunk_id"])
                for table in ("imports", "symbol_references", "embeddings"):
                    for row in source.execute(f"SELECT * FROM {table}"):
                        if row["file_id"] not in file_ids:
                            continue
                        overrides = {"file_id": file_ids[row["file_id"]]}
                        if "symbol_id" in row.keys():
                            if row["symbol_id"] is not None and row["symbol_id"] not in symbol_ids:
                                continue
                            overrides["symbol_id"] = symbol_ids.get(row["symbol_id"])
                        insert(table, row, **overrides)
                for row in source.execute("SELECT * FROM chunk_summaries"):
                    if (
                        row["chunk_hash"] in imported_chunks
                        and not target.execute(
                            "SELECT 1 FROM chunk_summaries WHERE chunk_hash=?",
                            (row["chunk_hash"],),
                        ).fetchone()
                    ):
                        insert("chunk_summaries", row, file_id=file_ids[row["file_id"]])
                mappings = []
                file_summaries = {f"{path}:file-summary" for path in code_paths}
                for row in source.execute("SELECT * FROM semantic_points"):
                    chunk_id = row["chunk_id"]
                    if (
                        chunk_id in imported_chunks
                        or chunk_id.split(":part:")[0] in imported_chunks
                        or chunk_id in file_summaries
                    ):
                        if target.execute(
                            "SELECT 1 FROM semantic_points WHERE profile_id=? AND chunk_id=?",
                            (row["profile_id"], chunk_id),
                        ).fetchone():
                            raise ValueError("Artifact mapping collides with retained data")
                        insert("semantic_points", row)
                        mappings.append(dict(row))
                if target.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("Artifact merge has dangling storage relationships")
        return mappings

    def rebuild_generation_indexes(self) -> None:
        """Reconstruct symbol indexes and retained-document FTS from staged rows."""
        with self._get_connection() as conn:
            conn.execute("INSERT INTO fts_symbols(fts_symbols) VALUES('rebuild')")
            conn.execute("DELETE FROM symbol_trigrams")
            for symbol_id, name in conn.execute("SELECT id, name FROM symbols").fetchall():
                self._store_trigrams(conn, symbol_id, name)
            for row in conn.execute(
                "SELECT DISTINCT file_id FROM code_chunks WHERE chunk_type = ? "
                "OR chunk_id LIKE 'history:%'",
                (PRESERVED_CHUNK_TYPE,),
            ).fetchall():
                self._refresh_fts_code_for_file(row[0])
            conn.execute("INSERT INTO fts_code(fts_code) VALUES('rebuild')")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("Staged generation has invalid row relationships")

    def _mark_readonly_from_storage_failure(
        self, error: sqlite3.OperationalError
    ) -> Optional[Dict[str, Any]]:
        diagnostics = classify_sqlite_storage_failure(error)
        if diagnostics is None:
            return None
        self._readonly = True
        self._readonly_diagnostics = diagnostics
        from mcp_server.metrics.prometheus_exporter import mcp_storage_readonly_total

        mcp_storage_readonly_total.inc()
        return diagnostics

    def _ensure_semantic_points_table(self) -> None:
        """Ensure semantic point mapping table exists for stale vector cleanup."""
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS semantic_points (
                    profile_id TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    point_id INTEGER NOT NULL,
                    collection TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (profile_id, chunk_id)
                );

                CREATE INDEX IF NOT EXISTS idx_semantic_points_profile
                    ON semantic_points(profile_id);
                CREATE INDEX IF NOT EXISTS idx_semantic_points_point
                    ON semantic_points(profile_id, point_id);
                CREATE INDEX IF NOT EXISTS idx_semantic_points_collection
                    ON semantic_points(collection);
                """)

    def _ensure_pending_vector_deletions_table(self) -> None:
        """Durable crash-ledger of remote (Qdrant) vector ids awaiting deletion.

        A scheme rebuild deletes local ``semantic_points`` rows in-transaction but
        the matching *remote* vectors are cleaned up by the caller AFTER the
        commit returns.  A crash in that window would orphan the remote vectors
        forever, because the only record of them was the in-memory return value.
        Persisting the stale ids here (in the same transaction as the local
        delete) means the ledger survives a crash and a recovery pass can finish
        the remote cleanup (I4).
        """
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS pending_vector_deletions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    point_id INTEGER,
                    collection TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_pending_vector_deletions_profile
                    ON pending_vector_deletions(profile_id);
                """)

    def _record_pending_vector_deletions(
        self, conn: sqlite3.Connection, points: List[Dict[str, Any]]
    ) -> None:
        """Persist stale remote-vector ids to the crash-ledger inside ``conn``'s
        transaction, so they survive a crash before the caller's remote cleanup."""
        if not points:
            return
        conn.executemany(
            "INSERT INTO pending_vector_deletions "
            "(profile_id, chunk_id, point_id, collection) VALUES (?, ?, ?, ?)",
            [
                (
                    point.get("profile_id"),
                    point.get("chunk_id"),
                    point.get("point_id"),
                    point.get("collection"),
                )
                for point in points
            ],
        )

    def get_pending_vector_deletions(self) -> List[Dict[str, Any]]:
        """Return the durable crash-ledger of remote-vector ids awaiting cleanup."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT id, profile_id, chunk_id, point_id, collection "
                "FROM pending_vector_deletions ORDER BY id"
            ).fetchall()
        return [
            {
                "id": row[0],
                "profile_id": row[1],
                "chunk_id": row[2],
                "point_id": row[3],
                "collection": row[4],
            }
            for row in rows
        ]

    def clear_pending_vector_deletions(self, ledger_ids: Optional[List[int]] = None) -> int:
        """Clear ledger rows once their remote vectors are deleted.

        With ``ledger_ids`` clears exactly those rows (the ids from
        :meth:`get_pending_vector_deletions`); with ``None`` clears the whole
        ledger.  Returns the number of rows removed.
        """
        with self._get_connection() as conn:
            if ledger_ids is None:
                cursor = conn.execute("DELETE FROM pending_vector_deletions")
                return cursor.rowcount
            removed = 0
            for start in range(0, len(ledger_ids), 500):
                batch = ledger_ids[start : start + 500]
                placeholders = ",".join("?" * len(batch))
                cursor = conn.execute(
                    f"DELETE FROM pending_vector_deletions WHERE id IN ({placeholders})",
                    batch,
                )
                removed += cursor.rowcount
            return removed

    def get_live_semantic_point_ids(
        self,
        profile_id: Optional[str],
        collection: Optional[str],
        point_ids: List[Any],
    ) -> set:
        """Return the subset of ``point_ids`` that still have a live mapping.

        A ledger point id may have been legitimately RE-CREATED (deterministic,
        content-derived point ids) by an incremental index pass before the drain
        runs.  Such a point id has a fresh ``semantic_points`` row and its remote
        vector is in active use - it is NOT an orphan and must NOT be remote
        deleted.  This resolves, in a single batched query per group, which of the
        candidate ``point_ids`` still map to a live ``semantic_points`` row for the
        given ``profile_id`` / ``collection``.  When the ledger row recorded no
        ``collection`` we protect the point id across any collection for the
        profile (err toward never deleting a live vector).
        """
        if not point_ids:
            return set()
        live: set = set()
        with self._get_connection() as conn:
            for start in range(0, len(point_ids), 500):
                batch = point_ids[start : start + 500]
                placeholders = ",".join("?" * len(batch))
                if collection is None:
                    sql = (
                        f"SELECT point_id FROM semantic_points "
                        f"WHERE profile_id = ? AND point_id IN ({placeholders})"
                    )
                    params: List[Any] = [profile_id, *batch]
                else:
                    sql = (
                        f"SELECT point_id FROM semantic_points "
                        f"WHERE profile_id = ? AND collection = ? "
                        f"AND point_id IN ({placeholders})"
                    )
                    params = [profile_id, collection, *batch]
                for row in conn.execute(sql, params).fetchall():
                    live.add(row[0])
        return live

    def drain_pending_vector_deletions(
        self,
        delete_remote: Callable[[Optional[str], List[Any]], None],
        *,
        only_collection: Optional[str] = None,
    ) -> Dict[str, int]:
        """Best-effort recovery drain of the remote-vector crash-ledger (I4).

        When a chunk-scheme rebuild deletes the local ``semantic_points`` rows but
        crashes before the caller finishes the matching *remote* (Qdrant) delete,
        the orphaned remote point ids survive only in the durable
        ``pending_vector_deletions`` ledger.  Nothing else will ever clean them up
        because the local mapping is already gone.  This drains that ledger: it
        reads the pending rows, groups them by ``(profile_id, collection)`` so each
        remote delete targets the right collection, and asks
        ``delete_remote(collection, point_ids)`` to remove those remote points.

        Guarantees:

        * **no live-vector loss** - because point ids are deterministic and
          content-derived, an incremental index pass can legitimately RE-CREATE a
          ledger point id (fresh ``semantic_points`` row + remote vector) before
          this drain runs.  Such a revived point id is in active use and is NEVER
          remote deleted; only true orphans (ledger point ids with no live
          ``semantic_points`` mapping) are deleted.  A revived row's ledger debt is
          still cleared on success (the remote vector it pointed to is now owned by
          the live mapping).
        * **fail-safe** - a group's ledger rows are cleared ONLY when its
          ``delete_remote`` returns without raising; a raise leaves that group's
          rows in the ledger for the next attempt (an un-drained row is never
          cleared), while OTHER healthy groups still drain.
        * **idempotent** - an empty ledger (including a re-run after a successful
          drain) is a no-op.

        Best-effort: a remote-delete error for one group is isolated (logged and
        swallowed) and never raises; but the ledger read/clear I/O
        (:meth:`get_pending_vector_deletions` / :meth:`clear_pending_vector_deletions`)
        sits outside the per-group guard and may propagate to the caller (the
        dispatcher drain hook catches it).

        Returns a small counts dict (``rows_drained``, ``rows_remaining``,
        ``groups_failed``) for observability and tests.
        """
        pending = self.get_pending_vector_deletions()
        if not pending:
            return {"rows_drained": 0, "rows_remaining": 0, "groups_failed": 0}
        total_pending = len(pending)
        if only_collection is not None:
            pending = [row for row in pending if row.get("collection") == only_collection]

        groups: Dict[Tuple[Optional[str], Optional[str]], Dict[str, List[Any]]] = {}
        for row in pending:
            key = (row.get("profile_id"), row.get("collection"))
            group = groups.setdefault(key, {"ledger_ids": [], "point_ids": []})
            group["ledger_ids"].append(row["id"])
            point_id = row.get("point_id")
            if point_id is not None:
                group["point_ids"].append(point_id)

        drainable_ids: List[int] = []
        groups_failed = 0
        for (profile_id, collection), group in groups.items():
            point_ids = list(dict.fromkeys(group["point_ids"]))
            try:
                # A group with no remote point ids carries no remote debt; its
                # rows are ledger noise we can clear without a remote call.
                if point_ids:
                    # Partition into true orphans vs. revived point ids: a point id
                    # that still has a live ``semantic_points`` mapping was
                    # legitimately RE-CREATED before this drain ran and must NOT be
                    # deleted (its remote vector is in active use).  Only orphans
                    # (no live mapping) are remote deleted; the revived rows' ledger
                    # debt is still cleared below on success.
                    live = self.get_live_semantic_point_ids(profile_id, collection, point_ids)
                    orphan_ids = [p for p in point_ids if p not in live]
                    if orphan_ids:
                        delete_remote(collection, orphan_ids)
            except Exception as exc:  # best-effort recovery: keep this group's rows
                groups_failed += 1
                logger.warning(
                    "Deferred pending-vector-deletion drain for profile %r / "
                    "collection %r (%d point ids): %s",
                    profile_id,
                    collection,
                    len(point_ids),
                    type(exc).__name__,
                )
                continue
            drainable_ids.extend(group["ledger_ids"])

        rows_drained = self.clear_pending_vector_deletions(drainable_ids) if drainable_ids else 0
        return {
            "rows_drained": rows_drained,
            "rows_remaining": total_pending - rows_drained,
            "groups_failed": groups_failed,
        }

    def _ensure_chunk_summary_audit_columns(self) -> None:
        """Ensure additive chunk summary audit columns exist."""
        with self._get_connection() as conn:
            for column, ddl in (
                ("provider_name", "ALTER TABLE chunk_summaries ADD COLUMN provider_name TEXT"),
                ("profile_id", "ALTER TABLE chunk_summaries ADD COLUMN profile_id TEXT"),
                (
                    "prompt_fingerprint",
                    "ALTER TABLE chunk_summaries ADD COLUMN prompt_fingerprint TEXT",
                ),
                ("audit_metadata", "ALTER TABLE chunk_summaries ADD COLUMN audit_metadata TEXT"),
            ):
                if self._check_column_exists(conn, "chunk_summaries", column):
                    continue
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError:
                    logger.debug("Could not add chunk_summaries.%s", column)

    def _init_database(self):
        """Initialize database and create schema if needed."""
        with self._get_connection() as conn:
            # Enable WAL mode for better concurrency
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 5000")  # 5 second timeout
            # Enable foreign keys
            conn.execute("PRAGMA foreign_keys = ON")

            # Enable FTS5 if available
            self._check_fts5_support(conn)

            # Check if this is a simple BM25 index (no schema_version table)
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='bm25_content'"
            )
            if cursor.fetchone():
                # This is a simple BM25 index, don't try to initialize schema
                logger.info(f"Using existing BM25 index at {self.db_path}")
                return

            # Check if schema exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
            )
            if not cursor.fetchone():
                self._init_schema(conn)
                logger.info(f"Initialized database schema at {self.db_path}")
            else:
                logger.info(f"Using existing database at {self.db_path}")

                # Check for missing columns that may need migration
                if not self._check_column_exists(conn, "files", "content_hash"):
                    logger.warning(
                        "Column 'content_hash' missing from 'files' table - migration may be needed"
                    )
                if not self._check_column_exists(conn, "files", "is_deleted"):
                    logger.warning(
                        "Column 'is_deleted' missing from 'files' table - migration may be needed"
                    )
                if not self._check_column_exists(conn, "files", "deleted_at"):
                    logger.warning(
                        "Column 'deleted_at' missing from 'files' table - migration may be needed"
                    )

    def _run_migrations(self):
        """Run any pending database migrations."""
        # Skip migrations for BM25-only databases
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='bm25_content'"
            )
            if (
                cursor.fetchone()
                and not conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
                ).fetchone()
            ):
                logger.debug("Skipping migrations for BM25-only database")
                return

        migrations_dir = files("mcp_server.storage").joinpath("migrations")
        if not migrations_dir.is_dir():
            raise RuntimeError(
                "Installed SQLite migration resources are missing; reinstall index-it-mcp"
            )
        migration_files = sorted(
            (item for item in migrations_dir.iterdir() if item.name.endswith(".sql")),
            key=lambda item: item.name,
        )
        if not migration_files:
            raise RuntimeError(
                "Installed SQLite migration resources are empty; reinstall index-it-mcp"
            )

        with self._get_connection() as conn:
            # Get current schema version
            try:
                cursor = conn.execute("SELECT MAX(version) FROM schema_version")
                current_version = cursor.fetchone()[0] or 0
            except sqlite3.OperationalError:
                current_version = 0

            # Reconcile pre-v7 partial scripts once, including falsely advanced versions.
            for migration_file in migration_files:
                # Extract version from filename (e.g., "002_relative_paths.sql" -> 2)
                try:
                    version = int(migration_file.name.split("_")[0])
                except (ValueError, IndexError):
                    continue

                if version > current_version or (current_version < 7 and 2 <= version <= 7):
                    logger.info(f"Running migration {migration_file.name}")
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        statement = ""
                        for line in migration_file.read_text(encoding="utf-8").splitlines(True):
                            statement += line
                            if not sqlite3.complete_statement(statement):
                                continue
                            try:
                                conn.execute(statement)
                            except sqlite3.OperationalError as exc:
                                ddl = re.sub(
                                    r"^\s*--[^\n]*", "", statement, flags=re.MULTILINE
                                ).strip()
                                addition = re.match(
                                    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)\b",
                                    ddl,
                                    re.IGNORECASE,
                                )
                                if not (
                                    addition
                                    and str(exc).lower().startswith("duplicate column name:")
                                    and self._check_column_exists(conn, *addition.groups())
                                ):
                                    raise
                            statement = ""
                        if re.sub(r"--[^\n]*", "", statement).strip():
                            raise sqlite3.OperationalError("Incomplete migration statement")
                        conn.commit()
                        logger.info(f"Completed migration to version {version}")
                    except Exception:
                        conn.rollback()
                        raise

        self._add_repo_identity_columns()

    def _add_repo_identity_columns(self) -> None:
        """Additive migration: ensure repositories has tracked_branch and git_common_dir."""
        with self._get_connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repositories'"
            ).fetchone():
                # Permit health_check to diagnose incomplete, version-stamped databases.
                return
            for col in ("tracked_branch", "git_common_dir"):
                if not self._check_column_exists(conn, "repositories", col):
                    conn.execute(f"ALTER TABLE repositories ADD COLUMN {col} TEXT")
                    logger.debug(f"Added column repositories.{col}")

    @contextmanager
    def _get_connection(self):
        """Get a database connection with proper error handling.

        When a ConnectionPool is attached, borrows a connection from it
        (skipping open/close) and applies row_factory + foreign_keys each
        time.  Commit/rollback semantics are identical to the non-pool path.
        """
        existing = getattr(self._connection_state, "connection", None)
        if existing is not None:
            # A nested method must neither borrow another pool slot nor commit
            # its caller's transaction. Savepoints isolate caught inner errors.
            savepoint = f"mcp_nested_{uuid.uuid4().hex}"
            if not existing.in_transaction:
                existing.execute("BEGIN")
            existing.execute(f"SAVEPOINT {savepoint}")
            try:
                yield existing
                existing.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                existing.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                existing.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            return
        if self._pool is not None:
            with self._pool.acquire() as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                try:
                    # Preopened connections may cache the empty schema before migrations.
                    conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
                    self._connection_state.connection = conn
                    yield conn
                    conn.commit()
                except sqlite3.OperationalError as e:
                    conn.rollback()
                    if self._mark_readonly_from_storage_failure(e) is not None:
                        raise TransientArtifactError(str(e)) from e
                    raise
                except BaseException:
                    conn.rollback()
                    raise
                finally:
                    self._connection_state.connection = None
        else:
            if self._memory_uri is not None:
                if self._memory_anchor is None:
                    raise RuntimeError("In-memory SQLiteStore is closed")
                conn = sqlite3.connect(self._memory_uri, uri=True)
            else:
                conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                self._connection_state.connection = conn
                yield conn
                conn.commit()
            except sqlite3.OperationalError as e:
                conn.rollback()
                if self._mark_readonly_from_storage_failure(e) is not None:
                    raise TransientArtifactError(str(e)) from e
                raise
            except BaseException:
                conn.rollback()
                raise
            finally:
                self._connection_state.connection = None
                conn.close()

    def _check_fts5_support(self, conn: sqlite3.Connection) -> bool:
        """Check if FTS5 is supported in this SQLite build."""
        try:
            cursor = conn.execute("PRAGMA compile_options;")
            options = [row[0] for row in cursor]
            fts5_enabled = any("ENABLE_FTS5" in option for option in options)
            if fts5_enabled:
                logger.info("FTS5 support confirmed")
            else:
                logger.warning("FTS5 may not be available in this SQLite build")
            return fts5_enabled
        except Exception as e:
            logger.warning(f"Could not check FTS5 support: {e}")
            return False

    def _check_column_exists(self, conn: sqlite3.Connection, table: str, column: str) -> bool:
        """Check if a column exists in a table."""
        try:
            cursor = conn.execute(f"PRAGMA table_info({table})")
            columns = [row[1] for row in cursor.fetchall()]
            return column in columns
        except Exception as e:
            logger.warning(f"Could not check column {table}.{column}: {e}")
            return False

    def _init_schema(self, conn: sqlite3.Connection):
        """Initialize the database schema."""
        # Create core tables
        conn.executescript("""
            -- Schema Version
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                description TEXT
            );

            -- Repositories
            CREATE TABLE IF NOT EXISTS repositories (
                id INTEGER PRIMARY KEY,
                path TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                metadata JSON
            );

            -- Files
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                repository_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                language TEXT,
                size INTEGER,
                hash TEXT,
                content_hash TEXT,
                last_modified TIMESTAMP,
                indexed_at TIMESTAMP,
                is_deleted BOOLEAN DEFAULT FALSE,
                deleted_at TIMESTAMP,
                metadata JSON,
                FOREIGN KEY (repository_id) REFERENCES repositories(id),
                UNIQUE(repository_id, relative_path)
            );

            CREATE INDEX IF NOT EXISTS idx_files_language ON files(language);
            CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash);
            CREATE INDEX IF NOT EXISTS idx_files_content_hash ON files(content_hash);
            CREATE INDEX IF NOT EXISTS idx_files_deleted ON files(is_deleted);
            CREATE INDEX IF NOT EXISTS idx_files_relative_path ON files(relative_path);

            -- Symbols
            CREATE TABLE IF NOT EXISTS symbols (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,
                column_start INTEGER,
                column_end INTEGER,
                signature TEXT,
                documentation TEXT,
                token_count INTEGER,
                token_model TEXT DEFAULT 'cl100k_base',
                metadata JSON,
                FOREIGN KEY (file_id) REFERENCES files(id)
            );

            CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
            CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
            CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
            CREATE INDEX IF NOT EXISTS idx_symbols_token_count ON symbols(token_count);

            -- Code Chunks (for TreeSitter integration)
            CREATE TABLE IF NOT EXISTS code_chunks (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL,
                symbol_id INTEGER,
                content TEXT NOT NULL,
                content_start INTEGER NOT NULL,
                content_end INTEGER NOT NULL,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,

                -- 5 Stable IDs from TreeSitter Chunker
                chunk_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                treesitter_file_id TEXT NOT NULL,
                symbol_hash TEXT,
                definition_id TEXT,

                -- Token counting
                token_count INTEGER,
                token_model TEXT DEFAULT 'cl100k_base',

                -- Metadata
                chunk_type TEXT NOT NULL DEFAULT 'code',
                language TEXT,
                node_type TEXT,
                parent_chunk_id TEXT,
                depth INTEGER DEFAULT 0,
                chunk_index INTEGER NOT NULL DEFAULT 0,
                metadata JSON,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
                FOREIGN KEY (symbol_id) REFERENCES symbols(id) ON DELETE SET NULL,
                UNIQUE(file_id, chunk_id)
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_file_id ON code_chunks(file_id);
            CREATE INDEX IF NOT EXISTS idx_chunks_symbol_id ON code_chunks(symbol_id);
            CREATE INDEX IF NOT EXISTS idx_chunks_chunk_id ON code_chunks(chunk_id);
            CREATE INDEX IF NOT EXISTS idx_chunks_node_id ON code_chunks(node_id);
            CREATE INDEX IF NOT EXISTS idx_chunks_treesitter_file_id ON code_chunks(treesitter_file_id);
            CREATE INDEX IF NOT EXISTS idx_chunks_symbol_hash ON code_chunks(symbol_hash);
            CREATE INDEX IF NOT EXISTS idx_chunks_definition_id ON code_chunks(definition_id);
            CREATE INDEX IF NOT EXISTS idx_chunks_chunk_type ON code_chunks(chunk_type);
            CREATE INDEX IF NOT EXISTS idx_chunks_language ON code_chunks(language);
            CREATE INDEX IF NOT EXISTS idx_chunks_parent_chunk_id ON code_chunks(parent_chunk_id);
            CREATE INDEX IF NOT EXISTS idx_chunks_token_count ON code_chunks(token_count);

            -- Semantic point mappings for profile-scoped stale vector cleanup
            CREATE TABLE IF NOT EXISTS semantic_points (
                profile_id TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                point_id INTEGER NOT NULL,
                collection TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (profile_id, chunk_id)
            );

            CREATE INDEX IF NOT EXISTS idx_semantic_points_profile ON semantic_points(profile_id);
            CREATE INDEX IF NOT EXISTS idx_semantic_points_point ON semantic_points(profile_id, point_id);
            CREATE INDEX IF NOT EXISTS idx_semantic_points_collection ON semantic_points(collection);
            
            -- Chunk Summaries (for LLM-generated summaries)
            CREATE TABLE IF NOT EXISTS chunk_summaries (
                chunk_hash TEXT PRIMARY KEY,
                file_id INTEGER NOT NULL,
                chunk_start INTEGER NOT NULL,
                chunk_end INTEGER NOT NULL,
                symbol TEXT,
                summary_text TEXT NOT NULL,
                is_authoritative BOOLEAN DEFAULT 0,
                llm_model TEXT,
                provider_name TEXT,
                profile_id TEXT,
                prompt_fingerprint TEXT,
                audit_metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_chunk_summaries_file_bounds ON chunk_summaries(file_id, chunk_start, chunk_end);

            -- Imports
            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL,
                imported_path TEXT NOT NULL,
                imported_name TEXT,
                alias TEXT,
                line_number INTEGER,
                is_relative BOOLEAN,
                metadata JSON,
                FOREIGN KEY (file_id) REFERENCES files(id)
            );
            
            CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_id);
            CREATE INDEX IF NOT EXISTS idx_imports_path ON imports(imported_path);
            
            -- References (using symbol_references to avoid keyword conflict)
            CREATE TABLE IF NOT EXISTS symbol_references (
                id INTEGER PRIMARY KEY,
                symbol_id INTEGER NOT NULL,
                file_id INTEGER NOT NULL,
                line_number INTEGER NOT NULL,
                column_number INTEGER,
                reference_kind TEXT,
                metadata JSON,
                FOREIGN KEY (symbol_id) REFERENCES symbols(id),
                FOREIGN KEY (file_id) REFERENCES files(id)
            );
            
            CREATE INDEX IF NOT EXISTS idx_references_symbol ON symbol_references(symbol_id);
            CREATE INDEX IF NOT EXISTS idx_references_file ON symbol_references(file_id);
            
            -- Full-Text Search tables
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_symbols USING fts5(
                name,
                documentation,
                content=symbols,
                content_rowid=id
            );
            
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_code USING fts5(
                content,
                file_id UNINDEXED
            );
            
            -- Fuzzy Search
            CREATE TABLE IF NOT EXISTS symbol_trigrams (
                symbol_id INTEGER NOT NULL,
                trigram TEXT NOT NULL,
                FOREIGN KEY (symbol_id) REFERENCES symbols(id)
            );
            
            CREATE INDEX IF NOT EXISTS idx_trigrams ON symbol_trigrams(trigram);
            CREATE INDEX IF NOT EXISTS idx_trigrams_symbol_id ON symbol_trigrams(symbol_id);
            
            -- Embeddings
            CREATE TABLE IF NOT EXISTS embeddings (
                id INTEGER PRIMARY KEY,
                file_id INTEGER,
                symbol_id INTEGER,
                chunk_start INTEGER,
                chunk_end INTEGER,
                embedding BLOB NOT NULL,
                model_version TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                metadata JSON,
                FOREIGN KEY (file_id) REFERENCES files(id),
                FOREIGN KEY (symbol_id) REFERENCES symbols(id)
            );
            
            CREATE INDEX IF NOT EXISTS idx_embeddings_file ON embeddings(file_id);
            CREATE INDEX IF NOT EXISTS idx_embeddings_symbol ON embeddings(symbol_id);
            
            -- Cache Tables
            CREATE TABLE IF NOT EXISTS query_cache (
                query_hash TEXT PRIMARY KEY,
                query_text TEXT NOT NULL,
                result JSON NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                hit_count INTEGER DEFAULT 0
            );
            
            CREATE INDEX IF NOT EXISTS idx_cache_expires ON query_cache(expires_at);
            
            CREATE TABLE IF NOT EXISTS parse_cache (
                file_hash TEXT PRIMARY KEY,
                ast JSON NOT NULL,
                parser_version TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            -- Migration Log
            CREATE TABLE IF NOT EXISTS migrations (
                id INTEGER PRIMARY KEY,
                version_from INTEGER,
                version_to INTEGER,
                executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                duration_ms INTEGER,
                status TEXT
            );
            
            -- Index Configuration
            CREATE TABLE IF NOT EXISTS index_config (
                id INTEGER PRIMARY KEY,
                config_key TEXT UNIQUE NOT NULL,
                config_value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                description TEXT
            );

            -- File Moves Tracking
            CREATE TABLE IF NOT EXISTS file_moves (
                id INTEGER PRIMARY KEY,
                repository_id INTEGER NOT NULL,
                old_relative_path TEXT NOT NULL,
                new_relative_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                moved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                move_type TEXT CHECK(move_type IN ('rename', 'relocate', 'restructure')),
                FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_moves_content_hash ON file_moves(content_hash);
            CREATE INDEX IF NOT EXISTS idx_moves_timestamp ON file_moves(moved_at);
            CREATE INDEX IF NOT EXISTS idx_moves_old_path ON file_moves(old_relative_path);
            CREATE INDEX IF NOT EXISTS idx_moves_new_path ON file_moves(new_relative_path);

            -- Insert initial index configuration
            INSERT OR IGNORE INTO index_config (config_key, config_value, description) VALUES
                ('embedding_model', 'voyage-code-3', 'Current embedding model used for vector search'),
                ('model_dimension', '1024', 'Embedding vector dimension'),
                ('distance_metric', 'cosine', 'Distance metric for vector similarity'),
                ('index_version', '1.0', 'Index schema version');

            -- Insert initial schema version
            INSERT INTO schema_version (version, description)
            VALUES (2, 'Initial schema creation with content tracking and soft delete support');
        """)

        # Create triggers for FTS
        conn.executescript("""
            -- Triggers to keep FTS in sync with symbols table
            CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols
            BEGIN
                INSERT INTO fts_symbols(rowid, name, documentation)
                VALUES (new.id, new.name, new.documentation);
            END;
            
            CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols
            BEGIN
                DELETE FROM fts_symbols WHERE rowid = old.id;
            END;
            
            CREATE TRIGGER IF NOT EXISTS symbols_au AFTER UPDATE ON symbols
            BEGIN
                UPDATE fts_symbols 
                SET name = new.name, documentation = new.documentation
                WHERE rowid = new.id;
            END;
        """)

    # Repository operations
    def create_repository(self, path: str, name: str, metadata: Optional[Dict] = None) -> int:
        """Create a new repository entry."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """INSERT INTO repositories (path, name, metadata) 
                   VALUES (?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET 
                   name=excluded.name, 
                   metadata=excluded.metadata,
                   updated_at=CURRENT_TIMESTAMP""",
                (path, name, json.dumps(metadata or {})),
            )
            cursor = conn.execute("SELECT id FROM repositories WHERE path = ?", (path,))
            return cursor.fetchone()[0]

    def get_repository(self, path: str) -> Optional[Dict]:
        """Get repository by path."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM repositories WHERE path = ?", (path,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def ensure_repository_row(
        self,
        repo_path: Union[str, Path],
        name: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> int:
        """Return the internal repositories.id row for repo_path, creating it if needed."""
        resolved_path = str(Path(repo_path).expanduser().resolve())
        repo_name = name if name is not None else Path(resolved_path).name
        return self.create_repository(resolved_path, repo_name, metadata)

    # File operations
    def store_file(
        self,
        repository_id: int,
        path: Optional[Union[str, Path]] = None,
        relative_path: Optional[Union[str, Path]] = None,
        language: Optional[str] = None,
        size: Optional[int] = None,
        hash: Optional[str] = None,
        content_hash: Optional[str] = None,
        metadata: Optional[Dict] = None,
        is_deleted: bool = False,
        deleted_at: Optional[Union[str, datetime]] = None,
        **kwargs: Any,
    ) -> int:
        """Store file information using relative paths and content hashes."""
        self._require_writable()
        file_path_arg = kwargs.pop("file_path", None)
        resolved_path = Path(path or file_path_arg) if path or file_path_arg else None

        if not resolved_path and not relative_path:
            raise ValueError("Either 'path' or 'relative_path' must be provided to store_file")

        relative_path_str: Optional[str] = None
        if relative_path is not None:
            relative_path_str = str(relative_path).replace("\\", "/")
        elif resolved_path:
            try:
                relative_path_str = self.path_resolver.normalize_path(resolved_path)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(f"Could not normalize path {resolved_path}: {exc}")
                relative_path_str = str(resolved_path).replace("\\", "/")

        if not relative_path_str:
            raise ValueError("Relative path could not be determined for file storage")

        # Normalize stored path string
        stored_path = str(resolved_path).replace("\\", "/") if resolved_path else relative_path_str

        # Determine filesystem metadata when available
        path_exists = resolved_path.exists() if resolved_path else False
        file_size = size
        if file_size is None and path_exists:
            try:
                file_size = resolved_path.stat().st_size
            except OSError as exc:  # pragma: no cover - defensive
                logger.warning(f"Could not read size for {resolved_path}: {exc}")

        file_hash = hash
        if file_hash is None and path_exists:
            try:
                file_hash = self.path_resolver.compute_file_hash(resolved_path)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(f"Failed to compute file hash for {resolved_path}: {exc}")

        content_hash_value = content_hash
        if content_hash_value is None and path_exists:
            try:
                content_hash_value = self.path_resolver.compute_content_hash(resolved_path)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(f"Failed to compute content hash for {resolved_path}: {exc}")

        last_modified_value = (
            datetime.fromtimestamp(resolved_path.stat().st_mtime, tz=timezone.utc)
            if path_exists
            else datetime.now(timezone.utc)
        )
        indexed_at_value = datetime.now(timezone.utc)

        with self._get_connection() as conn:
            # Store using relative path as primary identifier
            cursor = conn.execute(
                """INSERT INTO files 
                   (repository_id, path, relative_path, language, size, hash, content_hash,
                    last_modified, indexed_at, metadata, is_deleted, deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(repository_id, relative_path) DO UPDATE SET
                   path=excluded.path,
                   language=excluded.language,
                   size=excluded.size,
                   hash=excluded.hash,
                   content_hash=excluded.content_hash,
                   last_modified=excluded.last_modified,
                   indexed_at=excluded.indexed_at,
                   metadata=excluded.metadata,
                   is_deleted=excluded.is_deleted,
                   deleted_at=excluded.deleted_at""",
                (
                    repository_id,
                    stored_path,
                    relative_path_str,
                    language,
                    file_size,
                    file_hash,
                    content_hash_value,
                    last_modified_value,
                    indexed_at_value,
                    json.dumps(metadata or {}),
                    bool(is_deleted),
                    deleted_at,
                ),
            )
            cursor = conn.execute(
                "SELECT id FROM files WHERE repository_id = ? AND relative_path = ?",
                (repository_id, relative_path_str),
            )
            row = cursor.fetchone()
            if row is None:  # pragma: no cover - defensive
                raise RuntimeError(f"Failed to resolve stored file row for {relative_path_str}")
            return row[0]

    def get_file(
        self, file_path: Union[str, Path], repository_id: Optional[int] = None
    ) -> Optional[Dict]:
        """Get file by path (relative or absolute)."""
        # Try to normalize path if it's absolute
        try:
            relative_path = self.path_resolver.normalize_path(file_path)
        except ValueError:
            # Path might already be relative
            relative_path = str(file_path).replace("\\", "/")

        with self._get_connection() as conn:
            if repository_id:
                cursor = conn.execute(
                    "SELECT * FROM files WHERE relative_path = ? AND repository_id = ? AND is_deleted = FALSE",
                    (relative_path, repository_id),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM files WHERE relative_path = ? AND is_deleted = FALSE",
                    (relative_path,),
                )
            row = cursor.fetchone()
            if row is None:
                # Fallback: search by absolute path stored in 'path' column
                abs_path = str(file_path).replace("\\", "/")
                if repository_id:
                    cursor = conn.execute(
                        "SELECT * FROM files WHERE path = ? AND repository_id = ? AND is_deleted = FALSE",
                        (abs_path, repository_id),
                    )
                else:
                    cursor = conn.execute(
                        "SELECT * FROM files WHERE path = ? AND is_deleted = FALSE",
                        (abs_path,),
                    )
                row = cursor.fetchone()
            return dict(row) if row else None

    def get_file_by_path(
        self, file_path: Union[str, Path], repository_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """Get a file record by path, returning normalized paths and content hash."""
        try:
            relative_path = self.path_resolver.normalize_path(file_path)
        except ValueError:
            relative_path = str(file_path).replace("\\", "/")

        with self._get_connection() as conn:
            query = "SELECT * FROM files WHERE relative_path = ? AND is_deleted = FALSE"
            params: Tuple[Union[str, int], ...]

            if repository_id is not None:
                query += " AND repository_id = ?"
                params = (relative_path, repository_id)
            else:
                params = (relative_path,)

            cursor = conn.execute(query, params)
            row = cursor.fetchone()
            return self._normalize_file_record(row) if row else None

    def get_all_files(self, repository_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get all non-deleted files with normalized paths and content hashes."""
        with self._get_connection() as conn:
            query = "SELECT * FROM files WHERE is_deleted = FALSE"
            params: Tuple[Union[str, int], ...] = ()

            if repository_id is not None:
                query += " AND repository_id = ?"
                params = (repository_id,)

            cursor = conn.execute(query, params)
            return [self._normalize_file_record(row) for row in cursor.fetchall()]

    def _normalize_file_record(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Normalize file record paths and include content hash."""
        record = dict(row)
        original_path = record.get("path") or ""
        relative_path = record.get("relative_path")

        normalized_path: Optional[str] = None
        if relative_path:
            normalized_path = str(relative_path).replace("\\", "/")
        elif original_path:
            try:
                normalized_path = self.path_resolver.normalize_path(original_path)
            except Exception:
                normalized_path = str(original_path).replace("\\", "/")

        if normalized_path:
            record["path"] = normalized_path
            record["relative_path"] = normalized_path
            try:
                record["absolute_path"] = str(self.path_resolver.resolve_path(normalized_path))
            except Exception:
                record["absolute_path"] = original_path or normalized_path
        else:
            record["absolute_path"] = original_path

        record["content_hash"] = record.get("content_hash") or record.get("hash")
        return record

    # Symbol operations
    def store_symbol(
        self,
        file_id: int,
        name: str,
        kind: str,
        line_start: int,
        line_end: int,
        column_start: Optional[int] = None,
        column_end: Optional[int] = None,
        signature: Optional[str] = None,
        documentation: Optional[str] = None,
        metadata: Optional[Dict] = None,
        token_count: Optional[int] = None,
        token_model: Optional[str] = None,
    ) -> int:
        """Store a symbol definition with optional token counting."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """INSERT INTO symbols
                   (file_id, name, kind, line_start, line_end, column_start,
                    column_end, signature, documentation, metadata, token_count, token_model)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    file_id,
                    name,
                    kind,
                    line_start,
                    line_end,
                    column_start,
                    column_end,
                    signature,
                    documentation,
                    json.dumps(metadata or {}),
                    token_count,
                    token_model or "cl100k_base" if token_count is not None else None,
                ),
            )
            symbol_id = cursor.lastrowid

            # Generate and store trigrams for fuzzy search
            self._store_trigrams(conn, symbol_id, name)

            return symbol_id

    def _store_trigrams(self, conn: sqlite3.Connection, symbol_id: int, name: str):
        """Generate and store trigrams for a symbol name."""
        # Generate trigrams
        trigrams = set()
        padded_name = f"  {name}  "  # Pad with spaces for edge trigrams
        for i in range(len(padded_name) - 2):
            trigram = padded_name[i : i + 3].lower()
            trigrams.add(trigram)

        # Store trigrams
        for trigram in trigrams:
            conn.execute(
                "INSERT INTO symbol_trigrams (symbol_id, trigram) VALUES (?, ?)",
                (symbol_id, trigram),
            )

    def get_symbol(self, name: str, kind: Optional[str] = None) -> List[Dict]:
        """Get symbols by name and optionally kind."""
        with self._get_connection() as conn:
            if kind:
                cursor = conn.execute(
                    """SELECT s.*, f.path as file_path
                       FROM symbols s
                       JOIN files f ON s.file_id = f.id
                       WHERE s.name = ? AND s.kind = ?""",
                    (name, kind),
                )
            else:
                cursor = conn.execute(
                    """SELECT s.*, f.path as file_path
                       FROM symbols s
                       JOIN files f ON s.file_id = f.id
                       WHERE s.name = ?""",
                    (name,),
                )
            return [dict(row) for row in cursor.fetchall()]

    def count_symbols_for_file(self, file_id: int) -> int:
        """Return the number of symbols stored for a given file."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM symbols WHERE file_id = ?", (file_id,))
            return cursor.fetchone()[0]

    # Code Chunk operations
    # ------------------------------------------------------------------
    # Chunk identity-scheme marker + central guard (CHUNKERSAFE Lane A)
    # ------------------------------------------------------------------
    def _current_scheme(self) -> str:
        return current_chunk_id_scheme()

    def _set_config(
        self,
        conn: sqlite3.Connection,
        key: str,
        value: str,
        description: Optional[str] = None,
    ) -> None:
        conn.execute(
            "INSERT INTO index_config (config_key, config_value, description) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(config_key) DO UPDATE SET "
            "config_value=excluded.config_value, "
            "description=COALESCE(excluded.description, index_config.description), "
            "updated_at=CURRENT_TIMESTAMP",
            (key, value, description),
        )

    def _delete_config(self, conn: sqlite3.Connection, key: str) -> None:
        conn.execute("DELETE FROM index_config WHERE config_key = ?", (key,))

    def get_chunk_scheme_marker(self) -> Optional[str]:
        """Return the persisted chunk-identity-scheme marker, or None if unmarked."""
        with self._get_connection() as conn:
            return _scheme_read_config_value(conn, CHUNK_SCHEME_MARKER_KEY)

    def get_chunk_scheme_status(self) -> Tuple[str, Optional[str], str]:
        """Return ``(status, marker, target)`` for the current scheme (see
        :func:`evaluate_chunk_scheme`)."""
        with self._get_connection() as conn:
            return evaluate_chunk_scheme(conn, self._current_scheme())

    def _stamp_scheme(self, conn: sqlite3.Connection, target: str) -> None:
        """Persist the scheme marker, keeping it coherent with the manifest
        ``chunk_identity_algorithm`` fingerprint."""
        self._set_config(
            conn,
            CHUNK_SCHEME_MARKER_KEY,
            target,
            "Chunk identity scheme stamped on first build (CHUNKERSAFE)",
        )
        self._set_config(
            conn,
            CHUNK_IDENTITY_ALGORITHM_KEY,
            target,
            "Chunk identity algorithm reconciled with manifest_v2 contract",
        )

    def _assert_chunk_scheme_writable(
        self,
        conn: sqlite3.Connection,
        *,
        chunk_type: str = "code",
        target: Optional[str] = None,
    ) -> None:
        """Central write choke point: refuse chunker writes across a scheme
        boundary; stamp the scheme on the first write into an empty index.

        Synthetic ``document`` rows (history docs) are not chunker-derived and
        bypass the guard entirely.
        """
        if chunk_type == PRESERVED_CHUNK_TYPE:
            return
        self._require_writable()
        target = target or self._current_scheme()
        status, marker, target = evaluate_chunk_scheme(conn, target)
        if status == "rebuilding":
            # A rebuild is in-flight: ``marker`` is the persisted rebuild target.
            # ONLY the explicitly-scoped rebuild writer (whose target equals that
            # rebuild target) may commit rows during the begin->finalize window.
            # An ordinary writer (target == current runtime scheme) that does not
            # match would otherwise commit old-scheme rows that finalize then
            # stamps as the new scheme - the exact mixed-scheme corruption the
            # guard exists to prevent.
            if target != marker:
                raise ChunkSchemeMismatchError(
                    f"code_chunks is rebuilding to {marker!r}; refusing a write "
                    f"scoped to {target!r}. Only the rebuild writer (scheme_target="
                    f"{marker!r}) may write until the rebuild is finalized.",
                    expected=marker,
                    found=target,
                    state=status,
                )
            return
        if status in _SCHEME_WRITE_BLOCKING:
            raise ChunkSchemeMismatchError(
                f"code_chunks scheme is {status!r} (index marker={marker!r}, "
                f"current scheme={target!r}); refusing chunker write until an "
                "atomic rebuild completes.",
                expected=target,
                found=marker,
                state=status,
            )
        if status in ("empty", "compatible_legacy"):
            # First write into an empty index, or the first write into a legacy
            # unmarked (v3-built == v1) index: durably stamp the scheme so the
            # marker becomes authoritative (B1 auto-stamp).
            self._stamp_scheme(conn, target)

    def _assert_chunk_scheme_deletable(
        self, conn: sqlite3.Connection, *, target: Optional[str] = None
    ) -> None:
        """Refuse scheme-dependent deletes over an incompatible index (a delete of
        old-scheme rows before a mismatched store would destroy the coherent
        index)."""
        target = target or self._current_scheme()
        status, marker, target = evaluate_chunk_scheme(conn, target)
        if status == "rebuilding":
            # Symmetric with ``_assert_chunk_scheme_writable``: during the
            # begin->finalize rebuild window ONLY the explicitly-scoped rebuild
            # writer (whose target equals the persisted rebuild target) may touch
            # code_chunks. An ordinary cross-scheme deleter would otherwise erase
            # old-scheme rows that finalize then restamps, matching the mixed-scheme
            # corruption the guard exists to prevent -- so refuse it exactly as the
            # writable assert refuses the corresponding insert.
            if target != marker:
                raise ChunkSchemeMismatchError(
                    f"code_chunks is rebuilding to {marker!r}; refusing a delete "
                    f"scoped to {target!r}. Only the rebuild writer (scheme_target="
                    f"{marker!r}) may modify chunks until the rebuild is finalized.",
                    expected=marker,
                    found=target,
                    state=status,
                )
            return
        if status in _SCHEME_WRITE_BLOCKING:
            raise ChunkSchemeMismatchError(
                f"code_chunks scheme is {status!r} (index marker={marker!r}, "
                f"current scheme={target!r}); refusing chunk delete until an "
                "atomic rebuild completes.",
                expected=target,
                found=marker,
                state=status,
            )

    def _collect_stale_chunk_artifacts(
        self, conn: sqlite3.Connection, profile_id: Optional[str]
    ) -> Dict[str, Any]:
        """Gather chunker-derived ids (preserving synthetic history/document rows)
        across code_chunks, chunk_summaries, and semantic_points, so the rebuild
        can delete locally and hand stale remote-vector ids to a semantic indexer.
        """
        preserved = [
            row[0]
            for row in conn.execute(
                "SELECT chunk_id FROM code_chunks WHERE chunk_type = ?",
                (PRESERVED_CHUNK_TYPE,),
            ).fetchall()
        ]

        def _is_preserved(identifier: Optional[str]) -> bool:
            if identifier is None:
                return False
            if identifier.startswith("history:"):
                return True
            for base in preserved:
                if identifier == base or identifier.startswith(f"{base}:"):
                    return True
            return False

        stale_chunk_ids = [
            row[0]
            for row in conn.execute(
                "SELECT chunk_id FROM code_chunks WHERE chunk_type != ?",
                (PRESERVED_CHUNK_TYPE,),
            ).fetchall()
        ]

        stale_summary_hashes = [
            row[0]
            for row in conn.execute("SELECT chunk_hash FROM chunk_summaries").fetchall()
            if not _is_preserved(row[0])
        ]

        # The scheme marker is DB-wide, so a rebuild must invalidate EVERY
        # profile's stale semantic-point mappings - not just ``profile_id``.
        # Restricting to one profile would leave other profiles' old-scheme
        # mappings (and their orphaned remote vectors) behind.  ``profile_id`` is
        # retained only as documentation of the caller's primary profile; the
        # per-point ``profile_id``/``collection`` is returned so remote cleanup can
        # target the right collection for each.
        point_rows = conn.execute(
            "SELECT profile_id, chunk_id, point_id, collection FROM semantic_points"
        ).fetchall()
        stale_points = [
            {
                "profile_id": row[0],
                "chunk_id": row[1],
                "point_id": row[2],
                "collection": row[3],
            }
            for row in point_rows
            if not _is_preserved(row[1])
        ]
        return {
            "chunk_ids": stale_chunk_ids,
            "summary_hashes": stale_summary_hashes,
            "points": stale_points,
        }

    def begin_chunk_scheme_rebuild(
        self, target_scheme: Optional[str] = None, *, profile_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Phase 1 of an atomic/resumable rebuild.

        In a single transaction: collect the stale remote-vector ids, delete the
        chunker-derived rows from ``code_chunks`` / ``chunk_summaries`` /
        ``semantic_points`` (PRESERVING synthetic history/document rows), and mark
        the DB ``rebuilding`` (which trips the guard/readiness).  The scheme marker
        is NOT flipped here, so a crash after this commit leaves a blocked,
        resumable state - never a half-populated "ready" index.

        Returns the stale ids so a caller holding a semantic indexer can delete the
        matching remote Qdrant vectors.
        """
        self._require_writable()
        target = target_scheme or self._current_scheme()
        with self._get_connection() as conn:
            stale = self._collect_stale_chunk_artifacts(conn, profile_id)
            self._set_config(
                conn,
                CHUNK_SCHEME_REBUILD_KEY,
                target,
                "Chunk-scheme rebuild in progress (CHUNKERSAFE)",
            )
            self._delete_chunker_rows(conn, stale)
        return {"target_scheme": target, "profile_id": profile_id, **stale}

    def finalize_chunk_scheme_rebuild(self, target_scheme: Optional[str] = None) -> None:
        """Phase 2: flip the scheme marker to the recorded rebuild target and clear
        the rebuilding flag, in one transaction - but ONLY after validating that:

        1. a rebuild is actually in progress (a recorded rebuild target exists);
        2. the caller's ``target_scheme`` (if supplied) matches that recorded
           target - never let a caller stamp a scheme the rebuild did not build;
        3. repopulation actually happened (chunker rows are present) - refusing to
           stamp an empty index as "ready", which would advertise a coherent index
           over zero rows.

        Without these checks, finalize is a no-op rubber stamp that can flip the
        marker over an unpopulated (or wrong-scheme) DB.
        """
        self._require_writable()
        with self._get_connection() as conn:
            recorded = _scheme_read_config_value(conn, CHUNK_SCHEME_REBUILD_KEY)
            if recorded is None:
                raise ChunkSchemeMismatchError(
                    "finalize_chunk_scheme_rebuild called with no rebuild in "
                    "progress (no recorded rebuild target).",
                    state="not_rebuilding",
                )
            target = target_scheme or recorded
            if target != recorded:
                raise ChunkSchemeMismatchError(
                    f"finalize target {target!r} does not match the recorded "
                    f"rebuild target {recorded!r}; refusing to stamp a scheme the "
                    "rebuild did not build.",
                    expected=recorded,
                    found=target,
                    state="rebuild_target_mismatch",
                )
            if not _has_chunker_rows(conn):
                raise ChunkSchemeMismatchError(
                    f"refusing to finalize rebuild to {recorded!r}: no chunker rows "
                    "were repopulated, so the index is empty. Repopulate before "
                    "finalizing (or the old, coherent index is lost for nothing).",
                    expected=recorded,
                    state="rebuild_not_repopulated",
                )
            self._stamp_scheme(conn, target)
            self._delete_config(conn, CHUNK_SCHEME_REBUILD_KEY)

    def _delete_chunker_rows(self, conn: sqlite3.Connection, stale: Dict[str, Any]) -> None:
        conn.execute(
            "DELETE FROM code_chunks WHERE chunk_type != ? AND chunk_id NOT LIKE 'history:%'",
            (PRESERVED_CHUNK_TYPE,),
        )
        summary_hashes = stale.get("summary_hashes") or []
        for start in range(0, len(summary_hashes), 500):
            batch = summary_hashes[start : start + 500]
            placeholders = ",".join("?" * len(batch))
            conn.execute(
                f"DELETE FROM chunk_summaries WHERE chunk_hash IN ({placeholders})",
                batch,
            )
        points = stale.get("points") or []
        # Persist the stale remote-vector ids to the durable crash-ledger BEFORE
        # deleting their local mappings, in this same transaction: if the process
        # dies before the caller cleans up remote Qdrant vectors, the ledger still
        # names every orphan so recovery can finish the job (I4).
        self._record_pending_vector_deletions(conn, points)
        for start in range(0, len(points), 500):
            batch = points[start : start + 500]
            params: List[Any] = []
            for point in batch:
                params.extend([point["profile_id"], point["chunk_id"]])
            clause = " OR ".join(["(profile_id = ? AND chunk_id = ?)"] * len(batch))
            conn.execute(f"DELETE FROM semantic_points WHERE {clause}", params)

    def rebuild_chunk_scheme(
        self,
        target_scheme: Optional[str] = None,
        *,
        profile_id: Optional[str] = None,
        repopulate: Optional[Callable[[sqlite3.Connection], None]] = None,
    ) -> Dict[str, Any]:
        """Atomic chunk-scheme rebuild (public operator entry point).

        Invalidates every chunker-derived row across ``code_chunks``,
        ``chunk_summaries`` and ``semantic_points`` (PRESERVING synthetic
        history/document rows) and returns the stale remote-vector ids for Qdrant
        cleanup.

        Atomicity:

        - With ``repopulate`` supplied, invalidation, repopulation, and the scheme
          marker flip all run in ONE transaction.  If ``repopulate`` raises, the
          transaction rolls back and the old, coherent index survives untouched -
          never a half-populated "ready" DB.
        - Without ``repopulate``, this commits a blocked ``rebuilding`` state
          (readiness reports non-``ready``, reads/writes fail closed) whose scheme
          marker is only flipped by a later
          :meth:`finalize_chunk_scheme_rebuild` - a crash in between is resumable.
        """
        self._require_writable()
        target = target_scheme or self._current_scheme()
        if repopulate is None:
            return self.begin_chunk_scheme_rebuild(target, profile_id=profile_id)

        with self._get_connection() as conn:
            stale = self._collect_stale_chunk_artifacts(conn, profile_id)
            self._set_config(
                conn,
                CHUNK_SCHEME_REBUILD_KEY,
                target,
                "Chunk-scheme rebuild in progress (CHUNKERSAFE)",
            )
            self._delete_chunker_rows(conn, stale)
            # Repopulation runs inside the same transaction; a failure here rolls
            # the whole rebuild back to the prior coherent index.
            repopulate(conn)
            self._stamp_scheme(conn, target)
            self._delete_config(conn, CHUNK_SCHEME_REBUILD_KEY)
        return {"target_scheme": target, "profile_id": profile_id, **stale}

    def store_chunk(
        self,
        file_id: int,
        content: str,
        content_start: int,
        content_end: int,
        line_start: int,
        line_end: int,
        chunk_id: str,
        node_id: str,
        treesitter_file_id: str,
        symbol_id: Optional[int] = None,
        symbol_hash: Optional[str] = None,
        definition_id: Optional[str] = None,
        token_count: Optional[int] = None,
        token_model: Optional[str] = None,
        chunk_type: str = "code",
        language: Optional[str] = None,
        node_type: Optional[str] = None,
        parent_chunk_id: Optional[str] = None,
        depth: int = 0,
        chunk_index: int = 0,
        metadata: Optional[Dict] = None,
        scheme_target: Optional[str] = None,
    ) -> int:
        """Store a code chunk with stable IDs and token counting.

        ``scheme_target`` is the explicitly-scoped rebuild-writer escape hatch:
        during a ``rebuilding`` window the guard only admits writes whose target
        equals the persisted rebuild target, so a repopulation into a *different*
        target scheme (e.g. a v4 rebuild running under a v1 runtime) must pass
        ``scheme_target`` to identify itself as the rebuild writer.  Ordinary
        callers leave it ``None`` (defaults to the current runtime scheme).
        """
        serialized_metadata = _merge_chunk_source_metadata(metadata, content, line_start)
        with self._get_connection() as conn:
            self._assert_chunk_scheme_writable(conn, chunk_type=chunk_type, target=scheme_target)
            cursor = conn.execute(
                """INSERT INTO code_chunks
                   (file_id, symbol_id, content, content_start, content_end,
                    line_start, line_end, chunk_id, node_id, treesitter_file_id,
                    symbol_hash, definition_id, token_count, token_model,
                    chunk_type, language, node_type, parent_chunk_id, depth,
                    chunk_index, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(file_id, chunk_id) DO UPDATE SET
                   symbol_id=excluded.symbol_id,
                   content=excluded.content,
                   content_start=excluded.content_start,
                   content_end=excluded.content_end,
                   line_start=excluded.line_start,
                   line_end=excluded.line_end,
                   node_id=excluded.node_id,
                   treesitter_file_id=excluded.treesitter_file_id,
                   symbol_hash=excluded.symbol_hash,
                   definition_id=excluded.definition_id,
                   token_count=excluded.token_count,
                   token_model=excluded.token_model,
                   chunk_type=excluded.chunk_type,
                   language=excluded.language,
                   node_type=excluded.node_type,
                   parent_chunk_id=excluded.parent_chunk_id,
                   depth=excluded.depth,
                   chunk_index=excluded.chunk_index,
                   metadata=excluded.metadata,
                   updated_at=CURRENT_TIMESTAMP""",
                (
                    file_id,
                    symbol_id,
                    content,
                    content_start,
                    content_end,
                    line_start,
                    line_end,
                    chunk_id,
                    node_id,
                    treesitter_file_id,
                    symbol_hash,
                    definition_id,
                    token_count,
                    token_model or "cl100k_base" if token_count is not None else None,
                    chunk_type,
                    language,
                    node_type,
                    parent_chunk_id,
                    depth,
                    chunk_index,
                    json.dumps(serialized_metadata),
                ),
            )
            if cursor.lastrowid:
                return cursor.lastrowid
            else:
                # Get the id of the updated chunk
                cursor = conn.execute(
                    "SELECT id FROM code_chunks WHERE file_id = ? AND chunk_id = ?",
                    (file_id, chunk_id),
                )
                return cursor.fetchone()[0]

    def get_chunk_by_chunk_id(self, chunk_id: str, file_id: Optional[int] = None) -> Optional[Dict]:
        """Get chunk by chunk_id, optionally filtered by file_id."""
        with self._get_connection() as conn:
            assert_chunk_scheme_readable(conn)
            if file_id is not None:
                cursor = conn.execute(
                    "SELECT * FROM code_chunks WHERE chunk_id = ? AND file_id = ?",
                    (chunk_id, file_id),
                )
            else:
                cursor = conn.execute("SELECT * FROM code_chunks WHERE chunk_id = ?", (chunk_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_chunk_by_node_id(self, node_id: str, file_id: Optional[int] = None) -> Optional[Dict]:
        """Get chunk by node_id, optionally filtered by file_id."""
        with self._get_connection() as conn:
            assert_chunk_scheme_readable(conn)
            if file_id is not None:
                cursor = conn.execute(
                    "SELECT * FROM code_chunks WHERE node_id = ? AND file_id = ?",
                    (node_id, file_id),
                )
            else:
                cursor = conn.execute("SELECT * FROM code_chunks WHERE node_id = ?", (node_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_chunk_by_definition_id(self, definition_id: str) -> List[Dict]:
        """Get chunks by definition_id (may return multiple chunks)."""
        with self._get_connection() as conn:
            assert_chunk_scheme_readable(conn)
            cursor = conn.execute(
                "SELECT * FROM code_chunks WHERE definition_id = ?", (definition_id,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_chunks_for_file(self, file_id: int) -> List[Dict]:
        """Get all chunks for a file, ordered by chunk_index."""
        with self._get_connection() as conn:
            assert_chunk_scheme_readable(conn)
            cursor = conn.execute(
                """SELECT * FROM code_chunks
                   WHERE file_id = ?
                   ORDER BY chunk_index, line_start""",
                (file_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def update_chunk_token_count(
        self,
        chunk_id: str,
        token_count: int,
        token_model: str = "cl100k_base",
        *,
        file_id: Optional[int] = None,
    ) -> bool:
        """Update token count for a chunk.

        ``chunk_id`` is only unique per ``(file_id, chunk_id)``; pass ``file_id``
        to scope the update so a chunk id shared across files cannot collide.
        """
        with self._get_connection() as conn:
            # Guard the chunker-derived write path: refuse a token-count update
            # across a scheme boundary (cheap central check; also stamps a legacy
            # unmarked index on first touch).
            self._assert_chunk_scheme_writable(conn, chunk_type="code")
            if file_id is not None:
                cursor = conn.execute(
                    """UPDATE code_chunks
                       SET token_count = ?, token_model = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE chunk_id = ? AND file_id = ?""",
                    (token_count, token_model, chunk_id, file_id),
                )
            else:
                cursor = conn.execute(
                    """UPDATE code_chunks
                       SET token_count = ?, token_model = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE chunk_id = ?""",
                    (token_count, token_model, chunk_id),
                )
            return cursor.rowcount > 0

    def delete_chunks_for_file(
        self, file_id: int, *, preserve_imported: bool = False, preserve_summaries: bool = False
    ) -> int:
        """Invalidate owned chunks, persisting vector debt before removing mappings."""
        with self._get_connection() as conn:
            self._assert_chunk_scheme_deletable(conn)
            chunks = conn.execute(
                "SELECT chunk_id, chunk_type FROM code_chunks WHERE file_id=?", (file_id,)
            ).fetchall()
            retained = {
                row[0]
                for row in chunks
                if preserve_imported
                and (row[1] == PRESERVED_CHUNK_TYPE or row[0].startswith("history:"))
            }
            stale_ids = {row[0] for row in chunks if row[0] not in retained}
            summaries = conn.execute(
                "SELECT chunk_hash FROM chunk_summaries WHERE file_id=?", (file_id,)
            ).fetchall()
            stale_summaries = [
                row[0]
                for row in summaries
                if not any(row[0] == key or row[0].startswith(key + ":") for key in retained)
            ]
            stale_ids.update(stale_summaries)
            file = conn.execute("SELECT relative_path FROM files WHERE id=?", (file_id,)).fetchone()
            if file and file[0]:
                stale_ids.add(f"{file[0]}:file-summary")
            identifiers = sorted(stale_ids)
            points = {}
            for start in range(0, len(identifiers), 64):
                batch = identifiers[start : start + 64]
                clause = " OR ".join("chunk_id=? OR chunk_id LIKE ? ESCAPE '\\'" for _ in batch)
                parameters = [
                    value for key in batch for value in (key, _escape_like(key) + ":part:%")
                ]
                for point in conn.execute(
                    f"SELECT profile_id, chunk_id, point_id, collection FROM semantic_points WHERE {clause}",
                    parameters,
                ).fetchall():
                    points[(point["profile_id"], point["chunk_id"])] = dict(point)
            self._record_pending_vector_deletions(conn, list(points.values()))
            conn.executemany(
                "DELETE FROM semantic_points WHERE profile_id=? AND chunk_id=?", list(points)
            )
            if not preserve_summaries:
                conn.executemany(
                    "DELETE FROM chunk_summaries WHERE chunk_hash=?",
                    [(key,) for key in stale_summaries],
                )
            if preserve_imported:
                cursor = conn.execute(
                    "DELETE FROM code_chunks WHERE file_id=? AND chunk_type!=? "
                    "AND chunk_id NOT LIKE 'history:%'",
                    (file_id, PRESERVED_CHUNK_TYPE),
                )
            else:
                cursor = conn.execute("DELETE FROM code_chunks WHERE file_id=?", (file_id,))
            return cursor.rowcount

    def upsert_semantic_point(
        self,
        profile_id: str,
        chunk_id: str,
        point_id: int,
        collection: str,
    ) -> None:
        """Insert or update mapping from chunk id to semantic point id."""
        with self._get_connection() as conn:
            conn.execute(
                """INSERT INTO semantic_points (profile_id, chunk_id, point_id, collection)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(profile_id, chunk_id) DO UPDATE SET
                       point_id = excluded.point_id,
                       collection = excluded.collection,
                       updated_at = CURRENT_TIMESTAMP""",
                (profile_id, chunk_id, point_id, collection),
            )

    def get_semantic_point_ids(
        self, profile_id: str, chunk_ids: List[str]
    ) -> List[Union[int, str]]:
        """Resolve semantic point ids for a profile/chunk set."""
        if not chunk_ids:
            return []

        placeholders = ",".join(["?"] * len(chunk_ids))
        params: List[Union[str, int]] = [profile_id, *chunk_ids]

        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""SELECT point_id
                    FROM semantic_points
                    WHERE profile_id = ?
                      AND chunk_id IN ({placeholders})""",
                params,
            )
            return [row[0] for row in cursor.fetchall()]

    def delete_semantic_point_mappings(self, profile_id: str, chunk_ids: List[str]) -> int:
        """Delete semantic point mappings for the supplied chunks."""
        if not chunk_ids:
            return 0

        placeholders = ",".join(["?"] * len(chunk_ids))
        params: List[Union[str, int]] = [profile_id, *chunk_ids]

        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""DELETE FROM semantic_points
                    WHERE profile_id = ?
                      AND chunk_id IN ({placeholders})""",
                params,
            )
            return cursor.rowcount

    def delete_chunk_summaries(self, chunk_ids: List[str]) -> int:
        """Delete authoritative summary rows for the supplied chunk ids."""
        if not chunk_ids:
            return 0

        placeholders = ",".join(["?"] * len(chunk_ids))
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""DELETE FROM chunk_summaries
                    WHERE chunk_hash IN ({placeholders})""",
                list(chunk_ids),
            )
            return cursor.rowcount

    def store_chunks_batch(self, chunks: List[Dict[str, Any]]) -> int:
        """
        Store multiple chunks in a batch operation for efficiency.

        Args:
            chunks: List of chunk dictionaries with required fields

        Returns:
            Number of chunks stored
        """
        if not chunks:
            return 0

        with self._get_connection() as conn:
            # Central guard: any non-document (chunker-derived) row in the batch
            # must satisfy the scheme check before any write; stamps an empty index.
            if any(chunk.get("chunk_type", "code") != PRESERVED_CHUNK_TYPE for chunk in chunks):
                self._assert_chunk_scheme_writable(conn, chunk_type="code")
            count = 0
            for chunk in chunks:
                try:
                    serialized_metadata = _merge_chunk_source_metadata(
                        chunk.get("metadata"),
                        chunk["content"],
                        int(chunk["line_start"]),
                    )
                    conn.execute(
                        """INSERT INTO code_chunks
                           (file_id, symbol_id, content, content_start, content_end,
                            line_start, line_end, chunk_id, node_id, treesitter_file_id,
                            symbol_hash, definition_id, token_count, token_model,
                            chunk_type, language, node_type, parent_chunk_id, depth,
                            chunk_index, metadata)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(file_id, chunk_id) DO UPDATE SET
                           symbol_id=excluded.symbol_id,
                           content=excluded.content,
                           content_start=excluded.content_start,
                           content_end=excluded.content_end,
                           line_start=excluded.line_start,
                           line_end=excluded.line_end,
                           node_id=excluded.node_id,
                           treesitter_file_id=excluded.treesitter_file_id,
                           symbol_hash=excluded.symbol_hash,
                           definition_id=excluded.definition_id,
                           token_count=excluded.token_count,
                           token_model=excluded.token_model,
                           chunk_type=excluded.chunk_type,
                           language=excluded.language,
                           node_type=excluded.node_type,
                           parent_chunk_id=excluded.parent_chunk_id,
                           depth=excluded.depth,
                           chunk_index=excluded.chunk_index,
                           metadata=excluded.metadata,
                           updated_at=CURRENT_TIMESTAMP""",
                        (
                            chunk["file_id"],
                            chunk.get("symbol_id"),
                            chunk["content"],
                            chunk["content_start"],
                            chunk["content_end"],
                            chunk["line_start"],
                            chunk["line_end"],
                            chunk["chunk_id"],
                            chunk["node_id"],
                            chunk["treesitter_file_id"],
                            chunk.get("symbol_hash"),
                            chunk.get("definition_id"),
                            chunk.get("token_count"),
                            (
                                chunk.get("token_model") or "cl100k_base"
                                if chunk.get("token_count") is not None
                                else None
                            ),
                            chunk.get("chunk_type", "code"),
                            chunk.get("language"),
                            chunk.get("node_type"),
                            chunk.get("parent_chunk_id"),
                            chunk.get("depth", 0),
                            chunk.get("chunk_index", 0),
                            json.dumps(serialized_metadata),
                        ),
                    )
                    count += 1
                except Exception as e:
                    logger.warning(f"Failed to store chunk {chunk.get('chunk_id')}: {e}")
                    continue

            return count

    def search_chunks_by_source_metadata(
        self,
        *,
        query: Optional[str] = None,
        source_type: str = "friction",
        friction_categories: Optional[List[str]] = None,
        history_labels: Optional[List[str]] = None,
        history_repos: Optional[List[str]] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Filter source records, then apply literal-term FTS ranking before limiting."""
        if limit <= 0:
            return []
        with self._get_connection() as conn:
            assert_chunk_scheme_readable(conn)
            cursor = conn.execute(
                """SELECT c.content, c.line_start, c.line_end, c.metadata,
                          COALESCE(f.path, f.relative_path, CAST(c.file_id AS TEXT)) AS file_path
                   FROM code_chunks c
                   JOIN files f ON c.file_id = f.id
                   WHERE c.metadata LIKE '%"source_metadata"%'
                     AND COALESCE(f.is_deleted, 0) = 0
                   ORDER BY f.path, c.line_start
                   """,
            )
            rows = cursor.fetchall()

        results: List[Dict[str, Any]] = []
        for row in rows:
            metadata_raw = row["metadata"]
            if not metadata_raw:
                continue
            try:
                metadata = json.loads(metadata_raw)
            except (TypeError, json.JSONDecodeError):
                continue
            source_metadata = extract_matching_source_metadata(
                metadata,
                source_type=source_type,
                friction_categories=friction_categories,
                history_labels=history_labels,
                history_repos=history_repos,
            )
            if source_metadata is None:
                continue
            results.append(
                {
                    "file": row["file_path"],
                    "line": row["line_start"],
                    "line_end": row["line_end"],
                    "snippet": row["content"],
                    "source_metadata": source_metadata,
                }
            )
            if query is None and len(results) >= limit:
                break
        if query is None or not results:
            return results
        terms = re.findall(r"\w+", query.casefold())
        if not terms:
            return []
        # A private, ephemeral FTS table ranks exact chunks without modifying the
        # admitted index or matching text from another chunk in the same file.
        with closing(sqlite3.connect(":memory:")) as ranking:
            ranking.execute("CREATE VIRTUAL TABLE fts_code USING fts5(content, file_id UNINDEXED)")
            ranking.executemany(
                "INSERT INTO fts_code(rowid, content) VALUES (?, ?)",
                [(index, row["snippet"]) for index, row in enumerate(results)],
            )
            matches = ranking.execute(
                "SELECT rowid, bm25(fts_code) FROM fts_code WHERE fts_code MATCH ? "
                "ORDER BY bm25(fts_code), rowid LIMIT ?",
                (" AND ".join('"' + term + '"' for term in terms), limit),
            ).fetchall()
        return [{**results[index], "score": -rank} for index, rank in matches]

    def upsert_history_issue_documents(
        self,
        repository_id: int,
        records: List[HistoryIssueRecord],
    ) -> Dict[str, int]:
        """Persist metadata-only GitHub issue history documents for a repository."""
        self._require_writable()
        if not records:
            return {"stored": 0, "skipped": 0}

        grouped_records: Dict[str, List[HistoryIssueRecord]] = {}
        for record in records:
            repo_parts = record["repo"].split("/", 1)
            owner = repo_parts[0]
            repo_name = repo_parts[1] if len(repo_parts) == 2 else repo_parts[0]
            relative_path = f".mcp-index/history/{owner}/{repo_name}/issues/{record['number']}.md"
            grouped_records.setdefault(relative_path, []).append(record)

        stored = 0
        skipped = 0
        repository_root = self._repository_root_path(repository_id)
        for relative_path, file_records in grouped_records.items():
            absolute_path = str((repository_root / relative_path).resolve())
            file_id = self.store_file(
                repository_id,
                path=absolute_path,
                relative_path=relative_path,
                language="history",
                metadata={"source_type": HISTORY_SOURCE_TYPE},
            )
            for index, record in enumerate(
                sorted(file_records, key=lambda item: (item["type"], item["updated_at"]))
            ):
                chunk_id = f"history:{record['type']}"
                chunk_metadata = merge_source_metadata(
                    {
                        "history_dedupe_key": issue_history_dedupe_key(record),
                        "source_type": HISTORY_SOURCE_TYPE,
                    },
                    [record],
                )
                self.store_chunk(
                    file_id=file_id,
                    content=_history_issue_document_content(record),
                    content_start=0,
                    content_end=len(record["summary"]),
                    line_start=1,
                    line_end=max(1, len(record["learnings"]) + 10),
                    chunk_id=chunk_id,
                    node_id=chunk_id,
                    treesitter_file_id=relative_path,
                    chunk_type="document",
                    language="history",
                    chunk_index=index,
                    metadata=chunk_metadata,
                )
                stored += 1
            self._refresh_fts_code_for_file(file_id)
        return {"stored": stored, "skipped": skipped}

    def _repository_root_path(self, repository_id: int) -> Path:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT path FROM repositories WHERE id = ?", (repository_id,))
            row = cursor.fetchone()
        if row is None or not row[0]:
            return Path.cwd()
        return Path(str(row[0]))

    def _refresh_fts_code_for_file(self, file_id: int) -> None:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """SELECT content
                   FROM code_chunks
                   WHERE file_id = ?
                   ORDER BY line_start, chunk_index, id""",
                (file_id,),
            )
            content = "\n\n".join(
                row["content"]
                for row in cursor.fetchall()
                if isinstance(row["content"], str) and row["content"].strip()
            )
            conn.execute("DELETE FROM fts_code WHERE CAST(file_id AS TEXT) = ?", (str(file_id),))
            conn.execute(
                "INSERT INTO fts_code (content, file_id) VALUES (?, ?)", (content, file_id)
            )

    # Reference operations
    def store_reference(
        self,
        symbol_id: int,
        file_id: int,
        line_number: int,
        column_number: Optional[int] = None,
        reference_kind: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> int:
        """Store a symbol reference."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """INSERT INTO symbol_references 
                   (symbol_id, file_id, line_number, column_number, 
                    reference_kind, metadata)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    symbol_id,
                    file_id,
                    line_number,
                    column_number,
                    reference_kind,
                    json.dumps(metadata or {}),
                ),
            )
            return cursor.lastrowid

    def get_references(self, symbol_id: int) -> List[Dict]:
        """Get all references to a symbol."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """SELECT r.*, f.path as file_path
                   FROM symbol_references r
                   JOIN files f ON r.file_id = f.id
                   WHERE r.symbol_id = ?""",
                (symbol_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    # Search operations
    def search_symbols(
        self,
        query: Optional[str] = None,
        where_clause: Optional[str] = None,
        params: Optional[List[Any]] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Search for symbols with optional filtering.

        Args:
            query: Optional symbol name or pattern (uses LIKE)
            where_clause: Optional WHERE clause fragment (without the WHERE keyword)
            params: Parameters for the WHERE clause
            limit: Maximum number of results

        Returns:
            List of matching symbols with normalized fields
        """
        if not where_clause and query is not None:
            where_clause = "s.name LIKE ?"
            params = [f"%{query}%"]

        where_clause = where_clause or "1=1"
        parameters: List[Any] = list(params or [])

        sql = f"""
            SELECT
                s.name,
                s.kind AS type,
                f.language,
                COALESCE(f.relative_path, f.path) AS file_path,
                s.line_start AS line,
                s.line_end,
                s.column_start,
                s.column_end,
                s.signature,
                s.documentation
            FROM symbols s
            JOIN files f ON s.file_id = f.id
            WHERE {where_clause}
            ORDER BY s.name
            LIMIT ?
        """

        parameters.append(limit)

        with self._get_connection() as conn:
            cursor = conn.execute(sql, parameters)
            results = [dict(row) for row in cursor.fetchall()]

        for result in results:
            if "type" not in result and "kind" in result:
                result["type"] = result["kind"]
            if "file_path" not in result:
                result["file_path"] = result.get("path") or result.get("relative_path")
            if "line" not in result:
                result["line"] = result.get("line_start") or result.get("line_number")

        return results

    def search_symbols_fuzzy(self, query: str, limit: int = 20) -> List[Dict]:
        """
        Fuzzy search for symbols using trigrams.

        Args:
            query: Search query
            limit: Maximum number of results

        Returns:
            List of matching symbols with relevance scores
        """
        # Generate trigrams for the query
        query_trigrams = set()
        padded_query = f"  {query}  "
        for i in range(len(padded_query) - 2):
            trigram = padded_query[i : i + 3].lower()
            query_trigrams.add(trigram)

        if not query_trigrams:
            return []

        n_query = len(query_trigrams)
        placeholders = ",".join(["?"] * n_query)

        # CTE approach: avoid correlated subquery over 1.4M rows.
        # matched_syms: symbols that share at least one trigram with the query.
        # sym_totals:   total trigram count per matched symbol (uses idx_trigrams_symbol_id).
        # Jaccard = intersection / (|query| + |symbol| - intersection)
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""
                WITH matched_syms AS (
                    SELECT symbol_id, COUNT(DISTINCT trigram) AS intersection
                    FROM symbol_trigrams
                    WHERE trigram IN ({placeholders})
                    GROUP BY symbol_id
                ),
                sym_totals AS (
                    SELECT st.symbol_id, COUNT(*) AS total
                    FROM symbol_trigrams st
                    WHERE st.symbol_id IN (SELECT symbol_id FROM matched_syms)
                    GROUP BY st.symbol_id
                )
                SELECT s.*, f.path AS file_path,
                       m.intersection AS matches,
                       m.intersection * 1.0 / (? + t.total - m.intersection) AS score
                FROM matched_syms m
                JOIN sym_totals t ON t.symbol_id = m.symbol_id
                JOIN symbols s ON s.id = m.symbol_id
                JOIN files f ON f.id = s.file_id
                ORDER BY score DESC, s.name
                LIMIT ?
                """,
                list(query_trigrams) + [n_query, limit],
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_file_id_by_path(self, file_path: str) -> Optional[int]:
        """Return the integer file id for a given relative or absolute path, or None."""
        with self._get_connection() as conn:
            # Try relative_path first (most common), then path
            for col in ("relative_path", "path"):
                cursor = conn.execute(f"SELECT id FROM files WHERE {col} = ? LIMIT 1", (file_path,))
                row = cursor.fetchone()
                if row:
                    return row[0]
            # Try matching just the end of the stored path (handles abs vs rel mismatch)
            cursor = conn.execute(
                "SELECT id FROM files WHERE relative_path LIKE ? OR path LIKE ? LIMIT 1",
                (f"%{file_path}", f"%{file_path}"),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def search_files_fuzzy(self, query: str, limit: int = 20) -> List[Dict]:
        """
        Fuzzy search for file paths using trigrams.

        Computes Jaccard trigram similarity between the query and each
        relative_path in the files table, returning the top matches.

        Args:
            query: Search query (may be misspelled)
            limit: Maximum number of results

        Returns:
            List of dicts with file_path and score, sorted descending by score
        """
        # Generate trigrams for the query
        query_trigrams: set = set()
        padded_query = f"  {query.lower()}  "
        for i in range(len(padded_query) - 2):
            query_trigrams.add(padded_query[i : i + 3])

        if not query_trigrams:
            return []

        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT id, COALESCE(relative_path, path) as relative_path FROM files WHERE is_deleted = 0 OR is_deleted IS NULL"
            )
            rows = cursor.fetchall()

        results = []
        for row in rows:
            file_path = row[1] or ""
            if not file_path:
                continue
            padded_path = f"  {file_path.lower()}  "
            path_trigrams: set = set()
            for i in range(len(padded_path) - 2):
                path_trigrams.add(padded_path[i : i + 3])
            union_size = len(query_trigrams | path_trigrams)
            if union_size == 0:
                continue
            intersection = len(query_trigrams & path_trigrams)
            score = intersection / union_size
            if score > 0:
                results.append({"file_path": file_path, "score": score, "file_id": row[0]})

        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]

    def find_best_chunk_for_file(self, file_id: int, query_words: List[str]) -> Optional[Dict]:
        """
        Find the best-matching code chunk for a file given query words.

        Scores each chunk by how many query words appear in its content,
        returning the highest-scoring chunk as a dict with symbol, line_start,
        line_end, and node_type. Returns None if no chunks exist for the file.

        Args:
            file_id: The integer file id from the files table
            query_words: List of query words to match against chunk content

        Returns:
            Dict with symbol, line_start, line_end, node_type or None
        """
        with self._get_connection() as conn:
            assert_chunk_scheme_readable(conn)
            cursor = conn.execute(
                """
                SELECT cc.line_start, cc.line_end, cc.node_type, cc.content,
                       s.name as symbol_name
                FROM code_chunks cc
                LEFT JOIN symbols s ON cc.symbol_id = s.id
                WHERE cc.file_id = ?
                ORDER BY cc.line_start
                """,
                (file_id,),
            )
            chunks = cursor.fetchall()

        if not chunks:
            return None

        lower_words = [w.lower() for w in query_words if w]
        if not lower_words:
            # No query words: return the chunk with the most content
            best_chunk = max(chunks, key=lambda c: len(c[3] or ""))
        else:
            best_chunk = None
            best_score = -1
            for chunk in chunks:
                content_lower = (chunk[3] or "").lower()
                score = sum(1 for w in lower_words if w in content_lower)
                if score > best_score:
                    best_score = score
                    best_chunk = chunk
            if best_chunk is None:
                best_chunk = chunks[0]

        return {
            "symbol": best_chunk[4],
            "line_start": best_chunk[0],
            "line_end": best_chunk[1],
            "node_type": best_chunk[2],
            "content": best_chunk[3],
        }

    def search_symbols_fts(self, query: str, limit: int = 20) -> List[Dict]:
        """
        Full-text search for symbols.

        Args:
            query: Search query
            limit: Maximum number of results

        Returns:
            List of matching symbols
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """SELECT s.*, f.path as file_path
                   FROM symbols s
                   JOIN files f ON s.file_id = f.id
                   JOIN fts_symbols ON s.id = fts_symbols.rowid
                   WHERE fts_symbols MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def search_code_fts(self, query: str, limit: int = 20) -> List[Dict]:
        """
        Full-text search in code content.

        Args:
            query: Search query
            limit: Maximum number of results

        Returns:
            List of matching code snippets
        """
        fetch_limit = min(max(limit * 4, limit), 100)
        with self._get_connection() as conn:
            cursor = conn.execute(
                """SELECT
                       fts.*,
                       COALESCE(f.path, f.relative_path, CAST(fts.file_id AS TEXT)) as file_path
                   FROM fts_code fts
                   LEFT JOIN files f
                     ON fts.file_id = f.id
                     OR CAST(fts.file_id AS TEXT) = f.path
                     OR CAST(fts.file_id AS TEXT) = f.relative_path
                   WHERE fts_code MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, fetch_limit),
            )
            results = [dict(row) for row in cursor.fetchall()]

        for result in results:
            file_path = str(result.get("file_path", ""))
            base_rank = float(result.get("rank", 0.0) or 0.0)
            result["adjusted_rank"] = base_rank + _fts_rank_adjustment(query, file_path)

        results.sort(key=lambda row: float(row.get("adjusted_rank", row.get("rank", 0.0))))
        return results[:limit]

    # Index operations for fuzzy_indexer.py integration
    def persist_fuzzy_index(self, index_data: Dict[str, List[Tuple[str, Any]]]):
        """
        Persist fuzzy index data to database.

        Args:
            index_data: Dictionary mapping trigrams to list of (item, metadata) tuples
        """
        with self._get_connection() as conn:
            # Clear existing fuzzy index (for simplicity; in production, use incremental updates)
            conn.execute("DELETE FROM symbol_trigrams")

            # Store all symbols and their trigrams
            for trigram, items in index_data.items():
                for item, metadata in items:
                    # For now, assume item is a symbol name and metadata contains file info
                    # This is a simplified implementation
                    if isinstance(metadata, dict) and "file_id" in metadata:
                        conn.execute(
                            "INSERT INTO symbol_trigrams (symbol_id, trigram) VALUES (?, ?)",
                            (metadata.get("symbol_id", 0), trigram),
                        )

    def load_fuzzy_index(self) -> Dict[str, List[Tuple[str, Any]]]:
        """
        Load fuzzy index data from database.

        Returns:
            Dictionary mapping trigrams to list of (item, metadata) tuples
        """
        index_data = {}

        with self._get_connection() as conn:
            cursor = conn.execute("""SELECT st.trigram, s.name, s.id, s.file_id, f.path
                   FROM symbol_trigrams st
                   JOIN symbols s ON st.symbol_id = s.id
                   JOIN files f ON s.file_id = f.id""")

            for row in cursor:
                trigram = row["trigram"]
                if trigram not in index_data:
                    index_data[trigram] = []

                # Store symbol name with metadata
                metadata = {
                    "symbol_id": row["id"],
                    "file_id": row["file_id"],
                    "file_path": row["path"],
                }
                index_data[trigram].append((row["name"], metadata))

        return index_data

    # Utility methods
    def clear_cache(self):
        """Clear expired cache entries."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM query_cache WHERE expires_at < CURRENT_TIMESTAMP")

    def get_statistics(self) -> Dict[str, int]:
        """Get database statistics."""
        stats = {}
        with self._get_connection() as conn:
            tables = [
                "repositories",
                "files",
                "symbols",
                "symbol_references",
                "imports",
            ]
            for table in tables:
                cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
                stats[table] = cursor.fetchone()[0]
        return stats

    def health_check(self) -> Dict[str, Any]:
        """
        Perform health check on database.

        Returns:
            Dictionary containing:
            - status: "healthy" | "degraded" | "unhealthy"
            - tables: Dict[str, bool] - table existence check
            - fts5: bool - FTS5 availability
            - wal: bool - WAL mode status
            - version: int - schema version (0 if not available)
            - error: Optional[str] - error message if unhealthy
        """
        result = {
            "status": "healthy",
            "tables": {},
            "fts5": False,
            "wal": False,
            "version": 0,
            "journal_mode": None,
            "busy_timeout_ms": 0,
            "wal_checkpoint": {"status": "not_run"},
            "database_files": {},
            "error": None,
            "readonly": self._readonly,
            "storage_failure_family": None,
            "storage_failure_reason": None,
            "storage_failure_message": None,
        }
        if self._readonly_diagnostics is not None:
            result.update(self._readonly_diagnostics)

        try:
            with self._get_connection() as conn:
                # Check required tables
                required_tables = [
                    "schema_version",
                    "repositories",
                    "files",
                    "symbols",
                    "code_chunks",
                    "symbol_references",
                    "imports",
                    "fts_symbols",
                    "fts_code",
                    "symbol_trigrams",
                    "embeddings",
                    "query_cache",
                    "parse_cache",
                    "migrations",
                    "index_config",
                    "file_moves",
                    "semantic_points",
                ]

                for table in required_tables:
                    cursor = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                        (table,),
                    )
                    result["tables"][table] = cursor.fetchone() is not None

                # Check FTS5 support
                result["fts5"] = self._check_fts5_support(conn)

                # Check WAL mode
                cursor = conn.execute("PRAGMA journal_mode")
                journal_mode = cursor.fetchone()[0].upper()
                result["journal_mode"] = journal_mode
                result["wal"] = journal_mode == "WAL"

                cursor = conn.execute("PRAGMA busy_timeout")
                busy_timeout_row = cursor.fetchone()
                result["busy_timeout_ms"] = (
                    int(busy_timeout_row[0]) if busy_timeout_row and busy_timeout_row[0] else 0
                )

                try:
                    cursor = conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    checkpoint_row = cursor.fetchone()
                    if checkpoint_row is not None:
                        result["wal_checkpoint"] = {
                            "status": "ok",
                            "busy": int(checkpoint_row[0]),
                            "log_frames": int(checkpoint_row[1]),
                            "checkpointed_frames": int(checkpoint_row[2]),
                        }
                except sqlite3.OperationalError as exc:
                    result["wal_checkpoint"] = {
                        "status": "error",
                        "error": str(exc),
                    }

                # Get schema version
                if result["tables"].get("schema_version", False):
                    try:
                        cursor = conn.execute("SELECT MAX(version) FROM schema_version")
                        version_row = cursor.fetchone()
                        result["version"] = version_row[0] if version_row and version_row[0] else 0
                    except sqlite3.OperationalError:
                        result["version"] = 0

                db_path = Path(self.db_path)
                for label, candidate in (
                    ("main", db_path),
                    ("wal", db_path.with_name(f"{db_path.name}-wal")),
                    ("shm", db_path.with_name(f"{db_path.name}-shm")),
                ):
                    exists = candidate.exists()
                    result["database_files"][label] = {
                        "path": str(candidate),
                        "exists": exists,
                        "bytes": candidate.stat().st_size if exists else 0,
                    }

                # Determine health status
                missing_tables = [table for table, exists in result["tables"].items() if not exists]
                if missing_tables:
                    if len(missing_tables) > 5:
                        result["status"] = "unhealthy"
                        result["error"] = (
                            f"Critical tables missing: {', '.join(missing_tables[:5])} and {len(missing_tables) - 5} more"
                        )
                    else:
                        result["status"] = "degraded"
                        result["error"] = f"Some tables missing: {', '.join(missing_tables)}"
                elif not result["fts5"]:
                    result["status"] = "degraded"
                    result["error"] = "FTS5 support not available - search functionality limited"
                elif not result["wal"]:
                    result["status"] = "degraded"
                    result["error"] = "WAL mode not enabled - concurrency may be affected"
                elif result["wal_checkpoint"].get("status") == "error":
                    result["status"] = "degraded"
                    result["error"] = "WAL checkpoint diagnostics failed"

        except Exception as e:
            result["status"] = "unhealthy"
            result["error"] = f"Database health check failed: {str(e)}"
            logger.error(f"Health check error: {e}")

        return result

    # Enhanced FTS5 search methods for BM25

    def search_bm25(
        self,
        query: str,
        table: str = "fts_code",
        limit: int = 20,
        offset: int = 0,
        columns: Optional[List[str]] = None,
        file_pattern: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Perform BM25 search using FTS5.

        Args:
            query: FTS5 query string
            table: FTS table to search
            limit: Maximum results
            offset: Result offset for pagination
            columns: Specific columns to return

        Returns:
            List of search results with BM25 scores
        """
        with self._get_connection() as conn:
            # Check if this is a simple BM25 table (like bm25_content) or structured FTS
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            if not cursor.fetchone():
                # Table doesn't exist
                return []

            # Check table structure
            cursor = conn.execute(f"PRAGMA table_info({table})")
            table_columns = {row[1] for row in cursor.fetchall()}

            if table == "fts_code" and "file_id" in table_columns:
                # Support both integer file_id references and legacy path-style file_id values.
                cursor = conn.execute(
                    """
                    SELECT
                        fts.content,
                        fts.file_id,
                        COALESCE(f.path, f.relative_path, CAST(fts.file_id AS TEXT)) as filepath,
                        f.relative_path,
                        bm25(fts_code) as score,
                        snippet(fts_code, 0, '<mark>', '</mark>', '...', 32) as snippet,
                        f.last_modified
                    FROM fts_code fts
                    LEFT JOIN files f ON fts.file_id = f.id
                        OR CAST(fts.file_id AS TEXT) = f.path
                        OR CAST(fts.file_id AS TEXT) = f.relative_path
                    WHERE fts_code MATCH ?
                        AND (? IS NULL OR COALESCE(f.path, CAST(fts.file_id AS TEXT)) GLOB ?
                             OR f.relative_path GLOB ?)
                    ORDER BY bm25(fts_code)
                    LIMIT ? OFFSET ?
                    """,
                    (query, file_pattern, file_pattern, file_pattern, limit, offset),
                )
            elif table == "bm25_content" and "filepath" in table_columns:
                # Legacy BM25 schema with direct filepath column
                if not columns:
                    columns = ["*", f"bm25({table}) as score"]
                else:
                    columns = columns + [f"bm25({table}) as score"]

                columns_str = ", ".join(columns)
                cursor = conn.execute(
                    f"""
                    SELECT {columns_str}
                    FROM {table}
                    WHERE {table} MATCH ?
                    ORDER BY bm25({table})
                    LIMIT ? OFFSET ?
                    """,
                    (query, limit, offset),
                )
            else:
                # Generic fallback
                if not columns:
                    columns = ["*", f"bm25({table}) as score"]
                else:
                    columns = columns + [f"bm25({table}) as score"]

                columns_str = ", ".join(columns)
                cursor = conn.execute(
                    f"""
                    SELECT {columns_str}
                    FROM {table}
                    WHERE {table} MATCH ?
                    ORDER BY bm25({table})
                    LIMIT ? OFFSET ?
                    """,
                    (query, limit, offset),
                )

            results = []
            for row in cursor:
                results.append(dict(row))

            return results

    def search_bm25_with_snippets(
        self,
        query: str,
        table: str = "fts_code",
        content_column: int = 0,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Search with BM25 and return snippets.

        Args:
            query: FTS5 query string
            table: FTS table to search
            content_column: Column index for snippet extraction
            limit: Maximum results

        Returns:
            List of results with snippets
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT 
                    *,
                    snippet({table}, {content_column}, '<mark>', '</mark>', '...', 32) as snippet,
                    bm25({table}) as score
                FROM {table}
                WHERE {table} MATCH ?
                ORDER BY bm25({table})
                LIMIT ?
            """,
                (query, limit),
            )

            results = []
            for row in cursor:
                results.append(dict(row))

            return results

    def search_bm25_with_highlight(
        self,
        query: str,
        table: str = "fts_code",
        highlight_column: int = 0,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Search with BM25 and return highlighted content.

        Args:
            query: FTS5 query string
            table: FTS table to search
            highlight_column: Column index for highlighting
            limit: Maximum results

        Returns:
            List of results with highlighted content
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT 
                    *,
                    highlight({table}, {highlight_column}, '<b>', '</b>') as highlighted,
                    bm25({table}) as score
                FROM {table}
                WHERE {table} MATCH ?
                ORDER BY bm25({table})
                LIMIT ?
            """,
                (query, limit),
            )

            results = []
            for row in cursor:
                results.append(dict(row))

        return results

    def rebuild_fts_code(self) -> int:
        """Rebuild the fts_code table from current file records."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM fts_code")
            cursor = conn.execute(
                "SELECT id, path, relative_path FROM files WHERE is_deleted = FALSE"
            )
            rows = cursor.fetchall()
            inserted = 0

            for row in rows:
                file_id = row[0]
                if conn.execute(
                    "SELECT 1 FROM code_chunks WHERE file_id=? "
                    "AND (chunk_type=? OR chunk_id LIKE 'history:%') LIMIT 1",
                    (file_id, PRESERVED_CHUNK_TYPE),
                ).fetchone():
                    self._refresh_fts_code_for_file(file_id)
                    inserted += 1
                    continue
                path_value = row[1] or row[2]
                if not path_value:
                    continue

                file_path = Path(path_value)
                if self.path_resolver.source_root is not None:
                    relative = row[2] or self.path_resolver.normalize_path(file_path)
                    file_path = (self.path_resolver.source_root / relative).resolve()
                    if not file_path.is_relative_to(self.path_resolver.source_root):
                        raise ValueError("FTS input is outside the committed snapshot")
                if not file_path.exists() or file_path.name in _LEXICAL_EXCLUDED_FILENAMES:
                    continue

                try:
                    content = file_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    try:
                        content = file_path.read_text(encoding="latin-1")
                    except Exception:
                        continue
                except Exception:
                    continue

                conn.execute(
                    "INSERT INTO fts_code (content, file_id) VALUES (?, ?)",
                    (content, file_id),
                )
                inserted += 1

            return inserted

    def get_bm25_term_statistics(self, term: str, table: str = "fts_code") -> Dict[str, Any]:
        """
        Get term statistics for BM25 tuning.

        Args:
            term: Term to analyze
            table: FTS table to search

        Returns:
            Dictionary with term statistics
        """
        with self._get_connection() as conn:
            # Get document frequency
            cursor = conn.execute(
                f"""
                SELECT COUNT(*) FROM {table}
                WHERE {table} MATCH ?
            """,
                (term,),
            )
            doc_freq = cursor.fetchone()[0]

            # Get total documents
            cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
            total_docs = cursor.fetchone()[0]

            # Get average document length (approximation)
            cursor = conn.execute(f"""
                SELECT AVG(LENGTH(content)) FROM {table}
            """)
            avg_doc_length = cursor.fetchone()[0] or 0

            return {
                "term": term,
                "document_frequency": doc_freq,
                "total_documents": total_docs,
                "idf": self._calculate_idf(doc_freq, total_docs),
                "average_document_length": avg_doc_length,
            }

    def _calculate_idf(self, doc_freq: int, total_docs: int) -> float:
        """Calculate Inverse Document Frequency."""
        import math

        if doc_freq == 0:
            return 0
        return math.log((total_docs - doc_freq + 0.5) / (doc_freq + 0.5))

    def optimize_fts_tables(self):
        """Optimize all FTS5 tables for better performance."""
        with self._get_connection() as conn:
            # Get all FTS5 tables
            cursor = conn.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND sql LIKE '%USING fts5%'
            """)

            fts_tables = [row[0] for row in cursor]

            for table in fts_tables:
                try:
                    conn.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")
                    logger.info(f"Optimized FTS5 table: {table}")
                except Exception as e:
                    logger.warning(f"Could not optimize {table}: {e}")

    def rebuild_fts_tables(self):
        """Rebuild all FTS5 tables."""
        with self._get_connection() as conn:
            # Get all FTS5 tables
            cursor = conn.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND sql LIKE '%USING fts5%'
            """)

            fts_tables = [row[0] for row in cursor]

            for table in fts_tables:
                try:
                    conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
                    logger.info(f"Rebuilt FTS5 table: {table}")
                except Exception as e:
                    logger.warning(f"Could not rebuild {table}: {e}")

    # New file operation methods for path management
    def get_file_by_content_hash(self, content_hash: str, repository_id: int) -> Optional[Dict]:
        """Get file by content hash."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """SELECT * FROM files 
                   WHERE content_hash = ? AND repository_id = ? AND is_deleted = FALSE
                   ORDER BY indexed_at DESC LIMIT 1""",
                (content_hash, repository_id),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def mark_file_deleted(self, relative_path: str, repository_id: int):
        """Mark a file as deleted (soft delete)."""
        with self._get_connection() as conn:
            conn.execute(
                """UPDATE files 
                   SET is_deleted = TRUE, deleted_at = CURRENT_TIMESTAMP
                   WHERE relative_path = ? AND repository_id = ?""",
                (relative_path, repository_id),
            )
            logger.info(f"Marked file as deleted: {relative_path}")

    def remove_file(self, relative_path: str, repository_id: int) -> bool:
        """Remove file and all associated data (hard delete)."""
        with self._get_connection() as conn:
            # Get file ID first
            cursor = conn.execute(
                "SELECT id FROM files WHERE relative_path = ? AND repository_id = ?",
                (relative_path, repository_id),
            )
            row = cursor.fetchone()
            if not row:
                logger.warning(f"File not found for removal: {relative_path}")
                return False

            file_id = row[0]

            # Look up absolute path before deletion — fts_code may store path strings
            # rather than integer file IDs depending on how the plugin indexed the file.
            path_row = conn.execute("SELECT path FROM files WHERE id = ?", (file_id,)).fetchone()
            absolute_path = path_row[0] if path_row else None

            # Delete all associated data (cascade should handle most)
            self.delete_chunks_for_file(file_id)
            conn.execute(
                "DELETE FROM symbol_references WHERE file_id=? OR symbol_id IN "
                "(SELECT id FROM symbols WHERE file_id=?)",
                (file_id, file_id),
            )
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in ("bm25_content", "bm25_documents", "bm25_index_status"):
                if table in tables:
                    conn.execute(f"DELETE FROM {table} WHERE file_id=?", (file_id,))
            if "bm25_symbols" in tables:
                conn.execute(
                    "DELETE FROM bm25_symbols WHERE symbol_id IN (SELECT id FROM symbols WHERE file_id=?)",
                    (file_id,),
                )
            conn.execute("DELETE FROM imports WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM embeddings WHERE file_id = ?", (file_id,))
            # fts_code.file_id may be stored as an integer, an absolute path, or a
            # relative path depending on the indexing plugin — delete all three forms.
            conn.execute(
                "DELETE FROM fts_code WHERE CAST(file_id AS TEXT) = ? OR file_id = ? OR file_id = ?",
                (str(file_id), absolute_path or relative_path, relative_path),
            )
            conn.execute(
                "DELETE FROM symbol_trigrams WHERE symbol_id IN "
                "(SELECT id FROM symbols WHERE file_id = ?)",
                (file_id,),
            )
            conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

            logger.info(f"Removed file and all associated data: {relative_path}")
            return True

    def move_file(
        self, old_path: str, new_path: str, repository_id: int, content_hash: str
    ) -> bool:
        """Record a file move operation."""
        with self._get_connection() as conn:
            # Update the file path
            cursor = conn.execute(
                """UPDATE files 
                   SET relative_path = ?, path = ?
                   WHERE relative_path = ? AND repository_id = ?""",
                (new_path, new_path, old_path, repository_id),
            )
            if cursor.rowcount <= 0:
                logger.warning("File not found for move: %s", old_path)
                return False

            # Record the move
            conn.execute(
                """INSERT INTO file_moves 
                   (repository_id, old_relative_path, new_relative_path, content_hash, move_type)
                   VALUES (?, ?, ?, ?, ?)""",
                (repository_id, old_path, new_path, content_hash, "rename"),
            )

            logger.info(f"Recorded file move: {old_path} -> {new_path}")
            return True

    def cleanup_deleted_files(self, days_old: int = 30):
        """Clean up files marked as deleted for more than specified days."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """SELECT COUNT(*) FROM files 
                   WHERE is_deleted = TRUE 
                   AND deleted_at < datetime('now', '-' || ? || ' days')""",
                (days_old,),
            )
            count = cursor.fetchone()[0]

            if count > 0:
                # Get file IDs for cleanup
                cursor = conn.execute(
                    """SELECT id, relative_path FROM files 
                       WHERE is_deleted = TRUE 
                       AND deleted_at < datetime('now', '-' || ? || ' days')""",
                    (days_old,),
                )

                for row in cursor:
                    file_id, path = row
                    # Use remove_file for thorough cleanup
                    self.remove_file(path, repository_id=0)

                logger.info(f"Cleaned up {count} deleted files older than {days_old} days")

    def store_chunk_summary(
        self,
        chunk_hash: str,
        file_id: int,
        chunk_start: int,
        chunk_end: int,
        summary_text: str,
        llm_model: str,
        symbol: Optional[str] = None,
        is_authoritative: bool = False,
        provider_name: Optional[str] = None,
        profile_id: Optional[str] = None,
        prompt_fingerprint: Optional[str] = None,
        audit_metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Store or update a semantic chunk summary."""
        with self._get_connection() as conn:
            # Check if authoritative summary exists
            cursor = conn.execute(
                "SELECT is_authoritative FROM chunk_summaries WHERE chunk_hash = ?",
                (chunk_hash,),
            )
            row = cursor.fetchone()
            if row and row[0] and not is_authoritative:
                return False  # Cannot overwrite authoritative summary with non-authoritative

            conn.execute(
                """INSERT INTO chunk_summaries 
                   (chunk_hash, file_id, chunk_start, chunk_end, symbol, summary_text,
                    is_authoritative, llm_model, provider_name, profile_id,
                    prompt_fingerprint, audit_metadata, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(chunk_hash) DO UPDATE SET
                   summary_text=excluded.summary_text,
                   is_authoritative=excluded.is_authoritative,
                   llm_model=excluded.llm_model,
                   provider_name=excluded.provider_name,
                   profile_id=excluded.profile_id,
                   prompt_fingerprint=excluded.prompt_fingerprint,
                   audit_metadata=excluded.audit_metadata,
                   updated_at=CURRENT_TIMESTAMP
                """,
                (
                    chunk_hash,
                    file_id,
                    chunk_start,
                    chunk_end,
                    symbol,
                    summary_text,
                    is_authoritative,
                    llm_model,
                    provider_name,
                    profile_id,
                    prompt_fingerprint,
                    json.dumps(audit_metadata) if audit_metadata is not None else None,
                ),
            )
            return True

    def get_chunk_summary(self, chunk_hash: str) -> Optional[Dict[str, Any]]:
        """Get a specific chunk summary."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """SELECT chunk_hash, file_id, chunk_start, chunk_end, symbol, 
                          summary_text, is_authoritative, llm_model, provider_name,
                          profile_id, prompt_fingerprint, audit_metadata,
                          created_at, updated_at
                   FROM chunk_summaries WHERE chunk_hash = ?""",
                (chunk_hash,),
            )
            row = cursor.fetchone()
            if row:
                audit_metadata = None
                if row[11]:
                    try:
                        audit_metadata = json.loads(row[11])
                    except json.JSONDecodeError:
                        audit_metadata = None
                return {
                    "chunk_hash": row[0],
                    "file_id": row[1],
                    "chunk_start": row[2],
                    "chunk_end": row[3],
                    "symbol": row[4],
                    "summary_text": row[5],
                    "is_authoritative": bool(row[6]),
                    "llm_model": row[7],
                    "provider_name": row[8],
                    "profile_id": row[9],
                    "prompt_fingerprint": row[10],
                    "audit_metadata": audit_metadata,
                    "created_at": row[12],
                    "updated_at": row[13],
                }
            return None

    def plan_semantic_invalidation(
        self,
        relative_path: str,
        repository_id: int,
        *,
        profile_id: Optional[str] = None,
        new_relative_path: Optional[str] = None,
        preserve_matching_summaries: bool = False,
        expected_summary_profile_id: Optional[str] = None,
        expected_prompt_fingerprint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Plan summary/vector invalidation for a file mutation without mutating state."""
        result: Dict[str, Any] = {
            "relative_path": relative_path,
            "new_relative_path": new_relative_path,
            "file_id": None,
            "source_chunk_ids": [],
            "vector_chunk_ids": [],
            "summary_chunk_ids_to_delete": [],
            "summary_chunk_ids_preserved": [],
            "file_summary_chunk_id": f"{relative_path}:file-summary",
            "new_file_summary_chunk_id": (
                f"{new_relative_path}:file-summary" if new_relative_path else None
            ),
            "requires_file_summary_regeneration": bool(
                new_relative_path and new_relative_path != relative_path
            ),
            "requires_summary_regeneration": False,
        }

        file_row = self.get_file_by_path(relative_path, repository_id)
        if not file_row:
            return result

        file_id = int(file_row["id"])
        result["file_id"] = file_id

        chunks = self.get_chunks_for_file(file_id)
        source_chunk_ids = [str(chunk["chunk_id"]) for chunk in chunks]
        result["source_chunk_ids"] = source_chunk_ids

        with self._get_connection() as conn:
            summary_rows = conn.execute(
                """SELECT chunk_hash, is_authoritative, profile_id, prompt_fingerprint
                   FROM chunk_summaries
                   WHERE file_id = ?""",
                (file_id,),
            ).fetchall()

            vector_chunk_ids: List[str] = list(source_chunk_ids)
            if profile_id:
                if source_chunk_ids:
                    like_clauses = " OR ".join(
                        ["chunk_id = ? OR chunk_id LIKE ? ESCAPE '\\'"] * len(source_chunk_ids)
                    )
                    params: List[Any] = [profile_id]
                    for chunk_id in source_chunk_ids:
                        # Escape LIKE metacharacters: a collision-free (v4) chunk_id
                        # may legitimately contain %/_ which would otherwise make the
                        # ':part:%' sub-chunk match hit unrelated rows (I2).
                        params.extend([chunk_id, f"{_escape_like(chunk_id)}:part:%"])
                    params.append(result["file_summary_chunk_id"])
                    vector_rows = conn.execute(
                        f"""SELECT chunk_id
                            FROM semantic_points
                            WHERE profile_id = ?
                              AND (({like_clauses}) OR chunk_id = ?)""",
                        params,
                    ).fetchall()
                else:
                    vector_rows = conn.execute(
                        """SELECT chunk_id
                           FROM semantic_points
                           WHERE profile_id = ?
                             AND chunk_id = ?""",
                        (profile_id, result["file_summary_chunk_id"]),
                    ).fetchall()
                vector_chunk_ids.extend(str(row[0]) for row in vector_rows)

        preserved: List[str] = []
        deleted: List[str] = []
        for row in summary_rows:
            chunk_hash = str(row[0])
            is_authoritative = bool(row[1])
            row_profile_id = row[2]
            row_prompt_fingerprint = row[3]
            matches_profile = (
                expected_summary_profile_id is None or row_profile_id == expected_summary_profile_id
            )
            matches_prompt = (
                expected_prompt_fingerprint is None
                or row_prompt_fingerprint == expected_prompt_fingerprint
            )
            can_preserve = (
                preserve_matching_summaries
                and is_authoritative
                and matches_profile
                and matches_prompt
            )
            if can_preserve:
                preserved.append(chunk_hash)
            else:
                deleted.append(chunk_hash)

        dedup_vector_chunk_ids = list(dict.fromkeys(vector_chunk_ids))
        result["vector_chunk_ids"] = dedup_vector_chunk_ids
        result["summary_chunk_ids_to_delete"] = deleted
        result["summary_chunk_ids_preserved"] = preserved
        result["requires_summary_regeneration"] = bool(
            deleted or result["requires_file_summary_regeneration"]
        )
        return result

    def get_missing_summaries(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Find chunks that don't have summaries yet."""
        with self._get_connection() as conn:
            assert_chunk_scheme_readable(conn)
            cursor = conn.execute(
                """SELECT c.chunk_id, c.file_id, c.content_start, c.content_end, c.line_start, c.line_end, c.content, s.name as symbol
                   FROM code_chunks c
                   LEFT JOIN symbols s ON c.symbol_id = s.id
                   LEFT JOIN chunk_summaries cs ON c.chunk_id = cs.chunk_hash
                   WHERE cs.chunk_hash IS NULL
                   LIMIT ?""",
                (limit,),
            )
            return [
                {
                    "chunk_id": row[0],
                    "file_id": row[1],
                    "content_start": row[2],
                    "content_end": row[3],
                    "line_start": row[4],
                    "line_end": row[5],
                    "content": row[6],
                    "symbol": row[7],
                }
                for row in cursor.fetchall()
            ]

    def get_semantic_readiness_evidence(
        self,
        profile_id: str,
        *,
        collection: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Summarize durable semantic evidence without mutating the store."""
        with self._get_connection() as conn:
            total_chunks = int(conn.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0])
            summary_count = int(
                conn.execute("SELECT COUNT(DISTINCT chunk_hash) FROM chunk_summaries").fetchone()[0]
            )
            missing_summaries = int(conn.execute("""SELECT COUNT(*)
                       FROM code_chunks c
                       LEFT JOIN chunk_summaries cs ON c.chunk_id = cs.chunk_hash
                       WHERE cs.chunk_hash IS NULL""").fetchone()[0])

            vector_link_count = int(
                conn.execute(
                    """SELECT COUNT(DISTINCT chunk_id)
                       FROM semantic_points
                       WHERE profile_id = ?""",
                    (profile_id,),
                ).fetchone()[0]
            )
            missing_vectors = int(
                conn.execute(
                    """SELECT COUNT(*)
                       FROM code_chunks c
                       LEFT JOIN semantic_points sp
                         ON c.chunk_id = sp.chunk_id
                        AND sp.profile_id = ?
                       WHERE sp.chunk_id IS NULL""",
                    (profile_id,),
                ).fetchone()[0]
            )

            matching_collection_links = vector_link_count
            collection_mismatches = 0
            if collection is not None:
                matching_collection_links = int(
                    conn.execute(
                        """SELECT COUNT(DISTINCT chunk_id)
                           FROM semantic_points
                           WHERE profile_id = ?
                             AND collection = ?""",
                        (profile_id, collection),
                    ).fetchone()[0]
                )
                collection_mismatches = int(
                    conn.execute(
                        """SELECT COUNT(*)
                           FROM semantic_points
                           WHERE profile_id = ?
                             AND collection != ?""",
                        (profile_id, collection),
                    ).fetchone()[0]
                )

            return {
                "profile_id": profile_id,
                "collection": collection,
                "total_chunks": total_chunks,
                "summary_count": summary_count,
                "missing_summaries": missing_summaries,
                "vector_link_count": vector_link_count,
                "missing_vectors": missing_vectors,
                "matching_collection_links": matching_collection_links,
                "collection_mismatches": collection_mismatches,
            }

    def find_chunk_at_line(self, file_path: str, line: int) -> Optional[Dict[str, Any]]:
        """Return the tightest code chunk that covers *line* in *file_path*.

        Returns ``None`` when no matching chunk is found.  The dict keys
        match the kwargs expected by ``ChunkWriter.summarize_chunk``:
        ``chunk_hash``, ``file_id``, ``chunk_start``, ``chunk_end``,
        ``symbol``, ``content``.
        """
        with self._get_connection() as conn:
            assert_chunk_scheme_readable(conn)
            cursor = conn.execute(
                """SELECT c.chunk_id, c.file_id, c.line_start, c.line_end,
                          c.content, s.name AS symbol, c.metadata
                   FROM code_chunks c
                   JOIN files f ON c.file_id = f.id
                   LEFT JOIN symbols s ON c.symbol_id = s.id
                   WHERE (f.path = ? OR f.path LIKE ?)
                     AND c.line_start <= ?
                     AND c.line_end   >= ?
                   ORDER BY (c.line_end - c.line_start) ASC
                   LIMIT 1""",
                (file_path, f"%{file_path}", line, line),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            metadata: Dict[str, Any] = {}
            if row[6]:
                try:
                    metadata = json.loads(row[6])
                except (TypeError, json.JSONDecodeError):
                    metadata = {}
            return {
                "chunk_hash": row[0],
                "file_id": row[1],
                "chunk_start": row[2] or line,
                "chunk_end": row[3] or line,
                "symbol": row[5] or "unknown",
                "content": row[4] or "",
                "metadata": metadata,
            }
