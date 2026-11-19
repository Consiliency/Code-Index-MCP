"""
Semantic Database Discovery System for MCP
Automatically discovers and maps semantic collections to codebases.
"""

import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from qdrant_client import QdrantClient

from ..artifacts.semantic_namespace import SemanticNamespaceResolver

logger = logging.getLogger(__name__)


class SemanticDatabaseDiscovery:
    """Discovers and maps semantic collections to specific codebases."""

    def __init__(self, workspace_root: Path, qdrant_paths: Optional[List[str]] = None):
        """Initialize semantic database discovery.

        Args:
            workspace_root: Root directory of the current workspace
            qdrant_paths: Optional list of Qdrant database paths to search
        """
        self.workspace_root = Path(workspace_root)
        self.qdrant_paths = qdrant_paths or self._discover_qdrant_paths()
        self._clients: Dict[str, QdrantClient] = {}
        self._namespace_resolver = SemanticNamespaceResolver()

    def _discover_qdrant_paths(self) -> List[str]:
        """Discover available Qdrant database paths."""
        paths = []

        # Standard paths
        candidates = [
            ".indexes/qdrant/main.qdrant",  # Centralized
            "vector_index.qdrant",  # Legacy local
            ".mcp-index/vector_index.qdrant",  # Kit location
            "data/indexes/vector_index.qdrant",  # Alternative
        ]

        for candidate in candidates:
            full_path = self.workspace_root / candidate
            if full_path.exists():
                paths.append(str(full_path))

        return paths

    def _get_client(self, qdrant_path: str) -> Optional[QdrantClient]:
        """Get or create Qdrant client for a path."""
        if qdrant_path not in self._clients:
            try:
                # Handle potential lock issues
                lock_file = Path(qdrant_path) / ".lock"
                if lock_file.exists():
                    logger.warning(f"Removing stale lock: {lock_file}")
                    try:
                        lock_file.unlink()
                    except OSError:
                        pass

                client = QdrantClient(path=qdrant_path)
                # Test connection
                client.get_collections()
                self._clients[qdrant_path] = client
                logger.info(f"Connected to Qdrant: {qdrant_path}")
            except Exception as e:
                logger.warning(f"Failed to connect to {qdrant_path}: {e}")
                return None

        return self._clients.get(qdrant_path)

    def get_repository_identifier(self) -> str:
        """Get unique identifier for the current repository."""
        repo_identifier = self._get_repository_namespace_identifier()
        return self._namespace_resolver.derive_repo_hash(repo_identifier)

    def _get_repository_namespace_identifier(self) -> str:
        """Return raw repository identifier for namespace derivation."""
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                cwd=str(self.workspace_root),
                check=True,
            )
            remote_url = result.stdout.strip()
            if remote_url:
                return remote_url
        except subprocess.CalledProcessError, FileNotFoundError:
            pass

        return str(self.workspace_root)

    def _get_git_branch(self) -> Optional[str]:
        """Return current branch name when available."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(self.workspace_root),
                check=True,
            )
        except subprocess.CalledProcessError, FileNotFoundError:
            return None

        branch = result.stdout.strip()
        if not branch or branch == "HEAD":
            return None
        return branch

    def _get_git_commit(self) -> Optional[str]:
        """Return current commit hash when available."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(self.workspace_root),
                check=True,
            )
        except subprocess.CalledProcessError, FileNotFoundError:
            return None

        commit = result.stdout.strip()
        return commit or None

    def find_codebase_collections(self) -> List[Tuple[str, str, Dict]]:
        """Find collections that contain files from the current codebase.

        Returns:
            List of (qdrant_path, collection_name, metadata) tuples
        """
        matches = []
        repo_patterns = self._get_repository_patterns()

        for qdrant_path in self.qdrant_paths:
            client = self._get_client(qdrant_path)
            if not client:
                continue

            try:
                collections = client.get_collections()
                for collection in collections.collections:
                    # Skip obvious test collections
                    if self._is_test_collection(collection.name):
                        continue

                    # Sample data to check if it matches current codebase
                    try:
                        sample = client.scroll(
                            collection_name=collection.name, limit=50, with_payload=True
                        )

                        if not sample[0]:  # Empty collection
                            continue

                        # Analyze file paths to see if they match current codebase
                        file_paths = self._extract_file_paths(sample[0])
                        match_score = self._calculate_match_score(file_paths, repo_patterns)

                        if match_score > 0.1:  # At least 10% match
                            metadata = {
                                "match_score": match_score,
                                "sample_files": file_paths[:5],
                                "total_files": len(file_paths),
                                "collection_status": "active",
                            }
                            matches.append((qdrant_path, collection.name, metadata))
                            logger.info(
                                f"Found matching collection: {collection.name} (score: {match_score:.2f})"
                            )

                    except Exception as e:
                        logger.debug(f"Error sampling collection {collection.name}: {e}")

            except Exception as e:
                logger.warning(f"Error listing collections in {qdrant_path}: {e}")

        # Sort by match score
        matches.sort(key=lambda x: x[2]["match_score"], reverse=True)
        return matches

    def _get_repository_patterns(self) -> Set[str]:
        """Get patterns that identify files from the current repository."""
        patterns = set()

        # Workspace root name and path components
        workspace_name = self.workspace_root.name.lower()
        patterns.add(workspace_name)

        # Common patterns for this specific repository
        if "code-index-mcp" in workspace_name:
            patterns.update(
                [
                    "code-index-mcp",
                    "mcp_server",
                    "/workspaces/code-index-mcp",
                    "code_index_mcp",
                    "mcp-server",
                ]
            )

        # Add absolute path patterns
        abs_path = str(self.workspace_root).lower()
        patterns.add(abs_path)

        # Add git repository name if available
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                cwd=str(self.workspace_root),
                check=True,
            )
            remote_url = result.stdout.strip()
            if remote_url:
                # Extract repo name from URL
                repo_name = remote_url.split("/")[-1].replace(".git", "").lower()
                patterns.add(repo_name)
        except Exception:
            pass

        return patterns

    def _is_test_collection(self, collection_name: str) -> bool:
        """Check if a collection name indicates it's for test data."""
        test_indicators = [
            "typescript-",  # Test repo collections
            "test-",
            "sample-",
            "demo-",
            "fixture-",
        ]

        collection_lower = collection_name.lower()
        return any(indicator in collection_lower for indicator in test_indicators)

    def _extract_file_paths(self, points) -> List[str]:
        """Extract file paths from Qdrant points."""
        file_paths = []
        for point in points:
            # Try different payload keys
            file_path = (
                point.payload.get("file")
                or point.payload.get("relative_path")
                or point.payload.get("filepath")
                or point.payload.get("path")
                or ""
            )
            if file_path:
                file_paths.append(file_path.lower())

            # Also check content or other fields that might contain path info
            for key, value in point.payload.items():
                if isinstance(value, str) and ("/" in value or "\\" in value):
                    # Might be a path
                    if any(
                        ext in value
                        for ext in [
                            ".py",
                            ".js",
                            ".ts",
                            ".java",
                            ".go",
                            ".rs",
                            ".cpp",
                            ".c",
                        ]
                    ):
                        file_paths.append(value.lower())
                        break

        return list(set(file_paths))  # Remove duplicates

    def _calculate_match_score(self, file_paths: List[str], repo_patterns: Set[str]) -> float:
        """Calculate how well file paths match the current repository."""
        if not file_paths:
            return 0.0

        matches = 0
        for file_path in file_paths:
            if any(pattern in file_path for pattern in repo_patterns):
                matches += 1

        return matches / len(file_paths)

    def get_best_collection(self) -> Optional[Tuple[str, str]]:
        """Get the best semantic collection for the current codebase.

        Returns:
            Tuple of (qdrant_path, collection_name) or None if not found
        """
        matches = self.find_codebase_collections()

        if matches:
            best_match = matches[0]
            qdrant_path, collection_name, metadata = best_match
            logger.info(
                f"Selected semantic collection: {collection_name} (score: {metadata['match_score']:.2f})"
            )
            return (qdrant_path, collection_name)

        # If no matches found, check if code-embeddings has data
        logger.info("No specific collection found, checking code-embeddings fallback...")
        for qdrant_path in self.qdrant_paths:
            client = self._get_client(qdrant_path)
            if not client:
                continue

            try:
                # Check if code-embeddings exists and has data
                info = client.get_collection("code-embeddings")
                points_count = info.points_count or 0
                if points_count > 0:
                    logger.info(
                        f"Using fallback collection 'code-embeddings' with {points_count} points"
                    )
                    return (qdrant_path, "code-embeddings")
            except Exception:
                continue

        return None

    def get_default_collection_config(self) -> Tuple[str, str]:
        """Get default collection configuration for current codebase.

        Returns:
            Tuple of (qdrant_path, collection_name)
        """
        # Try centralized path first
        centralized_path = str(self.workspace_root / ".indexes/qdrant/main.qdrant")
        if Path(centralized_path).exists():
            repo_identifier = self._get_repository_namespace_identifier()
            lineage_id = self._namespace_resolver.derive_lineage_id(
                self._get_git_branch(), self._get_git_commit()
            )
            collection_name = self._namespace_resolver.resolve_collection_name(
                repo_identifier=repo_identifier,
                profile_id="default",
                lineage_id=lineage_id,
            )
            return (centralized_path, collection_name)

        # Fall back to legacy path
        legacy_path = str(self.workspace_root / "vector_index.qdrant")
        return (legacy_path, "code-embeddings")

    def create_codebase_collection(self, force: bool = False) -> Tuple[str, str]:
        """Create a new semantic collection for the current codebase.

        Args:
            force: Whether to recreate if collection already exists

        Returns:
            Tuple of (qdrant_path, collection_name)
        """
        qdrant_path, collection_name = self.get_default_collection_config()

        try:
            client = self._get_client(qdrant_path)
            if not client:
                raise RuntimeError(f"Cannot connect to Qdrant at {qdrant_path}")

            # Check if collection exists
            collections = client.get_collections()
            exists = any(col.name == collection_name for col in collections.collections)

            if exists and not force:
                logger.info(f"Collection {collection_name} already exists")
                return (qdrant_path, collection_name)

            # Create or recreate collection
            from qdrant_client import models

            if exists:
                logger.info(f"Recreating collection: {collection_name}")
                client.delete_collection(collection_name)
            else:
                logger.info(f"Creating collection: {collection_name}")

            client.create_collection(
                collection_name=collection_name,
                vectors_config=models.VectorParams(
                    size=1024,
                    distance=models.Distance.COSINE,  # Voyage Code 3 dimension
                ),
            )

            logger.info(f"Successfully created collection: {collection_name}")
            return (qdrant_path, collection_name)

        except Exception as e:
            logger.error(f"Failed to create collection: {e}")
            raise

    def get_collection_summary(self) -> Dict:
        """Get summary of all available collections and their relevance."""
        summary = {
            "repository_id": self.get_repository_identifier(),
            "workspace_root": str(self.workspace_root),
            "qdrant_paths": self.qdrant_paths,
            "collections": [],
            "recommendations": [],
        }

        # Find all collections
        matches = self.find_codebase_collections()

        for qdrant_path, collection_name, metadata in matches:
            summary["collections"].append(
                {
                    "qdrant_path": qdrant_path,
                    "collection_name": collection_name,
                    "match_score": metadata["match_score"],
                    "sample_files": metadata["sample_files"],
                    "recommendation": "primary" if metadata["match_score"] > 0.5 else "secondary",
                }
            )

        # Add recommendations
        if not matches:
            summary["recommendations"].append("No existing collections found for this codebase")
            summary["recommendations"].append("Consider creating a new semantic index")
        else:
            best_match = matches[0]
            if best_match[2]["match_score"] < 0.3:
                summary["recommendations"].append(
                    "Low match confidence - may need new semantic index"
                )
            else:
                summary["recommendations"].append(f"Use collection: {best_match[1]}")

        return summary
