#!/bin/bash
# Scaffolds the MCP server with Structurizr diagrams and FastAPI stubs

mkdir -p mcp_server/plugins/{python_plugin,dart_plugin,js_plugin,c_plugin,cpp_plugin,html_css_plugin} mcp_server/utils architecture .devcontainer

# .gitignore
cat > .gitignore <<EOG
__pycache__/
*.py[cod]
*.sqlite
*.db
.env
.vscode/
.idea/
*.DS_Store
architecture/out/
.devcontainer/.venv
EOG

# README.md
cat > README.md <<EOR
# MCP Server (Local-first Code Indexer)

Modular, extensible local-first indexer for enhancing Claude Code and other LLMs.

## Features
- Local-first code index (Tree-sitter, LSP, etc.)
- Plugin architecture (Python, Dart, JS, C/C++, HTML/CSS)
- Cloud sync and embeddings fallback (optional)
- Structurizr C4 diagrams

## Quickstart
\`\`\`bash
uvicorn mcp_server.gateway:app --reload
docker run --rm -p 8080:8080 \\
  -v "\$(pwd)/architecture":/usr/local/structurizr \\
  structurizr/lite
\`\`\`
EOR

# pyproject.toml
cat > pyproject.toml <<EOP
[project]
name = "mcp_server"
version = "0.0.0"
description = "Local-first MCP server"
requires-python = ">=3.14"
dependencies = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.29",
  "watchdog>=4.0",
]

[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"
EOP

# devcontainer
cat > .devcontainer/devcontainer.json <<EOD
{
  "name": "MCP Server Dev",
  "image": "mcr.microsoft.com/devcontainers/python:3-3.14-bookworm",
  "features": {
    "ghcr.io/devcontainers/features/docker-in-docker:2": {}
  },
  "postCreateCommand": "pip install -e ."
}
EOD

# Empty stubs
touch mcp_server/__init__.py mcp_server/gateway.py mcp_server/dispatcher.py mcp_server/plugin_base.py
touch mcp_server/sync.py mcp_server/watcher.py mcp_server/utils/treesitter_wrapper.py

for lang in python dart js c cpp html_css; do
cat > "mcp_server/plugins/${lang}_plugin/plugin.py" <<EOF2
from pathlib import Path
from ...plugin_base import IPlugin, IndexShard, SymbolDef, Reference, SearchResult, SearchOpts

class Plugin(IPlugin):
    lang = "$lang"
    def supports(self, p: str | Path) -> bool: ...
    def indexFile(self, p, content) -> IndexShard: ...
    def getDefinition(self, symbol) -> SymbolDef | None: ...
    def findReferences(self, symbol): ...
    def search(self, query, opts: SearchOpts | None = None): ...
EOF2
done

# DSL level 1
cat > architecture/level1_context.dsl <<'EOL1'
workspace "MCP Server – Level 1" {
  model {
    user = person "Developer"
    llm = softwareSystem "Claude"
    mcp = softwareSystem "MCP Server"
    repo = container "Local Code Repo"
    cloud = container "Cloud"

    user -> llm "asks help"
    llm -> mcp "tool.use()"
    mcp -> repo "indexes"
    mcp -> cloud "pulls shards"
  }
  views { systemContext ctx { include * ; autolayout lr } }
}
EOL1

# DSL level 2
cat > architecture/level2_containers.dsl <<'EOL2'
workspace "MCP Server – Level 2" {
  model {
    mcp = softwareSystem "MCP Server"
    api = container mcp "API Gateway" "FastAPI"
    dispatcher = container mcp "Dispatcher"
    indexer = container mcp "Indexer"
    store = container mcp "Local Index Store"
    sync = container mcp "Cloud Sync"
    embed = container mcp "Embedding"

    api -> dispatcher
    dispatcher -> store
    dispatcher -> sync
    dispatcher -> embed
    indexer -> store
    sync -> store
  }
  views { container containers { include * ; autolayout lr } }
}
EOL2

# DSL level 3
cat > architecture/level3_mcp_components.dsl <<'EOL3'
workspace "MCP Server – Level 3" {
  model {
    mcp = softwareSystem "MCP Server"
    api = container mcp "API Gateway"
    dispatcher = container mcp "Dispatcher"

    component gateway "Gateway Controller" "mcp_server.gateway"
    component core "Dispatcher Core" "mcp_server.dispatcher"
    component base "Plugin Base" "mcp_server.plugin_base"

    api -> gateway
    dispatcher -> core
    core -> base
  }
  views { component components { include * ; autolayout lr } }
}
EOL3

echo "✅ MCP scaffold complete. Run 'tree' or 'git init' to continue."
