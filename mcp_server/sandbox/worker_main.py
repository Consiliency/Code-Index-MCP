"""Worker subprocess entry point.

Invoked as::

    python -m mcp_server.sandbox.worker_main <plugin_module> <caps_json>

The worker:

  1. Parses the capability set from argv[2].
  2. Applies all non-FS caps via :func:`caps_apply.apply`.
  3. Imports the target plugin module.
  4. Installs the ``open`` guard (FS cap) — deferred until here so Python's
     own import machinery is not blocked.
  5. Locates the single ``IPlugin`` subclass in the module.
  6. Reads ``Envelope``\\ s from stdin and dispatches each call to the plugin,
     writing a ``result`` or ``error`` envelope per call.

One Envelope per line. UTF-8. Line frame ends with ``\\n``.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import os
import sys
import traceback
from typing import Any, List, Optional

from mcp_server.plugin_base import IPlugin, Reference
from mcp_server.sandbox import caps_apply
from mcp_server.sandbox.capabilities import CapabilitySet, SandboxViolation
from mcp_server.sandbox.protocol import Envelope, ProtocolError, decode, encode

_MATERIALIZE_CAP = 1000  # max generator results to materialize per call

_MISSING_EXTRA_REMEDIATION = {
    "javalang": "Install the Java plugin extra with `uv sync --locked --extra java`.",
    "p24_missing_extra": "Install the optional dependency required by this plugin.",
}


def _normalize_startup_error(exc: BaseException, plugin_module_name: str) -> dict:
    payload = {
        "type": exc.__class__.__name__,
        "message": str(exc),
        "state": "load_error",
        "language": plugin_module_name.rsplit(".", 1)[-1].replace("_plugin", ""),
        "remediation": None,
    }
    if isinstance(exc, ModuleNotFoundError):
        missing = getattr(exc, "name", "") or str(exc)
        payload["state"] = (
            "missing_extra" if missing in _MISSING_EXTRA_REMEDIATION else "unsupported"
        )
        payload["missing_extra"] = missing
        payload["required_extras"] = [missing]
        payload["remediation"] = _MISSING_EXTRA_REMEDIATION.get(
            missing,
            "The plugin module or one of its optional dependencies is not importable.",
        )
    elif "No IPlugin subclass" in str(exc) or "Multiple IPlugin subclasses" in str(exc):
        payload["state"] = "unsupported"
        payload["remediation"] = (
            "Plugin module does not expose a single IPlugin-compatible Plugin class."
        )
    return payload


def _reference_to_dict(r: Reference) -> dict:
    return {"file": r.file, "start_line": r.start_line, "end_line": r.end_line}


def _normalize_result(method: str, value: Any) -> Any:
    """Coerce plugin return values into JSON-safe shapes."""
    if method == "supports":
        return {"value": bool(value)}
    if method in ("findReferences", "search"):
        items: List[Any] = []
        if value is None:
            return {"items": []}
        for i, item in enumerate(value):
            if i >= _MATERIALIZE_CAP:
                break
            if isinstance(item, Reference):
                items.append(_reference_to_dict(item))
            elif dataclasses.is_dataclass(item):
                items.append(dataclasses.asdict(item))
            elif isinstance(item, dict):
                items.append(item)
            else:
                # TypedDict-like; fall back to __dict__ then str.
                items.append(getattr(item, "__dict__", {"repr": repr(item)}))
        return {"items": items}
    if method == "getDefinition":
        if value is None:
            return {"value": None}
        return {
            "value": (
                dict(value)
                if isinstance(value, dict)
                else getattr(value, "__dict__", {"repr": repr(value)})
            )
        }
    if method == "indexFile":
        # IndexShard is a TypedDict — dict passthrough is JSON-safe.
        return dict(value) if isinstance(value, dict) else {"repr": repr(value)}
    if method in ("bind", "close", "language"):
        if method == "language":
            return {"value": str(value)}
        return {"ok": True}
    # Unknown method — pass through if it's a dict, else wrap.
    return value if isinstance(value, dict) else {"value": value}


def _find_plugin_class(module) -> type:
    """Return the single IPlugin subclass defined in ``module``."""
    candidates: List[type] = []
    for _name, obj in inspect.getmembers(module, inspect.isclass):
        if obj is IPlugin:
            continue
        if issubclass(obj, IPlugin) and obj.__module__ == module.__name__:
            candidates.append(obj)
    if not candidates:
        # Fallback: accept classes defined elsewhere but re-exported. Common
        # pattern in this repo: ``from .plugin import Plugin``.
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if obj is IPlugin:
                continue
            if issubclass(obj, IPlugin):
                candidates.append(obj)
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        raise RuntimeError(f"No IPlugin subclass found in module {module.__name__!r}")
    if len(candidates) > 1:
        # Pick the one literally named "Plugin" if present — deterministic.
        named = [c for c in candidates if c.__name__ == "Plugin"]
        if len(named) == 1:
            return named[0]
        raise RuntimeError(
            f"Multiple IPlugin subclasses in {module.__name__!r}: "
            f"{[c.__name__ for c in candidates]}"
        )
    return candidates[0]


def _instantiate(plugin_cls: type) -> IPlugin:
    """Construct the plugin with permissive defaults.

    Plugins in this repo accept ``sqlite_store=None, enable_semantic=False``.
    If the ctor rejects those kwargs, fall back to no-arg.
    """
    try:
        return plugin_cls(sqlite_store=None, enable_semantic=False)
    except TypeError:
        try:
            return plugin_cls(sqlite_store=None)
        except TypeError:
            return plugin_cls()


def _write_envelope(env: Envelope) -> None:
    sys.stdout.buffer.write(encode(env))
    sys.stdout.buffer.flush()


def _result_env(call_id: str, method: str, payload: dict) -> Envelope:
    return Envelope(v=1, id=call_id, kind="result", method=method, payload=payload)


def _error_env(call_id: str, method: str, exc: BaseException) -> Envelope:
    return Envelope(
        v=1,
        id=call_id,
        kind="error",
        method=method,
        payload={
            "type": exc.__class__.__name__,
            "message": str(exc),
        },
    )


def _startup_error_env(payload: dict) -> Envelope:
    return Envelope(v=1, id="", kind="error", method="startup", payload=payload)


def _dispatch(plugin: IPlugin, method: str, payload: dict) -> Any:
    """Invoke a plugin method by name with the given JSON payload."""
    if method == "bind":
        # Sandboxed bind intentionally drops the live SQLiteStore/registry
        # refs (they can't cross process boundaries). Plugins override bind
        # if they need per-repo state; the default IPlugin.bind is a no-op.
        # We pass a lightweight SimpleNamespace that mimics the few fields
        # plugins commonly read.
        from types import SimpleNamespace

        ctx = SimpleNamespace(
            repo_id=payload.get("repo_id", ""),
            workspace_root=payload.get("workspace_root", ""),
            tracked_branch=payload.get("tracked_branch", ""),
            sqlite_store=None,
            registry_entry=None,
        )
        plugin.bind(ctx)
        return None
    if method == "language":
        return plugin.language
    if method == "supports":
        return plugin.supports(payload["path"])
    if method == "indexFile":
        return plugin.indexFile(payload["path"], payload["content"])
    if method == "getDefinition":
        return plugin.getDefinition(payload["symbol"])
    if method == "findReferences":
        return plugin.findReferences(payload["symbol"])
    if method == "search":
        opts = payload.get("opts")
        return plugin.search(payload["query"], opts)
    raise ValueError(f"Unknown method: {method!r}")


#: Plugin ``lang`` values that differ from the chunker/registry language code.
_CHUNKER_LANGUAGE_ALIASES = {"js": "javascript"}


def _prewarm_chunker_parsers(plugin: Any) -> None:
    """Load the chunker parser for ``plugin``'s language before the FS guard.

    Best effort: a plugin whose language the chunker does not handle, or a
    chunker that is not installed, is left alone.
    """
    lang = getattr(plugin, "lang", None)
    if not isinstance(lang, str) or not lang:
        return
    try:
        from mcp_server.plugins.language_registry import LANGUAGE_CONFIGS

        code = _CHUNKER_LANGUAGE_ALIASES.get(lang, lang)
        if code not in LANGUAGE_CONFIGS or code == "plaintext":
            return
        from chunker.parser import get_parser

        get_parser(code)
    except Exception:  # noqa: BLE001 - warm-up must never block startup or touch stdout
        return


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 3:
        sys.stderr.write("worker_main: usage: <plugin_module> <caps_json>\n")
        return 2

    plugin_module_name = argv[1]
    caps = CapabilitySet.from_json(argv[2])

    # Phase 1: apply all caps EXCEPT the FS guard (import needs real open).
    caps_apply.apply(caps)

    # Phase 2: import plugin module.
    try:
        module = importlib.import_module(plugin_module_name)
        plugin_cls = _find_plugin_class(module)
        os.environ.setdefault("MCP_SKIP_PLUGIN_PREINDEX", "true")
        plugin = _instantiate(plugin_cls)
    except BaseException as exc:
        payload = _normalize_startup_error(exc, plugin_module_name)
        if payload.get("state") == "load_error":
            traceback.print_exc(file=sys.stderr)
        try:
            _write_envelope(_startup_error_env(payload))
        except Exception:
            pass
        return 1

    # Phase 2b: warm the chunker parser for the plugin's language. treesitter-
    # chunker 6 reads installed dist-info metadata the first time it loads the
    # python/javascript grammars, which the FS guard would deny (#115).
    _prewarm_chunker_parsers(plugin)

    # Phase 3: NOW install the FS guard — plugin code is loaded.
    caps_apply.install_fs_guard(caps)

    # Phase 4: IPC loop.
    stdin = sys.stdin.buffer
    while True:
        try:
            line = stdin.readline()
        except Exception:
            return 1
        if not line:
            return 0

        try:
            env = decode(line)
        except ProtocolError as exc:
            # Can't attribute this to a call_id — emit a log envelope and continue.
            try:
                _write_envelope(
                    Envelope(
                        v=1,
                        id="",
                        kind="error",
                        method="",
                        payload={"type": "ProtocolError", "message": str(exc)},
                    )
                )
            except Exception:
                pass
            continue

        if env.kind != "call":
            continue

        if env.method == "close":
            try:
                _write_envelope(_result_env(env.id, env.method, {"ok": True}))
            except Exception:
                pass
            return 0

        try:
            raw = _dispatch(plugin, env.method, env.payload)
            norm = _normalize_result(env.method, raw)
            _write_envelope(_result_env(env.id, env.method, norm))
        except SandboxViolation as exc:
            _write_envelope(_error_env(env.id, env.method, exc))
        except BaseException as exc:
            # Capture traceback to stderr for debugging — stderr is inherited.
            traceback.print_exc()
            try:
                _write_envelope(_error_env(env.id, env.method, exc))
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main(sys.argv))
