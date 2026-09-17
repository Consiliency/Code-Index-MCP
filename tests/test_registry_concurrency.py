"""Tests for multi-process safe RepositoryRegistry.save() via flock."""

import json
import multiprocessing
import os
import stat
import time
from datetime import datetime
from pathlib import Path

import pytest

from mcp_server.storage.multi_repo_manager import RepositoryInfo
from mcp_server.storage.repository_registry import RepositoryRegistry


def _worker_save_many(registry_path: Path, start_idx: int, count: int) -> None:
    """Worker: register `count` repos in a fresh RepositoryRegistry and save each."""
    reg = RepositoryRegistry(registry_path=registry_path)
    for i in range(start_idx, start_idx + count):
        repo_info = RepositoryInfo(
            repository_id=f"repo_{i:04d}",
            name=f"repo_{i:04d}",
            path=registry_path.parent / f"repo_{i:04d}",
            index_path=registry_path.parent / f"repo_{i:04d}" / "index.db",
            language_stats={},
            total_files=0,
            total_symbols=0,
            indexed_at=datetime.now(),
        )
        reg.register(repo_info)


def test_save_is_flocked(tmp_path: Path):
    """Two processes each register 50 repos; after both exit all 100 must be present."""
    registry_path = tmp_path / "registry.json"

    p1 = multiprocessing.Process(target=_worker_save_many, args=(registry_path, 0, 50))
    p2 = multiprocessing.Process(target=_worker_save_many, args=(registry_path, 50, 50))
    p1.start()
    p2.start()
    p1.join(timeout=30)
    p2.join(timeout=30)

    assert p1.exitcode == 0, f"Worker 1 exited with code {p1.exitcode}"
    assert p2.exitcode == 0, f"Worker 2 exited with code {p2.exitcode}"

    final = RepositoryRegistry(registry_path=registry_path)
    assert len(final._registry) == 100, f"Expected 100 entries, got {len(final._registry)}"


def _worker_slow_replace(registry_path, entered, release):
    original_replace = Path.replace

    def paused_replace(self, target):
        entered.set()
        assert release.wait(10)
        return original_replace(self, target)

    Path.replace = paused_replace
    try:
        _worker_save_many(registry_path, 0, 1)
    finally:
        Path.replace = original_replace


def test_save_holds_lock_during_rename(tmp_path: Path):
    """The actual persistence path retains its process lock through rename."""
    import fcntl

    registry_path = tmp_path / "registry.json"
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    worker = context.Process(target=_worker_slow_replace, args=(registry_path, entered, release))
    worker.start()
    try:
        assert entered.wait(10)
        with registry_path.with_suffix(".lock").open() as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release.set()
        worker.join(10)
        assert worker.exitcode == 0
        assert len(RepositoryRegistry(registry_path).list_all()) == 1
    finally:
        release.set()
        if worker.is_alive():
            worker.terminate()
        worker.join(10)
        worker.close()


def test_save_releases_lock_on_exception(tmp_path: Path):
    """Lock must be released even when save raises; subsequent save must succeed."""
    registry_path = tmp_path / "registry.json"
    reg = RepositoryRegistry(registry_path=registry_path)

    repo_info = RepositoryInfo(
        repository_id="before_err",
        name="before_err",
        path=tmp_path / "before_err",
        index_path=tmp_path / "before_err" / "index.db",
        language_stats={},
        total_files=0,
        total_symbols=0,
        indexed_at=datetime.now(),
    )
    reg._registry["before_err"] = {
        "repository_id": "before_err",
        "name": "before_err",
        "path": tmp_path / "before_err",
        "index_path": tmp_path / "before_err" / "index.db",
        "language_stats": {},
        "total_files": 0,
        "total_symbols": 0,
        "indexed_at": datetime.now(),
        "active": True,
        "priority": 0,
    }

    import builtins

    original_json_dumps = json.dumps
    call_count = {"n": 0}

    def _raise_once(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("injected json.dumps failure")
        return original_json_dumps(*args, **kwargs)

    import mcp_server.storage.repository_registry as _mod

    original_json_mod = _mod.json

    class _PatchedJson:
        def __getattr__(self, name):
            if name == "dumps":
                return _raise_once
            return getattr(original_json_mod, name)

    _mod.json = _PatchedJson()  # type: ignore[assignment]
    try:
        try:
            reg.save()
        except Exception:
            pass

        # Restore json before the second save
        _mod.json = original_json_mod
        reg.save()
    finally:
        _mod.json = original_json_mod

    assert registry_path.exists(), "Registry file should exist after successful second save"


def test_lock_file_mode_0o600(tmp_path: Path):
    """Sidecar .lock file must have mode 0o600."""
    registry_path = tmp_path / "registry.json"
    reg = RepositoryRegistry(registry_path=registry_path)

    repo_info = RepositoryInfo(
        repository_id="mode_test",
        name="mode_test",
        path=tmp_path / "mode_test",
        index_path=tmp_path / "mode_test" / "index.db",
        language_stats={},
        total_files=0,
        total_symbols=0,
        indexed_at=datetime.now(),
    )
    reg.register(repo_info)

    lock_path = registry_path.with_suffix(".lock")
    assert lock_path.exists(), "Lock file should be created after save()"
    mode = stat.S_IMODE(os.stat(lock_path).st_mode)
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"
