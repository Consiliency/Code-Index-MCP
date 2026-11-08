FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    curl \
    libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency metadata first for better caching
COPY pyproject.toml uv.lock README.md ./

# Install Python dependencies
RUN pip install --no-cache-dir uv && \
    uv sync --locked --no-install-project
ENV PATH="/app/.venv/bin:$PATH"

# Copy application code
COPY mcp_server/ ./mcp_server/
COPY tests/ ./tests/
COPY pytest.ini .
COPY .coveragerc .

# Install the package in development mode
RUN uv sync --locked

# Prefetch every registry grammar (tree-sitter-language-pack 1.20 downloads on
# first use) and export the parser libraries to a shared read-only directory, so
# containers never download at runtime and any user can load them
# (Consiliency/Code-Index-MCP#115).
RUN python -m mcp_server.cli.grammar_commands prefetch --cache-dir /tmp/ts-grammar-cache \
        --export-libs /opt/tree-sitter-language-pack/libs && \
    rm -rf /tmp/ts-grammar-cache
ENV TREE_SITTER_LANGUAGE_PACK_LIBS_DIR=/opt/tree-sitter-language-pack/libs

# Create volume for code indexing
VOLUME ["/code"]

# Expose API port
EXPOSE 8000

# Default command runs the server
CMD ["uvicorn", "mcp_server.gateway:app", "--host", "0.0.0.0", "--port", "8000"]
