"""Per-repo reentrant lock registry (IF-0-P12-2)."""

import fcntl
import json
import os
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
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


# ---------------------------------------------------------------------------
# Live staged-build owner (Consiliency/Code-Index-MCP#145)
#
# A staged build holds the cross-process ``writer.lock`` for its whole run and
# leaves the published generation untouched until the publish swap. Readers may
# keep serving that published generation while the build runs, but only while
# its owner is provably alive: a crash mid-build must stay fail-closed. The
# owner record lives next to the writer lock; the lock itself is the liveness
# signal (the kernel drops a flock when its process dies), and the recorded
# pid, process start time and host rule out a different process holding it.

_OWNER_FILE = "rebuild-owner.json"


class WriteGate(str, Enum):
    """What a persistent-write fence decided (Consiliency/Code-Index-MCP#145).

    ``FENCED``: a staged rebuild owns the target generation, or the
    registration or generation changed; work for it is obsolete. ``BUSY``:
    another writer (a sync, ``repository summarize``) holds the writer lock
    while the generation is unchanged; the write is retryable.
    """

    ALLOWED = "allowed"
    FENCED = "fenced"
    BUSY = "busy"

    def __bool__(self) -> bool:
        return self is WriteGate.ALLOWED

    @classmethod
    def of(cls, value: object) -> "WriteGate":
        """Normalize a fence's yield: True/False (legacy) or a WriteGate."""
        if isinstance(value, WriteGate):
            return value
        return cls.ALLOWED if value is True else cls.FENCED


def _index_lock_dir(repo_path: Path) -> Path:
    return Path(repo_path).resolve() / ".mcp-index"


def _proc_start_time(pid: int) -> Optional[str]:
    """Return the kernel start time of ``pid`` from Linux ``/proc``, else None."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name may contain spaces or parentheses; fields follow the last ")".
    fields = stat.rsplit(")", 1)[-1].split()
    # Field 22 (starttime) is the 20th field after "pid (comm)".
    return f"proc:{fields[19]}" if len(fields) > 19 else None


def _psutil_start_time(pid: int) -> Optional[str]:
    """Return the process creation time from psutil (macOS and others), else None."""
    try:
        import psutil

        return f"psutil:{psutil.Process(pid).create_time()!r}"
    except Exception:  # noqa: BLE001 - no identity means not provably alive
        return None


def _process_start_time(pid: int) -> Optional[str]:
    """Return a stable start-time identity for ``pid``, or None when unavailable.

    None means the owner's identity cannot be proven, and callers treat that
    as a dead owner (fail closed).
    """
    return _proc_start_time(pid) or _psutil_start_time(pid)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def writer_lock_held(repo_path: Path) -> bool:
    """Return True when some process currently holds the repository writer lock.

    Probes with a non-blocking shared lock on a fresh descriptor, so a holder in
    this same process is detected too. Never creates the lock file.
    """
    try:
        fd = os.open(_index_lock_dir(repo_path) / "writer.lock", os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def record_rebuild_owner(
    repo_path: Path,
    *,
    registration_id: Optional[str],
    published_generation: Optional[str],
    target_commit: str,
) -> Optional[dict]:
    """Record this process as the owner of a staged build; None if unwritable."""
    owner = {
        "token": os.urandom(16).hex(),
        "pid": os.getpid(),
        "process_start": _process_start_time(os.getpid()),
        "host": socket.gethostname(),
        "registration_id": registration_id,
        "published_generation": published_generation,
        "target_commit": target_commit,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    directory = _index_lock_dir(repo_path)
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{_OWNER_FILE}.", dir=directory)
        temporary = Path(name)
        with os.fdopen(fd, "w") as target:
            json.dump(owner, target)
        os.replace(temporary, directory / _OWNER_FILE)
        temporary = None
    except OSError:
        return None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return owner


def clear_rebuild_owner(repo_path: Path, token: str) -> None:
    """Remove the owner record if it is still ours. Never raises."""
    path = _index_lock_dir(repo_path) / _OWNER_FILE
    try:
        current = json.loads(path.read_text())
        if isinstance(current, dict) and current.get("token") == token:
            path.unlink(missing_ok=True)
    except OSError, ValueError:
        pass


def live_rebuild_owner(
    repo_path: Path,
    *,
    registration_id: Optional[str],
    published_generation: Optional[str],
) -> Optional[dict]:
    """Return the owner record of a staged build whose owner is alive, else None.

    Alive means: the record names this registration and the generation that is
    still published, it was written on this host, its pid exists with the same
    start time, and the writer lock is held. Any doubt returns None, which
    keeps the caller fail-closed; that includes a start time that was not
    recorded or cannot be read now, since a reused pid would otherwise pass.
    """
    try:
        owner = json.loads((_index_lock_dir(repo_path) / _OWNER_FILE).read_text())
    except OSError, ValueError:
        return None
    if not isinstance(owner, dict):
        return None
    pid = owner.get("pid")
    if (
        not isinstance(pid, int)
        or pid <= 0
        or owner.get("registration_id") != registration_id
        or owner.get("published_generation") != published_generation
        or owner.get("host") != socket.gethostname()
        or not _pid_alive(pid)
    ):
        return None
    recorded_start = owner.get("process_start")
    if (
        not isinstance(recorded_start, str)
        or not recorded_start
        or _process_start_time(pid) != recorded_start
    ):
        return None
    if not writer_lock_held(repo_path):
        return None
    return owner
