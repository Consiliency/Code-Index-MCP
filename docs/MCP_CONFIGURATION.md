# MCP Configuration Guide

This guide provides comprehensive information on configuring the Code-Index-MCP for different environments and use cases.

> **Stable-surface prep status**: This guide targets the `1.4.1` prepared candidate.
> MCP STDIO is primary; FastAPI is secondary admin only. Docker examples require
> the matching registry publication. Published `1.4.0` lacks the v13 repairs.
> See [release preparation and acceptance](operations/v13-release.md).
> That container image name is a container identity surface, not the canonical
> Python distribution name.
> Language behavior is defined in [SUPPORT_MATRIX.md](SUPPORT_MATRIX.md), and
> the pre-GA release boundary is frozen in
> [validation/ga-readiness-checklist.md](validation/ga-readiness-checklist.md).
> Support tiers for native installs, package installs, Docker, and optional
> extras live in [SUPPORT_MATRIX.md](SUPPORT_MATRIX.md); this guide does not
> imply identical support for every configuration path.

For release containers, replace every `<published-digest>` placeholder below
with the digest from the accepted GitHub release's `image-reference.txt` asset,
or use the Docker installer's generated configuration. Set `MCP_DOCKER_IMAGE`
to that full digest reference when using a client that expands environment
variables. There is no version-tag or `latest`-tag fallback for `1.4.1`.
>
> **Public alpha repository model**: one server can serve many unrelated
> repositories, with one registered worktree per git common directory. Only the
> tracked/default branch is indexed automatically. Indexed MCP search is
> authoritative only when readiness is `ready`; unavailable indexes return
> `index_unavailable` with `safe_fallback: "native_search"`.

## Table of Contents

1. [Understanding MCP Communication](#understanding-mcp-communication)
2. [Environment Detection](#environment-detection)
3. [Configuration Templates](#configuration-templates)
4. [Manual Configuration](#manual-configuration)
5. [Multi-Repository and Smart Plugin Loading](#multi-repository-and-smart-plugin-loading)
5. [Advanced Configurations](#advanced-configurations)
6. [Troubleshooting](#troubleshooting)
7. [Security Considerations](#security-considerations)

## Understanding MCP Communication

The Model Context Protocol (MCP) uses JSON-RPC over standard input/output (stdio) for communication:

```
┌─────────────┐         stdio          ┌──────────────┐
│   Claude    │ <──────────────────> │  MCP Server  │
│    Code     │    JSON-RPC msgs     │ (Code-Index) │
└─────────────┘                      └──────────────┘
```

**Key Points:**
- No network ports or HTTP servers required
- Direct process communication via stdin/stdout
- Secure by default (no network exposure)
- Works in all environments (containers, WSL, native)

`MCP_CLIENT_SECRET` is a local STDIO handshake guard for `mcp-index stdio`.
The FastAPI gateway uses separate admin/debug bearer token authentication, and
no remote MCP authorization is implemented while remote MCP transport remains
deferred.

## Environment Detection

The setup script automatically detects your environment:

```bash
# Automatic detection and setup
./scripts/setup-mcp-json.sh

# Interactive mode with options
./scripts/setup-mcp-json.sh --interactive

# Force specific environment
./scripts/setup-mcp-json.sh --env docker
```

### Detection Logic

| Environment | Detection Method | Configuration Used |
|-------------|-----------------|-------------------|
| Dev Container | Checks for `/.dockerenv` file | `native.json` |
| Docker Available | Checks `docker` command | `docker-minimal.json` |
| WSL | Checks `/proc/version` for "microsoft" | `wsl-docker.json` |
| Native | Default if others not detected | `native.json` |

## Configuration Templates

All repo-shipped MCP client templates launch `mcp-index stdio`. Use
`mcp-index serve` separately only for the secondary FastAPI admin/debug HTTP
gateway, not as MCP client registration.

### Native Python Configuration

**File:** `.mcp.json.templates/native.json`

```json
{
  "mcpServers": {
    "code-index-native": {
      "command": "mcp-index",
      "args": ["stdio"],
      "cwd": "${workspace}",
      "env": {
        "PYTHONPATH": "${workspace}",
        "VOYAGE_API_KEY": "${VOYAGE_API_KEY:-}",
        "SEMANTIC_SEARCH_ENABLED": "${SEMANTIC_SEARCH_ENABLED:-false}",
        "MCP_WORKSPACE_ROOT": "${workspace}",
        "MCP_ARTIFACT_SYNC": "${MCP_ARTIFACT_SYNC:-false}",
        "LOG_LEVEL": "${LOG_LEVEL:-INFO}"
      }
    }
  }
}
```

**Use Cases:**
- Development containers
- Local development with Python installed
- CI/CD environments

This native template targets the primary MCP STDIO surface. Use
`mcp-index serve` separately only when you need the secondary FastAPI
admin/debug HTTP gateway; it is not the repo's MCP Streamable HTTP transport.
If you enable `MCP_CLIENT_SECRET`, clients must satisfy that local STDIO
handshake guard through the `handshake` tool. It does not configure the
gateway's admin/debug bearer token authentication, and no remote MCP
authorization is implemented in this repo's current transport posture.
For the current named-client and transport matrix, see
`docs/status/MCP_COMPATIBILITY_EVALUATION.md`.

### Docker Minimal Configuration

**File:** `.mcp.json.templates/docker-minimal.json`

```json
{
  "mcpServers": {
    "code-index-minimal": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "-v", "${workspace}:/workspace",
        "-v", "${HOME}/.mcp-index:/app/.mcp-index",
        "-e", "MCP_WORKSPACE_ROOT=/workspace",
        "-e", "LOG_LEVEL=${LOG_LEVEL:-INFO}",
        "-e", "MCP_ARTIFACT_SYNC=false",
        "${MCP_DOCKER_IMAGE:-ghcr.io/consiliency/code-index-mcp@sha256:<published-digest>}",
        "mcp-index",
        "stdio"
      ]
    }
  }
}
```

**Features:**
- Privacy-first (no external API calls)
- No artifact sync
- Basic code search functionality; see [SUPPORT_MATRIX.md](SUPPORT_MATRIX.md)
- Smallest image size

### Docker Standard Configuration

**File:** `.mcp.json.templates/docker-standard.json`

```json
{
  "mcpServers": {
    "code-index-standard": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "-v", "${workspace}:/workspace",
        "-v", "${HOME}/.mcp-index:/app/.mcp-index",
        "-e", "MCP_WORKSPACE_ROOT=/workspace",
        "-e", "VOYAGE_API_KEY=${VOYAGE_API_KEY:-}",
        "-e", "SEMANTIC_SEARCH_ENABLED=${SEMANTIC_SEARCH_ENABLED:-true}",
        "-e", "MCP_ARTIFACT_SYNC=${MCP_ARTIFACT_SYNC:-true}",
        "-e", "LOG_LEVEL=${LOG_LEVEL:-INFO}",
        "${MCP_DOCKER_IMAGE:-ghcr.io/consiliency/code-index-mcp@sha256:<published-digest>}",
        "mcp-index",
        "stdio"
      ]
    }
  }
}
```

**Features:**
- Semantic search with Voyage AI
- GitHub artifact synchronization
- Optimal for most use cases

### Docker Sidecar Configuration

**File:** `.mcp.json.templates/docker-sidecar.json`

```json
{
  "mcpServers": {
    "code-index-sidecar": {
      "command": "docker",
      "args": [
        "exec",
        "-i",
        "mcp-sidecar",
        "mcp-index",
        "stdio"
      ],
      "env": {
        "MCP_WORKSPACE_ROOT": "/workspace",
        "VOYAGE_API_KEY": "${VOYAGE_API_KEY:-}",
        "SEMANTIC_SEARCH_ENABLED": "${SEMANTIC_SEARCH_ENABLED:-false}",
        "LOG_LEVEL": "${LOG_LEVEL:-INFO}"
      }
    }
  }
}
```

**Use Cases:**
- Nested container scenarios
- VS Code dev containers
- Avoiding dependency conflicts

**Setup:**
```bash
# Start sidecar container
docker-compose -f docker/compose/development/docker-compose.mcp-sidecar.yml up -d

# Use sidecar configuration
cp .mcp.json.templates/docker-sidecar.json .mcp.json
```

## Manual Configuration

### Basic Structure

Every `.mcp.json` file follows this structure:

```json
{
  "mcpServers": {
    "server-name": {
      "command": "executable",
      "args": ["arg1", "arg2"],
      "cwd": "working-directory",
      "env": {
        "KEY": "value"
      }
    }
  }
}
```

### Common Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `${workspace}` | Current workspace directory | Current directory |
| `${HOME}` | User's home directory | System home |
| `${VARIABLE:-default}` | Environment variable with default | `default` if not set |

### Environment Variables

#### Core Settings

| Variable | Description | Values |
|----------|-------------|--------|
| `MCP_WORKSPACE_ROOT` | Root directory to index | Path |
| `LOG_LEVEL` | Logging verbosity | DEBUG, INFO, WARN, ERROR |
| `MCP_MAX_FILE_SIZE` | Max file size to index | Bytes (default: 10MB) |

#### Feature Flags

| Variable | Description | Default |
|----------|-------------|---------|
| `SEMANTIC_SEARCH_ENABLED` | Enable AI-powered search | false |
| `MCP_ARTIFACT_SYNC` | Enable GitHub sync | false |
| `AUTO_UPLOAD` | Auto-upload indexes | false |
| `AUTO_DOWNLOAD` | Auto-download indexes | true |

#### API Keys

| Variable | Description | Required For |
|----------|-------------|--------------|
| `VOYAGE_API_KEY` | Voyage AI API key | Semantic search |
| `GITHUB_TOKEN` | GitHub access token | Private repo artifacts |

## Advanced Configurations

### Multi-Repository Setup

For one shared server, mount or allow the unrelated repository roots, register
one worktree per git common directory, and check readiness before MCP tool use:

```json
{
  "mcpServers": {
    "code-index": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "${HOME}/projects/repo-a:/repos/repo-a",
        "-v", "${HOME}/projects/repo-b:/repos/repo-b",
        "-e", "MCP_ALLOWED_ROOTS=/repos/repo-a:/repos/repo-b",
        "ghcr.io/consiliency/code-index-mcp@sha256:<published-digest>"
      ]
    }
  }
}
```

Then register and inspect:

```bash
mcp-index repository register /repos/repo-a
mcp-index repository register /repos/repo-b
mcp-index repository list -v
```

Same-repo sibling worktrees and non-default branch queries are unsupported in
v3 indexed routing. They return `index_unavailable` with
`safe_fallback: "native_search"` and readiness remediation. If you need separate
branch checkouts as separate MCP contexts, run separate server instances rather
than one shared multi-worktree index.

### Custom Docker Images

Use locally built or custom images:

```json
{
  "mcpServers": {
    "code-index-custom": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "${workspace}:/workspace",
        "my-company/mcp-index:custom"
      ]
    }
  }
}
```

### Resource Limits

Add Docker resource constraints:

```json
{
  "mcpServers": {
    "code-index-limited": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "--memory", "2g",
        "--cpus", "2",
        "-v", "${workspace}:/workspace",
        "ghcr.io/consiliency/code-index-mcp@sha256:<published-digest>"
      ]
    }
  }
}
```

### Network Isolation

For maximum security:

```json
{
  "mcpServers": {
    "code-index-isolated": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "--network", "none",
        "-v", "${workspace}:/workspace:ro",
        "ghcr.io/consiliency/code-index-mcp@sha256:<published-digest>"
      ],
      "env": {
        "MCP_ARTIFACT_SYNC": "false"
      }
    }
  }
}
```

## Troubleshooting

### Common Issues

#### 1. Docker Command Not Found

**Symptoms:**
```
Error: command 'docker' not found
```

**Solutions:**
- Install Docker Desktop (Windows/Mac)
- Install Docker Engine (Linux)
- Use native Python configuration instead

#### 2. Permission Denied

**Symptoms:**
```
docker: permission denied while trying to connect to the Docker daemon
```

**Solutions:**
- Add user to docker group: `sudo usermod -aG docker $USER`
- Restart terminal/system
- Use `sudo` (not recommended)

#### 3. Volume Mount Issues

**Symptoms:**
```
Error: invalid mount config for type "bind"
```

**Solutions:**
- Check path exists
- Use absolute paths
- On Windows, ensure drive is shared in Docker Desktop

#### 4. MCP Connection Timeout

**Symptoms:**
```
MCP server did not respond within timeout
```

**Solutions:**
- Increase Claude Code timeout settings
- Check Docker is running
- Verify image is downloaded
- Check system resources

### Debug Mode

Enable debug logging:

```json
{
  "mcpServers": {
    "code-index-debug": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "${workspace}:/workspace",
        "-e", "LOG_LEVEL=DEBUG",
        "-e", "MCP_DEBUG=true",
        "ghcr.io/consiliency/code-index-mcp@sha256:<published-digest>"
      ]
    }
  }
}
```

### Testing Configuration

Test your configuration:

```bash
# Test MCP connection
echo '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{}}' | docker run -i --rm ghcr.io/consiliency/code-index-mcp@sha256:<published-digest>

# Expected response:
# {"jsonrpc":"2.0","id":1,"result":{"capabilities":...}}
```

## Security Considerations

### Best Practices

1. **Use Read-Only Mounts** when possible:
   ```json
   "-v", "${workspace}:/workspace:ro"
   ```

2. **Disable Network** for offline usage:
   ```json
   "--network", "none"
   ```

3. **Limit Resources** to prevent DoS:
   ```json
   "--memory", "2g",
   "--cpus", "2"
   ```

4. **Use Non-Root User** (built into our images):
   ```dockerfile
   USER mcp-user
   ```

5. **Validate Environment Variables**:
   - Never hardcode API keys
   - Use `${VAR:-}` syntax for optional vars
   - Validate paths before mounting

### Sensitive Data

The system automatically filters sensitive files:

**Always Excluded:**
- `.env` files
- `*.key`, `*.pem` files
- `.git` directory contents
- Files matching `.gitignore`
- Files matching `.mcp-index-ignore`

**Additional Security:**
```json
{
  "github_artifacts": {
    "enabled": false,
    "exclude_patterns": [
      "internal/*",
      "credentials/*",
      "*.secret"
    ]
  }
}
```

### Audit Logging

Enable security audit logs:

```json
{
  "mcpServers": {
    "code-index-audit": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "${workspace}:/workspace:ro",
        "-v", "${HOME}/mcp-audit:/app/logs",
        "-e", "MCP_AUDIT_LOG=/app/logs/audit.log",
        "-e", "MCP_SECURITY_MODE=strict",
        "ghcr.io/consiliency/code-index-mcp@sha256:<published-digest>"
      ]
    }
  }
}
```

## Next Steps

1. **Quick Start**: Run `./scripts/setup-mcp-json.sh` for automatic setup
2. **Customize**: Copy and modify templates as needed
3. **Test**: Use debug mode to verify configuration
4. **Optimize**: Adjust resource limits and features
5. **Secure**: Follow security best practices

For more information:
- [Docker Guide](DOCKER_GUIDE.md) - Docker-specific setup
- [README](../README.md) - General documentation
- [Architecture](../architecture/README.md) - System design
