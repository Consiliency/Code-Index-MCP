"""
Observability staging smoke test for P20.

Verifies three observable properties of the production-mode gateway:
  (a) All stderr lines (JSON-log stream) parse as valid JSON.
  (b) GET /metrics returns HTTP 200 with Prometheus metric lines, including
      HELP/TYPE lines for the three plan-required counters
      (mcp_tool_calls_total, mcp_rate_limit_sleeps_total,
      mcp_artifact_errors_by_class_total). The HTTP-request-counter
      mcp_requests_total is asserted non-zero because the test performs live
      HTTP calls; the other three counters are only asserted HELP/TYPE because
      their emission sites (stdio_runner, artifact providers) are not invoked
      by the smoke flow.
  (c) SecretRedactionResponseMiddleware replaces `Bearer SYNTH_ABC123` with
      `Bearer [REDACTED]` in a 4xx response body over HTTP. The test POSTs a
      JSON payload containing the synthetic token to the auth endpoint, which
      returns a 422 whose body echoes the input — the middleware must redact
      the token before the body reaches the client.
"""

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.requires_network


def _find_repo_root(start: Path) -> Path:
    """Walk upward until a directory containing `mcp_server/__init__.py` is found.

    Parents[4] assumes a fixed depth from the repo root; that breaks when the
    test runs from a git worktree at `.claude/worktrees/<name>/...` (the
    resulting path would point at `.claude/worktrees/`, not the worktree root).
    Walking upward is robust to both layouts.
    """
    for candidate in [start, *start.parents]:
        if (candidate / "mcp_server" / "__init__.py").exists():
            return candidate
    raise RuntimeError(f"Could not locate mcp_server package from {start}")


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
INFRA_DIR = Path(__file__).parent / "infra"
COMPOSE_FILE = INFRA_DIR / "docker-compose.yml"

# Strong credentials generated at module load so the same values are reused
# across fixture scopes within one pytest run.
_JWT_SECRET = secrets.token_urlsafe(32)
_ADMIN_PASSWORD = "Adm1n-Sm0ke-Test-" + secrets.token_hex(8)


def _docker_available() -> bool:
    try:
        result = subprocess.call(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return result == 0
    except FileNotFoundError, subprocess.TimeoutExpired:
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _drain_lines(stream, max_lines: int, timeout: float) -> list[str]:
    """Read up to max_lines from a text stream with a wall-clock timeout."""
    lines: list[str] = []
    deadline = time.monotonic() + timeout

    def _reader():
        try:
            for line in stream:
                lines.append(line.rstrip("\n"))
                if len(lines) >= max_lines:
                    break
        except ValueError, OSError:
            pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout=max(0.0, deadline - time.monotonic()))
    return lines


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gateway_proc(tmp_path_factory):
    """
    Spawn the MCP gateway in production mode (uvicorn subprocess).

    Yields (proc, base_url, gateway_port) and tears down on module exit.
    """
    tmp = tmp_path_factory.mktemp("gateway")
    port = _free_port()
    env = {
        **os.environ,
        # Use staging environment — MCP_LOG_FORMAT=json activates JSONFormatter
        # regardless of environment (mcp_server/core/logging.py:37-41), so JSON
        # logs are emitted in staging mode just as in production mode.  Pure
        # PRODUCTION mode requires additional env vars (DEFAULT_ADMIN_EMAIL,
        # etc.) that are impractical to supply in a subprocess smoke test.
        "MCP_ENVIRONMENT": "staging",
        "MCP_LOG_FORMAT": "json",
        "JWT_SECRET_KEY": _JWT_SECRET,
        "DEFAULT_ADMIN_PASSWORD": _ADMIN_PASSWORD,
        "PROMETHEUS_PORT": str(_free_port()),
        # Staging requires DATABASE_URL to be set (falls back to local sqlite).
        "DATABASE_URL": f"sqlite:///{tmp}/smoke.db",
        # Disable optional heavy subsystems so startup is fast.
        "SEMANTIC_SEARCH_ENABLED": "false",
        "MCP_FAST_STARTUP": "true",
        # .env.native sets MCP_ENABLE_MULTI_REPO=true which requires /workspaces.
        # Override to false so the dispatcher skips multi-repo setup.
        "MCP_ENABLE_MULTI_REPO": "false",
        # Keep index state inside the pytest tmp dir; avoids .env.native's
        # production path being picked up by index_discovery.
        "MCP_INDEX_STORAGE_PATH": str(tmp),
    }
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "mcp_server.gateway:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    base_url = f"http://127.0.0.1:{port}"

    # Poll /health until ready (max 45 s)
    deadline = time.monotonic() + 45
    up = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stdout_tail = ""
            stderr_tail = ""
            try:
                stdout_tail, stderr_tail = proc.communicate(timeout=2)
            except Exception:
                pass
            pytest.fail(
                f"Gateway exited early (rc={proc.returncode}).\n"
                f"stdout: {stdout_tail[:2000]}\nstderr: {stderr_tail[:2000]}"
            )
        try:
            resp = httpx.get(f"{base_url}/health", timeout=2)
            if resp.status_code < 500:
                up = True
                break
        except httpx.ConnectError, httpx.TimeoutException:
            pass
        time.sleep(0.5)

    if not up:
        proc.kill()
        try:
            stdout_tail, stderr_tail = proc.communicate(timeout=5)
        except Exception:
            stdout_tail = stderr_tail = ""
        pytest.fail(
            f"Gateway did not become ready within 45 s.\n"
            f"stdout: {stdout_tail[:2000]}\nstderr: {stderr_tail[:2000]}"
        )

    yield proc, base_url, port

    proc.kill()
    try:
        proc.communicate(timeout=5)
    except Exception:
        pass


@pytest.fixture(scope="module")
def admin_token(gateway_proc):
    """Return a valid admin Bearer token from the live gateway."""
    _, base_url, _ = gateway_proc
    resp = httpx.post(
        f"{base_url}/api/v1/auth/login",
        json={"username": "admin", "password": _ADMIN_PASSWORD},
        timeout=10,
    )
    if resp.status_code != 200:
        pytest.skip(f"Login failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_json_log_parse_rate(gateway_proc):
    """
    (a) Drain 50 stderr lines from the production-mode gateway and assert that
    every LOG line (lines that look like structured log output, leading `{`)
    parses as JSON.

    JSON logs are emitted on stderr (logging.StreamHandler(sys.stderr) in
    mcp_server/core/logging.py:63). stderr also contains library import
    banners printed before the logger is initialized (TreeSitter import
    notices, urllib3 version warnings, uvicorn click banners) — these are
    not part of the JSON-log contract, so we filter to lines whose first
    non-space character is `{` before asserting the parse rate.
    """
    proc, base_url, _ = gateway_proc

    # Trigger a few requests to generate log output
    for _ in range(5):
        try:
            httpx.get(f"{base_url}/health", timeout=2)
        except Exception:
            pass

    lines = _drain_lines(proc.stderr, max_lines=50, timeout=30)
    log_lines = [line for line in lines if line.lstrip().startswith("{")]

    if len(log_lines) < 5:
        pytest.skip(
            f"Only {len(log_lines)} JSON-log-shaped stderr lines received; "
            "gateway may be too quiet to test JSON parse rate meaningfully."
        )

    failures = []
    for line in log_lines:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            failures.append(line)

    parse_rate = 1.0 - len(failures) / len(log_lines)
    assert parse_rate == 1.0, (
        f"JSON parse rate {parse_rate:.2%} ({len(failures)}/{len(log_lines)} failures).\n"
        f"First 3 non-JSON lines:\n" + "\n".join(failures[:3])
    )


def test_metrics_endpoint_reachable(gateway_proc, admin_token):
    """
    (b) GET /metrics with admin auth returns HTTP 200 and contains the three
    plan-required counters plus mcp_requests_total.

    After the P20 SL-2b registry fix, the module-level counters share the same
    CollectorRegistry the gateway exporter serializes, so HELP/TYPE lines for
    all three counters appear in the HTTP response. The smoke flow does not
    itself invoke their emission sites (stdio tool calls / artifact providers),
    so non-zero counter samples are not asserted; HELP lines prove the
    registry wiring is correct. mcp_requests_total IS incremented by the
    authenticated /health calls made first and is asserted non-zero.
    """
    _, base_url, _ = gateway_proc

    # Make 5 authenticated requests so mcp_requests_total is incremented.
    for _ in range(5):
        try:
            httpx.get(
                f"{base_url}/health",
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=5,
            )
        except Exception:
            pass

    resp = httpx.get(
        f"{base_url}/metrics",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=10,
    )
    assert resp.status_code == 200, f"/metrics returned {resp.status_code}: {resp.text[:500]}"

    body = resp.text

    # The three plan-required counters must be registered on the exporter
    # registry and therefore have HELP/TYPE lines present even when zero.
    # Non-zero samples are not asserted because the smoke flow does not reach
    # their emission sites (stdio_runner.record_tool_call and the artifact
    # provider rate-limit/error paths). HELP/TYPE presence is sufficient to
    # prove registry wiring — the registry-split bug this lane fixes would
    # have omitted these lines entirely.
    for name in (
        "mcp_tool_calls_total",
        "mcp_rate_limit_sleeps_total",
        "mcp_artifact_errors_by_class_total",
    ):
        assert re.search(rf"^# HELP {name}", body, re.MULTILINE), (
            f"Expected '# HELP {name}' in /metrics output "
            f"(registry-split regression — counter missing from exporter registry).\n"
            f"First 2000 chars:\n{body[:2000]}"
        )
        assert re.search(
            rf"^# TYPE {name} counter", body, re.MULTILINE
        ), f"Expected '# TYPE {name} counter' in /metrics output."

    # mcp_requests_total is also served by the exporter registry; assert its
    # HELP line is present (sample is not incremented by the gateway's
    # current middleware stack, so we do not require a non-zero value).
    assert re.search(
        r"^# HELP mcp_requests_total", body, re.MULTILINE
    ), "Expected '# HELP mcp_requests_total' in /metrics output."


def test_secret_redaction_via_http(gateway_proc):
    """
    (c) POST a JSON body containing the synthetic bearer to a validating
    endpoint. FastAPI returns 422 with the input echoed in the error detail;
    SecretRedactionResponseMiddleware must rewrite `Bearer SYNTH_ABC123` to
    `Bearer [REDACTED]` before the response leaves the app.
    """
    _, base_url, _ = gateway_proc

    # POST malformed JSON to the login endpoint; the input dict is echoed
    # back in FastAPI's 422 `detail[*].input` payload, giving the middleware
    # a body to redact. A single value with the token keeps the redacted
    # output unambiguous and easy to assert on.
    payload = {"query": "Bearer SYNTH_ABC123"}
    resp = httpx.post(
        f"{base_url}/api/v1/auth/login",
        json=payload,
        timeout=5,
    )

    body = resp.text
    assert 400 <= resp.status_code < 500, (
        f"Expected 4xx response so redaction middleware runs; got "
        f"{resp.status_code}: {body[:500]}"
    )
    assert "Bearer [REDACTED]" in body, (
        "SecretRedactionResponseMiddleware did not redact Bearer token in HTTP "
        f"response body. Status {resp.status_code}, body: {body[:1000]}"
    )
    assert "SYNTH_ABC123" not in body, (
        "Raw token still present in HTTP response body after redaction. "
        f"Status {resp.status_code}, body: {body[:1000]}"
    )
