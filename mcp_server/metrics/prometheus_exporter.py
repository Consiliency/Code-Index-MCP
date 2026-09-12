"""
Prometheus metrics exporter for MCP Server.
Provides detailed metrics for monitoring and alerting.
"""

import logging
import threading
from typing import Any, Callable, Dict, Optional

try:
    from prometheus_client import (  # type: ignore
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        Info,
        generate_latest,
        start_http_server,
    )
    from prometheus_client.core import GaugeMetricFamily  # type: ignore

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    PROMETHEUS_AVAILABLE = False

    class _NoOpMetric:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def labels(self, *args: Any, **kwargs: Any) -> "_NoOpMetric":
            return self

        def observe(self, *args: Any, **kwargs: Any) -> None:
            return None

        def inc(self, *args: Any, **kwargs: Any) -> None:
            return None

        def set(self, *args: Any, **kwargs: Any) -> None:
            return None

    class CollectorRegistry:  # type: ignore[override]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    Counter = Gauge = Histogram = Info = GaugeMetricFamily = _NoOpMetric  # type: ignore
    CONTENT_TYPE_LATEST = "text/plain"

    def generate_latest(registry: CollectorRegistry | None = None) -> bytes:
        return b""

    def start_http_server(port: int, addr: str = "0.0.0.0", registry: Any = None) -> None:  # type: ignore
        return None


logger = logging.getLogger(__name__)

_DISPATCHER_FALLBACK_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

# Shared module-level registry used by the module-level counters AND the
# global PrometheusExporter instance, so /metrics serializes the same
# registry that emission sites increment.
_EXPORTER_REGISTRY = CollectorRegistry()

# Module-level counters registered on the shared exporter registry so they
# appear in PrometheusExporter.generate_metrics() output.
mcp_tool_calls_total = Counter(
    "mcp_tool_calls_total",
    "MCP tool call counter",
    ["tool", "status"],
    registry=_EXPORTER_REGISTRY,
)


mcp_watcher_sweep_errors_total = Counter(
    "mcp_watcher_sweep_errors_total",
    "Count of watcher sweep iterations that raised an exception.",
    registry=_EXPORTER_REGISTRY,
)


mcp_storage_readonly_total = Counter(
    "mcp_storage_readonly_total",
    "Count of SQLite stores that transitioned to read-only mode due to ENOSPC.",
    registry=_EXPORTER_REGISTRY,
)


mcp_rate_limit_sleeps_total = Counter(
    "mcp_rate_limit_sleeps_total",
    "Count of rate-limit back-off sleeps injected by the dispatcher.",
    registry=_EXPORTER_REGISTRY,
)


mcp_artifact_errors_by_class_total = Counter(
    "mcp_artifact_errors_by_class_total",
    "Count of artifact processing errors, labelled by error class.",
    ["error_class"],
    registry=_EXPORTER_REGISTRY,
)


def record_tool_call(tool: str, status: str) -> None:
    """Increment mcp_tool_calls_total for the given tool/status label pair."""
    if not PROMETHEUS_AVAILABLE:
        logger.warning("prometheus_client unavailable; record_tool_call is a no-op")
        return
    try:
        mcp_tool_calls_total.labels(tool=tool, status=status).inc()
    except Exception as exc:  # pragma: no cover
        logger.warning("record_tool_call failed: %s", exc)


class PrometheusExporter:
    """Exports MCP Server metrics in Prometheus format."""

    def __init__(self, registry: CollectorRegistry = None):
        """
        Initialize Prometheus exporter.

        Args:
            registry: Prometheus collector registry
        """
        self.registry = registry or CollectorRegistry()
        self._started_port: Optional[int] = None
        self._server: Any = None
        self._server_thread: Any = None
        if not PROMETHEUS_AVAILABLE:
            logger.warning("prometheus_client is not available; exporting metrics as no-ops")
        self._initialize_metrics()

    def _initialize_metrics(self):
        """Initialize all Prometheus metrics."""
        # Request metrics
        self.request_count = Counter(
            "mcp_requests_total",
            "Total number of requests",
            ["method", "endpoint", "status"],
            registry=self.registry,
        )

        self.request_duration = Histogram(
            "mcp_request_duration_seconds",
            "Request duration in seconds",
            ["method", "endpoint"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
            registry=self.registry,
        )

        # Plugin metrics
        self.plugin_load_duration = Histogram(
            "mcp_plugin_load_duration_seconds",
            "Plugin load duration in seconds",
            ["plugin", "language"],
            registry=self.registry,
        )

        self.plugin_status = Gauge(
            "mcp_plugin_status",
            "Plugin status (1=active, 0=inactive)",
            ["plugin", "language"],
            registry=self.registry,
        )

        self.plugin_errors = Counter(
            "mcp_plugin_errors_total",
            "Total plugin errors",
            ["plugin", "language", "error_type"],
            registry=self.registry,
        )

        # Indexing metrics
        self.symbols_indexed = Counter(
            "mcp_symbols_indexed_total",
            "Total symbols indexed",
            ["language", "symbol_type"],
            registry=self.registry,
        )

        self.files_indexed = Counter(
            "mcp_files_indexed_total",
            "Total files indexed",
            ["language"],
            registry=self.registry,
        )

        self.indexing_duration = Histogram(
            "mcp_indexing_duration_seconds",
            "File indexing duration",
            ["language"],
            buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0),
            registry=self.registry,
        )

        # Search metrics
        self.search_requests = Counter(
            "mcp_search_requests_total",
            "Total search requests",
            ["search_type", "language"],
            registry=self.registry,
        )

        self.search_duration = Histogram(
            "mcp_search_duration_seconds",
            "Search duration",
            ["search_type"],
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0),
            registry=self.registry,
        )

        self.search_results = Histogram(
            "mcp_search_results_count",
            "Number of search results",
            ["search_type"],
            buckets=(0, 1, 5, 10, 25, 50, 100, 250, 500, 1000),
            registry=self.registry,
        )

        # Cache metrics
        self.cache_hits = Counter(
            "mcp_cache_hits_total", "Cache hits", ["cache_type"], registry=self.registry
        )

        self.cache_misses = Counter(
            "mcp_cache_misses_total",
            "Cache misses",
            ["cache_type"],
            registry=self.registry,
        )

        self.cache_evictions = Counter(
            "mcp_cache_evictions_total",
            "Cache evictions",
            ["cache_type", "reason"],
            registry=self.registry,
        )

        # Database metrics
        self.db_queries = Counter(
            "mcp_database_queries_total",
            "Total database queries",
            ["query_type"],
            registry=self.registry,
        )

        self.db_query_duration = Histogram(
            "mcp_database_query_duration_seconds",
            "Database query duration",
            ["query_type"],
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0),
            registry=self.registry,
        )

        self.db_connections = Gauge(
            "mcp_database_connections",
            "Active database connections",
            registry=self.registry,
        )

        # System metrics
        self.memory_usage = Gauge(
            "mcp_memory_usage_bytes",
            "Memory usage in bytes",
            ["type"],
            registry=self.registry,
        )

        self.cpu_usage = Gauge(
            "mcp_cpu_usage_percent", "CPU usage percentage", registry=self.registry
        )

        self.active_threads = Gauge(
            "mcp_active_threads", "Number of active threads", registry=self.registry
        )

        # File watcher metrics
        self.files_watched = Gauge(
            "mcp_files_watched", "Number of files being watched", registry=self.registry
        )

        self.file_changes = Counter(
            "mcp_file_changes_total",
            "Total file changes detected",
            ["change_type"],
            registry=self.registry,
        )

        # Error metrics
        self.errors = Counter(
            "mcp_errors_total",
            "Total errors",
            ["error_type", "component"],
            registry=self.registry,
        )

        # Info metric
        self.build_info = Info("mcp_build", "Build information", registry=self.registry)

        # Dispatcher fallback histogram (IF-0-P11-3)
        if PROMETHEUS_AVAILABLE:
            self.dispatcher_fallback_histogram = Histogram(
                "mcp_dispatcher_fallback_duration_seconds",
                "Dispatcher fallback attempt duration in seconds",
                ["outcome"],
                buckets=_DISPATCHER_FALLBACK_BUCKETS,
                registry=self.registry,
            )
        else:
            self.dispatcher_fallback_histogram = None

        # Hot-path latency histograms (IF-0-P12-4)
        if PROMETHEUS_AVAILABLE:
            self.dispatcher_lookup_histogram = Histogram(
                "mcp_dispatcher_symbol_lookup_duration_seconds",
                "EnhancedDispatcher.lookup end-to-end duration in seconds",
                buckets=_DISPATCHER_FALLBACK_BUCKETS,
                registry=self.registry,
            )
            self.dispatcher_search_histogram = Histogram(
                "mcp_dispatcher_search_duration_seconds",
                "EnhancedDispatcher.search end-to-end duration in seconds",
                buckets=_DISPATCHER_FALLBACK_BUCKETS,
                registry=self.registry,
            )
        else:
            self.dispatcher_lookup_histogram = None
            self.dispatcher_search_histogram = None

        # Structured error taxonomy counter (IF-0-P13-5)
        if PROMETHEUS_AVAILABLE:
            self.errors_by_type = Counter(
                "mcp_errors_by_type_total",
                "Handled errors broken down by module and exception type",
                ["module", "exception"],
                registry=self.registry,
            )
        else:
            self.errors_by_type = None

    def start(self, port: int) -> None:
        """Start the Prometheus HTTP metrics server on the given port (idempotent)."""
        if not PROMETHEUS_AVAILABLE:
            logger.warning("prometheus_client unavailable; PrometheusExporter.start is a no-op")
            return
        if self._started_port is not None:
            if self._started_port == port:
                logger.debug("PrometheusExporter already started on port %d; no-op", port)
            else:
                logger.warning(
                    "PrometheusExporter already started on port %d; ignoring request to bind %d",
                    self._started_port,
                    port,
                )
            return
        try:
            result = start_http_server(port, addr="127.0.0.1", registry=self.registry)
            if isinstance(result, tuple) and len(result) == 2:
                self._server, self._server_thread = result
            if self._server is None:
                raise RuntimeError("Metrics server did not return an owned HTTP listener")
            self._started_port = self._server.server_address[1]
            logger.info("PrometheusExporter HTTP server started on port %d", self._started_port)
        except OSError as exc:
            logger.warning(
                "PrometheusExporter could not bind port %d (%s)", port, type(exc).__name__
            )
            raise

    def stop(self) -> None:
        """Stop the Prometheus HTTP metrics server (best-effort)."""
        if not PROMETHEUS_AVAILABLE:
            logger.warning("prometheus_client unavailable; PrometheusExporter.stop is a no-op")
            return
        if self._started_port is None:
            return
        try:
            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()
            if self._server_thread is not None:
                self._server_thread.join(timeout=5)
        except Exception as exc:  # pragma: no cover
            logger.warning("PrometheusExporter.stop encountered an error: %s", exc)
        finally:
            self._server = None
            self._server_thread = None
            self._started_port = None
            logger.info("PrometheusExporter HTTP server stopped")

    def record_request(self, method: str, endpoint: str, status: int, duration: float):
        """Record HTTP request metrics."""
        self.request_count.labels(method=method, endpoint=endpoint, status=str(status)).inc()
        self.request_duration.labels(method=method, endpoint=endpoint).observe(duration)

    def record_plugin_load(self, plugin: str, language: str, duration: float, success: bool):
        """Record plugin load metrics."""
        self.plugin_load_duration.labels(plugin=plugin, language=language).observe(duration)
        self.plugin_status.labels(plugin=plugin, language=language).set(1 if success else 0)

    def record_plugin_error(self, plugin: str, language: str, error_type: str):
        """Record plugin error."""
        self.plugin_errors.labels(plugin=plugin, language=language, error_type=error_type).inc()

    def record_indexing(self, language: str, symbols_count: int, duration: float):
        """Record indexing metrics."""
        self.files_indexed.labels(language=language).inc()
        self.indexing_duration.labels(language=language).observe(duration)
        # Note: Individual symbol counts would be recorded separately

    def record_search(self, search_type: str, language: str, duration: float, results_count: int):
        """Record search metrics."""
        self.search_requests.labels(search_type=search_type, language=language).inc()
        self.search_duration.labels(search_type=search_type).observe(duration)
        self.search_results.labels(search_type=search_type).observe(results_count)

    def record_cache_access(self, cache_type: str, hit: bool):
        """Record cache access."""
        if hit:
            self.cache_hits.labels(cache_type=cache_type).inc()
        else:
            self.cache_misses.labels(cache_type=cache_type).inc()

    def record_cache_eviction(self, cache_type: str, reason: str):
        """Record cache eviction."""
        self.cache_evictions.labels(cache_type=cache_type, reason=reason).inc()

    def record_db_query(self, query_type: str, duration: float):
        """Record database query."""
        self.db_queries.labels(query_type=query_type).inc()
        self.db_query_duration.labels(query_type=query_type).observe(duration)

    def set_db_connections(self, count: int):
        """Set database connection count."""
        self.db_connections.set(count)

    def set_memory_usage(self, rss: int, vms: int = None):
        """Set memory usage metrics."""
        self.memory_usage.labels(type="rss").set(rss)
        if vms is not None:
            self.memory_usage.labels(type="vms").set(vms)

    def set_cpu_usage(self, percent: float):
        """Set CPU usage."""
        self.cpu_usage.set(percent)

    def set_active_threads(self, count: int):
        """Set active thread count."""
        self.active_threads.set(count)

    def set_files_watched(self, count: int):
        """Set number of files being watched."""
        self.files_watched.set(count)

    def record_file_change(self, change_type: str):
        """Record file change."""
        self.file_changes.labels(change_type=change_type).inc()

    def record_error(self, error_type: str, component: str):
        """Record error."""
        self.errors.labels(error_type=error_type, component=component).inc()

    def set_build_info(self, version: str, commit: str = "", build_time: str = ""):
        """Set build information."""
        self.build_info.info({"version": version, "commit": commit, "build_time": build_time})

    def generate_metrics(self) -> bytes:
        """Generate metrics in Prometheus format."""
        return generate_latest(self.registry)

    def get_content_type(self) -> str:
        """Get Prometheus content type."""
        return CONTENT_TYPE_LATEST


class PrometheusCollector:
    """Custom collector for dynamic metrics."""

    def __init__(self, get_metrics_func: Callable[[], Dict[str, Any]]):
        """
        Initialize collector.

        Args:
            get_metrics_func: Function that returns current metrics
        """
        self.get_metrics_func = get_metrics_func

    def collect(self):
        """Collect metrics."""
        metrics = self.get_metrics_func()

        # Plugin metrics
        if "plugins" in metrics:
            active_plugins = GaugeMetricFamily("mcp_active_plugins_total", "Total active plugins")
            active_plugins.add_metric([], len(metrics["plugins"]))
            yield active_plugins

            for plugin_name, plugin_data in metrics["plugins"].items():
                if "symbols_count" in plugin_data:
                    symbols = GaugeMetricFamily(
                        "mcp_plugin_symbols_total",
                        "Total symbols per plugin",
                        labels=["plugin"],
                    )
                    symbols.add_metric([plugin_name], plugin_data["symbols_count"])
                    yield symbols

        # Index metrics
        if "index" in metrics:
            total_files = GaugeMetricFamily("mcp_indexed_files_total", "Total indexed files")
            total_files.add_metric([], metrics["index"].get("total_files", 0))
            yield total_files

            total_symbols = GaugeMetricFamily("mcp_indexed_symbols_total", "Total indexed symbols")
            total_symbols.add_metric([], metrics["index"].get("total_symbols", 0))
            yield total_symbols

        # Database metrics
        if "database" in metrics:
            db_size = GaugeMetricFamily("mcp_database_size_bytes", "Database size in bytes")
            db_size.add_metric([], metrics["database"].get("size_bytes", 0))
            yield db_size


# Global exporter instance
_exporter = None
_exporter_lock = threading.Lock()


def get_prometheus_exporter() -> PrometheusExporter:
    """Get the global Prometheus exporter instance."""
    global _exporter
    with _exporter_lock:
        if _exporter is None:
            _exporter = PrometheusExporter(registry=_EXPORTER_REGISTRY)
    return _exporter
