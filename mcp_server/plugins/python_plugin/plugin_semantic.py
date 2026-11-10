"""Python plugin with semantic search support."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable, Optional, cast

import jedi

try:
    from chunker import chunk_text as chunk_text

    _CHUNKER_AVAILABLE = True
except Exception:
    chunk_text = None  # type: ignore[assignment]
    _CHUNKER_AVAILABLE = False

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
from ...utils.treesitter_wrapper import TreeSitterWrapper

logger = logging.getLogger(__name__)


class PythonPluginSemantic(PluginWithSemanticSearch):
    """Python language plugin with semantic search capabilities."""

    lang = "python"

    def __init__(
        self, sqlite_store: Optional[SQLiteStore] = None, enable_semantic: bool = False
    ) -> None:
        # Initialize enhanced base class
        super().__init__(sqlite_store=sqlite_store, enable_semantic=enable_semantic)
        self._sqlite_store = sqlite_store

        # Initialize Python-specific components
        self._ts = TreeSitterWrapper()
        self._indexer = FuzzyIndexer(sqlite_store=sqlite_store)
        self._repository_id = None

        # Create or get repository if SQLite is enabled
        if self._sqlite_store:
            try:
                self._repository_id = self._sqlite_store.create_repository(
                    str(Path.cwd()), Path.cwd().name, {"language": "python"}
                )
            except Exception as e:
                logger.warning(f"Failed to create repository: {e}")
                self._repository_id = None

        if os.getenv("MCP_SKIP_PLUGIN_PREINDEX", "false").lower() != "true":
            self._preindex()

    def _preindex(self) -> None:
        """Pre-index Python files in the current directory."""
        for path in Path(".").rglob("*.py"):
            try:
                text = path.read_text()
                self._indexer.add_file(str(path), text)
            except Exception:
                continue

    def supports(self, path: str | Path) -> bool:
        """Return True if file extension matches plugin."""
        return Path(path).suffix == ".py"

    def indexFile(self, path: str | Path, content: str) -> IndexShard:
        """Index a Python file with optional semantic embeddings."""
        if isinstance(path, str):
            path = Path(path)

        # Add to fuzzy indexer
        self._indexer.add_file(str(path), content)

        # Parse with tree-sitter
        tree = self._ts._parser.parse(content.encode("utf-8"))
        root = tree.root_node

        # Store file in SQLite if available
        file_id = None
        if self._sqlite_store and self._repository_id:
            import hashlib

            file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            file_id = self._sqlite_store.store_file(
                self._repository_id,
                str(path),
                str(path.relative_to(Path.cwd()) if path.is_absolute() else path),
                language="python",
                size=len(content),
                hash=file_hash,
            )

        symbols: list[dict] = []

        # Extract symbols and documentation
        for child in root.named_children:
            if child.type == "decorated_definition":
                definition = child.child_by_field_name("definition")
                if definition is None:
                    continue
                child = definition
            if child.type not in {"function_definition", "class_definition"}:
                continue

            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            name = name_node.text.decode("utf-8")

            start_line = child.start_point[0] + 1
            end_line = child.end_point[0] + 1

            # Extract docstring
            doc = None
            if child.type == "function_definition":
                kind = "function"
                signature = self._extract_function_signature(child, content)
                doc = self._extract_docstring(child, content)
            else:
                kind = "class"
                signature = f"class {name}:"
                doc = self._extract_docstring(child, content)

            # Store symbol in SQLite if available
            if self._sqlite_store and file_id:
                symbol_id = self._sqlite_store.store_symbol(
                    file_id, name, kind, start_line, end_line, signature=signature
                )
                # Add to fuzzy indexer with metadata
                self._indexer.add_symbol(
                    name,
                    str(path),
                    start_line,
                    {"symbol_id": symbol_id, "file_id": file_id},
                )

            symbols.append(
                {
                    "symbol": name,
                    "kind": kind,
                    "signature": signature,
                    "line": start_line,
                    "end_line": end_line,
                    "span": [start_line, end_line],
                    "doc": doc,
                }
            )

            # Index class methods
            if child.type == "class_definition":
                body = child.child_by_field_name("body")
                if body:
                    for method_node in body.named_children:
                        if method_node.type == "decorated_definition":
                            definition = method_node.child_by_field_name("definition")
                            if definition is None:
                                continue
                            method_node = definition
                        if method_node.type != "function_definition":
                            continue
                        method_name_node = method_node.child_by_field_name("name")
                        if method_name_node is None:
                            continue
                        method_name = method_name_node.text.decode("utf-8")
                        method_start = method_node.start_point[0] + 1
                        method_end = method_node.end_point[0] + 1
                        method_sig = self._extract_function_signature(method_node, content)
                        method_doc = self._extract_docstring(method_node, content)
                        if self._sqlite_store and file_id:
                            method_id = self._sqlite_store.store_symbol(
                                file_id,
                                method_name,
                                "method",
                                method_start,
                                method_end,
                                signature=method_sig,
                                metadata={"parent_class": name},
                            )
                            self._indexer.add_symbol(
                                method_name,
                                str(path),
                                method_start,
                                {"symbol_id": method_id, "file_id": file_id, "parent_class": name},
                            )
                        symbols.append(
                            {
                                "symbol": method_name,
                                "kind": "method",
                                "signature": method_sig,
                                "line": method_start,
                                "end_line": method_end,
                                "span": [method_start, method_end],
                                "doc": method_doc,
                            }
                        )

        # Store chunks in SQLite (delete old ones first since chunk_id uses temp path)
        if self._sqlite_store and file_id:
            _chunks = []
            try:
                _chunks = chunk_text(content, "python")
            except Exception as e:
                logger.error(f"Failed to chunk {path}: {e}")
            self._sqlite_store.delete_chunks_for_file(file_id)
            for i, chunk in enumerate(_chunks):
                try:
                    self._sqlite_store.store_chunk(
                        file_id=file_id,
                        content=chunk.content,
                        content_start=chunk.byte_start,
                        content_end=chunk.byte_end,
                        line_start=chunk.start_line,
                        line_end=chunk.end_line,
                        chunk_id=chunk.chunk_id,
                        node_id=chunk.node_id,
                        treesitter_file_id=str(chunk.file_id),
                        definition_id=chunk.definition_id,
                        parent_chunk_id=chunk.parent_chunk_id,
                        node_type=chunk.node_type,
                        language="python",
                        chunk_index=i,
                        metadata=chunk.metadata,
                    )
                except Exception as e:
                    logger.error(f"Failed to store chunk: {e}")

        # Create semantic embeddings if enabled
        if self._enable_semantic:
            self.index_with_embeddings(path, content, symbols)

        return IndexShard(file=str(path), symbols=symbols, language="python")

    def _extract_function_signature(self, node, content: str) -> str:
        """Extract function signature from AST node."""
        try:
            # Get the function definition line
            content_bytes = content.encode("utf-8")
            colon_pos = content_bytes.find(b":", node.start_byte)
            if colon_pos != -1:
                signature = content_bytes[node.start_byte : colon_pos + 1].decode("utf-8").strip()
                # Clean up multiline signatures
                signature = " ".join(signature.split())
                return signature
        except Exception:
            pass

        # Fallback to simple signature
        name_node = node.child_by_field_name("name")
        if name_node:
            name = name_node.text.decode("utf-8")
            return f"def {name}(...):"
        return "def unknown(...):"

    def _extract_docstring(self, node, content: str) -> Optional[str]:
        """Extract docstring from function or class node."""
        try:
            # Look for the first string literal in the body
            body_node = node.child_by_field_name("body")
            if body_node and body_node.named_child_count > 0:
                first_stmt = body_node.named_children[0]
                if first_stmt.type == "expression_statement":
                    expr = (
                        first_stmt.named_children[0] if first_stmt.named_child_count > 0 else None
                    )
                    if expr and expr.type == "string":
                        # Extract the string content
                        doc_text: str = expr.text.decode("utf-8")
                        # Remove quotes and clean up
                        doc_text = doc_text.strip()
                        if doc_text.startswith('"""') or doc_text.startswith("'''"):
                            doc_text = doc_text[3:-3]
                        elif doc_text.startswith('"') or doc_text.startswith("'"):
                            doc_text = doc_text[1:-1]
                        return doc_text.strip()
        except Exception:
            pass
        return None

    def getDefinition(self, symbol: str) -> SymbolDef | None:
        """Get symbol definition using Jedi."""
        for path in Path(".").rglob("*.py"):
            try:
                source = path.read_text()
                script = jedi.Script(code=source, path=str(path))

                for name in script.get_names(all_scopes=True, definitions=True):
                    if name.name == symbol and name.is_definition():
                        return SymbolDef(
                            symbol=symbol,
                            kind=name.type,
                            language="python",
                            signature=name.description,
                            doc=name.docstring() or None,
                            defined_in=str(path),
                            line=name.line,
                            span=(
                                name.line,
                                (
                                    name.get_definition_end_position()[0]
                                    if hasattr(name, "get_definition_end_position")
                                    else name.line + 5
                                ),
                            ),
                        )
            except Exception:
                continue
        return None

    def findReferences(self, symbol: str) -> list[Reference]:
        """Find all references to a symbol."""
        refs: list[Reference] = []
        seen: set[tuple[str, int]] = set()
        for path in Path(".").rglob("*.py"):
            try:
                source = path.read_text()
                script = jedi.Script(code=source, path=str(path))
                for r in script.get_references():
                    if r.name == symbol:
                        key = (str(path), r.line)
                        if key not in seen:
                            refs.append(Reference(file=str(path), line=r.line))
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
        return cast(Iterable[SearchResult], self._indexer.search(query, limit=limit))

    def get_indexed_count(self) -> int:
        """Return the number of indexed files."""
        if hasattr(self._indexer, "index"):
            return len(self._indexer.index)
        return 0
