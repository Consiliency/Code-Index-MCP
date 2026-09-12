#!/bin/bash
# MCP Index Docker Installation Script
# Detects OS, installs Docker if needed, and sets up MCP Index

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
MCP_VERSION="${MCP_VERSION:-v1.4.1}"
MCP_VARIANT="${MCP_VARIANT:-v1.4.1}"
DOCKER_REGISTRY="${DOCKER_REGISTRY:-ghcr.io}"
MCP_IMAGE="${DOCKER_REGISTRY}/consiliency/code-index-mcp"

# Functions
print_banner() {
    echo -e "${BLUE}"
    echo "╔═══════════════════════════════════════════╗"
    echo "║        MCP Index Docker Installer         ║"
    echo "║   Fast, local-first code indexing tool    ║"
    echo "╚═══════════════════════════════════════════╝"
    echo -e "${NC}"
}

print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

detect_os() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if [ -f /etc/os-release ]; then
            . /etc/os-release
            OS=$ID
            VER=$VERSION_ID
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        OS="macos"
        VER=$(sw_vers -productVersion)
    elif [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "cygwin" ]]; then
        OS="windows"
        VER="unknown"
    else
        OS="unknown"
        VER="unknown"
    fi
    
    print_info "Detected OS: $OS $VER"
}

check_docker() {
    if command -v docker &> /dev/null; then
        DOCKER_VERSION=$(docker --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
        print_info "Docker is installed (version $DOCKER_VERSION)"
        return 0
    else
        return 1
    fi
}

install_docker() {
    print_warn "Docker is not installed"
    
    read -p "Would you like to install Docker? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_error "Docker is required for MCP Index. Please install Docker manually."
        exit 1
    fi
    
    case $OS in
        ubuntu|debian)
            print_info "Installing Docker on $OS..."
            curl -fsSL https://get.docker.com | sudo sh
            sudo usermod -aG docker $USER
            print_warn "You need to log out and back in for Docker permissions to take effect"
            ;;
        fedora|centos|rhel)
            print_info "Installing Docker on $OS..."
            sudo dnf install -y docker
            sudo systemctl start docker
            sudo systemctl enable docker
            sudo usermod -aG docker $USER
            ;;
        macos)
            print_info "Please install Docker Desktop from:"
            echo "https://www.docker.com/products/docker-desktop/"
            exit 1
            ;;
        windows)
            print_info "Please install Docker Desktop from:"
            echo "https://www.docker.com/products/docker-desktop/"
            exit 1
            ;;
        *)
            print_error "Unsupported OS for automatic Docker installation"
            print_info "Please install Docker manually from: https://docs.docker.com/get-docker/"
            exit 1
            ;;
    esac
}

choose_variant() {
    echo
    echo "Choose MCP Index variant:"
    echo "1) v1.4.1      - Versioned release image (requires publication)"
    echo "2) local-smoke - Locally built via make release-smoke-container"
    echo "3) latest      - Stable channel (published)"
    echo

    read -p "Select variant [1-3] (default: 1): " -n 1 -r
    echo

    case $REPLY in
        2)
            MCP_VARIANT="local-smoke"
            print_info "Selected: local-smoke"
            ;;
        3)
            MCP_VARIANT="latest"
            print_info "Selected: latest"
            ;;
        *)
            MCP_VARIANT="v1.4.1"
            print_info "Selected: v1.4.1"
            ;;
    esac
}

pull_image() {
    if [ "$MCP_VARIANT" = "local-smoke" ]; then
        print_info "Using the locally built smoke image (dev option)."
        if ! docker image inspect "${MCP_IMAGE}:local-smoke" >/dev/null 2>&1; then
            print_error "Local smoke image not found. Run 'make release-smoke-container' first."
            exit 1
        fi
        return
    fi
    print_info "Pulling MCP Index image: ${MCP_IMAGE}:${MCP_VARIANT}"
    docker pull "${MCP_IMAGE}:${MCP_VARIANT}"
}

create_launcher() {
    LAUNCHER_PATH="/usr/local/bin/mcp-index"
    
    print_info "Creating launcher script..."
    
    # Create the launcher script
    cat > /tmp/mcp-index << 'EOF'
#!/bin/bash
# MCP Index Docker Launcher

# Default settings
MCP_VARIANT="${MCP_VARIANT:-v1.4.1}"
MCP_IMAGE="${MCP_IMAGE:-ghcr.io/consiliency/code-index-mcp}"
WORKSPACE="${WORKSPACE:-$(pwd)}"

# Handle commands
case "$1" in
    setup)
        echo "Running MCP Index setup wizard..."
        docker run -it --rm -v "$WORKSPACE:/workspace" "${MCP_IMAGE}:${MCP_VARIANT}" --setup
        ;;
    upgrade)
        echo "Upgrading MCP Index..."
        docker pull "${MCP_IMAGE}:${MCP_VARIANT}"
        ;;
    *)
        # Run MCP server with all arguments passed through
        docker run -i --rm \
            -v "$WORKSPACE:/workspace" \
            -v "$HOME/.mcp-index:/app/.mcp-index" \
            -e VOYAGE_API_KEY="${VOYAGE_API_KEY:-}" \
            -e MCP_ARTIFACT_SYNC="${MCP_ARTIFACT_SYNC:-true}" \
            "${MCP_IMAGE}:${MCP_VARIANT}" "$@"
        ;;
esac
EOF
    
    # Install the launcher
    if [ -w /usr/local/bin ]; then
        mv /tmp/mcp-index /usr/local/bin/mcp-index
        chmod +x /usr/local/bin/mcp-index
    else
        sudo mv /tmp/mcp-index /usr/local/bin/mcp-index
        sudo chmod +x /usr/local/bin/mcp-index
    fi
    
    print_info "Launcher installed at: /usr/local/bin/mcp-index"
}

setup_mcp_json() {
    print_info "Creating .mcp.json template..."
    
    cat > .mcp.json << EOF
{
  "mcpServers": {
    "code-index": {
      "command": "docker",
      "args": [
        "run", 
        "-i", 
        "--rm",
        "-v", "\${workspace}:/workspace",
        "-v", "\${HOME}/.mcp-index:/app/.mcp-index",
        "-e", "VOYAGE_API_KEY=\${VOYAGE_API_KEY:-}",
        "-e", "MCP_ARTIFACT_SYNC=\${MCP_ARTIFACT_SYNC:-true}",
        "${MCP_IMAGE}:${MCP_VARIANT}"
      ]
    }
  }
}
EOF
    
    print_info "Created .mcp.json for Claude Code integration"
}

print_next_steps() {
    echo
    echo -e "${GREEN}✅ MCP Index Docker installation complete!${NC}"
    echo
    echo "Next steps:"
    echo "1. Test the installation:"
    echo "   mcp-index --version"
    echo
    echo "2. Index your current directory:"
    echo "   mcp-index"
    echo
    
    echo "3. Optional: configure semantic search:"
    echo "   export VOYAGE_API_KEY='your-key-here'"
    echo "   Get your key at: https://www.voyageai.com/"
    echo

    echo "4. For Claude Code integration:"
    echo "   - Copy .mcp.json to your project root"
    echo "   - Claude will automatically use the Docker version"
    echo
    echo "Documentation: https://github.com/Code-Index-MCP/docs/DOCKER_GUIDE.md"
}

# Main installation flow
main() {
    print_banner
    detect_os
    
    # Check if Docker is installed
    if ! check_docker; then
        install_docker
    fi
    
    # Choose variant
    choose_variant
    
    # Pull the image
    pull_image
    
    # Create launcher script
    create_launcher
    
    # Create .mcp.json template
    setup_mcp_json

    # Activate committed git hooks
    if [ -d ".git" ] && [ -d "mcp-index-kit/hooks" ]; then
        git config core.hooksPath mcp-index-kit/hooks
        print_info "Git hooks activated (core.hooksPath = mcp-index-kit/hooks)"
    fi

    # Print next steps
    print_next_steps
}

# Run main function
main "$@"
