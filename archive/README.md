# Archived Code

This directory contains older or duplicate implementations that have been replaced by newer, more complete versions.

## Archive Structure

### `/dispatcher/`
- `dispatcher.py` - Original dispatcher implementation (replaced by EnhancedDispatcher)
- `dispatcher_enhanced.py.backup` - Backup file

### `/scripts/`
- Various duplicate or older versions of scripts that have been consolidated

## Replacement Guide

| Archived File | Current Replacement |
|--------------|-------------------|
| `dispatcher.py` | `dispatcher_enhanced.py` |
| `index_test_repos_simple.py` | `index_all_repos_with_mcp.py` |
| `simple_index_test_repos.py` | `index_all_repos_with_mcp.py` |
| `populate_bm25_simple.py` | None. The standalone BM25 server was removed; use the STDIO server (`python -m mcp_server.cli stdio`) |
| `comprehensive_mcp_test.py` | `comprehensive_mcp_test_runner.py` |
| `analyze_claude_behavior_simple.py` | `analyze_claude_code_behavior_comprehensive.py` |

## Note
These files are kept for reference but should not be used in new development.