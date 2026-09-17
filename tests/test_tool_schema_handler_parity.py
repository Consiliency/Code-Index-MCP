"""Schema-handler parity test — IF-0-P9-3.

Asserts bidirectional parity: every tool schema that advertises a
`repository` property has a handler that extracts it, and every handler
that references `arguments.get("repository")` has a matching schema entry.
"""

from __future__ import annotations

import inspect

import jsonschema
import pytest

from mcp_server.cli.stdio_runner import _build_tool_list
from mcp_server.cli.tool_handlers import (
    handle_get_status,
    handle_list_plugins,
    handle_reindex,
    handle_search_code,
    handle_summarize_sample,
    handle_symbol_lookup,
    handle_write_summaries,
)

TOOL_NAME_TO_HANDLER = {
    "symbol_lookup": handle_symbol_lookup,
    "search_code": handle_search_code,
    "reindex": handle_reindex,
    "write_summaries": handle_write_summaries,
    "summarize_sample": handle_summarize_sample,
    "get_status": handle_get_status,
    "list_plugins": handle_list_plugins,
}

ZERO_ARGUMENT_TOOLS = {"get_status", "list_plugins"}


def test_staged_reindex_output_matches_advertised_schema():
    schema = next(t.outputSchema for t in _build_tool_list() if t.name == "reindex")
    result = {
        "path": "/synthetic/fixture",
        "mode": "staged_full",
        "indexed_files": 1,
        "mutation_performed": True,
        "commit": "a" * 40,
        "recovery": {"previous_state": "missing_index"},
        "semantic": None,
    }
    jsonschema.validate(result, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**result, "mutation_performed": False}, schema)
    jsonschema.validate(
        {
            "error": "Reindex failed",
            "code": "wrong_branch",
            "details": None,
            "readiness": {},
            "mutation_performed": False,
        },
        schema,
    )


def _schema_advertises_repository(tool_schema: dict) -> bool:
    return "repository" in tool_schema.get("inputSchema", {}).get("properties", {})


def _handler_extracts_repository(handler) -> bool:
    src = inspect.getsource(handler)
    return (
        'arguments.get("repository")' in src
        or "arguments['repository']" in src
        or '.get("repository")' in src
    )


def test_schema_advertises_repository_implies_handler_accepts_it():
    """Every tool whose schema has 'repository' must have a handler that extracts it."""
    tool_list = _build_tool_list()
    schema_map = {t.name: t.inputSchema for t in tool_list}

    failures = []
    for tool_name, handler in TOOL_NAME_TO_HANDLER.items():
        tool_schema = schema_map.get(tool_name, {})
        if _schema_advertises_repository({"inputSchema": tool_schema}):
            if not _handler_extracts_repository(handler):
                failures.append(
                    f"{tool_name}: schema advertises 'repository' but handler does not "
                    f'extract arguments.get("repository")'
                )

    assert not failures, "\n".join(failures)


def test_handler_accepts_repository_implies_schema_advertises_it():
    """Every handler that extracts 'repository' must have a schema that advertises it."""
    tool_list = _build_tool_list()
    schema_map = {t.name: t.inputSchema for t in tool_list}

    failures = []
    for tool_name, handler in TOOL_NAME_TO_HANDLER.items():
        if _handler_extracts_repository(handler):
            tool_schema = schema_map.get(tool_name, {})
            if not _schema_advertises_repository({"inputSchema": tool_schema}):
                failures.append(
                    f"{tool_name}: handler extracts 'repository' but schema does not "
                    f"advertise it in inputSchema.properties"
                )

    assert not failures, "\n".join(failures)


def test_every_public_tool_has_output_schema_and_annotations():
    tool_list = _build_tool_list()

    for tool in tool_list:
        assert tool.outputSchema is not None, f"{tool.name} must advertise outputSchema"
        assert tool.annotations is not None, f"{tool.name} must advertise annotations"


def test_mcpstruct_output_schemas_stay_object_shaped_for_structured_results():
    schema_map = {t.name: t.outputSchema for t in _build_tool_list()}
    search_branch_types = [
        branch.get("type")
        for branch in schema_map["search_code"]["oneOf"]
        if isinstance(branch, dict)
    ]
    reindex_branch_types = [
        branch.get("type") for branch in schema_map["reindex"]["oneOf"] if isinstance(branch, dict)
    ]

    assert "array" not in search_branch_types
    assert "string" not in reindex_branch_types


def test_zero_argument_tools_remain_zero_argument_after_metadata_refactor():
    schema_map = {t.name: t.inputSchema for t in _build_tool_list()}

    for tool_name in ZERO_ARGUMENT_TOOLS:
        schema = schema_map[tool_name]
        assert schema["properties"] == {}
        assert schema.get("required", []) == []
        assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("tool_name", "expected_read_only"),
    [
        ("symbol_lookup", True),
        ("search_code", True),
        ("get_status", True),
        ("list_plugins", True),
        ("reindex", False),
        ("write_summaries", False),
        ("summarize_sample", False),
        ("handshake", False),
    ],
)
def test_annotations_match_read_only_posture(tool_name: str, expected_read_only: bool):
    tools = {tool.name: tool for tool in _build_tool_list()}
    assert tools[tool_name].annotations is not None
    assert tools[tool_name].annotations.readOnlyHint is expected_read_only
