"""Production committed-source and watcher reconciliation controls."""

import hashlib
import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mcp_server.core.repo_resolver import RepoResolver
from mcp_server.storage import git_index_manager
from mcp_server.storage.git_index_manager import RebuildShutdownRequested
from mcp_server.storage.store_registry import StoreRegistry
from mcp_server.watcher_multi_repo import MultiRepositoryWatcher
from tests.test_v13_data_storage import runtime


@pytest.mark.parametrize("source_bytes", [b"name = 1\r\n", b"# caf\xe9\n", b"a" * 8191 + b"\r\n"])
def test_committed_snapshot_hash_matches_indexed_text(runtime, tmp_path, monkeypatch, source_bytes):
    repo, _registry, _repo_id, _store, manager = runtime
    (repo / "hello.py").write_bytes(source_bytes)
    subprocess.run(["git", "add", "hello.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic encoded source"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    destination = tmp_path / "snapshot"
    destination.mkdir()
    original_read_bytes = Path.read_bytes

    def refuse_full_snapshot_read(path):
        if path == destination / "hello.py":
            raise AssertionError("Snapshot hashing must stream admitted content")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", refuse_full_snapshot_read)
    hashes = manager._snapshot_committed_inputs(repo, head, destination)
    normalized = source_bytes.decode("utf-8" if b"\xe9" not in source_bytes else "latin-1")
    normalized = normalized.replace("\r\n", "\n")
    assert hashes["hello.py"] == hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def test_pilot_estimate_is_offline_and_bounds_every_request_class():
    from scripts.v13_pilot_estimate import estimate

    result = estimate()
    assert result["inference_requests_made"] == 0
    assert result["repositories"] == 2
    assert result["source_chunks"] == 2
    assert result["input_token_upper_bound"] == 87138
    assert all(item["framing_input_units"] > 0 for item in result["request_envelopes"].values())
    assert result["within_approved_token_budget"] is True
    assert result["measured_quality_or_performance"] is False


def test_committed_snapshot_filters_before_reading_excluded_blobs(runtime, tmp_path, monkeypatch):
    repo, _registry, _repo_id, _original, manager = runtime
    (repo / ".gitignore").write_text("blocked/\n")
    (repo / "blocked").mkdir()
    (repo / "blocked" / "fixture.py").write_text("excluded_fixture = 1\n")
    (repo / "nested").mkdir()
    (repo / "nested" / ".gitignore").write_text("*.py\n!keep.py\n")
    (repo / "nested" / "hidden.py").write_text("excluded_fixture = 2\n")
    (repo / "nested" / "keep.py").write_text("admitted_fixture = 1\n")
    (repo / "synthetic.env").write_text("EXCLUDED_FIXTURE=not-a-secret\n")
    (repo / "link.py").symlink_to("hello.py")
    subprocess.run(["git", "add", "-f", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic ignore policy"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    excluded = {
        subprocess.run(
            ["git", "rev-parse", f"HEAD:{name}"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for name in ("blocked/fixture.py", "nested/hidden.py", "synthetic.env")
    }
    original_popen = subprocess.Popen
    read_blobs = set()

    def observe(command, **kwargs):
        if command[:3] == ["git", "cat-file", "blob"]:
            read_blobs.add(command[3])
        return original_popen(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", observe)
    destination = tmp_path / "snapshot"
    destination.mkdir()
    hashes = manager._snapshot_committed_inputs(repo, head, destination)
    assert read_blobs, "blob reads must go through the observed git runner"
    assert not excluded.intersection(read_blobs)
    assert "nested/keep.py" in hashes
    assert not (destination / "link.py").exists()
    assert not (destination / "blocked" / "fixture.py").exists()


def test_snapshot_keeps_existing_bounded_json_exception(runtime, tmp_path, monkeypatch):
    repo, _registry, _repo_id, _store, manager = runtime
    (repo / ".devcontainer").mkdir()
    (repo / ".devcontainer" / "devcontainer.json").write_text('{"name":"' + "fixture" * 20 + '"}')
    (repo / "ordinary.json").write_text('{"name":"' + "fixture" * 20 + '"}')
    subprocess.run(["git", "add", ".devcontainer", "ordinary.json"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic bounded input"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    monkeypatch.setattr("mcp_server.storage.git_index_manager.get_max_file_size_bytes", lambda: 32)
    destination = tmp_path / "bounded-snapshot"
    destination.mkdir()
    hashes = manager._snapshot_committed_inputs(repo, head, destination)
    assert ".devcontainer/devcontainer.json" in hashes
    assert "ordinary.json" not in hashes


def test_committed_snapshot_honors_shutdown_between_blobs(runtime, tmp_path):
    """#112: a shutdown request stops the input copy before the next blob."""
    repo, _registry, _repo_id, _store, manager = runtime
    for index in range(4):
        (repo / f"module_{index}.py").write_text(f"value_{index} = {index}\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic inputs"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    original_checkpoint = manager._rebuild_checkpoint
    stages = []

    def checkpoint(stage):
        stages.append(stage)
        # The listing checkpoint, then one per blob: stop before the second blob.
        if stages.count("snapshot_inputs") == 3:
            manager.request_shutdown()
        original_checkpoint(stage)

    manager._rebuild_checkpoint = checkpoint
    destination = tmp_path / "snapshot"
    destination.mkdir()

    with pytest.raises(RebuildShutdownRequested, match="snapshot_inputs"):
        manager._snapshot_committed_inputs(repo, head, destination)

    copied = [path for path in destination.rglob("*") if path.is_file()]
    assert len(copied) == 1


class _HungGit:
    """Popen stand-in whose output never arrives."""

    instances = []
    by_pid = {}

    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.killed = False
        self.returncode = None
        self.waits = []
        self.pid = 1_000_000 + len(_HungGit.instances)
        _HungGit.instances.append(self)
        _HungGit.by_pid[self.pid] = self

    def communicate(self, timeout=None):
        if self.killed:
            self.returncode = -9
            return b"", b""
        self.waits.append(timeout)
        raise subprocess.TimeoutExpired(self.command, timeout)

    def kill(self):
        self.killed = True


def _install_hung_git(monkeypatch):
    _HungGit.instances.clear()
    _HungGit.by_pid.clear()
    monkeypatch.setattr(subprocess, "Popen", _HungGit)
    # Stopping git kills its whole process group; route that to the fake.
    monkeypatch.setattr(os, "killpg", lambda pid, sig: _HungGit.by_pid[pid].kill(), raising=False)


@pytest.mark.parametrize(("configured", "expected"), [(None, 300), ("42", 42), ("1", 10)])
def test_git_timeout_comes_from_environment_and_kills_git(
    runtime, monkeypatch, configured, expected
):
    _repo, _registry, _repo_id, _store, manager = runtime
    if configured is None:
        monkeypatch.delenv("MCP_INDEX_GIT_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("MCP_INDEX_GIT_TIMEOUT_SECONDS", configured)
    clock = iter(float(tick) for tick in range(0, 10_000, 5))
    monkeypatch.setattr(git_index_manager, "_monotonic", lambda: next(clock))
    _install_hung_git(monkeypatch)

    with pytest.raises(subprocess.TimeoutExpired) as timed_out:
        manager._run_git(["ls-tree", "-r", "HEAD"], Path("."))

    assert timed_out.value.timeout == expected
    hung = _HungGit.instances[-1]
    assert hung.killed
    assert set(hung.waits) == {git_index_manager._GIT_POLL_SECONDS}
    # One poll per 5 s fake tick until the deadline passes.
    assert len(hung.waits) == expected // 5 + (expected % 5 > 0)


def test_git_runner_kills_git_on_shutdown(runtime, monkeypatch):
    _repo, _registry, _repo_id, _store, manager = runtime
    monkeypatch.setattr(git_index_manager, "_monotonic", lambda: 0.0)
    _install_hung_git(monkeypatch)
    manager.request_shutdown()

    with pytest.raises(RebuildShutdownRequested, match="during git cat-file"):
        manager._run_git(["cat-file", "blob", "0" * 40], Path("."))

    hung = _HungGit.instances[-1]
    assert hung.killed
    assert len(hung.waits) == 1
    if os.name != "nt":
        assert hung.kwargs.get("start_new_session") is True


def _run_shell_as_git(monkeypatch, script, *, ready_file=None):
    """Make ``_run_git`` start ``sh -c script`` with the git Popen arguments."""
    original_popen = subprocess.Popen

    def popen(command, **kwargs):
        process = original_popen(["sh", "-c", script], **kwargs)
        if ready_file is not None:
            deadline = time.monotonic() + 10
            while not ready_file.exists() or not ready_file.read_text().strip():
                assert time.monotonic() < deadline, "fixture process did not start"
                time.sleep(0.01)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)


class _ExitedGit:
    pid = 4242

    def __init__(self):
        self.killed = False
        self.stdout = None
        self.stderr = None

    def kill(self):
        self.killed = True

    def communicate(self, timeout=None):
        return b"", b""


@pytest.mark.parametrize("taskkill_error", [None, FileNotFoundError, subprocess.TimeoutExpired])
def test_windows_stop_tries_taskkill_tree_then_kills_git(monkeypatch, taskkill_error):
    """Windows is best effort: ``taskkill /T /F`` for the tree, then ``kill()``."""
    from mcp_server.storage.git_index_manager import GitAwareIndexManager

    monkeypatch.setattr(git_index_manager, "_WINDOWS", True)
    monkeypatch.setattr(
        os, "killpg", MagicMock(side_effect=AssertionError("no killpg")), raising=False
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if taskkill_error is FileNotFoundError:
            raise FileNotFoundError("taskkill")
        if taskkill_error is subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    process = _ExitedGit()

    GitAwareIndexManager._stop_git(process, "fetch")

    assert [command for command, _kwargs in calls] == [["taskkill", "/T", "/F", "/PID", "4242"]]
    kwargs = calls[0][1]
    assert kwargs["timeout"] == git_index_manager._GIT_TASKKILL_SECONDS
    assert kwargs["check"] is False
    assert process.killed


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_git_shutdown_kills_descendants_holding_the_pipes(runtime, tmp_path, monkeypatch):
    """#140 review: a helper git spawned must not keep the reap blocked."""
    _repo, _registry, _repo_id, _store, manager = runtime
    ready = tmp_path / "helper.pid"
    monkeypatch.setattr(git_index_manager, "_GIT_REAP_SECONDS", 20.0)
    _run_shell_as_git(monkeypatch, f"sleep 30 & echo $! > '{ready}'; wait", ready_file=ready)
    manager.request_shutdown()

    started = time.monotonic()
    with pytest.raises(RebuildShutdownRequested):
        manager._run_git(["fetch"], tmp_path)

    # The helper shared the pipes; killing the group released them at once.
    assert time.monotonic() - started < 10


@pytest.mark.skipif(os.name == "nt" or shutil.which("setsid") is None, reason="needs POSIX setsid")
def test_git_reap_is_bounded_when_an_escaped_descendant_holds_the_pipes(
    runtime, tmp_path, monkeypatch, caplog
):
    _repo, _registry, _repo_id, _store, manager = runtime
    escaped = tmp_path / "escaped.pid"
    monkeypatch.setattr(git_index_manager, "_GIT_REAP_SECONDS", 0.5)
    _run_shell_as_git(
        monkeypatch,
        f"setsid sh -c 'echo $$ > \"{escaped}\"; exec sleep 30' & sleep 30",
        ready_file=escaped,
    )
    manager.request_shutdown()
    caplog.set_level(logging.WARNING, logger="mcp_server.storage.git_index_manager")
    try:
        started = time.monotonic()
        with pytest.raises(RebuildShutdownRequested):
            manager._run_git(["fetch"], tmp_path)
        assert time.monotonic() - started < 10
        assert "did not release its output" in caplog.text
    finally:
        try:
            os.kill(int(escaped.read_text().strip()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def test_committed_snapshot_reports_no_inflated_total_when_stopped_early(runtime, tmp_path):
    """#140 re-review: stopping after the policy blobs must not leave a candidate total."""
    repo, _registry, _repo_id, _store, manager = runtime
    (repo / ".gitignore").write_text("blocked/\n")
    (repo / "blocked").mkdir()
    for index in range(3):
        (repo / "blocked" / f"fixture_{index}.py").write_text(f"excluded_{index} = 1\n")
    (repo / "kept.py").write_text("kept = 1\n")
    subprocess.run(["git", "add", "-f", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic policy"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    complete = tmp_path / "complete"
    complete.mkdir()
    admitted = len(manager._snapshot_committed_inputs(repo, head, complete))
    original_checkpoint = manager._rebuild_checkpoint
    checkpoints = []

    def checkpoint(stage):
        checkpoints.append(stage)
        # Listing, then the root .gitignore blob, then the first ordinary blob.
        if len(checkpoints) == 3:
            manager.request_shutdown()
        original_checkpoint(stage)

    manager._rebuild_checkpoint = checkpoint
    progress = []
    destination = tmp_path / "stopped"
    destination.mkdir()

    with pytest.raises(RebuildShutdownRequested):
        manager._snapshot_committed_inputs(
            repo, head, destination, on_progress=lambda done, total: progress.append((done, total))
        )

    assert progress == [(1, admitted)]


def test_committed_snapshot_progress_total_counts_only_admitted_blobs(runtime, tmp_path):
    repo, _registry, _repo_id, _store, manager = runtime
    (repo / ".gitignore").write_text("blocked/\n")
    (repo / "blocked").mkdir()
    (repo / "blocked" / "fixture.py").write_text("excluded_fixture = 1\n")
    (repo / "kept.py").write_text("kept = 1\n")
    subprocess.run(["git", "add", "-f", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic policy"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    destination = tmp_path / "snapshot"
    destination.mkdir()
    progress = []

    hashes = manager._snapshot_committed_inputs(
        repo, head, destination, on_progress=lambda done, total: progress.append((done, total))
    )

    assert "blocked/fixture.py" not in hashes
    assert progress[-1] == (len(hashes), len(hashes))


@pytest.mark.parametrize("operation", ["modify", "rename", "delete"])
def test_incremental_committed_mutation_updates_content_and_keeps_move_identity(runtime, operation):
    repo, _registry, repo_id, _original, manager = runtime
    source = repo / "hello.py"
    content = "\n".join(f"number_{i} = {i}" for i in range(40)) + "\noldsentinel = 1\n"
    source.write_text(content)
    for i in range(8):
        (repo / f"unchanged{i}.py").write_text(f"unchanged_{i} = {i}\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic incremental base"], cwd=repo, check=True)
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    old_id = manager._resolve_ctx(repo_id).sqlite_store.get_file_id_by_path("hello.py")
    if operation == "delete":
        source.unlink()
    else:
        if operation == "rename":
            target = repo / "renamed.py"
            source.rename(target)
            source = target
        source.write_text(content.replace("oldsentinel", "newsentinel"))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic incremental change"], cwd=repo, check=True)
    result = manager.sync_repository_index(repo_id)
    assert result.action == "incremental_update", result.error
    store = manager._resolve_ctx(repo_id).sqlite_store
    assert not store.search_code_fts("oldsentinel")
    if operation != "delete":
        assert store.search_code_fts("newsentinel")
        assert store.get_file_id_by_path(source.name) == old_id
    if operation == "rename":
        assert store.get_file_id_by_path("hello.py") is None
        with store._get_connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM file_moves").fetchone()[0] == 1


def test_sweep_repairs_existing_path_drift_once_and_ignores_untracked(runtime):
    repo, registry, repo_id, _original, manager = runtime
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    stores = StoreRegistry.for_registry(registry)
    manager.store_registry = stores
    resolver = RepoResolver(registry, stores)
    manager.repo_resolver = resolver
    watcher = MultiRepositoryWatcher(registry, manager.dispatcher, manager, repo_resolver=resolver)
    try:
        first = registry.get(repo_id).index_generation
        active = stores.get(repo_id)
        with active._get_connection() as connection:
            connection.execute("UPDATE files SET content_hash='missed-update'")
        assert watcher.sweeper.sweep_once() == [repo_id]
        assert registry.get(repo_id).index_generation != first
        assert watcher.sweeper.sweep_once() == []
        (repo / "untracked.py").write_text("untracked = 1\n")
        assert watcher.sweeper.sweep_once() == []
    finally:
        watcher.executor.shutdown(wait=True)


def test_sweep_refuses_dirty_source_then_reconciles_committed_change(runtime):
    repo, registry, repo_id, _original, manager = runtime
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    manager.store_registry = StoreRegistry.for_registry(registry)
    resolver = RepoResolver(registry, manager.store_registry)
    manager.repo_resolver = resolver
    watcher = MultiRepositoryWatcher(registry, manager.dispatcher, manager, repo_resolver=resolver)
    try:
        original = registry.get(repo_id).index_generation
        (repo / "hello.py").write_text("committedchange = 42\n")
        watcher.sweeper.sweep_once()
        assert registry.get(repo_id).index_generation == original
        subprocess.run(["git", "add", "hello.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "Synthetic change"], cwd=repo, check=True)
        watcher.sweeper.sweep_once()
        assert registry.get(repo_id).index_generation != original
        assert manager.store_registry.get(repo_id).search_code_fts("committedchange")
    finally:
        watcher.executor.shutdown(wait=True)


def test_single_repository_watcher_uses_generation_owner(runtime, monkeypatch):
    from mcp_server.watcher.file_watcher import _Handler

    repo, registry, repo_id, _original, manager = runtime
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    ctx = manager._resolve_ctx(repo_id)
    handler = _Handler(manager.dispatcher, ctx=ctx, index_manager=manager)
    try:
        before = registry.get(repo_id).index_generation
        (repo / "hello.py").write_text("updatedvalue = 2\n")
        handler.trigger_reindex(repo / "hello.py")
        assert registry.get(repo_id).index_generation == before
        subprocess.run(["git", "add", "hello.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "Synthetic watcher change"], cwd=repo, check=True)
        handler.trigger_reindex(repo / "hello.py")
        assert registry.get(repo_id).index_generation != before
        assert manager._resolve_ctx(repo_id).sqlite_store.search_code_fts("updatedvalue")
    finally:
        handler.stop()
