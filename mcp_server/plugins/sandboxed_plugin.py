"""Sandboxed plugin adapter — forwards IPlugin calls over subprocess IPC.

``SandboxedPlugin`` wraps a target plugin module and runs it inside a
subprocess governed by a :class:`CapabilitySet`. Every ``IPlugin`` method
call on the adapter becomes a JSON-line envelope over the worker's
stdin/stdout. The adapter surfaces :class:`SandboxCallError` (including
worker-side :class:`SandboxViolation`) for forbidden operations.

Typical use::

    caps = CapabilitySet(fs_read=(Path("/repo"),), fs_write=(), env_allow=frozenset())
    p = SandboxedPlugin("mcp_server.plugins.python_plugin", caps)
    shard = p.indexFile("x.py", "print(1)")
    p.close()
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional

from mcp_server.plugin_base import (
    IndexShard,
    IPlugin,
    Reference,
    SearchOpts,
    SearchResult,
    SymbolDef,
)
from mcp_server.sandbox.capabilities import CapabilitySet
from mcp_server.sandbox.supervisor import SandboxSupervisor

logger = logging.getLogger(__name__)

WORKER_MODULE = "mcp_server.sandbox.worker_main"

# The worker runs under ``-I`` (isolated mode: ignore ``PYTHON*`` variables,
# keep the working directory and script directory off ``sys.path``, skip the
# user site). Site-packages ``.pth`` files still load, so installed and
# editable installs resolve as usual. This bootstrap then pins ``mcp_server`` to
# the package root the host itself imported. When the isolated path does not
# already resolve the package there (for example a plain source checkout), the
# root is prepended; otherwise it is appended so modules beside the package (a
# source checkout's ``tests`` fixtures, for instance) stay importable without
# outranking the standard library or installed packages. If ``mcp_server``
# still resolves elsewhere, the worker fails closed instead of running a
# different copy (#146).
_WORKER_BOOTSTRAP = """\
import importlib, importlib.util, os, runpy, sys
root = sys.argv.pop(1)
def pinned():
    spec = importlib.util.find_spec("mcp_server")
    locs = list(spec.submodule_search_locations or ()) if spec else []
    return bool(locs) and all(
        os.path.dirname(os.path.realpath(p)) == root for p in locs
    )
if root not in sys.path:
    if pinned():
        sys.path.append(root)
    else:
        sys.path.insert(0, root)
importlib.invalidate_caches()
if not pinned():
    sys.stderr.write("sandbox worker: mcp_server does not resolve to the host package\\n")
    sys.exit(3)
runpy.run_module(%r, run_name="__main__", alter_sys=True)
""" % (WORKER_MODULE,)


def host_package_root() -> str:
    """Return the real directory that contains the host's ``mcp_server`` package."""
    import mcp_server

    return os.path.dirname(os.path.realpath(next(iter(mcp_server.__path__))))


def build_worker_command(interpreter: str, plugin_module: str, caps_json: str) -> List[str]:
    """Build an isolated worker command that runs the host's ``mcp_server``.

    Never ``python -m``: that puts the working directory first on ``sys.path``
    and honours ``PYTHONPATH``, so a checkout or environment containing a
    top-level ``mcp_server`` could replace the worker (#146).
    """
    return [
        interpreter,
        "-I",
        "-c",
        _WORKER_BOOTSTRAP,
        host_package_root(),
        plugin_module,
        caps_json,
    ]


class SandboxedPlugin(IPlugin):
    """IPlugin adapter that forwards every call to a sandboxed subprocess."""

    lang: str = ""

    def __init__(
        self,
        plugin_module: str,
        capabilities: CapabilitySet,
        *,
        gh_cmd: str = "python",  # interpreter for worker subprocess, not the gh CLI
    ) -> None:
        self._plugin_module = plugin_module
        self._capabilities = capabilities
        # Default ``"python"`` resolves via PATH; callers that need byte-for-byte
        # interpreter parity (test harness, mcp_server CLI) pass ``sys.executable``.
        interp = gh_cmd if gh_cmd != "python" else sys.executable or "python"
        worker_cmd = build_worker_command(interp, plugin_module, capabilities.to_json())
        self._supervisor = SandboxSupervisor(worker_cmd, capabilities)
        self._closed = False
        self._cached_language: str | None = None

    # ------------------------------------------------------------------ IPlugin

    @property
    def language(self) -> str:
        if self._cached_language is None:
            resp = self._supervisor.call("language", {})
            self._cached_language = str(resp.get("value", ""))
            self.lang = self._cached_language
        return self._cached_language

    def bind(self, ctx) -> None:  # type: ignore[override]
        self._ctx = ctx
        # Forward only JSON-safe scalar fields. SQLiteStore / RepositoryInfo
        # cannot cross process boundaries; sandboxed plugins operate without
        # per-repo SQLite state in this release.
        payload = {
            "repo_id": str(getattr(ctx, "repo_id", "") or ""),
            "workspace_root": str(getattr(ctx, "workspace_root", "") or ""),
            "tracked_branch": str(getattr(ctx, "tracked_branch", "") or ""),
        }
        self._supervisor.call("bind", payload)

    def supports(self, path: str | Path) -> bool:
        resp = self._supervisor.call("supports", {"path": str(path)})
        return bool(resp.get("value", False))

    def indexFile(self, path: str | Path, content: str) -> IndexShard:
        resp = self._supervisor.call("indexFile", {"path": str(path), "content": content})
        # IndexShard is a TypedDict; the raw dict is the shape.
        return resp  # type: ignore[return-value]

    def getDefinition(self, symbol: str) -> SymbolDef | None:
        resp = self._supervisor.call("getDefinition", {"symbol": symbol})
        value = resp.get("value")
        return value  # type: ignore[return-value]

    def findReferences(self, symbol: str) -> Iterable[Reference]:
        resp = self._supervisor.call("findReferences", {"symbol": symbol})
        out: List[Reference] = []
        for item in resp.get("items", []):
            try:
                out.append(
                    Reference(
                        file=item.get("file", ""),
                        start_line=int(item.get("start_line", 0)),
                        end_line=int(item.get("end_line", 0)),
                    )
                )
            except Exception:
                continue
        return out

    def search(self, query: str, opts: SearchOpts | None = None) -> Iterable[SearchResult]:
        resp = self._supervisor.call(
            "search", {"query": query, "opts": dict(opts) if opts else None}
        )
        return list(resp.get("items", []))  # type: ignore[return-value]

    # ------------------------------------------------------------------ lifecycle

    @property
    def worker_pid(self) -> Optional[int]:
        """Return the current worker PID without triggering a spawn."""
        return self._supervisor.worker_pid

    @property
    def is_worker_running(self) -> bool:
        """Return whether the sandbox worker is live."""
        return self._supervisor.is_worker_running

    def worker_rss_bytes(self) -> int:
        """Return live child RSS for resource governance."""
        return self._supervisor.worker_rss_bytes()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._supervisor.close()
        except Exception as exc:
            logger.debug("SandboxedPlugin.close swallowed: %s", exc)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
