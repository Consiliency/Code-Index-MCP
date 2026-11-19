#!/bin/bash

# MCP Dependencies Setup Script for Codex Development Container
# This script sets up MCP-specific dependencies needed for Codex IDE development
# Focuses only on MCP runtime dependencies, no IDE-specific configurations
# Optimized for development containers with UV package manager

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging function
log() {
    echo -e "${BLUE}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

# Check if running as root (for system package installation)
check_privileges() {
    if [[ $EUID -eq 0 ]]; then
        log "Running as root - can install system packages"
        SUDO=""
    else
        log "Running as regular user - will use sudo for system packages"
        SUDO="sudo"
    fi
}

# Check prerequisites
check_prerequisites() {
    log "Checking prerequisites..."
    
    # Check Python version
    if command -v python3 &> /dev/null; then
        PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
        log "Python version: $PYTHON_VERSION"
        
        # Check if version is 3.14 or higher (requires-python >=3.14)
        if python3 -c "import sys; exit(0 if sys.version_info >= (3, 14) else 1)"; then
            success "Python version is compatible"
        else
            error "Python 3.14+ required, found $PYTHON_VERSION"
            exit 1
        fi
    else
        error "Python3 not found"
        exit 1
    fi
    
    # Check Node.js version
    if command -v node &> /dev/null; then
        NODE_VERSION=$(node --version)
        log "Node.js version: $NODE_VERSION"
        
        # Extract major version (remove 'v' prefix)
        NODE_MAJOR=$(echo $NODE_VERSION | sed 's/v\([0-9]*\).*/\1/')
        if [[ $NODE_MAJOR -ge 16 ]]; then
            success "Node.js version is compatible"
        else
            warning "Node.js 16+ recommended, found $NODE_VERSION"
        fi
    else
        warning "Node.js not found - some features may not work"
    fi
    
    # Check pip
    if ! command -v pip3 &> /dev/null; then
        error "pip3 not found"
        exit 1
    fi
}

# Install system dependencies
install_system_deps() {
    log "Installing system dependencies..."
    
    # Detect package manager
    if command -v apt-get &> /dev/null; then
        PKG_MANAGER="apt-get"
        PKG_UPDATE="$SUDO apt-get update"
        PKG_INSTALL="$SUDO apt-get install -y"
    elif command -v yum &> /dev/null; then
        PKG_MANAGER="yum"
        PKG_UPDATE="$SUDO yum update -y"
        PKG_INSTALL="$SUDO yum install -y"
    elif command -v dnf &> /dev/null; then
        PKG_MANAGER="dnf"
        PKG_UPDATE="$SUDO dnf update -y"
        PKG_INSTALL="$SUDO dnf install -y"
    elif command -v apk &> /dev/null; then
        PKG_MANAGER="apk"
        PKG_UPDATE="$SUDO apk update"
        PKG_INSTALL="$SUDO apk add"
    else
        warning "No supported package manager found - skipping system dependencies"
        return
    fi
    
    log "Using package manager: $PKG_MANAGER"
    
    # Update package lists
    log "Updating package lists..."
    $PKG_UPDATE
    
    # Install system dependencies based on Dockerfile
    log "Installing build dependencies..."
    case $PKG_MANAGER in
        "apt-get")
            $PKG_INSTALL build-essential git curl libcurl4-openssl-dev \
                        libffi-dev libssl-dev python3-dev \
                        sqlite3 libsqlite3-dev \
                        redis-tools
            ;;
        "yum"|"dnf")
            $PKG_INSTALL gcc gcc-c++ make git curl libcurl-devel \
                        libffi-devel openssl-devel python3-devel \
                        sqlite sqlite-devel \
                        redis
            ;;
        "apk")
            $PKG_INSTALL build-base git curl curl-dev \
                        libffi-dev openssl-dev python3-dev \
                        sqlite sqlite-dev \
                        redis
            ;;
    esac
    
    success "System dependencies installed"
}

# Create directory structure
setup_directories() {
    log "Setting up directory structure..."
    
    # Create data directories
    mkdir -p data
    mkdir -p logs
    mkdir -p backup
    mkdir -p test_results
    mkdir -p htmlcov
    
    # Create configuration directories if they don't exist
    mkdir -p config
    
    success "Directory structure created"
}

# Install MCP dependencies using UV (faster) or pip fallback
install_mcp_deps() {
    log "Installing MCP dependencies..."
    
    # Check if UV is available for faster installation
    if command -v uv &> /dev/null; then
        log "Using UV package manager for faster installation..."
        INSTALLER="uv pip install"
    else
        log "UV not available, using pip..."
        INSTALLER="python3 -m pip install --no-cache-dir"
        # Upgrade pip first
        python3 -m pip install --upgrade pip setuptools wheel
    fi
    
    # Install MCP core dependencies
    log "Installing MCP core dependencies..."
    $INSTALLER \
        "mcp>=1.9.0" \
        "fastapi>=0.110" \
        "uvicorn[standard]>=0.29"
    
    # Install search and AI dependencies
    log "Installing semantic search dependencies..."
    $INSTALLER \
        "voyageai>=0.2.0" \
        "qdrant-client>=1.7.0"
    
    # Install code processing dependencies
    log "Installing code processing dependencies..."
    $INSTALLER \
        "tree-sitter>=0.20.0" \
        "tree-sitter-languages>=1.8.0" \
        "jedi>=0.19.0"
    
    # Install database and caching
    log "Installing database and caching dependencies..."
    $INSTALLER \
        "redis>=4.5.0" \
        "aiofiles>=23.2.1"
    
    # Install security dependencies
    log "Installing security dependencies..."
    $INSTALLER \
        "pyjwt>=2.8.0" \
        "passlib[bcrypt]>=1.7.4" \
        "cryptography>=41.0.4"
    
    # Install testing framework for MCP
    log "Installing MCP testing dependencies..."
    $INSTALLER \
        "pytest>=7.4.0" \
        "pytest-asyncio>=0.21.0" \
        "httpx>=0.24.1"
    
    # Install requirements.txt if available
    if [[ -f "requirements.txt" ]]; then
        log "Installing additional requirements from requirements.txt..."
        $INSTALLER -r requirements.txt
    fi
    
    # Install the project in development mode
    if [[ -f "pyproject.toml" ]]; then
        log "Installing project in development mode..."
        $INSTALLER -e .
    fi
    
    success "MCP dependencies installed"
}

# Setup Tree-sitter grammars
setup_tree_sitter() {
    log "Setting up Tree-sitter grammars..."
    
    # Create a simple Python script to initialize tree-sitter
    cat > init_tree_sitter.py << 'EOF'
#!/usr/bin/env python3
"""Initialize Tree-sitter grammars for the project."""

try:
    import tree_sitter_languages
    from tree_sitter import Language
    
    # Test loading common languages
    languages = ['python', 'javascript', 'c', 'cpp', 'html', 'css', 'dart']
    
    for lang in languages:
        try:
            language = tree_sitter_languages.get_language(lang)
            print(f"✅ {lang} grammar loaded successfully")
        except Exception as e:
            print(f"⚠️  Failed to load {lang} grammar: {e}")
    
    print("Tree-sitter setup completed")
    
except ImportError as e:
    print(f"❌ Tree-sitter not available: {e}")
    exit(1)
EOF
    
    python3 init_tree_sitter.py
    rm init_tree_sitter.py
    
    success "Tree-sitter grammars initialized"
}

# Create MCP configuration files
create_mcp_config() {
    log "Creating MCP configuration files..."
    
    # Create .env file if it doesn't exist
    if [[ ! -f ".env" ]]; then
        log "Creating MCP .env configuration..."
        cat > .env << 'EOF'
# MCP Server Configuration for Codex Development

# Server Configuration
HOST=0.0.0.0
PORT=8000
DEBUG=true
LOG_LEVEL=INFO

# MCP-Specific Settings
MCP_SERVER_NAME=codex-mcp-server
MCP_VERSION=1.0.0

# Database
DATABASE_URL=sqlite:///./data/code_index.db

# Semantic Search (Voyage AI)
VOYAGE_API_KEY=
SEMANTIC_SEARCH_ENABLED=true

# Vector Database (Qdrant)
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION_NAME=codebase

# Cache Configuration
REDIS_URL=redis://localhost:6379
CACHE_TTL=3600

# Security
JWT_SECRET_KEY=codex-dev-secret-change-in-production
JWT_ALGORITHM=HS256

# CORS for development
CORS_ORIGINS=["http://localhost:3000", "http://localhost:8080", "http://localhost:8000"]
EOF
        success "Created MCP .env file"
    else
        log ".env file already exists"
    fi
    
    # Create MCP-specific pytest configuration
    if [[ ! -f "pytest.ini" ]]; then
        log "Creating MCP pytest.ini..."
        cat > pytest.ini << 'EOF'
[tool:pytest]
testpaths = tests
python_files = test_*.py *_test.py
python_classes = Test*
python_functions = test_*
addopts = 
    -v
    --tb=short
    --strict-markers
    --disable-warnings
    --color=yes
    --durations=10
markers =
    unit: Unit tests
    integration: Integration tests
    mcp: MCP-specific tests
    semantic: Semantic search tests
    vector: Vector database tests
    slow: Slow tests
filterwarnings =
    ignore::DeprecationWarning
    ignore::PendingDeprecationWarning
EOF
        success "Created MCP pytest.ini"
    else
        log "pytest.ini already exists"
    fi
}

# Setup permissions and ownership
setup_permissions() {
    log "Setting up permissions..."
    
    # Make directories writable
    chmod -R 755 data logs backup test_results htmlcov 2>/dev/null || true
    
    # Make any shell scripts executable
    find . -name "*.sh" -type f -exec chmod +x {} \; 2>/dev/null || true
    
    success "Permissions set"
}

# Validate MCP installation
validate_mcp_installation() {
    log "Validating MCP installation..."
    
    # Test MCP imports
    log "Testing MCP dependencies..."
    python3 -c "
import sys
print(f'Python version: {sys.version}')

# Test MCP core imports
try:
    import mcp
    import fastapi
    import uvicorn
    print('✅ MCP core dependencies imported successfully')
except ImportError as e:
    print(f'❌ MCP core import failed: {e}')
    sys.exit(1)

# Test semantic search imports
try:
    import voyageai
    import qdrant_client
    print('✅ Semantic search dependencies available')
except ImportError as e:
    print(f'⚠️  Semantic search import issue: {e}')

# Test code processing imports
try:
    import tree_sitter
    import tree_sitter_languages
    import jedi
    print('✅ Code processing dependencies available')
except ImportError as e:
    print(f'⚠️  Code processing import issue: {e}')

# Test other MCP dependencies
mcp_deps = {
    'redis': 'Redis cache support',
    'pytest': 'MCP testing framework',
    'jwt': 'Security (PyJWT)',
    'passlib': 'Password security',
}

for module, description in mcp_deps.items():
    try:
        __import__(module)
        print(f'✅ {description} available')
    except ImportError:
        print(f'⚠️  {description} not available')
"
    
    # Test project structure
    log "Checking MCP project structure..."
    required_dirs=("mcp_server" "tests")
    for dir in "${required_dirs[@]}"; do
        if [[ -d "$dir" ]]; then
            success "$dir directory found"
        else
            error "$dir directory missing"
        fi
    done
    
    # Test if MCP server can be imported
    log "Testing MCP server import..."
    python3 -c "
try:
    from mcp_server import gateway
    print('✅ MCP server can be imported')
except ImportError as e:
    print(f'⚠️  MCP server import issue: {e}')
"
    
    success "MCP installation validation completed"
}

# Create MCP startup script
create_mcp_startup_script() {
    log "Creating MCP startup script..."
    
    cat > start-mcp-server.sh << 'EOF'
#!/bin/bash
# Start the MCP server for Codex development

set -euo pipefail

# Check if UV virtual environment should be activated
if [[ -d ".venv" ]]; then
    echo "Activating UV virtual environment..."
    source .venv/bin/activate
elif [[ -d "venv" ]]; then
    echo "Activating virtual environment..."
    source venv/bin/activate
fi

# Load environment variables
if [[ -f ".env" ]]; then
    echo "Loading MCP environment variables..."
    export $(grep -v '^#' .env | xargs)
fi

# Create data directory if it doesn't exist
mkdir -p data

# Start the MCP server
echo "Starting MCP server for Codex..."
exec python3 -m uvicorn mcp_server.gateway:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-8000}" \
    --reload \
    --log-level "${LOG_LEVEL:-info}"
EOF
    
    chmod +x start-mcp-server.sh
    success "Created start-mcp-server.sh"
}

# Create MCP test runner script
create_mcp_test_script() {
    log "Creating MCP test runner script..."
    
    cat > run-mcp-tests.sh << 'EOF'
#!/bin/bash
# Run MCP tests for Codex development

set -euo pipefail

# Activate virtual environment if it exists
if [[ -d ".venv" ]]; then
    source .venv/bin/activate
elif [[ -d "venv" ]]; then
    source venv/bin/activate
fi

# Default to MCP unit tests if no argument provided
TEST_TYPE=${1:-mcp}

case $TEST_TYPE in
    "mcp")
        echo "Running MCP-specific tests..."
        python3 -m pytest tests -v -m "mcp and not slow"
        ;;
    "semantic")
        echo "Running semantic search tests..."
        python3 -m pytest tests -v -m "semantic"
        ;;
    "vector")
        echo "Running vector database tests..."
        python3 -m pytest tests -v -m "vector"
        ;;
    "integration")
        echo "Running MCP integration tests..."
        python3 -m pytest tests -v -m "integration and mcp"
        ;;
    "all")
        echo "Running all MCP tests with coverage..."
        python3 -m pytest tests -v --cov=mcp_server --cov-report=term-missing --cov-report=html
        ;;
    "quick")
        echo "Running quick MCP smoke tests..."
        python3 -m pytest tests -v -m "mcp" --maxfail=3 -x
        ;;
    *)
        echo "Usage: $0 [mcp|semantic|vector|integration|all|quick]"
        exit 1
        ;;
esac
EOF
    
    chmod +x run-mcp-tests.sh
    success "Created run-mcp-tests.sh"
}

# Create maintenance script
create_maintenance_script() {
    log "Creating maintenance script..."
    
    cat > maintenance.sh << 'EOF'
#!/bin/bash
# Maintenance script for Code-Index-MCP

set -euo pipefail

# Function to clean up
cleanup() {
    echo "Cleaning up temporary files..."
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name "*.pyc" -delete 2>/dev/null || true
    find . -type f -name "*.pyo" -delete 2>/dev/null || true
    find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
    echo "✅ Cleanup completed"
}

# Function to check health
health_check() {
    echo "Performing health check..."
    
    # Check if server is running
    if pgrep -f "uvicorn.*mcp_server" > /dev/null; then
        echo "✅ Server is running"
    else
        echo "⚠️  Server is not running"
    fi
    
    # Check disk space
    echo "Disk usage:"
    df -h . | tail -1
    
    # Check memory usage
    echo "Memory usage:"
    free -h 2>/dev/null || vm_stat 2>/dev/null || echo "Memory info not available"
}

# Function to backup data
backup() {
    echo "Creating backup..."
    BACKUP_DIR="backup/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$BACKUP_DIR"
    
    # Backup database
    if [[ -f "data/code_index.db" ]]; then
        cp "data/code_index.db" "$BACKUP_DIR/"
        echo "✅ Database backed up"
    fi
    
    # Backup logs
    if [[ -d "logs" ]]; then
        cp -r logs "$BACKUP_DIR/"
        echo "✅ Logs backed up"
    fi
    
    echo "✅ Backup completed in $BACKUP_DIR"
}

# Main menu
case "${1:-help}" in
    "cleanup")
        cleanup
        ;;
    "health")
        health_check
        ;;
    "backup")
        backup
        ;;
    "help"|*)
        echo "Usage: $0 [cleanup|health|backup]"
        echo "  cleanup - Remove temporary files"
        echo "  health  - Check system health"
        echo "  backup  - Backup data and logs"
        ;;
esac
EOF
    
    chmod +x maintenance.sh
    success "Created maintenance.sh"
}

# Main execution for MCP setup
main() {
    log "Starting MCP dependencies setup for Codex development..."
    
    check_privileges
    check_prerequisites
    install_system_deps
    setup_directories
    install_mcp_deps
    setup_tree_sitter
    create_mcp_config
    setup_permissions
    create_mcp_startup_script
    create_mcp_test_script
    create_maintenance_script
    validate_mcp_installation
    
    success "MCP setup completed successfully!"
    
    log "Next steps for Codex development:"
    echo "  1. Review and update .env file with your MCP configuration"
    echo "  2. Add your Voyage AI API key to .env for semantic search"
    echo "  3. Start the MCP server: ./start-mcp-server.sh"
    echo "  4. Run MCP tests: ./run-mcp-tests.sh"
    echo "  5. Check health: ./maintenance.sh health"
    echo ""
    log "Available MCP scripts:"
    echo "  - start-mcp-server.sh : Start the MCP server for Codex"
    echo "  - run-mcp-tests.sh    : Run MCP test suite"
    echo "  - maintenance.sh      : System maintenance"
    echo ""
    log "MCP development workflow:"
    echo "  - ./run-mcp-tests.sh mcp       : Test MCP functionality"
    echo "  - ./run-mcp-tests.sh semantic  : Test semantic search"
    echo "  - ./run-mcp-tests.sh vector    : Test vector database"
    echo "  - ./run-mcp-tests.sh all       : Full test suite with coverage"
}

# Execute main function
main "$@"