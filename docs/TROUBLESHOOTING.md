# Code-Index-MCP Troubleshooting Guide

## Common Issues and Solutions

### 1. MCP Server Returns No Results

**Symptoms:**
- `mcp-index search` returns 0 results
- Claude Code MCP tools return empty results
- Direct SQL queries work but MCP doesn't

**Solutions:**

#### A. Plugin Loading Timeout
The dispatcher may be timing out while loading plugins. This is the most common issue.

**Fix:**
```bash
# Use simple dispatcher for BM25-only search (fastest)
export MCP_USE_SIMPLE_DISPATCHER=true

# Or increase plugin timeout (default is 5 seconds)
export MCP_PLUGIN_TIMEOUT=10
```

#### B. Index Not Found
```bash
# Check if index exists
ls ~/.mcp/indexes/*/current.db

# If missing, create index
mcp-index index

# Or move existing index to central location
python scripts/move_indexes_to_central.py
```

### 2. Qdrant Lock Conflicts

**Symptoms:**
- Error: "Storage folder already accessed by another instance"
- Semantic search not working
- Vector database initialization fails

**Solutions:**

#### A. Clean Up Lock Files
```bash
# Remove Qdrant lock files
rm -f vector_index.qdrant/.lock

# Or use server mode instead
export QDRANT_USE_SERVER=true
docker-compose -f docker-compose.qdrant.yml up -d
```

#### B. Use File Mode Fallback
```bash
# Force file mode
export QDRANT_USE_SERVER=false
```

### 3. Index Out of Sync with Repository

**Symptoms:**
- Search results show outdated code
- Files that were deleted still appear in results
- New files not being found

**Solutions:**

#### A. Sync Index with Git
```bash
# Sync all indexes with their repositories
python sync_indexes_with_git.py

# Check sync status
python scripts/check_index_status.py
```

#### B. Reindex Changed Files
```bash
# Reindex current directory
mcp-index reindex

# Or full reindex
mcp-index index --force
```

### 4. Performance Issues

**Symptoms:**
- Searches take > 1 second
- Indexing is very slow
- High memory usage

**Solutions:**

#### A. Use Simple Dispatcher
```bash
# For basic text search without semantic features
export MCP_USE_SIMPLE_DISPATCHER=true
```

#### B. Disable Semantic Search
```bash
# In your search commands
mcp-index search "query" --no-semantic
```

#### C. Check Index Size
```bash
# Find large indexes
du -sh ~/.mcp/indexes/*/*db | sort -h

# Consider excluding large generated files
echo "node_modules/" >> .mcp-index-ignore
echo "dist/" >> .mcp-index-ignore
```

### 5. Docker Issues

**Symptoms:**
- Can't connect to Qdrant server
- MCP server not accessible
- Container startup failures

**Solutions:**

#### A. Check Container Status
```bash
docker-compose ps
docker-compose logs mcp-server
docker-compose logs qdrant
```

#### B. Reset Containers
```bash
docker-compose down
docker-compose up -d
```

#### C. Use Native Installation
```bash
# Install without Docker
pip install -e .
```

### 6. Claude Code Integration Issues

**Symptoms:**
- Claude Code can't find MCP server
- Tools not appearing in Claude Code
- Connection timeouts

**Solutions:**

#### A. Check MCP Configuration
```bash
# Verify .mcp.json exists
cat .mcp.json

# Check server is running
ps aux | grep mcp_server_cli
```

#### B. Restart Claude Code
1. Close Claude Code completely
2. Clear any cache/temp files
3. Restart and wait for MCP discovery

#### C. Use Debug Mode
```bash
# Enable debug logging
export MCP_DEBUG=1
```

### 7. Missing Language Support

**Symptoms:**
- Files of certain types not being indexed
- "No plugin found for extension" errors

**Solutions:**

#### A. Check Supported Languages
```bash
# List all supported languages
mcp-index status | grep -A50 "Supported Languages"
```

#### B. Verify Tree-Sitter Parser
```bash
# Install missing parsers
pip install tree-sitter-[language]
```

### 8. Git Integration Issues

**Symptoms:**
- Repository ID not found
- Can't determine git remote
- Index not linking to repository

**Solutions:**

#### A. Check Git Configuration
```bash
# Verify git remote exists
git remote -v

# Add remote if missing
git remote add origin [url]
```

#### B. Use Path-Based ID
```bash
# The system will fall back to path-based hash
# Check logs for the generated ID
export MCP_DEBUG=1
mcp-index index
```

### 9. Windows Path-Length Fallback

The tracked repository tree is constrained to the 160-character tracked-path limit,
and the release audit also checks wheel member depth so install paths do not
regress silently.

If a Windows clone still fails because your checkout root or another tool adds
extra path depth, shorten the clone location first. Use
`git config --global core.longpaths true` only as a fallback after the repo
tree is clean.

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_USE_SIMPLE_DISPATCHER` | `false` | Use lightweight BM25-only dispatcher |
| `MCP_PLUGIN_TIMEOUT` | `5` | Seconds before plugin loading timeout |
| `MCP_DEBUG` | `0` | Enable debug logging (1 = enabled) |
| `MCP_INDEX_STORAGE_PATH` | `~/.mcp/indexes` | Central index storage location |
| `QDRANT_USE_SERVER` | `true` | Use Qdrant server mode vs file mode |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant server URL |
| `QDRANT_API_KEY` | None | API key sent to server-mode Qdrant; must match the server's `QDRANT__SERVICE__API_KEY` (an HTTP 401 from the Qdrant probe means it is missing or wrong) |
| `VOYAGE_API_KEY` | None | API key for semantic embeddings |

## Debug Commands

```bash
# Check index status
python scripts/check_test_indexes.py

# Verify dispatcher functionality
python scripts/test_mcp_directly.py

# Test search performance
python scripts/test_mcp_performance.py

# Validate all indexes
python scripts/verify_all_indexes_parallel.py
```

## Getting Help

1. **Check Logs**: Always check stderr output for detailed error messages
2. **Enable Debug Mode**: `export MCP_DEBUG=1` for verbose logging
3. **GitHub Issues**: Report bugs at https://github.com/[repo]/issues
4. **Documentation**: See `/docs` directory for detailed guides

## Quick Fixes Checklist

- [ ] Is the index created? (`mcp-index index`)
- [ ] Is the index in central location? (`ls ~/.mcp/indexes/`)
- [ ] Are there any lock files? (`rm vector_index.qdrant/.lock`)
- [ ] Is simple dispatcher faster? (`export MCP_USE_SIMPLE_DISPATCHER=true`)
- [ ] Is the repository synced? (`python sync_indexes_with_git.py`)
- [ ] Are large files excluded? (`.mcp-index-ignore`)
- [ ] Is debug mode helpful? (`export MCP_DEBUG=1`)
