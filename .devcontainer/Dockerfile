# bookworm (glibc 2.36): tree-sitter-language-pack 1.20 wheels need glibc >= 2.34
FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /workspace

# Install curl, git, node, build tools
RUN apt-get update && apt-get install -y \
    curl git build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

# Install Node.js (Claude & Codex CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y nodejs

# Install Claude & Codex CLI
RUN npm install -g @anthropic-ai/claude-code @openai/codex

# Install uv package manager (Rust-based)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && cp /root/.local/bin/uv /usr/local/bin/uv \
    && chmod +x /usr/local/bin/uv

# Default entrypoint
CMD [ "bash" ]
