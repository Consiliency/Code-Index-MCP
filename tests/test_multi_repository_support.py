"""Tests for current multi-repository support surfaces."""

import asyncio
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from mcp_server.plugins.memory_aware_manager import MemoryAwarePluginManager
from mcp_server.plugins.repository_plugin_loader import RepositoryPluginLoader
from mcp_server.storage.multi_repo_manager import MultiRepositoryManager, RepositoryInfo
from mcp_server.storage.sqlite_store import SQLiteStore


def _repo_info(repo_id: str, path: Path) -> RepositoryInfo:
    index_base = path / ".mcp-index"
    index_base.mkdir(parents=True, exist_ok=True)
    return RepositoryInfo(
        repository_id=repo_id,
        name=path.name,
        path=path,
        index_path=index_base / "current.db",
        language_stats={"python": 1},
        total_files=1,
        total_symbols=1,
        indexed_at=datetime.now(),
        current_commit=f"commit-{repo_id}",
        artifact_enabled=True,
        active=True,
        index_location=str(index_base),
    )


def test_multi_repo_manager_ready_and_stale_sets(tmp_path: Path):
    manager = MultiRepositoryManager(central_index_path=tmp_path / "registry.json")

    ready_repo_path = tmp_path / "ready_repo"
    ready_repo_path.mkdir()
    ready_repo = _repo_info("ready", ready_repo_path)
    ready_repo.artifact_health = "ready"
    ready_repo.last_recovered_commit = ready_repo.current_commit
    source = ready_repo_path / "ready.py"
    source.write_text("ready = 1\n")
    store = SQLiteStore(str(ready_repo.index_path))
    try:
        row = store.ensure_repository_row(ready_repo_path)
        store.store_file(row, source, "ready.py")
    finally:
        store.close()
    ready_repo.last_indexed_commit = ready_repo.current_commit

    stale_repo_path = tmp_path / "stale_repo"
    stale_repo_path.mkdir()
    stale_repo = _repo_info("stale", stale_repo_path)
    stale_repo.last_recovered_commit = "older"

    manager.registry.register(ready_repo)
    manager.registry.register(stale_repo)

    assert [repo.repository_id for repo in manager.get_ready_repositories()] == ["ready"]
    assert {repo.repository_id for repo in manager.get_stale_repositories()} == {"stale"}


def test_repository_plugin_loader_analyzes_index_languages(tmp_path: Path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    index_dir = repo_path / ".mcp-index"
    index_dir.mkdir()
    db_path = index_dir / "current.db"

    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE files (id INTEGER PRIMARY KEY, language TEXT, path TEXT, is_deleted BOOLEAN DEFAULT 0)"
    )
    for language, path in [
        ("python", "main.py"),
        ("python", "test.py"),
        ("javascript", "app.js"),
    ]:
        conn.execute("INSERT INTO files (language, path) VALUES (?, ?)", (language, path))
    conn.commit()
    conn.close()

    loader = RepositoryPluginLoader(plugin_strategy="auto", preload_threshold=1)
    with patch(
        "mcp_server.plugins.repository_plugin_loader.IndexDiscovery.get_local_index_path",
        return_value=db_path,
    ):
        profile = asyncio.run(loader.analyze_repository(repo_path))

    assert profile.languages["python"] == 2
    assert profile.languages["javascript"] == 1
    assert profile.primary_languages[0] == "python"


def test_memory_aware_plugin_manager_reports_memory_status():
    manager = MemoryAwarePluginManager(max_memory_mb=64)
    status = manager.get_memory_status()

    assert status["max_memory_mb"] == 64
    assert "loaded_plugins" in status
    assert "plugin_details" in status


def test_multi_repo_manager_registry_persists_local_artifact_fields(tmp_path: Path):
    manager = MultiRepositoryManager(central_index_path=tmp_path / "registry.json")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo = _repo_info("repo-1", repo_path)
    repo.available_semantic_profiles = ["commercial_high", "oss_high"]
    repo.artifact_backend = "local_workspace"
    repo.artifact_health = "prepared"
    manager.registry.register(repo)

    reloaded = MultiRepositoryManager(central_index_path=tmp_path / "registry.json")
    # The registry _load() re-keys legacy ids to the canonical sha256 derived
    # from the resolved path, so look up via list_all() rather than the
    # original id.
    all_repos = reloaded.registry.list_all()
    assert len(all_repos) == 1
    stored = all_repos[0]

    assert stored is not None
    assert stored.available_semantic_profiles == ["commercial_high", "oss_high"]
    assert stored.artifact_backend == "local_workspace"
    assert stored.artifact_health == "prepared"
