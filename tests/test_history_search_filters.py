from types import SimpleNamespace

from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher
from mcp_server.indexing.github_issues import normalize_github_issue


def _store_history(sqlite_store, tmp_path, *, repo="owner/repo", labels=None, issue_number=1):
    repo_id = sqlite_store.create_repository(str(tmp_path), "repo")
    record = normalize_github_issue(
        {
            "number": issue_number,
            "title": "Reflection issue",
            "labels": [{"name": label} for label in (labels or ["reflection"])],
            "state": "closed",
            "createdAt": "2026-07-01T00:00:00Z",
            "updatedAt": "2026-07-02T00:00:00Z",
            "closedAt": "2026-07-03T00:00:00Z",
            "url": f"https://github.com/{repo}/issues/{issue_number}",
            "body": "Learning: Keep filtering explicit",
        },
        repo=repo,
        include_body_learnings=True,
    )
    sqlite_store.upsert_history_issue_documents(repo_id, [record])


def _ctx(sqlite_store, tmp_path):
    return SimpleNamespace(
        sqlite_store=sqlite_store,
        repo_id="repo-id",
        registry_entry=SimpleNamespace(path=tmp_path, name="repo"),
        workspace_root=tmp_path,
        staging=False,
    )


def test_history_filtered_search_returns_only_matching_history_chunks(sqlite_store, tmp_path):
    _store_history(sqlite_store, tmp_path, repo="owner/repo", labels=["reflection"], issue_number=1)
    _store_history(sqlite_store, tmp_path, repo="other/repo", labels=["backlog"], issue_number=2)
    dispatcher = EnhancedDispatcher()

    results = list(
        dispatcher.search(
            _ctx(sqlite_store, tmp_path),
            "Reflection",
            source_type="history",
            history_labels=["reflection"],
            history_repos=["owner/repo"],
            limit=5,
        )
    )

    assert len(results) == 1
    assert results[0]["source_metadata"]["records"][0]["repo"] == "owner/repo"


def test_include_source_metadata_enriches_unfiltered_history_hits(sqlite_store, tmp_path):
    _store_history(sqlite_store, tmp_path, repo="owner/repo", labels=["reflection"], issue_number=3)
    dispatcher = EnhancedDispatcher()

    results = list(
        dispatcher.search(
            _ctx(sqlite_store, tmp_path),
            "Reflection issue",
            include_source_metadata=True,
            limit=5,
        )
    )

    assert results
    assert results[0]["source_metadata"]["records"][0]["source_type"] == "history"


def test_unfiltered_lexical_history_search_keeps_legacy_shape(sqlite_store, tmp_path):
    _store_history(sqlite_store, tmp_path, repo="owner/repo", labels=["reflection"], issue_number=4)
    dispatcher = EnhancedDispatcher()

    results = list(dispatcher.search(_ctx(sqlite_store, tmp_path), "Reflection issue", limit=5))

    assert results
    assert "source_metadata" not in results[0]
