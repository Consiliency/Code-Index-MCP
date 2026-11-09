"""Tests for WatcherSweeper — periodic full-tree sweep for inotify/FSEvents drop recovery."""

import hashlib
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from mcp_server.watcher.sweeper import DEFAULT_SWEEP_MINUTES, ENV_SWEEP_MINUTES, WatcherSweeper


@pytest.mark.parametrize(
    ("content", "normalized"),
    [(b"a\r\nb\rc\n", "a\nb\nc\n"), (b"caf\xe9\r\n", "caf\u00e9\n")],
)
def test_sweep_hash_streams_normalized_source(tmp_path, monkeypatch, content, normalized):
    source = tmp_path / "source.py"
    source.write_bytes(content)
    sweeper = WatcherSweeper(
        on_missed_path=None,
        repo_roots_provider=lambda: {},
        store=None,
    )

    def refuse_read_bytes(_path):
        raise AssertionError("Sweeper materialized the source file")

    monkeypatch.setattr(Path, "read_bytes", refuse_read_bytes)
    assert sweeper._hash_file(source) == hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("change", ["modify", "delete"])
def test_sweep_skips_uncommitted_tracked_changes(tmp_path, change):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    source = repo_root / "source.py"
    source.write_text("value = 1\n")
    subprocess.run(["git", "add", "source.py"], cwd=repo_root, check=True)
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
    store = _make_sqlite_store(tmp_path)
    repo_id = store.create_repository(str(repo_root), "repo")
    store.store_file(
        file_path=source,
        language="python",
        repository_id=repo_id,
        relative_path="source.py",
        content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    if change == "modify":
        source.write_text("value = 2\n")
    else:
        source.unlink()
    drift_calls = []
    sweeper = WatcherSweeper(
        None,
        lambda: {"repo": repo_root},
        store,
        on_repository_drift=drift_calls.append,
    )
    try:
        assert sweeper.sweep_once() == []
        assert drift_calls == []
    finally:
        store.close()


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

    @pytest.mark.parametrize("extension", [".md", ".pyw", ".mjs"])
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

    @pytest.mark.parametrize(
        "name",
        [
            "notes.txt",
            "ci.yml",
            "compose.yaml",
            "post_create.sh",
            "settings.json",
            "a.toml",
            "x.cfg",
        ],
    )
    def test_sweep_ignores_files_the_indexer_never_stores(self, tmp_path, monkeypatch, name):
        """Disabled-by-default and unsupported files are not drift (#114)."""
        monkeypatch.delenv("MCP_PLUGIN_SANDBOX_DISABLE", raising=False)
        root = tmp_path / "repo"
        root.mkdir()
        (root / name).write_text("synthetic: content\n")
        store = _make_sqlite_store(tmp_path)
        store.create_repository(str(root), "repo")
        calls = []
        drift = []
        sweeper = WatcherSweeper(
            on_missed_path=lambda repo, path: calls.append((repo, path)),
            repo_roots_provider=lambda: {"repo": root},
            store=store,
        )
        drift_sweeper = WatcherSweeper(
            None, lambda: {"repo": root}, store, on_repository_drift=drift.append
        )
        try:
            assert sweeper.sweep_once() == []
            assert drift_sweeper.sweep_once() == []
            assert calls == [] and drift == []
        finally:
            store.close()

    def test_sweep_follows_plugin_enablement(self, tmp_path, monkeypatch):
        """With the sandbox disabled YAML is indexed, so a missing YAML row is drift."""
        monkeypatch.setenv("MCP_PLUGIN_SANDBOX_DISABLE", "1")
        root = tmp_path / "repo"
        root.mkdir()
        (root / "ci.yaml").write_text("key: value\n")
        store = _make_sqlite_store(tmp_path)
        store.create_repository(str(root), "repo")
        calls = []
        sweeper = WatcherSweeper(
            lambda repo, path: calls.append((repo, path)), lambda: {"repo": root}, store
        )
        try:
            assert sweeper.sweep_once() == ["repo"]
            assert calls == [("repo", "ci.yaml")]
        finally:
            store.close()

    def test_sweep_uses_the_injected_admission_predicate(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "kept.py").write_text("a = 1\n")
        (root / "skipped.py").write_text("b = 2\n")
        store = _make_sqlite_store(tmp_path)
        store.create_repository(str(root), "repo")
        calls = []
        seen = []

        def admission(repo_id, path, repo_root, is_excluded, size_bytes):
            seen.append((repo_id, path.name, repo_root))
            return "test_rejected" if path.name == "skipped.py" else None

        sweeper = WatcherSweeper(
            lambda repo, path: calls.append(path),
            lambda: {"repo": root},
            store,
            admission_reason=admission,
        )
        try:
            assert sweeper.sweep_once() == ["repo"]
            assert calls == ["kept.py"]
            assert {item[1] for item in seen} == {"kept.py", "skipped.py"}
            assert all(item[0] == "repo" and item[2] == root for item in seen)
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

    def test_tracked_crlf_checkout_uses_committed_size_limit(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
        subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=repo_root, check=True)
        source = repo_root / "source.py"
        source.write_bytes(b"a\nb\nc\n")
        subprocess.run(["git", "add", "source.py"], cwd=repo_root, check=True)
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
        source.unlink()
        subprocess.run(["git", "checkout-index", "--", "source.py"], cwd=repo_root, check=True)
        assert source.read_bytes() == b"a\r\nb\r\nc\r\n"
        monkeypatch.setenv("MCP_MAX_FILE_SIZE_BYTES", "7")

        store = _make_sqlite_store(tmp_path)
        repo_id = store.create_repository(str(repo_root), "repo")
        store.store_file(
            file_path=source,
            language="python",
            repository_id=repo_id,
            relative_path="source.py",
            content_hash=hashlib.sha256(b"a\nb\nc\n").hexdigest(),
        )
        drift_calls = []
        sweeper = WatcherSweeper(
            None, lambda: {"repo": repo_root}, store, on_repository_drift=drift_calls.append
        )
        try:
            assert sweeper.sweep_once() == []
            assert sweeper.sweep_once() == []
            assert drift_calls == []
        finally:
            store.close()

    @pytest.mark.parametrize("limit", [8, 100])
    def test_tracked_smudge_filter_uses_committed_content(self, tmp_path, monkeypatch, limit):
        import shutil

        if shutil.which("sed") is None:
            pytest.skip("Git fixture requires sed")
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
        subprocess.run(
            ["git", "config", "filter.demo.clean", "sed 's/SMUDGED-LARGE-CONTENT/RAW/g'"],
            cwd=repo_root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "filter.demo.smudge", "sed 's/RAW/SMUDGED-LARGE-CONTENT/g'"],
            cwd=repo_root,
            check=True,
        )
        (repo_root / ".gitattributes").write_text("*.py filter=demo\n")
        source = repo_root / "source.py"
        source.write_bytes(b"RAW\n")
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
        source.unlink()
        subprocess.run(["git", "checkout-index", "--", "source.py"], cwd=repo_root, check=True)
        assert source.read_bytes() == b"SMUDGED-LARGE-CONTENT\n"
        assert subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=repo_root).returncode == 0
        monkeypatch.setenv("MCP_MAX_FILE_SIZE_BYTES", str(limit))

        store = _make_sqlite_store(tmp_path)
        repo_id = store.create_repository(str(repo_root), "repo")
        store.store_file(
            file_path=source,
            language="python",
            repository_id=repo_id,
            relative_path="source.py",
            content_hash=hashlib.sha256(b"RAW\n").hexdigest(),
        )
        drift_calls = []
        sweeper = WatcherSweeper(
            None, lambda: {"repo": repo_root}, store, on_repository_drift=drift_calls.append
        )
        try:
            assert sweeper.sweep_once() == []
            assert sweeper.sweep_once() == []
            assert drift_calls == []
        finally:
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


# ---------------------------------------------------------------------------
# Drift backoff and retained rows (Consiliency/Code-Index-MCP#114)
# ---------------------------------------------------------------------------


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )


def _committed_repo(tmp_path: Path, files: dict) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    for name, content in files.items():
        (repo_root / name).write_text(content)
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-qm", "fixture")
    return repo_root


def _normalized_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestSweeperDriftBackoff:
    def test_unfixed_drift_is_reconciled_once_until_the_tree_changes(self, tmp_path, caplog):
        repo_root = _committed_repo(tmp_path, {"a.py": "a = 1\n"})
        store = _make_sqlite_store(tmp_path)
        repo_row = store.create_repository(str(repo_root), "repo")
        drift_calls = []
        # The reconciliation succeeds but does not fix the drift (a futile rebuild).
        sweeper = WatcherSweeper(
            None,
            lambda: {"repo": repo_root},
            store,
            on_repository_drift=lambda repo: drift_calls.append(repo) or True,
        )
        try:
            assert sweeper.sweep_once() == ["repo"]
            with caplog.at_level("WARNING", logger="mcp_server.watcher.sweeper"):
                assert sweeper.sweep_once() == []
                assert sweeper.sweep_once() == []
            assert drift_calls == ["repo"]
            suppressed = [r for r in caplog.records if "survived reconciliation" in r.message]
            assert len(suppressed) == 1

            # A new commit changes the tree: the sweep reconciles again.
            (repo_root / "a.py").write_text("a = 2\n")
            _git(repo_root, "commit", "-qam", "change")
            assert sweeper.sweep_once() == ["repo"]
            assert drift_calls == ["repo", "repo"]

            # Once the index matches, the remembered drift is cleared ...
            _store_file(store, repo_row, "a.py", _normalized_hash("a = 2\n"))
            assert sweeper.sweep_once() == []
            assert sweeper._acted_drift == {}
            # ... so the same drift reappearing later is reconciled again.
            with store._get_connection() as conn:
                conn.execute("DELETE FROM files WHERE relative_path = 'a.py'")
            assert sweeper.sweep_once() == ["repo"]
            assert drift_calls == ["repo", "repo", "repo"]
        finally:
            store.close()

    def test_changed_drift_content_is_reconciled_without_a_commit(self, tmp_path):
        repo_root = _committed_repo(tmp_path, {"a.py": "a = 1\n"})
        store = _make_sqlite_store(tmp_path)
        repo_row = store.create_repository(str(repo_root), "repo")
        _store_file(store, repo_row, "a.py", _normalized_hash("stale = 0\n"))
        drift_calls = []
        sweeper = WatcherSweeper(
            None,
            lambda: {"repo": repo_root},
            store,
            on_repository_drift=lambda repo: drift_calls.append(repo) or True,
        )
        try:
            assert sweeper.sweep_once() == ["repo"]
            assert sweeper.sweep_once() == []
            # The indexed side changed (for example a partial reconciliation).
            with store._get_connection() as conn:
                conn.execute(
                    "UPDATE files SET content_hash = ? WHERE relative_path = 'a.py'",
                    (_normalized_hash("other = 3\n"),),
                )
            assert sweeper.sweep_once() == ["repo"]
            assert drift_calls == ["repo", "repo"]
        finally:
            store.close()

    def test_reconciliation_that_raised_is_retried_on_the_next_sweep(self, tmp_path):
        repo_root = _committed_repo(tmp_path, {"a.py": "a = 1\n"})
        store = _make_sqlite_store(tmp_path)
        store.create_repository(str(repo_root), "repo")
        attempts = []

        def flaky_reconcile(repo_id):
            attempts.append(repo_id)
            if len(attempts) == 1:
                raise TimeoutError("writer lock busy")
            return True

        sweeper = WatcherSweeper(
            None, lambda: {"repo": repo_root}, store, on_repository_drift=flaky_reconcile
        )
        try:
            with pytest.raises(TimeoutError):
                sweeper.sweep_once()
            assert sweeper.sweep_once() == ["repo"]
            assert sweeper.sweep_once() == []
            assert attempts == ["repo", "repo"]
        finally:
            store.close()

    def test_failed_sync_is_retried_not_suppressed(self, tmp_path, caplog):
        """#114 review B2 (probe_backoff_failed_sync), real reconcile callback."""
        from types import SimpleNamespace

        from mcp_server.storage.git_index_manager import IndexSyncResult
        from mcp_server.watcher_multi_repo import MultiRepositoryWatcher

        repo_root = _committed_repo(tmp_path, {"a.py": "a = 1\n"})
        store = _make_sqlite_store(tmp_path)
        store.create_repository(str(repo_root), "repo")
        attempts = []

        def sync(repo_id, **_kwargs):
            attempts.append(repo_id)
            if len(attempts) == 1:  # transient failure, e.g. a storage outage
                return IndexSyncResult(action="failed", commit="x", error="Staged rebuild failed")
            return IndexSyncResult(action="full_index", commit="x")

        watcher = SimpleNamespace(
            registry=SimpleNamespace(
                get_repository=lambda _r: SimpleNamespace(
                    auto_sync=True, active=True, registration_id="reg"
                )
            ),
            index_manager=SimpleNamespace(sync_repository_index=sync),
            mark_repository_changed=lambda _r: None,
        )
        sweeper = WatcherSweeper(
            None,
            lambda: {"repo": repo_root},
            store,
            on_repository_drift=lambda repo_id: MultiRepositoryWatcher._reconcile_repository_drift(
                watcher, repo_id
            ),
        )
        try:
            with caplog.at_level("WARNING", logger="mcp_server.watcher.sweeper"):
                assert sweeper.sweep_once() == ["repo"]  # failed
                assert sweeper.sweep_once() == ["repo"]  # retried, succeeds
                assert sweeper.sweep_once() == []  # identical drift now suppressed
            assert attempts == ["repo", "repo"]
            assert any("did not succeed" in r.message for r in caplog.records)
        finally:
            store.close()

    def test_persistent_failures_back_off_exponentially_with_a_cap(self, tmp_path):
        repo_root = _committed_repo(tmp_path, {"a.py": "a = 1\n"})
        store = _make_sqlite_store(tmp_path)
        store.create_repository(str(repo_root), "repo")
        attempts = []
        sweeper = WatcherSweeper(
            None,
            lambda: {"repo": repo_root},
            store,
            on_repository_drift=lambda repo: attempts.append(repo) or False,
        )
        try:
            pattern = [bool(sweeper.sweep_once()) for _ in range(60)]
        finally:
            store.close()
        attempted_at = [index for index, attempted in enumerate(pattern) if attempted]
        gaps = [b - a for a, b in zip(attempted_at, attempted_at[1:])]
        assert gaps[:5] == [1, 2, 4, 8, 16]
        assert set(gaps[5:]) == {16}

    def test_refused_or_disabled_reconcile_is_retried(self, tmp_path):
        """A repository whose auto-sync is off returns False and is retried later."""
        from types import SimpleNamespace

        from mcp_server.watcher_multi_repo import MultiRepositoryWatcher

        repo_root = _committed_repo(tmp_path, {"a.py": "a = 1\n"})
        store = _make_sqlite_store(tmp_path)
        store.create_repository(str(repo_root), "repo")
        state = {"auto_sync": False}
        syncs = []
        watcher = SimpleNamespace(
            registry=SimpleNamespace(
                get_repository=lambda _r: SimpleNamespace(
                    auto_sync=state["auto_sync"], active=True, registration_id="reg"
                )
            ),
            index_manager=SimpleNamespace(
                sync_repository_index=lambda repo_id, **_k: syncs.append(repo_id)
                or SimpleNamespace(action="full_index")
            ),
            mark_repository_changed=lambda _r: None,
        )
        sweeper = WatcherSweeper(
            None,
            lambda: {"repo": repo_root},
            store,
            on_repository_drift=lambda repo_id: MultiRepositoryWatcher._reconcile_repository_drift(
                watcher, repo_id
            ),
        )
        try:
            sweeper.sweep_once()
            assert syncs == []
            state["auto_sync"] = True
            sweeper.sweep_once()  # retry slot after one failure is the next sweep
            assert syncs == ["repo"]
        finally:
            store.close()

    def test_per_file_callbacks_back_off_too(self, tmp_path):
        repo_root = _committed_repo(tmp_path, {"a.py": "a = 1\n"})
        store = _make_sqlite_store(tmp_path)
        store.create_repository(str(repo_root), "repo")
        calls = []
        sweeper = WatcherSweeper(
            lambda repo, path: calls.append(path), lambda: {"repo": repo_root}, store
        )
        try:
            assert sweeper.sweep_once() == ["repo"]
            assert sweeper.sweep_once() == []
            assert calls == ["a.py"]
        finally:
            store.close()


def test_rows_a_rebuild_retains_are_not_delete_drift(tmp_path):
    repo_root = _committed_repo(tmp_path, {"a.py": "a = 1\n"})
    store = _make_sqlite_store(tmp_path)
    repo_row = store.create_repository(str(repo_root), "repo")
    _store_file(store, repo_row, "a.py", _normalized_hash("a = 1\n"))
    _store_file(store, repo_row, "imported/history.md", _normalized_hash("issue\n"))
    with store._get_connection() as conn:
        file_id = conn.execute(
            "SELECT id FROM files WHERE relative_path = 'imported/history.md'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO code_chunks (file_id, content, content_start, content_end, "
            "line_start, line_end, chunk_id, node_id, treesitter_file_id, chunk_type) "
            "VALUES (?, 'issue', 0, 5, 1, 1, 'history:1', 'n', 't', 'document')",
            (file_id,),
        )
    assert store.get_retained_relative_paths(repo_row) == ["imported/history.md"]
    drift_calls = []
    sweeper = WatcherSweeper(
        None, lambda: {"repo": repo_root}, store, on_repository_drift=drift_calls.append
    )
    try:
        assert sweeper.sweep_once() == []
        assert drift_calls == []
    finally:
        store.close()
