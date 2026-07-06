# PMCP Fleet Integration

This guide freezes the local PMCP pilot contract for `Code-Index-MCP`.
PMCP owns the shipped gateway manifest and provisioning fix tracked in
[PMCP issue #89](https://github.com/ViperJuice/pmcp/issues/89). Until that
upstream work lands, use a repo-local PMCP `.mcp.json` override for internal
dogfooding.

## Ownership Boundary

- PMCP owns gateway discovery, provisioning, manifest defaults, and the
  shipped fleet entry.
- Code-Index-MCP owns the stdio server, readiness contract, semantic preflight,
  repo registration guidance, and native-search fallback behavior.
- This guide is a local pilot override, not a replacement for the PMCP-owned
  manifest fix.

## Local PMCP Override

Use a pinned local override instead of PMCP's current floating manifest entry:

```json
{
  "mcpServers": {
    "code-index-mcp": {
      "command": "uvx",
      "args": [
        "--from",
        "index-it-mcp==1.2.0",
        "index-it-mcp",
        "stdio"
      ],
      "env": {
        "MCP_ALLOWED_ROOTS": "/abs/path/to/repos",
        "MCP_INDEX_STORAGE_PATH": "/abs/path/to/.mcp/indexes",
        "SEMANTIC_SEARCH_ENABLED": "true",
        "MCP_QDRANT_URL": "http://localhost:6333"
      }
    }
  }
}
```

Pilot command:

```bash
uvx --from index-it-mcp==1.2.0 index-it-mcp stdio
```

This command starts the local child-process MCP transport PMCP expects.

## Why `stdio` And Not `serve`

`index-it-mcp stdio` starts the JSON-RPC server over stdin/stdout for PMCP's
local child-process MCP transport.

`index-it-mcp serve` is different. It starts the FastAPI admin/debug surface,
not the PMCP child-process MCP transport. Use `serve` only when you are
deliberately operating the secondary HTTP/admin surface outside this local PMCP
pilot flow.

## Version And Channel Risk

Freeze the pilot to `index-it-mcp==1.2.0`.

PyPI currently exposes multiple `index-it-mcp` release lines. An unpinned
command can drift to a different package version or channel than the one
validated for this rollout.

Do not use `uvx index-it-mcp`, a floating channel, or `index-it-mcp serve` as
the pilot PMCP child-process override.

## Local STDIO Authentication Posture

For the internal PMCP-managed stdio pilot, leave `MCP_CLIENT_SECRET` unset.

When `MCP_CLIENT_SECRET` is unset, the stdio server logs
`running unauthenticated — MCP_CLIENT_SECRET not set` and does not require the
`handshake` tool before other calls. If `MCP_CLIENT_SECRET` is set, the server
requires a successful `handshake` call before tool use. That handshake flow is
not part of this temporary PMCP local override, so the pinned pilot posture is
to keep `MCP_CLIENT_SECRET` unset for local stdio until PMCP explicitly models
the handshake path.

## Non-Goals

- No PMCP code changes in this repository.
- No remote MCP transport implementation.
- No fleet-wide auto-start or surprise indexing.
- No secret values in examples, docs, or handoffs.
