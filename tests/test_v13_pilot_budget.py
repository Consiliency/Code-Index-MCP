"""Offline counterexamples for the one approved synthetic inference allowance."""

import json
import socket
import sqlite3
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import httpx
import pytest

from scripts.v13_pilot_budget import BudgetDenied, BudgetLedger, LocalForwarder

_CONNECT = socket.socket.connect
_CONNECT_EX = socket.socket.connect_ex
_CREATE_CONNECTION = socket.create_connection


@pytest.fixture(autouse=True)
def loopback_only(block_unmarked_network, monkeypatch):
    def checked_connect(sock, address):
        if sock.family != socket.AF_UNIX and address[0] != "127.0.0.1":
            raise OSError("Only synthetic loopback stubs are allowed")
        return _CONNECT(sock, address)

    def checked_connect_ex(sock, address):
        if sock.family != socket.AF_UNIX and address[0] != "127.0.0.1":
            raise OSError("Only synthetic loopback stubs are allowed")
        return _CONNECT_EX(sock, address)

    def checked_create(address, *args, **kwargs):
        if address[0] != "127.0.0.1":
            raise OSError("Only synthetic loopback stubs are allowed")
        return _CREATE_CONNECTION(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", checked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", checked_connect_ex)
    monkeypatch.setattr(socket, "create_connection", checked_create)


@pytest.fixture
def clock():
    return [1000.0, 500.0]


@pytest.fixture
def ledger(tmp_path, clock):
    root = tmp_path / "allowance"
    BudgetLedger.initialize(root, "a" * 64)
    return BudgetLedger(root, "a" * 64, clock=lambda: tuple(clock))


def test_reservations_are_cumulative_across_failures_and_restart(ledger, clock):
    first = ledger.reserve("embedding", 50000)
    ledger.finish(first, "http_error", 500)
    reopened = BudgetLedger(ledger.root, "a" * 64, clock=lambda: tuple(clock))
    second = reopened.reserve("embedding", 50000)
    reopened.finish(second, "success", 200)
    with pytest.raises(BudgetDenied, match="token_limit"):
        reopened.reserve("embedding", 1)
    snapshot = reopened.snapshot()
    assert snapshot["reserved_input_units"] == 100000
    assert snapshot["request_count"] == 2
    assert snapshot["requests"][0]["outcome"] == "http_error"


def test_unsettled_restart_fails_closed(ledger, clock):
    ledger.reserve("embedding", 10)
    reopened = BudgetLedger(ledger.root, "a" * 64, clock=lambda: tuple(clock))
    with pytest.raises(BudgetDenied, match="request_inflight"):
        reopened.reserve("embedding", 10)
    assert reopened.snapshot()["reserved_input_units"] == 10


@pytest.mark.parametrize("clock_delta", [(900, 900), (901, 1), (1, 901), (-1, 1), (1, -1)])
def test_deadline_and_clock_rollback_cannot_reset_allowance(ledger, clock, clock_delta):
    request = ledger.reserve("embedding", 10)
    ledger.finish(request, "success", 200)
    clock[0] += clock_delta[0]
    clock[1] += clock_delta[1]
    with pytest.raises(BudgetDenied, match="deadline|clock_rollback"):
        ledger.reserve("embedding", 10)


def test_manifest_change_cannot_reuse_allowance(ledger):
    with pytest.raises(BudgetDenied, match="manifest_mismatch"):
        BudgetLedger(ledger.root, "b" * 64)


@pytest.mark.parametrize("damage", ["delete", "empty", "garbage", "accounting"])
def test_existing_ledger_cannot_be_recreated_or_repaired(ledger, damage):
    path = ledger.root / "ledger.sqlite"
    if damage == "delete":
        path.unlink()
    elif damage == "empty":
        path.write_bytes(b"")
    elif damage == "garbage":
        path.write_bytes(b"not a database")
    else:
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE allowance SET reserved_input_units=1")
    with pytest.raises(BudgetDenied, match="ledger_invalid"):
        BudgetLedger(ledger.root, "a" * 64)
    with pytest.raises(FileExistsError):
        BudgetLedger.initialize(ledger.root, "a" * 64)


def test_uncertain_transport_completion_permanently_stops_admission(ledger):
    request = ledger.reserve("embedding", 10)
    ledger.finish(request, "transport_unknown", None)
    with pytest.raises(BudgetDenied, match="unsettled_transport"):
        ledger.reserve("embedding", 10)


@pytest.mark.parametrize("field", ["start_wall", "start_mono", "last_wall", "last_mono"])
def test_missing_clock_state_cannot_restart_spent_allowance(ledger, field):
    request = ledger.reserve("embedding", 10)
    ledger.finish(request, "success", 200)
    with sqlite3.connect(ledger.root / "ledger.sqlite") as db:
        db.execute(f"UPDATE allowance SET {field}=NULL")
    with pytest.raises(BudgetDenied, match="ledger_invalid"):
        ledger.reserve("embedding", 10)


def test_guard_authentication_precedes_reservation(ledger):
    guard = forwarder(ledger, "http://127.0.0.1:9/v1")
    server = guard.server("SYNTHETIC_TOKEN_" + "x" * 32)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                f"http://127.0.0.1:{server.server_port}/embedding/v1/embeddings",
                json={"model": "fixture", "input": ["x"]},
            )
        assert response.status_code == 403
        assert response.json() == {"error": "unauthorized"}
        assert ledger.snapshot()["request_count"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@contextmanager
def upstream(status=200, entered=None, release=None):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            observed.append((self.path, body, dict(self.headers)))
            if entered is not None:
                entered.set()
                assert release.wait(5)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if status == 302:
                self.send_header("Location", "https://commercial.invalid/leak")
            self.end_headers()
            self.wfile.write(b'{"data":[],"model":"fixture"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def forwarder(ledger, endpoint):
    return LocalForwarder(ledger, {"embedding": endpoint, "enrichment": endpoint})


def test_forwarding_is_reserved_first_and_drops_ambient_secrets(ledger, monkeypatch, caplog):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_SECRET_NEVER_FORWARD")
    body = json.dumps({"model": "fixture", "input": ["SYNTHETIC_SOURCE_NEVER_LOG"]}).encode()
    with upstream() as (endpoint, observed):
        status, _ = forwarder(ledger, endpoint).forward("POST", "/embedding/v1/embeddings", body)
    assert status == 200
    assert len(observed) == 1
    assert observed[0][0] == "/v1/embeddings"
    assert "Authorization" not in observed[0][2]
    snapshot = ledger.snapshot()
    assert snapshot["reserved_input_units"] >= len(body)
    assert snapshot["request_count"] == 1
    assert "SYNTHETIC_SOURCE_NEVER_LOG" not in json.dumps(snapshot) + caplog.text
    assert "SYNTHETIC_SECRET_NEVER_FORWARD" not in json.dumps(snapshot) + caplog.text


def test_redirect_is_never_followed_and_consumes_reservation(ledger):
    with upstream(302) as (endpoint, observed):
        status, body = forwarder(ledger, endpoint).forward(
            "POST", "/embedding/v1/embeddings", b'{"model":"fixture","input":["x"]}'
        )
    assert status == 502
    assert b"redirect_refused" in body
    assert len(observed) == 1
    assert ledger.snapshot()["request_count"] == 1


@pytest.mark.parametrize(
    "route",
    [
        "/embedding/v1/../chat/completions",
        "/embedding/v1/embeddings?url=https://evil.invalid",
        "https://evil.invalid/v1/embeddings",
        "/enrichment/v1/embeddings",
        "/embedding/v1/chat/completions",
    ],
)
def test_routes_are_an_exact_allowlist(ledger, route):
    with upstream() as (endpoint, observed):
        with pytest.raises(BudgetDenied, match="route_refused"):
            forwarder(ledger, endpoint).forward("POST", route, b"{}")
    assert observed == []
    assert ledger.snapshot()["request_count"] == 0


def test_commercial_target_is_rejected_before_io(ledger):
    with pytest.raises(BudgetDenied, match="endpoint_refused"):
        forwarder(ledger, "https://api.openai.com/v1")


def test_concurrent_requests_do_not_overlap_upstream(ledger):
    entered, release = Event(), Event()
    with upstream(entered=entered, release=release) as (endpoint, observed):
        guard = forwarder(ledger, endpoint)
        results = []
        body = b'{"model":"fixture","input":["x"]}'
        worker = Thread(
            target=lambda: results.append(guard.forward("POST", "/embedding/v1/embeddings", body))
        )
        worker.start()
        try:
            assert entered.wait(5)
            with pytest.raises(BudgetDenied, match="request_inflight"):
                guard.forward("POST", "/embedding/v1/embeddings", body)
            assert ledger.snapshot()["request_count"] == 1
        finally:
            release.set()
            worker.join(5)
    assert not worker.is_alive()
    assert len(observed) == len(results) == 1


def test_timeout_does_not_allow_a_retry_to_overlap_unknown_work(ledger, monkeypatch):
    async def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("SYNTHETIC_SOURCE_NEVER_LOG")

    monkeypatch.setattr(httpx.AsyncClient, "send", timeout)
    guard = forwarder(ledger, "http://127.0.0.1:9/v1")
    status, body = guard.forward(
        "POST", "/embedding/v1/embeddings", b'{"model":"fixture","input":["x"]}'
    )
    assert status == 502
    assert b"SYNTHETIC_SOURCE_NEVER_LOG" not in body
    with pytest.raises(BudgetDenied, match="unsettled_transport"):
        guard.forward("POST", "/embedding/v1/embeddings", b'{"model":"fixture","input":["x"]}')
