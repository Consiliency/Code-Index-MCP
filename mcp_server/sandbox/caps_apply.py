"""Apply a CapabilitySet to the current (worker) process.

Called inside the worker subprocess BEFORE the plugin module is imported.

ORDERING CONTRACT (matters for correctness):

  1. ``chdir`` into a scratch temp directory (drop CWD reachability).
  2. Close inherited FDs > 2.
  3. Scrub ``os.environ`` down to ``caps.env_allow``.
  4. If ``caps.network is False`` — monkey-patch ``socket`` to forbid connect.
  5. Soft resource limits via :mod:`resource` (best-effort; swallow failures).
  6. If ``caps.sqlite == "none"`` — monkey-patch ``sqlite3.connect`` to raise.
     If ``caps.sqlite == "readonly"`` — force URI + ``mode=ro`` on connects.
  7. **Do NOT install the ``open`` wrapper yet.** The caller (``worker_main``)
     imports the target plugin module AFTER this function returns, and Python
     opens ``.py``/``.pyc`` files as part of import. Only after the plugin
     module is loaded should :func:`install_fs_guard` be called to lock
     builtins/io/pathlib/os opens to the declared capability roots.

Subprocess is deliberately NOT blanket-blocked: the Go plugin imports
``subprocess`` at module load, and several plugins shell out to language
toolchains. These are cooperative Python API guards, not OS-enforced containment
of hostile plugins, native extensions or child executables.
"""

from __future__ import annotations

import builtins
import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from mcp_server.sandbox.capabilities import CapabilitySet, SandboxViolation

logger = logging.getLogger(__name__)


_ORIGINAL_OPEN: Optional[Callable] = None
_FS_GUARD_INSTALLED: bool = False
_SCRATCH_DIR: Optional[Path] = None


def _close_inherited_fds() -> None:
    """Close every inherited FD except stdin/stdout/stderr."""
    keep = {0, 1, 2}
    try:
        # /proc/self/fd is the precise list on Linux. Snapshot first (listdir
        # itself opens an FD) then close.
        entries = list(os.listdir("/proc/self/fd"))
        fds = []
        for e in entries:
            try:
                fd = int(e)
            except ValueError:
                continue
            if fd not in keep:
                fds.append(fd)
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        return
    except OSError:
        pass

    # Fallback: walk rlimit range. Noisy but portable.
    try:
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        for fd in range(3, min(soft, 4096)):
            try:
                os.close(fd)
            except OSError:
                pass
    except Exception:
        pass


def _scrub_env(env_allow: frozenset) -> None:
    """Remove every environ entry whose key is not in ``env_allow``."""
    to_drop = [k for k in list(os.environ.keys()) if k not in env_allow]
    for k in to_drop:
        try:
            del os.environ[k]
        except KeyError:
            pass


def _patch_socket_deny() -> None:
    """Replace socket.socket / socket.create_connection with SandboxViolation raisers."""
    import socket

    def _denied_socket(*args, **kwargs):
        raise SandboxViolation("network disabled by CapabilitySet")

    def _denied_create_connection(*args, **kwargs):
        raise SandboxViolation("network disabled by CapabilitySet")

    # Replace class __init__ so even already-imported ``socket.socket`` refs
    # hit the guard.
    orig_init = socket.socket.__init__

    def _guarded_init(self, *args, **kwargs):  # type: ignore[no-redef]
        raise SandboxViolation("network disabled by CapabilitySet")

    socket.socket.__init__ = _guarded_init  # type: ignore[assignment]
    socket.create_connection = _denied_create_connection  # type: ignore[assignment]
    # Preserve original for diagnostic test patches, not used at runtime.
    socket._orig_socket_init = orig_init  # type: ignore[attr-defined]


def _apply_rlimits(caps: CapabilitySet) -> None:
    """Best-effort: apply CPU + address-space rlimits; swallow failures."""
    try:
        import resource

        try:
            resource.setrlimit(resource.RLIMIT_CPU, (caps.cpu_seconds, caps.cpu_seconds))
        except (ValueError, OSError) as exc:
            logger.debug("RLIMIT_CPU setrlimit failed: %s", exc)

        try:
            mem_bytes = caps.mem_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except (ValueError, OSError) as exc:
            logger.debug("RLIMIT_AS setrlimit failed: %s", exc)
    except ImportError:
        pass


def _patch_sqlite(caps: CapabilitySet) -> None:
    """Enforce caps.sqlite — none/readonly."""
    try:
        import sqlite3
    except ImportError:
        return

    if caps.sqlite == "none":

        def _denied_connect(*args, **kwargs):
            raise SandboxViolation("sqlite disabled by CapabilitySet")

        sqlite3.connect = sqlite3.dbapi2.connect = _denied_connect
        return

    if caps.sqlite == "readonly":
        orig_connect = sqlite3.connect

        def _readonly_connect(database, *args, **kwargs):  # type: ignore[no-redef]
            name = os.fsdecode(database)
            options = {}
            if name.startswith("file:"):
                parsed = urlsplit(name)
                if parsed.netloc not in ("", "localhost") or parsed.fragment:
                    raise SandboxViolation("unsupported SQLite URI")
                name = unquote(parsed.path)
                options = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if name in ("", ":memory:") or options.get("mode") == "memory":
                raise SandboxViolation("readonly SQLite requires an allowed file")
            path = Path(name).resolve()
            if not _within_allowed(path, caps.fs_read):
                raise SandboxViolation("SQLite read outside capability roots")
            options["mode"] = "ro"
            database = path.as_uri() + "?" + urlencode(options)
            if len(args) >= 7:
                args = (*args[:6], True, *args[7:])
                kwargs.pop("uri", None)
            else:
                kwargs["uri"] = True
            conn = orig_connect(database, *args, **kwargs)
            conn.set_authorizer(
                lambda action, *_: (
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK
                )
            )
            return conn

        sqlite3.connect = sqlite3.dbapi2.connect = _readonly_connect


def _within_allowed(path: str, roots: tuple) -> bool:
    """True if ``path`` resolves inside any of ``roots``."""
    if not roots:
        return False
    try:
        resolved = Path(os.fsdecode(path)).resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    for r in roots:
        try:
            resolved.relative_to(Path(r).resolve(strict=False))
            return True
        except (ValueError, OSError):
            continue
    return False


def install_fs_guard(caps: CapabilitySet) -> None:
    """Guard supported Python open APIs using caps.fs_read/fs_write.

    Must be called AFTER the plugin module is imported — otherwise Python's
    own import machinery (which calls ``open`` on ``.py``/``.pyc``) would
    trip the guard.

    Paths are allowed iff they resolve inside ``fs_read`` (for read modes)
    and/or ``fs_write`` as required by the mode. Resolvable non-stdio descriptors
    follow the same roots. Only this worker's own scratch directory is implicit.
    """
    global _ORIGINAL_OPEN, _FS_GUARD_INSTALLED
    if _FS_GUARD_INSTALLED:
        return
    _FS_GUARD_INSTALLED = True
    _ORIGINAL_OPEN = builtins.open

    scratch = (_SCRATCH_DIR,) if _SCRATCH_DIR is not None else ()
    read_roots = tuple(caps.fs_read) + scratch
    write_roots = tuple(caps.fs_write) + scratch
    original_io_open = io.open
    original_os_open = os.open

    def check(file, *, read, write, dir_fd=None):
        if isinstance(file, int):
            if file in (0, 1, 2):
                return
            try:
                file = os.readlink(f"/proc/self/fd/{file}")
            except OSError as exc:
                raise SandboxViolation("unresolvable file descriptor") from exc
        path = Path(os.fsdecode(file))
        if dir_fd is not None and not path.is_absolute():
            try:
                path = Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / path
            except OSError as exc:
                raise SandboxViolation("unresolvable directory descriptor") from exc
        if read and not _within_allowed(path, read_roots):
            raise SandboxViolation("fs_read denied by capability roots")
        if write and not _within_allowed(path, write_roots):
            raise SandboxViolation("fs_write denied by capability roots")

    def _guarded_open(file, mode="r", *args, **kwargs):
        check(file, read="r" in mode or "+" in mode, write=any(ch in mode for ch in "wax+"))
        return _ORIGINAL_OPEN(file, mode, *args, **kwargs)

    def _guarded_io_open(file, mode="r", *args, **kwargs):
        check(file, read="r" in mode or "+" in mode, write=any(ch in mode for ch in "wax+"))
        return original_io_open(file, mode, *args, **kwargs)

    def _guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
        access = flags & os.O_ACCMODE
        check(
            path,
            read=access != os.O_WRONLY,
            write=access != os.O_RDONLY or bool(flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND)),
            dir_fd=dir_fd,
        )
        return original_os_open(path, flags, mode, dir_fd=dir_fd)

    builtins.open = _guarded_open  # type: ignore[assignment]
    io.open = _guarded_io_open
    os.open = _guarded_os_open


def apply(caps: CapabilitySet) -> None:
    """Apply ``caps`` to the current process (minus FS guard).

    Must run BEFORE the plugin module is imported. The FS guard is a
    separate :func:`install_fs_guard` call so plugin import can succeed.
    """
    global _SCRATCH_DIR
    # 1. chdir to a scratch dir.
    sandbox_tmp = tempfile.mkdtemp(prefix="sandbox-")
    _SCRATCH_DIR = Path(sandbox_tmp).resolve()
    try:
        os.chdir(sandbox_tmp)
    except OSError as exc:
        logger.debug("sandbox chdir failed: %s", exc)

    # 2. close inherited FDs.
    _close_inherited_fds()

    # 3. scrub env.
    _scrub_env(caps.env_allow)

    # 4. network deny.
    if not caps.network:
        _patch_socket_deny()

    # 5. rlimits.
    _apply_rlimits(caps)

    # 6. sqlite policy.
    _patch_sqlite(caps)

    # 7. FS guard deliberately deferred — install after plugin import.
