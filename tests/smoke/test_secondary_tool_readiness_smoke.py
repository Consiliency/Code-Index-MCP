"""TOOLRDY secondary-tool smoke coverage."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from mcp_server.cli import tool_handlers
from mcp_server.indexing.summarization import GeneratedSummary, SummaryGenerationResult
from tests.fixtures.multi_repo import boot_test_server, build_temp_repo


def test_ready_registered_repository_reindexes_searches_and_persists_rows(tmp_path: Path):
    token = "toolrdy_fresh_repo_smoke_token"
    symbol = "toolrdy_fresh_symbol"
    repo_path, repo_id = build_temp_repo(
        tmp_path,
        "toolrdy_fresh_repo",
        seed_files={"seed.py": "def toolrdy_seed_symbol():\n    return 'seed'\n"},
    )
    new_file = repo_path / "fresh.py"
    new_file.write_text(f"def {symbol}():\n    return '{token}'\n", encoding="utf-8")
    subprocess.run(["git", "add", "fresh.py"], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-qm", "Committed smoke input"], cwd=repo_path, check=True)

    with boot_test_server(tmp_path, [repo_path]) as server:
        reindex = server.call_tool("reindex", {"repository": str(repo_path)})
        search = server.call_tool(
            "search_code",
            {"repository": str(repo_path), "query": token, "semantic": False},
        )
        lookup = server.call_tool(
            "symbol_lookup",
            {"repository": str(repo_path), "symbol": symbol},
        )
        status = server.call_tool("get_status", {})

        lazy_summarizer = MagicMock()
        lazy_summarizer.can_summarize.return_value = True
        lazy_summarizer._get_model_name.return_value = "test-model"
        batch_summarizer = MagicMock()
        batch_summarizer.summarize_file_chunks = AsyncMock(
            return_value=SummaryGenerationResult(
                chunks_attempted=1,
                summaries_written=1,
                authoritative_chunks=1,
                files_attempted=1,
                files_summarized=1,
                summaries=(
                    GeneratedSummary(
                        chunk_id="fresh.py:1-2:function",
                        summary="Fresh repo smoke summary",
                    ),
                ),
            )
        )
        with patch(
            "mcp_server.indexing.summarization.FileBatchSummarizer",
            return_value=batch_summarizer,
        ):
            summarize = json.loads(
                asyncio.run(
                    tool_handlers.handle_summarize_sample(
                        arguments={"repository": str(repo_path), "paths": [str(new_file)]},
                        dispatcher=server.dispatcher,
                        repo_resolver=server.repo_resolver,
                        lazy_summarizer=lazy_summarizer,
                    )
                )[0].text
            )

        store = server.store_registry.get(repo_id)
        with sqlite3.connect(store.db_path) as conn:
            durable_rows = conn.execute(
                "SELECT COUNT(*) FROM files WHERE path = ? OR relative_path = ?",
                (str(new_file), "fresh.py"),
            ).fetchone()[0]

    assert reindex["indexed_files"] >= 1
    assert reindex["durable_files"] >= 1
    assert durable_rows >= 1
    assert any("fresh.py" in result.get("file", "") for result in search)
    assert lookup["symbol"] == symbol
    assert lookup["defined_in"].endswith("fresh.py")
    assert len(status["repositories"]) == 1
    assert status["repositories"][0]["readiness"] == "ready"
    assert summarize["files_processed"] == 1
    assert summarize["persisted"] is False
    assert summarize["model_used"] == "test-model"
    assert summarize["files"][0]["file_path"] == str(new_file)
