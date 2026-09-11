from types import SimpleNamespace

from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher


def test_unfiltered_lexical_search_keeps_legacy_shape(sqlite_store, tmp_path):
    repo_id = sqlite_store.create_repository(str(tmp_path), "repo")
    file_id = sqlite_store.store_file(
        repo_id, str(tmp_path / "main.py"), "main.py", language="python"
    )
    sqlite_store.store_chunk(
        file_id=file_id,
        content="def demo():\n    return 'demo'",
        content_start=0,
        content_end=29,
        line_start=1,
        line_end=2,
        chunk_id="chunk-1",
        node_id="node-1",
        treesitter_file_id="ts-1",
    )
    with sqlite_store._get_connection() as conn:
        conn.execute("INSERT INTO fts_code (content, file_id) VALUES (?, ?)", ("demo", file_id))

    ctx = SimpleNamespace(
        sqlite_store=sqlite_store,
        repo_id="repo-id",
        registry_entry=SimpleNamespace(path=tmp_path, name="repo"),
        workspace_root=tmp_path,
    )
    dispatcher = EnhancedDispatcher()

    results = list(dispatcher.search(ctx, "demo", semantic=False, limit=5))

    assert results
    assert "source_metadata" not in results[0]


def test_friction_filtered_search_returns_only_matching_chunks(sqlite_store, tmp_path):
    repo_id = sqlite_store.create_repository(str(tmp_path), "repo")
    file_id = sqlite_store.store_file(
        repo_id, str(tmp_path / "main.py"), "main.py", language="python"
    )
    sqlite_store.store_chunk(
        file_id=file_id,
        content="# TODO: first",
        content_start=0,
        content_end=13,
        line_start=1,
        line_end=1,
        chunk_id="chunk-1",
        node_id="node-1",
        treesitter_file_id="ts-1",
    )
    sqlite_store.store_chunk(
        file_id=file_id,
        content="# FIXME: second",
        content_start=14,
        content_end=29,
        line_start=2,
        line_end=2,
        chunk_id="chunk-2",
        node_id="node-2",
        treesitter_file_id="ts-2",
    )
    ctx = SimpleNamespace(
        sqlite_store=sqlite_store,
        repo_id="repo-id",
        registry_entry=SimpleNamespace(path=tmp_path, name="repo"),
        workspace_root=tmp_path,
    )
    dispatcher = EnhancedDispatcher()

    results = list(
        dispatcher.search(
            ctx,
            "second",
            source_type="friction",
            friction_categories=["fixme"],
            limit=5,
        )
    )

    assert len(results) == 1
    assert results[0]["source_metadata"]["records"][0]["category"] == "fixme"


def test_friction_filtered_search_finds_matches_after_many_plain_chunks(sqlite_store, tmp_path):
    repo_id = sqlite_store.create_repository(str(tmp_path), "repo")
    file_id = sqlite_store.store_file(
        repo_id, str(tmp_path / "main.py"), "main.py", language="python"
    )
    for index in range(120):
        sqlite_store.store_chunk(
            file_id=file_id,
            content=f"def filler_{index}():\n    return {index}",
            content_start=index * 30,
            content_end=(index + 1) * 30,
            line_start=index + 1,
            line_end=index + 1,
            chunk_id=f"plain-{index}",
            node_id=f"plain-{index}",
            treesitter_file_id=f"plain-{index}",
        )
    sqlite_store.store_chunk(
        file_id=file_id,
        content="# TODO: late marker",
        content_start=4000,
        content_end=4019,
        line_start=200,
        line_end=200,
        chunk_id="late-marker",
        node_id="late-marker",
        treesitter_file_id="late-marker",
    )
    dispatcher = EnhancedDispatcher()

    results = list(
        dispatcher.search(
            SimpleNamespace(
                sqlite_store=sqlite_store,
                repo_id="repo-id",
                registry_entry=SimpleNamespace(path=tmp_path, name="repo"),
                workspace_root=tmp_path,
            ),
            "late marker",
            source_type="friction",
            friction_categories=["todo"],
            limit=5,
        )
    )

    assert len(results) == 1
    assert results[0]["line"] == 200
    assert results[0]["source_metadata"]["records"][0]["category"] == "todo"


def test_include_source_metadata_enriches_unfiltered_results(sqlite_store, tmp_path):
    repo_id = sqlite_store.create_repository(str(tmp_path), "repo")
    file_id = sqlite_store.store_file(
        repo_id, str(tmp_path / "main.py"), "main.py", language="python"
    )
    sqlite_store.store_chunk(
        file_id=file_id,
        content="# TODO: first",
        content_start=0,
        content_end=13,
        line_start=1,
        line_end=1,
        chunk_id="chunk-1",
        node_id="node-1",
        treesitter_file_id="ts-1",
    )
    with sqlite_store._get_connection() as conn:
        conn.execute("INSERT INTO fts_code (content, file_id) VALUES (?, ?)", ("TODO", file_id))
    ctx = SimpleNamespace(
        sqlite_store=sqlite_store,
        repo_id="repo-id",
        registry_entry=SimpleNamespace(path=tmp_path, name="repo"),
        workspace_root=tmp_path,
    )
    dispatcher = EnhancedDispatcher()

    results = list(dispatcher.search(ctx, "TODO", include_source_metadata=True, limit=5))

    assert results[0]["source_metadata"]["records"][0]["category"] == "todo"
