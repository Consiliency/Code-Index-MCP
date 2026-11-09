"""P28 STDIO readiness handoff tests."""

from __future__ import annotations

from mcp_server.cli.stdio_runner import _SERVER_INSTRUCTIONS, _build_tool_list

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
    "symbol_lookup": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "search_code": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "get_status": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "list_plugins": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "reindex": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "write_summaries": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    "summarize_sample": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    "handshake": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
}


def _tools_by_name():
    return {tool.name: tool for tool in _build_tool_list()}


def _branch_types(tool_name: str) -> list[str | None]:
    schema = _tools_by_name()[tool_name].output_schema
    return [branch.get("type") for branch in schema.get("oneOf", []) if isinstance(branch, dict)]


def test_public_tool_order_is_frozen():
    assert [tool.name for tool in _build_tool_list()] == EXPECTED_TOOL_ORDER


def test_every_public_tool_has_expected_title_annotations_and_output_schema():
    tools = _tools_by_name()
    for tool_name, expected_title in EXPECTED_TITLES.items():
        tool = tools[tool_name]
        assert tool.title == expected_title
        assert tool.output_schema is not None
        assert tool.annotations is not None
        for field_name, expected_value in EXPECTED_ANNOTATIONS[tool_name].items():
            # Expected keys are wire (camelCase) names; mcp 2.x attributes are snake_case.
            assert tool.annotations.model_dump(by_alias=True)[field_name] is expected_value


def test_only_long_running_mutations_advertise_optional_task_support():
    tools = _tools_by_name()
    assert tools["reindex"].execution is not None
    assert tools["reindex"].execution.task_support == "optional"
    assert tools["write_summaries"].execution is not None
    assert tools["write_summaries"].execution.task_support == "optional"
    for tool_name in (
        "symbol_lookup",
        "search_code",
        "get_status",
        "list_plugins",
        "summarize_sample",
        "handshake",
    ):
        assert tools[tool_name].execution is None


def test_handshake_description_stays_local_stdio_only():
    description = _tools_by_name()["handshake"].description
    assert "local STDIO session" in description
    assert "MCP_CLIENT_SECRET" in description
    assert "remote" not in description.lower()
    assert "bearer" not in description.lower()


def test_server_instructions_are_readiness_gated():
    text = _SERVER_INSTRUCTIONS
    for expected in ("readiness", "ready", "index_unavailable", "native_search", "reindex"):
        assert expected in text


def test_query_tool_descriptions_are_readiness_gated():
    tools = _tools_by_name()
    for tool_name in ("search_code", "symbol_lookup"):
        description = tools[tool_name].description
        for expected in (
            "readiness",
            "ready",
            "index_unavailable",
            "native_search",
            "reindex",
        ):
            assert expected in description


def test_query_tool_descriptions_remove_unconditional_mcp_first_language():
    tools = _tools_by_name()
    forbidden = (
        "[USE BEFORE GREP]",
        "ALWAYS use",
        "Fall back to Grep ONLY",
        "0 results",
        "returns not_found",
    )
    for tool_name in ("search_code", "symbol_lookup"):
        description = tools[tool_name].description
        for phrase in forbidden:
            assert phrase not in description


def test_query_repository_inputs_remain_optional():
    tools = _tools_by_name()
    for tool_name in ("search_code", "symbol_lookup"):
        schema = tools[tool_name].input_schema
        assert "repository" in schema["properties"]
        assert "repository" not in schema.get("required", [])


def test_secondary_tool_descriptions_are_readiness_gated_without_native_fallback():
    tools = _tools_by_name()
    for tool_name in ("reindex", "write_summaries", "summarize_sample"):
        description = tools[tool_name].description
        assert "readiness" in description
        assert "ready" in description
        assert "structured readiness refusal" in description
        assert "native_search" not in description
        assert "safe_fallback" not in description


def test_list_plugins_description_mentions_machine_readable_support_facts():
    description = _tools_by_name()["list_plugins"].description
    for expected in ("machine-readable", "specific-plugin", "registry-only", "activation"):
        assert expected in description


def test_no_argument_tools_preserve_empty_object_contract():
    tools = _tools_by_name()
    for tool_name in ("get_status", "list_plugins"):
        schema = tools[tool_name].input_schema
        assert schema["type"] == "object"
        assert schema["properties"] == {}
        assert schema.get("required", []) == []
        assert schema["additionalProperties"] is False


def test_tool_input_schemas_are_explicit_about_additional_properties_and_defaults():
    tools = _tools_by_name()
    for tool_name, tool in tools.items():
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "required" in schema
        assert schema["additionalProperties"] is False

    search_schema = tools["search_code"].input_schema["properties"]
    assert search_schema["semantic"]["default"] is False
    assert search_schema["fuzzy"]["default"] is False
    assert search_schema["limit"]["default"] == 20
    assert search_schema["include_source_metadata"]["default"] is False
    assert search_schema["source_type"]["enum"] == ["friction", "history"]
    assert "history_labels" in search_schema
    assert "history_repos" in search_schema

    write_schema = tools["write_summaries"].input_schema["properties"]
    assert write_schema["limit"]["default"] == 500

    summarize_schema = tools["summarize_sample"].input_schema["properties"]
    assert summarize_schema["n"]["default"] == 3
    assert summarize_schema["persist"]["default"] is False


def test_structured_result_schemas_do_not_advertise_top_level_array_or_string_payloads():
    assert "array" not in _branch_types("search_code")
    assert "string" not in _branch_types("reindex")
