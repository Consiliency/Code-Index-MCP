# Troubleshooting Guide

This guide helps you resolve common issues with Code-Index-MCP.

## Table of Contents
- [Installation Issues](#installation-issues)
- [Server Startup Problems](#server-startup-problems)
- [Plugin Errors](#plugin-errors)
- [Performance Issues](#performance-issues)
- [API Errors](#api-errors)
- [File System Issues](#file-system-issues)

## Installation Issues

### Python Version Mismatch
**Problem**: the installer reports that `index-it-mcp` requires Python `>=3.14`

**Solution**:
```bash
# Check Python version
python --version

# Let uv provide a 3.14 interpreter for the project environment
uv python install 3.14
uv sync --locked --python 3.14
```

### Missing Dependencies
**Problem**: `ModuleNotFoundError: No module named 'fastapi'`

**Solution**:
```bash
# Ensure you're in virtual environment
source venv/bin/activate  # Linux/Mac
# or
venv\Scripts\activate  # Windows

# Reinstall dependencies
pip install -r requirements.txt
```

### Tree-sitter Build Errors
**Problem**: `error: Microsoft Visual C++ 14.0 is required`

**Solution**:
- Windows: Install Visual Studio Build Tools
- Linux: `sudo apt-get install build-essential`
- Mac: `xcode-select --install`

### Windows Path-Length Fallback
**Problem**: Windows tools still report long-path failures after cloning the repo.

**Solution**:
- Keep the checkout on the cleaned source tree; tracked files are held to the 160-character tracked-path limit and the release audit checks wheel member depth separately.
- Prefer a short local clone path first.
- If your machine or a third-party tool still hits Windows path limits, use `git config --global core.longpaths true` as a fallback after the repo tree is clean.

## Server Startup Problems

### Port Already in Use
**Problem**: `ERROR: [Errno 48] Address already in use`

**Solution**:
```bash
# Find process using port 8000
lsof -i :8000  # Linux/Mac
netstat -ano | findstr :8000  # Windows

# Kill the process or use different port
uvicorn mcp_server.gateway:app --port 8001
```

### Import Errors
**Problem**: `ImportError: cannot import name 'app' from 'mcp_server.gateway'`

**Solution**:
```bash
# Ensure you're in project root
pwd  # Should show /path/to/Code-Index-MCP

# Add project to Python path
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

## Plugin Errors

### Plugin Not Found
**Problem**: `Plugin 'python' not found`

**Solution**:
1. Check plugin is registered in dispatcher.py
2. Verify plugin file exists in correct location
3. Check for import errors in plugin

### Tree-sitter Language Not Installed
**Problem**: `Language 'python' not available`

**Solution**:
```python
# Install language grammar
from tree_sitter import Language

Language.build_library(
    'build/languages.so',
    ['vendor/tree-sitter-python']
)
```

## Performance Issues

### Slow Indexing
**Problem**: Indexing takes too long for large codebases

**Solutions**:
1. **Increase batch size**:
   ```bash
   export INDEX_BATCH_SIZE=1000
   ```

2. **Skip large files**:
   ```bash
   export MAX_FILE_SIZE=5242880  # 5MB
   ```

3. **Use faster storage**: Move index to SSD

4. **Limit file types**:
   ```python
   # Only index specific extensions
   ALLOWED_EXTENSIONS = ['.py', '.js', '.cpp']
   ```

### High Memory Usage
**Problem**: Server using too much RAM

**Solutions**:
1. **Limit cache size**:
   ```bash
   export CACHE_SIZE_MB=512
   ```

2. **Enable garbage collection**:
   ```python
   import gc
   gc.collect()  # Force garbage collection
   ```

3. **Process files in chunks**

## API Errors

### 400 Bad Request
**Problem**: `{"detail": "Invalid file path"}`

**Solution**: Ensure file paths are absolute and within project directory

### 404 Not Found
**Problem**: `{"detail": "Symbol not found"}`

**Solution**: 
- Verify file has been indexed
- Check symbol name spelling
- Ensure correct file extension

### 500 Internal Server Error
**Problem**: Server returns 500 error

**Solution**:
1. Check server logs for stack trace
2. Enable debug mode: `export DEBUG=true`
3. Common causes:
   - Plugin crashes
   - Database locked
   - Out of memory

## File System Issues

### Permission Denied
**Problem**: `PermissionError: [Errno 13] Permission denied`

**Solution**:
```bash
# Check file permissions
ls -la problem_file.py

# Fix permissions
chmod 644 problem_file.py
```

### File Not Found
**Problem**: `FileNotFoundError: [Errno 2] No such file or directory`

**Solution**:
- Use absolute paths
- Check working directory
- Verify file exists: `ls -la /path/to/file`

### Symlink Issues
**Problem**: Symbolic links not followed

**Solution**:
```python
# Enable symlink following
FOLLOW_SYMLINKS = True
```

## Debug Mode

Enable detailed logging:
```bash
# Set log level
export LOG_LEVEL=DEBUG

# Enable FastAPI debug mode
export DEBUG=true

# Start server with reload
uvicorn mcp_server.gateway:app --reload --log-level debug
```

## Getting Help

If you're still having issues:

1. **Check existing issues**: [GitHub Issues](https://github.com/yourusername/Code-Index-MCP/issues)
2. **Ask in discussions**: [GitHub Discussions](https://github.com/yourusername/Code-Index-MCP/discussions)
3. **Create a bug report** with:
   - Error message and stack trace
   - Steps to reproduce
   - System information (OS, Python version)
   - Relevant configuration

## Common Error Messages

| Error | Likely Cause | Solution |
|-------|--------------|----------|
| `ConnectionRefusedError` | Server not running | Start server with `uvicorn` |
| `JSONDecodeError` | Invalid request body | Check API request format |
| `TimeoutError` | Large file or slow system | Increase timeout settings |
| `MemoryError` | Out of RAM | Reduce batch size or file limits |
| `KeyError: 'symbol_name'` | Missing required field | Check API documentation |

## Performance Tuning

### Recommended Settings for Large Codebases
```bash
# .env file
INDEX_BATCH_SIZE=1000
MAX_FILE_SIZE=10485760  # 10MB
CACHE_SIZE_MB=1024
WORKER_THREADS=4
ENABLE_PROFILING=true
```

### Database Optimization
```sql
-- Optimize SQLite
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA cache_size = -64000;  -- 64MB
PRAGMA temp_store = MEMORY;
```
