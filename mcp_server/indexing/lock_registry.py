"""Per-repo reentrant lock registry (IF-0-P12-2)."""

import fcntl
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional


class IndexingLockRegistry:
    """Thread-safe registry of per-repo RLocks.

    acquire(repo_id) returns the repo's RLock as a context manager.
    RLocks are reentrant: the same thread may acquire the same repo's lock
    multiple times without deadlocking.
    """

    def __init__(self) -> None:
        self._locks: Dict[str, threading.RLock] = {}
        self._registry_lock = threading.Lock()
        self._held = threading.local()

    @contextmanager
    def acquire(self, repo_id: str, *, repo_path: Optional[Path] = None, timeout: float = 30.0):
        """Admit one writer across processes when a registered checkout is supplied.

        The lock lives outside generation files and is never removed on release.
        Calls without a checkout retain the legacy thread-only helper contract.
        """
        key = str(Path(repo_path).resolve()) if repo_path is not None else repo_id
        with self._registry_lock:
            if key not in self._locks:
                self._locks[key] = threading.RLock()
            lock = self._locks[key]
        deadline = time.monotonic() + timeout
        if not lock.acquire(timeout=timeout):
            raise TimeoutError(f"Index writer busy for {repo_id}")
        try:
            if repo_path is None:
                yield
                return
            lock_path = Path(repo_path).resolve() / ".mcp-index" / "writer.lock"
            held = getattr(self._held, "paths", None)
            if held is None:
                held = self._held.paths = set()
            if lock_path in held:
                yield
                return
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"Index writer busy for {repo_id}")
                        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                held.add(lock_path)
                try:
                    yield
                finally:
                    held.remove(lock_path)
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        finally:
            lock.release()


# Module-level singleton (IF-0-P12-2)
lock_registry: IndexingLockRegistry = IndexingLockRegistry()
