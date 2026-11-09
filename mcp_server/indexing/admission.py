"""Shared file-admission predicate for the indexer and the watcher sweep.

The full index walk, single-file indexing and the periodic watcher sweep must
agree on exactly which files the indexer stores. When they disagreed, the sweep
counted files that the indexer deliberately skips (disabled-by-default
languages, unsupported extensions) as drift and forced a futile full rebuild on
every interval (Consiliency/Code-Index-MCP#114). Every caller derives its file
set from :func:`index_admission_reason`, so the two can no longer drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from ..core.ignore_patterns import index_exclusion_reason

PluginSupports = Callable[[Path], bool]

#: Admission reasons that mean "no plugin stores this file" (as opposed to a
#: repository-policy exclusion such as ``ignored`` or ``oversized``).
UNSUPPORTED_EXTENSION = "unsupported_extension"
PLUGIN_REASON_PREFIX = "plugin_"

# Committed files that are indexed through exact, bounded shards instead of a
# language plugin. They are stored even when their language has no enabled
# plugin, so admission must accept them too.
EXACT_BOUNDED_PYTHON_PATHS = {
    "tests/root_tests/run_reranking_tests.py": "exact_run_reranking_tests_rebound",
    "tests/root_tests/test_swift_plugin.py": "exact_test_swift_plugin_rebound",
    "tests/root_tests/test_mcp_database_efficiency.py": (
        "exact_test_mcp_database_efficiency_rebound"
    ),
    "tests/security/test_route_auth_coverage.py": "exact_test_route_auth_coverage_rebound",
    "tests/security/test_p24_sandbox_degradation.py": (
        "exact_test_p24_sandbox_degradation_rebound"
    ),
    "tests/test_p24_plugin_availability.py": ("exact_test_p24_plugin_availability_rebound"),
    "tests/test_dispatcher_extension_gating.py": ("exact_test_dispatcher_extension_gating_rebound"),
    "scripts/validate_mcp_comprehensive.py": "exact_validate_mcp_comprehensive_rebound",
    "scripts/migrate_large_index_to_multi_repo.py": "exact_migrate_large_index_to_multi_repo_rebound",
    "scripts/check_index_languages.py": "exact_check_index_languages_rebound",
    "scripts/check_test_index_schema.py": "exact_check_test_index_schema_rebound",
    "scripts/ensure_test_repos_indexed.py": "exact_ensure_test_repos_indexed_rebound",
    "scripts/index_missing_repos_semantic.py": "exact_index_missing_repos_semantic_rebound",
    "scripts/identify_working_indexes.py": "exact_identify_working_indexes_rebound",
    "scripts/utilities/prepare_index_for_upload.py": ("exact_prepare_index_for_upload_rebound"),
    "scripts/utilities/verify_tool_usage.py": "exact_verify_tool_usage_rebound",
    "scripts/map_repos_to_qdrant.py": "exact_map_repos_to_qdrant_rebound",
    "scripts/create_claude_code_aware_report.py": ("exact_create_claude_code_aware_report_rebound"),
    "scripts/execute_optimized_analysis.py": "exact_execute_optimized_analysis_rebound",
    "scripts/index-artifact-upload-v2.py": "exact_index_artifact_upload_v2_rebound",
    "scripts/analyze_claude_code_edits.py": "exact_analyze_claude_code_edits_rebound",
    "scripts/verify_mcp_retrieval.py": "exact_verify_mcp_retrieval_rebound",
    "scripts/run_comprehensive_query_test.py": "exact_run_comprehensive_query_test_rebound",
    "scripts/index_all_repos_semantic_full.py": ("exact_index_all_repos_semantic_full_rebound"),
    "scripts/real_strategic_recommendations.py": ("exact_real_strategic_recommendations_rebound"),
    "scripts/migrate_to_centralized.py": "exact_migrate_to_centralized_rebound",
    "scripts/reindex_current_repository.py": "exact_reindex_current_repository_rebound",
    "scripts/demo_centralized_indexes.py": "exact_demo_centralized_indexes_rebound",
    "scripts/test_mcp_protocol_direct.py": "exact_test_mcp_protocol_direct_rebound",
    "scripts/verify_embeddings.py": "exact_verify_embeddings_rebound",
    "scripts/claude_code_behavior_simulator.py": "exact_claude_code_behavior_simulator_rebound",
    "scripts/create_semantic_embeddings.py": "exact_create_semantic_embeddings_rebound",
    "scripts/consolidate_real_performance_data.py": (
        "exact_consolidate_real_performance_data_rebound"
    ),
    "mcp_server/visualization/quick_charts.py": "exact_visualization_quick_charts_rebound",
    "tests/docs/test_mre2e_evidence_contract.py": "exact_mre2e_docs_contract_rebound",
    "tests/docs/test_gagov_governance_contract.py": "exact_gagov_docs_contract_rebound",
    "tests/docs/test_gaclose_evidence_closeout.py": "exact_gaclose_docs_contract_rebound",
    "tests/docs/test_p8_deployment_security.py": "exact_p8_security_docs_contract_rebound",
    "tests/docs/test_semincr_contract.py": "exact_semincr_docs_contract_rebound",
    "tests/docs/test_gabase_ga_readiness_contract.py": ("exact_gabase_docs_contract_rebound"),
    "tests/docs/test_garc_rc_soak_contract.py": "exact_garc_docs_contract_rebound",
    "tests/docs/test_garel_ga_release_contract.py": ("exact_garel_docs_contract_rebound"),
    "tests/docs/test_p23_doc_truth.py": "exact_p23_docs_truth_rebound",
    "tests/docs/test_semdogfood_evidence_contract.py": (
        "exact_semdogfood_evidence_contract_rebound"
    ),
    "tests/security/fixtures/mock_plugin/plugin.py": "exact_mock_plugin_fixture_rebound",
    "tests/security/fixtures/mock_plugin/__init__.py": "exact_mock_plugin_fixture_init_rebound",
    "tests/integration/__init__.py": "exact_integration_package_marker_rebound",
    "tests/integration/obs/test_obs_smoke.py": "exact_integration_obs_smoke_rebound",
}

EXACT_BOUNDED_SHELL_PATHS = {
    "scripts/preflight_upgrade.sh": "exact_preflight_upgrade_rebound",
}

_SHELL_SUFFIXES = frozenset({".sh", ".bash", ".zsh"})


def _relative_posix(path: Path, root: Path) -> Optional[str]:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def exact_bounded_reason(path: Path, root: Path) -> Optional[str]:
    """Return the bounded-shard reason when ``path`` bypasses plugin matching."""
    from ..plugins.generic_treesitter_plugin import GenericTreeSitterPlugin

    suffix = path.suffix.lower()
    if suffix == ".py":
        relative = _relative_posix(path, root)
        return EXACT_BOUNDED_PYTHON_PATHS.get(relative) if relative else None
    if suffix in _SHELL_SUFFIXES:
        relative = _relative_posix(path, root)
        return EXACT_BOUNDED_SHELL_PATHS.get(relative) if relative else None
    if suffix == ".json":
        return GenericTreeSitterPlugin.exact_bounded_json_reason(path, root)
    if suffix == ".jsonl":
        return GenericTreeSitterPlugin.exact_bounded_jsonl_reason(path, root)
    return None


def plugin_admission_reason(
    path: Path,
    root: Path,
    *,
    plugin_supports: Optional[PluginSupports] = None,
) -> Optional[str]:
    """Return why no plugin would store ``path``, or ``None`` when one would.

    A file is stored when it is an exact bounded path, or its extension maps to
    a language whose plugin is ``enabled`` in the current sandbox posture, or a
    plugin that is already loaded claims it through ``supports()``.
    ``plugin_supports`` supplies that last check; callers without loaded
    plugins (the default sweep wiring) omit it.
    """
    from ..plugins.language_registry import get_all_extensions, get_language_by_extension
    from ..plugins.plugin_factory import PluginFactory

    if exact_bounded_reason(path, root) is not None:
        return None
    if path.suffix not in get_all_extensions():
        if plugin_supports is not None and plugin_supports(path):
            return None
        return UNSUPPORTED_EXTENSION
    language = get_language_by_extension(path.suffix.lower())
    state = "unsupported"
    if language is not None:
        state = PluginFactory.get_plugin_availability(language)["state"]
        if state == "enabled":
            return None
    if plugin_supports is not None and plugin_supports(path):
        return None
    return f"{PLUGIN_REASON_PREFIX}{state}"


def index_admission_reason(
    path: Path,
    root: Path,
    is_excluded: Callable[[Path], bool],
    size_bytes: Optional[int] = None,
    *,
    plugin_supports: Optional[PluginSupports] = None,
) -> Optional[str]:
    """Return why the indexer would not store ``path``, or ``None`` if it would.

    Combines the repository-policy exclusion (:func:`index_exclusion_reason`)
    with plugin admission (:func:`plugin_admission_reason`).
    """
    exclusion = index_exclusion_reason(path, root, is_excluded, size_bytes)
    if exclusion:
        return exclusion
    return plugin_admission_reason(path, root, plugin_supports=plugin_supports)


def is_plugin_admission_reason(reason: Optional[str]) -> bool:
    """Return True when ``reason`` means no plugin stores the file."""
    return bool(reason) and (
        reason == UNSUPPORTED_EXTENSION or reason.startswith(PLUGIN_REASON_PREFIX)
    )
