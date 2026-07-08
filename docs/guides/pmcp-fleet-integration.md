# PMCP Fleet Integration

This guide freezes the local PMCP pilot contract for `Code-Index-MCP`.
PMCP owns the shipped gateway manifest and provisioning fix tracked in
[PMCP issue #89](https://github.com/ViperJuice/pmcp/issues/89), which was
closed after PMCP `1.19.1` shipped the stdio manifest fix and the registry-env
registration guidance. Keep the repo-local PMCP `.mcp.json` override below as
the internal pilot contract until the fleet entry is promoted everywhere.

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
        "MCP_REPO_REGISTRY": "/abs/path/to/.mcp/indexes/repository_registry.json",
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
matches the repo parents PMCP is allowed to expose. When `MCP_REPO_REGISTRY`
is set, it must point at the same registry file used by the PMCP-spawned
server.

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
support allows one registered worktree per git common directory. Run
registration with the same `MCP_INDEX_STORAGE_PATH` and `MCP_REPO_REGISTRY`
that PMCP passes to the spawned `index-it-mcp` server; otherwise the CLI may
write to the default registry while PMCP reads a different, empty registry.

```bash
MCP_INDEX_STORAGE_PATH=/abs/path/to/.mcp/indexes \
MCP_REPO_REGISTRY=/abs/path/to/.mcp/indexes/repository_registry.json \
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

Current pilot caveat: local `mcp-index repository register` evidence only
proves the PMCP path when registration uses the same storage env as the
PMCP-managed runtime. If PMCP-mediated query tools return
`unregistered_repository`, rerun registration with the configured
`MCP_INDEX_STORAGE_PATH`/`MCP_REPO_REGISTRY`, keep using native search, and
check `docs/status/PMCP_FLEET_PILOT.md` before treating indexed results as
available.

## Fleet Rollout Policy

PMCP rollout stays staged and conservative. PMCP `1.19.1` can expose the
configured registry when registration uses the same storage env, and the live
pilot has one PMCP-ready repo. Broad rollout still waits on explicit indexing,
semantic backend proof, and repo-by-repo readiness.

### Pilot repos

- `Code-Index-MCP`, `pmcp`, `pmcp-code-mode-mcp`, and `agent-harness` remain
  the only rollout gate for this policy checkpoint.
- The current adoption verdict is controlled lexical PMCP pilot only:
  `Code-Index-MCP` is ready through PMCP, while `pmcp`,
  `pmcp-code-mode-mcp`, and `agent-harness` remain non-ready and must keep
  using native search.

### Core engineering repos

- Expand to additional actively maintained engineering repos only after the
  operator intentionally registers and indexes each repo through the same
  PMCP storage env, then verifies PMCP-mediated readiness `ready`.
- Keep PMCP lazy-started; fleet-wide auto-start is not allowed.
- broad auto-registration is not allowed. Operators must register intended
  repos explicitly and verify the PMCP-managed runtime sees them.

### Long-tail repos

- Long-tail repos stay out of rollout until pilot repos and core engineering
  repos prove stable bootstrap, registration alignment, and truthful fallback.
- Agents must use native search whenever PMCP-mediated Code-Index-MCP readiness is not `ready`.
- If the query surface returns `index_unavailable` with
  `safe_fallback: "native_search"`, keep using native search.

### Opt-out repos

- Repos with no need for PMCP-managed indexed search should stay on native
  search and skip PMCP registration entirely.
- Opt-out remains the default when operators cannot justify the bootstrap and
  support cost.

### Repos requiring manual constraints

- Repos with stricter path allowlists, bespoke bootstrap requirements, or
  shared-service conflicts need manual operator constraints before any PMCP
  adoption step.
- The same manual-constraint bucket applies when the host cannot provision the
  required `index-it-mcp` environment cleanly.

## Semantic Versus Lexical Policy

Semantic indexing is enabled only when all of the following are proven for the
target repo and managed runtime path:

- Qdrant is reachable and correctly configured.
- The local embedding endpoint is reachable.
- The `oss_high` profile is selected and compatible with the current host.
- PMCP env propagation is correct for the managed runtime.
- PMCP runtime registry alignment is proven for the registered repo.
- repository readiness is `ready`.

Lexical-only PMCP use is acceptable only after lexical readiness is `ready`
and semantic readiness is the remaining non-critical blocker. If lexical
readiness is not `ready`, use native search instead of PMCP-mediated search.

## Troubleshooting Matrix

| Symptom | Meaning | Operator action |
| --- | --- | --- |
| wrong transport | PMCP is pointed at the wrong server mode. | Use `index-it-mcp stdio` for the child-process path, not `index-it-mcp serve`. |
| wrong package version | The managed runtime drifted away from the validated pilot package. | Re-pin to `index-it-mcp==1.2.0` before comparing results. |
| missing allowed roots | The target repo is outside `MCP_ALLOWED_ROOTS`. | Correct the absolute allowlist and restart the managed runtime. |
| missing Qdrant | Semantic storage is unavailable. | Keep semantic indexing disabled and continue with native search or lexical-only PMCP only after lexical readiness is `ready`. |
| missing embedding endpoint | The local embedding endpoint is unavailable. | Keep semantic indexing disabled and use native search or verified lexical-only PMCP. |
| wrong branch | The checkout does not match the registered/default branch contract. | Switch back to the tracked branch or re-register the intended checkout. |
| stale commit | The index no longer matches the working commit. | Reindex or sync before trusting indexed results. |
| missing index | No usable index artifact exists yet. | Create or download the expected index before relying on PMCP queries. |
| `path_outside_allowed_roots` | The request path violates the server sandbox. | Use a registered repo name or fix `MCP_ALLOWED_ROOTS`. |
| active system PMCP service versus repo-local command mode | A system PMCP service conflicts with a repo-local command-mode entry. | Point PMCP at the remote URL path while the system service stays active, or disable the conflicting service before using command mode. |
| PMCP-managed server registry isolation after local CLI bootstrap | A plain CLI bootstrap used the default registry while the PMCP-managed runtime used the configured registry, causing `repositories: []` or `unregistered_repository`. | Register again with the same `MCP_INDEX_STORAGE_PATH`/`MCP_REPO_REGISTRY` used by PMCP, then re-check PMCP-mediated readiness; keep native search until ready. |
| host `CPython 3.13` provisioning incompatibilities | The host cannot provision the validated `index-it-mcp` wheel set cleanly. | Keep the repo in the manual-constraints bucket and use the validated local environment or a compatible host before rollout. |

## Non-Goals

- No PMCP code changes in this repository.
- No remote MCP transport implementation.
- No fleet-wide auto-start or surprise indexing.
- No secret values in examples, docs, or handoffs.
