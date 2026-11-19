"""Direct searcher utility for grep/ripgrep operations with performance measurement."""

import logging
import shlex
import subprocess
import time
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


class DirectSearcher:
    """Unified interface for grep and ripgrep search operations."""

    def __init__(self):
        """Initialize the DirectSearcher."""
        self.grep_cmd = "grep"
        self.ripgrep_cmd = "rg"
        self._check_commands()

    def _check_commands(self):
        """Check if grep and ripgrep are available."""
        for cmd, attr in [(self.grep_cmd, "has_grep"), (self.ripgrep_cmd, "has_ripgrep")]:
            try:
                subprocess.run([cmd, "--version"], capture_output=True, check=True)
                setattr(self, attr, True)
            except subprocess.CalledProcessError, FileNotFoundError:
                setattr(self, attr, False)
                logger.warning(f"{cmd} not found in PATH")

    def search_pattern(
        self, pattern: str, path: str = ".", use_ripgrep: bool = True
    ) -> Dict[str, Any]:
        """
        Search for a regex pattern in files.

        Args:
            pattern: Regex pattern to search for
            path: Path to search in (default: current directory)
            use_ripgrep: Whether to use ripgrep (True) or grep (False)

        Returns:
            Dict containing results, errors, and timing information
        """
        if use_ripgrep and self.has_ripgrep:
            return self._run_ripgrep_pattern(pattern, path)
        elif self.has_grep:
            return self._run_grep_pattern(pattern, path)
        else:
            return self._error_result("No search command available")

    def search_string(
        self, string: str, path: str = ".", use_ripgrep: bool = True
    ) -> Dict[str, Any]:
        """
        Search for a literal string in files.

        Args:
            string: Literal string to search for
            path: Path to search in (default: current directory)
            use_ripgrep: Whether to use ripgrep (True) or grep (False)

        Returns:
            Dict containing results, errors, and timing information
        """
        if use_ripgrep and self.has_ripgrep:
            return self._run_ripgrep_string(string, path)
        elif self.has_grep:
            return self._run_grep_string(string, path)
        else:
            return self._error_result("No search command available")

    def measure_performance(self, search_func: Callable, *args, **kwargs) -> Dict[str, Any]:
        """
        Measure the performance of a search function.

        Args:
            search_func: Function to measure
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            Dict containing results and detailed timing information
        """
        start_time = time.time()
        result = search_func(*args, **kwargs)
        end_time = time.time()

        result["performance"] = {
            "total_time": end_time - start_time,
            "start_time": start_time,
            "end_time": end_time,
        }

        return result

    def _run_ripgrep_pattern(self, pattern: str, path: str) -> Dict[str, Any]:
        """Run ripgrep with regex pattern."""
        cmd = [self.ripgrep_cmd, "-n", "--no-heading", pattern, path]
        return self._execute_command(cmd, "ripgrep")

    def _run_ripgrep_string(self, string: str, path: str) -> Dict[str, Any]:
        """Run ripgrep with literal string."""
        cmd = [self.ripgrep_cmd, "-n", "--no-heading", "-F", string, path]
        return self._execute_command(cmd, "ripgrep")

    def _run_grep_pattern(self, pattern: str, path: str) -> Dict[str, Any]:
        """Run grep with regex pattern."""
        cmd = [self.grep_cmd, "-r", "-n", "-E", pattern, path]
        return self._execute_command(cmd, "grep")

    def _run_grep_string(self, string: str, path: str) -> Dict[str, Any]:
        """Run grep with literal string."""
        # Escape special characters for grep
        _ = shlex.quote(string)
        cmd = [self.grep_cmd, "-r", "-n", "-F", string, path]
        return self._execute_command(cmd, "grep")

    def _execute_command(self, cmd: List[str], tool_name: str) -> Dict[str, Any]:
        """Execute a command and return formatted results."""
        start_time = time.time()

        try:
            process = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30  # 30 second timeout
            )

            elapsed_time = time.time() - start_time

            # Parse results
            results = []
            if process.returncode == 0 and process.stdout:
                for line in process.stdout.strip().split("\n"):
                    if line:
                        results.append(self._parse_result_line(line, tool_name))

            return {
                "success": process.returncode in [0, 1],  # 1 means no matches found
                "tool": tool_name,
                "command": " ".join(cmd),
                "results": results,
                "match_count": len(results),
                "elapsed_time": elapsed_time,
                "errors": process.stderr if process.stderr else None,
                "returncode": process.returncode,
            }

        except subprocess.TimeoutExpired:
            elapsed_time = time.time() - start_time
            return self._error_result(
                "Command timed out after 30 seconds", tool_name, cmd, elapsed_time
            )
        except Exception as e:
            elapsed_time = time.time() - start_time
            return self._error_result(str(e), tool_name, cmd, elapsed_time)

    def _parse_result_line(self, line: str, tool_name: str) -> Dict[str, Any]:
        """Parse a result line from grep/ripgrep output."""
        # Both tools output in format: filename:line_number:content
        parts = line.split(":", 2)

        if len(parts) >= 3:
            return {
                "file": parts[0],
                "line_number": int(parts[1]) if parts[1].isdigit() else -1,
                "content": parts[2],
                "raw": line,
            }
        else:
            return {"file": "", "line_number": -1, "content": line, "raw": line}

    def _error_result(
        self,
        error_msg: str,
        tool_name: str = None,
        cmd: List[str] = None,
        elapsed_time: float = 0.0,
    ) -> Dict[str, Any]:
        """Create an error result dict."""
        return {
            "success": False,
            "tool": tool_name or "unknown",
            "command": " ".join(cmd) if cmd else "",
            "results": [],
            "match_count": 0,
            "elapsed_time": elapsed_time,
            "errors": error_msg,
            "returncode": -1,
        }

    def compare_tools(
        self, pattern: str, path: str = ".", is_literal: bool = False
    ) -> Dict[str, Any]:
        """
        Compare grep and ripgrep performance on the same search.

        Args:
            pattern: Pattern or string to search for
            path: Path to search in
            is_literal: Whether to treat pattern as literal string

        Returns:
            Dict containing comparison results
        """
        results = {}

        # Choose search method
        search_method = self.search_string if is_literal else self.search_pattern

        # Run ripgrep if available
        if self.has_ripgrep:
            results["ripgrep"] = self.measure_performance(
                search_method, pattern, path, use_ripgrep=True
            )

        # Run grep if available
        if self.has_grep:
            results["grep"] = self.measure_performance(
                search_method, pattern, path, use_ripgrep=False
            )

        # Calculate comparison metrics
        if len(results) == 2:
            rg_time = results["ripgrep"]["elapsed_time"]
            grep_time = results["grep"]["elapsed_time"]
            results["comparison"] = {
                "speedup": grep_time / rg_time if rg_time > 0 else float("inf"),
                "time_difference": grep_time - rg_time,
                "ripgrep_matches": results["ripgrep"]["match_count"],
                "grep_matches": results["grep"]["match_count"],
                "matches_equal": results["ripgrep"]["match_count"]
                == results["grep"]["match_count"],
            }

        return results


# Convenience functions
def quick_search(pattern: str, path: str = ".", use_ripgrep: bool = True) -> List[Dict[str, Any]]:
    """Quick search function that returns just the results."""
    searcher = DirectSearcher()
    result = searcher.search_pattern(pattern, path, use_ripgrep)
    return result["results"] if result["success"] else []


def compare_search_tools(pattern: str, path: str = ".") -> None:
    """Compare grep and ripgrep and print results."""
    searcher = DirectSearcher()
    comparison = searcher.compare_tools(pattern, path)

    print(f"\nSearch comparison for pattern: '{pattern}'")
    print(f"Path: {path}")
    print("-" * 60)

    for tool, result in comparison.items():
        if tool == "comparison":
            print("\nComparison Summary:")
            print(f"  Speedup: {result['speedup']:.2f}x")
            print(f"  Time difference: {result['time_difference']:.4f}s")
            print(f"  Matches equal: {result['matches_equal']}")
        else:
            print(f"\n{tool.upper()}:")
            print(f"  Matches: {result['match_count']}")
            print(f"  Time: {result['elapsed_time']:.4f}s")
            print(f"  Success: {result['success']}")
            if result["errors"]:
                print(f"  Errors: {result['errors']}")
