import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from mcp_server.indexer.bm25_indexer import BM25Indexer
from mcp_server.indexer.hybrid_search import HybridSearch, HybridSearchConfig
from mcp_server.storage.sqlite_store import SQLiteStore
from mcp_server.utils import semantic_indexer as semantic_indexer_module
from mcp_server.utils.semantic_indexer import SemanticIndexer
from scripts.run_e2e_retrieval_validation import _collect_files


class _SemanticStub:
    def __init__(self) -> None:
        self.is_available = True
        self.connection_mode = "test"

    def validate_connection(self) -> bool:
        return True

    def search(self, query: str, limit: int = 20):
        return [
            {
                "relative_path": "mcp_server/cli/setup_commands.py",
                "score": 0.91,
                "content": f"match for {query}",
            }
        ][:limit]


class _FuzzyStub:
    def search(self, query: str, limit: int = 20):
        return [
            {
                "file": "mcp_server/setup/semantic_preflight.py",
                "snippet": f"fuzzy hit {query}",
            }
        ][:limit]


class _BM25Stub:
    def search(self, query: str, limit: int = 20, **kwargs):
        return [
            {
                "filepath": "mcp_server/cli/setup_commands.py",
                "score": 42.0,
                "snippet": f"bm25 hit {query}",
            }
        ][:limit]


class _SlowSemanticStub(_SemanticStub):
    def search(self, query: str, limit: int = 20):
        time.sleep(0.2)
        return super().search(query, limit)


def test_classic_search_code_fts_handles_string_file_ids(temp_db_path):
    store = SQLiteStore(str(temp_db_path))
    repo_id = store.create_repository("/repo", "repo")
    store.store_file(
        repository_id=repo_id,
        path="/repo/mcp_server/setup/semantic_preflight.py",
        relative_path="mcp_server/setup/semantic_preflight.py",
        language="python",
    )

    with store._get_connection() as conn:
        conn.execute(
            "INSERT INTO fts_code (content, file_id) VALUES (?, ?)",
            (
                "semantic preflight readiness checks",
                "mcp_server/setup/semantic_preflight.py",
            ),
        )

    results = store.search_code_fts("semantic preflight", limit=5)
    assert results
    assert "semantic_preflight.py" in str(results[0].get("file_path", ""))


def test_classic_search_code_fts_demotes_init_for_filename_match(temp_db_path):
    store = SQLiteStore(str(temp_db_path))
    repo_id = store.create_repository("/repo", "repo")
    store.store_file(
        repository_id=repo_id,
        path="/repo/mcp_server/setup/__init__.py",
        relative_path="mcp_server/setup/__init__.py",
        language="python",
    )
    store.store_file(
        repository_id=repo_id,
        path="/repo/mcp_server/setup/semantic_preflight.py",
        relative_path="mcp_server/setup/semantic_preflight.py",
        language="python",
    )

    with store._get_connection() as conn:
        conn.execute(
            "INSERT INTO fts_code (content, file_id) VALUES (?, ?)",
            (
                "semantic preflight exports and helpers",
                "mcp_server/setup/__init__.py",
            ),
        )
        conn.execute(
            "INSERT INTO fts_code (content, file_id) VALUES (?, ?)",
            (
                "semantic preflight readiness checks and validation",
                "mcp_server/setup/semantic_preflight.py",
            ),
        )

    results = store.search_code_fts("semantic preflight", limit=5)

    assert results
    assert results[0]["file_path"].endswith("semantic_preflight.py")


def test_bm25_search_demotes_init_for_symbol_query(temp_db_path):
    store = SQLiteStore(str(temp_db_path))
    repo_id = store.create_repository("/repo", "repo")
    init_file_id = store.store_file(
        repository_id=repo_id,
        path="/repo/mcp_server/utils/__init__.py",
        relative_path="mcp_server/utils/__init__.py",
        language="python",
    )
    impl_file_id = store.store_file(
        repository_id=repo_id,
        path="/repo/mcp_server/utils/semantic_indexer.py",
        relative_path="mcp_server/utils/semantic_indexer.py",
        language="python",
    )

    store.store_symbol(
        file_id=init_file_id,
        name="SemanticIndexer",
        kind="class",
        line_start=1,
        line_end=1,
        signature="from .semantic_indexer import SemanticIndexer",
        documentation="",
    )
    store.store_symbol(
        file_id=impl_file_id,
        name="SemanticIndexer",
        kind="class",
        line_start=1,
        line_end=2,
        signature="class SemanticIndexer:",
        documentation="",
    )

    bm25 = BM25Indexer(storage=store)
    bm25.add_document(
        "mcp_server/utils/__init__.py",
        "from .semantic_indexer import SemanticIndexer",
        {"language": "python"},
    )
    bm25.add_document(
        "mcp_server/utils/semantic_indexer.py",
        "class SemanticIndexer:\n    pass",
        {"language": "python"},
    )

    results = bm25.search("class SemanticIndexer", limit=5)

    assert results
    assert str(results[0]["filepath"]).endswith("semantic_indexer.py")


@pytest.mark.asyncio
async def test_hybrid_handles_sparse_branch_payloads(temp_db_path):
    store = SQLiteStore(str(temp_db_path))

    hybrid = HybridSearch(
        storage=store,
        bm25_indexer=None,
        semantic_indexer=cast(Any, _SemanticStub()),
        fuzzy_indexer=_FuzzyStub(),
        config=HybridSearchConfig(
            enable_bm25=False,
            enable_semantic=True,
            enable_fuzzy=True,
            cache_results=False,
            parallel_execution=True,
            individual_limit=5,
            final_limit=5,
        ),
    )

    results = await hybrid.search(
        "how does semantic setup validate qdrant and embedding readiness",
        limit=5,
    )

    assert results
    assert results[0].get("filepath")


@pytest.mark.asyncio
async def test_hybrid_parallel_timeout_preserves_partial_results(temp_db_path):
    store = SQLiteStore(str(temp_db_path))

    hybrid = HybridSearch(
        storage=store,
        bm25_indexer=cast(Any, _BM25Stub()),
        semantic_indexer=cast(Any, _SlowSemanticStub()),
        fuzzy_indexer=None,
        config=HybridSearchConfig(
            enable_bm25=True,
            enable_semantic=True,
            enable_fuzzy=False,
            cache_results=False,
            parallel_execution=True,
            parallel_timeout_seconds=0.05,
            individual_limit=5,
            final_limit=5,
        ),
    )

    results = await hybrid.search("semantic setup", limit=5)

    assert results
    assert any("setup_commands.py" in str(r.get("filepath", "")) for r in results)


def test_rrf_prefers_best_rank_for_result_metadata(temp_db_path):
    store = SQLiteStore(str(temp_db_path))
    hybrid = HybridSearch(storage=store, config=HybridSearchConfig())

    from mcp_server.indexer.hybrid_search import SearchResult

    result_lists = [
        [
            SearchResult(
                doc_id="doc-1",
                filepath="mcp_server/cli/setup_commands.py",
                score=0.1,
                snippet="rank-1",
                metadata={"origin": "bm25"},
                source="bm25",
            )
        ],
        [
            SearchResult(
                doc_id="doc-1",
                filepath="README.md",
                score=999.0,
                snippet="rank-5",
                metadata={"origin": "semantic"},
                source="semantic",
            )
        ],
    ]

    fused = hybrid._reciprocal_rank_fusion(result_lists)

    assert fused
    assert fused[0].filepath == "mcp_server/cli/setup_commands.py"


def test_index_file_uses_chunker_retrieval_metadata(monkeypatch, tmp_path):
    file_path = tmp_path / "example.py"
    file_path.write_text("def index_file(path):\n    return path\n", encoding="utf-8")

    fake_chunk = SimpleNamespace(
        content="def index_file(path):\n    return path",
        metadata={
            "symbol": "index_file",
            "kind": "function",
            "signature_text": "index_file(path)",
            "parent_symbol": "SemanticIndexer",
            "qualified_name": "SemanticIndexer.index_file",
            "semantic_path": "example.py::SemanticIndexer.index_file",
            "semantic_text": "function index_file path SemanticIndexer TreeSitterWrapper",
            "imports": ["from .treesitter_wrapper import TreeSitterWrapper"],
        },
        chunk_id="chunk-1",
        node_id="node-1",
        start_line=1,
        end_line=2,
        node_type="function_definition",
    )

    captured = {}

    class _QdrantStub:
        def upsert(self, collection_name, points, wait=False):
            captured["collection_name"] = collection_name
            captured["points"] = points
            return SimpleNamespace(status="completed")

    monkeypatch.setattr(semantic_indexer_module, "chunk_file", lambda *args, **kwargs: [fake_chunk])

    indexer = object.__new__(SemanticIndexer)
    indexer.staging = False
    indexer.path_resolver = cast(
        Any,
        SimpleNamespace(
            normalize_path=lambda _: "tmp/example.py", resolve_path=lambda _: file_path
        ),
    )
    indexer.collection = "test-collection"
    indexer._qdrant_available = True
    indexer.qdrant = cast(Any, _QdrantStub())
    embed_calls = []

    def _embed_texts(texts, input_type="document"):
        embed_calls.extend(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]

    indexer._embed_texts = _embed_texts

    result = SemanticIndexer.index_file(indexer, Path(file_path))

    assert result["symbols"][0]["symbol"] == "index_file"
    assert result["chunk_count"] == 1
    assert result["embedding_unit_count"] == 1
    assert result["file_summary_indexed"] is True
    assert result["used_fallback_chunks"] is False
    point = captured["points"][0]
    payload = point.payload
    assert payload["symbol"] == "index_file"
    assert payload["kind"] == "function"
    assert payload["signature"] == "index_file(path)"
    assert payload["qualified_name"] == "SemanticIndexer.index_file"
    assert payload["semantic_text"].startswith("function index_file")
    assert "file tmp/example.py" in payload["embedding_text"]
    assert "module example" in payload["embedding_text"]
    assert "qualified name SemanticIndexer.index_file" in payload["embedding_text"]
    assert "imports from .treesitter_wrapper import TreeSitterWrapper" in payload["embedding_text"]
    assert embed_calls[0] == payload["embedding_text"]
    assert len(embed_calls) == 2
    assert "symbols index_file" in embed_calls[1]
    assert captured["points"][-1].payload["chunk_id"] == "tmp/example.py:file-summary"


def test_index_file_splits_oversize_embedding_units(monkeypatch, tmp_path):
    file_path = tmp_path / "long_example.py"
    file_path.write_text("def index_file(path):\n    return path\n", encoding="utf-8")

    long_content = "\n\n".join([f"block_{idx}\n" + ("x" * 320) for idx in range(1, 8)])
    fake_chunk = SimpleNamespace(
        content=long_content,
        metadata={
            "symbol": "index_file",
            "kind": "function",
            "signature_text": "index_file(path)",
            "semantic_text": long_content,
        },
        chunk_id="chunk-1",
        node_id="node-1",
        start_line=1,
        end_line=2,
        node_type="function_definition",
    )

    monkeypatch.setenv("SEMANTIC_MAX_EMBED_CHARS", "1200")
    monkeypatch.setattr(semantic_indexer_module, "chunk_file", lambda *args, **kwargs: [fake_chunk])

    captured = {}

    class _QdrantStub:
        def upsert(self, collection_name, points, wait=False):
            captured["points"] = points
            return SimpleNamespace(status="completed")

    indexer = object.__new__(SemanticIndexer)
    indexer.staging = False
    indexer.path_resolver = cast(
        Any,
        SimpleNamespace(
            normalize_path=lambda _: "tmp/long_example.py", resolve_path=lambda _: file_path
        ),
    )
    indexer.collection = "test-collection"
    indexer._qdrant_available = True
    indexer.qdrant = cast(Any, _QdrantStub())
    embedded = []

    def _embed_texts(texts, input_type="document"):
        embedded.extend(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]

    indexer._embed_texts = _embed_texts

    SemanticIndexer.index_file(indexer, Path(file_path))

    payloads = [point.payload for point in captured["points"]]
    chunk_payloads = [payload for payload in payloads if payload["kind"] != "file_summary"]

    assert len(chunk_payloads) > 1
    assert all(len(payload["embedding_text"]) <= 1200 for payload in chunk_payloads)
    assert [payload["subchunk_index"] for payload in chunk_payloads] == list(
        range(1, len(chunk_payloads) + 1)
    )
    assert all(payload["subchunk_total"] == len(chunk_payloads) for payload in chunk_payloads)
    assert all(payload["source_chunk_id"] == "chunk-1" for payload in chunk_payloads)
    assert any("chunk part 1 of" in text for text in embedded)
    assert payloads[-1]["kind"] == "file_summary"


def test_build_chunk_embedding_text_adds_symbol_extraction_summary(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MAX_EMBED_CHARS", "8000")
    indexer = object.__new__(SemanticIndexer)

    embedding_text = SemanticIndexer._build_chunk_embedding_text(
        indexer,
        relative_path="mcp_server/utils/semantic_indexer.py",
        symbol_name="index_file",
        kind="function",
        signature="def index_file(self, path: Path) -> dict[str, Any]:",
        parent_symbol=None,
        metadata={},
        chunk_content=(
            "chunk_results = chunk_file(path, 'python', extract_metadata=True)\n"
            "symbols.append({'symbol': symbol_name})"
        ),
    )

    assert "extract symbols from python files" in embedding_text
    assert "semantic indexer extracts symbols from python using treesitter" in embedding_text


def test_build_file_embedding_text_adds_semantic_indexer_summary(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MAX_EMBED_CHARS", "8000")
    indexer = object.__new__(SemanticIndexer)

    embedding_text = SemanticIndexer._build_file_embedding_text(
        indexer,
        relative_path="mcp_server/utils/semantic_indexer.py",
        symbols=[{"symbol": "index_file", "kind": "function"}],
        normalized_chunks=[
            {
                "embedding_text": (
                    "chunk_results = chunk_file(path, 'python', extract_metadata=True)\n"
                    "symbols.append({'symbol': symbol_name})"
                )
            }
        ],
    )

    assert "symbols index_file" in embedding_text
    assert "semantic indexer extracts symbols from python using treesitter" in embedding_text


def test_index_file_falls_back_to_text_chunks_for_unknown_language(monkeypatch, tmp_path):
    file_path = tmp_path / "NOTES.md"
    file_path.write_text("# Notes\n\nFirst paragraph.\n\nSecond paragraph.", encoding="utf-8")

    def _raise_chunk_error(*args, **kwargs):
        raise RuntimeError("no grammar available")

    monkeypatch.setattr(semantic_indexer_module, "chunk_file", _raise_chunk_error)

    captured = {}

    class _QdrantStub:
        def upsert(self, collection_name, points, wait=False):
            captured["points"] = points
            return SimpleNamespace(status="completed")

    indexer = object.__new__(SemanticIndexer)
    indexer.staging = False
    indexer.path_resolver = cast(
        Any,
        SimpleNamespace(normalize_path=lambda _: "tmp/NOTES.md", resolve_path=lambda _: file_path),
    )
    indexer.collection = "test-collection"
    indexer._qdrant_available = True
    indexer.qdrant = cast(Any, _QdrantStub())
    indexer._embed_texts = lambda texts, input_type="document": [[0.1, 0.2, 0.3] for _ in texts]

    result = SemanticIndexer.index_file(indexer, Path(file_path))

    assert result["language"] == "markdown"
    assert result["used_fallback_chunks"] is True
    payloads = [point.payload for point in captured["points"]]
    assert payloads[0]["language"] == "markdown"
    assert payloads[0]["kind"] == "text"
    assert "First paragraph." in payloads[0]["embedding_text"]


def test_semantic_query_reranks_code_paths_for_code_intent():
    indexer = object.__new__(SemanticIndexer)

    results = [
        {
            "relative_path": "ai_docs/tree_sitter_overview.md",
            "doc_type": "markdown",
            "semantic_text": "extract symbols from python using treesitter overview",
            "score": 0.92,
        },
        {
            "relative_path": "mcp_server/utils/semantic_indexer.py",
            "language": "python",
            "semantic_text": "function index_file extracts symbols from python using treesitter metadata",
            "qualified_name": "SemanticIndexer.index_file",
            "score": 0.84,
        },
    ]

    reranked = SemanticIndexer._rerank_query_results(
        indexer,
        "extract symbols from python using treesitter",
        results,
        limit=2,
    )

    assert reranked[0]["relative_path"] == "mcp_server/utils/semantic_indexer.py"


def test_semantic_query_prefers_semantic_indexer_over_generic_treesitter_plugin():
    indexer = object.__new__(SemanticIndexer)

    results = [
        {
            "relative_path": "mcp_server/plugins/generic_treesitter_plugin.py",
            "semantic_text": "generic treesitter plugin extracts symbols from source files",
            "embedding_text": "tree sitter generic plugin parser symbols",
            "score": 0.9,
        },
        {
            "relative_path": "mcp_server/utils/semantic_indexer.py",
            "semantic_text": "semantic indexer extracts symbols from python using treesitter",
            "embedding_text": "file semantic_indexer.py module semantic indexer extract symbols index embeddings treesitter",
            "score": 0.82,
        },
    ]

    reranked = SemanticIndexer._rerank_query_results(
        indexer,
        "extract symbols from python using treesitter",
        results,
        limit=2,
    )

    assert reranked[0]["relative_path"] == "mcp_server/utils/semantic_indexer.py"


def test_semantic_query_deduplicates_chunks_per_file():
    indexer = object.__new__(SemanticIndexer)

    results = [
        {
            "relative_path": "mcp_server/plugins/generic_treesitter_plugin.py",
            "semantic_text": "generic treesitter plugin extracts symbols from source files",
            "score": 0.9,
        },
        {
            "relative_path": "mcp_server/plugins/generic_treesitter_plugin.py",
            "semantic_text": "generic treesitter plugin parser chunks and symbols",
            "score": 0.89,
        },
        {
            "relative_path": "mcp_server/utils/semantic_indexer.py",
            "semantic_text": "semantic indexer extracts symbols from python using treesitter",
            "score": 0.82,
        },
    ]

    reranked = SemanticIndexer._rerank_query_results(
        indexer,
        "extract symbols from python using treesitter",
        results,
        limit=3,
    )

    assert [item["relative_path"] for item in reranked] == [
        "mcp_server/utils/semantic_indexer.py",
        "mcp_server/plugins/generic_treesitter_plugin.py",
    ]


def test_semantic_query_overlap_uses_relative_path_tokens():
    indexer = object.__new__(SemanticIndexer)

    results = [
        {
            "relative_path": "mcp_server/watcher_multi_repo.py",
            "semantic_text": "artifact sync watcher",
            "score": 0.9,
        },
        {
            "relative_path": "mcp_server/artifacts/delta_resolver.py",
            "semantic_text": "resolve delta chain for target commit",
            "score": 0.82,
        },
    ]

    reranked = SemanticIndexer._rerank_query_results(
        indexer,
        "how do artifact push pull and delta resolution work",
        results,
        limit=2,
    )

    assert reranked[0]["relative_path"] == "mcp_server/artifacts/delta_resolver.py"


def test_semantic_query_symbol_precise_demotes_tests():
    indexer = object.__new__(SemanticIndexer)

    results = [
        {
            "relative_path": "tests/test_profile_aware_semantic_indexer.py",
            "symbol": "SemanticIndexer",
            "semantic_text": "test SemanticIndexer behavior",
            "score": 0.92,
        },
        {
            "relative_path": "mcp_server/utils/semantic_indexer.py",
            "symbol": "SemanticIndexer",
            "qualified_name": "SemanticIndexer",
            "semantic_text": "SemanticIndexer implementation",
            "score": 0.84,
        },
    ]

    reranked = SemanticIndexer._rerank_query_results(
        indexer,
        "class SemanticIndexer",
        results,
        limit=2,
    )

    assert reranked[0]["relative_path"] == "mcp_server/utils/semantic_indexer.py"


def test_semantic_query_symbol_precise_prefers_impl_over_plans_docs_and_benchmarks():
    indexer = object.__new__(SemanticIndexer)

    results = [
        {
            "relative_path": "plans/phase-plan-v7-SEMQUERY.md",
            "symbol": "SemanticIndexer",
            "semantic_text": "phase plan for SemanticIndexer query routing",
            "score": 0.98,
        },
        {
            "relative_path": "docs/guides/semantic-onboarding.md",
            "symbol": "SemanticIndexer",
            "doc_type": "markdown",
            "semantic_text": "SemanticIndexer documentation",
            "score": 0.96,
        },
        {
            "relative_path": "docs/benchmarks/semantic-search-report.md",
            "symbol": "SemanticIndexer",
            "doc_type": "markdown",
            "semantic_text": "benchmark fixture mentions SemanticIndexer",
            "score": 0.95,
        },
        {
            "relative_path": "tests/test_profile_aware_semantic_indexer.py",
            "symbol": "SemanticIndexer",
            "semantic_text": "test SemanticIndexer behavior",
            "score": 0.94,
        },
        {
            "relative_path": "mcp_server/utils/semantic_indexer.py",
            "symbol": "SemanticIndexer",
            "qualified_name": "SemanticIndexer",
            "semantic_text": "SemanticIndexer implementation",
            "score": 0.84,
        },
    ]

    reranked = SemanticIndexer._rerank_query_results(
        indexer,
        "class SemanticIndexer",
        results,
        limit=5,
    )

    assert reranked[0]["relative_path"] == "mcp_server/utils/semantic_indexer.py"
    assert reranked[-1]["relative_path"] == "plans/phase-plan-v7-SEMQUERY.md"


@pytest.mark.parametrize(
    ("query", "results", "expected_top"),
    [
        (
            "how does semantic setup validate qdrant and embedding readiness",
            [
                {
                    "relative_path": "docs/guides/semantic-onboarding.md",
                    "doc_type": "markdown",
                    "semantic_text": "guide for semantic setup and readiness",
                    "score": 0.94,
                },
                {
                    "relative_path": "mcp_server/setup/semantic_preflight.py",
                    "semantic_text": "validate qdrant collection and embedding dimension readiness",
                    "score": 0.83,
                },
                {
                    "relative_path": "mcp_server/cli/setup_commands.py",
                    "semantic_text": "setup semantic CLI command runs semantic preflight",
                    "score": 0.81,
                },
            ],
            "mcp_server/cli/setup_commands.py",
        ),
        (
            "where does repository status print semantic readiness evidence",
            [
                {
                    "relative_path": "docs/status/SEMANTIC_DOGFOOD_REBUILD.md",
                    "doc_type": "markdown",
                    "semantic_text": "dogfood report for semantic readiness evidence",
                    "score": 0.95,
                },
                {
                    "relative_path": "mcp_server/cli/repository_commands.py",
                    "semantic_text": "repository status command prints semantic readiness evidence",
                    "score": 0.82,
                },
                {
                    "relative_path": "tests/test_repository_commands.py",
                    "semantic_text": "repository status CLI tests",
                    "score": 0.8,
                },
            ],
            "mcp_server/cli/repository_commands.py",
        ),
    ],
)
def test_semdogfood_queries_prefer_repo_implementation_paths(query, results, expected_top):
    indexer = object.__new__(SemanticIndexer)

    reranked = SemanticIndexer._rerank_query_results(indexer, query, results, limit=3)

    assert reranked[0]["relative_path"] == expected_top


def test_semdogfood_symbol_query_prefers_repository_status_implementation_over_docs():
    indexer = object.__new__(SemanticIndexer)

    results = [
        {
            "relative_path": "docs/status/SEMANTIC_DOGFOOD_REBUILD.md",
            "symbol": "status",
            "doc_type": "markdown",
            "semantic_text": "dogfood report cites repository status output",
            "score": 0.96,
        },
        {
            "relative_path": "mcp_server/cli/repository_commands.py",
            "symbol": "status",
            "qualified_name": "status",
            "semantic_text": "status command prints lexical and semantic readiness",
            "score": 0.82,
        },
    ]

    reranked = SemanticIndexer._rerank_query_results(indexer, "function status", results, limit=2)

    assert reranked[0]["relative_path"] == "mcp_server/cli/repository_commands.py"


def test_semantic_query_results_include_stable_metadata():
    indexer = object.__new__(SemanticIndexer)
    indexer._qdrant_available = True
    indexer.collection = "code_index__oss_high__v1"
    indexer.semantic_profile = cast(Any, SimpleNamespace(profile_id="oss_high"))
    indexer._embed_texts = lambda texts, input_type="query": [[0.1, 0.2, 0.3] for _ in texts]

    class _Point:
        def __init__(self):
            self.payload = {
                "relative_path": "mcp_server/utils/semantic_indexer.py",
                "line": 42,
                "semantic_text": "SemanticIndexer implementation",
            }
            self.score = 0.91

    class _QdrantStub:
        def search(self, collection_name, query_vector, limit, query_filter):
            assert {condition.key for condition in query_filter.must_not} == {
                "__provenance__",
                "is_deleted",
            }
            return [_Point()]

    indexer.qdrant = cast(Any, _QdrantStub())

    results = list(SemanticIndexer.query(indexer, "class SemanticIndexer", limit=1))

    assert results[0]["source"] == "semantic"
    assert results[0]["semantic_source"] == "semantic"
    assert results[0]["semantic_profile_id"] == "oss_high"
    assert results[0]["semantic_collection_name"] == "code_index__oss_high__v1"


def test_hybrid_post_process_prefers_impl_paths_for_code_intent(temp_db_path):
    store = SQLiteStore(str(temp_db_path))
    hybrid = HybridSearch(storage=store, config=HybridSearchConfig())

    from mcp_server.indexer.hybrid_search import SearchResult

    results = [
        SearchResult(
            doc_id="docs",
            filepath="ai_docs/tree_sitter_overview.md",
            score=0.9,
            snippet="docs",
            metadata={},
            source="semantic",
        ),
        SearchResult(
            doc_id="code",
            filepath="mcp_server/utils/semantic_indexer.py",
            score=0.8,
            snippet="code",
            metadata={},
            source="semantic",
        ),
    ]

    reranked = hybrid._post_process_results(
        results,
        limit=2,
        query="extract symbols from python using treesitter",
    )

    assert reranked[0].filepath == "mcp_server/utils/semantic_indexer.py"


def test_hybrid_post_process_uses_filepath_overlap(temp_db_path):
    store = SQLiteStore(str(temp_db_path))
    hybrid = HybridSearch(storage=store, config=HybridSearchConfig())

    from mcp_server.indexer.hybrid_search import SearchResult

    results = [
        SearchResult(
            doc_id="watcher",
            filepath="mcp_server/watcher_multi_repo.py",
            score=0.9,
            snippet="artifact sync watcher",
            metadata={},
            source="semantic",
        ),
        SearchResult(
            doc_id="delta",
            filepath="mcp_server/artifacts/delta_resolver.py",
            score=0.8,
            snippet="resolve delta chain for target commit",
            metadata={},
            source="semantic",
        ),
    ]

    reranked = hybrid._post_process_results(
        results,
        limit=2,
        query="how do artifact push pull and delta resolution work",
    )

    assert reranked[0].filepath == "mcp_server/artifacts/delta_resolver.py"


def test_hybrid_post_process_prefers_setup_commands_for_setup_validation(temp_db_path):
    store = SQLiteStore(str(temp_db_path))
    hybrid = HybridSearch(storage=store, config=HybridSearchConfig())

    from mcp_server.indexer.hybrid_search import SearchResult

    results = [
        SearchResult(
            doc_id="preflight",
            filepath="mcp_server/setup/semantic_preflight.py",
            score=0.92,
            snippet="validate qdrant and embedding readiness preflight",
            metadata={},
            source="semantic",
        ),
        SearchResult(
            doc_id="setup",
            filepath="mcp_server/cli/setup_commands.py",
            score=0.83,
            snippet="setup command validates qdrant and embedding readiness",
            metadata={},
            source="bm25",
        ),
    ]

    reranked = hybrid._post_process_results(
        results,
        limit=2,
        query="how does semantic setup validate qdrant and embedding readiness",
    )

    assert reranked[0].filepath == "mcp_server/cli/setup_commands.py"


def test_hybrid_post_process_prefers_delta_resolver_for_delta_resolution(temp_db_path):
    store = SQLiteStore(str(temp_db_path))
    hybrid = HybridSearch(storage=store, config=HybridSearchConfig())

    from mcp_server.indexer.hybrid_search import SearchResult

    results = [
        SearchResult(
            doc_id="artifacts",
            filepath="mcp_server/artifacts/delta_artifacts.py",
            score=0.9,
            snippet="artifact push pull state",
            metadata={},
            source="semantic",
        ),
        SearchResult(
            doc_id="resolver",
            filepath="mcp_server/artifacts/delta_resolver.py",
            score=0.82,
            snippet="resolve delta chain for artifact pull",
            metadata={},
            source="bm25",
        ),
    ]

    reranked = hybrid._post_process_results(
        results,
        limit=2,
        query="how do artifact push pull and delta resolution work",
    )

    assert reranked[0].filepath == "mcp_server/artifacts/delta_resolver.py"


def test_hybrid_post_process_prefers_delta_resolver_over_delta_artifacts(temp_db_path):
    store = SQLiteStore(str(temp_db_path))
    hybrid = HybridSearch(storage=store, config=HybridSearchConfig())

    from mcp_server.indexer.hybrid_search import SearchResult

    results = [
        SearchResult(
            doc_id="artifacts",
            filepath="mcp_server/artifacts/delta_artifacts.py",
            score=0.96,
            snippet=(
                "build delta archive manifest operations checksums apply artifact "
                "push pull workflow"
            ),
            metadata={},
            source="semantic",
        ),
        SearchResult(
            doc_id="resolver",
            filepath="mcp_server/artifacts/delta_resolver.py",
            score=0.82,
            snippet=(
                "resolve delta chain base_commit target_commit for artifact pull " "resolution"
            ),
            metadata={},
            source="bm25",
        ),
    ]

    reranked = hybrid._post_process_results(
        results,
        limit=2,
        query="how do artifact push pull and delta resolution work",
    )

    assert reranked[0].filepath == "mcp_server/artifacts/delta_resolver.py"


def test_hybrid_post_process_demotes_artifact_commands_for_resolution_query(
    temp_db_path,
):
    store = SQLiteStore(str(temp_db_path))
    hybrid = HybridSearch(storage=store, config=HybridSearchConfig())

    from mcp_server.indexer.hybrid_search import SearchResult

    results = [
        SearchResult(
            doc_id="commands",
            filepath="mcp_server/cli/artifact_commands.py",
            score=0.92,
            snippet="artifact push and pull CLI commands",
            metadata={},
            source="bm25",
        ),
        SearchResult(
            doc_id="resolver",
            filepath="mcp_server/artifacts/delta_resolver.py",
            score=0.81,
            snippet="resolve delta chain for artifact pull target commit",
            metadata={},
            source="semantic",
        ),
    ]

    reranked = hybrid._post_process_results(
        results,
        limit=2,
        query="how do artifact push pull and delta resolution work",
    )

    assert reranked[0].filepath == "mcp_server/artifacts/delta_resolver.py"


def test_hybrid_post_process_symbol_precise_demotes_tests(temp_db_path):
    store = SQLiteStore(str(temp_db_path))
    hybrid = HybridSearch(storage=store, config=HybridSearchConfig())

    from mcp_server.indexer.hybrid_search import SearchResult

    results = [
        SearchResult(
            doc_id="test",
            filepath="tests/test_profile_aware_semantic_indexer.py",
            score=0.9,
            snippet="class SemanticIndexer test",
            metadata={},
            source="semantic",
        ),
        SearchResult(
            doc_id="impl",
            filepath="mcp_server/utils/semantic_indexer.py",
            score=0.82,
            snippet="class SemanticIndexer implementation",
            metadata={},
            source="semantic",
        ),
    ]

    reranked = hybrid._post_process_results(
        results,
        limit=2,
        query="class SemanticIndexer",
    )

    assert reranked[0].filepath == "mcp_server/utils/semantic_indexer.py"


def test_collect_files_excludes_generated_dirs_and_prioritizes_source(tmp_path):
    (tmp_path / "mcp_server").mkdir()
    (tmp_path / "mcp_server" / "core.py").write_text("print('core')\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "runner.py").write_text("print('runner')\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (tmp_path / "docs" / "benchmarks").mkdir(parents=True)
    (tmp_path / "docs" / "benchmarks" / "report.md").write_text("# Report\n", encoding="utf-8")
    (tmp_path / "analysis_archive").mkdir()
    (tmp_path / "analysis_archive" / "notes.md").write_text("# Notes\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_core.py").write_text(
        "def test_core():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "htmlcov").mkdir()
    (tmp_path / "htmlcov" / "status.json").write_text("{}\n", encoding="utf-8")

    files = _collect_files(tmp_path, max_files=10)
    relative_paths = [path.relative_to(tmp_path).as_posix() for path in files]

    assert "mcp_server/core.py" in relative_paths
    assert "scripts/runner.py" in relative_paths
    assert "docs/guide.md" in relative_paths
    assert "tests/test_core.py" in relative_paths
    assert "docs/benchmarks/report.md" not in relative_paths
    assert "analysis_archive/notes.md" not in relative_paths
    assert "htmlcov/status.json" not in relative_paths
    assert relative_paths.index("mcp_server/core.py") < relative_paths.index("docs/guide.md")
    assert relative_paths.index("docs/guide.md") < relative_paths.index("tests/test_core.py")
