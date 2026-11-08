# Breaking Changes

This file logs every schema bump and other breaking changes to Code-Index-MCP.

## 1.5.0 (2026-11-08): chunk ids move to `treesitter_chunk_id_v6`

treesitter-chunker 6 (Code-Index-MCP#115) changes chunk identity. Indexes built
with chunker 4.x report `scheme_mismatch` and fail closed; semantic profiles
read as profile-incompatible. Grammars are no longer bundled with
tree-sitter-language-pack 1.20, and the Linux wheels need glibc 2.34 or newer.

**Migration**: Run `mcp-index grammars prefetch` (again after every
language-pack upgrade), then a full `reindex`, then rebuild semantic summaries
and vectors. Without a prefetch, files that need a grammar fail with
`grammar_unavailable` and the generation is not published.

## 1.5.0 (2026-11-08): baml-bridge 0.23.0 and the generated BAML SDK

`baml-bridge` moves from 0.20.1 to 0.23.0 (Code-Index-MCP#119) and `baml_sdk/`
is regenerated. Generated `FunctionSpec.client_id()` is now
`requested_model()` and returns the bare model name. Call recording is
controlled only by `MCP_BAML_TELEMETRY` (off by default); upstream
`BAML_TELEMETRY` is ignored.

**Migration**: Replace `client_id()` calls with `requested_model()`. Move any
`BAML_TELEMETRY` setting to `MCP_BAML_TELEMETRY`, noting that `low`, `medium`
or `high` uploads prompts containing source code when a Boundary key or saved
login exists.

## P10 (2026-04-18): `strict_compatibility` default flipped to True

`IndexManager.select_best_index` now raises `SchemaMismatchError` by default
when the on-disk index schema version does not match the expected current version.

**Migration**: Re-index affected repositories, or pass
`--rebuild-on-schema-mismatch` to `mcp-server stdio` / `serve` to rebuild
automatically on load.

## Schema migration history

| Migration | Date | Summary |
|---|---|---|
| 001_initial_schema | 2025-01-28 | Initial SQLite schema (files, symbols, chunks tables) |
| 002_relative_paths | 2025-03-28 | Relative-path migration |
| 003_stable_ids | 2025-01-28 | Stable symbol IDs |
| 004_semantic_points | 2025-03-12 | Semantic/embedding point storage |
| 005_chunk_summaries | 2025-03-16 | Chunk summaries table |
