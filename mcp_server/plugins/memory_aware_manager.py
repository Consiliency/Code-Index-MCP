"""
Memory-Aware Plugin Management System

This module implements intelligent memory management for language plugins,
including LRU caching, configurable memory limits, and transparent reloading.
"""

import gc
import logging
import os
import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from mcp_server.core.repo_context import RepoContext

try:
    import psutil
except ImportError:
    psutil = None

# from mcp_server.plugin_system.models import LoadedPlugin
from mcp_server.plugins.plugin_factory import PluginFactory, PluginUnavailableError

logger = logging.getLogger(__name__)


@dataclass
class LoadedPlugin:
    """Represents a loaded plugin instance."""

    name: str
    instance: Any
    metadata: Dict[str, Any]


@dataclass
class PluginMemoryInfo:
    """Memory usage information for a plugin."""

    plugin_name: str
    memory_bytes: int
    last_used: datetime
    load_time: float
    usage_count: int
    is_high_priority: bool
    last_used_monotonic: float = 0.0


@dataclass(frozen=True)
class PluginResourceSnapshot:
    """Immutable process and worker resource sample."""

    sampled_at_monotonic: float
    parent_rss_bytes: int
    child_rss_bytes: int
    reserved_workers: int
    live_workers: int
    measurement_ok: bool
    failure_reason: Optional[str] = None


class PluginBackpressureError(RuntimeError):
    """Raised when a plugin allocation would exceed a hard resource limit."""

    def __init__(self, reason: str, snapshot: PluginResourceSnapshot):
        self.reason = reason
        self.snapshot = snapshot
        super().__init__(f"plugin allocation blocked: {reason}")


class MemoryAwarePluginManager:
    """
    Manages plugins with memory awareness using LRU caching.

    Features:
    - Configurable memory limits (default 1GB)
    - LRU eviction based on real process RSS (via psutil)
    - High-priority plugin protection
    - Transparent plugin reloading
    - Per-repo plugin isolation (cache keyed by (repo_id, language))
    """

    def __init__(
        self,
        max_memory_mb: int = 1024,
        high_priority_langs: Optional[List[str]] = None,
        *,
        max_workers: int = 16,
        idle_timeout_seconds: float = 900.0,
        sample_interval_seconds: float = 1.0,
    ):
        """
        Initialize the memory-aware plugin manager.

        Args:
            max_memory_mb: Maximum memory limit in MB (default 1024)
            high_priority_langs: Languages to keep in memory (e.g., ['python', 'javascript'])

        Raises:
            RuntimeError: If psutil is not installed.
        """
        if psutil is None:
            raise RuntimeError(
                "MemoryAwarePluginManager requires psutil; install with: uv add psutil"
            )

        self.max_memory_bytes = max_memory_mb * 1024 * 1024
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if idle_timeout_seconds < 0:
            raise ValueError("idle_timeout_seconds must be non-negative")
        if sample_interval_seconds < 0:
            raise ValueError("sample_interval_seconds must be non-negative")
        self.max_workers = max_workers
        self.idle_timeout_seconds = idle_timeout_seconds
        self.sample_interval_seconds = sample_interval_seconds
        self.high_priority_langs = set(
            high_priority_langs or ["python", "javascript", "typescript"]
        )

        # Plugin storage keyed by (repo_id, language) for per-repo isolation.
        # repo_id=None is the legacy/global key used when no ctx is provided.
        self._plugins: OrderedDict[Tuple[Optional[str], str], LoadedPlugin] = OrderedDict()
        self._plugin_info: Dict[Tuple[Optional[str], str], PluginMemoryInfo] = {}

        # Weak references for garbage collection tracking
        self._weak_refs: Dict[Tuple[Optional[str], str], weakref.ReferenceType[Any]] = {}

        # Thread safety
        self._lock = threading.RLock()

        # Plugin factory for loading
        self._factory = PluginFactory()

        # Memory monitoring
        self._process = psutil.Process()
        self._base_memory = self._get_current_memory()
        self._last_snapshot: Optional[PluginResourceSnapshot] = None

        logger.info(f"Memory-aware plugin manager initialized with {max_memory_mb}MB limit")

    def get_plugin(self, language: str, ctx: Optional["RepoContext"] = None) -> Optional[Any]:
        """
        Get a plugin for the specified language, loading if necessary.

        Args:
            language: Programming language name
            ctx: Optional RepoContext; when provided the plugin cache is keyed by
                 (ctx.repo_id, language) so distinct repos get distinct instances.
                 ctx=None preserves backward compatibility with the single caller
                 at repository_plugin_loader.py that does not pass a context.

        Returns:
            Plugin instance or None if not available
        """
        repo_id = ctx.repo_id if ctx is not None else None
        cache_key = (repo_id, language)

        with self._lock:
            self._expire_idle_plugins()
            if cache_key in self._plugins:
                self._plugins.move_to_end(cache_key)
                self._update_usage(cache_key)
                return self._plugins[cache_key].instance

            return self._load_plugin(language, repo_id=repo_id)

    def _load_plugin(self, language: str, repo_id: Optional[str] = None) -> Optional[Any]:
        """Load a plugin with memory management."""
        cache_key = (repo_id, language)
        start_time = time.time()

        self._ensure_capacity_for_spawn()

        try:
            # Plugin-level semantic search is deprecated: the dispatcher's
            # generation-scoped indexer is the only semantic path (#121).
            plugin = self._factory.get_plugin(language, enable_semantic=False)
            if not plugin:
                return None

            memory_before = self._get_current_memory()

            loaded = LoadedPlugin(
                name=language,
                instance=plugin,
                metadata={
                    "language": language,
                    "repo_id": repo_id,
                    "loaded_at": datetime.now().isoformat(),
                },
            )

            self._plugins[cache_key] = loaded
            self._last_snapshot = None

            memory_after = self._get_current_memory()
            memory_used = max(0, memory_after - memory_before)

            self._plugin_info[cache_key] = PluginMemoryInfo(
                plugin_name=language,
                memory_bytes=memory_used,
                last_used=datetime.now(),
                load_time=time.time() - start_time,
                usage_count=1,
                is_high_priority=language in self.high_priority_langs,
                last_used_monotonic=time.monotonic(),
            )

            def on_plugin_deleted(
                _ref: weakref.ReferenceType[Any],
                key: Tuple[Optional[str], str] = cache_key,
            ) -> None:
                self._on_plugin_deleted(key)

            self._weak_refs[cache_key] = weakref.ref(plugin, on_plugin_deleted)

            logger.info(
                f"Loaded {language} plugin (repo={repo_id}) in "
                f"{self._plugin_info[cache_key].load_time:.2f}s, "
                f"using {memory_used / 1024 / 1024:.1f}MB"
            )

            return plugin

        except PluginBackpressureError:
            raise
        except PluginUnavailableError as e:
            logger.info(
                "Skipping unavailable %s plugin: %s",
                language,
                e.state,
            )
            return None
        except Exception as e:
            logger.error(f"Failed to load {language} plugin: {e}")
            return None

    def _should_evict(self) -> bool:
        """Return True if real process RSS exceeds the configured limit."""
        return self._get_current_memory() >= self.max_memory_bytes

    def _ensure_memory_available(self) -> bool:
        """
        Ensure enough memory is available, evicting plugins if necessary.

        Uses real process RSS (via psutil) as the eviction trigger rather than
        the sum of tracked PluginMemoryInfo.memory_bytes.  The tracked bytes are
        kept for per-plugin attribution / telemetry only.

        Returns:
            True if memory is available, False otherwise
        """
        if not self._should_evict():
            return True

        candidates = self._get_eviction_candidates()
        target_memory = self.max_memory_bytes * 0.9

        for cache_key in candidates:
            if self._get_current_memory() < target_memory:
                break
            self._evict_plugin(cache_key)

        return self._get_current_memory() < self.max_memory_bytes

    def _ensure_capacity_for_spawn(self) -> None:
        """Evict LRU entries until one worker slot and RSS budget are available."""
        snapshot = self.resource_snapshot(force=True)
        if not snapshot.measurement_ok:
            raise PluginBackpressureError("resource_measurement_failed", snapshot)

        while (
            snapshot.reserved_workers >= self.max_workers
            or snapshot.child_rss_bytes >= self.max_memory_bytes
        ):
            candidates = self._get_eviction_candidates(include_high_priority=True)
            if not candidates:
                reason = (
                    "worker_limit" if snapshot.reserved_workers >= self.max_workers else "rss_limit"
                )
                raise PluginBackpressureError(reason, snapshot)
            self._evict_plugin(candidates[0])
            snapshot = self.resource_snapshot(force=True)
            if not snapshot.measurement_ok:
                raise PluginBackpressureError("resource_measurement_failed", snapshot)

    def _get_eviction_candidates(
        self, include_high_priority: bool = False
    ) -> List[Tuple[Optional[str], str]]:
        """Get list of plugin cache keys that can be evicted, ordered by LRU."""
        candidates = []

        for cache_key, info in self._plugin_info.items():
            if include_high_priority or not info.is_high_priority:
                candidates.append((info.last_used, cache_key))

        candidates.sort(key=lambda x: x[0])

        return [key for _, key in candidates]

    def _evict_plugin(self, cache_key: Tuple[Optional[str], str]) -> int:
        """
        Evict a plugin from memory.

        Returns:
            Memory freed in bytes (from tracked attribution; may be 0 for new loads)
        """
        if cache_key not in self._plugins:
            return 0

        info = self._plugin_info.get(cache_key)
        memory_freed = info.memory_bytes if info else 0

        loaded = self._plugins.pop(cache_key, None)
        self._plugin_info.pop(cache_key, None)
        self._weak_refs.pop(cache_key, None)

        if loaded is not None:
            close = getattr(loaded.instance, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    logger.warning("Plugin close failed for %s: %s", cache_key, exc)
        del loaded
        gc.collect()
        self._last_snapshot = None

        language = cache_key[1]
        logger.info(f"Evicted {language} plugin, freed {memory_freed / 1024 / 1024:.1f}MB")

        return memory_freed

    def _update_usage(self, cache_key: Tuple[Optional[str], str]):
        """Update usage statistics for a plugin."""
        if cache_key in self._plugin_info:
            info = self._plugin_info[cache_key]
            info.last_used = datetime.now()
            info.last_used_monotonic = time.monotonic()
            info.usage_count += 1

    def _get_current_memory(self) -> int:
        """Get current process memory usage in bytes."""
        return int(self._process.memory_info().rss)

    def _get_plugin_memory_usage(self) -> int:
        """Get total memory used by plugins (attribution/telemetry only)."""
        return sum(info.memory_bytes for info in self._plugin_info.values())

    @staticmethod
    def _worker_state(plugin: Any) -> Tuple[bool, int]:
        """Return live state and RSS for one adapter, raising on invalid telemetry."""
        running_value = getattr(plugin, "is_worker_running", False)
        running = running_value() if callable(running_value) else running_value
        if not isinstance(running, bool):
            running = False
        if not running:
            return False, 0

        rss_value = getattr(plugin, "worker_rss_bytes", None)
        if rss_value is None:
            raise RuntimeError("live worker does not expose RSS telemetry")
        rss = rss_value() if callable(rss_value) else rss_value
        if not isinstance(rss, int) or rss < 0:
            raise RuntimeError("live worker returned invalid RSS telemetry")
        return True, rss

    def resource_snapshot(self, *, force: bool = False) -> PluginResourceSnapshot:
        """Return a cadence-bounded snapshot of parent and sandbox worker resources."""
        with self._lock:
            now = time.monotonic()
            if (
                not force
                and self._last_snapshot is not None
                and now - self._last_snapshot.sampled_at_monotonic < self.sample_interval_seconds
            ):
                return self._last_snapshot

            parent_rss = 0
            child_rss = 0
            live_workers = 0
            failure_reason: Optional[str] = None
            try:
                parent_rss = self._get_current_memory()
                for loaded in self._plugins.values():
                    running, rss = self._worker_state(loaded.instance)
                    live_workers += int(running)
                    child_rss += rss
            except Exception as exc:
                failure_reason = f"{type(exc).__name__}: {exc}"

            snapshot = PluginResourceSnapshot(
                sampled_at_monotonic=now,
                parent_rss_bytes=parent_rss,
                child_rss_bytes=child_rss,
                reserved_workers=len(self._plugins),
                live_workers=live_workers,
                measurement_ok=failure_reason is None,
                failure_reason=failure_reason,
            )
            self._last_snapshot = snapshot
            return snapshot

    def _expire_idle_plugins(self) -> int:
        """Close plugins idle beyond the configured timeout in deterministic LRU order."""
        if self.idle_timeout_seconds <= 0:
            return 0
        now = time.monotonic()
        expired = [
            cache_key
            for cache_key, info in self._plugin_info.items()
            if info.last_used_monotonic > 0
            and now - info.last_used_monotonic >= self.idle_timeout_seconds
        ]
        expired.sort(key=lambda key: self._plugin_info[key].last_used_monotonic)
        for cache_key in expired:
            self._evict_plugin(cache_key)
        return len(expired)

    def loaded_plugins_for(self, repo_id: str) -> List[Any]:
        """Return loaded plugin instances for one repository without allocating."""
        with self._lock:
            return [
                loaded.instance
                for (loaded_repo_id, _), loaded in self._plugins.items()
                if loaded_repo_id == repo_id
            ]

    def confirm_plugin_started(
        self, language: str, ctx: Optional["RepoContext"] = None
    ) -> PluginResourceSnapshot:
        """Revalidate hard limits after a lazy adapter starts its child worker."""
        repo_id = ctx.repo_id if ctx is not None else None
        cache_key = (repo_id, language)
        with self._lock:
            snapshot = self.resource_snapshot(force=True)
            if not snapshot.measurement_ok:
                self._evict_plugin(cache_key)
                raise PluginBackpressureError("resource_measurement_failed", snapshot)

            while snapshot.child_rss_bytes > self.max_memory_bytes:
                candidates = [
                    key
                    for key in self._get_eviction_candidates(include_high_priority=True)
                    if key != cache_key
                ]
                if not candidates:
                    self._evict_plugin(cache_key)
                    raise PluginBackpressureError("rss_limit", snapshot)
                self._evict_plugin(candidates[0])
                snapshot = self.resource_snapshot(force=True)
                if not snapshot.measurement_ok:
                    self._evict_plugin(cache_key)
                    raise PluginBackpressureError("resource_measurement_failed", snapshot)
            return snapshot

    def evict_repo(self, repo_id: str) -> int:
        """Close and forget every plugin owned by ``repo_id``."""
        with self._lock:
            keys = [key for key in self._plugins if key[0] == repo_id]
            for cache_key in keys:
                self._evict_plugin(cache_key)
            return len(keys)

    def shutdown(self) -> None:
        """Idempotently close every managed plugin adapter."""
        with self._lock:
            for cache_key in list(self._plugins):
                self._evict_plugin(cache_key)

    def _on_plugin_deleted(self, cache_key: Tuple[Optional[str], str]):
        """Callback when a plugin is garbage collected."""
        logger.debug(f"Plugin {cache_key} was garbage collected")
        self._plugin_info.pop(cache_key, None)

    def get_memory_status(self) -> Dict[str, Any]:
        """Get current memory usage status."""
        with self._lock:
            current_memory = self._get_plugin_memory_usage()

            return {
                "max_memory_mb": self.max_memory_bytes / 1024 / 1024,
                "used_memory_mb": current_memory / 1024 / 1024,
                "available_memory_mb": (self.max_memory_bytes - current_memory) / 1024 / 1024,
                "usage_percent": (current_memory / self.max_memory_bytes) * 100,
                "loaded_plugins": len(self._plugins),
                "resources": self.resource_snapshot().__dict__,
                "high_priority_plugins": list(self.high_priority_langs),
                "plugin_details": [
                    {
                        "language": info.plugin_name,
                        "memory_mb": info.memory_bytes / 1024 / 1024,
                        "last_used": info.last_used.isoformat(),
                        "usage_count": info.usage_count,
                        "is_high_priority": info.is_high_priority,
                        "load_time_seconds": info.load_time,
                    }
                    for info in sorted(
                        self._plugin_info.values(), key=lambda x: x.memory_bytes, reverse=True
                    )
                ],
            }

    def preload_high_priority(self, ctx: Optional["RepoContext"] = None):
        """Preload high-priority plugins."""
        logger.info(f"Preloading high-priority plugins: {self.high_priority_langs}")

        for language in self.high_priority_langs:
            self.get_plugin(language, ctx)

    def clear_cache(self, keep_high_priority: bool = True):
        """
        Clear plugin cache.

        Args:
            keep_high_priority: Whether to keep high-priority plugins loaded
        """
        with self._lock:
            keys = list(self._plugins.keys())

            for cache_key in keys:
                info = self._plugin_info.get(cache_key)
                if not keep_high_priority or not info or not info.is_high_priority:
                    self._evict_plugin(cache_key)

    def set_high_priority_languages(self, languages: List[str]):
        """Update the list of high-priority languages."""
        with self._lock:
            self.high_priority_langs = set(languages)

            for cache_key, info in self._plugin_info.items():
                info.is_high_priority = cache_key[1] in self.high_priority_langs

    def get_plugin_info(self, language: str) -> Optional[Dict[str, Any]]:
        """Get detailed information about a specific plugin (global/legacy key only)."""
        with self._lock:
            cache_key = (None, language)
            info = self._plugin_info.get(cache_key)
            if not info:
                return None

            return {
                "language": info.plugin_name,
                "memory_mb": info.memory_bytes / 1024 / 1024,
                "last_used": info.last_used.isoformat(),
                "usage_count": info.usage_count,
                "is_high_priority": info.is_high_priority,
                "load_time_seconds": info.load_time,
                "is_loaded": cache_key in self._plugins,
            }


# Singleton instance
_manager_instance: Optional[MemoryAwarePluginManager] = None
_manager_lock = threading.Lock()


def get_memory_aware_manager(
    max_memory_mb: Optional[int] = None, high_priority_langs: Optional[List[str]] = None
) -> MemoryAwarePluginManager:
    """
    Get the singleton memory-aware plugin manager.

    Args:
        max_memory_mb: Maximum memory limit (from env or default 1024)
        high_priority_langs: High-priority languages (from env or defaults)

    Returns:
        MemoryAwarePluginManager instance
    """
    global _manager_instance

    with _manager_lock:
        if _manager_instance is None:
            # Get configuration from environment
            if max_memory_mb is None:
                max_memory_mb = int(os.environ.get("MCP_MAX_MEMORY_MB", "1024"))

            if high_priority_langs is None:
                env_langs = os.environ.get("MCP_HIGH_PRIORITY_LANGS", "")
                if env_langs:
                    high_priority_langs = [lang.strip() for lang in env_langs.split(",")]
                else:
                    high_priority_langs = ["python", "javascript", "typescript"]

            _manager_instance = MemoryAwarePluginManager(
                max_memory_mb=max_memory_mb,
                high_priority_langs=high_priority_langs,
                max_workers=int(os.environ.get("MCP_MAX_PLUGIN_WORKERS", "16")),
                idle_timeout_seconds=float(
                    os.environ.get("MCP_PLUGIN_IDLE_TIMEOUT_SECONDS", "900")
                ),
                sample_interval_seconds=float(os.environ.get("MCP_PLUGIN_RSS_SAMPLE_SECONDS", "1")),
            )

            # Preload high-priority plugins if configured
            if os.environ.get("MCP_PRELOAD_PLUGINS", "false").lower() == "true":
                _manager_instance.preload_high_priority()

        return _manager_instance
