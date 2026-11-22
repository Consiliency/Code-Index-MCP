#!/usr/bin/env python3
"""Authenticated end-to-end semantic search canary (Consiliency/Code-Index-MCP#113).

The canary checks that embedding-backed semantic search works through the MCP
server against an authenticated Qdrant:

1. Create a small synthetic git repository in a private temporary directory.
2. Start ``index-it-mcp stdio`` with a per-run ``canary-<128-bit id>`` semantic
   profile. Its vectors go to a dedicated per-run collection
   (``ci__<repo hash>__canary-<id>__<generation>``).
3. Register and reindex the repository, then require semantic readiness ``ready``.
4. Run opaque semantic queries that share no term with any fixture file, so the
   indexer's lexical overlap bonus cannot rank them. The expected file must be
   the top hit, with ``semantic_source == "semantic"`` and the canary
   collection, and Qdrant must hold a non-sentinel vector for each file.
5. Require that anonymous Qdrant access returns 401 before, during and after.
   An anonymous-access failure outranks every other failure code.
6. Write a receipt holding only the model id, dimension, collection name,
   timings and the pass/fail result. At the end, also on failure, delete only
   the collections this run created in its exact namespace.

Secrets come only from the environment: ``QDRANT_API_KEY`` and, optionally, the
embedder key named by ``--embedder-key-env``. They are never printed, never put
on argv and never written to the receipt. HTTP calls build their headers in
process. The server child gets a minimal environment, not the caller's.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import AsyncExitStack
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib import error, request

if __package__:
    from .v13_pilot_estimate import SYNTHETIC_CORPUS
else:
    from v13_pilot_estimate import SYNTHETIC_CORPUS

RECEIPT_SCHEMA = "semantic-canary-receipt.v1"
#: The only keys a receipt may carry.
RECEIPT_KEYS = frozenset(
    {"schema", "result", "failed_check", "model", "dimension", "collection", "timings_seconds"}
)
#: Synthetic, non-sensitive corpus shared with the v13 pilot, flattened into one repository.
CORPUS = {
    path: content for entries in SYNTHETIC_CORPUS.values() for path, content in entries.items()
}
#: (query, file that must be the top hit). Two queries, so a constant ranking fails.
#: No query term of three or more characters occurs in any fixture file's path or
#: content: ``SemanticIndexer._query_overlap_bonus`` matches such terms as
#: substrings, so only vector similarity can order these results.
QUERIES = (
    ("work out leftover money once outgoing payments leave an account", "balance.py"),
    ("look up an item inside an inventory listing by a unique identifier", "catalog.py"),
)
#: Bits of randomness in the per-run profile id (and so in the collection name).
RUN_ID_BYTES = 16
DEFAULT_EMBEDDER_KEY_ENV = "CANARY_EMBEDDER_API_KEY"
ANONYMOUS_PROBE_INTERVAL_SECONDS = 0.5
#: Variables passed through to the server child, if set. Locations, not credentials.
GRAMMAR_CACHE_ENV = "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"
PASSTHROUGH_ENV = (
    "PATH",
    GRAMMAR_CACHE_ENV,
    "TREE_SITTER_LANGUAGE_PACK_LIBS_DIR",
    "UV_CACHE_DIR",
)
_NO_PROXY = request.build_opener(request.ProxyHandler({}))


class CanaryFailure(Exception):
    """A failed check. ``code`` is a fixed identifier, never request or secret data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    """Return (status, decoded JSON or None). Proxies are bypassed; errors carry no data."""
    data = None if body is None else json.dumps(body).encode()
    req = request.Request(url, data=data, method=method, headers=dict(headers or {}))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _NO_PROXY.open(req, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def anonymous_status(qdrant_url: str) -> int:
    """Status of an unauthenticated ``GET /collections``; 0 if unreachable."""
    try:
        return http_json("GET", qdrant_url + "/collections", timeout=5)[0]
    except OSError:
        return 0


class AnonymousProbe:
    """Poll anonymous access in the background and record every status seen."""

    def __init__(self, qdrant_url: str, interval: float = ANONYMOUS_PROBE_INTERVAL_SECONDS):
        self._url = qdrant_url
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="canary-anon-probe", daemon=True)
        self.statuses: list[int] = []

    def _run(self) -> None:
        while not self._stop.is_set():
            self.statuses.append(anonymous_status(self._url))
            self._stop.wait(self._interval)

    def __enter__(self) -> "AnonymousProbe":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(10)
        self.statuses.append(anonymous_status(self._url))


def require_anonymous_denied(statuses: list[int], code: str) -> None:
    if not statuses or any(status != 401 for status in statuses):
        raise CanaryFailure(code)


def embedder_headers(key: str | None) -> dict[str, str]:
    return {"Authorization": "Bearer " + key} if key else {}


def probe_dimension(embedder_url: str, model: str, key: str | None) -> int:
    status, value = http_json(
        "POST",
        embedder_url.rstrip("/") + "/embeddings",
        headers=embedder_headers(key),
        body={"model": model, "input": ["semantic canary dimension probe"]},
        timeout=120,
    )
    try:
        vector = value["data"][0]["embedding"]
    except TypeError, KeyError, IndexError:
        vector = None
    if status != 200 or not isinstance(vector, list) or not vector:
        raise CanaryFailure("embedder_dimension_probe_failed")
    return len(vector)


def chunker_version() -> str:
    try:
        return f"treesitter-chunker@{version('treesitter-chunker')}"
    except PackageNotFoundError:
        return "treesitter-chunker@not-installed"


def build_profile(
    *,
    model: str,
    dimension: int,
    embedder_url: str,
    enrichment_url: str,
    enrichment_model: str,
    key_env: str | None,
) -> dict:
    """Semantic profile; it names the key's environment variable, never the key."""
    metadata = {
        "embedding_api_base": embedder_url,
        "openai_api_base": embedder_url,
        "embedding_model_name": model,
        "enrichment_api_base": enrichment_url,
        "enrichment_model_name": enrichment_model,
    }
    if key_env:
        metadata.update(
            embedding_api_key_env=key_env,
            openai_api_key_env=key_env,
            enrichment_api_key_env=key_env,
        )
    return {
        "provider": "openai_compatible",
        "model_name": model,
        "model_version": "unreported",
        "vector_dimension": dimension,
        "distance_metric": "cosine",
        "normalization_policy": "provider-default",
        "chunk_schema_version": "1",
        "chunker_version": chunker_version(),
        "build_metadata": metadata,
    }


def git_env(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(root / "home"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


def server_env(
    root: Path,
    *,
    qdrant_url: str,
    qdrant_key: str,
    profile_id: str,
    profile: dict,
    key_env: str | None,
    embedder_key: str | None,
) -> dict[str, str]:
    """Minimal synthetic environment for the server child; no caller variables leak in."""
    env = {key: os.environ[key] for key in PASSTHROUGH_ENV if os.environ.get(key)}
    if GRAMMAR_CACHE_ENV not in env:
        # The child gets a private HOME, so point it at the caller's grammar cache
        # (a location, not a credential) instead of a cold one.
        from mcp_server.utils.grammar_cache import resolve_cache_root

        cache_root = resolve_cache_root()
        if cache_root is not None:
            env[GRAMMAR_CACHE_ENV] = str(cache_root)
    env.update(
        git_env(root),
        XDG_CONFIG_HOME=str(root / "home" / ".config"),
        MCP_REPO_REGISTRY=str(root / "registry.json"),
        MCP_INDEX_STORAGE_PATH=str(root / "indexes"),
        MCP_ALLOWED_ROOTS=str(root),
        MCP_WORKSPACE_ROOT=str(root / "repo"),
        MCP_AUTO_INDEX="false",
        MCP_METRICS_PORT="0",
        MCP_SKIP_PLUGIN_PREINDEX="true",
        MCP_ENVIRONMENT="development",
        MCP_DEPLOYMENT_PROFILE="fleet_local",
        # Never read a checkout or home .env into the canary.
        PYTHON_DOTENV_DISABLED="1",
        RERANKER_TYPE="none",
        SEMANTIC_SEARCH_ENABLED="true",
        SEMANTIC_AUTOSTART_QDRANT="false",
        SEMANTIC_PROFILES_JSON=json.dumps({profile_id: profile}),
        SEMANTIC_DEFAULT_PROFILE=profile_id,
        QDRANT_URL=qdrant_url,
        QDRANT_USE_SERVER="true",
        QDRANT_API_KEY=qdrant_key,
    )
    if key_env and embedder_key:
        env[key_env] = embedder_key
    return env


def run_quiet(argv: list[str], *, cwd: Path, env: dict[str, str], code: str) -> None:
    try:
        subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            check=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError, subprocess.SubprocessError:
        raise CanaryFailure(code) from None


def create_repository(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (root / "home").mkdir()
    for name, content in CORPUS.items():
        (repo / name).write_text(content)
    (repo / ".gitignore").write_text(".mcp-index/\n")
    env = git_env(root)
    run_quiet(["git", "init", "-q", "-b", "main", str(repo)], cwd=root, env=env, code="git_init")
    run_quiet(["git", "-C", str(repo), "add", "."], cwd=root, env=env, code="git_add")
    run_quiet(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Semantic Canary",
            "-c",
            "user.email=canary@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "synthetic canary fixture",
        ],
        cwd=root,
        env=env,
        code="git_commit",
    )
    return repo


def entrypoint_argv(entrypoint: str | None) -> list[str]:
    if entrypoint:
        return [entrypoint]
    found = shutil.which("index-it-mcp")
    return [found] if found else [sys.executable, "-m", "mcp_server.cli"]


def payload(result: Any) -> Any:
    text = "".join(block.text for block in result.content if hasattr(block, "text"))
    try:
        return json.loads(text)
    except ValueError:
        raise CanaryFailure("tool_response_not_json") from None


def find_status_row(status: Any) -> dict:
    """The canary's private registry holds exactly one repository."""
    rows = status.get("repositories", []) if isinstance(status, dict) else []
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise CanaryFailure("status_row_missing")
    return rows[0]


def check_search(value: Any, expected_file: str, profile_id: str) -> str:
    """Validate one semantic response; return the collection it came from."""
    if not isinstance(value, dict) or value.get("code") or value.get("error"):
        raise CanaryFailure("semantic_query_refused")
    results = value.get("results") or []
    if not results:
        raise CanaryFailure("semantic_query_empty")
    top = results[0]
    if Path(str(top.get("file") or "")).name != expected_file:
        raise CanaryFailure("semantic_top_hit_mismatch")
    collection = top.get("semantic_collection_name")
    if (
        value.get("semantic_source") != "semantic"
        or top.get("semantic_source") != "semantic"
        or top.get("semantic_profile_id") != profile_id
        or value.get("semantic_profile_id") != profile_id
        or not isinstance(collection, str)
        or value.get("semantic_collection_name") != collection
    ):
        raise CanaryFailure("semantic_linkage_mismatch")
    return collection


def overlap_terms(query: str, texts: list[str]) -> set[str]:
    """Query terms the indexer's lexical bonus would match in ``texts``."""
    haystack = " ".join(texts).lower()
    terms = {token for token in re.findall(r"[a-z0-9_]+", query.lower()) if len(token) >= 3}
    return {term for term in terms if term in haystack}


def qdrant_headers(key: str) -> dict[str, str]:
    return {"api-key": key}


def collection_shape(qdrant_url: str, key: str, collection: str) -> tuple[int, int]:
    """Return (vector size, point count) of a collection, read with the key."""
    status, value = http_json(
        "GET", f"{qdrant_url}/collections/{collection}", headers=qdrant_headers(key)
    )
    try:
        result = value["result"]
        vectors = result["config"]["params"]["vectors"]
        size = vectors["size"] if "size" in vectors else next(iter(vectors.values()))["size"]
        return int(size), int(result.get("points_count") or 0)
    except TypeError, KeyError, StopIteration, ValueError:
        raise CanaryFailure(
            "collection_missing" if status == 404 else "collection_unreadable"
        ) from None


def require_file_vectors(qdrant_url: str, key: str, collection: str, files: list[str]) -> None:
    """Require a non-sentinel point for every file (the provenance point does not count)."""
    seen: set[str] = set()
    offset: Any = None
    for _ in range(100):
        body: dict[str, Any] = {"limit": 256, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        status, value = http_json(
            "POST",
            f"{qdrant_url}/collections/{collection}/points/scroll",
            headers=qdrant_headers(key),
            body=body,
        )
        if status != 200 or not isinstance(value, dict):
            raise CanaryFailure("collection_scroll_failed")
        result = value.get("result") or {}
        for point in result.get("points") or []:
            data = point.get("payload") or {}
            if data.get("__provenance__"):
                continue
            path = next(
                (
                    data.get(field)
                    for field in ("relative_path", "file", "filepath", "path", "file_path")
                    if data.get(field)
                ),
                "",
            )
            seen.add(Path(str(path)).name)
        offset = result.get("next_page_offset")
        if offset is None:
            break
    if not set(files) <= seen:
        raise CanaryFailure("file_vectors_missing")


def list_collections(qdrant_url: str, key: str) -> set[str]:
    status, value = http_json("GET", qdrant_url + "/collections", headers=qdrant_headers(key))
    if status != 200 or not isinstance(value, dict):
        raise CanaryFailure("collection_list_failed")
    rows = (value.get("result") or {}).get("collections") or []
    return {
        row["name"] for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)
    }


def namespace_prefix(repo_id: str, profile_id: str) -> str:
    """Exact collection prefix for this repository and profile (the registry's scheme)."""
    from mcp_server.artifacts.semantic_namespace import SemanticNamespaceResolver

    resolver = SemanticNamespaceResolver()
    return f"ci__{resolver.derive_repo_hash(repo_id)}__{resolver.sanitize_profile_id(profile_id)}__"


def owned_collections(before: set[str], after: set[str], prefix: str) -> list[str]:
    """Collections this run created in its own namespace; nothing else is ever owned."""
    return sorted(name for name in after - before if name.startswith(prefix))


def delete_owned_collections(qdrant_url: str, key: str, before: set[str], prefix: str) -> bool:
    """Delete only collections created by this run. True when none of them remain."""
    try:
        owned = owned_collections(before, list_collections(qdrant_url, key), prefix)
        for name in owned:
            http_json("DELETE", f"{qdrant_url}/collections/{name}", headers=qdrant_headers(key))
        return not set(owned) & list_collections(qdrant_url, key)
    except OSError, CanaryFailure:
        return False


def anonymous_verdict(during: list[int] | None, after: int) -> str | None:
    """Failure code for the during/after anonymous checks, or None when all were 401.

    ``during`` is None when the server never started (nothing to observe).
    """
    if during is not None and (not during or any(status != 401 for status in during)):
        return "anonymous_access_during"
    if after != 401:
        return "anonymous_access_after"
    return None


def read_repository_id(root: Path) -> str:
    """The single repository id in the canary's private registry."""
    try:
        registry = json.loads((root / "registry.json").read_text())
    except OSError, ValueError:
        raise CanaryFailure("registry_unreadable") from None
    if not isinstance(registry, dict) or len(registry) != 1:
        raise CanaryFailure("registry_unreadable")
    return next(iter(registry))


async def run_session(
    argv: list[str],
    env: dict[str, str],
    root: Path,
    repo: Path,
    profile_id: str,
    timings: dict[str, float],
    *,
    timeout: float,
) -> str:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=argv[0], args=[*argv[1:], "stdio"], env=env, cwd=root)
    # Server logs stay in the private run directory, which is removed afterwards.
    with open(root / "server.log", "w") as errlog:
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
            client = await stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=timeout)
            )
            await client.initialize()

            started = time.monotonic()
            rebuilt = payload(await client.call_tool("reindex", {"repository": str(repo)}))
            timings["reindex"] = round(time.monotonic() - started, 3)
            if not isinstance(rebuilt, dict) or rebuilt.get("error"):
                raise CanaryFailure("reindex_failed")

            row = find_status_row(payload(await client.call_tool("get_status", {})))
            if row.get("semantic_readiness") != "ready" or row.get("semantic_ready") is not True:
                raise CanaryFailure("semantic_not_ready")
            readiness = (row.get("features") or {}).get("semantic", {}).get("readiness") or {}
            ready_collection = readiness.get("collection_name")

            collections = set()
            started = time.monotonic()
            for query, expected in QUERIES:
                value = payload(
                    await client.call_tool(
                        "search_code",
                        {"query": query, "repository": str(repo), "semantic": True, "limit": 5},
                    )
                )
                collections.add(check_search(value, expected, profile_id))
            timings["semantic_queries"] = round(time.monotonic() - started, 3)
            if len(collections) != 1 or (ready_collection and ready_collection not in collections):
                raise CanaryFailure("semantic_collection_mismatch")
            return collections.pop()


def failure_code(exc: BaseException) -> str:
    """Fixed code for a failure; unwraps the task groups the MCP client raises."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = next(
            (
                leaf
                for leaf in exc.exceptions
                if isinstance(leaf, (CanaryFailure, BaseExceptionGroup))
            ),
            exc.exceptions[0],
        )
    if isinstance(exc, CanaryFailure):
        return exc.code
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "unexpected_" + type(exc).__name__


def write_receipt(path: Path, receipt: dict) -> None:
    if set(receipt) - RECEIPT_KEYS:
        raise ValueError("receipt carries unexpected keys")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def run_canary(args: argparse.Namespace) -> dict:
    """Run the canary and return its receipt. Never raises for a failed check."""
    qdrant_url = args.qdrant_url.rstrip("/")
    embedder_url = args.embedder_url.rstrip("/")
    enrichment_url = (args.enrichment_url or args.embedder_url).rstrip("/")
    qdrant_key = os.environ.get("QDRANT_API_KEY") or ""
    key_env = args.embedder_key_env or None
    embedder_key = os.environ.get(key_env) if key_env else None
    # Per-run profile id; its sanitized form is the collection's profile segment.
    profile_id = f"canary-{secrets.token_hex(RUN_ID_BYTES)}"
    timings: dict[str, float] = {}
    receipt: dict = {
        "schema": RECEIPT_SCHEMA,
        "result": "fail",
        "failed_check": None,
        "model": args.model,
        "dimension": args.dimension,
        "collection": None,
        "timings_seconds": timings,
    }
    started = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix="semantic-canary-"))
    passed = False
    denied_before = False
    probe: AnonymousProbe | None = None
    before: set[str] | None = None
    prefix: str | None = None
    try:
        if not qdrant_key:
            raise CanaryFailure("qdrant_api_key_missing")
        require_anonymous_denied([anonymous_status(qdrant_url)], "anonymous_access_before")
        denied_before = True
        status, _ = http_json(
            "GET", qdrant_url + "/collections", headers=qdrant_headers(qdrant_key)
        )
        if status != 200:
            raise CanaryFailure("authenticated_access_failed")

        dimension = probe_dimension(embedder_url, args.model, embedder_key)
        if args.dimension is not None and dimension != args.dimension:
            raise CanaryFailure("embedder_dimension_mismatch")
        receipt["dimension"] = dimension

        repo = create_repository(root)
        env = server_env(
            root,
            qdrant_url=qdrant_url,
            qdrant_key=qdrant_key,
            profile_id=profile_id,
            profile=build_profile(
                model=args.model,
                dimension=dimension,
                embedder_url=embedder_url,
                enrichment_url=enrichment_url,
                enrichment_model=args.enrichment_model,
                key_env=key_env if embedder_key else None,
            ),
            key_env=key_env,
            embedder_key=embedder_key,
        )
        argv = entrypoint_argv(args.entrypoint)
        run_quiet(
            [*argv, "repository", "register", str(repo), "--no-auto-sync", "--no-artifacts"],
            cwd=root,
            env=env,
            code="register_failed",
        )
        run_prefix = namespace_prefix(read_repository_id(root), profile_id)
        # Snapshot before the server starts: cleanup only ever deletes collections
        # that appear after this point inside this run's exact namespace.
        before = list_collections(qdrant_url, qdrant_key)
        prefix = run_prefix

        probe = AnonymousProbe(qdrant_url)
        with probe:
            collection = asyncio.run(
                asyncio.wait_for(
                    run_session(
                        argv, env, root, repo, profile_id, timings, timeout=args.tool_timeout
                    ),
                    timeout=args.timeout,
                )
            )
        if not collection.startswith(prefix) or collection in before:
            raise CanaryFailure("collection_not_dedicated")
        size, _points = collection_shape(qdrant_url, qdrant_key, collection)
        if size != dimension:
            raise CanaryFailure("collection_shape_mismatch")
        require_file_vectors(qdrant_url, qdrant_key, collection, sorted(CORPUS))
        receipt["collection"] = collection
        passed = True
    except Exception as exc:  # noqa: BLE001 - report a fixed code, never a message
        receipt["failed_check"] = failure_code(exc)
    finally:
        if before is not None and prefix is not None:
            if not delete_owned_collections(qdrant_url, qdrant_key, before, prefix):
                receipt["failed_check"] = receipt["failed_check"] or "collection_cleanup_failed"
        if denied_before:
            # Evaluated on every path; an authentication failure outranks the rest.
            verdict = anonymous_verdict(
                probe.statuses if probe is not None else None, anonymous_status(qdrant_url)
            )
            if verdict:
                receipt["failed_check"] = verdict
        try:
            shutil.rmtree(root)
        except OSError:
            receipt["failed_check"] = receipt["failed_check"] or "temporary_cleanup_failed"
        receipt["result"] = "pass" if passed and receipt["failed_check"] is None else "fail"
        timings["total"] = round(time.monotonic() - started, 3)
    return receipt


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Authenticated end-to-end semantic canary. Reads QDRANT_API_KEY and the "
            "optional embedder key from the environment only."
        )
    )
    parser.add_argument("--qdrant-url", required=True, help="Authenticated Qdrant base URL")
    parser.add_argument(
        "--embedder-url", required=True, help="OpenAI-compatible base URL ending in /v1"
    )
    parser.add_argument("--model", required=True, help="Embedding model id")
    parser.add_argument(
        "--dimension", type=int, help="Expected vector dimension (probed when omitted)"
    )
    parser.add_argument(
        "--enrichment-url", help="OpenAI-compatible summary base URL (default: --embedder-url)"
    )
    parser.add_argument("--enrichment-model", default="chat", help="Summary model id")
    parser.add_argument(
        "--embedder-key-env",
        default=DEFAULT_EMBEDDER_KEY_ENV,
        help="Name of the environment variable holding the embedder/summary key (optional)",
    )
    parser.add_argument("--receipt", type=Path, help="Write the receipt JSON here")
    parser.add_argument("--entrypoint", help="index-it-mcp executable (default: from PATH)")
    parser.add_argument("--timeout", type=float, default=900.0, help="Overall MCP session limit")
    parser.add_argument("--tool-timeout", type=float, default=600.0, help="Per-tool-call limit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    receipt = run_canary(args)
    if args.receipt:
        write_receipt(args.receipt, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
