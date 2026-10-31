"""Authenticated Qdrant deployment: anonymous access is denied, authorized access works.

Starts the repository-owned ``docker-compose.qdrant.yml`` service in a disposable,
uniquely named Compose project, with an override that only isolates it (no fixed
container name, loopback-only host ports, tmpfs storage). The fail-closed guard,
pinned image and API-key wiring come from the real file. Only synthetic collections
are used. A per-run random key is passed through the environment and never printed.

Skips when Docker or the pinned image is unavailable (``--pull never``).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib import error, request

import pytest

from mcp_server.config.env_vars import QdrantAuthConfigError
from mcp_server.setup.semantic_preflight import ServiceStatus, check_qdrant
from mcp_server.utils.semantic_indexer import SemanticIndexer

pytestmark = [pytest.mark.integration, pytest.mark.requires_network]

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "docker-compose.qdrant.yml"
IMAGE = (
    "qdrant/qdrant:v1.17.0@sha256:"
    "f1c7272cdac52b38c1a0e89313922d940ba50afd90d593a1605dbbc214e66ffb"
)


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "compose", "version"], capture_output=True, check=True)
        subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


requires_docker = pytest.mark.skipif(
    not _docker_ready(), reason="docker compose and the pinned Qdrant image are required"
)


def _free_ports(count: int) -> list[int]:
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def _override(path: Path, http_port: int, grpc_port: int, key_env: str) -> Path:
    # Host network bound to loopback works on hosts whose firewall drops bridge traffic.
    override = path / "override.yml"
    override.write_text(
        "services:\n"
        "  qdrant:\n"
        "    container_name: !reset null\n"
        "    restart: 'no'\n"
        "    network_mode: host\n"
        "    ports: !reset []\n"
        # tmpfs: the container runs as root, so a host bind mount leaves root-owned files.
        "    volumes: !reset []\n"
        "    tmpfs:\n"
        "      - /qdrant/storage\n"
        "    environment:\n"
        "      - QDRANT__SERVICE__HOST=127.0.0.1\n"
        f"      - QDRANT__SERVICE__HTTP_PORT={http_port}\n"
        f"      - QDRANT__SERVICE__GRPC_PORT={grpc_port}\n"
        f"      - QDRANT__SERVICE__API_KEY=${{{key_env}}}\n"
    )
    return override


def _compose(project: str, override: Path, env: dict, *args: str, check: bool = True):
    return subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            project,
            "-f",
            str(COMPOSE_FILE),
            "-f",
            str(override),
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=check,
    )


def _status(url: str, method: str = "GET", headers: dict | None = None, body=None) -> int:
    data = None if body is None else json.dumps(body).encode()
    req = request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=5) as response:
            return response.status
    except error.HTTPError as exc:
        return exc.code


@pytest.fixture
def authenticated_qdrant(tmp_path):
    """Yield (url, key) for a disposable authenticated Qdrant from the repo Compose file."""
    key = secrets.token_urlsafe(32)
    project = "qdrant-auth-test-" + uuid.uuid4().hex[:12]
    http_port, grpc_port = _free_ports(2)
    override = _override(tmp_path, http_port, grpc_port, "QDRANT_API_KEY")
    env = {**os.environ, "QDRANT_API_KEY": key}
    url = f"http://127.0.0.1:{http_port}"
    try:
        _compose(project, override, env, "up", "-d", "--pull", "never", "qdrant")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if _status(url + "/readyz") == 200:
                    break
            except OSError:
                pass
            time.sleep(0.25)
        else:
            pytest.fail("disposable Qdrant did not become ready")
        yield url, key
    finally:
        _compose(project, override, env, "down", "-v", "--remove-orphans", check=False)


@requires_docker
def test_compose_refuses_to_render_without_key(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "QDRANT_API_KEY"}
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "config", "--quiet"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "QDRANT_API_KEY" in result.stderr


@requires_docker
def test_container_refuses_to_start_with_empty_key(tmp_path):
    project = "qdrant-auth-test-" + uuid.uuid4().hex[:12]
    http_port, grpc_port = _free_ports(2)
    # Compose renders (QDRANT_API_KEY is set) but the server receives an empty key.
    override = _override(tmp_path, http_port, grpc_port, "EMPTY_QDRANT_KEY")
    env = {**os.environ, "QDRANT_API_KEY": "render-only", "EMPTY_QDRANT_KEY": ""}
    try:
        _compose(project, override, env, "up", "-d", "--pull", "never", "qdrant")
        deadline = time.monotonic() + 30
        state = {}
        while time.monotonic() < deadline:
            ps = _compose(project, override, env, "ps", "--all", "--format", "json")
            rows = [json.loads(line) for line in ps.stdout.splitlines() if line.strip()]
            state = rows[0] if rows else {}
            if state.get("State") == "exited":
                break
            time.sleep(0.25)
        assert state.get("State") == "exited"
        assert state.get("ExitCode") == 64
        logs = _compose(project, override, env, "logs", "qdrant").stdout
        assert "refusing to start Qdrant unauthenticated" in logs
        assert _status_or_refused(f"http://127.0.0.1:{http_port}/collections")
    finally:
        _compose(project, override, env, "down", "-v", "--remove-orphans", check=False)


def _status_or_refused(url: str) -> bool:
    try:
        _status(url)
    except OSError:
        return True
    return False


@requires_docker
def test_anonymous_access_is_denied(authenticated_qdrant):
    url, _key = authenticated_qdrant
    synthetic = "synthetic-anon-" + uuid.uuid4().hex[:8]

    assert _status(url + "/readyz") == 200  # probe stays open for health checks
    assert _status(url + "/collections") == 401
    assert _status(url + "/collections", headers={"api-key": "wrong-key"}) == 401
    assert _status(url + "/telemetry") == 401
    assert _status(url + "/metrics") == 401
    create = {"vectors": {"size": 4, "distance": "Cosine"}}
    assert _status(url + f"/collections/{synthetic}", "PUT", body=create) == 401


@requires_docker
def test_clients_fail_closed_or_are_denied_without_key(authenticated_qdrant, monkeypatch):
    url, _key = authenticated_qdrant
    monkeypatch.delenv("QDRANT_API_KEY", raising=False)
    monkeypatch.delenv("QDRANT_REQUIRE_API_KEY", raising=False)

    # Default client configuration refuses to connect without a key.
    with pytest.raises(QdrantAuthConfigError):
        SemanticIndexer.__new__(SemanticIndexer)._init_qdrant_client(url)
    assert check_qdrant(url, timeout_s=5).details["failure_class"] == "qdrant_api_key_missing"

    # Even an explicitly unauthenticated client is rejected by the server.
    monkeypatch.setenv("QDRANT_REQUIRE_API_KEY", "false")
    with pytest.raises(RuntimeError):
        SemanticIndexer.__new__(SemanticIndexer)._init_qdrant_client(url)
    denied = check_qdrant(url, timeout_s=5)
    assert denied.status == ServiceStatus.UNREACHABLE
    assert denied.details["http_status"] == 401

    monkeypatch.setenv("QDRANT_API_KEY", "wrong-key")
    monkeypatch.delenv("QDRANT_REQUIRE_API_KEY", raising=False)
    assert check_qdrant(url, timeout_s=5).details["http_status"] == 401


@requires_docker
def test_authorized_client_reads_and_writes_synthetic_collection(authenticated_qdrant, monkeypatch):
    from qdrant_client import models

    url, key = authenticated_qdrant
    monkeypatch.setenv("QDRANT_API_KEY", key)
    monkeypatch.delenv("QDRANT_REQUIRE_API_KEY", raising=False)

    assert check_qdrant(url, timeout_s=5).status == ServiceStatus.READY

    indexer = SemanticIndexer.__new__(SemanticIndexer)
    client = indexer._init_qdrant_client(url)
    collection = "synthetic-auth-" + uuid.uuid4().hex[:8]
    try:
        client.create_collection(
            collection,
            vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE),
        )
        client.upsert(
            collection,
            points=[
                models.PointStruct(id=1, vector=[1.0, 0.0, 0.0, 0.0], payload={"t": "a"}),
                models.PointStruct(id=2, vector=[0.0, 1.0, 0.0, 0.0], payload={"t": "b"}),
            ],
            wait=True,
        )
        hits = client.query_points(collection, query=[1.0, 0.0, 0.0, 0.0], limit=1).points
        assert [hit.id for hit in hits] == [1]
        assert _status(url + "/collections") == 401  # still closed to anonymous callers
    finally:
        client.delete_collection(collection)
        client.close()
