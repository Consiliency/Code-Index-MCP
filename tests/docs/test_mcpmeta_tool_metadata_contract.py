"""MCPMETA tool metadata contract tests."""

from __future__ import annotations

from mcp_server.cli.stdio_runner import _build_tool_list

EXPECTED_TOOL_ORDER = [
    "symbol_lookup",
    "search_code",
    "get_status",
    "list_plugins",
    "reindex",
    "write_summaries",
    "summarize_sample",
    "handshake",
]

EXPECTED_TITLES = {
    "symbol_lookup": "Symbol Lookup",
    "search_code": "Search Code",
    "get_status": "Get Status",
    "list_plugins": "List Plugins",
    "reindex": "Reindex Repository",
    "write_summaries": "Write Summaries",
    "summarize_sample": "Summarize Sample",
    "handshake": "Authenticate Session",
}

EXPECTED_ANNOTATIONS = {
    "symbol_lookup": (True, False, True, False),
    "search_code": (True, False, True, False),
    "get_status": (True, False, True, False),
    "list_plugins": (True, False, True, False),
    "reindex": (False, False, False, False),
    "write_summaries": (False, False, False, True),
    "summarize_sample": (False, False, False, True),
    "handshake": (False, False, False, False),
}


def _tool_map():
    return {tool.name: tool for tool in _build_tool_list()}


def test_tool_order_and_titles_are_frozen():
    tools = _build_tool_list()
    assert [tool.name for tool in tools] == EXPECTED_TOOL_ORDER
    assert {tool.name: tool.title for tool in tools} == EXPECTED_TITLES


def test_annotations_are_explicit_for_every_public_tool():
    tools = _tool_map()
    for tool_name, expected in EXPECTED_ANNOTATIONS.items():
        annotations = tools[tool_name].annotations
        assert annotations is not None
        assert (
            annotations.read_only_hint,
            annotations.destructive_hint,
            annotations.idempotent_hint,
            annotations.open_world_hint,
        ) == expected


def test_output_schema_drafts_exist_with_only_planned_task_execution_metadata():
    for tool in _build_tool_list():
        assert tool.output_schema is not None
        assert "oneOf" in tool.output_schema
        dumped = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        assert "structuredContent" not in dumped
        if tool.name in {"reindex", "write_summaries"}:
            assert dumped["execution"]["taskSupport"] == "optional"
        else:
            assert "execution" not in dumped
