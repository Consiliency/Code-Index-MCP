"""Indexer/sweep parity, incremental ignores and generation retention.

Regression coverage for Consiliency/Code-Index-MCP#114 (the watcher sweep
counted files the indexer never stores as drift, forcing a full rebuild every
interval, and superseded generations were never pruned) and #111 (incremental
sync failed when a commit touched a disabled-language file).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mcp_server.core.ignore_patterns import build_walker_filter
from mcp_server.dispatcher.dispatcher_enhanced import (
    EnhancedDispatcher,
    IndexResult,
    IndexResultStatus,
)
from mcp_server.indexing.admission import index_admission_reason
from mcp_server.storage.git_index_manager import (
    GitAwareIndexManager,
    StagedRebuildError,
    UpdateResult,
    _format_rebuild_error,
)
from mcp_server.storage.repository_registry import RepositoryRegistry
from mcp_server.storage.sqlite_store import SQLiteStore
from mcp_server.watcher.sweeper import WatcherSweeper

FIXTURE_FILES = {
    "hello.py": "def hello():\n    return 1\n",
    "README.md": "# Title\n\nSome text.\n",
    # Disabled-by-default languages in the default sandbox posture.
    ".github/workflows/ci.yml": "on: push\n",
    "compose.yaml": "services: {}\n",
    ".claude/settings.json": "{}\n",
    ".devcontainer/post_create.sh": "echo ready\n",
    "notes.txt": "notes\n",
    "pyproject.toml": "[project]\n",
    # Unsupported extensions.
    "setup.cfg": "[metadata]\n",
    "LICENSE": "license text\n",
}
NEVER_STORED = set(FIXTURE_FILES) - {"hello.py", "README.md"}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def parity_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_PLUGIN_SANDBOX_DISABLE", raising=False)
    monkeypatch.delenv("MCP_INDEX_GENERATION_RETENTION", raising=False)
    monkeypatch.setenv("MCP_INDEX_STORAGE_PATH", str(tmp_path / "indexes"))
    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("MCP_ENABLE_MULTI_REPO", "false")
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    for name, content in FIXTURE_FILES.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    # Runtime databases must not become committed source.
    (repo / ".git" / "info" / "exclude").write_text("/.mcp-index/\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")

    registry = RepositoryRegistry(tmp_path / "registry.json")
    repo_id = registry.register_repository(str(repo))
    registry.get(repo_id).index_path.parent.mkdir(parents=True, exist_ok=True)
    dispatcher = EnhancedDispatcher(
        plugins=[],
        enable_advanced_features=False,
        use_plugin_factory=False,
        semantic_search_enabled=False,
        memory_aware=False,
        multi_repo_enabled=False,
    )
    manager = GitAwareIndexManager(registry, dispatcher)
    try:
        yield repo, registry, repo_id, dispatcher, manager
    finally:
        dispatcher.shutdown()
        if manager.store_registry is not None:
            manager.store_registry.shutdown()


def _published_store(registry: RepositoryRegistry, repo_id: str) -> SQLiteStore:
    info = RepositoryRegistry(registry.registry_path).get(repo_id)
    return SQLiteStore(str(info.index_path))


def _index_snapshot(registry: RepositoryRegistry, repo_id: str):
    store = _published_store(registry, repo_id)
    try:
        with store._get_connection() as conn:
            files = {
                (row[0], row[1])
                for row in conn.execute(
                    "SELECT relative_path, content_hash FROM files WHERE is_deleted = FALSE"
                )
            }
            symbols = {
                tuple(row)
                for row in conn.execute(
                    "SELECT f.relative_path, s.name, s.kind, s.line_start, s.line_end "
                    "FROM symbols s JOIN files f ON f.id = s.file_id"
                )
            }
        return files, symbols
    finally:
        store.close()


def test_full_rebuild_then_sweep_reports_no_drift(parity_runtime):
    """#114 acceptance: right after a full rebuild the sweep sees no drift."""
    repo, registry, repo_id, dispatcher, manager = parity_runtime
    result = manager.sync_repository_index(repo_id, force_full=True)
    assert result.action == "full_index", result.error

    files, _symbols = _index_snapshot(registry, repo_id)
    stored = {path for path, _hash in files}
    assert "hello.py" in stored
    assert stored.isdisjoint(NEVER_STORED)

    store = _published_store(registry, repo_id)
    try:
        for admission in (dispatcher.index_admission_reason, None):
            drift = []
            sweeper = WatcherSweeper(
                None,
                lambda: {repo_id: repo},
                store,
                on_repository_drift=drift.append,
                admission_reason=admission,
            )
            assert sweeper.sweep_once() == []
            assert drift == []

        # Sanity: the sweep really compares this repository's rows.
        with store._get_connection() as conn:
            conn.execute("UPDATE files SET content_hash = 'stale' WHERE relative_path = 'hello.py'")
        drift = []
        sweeper = WatcherSweeper(
            None,
            lambda: {repo_id: repo},
            store,
            on_repository_drift=drift.append,
            admission_reason=dispatcher.index_admission_reason,
        )
        assert sweeper.sweep_once() == [repo_id]
    finally:
        store.close()


def test_walk_and_sweep_share_one_admission_predicate(parity_runtime):
    repo, _registry, repo_id, dispatcher, _manager = parity_runtime
    is_excluded = build_walker_filter(repo)
    for name in FIXTURE_FILES:
        path = repo / name
        assert dispatcher.index_admission_reason(repo_id, path, repo, is_excluded) == (
            index_admission_reason(path, repo, is_excluded)
        )
        assert (dispatcher.index_admission_reason(repo_id, path, repo, is_excluded) is None) == (
            name not in NEVER_STORED
        )


def test_incremental_sync_ignores_disabled_language_files_like_a_full_rebuild(
    parity_runtime,
):
    """#111: a diff that touches YAML must sync incrementally and match a full rebuild."""
    repo, registry, repo_id, _dispatcher, manager = parity_runtime
    assert manager.sync_repository_index(repo_id, force_full=True).action == "full_index"

    (repo / ".github/workflows/ci.yml").write_text("on: [push, pull_request]\n")
    (repo / "compose.yaml").write_text("services:\n  web: {}\n")
    (repo / "hello.py").write_text("def hello():\n    return 1\n\n\ndef added():\n    return 2\n")
    _git(repo, "commit", "-qam", "touch yaml and python")

    incremental = manager.sync_repository_index(repo_id)
    assert incremental.action == "incremental_update", incremental.error
    assert RepositoryRegistry(registry.registry_path).get(repo_id).staleness_reason is None
    incremental_files, incremental_symbols = _index_snapshot(registry, repo_id)

    full = manager.sync_repository_index(repo_id, force_full=True)
    assert full.action == "full_index", full.error
    full_files, full_symbols = _index_snapshot(registry, repo_id)

    assert incremental_files == full_files
    assert incremental_symbols == full_symbols
    assert any(name == "added" for _path, name, *_rest in incremental_symbols)
    assert {path for path, _hash in incremental_files}.isdisjoint(NEVER_STORED)


def test_incremental_update_counts_unsupported_files_as_ignored(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ci.yml").write_text("on: push\n")
    (repo / "old.yml").write_text("on: push\n")
    index_path = tmp_path / "current.db"
    index_path.touch()
    repo_info = MagicMock(index_path=index_path, repository_id="r")
    registry = MagicMock()
    registry.get_repository.return_value = repo_info
    dispatcher = MagicMock()
    dispatcher.index_file.side_effect = lambda _ctx, path: IndexResult(
        IndexResultStatus.ERROR, path, None, None, f"No plugin for {path} (plugin_unsupported)"
    )
    dispatcher.remove_file.side_effect = lambda _ctx, path: IndexResult(
        IndexResultStatus.DELETED, path, None, None
    )
    manager = GitAwareIndexManager(registry, dispatcher)
    manager._index_exists = MagicMock(return_value=True)
    ctx = MagicMock(workspace_root=repo)
    from mcp_server.storage.git_index_manager import ChangeSet

    result = manager._incremental_index_update(
        "r", ctx, ChangeSet(added=["ci.yml"], modified=["old.yml"], deleted=[], renamed=[])
    )
    assert result.clean
    assert result.ignored == 2
    assert result.failed == 0
    # A previously indexed file that no plugin stores now is dropped, as a full
    # rebuild would drop it.
    assert result.deleted == 1
    dispatcher.remove_file.assert_called_once_with(ctx, repo / "old.yml")


def test_failed_sync_error_carries_message_and_bounded_redacted_file_errors(tmp_path):
    errors = [f"Failed to index {tmp_path}/repo/f{i}.py: bad token=hunter{i}" for i in range(8)]
    result = UpdateResult(failed=8, errors=errors)
    message = _format_rebuild_error(
        StagedRebuildError("Staged full index did not complete cleanly"),
        result,
        strip_prefixes=[str(tmp_path / "repo")],
    )
    assert message.startswith(
        "Staged rebuild failed (StagedRebuildError: Staged full index did not complete cleanly)"
    )
    assert "8 file error(s)" in message
    assert "Failed to index f0.py" in message
    assert "and 3 more" in message
    assert "hunter" not in message and "[REDACTED]" in message
    assert str(tmp_path) not in message
    assert len(message) <= 2000

    # Foreign exception text can carry private detail: only its type is kept.
    foreign = _format_rebuild_error(OSError("private detail"), UpdateResult(), strip_prefixes=[])
    assert foreign == "Staged rebuild failed (OSError)"


def test_failed_incremental_sync_stores_and_reports_file_errors(parity_runtime, monkeypatch):
    repo, registry, repo_id, dispatcher, manager = parity_runtime
    assert manager.sync_repository_index(repo_id, force_full=True).action == "full_index"
    (repo / "hello.py").write_text("def hello():\n    return 3\n")
    _git(repo, "commit", "-qam", "change")

    def failing_index_file(ctx, path, *args, **kwargs):
        return IndexResult(IndexResultStatus.ERROR, path, None, None, "parser exploded")

    monkeypatch.setattr(dispatcher, "index_file", failing_index_file)
    result = manager.sync_repository_index(repo_id)
    assert result.action == "failed"
    assert "Staged full index did not complete cleanly" in result.error
    assert "Failed to index hello.py: parser exploded" in result.error
    stored = RepositoryRegistry(registry.registry_path).get(repo_id)
    assert stored.last_sync_error == result.error


# ---------------------------------------------------------------------------
# Generation retention (#114)
# ---------------------------------------------------------------------------


def _generation_files(registry: RepositoryRegistry, repo_id: str):
    info = RepositoryRegistry(registry.registry_path).get(repo_id)
    generations = Path(info.index_path).parent
    return info, sorted(path.stem for path in generations.glob("*.db"))


def test_repeated_rebuilds_keep_live_plus_bounded_rollback_generations(parity_runtime, monkeypatch):
    repo, registry, repo_id, _dispatcher, manager = parity_runtime
    monkeypatch.setenv("MCP_INDEX_GENERATION_RETENTION", "2")
    published = []
    for _ in range(5):
        result = manager.sync_repository_index(repo_id, force_full=True)
        assert result.action == "full_index", result.error
        published.append(result.readiness["index_generation"])
        info, present = _generation_files(registry, repo_id)
        assert info.index_generation == published[-1]
        assert published[-1] in present
        assert len(present) <= 3
    info, present = _generation_files(registry, repo_id)
    assert set(present) == set(published[-3:])
    history = json.loads((Path(info.index_path).parent / "published-generations.json").read_text())
    assert history["published"] == published[-3:]


def test_retention_never_drops_below_one_rollback_generation(parity_runtime, monkeypatch):
    repo, registry, repo_id, _dispatcher, manager = parity_runtime
    monkeypatch.setenv("MCP_INDEX_GENERATION_RETENTION", "0")
    published = [
        manager.sync_repository_index(repo_id, force_full=True).readiness["index_generation"]
        for _ in range(3)
    ]
    _info, present = _generation_files(registry, repo_id)
    assert set(present) == set(published[-2:])


def _fake_generation(directory: Path, generation: str, mtime: float, semantic=False) -> Path:
    path = directory / f"{generation}.db"
    path.write_bytes(b"x" * 10)
    Path(f"{path}-wal").write_bytes(b"w")
    if semantic:
        (directory / f"{generation}.semantic").mkdir()
        (directory / f"{generation}.semantic" / "segment").write_bytes(b"v")
    os.utime(path, (mtime, mtime))
    return path


def _retention_manager(index_paths):
    registry = MagicMock()
    registry.get_all_repositories.return_value = {
        f"repo-{index}": MagicMock(index_path=path) for index, path in enumerate(index_paths)
    }
    return GitAwareIndexManager(registry, MagicMock())


def test_prune_policy_keeps_live_referenced_rollback_and_recent_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_INDEX_GENERATION_RETENTION", "1")
    generations = tmp_path / "generations"
    generations.mkdir()
    (generations / "quarantine").mkdir()
    quarantined = generations / "quarantine" / "current.db.corrupt_sqlite.20261108T000000Z"
    quarantined.write_bytes(b"q")
    now = time.time()
    ids = {name: name * 32 for name in "abcdefg"}
    old_published = _fake_generation(generations, ids["a"], now - 600, semantic=True)
    old_unpublished = _fake_generation(generations, ids["b"], now - 500)
    other_repo_live = _fake_generation(generations, ids["c"], now - 450)
    previous = _fake_generation(generations, ids["d"], now - 400)
    recent_failure = _fake_generation(generations, ids["e"], now - 300)
    live = _fake_generation(generations, ids["f"], now - 100)
    (generations / "published-generations.json").write_text(
        json.dumps({"version": 1, "published": [ids["a"], ids["d"]]})
    )
    manager = _retention_manager([live, other_repo_live])

    pruned = manager._prune_superseded_generations(
        generations, live_generation=ids["f"], previous_index_path=previous
    )

    assert sorted(pruned) == sorted([ids["a"], ids["b"]])
    assert not old_published.exists() and not Path(f"{old_published}-wal").exists()
    assert not (generations / f"{ids['a']}.semantic").exists()
    assert not old_unpublished.exists()
    for kept in (other_repo_live, previous, recent_failure, live, quarantined):
        assert kept.exists()
    history = json.loads((generations / "published-generations.json").read_text())
    assert history["published"] == [ids["d"], ids["f"]]


def test_prune_after_operator_rollback_keeps_the_previously_live_generation(tmp_path, monkeypatch):
    """dev0 rollback restores a registry that points at an older generation."""
    monkeypatch.setenv("MCP_INDEX_GENERATION_RETENTION", "1")
    generations = tmp_path / "generations"
    generations.mkdir()
    now = time.time()
    rolled_back_to, rolled_back_from, new_live = "1" * 32, "2" * 32, "3" * 32
    restored = _fake_generation(generations, rolled_back_to, now - 300)
    bad = _fake_generation(generations, rolled_back_from, now - 200)
    live = _fake_generation(generations, new_live, now - 100)
    (generations / "published-generations.json").write_text(
        json.dumps({"version": 1, "published": [rolled_back_to, rolled_back_from]})
    )
    manager = _retention_manager([live])

    pruned = manager._prune_superseded_generations(
        generations, live_generation=new_live, previous_index_path=restored
    )

    assert pruned == [rolled_back_from]
    assert restored.exists() and live.exists() and not bad.exists()


def test_prune_failure_never_fails_publication(tmp_path, caplog):
    manager = GitAwareIndexManager(MagicMock(), MagicMock())
    manager.registry.get_all_repositories.side_effect = RuntimeError("registry offline")
    generations = tmp_path / "generations"
    generations.mkdir()
    assert (
        manager._prune_superseded_generations(
            generations, live_generation="f" * 32, previous_index_path=None
        )
        == []
    )
    assert "Generation retention skipped" in caplog.text


def test_unreadable_history_is_rebuilt_without_deleting_rollback(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_INDEX_GENERATION_RETENTION", "1")
    generations = tmp_path / "generations"
    generations.mkdir()
    now = time.time()
    previous = _fake_generation(generations, "a" * 32, now - 200)
    live = _fake_generation(generations, "b" * 32, now - 100)
    (generations / "published-generations.json").write_text("{not json")
    manager = _retention_manager([live])
    assert (
        manager._prune_superseded_generations(
            generations, live_generation="b" * 32, previous_index_path=previous
        )
        == []
    )
    assert previous.exists() and live.exists()
    with closing(open(generations / "published-generations.json")) as handle:
        assert json.load(handle)["published"] == ["a" * 32, "b" * 32]


# ---------------------------------------------------------------------------
# Plugin worker state does not accumulate across rebuilds (#114 memory growth)
# ---------------------------------------------------------------------------


def test_each_staged_rebuild_releases_the_repository_plugin_workers(parity_runtime, monkeypatch):
    _repo, _registry, repo_id, dispatcher, manager = parity_runtime
    released = []
    original = dispatcher.release_staged_plugin_workers

    def tracking_release(target_repo_id):
        # Queries are still fenced when the staged workers are dropped.
        current = RepositoryRegistry(_registry.registry_path).get(target_repo_id)
        released.append((target_repo_id, current.staleness_reason))
        return original(target_repo_id)

    monkeypatch.setattr(dispatcher, "release_staged_plugin_workers", tracking_release)
    for _ in range(3):
        assert manager.sync_repository_index(repo_id, force_full=True).action == "full_index"
    assert released == [(repo_id, "index_publication_pending")] * 3


def test_release_staged_plugin_workers_evicts_only_that_repository():
    plugin_sets = MagicMock()
    dispatcher = EnhancedDispatcher.__new__(EnhancedDispatcher)
    dispatcher._plugin_set_registry = plugin_sets
    assert dispatcher.release_staged_plugin_workers("repo-a") is True
    plugin_sets.evict.assert_called_once_with("repo-a")

    plugin_sets.evict.side_effect = RuntimeError("worker gone")
    assert dispatcher.release_staged_plugin_workers("repo-a") is False
