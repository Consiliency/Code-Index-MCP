"""CLI commands for repository management and git integration.

This module provides commands for managing the repository registry,
tracking repositories, and syncing indexes with git.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, cast

import click

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from mcp_server.artifacts.commit_artifacts import CommitArtifactManager  # noqa: E402
from mcp_server.config.settings import reload_settings  # noqa: E402
from mcp_server.core.repo_resolver import RepoResolver  # noqa: E402
from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher  # noqa: E402
from mcp_server.health.repo_status import build_health_row  # noqa: E402
from mcp_server.health.repository_readiness import ReadinessClassifier  # noqa: E402
from mcp_server.setup.semantic_preflight import run_semantic_preflight  # noqa: E402
from mcp_server.storage.git_index_manager import GitAwareIndexManager  # noqa: E402
from mcp_server.storage.repository_registry import (  # noqa: E402
    MultipleWorktreesUnsupportedError,
    RepositoryRegistry,
)
from mcp_server.storage.sqlite_store import SQLiteStore  # noqa: E402,F401
from mcp_server.storage.store_registry import StoreRegistry  # noqa: E402
from mcp_server.watcher_multi_repo import MultiRepositoryWatcher  # noqa: E402


@click.group()
def repository():
    """Repository management commands."""


def _print_rollout_surface(prefix: str, health_row: dict[str, Any]) -> None:
    rollout = health_row["rollout_status"]
    rollout_color = "green" if rollout == "ready" else "yellow"
    click.echo(click.style(f"{prefix}Rollout status: {rollout}", fg=rollout_color))
    if health_row.get("rollout_remediation"):
        click.echo(f"{prefix}Rollout remediation: {health_row['rollout_remediation']}")
    query_status = health_row["query_status"]
    query_color = "green" if query_status == "ready" else "yellow"
    click.echo(click.style(f"{prefix}Query surface: {query_status}", fg=query_color))
    if health_row.get("query_remediation"):
        click.echo(f"{prefix}Query remediation: {health_row['query_remediation']}")


def _print_semantic_evidence(prefix: str, evidence: dict[str, Any]) -> None:
    click.echo(f"{prefix}Summary-backed chunks: {evidence.get('summary_count', 0)}")
    click.echo(f"{prefix}Chunks missing summaries: {evidence.get('missing_summaries', 0)}")
    click.echo(f"{prefix}Vector-linked chunks: {evidence.get('vector_link_count', 0)}")
    click.echo(f"{prefix}Chunks missing vectors: {evidence.get('missing_vectors', 0)}")
    if evidence.get("collection") is not None:
        click.echo(f"{prefix}Active collection: {evidence.get('collection')}")
        click.echo(
            f"{prefix}Collection-matched links: {evidence.get('matching_collection_links', 0)}"
        )
        click.echo(f"{prefix}Collection mismatches: {evidence.get('collection_mismatches', 0)}")


def _print_sync_semantic_details(prefix: str, semantic: Optional[dict[str, Any]]) -> None:
    if not semantic:
        return
    if semantic.get("semantic_stage"):
        click.echo(f"{prefix}Semantic stage: {semantic['semantic_stage']}")
    if semantic.get("summary_passes") is not None:
        click.echo(f"{prefix}Summary passes: {semantic.get('summary_passes', 0)}")
    if semantic.get("summary_remaining_chunks") is not None:
        click.echo(
            f"{prefix}Summary remaining chunks: {semantic.get('summary_remaining_chunks', 0)}"
        )
    if semantic.get("summary_call_timed_out"):
        if semantic.get("summary_call_file_path"):
            click.echo(f"{prefix}Timed-out summary file: {semantic['summary_call_file_path']}")
        if semantic.get("summary_call_chunk_ids"):
            click.echo(
                f"{prefix}Timed-out summary chunks: {', '.join(semantic['summary_call_chunk_ids'])}"
            )
        if semantic.get("summary_call_timeout_seconds") is not None:
            click.echo(
                f"{prefix}Timed-out summary timeout: {semantic['summary_call_timeout_seconds']}"
            )


def _archive_tail_pair(repo_path: Path) -> tuple[Path, Path]:
    return (
        repo_path
        / "analysis_archive"
        / "scripts_archive"
        / "scripts_test_files"
        / "verify_mcp_fix.py",
        repo_path / "analysis_archive" / "semantic_vs_sql_comparison_1750926162.json",
    )


def _optimized_final_report_pair(repo_path: Path) -> tuple[Path, Path]:
    return (
        repo_path / "final_optimized_report_final_report_1750958096" / "final_report_data.json",
        repo_path
        / "final_optimized_report_final_report_1750958096"
        / "FINAL_OPTIMIZED_ANALYSIS_REPORT.md",
    )


def _print_force_full_exit_trace(
    prefix: str, trace: Optional[dict[str, Any]], repo_path: Optional[Path] = None
) -> None:
    if not trace:
        click.echo(f"\n{prefix}Force-full exit trace: missing")
        return
    click.echo("\nForce-full exit trace:")
    if trace.get("status"):
        click.echo(f"{prefix}Trace status: {trace['status']}")
    if trace.get("stage"):
        click.echo(f"{prefix}Trace stage: {trace['stage']}")
    if trace.get("stage_family"):
        click.echo(f"{prefix}Trace stage family: {trace['stage_family']}")
    if trace.get("trace_timestamp"):
        click.echo(f"{prefix}Trace timestamp: {trace['trace_timestamp']}")
    if _force_full_trace_is_stale(trace):
        click.echo(f"{prefix}Trace freshness: stale-running snapshot")
    if trace.get("blocker_source"):
        click.echo(f"{prefix}Trace blocker source: {trace['blocker_source']}")
    if trace.get("storage_failure_family"):
        click.echo(f"{prefix}Trace storage failure family: {trace['storage_failure_family']}")
    if trace.get("storage_failure_reason"):
        click.echo(f"{prefix}Trace storage failure reason: {trace['storage_failure_reason']}")
    if trace.get("storage_failure_message"):
        click.echo(f"{prefix}Trace storage failure message: {trace['storage_failure_message']}")
    if trace.get("current_commit"):
        click.echo(f"{prefix}Trace current commit: {trace['current_commit']}")
    if trace.get("indexed_commit_before") is not None:
        click.echo(f"{prefix}Trace indexed commit before: {trace['indexed_commit_before']}")
    if trace.get("last_progress_path"):
        click.echo(f"{prefix}Last progress path: {trace['last_progress_path']}")
    if trace.get("in_flight_path"):
        click.echo(f"{prefix}In-flight path: {trace['in_flight_path']}")
    if repo_path is not None:
        devcontainer_script, devcontainer_json = _devcontainer_tail_pair(repo_path)
        if trace.get("last_progress_path") == str(devcontainer_script.resolve()) and trace.get(
            "in_flight_path"
        ) == str(devcontainer_json.resolve()):
            click.echo(
                f"{prefix}Devcontainer tail pair: exact bounded shell-to-JSON handoff "
                f"preserved lexical progress into {devcontainer_json.relative_to(repo_path)}"
            )
        archive_script, archive_json = _archive_tail_pair(repo_path)
        if (
            trace.get("last_progress_path") == str(archive_json.resolve())
            and trace.get("in_flight_path") is None
        ):
            click.echo(
                f"{prefix}Archive-tail successor: exact bounded JSON indexing preserved lexical "
                f"progress beyond {archive_script}"
            )
        optimized_json, optimized_markdown = _optimized_final_report_pair(repo_path)
        if trace.get("last_progress_path") == str(optimized_json.resolve()) and trace.get(
            "in_flight_path"
        ) == str(optimized_markdown.resolve()):
            click.echo(
                f"{prefix}Optimized-report boundary: exact bounded JSON indexing preserved "
                f"lexical progress into {optimized_markdown.relative_to(repo_path)}"
            )
        if (
            trace.get("last_progress_path") == str(optimized_markdown.resolve())
            and trace.get("in_flight_path") is None
        ):
            click.echo(
                f"{prefix}Optimized-report successor: bounded Markdown indexing preserved lexical "
                f"progress beyond {optimized_json.relative_to(repo_path)}"
            )
    if trace.get("summary_call_file_path"):
        click.echo(f"{prefix}Timed-out summary file: {trace['summary_call_file_path']}")
    if trace.get("summary_call_chunk_ids"):
        click.echo(
            f"{prefix}Timed-out summary chunks: {', '.join(trace['summary_call_chunk_ids'])}"
        )
    if trace.get("summary_call_timeout_seconds") is not None:
        click.echo(f"{prefix}Timed-out summary timeout: {trace['summary_call_timeout_seconds']}")
    if trace.get("runtime_restore_performed"):
        mode = trace.get("runtime_restore_mode") or "unknown"
        click.echo(f"{prefix}Trace runtime restore: performed via {mode}")
    elif trace.get("runtime_restore_declined_reason"):
        click.echo(
            f"{prefix}Trace runtime restore: skipped - {trace['runtime_restore_declined_reason']}"
        )
    diagnostics = trace.get("storage_diagnostics")
    if isinstance(diagnostics, dict) and diagnostics:
        parts = []
        for key in ("status", "journal_mode", "readonly"):
            if key in diagnostics:
                parts.append(f"{key}={diagnostics[key]}")
        if parts:
            click.echo(f"{prefix}Trace storage diagnostics: {' '.join(parts)}")


def _force_full_trace_is_stale(trace: dict[str, Any]) -> bool:
    if trace.get("status") != "running":
        return False
    trace_timestamp = trace.get("trace_timestamp")
    if not isinstance(trace_timestamp, str) or not trace_timestamp:
        return False
    try:
        observed_at = datetime.fromisoformat(trace_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    try:
        timeout_seconds = max(float(os.getenv("MCP_INDEX_LEXICAL_TIMEOUT_SECONDS", "20")), 1.0)
    except ValueError:
        timeout_seconds = 20.0
    age_seconds = (datetime.now(timezone.utc) - observed_at).total_seconds()
    if age_seconds < 0:
        return False
    return age_seconds > (timeout_seconds * 2)


def _print_fast_report_boundary(prefix: str, repo_path: Path) -> None:
    for ignore_name in (".mcp-index-ignore", ".gitignore"):
        ignore_path = repo_path / ignore_name
        if not ignore_path.exists():
            continue
        try:
            for raw_line in ignore_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if line == "fast_test_results/fast_report_*.md":
                    click.echo(
                        f"{prefix}Lexical boundary: ignoring generated fast-test reports matching "
                        "fast_test_results/fast_report_*.md"
                    )
                    return
        except OSError:
            return


def _print_test_workspace_boundary(prefix: str, repo_path: Path) -> None:
    for ignore_name in (".mcp-index-ignore", ".gitignore"):
        ignore_path = repo_path / ignore_name
        if not ignore_path.exists():
            continue
        try:
            for raw_line in ignore_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if line == "test_workspace/":
                    click.echo(
                        f"{prefix}Lexical boundary: fixture repositories under test_workspace/ "
                        "are ignored during lexical walking"
                    )
                    return
        except OSError:
            return


def _print_ai_docs_overview_boundary(prefix: str, repo_path: Path) -> None:
    ai_docs_dir = repo_path / "ai_docs"
    if not ai_docs_dir.is_dir():
        return
    if any(path.is_file() for path in ai_docs_dir.glob("*_overview.md")):
        click.echo(
            f"{prefix}Lexical boundary: using bounded Markdown indexing for ai_docs/*_overview.md"
        )


def _print_jedi_markdown_boundary(prefix: str, repo_path: Path) -> None:
    jedi_doc = repo_path / "ai_docs" / "jedi.md"
    if jedi_doc.is_file():
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Markdown indexing for ai_docs/jedi.md"
        )


def _print_ai_docs_readme_tail_boundary(
    prefix: str, repo_path: Path, trace: Optional[dict[str, Any]]
) -> None:
    if not trace:
        return
    pair_paths = (
        repo_path / "ai_docs" / "prometheus_overview.md",
        repo_path / "ai_docs" / "README.md",
    )
    if not all(path.is_file() for path in pair_paths):
        return
    referenced = {trace.get("last_progress_path"), trace.get("in_flight_path")}
    resolved_pair_paths = {str(path.resolve()) for path in pair_paths}
    if referenced == resolved_pair_paths:
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Markdown indexing for "
            "ai_docs/prometheus_overview.md -> ai_docs/README.md"
        )


def _print_architecture_api_markdown_boundary(prefix: str, repo_path: Path) -> None:
    seam_paths = (
        repo_path / "docs" / "architecture" / "P2B-known-limits.md",
        repo_path / "docs" / "api" / "API-REFERENCE.md",
    )
    if all(path.is_file() for path in seam_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Markdown indexing for "
            "docs/architecture/P2B-known-limits.md -> docs/api/API-REFERENCE.md"
        )


def _print_validation_markdown_boundaries(prefix: str, repo_path: Path) -> None:
    for relative_path in (
        "docs/validation/ga-closeout-decision.md",
        "docs/validation/mre2e-evidence.md",
    ):
        if (repo_path / relative_path).is_file():
            click.echo(
                f"{prefix}Lexical boundary: using exact bounded Markdown indexing for "
                f"{relative_path}"
            )


def _print_claude_command_markdown_boundary(prefix: str, repo_path: Path) -> None:
    command_paths = (
        repo_path / ".claude" / "commands" / "execute-lane.md",
        repo_path / ".claude" / "commands" / "plan-phase.md",
    )
    if all(path.is_file() for path in command_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Markdown indexing for "
            ".claude/commands/execute-lane.md -> .claude/commands/plan-phase.md"
        )


def _print_benchmark_markdown_boundary(prefix: str, repo_path: Path) -> None:
    benchmark_paths = (
        repo_path
        / "docs"
        / "benchmarks"
        / "mcp_vs_native_benchmark_fullrepo_fireworks_qwen_voyage_local_iter5_rerun.md",
        repo_path / "docs" / "benchmarks" / "production_benchmark.md",
    )
    if all(path.is_file() for path in benchmark_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Markdown indexing for "
            "docs/benchmarks/mcp_vs_native_benchmark_fullrepo_fireworks_qwen_voyage_local_iter5_rerun.md "
            "-> docs/benchmarks/production_benchmark.md"
        )


def _print_support_docs_markdown_boundary(prefix: str, repo_path: Path) -> None:
    support_doc_paths = (
        repo_path / "docs" / "markdown-table-of-contents.md",
        repo_path / "docs" / "SUPPORT_MATRIX.md",
    )
    if all(path.is_file() for path in support_doc_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Markdown indexing for "
            "docs/markdown-table-of-contents.md -> docs/SUPPORT_MATRIX.md"
        )


def _print_late_v7_phase_plan_markdown_boundary(prefix: str, repo_path: Path) -> None:
    phase_plan_paths = (
        repo_path / "plans" / "phase-plan-v7-SEMCODEXLOOPRELAPSETAIL.md",
        repo_path / "plans" / "phase-plan-v7-SEMGARELTAIL.md",
    )
    if all(path.is_file() for path in phase_plan_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Markdown indexing for "
            "plans/phase-plan-v7-SEMCODEXLOOPRELAPSETAIL.md -> "
            "plans/phase-plan-v7-SEMGARELTAIL.md"
        )


def _trace_references_path(trace: Optional[dict[str, Any]], path: Path) -> bool:
    if not trace:
        return False
    normalized = str(path.resolve())
    return normalized in {
        trace.get("last_progress_path"),
        trace.get("in_flight_path"),
    }


def _print_rebound_phase_plan_markdown_boundary(
    prefix: str, repo_path: Path, trace: Optional[dict[str, Any]]
) -> bool:
    phase_plan_paths = (
        repo_path / "plans" / "phase-plan-v7-SEMVERIFYSIMTAIL.md",
        repo_path / "plans" / "phase-plan-v7-SEMAPIDOCSTAIL.md",
    )
    if not all(path.is_file() for path in phase_plan_paths):
        return False
    if trace and any(
        _trace_references_path(
            trace,
            path,
        )
        for path in (
            repo_path / "plans" / "phase-plan-v7-SEMCODEXLOOPRELAPSETAIL.md",
            repo_path / "plans" / "phase-plan-v7-SEMGARELTAIL.md",
        )
    ):
        return False
    click.echo(
        f"{prefix}Lexical boundary: using exact bounded Markdown indexing for "
        "plans/phase-plan-v7-SEMVERIFYSIMTAIL.md -> "
        "plans/phase-plan-v7-SEMAPIDOCSTAIL.md"
    )
    return True


def _print_phase_plan_markdown_boundaries(
    prefix: str, repo_path: Path, trace: Optional[dict[str, Any]]
) -> None:
    if _print_rebound_phase_plan_markdown_boundary(prefix, repo_path, trace):
        return

    _print_late_v7_phase_plan_markdown_boundary(prefix, repo_path)


def _print_historical_phase_plan_markdown_boundary(prefix: str, repo_path: Path) -> None:
    phase_plan_paths = (
        repo_path / "plans" / "phase-plan-v6-WATCH.md",
        repo_path / "plans" / "phase-plan-v1-p19.md",
    )
    if all(path.is_file() for path in phase_plan_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Markdown indexing for "
            "plans/phase-plan-v6-WATCH.md -> plans/phase-plan-v1-p19.md"
        )


def _print_historical_v1_phase_plan_markdown_boundary(prefix: str, repo_path: Path) -> None:
    phase_plan_paths = (
        repo_path / "plans" / "phase-plan-v1-p13.md",
        repo_path / "plans" / "phase-plan-v1-p3.md",
    )
    if all(path.is_file() for path in phase_plan_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Markdown indexing for "
            "plans/phase-plan-v1-p13.md -> plans/phase-plan-v1-p3.md"
        )


def _print_mixed_version_phase_plan_markdown_boundary(prefix: str, repo_path: Path) -> None:
    phase_plan_paths = (
        repo_path / "plans" / "phase-plan-v7-SEMPHASETAIL.md",
        repo_path / "plans" / "phase-plan-v5-gagov.md",
    )
    if all(path.is_file() for path in phase_plan_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Markdown indexing for "
            "plans/phase-plan-v7-SEMPHASETAIL.md -> plans/phase-plan-v5-gagov.md"
        )


def _print_semjedi_p4_phase_plan_markdown_boundary(prefix: str, repo_path: Path) -> None:
    phase_plan_paths = (
        repo_path / "plans" / "phase-plan-v7-SEMJEDI.md",
        repo_path / "plans" / "phase-plan-v1-p4.md",
    )
    if all(path.is_file() for path in phase_plan_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Markdown indexing for "
            "plans/phase-plan-v7-SEMJEDI.md -> plans/phase-plan-v1-p4.md"
        )


def _print_visual_report_python_boundary(prefix: str, repo_path: Path) -> None:
    script_path = repo_path / "scripts" / "create_multi_repo_visual_report.py"
    if script_path.is_file():
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "scripts/create_multi_repo_visual_report.py"
        )


def _print_quick_validation_python_boundary(prefix: str, repo_path: Path) -> None:
    script_path = repo_path / "scripts" / "quick_mcp_vs_native_validation.py"
    if script_path.is_file():
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "scripts/quick_mcp_vs_native_validation.py"
        )


def _print_validate_mcp_comprehensive_python_boundary(prefix: str, repo_path: Path) -> None:
    script_path = repo_path / "scripts" / "validate_mcp_comprehensive.py"
    if script_path.is_file():
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "scripts/validate_mcp_comprehensive.py"
        )


def _print_run_reranking_tests_python_boundary(prefix: str, repo_path: Path) -> None:
    script_path = repo_path / "tests" / "root_tests" / "run_reranking_tests.py"
    if script_path.is_file():
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "tests/root_tests/run_reranking_tests.py"
        )


def _print_swift_database_efficiency_python_boundary(prefix: str, repo_path: Path) -> None:
    test_paths = (
        repo_path / "tests" / "root_tests" / "test_swift_plugin.py",
        repo_path / "tests" / "root_tests" / "test_mcp_database_efficiency.py",
    )
    if all(path.is_file() for path in test_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "tests/root_tests/test_swift_plugin.py -> "
            "tests/root_tests/test_mcp_database_efficiency.py"
        )


def _print_route_auth_sandbox_python_boundary(prefix: str, repo_path: Path) -> None:
    test_paths = (
        repo_path / "tests" / "security" / "test_route_auth_coverage.py",
        repo_path / "tests" / "security" / "test_p24_sandbox_degradation.py",
    )
    if all(path.is_file() for path in test_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "tests/security/test_route_auth_coverage.py -> "
            "tests/security/test_p24_sandbox_degradation.py"
        )


def _print_script_language_audit_python_boundary(prefix: str, repo_path: Path) -> None:
    script_paths = (
        repo_path / "scripts" / "migrate_large_index_to_multi_repo.py",
        repo_path / "scripts" / "check_index_languages.py",
    )
    if all(path.is_file() for path in script_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "scripts/migrate_large_index_to_multi_repo.py -> "
            "scripts/check_index_languages.py"
        )


def _print_preflight_upgrade_script_boundary(prefix: str, repo_path: Path) -> None:
    script_paths = (
        repo_path / "scripts" / "preflight_upgrade.sh",
        repo_path / "scripts" / "test_mcp_protocol_direct.py",
    )
    if all(path.is_file() for path in script_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded shell/Python indexing for "
            "scripts/preflight_upgrade.sh -> scripts/test_mcp_protocol_direct.py"
        )


def _print_verify_simulator_script_boundary(prefix: str, repo_path: Path) -> None:
    script_paths = (
        repo_path / "scripts" / "verify_embeddings.py",
        repo_path / "scripts" / "claude_code_behavior_simulator.py",
    )
    if all(path.is_file() for path in script_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "scripts/verify_embeddings.py -> scripts/claude_code_behavior_simulator.py"
        )


def _print_embed_consolidation_script_boundary(prefix: str, repo_path: Path) -> None:
    script_paths = (
        repo_path / "scripts" / "create_semantic_embeddings.py",
        repo_path / "scripts" / "consolidate_real_performance_data.py",
    )
    if all(path.is_file() for path in script_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "scripts/create_semantic_embeddings.py -> "
            "scripts/consolidate_real_performance_data.py"
        )


def _print_test_repo_index_script_boundary(prefix: str, repo_path: Path) -> None:
    script_paths = (
        repo_path / "scripts" / "check_test_index_schema.py",
        repo_path / "scripts" / "ensure_test_repos_indexed.py",
    )
    if all(path.is_file() for path in script_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "scripts/check_test_index_schema.py -> "
            "scripts/ensure_test_repos_indexed.py"
        )


def _print_missing_repo_semantic_script_boundary(prefix: str, repo_path: Path) -> None:
    script_paths = (
        repo_path / "scripts" / "index_missing_repos_semantic.py",
        repo_path / "scripts" / "identify_working_indexes.py",
    )
    if all(path.is_file() for path in script_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "scripts/index_missing_repos_semantic.py -> "
            "scripts/identify_working_indexes.py"
        )


def _print_utility_verification_script_boundary(prefix: str, repo_path: Path) -> None:
    script_paths = (
        repo_path / "scripts" / "utilities" / "prepare_index_for_upload.py",
        repo_path / "scripts" / "utilities" / "verify_tool_usage.py",
    )
    if all(path.is_file() for path in script_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "scripts/utilities/prepare_index_for_upload.py -> "
            "scripts/utilities/verify_tool_usage.py"
        )


def _print_qdrant_report_script_boundary(prefix: str, repo_path: Path) -> None:
    script_paths = (
        repo_path / "scripts" / "map_repos_to_qdrant.py",
        repo_path / "scripts" / "create_claude_code_aware_report.py",
    )
    if all(path.is_file() for path in script_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "scripts/map_repos_to_qdrant.py -> "
            "scripts/create_claude_code_aware_report.py"
        )


def _print_optimized_upload_script_boundary(prefix: str, repo_path: Path) -> None:
    script_paths = (
        repo_path / "scripts" / "execute_optimized_analysis.py",
        repo_path / "scripts" / "index-artifact-upload-v2.py",
    )
    if all(path.is_file() for path in script_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "scripts/execute_optimized_analysis.py -> "
            "scripts/index-artifact-upload-v2.py"
        )


def _print_edit_retrieval_script_boundary(prefix: str, repo_path: Path) -> None:
    script_paths = (
        repo_path / "scripts" / "analyze_claude_code_edits.py",
        repo_path / "scripts" / "verify_mcp_retrieval.py",
    )
    if all(path.is_file() for path in script_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "scripts/analyze_claude_code_edits.py -> "
            "scripts/verify_mcp_retrieval.py"
        )


def _print_comprehensive_query_full_sync_script_boundary(prefix: str, repo_path: Path) -> None:
    script_paths = (
        repo_path / "scripts" / "run_comprehensive_query_test.py",
        repo_path / "scripts" / "index_all_repos_semantic_full.py",
    )
    if all(path.is_file() for path in script_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "scripts/run_comprehensive_query_test.py -> "
            "scripts/index_all_repos_semantic_full.py"
        )


def _print_centralization_script_boundary(prefix: str, repo_path: Path) -> None:
    script_paths = (
        repo_path / "scripts" / "real_strategic_recommendations.py",
        repo_path / "scripts" / "migrate_to_centralized.py",
    )
    if all(path.is_file() for path in script_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "scripts/real_strategic_recommendations.py -> "
            "scripts/migrate_to_centralized.py"
        )


def _print_reindex_demo_script_boundary(prefix: str, repo_path: Path) -> None:
    script_paths = (
        repo_path / "scripts" / "reindex_current_repository.py",
        repo_path / "scripts" / "demo_centralized_indexes.py",
    )
    if all(path.is_file() for path in script_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "scripts/reindex_current_repository.py -> "
            "scripts/demo_centralized_indexes.py"
        )


def _print_artifact_publish_race_python_boundary(prefix: str, repo_path: Path) -> None:
    test_path = repo_path / "tests" / "test_artifact_publish_race.py"
    if test_path.is_file():
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "tests/test_artifact_publish_race.py"
        )


def _print_visualization_quick_charts_python_boundary(prefix: str, repo_path: Path) -> None:
    chart_path = repo_path / "mcp_server" / "visualization" / "quick_charts.py"
    if chart_path.is_file():
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "mcp_server/visualization/quick_charts.py"
        )


def _print_docs_governance_contract_python_boundary(prefix: str, repo_path: Path) -> None:
    contract_paths = (
        repo_path / "tests" / "docs" / "test_mre2e_evidence_contract.py",
        repo_path / "tests" / "docs" / "test_gagov_governance_contract.py",
    )
    if all(path.is_file() for path in contract_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "tests/docs/test_mre2e_evidence_contract.py -> "
            "tests/docs/test_gagov_governance_contract.py"
        )


def _print_docs_test_tail_python_boundary(prefix: str, repo_path: Path) -> None:
    contract_paths = (
        repo_path / "tests" / "docs" / "test_gaclose_evidence_closeout.py",
        repo_path / "tests" / "docs" / "test_p8_deployment_security.py",
    )
    if all(path.is_file() for path in contract_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "tests/docs/test_gaclose_evidence_closeout.py -> "
            "tests/docs/test_p8_deployment_security.py"
        )


def _print_docs_contract_tail_python_boundary(prefix: str, repo_path: Path) -> None:
    contract_paths = (
        repo_path / "tests" / "docs" / "test_semincr_contract.py",
        repo_path / "tests" / "docs" / "test_gabase_ga_readiness_contract.py",
    )
    if all(path.is_file() for path in contract_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "tests/docs/test_semincr_contract.py -> "
            "tests/docs/test_gabase_ga_readiness_contract.py"
        )


def _print_ga_release_docs_tail_python_boundary(prefix: str, repo_path: Path) -> None:
    contract_paths = (
        repo_path / "tests" / "docs" / "test_garc_rc_soak_contract.py",
        repo_path / "tests" / "docs" / "test_garel_ga_release_contract.py",
    )
    if all(path.is_file() for path in contract_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "tests/docs/test_garc_rc_soak_contract.py -> "
            "tests/docs/test_garel_ga_release_contract.py"
        )


def _print_docs_truth_tail_python_boundary(prefix: str, repo_path: Path) -> None:
    contract_paths = (
        repo_path / "tests" / "docs" / "test_p23_doc_truth.py",
        repo_path / "tests" / "docs" / "test_semdogfood_evidence_contract.py",
    )
    if all(path.is_file() for path in contract_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "tests/docs/test_p23_doc_truth.py -> "
            "tests/docs/test_semdogfood_evidence_contract.py"
        )


def _print_p24_plugin_tail_python_boundary(prefix: str, repo_path: Path) -> None:
    contract_paths = (
        repo_path / "tests" / "test_p24_plugin_availability.py",
        repo_path / "tests" / "test_dispatcher_extension_gating.py",
    )
    if all(path.is_file() for path in contract_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "tests/test_p24_plugin_availability.py -> "
            "tests/test_dispatcher_extension_gating.py"
        )


def _print_mock_plugin_fixture_python_boundary(prefix: str, repo_path: Path) -> None:
    fixture_paths = (
        repo_path / "tests" / "security" / "fixtures" / "mock_plugin" / "plugin.py",
        repo_path / "tests" / "security" / "fixtures" / "mock_plugin" / "__init__.py",
    )
    if all(path.is_file() for path in fixture_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "tests/security/fixtures/mock_plugin/plugin.py -> "
            "tests/security/fixtures/mock_plugin/__init__.py"
        )


def _print_integration_obs_smoke_python_boundary(prefix: str, repo_path: Path) -> None:
    test_paths = (
        repo_path / "tests" / "integration" / "__init__.py",
        repo_path / "tests" / "integration" / "obs" / "test_obs_smoke.py",
    )
    if all(path.is_file() for path in test_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded Python indexing for "
            "tests/integration/__init__.py -> "
            "tests/integration/obs/test_obs_smoke.py"
        )


def _print_devcontainer_json_boundary(prefix: str, repo_path: Path) -> None:
    config_path = repo_path / ".devcontainer" / "devcontainer.json"
    if config_path.is_file():
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded JSON indexing for "
            ".devcontainer/devcontainer.json"
        )


def _devcontainer_tail_pair(repo_path: Path) -> tuple[Path, Path]:
    return (
        repo_path / ".devcontainer" / "post_create.sh",
        repo_path / ".devcontainer" / "devcontainer.json",
    )


def _print_archive_tail_json_boundary(prefix: str, repo_path: Path) -> None:
    archive_script, archive_json = _archive_tail_pair(repo_path)
    if archive_script.is_file() and archive_json.is_file():
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded JSON indexing for "
            "analysis_archive/semantic_vs_sql_comparison_1750926162.json after "
            "analysis_archive/scripts_archive/scripts_test_files/verify_mcp_fix.py"
        )


def _print_optimized_final_report_boundary(prefix: str, repo_path: Path) -> None:
    optimized_json, optimized_markdown = _optimized_final_report_pair(repo_path)
    if optimized_json.is_file() and optimized_markdown.is_file():
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded JSON indexing for "
            "final_optimized_report_final_report_1750958096/final_report_data.json -> "
            "final_optimized_report_final_report_1750958096/FINAL_OPTIMIZED_ANALYSIS_REPORT.md"
        )


def _print_legacy_codex_phase_loop_boundary(prefix: str, repo_path: Path) -> None:
    legacy_paths = (
        repo_path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260424T180441Z-01-gagov-execute"
        / "launch.json",
        repo_path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T071807Z-02-artpub-execute"
        / "terminal-summary.json",
        repo_path / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "events.jsonl",
        repo_path / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "state.json",
    )
    if all(path.is_file() for path in legacy_paths):
        click.echo(
            f"{prefix}Lexical boundary: using exact bounded JSON/JSONL indexing for "
            "legacy .codex/phase-loop compatibility runtime artifacts while canonical "
            ".phase-loop remains authoritative"
        )


@repository.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--auto-sync/--no-auto-sync", default=True, help="Enable automatic synchronization")
@click.option("--artifacts/--no-artifacts", default=True, help="Enable artifact generation")
def register(path: str, auto_sync: bool, artifacts: bool):
    """Register a repository for tracking and indexing."""
    try:
        registry = RepositoryRegistry()
        repo_id = registry.register_repository(path, auto_sync=auto_sync)

        # Set artifact preference
        if not artifacts:
            registry.set_artifact_enabled(repo_id, False)

        repo_info = registry.get_repository(repo_id)
        if repo_info is None:
            raise ValueError(f"Failed to load registered repository: {repo_id}")
        repo_info = cast(Any, repo_info)

        click.echo(click.style(f"✓ Registered repository: {repo_info.name}", fg="green"))
        click.echo(f"  ID: {repo_id}")
        click.echo(f"  Path: {repo_info.path}")
        click.echo(f"  Remote: {repo_info.url or 'None'}")
        click.echo(f"  Auto-sync: {'Yes' if auto_sync else 'No'}")
        click.echo(f"  Artifacts: {'Yes' if artifacts else 'No'}")
        click.echo(f"  Artifact backend: {repo_info.artifact_backend or 'local_workspace'}")
        click.echo(f"  Artifact health: {repo_info.artifact_health or 'missing'}")

        # Check if index exists
        index_path = Path(repo_info.index_location) / "current.db"
        if not index_path.exists():
            click.echo(
                click.style(
                    "\nNote: No local runtime index found. Run 'mcp-index artifact pull --latest' or 'mcp repository sync' to prepare this repository.",
                    fg="yellow",
                )
            )

    except MultipleWorktreesUnsupportedError as e:
        details = e.to_dict()
        click.echo(click.style("✗ Failed to register repository", fg="red"), err=True)
        click.echo(f"  Code: {details['code']}", err=True)
        click.echo(f"  Registered path: {details['registered_path']}", err=True)
        click.echo(f"  Requested path: {details['requested_path']}", err=True)
        click.echo(f"  Git common dir: {details['git_common_dir']}", err=True)
        click.echo(f"  Remediation: {details['remediation']}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(click.style(f"✗ Failed to register repository: {e}", fg="red"), err=True)
        sys.exit(1)


@repository.command()
@click.option("--verbose", "-v", is_flag=True, help="Show detailed information")
def list(verbose: bool):
    """List all registered repositories."""
    try:
        registry = RepositoryRegistry()
        repos = registry.get_all_repositories()

        if not repos:
            click.echo("No repositories registered.")
            click.echo("Register a repository with: mcp repository register <path>")
            return

        click.echo(f"Registered repositories ({len(repos)}):\n")

        for repo_id, repo_info in repos.items():
            status_icon = "✓" if not repo_info.needs_update() else "⚠"
            sync_status = "auto" if repo_info.auto_sync else "manual"

            click.echo(f"{status_icon} {repo_info.name} [{repo_id[:8]}...]")
            click.echo(f"  Path: {repo_info.path}")

            if verbose:
                click.echo(f"  Remote: {repo_info.url or 'None'}")
                click.echo(
                    f"  Current commit: {repo_info.current_commit[:8] if repo_info.current_commit else 'None'}"
                )
                click.echo(
                    f"  Indexed commit: {repo_info.last_indexed_commit[:8] if repo_info.last_indexed_commit else 'Never'}"
                )
                click.echo(f"  Last indexed: {repo_info.last_indexed or 'Never'}")
                click.echo(f"  Sync: {sync_status}")
                click.echo(
                    f"  Artifacts: {'enabled' if repo_info.artifact_enabled else 'disabled'}"
                )
                click.echo(f"  Artifact backend: {repo_info.artifact_backend or 'local_workspace'}")
                click.echo(f"  Artifact health: {repo_info.artifact_health or 'missing'}")
                click.echo(
                    "  Semantic profiles: "
                    + (
                        ", ".join(repo_info.available_semantic_profiles)
                        if repo_info.available_semantic_profiles
                        else "none"
                    )
                )
                click.echo(
                    f"  Last recovered commit: {repo_info.last_recovered_commit[:8] if repo_info.last_recovered_commit else 'Never'}"
                )
                click.echo(
                    f"  Last published commit: {repo_info.last_published_commit[:8] if repo_info.last_published_commit else 'Never'}"
                )

                health_row = build_health_row(repo_info)
                readiness = health_row["readiness"]
                color = "green" if health_row["ready"] else "yellow"
                click.echo(click.style(f"  Readiness: {readiness}", fg=color))
                if health_row["remediation"]:
                    click.echo(f"  Remediation: {health_row['remediation']}")
                _print_rollout_surface("  ", health_row)

            click.echo()

    except Exception as e:
        click.echo(click.style(f"✗ Error listing repositories: {e}", fg="red"), err=True)
        sys.exit(1)


@repository.command()
@click.argument("repo_id")
def unregister(repo_id: str):
    """Remove a repository from tracking."""
    try:
        registry = RepositoryRegistry()
        repo_info = registry.get_repository(repo_id)

        if not repo_info:
            # Try to find by name or path
            for rid, info in registry.get_all_repositories().items():
                if info.name == repo_id or info.path == repo_id:
                    repo_id = rid
                    repo_info = info
                    break

        if not repo_info:
            click.echo(click.style(f"✗ Repository not found: {repo_id}", fg="red"), err=True)
            sys.exit(1)

        # Confirm
        if not click.confirm(f"Remove {repo_info.name} from tracking?"):
            return

        store_registry = StoreRegistry.for_registry(registry)
        store_registry.close(repo_id)
        try:
            from mcp_server.plugins.plugin_set_registry import PluginSetRegistry

            PluginSetRegistry().evict(repo_id)
        except Exception:
            pass
        try:
            from mcp_server.utils.semantic_indexer_registry import SemanticIndexerRegistry

            SemanticIndexerRegistry(registry).evict(repo_id)
        except Exception:
            pass

        if registry.unregister_repository(repo_id):
            click.echo(click.style(f"✓ Unregistered repository: {repo_info.name}", fg="green"))
        else:
            click.echo(click.style("✗ Failed to unregister repository", fg="red"), err=True)
            sys.exit(1)

    except Exception as e:
        click.echo(click.style(f"✗ Error: {e}", fg="red"), err=True)
        sys.exit(1)


@repository.command()
@click.option("--repo-id", help="Repository ID (default: current directory)")
@click.option("--force-full", is_flag=True, help="Force full reindex instead of incremental")
@click.option("--all", "sync_all", is_flag=True, help="Sync all repositories")
def sync(repo_id: Optional[str], force_full: bool, sync_all: bool):
    """Synchronize repository index with current git state."""
    try:
        registry = RepositoryRegistry()
        store_registry = StoreRegistry.for_registry(registry)
        repo_resolver = RepoResolver(registry, store_registry)

        if sync_all:
            # Sync all repositories
            click.echo("Synchronizing all repositories...")

            # Create index manager
            dispatcher = EnhancedDispatcher(
                semantic_search_enabled=reload_settings().semantic_search_enabled
            )
            index_manager = GitAwareIndexManager(
                registry,
                dispatcher,
                repo_resolver=repo_resolver,
                store_registry=store_registry,
            )
            results = index_manager.sync_all_repositories()

            for rid, result in results.items():
                repo_info = registry.get_repository(rid)
                if repo_info:
                    if result.action == "up_to_date":
                        click.echo(
                            click.style(f"✓ {repo_info.name}: Already up to date", fg="green")
                        )
                    elif result.action in {"indexed", "incremental_update", "full_index"}:
                        click.echo(
                            click.style(
                                f"✓ {repo_info.name}: Indexed {result.files_processed} files in {result.duration_seconds:.1f}s",
                                fg="green",
                            )
                        )
                    elif result.action == "downloaded":
                        click.echo(
                            click.style(
                                f"✓ {repo_info.name}: Downloaded from artifact",
                                fg="green",
                            )
                        )
                    else:
                        click.echo(click.style(f"✗ {repo_info.name}: {result.error}", fg="red"))

        else:
            # Sync single repository
            if not repo_id:
                # Try current directory
                repo_info = registry.get_repository_by_path(os.getcwd())
                if repo_info:
                    repo_id = repo_info.repository_id
                else:
                    click.echo(
                        click.style(
                            "✗ Current directory is not a registered repository",
                            fg="red",
                        ),
                        err=True,
                    )
                    click.echo("Register with: mcp repository register .")
                    sys.exit(1)

            assert repo_id is not None

            # Create dispatcher and index manager
            repo_info = registry.get_repository(repo_id)
            if not repo_info:
                click.echo(
                    click.style(f"✗ Repository not found: {repo_id}", fg="red"),
                    err=True,
                )
                sys.exit(1)

            # Create necessary components
            dispatcher = EnhancedDispatcher(
                semantic_search_enabled=reload_settings().semantic_search_enabled
            )
            index_manager = GitAwareIndexManager(
                registry,
                dispatcher,
                repo_resolver=repo_resolver,
                store_registry=store_registry,
            )

            click.echo(f"Synchronizing {repo_info.name}...")

            # Update current commit
            registry.update_current_commit(repo_id)

            # Sync the repository
            result = index_manager.sync_repository_index(repo_id, force_full=force_full)

            if result.action == "up_to_date":
                click.echo(click.style("✓ Repository is already up to date", fg="green"))
            elif result.action in {"indexed", "incremental_update", "full_index"}:
                click.echo(
                    click.style(
                        f"✓ Indexed {result.files_processed} files in {result.duration_seconds:.1f}s",
                        fg="green",
                    )
                )
            elif result.action == "downloaded":
                click.echo(click.style("✓ Downloaded index from artifact", fg="green"))
                click.echo(
                    "  Next step: run 'mcp-index artifact reconcile-workspace' if you manage multiple local repositories."
                )
            elif result.action == "failed":
                _print_sync_semantic_details("  ", result.semantic)
                click.echo(click.style(f"✗ Sync failed: {result.error}", fg="red"), err=True)
                sys.exit(1)

    except Exception as e:
        click.echo(click.style(f"✗ Sync failed: {e}", fg="red"), err=True)
        sys.exit(1)


@repository.command()
@click.option("--repo-id", help="Repository ID")
def status(repo_id: Optional[str]):
    """Show detailed repository status."""
    try:
        registry = RepositoryRegistry()
        store_registry = StoreRegistry.for_registry(registry)
        repo_resolver = RepoResolver(registry, store_registry)

        if not repo_id:
            # Try current directory
            repo_info = registry.get_repository_by_path(os.getcwd())
            if repo_info:
                repo_id = repo_info.repository_id
            else:
                click.echo(
                    click.style("✗ Current directory is not a registered repository", fg="red"),
                    err=True,
                )
                sys.exit(1)

        assert repo_id is not None

        # Get repository status
        index_manager = GitAwareIndexManager(
            registry,
            repo_resolver=repo_resolver,
            store_registry=store_registry,
        )
        repo_id_str = repo_id
        status = index_manager.get_repository_status(repo_id_str)

        if "error" in status:
            click.echo(click.style(f"✗ {status['error']}", fg="red"), err=True)
            sys.exit(1)

        settings = reload_settings()
        semantic_preflight = run_semantic_preflight(
            settings=settings,
            profile=settings.get_semantic_default_profile(),
            strict=settings.semantic_strict_mode,
        ).to_dict()
        repo_ctx = None
        if hasattr(repo_resolver, "resolve"):
            repo_ctx = repo_resolver.resolve(Path(status["path"]))
        if repo_ctx is not None:
            semantic_readiness = ReadinessClassifier.classify_semantic_registered(
                repo_ctx.registry_entry,
                repo_ctx.sqlite_store,
            )
            status["semantic_readiness"] = semantic_readiness.state.value
            status["semantic_ready"] = semantic_readiness.ready
            status["semantic_readiness_code"] = semantic_readiness.code
            status["semantic_remediation"] = semantic_readiness.remediation
            status.setdefault("features", {}).setdefault("semantic", {})[
                "readiness"
            ] = semantic_readiness.to_dict()
        status["features"]["semantic"]["preflight"] = semantic_preflight

        # Display status
        click.echo(f"Repository: {status['name']}")
        click.echo(f"Path: {status['path']}")
        click.echo(f"ID: {status['repo_id']}")
        click.echo()

        # Git status
        click.echo("Git Status:")
        click.echo(
            f"  Current commit: {status['current_commit'][:8] if status['current_commit'] else 'None'}"
        )
        click.echo(
            f"  Indexed commit: {status['last_indexed_commit'][:8] if status['last_indexed_commit'] else 'Never'}"
        )
        click.echo(
            click.style(
                f"  Readiness: {status['readiness']}",
                fg="green" if status["ready"] else "yellow",
            )
        )
        if status["remediation"]:
            click.echo(f"  Remediation: {status['remediation']}")
        click.echo(
            click.style(
                f"  Semantic readiness: {status['semantic_readiness']}",
                fg="green" if status.get("semantic_ready") else "yellow",
            )
        )
        if status.get("semantic_remediation"):
            click.echo(f"  Semantic remediation: {status['semantic_remediation']}")
        _print_rollout_surface("  ", status)

        # Index status
        click.echo("\nIndex Status:")
        if status["index_exists"]:
            click.echo(f"  Index size: {status['index_size_mb']:.1f} MB")
            click.echo(f"  Last indexed: {status['last_indexed'] or 'Unknown'}")
        else:
            click.echo(click.style("  No index found", fg="yellow"))
        if status.get("staleness_reason"):
            click.echo(f"  Staleness reason: {status['staleness_reason']}")
        if status.get("last_sync_error"):
            click.echo(f"  Last sync error: {status['last_sync_error']}")
        _print_fast_report_boundary("  ", Path(status["path"]))
        _print_test_workspace_boundary("  ", Path(status["path"]))
        _print_ai_docs_overview_boundary("  ", Path(status["path"]))
        _print_jedi_markdown_boundary("  ", Path(status["path"]))
        _print_ai_docs_readme_tail_boundary(
            "  ",
            Path(status["path"]),
            cast(Optional[dict[str, Any]], status.get("force_full_exit_trace")),
        )
        _print_architecture_api_markdown_boundary("  ", Path(status["path"]))
        _print_validation_markdown_boundaries("  ", Path(status["path"]))
        _print_claude_command_markdown_boundary("  ", Path(status["path"]))
        _print_benchmark_markdown_boundary("  ", Path(status["path"]))
        _print_support_docs_markdown_boundary("  ", Path(status["path"]))
        _print_phase_plan_markdown_boundaries(
            "  ", Path(status["path"]), status.get("force_full_exit_trace")
        )
        _print_historical_phase_plan_markdown_boundary("  ", Path(status["path"]))
        _print_historical_v1_phase_plan_markdown_boundary("  ", Path(status["path"]))
        _print_mixed_version_phase_plan_markdown_boundary("  ", Path(status["path"]))
        _print_semjedi_p4_phase_plan_markdown_boundary("  ", Path(status["path"]))
        _print_visual_report_python_boundary("  ", Path(status["path"]))
        _print_quick_validation_python_boundary("  ", Path(status["path"]))
        _print_validate_mcp_comprehensive_python_boundary("  ", Path(status["path"]))
        _print_run_reranking_tests_python_boundary("  ", Path(status["path"]))
        _print_swift_database_efficiency_python_boundary("  ", Path(status["path"]))
        _print_route_auth_sandbox_python_boundary("  ", Path(status["path"]))
        _print_script_language_audit_python_boundary("  ", Path(status["path"]))
        _print_preflight_upgrade_script_boundary("  ", Path(status["path"]))
        _print_verify_simulator_script_boundary("  ", Path(status["path"]))
        _print_embed_consolidation_script_boundary("  ", Path(status["path"]))
        _print_test_repo_index_script_boundary("  ", Path(status["path"]))
        _print_missing_repo_semantic_script_boundary("  ", Path(status["path"]))
        _print_utility_verification_script_boundary("  ", Path(status["path"]))
        _print_qdrant_report_script_boundary("  ", Path(status["path"]))
        _print_optimized_upload_script_boundary("  ", Path(status["path"]))
        _print_edit_retrieval_script_boundary("  ", Path(status["path"]))
        _print_comprehensive_query_full_sync_script_boundary("  ", Path(status["path"]))
        _print_centralization_script_boundary("  ", Path(status["path"]))
        _print_reindex_demo_script_boundary("  ", Path(status["path"]))
        _print_artifact_publish_race_python_boundary("  ", Path(status["path"]))
        _print_visualization_quick_charts_python_boundary("  ", Path(status["path"]))
        _print_docs_governance_contract_python_boundary("  ", Path(status["path"]))
        _print_docs_test_tail_python_boundary("  ", Path(status["path"]))
        _print_docs_contract_tail_python_boundary("  ", Path(status["path"]))
        _print_ga_release_docs_tail_python_boundary("  ", Path(status["path"]))
        _print_docs_truth_tail_python_boundary("  ", Path(status["path"]))
        _print_p24_plugin_tail_python_boundary("  ", Path(status["path"]))
        _print_mock_plugin_fixture_python_boundary("  ", Path(status["path"]))
        _print_integration_obs_smoke_python_boundary("  ", Path(status["path"]))
        _print_devcontainer_json_boundary("  ", Path(status["path"]))
        _print_archive_tail_json_boundary("  ", Path(status["path"]))
        _print_optimized_final_report_boundary("  ", Path(status["path"]))
        _print_legacy_codex_phase_loop_boundary("  ", Path(status["path"]))
        _print_force_full_exit_trace(
            "  ", status.get("force_full_exit_trace"), Path(status["path"])
        )

        semantic_preflight = status["features"]["semantic"].get("preflight") or {}
        blocker = semantic_preflight.get("blocker") or {}
        effective_config = semantic_preflight.get("effective_config") or {}
        click.echo("\nSemantic Preflight:")
        click.echo(
            "  Active-profile preflight: "
            + ("ready" if semantic_preflight.get("overall_ready") else "blocked")
        )
        click.echo(
            "  Can write semantic vectors: "
            + ("yes" if semantic_preflight.get("can_write_semantic_vectors") else "no")
        )
        if blocker:
            click.echo(f"  Blocker: {blocker.get('code')} - {blocker.get('message')}")
            for fix in blocker.get("remediation") or []:
                click.echo(f"  Remediation: {fix}")
        if effective_config:
            click.echo(f"  Active profile: {effective_config.get('selected_profile')}")
            click.echo(f"  Active collection: {effective_config.get('collection_name')}")
            bootstrap_state = (
                "reused" if semantic_preflight.get("can_write_semantic_vectors") else "blocked"
            )
            click.echo(f"  Collection bootstrap state: {bootstrap_state}")
        semantic_evidence = (
            status.get("features", {}).get("semantic", {}).get("readiness", {}).get("evidence")
            or {}
        )
        if semantic_evidence:
            click.echo("\nSemantic Evidence:")
            _print_semantic_evidence("  ", semantic_evidence)

        # Settings
        click.echo("\nSettings:")
        click.echo(f"  Auto-sync: {'Yes' if status['auto_sync'] else 'No'}")
        click.echo(f"  Artifacts: {'Yes' if status['artifact_enabled'] else 'No'}")
        click.echo(f"  Artifact backend: {status['artifact_backend'] or 'local_workspace'}")
        click.echo(f"  Artifact health: {status['artifact_health'] or 'missing'}")

    except Exception as e:
        click.echo(click.style(f"✗ Error: {e}", fg="red"), err=True)
        sys.exit(1)


@repository.command()
@click.argument("search_paths", nargs=-1, required=True)
@click.option(
    "--register/--no-register",
    default=True,
    help="Automatically register found repositories",
)
def discover(search_paths: tuple, register: bool):
    """Discover git repositories in given paths."""
    try:
        registry = RepositoryRegistry()

        # Expand paths
        paths = []
        for path in search_paths:
            expanded = Path(path).expanduser().resolve()
            if expanded.exists():
                paths.append(str(expanded))
            else:
                click.echo(click.style(f"Warning: Path does not exist: {path}", fg="yellow"))

        if not paths:
            click.echo(click.style("✗ No valid paths provided", fg="red"), err=True)
            sys.exit(1)

        click.echo(f"Searching for repositories in {len(paths)} path(s)...")

        # Discover repositories
        discover = cast(Any, registry).discover_repositories
        discovered = discover(paths)

        if not discovered:
            click.echo("No git repositories found.")
            return

        click.echo(f"\nFound {len(discovered)} repository(ies):")

        registered_count = 0
        for repo_path in discovered:
            repo_name = Path(repo_path).name

            # Check if already registered
            existing = registry.get_repository_by_path(repo_path)
            if existing:
                click.echo(f"  ✓ {repo_name} (already registered)")
                continue

            if register:
                try:
                    repo_id = registry.register_repository(repo_path)
                    click.echo(
                        click.style(
                            f"  ✓ {repo_name} (registered as {repo_id[:8]}...)",
                            fg="green",
                        )
                    )
                    registered_count += 1
                except Exception as e:
                    click.echo(click.style(f"  ✗ {repo_name} (failed: {e})", fg="red"))
            else:
                click.echo(f"  - {repo_name} at {repo_path}")

        if register and registered_count > 0:
            click.echo(f"\nRegistered {registered_count} new repository(ies)")
            click.echo("Run 'mcp repository list -v' to inspect readiness across repos")
            click.echo(
                "Run 'mcp-index artifact workspace-status' to see local-first artifact/runtime readiness"
            )

    except Exception as e:
        click.echo(click.style(f"✗ Discovery failed: {e}", fg="red"), err=True)
        sys.exit(1)


@repository.command()
@click.option("--all", "watch_all", is_flag=True, help="Watch all registered repositories")
@click.option("--daemon", is_flag=True, help="Run as background daemon")
def watch(watch_all: bool, daemon: bool):
    """Start watching repositories for changes."""
    try:
        registry = RepositoryRegistry()
        store_registry = StoreRegistry.for_registry(registry)
        repo_resolver = RepoResolver(registry, store_registry)

        if not watch_all:
            click.echo(
                click.style("✗ Please specify --all to watch all repositories", fg="red"),
                err=True,
            )
            click.echo("Individual repository watching coming soon")
            sys.exit(1)

        # Create components
        dispatcher = EnhancedDispatcher(
            semantic_search_enabled=reload_settings().semantic_search_enabled
        )
        index_manager = GitAwareIndexManager(
            registry,
            dispatcher,
            repo_resolver=repo_resolver,
            store_registry=store_registry,
        )
        artifact_manager = CommitArtifactManager()

        # Create watcher
        watcher = MultiRepositoryWatcher(
            registry=registry,
            dispatcher=dispatcher,
            index_manager=index_manager,
            artifact_manager=artifact_manager,
            repo_resolver=repo_resolver,
        )

        click.echo("Starting multi-repository watcher...")

        # Start watching
        watcher.start_watching_all()

        # Get status
        status = watcher.get_status()
        click.echo(f"Watching {status['watching']} repository(ies)")

        if daemon:
            click.echo("Running as daemon. Press Ctrl+C to stop.")
            try:
                import time

                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                click.echo("\nStopping watcher...")
                watcher.stop_watching_all()
        else:
            click.echo("Watcher started. Run with --daemon to keep running.")

    except Exception as e:
        click.echo(click.style(f"✗ Watch failed: {e}", fg="red"), err=True)
        sys.exit(1)


@repository.command()
@click.option("--enable/--disable", "enable", default=True, help="Enable or disable git hooks")
def init_hooks(enable: bool):
    """Install git hooks for automatic index synchronization."""
    try:
        # Check if in a git repository
        if not Path(".git").exists():
            click.echo(click.style("✗ Not in a git repository", fg="red"), err=True)
            sys.exit(1)

        hooks_dir = Path(".git/hooks")
        hooks_dir.mkdir(exist_ok=True)

        # Source hooks directory
        source_hooks = Path(__file__).parent.parent.parent / "mcp-index-kit" / "hooks"

        hooks_to_install = ["post-commit", "pre-push", "post-checkout", "post-merge"]

        if enable:
            click.echo("Installing git hooks...")

            for hook_name in hooks_to_install:
                source = source_hooks / hook_name
                target = hooks_dir / hook_name

                if source.exists():
                    # Copy hook
                    import shutil

                    shutil.copy2(source, target)

                    # Make executable
                    import stat

                    st = target.stat()
                    target.chmod(st.st_mode | stat.S_IEXEC)

                    click.echo(click.style(f"  ✓ Installed {hook_name}", fg="green"))
                else:
                    click.echo(click.style(f"  ⚠ Source hook not found: {hook_name}", fg="yellow"))

            click.echo("\nGit hooks installed. Index will now sync automatically on:")
            click.echo("  - commit: Update index incrementally")
            click.echo("  - push: Upload index artifacts")
            click.echo("  - checkout/merge: Check for index updates")

        else:
            click.echo("Removing git hooks...")

            for hook_name in hooks_to_install:
                target = hooks_dir / hook_name
                if target.exists():
                    target.unlink()
                    click.echo(click.style(f"  ✓ Removed {hook_name}", fg="green"))

            click.echo("\nGit hooks removed. Manual index management required.")

    except Exception as e:
        click.echo(click.style(f"✗ Hook installation failed: {e}", fg="red"), err=True)
        sys.exit(1)
