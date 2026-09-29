"""
Reranking Module for Search Result Optimization

This module provides implementations for reranking search results to improve relevance.
It supports both Cohere's reranking API and local cross-encoder models as fallback.

Canonical reranker interface (RERANKEND / IF-0-RERANKEND-1)
----------------------------------------------------------
The async :class:`IReranker` defined **in this module** is the single canonical
reranker interface for the codebase. The historical duplicate ABC in
``mcp_server.interfaces.indexing_interfaces`` is now a re-export of the symbols
defined here (see that module's reranking region) so there is exactly one source
of truth. New code should import :class:`IReranker` / :class:`IRerankerFactory`
/ :class:`RerankResult` / :class:`RerankItem` from here.

Return contract
~~~~~~~~~~~~~~~
Although the ABC signature is annotated ``-> RerankResult`` for brevity, the
**de-facto** canonical return convention — established by every concrete
implementation (Cohere/CrossEncoder/TFIDF/Hybrid) and by the live consumer
``hybrid_search.py`` which reads ``rerank_result.data`` — is
``Result[RerankResult]``: success carries the :class:`RerankResult` container in
``.data``; failure carries an error string. :class:`EndpointReranker` follows
this convention. Contract violations detected by ``validate_rerank_response``
(missing / duplicated / unknown candidate ids) are a *wire-contract* fault and
**propagate as** :class:`~mcp_server.interfaces.rerank_contracts.RerankContractError`;
they are deliberately NOT folded into a per-candidate ``FAILED`` outcome, which
is a separate provider-level partial-failure axis.

Sync/async bridge
~~~~~~~~~~~~~~~~~
The synchronous dispatcher path drives async rerankers through
:func:`run_coroutine_sync` (and the thin :class:`SyncRerankerAdapter`). That
helper NEVER calls ``asyncio.run`` inside an already-running event loop: when a
loop is running in the calling thread it runs the coroutine on a private loop in
a dedicated worker thread instead. R2 (consumer migration) drives this primitive.

Cross-provider scores are never assumed comparable — see
``mcp_server.interfaces.rerank_contracts`` for the full score-comparability note.
"""

import asyncio
import hashlib
import json
import logging
import os
import threading
import uuid

# Define interfaces inline for now
from abc import ABC, abstractmethod
from dataclasses import asdict
from dataclasses import dataclass as dc
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar


# Define SearchResult inline
@dc
class SearchResult:
    """Search result information"""

    file_path: str
    start_line: int
    end_line: int
    column: int
    snippet: str
    match_type: str  # exact, fuzzy, semantic
    score: float
    context: Optional[str] = None
    generation_key: Optional[str] = None

    @property
    def line(self) -> int:
        return self.start_line


# Define RerankItem to wrap original SearchResult
@dc
class RerankItem:
    """A single reranked item that preserves the complete original SearchResult"""

    original_result: SearchResult  # Complete original SearchResult
    rerank_score: float  # The reranking score
    original_rank: int  # Original position in results
    new_rank: int  # New position after reranking
    explanation: Optional[str] = None  # Optional explanation for the ranking


# Define RerankResult with proper structure
@dc
class RerankResult:
    """Result from reranking operation"""

    results: List[RerankItem]  # List of reranked items
    metadata: Dict[str, Any]  # Metadata about the reranking operation


# Define IReranker interface
class IReranker(ABC):
    @abstractmethod
    async def rerank(
        self, query: str, results: List[SearchResult], top_k: int = 10
    ) -> RerankResult:
        pass


# Define IRerankerFactory interface
class IRerankerFactory(ABC):
    @abstractmethod
    def create_reranker(self, config: Any) -> Optional[IReranker]:
        pass


class MCPError(Exception):
    """Base exception for MCP errors."""


# Simple Result class for this module
class Result:
    def __init__(self, success: bool, data=None, error=None):
        self.is_success = success
        self.data = data
        self.error = error

    @classmethod
    def ok(cls, data=None):
        return cls(True, data)

    @classmethod
    def error(cls, error):
        return cls(False, None, error)


logger = logging.getLogger(__name__)


class RerankOutcome(str, Enum):
    """Structured, truthful outcome vocabulary for a rerank attempt.

    Shared between :mod:`reranker` and the dispatcher so the two agree on a
    single set of states. A failed rerank, or one that returned the original
    ordering unchanged, must NEVER be reported as :attr:`SUCCEEDED`.
    """

    #: No reranker is configured on the dispatcher path.
    NOT_CONFIGURED = "not_configured"
    #: The reranker ran but produced no observable reordering (ambiguous —
    #: this is also the signature of a masked internal failure).
    ATTEMPTED = "attempted"
    #: The reranker ran and produced a genuinely reordered result.
    SUCCEEDED = "succeeded"
    #: The reranker raised; the caller kept the original ordering.
    FAILED = "failed"
    #: A configured fallback provider was used after the primary failed.
    FALLBACK_APPLIED = "fallback_applied"
    #: Reranking was deliberately skipped by policy (e.g. text rerankers on
    #: the pure-vector semantic path); carries the policy identity.
    SKIPPED_POLICY = "skipped_policy"


@dc
class RerankFallbackSignal:
    """Optional signal a reranker MAY publish to report fallback usage.

    A reranker sets this on itself (attribute ``last_fallback``) when it fails
    over from a primary provider to a fallback. The dispatcher reads it after a
    rerank call to build truthful diagnostics. Carries provider identities only
    — never document or query text.
    """

    failed_provider: str
    fallback_provider: str


@dc
class RerankDiagnostics:
    """Truthful, redaction-safe record of a single rerank attempt.

    Contains only ids, counts, provider names, timings, and error strings —
    never raw code, snippet, or document text.
    """

    outcome: RerankOutcome
    provider: Optional[str] = None
    failed_provider: Optional[str] = None
    fallback_provider: Optional[str] = None
    candidate_count: int = 0
    returned_count: int = 0
    reordered: bool = False
    duration_ms: Optional[float] = None
    error: Optional[str] = None
    policy: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "provider": self.provider,
            "failed_provider": self.failed_provider,
            "fallback_provider": self.fallback_provider,
            "candidate_count": self.candidate_count,
            "returned_count": self.returned_count,
            "reordered": self.reordered,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "policy": self.policy,
        }


class BaseReranker(IReranker, ABC):
    """Base class for all reranker implementations"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._cache: Dict[str, Any] = {}  # Simple in-memory cache
        self.cache_ttl = config.get("cache_ttl", 3600)  # 1 hour default
        self.initialized = False

    async def _get_cache_key(
        self, query: str, results: List[SearchResult], top_k: Optional[int] = None
    ) -> str:
        """Generate cache key for reranking results"""
        payload = json.dumps(
            {
                "query": query,
                "config": self.config,
                "top_k": top_k,
                "candidates": [asdict(r) for r in results],
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return f"rerank:{self.__class__.__name__}:{hashlib.sha256(payload.encode()).hexdigest()}"

    async def _get_cached_results(
        self, query: str, results: List[SearchResult], top_k: Optional[int] = None
    ) -> Optional[List[RerankItem]]:
        """Get cached reranking results if available"""
        if not results or any(result.generation_key is None for result in results):
            return None
        cache_key = await self._get_cache_key(query, results, top_k)

        if cache_key in self._cache:
            cached_data = self._cache[cache_key]
            # Check if cache is still valid (simple time-based check)
            import time

            if time.time() - cached_data["timestamp"] < self.cache_ttl:
                logger.debug("Cache hit for reranking query (chars=%d)", len(query))
                return cached_data["results"]
            else:
                # Cache expired
                del self._cache[cache_key]

        return None

    async def _cache_results(
        self,
        query: str,
        results: List[SearchResult],
        reranked: List[RerankItem],
        top_k: Optional[int] = None,
    ):
        """Cache reranking results"""
        import time

        if not results or any(result.generation_key is None for result in results):
            return
        cache_key = await self._get_cache_key(query, results, top_k)
        self._cache[cache_key] = {"results": reranked, "timestamp": time.time()}

        # Simple cache size limit
        if len(self._cache) > 1000:
            # Remove oldest entries
            sorted_keys = sorted(self._cache.keys(), key=lambda k: self._cache[k]["timestamp"])
            for key in sorted_keys[:100]:  # Remove oldest 100
                del self._cache[key]


class CohereReranker(BaseReranker):
    """Reranker using Cohere's reranking API"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_key = config.get("cohere_api_key") or os.getenv("COHERE_API_KEY")
        self.model = config.get("model", "rerank-english-v2.0")
        self.client = None

    async def initialize(self, config: Dict[str, Any]) -> Result:
        """Initialize Cohere client"""
        try:
            if not self.api_key:
                return Result.error("Cohere API key not configured")

            # Lazy import to avoid dependency if not used
            try:
                import cohere

                self.client = cohere.Client(self.api_key)
                self.initialized = True
                logger.info(f"Initialized Cohere reranker with model: {self.model}")
                return Result.ok(None)
            except ImportError:
                return Result.error(
                    "Cohere library not installed. Install with: pip install cohere"
                )
        except Exception as e:
            logger.error("Failed to initialize Cohere reranker: %s", _redact_error(e))
            return Result.error(f"Initialization failed: {str(e)}")

    async def shutdown(self) -> Result:
        """Shutdown Cohere client"""
        self.client = None
        self.initialized = False
        return Result.ok(None)

    async def rerank(
        self, query: str, results: List[SearchResult], top_k: Optional[int] = None
    ) -> Result:
        """Rerank results using Cohere API"""
        if not self.initialized:
            return Result.error("Cohere reranker not initialized")

        # Check cache first
        cached = await self._get_cached_results(query, results, top_k)
        if cached:
            rerank_result = RerankResult(
                results=cached[:top_k] if top_k else cached,
                metadata={
                    "reranker": "cohere",
                    "model": self.model,
                    "from_cache": True,
                    "total_results": len(results),
                    "returned_results": len(cached[:top_k]) if top_k else len(cached),
                },
            )
            return Result.ok(rerank_result)

        try:
            # Prepare documents for reranking
            documents = []
            for result in results:
                # Combine relevant information for reranking
                doc_text = f"{result.snippet}"
                if result.context:
                    doc_text = f"{doc_text} {result.context}"
                documents.append(doc_text)

            # Call Cohere rerank API
            response = await asyncio.to_thread(
                self.client.rerank,
                model=self.model,
                query=query,
                documents=documents,
                top_n=top_k or len(results),
            )

            # Build reranked results
            reranked_items = []
            for idx, result in enumerate(response.results):
                original_idx = result.index
                original_result = results[original_idx]

                rerank_item = RerankItem(
                    original_result=original_result,
                    rerank_score=result.relevance_score,
                    original_rank=original_idx,
                    new_rank=idx,
                )
                reranked_items.append(rerank_item)

            # Cache results
            await self._cache_results(query, results, reranked_items, top_k)

            # Create RerankResult with metadata
            rerank_result = RerankResult(
                results=reranked_items,
                metadata={
                    "reranker": "cohere",
                    "model": self.model,
                    "from_cache": False,
                    "total_results": len(results),
                    "returned_results": len(reranked_items),
                },
            )

            return Result.ok(rerank_result)

        except Exception as e:
            logger.error("Cohere reranking failed: %s", _redact_error(e))
            return Result.error(f"Reranking failed: {str(e)}")

    def get_capabilities(self) -> Dict[str, Any]:
        """Get Cohere reranker capabilities"""
        return {
            "name": "Cohere Reranker",
            "model": self.model,
            "supports_multilingual": self.model.startswith("rerank-multilingual"),
            "max_documents": 1000,
            "requires_api_key": True,
            "initialized": self.initialized,
        }


class LocalCrossEncoderReranker(BaseReranker):
    """Local reranker using cross-encoder models"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.model_name = config.get("model", "cross-encoder/ms-marco-MiniLM-L-6-v2")
        self.device = config.get("device", "cpu")
        self.model = None
        self.tokenizer = None

    async def initialize(self, config: Dict[str, Any]) -> Result:
        """Initialize cross-encoder model"""
        try:
            # Lazy import to avoid dependency if not used
            try:
                from sentence_transformers import CrossEncoder

                logger.info(f"Loading cross-encoder model: {self.model_name}")
                self.model = CrossEncoder(self.model_name, device=self.device)
                self.initialized = True
                logger.info(f"Initialized local cross-encoder reranker on {self.device}")
                return Result.ok(None)

            except ImportError:
                return Result.error(
                    "Sentence-transformers library not installed. "
                    "Install with: pip install sentence-transformers"
                )
        except Exception as e:
            logger.error("Failed to initialize cross-encoder: %s", _redact_error(e))
            return Result.error(f"Initialization failed: {str(e)}")

    async def shutdown(self) -> Result:
        """Shutdown cross-encoder model"""
        self.model = None
        self.initialized = False
        return Result.ok(None)

    async def rerank(
        self, query: str, results: List[SearchResult], top_k: Optional[int] = None
    ) -> Result:
        """Rerank results using cross-encoder model"""
        if not self.initialized:
            return Result.error("Cross-encoder reranker not initialized")

        # Check cache first
        cached = await self._get_cached_results(query, results, top_k)
        if cached:
            rerank_result = RerankResult(
                results=cached[:top_k] if top_k else cached,
                metadata={
                    "reranker": "cross-encoder",
                    "model": self.model_name,
                    "device": self.device,
                    "from_cache": True,
                    "total_results": len(results),
                    "returned_results": len(cached[:top_k]) if top_k else len(cached),
                },
            )
            return Result.ok(rerank_result)

        try:
            # Early return for empty results
            if not results:
                return Result.ok(
                    RerankResult(
                        results=[],
                        metadata={
                            "reranker": "cross-encoder",
                            "model": self.model_name,
                            "device": self.device,
                            "from_cache": False,
                            "total_results": 0,
                            "returned_results": 0,
                        },
                    )
                )

            # Prepare query-document pairs
            pairs = []
            for result in results:
                # Combine relevant information for reranking
                doc_text = f"{result.snippet}"
                if result.context:
                    doc_text = f"{doc_text} {result.context}"
                pairs.append([query, doc_text])

            # Get scores from cross-encoder
            scores = await asyncio.to_thread(self.model.predict, pairs)

            # Create indexed scores for sorting
            indexed_scores = [(score, idx) for idx, score in enumerate(scores)]
            indexed_scores.sort(reverse=True, key=lambda x: x[0])

            # Build reranked results
            reranked_items = []
            for new_rank, (score, original_idx) in enumerate(indexed_scores):
                if top_k and new_rank >= top_k:
                    break

                rerank_item = RerankItem(
                    original_result=results[original_idx],
                    rerank_score=float(score),
                    original_rank=original_idx,
                    new_rank=new_rank,
                )
                reranked_items.append(rerank_item)

            # Cache results
            await self._cache_results(query, results, reranked_items, top_k)

            # Create RerankResult with metadata
            rerank_result = RerankResult(
                results=reranked_items,
                metadata={
                    "reranker": "cross-encoder",
                    "model": self.model_name,
                    "device": self.device,
                    "from_cache": False,
                    "total_results": len(results),
                    "returned_results": len(reranked_items),
                },
            )

            return Result.ok(rerank_result)

        except Exception as e:
            logger.error("Cross-encoder reranking failed: %s", _redact_error(e))
            return Result.error(f"Reranking failed: {str(e)}")

    def get_capabilities(self) -> Dict[str, Any]:
        """Get cross-encoder reranker capabilities"""
        return {
            "name": "Local Cross-Encoder Reranker",
            "model": self.model_name,
            "device": self.device,
            "supports_multilingual": "multilingual" in self.model_name.lower(),
            "max_documents": 10000,  # Limited by memory
            "requires_api_key": False,
            "initialized": self.initialized,
        }


class TFIDFReranker(BaseReranker):
    """Simple TF-IDF based reranker as lightweight fallback"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.vectorizer = None

    async def initialize(self, config: Dict[str, Any]) -> Result:
        """Initialize TF-IDF vectorizer"""
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity

            self.vectorizer = TfidfVectorizer(
                max_features=config.get("max_features", 5000),
                ngram_range=(1, 2),
                stop_words="english",
            )
            self.cosine_similarity = cosine_similarity
            self.initialized = True
            logger.info("Initialized TF-IDF reranker")
            return Result.ok(None)

        except ImportError:
            return Result.error(
                "Scikit-learn not installed. Install with: pip install scikit-learn"
            )
        except Exception as e:
            logger.error("Failed to initialize TF-IDF reranker: %s", _redact_error(e))
            return Result.error(f"Initialization failed: {str(e)}")

    async def shutdown(self) -> Result:
        """Shutdown TF-IDF reranker"""
        self.vectorizer = None
        self.initialized = False
        return Result.ok(None)

    async def rerank(
        self, query: str, results: List[SearchResult], top_k: Optional[int] = None
    ) -> Result:
        """Rerank results using TF-IDF similarity"""
        if not self.initialized:
            return Result.error("TF-IDF reranker not initialized")

        # Check cache first
        cached = await self._get_cached_results(query, results, top_k)
        if cached:
            rerank_result = RerankResult(
                results=cached[:top_k] if top_k else cached,
                metadata={
                    "reranker": "tfidf",
                    "max_features": self.config.get("max_features", 5000),
                    "from_cache": True,
                    "total_results": len(results),
                    "returned_results": len(cached[:top_k]) if top_k else len(cached),
                },
            )
            return Result.ok(rerank_result)

        try:
            # Prepare documents
            documents = []
            for result in results:
                doc_text = f"{result.snippet}"
                if result.context:
                    doc_text = f"{doc_text} {result.context}"
                documents.append(doc_text)

            # Add query to documents for vectorization
            all_texts = [query] + documents

            # Vectorize texts
            tfidf_matrix = await asyncio.to_thread(self.vectorizer.fit_transform, all_texts)

            # Calculate similarities
            query_vector = tfidf_matrix[0:1]
            doc_vectors = tfidf_matrix[1:]
            similarities = self.cosine_similarity(query_vector, doc_vectors)[0]

            # Create indexed scores for sorting
            indexed_scores = [(score, idx) for idx, score in enumerate(similarities)]
            indexed_scores.sort(reverse=True, key=lambda x: x[0])

            # Build reranked results
            reranked_items = []
            for new_rank, (score, original_idx) in enumerate(indexed_scores):
                if top_k and new_rank >= top_k:
                    break

                rerank_item = RerankItem(
                    original_result=results[original_idx],
                    rerank_score=float(score),
                    original_rank=original_idx,
                    new_rank=new_rank,
                )
                reranked_items.append(rerank_item)

            # Cache results
            await self._cache_results(query, results, reranked_items, top_k)

            # Create RerankResult with metadata
            rerank_result = RerankResult(
                results=reranked_items,
                metadata={
                    "reranker": "tfidf",
                    "max_features": self.config.get("max_features", 5000),
                    "from_cache": False,
                    "total_results": len(results),
                    "returned_results": len(reranked_items),
                },
            )

            return Result.ok(rerank_result)

        except Exception as e:
            logger.error("TF-IDF reranking failed: %s", _redact_error(e))
            return Result.error(f"Reranking failed: {str(e)}")

    def get_capabilities(self) -> Dict[str, Any]:
        """Get TF-IDF reranker capabilities"""
        return {
            "name": "TF-IDF Reranker",
            "algorithm": "TF-IDF with cosine similarity",
            "supports_multilingual": False,
            "max_documents": 100000,
            "requires_api_key": False,
            "initialized": self.initialized,
        }


class HybridReranker(BaseReranker):
    """Hybrid reranker that combines multiple reranking strategies"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.primary_reranker: Optional[IReranker] = None
        self.fallback_reranker: Optional[IReranker] = None
        self.weight_primary = config.get("weight_primary", 0.7)
        self.weight_fallback = config.get("weight_fallback", 0.3)

    def set_rerankers(self, primary: IReranker, fallback: Optional[IReranker] = None):
        """Set primary and fallback rerankers"""
        self.primary_reranker = primary
        self.fallback_reranker = fallback

    async def initialize(self, config: Dict[str, Any]) -> Result:
        """Initialize hybrid reranker"""
        if not self.primary_reranker:
            return Result.error("Primary reranker not set")

        # Initialize primary reranker
        primary_result = await self.primary_reranker.initialize(config)
        if not primary_result.is_success:
            return primary_result

        # Initialize fallback if available
        if self.fallback_reranker:
            fallback_result = await self.fallback_reranker.initialize(config)
            if not fallback_result.is_success:
                logger.warning(f"Fallback reranker initialization failed: {fallback_result.error}")

        self.initialized = True
        return Result.ok(None)

    async def shutdown(self) -> Result:
        """Shutdown hybrid reranker"""
        if self.primary_reranker:
            await self.primary_reranker.shutdown()
        if self.fallback_reranker:
            await self.fallback_reranker.shutdown()
        self.initialized = False
        return Result.ok(None)

    async def rerank(
        self, query: str, results: List[SearchResult], top_k: Optional[int] = None
    ) -> Result:
        """Rerank using hybrid approach"""
        if not self.initialized:
            return Result.error("Hybrid reranker not initialized")

        # Try primary reranker first
        primary_result = await self.primary_reranker.rerank(query, results, top_k)

        if primary_result.is_success:
            # Update metadata to indicate hybrid reranker was used
            if isinstance(primary_result.data, RerankResult):
                primary_result.data.metadata["hybrid"] = True
                primary_result.data.metadata["primary_succeeded"] = True
            return primary_result

        # If primary fails and we have fallback, use it
        if self.fallback_reranker:
            logger.warning(f"Primary reranker failed: {primary_result.error}, using fallback")
            fallback_result = await self.fallback_reranker.rerank(query, results, top_k)
            if fallback_result.is_success and isinstance(fallback_result.data, RerankResult):
                fallback_result.data.metadata["hybrid"] = True
                fallback_result.data.metadata["primary_succeeded"] = False
                fallback_result.data.metadata["fallback_reason"] = str(primary_result.error)
            return fallback_result

        return primary_result

    def get_capabilities(self) -> Dict[str, Any]:
        """Get hybrid reranker capabilities"""
        capabilities = {
            "name": "Hybrid Reranker",
            "primary": (
                self.primary_reranker.get_capabilities() if self.primary_reranker else None
            ),
            "fallback": (
                self.fallback_reranker.get_capabilities() if self.fallback_reranker else None
            ),
            "weight_primary": self.weight_primary,
            "weight_fallback": self.weight_fallback,
            "initialized": self.initialized,
        }
        return capabilities


import re as _re

# Substrings that look like secrets (bearer tokens / api keys) are scrubbed from
# any error string exposed via ``last_error`` so the dispatcher lane never sees a
# credential. Candidate/query text is NEVER put in ``last_error``.
_SECRET_PATTERNS = [
    _re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
    _re.compile(r"(?i)(api[_-]?key|token|secret|authorization)\s*[=:]\s*[^\s,;]+"),
    _re.compile(r"\bsk-[A-Za-z0-9._\-]{8,}"),
    _re.compile(r"\bpa-[A-Za-z0-9._\-]{8,}"),
]


def _redact_error(exc: BaseException) -> str:
    """Return a redacted ``"<ExcType>: <message>"`` string safe for ``last_error``.

    Strips bearer/api-key-like substrings; carries no candidate/query text.
    """
    return _redact_text(f"{type(exc).__name__}: {exc}")


def _redact_text(text: Optional[str]) -> Optional[str]:
    """Scrub bearer/api-key-like substrings from an arbitrary string.

    Safe for error strings surfaced from provider ``Result.error`` fields or
    exception messages before they reach logs or diagnostics.
    """
    if text is None:
        return None
    msg = str(text)
    for pat in _SECRET_PATTERNS:
        msg = pat.sub("[REDACTED]", msg)
    return msg


class VoyageReranker:
    """Synchronous Voyage AI reranker using voyageai.Client.rerank().

    Intentionally does not extend BaseReranker — the base class interface is
    async and the dispatcher's search() is synchronous.  voyageai.Client.rerank()
    is synchronous, so no bridge is needed.
    """

    def __init__(self, model: str = "rerank-2"):
        api_key = os.getenv("VOYAGE_API_KEY")
        import voyageai

        self._client = voyageai.Client(api_key=api_key) if api_key else voyageai.Client()
        self._model = model
        #: Dispatcher-lane failure signal: None = ok, str = a swallowed failure.
        self.last_error: Optional[str] = None

    def rerank(self, query: str, candidates: List[Dict], top_k: int) -> List[Dict]:
        """Reorder candidates by Voyage relevance score.

        Document text preference order: `_rerank_doc` (full chunk content),
        then `snippet`, then `file` path.  Falls back to original order on
        empty documents or any API error.
        """
        self.last_error = None
        documents = [
            c.get("_rerank_doc") or c.get("snippet") or c.get("file", "") for c in candidates
        ]
        if not any(documents):
            return candidates[:top_k]
        try:
            result = self._client.rerank(
                query=query, documents=documents, model=self._model, top_k=top_k
            )
            return [candidates[r.index] for r in result.results]
        except Exception as e:
            self.last_error = _redact_error(e)
            logger.warning(
                "VoyageReranker.rerank() failed, using original order: %s", _redact_error(e)
            )
            return candidates[:top_k]


class FlashRankReranker:
    """Synchronous FlashRank reranker (ONNX-quantized cross-encoder, no API key needed).

    Lightweight ~34MB model, CPU-friendly. Default OSS reranker when no Voyage key
    is present.
    """

    def __init__(self, model: str = "ms-marco-MiniLM-L-12-v2"):
        self._model_name = model
        self._ranker = None
        #: Dispatcher-lane failure signal: None = ok, str = a swallowed failure.
        self.last_error: Optional[str] = None

    def _load(self):
        if self._ranker is None:
            from flashrank import Ranker

            self._ranker = Ranker(model_name=self._model_name)

    def rerank(self, query: str, candidates: List[Dict], top_k: int) -> List[Dict]:
        """Reorder candidates by FlashRank relevance score.

        Document text preference order: ``_rerank_doc``, then ``snippet``, then
        ``file`` path.  Falls back to original order on import error or exception.
        """
        self.last_error = None
        try:
            self._load()
        except ImportError as e:
            self.last_error = _redact_error(e)
            logger.warning("FlashRankReranker: flashrank not installed, using original order")
            return candidates[:top_k]
        try:
            from flashrank import RerankRequest

            passages = [
                {
                    "id": i,
                    "text": c.get("_rerank_doc") or c.get("snippet") or c.get("file", ""),
                }
                for i, c in enumerate(candidates)
            ]
            request = RerankRequest(query=query, passages=passages)
            results = self._ranker.rerank(request)
            return [candidates[r["id"]] for r in results[:top_k]]
        except Exception as e:
            self.last_error = _redact_error(e)
            logger.warning(
                "FlashRankReranker.rerank() failed, using original order: %s", _redact_error(e)
            )
            return candidates[:top_k]


class CrossEncoderReranker:
    """Synchronous cross-encoder reranker via sentence-transformers.

    Better quality than FlashRank but heavier (~500MB torch + model). Opt-in via
    ``RERANKER_TYPE=cross-encoder``.
    """

    def __init__(self, model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        self._model_name = model
        self._model = None
        #: Dispatcher-lane failure signal: None = ok, str = a swallowed failure.
        self.last_error: Optional[str] = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self._model_name)

    def rerank(self, query: str, candidates: List[Dict], top_k: int) -> List[Dict]:
        """Reorder candidates by cross-encoder relevance score.

        Falls back to original order on import error or exception.
        """
        self.last_error = None
        try:
            self._load()
        except ImportError as e:
            self.last_error = _redact_error(e)
            logger.warning(
                "CrossEncoderReranker: sentence-transformers not installed, using original order"
            )
            return candidates[:top_k]
        try:
            docs = [
                c.get("_rerank_doc") or c.get("snippet") or c.get("file", "") for c in candidates
            ]
            pairs = [(query, doc) for doc in docs]
            scores = self._model.predict(pairs)
            indexed = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
            return [candidates[i] for i, _ in indexed[:top_k]]
        except Exception as e:
            self.last_error = _redact_error(e)
            logger.warning(
                "CrossEncoderReranker.rerank() failed, using original order: %s", _redact_error(e)
            )
            return candidates[:top_k]


# =====================================================================
# Sync <-> async bridge (RERANKEND / IF-0-RERANKEND-1)
# =====================================================================

_T = TypeVar("_T")


def run_coroutine_sync(coro: Awaitable[_T], *, timeout: Optional[float] = None) -> _T:
    """Run an awaitable to completion from synchronous code, loop-safe.

    This is the named sync/async bridge primitive for the reranker stack. The
    synchronous dispatcher path uses it to drive an async :class:`IReranker`
    WITHOUT ever calling :func:`asyncio.run` inside an already-running event
    loop.

    Behavior:
        * If **no** event loop is running in the current thread, the coroutine
          is executed on a fresh private loop via ``loop.run_until_complete``.
        * If a loop **is** already running in the current thread, the coroutine
          is executed on its own private loop inside a dedicated worker thread,
          and the calling thread blocks on that worker. This avoids the
          ``RuntimeError: asyncio.run() cannot be called from a running event
          loop`` and never re-enters or blocks the live loop.

    Args:
        coro: The awaitable/coroutine to run.
        timeout: Optional seconds to wait when a worker thread is used. ``None``
            waits indefinitely. Ignored on the no-running-loop fast path.

    Returns:
        The coroutine's result.

    Raises:
        Any exception raised by the coroutine is re-raised in the calling thread.
        TimeoutError: if ``timeout`` elapses while a worker thread is running.
    """
    try:
        asyncio.get_running_loop()
        loop_running = True
    except RuntimeError:
        loop_running = False

    if not loop_running:
        # Fast path: safe to own the loop on this thread. Preserve any
        # pre-existing (non-running) loop bound to this thread and RESTORE it in
        # ``finally`` — never clobber it with ``set_event_loop(None)``.
        try:
            prior_loop: Optional[asyncio.AbstractEventLoop] = (
                asyncio.get_event_loop_policy().get_event_loop()
            )
        except Exception:  # noqa: BLE001 - no bound loop / policy raised
            prior_loop = None
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            if timeout is not None:
                return loop.run_until_complete(asyncio.wait_for(coro, timeout))
            return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(prior_loop)
            loop.close()

    # A loop is already running here; drive the coroutine on a private loop in a
    # worker thread so we never call into / block the live loop.
    box: Dict[str, Any] = {}

    def _runner() -> None:
        worker_loop = asyncio.new_event_loop()
        try:
            if timeout is not None:
                box["value"] = worker_loop.run_until_complete(asyncio.wait_for(coro, timeout))
            else:
                box["value"] = worker_loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on caller thread
            box["error"] = exc
        finally:
            worker_loop.close()

    thread = threading.Thread(target=_runner, name="rerank-sync-bridge", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"run_coroutine_sync timed out after {timeout}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


class SyncRerankerAdapter:
    """Thin synchronous facade over an async :class:`IReranker`.

    Lets synchronous call sites drive an async reranker through
    :func:`run_coroutine_sync` without managing an event loop themselves. It is
    intentionally minimal: it forwards ``(query, results, top_k)`` straight to
    the async ``rerank`` and returns whatever that returns (a
    ``Result[RerankResult]`` for canonical implementations). Any dispatcher-side
    ``List[Dict]`` <-> :class:`SearchResult` shape migration is a consumer (R2)
    concern and deliberately NOT baked in here.
    """

    def __init__(self, async_reranker: "IReranker", *, timeout: Optional[float] = None):
        self._reranker = async_reranker
        self._timeout = timeout

    def rerank(self, query: str, results: List[SearchResult], top_k: Optional[int] = None) -> Any:
        return run_coroutine_sync(
            self._reranker.rerank(query, results, top_k), timeout=self._timeout
        )


# =====================================================================
# EndpointReranker — provider-neutral rerank.v1 implementation
# =====================================================================


class EndpointReranker(IReranker):
    """Canonical :class:`IReranker` that reranks over the ``rerank.v1`` wire
    contract via an INJECTED transport callable.

    The transport is ``Callable[[Dict], Dict]``: it receives a serialized
    :class:`~mcp_server.interfaces.rerank_contracts.RerankRequest` (as a dict)
    and returns a serialized
    :class:`~mcp_server.interfaces.rerank_contracts.RerankResponse` (as a dict).
    Injecting the transport keeps the reranker fully testable with a fake — no
    live network is ever performed by this class.

    Guarantees:
        * Stable, caller-assigned ``candidate_id`` values survive request ->
          response reordering; results are correlated back to their original
          :class:`SearchResult` by id only.
        * The response is validated with ``validate_rerank_response``; missing /
          duplicated / unknown ids RAISE ``RerankContractError`` (a wire-contract
          fault) rather than being masked as per-candidate failures.
        * Per-candidate provider partial failure (a result with status
          ``FAILED`` / no score) is represented per candidate: such candidates
          are ordered last and reported in the result metadata.
        * A structured :class:`RerankDiagnostics` / :class:`RerankOutcome` is
          reported (``last_diagnostics`` / ``last_outcome``); no query or
          document text is placed in diagnostics.
        * Scores are only meaningful within a single provider response and are
          never treated as comparable across providers.
    """

    def __init__(
        self,
        transport: Callable[[Dict[str, Any]], Dict[str, Any]],
        *,
        provider: str = "endpoint",
        timeout: Optional[float] = None,
    ):
        self._transport = transport
        self.provider = provider
        # Intrinsic per-call timeout so DIRECT async consumers (hybrid /
        # cross_repo) are bounded even without the dispatcher's wait_for wrapper.
        if timeout is None:
            # Mirror the dispatcher's _get_endpoint_rerank_timeout_seconds clamp:
            # fall back to 30s on an unparseable value and floor to >=1s so a
            # 0/negative env can never disable the intrinsic per-call timeout.
            try:
                timeout = max(float(os.getenv("MCP_RERANK_TIMEOUT_S", "30.0")), 1.0)
            except (TypeError, ValueError):
                timeout = 30.0
        self._timeout = timeout
        self.last_outcome: Optional[RerankOutcome] = None
        self.last_diagnostics: Optional[RerankDiagnostics] = None
        self.last_fallback: Optional[RerankFallbackSignal] = None

    @staticmethod
    def _candidate_text(result: SearchResult) -> str:
        text = result.snippet or ""
        if result.context:
            text = f"{text} {result.context}".strip()
        return text

    async def rerank(
        self, query: str, results: List[SearchResult], top_k: Optional[int] = None
    ) -> Result:
        """Rerank ``results`` against ``query`` over ``rerank.v1``.

        Returns ``Result.ok(RerankResult)`` on success (see the module-level
        return-contract note). Propagates ``RerankContractError`` on a
        wire-contract violation.
        """
        # Lazy import avoids a module-load cycle: rerank_contracts imports
        # RerankOutcome from this module.
        import time

        from mcp_server.interfaces.rerank_contracts import (
            RerankCandidate,
            RerankRequest,
            RerankResponse,
            validate_rerank_response,
        )

        limit = top_k if top_k is not None else len(results)

        # Assign STABLE candidate ids (index-based; stable within this call and
        # across request -> response reordering) and remember the mapping back to
        # the original SearchResult and its original rank.
        by_id: Dict[str, SearchResult] = {}
        original_rank: Dict[str, int] = {}
        candidates = []
        for idx, result in enumerate(results):
            cid = f"cand-{idx}"
            by_id[cid] = result
            original_rank[cid] = idx
            candidates.append(RerankCandidate(candidate_id=cid, text=self._candidate_text(result)))

        request = RerankRequest(
            request_id=f"rerank-{uuid.uuid4().hex}",
            query=query,
            candidates=candidates,
            top_k=limit,
        )

        started = time.perf_counter()
        # Run the synchronous transport off the event loop so blocking network
        # I/O never stalls the running loop (IF-0-RERANKEND-1).
        raw_response = await asyncio.wait_for(
            asyncio.to_thread(self._transport, request.to_dict()),
            timeout=self._timeout,
        )
        response = RerankResponse.from_dict(raw_response)
        duration_ms = (time.perf_counter() - started) * 1000.0

        # Wire-contract validation: missing / duplicated / unknown ids RAISE.
        validate_rerank_response(request, response)

        # Partition by outcome. Scored (SUCCEEDED, real score) candidates are
        # ordered by descending score; per-candidate failures are represented and
        # placed last in their original relative order. Scores are only
        # meaningful within this single response.
        scored = []
        failed_ids: List[str] = []
        statuses: Dict[str, str] = {}
        for r in response.results:
            statuses[r.candidate_id] = r.status.value
            if r.status is RerankOutcome.SUCCEEDED and r.score is not None:
                scored.append(r)
            else:
                failed_ids.append(r.candidate_id)

        scored.sort(key=lambda r: r.score, reverse=True)
        failed_ids.sort(key=lambda cid: original_rank[cid])

        ordered: List[tuple] = [(r.candidate_id, r.score) for r in scored]
        ordered += [(cid, None) for cid in failed_ids]

        reranked_items: List[RerankItem] = []
        for new_rank, (cid, score) in enumerate(ordered):
            if limit is not None and new_rank >= limit:
                break
            reranked_items.append(
                RerankItem(
                    original_result=by_id[cid],
                    rerank_score=float(score) if score is not None else 0.0,
                    original_rank=original_rank[cid],
                    new_rank=new_rank,
                    explanation=(
                        None if score is not None else f"status={statuses.get(cid, 'failed')}"
                    ),
                )
            )

        # Determine outcome truthfully.
        new_order_ids = [cid for cid, _ in ordered[: len(reranked_items)]]
        original_order_ids = [f"cand-{i}" for i in range(len(reranked_items))]
        reordered = new_order_ids != original_order_ids
        if reordered:
            outcome = RerankOutcome.SUCCEEDED
        else:
            outcome = RerankOutcome.ATTEMPTED

        self.last_outcome = outcome
        self.last_diagnostics = RerankDiagnostics(
            outcome=outcome,
            provider=response.provider or self.provider,
            candidate_count=response.candidate_count or len(results),
            returned_count=len(reranked_items),
            reordered=reordered,
            duration_ms=response.latency_ms if response.latency_ms is not None else duration_ms,
            policy=None,
        )

        rerank_result = RerankResult(
            results=reranked_items,
            metadata={
                "reranker": "endpoint",
                "provider": response.provider or self.provider,
                "model_id": response.model_id,
                "model_revision": response.model_revision,
                "outcome": outcome.value,
                "from_cache": False,
                "total_results": len(results),
                "returned_results": len(reranked_items),
                "scored_count": response.scored_count or len(scored),
                "partial_failures": list(failed_ids),
                "candidate_statuses": statuses,
                # Scores are only comparable within this single provider response.
                "cross_provider_comparable": False,
            },
        )
        return Result.ok(rerank_result)

    def get_capabilities(self) -> Dict[str, Any]:
        return {
            "name": "Endpoint Reranker",
            "contract_version": "rerank.v1",
            "provider": self.provider,
            "transport_injected": True,
            "requires_api_key": False,
            "cross_provider_comparable": False,
        }


# =====================================================================
# Commercial adapter: Cohere /v2/rerank behind the endpoint transport
# =====================================================================

#: Current (non-legacy) Cohere rerank model. Legacy models remain usable but
#: must be selected explicitly via the ``model`` argument.
COHERE_CURRENT_RERANK_MODEL = "rerank-v3.5"
#: A known legacy Cohere model, provided only for explicit opt-in.
COHERE_LEGACY_RERANK_MODEL = "rerank-english-v2.0"


class CohereV2RerankAdapter:
    """``rerank.v1`` transport that speaks Cohere's ``/v2/rerank`` request shape.

    This is an endpoint *transport* (a ``Callable[[Dict], Dict]``) suitable for
    injection into :class:`EndpointReranker`. It:

        1. Translates an inbound ``rerank.v1`` request dict into the current
           Cohere ``/v2/rerank`` body shape (``{model, query, documents,
           top_n}``).
        2. Hands that body to an INJECTED ``send`` callable representing the HTTP
           endpoint (``Callable[[Dict], Dict]`` returning Cohere's native
           response ``{"results": [{"index": i, "relevance_score": s}, ...]}``).
           ``send`` is required — there is no default, so the adapter can never
           make a live call in tests.
        3. Maps the Cohere response back into a ``rerank.v1`` response dict,
           echoing the stable ``candidate_id`` for each result by index.

    The commercial provider's raw scores are NOT assumed comparable to any other
    provider's; they are carried through only as within-response scores.
    """

    def __init__(
        self,
        send: Callable[[Dict[str, Any]], Dict[str, Any]],
        *,
        model: str = COHERE_CURRENT_RERANK_MODEL,
        provider: str = "cohere",
    ):
        self._send = send
        self.model = model
        self.provider = provider

    def build_v2_request(self, request_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Build the Cohere ``/v2/rerank`` body from a ``rerank.v1`` request.

        ``top_n`` is set to the FULL candidate count, never the request's
        ``top_k``. Cohere treats ``top_n`` as response truncation, but the
        ``rerank.v1`` contract requires one result per requested candidate (so
        set-based id validation holds and per-candidate failures stay
        representable). The caller's ``top_k`` truncation is applied by
        :class:`EndpointReranker` on the mapped-back results instead.
        """
        candidates = request_dict.get("candidates", [])
        return {
            "model": self.model,
            "query": request_dict.get("query", ""),
            "documents": [c.get("text", "") for c in candidates],
            "top_n": len(candidates),
        }

    def __call__(self, request_dict: Dict[str, Any]) -> Dict[str, Any]:
        candidates = request_dict.get("candidates", [])
        ids = [c["candidate_id"] for c in candidates]

        body = self.build_v2_request(request_dict)
        cohere_response = self._send(body)

        results = []
        scored = 0
        for item in cohere_response.get("results", []):
            index = item["index"]
            # Guard against a malformed provider index: a negative or
            # out-of-range value would silently wrap via ``ids[index]``.
            if not (0 <= index < len(ids)):
                raise IndexError(
                    f"Cohere result index {index} out of range for {len(ids)} candidates"
                )
            score = item.get("relevance_score")
            status = RerankOutcome.SUCCEEDED if score is not None else RerankOutcome.FAILED
            if score is not None:
                scored += 1
            results.append(
                {
                    "candidate_id": ids[index],
                    "status": status.value,
                    "score": score,
                }
            )

        return {
            "contract_version": request_dict.get("contract_version", "rerank.v1"),
            "request_id": request_dict.get("request_id", ""),
            "provider": self.provider,
            "model_id": self.model,
            # Report the provider's response revision when present; otherwise
            # None (unknown). Never echo the configured ``model`` as a revision
            # it isn't -- that would misreport the served model version.
            "model_revision": cohere_response.get("model_revision"),
            "results": results,
            "candidate_count": len(candidates),
            "scored_count": scored,
            "latency_ms": cohere_response.get("latency_ms"),
        }


class RerankerFactory(IRerankerFactory):
    """Factory for creating reranker instances"""

    #: In-process (standalone-profile) rerankers. These are NOT an implicit
    #: default: they are only constructable via
    #: :meth:`create_standalone_reranker` with ``standalone_profile=True``.
    IN_PROCESS_STANDALONE_RERANKERS: Dict[str, type] = {
        "flashrank": FlashRankReranker,
        "cross-encoder": CrossEncoderReranker,
    }

    def __init__(self):
        self.reranker_types = {
            "cohere": CohereReranker,
            "cross-encoder": LocalCrossEncoderReranker,
            "tfidf": TFIDFReranker,
            "hybrid": HybridReranker,
            "voyage": VoyageReranker,
        }

    def create_reranker(self, reranker_type: str, config: Dict[str, Any]) -> IReranker:
        """Create a reranker instance"""
        if reranker_type not in self.reranker_types:
            raise ValueError(f"Unknown reranker type: {reranker_type}")

        reranker_class = self.reranker_types[reranker_type]
        reranker = reranker_class(config)

        # Special handling for hybrid reranker
        if reranker_type == "hybrid":
            primary_type = config.get("primary_type", "cohere")
            fallback_type = config.get("fallback_type", "tfidf")

            primary = self.create_reranker(primary_type, config)
            fallback = self.create_reranker(fallback_type, config) if fallback_type else None

            reranker.set_rerankers(primary, fallback)

        return reranker

    def get_available_rerankers(self) -> List[str]:
        """Get list of available reranker types"""
        return list(self.reranker_types.keys())

    def register_reranker(self, name: str, reranker_class: type):
        """Register a custom reranker type"""
        if not issubclass(reranker_class, IReranker):
            raise ValueError(f"{reranker_class} must implement IReranker interface")
        self.reranker_types[name] = reranker_class

    @classmethod
    def create_default(cls) -> "TFIDFReranker":
        """Return a zero-dependency default reranker."""
        return TFIDFReranker({})

    def create_standalone_reranker(
        self,
        name: str,
        *,
        standalone_profile: bool = False,
        model: Optional[str] = None,
    ):
        """Construct an in-process (FlashRank / cross-encoder) reranker.

        In-process rerankers are demoted to an EXPLICIT standalone profile — they
        are never an implicit default. The caller must pass
        ``standalone_profile=True`` to opt in; otherwise this raises. This is the
        factory gate the RERANKEND phase requires so the endpoint rerankers stay
        the default path and the heavy local models are only selected on purpose.

        Args:
            name: One of :attr:`IN_PROCESS_STANDALONE_RERANKERS` (``"flashrank"``
                or ``"cross-encoder"``).
            standalone_profile: Must be ``True`` to construct an in-process
                reranker. When ``False`` (the default), raises ``ValueError``.
            model: Optional explicit model id; falls back to the class default.

        Raises:
            ValueError: if ``standalone_profile`` is not ``True``, or ``name`` is
                not a known in-process standalone reranker.
        """
        if not standalone_profile:
            raise ValueError(
                "In-process rerankers are a standalone profile, not an implicit "
                "default; pass standalone_profile=True to select "
                f"'{name}' explicitly."
            )
        cls = self.IN_PROCESS_STANDALONE_RERANKERS.get(name)
        if cls is None:
            raise ValueError(
                f"Unknown in-process standalone reranker: {name!r}. "
                f"Available: {sorted(self.IN_PROCESS_STANDALONE_RERANKERS)}"
            )
        return cls(model=model) if model else cls()


# Default factory instance
default_reranker_factory = RerankerFactory()
