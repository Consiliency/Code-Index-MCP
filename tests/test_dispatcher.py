"""
Comprehensive tests for the Dispatcher component.

Tests cover:
- Plugin registration and management
- Symbol lookup and caching
- Search functionality
- File indexing with caching
- Error handling and edge cases
"""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from mcp_server.core.errors import TransientArtifactError
from mcp_server.core.repo_context import RepoContext
from mcp_server.dispatcher import EnhancedDispatcher as Dispatcher

# ---------------------------------------------------------------------------
# SL-1.1 imports (new Protocol-conformance tests)
# ---------------------------------------------------------------------------
from mcp_server.dispatcher.dispatcher_enhanced import (
    _EXACT_BOUNDED_PYTHON_PATHS,
    IndexResult,
    IndexResultStatus,
    SemanticSearchFailure,
)
from mcp_server.dispatcher.protocol import DispatcherProtocol
from mcp_server.dispatcher.query_intent import QueryIntent, classify
from mcp_server.dispatcher.simple_dispatcher import SimpleDispatcher
from mcp_server.plugin_base import IPlugin, SearchResult, SymbolDef
from tests.conftest import measure_time


class TestDispatcherInitialization:
    """Test Dispatcher initialization and configuration."""

    def test_init_with_single_plugin(self, mock_plugin):
        """Test initialization with a single plugin."""
        dispatcher = Dispatcher([mock_plugin])

        assert len(dispatcher.plugins()) == 1
        assert mock_plugin in dispatcher.plugins()
        assert dispatcher._file_cache == {}

    def test_init_with_multiple_plugins(self):
        """Test initialization with multiple plugins."""
        plugin1 = Mock(spec=IPlugin, lang="python")
        plugin2 = Mock(spec=IPlugin, lang="javascript")
        plugin3 = Mock(spec=IPlugin, lang="java")

        dispatcher = Dispatcher([plugin1, plugin2, plugin3])

        assert len(dispatcher.plugins()) == 3
        assert plugin1 in dispatcher.plugins()
        assert plugin2 in dispatcher.plugins()
        assert plugin3 in dispatcher.plugins()

    def test_init_empty_plugins(self):
        """Test initialization with no plugins."""
        dispatcher = Dispatcher([])

        assert len(dispatcher.plugins()) == 0

    def test_plugins_method(self, mock_plugin):
        """Test the plugins() method returns list of plugins."""
        dispatcher = Dispatcher([mock_plugin])

        plugins = dispatcher.plugins()
        assert isinstance(plugins, list)
        assert mock_plugin in plugins

    @patch("mcp_server.dispatcher.dispatcher_enhanced.MultiRepositoryManager")
    @patch("mcp_server.dispatcher.dispatcher_enhanced.CrossRepositorySearchCoordinator")
    @patch("pathlib.Path.home")
    def test_multi_repo_default_registry_path_delegates_to_shared_manager_policy(
        self, mock_home, mock_cross_repo, mock_multi_repo
    ):
        """The manager owns MCP_REPO_REGISTRY/default resolution for every entrypoint."""
        mock_home.return_value = Path("/tmp/test-home")

        with patch.dict("os.environ", {"MCP_ENABLE_MULTI_REPO": "true"}, clear=False):
            Dispatcher([])

        registry_path = mock_multi_repo.call_args.kwargs["central_index_path"]
        assert registry_path is None


class TestPluginMatching:
    """Test plugin matching for files."""

    def test_match_plugin_success(self):
        """Test successful plugin matching."""
        py_plugin = Mock(spec=IPlugin, lang="python")
        py_plugin.supports.return_value = True

        js_plugin = Mock(spec=IPlugin, lang="javascript")
        js_plugin.supports.return_value = False

        dispatcher = Dispatcher([py_plugin, js_plugin])

        result = dispatcher._match_plugin(Path("test.py"))
        assert result == py_plugin
        py_plugin.supports.assert_called_with(Path("test.py"))

    def test_match_plugin_no_match(self, mock_plugin):
        """Test when no plugin matches the file."""
        mock_plugin.supports.return_value = False
        dispatcher = Dispatcher([mock_plugin])

        with pytest.raises(RuntimeError, match="No plugin"):
            dispatcher._match_plugin(Path("test.unknown"))

    def test_match_plugin_multiple_matches(self):
        """Test when multiple plugins match (first wins)."""
        plugin1 = Mock(spec=IPlugin, lang="plugin1")
        plugin1.supports.return_value = True

        plugin2 = Mock(spec=IPlugin, lang="plugin2")
        plugin2.supports.return_value = True

        dispatcher = Dispatcher([plugin1, plugin2])

        result = dispatcher._match_plugin(Path("test.file"))
        assert result == plugin1  # First matching plugin wins


class TestSymbolLookup:
    """Test symbol lookup functionality (first-hit semantics via run_gated_fallback)."""

    def test_lookup_found(self, mock_plugin):
        """Test successful symbol lookup — first-truthy-hit semantics (IF-0-P11-1)."""
        from mcp_server.dispatcher.fallback import run_gated_fallback

        expected_symbol = SymbolDef(name="test_func", kind="function", path="/test.py", line=10)
        mock_plugin.getDefinition.return_value = expected_symbol

        result = run_gated_fallback([mock_plugin], "test_func", source_ext=".py", timeout_ms=1000)

        assert result == expected_symbol
        mock_plugin.getDefinition.assert_called_once_with("test_func")

    def test_lookup_not_found(self, mock_plugin):
        """Test symbol lookup when not found — run_gated_fallback returns None."""
        from mcp_server.dispatcher.fallback import run_gated_fallback

        mock_plugin.getDefinition.return_value = None

        result = run_gated_fallback([mock_plugin], "nonexistent", source_ext=".py", timeout_ms=1000)

        assert result is None
        mock_plugin.getDefinition.assert_called_once_with("nonexistent")

    def test_lookup_multiple_plugins(self):
        """Test lookup across multiple plugins — stops at first truthy hit (IF-0-P11-1)."""
        from mcp_server.dispatcher.fallback import run_gated_fallback

        plugin1 = Mock(spec=IPlugin, lang="python")
        plugin1.getDefinition.return_value = None

        expected_symbol = SymbolDef(name="found", kind="class", path="/found.js", line=5)
        plugin2 = Mock(spec=IPlugin, lang="javascript")
        plugin2.getDefinition.return_value = expected_symbol

        plugin3 = Mock(spec=IPlugin, lang="java")
        plugin3.getDefinition.return_value = None

        result = run_gated_fallback(
            [plugin1, plugin2, plugin3], "found", source_ext=".py", timeout_ms=1000
        )

        assert result == expected_symbol
        plugin1.getDefinition.assert_called_once_with("found")
        plugin2.getDefinition.assert_called_once_with("found")
        # plugin3 is NOT called since plugin2 returned a truthy hit
        plugin3.getDefinition.assert_not_called()

    def test_lookup_plugin_error(self, mock_plugin):
        """Test lookup when plugin raises error."""
        mock_plugin.getDefinition.side_effect = Exception("Plugin error")

        ctx = _make_repo_ctx()
        dispatcher = Dispatcher([mock_plugin])

        # Dispatcher catches plugin errors and returns None
        result = dispatcher.lookup(ctx, "test")
        assert result is None


def _make_psr_with_plugins(repo_id: str, plugins: list):
    """Build a PluginSetRegistry pre-seeded with *plugins* for *repo_id*.

    This bypasses MemoryAwarePluginManager so mock plugins are visible to
    the dispatcher's plugin-fallback search path (first-hit semantics, SL-4).
    """
    from mcp_server.plugins.plugin_set_registry import PluginSetRegistry

    psr = PluginSetRegistry()
    psr._cache[repo_id] = list(plugins)
    return psr


class TestSearch:
    """Test search functionality."""

    _REPO_ID = "test-repo"

    def _make_no_sqlite_ctx(self):
        """Build a RepoContext with no sqlite_store so dispatcher falls back to plugins."""
        from pathlib import Path
        from unittest.mock import MagicMock

        from mcp_server.core.repo_context import RepoContext
        from mcp_server.storage.multi_repo_manager import RepositoryInfo

        registry_entry = MagicMock(spec=RepositoryInfo)
        registry_entry.tracked_branch = "main"
        return RepoContext(
            repo_id=self._REPO_ID,
            sqlite_store=None,
            workspace_root=Path("/tmp/test"),
            tracked_branch="main",
            registry_entry=registry_entry,
        )

    def test_search_basic(self, mock_plugin):
        """Test basic search across plugins when no sqlite_store (plugin fallback path)."""
        expected_results = [
            SearchResult(name="func1", kind="function", path="/f1.py", score=0.9),
            SearchResult(name="func2", kind="function", path="/f2.py", score=0.8),
        ]
        mock_plugin.search.return_value = expected_results

        ctx = self._make_no_sqlite_ctx()
        psr = _make_psr_with_plugins(self._REPO_ID, [mock_plugin])
        dispatcher = Dispatcher([mock_plugin], plugin_set_registry=psr)
        results = list(dispatcher.search(ctx, "func"))

        assert results == expected_results
        mock_plugin.search.assert_called_once_with("func", {"semantic": False, "limit": 20})

    def test_search_semantic(self, mock_plugin):
        """Test semantic search falls back to plugins when no sqlite/semantic indexer."""
        mock_plugin.search.return_value = []

        ctx = self._make_no_sqlite_ctx()
        psr = _make_psr_with_plugins(self._REPO_ID, [mock_plugin])
        dispatcher = Dispatcher([mock_plugin], plugin_set_registry=psr)
        dispatcher._semantic_indexer_fallback = None
        list(dispatcher.search(ctx, "test", semantic=True, limit=10))

        mock_plugin.search.assert_called_once_with("test", {"semantic": True, "limit": 10})

    def test_search_registered_semantic_returns_semantic_metadata(self):
        store = MagicMock()
        ctx = _make_repo_ctx(store)
        semantic_indexer = MagicMock()
        semantic_indexer.search.return_value = [
            {
                "relative_path": "mcp_server/utils/semantic_indexer.py",
                "line": 42,
                "snippet": "class SemanticIndexer:",
                "score": 0.84,
                "semantic_source": "semantic",
                "semantic_profile_id": "oss_high",
                "semantic_collection_name": "code_index__oss_high__v1",
            }
        ]

        dispatcher = Dispatcher([])
        dispatcher._semantic_registry = MagicMock(get=MagicMock(return_value=semantic_indexer))
        dispatcher._semantic_registry.lease.return_value.__enter__.return_value = semantic_indexer

        results = list(dispatcher.search(ctx, "class SemanticIndexer", semantic=True, limit=5))

        assert results[0]["file"] == "mcp_server/utils/semantic_indexer.py"
        assert results[0]["semantic_source"] == "semantic"
        assert results[0]["semantic_profile_id"] == "oss_high"
        assert results[0]["semantic_collection_name"] == "code_index__oss_high__v1"
        store.search_bm25.assert_not_called()

    def test_search_registered_semantic_failure_does_not_fall_back_to_bm25(self):
        store = MagicMock()
        store.search_bm25.return_value = [
            {"filepath": "docs/guides/semantic-onboarding.md", "score": -1.0, "snippet": "docs"}
        ]
        ctx = _make_repo_ctx(store)
        semantic_indexer = MagicMock()
        semantic_indexer.search.side_effect = RuntimeError("qdrant offline")
        semantic_indexer.semantic_profile = SimpleNamespace(profile_id="oss_high")
        semantic_indexer.collection = "code_index__oss_high__v1"

        dispatcher = Dispatcher([])
        dispatcher._semantic_registry = MagicMock(get=MagicMock(return_value=semantic_indexer))
        dispatcher._semantic_registry.lease.return_value.__enter__.return_value = semantic_indexer

        with pytest.raises(SemanticSearchFailure, match="Semantic search failed"):
            list(dispatcher.search(ctx, "class SemanticIndexer", semantic=True, limit=5))

        store.search_bm25.assert_not_called()

    def test_search_lexical_false_path_remains_unchanged_when_semantic_available(self):
        store = MagicMock()
        store.search_bm25.return_value = [
            {
                "filepath": "mcp_server/utils/semantic_indexer.py",
                "score": -2.0,
                "snippet": "class SemanticIndexer",
                "language": "python",
            }
        ]
        store.find_best_chunk_for_file.return_value = None
        ctx = _make_repo_ctx(store)
        semantic_indexer = MagicMock()

        dispatcher = Dispatcher([])
        dispatcher._semantic_registry = MagicMock(get=MagicMock(return_value=semantic_indexer))
        dispatcher._semantic_registry.lease.return_value.__enter__.return_value = semantic_indexer

        results = list(
            dispatcher.search(ctx, "semantic preflight validation", semantic=False, limit=5)
        )

        assert results[0]["file"] == "mcp_server/utils/semantic_indexer.py"
        semantic_indexer.search.assert_not_called()
        store.search_bm25.assert_called()

    def test_search_multiple_plugins(self):
        """Test search results from multiple plugins are combined (first-hit plugin path)."""
        plugin1 = Mock(spec=IPlugin, lang="python")
        plugin1.search.return_value = [
            SearchResult(name="py_func", kind="function", path="/test.py", score=0.9)
        ]

        plugin2 = Mock(spec=IPlugin, lang="javascript")
        plugin2.search.return_value = [
            SearchResult(name="js_func", kind="function", path="/test.js", score=0.8),
            SearchResult(name="js_class", kind="class", path="/test2.js", score=0.7),
        ]

        from pathlib import Path
        from unittest.mock import MagicMock

        from mcp_server.core.repo_context import RepoContext
        from mcp_server.storage.multi_repo_manager import RepositoryInfo

        registry_entry = MagicMock(spec=RepositoryInfo)
        registry_entry.tracked_branch = "main"
        ctx = RepoContext(
            repo_id=self._REPO_ID,
            sqlite_store=None,
            workspace_root=Path("/tmp/test"),
            tracked_branch="main",
            registry_entry=registry_entry,
        )

        psr = _make_psr_with_plugins(self._REPO_ID, [plugin1, plugin2])
        dispatcher = Dispatcher([plugin1, plugin2], plugin_set_registry=psr)
        results = list(dispatcher.search(ctx, "test"))

        assert len(results) == 3
        assert results[0]["name"] == "py_func"
        assert results[1]["name"] == "js_func"
        assert results[2]["name"] == "js_class"

    def test_search_empty_query(self, mock_plugin):
        """Test search with empty query."""
        mock_plugin.search.return_value = []

        ctx = self._make_no_sqlite_ctx()
        psr = _make_psr_with_plugins(self._REPO_ID, [mock_plugin])
        dispatcher = Dispatcher([mock_plugin], plugin_set_registry=psr)
        results = list(dispatcher.search(ctx, ""))

        assert results == []
        mock_plugin.search.assert_called_once()

    def test_search_plugin_error(self, mock_plugin):
        """Test search when plugin raises error."""
        mock_plugin.search.side_effect = Exception("Search error")

        ctx = self._make_no_sqlite_ctx()
        psr = _make_psr_with_plugins(self._REPO_ID, [mock_plugin])
        dispatcher = Dispatcher([mock_plugin], plugin_set_registry=psr)

        # Dispatcher catches plugin errors and returns empty results
        results = list(dispatcher.search(ctx, "test"))
        assert results == []


class TestFileHashing:
    """Test file content hashing functionality."""

    def test_get_file_hash(self, mock_plugin):
        """Test file hash calculation."""
        dispatcher = Dispatcher([mock_plugin])

        content = "def hello():\n    print('world')\n"
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        result = dispatcher._get_file_hash(content)
        assert result == expected_hash

    def test_get_file_hash_unicode(self, mock_plugin):
        """Test file hash with unicode content."""
        dispatcher = Dispatcher([mock_plugin])

        content = "def 你好():\n    print('世界')\n"
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        result = dispatcher._get_file_hash(content)
        assert result == expected_hash

    def test_get_file_hash_empty(self, mock_plugin):
        """Test file hash for empty content."""
        dispatcher = Dispatcher([mock_plugin])

        content = ""
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        result = dispatcher._get_file_hash(content)
        assert result == expected_hash


class TestCacheLogic:
    """Test file caching logic."""

    def test_should_reindex_new_file(self, mock_plugin):
        """Test that new files should be indexed."""
        dispatcher = Dispatcher([mock_plugin])

        path = Path("/test/new_file.py")
        content = "print('hello')"

        assert dispatcher._should_reindex(path, content) is True

    def test_should_reindex_modified_file(self, mock_plugin):
        """Test that modified files should be reindexed."""
        dispatcher = Dispatcher([mock_plugin])

        path = Path("/test/file.py")
        old_content = "print('hello')"
        new_content = "print('hello world')"

        # First index
        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value.st_mtime = 1000.0
            mock_stat.return_value.st_size = len(old_content)

            assert dispatcher._should_reindex(path, old_content) is True

            # Update cache manually
            dispatcher._file_cache[str(path)] = (
                1000.0,
                len(old_content),
                dispatcher._get_file_hash(old_content),
            )

        # Second index with different content
        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value.st_mtime = 2000.0
            mock_stat.return_value.st_size = len(new_content)

            assert dispatcher._should_reindex(path, new_content) is True

    def test_should_not_reindex_unchanged_file(self, mock_plugin):
        """Test that unchanged files are not reindexed."""
        dispatcher = Dispatcher([mock_plugin])

        path = Path("/test/file.py")
        content = "print('hello')"
        content_hash = dispatcher._get_file_hash(content)

        # Set up cache
        dispatcher._file_cache[str(path)] = (1000.0, len(content), content_hash)

        # Same mtime and size
        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value.st_mtime = 1000.0
            mock_stat.return_value.st_size = len(content)

            assert dispatcher._should_reindex(path, content) is False

    def test_should_reindex_size_changed_content_same(self, mock_plugin):
        """Test when size changes but content hash is same (rare edge case)."""
        dispatcher = Dispatcher([mock_plugin])

        path = Path("/test/file.py")
        content = "print('hello')"
        content_hash = dispatcher._get_file_hash(content)

        # Set up cache with different size
        dispatcher._file_cache[str(path)] = (1000.0, 999, content_hash)

        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value.st_mtime = 2000.0
            mock_stat.return_value.st_size = len(content)

            # Should not reindex if hash is same
            assert dispatcher._should_reindex(path, content) is False

    def test_should_reindex_stat_error(self, mock_plugin):
        """Test when file stat fails."""
        dispatcher = Dispatcher([mock_plugin])

        path = Path("/test/file.py")
        content = "print('hello')"

        with patch.object(Path, "stat") as mock_stat:
            mock_stat.side_effect = OSError("File not found")

            assert dispatcher._should_reindex(path, content) is True


class TestIndexFile:
    """Test file indexing functionality."""

    def test_index_file_success(self, mock_plugin, tmp_path):
        """Test successful file indexing."""
        dispatcher = Dispatcher([mock_plugin], use_plugin_factory=False, lazy_load=False)

        # Create test file
        test_file = tmp_path / "test.py"
        test_file.write_text("def hello(): pass")

        mock_plugin.supports.return_value = True
        mock_plugin.indexFile.return_value = {"symbols": [{"name": "hello", "kind": "function"}]}

        ctx = _make_repo_ctx(workspace_root=tmp_path)
        dispatcher.index_file(ctx, test_file)

        mock_plugin.indexFile.assert_called_once()
        assert str(test_file) in dispatcher._file_cache

    def test_index_file_unicode_decode_error(self, mock_plugin, tmp_path):
        """Test indexing file with encoding issues."""
        dispatcher = Dispatcher([mock_plugin], use_plugin_factory=False, lazy_load=False)

        test_file = tmp_path / "test.py"
        # Write binary data that's not valid UTF-8
        test_file.write_bytes(b"\xff\xfe invalid utf-8")

        mock_plugin.supports.return_value = True

        # Should try latin-1 encoding
        ctx = _make_repo_ctx(workspace_root=tmp_path)
        dispatcher.index_file(ctx, test_file)

        # Plugin should be called with latin-1 decoded content
        mock_plugin.indexFile.assert_called_once()

    def test_index_file_read_error(self, mock_plugin):
        """Test indexing when file read fails."""
        dispatcher = Dispatcher([mock_plugin])

        path = Path("/nonexistent/file.py")
        mock_plugin.supports.return_value = True

        # Should not raise, just log error
        ctx = _make_repo_ctx()
        dispatcher.index_file(ctx, path)

        mock_plugin.indexFile.assert_not_called()

    def test_index_file_no_plugin_match(self, mock_plugin, tmp_path):
        """Test indexing file with no matching plugin."""
        dispatcher = Dispatcher([mock_plugin])

        test_file = tmp_path / "test.unknown"
        test_file.write_text("unknown content")

        mock_plugin.supports.return_value = False

        # Should not raise, just log debug
        ctx = _make_repo_ctx()
        dispatcher.index_file(ctx, test_file)

        mock_plugin.indexFile.assert_not_called()

    def test_index_file_plugin_error(self, mock_plugin, tmp_path):
        """Test indexing when plugin raises error."""
        dispatcher = Dispatcher([mock_plugin])

        test_file = tmp_path / "test.py"
        test_file.write_text("def hello(): pass")

        mock_plugin.supports.return_value = True
        mock_plugin.indexFile.side_effect = Exception("Plugin error")

        # Should not raise, just log error
        ctx = _make_repo_ctx()
        dispatcher.index_file(ctx, test_file)

        # File should not be cached on error
        assert str(test_file) not in dispatcher._file_cache

    def test_index_file_skip_cached(self, mock_plugin, tmp_path):
        """Test that cached files are skipped."""
        dispatcher = Dispatcher([mock_plugin], use_plugin_factory=False, lazy_load=False)

        test_file = tmp_path / "test.py"
        content = "def hello(): pass"
        test_file.write_text(content)

        # Pre-populate cache
        stat = test_file.stat()
        dispatcher._file_cache[str(test_file)] = (
            stat.st_mtime,
            stat.st_size,
            dispatcher._get_file_hash(content),
        )

        mock_plugin.supports.return_value = True

        ctx = _make_repo_ctx()
        dispatcher.index_file(ctx, test_file)

        # Should not call plugin for cached file
        mock_plugin.indexFile.assert_not_called()


class TestStatistics:
    """Test statistics gathering."""

    def test_get_statistics_empty(self, mock_plugin):
        """Test statistics with no indexed files."""
        ctx = _make_repo_ctx()
        dispatcher = Dispatcher([mock_plugin])

        stats = dispatcher.get_statistics(ctx)

        assert stats["total"] == 0
        assert stats["by_language"] == {}

    def test_get_statistics_with_files(self):
        """Test statistics with indexed files."""
        py_plugin = Mock(spec=IPlugin, lang="python")
        js_plugin = Mock(spec=IPlugin, lang="javascript")

        ctx = _make_repo_ctx()
        dispatcher = Dispatcher([py_plugin, js_plugin])

        # Simulate cached files
        dispatcher._file_cache = {
            "/test/file1.py": (1000, 100, "hash1"),
            "/test/file2.py": (1001, 200, "hash2"),
            "/test/app.js": (1002, 300, "hash3"),
        }

        # Configure plugin supports
        py_plugin.supports.side_effect = lambda p: p.suffix == ".py"
        js_plugin.supports.side_effect = lambda p: p.suffix == ".js"

        stats = dispatcher.get_statistics(ctx)

        assert stats["total"] == 3
        assert stats["by_language"]["python"] == 2
        assert stats["by_language"]["javascript"] == 1

    def test_get_statistics_plugin_error(self, mock_plugin):
        """Test statistics when plugin.supports raises error."""
        ctx = _make_repo_ctx()
        dispatcher = Dispatcher([mock_plugin])
        dispatcher._file_cache = {"/test/file.py": (1000, 100, "hash")}

        mock_plugin.supports.side_effect = Exception("Plugin error")

        stats = dispatcher.get_statistics(ctx)

        # Should handle error gracefully
        assert stats["total"] == 0
        assert stats["by_language"] == {}


class TestConcurrency:
    """Test concurrent operations."""

    def test_concurrent_indexing(self, mock_plugin, tmp_path):
        """Test concurrent file indexing."""
        import concurrent.futures

        dispatcher = Dispatcher([mock_plugin], use_plugin_factory=False, lazy_load=False)
        mock_plugin.supports.return_value = True
        mock_plugin.indexFile.return_value = {"symbols": []}

        # Create multiple test files
        files = []
        for i in range(10):
            test_file = tmp_path / f"test{i}.py"
            test_file.write_text(f"def func{i}(): pass")
            files.append(test_file)

        ctx = _make_repo_ctx(workspace_root=tmp_path)
        # Index files concurrently
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(dispatcher.index_file, ctx, f) for f in files]
            concurrent.futures.wait(futures)

        # All files should be cached
        assert len(dispatcher._file_cache) == 10
        assert mock_plugin.indexFile.call_count == 10

    def test_concurrent_search(self, mock_plugin):
        """Test concurrent search operations (plugin fallback path, no sqlite)."""
        import concurrent.futures
        from pathlib import Path
        from unittest.mock import MagicMock as _MM

        from mcp_server.core.repo_context import RepoContext
        from mcp_server.storage.multi_repo_manager import RepositoryInfo

        _REPO_ID = "test-repo"
        psr = _make_psr_with_plugins(_REPO_ID, [mock_plugin])
        dispatcher = Dispatcher([mock_plugin], plugin_set_registry=psr)
        mock_plugin.search.return_value = [
            SearchResult(name="result", kind="function", path="/test.py", score=1.0)
        ]

        reg = _MM(spec=RepositoryInfo)
        reg.tracked_branch = "main"
        ctx = RepoContext(
            repo_id=_REPO_ID,
            sqlite_store=None,
            workspace_root=Path("/tmp/test"),
            tracked_branch="main",
            registry_entry=reg,
        )

        def search_task(query):
            return list(dispatcher.search(ctx, query))

        # Perform concurrent searches
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(search_task, f"query{i}") for i in range(20)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        # All searches should complete
        assert len(results) == 20
        assert all(len(r) == 1 for r in results)
        assert mock_plugin.search.call_count == 20


class TestPerformance:
    """Performance benchmarks for dispatcher operations."""

    @pytest.mark.benchmark
    def test_lookup_performance(self, benchmark_results):
        """Benchmark first-hit fallback lookup performance (IF-0-P11-1)."""
        from mcp_server.dispatcher.fallback import run_gated_fallback

        # Create five mock plugins; only the last returns the target symbol
        plugins = []
        for i in range(5):
            plugin = Mock(spec=IPlugin, lang=f"lang{i}")
            plugin.getDefinition.return_value = None
            plugins.append(plugin)

        # Last plugin has the symbol
        plugins[-1].getDefinition.return_value = SymbolDef(
            name="target", kind="function", path="/found.py", line=1
        )

        with measure_time("dispatcher_lookup", benchmark_results):
            for _ in range(1000):
                result = run_gated_fallback(plugins, "target", source_ext=".py", timeout_ms=1000)
                assert result is not None

    @pytest.mark.benchmark
    def test_cache_performance(self, benchmark_results, tmp_path):
        """Benchmark cache hit performance."""
        dispatcher = Dispatcher([])

        # Create test files with cached entries
        for i in range(100):
            path = tmp_path / f"file{i}.py"
            content = f"def func{i}(): pass"
            path.write_text(content)

            # Pre-populate cache
            stat = path.stat()
            dispatcher._file_cache[str(path)] = (
                stat.st_mtime,
                stat.st_size,
                dispatcher._get_file_hash(content),
            )

        # Benchmark cache lookups
        paths = list(tmp_path.glob("*.py"))

        with measure_time("dispatcher_cache_check", benchmark_results):
            for _ in range(10):
                for path in paths:
                    content = path.read_text()
                    should_index = dispatcher._should_reindex(path, content)
                    assert not should_index  # All should be cached


class TestQueryIntent:
    """Unit tests for the query intent classifier."""

    def test_class_prefix(self):
        intent, name, kind = classify("class SemanticIndexer")
        assert intent == QueryIntent.SYMBOL
        assert name == "SemanticIndexer"
        assert kind == "class"

    def test_def_prefix(self):
        intent, name, kind = classify("def rerank")
        assert intent == QueryIntent.SYMBOL
        assert name == "rerank"
        assert kind == "function"

    def test_function_prefix(self):
        intent, name, kind = classify("function myHandler")
        assert intent == QueryIntent.SYMBOL
        assert name == "myHandler"
        assert kind == "function"

    def test_camelcase_single_token(self):
        intent, name, kind = classify("EnhancedDispatcher")
        assert intent == QueryIntent.SYMBOL
        assert name == "EnhancedDispatcher"
        assert kind == "class"

    def test_snake_case_single_token(self):
        intent, name, kind = classify("search_symbols")
        assert intent == QueryIntent.SYMBOL
        assert name == "search_symbols"

    def test_dotted_name(self):
        intent, name, kind = classify("SQLiteStore.get_symbol")
        assert intent == QueryIntent.SYMBOL
        assert name == "get_symbol"

    def test_multiword_is_lexical(self):
        intent, name, kind = classify("semantic preflight")
        assert intent == QueryIntent.LEXICAL

    def test_natural_language_is_lexical(self):
        intent, _, _ = classify("where is qdrant autostart implemented")
        assert intent == QueryIntent.LEXICAL

    def test_qdrant_docker_is_lexical(self):
        intent, _, _ = classify("qdrant docker compose autostart")
        assert intent == QueryIntent.LEXICAL

    def test_empty_is_lexical(self):
        intent, _, _ = classify("")
        assert intent == QueryIntent.LEXICAL

    def test_case_insensitive_prefix(self):
        intent, name, kind = classify("Class MyModel")
        assert intent == QueryIntent.SYMBOL
        assert name == "MyModel"
        assert kind == "class"


class TestSymbolRouting:
    """Tests for _symbol_route and its integration into search()."""

    def setup_method(self):
        import os

        # .env.native has MCP_ENABLE_MULTI_REPO=true with stale /workspaces paths.
        # Force it off so dispatcher init doesn't try to create that directory.
        os.environ["MCP_ENABLE_MULTI_REPO"] = "false"

    def _make_store(self, symbol_rows):
        """Return a mock SQLiteStore whose get_symbol returns symbol_rows."""
        store = MagicMock()
        store.get_symbol.return_value = symbol_rows
        return store

    def _make_dispatcher_with_ctx(self, store):
        d = Dispatcher([])
        ctx = _make_repo_ctx(store)
        return d, ctx

    def test_symbol_route_returns_definition_file(self):
        store = self._make_store(
            [
                {
                    "name": "SemanticIndexer",
                    "kind": "class",
                    "line_start": 42,
                    "line_end": 120,
                    "signature": "class SemanticIndexer:",
                    "documentation": None,
                    "file_path": "mcp_server/utils/semantic_indexer.py",
                }
            ]
        )
        d, ctx = self._make_dispatcher_with_ctx(store)
        results = d._symbol_route(store, "SemanticIndexer", "class", 5)
        assert len(results) == 1
        assert results[0]["file"] == "mcp_server/utils/semantic_indexer.py"
        assert results[0]["line"] == 42
        assert results[0]["symbol"] == "SemanticIndexer"

    def test_symbol_route_prefers_non_init_file(self):
        """__init__.py re-export should sort after the definition file."""
        store = self._make_store(
            [
                {
                    "name": "SemanticIndexer",
                    "kind": "class",
                    "line_start": 1,
                    "line_end": 1,
                    "signature": "from .semantic_indexer import SemanticIndexer",
                    "documentation": None,
                    "file_path": "mcp_server/utils/__init__.py",
                },
                {
                    "name": "SemanticIndexer",
                    "kind": "class",
                    "line_start": 42,
                    "line_end": 120,
                    "signature": "class SemanticIndexer:",
                    "documentation": None,
                    "file_path": "mcp_server/utils/semantic_indexer.py",
                },
            ]
        )
        d, ctx = self._make_dispatcher_with_ctx(store)
        results = d._symbol_route(store, "SemanticIndexer", "class", 5)
        assert results[0]["file"] == "mcp_server/utils/semantic_indexer.py"
        assert results[1]["file"] == "mcp_server/utils/__init__.py"

    def test_symbol_route_empty_when_not_found(self):
        store = self._make_store([])
        # Both strict and relaxed calls return empty
        store.get_symbol.return_value = []
        d, ctx = self._make_dispatcher_with_ctx(store)
        results = d._symbol_route(store, "NonExistentSymbol", "class", 5)
        assert results == []

    def test_symbol_route_relaxes_kind_on_miss(self):
        """If kind-restricted lookup returns nothing, retry without kind."""
        store = MagicMock()
        store.get_symbol.side_effect = [
            [],  # first call (with kind) → empty
            [
                {
                    "name": "rerank",
                    "kind": "method",
                    "line_start": 10,
                    "line_end": 20,
                    "signature": "def rerank()",
                    "documentation": None,
                    "file_path": "mcp_server/indexer/reranker.py",
                }
            ],
        ]
        d, ctx = self._make_dispatcher_with_ctx(store)
        results = d._symbol_route(store, "rerank", "class", 5)
        assert len(results) == 1
        assert "reranker.py" in results[0]["file"]

    def test_search_routes_class_query_to_symbols(self):
        """search('class SemanticIndexer') should hit symbol table, not BM25."""
        store = MagicMock()
        store.get_symbol.return_value = [
            {
                "name": "SemanticIndexer",
                "kind": "class",
                "line_start": 42,
                "line_end": 120,
                "signature": "class SemanticIndexer:",
                "documentation": None,
                "file_path": "mcp_server/utils/semantic_indexer.py",
            }
        ]
        d, ctx = self._make_dispatcher_with_ctx(store)
        results = list(d.search(ctx, "class SemanticIndexer", limit=5))
        assert len(results) >= 1
        assert results[0]["file"] == "mcp_server/utils/semantic_indexer.py"
        # BM25 (search_bm25) should NOT have been called
        store.search_bm25.assert_not_called()

    def test_search_falls_back_to_bm25_when_symbol_not_found(self):
        """When symbols table has no match, BM25 path should still run."""
        store = MagicMock()
        store.get_symbol.return_value = []
        store.search_bm25.return_value = []  # BM25 also returns nothing
        d, ctx = self._make_dispatcher_with_ctx(store)
        list(d.search(ctx, "class SemanticIndexer", limit=5))
        # BM25 was attempted as fallback
        store.search_bm25.assert_called()

    def test_multiword_query_skips_symbol_route(self):
        """Multi-word lexical queries must NOT hit the symbol table."""
        store = MagicMock()
        store.search_bm25.return_value = []
        d, ctx = self._make_dispatcher_with_ctx(store)
        list(d.search(ctx, "qdrant docker compose autostart", limit=5))
        store.get_symbol.assert_not_called()


# ---------------------------------------------------------------------------
# SL-1.1 — Protocol-conformance tests (initially RED; green after SL-1.2 impl)
# ---------------------------------------------------------------------------


def _make_repo_ctx(sqlite_store=None, workspace_root=Path("/tmp/test-repo")) -> RepoContext:
    """Build a minimal RepoContext for tests."""
    from pathlib import Path
    from unittest.mock import MagicMock

    from mcp_server.storage.multi_repo_manager import RepositoryInfo

    if sqlite_store is None:
        sqlite_store = MagicMock()

    registry_entry = MagicMock(spec=RepositoryInfo)
    registry_entry.tracked_branch = "main"
    registry_entry.path = workspace_root

    return RepoContext(
        repo_id="test-repo-id-0001",
        sqlite_store=sqlite_store,
        workspace_root=workspace_root,
        tracked_branch="main",
        registry_entry=registry_entry,
    )


class TestEnhancedDispatcherProtocolConformance:
    """EnhancedDispatcher must conform to DispatcherProtocol (SL-1.1)."""

    def test_direct_symbol_response_satisfies_http_contract(self, sqlite_store):
        from pydantic import TypeAdapter

        from mcp_server.plugin_base import SymbolDef

        repo = sqlite_store.create_repository("/tmp/test-repo", "fixture")
        file_id = sqlite_store.store_file(
            repo, "/tmp/test-repo/source.py", "source.py", language="python"
        )
        sqlite_store.store_symbol(file_id, "fixture_symbol", "function", line_start=3, line_end=18)
        result = Dispatcher([]).lookup(_make_repo_ctx(sqlite_store), "fixture_symbol")
        value = TypeAdapter(SymbolDef).validate_python(result)
        assert value["start_line"] == 3
        assert value["end_line"] == 18
        assert value["language"] == "python"

    def test_isinstance_dispatcher_protocol(self):
        """runtime_checkable isinstance check must pass."""
        d = Dispatcher([])
        assert isinstance(d, DispatcherProtocol)

    def test_ctor_rejects_sqlite_store_kwarg(self):
        """EnhancedDispatcher() must raise TypeError when sqlite_store= is passed."""
        with pytest.raises(TypeError):
            Dispatcher(sqlite_store=MagicMock())

    def test_lookup_accepts_ctx_first_arg(self):
        """lookup(ctx, symbol) must route through ctx.sqlite_store."""
        store = MagicMock()
        store.get_symbol.return_value = []
        ctx = _make_repo_ctx(store)
        d = Dispatcher([])
        # Should not raise; returns None when nothing found
        result = d.lookup(ctx, "nonexistent_symbol_xyz")
        assert result is None

    def test_search_accepts_ctx_first_arg(self):
        """search(ctx, query) must not raise and must use ctx.sqlite_store."""
        store = MagicMock()
        store.search_bm25.return_value = []
        ctx = _make_repo_ctx(store)
        d = Dispatcher([])
        results = list(d.search(ctx, "test query"))
        assert isinstance(results, list)

    def test_search_uses_ctx_sqlite_store_not_instance_store(self):
        """search must consult ctx.sqlite_store, not a captured self._sqlite_store."""
        store_a = MagicMock()
        store_a.search_bm25.return_value = [
            {"filepath": "/repo_a/file.py", "snippet": "hello", "score": 1.0, "language": "python"}
        ]
        store_a.find_best_chunk_for_file = MagicMock(return_value=None)
        ctx_a = _make_repo_ctx(store_a)

        store_b = MagicMock()
        store_b.search_bm25.return_value = []
        ctx_b = _make_repo_ctx(store_b)

        d = Dispatcher([])
        # Search via ctx_a should use store_a
        list(d.search(ctx_a, "hello"))
        store_a.search_bm25.assert_called()
        # store_b was never accessed
        store_b.search_bm25.assert_not_called()

        # Search via ctx_b should use store_b (not store_a again)
        list(d.search(ctx_b, "hello"))
        store_b.search_bm25.assert_called()

    def test_health_check_accepts_ctx(self):
        """health_check(ctx) must accept ctx and return a dict."""
        ctx = _make_repo_ctx()
        d = Dispatcher([])
        result = d.health_check(ctx)
        assert isinstance(result, dict)
        assert "status" in result

    def test_get_statistics_accepts_ctx(self):
        """get_statistics(ctx) must accept ctx and return a dict."""
        ctx = _make_repo_ctx()
        d = Dispatcher([])
        result = d.get_statistics(ctx)
        assert isinstance(result, dict)

    def test_index_file_accepts_ctx(self, tmp_path):
        """index_file(ctx, path) must accept ctx as first positional arg."""
        ctx = _make_repo_ctx(workspace_root=tmp_path)
        d = Dispatcher([])
        f = tmp_path / "test.py"
        f.write_text("x = 1")
        result = d.index_file(ctx, f)
        assert result.status == IndexResultStatus.INDEXED

    def test_index_directory_accepts_ctx(self, tmp_path):
        """index_directory(ctx, path) must accept ctx and return a dict."""
        ctx = _make_repo_ctx()
        d = Dispatcher([])
        result = d.index_directory(ctx, tmp_path)
        assert isinstance(result, dict)

    def test_index_directory_treats_unsupported_plugin_files_as_ignored(self, tmp_path):
        ctx = _make_repo_ctx(workspace_root=tmp_path)
        target = tmp_path / "data.json"
        target.write_text('{"ok": true}\n')

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert result["failed_files"] == 0
        assert result["ignored_files"] == 1

    def test_index_directory_records_low_level_lexical_timeout(self, tmp_path, monkeypatch):
        store = MagicMock(db_path=str(tmp_path / "index.db"))
        store.health_check.return_value = {
            "status": "healthy",
            "journal_mode": "WAL",
            "busy_timeout_ms": 5000,
        }
        ctx = _make_repo_ctx(sqlite_store=store)
        target = tmp_path / "sample.py"
        target.write_text("x = 1\n")

        def _timeout(self, _ctx, _path, _stats, progress_callback=None):
            _stats["lexical_stage"] = "walking"
            _stats["lexical_files_attempted"] += 1
            _stats["in_flight_path"] = str(_path.resolve())
            raise TimeoutError("Operation timed out after 20 seconds")

        monkeypatch.setattr(Dispatcher, "_index_file_with_lexical_timeout", _timeout)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert result["lexical_stage"] == "blocked_file_timeout"
        assert result["lexical_files_attempted"] == 1
        assert result["lexical_files_completed"] == 0
        assert result["last_progress_path"] is None
        assert result["in_flight_path"] == str(target.resolve())
        assert result["semantic_stage"] == "not_run"
        assert result["low_level_blocker"]["code"] == "lexical_file_timeout"
        assert result["storage_diagnostics"]["journal_mode"] == "WAL"
        store.health_check.assert_called_once()

    def test_index_directory_emits_in_flight_progress_before_lexical_completion(
        self, tmp_path, monkeypatch
    ):
        ctx = _make_repo_ctx()
        first = tmp_path / "first.py"
        second = tmp_path / "second.py"
        first.write_text("x = 1\n", encoding="utf-8")
        second.write_text("y = 2\n", encoding="utf-8")
        snapshots = []

        monkeypatch.setattr(
            "mcp_server.dispatcher.dispatcher_enhanced.os.walk",
            lambda _root, followlinks=False: [(str(tmp_path), [], [first.name, second.name])],
        )

        def _index_file(self, _ctx, path, do_semantic=False):
            if path == second:
                assert any(
                    snapshot["last_progress_path"] == str(first.resolve())
                    and snapshot["in_flight_path"] == str(second.resolve())
                    for snapshot in snapshots
                )
            return IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            )

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)
        monkeypatch.setattr(Dispatcher, "index_file", _index_file)

        result = Dispatcher([]).index_directory(
            ctx,
            tmp_path,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        assert result["indexed_files"] == 2
        assert result["failed_files"] == 0
        assert any(
            snapshot["last_progress_path"] == str(first.resolve())
            and snapshot["in_flight_path"] == str(second.resolve())
            for snapshot in snapshots
        )
        assert snapshots[-1]["last_progress_path"] == str(second.resolve())
        assert snapshots[-1]["in_flight_path"] is None

    def test_index_directory_uses_bounded_markdown_path_for_changelog(self, tmp_path, monkeypatch):
        ctx = _make_repo_ctx(workspace_root=tmp_path)
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Added\n- Bounded lexical repair\n",
            encoding="utf-8",
        )

        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin

        def _unexpected(*_args, **_kwargs):
            raise AssertionError("heavy Markdown path should not run for CHANGELOG.md")

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(changelog.resolve())

    def test_index_directory_uses_bounded_markdown_path_for_roadmap(self, tmp_path, monkeypatch):
        ctx = _make_repo_ctx(workspace_root=tmp_path)
        roadmap = tmp_path / "ROADMAP.md"
        roadmap.write_text(
            "# Roadmap\n\n## Current Phase\n\n### Next Up\n- Repair roadmap lexical timeout\n",
            encoding="utf-8",
        )

        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin

        def _unexpected(*_args, **_kwargs):
            raise AssertionError("heavy Markdown path should not run for ROADMAP.md")

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(roadmap.resolve())

    def test_index_directory_uses_bounded_markdown_path_for_final_analysis(
        self, tmp_path, monkeypatch
    ):
        ctx = _make_repo_ctx(workspace_root=tmp_path)
        analysis_report = tmp_path / "FINAL_COMPREHENSIVE_MCP_ANALYSIS.md"
        analysis_report.write_text(
            "# Final Comprehensive MCP Analysis\n\n## Executive Summary\n\n### Timeout Evidence\n- Repair final-analysis lexical timeout\n",
            encoding="utf-8",
        )

        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin

        def _unexpected(*_args, **_kwargs):
            raise AssertionError(
                "heavy Markdown path should not run for FINAL_COMPREHENSIVE_MCP_ANALYSIS.md"
            )

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(analysis_report.resolve())

    def test_index_directory_uses_bounded_markdown_path_for_agents(self, tmp_path, monkeypatch):
        ctx = _make_repo_ctx(workspace_root=tmp_path)
        agents = tmp_path / "AGENTS.md"
        agents.write_text(
            "# MCP Server Agent Configuration\n\n## Current State\n\n### Search Strategy\n- Preserve AGENTS heading discoverability\n",
            encoding="utf-8",
        )

        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin

        def _unexpected(*_args, **_kwargs):
            raise AssertionError("heavy Markdown path should not run for AGENTS.md")

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(agents.resolve())

    def test_index_directory_uses_bounded_markdown_path_for_readme(self, tmp_path, monkeypatch):
        ctx = _make_repo_ctx(workspace_root=tmp_path)
        readme = tmp_path / "README.md"
        readme.write_text(
            "# Code Index MCP\n\n## Overview\n\n### Local-first search\n- Preserve README heading discoverability\n",
            encoding="utf-8",
        )

        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin

        def _unexpected(*_args, **_kwargs):
            raise AssertionError("heavy Markdown path should not run for README.md")

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(readme.resolve())

    def test_index_directory_persists_bounded_readme_without_per_symbol_store_calls(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        repo.mkdir()
        readme = repo / "README.md"
        readme.write_text(
            "# Code-Index-MCP\n\n## Overview\n\n### Search Strategy\n- Preserve README heading discoverability\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        monkeypatch.setattr(
            SQLiteStore,
            "store_symbol",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("README bounded persistence should not call store_symbol")
            ),
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        with store._get_connection() as conn:
            symbol_count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'README.md')"
            ).fetchone()[0]
        store.close()
        assert symbol_count >= 3

    def test_index_directory_uses_bounded_markdown_path_for_ai_docs_overview(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        (repo / "ai_docs").mkdir(parents=True)
        overview = repo / "ai_docs" / "pytest_overview.md"
        overview.write_text(
            "# pytest AI Context\n\n## Framework Overview\n\n### Test Structure\n- Preserve overview heading discoverability\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected(*_args, **_kwargs):
            raise AssertionError("heavy Markdown path should not run for ai_docs/*_overview.md")

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(overview.resolve())
        with store._get_connection() as conn:
            symbol_names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'ai_docs/pytest_overview.md') ORDER BY id"
                ).fetchall()
            ]
        store.close()
        assert "pytest AI Context" in symbol_names
        assert "Framework Overview" in symbol_names
        assert "Test Structure" in symbol_names

    def test_index_directory_uses_bounded_markdown_path_for_later_ai_docs_overview_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        (repo / "ai_docs").mkdir(parents=True)
        black_doc = repo / "ai_docs" / "black_isort_overview.md"
        sqlite_doc = repo / "ai_docs" / "sqlite_fts5_overview.md"
        unrelated_doc = repo / "ai_docs" / "qdrant.md"
        jedi_doc = repo / "ai_docs" / "jedi.md"
        black_doc.write_text(
            "# Black & isort AI Context\n\n## Framework Overview\n\n### Tooling Contract\n"
            "- Preserve lexical discoverability\n",
            encoding="utf-8",
        )
        sqlite_doc.write_text(
            "# SQLite FTS5 Comprehensive Guide for Code Indexing\n\n## Table of Contents\n\n"
            "### Introduction to FTS5\n- Keep the later overview seam on the bounded path\n",
            encoding="utf-8",
        )
        unrelated_doc.write_text(
            "# Qdrant Notes\n\n## Heavy Path\n\n### Guardrail\n- Do not broaden ai_docs bounds\n",
            encoding="utf-8",
        )
        jedi_doc.write_text(
            "# Jedi Documentation\n\n## Overview and Key Features\n\n### Code Completion\n"
            "- Preserve the exact bounded path\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected(*_args, **_kwargs):
            raise AssertionError(
                "heavy Markdown path should not run for the later ai_docs overview pair"
            )

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        plugin = MarkdownPlugin()
        assert (
            plugin._resolve_lightweight_reason(black_doc, black_doc.read_text(encoding="utf-8"))
            == "ai_docs_overview_path"
        )
        assert (
            plugin._resolve_lightweight_reason(sqlite_doc, sqlite_doc.read_text(encoding="utf-8"))
            == "ai_docs_overview_path"
        )
        assert (
            plugin._resolve_lightweight_reason(
                unrelated_doc, unrelated_doc.read_text(encoding="utf-8")
            )
            is None
        )
        assert (
            plugin._resolve_lightweight_reason(jedi_doc, jedi_doc.read_text(encoding="utf-8"))
            == "ai_docs_jedi_path"
        )

        result = Dispatcher([]).index_directory(ctx, repo / "ai_docs")

        assert result["indexed_files"] == 4
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(black_doc.resolve()),
            str(sqlite_doc.resolve()),
            str(unrelated_doc.resolve()),
            str(jedi_doc.resolve()),
        }
        with store._get_connection() as conn:
            black_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = 'ai_docs/black_isort_overview.md') "
                    "ORDER BY id"
                ).fetchall()
            ]
            sqlite_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = 'ai_docs/sqlite_fts5_overview.md') "
                    "ORDER BY id"
                ).fetchall()
            ]
            black_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = 'ai_docs/black_isort_overview.md')"
            ).fetchone()[0]
            sqlite_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = 'ai_docs/sqlite_fts5_overview.md')"
            ).fetchone()[0]
        store.close()
        assert "Black & isort AI Context" in black_symbols
        assert "Framework Overview" in black_symbols
        assert "Tooling Contract" in black_symbols
        assert "SQLite FTS5 Comprehensive Guide for Code Indexing" in sqlite_symbols
        assert "Table of Contents" in sqlite_symbols
        assert "Introduction to FTS5" in sqlite_symbols
        assert black_chunk_count == 0
        assert sqlite_chunk_count == 0

    def test_index_directory_uses_exact_bounded_markdown_path_for_jedi_ai_doc(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        (repo / "ai_docs").mkdir(parents=True)
        jedi_doc = repo / "ai_docs" / "jedi.md"
        jedi_doc.write_text(
            "# Jedi Documentation\n\n## Overview and Key Features\n\n### Code Completion\n- Preserve heading discoverability\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected(*_args, **_kwargs):
            raise AssertionError("heavy Markdown path should not run for ai_docs/jedi.md")

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(jedi_doc.resolve())
        with store._get_connection() as conn:
            symbol_names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'ai_docs/jedi.md') ORDER BY id"
                ).fetchall()
            ]
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'ai_docs/jedi.md')"
            ).fetchone()[0]
        store.close()
        assert "Jedi Documentation" in symbol_names
        assert "Overview and Key Features" in symbol_names
        assert "Code Completion" in symbol_names
        assert chunk_count == 0

    def test_index_directory_uses_exact_bounded_markdown_path_for_ai_docs_readme_tail_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        ai_docs_dir = repo / "ai_docs"
        ai_docs_dir.mkdir(parents=True)
        prometheus_doc = ai_docs_dir / "prometheus_overview.md"
        ai_docs_readme = ai_docs_dir / "README.md"
        unrelated_doc = ai_docs_dir / "qdrant.md"
        jedi_doc = ai_docs_dir / "jedi.md"
        root_readme = repo / "README.md"
        prometheus_doc.write_text(
            "# Prometheus Overview\n\n## Introduction\n\n```python\n"
            "# Ignore this code comment heading\n```\n\n### Monitoring surface\n"
            "- Preserve title and heading discoverability\n",
            encoding="utf-8",
        )
        ai_docs_readme.write_text(
            "# AI Documentation Index\n\n## Core Frameworks\n\n"
            "### [FastAPI](./fastapi_overview.md)\n- Keep the exact README tail bounded\n"
            "\n## Security & Monitoring\n\n### [Prometheus](./prometheus_overview.md)\n"
            "- Preserve cross-link discoverability\n",
            encoding="utf-8",
        )
        unrelated_doc.write_text(
            "# Qdrant Notes\n\n## Heavy Path\n\n### Guardrail\n"
            "- Do not broaden the exact ai_docs pair\n",
            encoding="utf-8",
        )
        jedi_doc.write_text("# Jedi Documentation\n", encoding="utf-8")
        root_readme.write_text(
            "# Root README\n\n## Overview\n\n### Install\n- Keep generic README handling intact\n",
            encoding="utf-8",
        )

        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected(*_args, **_kwargs):
            raise AssertionError(
                "heavy Markdown path should not run for the exact ai_docs README-tail pair"
            )

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        plugin = MarkdownPlugin()
        assert (
            plugin._resolve_lightweight_reason(
                prometheus_doc, prometheus_doc.read_text(encoding="utf-8")
            )
            == "ai_docs_prometheus_overview_path"
        )
        assert (
            plugin._resolve_lightweight_reason(
                ai_docs_readme, ai_docs_readme.read_text(encoding="utf-8")
            )
            == "ai_docs_readme_path"
        )
        assert (
            plugin._resolve_lightweight_reason(
                unrelated_doc, unrelated_doc.read_text(encoding="utf-8")
            )
            is None
        )
        assert (
            plugin._resolve_lightweight_reason(jedi_doc, jedi_doc.read_text(encoding="utf-8"))
            == "ai_docs_jedi_path"
        )
        assert (
            plugin._resolve_lightweight_reason(root_readme, root_readme.read_text(encoding="utf-8"))
            == "readme_path"
        )

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 5
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(prometheus_doc.resolve()),
            str(ai_docs_readme.resolve()),
            str(unrelated_doc.resolve()),
            str(jedi_doc.resolve()),
            str(root_readme.resolve()),
        }
        with store._get_connection() as conn:
            prometheus_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'ai_docs/prometheus_overview.md') ORDER BY id"
                ).fetchall()
            ]
            ai_docs_readme_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'ai_docs/README.md') ORDER BY id"
                ).fetchall()
            ]
            prometheus_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'ai_docs/prometheus_overview.md')"
            ).fetchone()[0]
            ai_docs_readme_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'ai_docs/README.md')"
            ).fetchone()[0]
        store.close()

        assert prometheus_symbols == [
            "Prometheus Overview",
            "Prometheus Overview",
            "Introduction",
            "Monitoring surface",
        ]
        assert ai_docs_readme_symbols == [
            "AI Documentation Index",
            "AI Documentation Index",
            "Core Frameworks",
            "[FastAPI](./fastapi_overview.md)",
            "Security & Monitoring",
            "[Prometheus](./prometheus_overview.md)",
        ]
        assert prometheus_chunk_count == 0
        assert ai_docs_readme_chunk_count == 0

    def test_index_directory_uses_exact_bounded_markdown_path_for_architecture_api_docs(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        architecture_dir = repo / "docs" / "architecture"
        api_dir = repo / "docs" / "api"
        architecture_dir.mkdir(parents=True)
        api_dir.mkdir(parents=True)
        p2b_doc = architecture_dir / "P2B-known-limits.md"
        api_doc = api_dir / "API-REFERENCE.md"
        unrelated_doc = api_dir / "OTHER.md"
        p2b_doc.write_text(
            "# P2B Known Limits - Deferred Per-Repo Global State\n\n"
            "## Status\n\n"
            "### Deferred: Process-Global Dispatcher State\n"
            "- Preserve exact bounded lexical discoverability\n\n"
            "## Impact\n\n"
            "## Resolution Plan\n",
            encoding="utf-8",
        )
        api_doc.write_text(
            "# Code-Index-MCP API Reference\n\n"
            "## Overview\n\n"
            "## Authentication\n\n"
            "## API Endpoints\n\n"
            "#### POST /auth/login\n",
            encoding="utf-8",
        )
        unrelated_doc.write_text(
            "# Other API Notes\n\n"
            "## Scope\n\n"
            "### Guardrail\n"
            "- Keep unrelated docs on the heavy Markdown path\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected(*_args, **_kwargs):
            raise AssertionError(
                "heavy Markdown path should not run for the exact P2B/API-reference pair"
            )

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        plugin = MarkdownPlugin()
        assert (
            plugin._resolve_lightweight_reason(p2b_doc, p2b_doc.read_text(encoding="utf-8"))
            == "architecture_p2b_known_limits_path"
        )
        assert (
            plugin._resolve_lightweight_reason(api_doc, api_doc.read_text(encoding="utf-8"))
            == "api_reference_path"
        )
        assert (
            plugin._resolve_lightweight_reason(
                unrelated_doc, unrelated_doc.read_text(encoding="utf-8")
            )
            is None
        )

        result = Dispatcher([]).index_directory(ctx, repo / "docs")

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(p2b_doc.resolve()),
            str(api_doc.resolve()),
            str(unrelated_doc.resolve()),
        }
        with store._get_connection() as conn:
            p2b_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'docs/architecture/P2B-known-limits.md') ORDER BY id"
                ).fetchall()
            ]
            api_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'docs/api/API-REFERENCE.md') ORDER BY id"
                ).fetchall()
            ]
            unrelated_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = 'docs/api/OTHER.md') "
                    "ORDER BY id"
                ).fetchall()
            ]
            p2b_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'docs/architecture/P2B-known-limits.md')"
            ).fetchone()[0]
            api_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = 'docs/api/API-REFERENCE.md')"
            ).fetchone()[0]
            unrelated_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = 'docs/api/OTHER.md')"
            ).fetchone()[0]
            p2b_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'docs/architecture/P2B-known-limits.md')"
            ).fetchall()
            api_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = 'docs/api/API-REFERENCE.md')"
            ).fetchall()
        store.close()

        assert p2b_symbols[0] == "P2B Known Limits - Deferred Per-Repo Global State"
        assert "Status" in p2b_symbols
        assert "Deferred: Process-Global Dispatcher State" in p2b_symbols
        assert "Impact" in p2b_symbols
        assert "Resolution Plan" in p2b_symbols
        assert api_symbols[0] == "Code-Index-MCP API Reference"
        assert "Overview" in api_symbols
        assert "Authentication" in api_symbols
        assert "API Endpoints" in api_symbols
        assert "POST /auth/login" in api_symbols
        assert "Other API Notes" in unrelated_symbols
        assert "Scope" in unrelated_symbols
        assert "Guardrail" in unrelated_symbols
        assert p2b_chunk_count == 0
        assert api_chunk_count == 0
        assert unrelated_chunk_count > 0
        assert len(p2b_fts_rows) == 1
        assert len(api_fts_rows) == 1
        assert "Deferred: Process-Global Dispatcher State" in p2b_fts_rows[0][0]
        assert "Resolution Plan" in p2b_fts_rows[0][0]
        assert "Authentication" in api_fts_rows[0][0]
        assert "POST /auth/login" in api_fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_markdown_path_for_validation_docs(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        validation_dir = repo / "docs" / "validation"
        validation_dir.mkdir(parents=True)
        ga_doc = validation_dir / "ga-closeout-decision.md"
        mre2e_doc = validation_dir / "mre2e-evidence.md"
        ga_doc.write_text(
            "# GA Closeout Decision\n\n## Summary\n\n### Final decision\n- stay on RC/public-alpha\n",
            encoding="utf-8",
        )
        mre2e_doc.write_text(
            "# MRE2E Evidence\n\n## Fresh-State Multi-Repo Lifecycle\n\n### Frozen command set\n- preserve lifecycle evidence\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected(*_args, **_kwargs):
            raise AssertionError(
                "heavy Markdown path should not run for docs/validation bounded files"
            )

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 2
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(mre2e_doc.resolve())
        with store._get_connection() as conn:
            ga_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = 'docs/validation/ga-closeout-decision.md') "
                    "ORDER BY id"
                ).fetchall()
            ]
            mre2e_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = 'docs/validation/mre2e-evidence.md') "
                    "ORDER BY id"
                ).fetchall()
            ]
            ga_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = 'docs/validation/ga-closeout-decision.md')"
            ).fetchone()[0]
            mre2e_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = 'docs/validation/mre2e-evidence.md')"
            ).fetchone()[0]
            ga_file_rows = conn.execute(
                "SELECT COUNT(*) FROM files WHERE relative_path = 'docs/validation/ga-closeout-decision.md'"
            ).fetchone()[0]
            mre2e_file_rows = conn.execute(
                "SELECT COUNT(*) FROM files WHERE relative_path = 'docs/validation/mre2e-evidence.md'"
            ).fetchone()[0]
        store.close()

        assert ga_symbols == [
            "GA Closeout Decision",
            "GA Closeout Decision",
            "Summary",
            "Final decision",
        ]
        assert mre2e_symbols == [
            "MRE2E Evidence",
            "MRE2E Evidence",
            "Fresh-State Multi-Repo Lifecycle",
            "Frozen command set",
        ]
        assert ga_chunk_count == 0
        assert mre2e_chunk_count == 0
        assert ga_file_rows == 1
        assert mre2e_file_rows == 1

    def test_index_directory_uses_exact_bounded_markdown_path_for_claude_commands(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        commands_dir = repo / ".claude" / "commands"
        commands_dir.mkdir(parents=True)
        execute_doc = commands_dir / "execute-lane.md"
        plan_doc = commands_dir / "plan-phase.md"
        review_doc = commands_dir / "review-phase.md"
        execute_doc.write_text(
            "# Execute Lane\n\n## Workflow\n\n### Lane execution\n- preserve lexical progress\n",
            encoding="utf-8",
        )
        plan_doc.write_text(
            "# Plan Phase\n\n## Planning Rules\n\n### Dependency freeze\n- preserve heading discoverability\n",
            encoding="utf-8",
        )
        review_doc.write_text(
            "# Review Phase\n\n## Review Rules\n\n### Manual follow-up\n- stay on heavy Markdown path\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        def _tracked_chunk_text(*_args, **_kwargs):
            calls.append("called")
            return []

        monkeypatch.setattr(
            "mcp_server.plugins.python_plugin.plugin.chunk_text", _tracked_chunk_text
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(execute_doc.resolve()),
            str(plan_doc.resolve()),
            str(review_doc.resolve()),
        }
        with store._get_connection() as conn:
            execute_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = '.claude/commands/execute-lane.md') "
                    "ORDER BY id"
                ).fetchall()
            ]
            plan_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = '.claude/commands/plan-phase.md') "
                    "ORDER BY id"
                ).fetchall()
            ]
            execute_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = '.claude/commands/execute-lane.md')"
            ).fetchone()[0]
            plan_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = '.claude/commands/plan-phase.md')"
            ).fetchone()[0]
            review_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = '.claude/commands/review-phase.md')"
            ).fetchone()[0]
        store.close()

        assert execute_symbols == [
            "Execute Lane",
            "Execute Lane",
            "Workflow",
            "Lane execution",
        ]
        assert plan_symbols == [
            "Plan Phase",
            "Plan Phase",
            "Planning Rules",
            "Dependency freeze",
        ]
        assert execute_chunk_count == 0
        assert plan_chunk_count == 0
        assert review_chunk_count > 0

    def test_index_directory_uses_exact_bounded_markdown_path_for_benchmark_docs(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        benchmark_dir = repo / "docs" / "benchmarks"
        benchmark_dir.mkdir(parents=True)
        rerun_doc = (
            benchmark_dir
            / "mcp_vs_native_benchmark_fullrepo_fireworks_qwen_voyage_local_iter5_rerun.md"
        )
        production_doc = benchmark_dir / "production_benchmark.md"
        rerun_doc.write_text(
            "# MCP vs Native Benchmark\n\n## Native Tools\n\n### ripgrep\n- Preserve benchmark evidence\n",
            encoding="utf-8",
        )
        production_doc.write_text(
            "# Production Retrieval Benchmark\n\n## MCP Dispatcher Results\n\n### Hybrid search\n- Preserve production benchmark evidence\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected(*_args, **_kwargs):
            raise AssertionError(
                "heavy Markdown path should not run for docs/benchmarks bounded files"
            )

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 2
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(production_doc.resolve())
        with store._get_connection() as conn:
            rerun_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'docs/benchmarks/mcp_vs_native_benchmark_fullrepo_fireworks_qwen_voyage_local_iter5_rerun.md') "
                    "ORDER BY id"
                ).fetchall()
            ]
            production_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'docs/benchmarks/production_benchmark.md') ORDER BY id"
                ).fetchall()
            ]
            rerun_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'docs/benchmarks/mcp_vs_native_benchmark_fullrepo_fireworks_qwen_voyage_local_iter5_rerun.md')"
            ).fetchone()[0]
            production_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'docs/benchmarks/production_benchmark.md')"
            ).fetchone()[0]
            rerun_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'docs/benchmarks/mcp_vs_native_benchmark_fullrepo_fireworks_qwen_voyage_local_iter5_rerun.md')"
            ).fetchall()
            production_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = 'docs/benchmarks/production_benchmark.md')"
            ).fetchall()
        store.close()

        assert rerun_symbols == [
            "MCP vs Native Benchmark",
            "MCP vs Native Benchmark",
            "Native Tools",
            "ripgrep",
        ]
        assert production_symbols == [
            "Production Retrieval Benchmark",
            "Production Retrieval Benchmark",
            "MCP Dispatcher Results",
            "Hybrid search",
        ]
        assert rerun_chunk_count == 0
        assert production_chunk_count == 0
        assert len(rerun_fts_rows) == 1
        assert len(production_fts_rows) == 1
        assert "Native Tools" in rerun_fts_rows[0][0]
        assert "MCP Dispatcher Results" in production_fts_rows[0][0]

    def test_index_directory_uses_bounded_markdown_path_for_late_v7_phase_plan_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        plans_dir = repo / "plans"
        plans_dir.mkdir(parents=True)
        semcodexlooprelapsetail_doc = plans_dir / "phase-plan-v7-SEMCODEXLOOPRELAPSETAIL.md"
        semgareltail_doc = plans_dir / "phase-plan-v7-SEMGARELTAIL.md"
        unrelated_doc = plans_dir / "phase-plan-v7-SEMUNRELATED.md"
        semcodexlooprelapsetail_doc.write_text(
            "---\n"
            "title: Phase Plan v7 SEMCODEXLOOPRELAPSETAIL\n"
            "---\n"
            "# SEMCODEXLOOPRELAPSETAIL Recovery\n\n"
            "## Context\n\n"
            "### Legacy loop rebound\n"
            "- Preserve bounded discoverability\n\n"
            "## Automation Handoff\n\n"
            "### Phase-loop compatibility\n"
            "- Preserve heading discoverability\n",
            encoding="utf-8",
        )
        semgareltail_doc.write_text(
            "---\n"
            "title: Phase Plan v7 SEMGARELTAIL\n"
            "---\n"
            "# SEMGARELTAIL Follow-up\n\n"
            "## Verification\n\n"
            "### Later blocker\n"
            "- Preserve bounded discoverability\n\n"
            "## Acceptance Criteria\n\n"
            "### Release docs seam\n"
            "- Preserve heading discoverability\n",
            encoding="utf-8",
        )
        unrelated_doc.write_text(
            "# SEMUNRELATED Notes\n\n"
            "## Scope\n\n"
            "### Guardrail\n"
            "- Keep generic phase-plan handling intact\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected(*_args, **_kwargs):
            raise AssertionError("heavy Markdown path should not run for bounded phase-plan files")

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(semcodexlooprelapsetail_doc.resolve()),
            str(semgareltail_doc.resolve()),
            str(unrelated_doc.resolve()),
        }
        with store._get_connection() as conn:
            semcodexlooprelapsetail_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v7-SEMCODEXLOOPRELAPSETAIL.md') ORDER BY id"
                ).fetchall()
            ]
            semgareltail_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v7-SEMGARELTAIL.md') ORDER BY id"
                ).fetchall()
            ]
            unrelated_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v7-SEMUNRELATED.md') ORDER BY id"
                ).fetchall()
            ]
            semcodexlooprelapsetail_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMCODEXLOOPRELAPSETAIL.md')"
            ).fetchone()[0]
            semgareltail_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMGARELTAIL.md')"
            ).fetchone()[0]
            unrelated_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMUNRELATED.md')"
            ).fetchone()[0]
            semcodexlooprelapsetail_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMCODEXLOOPRELAPSETAIL.md')"
            ).fetchall()
            semgareltail_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMGARELTAIL.md')"
            ).fetchall()
        store.close()

        assert semcodexlooprelapsetail_symbols == [
            "Phase Plan v7 SEMCODEXLOOPRELAPSETAIL",
            "SEMCODEXLOOPRELAPSETAIL Recovery",
            "Context",
            "Legacy loop rebound",
            "Automation Handoff",
            "Phase-loop compatibility",
        ]
        assert semgareltail_symbols == [
            "Phase Plan v7 SEMGARELTAIL",
            "SEMGARELTAIL Follow-up",
            "Verification",
            "Later blocker",
            "Acceptance Criteria",
            "Release docs seam",
        ]
        assert unrelated_symbols == [
            "SEMUNRELATED Notes",
            "SEMUNRELATED Notes",
            "Scope",
            "Guardrail",
        ]
        assert semcodexlooprelapsetail_chunk_count == 0
        assert semgareltail_chunk_count == 0
        assert unrelated_chunk_count == 0
        assert len(semcodexlooprelapsetail_fts_rows) == 1
        assert len(semgareltail_fts_rows) == 1
        assert "Phase-loop compatibility" in semcodexlooprelapsetail_fts_rows[0][0]
        assert "Release docs seam" in semgareltail_fts_rows[0][0]

    def test_index_directory_uses_bounded_markdown_path_for_historical_phase_plan_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        plans_dir = repo / "plans"
        plans_dir.mkdir(parents=True)
        watch_doc = plans_dir / "phase-plan-v6-WATCH.md"
        p19_doc = plans_dir / "phase-plan-v1-p19.md"
        unrelated_doc = plans_dir / "phase-plan-v7-SEMUNRELATED.md"
        watch_doc.write_text(
            "---\n"
            "title: Phase Plan v6 WATCH\n"
            "---\n"
            "# WATCH: Multi-Repo Watcher Event Correctness\n\n"
            "## Context\n\n"
            "### Watch truth\n"
            "- Preserve bounded discoverability\n",
            encoding="utf-8",
        )
        p19_doc.write_text(
            "# P19: Release Readiness\n\n"
            "## Context\n\n"
            "### What actually exists today\n"
            "- Preserve bounded discoverability\n\n"
            "## Lane Index & Dependencies\n\n"
            "### Wave plan\n"
            "- Keep legacy heading discoverability\n",
            encoding="utf-8",
        )
        unrelated_doc.write_text(
            "# SEMUNRELATED Notes\n\n"
            "## Scope\n\n"
            "### Guardrail\n"
            "- Keep generic phase-plan handling intact\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected(*_args, **_kwargs):
            raise AssertionError(
                "heavy Markdown path should not run for bounded historical phase-plan files"
            )

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(watch_doc.resolve()),
            str(p19_doc.resolve()),
            str(unrelated_doc.resolve()),
        }
        with store._get_connection() as conn:
            watch_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v6-WATCH.md') ORDER BY id"
                ).fetchall()
            ]
            p19_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v1-p19.md') ORDER BY id"
                ).fetchall()
            ]
            unrelated_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v7-SEMUNRELATED.md') ORDER BY id"
                ).fetchall()
            ]
            watch_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v6-WATCH.md')"
            ).fetchone()[0]
            p19_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v1-p19.md')"
            ).fetchone()[0]
            unrelated_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMUNRELATED.md')"
            ).fetchone()[0]
            watch_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v6-WATCH.md')"
            ).fetchall()
            p19_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v1-p19.md')"
            ).fetchall()
        store.close()

        assert watch_symbols == [
            "Phase Plan v6 WATCH",
            "WATCH: Multi-Repo Watcher Event Correctness",
            "Context",
            "Watch truth",
        ]
        assert p19_symbols == [
            "P19: Release Readiness",
            "P19: Release Readiness",
            "Context",
            "What actually exists today",
            "Lane Index & Dependencies",
            "Wave plan",
        ]
        assert unrelated_symbols == [
            "SEMUNRELATED Notes",
            "SEMUNRELATED Notes",
            "Scope",
            "Guardrail",
        ]
        assert watch_chunk_count == 0
        assert p19_chunk_count == 0
        assert unrelated_chunk_count == 0
        assert len(watch_fts_rows) == 1
        assert len(p19_fts_rows) == 1
        assert "Watch truth" in watch_fts_rows[0][0]
        assert "Wave plan" in p19_fts_rows[0][0]

    def test_index_directory_uses_bounded_markdown_path_for_historical_v1_phase_plan_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        plans_dir = repo / "plans"
        plans_dir.mkdir(parents=True)
        p13_doc = plans_dir / "phase-plan-v1-p13.md"
        p3_doc = plans_dir / "phase-plan-v1-p3.md"
        unrelated_doc = plans_dir / "phase-plan-v1-p17.md"
        p13_doc.write_text(
            "# P13: Reindex Durability + Artifact Automation\n\n"
            "## Context\n\n"
            "### What exists today\n"
            "- Preserve bounded discoverability\n\n"
            "## Interface Freeze Gates\n\n"
            "### Gate contract\n"
            "- Keep interface-freeze prose searchable\n",
            encoding="utf-8",
        )
        p3_doc.write_text(
            "> Plan doc produced by `/plan-phase P3 --consensus`\n\n"
            "# PHASE-3-per-repo-plugins-stores-memory\n\n"
            "## Context\n\n"
            "### What exists\n"
            "- Preserve quoted preamble discoverability\n\n"
            "## Lane Index & Dependencies\n\n"
            "### Wave plan\n"
            "- Keep lane-table structure searchable\n",
            encoding="utf-8",
        )
        unrelated_doc.write_text(
            "# P17: Another Historical Plan\n\n"
            "## Context\n\n"
            "### Guardrail\n"
            "- Keep generic phase-plan handling intact\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected(*_args, **_kwargs):
            raise AssertionError(
                "heavy Markdown path should not run for bounded historical v1 phase-plan files"
            )

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(p13_doc.resolve()),
            str(p3_doc.resolve()),
            str(unrelated_doc.resolve()),
        }
        with store._get_connection() as conn:
            p13_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v1-p13.md') ORDER BY id"
                ).fetchall()
            ]
            p3_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v1-p3.md') ORDER BY id"
                ).fetchall()
            ]
            unrelated_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v1-p17.md') ORDER BY id"
                ).fetchall()
            ]
            p13_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v1-p13.md')"
            ).fetchone()[0]
            p3_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v1-p3.md')"
            ).fetchone()[0]
            unrelated_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v1-p17.md')"
            ).fetchone()[0]
            p13_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v1-p13.md')"
            ).fetchall()
            p3_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v1-p3.md')"
            ).fetchall()
        store.close()

        assert p13_symbols == [
            "P13: Reindex Durability + Artifact Automation",
            "P13: Reindex Durability + Artifact Automation",
            "Context",
            "What exists today",
            "Interface Freeze Gates",
            "Gate contract",
        ]
        assert p3_symbols == [
            "PHASE-3-per-repo-plugins-stores-memory",
            "PHASE-3-per-repo-plugins-stores-memory",
            "Context",
            "What exists",
            "Lane Index & Dependencies",
            "Wave plan",
        ]
        assert unrelated_symbols == [
            "P17: Another Historical Plan",
            "P17: Another Historical Plan",
            "Context",
            "Guardrail",
        ]
        assert p13_chunk_count == 0
        assert p3_chunk_count == 0
        assert unrelated_chunk_count == 0
        assert len(p13_fts_rows) == 1
        assert len(p3_fts_rows) == 1
        assert "Gate contract" in p13_fts_rows[0][0]
        assert "Wave plan" in p3_fts_rows[0][0]

    def test_index_directory_uses_bounded_markdown_path_for_mixed_version_phase_plan_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        plans_dir = repo / "plans"
        plans_dir.mkdir(parents=True)
        semphasetail_doc = plans_dir / "phase-plan-v7-SEMPHASETAIL.md"
        gagov_doc = plans_dir / "phase-plan-v5-gagov.md"
        unrelated_doc = plans_dir / "phase-plan-v7-SEMUNRELATED.md"
        semphasetail_doc.write_text(
            "---\n"
            "title: Phase Plan v7 SEMPHASETAIL\n"
            "---\n"
            "# SEMPHASETAIL Tail Recovery\n\n"
            "## Context\n\n"
            "### Later blocker\n"
            "- Preserve bounded discoverability\n",
            encoding="utf-8",
        )
        gagov_doc.write_text(
            "---\n"
            "title: Phase Plan v5 GAGOV\n"
            "---\n"
            "# GAGOV: Governance Recovery\n\n"
            "## Context\n\n"
            "### Legacy loop note\n"
            "- Keep `.codex/phase-loop/` prose discoverable\n",
            encoding="utf-8",
        )
        unrelated_doc.write_text(
            "# SEMUNRELATED Notes\n\n"
            "## Scope\n\n"
            "### Guardrail\n"
            "- Keep generic phase-plan handling intact\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected(*_args, **_kwargs):
            raise AssertionError(
                "heavy Markdown path should not run for bounded mixed-version phase-plan files"
            )

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(semphasetail_doc.resolve()),
            str(gagov_doc.resolve()),
            str(unrelated_doc.resolve()),
        }
        with store._get_connection() as conn:
            semphasetail_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v7-SEMPHASETAIL.md') ORDER BY id"
                ).fetchall()
            ]
            gagov_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v5-gagov.md') ORDER BY id"
                ).fetchall()
            ]
            unrelated_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v7-SEMUNRELATED.md') ORDER BY id"
                ).fetchall()
            ]
            semphasetail_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMPHASETAIL.md')"
            ).fetchone()[0]
            gagov_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v5-gagov.md')"
            ).fetchone()[0]
            unrelated_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMUNRELATED.md')"
            ).fetchone()[0]
            semphasetail_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMPHASETAIL.md')"
            ).fetchall()
            gagov_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v5-gagov.md')"
            ).fetchall()
        store.close()

        assert semphasetail_symbols == [
            "Phase Plan v7 SEMPHASETAIL",
            "SEMPHASETAIL Tail Recovery",
            "Context",
            "Later blocker",
        ]
        assert gagov_symbols == [
            "Phase Plan v5 GAGOV",
            "GAGOV: Governance Recovery",
            "Context",
            "Legacy loop note",
        ]
        assert unrelated_symbols == [
            "SEMUNRELATED Notes",
            "SEMUNRELATED Notes",
            "Scope",
            "Guardrail",
        ]
        assert semphasetail_chunk_count == 0
        assert gagov_chunk_count == 0
        assert unrelated_chunk_count == 0
        assert len(semphasetail_fts_rows) == 1
        assert len(gagov_fts_rows) == 1
        assert "Later blocker" in semphasetail_fts_rows[0][0]
        assert ".codex/phase-loop/" in gagov_fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_markdown_path_for_semjedi_p4_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        plans_dir = repo / "plans"
        plans_dir.mkdir(parents=True)
        semjedi_doc = plans_dir / "phase-plan-v7-SEMJEDI.md"
        p4_doc = plans_dir / "phase-plan-v1-p4.md"
        unrelated_doc = plans_dir / "phase-plan-v7-SEMUNRELATED.md"
        semjedi_doc.write_text(
            "---\n"
            "title: Phase Plan v7 SEMJEDI\n"
            "---\n"
            "# SEMJEDI Tail Recovery\n\n"
            "## Context\n\n"
            "### Later blocker\n"
            "- Preserve bounded frontmatter and heading discoverability\n",
            encoding="utf-8",
        )
        p4_doc.write_text(
            '"Historical phase plan"\n\n'
            "# PHASE-4-legacy-plan\n\n"
            "## Context\n\n"
            "### Execution notes\n"
            "- Keep quoted preamble discoverable\n",
            encoding="utf-8",
        )
        unrelated_doc.write_text(
            "# SEMUNRELATED Notes\n\n"
            "## Scope\n\n"
            "### Guardrail\n"
            "- Keep generic phase-plan handling intact\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected(*_args, **_kwargs):
            raise AssertionError(
                "heavy Markdown path should not run for the exact SEMJEDI/P4 phase-plan files"
            )

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        plugin = MarkdownPlugin()
        assert (
            plugin._resolve_lightweight_reason(semjedi_doc, semjedi_doc.read_text(encoding="utf-8"))
            == "mixed_semjedi_phase_plan_path"
        )
        assert (
            plugin._resolve_lightweight_reason(p4_doc, p4_doc.read_text(encoding="utf-8"))
            == "mixed_p4_phase_plan_path"
        )
        assert (
            plugin._resolve_lightweight_reason(
                unrelated_doc, unrelated_doc.read_text(encoding="utf-8")
            )
            == "roadmap_path"
        )

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(semjedi_doc.resolve()),
            str(p4_doc.resolve()),
            str(unrelated_doc.resolve()),
        }
        with store._get_connection() as conn:
            semjedi_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v7-SEMJEDI.md') ORDER BY id"
                ).fetchall()
            ]
            p4_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v1-p4.md') ORDER BY id"
                ).fetchall()
            ]
            unrelated_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v7-SEMUNRELATED.md') ORDER BY id"
                ).fetchall()
            ]
            semjedi_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMJEDI.md')"
            ).fetchone()[0]
            p4_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v1-p4.md')"
            ).fetchone()[0]
            semjedi_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMJEDI.md')"
            ).fetchall()
            p4_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v1-p4.md')"
            ).fetchall()
        store.close()

        assert semjedi_symbols == [
            "Phase Plan v7 SEMJEDI",
            "SEMJEDI Tail Recovery",
            "Context",
            "Later blocker",
        ]
        assert p4_symbols == [
            "PHASE-4-legacy-plan",
            "PHASE-4-legacy-plan",
            "Context",
            "Execution notes",
        ]
        assert unrelated_symbols == [
            "SEMUNRELATED Notes",
            "SEMUNRELATED Notes",
            "Scope",
            "Guardrail",
        ]
        assert semjedi_chunk_count == 0
        assert p4_chunk_count == 0
        assert len(semjedi_fts_rows) == 1
        assert len(p4_fts_rows) == 1
        assert "Later blocker" in semjedi_fts_rows[0][0]
        assert "Execution notes" in p4_fts_rows[0][0]

    def test_index_directory_uses_bounded_markdown_path_for_support_docs_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        docs_dir = repo / "docs"
        docs_dir.mkdir(parents=True)
        toc_doc = docs_dir / "markdown-table-of-contents.md"
        support_doc = docs_dir / "SUPPORT_MATRIX.md"
        unrelated_doc = docs_dir / "OTHER.md"
        toc_doc.write_text(
            "# Code-Index-MCP Documentation Table of Contents\n\n"
            "## Executive Summary\n\n"
            "### Project Overview\n"
            "- Preserve navigation headings\n",
            encoding="utf-8",
        )
        support_doc.write_text(
            "# Code-Index-MCP Support Matrix\n\n"
            "## Claim tiers\n\n"
            "## Machine-checkable support facts\n\n"
            "| Language | Support tier | Runtime behavior | Parser status | Sandbox support | Required extras | Symbol quality | Semantic support | Known limitations |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| Python | GA-supported | Specialized plugin | Tree-sitter plus Jedi | Default sandbox module | Core | High for Python symbols | Optional provider/API key | Dynamic runtime behavior is best effort |\n",
            encoding="utf-8",
        )
        unrelated_doc.write_text(
            "# Other Doc\n\n"
            "## Scope\n\n"
            "### Guardrail\n"
            "- Keep generic Markdown handling intact\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected(*_args, **_kwargs):
            raise AssertionError("heavy Markdown path should not run for bounded support-doc files")

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(toc_doc.resolve()),
            str(support_doc.resolve()),
            str(unrelated_doc.resolve()),
        }
        with store._get_connection() as conn:
            toc_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'docs/markdown-table-of-contents.md') ORDER BY id"
                ).fetchall()
            ]
            support_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'docs/SUPPORT_MATRIX.md') ORDER BY id"
                ).fetchall()
            ]
            unrelated_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'docs/OTHER.md') ORDER BY id"
                ).fetchall()
            ]
            toc_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'docs/markdown-table-of-contents.md')"
            ).fetchone()[0]
            support_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'docs/SUPPORT_MATRIX.md')"
            ).fetchone()[0]
            unrelated_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'docs/OTHER.md')"
            ).fetchone()[0]
            toc_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'docs/markdown-table-of-contents.md')"
            ).fetchall()
            support_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'docs/SUPPORT_MATRIX.md')"
            ).fetchall()
        store.close()

        assert toc_symbols == [
            "Code-Index-MCP Documentation Table of Contents",
            "Code-Index-MCP Documentation Table of Contents",
            "Executive Summary",
            "Project Overview",
        ]
        assert support_symbols == [
            "Code-Index-MCP Support Matrix",
            "Code-Index-MCP Support Matrix",
            "Claim tiers",
            "Machine-checkable support facts",
        ]
        assert "Other Doc" in unrelated_symbols
        assert "Scope" in unrelated_symbols
        assert "Guardrail" in unrelated_symbols
        assert toc_chunk_count == 0
        assert support_chunk_count == 0
        assert unrelated_chunk_count >= 0
        assert len(toc_fts_rows) == 1
        assert len(support_fts_rows) == 1
        assert "Project Overview" in toc_fts_rows[0][0]
        assert "Machine-checkable support facts" in support_fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_path_for_visual_report_script(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        script = repo / "scripts" / "create_multi_repo_visual_report.py"
        script.parent.mkdir(parents=True)
        script.write_text(
            "def setup_style():\n    return 'ok'\n\n\ndef main():\n    return setup_style()\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        def _tracked_chunk_text(*_args, **_kwargs):
            calls.append("called")
            return []

        monkeypatch.setattr(
            "mcp_server.plugins.python_plugin.plugin.chunk_text", _tracked_chunk_text
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(script.resolve())
        with store._get_connection() as conn:
            symbol_names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/create_multi_repo_visual_report.py') ORDER BY id"
                ).fetchall()
            ]
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/create_multi_repo_visual_report.py')"
            ).fetchone()[0]
        store.close()
        assert "setup_style" in symbol_names
        assert "main" in symbol_names
        assert chunk_count == 0
        assert calls == []

    def test_index_directory_uses_exact_bounded_python_path_for_quick_validation_script(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        script = repo / "scripts" / "quick_mcp_vs_native_validation.py"
        script.parent.mkdir(parents=True)
        script.write_text(
            "def compare_results():\n    return 'ok'\n\n\ndef main():\n    return compare_results()\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        def _tracked_chunk_text(*_args, **_kwargs):
            calls.append("called")
            return []

        monkeypatch.setattr(
            "mcp_server.plugins.python_plugin.plugin.chunk_text", _tracked_chunk_text
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(script.resolve())
        with store._get_connection() as conn:
            symbol_names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/quick_mcp_vs_native_validation.py') ORDER BY id"
                ).fetchall()
            ]
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/quick_mcp_vs_native_validation.py')"
            ).fetchone()[0]
            fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/quick_mcp_vs_native_validation.py')"
            ).fetchall()
        store.close()
        assert "compare_results" in symbol_names
        assert "main" in symbol_names
        assert chunk_count == 0
        assert calls == []
        assert len(fts_rows) == 1
        assert "compare_results" in fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_path_for_validate_script(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        script = repo / "scripts" / "validate_mcp_comprehensive.py"
        script.parent.mkdir(parents=True)
        script.write_text(
            "class ValidationSuite:\n"
            "    def run(self):\n"
            "        return True\n\n"
            "def validate_mcp():\n"
            "    return ValidationSuite().run()\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        def _tracked_chunk_text(*_args, **_kwargs):
            calls.append("called")
            return []

        monkeypatch.setattr(
            "mcp_server.plugins.python_plugin.plugin.chunk_text", _tracked_chunk_text
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(script.resolve())
        with store._get_connection() as conn:
            symbol_names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/validate_mcp_comprehensive.py') ORDER BY id"
                ).fetchall()
            ]
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/validate_mcp_comprehensive.py')"
            ).fetchone()[0]
            fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/validate_mcp_comprehensive.py')"
            ).fetchall()
        store.close()
        assert "ValidationSuite" in symbol_names
        assert "run" in symbol_names
        assert "validate_mcp" in symbol_names
        assert chunk_count == 0
        assert calls == []
        assert len(fts_rows) == 1
        assert "validate_mcp" in fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_path_for_artifact_publish_race(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        test_file = repo / "tests" / "test_artifact_publish_race.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text(
            "class TestArtifactPublishRace:\n"
            "    def test_publish_race_boundary(self):\n"
            "        return 'ok'\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        def _tracked_chunk_text(*_args, **_kwargs):
            calls.append("called")
            return []

        monkeypatch.setattr(
            "mcp_server.plugins.python_plugin.plugin.chunk_text", _tracked_chunk_text
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(test_file.resolve())
        with store._get_connection() as conn:
            symbol_names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/test_artifact_publish_race.py') ORDER BY id"
                ).fetchall()
            ]
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/test_artifact_publish_race.py')"
            ).fetchone()[0]
            fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/test_artifact_publish_race.py')"
            ).fetchall()
        store.close()
        assert "TestArtifactPublishRace" in symbol_names
        assert "test_publish_race_boundary" in symbol_names
        assert chunk_count == 0
        assert calls == []
        assert len(fts_rows) == 1
        assert "test_publish_race_boundary" in fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_path_for_run_reranking_tests(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        script = repo / "tests" / "root_tests" / "run_reranking_tests.py"
        script.parent.mkdir(parents=True)
        script.write_text(
            "class TestRerankingBoundary:\n"
            "    def test_root_contract(self):\n"
            "        return 'ok'\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        def _tracked_chunk_text(*_args, **_kwargs):
            calls.append("called")
            return []

        monkeypatch.setattr(
            "mcp_server.plugins.python_plugin.plugin.chunk_text", _tracked_chunk_text
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(script.resolve())
        with store._get_connection() as conn:
            symbol_names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/root_tests/run_reranking_tests.py') ORDER BY id"
                ).fetchall()
            ]
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/root_tests/run_reranking_tests.py')"
            ).fetchone()[0]
            fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/root_tests/run_reranking_tests.py')"
            ).fetchall()
        store.close()
        assert "TestRerankingBoundary" in symbol_names
        assert "test_root_contract" in symbol_names
        assert chunk_count == 0
        assert calls == []
        assert len(fts_rows) == 1
        assert "test_root_contract" in fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_pair_for_swift_database_efficiency_tail(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        tests_dir = repo / "tests" / "root_tests"
        tests_dir.mkdir(parents=True)
        swift_test = tests_dir / "test_swift_plugin.py"
        db_efficiency_test = tests_dir / "test_mcp_database_efficiency.py"
        helper_test = tests_dir / "test_control.py"
        swift_test.write_text(
            "def test_swift_plugin_basic():\n"
            "    assert True\n\n"
            "def test_swift_package_analysis():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        db_efficiency_test.write_text(
            "class DatabaseEfficiencyTester:\n"
            "    pass\n\n"
            "def main():\n"
            "    return DatabaseEfficiencyTester()\n",
            encoding="utf-8",
        )
        helper_test.write_text(
            "def test_control():\n" "    assert True\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(swift_test.resolve()),
            str(db_efficiency_test.resolve()),
            str(helper_test.resolve()),
        }
        with store._get_connection() as conn:
            swift_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/root_tests/test_swift_plugin.py') ORDER BY id"
                ).fetchall()
            ]
            db_efficiency_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/root_tests/test_mcp_database_efficiency.py') ORDER BY id"
                ).fetchall()
            ]
            helper_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/root_tests/test_control.py') ORDER BY id"
                ).fetchall()
            ]
            swift_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/root_tests/test_swift_plugin.py')"
            ).fetchone()[0]
            db_efficiency_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/root_tests/test_mcp_database_efficiency.py')"
            ).fetchone()[0]
            swift_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'tests/root_tests/test_swift_plugin.py'"
            ).fetchone()[0]
            db_efficiency_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'tests/root_tests/test_mcp_database_efficiency.py'"
            ).fetchone()[0]
            helper_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'tests/root_tests/test_control.py'"
            ).fetchone()[0]
            swift_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/root_tests/test_swift_plugin.py')"
            ).fetchall()
            db_efficiency_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/root_tests/test_mcp_database_efficiency.py')"
            ).fetchall()
        store.close()
        assert swift_symbols == ["test_swift_plugin_basic", "test_swift_package_analysis"]
        assert db_efficiency_symbols == ["DatabaseEfficiencyTester", "main"]
        assert helper_symbols == ["test_control"]
        assert swift_chunk_count == 0
        assert db_efficiency_chunk_count == 0
        assert "exact_test_swift_plugin_rebound" in swift_metadata
        assert "exact_test_mcp_database_efficiency_rebound" in db_efficiency_metadata
        assert "exact_test_swift_plugin_rebound" not in helper_metadata
        assert "exact_test_mcp_database_efficiency_rebound" not in helper_metadata
        assert len(swift_fts_rows) == 1
        assert "test_swift_plugin_basic" in swift_fts_rows[0][0]
        assert "test_swift_package_analysis" in swift_fts_rows[0][0]
        assert len(db_efficiency_fts_rows) == 1
        assert "DatabaseEfficiencyTester" in db_efficiency_fts_rows[0][0]
        assert "main" in db_efficiency_fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_pair_for_route_auth_sandbox_tail(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.python_plugin.plugin import Plugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        security_dir = repo / "tests" / "security"
        security_dir.mkdir(parents=True)
        route_auth = security_dir / "test_route_auth_coverage.py"
        sandbox_degradation = security_dir / "test_p24_sandbox_degradation.py"
        control = security_dir / "test_control.py"
        route_auth.write_text(
            "def _protected_routes():\n"
            "    return [('GET', '/search/capabilities')]\n\n"
            "def _has_auth_dependency(route):\n"
            "    return bool(route)\n\n"
            "def test_search_capabilities_requires_auth():\n"
            "    assert True\n\n"
            "def test_all_protected_routes_return_401_without_token():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        sandbox_degradation.write_text(
            "def _caps():\n"
            "    return {'network': False}\n\n"
            "def test_worker_missing_extra_failure_has_capability_state():\n"
            "    assert _caps()['network'] is False\n\n"
            "def test_csharp_aliases_construct_in_sandbox():\n"
            "    assert True\n\n"
            "def test_specific_plugin_alias_exports_construct_in_sandbox():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        control.write_text("def test_control():\n    assert True\n", encoding="utf-8")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(route_auth.resolve()),
            str(sandbox_degradation.resolve()),
            str(control.resolve()),
        }
        with store._get_connection() as conn:
            route_auth_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/security/test_route_auth_coverage.py') ORDER BY id"
                ).fetchall()
            ]
            sandbox_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/security/test_p24_sandbox_degradation.py') ORDER BY id"
                ).fetchall()
            ]
            control_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/security/test_control.py') ORDER BY id"
                ).fetchall()
            ]
            route_auth_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/security/test_route_auth_coverage.py')"
            ).fetchone()[0]
            sandbox_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/security/test_p24_sandbox_degradation.py')"
            ).fetchone()[0]
            route_auth_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'tests/security/test_route_auth_coverage.py'"
            ).fetchone()[0]
            sandbox_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'tests/security/test_p24_sandbox_degradation.py'"
            ).fetchone()[0]
            control_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'tests/security/test_control.py'"
            ).fetchone()[0]
            route_auth_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/security/test_route_auth_coverage.py')"
            ).fetchall()
            sandbox_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/security/test_p24_sandbox_degradation.py')"
            ).fetchall()
        store.close()
        assert route_auth_symbols == [
            "_protected_routes",
            "_has_auth_dependency",
            "test_search_capabilities_requires_auth",
            "test_all_protected_routes_return_401_without_token",
        ]
        assert sandbox_symbols == [
            "_caps",
            "test_worker_missing_extra_failure_has_capability_state",
            "test_csharp_aliases_construct_in_sandbox",
            "test_specific_plugin_alias_exports_construct_in_sandbox",
        ]
        assert control_symbols == ["test_control"]
        assert route_auth_chunk_count == 0
        assert sandbox_chunk_count == 0
        assert "exact_test_route_auth_coverage_rebound" in route_auth_metadata
        assert "exact_test_p24_sandbox_degradation_rebound" in sandbox_metadata
        assert "exact_test_route_auth_coverage_rebound" not in control_metadata
        assert "exact_test_p24_sandbox_degradation_rebound" not in control_metadata
        assert len(route_auth_fts) == 1
        assert "_protected_routes" in route_auth_fts[0][0]
        assert "_has_auth_dependency" in route_auth_fts[0][0]
        assert "test_search_capabilities_requires_auth" in route_auth_fts[0][0]
        assert "test_all_protected_routes_return_401_without_token" in route_auth_fts[0][0]
        assert len(sandbox_fts) == 1
        assert "_caps" in sandbox_fts[0][0]
        assert "test_worker_missing_extra_failure_has_capability_state" in sandbox_fts[0][0]
        assert "test_csharp_aliases_construct_in_sandbox" in sandbox_fts[0][0]
        assert "test_specific_plugin_alias_exports_construct_in_sandbox" in sandbox_fts[0][0]
        assert "tests/security/test_route_auth_coverage.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/security/test_p24_sandbox_degradation.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/security/fixtures/mock_plugin/plugin.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/security/fixtures/mock_plugin/__init__.py" in Plugin._BOUNDED_CHUNK_PATHS

    def test_index_directory_uses_exact_bounded_python_path_for_integration_obs_smoke_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        integration_dir = repo / "tests" / "integration"
        obs_dir = integration_dir / "obs"
        obs_dir.mkdir(parents=True)
        init_file = integration_dir / "__init__.py"
        obs_smoke = obs_dir / "test_obs_smoke.py"
        control_file = obs_dir / "test_control.py"
        init_file.write_text('"""Integration tests for Code-Index-MCP."""\n', encoding="utf-8")
        obs_smoke.write_text(
            "import pytest\n\n"
            "def _find_repo_root():\n"
            "    return 'repo'\n\n"
            "@pytest.fixture(scope='module')\n"
            "def gateway_proc():\n"
            "    return ('proc', 'http://127.0.0.1:8000', 8000)\n\n"
            "@pytest.fixture(scope='module')\n"
            "def admin_token(gateway_proc):\n"
            "    return 'token'\n\n"
            "def test_json_log_parse_rate(gateway_proc):\n"
            "    assert gateway_proc[1].startswith('http')\n\n"
            "def test_metrics_endpoint_reachable(gateway_proc):\n"
            "    assert gateway_proc[2] == 8000\n\n"
            "def test_secret_redaction_via_http(admin_token):\n"
            "    assert admin_token == 'token'\n",
            encoding="utf-8",
        )
        control_file.write_text(
            "def test_control_boundary():\n" "    assert True\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(init_file.resolve()),
            str(obs_smoke.resolve()),
            str(control_file.resolve()),
        }
        with store._get_connection() as conn:
            init_symbol_count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = 'tests/integration/__init__.py')"
            ).fetchone()[0]
            init_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = 'tests/integration/__init__.py')"
            ).fetchone()[0]
            init_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = 'tests/integration/__init__.py')"
            ).fetchall()
            obs_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'tests/integration/obs/test_obs_smoke.py') ORDER BY id"
                ).fetchall()
            ]
            obs_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'tests/integration/obs/test_obs_smoke.py')"
            ).fetchone()[0]
            obs_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'tests/integration/obs/test_obs_smoke.py')"
            ).fetchall()
        store.close()
        assert init_symbol_count == 0
        assert init_chunk_count == 0
        assert len(init_fts_rows) == 1
        assert "Integration tests for Code-Index-MCP" in init_fts_rows[0][0]
        assert "_find_repo_root" in obs_symbols
        assert "gateway_proc" in obs_symbols
        assert "admin_token" in obs_symbols
        assert "test_json_log_parse_rate" in obs_symbols
        assert "test_metrics_endpoint_reachable" in obs_symbols
        assert "test_secret_redaction_via_http" in obs_symbols
        assert obs_chunk_count == 0
        assert len(obs_fts_rows) == 1
        assert "test_secret_redaction_via_http" in obs_fts_rows[0][0]
        assert "tests/integration/__init__.py" in _EXACT_BOUNDED_PYTHON_PATHS
        assert "tests/integration/obs/test_obs_smoke.py" in _EXACT_BOUNDED_PYTHON_PATHS
        assert "tests/integration/obs/test_control.py" not in _EXACT_BOUNDED_PYTHON_PATHS

    def test_index_directory_uses_exact_bounded_python_path_for_migration_script(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        script = repo / "scripts" / "migrate_large_index_to_multi_repo.py"
        script.parent.mkdir(parents=True)
        script.write_text(
            "class RepositoryMigration:\n"
            "    pass\n\n"
            "class LargeIndexMigrator:\n"
            "    def migrate_repository(self):\n"
            "        return 'ok'\n\n"
            "def main():\n"
            "    return LargeIndexMigrator()\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        def _tracked_chunk_text(*_args, **_kwargs):
            calls.append("called")
            return []

        monkeypatch.setattr(
            "mcp_server.plugins.python_plugin.plugin.chunk_text", _tracked_chunk_text
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(script.resolve())
        with store._get_connection() as conn:
            symbol_names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/migrate_large_index_to_multi_repo.py') ORDER BY id"
                ).fetchall()
            ]
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/migrate_large_index_to_multi_repo.py')"
            ).fetchone()[0]
            fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/migrate_large_index_to_multi_repo.py')"
            ).fetchall()
        store.close()
        assert "RepositoryMigration" in symbol_names
        assert "LargeIndexMigrator" in symbol_names
        assert "migrate_repository" in symbol_names
        assert "main" in symbol_names
        assert chunk_count == 0
        assert calls == []
        assert len(fts_rows) == 1
        assert "LargeIndexMigrator" in fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_path_for_script_language_audit(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        script = repo / "scripts" / "check_index_languages.py"
        script.parent.mkdir(parents=True)
        script.write_text(
            "print('Checking languages')\n"
            "tables = ['files']\n"
            "for table in tables:\n"
            "    print(table)\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        def _tracked_chunk_text(*_args, **_kwargs):
            calls.append("called")
            return []

        monkeypatch.setattr(
            "mcp_server.plugins.python_plugin.plugin.chunk_text", _tracked_chunk_text
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(script.resolve())
        with store._get_connection() as conn:
            symbol_count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/check_index_languages.py')"
            ).fetchone()[0]
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/check_index_languages.py')"
            ).fetchone()[0]
            fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/check_index_languages.py')"
            ).fetchall()
        store.close()
        assert symbol_count == 0
        assert chunk_count == 0
        assert calls == []
        assert len(fts_rows) == 1
        assert "Checking languages" in fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_script_pair_for_preflight_upgrade_tail(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        shell_script = repo / "scripts" / "preflight_upgrade.sh"
        python_script = repo / "scripts" / "test_mcp_protocol_direct.py"
        shell_script.parent.mkdir(parents=True)
        shell_script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'exec "$PYTHON" -m mcp_server.cli preflight_env "$1"\n',
            encoding="utf-8",
        )
        python_script.write_text(
            "import asyncio\n\n"
            "async def test_mcp_protocol():\n"
            "    return 'ok'\n\n"
            "if __name__ == '__main__':\n"
            "    asyncio.run(test_mcp_protocol())\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        def _tracked_chunk_text(*_args, **_kwargs):
            calls.append("called")
            return []

        monkeypatch.setattr(
            "mcp_server.plugins.python_plugin.plugin.chunk_text", _tracked_chunk_text
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 2
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(python_script.resolve())
        with store._get_connection() as conn:
            shell_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/preflight_upgrade.sh') ORDER BY id"
                ).fetchall()
            ]
            python_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/test_mcp_protocol_direct.py') ORDER BY id"
                ).fetchall()
            ]
            shell_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/preflight_upgrade.sh')"
            ).fetchone()[0]
            python_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/test_mcp_protocol_direct.py')"
            ).fetchone()[0]
            shell_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/preflight_upgrade.sh')"
            ).fetchall()
            python_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/test_mcp_protocol_direct.py')"
            ).fetchall()
        store.close()
        assert shell_symbols == ["preflight_env"]
        assert "test_mcp_protocol" in python_symbols
        assert shell_chunk_count == 0
        assert python_chunk_count == 0
        assert calls == []
        assert len(shell_fts_rows) == 1
        assert "preflight_env" in shell_fts_rows[0][0]
        assert len(python_fts_rows) == 1
        assert "test_mcp_protocol" in python_fts_rows[0][0]

    def test_index_directory_keeps_unrelated_scripts_off_exact_preflight_upgrade_bypass(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        exact_shell = repo / "scripts" / "preflight_upgrade.sh"
        unrelated_shell = repo / "scripts" / "deploy_helper.sh"
        unrelated_python = repo / "scripts" / "helper.py"
        exact_shell.parent.mkdir(parents=True)
        exact_shell.write_text(
            "#!/usr/bin/env bash\n" 'exec "$PYTHON" -m mcp_server.cli preflight_env "$1"\n',
            encoding="utf-8",
        )
        unrelated_shell.write_text("#!/usr/bin/env bash\necho helper\n", encoding="utf-8")
        unrelated_python.write_text("def helper():\n    return 'ok'\n", encoding="utf-8")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        def _tracked_chunk_text(*_args, **_kwargs):
            calls.append("called")
            return []

        monkeypatch.setattr(
            "mcp_server.plugins.python_plugin.plugin.chunk_text", _tracked_chunk_text
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 2
        assert result["ignored_files"] == 1
        with store._get_connection() as conn:
            exact_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/preflight_upgrade.sh'"
            ).fetchone()[0]
            unrelated_shell_row = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/deploy_helper.sh'"
            ).fetchone()
            unrelated_python_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/helper.py')"
            ).fetchone()[0]
        store.close()
        assert "exact_preflight_upgrade_rebound" in exact_metadata
        assert unrelated_shell_row is None
        assert unrelated_python_chunk_count > 0

    def test_index_directory_uses_exact_bounded_python_pair_for_verify_simulator_tail(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        verify_script = repo / "scripts" / "verify_embeddings.py"
        simulator_script = repo / "scripts" / "claude_code_behavior_simulator.py"
        helper_script = repo / "scripts" / "helper.py"
        verify_script.parent.mkdir(parents=True)
        verify_script.write_text(
            "def verify_embeddings():\n" "    return 'verified'\n",
            encoding="utf-8",
        )
        simulator_script.write_text(
            "class ClaudeCodeSimulator:\n"
            "    def run_scenario(self):\n"
            "        return 'simulated'\n",
            encoding="utf-8",
        )
        helper_script.write_text(
            "def helper():\n" "    return 'helper'\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(helper_script.resolve()),
            str(simulator_script.resolve()),
        }
        with store._get_connection() as conn:
            verify_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/verify_embeddings.py') ORDER BY id"
                ).fetchall()
            ]
            simulator_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/claude_code_behavior_simulator.py') ORDER BY id"
                ).fetchall()
            ]
            helper_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/helper.py') ORDER BY id"
                ).fetchall()
            ]
            verify_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/verify_embeddings.py')"
            ).fetchone()[0]
            simulator_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/claude_code_behavior_simulator.py')"
            ).fetchone()[0]
            verify_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/verify_embeddings.py'"
            ).fetchone()[0]
            simulator_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/claude_code_behavior_simulator.py'"
            ).fetchone()[0]
            helper_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/helper.py'"
            ).fetchone()[0]
            verify_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/verify_embeddings.py')"
            ).fetchall()
            simulator_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/claude_code_behavior_simulator.py')"
            ).fetchall()
        store.close()
        assert verify_symbols == ["verify_embeddings"]
        assert simulator_symbols == ["ClaudeCodeSimulator", "run_scenario"]
        assert helper_symbols == ["helper"]
        assert verify_chunk_count == 0
        assert simulator_chunk_count == 0
        assert "exact_verify_embeddings_rebound" in verify_metadata
        assert "exact_claude_code_behavior_simulator_rebound" in simulator_metadata
        assert "exact_verify_embeddings_rebound" not in helper_metadata
        assert "exact_claude_code_behavior_simulator_rebound" not in helper_metadata
        assert len(verify_fts_rows) == 1
        assert "verify_embeddings" in verify_fts_rows[0][0]
        assert len(simulator_fts_rows) == 1
        assert "ClaudeCodeSimulator" in simulator_fts_rows[0][0]

    def test_index_directory_uses_bounded_markdown_path_for_rebound_phase_plan_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        plans_dir = repo / "plans"
        plans_dir.mkdir(parents=True)
        verifysim_doc = plans_dir / "phase-plan-v7-SEMVERIFYSIMTAIL.md"
        apidocs_doc = plans_dir / "phase-plan-v7-SEMAPIDOCSTAIL.md"
        unrelated_doc = plans_dir / "phase-plan-v7-SEMUNRELATED.md"
        verifysim_doc.write_text(
            "---\n"
            "title: Phase Plan v7 SEMVERIFYSIMTAIL\n"
            "---\n"
            "# SEMVERIFYSIMTAIL Live Rerun Check\n\n"
            "## Context\n\n"
            "### Later Python-script blocker\n"
            "- Preserve bounded discoverability\n\n"
            "## Interface Freeze Gates\n\n"
            "### Tight contract\n"
            "- Keep frontmatter-derived title discoverable\n\n"
            "## Lane Index & Dependencies\n\n"
            "### Serial recovery\n"
            "- Keep heading discoverability bounded\n\n"
            "## Verification\n\n"
            "### Rerun proof\n"
            "- Preserve lexical evidence\n\n"
            "## Acceptance Criteria\n\n"
            "### Steering\n"
            "- Keep document-level discoverability\n\n"
            "## Automation Handoff\n\n"
            "### Downstream note\n"
            "- Preserve rebound phase context\n",
            encoding="utf-8",
        )
        apidocs_doc.write_text(
            "---\n"
            "title: Phase Plan v7 SEMAPIDOCSTAIL\n"
            "---\n"
            "# SEMAPIDOCSTAIL Live Rerun Check\n\n"
            "## Context\n\n"
            "### Earlier docs blocker\n"
            "- Preserve bounded discoverability\n\n"
            "## Interface Freeze Gates\n\n"
            "### Exact seam\n"
            "- Preserve title and heading discoverability\n\n"
            "## Lane Index & Dependencies\n\n"
            "### Sequential proof\n"
            "- Keep lexical storage discoverable\n\n"
            "## Verification\n\n"
            "### Status proof\n"
            "- Preserve operator headings\n\n"
            "## Acceptance Criteria\n\n"
            "### Rebound verdict\n"
            "- Keep file-level storage discoverable\n\n"
            "## Automation Handoff\n\n"
            "### Stale downstream warning\n"
            "- Preserve rebound phase context\n",
            encoding="utf-8",
        )
        unrelated_doc.write_text(
            "# SEMUNRELATED Notes\n\n"
            "## Scope\n\n"
            "### Guardrail\n"
            "- Keep generic phase-plan handling intact\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected(*_args, **_kwargs):
            raise AssertionError("heavy Markdown path should not run for rebound phase-plan files")

        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        plugin = MarkdownPlugin()
        assert (
            plugin._resolve_lightweight_reason(
                verifysim_doc, verifysim_doc.read_text(encoding="utf-8")
            )
            == "roadmap_path"
        )
        assert (
            plugin._resolve_lightweight_reason(apidocs_doc, apidocs_doc.read_text(encoding="utf-8"))
            == "roadmap_path"
        )
        assert (
            plugin._resolve_lightweight_reason(
                unrelated_doc, unrelated_doc.read_text(encoding="utf-8")
            )
            == "roadmap_path"
        )

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(verifysim_doc.resolve()),
            str(apidocs_doc.resolve()),
            str(unrelated_doc.resolve()),
        }
        with store._get_connection() as conn:
            verifysim_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v7-SEMVERIFYSIMTAIL.md') ORDER BY id"
                ).fetchall()
            ]
            apidocs_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v7-SEMAPIDOCSTAIL.md') ORDER BY id"
                ).fetchall()
            ]
            unrelated_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'plans/phase-plan-v7-SEMUNRELATED.md') ORDER BY id"
                ).fetchall()
            ]
            verifysim_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMVERIFYSIMTAIL.md')"
            ).fetchone()[0]
            apidocs_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMAPIDOCSTAIL.md')"
            ).fetchone()[0]
            unrelated_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMUNRELATED.md')"
            ).fetchone()[0]
            verifysim_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMVERIFYSIMTAIL.md')"
            ).fetchall()
            apidocs_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'plans/phase-plan-v7-SEMAPIDOCSTAIL.md')"
            ).fetchall()
        store.close()

        assert verifysim_symbols == [
            "Phase Plan v7 SEMVERIFYSIMTAIL",
            "SEMVERIFYSIMTAIL Live Rerun Check",
            "Context",
            "Later Python-script blocker",
            "Interface Freeze Gates",
            "Tight contract",
            "Lane Index & Dependencies",
            "Serial recovery",
            "Verification",
            "Rerun proof",
            "Acceptance Criteria",
            "Steering",
            "Automation Handoff",
            "Downstream note",
        ]
        assert apidocs_symbols == [
            "Phase Plan v7 SEMAPIDOCSTAIL",
            "SEMAPIDOCSTAIL Live Rerun Check",
            "Context",
            "Earlier docs blocker",
            "Interface Freeze Gates",
            "Exact seam",
            "Lane Index & Dependencies",
            "Sequential proof",
            "Verification",
            "Status proof",
            "Acceptance Criteria",
            "Rebound verdict",
            "Automation Handoff",
            "Stale downstream warning",
        ]
        assert unrelated_symbols == [
            "SEMUNRELATED Notes",
            "SEMUNRELATED Notes",
            "Scope",
            "Guardrail",
        ]
        assert verifysim_chunk_count == 0
        assert apidocs_chunk_count == 0
        assert unrelated_chunk_count == 0
        assert len(verifysim_fts_rows) == 1
        assert len(apidocs_fts_rows) == 1
        assert "Automation Handoff" in verifysim_fts_rows[0][0]
        assert "Acceptance Criteria" in apidocs_fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_pair_for_embed_consolidation_tail(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        embed_script = repo / "scripts" / "create_semantic_embeddings.py"
        consolidation_script = repo / "scripts" / "consolidate_real_performance_data.py"
        helper_script = repo / "scripts" / "helper.py"
        embed_script.parent.mkdir(parents=True)
        embed_script.write_text(
            "def get_repository_info():\n"
            "    return {}\n\n"
            "def process_repository():\n"
            "    return get_repository_info()\n",
            encoding="utf-8",
        )
        consolidation_script.write_text(
            "class ConsolidatedResult:\n"
            "    pass\n\n"
            "class PerformanceDataConsolidator:\n"
            "    def consolidate(self):\n"
            "        return ConsolidatedResult()\n",
            encoding="utf-8",
        )
        helper_script.write_text(
            "def helper():\n" "    return 'helper'\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(embed_script.resolve()),
            str(helper_script.resolve()),
            str(consolidation_script.resolve()),
        }
        with store._get_connection() as conn:
            embed_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/create_semantic_embeddings.py') ORDER BY id"
                ).fetchall()
            ]
            consolidation_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/consolidate_real_performance_data.py') ORDER BY id"
                ).fetchall()
            ]
            helper_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/helper.py') ORDER BY id"
                ).fetchall()
            ]
            embed_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/create_semantic_embeddings.py')"
            ).fetchone()[0]
            consolidation_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/consolidate_real_performance_data.py')"
            ).fetchone()[0]
            embed_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/create_semantic_embeddings.py'"
            ).fetchone()[0]
            consolidation_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/consolidate_real_performance_data.py'"
            ).fetchone()[0]
            helper_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/helper.py'"
            ).fetchone()[0]
            embed_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/create_semantic_embeddings.py')"
            ).fetchall()
            consolidation_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/consolidate_real_performance_data.py')"
            ).fetchall()
        store.close()
        assert embed_symbols == ["get_repository_info", "process_repository"]
        assert consolidation_symbols == [
            "ConsolidatedResult",
            "PerformanceDataConsolidator",
            "consolidate",
        ]
        assert helper_symbols == ["helper"]
        assert embed_chunk_count == 0
        assert consolidation_chunk_count == 0
        assert "exact_create_semantic_embeddings_rebound" in embed_metadata
        assert "exact_consolidate_real_performance_data_rebound" in consolidation_metadata
        assert "exact_create_semantic_embeddings_rebound" not in helper_metadata
        assert "exact_consolidate_real_performance_data_rebound" not in helper_metadata
        assert len(embed_fts_rows) == 1
        assert "get_repository_info" in embed_fts_rows[0][0]
        assert "process_repository" in embed_fts_rows[0][0]
        assert len(consolidation_fts_rows) == 1
        assert "ConsolidatedResult" in consolidation_fts_rows[0][0]
        assert "PerformanceDataConsolidator" in consolidation_fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_pair_for_test_repo_index_tail(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        schema_script = repo / "scripts" / "check_test_index_schema.py"
        ensure_script = repo / "scripts" / "ensure_test_repos_indexed.py"
        helper_script = repo / "scripts" / "helper.py"
        schema_script.parent.mkdir(parents=True)
        schema_script.write_text(
            "def check_schema(db_path):\n"
            "    return db_path\n\n"
            "def main():\n"
            "    return check_schema('db')\n",
            encoding="utf-8",
        )
        ensure_script.write_text(
            "def check_index_exists(repo_info):\n"
            "    return bool(repo_info)\n\n"
            "def index_repository(repo_info):\n"
            "    return repo_info\n\n"
            "def main():\n"
            "    return index_repository({})\n",
            encoding="utf-8",
        )
        helper_script.write_text(
            "def helper():\n" "    return 'helper'\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(schema_script.resolve()),
            str(helper_script.resolve()),
            str(ensure_script.resolve()),
        }
        with store._get_connection() as conn:
            schema_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/check_test_index_schema.py') ORDER BY id"
                ).fetchall()
            ]
            ensure_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/ensure_test_repos_indexed.py') ORDER BY id"
                ).fetchall()
            ]
            helper_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/helper.py') ORDER BY id"
                ).fetchall()
            ]
            schema_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/check_test_index_schema.py')"
            ).fetchone()[0]
            ensure_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/ensure_test_repos_indexed.py')"
            ).fetchone()[0]
            schema_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/check_test_index_schema.py'"
            ).fetchone()[0]
            ensure_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/ensure_test_repos_indexed.py'"
            ).fetchone()[0]
            helper_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/helper.py'"
            ).fetchone()[0]
            schema_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/check_test_index_schema.py')"
            ).fetchall()
            ensure_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/ensure_test_repos_indexed.py')"
            ).fetchall()
        store.close()
        assert schema_symbols == ["check_schema", "main"]
        assert ensure_symbols == ["check_index_exists", "index_repository", "main"]
        assert helper_symbols == ["helper"]
        assert schema_chunk_count == 0
        assert ensure_chunk_count == 0
        assert "exact_check_test_index_schema_rebound" in schema_metadata
        assert "exact_ensure_test_repos_indexed_rebound" in ensure_metadata
        assert "exact_check_test_index_schema_rebound" not in helper_metadata
        assert "exact_ensure_test_repos_indexed_rebound" not in helper_metadata
        assert len(schema_fts_rows) == 1
        assert "check_schema" in schema_fts_rows[0][0]
        assert len(ensure_fts_rows) == 1
        assert "check_index_exists" in ensure_fts_rows[0][0]
        assert "index_repository" in ensure_fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_pair_for_missing_repo_semantic_tail(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        missing_repo_script = repo / "scripts" / "index_missing_repos_semantic.py"
        working_indexes_script = repo / "scripts" / "identify_working_indexes.py"
        helper_script = repo / "scripts" / "helper.py"
        missing_repo_script.parent.mkdir(parents=True)
        missing_repo_script.write_text(
            "def get_existing_collections():\n"
            "    return {}\n\n"
            "def find_missing_repositories():\n"
            "    return []\n\n"
            "def estimate_repo_size(repo_name):\n"
            "    return len(repo_name)\n\n"
            "def main():\n"
            "    return find_missing_repositories()\n",
            encoding="utf-8",
        )
        working_indexes_script.write_text(
            "def get_repo_hash(repo_path):\n"
            "    return str(repo_path)\n\n"
            "def check_index_health(db_path):\n"
            "    return True, {'path': str(db_path)}\n\n"
            "def find_test_repos():\n"
            "    return []\n\n"
            "def main():\n"
            "    return find_test_repos()\n",
            encoding="utf-8",
        )
        helper_script.write_text(
            "def helper():\n" "    return 'helper'\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(missing_repo_script.resolve()),
            str(helper_script.resolve()),
            str(working_indexes_script.resolve()),
        }
        with store._get_connection() as conn:
            missing_repo_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/index_missing_repos_semantic.py') ORDER BY id"
                ).fetchall()
            ]
            working_indexes_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/identify_working_indexes.py') ORDER BY id"
                ).fetchall()
            ]
            helper_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/helper.py') ORDER BY id"
                ).fetchall()
            ]
            missing_repo_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/index_missing_repos_semantic.py')"
            ).fetchone()[0]
            working_indexes_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/identify_working_indexes.py')"
            ).fetchone()[0]
            missing_repo_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/index_missing_repos_semantic.py'"
            ).fetchone()[0]
            working_indexes_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/identify_working_indexes.py'"
            ).fetchone()[0]
            helper_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/helper.py'"
            ).fetchone()[0]
            missing_repo_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/index_missing_repos_semantic.py')"
            ).fetchall()
            working_indexes_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/identify_working_indexes.py')"
            ).fetchall()
        store.close()
        assert missing_repo_symbols == [
            "get_existing_collections",
            "find_missing_repositories",
            "estimate_repo_size",
            "main",
        ]
        assert working_indexes_symbols == [
            "get_repo_hash",
            "check_index_health",
            "find_test_repos",
            "main",
        ]
        assert helper_symbols == ["helper"]
        assert missing_repo_chunk_count == 0
        assert working_indexes_chunk_count == 0
        assert "exact_index_missing_repos_semantic_rebound" in missing_repo_metadata
        assert "exact_identify_working_indexes_rebound" in working_indexes_metadata
        assert "exact_index_missing_repos_semantic_rebound" not in helper_metadata
        assert "exact_identify_working_indexes_rebound" not in helper_metadata
        assert len(missing_repo_fts_rows) == 1
        assert "get_existing_collections" in missing_repo_fts_rows[0][0]
        assert "find_missing_repositories" in missing_repo_fts_rows[0][0]
        assert len(working_indexes_fts_rows) == 1
        assert "get_repo_hash" in working_indexes_fts_rows[0][0]
        assert "check_index_health" in working_indexes_fts_rows[0][0]
        assert "find_test_repos" in working_indexes_fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_pair_for_utility_verification_tail(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        utility_dir = repo / "scripts" / "utilities"
        prepare_script = utility_dir / "prepare_index_for_upload.py"
        verify_script = utility_dir / "verify_tool_usage.py"
        helper_script = utility_dir / "helper.py"
        utility_dir.mkdir(parents=True)
        prepare_script.write_text(
            "def get_repo_hash():\n"
            "    return 'hash'\n\n"
            "def find_current_index(indexes_dir, repo_hash):\n"
            "    return indexes_dir / repo_hash\n\n"
            "def prepare_for_upload(index_dir, staging_dir):\n"
            "    return True, f'{index_dir}:{staging_dir}'\n\n"
            "def main():\n"
            "    return prepare_for_upload('index', 'staging')\n",
            encoding="utf-8",
        )
        verify_script.write_text(
            "class ToolUsageAnalyzer:\n"
            "    def parse_tool_sequence(self, session_log):\n"
            "        return [session_log]\n\n"
            "def create_mock_session_log():\n"
            "    return 'mock-session-log'\n",
            encoding="utf-8",
        )
        helper_script.write_text(
            "def helper():\n" "    return 'helper'\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(prepare_script.resolve()),
            str(helper_script.resolve()),
            str(verify_script.resolve()),
        }
        with store._get_connection() as conn:
            prepare_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/utilities/prepare_index_for_upload.py') ORDER BY id"
                ).fetchall()
            ]
            verify_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/utilities/verify_tool_usage.py') ORDER BY id"
                ).fetchall()
            ]
            helper_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/utilities/helper.py') ORDER BY id"
                ).fetchall()
            ]
            prepare_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/utilities/prepare_index_for_upload.py')"
            ).fetchone()[0]
            verify_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/utilities/verify_tool_usage.py')"
            ).fetchone()[0]
            prepare_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/utilities/prepare_index_for_upload.py'"
            ).fetchone()[0]
            verify_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/utilities/verify_tool_usage.py'"
            ).fetchone()[0]
            helper_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/utilities/helper.py'"
            ).fetchone()[0]
            prepare_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/utilities/prepare_index_for_upload.py')"
            ).fetchall()
            verify_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/utilities/verify_tool_usage.py')"
            ).fetchall()
        store.close()
        assert prepare_symbols == [
            "get_repo_hash",
            "find_current_index",
            "prepare_for_upload",
            "main",
        ]
        assert verify_symbols == [
            "ToolUsageAnalyzer",
            "parse_tool_sequence",
            "create_mock_session_log",
        ]
        assert helper_symbols == ["helper"]
        assert prepare_chunk_count == 0
        assert verify_chunk_count == 0
        assert "exact_prepare_index_for_upload_rebound" in prepare_metadata
        assert "exact_verify_tool_usage_rebound" in verify_metadata
        assert "exact_prepare_index_for_upload_rebound" not in helper_metadata
        assert "exact_verify_tool_usage_rebound" not in helper_metadata
        assert len(prepare_fts_rows) == 1
        assert "get_repo_hash" in prepare_fts_rows[0][0]
        assert "prepare_for_upload" in prepare_fts_rows[0][0]
        assert len(verify_fts_rows) == 1
        assert "ToolUsageAnalyzer" in verify_fts_rows[0][0]
        assert "create_mock_session_log" in verify_fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_pair_for_qdrant_report_tail(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        scripts_dir = repo / "scripts"
        map_script = scripts_dir / "map_repos_to_qdrant.py"
        report_script = scripts_dir / "create_claude_code_aware_report.py"
        helper_script = scripts_dir / "helper.py"
        scripts_dir.mkdir(parents=True)
        map_script.write_text(
            "def find_all_repositories():\n"
            "    return ['repo-a']\n\n"
            "def summarize_collection_points(collections):\n"
            "    return len(collections)\n",
            encoding="utf-8",
        )
        report_script.write_text(
            "def setup_style():\n"
            "    return 'style'\n\n"
            "def load_benchmark_data():\n"
            "    return {'repos': 1}\n\n"
            "def create_claude_code_pipeline_comparison():\n"
            "    return 'pipeline'\n\n"
            "def create_real_world_examples(data):\n"
            "    return data\n\n"
            "def create_token_breakdown_analysis(data):\n"
            "    return data\n\n"
            "def create_claude_code_instructions_visual():\n"
            "    return 'instructions'\n\n"
            "def create_comprehensive_report():\n"
            "    return 'report'\n\n"
            "def main():\n"
            "    return create_comprehensive_report()\n",
            encoding="utf-8",
        )
        helper_script.write_text(
            "def helper():\n" "    return 'helper'\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(map_script.resolve()),
            str(helper_script.resolve()),
            str(report_script.resolve()),
        }
        with store._get_connection() as conn:
            map_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/map_repos_to_qdrant.py') ORDER BY id"
                ).fetchall()
            ]
            report_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/create_claude_code_aware_report.py') ORDER BY id"
                ).fetchall()
            ]
            helper_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/helper.py') ORDER BY id"
                ).fetchall()
            ]
            map_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/map_repos_to_qdrant.py')"
            ).fetchone()[0]
            report_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/create_claude_code_aware_report.py')"
            ).fetchone()[0]
            map_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/map_repos_to_qdrant.py'"
            ).fetchone()[0]
            report_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/create_claude_code_aware_report.py'"
            ).fetchone()[0]
            helper_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/helper.py'"
            ).fetchone()[0]
            map_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/map_repos_to_qdrant.py')"
            ).fetchall()
            report_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/create_claude_code_aware_report.py')"
            ).fetchall()
        store.close()
        assert map_symbols == [
            "find_all_repositories",
            "summarize_collection_points",
        ]
        assert report_symbols == [
            "setup_style",
            "load_benchmark_data",
            "create_claude_code_pipeline_comparison",
            "create_real_world_examples",
            "create_token_breakdown_analysis",
            "create_claude_code_instructions_visual",
            "create_comprehensive_report",
            "main",
        ]
        assert helper_symbols == ["helper"]
        assert map_chunk_count == 0
        assert report_chunk_count == 0
        assert "exact_map_repos_to_qdrant_rebound" in map_metadata
        assert "exact_create_claude_code_aware_report_rebound" in report_metadata
        assert "exact_map_repos_to_qdrant_rebound" not in helper_metadata
        assert "exact_create_claude_code_aware_report_rebound" not in helper_metadata
        assert len(map_fts_rows) == 1
        assert "find_all_repositories" in map_fts_rows[0][0]
        assert "summarize_collection_points" in map_fts_rows[0][0]
        assert len(report_fts_rows) == 1
        assert "setup_style" in report_fts_rows[0][0]
        assert "create_comprehensive_report" in report_fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_pair_for_optimized_upload_tail(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        scripts_dir = repo / "scripts"
        analysis_script = scripts_dir / "execute_optimized_analysis.py"
        upload_script = scripts_dir / "index-artifact-upload-v2.py"
        helper_script = scripts_dir / "helper.py"
        scripts_dir.mkdir(parents=True)
        analysis_script.write_text(
            "class OptimizedAnalysisMetrics:\n"
            "    def as_dict(self):\n"
            "        return {'ok': True}\n\n"
            "class OptimizedAnalysisExecutor:\n"
            "    async def execute_optimized_analysis(self):\n"
            "        return {'status': 'ok'}\n\n"
            "    def _generate_final_results(self):\n"
            "        return {'final': True}\n\n"
            "    def _generate_executive_summary_markdown(self):\n"
            "        return '# Summary'\n\n"
            "async def main():\n"
            "    return await OptimizedAnalysisExecutor().execute_optimized_analysis()\n",
            encoding="utf-8",
        )
        upload_script.write_text(
            "class CompatibilityAwareUploader:\n"
            "    def generate_compatibility_hash(self):\n"
            "        return 'hash'\n\n"
            "    def compress_indexes(self):\n"
            "        return ('archive', 'checksum', 1)\n\n"
            "    def create_metadata(self):\n"
            "        return {'ok': True}\n\n"
            "    def trigger_workflow_upload(self):\n"
            "        return True\n\n"
            "def main():\n"
            "    return CompatibilityAwareUploader().create_metadata()\n",
            encoding="utf-8",
        )
        helper_script.write_text(
            "def helper():\n" "    return 'helper'\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(analysis_script.resolve()),
            str(helper_script.resolve()),
            str(upload_script.resolve()),
        }
        with store._get_connection() as conn:
            analysis_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/execute_optimized_analysis.py') ORDER BY id"
                ).fetchall()
            ]
            upload_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/index-artifact-upload-v2.py') ORDER BY id"
                ).fetchall()
            ]
            helper_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/helper.py') ORDER BY id"
                ).fetchall()
            ]
            analysis_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/execute_optimized_analysis.py')"
            ).fetchone()[0]
            upload_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/index-artifact-upload-v2.py')"
            ).fetchone()[0]
            analysis_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/execute_optimized_analysis.py'"
            ).fetchone()[0]
            upload_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/index-artifact-upload-v2.py'"
            ).fetchone()[0]
            helper_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/helper.py'"
            ).fetchone()[0]
            analysis_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/execute_optimized_analysis.py')"
            ).fetchall()
            upload_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/index-artifact-upload-v2.py')"
            ).fetchall()
        store.close()
        assert analysis_symbols == [
            "OptimizedAnalysisMetrics",
            "as_dict",
            "OptimizedAnalysisExecutor",
            "execute_optimized_analysis",
            "_generate_final_results",
            "_generate_executive_summary_markdown",
            "main",
        ]
        assert upload_symbols == [
            "CompatibilityAwareUploader",
            "generate_compatibility_hash",
            "compress_indexes",
            "create_metadata",
            "trigger_workflow_upload",
            "main",
        ]
        assert helper_symbols == ["helper"]
        assert analysis_chunk_count == 0
        assert upload_chunk_count == 0
        assert "exact_execute_optimized_analysis_rebound" in analysis_metadata
        assert "exact_index_artifact_upload_v2_rebound" in upload_metadata
        assert "exact_execute_optimized_analysis_rebound" not in helper_metadata
        assert "exact_index_artifact_upload_v2_rebound" not in helper_metadata
        assert len(analysis_fts_rows) == 1
        assert "OptimizedAnalysisExecutor" in analysis_fts_rows[0][0]
        assert "_generate_executive_summary_markdown" in analysis_fts_rows[0][0]
        assert len(upload_fts_rows) == 1
        assert "CompatibilityAwareUploader" in upload_fts_rows[0][0]
        assert "trigger_workflow_upload" in upload_fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_pair_for_edit_retrieval_tail(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        scripts_dir = repo / "scripts"
        analysis_script = scripts_dir / "analyze_claude_code_edits.py"
        retrieval_script = scripts_dir / "verify_mcp_retrieval.py"
        helper_script = scripts_dir / "helper.py"
        scripts_dir.mkdir(parents=True)
        analysis_script.write_text(
            "class ClaudeCodeTranscriptAnalyzer:\n"
            "    def parse_jsonl_chunk(self, chunk):\n"
            "        return [chunk]\n\n"
            "    def extract_tool_sequence(self, events):\n"
            "        return events\n\n"
            "    def identify_edit_patterns(self, tool_sequence):\n"
            "        return tool_sequence\n\n"
            "    def calculate_edit_metrics(self, patterns):\n"
            "        return {'count': len(patterns)}\n\n"
            "    async def analyze_transcript_file(self, file_path):\n"
            "        return {'path': str(file_path)}\n\n"
            "    async def analyze_all_transcripts(self, transcript_dir):\n"
            "        return {'dir': str(transcript_dir)}\n\n"
            "    def aggregate_results(self, results):\n"
            "        return {'results': len(results)}\n\n"
            "    def generate_insights(self, metrics):\n"
            "        return metrics\n\n"
            "async def main():\n"
            "    return await ClaudeCodeTranscriptAnalyzer().analyze_all_transcripts('logs')\n",
            encoding="utf-8",
        )
        retrieval_script.write_text(
            "def test_mcp_tool(tool_name, args):\n"
            "    return {'tool': tool_name, 'args': args}\n\n"
            "def verify_sql_search():\n"
            "    return {'mode': 'sql'}\n\n"
            "def verify_semantic_search():\n"
            "    return {'mode': 'semantic'}\n\n"
            "def verify_hybrid_search():\n"
            "    return {'mode': 'hybrid'}\n\n"
            "def test_direct_mcp_api():\n"
            "    return {'api': 'direct'}\n\n"
            "def verify_index_availability():\n"
            "    return True\n\n"
            "def main():\n"
            "    return verify_hybrid_search()\n",
            encoding="utf-8",
        )
        helper_script.write_text(
            "def helper():\n" "    return 'helper'\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(analysis_script.resolve()),
            str(helper_script.resolve()),
            str(retrieval_script.resolve()),
        }
        with store._get_connection() as conn:
            analysis_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/analyze_claude_code_edits.py') ORDER BY id"
                ).fetchall()
            ]
            retrieval_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/verify_mcp_retrieval.py') ORDER BY id"
                ).fetchall()
            ]
            helper_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/helper.py') ORDER BY id"
                ).fetchall()
            ]
            analysis_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/analyze_claude_code_edits.py')"
            ).fetchone()[0]
            retrieval_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/verify_mcp_retrieval.py')"
            ).fetchone()[0]
            analysis_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/analyze_claude_code_edits.py'"
            ).fetchone()[0]
            retrieval_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/verify_mcp_retrieval.py'"
            ).fetchone()[0]
            helper_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/helper.py'"
            ).fetchone()[0]
            analysis_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/analyze_claude_code_edits.py')"
            ).fetchall()
            retrieval_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/verify_mcp_retrieval.py')"
            ).fetchall()
        store.close()
        assert analysis_symbols == [
            "ClaudeCodeTranscriptAnalyzer",
            "parse_jsonl_chunk",
            "extract_tool_sequence",
            "identify_edit_patterns",
            "calculate_edit_metrics",
            "analyze_transcript_file",
            "analyze_all_transcripts",
            "aggregate_results",
            "generate_insights",
            "main",
        ]
        assert retrieval_symbols == [
            "test_mcp_tool",
            "verify_sql_search",
            "verify_semantic_search",
            "verify_hybrid_search",
            "test_direct_mcp_api",
            "verify_index_availability",
            "main",
        ]
        assert helper_symbols == ["helper"]
        assert analysis_chunk_count == 0
        assert retrieval_chunk_count == 0
        assert "exact_analyze_claude_code_edits_rebound" in analysis_metadata
        assert "exact_verify_mcp_retrieval_rebound" in retrieval_metadata
        assert "exact_analyze_claude_code_edits_rebound" not in helper_metadata
        assert "exact_verify_mcp_retrieval_rebound" not in helper_metadata
        assert len(analysis_fts_rows) == 1
        assert "ClaudeCodeTranscriptAnalyzer" in analysis_fts_rows[0][0]
        assert "generate_insights" in analysis_fts_rows[0][0]
        assert len(retrieval_fts_rows) == 1
        assert "verify_hybrid_search" in retrieval_fts_rows[0][0]
        assert "verify_index_availability" in retrieval_fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_path_for_comprehensive_query_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        query_script = scripts_dir / "run_comprehensive_query_test.py"
        semantic_script = scripts_dir / "index_all_repos_semantic_full.py"
        helper_script = scripts_dir / "helper.py"
        query_script.write_text(
            "class ParallelQueryTester:\n"
            "    async def test_bm25_query(self):\n"
            "        return {}\n\n"
            "    async def test_semantic_query(self):\n"
            "        return {}\n\n"
            "    async def test_queries_batch(self):\n"
            "        return []\n\n"
            "    async def test_repository(self):\n"
            "        return {}\n\n"
            "def run_all_repository_tests():\n"
            "    return []\n\n"
            "def aggregate_results():\n"
            "    return {}\n\n"
            "def main():\n"
            "    return ParallelQueryTester()\n",
            encoding="utf-8",
        )
        semantic_script.write_text(
            "def get_all_code_files():\n"
            "    return []\n\n"
            "def create_embeddings_batch():\n"
            "    return []\n\n"
            "def process_repository():\n"
            "    return {}\n\n"
            "def find_test_repositories():\n"
            "    return []\n\n"
            "def main():\n"
            "    return find_test_repositories()\n",
            encoding="utf-8",
        )
        helper_script.write_text("def helper():\n    return True\n", encoding="utf-8")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        def _tracked_chunk_text(*_args, **_kwargs):
            calls.append("called")
            return []

        monkeypatch.setattr(
            "mcp_server.plugins.python_plugin.plugin.chunk_text", _tracked_chunk_text
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(query_script.resolve()),
            str(semantic_script.resolve()),
            str(helper_script.resolve()),
        }
        with store._get_connection() as conn:
            query_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/run_comprehensive_query_test.py') ORDER BY id"
                ).fetchall()
            ]
            semantic_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/index_all_repos_semantic_full.py') ORDER BY id"
                ).fetchall()
            ]
            helper_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/helper.py') ORDER BY id"
                ).fetchall()
            ]
            query_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/run_comprehensive_query_test.py')"
            ).fetchone()[0]
            semantic_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/index_all_repos_semantic_full.py')"
            ).fetchone()[0]
            query_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/run_comprehensive_query_test.py'"
            ).fetchone()[0]
            semantic_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/index_all_repos_semantic_full.py'"
            ).fetchone()[0]
            helper_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/helper.py'"
            ).fetchone()[0]
            query_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/run_comprehensive_query_test.py')"
            ).fetchall()
            semantic_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/index_all_repos_semantic_full.py')"
            ).fetchall()
        store.close()
        assert query_symbols == [
            "ParallelQueryTester",
            "test_bm25_query",
            "test_semantic_query",
            "test_queries_batch",
            "test_repository",
            "run_all_repository_tests",
            "aggregate_results",
            "main",
        ]
        assert semantic_symbols == [
            "get_all_code_files",
            "create_embeddings_batch",
            "process_repository",
            "find_test_repositories",
            "main",
        ]
        assert helper_symbols == ["helper"]
        assert query_chunk_count == 0
        assert semantic_chunk_count == 0
        assert "exact_run_comprehensive_query_test_rebound" in query_metadata
        assert "exact_index_all_repos_semantic_full_rebound" in semantic_metadata
        assert "exact_run_comprehensive_query_test_rebound" not in helper_metadata
        assert "exact_index_all_repos_semantic_full_rebound" not in helper_metadata
        assert len(query_fts_rows) == 1
        assert "ParallelQueryTester" in query_fts_rows[0][0]
        assert "run_all_repository_tests" in query_fts_rows[0][0]
        assert len(semantic_fts_rows) == 1
        assert "create_embeddings_batch" in semantic_fts_rows[0][0]
        assert "find_test_repositories" in semantic_fts_rows[0][0]
        assert calls == []

    def test_index_directory_uses_exact_bounded_python_path_for_centralization_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        strategic_script = scripts_dir / "real_strategic_recommendations.py"
        migration_script = scripts_dir / "migrate_to_centralized.py"
        helper_script = scripts_dir / "helper.py"
        strategic_script.write_text(
            "from dataclasses import dataclass\n"
            "from enum import Enum\n\n"
            "class RecommendationPriority(Enum):\n"
            "    CRITICAL = 'critical'\n\n"
            "class RecommendationCategory(Enum):\n"
            "    IMPLEMENTATION = 'implementation'\n\n"
            "@dataclass\n"
            "class StrategicRecommendation:\n"
            "    id: str\n\n"
            "@dataclass\n"
            "class ImplementationRoadmap:\n"
            "    phase_name: str\n\n"
            "class RealStrategicRecommendationGenerator:\n"
            "    def build(self):\n"
            "        return []\n\n"
            "def main():\n"
            "    return RealStrategicRecommendationGenerator()\n",
            encoding="utf-8",
        )
        migration_script.write_text(
            "from pathlib import Path\n\n"
            "def migrate_repository_index(repo_path: Path, dry_run: bool = False):\n"
            "    return repo_path, dry_run\n\n"
            "def main():\n"
            "    return migrate_repository_index(Path('.'))\n",
            encoding="utf-8",
        )
        helper_script.write_text("def helper():\n    return True\n", encoding="utf-8")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        def _tracked_chunk_text(*_args, **_kwargs):
            calls.append("called")
            return []

        monkeypatch.setattr(
            "mcp_server.plugins.python_plugin.plugin.chunk_text", _tracked_chunk_text
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(strategic_script.resolve()),
            str(migration_script.resolve()),
            str(helper_script.resolve()),
        }
        with store._get_connection() as conn:
            strategic_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/real_strategic_recommendations.py') ORDER BY id"
                ).fetchall()
            ]
            migration_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/migrate_to_centralized.py') ORDER BY id"
                ).fetchall()
            ]
            helper_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/helper.py') ORDER BY id"
                ).fetchall()
            ]
            strategic_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/real_strategic_recommendations.py')"
            ).fetchone()[0]
            migration_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/migrate_to_centralized.py')"
            ).fetchone()[0]
            strategic_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/real_strategic_recommendations.py'"
            ).fetchone()[0]
            migration_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/migrate_to_centralized.py'"
            ).fetchone()[0]
            helper_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/helper.py'"
            ).fetchone()[0]
            strategic_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/real_strategic_recommendations.py')"
            ).fetchall()
            migration_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/migrate_to_centralized.py')"
            ).fetchall()
        store.close()
        assert strategic_symbols == [
            "RecommendationPriority",
            "RecommendationCategory",
            "StrategicRecommendation",
            "ImplementationRoadmap",
            "RealStrategicRecommendationGenerator",
            "build",
            "main",
        ]
        assert migration_symbols == ["migrate_repository_index", "main"]
        assert helper_symbols == ["helper"]
        assert strategic_chunk_count == 0
        assert migration_chunk_count == 0
        assert "exact_real_strategic_recommendations_rebound" in strategic_metadata
        assert "exact_migrate_to_centralized_rebound" in migration_metadata
        assert "exact_real_strategic_recommendations_rebound" not in helper_metadata
        assert "exact_migrate_to_centralized_rebound" not in helper_metadata
        assert len(strategic_fts_rows) == 1
        assert "RecommendationPriority" in strategic_fts_rows[0][0]
        assert "RealStrategicRecommendationGenerator" in strategic_fts_rows[0][0]
        assert len(migration_fts_rows) == 1
        assert "migrate_repository_index" in migration_fts_rows[0][0]
        assert "main" in migration_fts_rows[0][0]
        assert calls == []
        assert "scripts/real_strategic_recommendations.py" in _EXACT_BOUNDED_PYTHON_PATHS
        assert "scripts/migrate_to_centralized.py" in _EXACT_BOUNDED_PYTHON_PATHS

    def test_index_directory_uses_exact_bounded_python_path_for_reindex_demo_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        reindex_script = scripts_dir / "reindex_current_repository.py"
        demo_script = scripts_dir / "demo_centralized_indexes.py"
        helper_script = scripts_dir / "helper.py"
        reindex_script.write_text(
            "from pathlib import Path\n\n"
            "def reindex_current_repo():\n"
            "    return Path('.').resolve()\n\n"
            "def update_registry(repo_hash: str, repo_path: Path):\n"
            "    return repo_hash, repo_path\n",
            encoding="utf-8",
        )
        demo_script.write_text(
            "from pathlib import Path\n\n"
            "def demo_centralized_storage():\n"
            "    return Path('.').name\n\n"
            "def demo_storage_strategies():\n"
            "    return ['centralized', 'portable', 'inline']\n",
            encoding="utf-8",
        )
        helper_script.write_text("def helper():\n    return True\n", encoding="utf-8")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        def _tracked_chunk_text(*_args, **_kwargs):
            calls.append("called")
            return []

        monkeypatch.setattr(
            "mcp_server.plugins.python_plugin.plugin.chunk_text", _tracked_chunk_text
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(reindex_script.resolve()),
            str(demo_script.resolve()),
            str(helper_script.resolve()),
        }
        with store._get_connection() as conn:
            reindex_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/reindex_current_repository.py') ORDER BY id"
                ).fetchall()
            ]
            demo_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/demo_centralized_indexes.py') ORDER BY id"
                ).fetchall()
            ]
            helper_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/helper.py') ORDER BY id"
                ).fetchall()
            ]
            reindex_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/reindex_current_repository.py')"
            ).fetchone()[0]
            demo_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/demo_centralized_indexes.py')"
            ).fetchone()[0]
            reindex_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/reindex_current_repository.py'"
            ).fetchone()[0]
            demo_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/demo_centralized_indexes.py'"
            ).fetchone()[0]
            helper_metadata = conn.execute(
                "SELECT metadata FROM files WHERE relative_path = 'scripts/helper.py'"
            ).fetchone()[0]
            reindex_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/reindex_current_repository.py')"
            ).fetchall()
            demo_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/demo_centralized_indexes.py')"
            ).fetchall()
        store.close()
        assert reindex_symbols == ["reindex_current_repo", "update_registry"]
        assert demo_symbols == ["demo_centralized_storage", "demo_storage_strategies"]
        assert helper_symbols == ["helper"]
        assert reindex_chunk_count == 0
        assert demo_chunk_count == 0
        assert "exact_reindex_current_repository_rebound" in reindex_metadata
        assert "exact_demo_centralized_indexes_rebound" in demo_metadata
        assert "exact_reindex_current_repository_rebound" not in helper_metadata
        assert "exact_demo_centralized_indexes_rebound" not in helper_metadata
        assert len(reindex_fts_rows) == 1
        assert "reindex_current_repo" in reindex_fts_rows[0][0]
        assert "update_registry" in reindex_fts_rows[0][0]
        assert len(demo_fts_rows) == 1
        assert "demo_centralized_storage" in demo_fts_rows[0][0]
        assert "demo_storage_strategies" in demo_fts_rows[0][0]
        assert calls == []
        assert "scripts/reindex_current_repository.py" in _EXACT_BOUNDED_PYTHON_PATHS
        assert "scripts/demo_centralized_indexes.py" in _EXACT_BOUNDED_PYTHON_PATHS

    def test_index_directory_uses_exact_bounded_python_path_for_quick_charts(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        chart_file = repo / "mcp_server" / "visualization" / "quick_charts.py"
        chart_file.parent.mkdir(parents=True)
        chart_file.write_text(
            "class QuickCharts:\n"
            "    def latency_comparison(self):\n"
            "        return 1\n\n"
            "def build_chart():\n"
            "    return QuickCharts()\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        def _tracked_chunk_text(*_args, **_kwargs):
            calls.append("called")
            return []

        monkeypatch.setattr(
            "mcp_server.plugins.python_plugin.plugin.chunk_text", _tracked_chunk_text
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(chart_file.resolve())
        with store._get_connection() as conn:
            symbol_names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'mcp_server/visualization/quick_charts.py') ORDER BY id"
                ).fetchall()
            ]
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'mcp_server/visualization/quick_charts.py')"
            ).fetchone()[0]
            fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'mcp_server/visualization/quick_charts.py')"
            ).fetchall()
        store.close()
        assert "QuickCharts" in symbol_names
        assert "latency_comparison" in symbol_names
        assert "build_chart" in symbol_names
        assert chunk_count == 0
        assert calls == []
        assert len(fts_rows) == 1
        assert "build_chart" in fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_python_path_for_docs_governance_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        docs_dir = repo / "tests" / "docs"
        docs_dir.mkdir(parents=True)
        mre2e_test = docs_dir / "test_mre2e_evidence_contract.py"
        gagov_test = docs_dir / "test_gagov_governance_contract.py"
        mre2e_test.write_text(
            "def test_mre2e_contract():\n    assert True\n",
            encoding="utf-8",
        )
        gagov_test.write_text(
            "class TestGovernanceContract:\n"
            "    def test_gagov_contract(self):\n"
            "        assert True\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        def _tracked_chunk_text(*_args, **_kwargs):
            calls.append("called")
            return []

        monkeypatch.setattr(
            "mcp_server.plugins.python_plugin.plugin.chunk_text", _tracked_chunk_text
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 2
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(mre2e_test.resolve()),
            str(gagov_test.resolve()),
        }
        with store._get_connection() as conn:
            mre2e_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'tests/docs/test_mre2e_evidence_contract.py') ORDER BY id"
                ).fetchall()
            ]
            gagov_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'tests/docs/test_gagov_governance_contract.py') ORDER BY id"
                ).fetchall()
            ]
            mre2e_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'tests/docs/test_mre2e_evidence_contract.py')"
            ).fetchone()[0]
            gagov_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'tests/docs/test_gagov_governance_contract.py')"
            ).fetchone()[0]
            mre2e_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'tests/docs/test_mre2e_evidence_contract.py')"
            ).fetchall()
            gagov_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'tests/docs/test_gagov_governance_contract.py')"
            ).fetchall()
        store.close()
        assert "test_mre2e_contract" in mre2e_symbols
        assert "TestGovernanceContract" in gagov_symbols
        assert "test_gagov_contract" in gagov_symbols
        assert mre2e_chunk_count == 0
        assert gagov_chunk_count == 0
        assert len(mre2e_fts_rows) == 1
        assert len(gagov_fts_rows) == 1
        assert "test_mre2e_contract" in mre2e_fts_rows[0][0]
        assert "test_gagov_contract" in gagov_fts_rows[0][0]
        assert calls == []

    def test_index_directory_uses_exact_bounded_json_path_for_devcontainer_config(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        config_file = repo / ".devcontainer" / "devcontainer.json"
        config_file.parent.mkdir(parents=True)
        config_file.write_text('{"name": "devcontainer"}\n', encoding="utf-8")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected_json_plugin_load(_self, language):
            if language == "json":
                raise AssertionError(
                    "exact bounded devcontainer JSON path should bypass plugin load"
                )
            return None

        monkeypatch.setattr(Dispatcher, "_ensure_plugin_loaded", _unexpected_json_plugin_load)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(config_file.resolve())
        with store._get_connection() as conn:
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = '.devcontainer/devcontainer.json')"
            ).fetchone()[0]
            fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = '.devcontainer/devcontainer.json')"
            ).fetchall()
        store.close()
        assert chunk_count == 0
        assert len(fts_rows) == 1
        assert "devcontainer" in fts_rows[0][0]

    def test_index_directory_emits_post_lexical_handoff_after_devcontainer_boundary(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        config_file = repo / ".devcontainer" / "devcontainer.json"
        config_file.parent.mkdir(parents=True)
        config_file.write_text('{"name": "devcontainer"}\n', encoding="utf-8")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_index_file_with_lexical_timeout(self, _ctx, path, stats, progress_callback=None):
            stats["lexical_stage"] = "walking"
            stats["lexical_files_attempted"] += 1
            stats["in_flight_path"] = str(path.resolve())
            if progress_callback is not None:
                progress_callback("lexical_walking", "lexical", "lexical_mutation")
            stats["lexical_files_completed"] += 1
            stats["last_progress_path"] = str(path.resolve())
            stats["in_flight_path"] = None
            return IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            )

        monkeypatch.setattr(
            Dispatcher,
            "_index_file_with_lexical_timeout",
            _fake_index_file_with_lexical_timeout,
        )

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        dispatcher = Dispatcher([])

        class _FakeBashPlugin:
            @staticmethod
            def supports(path):
                return path.name == "post_create.sh"

        dispatcher._plugin_set_registry.plugins_for = lambda _repo_id: [_FakeBashPlugin()]

        result = dispatcher.index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        store.close()
        assert result["indexed_files"] == 1
        assert result["semantic_stage"] == "failed"
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(config_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None

    def test_index_file_with_lexical_timeout_records_same_devcontainer_path_before_handoff(
        self, tmp_path, monkeypatch
    ):
        repo = tmp_path / "repo"
        config_file = repo / ".devcontainer" / "devcontainer.json"
        config_file.parent.mkdir(parents=True)
        config_file.write_text('{"name": "devcontainer"}\n', encoding="utf-8")
        stats = {
            "lexical_stage": "not_run",
            "lexical_files_attempted": 0,
            "lexical_files_completed": 0,
            "last_progress_path": None,
            "in_flight_path": None,
        }
        snapshots = []
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=MagicMock(),
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        monkeypatch.setattr(
            Dispatcher,
            "index_file",
            lambda self, _ctx, path, do_semantic=False: IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            ),
        )

        mutation = Dispatcher([])._index_file_with_lexical_timeout(
            ctx,
            config_file,
            stats,
            progress_callback=lambda stage, family, source: snapshots.append(
                {
                    "stage": stage,
                    "stage_family": family,
                    "blocker_source": source,
                    "last_progress_path": stats["last_progress_path"],
                    "in_flight_path": stats["in_flight_path"],
                }
            ),
        )

        assert mutation.status == IndexResultStatus.INDEXED
        assert snapshots == [
            {
                "stage": "lexical_walking",
                "stage_family": "lexical",
                "blocker_source": "lexical_mutation",
                "last_progress_path": None,
                "in_flight_path": str(config_file.resolve()),
            }
        ]
        assert stats["lexical_stage"] == "walking"
        assert stats["lexical_files_attempted"] == 1
        assert stats["lexical_files_completed"] == 1
        assert stats["last_progress_path"] == str(config_file.resolve())
        assert stats["in_flight_path"] is None
        assert ".devcontainer/post_create.sh" not in stats["last_progress_path"]
        assert "tests/test_reindex_resume.py" not in stats["last_progress_path"]
        assert "scripts/validate_mcp_comprehensive.py" not in stats["last_progress_path"]
        assert "tests/root_tests/run_reranking_tests.py" not in stats["last_progress_path"]

    def test_index_directory_emits_exact_devcontainer_tail_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        import mcp_server.dispatcher.dispatcher_enhanced as dispatcher_module
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        devcontainer_dir = repo / ".devcontainer"
        devcontainer_dir.mkdir(parents=True)
        post_create = devcontainer_dir / "post_create.sh"
        config_file = devcontainer_dir / "devcontainer.json"
        post_create.write_text("#!/bin/bash\necho devcontainer\n", encoding="utf-8")
        config_file.write_text('{"name": "devcontainer"}\n', encoding="utf-8")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_index_file_with_lexical_timeout(self, _ctx, path, stats, progress_callback=None):
            stats["lexical_stage"] = "walking"
            stats["lexical_files_attempted"] += 1
            stats["in_flight_path"] = str(path.resolve())
            if progress_callback is not None:
                progress_callback("lexical_walking", "lexical", "lexical_mutation")
            stats["lexical_files_completed"] += 1
            stats["last_progress_path"] = str(path.resolve())
            stats["in_flight_path"] = None
            return IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            )

        monkeypatch.setattr(
            Dispatcher,
            "_index_file_with_lexical_timeout",
            _fake_index_file_with_lexical_timeout,
        )
        original_get_all_extensions = dispatcher_module.get_all_extensions
        monkeypatch.setattr(
            dispatcher_module,
            "get_all_extensions",
            lambda: set(original_get_all_extensions()) | {".sh"},
        )

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        store.close()
        assert result["indexed_files"] == 2
        assert result["semantic_stage"] == "failed"
        assert any(
            snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(post_create.resolve())
            and snapshot["in_flight_path"] == str(config_file.resolve())
            for snapshot in snapshots
        )
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(config_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None

    def test_index_directory_emits_later_test_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        tests_dir = repo / "tests"
        tests_dir.mkdir(parents=True)
        prior_file = tests_dir / "test_deployment_runbook_shape.py"
        prior_file.write_text("def test_runbook_shape():\n    assert True\n", encoding="utf-8")
        blocked_file = tests_dir / "test_reindex_resume.py"
        blocked_file.write_text("def test_reindex_resume():\n    assert True\n", encoding="utf-8")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(tests_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        store.close()
        assert result["indexed_files"] == 2
        assert result["semantic_stage"] == "failed"
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                ".devcontainer/devcontainer.json",
                "scripts/quick_mcp_vs_native_validation.py",
                "tests/test_artifact_publish_race.py",
            )
            for snapshot in snapshots
        )

    def test_index_directory_emits_later_script_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        prior_file = scripts_dir / "run_test_batch.py"
        prior_file.write_text("def main():\n    return 1\n", encoding="utf-8")
        blocked_file = scripts_dir / "validate_mcp_comprehensive.py"
        blocked_file.write_text("def validate_mcp():\n    return True\n", encoding="utf-8")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(scripts_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        store.close()
        assert result["indexed_files"] == 2
        assert result["semantic_stage"] == "failed"
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                ".devcontainer/devcontainer.json",
                "scripts/quick_mcp_vs_native_validation.py",
                "tests/test_artifact_publish_race.py",
            )
            for snapshot in snapshots
        )

    def test_index_directory_emits_later_root_test_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        tests_dir = repo / "tests" / "root_tests"
        tests_dir.mkdir(parents=True)
        prior_file = tests_dir / "test_voyage_api.py"
        prior_file.write_text("def test_voyage_api():\n    assert True\n", encoding="utf-8")
        blocked_file = tests_dir / "run_reranking_tests.py"
        blocked_file.write_text("def main():\n    return 1\n", encoding="utf-8")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(tests_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        store.close()
        assert result["indexed_files"] == 2
        assert result["semantic_stage"] == "failed"
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                ".devcontainer/devcontainer.json",
                "scripts/validate_mcp_comprehensive.py",
                "tests/test_artifact_publish_race.py",
                "tests/test_reindex_resume.py",
            )
            for snapshot in snapshots
        )

    def test_index_directory_emits_script_language_audit_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        prior_file = scripts_dir / "migrate_large_index_to_multi_repo.py"
        prior_file.write_text(
            "class RepositoryMigration:\n"
            "    pass\n\n"
            "class LargeIndexMigrator:\n"
            "    def migrate_repository(self):\n"
            "        return 'ok'\n\n"
            "def main():\n"
            "    return LargeIndexMigrator()\n",
            encoding="utf-8",
        )
        blocked_file = scripts_dir / "check_index_languages.py"
        blocked_file.write_text(
            "print('Checking languages')\n"
            "tables = ['files']\n"
            "for table in tables:\n"
            "    print(table)\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(scripts_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        assert result["indexed_files"] == 2
        assert result["failed_files"] == 0
        assert result["semantic_stage"] == "failed"
        assert result["last_progress_path"] == str(blocked_file.resolve())
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        with store._get_connection() as conn:
            migration_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/migrate_large_index_to_multi_repo.py') ORDER BY id"
                ).fetchall()
            ]
            audit_symbol_count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/check_index_languages.py')"
            ).fetchone()[0]
            migration_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/migrate_large_index_to_multi_repo.py')"
            ).fetchone()[0]
            audit_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/check_index_languages.py')"
            ).fetchone()[0]
            migration_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/migrate_large_index_to_multi_repo.py')"
            ).fetchall()
            audit_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/check_index_languages.py')"
            ).fetchall()
        store.close()
        assert "RepositoryMigration" in migration_symbols
        assert "LargeIndexMigrator" in migration_symbols
        assert "main" in migration_symbols
        assert audit_symbol_count == 0
        assert migration_chunk_count == 0
        assert audit_chunk_count == 0
        assert len(migration_fts) == 1
        assert "LargeIndexMigrator" in migration_fts[0][0]
        assert len(audit_fts) == 1
        assert "Checking languages" in audit_fts[0][0]
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                ".claude/commands/execute-lane.md",
                ".claude/commands/plan-phase.md",
                "tests/root_tests/run_reranking_tests.py",
                "scripts/validate_mcp_comprehensive.py",
            )
            for snapshot in snapshots
        )

    def test_index_directory_emits_docs_governance_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        docs_dir = repo / "tests" / "docs"
        docs_dir.mkdir(parents=True)
        prior_file = docs_dir / "test_mre2e_evidence_contract.py"
        prior_file.write_text("def test_mre2e_contract():\n    assert True\n", encoding="utf-8")
        blocked_file = docs_dir / "test_gagov_governance_contract.py"
        blocked_file.write_text(
            "def test_gagov_contract():\n    assert True\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(docs_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        store.close()
        assert result["indexed_files"] == 2
        assert result["semantic_stage"] == "failed"
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                ".devcontainer/devcontainer.json",
                "scripts/validate_mcp_comprehensive.py",
                "tests/root_tests/run_reranking_tests.py",
                "analysis_archive/semantic_vs_sql_comparison_1750926162.json",
            )
            for snapshot in snapshots
        )

    def test_index_directory_emits_docs_test_tail_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.python_plugin.plugin import Plugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        docs_dir = repo / "tests" / "docs"
        docs_dir.mkdir(parents=True)
        prior_file = docs_dir / "test_gaclose_evidence_closeout.py"
        prior_file.write_text(
            "def test_decision_artifact_links_prerequisite_evidence_and_verification():\n"
            "    assert True\n\n"
            "def test_support_matrix_freezes_claim_tiers_and_topology_limits():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        blocked_file = docs_dir / "test_p8_deployment_security.py"
        blocked_file.write_text(
            "def test_security_heading_present():\n"
            "    assert True\n\n"
            "def test_mcp_access_controls_subsection_present():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(docs_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        assert result["indexed_files"] == 2
        assert result["semantic_stage"] == "failed"
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        with store._get_connection() as conn:
            gaclose_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_gaclose_evidence_closeout.py') ORDER BY id"
                ).fetchall()
            ]
            p8_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_p8_deployment_security.py') ORDER BY id"
                ).fetchall()
            ]
            gaclose_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_gaclose_evidence_closeout.py')"
            ).fetchone()[0]
            p8_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_p8_deployment_security.py')"
            ).fetchone()[0]
            gaclose_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_gaclose_evidence_closeout.py')"
            ).fetchall()
            p8_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_p8_deployment_security.py')"
            ).fetchall()
        store.close()
        assert (
            "test_decision_artifact_links_prerequisite_evidence_and_verification" in gaclose_symbols
        )
        assert "test_support_matrix_freezes_claim_tiers_and_topology_limits" in gaclose_symbols
        assert "test_security_heading_present" in p8_symbols
        assert "test_mcp_access_controls_subsection_present" in p8_symbols
        assert gaclose_chunk_count == 0
        assert p8_chunk_count == 0
        assert len(gaclose_fts) == 1
        assert (
            "test_decision_artifact_links_prerequisite_evidence_and_verification"
            in gaclose_fts[0][0]
        )
        assert len(p8_fts) == 1
        assert "test_mcp_access_controls_subsection_present" in p8_fts[0][0]
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert "tests/docs/test_gaclose_evidence_closeout.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/docs/test_p8_deployment_security.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/docs/test_mre2e_evidence_contract.py" not in Plugin._BOUNDED_CHUNK_PATHS
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                "tests/docs/test_mre2e_evidence_contract.py",
                "tests/docs/test_gagov_governance_contract.py",
                "scripts/create_semantic_embeddings.py",
                "scripts/consolidate_real_performance_data.py",
            )
            for snapshot in snapshots
        )

    def test_index_directory_emits_docs_contract_tail_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.python_plugin.plugin import Plugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        docs_dir = repo / "tests" / "docs"
        docs_dir.mkdir(parents=True)
        prior_file = docs_dir / "test_semincr_contract.py"
        prior_file.write_text(
            "def test_semincr_docs_freeze_incremental_mutation_contract():\n"
            "    assert True\n\n"
            "def test_semincr_docs_stay_out_of_query_routing_and_dogfood_claims():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        blocked_file = docs_dir / "test_gabase_ga_readiness_contract.py"
        blocked_file.write_text(
            "def test_checklist_exists_with_required_sections_and_baseline():\n"
            "    assert True\n\n"
            "def test_public_docs_remain_pre_ga_and_route_to_canonical_artifacts():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(docs_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        assert result["indexed_files"] == 2
        assert result["semantic_stage"] == "failed"
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        with store._get_connection() as conn:
            semincr_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_semincr_contract.py') ORDER BY id"
                ).fetchall()
            ]
            gabase_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_gabase_ga_readiness_contract.py') ORDER BY id"
                ).fetchall()
            ]
            semincr_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_semincr_contract.py')"
            ).fetchone()[0]
            gabase_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_gabase_ga_readiness_contract.py')"
            ).fetchone()[0]
            semincr_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_semincr_contract.py')"
            ).fetchall()
            gabase_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_gabase_ga_readiness_contract.py')"
            ).fetchall()
        store.close()
        assert "test_semincr_docs_freeze_incremental_mutation_contract" in semincr_symbols
        assert "test_semincr_docs_stay_out_of_query_routing_and_dogfood_claims" in semincr_symbols
        assert "test_checklist_exists_with_required_sections_and_baseline" in gabase_symbols
        assert "test_public_docs_remain_pre_ga_and_route_to_canonical_artifacts" in gabase_symbols
        assert semincr_chunk_count == 0
        assert gabase_chunk_count == 0
        assert len(semincr_fts) == 1
        assert "test_semincr_docs_freeze_incremental_mutation_contract" in semincr_fts[0][0]
        assert len(gabase_fts) == 1
        assert "test_public_docs_remain_pre_ga_and_route_to_canonical_artifacts" in gabase_fts[0][0]
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert "tests/docs/test_semincr_contract.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/docs/test_gabase_ga_readiness_contract.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/docs/test_gaclose_evidence_closeout.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                "tests/docs/test_mre2e_evidence_contract.py",
                "tests/docs/test_gagov_governance_contract.py",
                "tests/docs/test_gaclose_evidence_closeout.py",
                "tests/docs/test_p8_deployment_security.py",
            )
            for snapshot in snapshots
        )

    def test_index_directory_emits_ga_release_docs_tail_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.python_plugin.plugin import Plugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        docs_dir = repo / "tests" / "docs"
        docs_dir.mkdir(parents=True)
        prior_file = docs_dir / "test_garc_rc_soak_contract.py"
        prior_file.write_text(
            "def test_rc8_contract_surfaces_are_frozen():\n"
            "    assert True\n\n"
            "def test_runbooks_freeze_pre_dispatch_and_observation_commands():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        blocked_file = docs_dir / "test_garel_ga_release_contract.py"
        blocked_file.write_text(
            "def test_final_decision_exists_and_cites_all_ga_inputs():\n"
            "    assert True\n\n"
            "def test_workflow_runtime_warning_is_remediated_before_any_future_ga_dispatch():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(docs_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        assert result["indexed_files"] == 2
        assert result["semantic_stage"] == "failed"
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        with store._get_connection() as conn:
            garc_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_garc_rc_soak_contract.py') ORDER BY id"
                ).fetchall()
            ]
            garel_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_garel_ga_release_contract.py') ORDER BY id"
                ).fetchall()
            ]
            garc_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_garc_rc_soak_contract.py')"
            ).fetchone()[0]
            garel_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_garel_ga_release_contract.py')"
            ).fetchone()[0]
            garc_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_garc_rc_soak_contract.py')"
            ).fetchall()
            garel_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_garel_ga_release_contract.py')"
            ).fetchall()
        store.close()
        assert "test_rc8_contract_surfaces_are_frozen" in garc_symbols
        assert "test_runbooks_freeze_pre_dispatch_and_observation_commands" in garc_symbols
        assert "test_final_decision_exists_and_cites_all_ga_inputs" in garel_symbols
        assert (
            "test_workflow_runtime_warning_is_remediated_before_any_future_ga_dispatch"
            in garel_symbols
        )
        assert garc_chunk_count == 0
        assert garel_chunk_count == 0
        assert len(garc_fts) == 1
        assert "test_rc8_contract_surfaces_are_frozen" in garc_fts[0][0]
        assert len(garel_fts) == 1
        assert (
            "test_workflow_runtime_warning_is_remediated_before_any_future_ga_dispatch"
            in garel_fts[0][0]
        )
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert "tests/docs/test_garc_rc_soak_contract.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/docs/test_garel_ga_release_contract.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/docs/test_semincr_contract.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                "tests/docs/test_semincr_contract.py",
                "tests/docs/test_gabase_ga_readiness_contract.py",
                "tests/security/fixtures/mock_plugin/plugin.py",
                "tests/security/fixtures/mock_plugin/__init__.py",
            )
            for snapshot in snapshots
        )

    def test_index_directory_emits_p24_plugin_tail_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        tests_dir = repo / "tests"
        tests_dir.mkdir(parents=True)
        prior_file = tests_dir / "test_p24_plugin_availability.py"
        prior_file.write_text(
            "P24_FIELDS = {'language', 'state'}\n\n"
            "def test_availability_has_one_stable_row_per_supported_language():\n"
            "    assert P24_FIELDS\n\n"
            "def test_missing_optional_dependency_is_machine_readable():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        blocked_file = tests_dir / "test_dispatcher_extension_gating.py"
        blocked_file.write_text(
            "REPO_ID = 'test-gating-repo'\n\n"
            "def _make_db():\n"
            "    return None\n\n"
            "def _make_ctx():\n"
            "    return None\n\n"
            "class FakePyPlugin:\n"
            "    pass\n\n"
            "class SpyPlugin:\n"
            "    pass\n\n"
            "def test_py_extension_gating_no_plugin_instantiation():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(tests_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        assert result["indexed_files"] == 2
        assert result["semantic_stage"] == "failed"
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        with store._get_connection() as conn:
            p24_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/test_p24_plugin_availability.py') ORDER BY id"
                ).fetchall()
            ]
            gating_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/test_dispatcher_extension_gating.py') ORDER BY id"
                ).fetchall()
            ]
            p24_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/test_p24_plugin_availability.py')"
            ).fetchone()[0]
            gating_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/test_dispatcher_extension_gating.py')"
            ).fetchone()[0]
            p24_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/test_p24_plugin_availability.py')"
            ).fetchall()
            gating_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/test_dispatcher_extension_gating.py')"
            ).fetchall()
        store.close()
        assert "test_availability_has_one_stable_row_per_supported_language" in p24_symbols
        assert "test_missing_optional_dependency_is_machine_readable" in p24_symbols
        assert "_make_db" in gating_symbols
        assert "_make_ctx" in gating_symbols
        assert "FakePyPlugin" in gating_symbols
        assert "SpyPlugin" in gating_symbols
        assert "test_py_extension_gating_no_plugin_instantiation" in gating_symbols
        assert p24_chunk_count == 0
        assert gating_chunk_count == 0
        assert len(p24_fts) == 1
        assert "P24_FIELDS" in p24_fts[0][0]
        assert len(gating_fts) == 1
        assert "REPO_ID" in gating_fts[0][0]
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert "tests/test_p24_plugin_availability.py" in _EXACT_BOUNDED_PYTHON_PATHS
        assert "tests/test_dispatcher_extension_gating.py" in _EXACT_BOUNDED_PYTHON_PATHS
        assert "tests/docs/test_garel_ga_release_contract.py" in _EXACT_BOUNDED_PYTHON_PATHS
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                "tests/docs/test_semincr_contract.py",
                "tests/docs/test_gabase_ga_readiness_contract.py",
                "tests/security/fixtures/mock_plugin/plugin.py",
                "tests/security/fixtures/mock_plugin/__init__.py",
            )
            for snapshot in snapshots
        )

    def test_index_directory_emits_test_repo_index_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        prior_file = scripts_dir / "check_test_index_schema.py"
        prior_file.write_text(
            "def check_schema(db_path):\n"
            "    return db_path\n\n"
            "def main():\n"
            "    return check_schema('db')\n",
            encoding="utf-8",
        )
        blocked_file = scripts_dir / "ensure_test_repos_indexed.py"
        blocked_file.write_text(
            "def check_index_exists(repo_info):\n"
            "    return bool(repo_info)\n\n"
            "def index_repository(repo_info):\n"
            "    return repo_info\n\n"
            "def main():\n"
            "    return index_repository({})\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(scripts_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        assert result["indexed_files"] == 2
        assert result["failed_files"] == 0
        assert result["semantic_stage"] == "failed"
        assert result["last_progress_path"] == str(blocked_file.resolve())
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        with store._get_connection() as conn:
            schema_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/check_test_index_schema.py') ORDER BY id"
                ).fetchall()
            ]
            ensure_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/ensure_test_repos_indexed.py') ORDER BY id"
                ).fetchall()
            ]
            schema_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/check_test_index_schema.py')"
            ).fetchone()[0]
            ensure_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/ensure_test_repos_indexed.py')"
            ).fetchone()[0]
            schema_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/check_test_index_schema.py')"
            ).fetchall()
            ensure_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/ensure_test_repos_indexed.py')"
            ).fetchall()
        store.close()
        assert schema_symbols == ["check_schema", "main"]
        assert ensure_symbols == ["check_index_exists", "index_repository", "main"]
        assert schema_chunk_count == 0
        assert ensure_chunk_count == 0
        assert len(schema_fts) == 1
        assert "check_schema" in schema_fts[0][0]
        assert len(ensure_fts) == 1
        assert "check_index_exists" in ensure_fts[0][0]
        assert "index_repository" in ensure_fts[0][0]
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                "scripts/check_index_languages.py",
                "scripts/test_mcp_protocol_direct.py",
                "scripts/consolidate_real_performance_data.py",
                "tests/docs/test_p8_deployment_security.py",
            )
            for snapshot in snapshots
        )

    def test_index_directory_emits_missing_repo_semantic_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        prior_file = scripts_dir / "index_missing_repos_semantic.py"
        prior_file.write_text(
            "def get_existing_collections():\n"
            "    return {}\n\n"
            "def find_missing_repositories():\n"
            "    return []\n\n"
            "def estimate_repo_size(repo_name):\n"
            "    return len(repo_name)\n\n"
            "def main():\n"
            "    return find_missing_repositories()\n",
            encoding="utf-8",
        )
        blocked_file = scripts_dir / "identify_working_indexes.py"
        blocked_file.write_text(
            "def get_repo_hash(repo_path):\n"
            "    return str(repo_path)\n\n"
            "def check_index_health(db_path):\n"
            "    return True, {'path': str(db_path)}\n\n"
            "def find_test_repos():\n"
            "    return []\n\n"
            "def main():\n"
            "    return find_test_repos()\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(scripts_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        assert result["indexed_files"] == 2
        assert result["failed_files"] == 0
        assert result["semantic_stage"] == "failed"
        assert result["last_progress_path"] == str(blocked_file.resolve())
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        with store._get_connection() as conn:
            missing_repo_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/index_missing_repos_semantic.py') ORDER BY id"
                ).fetchall()
            ]
            working_indexes_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/identify_working_indexes.py') ORDER BY id"
                ).fetchall()
            ]
            missing_repo_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/index_missing_repos_semantic.py')"
            ).fetchone()[0]
            working_indexes_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/identify_working_indexes.py')"
            ).fetchone()[0]
            missing_repo_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/index_missing_repos_semantic.py')"
            ).fetchall()
            working_indexes_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/identify_working_indexes.py')"
            ).fetchall()
        store.close()
        assert missing_repo_symbols == [
            "get_existing_collections",
            "find_missing_repositories",
            "estimate_repo_size",
            "main",
        ]
        assert working_indexes_symbols == [
            "get_repo_hash",
            "check_index_health",
            "find_test_repos",
            "main",
        ]
        assert missing_repo_chunk_count == 0
        assert working_indexes_chunk_count == 0
        assert len(missing_repo_fts) == 1
        assert "get_existing_collections" in missing_repo_fts[0][0]
        assert "find_missing_repositories" in missing_repo_fts[0][0]
        assert len(working_indexes_fts) == 1
        assert "get_repo_hash" in working_indexes_fts[0][0]
        assert "check_index_health" in working_indexes_fts[0][0]
        assert "find_test_repos" in working_indexes_fts[0][0]
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                "scripts/check_test_index_schema.py",
                "scripts/ensure_test_repos_indexed.py",
                "scripts/consolidate_real_performance_data.py",
                "tests/docs/test_p8_deployment_security.py",
            )
            for snapshot in snapshots
        )

    def test_index_directory_emits_utility_verification_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        utility_dir = repo / "scripts" / "utilities"
        utility_dir.mkdir(parents=True)
        prior_file = utility_dir / "prepare_index_for_upload.py"
        prior_file.write_text(
            "def get_repo_hash():\n"
            "    return 'hash'\n\n"
            "def find_current_index(indexes_dir, repo_hash):\n"
            "    return indexes_dir / repo_hash\n\n"
            "def prepare_for_upload(index_dir, staging_dir):\n"
            "    return True, f'{index_dir}:{staging_dir}'\n\n"
            "def main():\n"
            "    return prepare_for_upload('index', 'staging')\n",
            encoding="utf-8",
        )
        blocked_file = utility_dir / "verify_tool_usage.py"
        blocked_file.write_text(
            "class ToolUsageAnalyzer:\n"
            "    def parse_tool_sequence(self, session_log):\n"
            "        return [session_log]\n\n"
            "def create_mock_session_log():\n"
            "    return 'mock-session-log'\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(utility_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        assert result["indexed_files"] == 2
        assert result["failed_files"] == 0
        assert result["semantic_stage"] == "failed"
        assert result["last_progress_path"] == str(blocked_file.resolve())
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        with store._get_connection() as conn:
            prepare_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/utilities/prepare_index_for_upload.py') ORDER BY id"
                ).fetchall()
            ]
            verify_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/utilities/verify_tool_usage.py') ORDER BY id"
                ).fetchall()
            ]
            prepare_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/utilities/prepare_index_for_upload.py')"
            ).fetchone()[0]
            verify_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/utilities/verify_tool_usage.py')"
            ).fetchone()[0]
            prepare_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/utilities/prepare_index_for_upload.py')"
            ).fetchall()
            verify_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/utilities/verify_tool_usage.py')"
            ).fetchall()
        store.close()
        assert prepare_symbols == [
            "get_repo_hash",
            "find_current_index",
            "prepare_for_upload",
            "main",
        ]
        assert verify_symbols == [
            "ToolUsageAnalyzer",
            "parse_tool_sequence",
            "create_mock_session_log",
        ]
        assert prepare_chunk_count == 0
        assert verify_chunk_count == 0
        assert len(prepare_fts) == 1
        assert "get_repo_hash" in prepare_fts[0][0]
        assert "prepare_for_upload" in prepare_fts[0][0]
        assert len(verify_fts) == 1
        assert "ToolUsageAnalyzer" in verify_fts[0][0]
        assert "create_mock_session_log" in verify_fts[0][0]

    def test_index_directory_emits_qdrant_report_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        prior_file = scripts_dir / "map_repos_to_qdrant.py"
        prior_file.write_text(
            "def find_all_repositories():\n"
            "    return ['repo-a']\n\n"
            "def summarize_collection_points(collections):\n"
            "    return len(collections)\n",
            encoding="utf-8",
        )
        blocked_file = scripts_dir / "create_claude_code_aware_report.py"
        blocked_file.write_text(
            "def setup_style():\n"
            "    return 'style'\n\n"
            "def load_benchmark_data():\n"
            "    return {'repos': 1}\n\n"
            "def create_claude_code_pipeline_comparison():\n"
            "    return 'pipeline'\n\n"
            "def create_real_world_examples(data):\n"
            "    return data\n\n"
            "def create_token_breakdown_analysis(data):\n"
            "    return data\n\n"
            "def create_claude_code_instructions_visual():\n"
            "    return 'instructions'\n\n"
            "def create_comprehensive_report():\n"
            "    return 'report'\n\n"
            "def main():\n"
            "    return create_comprehensive_report()\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(scripts_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        assert result["indexed_files"] == 2
        assert result["failed_files"] == 0
        assert result["semantic_stage"] == "failed"
        assert result["last_progress_path"] == str(blocked_file.resolve())
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        with store._get_connection() as conn:
            map_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/map_repos_to_qdrant.py') ORDER BY id"
                ).fetchall()
            ]
            report_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/create_claude_code_aware_report.py') ORDER BY id"
                ).fetchall()
            ]
            map_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/map_repos_to_qdrant.py')"
            ).fetchone()[0]
            report_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/create_claude_code_aware_report.py')"
            ).fetchone()[0]
            map_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/map_repos_to_qdrant.py')"
            ).fetchall()
            report_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/create_claude_code_aware_report.py')"
            ).fetchall()
        store.close()
        assert map_symbols == [
            "find_all_repositories",
            "summarize_collection_points",
        ]
        assert report_symbols == [
            "setup_style",
            "load_benchmark_data",
            "create_claude_code_pipeline_comparison",
            "create_real_world_examples",
            "create_token_breakdown_analysis",
            "create_claude_code_instructions_visual",
            "create_comprehensive_report",
            "main",
        ]
        assert map_chunk_count == 0
        assert report_chunk_count == 0
        assert len(map_fts) == 1
        assert "find_all_repositories" in map_fts[0][0]
        assert "summarize_collection_points" in map_fts[0][0]
        assert len(report_fts) == 1
        assert "setup_style" in report_fts[0][0]
        assert "create_comprehensive_report" in report_fts[0][0]
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                "scripts/utilities/prepare_index_for_upload.py",
                "scripts/utilities/verify_tool_usage.py",
                "scripts/index_missing_repos_semantic.py",
                "scripts/identify_working_indexes.py",
            )
            for snapshot in snapshots
        )

    def test_index_directory_emits_optimized_upload_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        prior_file = scripts_dir / "execute_optimized_analysis.py"
        prior_file.write_text(
            "class OptimizedAnalysisMetrics:\n"
            "    def as_dict(self):\n"
            "        return {'ok': True}\n\n"
            "class OptimizedAnalysisExecutor:\n"
            "    async def execute_optimized_analysis(self):\n"
            "        return {'status': 'ok'}\n\n"
            "    def _generate_final_results(self):\n"
            "        return {'final': True}\n\n"
            "async def main():\n"
            "    return await OptimizedAnalysisExecutor().execute_optimized_analysis()\n",
            encoding="utf-8",
        )
        blocked_file = scripts_dir / "index-artifact-upload-v2.py"
        blocked_file.write_text(
            "class CompatibilityAwareUploader:\n"
            "    def generate_compatibility_hash(self):\n"
            "        return 'hash'\n\n"
            "    def compress_indexes(self):\n"
            "        return ('archive', 'checksum', 1)\n\n"
            "    def create_metadata(self):\n"
            "        return {'ok': True}\n\n"
            "def main():\n"
            "    return CompatibilityAwareUploader().create_metadata()\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(scripts_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        assert result["indexed_files"] == 2
        assert result["failed_files"] == 0
        assert result["semantic_stage"] == "failed"
        assert result["last_progress_path"] == str(blocked_file.resolve())
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        with store._get_connection() as conn:
            analysis_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/execute_optimized_analysis.py') ORDER BY id"
                ).fetchall()
            ]
            upload_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/index-artifact-upload-v2.py') ORDER BY id"
                ).fetchall()
            ]
            analysis_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/execute_optimized_analysis.py')"
            ).fetchone()[0]
            upload_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/index-artifact-upload-v2.py')"
            ).fetchone()[0]
            analysis_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/execute_optimized_analysis.py')"
            ).fetchall()
            upload_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/index-artifact-upload-v2.py')"
            ).fetchall()
        store.close()
        assert analysis_symbols == [
            "OptimizedAnalysisMetrics",
            "as_dict",
            "OptimizedAnalysisExecutor",
            "execute_optimized_analysis",
            "_generate_final_results",
            "main",
        ]
        assert upload_symbols == [
            "CompatibilityAwareUploader",
            "generate_compatibility_hash",
            "compress_indexes",
            "create_metadata",
            "main",
        ]
        assert analysis_chunk_count == 0
        assert upload_chunk_count == 0
        assert len(analysis_fts) == 1
        assert "OptimizedAnalysisExecutor" in analysis_fts[0][0]
        assert "execute_optimized_analysis" in analysis_fts[0][0]
        assert len(upload_fts) == 1
        assert "CompatibilityAwareUploader" in upload_fts[0][0]
        assert "create_metadata" in upload_fts[0][0]
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                "scripts/utilities/prepare_index_for_upload.py",
                "scripts/utilities/verify_tool_usage.py",
                "scripts/map_repos_to_qdrant.py",
                "scripts/create_claude_code_aware_report.py",
            )
            for snapshot in snapshots
        )
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                "scripts/check_test_index_schema.py",
                "scripts/ensure_test_repos_indexed.py",
                "scripts/index_missing_repos_semantic.py",
                "scripts/identify_working_indexes.py",
            )
            for snapshot in snapshots
        )

    def test_index_directory_emits_edit_retrieval_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        prior_file = scripts_dir / "analyze_claude_code_edits.py"
        prior_file.write_text(
            "class ClaudeCodeTranscriptAnalyzer:\n"
            "    def parse_jsonl_chunk(self, chunk):\n"
            "        return [chunk]\n\n"
            "    def extract_tool_sequence(self, events):\n"
            "        return events\n\n"
            "    def identify_edit_patterns(self, tool_sequence):\n"
            "        return tool_sequence\n\n"
            "    def calculate_edit_metrics(self, patterns):\n"
            "        return {'count': len(patterns)}\n\n"
            "    async def analyze_transcript_file(self, file_path):\n"
            "        return {'path': str(file_path)}\n\n"
            "    async def analyze_all_transcripts(self, transcript_dir):\n"
            "        return {'dir': str(transcript_dir)}\n\n"
            "    def aggregate_results(self, results):\n"
            "        return {'results': len(results)}\n\n"
            "    def generate_insights(self, metrics):\n"
            "        return metrics\n\n"
            "async def main():\n"
            "    return await ClaudeCodeTranscriptAnalyzer().analyze_all_transcripts('logs')\n",
            encoding="utf-8",
        )
        blocked_file = scripts_dir / "verify_mcp_retrieval.py"
        blocked_file.write_text(
            "def test_mcp_tool(tool_name, args):\n"
            "    return {'tool': tool_name, 'args': args}\n\n"
            "def verify_sql_search():\n"
            "    return {'mode': 'sql'}\n\n"
            "def verify_semantic_search():\n"
            "    return {'mode': 'semantic'}\n\n"
            "def verify_hybrid_search():\n"
            "    return {'mode': 'hybrid'}\n\n"
            "def test_direct_mcp_api():\n"
            "    return {'api': 'direct'}\n\n"
            "def verify_index_availability():\n"
            "    return True\n\n"
            "def main():\n"
            "    return verify_hybrid_search()\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(scripts_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        assert result["indexed_files"] == 2
        assert result["failed_files"] == 0
        assert result["semantic_stage"] == "failed"
        assert result["last_progress_path"] == str(blocked_file.resolve())
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        with store._get_connection() as conn:
            analysis_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/analyze_claude_code_edits.py') ORDER BY id"
                ).fetchall()
            ]
            retrieval_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/verify_mcp_retrieval.py') ORDER BY id"
                ).fetchall()
            ]
            analysis_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/analyze_claude_code_edits.py')"
            ).fetchone()[0]
            retrieval_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/verify_mcp_retrieval.py')"
            ).fetchone()[0]
            analysis_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/analyze_claude_code_edits.py')"
            ).fetchall()
            retrieval_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/verify_mcp_retrieval.py')"
            ).fetchall()
        store.close()
        assert analysis_symbols == [
            "ClaudeCodeTranscriptAnalyzer",
            "parse_jsonl_chunk",
            "extract_tool_sequence",
            "identify_edit_patterns",
            "calculate_edit_metrics",
            "analyze_transcript_file",
            "analyze_all_transcripts",
            "aggregate_results",
            "generate_insights",
            "main",
        ]
        assert retrieval_symbols == [
            "test_mcp_tool",
            "verify_sql_search",
            "verify_semantic_search",
            "verify_hybrid_search",
            "test_direct_mcp_api",
            "verify_index_availability",
            "main",
        ]
        assert analysis_chunk_count == 0
        assert retrieval_chunk_count == 0
        assert len(analysis_fts) == 1
        assert "ClaudeCodeTranscriptAnalyzer" in analysis_fts[0][0]
        assert "generate_insights" in analysis_fts[0][0]
        assert len(retrieval_fts) == 1
        assert "verify_hybrid_search" in retrieval_fts[0][0]
        assert "verify_index_availability" in retrieval_fts[0][0]
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                "scripts/execute_optimized_analysis.py",
                "scripts/index-artifact-upload-v2.py",
                "scripts/map_repos_to_qdrant.py",
                "scripts/create_claude_code_aware_report.py",
            )
            for snapshot in snapshots
        )

    def test_index_directory_emits_docs_truth_tail_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.python_plugin.plugin import Plugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        docs_dir = repo / "tests" / "docs"
        docs_dir.mkdir(parents=True)
        prior_file = docs_dir / "test_p23_doc_truth.py"
        prior_file.write_text(
            "def test_active_docs_do_not_contain_stale_strings():\n"
            "    assert True\n\n"
            "def test_active_release_docs_name_stable_surface_and_support_status():\n"
            "    assert True\n\n"
            "def test_agent_docs_point_to_support_matrix_and_current_install_path():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        blocked_file = docs_dir / "test_semdogfood_evidence_contract.py"
        blocked_file.write_text(
            "def test_semdogfood_report_exists_and_names_required_evidence_sections():\n"
            "    assert True\n\n"
            "def test_semdogfood_report_records_trace_freshness_recovery_and_roadmap_steering():\n"
            "    assert True\n\n"
            "def test_semdogfood_report_preserves_command_level_verification_and_runtime_paths():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(docs_dir), [], [prior_file.name, blocked_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        assert result["indexed_files"] == 2
        assert result["semantic_stage"] == "failed"
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(blocked_file.resolve())
        ]
        assert lexical_pair
        with store._get_connection() as conn:
            p23_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_p23_doc_truth.py') ORDER BY id"
                ).fetchall()
            ]
            semdogfood_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_semdogfood_evidence_contract.py') ORDER BY id"
                ).fetchall()
            ]
            p23_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_p23_doc_truth.py')"
            ).fetchone()[0]
            semdogfood_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_semdogfood_evidence_contract.py')"
            ).fetchone()[0]
            p23_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_p23_doc_truth.py')"
            ).fetchall()
            semdogfood_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/docs/test_semdogfood_evidence_contract.py')"
            ).fetchall()
        store.close()
        assert "test_active_docs_do_not_contain_stale_strings" in p23_symbols
        assert "test_active_release_docs_name_stable_surface_and_support_status" in p23_symbols
        assert "test_agent_docs_point_to_support_matrix_and_current_install_path" in p23_symbols
        assert (
            "test_semdogfood_report_exists_and_names_required_evidence_sections"
            in semdogfood_symbols
        )
        assert (
            "test_semdogfood_report_records_trace_freshness_recovery_and_roadmap_steering"
            in semdogfood_symbols
        )
        assert (
            "test_semdogfood_report_preserves_command_level_verification_and_runtime_paths"
            in semdogfood_symbols
        )
        assert p23_chunk_count == 0
        assert semdogfood_chunk_count == 0
        assert len(p23_fts) == 1
        assert "test_active_release_docs_name_stable_surface_and_support_status" in p23_fts[0][0]
        assert len(semdogfood_fts) == 1
        assert (
            "test_semdogfood_report_preserves_command_level_verification_and_runtime_paths"
            in semdogfood_fts[0][0]
        )
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(blocked_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert "tests/docs/test_p23_doc_truth.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/docs/test_semdogfood_evidence_contract.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/docs/test_garc_rc_soak_contract.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                "tests/docs/test_semincr_contract.py",
                "tests/docs/test_gabase_ga_readiness_contract.py",
                "tests/docs/test_garc_rc_soak_contract.py",
                "tests/docs/test_garel_ga_release_contract.py",
                "tests/security/fixtures/mock_plugin/plugin.py",
                "tests/security/fixtures/mock_plugin/__init__.py",
            )
            for snapshot in snapshots
        )

    def test_index_directory_emits_mock_plugin_fixture_pair_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.python_plugin.plugin import Plugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        fixture_dir = repo / "tests" / "security" / "fixtures" / "mock_plugin"
        fixture_dir.mkdir(parents=True)
        plugin_file = fixture_dir / "plugin.py"
        plugin_file.write_text(
            "from __future__ import annotations\n\n"
            "from typing import Iterable\n\n"
            "from mcp_server.plugin_base import IndexShard, IPlugin, Reference, SearchOpts, SearchResult\n\n"
            "class Plugin(IPlugin):\n"
            '    lang = "mock"\n\n'
            "    def __init__(self, sqlite_store=None, enable_semantic=False):\n"
            "        self._ctx = None\n\n"
            "    def bind(self, ctx) -> None:\n"
            "        self._ctx = ctx\n\n"
            "    def supports(self, path) -> bool:\n"
            "        return True\n\n"
            "    def indexFile(self, path, content: str) -> IndexShard:\n"
            '        return {"file": str(path), "symbols": [{"name": "echo", "len": len(content)}], "language": self.lang}\n\n'
            "    def getDefinition(self, symbol: str):\n"
            '        return {"symbol": symbol, "kind": "function", "language": self.lang, "signature": f"def {symbol}()", "doc": None, "defined_in": "mock.py", "start_line": 1, "end_line": 1, "span": [1, 1]}\n\n'
            "    def findReferences(self, symbol: str) -> Iterable[Reference]:\n"
            '        return [Reference(file="mock.py", start_line=1, end_line=1)]\n\n'
            "    def search(self, query: str, opts: SearchOpts | None = None) -> Iterable[SearchResult]:\n"
            '        return [{"file": "mock.py", "start_line": 1, "end_line": 1, "snippet": query}]\n',
            encoding="utf-8",
        )
        init_file = fixture_dir / "__init__.py"
        init_file.write_text(
            'from .plugin import Plugin\n\n__all__ = ["Plugin"]\n',
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(fixture_dir), [], [plugin_file.name, init_file.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        assert result["indexed_files"] == 2
        assert result["semantic_stage"] == "failed"
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(plugin_file.resolve())
            and snapshot["in_flight_path"] == str(init_file.resolve())
        ]
        assert lexical_pair
        with store._get_connection() as conn:
            plugin_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/security/fixtures/mock_plugin/plugin.py') ORDER BY id"
                ).fetchall()
            ]
            plugin_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/security/fixtures/mock_plugin/plugin.py')"
            ).fetchone()[0]
            init_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/security/fixtures/mock_plugin/__init__.py')"
            ).fetchone()[0]
            plugin_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/security/fixtures/mock_plugin/plugin.py')"
            ).fetchall()
            init_fts = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/security/fixtures/mock_plugin/__init__.py')"
            ).fetchall()
        store.close()
        assert "Plugin" in plugin_symbols
        assert "supports" in plugin_symbols
        assert "indexFile" in plugin_symbols
        assert "findReferences" in plugin_symbols
        assert plugin_chunk_count == 0
        assert init_chunk_count == 0
        assert len(plugin_fts) == 1
        assert "class Plugin" in plugin_fts[0][0]
        assert "def findReferences" in plugin_fts[0][0]
        assert len(init_fts) == 1
        assert "from .plugin import Plugin" in init_fts[0][0]
        assert '__all__ = ["Plugin"]' in init_fts[0][0]
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(init_file.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert "tests/security/fixtures/mock_plugin/plugin.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/security/fixtures/mock_plugin/__init__.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/security/test_plugin_sandbox.py" not in Plugin._BOUNDED_CHUNK_PATHS
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                "tests/docs/test_gaclose_evidence_closeout.py",
                "tests/docs/test_p8_deployment_security.py",
                "scripts/create_semantic_embeddings.py",
                "scripts/consolidate_real_performance_data.py",
            )
            for snapshot in snapshots
        )

    def test_index_directory_keeps_invalid_exact_bounded_python_path_discoverable(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        scripts_dir = repo / "scripts"
        scripts_dir.mkdir(parents=True)
        blocked_file = scripts_dir / "consolidate_real_performance_data.py"
        blocked_file.write_text(
            "class PerformanceDataConsolidator:\n"
            "    def __init__(self):\n"
            '        self.transcript_dir = Path("Path.home() / ".claude"/projects")\n',
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["last_progress_path"] == str(blocked_file.resolve())
        with store._get_connection() as conn:
            symbol_count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/consolidate_real_performance_data.py')"
            ).fetchone()[0]
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/consolidate_real_performance_data.py')"
            ).fetchone()[0]
            fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'scripts/consolidate_real_performance_data.py')"
            ).fetchall()
        store.close()
        assert symbol_count == 0
        assert chunk_count == 0
        assert len(fts_rows) == 1
        assert 'Path("Path.home() / ".claude"/projects")' in fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_json_path_for_archive_tail_successor(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        archive_json = repo / "analysis_archive" / "semantic_vs_sql_comparison_1750926162.json"
        archive_json.parent.mkdir(parents=True)
        archive_json.write_text('{"comparison": "archive tail"}\n', encoding="utf-8")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected_json_plugin_load(_self, language):
            if language == "json":
                raise AssertionError(
                    "exact bounded archive-tail JSON path should bypass plugin load"
                )
            return None

        monkeypatch.setattr(Dispatcher, "_ensure_plugin_loaded", _unexpected_json_plugin_load)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] == str(archive_json.resolve())
        with store._get_connection() as conn:
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'analysis_archive/semantic_vs_sql_comparison_1750926162.json')"
            ).fetchone()[0]
            fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'analysis_archive/semantic_vs_sql_comparison_1750926162.json')"
            ).fetchall()
        store.close()
        assert chunk_count == 0
        assert len(fts_rows) == 1
        assert "archive tail" in fts_rows[0][0]

    def test_index_directory_uses_exact_bounded_json_path_for_optimized_final_report_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.markdown_plugin.plugin import MarkdownPlugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        report_dir = repo / "final_optimized_report_final_report_1750958096"
        report_dir.mkdir(parents=True)
        report_json = report_dir / "final_report_data.json"
        report_markdown = report_dir / "FINAL_OPTIMIZED_ANALYSIS_REPORT.md"
        unrelated_json = repo / "config.json"
        report_json.write_text(
            "{\n"
            '  "session_id": "final_report_1750958096",\n'
            '  "business_metrics": {"time_reduction_percent": 81.0},\n'
            '  "executive_dashboard": {"headline_metrics": {"speedup_factor": "5.3x"}},\n'
            '  "technical_analysis": {"throughput_improvement": "5.3x"},\n'
            '  "recommendations": {"immediate_actions": ["Deploy optimized analysis framework"]}\n'
            "}\n",
            encoding="utf-8",
        )
        report_markdown.write_text(
            "# Optimized Enhanced MCP Analysis - Final Report\n\n"
            "## Executive Summary\n\n"
            "A concise summary.\n\n"
            "## Technical Achievements\n\n"
            "- Parallelization and bounded indexing.\n\n"
            "## Financial Analysis\n\n"
            "- Savings and ROI.\n\n"
            "## Quality Improvements\n\n"
            "- Better precision.\n\n"
            "## Strategic Recommendations\n\n"
            "- Preserve discoverability.\n",
            encoding="utf-8",
        )
        unrelated_json.write_text('{"name": "config"}\n', encoding="utf-8")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        class _FakeJsonPlugin:
            lang = "json"
            language = "json"

            def indexFile(self, path, content):
                if Path(path).resolve() == report_json.resolve():
                    raise AssertionError(
                        "exact bounded optimized final report JSON path should bypass plugin load"
                    )
                return {"symbols": [], "chunks": [{"content": content}], "metadata": {"fake": True}}

        def _unexpected_markdown_heavy_path(*_args, **_kwargs):
            raise AssertionError(
                "heavy Markdown path should not run for the optimized final report Markdown file"
            )

        original_ensure_plugin_loaded = Dispatcher._ensure_plugin_loaded

        monkeypatch.setattr(
            Dispatcher,
            "_ensure_plugin_loaded",
            lambda _self, language: (
                _FakeJsonPlugin()
                if language == "json"
                else original_ensure_plugin_loaded(_self, language)
            ),
        )
        monkeypatch.setattr(MarkdownPlugin, "extract_structure", _unexpected_markdown_heavy_path)
        monkeypatch.setattr(MarkdownPlugin, "chunk_document", _unexpected_markdown_heavy_path)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        plugin = MarkdownPlugin()
        assert (
            plugin._resolve_lightweight_reason(
                report_markdown, report_markdown.read_text(encoding="utf-8")
            )
            == "analysis_report_path"
        )

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        assert result["lexical_stage"] == "completed"
        assert result["last_progress_path"] in {
            str(report_json.resolve()),
            str(report_markdown.resolve()),
            str(unrelated_json.resolve()),
        }
        with store._get_connection() as conn:
            report_json_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'final_optimized_report_final_report_1750958096/final_report_data.json')"
            ).fetchone()[0]
            report_json_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'final_optimized_report_final_report_1750958096/final_report_data.json')"
            ).fetchall()
            report_markdown_symbols = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM symbols WHERE file_id IN "
                    "(SELECT id FROM files WHERE relative_path = "
                    "'final_optimized_report_final_report_1750958096/FINAL_OPTIMIZED_ANALYSIS_REPORT.md') "
                    "ORDER BY id"
                ).fetchall()
            ]
            report_markdown_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'final_optimized_report_final_report_1750958096/FINAL_OPTIMIZED_ANALYSIS_REPORT.md')"
            ).fetchone()[0]
            report_markdown_fts_rows = conn.execute(
                "SELECT content FROM fts_code WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = "
                "'final_optimized_report_final_report_1750958096/FINAL_OPTIMIZED_ANALYSIS_REPORT.md')"
            ).fetchall()
            unrelated_json_chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN "
                "(SELECT id FROM files WHERE relative_path = 'config.json')"
            ).fetchone()[0]
        store.close()

        assert report_json_chunk_count == 0
        assert len(report_json_fts_rows) == 1
        assert "business_metrics" in report_json_fts_rows[0][0]
        assert "executive_dashboard" in report_json_fts_rows[0][0]
        assert "recommendations" in report_json_fts_rows[0][0]
        assert report_markdown_symbols == [
            "Optimized Enhanced MCP Analysis - Final Report",
            "Optimized Enhanced MCP Analysis - Final Report",
            "Executive Summary",
            "Technical Achievements",
            "Financial Analysis",
            "Quality Improvements",
            "Strategic Recommendations",
        ]
        assert report_markdown_chunk_count == 0
        assert len(report_markdown_fts_rows) == 1
        assert "Technical Achievements" in report_markdown_fts_rows[0][0]
        assert "Strategic Recommendations" in report_markdown_fts_rows[0][0]
        assert unrelated_json_chunk_count > 0

    def test_index_directory_uses_exact_bounded_paths_for_legacy_codex_phase_loop_runtime(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        launch_file = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260424T180441Z-01-gagov-execute"
            / "launch.json"
        )
        terminal_file = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260427T071807Z-02-artpub-execute"
            / "terminal-summary.json"
        )
        heartbeat_file = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260427T085911Z-02-mrready-execute"
            / "heartbeat.json"
        )
        archive_events = (
            repo / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "events.jsonl"
        )
        archive_state = (
            repo / ".codex" / "phase-loop" / "archive" / "20260424T211515Z" / "state.json"
        )
        for path in (launch_file, terminal_file, heartbeat_file, archive_events, archive_state):
            path.parent.mkdir(parents=True, exist_ok=True)
        launch_file.write_text('{"command": ["codex"], "phase": "GAGOV"}\n', encoding="utf-8")
        terminal_file.write_text(
            '{"artifact_paths": {"terminal": "terminal-summary.json"}, "terminal_status": "executed", "next_action": "preserve"}\n',
            encoding="utf-8",
        )
        heartbeat_file.write_text(
            '{"phase": "MRREADY", "current_phase": "MRREADY", "next_action": "monitor"}\n',
            encoding="utf-8",
        )
        archive_events.write_text(
            '{"phase": "GARC", "next_action": "resume"}\n{"phase": "GAREL", "next_action": "wait"}\n',
            encoding="utf-8",
        )
        archive_state.write_text(
            '{"current_phase": "GARC", "artifact_paths": ["launch.json"]}\n', encoding="utf-8"
        )

        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected_json_plugin_load(_self, language):
            if language == "json":
                raise AssertionError(
                    "legacy .codex/phase-loop JSON paths should bypass plugin load"
                )
            return None

        monkeypatch.setattr(Dispatcher, "_ensure_plugin_loaded", _unexpected_json_plugin_load)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 5
        assert result["failed_files"] == 0
        with store._get_connection() as conn:
            chunk_counts = conn.execute(
                "SELECT relative_path, COUNT(*) FROM code_chunks "
                "JOIN files ON code_chunks.file_id = files.id "
                "GROUP BY relative_path"
            ).fetchall()
            fts_rows = dict(
                conn.execute(
                    "SELECT relative_path, content FROM fts_code "
                    "JOIN files ON fts_code.file_id = files.id"
                ).fetchall()
            )
        store.close()
        assert chunk_counts == []
        assert ".codex/phase-loop/runs/20260424T180441Z-01-gagov-execute/launch.json" in fts_rows
        assert (
            "command"
            in fts_rows[".codex/phase-loop/runs/20260424T180441Z-01-gagov-execute/launch.json"]
        )
        assert (
            ".codex/phase-loop/runs/20260427T071807Z-02-artpub-execute/terminal-summary.json"
            in fts_rows
        )
        assert (
            "terminal_status"
            in fts_rows[
                ".codex/phase-loop/runs/20260427T071807Z-02-artpub-execute/terminal-summary.json"
            ]
        )
        assert (
            ".codex/phase-loop/runs/20260427T085911Z-02-mrready-execute/heartbeat.json" in fts_rows
        )
        assert (
            "current_phase"
            in fts_rows[".codex/phase-loop/runs/20260427T085911Z-02-mrready-execute/heartbeat.json"]
        )
        assert ".codex/phase-loop/archive/20260424T211515Z/events.jsonl" in fts_rows
        assert "next_action" in fts_rows[".codex/phase-loop/archive/20260424T211515Z/events.jsonl"]
        assert ".codex/phase-loop/archive/20260424T211515Z/state.json" in fts_rows
        assert "current_phase" in fts_rows[".codex/phase-loop/archive/20260424T211515Z/state.json"]

    def test_index_directory_uses_exact_bounded_paths_for_later_legacy_codex_phase_loop_rebound_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        launch_file = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260424T190651Z-01-garc-plan"
            / "launch.json"
        )
        terminal_file = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260427T075236Z-05-idxsafe-repair"
            / "terminal-summary.json"
        )
        for path in (launch_file, terminal_file):
            path.parent.mkdir(parents=True, exist_ok=True)
        launch_file.write_text(
            '{"command": ["codex", "plan"], "phase": "GARC", ".codex/phase-loop": "legacy"}\n',
            encoding="utf-8",
        )
        terminal_file.write_text(
            '{"artifact_paths": {"terminal": "terminal-summary.json"}, "terminal_status": "interrupted", "next_action": "repair"}\n',
            encoding="utf-8",
        )

        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected_json_plugin_load(_self, language):
            if language == "json":
                raise AssertionError(
                    "legacy .codex/phase-loop JSON paths should bypass plugin load"
                )
            return None

        monkeypatch.setattr(Dispatcher, "_ensure_plugin_loaded", _unexpected_json_plugin_load)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 2
        assert result["failed_files"] == 0
        with store._get_connection() as conn:
            chunk_counts = conn.execute(
                "SELECT relative_path, COUNT(*) FROM code_chunks "
                "JOIN files ON code_chunks.file_id = files.id "
                "GROUP BY relative_path"
            ).fetchall()
            fts_rows = dict(
                conn.execute(
                    "SELECT relative_path, content FROM fts_code "
                    "JOIN files ON fts_code.file_id = files.id"
                ).fetchall()
            )
        store.close()
        assert chunk_counts == []
        assert ".codex/phase-loop/runs/20260424T190651Z-01-garc-plan/launch.json" in fts_rows
        assert (
            "command"
            in fts_rows[".codex/phase-loop/runs/20260424T190651Z-01-garc-plan/launch.json"]
        )
        assert (
            "phase" in fts_rows[".codex/phase-loop/runs/20260424T190651Z-01-garc-plan/launch.json"]
        )
        assert (
            ".codex/phase-loop"
            in fts_rows[".codex/phase-loop/runs/20260424T190651Z-01-garc-plan/launch.json"]
        )
        assert (
            ".codex/phase-loop/runs/20260427T075236Z-05-idxsafe-repair/terminal-summary.json"
            in fts_rows
        )
        assert (
            "artifact_paths"
            in fts_rows[
                ".codex/phase-loop/runs/20260427T075236Z-05-idxsafe-repair/terminal-summary.json"
            ]
        )
        assert (
            "terminal_status"
            in fts_rows[
                ".codex/phase-loop/runs/20260427T075236Z-05-idxsafe-repair/terminal-summary.json"
            ]
        )
        assert (
            "next_action"
            in fts_rows[
                ".codex/phase-loop/runs/20260427T075236Z-05-idxsafe-repair/terminal-summary.json"
            ]
        )

    def test_index_directory_uses_exact_bounded_paths_for_legacy_codex_phase_loop_relapse_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        terminal_file = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260427T081107Z-08-ciflow-plan"
            / "terminal-summary.json"
        )
        launch_file = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260427T081107Z-08-ciflow-plan"
            / "launch.json"
        )
        for path in (terminal_file, launch_file):
            path.parent.mkdir(parents=True, exist_ok=True)
        terminal_file.write_text(
            '{"artifact_paths": {"terminal": "terminal-summary.json"}, "terminal_status": "interrupted", "next_action": "repair", "current_phase": "SEMQUERYFULLREBOUNDTAIL"}\n',
            encoding="utf-8",
        )
        launch_file.write_text(
            '{"command": ["codex", "plan"], "phase": "SEMCODEXLOOPRELAPSETAIL", ".codex/phase-loop": "legacy"}\n',
            encoding="utf-8",
        )

        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected_json_plugin_load(_self, language):
            if language == "json":
                raise AssertionError(
                    "legacy .codex/phase-loop JSON paths should bypass plugin load"
                )
            return None

        monkeypatch.setattr(Dispatcher, "_ensure_plugin_loaded", _unexpected_json_plugin_load)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 2
        assert result["failed_files"] == 0
        with store._get_connection() as conn:
            chunk_counts = conn.execute(
                "SELECT relative_path, COUNT(*) FROM code_chunks "
                "JOIN files ON code_chunks.file_id = files.id "
                "GROUP BY relative_path"
            ).fetchall()
            fts_rows = dict(
                conn.execute(
                    "SELECT relative_path, content FROM fts_code "
                    "JOIN files ON fts_code.file_id = files.id"
                ).fetchall()
            )
        store.close()
        assert chunk_counts == []
        assert (
            ".codex/phase-loop/runs/20260427T081107Z-08-ciflow-plan/terminal-summary.json"
            in fts_rows
        )
        assert (
            "artifact_paths"
            in fts_rows[
                ".codex/phase-loop/runs/20260427T081107Z-08-ciflow-plan/terminal-summary.json"
            ]
        )
        assert (
            "terminal_status"
            in fts_rows[
                ".codex/phase-loop/runs/20260427T081107Z-08-ciflow-plan/terminal-summary.json"
            ]
        )
        assert (
            "next_action"
            in fts_rows[
                ".codex/phase-loop/runs/20260427T081107Z-08-ciflow-plan/terminal-summary.json"
            ]
        )
        assert (
            "current_phase"
            in fts_rows[
                ".codex/phase-loop/runs/20260427T081107Z-08-ciflow-plan/terminal-summary.json"
            ]
        )
        assert ".codex/phase-loop/runs/20260427T081107Z-08-ciflow-plan/launch.json" in fts_rows
        assert (
            "command"
            in fts_rows[".codex/phase-loop/runs/20260427T081107Z-08-ciflow-plan/launch.json"]
        )
        assert (
            "phase"
            in fts_rows[".codex/phase-loop/runs/20260427T081107Z-08-ciflow-plan/launch.json"]
        )
        assert (
            ".codex/phase-loop"
            in fts_rows[".codex/phase-loop/runs/20260427T081107Z-08-ciflow-plan/launch.json"]
        )

    def test_index_directory_uses_exact_bounded_paths_for_legacy_codex_phase_loop_ciflow_execute_relapse_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        terminal_file = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260427T081704Z-09-ciflow-execute"
            / "terminal-summary.json"
        )
        launch_file = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260427T081704Z-09-ciflow-execute"
            / "launch.json"
        )
        prior_terminal = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260427T075236Z-05-idxsafe-repair"
            / "terminal-summary.json"
        )
        prior_launch = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260424T190651Z-01-garc-plan"
            / "launch.json"
        )
        for path in (terminal_file, launch_file, prior_terminal, prior_launch):
            path.parent.mkdir(parents=True, exist_ok=True)
        terminal_file.write_text(
            '{"artifact_paths": {"terminal": "terminal-summary.json"}, "terminal_status": "interrupted", "next_action": "repair", "current_phase": "SEMCODEXLOOPCIFLOWEXECRELAPSETAIL"}\n',
            encoding="utf-8",
        )
        launch_file.write_text(
            '{"command": ["codex", "exec"], "phase": "SEMCODEXLOOPCIFLOWEXECRELAPSETAIL", ".codex/phase-loop": "legacy"}\n',
            encoding="utf-8",
        )
        prior_terminal.write_text(
            '{"artifact_paths": {"terminal": "terminal-summary.json"}, "terminal_status": "interrupted", "next_action": "repair"}\n',
            encoding="utf-8",
        )
        prior_launch.write_text(
            '{"command": ["codex", "plan"], "phase": "GARC", ".codex/phase-loop": "legacy"}\n',
            encoding="utf-8",
        )

        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected_json_plugin_load(_self, language):
            if language == "json":
                raise AssertionError(
                    "legacy .codex/phase-loop JSON paths should bypass plugin load"
                )
            return None

        monkeypatch.setattr(Dispatcher, "_ensure_plugin_loaded", _unexpected_json_plugin_load)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 4
        assert result["failed_files"] == 0
        with store._get_connection() as conn:
            chunk_counts = conn.execute(
                "SELECT relative_path, COUNT(*) FROM code_chunks "
                "JOIN files ON code_chunks.file_id = files.id "
                "GROUP BY relative_path"
            ).fetchall()
            fts_rows = dict(
                conn.execute(
                    "SELECT relative_path, content FROM fts_code "
                    "JOIN files ON fts_code.file_id = files.id"
                ).fetchall()
            )
        store.close()
        assert chunk_counts == []
        assert (
            ".codex/phase-loop/runs/20260427T081704Z-09-ciflow-execute/terminal-summary.json"
            in fts_rows
        )
        assert (
            "artifact_paths"
            in fts_rows[
                ".codex/phase-loop/runs/20260427T081704Z-09-ciflow-execute/terminal-summary.json"
            ]
        )
        assert (
            "terminal_status"
            in fts_rows[
                ".codex/phase-loop/runs/20260427T081704Z-09-ciflow-execute/terminal-summary.json"
            ]
        )
        assert (
            "next_action"
            in fts_rows[
                ".codex/phase-loop/runs/20260427T081704Z-09-ciflow-execute/terminal-summary.json"
            ]
        )
        assert (
            "current_phase"
            in fts_rows[
                ".codex/phase-loop/runs/20260427T081704Z-09-ciflow-execute/terminal-summary.json"
            ]
        )
        assert ".codex/phase-loop/runs/20260427T081704Z-09-ciflow-execute/launch.json" in fts_rows
        assert (
            "command"
            in fts_rows[".codex/phase-loop/runs/20260427T081704Z-09-ciflow-execute/launch.json"]
        )
        assert (
            "phase"
            in fts_rows[".codex/phase-loop/runs/20260427T081704Z-09-ciflow-execute/launch.json"]
        )
        assert (
            ".codex/phase-loop"
            in fts_rows[".codex/phase-loop/runs/20260427T081704Z-09-ciflow-execute/launch.json"]
        )

    def test_index_directory_uses_exact_bounded_paths_for_legacy_codex_phase_loop_heartbeat_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        launch_file = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260427T071207Z-01-artpub-plan"
            / "launch.json"
        )
        heartbeat_file = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260427T071207Z-01-artpub-plan"
            / "heartbeat.json"
        )
        prior_terminal = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260427T081107Z-08-ciflow-plan"
            / "terminal-summary.json"
        )
        for path in (launch_file, heartbeat_file, prior_terminal):
            path.parent.mkdir(parents=True, exist_ok=True)
        launch_file.write_text(
            '{"command": ["codex", "exec"], "phase": "ARTPUB", ".codex/phase-loop": "legacy"}\n',
            encoding="utf-8",
        )
        heartbeat_file.write_text(
            '{"phase": "ARTPUB", "current_phase": "ARTPUB", "status": "exited", "heartbeat_status": "exited"}\n',
            encoding="utf-8",
        )
        prior_terminal.write_text(
            '{"artifact_paths": {"terminal": "terminal-summary.json"}, "terminal_status": "interrupted", "current_phase": "SEMQUERYFULLREBOUNDTAIL"}\n',
            encoding="utf-8",
        )

        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected_json_plugin_load(_self, language):
            if language == "json":
                raise AssertionError(
                    "legacy .codex/phase-loop JSON paths should bypass plugin load"
                )
            return None

        monkeypatch.setattr(Dispatcher, "_ensure_plugin_loaded", _unexpected_json_plugin_load)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 3
        assert result["failed_files"] == 0
        with store._get_connection() as conn:
            chunk_counts = conn.execute(
                "SELECT relative_path, COUNT(*) FROM code_chunks "
                "JOIN files ON code_chunks.file_id = files.id "
                "GROUP BY relative_path"
            ).fetchall()
            fts_rows = dict(
                conn.execute(
                    "SELECT relative_path, content FROM fts_code "
                    "JOIN files ON fts_code.file_id = files.id"
                ).fetchall()
            )
        store.close()
        assert chunk_counts == []
        assert ".codex/phase-loop/runs/20260427T071207Z-01-artpub-plan/launch.json" in fts_rows
        assert (
            "command"
            in fts_rows[".codex/phase-loop/runs/20260427T071207Z-01-artpub-plan/launch.json"]
        )
        assert (
            "phase"
            in fts_rows[".codex/phase-loop/runs/20260427T071207Z-01-artpub-plan/launch.json"]
        )
        assert (
            ".codex/phase-loop"
            in fts_rows[".codex/phase-loop/runs/20260427T071207Z-01-artpub-plan/launch.json"]
        )
        assert ".codex/phase-loop/runs/20260427T071207Z-01-artpub-plan/heartbeat.json" in fts_rows
        assert (
            "current_phase"
            in fts_rows[".codex/phase-loop/runs/20260427T071207Z-01-artpub-plan/heartbeat.json"]
        )
        assert (
            "status"
            in fts_rows[".codex/phase-loop/runs/20260427T071207Z-01-artpub-plan/heartbeat.json"]
        )
        assert (
            "heartbeat_status"
            in fts_rows[".codex/phase-loop/runs/20260427T071207Z-01-artpub-plan/heartbeat.json"]
        )
        assert (
            ".codex/phase-loop/runs/20260427T081107Z-08-ciflow-plan/terminal-summary.json"
            in fts_rows
        )
        assert (
            "terminal_status"
            in fts_rows[
                ".codex/phase-loop/runs/20260427T081107Z-08-ciflow-plan/terminal-summary.json"
            ]
        )

    def test_index_directory_uses_exact_bounded_paths_for_legacy_codex_phase_loop_garecut_heartbeat_pair(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        launch_file = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260425T051448Z-01-garecut-execute"
            / "launch.json"
        )
        heartbeat_file = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260425T051448Z-01-garecut-execute"
            / "heartbeat.json"
        )
        prior_heartbeat = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260427T071207Z-01-artpub-plan"
            / "heartbeat.json"
        )
        prior_terminal = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260427T081107Z-08-ciflow-plan"
            / "terminal-summary.json"
        )
        for path in (launch_file, heartbeat_file, prior_heartbeat, prior_terminal):
            path.parent.mkdir(parents=True, exist_ok=True)
        launch_file.write_text(
            '{"command": ["codex", "exec"], "phase": "GARECUT", ".codex/phase-loop": "legacy"}\n',
            encoding="utf-8",
        )
        heartbeat_file.write_text(
            '{"phase": "GARECUT", "current_phase": "SEMCODEXLOOPGARECUTHEARTBEATTAIL", "status": "interrupted", "trace": "legacy-heartbeat", "heartbeat": {"status": "pending"}}\n',
            encoding="utf-8",
        )
        prior_heartbeat.write_text(
            '{"phase": "ARTPUB", "current_phase": "ARTPUB", "status": "exited"}\n',
            encoding="utf-8",
        )
        prior_terminal.write_text(
            '{"artifact_paths": {"terminal": "terminal-summary.json"}, "terminal_status": "interrupted", "current_phase": "SEMQUERYFULLREBOUNDTAIL"}\n',
            encoding="utf-8",
        )

        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        def _unexpected_json_plugin_load(_self, language):
            if language == "json":
                raise AssertionError(
                    "legacy .codex/phase-loop JSON paths should bypass plugin load"
                )
            return None

        monkeypatch.setattr(Dispatcher, "_ensure_plugin_loaded", _unexpected_json_plugin_load)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 4
        assert result["failed_files"] == 0
        with store._get_connection() as conn:
            chunk_counts = conn.execute(
                "SELECT relative_path, COUNT(*) FROM code_chunks "
                "JOIN files ON code_chunks.file_id = files.id "
                "GROUP BY relative_path"
            ).fetchall()
            fts_rows = dict(
                conn.execute(
                    "SELECT relative_path, content FROM fts_code "
                    "JOIN files ON fts_code.file_id = files.id"
                ).fetchall()
            )
        store.close()
        assert chunk_counts == []
        assert ".codex/phase-loop/runs/20260425T051448Z-01-garecut-execute/launch.json" in fts_rows
        assert (
            "command"
            in fts_rows[".codex/phase-loop/runs/20260425T051448Z-01-garecut-execute/launch.json"]
        )
        assert (
            "phase"
            in fts_rows[".codex/phase-loop/runs/20260425T051448Z-01-garecut-execute/launch.json"]
        )
        assert (
            ".codex/phase-loop"
            in fts_rows[".codex/phase-loop/runs/20260425T051448Z-01-garecut-execute/launch.json"]
        )
        assert (
            ".codex/phase-loop/runs/20260425T051448Z-01-garecut-execute/heartbeat.json" in fts_rows
        )
        assert (
            "current_phase"
            in fts_rows[".codex/phase-loop/runs/20260425T051448Z-01-garecut-execute/heartbeat.json"]
        )
        assert (
            "status"
            in fts_rows[".codex/phase-loop/runs/20260425T051448Z-01-garecut-execute/heartbeat.json"]
        )
        assert (
            "trace"
            in fts_rows[".codex/phase-loop/runs/20260425T051448Z-01-garecut-execute/heartbeat.json"]
        )
        assert (
            "heartbeat"
            in fts_rows[".codex/phase-loop/runs/20260425T051448Z-01-garecut-execute/heartbeat.json"]
        )
        assert ".codex/phase-loop/runs/20260427T071207Z-01-artpub-plan/heartbeat.json" in fts_rows
        assert (
            ".codex/phase-loop/runs/20260427T081107Z-08-ciflow-plan/terminal-summary.json"
            in fts_rows
        )

    def test_index_directory_keeps_canonical_phase_loop_json_on_normal_plugin_path(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        legacy_launch = (
            repo
            / ".codex"
            / "phase-loop"
            / "runs"
            / "20260424T180441Z-01-gagov-execute"
            / "launch.json"
        )
        canonical_state = repo / ".phase-loop" / "state.json"
        for path in (legacy_launch, canonical_state):
            path.parent.mkdir(parents=True, exist_ok=True)
        legacy_launch.write_text('{"command": ["codex"], "phase": "GAGOV"}\n', encoding="utf-8")
        canonical_state.write_text('{"current_phase": "SEMCODEXLOOPTAIL"}\n', encoding="utf-8")

        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        json_plugin = Mock()
        json_plugin.language = "json"
        json_plugin.lang = "json"
        json_plugin.supports.side_effect = lambda path: Path(path).suffix == ".json"
        json_plugin.indexFile.return_value = {
            "symbols": [],
            "chunks": [],
            "metadata": {"plugin_path": "normal_json_plugin"},
        }

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([json_plugin]).index_directory(ctx, repo)

        assert result["indexed_files"] == 2
        assert result["failed_files"] == 0
        assert json_plugin.indexFile.call_count == 1
        indexed_path = json_plugin.indexFile.call_args.args[0]
        assert Path(indexed_path).resolve() == canonical_state.resolve()
        with store._get_connection() as conn:
            fts_rows = dict(
                conn.execute(
                    "SELECT relative_path, content FROM fts_code "
                    "JOIN files ON fts_code.file_id = files.id"
                ).fetchall()
            )
        store.close()
        assert ".codex/phase-loop/runs/20260424T180441Z-01-gagov-execute/launch.json" in fts_rows
        assert ".phase-loop/state.json" in fts_rows

    def test_index_directory_emits_archive_tail_successor_before_closeout_handoff(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        prior_file = (
            repo
            / "analysis_archive"
            / "scripts_archive"
            / "scripts_test_files"
            / "verify_mcp_fix.py"
        )
        prior_file.parent.mkdir(parents=True)
        prior_file.write_text("def verify_fix():\n    return True\n", encoding="utf-8")
        archive_json = repo / "analysis_archive" / "semantic_vs_sql_comparison_1750926162.json"
        archive_json.write_text('{"comparison": "archive tail"}\n', encoding="utf-8")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        snapshots = []

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: object())

        def _fake_walk(_directory, followlinks=False):
            assert followlinks is False
            yield str(prior_file.parent), [], [prior_file.name]
            yield str(archive_json.parent), [], [archive_json.name]

        def _explode_before_semantic_progress(self, _ctx, _paths, progress_callback=None):
            raise RuntimeError("semantic closeout entry stalled before emitting progress")

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(
            Dispatcher,
            "rebuild_semantic_for_paths",
            _explode_before_semantic_progress,
        )

        result = Dispatcher([]).index_directory(
            ctx,
            repo,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        store.close()
        assert result["indexed_files"] == 2
        assert result["semantic_stage"] == "failed"
        lexical_pair = [
            snapshot
            for snapshot in snapshots
            if snapshot["stage"] == "lexical_walking"
            and snapshot["last_progress_path"] == str(prior_file.resolve())
            and snapshot["in_flight_path"] == str(archive_json.resolve())
        ]
        assert lexical_pair
        assert snapshots[-1]["stage"] == "force_full_closeout_handoff"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["last_progress_path"] == str(archive_json.resolve())
        assert snapshots[-1]["in_flight_path"] is None
        assert all(
            needle
            not in (
                (snapshot.get("last_progress_path") or "")
                + " "
                + (snapshot.get("in_flight_path") or "")
            )
            for needle in (
                ".devcontainer/devcontainer.json",
                "mcp_server/visualization/quick_charts.py",
                "docs/benchmarks/production_benchmark.md",
                "test_workspace/",
            )
            for snapshot in snapshots
        )

    def test_index_directory_keeps_unrelated_json_files_off_exact_devcontainer_bypass(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        config_file = repo / "config.json"
        repo.mkdir()
        config_file.write_text('{"name": "config"}\n', encoding="utf-8")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        calls = []

        class _FakeJsonPlugin:
            lang = "json"
            language = "json"

            def indexFile(self, path, content):
                calls.append(Path(path).name)
                return {"symbols": [], "chunks": [], "metadata": {"fake": True}}

        monkeypatch.setattr(
            Dispatcher, "_ensure_plugin_loaded", lambda _self, language: _FakeJsonPlugin()
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["last_progress_path"] == str(config_file.resolve())
        assert calls == ["config.json"]

    def test_index_directory_keeps_other_bounded_python_tests_on_chunked_path(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.plugins.python_plugin.plugin import Plugin
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        test_file = repo / "tests" / "test_benchmarks.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text(
            "def test_benchmark_listing():\n" "    return 'ok'\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )

        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["last_progress_path"] == str(test_file.resolve())
        with store._get_connection() as conn:
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'tests/test_benchmarks.py')"
            ).fetchone()[0]
        store.close()
        assert chunk_count > 0
        assert "scripts/quick_mcp_vs_native_validation.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "scripts/verify_embeddings.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "scripts/claude_code_behavior_simulator.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "scripts/create_semantic_embeddings.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "scripts/consolidate_real_performance_data.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/docs/test_gaclose_evidence_closeout.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/docs/test_p8_deployment_security.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "scripts/rerun_failed_native_tests.py" not in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/test_artifact_publish_race.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/root_tests/run_reranking_tests.py" not in Plugin._BOUNDED_CHUNK_PATHS
        assert "scripts/validate_mcp_comprehensive.py" not in Plugin._BOUNDED_CHUNK_PATHS
        assert "mcp_server/visualization/quick_charts.py" in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/root_tests/test_voyage_api.py" not in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/test_benchmarks.py" not in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/docs/test_mre2e_evidence_contract.py" not in Plugin._BOUNDED_CHUNK_PATHS
        assert "tests/security/test_plugin_sandbox.py" not in Plugin._BOUNDED_CHUNK_PATHS

    def test_index_directory_keeps_unrelated_status_markdown_on_heavy_path(
        self, tmp_path, monkeypatch
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo = tmp_path / "repo"
        status_doc = repo / "docs" / "status" / "SEMANTIC_DOGFOOD_REBUILD.md"
        status_doc.parent.mkdir(parents=True)
        status_doc.write_text(
            "# Semantic Dogfood Rebuild\n\n## Dogfood Verdict\n\n- Preserve the existing heavy Markdown path here.\n",
            encoding="utf-8",
        )
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = RepoContext(
            repo_id="test-repo-id-0001",
            sqlite_store=store,
            workspace_root=repo,
            tracked_branch="main",
            registry_entry=SimpleNamespace(
                tracked_branch="main",
                path=repo,
                name="repo",
                repository_id="test-repo-id-0001",
            ),
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)

        result = Dispatcher([]).index_directory(ctx, repo)

        assert result["indexed_files"] == 1
        assert result["failed_files"] == 0
        assert result["last_progress_path"] == str(status_doc.resolve())
        with store._get_connection() as conn:
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM code_chunks WHERE file_id IN (SELECT id FROM files WHERE relative_path = 'docs/status/SEMANTIC_DOGFOOD_REBUILD.md')"
            ).fetchone()[0]
        store.close()
        assert chunk_count > 0

    def test_index_directory_runs_lexical_then_summaries_then_semantic(self, tmp_path, monkeypatch):
        events = []
        ctx = _make_repo_ctx(sqlite_store=MagicMock(db_path=str(tmp_path / "index.db")))
        target = tmp_path / "sample.py"
        target.write_text("x = 1\n")

        class FakeWriter:
            def __init__(self, *args, **kwargs):
                pass

            async def process_scope(self, **kwargs):
                events.append(("summary", sorted(str(path) for path in kwargs["target_paths"])))
                return SimpleNamespace(
                    summaries_written=1,
                    chunks_attempted=1,
                    authoritative_chunks=1,
                    missing_chunk_ids=[],
                    files_attempted=1,
                    files_summarized=1,
                )

        class FakeSemanticIndexer:
            def index_files_batch(self, paths, **kwargs):
                events.append(("semantic", [str(path) for path in paths], kwargs))
                return {"files_indexed": 1, "files_failed": 0, "files_skipped": 0}

        remaining_counts = [1, 0]

        monkeypatch.setattr(
            "mcp_server.indexing.summarization.ComprehensiveChunkWriter",
            FakeWriter,
        )
        monkeypatch.setattr(
            "mcp_server.setup.semantic_preflight.run_semantic_preflight",
            lambda **_kwargs: SimpleNamespace(
                to_dict=lambda: {"can_write_semantic_vectors": True, "blocker": None}
            ),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_get_semantic_indexer",
            lambda self, _ctx: FakeSemanticIndexer(),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_count_missing_summaries_for_paths",
            lambda self, _ctx, _paths: 0 if events and events[-1][0] == "summary" else 1,
        )

        def _fake_index_file(self, _ctx, path, do_semantic=False):
            events.append(("lexical", str(path), do_semantic))
            return IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            )

        monkeypatch.setattr(Dispatcher, "index_file", _fake_index_file)

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert [event[0] for event in events] == ["lexical", "summary", "semantic"]
        assert result["lexical_stage"] == "completed"
        assert result["lexical_files_attempted"] == 1
        assert result["lexical_files_completed"] == 1
        assert result["last_progress_path"] == str(target.resolve())
        assert result["semantic_stage"] == "indexed"

    def test_index_directory_blocks_semantic_stage_when_summaries_missing(
        self, tmp_path, monkeypatch
    ):
        ctx = _make_repo_ctx(sqlite_store=MagicMock(db_path=str(tmp_path / "index.db")))
        target = tmp_path / "sample.py"
        target.write_text("x = 1\n")

        class FakeWriter:
            def __init__(self, *args, **kwargs):
                pass

            async def process_scope(self, **kwargs):
                return SimpleNamespace(
                    summaries_written=0,
                    chunks_attempted=1,
                    authoritative_chunks=0,
                    missing_chunk_ids=["chunk-1"],
                    files_attempted=1,
                    files_summarized=0,
                )

        semantic_called = {"value": False}

        class FakeSemanticIndexer:
            def index_files_batch(self, paths, **kwargs):
                semantic_called["value"] = True
                return {"files_indexed": 0, "files_failed": 0, "files_skipped": 0}

        monkeypatch.setattr(
            "mcp_server.indexing.summarization.ComprehensiveChunkWriter",
            FakeWriter,
        )
        monkeypatch.setattr(
            "mcp_server.setup.semantic_preflight.run_semantic_preflight",
            lambda **_kwargs: SimpleNamespace(
                to_dict=lambda: {"can_write_semantic_vectors": True, "blocker": None}
            ),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_get_semantic_indexer",
            lambda self, _ctx: FakeSemanticIndexer(),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_count_missing_summaries_for_paths",
            lambda self, _ctx, _paths: 1,
        )
        monkeypatch.setattr(
            Dispatcher,
            "index_file",
            lambda self, _ctx, path, do_semantic=False: IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            ),
        )

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert semantic_called["value"] is False
        assert result["semantic_stage"] == "blocked_missing_summaries"
        assert result["semantic_blocked"] == 1

    def test_index_directory_retries_summary_generation_until_scope_is_drained(
        self, tmp_path, monkeypatch
    ):
        summary_limits = []
        ctx = _make_repo_ctx(sqlite_store=MagicMock(db_path=str(tmp_path / "index.db")))
        target = tmp_path / "sample.py"
        target.write_text("x = 1\n")

        class FakeWriter:
            call_count = 0

            def __init__(self, *args, **kwargs):
                pass

            async def process_scope(self, **kwargs):
                FakeWriter.call_count += 1
                summary_limits.append(kwargs["limit"])
                assert kwargs["max_batches"] == 1
                if FakeWriter.call_count == 1:
                    return SimpleNamespace(
                        summaries_written=1,
                        chunks_attempted=1,
                        authoritative_chunks=1,
                        missing_chunk_ids=[],
                        files_attempted=1,
                        files_summarized=1,
                    )
                return SimpleNamespace(
                    summaries_written=1,
                    chunks_attempted=1,
                    authoritative_chunks=1,
                    missing_chunk_ids=[],
                    files_attempted=1,
                    files_summarized=1,
                )

        class FakeSemanticIndexer:
            def index_files_batch(self, paths, **kwargs):
                return {"files_indexed": 1, "files_failed": 0, "files_skipped": 0}

        unresolved = [{"remaining": 2}, {"remaining": 1}, {"remaining": 0}]

        monkeypatch.setattr(
            "mcp_server.indexing.summarization.ComprehensiveChunkWriter",
            FakeWriter,
        )
        monkeypatch.setattr(
            "mcp_server.setup.semantic_preflight.run_semantic_preflight",
            lambda **_kwargs: SimpleNamespace(
                to_dict=lambda: {"can_write_semantic_vectors": True, "blocker": None}
            ),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_get_semantic_indexer",
            lambda self, _ctx: FakeSemanticIndexer(),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_count_missing_summaries_for_paths",
            lambda self, _ctx, _paths: remaining_counts.pop(0),
        )
        monkeypatch.setattr(
            Dispatcher,
            "index_file",
            lambda self, _ctx, path, do_semantic=False: IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            ),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_count_missing_summaries_for_paths",
            lambda self, _ctx, _paths: unresolved.pop(0)["remaining"],
        )

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert result["semantic_stage"] == "indexed"
        assert result["summaries_written"] == 2
        assert result["summary_chunks_attempted"] == 2
        assert summary_limits == [64, 64]

    def test_index_directory_blocks_when_summary_progress_plateaus(self, tmp_path, monkeypatch):
        ctx = _make_repo_ctx(sqlite_store=MagicMock(db_path=str(tmp_path / "index.db")))
        target = tmp_path / "sample.py"
        target.write_text("x = 1\n")

        class FakeWriter:
            def __init__(self, *args, **kwargs):
                pass

            async def process_scope(self, **kwargs):
                assert kwargs["max_batches"] == 1
                return SimpleNamespace(
                    summaries_written=1,
                    chunks_attempted=1,
                    authoritative_chunks=1,
                    missing_chunk_ids=["chunk-1"],
                    files_attempted=1,
                    files_summarized=1,
                )

        semantic_called = {"value": False}

        class FakeSemanticIndexer:
            def index_files_batch(self, paths, **kwargs):
                semantic_called["value"] = True
                return {"files_indexed": 1, "files_failed": 0, "files_skipped": 0}

        unresolved = [{"remaining": 2}, {"remaining": 2}, {"remaining": 2}]

        monkeypatch.setattr(
            "mcp_server.indexing.summarization.ComprehensiveChunkWriter",
            FakeWriter,
        )
        monkeypatch.setattr(
            "mcp_server.setup.semantic_preflight.run_semantic_preflight",
            lambda **_kwargs: SimpleNamespace(
                to_dict=lambda: {"can_write_semantic_vectors": True, "blocker": None}
            ),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_get_semantic_indexer",
            lambda self, _ctx: FakeSemanticIndexer(),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_count_missing_summaries_for_paths",
            lambda self, _ctx, _paths: unresolved.pop(0)["remaining"],
        )
        monkeypatch.setattr(
            Dispatcher,
            "index_file",
            lambda self, _ctx, path, do_semantic=False: IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            ),
        )

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert semantic_called["value"] is False
        assert result["semantic_stage"] == "blocked_summary_plateau"
        assert result["semantic_blocked"] == 1
        assert result["summary_missing_chunks"] == 2
        assert result["summary_passes"] == 2

    def test_index_directory_retries_summary_timeout_with_smaller_batch_limit(
        self, tmp_path, monkeypatch
    ):
        summary_limits = []
        ctx = _make_repo_ctx(sqlite_store=MagicMock(db_path=str(tmp_path / "index.db")))
        target = tmp_path / "sample.py"
        target.write_text("x = 1\n")

        class FakeWriter:
            call_count = 0

            def __init__(self, *args, **kwargs):
                pass

            async def process_scope(self, **kwargs):
                FakeWriter.call_count += 1
                summary_limits.append(kwargs["limit"])
                assert kwargs["max_batches"] == 1
                if FakeWriter.call_count == 1:
                    raise TimeoutError("synthetic timeout")
                return SimpleNamespace(
                    summaries_written=1,
                    chunks_attempted=1,
                    authoritative_chunks=1,
                    missing_chunk_ids=[],
                    files_attempted=1,
                    files_summarized=1,
                )

        class FakeSemanticIndexer:
            def index_files_batch(self, paths, **kwargs):
                return {"files_indexed": 1, "files_failed": 0, "files_skipped": 0}

        unresolved = [{"remaining": 1}, {"remaining": 1}, {"remaining": 0}]

        monkeypatch.setattr(
            "mcp_server.indexing.summarization.ComprehensiveChunkWriter",
            FakeWriter,
        )
        monkeypatch.setattr(
            "mcp_server.setup.semantic_preflight.run_semantic_preflight",
            lambda **_kwargs: SimpleNamespace(
                to_dict=lambda: {"can_write_semantic_vectors": True, "blocker": None}
            ),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_get_semantic_indexer",
            lambda self, _ctx: FakeSemanticIndexer(),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_count_missing_summaries_for_paths",
            lambda self, _ctx, _paths: unresolved.pop(0)["remaining"],
        )
        monkeypatch.setattr(
            Dispatcher,
            "index_file",
            lambda self, _ctx, path, do_semantic=False: IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            ),
        )

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert result["semantic_stage"] == "indexed"
        assert result["summaries_written"] == 1
        assert result["summary_passes"] == 1
        assert summary_limits == [64, 32]

    def test_index_directory_blocks_on_first_summary_call_timeout(self, tmp_path, monkeypatch):
        ctx = _make_repo_ctx(sqlite_store=MagicMock(db_path=str(tmp_path / "index.db")))
        first = tmp_path / "sample_a.md"
        second = tmp_path / "sample_b.md"
        first.write_text("doc\n")
        second.write_text("doc\n")

        class FakeWriter:
            def __init__(self, *args, **kwargs):
                pass

            async def process_scope(self, **kwargs):
                assert kwargs["max_batches"] == 1
                return SimpleNamespace(
                    summaries_written=0,
                    chunks_attempted=1,
                    authoritative_chunks=0,
                    missing_chunk_ids=["sample-a-chunk-1", "sample-a-chunk-2"],
                    files_attempted=1,
                    files_summarized=0,
                    remaining_chunks=2,
                    scope_drained=False,
                    blocked_call_reason="timeout",
                    blocked_call_file_path=str(first),
                    blocked_call_chunk_ids=["sample-a-chunk-1"],
                    blocked_call_timeout_seconds=30.0,
                )

        semantic_called = {"value": False}

        class FakeSemanticIndexer:
            def index_files_batch(self, paths, **kwargs):
                semantic_called["value"] = True
                return {"files_indexed": 2, "files_failed": 0, "files_skipped": 0}

        monkeypatch.setattr(
            "mcp_server.indexing.summarization.ComprehensiveChunkWriter",
            FakeWriter,
        )
        monkeypatch.setattr(
            "mcp_server.setup.semantic_preflight.run_semantic_preflight",
            lambda **_kwargs: SimpleNamespace(
                to_dict=lambda: {"can_write_semantic_vectors": True, "blocker": None}
            ),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_get_semantic_indexer",
            lambda self, _ctx: FakeSemanticIndexer(),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_count_missing_summaries_for_paths",
            lambda self, _ctx, _paths: 2,
        )
        monkeypatch.setattr(
            Dispatcher,
            "index_file",
            lambda self, _ctx, path, do_semantic=False: IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            ),
        )

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert semantic_called["value"] is False
        assert result["semantic_stage"] == "blocked_summary_call_timeout"
        assert result["semantic_blocked"] == 2
        assert result["summary_passes"] == 0
        assert result["summary_missing_chunks"] == 2
        assert result["summary_remaining_chunks"] == 2
        assert result["summary_call_timed_out"] is True
        assert result["summary_call_file_path"] == str(first)
        assert result["summary_call_chunk_ids"] == ["sample-a-chunk-1"]
        assert result["summary_call_timeout_seconds"] == 30.0

    def test_index_directory_emits_force_full_progress_snapshots(self, tmp_path, monkeypatch):
        ctx = _make_repo_ctx(sqlite_store=MagicMock(db_path=str(tmp_path / "index.db")))
        first = tmp_path / "sample_a.md"
        second = tmp_path / "sample_b.md"
        first.write_text("doc\n")
        second.write_text("doc\n")
        snapshots = []

        class FakeWriter:
            def __init__(self, *args, **kwargs):
                pass

            async def process_scope(self, **kwargs):
                return SimpleNamespace(
                    summaries_written=0,
                    chunks_attempted=1,
                    authoritative_chunks=0,
                    missing_chunk_ids=["sample-a-chunk-1", "sample-a-chunk-2"],
                    files_attempted=1,
                    files_summarized=0,
                    remaining_chunks=2,
                    scope_drained=False,
                    blocked_call_reason="timeout",
                    blocked_call_file_path=str(first),
                    blocked_call_chunk_ids=["sample-a-chunk-1"],
                    blocked_call_timeout_seconds=30.0,
                )

        monkeypatch.setattr(
            "mcp_server.indexing.summarization.ComprehensiveChunkWriter",
            FakeWriter,
        )
        monkeypatch.setattr(
            Dispatcher,
            "_get_semantic_indexer",
            lambda self, _ctx: object(),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_count_missing_summaries_for_paths",
            lambda self, _ctx, _paths: 2,
        )
        monkeypatch.setattr(
            Dispatcher,
            "index_file",
            lambda self, _ctx, path, do_semantic=False: IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            ),
        )

        result = Dispatcher([]).index_directory(
            ctx,
            tmp_path,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        stages = [snapshot["stage"] for snapshot in snapshots]
        assert stages == [
            "lexical_walking",
            "lexical_walking",
            "lexical_walking",
            "lexical_walking",
            "force_full_closeout_handoff",
            "summary_shutdown_started",
            "blocked_summary_call_timeout",
        ]
        assert snapshots[0]["stage_family"] == "lexical"
        assert snapshots[1]["last_progress_path"] == str(first.resolve())
        assert snapshots[2]["in_flight_path"] == str(second.resolve())
        assert snapshots[3]["last_progress_path"] == str(second.resolve())
        assert snapshots[4]["stage_family"] == "final_closeout"
        assert snapshots[4]["last_progress_path"] == str(second.resolve())
        assert snapshots[4]["in_flight_path"] is None
        assert snapshots[5]["stage_family"] == "summary_shutdown"
        assert snapshots[6]["stage_family"] == "semantic_closeout"
        assert snapshots[6]["summary_call_timed_out"] is True
        assert snapshots[6]["summary_call_file_path"] == str(first)
        assert snapshots[6]["semantic_stage"] == "blocked_summary_call_timeout"
        assert result["semantic_stage"] == "blocked_summary_call_timeout"

    def test_index_directory_classifies_post_lexical_storage_closeout(self, tmp_path, monkeypatch):
        store = MagicMock(db_path=str(tmp_path / "index.db"))
        store.health_check.return_value = {
            "status": "degraded",
            "journal_mode": "WAL",
            "busy_timeout_ms": 5000,
            "readonly": True,
        }
        store.get_readonly_diagnostics.return_value = {
            "storage_failure_family": "sqlite_operational",
            "storage_failure_reason": "disk_io_error",
            "storage_failure_message": "disk I/O error",
            "readonly": True,
        }
        ctx = _make_repo_ctx(sqlite_store=store)
        sample = tmp_path / "sample.py"
        sample.write_text("x = 1\n", encoding="utf-8")
        snapshots = []

        class FakeSemanticIndexer:
            def index_files_batch(self, *_args, **_kwargs):
                raise TransientArtifactError("disk I/O error")

        monkeypatch.setattr(
            "mcp_server.dispatcher.dispatcher_enhanced.reload_settings",
            lambda: SimpleNamespace(
                semantic_default_profile="oss_high",
                semantic_preflight_timeout_seconds=5,
                get_profile_summarization_config=lambda _profile: {},
            ),
        )
        monkeypatch.setattr(
            "mcp_server.setup.semantic_preflight.run_semantic_preflight",
            lambda **_kwargs: SimpleNamespace(
                to_dict=lambda: {"can_write_semantic_vectors": True, "blocker": None}
            ),
        )
        monkeypatch.setattr(
            Dispatcher, "_get_semantic_indexer", lambda self, _ctx: FakeSemanticIndexer()
        )
        monkeypatch.setattr(
            Dispatcher,
            "_count_missing_summaries_for_paths",
            lambda self, _ctx, _paths: 0,
        )
        monkeypatch.setattr(
            Dispatcher,
            "index_file",
            lambda self, _ctx, path, do_semantic=False: IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            ),
        )

        result = Dispatcher([]).index_directory(
            ctx,
            tmp_path,
            progress_callback=lambda snapshot: snapshots.append(dict(snapshot)),
        )

        assert result["semantic_stage"] == "blocked_storage_error"
        assert result["semantic_error"] == "disk I/O error"
        assert result["semantic_blocker"] == {
            "code": "storage_closeout",
            "message": "disk I/O error",
        }
        assert result["storage_failure_family"] == "sqlite_operational"
        assert result["storage_failure_reason"] == "disk_io_error"
        assert result["storage_diagnostics"]["readonly"] is True
        assert snapshots[-1]["stage"] == "blocked_storage_error"
        assert snapshots[-1]["stage_family"] == "final_closeout"
        assert snapshots[-1]["blocker_source"] == "storage_closeout"

    def test_index_directory_skips_fast_report_family_via_repo_ignore_boundary(
        self, tmp_path, monkeypatch
    ):
        ctx = _make_repo_ctx()
        (tmp_path / ".mcp-index-ignore").write_text("fast_test_results/fast_report_*.md\n")
        sample = tmp_path / "src" / "sample.py"
        sample.parent.mkdir()
        sample.write_text("x = 1\n")
        fast_report = tmp_path / "fast_test_results" / "fast_report_20250628_193425.md"
        fast_report.parent.mkdir()
        fast_report.write_text("# generated\n")

        monkeypatch.setattr(
            "mcp_server.dispatcher.dispatcher_enhanced.os.walk",
            lambda _root, followlinks=False: [
                (str(tmp_path), ["src", "fast_test_results"], []),
                (str(sample.parent), [], [sample.name]),
                (str(fast_report.parent), [], [fast_report.name]),
            ],
        )
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)
        monkeypatch.setattr(
            Dispatcher,
            "index_file",
            lambda self, _ctx, path, do_semantic=False: IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            ),
        )

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert result["indexed_files"] == 1
        assert result["ignored_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_files_attempted"] == 1
        assert result["last_progress_path"] == str(sample.resolve())
        assert result["last_progress_path"] != str(fast_report.resolve())

    def test_index_directory_prunes_test_workspace_after_devcontainer_tail(
        self, tmp_path, monkeypatch
    ):
        ctx = _make_repo_ctx()
        (tmp_path / ".mcp-index-ignore").write_text(
            "fast_test_results/fast_report_*.md\ntest_workspace/\n"
        )
        devcontainer = tmp_path / ".devcontainer" / "devcontainer.json"
        devcontainer.parent.mkdir()
        devcontainer.write_text('{"name": "devcontainer"}\n', encoding="utf-8")
        fast_report = tmp_path / "fast_test_results" / "fast_report_20250628_193425.md"
        fast_report.parent.mkdir()
        fast_report.write_text("# generated\n", encoding="utf-8")
        ignored_workspace_file = (
            tmp_path / "test_workspace" / "real_repos" / "search_scaling" / "package.json"
        )
        ignored_workspace_file.parent.mkdir(parents=True)
        ignored_workspace_file.write_text('{"name": "ignored-fixture"}\n', encoding="utf-8")
        later_file = tmp_path / "src" / "later.py"
        later_file.parent.mkdir()
        later_file.write_text("value = 1\n", encoding="utf-8")

        def _fake_walk(_root, followlinks=False):
            root_dirnames = [".devcontainer", "fast_test_results", "test_workspace", "src"]
            yield str(tmp_path), root_dirnames, []
            if ".devcontainer" in root_dirnames:
                yield str(devcontainer.parent), [], [devcontainer.name]
            if "fast_test_results" in root_dirnames:
                yield str(fast_report.parent), [], [fast_report.name]
            if "test_workspace" in root_dirnames:
                raise AssertionError(
                    "ignored test_workspace subtree should be pruned before walking"
                )
            if "src" in root_dirnames:
                yield str(later_file.parent), [], [later_file.name]

        monkeypatch.setattr("mcp_server.dispatcher.dispatcher_enhanced.os.walk", _fake_walk)
        monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: None)
        monkeypatch.setattr(
            Dispatcher,
            "index_file",
            lambda self, _ctx, path, do_semantic=False: IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            ),
        )

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert result["indexed_files"] == 2
        assert result["ignored_files"] == 1
        assert result["failed_files"] == 0
        assert result["lexical_files_attempted"] == 2
        assert result["last_progress_path"] == str(later_file.resolve())
        assert result["last_progress_path"] != str(devcontainer.resolve())
        assert result["last_progress_path"] != str(ignored_workspace_file.resolve())

    def test_index_directory_keeps_halving_summary_limit_until_timeout_recovers(
        self, tmp_path, monkeypatch
    ):
        summary_limits = []
        ctx = _make_repo_ctx(sqlite_store=MagicMock(db_path=str(tmp_path / "index.db")))
        target = tmp_path / "sample.py"
        target.write_text("x = 1\n")

        class FakeWriter:
            call_count = 0

            def __init__(self, *args, **kwargs):
                pass

            async def process_scope(self, **kwargs):
                FakeWriter.call_count += 1
                summary_limits.append(kwargs["limit"])
                assert kwargs["max_batches"] == 1
                if FakeWriter.call_count < 4:
                    raise TimeoutError("synthetic timeout")
                return SimpleNamespace(
                    summaries_written=1,
                    chunks_attempted=1,
                    authoritative_chunks=1,
                    missing_chunk_ids=[],
                    files_attempted=1,
                    files_summarized=1,
                    remaining_chunks=0,
                    scope_drained=True,
                )

        class FakeSemanticIndexer:
            def index_files_batch(self, paths, **kwargs):
                return {"files_indexed": 1, "files_failed": 0, "files_skipped": 0}

        unresolved = [1, 1, 1, 1, 0]

        monkeypatch.setattr(
            "mcp_server.indexing.summarization.ComprehensiveChunkWriter",
            FakeWriter,
        )
        monkeypatch.setattr(
            "mcp_server.setup.semantic_preflight.run_semantic_preflight",
            lambda **_kwargs: SimpleNamespace(
                to_dict=lambda: {"can_write_semantic_vectors": True, "blocker": None}
            ),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_get_semantic_indexer",
            lambda self, _ctx: FakeSemanticIndexer(),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_count_missing_summaries_for_paths",
            lambda self, _ctx, _paths: unresolved.pop(0),
        )
        monkeypatch.setattr(
            Dispatcher,
            "index_file",
            lambda self, _ctx, path, do_semantic=False: IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            ),
        )

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert result["semantic_stage"] == "indexed"
        assert result["summaries_written"] == 1
        assert result["summary_passes"] == 1
        assert summary_limits == [64, 32, 16, 8]

    def test_index_directory_returns_bounded_summary_continuation_for_repo_scope(
        self, tmp_path, monkeypatch
    ):
        ctx = _make_repo_ctx(sqlite_store=MagicMock(db_path=str(tmp_path / "index.db")))
        first = tmp_path / "sample_a.py"
        second = tmp_path / "sample_b.py"
        first.write_text("x = 1\n")
        second.write_text("y = 2\n")

        class FakeWriter:
            call_count = 0

            def __init__(self, *args, **kwargs):
                pass

            async def process_scope(self, **kwargs):
                FakeWriter.call_count += 1
                assert kwargs["max_batches"] == 1
                return SimpleNamespace(
                    summaries_written=1,
                    chunks_attempted=1,
                    authoritative_chunks=1,
                    missing_chunk_ids=[f"chunk-{FakeWriter.call_count}"],
                    files_attempted=1,
                    files_summarized=1,
                )

        semantic_called = {"value": False}

        class FakeSemanticIndexer:
            def index_files_batch(self, paths, **kwargs):
                semantic_called["value"] = True
                return {"files_indexed": 2, "files_failed": 0, "files_skipped": 0}

        unresolved = [9, 8, 7, 6, 5, 4, 3, 2, 1]

        monkeypatch.setattr(
            "mcp_server.indexing.summarization.ComprehensiveChunkWriter",
            FakeWriter,
        )
        monkeypatch.setattr(
            "mcp_server.setup.semantic_preflight.run_semantic_preflight",
            lambda **_kwargs: SimpleNamespace(
                to_dict=lambda: {"can_write_semantic_vectors": True, "blocker": None}
            ),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_get_semantic_indexer",
            lambda self, _ctx: FakeSemanticIndexer(),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_count_missing_summaries_for_paths",
            lambda self, _ctx, _paths: unresolved.pop(0),
        )
        monkeypatch.setattr(
            Dispatcher,
            "index_file",
            lambda self, _ctx, path, do_semantic=False: IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            ),
        )

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert semantic_called["value"] is False
        assert result["semantic_stage"] == "blocked_missing_summaries"
        assert result["semantic_blocked"] == 2
        assert result["summary_passes"] == 8
        assert result["summaries_written"] == 8
        assert result["summary_missing_chunks"] == 1
        assert result["summary_remaining_chunks"] == 1
        assert result["summary_scope_drained"] is False
        assert result["summary_continuation_required"] is True
        assert "8 bounded summary passes" in result["semantic_error"]

    def test_index_directory_prefers_writer_reported_remaining_for_repo_scope_continuation(
        self, tmp_path, monkeypatch
    ):
        ctx = _make_repo_ctx(sqlite_store=MagicMock(db_path=str(tmp_path / "index.db")))
        first = tmp_path / "sample_a.md"
        second = tmp_path / "sample_b.md"
        first.write_text("doc\n")
        second.write_text("doc\n")

        class FakeWriter:
            call_count = 0

            def __init__(self, *args, **kwargs):
                pass

            async def process_scope(self, **kwargs):
                FakeWriter.call_count += 1
                assert kwargs["max_batches"] == 1
                return SimpleNamespace(
                    summaries_written=1,
                    chunks_attempted=1,
                    authoritative_chunks=1,
                    missing_chunk_ids=[f"chunk-{FakeWriter.call_count}"],
                    files_attempted=1,
                    files_summarized=1,
                    remaining_chunks=9 - FakeWriter.call_count,
                    scope_drained=False,
                )

        semantic_called = {"value": False}

        class FakeSemanticIndexer:
            def index_files_batch(self, paths, **kwargs):
                semantic_called["value"] = True
                return {"files_indexed": 2, "files_failed": 0, "files_skipped": 0}

        fallback_counts = [99] * 8

        monkeypatch.setattr(
            "mcp_server.indexing.summarization.ComprehensiveChunkWriter",
            FakeWriter,
        )
        monkeypatch.setattr(
            "mcp_server.setup.semantic_preflight.run_semantic_preflight",
            lambda **_kwargs: SimpleNamespace(
                to_dict=lambda: {"can_write_semantic_vectors": True, "blocker": None}
            ),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_get_semantic_indexer",
            lambda self, _ctx: FakeSemanticIndexer(),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_count_missing_summaries_for_paths",
            lambda self, _ctx, _paths: fallback_counts.pop(0),
        )
        monkeypatch.setattr(
            Dispatcher,
            "index_file",
            lambda self, _ctx, path, do_semantic=False: IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            ),
        )

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert semantic_called["value"] is False
        assert result["semantic_stage"] == "blocked_missing_summaries"
        assert result["summary_passes"] == 8
        assert result["summary_remaining_chunks"] == 1
        assert result["summary_missing_chunks"] == 1
        assert result["summary_scope_drained"] is False
        assert result["summary_continuation_required"] is True

    def test_index_directory_bootstraps_missing_collection_before_semantic_writes(
        self, tmp_path, monkeypatch
    ):
        events = []
        ctx = _make_repo_ctx(sqlite_store=MagicMock(db_path=str(tmp_path / "index.db")))
        target = tmp_path / "sample.py"
        target.write_text("x = 1\n")

        class FakeWriter:
            def __init__(self, *args, **kwargs):
                pass

            async def process_scope(self, **kwargs):
                assert kwargs["max_batches"] == 1
                return SimpleNamespace(
                    summaries_written=1,
                    chunks_attempted=1,
                    authoritative_chunks=1,
                    missing_chunk_ids=[],
                    files_attempted=1,
                    files_summarized=1,
                )

        preflight_calls = {"count": 0}

        def _fake_preflight(**_kwargs):
            preflight_calls["count"] += 1
            if preflight_calls["count"] == 1:
                payload = {
                    "overall_ready": False,
                    "can_write_semantic_vectors": False,
                    "blocker": {
                        "code": "collection_missing",
                        "message": "Qdrant collection is missing for the active semantic profile",
                    },
                    "effective_config": {
                        "selected_profile": "oss_high",
                        "collection_name": "code_index__oss_high__v1",
                        "normalized_collection_name": "code_index__oss_high__v1",
                        "qdrant_url": "http://localhost:6333",
                        "vector_dimension": 4096,
                        "distance_metric": "cosine",
                    },
                }
            else:
                payload = {
                    "overall_ready": True,
                    "can_write_semantic_vectors": True,
                    "blocker": None,
                    "effective_config": {
                        "selected_profile": "oss_high",
                        "collection_name": "code_index__oss_high__v1",
                        "normalized_collection_name": "code_index__oss_high__v1",
                        "qdrant_url": "http://localhost:6333",
                        "vector_dimension": 4096,
                        "distance_metric": "cosine",
                    },
                }
            return SimpleNamespace(to_dict=lambda: payload)

        class FakeSemanticIndexer:
            def index_files_batch(self, paths, **kwargs):
                events.append(("semantic", [str(path) for path in paths], kwargs))
                return {"files_indexed": 1, "files_failed": 0, "files_skipped": 0}

        monkeypatch.setattr(
            "mcp_server.indexing.summarization.ComprehensiveChunkWriter",
            FakeWriter,
        )
        monkeypatch.setattr(
            "mcp_server.setup.semantic_preflight.run_semantic_preflight",
            _fake_preflight,
        )
        monkeypatch.setattr(
            "mcp_server.setup.semantic_preflight.bootstrap_active_profile_collection",
            lambda **_kwargs: SimpleNamespace(
                to_dict=lambda: {"status": "created", "message": "created"},
                status="created",
                message="created",
            ),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_get_semantic_indexer",
            lambda self, _ctx: FakeSemanticIndexer(),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_count_missing_summaries_for_paths",
            lambda self, _ctx, _paths: remaining.pop(0),
        )
        monkeypatch.setattr(
            Dispatcher,
            "index_file",
            lambda self, _ctx, path, do_semantic=False: IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            ),
        )

        remaining = [1, 0]

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert preflight_calls["count"] == 2
        assert result["semantic_stage"] == "indexed"
        assert result["semantic_collection_bootstrap"]["status"] == "created"

    @pytest.mark.parametrize("indexed", [0, 1])
    @pytest.mark.parametrize("outcome", ["blocked", "failed"])
    def test_index_directory_records_semantic_batch_blockers(
        self, tmp_path, monkeypatch, indexed, outcome
    ):
        ctx = _make_repo_ctx(sqlite_store=MagicMock(db_path=str(tmp_path / "index.db")))
        target = tmp_path / "sample.py"
        target.write_text("x = 1\n")
        if indexed:
            (tmp_path / "successful.py").write_text("x = 2\n")

        class FakeWriter:
            def __init__(self, *args, **kwargs):
                pass

            async def process_scope(self, **kwargs):
                return SimpleNamespace(
                    summaries_written=1,
                    chunks_attempted=1,
                    authoritative_chunks=1,
                    missing_chunk_ids=[],
                    files_attempted=1,
                    files_summarized=1,
                )

        class FakeSemanticIndexer:
            def index_files_batch(self, paths, **kwargs):
                return {
                    "files_indexed": indexed,
                    "files_failed": int(outcome == "failed"),
                    "files_skipped": 0,
                    "files_blocked": int(outcome == "blocked"),
                    "semantic_blocker": {
                        "code": "semantic_batch_blocked",
                        "message": "Semantic batch writes were blocked after summaries were generated",
                    },
                    "semantic_error": "Semantic batch writes were blocked after summaries were generated",
                }

        monkeypatch.setattr(
            "mcp_server.indexing.summarization.ComprehensiveChunkWriter",
            FakeWriter,
        )
        monkeypatch.setattr(
            "mcp_server.setup.semantic_preflight.run_semantic_preflight",
            lambda **_kwargs: SimpleNamespace(
                to_dict=lambda: {"can_write_semantic_vectors": True, "blocker": None}
            ),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_get_semantic_indexer",
            lambda self, _ctx: FakeSemanticIndexer(),
        )
        monkeypatch.setattr(
            Dispatcher,
            "_count_missing_summaries_for_paths",
            lambda self, _ctx, _paths: 0,
        )
        monkeypatch.setattr(
            Dispatcher,
            "index_file",
            lambda self, _ctx, path, do_semantic=False: IndexResult(
                status=IndexResultStatus.INDEXED,
                path=path,
                observed_hash=None,
                actual_hash=None,
            ),
        )

        result = Dispatcher([]).index_directory(ctx, tmp_path)

        assert result["semantic_stage"] == f"{outcome}_semantic_batch"
        assert result[f"semantic_{outcome}"] == 1
        assert (
            result["semantic_error"]
            == "Semantic batch writes were blocked after summaries were generated"
        )

    def test_remove_file_accepts_ctx(self, tmp_path):
        """remove_file(ctx, path) must accept ctx as first positional arg."""
        ctx = _make_repo_ctx()
        d = Dispatcher([])
        f = tmp_path / "foo.py"
        f.write_text("x = 1")
        d.remove_file(ctx, f)  # must not raise

    def test_move_file_accepts_ctx(self, tmp_path):
        """move_file(ctx, old, new) must accept ctx as first positional arg."""
        ctx = _make_repo_ctx()
        d = Dispatcher([])
        d.move_file(ctx, tmp_path / "old.py", tmp_path / "new.py")  # must not raise

    def test_remove_file_uses_ctx_repository_row(self, tmp_path):
        """remove_file must delete from the repository row matching ctx root, not row 1."""
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()
        target_a = repo_a / "same.py"
        target_b = repo_b / "same.py"
        target_a.write_text("a = 1\n")
        target_b.write_text("b = 1\n")

        store = SQLiteStore(str(tmp_path / "index.db"))
        row_a = store.ensure_repository_row(repo_a, name="a")
        row_b = store.ensure_repository_row(repo_b, name="b")
        store.store_file(repository_id=row_a, path=target_a, relative_path="same.py")
        store.store_file(repository_id=row_b, path=target_b, relative_path="same.py")

        ctx = _make_repo_ctx(store)
        ctx.registry_entry.path = repo_b
        ctx.registry_entry.name = "b"
        ctx = RepoContext(
            repo_id=ctx.repo_id,
            sqlite_store=store,
            workspace_root=repo_b,
            tracked_branch=ctx.tracked_branch,
            registry_entry=ctx.registry_entry,
        )

        result = Dispatcher([]).remove_file(ctx, target_b)

        assert result.status == IndexResultStatus.DELETED
        assert store.get_file_by_path("same.py", row_a) is not None
        assert store.get_file_by_path("same.py", row_b) is None

    def test_index_file_returns_not_found_for_missing_required_path(self, tmp_path):
        ctx = _make_repo_ctx(workspace_root=tmp_path)
        result = Dispatcher([]).index_file(ctx, tmp_path / "missing.py")

        assert result.status == IndexResultStatus.NOT_FOUND
        assert result.error == "file not found"

    def test_index_file_returns_explicit_plugin_failure(self, tmp_path):
        plugin = MagicMock(spec=IPlugin, lang="python")
        plugin.supports.return_value = True
        plugin.indexFile.side_effect = RuntimeError("plugin boom")
        dispatcher = Dispatcher([plugin])
        ctx = _make_repo_ctx(workspace_root=tmp_path)
        target = tmp_path / "test.py"
        target.write_text("x = 1\n")

        result = dispatcher.index_file(ctx, target)

        assert result.status == IndexResultStatus.ERROR
        assert result.error == "plugin boom"

    def test_index_file_persists_plugin_shard_to_ctx_sqlite_store(self, tmp_path):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        target = repo_root / "sample.py"
        target.write_text("def persisted():\n    return 1\n")
        store = SQLiteStore(str(tmp_path / "index.db"))

        plugin = MagicMock(spec=IPlugin, lang="python")
        plugin.language = "python"
        plugin.supports.return_value = True
        plugin.indexFile.return_value = {
            "file": str(target),
            "language": "python",
            "symbols": [
                {
                    "symbol": "persisted",
                    "kind": "function",
                    "line": 1,
                    "span": [1, 2],
                    "signature": "def persisted():",
                }
            ],
        }
        ctx = _make_repo_ctx(store)
        ctx.registry_entry.path = repo_root
        ctx.registry_entry.name = "repo"
        ctx = RepoContext(
            repo_id=ctx.repo_id,
            sqlite_store=store,
            workspace_root=repo_root,
            tracked_branch=ctx.tracked_branch,
            registry_entry=ctx.registry_entry,
        )

        result = Dispatcher([plugin]).index_file(ctx, target)

        repo_row = store.ensure_repository_row(repo_root, name="repo")
        file_row = store.get_file_by_path("sample.py", repo_row)
        assert result.status == IndexResultStatus.INDEXED
        assert file_row is not None
        assert store.get_symbol("persisted")[0]["file_id"] == file_row["id"]

    def test_index_file_returns_skipped_unchanged_when_cache_matches(self, tmp_path):
        plugin = MagicMock(spec=IPlugin, lang="python")
        plugin.supports.return_value = True
        plugin.indexFile.return_value = {"symbols": []}
        dispatcher = Dispatcher([plugin])
        ctx = _make_repo_ctx(workspace_root=tmp_path)
        target = tmp_path / "test.py"
        target.write_text("x = 1\n")

        first = dispatcher.index_file(ctx, target)
        second = dispatcher.index_file(ctx, target)

        assert first.status == IndexResultStatus.INDEXED
        assert second.status == IndexResultStatus.SKIPPED_UNCHANGED

    @pytest.mark.parametrize("guarded", [False, True])
    @pytest.mark.parametrize("failure", ["exception", "semantic_failed", "semantic_blocked"])
    def test_single_file_semantic_failure_retries_unchanged_content(
        self, tmp_path, monkeypatch, guarded, failure
    ):
        from mcp_server.storage.sqlite_store import SQLiteStore

        target = tmp_path / "sample.py"
        target.write_text("value = 1\n")
        store = SQLiteStore(str(tmp_path / "index.db"))
        ctx = _make_repo_ctx(store, workspace_root=tmp_path)
        plugin = MagicMock(spec=IPlugin, lang="python")
        plugin.language = "python"
        plugin.supports.return_value = True
        plugin.indexFile.return_value = {"symbols": []}
        dispatcher = Dispatcher([plugin], semantic_search_enabled=False)
        monkeypatch.setattr(dispatcher, "_get_semantic_indexer", lambda ctx: object())
        semantic = MagicMock(
            side_effect=[
                RuntimeError("provider failed") if failure == "exception" else {failure: 1},
                {"semantic_indexed": 1, "semantic_failed": 0, "semantic_blocked": 0},
            ]
        )
        monkeypatch.setattr(dispatcher, "rebuild_semantic_for_paths", semantic)

        def index():
            if guarded:
                return dispatcher.index_file_guarded(
                    ctx, target, hashlib.sha256(target.read_bytes()).hexdigest()
                )
            return dispatcher.index_file(ctx, target)

        try:
            assert index().status == IndexResultStatus.ERROR
            assert store.get_file(target) is not None
            assert str(target) not in dispatcher._file_cache
            assert index().status == IndexResultStatus.INDEXED
            assert semantic.call_count == 2
            assert index().status == IndexResultStatus.SKIPPED_UNCHANGED
        finally:
            dispatcher.shutdown()
            store.close()

    def test_skip_cache_does_not_suppress_writes_to_another_store(self, tmp_path):
        from dataclasses import replace

        from mcp_server.storage.sqlite_store import SQLiteStore

        target = tmp_path / "sample.py"
        target.write_text("value = 1\n")
        plugin = MagicMock(spec=IPlugin, lang="python")
        plugin.language = "python"
        plugin.supports.return_value = True
        plugin.indexFile.return_value = {"symbols": []}
        dispatcher = Dispatcher([plugin])
        stores = [SQLiteStore(str(tmp_path / name)) for name in ("active.db", "staged.db")]
        ctx = _make_repo_ctx(stores[0])
        ctx.registry_entry.path = tmp_path
        ctx = replace(ctx, workspace_root=tmp_path)
        try:
            assert dispatcher.index_file(ctx, target).status == IndexResultStatus.INDEXED
            staged_ctx = replace(ctx, sqlite_store=stores[1])
            assert dispatcher.index_file(staged_ctx, target).status == IndexResultStatus.INDEXED
            assert stores[1].get_file(target)["content_hash"] == dispatcher._get_file_hash(
                target.read_text()
            )
            assert (
                dispatcher.index_file(staged_ctx, target).status
                == IndexResultStatus.SKIPPED_UNCHANGED
            )
            target.write_text("value = 2\n")
            assert dispatcher.index_file(staged_ctx, target).status == IndexResultStatus.INDEXED
            assert dispatcher.index_file(ctx, target).status == IndexResultStatus.INDEXED
        finally:
            for store in stores:
                store.close()

    def test_remove_file_returns_not_found_when_repo_row_missing(self, tmp_path):
        from mcp_server.storage.sqlite_store import SQLiteStore

        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        target = repo_root / "missing.py"
        target.write_text("x = 1\n")
        store = SQLiteStore(str(tmp_path / "index.db"))
        store.ensure_repository_row(repo_root, name="repo")
        ctx = _make_repo_ctx(store)
        ctx.registry_entry.path = repo_root
        ctx.registry_entry.name = "repo"
        ctx = RepoContext(
            repo_id=ctx.repo_id,
            sqlite_store=store,
            workspace_root=repo_root,
            tracked_branch=ctx.tracked_branch,
            registry_entry=ctx.registry_entry,
        )

        result = Dispatcher([]).remove_file(ctx, target)

        assert result.status == IndexResultStatus.NOT_FOUND

    def test_runtime_feature_status_registered_refuses_missing_generation_owner(
        self,
    ):
        ctx = _make_repo_ctx(MagicMock())
        ctx.registry_entry.path = ctx.workspace_root
        d = Dispatcher([])
        d._semantic_registry = None
        d._semantic_indexer_fallback = None

        status = d.get_runtime_feature_status(ctx)

        assert status["semantic"]["status"] == "unavailable"
        assert status["semantic"]["reason"] == "semantic_indexer_unavailable"

    def test_plugins_takes_no_ctx(self):
        """plugins() is process-global; must accept no ctx arg."""
        d = Dispatcher([])
        result = d.plugins()
        assert isinstance(result, list)

    def test_supported_languages_takes_no_ctx(self):
        """supported_languages() is process-global; must accept no ctx arg."""
        d = Dispatcher([])
        result = d.supported_languages()
        assert isinstance(result, list)

    def test_get_plugins_for_file_accepts_ctx(self, tmp_path):
        """get_plugins_for_file(ctx, path) must accept ctx as first arg."""
        ctx = _make_repo_ctx()
        d = Dispatcher([])
        f = tmp_path / "test.py"
        result = d.get_plugins_for_file(ctx, f)
        assert isinstance(result, list)

    def test_search_scoped_to_ctx_repo_id(self):
        """Results from search(ctx_a, q) must not bleed store_b data."""
        store_a = MagicMock()
        store_a.search_bm25.return_value = [
            {"filepath": "/repo_a/x.py", "snippet": "foo", "score": 1.0, "language": "python"}
        ]
        store_a.find_best_chunk_for_file = MagicMock(return_value=None)

        store_b = MagicMock()
        store_b.search_bm25.return_value = [
            {"filepath": "/repo_b/y.py", "snippet": "foo", "score": 1.0, "language": "python"}
        ]

        ctx_a = _make_repo_ctx(store_a)
        d = Dispatcher([])
        list(d.search(ctx_a, "foo"))
        # store_b must never be touched
        store_b.search_bm25.assert_not_called()


class TestSimpleDispatcherProtocolConformance:
    """SimpleDispatcher must conform to the query subset of DispatcherProtocol (SL-1.1)."""

    def test_isinstance_dispatcher_protocol(self):
        """runtime_checkable isinstance check must pass for SimpleDispatcher."""
        d = SimpleDispatcher()
        assert isinstance(d, DispatcherProtocol)

    def test_ctor_rejects_sqlite_store_kwarg(self):
        """SimpleDispatcher() must raise TypeError when sqlite_store= is passed."""
        with pytest.raises(TypeError):
            SimpleDispatcher(sqlite_store=MagicMock())

    def test_search_accepts_ctx(self):
        """SimpleDispatcher.search(ctx, query) must accept ctx first positional arg."""
        store = MagicMock()
        store.search_bm25.return_value = []
        ctx = _make_repo_ctx(store)
        d = SimpleDispatcher()
        results = list(d.search(ctx, "hello"))
        assert isinstance(results, list)

    def test_health_check_accepts_ctx(self):
        """SimpleDispatcher.health_check(ctx) must accept ctx."""
        ctx = _make_repo_ctx()
        d = SimpleDispatcher()
        result = d.health_check(ctx)
        assert isinstance(result, dict)

    def test_get_statistics_accepts_ctx(self):
        """SimpleDispatcher.get_statistics(ctx) must accept ctx."""
        ctx = _make_repo_ctx()
        d = SimpleDispatcher()
        result = d.get_statistics(ctx)
        assert isinstance(result, dict)


class TestStorageCrossRepoCoordinatorDeleted:
    """mcp_server.storage.cross_repo_coordinator must be deleted (SL-1.1)."""

    def test_storage_cross_repo_import_raises(self):
        """Importing from mcp_server.storage.cross_repo_coordinator must raise ModuleNotFoundError."""
        import importlib
        import sys

        # Remove from sys.modules cache if already imported (pre-deletion)
        sys.modules.pop("mcp_server.storage.cross_repo_coordinator", None)
        with pytest.raises((ModuleNotFoundError, ImportError)):
            importlib.import_module("mcp_server.storage.cross_repo_coordinator")
