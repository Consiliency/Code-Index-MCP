from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from mcp_server.cli.repository_commands import repository
from mcp_server.storage.multi_repo_manager import RepositoryInfo
from mcp_server.storage.sqlite_store import SQLiteStore


def _repo_info(tmp_path: Path) -> RepositoryInfo:
    repo_path = tmp_path / "repo"
    repo_path.mkdir(exist_ok=True)
    index_base = repo_path / ".mcp-index"
    index_base.mkdir(exist_ok=True)
    index_path = index_base / "current.db"
    (repo_path / ".git").mkdir(exist_ok=True)
    source = repo_path / "README.md"
    source.write_text("repository command fixture\n", encoding="utf-8")
    store = SQLiteStore(str(index_path))
    repository_id = store.ensure_repository_row(repo_path, name="repo")
    store.store_file(
        repository_id,
        path=source,
        relative_path="README.md",
        language="markdown",
    )
    store.close()
    return RepositoryInfo(
        repository_id="repo-1",
        name="repo",
        path=repo_path,
        index_path=index_path,
        language_stats={"python": 1},
        total_files=1,
        total_symbols=1,
        indexed_at=datetime.now(),
        current_commit="abcdef123456",
        last_indexed_commit="abcdef123456",
        current_branch="main",
        tracked_branch="main",
        git_common_dir=str(repo_path / ".git"),
        artifact_enabled=True,
        artifact_backend="local_workspace",
        artifact_health="ready",
        available_semantic_profiles=["commercial_high", "oss_high"],
        index_location=str(index_base),
    )


def _semantic_preflight_ready() -> SimpleNamespace:
    return SimpleNamespace(
        to_dict=lambda: {
            "overall_ready": True,
            "can_write_semantic_vectors": True,
            "effective_config": {
                "selected_profile": "oss_high",
                "collection_name": "code_index__oss_high__v1",
            },
        }
    )


def test_register_sets_local_first_defaults(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_info = _repo_info(tmp_path)

    class FakeRegistry:
        def register_repository(self, path, auto_sync=True, artifact_enabled=True, priority=0):
            return "repo-1"

        def set_artifact_enabled(self, repo_id, enabled):
            return True

        def get_repository(self, repo_id):
            return repo_info

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    result = runner.invoke(repository, ["register", str(repo_path)])

    assert result.exit_code == 0
    assert "Artifact backend: local_workspace" in result.output
    assert "Artifact health: ready" in result.output or "Artifact health: missing" in result.output


def test_list_shows_artifact_readiness_fields(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    repo_info.last_recovered_commit = "abcdef123456"

    class FakeRegistry:
        def get_all_repositories(self):
            return {"repo-1": repo_info}

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)

    result = runner.invoke(repository, ["list", "-v"])

    assert result.exit_code == 0
    assert "Artifact backend: local_workspace" in result.output
    assert "Artifact health: ready" in result.output
    assert "Semantic profiles: commercial_high, oss_high" in result.output
    assert "Readiness: ready" in result.output
    assert "Rollout status: ready" in result.output
    assert "Query surface: ready" in result.output


def test_register_surfaces_multiple_worktrees_error(monkeypatch, tmp_path: Path):
    from mcp_server.storage.repository_registry import MultipleWorktreesUnsupportedError

    runner = CliRunner()
    registered = tmp_path / "registered"
    requested = tmp_path / "requested"
    git_common = tmp_path / "common.git"
    requested.mkdir()

    class FakeRegistry:
        def register_repository(self, path, auto_sync=True, artifact_enabled=True, priority=0):
            raise MultipleWorktreesUnsupportedError(
                registered_path=registered,
                requested_path=requested,
                git_common_dir=git_common,
            )

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)

    result = runner.invoke(repository, ["register", str(requested)])

    assert result.exit_code == 1
    assert "multiple_worktrees_unsupported" in result.output
    assert str(registered.resolve()) in result.output
    assert str(requested.resolve()) in result.output
    assert "unregister" in result.output


def test_unregister_removes_repo_from_registry(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)

    class FakeRegistry:
        def get_repository(self, repo_id):
            return repo_info

        def get_all_repositories(self):
            return {"repo-1": repo_info}

        def unregister_repository(self, repo_id):
            return True

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.click.confirm", lambda prompt: True)
    monkeypatch.setattr(
        "mcp_server.plugins.plugin_set_registry.PluginSetRegistry.evict",
        lambda self, repo_id: None,
    )

    result = runner.invoke(repository, ["unregister", "repo-1"])

    assert result.exit_code == 0
    assert "Unregistered repository: repo" in result.output


def test_sync_guidance_matches_local_first_workflow(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    dispatcher_options = []

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

        def get_repository(self, repo_id):
            return repo_info

        def update_current_commit(self, repo_id):
            return None

    class FakeResult:
        action = "downloaded"
        error = None

    class FakeIndexManager:
        def __init__(self, registry, dispatcher=None, **kwargs):
            pass

        def sync_repository_index(self, repo_id, force_full=False):
            return FakeResult()

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.EnhancedDispatcher",
        lambda **kwargs: dispatcher_options.append(kwargs) or object(),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(semantic_search_enabled=False),
    )
    monkeypatch.setattr("mcp_server.cli.repository_commands.SQLiteStore", lambda path: object())

    result = runner.invoke(repository, ["sync"])

    assert result.exit_code == 0
    assert "Downloaded index from artifact" in result.output
    assert "artifact reconcile-workspace" in result.output
    assert dispatcher_options == [{"semantic_search_enabled": False}]


def test_status_reports_rollout_and_query_surfaces(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    repo_info.staleness_reason = "partial_index_failure"

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required incremental mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "vectors_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "features": {
                    "semantic": {
                        "readiness": {
                            "state": "vectors_missing",
                            "ready": False,
                            "code": "vectors_missing",
                            "remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                            "evidence": {
                                "profile_id": "oss_high",
                                "collection": "code_index__oss_high__v1",
                                "total_chunks": 12,
                                "summary_count": 11,
                                "missing_summaries": 1,
                                "vector_link_count": 10,
                                "missing_vectors": 2,
                                "matching_collection_links": 9,
                                "collection_mismatches": 1,
                            },
                        },
                        "preflight": {
                            "overall_ready": False,
                            "can_write_semantic_vectors": False,
                            "blocker": {
                                "code": "collection_missing",
                                "message": "Qdrant collection is missing for the active semantic profile",
                                "remediation": ["Create the expected collection"],
                            },
                        },
                    }
                },
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": False,
                "can_write_semantic_vectors": False,
                "blocker": {
                    "code": "collection_missing",
                    "message": "Qdrant collection is missing for the active semantic profile",
                    "remediation": ["Create the expected collection"],
                },
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert "Rollout status: partial_index_failure" in result.output
    assert "Query surface: index_unavailable" in result.output
    assert 'safe_fallback: "native_search"' in result.output
    assert "Artifact health: ready" in result.output
    assert "Semantic readiness: vectors_missing" in result.output
    assert (
        "Semantic remediation: Run semantic summary/vector generation for the current profile before semantic queries."
        in result.output
    )
    assert "Semantic Preflight:" in result.output
    assert "Can write semantic vectors: no" in result.output
    assert "collection_missing" in result.output
    assert "Active profile: oss_high" in result.output
    assert "Active collection: code_index__oss_high__v1" in result.output
    assert "Collection bootstrap state: blocked" in result.output
    assert "Semantic Evidence:" in result.output
    assert "Summary-backed chunks: 11" in result.output
    assert "Chunks missing summaries: 1" in result.output
    assert "Vector-linked chunks: 10" in result.output
    assert "Chunks missing vectors: 2" in result.output
    assert "Active collection: code_index__oss_high__v1" in result.output
    assert "Collection-matched links: 9" in result.output
    assert "Collection mismatches: 1" in result.output


def test_status_reports_durable_force_full_exit_trace(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    (repo_info.path / ".mcp-index-ignore").write_text("fast_test_results/fast_report_*.md\n")
    (repo_info.path / "ai_docs").mkdir()
    (repo_info.path / "ai_docs" / "pytest_overview.md").write_text("# pytest\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required incremental mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "blocked_summary_call_timeout",
                    "stage_family": "semantic_closeout",
                    "trace_timestamp": "2026-04-29T09:00:00Z",
                    "current_commit": "abcdef123456",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(repo_info.path / "README.md"),
                    "in_flight_path": str(repo_info.path / "README.md"),
                    "summary_call_timed_out": True,
                    "summary_call_file_path": str(repo_info.path / "README.md"),
                    "summary_call_chunk_ids": ["chunk-1"],
                    "summary_call_timeout_seconds": 30.0,
                    "blocker_source": "summary_call_shutdown",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: ignoring generated fast-test reports matching "
        "fast_test_results/fast_report_*.md" in result.output
    )
    assert (
        "Lexical boundary: using bounded Markdown indexing for ai_docs/*_overview.md"
        in result.output
    )
    assert "Force-full exit trace:" in result.output
    assert "Trace status: interrupted" in result.output
    assert "Trace stage: blocked_summary_call_timeout" in result.output
    assert "Trace stage family: semantic_closeout" in result.output
    assert "Trace timestamp: 2026-04-29T09:00:00Z" in result.output
    assert "Trace blocker source: summary_call_shutdown" in result.output
    assert "Timed-out summary file:" in result.output
    assert "Timed-out summary timeout: 30.0" in result.output


def test_status_reports_storage_closeout_restore_context(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    (repo_info.path / ".mcp-index-ignore").write_text("fast_test_results/fast_report_*.md\n")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required semantic closeout mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "last_sync_error": "disk I/O error [runtime restored via sqlite_restored+qdrant_preserved_empty]",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "completed",
                    "stage": "runtime_restore_completed",
                    "stage_family": "final_closeout",
                    "trace_timestamp": "2026-04-29T12:00:00Z",
                    "current_commit": "abcdef123456",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(
                        repo_info.path / "mcp_server" / "document_processing" / "__init__.py"
                    ),
                    "in_flight_path": str(
                        repo_info.path / "mcp_server" / "document_processing" / "chunk_optimizer.py"
                    ),
                    "blocker_source": "storage_closeout",
                    "storage_failure_family": "sqlite_operational",
                    "storage_failure_reason": "disk_io_error",
                    "storage_failure_message": "disk I/O error",
                    "storage_diagnostics": {
                        "status": "degraded",
                        "journal_mode": "WAL",
                        "readonly": True,
                    },
                    "runtime_restore_performed": True,
                    "runtime_restore_mode": "sqlite_restored+qdrant_preserved_empty",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert "Trace stage: runtime_restore_completed" in result.output
    assert "Trace stage family: final_closeout" in result.output
    assert "Trace blocker source: storage_closeout" in result.output
    assert "Trace storage failure family: sqlite_operational" in result.output
    assert "Trace storage failure reason: disk_io_error" in result.output
    assert "Trace storage failure message: disk I/O error" in result.output
    assert (
        "Trace runtime restore: performed via sqlite_restored+qdrant_preserved_empty"
        in result.output
    )
    assert (
        "Trace storage diagnostics: status=degraded journal_mode=WAL readonly=True" in result.output
    )


def test_status_reports_missing_force_full_exit_trace(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "stale_commit",
                "rollout_remediation": "Run reindex to update the repository index to the current commit.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "stale_commit",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": None,
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert "Force-full exit trace: missing" in result.output
    assert "Trace status:" not in result.output


def test_status_marks_stale_running_force_full_exit_trace(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    blocked_test = repo_info.path / "tests" / "test_artifact_publish_race.py"
    blocked_test.parent.mkdir(parents=True)
    blocked_test.write_text("def test_publish_race():\n    assert True\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "stale_commit",
                "rollout_remediation": "Run reindex to update the repository index to the current commit.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "stale_commit",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "running",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2020-01-01T00:00:00Z",
                    "current_commit": "abcdef123456",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(repo_info.path / "tests" / "test_benchmarks.py"),
                    "in_flight_path": str(blocked_test),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )
    monkeypatch.setenv("MCP_INDEX_LEXICAL_TIMEOUT_SECONDS", "5")

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert "Trace freshness: stale-running snapshot" in result.output
    assert f"Last progress path: {repo_info.path / 'tests' / 'test_benchmarks.py'}" in result.output
    assert f"In-flight path: {blocked_test}" in result.output
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "tests/test_artifact_publish_race.py" in result.output
    )
    assert "In-flight path:" in result.output


def test_status_does_not_report_stale_running_for_interrupted_later_test_pair(
    monkeypatch, tmp_path: Path
):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    blocked_test = repo_info.path / "tests" / "test_reindex_resume.py"
    blocked_test.parent.mkdir(parents=True)
    blocked_test.write_text("def test_reindex_resume():\n    assert True\n", encoding="utf-8")
    prior_test = repo_info.path / "tests" / "test_deployment_runbook_shape.py"
    prior_test.write_text("def test_runbook_shape():\n    assert True\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2020-01-01T00:00:00Z",
                    "current_commit": "abcdef123456",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(prior_test),
                    "in_flight_path": str(blocked_test),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )
    monkeypatch.setenv("MCP_INDEX_LEXICAL_TIMEOUT_SECONDS", "5")

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert "Trace status: interrupted" in result.output
    assert "Trace freshness: stale-running snapshot" not in result.output
    assert f"Last progress path: {prior_test}" in result.output
    assert f"In-flight path: {blocked_test}" in result.output


def test_status_reports_exact_visual_report_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    script = repo_info.path / "scripts" / "create_multi_repo_visual_report.py"
    script.parent.mkdir(parents=True)
    script.write_text("def main():\n    return 1\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "running",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T10:00:00Z",
                    "current_commit": "abcdef123456",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(script),
                    "in_flight_path": str(script),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "scripts/create_multi_repo_visual_report.py" in result.output
    )
    assert f"Last progress path: {script}" in result.output


def test_status_reports_exact_quick_validation_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    previous_script = repo_info.path / "scripts" / "rerun_failed_native_tests.py"
    blocked_script = repo_info.path / "scripts" / "quick_mcp_vs_native_validation.py"
    previous_script.parent.mkdir(parents=True)
    previous_script.write_text("def rerun_failed_tests():\n    return []\n", encoding="utf-8")
    blocked_script.write_text("def compare_results():\n    return []\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "running",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T10:00:00Z",
                    "current_commit": "abcdef123456",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(previous_script),
                    "in_flight_path": str(blocked_script),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )
    monkeypatch.setenv("MCP_INDEX_LEXICAL_TIMEOUT_SECONDS", "5")

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert "Trace freshness: stale-running snapshot" in result.output
    assert f"Last progress path: {previous_script}" in result.output
    assert f"In-flight path: {blocked_script}" in result.output
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "scripts/quick_mcp_vs_native_validation.py" in result.output
    )


def test_status_reports_exact_validate_script_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    previous_script = repo_info.path / "scripts" / "run_test_batch.py"
    blocked_script = repo_info.path / "scripts" / "validate_mcp_comprehensive.py"
    previous_script.parent.mkdir(parents=True)
    previous_script.write_text("def main():\n    return 1\n", encoding="utf-8")
    blocked_script.write_text("def validate_mcp():\n    return True\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T10:00:00Z",
                    "current_commit": "abcdef123456",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(previous_script),
                    "in_flight_path": str(blocked_script),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )
    monkeypatch.setenv("MCP_INDEX_LEXICAL_TIMEOUT_SECONDS", "5")

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert "Trace status: interrupted" in result.output
    assert f"Last progress path: {previous_script}" in result.output
    assert f"In-flight path: {blocked_script}" in result.output
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "scripts/validate_mcp_comprehensive.py" in result.output
    )
    assert "scripts/quick_mcp_vs_native_validation.py" not in result.output


def test_status_reports_exact_artifact_publish_race_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    blocked_test = repo_info.path / "tests" / "test_artifact_publish_race.py"
    blocked_test.parent.mkdir(parents=True)
    blocked_test.write_text("def test_publish_race():\n    assert True\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "running",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T10:00:00Z",
                    "current_commit": "abcdef123456",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(repo_info.path / "tests" / "test_benchmarks.py"),
                    "in_flight_path": str(blocked_test),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "tests/test_artifact_publish_race.py" in result.output
    )
    assert f"Last progress path: {repo_info.path / 'tests' / 'test_benchmarks.py'}" in result.output
    assert f"In-flight path: {blocked_test}" in result.output


def test_status_reports_exact_run_reranking_tests_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    previous_test = repo_info.path / "tests" / "root_tests" / "test_voyage_api.py"
    blocked_test = repo_info.path / "tests" / "root_tests" / "run_reranking_tests.py"
    previous_test.parent.mkdir(parents=True)
    previous_test.write_text("def test_voyage_api():\n    assert True\n", encoding="utf-8")
    blocked_test.write_text("def main():\n    return 1\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T11:43:19Z",
                    "current_commit": "abcdef123456",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(previous_test),
                    "in_flight_path": str(blocked_test),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert "Trace status: interrupted" in result.output
    assert f"Last progress path: {previous_test}" in result.output
    assert f"In-flight path: {blocked_test}" in result.output
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "tests/root_tests/run_reranking_tests.py" in result.output
    )
    assert "tests/test_reindex_resume.py" not in result.output
    assert "scripts/validate_mcp_comprehensive.py" not in result.output


def test_status_reports_exact_swift_database_efficiency_python_boundary(
    monkeypatch, tmp_path: Path
):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    swift_test = repo_info.path / "tests" / "root_tests" / "test_swift_plugin.py"
    db_efficiency_test = repo_info.path / "tests" / "root_tests" / "test_mcp_database_efficiency.py"
    later_file = repo_info.path / "docs" / "status" / "semantic_tail_13.md"
    swift_test.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    swift_test.write_text("def test_swift_plugin_basic():\n    assert True\n", encoding="utf-8")
    db_efficiency_test.write_text(
        "class DatabaseEfficiencyTester:\n    pass\n",
        encoding="utf-8",
    )
    later_file.write_text("# Semantic tail 13\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-30T02:14:12Z",
                    "current_commit": "5117d854",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(db_efficiency_test),
                    "in_flight_path": str(later_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "tests/root_tests/test_swift_plugin.py -> "
        "tests/root_tests/test_mcp_database_efficiency.py" in result.output
    )
    assert f"Last progress path: {db_efficiency_test}" in result.output
    assert f"In-flight path: {later_file}" in result.output
    assert "tests/root_tests/run_reranking_tests.py" not in result.output
    assert "scripts/validate_mcp_comprehensive.py" not in result.output


def test_status_reports_exact_route_auth_sandbox_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    route_auth = repo_info.path / "tests" / "security" / "test_route_auth_coverage.py"
    sandbox_degradation = repo_info.path / "tests" / "security" / "test_p24_sandbox_degradation.py"
    later_file = repo_info.path / "tests" / "security" / "fixtures" / "mock_plugin" / "plugin.py"
    route_auth.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    route_auth.write_text(
        "def test_search_capabilities_requires_auth():\n    assert True\n", encoding="utf-8"
    )
    sandbox_degradation.write_text(
        "def test_worker_missing_extra_failure_has_capability_state():\n    assert True\n",
        encoding="utf-8",
    )
    later_file.write_text("class Plugin:\n    pass\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-30T02:33:57Z",
                    "current_commit": "748d4b870b7a68408260745d0777f108a197dc37",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(route_auth),
                    "in_flight_path": str(sandbox_degradation),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "tests/security/test_route_auth_coverage.py -> "
        "tests/security/test_p24_sandbox_degradation.py" in result.output
    )
    assert "Trace status: interrupted" in result.output
    assert f"Last progress path: {route_auth}" in result.output
    assert f"In-flight path: {sandbox_degradation}" in result.output
    assert "tests/root_tests/test_swift_plugin.py" not in result.output


def test_status_reports_exact_script_language_audit_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    previous_script = repo_info.path / "scripts" / "migrate_large_index_to_multi_repo.py"
    blocked_script = repo_info.path / "scripts" / "check_index_languages.py"
    previous_script.parent.mkdir(parents=True)
    previous_script.write_text(
        "class RepositoryMigration:\n"
        "    pass\n\n"
        "class LargeIndexMigrator:\n"
        "    def migrate_repository(self):\n"
        "        return 'ok'\n\n"
        "def main():\n"
        "    return LargeIndexMigrator()\n",
        encoding="utf-8",
    )
    blocked_script.write_text(
        "print('Checking languages')\n"
        "tables = ['files']\n"
        "for table in tables:\n"
        "    print(table)\n",
        encoding="utf-8",
    )
    later_file = repo_info.path / "docs" / "status" / "semantic_tail.md"
    later_file.parent.mkdir(parents=True)
    later_file.write_text("# Semantic tail\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T15:03:10Z",
                    "current_commit": "248dd68d06bced46adc2460c44fbcb2d38e72614",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(blocked_script),
                    "in_flight_path": str(later_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "scripts/migrate_large_index_to_multi_repo.py -> "
        "scripts/check_index_languages.py" in result.output
    )
    assert f"Last progress path: {blocked_script}" in result.output
    assert f"In-flight path: {later_file}" in result.output


def test_status_reports_exact_preflight_upgrade_script_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    preflight_script = repo_info.path / "scripts" / "preflight_upgrade.sh"
    protocol_script = repo_info.path / "scripts" / "test_mcp_protocol_direct.py"
    later_file = repo_info.path / "docs" / "status" / "semantic_tail.md"
    preflight_script.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    preflight_script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    protocol_script.write_text(
        "async def test_mcp_protocol():\n    return 'ok'\n", encoding="utf-8"
    )
    later_file.write_text("# Semantic tail\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T17:35:00Z",
                    "current_commit": "dbfd6ccd5577d104313110e40a83c28647385b58",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(protocol_script),
                    "in_flight_path": str(later_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded shell/Python indexing for "
        "scripts/preflight_upgrade.sh -> scripts/test_mcp_protocol_direct.py" in result.output
    )
    assert f"Last progress path: {protocol_script}" in result.output
    assert f"In-flight path: {later_file}" in result.output
    assert "scripts/validate_mcp_comprehensive.py" not in result.output
    assert "tests/root_tests/run_reranking_tests.py" not in result.output


def test_status_reports_exact_verify_simulator_script_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    verify_script = repo_info.path / "scripts" / "verify_embeddings.py"
    simulator_script = repo_info.path / "scripts" / "claude_code_behavior_simulator.py"
    later_file = repo_info.path / "docs" / "status" / "semantic_tail_2.md"
    verify_script.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    verify_script.write_text("def verify_embeddings():\n    return 'ok'\n", encoding="utf-8")
    simulator_script.write_text(
        "class ClaudeCodeSimulator:\n    def run_scenario(self):\n        return 'ok'\n",
        encoding="utf-8",
    )
    later_file.write_text("# Semantic tail 2\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T17:45:00Z",
                    "current_commit": "dbfd6ccd5577d104313110e40a83c28647385b58",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(simulator_script),
                    "in_flight_path": str(later_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "scripts/verify_embeddings.py -> scripts/claude_code_behavior_simulator.py" in result.output
    )
    assert f"Last progress path: {simulator_script}" in result.output
    assert f"In-flight path: {later_file}" in result.output
    assert "scripts/preflight_upgrade.sh" not in result.output
    assert "tests/root_tests/run_reranking_tests.py" not in result.output


def test_status_reports_exact_embed_consolidation_script_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    embed_script = repo_info.path / "scripts" / "create_semantic_embeddings.py"
    consolidation_script = repo_info.path / "scripts" / "consolidate_real_performance_data.py"
    later_file = repo_info.path / "docs" / "status" / "semantic_tail_3.md"
    embed_script.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    embed_script.write_text(
        "def get_repository_info():\n    return {}\n\ndef process_repository():\n    return {}\n",
        encoding="utf-8",
    )
    consolidation_script.write_text(
        "class ConsolidatedResult:\n    pass\n\nclass PerformanceDataConsolidator:\n    pass\n",
        encoding="utf-8",
    )
    later_file.write_text("# Semantic tail 3\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T18:05:00Z",
                    "current_commit": "82f8e44821ec14af93ef3bd5770e8ebf92b08961",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(consolidation_script),
                    "in_flight_path": str(later_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "scripts/create_semantic_embeddings.py -> "
        "scripts/consolidate_real_performance_data.py" in result.output
    )
    assert f"Last progress path: {consolidation_script}" in result.output
    assert f"In-flight path: {later_file}" in result.output
    assert "scripts/verify_embeddings.py" not in result.output
    assert "tests/root_tests/run_reranking_tests.py" not in result.output


def test_status_reports_exact_test_repo_index_script_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    schema_script = repo_info.path / "scripts" / "check_test_index_schema.py"
    ensure_script = repo_info.path / "scripts" / "ensure_test_repos_indexed.py"
    later_file = repo_info.path / "docs" / "status" / "semantic_tail_4.md"
    schema_script.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    schema_script.write_text(
        "def check_schema(db_path):\n    return db_path\n",
        encoding="utf-8",
    )
    ensure_script.write_text(
        "def check_index_exists(repo_info):\n    return bool(repo_info)\n",
        encoding="utf-8",
    )
    later_file.write_text("# Semantic tail 4\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T22:08:25Z",
                    "current_commit": "a0811d8a",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(ensure_script),
                    "in_flight_path": str(later_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "scripts/check_test_index_schema.py -> "
        "scripts/ensure_test_repos_indexed.py" in result.output
    )
    assert f"Last progress path: {ensure_script}" in result.output
    assert f"In-flight path: {later_file}" in result.output
    assert "scripts/consolidate_real_performance_data.py" not in result.output
    assert "tests/docs/test_p8_deployment_security.py" not in result.output


def test_status_reports_exact_missing_repo_semantic_script_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    missing_repo_script = repo_info.path / "scripts" / "index_missing_repos_semantic.py"
    working_indexes_script = repo_info.path / "scripts" / "identify_working_indexes.py"
    later_file = repo_info.path / "docs" / "status" / "semantic_tail_5.md"
    missing_repo_script.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    missing_repo_script.write_text(
        "def get_existing_collections():\n    return {}\n",
        encoding="utf-8",
    )
    working_indexes_script.write_text(
        "def get_repo_hash(repo_path):\n    return str(repo_path)\n",
        encoding="utf-8",
    )
    later_file.write_text("# Semantic tail 5\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T22:27:04Z",
                    "current_commit": "0195d3c",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(working_indexes_script),
                    "in_flight_path": str(later_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "scripts/index_missing_repos_semantic.py -> "
        "scripts/identify_working_indexes.py" in result.output
    )
    assert f"Last progress path: {working_indexes_script}" in result.output
    assert f"In-flight path: {later_file}" in result.output
    assert "scripts/check_test_index_schema.py" not in result.output
    assert "scripts/ensure_test_repos_indexed.py" not in result.output


def test_status_reports_exact_utility_verification_script_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    prepare_script = repo_info.path / "scripts" / "utilities" / "prepare_index_for_upload.py"
    verify_script = repo_info.path / "scripts" / "utilities" / "verify_tool_usage.py"
    later_file = repo_info.path / "docs" / "status" / "semantic_tail_6.md"
    prepare_script.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    prepare_script.write_text(
        "def get_repo_hash():\n    return 'hash'\n",
        encoding="utf-8",
    )
    verify_script.write_text(
        "class ToolUsageAnalyzer:\n    pass\n",
        encoding="utf-8",
    )
    later_file.write_text("# Semantic tail 6\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T22:44:15Z",
                    "current_commit": "d0732db6",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(verify_script),
                    "in_flight_path": str(later_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "scripts/utilities/prepare_index_for_upload.py -> "
        "scripts/utilities/verify_tool_usage.py" in result.output
    )
    assert f"Last progress path: {verify_script}" in result.output
    assert f"In-flight path: {later_file}" in result.output
    assert "scripts/index_missing_repos_semantic.py" not in result.output
    assert "scripts/identify_working_indexes.py" not in result.output


def test_status_reports_exact_qdrant_report_script_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    map_script = repo_info.path / "scripts" / "map_repos_to_qdrant.py"
    report_script = repo_info.path / "scripts" / "create_claude_code_aware_report.py"
    later_file = repo_info.path / "docs" / "status" / "semantic_tail_7.md"
    map_script.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    map_script.write_text(
        "def find_all_repositories():\n    return []\n",
        encoding="utf-8",
    )
    report_script.write_text(
        "def create_comprehensive_report():\n    return 'report'\n",
        encoding="utf-8",
    )
    later_file.write_text("# Semantic tail 7\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T23:02:25Z",
                    "current_commit": "490ad260",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(report_script),
                    "in_flight_path": str(later_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "scripts/map_repos_to_qdrant.py -> "
        "scripts/create_claude_code_aware_report.py" in result.output
    )
    assert f"Last progress path: {report_script}" in result.output
    assert f"In-flight path: {later_file}" in result.output
    assert "scripts/utilities/prepare_index_for_upload.py" not in result.output
    assert "scripts/utilities/verify_tool_usage.py" not in result.output


def test_status_reports_exact_optimized_upload_script_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    analysis_script = repo_info.path / "scripts" / "execute_optimized_analysis.py"
    upload_script = repo_info.path / "scripts" / "index-artifact-upload-v2.py"
    later_file = repo_info.path / "docs" / "status" / "semantic_tail_8.md"
    analysis_script.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    analysis_script.write_text(
        "class OptimizedAnalysisExecutor:\n    pass\n",
        encoding="utf-8",
    )
    upload_script.write_text(
        "class CompatibilityAwareUploader:\n    pass\n",
        encoding="utf-8",
    )
    later_file.write_text("# Semantic tail 8\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T23:19:22Z",
                    "current_commit": "2a439a39",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(upload_script),
                    "in_flight_path": str(later_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "scripts/execute_optimized_analysis.py -> "
        "scripts/index-artifact-upload-v2.py" in result.output
    )
    assert f"Last progress path: {upload_script}" in result.output
    assert f"In-flight path: {later_file}" in result.output
    assert "scripts/map_repos_to_qdrant.py" not in result.output
    assert "scripts/create_claude_code_aware_report.py" not in result.output


def test_status_reports_exact_edit_retrieval_script_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    analysis_script = repo_info.path / "scripts" / "analyze_claude_code_edits.py"
    retrieval_script = repo_info.path / "scripts" / "verify_mcp_retrieval.py"
    later_file = repo_info.path / "docs" / "status" / "semantic_tail_9.md"
    analysis_script.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    analysis_script.write_text(
        "class ClaudeCodeTranscriptAnalyzer:\n    pass\n",
        encoding="utf-8",
    )
    retrieval_script.write_text(
        "def verify_hybrid_search():\n    return {'mode': 'hybrid'}\n",
        encoding="utf-8",
    )
    later_file.write_text("# Semantic tail 9\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T23:37:37Z",
                    "current_commit": "2a439a39",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(retrieval_script),
                    "in_flight_path": str(later_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "scripts/analyze_claude_code_edits.py -> "
        "scripts/verify_mcp_retrieval.py" in result.output
    )
    assert f"Last progress path: {retrieval_script}" in result.output
    assert f"In-flight path: {later_file}" in result.output
    assert "scripts/execute_optimized_analysis.py" not in result.output
    assert "scripts/index-artifact-upload-v2.py" not in result.output


def test_status_reports_exact_comprehensive_query_full_sync_script_boundary(
    monkeypatch, tmp_path: Path
):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    query_script = repo_info.path / "scripts" / "run_comprehensive_query_test.py"
    semantic_script = repo_info.path / "scripts" / "index_all_repos_semantic_full.py"
    later_file = repo_info.path / "docs" / "status" / "semantic_tail_10.md"
    query_script.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    query_script.write_text(
        "class ParallelQueryTester:\n    pass\n",
        encoding="utf-8",
    )
    semantic_script.write_text(
        "def find_test_repositories():\n    return []\n",
        encoding="utf-8",
    )
    later_file.write_text("# Semantic tail 10\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-30T00:12:10Z",
                    "current_commit": "96bc35b0",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(semantic_script),
                    "in_flight_path": str(later_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "scripts/run_comprehensive_query_test.py -> "
        "scripts/index_all_repos_semantic_full.py" in result.output
    )
    assert f"Last progress path: {semantic_script}" in result.output
    assert f"In-flight path: {later_file}" in result.output
    assert "scripts/analyze_claude_code_edits.py" not in result.output
    assert "scripts/verify_mcp_retrieval.py" not in result.output


def test_status_reports_visualization_quick_charts_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    previous_file = repo_info.path / "mcp_server" / "visualization" / "__init__.py"
    blocked_file = repo_info.path / "mcp_server" / "visualization" / "quick_charts.py"
    previous_file.parent.mkdir(parents=True)
    previous_file.write_text("from .quick_charts import QuickCharts\n", encoding="utf-8")
    blocked_file.write_text("class QuickCharts:\n    pass\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T12:21:08Z",
                    "current_commit": "ec443d85edd902cdcc018d2103a334abe5235124",
                    "indexed_commit_before": "e2e9519858c3683c06b152c94a99e52098beaec6",
                    "last_progress_path": str(previous_file),
                    "in_flight_path": str(blocked_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {previous_file}" in result.output
    assert f"In-flight path: {blocked_file}" in result.output
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "mcp_server/visualization/quick_charts.py" in result.output
    )
    assert "scripts/validate_mcp_comprehensive.py" not in result.output


def test_status_reports_docs_governance_contract_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    previous_test = repo_info.path / "tests" / "docs" / "test_mre2e_evidence_contract.py"
    blocked_test = repo_info.path / "tests" / "docs" / "test_gagov_governance_contract.py"
    previous_test.parent.mkdir(parents=True)
    previous_test.write_text("def test_mre2e_contract():\n    assert True\n", encoding="utf-8")
    blocked_test.write_text("def test_gagov_contract():\n    assert True\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T14:13:50Z",
                    "current_commit": "9138e0b0",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(previous_test),
                    "in_flight_path": str(blocked_test),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {previous_test}" in result.output
    assert f"In-flight path: {blocked_test}" in result.output
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "tests/docs/test_mre2e_evidence_contract.py -> "
        "tests/docs/test_gagov_governance_contract.py" in result.output
    )
    assert "scripts/validate_mcp_comprehensive.py" not in result.output
    assert "mcp_server/visualization/quick_charts.py" not in result.output


def test_status_reports_docs_test_tail_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    previous_test = repo_info.path / "tests" / "docs" / "test_gaclose_evidence_closeout.py"
    blocked_test = repo_info.path / "tests" / "docs" / "test_p8_deployment_security.py"
    later_file = repo_info.path / "docs" / "status" / "semantic_tail_4.md"
    previous_test.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    previous_test.write_text(
        "def test_decision_artifact_links_prerequisite_evidence_and_verification():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    blocked_test.write_text(
        "def test_mcp_access_controls_subsection_present():\n" "    assert True\n",
        encoding="utf-8",
    )
    later_file.write_text("# Semantic tail 4\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T18:45:00Z",
                    "current_commit": "82f8e44821ec14af93ef3bd5770e8ebf92b08961",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(blocked_test),
                    "in_flight_path": str(later_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {blocked_test}" in result.output
    assert f"In-flight path: {later_file}" in result.output
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "tests/docs/test_gaclose_evidence_closeout.py -> "
        "tests/docs/test_p8_deployment_security.py" in result.output
    )
    assert "tests/docs/test_mre2e_evidence_contract.py" not in result.output
    assert "scripts/create_semantic_embeddings.py" not in result.output


def test_status_reports_mock_plugin_fixture_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    plugin_file = repo_info.path / "tests" / "security" / "fixtures" / "mock_plugin" / "plugin.py"
    init_file = repo_info.path / "tests" / "security" / "fixtures" / "mock_plugin" / "__init__.py"
    later_file = repo_info.path / "tests" / "security" / "test_plugin_sandbox.py"
    plugin_file.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True, exist_ok=True)
    plugin_file.write_text(
        "class Plugin:\n    def supports(self, path):\n        return True\n", encoding="utf-8"
    )
    init_file.write_text('from .plugin import Plugin\n\n__all__ = ["Plugin"]\n', encoding="utf-8")
    later_file.write_text("def test_sandbox_round_trip():\n    assert True\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T18:06:41Z",
                    "current_commit": "4133bfe",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(init_file),
                    "in_flight_path": str(later_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {init_file}" in result.output
    assert f"In-flight path: {later_file}" in result.output
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "tests/security/fixtures/mock_plugin/plugin.py -> "
        "tests/security/fixtures/mock_plugin/__init__.py" in result.output
    )
    assert "tests/docs/test_gaclose_evidence_closeout.py" not in result.output
    assert "scripts/create_semantic_embeddings.py" not in result.output


def test_status_reports_integration_obs_smoke_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    previous_test = repo_info.path / "tests" / "integration" / "__init__.py"
    blocked_test = repo_info.path / "tests" / "integration" / "obs" / "test_obs_smoke.py"
    later_file = repo_info.path / "docs" / "status" / "semantic_tail_11.md"
    blocked_test.parent.mkdir(parents=True, exist_ok=True)
    later_file.parent.mkdir(parents=True, exist_ok=True)
    previous_test.write_text('"""Integration tests for Code-Index-MCP."""\n', encoding="utf-8")
    blocked_test.write_text(
        "def test_secret_redaction_via_http():\n" "    assert True\n",
        encoding="utf-8",
    )
    later_file.write_text("# Semantic tail 11\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-30T00:46:51Z",
                    "current_commit": "c5170eff",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(previous_test),
                    "in_flight_path": str(blocked_test),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {previous_test}" in result.output
    assert f"In-flight path: {blocked_test}" in result.output
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "tests/integration/__init__.py -> "
        "tests/integration/obs/test_obs_smoke.py" in result.output
    )
    assert "tests/security/fixtures/mock_plugin/plugin.py" not in result.output
    assert "tests/security/fixtures/mock_plugin/__init__.py" not in result.output


def test_status_reports_centralization_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    previous_script = repo_info.path / "scripts" / "real_strategic_recommendations.py"
    blocked_script = repo_info.path / "scripts" / "migrate_to_centralized.py"
    later_file = repo_info.path / "docs" / "status" / "semantic_tail_12.md"
    previous_script.parent.mkdir(parents=True, exist_ok=True)
    later_file.parent.mkdir(parents=True, exist_ok=True)
    previous_script.write_text(
        "class RealStrategicRecommendationGenerator:\n" "    pass\n",
        encoding="utf-8",
    )
    blocked_script.write_text(
        "def migrate_repository_index(repo_path, dry_run=False):\n"
        "    return repo_path, dry_run\n",
        encoding="utf-8",
    )
    later_file.write_text("# Semantic tail 12\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-30T01:13:41Z",
                    "current_commit": "fd89efec",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(previous_script),
                    "in_flight_path": str(blocked_script),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {previous_script}" in result.output
    assert f"In-flight path: {blocked_script}" in result.output
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "scripts/real_strategic_recommendations.py -> "
        "scripts/migrate_to_centralized.py" in result.output
    )
    assert "tests/integration/__init__.py" not in result.output
    assert "tests/integration/obs/test_obs_smoke.py" not in result.output


def test_status_reports_reindex_demo_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    previous_script = repo_info.path / "scripts" / "reindex_current_repository.py"
    blocked_script = repo_info.path / "scripts" / "demo_centralized_indexes.py"
    later_file = repo_info.path / "docs" / "status" / "semantic_tail_13.md"
    previous_script.parent.mkdir(parents=True, exist_ok=True)
    later_file.parent.mkdir(parents=True, exist_ok=True)
    previous_script.write_text(
        "def reindex_current_repo():\n" "    return True\n",
        encoding="utf-8",
    )
    blocked_script.write_text(
        "def demo_centralized_storage():\n" "    return True\n",
        encoding="utf-8",
    )
    later_file.write_text("# Semantic tail 13\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-30T05:58:46Z",
                    "current_commit": "544d9844",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(previous_script),
                    "in_flight_path": str(blocked_script),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {previous_script}" in result.output
    assert f"In-flight path: {blocked_script}" in result.output
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "scripts/reindex_current_repository.py -> "
        "scripts/demo_centralized_indexes.py" in result.output
    )
    assert "plans/phase-plan-v7-SEMVERIFYSIMTAIL.md" not in result.output
    assert "plans/phase-plan-v7-SEMAPIDOCSTAIL.md" not in result.output


def test_status_reports_docs_contract_tail_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    previous_test = repo_info.path / "tests" / "docs" / "test_semincr_contract.py"
    blocked_test = repo_info.path / "tests" / "docs" / "test_gabase_ga_readiness_contract.py"
    later_file = repo_info.path / "ai_docs" / "qdrant.md"
    previous_test.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    previous_test.write_text(
        "def test_semincr_docs_freeze_incremental_mutation_contract():\n" "    assert True\n",
        encoding="utf-8",
    )
    blocked_test.write_text(
        "def test_public_docs_remain_pre_ga_and_route_to_canonical_artifacts():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    later_file.write_text("# Qdrant\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T18:52:55Z",
                    "current_commit": "3d627c33b3fefe90a5c801b6db5712d274402df9",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(previous_test),
                    "in_flight_path": str(blocked_test),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {previous_test}" in result.output
    assert f"In-flight path: {blocked_test}" in result.output
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "tests/docs/test_semincr_contract.py -> "
        "tests/docs/test_gabase_ga_readiness_contract.py" in result.output
    )
    assert "tests/docs/test_gaclose_evidence_closeout.py" not in result.output
    assert "tests/security/fixtures/mock_plugin/plugin.py" not in result.output


def test_status_reports_ga_release_docs_tail_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    previous_test = repo_info.path / "tests" / "docs" / "test_garc_rc_soak_contract.py"
    blocked_test = repo_info.path / "tests" / "docs" / "test_garel_ga_release_contract.py"
    later_file = repo_info.path / "ai_docs" / "qdrant.md"
    previous_test.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    previous_test.write_text(
        "def test_rc8_contract_surfaces_are_frozen():\n" "    assert True\n",
        encoding="utf-8",
    )
    blocked_test.write_text(
        "def test_final_decision_exists_and_cites_all_ga_inputs():\n" "    assert True\n",
        encoding="utf-8",
    )
    later_file.write_text("# Qdrant\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T19:13:19Z",
                    "current_commit": "2167f184",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(previous_test),
                    "in_flight_path": str(blocked_test),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {previous_test}" in result.output
    assert f"In-flight path: {blocked_test}" in result.output
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "tests/docs/test_garc_rc_soak_contract.py -> "
        "tests/docs/test_garel_ga_release_contract.py" in result.output
    )
    assert "tests/docs/test_gabase_ga_readiness_contract.py" not in result.output
    assert "tests/security/fixtures/mock_plugin/plugin.py" not in result.output


def test_status_reports_docs_truth_tail_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    previous_test = repo_info.path / "tests" / "docs" / "test_p23_doc_truth.py"
    blocked_test = repo_info.path / "tests" / "docs" / "test_semdogfood_evidence_contract.py"
    later_file = repo_info.path / "specs" / "phase-plans-v7.md"
    previous_test.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    previous_test.write_text(
        "def test_active_docs_do_not_contain_stale_strings():\n" "    assert True\n",
        encoding="utf-8",
    )
    blocked_test.write_text(
        "def test_semdogfood_report_exists_and_names_required_evidence_sections():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    later_file.write_text("# Phase roadmap v7\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T19:33:16Z",
                    "current_commit": "a3af755000000000000000000000000000000000",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(previous_test),
                    "in_flight_path": str(blocked_test),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {previous_test}" in result.output
    assert f"In-flight path: {blocked_test}" in result.output
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "tests/docs/test_p23_doc_truth.py -> "
        "tests/docs/test_semdogfood_evidence_contract.py" in result.output
    )
    assert "tests/docs/test_garc_rc_soak_contract.py" not in result.output
    assert "tests/security/fixtures/mock_plugin/plugin.py" not in result.output


def test_status_reports_p24_plugin_tail_python_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    previous_test = repo_info.path / "tests" / "test_p24_plugin_availability.py"
    blocked_test = repo_info.path / "tests" / "test_dispatcher_extension_gating.py"
    later_file = repo_info.path / "tests" / "docs" / "test_semincr_contract.py"
    previous_test.parent.mkdir(parents=True)
    later_file.parent.mkdir(parents=True)
    previous_test.write_text(
        "P24_FIELDS = {'language', 'state'}\n"
        "def test_availability_has_one_stable_row_per_supported_language():\n"
        "    assert P24_FIELDS\n",
        encoding="utf-8",
    )
    blocked_test.write_text(
        "REPO_ID = 'test-gating-repo'\n"
        "def test_py_extension_gating_no_plugin_instantiation():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    later_file.write_text("def test_semincr_contract():\n    assert True\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-30T04:41:42Z",
                    "current_commit": "c4419ef3",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(previous_test),
                    "in_flight_path": str(blocked_test),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert "Trace status: interrupted" in result.output
    assert f"Last progress path: {previous_test}" in result.output
    assert f"In-flight path: {blocked_test}" in result.output
    assert (
        "Lexical boundary: using exact bounded Python indexing for "
        "tests/test_p24_plugin_availability.py -> "
        "tests/test_dispatcher_extension_gating.py" in result.output
    )
    assert "tests/docs/test_semincr_contract.py" not in result.output


def test_status_reports_exact_devcontainer_json_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    config_file = repo_info.path / ".devcontainer" / "devcontainer.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text('{"name": "devcontainer"}\n', encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "running",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T10:00:00Z",
                    "current_commit": "abcdef123456",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(repo_info.path / ".devcontainer" / "post_create.sh"),
                    "in_flight_path": str(config_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded JSON indexing for "
        ".devcontainer/devcontainer.json" in result.output
    )
    assert (
        "Devcontainer tail pair: exact bounded shell-to-JSON handoff preserved lexical "
        "progress into .devcontainer/devcontainer.json" in result.output
    )
    assert (
        f"Last progress path: {repo_info.path / '.devcontainer' / 'post_create.sh'}"
        in result.output
    )
    assert f"In-flight path: {config_file}" in result.output


def test_status_reports_archive_tail_bounded_successor_when_in_flight_is_null(
    monkeypatch, tmp_path: Path
):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    prior_file = (
        repo_info.path
        / "analysis_archive"
        / "scripts_archive"
        / "scripts_test_files"
        / "verify_mcp_fix.py"
    )
    prior_file.parent.mkdir(parents=True)
    prior_file.write_text("def verify_fix():\n    return True\n", encoding="utf-8")
    archive_json = (
        repo_info.path / "analysis_archive" / "semantic_vs_sql_comparison_1750926162.json"
    )
    archive_json.write_text('{"comparison": "archive tail"}\n', encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T14:10:00Z",
                    "current_commit": "7282e34100000000000000000000000000000000",
                    "indexed_commit_before": "705a506f00000000000000000000000000000000",
                    "last_progress_path": str(archive_json),
                    "in_flight_path": None,
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded JSON indexing for "
        "analysis_archive/semantic_vs_sql_comparison_1750926162.json after "
        "analysis_archive/scripts_archive/scripts_test_files/verify_mcp_fix.py"
    ) in result.output
    assert f"Last progress path: {archive_json}" in result.output
    assert "In-flight path:" not in result.output
    assert (
        "Archive-tail successor: exact bounded JSON indexing preserved lexical progress "
        f"beyond {prior_file}"
    ) in result.output
    assert ".devcontainer/devcontainer.json" not in result.output
    assert "mcp_server/visualization/quick_charts.py" not in result.output
    assert "docs/benchmarks/production_benchmark.md" not in result.output


def test_status_reports_legacy_codex_phase_loop_compatibility_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    launch_file = (
        repo_info.path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260424T180441Z-01-gagov-execute"
        / "launch.json"
    )
    terminal_file = (
        repo_info.path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T071807Z-02-artpub-execute"
        / "terminal-summary.json"
    )
    archive_events = (
        repo_info.path / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "events.jsonl"
    )
    archive_state = (
        repo_info.path / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "state.json"
    )
    legacy_boundary_launch = (
        repo_info.path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260424T180441Z-01-gagov-execute"
        / "launch.json"
    )
    legacy_boundary_terminal = (
        repo_info.path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T071807Z-02-artpub-execute"
        / "terminal-summary.json"
    )
    for path in (
        launch_file,
        terminal_file,
        archive_events,
        archive_state,
        legacy_boundary_launch,
        legacy_boundary_terminal,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T18:26:11Z",
                    "current_commit": "96e0f26500000000000000000000000000000000",
                    "indexed_commit_before": "96e0f26500000000000000000000000000000000",
                    "last_progress_path": str(launch_file),
                    "in_flight_path": str(terminal_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {launch_file}" in result.output
    assert f"In-flight path: {terminal_file}" in result.output


def test_status_reports_later_legacy_codex_phase_loop_rebound_pair(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    launch_file = (
        repo_info.path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260424T190651Z-01-garc-plan"
        / "launch.json"
    )
    terminal_file = (
        repo_info.path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T075236Z-05-idxsafe-repair"
        / "terminal-summary.json"
    )
    archive_events = (
        repo_info.path / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "events.jsonl"
    )
    archive_state = (
        repo_info.path / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "state.json"
    )
    for path in (launch_file, terminal_file, archive_events, archive_state):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-30T00:30:24Z",
                    "current_commit": "230557ff00000000000000000000000000000000",
                    "indexed_commit_before": "230557ff00000000000000000000000000000000",
                    "last_progress_path": str(launch_file),
                    "in_flight_path": str(terminal_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {launch_file}" in result.output
    assert f"In-flight path: {terminal_file}" in result.output


def test_status_reports_legacy_codex_phase_loop_relapse_pair(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    terminal_file = (
        repo_info.path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T081107Z-08-ciflow-plan"
        / "terminal-summary.json"
    )
    launch_file = (
        repo_info.path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T081107Z-08-ciflow-plan"
        / "launch.json"
    )
    archive_events = (
        repo_info.path / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "events.jsonl"
    )
    archive_state = (
        repo_info.path / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "state.json"
    )
    for path in (terminal_file, launch_file, archive_events, archive_state):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-30T01:52:29Z",
                    "current_commit": "250dcd0f00000000000000000000000000000000",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(terminal_file),
                    "in_flight_path": str(launch_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {terminal_file}" in result.output
    assert f"In-flight path: {launch_file}" in result.output


def test_status_reports_legacy_codex_phase_loop_heartbeat_pair(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    launch_file = (
        repo_info.path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T071207Z-01-artpub-plan"
        / "launch.json"
    )
    heartbeat_file = (
        repo_info.path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T071207Z-01-artpub-plan"
        / "heartbeat.json"
    )
    archive_events = (
        repo_info.path / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "events.jsonl"
    )
    archive_state = (
        repo_info.path / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "state.json"
    )
    for path in (launch_file, heartbeat_file, archive_events, archive_state):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-30T02:53:44Z",
                    "current_commit": "d9d15fd500000000000000000000000000000000",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(launch_file),
                    "in_flight_path": str(heartbeat_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {launch_file}" in result.output
    assert f"In-flight path: {heartbeat_file}" in result.output
    assert ".phase-loop/runs/" not in result.output
    assert "run_comprehensive_query_test.py" not in result.output
    assert "index_all_repos_semantic_full.py" not in result.output


def test_status_reports_legacy_codex_phase_loop_garecut_heartbeat_pair(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    launch_file = (
        repo_info.path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260425T051448Z-01-garecut-execute"
        / "launch.json"
    )
    heartbeat_file = (
        repo_info.path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260425T051448Z-01-garecut-execute"
        / "heartbeat.json"
    )
    archive_events = (
        repo_info.path / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "events.jsonl"
    )
    archive_state = (
        repo_info.path / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "state.json"
    )
    for path in (launch_file, heartbeat_file, archive_events, archive_state):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-30T03:26:49Z",
                    "current_commit": "fd9de7cc00000000000000000000000000000000",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(launch_file),
                    "in_flight_path": str(heartbeat_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {launch_file}" in result.output
    assert f"In-flight path: {heartbeat_file}" in result.output
    assert "20260427T071207Z-01-artpub-plan" not in result.output
    assert "20260427T081107Z-08-ciflow-plan" not in result.output
    assert ".phase-loop/runs/" not in result.output


def test_status_reports_legacy_codex_phase_loop_ciflow_execute_relapse_pair(
    monkeypatch, tmp_path: Path
):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    terminal_file = (
        repo_info.path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T081704Z-09-ciflow-execute"
        / "terminal-summary.json"
    )
    launch_file = (
        repo_info.path
        / ".codex"
        / "phase-loop"
        / "runs"
        / "20260427T081704Z-09-ciflow-execute"
        / "launch.json"
    )
    archive_events = (
        repo_info.path / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "events.jsonl"
    )
    archive_state = (
        repo_info.path / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "state.json"
    )
    for path in (terminal_file, launch_file, archive_events, archive_state):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-30T04:05:03Z",
                    "current_commit": "ee2e04c606a9e7737dc875b4c25e9af685a96220",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(terminal_file),
                    "in_flight_path": str(launch_file),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert f"Last progress path: {terminal_file}" in result.output
    assert f"In-flight path: {launch_file}" in result.output
    assert ".phase-loop/runs/" not in result.output
    assert "20260424T190651Z-01-garc-plan" not in result.output
    assert "20260427T075236Z-05-idxsafe-repair" not in result.output


def test_status_reports_same_devcontainer_relapse_without_older_or_later_boundaries(
    monkeypatch, tmp_path: Path
):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    config_file = repo_info.path / ".devcontainer" / "devcontainer.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text('{"name": "devcontainer"}\n', encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T12:21:08Z",
                    "current_commit": "ec443d85edd902cdcc018d2103a334abe5235124",
                    "indexed_commit_before": "e2e9519858c3683c06b152c94a99e52098beaec6",
                    "last_progress_path": str(config_file),
                    "in_flight_path": None,
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert "Trace status: interrupted" in result.output
    assert "Trace stage: lexical_walking" in result.output
    assert f"Last progress path: {config_file}" in result.output
    assert "In-flight path:" not in result.output
    assert ".devcontainer/post_create.sh" not in result.output
    assert "tests/test_reindex_resume.py" not in result.output
    assert "scripts/validate_mcp_comprehensive.py" not in result.output
    assert "tests/root_tests/run_reranking_tests.py" not in result.output


def test_status_reports_test_workspace_boundary_with_post_devcontainer_later_path(
    monkeypatch, tmp_path: Path
):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    (repo_info.path / ".mcp-index-ignore").write_text(
        "fast_test_results/fast_report_*.md\ntest_workspace/\n",
        encoding="utf-8",
    )
    config_file = repo_info.path / ".devcontainer" / "devcontainer.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text('{"name": "devcontainer"}\n', encoding="utf-8")
    later_file = repo_info.path / "mcp_server" / "cli" / "repository_commands.py"
    later_file.parent.mkdir(parents=True)
    later_file.write_text("# later included file\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T12:47:18Z",
                    "current_commit": "26a163da52865a85c4f0c91e657c3f959e26b00e",
                    "indexed_commit_before": "869eea9302c687b7b4b496735de74e85a72e95f0",
                    "last_progress_path": str(later_file),
                    "in_flight_path": None,
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: fixture repositories under test_workspace/ are ignored during "
        "lexical walking" in result.output
    )
    assert (
        "Lexical boundary: using exact bounded JSON indexing for "
        ".devcontainer/devcontainer.json" in result.output
    )
    assert f"Last progress path: {later_file}" in result.output
    assert f"Last progress path: {config_file}" not in result.output


def test_status_reports_exact_jedi_markdown_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    jedi_doc = repo_info.path / "ai_docs" / "jedi.md"
    jedi_doc.parent.mkdir(parents=True)
    jedi_doc.write_text("# Jedi\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "running",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T11:00:00Z",
                    "current_commit": "abcdef123456",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(jedi_doc),
                    "in_flight_path": str(jedi_doc),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Markdown indexing for ai_docs/jedi.md"
        in result.output
    )
    assert f"Last progress path: {jedi_doc}" in result.output


def test_status_reports_later_ai_docs_overview_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    black_doc = repo_info.path / "ai_docs" / "black_isort_overview.md"
    sqlite_doc = repo_info.path / "ai_docs" / "sqlite_fts5_overview.md"
    black_doc.parent.mkdir(parents=True)
    black_doc.write_text("# Black & isort AI Context\n", encoding="utf-8")
    sqlite_doc.write_text("# SQLite FTS5 Comprehensive Guide for Code Indexing\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T21:50:51Z",
                    "current_commit": "abcdef123456",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(black_doc),
                    "in_flight_path": str(sqlite_doc),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using bounded Markdown indexing for ai_docs/*_overview.md"
        in result.output
    )
    assert f"Last progress path: {black_doc}" in result.output
    assert f"In-flight path: {sqlite_doc}" in result.output
    assert "tests/fixtures/multi_repo.py" not in result.output


def test_status_reports_exact_ai_docs_readme_tail_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    prometheus_doc = repo_info.path / "ai_docs" / "prometheus_overview.md"
    ai_docs_readme = repo_info.path / "ai_docs" / "README.md"
    jedi_doc = repo_info.path / "ai_docs" / "jedi.md"
    prometheus_doc.parent.mkdir(parents=True)
    prometheus_doc.write_text("# Prometheus Overview\n", encoding="utf-8")
    ai_docs_readme.write_text("# AI Documentation Index\n", encoding="utf-8")
    jedi_doc.write_text("# Jedi\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-30T06:17:40Z",
                    "current_commit": "abcdef123456",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(prometheus_doc),
                    "in_flight_path": str(ai_docs_readme),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Markdown indexing for "
        "ai_docs/prometheus_overview.md -> ai_docs/README.md" in result.output
    )
    assert (
        "Lexical boundary: using exact bounded Markdown indexing for ai_docs/jedi.md"
        in result.output
    )
    assert f"Last progress path: {prometheus_doc}" in result.output
    assert f"In-flight path: {ai_docs_readme}" in result.output


def test_status_reports_exact_validation_markdown_boundaries(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    ga_doc = repo_info.path / "docs" / "validation" / "ga-closeout-decision.md"
    mre2e_doc = repo_info.path / "docs" / "validation" / "mre2e-evidence.md"
    ga_doc.parent.mkdir(parents=True)
    ga_doc.write_text("# GA Closeout Decision\n", encoding="utf-8")
    mre2e_doc.write_text("# MRE2E Evidence\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T13:16:25Z",
                    "current_commit": "8f5c5f0a00000000000000000000000000000000",
                    "indexed_commit_before": "26a163da00000000000000000000000000000000",
                    "last_progress_path": str(ga_doc),
                    "in_flight_path": str(mre2e_doc),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Markdown indexing for "
        "docs/validation/ga-closeout-decision.md" in result.output
    )
    assert (
        "Lexical boundary: using exact bounded Markdown indexing for "
        "docs/validation/mre2e-evidence.md" in result.output
    )
    assert f"Last progress path: {ga_doc}" in result.output
    assert f"In-flight path: {mre2e_doc}" in result.output


def test_status_reports_exact_architecture_api_markdown_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    p2b_doc = repo_info.path / "docs" / "architecture" / "P2B-known-limits.md"
    api_doc = repo_info.path / "docs" / "api" / "API-REFERENCE.md"
    later_doc = repo_info.path / "docs" / "api" / "OTHER.md"
    in_flight_doc = repo_info.path / "docs" / "runbooks" / "ops.md"
    p2b_doc.parent.mkdir(parents=True)
    api_doc.parent.mkdir(parents=True)
    in_flight_doc.parent.mkdir(parents=True)
    p2b_doc.write_text("# P2B Known Limits\n", encoding="utf-8")
    api_doc.write_text("# API Reference\n", encoding="utf-8")
    later_doc.write_text("# Other API Notes\n", encoding="utf-8")
    in_flight_doc.write_text("# Ops\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-30T05:20:00Z",
                    "current_commit": "91b8f11300000000000000000000000000000000",
                    "indexed_commit_before": "4a31de0d00000000000000000000000000000000",
                    "last_progress_path": str(later_doc),
                    "in_flight_path": str(in_flight_doc),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Markdown indexing for "
        "docs/architecture/P2B-known-limits.md -> docs/api/API-REFERENCE.md" in result.output
    )
    assert f"Last progress path: {later_doc}" in result.output
    assert f"In-flight path: {in_flight_doc}" in result.output


def test_status_reports_exact_benchmark_markdown_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    rerun_doc = (
        repo_info.path
        / "docs"
        / "benchmarks"
        / "mcp_vs_native_benchmark_fullrepo_fireworks_qwen_voyage_local_iter5_rerun.md"
    )
    production_doc = repo_info.path / "docs" / "benchmarks" / "production_benchmark.md"
    rerun_doc.parent.mkdir(parents=True)
    rerun_doc.write_text("# MCP vs Native Benchmark\n", encoding="utf-8")
    production_doc.write_text("# Production Retrieval Benchmark\n", encoding="utf-8")
    later_doc = repo_info.path / "docs" / "status" / "semantic_tail.md"
    later_doc.parent.mkdir(parents=True)
    later_doc.write_text("# Semantic tail\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T13:34:05Z",
                    "current_commit": "705a506f00000000000000000000000000000000",
                    "indexed_commit_before": "26a163da00000000000000000000000000000000",
                    "last_progress_path": str(production_doc),
                    "in_flight_path": str(later_doc),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Markdown indexing for "
        "docs/benchmarks/mcp_vs_native_benchmark_fullrepo_fireworks_qwen_voyage_local_iter5_rerun.md "
        "-> docs/benchmarks/production_benchmark.md" in result.output
    )
    assert f"Last progress path: {production_doc}" in result.output
    assert f"In-flight path: {later_doc}" in result.output


def test_status_reports_exact_support_docs_markdown_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    toc_doc = repo_info.path / "docs" / "markdown-table-of-contents.md"
    support_doc = repo_info.path / "docs" / "SUPPORT_MATRIX.md"
    later_doc = repo_info.path / "ai_docs" / "qdrant.md"
    toc_doc.parent.mkdir(parents=True)
    later_doc.parent.mkdir(parents=True)
    toc_doc.write_text("# Documentation Table of Contents\n", encoding="utf-8")
    support_doc.write_text("# Support Matrix\n", encoding="utf-8")
    later_doc.write_text("# Qdrant\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T19:52:03Z",
                    "current_commit": "a7a71f2400000000000000000000000000000000",
                    "indexed_commit_before": "095d4fc800000000000000000000000000000000",
                    "last_progress_path": str(toc_doc),
                    "in_flight_path": str(support_doc),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Markdown indexing for "
        "docs/markdown-table-of-contents.md -> docs/SUPPORT_MATRIX.md" in result.output
    )
    assert f"Last progress path: {toc_doc}" in result.output
    assert f"In-flight path: {support_doc}" in result.output


def test_status_reports_exact_claude_command_markdown_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    execute_doc = repo_info.path / ".claude" / "commands" / "execute-lane.md"
    plan_doc = repo_info.path / ".claude" / "commands" / "plan-phase.md"
    later_doc = repo_info.path / "docs" / "status" / "semantic_tail.md"
    execute_doc.parent.mkdir(parents=True)
    execute_doc.write_text("# Execute Lane\n", encoding="utf-8")
    plan_doc.write_text("# Plan Phase\n", encoding="utf-8")
    later_doc.parent.mkdir(parents=True, exist_ok=True)
    later_doc.write_text("# Semantic tail\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T14:41:32Z",
                    "current_commit": "3dba956300000000000000000000000000000000",
                    "indexed_commit_before": "26a163da00000000000000000000000000000000",
                    "last_progress_path": str(execute_doc),
                    "in_flight_path": str(plan_doc),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Markdown indexing for "
        ".claude/commands/execute-lane.md -> .claude/commands/plan-phase.md" in result.output
    )
    assert f"Last progress path: {execute_doc}" in result.output
    assert f"In-flight path: {plan_doc}" in result.output


def test_status_reports_exact_late_v7_phase_plan_markdown_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    semcodexlooprelapsetail_doc = (
        repo_info.path / "plans" / "phase-plan-v7-SEMCODEXLOOPRELAPSETAIL.md"
    )
    semgareltail_doc = repo_info.path / "plans" / "phase-plan-v7-SEMGARELTAIL.md"
    later_doc = repo_info.path / "plans" / "phase-plan-v6-WATCH.md"
    semcodexlooprelapsetail_doc.parent.mkdir(parents=True)
    semcodexlooprelapsetail_doc.write_text("# SEMCODEXLOOPRELAPSETAIL\n", encoding="utf-8")
    semgareltail_doc.write_text("# SEMGARELTAIL\n", encoding="utf-8")
    later_doc.write_text("# WATCH\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T16:07:22Z",
                    "current_commit": "fe501b9700000000000000000000000000000000",
                    "indexed_commit_before": "e2e9519800000000000000000000000000000000",
                    "last_progress_path": str(semgareltail_doc),
                    "in_flight_path": str(later_doc),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Markdown indexing for "
        "plans/phase-plan-v7-SEMCODEXLOOPRELAPSETAIL.md -> "
        "plans/phase-plan-v7-SEMGARELTAIL.md" in result.output
    )
    assert f"Last progress path: {semgareltail_doc}" in result.output
    assert f"In-flight path: {later_doc}" in result.output


def test_status_reports_exact_rebound_phase_plan_markdown_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    verifysim_doc = repo_info.path / "plans" / "phase-plan-v7-SEMVERIFYSIMTAIL.md"
    apidocs_doc = repo_info.path / "plans" / "phase-plan-v7-SEMAPIDOCSTAIL.md"
    later_doc = repo_info.path / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"
    verifysim_doc.parent.mkdir(parents=True)
    later_doc.parent.mkdir(parents=True)
    verifysim_doc.write_text("# SEMVERIFYSIMTAIL\n", encoding="utf-8")
    apidocs_doc.write_text("# SEMAPIDOCSTAIL\n", encoding="utf-8")
    later_doc.write_text("# Evidence\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-30T05:36:47Z",
                    "current_commit": "e80ecc4600000000000000000000000000000000",
                    "indexed_commit_before": "d9db21d700000000000000000000000000000000",
                    "last_progress_path": str(verifysim_doc),
                    "in_flight_path": str(apidocs_doc),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Markdown indexing for "
        "plans/phase-plan-v7-SEMVERIFYSIMTAIL.md -> "
        "plans/phase-plan-v7-SEMAPIDOCSTAIL.md" in result.output
    )
    assert "plans/phase-plan-v7-SEMCODEXLOOPRELAPSETAIL.md ->" not in result.output
    assert f"Last progress path: {verifysim_doc}" in result.output
    assert f"In-flight path: {apidocs_doc}" in result.output


def test_status_reports_exact_historical_phase_plan_markdown_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    watch_doc = repo_info.path / "plans" / "phase-plan-v6-WATCH.md"
    p19_doc = repo_info.path / "plans" / "phase-plan-v1-p19.md"
    later_doc = repo_info.path / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"
    watch_doc.parent.mkdir(parents=True)
    later_doc.parent.mkdir(parents=True)
    watch_doc.write_text("# WATCH\n", encoding="utf-8")
    p19_doc.write_text("# P19\n", encoding="utf-8")
    later_doc.write_text("# Evidence\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T16:18:31Z",
                    "current_commit": "f1ee24f670000000000000000000000000000000",
                    "indexed_commit_before": "fe501b9700000000000000000000000000000000",
                    "last_progress_path": str(p19_doc),
                    "in_flight_path": str(later_doc),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Markdown indexing for "
        "plans/phase-plan-v6-WATCH.md -> plans/phase-plan-v1-p19.md" in result.output
    )
    assert f"Last progress path: {p19_doc}" in result.output
    assert f"In-flight path: {later_doc}" in result.output


def test_status_reports_exact_historical_v1_phase_plan_markdown_boundary(
    monkeypatch, tmp_path: Path
):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    p13_doc = repo_info.path / "plans" / "phase-plan-v1-p13.md"
    p3_doc = repo_info.path / "plans" / "phase-plan-v1-p3.md"
    later_doc = repo_info.path / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"
    p13_doc.parent.mkdir(parents=True)
    later_doc.parent.mkdir(parents=True)
    p13_doc.write_text("# P13\n", encoding="utf-8")
    p3_doc.write_text("# P3\n", encoding="utf-8")
    later_doc.write_text("# Evidence\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T20:12:44Z",
                    "current_commit": "e78c286100000000000000000000000000000000",
                    "indexed_commit_before": "a7a71f2400000000000000000000000000000000",
                    "last_progress_path": str(p13_doc),
                    "in_flight_path": str(p3_doc),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Markdown indexing for "
        "plans/phase-plan-v1-p13.md -> plans/phase-plan-v1-p3.md" in result.output
    )
    assert f"Last progress path: {p13_doc}" in result.output
    assert f"In-flight path: {p3_doc}" in result.output


def test_status_reports_exact_optimized_final_report_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    report_dir = repo_info.path / "final_optimized_report_final_report_1750958096"
    report_json = report_dir / "final_report_data.json"
    report_markdown = report_dir / "FINAL_OPTIMIZED_ANALYSIS_REPORT.md"
    later_script = repo_info.path / "scripts" / "generate_final_optimized_report.py"
    report_dir.mkdir(parents=True)
    later_script.parent.mkdir(parents=True)
    report_json.write_text(
        '{"business_metrics": {"time_reduction_percent": 81.0}}\n', encoding="utf-8"
    )
    report_markdown.write_text(
        "# Optimized Enhanced MCP Analysis - Final Report\n", encoding="utf-8"
    )
    later_script.write_text("def main():\n    return 0\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T21:15:52Z",
                    "current_commit": "e6584ee500000000000000000000000000000000",
                    "indexed_commit_before": "oldercommit",
                    "last_progress_path": str(report_json),
                    "in_flight_path": str(report_markdown),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: _semantic_preflight_ready(),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded JSON indexing for "
        "final_optimized_report_final_report_1750958096/final_report_data.json -> "
        "final_optimized_report_final_report_1750958096/FINAL_OPTIMIZED_ANALYSIS_REPORT.md"
    ) in result.output
    assert f"Last progress path: {report_json}" in result.output
    assert f"In-flight path: {report_markdown}" in result.output
    assert (
        "Optimized-report boundary: exact bounded JSON indexing preserved lexical progress "
        "into final_optimized_report_final_report_1750958096/FINAL_OPTIMIZED_ANALYSIS_REPORT.md"
    ) in result.output
    assert str(later_script) not in result.output
    assert ".devcontainer/devcontainer.json" not in result.output


def test_status_reports_exact_mixed_version_phase_plan_markdown_boundary(
    monkeypatch, tmp_path: Path
):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    semphasetail_doc = repo_info.path / "plans" / "phase-plan-v7-SEMPHASETAIL.md"
    gagov_doc = repo_info.path / "plans" / "phase-plan-v5-gagov.md"
    later_doc = repo_info.path / "ai_docs" / "celery_overview.md"
    in_flight_doc = repo_info.path / "ai_docs" / "qdrant.md"
    semphasetail_doc.parent.mkdir(parents=True)
    later_doc.parent.mkdir(parents=True)
    semphasetail_doc.write_text("# SEMPHASETAIL\n", encoding="utf-8")
    gagov_doc.write_text("# GAGOV\n", encoding="utf-8")
    later_doc.write_text("# Celery Overview\n", encoding="utf-8")
    in_flight_doc.write_text("# Qdrant\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T16:42:21Z",
                    "current_commit": "468dee1800000000000000000000000000000000",
                    "indexed_commit_before": "e2e9519800000000000000000000000000000000",
                    "last_progress_path": str(later_doc),
                    "in_flight_path": str(in_flight_doc),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Markdown indexing for "
        "plans/phase-plan-v7-SEMPHASETAIL.md -> plans/phase-plan-v5-gagov.md" in result.output
    )
    assert f"Last progress path: {later_doc}" in result.output
    assert f"In-flight path: {in_flight_doc}" in result.output


def test_status_reports_exact_semjedi_p4_phase_plan_markdown_boundary(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    repo_info = _repo_info(tmp_path)
    semjedi_doc = repo_info.path / "plans" / "phase-plan-v7-SEMJEDI.md"
    p4_doc = repo_info.path / "plans" / "phase-plan-v1-p4.md"
    later_doc = repo_info.path / "specs" / "phase-plans-v7.md"
    in_flight_doc = repo_info.path / "ai_docs" / "qdrant.md"
    semjedi_doc.parent.mkdir(parents=True)
    later_doc.parent.mkdir(parents=True)
    in_flight_doc.parent.mkdir(parents=True)
    semjedi_doc.write_text("# SEMJEDI\n", encoding="utf-8")
    p4_doc.write_text("# P4\n", encoding="utf-8")
    later_doc.write_text("# Roadmap\n", encoding="utf-8")
    in_flight_doc.write_text("# Qdrant\n", encoding="utf-8")

    class FakeRegistry:
        def get_repository_by_path(self, path):
            return repo_info

    class FakeStoreRegistry:
        @classmethod
        def for_registry(cls, registry):
            return object()

    class FakeRepoResolver:
        def __init__(self, registry, store_registry):
            pass

    class FakeIndexManager:
        def __init__(self, registry, repo_resolver=None, store_registry=None):
            pass

        def get_repository_status(self, repo_id):
            return {
                "repo_id": repo_info.repository_id,
                "name": repo_info.name,
                "path": repo_info.path,
                "current_commit": repo_info.current_commit,
                "last_indexed_commit": repo_info.last_indexed_commit,
                "last_indexed": repo_info.last_indexed,
                "needs_update": True,
                "auto_sync": repo_info.auto_sync,
                "artifact_enabled": repo_info.artifact_enabled,
                "artifact_backend": repo_info.artifact_backend,
                "artifact_health": repo_info.artifact_health,
                "index_exists": True,
                "index_size_mb": 0.1,
                "readiness": "stale_commit",
                "ready": False,
                "remediation": "Run reindex to update the repository index to the current commit.",
                "rollout_status": "partial_index_failure",
                "rollout_remediation": "A required lexical mutation failed.",
                "query_status": "index_unavailable",
                "query_remediation": 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".',
                "staleness_reason": "partial_index_failure",
                "semantic_readiness": "summaries_missing",
                "semantic_ready": False,
                "semantic_remediation": "Run semantic summary/vector generation for the current profile before semantic queries.",
                "force_full_exit_trace": {
                    "status": "interrupted",
                    "stage": "lexical_walking",
                    "stage_family": "lexical",
                    "trace_timestamp": "2026-04-29T20:38:17Z",
                    "current_commit": "dab8822a00000000000000000000000000000000",
                    "indexed_commit_before": "095d4fc800000000000000000000000000000000",
                    "last_progress_path": str(later_doc),
                    "in_flight_path": str(in_flight_doc),
                    "blocker_source": "lexical_mutation",
                },
                "features": {"semantic": {"readiness": {"evidence": {}}, "preflight": {}}},
            }

    monkeypatch.setattr("mcp_server.cli.repository_commands.RepositoryRegistry", FakeRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.StoreRegistry", FakeStoreRegistry)
    monkeypatch.setattr("mcp_server.cli.repository_commands.RepoResolver", FakeRepoResolver)
    monkeypatch.setattr("mcp_server.cli.repository_commands.GitAwareIndexManager", FakeIndexManager)
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.reload_settings",
        lambda: SimpleNamespace(
            get_semantic_default_profile=lambda: "oss_high",
            semantic_strict_mode=False,
        ),
    )
    monkeypatch.setattr(
        "mcp_server.cli.repository_commands.run_semantic_preflight",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "overall_ready": True,
                "can_write_semantic_vectors": True,
                "effective_config": {
                    "selected_profile": "oss_high",
                    "collection_name": "code_index__oss_high__v1",
                },
            }
        ),
    )

    result = runner.invoke(repository, ["status"])

    assert result.exit_code == 0
    assert (
        "Lexical boundary: using exact bounded Markdown indexing for "
        "plans/phase-plan-v7-SEMJEDI.md -> plans/phase-plan-v1-p4.md" in result.output
    )
    assert f"Last progress path: {later_doc}" in result.output
    assert f"In-flight path: {in_flight_doc}" in result.output
