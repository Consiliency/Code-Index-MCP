"""Durable, fail-closed allowance for the approved synthetic local pilot only."""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

INPUT_LIMIT = 100000
SECONDS_LIMIT = 900
APPROVAL = "v13-freeze-178b8328-20260911-synthetic-local"
ENDPOINTS = {"embedding": "http://ai:8001/v1", "enrichment": "http://ai:8002/v1"}
ROUTES = {
    ("GET", "/embedding/v1/models"): ("embedding", "/models"),
    ("GET", "/enrichment/v1/models"): ("enrichment", "/models"),
    ("POST", "/embedding/v1/embeddings"): ("embedding", "/embeddings"),
    ("POST", "/enrichment/v1/chat/completions"): ("enrichment", "/chat/completions"),
}


class BudgetDenied(RuntimeError):
    """Reason-coded refusal containing no provider payload or credentials."""


class BudgetLedger:
    """One persistent allowance; opening an existing root never initializes it."""

    @staticmethod
    def initialize(root: Path, manifest_sha256: str) -> None:
        if len(manifest_sha256) != 64 or any(c not in "0123456789abcdef" for c in manifest_sha256):
            raise BudgetDenied("manifest_invalid")
        root.mkdir(mode=0o700, parents=False, exist_ok=False)
        with sqlite3.connect(root / "ledger.sqlite") as db:
            db.executescript("""
                PRAGMA synchronous=FULL;
                CREATE TABLE allowance (
                    id INTEGER PRIMARY KEY CHECK (id=1), approval TEXT NOT NULL,
                    manifest TEXT NOT NULL, reserved_input_units INTEGER NOT NULL DEFAULT 0,
                    start_wall REAL, start_mono REAL, last_wall REAL, last_mono REAL,
                    blocked TEXT
                );
                CREATE TABLE requests (
                    id TEXT PRIMARY KEY, role TEXT NOT NULL, input_units INTEGER NOT NULL,
                    started_wall REAL NOT NULL, finished_wall REAL, outcome TEXT NOT NULL,
                    http_status INTEGER
                );
                """)
            db.execute(
                "INSERT INTO allowance(id, approval, manifest) VALUES (1, ?, ?)",
                (APPROVAL, manifest_sha256),
            )
        (root / "ledger.sqlite").chmod(0o600)

    def __init__(
        self,
        root: Path,
        manifest_sha256: str,
        *,
        clock: Callable[[], tuple[float, float]] = lambda: (time.time(), time.monotonic()),
    ) -> None:
        self.root = root
        self.manifest_sha256 = manifest_sha256
        self.clock = clock
        with self._transaction():
            pass

    @contextmanager
    def _transaction(self):
        db = None
        try:
            path = self.root / "ledger.sqlite"
            if path.is_symlink() or not path.is_file():
                raise BudgetDenied("ledger_invalid")
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise BudgetDenied("ledger_invalid")
            rows = db.execute("SELECT * FROM allowance").fetchall()
            if len(rows) != 1 or rows[0]["approval"] != APPROVAL:
                raise BudgetDenied("ledger_invalid")
            state = dict(rows[0])
            if state["manifest"] != self.manifest_sha256:
                raise BudgetDenied("manifest_mismatch")
            used = db.execute("SELECT COALESCE(SUM(input_units),0) FROM requests").fetchone()[0]
            invalid = db.execute(
                "SELECT COUNT(*) FROM requests WHERE input_units<=0 OR outcome NOT IN "
                "('inflight','success','http_error','redirect_refused','transport_unknown')"
            ).fetchone()[0]
            if used != state["reserved_input_units"] or not 0 <= used <= INPUT_LIMIT or invalid:
                raise BudgetDenied("ledger_invalid")
            clocks = [state[key] for key in ("start_wall", "start_mono", "last_wall", "last_mono")]
            if used and any(value is None or not math.isfinite(value) for value in clocks):
                raise BudgetDenied("ledger_invalid")
            if not used and any(value is not None for value in clocks):
                raise BudgetDenied("ledger_invalid")
            yield db, state
            db.commit()
        except sqlite3.Error:
            raise BudgetDenied("ledger_invalid") from None
        finally:
            if db is not None:
                db.close()

    def _remaining(self, state: dict) -> tuple[float, float, float]:
        wall, mono = self.clock()
        if not all(math.isfinite(value) for value in (wall, mono)):
            raise BudgetDenied("clock_invalid")
        if state["start_wall"] is None:
            return wall, mono, float(SECONDS_LIMIT)
        if wall < state["last_wall"] or mono < state["last_mono"]:
            raise BudgetDenied("clock_rollback")
        remaining = SECONDS_LIMIT - max(wall - state["start_wall"], mono - state["start_mono"])
        if remaining <= 0:
            raise BudgetDenied("deadline")
        return wall, mono, remaining

    def reserve(self, role: str, input_units: int) -> str:
        if role not in ENDPOINTS or type(input_units) is not int or input_units <= 0:
            raise BudgetDenied("reservation_invalid")
        with self._transaction() as (db, state):
            if state["blocked"]:
                raise BudgetDenied(state["blocked"])
            wall, mono, _ = self._remaining(state)
            if db.execute("SELECT COUNT(*) FROM requests WHERE outcome='inflight'").fetchone()[0]:
                raise BudgetDenied("request_inflight")
            if state["reserved_input_units"] + input_units > INPUT_LIMIT:
                raise BudgetDenied("token_limit")
            request_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO requests(id,role,input_units,started_wall,outcome) VALUES (?,?,?,?,'inflight')",
                (request_id, role, input_units, wall),
            )
            db.execute(
                "UPDATE allowance SET reserved_input_units=reserved_input_units+?, "
                "start_wall=COALESCE(start_wall,?), start_mono=COALESCE(start_mono,?), "
                "last_wall=?, last_mono=? WHERE id=1",
                (input_units, wall, mono, wall, mono),
            )
        return request_id

    def remaining_seconds(self) -> float:
        with self._transaction() as (_, state):
            return self._remaining(state)[2]

    def finish(self, request_id: str, outcome: str, http_status: int | None) -> None:
        if outcome not in {"success", "http_error", "redirect_refused", "transport_unknown"}:
            raise BudgetDenied("outcome_invalid")
        with self._transaction() as (db, state):
            wall, mono = self.clock()
            updated = db.execute(
                "UPDATE requests SET outcome=?, http_status=?, finished_wall=? "
                "WHERE id=? AND outcome='inflight'",
                (outcome, http_status, wall, request_id),
            ).rowcount
            if updated != 1:
                raise BudgetDenied("settlement_invalid")
            blocked = "unsettled_transport" if outcome == "transport_unknown" else state["blocked"]
            if wall < state["last_wall"] or mono < state["last_mono"]:
                blocked = "clock_rollback"
            db.execute(
                "UPDATE allowance SET last_wall=MAX(last_wall,?),last_mono=MAX(last_mono,?),blocked=?",
                (wall, mono, blocked),
            )

    def snapshot(self) -> dict:
        with self._transaction() as (db, state):
            rows = [dict(row) for row in db.execute("SELECT * FROM requests ORDER BY rowid")]
            return {
                "schema": "v13-pilot-budget.v1",
                "approval": APPROVAL,
                "manifest_sha256": state["manifest"],
                "input_limit": INPUT_LIMIT,
                "seconds_limit": SECONDS_LIMIT,
                "reserved_input_units": state["reserved_input_units"],
                "request_count": len(rows),
                "started_wall": state["start_wall"],
                "last_wall": state["last_wall"],
                "blocked": state["blocked"],
                "requests": rows,
            }


class LocalForwarder:
    """No proxy env, redirects, commercial keys, retries or arbitrary routes."""

    def __init__(self, ledger: BudgetLedger, endpoints: dict[str, str] | None = None) -> None:
        self.ledger = ledger
        self._forward_lock = threading.Lock()
        self.endpoints = dict(ENDPOINTS if endpoints is None else endpoints)
        if set(self.endpoints) != set(ENDPOINTS):
            raise BudgetDenied("endpoint_refused")
        for role, endpoint in self.endpoints.items():
            url = urlsplit(endpoint)
            loopback = (
                url.scheme == "http"
                and url.hostname == "127.0.0.1"
                and url.port
                and url.path == "/v1"
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
            )
            if endpoint != ENDPOINTS[role] and not loopback:
                raise BudgetDenied("endpoint_refused")

    async def _request(self, method: str, target: str, body: bytes, remaining: float):
        async with asyncio.timeout(min(90.0, remaining)):
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=remaining
            ) as client:
                async with client.stream(
                    method, target, content=body, headers={"Content-Type": "application/json"}
                ) as response:
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > 32 * 1024 * 1024:
                            raise BudgetDenied("response_limit")
                    return response.status_code, bytes(chunks)

    def forward(self, method: str, route: str, body: bytes) -> tuple[int, bytes]:
        if (method, route) not in ROUTES:
            raise BudgetDenied("route_refused")
        role, suffix = ROUTES[method, route]
        if len(body) > 8192 or (method == "GET" and body):
            raise BudgetDenied("request_limit")
        messages = []
        if method == "POST":
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
                    raise ValueError
                if suffix == "/chat/completions":
                    messages = payload["messages"]
                    if not isinstance(messages, list) or payload.get("stream"):
                        raise ValueError
                    if any(
                        not isinstance(m, dict) or not isinstance(m.get("content"), str)
                        for m in messages
                    ):
                        raise ValueError
                elif not isinstance(payload.get("input"), (str, list)):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise BudgetDenied("request_invalid") from None
        # UTF-8 bytes conservatively bound input tokens, plus chat framing overhead.
        units = max(1, len(body)) + 32 * (1 + len(messages))
        request_id = self.ledger.reserve(role, units)
        try:
            status, response = asyncio.run(
                self._request(
                    method, self.endpoints[role] + suffix, body, self.ledger.remaining_seconds()
                )
            )
        except Exception:
            self.ledger.finish(request_id, "transport_unknown", None)
            return 502, b'{"error":"transport_unknown"}'
        if 300 <= status < 400:
            self.ledger.finish(request_id, "redirect_refused", status)
            return 502, b'{"error":"redirect_refused"}'
        self.ledger.finish(request_id, "success" if 200 <= status < 300 else "http_error", status)
        return status, response

    def server(self, token: str) -> ThreadingHTTPServer:
        if len(token) < 32:
            raise BudgetDenied("guard_token_invalid")
        guard = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def dispatch(self):
                try:
                    if not hmac.compare_digest(
                        self.headers.get("Authorization", ""), "Bearer " + token
                    ):
                        raise BudgetDenied("unauthorized")
                    if self.headers.get("Transfer-Encoding"):
                        raise BudgetDenied("request_invalid")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= 8192:
                        raise BudgetDenied("request_limit")
                    self.connection.settimeout(5)
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise BudgetDenied("request_invalid")
                    status, response = guard.forward_serialized(self.command, self.path, body)
                except (BudgetDenied, ValueError, OSError) as exc:
                    reason = str(exc) if isinstance(exc, BudgetDenied) else "request_invalid"
                    status, response = 403, json.dumps({"error": reason}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                try:
                    self.wfile.write(response)
                except OSError:
                    pass

            do_GET = dispatch
            do_POST = dispatch

        return ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    def forward_serialized(self, method: str, route: str, body: bytes) -> tuple[int, bytes]:
        if not self._forward_lock.acquire(timeout=min(90, self.ledger.remaining_seconds())):
            raise BudgetDenied("queue_timeout")
        try:
            return self.forward(method, route, body)
        finally:
            self._forward_lock.release()
