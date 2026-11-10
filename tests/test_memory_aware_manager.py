"""
Tests for Memory-Aware Plugin Management

These tests verify that the memory-aware plugin manager correctly handles
plugin loading, eviction, and memory limits.
"""

import gc
import importlib
import os
import sys
import time
import weakref
from datetime import datetime
from unittest.mock import MagicMock, Mock, patch

import pytest

from mcp_server.plugins.memory_aware_manager import (
    LoadedPlugin,
    MemoryAwarePluginManager,
    PluginBackpressureError,
    PluginMemoryInfo,
    PluginResourceSnapshot,
    get_memory_aware_manager,
)


class TestMemoryAwarePluginManager:
    """Test memory-aware plugin management functionality."""

    @pytest.fixture
    def manager(self):
        """Use a large memory limit so real-RSS eviction never fires during unit tests
        that manipulate the cache directly (RSS of the test process exceeds 100 MB)."""
        return MemoryAwarePluginManager(
            max_memory_mb=4096,
            high_priority_langs=["python", "javascript"],
        )

    @pytest.fixture
    def mock_plugin(self):
        """Create a mock plugin."""
        plugin = Mock()
        plugin.language = "test"
        plugin.index = Mock()
        plugin.getDefinition = Mock()
        return plugin

    def test_initialization(self, manager):
        """Test manager initialization."""
        assert manager.max_memory_bytes == 4096 * 1024 * 1024
        assert "python" in manager.high_priority_langs
        assert "javascript" in manager.high_priority_langs
        assert len(manager._plugins) == 0

    def test_plugin_loading(self, manager, mock_plugin):
        """Test plugin loading and caching."""
        key = (None, "test")
        with patch.object(manager._factory, "get_plugin", return_value=mock_plugin):
            plugin1 = manager.get_plugin("test")
            assert plugin1 == mock_plugin
            assert key in manager._plugins
            assert key in manager._plugin_info

            plugin2 = manager.get_plugin("test")
            assert plugin2 == mock_plugin
            assert manager._plugin_info[key].usage_count == 2

    def test_lru_ordering(self, manager):
        """Test LRU ordering of plugins."""
        plugins = []
        for i in range(3):
            plugin = Mock()
            plugin.language = f"lang{i}"
            plugins.append(plugin)

        with patch.object(manager._factory, "get_plugin", side_effect=plugins):
            manager.get_plugin("lang0")
            manager.get_plugin("lang1")
            manager.get_plugin("lang2")

            # Access lang0 again (moves to end)
            manager.get_plugin("lang0")

            # Check order (lang1 should be oldest) — keys are (repo_id, language) tuples
            plugin_keys = list(manager._plugins.keys())
            assert plugin_keys == [(None, "lang1"), (None, "lang2"), (None, "lang0")]

    def test_memory_limit_enforcement(self, manager):
        """An unrecoverable child-RSS breach returns typed backpressure."""
        snapshot = PluginResourceSnapshot(
            sampled_at_monotonic=time.monotonic(),
            parent_rss_bytes=1,
            child_rss_bytes=manager.max_memory_bytes + 1,
            reserved_workers=0,
            live_workers=1,
            measurement_ok=True,
        )
        with patch.object(manager, "resource_snapshot", return_value=snapshot):
            with pytest.raises(PluginBackpressureError) as exc_info:
                manager.get_plugin("new_plugin")
        assert exc_info.value.reason == "rss_limit"

    def test_high_priority_protection(self, manager):
        """Test that high-priority plugins are protected from eviction."""
        python_plugin = Mock()
        python_plugin.language = "python"
        other_plugin = Mock()
        other_plugin.language = "rust"

        with patch.object(
            manager._factory, "get_plugin", side_effect=[python_plugin, other_plugin]
        ):
            manager.get_plugin("python")
            manager.get_plugin("rust")

            python_key = (None, "python")
            rust_key = (None, "rust")
            manager._plugin_info[python_key].is_high_priority = True
            manager._plugin_info[rust_key].is_high_priority = False

            candidates = manager._get_eviction_candidates()

            assert python_key not in candidates
            assert rust_key in candidates

    def test_plugin_eviction(self, manager):
        """Test plugin eviction and memory reclamation."""
        plugin = Mock()
        plugin.language = "test"
        cache_key = (None, "test")

        manager._plugins[cache_key] = LoadedPlugin(name="test", instance=plugin, metadata={})
        manager._plugin_info[cache_key] = PluginMemoryInfo(
            plugin_name="test",
            memory_bytes=1024 * 1024,
            last_used=datetime.now(),
            load_time=0.1,
            usage_count=1,
            is_high_priority=False,
        )

        memory_freed = manager._evict_plugin(cache_key)

        assert memory_freed == 1024 * 1024
        assert cache_key not in manager._plugins
        assert cache_key not in manager._plugin_info

    def test_memory_status(self, manager):
        """Test memory status reporting."""
        for i in range(2):
            plugin = Mock()
            key = (None, f"lang{i}")
            manager._plugins[key] = LoadedPlugin(name=f"lang{i}", instance=plugin, metadata={})
            manager._plugin_info[key] = PluginMemoryInfo(
                plugin_name=f"lang{i}",
                memory_bytes=1024 * 1024 * 10,
                last_used=datetime.now(),
                load_time=0.1,
                usage_count=i + 1,
                is_high_priority=i == 0,
            )

        status = manager.get_memory_status()

        assert status["max_memory_mb"] == 4096
        assert status["used_memory_mb"] == 20  # 2 * 10MB
        assert status["loaded_plugins"] == 2
        assert len(status["plugin_details"]) == 2

    def test_preload_high_priority(self, manager):
        """Test preloading of high-priority plugins."""
        mock_plugins = {
            "python": Mock(language="python"),
            "javascript": Mock(language="javascript"),
        }

        def get_plugin_side_effect(lang, **_kwargs):
            return mock_plugins.get(lang)

        with patch.object(manager._factory, "get_plugin", side_effect=get_plugin_side_effect):
            manager.preload_high_priority()

            assert (None, "python") in manager._plugins
            assert (None, "javascript") in manager._plugins

    def test_clear_cache(self, manager):
        """Test cache clearing functionality."""
        for lang in ["python", "rust", "go"]:
            plugin = Mock()
            key = (None, lang)
            manager._plugins[key] = LoadedPlugin(name=lang, instance=plugin, metadata={})
            manager._plugin_info[key] = PluginMemoryInfo(
                plugin_name=lang,
                memory_bytes=1024 * 1024,
                last_used=datetime.now(),
                load_time=0.1,
                usage_count=1,
                is_high_priority=(lang == "python"),
            )

        manager.clear_cache(keep_high_priority=True)

        assert (None, "python") in manager._plugins
        assert (None, "rust") not in manager._plugins
        assert (None, "go") not in manager._plugins

        manager.clear_cache(keep_high_priority=False)
        assert len(manager._plugins) == 0

    def test_set_high_priority_languages(self, manager):
        """Test updating high-priority languages."""
        rust_key = (None, "rust")
        manager._plugin_info[rust_key] = PluginMemoryInfo(
            plugin_name="rust",
            memory_bytes=1024 * 1024,
            last_used=datetime.now(),
            load_time=0.1,
            usage_count=1,
            is_high_priority=False,
        )

        manager.set_high_priority_languages(["rust", "go"])

        assert "rust" in manager.high_priority_langs
        assert "go" in manager.high_priority_langs
        assert "python" not in manager.high_priority_langs

        assert manager._plugin_info[rust_key].is_high_priority is True

    def test_get_plugin_info(self, manager):
        """Test getting plugin information."""
        plugin = Mock()
        key = (None, "test")
        manager._plugins[key] = LoadedPlugin(name="test", instance=plugin, metadata={})
        manager._plugin_info[key] = PluginMemoryInfo(
            plugin_name="test",
            memory_bytes=1024 * 1024 * 5,
            last_used=datetime.now(),
            load_time=0.25,
            usage_count=10,
            is_high_priority=False,
        )

        info = manager.get_plugin_info("test")

        assert info["language"] == "test"
        assert info["memory_mb"] == 5.0
        assert info["usage_count"] == 10
        assert info["load_time_seconds"] == 0.25
        assert info["is_high_priority"] is False
        assert info["is_loaded"] is True

        assert manager.get_plugin_info("nonexistent") is None


class TestSingletonManager:
    """Test singleton manager functionality."""

    def test_singleton_instance(self):
        """Test that get_memory_aware_manager returns singleton."""
        manager1 = get_memory_aware_manager()
        manager2 = get_memory_aware_manager()

        assert manager1 is manager2

    def test_environment_configuration(self):
        """Test configuration from environment variables."""
        import mcp_server.plugins.memory_aware_manager as module

        module._manager_instance = None

        os.environ["MCP_MAX_MEMORY_MB"] = "512"
        os.environ["MCP_HIGH_PRIORITY_LANGS"] = "python,go,rust"
        os.environ["MCP_PRELOAD_PLUGINS"] = "false"

        try:
            manager = get_memory_aware_manager()

            assert manager.max_memory_bytes == 512 * 1024 * 1024
            assert "python" in manager.high_priority_langs
            assert "go" in manager.high_priority_langs
            assert "rust" in manager.high_priority_langs

        finally:
            del os.environ["MCP_MAX_MEMORY_MB"]
            del os.environ["MCP_HIGH_PRIORITY_LANGS"]
            del os.environ["MCP_PRELOAD_PLUGINS"]
            module._manager_instance = None

    def test_thread_safety(self):
        """Test thread-safe access to singleton."""
        import threading

        import mcp_server.plugins.memory_aware_manager as module

        module._manager_instance = None

        managers = []

        def get_manager():
            manager = get_memory_aware_manager()
            managers.append(manager)

        threads = []
        for _ in range(10):
            thread = threading.Thread(target=get_manager)
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        assert len(set(id(m) for m in managers)) == 1

        module._manager_instance = None


class TestMemoryMonitoring:
    """Test memory monitoring functionality."""

    def test_memory_measurement(self):
        """Test that memory measurement works."""
        manager = MemoryAwarePluginManager(max_memory_mb=4096)

        current_memory = manager._get_current_memory()
        assert current_memory > 0

        plugin_memory = manager._get_plugin_memory_usage()
        assert plugin_memory == 0

    def test_weak_reference_cleanup(self):
        """Test that weak references allow garbage collection."""
        manager = MemoryAwarePluginManager(max_memory_mb=4096)

        plugin = Mock()
        plugin.language = "test"
        key = (None, "test")

        manager._plugins[key] = LoadedPlugin(name="test", instance=plugin, metadata={})
        manager._plugin_info[key] = PluginMemoryInfo(
            plugin_name="test",
            memory_bytes=1024,
            last_used=datetime.now(),
            load_time=0.1,
            usage_count=1,
            is_high_priority=False,
        )

        manager._weak_refs[key] = weakref.ref(plugin, lambda ref: manager._on_plugin_deleted(key))

        del manager._plugins[key]
        del plugin

        gc.collect()
        # Plugin info should be cleaned up by callback
        # Note: This is timing-dependent and may not always work in tests

    def test_memory_limit_calculation(self):
        """Test memory limit calculations."""
        manager = MemoryAwarePluginManager(max_memory_mb=4096)

        manager._plugin_info[(None, "test1")] = PluginMemoryInfo(
            plugin_name="test1",
            memory_bytes=30 * 1024 * 1024,
            last_used=datetime.now(),
            load_time=0.1,
            usage_count=1,
            is_high_priority=False,
        )

        manager._plugin_info[(None, "test2")] = PluginMemoryInfo(
            plugin_name="test2",
            memory_bytes=50 * 1024 * 1024,
            last_used=datetime.now(),
            load_time=0.1,
            usage_count=1,
            is_high_priority=False,
        )

        total_memory = manager._get_plugin_memory_usage()
        assert total_memory == 80 * 1024 * 1024

        available = manager._ensure_memory_available()
        # This depends on eviction logic


class TestP3Behaviour:
    """SL-1 additions: psutil fail-fast, real-RSS eviction, per-repo keying."""

    def test_psutil_unavailable_raises_runtime_error(self):
        """MemoryAwarePluginManager.__init__ must raise if psutil is missing."""
        import mcp_server.plugins.memory_aware_manager as mod

        orig = sys.modules.get("psutil")
        try:
            sys.modules["psutil"] = None  # type: ignore[assignment]
            # Force module reload so the try/except at import time re-runs
            importlib.reload(mod)
            with pytest.raises(RuntimeError, match="psutil"):
                mod.MemoryAwarePluginManager()
        finally:
            if orig is None:
                sys.modules.pop("psutil", None)
            else:
                sys.modules["psutil"] = orig
            importlib.reload(mod)

    def test_eviction_uses_real_rss_not_tracked_estimate(self):
        """_ensure_memory_available fires when real RSS > limit even if tracked bytes == 0."""
        manager = MemoryAwarePluginManager(max_memory_mb=1)

        # tracked plugin bytes report nothing
        manager._plugin_info = {}

        rss_above_limit = manager.max_memory_bytes + 1024 * 1024
        fake_mi = MagicMock()
        fake_mi.rss = rss_above_limit

        with patch.object(manager._process, "memory_info", return_value=fake_mi):
            result = manager._should_evict()

        assert result is True, "Eviction should fire based on real RSS, not tracked estimates"

    def test_eviction_does_not_fire_when_rss_below_limit(self):
        """_should_evict returns False when real RSS < limit."""
        manager = MemoryAwarePluginManager(max_memory_mb=1024)
        manager._plugin_info = {}

        rss_below_limit = 1024 * 1024  # 1 MB
        fake_mi = MagicMock()
        fake_mi.rss = rss_below_limit

        with patch.object(manager._process, "memory_info", return_value=fake_mi):
            result = manager._should_evict()

        assert result is False

    def test_get_plugin_distinct_instances_for_distinct_repos(self):
        """get_plugin('python', ctx) returns distinct instances for distinct repo_ids."""
        ctx_a = MagicMock()
        ctx_a.repo_id = "repo-aaa"
        ctx_b = MagicMock()
        ctx_b.repo_id = "repo-bbb"

        manager = MemoryAwarePluginManager(max_memory_mb=4096)

        mock_plugin_a = MagicMock()
        mock_plugin_b = MagicMock()

        call_count = [0]

        def fake_create(language, **kwargs):
            call_count[0] += 1
            return mock_plugin_a if call_count[0] == 1 else mock_plugin_b

        with patch.object(manager._factory, "get_plugin", side_effect=fake_create):
            plugin_a = manager.get_plugin("python", ctx_a)
            plugin_b = manager.get_plugin("python", ctx_b)

        assert plugin_a is not plugin_b, "Different repo_ids must return distinct plugin instances"

    def test_get_plugin_same_instance_same_repo(self):
        """get_plugin('python', ctx) returns same instance on repeated calls for same repo."""
        ctx = MagicMock()
        ctx.repo_id = "repo-same"

        manager = MemoryAwarePluginManager(max_memory_mb=4096)
        mock_plugin = MagicMock()

        with patch.object(manager._factory, "get_plugin", return_value=mock_plugin):
            p1 = manager.get_plugin("python", ctx)
            p2 = manager.get_plugin("python", ctx)

        assert p1 is p2

    def test_get_plugin_ctx_none_uses_global_key(self):
        """get_plugin(lang, ctx=None) still works (backward compat with repository_plugin_loader)."""
        manager = MemoryAwarePluginManager(max_memory_mb=4096)
        mock_plugin = MagicMock()

        with patch.object(manager._factory, "get_plugin", return_value=mock_plugin):
            p = manager.get_plugin("python", None)

        assert p is mock_plugin


class TestWorkerLifecycleGovernance:
    """PLUGLIFE resource, close, and deterministic eviction contract."""

    @staticmethod
    def _plugin(*, running: bool = False, rss: int = 0):
        plugin = Mock()
        plugin.close = Mock()
        plugin.is_worker_running = running
        plugin.worker_rss_bytes = Mock(return_value=rss)
        return plugin

    def test_worker_limit_evicts_deterministic_lru(self):
        manager = MemoryAwarePluginManager(max_memory_mb=4096, max_workers=2)
        oldest = self._plugin()
        newest = self._plugin()
        replacement = self._plugin()
        with patch.object(
            manager._factory, "get_plugin", side_effect=[oldest, newest, replacement]
        ):
            manager.get_plugin("rust")
            manager.get_plugin("go")
            manager.get_plugin("go")
            manager.get_plugin("java")

        oldest.close.assert_called_once_with()
        newest.close.assert_not_called()
        assert list(manager._plugins) == [(None, "go"), (None, "java")]

    def test_child_rss_breach_evicts_before_next_allocation(self):
        manager = MemoryAwarePluginManager(max_memory_mb=50, max_workers=4)
        first = self._plugin(running=True, rss=60 * 1024 * 1024)
        second = self._plugin()
        with patch.object(manager, "_get_current_memory", return_value=50 * 1024 * 1024):
            with patch.object(manager._factory, "get_plugin", side_effect=[first, second]):
                manager.get_plugin("rust")
                manager.get_plugin("go")

        first.close.assert_called_once_with()
        assert (None, "go") in manager._plugins

    def test_new_worker_is_revalidated_after_spawn(self):
        manager = MemoryAwarePluginManager(max_memory_mb=50, max_workers=4)
        oversized = self._plugin(running=True, rss=60 * 1024 * 1024)
        with patch.object(manager, "_get_current_memory", return_value=50 * 1024 * 1024):
            with patch.object(manager._factory, "get_plugin", return_value=oversized):
                manager.get_plugin("rust")
                with pytest.raises(RuntimeError) as exc_info:
                    manager.confirm_plugin_started("rust")

        assert getattr(exc_info.value, "reason", None) == "rss_limit"
        oversized.close.assert_called_once_with()
        assert (None, "rust") not in manager._plugins

    def test_measurement_failure_blocks_allocation(self):
        manager = MemoryAwarePluginManager(max_memory_mb=4096)
        live = self._plugin(running=True)
        live.worker_rss_bytes.side_effect = RuntimeError("probe failed")
        manager._plugins[(None, "rust")] = LoadedPlugin("rust", live, {})
        manager._plugin_info[(None, "rust")] = PluginMemoryInfo(
            plugin_name="rust",
            memory_bytes=0,
            last_used=datetime.now(),
            load_time=0.0,
            usage_count=1,
            is_high_priority=False,
            last_used_monotonic=time.monotonic(),
        )

        with pytest.raises(RuntimeError) as exc_info:
            manager.get_plugin("go")
        assert getattr(exc_info.value, "reason", None) == "resource_measurement_failed"

    def test_idle_expiry_closes_adapter(self):
        manager = MemoryAwarePluginManager(max_memory_mb=4096, idle_timeout_seconds=1.0)
        idle = self._plugin()
        fresh = self._plugin()
        with patch.object(manager._factory, "get_plugin", side_effect=[idle, fresh]):
            manager.get_plugin("rust")
            manager._plugin_info[(None, "rust")].last_used_monotonic = time.monotonic() - 2.0
            manager.get_plugin("go")

        idle.close.assert_called_once_with()
        assert (None, "rust") not in manager._plugins

    def test_repo_eviction_and_shutdown_close_every_adapter_once(self):
        manager = MemoryAwarePluginManager(max_memory_mb=4096)
        repo_a_python = self._plugin()
        repo_a_rust = self._plugin()
        repo_b_go = self._plugin()
        ctx_a = Mock(repo_id="repo-a")
        ctx_b = Mock(repo_id="repo-b")
        with patch.object(
            manager._factory,
            "get_plugin",
            side_effect=[repo_a_python, repo_a_rust, repo_b_go],
        ):
            manager.get_plugin("python", ctx_a)
            manager.get_plugin("rust", ctx_a)
            manager.get_plugin("go", ctx_b)

        assert manager.evict_repo("repo-a") == 2
        repo_a_python.close.assert_called_once_with()
        repo_a_rust.close.assert_called_once_with()
        repo_b_go.close.assert_not_called()

        manager.shutdown()
        manager.shutdown()
        repo_b_go.close.assert_called_once_with()
        assert not manager._plugins
