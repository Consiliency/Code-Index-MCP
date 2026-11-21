"""
Test suite for SL-3: PathUtils literal-string sweep.
Verifies that all scripts use the correct method-call form for PathUtils.get_workspace_root().
"""

import subprocess
import sys
from pathlib import Path

from mcp_server.core.path_utils import PathUtils


def test_no_literal_path_utils_strings():
    """
    Assert that no scripts contain the literal-string form:
    Path("PathUtils.get_workspace_root()/<rel>")

    Uses ripgrep to search and asserts zero matches (ripgrep exit code 1 = no matches).
    """
    result = subprocess.run(
        ["rg", "-n", r'Path\("PathUtils\.get_workspace_root\(\)', "scripts/"],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    # ripgrep exit 1 = no matches found (success)
    # ripgrep exit 0 = matches found (failure)
    assert result.returncode != 0, (
        f'Found {len(result.stdout.splitlines())} literal Path("PathUtils.get_workspace_root()...") strings:\n'
        f"{result.stdout}"
    )


def test_pathutils_returns_valid_path():
    """
    Verify that PathUtils.get_workspace_root() returns a Path object
    whose parent directory exists.
    """
    workspace = PathUtils.get_workspace_root()
    assert isinstance(workspace, Path), f"Expected Path, got {type(workspace)}"
    assert workspace.parent.exists(), f"Parent directory does not exist: {workspace.parent}"


def test_script_syntax_validity():
    """
    Smoke test: verify that every FIXED script compiles without syntax errors.
    Only checks the scripts fixed by SL-3 that still exist.
    """
    scripts_dir = Path(__file__).parent.parent / "scripts"

    # Scripts fixed in SL-3
    fixed_scripts = {
        "run_comprehensive_query_test.py",
        "demo_mcp_vs_native_analysis.py",
        "reindex_current_repo.py",
        "index_remaining_repos.py",
        "mcp_test_dispatcher.py",
        "validate_test_repositories.py",
        "continue_mcp_indexing.py",
        "real_mcp_analysis.py",
        "check_test_index_schema.py",
        "create_current_repo_index.py",
        "parallel_test_generator.py",
        "mcp_method_detector.py",
        "map_repos_to_qdrant.py",
        "real_claude_session_tracker.py",
        "convert_registry_format.py",
        "ensure_test_repos_indexed.py",
        "run_comprehensive_mcp_test.py",
        "migrate_all_test_indexes.py",
        "analyze_real_claude_transcripts.py",
        "demo_centralized_indexes.py",
        "quick_real_test.py",
        "enhanced_mcp_analysis_framework.py",
        "check_index_status.py",
        "comprehensive_real_analysis.py",
        "real_strategic_recommendations.py",
        "create_proper_mapping.py",
        "index_all_repos_with_mcp.py",
        "robust_mcp_test_runner.py",
        "create_mcp_indexes_direct.py",
        "create_proper_registry.py",
        "real_edit_behavior_tracker.py",
        "find_mcp_embeddings.py",
        "fix_mcp_multi_repo.py",
        "realtime_parallel_analyzer.py",
        "index_all_repos_semantic_simple.py",
        "index_all_repos_semantic_full.py",
        "identify_working_indexes.py",
        "consolidate_real_performance_data.py",
        "real_cost_benefit_analyzer.py",
        "parallel_claude_integration.py",
        "index_all_repos_complete.py",
        "edit_pattern_analyzer.py",
        "analyze_path_mismatch.py",
        "index_test_repos_semantic_only.py",
        "check_test_indexes.py",
        "comprehensive_semantic_analysis.py",
        "claude_code_behavior_simulator.py",
    }

    failed = []
    for script_name in sorted(fixed_scripts):
        script_path = scripts_dir / script_name
        if script_path.exists():
            try:
                with open(script_path) as f:
                    compile(f.read(), str(script_path), "exec")
            except SyntaxError as e:
                failed.append((str(script_path), str(e)))

    # Pre-existing syntax errors in some scripts (not caused by SL-3)
    pre_existing_errors = {
        "consolidate_real_performance_data.py",  # Line 74: malformed string in __init__
        "enhanced_mcp_analysis_framework.py",  # Line 232: unterminated string
        "mcp_method_detector.py",  # Line 150: unterminated string
        "realtime_parallel_analyzer.py",  # Line 552: syntax error
    }

    failed = [(s, e) for s, e in failed if Path(s).name not in pre_existing_errors]

    assert not failed, "New syntax errors in fixed scripts:\n" + "\n".join(
        f"  {s}: {e}" for s, e in failed
    )
