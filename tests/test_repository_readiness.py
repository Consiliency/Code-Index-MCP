"""P27 repository readiness contract tests."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_server.health import repository_readiness
from mcp_server.health.repository_readiness import (
    ReadinessClassifier,
    RepositoryReadinessState,
    SemanticReadinessState,
)
from mcp_server.storage.multi_repo_manager import RepositoryInfo
from mcp_server.storage.repository_registry import RepositoryRegistry
from mcp_server.storage.sqlite_store import SQLiteStore
from tests.fixtures.health_repo import make_repo_info


def git(*args, cwd=None):
    subprocess.run(["git"] + list(args), cwd=cwd, check=True, capture_output=True)


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-b", "main", str(path))
    git("config", "user.email", "test@test.com", cwd=path)
    git("config", "user.name", "Test", cwd=path)
    (path / "README.md").write_text("hello")
    git("add", "README.md", cwd=path)
    git("commit", "-m", "init", cwd=path)
    return path


def test_readiness_state_values_are_exact():
    assert {state.value for state in RepositoryReadinessState} == {
        "ready",
        "unregistered_repository",
        "missing_index",
        "index_empty",
        "stale_commit",
        "wrong_branch",
        "index_building",
        "unsupported_worktree",
        "ambiguous_selector",
        "corrupt_sqlite",
        "index_locked",
        "missing_schema",
        "missing_provenance",
        "scheme_mismatch",
        "index_rebuilding",
    }


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize(
    "change,expected",
    [
        ("none", "ready"),
        ("untracked", "ready"),
        ("unstaged", "stale_commit"),
        ("staged", "stale_commit"),
        ("staged_then_restored", "stale_commit"),
        ("rename", "stale_commit"),
        ("delete", "stale_commit"),
        ("commit", "stale_commit"),
        ("branch", "wrong_branch"),
        ("detached", "unregistered_repository"),
        ("missing_git", "unregistered_repository"),
    ],
)
def test_fresh_single_git_probe_keeps_readiness_fences(
    tmp_path, monkeypatch, object_format, change, expected
):
    monkeypatch.setenv("GIT_DEFAULT_HASH", object_format)
    info = make_repo_info(tmp_path)
    repo = make_git_repo(info.path)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    info.current_commit = info.last_indexed_commit = commit
    assert len(commit) == (40 if object_format == "sha1" else 64)
    assert ReadinessClassifier.classify_registered(info).ready
    if change == "untracked":
        (repo / "untracked.txt").write_text("not indexed")
    elif change in {"unstaged", "staged", "staged_then_restored", "commit"}:
        (repo / "README.md").write_text("changed")
        if change != "unstaged":
            git("add", "README.md", cwd=repo)
        if change == "commit":
            git("commit", "-m", "change", cwd=repo)
        elif change == "staged_then_restored":
            (repo / "README.md").write_text("hello")
    elif change == "rename":
        git("mv", "README.md", "renamed\n# branch.head main", cwd=repo)
    elif change == "delete":
        (repo / "README.md").unlink()
    elif change == "branch":
        git("checkout", "-b", "feature", cwd=repo)
    elif change == "detached":
        git("checkout", "--detach", cwd=repo)
    elif change == "missing_git":
        (repo / ".git").rename(repo / "removed-git")

    run = subprocess.run
    calls = []

    def counted_run(args, **kwargs):
        calls.append(args)
        return run(args, **kwargs)

    monkeypatch.setattr(repository_readiness.subprocess, "run", counted_run)
    result = ReadinessClassifier.classify_registered(info)
    assert result.state.value == expected
    assert len(calls) == 1
    assert "--no-optional-locks" in calls[0]


@pytest.mark.parametrize("failure", [OSError("missing git"), subprocess.TimeoutExpired("git", 10)])
def test_live_git_probe_failure_does_not_trust_cached_commit(tmp_path, monkeypatch, failure):
    info = make_repo_info(tmp_path, current_commit="a" * 40)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(repository_readiness.subprocess, "run", fail)
    result = ReadinessClassifier.classify_registered(info)
    assert result.state == RepositoryReadinessState.UNREGISTERED_REPOSITORY


def test_semantic_readiness_state_values_are_exact():
    assert {state.value for state in SemanticReadinessState} == {
        "ready",
        "enrichment_unavailable",
        "summaries_missing",
        "vectors_missing",
        "vector_dimension_mismatch",
        "profile_mismatch",
        "semantic_stale",
    }


def test_serialized_response_keys(tmp_path):
    readiness = ReadinessClassifier.classify_registered(make_repo_info(tmp_path))

    assert set(readiness.to_dict()) == {
        "state",
        "ready",
        "code",
        "repository_id",
        "repository_name",
        "registered_path",
        "requested_path",
        "tracked_branch",
        "current_branch",
        "current_commit",
        "last_indexed_commit",
        "index_path",
        "remediation",
    }
    assert readiness.to_dict()["state"] == "ready"
    assert readiness.to_dict()["code"] is None


def _seed_semantic_rows(
    index_path: Path,
    *,
    with_summary: bool = False,
    with_vector: bool = False,
    collection: str = "code_index__oss_high__v1",
) -> None:
    SQLiteStore(str(index_path))
    conn = sqlite3.connect(index_path)
    file_id = int(conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0])
    conn.execute(
        """INSERT INTO code_chunks
           (file_id, content, content_start, content_end, line_start, line_end,
            chunk_id, node_id, treesitter_file_id, chunk_type, chunk_index)
           VALUES (?, ?, 0, 10, 1, 1, 'chunk-1', 'node-1', 'ts-file-1', 'code', 0)""",
        (file_id, "def demo():\n    return 1\n"),
    )
    if with_summary:
        conn.execute(
            """INSERT INTO chunk_summaries
               (chunk_hash, file_id, chunk_start, chunk_end, summary_text, is_authoritative, llm_model)
               VALUES ('chunk-1', ?, 0, 10, 'demo summary', 1, 'chat')""",
            (file_id,),
        )
    if with_vector:
        conn.execute(
            """INSERT INTO semantic_points (profile_id, chunk_id, point_id, collection)
               VALUES ('oss_high', 'chunk-1', 1, ?)""",
            (collection,),
        )
    conn.commit()
    conn.close()


def _make_semantic_repo_info(tmp_path: Path) -> tuple[RepositoryInfo, SQLiteStore]:
    repo_root = tmp_path / "semantic-repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    readme = repo_root / "README.md"
    readme.write_text("hello\n", encoding="utf-8")
    index_path = repo_root / ".mcp-index" / "current.db"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(str(index_path))
    repository_row = store.ensure_repository_row(repo_root, name="semantic-repo")
    store.store_file(
        repository_row,
        path=readme,
        relative_path="README.md",
        language="python",
        size=readme.stat().st_size,
    )
    info = RepositoryInfo(
        repository_id="semantic-repo",
        name="semantic-repo",
        path=repo_root,
        index_path=index_path,
        language_stats={},
        total_files=1,
        total_symbols=0,
        indexed_at=datetime.now(),
        tracked_branch="main",
        current_branch="main",
        git_common_dir=str(repo_root / ".git"),
    )
    return info, store


def _profile() -> SimpleNamespace:
    return SimpleNamespace(
        profile_id="oss_high",
        compatibility_fingerprint="fingerprint-1",
        vector_dimension=4096,
        build_metadata={"collection_name": "code_index__oss_high__v1"},
    )


def test_sqlite_semantic_evidence_counts_chunks_summaries_and_vectors(tmp_path):
    info, store = _make_semantic_repo_info(tmp_path)
    _seed_semantic_rows(info.index_path, with_summary=True, with_vector=False)

    evidence = store.get_semantic_readiness_evidence(
        "oss_high",
        collection="code_index__oss_high__v1",
    )

    assert evidence["total_chunks"] == 1
    assert evidence["summary_count"] == 1
    assert evidence["missing_summaries"] == 0
    assert evidence["vector_link_count"] == 0
    assert evidence["missing_vectors"] == 1


def test_semantic_readiness_reports_summaries_missing_for_lexically_ready_repo(
    tmp_path, monkeypatch
):
    import mcp_server.health.repository_readiness as readiness_module

    info, store = _make_semantic_repo_info(tmp_path)
    _seed_semantic_rows(info.index_path, with_summary=False, with_vector=False)
    monkeypatch.setattr(readiness_module, "_current_semantic_profile", lambda: _profile())
    (Path(info.path) / ".index_metadata.json").write_text(
        '{"semantic_profile":"oss_high","semantic_profiles":{"oss_high":{"compatibility_fingerprint":"fingerprint-1","model_dimension":4096,"collection_name":"code_index__oss_high__v1"}}}',
        encoding="utf-8",
    )

    semantic = ReadinessClassifier.classify_semantic_registered(info, store)

    assert semantic.state == SemanticReadinessState.SUMMARIES_MISSING
    assert semantic.ready is False


def test_semantic_readiness_reports_vectors_missing_when_summaries_exist(tmp_path, monkeypatch):
    import mcp_server.health.repository_readiness as readiness_module

    info, store = _make_semantic_repo_info(tmp_path)
    _seed_semantic_rows(info.index_path, with_summary=True, with_vector=False)
    monkeypatch.setattr(readiness_module, "_current_semantic_profile", lambda: _profile())
    (Path(info.path) / ".index_metadata.json").write_text(
        '{"semantic_profile":"oss_high","semantic_profiles":{"oss_high":{"compatibility_fingerprint":"fingerprint-1","model_dimension":4096,"collection_name":"code_index__oss_high__v1"}}}',
        encoding="utf-8",
    )

    semantic = ReadinessClassifier.classify_semantic_registered(info, store)

    assert semantic.state == SemanticReadinessState.VECTORS_MISSING


def test_semantic_readiness_reports_stale_when_fingerprint_mismatches(tmp_path, monkeypatch):
    import mcp_server.health.repository_readiness as readiness_module

    info, store = _make_semantic_repo_info(tmp_path)
    _seed_semantic_rows(info.index_path, with_summary=True, with_vector=True)
    monkeypatch.setattr(readiness_module, "_current_semantic_profile", lambda: _profile())
    (Path(info.path) / ".index_metadata.json").write_text(
        '{"semantic_profile":"oss_high","semantic_profiles":{"oss_high":{"compatibility_fingerprint":"fingerprint-old","model_dimension":4096,"collection_name":"code_index__oss_high__v1"}}}',
        encoding="utf-8",
    )

    semantic = ReadinessClassifier.classify_semantic_registered(info, store)

    assert semantic.state == SemanticReadinessState.SEMANTIC_STALE


def test_semantic_readiness_reports_dimension_mismatch(tmp_path, monkeypatch):
    import mcp_server.health.repository_readiness as readiness_module

    info, store = _make_semantic_repo_info(tmp_path)
    _seed_semantic_rows(info.index_path, with_summary=True, with_vector=True)
    monkeypatch.setattr(readiness_module, "_current_semantic_profile", lambda: _profile())
    (Path(info.path) / ".index_metadata.json").write_text(
        '{"semantic_profile":"oss_high","semantic_profiles":{"oss_high":{"compatibility_fingerprint":"fingerprint-1","model_dimension":1024,"collection_name":"code_index__oss_high__v1"}}}',
        encoding="utf-8",
    )

    semantic = ReadinessClassifier.classify_semantic_registered(info, store)

    assert semantic.state == SemanticReadinessState.VECTOR_DIMENSION_MISMATCH


def test_classifies_missing_index(tmp_path):
    readiness = ReadinessClassifier.classify_registered(
        make_repo_info(tmp_path, missing_index=True)
    )
    assert readiness.state == RepositoryReadinessState.MISSING_INDEX
    assert readiness.ready is False
    assert readiness.code == "missing_index"


def test_classifies_empty_sqlite_index(tmp_path):
    readiness = ReadinessClassifier.classify_registered(make_repo_info(tmp_path, empty_index=True))
    assert readiness.state == RepositoryReadinessState.INDEX_EMPTY


def test_classifies_stale_commit(tmp_path):
    readiness = ReadinessClassifier.classify_registered(
        make_repo_info(
            tmp_path,
            current_commit="bbbb",
            last_indexed_commit="aaaa",
        )
    )
    assert readiness.state == RepositoryReadinessState.STALE_COMMIT


def test_classifies_wrong_branch(tmp_path):
    readiness = ReadinessClassifier.classify_registered(
        make_repo_info(tmp_path, tracked_branch="main", current_branch="feature")
    )
    assert readiness.state == RepositoryReadinessState.WRONG_BRANCH


def test_classifies_index_building(tmp_path):
    readiness = ReadinessClassifier.classify_registered(
        make_repo_info(tmp_path),
        indexing_active=True,
    )
    assert readiness.state == RepositoryReadinessState.INDEX_BUILDING


def test_classifies_unregistered_path(tmp_path):
    registry = RepositoryRegistry(tmp_path / "registry.json")
    unregistered = make_git_repo(tmp_path / "unregistered")

    readiness = ReadinessClassifier.classify_path(registry, unregistered)

    assert readiness.state == RepositoryReadinessState.UNREGISTERED_REPOSITORY
    assert readiness.requested_path == str(unregistered.resolve())


def test_classifies_unsupported_worktree(tmp_path):
    source = make_git_repo(tmp_path / "source")
    worktree = tmp_path / "wt"
    git("worktree", "add", str(worktree), "-b", "feature", cwd=source)

    registry = RepositoryRegistry(tmp_path / "registry.json")
    repo_id = registry.register_repository(str(source))
    info = registry.get(repo_id)
    info.index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(info.index_path)
    conn.execute(
        "CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT, is_deleted BOOLEAN DEFAULT 0)"
    )
    conn.execute("INSERT INTO files (path, is_deleted) VALUES ('README.md', 0)")
    conn.commit()
    conn.close()

    readiness = ReadinessClassifier.classify_path(registry, worktree)

    assert readiness.state == RepositoryReadinessState.UNSUPPORTED_WORKTREE
    assert readiness.registered_path == str(source.resolve())
    assert readiness.requested_path == str(worktree.resolve())


def test_classifies_corrupt_sqlite(tmp_path):
    repo_info = make_repo_info(tmp_path)
    repo_info.index_path.write_text(
        "this is not a database file - it is plain text and corrupt!",
        encoding="utf-8",
    )

    readiness = ReadinessClassifier.classify_registered(repo_info)
    assert readiness.state == RepositoryReadinessState.CORRUPT_SQLITE
    assert readiness.ready is False
    assert "quarantine" in (readiness.remediation or "").lower()


def test_classifies_missing_schema(tmp_path):
    repo_info = make_repo_info(tmp_path)
    conn = sqlite3.connect(repo_info.index_path)
    conn.execute("DROP TABLE files")
    conn.commit()
    conn.close()

    readiness = ReadinessClassifier.classify_registered(repo_info)
    assert readiness.state == RepositoryReadinessState.MISSING_SCHEMA
    assert readiness.ready is False
    assert "quarantine" in (readiness.remediation or "").lower()


def test_classifies_missing_provenance(tmp_path):
    repo_info = make_repo_info(tmp_path)
    repo_info.last_indexed_commit = None

    readiness = ReadinessClassifier.classify_registered(repo_info)
    assert readiness.state == RepositoryReadinessState.MISSING_PROVENANCE
    assert readiness.ready is False
    assert "quarantine" in (readiness.remediation or "").lower()


def test_wrong_branch_precedes_unsafe_storage_classification(tmp_path):
    repo_info = make_repo_info(tmp_path, current_branch="feature")
    repo_info.index_path.write_text("corrupt", encoding="utf-8")

    readiness = ReadinessClassifier.classify_registered(repo_info)

    assert readiness.state == RepositoryReadinessState.WRONG_BRANCH


def test_interrupted_publication_is_missing_provenance(tmp_path):
    repo_info = make_repo_info(tmp_path)
    repo_info.staleness_reason = "partial_index_failure"

    readiness = ReadinessClassifier.classify_registered(repo_info)

    assert readiness.state == RepositoryReadinessState.MISSING_PROVENANCE


def test_sqlite_query_exception_fails_closed_as_corrupt(tmp_path, monkeypatch):
    repo_info = make_repo_info(tmp_path)

    def fail_connect(*_args, **_kwargs):
        raise sqlite3.DatabaseError("cannot open")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)

    readiness = ReadinessClassifier.classify_registered(repo_info)

    assert readiness.state == RepositoryReadinessState.CORRUPT_SQLITE


def test_locked_database_classifies_as_index_locked_not_corrupt(tmp_path, monkeypatch):
    """Cleanup item 4: a 'database is locked'/'database is busy' OperationalError is
    a busy-but-HEALTHY index (concurrent writer contention), not corruption, and
    must classify as the distinct INDEX_LOCKED state -- never the alarming
    CORRUPT_SQLITE, which would advise a destructive quarantine/reindex."""
    ReadinessClassifier.clear_index_inspection_cache()
    repo_info = make_repo_info(tmp_path)

    def locked_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite3, "connect", locked_connect)

    readiness = ReadinessClassifier.classify_registered(repo_info)

    assert readiness.state == RepositoryReadinessState.INDEX_LOCKED
    assert readiness.state != RepositoryReadinessState.CORRUPT_SQLITE
    assert readiness.ready is False
    # Remediation must NOT tell the operator to quarantine/reindex a healthy index.
    assert "quarantine" not in (readiness.remediation or "").lower()
    assert "retry" in (readiness.remediation or "").lower()
    ReadinessClassifier.clear_index_inspection_cache()


def test_non_lock_operational_error_still_classifies_as_corrupt(tmp_path, monkeypatch):
    """The lock guard is specific: an OperationalError that is NOT a lock/busy
    contention signal still fails closed as CORRUPT_SQLITE."""
    ReadinessClassifier.clear_index_inspection_cache()
    repo_info = make_repo_info(tmp_path)

    def broken_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", broken_connect)

    readiness = ReadinessClassifier.classify_registered(repo_info)

    assert readiness.state == RepositoryReadinessState.CORRUPT_SQLITE
    ReadinessClassifier.clear_index_inspection_cache()


def test_unchanged_index_reuses_cached_integrity_inspection(tmp_path, monkeypatch):
    repo_info = make_repo_info(tmp_path)
    calls = 0
    original = repository_readiness._inspect_index_uncached

    def counted(index_path: Path):
        nonlocal calls
        calls += 1
        return original(index_path)

    ReadinessClassifier.clear_index_inspection_cache()
    monkeypatch.setattr(repository_readiness, "_inspect_index_uncached", counted)

    assert ReadinessClassifier._inspect_index(repo_info.index_path) is None
    assert ReadinessClassifier._inspect_index(repo_info.index_path) is None
    assert calls == 1

    stat = repo_info.index_path.stat()
    os.utime(repo_info.index_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
    assert ReadinessClassifier._inspect_index(repo_info.index_path) is None
    assert calls == 2
    ReadinessClassifier.clear_index_inspection_cache()


def test_failed_integrity_inspection_is_not_cached(tmp_path, monkeypatch):
    repo_info = make_repo_info(tmp_path)
    results = iter((RepositoryReadinessState.CORRUPT_SQLITE, None))
    calls = 0

    def transient(_index_path: Path):
        nonlocal calls
        calls += 1
        return next(results)

    ReadinessClassifier.clear_index_inspection_cache()
    monkeypatch.setattr(repository_readiness, "_inspect_index_uncached", transient)

    assert (
        ReadinessClassifier._inspect_index(repo_info.index_path)
        == RepositoryReadinessState.CORRUPT_SQLITE
    )
    assert ReadinessClassifier._inspect_index(repo_info.index_path) is None
    assert ReadinessClassifier._inspect_index(repo_info.index_path) is None
    assert calls == 2
    ReadinessClassifier.clear_index_inspection_cache()
