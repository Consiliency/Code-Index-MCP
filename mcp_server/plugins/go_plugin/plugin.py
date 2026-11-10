"""Go plugin for code indexing with enhanced features."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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
from ..generic_treesitter_plugin import GenericTreeSitterPlugin
from .interface_checker import GoInterfaceChecker
from .module_resolver import GoModuleResolver
from .package_analyzer import GoPackageAnalyzer

logger = logging.getLogger(__name__)


class Plugin(PluginWithSemanticSearch):
    """Specialized Go plugin with module resolution and interface checking."""

    lang = "go"

    def __init__(
        self, sqlite_store: Optional[SQLiteStore] = None, enable_semantic: bool = False
    ) -> None:
        """Initialize Go plugin with enhanced features."""
        # Initialize base class
        super().__init__(sqlite_store=sqlite_store, enable_semantic=enable_semantic)
        self._sqlite_store = sqlite_store

        # Initialize Go-specific components
        self._indexer = FuzzyIndexer(sqlite_store=sqlite_store)
        self._repository_id = None

        # Initialize module resolver
        self.module_resolver = GoModuleResolver(Path.cwd())

        # Initialize package analyzer
        self.package_analyzer = GoPackageAnalyzer(self.module_resolver)

        # Initialize interface checker
        self.interface_checker = GoInterfaceChecker(self.package_analyzer)

        # Initialize tree-sitter for Go
        self._init_treesitter()

        # Cache for Go tools integration
        self._go_tools_cache: Dict[str, Any] = {}

        # Create or get repository if SQLite is enabled
        if self._sqlite_store:
            try:
                self._repository_id = self._sqlite_store.create_repository(
                    str(Path.cwd()), Path.cwd().name, {"language": "go"}
                )
            except Exception as e:
                logger.warning(f"Failed to create repository: {e}")
                self._repository_id = None

        # Pre-index existing files
        if os.getenv("MCP_SKIP_PLUGIN_PREINDEX", "false").lower() != "true":
            self._preindex()

    def _init_treesitter(self):
        """Initialize tree-sitter for Go parsing."""
        try:
            # Create a generic tree-sitter plugin for Go
            go_config = {
                "code": "go",
                "name": "Go",
                "extensions": [".go"],
                "symbols": [
                    "function_declaration",
                    "method_declaration",
                    "type_declaration",
                    "interface_declaration",
                ],
                "query": """
                (function_declaration
                  name: (identifier) @function)
                
                (method_declaration
                  name: (field_identifier) @method)
                
                (type_declaration
                  (type_spec
                    name: (type_identifier) @type))
                
                (type_declaration
                  (type_spec
                    name: (type_identifier) @interface
                    type: (interface_type)))
                """,
            }

            self._ts_plugin = GenericTreeSitterPlugin(
                go_config,
                sqlite_store=self._sqlite_store,
                enable_semantic=False,  # We handle semantic ourselves
            )
            logger.info("Initialized tree-sitter for Go")
        except Exception as e:
            logger.warning(f"Failed to initialize tree-sitter for Go: {e}")
            self._ts_plugin = None

    def _preindex(self) -> None:
        """Pre-index Go files in the project."""
        for path in Path(".").rglob("*.go"):
            try:
                text = path.read_text(encoding="utf-8")
                self._indexer.add_file(str(path), text)

                # Analyze package structure
                package_dir = path.parent
                if package_dir not in self.package_analyzer.packages:
                    self.package_analyzer.analyze_package(package_dir)

            except Exception as e:
                logger.error(f"Failed to pre-index {path}: {e}")
                continue

    def supports(self, path: str | Path) -> bool:
        """Check if this plugin supports the given file."""
        return Path(path).suffix == ".go"

    def indexFile(self, path: str | Path, content: str) -> IndexShard:
        """Index a Go file with enhanced analysis."""
        if isinstance(path, str):
            path = Path(path)

        # Add to fuzzy indexer
        self._indexer.add_file(str(path), content)

        # Use tree-sitter for parsing if available
        symbols = []
        if self._ts_plugin:
            try:
                shard = self._ts_plugin.indexFile(path, content)
                # Filter out symbols with empty names (generic fallback produces ''
                # for receiver methods like "func (s *Server) Run()")
                ts_symbols = [s for s in shard["symbols"] if s.get("symbol", "").strip()]
            except Exception as e:
                logger.error(f"Tree-sitter parsing failed for {path}: {e}")
                ts_symbols = []
        else:
            ts_symbols = []

        # Always supplement with Go-specific basic extraction to catch receiver
        # methods that the generic tree-sitter fallback misparses
        basic_symbols = self._extract_symbols_basic(content)
        ts_names = {s["symbol"] for s in ts_symbols}
        for bs in basic_symbols:
            if bs.get("symbol") and bs["symbol"] not in ts_names:
                ts_symbols.append(bs)
        symbols = ts_symbols

        # Enhance symbols with Go-specific information
        symbols = self._enhance_symbols(path, content, symbols)

        # Store file in SQLite if available
        file_id = None
        if self._sqlite_store and self._repository_id:
            import hashlib

            file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            file_id = self._sqlite_store.store_file(
                self._repository_id,
                str(path),
                str(path.relative_to(Path.cwd()) if path.is_absolute() else path),
                language=self.lang,
                size=len(content),
                hash=file_hash,
            )

            # Store symbols
            for symbol in symbols:
                if file_id:
                    try:
                        symbol_id = self._sqlite_store.store_symbol(
                            file_id,
                            symbol["symbol"],
                            symbol["kind"],
                            symbol["line"],
                            symbol.get("end_line", symbol["line"]),
                            signature=symbol.get("signature", ""),
                            metadata=symbol.get("metadata", {}),
                        )
                        # Add to indexer with metadata
                        self._indexer.add_symbol(
                            symbol["symbol"],
                            str(path),
                            symbol["line"],
                            {"symbol_id": symbol_id, "file_id": file_id},
                        )
                    except Exception as e:
                        logger.error(f"Failed to store symbol: {e}")

        # Update package analysis
        package_dir = path.parent
        self.package_analyzer.analyze_package(package_dir)

        # Create semantic embeddings if enabled
        if self._enable_semantic and symbols:
            self.index_with_embeddings(path, content, symbols)

        return IndexShard(file=str(path), symbols=symbols, language=self.lang)

    def _enhance_symbols(self, path: Path, content: str, symbols: List[Dict]) -> List[Dict]:
        """Enhance symbols with Go-specific information."""
        enhanced = []

        for symbol in symbols:
            enhanced_symbol = dict(symbol)

            # Add package information
            package_info = self.package_analyzer.packages.get(str(path.parent))
            if package_info:
                enhanced_symbol["metadata"] = {
                    "package": package_info.name,
                    "imports": list(package_info.imports),
                }

                # Check interface implementations
                if symbol["kind"] in ["type", "struct"]:
                    type_name = symbol["symbol"]
                    if type_name in package_info.types:
                        type_info = package_info.types[type_name]
                        interfaces = self.interface_checker.find_interfaces_for_type(
                            type_info, [package_info]
                        )
                        if interfaces:
                            enhanced_symbol["metadata"]["implements"] = [i.name for i in interfaces]

            enhanced.append(enhanced_symbol)

        return enhanced

    def _extract_symbols_basic(self, content: str) -> List[Dict]:
        """Basic symbol extraction for Go without tree-sitter."""
        symbols = []
        lines = content.split("\n")

        import re

        for i, line in enumerate(lines):
            line = line.strip()

            # Function declarations
            if line.startswith("func "):
                func_match = re.match(r"func\s+(?:\(([^)]+)\)\s+)?(\w+)\s*\(([^)]*)\)", line)
                if func_match:
                    receiver = func_match.group(1)
                    func_name = func_match.group(2)
                    kind = "method" if receiver else "function"

                    symbols.append(
                        {
                            "symbol": func_name,
                            "kind": kind,
                            "line": i + 1,
                            "signature": line.split("{")[0].strip(),
                        }
                    )

            # Type declarations
            elif line.startswith("type "):
                type_match = re.match(r"type\s+(\w+)\s+(\w+)", line)
                if type_match:
                    type_name = type_match.group(1)
                    type_kind = type_match.group(2)

                    kind = "interface" if type_kind == "interface" else "type"
                    symbols.append(
                        {
                            "symbol": type_name,
                            "kind": kind,
                            "line": i + 1,
                            "signature": line.split("{")[0].strip(),
                        }
                    )

        return symbols

    def getDefinition(self, symbol: str) -> SymbolDef | None:
        """Get definition with Go tools integration."""
        # Search local source files first for accurate file/line info
        for path in Path(".").rglob("*.go"):
            try:
                content = path.read_text(encoding="utf-8")
                if symbol in content:
                    # Parse and search
                    symbols = self._extract_symbols_basic(content)
                    for sym in symbols:
                        if sym["symbol"] == symbol:
                            # Get documentation
                            doc = self._extract_doc_comment(content, sym["line"])

                            return SymbolDef(
                                symbol=symbol,
                                kind=sym["kind"],
                                language=self.lang,
                                signature=sym["signature"],
                                doc=doc,
                                defined_in=str(path),
                                line=sym["line"],
                                span=(sym["line"], sym.get("end_line", sym["line"])),
                            )
            except Exception:
                continue

        # Fallback: use go tools for stdlib/external symbols
        return self._get_definition_with_go_tools(symbol)

    def _get_definition_with_go_tools(self, symbol: str) -> Optional[SymbolDef]:
        """Use go tools to get symbol definition."""
        try:
            # Use guru or gopls for definition lookup
            result = subprocess.run(
                ["go", "doc", symbol], capture_output=True, text=True, timeout=5
            )

            if result.returncode == 0 and result.stdout:
                # Parse go doc output
                lines = result.stdout.strip().split("\n")
                if lines:
                    signature = lines[0]
                    doc = "\n".join(lines[1:]) if len(lines) > 1 else None

                    # Infer kind from the signature prefix
                    first = signature.strip()
                    if first.startswith("func "):
                        # Check for method receiver: func (recv T) Name(...)
                        import re as _re

                        kind = "method" if _re.match(r"func\s+\(", first) else "function"
                    elif first.startswith("type "):
                        kind = "type"
                    elif first.startswith("var "):
                        kind = "variable"
                    elif first.startswith("const "):
                        kind = "constant"
                    else:
                        kind = "unknown"

                    return SymbolDef(
                        symbol=symbol,
                        kind=kind,
                        language=self.lang,
                        signature=signature,
                        doc=doc,
                        defined_in="stdlib",  # Could be stdlib or external
                        line=0,
                        span=(0, 0),
                    )
        except Exception as e:
            logger.debug(f"go tools lookup failed for {symbol}: {e}")

        return None

    def _extract_doc_comment(self, content: str, line_num: int) -> Optional[str]:
        """Extract documentation comment for a symbol."""
        lines = content.split("\n")
        doc_lines = []

        # Look backwards from the symbol line for comment block
        i = line_num - 2  # 0-indexed
        while i >= 0 and i < len(lines):
            line = lines[i].strip()
            if line.startswith("//"):
                doc_lines.insert(0, line[2:].strip())
            elif not line:
                # Empty line might be part of doc block
                i -= 1
                continue
            else:
                # Non-comment, non-empty line - stop
                break
            i -= 1

        return "\n".join(doc_lines) if doc_lines else None

    def findReferences(self, symbol: str) -> list[Reference]:
        """Find references with cross-file tracking."""
        refs: list[Reference] = []
        seen: set[tuple[str, int]] = set()

        # Check if it's a package-level symbol
        package_symbol = None
        for package_info in self.package_analyzer.packages.values():
            exports = self.package_analyzer.get_package_exports(package_info)
            if symbol in exports:
                package_symbol = f"{package_info.name}.{symbol}"
                break

        for path in Path(".").rglob("*.go"):
            try:
                content = path.read_text(encoding="utf-8")
                lines = content.split("\n")

                for i, line in enumerate(lines):
                    # Look for direct references
                    if symbol in line:
                        # Check it's not part of a larger identifier
                        import re

                        if re.search(r"\b" + re.escape(symbol) + r"\b", line):
                            key = (str(path), i + 1)
                            if key not in seen:
                                refs.append(
                                    Reference(file=str(path), start_line=i + 1, end_line=i + 1)
                                )
                                seen.add(key)

                    # Look for qualified references
                    if package_symbol and package_symbol in line:
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

    def get_module_info(self) -> Optional[Dict[str, Any]]:
        """Get information about the current Go module."""
        if self.module_resolver.current_module:
            module = self.module_resolver.current_module
            return {
                "name": module.name,
                "version": module.version,
                "dependencies": [
                    {"module": d.module_path, "version": d.version} for d in module.dependencies
                ],
                "replacements": module.replacements,
            }
        return None

    def get_package_info(self, package_path: str) -> Optional[Dict[str, Any]]:
        """Get detailed information about a package."""
        package_info = self.package_analyzer.packages.get(package_path)
        if not package_info:
            # Try to analyze it
            package_info = self.package_analyzer.analyze_package(Path(package_path))

        if package_info:
            return {
                "name": package_info.name,
                "path": str(package_info.path),
                "imports": list(package_info.imports),
                "exports": self.package_analyzer.get_package_exports(package_info),
                "types": list(package_info.types.keys()),
                "functions": list(package_info.functions.keys()),
                "interfaces": list(package_info.interfaces.keys()),
            }
        return None

    def check_interface_implementation(
        self, type_name: str, interface_name: str
    ) -> Optional[Dict[str, Any]]:
        """Check if a type implements an interface."""
        # Find the type and interface
        type_info = None
        interface_info = None

        for package_info in self.package_analyzer.packages.values():
            if type_name in package_info.types:
                type_info = package_info.types[type_name]
            if interface_name in package_info.interfaces:
                interface_info = package_info.interfaces[interface_name]

        if type_info and interface_info:
            result = self.interface_checker.check_interface_satisfaction(type_info, interface_info)
            return {
                "type": result.type_name,
                "interface": result.interface_name,
                "satisfied": result.satisfied,
                "missing_methods": result.missing_methods,
                "implemented_methods": result.implemented_methods,
                "notes": result.notes,
            }

        return None
