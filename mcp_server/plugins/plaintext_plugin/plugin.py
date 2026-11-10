"""Plain Text Plugin for natural language document processing."""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp_server.document_processing.base_document_plugin import (
    BaseDocumentPlugin,
    DocumentChunk,
    DocumentMetadata,
    DocumentStructure,
)
from mcp_server.plugin_base import SearchResult
from mcp_server.storage.sqlite_store import SQLiteStore

from .nlp_processor import NLPProcessor, TextAnalysis, TextType
from .paragraph_detector import Paragraph, ParagraphDetector
from .sentence_splitter import SentenceSplitter
from .topic_extractor import TopicExtractor

logger = logging.getLogger(__name__)


class PlainTextPlugin(BaseDocumentPlugin):
    """Plugin for processing plain text documents with NLP capabilities."""

    def __init__(
        self,
        language_config: Dict[str, Any],
        sqlite_store: Optional[SQLiteStore] = None,
        enable_semantic: bool = False,
        chunk_size: int = BaseDocumentPlugin.DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = BaseDocumentPlugin.DEFAULT_CHUNK_OVERLAP,
    ):
        """Initialize plain text plugin with NLP processors."""
        super().__init__(language_config, sqlite_store, enable_semantic, chunk_size, chunk_overlap)

        # Initialize NLP components
        self.nlp_processor = NLPProcessor()
        self.paragraph_detector = ParagraphDetector()
        self.sentence_splitter = SentenceSplitter()
        self.topic_extractor = TopicExtractor()

        # Cache for text analysis
        self._analysis_cache: Dict[str, TextAnalysis] = {}

        # Text type specific processors
        self._type_processors = {
            TextType.TECHNICAL: self._process_technical_text,
            TextType.NARRATIVE: self._process_narrative_text,
            TextType.INSTRUCTIONAL: self._process_instructional_text,
            TextType.CONVERSATIONAL: self._process_conversational_text,
            TextType.MIXED: self._process_mixed_text,
        }

    def _get_supported_extensions(self) -> List[str]:
        """Get list of supported file extensions."""
        return [".txt", ".text", ".md", ".markdown", ".rst", ".log", ".readme"]

    def extract_structure(self, content: str, file_path: Path) -> DocumentStructure:
        """Extract document structure using NLP analysis."""
        # Detect paragraphs
        paragraphs = self.paragraph_detector.detect_paragraphs(content)

        # Find headings and sections
        sections = []
        headings = []
        current_section = None
        section_hierarchy = []

        for i, para in enumerate(paragraphs):
            # Check if paragraph is a heading
            if self._is_heading_paragraph(para):
                heading_level = self._determine_heading_level(para.text)

                heading = {
                    "text": para.text.strip(),
                    "level": heading_level,
                    "position": para.start_line,
                    "line": para.start_line + 1,
                }
                headings.append(heading)

                # Create section
                if current_section:
                    current_section["end_pos"] = para.start_line - 1
                    current_section["end_line"] = para.start_line
                    sections.append(current_section)

                current_section = {
                    "title": para.text.strip(),
                    "level": heading_level,
                    "start_pos": para.start_line,
                    "end_pos": None,
                    "start_line": para.start_line + 1,
                    "end_line": None,
                    "line": para.start_line + 1,
                }

                # Update hierarchy
                section_hierarchy = section_hierarchy[: heading_level - 1]
                if section_hierarchy:
                    current_section["parent"] = section_hierarchy[-1]
                section_hierarchy.append(para.text.strip())

        # Close last section
        if current_section:
            lines = content.split("\n")
            current_section["end_pos"] = len(lines) - 1
            current_section["end_line"] = len(lines)
            sections.append(current_section)

        # Build outline
        outline = self._build_outline(headings)

        # Extract metadata from content
        metadata_dict = self._extract_inline_metadata(content)

        return DocumentStructure(
            sections=sections,
            headings=headings,
            metadata=metadata_dict,
            outline=outline,
        )

    def extract_metadata(self, content: str, file_path: Path) -> DocumentMetadata:
        """Extract document metadata using NLP analysis."""
        # Perform text analysis
        analysis = self.nlp_processor.analyze_text(content)
        self._analysis_cache[str(file_path)] = analysis

        # Extract title (first heading or first line)
        title = None
        lines = content.split("\n")
        for line in lines[:10]:  # Check first 10 lines
            line = line.strip()
            if line and not line.startswith("#"):
                title = line
                break
            elif line.startswith("#"):
                title = line.lstrip("#").strip()
                break

        # Extract inline metadata
        inline_metadata = self._extract_inline_metadata(content)

        # Determine language
        language = self._detect_language(content)

        # Extract keywords as tags
        keywords = [kw for kw, _ in self.topic_extractor.extract_keywords(content, max_keywords=10)]

        return DocumentMetadata(
            title=title or file_path.stem,
            author=inline_metadata.get("author"),
            created_date=inline_metadata.get("date"),
            modified_date=None,  # Would need file system info
            document_type=analysis.text_type.value,
            language=language,
            tags=keywords,
            custom={
                "readability_score": analysis.readability_score,
                "avg_sentence_length": analysis.avg_sentence_length,
                "vocabulary_richness": analysis.vocabulary_richness,
                "topics": [{"keywords": t.keywords, "score": t.score} for t in analysis.topics[:3]],
            },
        )

    def parse_content(self, content: str, file_path: Path) -> str:
        """Parse and clean plain text content."""
        # Normalize line endings
        content = content.replace("\r\n", "\n").replace("\r", "\n")

        # Remove excessive blank lines
        content = re.sub(r"\n{3,}", "\n\n", content)

        # Fix common encoding issues
        content = self._fix_encoding_issues(content)

        # Handle special text formats
        if file_path.suffix.lower() in [".md", ".markdown"]:
            content = self._process_markdown(content)
        elif file_path.suffix.lower() == ".rst":
            content = self._process_restructured_text(content)

        return content.strip()

    def _intelligent_chunk(self, text: str, structure: DocumentStructure) -> List[DocumentChunk]:
        """Perform NLP-aware intelligent chunking."""
        chunks = []

        # Get text analysis from cache or perform new analysis
        analysis = None
        for cached_path, cached_analysis in self._analysis_cache.items():
            if cached_path in str(structure):
                analysis = cached_analysis
                break

        if not analysis:
            analysis = self.nlp_processor.analyze_text(text)

        # Use NLP processor for semantic chunking
        semantic_chunks = self.nlp_processor.extract_semantic_chunks(
            text, target_size=self.chunk_size * self.CHARS_PER_TOKEN
        )

        # Convert to DocumentChunk objects
        current_pos = 0
        for i, chunk_text in enumerate(semantic_chunks):
            # Find chunk position in original text
            chunk_start = text.find(chunk_text, current_pos)
            if chunk_start == -1:
                chunk_start = current_pos

            chunk_end = chunk_start + len(chunk_text)

            # Extract relevant metadata
            chunk_metadata = {
                "text_type": analysis.text_type.value,
                "chunk_topics": self._find_chunk_topics(chunk_text, analysis.topics),
            }

            # Find which section this chunk belongs to
            for section in structure.sections:
                if chunk_start >= section.get("start_pos", 0) and chunk_end <= section.get(
                    "end_pos", len(text)
                ):
                    chunk_metadata["section"] = section.get("title", "")
                    chunk_metadata["section_level"] = section.get("level", 0)
                    break

            # Create optimized embedding text
            embedding_text = self._create_embedding_text(chunk_text, chunk_metadata, analysis)

            chunks.append(
                DocumentChunk(
                    content=chunk_text,
                    start_pos=chunk_start,
                    end_pos=chunk_end,
                    chunk_index=i,
                    metadata=chunk_metadata,
                    embedding_text=embedding_text,
                )
            )

            current_pos = chunk_end

        return chunks

    def search(self, query: str, opts: Optional[Dict] = None) -> List[SearchResult]:
        """Enhanced search with NLP understanding."""
        opts = opts or {}
        results = []

        # Analyze query for better understanding
        query_keywords = [
            kw for kw, _ in self.topic_extractor.extract_keywords(query, max_keywords=5)
        ]

        if opts.get("semantic", False) and self.enable_semantic:
            # Semantic search with query expansion
            expanded_query = self._expand_query(query, query_keywords)
            semantic_results = self.semantic_indexer.search(
                expanded_query, limit=opts.get("limit", 20)
            )

            for result in semantic_results:
                if result.get("kind") == "chunk":
                    file_path = result["file"]
                    chunk_index = result["line"]

                    if file_path in self._chunk_cache:
                        chunks = self._chunk_cache[file_path]
                        if chunk_index < len(chunks):
                            chunk = chunks[chunk_index]

                            # Calculate relevance score
                            relevance = self._calculate_relevance(
                                query, chunk.content, query_keywords
                            )

                            results.append(
                                {
                                    "file": file_path,
                                    "line": chunk_index,
                                    "snippet": self._create_contextual_snippet(
                                        chunk.content, query
                                    ),
                                    "relevance": relevance,
                                    "metadata": chunk.metadata,
                                }
                            )

            # Sort by relevance
            results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
        else:
            # Enhanced full-text search
            results = self._enhanced_text_search(query, query_keywords, opts)

        return results[: opts.get("limit", 20)]

    # Helper methods

    def _is_heading_paragraph(self, para: Paragraph) -> bool:
        """Determine if a paragraph is likely a heading."""
        text = para.text.strip()

        # Markdown headings
        if text.startswith("#"):
            return True

        # Short, no punctuation at end
        if (
            len(text.split()) <= 10
            and not text.endswith((".", "!", "?", ":"))
            and text
            and text[0].isupper()
        ):
            return True

        # All caps
        if text.isupper() and len(text.split()) <= 10:
            return True

        # Numbered headings
        if re.match(r"^\d+\.?\s+[A-Z]", text):
            return True

        return False

    def _determine_heading_level(self, text: str) -> int:
        """Determine heading level from text."""
        text = text.strip()

        # Markdown style
        if text.startswith("#"):
            return min(text.count("#"), 6)

        # Numbered style (1., 1.1., etc.)
        match = re.match(r"^(\d+(?:\.\d+)*)", text)
        if match:
            return len(match.group(1).split("."))

        # All caps = level 1, Title Case = level 2, otherwise level 3
        if text.isupper():
            return 1
        elif text.istitle():
            return 2
        else:
            return 3

    def _build_outline(self, headings: List[Dict]) -> List[Dict]:
        """Build hierarchical outline from headings."""
        outline = []
        stack = []

        for heading in headings:
            level = heading["level"]

            # Pop stack to current level
            while stack and stack[-1]["level"] >= level:
                stack.pop()

            # Create outline entry
            entry = {
                "title": heading["text"],
                "level": level,
                "line": heading["line"],
                "children": [],
            }

            # Add to parent or root
            if stack:
                stack[-1]["children"].append(entry)
            else:
                outline.append(entry)

            stack.append(entry)

        return outline

    def _extract_inline_metadata(self, content: str) -> Dict[str, Any]:
        """Extract metadata from text content."""
        metadata = {}

        # Look for common metadata patterns
        patterns = {
            "author": r"(?:Author|By|Written by):\s*(.+?)(?:\n|$)",
            "date": r"(?:Date|Created|Updated):\s*(.+?)(?:\n|$)",
            "version": r"(?:Version|v):\s*(.+?)(?:\n|$)",
            "copyright": r"(?:Copyright|©)\s*(.+?)(?:\n|$)",
        }

        for key, pattern in patterns.items():
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                metadata[key] = match.group(1).strip()

        return metadata

    def _detect_language(self, content: str) -> str:
        """Simple language detection based on common words."""
        # This is a very simple implementation
        # In production, you'd use a proper language detection library

        # Check for common English words
        english_words = {
            "the",
            "is",
            "are",
            "was",
            "were",
            "have",
            "has",
            "and",
            "or",
            "but",
        }
        words = set(content.lower().split())
        english_count = len(words & english_words)

        if english_count > 5:
            return "en"

        return "unknown"

    def _fix_encoding_issues(self, content: str) -> str:
        """Fix common encoding issues in text."""
        replacements = {
            '"': '"',
            '"': '"',
            """: "'", """: "'",
            "—": "--",
            "–": "-",
            "…": "...",
            "\u00a0": " ",  # Non-breaking space
            "\u200b": "",  # Zero-width space
        }

        for old, new in replacements.items():
            content = content.replace(old, new)

        return content

    def _process_markdown(self, content: str) -> str:
        """Process markdown-specific elements."""
        # Remove markdown syntax for plain text processing
        # Keep the structure but remove formatting

        # Remove code blocks but keep content
        content = re.sub(r"```[^\n]*\n(.*?)\n```", r"\1", content, flags=re.DOTALL)

        # Remove inline code but keep content
        content = re.sub(r"`([^`]+)`", r"\1", content)

        # Remove emphasis but keep text
        content = re.sub(r"\*\*([^*]+)\*\*", r"\1", content)
        content = re.sub(r"\*([^*]+)\*", r"\1", content)
        content = re.sub(r"__([^_]+)__", r"\1", content)
        content = re.sub(r"_([^_]+)_", r"\1", content)

        # Convert links to text
        content = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", content)

        return content

    def _process_restructured_text(self, content: str) -> str:
        """Process reStructuredText elements."""
        # Basic RST processing

        # Remove directive syntax
        content = re.sub(r"\.\. [^:]+::", "", content)

        # Remove role syntax
        content = re.sub(r":[^:]+:`([^`]+)`", r"\1", content)

        return content

    def _find_chunk_topics(self, chunk_text: str, document_topics: List) -> List[str]:
        """Find which topics are present in a chunk."""
        chunk_topics = []
        chunk_lower = chunk_text.lower()

        for topic in document_topics[:5]:  # Check top 5 topics
            # Check if topic keywords appear in chunk
            keyword_count = sum(1 for kw in topic.keywords if kw.lower() in chunk_lower)
            if keyword_count >= 2:  # At least 2 keywords
                chunk_topics.append(topic.keywords[0])  # Use primary keyword

        return chunk_topics

    def _create_embedding_text(
        self, chunk_text: str, metadata: Dict, analysis: TextAnalysis
    ) -> str:
        """Create optimized text for embedding generation."""
        parts = []

        # Add document type context
        parts.append(f"Text type: {analysis.text_type.value}")

        # Add section context if available
        if metadata.get("section"):
            parts.append(f"Section: {metadata['section']}")

        # Add topic context
        if metadata.get("chunk_topics"):
            parts.append(f"Topics: {', '.join(metadata['chunk_topics'])}")

        # Add the actual content
        parts.append(chunk_text)

        # Join with newlines
        embedding_text = "\n".join(parts)

        # Limit length
        max_chars = 2000
        if len(embedding_text) > max_chars:
            embedding_text = embedding_text[:max_chars] + "..."

        return embedding_text

    def _expand_query(self, query: str, keywords: List[str]) -> str:
        """Expand query with related terms."""
        # Simple query expansion
        expanded_parts = [query]

        # Add keywords if not already in query
        query_lower = query.lower()
        for keyword in keywords:
            if keyword.lower() not in query_lower:
                expanded_parts.append(keyword)

        return " ".join(expanded_parts)

    def _calculate_relevance(self, query: str, content: str, query_keywords: List[str]) -> float:
        """Calculate relevance score for search result."""
        score = 0.0
        content_lower = content.lower()
        query_lower = query.lower()

        # Exact query match
        if query_lower in content_lower:
            score += 1.0

        # Keyword matches
        for keyword in query_keywords:
            if keyword.lower() in content_lower:
                score += 0.5

        # Proximity of keywords
        if len(query_keywords) > 1:
            # Check if keywords appear near each other
            positions = []
            for keyword in query_keywords:
                pos = content_lower.find(keyword.lower())
                if pos != -1:
                    positions.append(pos)

            if len(positions) > 1:
                positions.sort()
                avg_distance = sum(
                    positions[i + 1] - positions[i] for i in range(len(positions) - 1)
                ) / (len(positions) - 1)
                if avg_distance < 100:  # Within 100 chars
                    score += 0.5

        return min(score, 2.0)  # Cap at 2.0

    def _create_contextual_snippet(self, content: str, query: str, max_length: int = 200) -> str:
        """Create a contextual snippet highlighting query terms."""
        query_lower = query.lower()
        content_lower = content.lower()

        # Find best position for snippet
        pos = content_lower.find(query_lower)
        if pos == -1:
            # Find first keyword
            keywords = query_lower.split()
            for keyword in keywords:
                pos = content_lower.find(keyword)
                if pos != -1:
                    break

        if pos == -1:
            # Just return beginning
            return content[:max_length] + "..." if len(content) > max_length else content

        # Extract context around match
        start = max(0, pos - max_length // 2)
        end = min(len(content), pos + max_length // 2)

        snippet = content[start:end]

        # Add ellipsis
        if start > 0:
            snippet = "..." + snippet
        if end < len(content):
            snippet = snippet + "..."

        return snippet

    def _enhanced_text_search(
        self, query: str, keywords: List[str], opts: Dict
    ) -> List[SearchResult]:
        """Enhanced text search with NLP features."""
        results = []
        query_lower = query.lower()

        for file_path, chunks in self._chunk_cache.items():
            for chunk in chunks:
                content_lower = chunk.content.lower()

                # Check for matches
                if query_lower in content_lower or any(
                    kw.lower() in content_lower for kw in keywords
                ):
                    # Calculate relevance
                    relevance = self._calculate_relevance(query, chunk.content, keywords)

                    results.append(
                        {
                            "file": file_path,
                            "line": chunk.chunk_index,
                            "snippet": self._create_contextual_snippet(chunk.content, query),
                            "relevance": relevance,
                            "metadata": chunk.metadata,
                        }
                    )

        # Sort by relevance
        results.sort(key=lambda x: x.get("relevance", 0), reverse=True)

        return results

    # Text type specific processors

    def _process_technical_text(self, text: str, analysis: TextAnalysis) -> Dict[str, Any]:
        """Special processing for technical documents."""
        return {
            "code_snippets": self._extract_code_snippets(text),
            "technical_terms": self._extract_technical_terms(text),
            "formulas": self._extract_formulas(text),
        }

    def _process_narrative_text(self, text: str, analysis: TextAnalysis) -> Dict[str, Any]:
        """Special processing for narrative text."""
        return {
            "summary": " ".join(analysis.summary_sentences),
            "key_phrases": analysis.key_phrases,
        }

    def _process_instructional_text(self, text: str, analysis: TextAnalysis) -> Dict[str, Any]:
        """Special processing for instructional text."""
        structured = self.nlp_processor.extract_structured_content(text)
        return {
            "steps": structured.get("lists", []),
            "warnings": self._extract_warnings(text),
            "tips": self._extract_tips(text),
        }

    def _process_conversational_text(self, text: str, analysis: TextAnalysis) -> Dict[str, Any]:
        """Special processing for conversational text."""
        return {
            "questions": self._extract_questions(text),
            "speakers": self._extract_speakers(text),
        }

    def _process_mixed_text(self, text: str, analysis: TextAnalysis) -> Dict[str, Any]:
        """Processing for mixed content types."""
        return {"content_types": self._identify_content_sections(text)}

    # Extraction helpers

    def _extract_code_snippets(self, text: str) -> List[str]:
        """Extract code snippets from text."""
        snippets = []

        # Indented code blocks
        indented_pattern = re.compile(r"^([ ]{4,}|\t+)(.+)$", re.MULTILINE)
        for match in indented_pattern.finditer(text):
            snippets.append(match.group(2))

        return snippets

    def _extract_technical_terms(self, text: str) -> List[str]:
        """Extract technical terms."""
        return self.topic_extractor._extract_special_terms(text)

    def _extract_formulas(self, text: str) -> List[str]:
        """Extract mathematical formulas."""
        formulas = []

        # Simple pattern for formulas
        formula_pattern = re.compile(r"[A-Za-z]+\s*=\s*[^.!?\n]+")
        for match in formula_pattern.finditer(text):
            formulas.append(match.group().strip())

        return formulas

    def _extract_warnings(self, text: str) -> List[str]:
        """Extract warnings and cautions."""
        warnings = []
        warning_pattern = re.compile(
            r"(?:warning|caution|danger|important|note):\s*(.+?)(?:\n|$)", re.IGNORECASE
        )

        for match in warning_pattern.finditer(text):
            warnings.append(match.group(1).strip())

        return warnings

    def _extract_tips(self, text: str) -> List[str]:
        """Extract tips and hints."""
        tips = []
        tip_pattern = re.compile(r"(?:tip|hint|pro tip|suggestion):\s*(.+?)(?:\n|$)", re.IGNORECASE)

        for match in tip_pattern.finditer(text):
            tips.append(match.group(1).strip())

        return tips

    def _extract_questions(self, text: str) -> List[str]:
        """Extract questions from text."""
        sentences = self.sentence_splitter.split_sentences(text)
        return [s for s in sentences if s.strip().endswith("?")]

    def _extract_speakers(self, text: str) -> List[str]:
        """Extract speaker names from conversational text."""
        speakers = set()

        # Pattern for dialogue attribution
        speaker_pattern = re.compile(r"^([A-Z][A-Za-z\s]+):\s*", re.MULTILINE)
        for match in speaker_pattern.finditer(text):
            speakers.add(match.group(1).strip())

        return list(speakers)

    def _identify_content_sections(self, text: str) -> List[Dict[str, Any]]:
        """Identify different types of content sections."""
        sections = []
        paragraphs = self.paragraph_detector.detect_paragraphs(text)

        for para in paragraphs:
            if para.is_code_block:
                section_type = "code"
            elif para.is_list_item:
                section_type = "list"
            elif self._is_heading_paragraph(para):
                section_type = "heading"
            elif "?" in para.text:
                section_type = "question"
            else:
                section_type = "text"

            sections.append(
                {
                    "type": section_type,
                    "start_line": para.start_line,
                    "end_line": para.end_line,
                    "preview": (para.text[:100] + "..." if len(para.text) > 100 else para.text),
                }
            )

        return sections
