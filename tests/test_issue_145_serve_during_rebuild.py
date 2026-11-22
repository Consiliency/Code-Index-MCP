"""Queries keep serving the published generation during a staged rebuild (#145).

Owner decision: while a staged build runs and its owner is alive, readiness
classifies against the published generation (``ready`` for a same-commit
rebuild, ``stale_commit`` while a newer commit is indexed) with an additive
``rebuild`` object, and ``search_code``/``symbol_lookup`` serve the published
generation. A dead owner keeps today's fail-closed ``missing_provenance``.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_server.core.repo_resolver import run_repository_mutation
from mcp_server.health.repository_readiness import ReadinessClassifier, RepositoryReadinessState
from tests.fixtures.multi_repo import boot_test_server, build_temp_repo, git, git_head

readiness_module = importlib.import_module("mcp_server.health.repository_readiness")

# ``mcp_server.indexing`` re-exports the ``lock_registry`` singleton under the
# module's own name, so import the module explicitly.
locks = importlib.import_module("mcp_server.indexing.lock_registry")

SEED = {
    "alpha.py": "def alpha_token():\n    return 1\n\n\nclass AlphaSymbol:\n    pass\n",
}


def _owner_file(path: Path) -> Path:
    return path / ".mcp-index" / "rebuild-owner.json"


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _search(server, path: Path, query: str) -> dict | list:
    return server.call_tool("search_code", {"query": query, "repository": str(path)})


def _symbol(server, path: Path, symbol: str) -> dict:
    return server.call_tool("symbol_lookup", {"symbol": symbol, "repository": str(path)})


def _result_files(response) -> list[str]:
    results = response["results"] if isinstance(response, dict) else response
    return [Path(item["file"]).name for item in results]


# ---------------------------------------------------------------------------
# Owner record and liveness
# ---------------------------------------------------------------------------


def test_live_owner_requires_matching_identity_and_a_held_writer_lock(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    owner = locks.record_rebuild_owner(
        repo, registration_id="reg", published_generation="gen", target_commit="c" * 40
    )
    assert owner is not None and owner["pid"] == os.getpid()
    assert owner["host"] == socket.gethostname()
    identity = {"registration_id": "reg", "published_generation": "gen"}

    # The pid is alive, but nobody holds the writer lock: not a live build.
    assert locks.live_rebuild_owner(repo, **identity) is None
    with locks.lock_registry.acquire("repo", repo_path=repo):
        assert locks.writer_lock_held(repo)
        live = locks.live_rebuild_owner(repo, **identity)
        assert live is not None and live["target_commit"] == "c" * 40
        # A record for another registration or generation is not this build.
        assert (
            locks.live_rebuild_owner(repo, registration_id="other", published_generation="gen")
            is None
        )
        assert (
            locks.live_rebuild_owner(repo, registration_id="reg", published_generation="old")
            is None
        )
    assert not locks.writer_lock_held(repo)

    # Someone else's token never clears the record; ours does.
    locks.clear_rebuild_owner(repo, "not-the-token")
    assert _owner_file(repo).exists()
    locks.clear_rebuild_owner(repo, owner["token"])
    assert not _owner_file(repo).exists()


@pytest.mark.parametrize("tamper", ["dead_pid", "reused_pid", "other_host", "unreadable"])
def test_owner_that_is_not_provably_alive_is_dead(tmp_path, tamper):
    repo = tmp_path / "repo"
    repo.mkdir()
    locks.record_rebuild_owner(
        repo, registration_id="reg", published_generation="gen", target_commit="c" * 40
    )
    record = json.loads(_owner_file(repo).read_text())
    if tamper == "dead_pid":
        record["pid"] = _dead_pid()
    elif tamper == "reused_pid":
        record["process_start"] = "1"
    elif tamper == "other_host":
        record["host"] = record["host"] + "-elsewhere"
    if tamper == "unreadable":
        _owner_file(repo).write_text("{not json")
    else:
        _owner_file(repo).write_text(json.dumps(record))
    with locks.lock_registry.acquire("repo", repo_path=repo):
        assert (
            locks.live_rebuild_owner(repo, registration_id="reg", published_generation="gen")
            is None
        )


@pytest.mark.parametrize("unavailable", ["when_recorded", "when_checked"])
def test_owner_without_a_provable_start_time_is_dead(tmp_path, monkeypatch, unavailable):
    # A host without /proc (or a psutil failure) cannot rule out a reused pid,
    # so the owner is not provably alive even while a writer holds the lock.
    repo = tmp_path / "repo"
    repo.mkdir()
    if unavailable == "when_recorded":
        monkeypatch.setattr(locks, "_process_start_time", lambda pid: None)
    owner = locks.record_rebuild_owner(
        repo, registration_id="reg", published_generation="gen", target_commit="c" * 40
    )
    assert owner is not None
    if unavailable == "when_recorded":
        assert owner["process_start"] is None
    else:
        monkeypatch.setattr(locks, "_process_start_time", lambda pid: None)
    with locks.lock_registry.acquire("repo", repo_path=repo):
        assert (
            locks.live_rebuild_owner(repo, registration_id="reg", published_generation="gen")
            is None
        )


def test_start_time_falls_back_to_psutil_without_proc(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(locks, "_proc_start_time", lambda pid: None)
    owner = locks.record_rebuild_owner(
        repo, registration_id="reg", published_generation="gen", target_commit="c" * 40
    )
    assert owner["process_start"].startswith("psutil:")
    identity = {"registration_id": "reg", "published_generation": "gen"}
    with locks.lock_registry.acquire("repo", repo_path=repo):
        assert locks.live_rebuild_owner(repo, **identity) is not None
        # Without any start-time source the same record is no longer provable.
        monkeypatch.setattr(locks, "_psutil_start_time", lambda pid: None)
        assert locks.live_rebuild_owner(repo, **identity) is None


# ---------------------------------------------------------------------------
# Same-commit rebuild with a live owner
# ---------------------------------------------------------------------------


def test_same_commit_rebuild_stays_ready_and_serves_the_published_generation(tmp_path):
    path, repo_id = build_temp_repo(tmp_path, "same", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        resolver = server.repo_resolver
        ctx = resolver.resolve_ready(path)
        published = server.registry.get(repo_id)
        head = git_head(path)
        observed: dict = {}

        def operation(stage):
            registry_entry = server.registry.get(repo_id)
            assert registry_entry.staleness_reason == "index_publication_pending"
            readiness = resolver.classify(path)
            observed["readiness"] = readiness
            # Readers resolve the published file, never the stage.
            reader = resolver.resolve_ready(path)
            assert reader is not None
            assert Path(reader.sqlite_store.db_path) == Path(published.index_path)
            assert Path(stage.sqlite_store.db_path) != Path(published.index_path)
            assert resolver.is_current(ctx)
            observed["search"] = _search(server, path, "alpha_token")
            observed["symbol"] = _symbol(server, path, "AlphaSymbol")
            observed["status"] = server.call_tool("get_status", {})
            return {"failed_files": 0}

        run_repository_mutation(resolver, ctx, operation)

        readiness = observed["readiness"]
        assert readiness.state == RepositoryReadinessState.READY
        assert readiness.rebuild["in_progress"] is True
        assert readiness.rebuild["target_commit"] == head
        assert readiness.rebuild["started_at"]
        assert readiness.to_dict()["rebuild"] == readiness.rebuild
        assert "alpha.py" in _result_files(observed["search"])
        assert observed["symbol"]["symbol"] == "AlphaSymbol"
        assert "error" not in observed["symbol"]

        # Publication cleared the owner and the rebuild marker.
        assert not _owner_file(path).exists()
        after = resolver.classify(path)
        assert after.ready and after.rebuild is None
        assert server.registry.get(repo_id).index_generation != published.index_generation


# ---------------------------------------------------------------------------
# Newer-commit rebuild with a live owner
# ---------------------------------------------------------------------------


def test_newer_commit_rebuild_reports_stale_and_serves_with_stale_metadata(tmp_path, monkeypatch):
    path, repo_id = build_temp_repo(tmp_path, "newer", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        resolver = server.repo_resolver
        manager = resolver._index_manager
        indexed = git_head(path)
        (path / "beta.py").write_text("def beta_token():\n    return 2\n")
        git(path, "add", "beta.py")
        git(path, "commit", "-m", "add beta")
        target = git_head(path)
        observed: dict = {}
        original = manager._rebuild_checkpoint

        def checkpoint(stage):
            if stage == "before_publish" and "readiness" not in observed:
                observed["readiness"] = resolver.classify(path)
                observed["search"] = _search(server, path, "alpha_token")
                observed["symbol"] = _symbol(server, path, "AlphaSymbol")
                observed["missing"] = _symbol(server, path, "NoSuchSymbol")
            return original(stage)

        monkeypatch.setattr(manager, "_rebuild_checkpoint", checkpoint)
        result = manager.sync_repository_index(repo_id)
        assert result.action in {"incremental_update", "full_index"}, result

        readiness = observed["readiness"]
        assert readiness.state == RepositoryReadinessState.STALE_COMMIT
        assert readiness.serving_stale and readiness.queryable
        assert readiness.last_indexed_commit == indexed
        assert readiness.current_commit == target
        assert readiness.rebuild["target_commit"] == target

        search = observed["search"]
        assert isinstance(search, dict), search
        assert "alpha.py" in _result_files(search)
        assert "beta.py" not in _result_files(search)
        assert search["readiness"]["state"] == "stale_commit"
        assert search["readiness"]["last_indexed_commit"] == indexed
        assert search["readiness"]["current_commit"] == target
        assert search["readiness"]["rebuild"]["in_progress"] is True
        assert "stale_commit" in search["note"]

        symbol = observed["symbol"]
        assert symbol["symbol"] == "AlphaSymbol"
        assert symbol["readiness"]["state"] == "stale_commit"
        assert observed["missing"]["result"] == "not_found"
        assert observed["missing"]["readiness"]["state"] == "stale_commit"

        assert not _owner_file(path).exists()
        after = resolver.classify(path)
        assert after.ready and after.rebuild is None
        assert after.last_indexed_commit == target


def test_head_moving_away_from_the_rebuild_target_fails_closed(tmp_path, monkeypatch):
    path, repo_id = build_temp_repo(tmp_path, "moved", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        resolver = server.repo_resolver
        manager = resolver._index_manager
        (path / "beta.py").write_text("def beta_token():\n    return 2\n")
        git(path, "add", "beta.py")
        git(path, "commit", "-m", "add beta")
        target = git_head(path)
        observed: dict = {}
        original = manager._rebuild_checkpoint

        def checkpoint(stage):
            if stage == "before_provenance" and "readiness" not in observed:
                # HEAD moves past the commit the live build is indexing.
                (path / "gamma.py").write_text("gamma = 3\n")
                git(path, "add", "gamma.py")
                git(path, "commit", "-m", "add gamma")
                observed["readiness"] = resolver.classify(path)
                observed["search"] = _search(server, path, "alpha_token")
                observed["symbol"] = _symbol(server, path, "AlphaSymbol")
            return original(stage)

        monkeypatch.setattr(manager, "_rebuild_checkpoint", checkpoint)
        result = manager.sync_repository_index(repo_id)

        readiness = observed["readiness"]
        assert readiness.current_commit != target
        assert readiness.state == RepositoryReadinessState.MISSING_PROVENANCE
        assert readiness.rebuild is None
        assert not readiness.queryable and not readiness.serving_stale
        assert observed["search"]["code"] == "index_unavailable"
        assert observed["symbol"]["code"] == "index_unavailable"
        # The build itself refuses to publish the moved source.
        assert result.action == "failed"
        assert "changed" in (result.error or "")
        assert not _owner_file(path).exists()


@pytest.mark.parametrize("scenario", ["same_commit", "newer_commit"])
def test_dirty_tracked_tree_during_a_rebuild_fails_closed(tmp_path, monkeypatch, scenario):
    path, repo_id = build_temp_repo(tmp_path, f"dirty-{scenario}", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        resolver = server.repo_resolver
        manager = resolver._index_manager
        if scenario == "newer_commit":
            (path / "beta.py").write_text("def beta_token():\n    return 2\n")
            git(path, "add", "beta.py")
            git(path, "commit", "-m", "add beta")
        target = git_head(path)
        observed: dict = {}
        original = manager._rebuild_checkpoint

        def checkpoint(stage):
            if stage == "stage_created" and "dirty" not in observed:
                observed["clean"] = resolver.classify(path)
                (path / "alpha.py").write_text("def alpha_token():\n    return 99\n")
                observed["dirty"] = resolver.classify(path)
                observed["search"] = _search(server, path, "alpha_token")
                observed["symbol"] = _symbol(server, path, "AlphaSymbol")
                # Restore the tracked file so the build itself can publish.
                git(path, "checkout", "--", "alpha.py")
            return original(stage)

        monkeypatch.setattr(manager, "_rebuild_checkpoint", checkpoint)
        if scenario == "same_commit":
            result = manager.rebuild_repository_index(repo_id)
        else:
            result = manager.sync_repository_index(repo_id)

        clean = observed["clean"]
        assert clean.rebuild is not None and clean.rebuild["target_commit"] == target
        assert clean.queryable
        dirty = observed["dirty"]
        assert dirty.state == RepositoryReadinessState.MISSING_PROVENANCE
        assert dirty.rebuild is None and not dirty.queryable
        assert observed["search"]["code"] == "index_unavailable"
        assert observed["symbol"]["code"] == "index_unavailable"
        assert result.action in {"full_index", "incremental_update"}, result
        assert resolver.classify(path).ready


def test_serving_stale_requires_the_rebuild_to_target_the_current_commit():
    from mcp_server.health.repository_readiness import RepositoryReadiness

    base = dict(
        state=RepositoryReadinessState.STALE_COMMIT,
        current_commit="b" * 40,
        last_indexed_commit="a" * 40,
    )
    targeted = RepositoryReadiness(
        **base, rebuild={"in_progress": True, "target_commit": "b" * 40, "started_at": "t"}
    )
    elsewhere = RepositoryReadiness(
        **base, rebuild={"in_progress": True, "target_commit": "c" * 40, "started_at": "t"}
    )
    assert targeted.serving_stale and targeted.queryable
    assert not elsewhere.serving_stale and not elsewhere.queryable
    assert not RepositoryReadiness(**base).queryable


def test_stale_commit_without_a_rebuild_stays_fail_closed(tmp_path):
    path, repo_id = build_temp_repo(tmp_path, "plainstale", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        (path / "beta.py").write_text("x = 1\n")
        git(path, "add", "beta.py")
        git(path, "commit", "-m", "advance")
        readiness = server.repo_resolver.classify(path)
        assert readiness.state == RepositoryReadinessState.STALE_COMMIT
        assert readiness.rebuild is None and not readiness.queryable
        assert _search(server, path, "alpha_token")["code"] == "index_unavailable"
        assert _symbol(server, path, "AlphaSymbol")["code"] == "index_unavailable"


# ---------------------------------------------------------------------------
# Dead owner
# ---------------------------------------------------------------------------


def test_dead_owner_keeps_missing_provenance_and_fails_closed(tmp_path):
    path, repo_id = build_temp_repo(tmp_path, "dead", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        info = server.registry.get(repo_id)
        server.registry.begin_generation_mutation(
            repo_id,
            expected_registration_id=info.registration_id,
            expected_generation=info.index_generation,
        )
        # A crashed build: the record names it, but its process is gone and
        # the kernel released its writer lock.
        locks.record_rebuild_owner(
            path,
            registration_id=info.registration_id,
            published_generation=info.index_generation,
            target_commit=git_head(path),
        )
        record = json.loads(_owner_file(path).read_text())
        record["pid"] = _dead_pid()
        _owner_file(path).write_text(json.dumps(record))

        readiness = server.repo_resolver.classify(path)
        assert readiness.state == RepositoryReadinessState.MISSING_PROVENANCE
        assert readiness.rebuild is None and not readiness.queryable
        assert server.repo_resolver.resolve_ready(path) is None
        assert _search(server, path, "alpha_token")["code"] == "index_unavailable"
        assert _symbol(server, path, "AlphaSymbol")["code"] == "index_unavailable"

        # Even with the writer lock held by a live process, a dead recorded
        # owner is not this build.
        with locks.lock_registry.acquire(repo_id, repo_path=path):
            again = ReadinessClassifier.classify_registered(server.registry.get(repo_id))
            assert again.state == RepositoryReadinessState.MISSING_PROVENANCE

        # And a pending generation with no owner record at all stays fenced.
        _owner_file(path).unlink()
        assert (
            server.repo_resolver.classify(path).state == RepositoryReadinessState.MISSING_PROVENANCE
        )


# ---------------------------------------------------------------------------
# Failure and abort clear the owner
# ---------------------------------------------------------------------------


class _Abort(BaseException):
    """Stands in for KeyboardInterrupt/SystemExit escaping the build."""


@pytest.mark.parametrize("outcome", ["error", "exception", "abort"])
def test_failed_or_aborted_build_clears_the_owner_and_stays_fail_closed(tmp_path, outcome):
    path, repo_id = build_temp_repo(tmp_path, f"fail-{outcome}", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        resolver = server.repo_resolver
        ctx = resolver.resolve_ready(path)
        seen: dict = {}

        def operation(stage):
            seen["owner"] = _owner_file(path).exists()
            seen["ready"] = resolver.classify(path).ready
            if outcome == "exception":
                raise RuntimeError("injected")
            if outcome == "abort":
                raise _Abort()
            return {"failed_files": 1}

        expected = _Abort if outcome == "abort" else (RuntimeError, ValueError)
        with pytest.raises(expected):
            run_repository_mutation(resolver, ctx, operation)

        assert seen == {"owner": True, "ready": True}
        assert not _owner_file(path).exists()
        after = resolver.classify(path)
        assert after.state == RepositoryReadinessState.MISSING_PROVENANCE
        assert after.rebuild is None
        assert _search(server, path, "alpha_token")["code"] == "index_unavailable"


# ---------------------------------------------------------------------------
# Publish swap during a query
# ---------------------------------------------------------------------------


def test_publish_swap_mid_query_finishes_on_the_old_file_then_fails_closed(tmp_path, monkeypatch):
    path, repo_id = build_temp_repo(tmp_path, "swap", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        resolver = server.repo_resolver
        manager = resolver._index_manager
        before = server.registry.get(repo_id)
        old_file = Path(before.index_path)
        original_search = server.dispatcher.search
        observed: dict = {}

        def search(ctx, query, **kwargs):
            assert Path(ctx.sqlite_store.db_path) == old_file
            with ctx.sqlite_store._get_connection() as connection:
                observed["before"] = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                # The rebuild publishes a new generation while this reader
                # still holds its connection to the old file.
                observed["rebuild"] = manager.rebuild_repository_index(repo_id)
                observed["after"] = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            observed["old_file_kept"] = old_file.exists()
            return original_search(ctx, query, **kwargs)

        monkeypatch.setattr(server.dispatcher, "search", search)
        response = _search(server, path, "alpha_token")

        assert observed["rebuild"].action == "full_index", observed["rebuild"]
        assert observed["before"] == observed["after"] > 0
        # Retention keeps the superseded generation (live + at least one previous).
        assert observed["old_file_kept"]
        assert old_file.exists()
        assert server.registry.get(repo_id).index_path != before.index_path
        # The generation recheck refuses results that straddled the swap.
        assert response["code"] == "index_unavailable"
        assert response["safe_fallback"] == "native_search"

        monkeypatch.setattr(server.dispatcher, "search", original_search)
        retry = _search(server, path, "alpha_token")
        assert "alpha.py" in _result_files(retry)


# ---------------------------------------------------------------------------
# Persistent index writes refuse while a rebuild runs
# ---------------------------------------------------------------------------


class _FakeSummarizer:
    """Stands in for the lazy summarizer: summarization available, enqueue recorded."""

    def __init__(self):
        self.enqueued: list = []

    def can_summarize(self) -> bool:
        return True

    def _get_model_name(self) -> str:
        return "fake-model"

    def enqueue(self, chunk, write_fence=None) -> None:
        self.enqueued.append((chunk, write_fence))


class _FakeStore:
    """The handler's global store, only consulted for lazy enqueue."""

    def get_chunk_scheme_status(self):
        return ("compatible", None, None)

    def find_chunk_at_line(self, path, line):
        return {"chunk_id": f"{path}:{line}"}


def _run_tool(handler, **kwargs) -> dict:
    from mcp_server.cli import tool_handlers

    result = asyncio.run(getattr(tool_handlers, handler)(**kwargs))
    return json.loads(result[0].text)


def _install_fake_writer(monkeypatch, on_process=None):
    from mcp_server.indexing import summarization
    from mcp_server.indexing.summarization import SummaryGenerationResult

    calls: list = []

    class FakeWriter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def process_scope(self, limit=500, cancel_check=None, **kwargs):
            calls.append(self.kwargs["db_path"])
            if on_process is not None:
                on_process()
            cancelled = bool(cancel_check and cancel_check())
            return SummaryGenerationResult(
                chunks_attempted=1,
                summaries_written=1,
                summaries_committed=1,
                cancelled=cancelled,
            )

    monkeypatch.setattr(summarization, "ComprehensiveChunkWriter", FakeWriter)
    return calls


def test_write_summaries_refuses_during_a_rebuild_and_writes_after(tmp_path, monkeypatch):
    path, repo_id = build_temp_repo(tmp_path, "summaries", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        resolver = server.repo_resolver
        calls = _install_fake_writer(monkeypatch)
        common = dict(
            arguments={"repository": str(path)},
            dispatcher=server.dispatcher,
            repo_resolver=resolver,
            lazy_summarizer=_FakeSummarizer(),
        )
        observed: dict = {}

        def operation(stage):
            observed["readiness"] = resolver.classify(path)
            observed["write"] = _run_tool("handle_write_summaries", **common)
            return {"failed_files": 0}

        run_repository_mutation(resolver, resolver.resolve_ready(path), operation)

        assert observed["readiness"].ready and observed["readiness"].rebuild
        refused = observed["write"]
        assert refused["code"] == "rebuild_in_progress"
        assert refused["mutation_performed"] is False
        assert refused["readiness"]["rebuild"]["in_progress"] is True
        assert calls == []  # nothing reached the published generation

        written = _run_tool("handle_write_summaries", **common)
        assert written["persisted"] is True and written["chunks_summarized"] == 1
        assert len(calls) == 1


def test_write_summaries_stops_when_a_rebuild_starts_mid_run(tmp_path, monkeypatch):
    path, repo_id = build_temp_repo(tmp_path, "midrun", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        info = server.registry.get(repo_id)

        def rebuild_starts():
            server.registry.begin_generation_mutation(
                repo_id,
                expected_registration_id=info.registration_id,
                expected_generation=info.index_generation,
            )

        calls = _install_fake_writer(monkeypatch, on_process=rebuild_starts)
        response = _run_tool(
            "handle_write_summaries",
            arguments={"repository": str(path)},
            dispatcher=server.dispatcher,
            repo_resolver=server.repo_resolver,
            lazy_summarizer=_FakeSummarizer(),
        )
        assert len(calls) == 1
        assert response["code"] == "rebuild_in_progress"
        assert response["chunks_summarized"] == 1
        assert response["mutation_performed"] is True
        assert response["retryable"] is True
        assert "kept" in response["note"]
        _assert_matches_schema("write_summaries", response)


def test_summarize_sample_refuses_to_persist_during_a_rebuild(tmp_path, monkeypatch):
    path, repo_id = build_temp_repo(tmp_path, "sample", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        resolver = server.repo_resolver
        observed: dict = {}

        def sample(persist: bool) -> dict:
            return _run_tool(
                "handle_summarize_sample",
                arguments={"repository": str(path), "persist": persist, "n": 1},
                dispatcher=server.dispatcher,
                repo_resolver=resolver,
                lazy_summarizer=None,
            )

        def operation(stage):
            observed["persist"] = sample(True)
            observed["preview"] = sample(False)
            return {"failed_files": 0}

        run_repository_mutation(resolver, resolver.resolve_ready(path), operation)

        assert observed["persist"]["code"] == "rebuild_in_progress"
        assert observed["persist"]["mutation_performed"] is False
        # A non-persisting sample only reads; it passes the rebuild gate and
        # stops at the (absent) summarizer instead.
        assert observed["preview"].get("code") != "rebuild_in_progress"
        assert observed["preview"]["error"] == "Summarization not available"


def test_lazy_summary_enqueue_is_skipped_during_a_rebuild(tmp_path):
    path, repo_id = build_temp_repo(tmp_path, "lazy", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        resolver = server.repo_resolver
        summarizer = _FakeSummarizer()

        def search() -> object:
            return _run_tool(
                "handle_search_code",
                arguments={"query": "alpha_token", "repository": str(path)},
                dispatcher=server.dispatcher,
                repo_resolver=resolver,
                sqlite_store=_FakeStore(),
                lazy_summarizer=summarizer,
            )

        observed: dict = {}

        def operation(stage):
            observed["search"] = search()
            return {"failed_files": 0}

        run_repository_mutation(resolver, resolver.resolve_ready(path), operation)

        assert "alpha.py" in _result_files(observed["search"])
        assert summarizer.enqueued == []
        # Control: once the rebuild published, the same search enqueues.
        assert "alpha.py" in _result_files(search())
        assert summarizer.enqueued


# ---------------------------------------------------------------------------
# One readiness observation per served query
# ---------------------------------------------------------------------------


def _count_git_status(monkeypatch) -> list:
    calls: list = []
    original = readiness_module._observe_git_state

    def counting(path):
        calls.append(str(path))
        return original(path)

    monkeypatch.setattr(readiness_module, "_observe_git_state", counting)
    return calls


def test_served_queries_observe_git_at_most_twice(tmp_path, monkeypatch):
    path, repo_id = build_temp_repo(tmp_path, "count", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        resolver = server.repo_resolver
        manager = resolver._index_manager
        (path / "beta.py").write_text("def beta_token():\n    return 2\n")
        git(path, "add", "beta.py")
        git(path, "commit", "-m", "add beta")
        calls = _count_git_status(monkeypatch)
        observed: dict = {}
        original = manager._rebuild_checkpoint

        def checkpoint(stage):
            if stage == "before_publish" and "search" not in observed:
                calls.clear()
                observed["search"] = _search(server, path, "alpha_token")
                observed["search_calls"] = len(calls)
                calls.clear()
                observed["symbol"] = _symbol(server, path, "AlphaSymbol")
                observed["symbol_calls"] = len(calls)
            return original(stage)

        monkeypatch.setattr(manager, "_rebuild_checkpoint", checkpoint)
        manager.sync_repository_index(repo_id)

        # Served stale during the rebuild, with at most admission + recheck.
        assert observed["search"]["readiness"]["state"] == "stale_commit"
        assert observed["symbol"]["readiness"]["state"] == "stale_commit"
        assert observed["search_calls"] <= 2, observed
        assert observed["symbol_calls"] <= 2, observed

        calls.clear()
        assert "alpha.py" in _result_files(_search(server, path, "alpha_token"))
        assert len(calls) <= 2, calls
        calls.clear()
        assert _symbol(server, path, "AlphaSymbol")["symbol"] == "AlphaSymbol"
        assert len(calls) <= 2, calls


@pytest.mark.parametrize("change", ["head_moved", "tree_dirtied"])
def test_post_query_recheck_catches_source_changes_during_the_query(tmp_path, monkeypatch, change):
    path, repo_id = build_temp_repo(tmp_path, f"recheck-{change}", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        original_search = server.dispatcher.search
        original_lookup = server.dispatcher.lookup

        def change_source():
            if change == "head_moved":
                (path / f"gamma{len(list(path.glob('gamma*.py')))}.py").write_text("g = 1\n")
                git(path, "add", ".")
                git(path, "commit", "-m", "move head")
            else:
                (path / "alpha.py").write_text("def alpha_token():\n    return 7\n")

        def search(ctx, query, **kwargs):
            results = list(original_search(ctx, query, **kwargs))
            change_source()
            return results

        def lookup(ctx, symbol):
            result = original_lookup(ctx, symbol)
            change_source()
            return result

        monkeypatch.setattr(server.dispatcher, "search", search)
        response = _search(server, path, "alpha_token")
        assert response["code"] == "index_unavailable", response

        if change == "tree_dirtied":
            git(path, "checkout", "--", "alpha.py")
        else:
            server.seed_repo_index(repo_id, path)
        monkeypatch.setattr(server.dispatcher, "lookup", lookup)
        symbol = _symbol(server, path, "AlphaSymbol")
        assert symbol["code"] == "index_unavailable", symbol


# ---------------------------------------------------------------------------
# Writes are fenced at the persistence point, after the provider call
# ---------------------------------------------------------------------------


import threading  # noqa: E402
import time  # noqa: E402

from mcp_server.indexing.summarization import (  # noqa: E402
    FileBatchSummarizer,
    LazyChunkWriter,
)


def _assert_matches_schema(tool: str, response: dict) -> None:
    import jsonschema

    from mcp_server.cli.stdio_runner import _build_tool_list

    schema = next(t.output_schema for t in _build_tool_list() if t.name == tool)
    jsonschema.validate(response, schema)


class _RebuildStarter:
    """Starts a competing writer while a summary provider call is in flight.

    ``fence_flag``: a staged build fenced the registry entry. ``rebuild_lock``:
    a staged build holds the writer lock and recorded itself as the live
    owner, before fencing. ``busy_lock``: an ordinary writer (a sync, or
    ``repository summarize``) holds the lock; no rebuild runs.
    """

    def __init__(self, server, repo_id: str, path: Path, how: str):
        self.server, self.repo_id, self.path, self.how = server, repo_id, path, how
        self._release = threading.Event()
        self._thread: threading.Thread | None = None
        self._owner: dict | None = None

    def start(self) -> None:
        if self.how == "none":
            return
        if self.how == "fence_flag":
            info = self.server.registry.get(self.repo_id)
            self.server.registry.begin_generation_mutation(
                self.repo_id,
                expected_registration_id=info.registration_id,
                expected_generation=info.index_generation,
            )
            return
        acquired = threading.Event()
        info = self.server.registry.get(self.repo_id)

        def hold():
            with locks.lock_registry.acquire(self.repo_id, repo_path=self.path):
                if self.how == "rebuild_lock":
                    self._owner = locks.record_rebuild_owner(
                        self.path,
                        registration_id=info.registration_id,
                        published_generation=info.index_generation,
                        target_commit=git_head(self.path),
                    )
                acquired.set()
                self._release.wait(10)

        self._thread = threading.Thread(target=hold, daemon=True)
        self._thread.start()
        assert acquired.wait(10)

    def stop(self) -> None:
        self._release.set()
        if self._thread is not None:
            self._thread.join(10)
        if self._owner is not None:
            locks.clear_rebuild_owner(self.path, self._owner["token"])


def _summary_rows(db_path: Path) -> int:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM chunk_summaries").fetchone()[0]


def _seed_chunk(
    db_path: Path, filename: str = "alpha.py", chunk_id: str = "chunk-alpha"
) -> tuple[int, str]:
    """Store one chunk through the store API, which stamps the chunk scheme."""
    from mcp_server.storage.sqlite_store import SQLiteStore

    store = SQLiteStore(str(db_path))
    try:
        with store._get_connection() as conn:
            file_id = conn.execute(
                "SELECT id FROM files WHERE path LIKE ?", (f"%{filename}",)
            ).fetchone()[0]
        store.store_chunk(
            file_id=file_id,
            content="def alpha_token():",
            content_start=0,
            content_end=18,
            line_start=1,
            line_end=2,
            chunk_id=chunk_id,
            node_id="n",
            treesitter_file_id="t",
            language="python",
        )
    finally:
        store.close()
    ReadinessClassifier.clear_index_inspection_cache()
    return file_id, chunk_id


@pytest.mark.parametrize("how", ["none", "fence_flag", "rebuild_lock", "busy_lock"])
def test_summarize_sample_write_is_fenced_after_the_provider_call(tmp_path, monkeypatch, how):
    path, repo_id = build_temp_repo(tmp_path, f"fenced-sample-{how}", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        published = Path(server.registry.get(repo_id).index_path)
        _seed_chunk(published)
        starter = _RebuildStarter(server, repo_id, path, how)
        provider_calls: list = []

        async def provider(self, *args, **kwargs):
            # The rebuild begins while the provider call is in flight.
            provider_calls.append(1)
            starter.start()
            from types import SimpleNamespace

            return [SimpleNamespace(chunk_id="chunk-alpha", summary="Synthetic summary.")]

        monkeypatch.setattr(FileBatchSummarizer, "_call_batch_api", provider)
        monkeypatch.setattr(FileBatchSummarizer, "_call_profile_batch_api", provider)
        monkeypatch.setattr(
            FileBatchSummarizer, "_get_model_name", lambda self, **kwargs: "fake-model"
        )
        try:
            response = _run_tool(
                "handle_summarize_sample",
                arguments={"repository": str(path), "persist": True, "n": 1},
                dispatcher=server.dispatcher,
                repo_resolver=server.repo_resolver,
                lazy_summarizer=_FakeSummarizer(),
            )
        finally:
            starter.stop()

        assert provider_calls == [1]
        if how == "none":
            # Control: with no rebuild the same call persists its summary.
            assert response["persisted"] is True and response["total_chunks"] == 1, response
            # Success responses are unchanged: no refusal-only fields.
            assert "summaries_committed" not in response
            assert all("summaries_committed" not in entry for entry in response["files"])
            assert _summary_rows(published) == 1
            _assert_matches_schema("summarize_sample", response)
            return
        expected = "writer_busy" if how == "busy_lock" else "rebuild_in_progress"
        assert response["code"] == expected, response
        assert response["retryable"] is True
        assert response["mutation_performed"] is False
        assert response["files"][0]["summaries_written"] == 0
        assert response["total_chunks"] == 0
        assert _summary_rows(published) == 0  # nothing reached the index
        _assert_matches_schema("summarize_sample", response)


@pytest.mark.parametrize("how", ["fence_flag", "rebuild_lock"])
def test_lazy_queue_entry_is_dropped_when_a_rebuild_starts_mid_call(tmp_path, monkeypatch, how):
    path, repo_id = build_temp_repo(tmp_path, f"fenced-lazy-{how}", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        info = server.registry.get(repo_id)
        published = Path(info.index_path)
        file_id, chunk_id = _seed_chunk(published)
        fence = server.repo_resolver.generation_write_fence(repo_id, info, path)
        starter = _RebuildStarter(server, repo_id, path, how)
        monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
        writer = LazyChunkWriter(
            db_path=str(published),
            qdrant_client=None,
            summarization_config={"base_url": "http://synthetic.invalid/v1"},
        )
        provider_calls: list = []

        async def direct(*args, **kwargs):
            provider_calls.append(1)
            starter.start()
            return "Synthetic summary.", "fake-model"

        monkeypatch.setattr(writer, "_has_sampling_capability", lambda: False)
        monkeypatch.setattr(writer, "_call_direct_api", direct)
        chunk = dict(
            chunk_hash=chunk_id,
            file_id=file_id,
            chunk_start=1,
            chunk_end=2,
            symbol="alpha_token",
            content="def alpha_token():",
            language="python",
            is_authoritative=False,
        )
        try:
            stored = asyncio.run(writer._summarize_queued((chunk, fence)))
        finally:
            starter.stop()
        assert provider_calls == [1]
        assert stored is None
        assert writer.fenced_writes == 1 and writer.busy_writes == 0
        assert writer.queue.qsize() == 0  # dropped, not requeued
        assert _summary_rows(published) == 0

        # An entry whose generation is already fenced is dropped before any
        # provider call; with no rebuild the same entry is stored.
        if how == "fence_flag":
            provider_calls.clear()
            assert asyncio.run(writer._summarize_queued((chunk, fence))) is None
            assert provider_calls == []


def test_lazy_queue_entry_is_stored_without_a_rebuild(tmp_path, monkeypatch):
    path, repo_id = build_temp_repo(tmp_path, "lazy-stored", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        info = server.registry.get(repo_id)
        published = Path(info.index_path)
        file_id, chunk_id = _seed_chunk(published)
        fence = server.repo_resolver.generation_write_fence(repo_id, info, path)
        monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
        writer = LazyChunkWriter(
            db_path=str(published),
            qdrant_client=None,
            summarization_config={"base_url": "http://synthetic.invalid/v1"},
        )

        async def direct(*args, **kwargs):
            return "Synthetic summary.", "fake-model"

        monkeypatch.setattr(writer, "_has_sampling_capability", lambda: False)
        monkeypatch.setattr(writer, "_call_direct_api", direct)
        chunk = dict(
            chunk_hash=chunk_id,
            file_id=file_id,
            chunk_start=1,
            chunk_end=2,
            symbol="alpha_token",
            content="def alpha_token():",
            language="python",
        )
        assert asyncio.run(writer._summarize_queued((chunk, fence))) == "Synthetic summary."
        assert _summary_rows(published) == 1
        writer_store = writer._sqlite_store
        if writer_store is not None:
            writer_store.close()


def test_search_enqueues_lazy_entries_with_a_generation_fence(tmp_path):
    path, repo_id = build_temp_repo(tmp_path, "lazy-fence", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        summarizer = _FakeSummarizer()
        _run_tool(
            "handle_search_code",
            arguments={"query": "alpha_token", "repository": str(path)},
            dispatcher=server.dispatcher,
            repo_resolver=server.repo_resolver,
            sqlite_store=_FakeStore(),
            lazy_summarizer=summarizer,
        )
        assert summarizer.enqueued
        _chunk, fence = summarizer.enqueued[0]
        with fence() as gate:
            assert gate is locks.WriteGate.ALLOWED
        info = server.registry.get(repo_id)
        server.registry.begin_generation_mutation(
            repo_id,
            expected_registration_id=info.registration_id,
            expected_generation=info.index_generation,
        )
        with fence() as gate:
            assert gate is locks.WriteGate.FENCED


def test_rebuild_in_progress_responses_match_the_advertised_schemas(tmp_path, monkeypatch):
    path, repo_id = build_temp_repo(tmp_path, "schemas", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        resolver = server.repo_resolver
        _install_fake_writer(monkeypatch)
        observed: dict = {}

        def operation(stage):
            observed["write"] = _run_tool(
                "handle_write_summaries",
                arguments={"repository": str(path)},
                dispatcher=server.dispatcher,
                repo_resolver=resolver,
                lazy_summarizer=_FakeSummarizer(),
            )
            observed["sample"] = _run_tool(
                "handle_summarize_sample",
                arguments={"repository": str(path), "persist": True, "n": 1},
                dispatcher=server.dispatcher,
                repo_resolver=resolver,
                lazy_summarizer=_FakeSummarizer(),
            )
            return {"failed_files": 0}

        run_repository_mutation(resolver, resolver.resolve_ready(path), operation)

        assert observed["write"]["code"] == "rebuild_in_progress"
        assert observed["sample"]["code"] == "rebuild_in_progress"
        _assert_matches_schema("write_summaries", observed["write"])
        _assert_matches_schema("summarize_sample", observed["sample"])


# ---------------------------------------------------------------------------
# A busy (non-rebuild) writer is retryable, never a silent drop
# ---------------------------------------------------------------------------


def _lazy_writer(published: Path, monkeypatch, on_call=None) -> tuple:
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    writer = LazyChunkWriter(
        db_path=str(published),
        qdrant_client=None,
        summarization_config={"base_url": "http://synthetic.invalid/v1"},
    )
    calls: list = []

    async def direct(*args, **kwargs):
        calls.append(1)
        if on_call is not None:
            on_call()
        return "Synthetic summary.", "fake-model"

    monkeypatch.setattr(writer, "_has_sampling_capability", lambda: False)
    monkeypatch.setattr(writer, "_call_direct_api", direct)
    return writer, calls


def _lazy_chunk(file_id: int, chunk_id: str) -> dict:
    return dict(
        chunk_hash=chunk_id,
        file_id=file_id,
        chunk_start=1,
        chunk_end=2,
        symbol="alpha_token",
        content="def alpha_token():",
        language="python",
    )


def test_lazy_entry_preflight_never_records_into_an_outer_tally(tmp_path, monkeypatch):
    """A lazy entry's preflight BUSY outcome stays in the entry's own tally."""
    from mcp_server.indexing import summarization

    path, repo_id = build_temp_repo(tmp_path, "lazy-outer-tally", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        info = server.registry.get(repo_id)
        published = Path(info.index_path)
        file_id, chunk_id = _seed_chunk(published)
        fence = server.repo_resolver.generation_write_fence(repo_id, info, path)
        writer, calls = _lazy_writer(published, monkeypatch)
        writer.enqueue(_lazy_chunk(file_id, chunk_id), write_fence=fence)
        busy = _RebuildStarter(server, repo_id, path, "busy_lock")
        busy.start()
        try:
            # The worker runs inside a context that already has an unrelated
            # caller's tally active; the entry's preflight sees a busy writer.
            with summarization._call_tally() as outer:
                assert asyncio.run(writer._summarize_queued(writer.queue.get_nowait())) is None
            assert calls == []
            assert outer.busy == 0 and outer.fenced == 0
            assert writer.queue.get_nowait().attempts == 1
        finally:
            busy.stop()
        if writer._sqlite_store is not None:
            writer._sqlite_store.close()


def test_lazy_entry_is_requeued_while_a_non_rebuild_writer_holds_the_lock(tmp_path, monkeypatch):
    path, repo_id = build_temp_repo(tmp_path, "lazy-busy", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        info = server.registry.get(repo_id)
        published = Path(info.index_path)
        file_id, chunk_id = _seed_chunk(published)
        fence = server.repo_resolver.generation_write_fence(repo_id, info, path)
        busy = _RebuildStarter(server, repo_id, path, "busy_lock")
        writer, calls = _lazy_writer(published, monkeypatch, on_call=busy.start)
        writer.enqueue(_lazy_chunk(file_id, chunk_id), write_fence=fence)
        try:
            # The busy writer took the lock during the provider call.
            assert asyncio.run(writer._summarize_queued(writer.queue.get_nowait())) is None
            assert writer.busy_writes == 1 and writer.fenced_writes == 0
            assert _summary_rows(published) == 0
            requeued = writer.queue.get_nowait()
            assert requeued.attempts == 1 and requeued.not_before > 0
            # Still busy when it comes round again: requeued before any
            # provider call.
            calls.clear()
            assert asyncio.run(writer._summarize_queued(requeued)) is None
            assert calls == []
            requeued = writer.queue.get_nowait()
            assert requeued.attempts == 2
        finally:
            busy.stop()
        # The other writer finished and no rebuild ran: the generation is
        # rechecked and the entry is stored.
        assert asyncio.run(writer._summarize_queued(requeued)) == "Synthetic summary."
        assert _summary_rows(published) == 1
        assert writer.queue.qsize() == 0
        if writer._sqlite_store is not None:
            writer._sqlite_store.close()


def test_lazy_busy_retries_are_bounded(tmp_path, monkeypatch, caplog):
    from mcp_server.indexing.summarization import _QueuedSummary

    path, repo_id = build_temp_repo(tmp_path, "lazy-bounded", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        info = server.registry.get(repo_id)
        published = Path(info.index_path)
        file_id, chunk_id = _seed_chunk(published)
        fence = server.repo_resolver.generation_write_fence(repo_id, info, path)
        writer, calls = _lazy_writer(published, monkeypatch)
        busy = _RebuildStarter(server, repo_id, path, "busy_lock")
        busy.start()
        try:
            last = _QueuedSummary(
                chunk=_lazy_chunk(file_id, chunk_id),
                fence=fence,
                attempts=writer.MAX_BUSY_RETRIES,
            )
            with caplog.at_level("WARNING"):
                assert asyncio.run(writer._summarize_queued(last)) is None
        finally:
            busy.stop()
        assert writer.queue.qsize() == 0
        assert calls == []
        assert "stayed busy" in caplog.text


def test_write_summaries_reports_writer_busy_with_partial_counts(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from mcp_server.indexing.summarization import ComprehensiveChunkWriter

    path, repo_id = build_temp_repo(tmp_path, "busy-write", seed_files=SEED)
    with boot_test_server(tmp_path, [path]) as server:
        published = Path(server.registry.get(repo_id).index_path)
        _seed_chunk(published)
        busy = _RebuildStarter(server, repo_id, path, "busy_lock")

        async def provider(self, *args, **kwargs):
            if not busy._release.is_set():
                busy.start()
            return [SimpleNamespace(chunk_id="chunk-alpha", summary="Synthetic summary.")]

        monkeypatch.setattr(FileBatchSummarizer, "_call_batch_api", provider)
        monkeypatch.setattr(FileBatchSummarizer, "_call_profile_batch_api", provider)
        monkeypatch.setattr(
            FileBatchSummarizer, "_get_model_name", lambda self, **kwargs: "fake-model"
        )
        monkeypatch.setattr(ComprehensiveChunkWriter, "can_summarize", lambda self: True)
        common = dict(
            arguments={"repository": str(path)},
            dispatcher=server.dispatcher,
            repo_resolver=server.repo_resolver,
            lazy_summarizer=_FakeSummarizer(),
        )
        try:
            response = _run_tool("handle_write_summaries", **common)
        finally:
            busy.stop()

        assert response["code"] == "writer_busy", response
        assert response["retryable"] is True
        assert response["mutation_performed"] is False
        assert response["chunks_summarized"] == 0
        assert response["summary_chunks_attempted"] == 1
        assert response["readiness"]["rebuild"] is None  # no rebuild was running
        assert _summary_rows(published) == 0
        _assert_matches_schema("write_summaries", response)

        # Once the other writer is done, the retry stores the summary.
        retry = _run_tool("handle_write_summaries", **common)
        assert retry["persisted"] is True and retry["chunks_summarized"] == 1, retry
        assert _summary_rows(published) == 1
        _assert_matches_schema("write_summaries", retry)


class _FakeTask:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.is_cancelled = False

    def request_cancellation(self) -> None:
        self.is_cancelled = True

    async def update_status(self, message: str) -> None:
        pass


@pytest.mark.parametrize(
    ("outcome", "code"),
    [("write_fenced", "rebuild_in_progress"), ("write_busy", "writer_busy")],
)
def test_task_write_summaries_refusal_matches_the_advertised_schema(monkeypatch, outcome, code):
    from types import SimpleNamespace

    from mcp.types import TaskMetadata

    from mcp_server.cli import task_write_summaries
    from mcp_server.indexing.summarization import SummaryGenerationResult
    from mcp_server.storage.mcp_task_registry import MCPTaskRegistry

    class Writer:
        def __init__(self, **kwargs):
            pass

        async def process_scope(self, **kwargs):
            # The very first batch is refused, so nothing was persisted.
            return SummaryGenerationResult(
                chunks_attempted=2, summaries_written=0, remaining_chunks=2, **{outcome: True}
            )

    monkeypatch.setattr(task_write_summaries, "ComprehensiveChunkWriter", Writer)
    monkeypatch.setattr(
        task_write_summaries.Settings,
        "from_environment",
        staticmethod(
            lambda: SimpleNamespace(
                get_semantic_default_profile=lambda: "oss_high",
                get_profile_summarization_config=lambda _profile: {},
            )
        ),
    )

    async def run():
        registry = MCPTaskRegistry()
        state = await registry.create_task(TaskMetadata())
        return await task_write_summaries.run_write_summaries_task(
            task=_FakeTask(state.task_id),
            registry=registry,
            active_store=SimpleNamespace(db_path="/synthetic/index.db"),
            current_session=None,
            client_name=None,
            limit_arg=10,
            model_used="fake-model",
            write_blocked=lambda: False,
        )

    payload = asyncio.run(run()).structured_content
    assert payload["code"] == code
    assert payload["tool"] == "write_summaries"
    assert payload["mutation_performed"] is False
    assert payload["chunks_summarized"] == 0
    assert payload["summary_chunks_attempted"] == 2
    assert payload["summary_missing_chunks"] == 2
    _assert_matches_schema("write_summaries", payload)


# ---------------------------------------------------------------------------
# Refusals report only what this request itself committed
# ---------------------------------------------------------------------------


def test_refusal_after_another_writer_stored_this_requests_summaries(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from mcp_server.indexing.summarization import ComprehensiveChunkWriter
    from mcp_server.storage.sqlite_store import SQLiteStore

    seed = {**SEED, "beta.py": "def beta_token():\n    return 2\n"}
    path, repo_id = build_temp_repo(tmp_path, "committed", seed_files=seed)
    with boot_test_server(tmp_path, [path]) as server:
        published = Path(server.registry.get(repo_id).index_path)
        _seed_chunk(published)
        _seed_chunk(published, filename="beta.py", chunk_id="chunk-beta")
        busy = _RebuildStarter(server, repo_id, path, "busy_lock")
        calls: list = []

        async def provider(self, *args, **kwargs):
            chunks = next(
                value
                for value in list(args) + list(kwargs.values())
                if isinstance(value, list) and value and isinstance(value[0], dict)
            )
            calls.append([chunk["chunk_id"] for chunk in chunks])
            if len(calls) == 1:
                # Another writer stores this call's summaries while the
                # provider call is in flight.
                other = SQLiteStore(str(published))
                try:
                    for chunk in chunks:
                        other.store_chunk_summary(
                            chunk_hash=chunk["chunk_id"],
                            file_id=chunk["file_id"],
                            chunk_start=1,
                            chunk_end=2,
                            summary_text="Synthetic summary.",
                            llm_model="other-writer",
                            is_authoritative=True,
                            profile_id=self.summarization_config.get("profile_id"),
                            prompt_fingerprint=self._prompt_fingerprint(),
                        )
                finally:
                    other.close()
            else:
                busy.start()
            return [
                SimpleNamespace(chunk_id=chunk["chunk_id"], summary="Synthetic summary.")
                for chunk in chunks
            ]

        monkeypatch.setattr(FileBatchSummarizer, "_call_batch_api", provider)
        monkeypatch.setattr(FileBatchSummarizer, "_call_profile_batch_api", provider)
        monkeypatch.setattr(
            FileBatchSummarizer, "_get_model_name", lambda self, **kwargs: "fake-model"
        )
        monkeypatch.setattr(ComprehensiveChunkWriter, "can_summarize", lambda self: True)
        try:
            response = _run_tool(
                "handle_write_summaries",
                arguments={"repository": str(path)},
                dispatcher=server.dispatcher,
                repo_resolver=server.repo_resolver,
                lazy_summarizer=_FakeSummarizer(),
            )
        finally:
            busy.stop()

        assert len(calls) == 2, calls
        assert response["code"] == "writer_busy", response
        # The first file's summaries were completed by the other writer, not
        # stored by this request, so nothing was mutated by it.
        assert response["mutation_performed"] is False
        assert response["summaries_committed"] == 0
        assert response["chunks_summarized"] == 0
        assert "note" not in response
        assert _summary_rows(published) == 1  # only the other writer's row
        _assert_matches_schema("write_summaries", response)


def test_task_refusal_counts_only_committed_summaries(monkeypatch):
    from types import SimpleNamespace

    from mcp.types import TaskMetadata

    from mcp_server.cli import task_write_summaries
    from mcp_server.indexing.summarization import SummaryGenerationResult
    from mcp_server.storage.mcp_task_registry import MCPTaskRegistry

    passes = [
        # Pass 1 completed a summary another writer had stored meanwhile.
        SummaryGenerationResult(
            chunks_attempted=1, summaries_written=1, summaries_committed=0, remaining_chunks=1
        ),
        # Pass 2 hit a busy writer.
        SummaryGenerationResult(
            chunks_attempted=1, summaries_written=0, remaining_chunks=1, write_busy=True
        ),
    ]

    class Writer:
        def __init__(self, **kwargs):
            pass

        async def process_scope(self, **kwargs):
            return passes.pop(0)

    monkeypatch.setattr(task_write_summaries, "ComprehensiveChunkWriter", Writer)
    monkeypatch.setattr(
        task_write_summaries.Settings,
        "from_environment",
        staticmethod(
            lambda: SimpleNamespace(
                get_semantic_default_profile=lambda: "oss_high",
                get_profile_summarization_config=lambda _profile: {},
            )
        ),
    )

    async def run():
        registry = MCPTaskRegistry()
        state = await registry.create_task(TaskMetadata())
        return await task_write_summaries.run_write_summaries_task(
            task=_FakeTask(state.task_id),
            registry=registry,
            active_store=SimpleNamespace(db_path="/synthetic/index.db"),
            current_session=None,
            client_name=None,
            limit_arg=10,
            model_used="fake-model",
            write_blocked=lambda: False,
        )

    payload = asyncio.run(run()).structured_content
    assert passes == []
    assert payload["code"] == "writer_busy"
    assert payload["mutation_performed"] is False
    assert payload["summaries_committed"] == 0
    assert payload["chunks_summarized"] == 0
    _assert_matches_schema("write_summaries", payload)


def test_lazy_writer_shutdown_logs_dropped_entries(tmp_path, caplog):
    from mcp_server.indexing.summarization import _QueuedSummary

    writer = LazyChunkWriter(db_path=str(tmp_path / "unused.db"), qdrant_client=None)

    async def run():
        writer.start()
        # One entry backing off in the worker, two still queued.
        writer.queue.put_nowait(
            _QueuedSummary(chunk={}, fence=None, attempts=1, not_before=time.monotonic() + 60)
        )
        await asyncio.sleep(0.05)
        writer.enqueue({"chunk_hash": "a"})
        writer.enqueue({"chunk_hash": "b"})
        with caplog.at_level("INFO", logger="mcp_server.indexing.summarization"):
            await writer.stop()

    asyncio.run(run())
    assert "3 pending entries dropped" in caplog.text
    assert "chunk_hash" not in caplog.text  # counts only, no content


def test_overlapping_calls_on_one_writer_count_only_their_own_commits(tmp_path, monkeypatch):
    from types import SimpleNamespace

    seed = {**SEED, "beta.py": "def beta_token():\n    return 2\n"}
    path, repo_id = build_temp_repo(tmp_path, "overlap", seed_files=seed)
    with boot_test_server(tmp_path, [path]) as server:
        published = Path(server.registry.get(repo_id).index_path)
        alpha_file, alpha_chunk = _seed_chunk(published)
        beta_file, beta_chunk = _seed_chunk(published, filename="beta.py", chunk_id="chunk-beta")
        writer = FileBatchSummarizer(
            db_path=str(published),
            qdrant_client=None,
            summarization_config={"profile_id": "synthetic-profile"},
        )
        monkeypatch.setattr(writer, "_get_model_name", lambda **kwargs: "fake-model")
        order: list = []

        async def provider(file_id, file_path, file_content, chunks, symbol_map):
            if chunks[0]["chunk_id"] == alpha_chunk:
                # Let the other call start and enter its provider first.
                await asyncio.sleep(0.02)
                order.append("alpha-returns")
                return [SimpleNamespace(chunk_id=alpha_chunk, summary="Alpha summary.")]
            order.append("beta-waits")
            # Stay in flight until the alpha call has committed its row.
            while writer.committed_writes < 1:
                await asyncio.sleep(0.005)
            order.append("beta-returns")
            return []  # nothing to store for this call

        monkeypatch.setattr(writer, "_call_batch_api", provider)

        def chunk(file_id: int, chunk_id: str) -> dict:
            return {
                "chunk_id": chunk_id,
                "file_id": file_id,
                "line_start": 1,
                "line_end": 2,
                "content": "def token():",
                "language": "python",
            }

        async def run():
            return await asyncio.gather(
                writer.summarize_file_chunks(
                    alpha_file, "alpha.py", "def alpha_token():", [chunk(alpha_file, alpha_chunk)]
                ),
                writer.summarize_file_chunks(
                    beta_file, "beta.py", "def beta_token():", [chunk(beta_file, beta_chunk)]
                ),
            )

        alpha_result, beta_result = asyncio.run(run())
        writer._get_sqlite_store().close()

        assert order == ["beta-waits", "alpha-returns", "beta-returns"], order
        assert alpha_result.summaries_committed == 1
        # The beta call overlapped the alpha commit but stored nothing itself.
        assert beta_result.summaries_committed == 0
        assert writer.committed_writes == 1
        assert _summary_rows(published) == 1
