"""
Comprehensive test suite for verifying metadata preservation through the reranking process.

This test suite ensures that all metadata (file paths, line numbers, columns, snippets,
match types, and context) is preserved correctly during reranking operations.
"""

import os

# Import the reranker module and its classes
import sys
import types
from contextlib import contextmanager
from typing import List
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_server.indexer.reranker import (
    BaseReranker,
    CohereReranker,
    HybridReranker,
    LocalCrossEncoderReranker,
    RerankerFactory,
    RerankItem,
    RerankResult,
    Result,
    SearchResult,
    TFIDFReranker,
)


@contextmanager
def _stub_module(module_name: str, **attrs):
    """Temporarily inject a stub module into sys.modules so patch() can target it."""
    was_present = module_name in sys.modules
    original = sys.modules.get(module_name)
    if not was_present:
        stub = types.ModuleType(module_name)
        for attr_name, attr_val in attrs.items():
            setattr(stub, attr_name, attr_val)
        sys.modules[module_name] = stub
    try:
        yield
    finally:
        if not was_present:
            del sys.modules[module_name]
        else:
            sys.modules[module_name] = original


class TestDataFactory:
    """Factory for creating test data with various scenarios"""

    @staticmethod
    def create_search_results(count: int = 5) -> List[SearchResult]:
        """Create a list of diverse search results with full metadata"""
        results = []
        for i in range(count):
            result = SearchResult(
                file_path=f"/path/to/file_{i}.py",
                start_line=100 + i * 10,
                end_line=100 + i * 10,
                column=5 + i,
                snippet=f"def function_{i}():\n    '''Function {i} documentation'''\n    return {i}",
                match_type=["exact", "fuzzy", "semantic"][i % 3],
                score=0.9 - (i * 0.1),
                context=f"This is the surrounding context for function_{i}",
            )
            results.append(result)
        return results

    @staticmethod
    def create_edge_case_results() -> List[SearchResult]:
        """Create search results with edge cases"""
        return [
            # Unicode file path and content
            SearchResult(
                file_path="/path/to/αβγ_unicode.py",
                start_line=42,
                end_line=42,
                column=0,
                snippet="def 你好():\n    return '世界'",
                match_type="exact",
                score=0.95,
                context="Unicode context with emojis 🎉",
            ),
            # Very long file path
            SearchResult(
                file_path="/very/long/path/that/goes/on/and/on/and/on/deeply/nested/in/directories/file.py",
                start_line=99999,
                end_line=99999,
                column=500,
                snippet="x" * 1000,  # Very long snippet
                match_type="fuzzy",
                score=0.85,
                context="y" * 2000,  # Very long context
            ),
            # Special characters in path
            SearchResult(
                file_path="/path/with spaces/and-special@chars!/file#1.py",
                start_line=1,
                end_line=1,
                column=1,
                snippet="# Special\n\tcharacters\n\there",
                match_type="semantic",
                score=0.75,
                context=None,  # No context
            ),
            # Zero values
            SearchResult(
                file_path="/zero/values.py",
                start_line=0,
                end_line=0,
                column=0,
                snippet="",
                match_type="exact",
                score=0.0,
                context="",
            ),
        ]


class TestRerankItemDataclass:
    """Test the RerankItem dataclass"""

    def test_rerank_item_creation(self):
        """Test creating RerankItem with all fields"""
        original = SearchResult(
            file_path="/test.py",
            start_line=10,
            end_line=10,
            column=5,
            snippet="test code",
            match_type="exact",
            score=0.8,
            context="test context",
        )

        item = RerankItem(
            original_result=original,
            rerank_score=0.95,
            original_rank=2,
            new_rank=0,
            explanation="High relevance due to exact match",
        )

        assert item.original_result == original
        assert item.rerank_score == 0.95
        assert item.original_rank == 2
        assert item.new_rank == 0
        assert item.explanation == "High relevance due to exact match"

    def test_rerank_item_preserves_original(self):
        """Test that RerankItem preserves complete original SearchResult"""
        original = TestDataFactory.create_search_results(1)[0]
        item = RerankItem(original_result=original, rerank_score=0.99, original_rank=0, new_rank=0)

        # Verify all original fields are preserved
        assert item.original_result.file_path == original.file_path
        assert item.original_result.line == original.line
        assert item.original_result.column == original.column
        assert item.original_result.snippet == original.snippet
        assert item.original_result.match_type == original.match_type
        assert item.original_result.score == original.score
        assert item.original_result.context == original.context


class TestRerankResultDataclass:
    """Test the RerankResult dataclass"""

    def test_rerank_result_creation(self):
        """Test creating RerankResult with items and metadata"""
        results = TestDataFactory.create_search_results(3)
        items = [
            RerankItem(
                original_result=result,
                rerank_score=0.9 - i * 0.1,
                original_rank=i,
                new_rank=i,
            )
            for i, result in enumerate(results)
        ]

        metadata = {
            "reranker": "test",
            "model": "test-model",
            "total_results": 3,
            "returned_results": 3,
        }

        rerank_result = RerankResult(results=items, metadata=metadata)

        assert len(rerank_result.results) == 3
        assert rerank_result.metadata == metadata
        assert all(isinstance(item, RerankItem) for item in rerank_result.results)


class TestTFIDFReranker:
    """Test TF-IDF reranker metadata preservation"""

    @pytest.fixture
    def tfidf_reranker(self):
        """Create TF-IDF reranker instance"""
        return TFIDFReranker({"max_features": 1000})

    @pytest.mark.asyncio
    async def test_tfidf_preserves_metadata(self, tfidf_reranker):
        """Test that TF-IDF reranker preserves all metadata"""
        # Initialize reranker
        init_result = await tfidf_reranker.initialize({})
        if not init_result.is_success:
            pytest.skip("scikit-learn not installed")

        # Create test data
        query = "test query"
        original_results = TestDataFactory.create_search_results(5)

        # Perform reranking
        result = await tfidf_reranker.rerank(query, original_results, top_k=3)
        assert result.is_success

        rerank_result = result.data
        assert isinstance(rerank_result, RerankResult)
        assert len(rerank_result.results) == 3

        # Verify metadata preservation
        for item in rerank_result.results:
            assert isinstance(item, RerankItem)
            original = item.original_result

            # Find corresponding original result
            original_match = original_results[item.original_rank]

            # Verify all fields are preserved
            assert original.file_path == original_match.file_path
            assert original.line == original_match.line
            assert original.column == original_match.column
            assert original.snippet == original_match.snippet
            assert original.match_type == original_match.match_type
            assert original.score == original_match.score
            assert original.context == original_match.context

            # Verify only rerank_score is new
            assert isinstance(item.rerank_score, float)
            assert 0 <= item.rerank_score <= 1

    @pytest.mark.asyncio
    async def test_tfidf_edge_cases(self, tfidf_reranker):
        """Test TF-IDF reranker with edge case data"""
        # Initialize reranker
        init_result = await tfidf_reranker.initialize({})
        if not init_result.is_success:
            pytest.skip("scikit-learn not installed")

        # Test with edge case results
        query = "edge case query"
        edge_results = TestDataFactory.create_edge_case_results()

        result = await tfidf_reranker.rerank(query, edge_results)
        assert result.is_success

        rerank_result = result.data
        for item in rerank_result.results:
            original = item.original_result
            original_match = edge_results[item.original_rank]

            # Verify edge case data is preserved
            assert original.file_path == original_match.file_path
            assert original.snippet == original_match.snippet
            assert original.context == original_match.context


class TestCohereReranker:
    """Test Cohere reranker metadata preservation"""

    @pytest.fixture
    def cohere_reranker(self):
        """Create Cohere reranker instance"""
        return CohereReranker({"cohere_api_key": "test-key", "model": "rerank-english-v2.0"})

    @pytest.mark.asyncio
    async def test_cohere_preserves_metadata(self, cohere_reranker):
        """Test that Cohere reranker preserves all metadata"""
        # Mock Cohere client
        mock_client = Mock()
        mock_response = Mock()
        mock_response.results = [
            Mock(index=2, relevance_score=0.95),
            Mock(index=0, relevance_score=0.90),
            Mock(index=1, relevance_score=0.85),
        ]
        mock_client.rerank.return_value = mock_response

        with (
            _stub_module("cohere", Client=Mock()),
            patch("cohere.Client", return_value=mock_client),
        ):
            # Initialize reranker
            init_result = await cohere_reranker.initialize({})
            if not init_result.is_success:
                pytest.skip("Cohere library not available")

            # Create test data
            query = "test query"
            original_results = TestDataFactory.create_search_results(3)

            # Perform reranking
            result = await cohere_reranker.rerank(query, original_results)
            assert result.is_success

            rerank_result = result.data
            assert isinstance(rerank_result, RerankResult)

            # Verify reordering and metadata preservation
            assert len(rerank_result.results) == 3

            # First result should be original index 2
            first_item = rerank_result.results[0]
            assert first_item.original_rank == 2
            assert first_item.new_rank == 0
            assert first_item.rerank_score == 0.95

            # Verify all original metadata is preserved
            original = first_item.original_result
            original_match = original_results[2]
            assert original.file_path == original_match.file_path
            assert original.line == original_match.line
            assert original.column == original_match.column
            assert original.snippet == original_match.snippet
            assert original.match_type == original_match.match_type
            assert original.score == original_match.score
            assert original.context == original_match.context

    @pytest.mark.asyncio
    @pytest.mark.parametrize("generation_key", [None, "repo:generation:profile"])
    async def test_cohere_caching_preserves_metadata(self, cohere_reranker, generation_key):
        """Test that cached results preserve metadata"""
        # Mock Cohere client
        mock_client = Mock()
        mock_response = Mock()
        mock_response.results = [Mock(index=1, relevance_score=0.99)]
        mock_client.rerank.return_value = mock_response

        with (
            _stub_module("cohere", Client=Mock()),
            patch("cohere.Client", return_value=mock_client),
        ):
            init_result = await cohere_reranker.initialize({})
            if not init_result.is_success:
                pytest.skip("Cohere library not available")

            query = "cached query"
            original_results = TestDataFactory.create_search_results(2)
            for candidate in original_results:
                candidate.generation_key = generation_key

            # Only generation-bound candidates are eligible for reuse.
            result1 = await cohere_reranker.rerank(query, original_results, top_k=1)
            assert result1.is_success
            assert not result1.data.metadata["from_cache"]

            result2 = await cohere_reranker.rerank(query, original_results, top_k=1)
            assert result2.is_success
            assert result2.data.metadata["from_cache"] is (generation_key is not None)
            assert mock_client.rerank.call_count == (1 if generation_key else 2)

            # Verify cached results preserve metadata
            cached_item = result2.data.results[0]
            original_item = result1.data.results[0]

            assert cached_item.original_result.file_path == original_item.original_result.file_path
            assert cached_item.original_result.line == original_item.original_result.line
            assert cached_item.original_result.column == original_item.original_result.column
            assert cached_item.original_result.snippet == original_item.original_result.snippet
            assert (
                cached_item.original_result.match_type == original_item.original_result.match_type
            )
            assert cached_item.original_result.score == original_item.original_result.score
            assert cached_item.original_result.context == original_item.original_result.context
            assert cached_item.rerank_score == original_item.rerank_score
            assert cached_item.original_result.generation_key == generation_key


class TestCrossEncoderReranker:
    """Test Cross-Encoder reranker metadata preservation"""

    @pytest.fixture
    def cross_encoder_reranker(self):
        """Create Cross-Encoder reranker instance"""
        return LocalCrossEncoderReranker(
            {"model": "cross-encoder/ms-marco-MiniLM-L-6-v2", "device": "cpu"}
        )

    @pytest.mark.asyncio
    async def test_cross_encoder_preserves_metadata(self, cross_encoder_reranker):
        """Test that Cross-Encoder reranker preserves all metadata"""
        # Mock CrossEncoder
        mock_model = Mock()
        mock_model.predict.return_value = np.array([0.2, 0.9, 0.5, 0.7, 0.3])

        with (
            _stub_module("sentence_transformers", CrossEncoder=Mock()),
            patch("sentence_transformers.CrossEncoder", return_value=mock_model),
        ):
            # Initialize reranker
            init_result = await cross_encoder_reranker.initialize({})
            if not init_result.is_success:
                pytest.skip("sentence-transformers not available")

            # Create test data
            query = "test query"
            original_results = TestDataFactory.create_search_results(5)

            # Perform reranking
            result = await cross_encoder_reranker.rerank(query, original_results, top_k=3)
            assert result.is_success

            rerank_result = result.data
            assert len(rerank_result.results) == 3

            # Verify results are reordered by score
            scores = [item.rerank_score for item in rerank_result.results]
            assert scores == sorted(scores, reverse=True)

            # Verify metadata preservation
            for item in rerank_result.results:
                original = item.original_result
                original_match = original_results[item.original_rank]

                assert original.file_path == original_match.file_path
                assert original.line == original_match.line
                assert original.column == original_match.column
                assert original.snippet == original_match.snippet
                assert original.match_type == original_match.match_type
                assert original.score == original_match.score
                assert original.context == original_match.context

    @pytest.mark.asyncio
    async def test_cross_encoder_empty_results(self, cross_encoder_reranker):
        """Test Cross-Encoder with empty results"""
        mock_model = Mock()

        with (
            _stub_module("sentence_transformers", CrossEncoder=Mock()),
            patch("sentence_transformers.CrossEncoder", return_value=mock_model),
        ):
            init_result = await cross_encoder_reranker.initialize({})
            if not init_result.is_success:
                pytest.skip("sentence-transformers not available")

            result = await cross_encoder_reranker.rerank("query", [])
            assert result.is_success
            assert len(result.data.results) == 0


class TestHybridReranker:
    """Test Hybrid reranker metadata preservation"""

    @pytest.fixture
    def hybrid_reranker(self):
        """Create Hybrid reranker with mocked components"""
        config = {"weight_primary": 0.7, "weight_fallback": 0.3}
        return HybridReranker(config)

    @pytest.mark.asyncio
    async def test_hybrid_primary_success(self, hybrid_reranker):
        """Test hybrid reranker when primary succeeds"""
        # Create mock primary reranker
        primary = AsyncMock(spec=BaseReranker)
        primary.initialize = AsyncMock(return_value=Result.ok())

        # Create expected rerank result
        original_results = TestDataFactory.create_search_results(3)
        reranked_items = [
            RerankItem(
                original_result=original_results[2],
                rerank_score=0.95,
                original_rank=2,
                new_rank=0,
            ),
            RerankItem(
                original_result=original_results[0],
                rerank_score=0.85,
                original_rank=0,
                new_rank=1,
            ),
            RerankItem(
                original_result=original_results[1],
                rerank_score=0.75,
                original_rank=1,
                new_rank=2,
            ),
        ]

        primary.rerank = AsyncMock(
            return_value=Result.ok(
                RerankResult(
                    results=reranked_items,
                    metadata={"reranker": "primary", "model": "test"},
                )
            )
        )

        # Set up hybrid reranker
        hybrid_reranker.set_rerankers(primary, None)
        await hybrid_reranker.initialize({})

        # Perform reranking
        result = await hybrid_reranker.rerank("query", original_results)
        assert result.is_success

        # Verify metadata
        rerank_result = result.data
        assert rerank_result.metadata["hybrid"] is True
        assert rerank_result.metadata["primary_succeeded"] is True

        # Verify all items preserve metadata
        for item in rerank_result.results:
            assert item in reranked_items
            # Verify original data is preserved
            assert item.original_result in original_results

    @pytest.mark.asyncio
    async def test_hybrid_fallback_on_failure(self, hybrid_reranker):
        """Test hybrid reranker falls back when primary fails"""
        # Create mock rerankers
        primary = AsyncMock(spec=BaseReranker)
        primary.initialize = AsyncMock(return_value=Result.ok())
        primary.rerank = AsyncMock(return_value=Result.error("Primary failed"))
        primary.get_capabilities = Mock(return_value={"name": "primary"})

        fallback = AsyncMock(spec=BaseReranker)
        fallback.initialize = AsyncMock(return_value=Result.ok())

        # Create fallback result
        original_results = TestDataFactory.create_search_results(2)
        fallback_items = [
            RerankItem(
                original_result=result,
                rerank_score=0.8 - i * 0.1,
                original_rank=i,
                new_rank=i,
            )
            for i, result in enumerate(original_results)
        ]

        fallback.rerank = AsyncMock(
            return_value=Result.ok(
                RerankResult(results=fallback_items, metadata={"reranker": "fallback"})
            )
        )
        fallback.get_capabilities = Mock(return_value={"name": "fallback"})

        # Set up hybrid reranker
        hybrid_reranker.set_rerankers(primary, fallback)
        await hybrid_reranker.initialize({})

        # Perform reranking
        result = await hybrid_reranker.rerank("query", original_results)
        assert result.is_success

        # Verify fallback was used
        rerank_result = result.data
        assert rerank_result.metadata["hybrid"] is True
        assert rerank_result.metadata["primary_succeeded"] is False
        assert "Primary failed" in rerank_result.metadata["fallback_reason"]

        # Verify metadata preservation
        for item in rerank_result.results:
            assert item.original_result in original_results


class TestRerankerFactory:
    """Test reranker factory functionality"""

    def test_factory_creates_all_types(self):
        """Test factory can create all reranker types"""
        factory = RerankerFactory()

        # Test creating each type
        tfidf = factory.create_reranker("tfidf", {})
        assert isinstance(tfidf, TFIDFReranker)

        cohere = factory.create_reranker("cohere", {"cohere_api_key": "test"})
        assert isinstance(cohere, CohereReranker)

        cross_encoder = factory.create_reranker("cross-encoder", {})
        assert isinstance(cross_encoder, LocalCrossEncoderReranker)

        hybrid = factory.create_reranker("hybrid", {})
        assert isinstance(hybrid, HybridReranker)

    def test_factory_hybrid_configuration(self):
        """Test factory configures hybrid reranker correctly"""
        factory = RerankerFactory()

        config = {
            "primary_type": "tfidf",
            "fallback_type": "cross-encoder",
            "weight_primary": 0.8,
            "weight_fallback": 0.2,
        }

        hybrid = factory.create_reranker("hybrid", config)
        assert isinstance(hybrid, HybridReranker)
        assert hybrid.primary_reranker is not None
        assert hybrid.fallback_reranker is not None
        assert isinstance(hybrid.primary_reranker, TFIDFReranker)
        assert isinstance(hybrid.fallback_reranker, LocalCrossEncoderReranker)


class TestEdgeCasesAndErrors:
    """Test edge cases and error scenarios"""

    @pytest.mark.asyncio
    async def test_reranking_with_none_context(self):
        """Test reranking results with None context"""
        results = [
            SearchResult(
                file_path="/test.py",
                start_line=1,
                end_line=1,
                column=0,
                snippet="code",
                match_type="exact",
                score=0.9,
                context=None,  # No context
            )
        ]

        reranker = TFIDFReranker({})
        init_result = await reranker.initialize({})
        if not init_result.is_success:
            pytest.skip("scikit-learn not available")

        result = await reranker.rerank("query", results)
        assert result.is_success

        # Verify None context is preserved
        item = result.data.results[0]
        assert item.original_result.context is None

    @pytest.mark.asyncio
    async def test_reranking_preserves_zero_scores(self):
        """Test that zero scores are preserved"""
        results = [
            SearchResult(
                file_path="/zero.py",
                start_line=0,
                end_line=0,
                column=0,
                snippet="",
                match_type="exact",
                score=0.0,  # Zero score
                context="",
            )
        ]

        reranker = TFIDFReranker({})
        init_result = await reranker.initialize({})
        if not init_result.is_success:
            pytest.skip("scikit-learn not available")

        result = await reranker.rerank("query", results)
        assert result.is_success

        item = result.data.results[0]
        assert item.original_result.score == 0.0
        assert item.original_result.line == 0
        assert item.original_result.column == 0

    @pytest.mark.asyncio
    async def test_reranking_large_dataset(self):
        """Test reranking with a large number of results"""
        # Create 1000 results
        large_results = []
        for i in range(1000):
            result = SearchResult(
                file_path=f"/large/file_{i}.py",
                start_line=i,
                end_line=i,
                column=i % 100,
                snippet=f"snippet {i}",
                match_type=["exact", "fuzzy", "semantic"][i % 3],
                score=1.0 - (i / 1000),
                context=f"context {i}",
            )
            large_results.append(result)

        reranker = TFIDFReranker({"max_features": 5000})
        init_result = await reranker.initialize({})
        if not init_result.is_success:
            pytest.skip("scikit-learn not available")

        # Rerank and get top 10
        result = await reranker.rerank("large query", large_results, top_k=10)
        assert result.is_success
        assert len(result.data.results) == 10

        # Verify metadata is preserved for all results
        for item in result.data.results:
            original_idx = item.original_rank
            assert item.original_result.file_path == f"/large/file_{original_idx}.py"
            assert item.original_result.line == original_idx

    @pytest.mark.asyncio
    async def test_reranking_special_characters(self):
        """Test reranking with special characters in all fields"""
        special_results = [
            SearchResult(
                file_path="/path/with\nnewlines\tand\ttabs.py",
                start_line=42,
                end_line=42,
                column=13,
                snippet="code with\nnewlines\tand\ttabs",
                match_type="exact",
                score=0.9,
                context="context\nwith\nspecial\tcharacters\r\n",
            ),
            SearchResult(
                file_path="/path/with/♠♣♥♦/cards.py",
                start_line=100,
                end_line=100,
                column=50,
                snippet="♠♣♥♦ unicode cards",
                match_type="fuzzy",
                score=0.8,
                context="Full deck: ♠♣♥♦",
            ),
        ]

        reranker = TFIDFReranker({})
        init_result = await reranker.initialize({})
        if not init_result.is_success:
            pytest.skip("scikit-learn not available")

        result = await reranker.rerank("special query", special_results)
        assert result.is_success

        # Verify special characters are preserved
        for i, item in enumerate(result.data.results):
            original_idx = item.original_rank
            original = special_results[original_idx]
            assert item.original_result.file_path == original.file_path
            assert item.original_result.snippet == original.snippet
            assert item.original_result.context == original.context


class TestMetadataConsistency:
    """Test metadata consistency across different scenarios"""

    @pytest.mark.asyncio
    async def test_multiple_reranking_preserves_original(self):
        """Test that multiple reranking operations preserve original metadata"""
        original_results = TestDataFactory.create_search_results(5)

        # Create reranker
        reranker = TFIDFReranker({})
        init_result = await reranker.initialize({})
        if not init_result.is_success:
            pytest.skip("scikit-learn not available")

        # First reranking
        result1 = await reranker.rerank("query 1", original_results)
        assert result1.is_success

        # Extract SearchResults from RerankItems for second reranking
        # This simulates what would happen in a real pipeline
        intermediate_results = [item.original_result for item in result1.data.results]

        # Second reranking with different query
        result2 = await reranker.rerank("query 2", intermediate_results)
        assert result2.is_success

        # Verify original metadata is still intact
        for item in result2.data.results:
            # Find the original result
            original_found = False
            for original in original_results:
                if (
                    original.file_path == item.original_result.file_path
                    and original.line == item.original_result.line
                ):
                    original_found = True
                    # Verify all fields match
                    assert item.original_result.column == original.column
                    assert item.original_result.snippet == original.snippet
                    assert item.original_result.match_type == original.match_type
                    assert item.original_result.score == original.score
                    assert item.original_result.context == original.context
                    break
            assert original_found, f"Original result not found for {item.original_result.file_path}"

    @pytest.mark.asyncio
    async def test_reranker_result_immutability(self):
        """Test that reranking doesn't modify original results"""
        original_results = TestDataFactory.create_search_results(3)

        # Create deep copies to verify immutability
        original_copies = []
        for result in original_results:
            copy = SearchResult(
                file_path=result.file_path,
                start_line=result.line,
                end_line=result.line,
                column=result.column,
                snippet=result.snippet,
                match_type=result.match_type,
                score=result.score,
                context=result.context,
            )
            original_copies.append(copy)

        # Perform reranking
        reranker = TFIDFReranker({})
        await reranker.initialize({})
        await reranker.rerank("test query", original_results)

        # Verify original results are unchanged
        for original, copy in zip(original_results, original_copies):
            assert original.file_path == copy.file_path
            assert original.line == copy.line
            assert original.column == copy.column
            assert original.snippet == copy.snippet
            assert original.match_type == copy.match_type
            assert original.score == copy.score
            assert original.context == copy.context


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "-s"])
