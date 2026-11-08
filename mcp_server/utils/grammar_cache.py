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

A warm cache is read-only safe: loading cached grammars needs no write access,
so images can ship a root-owned cache.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from mcp_server.plugins.language_registry import get_required_grammars

#: Environment variable the language pack reads for its cache root.
CACHE_DIR_ENV = "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"

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


def cached_grammars() -> List[str]:
    """Return grammar names already present in the local cache."""
    return sorted(_pack().downloaded_languages())


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
    cached = set(pack.downloaded_languages()) if offline else set()
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
