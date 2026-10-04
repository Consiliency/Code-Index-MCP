"""Server startup CLI commands."""

from __future__ import annotations

import os

import click


def _default_host() -> str:
    return os.getenv("MCP_SERVER_HOST", "127.0.0.1")


def _default_port() -> int:
    try:
        return int(os.getenv("MCP_SERVER_PORT", "8765"))
    except ValueError:
        return 8765


@click.command("serve")
@click.option("--host", default=_default_host, show_default="env MCP_SERVER_HOST or 127.0.0.1")
@click.option(
    "--port",
    default=_default_port,
    type=int,
    show_default="env MCP_SERVER_PORT or 8765",
)
@click.option("--reload", is_flag=True, help="Enable auto-reload for development")
@click.option(
    "--rebuild-on-schema-mismatch",
    is_flag=True,
    default=False,
    help="On schema mismatch, rebuild the index and retry the load.",
)
def serve(host: str, port: int, reload: bool, rebuild_on_schema_mismatch: bool) -> None:
    """Start the FastAPI admin/debug HTTP gateway, not the MCP Streamable HTTP transport."""
    from pathlib import Path

    import uvicorn

    os.environ.setdefault("MCP_INDEX_STORAGE_PATH", str(Path.home() / ".mcp" / "indexes"))
    os.environ.setdefault("MCP_SKIP_PLUGIN_PREINDEX", "true")
    if rebuild_on_schema_mismatch:
        os.environ["MCP_REBUILD_ON_SCHEMA_MISMATCH"] = "1"

    click.echo(
        f"Starting FastAPI admin/debug gateway on http://{host}:{port} "
        "(not the MCP Streamable HTTP transport)"
    )
    # Middleware evaluates explicit proxy trust against the actual socket peer.
    uvicorn.run("mcp_server.gateway:app", host=host, port=port, reload=reload, proxy_headers=False)


@click.command("stdio")
@click.option(
    "--rebuild-on-schema-mismatch",
    is_flag=True,
    default=False,
    help="On schema mismatch, rebuild the index and retry the load.",
)
def stdio(rebuild_on_schema_mismatch: bool) -> None:
    """Start the primary MCP client transport over STDIO (JSON-RPC over stdin/stdout)."""
    from mcp_server.cli.stdio_runner import run

    if rebuild_on_schema_mismatch:
        os.environ["MCP_REBUILD_ON_SCHEMA_MISMATCH"] = "1"

    run()
