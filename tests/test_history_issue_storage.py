import json

from mcp_server.indexing.github_issues import normalize_github_issue


def _history_record(number: int, issue_type: str, updated_at: str, *, repo: str = "owner/repo"):
    issue = {
        "number": number,
        "title": f"{issue_type} issue {number}",
        "labels": [{"name": issue_type.replace("_", "-")}],
        "state": "closed",
        "createdAt": "2026-07-01T00:00:00Z",
        "updatedAt": updated_at,
        "closedAt": "2026-07-02T00:00:00Z",
        "url": f"https://github.com/{repo}/issues/{number}",
        "body": "Learning: Stable dedupe",
    }
    return normalize_github_issue(issue, repo=repo, include_body_learnings=True)


def test_history_issue_documents_upsert_by_stable_chunk_key(sqlite_store):
    repo_id = sqlite_store.create_repository("/repo", "repo")
    first = _history_record(11, "reflection", "2026-07-02T00:00:00Z")
    second = _history_record(11, "reflection", "2026-07-03T00:00:00Z")

    sqlite_store.upsert_history_issue_documents(repo_id, [first])
    sqlite_store.upsert_history_issue_documents(repo_id, [second])

    with sqlite_store._get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM code_chunks").fetchone()
    assert row[0] == 1


def test_history_issue_storage_round_trips_normalized_metadata(sqlite_store):
    repo_id = sqlite_store.create_repository("/repo", "repo")
    record = _history_record(12, "retrospective", "2026-07-04T00:00:00Z")

    sqlite_store.upsert_history_issue_documents(repo_id, [record])

    results = sqlite_store.search_chunks_by_source_metadata(
        source_type="history",
        history_labels=["retrospective"],
        history_repos=["owner/repo"],
        limit=5,
    )

    assert len(results) == 1
    stored_record = results[0]["source_metadata"]["records"][0]
    assert stored_record["repo"] == "owner/repo"
    assert stored_record["learnings"] == ["Stable dedupe"]


def test_history_issue_storage_filters_exclude_source_code_and_friction_chunks(sqlite_store):
    repo_id = sqlite_store.create_repository("/repo", "repo")
    file_id = sqlite_store.store_file(repo_id, "/repo/file.py", "file.py", language="python")
    sqlite_store.store_chunk(
        file_id=file_id,
        content="# TODO: regular chunk",
        content_start=0,
        content_end=21,
        line_start=1,
        line_end=1,
        chunk_id="chunk-1",
        node_id="node-1",
        treesitter_file_id="ts-1",
    )
    sqlite_store.upsert_history_issue_documents(
        repo_id,
        [_history_record(13, "reflection", "2026-07-05T00:00:00Z")],
    )

    history_results = sqlite_store.search_chunks_by_source_metadata(
        source_type="history",
        history_labels=["reflection"],
        limit=5,
    )
    friction_results = sqlite_store.search_chunks_by_source_metadata(
        source_type="friction",
        friction_categories=["todo"],
        limit=5,
    )

    assert len(history_results) == 1
    assert len(friction_results) == 1
    assert history_results[0]["file"] != friction_results[0]["file"]


def test_history_query_filters_and_ranks_before_limit(sqlite_store):
    repo_id = sqlite_store.create_repository("/repo", "repo")
    unrelated = _history_record(11, "reflection", "2026-07-05T00:00:00Z")
    weak = _history_record(12, "reflection", "2026-07-05T00:00:00Z")
    weak["summary"] = "quasar " + "unrelated " * 200
    strong = _history_record(99, "reflection", "2026-07-05T00:00:00Z")
    strong["summary"] = "quasar quasar quasar"
    sqlite_store.upsert_history_issue_documents(repo_id, [unrelated, weak, strong])
    result = sqlite_store.search_chunks_by_source_metadata(
        query="quasar",
        source_type="history",
        history_labels=["reflection"],
        limit=1,
    )
    assert len(result) == 1
    assert result[0]["source_metadata"]["records"][0]["number"] == 99
    assert (
        sqlite_store.search_chunks_by_source_metadata(query="absenttoken", source_type="history")
        == []
    )
    assert (
        sqlite_store.search_chunks_by_source_metadata(
            query="quasar", source_type="history", limit=0
        )
        == []
    )


def test_filtered_query_cannot_match_a_different_chunk_in_same_file(sqlite_store):
    repo_id = sqlite_store.create_repository("/repo", "repo")
    reflection = _history_record(11, "reflection", "2026-07-05T00:00:00Z")
    retrospective = _history_record(11, "retrospective", "2026-07-05T00:00:00Z")
    retrospective["summary"] = "uniquemarker"
    sqlite_store.upsert_history_issue_documents(repo_id, [reflection, retrospective])
    assert (
        sqlite_store.search_chunks_by_source_metadata(
            query="uniquemarker",
            source_type="history",
            history_labels=["reflection"],
        )
        == []
    )
