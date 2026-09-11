"""MCP stdio server runner — IF-0-P2B-3 / IF-0-P18-3 implementation.

Wires the MCP Server, list_tools(), and call_tool() dispatcher using
tool_handlers. Invoked by server_commands:stdio() and the shim at
scripts/cli/mcp_server_cli.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional, Sequence

import anyio
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError

from mcp_server.cli import tool_handlers
from mcp_server.cli.bootstrap import initialize_stateless_services, timeout, validate_index
from mcp_server.cli.handshake import HandshakeGate
from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher
from mcp_server.dispatcher.simple_dispatcher import SimpleDispatcher
from mcp_server.health.repository_readiness import ReadinessClassifier
from mcp_server.metrics.prometheus_exporter import (
    PrometheusExporter,
    get_prometheus_exporter,
    record_tool_call,
)
from mcp_server.plugin_system import PluginManager
from mcp_server.storage.mcp_task_registry import MCPTaskRegistry
from mcp_server.storage.sqlite_store import SQLiteStore
from mcp_server.utils.index_discovery import IndexDiscovery
from mcp_server.watcher import FileWatcher
from mcp_server.watcher.ref_poller import RefPoller
from mcp_server.watcher_multi_repo import MultiRepositoryWatcher

logger = logging.getLogger(__name__)

# Server-level constants
_SERVER_NAME = "code-index-mcp-fast-search"
_SERVER_INSTRUCTIONS = (
    "This server provides a pre-built code index (BM25 + semantic vector search). "
    "Indexed search is authoritative when repository readiness is ready. "
    "Use get_status to inspect readiness, or honor an index_unavailable response from "
    "search_code or symbol_lookup. index_unavailable includes safe_fallback=native_search "
    "and remediation; use native search while following remediation such as reindex. "
    "Ready indexes with no matches return ordinary empty results or not_found."
)

try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        _SERVER_VERSION = _pkg_version("index-it-mcp")
    except PackageNotFoundError:
        _SERVER_VERSION = "unknown"
except Exception:
    _SERVER_VERSION = "unknown"

USE_SIMPLE_DISPATCHER = os.getenv("MCP_USE_SIMPLE_DISPATCHER", "false").lower() == "true"
PLUGIN_LOAD_TIMEOUT = int(os.getenv("MCP_PLUGIN_TIMEOUT", "5"))

# ---------------------------------------------------------------------------
# Module-level mutable state (7 globals + 2 aux)
# ---------------------------------------------------------------------------

dispatcher: EnhancedDispatcher | SimpleDispatcher | None = None
plugin_manager: PluginManager | None = None
sqlite_store: SQLiteStore | None = None
initialization_error: str | None = None
_file_watcher: FileWatcher | None = None
_indexing_thread: threading.Thread | None = None
_fts_rebuild_thread: threading.Thread | None = None
_indexing_total_files: int = 0
_indexing_started_at: Optional[float] = None
_repo_resolver: Any = None
_store_registry: Any = None
_git_index_manager: Any = None
_local_ctx: Any = None  # RepoContext for the local repo, built by initialize_services()
_task_registry: MCPTaskRegistry | None = None


class _LocalRepoResolver:
    """Minimal resolver that returns _local_ctx for any path, used when no registry is available."""

    def classify(self, path: Any) -> Any:
        if _local_ctx is None:
            return None
        return ReadinessClassifier.classify_registered(
            _local_ctx.registry_entry,
            requested_path=Path(path),
        )

    def resolve(self, path: Any) -> Any:
        return _local_ctx


_REPOSITORY_DESCRIPTION = "Exact registered repository ID, registered name, or registered path."
_PATH_OUTSIDE_ALLOWED_ROOTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "error": {"type": "string"},
        "code": {"const": "path_outside_allowed_roots"},
        "path": {"type": "string"},
        "allowed_roots": {"type": "array", "items": {"type": "string"}},
        "hint": {"type": "string"},
    },
    "required": ["error", "code", "path", "allowed_roots", "hint"],
    "additionalProperties": True,
}
_HANDSHAKE_REQUIRED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "error": {"type": "string"},
        "code": {"const": "handshake_required"},
    },
    "required": ["error", "code"],
    "additionalProperties": True,
}
_MISSING_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "error": {"const": "Missing parameter"},
        "details": {"type": "string"},
    },
    "required": ["error", "details"],
    "additionalProperties": True,
}
_INDEX_UNAVAILABLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "error": {"const": "Index unavailable"},
        "code": {"const": "index_unavailable"},
        "tool": {"type": "string"},
        "safe_fallback": {"const": "native_search"},
        "readiness": {"type": "object"},
        "message": {"type": "string"},
        "remediation": {"type": ["string", "array", "object", "null"]},
        "query": {"type": "string"},
        "symbol": {"type": "string"},
    },
    "required": ["error", "code", "tool", "safe_fallback", "readiness", "message"],
    "additionalProperties": True,
}
_SECONDARY_READINESS_REFUSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {"type": "array"},
        "code": {"type": "string"},
        "tool": {"type": "string"},
        "readiness": {"type": "object"},
        "message": {"type": "string"},
        "remediation": {"type": ["string", "array", "object", "null"]},
        "mutation_performed": {"type": "boolean"},
        "persisted": {"type": "boolean"},
    },
    "required": ["results", "code", "tool", "readiness", "message"],
    "additionalProperties": True,
}
_SEMANTIC_REFUSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {"type": "array"},
        "code": {"enum": ["semantic_not_ready", "semantic_search_failed"]},
        "query": {"type": "string"},
        "semantic_requested": {"const": True},
        "semantic_source": {"const": "semantic"},
        "semantic_profile_id": {"type": ["string", "null"]},
        "semantic_collection_name": {"type": ["string", "null"]},
        "semantic_fallback_status": {"type": "string"},
        "semantic_readiness": {"type": ["object", "null"]},
        "message": {"type": "string"},
        "remediation": {"type": ["string", "array", "object", "null"]},
        "details": {"type": "string"},
    },
    "required": [
        "results",
        "code",
        "query",
        "semantic_requested",
        "semantic_source",
        "semantic_fallback_status",
        "message",
    ],
    "additionalProperties": True,
}
_CONFLICTING_SCOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "error": {"const": "Conflicting scope"},
        "code": {"const": "conflicting_path_and_repository"},
        "path": {"type": "string"},
        "paths": {"type": "array", "items": {"type": "string"}},
        "repository": {"type": "string"},
        "hint": {"type": "string"},
    },
    "required": ["error", "code", "hint"],
    "additionalProperties": True,
}
_SUMMARIZATION_UNAVAILABLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "error": {"const": "Summarization not available"},
        "details": {"type": "string"},
    },
    "required": ["error", "details"],
    "additionalProperties": True,
}
_SQLITE_NOT_INITIALIZED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "error": {"const": "sqlite_store not initialized"},
    },
    "required": ["error"],
    "additionalProperties": True,
}


def _tool_annotations(
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
    open_world: bool,
) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


def _object_schema(
    properties: dict[str, Any],
    *,
    required: Sequence[str] = (),
    additional_properties: bool = False,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": additional_properties,
    }


def _tool(
    *,
    name: str,
    title: str,
    description: str,
    input_schema: dict[str, Any],
    annotations: types.ToolAnnotations,
    output_schema: dict[str, Any],
) -> types.Tool:
    return types.Tool(
        name=name,
        title=title,
        description=description,
        inputSchema=input_schema,
        annotations=annotations,
        outputSchema=output_schema,
        execution=(
            types.ToolExecution(taskSupport="optional")
            if name in {"reindex", "write_summaries"}
            else None
        ),
    )


def _structured_payload_from_legacy_text(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return {"results": payload}
    return {"message": str(payload)}


def _payload_is_error(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if "error" in payload:
        return True
    code = payload.get("code")
    if code in {
        "handshake_required",
        "index_unavailable",
        "path_outside_allowed_roots",
        "conflicting_path_and_repository",
        "semantic_not_ready",
        "semantic_search_failed",
        "reindex_failed",
    }:
        return True
    readiness = payload.get("readiness")
    return isinstance(readiness, dict) and readiness.get("ready") is False and bool(code)


def _to_call_tool_result(
    response: Sequence[types.TextContent | types.ImageContent | types.EmbeddedResource],
) -> types.CallToolResult:
    structured_payload: dict[str, Any] | None = None
    if response and isinstance(response[0], types.TextContent):
        try:
            parsed_payload = json.loads(response[0].text)
        except json.JSONDecodeError:
            parsed_payload = {"message": response[0].text}
        structured_payload = _structured_payload_from_legacy_text(parsed_payload)

    return types.CallToolResult(
        content=list(response),
        structuredContent=structured_payload,
        isError=_payload_is_error(structured_payload),
    )


def _build_tool_list() -> list[types.Tool]:
    return [
        _tool(
            name="symbol_lookup",
            title="Symbol Lookup",
            description="Look up a class, function, method, or variable definition from the index when repository readiness is ready. If the tool returns index_unavailable with safe_fallback=native_search, use native search and follow the readiness remediation, such as reindex. Ready misses return not_found with readiness metadata. Accepts optional repository param (registered repo name or filesystem path); filesystem paths must be inside MCP_ALLOWED_ROOTS or the tool returns path_outside_allowed_roots.",
            input_schema=_object_schema(
                {
                    "symbol": {"type": "string", "description": "The symbol name to look up"},
                    "repository": {
                        "type": "string",
                        "description": _REPOSITORY_DESCRIPTION,
                    },
                },
                required=("symbol",),
            ),
            annotations=_tool_annotations(
                read_only=True,
                destructive=False,
                idempotent=True,
                open_world=False,
            ),
            output_schema={
                "oneOf": [
                    _MISSING_PARAMETER_SCHEMA,
                    _HANDSHAKE_REQUIRED_SCHEMA,
                    _PATH_OUTSIDE_ALLOWED_ROOTS_SCHEMA,
                    _INDEX_UNAVAILABLE_SCHEMA,
                    _object_schema(
                        {
                            "symbol": {"type": "string"},
                            "kind": {"type": ["string", "null"]},
                            "language": {"type": ["string", "null"]},
                            "signature": {"type": ["string", "null"]},
                            "doc": {"type": ["string", "null"]},
                            "defined_in": {"type": "string"},
                            "line": {"type": ["integer", "null"]},
                            "span": {"type": ["object", "array", "null"]},
                            "_usage_hint": {"type": "string"},
                        },
                        required=("symbol", "defined_in"),
                        additional_properties=True,
                    ),
                    _object_schema(
                        {
                            "result": {"const": "not_found"},
                            "symbol": {"type": "string"},
                            "message": {"type": "string"},
                            "readiness": {"type": "object"},
                            "index_issues": {"type": "array"},
                            "suggestion": {"type": "string"},
                        },
                        required=("result", "symbol", "message"),
                        additional_properties=True,
                    ),
                ]
            },
        ),
        _tool(
            name="search_code",
            title="Search Code",
            description="Search indexed code by pattern, keyword, or natural language (semantic=true) when repository readiness is ready. If the tool returns index_unavailable with safe_fallback=native_search, use native search and follow the readiness remediation, such as reindex. Ready misses return results=[] with readiness metadata. Accepts optional repository param (registered repo name or filesystem path); filesystem paths must be inside MCP_ALLOWED_ROOTS or the tool returns path_outside_allowed_roots.",
            input_schema=_object_schema(
                {
                    "query": {"type": "string", "description": "The search query"},
                    "repository": {
                        "type": "string",
                        "description": _REPOSITORY_DESCRIPTION,
                    },
                    "semantic": {
                        "type": "boolean",
                        "description": "Whether to use semantic search",
                        "default": False,
                    },
                    "fuzzy": {
                        "type": "boolean",
                        "description": "Trigram-based fuzzy match for misspelled queries",
                        "default": False,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results",
                        "default": 20,
                    },
                    "source_type": {
                        "type": "string",
                        "enum": ["friction", "history"],
                        "description": "Filter results to a specific source metadata type",
                    },
                    "friction_categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional friction categories: hack, todo, fixme, workaround, wish, extraction_hint",
                    },
                    "history_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional history labels to match on history issue documents",
                    },
                    "history_repos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional owner/repo filters for history issue documents",
                    },
                    "include_source_metadata": {
                        "type": "boolean",
                        "description": "Include source metadata on returned results",
                        "default": False,
                    },
                },
                required=("query",),
            ),
            annotations=_tool_annotations(
                read_only=True,
                destructive=False,
                idempotent=True,
                open_world=False,
            ),
            output_schema={
                "oneOf": [
                    _MISSING_PARAMETER_SCHEMA,
                    _HANDSHAKE_REQUIRED_SCHEMA,
                    _PATH_OUTSIDE_ALLOWED_ROOTS_SCHEMA,
                    _INDEX_UNAVAILABLE_SCHEMA,
                    _SEMANTIC_REFUSAL_SCHEMA,
                    _object_schema(
                        {
                            "error": {
                                "enum": [
                                    "Search timeout",
                                    "Search failed",
                                    "Invalid friction categories",
                                    "Invalid search source filters",
                                    "Invalid source type",
                                ]
                            },
                            "details": {"type": "string"},
                            "query": {"type": "string"},
                            "allowed_categories": {"type": "array"},
                            "suggestion": {"type": "string"},
                        },
                        required=("error", "details"),
                        additional_properties=True,
                    ),
                    _object_schema(
                        {
                            "results": {
                                "type": "array",
                                "items": _object_schema(
                                    {
                                        "file": {"type": "string"},
                                        "line": {"type": ["integer", "null"]},
                                        "line_end": {"type": ["integer", "null"]},
                                        "symbol": {"type": ["string", "null"]},
                                        "snippet": {"type": ["string", "null"]},
                                        "last_indexed": {"type": ["string", "null"]},
                                        "semantic_source": {"type": ["string", "null"]},
                                        "semantic_profile_id": {"type": ["string", "null"]},
                                        "semantic_collection_name": {"type": ["string", "null"]},
                                        "source_metadata": {"type": ["object", "null"]},
                                        "_usage_hint": {"type": "string"},
                                    },
                                    required=("file",),
                                    additional_properties=True,
                                ),
                            },
                            "query": {"type": "string"},
                            "message": {"type": "string"},
                            "readiness": {"type": "object"},
                            "indexing_in_progress": {"type": "boolean"},
                            "note": {"type": "string"},
                            "semantic_requested": {"type": "boolean"},
                            "semantic_source": {"type": "string"},
                            "semantic_profile_id": {"type": ["string", "null"]},
                            "semantic_collection_name": {"type": ["string", "null"]},
                            "semantic_fallback_status": {"type": "string"},
                            "source_type": {"type": "string"},
                            "friction_categories": {"type": "array"},
                            "history_labels": {"type": "array"},
                            "history_repos": {"type": "array"},
                            "include_source_metadata": {"type": "boolean"},
                        },
                        required=("results",),
                        additional_properties=True,
                    ),
                ]
            },
        ),
        _tool(
            name="get_status",
            title="Get Status",
            description="Get the status of the code index server. Shows index health, supported languages, and performance statistics.",
            input_schema=_object_schema({}),
            annotations=_tool_annotations(
                read_only=True,
                destructive=False,
                idempotent=True,
                open_world=False,
            ),
            output_schema={
                "oneOf": [
                    _HANDSHAKE_REQUIRED_SCHEMA,
                    _object_schema(
                        {
                            "status": {"type": "string"},
                            "version": {"type": "string"},
                            "features": {"type": "object"},
                            "languages": {"type": "object"},
                            "plugins": {"type": "object"},
                            "performance": {"type": "object"},
                            "operations": {"type": "object"},
                            "health": {"type": "object"},
                            "summarization": {"type": "object"},
                            "repositories": {"type": "array"},
                            "client": {"type": ["object", "null"]},
                        },
                        required=("status", "version", "features", "plugins", "repositories"),
                        additional_properties=True,
                    ),
                ]
            },
        ),
        _tool(
            name="list_plugins",
            title="List Plugins",
            description=(
                "List all loaded plugins and machine-readable plugin availability facts, "
                "including enabled, unsupported, disabled, missing-extra, load-error, "
                "specific-plugin versus registry-only coverage, and default activation posture."
            ),
            input_schema=_object_schema({}),
            annotations=_tool_annotations(
                read_only=True,
                destructive=False,
                idempotent=True,
                open_world=False,
            ),
            output_schema={
                "oneOf": [
                    _HANDSHAKE_REQUIRED_SCHEMA,
                    _object_schema(
                        {
                            "plugin_manager_plugins": {"type": "array"},
                            "supported_languages": {"type": "array"},
                            "loaded_plugins": {"type": "array"},
                            "plugin_availability": {"type": "array"},
                            "availability_counts": {"type": "object"},
                        },
                        required=(
                            "plugin_manager_plugins",
                            "supported_languages",
                            "loaded_plugins",
                            "plugin_availability",
                            "availability_counts",
                        ),
                        additional_properties=True,
                    ),
                ]
            },
        ),
        _tool(
            name="reindex",
            title="Reindex Repository",
            description=(
                "Reindex files in the codebase only when repository readiness is ready. "
                "Non-ready repositories return a structured readiness refusal without mutation. "
                "Updates the index for changed files or specific paths. The path must be inside "
                "MCP_ALLOWED_ROOTS or the tool returns path_outside_allowed_roots."
            ),
            input_schema=_object_schema(
                {
                    "path": {
                        "type": "string",
                        "description": "Optional path to reindex. If not provided, reindexes all files.",
                    },
                    "repository": {
                        "type": "string",
                        "description": _REPOSITORY_DESCRIPTION,
                    },
                },
            ),
            annotations=_tool_annotations(
                read_only=False,
                destructive=False,
                idempotent=False,
                open_world=False,
            ),
            output_schema={
                "oneOf": [
                    _HANDSHAKE_REQUIRED_SCHEMA,
                    _PATH_OUTSIDE_ALLOWED_ROOTS_SCHEMA,
                    _SECONDARY_READINESS_REFUSAL_SCHEMA,
                    _CONFLICTING_SCOPE_SCHEMA,
                    _object_schema(
                        {
                            "error": {"const": "Path not found"},
                            "path": {"type": "string"},
                        },
                        required=("error", "path"),
                        additional_properties=True,
                    ),
                    _object_schema(
                        {
                            "path": {"type": "string"},
                            "mode": {"const": "staged_full"},
                            "indexed_files": {"type": "integer"},
                            "mutation_performed": {"const": True},
                            "commit": {"type": ["string", "null"]},
                            "recovery": {"type": ["object", "null"]},
                            "semantic": {"type": ["object", "null"]},
                        },
                        required=("path", "mode", "indexed_files", "mutation_performed", "commit"),
                        additional_properties=True,
                    ),
                    _object_schema(
                        {
                            "error": {"const": "Full repository rebuild required"},
                            "code": {"const": "full_rebuild_required"},
                            "readiness": {"type": "object"},
                            "mutation_performed": {"const": False},
                        },
                        required=("error", "code", "readiness", "mutation_performed"),
                        additional_properties=True,
                    ),
                    _object_schema(
                        {
                            "path": {"type": "string"},
                            "mode": {"const": "file"},
                            "indexed_files": {"const": 1},
                            "durable_files": {"type": ["integer", "null"]},
                            "mutation_performed": {"const": True},
                            "message": {"type": "string"},
                        },
                        required=(
                            "path",
                            "mode",
                            "indexed_files",
                            "mutation_performed",
                            "message",
                        ),
                        additional_properties=True,
                    ),
                    _object_schema(
                        {
                            "error": {"const": "Reindex failed"},
                            "code": {"type": "string"},
                            "path": {"type": "string"},
                            "message": {"type": "string"},
                            "details": {"type": ["string", "null"]},
                            "mutation_performed": {"const": False},
                        },
                        required=(
                            "error",
                            "code",
                            "mutation_performed",
                        ),
                        additional_properties=True,
                    ),
                    _object_schema(
                        {
                            "path": {"type": "string"},
                            "mode": {"const": "merge"},
                            "mutation_performed": {"const": True},
                            "indexed_files": {"type": ["integer", "null"]},
                            "ignored_files": {"type": ["integer", "null"]},
                            "failed_files": {"type": ["integer", "null"]},
                            "total_files": {"type": ["integer", "null"]},
                            "by_language": {"type": ["object", "null"]},
                            "lexical_rows": {"type": ["integer", "null"]},
                            "durable_files": {"type": ["integer", "null"]},
                            "semantic_indexed": {"type": ["integer", "null"]},
                            "semantic_failed": {"type": ["integer", "null"]},
                            "semantic_skipped": {"type": ["integer", "null"]},
                            "semantic_blocked": {"type": ["integer", "null"]},
                            "semantic_stage": {"type": ["string", "null"]},
                            "summaries_written": {"type": ["integer", "null"]},
                            "summary_chunks_attempted": {"type": ["integer", "null"]},
                            "summary_missing_chunks": {"type": ["integer", "null"]},
                            "total_embedding_units": {"type": ["integer", "null"]},
                            "semantic_error": {"type": ["string", "null"]},
                            "semantic_blocker": {"type": ["string", "null"]},
                            "semantic_paths_queued": {"type": ["integer", "null"]},
                            "semantic_indexer_present": {"type": ["boolean", "null"]},
                            "merge_note": {"type": "string"},
                        },
                        required=("path", "mode", "mutation_performed", "merge_note"),
                        additional_properties=True,
                    ),
                ]
            },
        ),
        _tool(
            name="write_summaries",
            title="Write Summaries",
            description=(
                "Run the full LLM summarization pass over all un-summarized chunks in the index. "
                "Runs only when repository readiness is ready; non-ready repositories return a "
                "structured readiness refusal without persistence. Persists results. Use "
                "summarize_sample first to validate quality."
            ),
            input_schema=_object_schema(
                {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of chunks to summarize in this call.",
                        "default": 500,
                    },
                    "repository": {
                        "type": "string",
                        "description": _REPOSITORY_DESCRIPTION,
                    },
                },
            ),
            annotations=_tool_annotations(
                read_only=False,
                destructive=False,
                idempotent=False,
                open_world=True,
            ),
            output_schema={
                "oneOf": [
                    _HANDSHAKE_REQUIRED_SCHEMA,
                    _PATH_OUTSIDE_ALLOWED_ROOTS_SCHEMA,
                    _SECONDARY_READINESS_REFUSAL_SCHEMA,
                    _SUMMARIZATION_UNAVAILABLE_SCHEMA,
                    _SQLITE_NOT_INITIALIZED_SCHEMA,
                    _object_schema(
                        {
                            "chunks_summarized": {"type": "integer"},
                            "limit": {"type": "integer"},
                            "model_used": {"type": ["string", "null"]},
                            "persisted": {"const": True},
                            "semantic_vectors_written": {"type": "boolean"},
                            "summary_chunks_attempted": {"type": "integer"},
                            "summary_missing_chunks": {"type": "integer"},
                        },
                        required=(
                            "chunks_summarized",
                            "limit",
                            "persisted",
                            "semantic_vectors_written",
                            "summary_chunks_attempted",
                            "summary_missing_chunks",
                        ),
                        additional_properties=True,
                    ),
                ]
            },
        ),
        _tool(
            name="summarize_sample",
            title="Summarize Sample",
            description=(
                "Summarize a sample of indexed files using the LLM and return results "
                "for quality evaluation only when repository readiness is ready; non-ready "
                "repositories return a structured readiness refusal without persistence. "
                "Does not persist results by default. "
                "Each entry in paths is checked against the sandbox; mismatches return path_outside_allowed_roots."
            ),
            input_schema=_object_schema(
                {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific file paths to summarize (optional).",
                    },
                    "n": {
                        "type": "integer",
                        "description": "Number of random files to sample when paths not given.",
                        "default": 3,
                    },
                    "persist": {
                        "type": "boolean",
                        "description": "Save summaries to index (default false for evaluation).",
                        "default": False,
                    },
                    "repository": {
                        "type": "string",
                        "description": _REPOSITORY_DESCRIPTION,
                    },
                },
            ),
            annotations=_tool_annotations(
                read_only=False,
                destructive=False,
                idempotent=False,
                open_world=True,
            ),
            output_schema={
                "oneOf": [
                    _HANDSHAKE_REQUIRED_SCHEMA,
                    _PATH_OUTSIDE_ALLOWED_ROOTS_SCHEMA,
                    _SECONDARY_READINESS_REFUSAL_SCHEMA,
                    _CONFLICTING_SCOPE_SCHEMA,
                    _SUMMARIZATION_UNAVAILABLE_SCHEMA,
                    _SQLITE_NOT_INITIALIZED_SCHEMA,
                    _object_schema(
                        {
                            "files_processed": {"type": "integer"},
                            "total_chunks": {"type": "integer"},
                            "model_used": {"type": ["string", "null"]},
                            "persisted": {"type": "boolean"},
                            "files": {
                                "type": "array",
                                "items": _object_schema(
                                    {
                                        "file_path": {"type": "string"},
                                        "error": {"type": "string"},
                                        "chunk_count": {"type": "integer"},
                                        "summaries": {
                                            "type": "array",
                                            "items": _object_schema(
                                                {
                                                    "symbol": {"type": "string"},
                                                    "lines": {"type": "string"},
                                                    "summary": {"type": "string"},
                                                },
                                                required=("symbol", "lines", "summary"),
                                                additional_properties=True,
                                            ),
                                        },
                                    },
                                    required=("file_path",),
                                    additional_properties=True,
                                ),
                            },
                        },
                        required=("files_processed", "total_chunks", "persisted", "files"),
                        additional_properties=True,
                    ),
                ]
            },
        ),
        _tool(
            name="handshake",
            title="Authenticate Session",
            description=(
                "Authenticate the local STDIO session with the configured "
                "MCP_CLIENT_SECRET guard. Required before other tools when "
                "that local STDIO guard is enabled."
            ),
            input_schema=_object_schema(
                {"secret": {"type": "string"}},
                required=("secret",),
            ),
            annotations=_tool_annotations(
                read_only=False,
                destructive=False,
                idempotent=False,
                open_world=False,
            ),
            output_schema={
                "oneOf": [
                    _MISSING_PARAMETER_SCHEMA,
                    _object_schema(
                        {
                            "authenticated": {"const": True},
                        },
                        required=("authenticated",),
                        additional_properties=True,
                    ),
                    _HANDSHAKE_REQUIRED_SCHEMA,
                ]
            },
        ),
    ]


_shutdown_called = False
_shutdown_task: asyncio.Task | None = None


async def _graceful_shutdown(
    multi_watcher: Any,
    ref_poller: Any,
    store_registry: Any,
    exporter: Any,
    dispatcher: Any = None,
    timeout: float = 5.0,
) -> None:
    """Await one cleanup owner; a timeout never abandons a resource owner."""
    global _shutdown_called, _shutdown_task

    async def cleanup() -> None:
        if _lazy_summarizer is not None:
            await _lazy_summarizer.stop()
        components = [
            ("MultiRepositoryWatcher", multi_watcher, "stop"),
            ("RefPoller", ref_poller, "stop"),
            ("FileWatcher", _file_watcher, "stop"),
        ]
        for name, component, method in components:
            if component is not None:
                await stop_component(name, getattr(component, method))
        for thread in (_indexing_thread, _fts_rebuild_thread):
            if thread is not None and thread.is_alive():
                await asyncio.to_thread(thread.join)
        for name, component, method in [
            ("Dispatcher", dispatcher, "shutdown"),
            ("StoreRegistry", store_registry, "shutdown"),
            ("PrometheusExporter", exporter, "stop"),
        ]:
            if component is not None:
                await stop_component(name, getattr(component, method))

    async def stop_component(name: str, stop: Callable[[], None]) -> None:
        worker = asyncio.create_task(asyncio.to_thread(stop))
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout)
        except asyncio.TimeoutError:
            logger.warning("%s cleanup exceeded %.1fs; awaiting its resource owner", name, timeout)
            await worker
        logger.info("%s stopped", name)

    if not _shutdown_called:
        _shutdown_called = True
        _shutdown_task = asyncio.create_task(cleanup())
    if _shutdown_task is not None:
        with anyio.CancelScope(shield=True):
            await asyncio.shield(_shutdown_task)


def _force_shutdown() -> None:
    """Last resort for an uncooperative worker: fail the entire owning service."""
    import psutil

    # Do not acquire logging locks or write potentially blocked client pipes here.
    try:
        children = psutil.Process().children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(children, timeout=1)
    finally:
        os._exit(1)


async def _stdio_input(
    reader: asyncio.StreamReader, on_eof: Callable[[], None]
) -> AsyncIterator[str]:
    while True:
        line = await reader.readline()
        if not line:
            on_eof()
            return
        yield line.decode("utf-8", errors="replace")


class _StdioOutput:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer

    async def write(self, value: str) -> None:
        self.writer.write(value.encode("utf-8"))
        await self.writer.drain()

    async def flush(self) -> None:
        await self.writer.drain()


# ---------------------------------------------------------------------------
# Module-level initialize_services() — hoisted from _serve()'s lazy_initialize()
# ---------------------------------------------------------------------------


async def initialize_services() -> None:
    """Initialize all services needed for the MCP server."""
    global dispatcher, plugin_manager, sqlite_store, initialization_error
    global _file_watcher, _indexing_thread, _fts_rebuild_thread
    global _indexing_total_files, _indexing_started_at, _local_ctx
    _auto_index = False

    try:
        from mcp_server.core.ignore_patterns import build_walker_filter

        current_dir = Path.cwd()
        enable_multi_path = os.getenv("MCP_ENABLE_MULTI_PATH", "true").lower() == "true"

        logger.info("Searching for index using multi-path discovery...")
        discovery = IndexDiscovery(current_dir, enable_multi_path=enable_multi_path)
        index_info = discovery.get_index_info()

        if not index_info["enabled"]:
            if os.getenv("MCP_INDEX_ENABLED", "").lower() == "false":
                raise RuntimeError("MCP indexing disabled via MCP_INDEX_ENABLED=false")
            explicit_config = discovery.get_index_config()
            if explicit_config is not None and explicit_config.get("enabled") is False:
                raise RuntimeError("MCP indexing disabled in .mcp-index.json")
            logger.info("No MCP index config found — will auto-initialize index on first use")

        index_path = discovery.get_local_index_path()

        if index_path:
            logger.info(f"Found index at: {index_path}")
            sqlite_store = SQLiteStore(str(index_path))

            validation_result = validate_index(sqlite_store, current_dir)
            if not validation_result["valid"]:
                for issue in validation_result["issues"]:
                    logger.warning(f"  - {issue}")

            _vstats = validation_result.get("stats", {})
            if _vstats.get("bm25_documents", 1) == 0 and _vstats.get("total_files", 0) > 0:
                _heal_store = sqlite_store

                def _rebuild_fts():
                    try:
                        rows = _heal_store.rebuild_fts_code()
                        logger.info(f"BM25 FTS rebuild complete: {rows} documents")
                    except Exception as _fts_err:
                        logger.warning(
                            f"BM25 FTS rebuild failed (non-fatal): {type(_fts_err).__name__}"
                        )

                _fts_rebuild_thread = threading.Thread(
                    target=_rebuild_fts, daemon=True, name="mcp-fts-rebuild"
                )
                _fts_rebuild_thread.start()
        else:
            index_dir = current_dir / ".mcp-index"
            index_dir.mkdir(exist_ok=True)
            index_path = index_dir / "code_index.db"
            logger.info(f"No index found — creating new index at {index_path}")
            sqlite_store = SQLiteStore(str(index_path))
            _auto_index = True

            gitignore_path = current_dir / ".gitignore"
            gitignore_entries = [
                ".mcp-index/code_index.db",
                ".mcp-index/code_index.db-shm",
                ".mcp-index/code_index.db-wal",
                ".mcp-index/.index_metadata.json",
            ]
            try:
                existing = (
                    gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
                )
                missing = [e for e in gitignore_entries if e not in existing]
                if missing:
                    with gitignore_path.open("a", encoding="utf-8") as _gf:
                        _gf.write("\n# MCP Index files\n")
                        _gf.write("\n".join(missing) + "\n")
            except Exception as _gi_err:
                logger.debug(f"Could not update .gitignore: {type(_gi_err).__name__}")

        # Initialize dispatcher (simple or enhanced with plugins)
        if USE_SIMPLE_DISPATCHER:
            logger.info("Using SimpleDispatcher (BM25-only mode)")
            dispatcher = SimpleDispatcher()
        else:
            logger.info("Initializing plugin system...")
            config_path = Path("plugins.yaml")
            plugin_manager = PluginManager(sqlite_store=sqlite_store)

            logger.info(f"Loading plugins (timeout: {PLUGIN_LOAD_TIMEOUT}s)...")
            try:
                with timeout(PLUGIN_LOAD_TIMEOUT):
                    load_result = plugin_manager.load_plugins_safe(
                        config_path if config_path.exists() else None
                    )
                    if not load_result.success:
                        logger.error(f"Plugin loading failed: {load_result.error.message}")
                    else:
                        logger.info(f"Plugin loading completed: {load_result.metadata}")
            except TimeoutError:
                logger.warning(f"Plugin loading timed out after {PLUGIN_LOAD_TIMEOUT} seconds")

            active_plugins = plugin_manager.get_active_plugins() if plugin_manager else {}
            plugin_instances = list(active_plugins.values())
            logger.info(f"Loaded {len(plugin_instances)} active plugins from plugin manager")

            logger.info("Creating enhanced dispatcher with timeout protection...")
            _explicit = os.getenv("RERANKER_TYPE", "").strip().lower()
            reranker_type = _explicit if _explicit else "none"
            semantic_registry = None
            try:
                if _repo_resolver is not None and getattr(_repo_resolver, "_registry", None):
                    from mcp_server.utils.semantic_indexer_registry import SemanticIndexerRegistry

                    semantic_registry = SemanticIndexerRegistry(_repo_resolver._registry)
            except Exception as _sem_reg_err:
                logger.warning("Semantic registry unavailable: %s", type(_sem_reg_err).__name__)
            dispatcher = EnhancedDispatcher(
                plugins=plugin_instances,
                enable_advanced_features=True,
                use_plugin_factory=True,
                lazy_load=True,
                semantic_search_enabled=True,
                memory_aware=True,
                multi_repo_enabled=None,
                reranker_type=reranker_type,
                semantic_indexer_registry=semantic_registry,
            )

        _supported = dispatcher.supported_languages
        supported_languages = _supported() if callable(_supported) else _supported
        logger.info(f"Enhanced dispatcher created - supports {len(supported_languages)} languages")

        # Build a minimal RepoContext for local single-repo use when no registry is available
        if sqlite_store is not None and _local_ctx is None:
            try:
                from datetime import datetime as _dt

                from mcp_server.core.repo_context import RepoContext
                from mcp_server.storage.multi_repo_manager import RepositoryInfo
                from mcp_server.storage.repo_identity import (
                    _run_git,
                    compute_repo_id,
                    resolve_tracked_branch,
                )

                _repo_id_result = compute_repo_id(current_dir)
                _repo_id = (
                    _repo_id_result.repo_id
                    if hasattr(_repo_id_result, "repo_id")
                    else str(_repo_id_result)
                )
                _git_common_dir = getattr(_repo_id_result, "git_common_dir", None)
                _current_commit = _run_git(["rev-parse", "HEAD"], current_dir)
                _current_branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], current_dir)
                _tracked_branch = resolve_tracked_branch(_git_common_dir)
                _reg_entry = RepositoryInfo(
                    repository_id=_repo_id,
                    name=current_dir.name,
                    path=current_dir,
                    index_path=Path(sqlite_store.db_path),
                    language_stats={},
                    total_files=0,
                    total_symbols=0,
                    indexed_at=_dt.now(),
                    current_commit=_current_commit,
                    current_branch=_current_branch,
                    tracked_branch=_tracked_branch,
                    git_common_dir=(str(_git_common_dir) if _git_common_dir else None),
                )
                _local_ctx = RepoContext(
                    repo_id=_repo_id,
                    sqlite_store=sqlite_store,
                    workspace_root=current_dir,
                    tracked_branch=_tracked_branch,
                    registry_entry=_reg_entry,
                )
            except Exception as _ctx_err:
                logger.debug(
                    f"Could not build local RepoContext (non-fatal): {type(_ctx_err).__name__}"
                )

        if isinstance(dispatcher, EnhancedDispatcher) and not (
            getattr(dispatcher, "_semantic_registry", None)
            or getattr(dispatcher, "_semantic_indexer_fallback", None)
        ):
            logger.warning(
                "Semantic search not available — running in BM25-only mode. "
                "Set VOYAGE_API_KEY (Voyage AI) or configure a vLLM endpoint in "
                "code-index-mcp.profiles.yaml to enable semantic (vector) search."
            )

        # Start FileWatcher — deferred until after auto-index when _auto_index is True
        if _file_watcher is None and isinstance(dispatcher, EnhancedDispatcher):
            try:
                _file_watcher = FileWatcher(root=current_dir, dispatcher=dispatcher)
                if not _auto_index:
                    _file_watcher.start()
                    logger.info(f"FileWatcher started, watching {current_dir}")
                # else: started inside _run_initial_index() after indexing completes
            except Exception as _fw_err:
                logger.warning(f"FileWatcher failed to start (non-fatal): {type(_fw_err).__name__}")

        # Guard: skip auto-index for very large repos
        if _auto_index:
            _max_files = int(os.getenv("MCP_AUTO_INDEX_MAX_FILES", "100000"))
            _file_count = 0
            _is_excluded = build_walker_filter(current_dir)
            for _root, _dirs, _files in os.walk(current_dir, followlinks=False):
                _dirs[:] = [d for d in _dirs if not _is_excluded(Path(_root) / d)]
                _file_count += len(_files)
                if _file_count > _max_files:
                    break
            if _file_count > _max_files:
                logger.warning(f"Repository has >{_max_files} files — skipping auto-index")
                _auto_index = False

        if (
            _auto_index
            and _indexing_thread is None
            and isinstance(dispatcher, EnhancedDispatcher)
            and os.getenv("MCP_AUTO_INDEX", "true").lower() != "false"
        ):
            _captured_dir = current_dir
            _captured_store = sqlite_store

            _captured_ctx = _local_ctx

            def _run_initial_index():
                logger.info("Background initial index started...")
                try:
                    if _captured_ctx is not None:
                        stats = dispatcher.index_directory(
                            _captured_ctx, _captured_dir, recursive=True
                        )
                    else:
                        # Fallback: walk files and index without ctx (BM25 only via sqlite_store)
                        stats = {"indexed_files": 0}
                        if _captured_store is not None:
                            for _f in _captured_dir.rglob("*.py"):
                                try:
                                    _content = _f.read_text(encoding="utf-8")
                                    _hash = (
                                        __import__("hashlib").sha256(_content.encode()).hexdigest()
                                    )
                                    _fid = _captured_store.store_file(
                                        "local",
                                        str(_f),
                                        str(_f.relative_to(_captured_dir)),
                                        language="python",
                                        size=len(_content),
                                        hash=_hash,
                                    )
                                    if _fid:
                                        _captured_store.store_code_chunk(
                                            _fid, _content, 1, _content.count("\n") + 1
                                        )
                                        stats["indexed_files"] += 1
                                except Exception:
                                    pass
                    if _captured_store:
                        _captured_store.rebuild_fts_code()
                    if (
                        _captured_ctx is not None
                        and _captured_ctx.registry_entry.current_commit
                        and int(stats.get("failed_files", stats.get("failed", 0)) or 0) == 0
                    ):
                        _captured_ctx.registry_entry.last_indexed_commit = (
                            _captured_ctx.registry_entry.current_commit
                        )
                        _captured_ctx.registry_entry.last_indexed_branch = (
                            _captured_ctx.registry_entry.current_branch
                        )
                        _captured_ctx.registry_entry.last_indexed = _dt.now()
                    logger.info(f"Background initial index complete: {stats}")
                except Exception as _idx_err:
                    logger.error(f"Background initial index failed: {type(_idx_err).__name__}")
                finally:
                    if _file_watcher is not None:
                        try:
                            _file_watcher.start()
                            logger.info(
                                f"FileWatcher started after initial index, watching {_captured_dir}"
                            )
                        except Exception as _fw_err:
                            logger.warning(
                                f"FileWatcher failed to start (non-fatal): {type(_fw_err).__name__}"
                            )

            _indexing_total_files = _file_count
            _indexing_started_at = time.time()
            _indexing_thread = threading.Thread(
                target=_run_initial_index, daemon=True, name="mcp-initial-index"
            )
            _indexing_thread.start()
            logger.info(f"Indexing {_captured_dir} in background")

    except Exception as e:
        logger.error(f"Failed to initialize services: {type(e).__name__}")
        initialization_error = str(e)


# ---------------------------------------------------------------------------
# Module-level call_tool() — hoisted from _serve()'s closure
# ---------------------------------------------------------------------------

# Per-process singleton gate and lazy summarizer (scoped to process lifetime)
_gate: Optional[HandshakeGate] = None
_lazy_summarizer: Optional[Any] = None
_init_lock: Optional[asyncio.Lock] = None


async def call_tool(
    name: str, arguments: dict | None
) -> types.CallToolResult | types.CreateTaskResult:
    global _lazy_summarizer, _init_lock, _gate

    # Authenticate before request-scoped setup or any lazy service side effects.
    if _gate is None:
        _gate = HandshakeGate()
        if not _gate.enabled:
            logger.warning("running unauthenticated \u2014 MCP_CLIENT_SECRET not set")
    gate_error = _gate.check(name, arguments or {})
    if gate_error is not None:
        return _to_call_tool_result(
            [types.TextContent(type="text", text=tool_handlers._ensure_response(gate_error))]
        )

    if name == "handshake":
        logger.info("=== MCP Tool Call: handshake args=%s ===", {"secret": "[REDACTED]"})
        start_time = time.time()
        secret = (arguments or {}).get("secret", "")
        payload: dict[str, Any]
        if _gate.verify(secret):
            payload = {"authenticated": True}
            tool_status = "success"
        else:
            payload = {"error": "Invalid secret.", "code": "handshake_required"}
            tool_status = "error"
        record_tool_call(name, tool_status)
        logger.info("=== MCP Tool Response: handshake (%.2fs) ===", time.time() - start_time)
        return _to_call_tool_result(
            [types.TextContent(type="text", text=tool_handlers._ensure_response(payload))]
        )

    _current_session = None
    _client_name = None
    _request_experimental = None
    try:
        from mcp.server.lowlevel.server import request_ctx

        _ctx = request_ctx.get()
        _current_session = _ctx.session
        _request_experimental = _ctx.experimental
        _params = getattr(_current_session, "client_params", None)
        _client_name = getattr(getattr(_params, "clientInfo", None), "name", None)
    except Exception as _e:
        logger.debug("request_ctx not available: %s", type(_e).__name__)

    # Lazy initialize on first call
    if dispatcher is None and sqlite_store is None and initialization_error is None:
        if _init_lock is None:
            _init_lock = asyncio.Lock()
        async with _init_lock:
            if dispatcher is None and sqlite_store is None and initialization_error is None:
                await initialize_services()

    if initialization_error:
        return _to_call_tool_result(
            [
                types.TextContent(
                    type="text",
                    text=tool_handlers._ensure_response(
                        {
                            "error": "MCP server initialization failed",
                            "details": initialization_error,
                            "suggestion": (
                                "No index found. Run 'mcp-index index' or "
                                "'python scripts/reindex_current_repo.py' in your repository root."
                            ),
                        }
                    ),
                )
            ]
        )

    if _lazy_summarizer is None and sqlite_store is not None:
        from mcp_server.config.settings import Settings
        from mcp_server.indexing.summarization import LazyChunkWriter

        _settings = Settings.from_environment()
        _lazy_summarizer = LazyChunkWriter(
            db_path=sqlite_store.db_path,
            qdrant_client=None,
            session=_current_session,
            client_name=_client_name,
            summarization_config=_settings.get_profile_summarization_config(
                _settings.semantic_default_profile
            ),
        )
        _lazy_summarizer.start()
    elif _lazy_summarizer is not None and _current_session is not None:
        _lazy_summarizer.update_session(_current_session)

    log_name = name if name in {tool.name for tool in _build_tool_list()} else "unknown"
    logger.info("MCP tool call: %s", log_name)
    start_time = time.time()

    _effective_resolver = (
        _repo_resolver
        if _repo_resolver is not None
        else (_LocalRepoResolver() if _local_ctx is not None else None)
    )
    common_kwargs = dict(
        arguments=arguments or {},
        dispatcher=dispatcher,
        repo_resolver=_effective_resolver,
    )

    _tool_status = "success"
    try:
        tool_def = next((tool for tool in _build_tool_list() if tool.name == name), None)
        if _request_experimental is not None and tool_def is not None:
            _request_experimental.validate_for_tool(tool_def)
        if name == "symbol_lookup":
            response = await tool_handlers.handle_symbol_lookup(
                **common_kwargs,
                sqlite_store=sqlite_store,
            )
        elif name == "search_code":
            response = await tool_handlers.handle_search_code(
                **common_kwargs,
                sqlite_store=sqlite_store,
                indexing_thread=_indexing_thread,
                lazy_summarizer=_lazy_summarizer,
            )
        elif name == "get_status":
            response = await tool_handlers.handle_get_status(
                **common_kwargs,
                sqlite_store=sqlite_store,
                file_watcher=_file_watcher,
                indexing_thread=_indexing_thread,
                indexing_started_at=_indexing_started_at,
                indexing_total_files=_indexing_total_files,
                lazy_summarizer=_lazy_summarizer,
                server_version=_SERVER_VERSION,
                use_simple_dispatcher=USE_SIMPLE_DISPATCHER,
                current_session=_current_session,
                client_name=_client_name,
            )
        elif name == "list_plugins":
            response = await tool_handlers.handle_list_plugins(
                **common_kwargs,
                plugin_manager=plugin_manager,
            )
        elif name == "reindex":
            response = await tool_handlers.handle_reindex(
                **common_kwargs,
                sqlite_store=sqlite_store,
                request_experimental=_request_experimental,
                task_registry=_task_registry,
                git_index_manager=_git_index_manager,
            )
        elif name == "write_summaries":
            response = await tool_handlers.handle_write_summaries(
                **common_kwargs,
                sqlite_store=sqlite_store,
                lazy_summarizer=_lazy_summarizer,
                current_session=_current_session,
                client_name=_client_name,
                request_experimental=_request_experimental,
                task_registry=_task_registry,
            )
        elif name == "summarize_sample":
            response = await tool_handlers.handle_summarize_sample(
                **common_kwargs,
                sqlite_store=sqlite_store,
                lazy_summarizer=_lazy_summarizer,
                current_session=_current_session,
                client_name=_client_name,
            )
        else:
            response = [
                types.TextContent(
                    type="text",
                    text=tool_handlers._ensure_response(
                        {
                            "error": "Unknown tool",
                            "tool": name,
                            "available_tools": [
                                "symbol_lookup",
                                "search_code",
                                "get_status",
                                "list_plugins",
                                "reindex",
                                "write_summaries",
                                "summarize_sample",
                                "handshake",
                            ],
                        }
                    ),
                )
            ]
    except Exception as e:
        _tool_status = "error"
        if isinstance(e, McpError):
            raise
        logger.error("MCP tool execution failed (%s)", type(e).__name__)
        response = [
            types.TextContent(
                type="text",
                text=tool_handlers._ensure_response(
                    {
                        "error": f"Tool execution failed: {name}",
                        "details": type(e).__name__,
                        "tool": name,
                    }
                ),
            )
        ]

    record_tool_call(log_name, _tool_status)

    elapsed = time.time() - start_time
    logger.info("MCP tool response: %s (%.2fs)", log_name, elapsed)

    if isinstance(response, types.CreateTaskResult):
        return response

    if not response:
        response = [
            types.TextContent(
                type="text",
                text=tool_handlers._ensure_response(
                    {
                        "error": "No response generated",
                        "tool": name,
                        "elapsed": elapsed,
                    }
                ),
            )
        ]

    return _to_call_tool_result(response)


# ---------------------------------------------------------------------------
# _serve() — thin orchestrator
# ---------------------------------------------------------------------------


async def _serve(registry_path=None) -> None:
    """Set up and run the MCP stdio server."""
    global _shutdown_called, _gate, _repo_resolver, _store_registry, _task_registry
    global _git_index_manager, dispatcher

    _shutdown_called = False

    from dotenv import load_dotenv

    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(__import__("sys").stderr)],
    )

    logger.info("=" * 60)
    logger.info("Code Index MCP Server Starting")
    logger.info(
        f"Dispatcher Mode: {'Simple (BM25-only)' if USE_SIMPLE_DISPATCHER else 'Enhanced (with plugins)'}"
    )
    logger.info(f"Plugin Timeout: {PLUGIN_LOAD_TIMEOUT} seconds")
    logger.info("=" * 60)
    from mcp_server.artifacts.attestation import warn_if_gh_attestation_missing

    warn_if_gh_attestation_missing()

    # Process-wide service pool — stateless, no cwd capture
    store_registry, repo_resolver, _disp, repo_registry, git_index_manager = (
        initialize_stateless_services(registry_path=registry_path)
    )
    _repo_resolver = repo_resolver
    _store_registry = store_registry
    _git_index_manager = git_index_manager
    dispatcher = _disp

    # Start Prometheus metrics exporter
    exporter: PrometheusExporter = get_prometheus_exporter()
    if os.getenv("MCP_METRICS_PORT"):
        exporter.start(int(os.environ["MCP_METRICS_PORT"]))

    # Start multi-repo watcher + ref poller eagerly, after registries are ready
    multi_watcher: Optional[MultiRepositoryWatcher] = None
    ref_poller: Optional[RefPoller] = None
    try:
        from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher as _ED

        if isinstance(_disp, _ED):
            multi_watcher = MultiRepositoryWatcher(
                registry=repo_registry,
                dispatcher=_disp,
                index_manager=git_index_manager,
                repo_resolver=repo_resolver,
            )
            ref_poller = RefPoller(
                registry=repo_registry,
                git_index_manager=git_index_manager,
                dispatcher=_disp,
                repo_resolver=repo_resolver,
            )
            multi_watcher.start_watching_all()
            ref_poller.start()
            logger.info("MultiRepositoryWatcher and RefPoller started")
    except Exception as _watcher_err:
        logger.warning("MultiRepositoryWatcher failed to start (%s)", type(_watcher_err).__name__)

    # Install SIGTERM/SIGINT handlers for graceful shutdown
    _loop = asyncio.get_running_loop()
    serve_scope = anyio.CancelScope()
    watchdog: threading.Timer | None = None

    def _handle_signal() -> None:
        nonlocal watchdog
        if watchdog is None:
            logger.info("Transport stopping; shutting down owned resources")
            watchdog = threading.Timer(15.0, _force_shutdown)
            watchdog.daemon = True
            watchdog.start()
        serve_scope.cancel()

    try:
        _loop.add_signal_handler(signal.SIGTERM, _handle_signal)
        _loop.add_signal_handler(signal.SIGINT, _handle_signal)
    except NotImplementedError:
        pass

    # Fresh gate for this server invocation
    _gate = HandshakeGate()
    if not _gate.enabled:
        logger.warning("running unauthenticated \u2014 MCP_CLIENT_SECRET not set")

    server = Server(_SERVER_NAME)
    server.instructions = _SERVER_INSTRUCTIONS
    _task_registry = MCPTaskRegistry()
    server.experimental.enable_tasks(store=_task_registry)

    @server.experimental.cancel_task()
    async def _cancel_task(req: types.CancelTaskRequest) -> types.CancelTaskResult:
        if _task_registry is None:
            raise RuntimeError("Task registry not initialized")
        task = await _task_registry.request_cancellation(
            req.params.taskId,
            status_message="Cancellation requested; waiting for a safe stop point.",
        )
        return types.CancelTaskResult(**task.model_dump())

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return _build_tool_list()

    # Register module-level call_tool via imperative decorator application
    server.call_tool()(call_tool)

    transports = []
    try:
        with serve_scope:
            transport_options = {}
            if os.name == "posix":
                # Cancellable pipes avoid an uninterruptible SDK readline worker
                # when a client keeps stdin open after sending a termination signal.
                reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
                transport, _ = await _loop.connect_read_pipe(
                    lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
                )
                transports.append(transport)
                transport, protocol = await _loop.connect_write_pipe(
                    lambda: asyncio.streams.FlowControlMixin(loop=_loop), sys.stdout.buffer
                )
                transports.append(transport)
                writer = asyncio.StreamWriter(transport, protocol, None, _loop)
                transport_options = {
                    "stdin": _stdio_input(reader, _handle_signal),
                    "stdout": _StdioOutput(writer),
                }
            async with stdio_server(**transport_options) as (read_stream, write_stream):
                await server.run(
                    read_stream,
                    write_stream,
                    server.create_initialization_options(),
                    raise_exceptions=False,
                )
    finally:
        _handle_signal()
        await _graceful_shutdown(
            multi_watcher,
            ref_poller,
            store_registry,
            exporter,
            dispatcher=_disp,
            timeout=5.0,
        )
        for transport in transports:
            transport.close()
        await _loop.shutdown_default_executor()
        if watchdog is not None:
            watchdog.cancel()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                _loop.remove_signal_handler(sig)
            except NotImplementedError:
                pass


def run() -> None:
    """Boot the MCP stdio server."""
    asyncio.run(_serve())


async def main() -> None:
    """Async entry point — used by the shim for asyncio.run(main())."""
    await _serve()


if __name__ == "__main__":
    run()
