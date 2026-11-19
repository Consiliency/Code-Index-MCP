"""FastAPI middleware for automatic metrics collection."""

import logging
import time
from typing import Callable, Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from ..metrics import get_metrics_collector

logger = logging.getLogger(__name__)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Middleware that automatically collects HTTP request metrics."""

    def __init__(self, app: ASGIApp, enable_detailed_metrics: bool = True):
        """Initialize the metrics middleware.

        Args:
            app: FastAPI application instance
            enable_detailed_metrics: Whether to collect detailed per-endpoint metrics
        """
        super().__init__(app)
        self.enable_detailed_metrics = enable_detailed_metrics
        self.metrics_collector = get_metrics_collector()
        logger.info("Initialized metrics middleware")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process HTTP request and collect metrics."""
        start_time = time.time()

        # Extract request information
        method = request.method
        path = request.url.path

        # Normalize path for metrics (remove path parameters)
        normalized_path = self._normalize_path(path)

        # Increment request counter
        self.metrics_collector.increment_counter(
            "http_requests_total",
            labels={"method": method, "endpoint": normalized_path},
        )

        # Track active requests
        self.metrics_collector.set_gauge(
            "http_requests_active",
            self.metrics_collector.get_metric_value("http_requests_active", {}) or 0 + 1,
        )

        try:
            # Process request
            response = await call_next(request)

            # Calculate request duration
            duration = time.time() - start_time

            # Record response metrics
            status_code = response.status_code
            status_class = f"{status_code // 100}xx"

            # Record duration histogram
            self.metrics_collector.observe_histogram(
                "http_request_duration_seconds",
                duration,
                labels={
                    "method": method,
                    "endpoint": normalized_path,
                    "status_code": str(status_code),
                    "status_class": status_class,
                },
            )

            # Count responses by status
            self.metrics_collector.increment_counter(
                "http_responses_total",
                labels={
                    "method": method,
                    "endpoint": normalized_path,
                    "status_code": str(status_code),
                    "status_class": status_class,
                },
            )

            # Track response size if available
            if hasattr(response, "headers") and "content-length" in response.headers:
                try:
                    content_length = int(response.headers["content-length"])
                    self.metrics_collector.observe_histogram(
                        "http_response_size_bytes",
                        content_length,
                        labels={"method": method, "endpoint": normalized_path},
                    )
                except ValueError, TypeError:
                    pass

            # Detailed endpoint metrics
            if self.enable_detailed_metrics:
                self._record_endpoint_metrics(method, normalized_path, status_code, duration)

            return response

        except Exception as e:
            # Record error metrics
            duration = time.time() - start_time

            self.metrics_collector.increment_counter(
                "http_requests_failed_total",
                labels={
                    "method": method,
                    "endpoint": normalized_path,
                    "error_type": type(e).__name__,
                },
            )

            self.metrics_collector.observe_histogram(
                "http_request_duration_seconds",
                duration,
                labels={
                    "method": method,
                    "endpoint": normalized_path,
                    "status_code": "500",
                    "status_class": "5xx",
                },
            )

            raise

        finally:
            # Decrement active requests
            current_active = (
                self.metrics_collector.get_metric_value("http_requests_active", {}) or 0
            )
            self.metrics_collector.set_gauge("http_requests_active", max(0, current_active - 1))

    def _normalize_path(self, path: str) -> str:
        """Normalize URL path for metrics to avoid high cardinality.

        Args:
            path: Raw URL path

        Returns:
            Normalized path suitable for metrics
        """
        # Remove query parameters
        if "?" in path:
            path = path.split("?")[0]

        # Replace common path parameters with placeholders
        # This helps avoid high cardinality metrics
        parts = path.split("/")
        normalized_parts = []

        for part in parts:
            if not part:
                continue

            # Replace UUIDs, IDs, and other variable parts
            if self._looks_like_id(part) or part.isdigit():
                normalized_parts.append("{id}")
            else:
                normalized_parts.append(part)

        normalized = "/" + "/".join(normalized_parts) if normalized_parts else "/"

        # Limit path length to prevent extremely long metric names
        if len(normalized) > 100:
            normalized = normalized[:97] + "..."

        return normalized

    def _looks_like_id(self, part: str) -> bool:
        """Check if a path part looks like an ID.

        Args:
            part: Path component to check

        Returns:
            True if it looks like an ID, False otherwise
        """
        # Check for UUID patterns
        if len(part) == 36 and part.count("-") == 4:
            return True

        # Check for long alphanumeric strings (likely IDs)
        if len(part) > 10 and part.replace("-", "").replace("_", "").isalnum():
            return True

        # Check for hex strings
        if len(part) > 8 and all(c in "0123456789abcdefABCDEF" for c in part):
            return True

        return False

    def _record_endpoint_metrics(
        self, method: str, endpoint: str, status_code: int, duration: float
    ) -> None:
        """Record detailed metrics for specific endpoints.

        Args:
            method: HTTP method
            endpoint: Normalized endpoint path
            status_code: HTTP status code
            duration: Request duration in seconds
        """
        # Track specific endpoint performance
        if endpoint in ["/search", "/symbol", "/status"]:
            # Record endpoint-specific metrics
            self.metrics_collector.observe_histogram(
                f"endpoint_{endpoint.lstrip('/').replace('/', '_')}_duration_seconds",
                duration,
                labels={"method": method},
            )

            # Count endpoint usage
            self.metrics_collector.increment_counter(
                f"endpoint_{endpoint.lstrip('/').replace('/', '_')}_requests_total",
                labels={"method": method, "status_code": str(status_code)},
            )


class BusinessMetricsCollector:
    """Collector for business-specific metrics like indexing performance."""

    def __init__(self):
        """Initialize the business metrics collector."""
        self.metrics_collector = get_metrics_collector()
        logger.info("Initialized business metrics collector")

    def record_file_indexed(
        self, file_path: str, language: str, symbols_count: int, duration: float
    ) -> None:
        """Record metrics when a file is indexed.

        Args:
            file_path: Path of the indexed file
            language: Programming language of the file
            symbols_count: Number of symbols found in the file
            duration: Time taken to index the file in seconds
        """
        # Count files indexed by language
        self.metrics_collector.increment_counter(
            "files_indexed_total", labels={"language": language}
        )

        # Record symbols found
        self.metrics_collector.increment_counter(
            "symbols_indexed_total", symbols_count, labels={"language": language}
        )

        # Record indexing duration
        self.metrics_collector.observe_histogram(
            "file_indexing_duration_seconds", duration, labels={"language": language}
        )

        # Track file size if possible
        try:
            from pathlib import Path

            file_size = Path(file_path).stat().st_size
            self.metrics_collector.observe_histogram(
                "indexed_file_size_bytes", file_size, labels={"language": language}
            )
        except OSError, FileNotFoundError:
            pass

    def record_search_performed(
        self, query: str, semantic: bool, results_count: int, duration: float
    ) -> None:
        """Record metrics when a search is performed.

        Args:
            query: Search query
            semantic: Whether semantic search was used
            results_count: Number of results returned
            duration: Time taken to perform search in seconds
        """
        search_type = "semantic" if semantic else "fuzzy"

        # Count searches by type
        self.metrics_collector.increment_counter(
            "searches_performed_total", labels={"search_type": search_type}
        )

        # Record search duration
        self.metrics_collector.observe_histogram(
            "search_duration_seconds", duration, labels={"search_type": search_type}
        )

        # Record results count
        self.metrics_collector.observe_histogram(
            "search_results_count", results_count, labels={"search_type": search_type}
        )

        # Track query length
        self.metrics_collector.observe_histogram(
            "search_query_length", len(query), labels={"search_type": search_type}
        )

    def record_plugin_event(
        self, plugin_name: str, event_type: str, duration: Optional[float] = None
    ) -> None:
        """Record plugin-related metrics.

        Args:
            plugin_name: Name of the plugin
            event_type: Type of event (load, reload, error, etc.)
            duration: Optional duration for timed events
        """
        # Count plugin events
        self.metrics_collector.increment_counter(
            "plugin_events_total",
            labels={"plugin_name": plugin_name, "event_type": event_type},
        )

        # Record timing for relevant events
        if duration is not None and event_type in ["load", "reload", "initialize"]:
            self.metrics_collector.observe_histogram(
                "plugin_operation_duration_seconds",
                duration,
                labels={"plugin_name": plugin_name, "operation": event_type},
            )

    def update_system_metrics(
        self,
        active_plugins: int,
        indexed_files: int,
        database_size: int,
        memory_usage: int,
    ) -> None:
        """Update system-level metrics.

        Args:
            active_plugins: Number of active plugins
            indexed_files: Total number of indexed files
            database_size: Database size in bytes
            memory_usage: Memory usage in bytes
        """
        self.metrics_collector.set_gauge("plugins_active_total", active_plugins)
        self.metrics_collector.set_gauge("files_indexed_current", indexed_files)
        self.metrics_collector.set_gauge("database_size_bytes", database_size)
        self.metrics_collector.set_gauge("memory_usage_bytes", memory_usage)


# Global business metrics collector instance
_business_metrics: Optional[BusinessMetricsCollector] = None


def get_business_metrics() -> BusinessMetricsCollector:
    """Get the global business metrics collector instance.

    Returns:
        Global business metrics collector instance
    """
    global _business_metrics
    if _business_metrics is None:
        _business_metrics = BusinessMetricsCollector()
        logger.info("Initialized global business metrics collector")
    return _business_metrics


def setup_metrics_middleware(app, enable_detailed_metrics: bool = True) -> None:
    """Setup metrics collection middleware for FastAPI app.

    Args:
        app: FastAPI application instance
        enable_detailed_metrics: Whether to enable detailed per-endpoint metrics
    """
    _ = MetricsMiddleware(app, enable_detailed_metrics)
    app.add_middleware(MetricsMiddleware, enable_detailed_metrics=enable_detailed_metrics)
    logger.info("Added metrics middleware to FastAPI application")
