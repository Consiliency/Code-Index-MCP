"""Transition-focused SAFETY controls using synthetic inputs and owned processes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import psutil
import pytest
from starlette.requests import Request


def test_timeout_does_not_return_while_mutation_is_running():
    from mcp_server.dispatcher.dispatcher_enhanced import _run_blocking_with_timeout

    writes = []

    def mutate():
        time.sleep(0.08)
        writes.append("complete")

    with pytest.raises(TimeoutError):
        _run_blocking_with_timeout(mutate, timeout_seconds=0.01)
    assert writes == ["complete"], "timeout returned while mutation still owned a live thread"


@pytest.mark.parametrize(
    "behavior", ["partial", "stderr_flood", "blocked_stdin", "concurrent_close"]
)
def test_supervisor_transport_deadline_reaps_worker(tmp_path, behavior):
    probe = r"""
import subprocess, sys, threading, time
from mcp_server.sandbox.capabilities import CapabilitySet
from mcp_server.sandbox.supervisor import SandboxSupervisor, SandboxTimeout, SandboxCallError
behavior = sys.argv[1]
worker = "import sys,time; sys.stdout.write('{'); sys.stdout.flush(); time.sleep(60)"
if behavior == "stderr_flood":
    worker = "import sys,time; sys.stderr.write('private-sentinel' * 200000); sys.stderr.flush(); time.sleep(60)"
elif behavior == "blocked_stdin":
    worker = "import time; time.sleep(60)"
sup = SandboxSupervisor([sys.executable, '-c', worker], CapabilitySet((), (), frozenset()))
if behavior == "concurrent_close":
    threading.Timer(0.1, sup.close).start()
start = time.monotonic()
try:
    payload = {'data': 'x' * 1000000} if behavior == "blocked_stdin" else {}
    sup.call('ping', payload, timeout=0.3 if behavior != "concurrent_close" else 30)
except (SandboxTimeout, SandboxCallError):
    pass
else:
    raise AssertionError('request unexpectedly completed')
sup.close()
assert not sup.is_worker_running
assert time.monotonic() - start < 2.5
print('reaped')
"""
    process = subprocess.Popen(
        [sys.executable, "-c", probe, behavior],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=4)
        assert process.returncode == 0, stderr
        assert stdout.strip() == "reaped"
        assert "private-sentinel" not in stderr
    finally:
        if process.poll() is None:
            children = psutil.Process(process.pid).children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            process.kill()
            process.communicate()
            psutil.wait_procs(children, timeout=2)


@pytest.mark.asyncio
async def test_concurrent_shutdown_callers_await_one_cleanup(monkeypatch):
    from mcp_server.cli import stdio_runner as runner

    monkeypatch.setattr(runner, "_shutdown_called", False)
    calls = []
    watcher = MagicMock()

    def stop():
        time.sleep(0.1)
        calls.append("stopped")

    watcher.stop.side_effect = stop
    first = asyncio.create_task(runner._graceful_shutdown(watcher, None, None, None))
    await asyncio.sleep(0.02)
    await runner._graceful_shutdown(watcher, None, None, None)
    assert calls == ["stopped"]
    await first
    watcher.stop.assert_called_once()


def test_metrics_bind_is_loopback_owned_and_reusable():
    from mcp_server.metrics.prometheus_exporter import PrometheusExporter

    first, second = PrometheusExporter(), PrometheusExporter()
    try:
        first.start(0)
        port = first._started_port
        assert port and first._server.server_address[0] == "127.0.0.1"
        with pytest.raises(OSError):
            second.start(port)
        assert second._started_port is None
        first.stop()
        second.start(port)
        assert second._started_port == port
    finally:
        first.stop()
        second.stop()


@pytest.mark.parametrize(
    "trusted,peer,forwarded,expected",
    [
        ("", "192.0.2.5", "198.51.100.5", "192.0.2.5"),
        ("192.0.2.0/24", "192.0.2.5", "198.51.100.5", "198.51.100.5"),
        ("192.0.2.0/24", "192.0.2.5", "203.0.113.1, 198.51.100.5", "198.51.100.5"),
        ("192.0.2.0/24", "192.0.2.5", "invalid", "192.0.2.5"),
    ],
)
def test_forwarded_identity_requires_trusted_peer(monkeypatch, trusted, peer, forwarded, expected):
    from mcp_server.security.security_middleware import RateLimitMiddleware

    monkeypatch.setenv("MCP_TRUSTED_PROXIES", trusted)
    request = Request(
        {
            "type": "http",
            "client": (peer, 1234),
            "headers": [(b"x-forwarded-for", forwarded.encode())],
        }
    )
    middleware = RateLimitMiddleware(MagicMock(), MagicMock())
    assert middleware._get_client_ip(request) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_stdio_normal_logs_omit_arguments_and_exception_payload(monkeypatch, caplog, failure):
    from unittest.mock import AsyncMock

    from mcp.types import TextContent

    from mcp_server.cli import stdio_runner as runner
    from mcp_server.cli.handshake import HandshakeGate

    sentinel = "PRIVATE_CONTENT_SENTINEL_1825"
    monkeypatch.delenv("MCP_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(runner, "_gate", HandshakeGate())
    monkeypatch.setattr(runner, "dispatcher", MagicMock())
    monkeypatch.setattr(runner, "sqlite_store", None)
    monkeypatch.setattr(runner, "initialization_error", None)
    monkeypatch.setattr(runner, "_lazy_summarizer", None)
    handler = (
        AsyncMock(side_effect=RuntimeError(sentinel))
        if failure
        else AsyncMock(return_value=[TextContent(type="text", text="[]")])
    )
    monkeypatch.setattr(runner.tool_handlers, "handle_search_code", handler)
    with caplog.at_level("INFO"):
        result = await runner.call_tool("search_code", {"query": sentinel})
    assert sentinel not in caplog.text
    assert "search_code" in caplog.text
    assert result.isError == failure


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["search_code", "search_symbol"])
@pytest.mark.parametrize("failure", [False, True])
async def test_cross_repo_logs_omit_query_and_failed_provider_payload(
    tmp_path, monkeypatch, caplog, method, failure
):
    from types import SimpleNamespace

    from mcp_server.storage.multi_repo_manager import MultiRepositoryManager

    sentinel = "PRIVATE_CROSS_REPO_SENTINEL_8316"
    manager = MultiRepositoryManager(tmp_path / "registry.json")
    monkeypatch.setattr(
        manager,
        "list_repositories",
        lambda **kw: [SimpleNamespace(repository_id="fixture", name="fixture", priority=0)],
    )
    monkeypatch.setattr(
        manager,
        "_search_code_in_repository" if method == "search_code" else "_search_repository",
        MagicMock(side_effect=RuntimeError(sentinel)) if failure else MagicMock(return_value=None),
    )
    with caplog.at_level("INFO"):
        results = await getattr(manager, method)(sentinel)
    assert sentinel not in caplog.text
    assert sentinel not in repr(results)
    if failure:
        assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_summary_logs_omit_symbol_and_provider_payload(
    tmp_path, monkeypatch, caplog, failure
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from mcp_server.indexing.summarization import ChunkWriter
    from mcp_server.storage.sqlite_store import SQLiteStore

    sentinel = "PRIVATE_SUMMARY_CONTENT_852"
    store = SQLiteStore(str(tmp_path / "summary.db"))
    writer = ChunkWriter(db_path=store.db_path, qdrant_client=None)
    writer.session = SimpleNamespace(
        create_message=(
            AsyncMock(side_effect=RuntimeError(sentinel))
            if failure
            else AsyncMock(
                return_value=SimpleNamespace(
                    content=SimpleNamespace(text="synthetic summary"), model="synthetic"
                )
            )
        )
    )
    monkeypatch.setattr(writer, "can_summarize", lambda: True)
    monkeypatch.setattr(writer, "_has_sampling_capability", lambda: True)
    monkeypatch.setattr(writer, "_has_direct_api", lambda: False)
    monkeypatch.setattr(writer, "_persist_summary", MagicMock())
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    try:
        with caplog.at_level("DEBUG"):
            result = await writer.summarize_chunk("chunk", 1, 1, 2, sentinel, sentinel)
        assert result is None if failure else result == "synthetic summary"
        assert sentinel not in caplog.text
    finally:
        store.close()


@pytest.mark.parametrize(
    "name", ["uvicorn.access", "gunicorn.access", "mcp.server.lowlevel.server"]
)
def test_transport_log_filter_omits_external_content(monkeypatch, caplog, name):
    import logging

    from mcp_server.core.logging import configure_private_diagnostics

    log = logging.getLogger(name)
    monkeypatch.setattr(log, "filters", [])
    configure_private_diagnostics()
    sentinel = "PRIVATE_TRANSPORT_VALUE_833"
    with caplog.at_level("INFO", logger=name):
        if name.endswith("access"):
            log.info(
                '%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/search?q=" + sentinel, "1.1", 200
            )
        else:
            log.error("Received exception from stream: " + sentinel)
    assert sentinel not in caplog.text


def test_background_cache_failure_omits_exception_payload(caplog):
    from mcp_server.watcher.file_watcher import _swallow_task_exception

    task = MagicMock()
    task.exception.return_value = RuntimeError("PRIVATE_WATCHER_VALUE_122")
    with caplog.at_level("ERROR"):
        _swallow_task_exception(task)
    assert "PRIVATE_WATCHER_VALUE_122" not in caplog.text


@pytest.mark.asyncio
async def test_restart_invalidates_access_and_refresh_identity():
    from mcp_server.security.auth_manager import AuthManager
    from mcp_server.security.models import SecurityConfig

    config = SecurityConfig(jwt_secret_key="synthetic-restart-key-000000000000000000")
    first = AuthManager(config)
    user = await first.create_user("fixture", "Synthetic-password-123!", "fixture@example.invalid")
    access = await first.create_access_token(user)
    refresh = await first.create_refresh_token(user)
    assert await first.verify_token(access) is not None
    second = AuthManager(config)
    assert await second.verify_token(access) is None
    assert await second.refresh_access_token(refresh) is None


def test_bootstrap_preserves_shared_metrics_owner():
    from mcp_server.cli.bootstrap import reset_process_singletons
    from mcp_server.metrics.prometheus_exporter import get_prometheus_exporter, record_tool_call

    first = get_prometheus_exporter()
    record_tool_call("safety_owner_test", "success")
    reset_process_singletons()
    assert get_prometheus_exporter() is first
    assert b"safety_owner_test" in first.generate_metrics()


def test_two_processes_cannot_claim_same_metrics_listener():
    from mcp_server.metrics.prometheus_exporter import PrometheusExporter

    exporter = PrometheusExporter()
    exporter.start(0)
    try:
        probe = """
import sys
from mcp_server.metrics.prometheus_exporter import PrometheusExporter
exporter = PrometheusExporter()
try:
    exporter.start(int(sys.argv[1]))
except OSError:
    assert exporter._started_port is None
    print("not-owned")
else:
    exporter.stop()
    raise AssertionError("borrowed another process listener")
"""
        result = subprocess.run(
            [sys.executable, "-c", probe, str(exporter._started_port)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "not-owned"
    finally:
        exporter.stop()
