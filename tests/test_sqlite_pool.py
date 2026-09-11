"""
Tests for ConnectionPool and its integration with SQLiteStore.

Covers:
- 16 concurrent readers against a pool-backed SQLiteStore (pool size 4)
- close_all() is idempotent
- close_all() closes every connection; post-call acquire raises RuntimeError
- Write path round trip works with a pool-backed store
"""

import concurrent.futures
import multiprocessing
import queue
import sqlite3
import threading

import pytest

from mcp_server.storage.connection_pool import ConnectionPool
from mcp_server.storage.sqlite_store import SQLiteStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(db_path: str, size: int = 4) -> ConnectionPool:
    return ConnectionPool(
        factory=lambda: sqlite3.connect(db_path, check_same_thread=False),
        size=size,
    )


def _make_pool_store(tmp_path, size: int = 4):
    """Return (store, pool) backed by a temp DB with a pool attached."""
    db_path = str(tmp_path / "pool_test.db")
    # Build store without pool first so schema initialises.
    store_init = SQLiteStore(db_path)
    del store_init
    pool = _make_pool(db_path, size=size)
    store = SQLiteStore(db_path, pool=pool)
    return store, pool


# ---------------------------------------------------------------------------
# Concurrency test
# ---------------------------------------------------------------------------


class TestConcurrentReaders:
    def test_16_readers_no_locked_error(self, tmp_path):
        """16 concurrent reader threads against pool-size-4 store — no OperationalError."""
        store, pool = _make_pool_store(tmp_path, size=4)
        repo_id = store.create_repository(str(tmp_path), "test-repo")
        store.store_file(
            repository_id=repo_id,
            relative_path="hello.py",
            language="python",
        )

        errors = []
        results = []

        def read_task():
            try:
                with store._get_connection() as conn:
                    rows = conn.execute("SELECT * FROM files").fetchall()
                    results.append(len(rows))
            except sqlite3.OperationalError as e:
                errors.append(str(e))

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(read_task) for _ in range(16)]
            concurrent.futures.wait(futures)

        assert errors == [], f"OperationalError(s) encountered: {errors}"
        assert len(results) == 16
        assert all(r >= 1 for r in results), "Each reader should see at least 1 file"

        pool.close_all()


# ---------------------------------------------------------------------------
# close_all() behaviour
# ---------------------------------------------------------------------------


class TestCloseAll:
    def test_close_all_idempotent(self, tmp_path):
        """close_all() called twice must not raise."""
        pool = _make_pool(str(tmp_path / "idem.db"))
        pool.close_all()
        pool.close_all()  # second call must be a no-op

    def test_close_all_raises_on_acquire(self, tmp_path):
        """After close_all(), acquire() raises RuntimeError immediately."""
        pool = _make_pool(str(tmp_path / "closed.db"))
        pool.close_all()
        with pytest.raises(RuntimeError):
            with pool.acquire():
                pass


# ---------------------------------------------------------------------------
# Write path round trip
# ---------------------------------------------------------------------------


class TestWritePathWithPool:
    def test_store_file_get_file_round_trip(self, tmp_path):
        """store_file + get_file works correctly with a pool-backed store."""
        store, pool = _make_pool_store(tmp_path)
        repo_id = store.create_repository(str(tmp_path), "write-test-repo")

        file_id = store.store_file(
            repository_id=repo_id,
            relative_path="src/main.py",
            language="python",
        )
        assert isinstance(file_id, int)

        row = store.get_file("src/main.py", repository_id=repo_id)
        assert row is not None
        assert row["language"] == "python"

        pool.close_all()


def _exercise_pool_boundary(path, mode, result):
    """Keep a failing deadlock reproduction bounded to its synthetic child process."""
    pool = _make_pool(str(path), size=1)
    if mode == "shutdown":
        waiting, finished = threading.Event(), threading.Event()
        original_wait = threading.Condition.wait

        def observed_wait(condition, *args, **kwargs):
            if threading.current_thread() is waiter:
                waiting.set()
            return original_wait(condition, *args, **kwargs)

        def borrow():
            try:
                with pool.acquire():
                    result.put("unexpected borrow")
            except RuntimeError:
                result.put("closed")
            finally:
                finished.set()

        waiter = threading.Thread(target=borrow, daemon=True)
        threading.Condition.wait = observed_wait
        try:
            with pool.acquire() as held:
                waiter.start()
                assert waiting.wait(2)
                pool.close_all()
                assert finished.wait(2), "pool shutdown stranded an existing waiter"
                assert held.execute("SELECT 1").fetchone()[0] == 1
            waiter.join(2)
            try:
                held.execute("SELECT 1")
            except sqlite3.ProgrammingError:
                pass
            else:
                raise AssertionError("returned connection was not closed")
        finally:
            threading.Condition.wait = original_wait
    else:
        store = SQLiteStore(str(path), pool=pool)
        with pytest.raises(ValueError, match="abort outer"):
            with store._get_connection() as outer:
                repository_id = store.create_repository("synthetic", "synthetic")
                store.store_file(repository_id, relative_path="sample.py", content_hash="hash-one")
                assert outer.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
                raise ValueError("abort outer")
        with store._get_connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM repositories").fetchone()[0] == 0
        result.put("rolled back")
        store.close()


@pytest.mark.parametrize("mode, expected", [("shutdown", "closed"), ("nested", "rolled back")])
def test_pool_lifetime_boundaries(tmp_path, mode, expected):
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    worker = context.Process(
        target=_exercise_pool_boundary, args=(tmp_path / "boundary.db", mode, result)
    )
    worker.start()
    try:
        worker.join(5)
        assert not worker.is_alive(), "pool operation deadlocked"
        assert worker.exitcode == 0
        assert result.get(timeout=1) == expected
    finally:
        if worker.is_alive():
            worker.terminate()
        worker.join(5)
        worker.close()
        result.close()
        result.join_thread()


def test_cancelled_borrow_rolls_back_before_connection_reuse(tmp_path):
    path = str(tmp_path / "cancelled.db")
    store = SQLiteStore(path, pool=_make_pool(path, size=1))
    try:
        with pytest.raises(KeyboardInterrupt):
            with store._get_connection() as connection:
                connection.execute(
                    "INSERT INTO repositories(path, name) VALUES ('synthetic', 'cancelled')"
                )
                raise KeyboardInterrupt()
        with store._get_connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM repositories").fetchone()[0] == 0
    finally:
        store.close()
