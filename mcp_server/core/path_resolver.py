"""
Path resolver for managing relative paths and content hashing.

This module provides centralized path management to ensure all paths are stored
as relative to the repository root, enabling true index portability.
"""

import hashlib
import logging
import os
from pathlib import Path
from typing import Optional, Tuple, Union

logger = logging.getLogger(__name__)


class PathResolver:
    """Handles path normalization and resolution for portable indexes."""

    def __init__(
        self,
        repository_root: Optional[Path] = None,
        index_storage_path: Optional[Path] = None,
        storage_strategy: str = "centralized",
        source_root: Optional[Path] = None,
    ):
        """
        Initialize the path resolver.

        Args:
            repository_root: Root directory of the repository. If None, will auto-detect.
            index_storage_path: Path for centralized index storage
            storage_strategy: Storage strategy - "centralized", "portable", or "inline"
        """
        self.repository_root = repository_root or self._detect_repository_root()
        self.source_root = source_root
        self.index_storage_path = index_storage_path or self._get_default_index_path()
        self.storage_strategy = storage_strategy
        logger.info(f"PathResolver initialized with root: {self.repository_root}")

    def normalize_path(self, absolute_path: Union[str, Path]) -> str:
        """
        Convert absolute path to relative path from repository root.

        Args:
            absolute_path: Absolute file path

        Returns:
            Relative path as string

        Raises:
            ValueError: If path is outside repository
        """
        path = Path(absolute_path).resolve()
        if self.source_root is not None and path.is_relative_to(self.source_root):
            return path.relative_to(self.source_root).as_posix()
        try:
            relative = path.relative_to(self.repository_root)
            return str(relative).replace("\\", "/")  # Normalize to forward slashes
        except ValueError:
            raise ValueError(f"Path {path} is outside repository root {self.repository_root}")

    def resolve_path(self, relative_path: str) -> Path:
        """
        Convert relative path to absolute path.

        Args:
            relative_path: Relative path from repository root

        Returns:
            Absolute Path object
        """
        # Handle both forward and backward slashes
        relative_path = relative_path.replace("\\", "/")
        return (self.repository_root / relative_path).resolve()

    def compute_content_hash(self, file_path: Union[str, Path]) -> str:
        """
        Compute SHA-256 hash of file content.

        Args:
            file_path: Path to file

        Returns:
            Hex string of content hash
        """
        sha256_hash = hashlib.sha256()
        path = Path(file_path)

        try:
            with open(path, "rb") as f:
                # Read in chunks to handle large files
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            return sha256_hash.hexdigest()
        except Exception as e:
            logger.error(f"Failed to compute hash for {path}: {e}")
            raise

    def compute_file_hash(self, file_path: Union[str, Path]) -> str:
        """
        Compute hash including file metadata (size, mtime).

        Args:
            file_path: Path to file

        Returns:
            Hex string of file hash
        """
        path = Path(file_path)
        stat = path.stat()

        # Include size and mtime in hash for quick change detection
        metadata = f"{stat.st_size}:{stat.st_mtime_ns}".encode()

        sha256_hash = hashlib.sha256()
        sha256_hash.update(metadata)

        # For small files, include content
        if stat.st_size < 1024 * 1024:  # 1MB
            try:
                with open(path, "rb") as f:
                    sha256_hash.update(f.read())
            except Exception:
                pass  # Ignore read errors for metadata hash

        return sha256_hash.hexdigest()

    def is_within_repository(self, path: Union[str, Path]) -> bool:
        """
        Check if a path is within the repository.

        Args:
            path: Path to check

        Returns:
            True if path is within repository
        """
        try:
            path = Path(path).resolve()
            path.relative_to(self.repository_root)
            return True
        except ValueError:
            return False

    def get_relative_to(self, path: Union[str, Path], base: Union[str, Path]) -> str:
        """
        Get relative path from a base directory.

        Args:
            path: Target path
            base: Base directory

        Returns:
            Relative path string
        """
        path = Path(path).resolve()
        base = Path(base).resolve()
        try:
            return str(path.relative_to(base)).replace("\\", "/")
        except ValueError:
            # Paths don't share a common base
            return str(path)

    def _detect_repository_root(self) -> Path:
        """
        Auto-detect repository root by finding .git directory.

        Returns:
            Path to repository root
        """
        current = Path.cwd()

        # Look for .git directory
        while current != current.parent:
            if (current / ".git").exists():
                logger.info(f"Detected repository root from .git: {current}")
                return current
            current = current.parent

        # Look for .mcp-index directory as fallback
        current = Path.cwd()
        while current != current.parent:
            if (current / ".mcp-index").exists():
                logger.info(f"Detected repository root from .mcp-index: {current}")
                return current
            current = current.parent

        # Default to current directory
        logger.warning("Could not detect repository root, using current directory")
        return Path.cwd()

    def _get_default_index_path(self) -> Path:
        """Get default path for centralized index storage."""
        # Check environment variable first
        env_path = os.getenv("MCP_INDEX_STORAGE_PATH")
        if env_path:
            return Path(env_path).expanduser()

        # Default to ~/.mcp/indexes
        return Path.home() / ".mcp" / "indexes"

    def get_index_storage_path(self) -> Path:
        """
        Get the path where indexes should be stored based on strategy.

        Returns:
            Path to index storage location
        """
        if self.storage_strategy == "centralized":
            return self.index_storage_path
        else:
            # For inline/portable, return repo-specific path
            return self.repository_root / ".mcp-index"

    def split_path(self, relative_path: str) -> Tuple[str, ...]:
        """
        Split relative path into components.

        Args:
            relative_path: Relative path to split

        Returns:
            Tuple of path components
        """
        # Normalize slashes
        relative_path = relative_path.replace("\\", "/")
        return tuple(relative_path.split("/"))

    def join_path(self, *components: str) -> str:
        """
        Join path components into a relative path.

        Args:
            *components: Path components to join

        Returns:
            Joined relative path with forward slashes
        """
        return "/".join(components)
