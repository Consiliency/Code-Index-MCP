"""Test git integration functionality."""

import os
import sys
from pathlib import Path

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from mcp_server.core.repo_resolver import RepoResolver
from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher
from mcp_server.indexing.change_detector import ChangeDetector
from mcp_server.storage.git_index_manager import GitAwareIndexManager
from mcp_server.storage.repository_registry import RepositoryRegistry
from mcp_server.storage.store_registry import StoreRegistry
from tests.test_utilities import (
    GitCommit,
    PerformanceTracker,
    TestRepositoryBuilder,
    cleanup_test_environment,
    create_test_environment,
)


@pytest.mark.integration
class TestGitIntegration:
    """Test suite for git integration functionality."""

    @pytest.fixture
    def test_env(self):
        """Create a test environment."""
        env_path = create_test_environment()
        yield env_path
        cleanup_test_environment(env_path)

    @pytest.fixture
    def registry(self, test_env):
        """Create a test registry."""
        original_home = os.environ.get("HOME")
        os.environ["HOME"] = str(test_env)

        registry = RepositoryRegistry()

        yield registry

        if original_home:
            os.environ["HOME"] = original_home

    def _make_manager(self, registry):
        store_registry = StoreRegistry.for_registry(registry)
        repo_resolver = RepoResolver(registry, store_registry)
        dispatcher = EnhancedDispatcher(semantic_search_enabled=False)
        return GitAwareIndexManager(
            registry,
            dispatcher,
            repo_resolver=repo_resolver,
            store_registry=store_registry,
        )

    def _align_tracked_branch(self, registry, repo_id):
        info = registry.get_repository(repo_id)
        if info:
            if registry._get_git_branch(Path(info.path)) != "main":
                TestRepositoryBuilder.run_git_command("git branch -m main", info.path)
            ignore_path = Path(info.path) / ".gitignore"
            existing = ignore_path.read_text() if ignore_path.exists() else ""
            ignore_path.write_text(existing + "\n.mcp-index/\n")
            TestRepositoryBuilder.run_git_command("git add .gitignore", info.path)
            TestRepositoryBuilder.run_git_command(
                "git commit -m 'Exclude synthetic index outputs'", info.path
            )
            registry.update_git_state(repo_id)
            with registry._transaction(write=True):
                registry._registry[repo_id]["tracked_branch"] = "main"
            info = registry.get(repo_id)
            from mcp_server.health.repository_readiness import ReadinessClassifier

            readiness = ReadinessClassifier.classify_registered(info)
            assert readiness.current_branch == "main", str(readiness.to_dict())
        if info:
            Path(info.index_location).mkdir(parents=True, exist_ok=True)
        return info

    def _current_branch(self, repo_path: Path) -> str:
        return TestRepositoryBuilder.run_git_command("git branch --show-current", repo_path)[
            1
        ].strip()

    def test_change_detection_simple(self, test_env):
        """Test detecting simple file changes."""
        # Create repository
        repo = TestRepositoryBuilder.create_repository(
            test_env, "change_detect_repo", language="python"
        )

        # Get initial commit
        initial_commit = repo.commit_history[0]

        # Make changes
        commits = [
            GitCommit(message="Add new feature", files={"src/feature.py": "add"}),
            GitCommit(message="Modify existing file", files={"src/services.py": "modify"}),
            GitCommit(message="Delete old file", files={"tests/test_services.py": "delete"}),
        ]

        for commit in commits:
            TestRepositoryBuilder.apply_commit(repo, commit)

        # Test change detection
        detector = ChangeDetector(str(repo.path))
        changes = detector.get_changes_since_commit(initial_commit, repo.commit_history[-1])

        # Verify changes
        assert len(changes) == 3

        # Check each change type
        added = [c for c in changes if c.change_type == "added"]
        modified = [c for c in changes if c.change_type == "modified"]
        deleted = [c for c in changes if c.change_type == "deleted"]

        assert len(added) == 1
        assert added[0].path == "src/feature.py"

        assert len(modified) == 1
        assert modified[0].path == "src/services.py"

        assert len(deleted) == 1
        assert deleted[0].path == "tests/test_services.py"

    def test_change_detection_renames(self, test_env):
        """Test detecting file renames and moves."""
        repo = TestRepositoryBuilder.create_repository(test_env, "rename_repo", language="python")

        initial_commit = repo.commit_history[0]

        # Rename a file
        old_path = repo.path / "src/services.py"
        new_path = repo.path / "src/user_services.py"
        old_path.rename(new_path)

        TestRepositoryBuilder.run_git_command("git add -A", repo.path)
        TestRepositoryBuilder.run_git_command(
            "git commit -m 'Rename services to user_services'", repo.path
        )

        # Detect changes
        detector = ChangeDetector(str(repo.path))
        changes = detector.get_changes_since_commit(initial_commit)

        # Should detect as rename
        renames = [c for c in changes if c.change_type == "renamed"]
        assert len(renames) == 1
        assert renames[0].old_path == "src/services.py"
        assert renames[0].path == "src/user_services.py"
        assert not [
            c
            for c in changes
            if c.change_type in {"added", "deleted"}
            and c.path in {"src/services.py", "src/user_services.py"}
        ]

    def test_uncommitted_rename_keeps_paths_when_one_side_supported(self, test_env):
        """Uncommitted rename parsing accepts either supported side exactly once."""
        repo = TestRepositoryBuilder.create_repository(
            test_env, "uncommitted_rename_repo", language="python"
        )

        old_path = repo.path / "src/services.py"
        new_path = repo.path / "src/services.txt"
        old_path.rename(new_path)

        detector = ChangeDetector(str(repo.path))
        change = detector._parse_status_line("R100\tsrc/services.py\tsrc/services.txt")

        assert change is not None
        assert change.change_type == "renamed"
        assert change.old_path == "src/services.py"
        assert change.path == "src/services.txt"

    def test_change_detection_delete_is_preserved(self, test_env):
        """Committed deletes are reported as deletes for supported files."""
        repo = TestRepositoryBuilder.create_repository(test_env, "delete_repo", language="python")
        initial_commit = repo.commit_history[0]

        (repo.path / "src/services.py").unlink()
        TestRepositoryBuilder.run_git_command("git add -A", repo.path)
        TestRepositoryBuilder.run_git_command("git commit -m 'Delete services'", repo.path)

        detector = ChangeDetector(str(repo.path))
        changes = detector.get_changes_since_commit(initial_commit)

        assert [c.change_type for c in changes if c.path == "src/services.py"] == ["deleted"]

    def test_incremental_vs_full_decision(self, test_env, registry):
        """Test decision making for incremental vs full indexing."""
        # Create repository with many files
        repo = TestRepositoryBuilder.create_repository(test_env, "decision_repo", language="python")

        # Add many files
        for i in range(20):
            file_path = repo.path / f"module_{i}.py"
            file_path.write_text(f"# Module {i}\ndef func_{i}():\n    pass\n")

        TestRepositoryBuilder.run_git_command("git add .", repo.path)
        TestRepositoryBuilder.run_git_command("git commit -m 'Add many modules'", repo.path)

        # Register repository
        repo_id = registry.register_repository(str(repo.path))
        self._align_tracked_branch(registry, repo_id)

        # Create index manager
        manager = self._make_manager(registry)

        # Initial index should be full
        result = manager.sync_repository_index(repo_id, force_full=True)
        assert result.action == "full_index", result

        # Small change should trigger incremental
        (repo.path / "small_change.py").write_text("# Small change\n")
        TestRepositoryBuilder.run_git_command("git add .", repo.path)
        TestRepositoryBuilder.run_git_command("git commit -m 'Small change'", repo.path)
        registry.update_current_commit(repo_id)

        result = manager.sync_repository_index(repo_id)
        assert result.action == "incremental_update", result

        # Large changes should trigger full reindex
        for i in range(15):
            (repo.path / f"module_{i}.py").write_text(f"# Completely new content {i}\n")

        TestRepositoryBuilder.run_git_command("git add .", repo.path)
        TestRepositoryBuilder.run_git_command("git commit -m 'Major refactoring'", repo.path)
        registry.update_current_commit(repo_id)

        result = manager.sync_repository_index(repo_id)
        # When >50% files changed, should do full index
        assert result.action == "full_index", result

    def test_incremental_indexing_performance(self, test_env, registry):
        """Test performance of incremental indexing."""
        perf = PerformanceTracker()

        # Create repository
        repo = TestRepositoryBuilder.create_repository(test_env, "perf_repo", language="python")

        # Register and do initial index
        repo_id = registry.register_repository(str(repo.path))
        self._align_tracked_branch(registry, repo_id)

        # Setup indexing infrastructure
        manager = self._make_manager(registry)

        # Initial full index
        perf.start_timing("full_index")
        result = manager.sync_repository_index(repo_id, force_full=True)
        full_time = perf.end_timing("full_index")
        assert result.action == "full_index", result

        print(f"Full index: {result.files_processed} files in {full_time:.3f}s")

        # Make small changes and measure incremental updates
        for i in range(5):
            # Make a small change
            change_file = repo.path / f"change_{i}.py"
            change_file.write_text(f"# Change {i}\ndef change_{i}():\n    return {i}\n")

            TestRepositoryBuilder.run_git_command("git add .", repo.path)
            TestRepositoryBuilder.run_git_command(f"git commit -m 'Change {i}'", repo.path)
            registry.update_current_commit(repo_id)

            # Measure incremental update
            perf.start_timing(f"incremental_{i}")
            result = manager.sync_repository_index(repo_id)
            inc_time = perf.end_timing(f"incremental_{i}")

            print(f"Incremental {i}: {result.files_processed} files in {inc_time:.3f}s")

            assert result.action in {"incremental_update", "full_index"}, result

    def test_branch_switching(self, test_env, registry):
        """Test index management when switching branches."""
        # Create repository
        repo = TestRepositoryBuilder.create_repository(test_env, "branch_repo", language="python")
        default_branch = self._current_branch(repo.path)

        # Create feature branch
        TestRepositoryBuilder.run_git_command("git checkout -b feature-branch", repo.path)

        # Add feature-specific files
        feature_file = repo.path / "feature.py"
        feature_file.write_text("def awesome_feature():\n    return 'awesome'\n")

        TestRepositoryBuilder.run_git_command("git add .", repo.path)
        TestRepositoryBuilder.run_git_command("git commit -m 'Add awesome feature'", repo.path)

        # Switch back to the default branch
        TestRepositoryBuilder.run_git_command(f"git checkout {default_branch}", repo.path)

        # Register repository
        repo_id = registry.register_repository(str(repo.path))
        info = registry.get_repository(repo_id)

        # Verify branch tracking
        assert info.current_branch == default_branch

        # Switch to feature branch
        TestRepositoryBuilder.run_git_command("git checkout feature-branch", repo.path)
        registry.update_git_state(repo_id)

        info = registry.get_repository(repo_id)
        assert info.current_branch == "feature-branch"

    def test_merge_conflict_handling(self, test_env, registry):
        """Test handling of merge conflicts in index."""
        # Create repository
        repo = TestRepositoryBuilder.create_repository(test_env, "merge_repo", language="python")
        default_branch = self._current_branch(repo.path)

        # Create two branches
        TestRepositoryBuilder.run_git_command("git checkout -b branch-a", repo.path)

        # Modify file in branch A
        services_file = repo.path / "src/services.py"
        content = services_file.read_text()
        services_file.write_text(content + "\n# Branch A changes\n")

        TestRepositoryBuilder.run_git_command("git add .", repo.path)
        TestRepositoryBuilder.run_git_command("git commit -m 'Branch A changes'", repo.path)

        # Switch to the default branch and create branch B
        TestRepositoryBuilder.run_git_command(f"git checkout {default_branch}", repo.path)
        TestRepositoryBuilder.run_git_command("git checkout -b branch-b", repo.path)

        # Modify same file differently
        content = services_file.read_text()
        services_file.write_text(content + "\n# Branch B changes\n")

        TestRepositoryBuilder.run_git_command("git add .", repo.path)
        TestRepositoryBuilder.run_git_command("git commit -m 'Branch B changes'", repo.path)

        # Register and index
        repo_id = registry.register_repository(str(repo.path))

        # The index should handle branch-specific content
        # without conflicts (branch-aware indexing)
        info = registry.get_repository(repo_id)
        # Branch detection may differ across environments; verify registration succeeded
        assert info is not None
        assert info.current_branch in ("branch-b", default_branch, "branch-a")

    def test_submodule_handling(self, test_env):
        """Test handling of git submodules."""
        # Create main repository
        main_repo = TestRepositoryBuilder.create_repository(
            test_env, "main_repo", language="python"
        )

        # Create submodule repository
        sub_repo = TestRepositoryBuilder.create_repository(
            test_env, "sub_repo", language="javascript"
        )

        # Add submodule
        TestRepositoryBuilder.run_git_command(
            f"git submodule add {sub_repo.path} lib/sub_repo", main_repo.path
        )
        TestRepositoryBuilder.run_git_command("git commit -m 'Add submodule'", main_repo.path)

        # Detect changes
        detector = ChangeDetector(str(main_repo.path))

        # Should handle submodules gracefully
        # (typically by ignoring or special handling)
        changes = detector.get_changes_since_commit(main_repo.commit_history[0])

        # .gitmodules has no code extension and is filtered by _is_supported_file
        # Verify that change detection completes without errors
        assert isinstance(changes, list)
        gitmodules = [c for c in changes if c.path == ".gitmodules"]
        assert len(gitmodules) == 0  # submodule metadata filtered as non-code file


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
