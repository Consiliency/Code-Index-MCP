# Semantic search canary

Tracking: Consiliency/Code-Index-MCP#113 (plan item C5 of #121).

`scripts/semantic_canary.py` is a small authenticated end-to-end check that
embedding-backed semantic search works through the MCP server. It uses only
synthetic data and a dedicated per-run collection.

## What it does

1. Creates a two-file synthetic git repository (the v13 pilot corpus,
   `balance.py` and `catalog.py`) in a private temporary directory.
2. Starts `index-it-mcp stdio` with a minimal environment: a per-run
   `canary-<id>` profile (`<id>` is 128 random bits) in `SEMANTIC_PROFILES_JSON`, a private registry and
   index directory, `SEMANTIC_AUTOSTART_QDRANT=false` and
   `PYTHON_DOTENV_DISABLED=1`. No caller variables leak into the server, apart
   from `PATH` and the tree-sitter grammar cache locations.
3. Registers and reindexes the repository and requires `get_status` to report
   semantic readiness `ready`.
4. Runs two opaque semantic queries. No query term of three or more characters
   occurs in any fixture path or source, so the indexer's lexical overlap bonus
   (`SemanticIndexer._query_overlap_bonus`, a substring match) is the same for
   every result and only vector similarity orders them. Each expected file must be
   the top hit, with `semantic_source == "semantic"`, the canary profile id and
   one collection that matches readiness. The collection must sit in this run's
   exact namespace (`ci__<repo hash>__canary-<id>__<generation>`, the registry's
   naming scheme), have the probed dimension, and hold a non-sentinel vector for
   each file (read back with an authenticated scroll; the provenance point does
   not count).
5. Requires anonymous `GET /collections` to return 401 before the run, every 0.5 s
   while the server runs, and after cleanup. These checks are evaluated on every
   path, and an anonymous-access failure outranks any other failure code.
6. Snapshots the collection list before the server starts. At the end, also on
   failure, deletes only collections that appeared after the snapshot and start
   with this run's exact `ci__<repo hash>__canary-<id>__` prefix. Nothing else is
   ever deleted. Then removes the temporary directory; a failure to remove it
   fails the run with `temporary_cleanup_failed`.

The receipt carries only `schema`, `result` (`pass`/`fail`), `failed_check` (a
fixed code such as `semantic_top_hit_mismatch`), `model`, `dimension`,
`collection` and `timings_seconds`. It never contains environment values, keys,
URLs or hostnames. The exit status is 0 on pass and 1 on fail.

## Secrets

- `QDRANT_API_KEY` is read from the environment only. It is required; without it
  the canary fails with `qdrant_api_key_missing` before any request.
- The embedder and summary key is optional. It is read from the variable named by
  `--embedder-key-env` (default `CANARY_EMBEDDER_API_KEY`) and passed to the
  server under that name; the profile names the variable, never the value.
- Keys never appear on argv. HTTP headers are built in process, and server logs
  go to the private temporary directory, which is deleted at the end.

## Options

| Option | Meaning |
|---|---|
| `--qdrant-url` | Authenticated Qdrant base URL (required) |
| `--embedder-url` | OpenAI-compatible embedding base URL ending in `/v1` (required) |
| `--model` | Embedding model id (required) |
| `--dimension` | Expected vector dimension. The canary always probes the embedder and fails on a mismatch |
| `--enrichment-url` | OpenAI-compatible summary base URL (default: `--embedder-url`). Semantic indexing requires authoritative summaries |
| `--enrichment-model` | Summary model id (default `chat`) |
| `--embedder-key-env` | Name of the variable holding the embedder/summary key |
| `--receipt PATH` | Write the receipt JSON to `PATH` |
| `--entrypoint` | `index-it-mcp` executable (default: from `PATH`, else `python -m mcp_server.cli`) |

## Tier A: CI wiring (not acceptance)

`tests/integration/test_semantic_canary_container.py` (marker `integration`)
starts a disposable authenticated Qdrant with the `authenticated_qdrant` fixture
from `tests/integration/test_qdrant_auth_container.py` (repository Compose file,
per-run random key, random loopback ports, tmpfs storage). It also starts a
loopback OpenAI-compatible embedder that requires a per-run bearer token. The
embedder maps each fixture file and its query to the same basis vector and the
two files to orthogonal ones, so only vector similarity can rank the opaque
queries. Each case also creates a decoy collection shaped like a canary
collection, which must survive. The cases:

- pass: the canary passes end to end;
- swapped query vectors (each query points at the other file): the canary must
  fail with `semantic_top_hit_mismatch`;
- query embeddings refused after indexing: the canary must fail with
  `semantic_query_refused`.

After every case the receipt must be secret-free, only the decoy may remain and
anonymous access must still return 401.

This proves the wiring, including that ranking through the server follows the
vectors. It does not prove retrieval quality or that a real model, the `ai`
endpoints or a deployed Qdrant work.

CI runs it in the `Integration - Semantic Canary (Tier A)` job of
`ci-cd-pipeline.yml`: Ubuntu, the Qdrant image pulled by digest, serial, with a
30-minute job limit. It is a separate job and neither waits on nor gates any
other job. `SEMANTIC_CANARY_REQUIRE_DOCKER=1` makes a missing Docker or image a
failure there instead of a skip. The offline unit tests
(`tests/test_semantic_canary.py`) run in the protected agent gate.

Locally, the test skips when Docker Compose or the pinned image is missing:

```bash
docker pull qdrant/qdrant:v1.17.0@sha256:f1c7272cdac52b38c1a0e89313922d940ba50afd90d593a1605dbbc214e66ffb
uv run --extra dev --extra semantic pytest tests/integration/test_semantic_canary_container.py -m integration --no-cov
```

## Tier B: acceptance (operator run)

Run against the real embedder on `ai` and the authenticated Qdrant deployment,
from a host that can reach both (GitHub-hosted runners cannot reach `ai`). It
needs owner approval of the embedding provider and budget (#113 prerequisites).
The run embeds and summarizes two small files.

Keep only 1Password references in the env file, never values:

```bash
umask 077
cat > semantic-canary.env <<'EOF'
QDRANT_API_KEY=op://<vault>/<qdrant item>/<field>
# Only if the ai endpoints require a key:
CANARY_EMBEDDER_API_KEY=op://<vault>/<embedder item>/<field>
EOF

op run --env-file=semantic-canary.env -- \
  uv run --extra semantic python scripts/semantic_canary.py \
    --qdrant-url <authenticated Qdrant URL> \
    --embedder-url http://ai:8001/v1 \
    --model Qwen/Qwen3-Embedding-8B \
    --enrichment-url http://ai:8002/v1 \
    --enrichment-model chat \
    --receipt <evidence dir>/semantic-canary-receipt.json
```

Check the model id against `GET http://ai:8001/v1/models` first; pass
`--dimension` when the expected dimension is known. Acceptance is a receipt with
`"result": "pass"`. Afterwards, confirm with a GET-only, authenticated
`GET /collections` that the run's collection (named in the receipt) is gone.
