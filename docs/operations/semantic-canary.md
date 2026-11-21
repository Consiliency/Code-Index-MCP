# Semantic search canary

Tracking: Consiliency/Code-Index-MCP#113 (plan item C5 of #121).

`scripts/semantic_canary.py` is a small authenticated end-to-end check that
embedding-backed semantic search works through the MCP server. It uses only
synthetic data and a dedicated per-run collection.

## What it does

1. Creates a two-file synthetic git repository (the v13 pilot corpus,
   `balance.py` and `catalog.py`) in a private temporary directory.
2. Starts `index-it-mcp stdio` with a minimal environment: a per-run
   `canary-<id>` profile in `SEMANTIC_PROFILES_JSON`, a private registry and
   index directory, `SEMANTIC_AUTOSTART_QDRANT=false` and
   `PYTHON_DOTENV_DISABLED=1`. No caller variables leak into the server, apart
   from `PATH` and the tree-sitter grammar cache locations.
3. Registers and reindexes the repository and requires `get_status` to report
   semantic readiness `ready`.
4. Runs two semantic queries. Each expected file must be the top hit, with
   `semantic_source == "semantic"`, the canary profile id and one collection that
   matches readiness. The collection must contain the canary profile segment
   (`ci__<repo>__canary-<id>__<generation>`, the registry's naming scheme) and
   hold points of the probed dimension.
5. Requires anonymous `GET /collections` to return 401 before the run, every 0.5 s
   while the server runs, and after cleanup.
6. Deletes every collection carrying this run's profile segment, also on
   failure, and removes the temporary directory.

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
loopback OpenAI-compatible embedder that requires a per-run bearer token and
embeds with a deterministic hashed bag-of-words. It then runs the canary end to
end, and runs it again with query embeddings refused after indexing, which must
fail with `semantic_query_refused` and still delete the collection.

This proves the wiring: profile, stdio server, reindex with summaries, readiness,
collection linkage and shape, anonymous denial, receipt and cleanup. It does not
prove retrieval quality or that a real model, the `ai` endpoints or a deployed
Qdrant work. The top-hit check is also not pure vector evidence: semantic results
pass through `SemanticIndexer._rerank_query_results`, which multiplies the vector
score by a query-overlap bonus, so even identical vectors can rank the expected
file first. Vector linkage is shown by the collection's dimension and point count.

The test skips when Docker Compose or the pinned Qdrant image is missing. No CI
job currently provides the image, so it runs locally:

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
`GET /collections` that no `__canary-` collection remains.
