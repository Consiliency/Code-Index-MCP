# AUTHBOUND FastAPI Auth Boundary

FastAPI protected routes accept only validated JWT bearer tokens. Arbitrary bearer headers are rejected, and no dependency or middleware creates `fallback-user`, grants `admin`, or grants every permission from header presence alone.

The public allowlist is explicit: `/docs`, `/redoc`, `/openapi.json`, `/health`, `/ready`, and `/liveness`. All other FastAPI admin/debug routes require validated JWT authentication and then enforce role or permission checks.

Startup and request handling fail closed. Missing or invalid JWT signing configuration, missing default admin password, or weak blocklisted admin password do not enable a permissive mode. When auth state is unavailable, protected routes stay unavailable instead of trusting headers.

This phase does not change the STDIO `MCP_CLIENT_SECRET` handshake. The HTTP admin/debug boundary and the STDIO handshake remain separate contracts.

## Local And Ephemeral

The admin gateway defaults to `127.0.0.1`. Users, refresh tokens, sessions,
lockouts and rate-limit state are process-local memory, not durable identity
storage. Restart invalidates existing access and refresh sessions. Multiple
workers or replicas do not share identity or rate limits. This is not a hosted
multi-tenant authentication service. An explicit non-loopback bind requires a
separately secured operator deployment; it is not a fleet-readiness claim.

The `serve` entrypoint disables Uvicorn's implicit proxy-header rewriting.
`MCP_TRUSTED_PROXIES` optionally lists comma-separated proxy IPs/CIDRs. Security
middleware accepts forwarded identity only from a trusted socket peer, walking
the forwarded chain from the right to the first untrusted address. Untrusted,
malformed or unconfigured forwarded headers use the actual peer. Deployments
invoking Uvicorn directly must also set `--no-proxy-headers`; otherwise the
middleware cannot recover a peer address already rewritten upstream.

STDIO's standalone metrics listener is disabled unless `MCP_METRICS_PORT` is
explicitly set. When enabled it binds only `127.0.0.1`; port `0` selects an
ephemeral port. Bind failure is visible and does not claim another process's
listener. The shared process registry receives tool counters. Stopping the
listener closes its socket. The gateway's separate `/metrics` route retains
JWT/bearer authorization; do not expose the standalone listener remotely.

Normal request diagnostics record tool names, counts, duration and exception
types, not tool arguments, query/symbol content or raw provider exception
payloads. Plugin stderr is not a trusted log channel; typed IPC error envelopes
provide diagnostics. Cooperative plugin guards are not hostile-code isolation.
