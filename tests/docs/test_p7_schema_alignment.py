"""P7 schema alignment tests — IF-0-P7-3.

Asserts that _build_tool_list returns Tool entries with correct repository
property presence and description across all path-accepting MCP tools.
"""

import pytest

from mcp_server.cli.stdio_runner import _build_tool_list


class TestP7SchemaAlignment:
    """Verify repository property presence and description in tool schemas."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Cache tool list for all tests."""
        self.tools = {tool.name: tool for tool in _build_tool_list()}

    def test_repository_present_in_path_accepting_tools(self):
        """Assert repository property exists in search_code, symbol_lookup, reindex, write_summaries, summarize_sample."""
        path_tools = [
            "search_code",
            "symbol_lookup",
            "reindex",
            "write_summaries",
            "summarize_sample",
        ]
        for tool_name in path_tools:
            assert tool_name in self.tools, f"Tool {tool_name} not found in tool list"
            input_schema = self.tools[tool_name].input_schema
            assert (
                "properties" in input_schema
            ), f"Tool {tool_name} missing properties in inputSchema"
            assert (
                "repository" in input_schema["properties"]
            ), f"Tool {tool_name} missing repository property"

    def test_repository_description_verbatim(self):
        """Assert repository description matches verbatim across all path-accepting tools."""
        expected_description = (
            "Exact registered repository ID, registered name, or registered path."
        )
        path_tools = [
            "search_code",
            "symbol_lookup",
            "reindex",
            "write_summaries",
            "summarize_sample",
        ]
        for tool_name in path_tools:
            actual_description = self.tools[tool_name].input_schema["properties"]["repository"][
                "description"
            ]
            assert actual_description == expected_description, (
                f"Tool {tool_name} repository description mismatch.\n"
                f"Expected: {expected_description}\n"
                f"Got: {actual_description}"
            )

    def test_repository_type_is_string(self):
        """Assert repository property type is string."""
        path_tools = [
            "search_code",
            "symbol_lookup",
            "reindex",
            "write_summaries",
            "summarize_sample",
        ]
        for tool_name in path_tools:
            repo_prop = self.tools[tool_name].input_schema["properties"]["repository"]
            assert (
                repo_prop.get("type") == "string"
            ), f"Tool {tool_name} repository property must have type='string'"

    def test_repository_not_in_required(self):
        """Assert repository is NOT in any tool's required array."""
        path_tools = [
            "search_code",
            "symbol_lookup",
            "reindex",
            "write_summaries",
            "summarize_sample",
        ]
        for tool_name in path_tools:
            input_schema = self.tools[tool_name].input_schema
            required = input_schema.get("required", [])
            assert (
                "repository" not in required
            ), f"Tool {tool_name} must not have repository in required array"

    def test_repository_accepting_tools_disallow_undeclared_input_properties(self):
        """Assert path-accepting tools keep explicit additionalProperties=false schemas."""
        path_tools = [
            "search_code",
            "symbol_lookup",
            "reindex",
            "write_summaries",
            "summarize_sample",
        ]
        for tool_name in path_tools:
            input_schema = self.tools[tool_name].input_schema
            assert (
                input_schema.get("additionalProperties") is False
            ), f"Tool {tool_name} must set additionalProperties=false"

    def test_repository_absent_from_non_path_tools(self):
        """Assert repository property absent from handshake, get_status, list_plugins."""
        non_path_tools = ["handshake", "get_status", "list_plugins"]
        for tool_name in non_path_tools:
            assert tool_name in self.tools, f"Tool {tool_name} not found in tool list"
            input_schema = self.tools[tool_name].input_schema
            properties = input_schema.get("properties", {})
            assert (
                "repository" not in properties
            ), f"Tool {tool_name} must not have repository property"
            assert (
                input_schema.get("additionalProperties") is False
            ), f"Tool {tool_name} must set additionalProperties=false"
