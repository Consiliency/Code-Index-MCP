"""
Pre-Flight Validation System for MCP

This module provides validation to ensure MCP tools and configuration are
properly available before attempting operations, preventing runtime failures.
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp_server.core.path_utils import PathUtils
from mcp_server.utils.subprocess_env import get_full_env

logger = logging.getLogger(__name__)


class PreFlightValidator:
    """Validates MCP configuration and availability before operations."""

    def __init__(self):
        """Initialize the pre-flight validator."""
        self.validation_results: Dict[str, Any] = {}
        self.start_time = datetime.now()

    async def validate_all(self) -> Dict[str, Any]:
        """
        Run all validation checks.

        Returns:
            Dictionary with validation results and recommendations
        """
        logger.info("Starting pre-flight validation...")

        results = {
            "timestamp": datetime.now().isoformat(),
            "duration_ms": 0,
            "overall_status": "pending",
            "checks": {},
            "recommendations": [],
            "can_proceed": False,
        }

        # Run all validation checks
        checks = [
            ("mcp_tools", self._validate_mcp_tools()),
            ("index_availability", self._validate_index_availability()),
            ("configuration", self._validate_configuration()),
            ("environment", self._validate_environment()),
            ("dependencies", self._validate_dependencies()),
            ("permissions", self._validate_permissions()),
        ]

        # Execute checks concurrently
        check_results = await asyncio.gather(*[check[1] for check in checks])

        # Compile results
        for (name, _), result in zip(checks, check_results):
            results["checks"][name] = result

        # Determine overall status
        all_passed = all(check.get("status") == "passed" for check in results["checks"].values())
        critical_passed = all(
            check.get("status") != "failed"
            for name, check in results["checks"].items()
            if check.get("critical", False)
        )

        results["overall_status"] = (
            "passed" if all_passed else ("warning" if critical_passed else "failed")
        )
        results["can_proceed"] = critical_passed

        # Generate recommendations
        results["recommendations"] = self._generate_recommendations(results["checks"])

        # Calculate duration
        duration = (datetime.now() - self.start_time).total_seconds() * 1000
        results["duration_ms"] = round(duration, 2)

        self.validation_results = results
        logger.info(f"Pre-flight validation completed: {results['overall_status']}")

        return results

    async def _validate_mcp_tools(self) -> Dict[str, Any]:
        """Validate MCP tool availability."""
        result = {"status": "pending", "critical": True, "details": {}, "issues": []}

        try:
            # Check if we're in a sub-agent context
            is_sub_agent = os.environ.get("MCP_SUB_AGENT_ACCESS") == "true"

            # Check for MCP tool functions
            mcp_tools = []
            for key in os.environ:
                if key.startswith("MCP_") and key.endswith("_COMMAND"):
                    mcp_tools.append(key)

            result["details"]["is_sub_agent"] = is_sub_agent
            result["details"]["tool_count"] = len(mcp_tools)
            result["details"]["inheritance_enabled"] = (
                os.environ.get("MCP_INHERIT_CONFIG") == "true"
            )

            # Check tool registry
            tool_registry = os.environ.get("MCP_TOOL_REGISTRY")
            if tool_registry:
                try:
                    registry_data = json.loads(tool_registry)
                    result["details"]["registry_tool_count"] = len(registry_data.get("tools", {}))
                except Exception:
                    result["issues"].append("Tool registry JSON is invalid")

            # Validate common MCP tools
            _ = [
                "mcp__code-index-mcp__symbol_lookup",
                "mcp__code-index-mcp__search_code",
                "mcp__code-index-mcp__get_status",
            ]

            # In a real implementation, we'd check if these are callable
            # For now, we'll check environment setup
            if is_sub_agent and not result["details"].get("inheritance_enabled"):
                result["issues"].append("Sub-agent detected but inheritance not enabled")

            if len(mcp_tools) == 0 and not tool_registry:
                result["issues"].append("No MCP tools found in environment")
                result["status"] = "failed"
            else:
                result["status"] = "passed"

        except Exception as e:
            result["status"] = "error"
            result["issues"].append(f"Error validating MCP tools: {str(e)}")

        return result

    async def _validate_index_availability(self) -> Dict[str, Any]:
        """Validate that required indexes are available."""
        result = {"status": "pending", "critical": True, "details": {}, "issues": []}

        try:
            from mcp_server.config.settings import get_settings
            from mcp_server.utils.index_discovery import IndexDiscovery

            # Check current directory
            workspace = Path.cwd()
            discovery = IndexDiscovery(workspace)
            settings = get_settings()

            compatible_index = discovery.get_local_index_path(
                requested_schema_version=settings.index_schema_version,
                requested_embedding_model=settings.semantic_embedding_model,
                strict_compatibility=settings.strict_index_compatibility,
            )

            # Get index information
            index_info = discovery.get_index_info()
            result["details"]["index_enabled"] = index_info["enabled"]
            result["details"]["has_index"] = compatible_index is not None
            result["details"]["found_at"] = (
                str(compatible_index) if compatible_index else index_info.get("found_at")
            )

            if not index_info["enabled"]:
                result["issues"].append("MCP indexing not enabled for this repository")
                result["status"] = "failed"
            elif compatible_index is None:
                result["issues"].append("No index found in any configured location")
                result["details"]["search_paths"] = index_info.get("search_paths", [])
                result["status"] = "failed"
            else:
                # Validate the index
                index_path = Path(compatible_index)
                if index_path.exists():
                    try:
                        conn = sqlite3.connect(str(index_path))
                        cursor = conn.execute("SELECT COUNT(*) FROM files")
                        file_count = cursor.fetchone()[0]
                        result["details"]["file_count"] = file_count

                        if file_count == 0:
                            result["issues"].append("Index exists but contains no files")
                            result["status"] = "warning"
                        else:
                            result["status"] = "passed"

                        conn.close()
                    except Exception as e:
                        result["issues"].append(f"Index validation error: {str(e)}")
                        result["status"] = "failed"
                else:
                    result["issues"].append("Index path exists in config but file not found")
                    result["status"] = "failed"

        except Exception as e:
            result["status"] = "error"
            result["issues"].append(f"Error checking index availability: {str(e)}")

        return result

    async def _validate_configuration(self) -> Dict[str, Any]:
        """Validate MCP configuration files."""
        result = {"status": "pending", "critical": False, "details": {}, "issues": []}

        try:
            # Check for .mcp.json
            mcp_json_paths = [
                Path.cwd() / ".mcp.json",
                Path.home() / ".mcp.json",
                PathUtils.get_workspace_root() / ".mcp.json",
            ]

            config_found = False
            for config_path in mcp_json_paths:
                if config_path.exists():
                    config_found = True
                    result["details"]["config_path"] = str(config_path)

                    try:
                        with open(config_path) as f:
                            config = json.load(f)
                            result["details"]["servers"] = list(config.get("mcpServers", {}).keys())

                            # Check for code-index-mcp server
                            if "code-index-mcp" not in config.get("mcpServers", {}):
                                result["issues"].append("code-index-mcp server not configured")

                    except json.JSONDecodeError:
                        result["issues"].append(f"Invalid JSON in {config_path}")

                    break

            if not config_found:
                result["issues"].append("No .mcp.json configuration found")
                result["status"] = "warning"
            else:
                result["status"] = "passed" if not result["issues"] else "warning"

            # Check for .mcp-index.json
            index_config = Path.cwd() / ".mcp-index.json"
            if index_config.exists():
                result["details"]["has_index_config"] = True
                try:
                    with open(index_config) as f:
                        idx_config = json.load(f)
                        result["details"]["index_enabled"] = idx_config.get("enabled", True)
                except Exception:
                    result["issues"].append("Invalid .mcp-index.json")
            else:
                result["details"]["has_index_config"] = False

        except Exception as e:
            result["status"] = "error"
            result["issues"].append(f"Error validating configuration: {str(e)}")

        return result

    async def _validate_environment(self) -> Dict[str, Any]:
        """Validate environment setup."""
        result = {"status": "pending", "critical": False, "details": {}, "issues": []}

        try:
            # Check critical environment variables
            env_vars = {
                "MCP_INHERIT_CONFIG": os.environ.get("MCP_INHERIT_CONFIG"),
                "MCP_PROPAGATE_TOOLS": os.environ.get("MCP_PROPAGATE_TOOLS"),
                "MCP_SUB_AGENT_ACCESS": os.environ.get("MCP_SUB_AGENT_ACCESS"),
                "MCP_ENABLE_MULTI_PATH": os.environ.get("MCP_ENABLE_MULTI_PATH", "true"),
                "MCP_INDEX_PATHS": os.environ.get("MCP_INDEX_PATHS"),
            }

            result["details"]["environment"] = env_vars

            # Detect environment type
            is_docker = os.path.exists("/.dockerenv") or os.environ.get("DOCKER_CONTAINER")
            is_ci = os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS")
            is_test = os.environ.get("PYTEST_CURRENT_TEST") is not None

            result["details"]["environment_type"] = {
                "docker": is_docker,
                "ci": is_ci,
                "test": is_test,
            }

            # Check Python version
            import sys

            python_version = (
                f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            )
            result["details"]["python_version"] = python_version
            # requires-python (>=3.14) is enforced at install time.
            result["status"] = "passed"

        except Exception as e:
            result["status"] = "error"
            result["issues"].append(f"Error validating environment: {str(e)}")

        return result

    async def _validate_dependencies(self) -> Dict[str, Any]:
        """Validate required dependencies."""
        result = {"status": "pending", "critical": False, "details": {}, "issues": []}

        try:
            # Check for required commands
            commands = {
                "git": ["git", "--version"],
                "sqlite3": ["sqlite3", "--version"],
            }

            for cmd_name, cmd_args in commands.items():
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd_args,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=get_full_env(),
                    )
                    await proc.wait()
                    result["details"][f"{cmd_name}_available"] = proc.returncode == 0

                    if proc.returncode != 0:
                        result["issues"].append(f"{cmd_name} command not available")
                except FileNotFoundError:
                    result["details"][f"{cmd_name}_available"] = False
                    result["issues"].append(f"{cmd_name} command not found")

            # Check Python packages
            required_packages = ["mcp", "sqlite3", "pathlib"]
            missing_packages = []

            for package in required_packages:
                try:
                    __import__(package)
                    result["details"][f"{package}_available"] = True
                except ImportError:
                    result["details"][f"{package}_available"] = False
                    missing_packages.append(package)

            if missing_packages:
                result["issues"].append(f"Missing Python packages: {', '.join(missing_packages)}")

            result["status"] = "passed" if not result["issues"] else "warning"

        except Exception as e:
            result["status"] = "error"
            result["issues"].append(f"Error validating dependencies: {str(e)}")

        return result

    async def _validate_permissions(self) -> Dict[str, Any]:
        """Validate file system permissions."""
        result = {"status": "pending", "critical": False, "details": {}, "issues": []}

        try:
            # Check write permissions in key directories
            test_dirs = [
                Path.cwd(),
                Path.cwd() / ".mcp-index",
                Path.home() / ".mcp",
            ]

            for test_dir in test_dirs:
                if test_dir.exists():
                    # Try to create a test file
                    test_file = test_dir / f".mcp_test_{os.getpid()}"
                    try:
                        test_file.touch()
                        test_file.unlink()
                        result["details"][f"writable_{test_dir.name}"] = True
                    except Exception:
                        result["details"][f"writable_{test_dir.name}"] = False
                        if test_dir == Path.cwd():
                            result["issues"].append(f"No write permission in {test_dir}")

            result["status"] = "passed" if not result["issues"] else "warning"

        except Exception as e:
            result["status"] = "error"
            result["issues"].append(f"Error validating permissions: {str(e)}")

        return result

    def _generate_recommendations(self, checks: Dict[str, Any]) -> List[str]:
        """Generate recommendations based on validation results."""
        recommendations = []

        # MCP tools recommendations
        mcp_check = checks.get("mcp_tools", {})
        if mcp_check.get("status") == "failed":
            if mcp_check.get("details", {}).get("is_sub_agent"):
                recommendations.append(
                    "Enable MCP tool inheritance: export MCP_INHERIT_CONFIG=true"
                )
            else:
                recommendations.append("Configure MCP tools in .mcp.json")

        # Index recommendations
        index_check = checks.get("index_availability", {})
        if index_check.get("status") == "failed":
            if not index_check.get("details", {}).get("index_enabled"):
                recommendations.append(
                    'Enable indexing: Create .mcp-index.json with {"enabled": true}'
                )
            else:
                recommendations.append(
                    "Create index: Run 'mcp-index index' or 'claude-index create'"
                )

        # Configuration recommendations
        config_check = checks.get("configuration", {})
        if config_check.get("status") in ["warning", "failed"]:
            if "No .mcp.json" in str(config_check.get("issues", [])):
                recommendations.append("Create .mcp.json configuration file")

        # Environment recommendations
        env_check = checks.get("environment", {})
        if env_check.get("details", {}).get("environment_type", {}).get("docker"):
            recommendations.append("Consider using Docker-optimized index paths")

        # Dependencies recommendations
        deps_check = checks.get("dependencies", {})
        if deps_check.get("issues"):
            for issue in deps_check["issues"]:
                if "git" in issue:
                    recommendations.append("Install git: Required for repository identification")
                elif "Missing Python packages" in issue:
                    recommendations.append("Install missing packages: pip install mcp")

        return recommendations

    def print_summary(self, results: Optional[Dict[str, Any]] = None):
        """Print a human-readable summary of validation results."""
        if results is None:
            results = self.validation_results

        if not results:
            print("No validation results available")
            return

        # Header
        print("\n" + "=" * 60)
        print("MCP PRE-FLIGHT VALIDATION SUMMARY")
        print("=" * 60)

        # Overall status
        status = results["overall_status"]
        status_symbol = "✅" if status == "passed" else "⚠️" if status == "warning" else "❌"
        print(f"\nOverall Status: {status_symbol} {status.upper()}")
        print(f"Can Proceed: {'Yes' if results['can_proceed'] else 'No'}")
        print(f"Duration: {results['duration_ms']}ms")

        # Individual checks
        print("\nValidation Checks:")
        print("-" * 40)

        for check_name, check_result in results["checks"].items():
            status = check_result["status"]
            critical = "CRITICAL" if check_result.get("critical") else ""
            status_symbol = "✅" if status == "passed" else "⚠️" if status == "warning" else "❌"

            print(f"{status_symbol} {check_name.replace('_', ' ').title():<25} {critical}")

            if check_result.get("issues"):
                for issue in check_result["issues"]:
                    print(f"   - {issue}")

        # Recommendations
        if results["recommendations"]:
            print("\nRecommendations:")
            print("-" * 40)
            for i, rec in enumerate(results["recommendations"], 1):
                print(f"{i}. {rec}")

        print("\n" + "=" * 60 + "\n")


class MCPHealthCheck:
    """Quick health check for MCP availability."""

    @staticmethod
    async def check_mcp_available() -> Tuple[bool, str]:
        """
        Quick check if MCP tools are available.

        Returns:
            Tuple of (is_available, message)
        """
        try:
            # Check for sub-agent with inheritance
            is_sub_agent = os.environ.get("MCP_SUB_AGENT_ACCESS") == "true"
            inheritance_enabled = os.environ.get("MCP_INHERIT_CONFIG") == "true"

            if is_sub_agent and not inheritance_enabled:
                return False, "Sub-agent detected but MCP inheritance not enabled"

            # Check for tool registry
            tool_registry = os.environ.get("MCP_TOOL_REGISTRY")
            if tool_registry:
                try:
                    data = json.loads(tool_registry)
                    tool_count = len(data.get("tools", {}))
                    return True, f"MCP tools available ({tool_count} tools in registry)"
                except Exception:
                    return False, "MCP tool registry is corrupted"

            # Check for MCP server configs
            mcp_servers = sum(
                1 for k in os.environ if k.startswith("MCP_SERVER_") and k.endswith("_COMMAND")
            )
            if mcp_servers > 0:
                return True, f"MCP servers configured ({mcp_servers} servers)"

            return False, "No MCP configuration found"

        except Exception as e:
            return False, f"Health check error: {str(e)}"

    @staticmethod
    async def check_index_available() -> Tuple[bool, str]:
        """
        Quick check if index is available.

        Returns:
            Tuple of (is_available, message)
        """
        try:
            from mcp_server.utils.index_discovery import IndexDiscovery

            discovery = IndexDiscovery(Path.cwd())
            index_path = discovery.get_local_index_path()

            if index_path and index_path.exists():
                return True, f"Index available at {index_path}"
            else:
                return False, "No index found"

        except Exception as e:
            return False, f"Index check error: {str(e)}"
