"""Tests for advanced dispatcher components."""

import time
from pathlib import Path
from typing import List

import pytest

from mcp_server.dispatcher import (
    FileTypeMatcher,
    PluginCapability,
    PluginRouter,
    RankingCriteria,
    ResultAggregator,
    SimpleAggregationStrategy,
    SmartAggregationStrategy,
)
from mcp_server.plugin_base import IPlugin, Reference


class MockPlugin(IPlugin):
    """Mock plugin for testing."""

    def __init__(self, lang: str, supported_extensions: List[str] = None):
        self.lang = lang
        self._supported_extensions = supported_extensions or []
        self._indexed_files = {}
        self._symbols = {}
        self._references = {}

    def supports(self, path) -> bool:
        """Check if plugin supports the file."""
        if isinstance(path, (str, Path)):
            path = Path(path)
            return path.suffix in self._supported_extensions
        return False

    def indexFile(self, path, content: str):
        """Index a file."""
        self._indexed_files[str(path)] = content
        return {"file": str(path), "symbols": [], "language": self.lang}

    def getDefinition(self, symbol: str):
        """Get symbol definition."""
        return self._symbols.get(symbol)

    def findReferences(self, symbol: str):
        """Find references to symbol."""
        return self._references.get(symbol, [])

    def search(self, query: str, opts=None):
        """Search for query."""
        # Simple mock search
        for file_path, content in self._indexed_files.items():
            if query.lower() in content.lower():
                yield {
                    "file": file_path,
                    "line": 1,
                    "snippet": content[:100],
                    "score": 0.8,
                }


@pytest.fixture
def python_plugin():
    """Create a Python plugin mock."""
    plugin = MockPlugin("python", [".py", ".pyx"])
    plugin._indexed_files = {
        "/test/file1.py": 'def hello_world():\n    print("Hello")',
        "/test/file2.py": "class TestClass:\n    def method(self):\n        pass",
    }
    plugin._symbols = {
        "hello_world": {
            "symbol": "hello_world",
            "kind": "function",
            "language": "python",
            "signature": "def hello_world()",
            "doc": "A simple hello world function",
            "defined_in": "/test/file1.py",
            "line": 1,
            "span": (0, 20),
        }
    }
    plugin._references = {"hello_world": [Reference(file="/test/file3.py", line=5)]}
    return plugin


@pytest.fixture
def javascript_plugin():
    """Create a JavaScript plugin mock."""
    plugin = MockPlugin("javascript", [".js", ".jsx", ".ts", ".tsx"])
    plugin._indexed_files = {
        "/test/app.js": 'function hello() {\n    console.log("Hello");\n}',
        "/test/utils.js": "export function utility() {\n    return true;\n}",
    }
    plugin._symbols = {
        "hello": {
            "symbol": "hello",
            "kind": "function",
            "language": "javascript",
            "signature": "function hello()",
            "doc": None,
            "defined_in": "/test/app.js",
            "line": 1,
            "span": (0, 15),
        }
    }
    return plugin


@pytest.fixture
def file_matcher():
    """Create a file type matcher."""
    return FileTypeMatcher()


@pytest.fixture
def plugin_router(file_matcher, python_plugin, javascript_plugin):
    """Create a plugin router with test plugins."""
    router = PluginRouter(file_matcher)

    # Register plugins with capabilities
    python_caps = [
        PluginCapability("syntax_analysis", "1.0", "Python syntax analysis", 80),
        PluginCapability("semantic_search", "1.0", "Semantic search for Python", 70),
    ]
    js_caps = [
        PluginCapability("syntax_analysis", "1.0", "JavaScript syntax analysis", 75),
        PluginCapability("linting", "1.0", "JavaScript linting", 85),
    ]

    router.register_plugin(python_plugin, python_caps)
    router.register_plugin(javascript_plugin, js_caps)

    return router


@pytest.fixture
def result_aggregator():
    """Create a result aggregator."""
    return ResultAggregator()


@pytest.fixture
def sample_search_results():
    """Create sample search results for testing."""
    return {
        "python_results": [
            {
                "file": "/test/file1.py",
                "line": 1,
                "snippet": "def hello_world():",
                "score": 0.9,
            },
            {
                "file": "/test/file2.py",
                "line": 5,
                "snippet": "    hello_world()",
                "score": 0.7,
            },
        ],
        "js_results": [
            {
                "file": "/test/app.js",
                "line": 1,
                "snippet": "function hello() {",
                "score": 0.8,
            },
            {
                "file": "/test/file1.py",  # Same file as Python result
                "line": 1,
                "snippet": "def hello_world():",  # Same content
                "score": 0.85,
            },
        ],
    }


class TestFileTypeMatcher:
    """Test FileTypeMatcher functionality."""

    def test_get_file_info_python(self, file_matcher):
        """Test file info for Python files."""
        path = Path("/test/example.py")
        info = file_matcher.get_file_info(path)

        assert info.extension == ".py"
        assert info.language == "python"
        assert not info.is_binary
        assert info.encoding == "utf-8"

    def test_get_file_info_javascript(self, file_matcher):
        """Test file info for JavaScript files."""
        path = Path("/test/app.js")
        info = file_matcher.get_file_info(path)

        assert info.extension == ".js"
        assert info.language == "javascript"
        assert not info.is_binary

    def test_get_file_info_binary(self, file_matcher):
        """Test file info for binary files."""
        path = Path("/test/image.png")
        info = file_matcher.get_file_info(path)

        assert info.extension == ".png"
        assert info.is_binary
        assert info.encoding == "binary"

    def test_get_file_info_dockerfile(self, file_matcher):
        """Test file info for Dockerfile."""
        path = Path("/test/Dockerfile")
        info = file_matcher.get_file_info(path)

        assert info.extension == ""
        assert info.language == "dockerfile"
        assert not info.is_binary

    def test_is_supported(self, file_matcher):
        """Test file support detection."""
        assert file_matcher.is_supported(Path("/test/file.py"))
        assert file_matcher.is_supported(Path("/test/file.js"))
        assert not file_matcher.is_supported(Path("/test/image.png"))

    def test_get_language(self, file_matcher):
        """Test language detection."""
        assert file_matcher.get_language(Path("/test/file.py")) == "python"
        assert file_matcher.get_language(Path("/test/file.js")) == "javascript"
        assert file_matcher.get_language(Path("/test/file.unknown")) is None

    def test_cache_behavior(self, file_matcher):
        """Test caching behavior."""
        path = Path("/test/example.py")

        # First call should populate cache
        info1 = file_matcher.get_file_info(path)

        # Second call should use cache
        info2 = file_matcher.get_file_info(path)

        assert info1 == info2
        assert id(info1) == id(info2)  # Same object from cache


class TestPluginRouter:
    """Test PluginRouter functionality."""

    def test_register_plugin(self, plugin_router, python_plugin):
        """Test plugin registration."""
        # Plugin should already be registered from fixture
        assert python_plugin in plugin_router._plugins
        assert "python" in plugin_router._language_plugins
        assert python_plugin in plugin_router._language_plugins["python"]

    def test_unregister_plugin(self, plugin_router, python_plugin):
        """Test plugin unregistration."""
        plugin_router.unregister_plugin(python_plugin)

        assert python_plugin not in plugin_router._plugins
        assert python_plugin not in plugin_router._language_plugins.get("python", [])

    def test_route_file_python(self, plugin_router, python_plugin):
        """Test routing Python files."""
        path = Path("/test/example.py")
        results = plugin_router.route_file(path)

        assert len(results) > 0
        assert results[0].plugin == python_plugin
        assert results[0].confidence > 0
        assert "Language match: python" in results[0].match_reasons

    def test_route_file_javascript(self, plugin_router, javascript_plugin):
        """Test routing JavaScript files."""
        path = Path("/test/app.js")
        results = plugin_router.route_file(path)

        assert len(results) > 0
        assert results[0].plugin == javascript_plugin
        assert results[0].confidence > 0

    def test_route_file_binary_skipped(self, plugin_router):
        """Test that binary files are skipped."""
        path = Path("/test/image.png")
        results = plugin_router.route_file(path)

        assert len(results) == 0

    def test_route_by_language(self, plugin_router, python_plugin):
        """Test routing by language."""
        results = plugin_router.route_by_language("python")

        assert len(results) > 0
        assert results[0].plugin == python_plugin
        assert results[0].confidence >= 0.9  # High confidence for exact match

    def test_route_by_capability(self, plugin_router, python_plugin, javascript_plugin):
        """Test routing by capability."""
        results = plugin_router.route_by_capability("syntax_analysis")

        assert len(results) >= 2  # Both plugins have this capability

        # Should be sorted by priority/confidence
        plugin_langs = [getattr(r.plugin, "lang", None) for r in results]
        assert "python" in plugin_langs
        assert "javascript" in plugin_langs

    def test_get_best_plugin(self, plugin_router, python_plugin):
        """Test getting best plugin for a file."""
        path = Path("/test/example.py")
        result = plugin_router.get_best_plugin(path)

        assert result is not None
        assert result.plugin == python_plugin

        # Should update usage tracking
        assert plugin_router._plugin_usage_count[python_plugin] > 0

    def test_load_balancing(self, plugin_router, python_plugin):
        """Test load balancing behavior."""
        path = Path("/test/example.py")

        # Use plugin multiple times to trigger load balancing
        for _ in range(15):
            plugin_router.get_best_plugin(path)

        # Usage count should be tracked
        assert plugin_router._plugin_usage_count[python_plugin] >= 15

    def test_performance_tracking(self, plugin_router, python_plugin):
        """Test performance tracking."""
        # Record some performance data
        plugin_router.record_performance(python_plugin, 0.1)
        plugin_router.record_performance(python_plugin, 0.2)
        plugin_router.record_performance(python_plugin, 0.15)

        avg_perf = plugin_router._get_avg_performance(python_plugin)
        assert 0.14 <= avg_perf <= 0.16  # Should be around 0.15

    def test_get_plugin_stats(self, plugin_router):
        """Test getting plugin statistics."""
        stats = plugin_router.get_plugin_stats()

        assert "total_plugins" in stats
        assert "language_coverage" in stats
        assert "capability_coverage" in stats
        assert stats["total_plugins"] >= 2


class TestResultAggregator:
    """Test ResultAggregator functionality."""

    def test_aggregate_search_results_basic(
        self, result_aggregator, python_plugin, javascript_plugin, sample_search_results
    ):
        """Test basic search result aggregation."""
        results_by_plugin = {
            python_plugin: sample_search_results["python_results"],
            javascript_plugin: sample_search_results["js_results"],
        }

        aggregated, stats = result_aggregator.aggregate_search_results(results_by_plugin)

        assert len(aggregated) > 0
        assert stats.total_results == 4  # 2 + 2 results
        assert stats.plugins_used == 2
        assert stats.duplicates_removed >= 0
        assert stats.execution_time >= 0

    def test_aggregate_search_results_with_limit(
        self, result_aggregator, python_plugin, sample_search_results
    ):
        """Test search result aggregation with limit."""
        results_by_plugin = {python_plugin: sample_search_results["python_results"]}

        aggregated, stats = result_aggregator.aggregate_search_results(results_by_plugin, limit=1)

        assert len(aggregated) == 1
        assert stats.unique_results == 1

    def test_aggregate_symbol_definitions(
        self, result_aggregator, python_plugin, javascript_plugin
    ):
        """Test symbol definition aggregation."""
        python_def = {
            "symbol": "hello",
            "kind": "function",
            "language": "python",
            "signature": "def hello()",
            "doc": "A hello function",
            "defined_in": "/test/file.py",
            "line": 1,
            "span": (0, 10),
        }

        js_def = {
            "symbol": "hello",
            "kind": "function",
            "language": "javascript",
            "signature": "function hello()",
            "doc": None,  # Less complete
            "defined_in": "/test/file.js",
            "line": 1,
            "span": (0, 15),
        }

        definitions_by_plugin = {python_plugin: python_def, javascript_plugin: js_def}

        best_def = result_aggregator.aggregate_symbol_definitions(definitions_by_plugin)

        assert best_def == python_def  # Should prefer more complete definition

    def test_aggregate_references(self, result_aggregator, python_plugin, javascript_plugin):
        """Test reference aggregation."""
        python_refs = [
            Reference(file="/test/file1.py", line=5),
            Reference(file="/test/file2.py", line=10),
        ]

        js_refs = [
            Reference(file="/test/file1.py", line=5),  # Duplicate
            Reference(file="/test/app.js", line=3),
        ]

        references_by_plugin = {python_plugin: python_refs, javascript_plugin: js_refs}

        aggregated_refs = result_aggregator.aggregate_references(references_by_plugin)

        # Should deduplicate and have 3 unique references
        assert len(aggregated_refs) == 3

        # Should be sorted by file and line
        assert aggregated_refs[0].file <= aggregated_refs[1].file

    def test_caching_behavior(self, result_aggregator, python_plugin, sample_search_results):
        """Test result caching."""
        results_by_plugin = {python_plugin: sample_search_results["python_results"]}

        # First call should miss cache
        aggregated1, stats1 = result_aggregator.aggregate_search_results(results_by_plugin)
        assert stats1.cache_misses > 0

        # Second call should hit cache
        aggregated2, stats2 = result_aggregator.aggregate_search_results(results_by_plugin)
        assert stats2.cache_hits > 0

        # Results should be the same
        assert len(aggregated1) == len(aggregated2)

    def test_aggregation_stats(self, result_aggregator):
        """Test aggregation statistics."""
        stats = result_aggregator.get_aggregation_stats()

        assert "total_aggregations" in stats
        assert "cache_hits" in stats
        assert "cache_misses" in stats
        assert "cache_hit_rate" in stats
        assert "strategy" in stats

    def test_clear_cache(self, result_aggregator, python_plugin, sample_search_results):
        """Test cache clearing."""
        results_by_plugin = {python_plugin: sample_search_results["python_results"]}

        # Populate cache
        result_aggregator.aggregate_search_results(results_by_plugin)
        assert len(result_aggregator._result_cache) > 0

        # Clear cache
        result_aggregator.clear_cache()
        assert len(result_aggregator._result_cache) == 0

    def test_configuration(self, result_aggregator):
        """Test aggregator configuration."""
        new_criteria = RankingCriteria(relevance_weight=0.5, confidence_weight=0.5)
        new_strategy = SimpleAggregationStrategy()

        result_aggregator.configure(
            strategy=new_strategy, ranking_criteria=new_criteria, cache_enabled=False
        )

        assert result_aggregator.strategy == new_strategy
        assert result_aggregator.ranking_criteria == new_criteria
        assert not result_aggregator.cache_enabled


class TestAggregationStrategies:
    """Test different aggregation strategies."""

    def test_simple_aggregation_strategy(self, python_plugin, sample_search_results):
        """Test simple aggregation strategy."""
        strategy = SimpleAggregationStrategy()
        criteria = RankingCriteria()

        results_by_plugin = {python_plugin: sample_search_results["python_results"]}

        aggregated = strategy.aggregate(results_by_plugin, criteria)

        assert len(aggregated) == 2  # Two distinct results

        # Should be sorted by rank score
        assert aggregated[0].rank_score >= aggregated[1].rank_score

    def test_smart_aggregation_strategy(
        self, python_plugin, javascript_plugin, sample_search_results
    ):
        """Test smart aggregation strategy."""
        strategy = SmartAggregationStrategy(similarity_threshold=0.8)
        criteria = RankingCriteria()

        results_by_plugin = {
            python_plugin: sample_search_results["python_results"],
            javascript_plugin: sample_search_results["js_results"],
        }

        aggregated = strategy.aggregate(results_by_plugin, criteria)

        assert len(aggregated) > 0

        # Check for similarity grouping
        for result in aggregated:
            assert result.confidence > 0
            assert result.rank_score > 0

    def test_smart_strategy_similarity_detection(self):
        """Test similarity detection in smart strategy."""
        strategy = SmartAggregationStrategy(similarity_threshold=0.8)

        result1 = {"file": "/test/file.py", "line": 1, "snippet": "def hello_world():"}

        result2 = {
            "file": "/test/file.py",
            "line": 2,  # Close line number
            "snippet": "def hello_world():",  # Same snippet
        }

        result3 = {
            "file": "/test/other.py",
            "line": 100,
            "snippet": "completely different content",
        }

        # Should detect similarity between result1 and result2
        assert strategy._are_results_similar(result1, result2)

        # Should not detect similarity with result3
        assert not strategy._are_results_similar(result1, result3)


class TestIntegration:
    """Integration tests for advanced dispatcher components."""

    def test_router_aggregator_integration(self, plugin_router, python_plugin, javascript_plugin):
        """Test integration between router and aggregator."""
        # Route a Python file
        path = Path("/test/example.py")
        route_results = plugin_router.route_file(path)

        assert len(route_results) > 0

        # Create aggregator and test with search results
        aggregator = ResultAggregator()

        # Simulate search results from routed plugins
        search_results = {}
        for route_result in route_results[:2]:  # Use top 2 plugins
            plugin = route_result.plugin
            # Simulate plugin search
            mock_results = [
                {
                    "file": str(path),
                    "line": 1,
                    "snippet": f"Result from {plugin.lang} plugin",
                    "score": route_result.confidence,
                }
            ]
            search_results[plugin] = mock_results

        aggregated, stats = aggregator.aggregate_search_results(search_results)

        assert len(aggregated) > 0
        assert stats.plugins_used > 0

    def test_end_to_end_search_flow(self, plugin_router, python_plugin, javascript_plugin):
        """Test end-to-end search flow with routing and aggregation."""
        # Index some content
        python_plugin.indexFile(
            "/test/example.py", 'def hello_world():\n    print("Hello, World!")'
        )
        javascript_plugin.indexFile(
            "/test/app.js", 'function hello() {\n    console.log("Hello!");\n}'
        )

        # Route search query
        query = "hello"

        # Get plugins that can handle search
        search_plugins = []
        for plugin in plugin_router._plugins:
            capabilities = plugin_router._plugin_capabilities.get(plugin, [])
            if (
                any(cap.name == "semantic_search" for cap in capabilities) or True
            ):  # All plugins can search
                search_plugins.append(plugin)

        # Perform search across plugins
        search_results = {}
        for plugin in search_plugins:
            results = list(plugin.search(query))
            if results:
                search_results[plugin] = results

        # Aggregate results
        aggregator = ResultAggregator()
        aggregated, stats = aggregator.aggregate_search_results(search_results)

        assert len(aggregated) > 0
        assert stats.total_results > 0
        assert all(
            query.lower() in result.primary_result["snippet"].lower() for result in aggregated
        )

    def test_performance_tracking_integration(self, plugin_router, python_plugin):
        """Test performance tracking integration."""
        path = Path("/test/example.py")

        # Simulate multiple operations with timing
        start_time = time.time()

        # Route file multiple times
        for _ in range(5):
            result = plugin_router.get_best_plugin(path)
            assert result is not None

            # Record performance
            execution_time = time.time() - start_time
            plugin_router.record_performance(result.plugin, execution_time)
            start_time = time.time()

        # Check that performance was tracked
        avg_perf = plugin_router._get_avg_performance(python_plugin)
        assert avg_perf >= 0

        # Check stats
        stats = plugin_router.get_plugin_stats()
        assert python_plugin in stats["plugin_performance"]
        assert stats["plugin_performance"][python_plugin] >= 0


class TestRemoveFileSemanticCleanup:
    """Verify EnhancedDispatcher.remove_file() cleans up the Qdrant semantic index."""

    def _make_dispatcher(self, tmp_path, semantic_indexer=None):
        """Build a minimal EnhancedDispatcher without calling __init__."""
        import threading

        from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher

        d = EnhancedDispatcher.__new__(EnhancedDispatcher)
        d._file_cache = {}
        d._file_cache_lock = threading.Lock()
        d._operation_stats = {}
        d._semantic_indexer = semantic_indexer
        d._plugins = []
        # Attributes added by post-P15 __init__ that remove_file() reads.
        d._legacy_plugins = []
        d._semantic_registry = None
        d._semantic_indexer_fallback = semantic_indexer
        d._semantic_enabled = semantic_indexer is not None
        d._lazy_load = False
        d._use_factory = False
        d._enable_advanced = False
        d._router = None
        return d

    def _make_ctx(self, workspace_root):
        from unittest.mock import MagicMock

        from mcp_server.core.repo_context import RepoContext
        from mcp_server.storage.multi_repo_manager import RepositoryInfo

        registry_entry = MagicMock(spec=RepositoryInfo)
        registry_entry.tracked_branch = "main"
        return RepoContext(
            repo_id="test-repo",
            sqlite_store=MagicMock(),
            workspace_root=Path(workspace_root),
            tracked_branch="main",
            registry_entry=registry_entry,
        )

    def test_remove_file_calls_semantic_indexer(self, tmp_path):
        """remove_file() must call semantic_indexer.remove_file() when one is present."""
        from unittest.mock import MagicMock

        mock_semantic = MagicMock()
        dispatcher = self._make_dispatcher(tmp_path, semantic_indexer=mock_semantic)
        ctx = self._make_ctx(tmp_path)

        target = tmp_path / "foo.py"
        target.write_text("x = 1")
        dispatcher.remove_file(ctx, target)

        mock_semantic.cleanup_stale_semantic_artifacts.assert_called_once()

    def test_remove_file_no_semantic_indexer_does_not_raise(self, tmp_path):
        """remove_file() must not raise when _semantic_indexer is None."""
        dispatcher = self._make_dispatcher(tmp_path, semantic_indexer=None)
        ctx = self._make_ctx(tmp_path)

        target = tmp_path / "bar.py"
        target.write_text("y = 2")
        dispatcher.remove_file(ctx, target)  # must not raise

    def test_remove_file_semantic_indexer_exception_is_swallowed(self, tmp_path):
        """remove_file() must not propagate exceptions from the semantic indexer."""
        from unittest.mock import MagicMock

        mock_semantic = MagicMock()
        mock_semantic.cleanup_stale_semantic_artifacts.side_effect = RuntimeError(
            "qdrant unavailable"
        )
        dispatcher = self._make_dispatcher(tmp_path, semantic_indexer=mock_semantic)
        ctx = self._make_ctx(tmp_path)

        target = tmp_path / "baz.py"
        target.write_text("z = 3")
        dispatcher.remove_file(ctx, target)  # must not raise despite indexer error

        mock_semantic.cleanup_stale_semantic_artifacts.assert_called_once()
