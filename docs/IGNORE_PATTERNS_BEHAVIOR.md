# Ignore Patterns Behavior

## Overview

Registered repository generations are built from committed source on the tracked
branch. Ignore policy applies before indexing, not just when sharing artifacts.

## How It Works

### 🔍 Local BM25/FTS5 Index (SQLite)
The shared walker excludes ignored paths before creating lexical rows. It combines
built-in MCP exclusions, `.mcp-index-ignore`, and scoped `.gitignore` rules. Outside
paths and symlink traversal are rejected. Internal/cache directories, including
`.git`, `.mcp-index`, virtual environments, and `node_modules`, are also excluded.

### 🧠 Semantic Index (Qdrant vectors)
The semantic (vector) index **respects `.gitignore` and `.mcp-index-ignore`** at build
time. Files matching those patterns are not embedded into Qdrant. This prevents large
external fixture directories (e.g. `test_workspace/`) from polluting vector search results
and causing unnecessary embedding API spend during rebuilds.

### 🔒 Sharing/Exporting (GitHub Artifacts)
Secure export applies filename-based filtering before sharing:
- Files matching `.gitignore` patterns are excluded
- Files matching `.mcp-index-ignore` patterns are excluded  
- Review the actual artifact before sharing
- Credentials in ordinary source files can still be indexed and exported

**Example**: When you push an index to GitHub Artifacts, `.env` files are automatically excluded.

## Why This Approach?

The same corpus policy limits local indexing, semantic inference spend, and export
exposure. Filename filtering is not a secret scanner and cannot prove that source
or an artifact is free of sensitive content.

## Pattern Files

### `.gitignore`
Standard scoped git ignore patterns apply during registered builds and export:
```
.env
*.key
*.pem
node_modules/
__pycache__/
build/
dist/
```

### `.mcp-index-ignore`  
Additional MCP exclusions extend the built-in defaults. An empty or comment-only
file does not disable credential, cache, or generated-file exclusions:
```
# External fixture repos (excluded from semantic index at build time)
test_workspace/
test_repos/
vendor/
third_party/

# Generated code
baml_client/
*_pb2.py

# Large files
*.zip
*.tar.gz

# Temporary files
*.tmp
temp/
```

These patterns affect both lexical and semantic registered builds. An explicit
negation such as `!.env.example` can admit a reviewed, credential-free example.
It cannot override a separate `.gitignore` exclusion, a fixed excluded directory,
or path confinement. An excluded parent directory still prevents traversal.

## Existing Indexes

Changing rules does not erase previous generations or shared artifacts. Rebuild
the registered generation and check readiness before trusting indexed results.
Previously shared data requires separate exposure handling; exclusions do not
revoke copies or rotate credentials.

## Common Use Cases

Use the registered repository and artifact commands in the
[data-generation runbook](operations/v13-data-generation.md). Do not use legacy
unregistered restore paths to replace a live generation. A dirty checkout does
not authorize indexing uncommitted bytes.

## Best Practices

1. **Review Before Sharing**: Inspect the policy and prepared artifact locally.

2. **Use .mcp-index-ignore**: Add patterns for files that shouldn't be in shared indexes but aren't in .gitignore:
   ```
   # Large test fixtures
   tests/fixtures/large_*.json
   
   # Personal notes
   personal_notes.md
   TODO_private.md
   ```

3. **Security Audit**: Check ordinary source files for credentials as well as
   reviewing filename exclusions.

## Implementation Details

Build filtering is owned by `mcp_server/core/ignore_patterns.py` and the shared
walker. `SecureIndexExporter` also:
1. Loads patterns from `.gitignore` and `.mcp-index-ignore`
2. Creates a filtered copy of the SQLite database
3. Excludes all files matching the patterns
4. Creates an audit log of excluded files
5. Packages the clean index for sharing

Tests in `tests/test_ignore_patterns.py` cover default preservation, explicit
negation, nested rules, and path boundaries. Committed-generation exclusion
transitions are covered in `tests/test_v13_prep_panel_repairs.py`.
