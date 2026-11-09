# Environment Variables Reference

This document provides a comprehensive reference of all environment variables supported by Code-Index-MCP, including their purposes, default values, and usage examples.

## Core Server Configuration

### `MCP_SERVER_HOST`
- **Description**: The host address for the MCP FastAPI server
- **Type**: String
- **Default**: `"127.0.0.1"`
- **Example**: 
  ```bash
  export MCP_SERVER_HOST="0.0.0.0"  # Listen on all interfaces
  ```

### `MCP_SERVER_PORT`
- **Description**: The port number for the MCP FastAPI server
- **Type**: Integer
- **Default**: `8000`
- **Example**: 
  ```bash
  export MCP_SERVER_PORT=8080
  ```

### `MCP_LOG_LEVEL`
- **Description**: Logging level for the application
- **Type**: String
- **Default**: `"INFO"`
- **Valid Values**: `"DEBUG"`, `"INFO"`, `"WARNING"`, `"ERROR"`, `"CRITICAL"`
- **Example**: 
  ```bash
  export MCP_LOG_LEVEL="DEBUG"
  ```

## File System Configuration

### `MCP_WORKSPACE_ROOT`
- **Description**: Root directory for code indexing operations
- **Type**: Path
- **Default**: Current working directory
- **Example**: 
  ```bash
  export MCP_WORKSPACE_ROOT="/home/user/projects"
  ```

### `MCP_ALLOWED_ROOTS`
- **Description**: OS-path-separator-separated list of allowed roots for indexing and read tools (security feature)
- **Type**: String (`:` on Unix, `;` on Windows; comma accepted only as a legacy fallback)
- **Default**: Current workspace only
- **Example**: 
  ```bash
  export MCP_ALLOWED_ROOTS="/home/user/projects:/workspace"
  ```

### `MCP_DENIED_PATTERNS`
- **Description**: Comma-separated list of file patterns to exclude from indexing
- **Type**: String (comma-separated glob patterns)
- **Default**: `"*.key,*.pem,.env*,*secrets*"`
- **Example**: 
  ```bash
  export MCP_DENIED_PATTERNS="*.key,*.pem,.env*,*secrets*,*.log"
  ```

### `MCP_MAX_FILE_SIZE`
- **Description**: Maximum file size to index (in MB)
- **Type**: Integer
- **Default**: `10`
- **Example**: 
  ```bash
  export MCP_MAX_FILE_SIZE=20  # Allow files up to 20MB
  ```

## Indexing Configuration

### `MCP_INDEX_THREADS`
- **Description**: Number of threads for parallel file indexing
- **Type**: Integer
- **Default**: Number of CPU cores
- **Example**: 
  ```bash
  export MCP_INDEX_THREADS=8
  ```

### `MCP_INDEX_BATCH_SIZE`
- **Description**: Number of files to process in a single batch
- **Type**: Integer
- **Default**: `100`
- **Example**: 
  ```bash
  export MCP_INDEX_BATCH_SIZE=50
  ```

### `MCP_PARSE_TIMEOUT`
- **Description**: Maximum time (in seconds) to parse a single file
- **Type**: Integer
- **Default**: `30`
- **Example**: 
  ```bash
  export MCP_PARSE_TIMEOUT=60
  ```

## Database Configuration

### `MCP_QDRANT_PATH`
- **Description**: Path to Qdrant vector database storage
- **Type**: Path or `:memory:`
- **Default**: `:memory:` (in-memory database)
- **Example**: 
  ```bash
  export MCP_QDRANT_PATH="/var/lib/mcp/qdrant"
  ```

### `MCP_QDRANT_COLLECTION`
- **Description**: Name of the Qdrant collection for code embeddings
- **Type**: String
- **Default**: `"code-index"`
- **Example**: 
  ```bash
  export MCP_QDRANT_COLLECTION="my-project-index"
  ```

## API Keys and External Services

### `VOYAGEAI_API_KEY`
- **Description**: API key for Voyage AI code embeddings service
- **Type**: String
- **Default**: None (required for semantic search)
- **Example**: 
  ```bash
  export VOYAGEAI_API_KEY="pa-xxxxxxxxxxxxx"
  ```
- **Security**: Store in secure keyring or secrets manager

### `OPENAI_API_KEY`
- **Description**: OpenAI API key (for future LLM features)
- **Type**: String
- **Default**: None
- **Example**: 
  ```bash
  export OPENAI_API_KEY="sk-xxxxxxxxxxxxx"
  ```
- **Security**: Never commit to version control

### `GOOGLE_API_KEY`
- **Description**: Google API key (for future features)
- **Type**: String
- **Default**: None
- **Example**: 
  ```bash
  export GOOGLE_API_KEY="AIzaxxxxxxxxxxxxx"
  ```
- **Security**: Use environment-specific keys

## Cache Configuration

### `MCP_CACHE_SIZE`
- **Description**: Maximum number of entries in query cache
- **Type**: Integer
- **Default**: `10000`
- **Example**: 
  ```bash
  export MCP_CACHE_SIZE=5000
  ```

### `MCP_CACHE_TTL`
- **Description**: Cache time-to-live in seconds
- **Type**: Integer
- **Default**: `300` (5 minutes)
- **Example**: 
  ```bash
  export MCP_CACHE_TTL=600  # 10 minutes
  ```

## Performance Tuning

### `MCP_MEMORY_LIMIT`
- **Description**: Maximum memory usage for indexing operations (in MB)
- **Type**: Integer
- **Default**: `2048` (2GB)
- **Example**: 
  ```bash
  export MCP_MEMORY_LIMIT=4096  # 4GB
  ```

### `MCP_CPU_LIMIT`
- **Description**: Maximum CPU usage percentage
- **Type**: Integer (0-100)
- **Default**: `50`
- **Example**: 
  ```bash
  export MCP_CPU_LIMIT=75  # Use up to 75% CPU
  ```

### `MCP_RATE_LIMIT`
- **Description**: API rate limit (requests per minute)
- **Type**: Integer
- **Default**: `1000`
- **Example**: 
  ```bash
  export MCP_RATE_LIMIT=500
  ```

## Security Configuration

### `MCP_AUTH_ENABLED`
- **Description**: Enable API authentication
- **Type**: Boolean (`true`/`false`)
- **Default**: `false`
- **Example**: 
  ```bash
  export MCP_AUTH_ENABLED=true
  ```

### `MCP_JWT_SECRET`
- **Description**: Secret key for JWT token signing
- **Type**: String
- **Default**: None (required if auth enabled)
- **Example**: 
  ```bash
  export MCP_JWT_SECRET="your-secret-key-here"
  ```
- **Security**: Generate with `openssl rand -hex 32`

### `MCP_TOKEN_EXPIRY`
- **Description**: JWT token expiration time (in hours)
- **Type**: Integer
- **Default**: `24`
- **Example**: 
  ```bash
  export MCP_TOKEN_EXPIRY=48  # 2 days
  ```

## Plugin Configuration

### `MCP_ENABLED_PLUGINS`
- **Description**: Comma-separated list of enabled language plugins
- **Type**: String
- **Default**: `"python,javascript,typescript,c,cpp,dart,html,css"`
- **Example**: 
  ```bash
  export MCP_ENABLED_PLUGINS="python,javascript"  # Only enable specific plugins
  ```

### `MCP_PLUGIN_TIMEOUT`
- **Description**: Maximum execution time for plugin operations (seconds)
- **Type**: Integer
- **Default**: `30`
- **Example**: 
  ```bash
  export MCP_PLUGIN_TIMEOUT=60
  ```

## Development and Debug

### `MCP_DEBUG`
- **Description**: Enable debug mode with additional logging
- **Type**: Boolean (`true`/`false`)
- **Default**: `false`
- **Example**: 
  ```bash
  export MCP_DEBUG=true
  ```

### `MCP_PROFILE`
- **Description**: Enable performance profiling
- **Type**: Boolean (`true`/`false`)
- **Default**: `false`
- **Example**: 
  ```bash
  export MCP_PROFILE=true
  ```

### `MCP_TEST_MODE`
- **Description**: Run in test mode with mock services
- **Type**: Boolean (`true`/`false`)
- **Default**: `false`
- **Example**: 
  ```bash
  export MCP_TEST_MODE=true
  ```

## Artifact & Watcher Configuration

### `MCP_ARTIFACT_FULL_SIZE_LIMIT`
- **Description**: Byte threshold above which the artifact publisher switches to delta mode (requires a previously recorded artifact ID). Introduced in P14 SL-4 (`mcp_server/artifacts/delta_policy.py`).
- **Type**: Integer (bytes)
- **Default**: `524288000` (500 MB)
- **Example**:
  ```bash
  export MCP_ARTIFACT_FULL_SIZE_LIMIT=104857600  # Force delta above 100 MB
  ```

### `MCP_WATCHER_SWEEP_MINUTES`
- **Description**: Interval in minutes between full-tree periodic sweeps performed by `WatcherSweeper` to recover events dropped by inotify/FSEvents. Introduced in P14 SL-5 (`mcp_server/watcher/sweeper.py`). The sweep compares the index with exactly the files the indexer stores (the shared admission predicate), and does not reconcile identical drift again until the tracked tree changes. Drift is only treated as handled after a reconciling rebuild actually published; a failed or refused rebuild is retried on the next sweep, then with backoff (skipping 1, 3, 7 and at most 15 sweeps) until one succeeds or the tree changes (Consiliency/Code-Index-MCP#114).
- **Type**: Integer (minutes)
- **Default**: `60`
- **Example**:
  ```bash
  export MCP_WATCHER_SWEEP_MINUTES=30  # Sweep every 30 minutes
  ```

### `MCP_INDEX_GENERATION_RETENTION`
- **Description**: Number of previous index generations kept for rollback, besides the live one, when a rebuild publishes a new generation. Only generations recorded in `generations/generation-history.json` (published or failed by this version) are ever pruned, together with their SQLite sidecars and local semantic directory; the newest failed generation is kept as evidence. Never pruned: generations without a record (for example from before the upgrade), generations any registry entry references, generations pinned with an empty `generations/<id>.keep` file, and generations a reader may still hold open. Quarantine copies and Qdrant server collections are not touched (Consiliency/Code-Index-MCP#114).
- **Type**: Integer
- **Default**: `2` (values below `1` are treated as `1`)
- **Example**:
  ```bash
  export MCP_INDEX_GENERATION_RETENTION=3  # Keep live plus three previous generations
  ```

## Cloud Sync Configuration (Optional)

### `MCP_CLOUD_SYNC_ENABLED`
- **Description**: Enable cloud synchronization features
- **Type**: Boolean (`true`/`false`)
- **Default**: `false`
- **Example**: 
  ```bash
  export MCP_CLOUD_SYNC_ENABLED=true
  ```

### `MCP_CLOUD_ENDPOINT`
- **Description**: Cloud sync service endpoint
- **Type**: URL
- **Default**: None
- **Example**: 
  ```bash
  export MCP_CLOUD_ENDPOINT="https://sync.example.com"
  ```

### `MCP_CLOUD_API_KEY`
- **Description**: API key for cloud sync service
- **Type**: String
- **Default**: None
- **Example**: 
  ```bash
  export MCP_CLOUD_API_KEY="cloud-key-xxxxx"
  ```

## Usage Examples

### Basic Development Setup
```bash
# .env.development
MCP_SERVER_HOST=127.0.0.1
MCP_SERVER_PORT=8000
MCP_LOG_LEVEL=DEBUG
MCP_DEBUG=true
VOYAGEAI_API_KEY=pa-xxxxxxxxxxxxx
```

### Production Setup
```bash
# .env.production
MCP_SERVER_HOST=0.0.0.0
MCP_SERVER_PORT=8080
MCP_LOG_LEVEL=INFO
MCP_AUTH_ENABLED=true
MCP_JWT_SECRET=$(openssl rand -hex 32)
MCP_QDRANT_PATH=/var/lib/mcp/qdrant
MCP_MEMORY_LIMIT=4096
MCP_CPU_LIMIT=75
VOYAGEAI_API_KEY=${VOYAGEAI_API_KEY}  # From secrets manager
```

### Testing Setup
```bash
# .env.test
MCP_TEST_MODE=true
MCP_QDRANT_PATH=:memory:
MCP_LOG_LEVEL=WARNING
MCP_PARSE_TIMEOUT=5
MCP_INDEX_THREADS=2
```

## Environment File Loading

The application supports loading environment variables from files in the following order:

1. System environment variables
2. `.env` file in the project root
3. `.env.local` file (for local overrides, gitignored)
4. Environment-specific files (e.g., `.env.development`, `.env.production`)

Example `.env` file:
```bash
# Core configuration
MCP_SERVER_PORT=8000
MCP_LOG_LEVEL=INFO

# Security
MCP_AUTH_ENABLED=false
MCP_ALLOWED_ROOTS=/workspace

# Performance
MCP_INDEX_THREADS=4
MCP_CACHE_SIZE=10000

# External services (use secure storage in production)
VOYAGEAI_API_KEY=your-api-key-here
```

## Best Practices

1. **Security**: Never commit sensitive API keys to version control
2. **Defaults**: Always provide sensible defaults for optional variables
3. **Validation**: Validate environment variables at startup
4. **Documentation**: Keep this document updated with new variables
5. **Typing**: Use appropriate types (string, integer, boolean, path)
6. **Naming**: Use `MCP_` prefix for all application-specific variables

## P17 enforced (wired in P17)

### `MCP_MAX_FILE_SIZE_BYTES`
- **Description**: Maximum size (in bytes) of a single file for indexing operations
- **Type**: Integer
- **Default**: `10485760` (10 MiB)
- **Getter**: `mcp_server.config.env_vars.get_max_file_size_bytes()`
- **Enforced**: Dispatcher walker (`index_directory`) skips files larger than this
  value; oversize files are logged at WARNING level and excluded from the index without
  stalling the indexer. Wired in P17 SL-2 (`mcp_server/dispatcher/dispatcher_enhanced.py`).

## P18 enforced (Enforcement + Artifact Resilience + Ops)

### `MCP_PLUGIN_SANDBOX_DISABLE`
- **Description**: Opt out of plugin sandboxing (disabled=1 means run unsandboxed)
- **Type**: Boolean (`0`/`1`)
- **Default**: `0` (sandbox ON)
- **Example**:
  ```bash
  export MCP_PLUGIN_SANDBOX_DISABLE=1  # Disable sandbox (not recommended)
  ```
- **Security**: Default-on sandbox prevents plugins from accessing the filesystem, network, and subprocess APIs outside approved capabilities. Only disable if you fully trust all loaded plugins.

### `MCP_ARTIFACT_RETENTION_COUNT`
- **Description**: Maximum number of artifact revisions to retain per artifact (used by retention janitor)
- **Type**: Integer
- **Default**: `10`
- **Getter**: `mcp_server.config.env_vars.get_artifact_retention_count()`
- **Example**:
  ```bash
  export MCP_ARTIFACT_RETENTION_COUNT=5  # Keep only the 5 most recent revisions
  ```

### `MCP_ARTIFACT_RETENTION_DAYS`
- **Description**: Maximum age (in days) for retained artifact revisions (used by retention janitor)
- **Type**: Integer
- **Default**: `30`
- **Getter**: `mcp_server.config.env_vars.get_artifact_retention_days()`
- **Example**:
  ```bash
  export MCP_ARTIFACT_RETENTION_DAYS=14  # Retain artifacts for 14 days
  ```

### `MCP_LOG_FORMAT`
- **Description**: Force JSON log formatter regardless of environment
- **Type**: String
- **Default**: Unset (format determined by `MCP_ENVIRONMENT`)
- **Valid Values**: `json`
- **Example**:
  ```bash
  export MCP_LOG_FORMAT=json  # Force JSON logs in all environments
  ```
- **Note**: When `MCP_ENVIRONMENT=production` or `MCP_LOG_FORMAT=json`, logs emit as JSON with fields: `timestamp`, `level`, `name`, `message`, and additional context fields.

## P17 enforced (Durability & Multi-Instance Safety)

### `MCP_ENVIRONMENT`
- **Description**: Deployment environment (gates validation fatals and JSON log default)
- **Type**: String
- **Default**: `test`
- **Valid Values**: `production`, `dev`, `test`
- **Example**:
  ```bash
  export MCP_ENVIRONMENT=production
  ```
- **Security**: In production, weak credentials (JWT, password, CORS wildcard, permissive rate-limit) are fatal and cause startup failure. In dev/test, the same errors log as WARN and startup continues.

### `JWT_SECRET_KEY`
- **Description**: Secret key for JWT token signing
- **Type**: String
- **Default**: None (required in production)
- **Example**:
  ```bash
  export JWT_SECRET_KEY=$(openssl rand -hex 32)
  ```
- **Security**: Generate with `openssl rand -hex 32`. Weak values (length < 32 hex chars / 16 bytes) are fatal in production, warn in dev.

### `DEFAULT_ADMIN_PASSWORD`
- **Description**: Default admin account password
- **Type**: String
- **Default**: None (required in production)
- **Security**: Weak values are fatal in production, warn in dev.

### `MCP_ATTESTATION_MODE`
- **Description**: Artifact attestation enforcement level
- **Type**: String
- **Default**: `enforce`
- **Valid Values**: `enforce`, `warn`, `off`
- **Example**:
  ```bash
  export MCP_ATTESTATION_MODE=warn  # Log warnings instead of failing
  ```

## P16 reserved (stub-only; not yet enforced)

The following environment variables are defined as part of the P16 Shared Vocabulary Preamble but are not yet wired to consumer logic beyond P17/P18. All values are accessed via lazy getters in `mcp_server.config.env_vars`.

### `MCP_ARTIFACT_RETENTION_COUNT`
- **Description**: Maximum number of artifact revisions to retain per artifact
- **Type**: Integer
- **Default**: `10`
- **Getter**: `mcp_server.config.env_vars.get_artifact_retention_count()`
- **Note**: Stub definition in P16; consumer wiring deferred to P17/P18

### `MCP_ARTIFACT_RETENTION_DAYS`
- **Description**: Maximum age (in days) for retained artifact revisions
- **Type**: Integer
- **Default**: `30`
- **Getter**: `mcp_server.config.env_vars.get_artifact_retention_days()`
- **Note**: Stub definition in P16; consumer wiring deferred to P17/P18

### `MCP_DISK_READONLY_THRESHOLD_MB`
- **Description**: Disk free space threshold (in MB) below which the system transitions to read-only mode
- **Type**: Integer
- **Default**: `100`
- **Getter**: `mcp_server.config.env_vars.get_disk_readonly_threshold_mb()`
- **Note**: Stub definition in P16; consumer wiring deferred to P17/P18

### `MCP_PUBLISH_ROLLBACK_ENABLED`
- **Description**: Enable automatic rollback on artifact publication failures
- **Type**: Boolean
- **Default**: `true` (enabled)
- **Getter**: `mcp_server.config.env_vars.get_publish_rollback_enabled()`
- **Truthy Values**: `1`, `true`, `yes`, `on` (case-insensitive); unset = `true`
- **Note**: Stub definition in P16; consumer wiring deferred to P17/P18
