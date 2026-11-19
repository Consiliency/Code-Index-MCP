"""
MCP Health Check Utilities

Quick health checks and diagnostics for MCP tool availability.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp_server.core.path_utils import PathUtils

logger = logging.getLogger(__name__)


class MCPDiagnostics:
    """Comprehensive diagnostics for MCP issues."""

    def __init__(self):
        """Initialize diagnostics."""
        self.diagnostics_data: Dict[str, Any] = {}

    async def run_diagnostics(self) -> Dict[str, Any]:
        """
        Run comprehensive MCP diagnostics.

        Returns:
            Dictionary with diagnostic results
        """
        logger.info("Running MCP diagnostics...")

        results = {
            "timestamp": datetime.now().isoformat(),
            "diagnostics": {},
            "issues_found": [],
            "suggested_fixes": [],
        }

        # Run diagnostic checks
        diagnostics = [
            ("tool_availability", self._diagnose_tool_availability()),
            ("configuration", self._diagnose_configuration()),
            ("index_access", self._diagnose_index_access()),
            ("sub_agent_setup", self._diagnose_sub_agent_setup()),
            ("environment_setup", self._diagnose_environment()),
        ]

        # Execute diagnostics concurrently
        diag_results = await asyncio.gather(*[diag[1] for diag in diagnostics])

        # Compile results
        for (name, _), result in zip(diagnostics, diag_results):
            results["diagnostics"][name] = result
            results["issues_found"].extend(result.get("issues", []))

        # Generate suggested fixes
        results["suggested_fixes"] = self._generate_fixes(results["diagnostics"])

        self.diagnostics_data = results
        return results

    async def _diagnose_tool_availability(self) -> Dict[str, Any]:
        """Diagnose MCP tool availability issues."""
        result = {"status": "checking", "details": {}, "issues": []}

        try:
            # Check environment variables
            mcp_vars = {k: v for k, v in os.environ.items() if k.startswith("MCP_")}
            result["details"]["mcp_env_vars"] = len(mcp_vars)

            # Check specific tools
            tool_checks = {
                "config_propagation": "MCP_INHERIT_CONFIG" in mcp_vars,
                "tool_propagation": "MCP_PROPAGATE_TOOLS" in mcp_vars,
                "sub_agent_access": "MCP_SUB_AGENT_ACCESS" in mcp_vars,
                "tool_registry": "MCP_TOOL_REGISTRY" in mcp_vars,
            }

            result["details"]["tool_checks"] = tool_checks

            # Check tool registry contents
            if tool_checks["tool_registry"]:
                try:
                    registry = json.loads(os.environ["MCP_TOOL_REGISTRY"])
                    tool_names = list(registry.get("tools", {}).keys())
                    result["details"]["registered_tools"] = tool_names
                except Exception:
                    result["issues"].append("Tool registry JSON is invalid")

            # Check for MCP server commands
            server_commands = [k for k in mcp_vars if k.endswith("_COMMAND")]
            result["details"]["server_commands"] = server_commands

            # Determine issues
            if (
                not tool_checks["config_propagation"]
                and os.environ.get("MCP_SUB_AGENT_ACCESS") == "true"
            ):
                result["issues"].append("Sub-agent detected but config propagation not enabled")

            if not server_commands and not tool_checks["tool_registry"]:
                result["issues"].append("No MCP servers or tools configured")

            result["status"] = "ok" if not result["issues"] else "issues_found"

        except Exception as e:
            result["status"] = "error"
            result["issues"].append(f"Error during tool diagnosis: {str(e)}")

        return result

    async def _diagnose_configuration(self) -> Dict[str, Any]:
        """Diagnose MCP configuration issues."""
        result = {"status": "checking", "details": {}, "issues": []}

        try:
            # Find .mcp.json
            config_locations = [
                Path.cwd() / ".mcp.json",
                Path.home() / ".mcp.json",
                PathUtils.get_workspace_root() / ".mcp.json",
            ]

            config_path = None
            for path in config_locations:
                if path.exists():
                    config_path = path
                    break

            result["details"]["config_path"] = str(config_path) if config_path else None

            if config_path:
                # Validate configuration
                try:
                    with open(config_path) as f:
                        config = json.load(f)

                    servers = config.get("mcpServers", {})
                    result["details"]["server_count"] = len(servers)
                    result["details"]["servers"] = list(servers.keys())

                    # Check for code-index-mcp
                    if "code-index-mcp" in servers:
                        server_config = servers["code-index-mcp"]
                        result["details"]["code_index_config"] = {
                            "has_command": "command" in server_config,
                            "has_args": "args" in server_config,
                            "has_env": "env" in server_config,
                        }

                        # Check for inheritance settings
                        if not server_config.get("inherit_env"):
                            result["issues"].append("Server config missing 'inherit_env: true'")
                        if not server_config.get("sub_agent_access"):
                            result["issues"].append(
                                "Server config missing 'sub_agent_access: true'"
                            )
                    else:
                        result["issues"].append("code-index-mcp server not configured")

                except json.JSONDecodeError:
                    result["issues"].append("Invalid JSON in configuration file")
                except Exception as e:
                    result["issues"].append(f"Error reading config: {str(e)}")
            else:
                result["issues"].append("No .mcp.json configuration file found")

            result["status"] = "ok" if not result["issues"] else "issues_found"

        except Exception as e:
            result["status"] = "error"
            result["issues"].append(f"Error during configuration diagnosis: {str(e)}")

        return result

    async def _diagnose_index_access(self) -> Dict[str, Any]:
        """Diagnose index access issues."""
        result = {"status": "checking", "details": {}, "issues": []}

        try:
            from mcp_server.config.index_paths import IndexPathConfig
            from mcp_server.utils.index_discovery import IndexDiscovery

            # Check index discovery
            discovery = IndexDiscovery(Path.cwd())
            index_info = discovery.get_index_info()

            result["details"]["index_enabled"] = index_info["enabled"]
            result["details"]["index_found"] = index_info["has_local_index"]
            result["details"]["index_path"] = index_info.get("found_at")

            if not index_info["enabled"]:
                result["issues"].append(
                    "MCP indexing not enabled (.mcp-index.json missing or disabled)"
                )

            if index_info["enabled"] and not index_info["has_local_index"]:
                # Get search paths for debugging
                path_config = IndexPathConfig()
                repo_id = discovery._get_repository_identifier()
                search_paths = path_config.get_search_paths(repo_id)

                result["details"]["searched_paths"] = [str(p) for p in search_paths[:5]]
                result["issues"].append(f"No index found after searching {len(search_paths)} paths")

            # Check index validity if found
            if index_info["has_local_index"] and index_info["found_at"]:
                try:
                    import sqlite3

                    conn = sqlite3.connect(index_info["found_at"])
                    cursor = conn.execute("SELECT COUNT(*) FROM files")
                    file_count = cursor.fetchone()[0]
                    result["details"]["file_count"] = file_count

                    if file_count == 0:
                        result["issues"].append("Index exists but is empty")

                    conn.close()
                except Exception as e:
                    result["issues"].append(f"Index validation failed: {str(e)}")

            result["status"] = "ok" if not result["issues"] else "issues_found"

        except Exception as e:
            result["status"] = "error"
            result["issues"].append(f"Error during index diagnosis: {str(e)}")

        return result

    async def _diagnose_sub_agent_setup(self) -> Dict[str, Any]:
        """Diagnose sub-agent specific issues."""
        result = {"status": "checking", "details": {}, "issues": []}

        try:
            is_sub_agent = os.environ.get("MCP_SUB_AGENT_ACCESS") == "true"
            result["details"]["is_sub_agent"] = is_sub_agent

            if is_sub_agent:
                # Check inheritance setup
                checks = {
                    "config_inherited": os.environ.get("MCP_INHERIT_CONFIG") == "true",
                    "tools_propagated": os.environ.get("MCP_PROPAGATE_TOOLS") == "true",
                    "has_tool_registry": "MCP_TOOL_REGISTRY" in os.environ,
                    "has_config_path": "MCP_CONFIG_PATH" in os.environ,
                }

                result["details"]["inheritance_checks"] = checks

                # Check for issues
                if not checks["config_inherited"]:
                    result["issues"].append("Config inheritance not enabled in sub-agent")
                if not checks["tools_propagated"]:
                    result["issues"].append("Tool propagation not enabled in sub-agent")
                if not checks["has_tool_registry"]:
                    result["issues"].append("No tool registry found in sub-agent environment")

                # Check parent agent setup
                if not any(checks.values()):
                    result["issues"].append("Parent agent did not propagate MCP configuration")

            result["status"] = "ok" if not result["issues"] else "issues_found"

        except Exception as e:
            result["status"] = "error"
            result["issues"].append(f"Error during sub-agent diagnosis: {str(e)}")

        return result

    async def _diagnose_environment(self) -> Dict[str, Any]:
        """Diagnose environment setup issues."""
        result = {"status": "checking", "details": {}, "issues": []}

        try:
            # Check Python version
            import sys

            py_version = f"{sys.version_info.major}.{sys.version_info.minor}"
            result["details"]["python_version"] = py_version

            # Check environment type
            env_type = {
                "docker": os.path.exists("/.dockerenv"),
                "github_actions": bool(os.environ.get("GITHUB_ACTIONS")),
                "ci": bool(os.environ.get("CI")),
                "pytest": bool(os.environ.get("PYTEST_CURRENT_TEST")),
            }

            result["details"]["environment_type"] = env_type

            # Check path configuration
            custom_paths = os.environ.get("MCP_INDEX_PATHS")
            result["details"]["custom_index_paths"] = bool(custom_paths)

            # Docker-specific checks
            if env_type["docker"]:
                workspace_path = Path("/workspaces")
                if workspace_path.exists():
                    projects = list(workspace_path.iterdir())
                    result["details"]["docker_projects"] = [p.name for p in projects if p.is_dir()]
                else:
                    result["issues"].append("Docker environment but /workspaces not found")

            result["status"] = "ok" if not result["issues"] else "issues_found"

        except Exception as e:
            result["status"] = "error"
            result["issues"].append(f"Error during environment diagnosis: {str(e)}")

        return result

    def _generate_fixes(self, diagnostics: Dict[str, Any]) -> List[Dict[str, str]]:
        """Generate suggested fixes based on diagnostics."""
        fixes = []

        # Tool availability fixes
        tool_diag = diagnostics.get("tool_availability", {})
        if "Sub-agent detected but config propagation not enabled" in tool_diag.get("issues", []):
            fixes.append(
                {
                    "issue": "MCP tools not available in sub-agent",
                    "fix": "Set environment variables before spawning sub-agent",
                    "commands": [
                        "export MCP_INHERIT_CONFIG=true",
                        "export MCP_PROPAGATE_TOOLS=true",
                        "export MCP_SUB_AGENT_ACCESS=true",
                    ],
                }
            )

        # Configuration fixes
        config_diag = diagnostics.get("configuration", {})
        if "No .mcp.json configuration file found" in config_diag.get("issues", []):
            fixes.append(
                {
                    "issue": "Missing MCP configuration",
                    "fix": "Create .mcp.json configuration file",
                    "commands": ["Create .mcp.json with code-index-mcp server configuration"],
                }
            )

        if "Server config missing 'inherit_env: true'" in config_diag.get("issues", []):
            fixes.append(
                {
                    "issue": "Sub-agent inheritance not configured",
                    "fix": "Update .mcp.json to enable inheritance",
                    "commands": ["Add 'inherit_env: true' to code-index-mcp server config"],
                }
            )

        # Index access fixes
        index_diag = diagnostics.get("index_access", {})
        if "MCP indexing not enabled" in index_diag.get("issues", []):
            fixes.append(
                {
                    "issue": "Indexing not enabled",
                    "fix": "Create .mcp-index.json to enable indexing",
                    "commands": ["echo '{\"enabled\": true}' > .mcp-index.json"],
                }
            )

        if "No index found" in str(index_diag.get("issues", [])):
            fixes.append(
                {
                    "issue": "No index available",
                    "fix": "Create an index for the repository",
                    "commands": [
                        "mcp-index index",
                        "# or",
                        "claude-index create --repo . --path .",
                    ],
                }
            )

        return fixes

    def print_report(self, results: Optional[Dict[str, Any]] = None):
        """Print a human-readable diagnostic report."""
        if results is None:
            results = self.diagnostics_data

        if not results:
            print("No diagnostic results available")
            return

        print("\n" + "=" * 60)
        print("MCP DIAGNOSTICS REPORT")
        print("=" * 60)
        print(f"\nTimestamp: {results['timestamp']}")

        # Summary
        total_issues = len(results["issues_found"])
        print(f"\nTotal Issues Found: {total_issues}")

        # Detailed diagnostics
        print("\nDiagnostic Results:")
        print("-" * 40)

        for diag_name, diag_result in results["diagnostics"].items():
            status = diag_result["status"]
            status_symbol = "✅" if status == "ok" else "⚠️" if status == "issues_found" else "❌"

            print(f"\n{status_symbol} {diag_name.replace('_', ' ').title()}")

            # Show relevant details
            if diag_result.get("details"):
                for key, value in diag_result["details"].items():
                    if value is not None and str(value):
                        print(f"   {key}: {value}")

            # Show issues
            if diag_result.get("issues"):
                print("   Issues:")
                for issue in diag_result["issues"]:
                    print(f"   - {issue}")

        # Suggested fixes
        if results["suggested_fixes"]:
            print("\nSuggested Fixes:")
            print("-" * 40)

            for i, fix in enumerate(results["suggested_fixes"], 1):
                print(f"\n{i}. Issue: {fix['issue']}")
                print(f"   Fix: {fix['fix']}")
                if fix.get("commands"):
                    print("   Commands:")
                    for cmd in fix["commands"]:
                        print(f"     $ {cmd}")

        print("\n" + "=" * 60 + "\n")


async def quick_mcp_check() -> bool:
    """
    Quick check if MCP is properly configured and available.

    Returns:
        True if MCP is available, False otherwise
    """
    try:
        # Check for sub-agent with proper setup
        is_sub_agent = os.environ.get("MCP_SUB_AGENT_ACCESS") == "true"
        if is_sub_agent:
            return os.environ.get("MCP_INHERIT_CONFIG") == "true"

        # Check for tool registry or server config
        has_registry = bool(os.environ.get("MCP_TOOL_REGISTRY"))
        has_servers = any(
            k.startswith("MCP_SERVER_") and k.endswith("_COMMAND") for k in os.environ
        )

        return has_registry or has_servers

    except Exception:
        return False
