# Docker Guide for MCP Index Server

This guide documents the published stable-surface Docker path for Code-Index-MCP `1.4.0`.
The documented container naming surface is `ghcr.io/consiliency/code-index-mcp`.
MCP STDIO is the primary LLM surface; FastAPI remains a secondary admin
surface. The pre-GA release boundary and evidence map are frozen in
[validation/ga-readiness-checklist.md](validation/ga-readiness-checklist.md).
The Docker path is one explicit support surface in
[SUPPORT_MATRIX.md](SUPPORT_MATRIX.md); it does not widen the documented
language/runtime or topology contract. The container name is a separate surface
from the canonical Python distribution name `index-it-mcp`.

> **Published:** `v1.4.0` is published — pull
> `ghcr.io/consiliency/code-index-mcp:v1.4.0` (or `:latest`). GHCR commands in
> this guide work as written and the installer helpers default to the published
> image. For local development and testing you can still run
> `make release-smoke-container` and use the `local-smoke` tag; the installer
> offers it as a dev option and fails clearly if it has not been built.

Public alpha supports many unrelated repositories from one server when each repo
has one registered worktree per git common directory and the container
`MCP_ALLOWED_ROOTS` covers those mounts. Only the tracked/default branch is
indexed automatically. Indexed MCP results are authoritative only when readiness
is `ready`; unavailable indexes return `index_unavailable` with
`safe_fallback: "native_search"`.

## Table of Contents

1. [Overview](#overview)
2. [Image Contract](#image-contract)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Usage](#usage)
6. [API Key Management](#api-key-management)
7. [GitHub Artifact Sync](#github-artifact-sync)
8. [MCP Integration](#mcp-integration)
9. [Troubleshooting](#troubleshooting)
10. [Advanced Topics](#advanced-topics)

## Overview

The MCP Index Server image provides a containerized solution for code indexing
without requiring local Python installation. Language support varies by plugin,
sandbox path, and optional dependencies; see [SUPPORT_MATRIX.md](SUPPORT_MATRIX.md)
for the canonical support contract.

### Why Docker?

- **Containerized setup**: Docker supplies the Python runtime and package install
- **Consistency**: Same environment for all users
- **Isolation**: No conflicts with system packages
- **Portability**: Works identically on Linux, macOS, and Windows

## Image Contract

P23 documents one image package:

```bash
ghcr.io/consiliency/code-index-mcp:v1.4.0
```

Enable optional behavior with environment variables and installed extras rather
than relying on documented variant tags. Semantic search requires provider
configuration such as `VOYAGE_API_KEY`; artifact sync requires GitHub CLI
authentication and suitable repository permissions.

## Installation

### Quick Install (Linux/macOS)

```bash
curl -sSL https://raw.githubusercontent.com/Consiliency/Code-Index-MCP/main/scripts/install-mcp-docker.sh | bash
```

### Quick Install (Windows PowerShell)

```powershell
iwr -useb https://raw.githubusercontent.com/Consiliency/Code-Index-MCP/main/scripts/install-mcp-docker.ps1 | iex
```

### Manual Installation

1. **Install Docker**:
   - Linux: `curl -fsSL https://get.docker.com | sh`
   - macOS: Download [Docker Desktop](https://www.docker.com/products/docker-desktop/)
   - Windows: Download [Docker Desktop](https://www.docker.com/products/docker-desktop/)

2. **Pull the image**:
   ```bash
   docker pull ghcr.io/consiliency/code-index-mcp:v1.4.0
   ```

3. **Create launcher script**:
   ```bash
   # Linux/macOS
   echo 'docker run -i --rm -v $(pwd):/workspace ghcr.io/consiliency/code-index-mcp:v1.4.0 "$@"' > /usr/local/bin/mcp-index
   chmod +x /usr/local/bin/mcp-index
   ```

## Configuration

### Environment Variables

Configure the Docker container using environment variables:

```bash
# Basic configuration
docker run -it \
  -e LOG_LEVEL=DEBUG \
  -e MCP_WORKSPACE_ROOT=/workspace \
  ghcr.io/consiliency/code-index-mcp:v1.4.0
```

### Configuration File (.env)

Create a `.env` file in your project root:

```env
# Semantic Search (optional)
VOYAGE_API_KEY=your-api-key-here
SEMANTIC_SEARCH_ENABLED=true

# GitHub Artifact Sync
MCP_ARTIFACT_SYNC=true
AUTO_UPLOAD=true
AUTO_DOWNLOAD=true

# Performance
INDEXING_MAX_WORKERS=8
INDEXING_BATCH_SIZE=50

# Logging
LOG_LEVEL=INFO
```

The Docker container automatically loads `.env` from the workspace.

### MCP Index Configuration (.mcp-index.json)

Control index behavior and sharing:

```json
{
  "version": "1.0",
  "enabled": true,
  "github_artifacts": {
    "enabled": true,
    "auto_upload": false,
    "auto_download": true,
    "compression": true,
    "max_size_mb": 100,
    "exclude_patterns": [
      "internal/*",
      "*.secret"
    ]
  },
  "languages": "auto",
  "exclude_defaults": true
}
```

## Usage

### Basic Commands

```bash
# Index current directory
mcp-index

# Index specific directory
mcp-index /path/to/code

# Show version
mcp-index --version

# Run setup wizard
mcp-index setup

# Upgrade to latest version
mcp-index upgrade
```

### Docker Run Examples

```bash
# Minimal version - basic search
docker run -it -v $(pwd):/workspace ghcr.io/consiliency/code-index-mcp:v1.4.0

# Standard version - with API key
docker run -it \
  -v $(pwd):/workspace \
  -e VOYAGE_API_KEY=your-key \
  ghcr.io/consiliency/code-index-mcp:v1.4.0

# Full version - with external services
docker run -it \
  -v $(pwd):/workspace \
  -e VOYAGE_API_KEY=your-key \
  -e REDIS_URL=redis://redis:6379 \
  -e QDRANT_URL=http://qdrant:6333 \
  -e QDRANT_API_KEY \
  ghcr.io/consiliency/code-index-mcp:v1.4.0
```

For many unrelated repositories, mount each root deliberately, set
`MCP_ALLOWED_ROOTS`, register each repo, and inspect readiness before MCP tool
use:

```bash
docker run -it \
  -v /abs/repo-a:/repos/repo-a \
  -v /abs/repo-b:/repos/repo-b \
  -e MCP_ALLOWED_ROOTS=/repos/repo-a:/repos/repo-b \
  ghcr.io/consiliency/code-index-mcp:v1.4.0

mcp-index repository register /repos/repo-a
mcp-index repository register /repos/repo-b
mcp-index repository list -v
```

Register one worktree per git common directory. Same-repo sibling worktrees and
non-default branch queries return `index_unavailable` with
`safe_fallback: "native_search"` instead of reusing another checkout's index.

### Docker Compose

For production deployments, use Docker Compose:

```yaml
version: '3.8'

services:
  mcp-index:
    image: ghcr.io/consiliency/code-index-mcp:v1.4.0
    volumes:
      - ./:/workspace
      - ~/.mcp-index:/app/.mcp-index
    environment:
      - VOYAGE_API_KEY=${VOYAGE_API_KEY}
      - MCP_ARTIFACT_SYNC=true
    stdin_open: true
    tty: true
```

## API Key Management

### Voyage AI Setup

1. **Get an API Key**:
   - Visit https://www.voyageai.com/
   - Sign up for free account
   - Generate API key in dashboard

2. **Pricing**:
   - Free tier: 50M tokens/month (~50K embeddings)
   - Paid: $0.05 per 1M tokens
   - 1 token ≈ 1 character of code

3. **Configure the Key**:
   ```bash
   # Option 1: Environment variable
   export VOYAGE_API_KEY=your-key
   
   # Option 2: Docker run
   docker run -e VOYAGE_API_KEY=your-key ...
   
   # Option 3: .env file
   echo "VOYAGE_API_KEY=your-key" >> .env
   ```

### Cost Estimation

| Codebase Size | Files | Embeddings | Monthly Cost |
|---------------|-------|------------|--------------|
| Small (1MB) | ~100 | ~1,000 | Free tier |
| Medium (10MB) | ~1,000 | ~10,000 | Free tier |
| Large (100MB) | ~10,000 | ~100,000 | ~$5/month |
| Huge (1GB) | ~100,000 | ~1,000,000 | ~$50/month |

## GitHub Artifact Sync

### Privacy Settings

Control index sharing per repository:

```json
// .mcp-index.json
{
  "github_artifacts": {
    "enabled": false,     // Completely disable sync
    "auto_upload": false, // Manual upload only
    "auto_download": true // Still receive team indexes
  }
}
```

### Manual Sync Commands

```bash
# Check sync status
docker run -it -v $(pwd):/workspace ghcr.io/consiliency/code-index-mcp:v1.4.0 artifact status

# Download latest index
docker run -it -v $(pwd):/workspace ghcr.io/consiliency/code-index-mcp:v1.4.0 artifact pull

# Upload your index
docker run -it -v $(pwd):/workspace ghcr.io/consiliency/code-index-mcp:v1.4.0 artifact push
```

### Security Features

- Automatic .gitignore filtering
- Additional patterns via .mcp-index-ignore
- Audit logs of excluded files
- No secrets in shared indexes

## MCP Integration

### Claude Code Configuration

Create `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "code-index": {
      "command": "docker",
      "args": [
        "run", 
        "-i", 
        "--rm",
        "-v", "${workspace}:/workspace",
        "-v", "${HOME}/.mcp-index:/app/.mcp-index",
        "-e", "VOYAGE_API_KEY=${VOYAGE_API_KEY:-}",
        "-e", "MCP_ARTIFACT_SYNC=${MCP_ARTIFACT_SYNC:-true}",
        "ghcr.io/consiliency/code-index-mcp:v1.4.0"
      ]
    }
  }
}
```

### Environment Variables in MCP

Claude Code passes environment variables to the Docker container:

```json
{
  "mcpServers": {
    "code-index": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "..."],
      "env": {
        "VOYAGE_API_KEY": "your-key",
        "LOG_LEVEL": "DEBUG"
      }
    }
  }
}
```

## Nested Container Scenarios

### Overview

Nested container scenarios occur when you're already running inside a container (like a VS Code dev container) and need to run MCP Docker images. This is common in modern development workflows.

### Common Scenarios

1. **VS Code Dev Containers** in WSL2 with Docker Desktop
2. **GitHub Codespaces** or similar cloud environments
3. **CI/CD pipelines** running in containers
4. **Docker-in-Docker** development environments

### Solutions

#### Option 1: Native Python (Recommended for Dev Containers)

If you're already in a development container with Python:

```json
// .mcp.json
{
  "mcpServers": {
    "code-index-native": {
      "command": "python",
      "args": ["scripts/cli/mcp_server_cli.py"],
      "cwd": "${workspace}"
    }
  }
}
```

**Advantages:**
- No nested Docker complexity
- Direct file access
- Faster startup
- No permission issues

#### Option 2: Docker Sidecar Pattern

Run MCP as a separate service alongside your dev container:

1. **Start the sidecar container:**
   ```bash
   docker-compose -f docker/compose/development/docker-compose.mcp-sidecar.yml up -d
   ```

2. **Configure MCP to use the sidecar:**
   ```json
   // .mcp.json
   {
     "mcpServers": {
       "code-index-sidecar": {
         "command": "docker",
         "args": ["exec", "-i", "mcp-sidecar", "python", "/app/scripts/cli/mcp_server_cli.py"]
       }
     }
   }
   ```

**Advantages:**
- Isolated dependencies
- Consistent environment
- No conflicts with dev container packages

#### Option 3: Docker Socket Mounting

Share the Docker socket with your dev container:

```yaml
# .devcontainer/docker-compose.yml
version: '3.8'
services:
  devcontainer:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - .:/workspace
```

Then use standard Docker commands inside the container.

**⚠️ Security Warning:** Mounting Docker socket grants root-equivalent access.

#### Option 4: Remote Docker Context

Connect to Docker running on the host:

```bash
# Inside dev container
export DOCKER_HOST=tcp://host.docker.internal:2375
docker run -it -v /mnt/c/path/to/code:/workspace ghcr.io/consiliency/code-index-mcp:v1.4.0
```

**Note:** Requires Docker Desktop with exposed daemon.

### WSL2 Specific Configuration

For WSL2 with Docker Desktop:

1. **Ensure WSL integration is enabled** in Docker Desktop settings
2. **Use WSL paths** for volume mounts:
   ```bash
   # Convert Windows path to WSL path
   docker run -it -v /mnt/c/Users/name/project:/workspace ghcr.io/consiliency/code-index-mcp:v1.4.0
   ```

3. **Configure .mcp.json for WSL:**
   ```json
   {
     "mcpServers": {
       "code-index-wsl": {
         "command": "docker",
         "args": [
           "run", "-i", "--rm",
           "-v", "${workspace}:/workspace",
           "-v", "/mnt/c/Users/${USER}/.mcp-index:/app/.mcp-index",
           "ghcr.io/consiliency/code-index-mcp:v1.4.0"
         ]
       }
     }
   }
   ```

### Automatic Configuration

Use the setup script to auto-detect and configure:

```bash
# Detects nested container environment
./scripts/setup-mcp-json.sh

# Output:
# Detected environment: devcontainer
# Using native Python configuration for optimal performance
# Created .mcp.json for devcontainer environment
```

### Best Practices

1. **Check environment first:**
   ```bash
   # Are we in a container?
   if [ -f /.dockerenv ]; then
     echo "Running in container"
   fi
   ```

2. **Use appropriate configuration:**
   - Dev containers → Native Python
   - Production → Docker sidecar
   - CI/CD → Docker-in-Docker

3. **Avoid recursive containers:**
   - Don't run Docker inside Docker inside Docker
   - Use sidecar pattern instead

4. **Handle permissions carefully:**
   - Match UID/GID between containers
   - Use named volumes for persistent data

### Performance Considerations

| Method | Startup Time | Resource Usage | Complexity |
|--------|-------------|----------------|------------|
| Native Python | Fast (1-2s) | Low | Low |
| Docker Sidecar | Medium (5-10s) | Medium | Medium |
| Docker-in-Docker | Slow (10-20s) | High | High |
| Remote Docker | Fast (2-5s) | Low | Medium |

### Debugging Nested Containers

```bash
# Check Docker availability
docker version

# Test MCP connection
echo '{"jsonrpc":"2.0","method":"initialize","id":1}' | \
  docker run -i --rm ghcr.io/consiliency/code-index-mcp:v1.4.0

# Verify volume mounts
docker run --rm -v $(pwd):/workspace alpine ls -la /workspace

# Check container logs
docker logs mcp-sidecar
```

## Troubleshooting

### Common Issues

#### 1. Docker not found
```bash
Error: Docker command not found
Solution: Install Docker Desktop or Docker Engine
```

#### 2. Permission denied
```bash
Error: docker: permission denied
Solution: Add user to docker group: sudo usermod -aG docker $USER
```

#### 3. API key not working
```bash
Warning: Semantic search requires VOYAGE_API_KEY
Solution: Verify key is set: echo $VOYAGE_API_KEY
```

#### 4. Out of memory
```bash
Error: Container killed (OOM)
Solution: Increase Docker memory limit in Docker Desktop settings
```

### Debug Mode

Enable detailed logging:

```bash
docker run -it \
  -e LOG_LEVEL=DEBUG \
  -e MCP_LOG_FILE=/workspace/mcp-debug.log \
  -v $(pwd):/workspace \
  ghcr.io/consiliency/code-index-mcp:v1.4.0
```

### Health Checks

Check container health:

```bash
# Check if MCP server is responsive
docker run --rm ghcr.io/consiliency/code-index-mcp:v1.4.0 --health

# Test database connection
docker run --rm -v $(pwd):/workspace ghcr.io/consiliency/code-index-mcp:v1.4.0 test db

# Verify index status
docker run --rm -v $(pwd):/workspace ghcr.io/consiliency/code-index-mcp:v1.4.0 index status
```

## Advanced Topics

### Custom Dockerfile

Extend the base images for your needs:

```dockerfile
FROM ghcr.io/consiliency/code-index-mcp:v1.4.0

# Add custom dependencies
RUN pip install --no-cache-dir custom-package

# Add custom configuration
COPY my-config.yaml /app/config/

# Custom entrypoint
COPY entrypoint.sh /app/
ENTRYPOINT ["/app/entrypoint.sh"]
```

### Multi-Architecture Builds

The images support multiple architectures:

```bash
# Build for multiple platforms
docker buildx build \
  --platform linux/amd64,linux/arm64,linux/arm/v7 \
  -t my-mcp-index:latest \
  -f docker/dockerfiles/Dockerfile.minimal \
  .
```

### Volume Mounts

Advanced volume configurations:

```bash
# Persistent index across runs
docker run -it \
  -v $(pwd):/workspace \
  -v mcp-index-data:/app/data \
  -v mcp-index-cache:/app/.cache \
  ghcr.io/consiliency/code-index-mcp:v1.4.0

# Read-only workspace
docker run -it \
  -v $(pwd):/workspace:ro \
  ghcr.io/consiliency/code-index-mcp:v1.4.0
```

### Network Configuration

For production deployments:

```bash
# Create isolated network
docker network create mcp-network

# Run with Redis
docker run -d --name redis --network mcp-network redis:alpine

# Run MCP with Redis
docker run -it \
  --network mcp-network \
  -e REDIS_URL=redis://redis:6379 \
  ghcr.io/consiliency/code-index-mcp:v1.4.0
```

### Performance Tuning

Optimize for large codebases:

```bash
docker run -it \
  -e INDEXING_MAX_WORKERS=16 \
  -e INDEXING_BATCH_SIZE=100 \
  -e CACHE_MAX_MEMORY_MB=1000 \
  --cpus="4" \
  --memory="8g" \
  ghcr.io/consiliency/code-index-mcp:v1.4.0
```

## Support

- **Documentation**: https://github.com/Code-Index-MCP/docs
- **Issues**: https://github.com/Code-Index-MCP/issues
- **Discord**: https://discord.gg/code-index-mcp
- **Email**: support@code-index-mcp.io
