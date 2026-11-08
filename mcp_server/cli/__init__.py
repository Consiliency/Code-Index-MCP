"""
MCP Server CLI utilities.

This package provides command-line tools for managing the MCP server,
including index management, diagnostics, and maintenance operations.
"""

import click

from .. import __version__
from .grammar_commands import grammars
from .server_commands import serve, stdio


@click.group()
@click.version_option(version=__version__, prog_name="code-index-mcp")
def cli():
    """MCP Server CLI utilities."""


# Lightweight commands registered eagerly
cli.add_command(serve)
cli.add_command(stdio)
cli.add_command(grammars)


def _register_heavy_commands():
    """Lazy-register commands with heavy import chains (avoids circular imports on module load)."""
    from .artifact_commands import artifact
    from .history_commands import history
    from .index_management import index
    from .preflight_commands import preflight
    from .repository_commands import repository
    from .setup_commands import setup

    cli.add_command(index)
    cli.add_command(history)
    cli.add_command(artifact)
    cli.add_command(preflight)
    cli.add_command(repository)
    cli.add_command(setup)


# Register heavy commands immediately when this module is imported normally
# (i.e., not via `python -m mcp_server.cli stdio` which only needs lightweight commands).
# We call this here so existing `from mcp_server.cli import cli` callers still get all commands.
try:
    _register_heavy_commands()
except ImportError:
    pass


if __name__ == "__main__":
    cli()
