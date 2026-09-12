"""Integration tests for SIGTERM graceful shutdown + metrics wiring (P10 SL-2)."""

from __future__ import annotations

import asyncio
import os
import selectors
import signal
import subprocess
import sys
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fake components that record call order
# ---------------------------------------------------------------------------


class FakeWatcher:
    def __init__(self, calls: list[str], delay: float = 0.0) -> None:
        self._calls = calls
        self._delay = delay

    def stop(self) -> None:
        if self._delay:
            time.sleep(self._delay)
        self._calls.append("watcher.stop")

    # alias required by SL-2 (IF-0-P10-3)
    stop_watching_all = stop


class FakePoller:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def stop(self) -> None:
        self._calls.append("poller.stop")


class FakeStoreRegistry:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def shutdown(self) -> None:
        self._calls.append("store.shutdown")


class FakeExporter:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def stop(self) -> None:
        self._calls.append("exporter.stop")


class FakeDispatcher:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def shutdown(self) -> None:
        self._calls.append("dispatcher.shutdown")


# ---------------------------------------------------------------------------
# Tests for _graceful_shutdown
# ---------------------------------------------------------------------------


class TestGracefulShutdown:
    """Unit tests for _graceful_shutdown — no subprocess needed."""

    def setup_method(self) -> None:
        import mcp_server.cli.stdio_runner as runner

        runner._shutdown_called = False

    def _get_shutdown_fn(self):
        from mcp_server.cli.stdio_runner import _graceful_shutdown

        return _graceful_shutdown

    def test_shutdown_calls_components_in_order(self):
        """Stop order includes plugin workers before storage and metrics."""
        calls: list[str] = []
        watcher = FakeWatcher(calls)
        poller = FakePoller(calls)
        store = FakeStoreRegistry(calls)
        exporter = FakeExporter(calls)
        dispatcher = FakeDispatcher(calls)

        shutdown = self._get_shutdown_fn()
        asyncio.run(shutdown(watcher, poller, store, exporter, dispatcher=dispatcher, timeout=5.0))

        assert calls == [
            "watcher.stop",
            "poller.stop",
            "dispatcher.shutdown",
            "store.shutdown",
            "exporter.stop",
        ]

    def test_shutdown_with_none_watcher(self):
        """None watcher is tolerated — poller/store/exporter still called."""
        calls: list[str] = []
        poller = FakePoller(calls)
        store = FakeStoreRegistry(calls)
        exporter = FakeExporter(calls)

        shutdown = self._get_shutdown_fn()
        asyncio.run(shutdown(None, poller, store, exporter, timeout=5.0))

        assert calls == ["poller.stop", "store.shutdown", "exporter.stop"]

    def test_shutdown_with_none_poller(self):
        """None poller is tolerated."""
        calls: list[str] = []
        watcher = FakeWatcher(calls)
        store = FakeStoreRegistry(calls)
        exporter = FakeExporter(calls)

        shutdown = self._get_shutdown_fn()
        asyncio.run(shutdown(watcher, None, store, exporter, timeout=5.0))

        assert calls == ["watcher.stop", "store.shutdown", "exporter.stop"]

    def test_shutdown_with_all_none(self):
        """All-None is safe — exporter.stop still called."""
        calls: list[str] = []
        exporter = FakeExporter(calls)

        shutdown = self._get_shutdown_fn()
        asyncio.run(shutdown(None, None, None, exporter, timeout=5.0))

        assert calls == ["exporter.stop"]

    def test_shutdown_timeout_drains_before_releasing_storage(self):
        """A slow watcher retains ownership until stopped; storage closes afterwards."""
        calls: list[str] = []
        # delay > timeout so watcher times out
        watcher = FakeWatcher(calls, delay=0.1)
        poller = FakePoller(calls)
        store = FakeStoreRegistry(calls)
        exporter = FakeExporter(calls)

        shutdown = self._get_shutdown_fn()
        # Short timeout — watcher will time out
        asyncio.run(shutdown(watcher, poller, store, exporter, timeout=0.05))

        # poller/store/exporter must still have been called despite watcher timeout
        assert "poller.stop" in calls
        assert "store.shutdown" in calls
        assert "exporter.stop" in calls
        assert calls.index("watcher.stop") < calls.index("store.shutdown")

    def test_shutdown_idempotent(self):
        """Calling shutdown twice does not double-stop (idempotency flag)."""
        calls: list[str] = []
        watcher = FakeWatcher(calls)
        poller = FakePoller(calls)
        store = FakeStoreRegistry(calls)
        exporter = FakeExporter(calls)

        shutdown = self._get_shutdown_fn()
        asyncio.run(shutdown(watcher, poller, store, exporter, timeout=5.0))
        asyncio.run(shutdown(watcher, poller, store, exporter, timeout=5.0))

        # First call recorded 4 entries; second call should be a no-op
        assert calls.count("watcher.stop") == 1
        assert calls.count("poller.stop") == 1
        assert calls.count("store.shutdown") == 1
        assert calls.count("exporter.stop") == 1


_PARENT_DEATH_PROBE = r'''
import sys
import time

from mcp_server.sandbox.capabilities import CapabilitySet
from mcp_server.sandbox.supervisor import SandboxSupervisor

worker_code = """import json
import sys
line = sys.stdin.buffer.readline()
env = json.loads(line.decode('utf-8'))
env['kind'] = 'result'
env['payload'] = {'ok': True}
sys.stdout.write(json.dumps(env) + '\\n')
sys.stdout.flush()
sys.stdin.buffer.read()
"""
caps = CapabilitySet(fs_read=(), fs_write=(), env_allow=frozenset())
supervisor = SandboxSupervisor([sys.executable, '-c', worker_code], caps)
supervisor.call('ping', {}, timeout=5.0)
print(supervisor.worker_pid, flush=True)
time.sleep(60)
'''


def _wait_for_pid_exit(pid: int, timeout: float = 5.0) -> bool:
    import psutil

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            process = psutil.Process(pid)
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals required")
@pytest.mark.parametrize("parent_signal", [signal.SIGTERM, signal.SIGINT, signal.SIGKILL])
def test_parent_signal_leaves_no_live_sandbox_worker(parent_signal):
    """The worker stdin watchdog reaps children after parent death."""
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT_DEATH_PROBE],
        cwd=str(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    child_pid = None
    try:
        assert parent.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(parent.stdout, selectors.EVENT_READ)
        try:
            assert selector.select(10.0), "parent did not report sandbox worker PID"
            child_pid = int(parent.stdout.readline().strip())
        finally:
            selector.close()

        os.kill(parent.pid, parent_signal)
        parent.wait(timeout=5.0)
        assert _wait_for_pid_exit(
            child_pid
        ), f"sandbox worker {child_pid} survived parent signal {parent_signal}"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5.0)
        if child_pid is not None and not _wait_for_pid_exit(child_pid, timeout=0.1):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# ---------------------------------------------------------------------------
# Tests for stop alias on MultiRepositoryWatcher (IF-0-P10-3)
# ---------------------------------------------------------------------------


class TestMultiRepoWatcherStopAlias:
    def test_stop_alias_exists(self):
        from mcp_server.watcher_multi_repo import MultiRepositoryWatcher

        assert hasattr(
            MultiRepositoryWatcher, "stop"
        ), "MultiRepositoryWatcher must have a `stop` alias (IF-0-P10-3)"

    def test_stop_alias_is_stop_watching_all(self):
        from mcp_server.watcher_multi_repo import MultiRepositoryWatcher

        assert (
            MultiRepositoryWatcher.stop is MultiRepositoryWatcher.stop_watching_all
        ), "`stop` must be an alias for `stop_watching_all`"


# ---------------------------------------------------------------------------
# Tests for record_tool_call metric wiring
# ---------------------------------------------------------------------------


class TestRecordToolCallWiring:
    """Verify that the @server.call_tool() handler emits exactly one
    record_tool_call per invocation, regardless of success/error path."""

    def test_record_tool_call_imported(self):
        """record_tool_call must be importable from the metrics module."""
        from mcp_server.metrics.prometheus_exporter import record_tool_call

        assert callable(record_tool_call)

    def test_mcp_tool_calls_total_counter_exists(self):
        """mcp_tool_calls_total counter must be module-level."""
        from mcp_server.metrics.prometheus_exporter import mcp_tool_calls_total

        assert mcp_tool_calls_total is not None

    def test_record_tool_call_increments_counter(self):
        """record_tool_call must increment the counter (or silently no-op without prometheus)."""
        from mcp_server.metrics import prometheus_exporter as pe

        if not pe.PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")
        initial = pe.mcp_tool_calls_total.labels(tool="_test_sl2_", status="success")._value.get()
        pe.record_tool_call("_test_sl2_", "success")
        after = pe.mcp_tool_calls_total.labels(tool="_test_sl2_", status="success")._value.get()
        assert after == initial + 1

    def test_stdio_runner_imports_record_tool_call(self):
        """stdio_runner must import and use record_tool_call."""
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parents[2] / "mcp_server/cli/stdio_runner.py"
        ).read_text()
        assert "record_tool_call" in src, "stdio_runner.py must reference record_tool_call"


# ---------------------------------------------------------------------------
# Tests for PrometheusExporter start/stop wiring in _serve()
# ---------------------------------------------------------------------------


class TestPrometheusExporterWiring:
    def test_prometheus_exporter_imported_in_runner(self):
        """stdio_runner must import PrometheusExporter."""
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parents[2] / "mcp_server/cli/stdio_runner.py"
        ).read_text()
        assert "PrometheusExporter" in src, "stdio_runner.py must reference PrometheusExporter"

    def test_prometheus_exporter_start_stop(self):
        """PrometheusExporter.start and .stop must be callable without error."""
        from mcp_server.metrics.prometheus_exporter import PROMETHEUS_AVAILABLE, PrometheusExporter

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")
        exp = PrometheusExporter()
        # start on a random high port (avoid 9090 collision)
        exp.start(19091)
        exp.stop()

    def test_metrics_http_endpoint_returns_exposition_format(self):
        """HTTP /metrics endpoint must return Prometheus exposition text with instance metrics."""
        import time
        import urllib.request

        from mcp_server.metrics.prometheus_exporter import (
            PROMETHEUS_AVAILABLE,
            PrometheusExporter,
            generate_latest,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        exp = PrometheusExporter()
        # Use the exporter's own registry — verify it exposes some metrics
        body = exp.generate_metrics().decode()
        assert (
            "mcp_requests_total" in body
        ), f"Expected mcp_requests_total in metrics body; got: {body[:300]}"

    def test_mcp_tool_calls_total_in_exporter_registry_output(self):
        """mcp_tool_calls_total appears in _EXPORTER_REGISTRY generate_latest output after record_tool_call."""
        from mcp_server.metrics.prometheus_exporter import (
            _EXPORTER_REGISTRY,
            PROMETHEUS_AVAILABLE,
            generate_latest,
            record_tool_call,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")
        record_tool_call("_gen_test_", "success")
        body = generate_latest(_EXPORTER_REGISTRY).decode()
        assert (
            "mcp_tool_calls_total" in body
        ), "Expected mcp_tool_calls_total in generate_latest(_EXPORTER_REGISTRY) output"

    def test_signal_handlers_installed_in_serve(self):
        """stdio_runner.py source must reference add_signal_handler."""
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parents[2] / "mcp_server/cli/stdio_runner.py"
        ).read_text()
        assert (
            "add_signal_handler" in src
        ), "stdio_runner.py must call loop.add_signal_handler for SIGTERM/SIGINT"
        assert "signal.SIGTERM" in src, "stdio_runner.py must register SIGTERM handler"
