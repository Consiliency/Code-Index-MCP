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
3. otherwise ``$HOME/.cache``.

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

from mcp_server.plugins.language_registry import get_required_grammars

#: Environment variable the language pack reads for its cache root.
CACHE_DIR_ENV = "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"

#: Environment variable for a shared, read-only parser-library directory.
LIBS_DIR_ENV = "TREE_SITTER_LANGUAGE_PACK_LIBS_DIR"

#: Pack grammar names whose library file uses a different C symbol name.
_LIBRARY_SYMBOL_NAMES = {"csharp": "c_sharp"}

#: Smallest snippet every grammar parses; we only need a tree, not valid code.
_PROBE_SOURCE = b"x\n"


def resolve_cache_root(environ: Optional[os._Environ | dict] = None) -> Optional[Path]:
    """Return the cache root the language pack uses, without importing it.

    Returns ``None`` only when no root can be derived (no override, no
    ``XDG_CACHE_HOME`` and no ``HOME``), which is the case the pack itself
    refuses.
    """
    env = os.environ if environ is None else environ
    override = (env.get(CACHE_DIR_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    xdg = (env.get("XDG_CACHE_HOME") or "").strip()
    if xdg:
        return Path(xdg).expanduser()
    home = (env.get("HOME") or "").strip()
    if home:
        return Path(home).expanduser() / ".cache"
    return None


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
    value = (os.environ.get(LIBS_DIR_ENV) or "").strip()
    return Path(value).expanduser() if value else None


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
