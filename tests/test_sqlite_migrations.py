"""Installed-resource and transactional SQLite upgrade regression tests."""

import sqlite3
from importlib.resources import files

import pytest

from mcp_server.storage import sqlite_store
from mcp_server.storage.connection_pool import ConnectionPool
from mcp_server.storage.sqlite_store import SQLiteStore


def schema(path):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()


def test_fresh_schema_has_all_migration_versions_and_triggers(tmp_path):
    path = tmp_path / "fresh.db"
    SQLiteStore(str(path)).close()
    with sqlite3.connect(path) as conn:
        versions = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
        assert set(range(2, 8)) <= versions
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='update_chunk_timestamp'"
        ).fetchone()
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='soft_delete_symbols'"
        ).fetchone()
        assert {"tracked_branch", "git_common_dir"} <= {
            r[1] for r in conn.execute("PRAGMA table_info(repositories)")
        }


def test_in_memory_migrations_and_rows_survive_connection_boundaries():
    store = SQLiteStore(":memory:")
    try:
        with store._get_connection() as conn:
            assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 7
            conn.execute("INSERT INTO repositories(path,name) VALUES('/memory','memory')")
        with store._get_connection() as conn:
            assert conn.execute("SELECT name FROM repositories").fetchone()[0] == "memory"
    finally:
        store.close()
    with pytest.raises(RuntimeError, match="closed"):
        with store._get_connection():
            pytest.fail("Closed memory store was reopened")


def test_preopened_pool_connections_observe_migrated_schema(tmp_path):
    path = tmp_path / "pooled.db"
    pool = ConnectionPool(lambda: sqlite3.connect(path, check_same_thread=False), size=4)
    store = SQLiteStore(str(path), pool=pool)
    try:
        repository_id = store.create_repository(str(tmp_path), "pooled")
        for i in range(8):
            store.store_file(
                repository_id=repository_id,
                relative_path=f"module_{i}.py",
                content_hash=f"content-{i}",
            )
    finally:
        store.close()


def test_partial_upgrade_matches_fresh_and_preserves_rows(tmp_path):
    fresh, upgrade = tmp_path / "fresh.db", tmp_path / "upgrade.db"
    SQLiteStore(str(fresh)).close()
    # The actual v1 SQL payload, with one previously applied ALTER from v3.
    with sqlite3.connect(upgrade) as conn:
        conn.executescript(
            files("mcp_server.storage")
            .joinpath("migrations/001_initial_schema.sql")
            .read_text(encoding="utf-8")
        )
        conn.execute("ALTER TABLE symbols ADD COLUMN token_count INTEGER")
        conn.execute("INSERT INTO repositories(path,name) VALUES('/fixture','fixture')")
    SQLiteStore(str(upgrade)).close()
    with sqlite3.connect(upgrade) as conn:
        assert conn.execute("SELECT name FROM repositories").fetchall() == [("fixture",)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    # Compare structural columns, indexes and triggers, not equivalent DDL whitespace.
    with sqlite3.connect(fresh) as a, sqlite3.connect(upgrade) as b:
        tables = [r[0] for r in a.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            assert sorted(r[1:] for r in a.execute(f'PRAGMA table_info("{table}")')) == sorted(
                r[1:] for r in b.execute(f'PRAGMA table_info("{table}")')
            ), table
        for kind in ("index", "trigger"):
            query = "SELECT name FROM sqlite_master WHERE type=? AND sql IS NOT NULL ORDER BY name"
            assert a.execute(query, (kind,)).fetchall() == b.execute(query, (kind,)).fetchall()


def test_reopen_is_idempotent_and_repairs_legacy_version_gaps(tmp_path):
    path = tmp_path / "legacy.db"
    SQLiteStore(str(path)).close()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM schema_version WHERE version IN (3,5,7)")
        conn.execute("DROP TRIGGER IF EXISTS update_chunk_timestamp")
    SQLiteStore(str(path)).close()
    before = schema(path)
    with sqlite3.connect(path) as conn:
        logs = conn.execute("SELECT * FROM migrations").fetchall()
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (7,)
    SQLiteStore(str(path)).close()
    assert schema(path) == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM migrations").fetchall() == logs


def test_failed_migration_rolls_back_ddl_rows_and_version(tmp_path, monkeypatch):
    path = tmp_path / "rollback.db"
    store = SQLiteStore(str(path))
    resources = tmp_path / "resources"
    migrations = resources / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "008_failure.sql").write_text(
        "CREATE TABLE rollback_probe(id INTEGER);\n"
        "INSERT INTO repositories(path,name) VALUES('/rollback','rollback');\n"
        "INSERT INTO schema_version(version) VALUES(8);\n"
        "INSERT INTO no_such_table VALUES(1);\n"
    )
    monkeypatch.setattr(sqlite_store, "files", lambda package: resources)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        store._run_migrations()
    with sqlite3.connect(path) as conn:
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='rollback_probe'"
        ).fetchone()
        assert not conn.execute("SELECT 1 FROM repositories WHERE name='rollback'").fetchone()
        assert not conn.execute("SELECT 1 FROM schema_version WHERE version=8").fetchone()


def test_missing_migration_payload_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_store, "files", lambda package: tmp_path)
    with pytest.raises(RuntimeError, match="migration resources"):
        SQLiteStore(str(tmp_path / "missing.db"))
