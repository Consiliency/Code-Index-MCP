"""Tests for SemanticIndexerRegistry — SL-4.1."""

from __future__ import annotations

import importlib
import re
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COLLECTION_PATTERN = re.compile(r"^ci__[0-9a-f]{12}__.+__.+$")


def _make_registry_with_repos(tmp_path: Path):
    """Return a RepositoryRegistry pre-populated with two fake repos."""
    from mcp_server.storage.multi_repo_manager import RepositoryInfo
    from mcp_server.storage.repository_registry import RepositoryRegistry

    reg_file = tmp_path / "registry.json"
    repo_reg = RepositoryRegistry(registry_path=reg_file)

    for repo_id, name, branch, commit in [
        ("repo-a", "alpha", "main", "aabbccdd1234"),
        ("repo-b", "beta", "main", "11223344aabb"),
    ]:
        fake_path = tmp_path / name
        fake_path.mkdir()
        info = RepositoryInfo(
            repository_id=repo_id,
            name=name,
            path=fake_path,
            index_path=fake_path / ".mcp-index" / "current.db",
            language_stats={},
            total_files=0,
            total_symbols=0,
            indexed_at=datetime.now(),
            current_commit=commit,
            tracked_branch=branch,
        )
        repo_reg.register(info)

    return repo_reg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_qdrant(monkeypatch):
    """Patch QdrantClient so no server is needed."""
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = False
    mock_client.get_collection.side_effect = Exception("no collection")

    with patch("mcp_server.utils.semantic_indexer.QdrantClient", return_value=mock_client):
        yield mock_client


@pytest.fixture()
def mock_embedding_provider(monkeypatch):
    """Patch create_embedding_provider to avoid real HTTP calls."""
    mock_provider = MagicMock()
    mock_provider.provider_name = "mock"
    mock_provider.embed.return_value = [[0.1] * 1024]

    with patch(
        "mcp_server.utils.semantic_indexer.create_embedding_provider",
        return_value=mock_provider,
    ):
        yield mock_provider


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSemanticIndexerRegistry:
    def test_embedding_providers_module_imports_with_default_install_client_dependency(self):
        module = importlib.import_module("mcp_server.utils.embedding_providers")
        assert hasattr(module, "OpenAICompatibleEmbeddingProvider")

    def test_two_repos_distinct_collection_names(
        self, tmp_path, mock_qdrant, mock_embedding_provider
    ):
        """repo-a and repo-b must have different, well-formed collection names."""
        from mcp_server.utils.semantic_indexer_registry import SemanticIndexerRegistry

        repo_reg = _make_registry_with_repos(tmp_path)
        registry = SemanticIndexerRegistry(repository_registry=repo_reg)

        indexer_a = registry.get("repo-a")
        indexer_b = registry.get("repo-b")

        assert indexer_a.collection != indexer_b.collection
        assert COLLECTION_PATTERN.match(indexer_a.collection), indexer_a.collection
        assert COLLECTION_PATTERN.match(indexer_b.collection), indexer_b.collection

    def test_same_repo_id_returns_same_instance(
        self, tmp_path, mock_qdrant, mock_embedding_provider
    ):
        """Calling get() twice with the same repo_id returns the cached instance."""
        from mcp_server.utils.semantic_indexer_registry import SemanticIndexerRegistry

        repo_reg = _make_registry_with_repos(tmp_path)
        registry = SemanticIndexerRegistry(repository_registry=repo_reg)

        first = registry.get("repo-a")
        second = registry.get("repo-a")
        assert first is second

    def test_shutdown_closes_all_indexers(self, tmp_path, mock_qdrant, mock_embedding_provider):
        """shutdown() must close all cached indexers."""
        from mcp_server.utils.semantic_indexer_registry import SemanticIndexerRegistry

        repo_reg = _make_registry_with_repos(tmp_path)
        registry = SemanticIndexerRegistry(repository_registry=repo_reg)

        # Warm up both
        registry.get("repo-a")
        registry.get("repo-b")

        # Spy on qdrant close; SemanticIndexer delegates to self.qdrant.close()
        mock_qdrant.close = MagicMock()

        registry.shutdown()

        # One close per cached indexer (two repos)
        assert mock_qdrant.close.call_count == 2

    def test_repo_indexers_use_repo_scoped_qdrant_paths(
        self, tmp_path, mock_qdrant, mock_embedding_provider
    ):
        from mcp_server.utils.semantic_indexer_registry import SemanticIndexerRegistry

        repo_reg = _make_registry_with_repos(tmp_path)
        registry = SemanticIndexerRegistry(repository_registry=repo_reg)

        indexer_a = registry.get("repo-a")
        indexer_b = registry.get("repo-b")

        assert indexer_a.qdrant_path != ":memory:"
        assert indexer_b.qdrant_path != ":memory:"
        assert indexer_a.qdrant_path != indexer_b.qdrant_path
        assert str(tmp_path / "alpha" / ".mcp-index") in indexer_a.qdrant_path

    def test_evict_closes_target_only_and_rebuilds(
        self, tmp_path, mock_qdrant, mock_embedding_provider
    ):
        from mcp_server.utils.semantic_indexer_registry import SemanticIndexerRegistry

        repo_reg = _make_registry_with_repos(tmp_path)
        registry = SemanticIndexerRegistry(repository_registry=repo_reg)
        indexer_a = registry.get("repo-a")
        indexer_b = registry.get("repo-b")
        mock_qdrant.close = MagicMock()

        assert registry.evict("repo-a") is True
        rebuilt_a = registry.get("repo-a")

        assert mock_qdrant.close.call_count == 1
        assert rebuilt_a is not indexer_a
        assert registry.get("repo-b") is indexer_b

    def test_get_unknown_repo_raises(self, tmp_path, mock_qdrant, mock_embedding_provider):
        """get() for an unregistered repo_id should raise KeyError."""
        from mcp_server.utils.semantic_indexer_registry import SemanticIndexerRegistry

        repo_reg = _make_registry_with_repos(tmp_path)
        registry = SemanticIndexerRegistry(repository_registry=repo_reg)

        with pytest.raises(KeyError):
            registry.get("no-such-repo")

    @pytest.mark.parametrize("change", ["generation", "profile", "unregister"])
    def test_cached_indexer_rejects_changed_binding_without_closing_borrower(
        self, tmp_path, mock_qdrant, mock_embedding_provider, change
    ):
        from mcp_server.utils.semantic_indexer_registry import SemanticIndexerRegistry

        repo_reg = _make_registry_with_repos(tmp_path)
        registry = SemanticIndexerRegistry(repo_reg)
        held = registry.get("repo-a")
        info = repo_reg.get("repo-a")
        mock_qdrant.close.reset_mock()
        if change == "unregister":
            repo_reg.unregister("repo-a")
        else:
            repo_reg.publish_generation(
                "repo-a",
                generation="new" if change == "generation" else info.index_generation,
                index_path=info.index_path,
                commit=info.current_commit,
                branch="main",
                profile="changed" if change == "profile" else info.index_profile,
                expected_registration_id=info.registration_id,
                expected_generation=info.index_generation,
            )
        with pytest.raises((RuntimeError, KeyError)):
            registry.get("repo-a")
        assert held is registry._cache["repo-a"]
        mock_qdrant.close.assert_not_called()
        registry.shutdown()
