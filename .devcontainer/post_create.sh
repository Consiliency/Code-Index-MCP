#!/bin/bash
set -e

echo "🚀 Post-create: configuring environment"

# Install locked Python deps with uv (requirements.txt was retired)
uv sync --locked --extra dev
source .venv/bin/activate

# Prefetch tree-sitter grammars (language-pack 1.20 downloads on first use)
mcp-index grammars prefetch

# Confirm tools
echo "✅ Python: $(python --version)"
echo "✅ UV: $(uv --version)"
echo "✅ Claude CLI: $(command -v claude)"
echo "✅ Codex CLI: $(command -v codex)"

echo "✅ Post-create setup complete."
