"""Tests for mcp_server/indexing/summarization.py — ChunkWriter and LazyChunkWriter."""

import asyncio
import importlib
import json
import re
import socket
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_server.config.settings import get_settings
from mcp_server.indexing.summarization import (
    ChunkWriter,
    ComprehensiveChunkWriter,
    FileBatchSummarizer,
    GeneratedSummary,
    LazyChunkWriter,
    SummaryGenerationResult,
)
from mcp_server.setup.semantic_preflight import EnrichmentModelResolution
from mcp_server.storage.sqlite_store import SQLiteStore


@pytest.fixture(autouse=True)
def _deny_network_connections(monkeypatch):
    def deny_connect(*_args, **_kwargs):
        raise AssertionError("unit summarization tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", deny_connect)


def _seed_chunk_summary_tables(db_path: Path, tmp_path: Path) -> tuple[SQLiteStore, int]:
    store = SQLiteStore(str(db_path))
    repo_id = store.ensure_repository_row(tmp_path)
    source_file = tmp_path / "sample.py"
    source_file.write_text("def alpha():\n    return 1\n", encoding="utf-8")
    file_id = store.store_file(
        repo_id,
        path=source_file,
        relative_path="sample.py",
        language="python",
        size=source_file.stat().st_size,
        content_hash="content-hash-1",
    )
    return store, file_id


def _store_chunk(
    store: SQLiteStore,
    *,
    file_id: int,
    chunk_id: str,
    content: str,
    line_start: int,
    line_end: int,
    symbol: str | None = None,
) -> None:
    symbol_id = None
    if symbol is not None:
        symbol_id = store.store_symbol(
            file_id=file_id,
            name=symbol,
            kind="function",
            line_start=line_start,
            line_end=line_end,
        )
    store.store_chunk(
        file_id=file_id,
        content=content,
        content_start=0,
        content_end=len(content),
        line_start=line_start,
        line_end=line_end,
        chunk_id=chunk_id,
        node_id=f"node-{chunk_id}",
        treesitter_file_id=f"tree-{chunk_id}",
        symbol_id=symbol_id,
        language="python",
        node_type="function_definition",
    )


def test_summary_generation_result_normalizes_immutable_items_and_ids():
    source_items = [SimpleNamespace(chunk_id="chunk-1", summary="Summary text")]
    source_missing = ["chunk-2"]
    result = SummaryGenerationResult(
        chunks_attempted=2,
        summaries_written=1,
        missing_chunk_ids=source_missing,
        summaries=source_items,
    )

    source_items.append(SimpleNamespace(chunk_id="chunk-2", summary="Late mutation"))
    source_missing.append("chunk-3")

    assert result.summaries == (GeneratedSummary("chunk-1", "Summary text"),)
    assert result.missing_chunk_ids == ("chunk-2",)
    assert result.to_dict()["summaries"] == [{"chunk_id": "chunk-1", "summary": "Summary text"}]
    with pytest.raises(TypeError):
        iter(result)


class TestChunkWriterInit:
    def test_module_imports_with_default_install_client_dependency(self):
        module = importlib.import_module("mcp_server.indexing.summarization")
        assert hasattr(module, "ChunkWriter")

    def test_defaults_to_empty_summarization_config(self):
        cw = ChunkWriter(db_path="/tmp/test.db", qdrant_client=None)
        assert cw.summarization_config == {}

    def test_stores_provided_summarization_config(self):
        cfg = {"base_url": "http://localhost:8080", "model_name": "my-model"}
        cw = ChunkWriter(db_path="/tmp/test.db", qdrant_client=None, summarization_config=cfg)
        assert cw.summarization_config == cfg

    def test_stores_db_path_and_qdrant_client(self):
        cw = ChunkWriter(db_path="/data/code.db", qdrant_client="fake-client")
        assert cw.db_path == "/data/code.db"
        assert cw.qdrant_client == "fake-client"

    def test_has_direct_api_true_when_base_url_set(self):
        cw = ChunkWriter(
            db_path="/tmp/test.db",
            qdrant_client=None,
            summarization_config={"base_url": "http://localhost:8080"},
        )
        assert cw._has_direct_api() is True

    def test_has_direct_api_true_when_anthropic_api_key_set(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cw = ChunkWriter(db_path="/tmp/test.db", qdrant_client=None)
        assert cw._has_direct_api() is True

    def test_has_direct_api_true_when_openai_api_key_set(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        cw = ChunkWriter(db_path="/tmp/test.db", qdrant_client=None)
        assert cw._has_direct_api() is True

    def test_has_direct_api_false_when_no_keys_and_no_base_url(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cw = ChunkWriter(db_path="/tmp/test.db", qdrant_client=None)
        assert cw._has_direct_api() is False

    @pytest.mark.asyncio
    async def test_profile_api_closes_async_openai_client(self, monkeypatch):
        closed = {"value": False}

        class FakeCompletions:
            async def create(self, **_kwargs):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="summary text"))]
                )

        class FakeChat:
            def __init__(self):
                self.completions = FakeCompletions()

        class FakeAsyncOpenAI:
            def __init__(self, **_kwargs):
                self.chat = FakeChat()

            async def close(self):
                closed["value"] = True

        monkeypatch.setenv("OPENAI_API_KEY", "dummy-local-key")
        monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)

        writer = ChunkWriter(
            db_path="/tmp/test.db",
            qdrant_client=None,
            summarization_config={"base_url": "http://ai:8002/v1", "model_name": "chat"},
        )
        monkeypatch.setattr(
            writer,
            "_resolve_effective_profile_model",
            lambda: EnrichmentModelResolution(
                configured_model="chat",
                effective_model="chat",
                resolution_strategy="configured",
            ),
        )

        summary_text, model_name = await writer._call_profile_api("system", "prompt")

        assert summary_text == "summary text"
        assert model_name == "chat"
        assert closed["value"] is True

    def test_build_api_file_context_uses_smaller_budget_for_doc_like_languages(self):
        writer = ChunkWriter(db_path="/tmp/test.db", qdrant_client=None)
        file_content = ("0123456789" * 900)[:9000]

        markdown_context = writer._build_api_file_context(
            language="markdown",
            file_content=file_content,
            chunk_content="small chunk",
        )
        python_context = writer._build_api_file_context(
            language="python",
            file_content=file_content,
            chunk_content="small chunk",
        )

        assert "... [file truncated for context window]" in markdown_context
        assert file_content[:8000] in markdown_context
        assert len(markdown_context) < len(python_context)
        assert "... [file truncated for context window]" not in python_context

    def test_build_api_file_context_omits_full_file_when_chunk_is_already_large(self):
        writer = ChunkWriter(db_path="/tmp/test.db", qdrant_client=None)

        context = writer._build_api_file_context(
            language="markdown",
            file_content="a" * 9000,
            chunk_content="b" * 5000,
        )

        assert context == ""

    def test_has_sampling_capability_false_when_session_is_none(self):
        cw = ChunkWriter(db_path="/tmp/test.db", qdrant_client=None)
        assert cw._has_sampling_capability() is False

    def test_can_summarize_false_when_no_api_and_no_session(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cw = ChunkWriter(db_path="/tmp/test.db", qdrant_client=None)
        assert cw.can_summarize() is False

    def test_can_summarize_true_when_base_url_in_config(self):
        cw = ChunkWriter(
            db_path="/tmp/test.db",
            qdrant_client=None,
            summarization_config={"base_url": "http://vllm:8000"},
        )
        assert cw.can_summarize() is True


@pytest.mark.asyncio
async def test_repo_scope_timeout_returns_prompt_blocker_even_if_summary_call_ignores_cancel(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "code.db"
    store, file_id = _seed_chunk_summary_tables(db_path, tmp_path)
    _store_chunk(
        store,
        file_id=file_id,
        chunk_id="chunk-1",
        content="def beta():\n    return 2\n",
        line_start=1,
        line_end=2,
        symbol="beta",
    )

    lingered = asyncio.Event()

    async def _late_call(*_args, **_kwargs):
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)
            lingered.set()
            return [SimpleNamespace(chunk_id="chunk-1", summary="late summary")]
        return [SimpleNamespace(chunk_id="chunk-1", summary="on-time summary")]

    summarizer = FileBatchSummarizer(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={"base_url": "http://ai:8002/v1", "timeout_s": 0.05},
    )
    monkeypatch.setattr(summarizer, "_call_profile_batch_api", _late_call)

    started = time.perf_counter()
    result = await summarizer.summarize_file_chunks(
        file_id=file_id,
        file_path=str(tmp_path / "sample.py"),
        file_content="def beta():\n    return 2\n",
        chunks=[
            {
                "chunk_id": "chunk-1",
                "file_id": file_id,
                "line_start": 1,
                "line_end": 2,
                "content": "def beta():\n    return 2\n",
                "node_type": "function_definition",
                "parent_chunk_id": None,
                "language": "python",
                "symbol_id": None,
            }
        ],
        call_timeout_seconds=0.05,
    )
    elapsed = time.perf_counter() - started

    assert result.blocked_call_reason == "timeout"
    assert elapsed < 0.4
    await asyncio.sleep(0.3)
    assert lingered.is_set() is True
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM chunk_summaries").fetchone()
    assert int(row[0]) == 0


class TestLazyChunkWriterInit:
    def test_constructor_without_summarization_config_defaults_to_empty(self):
        lw = LazyChunkWriter(db_path="/tmp/test.db", qdrant_client=None)
        assert lw.summarization_config == {}

    def test_constructor_accepts_summarization_config(self):
        cfg = {"base_url": "http://vllm:8000", "model_name": "qwen"}
        lw = LazyChunkWriter(db_path="/tmp/test.db", qdrant_client=None, summarization_config=cfg)
        assert lw.summarization_config == cfg

    def test_inherits_chunk_writer(self):
        lw = LazyChunkWriter(db_path="/tmp/test.db", qdrant_client=None)
        assert isinstance(lw, ChunkWriter)

    def test_queue_initialized_as_asyncio_queue(self):
        lw = LazyChunkWriter(db_path="/tmp/test.db", qdrant_client=None)
        assert isinstance(lw.queue, asyncio.Queue)

    def test_task_starts_as_none(self):
        lw = LazyChunkWriter(db_path="/tmp/test.db", qdrant_client=None)
        assert lw._task is None

    def test_update_session_replaces_session_attribute(self):
        lw = LazyChunkWriter(db_path="/tmp/test.db", qdrant_client=None)
        mock_session = object()
        lw.update_session(mock_session)
        assert lw.session is mock_session

    def test_enqueue_puts_item_in_queue(self):
        lw = LazyChunkWriter(db_path="/tmp/test.db", qdrant_client=None)
        chunk = {"chunk_id": "foo.py:1", "content": "x = 1", "language": "python"}
        lw.enqueue(chunk)
        assert lw.queue.qsize() == 1

    def test_enqueue_multiple_items(self):
        lw = LazyChunkWriter(db_path="/tmp/test.db", qdrant_client=None)
        for i in range(5):
            lw.enqueue({"chunk_id": f"foo.py:{i}"})
        assert lw.queue.qsize() == 5

    def test_summarization_config_passed_through_to_has_direct_api(self):
        lw = LazyChunkWriter(
            db_path="/tmp/test.db",
            qdrant_client=None,
            summarization_config={"base_url": "http://localhost:11434"},
        )
        assert lw._has_direct_api() is True


@pytest.mark.asyncio
async def test_file_batch_summarizer_persists_authoritative_audit_metadata(tmp_path):
    db_path = tmp_path / "summaries.db"
    store, file_id = _seed_chunk_summary_tables(db_path, tmp_path)
    summarizer = FileBatchSummarizer(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={
            "base_url": "http://ai:8002/v1",
            "model_name": "chat",
            "profile_id": "oss_high",
        },
    )

    async def _fake_batch_api(*_args, **_kwargs):
        return [SimpleNamespace(chunk_id="chunk-1", summary="Semantic summary")]

    summarizer._call_profile_batch_api = _fake_batch_api  # type: ignore[method-assign]
    summarizer._profile_model_resolution = EnrichmentModelResolution(
        configured_model="chat",
        effective_model="cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit",
        resolution_strategy="single_served_model_for_chat_alias",
        available_models=["cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"],
        models_probe_verified=True,
    )
    result = await summarizer.summarize_file_chunks(
        file_id=file_id,
        file_path=str(tmp_path / "sample.py"),
        file_content="def alpha():\n    return 1\n",
        chunks=[
            {
                "chunk_id": "chunk-1",
                "line_start": 1,
                "line_end": 2,
                "node_type": "function_definition",
                "symbol_id": None,
            }
        ],
        symbol_map={},
        persist=True,
    )

    stored = store.get_chunk_summary("chunk-1")
    assert result.summaries_written == 1
    assert result.missing_chunk_ids == ()
    assert stored is not None
    assert stored["is_authoritative"] is True
    assert stored["provider_name"] == "openai_compatible"
    assert stored["profile_id"] == "oss_high"
    assert stored["prompt_fingerprint"]
    assert stored["audit_metadata"]["llm_model"] == "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"
    assert stored["audit_metadata"]["configured_model_name"] == "chat"
    assert (
        stored["audit_metadata"]["effective_model_name"] == "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"
    )
    assert (
        stored["audit_metadata"]["model_resolution_strategy"]
        == "single_served_model_for_chat_alias"
    )


@pytest.mark.asyncio
async def test_file_batch_summarizer_reports_missing_chunks_without_claiming_success(tmp_path):
    db_path = tmp_path / "summaries.db"
    _, file_id = _seed_chunk_summary_tables(db_path, tmp_path)
    summarizer = FileBatchSummarizer(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={"base_url": "http://ai:8002/v1", "model_name": "chat"},
    )

    async def _fake_batch_api(*_args, **_kwargs):
        return [SimpleNamespace(chunk_id="chunk-1", summary="Only one summary")]

    summarizer._call_profile_batch_api = _fake_batch_api  # type: ignore[method-assign]
    summarizer._profile_model_resolution = EnrichmentModelResolution(
        configured_model="chat",
        effective_model="fake-model",
        resolution_strategy="injected_test_model",
    )
    result = await summarizer.summarize_file_chunks(
        file_id=file_id,
        file_path=str(tmp_path / "sample.py"),
        file_content="def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
        chunks=[
            {"chunk_id": "chunk-1", "line_start": 1, "line_end": 2, "node_type": "function"},
            {"chunk_id": "chunk-2", "line_start": 4, "line_end": 5, "node_type": "function"},
        ],
        symbol_map={},
        persist=True,
    )

    assert result.summaries_written == 1
    assert result.missing_chunk_ids == ("chunk-2",)


@pytest.mark.asyncio
async def test_file_batch_summarizer_uses_profile_batch_recovery_before_topological_fallback_for_large_files(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "summaries.db"
    store, file_id = _seed_chunk_summary_tables(db_path, tmp_path)
    summarizer = FileBatchSummarizer(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={"base_url": "http://ai:8002/v1", "model_name": "chat"},
    )
    topological_called = {"value": False}
    profile_attempts = {"count": 0}
    profile_batches: list[list[str]] = []

    async def _raise_large(*_args, **_kwargs):
        raise importlib.import_module("mcp_server.indexing.summarization").FileTooLargeError("big")

    async def _fake_profile_batch(file_id, file_path, file_content, chunks, symbol_map):
        del file_id, file_path, file_content, symbol_map
        profile_attempts["count"] += 1
        if profile_attempts["count"] == 1:
            raise importlib.import_module("mcp_server.indexing.summarization").FileTooLargeError(
                "big"
            )
        profile_batches.append([chunk["chunk_id"] for chunk in chunks])
        return [
            SimpleNamespace(chunk_id=chunk["chunk_id"], summary=f"summary for {chunk['chunk_id']}")
            for chunk in chunks
        ]

    async def _should_not_run(*_args, **_kwargs):
        topological_called["value"] = True
        raise AssertionError(
            "topological fallback should not run when bounded profile recovery succeeds"
        )

    monkeypatch.setenv("OPENAI_API_KEY", "dummy-local-key")
    summarizer._call_batch_api = _raise_large  # type: ignore[method-assign]
    summarizer._call_profile_batch_api = _fake_profile_batch  # type: ignore[method-assign]
    summarizer._summarize_topological = _should_not_run  # type: ignore[method-assign]
    summarizer._profile_model_resolution = EnrichmentModelResolution(
        configured_model="chat",
        effective_model="fake-model",
        resolution_strategy="injected_test_model",
    )
    result = await summarizer.summarize_file_chunks(
        file_id=file_id,
        file_path=str(tmp_path / "sample.py"),
        file_content="x"
        * (
            importlib.import_module("mcp_server.indexing.summarization")._BATCH_FILE_SIZE_THRESHOLD
            + 1
        ),
        chunks=[
            {
                "chunk_id": f"chunk-{idx}",
                "line_start": idx,
                "line_end": idx,
                "node_type": "function",
                "language": "markdown",
            }
            for idx in range(1, 67)
        ],
        symbol_map={},
        persist=True,
    )

    assert topological_called["value"] is False
    assert profile_batches == [[f"chunk-{idx}" for idx in range(1, 67)]]
    assert result.summaries_written == 66
    assert result.missing_chunk_ids == ()
    stored = store.get_chunk_summary("chunk-1")
    assert stored is not None
    assert stored["is_authoritative"] is True
    assert stored["provider_name"] == "openai_compatible"


@pytest.mark.asyncio
async def test_file_batch_summarizer_tracks_missing_chunks_after_large_file_profile_batch_recovery(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "summaries.db"
    store, file_id = _seed_chunk_summary_tables(db_path, tmp_path)
    summarizer = FileBatchSummarizer(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={
            "base_url": "http://ai:8002/v1",
            "model_name": "chat",
            "profile_id": "oss_high",
        },
    )
    summarizer._profile_model_resolution = EnrichmentModelResolution(
        configured_model="chat",
        effective_model="cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit",
        resolution_strategy="single_served_model_for_chat_alias",
        available_models=["cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"],
        models_probe_verified=True,
    )

    async def _raise_large(*_args, **_kwargs):
        raise importlib.import_module("mcp_server.indexing.summarization").FileTooLargeError("big")

    profile_api_calls: list[list[str]] = []
    chunk_ids = [f"chunk-{idx}" for idx in range(1, 66 + 1)]

    async def _fake_profile_api(_system: str, prompt: str, **_kwargs) -> tuple[str, str]:
        prompt_chunk_ids = re.findall(r"chunk_id: ([^\n]+)", prompt)
        profile_api_calls.append(prompt_chunk_ids)
        if len(profile_api_calls) == 1:
            returned_ids = prompt_chunk_ids[:64]
        else:
            returned_ids = prompt_chunk_ids[:1]
        payload = {
            "summaries": [
                {"chunk_id": chunk_id, "summary": f"Summary for {chunk_id}. Still concise."}
                for chunk_id in returned_ids
            ]
        }
        return json.dumps(payload), "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"

    async def _should_not_run(*_args, **_kwargs):
        raise AssertionError(
            "topological fallback should not run when large-file profile batches succeed"
        )

    monkeypatch.setenv("OPENAI_API_KEY", "dummy-local-key")
    summarizer._call_batch_api = _raise_large  # type: ignore[method-assign]
    summarizer._call_profile_api = _fake_profile_api  # type: ignore[method-assign]
    summarizer._summarize_topological = _should_not_run  # type: ignore[method-assign]
    result = await summarizer.summarize_file_chunks(
        file_id=file_id,
        file_path=str(tmp_path / "sample.md"),
        file_content="x"
        * (
            importlib.import_module("mcp_server.indexing.summarization")._BATCH_FILE_SIZE_THRESHOLD
            + 1
        ),
        chunks=[
            {
                "chunk_id": chunk_id,
                "line_start": idx,
                "line_end": idx,
                "node_type": "paragraph",
                "language": "markdown",
            }
            for idx, chunk_id in enumerate(chunk_ids, start=1)
        ],
        symbol_map={},
        persist=True,
    )

    assert profile_api_calls[0] == chunk_ids[:64]
    assert profile_api_calls[1] == chunk_ids[64:]
    assert result.summaries_written == 65
    assert result.missing_chunk_ids == ("chunk-66",)
    stored = store.get_chunk_summary("chunk-1")
    assert stored is not None
    assert stored["is_authoritative"] is True
    assert stored["provider_name"] == "openai_compatible"
    assert stored["profile_id"] == "oss_high"
    assert stored["audit_metadata"]["configured_model_name"] == "chat"
    assert (
        stored["audit_metadata"]["effective_model_name"] == "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"
    )


@pytest.mark.asyncio
async def test_file_batch_summarizer_falls_back_to_topological_path_when_large_file_profile_recovery_fails(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "summaries.db"
    _, file_id = _seed_chunk_summary_tables(db_path, tmp_path)
    summarizer = FileBatchSummarizer(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={"base_url": "http://ai:8002/v1", "model_name": "chat"},
    )
    topological_called = {"value": False}
    profile_attempts = {"count": 0}

    async def _raise_large(*_args, **_kwargs):
        raise importlib.import_module("mcp_server.indexing.summarization").FileTooLargeError("big")

    async def _raise_profile(*_args, **_kwargs):
        profile_attempts["count"] += 1
        raise RuntimeError("profile batch failed")

    async def _fake_topological(*args, **kwargs):
        del args, kwargs
        topological_called["value"] = True
        return (
            GeneratedSummary("chunk-1", "Summary one"),
            GeneratedSummary("chunk-2", "Summary two"),
        )

    monkeypatch.setenv("OPENAI_API_KEY", "dummy-local-key")
    summarizer._call_batch_api = _raise_large  # type: ignore[method-assign]
    summarizer._call_profile_batch_api = _raise_profile  # type: ignore[method-assign]
    summarizer._summarize_topological = _fake_topological  # type: ignore[method-assign]
    result = await summarizer.summarize_file_chunks(
        file_id=file_id,
        file_path=str(tmp_path / "sample.py"),
        file_content="x"
        * (
            importlib.import_module("mcp_server.indexing.summarization")._BATCH_FILE_SIZE_THRESHOLD
            + 1
        ),
        chunks=[
            {"chunk_id": "chunk-1", "line_start": 1, "line_end": 2, "node_type": "function"},
            {"chunk_id": "chunk-2", "line_start": 3, "line_end": 4, "node_type": "function"},
        ],
        symbol_map={},
        persist=False,
    )

    assert topological_called["value"] is True
    assert profile_attempts["count"] == 1
    assert result.summaries_written == 2


@pytest.mark.asyncio
async def test_file_batch_summarizer_preserves_authoritative_metadata_on_batch_runtime_fallback(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "summaries.db"
    store, file_id = _seed_chunk_summary_tables(db_path, tmp_path)
    profile_id = get_settings().get_semantic_default_profile()
    summarizer = FileBatchSummarizer(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={
            **get_settings().get_profile_summarization_config(profile_id),
            "profile_id": profile_id,
        },
    )
    summarizer._profile_model_resolution = EnrichmentModelResolution(
        configured_model="chat",
        effective_model="cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit",
        resolution_strategy="single_served_model_for_chat_alias",
        available_models=["cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"],
        models_probe_verified=True,
    )

    async def _raise_runtime_mismatch(*_args, **_kwargs):
        raise ImportError("generated client 0.220.0 is incompatible with baml-py 0.221.0")

    async def _fake_profile_api(_system: str, _prompt: str, **_kwargs) -> tuple[str, str]:
        return "Recovered authoritative summary", "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"

    monkeypatch.setenv("OPENAI_API_KEY", "dummy-local-key")
    summarizer._call_batch_api = _raise_runtime_mismatch  # type: ignore[method-assign]
    summarizer._call_profile_api = _fake_profile_api  # type: ignore[method-assign]
    result = await summarizer.summarize_file_chunks(
        file_id=file_id,
        file_path=str(tmp_path / "sample.py"),
        file_content="def alpha():\n    return 1\n",
        chunks=[
            {
                "chunk_id": "chunk-1",
                "line_start": 1,
                "line_end": 2,
                "node_type": "function_definition",
                "language": "python",
                "symbol_id": None,
            }
        ],
        symbol_map={},
        persist=True,
    )

    stored = store.get_chunk_summary("chunk-1")
    assert result.summaries_written == 1
    assert result.authoritative_chunks == 1
    assert result.missing_chunk_ids == ()
    assert stored is not None
    assert stored["summary_text"] == "Recovered authoritative summary"
    assert stored["is_authoritative"] is True
    assert stored["provider_name"] == "openai_compatible"
    assert stored["profile_id"] == profile_id
    assert stored["audit_metadata"]["llm_model"] == "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"
    assert stored["audit_metadata"]["configured_model_name"] == "chat"
    assert (
        stored["audit_metadata"]["effective_model_name"] == "cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"
    )


@pytest.mark.asyncio
async def test_file_batch_summarizer_prefers_profile_batch_path_when_base_url_is_configured(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "summaries.db"
    store, file_id = _seed_chunk_summary_tables(db_path, tmp_path)
    profile_id = get_settings().get_semantic_default_profile()
    summarizer = FileBatchSummarizer(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={
            **get_settings().get_profile_summarization_config(profile_id),
            "profile_id": profile_id,
        },
    )
    summarizer._profile_model_resolution = EnrichmentModelResolution(
        configured_model="chat",
        effective_model="cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit",
        resolution_strategy="single_served_model_for_chat_alias",
        available_models=["cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit"],
        models_probe_verified=True,
    )

    async def _should_not_call_baml_batch(*_args, **_kwargs):
        raise AssertionError("configured profile batch path should bypass the BAML batch path")

    async def _fake_profile_batch(*_args, **_kwargs):
        return [SimpleNamespace(chunk_id="chunk-1", summary="Recovered authoritative summary")]

    monkeypatch.setenv("OPENAI_API_KEY", "dummy-local-key")
    summarizer._call_batch_api = _should_not_call_baml_batch  # type: ignore[method-assign]
    summarizer._call_profile_batch_api = _fake_profile_batch  # type: ignore[method-assign]
    result = await summarizer.summarize_file_chunks(
        file_id=file_id,
        file_path=str(tmp_path / "sample.py"),
        file_content="def alpha():\n    return 1\n",
        chunks=[
            {
                "chunk_id": "chunk-1",
                "line_start": 1,
                "line_end": 2,
                "node_type": "function_definition",
                "language": "python",
                "symbol_id": None,
            }
        ],
        symbol_map={},
        persist=True,
    )

    stored = store.get_chunk_summary("chunk-1")
    assert result.summaries_written == 1
    assert result.authoritative_chunks == 1
    assert result.missing_chunk_ids == ()
    assert stored is not None
    assert stored["summary_text"] == "Recovered authoritative summary"
    assert stored["provider_name"] == "openai_compatible"
    assert stored["profile_id"] == profile_id


@pytest.mark.asyncio
async def test_file_batch_summarizer_uses_profile_batch_fallback_before_per_chunk_recovery(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "summaries.db"
    store, file_id = _seed_chunk_summary_tables(db_path, tmp_path)
    summarizer = FileBatchSummarizer(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={
            "base_url": "http://ai:8002/v1",
            "model_name": "chat",
            "profile_id": "oss_high",
        },
    )

    async def _raise_runtime_mismatch(*_args, **_kwargs):
        raise ImportError("generated client 0.220.0 is incompatible with baml-py 0.221.0")

    async def _fake_profile_batch(*_args, **_kwargs):
        return [
            GeneratedSummary("chunk-1", "Summary one. Still concise."),
            GeneratedSummary("chunk-2", "Summary two. Still concise."),
        ]

    async def _should_not_run(*_args, **_kwargs):
        raise AssertionError(
            "per-chunk fallback should not run when profile batch fallback succeeds"
        )

    monkeypatch.setenv("OPENAI_API_KEY", "dummy-local-key")
    summarizer._call_batch_api = _raise_runtime_mismatch  # type: ignore[method-assign]
    summarizer._call_profile_batch_api = _fake_profile_batch  # type: ignore[method-assign]
    summarizer._summarize_topological = _should_not_run  # type: ignore[method-assign]
    summarizer._profile_model_resolution = EnrichmentModelResolution(
        configured_model="chat",
        effective_model="fake-model",
        resolution_strategy="injected_test_model",
    )

    result = await summarizer.summarize_file_chunks(
        file_id=file_id,
        file_path=str(tmp_path / "sample.py"),
        file_content="def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
        chunks=[
            {
                "chunk_id": "chunk-1",
                "line_start": 1,
                "line_end": 2,
                "node_type": "function_definition",
                "language": "python",
                "symbol_id": None,
            },
            {
                "chunk_id": "chunk-2",
                "line_start": 4,
                "line_end": 5,
                "node_type": "function_definition",
                "language": "python",
                "symbol_id": None,
            },
        ],
        symbol_map={},
        persist=True,
    )

    stored_one = store.get_chunk_summary("chunk-1")
    stored_two = store.get_chunk_summary("chunk-2")
    assert result.summaries_written == 2
    assert result.missing_chunk_ids == ()
    assert stored_one is not None
    assert stored_two is not None
    assert stored_one["summary_text"] == "Summary one. Still concise."
    assert stored_two["summary_text"] == "Summary two. Still concise."


def test_default_oss_high_profile_exposes_direct_summarization_config():
    settings = get_settings()
    profile_id = settings.get_semantic_default_profile()
    config = settings.get_profile_summarization_config(profile_id)

    assert profile_id == "oss_high"
    assert config["base_url"] == "http://ai:8002/v1"
    assert config["model_name"] == "chat"


def test_fetch_unsummarized_rows_filters_scope_before_limit(tmp_path):
    db_path = tmp_path / "summaries.db"
    store = SQLiteStore(str(db_path))
    repo_id = store.ensure_repository_row(tmp_path)

    off_scope = tmp_path / "off_scope.py"
    off_scope.write_text("def off_scope():\n    return 0\n", encoding="utf-8")
    off_scope_file_id = store.store_file(
        repo_id,
        path=off_scope,
        relative_path="off_scope.py",
        language="python",
        size=off_scope.stat().st_size,
        content_hash="off-scope-hash",
    )
    _store_chunk(
        store,
        file_id=off_scope_file_id,
        chunk_id="chunk-off-scope",
        content="def off_scope():\n    return 0\n",
        line_start=1,
        line_end=2,
        symbol="off_scope",
    )

    in_scope = tmp_path / "in_scope.py"
    in_scope.write_text("def in_scope():\n    return 1\n", encoding="utf-8")
    in_scope_file_id = store.store_file(
        repo_id,
        path=in_scope,
        relative_path="in_scope.py",
        language="python",
        size=in_scope.stat().st_size,
        content_hash="in-scope-hash",
    )
    _store_chunk(
        store,
        file_id=in_scope_file_id,
        chunk_id="chunk-in-scope",
        content="def in_scope():\n    return 1\n",
        line_start=1,
        line_end=2,
        symbol="in_scope",
    )

    writer = ComprehensiveChunkWriter(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={"base_url": "http://ai:8002/v1", "model_name": "chat"},
    )
    rows = writer._fetch_unsummarized_rows(limit=1, target_paths=[in_scope])

    assert len(rows) == 1
    assert rows[0][0] == "chunk-in-scope"
    assert rows[0][9] == str(in_scope)


@pytest.mark.asyncio
async def test_process_scope_drains_multiple_batches_for_target_scope(tmp_path, monkeypatch):
    db_path = tmp_path / "summaries.db"
    store = SQLiteStore(str(db_path))
    repo_id = store.ensure_repository_row(tmp_path)

    target = tmp_path / "target.py"
    target.write_text(
        "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
        encoding="utf-8",
    )
    file_id = store.store_file(
        repo_id,
        path=target,
        relative_path="target.py",
        language="python",
        size=target.stat().st_size,
        content_hash="target-hash",
    )
    _store_chunk(
        store,
        file_id=file_id,
        chunk_id="chunk-1",
        content="def alpha():\n    return 1\n",
        line_start=1,
        line_end=2,
        symbol="alpha",
    )
    _store_chunk(
        store,
        file_id=file_id,
        chunk_id="chunk-2",
        content="def beta():\n    return 2\n",
        line_start=4,
        line_end=5,
        symbol="beta",
    )

    writer = ComprehensiveChunkWriter(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={"base_url": "http://ai:8002/v1", "model_name": "chat"},
    )
    batch_order: list[list[str]] = []

    async def _fake_summarize_file_chunks(**kwargs):
        chunk_ids = [chunk["chunk_id"] for chunk in kwargs["chunks"]]
        batch_order.append(chunk_ids)
        for chunk_id in chunk_ids:
            store.store_chunk_summary(
                chunk_hash=chunk_id,
                file_id=file_id,
                chunk_start=1,
                chunk_end=2,
                summary_text=f"summary for {chunk_id}",
                llm_model="chat",
                is_authoritative=True,
                symbol=None,
                provider_name="openai_compatible",
                profile_id="oss_high",
                prompt_fingerprint="test-fingerprint",
                audit_metadata={"provider_name": "openai_compatible"},
            )
        return SummaryGenerationResult(
            chunks_attempted=len(chunk_ids),
            summaries_written=len(chunk_ids),
            authoritative_chunks=len(chunk_ids),
            missing_chunk_ids=[],
            files_attempted=1,
            files_summarized=1,
        )

    monkeypatch.setattr(writer, "summarize_file_chunks", _fake_summarize_file_chunks)

    result = await writer.process_scope(limit=1, target_paths=[target])

    assert batch_order == [["chunk-1"], ["chunk-2"]]
    assert result.summaries_written == 2
    assert result.authoritative_chunks == 2
    assert result.chunks_attempted == 2
    assert result.batches_processed == 2
    assert result.remaining_chunks == 0
    assert result.scope_drained is True
    assert result.missing_chunk_ids == ()


@pytest.mark.asyncio
async def test_process_scope_retries_large_file_profile_recovery_until_backlog_is_drained(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "summaries.db"
    store = SQLiteStore(str(db_path))
    repo_id = store.ensure_repository_row(tmp_path)

    target = tmp_path / "target.md"
    target.write_text("x" * 32, encoding="utf-8")
    file_id = store.store_file(
        repo_id,
        path=target,
        relative_path="target.md",
        language="markdown",
        size=target.stat().st_size,
        content_hash="target-hash",
    )
    for idx in range(1, 4):
        _store_chunk(
            store,
            file_id=file_id,
            chunk_id=f"chunk-{idx}",
            content=f"paragraph {idx}",
            line_start=idx,
            line_end=idx,
            symbol=f"chunk_{idx}",
        )

    writer = ComprehensiveChunkWriter(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={
            "base_url": "http://ai:8002/v1",
            "model_name": "chat",
            "profile_id": "oss_high",
        },
    )

    async def _raise_large(*_args, **_kwargs):
        raise importlib.import_module("mcp_server.indexing.summarization").FileTooLargeError("big")

    call_index = {"value": 0}
    seen_batches: list[list[str]] = []

    async def _fake_profile_batch(file_id, file_path, file_content, chunks, symbol_map):
        del file_path, file_content, symbol_map
        call_index["value"] += 1
        chunk_ids = [chunk["chunk_id"] for chunk in chunks]
        seen_batches.append(chunk_ids)
        if call_index["value"] == 1:
            chunk_ids = chunk_ids[:1]
        for chunk_id in chunk_ids:
            store.store_chunk_summary(
                chunk_hash=chunk_id,
                file_id=file_id,
                chunk_start=1,
                chunk_end=1,
                summary_text=f"summary for {chunk_id}",
                llm_model="chat",
                is_authoritative=True,
                symbol=None,
                provider_name="openai_compatible",
                profile_id="oss_high",
                prompt_fingerprint="test-fingerprint",
                audit_metadata={"provider_name": "openai_compatible", "profile_id": "oss_high"},
            )
        return [
            SimpleNamespace(chunk_id=chunk_id, summary=f"summary for {chunk_id}")
            for chunk_id in chunk_ids
        ]

    async def _should_not_run(*_args, **_kwargs):
        raise AssertionError(
            "topological fallback should not run when large-file profile recovery keeps making progress"
        )

    monkeypatch.setenv("OPENAI_API_KEY", "dummy-local-key")
    monkeypatch.setattr(writer, "_call_batch_api", _raise_large)
    monkeypatch.setattr(writer, "_call_profile_batch_api", _fake_profile_batch)
    monkeypatch.setattr(writer, "_summarize_topological", _should_not_run)
    writer._profile_model_resolution = EnrichmentModelResolution(
        configured_model="chat",
        effective_model="fake-model",
        resolution_strategy="injected_test_model",
    )

    result = await writer.process_scope(limit=10, target_paths=[target])

    assert seen_batches == [["chunk-1", "chunk-2", "chunk-3"], ["chunk-2", "chunk-3"]]
    assert result.summaries_written == 3
    assert result.authoritative_chunks == 3
    assert result.chunks_attempted == 5
    assert result.missing_chunk_ids == ("chunk-2", "chunk-3")


@pytest.mark.asyncio
async def test_process_scope_caps_doc_like_file_batches_and_resumes_progress(tmp_path, monkeypatch):
    db_path = tmp_path / "summaries.db"
    store = SQLiteStore(str(db_path))
    repo_id = store.ensure_repository_row(tmp_path)

    target = tmp_path / "target.md"
    target.write_text("doc\n" * 40, encoding="utf-8")
    file_id = store.store_file(
        repo_id,
        path=target,
        relative_path="target.md",
        language="markdown",
        size=target.stat().st_size,
        content_hash="target-hash",
    )
    for idx in range(1, 21):
        symbol_id = store.store_symbol(
            file_id=file_id,
            name=f"chunk_{idx}",
            kind="paragraph",
            line_start=idx,
            line_end=idx,
        )
        store.store_chunk(
            file_id=file_id,
            content=f"paragraph {idx}",
            content_start=0,
            content_end=len(f"paragraph {idx}"),
            line_start=idx,
            line_end=idx,
            chunk_id=f"chunk-{idx}",
            node_id=f"node-chunk-{idx}",
            treesitter_file_id=f"tree-chunk-{idx}",
            symbol_id=symbol_id,
            language="markdown",
            node_type="paragraph",
        )

    writer = ComprehensiveChunkWriter(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={"base_url": "http://ai:8002/v1", "model_name": "chat"},
    )
    batch_order: list[list[str]] = []

    async def _fake_summarize_file_chunks(**kwargs):
        chunk_ids = [chunk["chunk_id"] for chunk in kwargs["chunks"]]
        batch_order.append(chunk_ids)
        for chunk_id in chunk_ids:
            store.store_chunk_summary(
                chunk_hash=chunk_id,
                file_id=file_id,
                chunk_start=1,
                chunk_end=1,
                summary_text=f"summary for {chunk_id}",
                llm_model="chat",
                is_authoritative=True,
                symbol=None,
                provider_name="openai_compatible",
                profile_id="oss_high",
                prompt_fingerprint="test-fingerprint",
                audit_metadata={"provider_name": "openai_compatible"},
            )
        return SummaryGenerationResult(
            chunks_attempted=len(chunk_ids),
            summaries_written=len(chunk_ids),
            authoritative_chunks=len(chunk_ids),
            missing_chunk_ids=[],
            files_attempted=1,
            files_summarized=1,
        )

    monkeypatch.setattr(writer, "summarize_file_chunks", _fake_summarize_file_chunks)

    result = await writer.process_scope(limit=64, target_paths=[target])

    assert [len(batch) for batch in batch_order] == [16, 4]
    assert batch_order[0][0] == "chunk-1"
    assert batch_order[0][-1] == "chunk-16"
    assert batch_order[1] == ["chunk-17", "chunk-18", "chunk-19", "chunk-20"]
    assert result.summaries_written == 20
    assert result.authoritative_chunks == 20
    assert result.chunks_attempted == 20
    assert result.batches_processed == 2
    assert result.remaining_chunks == 0
    assert result.scope_drained is True


@pytest.mark.asyncio
async def test_process_scope_uses_smaller_doc_batch_cap_for_repo_scope(tmp_path, monkeypatch):
    db_path = tmp_path / "summaries.db"
    store = SQLiteStore(str(db_path))
    repo_id = store.ensure_repository_row(tmp_path)

    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("doc\n" * 20, encoding="utf-8")
    second.write_text("doc\n" * 20, encoding="utf-8")

    for file_path, content_hash in ((first, "first-hash"), (second, "second-hash")):
        file_id = store.store_file(
            repo_id,
            path=file_path,
            relative_path=file_path.name,
            language="markdown",
            size=file_path.stat().st_size,
            content_hash=content_hash,
        )
        for idx in range(1, 11):
            symbol_id = store.store_symbol(
                file_id=file_id,
                name=f"{file_path.stem}_{idx}",
                kind="paragraph",
                line_start=idx,
                line_end=idx,
            )
            store.store_chunk(
                file_id=file_id,
                content=f"{file_path.stem} paragraph {idx}",
                content_start=0,
                content_end=len(f"{file_path.stem} paragraph {idx}"),
                line_start=idx,
                line_end=idx,
                chunk_id=f"{file_path.stem}-chunk-{idx}",
                node_id=f"{file_path.stem}-node-{idx}",
                treesitter_file_id=f"{file_path.stem}-tree-{idx}",
                symbol_id=symbol_id,
                language="markdown",
                node_type="paragraph",
            )

    writer = ComprehensiveChunkWriter(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={"base_url": "http://ai:8002/v1", "model_name": "chat"},
    )
    batch_order: list[list[str]] = []

    async def _fake_summarize_file_chunks(**kwargs):
        chunk_ids = [chunk["chunk_id"] for chunk in kwargs["chunks"]]
        batch_order.append(chunk_ids)
        file_id = kwargs["file_id"]
        for chunk in kwargs["chunks"]:
            store.store_chunk_summary(
                chunk_hash=chunk["chunk_id"],
                file_id=file_id,
                chunk_start=chunk["line_start"],
                chunk_end=chunk["line_end"],
                summary_text=f"summary for {chunk['chunk_id']}",
                llm_model="chat",
                is_authoritative=True,
                symbol=None,
                provider_name="openai_compatible",
                profile_id="oss_high",
                prompt_fingerprint="test-fingerprint",
                audit_metadata={"provider_name": "openai_compatible"},
            )
        return SummaryGenerationResult(
            chunks_attempted=len(chunk_ids),
            summaries_written=len(chunk_ids),
            authoritative_chunks=len(chunk_ids),
            missing_chunk_ids=[],
            files_attempted=1,
            files_summarized=1,
        )

    monkeypatch.setattr(writer, "summarize_file_chunks", _fake_summarize_file_chunks)

    result = await writer.process_scope(
        limit=64,
        target_paths=[first, second],
        max_batches=1,
    )

    assert batch_order == [[f"first-chunk-{idx}" for idx in range(1, 5)]]
    assert result.summaries_written == 4
    assert result.chunks_attempted == 4
    assert result.remaining_chunks == 16
    assert result.scope_drained is False


@pytest.mark.asyncio
async def test_process_scope_uses_single_chunk_file_window_for_repo_scope_doc_like_files(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "summaries.db"
    store = SQLiteStore(str(db_path))
    repo_id = store.ensure_repository_row(tmp_path)

    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("doc\n" * 20, encoding="utf-8")
    second.write_text("doc\n" * 20, encoding="utf-8")

    for file_path, content_hash in ((first, "first-hash"), (second, "second-hash")):
        file_id = store.store_file(
            repo_id,
            path=file_path,
            relative_path=file_path.name,
            language="markdown",
            size=file_path.stat().st_size,
            content_hash=content_hash,
        )
        for idx in range(1, 5):
            symbol_id = store.store_symbol(
                file_id=file_id,
                name=f"{file_path.stem}_{idx}",
                kind="paragraph",
                line_start=idx,
                line_end=idx,
            )
            store.store_chunk(
                file_id=file_id,
                content=f"{file_path.stem} paragraph {idx}",
                content_start=0,
                content_end=len(f"{file_path.stem} paragraph {idx}"),
                line_start=idx,
                line_end=idx,
                chunk_id=f"{file_path.stem}-chunk-{idx}",
                node_id=f"{file_path.stem}-node-{idx}",
                treesitter_file_id=f"{file_path.stem}-tree-{idx}",
                symbol_id=symbol_id,
                language="markdown",
                node_type="paragraph",
            )

    writer = ComprehensiveChunkWriter(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={"base_url": "http://ai:8002/v1", "model_name": "chat"},
    )
    seen_windows: list[tuple[list[str], int | None]] = []

    async def _fake_summarize_file_chunks(**kwargs):
        chunk_ids = [chunk["chunk_id"] for chunk in kwargs["chunks"]]
        seen_windows.append((chunk_ids, kwargs.get("max_chunks")))
        file_id = kwargs["file_id"]
        chunk = kwargs["chunks"][0]
        store.store_chunk_summary(
            chunk_hash=chunk["chunk_id"],
            file_id=file_id,
            chunk_start=chunk["line_start"],
            chunk_end=chunk["line_end"],
            summary_text=f"summary for {chunk['chunk_id']}",
            llm_model="chat",
            is_authoritative=True,
            symbol=None,
            provider_name="openai_compatible",
            profile_id="oss_high",
            prompt_fingerprint="test-fingerprint",
            audit_metadata={"provider_name": "openai_compatible"},
        )
        return SummaryGenerationResult(
            chunks_attempted=1,
            summaries_written=1,
            authoritative_chunks=1,
            missing_chunk_ids=chunk_ids[1:],
            files_attempted=1,
            files_summarized=1,
            remaining_chunks=len(chunk_ids) - 1,
            scope_drained=False,
        )

    monkeypatch.setattr(writer, "summarize_file_chunks", _fake_summarize_file_chunks)

    result = await writer.process_scope(limit=64, target_paths=[first, second], max_batches=1)

    assert seen_windows == [([f"first-chunk-{idx}" for idx in range(1, 5)], 1)]
    assert result.summaries_written == 1
    assert result.chunks_attempted == 4
    assert result.remaining_chunks == 7
    assert result.scope_drained is False


@pytest.mark.asyncio
async def test_process_scope_bounds_doc_like_topological_recovery_after_batch_failure(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "summaries.db"
    store = SQLiteStore(str(db_path))
    repo_id = store.ensure_repository_row(tmp_path)

    target = tmp_path / "target.md"
    target.write_text("doc\n" * 20, encoding="utf-8")
    file_id = store.store_file(
        repo_id,
        path=target,
        relative_path="target.md",
        language="markdown",
        size=target.stat().st_size,
        content_hash="target-hash",
    )
    for idx in range(1, 11):
        symbol_id = store.store_symbol(
            file_id=file_id,
            name=f"chunk_{idx}",
            kind="paragraph",
            line_start=idx,
            line_end=idx,
        )
        store.store_chunk(
            file_id=file_id,
            content=f"paragraph {idx}",
            content_start=0,
            content_end=len(f"paragraph {idx}"),
            line_start=idx,
            line_end=idx,
            chunk_id=f"chunk-{idx}",
            node_id=f"node-chunk-{idx}",
            treesitter_file_id=f"tree-chunk-{idx}",
            symbol_id=symbol_id,
            language="markdown",
            node_type="paragraph",
        )

    writer = ComprehensiveChunkWriter(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={"base_url": "http://ai:8002/v1", "model_name": "chat"},
    )
    summarized: list[str] = []

    async def _raise_batch(*_args, **_kwargs):
        raise RuntimeError("Request timed out.")

    async def _fake_summarize_chunk(**kwargs):
        chunk_id = kwargs["chunk_hash"]
        summarized.append(chunk_id)
        store.store_chunk_summary(
            chunk_hash=chunk_id,
            file_id=file_id,
            chunk_start=kwargs["chunk_start"],
            chunk_end=kwargs["chunk_end"],
            summary_text=f"summary for {chunk_id}",
            llm_model="chat",
            is_authoritative=True,
            symbol=kwargs["symbol"],
            provider_name="openai_compatible",
            profile_id="oss_high",
            prompt_fingerprint="test-fingerprint",
            audit_metadata={"provider_name": "openai_compatible"},
        )
        return f"summary for {chunk_id}"

    monkeypatch.setattr(writer, "_call_profile_batch_api", _raise_batch)
    monkeypatch.setattr(writer, "summarize_chunk", _fake_summarize_chunk)

    result = await writer.process_scope(limit=16, target_paths=[target], max_batches=1)

    assert summarized == ["chunk-1", "chunk-2", "chunk-3", "chunk-4"]
    assert result.summaries_written == 4
    assert result.chunks_attempted == 10
    assert result.remaining_chunks == 6
    assert result.scope_drained is False


@pytest.mark.asyncio
async def test_summarize_file_chunks_bounds_repo_scope_doc_like_backlog_and_reports_remaining(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "summaries.db"
    store = SQLiteStore(str(db_path))
    repo_id = store.ensure_repository_row(tmp_path)

    target = tmp_path / "target.md"
    target.write_text("doc\n" * 12, encoding="utf-8")
    file_id = store.store_file(
        repo_id,
        path=target,
        relative_path="target.md",
        language="markdown",
        size=target.stat().st_size,
        content_hash="target-hash",
    )
    chunks = []
    for idx in range(1, 5):
        symbol_id = store.store_symbol(
            file_id=file_id,
            name=f"chunk_{idx}",
            kind="paragraph",
            line_start=idx,
            line_end=idx,
        )
        chunk = {
            "chunk_id": f"chunk-{idx}",
            "line_start": idx,
            "line_end": idx,
            "node_type": "paragraph",
            "symbol_id": symbol_id,
            "language": "markdown",
            "content": f"paragraph {idx}",
        }
        chunks.append(chunk)
        store.store_chunk(
            file_id=file_id,
            content=chunk["content"],
            content_start=0,
            content_end=len(chunk["content"]),
            line_start=idx,
            line_end=idx,
            chunk_id=chunk["chunk_id"],
            node_id=f"node-chunk-{idx}",
            treesitter_file_id=f"tree-chunk-{idx}",
            symbol_id=symbol_id,
            language="markdown",
            node_type="paragraph",
        )

    summarizer = FileBatchSummarizer(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={"base_url": "http://ai:8002/v1", "model_name": "chat"},
    )
    seen_batches: list[list[str]] = []

    async def _fake_profile_batch(file_id, file_path, file_content, chunks, symbol_map):
        del file_id, file_path, file_content, symbol_map
        seen_batches.append([chunk["chunk_id"] for chunk in chunks])
        return [SimpleNamespace(chunk_id=chunks[0]["chunk_id"], summary="Only one summary")]

    monkeypatch.setattr(summarizer, "_call_profile_batch_api", _fake_profile_batch)
    summarizer._profile_model_resolution = EnrichmentModelResolution(
        configured_model="chat",
        effective_model="fake-model",
        resolution_strategy="injected_test_model",
    )

    result = await summarizer.summarize_file_chunks(
        file_id=file_id,
        file_path=str(target),
        file_content=target.read_text(encoding="utf-8"),
        chunks=chunks,
        symbol_map={},
        persist=True,
        max_chunks=1,
    )

    assert seen_batches == [["chunk-1"]]
    assert result.chunks_attempted == 1
    assert result.summaries_written == 1
    assert result.authoritative_chunks == 1
    assert result.missing_chunk_ids == ("chunk-2", "chunk-3", "chunk-4")
    assert result.remaining_chunks == 3
    assert result.scope_drained is False
    assert store.get_chunk_summary("chunk-1") is not None
    assert store.get_chunk_summary("chunk-2") is None


@pytest.mark.asyncio
async def test_summarize_file_chunks_returns_exact_timeout_result_for_repo_scope_single_call(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "summaries.db"
    store = SQLiteStore(str(db_path))
    repo_id = store.ensure_repository_row(tmp_path)

    target = tmp_path / "target.md"
    target.write_text("doc\n" * 12, encoding="utf-8")
    file_id = store.store_file(
        repo_id,
        path=target,
        relative_path="target.md",
        language="markdown",
        size=target.stat().st_size,
        content_hash="target-hash",
    )
    chunks = []
    for idx in range(1, 5):
        symbol_id = store.store_symbol(
            file_id=file_id,
            name=f"chunk_{idx}",
            kind="paragraph",
            line_start=idx,
            line_end=idx,
        )
        chunk = {
            "chunk_id": f"chunk-{idx}",
            "line_start": idx,
            "line_end": idx,
            "node_type": "paragraph",
            "symbol_id": symbol_id,
            "language": "markdown",
            "content": f"paragraph {idx}",
        }
        chunks.append(chunk)
        store.store_chunk(
            file_id=file_id,
            content=chunk["content"],
            content_start=0,
            content_end=len(chunk["content"]),
            line_start=idx,
            line_end=idx,
            chunk_id=chunk["chunk_id"],
            node_id=f"node-chunk-{idx}",
            treesitter_file_id=f"tree-chunk-{idx}",
            symbol_id=symbol_id,
            language="markdown",
            node_type="paragraph",
        )

    summarizer = FileBatchSummarizer(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={"base_url": "http://ai:8002/v1", "model_name": "chat"},
    )

    async def _timeout_profile_batch(*_args, **_kwargs):
        raise TimeoutError("synthetic timed-out summary call")

    monkeypatch.setattr(summarizer, "_call_profile_batch_api", _timeout_profile_batch)

    result = await summarizer.summarize_file_chunks(
        file_id=file_id,
        file_path=str(target),
        file_content=target.read_text(encoding="utf-8"),
        chunks=chunks,
        symbol_map={},
        persist=True,
        max_chunks=1,
        call_timeout_seconds=17.0,
    )

    assert result.summaries_written == 0
    assert result.chunks_attempted == 1
    assert result.authoritative_chunks == 0
    assert result.missing_chunk_ids == ("chunk-1", "chunk-2", "chunk-3", "chunk-4")
    assert result.remaining_chunks == 4
    assert result.scope_drained is False
    assert result.blocked_call_reason == "timeout"
    assert result.blocked_call_file_path == str(target)
    assert result.blocked_call_chunk_ids == ("chunk-1",)
    assert result.blocked_call_timeout_seconds == 17.0
    assert store.get_chunk_summary("chunk-1") is None


@pytest.mark.asyncio
async def test_process_scope_max_batches_stops_after_one_batch(tmp_path, monkeypatch):
    db_path = tmp_path / "summaries.db"
    store = SQLiteStore(str(db_path))
    repo_id = store.ensure_repository_row(tmp_path)

    target = tmp_path / "target.py"
    target.write_text(
        "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
        encoding="utf-8",
    )
    file_id = store.store_file(
        repo_id,
        path=target,
        relative_path="target.py",
        language="python",
        size=target.stat().st_size,
        content_hash="target-hash",
    )
    _store_chunk(
        store,
        file_id=file_id,
        chunk_id="chunk-1",
        content="def alpha():\n    return 1\n",
        line_start=1,
        line_end=2,
        symbol="alpha",
    )
    _store_chunk(
        store,
        file_id=file_id,
        chunk_id="chunk-2",
        content="def beta():\n    return 2\n",
        line_start=4,
        line_end=5,
        symbol="beta",
    )

    writer = ComprehensiveChunkWriter(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={"base_url": "http://ai:8002/v1", "model_name": "chat"},
    )
    batch_order: list[list[str]] = []

    async def _fake_summarize_file_chunks(**kwargs):
        chunk_ids = [chunk["chunk_id"] for chunk in kwargs["chunks"]]
        batch_order.append(chunk_ids)
        for chunk_id in chunk_ids:
            store.store_chunk_summary(
                chunk_hash=chunk_id,
                file_id=file_id,
                chunk_start=1,
                chunk_end=2,
                summary_text=f"summary for {chunk_id}",
                llm_model="chat",
                is_authoritative=True,
                symbol=None,
                provider_name="openai_compatible",
                profile_id="oss_high",
                prompt_fingerprint="test-fingerprint",
                audit_metadata={"provider_name": "openai_compatible"},
            )
        return SummaryGenerationResult(
            chunks_attempted=len(chunk_ids),
            summaries_written=len(chunk_ids),
            authoritative_chunks=len(chunk_ids),
            missing_chunk_ids=[],
            files_attempted=1,
            files_summarized=1,
        )

    monkeypatch.setattr(writer, "summarize_file_chunks", _fake_summarize_file_chunks)

    result = await writer.process_scope(limit=1, target_paths=[target], max_batches=1)

    assert batch_order == [["chunk-1"]]
    assert result.summaries_written == 1
    assert result.authoritative_chunks == 1
    assert result.chunks_attempted == 1
    assert result.batches_processed == 1
    assert result.remaining_chunks == 1
    assert result.scope_drained is False
    assert result.missing_chunk_ids == ()


@pytest.mark.asyncio
async def test_process_scope_returns_exact_timeout_result_for_repo_scope_single_call(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "summaries.db"
    store = SQLiteStore(str(db_path))
    repo_id = store.ensure_repository_row(tmp_path)

    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("doc\n" * 12, encoding="utf-8")
    second.write_text("doc\n" * 12, encoding="utf-8")

    for file_path, content_hash in ((first, "first-hash"), (second, "second-hash")):
        file_id = store.store_file(
            repo_id,
            path=file_path,
            relative_path=file_path.name,
            language="markdown",
            size=file_path.stat().st_size,
            content_hash=content_hash,
        )
        for idx in range(1, 5):
            symbol_id = store.store_symbol(
                file_id=file_id,
                name=f"{file_path.stem}_{idx}",
                kind="paragraph",
                line_start=idx,
                line_end=idx,
            )
            store.store_chunk(
                file_id=file_id,
                content=f"{file_path.stem} paragraph {idx}",
                content_start=0,
                content_end=len(f"{file_path.stem} paragraph {idx}"),
                line_start=idx,
                line_end=idx,
                chunk_id=f"{file_path.stem}-chunk-{idx}",
                node_id=f"{file_path.stem}-node-{idx}",
                treesitter_file_id=f"{file_path.stem}-tree-{idx}",
                symbol_id=symbol_id,
                language="markdown",
                node_type="paragraph",
            )

    writer = ComprehensiveChunkWriter(
        db_path=str(db_path),
        qdrant_client=None,
        summarization_config={"base_url": "http://ai:8002/v1", "model_name": "chat"},
    )

    async def _timeout_first_file(**kwargs):
        return SummaryGenerationResult(
            chunks_attempted=1,
            summaries_written=0,
            authoritative_chunks=0,
            missing_chunk_ids=[f"first-chunk-{idx}" for idx in range(1, 5)],
            files_attempted=1,
            files_summarized=0,
            remaining_chunks=4,
            scope_drained=False,
            blocked_call_reason="timeout",
            blocked_call_file_path=kwargs["file_path"],
            blocked_call_chunk_ids=["first-chunk-1"],
            blocked_call_timeout_seconds=30.0,
        )

    monkeypatch.setattr(writer, "summarize_file_chunks", _timeout_first_file)

    result = await writer.process_scope(limit=64, target_paths=[first, second], max_batches=1)

    assert result.summaries_written == 0
    assert result.chunks_attempted == 4
    assert result.remaining_chunks == 8
    assert result.scope_drained is False
    assert result.blocked_call_reason == "timeout"
    assert result.blocked_call_file_path == str(first)
    assert result.blocked_call_chunk_ids == ("first-chunk-1",)
    assert result.blocked_call_timeout_seconds == 30.0


@pytest.mark.parametrize("posture", ["standalone_local", "fleet_local", "lexical_only", "invalid"])
@pytest.mark.parametrize("surface", ["chunk", "file"])
@pytest.mark.parametrize("configured_endpoint", [False, True])
@pytest.mark.asyncio
async def test_explicit_profiles_do_not_leak_summary_egress(
    tmp_path, monkeypatch, posture, surface, configured_endpoint
):
    from mcp_server.indexing.baml_client.baml_client.async_client import b

    monkeypatch.setenv("MCP_DEPLOYMENT_PROFILE", posture)
    monkeypatch.setenv("MCP_ALLOW_COMMERCIAL_EGRESS", "1")
    for key in ("CEREBRAS_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "synthetic-key")
    calls = []

    async def sample(**kwargs):
        calls.append("sampling")
        return SimpleNamespace(content=SimpleNamespace(text="Unexpected summary"), model="fixture")

    async def commercial(*args, **kwargs):
        calls.append("commercial")
        return "Unexpected summary"

    async def baml_chunk(**kwargs):
        calls.append("baml")
        return SimpleNamespace(summary="Unexpected summary")

    async def baml_file(**kwargs):
        calls.append("baml")
        return SimpleNamespace(
            summaries=[SimpleNamespace(chunk_id="policy-chunk", summary="Unexpected summary")]
        )

    async def local_failure(*args, **kwargs):
        calls.append("local")
        raise RuntimeError("Synthetic local endpoint unavailable")

    monkeypatch.setattr(b, "SummarizeChunkAlone", baml_chunk)
    monkeypatch.setattr(b, "SummarizeFileChunks", baml_file)
    db = tmp_path / "policy.db"
    store, file_id = _seed_chunk_summary_tables(db, tmp_path)
    content = "def alpha():\n    return 1\n"
    _store_chunk(
        store,
        file_id=file_id,
        chunk_id="policy-chunk",
        content=content,
        line_start=1,
        line_end=2,
        symbol="alpha",
    )
    session = SimpleNamespace(
        client_params=SimpleNamespace(capabilities=SimpleNamespace(sampling=SimpleNamespace())),
        create_message=sample,
    )
    cfg = (
        {"base_url": "http://127.0.0.1:9/v1", "model_name": "fixture"}
        if configured_endpoint
        else {}
    )
    writer = (ChunkWriter if surface == "chunk" else FileBatchSummarizer)(
        str(db), None, session=session, summarization_config=cfg
    )
    monkeypatch.setattr(writer, "_call_profile_api", local_failure)
    for method in ("_call_cerebras_api", "_call_anthropic_api", "_call_openai_api"):
        monkeypatch.setattr(writer, method, commercial)
    try:
        if surface == "chunk":
            result = await writer.summarize_chunk(
                "policy-chunk", file_id, 1, 2, "alpha", content, language="python"
            )
            assert result is None
        else:
            result = await writer.summarize_file_chunks(
                file_id,
                "sample.py",
                content,
                [
                    {
                        "chunk_id": "policy-chunk",
                        "content": content,
                        "line_start": 1,
                        "line_end": 2,
                        "language": "python",
                        "node_type": "function_definition",
                    }
                ],
            )
            assert result.summaries_written == 0
            assert result.missing_chunk_ids == ("policy-chunk",)
        assert not (set(calls) - {"local"}), calls
        if posture in {"lexical_only", "invalid"} or not configured_endpoint:
            assert calls == []
        else:
            assert "local" in calls
    finally:
        if writer._sqlite_store is not None:
            writer._sqlite_store.close()
        store.close()


@pytest.mark.asyncio
async def test_lexical_policy_blocks_profile_client_construction(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DEPLOYMENT_PROFILE", "lexical_only")
    constructed = []

    def unexpected_client(**kwargs):
        constructed.append(True)
        raise AssertionError("Provider constructor must not be called")

    monkeypatch.setattr("openai.AsyncOpenAI", unexpected_client)
    writer = ChunkWriter(
        str(tmp_path / "unused.db"),
        None,
        summarization_config={"base_url": "http://127.0.0.1:9/v1"},
    )
    monkeypatch.setattr(writer, "_resolve_effective_profile_model", lambda: None)
    with pytest.raises(RuntimeError, match="Deployment policy"):
        await writer._call_profile_api("synthetic system", "synthetic input")
    assert constructed == []


@pytest.mark.asyncio
async def test_local_policy_blocks_actual_baml_batch_construction(tmp_path, monkeypatch):
    from mcp_server.indexing.baml_client.baml_client.async_client import b

    monkeypatch.setenv("MCP_DEPLOYMENT_PROFILE", "fleet_local")
    calls = []

    async def unexpected_batch(**kwargs):
        calls.append(True)
        return SimpleNamespace(summaries=[])

    monkeypatch.setattr(b, "SummarizeFileChunks", unexpected_batch)
    writer = FileBatchSummarizer(str(tmp_path / "unused.db"), None)
    with pytest.raises(RuntimeError, match="Deployment policy"):
        await writer._call_batch_api(1, "sample.py", "pass", [], {})
    assert calls == []


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.asyncio
async def test_permitted_commercial_summary_fallback_remains_available(
    tmp_path, monkeypatch, explicit
):
    if explicit:
        monkeypatch.setenv("MCP_DEPLOYMENT_PROFILE", "commercial")
        monkeypatch.setenv("MCP_ALLOW_COMMERCIAL_EGRESS", "1")
    else:
        monkeypatch.delenv("MCP_DEPLOYMENT_PROFILE", raising=False)
    monkeypatch.setenv("CEREBRAS_API_KEY", "synthetic-key")
    calls = []

    async def allowed(*args):
        calls.append(True)
        return "Synthetic summary"

    writer = ChunkWriter(str(tmp_path / "unused.db"), None)
    monkeypatch.setattr(writer, "_call_cerebras_api", allowed)
    assert await writer._call_direct_api("synthetic", "synthetic") == ("Synthetic summary", None)
    assert calls == [True]
