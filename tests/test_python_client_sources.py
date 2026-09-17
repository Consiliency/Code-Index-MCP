from pathlib import Path

from mcp_server import ClientSearchOptions, open_client
from mcp_server.indexing.github_issues import normalize_github_issue
from tests.fixtures.multi_repo import boot_test_server, build_temp_repo


def _history_record(*, repo: str, issue_number: int, labels: list[str]) -> dict:
    return normalize_github_issue(
        {
            "number": issue_number,
            "title": "Reflection issue",
            "labels": [{"name": label} for label in labels],
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


def test_direct_client_supports_friction_filters(tmp_path: Path):
    repo_path, repo_id = build_temp_repo(
        tmp_path,
        "pyclient_friction_repo",
        seed_files={"seed.py": "# TODO: keep filtering explicit\n"},
    )

    with boot_test_server(tmp_path, [repo_path]) as server:
        store = server.store_registry.get(repo_id)
        with store._get_connection() as conn:
            file_row = conn.execute(
                "SELECT id FROM files WHERE relative_path = 'seed.py'"
            ).fetchone()
        store.store_chunk(
            file_id=file_row[0],
            content="# TODO: keep filtering explicit",
            content_start=0,
            content_end=31,
            line_start=1,
            line_end=1,
            chunk_id="seed.py:1",
            node_id="seed.py:1",
            treesitter_file_id="seed.py",
        )
        with store._get_connection() as conn:
            conn.execute(
                "INSERT INTO fts_code (content, file_id) VALUES (?, ?)", ("TODO", file_row[0])
            )

        with open_client(
            workspace_root=repo_path, registry_path=tmp_path / "registry.json"
        ) as client:
            result = client.search_code(
                ClientSearchOptions(
                    query="filtering",
                    source_type="friction",  # type: ignore[arg-type]
                    friction_categories=("todo",),
                    include_source_metadata=True,
                )
            )

    assert len(result.results) == 1
    assert result.results[0].source_metadata["records"][0]["category"] == "todo"


def test_direct_client_supports_history_filters(tmp_path: Path):
    repo_path, repo_id = build_temp_repo(
        tmp_path,
        "pyclient_history_repo",
        seed_files={"seed.py": "def seed():\n    return 'history'\n"},
    )

    with boot_test_server(tmp_path, [repo_path]) as server:
        store = server.store_registry.get(repo_id)
        store.upsert_history_issue_documents(
            store.create_repository(str(repo_path), repo_path.name),
            [_history_record(repo="owner/repo", issue_number=1, labels=["reflection"])],
        )

        with open_client(
            workspace_root=repo_path, registry_path=tmp_path / "registry.json"
        ) as client:
            result = client.search_code(
                ClientSearchOptions(
                    query="Reflection",
                    source_type="history",  # type: ignore[arg-type]
                    history_labels=("reflection",),
                    history_repos=("owner/repo",),
                    include_source_metadata=True,
                )
            )

    assert len(result.results) == 1
    assert result.results[0].source_metadata["records"][0]["repo"] == "owner/repo"
