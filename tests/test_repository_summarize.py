"""``mcp-index repository summarize``: resumable summary prefill (#121).

Every summarizer here is a loopback double patched over the provider call; no
test reaches the network or a Qdrant server (the staged rebuild uses the
generation's local-mode vector store, as the other staged-rebuild tests do).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mcp_server.cli import repository_commands
from mcp_server.cli.repository_commands import repository
from mcp_server.config.settings import Settings
from mcp_server.core.repo_context import RepoContext
from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher
from mcp_server.health.repository_readiness import ReadinessClassifier
from mcp_server.indexing import summarization
from mcp_server.setup.semantic_preflight import EnrichmentModelResolution
from mcp_server.storage.git_index_manager import GitAwareIndexManager
from mcp_server.storage.repository_registry import RepositoryRegistry
from tests.test_embedding_provenance import _FakeProvenanceProvider, _openai_response, _profile
from tests.test_git_index_manager import _make_git_repo

_SOURCES = {
    "alpha.py": (
        "def alpha_one(x):\n    return x + 1\n\n\n"
        "def alpha_two(y):\n    return y * 2\n\n\n"
        "class AlphaThing:\n    def method(self):\n        return 'alpha'\n"
    ),
    "beta.py": (
        "def beta_one(items):\n    return [i for i in items if i]\n\n\n"
        "def beta_two(text):\n    return text.upper()\n"
    ),
    "gamma.py": "def gamma(value):\n    return str(value)\n",
}


def _profile_payload(*, enrichment: bool = True) -> str:
    payload = _profile().to_dict()
    if enrichment:
        payload.setdefault("build_metadata", {}).update(
            {
                # Discard port: a call that escaped the loopback double fails fast.
                "enrichment_api_base": "http://127.0.0.1:9/v1",
                "enrichment_model_name": "fixture-summarizer",
            }
        )
    return json.dumps({"fixture": payload})


def _settings(*, enrichment: bool = True) -> Settings:
    return Settings(
        semantic_search_enabled=True,
        semantic_default_profile="fixture",
        semantic_profiles_json=_profile_payload(enrichment=enrichment),
    )


class _LoopbackSummarizer:
    """Stands in for the profile endpoint and records every summary call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.on_call = None
        self.delay_seconds = 0.0
        # Which chunk IDs the "provider" answers for; default: all requested.
        self.answer = None

    async def __call__(self, writer, system, prompt, *, max_tokens=150):
        chunk_ids = re.findall(r"^chunk_id: (.+)$", prompt, flags=re.MULTILINE)
        self.calls.append(chunk_ids)
        if self.on_call is not None:
            self.on_call(len(self.calls))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        answered = self.answer(chunk_ids) if self.answer is not None else chunk_ids
        body = {
            "summaries": [
                {"chunk_id": cid, "summary": f"Summary of {cid}. It is synthetic."}
                for cid in answered
            ]
        }
        return json.dumps(body), "fixture-summarizer"

    @property
    def requested(self) -> list[str]:
        return [cid for call in self.calls for cid in call]


@pytest.fixture
def prefill(tmp_path, monkeypatch, capsys):
    repo = _make_git_repo(tmp_path)
    (repo / ".git" / "info" / "exclude").write_text("/.mcp-index/\n")
    for name, text in _SOURCES.items():
        (repo / name).write_text(text)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "sources"], cwd=repo, check=True)
    monkeypatch.setenv("MCP_INDEX_STORAGE_PATH", str(tmp_path / "indexes"))
    monkeypatch.setenv("MCP_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("MCP_ENABLE_MULTI_REPO", "false")
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv("MCP_DEPLOYMENT_PROFILE", raising=False)
    monkeypatch.chdir(tmp_path)
    settings = _settings()
    monkeypatch.setattr("mcp_server.config.settings.get_settings", lambda: settings)
    monkeypatch.setattr("mcp_server.config.settings.reload_settings", lambda: settings)
    monkeypatch.setattr("mcp_server.utils.semantic_indexer_registry.get_settings", lambda: settings)
    monkeypatch.setattr(
        "mcp_server.dispatcher.dispatcher_enhanced.reload_settings", lambda: settings
    )
    monkeypatch.setattr(repository_commands, "reload_settings", lambda: settings)
    monkeypatch.setattr(
        summarization,
        "resolve_enrichment_model",
        lambda **kw: EnrichmentModelResolution(
            configured_model=kw["configured_model"],
            effective_model=kw["configured_model"],
            resolution_strategy="configured",
        ),
    )
    summarizer = _LoopbackSummarizer()
    monkeypatch.setattr(
        summarization.ChunkWriter,
        "_call_profile_api",
        lambda self, system, prompt, **kw: summarizer(self, system, prompt, **kw),
    )

    registry = RepositoryRegistry(tmp_path / "registry.json")
    repo_id = registry.register_repository(str(repo))
    # The command builds its own registry, as a separate CLI process would.
    monkeypatch.setattr(
        repository_commands,
        "RepositoryRegistry",
        lambda: RepositoryRegistry(tmp_path / "registry.json"),
    )
    dispatcher = EnhancedDispatcher(
        plugins=[],
        enable_advanced_features=False,
        use_plugin_factory=False,
        semantic_search_enabled=False,
        memory_aware=False,
        multi_repo_enabled=False,
    )
    manager = GitAwareIndexManager(registry, dispatcher)
    lexical = manager.rebuild_repository_index(repo_id)
    assert lexical.action == "full_index", lexical.error
    ReadinessClassifier.clear_index_inspection_cache()
    try:
        yield {
            "repo": repo,
            "repo_id": repo_id,
            "registry": registry,
            "manager": manager,
            "settings": settings,
            "summarizer": summarizer,
            "tmp_path": tmp_path,
            "monkeypatch": monkeypatch,
            "capsys": capsys,
        }
    finally:
        dispatcher.shutdown()
        if manager.store_registry is not None:
            manager.store_registry.shutdown()


def _index_path(ctx) -> Path:
    return Path(ctx["registry"].get(ctx["repo_id"]).index_path)


def _chunk_ids(ctx) -> set[str]:
    with closing(sqlite3.connect(_index_path(ctx))) as conn:
        return {row[0] for row in conn.execute("SELECT chunk_id FROM code_chunks")}


def _summaries(ctx) -> dict[str, tuple]:
    with closing(sqlite3.connect(_index_path(ctx))) as conn:
        return {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT chunk_hash, summary_text, is_authoritative, profile_id, "
                "prompt_fingerprint FROM chunk_summaries"
            )
        }


def _summarize(ctx, *args: str, selector: str | None = None):
    """Run the command in-process.

    pytest.ini enables ``log_cli``, whose live logging resets ``sys.stdout``
    around every record and so defeats ``CliRunner``'s stream swap; fixture
    capture (``capsys``) is restored correctly, so the command runs under it.
    """
    capsys = ctx["capsys"]
    capsys.readouterr()
    with pytest.raises(SystemExit) as exited:
        repository.main(
            ["summarize", selector or str(ctx["repo"]), "--json", *args],
            prog_name="repository",
        )
    out, err = capsys.readouterr()
    code = exited.value.code
    result = SimpleNamespace(
        exit_code=code if isinstance(code, int) else (0 if code is None else 1),
        stdout=out,
        stderr=err,
        output=out + err,
    )
    payload = json.loads(out) if out.strip() else None
    return result, payload


def _contract(ctx) -> dict:
    manager = ctx["manager"]
    store = manager._resolve_ctx(ctx["repo_id"]).sqlite_store
    return manager.dispatcher.get_semantic_summary_contract(
        RepoContext(ctx["repo_id"], store, ctx["repo"], "main", ctx["registry"].get(ctx["repo_id"]))
    )


def _semantic_force_full(ctx):
    """Run `repository sync --force-full` with semantic search enabled."""
    from mcp_server.utils.semantic_indexer_registry import SemanticIndexerRegistry

    manager = ctx["manager"]
    provider = _FakeProvenanceProvider(_openai_response)
    ctx["monkeypatch"].setattr(
        "mcp_server.utils.semantic_indexer.create_embedding_provider", lambda **kw: provider
    )
    ctx["monkeypatch"].setattr(
        "mcp_server.setup.semantic_preflight.run_semantic_preflight",
        MagicMock(side_effect=AssertionError("Unowned global backend probe")),
    )
    manager.dispatcher._semantic_enabled = True
    manager.dispatcher._semantic_registry = SemanticIndexerRegistry(ctx["registry"])
    rebuilt = manager.sync_repository_index(
        ctx["repo_id"], force_full=True, reason="operator requested --force-full"
    )
    return rebuilt, provider


def test_staged_rebuild_without_prefill_calls_the_summarizer(prefill):
    """Control for the reuse proof: the loopback summarizer is on the staged path."""
    ctx = prefill
    chunks = _chunk_ids(ctx)

    rebuilt, _provider = _semantic_force_full(ctx)

    assert rebuilt.action == "full_index", (rebuilt.error, rebuilt.semantic)
    assert sorted(ctx["summarizer"].requested) == sorted(chunks)
    assert rebuilt.semantic["summaries_written"] == len(chunks)


def test_prefilled_summaries_are_reused_by_the_next_staged_rebuild(prefill):
    ctx = prefill
    chunks = _chunk_ids(ctx)
    assert len(chunks) >= 5
    assert _summaries(ctx) == {}

    result, payload = _summarize(ctx)

    assert result.exit_code == 0, result.output
    assert payload["status"] == "complete"
    assert payload["stop_reason"] == "drained"
    assert payload["written"] == len(chunks)
    assert payload["remaining_at_start"] == len(chunks)
    assert payload["remaining"] == 0
    assert payload["elapsed_seconds"] >= 0
    contract = _contract(ctx)
    assert payload["profile_id"] == contract["profile_id"] == "fixture"
    assert payload["prompt_fingerprint"] == contract["prompt_fingerprint"]
    prefilled = _summaries(ctx)
    assert set(prefilled) == chunks
    # Same provenance the staged build checks in prepare_generation.
    assert {row[1:] for row in prefilled.values()} == {
        (1, contract["profile_id"], contract["prompt_fingerprint"])
    }
    assert sorted(ctx["summarizer"].requested) == sorted(chunks)
    generation_before = ctx["registry"].get(ctx["repo_id"]).index_generation

    # Now the operator's `repository sync --force-full` with semantic on.
    ctx["summarizer"].calls.clear()
    manager = ctx["manager"]
    rebuilt, provider = _semantic_force_full(ctx)

    assert rebuilt.action == "full_index", (rebuilt.error, rebuilt.semantic)
    assert ctx["summarizer"].calls == []  # zero new summary calls
    assert rebuilt.semantic["summaries_written"] == 0
    assert provider.calls  # it went straight to embedding
    info = ctx["registry"].get(ctx["repo_id"])
    assert info.index_generation != generation_before
    assert _chunk_ids(ctx) == chunks
    assert _summaries(ctx) == prefilled
    published = manager._resolve_ctx(ctx["repo_id"])
    semantic = ReadinessClassifier.classify_semantic_registered(
        published.registry_entry, published.sqlite_store
    )
    assert semantic.ready, semantic.to_dict()


def test_limit_windows_resume_with_only_the_missing_chunks(prefill):
    ctx = prefill
    chunks = _chunk_ids(ctx)

    first, first_payload = _summarize(ctx, "--limit", "2")
    assert first.exit_code == 0, first.output
    assert first_payload["status"] == "partial"
    assert first_payload["stop_reason"] == "limit"
    assert first_payload["written"] == 2
    assert first_payload["remaining"] == len(chunks) - 2
    assert len(_summaries(ctx)) == 2
    assert "written 2, remaining" in first.stderr

    second, second_payload = _summarize(ctx)
    assert second.exit_code == 0, second.output
    assert second_payload["status"] == "complete"
    assert second_payload["remaining_at_start"] == len(chunks) - 2
    assert second_payload["written"] == len(chunks) - 2
    assert set(_summaries(ctx)) == chunks
    # No chunk was sent to the summarizer twice across the two windows.
    requested = ctx["summarizer"].requested
    assert sorted(requested) == sorted(chunks)

    third, third_payload = _summarize(ctx)
    assert third.exit_code == 0, third.output
    assert third_payload["written"] == 0
    assert third_payload["remaining"] == 0
    assert len(ctx["summarizer"].requested) == len(chunks)


def test_max_seconds_closes_the_window_between_calls(prefill):
    ctx = prefill
    chunks = _chunk_ids(ctx)
    ctx["summarizer"].delay_seconds = 0.6

    result, payload = _summarize(ctx, "--max-seconds", "0.25")

    assert result.exit_code == 0, result.output
    assert payload["status"] == "partial"
    assert payload["stop_reason"] == "max_seconds"
    # The call in flight when the window closed finished and was kept.
    assert len(ctx["summarizer"].calls) == 1
    assert payload["written"] == len(ctx["summarizer"].calls[0]) > 0
    assert payload["remaining"] == len(chunks) - payload["written"]
    assert len(_summaries(ctx)) == payload["written"]


def _requires_main_thread():
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers can only be installed from the main thread")


def test_ctrl_c_stops_between_calls_and_keeps_what_was_written(prefill):
    _requires_main_thread()
    ctx = prefill
    chunks = _chunk_ids(ctx)
    ctx["summarizer"].on_call = lambda n: n == 1 and os.kill(os.getpid(), signal.SIGINT)
    previous = signal.getsignal(signal.SIGINT)

    result, payload = _summarize(ctx)

    assert signal.getsignal(signal.SIGINT) is previous
    assert result.exit_code == 130, result.output
    assert payload["status"] == "interrupted"
    assert len(ctx["summarizer"].calls) == 1
    written = len(ctx["summarizer"].calls[0])
    assert payload["written"] == written > 0
    assert payload["remaining"] == len(chunks) - written
    assert len(_summaries(ctx)) == written

    rerun, rerun_payload = _summarize(ctx)
    assert rerun.exit_code == 0, rerun.output
    assert rerun_payload["written"] == len(chunks) - written
    assert set(_summaries(ctx)) == chunks


def test_second_ctrl_c_aborts_the_call_without_partial_rows(prefill):
    _requires_main_thread()
    ctx = prefill
    chunks = _chunk_ids(ctx)

    def interrupt_twice(n):
        if n == 1:
            os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGINT)

    ctx["summarizer"].on_call = interrupt_twice

    result, payload = _summarize(ctx)

    assert result.exit_code == 130, result.output
    assert payload["status"] == "interrupted"
    assert payload["written"] == 0
    assert payload["remaining"] == len(chunks)
    assert _summaries(ctx) == {}


def _assert_refused(ctx, result, payload, reason):
    assert result.exit_code == 1, result.output
    assert payload["status"] == "refused"
    assert payload["reason"] == reason
    assert payload["message"]
    assert ctx["summarizer"].calls == []
    assert _summaries(ctx) == {}


def test_refuses_an_unregistered_repository(prefill):
    result, payload = _summarize(prefill, selector="no-such-repo")
    _assert_refused(prefill, result, payload, "repository_not_registered")


def test_refuses_a_repository_that_is_not_ready(prefill):
    ctx = prefill
    (ctx["repo"] / "delta.py").write_text("def delta():\n    return 4\n")
    subprocess.run(["git", "add", "delta.py"], cwd=ctx["repo"], check=True)
    subprocess.run(["git", "commit", "-qm", "unsynced"], cwd=ctx["repo"], check=True)
    ReadinessClassifier.clear_index_inspection_cache()

    result, payload = _summarize(ctx)

    _assert_refused(ctx, result, payload, "repository_not_ready")
    assert "stale_commit" in payload["message"]
    assert payload["remediation"]


def test_refuses_when_summarization_is_not_configured(prefill):
    ctx = prefill
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CEREBRAS_API_KEY"):
        ctx["monkeypatch"].delenv(key, raising=False)
    unconfigured = _settings(enrichment=False)
    ctx["monkeypatch"].setattr(repository_commands, "reload_settings", lambda: unconfigured)

    result, payload = _summarize(ctx)

    _assert_refused(ctx, result, payload, "summarization_unconfigured")


def test_refuses_while_a_staged_rebuild_marker_is_recorded(prefill):
    ctx = prefill
    ctx["registry"].update_staleness_reason(ctx["repo_id"], "index_publication_pending")

    result, payload = _summarize(ctx)

    _assert_refused(ctx, result, payload, "staged_rebuild_in_progress")


@pytest.fixture
def foreign_writer(prefill):
    """Hold the repository writer lock from another process."""
    lock_path = prefill["repo"].resolve() / ".mcp-index" / "writer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(60)\n",
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        yield holder
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_refuses_while_another_writer_holds_the_lock(prefill, foreign_writer):
    result, payload = _summarize(prefill)
    _assert_refused(prefill, result, payload, "writer_lock_held")


def test_refuses_while_a_staged_rebuild_holds_the_lock(prefill, foreign_writer):
    ctx = prefill
    index_location = Path(ctx["registry"].get(ctx["repo_id"]).index_location)
    staging = index_location / ".current.db.staging-fixture"
    staging.mkdir()
    before = sorted(p.name for p in staging.iterdir())

    result, payload = _summarize(ctx)

    _assert_refused(ctx, result, payload, "staged_rebuild_in_progress")
    assert sorted(p.name for p in staging.iterdir()) == before


def test_window_uses_published_content_not_the_live_checkout(prefill):
    """Edits and commits during a window never reach a stored summary."""
    ctx = prefill
    chunks = _chunk_ids(ctx)
    repo = ctx["repo"]
    seen: dict[str, str] = {}
    original = summarization.FileBatchSummarizer.summarize_file_chunks

    async def recording(self, file_id, file_path, file_content, chunks, *args, **kwargs):
        seen[Path(file_path).name] = file_content
        return await original(self, file_id, file_path, file_content, chunks, *args, **kwargs)

    ctx["monkeypatch"].setattr(
        summarization.FileBatchSummarizer, "summarize_file_chunks", recording
    )

    def edit_checkout(n):
        if n != 1:
            return
        # An uncommitted edit, and a new commit that moves HEAD.
        (repo / "beta.py").write_text("def beta_one(items):\n    raise RuntimeError\n")
        (repo / "gamma.py").write_text("def gamma(value):\n    return 'changed'\n")
        subprocess.run(["git", "add", "gamma.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "mid-window"], cwd=repo, check=True)

    ctx["summarizer"].on_call = edit_checkout

    result, payload = _summarize(ctx)

    assert result.exit_code == 0, result.output
    assert payload["status"] == "complete"
    assert set(_summaries(ctx)) == chunks
    assert len(ctx["summarizer"].calls) >= 3
    for name in ("alpha.py", "beta.py", "gamma.py"):
        assert seen[name] == _SOURCES[name], name


def test_unknown_chunk_ids_are_not_counted_as_written(prefill):
    ctx = prefill
    chunks = _chunk_ids(ctx)
    ctx["summarizer"].answer = lambda ids: [f"{cid}-hallucinated" for cid in ids]

    result, payload = _summarize(ctx)

    assert result.exit_code == 1, result.output
    assert payload["status"] == "stalled"
    assert payload["stop_reason"] == "no_progress"
    assert payload["written"] == 0
    assert payload["remaining"] == len(chunks)
    assert _summaries(ctx) == {}

    limited, limited_payload = _summarize(ctx, "--limit", "2")
    assert limited.exit_code == 1, limited.output
    assert limited_payload["status"] == "stalled"
    assert limited_payload["written"] == 0
    assert _summaries(ctx) == {}


def test_written_counts_only_stored_summaries(prefill):
    ctx = prefill
    chunks = _chunk_ids(ctx)
    # Each call answers its first chunk plus one unknown ID.
    ctx["summarizer"].answer = lambda ids: [ids[0], "not-a-chunk"]

    result, payload = _summarize(ctx)

    assert result.exit_code == 0, result.output
    assert payload["status"] == "complete"
    assert payload["written"] == len(chunks)
    assert payload["passes"] > 1
    assert set(_summaries(ctx)) == chunks


def test_republished_index_aborts_between_passes(prefill):
    ctx = prefill
    chunks = _chunk_ids(ctx)
    ctx["summarizer"].answer = lambda ids: ids[:1]

    def republish(n):
        if n == 1:
            ctx["registry"].update_indexed_commit(ctx["repo_id"], "f" * 40)

    ctx["summarizer"].on_call = republish

    result, payload = _summarize(ctx)

    assert result.exit_code == 1, result.output
    assert payload["status"] == "aborted"
    assert payload["stop_reason"] == "published_index_changed"
    # update_indexed_commit also mints a new index generation.
    assert payload["detail"] == "the published generation changed"
    assert payload["passes"] == 1
    stored = _summaries(ctx)
    assert 0 < len(stored) == payload["written"] < len(chunks)


def test_interrupt_while_storing_keeps_no_part_of_the_batch(prefill):
    _requires_main_thread()
    ctx = prefill
    from mcp_server.storage.sqlite_store import SQLiteStore

    original = SQLiteStore.store_chunk_summary
    stores = []

    def store_then_interrupt(self, *args, **kwargs):
        stored = original(self, *args, **kwargs)
        stores.append(args[0] if args else kwargs.get("chunk_hash"))
        if len(stores) == 1:
            os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGINT)
        return stored

    ctx["monkeypatch"].setattr(SQLiteStore, "store_chunk_summary", store_then_interrupt)

    result, payload = _summarize(ctx)

    assert result.exit_code == 130, result.output
    assert payload["status"] == "interrupted"
    assert len(stores) == 1  # one row was stored inside the batch transaction
    assert len(ctx["summarizer"].calls[0]) > 1
    assert payload["written"] == 0
    assert _summaries(ctx) == {}


def test_unregister_during_the_final_pass_aborts_instead_of_completing(prefill):
    ctx = prefill
    chunks = _chunk_ids(ctx)

    def unregister(n):
        if n == 1:
            assert ctx["registry"].unregister_repository(ctx["repo_id"])

    ctx["summarizer"].on_call = unregister

    result, payload = _summarize(ctx)

    # The single pass drained every chunk, but the repository went away.
    assert result.exit_code == 1, result.output
    assert payload["status"] == "aborted"
    assert payload["stop_reason"] == "published_index_changed"
    assert payload["detail"] == "the repository was unregistered"
    assert payload["remaining"] == 0
    assert payload["written"] == len(chunks)


def test_reregistration_during_a_limit_pass_aborts_instead_of_partial(prefill):
    ctx = prefill

    def reregister(n):
        if n == 1:
            assert ctx["registry"].unregister_repository(ctx["repo_id"])
            assert ctx["registry"].register_repository(str(ctx["repo"])) == ctx["repo_id"]

    ctx["summarizer"].on_call = reregister

    result, payload = _summarize(ctx, "--limit", "2")

    assert result.exit_code == 1, result.output
    assert payload["status"] == "aborted"
    assert payload["detail"] == "the repository registration changed"
    assert payload["written"] == 2


def test_zero_pass_window_still_rechecks_the_registry(prefill, monkeypatch):
    ctx = prefill
    calls = []

    def changed(*_args, **_kwargs):
        calls.append(1)
        return "the repository registration changed"

    # The window closes before the first pass, so no pass recheck runs; the
    # command must still check the registry before reporting success.
    monkeypatch.setattr(repository_commands, "_published_index_change", changed)
    # A window this short has always closed by the time the loop first checks it.
    result, payload = _summarize(ctx, "--max-seconds", "0.000000001")

    assert result.exit_code == 1, result.output
    assert payload["status"] == "aborted"
    assert payload["detail"] == "the repository registration changed"
    assert calls
    assert ctx["summarizer"].calls == []
