"""P27 tool-handler readiness plumbing tests."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mcp_server.health.repository_readiness import (
    RepositoryReadiness,
    RepositoryReadinessState,
    SemanticReadiness,
    SemanticReadinessState,
)
from mcp_server.storage.multi_repo_manager import RepositoryInfo


def _run(coro):
    return asyncio.run(coro)


def _parsed(result) -> dict:
    return json.loads(result[0].text)


def _unsupported(path: Path) -> RepositoryReadiness:
    return RepositoryReadiness(
        state=RepositoryReadinessState.UNSUPPORTED_WORKTREE,
        repository_id="repo-1",
        repository_name="repo",
        registered_path=str(path.parent / "registered"),
        requested_path=str(path),
        remediation="Use the registered path or unregister it before registering another worktree.",
    )


class FakeResolver:
    def __init__(self, readiness: RepositoryReadiness):
        self.readiness = readiness
        self.resolve_called = False

    def classify(self, path: Path) -> RepositoryReadiness:
        return self.readiness

    def resolve(self, path: Path):
        self.resolve_called = True
        return None


def _semantic_not_ready() -> SemanticReadiness:
    return SemanticReadiness(
        state=SemanticReadinessState.SUMMARIES_MISSING,
        profile_id="oss_high",
        remediation="Run semantic summary/vector generation for the current profile before semantic queries.",
    )


def test_search_code_exposes_unsupported_worktree_without_dispatch(tmp_path, monkeypatch):
    from mcp_server.cli.tool_handlers import handle_search_code

    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    dispatcher = MagicMock()
    resolver = FakeResolver(_unsupported(worktree))

    result = _run(
        handle_search_code(
            arguments={"query": "foo", "repository": str(worktree)},
            dispatcher=dispatcher,
            repo_resolver=resolver,
        )
    )

    data = _parsed(result)
    assert data["code"] == "index_unavailable"
    assert data["safe_fallback"] == "native_search"
    assert data["readiness"]["state"] == "unsupported_worktree"
    dispatcher.search.assert_not_called()
    assert resolver.resolve_called is False


def test_symbol_lookup_exposes_unsupported_worktree_without_dispatch(tmp_path, monkeypatch):
    from mcp_server.cli.tool_handlers import handle_symbol_lookup

    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    dispatcher = MagicMock()
    resolver = FakeResolver(_unsupported(worktree))

    result = _run(
        handle_symbol_lookup(
            arguments={"symbol": "Thing", "repository": str(worktree)},
            dispatcher=dispatcher,
            repo_resolver=resolver,
        )
    )

    data = _parsed(result)
    assert data["code"] == "index_unavailable"
    assert data["safe_fallback"] == "native_search"
    assert data["readiness"]["state"] == "unsupported_worktree"
    dispatcher.lookup.assert_not_called()
    assert resolver.resolve_called is False


def test_reindex_exposes_unsupported_worktree_without_dispatch(tmp_path, monkeypatch):
    from mcp_server.cli.tool_handlers import handle_reindex

    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    dispatcher = MagicMock()
    resolver = FakeResolver(_unsupported(worktree))

    result = _run(
        handle_reindex(
            arguments={"path": str(worktree)},
            dispatcher=dispatcher,
            repo_resolver=resolver,
        )
    )

    data = _parsed(result)
    assert data["code"] == "unsupported_worktree"
    dispatcher.index_directory.assert_not_called()
    assert resolver.resolve_called is False


def test_get_status_repository_rows_include_readiness(tmp_path):
    from mcp_server.cli.tool_handlers import handle_get_status
    from mcp_server.dispatcher.simple_dispatcher import SimpleDispatcher

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    index_path = repo_path / ".mcp-index" / "current.db"
    index_path.parent.mkdir()
    index_path.touch()
    info = RepositoryInfo(
        repository_id="repo-1",
        name="repo",
        path=repo_path,
        index_path=index_path,
        language_stats={},
        total_files=0,
        total_symbols=0,
        indexed_at=datetime.now(),
        tracked_branch="main",
        git_common_dir=str(repo_path / ".git"),
    )
    registry = MagicMock()
    registry.get_all_repositories.return_value = {"repo-1": info}
    resolver = MagicMock()
    resolver._registry = registry

    result = _run(
        handle_get_status(
            arguments={},
            dispatcher=SimpleDispatcher(),
            repo_resolver=resolver,
        )
    )

    repos = _parsed(result)["repositories"]
    assert repos[0]["readiness"] == "missing_schema"
    assert repos[0]["ready"] is False
    assert repos[0]["readiness_code"] == "missing_schema"
    assert repos[0]["semantic_readiness"] == "enrichment_unavailable"
    assert repos[0]["semantic_ready"] is False


def test_summarize_sample_consumes_named_result_fields(tmp_path, monkeypatch):
    from mcp_server.cli.tool_handlers import handle_summarize_sample
    from mcp_server.indexing.summarization import (
        FileBatchSummarizer,
        GeneratedSummary,
        SummaryGenerationResult,
    )
    from mcp_server.storage.sqlite_store import SQLiteStore

    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    source = tmp_path / "sample.py"
    source.write_text("def sample():\n    return 1\n", encoding="utf-8")
    store = SQLiteStore(str(tmp_path / "index.db"))
    repository_row = store.ensure_repository_row(tmp_path, name="repo")
    file_id = store.store_file(
        repository_row,
        path=source,
        relative_path="sample.py",
        language="python",
    )
    store.store_chunk(
        file_id=file_id,
        content=source.read_text(encoding="utf-8"),
        content_start=0,
        content_end=27,
        line_start=1,
        line_end=2,
        chunk_id="chunk-1",
        node_id="node-1",
        treesitter_file_id="tree-1",
        language="python",
        node_type="function_definition",
    )
    readiness = RepositoryReadiness(
        state=RepositoryReadinessState.READY,
        repository_id="repo-1",
        registered_path=str(tmp_path),
        requested_path=str(source),
    )
    resolver = MagicMock()
    resolver.classify.return_value = readiness
    resolver.resolve.return_value = SimpleNamespace(
        repo_id="repo-1",
        sqlite_store=store,
        workspace_root=tmp_path,
    )
    lazy_summarizer = MagicMock()
    lazy_summarizer.can_summarize.return_value = True
    lazy_summarizer._get_model_name.return_value = "fake-model"

    async def fake_summarize(*_args, **_kwargs):
        return SummaryGenerationResult(
            chunks_attempted=2,
            summaries_written=1,
            authoritative_chunks=1,
            missing_chunk_ids=("chunk-2",),
            remaining_chunks=1,
            scope_drained=False,
            summaries=(GeneratedSummary("chunk-1", "Generated summary"),),
        )

    monkeypatch.setattr(FileBatchSummarizer, "summarize_file_chunks", fake_summarize)

    result = _run(
        handle_summarize_sample(
            arguments={"paths": [str(source)]},
            dispatcher=MagicMock(),
            repo_resolver=resolver,
            lazy_summarizer=lazy_summarizer,
        )
    )

    payload = _parsed(result)
    assert payload["total_chunks"] == 1
    assert payload["files"][0]["summaries_written"] == 1
    assert payload["files"][0]["missing_chunk_ids"] == ["chunk-2"]
    assert payload["files"][0]["scope_drained"] is False


def test_search_code_semantic_not_ready_returns_semantic_metadata(tmp_path, monkeypatch):
    import mcp_server.health.repository_readiness as readiness_module
    from mcp_server.cli.tool_handlers import handle_search_code

    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    dispatcher = MagicMock()
    ctx = MagicMock()
    ctx.registry_entry = SimpleNamespace(path=worktree)
    ctx.sqlite_store = MagicMock()
    ctx.sqlite_store.get_semantic_readiness_evidence.return_value = {
        "profile_id": "oss_high",
        "collection": "code_index__oss_high__v1",
        "total_chunks": 1,
        "summary_count": 0,
        "missing_summaries": 1,
        "vector_link_count": 0,
        "missing_vectors": 1,
        "matching_collection_links": 0,
        "collection_mismatches": 0,
    }
    resolver = FakeResolver(
        RepositoryReadiness(
            state=RepositoryReadinessState.READY,
            repository_id="repo-1",
            repository_name="repo",
            requested_path=str(worktree),
        )
    )
    resolver.resolve = lambda _path: ctx
    monkeypatch.setattr(
        readiness_module,
        "_current_semantic_profile",
        lambda: MagicMock(
            profile_id="oss_high",
            compatibility_fingerprint="fingerprint-1",
            vector_dimension=4096,
            build_metadata={"collection_name": "code_index__oss_high__v1"},
        ),
    )

    result = _run(
        handle_search_code(
            arguments={"query": "semantic intent", "repository": str(worktree), "semantic": True},
            dispatcher=dispatcher,
            repo_resolver=resolver,
        )
    )

    data = _parsed(result)
    assert data["code"] == "semantic_not_ready"
    assert data["semantic_requested"] is True
    assert data["semantic_source"] == "semantic"
    assert data["semantic_profile_id"] == "oss_high"
    assert data["semantic_collection_name"] == "code_index__oss_high__v1"
    assert data["semantic_fallback_status"] == "refused_not_ready"
    assert data["semantic_readiness"]["state"] == "summaries_missing"
    dispatcher.search.assert_not_called()


def test_search_code_semantic_not_ready_preserves_state_specific_metadata(tmp_path, monkeypatch):
    import mcp_server.health.repository_readiness as readiness_module
    from mcp_server.cli.tool_handlers import handle_search_code

    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    states = {
        "vectors_missing": {
            "state": "vectors_missing",
            "vector_link_count": 0,
            "missing_vectors": 1,
            "collection_mismatches": 0,
        },
        "profile_mismatch": {
            "state": "profile_mismatch",
            "vector_link_count": 1,
            "missing_vectors": 0,
            "collection_mismatches": 1,
        },
        "vector_dimension_mismatch": {
            "state": "vector_dimension_mismatch",
            "vector_link_count": 1,
            "missing_vectors": 0,
            "collection_mismatches": 0,
            "discovered_dimension": 1024,
        },
    }

    monkeypatch.setattr(
        readiness_module,
        "_current_semantic_profile",
        lambda: MagicMock(
            profile_id="oss_high",
            compatibility_fingerprint="fingerprint-1",
            vector_dimension=4096,
            build_metadata={"collection_name": "code_index__oss_high__v1"},
        ),
    )

    for expected_state, evidence_overrides in states.items():
        dispatcher = MagicMock()
        ctx = MagicMock()
        ctx.registry_entry = SimpleNamespace(path=worktree)
        ctx.sqlite_store = MagicMock()
        ctx.sqlite_store.get_semantic_readiness_evidence.return_value = {
            "profile_id": "oss_high",
            "collection": "code_index__oss_high__v1",
            "total_chunks": 1,
            "summary_count": 1,
            "missing_summaries": 0,
            "vector_link_count": evidence_overrides.get("vector_link_count", 1),
            "missing_vectors": evidence_overrides.get("missing_vectors", 0),
            "matching_collection_links": 1,
            "collection_mismatches": evidence_overrides.get("collection_mismatches", 0),
        }
        resolver = FakeResolver(
            RepositoryReadiness(
                state=RepositoryReadinessState.READY,
                repository_id="repo-1",
                repository_name="repo",
                requested_path=str(worktree),
            )
        )
        resolver.resolve = lambda _path, ctx=ctx: ctx

        metadata = {
            "semantic_profile": "oss_high",
            "semantic_profiles": {
                "oss_high": {
                    "compatibility_fingerprint": "fingerprint-1",
                    "collection_name": "code_index__oss_high__v1",
                    "model_dimension": evidence_overrides.get("discovered_dimension", 4096),
                }
            },
        }
        (worktree / ".index_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

        result = _run(
            handle_search_code(
                arguments={
                    "query": "semantic intent",
                    "repository": str(worktree),
                    "semantic": True,
                },
                dispatcher=dispatcher,
                repo_resolver=resolver,
            )
        )

        data = _parsed(result)
        assert data["code"] == "semantic_not_ready"
        assert data["semantic_readiness"]["state"] == expected_state
        assert data["semantic_profile_id"] == "oss_high"
        assert data["semantic_collection_name"] == "code_index__oss_high__v1"
        dispatcher.search.assert_not_called()


def test_search_code_semantic_ready_returns_metadata_envelope(tmp_path, monkeypatch):
    import mcp_server.health.repository_readiness as readiness_module
    from mcp_server.cli.tool_handlers import handle_search_code

    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    dispatcher = MagicMock()
    dispatcher.search.return_value = [
        {
            "file": "mcp_server/utils/semantic_indexer.py",
            "line": 42,
            "snippet": "class SemanticIndexer:",
            "semantic_source": "semantic",
            "semantic_profile_id": "oss_high",
            "semantic_collection_name": "code_index__oss_high__v1",
        }
    ]
    ctx = MagicMock()
    ctx.registry_entry = SimpleNamespace(path=worktree)
    ctx.sqlite_store = MagicMock()
    ctx.sqlite_store.get_semantic_readiness_evidence.return_value = {
        "profile_id": "oss_high",
        "collection": "code_index__oss_high__v1",
        "total_chunks": 1,
        "summary_count": 1,
        "missing_summaries": 0,
        "vector_link_count": 1,
        "missing_vectors": 0,
        "matching_collection_links": 1,
        "collection_mismatches": 0,
    }
    resolver = FakeResolver(
        RepositoryReadiness(
            state=RepositoryReadinessState.READY,
            repository_id="repo-1",
            repository_name="repo",
            requested_path=str(worktree),
        )
    )
    resolver.resolve = lambda _path: ctx
    monkeypatch.setattr(
        readiness_module,
        "_current_semantic_profile",
        lambda: MagicMock(
            profile_id="oss_high",
            compatibility_fingerprint="fingerprint-1",
            vector_dimension=4096,
            build_metadata={"collection_name": "code_index__oss_high__v1"},
        ),
    )
    (worktree / ".index_metadata.json").write_text(
        json.dumps(
            {
                "semantic_profile": "oss_high",
                "semantic_profiles": {
                    "oss_high": {
                        "compatibility_fingerprint": "fingerprint-1",
                        "collection_name": "code_index__oss_high__v1",
                        "model_dimension": 4096,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = _run(
        handle_search_code(
            arguments={
                "query": "class SemanticIndexer",
                "repository": str(worktree),
                "semantic": True,
            },
            dispatcher=dispatcher,
            repo_resolver=resolver,
        )
    )

    data = _parsed(result)
    assert data["semantic_requested"] is True
    assert data["semantic_source"] == "semantic"
    assert data["semantic_profile_id"] == "oss_high"
    assert data["semantic_collection_name"] == "code_index__oss_high__v1"
    assert data["semantic_fallback_status"] == "not_attempted"
    assert data["results"][0]["semantic_source"] == "semantic"


def test_search_code_semantic_runtime_failure_returns_explicit_failure(tmp_path, monkeypatch):
    import mcp_server.health.repository_readiness as readiness_module
    from mcp_server.cli.tool_handlers import handle_search_code
    from mcp_server.dispatcher.dispatcher_enhanced import SemanticSearchFailure

    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    dispatcher = MagicMock()
    dispatcher.search.side_effect = SemanticSearchFailure(
        "semantic runtime boom",
        profile_id="oss_high",
        collection_name="code_index__oss_high__v1",
    )
    ctx = MagicMock()
    ctx.registry_entry = SimpleNamespace(path=worktree)
    ctx.sqlite_store = MagicMock()
    ctx.sqlite_store.get_semantic_readiness_evidence.return_value = {
        "profile_id": "oss_high",
        "collection": "code_index__oss_high__v1",
        "total_chunks": 1,
        "summary_count": 1,
        "missing_summaries": 0,
        "vector_link_count": 1,
        "missing_vectors": 0,
        "matching_collection_links": 1,
        "collection_mismatches": 0,
    }
    resolver = FakeResolver(
        RepositoryReadiness(
            state=RepositoryReadinessState.READY,
            repository_id="repo-1",
            repository_name="repo",
            requested_path=str(worktree),
        )
    )
    resolver.resolve = lambda _path: ctx
    monkeypatch.setattr(
        readiness_module,
        "_current_semantic_profile",
        lambda: MagicMock(
            profile_id="oss_high",
            compatibility_fingerprint="fingerprint-1",
            vector_dimension=4096,
            build_metadata={"collection_name": "code_index__oss_high__v1"},
        ),
    )
    (worktree / ".index_metadata.json").write_text(
        json.dumps(
            {
                "semantic_profile": "oss_high",
                "semantic_profiles": {
                    "oss_high": {
                        "compatibility_fingerprint": "fingerprint-1",
                        "collection_name": "code_index__oss_high__v1",
                        "model_dimension": 4096,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = _run(
        handle_search_code(
            arguments={
                "query": "class SemanticIndexer",
                "repository": str(worktree),
                "semantic": True,
            },
            dispatcher=dispatcher,
            repo_resolver=resolver,
        )
    )

    data = _parsed(result)
    assert data["code"] == "semantic_search_failed"
    assert data["semantic_requested"] is True
    assert data["semantic_fallback_status"] == "failed_runtime"
    assert data["semantic_profile_id"] == "oss_high"
    assert data["semantic_collection_name"] == "code_index__oss_high__v1"


def test_search_code_explicit_lexical_request_keeps_legacy_shape(tmp_path, monkeypatch):
    from mcp_server.cli.tool_handlers import handle_search_code

    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    dispatcher = MagicMock()
    dispatcher.search.return_value = [
        {
            "file": "mcp_server/utils/semantic_indexer.py",
            "line": 42,
            "snippet": "class SemanticIndexer:",
        }
    ]
    ctx = MagicMock()
    ctx.registry_entry = SimpleNamespace(path=worktree)
    resolver = FakeResolver(
        RepositoryReadiness(
            state=RepositoryReadinessState.READY,
            repository_id="repo-1",
            repository_name="repo",
            requested_path=str(worktree),
        )
    )
    resolver.resolve = lambda _path: ctx

    result = _run(
        handle_search_code(
            arguments={"query": "SemanticIndexer", "repository": str(worktree), "semantic": False},
            dispatcher=dispatcher,
            repo_resolver=resolver,
        )
    )

    data = _parsed(result)
    assert isinstance(data, list)
    assert "semantic_requested" not in data[0]


def test_reindex_reports_additive_semantic_stage_metadata(tmp_path, monkeypatch):
    from mcp_server.cli.tool_handlers import handle_reindex

    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    worktree = tmp_path / "repo"
    worktree.mkdir()
    dispatcher = MagicMock()
    dispatcher.index_directory.return_value = {
        "indexed_files": 1,
        "ignored_files": 0,
        "failed_files": 0,
        "total_files": 1,
        "by_language": {"python": 1},
        "summaries_written": 1,
        "summary_chunks_attempted": 2,
        "summary_missing_chunks": 1,
        "semantic_indexed": 0,
        "semantic_failed": 0,
        "semantic_skipped": 0,
        "semantic_blocked": 1,
        "semantic_stage": "blocked_missing_summaries",
        "semantic_error": "Missing authoritative summaries blocked strict semantic indexing",
    }
    ctx = MagicMock()
    ctx.workspace_root = worktree
    ctx.sqlite_store = MagicMock()
    ctx.sqlite_store.db_path = str(tmp_path / "index.db")
    ctx.sqlite_store.rebuild_fts_code.return_value = 1
    ctx.staging = False
    ctx.sqlite_store.path_resolver.normalize_path.return_value = "."
    ctx.sqlite_store._get_connection.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (
        1,
    )
    resolver = FakeResolver(
        RepositoryReadiness(
            state=RepositoryReadinessState.READY,
            repository_id="repo-1",
            repository_name="repo",
            requested_path=str(worktree),
        )
    )
    resolver.resolve = lambda _path: ctx

    result = _run(
        handle_reindex(
            arguments={"path": str(worktree)},
            dispatcher=dispatcher,
            repo_resolver=resolver,
        )
    )

    data = _parsed(result)
    assert data.get("summaries_written") == 1, data
    assert data["summary_chunks_attempted"] == 2
    assert data["summary_missing_chunks"] == 1
    assert data["semantic_blocked"] == 1
    assert data["semantic_stage"] == "blocked_missing_summaries"


def test_reindex_single_file_success_returns_object_payload(tmp_path, monkeypatch):
    from mcp_server.cli.tool_handlers import handle_reindex
    from mcp_server.dispatcher.dispatcher_enhanced import IndexResult, IndexResultStatus

    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    worktree = tmp_path / "repo"
    worktree.mkdir()
    source_file = worktree / "demo.py"
    source_file.write_text("def demo():\n    return 1\n", encoding="utf-8")

    dispatcher = MagicMock()
    dispatcher.index_file.return_value = IndexResult(
        IndexResultStatus.INDEXED, source_file, None, None
    )
    ctx = MagicMock()
    ctx.workspace_root = worktree
    ctx.sqlite_store = MagicMock()
    ctx.staging = False
    ctx.sqlite_store.path_resolver.normalize_path.return_value = "demo.py"
    ctx.sqlite_store._get_connection.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (
        1,
    )
    resolver = FakeResolver(
        RepositoryReadiness(
            state=RepositoryReadinessState.READY,
            repository_id="repo-1",
            repository_name="repo",
            requested_path=str(worktree),
        )
    )
    resolver.resolve = lambda _path: ctx

    result = _run(
        handle_reindex(
            arguments={"path": str(source_file)},
            dispatcher=dispatcher,
            repo_resolver=resolver,
        )
    )

    data = _parsed(result)
    assert data.get("path") == str(source_file), data
    assert data["mode"] == "file"
    assert data["indexed_files"] == 1
    assert data["mutation_performed"] is True
    assert "Reindexed file:" in data["message"]


@pytest.mark.parametrize("failure", ["raise", "error", "semantic_failed", "semantic_blocked"])
def test_reindex_single_file_failure_returns_structured_error(tmp_path, monkeypatch, failure):
    from mcp_server.cli.tool_handlers import handle_reindex
    from mcp_server.dispatcher.dispatcher_enhanced import IndexResult, IndexResultStatus

    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    worktree = tmp_path / "repo"
    worktree.mkdir()
    source_file = worktree / "broken.py"
    source_file.write_text("def broken():\n    return 1\n", encoding="utf-8")

    dispatcher = MagicMock()
    if failure == "raise":
        dispatcher.index_file.side_effect = RuntimeError("boom")
    else:
        dispatcher.index_file.return_value = IndexResult(
            IndexResultStatus.ERROR if failure == "error" else IndexResultStatus.INDEXED,
            source_file,
            None,
            None,
            semantic={failure: 1} if failure.startswith("semantic_") else None,
        )
    ctx = MagicMock()
    ctx.workspace_root = worktree
    ctx.sqlite_store = MagicMock()
    resolver = FakeResolver(
        RepositoryReadiness(
            state=RepositoryReadinessState.READY,
            repository_id="repo-1",
            repository_name="repo",
            requested_path=str(worktree),
        )
    )
    resolver.resolve = lambda _path: ctx

    result = _run(
        handle_reindex(
            arguments={"path": str(source_file)},
            dispatcher=dispatcher,
            repo_resolver=resolver,
        )
    )

    data = _parsed(result)
    assert data["error"] == "Reindex failed"
    assert data["code"] == "reindex_failed"
    assert data["path"] == str(source_file)
    assert data["mutation_performed"] is False
    assert data["details"] == (
        "boom"
        if failure == "raise"
        else (
            "File indexing did not complete"
            if failure == "error"
            else "Required semantic mutation did not complete"
        )
    )
    ctx.sqlite_store._get_connection.assert_not_called()


def test_write_summaries_remains_summary_only(tmp_path, monkeypatch):
    from mcp_server.cli.tool_handlers import handle_write_summaries

    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    worktree = tmp_path / "repo"
    worktree.mkdir()
    ctx = MagicMock()
    ctx.sqlite_store = MagicMock(db_path=str(tmp_path / "index.db"))
    resolver = FakeResolver(
        RepositoryReadiness(
            state=RepositoryReadinessState.READY,
            repository_id="repo-1",
            repository_name="repo",
            requested_path=str(worktree),
        )
    )
    resolver.resolve = lambda _path: ctx

    class FakeWriter:
        def __init__(self, *args, **kwargs):
            pass

        async def process_scope(self, limit=500):
            del limit
            return SimpleNamespace(
                summaries_written=3, chunks_attempted=4, missing_chunk_ids=["c4"]
            )

    monkeypatch.setattr("mcp_server.indexing.summarization.ComprehensiveChunkWriter", FakeWriter)
    lazy_summarizer = MagicMock()
    lazy_summarizer.can_summarize.return_value = True
    lazy_summarizer._get_model_name.return_value = "chat"

    result = _run(
        handle_write_summaries(
            arguments={"repository": str(worktree), "limit": 4},
            dispatcher=MagicMock(),
            repo_resolver=resolver,
            lazy_summarizer=lazy_summarizer,
            current_session=None,
        )
    )

    data = _parsed(result)
    assert data["chunks_summarized"] == 3
    assert data["semantic_vectors_written"] is False
    assert data["summary_missing_chunks"] == 1


def _run_summarization_gate(monkeypatch, scheme_status: str) -> bool:
    """Drive handle_search_code past the lazy-summarization pre-check with the
    given chunk-scheme status and report whether a chunk was enqueued.

    Cleanup item 2 regression harness: the gate must admit ``compatible_legacy``
    (a legacy unmarked index) so summarization is not skipped until the first
    write auto-stamps the marker."""
    from mcp_server.cli import tool_handlers as th

    item = SimpleNamespace(
        file="pkg/mod.py",
        line=10,
        line_end=20,
        symbol="fn",
        snippet="def fn(): ...",
        last_indexed=None,
        source_metadata=None,
        semantic_source=None,
        semantic_profile_id=None,
        semantic_collection_name=None,
    )
    fake_result = SimpleNamespace(
        results=(item,),
        semantic_requested=False,
        source_type=None,
        friction_categories=(),
        history_labels=(),
        history_repos=(),
        include_source_metadata=False,
        readiness={},
        semantic_readiness=None,
        index_unavailable=None,
        code=None,
        message=None,
        semantic_profile_id=None,
        semantic_collection_name=None,
    )
    monkeypatch.setattr(th, "execute_search_service", lambda **_kwargs: fake_result)

    lazy_summarizer = MagicMock()
    lazy_summarizer.can_summarize.return_value = True
    sqlite_store = MagicMock()
    sqlite_store.get_chunk_scheme_status.return_value = (scheme_status, None, "scheme-x")
    sqlite_store.find_chunk_at_line.return_value = {"chunk_id": "c1"}

    _run(
        th.handle_search_code(
            arguments={"query": "fn"},
            dispatcher=MagicMock(),
            repo_resolver=object(),  # no .classify -> selector path skipped
            sqlite_store=sqlite_store,
            lazy_summarizer=lazy_summarizer,
        )
    )
    return lazy_summarizer.enqueue.called


def test_compatible_legacy_scheme_allows_summarization(monkeypatch):
    """A ``compatible_legacy`` (legacy unmarked) index must NOT skip lazy
    summarization -- the pre-check now accepts it alongside compatible/empty."""
    assert _run_summarization_gate(monkeypatch, "compatible_legacy") is True


def test_incompatible_scheme_still_skips_summarization(monkeypatch):
    """The gate stays fail-closed: a mismatched/rebuilding scheme still skips
    summarization (proves the compatible_legacy admission is scoped, not a
    blanket open of the gate)."""
    assert _run_summarization_gate(monkeypatch, "mismatch") is False
    assert _run_summarization_gate(monkeypatch, "rebuilding") is False
