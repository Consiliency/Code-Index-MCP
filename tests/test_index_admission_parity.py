"""Indexer/sweep parity, incremental ignores and generation retention.

Regression coverage for Consiliency/Code-Index-MCP#114 (the watcher sweep
counted files the indexer never stores as drift, forcing a full rebuild every
interval, and superseded generations were never pruned) and #111 (incremental
sync failed when a commit touched a disabled-language file).
"""

from __future__ import annotations

import json
import os
import sqlite3
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
    assert "Staged index update did not complete cleanly" in result.error
    assert "Failed to index hello.py: parser exploded" in result.error
    stored = RepositoryRegistry(registry.registry_path).get(repo_id)
    assert stored.last_sync_error == result.error


def _simulate_plugin_that_will_not_resolve(dispatcher, name: str, monkeypatch):
    """A supported-language plugin whose worker fails to start for one file."""
    original = dispatcher._plugin_set_registry.plugins_for_file
    monkeypatch.setattr(
        dispatcher._plugin_set_registry,
        "plugins_for_file",
        lambda ctx, path: [] if str(path).endswith(name) else original(ctx, path),
    )


def _indexed(registry: RepositoryRegistry, repo_id: str, relative_path: str) -> bool:
    files, _symbols = _index_snapshot(registry, repo_id)
    return any(path == relative_path for path, _hash in files)


def test_incremental_sync_keeps_old_generation_when_a_supported_plugin_fails(
    parity_runtime, monkeypatch
):
    """#111 review B3 (probe_admitted_noplugin): no silent loss of real source."""
    repo, registry, repo_id, dispatcher, manager = parity_runtime
    assert manager.sync_repository_index(repo_id, force_full=True).action == "full_index"
    before = RepositoryRegistry(registry.registry_path).get(repo_id).index_generation
    (repo / "hello.py").write_text("def hello():\n    return 2\n")
    _git(repo, "commit", "-qam", "edit py")
    _simulate_plugin_that_will_not_resolve(dispatcher, "hello.py", monkeypatch)

    result = manager.sync_repository_index(repo_id)

    assert result.action == "failed"
    assert "Failed to index hello.py: No plugin for" in result.error
    current = RepositoryRegistry(registry.registry_path).get(repo_id)
    assert current.index_generation == before
    assert current.staleness_reason == "partial_index_failure"
    assert _indexed(registry, repo_id, "hello.py")


def test_full_rebuild_fails_when_an_admitted_file_has_no_plugin(parity_runtime, monkeypatch):
    repo, registry, repo_id, dispatcher, manager = parity_runtime
    assert manager.sync_repository_index(repo_id, force_full=True).action == "full_index"
    before = RepositoryRegistry(registry.registry_path).get(repo_id).index_generation
    _simulate_plugin_that_will_not_resolve(dispatcher, "hello.py", monkeypatch)

    result = manager.sync_repository_index(repo_id, force_full=True)

    assert result.action == "failed"
    assert RepositoryRegistry(registry.registry_path).get(repo_id).index_generation == before
    assert _indexed(registry, repo_id, "hello.py")


def test_rename_to_a_non_indexable_path_syncs_incrementally(parity_runtime):
    """#111 review S1 (probe_rename): delete the old path, ignore the new one."""
    repo, registry, repo_id, _dispatcher, manager = parity_runtime
    assert manager.sync_repository_index(repo_id, force_full=True).action == "full_index"
    assert _indexed(registry, repo_id, "README.md")
    _git(repo, "mv", "README.md", "README.txt")
    (repo / "hello.py").write_text("def hello():\n    return 2\n")
    _git(repo, "commit", "-qam", "rename md to txt")

    incremental = manager.sync_repository_index(repo_id)
    assert incremental.action == "incremental_update", incremental.error
    incremental_snapshot = _index_snapshot(registry, repo_id)
    stored = {path for path, _hash in incremental_snapshot[0]}
    assert "README.md" not in stored and "README.txt" not in stored

    assert manager.sync_repository_index(repo_id, force_full=True).action == "full_index"
    assert _index_snapshot(registry, repo_id) == incremental_snapshot


def test_error_summary_redacts_common_credential_formats():
    """#111 review S2: synthetic values only."""
    synthetic = {
        "github_classic": "ghp_" + "A1b2C3d4E5" * 4,
        "github_fine_grained": "github_pat_" + "11ABCDEFG0" * 3,
        "aws_access_key": "AKIA" + "ABCDEFGHIJ234567",
        "slack": "xoxb-" + "1234567890-abcdef",
        "url_credentials": "https://deploy:hunter2-synthetic@example.invalid/repo.git",
        "aws_secret": "aws_secret_access_key=SyntheticSecretValue0123456789",
    }
    errors = [f"Failed to index f{i}.py: {value}" for i, value in enumerate(synthetic.values())]
    message = _format_rebuild_error(
        StagedRebuildError("Staged index update did not complete cleanly"),
        UpdateResult(failed=len(errors), errors=errors[:5]),
        strip_prefixes=[],
    )
    message += _format_rebuild_error(
        StagedRebuildError("x"), UpdateResult(failed=1, errors=errors[5:]), strip_prefixes=[]
    )
    for name, value in synthetic.items():
        secret = value.split("=", 1)[-1] if name == "aws_secret" else value
        if name == "url_credentials":
            secret = "hunter2-synthetic"
        assert secret not in message, name
    assert message.count("[REDACTED]") >= len(synthetic)
    assert "Failed to index f0.py" in message


# ---------------------------------------------------------------------------
# Generation retention (#114)
# ---------------------------------------------------------------------------

HISTORY_FILE = "generation-history.json"


def _generation_files(registry: RepositoryRegistry, repo_id: str):
    info = RepositoryRegistry(registry.registry_path).get(repo_id)
    generations = Path(info.index_path).parent
    return info, sorted(path.stem for path in generations.glob("*.db"))


def _history(generations: Path) -> dict:
    return json.loads((generations / HISTORY_FILE).read_text())["generations"]


def test_repeated_rebuilds_keep_live_plus_bounded_previous_generations(parity_runtime, monkeypatch):
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
    assert set(_history(Path(info.index_path).parent)) == set(published[-3:])


def test_retention_never_drops_below_one_previous_generation(parity_runtime, monkeypatch):
    repo, registry, repo_id, _dispatcher, manager = parity_runtime
    monkeypatch.setenv("MCP_INDEX_GENERATION_RETENTION", "0")
    published = [
        manager.sync_repository_index(repo_id, force_full=True).readiness["index_generation"]
        for _ in range(3)
    ]
    _info, present = _generation_files(registry, repo_id)
    assert set(present) == set(published[-2:])


def test_failed_rebuilds_keep_only_the_newest_failed_generation(parity_runtime, monkeypatch):
    """#114 review N6: failed stages no longer accumulate without bound."""
    repo, registry, repo_id, _dispatcher, manager = parity_runtime
    live = manager.sync_repository_index(repo_id, force_full=True).readiness["index_generation"]

    def fail_before_publish(stage):
        if stage == "before_provenance":
            raise RuntimeError("synthetic publication failure")

    monkeypatch.setattr(manager, "_rebuild_checkpoint", fail_before_publish)
    for _ in range(3):
        assert manager.rebuild_repository_index(repo_id).action == "failed"
    info, present = _generation_files(registry, repo_id)
    assert info.index_generation == live
    failed = [key for key in present if key != live]
    assert len(failed) == 1
    assert _history(Path(info.index_path).parent)[failed[0]]["state"] == "failed"


def _fake_generation(directory: Path, generation: str, mtime: float, semantic=False) -> Path:
    path = directory / f"{generation}.db"
    path.write_bytes(b"x" * 10)
    if semantic:
        (directory / f"{generation}.semantic").mkdir()
        (directory / f"{generation}.semantic" / "segment").write_bytes(b"v")
    os.utime(path, (mtime, mtime))
    return path


def _write_history(directory: Path, entries: dict) -> None:
    (directory / HISTORY_FILE).write_text(json.dumps({"version": 2, "generations": entries}))


def _published(last_live_at: float) -> dict:
    return {"state": "published", "recorded_at": last_live_at, "last_live_at": last_live_at}


def _retention_manager(index_paths):
    registry = MagicMock()
    registry.get_all_repositories.return_value = {
        f"repo-{index}": MagicMock(index_path=path) for index, path in enumerate(index_paths)
    }
    return GitAwareIndexManager(registry, MagicMock())


@pytest.mark.parametrize("retention", ["2", "10"])
def test_first_prune_after_upgrade_never_deletes_unrecorded_generations(
    tmp_path, monkeypatch, retention
):
    """#114 review B1 (probe_prune_first_run): dev0-like layout, no history file."""
    monkeypatch.setenv("MCP_INDEX_GENERATION_RETENTION", retention)
    generations = tmp_path / "generations"
    generations.mkdir()
    now = time.time()
    names = {
        "a" * 32: ("rollback_v4", now - 3 * 86400),
        "b" * 32: ("replay_validated", now - 2 * 86400),
        "c" * 32: ("hourly_churn", now - 86400),
        "d" * 32: ("previous_live", now - 3600),
        "e" * 32: ("new_live", now),
    }
    paths = {key: _fake_generation(generations, key, mtime) for key, (_n, mtime) in names.items()}
    manager = _retention_manager([paths["e" * 32]])

    pruned = manager._prune_superseded_generations(
        generations, live_generation="e" * 32, previous_index_path=paths["d" * 32]
    )

    assert pruned == []
    assert all(path.exists() for path in paths.values())
    # Only the generation this code published is recorded.
    assert set(_history(generations)) == {"e" * 32}


def test_prune_policy_only_removes_recorded_superseded_generations(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_INDEX_GENERATION_RETENTION", "1")
    generations = tmp_path / "generations"
    generations.mkdir()
    (generations / "quarantine").mkdir()
    quarantined = generations / "quarantine" / "current.db.corrupt_sqlite.20261108T000000Z"
    quarantined.write_bytes(b"q")
    now = time.time()
    # Generation ids are 32 hex characters.
    ids = {name: char * 32 for name, char in zip("abcdefgh", "abcdef01")}
    old_published = _fake_generation(generations, ids["a"], now - 900, semantic=True)
    unknown = _fake_generation(generations, ids["b"], now - 800)
    other_repo_live = _fake_generation(generations, ids["c"], now - 700)
    pinned = _fake_generation(generations, ids["d"], now - 650)
    (generations / f"{ids['d']}.keep").write_text("")
    previous = _fake_generation(generations, ids["e"], now - 600)
    old_failure = _fake_generation(generations, ids["f"], now - 500)
    new_failure = _fake_generation(generations, ids["g"], now - 300)
    live = _fake_generation(generations, ids["h"], now - 100)
    _write_history(
        generations,
        {
            ids["a"]: _published(now - 900),
            ids["c"]: _published(now - 700),
            ids["d"]: _published(now - 650),
            ids["e"]: _published(now - 600),
            ids["f"]: {"state": "failed", "recorded_at": now - 500, "last_live_at": None},
            ids["g"]: {"state": "failed", "recorded_at": now - 300, "last_live_at": None},
        },
    )
    manager = _retention_manager([live, other_repo_live])

    pruned = manager._prune_superseded_generations(
        generations, live_generation=ids["h"], previous_index_path=previous
    )

    assert sorted(pruned) == sorted([ids["a"], ids["f"]])
    assert not old_published.exists()
    assert not (generations / f"{ids['a']}.semantic").exists()
    assert not old_failure.exists()
    for kept in (unknown, other_repo_live, pinned, previous, new_failure, live, quarantined):
        assert kept.exists()
    history = _history(generations)
    assert set(history) == {ids[key] for key in "cdegh"}
    assert history[ids["h"]]["state"] == "published"


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
    _write_history(
        generations,
        {rolled_back_to: _published(now - 300), rolled_back_from: _published(now - 200)},
    )
    manager = _retention_manager([live])

    pruned = manager._prune_superseded_generations(
        generations, live_generation=new_live, previous_index_path=restored
    )

    assert pruned == [rolled_back_from]
    assert restored.exists() and live.exists() and not bad.exists()


def test_generations_an_active_reader_holds_are_never_pruned(tmp_path, monkeypatch):
    """#114 review S3: pruning skips generations still open by a reader."""
    monkeypatch.setenv("MCP_INDEX_GENERATION_RETENTION", "1")
    generations = tmp_path / "generations"
    generations.mkdir()
    now = time.time()
    wal_reader, fd_reader, idle, previous, live = (c * 32 for c in "abcde")
    wal_path = generations / f"{wal_reader}.db"
    with closing(sqlite3.connect(wal_path)) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE t (x)")
        setup.commit()
    fd_path = _fake_generation(generations, fd_reader, now - 400)
    idle_path = _fake_generation(generations, idle, now - 350)
    previous_path = _fake_generation(generations, previous, now - 300)
    live_path = _fake_generation(generations, live, now - 100)
    _write_history(
        generations,
        {
            key: _published(now - 500 + index)
            for index, key in enumerate((wal_reader, fd_reader, idle))
        }
        | {previous: _published(now - 300)},
    )
    manager = _retention_manager([live_path])
    reader = sqlite3.connect(wal_path)
    handle = open(fd_path, "rb")
    try:
        reader.execute("SELECT * FROM t").fetchall()
        pruned = manager._prune_superseded_generations(
            generations, live_generation=live, previous_index_path=previous_path
        )
    finally:
        handle.close()
        reader.close()
    assert idle in pruned and not idle_path.exists()
    assert wal_reader not in pruned and wal_path.exists()
    if Path("/proc/self/fd").is_dir():
        assert fd_reader not in pruned and fd_path.exists()
    assert previous_path.exists() and live_path.exists()


def test_interrupted_removal_keeps_the_record_and_is_retried(tmp_path, monkeypatch):
    """#114 review N1: semantic data and sidecars go first, the database last."""
    monkeypatch.setenv("MCP_INDEX_GENERATION_RETENTION", "1")
    generations = tmp_path / "generations"
    generations.mkdir()
    now = time.time()
    old, previous, live = "a" * 32, "b" * 32, "c" * 32
    old_path = _fake_generation(generations, old, now - 300, semantic=True)
    previous_path = _fake_generation(generations, previous, now - 200)
    live_path = _fake_generation(generations, live, now - 100)
    _write_history(generations, {old: _published(now - 300), previous: _published(now - 200)})
    manager = _retention_manager([live_path])
    original_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self == old_path:
            raise PermissionError("database busy")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    assert (
        manager._prune_superseded_generations(
            generations, live_generation=live, previous_index_path=previous_path
        )
        == []
    )
    assert old_path.exists() and not (generations / f"{old}.semantic").exists()
    assert old in _history(generations)

    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert manager._prune_superseded_generations(generations, failed_generation=None) == [old]
    assert not old_path.exists()


def test_history_writes_are_durable(tmp_path, monkeypatch):
    """#114 review N2: the history file and its directory are fsynced."""
    from mcp_server.storage import git_index_manager as gim

    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(gim.os, "fsync", lambda fd: synced.append(fd) or real_fsync(fd))
    gim._write_generation_history(tmp_path / HISTORY_FILE, {"a" * 32: _published(1.0)})
    assert len(synced) >= (1 if os.name == "nt" else 2)
    assert set(_history(tmp_path)) == {"a" * 32}


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


@pytest.mark.parametrize("content", ["{not json", '{"version": 1, "published": []}'])
def test_unreadable_history_prunes_nothing(tmp_path, monkeypatch, content):
    monkeypatch.setenv("MCP_INDEX_GENERATION_RETENTION", "1")
    generations = tmp_path / "generations"
    generations.mkdir()
    now = time.time()
    older = _fake_generation(generations, "a" * 32, now - 300)
    previous = _fake_generation(generations, "b" * 32, now - 200)
    live = _fake_generation(generations, "c" * 32, now - 100)
    (generations / HISTORY_FILE).write_text(content)
    manager = _retention_manager([live])
    assert (
        manager._prune_superseded_generations(
            generations, live_generation="c" * 32, previous_index_path=previous
        )
        == []
    )
    assert older.exists() and previous.exists() and live.exists()
    assert set(_history(generations)) == {"c" * 32}


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


def test_admission_does_not_depend_on_which_repository_workers_are_loaded(tmp_path, monkeypatch):
    """#114 review N4: releasing or loading repo workers cannot change admission."""
    monkeypatch.delenv("MCP_PLUGIN_SANDBOX_DISABLE", raising=False)

    class ClaimsScss:
        lang = "css"
        language = "css"

        def supports(self, path):
            return str(path).endswith(".scss")

    style = tmp_path / "style.scss"
    style.write_text("a { color: red; }\n")
    never = lambda _path: False  # noqa: E731

    def build(plugins):
        return EnhancedDispatcher(
            plugins=plugins,
            enable_advanced_features=False,
            use_plugin_factory=False,
            semantic_search_enabled=False,
            memory_aware=False,
            multi_repo_enabled=False,
        )

    lazy = build([])
    try:
        before = lazy.index_admission_reason("r", style, tmp_path, never)
        monkeypatch.setattr(lazy._plugin_set_registry, "plugins_for", lambda _r: [ClaimsScss()])
        assert lazy.index_admission_reason("r", style, tmp_path, never) == before
        assert before is not None and before.startswith("plugin_")
    finally:
        lazy.shutdown()

    injected = build([ClaimsScss()])
    try:
        assert injected.index_admission_reason("r", style, tmp_path, never) is None
    finally:
        injected.shutdown()


def test_generation_kept_when_its_semantic_directory_cannot_be_removed(tmp_path, monkeypatch):
    """#129 re-check nit: if the local semantic directory survives removal, the
    database (and so its history record) is kept, so the next prune retries
    instead of orphaning the directory forever."""
    from mcp_server.storage import git_index_manager as gim

    real_rmtree = gim.shutil.rmtree

    database = tmp_path / "abc123.db"
    database.write_bytes(b"db")
    semantic = database.with_suffix(".semantic")
    semantic.mkdir()
    (semantic / "vectors.bin").write_bytes(b"v" * 16)

    monkeypatch.setattr(gim.shutil, "rmtree", lambda *args, **kwargs: None)
    assert gim._remove_generation_files(database) is None
    assert database.exists() and semantic.exists()

    monkeypatch.setattr(gim.shutil, "rmtree", real_rmtree)
    freed = gim._remove_generation_files(database)
    assert freed is not None and freed >= 18
    assert not database.exists() and not semantic.exists()
