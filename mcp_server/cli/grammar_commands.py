"""``mcp-index grammars``: prefetch and verify tree-sitter grammars.

tree-sitter-language-pack 1.20 downloads parser libraries on first use. Run
``mcp-index grammars prefetch`` while network access is available (install,
image build, CI) so indexing never depends on a first-use download
(Consiliency/Code-Index-MCP#115).
"""

from __future__ import annotations

import json
import sys
from typing import Optional, Tuple

import click

_CACHE_DIR_HELP = (
    "Cache root for parser libraries (default: $TREE_SITTER_LANGUAGE_PACK_CACHE_DIR, "
    "else $XDG_CACHE_HOME, else ~/.cache). Runtime processes must resolve the same root."
)


@click.group("grammars")
def grammars() -> None:
    """Manage the tree-sitter grammar cache used for parsing."""


@grammars.command("prefetch")
@click.option("--cache-dir", type=click.Path(file_okay=False), default=None, help=_CACHE_DIR_HELP)
@click.option(
    "--language",
    "languages",
    multiple=True,
    help="Grammar name to fetch (repeatable). Defaults to every registry grammar.",
)
@click.option(
    "--export-libs",
    type=click.Path(file_okay=False),
    default=None,
    help=(
        "Also copy the parser libraries into this shared, world-readable directory; "
        "set TREE_SITTER_LANGUAGE_PACK_LIBS_DIR to it at runtime (images, multi-user hosts)."
    ),
)
def prefetch_command(
    cache_dir: Optional[str], languages: Tuple[str, ...], export_libs: Optional[str]
) -> None:
    """Download every grammar the registry needs, then verify each one parses."""
    from mcp_server.utils import grammar_cache

    root = grammar_cache.configure_cache_dir(cache_dir)
    requested = list(languages) or None
    try:
        names = grammar_cache.prefetch(requested)
    except Exception as exc:  # noqa: BLE001 - surface the pack's download error
        click.echo(f"Grammar prefetch failed: {type(exc).__name__}: {exc}", err=True)
        sys.exit(1)
    check = grammar_cache.verify(names, offline=True)
    click.echo(f"Cache root: {root}")
    click.echo(f"Parser libraries: {grammar_cache.effective_cache_dir()}")
    click.echo(f"Prefetched and verified {len(check.loaded)}/{len(names)} grammars.")
    if check.ok and export_libs:
        copied = grammar_cache.export_libs(export_libs)
        click.echo(
            f"Exported {len(copied)} parser libraries to {export_libs}; "
            f"set {grammar_cache.LIBS_DIR_ENV}={export_libs} at runtime."
        )
    if not check.ok:
        for name, reason in sorted(check.failed.items()):
            click.echo(f"  FAILED {name}: {reason}", err=True)
        sys.exit(1)


@grammars.command("check")
@click.option("--cache-dir", type=click.Path(file_okay=False), default=None, help=_CACHE_DIR_HELP)
@click.option("--language", "languages", multiple=True, help="Grammar name to check (repeatable).")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON report.")
def check_command(cache_dir: Optional[str], languages: Tuple[str, ...], as_json: bool) -> None:
    """Verify grammars load and parse from the cache without downloading."""
    from mcp_server.utils import grammar_cache

    root = grammar_cache.configure_cache_dir(cache_dir)
    check = grammar_cache.verify(list(languages) or None, offline=True)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "cache_root": str(root) if root else None,
                    "parser_libraries": grammar_cache.effective_cache_dir(),
                    "loaded": check.loaded,
                    "failed": check.failed,
                    "ok": check.ok,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        click.echo(f"Parser libraries: {grammar_cache.effective_cache_dir()}")
        click.echo(f"{len(check.loaded)} grammars load offline; {len(check.failed)} missing.")
        for name, reason in sorted(check.failed.items()):
            click.echo(f"  MISSING {name}: {reason}", err=True)
    if not check.ok:
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover - `python -m` entry for image builds
    grammars()
