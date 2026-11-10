"""JavaScript plugin with semantic search support."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional

from tree_sitter import Node

from ...plugin_base import (
    IndexShard,
    Reference,
    SearchOpts,
    SearchResult,
    SymbolDef,
)
from ...plugin_base_enhanced import PluginWithSemanticSearch
from ...utils.fuzzy_indexer import FuzzyIndexer

if TYPE_CHECKING:
    from ...storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class JsPluginSemantic(PluginWithSemanticSearch):
    """JavaScript/TypeScript plugin with semantic search capabilities."""

    lang = "js"

    def __init__(
        self, sqlite_store: Optional["SQLiteStore"] = None, enable_semantic: bool = False
    ) -> None:
        # Initialize enhanced base class
        super().__init__(enable_semantic=enable_semantic)

        # Initialize JS-specific components
        self._indexer = FuzzyIndexer()
        self._repository_id = None

        # Initialize parser without tree-sitter for now
        self.parser = None
        self.language = None

        if os.getenv("MCP_SKIP_PLUGIN_PREINDEX", "false").lower() != "true":
            self._preindex()

    def bind(self, ctx) -> None:
        super().bind(ctx)
        if self._ctx.sqlite_store and not self._repository_id:
            try:
                self._repository_id = self._ctx.sqlite_store.create_repository(
                    str(Path.cwd()), Path.cwd().name, {"language": "javascript"}
                )
            except Exception as e:
                logger.warning(f"Failed to create repository: {e}")
                self._repository_id = None

    def _preindex(self) -> None:
        """Pre-index JavaScript/TypeScript files in the current directory."""
        extensions = [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"]
        for ext in extensions:
            for path in Path(".").rglob(f"*{ext}"):
                try:
                    text = path.read_text()
                    self._indexer.add_file(str(path), text)
                except Exception:
                    continue

    def supports(self, path: str | Path) -> bool:
        """Return True if file extension matches JavaScript/TypeScript."""
        extensions = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
        return Path(path).suffix in extensions

    def indexFile(self, path: str | Path, content: str) -> IndexShard:
        """Index a JavaScript/TypeScript file with optional semantic embeddings."""
        if isinstance(path, str):
            path = Path(path)

        # Add to fuzzy indexer
        self._indexer.add_file(str(path), content)

        # Store file in SQLite if available
        file_id = None
        if (self._ctx.sqlite_store if self._ctx else None) and self._repository_id:
            import hashlib

            file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            file_id = (self._ctx.sqlite_store if self._ctx else None).store_file(
                self._repository_id,
                str(path),
                str(path.relative_to(Path.cwd()) if path.is_absolute() else path),
                language="javascript",
                size=len(content),
                hash=file_hash,
            )

        # Extract symbols without tree-sitter for now
        symbols = self._extract_symbols_simple(content, file_id)

        # Create semantic embeddings if enabled
        if self._enable_semantic and symbols:
            self.index_with_embeddings(path, content, symbols)

        return IndexShard(file=str(path), symbols=symbols, language="javascript")

    def _extract_symbols_simple(self, content: str, file_id: Optional[int] = None) -> List[Dict]:
        """Simple symbol extraction without tree-sitter."""
        symbols = []
        lines = content.split("\n")

        for i, line in enumerate(lines):
            # Function declarations
            if "function" in line and "(" in line:
                import re

                match = re.search(r"function\s+(\w+)\s*\(", line)
                if match:
                    name = match.group(1)
                    symbols.append(
                        {
                            "symbol": name,
                            "kind": "function",
                            "signature": line.strip(),
                            "line": i + 1,
                            "end_line": i + 1,
                            "span": [i + 1, i + 1],
                        }
                    )

            # Class declarations
            elif "class" in line:
                match = re.search(r"class\s+(\w+)", line)
                if match:
                    name = match.group(1)
                    symbols.append(
                        {
                            "symbol": name,
                            "kind": "class",
                            "signature": line.strip(),
                            "line": i + 1,
                            "end_line": i + 1,
                            "span": [i + 1, i + 1],
                        }
                    )

            # Arrow functions and const declarations
            elif "const" in line and "=>" in line:
                match = re.search(r"const\s+(\w+)\s*=.*=>", line)
                if match:
                    name = match.group(1)
                    symbols.append(
                        {
                            "symbol": name,
                            "kind": "function",
                            "signature": line.strip(),
                            "line": i + 1,
                            "end_line": i + 1,
                            "span": [i + 1, i + 1],
                        }
                    )

        return symbols

    def _extract_symbols(
        self,
        node: Node,
        content: str,
        symbols: List[Dict],
        imports: List[Dict],
        exports: List[Dict],
        file_id: Optional[int] = None,
        scope_path: List[str] = None,
        path: Optional[Path] = None,
    ) -> None:
        """Recursively extract symbols from the AST."""
        if scope_path is None:
            scope_path = []

        # Extract imports and exports at the top level
        if not scope_path:
            imports.extend(self._extract_imports(node, content))
            exports.extend(self._extract_exports(node, content))

        # Function declarations
        if node.type in ["function_declaration", "function"]:
            name_node = node.child_by_field_name("name")
            if name_node:
                name = content[name_node.start_byte : name_node.end_byte]
                params = self._extract_parameters(node, content)
                is_async = self._is_async_function(node, content)
                is_generator = self._is_generator_function(node, content)
                doc = self._extract_jsdoc(node, content)

                kind = "function"
                if is_generator:
                    kind = "generator"

                signature = f"{'async ' if is_async else ''}function{'*' if is_generator else ''} {name}({params})"

                symbol_info = {
                    "symbol": name,
                    "kind": kind,
                    "signature": signature,
                    "line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "span": [node.start_point[0] + 1, node.end_point[0] + 1],
                    "scope": ".".join(scope_path) if scope_path else None,
                    "doc": doc,
                }
                symbols.append(symbol_info)

                # Store in SQLite if available
                if (self._ctx.sqlite_store if self._ctx else None) and file_id:
                    symbol_id = (self._ctx.sqlite_store if self._ctx else None).store_symbol(
                        file_id,
                        name,
                        kind,
                        symbol_info["line"],
                        symbol_info["end_line"],
                        signature=signature,
                    )
                    if path:
                        self._indexer.add_symbol(
                            name,
                            str(path),
                            symbol_info["line"],
                            {"symbol_id": symbol_id, "file_id": file_id},
                        )

        # Class declarations
        elif node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = content[name_node.start_byte : name_node.end_byte]
                doc = self._extract_jsdoc(node, content)

                symbol_info = {
                    "symbol": name,
                    "kind": "class",
                    "signature": f"class {name}",
                    "line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "span": [node.start_point[0] + 1, node.end_point[0] + 1],
                    "scope": ".".join(scope_path) if scope_path else None,
                    "doc": doc,
                }
                symbols.append(symbol_info)

                # Process class body
                body_node = node.child_by_field_name("body")
                if body_node:
                    self._extract_symbols(
                        body_node,
                        content,
                        symbols,
                        imports,
                        exports,
                        file_id,
                        scope_path + [name],
                        path,
                    )

        # Arrow functions and variables
        elif node.type == "variable_declaration":
            declarators = [child for child in node.children if child.type == "variable_declarator"]
            for declarator in declarators:
                name_node = declarator.child_by_field_name("name")
                value_node = declarator.child_by_field_name("value")

                if name_node and value_node and value_node.type == "arrow_function":
                    name = content[name_node.start_byte : name_node.end_byte]
                    params = self._extract_parameters(value_node, content)
                    is_async = self._is_async_function(value_node, content)
                    doc = self._extract_jsdoc(node, content)

                    signature = f"{'async ' if is_async else ''}const {name} = ({params}) => {{}}"

                    symbol_info = {
                        "symbol": name,
                        "kind": "function",
                        "signature": signature,
                        "line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                        "span": [node.start_point[0] + 1, node.end_point[0] + 1],
                        "scope": ".".join(scope_path) if scope_path else None,
                        "doc": doc,
                    }
                    symbols.append(symbol_info)

        # Recurse through children
        for child in node.children:
            self._extract_symbols(
                child, content, symbols, imports, exports, file_id, scope_path, path
            )

    def _extract_imports(self, node: Node, content: str) -> List[Dict]:
        """Extract import statements."""
        imports = []
        if node.type == "import_statement":
            source_node = node.child_by_field_name("source")
            if source_node:
                module = content[source_node.start_byte : source_node.end_byte].strip("\"'")
                imports.append({"module": module})

        for child in node.children:
            imports.extend(self._extract_imports(child, content))

        return imports

    def _extract_exports(self, node: Node, content: str) -> List[Dict]:
        """Extract export statements."""
        exports = []
        if node.type in ["export_statement", "export_default_declaration"]:
            # Simplified export extraction
            exports.append({"name": "export", "kind": "export", "line": node.start_point[0] + 1})

        for child in node.children:
            exports.extend(self._extract_exports(child, content))

        return exports

    def _extract_parameters(self, node: Node, content: str) -> str:
        """Extract function parameters."""
        params_node = node.child_by_field_name("parameters")
        if params_node:
            return content[params_node.start_byte : params_node.end_byte].strip("()")
        return ""

    def _is_async_function(self, node: Node, content: str) -> bool:
        """Check if function is async."""
        # Check for async keyword before function
        start = node.start_byte
        prefix = content[max(0, start - 10) : start].strip()
        return prefix.endswith("async")

    def _is_generator_function(self, node: Node, content: str) -> bool:
        """Check if function is a generator."""
        # Look for * after function keyword
        for child in node.children:
            if child.type == "*":
                return True
        return False

    def _extract_jsdoc(self, node: Node, content: str) -> Optional[str]:
        """Extract JSDoc comment preceding the node."""
        # Look for comment nodes before this node
        start_line = node.start_point[0]
        lines = content.split("\n")

        # Check previous lines for JSDoc
        for i in range(start_line - 1, max(0, start_line - 5), -1):
            line = lines[i].strip()
            if line.startswith("/**"):
                # Found JSDoc start, extract until */
                doc_lines = []
                for j in range(i, start_line):
                    doc_lines.append(lines[j])
                    if lines[j].strip().endswith("*/"):
                        break

                # Clean up JSDoc
                doc = "\n".join(doc_lines)
                doc = doc.replace("/**", "").replace("*/", "")
                doc = "\n".join(line.strip().lstrip("*").strip() for line in doc.split("\n"))
                return doc.strip()

        return None

    def getDefinition(self, symbol: str) -> SymbolDef | None:
        """Get symbol definition."""
        # Search through indexed files
        extensions = [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"]
        for ext in extensions:
            for path in Path(".").rglob(f"*{ext}"):
                try:
                    content = path.read_text()

                    # Simple search for the symbol
                    if symbol in content:
                        lines = content.split("\n")
                        for i, line in enumerate(lines):
                            if symbol in line and (
                                "function" in line or "class" in line or "const" in line
                            ):
                                return SymbolDef(
                                    symbol=symbol,
                                    kind=(
                                        "function"
                                        if "function" in line
                                        else "class" if "class" in line else "variable"
                                    ),
                                    language="javascript",
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

        extensions = [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"]
        for ext in extensions:
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
