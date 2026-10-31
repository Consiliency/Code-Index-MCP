# Docker Support for MCP Index Server

This directory contains Docker configurations for the MCP Index Server, providing containerized deployment options for different use cases.

## Directory Structure

```
docker/
├── dockerfiles/           # Dockerfile variants
│   ├── Dockerfile.minimal # Zero-configuration version
│   ├── Dockerfile.standard # Semantic search enabled
│   └── Dockerfile.full    # Complete production stack
├── compose/              # Docker Compose files
│   ├── development/      # Dev environment configs
│   └── production/       # Production configs
└── config/               # Configuration templates
```

## Quick Start

### Using Pre-built Images

> **Published:** `v1.4.0` is published — pull
> `ghcr.io/consiliency/code-index-mcp:v1.4.0` (or `:latest`). The GHCR commands
> below work as written. For local development and testing you can also build the
> image with `make release-smoke-container` and use the `local-smoke` tag — see
> `docs/DOCKER_GUIDE.md`.

```bash
# Basic search (no API keys required) - 2 minute setup
docker run -it -v $(pwd):/workspace ghcr.io/consiliency/code-index-mcp:v1.4.0

# AI-powered search (requires Voyage AI key; lexical search is the image default)
docker run -it -v $(pwd):/workspace -e SEMANTIC_SEARCH_ENABLED=true -e VOYAGE_API_KEY=your-key ghcr.io/consiliency/code-index-mcp:v1.4.0

# Install helper script (recommended)
curl -sSL https://raw.githubusercontent.com/Consiliency/Code-Index-MCP/main/scripts/install-mcp-docker.sh | bash
mcp-index  # Now available as a command
```

### Release Smoke

```bash
make release-smoke
make release-smoke-container
```

### Development Setup (All Features)
```bash
# Set your Voyage AI API key for semantic search
export VOYAGE_API_KEY="your-voyage-ai-key"

# Start development environment with all features
docker-compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# Access with monitoring
# - MCP Server: http://localhost:8000
# - Prometheus: http://localhost:9090
# - Grafana: http://localhost:3000 (admin/dev)
# - Redis Commander: http://localhost:8081
```

### Production Setup
```bash
# Copy and customize production environment
cp .env.production.example .env.production
# Edit .env.production with your production values

# Start production stack
docker-compose --env-file .env.production up -d
```

## Services Overview

### Core Services

| Service | Port | Description | Health Check |
|---------|------|-------------|--------------|
| mcp-server | 8000 | Main MCP Server API | `/health` |
| redis | 6379 | Cache and session storage | `redis-cli ping` |
| qdrant | 6333, 6334 (loopback) | Vector database for semantic search; requires `QDRANT_API_KEY` (see `docs/operations/qdrant-authentication.md`) | `/readyz` |

### Monitoring Services (Optional)

| Service | Port | Description | Profile |
|---------|------|-------------|---------|
| prometheus | 9090 | Metrics collection | `monitoring` |
| grafana | 3000 | Metrics visualization | `monitoring` |
| redis-commander | 8081 | Redis GUI | `debug` |

## Dormant Features Activation

### 1. Semantic Search
Requires Voyage AI API key and Qdrant:

```bash
# Set environment variables
export VOYAGE_API_KEY="your-key"
export SEMANTIC_SEARCH_ENABLED=true

# Or add to .env file
echo "VOYAGE_API_KEY=your-key" >> .env
echo "SEMANTIC_SEARCH_ENABLED=true" >> .env

# Restart services
docker-compose restart mcp-server
```

### 2. Redis Caching
Enabled by default in docker-compose:

```bash
# Verify Redis is working
docker-compose exec redis redis-cli ping
# Should return: PONG

# Check cache stats
curl http://localhost:8000/cache/stats
```

### 3. Advanced Indexing
Configure via environment variables:

```bash
# Enable advanced indexing features
export INDEXING_GENERATE_EMBEDDINGS=true
export INDEXING_MAX_WORKERS=8
export INDEXING_BATCH_SIZE=20

# Apply configuration
docker-compose restart mcp-server
```

## Environment Configuration

### Development (.env)
```bash
cp .env.example .env
# Edit .env for development settings
```

### Production (.env.production)
```bash
cp .env.production.example .env.production
# Edit .env.production for production settings

# Use production environment
docker-compose --env-file .env.production up -d
```

## Running Tests

### Basic Tests
```bash
# Run tests in container
docker-compose -f docker-compose.yml -f docker-compose.dev.yml run --rm test-runner

# Run specific test suites
docker-compose -f docker-compose.yml -f docker-compose.dev.yml run --rm test-runner \
  python -m pytest tests/real_world/ -v
```

### Dormant Features Tests
```bash
# Set required environment variables
export VOYAGE_API_KEY="your-key"
export REDIS_URL="redis://redis:6379"

# Run dormant features validation
docker-compose -f docker-compose.yml -f docker-compose.dev.yml run --rm test-runner \
  python run_parallel_tests.py --phases dormant_features_validation
```

### Performance Tests
```bash
# Run with real-world repositories
docker-compose -f docker-compose.yml -f docker-compose.dev.yml run --rm test-runner \
  python run_parallel_tests.py --phases real_world_validation --setup-real-world
```

## Data Persistence

### Volumes
- `redis-data`: Redis cache and session data
- `qdrant-data`: Vector embeddings and collections
- `prometheus-data`: Metrics historical data
- `grafana-data`: Dashboard configurations
- `./data`: SQLite database and application data

### Backup
```bash
# Backup all data
docker run --rm -v mcp_redis-data:/data -v $(pwd)/backup:/backup alpine \
  tar czf /backup/redis-$(date +%Y%m%d).tar.gz -C /data .

docker run --rm -v mcp_qdrant-data:/data -v $(pwd)/backup:/backup alpine \
  tar czf /backup/qdrant-$(date +%Y%m%d).tar.gz -C /data .

# Backup SQLite database
cp ./data/code_index.db ./backup/code_index-$(date +%Y%m%d).db
```

## Troubleshooting

### Common Issues

#### 1. Semantic Search Not Working
```bash
# Check Voyage AI API key
docker-compose exec mcp-server env | grep VOYAGE_API_KEY

# Check Qdrant connection
docker-compose exec mcp-server curl -f http://qdrant:6333/readyz

# Check logs
docker-compose logs mcp-server | grep -i semantic
```

#### 2. Redis Connection Issues
```bash
# Check Redis health
docker-compose exec redis redis-cli ping

# Check connection from app
docker-compose exec mcp-server redis-cli -h redis ping

# Check Redis logs
docker-compose logs redis
```

#### 3. Memory Issues
```bash
# Check container memory usage
docker stats

# Adjust memory limits in docker-compose.yml
# Add to service definition:
deploy:
  resources:
    limits:
      memory: 2G
```

#### 4. Performance Issues
```bash
# Check all service health
docker-compose ps

# Monitor resource usage
docker stats --no-stream

# Check application metrics
curl http://localhost:8000/metrics/json
```

### Logs and Debugging

```bash
# View all logs
docker-compose logs -f

# View specific service logs
docker-compose logs -f mcp-server
docker-compose logs -f redis
docker-compose logs -f qdrant

# Enter container for debugging
docker-compose exec mcp-server bash
docker-compose exec redis redis-cli
```

## Development Workflow

### 1. Initial Setup
```bash
# Clone repository and set up environment
git clone <repository>
cd Code-Index-MCP
cp .env.example .env
# Edit .env with your settings

# Get Voyage AI API key (for semantic search)
# Register at https://www.voyageai.com/
export VOYAGE_API_KEY="your-key"
```

### 2. Start Development Environment
```bash
# Start all services including monitoring
docker-compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# Check all services are healthy
docker-compose ps
```

### 3. Run Tests
```bash
# Run full test suite
make test

# Run specific test categories
make test-dormant-features
make test-real-world
make test-performance
```

### 4. Monitor and Debug
```bash
# Access monitoring dashboards
open http://localhost:3000  # Grafana
open http://localhost:9090  # Prometheus
open http://localhost:8081  # Redis Commander

# View real-time logs
make logs

# Check service health
make health-check
```

## Production Deployment

### 1. Security Considerations
- Change all default passwords
- Use strong JWT secrets
- Configure proper CORS origins
- Use SSL/TLS certificates
- Set up firewall rules
- Use managed services for Redis/Qdrant when possible

### 2. Scaling Considerations
- Use Redis cluster for high availability
- Use Qdrant Cloud for managed vector database
- Configure proper resource limits
- Set up load balancing
- Monitor memory and CPU usage

### 3. Monitoring Setup
- Configure Prometheus for metrics collection
- Set up Grafana dashboards
- Configure alerting rules
- Monitor service health endpoints
- Set up log aggregation

For more detailed deployment instructions, see the [Deployment Guide](../docs/DEPLOYMENT-GUIDE.md).
