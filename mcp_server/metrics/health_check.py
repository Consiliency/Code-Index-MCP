"""Health check system for monitoring component status."""

import asyncio
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

try:
    import psutil
except ImportError:  # pragma: no cover - optional dependency
    psutil = None

from . import HealthCheckResult, HealthStatus, IHealthCheck

logger = logging.getLogger(__name__)


class ComponentHealthChecker(IHealthCheck):
    """Health checker for system components."""

    def __init__(self, max_workers: int = 4):
        """Initialize the health checker.

        Args:
            max_workers: Maximum number of worker threads for health checks
        """
        self._checks: Dict[str, Callable[[], Awaitable[HealthCheckResult]]] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

        # Register default health checks
        self._register_default_checks()

    def _register_default_checks(self) -> None:
        """Register default system health checks."""
        if psutil is None:
            self.register_health_check("system", lambda: self._psutil_unavailable_async("system"))
            self.register_health_check("memory", lambda: self._psutil_unavailable_async("memory"))
            self.register_health_check("disk", lambda: self._psutil_unavailable_async("disk"))
            logger.warning("psutil is not available; system health metrics will be degraded")
            return

        self.register_health_check("system", self._check_system_health)
        self.register_health_check("memory", self._check_memory_health)
        self.register_health_check("disk", self._check_disk_health)

    async def check_component(self, component_name: str) -> HealthCheckResult:
        """Check the health of a specific component."""
        with self._lock:
            if component_name not in self._checks:
                return HealthCheckResult(
                    component=component_name,
                    status=HealthStatus.UNKNOWN,
                    message=f"No health check registered for component: {component_name}",
                )

        try:
            # Execute health check with timeout
            check_func = self._checks[component_name]
            result = await asyncio.wait_for(check_func(), timeout=30.0)
            logger.debug(f"Health check for {component_name}: {result.status.value}")
            return result
        except asyncio.TimeoutError:
            logger.warning(f"Health check for {component_name} timed out")
            return HealthCheckResult(
                component=component_name,
                status=HealthStatus.UNHEALTHY,
                message="Health check timed out",
            )
        except Exception as e:
            logger.error("Health check for %s failed (%s)", component_name, type(e).__name__)
            return HealthCheckResult(
                component=component_name,
                status=HealthStatus.UNHEALTHY,
                message="Health check failed",
            )

    async def check_all_components(self) -> List[HealthCheckResult]:
        """Check the health of all registered components."""
        results = []

        with self._lock:
            component_names = list(self._checks.keys())

        # Run all health checks concurrently
        tasks = [self.check_component(name) for name in component_names]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Convert exceptions to unhealthy results
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    results[i] = HealthCheckResult(
                        component=component_names[i],
                        status=HealthStatus.UNHEALTHY,
                        message="Health check failed",
                    )

        return results

    def register_health_check(
        self,
        component_name: str,
        check_func: Callable[[], Awaitable[HealthCheckResult]],
    ) -> None:
        """Register a health check function for a component."""
        with self._lock:
            self._checks[component_name] = check_func
            logger.info(f"Registered health check for component: {component_name}")

    def unregister_health_check(self, component_name: str) -> None:
        """Unregister a health check for a component."""
        with self._lock:
            if component_name in self._checks:
                del self._checks[component_name]
                logger.info(f"Unregistered health check for component: {component_name}")

    async def get_overall_health(self) -> HealthCheckResult:
        """Get overall system health status."""
        start_time = time.time()
        results = await self.check_all_components()
        check_duration = time.time() - start_time

        if not results:
            return HealthCheckResult(
                component="system",
                status=HealthStatus.UNKNOWN,
                message="No components registered for health checks",
            )

        # Determine overall status
        unhealthy_count = sum(1 for r in results if r.status == HealthStatus.UNHEALTHY)
        degraded_count = sum(1 for r in results if r.status == HealthStatus.DEGRADED)
        healthy_count = sum(1 for r in results if r.status == HealthStatus.HEALTHY)

        if unhealthy_count > 0:
            overall_status = HealthStatus.UNHEALTHY
            message = f"{unhealthy_count} components unhealthy"
        elif degraded_count > 0:
            overall_status = HealthStatus.DEGRADED
            message = f"{degraded_count} components degraded"
        else:
            overall_status = HealthStatus.HEALTHY
            message = f"All {healthy_count} components healthy"

        # Collect detailed information
        details = {
            "total_components": len(results),
            "healthy": healthy_count,
            "degraded": degraded_count,
            "unhealthy": unhealthy_count,
            "check_duration_seconds": round(check_duration, 3),
            "components": {r.component: r.status.value for r in results},
        }

        return HealthCheckResult(
            component="system", status=overall_status, message=message, details=details
        )

    # Default health check implementations
    async def _psutil_unavailable_async(self, component: str) -> HealthCheckResult:
        """Return a degraded health result when psutil is unavailable."""
        return HealthCheckResult(
            component=component,
            status=HealthStatus.DEGRADED,
            message="psutil is not available; system metrics are disabled",
        )

    async def _check_system_health(self) -> HealthCheckResult:
        """Check basic system health metrics."""
        if psutil is None:
            return await self._psutil_unavailable_async("system")
        try:
            # CPU usage
            cpu_percent = psutil.cpu_percent(interval=1)

            # Load average (Unix systems)
            try:
                load_avg = psutil.getloadavg()
                cpu_cores = psutil.cpu_count()
                load_per_core = load_avg[0] / cpu_cores if cpu_cores else 0
            except (AttributeError, OSError):
                load_per_core = 0  # Windows doesn't have load average

            details = {
                "cpu_percent": cpu_percent,
                "cpu_cores": psutil.cpu_count(),
                "load_average": load_per_core,
            }

            # Determine status based on CPU load
            if cpu_percent > 90 or load_per_core > 2.0:
                status = HealthStatus.UNHEALTHY
                message = "High CPU usage detected"
            elif cpu_percent > 70 or load_per_core > 1.0:
                status = HealthStatus.DEGRADED
                message = "Elevated CPU usage"
            else:
                status = HealthStatus.HEALTHY
                message = "System performance normal"

            return HealthCheckResult(
                component="system", status=status, message=message, details=details
            )
        except Exception:
            return HealthCheckResult(
                component="system",
                status=HealthStatus.UNHEALTHY,
                message="Failed to check system health",
            )

    async def _check_memory_health(self) -> HealthCheckResult:
        """Check memory usage health."""
        if psutil is None:
            return await self._psutil_unavailable_async("memory")
        try:
            memory = psutil.virtual_memory()
            swap = psutil.swap_memory()

            details = {
                "memory_total_gb": round(memory.total / (1024**3), 2),
                "memory_available_gb": round(memory.available / (1024**3), 2),
                "memory_used_percent": memory.percent,
                "swap_total_gb": round(swap.total / (1024**3), 2),
                "swap_used_percent": swap.percent,
            }

            # Determine status based on memory usage
            if memory.percent > 90:
                status = HealthStatus.UNHEALTHY
                message = "Critical memory usage"
            elif memory.percent > 80:
                status = HealthStatus.DEGRADED
                message = "High memory usage"
            else:
                status = HealthStatus.HEALTHY
                message = "Memory usage normal"

            return HealthCheckResult(
                component="memory", status=status, message=message, details=details
            )
        except Exception:
            return HealthCheckResult(
                component="memory",
                status=HealthStatus.UNHEALTHY,
                message="Failed to check memory health",
            )

    async def _check_disk_health(self) -> HealthCheckResult:
        """Check disk usage health."""
        if psutil is None:
            return await self._psutil_unavailable_async("disk")
        try:
            # Check current directory disk usage
            disk_usage = psutil.disk_usage(".")

            details = {
                "disk_total_gb": round(disk_usage.total / (1024**3), 2),
                "disk_free_gb": round(disk_usage.free / (1024**3), 2),
                "disk_used_percent": round((disk_usage.used / disk_usage.total) * 100, 2),
            }

            used_percent = (disk_usage.used / disk_usage.total) * 100

            # Determine status based on disk usage
            if used_percent > 95:
                status = HealthStatus.UNHEALTHY
                message = "Critical disk space"
            elif used_percent > 85:
                status = HealthStatus.DEGRADED
                message = "Low disk space"
            else:
                status = HealthStatus.HEALTHY
                message = "Disk space normal"

            return HealthCheckResult(
                component="disk", status=status, message=message, details=details
            )
        except Exception as e:
            return HealthCheckResult(
                component="disk",
                status=HealthStatus.UNHEALTHY,
                message="Failed to check disk health",
            )

    def create_database_health_check(
        self, db_path: str
    ) -> Callable[[], Awaitable[HealthCheckResult]]:
        """Create a health check function for a SQLite database.

        Args:
            db_path: Path to the SQLite database file

        Returns:
            Async health check function
        """

        async def check_database() -> HealthCheckResult:
            try:
                # Check if database file exists
                if not Path(db_path).exists():
                    return HealthCheckResult(
                        component="database",
                        status=HealthStatus.UNHEALTHY,
                        message=f"Database file does not exist: {db_path}",
                    )

                # Test database connection
                loop = asyncio.get_event_loop()

                def sync_check():
                    conn = sqlite3.connect(db_path, timeout=5.0)
                    try:
                        cursor = conn.cursor()
                        cursor.execute("SELECT 1")
                        result = cursor.fetchone()
                        return result is not None
                    finally:
                        conn.close()

                is_accessible = await loop.run_in_executor(self._executor, sync_check)

                if is_accessible:
                    return HealthCheckResult(
                        component="database",
                        status=HealthStatus.HEALTHY,
                        message="Database connection successful",
                        details={"database_path": db_path},
                    )
                else:
                    return HealthCheckResult(
                        component="database",
                        status=HealthStatus.UNHEALTHY,
                        message="Database connection failed",
                    )
            except Exception:
                return HealthCheckResult(
                    component="database",
                    status=HealthStatus.UNHEALTHY,
                    message="Database health check failed",
                )

        return check_database

    def create_plugin_health_check(
        self, plugin_manager
    ) -> Callable[[], Awaitable[HealthCheckResult]]:
        """Create a health check function for the plugin system.

        Args:
            plugin_manager: Plugin manager instance

        Returns:
            Async health check function
        """

        async def check_plugins() -> HealthCheckResult:
            try:
                if plugin_manager is None:
                    return HealthCheckResult(
                        component="plugins",
                        status=HealthStatus.UNHEALTHY,
                        message="Plugin manager not initialized",
                    )

                # Get plugin status
                plugin_status = plugin_manager.get_plugin_status()
                active_plugins = plugin_manager.get_active_plugins()

                total_plugins = len(plugin_status)
                active_count = len(active_plugins)
                failed_plugins = [
                    name
                    for name, status in plugin_status.items()
                    if status.get("state") == "failed"
                ]

                details = {
                    "total_plugins": total_plugins,
                    "active_plugins": active_count,
                    "failed_plugins": len(failed_plugins),
                    "failed_plugin_names": failed_plugins,
                }

                if len(failed_plugins) > 0:
                    status = HealthStatus.DEGRADED
                    message = f"{len(failed_plugins)} plugins failed to load"
                elif active_count == 0:
                    status = HealthStatus.UNHEALTHY
                    message = "No active plugins"
                else:
                    status = HealthStatus.HEALTHY
                    message = f"{active_count} plugins active"

                return HealthCheckResult(
                    component="plugins", status=status, message=message, details=details
                )
            except Exception:
                return HealthCheckResult(
                    component="plugins",
                    status=HealthStatus.UNHEALTHY,
                    message="Plugin health check failed",
                )

        return check_plugins

    def shutdown(self) -> None:
        """Shutdown the health checker and cleanup resources."""
        logger.info("Shutting down health checker")
        self._executor.shutdown(wait=True)


# Global health checker instance
_health_checker: Optional[ComponentHealthChecker] = None


def get_health_checker() -> ComponentHealthChecker:
    """Get the global health checker instance.

    Returns:
        Global health checker instance
    """
    global _health_checker
    if _health_checker is None:
        _health_checker = ComponentHealthChecker()
        logger.info("Initialized global health checker")
    return _health_checker


def set_health_checker(checker: ComponentHealthChecker) -> None:
    """Set the global health checker instance.

    Args:
        checker: Health checker instance to set as global
    """
    global _health_checker
    _health_checker = checker
    logger.info("Set global health checker")
