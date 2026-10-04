"""P33 fail-closed multi-repo regression matrix."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from mcp_server.artifacts.artifact_download import IndexArtifactDownloader
from mcp_server.artifacts.multi_repo_artifact_coordinator import MultiRepoArtifactCoordinator
from mcp_server.cli import tool_handlers
from mcp_server.indexing.summarization import SummaryGenerationResult
from mcp_server.storage.multi_repo_manager import MultiRepositoryManager
from mcp_server.watcher.ref_poller import RefPoller
from tests.fixtures.multi_repo import (
    boot_test_server,
    build_production_matrix,
    build_temp_repo,
    git,
)


def _assert_index_unavailable(payload: dict, state: str) -> None:
    assert payload["error"] == "Index unavailable"
    assert payload["code"] == "index_unavailable"
    assert payload["safe_fallback"] == "native_search"
    assert payload["readiness"]["state"] == state
    assert payload["readiness"]["ready"] is False


def _assert_secondary_refusal(payload: dict, state: str, tool: str) -> None:
    assert payload["code"] == state
    assert payload["tool"] == tool
    assert payload["results"] == []
    assert payload["readiness"]["state"] == state
    assert payload["readiness"]["ready"] is False
    assert "safe_fallback" not in payload
    if tool == "reindex":
        assert payload["mutation_performed"] is False
    else:
        assert payload["persisted"] is False


def _assert_reindex_rebuild_required(payload: dict, state: str) -> None:
    assert payload["code"] == "full_rebuild_required"
    assert payload["readiness"]["state"] == state
    assert payload["mutation_performed"] is False


def _sqlite_symbol_paths(server, repo_id: str, symbol: str) -> list[str]:
    store = server.store_registry.get(repo_id)
    with store._get_connection() as conn:
        return [
            row[0]
            for row in conn.execute(
                """
                SELECT COALESCE(f.relative_path, f.path)
                FROM symbols s
                JOIN files f ON s.file_id = f.id
                WHERE s.name = ?
                  AND (f.is_deleted = 0 OR f.is_deleted IS NULL)
                ORDER BY f.relative_path
                """,
                (symbol,),
            ).fetchall()
        ]


def test_same_repo_linked_worktree_fails_closed_for_queries_and_reindex(tmp_path: Path):
    matrix = build_production_matrix(tmp_path)
    linked = matrix.linked_worktree()

    with boot_test_server(
        tmp_path,
        matrix.repos,
        extra_roots=[linked.worktree_path],
    ) as server:
        search = server.call_tool(
            "search_code",
            {
                "query": matrix.alpha.token,
                "repository": str(linked.worktree_path),
                "semantic": False,
            },
        )
        lookup = server.call_tool(
            "symbol_lookup",
            {"symbol": matrix.alpha.symbol, "repository": str(linked.worktree_path)},
        )
        reindex = server.call_tool("reindex", {"repository": str(linked.worktree_path)})
        write_summaries = server.call_tool(
            "write_summaries", {"repository": str(linked.worktree_path)}
        )
        summarize_sample = server.call_tool(
            "summarize_sample", {"repository": str(linked.worktree_path)}
        )

        _assert_index_unavailable(search, "unsupported_worktree")
        _assert_index_unavailable(lookup, "unsupported_worktree")
        _assert_secondary_refusal(reindex, "unsupported_worktree", "reindex")
        _assert_secondary_refusal(write_summaries, "unsupported_worktree", "write_summaries")
        _assert_secondary_refusal(summarize_sample, "unsupported_worktree", "summarize_sample")
        assert reindex["readiness"]["registered_path"] == str(linked.source_path.resolve())


def test_wrong_branch_stale_commit_and_missing_index_do_not_return_empty_results(
    tmp_path: Path,
):
    matrix = build_production_matrix(tmp_path)

    with boot_test_server(tmp_path, matrix.repos) as server:
        matrix.alpha.checkout_new_branch("p33-feature")
        server.registry.update_git_state(matrix.alpha.repo_id)
        wrong_branch = server.call_tool(
            "search_code",
            {
                "query": matrix.alpha.token,
                "repository": str(matrix.alpha.path),
                "semantic": False,
            },
        )
        _assert_index_unavailable(wrong_branch, "wrong_branch")

        matrix.alpha.checkout("main")
        server.registry.update_git_state(matrix.alpha.repo_id)
        matrix.alpha.write_file("stale.py", "def p33_stale_token():\n    return 'stale'\n")
        matrix.alpha.commit_all("make index stale")
        server.registry.update_git_state(matrix.alpha.repo_id)
        stale = server.call_tool(
            "symbol_lookup",
            {"symbol": "p33_stale_token", "repository": str(matrix.alpha.path)},
        )
        _assert_index_unavailable(stale, "stale_commit")

        server.seed_repo_index(matrix.alpha.repo_id, matrix.alpha.path)
        Path(matrix.alpha.path / ".mcp-index" / "current.db").unlink()
        missing = server.call_tool(
            "search_code",
            {
                "query": matrix.alpha.token,
                "repository": str(matrix.alpha.path),
                "semantic": False,
            },
        )
        _assert_index_unavailable(missing, "missing_index")


def test_wrong_branch_stale_commit_and_missing_index_refuse_secondary_tools(
    tmp_path: Path,
):
    matrix = build_production_matrix(tmp_path)

    with boot_test_server(tmp_path, matrix.repos) as server:
        matrix.alpha.checkout_new_branch("toolrdy-feature")
        server.registry.update_git_state(matrix.alpha.repo_id)
        for tool in ("reindex", "write_summaries", "summarize_sample"):
            payload = server.call_tool(tool, {"repository": str(matrix.alpha.path)})
            _assert_secondary_refusal(payload, "wrong_branch", tool)

        matrix.alpha.checkout("main")
        server.registry.update_git_state(matrix.alpha.repo_id)
        matrix.alpha.write_file("stale.py", "def toolrdy_stale_token():\n    return 'stale'\n")
        matrix.alpha.commit_all("make toolrdy stale")
        server.registry.update_git_state(matrix.alpha.repo_id)
        for tool in ("reindex", "write_summaries", "summarize_sample"):
            payload = server.call_tool(tool, {"repository": str(matrix.alpha.path)})
            if tool == "reindex":
                _assert_reindex_rebuild_required(payload, "stale_commit")
            else:
                _assert_secondary_refusal(payload, "stale_commit", tool)

        server.seed_repo_index(matrix.alpha.repo_id, matrix.alpha.path)
        Path(matrix.alpha.path / ".mcp-index" / "current.db").unlink()
        for tool in ("reindex", "write_summaries", "summarize_sample"):
            payload = server.call_tool(tool, {"repository": str(matrix.alpha.path)})
            if tool == "reindex":
                _assert_reindex_rebuild_required(payload, "missing_index")
            else:
                _assert_secondary_refusal(payload, "missing_index", tool)


def test_unregistered_repository_refuses_secondary_tools(tmp_path: Path):
    matrix = build_production_matrix(tmp_path)
    unregistered_repo, _ = build_temp_repo(
        tmp_path,
        "toolrdy_unregistered",
        seed_files={"unregistered.py": "def toolrdy_unregistered():\n    return 1\n"},
    )

    with boot_test_server(
        tmp_path,
        matrix.repos,
        extra_roots=[unregistered_repo],
    ) as server:
        for tool in ("reindex", "write_summaries", "summarize_sample"):
            payload = server.call_tool(tool, {"repository": str(unregistered_repo)})
            _assert_secondary_refusal(payload, "unregistered_repository", tool)


def test_wrong_branch_workspace_lifecycle_refuses_mutation_and_preserves_tracked_repo_state(
    monkeypatch,
    tmp_path: Path,
):
    matrix = build_production_matrix(tmp_path)
    registry_path = tmp_path / "registry.json"

    with boot_test_server(tmp_path, matrix.repos) as server:
        manager = MultiRepositoryManager(central_index_path=registry_path)
        before_commit = manager.get_repository_info(matrix.beta.repo_id).last_recovered_commit

        matrix.alpha.checkout_new_branch("mre2e-feature")
        server.registry.update_git_state(matrix.alpha.repo_id)
        manager = MultiRepositoryManager(central_index_path=registry_path)

        monkeypatch.setattr(
            "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactUploader.compress_indexes",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("publish should refuse")),
        )
        monkeypatch.setattr(
            "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactDownloader.download_latest",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fetch should refuse")),
        )

        coordinator = MultiRepoArtifactCoordinator(manager)
        publish = coordinator.publish_workspace([matrix.alpha.repo_id])
        fetch = coordinator.fetch_workspace([matrix.alpha.repo_id])

        assert publish[0].success is False
        assert publish[0].details["readiness"]["state"] == "wrong_branch"
        assert fetch[0].success is False
        assert fetch[0].details["readiness"]["state"] == "wrong_branch"

        after_beta = manager.get_repository_info(matrix.beta.repo_id)
        assert after_beta.last_recovered_commit == before_commit
        assert after_beta.current_branch == "main"


def test_summarize_sample_explicit_path_scope_matrix(tmp_path: Path):
    matrix = build_production_matrix(tmp_path)
    linked = matrix.linked_worktree()

    with boot_test_server(
        tmp_path,
        matrix.repos,
        extra_roots=[linked.worktree_path],
    ) as server:
        unsupported = server.call_tool(
            "summarize_sample",
            {"paths": [str(linked.worktree_path / "alpha.py")]},
        )
        conflict = server.call_tool(
            "summarize_sample",
            {
                "repository": str(matrix.alpha.path),
                "paths": [str(matrix.beta.path / "beta.py")],
            },
        )

        _assert_secondary_refusal(unsupported, "unsupported_worktree", "summarize_sample")
        assert conflict["code"] == "conflicting_path_and_repository"

        lazy_summarizer = MagicMock()
        lazy_summarizer.can_summarize.return_value = True
        lazy_summarizer._get_model_name.return_value = "test-model"
        batch_summarizer = MagicMock()
        batch_summarizer.summarize_file_chunks = AsyncMock(
            return_value=SummaryGenerationResult(files_attempted=1, files_summarized=1)
        )

        with patch(
            "mcp_server.indexing.summarization.FileBatchSummarizer",
            return_value=batch_summarizer,
        ):
            import asyncio
            import json

            result = asyncio.run(
                tool_handlers.handle_summarize_sample(
                    arguments={"paths": [str(matrix.alpha.path / "alpha.py")]},
                    dispatcher=server.dispatcher,
                    repo_resolver=server.repo_resolver,
                    lazy_summarizer=lazy_summarizer,
                )
            )
        ready_payload = json.loads(result[0].text)
        assert ready_payload["persisted"] is False
        assert ready_payload["files_processed"] == 1
        assert ready_payload["files"][0]["file_path"] == str(matrix.alpha.path / "alpha.py")


def test_incremental_repair_revert_rename_and_delete_match_durable_index(tmp_path: Path):
    matrix = build_production_matrix(tmp_path)

    with boot_test_server(tmp_path, matrix.repos) as server:
        renamed_token = "p33_alpha_renamed_token"
        matrix.alpha.write_file(
            "alpha.py",
            f"class {matrix.alpha.symbol}Renamed:\n"
            f"    marker = '{renamed_token}'\n\n"
            f"def {renamed_token}():\n"
            f"    return '{renamed_token}'\n",
        )
        matrix.alpha.rename_file("alpha.py", "renamed_alpha.py", "rename alpha evidence")
        server.seed_repo_index(matrix.alpha.repo_id, matrix.alpha.path)

        renamed = server.call_tool(
            "symbol_lookup",
            {"symbol": renamed_token, "repository": str(matrix.alpha.path)},
        )
        old_token = server.call_tool(
            "symbol_lookup",
            {"symbol": matrix.alpha.token, "repository": str(matrix.alpha.path)},
        )
        assert renamed["symbol"] == renamed_token
        assert "defined_in" in renamed, str(renamed)
        assert "renamed_alpha.py" in renamed["defined_in"]
        assert old_token["result"] == "not_found"

        matrix.alpha.delete_file("renamed_alpha.py", "delete renamed alpha evidence")
        server.seed_repo_index(matrix.alpha.repo_id, matrix.alpha.path)
        assert _sqlite_symbol_paths(server, matrix.alpha.repo_id, renamed_token) == []

        matrix.alpha.write_file(
            "renamed_alpha.py",
            f"class {matrix.alpha.symbol}Restored:\n"
            f"    marker = '{renamed_token}'\n\n"
            f"def {renamed_token}():\n"
            f"    return '{renamed_token}'\n",
        )
        matrix.alpha.commit_all("restore renamed alpha evidence")
        server.seed_repo_index(matrix.alpha.repo_id, matrix.alpha.path)
        assert _sqlite_symbol_paths(server, matrix.alpha.repo_id, renamed_token) == [
            "renamed_alpha.py"
        ]


def test_force_push_ref_movement_queues_full_rescan_instead_of_incremental_sync():
    repo = SimpleNamespace(
        repository_id="repo-p33",
        path="/tmp/p33-repo",
        tracked_branch="main",
        last_indexed_commit="old-sha",
    )
    manager = MagicMock()
    poller = RefPoller(
        registry=MagicMock(list_all=MagicMock(return_value=[repo])),
        git_index_manager=manager,
        dispatcher=MagicMock(),
        repo_resolver=MagicMock(),
        interval_seconds=60,
    )

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 1
        return result

    with (
        patch.object(poller, "_current_branch", return_value="main"),
        patch.object(poller, "_read_ref", return_value="new-sha"),
        patch("subprocess.run", side_effect=fake_run),
    ):
        poller._poll_one(repo)

    manager.enqueue_full_rescan.assert_called_once_with("repo-p33")
    manager.sync_repository_index.assert_not_called()


def test_wrong_artifact_identity_is_rejected_without_unsafe_override():
    downloader = IndexArtifactDownloader(repo="owner/repo")
    reasons = downloader.validate_artifact_identity(
        {
            "repo_id": "other-repo",
            "tracked_branch": "feature",
            "commit": "old-commit",
            "schema_version": "2",
            "semantic_profile_hash": "wrong-profile",
        },
        repo_id="expected-repo",
        tracked_branch="main",
        target_commit="new-commit",
        semantic_profile_hash="lexical-only",
    )

    assert any("repo_id mismatch" in reason for reason in reasons)
    assert any("tracked_branch mismatch" in reason for reason in reasons)
    assert any("commit mismatch" in reason for reason in reasons)
    assert any("semantic_profile_hash mismatch" in reason for reason in reasons)
