# PMCP Fleet Integration

This guide freezes the local PMCP pilot contract for `Code-Index-MCP`.
PMCP owns the shipped gateway manifest and provisioning fix tracked in
[PMCP issue #89](https://github.com/ViperJuice/pmcp/issues/89). Until that
upstream work lands, use a repo-local PMCP `.mcp.json` override for internal
dogfooding.

Pilot status report: `docs/status/PMCP_FLEET_PILOT.md`
([PMCP Fleet Pilot](../status/PMCP_FLEET_PILOT.md)).

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
        "SEMANTIC_DEFAULT_PROFILE": "oss_high",
        "SEMANTIC_EMBEDDING_BASE_URL": "http://ai:8001/v1",
        "QDRANT_URL": "http://localhost:6333",
        "SEMANTIC_AUTOSTART_QDRANT": "false",
        "MCP_AUTO_INDEX": "false"
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

The PMCP local override must use canonical env names. Use `QDRANT_URL`, not
`MCP_QDRANT_URL`. Keep `MCP_ALLOWED_ROOTS` as an absolute path list that
matches the repo parents PMCP is allowed to expose.

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

## Semantic Preflight

Before trusting PMCP-mediated indexed results, validate the local semantic
stack with a dry run:

```bash
mcp-index setup semantic --dry-run \
  --profile oss_high \
  --qdrant-url http://localhost:6333 \
  --openai-api-base http://ai:8001/v1 \
  --no-autostart-qdrant
```

This preflight is read-only. It reports the selected profile, effective
embedding endpoint, Qdrant endpoint, collection bootstrap state, blocker code,
and whether the server can write semantic vectors for the active profile.

The PMCPBOOT contract is no-surprise bootstrap:

- `mcp-index setup semantic --dry-run` does not create collections.
- `mcp-index setup semantic --dry-run` does not write semantic vectors.
- `mcp-index setup semantic --dry-run` does not start long-running indexing.
- `--no-autostart-qdrant` keeps local Qdrant startup out of the pilot
  preflight path.
- `MCP_AUTO_INDEX=false` keeps repository registration from triggering
  surprise fleet indexing.

Use the dry-run output to verify:

- `SEMANTIC_DEFAULT_PROFILE=oss_high` selects the local 4096-dimension
  semantic profile.
- `SEMANTIC_EMBEDDING_BASE_URL=http://ai:8001/v1` points at the local
  OpenAI-compatible embedding endpoint.
- `QDRANT_URL=http://localhost:6333` points at the local vector store.
- `SEMANTIC_AUTOSTART_QDRANT=false` and `MCP_AUTO_INDEX=false` preserve the
  explicit operator-controlled bootstrap posture for this pilot.

## Repository Bootstrap And Readiness

Register only the checkout you want PMCP to trust. Public alpha multi-repo
support allows one registered worktree per git common directory.

```bash
mcp-index repository register /abs/path/to/repo
mcp-index repository list -v
mcp-index repository status
mcp-index artifact workspace-status
```

Treat those commands as the bootstrap and status set before PMCP-mediated
queries:

- `mcp-index repository register` records the tracked checkout.
- `mcp-index repository list -v` shows rollout status, query surface, and the
  registered worktree identity.
- `mcp-index repository status` shows readiness plus semantic preflight and
  remediation data for the current checkout.
- `mcp-index artifact workspace-status` shows local-first artifact/runtime
  readiness without forcing query traffic.

Interpret readiness before treating indexed results as authoritative:

- `ready`: indexed results may be trusted for that registered checkout.
- `stale_commit`: reindex or sync before trusting indexed results from the
  current commit.
- `wrong_branch`: switch back to the registered/default branch or register the
  intended checkout.
- `missing_index`: create or download the expected index before indexed query
  use.
- `path_outside_allowed_roots`: the requested filesystem path is outside
  `MCP_ALLOWED_ROOTS`; correct the allowlist or use a registered repo name.
- `index_unavailable` with `safe_fallback: "native_search"`: query tools stay
  fail-closed on non-ready repositories, so use native search and follow the
  surfaced readiness remediation first.

Remediation commands such as `reindex` or `repository sync` come after
non-ready evidence. They are not part of the no-surprise PMCPBOOT preflight.

## Non-Goals

- No PMCP code changes in this repository.
- No remote MCP transport implementation.
- No fleet-wide auto-start or surprise indexing.
- No secret values in examples, docs, or handoffs.
