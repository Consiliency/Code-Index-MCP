"""Tree-sitter grammar cache management for tree-sitter-language-pack 1.20+.

Language-pack 1.20 no longer bundles parser libraries in its wheel. It downloads
them on first use into a versioned cache
(``<root>/tree-sitter-language-pack/v<version>/libs``). A cold cache without
network access raises ``DownloadError`` (surfaced by treesitter-chunker as
``LanguageNotFoundError``), so the service, CI and container/sandbox images
prefetch every grammar the registry needs ahead of time
(Consiliency/Code-Index-MCP#115).

The cache root resolves the same way the pack resolves it:

1. ``TREE_SITTER_LANGUAGE_PACK_CACHE_DIR`` when set,
2. otherwise ``$XDG_CACHE_HOME``,
3. otherwise ``$HOME/.cache``,
4. otherwise ``<passwd home of the running uid>/.cache``.

The pack refuses a cache directory that is not owned by the running uid or is
group- or other-writable, so a cache is per user. A warm cache loads without
write access. For images and other shared installs, export the prefetched
parser libraries to a shared directory and point
``TREE_SITTER_LANGUAGE_PACK_LIBS_DIR`` at it: the pack loads that directory
first, without the ownership check and without touching ``$HOME``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

#: Environment variable the language pack reads for its cache root.
CACHE_DIR_ENV = "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"

#: Environment variable for a shared, read-only parser-library directory.
LIBS_DIR_ENV = "TREE_SITTER_LANGUAGE_PACK_LIBS_DIR"

#: Environment variable the pack reads (lazily) for its download manifest URL.
MANIFEST_URL_ENV = "TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL"

#: Manifest URL for network-denied processes. The pack's HTTP client rejects
#: the unknown scheme before it creates a socket, so even a download path the
#: Python guard cannot see fails without touching the network.
OFFLINE_MANIFEST_URL = "sandbox-offline://network-denied"

#: Pack entry points that download, write the cache or reconfigure it. A
#: network-denied process never calls them.
_OFFLINE_REFUSED = (
    "clean_cache",
    "configure",
    "download",
    "download_all",
    "download_group",
    "init",
    "manifest_groups",
    "manifest_languages",
    "prefetch",
)

#: Pack entry points that load a grammar by name; allowed only for grammars
#: already extracted in the cache or present in the shared libs dir.
_OFFLINE_GUARDED = ("get_language", "get_parser")

#: Pack grammar names whose library file uses a different C symbol name.
_LIBRARY_SYMBOL_NAMES = {"csharp": "c_sharp"}

#: Smallest snippet every grammar parses; we only need a tree, not valid code.
_PROBE_SOURCE = b"x\n"


def _passwd_home() -> Optional[Path]:
    try:
        import pwd

        home = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, AttributeError, OSError):
        return None
    return Path(home) if home else None


def _absolute(value: str, cwd: Optional[Path]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (cwd if cwd is not None else Path.cwd()) / path
    return path


def resolve_cache_root(
    environ: Optional[os._Environ | dict] = None, *, cwd: Optional[Path] = None
) -> Optional[Path]:
    """Return the absolute cache root the language pack uses, without importing it.

    Mirrors the pack: the override, then ``XDG_CACHE_HOME``, then
    ``$HOME/.cache``, then the running uid's passwd home. A relative value is
    resolved against ``cwd`` (default: the current directory), so a caller
    that is about to ``chdir`` keeps pointing at the same cache. Returns
    ``None`` only when no root can be derived at all.
    """
    env = os.environ if environ is None else environ
    override = (env.get(CACHE_DIR_ENV) or "").strip()
    if override:
        return _absolute(override, cwd)
    xdg = (env.get("XDG_CACHE_HOME") or "").strip()
    if xdg:
        return _absolute(xdg, cwd)
    home = (env.get("HOME") or "").strip()
    if home:
        return _absolute(home, cwd) / ".cache"
    passwd_home = _passwd_home()
    if passwd_home is not None:
        return passwd_home / ".cache"
    return None


def resolve_libs_dir(
    environ: Optional[os._Environ | dict] = None, *, cwd: Optional[Path] = None
) -> Optional[Path]:
    """Return the absolute shared parser-library directory, when configured."""
    env = os.environ if environ is None else environ
    value = (env.get(LIBS_DIR_ENV) or "").strip()
    return _absolute(value, cwd) if value else None


def _pack():
    import tree_sitter_language_pack

    return tree_sitter_language_pack


def configure_cache_dir(cache_dir: Optional[str | Path]) -> Optional[Path]:
    """Point the language pack (and child processes) at ``cache_dir``.

    Sets ``TREE_SITTER_LANGUAGE_PACK_CACHE_DIR`` for this process and its
    children and applies it to the already-imported pack. ``None`` keeps the
    current resolution. Returns the effective cache root.
    """
    if cache_dir is not None:
        root = Path(cache_dir).expanduser().resolve()
        os.environ[CACHE_DIR_ENV] = str(root)
        pack = _pack()
        pack.configure(pack.PackConfig(cache_dir=str(root)))
        return root
    return resolve_cache_root()


def effective_cache_dir() -> str:
    """Return the pack's versioned parser-library directory."""
    return str(_pack().cache_dir())


def libs_dir() -> Optional[Path]:
    """Return the shared parser-library directory, when configured."""
    return resolve_libs_dir()


def _library_names(directory: Path) -> set:
    names = set()
    if directory.is_dir():
        for entry in directory.iterdir():
            stem = entry.name.split(".", 1)[0]
            if stem.startswith("libtree_sitter_"):
                names.add(stem[len("libtree_sitter_") :])
            elif stem.startswith("tree_sitter_"):
                names.add(stem[len("tree_sitter_") :])
    return names


def get_required_grammars() -> List[str]:
    """Return every pack grammar name the language registry needs."""
    from mcp_server.plugins.language_registry import get_required_grammars as _required

    return _required()


def cached_grammars() -> List[str]:
    """Return grammar names loadable without a download (cache or libs dir)."""
    names = set(_pack().downloaded_languages())
    shared = libs_dir()
    if shared is not None:
        symbols = _library_names(shared)
        names.update(name for name in get_required_grammars() if _symbol(name) in symbols)
        names.update(symbols)
    return sorted(names)


def _symbol(name: str) -> str:
    return _LIBRARY_SYMBOL_NAMES.get(name, name)


def export_libs(destination: str | Path) -> List[str]:
    """Copy the cached parser libraries into a shared, world-readable directory.

    Point ``TREE_SITTER_LANGUAGE_PACK_LIBS_DIR`` at ``destination`` so any user
    loads them offline without a per-user cache. Returns the copied file names.
    """
    import shutil

    source = Path(effective_cache_dir())
    target = Path(destination).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for entry in sorted(source.iterdir()):
        if entry.is_file():
            shutil.copyfile(entry, target / entry.name)
            (target / entry.name).chmod(0o644)
            copied.append(entry.name)
    target.chmod(0o755)
    return copied


def missing_grammars(languages: Optional[Iterable[str]] = None) -> List[str]:
    """Return requested grammars that are not in the local cache yet."""
    wanted = list(languages) if languages is not None else get_required_grammars()
    cached = set(cached_grammars())
    return [name for name in wanted if name not in cached]


def prefetch(languages: Optional[Sequence[str]] = None) -> List[str]:
    """Download (if needed) and load every requested grammar.

    Defaults to every grammar the language registry advertises. Raises the
    pack's ``DownloadError``/``RuntimeError`` when a grammar cannot be fetched.
    Returns the grammar names that were prefetched.
    """
    names = sorted(set(languages if languages is not None else get_required_grammars()))
    if names:
        _pack().prefetch(names)
    return names


@dataclass
class GrammarCheck:
    """Result of loading and parsing each requested grammar from the cache."""

    loaded: List[str] = field(default_factory=list)
    failed: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed


def verify(languages: Optional[Sequence[str]] = None, *, offline: bool = True) -> GrammarCheck:
    """Load and parse every requested grammar.

    With ``offline=True`` (default) a grammar missing from the cache is reported
    as a failure instead of triggering a first-use download.
    """
    from tree_sitter import Parser

    pack = _pack()
    names = sorted(set(languages if languages is not None else get_required_grammars()))
    cached = set(cached_grammars()) if offline else set()
    result = GrammarCheck()
    for name in names:
        if offline and name not in cached:
            result.failed[name] = "not in local grammar cache (run `mcp-index grammars prefetch`)"
            continue
        try:
            parser = Parser(pack.get_language(name))
            tree = parser.parse(_PROBE_SOURCE)
            if tree is None or tree.root_node is None:
                raise RuntimeError("parser returned no tree")
        except Exception as exc:  # noqa: BLE001 - report every grammar failure
            result.failed[name] = f"{type(exc).__name__}: {exc}"
            continue
        result.loaded.append(name)
    return result


class OfflineGrammarGuard:
    """Refuses language-pack downloads and cache writes in a network-denied process.

    Installed by the sandbox worker when its capabilities deny network. A
    grammar loads only when it is already extracted in the cache or present
    in the shared libs dir; anything else raises the pack's ``DownloadError``
    before native code runs, so a cold or rolled-over cache fails closed
    without a connection and without writing the host cache.
    """

    def __init__(self, pack) -> None:
        self._pack = pack
        self._available: Optional[set] = None

    def _loadable(self, name: str) -> bool:
        candidates = {name, _symbol(name)}
        try:
            from mcp_server.plugins.language_registry import get_grammar_name

            grammar = get_grammar_name(name)
        except Exception:  # noqa: BLE001 - registry lookup is best effort
            grammar = None
        if grammar:
            candidates.update({grammar, _symbol(grammar)})
        for refresh in (False, True):
            if self._available is None or refresh:
                self._available = set(cached_grammars())
            if candidates & self._available:
                return True
        return False

    def require(self, name: object) -> None:
        if not isinstance(name, str) or not self._loadable(name):
            raise self._pack.DownloadError(
                f"grammar {name!r} is not in the local grammar cache, and this "
                "process may not download it (network denied). Run "
                "`mcp-index grammars prefetch` on the host."
            )

    def refuse(self, entry_point: str) -> None:
        raise self._pack.DownloadError(
            f"tree_sitter_language_pack.{entry_point} is disabled in a "
            "network-denied process; grammars must be prefetched on the host."
        )


def install_offline_guard() -> OfflineGrammarGuard:
    """Make this process load grammars from the local cache only.

    Points the pack's manifest URL at :data:`OFFLINE_MANIFEST_URL` (no socket
    is ever created for it) and wraps the pack's public entry points so a
    grammar that is not already extracted raises ``DownloadError`` instead of
    downloading or extracting into the cache. Idempotent.
    """
    import functools

    pack = _pack()
    os.environ[MANIFEST_URL_ENV] = OFFLINE_MANIFEST_URL
    os.environ.pop("TREE_SITTER_LANGUAGE_PACK_TLS_ROOTS", None)
    existing = getattr(pack, "_mcp_offline_guard", None)
    if existing is not None:
        return existing
    guard = OfflineGrammarGuard(pack)
    modules = [pack]
    api = getattr(pack, "api", None)
    if api is not None:
        modules.append(api)

    def _guarded(original):
        @functools.wraps(original)
        def wrapper(name, *args, **kwargs):
            guard.require(name)
            return original(name, *args, **kwargs)

        return wrapper

    def _refused(entry_point):
        def wrapper(*_args, **_kwargs):
            guard.refuse(entry_point)

        wrapper.__name__ = entry_point
        return wrapper

    def _guarded_process(original):
        @functools.wraps(original)
        def wrapper(source, config, *args, **kwargs):
            language = getattr(config, "language", None)
            if language is None and isinstance(config, dict):
                language = config.get("language")
            if language is None and isinstance(config, str):
                import json

                try:
                    language = json.loads(config).get("language")
                except (ValueError, AttributeError):
                    language = None
            guard.require(language)
            return original(source, config, *args, **kwargs)

        return wrapper

    for module in modules:
        for entry_point in _OFFLINE_GUARDED:
            original = getattr(module, entry_point, None)
            if original is not None:
                setattr(module, entry_point, _guarded(original))
        for entry_point in _OFFLINE_REFUSED:
            if hasattr(module, entry_point):
                setattr(module, entry_point, _refused(entry_point))
        if hasattr(module, "process"):
            setattr(module, "process", _guarded_process(getattr(module, "process")))
    pack._mcp_offline_guard = guard
    return guard
