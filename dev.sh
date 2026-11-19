#!/bin/bash
# Development environment launcher for Code-Index-MCP with Claude Code

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Starting Code-Index-MCP Development Environment${NC}"

# Check if ANTHROPIC_API_KEY is set
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo -e "${YELLOW}Warning: ANTHROPIC_API_KEY not set. Claude Code may have limited functionality.${NC}"
    echo "To set it: export ANTHROPIC_API_KEY='your-api-key'"
fi

# Build the development image
echo "Building development Docker image..."
docker-compose -f docker-compose.development.yml build dev

# Start the services
echo "Starting services..."
docker-compose -f docker-compose.development.yml up -d redis qdrant

# Wait for services to be healthy
echo "Waiting for services to be ready..."
sleep 5

# Run the development container
echo -e "${GREEN}Launching development container...${NC}"
echo "You'll have access to:"
echo "  - Python 3.14 with all project dependencies"
echo "  - Node.js and npm"
echo "  - Claude Code CLI"
echo "  - All testing and development tools"
echo ""
echo "Useful commands:"
echo "  claude-code         # Run Claude Code"
echo "  python mcp_server_cli.py  # Run the MCP server"
echo "  pytest              # Run tests"
echo "  uvicorn mcp_server.gateway:app --reload  # Run FastAPI with auto-reload"
echo ""

docker-compose -f docker-compose.development.yml run --rm dev

# Cleanup on exit
echo -e "${GREEN}Cleaning up...${NC}"
docker-compose -f docker-compose.development.yml down