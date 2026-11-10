"""Base class for document processing plugins with semantic capabilities."""

import hashlib
import logging
import mimetypes
import re
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp_server.plugin_base import IndexShard, Reference, SearchResult, SymbolDef
from mcp_server.plugins.specialized_plugin_base import SpecializedPluginBase
from mcp_server.storage.sqlite_store import SQLiteStore

from . import document_interfaces as document_contracts

logger = logging.getLogger(__name__)


@dataclass
class DocumentChunk:
    """Represents a chunk of document content."""

    content: str
    start_pos: int
    end_pos: int
    chunk_index: int
    metadata: Dict[str, Any] = field(default_factory=dict)
    embedding_text: Optional[str] = None  # Optimized text for embedding


@dataclass
class DocumentStructure:
    """Represents the structure of a document."""

    sections: List[Dict[str, Any]]  # title, level, start_pos, end_pos
    headings: List[Dict[str, Any]]  # text, level, position
    metadata: Dict[str, Any]  # title, author, date, etc.
    outline: List[Dict[str, Any]]  # hierarchical outline


@dataclass
class DocumentMetadata:
    """Standard document metadata."""

    title: Optional[str] = None
    author: Optional[str] = None
    created_date: Optional[str] = None
    modified_date: Optional[str] = None
    document_type: Optional[str] = None
    language: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    custom: Dict[str, Any] = field(default_factory=dict)


class BaseDocumentPlugin(SpecializedPluginBase):
    """Base class for document processing plugins."""

    # Default chunking parameters
    DEFAULT_CHUNK_SIZE = 512  # tokens
    DEFAULT_CHUNK_OVERLAP = 50  # tokens

    # Approximate characters per token (rough estimate)
    CHARS_PER_TOKEN = 4

    def __init__(
        self,
        language_config: Dict[str, Any],
        sqlite_store: Optional[SQLiteStore] = None,
        enable_semantic: bool = False,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ):
        """Initialize document plugin with chunking parameters."""
        # Store language info before calling parent
        self.lang = language_config.get("code", language_config.get("name", "unknown"))
        self.language_name = language_config.get("name", self.lang)

        super().__init__(language_config, sqlite_store, enable_semantic)

        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

        # Document-specific caches
        self._structure_cache: Dict[str, DocumentStructure] = {}
        self._chunk_cache: Dict[str, List[DocumentChunk]] = {}

        # Supported document types
        self.supported_extensions = self._get_supported_extensions()

    @abstractmethod
    def _get_supported_extensions(self) -> List[str]:
        """Get list of supported file extensions."""

    @abstractmethod
    def extract_structure(self, content: str, file_path: Path) -> DocumentStructure:
        """Extract document structure (headings, sections, etc)."""

    @abstractmethod
    def extract_metadata(self, content: str, file_path: Path) -> DocumentMetadata:
        """Extract document metadata."""

    @abstractmethod
    def parse_content(self, content: str, file_path: Path) -> str:
        """Parse raw content to plain text."""

    def process_document(
        self, content: str, file_path: Optional[str | Path] = None
    ) -> document_contracts.ProcessedDocument:
        """Process a document using the shared document contract."""
        resolved_path = Path(file_path or f"memory.{self.lang}")
        metadata = self.extract_metadata(content, resolved_path)
        structure = self.extract_structure(content, resolved_path)
        chunks = self.create_searchable_chunks(content, resolved_path)

        return document_contracts.ProcessedDocument(
            path=str(resolved_path),
            content=content,
            structure=self._to_contract_structure(structure, metadata),
            chunks=chunks,
            metadata=metadata.__dict__,
            language=self.lang,
        )

    def create_searchable_chunks(
        self, content: str, file_path: Optional[str | Path] = None
    ) -> List[document_contracts.DocumentChunk]:
        """Create document chunks that conform to the shared contract."""
        resolved_path = Path(file_path or f"memory.{self.lang}")
        raw_chunks = self.chunk_document(content, resolved_path)
        total_chunks = len(raw_chunks)

        return [
            self._to_contract_chunk(chunk, str(resolved_path), total_chunks) for chunk in raw_chunks
        ]

    def _to_contract_chunk(
        self, chunk: DocumentChunk, document_path: str, total_chunks: int
    ) -> document_contracts.DocumentChunk:
        """Convert internal chunk objects to the shared interface type."""
        metadata = document_contracts.ChunkMetadata(
            document_path=document_path,
            section_hierarchy=(
                [chunk.metadata.get("section", "")] if chunk.metadata.get("section") else []
            ),
            chunk_index=chunk.chunk_index,
            total_chunks=total_chunks,
            has_code=bool(chunk.metadata.get("has_code", False)),
            language=chunk.metadata.get("language"),
            keywords=list(chunk.metadata.get("keywords", [])),
            word_count=len(chunk.content.split()),
            line_start=int(chunk.metadata.get("line_start", 0) or 0),
            line_end=int(chunk.metadata.get("line_end", 0) or 0),
        )
        chunk_type = (
            document_contracts.ChunkType.CODE_BLOCK
            if metadata.has_code
            else document_contracts.ChunkType.PARAGRAPH
        )

        return document_contracts.DocumentChunk(
            id=f"{document_path}:chunk:{chunk.chunk_index}",
            content=chunk.content,
            type=chunk_type,
            metadata=metadata,
            context_before=chunk.metadata.get("context_before"),
            context_after=chunk.metadata.get("context_after"),
        )

    def _to_contract_structure(
        self, structure: DocumentStructure, metadata: DocumentMetadata
    ) -> document_contracts.DocumentStructure:
        """Convert internal structure data to the shared interface type."""
        sections = [self._to_contract_section(section) for section in structure.sections]
        return document_contracts.DocumentStructure(
            title=metadata.title,
            sections=sections,
            metadata=structure.metadata,
            outline=sections[0] if sections else None,
        )

    def _to_contract_section(self, section: Dict[str, Any]) -> document_contracts.Section:
        """Convert internal section dictionaries to the shared interface type."""
        return document_contracts.Section(
            id=str(section.get("title", "section")).lower().replace(" ", "-"),
            heading=str(section.get("title", "")),
            level=int(section.get("level", 1) or 1),
            content=str(section.get("content", section.get("title", ""))),
            start_line=int(section.get("start_line", section.get("line", 0)) or 0),
            end_line=int(section.get("end_line", section.get("line", 0)) or 0),
        )

    # Document chunking methods

    def chunk_document(self, content: str, file_path: Path) -> List[DocumentChunk]:
        """Chunk document into overlapping segments optimized for embeddings."""
        # Check cache first
        cache_key = str(file_path)
        if cache_key in self._chunk_cache:
            return self._chunk_cache[cache_key]

        # Extract structure first
        structure = self.extract_structure(content, file_path)

        # Parse to plain text
        plain_text = self.parse_content(content, file_path)

        # Perform intelligent chunking
        chunks = self._intelligent_chunk(plain_text, structure)

        # Cache results
        self._chunk_cache[cache_key] = chunks

        return chunks

    def _intelligent_chunk(self, text: str, structure: DocumentStructure) -> List[DocumentChunk]:
        """Perform structure-aware chunking."""
        chunks = []
        chunk_index = 0

        # Use sections as natural boundaries if available
        if structure.sections:
            for section in structure.sections:
                section_text = text[section["start_pos"] : section["end_pos"]]
                section_chunks = self._chunk_text(
                    section_text,
                    start_offset=section["start_pos"],
                    chunk_index_start=chunk_index,
                )

                # Add section metadata to chunks
                for chunk in section_chunks:
                    chunk.metadata["section"] = section.get("title", "")
                    chunk.metadata["section_level"] = section.get("level", 0)

                chunks.extend(section_chunks)
                chunk_index += len(section_chunks)
        else:
            # Fall back to simple chunking
            chunks = self._chunk_text(text, chunk_index_start=0)

        return chunks

    def _chunk_text(
        self, text: str, start_offset: int = 0, chunk_index_start: int = 0
    ) -> List[DocumentChunk]:
        """Chunk text with overlap, respecting sentence boundaries."""
        chunks = []

        # Normalize whitespace
        text = self._normalize_whitespace(text)

        # Estimate chunk size in characters
        chunk_size_chars = self.chunk_size * self.CHARS_PER_TOKEN
        overlap_chars = self.chunk_overlap * self.CHARS_PER_TOKEN

        # Find sentence boundaries
        sentences = self._split_sentences(text)

        current_chunk = []
        current_size = 0
        chunk_start = 0

        for sent_idx, sentence in enumerate(sentences):
            sent_size = len(sentence)

            if current_size + sent_size > chunk_size_chars and current_chunk:
                # Create chunk
                chunk_text = " ".join(current_chunk)
                chunks.append(
                    DocumentChunk(
                        content=chunk_text,
                        start_pos=start_offset + chunk_start,
                        end_pos=start_offset + chunk_start + len(chunk_text),
                        chunk_index=chunk_index_start + len(chunks),
                        embedding_text=self._optimize_for_embedding(chunk_text),
                    )
                )

                # Calculate overlap
                overlap_size = 0
                overlap_sentences = []
                for i in range(len(current_chunk) - 1, -1, -1):
                    overlap_size += len(current_chunk[i])
                    overlap_sentences.insert(0, current_chunk[i])
                    if overlap_size >= overlap_chars:
                        break

                # Start new chunk with overlap
                current_chunk = overlap_sentences + [sentence]
                current_size = sum(len(s) for s in current_chunk)
                chunk_start += len(chunk_text) - overlap_size
            else:
                current_chunk.append(sentence)
                current_size += sent_size

        # Add final chunk
        if current_chunk:
            chunk_text = " ".join(current_chunk)
            chunks.append(
                DocumentChunk(
                    content=chunk_text,
                    start_pos=start_offset + chunk_start,
                    end_pos=start_offset + chunk_start + len(chunk_text),
                    chunk_index=chunk_index_start + len(chunks),
                    embedding_text=self._optimize_for_embedding(chunk_text),
                )
            )

        return chunks

    # Text processing utilities

    def _normalize_whitespace(self, text: str) -> str:
        """Normalize whitespace and line breaks."""
        # Replace multiple spaces with single space
        text = re.sub(r"\s+", " ", text)

        # Normalize line breaks
        text = re.sub(r"\r\n|\r", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)

        # Remove leading/trailing whitespace
        text = text.strip()

        return text

    def _split_sentences(self, text: str) -> List[str]:
        """Split text into sentences."""
        # Simple sentence splitting (can be enhanced with NLP libraries)
        sentences = re.split(r"(?<=[.!?])\s+", text)

        # Filter empty sentences
        sentences = [s.strip() for s in sentences if s.strip()]

        return sentences

    def _optimize_for_embedding(self, text: str) -> str:
        """Optimize text for embedding generation."""
        # Remove excessive whitespace
        text = " ".join(text.split())

        # Limit length for embedding
        max_chars = 2000  # Reasonable limit for embeddings
        if len(text) > max_chars:
            text = text[:max_chars] + "..."

        return text

    # Document type detection

    def detect_document_type(self, file_path: Path) -> Optional[str]:
        """Detect document type from file extension and content."""
        # First try extension
        ext = file_path.suffix.lower()
        if ext in self.supported_extensions:
            return ext[1:]  # Remove dot

        # Try MIME type
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if mime_type:
            return mime_type.split("/")[-1]

        return None

    # Override plugin interface methods

    def supports(self, path: str | Path) -> bool:
        """Check if this plugin supports the file type."""
        path = Path(path)
        return path.suffix.lower() in self.supported_extensions

    def indexFile(self, path: str | Path, content: str | None = None) -> IndexShard:
        """Index a document file."""
        path = Path(path)
        if content is None:
            content = path.read_text(encoding="utf-8", errors="replace")

        # Extract metadata
        metadata = self.extract_metadata(content, path)

        # Extract structure
        structure = self.extract_structure(content, path)
        self._structure_cache[str(path)] = structure

        # Chunk document
        chunks = self.chunk_document(content, path)

        # Create symbols from chunks and structure
        symbols = []

        # Add document as a symbol
        doc_symbol = {
            "symbol": metadata.title or path.stem,
            "kind": "document",
            "signature": f"Document: {metadata.title or path.name}",
            "line": 1,
            "span": [1, len(content.splitlines())],
            "metadata": metadata.__dict__,
        }
        symbols.append(doc_symbol)

        # Add sections as symbols
        for section in structure.sections:
            section_symbol = {
                "symbol": section.get("title", f"Section {section.get('level', '')}"),
                "kind": "section",
                "signature": section.get("title", ""),
                "line": section.get("line", 1),
                "span": [section.get("start_line", 1), section.get("end_line", 1)],
                "metadata": {
                    "level": section.get("level", 1),
                    "parent": section.get("parent", None),
                },
            }
            symbols.append(section_symbol)

        # Index with semantic indexer if enabled
        if self._enable_semantic and hasattr(self, "_semantic_indexer"):
            self._index_chunks_semantically(str(path), chunks, metadata)

        return {
            "file": str(path),
            "symbols": symbols,
            "language": self.lang,
            "chunks": [chunk.__dict__ for chunk in chunks],
            "metadata": metadata.__dict__,
        }

    def _index_chunks_semantically(
        self, file_path: str, chunks: List[DocumentChunk], metadata: DocumentMetadata
    ):
        """Index document chunks for semantic search with contextual embeddings."""
        # Extract document structure for context
        structure = self._structure_cache.get(file_path)

        for i, chunk in enumerate(chunks):
            # Create a unique identifier for the chunk
            chunk_id = f"{file_path}:chunk:{chunk.chunk_index}"

            # Build contextual embedding text
            contextual_parts = []

            # 1. Document-level context
            if metadata.title:
                contextual_parts.append(f"Document: {metadata.title}")

            if metadata.document_type:
                contextual_parts.append(f"Type: {metadata.document_type}")

            if metadata.tags:
                contextual_parts.append(f"Tags: {', '.join(metadata.tags)}")

            # 2. Section hierarchy context
            section_context = []
            if chunk.metadata.get("section"):
                section_context.append(chunk.metadata["section"])

                # Find parent sections if structure is available
                if structure:
                    for section in structure.sections:
                        if section.get("title") == chunk.metadata["section"]:
                            # Build hierarchy path
                            parent = section.get("parent")
                            hierarchy = [chunk.metadata["section"]]
                            while parent:
                                hierarchy.insert(0, parent)
                                # Find parent's parent
                                for s in structure.sections:
                                    if s.get("title") == parent:
                                        parent = s.get("parent")
                                        break
                                else:
                                    parent = None
                            section_context = hierarchy
                            break

            if section_context:
                contextual_parts.append(f"Section: {' > '.join(section_context)}")

            # 3. Surrounding context from adjacent chunks
            context_before = ""
            context_after = ""

            # Get context from previous chunk (last 100 chars)
            if i > 0:
                prev_chunk = chunks[i - 1]
                context_before = prev_chunk.content[-100:].strip()
                if context_before:
                    contextual_parts.append(f"Previous context: ...{context_before}")

            # Get context from next chunk (first 100 chars)
            if i < len(chunks) - 1:
                next_chunk = chunks[i + 1]
                context_after = next_chunk.content[:100].strip()
                if context_after:
                    contextual_parts.append(f"Following context: {context_after}...")

            # 4. Chunk content (optimized for embedding)
            chunk_content = chunk.embedding_text or chunk.content
            contextual_parts.append(f"Content: {chunk_content}")

            # Combine all contextual information
            contextual_text = "\n\n".join(contextual_parts)

            # Store contextual information in chunk metadata
            chunk.metadata["contextual_text"] = contextual_text
            chunk.metadata["context_before"] = context_before
            chunk.metadata["context_after"] = context_after
            chunk.metadata["section_hierarchy"] = section_context
            chunk.metadata["document_title"] = metadata.title
            chunk.metadata["document_type"] = metadata.document_type
            chunk.metadata["document_tags"] = metadata.tags

            # Index the chunk with contextual embedding
            self.semantic_indexer.index_symbol(
                file=file_path,
                name=chunk_id,
                kind="chunk",
                signature=f"Chunk {chunk.chunk_index} of {metadata.title or Path(file_path).name}",
                line=chunk.chunk_index,
                span=(chunk.start_pos, chunk.end_pos),
                doc=chunk.content[:200],  # First 200 chars as doc
                content=contextual_text,  # Use contextual text for embedding
                metadata={
                    "chunk_id": chunk_id,
                    "contextual_text": contextual_text,
                    "original_content": chunk.content,
                    "section": chunk.metadata.get("section", ""),
                    "section_hierarchy": section_context,
                    "document_title": metadata.title,
                    "document_type": metadata.document_type,
                    "document_tags": metadata.tags,
                    "context_before": context_before,
                    "context_after": context_after,
                    "chunk_metadata": chunk.metadata,
                },
                sqlite_store=self._sqlite_store,
            )

    def getDefinition(self, symbol: str) -> Optional[SymbolDef]:
        """Get definition for a document symbol."""
        # For documents, symbols are document titles or section names
        # Search through cached structures
        for file_path, structure in self._structure_cache.items():
            # Check sections
            for section in structure.sections:
                if section.get("title") == symbol:
                    return {
                        "symbol": symbol,
                        "kind": "section",
                        "language": self.lang,
                        "signature": section.get("title", ""),
                        "doc": None,
                        "defined_in": file_path,
                        "line": section.get("line", 1),
                        "span": (
                            section.get("start_line", 1),
                            section.get("end_line", 1),
                        ),
                    }

        return None

    def findReferences(self, symbol: str) -> List[Reference]:
        """Find references to a symbol in documents."""
        references = []

        # Search through chunks for mentions
        for file_path, chunks in self._chunk_cache.items():
            for chunk in chunks:
                if symbol.lower() in chunk.content.lower():
                    # Simple line number estimation
                    lines_before = chunk.content[
                        : chunk.content.lower().index(symbol.lower())
                    ].count("\n")
                    references.append(
                        Reference(
                            file=file_path,
                            line=chunk.chunk_index * 10 + lines_before,  # Rough estimate
                        )
                    )

        return references

    def search(self, query: str, opts: Optional[Dict] = None) -> List[SearchResult]:
        """Search documents for query with enhanced context."""
        results = []
        opts = opts or {}

        if opts.get("semantic", False) and self.enable_semantic:
            # Use semantic search
            semantic_results = self.semantic_indexer.search(query, limit=opts.get("limit", 20))

            for result in semantic_results:
                if result.get("kind") == "chunk":
                    # Find the chunk
                    file_path = result["file"]
                    chunk_index = result["line"]  # We stored chunk index as line

                    if file_path in self._chunk_cache:
                        chunks = self._chunk_cache[file_path]
                        if chunk_index < len(chunks):
                            chunk = chunks[chunk_index]

                            # Build enhanced result with context
                            search_result = {
                                "file": file_path,
                                "line": chunk_index,
                                "snippet": chunk.content[:200] + "...",
                                "score": result.get("score", 0.0),
                                "metadata": {
                                    "section": chunk.metadata.get("section", ""),
                                    "section_hierarchy": chunk.metadata.get(
                                        "section_hierarchy", []
                                    ),
                                    "document_title": chunk.metadata.get("document_title", ""),
                                    "document_type": chunk.metadata.get("document_type", ""),
                                    "tags": chunk.metadata.get("document_tags", []),
                                    "chunk_index": chunk.chunk_index,
                                    "total_chunks": len(chunks),
                                },
                            }

                            # Add surrounding context if available
                            if chunk.metadata.get("context_before"):
                                search_result["context_before"] = chunk.metadata["context_before"]
                            if chunk.metadata.get("context_after"):
                                search_result["context_after"] = chunk.metadata["context_after"]

                            results.append(search_result)
        else:
            # Full text search through chunks
            query_lower = query.lower()
            for file_path, chunks in self._chunk_cache.items():
                for chunk in chunks:
                    if query_lower in chunk.content.lower():
                        results.append(
                            {
                                "file": file_path,
                                "line": chunk.chunk_index,
                                "snippet": self._extract_snippet(chunk.content, query),
                                "metadata": {
                                    "section": chunk.metadata.get("section", ""),
                                    "chunk_index": chunk.chunk_index,
                                    "total_chunks": len(chunks),
                                },
                            }
                        )

        return results[: opts.get("limit", 20)]

    def _extract_snippet(self, content: str, query: str, context_chars: int = 100) -> str:
        """Extract a snippet around the query match."""
        query_lower = query.lower()
        content_lower = content.lower()

        idx = content_lower.find(query_lower)
        if idx == -1:
            return content[:200] + "..."

        start = max(0, idx - context_chars)
        end = min(len(content), idx + len(query) + context_chars)

        snippet = content[start:end]
        if start > 0:
            snippet = "..." + snippet
        if end < len(content):
            snippet = snippet + "..."

        return snippet

    def invalidate_file(self, file_path: str):
        """Invalidate caches when a file changes."""
        super().invalidate_file(file_path)

        # Clear document-specific caches
        self._structure_cache.pop(file_path, None)
        self._chunk_cache.pop(file_path, None)

    # Helper methods for subclasses

    def _generate_chunk_id(self, file_path: str, chunk_index: int) -> str:
        """Generate a unique ID for a chunk."""
        hash_input = f"{file_path}:{chunk_index}"
        return hashlib.sha256(hash_input.encode()).hexdigest()[:16]

    def _extract_text_around_position(self, content: str, position: int, radius: int = 100) -> str:
        """Extract text around a specific position."""
        start = max(0, position - radius)
        end = min(len(content), position + radius)
        return content[start:end]

    # Abstract methods for specialized plugins (overriding from SpecializedPluginBase)

    def _create_import_resolver(self):
        """Documents don't have imports in the traditional sense."""
        return None

    def _create_type_analyzer(self):
        """Documents don't have type systems."""
        return None

    def _create_build_system(self):
        """Documents don't have build systems."""
        return None

    def _create_cross_file_analyzer(self):
        """Could be implemented for cross-document references."""
        return None
