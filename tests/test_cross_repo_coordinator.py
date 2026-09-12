"""
Tests for Cross-Repository Search Coordinator
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from mcp_server.dispatcher.cross_repo_coordinator import (
    AggregatedResult,
    CrossRepositorySearchCoordinator,
    SearchScope,
    get_cross_repo_coordinator,
)
from mcp_server.storage.multi_repo_manager import (
    CrossRepoSearchResult,
    MultiRepositoryManager,
    RepositoryInfo,
)


@pytest.fixture
def mock_multi_repo_manager():
    """Create a mock multi-repository manager."""
    manager = Mock(spec=MultiRepositoryManager)

    # Create mock repositories
    repo1 = RepositoryInfo(
        repository_id="repo1",
        name="Test Repo 1",
        path=Path("/test/repo1"),
        index_path=Path("/test/repo1/.index/test.db"),
        language_stats={"python": 10, "javascript": 5},
        total_files=15,
        total_symbols=100,
        indexed_at=datetime.now(),
        priority=1,
    )

    repo2 = RepositoryInfo(
        repository_id="repo2",
        name="Test Repo 2",
        path=Path("/test/repo2"),
        index_path=Path("/test/repo2/.index/test.db"),
        language_stats={"java": 8, "python": 3},
        total_files=11,
        total_symbols=80,
        indexed_at=datetime.now(),
        priority=2,
    )

    manager.list_repositories.return_value = [repo1, repo2]
    return manager


@pytest.fixture
def coordinator(mock_multi_repo_manager):
    """Create a test coordinator instance."""
    return CrossRepositorySearchCoordinator(
        multi_repo_manager=mock_multi_repo_manager, max_workers=2, default_result_limit=50
    )


@pytest.fixture
def sample_search_scope():
    """Create a sample search scope."""
    return SearchScope(
        repositories=["repo1", "repo2"],
        languages=["python"],
        max_repositories=5,
        priority_order=True,
    )


class TestCrossRepositorySearchCoordinator:
    """Test cases for CrossRepositorySearchCoordinator."""

    def test_initialization(self, coordinator):
        """Test coordinator initialization."""
        assert coordinator.max_workers == 2
        assert coordinator.default_result_limit == 50
        assert coordinator.multi_repo_manager is not None
        assert coordinator.memory_manager is not None
        assert coordinator.plugin_loader is not None

    @pytest.mark.asyncio
    async def test_get_target_repositories_all(self, coordinator):
        """Test getting all target repositories."""
        scope = SearchScope()
        repos = await coordinator._get_target_repositories(scope)

        assert len(repos) == 2
        assert repos[0].repository_id in ["repo1", "repo2"]

    @pytest.mark.asyncio
    async def test_get_target_repositories_filtered(self, coordinator):
        """Test getting filtered target repositories."""
        scope = SearchScope(repositories=["repo1"])
        repos = await coordinator._get_target_repositories(scope)

        assert len(repos) == 1
        assert repos[0].repository_id == "repo1"

    @pytest.mark.asyncio
    async def test_get_target_repositories_by_language(self, coordinator):
        """Language filters belong to indexed rows, not stale registry statistics."""
        scope = SearchScope(languages=["java"])
        repos = await coordinator._get_target_repositories(scope)

        assert {repo.repository_id for repo in repos} == {"repo1", "repo2"}

    @pytest.mark.asyncio
    async def test_get_target_repositories_priority_order(self, coordinator):
        """Test repository ordering by priority."""
        scope = SearchScope(priority_order=True)
        repos = await coordinator._get_target_repositories(scope)

        assert len(repos) == 2
        # repo2 has higher priority (2) than repo1 (1)
        assert repos[0].priority >= repos[1].priority

    @pytest.mark.asyncio
    async def test_get_target_repositories_max_limit(self, coordinator):
        """Test limiting maximum repositories."""
        scope = SearchScope(max_repositories=1)
        repos = await coordinator._get_target_repositories(scope)

        assert len(repos) == 1

    @pytest.mark.asyncio
    async def test_search_symbol_empty_repos(self, coordinator):
        """Test symbol search with no target repositories."""
        coordinator.multi_repo_manager.list_repositories.return_value = []

        result = await coordinator.search_symbol("test_symbol")

        assert result.total_results == 0
        assert result.repositories_searched == 0
        assert result.query == "test_symbol"

    @pytest.mark.asyncio
    @patch("mcp_server.dispatcher.cross_repo_coordinator.ThreadPoolExecutor")
    async def test_search_symbol_success(self, mock_executor, coordinator):
        """Test successful symbol search."""
        # Mock the executor and future results
        mock_future1 = Mock()
        mock_future1.result.return_value = CrossRepoSearchResult(
            repository_id="repo1",
            repository_name="Test Repo 1",
            results=[
                {
                    "symbol": "test_function",
                    "file_path": "/test/repo1/file1.py",
                    "line_number": 10,
                    "signature": "def test_function():",
                }
            ],
            search_time=0.1,
        )

        mock_future2 = Mock()
        mock_future2.result.return_value = CrossRepoSearchResult(
            repository_id="repo2",
            repository_name="Test Repo 2",
            results=[
                {
                    "symbol": "test_function",
                    "file_path": "/test/repo2/file2.py",
                    "line_number": 15,
                    "signature": "def test_function():",
                }
            ],
            search_time=0.2,
        )

        mock_executor_instance = Mock()
        mock_executor_instance.submit.side_effect = [mock_future1, mock_future2]
        mock_executor_instance.__enter__ = Mock(return_value=mock_executor_instance)
        mock_executor_instance.__exit__ = Mock(return_value=None)
        mock_executor.return_value = mock_executor_instance

        # Mock as_completed to return our futures
        with patch(
            "mcp_server.dispatcher.cross_repo_coordinator.as_completed"
        ) as mock_as_completed:
            mock_as_completed.return_value = [mock_future1, mock_future2]

            result = await coordinator.search_symbol("test_function")

            assert result.total_results == 2
            assert result.repositories_searched == 2
            assert result.query == "test_function"
            assert len(result.results) == 2
            assert "repository_id" in result.results[0]
            assert "repository_name" in result.results[0]

    @pytest.mark.asyncio
    @patch("mcp_server.dispatcher.cross_repo_coordinator.ThreadPoolExecutor")
    async def test_search_code_success(self, mock_executor, coordinator):
        """Test successful code search."""
        # Mock the executor and future results
        mock_future = Mock()
        mock_future.result.return_value = CrossRepoSearchResult(
            repository_id="repo1",
            repository_name="Test Repo 1",
            results=[
                {
                    "content": "def test_code():",
                    "file_path": "/test/repo1/file1.py",
                    "line_number": 5,
                    "score": 0.9,
                }
            ],
            search_time=0.1,
        )

        mock_executor_instance = Mock()
        mock_executor_instance.submit.return_value = mock_future
        mock_executor_instance.__enter__ = Mock(return_value=mock_executor_instance)
        mock_executor_instance.__exit__ = Mock(return_value=None)
        mock_executor.return_value = mock_executor_instance

        # Mock as_completed
        with patch(
            "mcp_server.dispatcher.cross_repo_coordinator.as_completed"
        ) as mock_as_completed:
            mock_as_completed.return_value = [mock_future]

            result = await coordinator.search_code("test_code", semantic=True, limit=10)

            assert result.total_results == 1
            assert result.repositories_searched == 1
            assert result.query == "test_code"
            assert len(result.results) == 1

    @pytest.mark.asyncio
    async def test_aggregate_symbol_results_deduplication(self, coordinator):
        """Test symbol result aggregation with deduplication."""
        search_results = [
            CrossRepoSearchResult(
                repository_id="repo1",
                repository_name="Test Repo 1",
                results=[
                    {
                        "symbol": "test_function",
                        "file_path": "/repo1/utils/helper.py",
                        "line_number": 10,
                    },
                    {"symbol": "other_function", "file_path": "/repo1/main.py", "line_number": 20},
                ],
                search_time=0.1,
            ),
            CrossRepoSearchResult(
                repository_id="repo2",
                repository_name="Test Repo 2",
                results=[
                    {
                        "symbol": "test_function",
                        "file_path": "/repo2/utils/helper.py",  # Same relative path
                        "line_number": 10,  # Same line number
                    }
                ],
                search_time=0.2,
            ),
        ]

        result = await coordinator._aggregate_symbol_results("test_function", search_results, 0.0)

        assert result.total_results >= 2  # Should keep different repo results
        assert result.repositories_searched == 2
        assert result.deduplication_stats["original_count"] == 3
        # The exact deduplication behavior depends on the signature creation
        assert "duplicates_removed" in result.deduplication_stats

    @pytest.mark.asyncio
    async def test_aggregate_code_results_with_limit(self, coordinator):
        """Test code result aggregation with limit."""
        search_results = [
            CrossRepoSearchResult(
                repository_id="repo1",
                repository_name="Test Repo 1",
                results=[
                    {"content": "result1", "file_path": "/file1.py", "score": 0.9},
                    {"content": "result2", "file_path": "/file2.py", "score": 0.8},
                    {"content": "result3", "file_path": "/file3.py", "score": 0.7},
                ],
                search_time=0.1,
            )
        ]

        result = await coordinator._aggregate_code_results(
            "test_query", search_results, 0.0, limit=2
        )

        assert result.total_results == 2  # Limited to 2 results
        assert result.results[0]["score"] >= result.results[1]["score"]  # Sorted by score

    def test_create_symbol_signature(self, coordinator):
        """Test symbol signature creation for deduplication."""
        result1 = {
            "symbol": "test_function",
            "file_path": "/repo1/src/utils/helper.py",
            "line_number": 10,
        }

        result2 = {
            "symbol": "test_function",
            "file_path": "/repo2/src/utils/helper.py",
            "line_number": 10,
        }

        sig1 = coordinator._create_symbol_signature(result1)
        sig2 = coordinator._create_symbol_signature(result2)

        # Should be the same (same symbol, same relative path, same line)
        assert sig1 == sig2
        assert isinstance(sig1, str)
        assert len(sig1) == 32  # MD5 hash length

    def test_create_content_hash(self, coordinator):
        """Test content hash creation for deduplication."""
        result1 = {"content": "def test_function():\n    pass", "file_path": "/repo1/src/helper.py"}

        result2 = {"content": "def test_function():\n    pass", "file_path": "/repo2/src/helper.py"}

        hash1 = coordinator._create_content_hash(result1)
        hash2 = coordinator._create_content_hash(result2)

        # Should be the same (same content, same relative path)
        assert hash1 == hash2
        assert isinstance(hash1, str)
        assert len(hash1) == 32  # MD5 hash length

    @pytest.mark.asyncio
    async def test_get_search_statistics(self, coordinator):
        """Test getting search statistics."""
        stats = await coordinator.get_search_statistics()

        assert "total_repositories" in stats
        assert "total_files" in stats
        assert "total_symbols" in stats
        assert "languages" in stats
        assert "repository_details" in stats

        assert stats["total_repositories"] == 2
        assert stats["total_files"] == 26  # 15 + 11
        assert stats["total_symbols"] == 180  # 100 + 80
        assert "python" in stats["languages"]
        assert "javascript" in stats["languages"]
        assert "java" in stats["languages"]

    @patch("mcp_server.dispatcher.cross_repo_coordinator.SQLiteStore")
    def test_search_symbol_in_repository(self, mock_store_class, coordinator):
        """Test searching symbol in a single repository."""
        # Mock SQLiteStore
        mock_store = Mock()
        mock_store.search_symbols.return_value = [
            {"symbol": "test_func", "file_path": "/test.py", "line_number": 10}
        ]
        mock_store_class.return_value = mock_store
        coordinator.multi_repo_manager._search_repository.return_value = CrossRepoSearchResult(
            "test_repo", "Test Repo", mock_store.search_symbols.return_value, 0.0
        )

        repo = RepositoryInfo(
            repository_id="test_repo",
            name="Test Repo",
            path=Path("/test"),
            index_path=Path("/test/.index/test.db"),
            language_stats={"python": 5},
            total_files=5,
            total_symbols=50,
            indexed_at=datetime.now(),
        )

        scope = SearchScope()
        result = coordinator._search_symbol_in_repository("test_func", repo, scope)

        assert result is not None
        assert result.repository_id == "test_repo"
        assert result.repository_name == "Test Repo"
        assert len(result.results) == 1
        assert result.error is None

    @patch("mcp_server.dispatcher.cross_repo_coordinator.SQLiteStore")
    def test_search_code_in_repository_with_filters(self, mock_store_class, coordinator):
        """Test searching code in repository with file type filters."""
        # Mock SQLiteStore
        mock_store = Mock()
        mock_store.search_content.return_value = [
            {"content": "test code", "file_path": "/test.py"},
            {"content": "test code", "file_path": "/test.js"},
            {"content": "test code", "file_path": "/test.txt"},
        ]
        mock_store_class.return_value = mock_store
        coordinator.multi_repo_manager._query_ready.return_value = True
        coordinator.multi_repo_manager._search_code_in_repository.side_effect = [
            CrossRepoSearchResult("test_repo", "Test Repo", [row], 0.0)
            for row in mock_store.search_content.return_value[:2]
        ]

        repo = RepositoryInfo(
            repository_id="test_repo",
            name="Test Repo",
            path=Path("/test"),
            index_path=Path("/test/.index/test.db"),
            language_stats={"python": 5},
            total_files=5,
            total_symbols=50,
            indexed_at=datetime.now(),
        )

        scope = SearchScope(file_types=[".py", ".js"])
        result = coordinator._search_code_in_repository(
            "test code", repo, scope, semantic=False, limit=10
        )

        assert result is not None
        assert len(result.results) == 2  # Only .py and .js files
        assert all(r["file_path"].endswith((".py", ".js")) for r in result.results)


class TestSearchScope:
    """Test cases for SearchScope dataclass."""

    def test_search_scope_defaults(self):
        """Test SearchScope default values."""
        scope = SearchScope()

        assert scope.repositories is None
        assert scope.languages is None
        assert scope.file_types is None
        assert scope.max_repositories == 10
        assert scope.priority_order is True

    def test_search_scope_custom_values(self):
        """Test SearchScope with custom values."""
        scope = SearchScope(
            repositories=["repo1"],
            languages=["python"],
            file_types=[".py"],
            max_repositories=5,
            priority_order=False,
        )

        assert scope.repositories == ["repo1"]
        assert scope.languages == ["python"]
        assert scope.file_types == [".py"]
        assert scope.max_repositories == 5
        assert scope.priority_order is False


class TestAggregatedResult:
    """Test cases for AggregatedResult dataclass."""

    def test_aggregated_result_creation(self):
        """Test AggregatedResult creation."""
        result = AggregatedResult(
            query="test_query",
            total_results=10,
            repositories_searched=2,
            search_time=0.5,
            results=[{"test": "data"}],
            repository_stats={"repo1": 6, "repo2": 4},
            deduplication_stats={"original_count": 12, "duplicates_removed": 2},
        )

        assert result.query == "test_query"
        assert result.total_results == 10
        assert result.repositories_searched == 2
        assert result.search_time == 0.5
        assert len(result.results) == 1
        assert result.repository_stats["repo1"] == 6
        assert result.deduplication_stats["duplicates_removed"] == 2


class TestSingletonFunction:
    """Test the singleton function."""

    def test_get_cross_repo_coordinator_singleton(self):
        """Test that get_cross_repo_coordinator returns singleton."""
        coord1 = get_cross_repo_coordinator()
        coord2 = get_cross_repo_coordinator()

        assert coord1 is coord2
        assert isinstance(coord1, CrossRepositorySearchCoordinator)

    def test_coordinator_properties(self):
        """Test coordinator has expected properties."""
        coord = get_cross_repo_coordinator()

        assert hasattr(coord, "multi_repo_manager")
        assert hasattr(coord, "max_workers")
        assert hasattr(coord, "default_result_limit")
        assert hasattr(coord, "memory_manager")
        assert hasattr(coord, "plugin_loader")


if __name__ == "__main__":
    pytest.main([__file__])
