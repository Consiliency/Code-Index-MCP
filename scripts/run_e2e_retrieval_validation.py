#!/usr/bin/env python3
"""End-to-end indexing and retrieval validation runner.

Runs lexical and semantic retrieval over this repository (or a target path),
evaluates against a fixed query suite, and emits JSON + Markdown reports.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Dict, List

from mcp_server.artifacts.semantic_profiles import SemanticProfileRegistry
from mcp_server.indexer.bm25_indexer import BM25Indexer
from mcp_server.indexer.hybrid_search import HybridSearch, HybridSearchConfig
from mcp_server.storage.sqlite_store import SQLiteStore
from mcp_server.utils.fuzzy_indexer import FuzzyIndexer
from mcp_server.utils.semantic_indexer import SemanticIndexer

#: Semantic profiles fingerprint the installed chunker, in the same form as the
#: automatic detection (``Settings._detect_treesitter_chunker_version``).
try:
    CHUNKER_VERSION = f"treesitter-chunker@{importlib.metadata.version('treesitter-chunker')}"
except importlib.metadata.PackageNotFoundError:
    CHUNKER_VERSION = "treesitter-chunker@not-installed"


INDEXABLE_EXTENSIONS = {".py", ".md", ".yml", ".yaml", ".json"}
IGNORED_DIR_NAMES = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".indexes",
    "__pycache__",
    "node_modules",
    "qdrant_storage",
    "htmlcov",
    "dist",
    "build",
    ".eggs",
    ".tox",
    "site-packages",
    "analysis_archive",
}
IGNORED_PATH_FRAGMENTS = {
    "docs/benchmarks/",
    "coverage.xml",
    ".coverage",
}


def _load_local_env_file() -> None:
    env_path = Path(".env.local")
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class QueryCase:
    mode: str
    query: str
    expected_path_fragment: str
    category: str = "general"


QUERY_SUITE = [
    QueryCase("classic", "semantic preflight", "semantic_preflight.py", "semantic"),
    QueryCase(
        "bm25",
        "qdrant docker compose autostart",
        "qdrant_autostart.py",
        "symbol_precise",
    ),
    QueryCase("fuzzy", "semntic preflite raport", "semantic_preflight.py", "noisy"),
    QueryCase(
        "semantic", "where is qdrant autostart implemented", "qdrant_autostart.py"
    ),
    QueryCase(
        "hybrid",
        "how does semantic setup validate qdrant and embedding readiness",
        "setup_commands.py",
        "semantic_intent",
    ),
    QueryCase(
        "bm25",
        "class SemanticIndexer",
        "semantic_indexer.py",
        "symbol_precise",
    ),
    QueryCase(
        "semantic",
        "extract symbols from python using treesitter",
        "semantic_indexer.py",
        "semantic_intent",
    ),
    QueryCase(
        "fuzzy",
        "SemnticIndexer index_file",
        "semantic_indexer.py",
        "noisy",
    ),
    QueryCase(
        "hybrid",
        "how do artifact push pull and delta resolution work",
        "delta_resolver.py",
        "persistence",
    ),
]


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(1.0, pct)) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _extract_file_path(result: Dict[str, object]) -> str:
    for key in ["file_path", "filepath", "file", "path", "relative_path"]:
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _collect_files(repo_path: Path, max_files: int) -> List[Path]:
    files: List[Path] = []
    impl_files: List[Path] = []
    script_files: List[Path] = []
    doc_files: List[Path] = []
    test_files: List[Path] = []

    for path in repo_path.rglob("*"):
        if any(part in IGNORED_DIR_NAMES for part in path.parts):
            continue
        if not path.is_file() or path.suffix.lower() not in INDEXABLE_EXTENSIONS:
            continue

        normalized = path.relative_to(repo_path).as_posix()
        if any(fragment in normalized for fragment in IGNORED_PATH_FRAGMENTS):
            continue

        if normalized.startswith(("mcp_server/", "src/")):
            impl_files.append(path)
        elif normalized.startswith("scripts/"):
            script_files.append(path)
        elif normalized.startswith("tests/"):
            test_files.append(path)
        else:
            doc_files.append(path)

    for bucket in (impl_files, script_files, doc_files, test_files):
        if len(files) >= max_files:
            break
        files.extend(bucket[: max_files - len(files)])
    return files[:max_files]


def _top_file(results: List[Dict[str, object]]) -> str:
    if not results:
        return ""
    return _extract_file_path(results[0])


def _top_k_match(results: List[Dict[str, object]], expected: str, k: int) -> bool:
    for result in results[:k]:
        if expected in _extract_file_path(result):
            return True
    return False


def _to_rows(
    top_file: str,
    query: str,
    mode: str,
    category: str,
    counts: List[int],
    latencies_ms: List[float],
    top1_hits: int,
    top3_hits: int,
    iterations: int,
    expected: str,
):
    return {
        "mode": mode,
        "category": category,
        "query": query,
        "expected": expected,
        "count": int(round(sum(counts) / max(len(counts), 1))),
        "top_file": top_file,
        "latency_ms": round(_percentile(latencies_ms, 0.5), 2),
        "latency_p50_ms": round(_percentile(latencies_ms, 0.5), 2),
        "latency_p95_ms": round(_percentile(latencies_ms, 0.95), 2),
        "top1_accuracy_pct": round((top1_hits / max(iterations, 1)) * 100.0, 1),
        "top3_accuracy_pct": round((top3_hits / max(iterations, 1)) * 100.0, 1),
        "pass": top1_hits > 0,
    }


def run(args: argparse.Namespace) -> Dict[str, object]:
    args.iterations = max(1, int(args.iterations))
    fireworks_api_key = (
        args.fireworks_api_key
        or os.getenv("FIREWORKS_API_KEY", "")
        or os.getenv("OPENAI_API_KEY", "")
    )
    if not fireworks_api_key:
        raise RuntimeError(
            "Missing Fireworks API key. Set --fireworks-api-key or FIREWORKS_API_KEY."
        )

    repo_path = Path(args.repo).resolve()
    files = _collect_files(repo_path, args.max_files)

    if not files:
        raise RuntimeError(f"No indexable files found at {repo_path}")

    with tempfile.TemporaryDirectory(prefix="mcp-e2e-") as td:
        store = SQLiteStore(str(Path(td) / "code_index.db"))
        repo_id = store.create_repository(str(repo_path), repo_path.name)
        bm25 = BM25Indexer(store)
        fuzzy = FuzzyIndexer(store)

        profiles = {
            "oss_high": {
                "provider": "openai_compatible",
                "model_name": args.qwen_model,
                "model_version": "e2e",
                "vector_dimension": args.qwen_dim,
                "distance_metric": "dot",
                "normalization_policy": "l2",
                "chunk_schema_version": "1",
                "chunker_version": CHUNKER_VERSION,
                "build_metadata": {"openai_api_base": args.fireworks_base},
            }
        }
        if args.enable_voyage:
            profiles["commercial_high"] = {
                "provider": "voyage",
                "model_name": args.voyage_model,
                "model_version": "3",
                "vector_dimension": args.voyage_dim,
                "distance_metric": "dot",
                "normalization_policy": "provider-default",
                "chunk_schema_version": "1",
                "chunker_version": CHUNKER_VERSION,
            }

        os.environ["OPENAI_API_BASE"] = args.fireworks_base
        os.environ["OPENAI_API_KEY"] = fireworks_api_key
        os.environ["QDRANT_URL"] = args.qdrant_url

        registry = SemanticProfileRegistry.from_raw(
            profiles, "oss_high", tool_version="e2e"
        )
        qwen_sem = SemanticIndexer(
            collection=args.qwen_collection,
            qdrant_path=args.qdrant_url,
            profile_registry=registry,
            semantic_profile="oss_high",
        )
        voyage_sem = None
        if args.enable_voyage:
            voyage_sem = SemanticIndexer(
                collection=args.voyage_collection,
                qdrant_path=args.qdrant_url,
                profile_registry=registry,
                semantic_profile="commercial_high",
            )

        semantic_files_indexed = 0
        semantic_embedding_units = 0
        semantic_fallback_files = 0
        for path in files:
            text = path.read_text(encoding="utf-8", errors="ignore")
            fid = store.store_file(
                repo_id, path, language="python" if path.suffix == ".py" else "text"
            )
            bm25.add_document(str(path), text, metadata={"language": "python"})
            fuzzy.add_file(str(path), text)
            qwen_result = qwen_sem.index_file(path)
            semantic_files_indexed += 1
            semantic_embedding_units += int(qwen_result.get("embedding_unit_count", 0))
            if qwen_result.get("used_fallback_chunks"):
                semantic_fallback_files += 1
            if voyage_sem:
                voyage_sem.index_file(path)
            # lightweight symbols for fuzzy symbol route
            for n, line in enumerate(text.splitlines(), 1):
                s = line.strip()
                if s.startswith("def ") or s.startswith("class "):
                    name = s.split()[1].split("(")[0].split(":")[0]
                    store.store_symbol(
                        file_id=fid,
                        name=name,
                        kind="function" if s.startswith("def ") else "class",
                        line_start=n,
                        line_end=n,
                        signature=s,
                        documentation="",
                        metadata={"source": "e2e"},
                    )

        hybrid = HybridSearch(
            storage=store,
            bm25_indexer=bm25,
            semantic_indexer=qwen_sem,
            fuzzy_indexer=fuzzy,
            config=HybridSearchConfig(
                enable_bm25=True,
                enable_semantic=True,
                enable_fuzzy=True,
                cache_results=False,
                final_limit=args.limit,
                individual_limit=max(args.limit, 8),
            ),
        )

        rows = []
        for case in QUERY_SUITE:
            latencies: List[float] = []
            counts: List[int] = []
            top_file = ""
            top1_hits = 0
            top3_hits = 0

            for _ in range(args.iterations):
                t0 = perf_counter()
                if case.mode == "classic":
                    results = store.search_code_fts(case.query, limit=args.limit)
                elif case.mode == "bm25":
                    results = bm25.search(case.query, limit=args.limit)
                elif case.mode == "fuzzy":
                    results = store.search_symbols_fuzzy(case.query, limit=args.limit)
                elif case.mode == "semantic":
                    results = qwen_sem.search(case.query, limit=args.limit)
                elif case.mode == "hybrid":
                    results = asyncio.run(hybrid.search(case.query, limit=args.limit))
                else:
                    continue
                elapsed = (perf_counter() - t0) * 1000.0
                latencies.append(elapsed)
                counts.append(len(results))
                if not top_file:
                    top_file = _top_file(results)
                if _top_k_match(results, case.expected_path_fragment, 1):
                    top1_hits += 1
                if _top_k_match(results, case.expected_path_fragment, 3):
                    top3_hits += 1

            rows.append(
                _to_rows(
                    top_file=top_file,
                    query=case.query,
                    mode=case.mode,
                    category=case.category,
                    counts=counts,
                    latencies_ms=latencies,
                    top1_hits=top1_hits,
                    top3_hits=top3_hits,
                    iterations=args.iterations,
                    expected=case.expected_path_fragment,
                )
            )

        model_rows = []
        if voyage_sem:
            q = "where is qdrant autostart implemented"
            qwen_lats: List[float] = []
            qwen_res: List[Dict[str, object]] = []
            for _ in range(args.iterations):
                t0 = perf_counter()
                qwen_res = qwen_sem.search(q, limit=args.limit)
                qwen_lats.append((perf_counter() - t0) * 1000.0)

            voy_lats: List[float] = []
            voy_res: List[Dict[str, object]] = []
            for _ in range(args.iterations):
                t0 = perf_counter()
                voy_res = voyage_sem.search(q, limit=args.limit)
                voy_lats.append((perf_counter() - t0) * 1000.0)

            model_rows.extend(
                [
                    _to_rows(
                        top_file=_top_file(qwen_res),
                        query=q,
                        mode="semantic_qwen",
                        category="provider_latency",
                        counts=[len(qwen_res)] * max(args.iterations, 1),
                        latencies_ms=qwen_lats,
                        top1_hits=sum(
                            1
                            for _ in qwen_lats
                            if _top_k_match(qwen_res, "qdrant_autostart.py", 1)
                        ),
                        top3_hits=sum(
                            1
                            for _ in qwen_lats
                            if _top_k_match(qwen_res, "qdrant_autostart.py", 3)
                        ),
                        iterations=args.iterations,
                        expected="qdrant_autostart.py",
                    ),
                    _to_rows(
                        top_file=_top_file(voy_res),
                        query=q,
                        mode="semantic_voyage",
                        category="provider_latency",
                        counts=[len(voy_res)] * max(args.iterations, 1),
                        latencies_ms=voy_lats,
                        top1_hits=sum(
                            1
                            for _ in voy_lats
                            if _top_k_match(voy_res, "qdrant_autostart.py", 1)
                        ),
                        top3_hits=sum(
                            1
                            for _ in voy_lats
                            if _top_k_match(voy_res, "qdrant_autostart.py", 3)
                        ),
                        iterations=args.iterations,
                        expected="qdrant_autostart.py",
                    ),
                ]
            )

        if args.local_openai_base:
            local_profiles = {
                "oss_high_local": {
                    "provider": "openai_compatible",
                    "model_name": args.local_qwen_model or args.qwen_model,
                    "model_version": "e2e-local",
                    "vector_dimension": args.qwen_dim,
                    "distance_metric": "dot",
                    "normalization_policy": "l2",
                    "chunk_schema_version": "1",
                    "chunker_version": CHUNKER_VERSION,
                    "build_metadata": {
                        "openai_api_base": args.local_openai_base,
                        "openai_api_key": args.local_openai_key or fireworks_api_key,
                    },
                }
            }
            local_registry = SemanticProfileRegistry.from_raw(
                local_profiles,
                "oss_high_local",
                tool_version="e2e-local",
            )
            qwen_sem_local = SemanticIndexer(
                collection=args.qwen_collection,
                qdrant_path=args.qdrant_url,
                profile_registry=local_registry,
                semantic_profile="oss_high_local",
            )

            q = "where is qdrant autostart implemented"
            local_lats: List[float] = []
            local_res: List[Dict[str, object]] = []
            for _ in range(args.iterations):
                t0 = perf_counter()
                local_res = qwen_sem_local.search(q, limit=args.limit)
                local_lats.append((perf_counter() - t0) * 1000.0)

            model_rows.append(
                _to_rows(
                    top_file=_top_file(local_res),
                    query=q,
                    mode="semantic_qwen_local",
                    category="provider_latency",
                    counts=[len(local_res)] * max(args.iterations, 1),
                    latencies_ms=local_lats,
                    top1_hits=sum(
                        1
                        for _ in local_lats
                        if _top_k_match(local_res, "qdrant_autostart.py", 1)
                    ),
                    top3_hits=sum(
                        1
                        for _ in local_lats
                        if _top_k_match(local_res, "qdrant_autostart.py", 3)
                    ),
                    iterations=args.iterations,
                    expected="qdrant_autostart.py",
                )
            )

        summary = {
            "repo": str(repo_path),
            "indexed_files": len(files),
            "semantic_files_indexed": semantic_files_indexed,
            "semantic_embedding_units": semantic_embedding_units,
            "semantic_fallback_files": semantic_fallback_files,
            "semantic_skipped_files": max(0, len(files) - semantic_files_indexed),
            "qdrant_url": args.qdrant_url,
            "fireworks_base": args.fireworks_base,
            "results": rows,
            "semantic_models": model_rows,
            "iterations": args.iterations,
            "pass_rate": round(
                sum(float(r.get("top1_accuracy_pct", 0.0)) for r in rows)
                / max(len(rows), 1),
                1,
            ),
            "top3_pass_rate": round(
                sum(float(r.get("top3_accuracy_pct", 0.0)) for r in rows)
                / max(len(rows), 1),
                1,
            ),
        }
        return summary


def write_markdown(result: Dict[str, object], output: Path) -> None:
    lines = [
        "# E2E Retrieval Validation",
        "",
        f"- Repo: `{result['repo']}`",
        f"- Indexed files: `{result['indexed_files']}`",
        f"- Semantic files indexed: `{result.get('semantic_files_indexed', 0)}`",
        f"- Semantic embedding units: `{result.get('semantic_embedding_units', 0)}`",
        f"- Semantic fallback files: `{result.get('semantic_fallback_files', 0)}`",
        f"- Iterations per query: `{result.get('iterations', 1)}`",
        f"- Top-1 pass rate: `{result['pass_rate']}%`",
        f"- Top-3 pass rate: `{result.get('top3_pass_rate', 0)}%`",
        "",
        "## Retrieval Modes",
        "",
        "| Mode | Category | Query | Top File | Top-1 | Top-3 | P50 (ms) | P95 (ms) |",
        "|---|---|---|---|---:|---:|---:|---:|",
    ]
    result_rows = result.get("results", [])
    if isinstance(result_rows, list):
        for row in result_rows:
            lines.append(
                f"| {row['mode']} | {row.get('category', 'general')} | {row['query']} | {row['top_file']} | {row.get('top1_accuracy_pct', 0)}% | {row.get('top3_accuracy_pct', 0)}% | {row.get('latency_p50_ms', row['latency_ms'])} | {row.get('latency_p95_ms', row['latency_ms'])} |"
            )

    semantic_models = result.get("semantic_models") or []
    if isinstance(semantic_models, list) and semantic_models:
        lines.extend(
            [
                "",
                "## Semantic Model Comparison",
                "",
                "| Mode | Top File | Top-1 | Top-3 | P50 (ms) | P95 (ms) |",
                "|---|---|---:|---:|---:|---:|",
            ]
        )
        for row in semantic_models:
            lines.append(
                f"| {row['mode']} | {row['top_file']} | {row.get('top1_accuracy_pct', 0)}% | {row.get('top3_accuracy_pct', 0)}% | {row.get('latency_p50_ms', row['latency_ms'])} | {row.get('latency_p95_ms', row['latency_ms'])} |"
            )

    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    _load_local_env_file()
    parser = argparse.ArgumentParser(description="Run MCP retrieval E2E validation")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--max-files", type=int, default=200)
    parser.add_argument("--semantic-max-chars", type=int, default=24000)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--qdrant-url", default="http://localhost:6333")
    parser.add_argument(
        "--fireworks-base",
        "--openai-base",
        dest="fireworks_base",
        default=os.getenv("FIREWORKS_BASE", "https://api.fireworks.ai/inference/v1"),
    )
    parser.add_argument(
        "--fireworks-api-key",
        "--openai-key",
        dest="fireworks_api_key",
        default="",
    )
    parser.add_argument("--enable-voyage", action="store_true")
    parser.add_argument("--voyage-model", default="voyage-code-3")
    parser.add_argument("--voyage-dim", type=int, default=2048)
    parser.add_argument("--qwen-model", default="Qwen/Qwen3-Embedding-8B")
    parser.add_argument("--qwen-dim", type=int, default=4096)
    parser.add_argument(
        "--local-openai-base", default=os.getenv("LOCAL_OPENAI_BASE", "")
    )
    parser.add_argument("--local-openai-key", default=os.getenv("LOCAL_OPENAI_KEY", ""))
    parser.add_argument("--local-qwen-model", default="")
    parser.add_argument("--qwen-collection", default="code-index-e2e-oss-high")
    parser.add_argument("--voyage-collection", default="code-index-e2e-commercial-high")
    parser.add_argument(
        "--json-out", default="docs/benchmarks/e2e_retrieval_validation.json"
    )
    parser.add_argument(
        "--md-out", default="docs/benchmarks/e2e_retrieval_validation.md"
    )
    args = parser.parse_args()

    result = run(args)

    json_path = Path(args.json_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    md_path = Path(args.md_out)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    write_markdown(result, md_path)

    print(
        json.dumps(
            {
                "json": str(json_path),
                "markdown": str(md_path),
                "pass_rate": result["pass_rate"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
