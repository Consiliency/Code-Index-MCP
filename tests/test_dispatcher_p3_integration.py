"""P3 SL-6 integration tests — dispatcher consumes PluginSetRegistry + SemanticIndexerRegistry.

These tests are RED until SL-6.2 rewires dispatcher_enhanced.py.
"""

from __future__ import annotations

import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_server.core.repo_context import RepoContext
from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher
from mcp_server.plugins.plugin_set_registry import PluginSetRegistry
from mcp_server.storage.multi_repo_manager import RepositoryInfo
from mcp_server.utils.semantic_indexer_registry import SemanticIndexerRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo_info(repo_id: str) -> RepositoryInfo:
    from datetime import datetime

    return RepositoryInfo(
        repository_id=repo_id,
        name=repo_id,
        path=Path("/tmp/fake"),
        index_path=Path("/tmp/fake/.index"),
        language_stats={},
        total_files=0,
        total_symbols=0,
        indexed_at=datetime(2024, 1, 1),
    )


def _make_ctx(repo_id: str) -> RepoContext:
    """Minimal RepoContext for testing."""
    return RepoContext(
        repo_id=repo_id,
        sqlite_store=None,
        workspace_root=Path("/tmp/fake"),
        tracked_branch="main",
        registry_entry=_make_repo_info(repo_id),
    )


def _make_plugin_registry() -> tuple[PluginSetRegistry, MagicMock]:
    """Return a PluginSetRegistry stub and a tracking mock."""
    registry = MagicMock(spec=PluginSetRegistry)
    registry.plugins_for.return_value = []
    registry.plugins_for_file.return_value = []
    return registry, registry


def _make_semantic_registry() -> tuple[MagicMock, MagicMock]:
    """Return a SemanticIndexerRegistry stub and a tracking mock."""
    registry = MagicMock(spec=SemanticIndexerRegistry)
    fake_indexer = MagicMock()
    fake_indexer.search.return_value = []
    registry.get.return_value = fake_indexer
    registry.lease.side_effect = lambda *args, **kwargs: nullcontext(fake_indexer)
    return registry, registry


# ---------------------------------------------------------------------------
# SL-6.1-a: constructor accepts registry kwargs
# ---------------------------------------------------------------------------


class TestEnhancedDispatcherAcceptsRegistries:
    def test_accepts_plugin_set_registry_kwarg(self):
        reg, _ = _make_plugin_registry()
        d = EnhancedDispatcher(plugin_set_registry=reg)
        assert d is not None

    def test_accepts_semantic_indexer_registry_kwarg(self):
        sem_reg, _ = _make_semantic_registry()
        d = EnhancedDispatcher(semantic_indexer_registry=sem_reg)
        assert d is not None

    def test_accepts_both_registry_kwargs(self):
        reg, _ = _make_plugin_registry()
        sem_reg, _ = _make_semantic_registry()
        d = EnhancedDispatcher(plugin_set_registry=reg, semantic_indexer_registry=sem_reg)
        assert d is not None

    def test_no_registries_builds_defaults(self):
        # Backward compat: dispatcher constructs its own defaults when kwargs absent
        d = EnhancedDispatcher()
        assert d is not None


# ---------------------------------------------------------------------------
# SL-6.1-b: search routes through plugin_set_registry
# ---------------------------------------------------------------------------


class TestSearchRoutesViaPluginSetRegistry:
    def test_search_calls_plugins_for_with_repo_id(self):
        reg, mock_reg = _make_plugin_registry()
        sem_reg, _ = _make_semantic_registry()
        d = EnhancedDispatcher(
            plugin_set_registry=reg,
            semantic_indexer_registry=sem_reg,
        )
        ctx_a = _make_ctx("repo-a")
        list(d.search(ctx_a, "some_query"))
        mock_reg.plugins_for.assert_called_with("repo-a")

    def test_search_different_repos_call_plugins_for_with_their_repo_id(self):
        reg, mock_reg = _make_plugin_registry()
        sem_reg, _ = _make_semantic_registry()
        d = EnhancedDispatcher(
            plugin_set_registry=reg,
            semantic_indexer_registry=sem_reg,
        )
        ctx_a = _make_ctx("repo-a")
        ctx_b = _make_ctx("repo-b")
        list(d.search(ctx_a, "q"))
        list(d.search(ctx_b, "q"))

        calls = [call.args[0] for call in mock_reg.plugins_for.call_args_list]
        assert "repo-a" in calls
        assert "repo-b" in calls


# ---------------------------------------------------------------------------
# SL-6.1-c: semantic search routes through semantic_indexer_registry
# ---------------------------------------------------------------------------


class TestSearchRoutesViaSemanticRegistry:
    def test_semantic_search_calls_registry_get_with_repo_id(self):
        reg, _ = _make_plugin_registry()
        sem_reg, mock_sem = _make_semantic_registry()
        d = EnhancedDispatcher(
            plugin_set_registry=reg,
            semantic_indexer_registry=sem_reg,
        )
        ctx_a = _make_ctx("repo-a")
        list(d.search(ctx_a, "some query", semantic=True))
        mock_sem.lease.assert_called_with("repo-a", ctx=ctx_a)

    def test_semantic_search_different_repos_use_distinct_registry_gets(self):
        reg, _ = _make_plugin_registry()
        sem_reg, mock_sem = _make_semantic_registry()
        d = EnhancedDispatcher(
            plugin_set_registry=reg,
            semantic_indexer_registry=sem_reg,
        )
        ctx_a = _make_ctx("repo-a")
        ctx_b = _make_ctx("repo-b")
        list(d.search(ctx_a, "q", semantic=True))
        list(d.search(ctx_b, "q", semantic=True))

        calls = [call.args[0] for call in mock_sem.lease.call_args_list]
        assert "repo-a" in calls
        assert "repo-b" in calls


# ---------------------------------------------------------------------------
# SL-6.1-d: grep-assert — no residual singleton references
# ---------------------------------------------------------------------------


class TestNoResidualSingletonReferences:
    def test_dispatcher_has_no_direct_plugin_singleton_access(self):
        """rg must find zero hits for self._plugins, self._by_lang, self._semantic_indexer."""
        dispatcher_path = (
            Path(__file__).parent.parent / "mcp_server" / "dispatcher" / "dispatcher_enhanced.py"
        )
        result = subprocess.run(
            [
                "rg",
                "--pcre2",
                "-nE",
                r"self\.(_plugins\b|_by_lang\b|_semantic_indexer\b)",
                str(dispatcher_path),
            ],
            capture_output=True,
            text=True,
        )
        # exit 1 = no matches (good); exit 0 = matches found (bad); exit 2 = error
        if result.returncode == 2:
            # rg not available or error — fall back to python search
            text = dispatcher_path.read_text()
            import re

            hits = re.findall(r"self\.(_plugins\b|_by_lang\b|_semantic_indexer\b)", text)
            assert not hits, f"Found residual singleton references: {hits}"
            return
        assert result.returncode == 1, (
            f"Found residual singleton references in dispatcher_enhanced.py:\n" f"{result.stdout}"
        )
