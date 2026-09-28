# BAML v1 Cerebras live smoke

- Run: 2026-09-28 06:46 UTC, from the Code-Index-MCP#99 implementation worktree.
- Toolchain and runtime: BAML 0.20.1, `baml-bridge==0.20.1`.
- Credential source: `Cerebras` item, `Consiliency Deploy Secrets` 1Password vault, injected with `op run`.
- Provider: Cerebras, model `gpt-oss-120b`.
- Input: one short synthetic Python function and one chunk; no repository source or secrets sent.

| Function | Request outcome | Parseable result | Requested chunk ID returned |
|---|---|---|---|
| `SummarizeFileChunks_async` | Success | Yes | Yes (`smoke-1`) |
| `SummarizeChunkAlone_async` | Success | Yes | Yes (`alpha`) |

The smoke printed only these booleans and did not log the credential, prompts, source content, or provider response.
