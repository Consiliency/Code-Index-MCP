"""Tier A wiring check for the semantic canary (Consiliency/Code-Index-MCP#113).

Runs ``scripts/semantic_canary.py`` end to end against a disposable authenticated
Qdrant (the ``authenticated_qdrant`` fixture from ``test_qdrant_auth_container``,
with its per-run key and skip-if-no-Docker behaviour) and a loopback embedder.

The loopback embedder maps each fixture file and its canary query to the same
basis vector, and different files to orthogonal ones. The canary queries share
no lexical term with the fixture, so only vector similarity can rank them: the
pass case needs the right vectors, and the swapped case (each query pointed at
the other file's vector) must fail the ranking check.

This proves the wiring: profile, MCP stdio server, reindex, readiness, vector
ranking through the server, collection linkage, anonymous denial, receipt and
cleanup. It is not acceptance evidence: no real model is involved. Tier B (real
embedder) is documented in ``docs/operations/semantic-canary.md``.

Set ``SEMANTIC_CANARY_REQUIRE_DOCKER=1`` (CI does) to fail instead of skipping
when Docker or the pinned Qdrant image is missing.
"""

# Test parameters reuse the imported ``authenticated_qdrant`` fixture by name.
# ruff: noqa: F811

from __future__ import annotations

import json
import os
import secrets
import threading
import uuid
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts import semantic_canary as canary
from tests.integration.test_qdrant_auth_container import (  # noqa: F401 - pytest fixture
    _status,
    authenticated_qdrant,
    requires_docker,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_network]

if os.environ.get("SEMANTIC_CANARY_REQUIRE_DOCKER") == "1":
    requires_docker = pytest.mark.usefixtures()  # never skip; a missing Docker fails

DIMENSION = 8
MODEL = "synthetic-loopback-embedder"
SUMMARY = "This function processes synthetic records. It returns the computed result."
#: A string that occurs only in each file's own source, used to recognise its chunks.
FILE_MARKERS = {"balance.py": "available_balance", "catalog.py": "find_product"}
FILES = sorted(FILE_MARKERS)


def basis(index: int) -> list[float]:
    vector = [0.0] * DIMENSION
    vector[index] = 1.0
    return vector


def orthogonal_embedding(text: str, *, swap_queries: bool = False) -> list[float]:
    """Each file and its query share a basis vector; anything else gets its own."""
    for query, expected in canary.QUERIES:
        if query in text:
            target = FILES.index(expected)
            return basis((1 - target) if swap_queries else target)
    for index, name in enumerate(FILES):
        if FILE_MARKERS[name] in text:
            return basis(index)
    return basis(len(FILES))


def loopback_provider(token: str, *, mode: str = "ok") -> ThreadingHTTPServer:
    """OpenAI-compatible embeddings and summaries on loopback; requires ``token``.

    Same protocol shape as ``scripts/v13_pmcp_pilot.py`` ``rehearsal_provider``,
    which returns one constant vector for every input (and sleeps on indexing
    input), so it cannot tell files apart. ``mode`` is ``ok``, ``swap`` (each
    query gets the other file's vector) or ``refuse`` (query embeddings get 400
    after indexing succeeds).
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            if self.headers.get("Authorization") == "Bearer " + token:
                return True
            self.respond({"error": "unauthorized"}, status=401)
            return False

        def do_GET(self):
            if self.authorized():
                self.respond({"object": "list", "data": [{"id": MODEL}, {"id": "chat"}]})

        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if not self.authorized():
                return
            if self.path.endswith("/embeddings"):
                inputs = value["input"]
                if isinstance(inputs, str):
                    inputs = [inputs]
                if mode == "refuse" and any(
                    query in text for text in inputs for query, _ in canary.QUERIES
                ):
                    self.respond({"error": "synthetic query refusal"}, status=400)
                    return
                self.respond(
                    {
                        "object": "list",
                        "model": MODEL,
                        "data": [
                            {
                                "object": "embedding",
                                "index": i,
                                "embedding": orthogonal_embedding(
                                    text, swap_queries=mode == "swap"
                                ),
                            }
                            for i, text in enumerate(inputs)
                        ],
                        "usage": {"prompt_tokens": 1, "total_tokens": 1},
                    }
                )
                return
            prompt = "\n".join(str(item.get("content", "")) for item in value["messages"])
            ids = [
                line.removeprefix("chunk_id: ").strip()
                for line in prompt.splitlines()
                if line.startswith("chunk_id: ")
            ]
            content = (
                json.dumps({"summaries": [{"chunk_id": key, "summary": SUMMARY} for key in ids]})
                if ids
                else SUMMARY
            )
            self.respond(
                {
                    "id": "loopback",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "chat",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


@pytest.fixture
def loopback_embedder(request):
    token = secrets.token_urlsafe(24)
    server = loopback_provider(token, mode=getattr(request, "param", "ok"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.fixture
def canary_env(authenticated_qdrant, loopback_embedder, monkeypatch):
    """Keys in the environment, plus a decoy collection the canary must never delete."""
    url, key = authenticated_qdrant
    embedder_url, token = loopback_embedder
    monkeypatch.setenv("QDRANT_API_KEY", key)
    monkeypatch.setenv(canary.DEFAULT_EMBEDDER_KEY_ENV, token)
    # Same shape and profile prefix as a canary collection, but not this run's.
    decoy = f"ci__{uuid.uuid4().hex[:12]}__canary-{uuid.uuid4().hex}__gen"
    status, _ = canary.http_json(
        "PUT",
        f"{url}/collections/{decoy}",
        headers={"api-key": key},
        body={"vectors": {"size": DIMENSION, "distance": "Cosine"}},
    )
    assert status == 200
    yield url, key, embedder_url, token, decoy


def _run(qdrant_url, embedder_url):
    return canary.run_canary(
        Namespace(
            qdrant_url=qdrant_url,
            embedder_url=embedder_url,
            enrichment_url=None,
            enrichment_model="chat",
            model=MODEL,
            dimension=DIMENSION,
            embedder_key_env=canary.DEFAULT_EMBEDDER_KEY_ENV,
            entrypoint=None,
            timeout=600.0,
            tool_timeout=300.0,
        )
    )


def _assert_after(receipt, url, key, token, decoy):
    """Secret-free receipt, only the decoy left, anonymous access still denied."""
    assert set(receipt) <= canary.RECEIPT_KEYS
    text = json.dumps(receipt)
    assert key not in text and token not in text
    assert "127.0.0.1" not in text and "http" not in text
    assert canary.list_collections(url, key) == {decoy}
    assert _status(url + "/collections") == 401


def test_orthogonal_embedder_is_unambiguous():
    for query, expected in canary.QUERIES:
        assert FILE_MARKERS[expected] in canary.CORPUS[expected]
        assert orthogonal_embedding(query) == basis(FILES.index(expected))
        assert orthogonal_embedding(query, swap_queries=True) != basis(FILES.index(expected))


@requires_docker
def test_canary_passes_end_to_end(canary_env):
    url, key, embedder_url, token, decoy = canary_env

    receipt = _run(url, embedder_url)

    assert receipt["result"] == "pass", receipt["failed_check"]
    assert receipt["failed_check"] is None
    assert receipt["model"] == MODEL
    assert receipt["dimension"] == DIMENSION
    assert receipt["collection"].startswith("ci__")
    assert "__canary-" in receipt["collection"]
    assert {"reindex", "semantic_queries", "total"} <= set(receipt["timings_seconds"])
    _assert_after(receipt, url, key, token, decoy)


@requires_docker
@pytest.mark.parametrize("loopback_embedder", ["swap"], indirect=True)
def test_canary_fails_when_query_vectors_point_at_the_wrong_file(canary_env):
    url, key, embedder_url, token, decoy = canary_env

    receipt = _run(url, embedder_url)

    assert receipt["result"] == "fail"
    assert receipt["failed_check"] == "semantic_top_hit_mismatch"
    _assert_after(receipt, url, key, token, decoy)


@requires_docker
@pytest.mark.parametrize("loopback_embedder", ["refuse"], indirect=True)
def test_canary_fails_when_queries_fail_and_still_cleans_up(canary_env):
    url, key, embedder_url, token, decoy = canary_env

    receipt = _run(url, embedder_url)

    assert receipt["result"] == "fail"
    assert receipt["failed_check"] == "semantic_query_refused"
    _assert_after(receipt, url, key, token, decoy)
