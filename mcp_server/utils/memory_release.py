"""Return memory a staged rebuild freed to the OS, and log the server's RSS.

A long-lived server runs every staged rebuild in-process. Each file of the
walk is parsed on a fresh short-lived thread, and glibc gives such threads
their own malloc arenas, which keep freed memory mapped instead of handing it
back. After a rebuild the resident size can then stay gigabytes above the live
working set until later allocation patterns happen to shrink it
(Consiliency/Code-Index-MCP#145). Collecting garbage and asking glibc to trim
every arena once a rebuild ends bounds that retention. The RSS before and after
is logged so an observation can tell retained-but-free memory (the trim drops
it) from live objects (it does not).

Set ``MCP_INDEX_MALLOC_TRIM=false`` to skip the trim; the RSS log stays.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import logging
import os
import sys
from typing import Any, Optional

logger = logging.getLogger(__name__)

ENV_MALLOC_TRIM = "MCP_INDEX_MALLOC_TRIM"

_libc: Any = None
_libc_loaded = False


def current_rss_bytes() -> Optional[int]:
    """Resident set size of this process, or None where /proc is unavailable."""
    try:
        with open("/proc/self/statm", "rb") as statm:
            resident_pages = int(statm.read().split()[1])
    except OSError, ValueError, IndexError:
        return None
    return resident_pages * os.sysconf("SC_PAGE_SIZE")


def _glibc() -> Any:
    global _libc, _libc_loaded
    if not _libc_loaded:
        _libc_loaded = True
        if sys.platform.startswith("linux"):
            try:
                library = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
                trim = getattr(library, "malloc_trim", None)
                if trim is not None:
                    trim.argtypes = [ctypes.c_size_t]
                    trim.restype = ctypes.c_int
                    _libc = library
            except OSError:
                _libc = None
    return _libc


def _trim_enabled() -> bool:
    return os.environ.get(ENV_MALLOC_TRIM, "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def release_rebuild_memory(repo_id: str) -> dict:
    """Collect garbage, trim glibc arenas, and log RSS around it.

    Never raises: memory release is best-effort and must not fail a rebuild.
    """
    before = current_rss_bytes()
    trimmed = False
    try:
        gc.collect()
        libc = _glibc() if _trim_enabled() else None
        if libc is not None:
            libc.malloc_trim(0)
            trimmed = True
    except Exception as exc:  # noqa: BLE001 - best effort only
        logger.debug("Rebuild memory release failed (%s)", type(exc).__name__)
    after = current_rss_bytes()
    if before is not None and after is not None:
        logger.info(
            "Staged rebuild of %s finished; server RSS %d MiB -> %d MiB%s",
            repo_id,
            before // (1024 * 1024),
            after // (1024 * 1024),
            " after malloc_trim" if trimmed else "",
        )
    return {"rss_before": before, "rss_after": after, "trimmed": trimmed}
