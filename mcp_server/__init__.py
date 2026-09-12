"""MCP Server - Local-first code indexer for LLMs."""

# Version information
# Historical GARC soak target: 1.2.0-rc6.
__version__ = "1.4.1"

# Public API exports
__all__ = [
    "__version__",
    "ClientSearchOptions",
    "ClientSearchResult",
    "ClientStatusResult",
    "ClientSymbolResult",
    "ClientReindexResult",
    "IndexItClient",
    "IndexUnavailable",
    "SourceType",
    "SQLiteStore",
    "EnhancedDispatcher",
    "PluginFactory",
    "open_client",
]


# Lazy imports to avoid circular dependencies
# Usage examples:
#   from mcp_server import __version__
#   from mcp_server import SQLiteStore
#   from mcp_server import EnhancedDispatcher, PluginFactory
def __getattr__(name):
    """Lazy import public API components to avoid circular import issues."""
    if name in {
        "ClientSearchOptions",
        "ClientSearchResult",
        "ClientStatusResult",
        "ClientSymbolResult",
        "ClientReindexResult",
        "IndexUnavailable",
        "SourceType",
    }:
        from .client_types import (
            ClientReindexResult,
            ClientSearchOptions,
            ClientSearchResult,
            ClientStatusResult,
            ClientSymbolResult,
            IndexUnavailable,
            SourceType,
        )

        return {
            "ClientSearchOptions": ClientSearchOptions,
            "ClientSearchResult": ClientSearchResult,
            "ClientStatusResult": ClientStatusResult,
            "ClientSymbolResult": ClientSymbolResult,
            "ClientReindexResult": ClientReindexResult,
            "IndexUnavailable": IndexUnavailable,
            "SourceType": SourceType,
        }[name]
    if name in {"IndexItClient", "open_client"}:
        from .client import IndexItClient, open_client

        return {"IndexItClient": IndexItClient, "open_client": open_client}[name]
    if name == "SQLiteStore":
        from .storage.sqlite_store import SQLiteStore

        return SQLiteStore
    if name == "EnhancedDispatcher":
        from .dispatcher.dispatcher_enhanced import EnhancedDispatcher

        return EnhancedDispatcher
    if name == "PluginFactory":
        from .plugins.plugin_factory import PluginFactory

        return PluginFactory
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
