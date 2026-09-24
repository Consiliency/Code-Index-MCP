"""Audit counterexamples for d4e09c5; intentionally fail before remediation.

Run explicitly from the repository root with PYTHONPATH set to the checkout.
This file is evidence, outside the normal tests/ discovery tree.
"""

import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_server.core.repo_resolver import RepoResolver
from mcp_server.health.repository_readiness import ReadinessClassifier
from mcp_server.storage.connection_pool import ConnectionPool
from mcp_server.storage.repository_registry import RepositoryRegistry
from mcp_server.storage.sqlite_store import SQLiteStore
from mcp_server.storage.store_registry import StoreRegistry


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def registered(tmp_path, object_format="sha1"):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main", f"--object-format={object_format}")
    git(repo, "config", "user.email", "audit@example.invalid")
    git(repo, "config", "user.name", "Audit")
    (repo / "sample.py").write_text("def original():\n    return 1\n")
    git(repo, "add", "sample.py")
    git(repo, "commit", "-m", "fixture")
    registry = RepositoryRegistry(tmp_path / "registry.json")
    rid = registry.register_repository(str(repo), artifact_enabled=False)
    info = registry.get(rid)
    info.index_path.parent.mkdir(exist_ok=True)
    seed_db(info.index_path, repo, "sample.py")
    registry.update_indexed_commit(rid, git(repo, "rev-parse", "HEAD"), branch="main")
    return repo, registry, rid


def seed_db(path, repo, name):
    store = SQLiteStore(str(path))
    row = store.ensure_repository_row(repo)
    fid = store.store_file(row, path=repo / name, relative_path=name, language="python")
    store.store_chunk(
        file_id=fid,
        content="def original(): pass",
        content_start=0,
        content_end=20,
        line_start=1,
        line_end=1,
        chunk_id=name,
        node_id=name,
        treesitter_file_id=name,
        language="python",
    )
    store.close()


def test_unregister_survives_reload(tmp_path):
    _, registry, rid = registered(tmp_path)
    assert registry.unregister_repository(rid)
    assert rid not in json.loads(registry.registry_path.read_text())


def test_stale_writer_preserves_new_provenance(tmp_path):
    _, first, rid = registered(tmp_path)
    second = RepositoryRegistry(first.registry_path)
    first.update_staleness_reason(rid, "index_publication_pending")
    second.update_priority(rid, 7)
    assert (
        RepositoryRegistry(first.registry_path).get(rid).staleness_reason
        == "index_publication_pending"
    )


def test_server_observes_external_registry_changes(tmp_path):
    _, first, rid = registered(tmp_path)
    second = RepositoryRegistry(first.registry_path)
    first.update_staleness_reason(rid, "index_publication_pending")
    assert second.get(rid).staleness_reason == "index_publication_pending"


def test_cached_store_reopens_after_replacement(tmp_path):
    repo, registry, rid = registered(tmp_path)
    stores = StoreRegistry(registry)
    resolver = RepoResolver(registry, stores)
    original = resolver.resolve_ready(str(repo))
    assert original is not None
    with original.sqlite_store._get_connection() as conn:
        assert conn.execute("SELECT relative_path FROM files").fetchone()[0] == "sample.py"
    stage = tmp_path / "stage.db"
    seed_db(stage, repo, "replacement.py")
    stage.replace(registry.get(rid).index_path)
    try:
        reopened = resolver.resolve_ready(str(repo))
        assert reopened is not None
        with reopened.sqlite_store._get_connection() as conn:
            assert conn.execute("SELECT relative_path FROM files").fetchone()[0] == "replacement.py"
    finally:
        stores.shutdown()


def test_sha256_readiness_checks_live_branch(tmp_path):
    repo, registry, rid = registered(tmp_path, "sha256")
    assert ReadinessClassifier.classify_registered(registry.get(rid)).ready
    git(repo, "checkout", "-b", "feature")
    assert ReadinessClassifier.classify_registered(registry.get(rid)).state.value == "wrong_branch"


def test_pool_shutdown_unblocks_waiter():
    pool = ConnectionPool(lambda: sqlite3.connect(":memory:", check_same_thread=False), size=1)
    entered = threading.Event()
    finished = threading.Event()
    original_get = pool._pool.get

    def observed_get(*args, **kwargs):
        entered.set()
        return original_get(*args, **kwargs)

    def waiter():
        try:
            with pool.acquire():
                pass
        except RuntimeError:
            pass
        finally:
            finished.set()

    with pool.acquire():
        pool._pool.get = observed_get
        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        assert entered.wait(1)
        pool.close_all()
    assert finished.wait(0.2), "waiter remains blocked on an empty closed queue"


def test_sandbox_pathlib_enforces_read_roots(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("nonsecret-audit-sentinel")
    script = """
import builtins, sys, tempfile
from pathlib import Path
from mcp_server.sandbox.capabilities import DEFAULT_DENY, SandboxViolation
from mcp_server.sandbox.caps_apply import install_fs_guard
tempfile.gettempdir = lambda: str(Path(sys.argv[1]).parent / 'scratch')
install_fs_guard(DEFAULT_DENY)
try:
    builtins.open(sys.argv[1]).read()
except SandboxViolation:
    pass
else:
    raise AssertionError('fixture file must be outside all allowed roots')
try:
    Path(sys.argv[1]).read_text()
except SandboxViolation:
    print('denied')
else:
    print('allowed')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(outside)], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "denied"


def test_sandbox_readonly_rejects_rw_uri(tmp_path):
    db = tmp_path / "readonly.db"
    sqlite3.connect(db).close()
    script = """
import sqlite3, sys
from dataclasses import replace
from mcp_server.sandbox.capabilities import DEFAULT_DENY
from mcp_server.sandbox.caps_apply import _patch_sqlite
_patch_sqlite(replace(DEFAULT_DENY, sqlite='readonly'))
try:
    with sqlite3.connect('file:' + sys.argv[1] + '?mode=rw', uri=True) as conn:
        conn.execute('CREATE TABLE should_be_denied (x)')
except Exception:
    print('denied')
else:
    print('allowed')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(db)], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "denied"


def test_staged_rebuild_preserves_synthetic_history(tmp_path, monkeypatch):
    from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher
    from mcp_server.storage.git_index_manager import GitAwareIndexManager

    monkeypatch.setenv("SEMANTIC_SEARCH_ENABLED", "false")
    monkeypatch.setenv("MCP_TEST_MODE", "1")
    monkeypatch.setenv("MCP_SKIP_PLUGIN_PREINDEX", "true")
    repo, registry, rid = registered(tmp_path)
    stores = StoreRegistry(registry)
    store = stores.get(rid)
    row = store.ensure_repository_row(repo)
    fid = store.store_file(
        row, path=repo / "history:issue:123", relative_path="history:issue:123", language="history"
    )
    store.store_chunk(
        file_id=fid,
        content="Imported issue body",
        content_start=0,
        content_end=19,
        line_start=1,
        line_end=1,
        chunk_id="history:issue:123",
        node_id="history:issue:123",
        treesitter_file_id="history",
        chunk_type="document",
        language="history",
    )
    dispatcher = EnhancedDispatcher(semantic_search_enabled=False, multi_repo_enabled=False)
    resolver = RepoResolver(registry, stores)
    manager = GitAwareIndexManager(
        registry, dispatcher, repo_resolver=resolver, store_registry=stores
    )
    try:
        result = manager.rebuild_repository_index(rid)
        assert result.action == "full_index", result.error
        with sqlite3.connect(registry.get(rid).index_path) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM code_chunks WHERE chunk_id='history:issue:123'"
                ).fetchone()[0]
                == 1
            )
    finally:
        dispatcher.shutdown()
        stores.shutdown()


def test_enforce_rejects_unsigned_archive(tmp_path, monkeypatch):
    import tarfile
    from mcp_server.artifacts.artifact_download import IndexArtifactDownloader

    monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
    monkeypatch.setenv("INDEX_SCHEMA_VERSION", "2")
    payload = tmp_path / "payload"
    payload.mkdir()
    item = payload / "current.db"
    item.write_bytes(b"fixture")
    archive = payload / "index.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(item, arcname="current.db")
    metadata = {
        "repo_id": "r",
        "tracked_branch": "main",
        "commit": "a" * 40,
        "schema_version": "2",
        "semantic_profile_hash": "a" * 64,
        "checksum": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "artifact_type": "full",
        "timestamp": "2026-09-09T00:00:00Z",
        "compatibility": {"schema_version": "2", "embedding_model": None},
    }
    (payload / "artifact-metadata.json").write_text(json.dumps(metadata))
    output = tmp_path / "output"
    output.mkdir()
    downloader = IndexArtifactDownloader(repo="audit/fixture", token="test-nonsecret")
    with pytest.raises(Exception, match="[Aa]ttestation"):
        downloader._restore_downloaded_payload(payload, output)


def test_locked_qdrant_file_removal_api(tmp_path):
    from qdrant_client import QdrantClient, models
    from mcp_server.core.path_resolver import PathResolver
    from mcp_server.utils.semantic_indexer import SemanticIndexer

    client = QdrantClient(":memory:")
    client.create_collection(
        "audit", vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE)
    )
    client.upsert(
        "audit",
        points=[
            models.PointStruct(id=1, vector=[1.0, 0.0], payload={"relative_path": "sample.py"})
        ],
    )
    indexer = SemanticIndexer.__new__(SemanticIndexer)
    indexer.qdrant = client
    indexer.collection = "audit"
    indexer.embedding_dimension = 2
    indexer.path_resolver = PathResolver(tmp_path)
    indexer._qdrant_available = True
    try:
        assert indexer.remove_file(tmp_path / "sample.py") == 1
    finally:
        client.close()


def test_batch_embedding_revalidates_provenance(tmp_path):
    from tests.test_embedding_provenance import (
        _FakeProvenanceProvider,
        _make_indexer,
        _openai_response,
    )

    model = ["srv-model"]
    provider = _FakeProvenanceProvider(
        lambda texts, role: _openai_response(texts, role, served_model=model[0])
    )
    indexer = _make_indexer(tmp_path, provider)
    indexer._prepare_for_writes()
    model[0] = "other-model-same-dimension"
    with pytest.raises(RuntimeError, match="[Pp]rovenance|[Mm]odel|[Ii]ncompatible"):
        indexer._embed_texts(["source chunk"])


def test_registry_does_not_ack_failed_persistence(tmp_path):
    _, registry, rid = registered(tmp_path)
    old_commit = registry.get(rid).last_indexed_commit
    original_replace = Path.replace

    def fail_registry_replace(path, target):
        if Path(target) == registry.registry_path:
            raise OSError("synthetic disk write failure")
        return original_replace(path, target)

    with patch.object(Path, "replace", fail_registry_replace):
        acknowledged = registry.update_indexed_commit(rid, "b" * 40, branch="main")
    disk_commit = RepositoryRegistry(registry.registry_path).get(rid).last_indexed_commit
    assert disk_commit == old_commit
    assert acknowledged is None, "commit publication acknowledged although durable write failed"


def test_source_metadata_filter_still_applies_query(tmp_path):
    from mcp_server.core.repo_context import RepoContext
    from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher

    repo, registry, rid = registered(tmp_path)
    stores = StoreRegistry(registry)
    store = stores.get(rid)
    with store._get_connection() as conn:
        envelope = {
            "source_metadata": {
                "schema_version": "search_source_metadata.v1",
                "records": [
                    {
                        "source_type": "friction",
                        "category": "todo",
                        "line": 1,
                        "description": "unrelated cleanup",
                        "pattern": "TODO",
                    }
                ],
            }
        }
        conn.execute("UPDATE code_chunks SET metadata=?", (json.dumps(envelope),))
    ctx = RepoContext(
        repo_id=rid,
        sqlite_store=store,
        workspace_root=repo,
        tracked_branch="main",
        registry_entry=registry.get(rid),
    )
    dispatcher = EnhancedDispatcher(semantic_search_enabled=False, multi_repo_enabled=False)
    try:
        results = list(dispatcher.search(ctx, "absent_query_marker_xyz", source_type="friction"))
        assert results == []
    finally:
        dispatcher.shutdown()
        stores.shutdown()


def test_legacy_cross_repo_search_refuses_wrong_branch(tmp_path):
    import asyncio
    from mcp_server.storage.multi_repo_manager import MultiRepositoryManager

    repo, registry, rid = registered(tmp_path)
    store = SQLiteStore(str(registry.get(rid).index_path))
    fid = store.get_all_files()[0]["id"]
    store.store_symbol(fid, "original", "function", line_start=1, line_end=2)
    store.close()
    git(repo, "checkout", "-b", "feature")
    assert ReadinessClassifier.classify_registered(registry.get(rid)).state.value == "wrong_branch"
    manager = MultiRepositoryManager(registry.registry_path)
    try:
        results = asyncio.run(manager.search_symbol("original", repository_ids=[rid]))
        assert not any(r.results for r in results), "wrong-branch index returned search hits"
    finally:
        manager._store_registry.shutdown()


def test_real_stdio_server_exits_on_sigterm(tmp_path):
    import os
    import time

    env = os.environ.copy()
    env.update(
        {
            "MCP_REPO_REGISTRY": str(tmp_path / "registry.json"),
            "MCP_WORKSPACE_ROOT": str(tmp_path),
            "MCP_ALLOWED_ROOTS": str(tmp_path),
            "MCP_METRICS_PORT": "0",
            "SEMANTIC_SEARCH_ENABLED": "false",
            "MCP_ENABLE_MULTI_REPO": "false",
            "PYTHON_DOTENV_DISABLED": "1",
        }
    )
    env.pop("MCP_CLIENT_SECRET", None)
    log_path = tmp_path / "server.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "mcp_server.cli.stdio_runner"],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 20
            while "running unauthenticated" not in log_path.read_text():
                assert process.poll() is None, "server failed before test"
                assert time.monotonic() < deadline, "server did not become ready"
                time.sleep(0.1)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pytest.fail("SIGTERM shut down services but did not exit the STDIO process")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdin:
                process.stdin.close()
