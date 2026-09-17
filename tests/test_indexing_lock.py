"""Tests for SL-2: per-repo reentrant indexing lock registry (IF-0-P12-2)."""

import multiprocessing
import threading
from contextlib import AbstractContextManager

import pytest

from mcp_server.indexing.lock_registry import IndexingLockRegistry, lock_registry


class TestIndexingLockRegistryReentrancy:
    """acquire() is reentrant for the same repo_id on the same thread."""

    def test_acquire_is_reentrant_same_repo_id(self):
        registry = IndexingLockRegistry()
        # RLock: acquiring twice on the same thread must not deadlock
        with registry.acquire("repo-a"):
            with registry.acquire("repo-a"):
                pass  # must reach here — deadlock would hang forever

    def test_acquire_returns_context_manager(self):
        registry = IndexingLockRegistry()
        cm = registry.acquire("repo-b")
        assert isinstance(cm, AbstractContextManager)


class TestIndexingLockRegistrySerializes:
    """Two threads on the same repo_id are serialized."""

    def test_serializes_two_threads_on_same_repo_id(self):
        registry = IndexingLockRegistry()
        order = []
        order_lock = threading.Lock()

        # Thread A: acquires the lock, records "A_enter", signals, waits, records "A_exit"
        a_entered = threading.Event()
        a_release = threading.Event()

        def thread_a():
            with registry.acquire("repo-x"):
                with order_lock:
                    order.append("A_enter")
                a_entered.set()
                a_release.wait(timeout=5)
                with order_lock:
                    order.append("A_exit")

        # Thread B: waits for A to enter, then tries to acquire
        b_acquired = threading.Event()

        def thread_b():
            a_entered.wait(timeout=5)
            with registry.acquire("repo-x"):
                with order_lock:
                    order.append("B_enter")
            b_acquired.set()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()

        # Let B be blocked, then release A
        a_entered.wait(timeout=5)
        a_release.set()

        ta.join(timeout=5)
        b_acquired.wait(timeout=5)
        tb.join(timeout=5)

        assert order == ["A_enter", "A_exit", "B_enter"]

    def test_different_repo_ids_do_not_serialize(self):
        registry = IndexingLockRegistry()
        both_inside = threading.Barrier(2, timeout=5)
        results = []

        def thread_fn(repo_id):
            with registry.acquire(repo_id):
                both_inside.wait()
                results.append(repo_id)

        ta = threading.Thread(target=thread_fn, args=("repo-1",))
        tb = threading.Thread(target=thread_fn, args=("repo-2",))
        ta.start()
        tb.start()
        ta.join(timeout=5)
        tb.join(timeout=5)

        # Both reached the barrier, so no serialization between distinct repos
        assert set(results) == {"repo-1", "repo-2"}


class TestIndexingLockRegistryConcurrentWatcherAndSync:
    """Concurrent watcher + sync_all_repositories on same repo serializes dispatcher calls."""

    def test_watcher_and_sync_serialize_dispatcher_calls(self):
        """Assert via lock-acquisition order, not timing."""
        registry = IndexingLockRegistry()
        call_log = []
        log_lock = threading.Lock()

        watcher_entered = threading.Event()
        watcher_release = threading.Event()

        def simulated_watcher_dispatch(repo_id: str):
            with registry.acquire(repo_id):
                with log_lock:
                    call_log.append(("watcher", "enter"))
                watcher_entered.set()
                watcher_release.wait(timeout=5)
                with log_lock:
                    call_log.append(("watcher", "exit"))

        def simulated_sync_dispatch(repo_id: str):
            watcher_entered.wait(timeout=5)
            with registry.acquire(repo_id):
                with log_lock:
                    call_log.append(("sync", "enter"))

        sync_done = threading.Event()

        def sync_thread():
            simulated_sync_dispatch("repo-shared")
            sync_done.set()

        watcher_thread = threading.Thread(target=simulated_watcher_dispatch, args=("repo-shared",))
        s_thread = threading.Thread(target=sync_thread)

        watcher_thread.start()
        s_thread.start()

        watcher_entered.wait(timeout=5)
        watcher_release.set()

        watcher_thread.join(timeout=5)
        sync_done.wait(timeout=5)
        s_thread.join(timeout=5)

        # Watcher must fully exit before sync enters
        assert call_log == [("watcher", "enter"), ("watcher", "exit"), ("sync", "enter")]


class TestModuleLevelSingleton:
    """Module-level lock_registry singleton is an IndexingLockRegistry."""

    def test_singleton_is_correct_type(self):
        assert isinstance(lock_registry, IndexingLockRegistry)

    def test_singleton_acquire_is_reentrant(self):
        with lock_registry.acquire("singleton-repo"):
            with lock_registry.acquire("singleton-repo"):
                pass  # must not deadlock


def _hold_process_writer(repo_path, entered, release):
    registry = IndexingLockRegistry()
    with registry.acquire("shared", repo_path=repo_path):
        with registry.acquire("shared", repo_path=repo_path):
            entered.set()
            assert release.wait(10)


def test_process_writer_exclusion_and_crash_release(tmp_path):
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    worker = context.Process(target=_hold_process_writer, args=(tmp_path, entered, release))
    worker.start()
    registry = IndexingLockRegistry()
    try:
        assert entered.wait(5)
        with pytest.raises(TimeoutError, match="writer busy"):
            with registry.acquire("shared", repo_path=tmp_path, timeout=0):
                pytest.fail("two processes entered the same writer boundary")
        worker.terminate()
        worker.join(5)
        with registry.acquire("shared", repo_path=tmp_path, timeout=1):
            assert (tmp_path / ".mcp-index" / "writer.lock").exists()
    finally:
        if worker.is_alive():
            worker.terminate()
        worker.join(5)
        worker.close()
