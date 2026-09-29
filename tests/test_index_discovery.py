"""
Tests for Multi-Path Index Discovery

These tests verify that indexes can be found across multiple locations,
fixing the issue where test repositories couldn't find their indexes.
"""

import json
import logging
import os
import sqlite3
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from mcp_server.config.index_paths import IndexPathConfig
from mcp_server.utils.index_discovery import (
    IndexDiscovery,
    _is_within_directory,
    _validate_tar_member,
)


class TestIndexPathConfig:
    """Test index path configuration."""

    def test_default_search_paths(self):
        """Test default search paths are configured correctly."""
        config = IndexPathConfig()

        assert len(config.search_paths) >= 5
        assert ".indexes/{repo_hash}" in config.search_paths
        assert ".mcp-index" in config.search_paths
        assert "test_indexes/{repo}" in config.search_paths

    def test_custom_search_paths(self):
        """Test custom search paths can be provided."""
        custom_paths = ["/custom/path1", "/custom/path2"]
        config = IndexPathConfig(custom_paths=custom_paths)

        assert config.search_paths == custom_paths

    def test_environment_search_paths(self):
        """Test search paths from environment variable."""
        original = os.environ.get("MCP_INDEX_PATHS")
        try:
            os.environ["MCP_INDEX_PATHS"] = "/env/path1:/env/path2:/env/path3"
            config = IndexPathConfig()

            assert config.search_paths == ["/env/path1", "/env/path2", "/env/path3"]
        finally:
            if original:
                os.environ["MCP_INDEX_PATHS"] = original
            else:
                del os.environ["MCP_INDEX_PATHS"]

    def test_environment_detection(self):
        """Test environment detection logic."""
        config = IndexPathConfig()
        env = config.environment

        assert "is_docker" in env
        assert "is_test" in env
        assert "is_ci" in env
        assert "workspace_root" in env

    def test_get_search_paths_with_substitution(self):
        """Test path template substitution."""
        config = IndexPathConfig(["{repo_hash}/index", "repos/{repo}/db", "/abs/path"])

        paths = config.get_search_paths("test-repo")

        # Should have paths with substitutions
        assert any("/abs/path" in p.as_posix() for p in paths)
        assert any("repos/test-repo/db" in p.as_posix() for p in paths)

    def test_repo_hash_detection(self):
        """Test repository hash detection."""
        config = IndexPathConfig()

        # Test with hash
        hash_str = "abcdef123456"
        assert config._get_repo_hash(hash_str) == hash_str

        # Test with name (should compute hash)
        name_hash = config._get_repo_hash("my-repo")
        assert name_hash is not None
        assert len(name_hash) == 12

    def test_add_remove_search_paths(self):
        """Test adding and removing search paths."""
        config = IndexPathConfig()
        original_count = len(config.search_paths)

        # Add path at end
        config.add_search_path("/new/path")
        assert len(config.search_paths) == original_count + 1
        assert config.search_paths[-1] == "/new/path"

        # Add path at beginning
        config.add_search_path("/priority/path", priority=0)
        assert config.search_paths[0] == "/priority/path"

        # Remove path
        config.remove_search_path("/new/path")
        assert "/new/path" not in config.search_paths


class TestIndexDiscovery:
    """Test enhanced index discovery functionality."""

    @pytest.fixture
    def temp_workspace(self):
        """Create a temporary workspace."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "test-repo"
            workspace.mkdir()
            yield workspace

    @pytest.fixture
    def create_test_index(self):
        """Helper to create a test SQLite index."""

        def _create(path: Path, with_tables: bool = True):
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path))

            if with_tables:
                # Create minimal schema
                conn.execute("""
                    CREATE TABLE files (
                        id INTEGER PRIMARY KEY,
                        path TEXT,
                        content TEXT
                    )
                """)
                conn.execute("""
                    CREATE TABLE symbols (
                        id INTEGER PRIMARY KEY,
                        name TEXT,
                        file_id INTEGER
                    )
                """)
                conn.execute("""
                    CREATE TABLE repositories (
                        id INTEGER PRIMARY KEY,
                        path TEXT
                    )
                """)

            conn.commit()
            conn.close()

        return _create

    def test_multi_path_discovery_enabled(self, temp_workspace):
        """Test that multi-path discovery is enabled by default."""
        discovery = IndexDiscovery(temp_workspace)

        assert discovery.enable_multi_path is True
        assert discovery.path_config is not None

    def test_is_within_directory_blocks_traversal(self, temp_workspace):
        """Ensure archive extraction guard rejects path traversal entries."""
        base = temp_workspace / ".mcp-index"
        base.mkdir(parents=True, exist_ok=True)

        assert _is_within_directory(base, base / "code_index.db") is True
        assert _is_within_directory(base, base / "subdir" / "../code_index.db") is True
        assert _is_within_directory(base, base / ".." / "outside.txt") is False

    def test_validate_tar_member_allows_safe_regular_file(self, temp_workspace):
        """Safe regular tar members should pass validation."""
        base = temp_workspace / ".mcp-index"
        member = tarfile.TarInfo(name="subdir/code_index.db")
        member.type = tarfile.REGTYPE

        assert _validate_tar_member(member, base) is True

    def test_validate_tar_member_blocks_traversal_member_name(self, temp_workspace):
        """Tar member names with traversal should be blocked."""
        base = temp_workspace / ".mcp-index"
        member = tarfile.TarInfo(name="../outside.db")
        member.type = tarfile.REGTYPE

        assert _validate_tar_member(member, base) is False

    def test_validate_tar_member_blocks_absolute_member_name(self, temp_workspace):
        """Absolute tar member names should be blocked."""
        base = temp_workspace / ".mcp-index"
        member = tarfile.TarInfo(name="/tmp/outside.db")
        member.type = tarfile.REGTYPE

        assert _validate_tar_member(member, base) is False

    def test_validate_tar_member_blocks_unsafe_symlink_target(self, temp_workspace):
        """Symlink targets escaping extraction directory should be blocked."""
        base = temp_workspace / ".mcp-index"
        member = tarfile.TarInfo(name="links/current")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside.db"

        assert _validate_tar_member(member, base) is False

    def test_validate_tar_member_allows_safe_symlink_target(self, temp_workspace):
        """Symlink targets inside extraction directory should be allowed."""
        base = temp_workspace / ".mcp-index"
        member = tarfile.TarInfo(name="links/current")
        member.type = tarfile.SYMTYPE
        member.linkname = "../code_index.db"

        assert _validate_tar_member(member, base) is True

    def test_validate_tar_member_blocks_unsafe_hardlink_target(self, temp_workspace):
        """Hardlink targets escaping extraction directory should be blocked."""
        base = temp_workspace / ".mcp-index"
        member = tarfile.TarInfo(name="links/current")
        member.type = tarfile.LNKTYPE
        member.linkname = "../../outside.db"

        assert _validate_tar_member(member, base) is False

    def test_validate_tar_member_allows_safe_hardlink_target(self, temp_workspace):
        """Hardlink targets inside extraction directory should be allowed."""
        base = temp_workspace / ".mcp-index"
        member = tarfile.TarInfo(name="links/current")
        member.type = tarfile.LNKTYPE
        member.linkname = "code_index.db"

        assert _validate_tar_member(member, base) is True

    def test_validate_tar_member_blocks_device_nodes(self, temp_workspace):
        """Device entries should be blocked from extraction."""
        base = temp_workspace / ".mcp-index"
        member = tarfile.TarInfo(name="devnode")
        member.type = tarfile.CHRTYPE

        assert _validate_tar_member(member, base) is False

    def test_validate_artifact_metadata_requires_compatibility_fields(self, temp_workspace):
        """Artifact metadata must include strict compatibility keys."""
        discovery = IndexDiscovery(temp_workspace)

        missing = {
            "checksum": "abc",
            "commit": "deadbeef",
            "branch": "main",
            "timestamp": "2026-01-01T00:00:00Z",
            "compatibility": {"embedding_model": "voyage-code-3"},
        }
        assert discovery._validate_artifact_metadata(missing) is not None

        valid = {
            "checksum": "abc",
            "commit": "deadbeef",
            "branch": "main",
            "timestamp": "2026-01-01T00:00:00Z",
            "compatibility": {
                "embedding_model": "voyage-code-3",
                "schema_version": "2",
            },
        }
        assert discovery._validate_artifact_metadata(valid) is None

    def test_find_index_in_legacy_location(self, temp_workspace, create_test_index, monkeypatch):
        """Test finding index in legacy .mcp-index location."""
        # Create .mcp-index.json to enable indexing
        config_file = temp_workspace / ".mcp-index.json"
        config_file.write_text(json.dumps({"enabled": True}))

        # Create index in legacy location
        legacy_index = temp_workspace / ".mcp-index" / "code_index.db"
        create_test_index(legacy_index)
        monkeypatch.setenv("MCP_INDEX_PATHS", str(legacy_index.parent))

        discovery = IndexDiscovery(temp_workspace)
        index_path = discovery.get_local_index_path()

        assert index_path == legacy_index

    def test_find_index_in_test_location(self, temp_workspace, create_test_index):
        """Test finding index in test_indexes location."""
        # Create .mcp-index.json to enable indexing
        config_file = temp_workspace / ".mcp-index.json"
        config_file.write_text(json.dumps({"enabled": True}))

        # Create test index location
        test_index_dir = temp_workspace.parent / "test_indexes" / temp_workspace.name
        test_index = test_index_dir / "code_index.db"
        create_test_index(test_index)

        # Mock the search paths to include our test location
        with patch.object(IndexPathConfig, "get_search_paths") as mock_paths:
            mock_paths.return_value = [
                temp_workspace / ".mcp-index",  # Legacy (not exists)
                test_index_dir,  # Test location (exists)
            ]

            discovery = IndexDiscovery(temp_workspace)
            index_path = discovery.get_local_index_path()

            assert index_path == test_index

    def test_find_index_with_current_db(self, temp_workspace, create_test_index):
        """Test finding index named current.db."""
        # Create .mcp-index.json to enable indexing
        config_file = temp_workspace / ".mcp-index.json"
        config_file.write_text(json.dumps({"enabled": True}))

        # Create index with current.db name
        index_dir = temp_workspace / ".indexes" / "abc123"
        current_db = index_dir / "current.db"
        create_test_index(current_db)

        with patch.object(IndexPathConfig, "get_search_paths") as mock_paths:
            mock_paths.return_value = [index_dir]

            discovery = IndexDiscovery(temp_workspace)
            index_path = discovery.get_local_index_path()

            assert index_path == current_db

    def test_manifest_selection_prefers_requested_model(
        self, temp_workspace, create_test_index, caplog
    ):
        """Test manifest-driven model preference across multiple indexes."""
        config_file = temp_workspace / ".mcp-index.json"
        config_file.write_text(json.dumps({"enabled": True}))

        index_a = temp_workspace / "test_indexes" / temp_workspace.name / "code_index.db"
        index_b = temp_workspace / "test_indexes" / f"{temp_workspace.name}_alt" / "code_index.db"
        create_test_index(index_a)
        create_test_index(index_b)

        discovery = IndexDiscovery(temp_workspace)
        discovery.write_index_manifest(
            index_a,
            schema_version="1.0",
            embedding_model="text-embedding-a",
            creation_commit="commit-a",
        )
        discovery.write_index_manifest(
            index_b,
            schema_version="1.0",
            embedding_model="text-embedding-b",
            creation_commit="commit-b",
        )

        with patch.object(IndexPathConfig, "get_search_paths") as mock_paths:
            mock_paths.return_value = [index_a.parent, index_b.parent]

            selected = discovery.get_local_index_path(
                requested_schema_version="1.0",
                requested_embedding_model="text-embedding-b",
            )
            assert selected == index_b

            caplog.set_level(logging.WARNING)
            caplog.clear()
            fallback = discovery.get_local_index_path(
                requested_schema_version="1.0",
                requested_embedding_model="text-embedding-c",
            )

            assert fallback in (index_a, index_b)
            assert any("embedding model" in message for message in caplog.messages)

    def test_manifest_selection_strict_mode_requires_exact_match(
        self, temp_workspace, create_test_index
    ):
        """Strict compatibility mode should reject non-exact manifest matches."""
        config_file = temp_workspace / ".mcp-index.json"
        config_file.write_text(json.dumps({"enabled": True}))

        index_a = temp_workspace / "test_indexes" / temp_workspace.name / "code_index.db"
        create_test_index(index_a)

        discovery = IndexDiscovery(temp_workspace)
        discovery.write_index_manifest(
            index_a,
            schema_version="2",
            embedding_model="voyage-code-3",
            creation_commit="commit-a",
        )

        with patch.object(IndexPathConfig, "get_search_paths") as mock_paths:
            mock_paths.return_value = [index_a.parent]

            selected = discovery.get_local_index_path(
                requested_schema_version="2",
                requested_embedding_model="voyage-code-3",
                strict_compatibility=True,
            )
            assert selected == index_a

            rejected = discovery.get_local_index_path(
                requested_schema_version="999",
                requested_embedding_model="voyage-code-3",
                strict_compatibility=True,
            )
            assert rejected is None

    def test_validate_sqlite_index(self, temp_workspace, create_test_index):
        """Test SQLite index validation."""
        discovery = IndexDiscovery(temp_workspace)

        # Valid index
        valid_index = temp_workspace / "valid.db"
        create_test_index(valid_index, with_tables=True)
        assert discovery._validate_sqlite_index(valid_index) is True

        # Invalid index (no tables)
        invalid_index = temp_workspace / "invalid.db"
        create_test_index(invalid_index, with_tables=False)
        assert discovery._validate_sqlite_index(invalid_index) is False

        # Non-existent index
        assert discovery._validate_sqlite_index(temp_workspace / "missing.db") is False

    def test_get_repository_identifier(self, temp_workspace):
        """Test repository identifier extraction."""
        discovery = IndexDiscovery(temp_workspace)

        # Mock git command
        with patch("subprocess.run") as mock_run:
            # Successful git remote
            mock_run.return_value = Mock(returncode=0, stdout="https://github.com/user/repo.git\n")

            identifier = discovery._get_repository_identifier()
            assert identifier == "https://github.com/user/repo.git"

            # No git remote
            mock_run.return_value = Mock(returncode=1)
            identifier = discovery._get_repository_identifier()
            assert identifier == temp_workspace.name

    def test_find_all_indexes(self, temp_workspace, create_test_index):
        """Test finding all available indexes."""
        # Create multiple indexes
        paths = [
            temp_workspace / ".mcp-index" / "code_index.db",
            temp_workspace / ".indexes" / "abc123" / "current.db",
            temp_workspace / "test_indexes" / "repo" / "code_index.db",
        ]

        for path in paths:
            create_test_index(path)

        # Create .mcp-index.json to enable indexing
        config_file = temp_workspace / ".mcp-index.json"
        config_file.write_text(json.dumps({"enabled": True}))

        with patch.object(IndexPathConfig, "get_search_paths") as mock_paths:
            mock_paths.return_value = [p.parent for p in paths]

            discovery = IndexDiscovery(temp_workspace)
            all_indexes = discovery.find_all_indexes()

            assert len(all_indexes) >= 3
            assert all(idx["valid"] for idx in all_indexes)
            assert any(idx["location_type"] == "legacy" for idx in all_indexes)
            assert any(idx["location_type"] == "centralized" for idx in all_indexes)
            assert any(idx["location_type"] == "test" for idx in all_indexes)

    def test_classify_location(self, temp_workspace):
        """Test location classification."""
        discovery = IndexDiscovery(temp_workspace)

        assert discovery._classify_location(Path("/test_indexes/repo")) == "test"
        assert discovery._classify_location(Path("/.indexes/hash")) == "centralized"
        assert discovery._classify_location(Path("/.mcp-index")) == "legacy"
        assert discovery._classify_location(Path("/tmp/indexes")) == "temporary"
        assert discovery._classify_location(Path.home() / "indexes") == "user"
        assert discovery._classify_location(Path("/workspaces/project")) == "docker"
        assert discovery._classify_location(Path("/random/path")) == "other"

    def test_index_info_with_multi_path(self, temp_workspace, create_test_index):
        """Test index info includes search paths."""
        # Create .mcp-index.json
        config_file = temp_workspace / ".mcp-index.json"
        config_file.write_text(json.dumps({"enabled": True, "auto_download": True}))

        # Create index
        index_path = temp_workspace / ".mcp-index" / "code_index.db"
        create_test_index(index_path)

        discovery = IndexDiscovery(temp_workspace)
        info = discovery.get_index_info()

        assert info["enabled"] is True
        assert info["has_local_index"] is True
        assert info["found_at"] is not None
        assert len(info["search_paths"]) > 0

    def test_multi_path_disabled(self, temp_workspace):
        """Test behavior when multi-path is disabled."""
        discovery = IndexDiscovery(temp_workspace, enable_multi_path=False)

        assert discovery.enable_multi_path is False
        assert discovery.path_config is None

        # Should only check default locations
        info = discovery.get_index_info()
        assert "search_paths" not in info or len(info["search_paths"]) == 0


class TestIntegration:
    """Integration tests for multi-path discovery in real scenarios."""

    @pytest.fixture
    def mock_environments(self, tmp_path):
        """Create mock environments for testing."""
        envs = {
            "docker": tmp_path / "docker_env",
            "native": tmp_path / "native_env",
            "test": tmp_path / "test_env",
        }

        for env_path in envs.values():
            env_path.mkdir(parents=True)

        return envs

    def test_docker_vs_native_path_resolution(self, mock_environments, create_test_index):
        """Test that indexes work across Docker and native environments."""
        # Create index in Docker-style path
        docker_index = (
            mock_environments["docker"]
            / "workspaces"
            / "project"
            / ".indexes"
            / "hash123"
            / "current.db"
        )
        create_test_index(docker_index)

        # Test from native environment
        native_workspace = mock_environments["native"] / "project"
        native_workspace.mkdir()

        # Create config
        config_file = native_workspace / ".mcp-index.json"
        config_file.write_text(json.dumps({"enabled": True}))

        # Mock environment detection and path resolution
        with patch.object(IndexPathConfig, "_detect_environment") as mock_env:
            mock_env.return_value = {
                "is_docker": False,
                "is_test": False,
                "workspace_root": None,
                "project_name": "project",
            }

            with patch.object(IndexPathConfig, "get_search_paths") as mock_paths:
                mock_paths.return_value = [
                    native_workspace / ".indexes" / "hash123",  # Native path (doesn't exist)
                    docker_index.parent,  # Docker path (exists)
                ]

                discovery = IndexDiscovery(native_workspace)
                index_path = discovery.get_local_index_path()

                assert index_path == docker_index

    def test_test_environment_priority(self, mock_environments, create_test_index):
        """Test that test indexes are found with priority."""
        # Create indexes in multiple locations
        prod_index = mock_environments["test"] / "project" / ".indexes" / "hash" / "code_index.db"
        test_index = mock_environments["test"] / "test_indexes" / "project" / "code_index.db"

        create_test_index(prod_index)
        create_test_index(test_index)

        workspace = mock_environments["test"] / "project"
        config_file = workspace / ".mcp-index.json"
        config_file.write_text(json.dumps({"enabled": True}))

        # Configure to check test location first
        with patch.object(IndexPathConfig, "get_search_paths") as mock_paths:
            mock_paths.return_value = [
                test_index.parent,  # Test location (first)
                prod_index.parent,  # Prod location (second)
            ]

            discovery = IndexDiscovery(workspace)
            index_path = discovery.get_local_index_path()

            # Should find test index first
            assert index_path == test_index

    def test_performance_with_many_paths(self, temp_workspace, create_test_index):
        """Test performance when searching many paths."""
        import time

        # Create 20 search paths
        search_paths = []
        for i in range(20):
            path = temp_workspace / f"path_{i}"
            path.mkdir()
            search_paths.append(path)

        # Put index in the last path
        last_index = search_paths[-1] / "code_index.db"
        create_test_index(last_index)

        # Enable indexing
        config_file = temp_workspace / ".mcp-index.json"
        config_file.write_text(json.dumps({"enabled": True}))

        with patch.object(IndexPathConfig, "get_search_paths") as mock_paths:
            mock_paths.return_value = search_paths

            discovery = IndexDiscovery(temp_workspace)

            start_time = time.time()
            index_path = discovery.get_local_index_path()
            elapsed = time.time() - start_time

            assert index_path == last_index
            assert elapsed < 1.0  # Should complete within 1 second even with 20 paths

    def test_find_latest_artifact_supports_both_name_prefixes(self, tmp_path):
        """Latest artifact discovery should include both index naming schemes."""
        discovery = IndexDiscovery(tmp_path)

        payload = {
            "artifacts": [
                {
                    "id": 1,
                    "name": "index-aaaa1111-main-20260101",
                    "created_at": "2026-01-01T00:00:00Z",
                },
                {
                    "id": 2,
                    "name": "mcp-index-bbbb2222",
                    "created_at": "2026-01-02T00:00:00Z",
                },
            ]
        }

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=json.dumps(payload))
            artifact = discovery._find_latest_artifact("owner/repo")

        assert artifact is not None
        assert artifact["id"] == 2

    def test_find_recovery_artifact_prefers_promoted_match(self, tmp_path):
        """Recovery selection should prefer promoted artifacts among matches."""
        discovery = IndexDiscovery(tmp_path)

        payload = {
            "artifacts": [
                {
                    "id": 11,
                    "name": "index-1234abcd-main-20260101",
                    "created_at": "2026-01-02T00:00:00Z",
                },
                {
                    "id": 12,
                    "name": "index-1234abcd-main-20260101-promoted",
                    "created_at": "2026-01-01T00:00:00Z",
                },
            ]
        }

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=json.dumps(payload))
            artifact = discovery._find_recovery_artifact(
                "owner/repo", branch="main", commit="1234abcd"
            )

        assert artifact is not None
        assert artifact["id"] == 12

    def test_download_recovery_index_requires_selector(self, tmp_path):
        """Recovery download should fail when neither branch nor commit is provided."""
        discovery = IndexDiscovery(tmp_path)
        assert discovery.download_recovery_index(branch=None, commit=None) is False
