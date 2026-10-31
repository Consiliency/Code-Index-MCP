# Qdrant authentication: migration and rollback

Tracking: Consiliency/Code-Index-MCP#106.

Server-mode Qdrant must run with an API key, and every Code-Index-MCP client must
send it. A private network or loopback binding does not replace authentication;
on a shared host, every local user can reach loopback.

## Behavior

| Surface | Before | After |
|---|---|---|
| `docker-compose.qdrant.yml`, `docker-compose.yml`, `docker/compose/development/docker-compose.development.yml` | API key commented out; Qdrant accepted anonymous calls; `qdrant/qdrant:latest`; some ports on all interfaces | `QDRANT__SERVICE__API_KEY=${QDRANT_API_KEY:?...}`: Compose refuses to render without a key; an entrypoint guard exits 64 if the key reaches the container empty; image pinned to `v1.17.0@sha256:f1c7272c...`; ports bound to `127.0.0.1`; telemetry off |
| `docker-compose.mcp.yml` (optional `with-vectors` profile) | No key | The key is not forced at render time, so users without vectors are unaffected. The same entrypoint guard refuses to start Qdrant without a key |
| Server-mode clients (`http://` or `https://`) | Sent `QDRANT_API_KEY` if set, otherwise connected anonymously | Refuse to connect without `QDRANT_API_KEY` (`QdrantAuthConfigError`, preflight `failure_class: qdrant_api_key_missing`). No request is sent |
| File-mode (`path=`) and `:memory:` clients | No key | Unchanged. These are embedded and have no network listener |
| Health probes `/readyz`, `/healthz`, `/livez`, `/` | Open | Still open (Qdrant exempts them). All other endpoints return 401 without a valid key |

### Client inventory

These are all Qdrant client constructions in supported code. Each one now uses
`mcp_server.config.env_vars.resolve_qdrant_server_api_key()`:

- `mcp_server/utils/semantic_indexer.py` `SemanticIndexer._init_qdrant_client`. This covers
  every `SemanticIndexer` user: the gateway, the plugin semantic path
  (`plugin_base_enhanced.py`, which logs a warning and disables semantic search on
  `QdrantAuthConfigError`), `semantic_indexer_registry.py` and the `index_management.py` CLI.
- `mcp_server/setup/semantic_preflight.py`: the `check_qdrant` and `check_qdrant_collection`
  HTTP probes, and `bootstrap_active_profile_collection`.
- `mcp_server/cli/index_management.py` `_get_vector_backend_status`. The key is resolved
  before the broad `except`, so a missing key is reported instead of hidden.
- `mcp_server/artifacts/secure_export.py` export from a server backend.
- `mcp_server/setup/qdrant_autostart.py`, through `check_qdrant`. Autostart runs
  `docker-compose.qdrant.yml`, which now needs `QDRANT_API_KEY` in the environment.
- Scripts: `scripts/utilities/debug_semantic_search.py` uses the resolver.
  `scripts/v13_qdrant_smoke.py` and `scripts/v13_pmcp_pilot.py` start their disposable
  servers with a random key for each run. Docker receives the key by name only, so it
  never appears in argv. The pilot writes the key into its private fixture `.mcp.json`
  next to its existing per-run guard token.

Not migrated:

- `mcp_server/utils/semantic_discovery.py` and the `scripts/*` maintenance utilities
  that open file-mode (`path=`) stores. There is no network listener.
- The frozen benchmark record `benchmarks/retrieval_eval/runs/inferbound-v10/run_inference_gate.py`,
  the legacy `tests/root_tests/` files (excluded by
  `norecursedirs` in `pytest.ini`) and `archive/`. Run against an
  authenticated server, these fail with HTTP 401. They do not bypass authentication.
- Scheduled GitHub workflows. None of them talk to a Qdrant server.
  `index-artifact-management.yml` only packages the file-mode `vector_index.qdrant` directory.
- `k8s/deployment.yaml` already maps `QDRANT_API_KEY` from the `qdrant-api-key` secret.
  It stays `optional: true` because Kubernetes semantic search is optional. The client
  now fails closed when a server URL is used without the key. The repository ships no
  Qdrant server manifest for Kubernetes.

## Configuration

- `QDRANT_API_KEY`: the key. Use at least 32 random bytes, for example
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`, stored in a secret
  manager and injected with `op run` or a mode-600 env file. Never commit it or pass it
  in a command line.
- `QDRANT_REQUIRE_API_KEY` defaults to `true`. `false` is only for a disposable,
  unauthenticated test server. It is not a rollback mechanism.

Qdrant also supports `QDRANT__SERVICE__READ_ONLY_API_KEY` and JWT RBAC. Code-Index-MCP
writes vectors, so it needs the full key. A read-only key can be added for read-only
consumers later. This change does not configure one.

Transport: the shipped Compose files use plain HTTP on loopback or on a private Compose
network. If a client reaches Qdrant across hosts, enable Qdrant TLS
(`QDRANT__SERVICE__ENABLE_TLS` with certificates) or tunnel it. qdrant-client warns when
it sends a key over plain HTTP.

## Migration (owner-approved maintenance window)

Qdrant has no hot reload of the API key and no dual-key overlap. Enabling the key
needs a container restart, and clients without the key fail until they are updated.
Plan for downtime.

1. Inventory the deployment. Record the container identity (`docker inspect` image,
   image ID, mounts and compose files) and confirm that its environment contains
   `QDRANT__SERVICE__API_KEY`. Print only the variable names, never the values.
2. Back up the storage. Take a Qdrant snapshot of each collection with the current
   credentials, or stop the container and copy the storage directory. Verify the
   backup's size and file count before you continue.
3. Provision the key in the secret manager. Do not create or rotate it without
   owner approval.
4. Update every client environment that sets `QDRANT_URL` with the same
   `QDRANT_API_KEY`: MCP server `.mcp.env` and `.mcp.json` entries, CI or scheduled
   jobs if any, and operator shells.
5. Restart Qdrant through the repository Compose file with the key in the environment:
   `op run --env-file=<env> -- docker compose -f docker-compose.qdrant.yml [-f <host override>] up -d qdrant`.
   In the merged model, `environment` takes precedence over `env_file`, so a host
   override that supplies the key through `env_file` must provide the same value as
   `QDRANT_API_KEY`. Otherwise drop the `env_file` from the override.
6. Read back the result:
   - `GET /readyz` returns 200.
   - Anonymous `GET /collections` returns 401.
   - `GET /collections` with the key returns 200, and the expected collections and point
     counts are present.
   - `mcp-index setup semantic` preflight reports Qdrant `ready`.
   - A semantic search returns results.
7. Restart the MCP clients and confirm that readiness is `ready`.

## Rollback

Never roll back by removing the API key, by setting `QDRANT_REQUIRE_API_KEY=false` on a
production client, or by restoring the previous anonymous configuration. Those restore
the unsafe state that #106 removes.

- **Client cannot authenticate (401 or `qdrant_api_key_missing`):** stop here and keep
  failing closed. Fix the client's `QDRANT_API_KEY` from the secret manager. Indexed
  search falls back to `index_unavailable` with `safe_fallback: "native_search"` and
  lexical (SQLite) search keeps working.
- **Qdrant does not start (guard exit 64, or a Compose render error):** the key did not
  reach the container. Fix the injection and run the restart again. The data on disk
  has not changed.
- **Data problem after the restart:** stop the container, restore the backup from
  step 2 into the storage path, and start it again with the API key still required.
- **Code regression:** revert this change's commits. The authenticated server
  configuration can stay in place, because the previous client code sends
  `QDRANT_API_KEY` whenever it is set.

## Verification in this repository

- `tests/test_qdrant_api_key.py` covers unit-level fail-closed behavior and key
  propagation for every client construction.
- `tests/integration/test_qdrant_auth_container.py` (marker `integration`) starts the
  repository `docker-compose.qdrant.yml` in a disposable Compose project. It checks:
  - rendering is refused without a key;
  - the container refuses to start with an empty key;
  - anonymous and wrong-key reads, writes, `/telemetry` and `/metrics` get 401;
  - default clients fail closed before connecting;
  - an authorized client creates, writes, queries and deletes a synthetic collection.

  It skips when Docker or the pinned image is unavailable:
  `uv run --extra dev pytest tests/integration/test_qdrant_auth_container.py -m integration --no-cov`.

A source change does not prove that a running deployment is repaired. Only the
readback in step 6 against the live service shows that.
