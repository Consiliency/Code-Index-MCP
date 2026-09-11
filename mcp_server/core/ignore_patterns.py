"""
Utility module for handling ignore patterns from .gitignore and .mcp-index-ignore files.
"""

import logging
from pathlib import Path
from typing import Callable, List, Optional

from pathspec import GitIgnoreSpec

logger = logging.getLogger(__name__)

# Union of _EXCLUDED_DIR_PARTS from watcher.py and _INDEX_EXCLUDED_DIRS from dispatcher_enhanced.py.
EXCLUDED_DIR_PARTS: frozenset[str] = frozenset(
    {
        # From watcher.py _EXCLUDED_DIR_PARTS
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mcp-index",
        ".indexes",
        ".tox",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "target",
        ".gradle",
        ".idea",
        ".vscode",
        "coverage",
        "htmlcov",
        # From dispatcher_enhanced.py _INDEX_EXCLUDED_DIRS (additions)
        ".hg",
        ".svn",
        "qdrant_storage",
        "vector_index.qdrant",
        "code_index_mcp.egg-info",
    }
)


def build_walker_filter(root: Path) -> Callable[[Path], bool]:
    """Apply repository-relative exclusions before entering or parsing a path."""
    ignore_mgr = IgnorePatternManager(root)

    def filter_fn(path: Path) -> bool:
        try:
            relative = path.absolute().relative_to(ignore_mgr.root_path)
        except ValueError:
            return True
        if any(part in EXCLUDED_DIR_PARTS for part in relative.parts):
            return True
        return ignore_mgr.should_ignore(path)

    return filter_fn


class IgnorePatternManager:
    """Repository-scoped Git-wildmatch rules, including nested ignore files."""

    def __init__(self, root_path: Optional[Path] = None):
        self.root_path = (root_path or Path.cwd()).absolute()
        self._load_patterns()

    @staticmethod
    def _read_patterns(path: Path) -> List[str]:
        if path.is_symlink():
            raise ValueError("Ignore policy cannot be a symbolic link")
        try:
            return [
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")
            ]
        except FileNotFoundError:
            return []

    def _load_patterns(self):
        self._gitignore_patterns = self._read_patterns(self.root_path / ".gitignore")
        self._mcp_ignore_patterns = self._load_mcp_ignore_patterns()
        self._patterns = self._gitignore_patterns + self._mcp_ignore_patterns
        self._git_specs = {self.root_path: GitIgnoreSpec.from_lines(self._gitignore_patterns)}
        self._mcp_spec = GitIgnoreSpec.from_lines(self._mcp_ignore_patterns)

    def _load_mcp_ignore_patterns(self) -> List[str]:
        """Load patterns from .mcp-index-ignore file."""
        patterns = []
        ignore_path = self.root_path / ".mcp-index-ignore"

        # Default patterns if file doesn't exist
        default_patterns = [
            # Security
            "*.env",
            ".env*",
            "*.key",
            "*.pem",
            "*.p12",
            "*secret*",
            "*password*",
            "*.credentials",
            "config/secrets/*",
            ".aws/*",
            ".ssh/*",
            # Build / cache
            "*.pyc",
            "__pycache__/*",
            "node_modules/*",
            ".git/*",
            "*.log",
            "*.tmp",
            "*.temp",
            "*.cache",
            # External fixture repos
            "test_workspace/",
            "test_repos/",
            "testdata/",
            "vendor/",
            "third_party/",
            # Generated code
            "baml_client/",
            "*_pb2.py",
            "*_pb2_grpc.py",
        ]

        if ignore_path.exists() or ignore_path.is_symlink():
            return self._read_patterns(ignore_path)
        return default_patterns

    def should_ignore(self, file_path: Path) -> bool:
        """Reject outside/symlink paths and honor scoped rules before traversal."""
        path = file_path if file_path.is_absolute() else self.root_path / file_path
        try:
            relative = path.absolute().relative_to(self.root_path)
        except ValueError:
            return True
        if ".." in relative.parts:
            return True
        parent = self.root_path
        scopes = []
        for index, component in enumerate(relative.parts):
            if parent not in self._git_specs:
                self._git_specs[parent] = GitIgnoreSpec.from_lines(
                    self._read_patterns(parent / ".gitignore")
                )
            scopes.append((parent, self._git_specs[parent]))
            candidate = parent / component
            if candidate.is_symlink():
                return True
            is_directory = index < len(relative.parts) - 1 or candidate.is_dir()
            ignored = False
            for scope, spec in scopes:
                name = candidate.relative_to(scope).as_posix() + ("/" if is_directory else "")
                match = spec.check_file(name).include
                if match is not None:
                    ignored = match
            name = candidate.relative_to(self.root_path).as_posix() + ("/" if is_directory else "")
            match = self._mcp_spec.check_file(name).include
            if match is not None:
                ignored = match
            if ignored:
                return True
            parent = candidate
        return False

    def get_patterns(self) -> List[str]:
        """Get all loaded patterns."""
        return self._patterns.copy()

    def get_gitignore_patterns(self) -> List[str]:
        """Get patterns from .gitignore."""
        return self._gitignore_patterns.copy()

    def get_mcp_ignore_patterns(self) -> List[str]:
        """Get patterns from .mcp-index-ignore."""
        return self._mcp_ignore_patterns.copy()

    def reload(self):
        """Reload patterns from files."""
        self._load_patterns()


# Singleton instance for easy access
_ignore_manager: Optional[IgnorePatternManager] = None


def get_ignore_manager(root_path: Path = None) -> IgnorePatternManager:
    """
    Get or create the ignore pattern manager.

    Args:
        root_path: Root directory for this policy instance.

    Returns:
        IgnorePatternManager instance
    """
    global _ignore_manager
    root = (root_path or Path.cwd()).absolute()
    if _ignore_manager is None or _ignore_manager.root_path != root:
        _ignore_manager = IgnorePatternManager(root)
    return _ignore_manager


def should_ignore_file(file_path: Path, root_path: Path = None) -> bool:
    """
    Convenience function to check if a file should be ignored.

    Args:
        file_path: Path to check
        root_path: Root directory for ignore files (optional)

    Returns:
        True if file should be ignored
    """
    manager = get_ignore_manager(root_path)
    return manager.should_ignore(file_path)
