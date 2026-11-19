# Contributing to Code-Index-MCP

Thank you for your interest in contributing to Code-Index-MCP! This document provides guidelines and instructions for contributing.

## Table of Contents
- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Process](#development-process)
- [Submitting Changes](#submitting-changes)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [Documentation](#documentation)

## Code of Conduct

By participating in this project, you agree to abide by our Code of Conduct:
- Be respectful and inclusive
- Welcome newcomers and help them get started
- Focus on constructive criticism
- Respect differing viewpoints and experiences

## Getting Started

### Prerequisites
- Python 3.14 or higher
- Git
- Basic understanding of language parsing and indexing

### Setting Up Development Environment

1. **Fork the repository**
   ```bash
   # Click "Fork" button on GitHub
   git clone https://github.com/YOUR_USERNAME/Code-Index-MCP.git
   cd Code-Index-MCP
   ```

2. **Create a virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/Mac
   # or
   venv\Scripts\activate  # Windows
   ```

3. **Install development dependencies**
   ```bash
   pip install -r requirements.txt
   pip install -r requirements-dev.txt  # Additional dev tools
   ```

4. **Set up pre-commit hooks**
   ```bash
   pre-commit install
   ```

5. **Run tests to verify setup**
   ```bash
   pytest
   ```

## Development Process

### 1. Find or Create an Issue
- Check existing issues for something you'd like to work on
- Create a new issue if needed, describing the problem or feature
- Comment on the issue to indicate you're working on it

### 2. Create a Feature Branch
```bash
# Update your fork
git checkout main
git pull upstream main

# Create a new branch
git checkout -b feature/your-feature-name
# or
git checkout -b fix/issue-number-description
```

### 3. Make Your Changes
- Write clean, readable code
- Follow the coding standards
- Add tests for new functionality
- Update documentation as needed

### 4. Test Your Changes
```bash
# Run all tests
pytest

# Run specific tests
pytest test_python_plugin.py

# Check code coverage
pytest --cov=mcp_server --cov-report=html

# Run linting
flake8 mcp_server/
black --check mcp_server/
mypy mcp_server/
```

### 5. Commit Your Changes
```bash
# Stage your changes
git add .

# Commit with a descriptive message
git commit -m "feat: add support for TypeScript parsing

- Implement TypeScript plugin using tree-sitter
- Add tests for TypeScript-specific features
- Update documentation

Fixes #123"
```

#### Commit Message Format
We follow [Conventional Commits](https://www.conventionalcommits.org/):
- `feat:` new feature
- `fix:` bug fix
- `docs:` documentation changes
- `style:` formatting changes (no code change)
- `refactor:` code restructuring
- `test:` adding tests
- `chore:` maintenance tasks

## Submitting Changes

### 1. Push to Your Fork
```bash
git push origin feature/your-feature-name
```

### 2. Create a Pull Request
- Go to your fork on GitHub
- Click "New Pull Request"
- Select your branch
- Fill out the PR template
- Link related issues

### 3. PR Review Process
- Maintainers will review your code
- Address any feedback
- Once approved, your PR will be merged

## Coding Standards

### Python Style Guide
- Follow [PEP 8](https://www.python.org/dev/peps/pep-0008/)
- Use type hints for all functions
- Maximum line length: 88 characters (Black default)

### Code Organization
```python
# Good example
from typing import Dict, List, Optional

class LanguagePlugin(PluginBase):
    """Plugin for analyzing a specific language.
    
    This plugin provides language-specific parsing and analysis
    capabilities for the MCP server.
    """
    
    def __init__(self, language: str) -> None:
        """Initialize the plugin.
        
        Args:
            language: The programming language this plugin handles
        """
        self.language = language
        self._parser = self._init_parser()
    
    def index(self, file_path: str) -> Dict[str, Any]:
        """Index a file and extract symbols.
        
        Args:
            file_path: Path to the file to index
            
        Returns:
            Dictionary containing indexed symbols and metadata
            
        Raises:
            FileNotFoundError: If file doesn't exist
            ParseError: If file cannot be parsed
        """
        # Implementation here
        pass
```

### Error Handling
```python
# Good error handling
try:
    result = parse_file(file_path)
except FileNotFoundError:
    logger.error(f"File not found: {file_path}")
    raise
except ParseError as e:
    logger.warning(f"Failed to parse {file_path}: {e}")
    return {"error": str(e), "partial": True}
```

## Testing

### Writing Tests
```python
# test_my_plugin.py
import pytest
from mcp_server.plugins.my_plugin import MyPlugin

class TestMyPlugin:
    @pytest.fixture
    def plugin(self):
        return MyPlugin()
    
    def test_index_simple_file(self, plugin, tmp_path):
        # Create test file
        test_file = tmp_path / "test.ext"
        test_file.write_text("test content")
        
        # Test indexing
        result = plugin.index(str(test_file))
        
        assert "symbols" in result
        assert result["language"] == "my_language"
    
    def test_handles_invalid_syntax(self, plugin):
        with pytest.raises(ParseError):
            plugin.parse("invalid syntax {{{")
```

### Test Coverage
- Aim for 90%+ test coverage
- Test edge cases and error conditions
- Include integration tests

## Documentation

### Code Documentation
- Add docstrings to all public functions and classes
- Use Google-style docstrings
- Include examples in docstrings

### Updating Documentation
When making changes:
1. Update relevant AGENTS.md files
2. Update CLAUDE.md if needed
3. Update API documentation
4. Add entries to CHANGELOG.md

### Architecture Changes
If your changes affect architecture:
1. Update C4 diagrams in `architecture/`
2. Update architecture documentation
3. Include diagrams in PR description

## Adding a New Language Plugin

### Step-by-Step Guide

1. **Create plugin directory**
   ```bash
   mkdir -p mcp_server/plugins/lang_plugin
   touch mcp_server/plugins/lang_plugin/__init__.py
   touch mcp_server/plugins/lang_plugin/plugin.py
   ```

2. **Implement plugin class**
   ```python
   from mcp_server.plugin_base import PluginBase
   
   class LangPlugin(PluginBase):
       # Implementation
   ```

3. **Add tests**
   ```bash
   touch test_lang_plugin.py
   ```

4. **Update documentation**
   - Add AGENTS.md and CLAUDE.md in plugin directory
   - Update main README.md

5. **Register plugin**
   - Add to dispatcher.py
   - Update plugin registry

## Questions?

- Check [existing issues](https://github.com/yourusername/Code-Index-MCP/issues)
- Join [discussions](https://github.com/yourusername/Code-Index-MCP/discussions)
- Read the [documentation](./docs/)

Thank you for contributing!