"""Tier A wiring check for the semantic canary (Consiliency/Code-Index-MCP#113).

Runs ``scripts/semantic_canary.py`` end to end against a disposable authenticated
Qdrant (the ``authenticated_qdrant`` fixture from ``test_qdrant_auth_container``,
with its per-run key and skip-if-no-Docker behaviour) and a loopback embedder.

This proves the wiring: profile, MCP stdio server, reindex, readiness, ranking
plumbing, collection linkage, anonymous denial, receipt and cleanup. It is not
acceptance evidence: the loopback embedder is a deterministic hashed
bag-of-words, not a real model. Tier B (real embedder) is documented in
``docs/operations/semantic-canary.md``.
"""

# Test parameters reuse the imported ``authenticated_qdrant`` fixture by name.
# ruff: noqa: F811

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import threading
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

DIMENSION = 256
MODEL = "synthetic-loopback-embedder"
SUMMARY = "This function processes synthetic records. It returns the computed result."


def hashed_embedding(text: str) -> list[float]:
    """Deterministic, L2-normalized bag-of-words vector."""
    vector = [0.0] * DIMENSION
    for token in re.findall(r"[a-z]+", text.lower()):
        vector[int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "big") % DIMENSION] += 1
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def loopback_provider(token: str, *, refuse_queries: bool = False) -> ThreadingHTTPServer:
    """OpenAI-compatible embeddings and summaries on loopback; requires ``token``.

    Same protocol shape as ``scripts/v13_pmcp_pilot.py`` ``rehearsal_provider``,
    which returns one constant vector for every input (and sleeps on indexing
    input). That cannot tell files apart, so this fixture embeds by content.
    ``refuse_queries`` answers 400 to query embeddings after indexing succeeds.
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
                if refuse_queries and any(
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
                                "embedding": hashed_embedding(text),
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
    server = loopback_provider(token, refuse_queries=getattr(request, "param", False))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


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


def _assert_secret_free(receipt, *secrets_):
    assert set(receipt) <= canary.RECEIPT_KEYS
    text = json.dumps(receipt)
    for value in secrets_:
        assert value not in text
    assert "127.0.0.1" not in text and "http" not in text


def _remaining_canary_collections(url, key):
    status, value = canary.http_json("GET", url + "/collections", headers={"api-key": key})
    assert status == 200
    return [row["name"] for row in value["result"]["collections"] if "__canary-" in row["name"]]


@requires_docker
def test_canary_passes_end_to_end(authenticated_qdrant, loopback_embedder, monkeypatch):
    url, key = authenticated_qdrant
    embedder_url, token = loopback_embedder
    monkeypatch.setenv("QDRANT_API_KEY", key)
    monkeypatch.setenv(canary.DEFAULT_EMBEDDER_KEY_ENV, token)

    receipt = _run(url, embedder_url)

    assert receipt["result"] == "pass", receipt["failed_check"]
    assert receipt["failed_check"] is None
    assert receipt["model"] == MODEL
    assert receipt["dimension"] == DIMENSION
    assert receipt["collection"].startswith("ci__")
    assert "__canary-" in receipt["collection"]
    assert {"reindex", "semantic_queries", "total"} <= set(receipt["timings_seconds"])
    _assert_secret_free(receipt, key, token)
    assert _remaining_canary_collections(url, key) == []
    assert _status(url + "/collections") == 401


@requires_docker
@pytest.mark.parametrize("loopback_embedder", [True], indirect=True)
def test_canary_fails_when_queries_fail_and_still_cleans_up(
    authenticated_qdrant, loopback_embedder, monkeypatch
):
    url, key = authenticated_qdrant
    embedder_url, token = loopback_embedder
    monkeypatch.setenv("QDRANT_API_KEY", key)
    monkeypatch.setenv(canary.DEFAULT_EMBEDDER_KEY_ENV, token)

    receipt = _run(url, embedder_url)

    assert receipt["result"] == "fail"
    assert receipt["failed_check"] == "semantic_query_refused"
    _assert_secret_free(receipt, key, token)
    assert _remaining_canary_collections(url, key) == []
