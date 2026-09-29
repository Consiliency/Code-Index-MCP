"""Tests for WatcherSweeper — periodic full-tree sweep for inotify/FSEvents drop recovery."""

import hashlib
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from mcp_server.watcher.sweeper import DEFAULT_SWEEP_MINUTES, ENV_SWEEP_MINUTES, WatcherSweeper

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sqlite_store(tmp_path: Path):
    """Return a real SQLiteStore backed by a temp DB with a file entry."""
    from mcp_server.storage.sqlite_store import SQLiteStore

    db_path = tmp_path / "test.db"
    store = SQLiteStore(str(db_path))
    return store


def _store_file(store, repo_id_int: int, relative_path: str, content_hash: str = None):
    """Insert a file record directly via the store."""
    store.store_file(
        file_path=Path(f"/repo/{relative_path}"),
        language="python",
        repository_id=repo_id_int,
        relative_path=relative_path,
        content_hash=content_hash,
    )


# ---------------------------------------------------------------------------
# test_sweeper_recovers_missed_event (acceptance #5)
# ---------------------------------------------------------------------------


class TestSweeperRecoversMissedEvent:
    """Sweeper detects a file present on disk but absent from SQLite."""

    @pytest.mark.parametrize("extension", [".md", ".txt", ".pyw", ".mjs", ".yaml"])
    def test_sweep_recovers_every_supported_file_family(self, tmp_path, extension):
        root = tmp_path / "repo"
        root.mkdir()
        (root / f"missed{extension}").write_text("synthetic content\n")
        store = _make_sqlite_store(tmp_path)
        store.create_repository(str(root), "repo")
        calls = []
        sweeper = WatcherSweeper(
            on_missed_path=lambda repo, path: calls.append((repo, path)),
            repo_roots_provider=lambda: {"repo": root},
            store=store,
        )
        try:
            assert sweeper.sweep_once() == ["repo"]
            assert calls == [("repo", f"missed{extension}")]
        finally:
            store.close()

    def test_sweeper_detects_changed_content_at_an_existing_path(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "existing.py").write_text("new = 2\n")
        store = _make_sqlite_store(tmp_path)
        repo_row = store.create_repository(str(root), "repo")
        _store_file(store, repo_row, "existing.py", hashlib.sha256(b"old = 1\n").hexdigest())
        calls = []
        sweeper = WatcherSweeper(
            lambda repo, path: calls.append((repo, path)), lambda: {"repo": root}, store
        )
        try:
            assert sweeper.sweep_once() == ["repo"]
            assert calls == [("repo", "existing.py")]
        finally:
            store.close()

    def test_sweeper_recovers_missed_event(self, tmp_path):
        """Create a file without firing watchdog; sweep_once should call on_missed_path."""
        repo_id = "repo-abc"
        repo_root = tmp_path / "myrepo"
        repo_root.mkdir()

        # Write a Python file WITHOUT triggering watchdog (filesystem only)
        missed_file = repo_root / "missed.py"
        missed_file.write_text("x = 1\n")

        # SQLite store has NO record of missed.py
        store = _make_sqlite_store(tmp_path)
        # Register a repository so store knows about it
        store.create_repository(path=str(repo_root), name="myrepo")

        missed_calls = []

        def on_missed(r_id, rel_path):
            missed_calls.append((r_id, rel_path))

        sweeper = WatcherSweeper(
            on_missed_path=on_missed,
            repo_roots_provider=lambda: {repo_id: repo_root},
            store=store,
            interval_minutes=60,
        )

        drifted = sweeper.sweep_once()

        assert repo_id in drifted
        assert len(missed_calls) == 1
        assert missed_calls[0][0] == repo_id
        assert missed_calls[0][1] == "missed.py"

    def test_sweeper_recovers_missed_delete(self, tmp_path):
        repo_id = "repo-delete"
        repo_root = tmp_path / "deleterepo"
        repo_root.mkdir()

        store = _make_sqlite_store(tmp_path)
        store.create_repository(path=str(repo_root), name="deleterepo")
        _store_file(store, 1, "missing.py")

        delete_calls = []
        sweeper = WatcherSweeper(
            on_missed_path=lambda _r, _p: None,
            repo_roots_provider=lambda: {repo_id: repo_root},
            store=store,
            on_missed_delete=lambda r, p: delete_calls.append((r, p)),
            interval_minutes=60,
        )

        drifted = sweeper.sweep_once()

        assert drifted == [repo_id]
        assert delete_calls == [(repo_id, "missing.py")]

    def test_sweeper_recovers_unambiguous_rename(self, tmp_path):
        repo_id = "repo-rename"
        repo_root = tmp_path / "renamerepo"
        repo_root.mkdir()
        content = b"x = 1\n"
        content_hash = hashlib.sha256(content).hexdigest()
        (repo_root / "new.py").write_bytes(content)

        store = _make_sqlite_store(tmp_path)
        store.create_repository(path=str(repo_root), name="renamerepo")
        _store_file(store, 1, "old.py", content_hash=content_hash)

        create_calls = []
        delete_calls = []
        rename_calls = []
        sweeper = WatcherSweeper(
            on_missed_path=lambda r, p: create_calls.append((r, p)),
            repo_roots_provider=lambda: {repo_id: repo_root},
            store=store,
            on_missed_delete=lambda r, p: delete_calls.append((r, p)),
            on_missed_rename=lambda r, old, new: rename_calls.append((r, old, new)),
            interval_minutes=60,
        )

        drifted = sweeper.sweep_once()

        assert drifted == [repo_id]
        assert rename_calls == [(repo_id, "old.py", "new.py")]
        assert create_calls == []
        assert delete_calls == []

    def test_sweeper_ambiguous_rename_falls_back_to_create_delete(self, tmp_path):
        repo_id = "repo-ambiguous"
        repo_root = tmp_path / "ambiguousrepo"
        repo_root.mkdir()
        (repo_root / "new.py").write_text("x = 1\n")

        store = _make_sqlite_store(tmp_path)
        store.create_repository(path=str(repo_root), name="ambiguousrepo")
        _store_file(store, 1, "old.py")

        create_calls = []
        delete_calls = []
        rename_calls = []
        sweeper = WatcherSweeper(
            on_missed_path=lambda r, p: create_calls.append((r, p)),
            repo_roots_provider=lambda: {repo_id: repo_root},
            store=store,
            on_missed_delete=lambda r, p: delete_calls.append((r, p)),
            on_missed_rename=lambda r, old, new: rename_calls.append((r, old, new)),
            interval_minutes=60,
        )

        drifted = sweeper.sweep_once()

        assert drifted == [repo_id]
        assert rename_calls == []
        assert create_calls == [(repo_id, "new.py")]
        assert delete_calls == [(repo_id, "old.py")]


# ---------------------------------------------------------------------------
# test_sweeper_interval_env_override
# ---------------------------------------------------------------------------


class TestSweeperIntervalEnvOverride:
    """ENV_SWEEP_MINUTES overrides the default interval."""

    def test_sweeper_interval_env_override(self, monkeypatch):
        """When env var is set, WatcherSweeper uses that interval."""
        monkeypatch.setenv(ENV_SWEEP_MINUTES, "5")

        sweeper = WatcherSweeper(
            on_missed_path=lambda r, p: None,
            repo_roots_provider=lambda: {},
            store=Mock(),
        )

        assert sweeper.interval_minutes == 5


# ---------------------------------------------------------------------------
# test_sweeper_noop_when_no_drift
# ---------------------------------------------------------------------------


class TestSweeperNoopWhenNoDrift:
    """Sweeper finds no missed paths when filesystem matches SQLite."""

    def test_sweeper_noop_when_no_drift(self, tmp_path):
        """If all disk files are in SQLite, on_missed_path is never called."""
        repo_id = "repo-clean"
        repo_root = tmp_path / "cleanrepo"
        repo_root.mkdir()

        py_file = repo_root / "existing.py"
        py_file.write_text("pass\n")

        store = _make_sqlite_store(tmp_path)
        store.create_repository(path=str(repo_root), name="cleanrepo")
        # Store the file record so sweeper sees it as known
        store.store_file(
            file_path=py_file,
            language="python",
            repository_id=1,
            relative_path="existing.py",
        )

        missed_calls = []

        sweeper = WatcherSweeper(
            on_missed_path=lambda r, p: missed_calls.append((r, p)),
            repo_roots_provider=lambda: {repo_id: repo_root},
            store=store,
            interval_minutes=60,
        )

        drifted = sweeper.sweep_once()

        assert missed_calls == []
        assert drifted == []

    def test_tracked_indexer_exclusions_do_not_trigger_repeated_resync(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
        source = repo_root / "source.py"
        source.write_text("value = 1\n")
        (repo_root / "pom.xml").write_text("<project/>\n")
        (repo_root / "large.json").write_text('{"value": "more than thirty two bytes"}\n')
        (repo_root / "mcp_validation_results.json").write_text("{}\n")
        subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            cwd=repo_root,
            check=True,
        )
        monkeypatch.setenv("MCP_MAX_FILE_SIZE_BYTES", "32")

        store = _make_sqlite_store(tmp_path)
        repo_id = store.create_repository(str(repo_root), "repo")
        store.store_file(
            file_path=source,
            language="python",
            repository_id=repo_id,
            relative_path="source.py",
            content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        drift_calls = []
        sweeper = WatcherSweeper(
            on_missed_path=None,
            repo_roots_provider=lambda: {"repo": repo_root},
            store=store,
            on_repository_drift=drift_calls.append,
        )
        try:
            assert sweeper.sweep_once() == []
            assert sweeper.sweep_once() == []
            assert drift_calls == []
            store.store_file(
                file_path=repo_root / "pom.xml",
                language="xml",
                repository_id=repo_id,
                relative_path="pom.xml",
            )
            assert sweeper.sweep_once() == ["repo"]
            assert drift_calls == ["repo"]
        finally:
            store.close()

    @pytest.mark.parametrize("content", [b"value = 1\r\n", b"word = caf\xe9\n"])
    def test_dispatcher_persisted_text_hash_converges(self, tmp_path, content):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from mcp_server.core.repo_context import RepoContext
        from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher, IndexResultStatus
        from mcp_server.plugin_base import IPlugin

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        source = repo_root / "source.py"
        source.write_bytes(content)
        store = _make_sqlite_store(tmp_path)
        store.create_repository(str(repo_root), "repo")
        plugin = MagicMock(spec=IPlugin, lang="python")
        plugin.language = "python"
        plugin.supports.return_value = True
        plugin.indexFile.return_value = {"symbols": []}
        dispatcher = EnhancedDispatcher([plugin], semantic_search_enabled=False)
        ctx = RepoContext(
            repo_id="repo",
            sqlite_store=store,
            workspace_root=repo_root,
            tracked_branch="main",
            registry_entry=SimpleNamespace(path=repo_root, name="repo"),
        )
        drift_calls = []
        sweeper = WatcherSweeper(
            on_missed_path=None,
            repo_roots_provider=lambda: {"repo": repo_root},
            store=store,
            on_repository_drift=drift_calls.append,
        )
        try:
            assert dispatcher.index_file(ctx, source).status == IndexResultStatus.INDEXED
            assert sweeper.sweep_once() == []
            assert sweeper.sweep_once() == []
            assert drift_calls == []
        finally:
            dispatcher.shutdown()
            store.close()


# ---------------------------------------------------------------------------
# test_sweeper_start_stop
# ---------------------------------------------------------------------------


class TestSweeperStartStop:
    """start/stop lifecycle — sweeper thread must not leak."""

    def test_start_stop_no_error(self, tmp_path):
        store = _make_sqlite_store(tmp_path)
        sweeper = WatcherSweeper(
            on_missed_path=lambda r, p: None,
            repo_roots_provider=lambda: {},
            store=store,
            interval_minutes=60,
        )
        sweeper.start()
        assert sweeper._thread is not None
        sweeper.stop()
        sweeper._thread.join(timeout=3)
        assert not sweeper._thread.is_alive()
