"""HTML/CSS plugin with semantic search support."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ...plugin_base import (
    IndexShard,
    Reference,
    SearchOpts,
    SearchResult,
    SymbolDef,
)
from ...plugin_base_enhanced import PluginWithSemanticSearch
from ...storage.sqlite_store import SQLiteStore
from ...utils.fuzzy_indexer import FuzzyIndexer

logger = logging.getLogger(__name__)


class HtmlCssPluginSemantic(PluginWithSemanticSearch):
    """HTML/CSS plugin with semantic search capabilities."""

    lang = "html_css"

    def __init__(
        self, sqlite_store: Optional[SQLiteStore] = None, enable_semantic: bool = False
    ) -> None:
        # Initialize enhanced base class
        super().__init__(sqlite_store=sqlite_store, enable_semantic=enable_semantic)

        # Initialize language-specific components
        self._indexer = FuzzyIndexer(sqlite_store=sqlite_store)
        self._repository_id = None

        # Create or get repository if SQLite is enabled
        if self._sqlite_store:
            try:
                self._repository_id = self._sqlite_store.create_repository(
                    str(Path.cwd()), Path.cwd().name, {"language": "html_css"}
                )
            except Exception as e:
                logger.warning(f"Failed to create repository: {e}")
                self._repository_id = None

        if os.getenv("MCP_SKIP_PLUGIN_PREINDEX", "false").lower() != "true":
            self._preindex()

    def _preindex(self) -> None:
        """Pre-index HTML/CSS files in the current directory."""
        for ext in self._get_extensions():
            for path in Path(".").rglob(f"*{ext}"):
                try:
                    text = path.read_text()
                    self._indexer.add_file(str(path), text)
                except Exception:
                    continue

    def _get_extensions(self) -> List[str]:
        """Get file extensions for this language."""
        return [".html", ".htm", ".css", ".scss", ".sass", ".less"]

    def supports(self, path: str | Path) -> bool:
        """Return True if file extension matches HTML/CSS."""
        return Path(path).suffix in self._get_extensions()

    def indexFile(self, path: str | Path, content: str) -> IndexShard:
        """Index a HTML/CSS file with optional semantic embeddings."""
        if isinstance(path, str):
            path = Path(path)

        # Add to fuzzy indexer
        self._indexer.add_file(str(path), content)

        # Store file in SQLite if available
        file_id = None
        if self._sqlite_store and self._repository_id:
            import hashlib

            file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            file_id = self._sqlite_store.store_file(
                self._repository_id,
                str(path),
                str(path.relative_to(Path.cwd()) if path.is_absolute() else path),
                language="html_css",
                size=len(content),
                hash=file_hash,
            )

        # Extract symbols (simplified for now)
        symbols = self._extract_symbols(content, file_id)

        # Create semantic embeddings if enabled
        if self._enable_semantic and symbols:
            self.index_with_embeddings(path, content, symbols)

        return IndexShard(file=str(path), symbols=symbols, language="html_css")

    def _extract_symbols(self, content: str, file_id: Optional[int] = None) -> List[Dict]:
        """Extract symbols from HTML/CSS code."""
        symbols = []
        lines = content.split("\n")

        # Basic symbol extraction - override in actual implementation
        for i, line in enumerate(lines):
            # HTML/CSS specific extraction
            # Check if line contains HTML tags
            if "<" in line and ">" in line:
                # Extract HTML IDs and classes
                import re

                for match in re.finditer(r'id=["\']([\w-]+)["\']', line):
                    symbols.append(
                        {
                            "symbol": f"#{match.group(1)}",
                            "kind": "id",
                            "signature": line.strip(),
                            "line": i + 1,
                            "end_line": i + 1,
                            "span": [i + 1, i + 1],
                        }
                    )
                for match in re.finditer(r'class=["\']([\w\s-]+)["\']', line):
                    for cls in match.group(1).split():
                        symbols.append(
                            {
                                "symbol": f".{cls}",
                                "kind": "class",
                                "signature": line.strip(),
                                "line": i + 1,
                                "end_line": i + 1,
                                "span": [i + 1, i + 1],
                            }
                        )
            # Check if line contains CSS selectors
            elif "{" in line:
                # Extract CSS selectors
                stripped = line.strip()
                if stripped and not stripped.startswith("//") and not stripped.startswith("/*"):
                    selector = stripped.split("{")[0].strip()
                    if selector:
                        symbols.append(
                            {
                                "symbol": selector,
                                "kind": "selector",
                                "signature": stripped,
                                "line": i + 1,
                                "end_line": i + 1,
                                "span": [i + 1, i + 1],
                            }
                        )

        return symbols

    def getDefinition(self, symbol: str) -> SymbolDef | None:
        """Get symbol definition."""
        # Simple search through indexed files
        for ext in self._get_extensions():
            for path in Path(".").rglob(f"*{ext}"):
                try:
                    content = path.read_text()
                    if symbol in content:
                        lines = content.split("\n")
                        for i, line in enumerate(lines):
                            if symbol in line:
                                return SymbolDef(
                                    symbol=symbol,
                                    kind="symbol",
                                    language="html_css",
                                    signature=line.strip(),
                                    doc=None,
                                    defined_in=str(path),
                                    line=i + 1,
                                    span=(i + 1, i + 3),
                                )
                except Exception:
                    continue
        return None

    def findReferences(self, symbol: str) -> list[Reference]:
        """Find all references to a symbol."""
        refs: list[Reference] = []
        seen: set[tuple[str, int]] = set()

        for ext in self._get_extensions():
            for path in Path(".").rglob(f"*{ext}"):
                try:
                    content = path.read_text()
                    lines = content.split("\n")

                    for i, line in enumerate(lines):
                        if symbol in line:
                            key = (str(path), i + 1)
                            if key not in seen:
                                refs.append(Reference(file=str(path), line=i + 1))
                                seen.add(key)
                except Exception:
                    continue

        return refs

    def _traditional_search(
        self, query: str, opts: SearchOpts | None = None
    ) -> Iterable[SearchResult]:
        """Traditional fuzzy search implementation."""
        limit = 20
        if opts and "limit" in opts:
            limit = opts["limit"]
        return self._indexer.search(query, limit=limit)

    def get_indexed_count(self) -> int:
        """Return the number of indexed files."""
        if hasattr(self._indexer, "_file_contents"):
            return len(self._indexer._file_contents)
        return 0
