#!/bin/bash
# MCP Index Kit - Universal Installer
# This script installs index management for any repository

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Default values
INSTALL_DIR=".mcp-index"
GITHUB_WORKFLOWS_DIR=".github/workflows"
CONFIG_FILE=".mcp-index.json"
IGNORE_FILE=".mcp-index-ignore"

# Print colored output
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if we're in a git repository
check_git_repo() {
    if ! git rev-parse --git-dir > /dev/null 2>&1; then
        print_error "Not in a git repository. Please run this from the root of your repository."
        exit 1
    fi
}

# Detect default branch
detect_default_branch() {
    git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo "main"
}

# Create installation directory
create_install_dir() {
    if [ ! -d "$INSTALL_DIR" ]; then
        mkdir -p "$INSTALL_DIR"
        print_info "Created $INSTALL_DIR directory"
    fi
}

# Create default configuration
create_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        cat > "$CONFIG_FILE" <<EOF
{
  "version": "1.0",
  "enabled": true,
  "auto_download": true,
  "auto_upload": true,
  "index_location": ".mcp-index/",
  "artifact_retention_days": 30,
  "ignore_file": ".mcp-index-ignore",
  "languages": "auto",
  "exclude_defaults": true,
  "custom_excludes": [],
  "github_artifacts": {
    "enabled": true,
    "compression": true,
    "max_size_mb": 100
  }
}
EOF
        print_success "Created $CONFIG_FILE"
    else
        print_warning "$CONFIG_FILE already exists, skipping"
    fi
}

# Create ignore file
create_ignore_file() {
    if [ ! -f "$IGNORE_FILE" ]; then
        cat > "$IGNORE_FILE" <<EOF
# MCP Index Ignore Patterns
# One pattern per line, supports gitignore syntax

# Version control
.git/
.svn/

# Dependencies
node_modules/
vendor/
venv/
.env/
__pycache__/
*.pyc

# Build outputs
dist/
build/
target/
*.min.js
*.min.css

# IDE files
.idea/
.vscode/
*.swp
*.swo

# OS files
.DS_Store
Thumbs.db

# Test files (optional - remove if you want to index tests)
test/
tests/
spec/
__tests__/

# Documentation (optional - remove if you want to index docs)
# docs/
# *.md

# Large files
*.zip
*.tar.gz
*.iso
*.exe
*.dll
*.so
*.dylib

# Temporary files
*.tmp
*.temp
*.log
EOF
        print_success "Created $IGNORE_FILE"
    else
        print_warning "$IGNORE_FILE already exists, skipping"
    fi
}

# Create GitHub workflow
create_github_workflow() {
    mkdir -p "$GITHUB_WORKFLOWS_DIR"
    
    local default_branch=$(detect_default_branch)
    local workflow_file="$GITHUB_WORKFLOWS_DIR/mcp-index.yml"
    
    if [ ! -f "$workflow_file" ]; then
        cat > "$workflow_file" <<EOF
name: MCP Index Management
on:
  push:
    branches: [$default_branch]
  pull_request:
    types: [opened, synchronize, reopened]
  schedule:
    - cron: '0 0 * * 0'  # Weekly rebuild
  workflow_dispatch:
    inputs:
      rebuild:
        description: 'Force rebuild index'
        type: boolean
        default: false

env:
  MCP_INDEX_ENABLED: \${{ vars.MCP_INDEX_ENABLED || 'true' }}
  # Optional: add VOYAGE_API_KEY to repository secrets to enable semantic (vector) search

jobs:
  check-enabled:
    runs-on: ubuntu-latest
    outputs:
      enabled: \${{ steps.check.outputs.enabled }}
    steps:
      - id: check
        run: |
          if [ "\${{ env.MCP_INDEX_ENABLED }}" = "false" ]; then
            echo "MCP indexing is disabled"
            echo "enabled=false" >> \$GITHUB_OUTPUT
          else
            echo "enabled=true" >> \$GITHUB_OUTPUT
          fi

  index-management:
    needs: check-enabled
    if: needs.check-enabled.outputs.enabled == 'true'
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v4
        with:
          fetch-depth: 0
      
      - name: Check for existing index
        id: check_index
        run: |
          if [ -f ".mcp-index/code_index.db" ]; then
            echo "found=true" >> \$GITHUB_OUTPUT
            echo "Index found in repository"
          else
            echo "found=false" >> \$GITHUB_OUTPUT
            echo "No index found, will build or download"
          fi
      
      - name: Try to download latest index
        if: steps.check_index.outputs.found == 'false'
        id: download
        run: |
          echo "Checking for available index artifacts..."
          # This would download from artifacts - simplified for now
          echo "downloaded=false" >> \$GITHUB_OUTPUT
      
      - name: Setup Python
        if: steps.check_index.outputs.found == 'false' && steps.download.outputs.downloaded == 'false'
        uses: actions/setup-python@v4
        with:
          python-version: '3.14'
      
      - name: Install MCP Indexer
        if: steps.check_index.outputs.found == 'false' && steps.download.outputs.downloaded == 'false'
        run: pip install index-it-mcp

      - name: Build Index
        if: steps.check_index.outputs.found == 'false' && steps.download.outputs.downloaded == 'false'
        run: |
          mkdir -p .mcp-index
          mcp-index index rebuild --sqlite-only
          mv code_index.db .mcp-index/code_index.db
      
      - name: Create index metadata
        if: steps.check_index.outputs.found == 'false' && steps.download.outputs.downloaded == 'false'
        run: |
          cat > .mcp-index/.index_metadata.json <<EOF
          {
            "version": "1.0",
            "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
            "commit": "\${{ github.sha }}",
            "branch": "\${{ github.ref_name }}",
            "repository": "\${{ github.repository }}"
          }
          EOF
      
      - name: Compress index for artifact
        run: |
          test -f .mcp-index/code_index.db || { echo "::error::Index build failed — no index file produced"; exit 1; }
          cd .mcp-index
          tar -czf ../mcp-index-archive.tar.gz .
          cd ..
      
      - name: Upload index artifact
        uses: actions/upload-artifact@v4
        with:
          name: mcp-index-\${{ github.sha }}
          path: mcp-index-archive.tar.gz
          retention-days: \${{ fromJSON(github.event_name == 'push' && '30' || '7') }}
      
      - name: Upload index for PRs
        if: github.event_name == 'pull_request'
        uses: actions/upload-artifact@v4
        with:
          name: mcp-index-pr-\${{ github.event.pull_request.number }}
          path: mcp-index-archive.tar.gz
          retention-days: 7

  cleanup-old-artifacts:
    runs-on: ubuntu-latest
    if: github.event_name == 'schedule'
    steps:
      - name: Cleanup old artifacts
        uses: actions/github-script@v6
        with:
          script: |
            const artifacts = await github.rest.actions.listArtifactsForRepo({
              owner: context.repo.owner,
              repo: context.repo.repo,
              per_page: 100
            });
            
            const cutoffDate = new Date();
            cutoffDate.setDate(cutoffDate.getDate() - 30);
            
            for (const artifact of artifacts.data.artifacts) {
              if (artifact.name.startsWith('mcp-index-') && 
                  new Date(artifact.created_at) < cutoffDate) {
                await github.rest.actions.deleteArtifact({
                  owner: context.repo.owner,
                  repo: context.repo.repo,
                  artifact_id: artifact.id
                });
                console.log(\`Deleted old artifact: \${artifact.name}\`);
              }
            }
EOF
        print_success "Created GitHub workflow: $workflow_file"
    else
        print_warning "$workflow_file already exists, skipping"
    fi
}

# Create README for .mcp-index directory
create_index_readme() {
    cat > "$INSTALL_DIR/README.md" <<EOF
# MCP Index Directory

This directory contains the code index for this repository, managed by the MCP Index Kit.

## Contents

- \`code_index.db\` - SQLite database containing code symbols and search index
- \`.index_metadata.json\` - Metadata about the index (version, creation time, etc.)

## Usage

The index is automatically managed by GitHub Actions. To use it locally:

1. Install the MCP extension for your IDE
2. The extension will automatically detect and use this index
3. Enjoy fast code navigation and search!

## Disabling

To disable MCP indexing:
1. Set \`MCP_INDEX_ENABLED=false\` in your repository settings
2. Or delete the \`.github/workflows/mcp-index.yml\` file
3. Or set \`"enabled": false\` in \`.mcp-index.json\`

## More Information

Visit https://github.com/yourusername/Code-Index-MCP for more details.
EOF
    print_info "Created index directory README"
}

# Main installation flow
main() {
    echo -e "${BLUE}MCP Index Kit Installer${NC}"
    echo "========================="
    echo
    
    # Check prerequisites
    check_git_repo
    
    print_info "Installing MCP Index Kit..."
    
    # Create necessary files and directories
    create_install_dir
    create_config
    create_ignore_file
    create_github_workflow
    create_index_readme
    
    # Create .gitignore entries
    if ! grep -q "^.mcp-index/code_index.db" .gitignore 2>/dev/null; then
        echo -e "\n# MCP Index (tracked for fast setup)" >> .gitignore
        echo ".mcp-index/code_index.db" >> .gitignore
        echo ".mcp-index/code_index.db-shm" >> .gitignore
        echo ".mcp-index/code_index.db-wal" >> .gitignore
        echo ".mcp-index/.index_metadata.json" >> .gitignore
        print_info "Updated .gitignore"
    fi
    
    # Offer to install git hooks
    echo
    read -p "Would you like to install git hooks for automatic index sync? [Y/n] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        # Find the hooks installer
        HOOKS_INSTALLER=""
        if [ -f "$(dirname "$0")/hooks/install-hooks.sh" ]; then
            HOOKS_INSTALLER="$(dirname "$0")/hooks/install-hooks.sh"
        elif [ -f "/usr/local/share/mcp-index-kit/hooks/install-hooks.sh" ]; then
            HOOKS_INSTALLER="/usr/local/share/mcp-index-kit/hooks/install-hooks.sh"
        fi
        
        if [ -n "$HOOKS_INSTALLER" ] && [ -f "$HOOKS_INSTALLER" ]; then
            bash "$HOOKS_INSTALLER"
        else
            print_warning "Git hooks installer not found. You can install them later with:"
            echo "  mcp-index hooks install"
        fi
    else
        print_info "Skipping git hooks installation"
        echo "You can install them later with: mcp-index hooks install"
    fi
    
    print_success "Installation complete!"
    echo
    echo "Next steps:"
    echo "1. Review and customize ${CONFIG_FILE} if needed"
    echo "2. Review and customize ${IGNORE_FILE} if needed"
    echo "3. Commit the changes: git add . && git commit -m 'Add MCP index management'"
    echo "4. Push to trigger index building: git push"
    echo
    echo "To disable indexing later, run: echo 'false' > .mcp-index-enabled"
    echo
    print_info "Happy coding! 🚀"
}

# Run main function
main "$@"