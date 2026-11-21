"""Regression tests for Consiliency/Code-Index-MCP#145.

A full rebuild ran on an unchanged, already-indexed commit, readiness reported
``missing_provenance`` during an ordinary update, and nothing logged why a full
rebuild was chosen.
"""

import logging
import subprocess
from types import SimpleNamespace

from mcp_server.health.repository_readiness import (
    ReadinessClassifier,
    RepositoryReadinessState,
)
from mcp_server.utils.memory_release import release_rebuild_memory
from mcp_server.watcher.sweeper import WatcherSweeper
from mcp_server.watcher_multi_repo import MultiRepositoryWatcher
from tests.test_git_index_manager import _get_head_commit
from tests.test_v13_data_storage import runtime  # noqa: F401 - fixture


def _commit_all(repo, message):
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, check=True)
    return _get_head_commit(repo)


def _no_full_index(manager, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a full rebuild must not run")

    monkeypatch.setattr(manager, "_full_index", refuse)


def test_change_set_for_an_old_head_is_recomputed_not_published(runtime, monkeypatch):  # noqa: F811
    """A sync that read HEAD mid-``git pull`` must not label stale rows as the new commit.

    On dev0 the 03:47Z sync for 3868ab8 read HEAD while the pull was still
    checking out: HEAD was the indexed commit, so the change set was empty.
    The staged build then re-read HEAD (now 3868ab8), applied the empty
    change set, and published the old rows under 3868ab8. The hourly sweep
    found that drift and forced a full rebuild.
    """
    repo, registry, repo_id, _store, manager = runtime
    for index in range(6):  # keep the change below the half-of-tree full-rebuild rule
        (repo / f"stable{index}.py").write_text(f"stable_value_{index} = {index}\n")
    (repo / "module.py").write_text("old_sentinel_value = 1\n")
    indexed = _commit_all(repo, "baseline")
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    (repo / "module.py").write_text("new_sentinel_value = 2\n")
    head = _commit_all(repo, "advance")

    real_update = registry.update_git_state
    calls = {"count": 0}

    def mid_pull_view(repository_id):
        calls["count"] += 1
        if calls["count"] == 1:  # the sync entry sees the pre-pull HEAD
            return {"commit": indexed, "branch": "main"}
        return real_update(repository_id)

    monkeypatch.setattr(registry, "update_git_state", mid_pull_view)
    _no_full_index(manager, monkeypatch)

    result = manager.sync_repository_index(repo_id)

    assert result.action == "incremental_update", result.error
    assert result.commit == head
    assert registry.get(repo_id).last_indexed_commit == head
    store = manager._resolve_ctx(repo_id).sqlite_store
    assert store.search_code_fts("new_sentinel_value")
    assert not store.search_code_fts("old_sentinel_value")


def test_stale_change_set_is_refused_when_head_moved(runtime):  # noqa: F811
    repo, _registry, repo_id, _store, manager = runtime
    (repo / "module.py").write_text("value = 1\n")
    indexed = _commit_all(repo, "baseline")
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    (repo / "module.py").write_text("value = 2\n")
    _commit_all(repo, "advance")
    from mcp_server.storage.git_index_manager import ChangeSet

    result = manager._rebuild_repository_index_locked(
        repo_id,
        changes=ChangeSet(added=[], modified=[], deleted=[], renamed=[]),
        changes_commit=indexed,
    )
    assert result.action == "refused"
    assert result.code == "commit_moved"


def _sweeper_for(manager, registry, repo_id, store):
    changed = []
    watcher = SimpleNamespace(
        registry=registry,
        index_manager=manager,
        mark_repository_changed=changed.append,
    )
    holder = {}

    def reconcile(rid):
        return MultiRepositoryWatcher._reconcile_repository_drift(
            watcher, rid, drift=holder["sweeper"].current_drift(rid)
        )

    holder["sweeper"] = WatcherSweeper(
        None,
        lambda: {repo_id: registry.get(repo_id).path},
        None,
        store_provider=lambda _rid: manager._resolve_ctx(repo_id).sqlite_store,
        on_repository_drift=reconcile,
    )
    return holder["sweeper"], changed


def test_sweep_drift_on_unchanged_commit_repairs_only_drifted_paths(
    runtime, monkeypatch, caplog  # noqa: F811
):
    repo, registry, repo_id, _store, manager = runtime
    for index in range(6):
        (repo / f"stable{index}.py").write_text(f"stable_value_{index} = {index}\n")
    (repo / "drifted.py").write_text("drifted_sentinel = 1\n")
    head = _commit_all(repo, "baseline")
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    generation_before = registry.get(repo_id).index_generation

    # Make one published row disagree with the committed file, as a missed
    # update would.
    store = manager._resolve_ctx(repo_id).sqlite_store
    with store._get_connection() as connection:
        changed = connection.execute(
            "UPDATE files SET content_hash='stale', hash='stale' WHERE relative_path=?",
            ("drifted.py",),
        ).rowcount
    assert changed == 1

    indexed_paths = []
    real_incremental = manager._incremental_index_update

    def spy(repo_id_arg, ctx, changes, *args, **kwargs):
        indexed_paths.extend(changes.added + changes.modified)
        return real_incremental(repo_id_arg, ctx, changes, *args, **kwargs)

    monkeypatch.setattr(manager, "_incremental_index_update", spy)
    _no_full_index(manager, monkeypatch)
    sweeper, marked = _sweeper_for(manager, registry, repo_id, store)

    with caplog.at_level(logging.INFO):
        assert sweeper.sweep_once() == [repo_id]

    assert indexed_paths == ["drifted.py"]
    info = registry.get(repo_id)
    assert info.last_indexed_commit == head
    assert info.index_generation != generation_before
    assert marked == [repo_id]
    messages = [record.getMessage() for record in caplog.records]
    assert any("drift in" in m and "1 modified" in m for m in messages)
    assert any("Repairing sweep drift" in m for m in messages)
    # The repaired generation agrees with the tree: the next sweep is quiet.
    assert sweeper.sweep_once() == []


def test_failed_targeted_repair_falls_back_to_a_logged_full_rebuild(
    runtime, monkeypatch, caplog  # noqa: F811
):
    repo, registry, repo_id, _store, manager = runtime
    (repo / "drifted.py").write_text("drifted_sentinel = 1\n")
    _commit_all(repo, "baseline")
    assert manager.rebuild_repository_index(repo_id).action == "full_index"

    from mcp_server.storage.git_index_manager import UpdateResult

    def broken(*_args, **_kwargs):
        result = UpdateResult()
        result.failed = 1
        result.errors.append("synthetic failure")
        return result

    monkeypatch.setattr(manager, "_incremental_index_update", broken)
    with caplog.at_level(logging.INFO):
        result = manager.repair_repository_drift(repo_id, modified=["drifted.py"])
    assert result.action == "full_index", result.error
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "Full rebuild of" in m and "targeted repair of watcher sweep drift" in m for m in messages
    )
    trace = manager._read_force_full_exit_trace(registry.get(repo_id)) or {}
    assert "targeted repair" in (trace.get("full_rebuild_reason") or "")


def test_forced_full_rebuild_logs_and_records_its_reason(runtime, caplog):  # noqa: F811
    repo, registry, repo_id, _store, manager = runtime
    (repo / "module.py").write_text("value = 1\n")
    _commit_all(repo, "baseline")
    with caplog.at_level(logging.INFO, logger="mcp_server.storage.git_index_manager"):
        result = manager.sync_repository_index(
            repo_id, force_full=True, reason="operator requested --force-full"
        )
    assert result.action == "full_index", result.error
    assert any(
        "Full rebuild of" in record.getMessage()
        and "operator requested --force-full" in record.getMessage()
        for record in caplog.records
    )
    trace = manager._read_force_full_exit_trace(registry.get(repo_id)) or {}
    assert "operator requested --force-full" in trace["full_rebuild_reason"]


def test_unchanged_clean_indexed_commit_is_up_to_date(runtime, monkeypatch):  # noqa: F811
    repo, _registry, repo_id, _store, manager = runtime
    (repo / "module.py").write_text("value = 1\n")
    _commit_all(repo, "baseline")
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    _no_full_index(manager, monkeypatch)
    assert manager.sync_repository_index(repo_id).action == "up_to_date"
    # A sweep repair with nothing drifted publishes nothing heavier than an
    # empty incremental and never a full rebuild.
    assert manager.repair_repository_drift(repo_id).action == "incremental_update"


def test_pending_publication_stays_fail_closed(runtime):  # noqa: F811
    """A pending publication keeps failing closed (by design, unchanged by #145).

    The 05:56Z ``missing_provenance`` sample was taken while the bd61265
    incremental was building; queries fail closed until it publishes.
    """
    _repo, registry, repo_id, _store, manager = runtime
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    info = registry.get(repo_id)
    info.staleness_reason = "index_publication_pending"
    assert (
        ReadinessClassifier.classify_registered(info).state
        == RepositoryReadinessState.MISSING_PROVENANCE
    )


def test_sweep_deletes_stale_row_of_a_newly_ignored_file(runtime, monkeypatch):  # noqa: F811
    """A mislabeled generation's row for a now-ignored file is drift to delete.

    The raced commit adds ``generated.py`` to .gitignore, but the generation
    published under it still holds the row from before. The full walk would
    not store that file, so the sweep must report the row as a deletion.
    """
    repo, registry, repo_id, _store, manager = runtime
    for index in range(6):
        (repo / f"stable{index}.py").write_text(f"stable_value_{index} = {index}\n")
    (repo / "generated.py").write_text("generated_sentinel = 1\n")
    _commit_all(repo, "baseline")
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    store = manager._resolve_ctx(repo_id).sqlite_store
    assert store.search_code_fts("generated_sentinel")

    gitignore = repo / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    gitignore.write_text(existing + "generated.py\n")
    head = _commit_all(repo, "ignore generated.py")
    # Simulate the mislabel: the old rows are recorded as the new commit.
    registry.update_indexed_commit(repo_id, head, branch="main")

    deleted = []
    real_incremental = manager._incremental_index_update

    def spy(repo_id_arg, ctx, changes, *args, **kwargs):
        deleted.extend(changes.deleted)
        return real_incremental(repo_id_arg, ctx, changes, *args, **kwargs)

    monkeypatch.setattr(manager, "_incremental_index_update", spy)
    _no_full_index(manager, monkeypatch)
    sweeper, _marked = _sweeper_for(manager, registry, repo_id, store)

    assert sweeper.sweep_once() == [repo_id]
    assert deleted == ["generated.py"]
    store = manager._resolve_ctx(repo_id).sqlite_store
    assert not store.search_code_fts("generated_sentinel")
    assert sweeper.sweep_once() == []


def test_sweep_deletion_of_a_committed_path_rebuilds_instead(
    runtime, monkeypatch, caplog  # noqa: F811
):
    """A case-only spelling mismatch must not delete valid committed code.

    On a case-insensitive filesystem the walk can list ``module.py`` while the
    tree records ``Module.py``; the sweep then reports ``Module.py`` deleted.
    The targeted repair must refuse that deletion and rebuild from the tree.
    """
    import os as os_module

    import mcp_server.watcher.sweeper as sweeper_module

    repo, registry, repo_id, _store, manager = runtime
    for index in range(6):
        (repo / f"stable{index}.py").write_text(f"stable_value_{index} = {index}\n")
    (repo / "Module.py").write_text("case_sentinel = 1\n")
    head = _commit_all(repo, "baseline")
    assert manager.rebuild_repository_index(repo_id).action == "full_index"

    real_walk = os_module.walk

    def case_folding_walk(top, *args, **kwargs):
        for directory, children, filenames in real_walk(top, *args, **kwargs):
            yield directory, children, [
                "module.py" if name == "Module.py" else name for name in filenames
            ]

    class CaseFoldingOs:
        """``os`` for the sweeper only; the indexer keeps the real walk."""

        walk = staticmethod(case_folding_walk)

        def __getattr__(self, name):
            return getattr(os_module, name)

    monkeypatch.setattr(sweeper_module, "os", CaseFoldingOs())
    deleted = []
    real_incremental = manager._incremental_index_update

    def spy(repo_id_arg, ctx, changes, *args, **kwargs):
        deleted.extend(changes.deleted)
        return real_incremental(repo_id_arg, ctx, changes, *args, **kwargs)

    monkeypatch.setattr(manager, "_incremental_index_update", spy)
    store = manager._resolve_ctx(repo_id).sqlite_store
    sweeper, _marked = _sweeper_for(manager, registry, repo_id, store)

    with caplog.at_level(logging.INFO):
        assert sweeper.sweep_once() == [repo_id]

    assert deleted == []
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "Full rebuild of" in m and "sweep_deletion_conflicts_with_tree" in m for m in messages
    )
    assert registry.get(repo_id).last_indexed_commit == head
    assert manager._resolve_ctx(repo_id).sqlite_store.search_code_fts("case_sentinel")


def test_memory_release_is_best_effort(monkeypatch):
    assert isinstance(release_rebuild_memory("repo"), dict)
    monkeypatch.setenv("MCP_INDEX_MALLOC_TRIM", "false")
    assert release_rebuild_memory("repo")["trimmed"] is False
