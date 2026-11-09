from mcp_server.cli.stdio_runner import _build_tool_list


def _tools_by_name():
    return {tool.name: tool for tool in _build_tool_list()}


def test_tool_descriptions():
    tools = _tools_by_name()
    assert "repository" in tools["search_code"].description
    assert "path_outside_allowed_roots" in tools["search_code"].description
    assert "repository" in tools["symbol_lookup"].description
    assert "path_outside_allowed_roots" in tools["symbol_lookup"].description
    assert "path_outside_allowed_roots" in tools["reindex"].description
    assert "MCP_ALLOWED_ROOTS" in tools["reindex"].description
    assert "path_outside_allowed_roots" in tools["summarize_sample"].description
    assert "MCP_CLIENT_SECRET" in tools["handshake"].description


def test_symbol_lookup_schema_has_repository():
    tools = _tools_by_name()
    assert "repository" in tools["symbol_lookup"].input_schema["properties"]


def test_write_summaries_sandbox_claim():
    tools = _tools_by_name()
    # write_summaries is exempt from path sandbox; must not claim otherwise.
    assert "path_outside_allowed_roots" not in tools["write_summaries"].description
