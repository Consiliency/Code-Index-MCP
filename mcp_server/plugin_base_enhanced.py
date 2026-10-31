"""Enhanced plugin base with semantic search support."""

from __future__ import annotations

import logging
import os
from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

from typing_extensions import TypedDict

from .config.env_vars import QdrantAuthConfigError
from .plugin_base import (
    IPlugin,
    SearchOpts,
    SearchResult,
)
from .utils.semantic_indexer import SemanticIndexer

if TYPE_CHECKING:
    from mcp_server.core.repo_context import RepoContext

    from .storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class SemanticSearchResult(TypedDict):
    """Extended search result with semantic relevance score."""

    file: str
    line: int
    snippet: str
    score: float
    embedding_id: Optional[str]


class PluginWithSemanticSearch(IPlugin):
    """Enhanced plugin base class with semantic search capabilities."""

    def __init__(
        self,
        sqlite_store: Optional["SQLiteStore"] = None,
        enable_semantic: bool = True,
        qdrant_host: Optional[str] = None,
        qdrant_port: Optional[int] = None,
    ):
        """Initialize plugin with optional semantic search support."""
        self._ctx: Optional["RepoContext"] = None
        self._semantic_indexer: Optional[SemanticIndexer] = None
        self._enable_semantic = (
            enable_semantic and os.getenv("SEMANTIC_SEARCH_ENABLED", "false").lower() == "true"
        )

        if self._enable_semantic:
            try:
                # Initialize semantic indexer with Qdrant configuration
                qdrant_host = qdrant_host or os.getenv("QDRANT_HOST", "localhost")
                qdrant_port = qdrant_port or int(os.getenv("QDRANT_PORT", "6333"))
                collection_name = os.getenv("SEMANTIC_COLLECTION_NAME", "code-embeddings")

                # Create Qdrant URL or use memory
                if qdrant_host == ":memory:":
                    qdrant_path = ":memory:"
                else:
                    qdrant_path = f"http://{qdrant_host}:{qdrant_port}"

                self._semantic_indexer = SemanticIndexer(
                    collection=f"{collection_name}-{self.lang}", qdrant_path=qdrant_path
                )
                logger.info(f"Semantic search enabled for {self.lang} plugin")
            except QdrantAuthConfigError as e:
                logger.warning("Semantic search disabled for %s: %s", self.lang, e)
                self._enable_semantic = False
                self._semantic_indexer = None
            except ConnectionRefusedError:
                logger.debug(
                    f"Qdrant not available at {qdrant_path}, semantic search disabled for {self.lang}"
                )
                self._enable_semantic = False
                self._semantic_indexer = None
            except Exception as e:
                logger.debug(f"Failed to initialize semantic search for {self.lang}: {e}")
                self._enable_semantic = False
                self._semantic_indexer = None

    def bind(self, ctx: "RepoContext") -> None:
        self._ctx = ctx

    def index_with_embeddings(
        self, path: Path, content: str, symbols: List[Dict[str, Any]]
    ) -> None:
        """Create and store embeddings for code symbols.

        Args:
            path: File path
            content: File content
            symbols: List of symbols extracted from the file
        """
        if not self._semantic_indexer or not self._enable_semantic:
            return

        try:
            # Skip the file-level indexing since we'll index symbols directly
            # self._semantic_indexer.index_file(str(path))

            # Index individual symbols with context
            for symbol in symbols:
                # Extract symbol context (e.g., function body, class definition)
                start_line = symbol.get("line", 1) - 1
                end_line = symbol.get("end_line", start_line + 5)

                lines = content.split("\n")
                context_lines = lines[max(0, start_line) : min(len(lines), end_line + 1)]
                symbol_context = "\n".join(context_lines)

                # Create a searchable text representation
                searchable_text = f"{symbol['kind']} {symbol['symbol']}\n{symbol.get('signature', '')}\n{symbol.get('doc', '')}\n{symbol_context}"

                # Index the symbol
                self._semantic_indexer.index_symbol(
                    file=str(path),
                    name=symbol["symbol"],
                    kind=symbol["kind"],
                    signature=symbol.get("signature", ""),
                    line=symbol["line"],
                    span=(start_line, end_line),
                    doc=symbol.get("doc"),
                    content=searchable_text,
                )

            logger.debug(f"Indexed {len(symbols)} symbols with embeddings for {path}")

        except Exception as e:
            logger.error(f"Failed to create embeddings for {path}: {e}")

    def semantic_search(self, query: str, limit: int = 20) -> List[SemanticSearchResult]:
        """Perform semantic search using vector embeddings.

        Args:
            query: Search query
            limit: Maximum number of results

        Returns:
            List of semantic search results with relevance scores
        """
        if not self._semantic_indexer or not self._enable_semantic:
            return []

        try:
            # Perform semantic search
            results = self._semantic_indexer.search(query, limit=limit)

            # Convert to SemanticSearchResult format
            semantic_results = []
            for result in results:
                # Extract snippet around the matched symbol
                try:
                    with open(result["file"], "r") as f:
                        lines = f.readlines()
                        line_idx = result["line"] - 1

                        # Get context lines
                        start = max(0, line_idx - 2)
                        end = min(len(lines), line_idx + 3)
                        snippet_lines = lines[start:end]
                        snippet = "".join(snippet_lines).strip()

                        semantic_results.append(
                            SemanticSearchResult(
                                file=result["file"],
                                start_line=result["line"],
                                end_line=result["line"],
                                snippet=snippet,
                                score=result["score"],
                                embedding_id=result.get("id"),
                            )
                        )
                except Exception as e:
                    logger.warning(f"Failed to extract snippet for {result['file']}: {e}")

            return semantic_results

        except Exception as e:
            logger.error(f"Semantic search failed: {e}")
            return []

    def search(self, query: str, opts: SearchOpts | None = None) -> Iterable[SearchResult]:
        """Enhanced search with semantic capabilities.

        Args:
            query: Search query
            opts: Search options including semantic flag

        Returns:
            Search results (semantic or traditional based on opts)
        """
        if opts and opts.get("semantic") and self._enable_semantic:
            # Perform semantic search
            limit = opts.get("limit", 20)
            semantic_results = self.semantic_search(query, limit)

            # Convert to standard SearchResult format
            for result in semantic_results:
                yield SearchResult(
                    file=result["file"],
                    start_line=result["line"],
                    end_line=result["line"],
                    snippet=result["snippet"],
                )
        else:
            # Fallback to traditional search (must be implemented by subclass)
            yield from self._traditional_search(query, opts)

    @abstractmethod
    def _traditional_search(
        self, query: str, opts: SearchOpts | None = None
    ) -> Iterable[SearchResult]:
        """Traditional search implementation (must be overridden by subclasses)."""
