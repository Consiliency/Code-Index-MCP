"""TypeScript plugin extending JavaScript plugin with advanced type system support."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from tree_sitter import Node, Parser
from tree_sitter_language_pack import get_language as _get_ts_language

from ...plugin_base import (
    IndexShard,
    IPlugin,
    Reference,
    SearchOpts,
    SearchResult,
    SymbolDef,
)
from ...storage.sqlite_store import SQLiteStore
from ...utils.fuzzy_indexer import FuzzyIndexer
from ...utils.semantic_indexer import SemanticIndexer
from .declaration_handler import DeclarationHandler
from .tsconfig_parser import TSConfigParser
from .type_system import TypeAnnotationExtractor, TypeInferenceEngine

logger = logging.getLogger(__name__)


class Plugin(IPlugin):
    """TypeScript plugin for advanced code intelligence with type system support."""

    lang = "typescript"

    def __init__(
        self, sqlite_store: Optional[SQLiteStore] = None, enable_semantic: bool = False
    ) -> None:
        """Initialize the TypeScript plugin.

        Args:
            sqlite_store: Optional SQLite store for persistence
            enable_semantic: Deprecated plugin-level semantic indexing into a
                per-instance ``typescript-<id>`` collection. The dispatcher always
                passes False; its generation-scoped indexer is the semantic path.
        """
        # Initialize parsers for both JavaScript and TypeScript
        self._js_parser = Parser()
        self._ts_parser = Parser()
        self._tsx_parser = Parser()

        # Load language grammars
        self._js_language = _get_ts_language("javascript")
        self._js_parser.language = self._js_language

        self._ts_language = _get_ts_language("typescript")
        self._ts_parser.language = self._ts_language

        # Configure TSX
        try:
            self._tsx_language = _get_ts_language("tsx")
            self._tsx_parser.language = self._tsx_language
        except Exception:
            # Fallback to TypeScript parser for TSX
            self._tsx_parser.language = self._ts_language
            logger.warning("TSX parser not available, using TypeScript parser")

        # Initialize indexers and storage
        self._indexer = FuzzyIndexer(sqlite_store=sqlite_store)

        # Deprecated plugin-level semantic indexer: only when enabled and a Voyage
        # API key is available.
        self._semantic_indexer = None
        try:
            if not enable_semantic:
                logger.debug("Plugin-level semantic indexing disabled for TypeScript plugin")
            elif os.getenv("VOYAGE_API_KEY") or os.getenv("VOYAGE_API_KEY_PATH"):
                self._semantic_indexer = SemanticIndexer(collection=f"typescript-{id(self)}")
                logger.info("Semantic indexing enabled for TypeScript plugin")
            else:
                logger.info("Semantic indexing disabled (no Voyage API key)")
        except Exception as e:
            logger.debug(f"Failed to initialize semantic indexer: {e}")

        self._sqlite_store = sqlite_store
        self._repository_id = None

        # TypeScript-specific components
        self._tsconfig_parser = TSConfigParser(Path.cwd())
        self._declaration_handler = DeclarationHandler()
        self._type_engine = TypeInferenceEngine()
        self._type_extractor = TypeAnnotationExtractor()

        # State tracking
        self._scope_stack: List[str] = []
        self._current_file: Optional[Path] = None
        self._type_context: Dict[str, Any] = {}

        # Symbol cache for faster lookups
        self._symbol_cache: Dict[str, List[SymbolDef]] = {}
        self._type_cache: Dict[str, Dict[str, str]] = {}
        self._declaration_cache: Dict[str, Dict[str, Any]] = {}

        # Create or get repository if SQLite is enabled
        if self._sqlite_store:
            try:
                self._repository_id = self._sqlite_store.create_repository(
                    str(Path.cwd()), Path.cwd().name, {"language": "typescript"}
                )
            except Exception as e:
                logger.debug(f"Failed to create repository in SQLite: {e}")
                self._repository_id = None

        # Pre-index existing files
        if os.getenv("MCP_SKIP_PLUGIN_PREINDEX", "false").lower() != "true":
            self._preindex()

    def _preindex(self) -> None:
        """Pre-index all supported files in the current directory."""
        patterns = ["*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cjs", "*.d.ts"]

        for pattern in patterns:
            for path in Path(".").rglob(pattern):
                try:
                    # Skip node_modules and common build directories
                    if any(
                        part in path.parts
                        for part in [
                            "node_modules",
                            "dist",
                            "build",
                            ".next",
                            "coverage",
                        ]
                    ):
                        continue

                    # Skip minified files
                    if path.stem.endswith(".min"):
                        continue

                    # Check if file should be included per tsconfig
                    if not self._tsconfig_parser.is_file_included(path):
                        continue

                    text = path.read_text(encoding="utf-8")
                    self._indexer.add_file(str(path), text)

                    # Index for semantic search if available
                    if self._semantic_indexer:
                        try:
                            self._semantic_indexer.index_file(path)
                        except Exception as e:
                            logger.debug(f"Semantic indexing failed for {path}: {e}")

                except Exception as e:
                    logger.warning(f"Failed to pre-index {path}: {e}")
                    continue

    def supports(self, path: str | Path) -> bool:
        """Check if this plugin supports the given file."""
        suffixes = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".d.ts"}
        path_obj = Path(path)

        # Check extension
        if path_obj.suffix.lower() not in suffixes:
            return False

        # Check if included by tsconfig
        return self._tsconfig_parser.is_file_included(path_obj)

    def _get_parser(self, path: Path) -> Parser:
        """Get the appropriate parser for the file type."""
        if path.suffix in {".tsx"}:
            return self._tsx_parser
        elif path.suffix in {".ts", ".d.ts"}:
            return self._ts_parser
        return self._js_parser

    def indexFile(self, path: str | Path, content: str) -> IndexShard:
        """Parse and index a TypeScript file with enhanced type information."""
        if isinstance(path, str):
            path = Path(path)

        self._current_file = path
        self._indexer.add_file(str(path), content)

        # Choose parser based on file extension
        parser = self._get_parser(path)
        tree = parser.parse(content.encode("utf-8"))
        root = tree.root_node

        # Store file in SQLite if available
        file_id = None
        if self._sqlite_store and self._repository_id:
            import hashlib

            file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            file_id = self._sqlite_store.store_file(
                self._repository_id,
                str(path),
                language=self._determine_language(path),
                size=len(content),
                metadata={"hash": file_hash},
            )

        # Extract symbols with type information
        symbols: List[Dict[str, Any]] = []
        imports: List[Dict[str, Any]] = []
        exports: List[Dict[str, Any]] = []

        # Initialize type context for this file
        self._type_context = {
            "variables": {},
            "functions": {},
            "classes": {},
            "interfaces": {},
            "types": {},
            "enums": {},
        }

        # Handle declaration files specially
        if path.suffix == ".d.ts":
            declarations = self._declaration_handler.parse_declaration_file(path, content, root)
            self._declaration_cache[str(path)] = declarations
            symbols = self._convert_declarations_to_symbols(declarations, str(path), content)
        else:
            self._extract_symbols_with_types(root, content, symbols, imports, exports, file_id)

        # Store module information if SQLite available
        if self._sqlite_store and self._repository_id and file_id:
            self._store_module_info(path, imports, exports, file_id)

        # Cache symbols for quick lookup
        cache_key = str(path)
        self._symbol_cache[cache_key] = [
            self._symbol_to_def(s, str(path), content) for s in symbols
        ]

        # Index symbols for semantic search if available
        if self._semantic_indexer:
            try:
                for symbol in symbols:
                    if symbol.get("signature"):
                        self._semantic_indexer.index_symbol(
                            file=str(path),
                            name=symbol["symbol"],
                            kind=symbol["kind"],
                            signature=symbol["signature"],
                            line=symbol["line"],
                            span=symbol.get("span", (symbol["line"], symbol["line"] + 1)),
                            doc=symbol.get("doc"),
                            content=symbol.get("full_text", ""),
                        )
            except Exception as e:
                logger.debug(f"Semantic symbol indexing failed for {path}: {e}")

        return {
            "file": str(path),
            "symbols": symbols,
            "language": self.lang,
            "types": self._type_cache.get(str(path), {}),
            "declarations": self._declaration_cache.get(str(path), {}),
        }

    def _determine_language(self, path: Path) -> str:
        """Determine the specific language variant."""
        suffix = path.suffix.lower()
        if suffix in {".ts", ".d.ts"}:
            return "typescript"
        elif suffix == ".tsx":
            return "tsx"
        elif suffix in {".js", ".mjs", ".cjs"}:
            return "javascript"
        elif suffix == ".jsx":
            return "jsx"
        return "typescript"

    def _extract_symbols_with_types(
        self,
        node: Node,
        content: str,
        symbols: List[Dict],
        imports: List[Dict],
        exports: List[Dict],
        file_id: Optional[int] = None,
        scope_path: List[str] = None,
    ) -> None:
        """Recursively extract symbols with TypeScript type information."""
        if scope_path is None:
            scope_path = []

        # Extract imports and exports at the top level
        if not scope_path:
            imports.extend(self._extract_imports(node, content))
            exports.extend(self._extract_exports(node, content))

        # Function declarations with type annotations
        if node.type in ["function_declaration", "function"]:
            self._extract_function_with_types(node, content, symbols, scope_path, file_id)

        # Variable declarations with type inference
        elif node.type in ["variable_declaration", "lexical_declaration"]:
            self._extract_variables_with_types(node, content, symbols, scope_path, file_id)

        # Class declarations with type information
        elif node.type in ["class_declaration", "class"]:
            self._extract_class_with_types(node, content, symbols, scope_path, file_id)

        # Interface declarations
        elif node.type == "interface_declaration":
            self._extract_interface(node, content, symbols, scope_path, file_id)

        # Type alias declarations
        elif node.type == "type_alias_declaration":
            self._extract_type_alias(node, content, symbols, scope_path, file_id)

        # Enum declarations
        elif node.type == "enum_declaration":
            self._extract_enum(node, content, symbols, scope_path, file_id)

        # Module/namespace declarations
        elif node.type in ["module_declaration", "namespace_declaration"]:
            self._extract_module(node, content, symbols, scope_path, file_id)

        # Method definitions with type information
        elif node.type in ["method_definition", "method_declaration"]:
            self._extract_method_with_types(node, content, symbols, scope_path, file_id)

        # Continue recursion for child nodes
        for child in node.named_children:
            if child.type not in ["class_body", "interface_body", "module_body"]:
                self._extract_symbols_with_types(
                    child, content, symbols, imports, exports, file_id, scope_path
                )

    def _extract_function_with_types(
        self,
        node: Node,
        content: str,
        symbols: List[Dict],
        scope_path: List[str],
        file_id: Optional[int],
    ) -> None:
        """Extract function with complete type information."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return

        name = content[name_node.start_byte : name_node.end_byte]

        # Extract type parameters
        type_params = []
        type_params_node = node.child_by_field_name("type_parameters")
        if type_params_node:
            type_params = self._extract_type_parameters(type_params_node, content)

        # Extract parameters with types
        params = []
        params_node = node.child_by_field_name("parameters")
        if params_node:
            params = self._extract_typed_parameters(params_node, content)

        # Extract return type
        return_type = None
        return_type_node = node.child_by_field_name("return_type")
        if return_type_node:
            type_node = (
                return_type_node.named_children[0] if return_type_node.named_children else None
            )
            if type_node:
                return_type = content[type_node.start_byte : type_node.end_byte]
        else:
            # Infer return type
            body_node = node.child_by_field_name("body")
            if body_node:
                return_type = self._type_engine.infer_type(body_node, content, self._type_context)

        # Check for modifiers
        is_async = self._has_modifier(node, content, "async")
        is_generator = self._has_modifier(node, content, "*")
        is_export = self._is_exported(node)

        # Build signature
        signature = self._build_function_signature(
            name, type_params, params, return_type, is_async, is_generator
        )

        # Extract documentation
        doc = self._extract_jsdoc(node, content)

        symbol_info = {
            "symbol": name,
            "kind": "function",
            "signature": signature,
            "line": node.start_point[0] + 1,
            "span": (node.start_point[0] + 1, node.end_point[0] + 1),
            "scope": ".".join(scope_path) if scope_path else None,
            "type_parameters": type_params,
            "parameters": params,
            "return_type": return_type,
            "is_async": is_async,
            "is_generator": is_generator,
            "is_export": is_export,
            "doc": doc,
        }
        symbols.append(symbol_info)

        # Store type information
        full_name = ".".join(scope_path + [name])
        self._type_context["functions"][full_name] = signature
        self._type_engine.set_symbol_type(name, signature, self._current_file)

        # Store in SQLite if available
        if self._sqlite_store and file_id:
            symbol_id = self._sqlite_store.store_symbol(
                file_id,
                name,
                "function",
                node.start_point[0] + 1,
                node.end_point[0] + 1,
                signature=signature,
            )
            self._indexer.add_symbol(
                name,
                str(self._current_file),
                node.start_point[0] + 1,
                {"symbol_id": symbol_id, "file_id": file_id},
            )

    def _extract_variables_with_types(
        self,
        node: Node,
        content: str,
        symbols: List[Dict],
        scope_path: List[str],
        file_id: Optional[int],
    ) -> None:
        """Extract variables with type information."""
        kind_keyword = self._get_declaration_kind(node, content)

        for child in node.named_children:
            if child.type == "variable_declarator":
                name_node = child.child_by_field_name("name")
                value_node = child.child_by_field_name("value") or child.child_by_field_name("init")
                type_annotation = child.child_by_field_name("type")

                if name_node:
                    name = content[name_node.start_byte : name_node.end_byte]

                    # Extract explicit type annotation
                    explicit_type = None
                    if type_annotation:
                        type_node = (
                            type_annotation.named_children[0]
                            if type_annotation.named_children
                            else None
                        )
                        if type_node:
                            explicit_type = content[type_node.start_byte : type_node.end_byte]

                    # Infer type from value if no explicit type
                    inferred_type = None
                    if value_node:
                        inferred_type = self._type_engine.infer_type(
                            value_node, content, self._type_context
                        )

                    # Determine final type
                    final_type = explicit_type or inferred_type or "any"

                    # Determine symbol kind
                    symbol_kind = "variable"
                    signature = f"{kind_keyword} {name}: {final_type}"
                    span = (node.start_point[0] + 1, node.end_point[0] + 1)

                    if value_node:
                        if value_node.type == "arrow_function":
                            symbol_kind = "arrow_function"
                            func_sig = self._extract_arrow_function_signature(
                                value_node, content, name
                            )
                            signature = f"{kind_keyword} {name} = {func_sig}"
                            span = (
                                node.start_point[0] + 1,
                                value_node.end_point[0] + 1,
                            )
                        elif value_node.type in ["function_expression", "function"]:
                            symbol_kind = "function"
                            func_sig = self._extract_function_expression_signature(
                                value_node, content, name
                            )
                            signature = f"{kind_keyword} {name} = {func_sig}"
                            span = (
                                node.start_point[0] + 1,
                                value_node.end_point[0] + 1,
                            )
                        elif value_node.type in ["class_expression", "class"]:
                            symbol_kind = "class"
                            signature = f"{kind_keyword} {name} = class"
                            span = (
                                node.start_point[0] + 1,
                                value_node.end_point[0] + 1,
                            )

                    symbol_info = {
                        "symbol": name,
                        "kind": symbol_kind,
                        "signature": signature,
                        "line": node.start_point[0] + 1,
                        "span": span,
                        "scope": ".".join(scope_path) if scope_path else None,
                        "type": final_type,
                        "declaration_kind": kind_keyword,
                    }
                    symbols.append(symbol_info)

                    # Store type information
                    full_name = ".".join(scope_path + [name])
                    self._type_context["variables"][full_name] = final_type
                    self._type_engine.set_symbol_type(name, final_type, self._current_file)

    def _extract_class_with_types(
        self,
        node: Node,
        content: str,
        symbols: List[Dict],
        scope_path: List[str],
        file_id: Optional[int],
    ) -> None:
        """Extract class with complete type information."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return

        name = content[name_node.start_byte : name_node.end_byte]

        # Extract type parameters
        type_params = []
        type_params_node = node.child_by_field_name("type_parameters")
        if type_params_node:
            type_params = self._extract_type_parameters(type_params_node, content)

        # Extract heritage (extends/implements)
        extends_clause = None
        implements_clause = []
        heritage_node = node.child_by_field_name("heritage")
        if heritage_node:
            for child in heritage_node.named_children:
                if child.type == "extends_clause":
                    type_node = child.named_children[0] if child.named_children else None
                    if type_node:
                        extends_clause = content[type_node.start_byte : type_node.end_byte]
                elif child.type == "implements_clause":
                    for impl_child in child.named_children:
                        implements_clause.append(
                            content[impl_child.start_byte : impl_child.end_byte]
                        )

        # Build class signature
        signature_parts = ["class", name]
        if type_params:
            type_param_strs = [tp["name"] for tp in type_params]
            signature_parts.append(f"<{', '.join(type_param_strs)}>")
        if extends_clause:
            signature_parts.append(f"extends {extends_clause}")
        if implements_clause:
            signature_parts.append(f"implements {', '.join(implements_clause)}")

        signature = " ".join(signature_parts)

        # Extract documentation
        doc = self._extract_jsdoc(node, content)

        symbol_info = {
            "symbol": name,
            "kind": "class",
            "signature": signature,
            "line": node.start_point[0] + 1,
            "span": (node.start_point[0] + 1, node.end_point[0] + 1),
            "scope": ".".join(scope_path) if scope_path else None,
            "type_parameters": type_params,
            "extends": extends_clause,
            "implements": implements_clause,
            "doc": doc,
        }
        symbols.append(symbol_info)

        # Store type information
        full_name = ".".join(scope_path + [name])
        self._type_context["classes"][full_name] = signature
        self._type_engine.set_symbol_type(name, name, self._current_file)  # Class type is its name

        # Extract class members
        body_node = node.child_by_field_name("body")
        if body_node:
            new_scope = scope_path + [name]
            for child in body_node.named_children:
                self._extract_symbols_with_types(
                    child, content, symbols, [], [], file_id, new_scope
                )

    def _extract_interface(
        self,
        node: Node,
        content: str,
        symbols: List[Dict],
        scope_path: List[str],
        file_id: Optional[int],
    ) -> None:
        """Extract interface declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return

        name = content[name_node.start_byte : name_node.end_byte]

        # Extract type parameters
        type_params = []
        type_params_node = node.child_by_field_name("type_parameters")
        if type_params_node:
            type_params = self._extract_type_parameters(type_params_node, content)

        # Extract heritage
        heritage = []
        heritage_node = node.child_by_field_name("heritage")
        if heritage_node:
            for child in heritage_node.named_children:
                if child.type == "extends_clause":
                    for ext_child in child.named_children:
                        heritage.append(content[ext_child.start_byte : ext_child.end_byte])

        # Build signature
        signature_parts = ["interface", name]
        if type_params:
            type_param_strs = [tp["name"] for tp in type_params]
            signature_parts.append(f"<{', '.join(type_param_strs)}>")
        if heritage:
            signature_parts.append(f"extends {', '.join(heritage)}")

        signature = " ".join(signature_parts)

        # Extract documentation
        doc = self._extract_jsdoc(node, content)

        symbol_info = {
            "symbol": name,
            "kind": "interface",
            "signature": signature,
            "line": node.start_point[0] + 1,
            "span": (node.start_point[0] + 1, node.end_point[0] + 1),
            "scope": ".".join(scope_path) if scope_path else None,
            "type_parameters": type_params,
            "extends": heritage,
            "doc": doc,
        }
        symbols.append(symbol_info)

        # Store type information
        full_name = ".".join(scope_path + [name])
        self._type_context["interfaces"][full_name] = signature

    def _extract_type_alias(
        self,
        node: Node,
        content: str,
        symbols: List[Dict],
        scope_path: List[str],
        file_id: Optional[int],
    ) -> None:
        """Extract type alias declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return

        name = content[name_node.start_byte : name_node.end_byte]

        # Extract type parameters
        type_params = []
        type_params_node = node.child_by_field_name("type_parameters")
        if type_params_node:
            type_params = self._extract_type_parameters(type_params_node, content)

        # Extract type definition
        type_node = node.child_by_field_name("type")
        type_def = "any"
        if type_node:
            type_def = content[type_node.start_byte : type_node.end_byte]

        # Build signature
        signature_parts = ["type", name]
        if type_params:
            type_param_strs = [tp["name"] for tp in type_params]
            signature_parts.append(f"<{', '.join(type_param_strs)}>")
        signature_parts.extend(["=", type_def])

        signature = " ".join(signature_parts)

        # Extract documentation
        doc = self._extract_jsdoc(node, content)

        symbol_info = {
            "symbol": name,
            "kind": "type",
            "signature": signature,
            "line": node.start_point[0] + 1,
            "span": (node.start_point[0] + 1, node.end_point[0] + 1),
            "scope": ".".join(scope_path) if scope_path else None,
            "type_parameters": type_params,
            "type_definition": type_def,
            "doc": doc,
        }
        symbols.append(symbol_info)

        # Store type information
        full_name = ".".join(scope_path + [name])
        self._type_context["types"][full_name] = signature

    def _extract_enum(
        self,
        node: Node,
        content: str,
        symbols: List[Dict],
        scope_path: List[str],
        file_id: Optional[int],
    ) -> None:
        """Extract enum declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return

        name = content[name_node.start_byte : name_node.end_byte]

        # Extract enum members
        body_node = node.child_by_field_name("body")
        members = []

        if body_node:
            for child in body_node.named_children:
                if child.type == "property_identifier":
                    member_name = content[child.start_byte : child.end_byte]
                    members.append(member_name)
                elif child.type == "enum_assignment":
                    name_child = child.child_by_field_name("name")
                    if name_child:
                        member_name = content[name_child.start_byte : name_child.end_byte]
                        members.append(member_name)

        signature = f"enum {name} {{ {', '.join(members)} }}"

        # Extract documentation
        doc = self._extract_jsdoc(node, content)

        symbol_info = {
            "symbol": name,
            "kind": "enum",
            "signature": signature,
            "line": node.start_point[0] + 1,
            "span": (node.start_point[0] + 1, node.end_point[0] + 1),
            "scope": ".".join(scope_path) if scope_path else None,
            "members": members,
            "doc": doc,
        }
        symbols.append(symbol_info)

        # Store type information
        full_name = ".".join(scope_path + [name])
        self._type_context["enums"][full_name] = name

    def _extract_module(
        self,
        node: Node,
        content: str,
        symbols: List[Dict],
        scope_path: List[str],
        file_id: Optional[int],
    ) -> None:
        """Extract module/namespace declaration."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return

        name = content[name_node.start_byte : name_node.end_byte]
        signature = f"namespace {name}"

        # Extract documentation
        doc = self._extract_jsdoc(node, content)

        symbol_info = {
            "symbol": name,
            "kind": "namespace",
            "signature": signature,
            "line": node.start_point[0] + 1,
            "span": (node.start_point[0] + 1, node.end_point[0] + 1),
            "scope": ".".join(scope_path) if scope_path else None,
            "doc": doc,
        }
        symbols.append(symbol_info)

        # Extract nested declarations
        body_node = node.child_by_field_name("body")
        if body_node:
            new_scope = scope_path + [name]
            for child in body_node.named_children:
                self._extract_symbols_with_types(
                    child, content, symbols, [], [], file_id, new_scope
                )

    def _extract_method_with_types(
        self,
        node: Node,
        content: str,
        symbols: List[Dict],
        scope_path: List[str],
        file_id: Optional[int],
    ) -> None:
        """Extract method with type information."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return

        name = content[name_node.start_byte : name_node.end_byte]

        # Extract parameters with types
        params = []
        params_node = node.child_by_field_name("parameters")
        if params_node:
            params = self._extract_typed_parameters(params_node, content)

        # Extract return type
        return_type = None
        return_type_node = node.child_by_field_name("return_type")
        if return_type_node:
            type_node = (
                return_type_node.named_children[0] if return_type_node.named_children else None
            )
            if type_node:
                return_type = content[type_node.start_byte : type_node.end_byte]

        # Check for modifiers
        is_static = self._has_modifier(node, content, "static")
        is_async = self._has_modifier(node, content, "async")
        is_private = self._has_modifier(node, content, "private")
        is_protected = self._has_modifier(node, content, "protected")
        _ = self._has_modifier(node, content, "public")
        is_getter = self._has_modifier(node, content, "get")
        is_setter = self._has_modifier(node, content, "set")

        # Determine visibility
        visibility = "public"
        if is_private:
            visibility = "private"
        elif is_protected:
            visibility = "protected"

        # Determine kind
        kind = "method"
        if is_getter:
            kind = "getter"
        elif is_setter:
            kind = "setter"

        # Build signature
        signature_parts = []
        if is_static:
            signature_parts.append("static")
        if visibility != "public":
            signature_parts.append(visibility)
        if is_async:
            signature_parts.append("async")

        if is_getter:
            signature_parts.extend(["get", name, "()"])
        elif is_setter:
            param_strs = [f"{p['name']}: {p.get('type', 'any')}" for p in params]
            signature_parts.extend(["set", name, f"({', '.join(param_strs)})"])
        else:
            param_strs = [f"{p['name']}: {p.get('type', 'any')}" for p in params]
            signature_parts.extend([name, f"({', '.join(param_strs)})"])

        if return_type and not is_setter:
            signature_parts.extend([":", return_type])

        signature = " ".join(signature_parts)

        # Extract documentation
        doc = self._extract_jsdoc(node, content)

        symbol_info = {
            "symbol": f"{'.'.join(scope_path)}.{name}" if scope_path else name,
            "kind": kind,
            "signature": signature,
            "line": node.start_point[0] + 1,
            "span": (node.start_point[0] + 1, node.end_point[0] + 1),
            "scope": ".".join(scope_path) if scope_path else None,
            "parameters": params,
            "return_type": return_type,
            "visibility": visibility,
            "is_static": is_static,
            "is_async": is_async,
            "doc": doc,
        }
        symbols.append(symbol_info)

    def _extract_type_parameters(self, node: Node, content: str) -> List[Dict[str, Any]]:
        """Extract type parameters from a node."""
        type_params = []

        for child in node.named_children:
            if child.type == "type_parameter":
                name_node = child.child_by_field_name("name")
                constraint_node = child.child_by_field_name("constraint")
                default_node = child.child_by_field_name("default_type")

                if name_node:
                    param_name = content[name_node.start_byte : name_node.end_byte]
                    constraint = None
                    default_type = None

                    if constraint_node:
                        # Skip 'extends' keyword
                        type_node = (
                            constraint_node.named_children[0]
                            if constraint_node.named_children
                            else None
                        )
                        if type_node:
                            constraint = content[type_node.start_byte : type_node.end_byte]

                    if default_node:
                        default_type = content[default_node.start_byte : default_node.end_byte]

                    type_params.append(
                        {
                            "name": param_name,
                            "constraint": constraint,
                            "default": default_type,
                        }
                    )

        return type_params

    def _extract_typed_parameters(self, node: Node, content: str) -> List[Dict[str, Any]]:
        """Extract function parameters with type information."""
        params = []

        for child in node.named_children:
            if child.type in [
                "required_parameter",
                "optional_parameter",
                "rest_parameter",
            ]:
                pattern_node = child.child_by_field_name("pattern")
                type_annotation = child.child_by_field_name("type")

                if pattern_node:
                    param_name = content[pattern_node.start_byte : pattern_node.end_byte]
                    param_type = "any"

                    if type_annotation:
                        type_node = (
                            type_annotation.named_children[0]
                            if type_annotation.named_children
                            else None
                        )
                        if type_node:
                            param_type = content[type_node.start_byte : type_node.end_byte]

                    is_optional = child.type == "optional_parameter"
                    is_rest = child.type == "rest_parameter"

                    params.append(
                        {
                            "name": param_name,
                            "type": param_type,
                            "optional": is_optional,
                            "rest": is_rest,
                        }
                    )

        return params

    def _build_function_signature(
        self,
        name: str,
        type_params: List[Dict],
        params: List[Dict],
        return_type: Optional[str],
        is_async: bool,
        is_generator: bool,
    ) -> str:
        """Build a complete function signature."""
        sig_parts = []

        if is_async:
            sig_parts.append("async")

        sig_parts.append("function")

        if is_generator:
            sig_parts.append("*")

        sig_parts.append(name)

        # Type parameters
        if type_params:
            type_param_strs = []
            for tp in type_params:
                tp_str = tp["name"]
                if tp.get("constraint"):
                    tp_str += f" extends {tp['constraint']}"
                if tp.get("default"):
                    tp_str += f" = {tp['default']}"
                type_param_strs.append(tp_str)
            sig_parts.append(f"<{', '.join(type_param_strs)}>")

        # Parameters
        param_strs = []
        for param in params:
            param_str = param["name"]
            if param.get("optional"):
                param_str += "?"
            if param.get("rest"):
                param_str = f"...{param_str}"
            if param.get("type"):
                param_str += f": {param['type']}"
            param_strs.append(param_str)

        sig_parts.append(f"({', '.join(param_strs)})")

        # Return type
        if return_type:
            sig_parts.append(f": {return_type}")

        return " ".join(sig_parts)

    def _extract_arrow_function_signature(self, node: Node, content: str, var_name: str) -> str:
        """Extract arrow function signature."""
        # Extract parameters
        params = []
        for child in node.children:
            if child.type == "formal_parameters":
                for param_child in child.named_children:
                    if param_child.type in [
                        "identifier",
                        "required_parameter",
                        "optional_parameter",
                    ]:
                        if param_child.type == "identifier":
                            params.append(content[param_child.start_byte : param_child.end_byte])
                        else:
                            pattern_node = param_child.child_by_field_name("pattern")
                            type_annotation = param_child.child_by_field_name("type")
                            if pattern_node:
                                param_name = content[
                                    pattern_node.start_byte : pattern_node.end_byte
                                ]
                                if type_annotation:
                                    type_node = (
                                        type_annotation.named_children[0]
                                        if type_annotation.named_children
                                        else None
                                    )
                                    if type_node:
                                        param_type = content[
                                            type_node.start_byte : type_node.end_byte
                                        ]
                                        param_name += f": {param_type}"
                                if param_child.type == "optional_parameter":
                                    param_name += "?"
                                params.append(param_name)
            elif child.type == "identifier":
                # Single parameter arrow function
                params.append(content[child.start_byte : child.end_byte])
                break

        # Extract return type
        return_type = None
        return_type_node = node.child_by_field_name("return_type")
        if return_type_node:
            type_node = (
                return_type_node.named_children[0] if return_type_node.named_children else None
            )
            if type_node:
                return_type = content[type_node.start_byte : type_node.end_byte]

        # Check for async
        is_async = self._has_modifier(node, content, "async")

        sig_parts = []
        if is_async:
            sig_parts.append("async")

        param_str = ", ".join(params) if len(params) != 1 else params[0] if params else ""
        if len(params) != 1 or ":" in param_str:
            param_str = f"({param_str})"

        sig_parts.append(f"{param_str} =>")

        if return_type:
            sig_parts.append(return_type)
        else:
            sig_parts.append("{...}")

        return " ".join(sig_parts)

    def _extract_function_expression_signature(
        self, node: Node, content: str, var_name: str
    ) -> str:
        """Extract function expression signature."""
        # Extract parameters
        params_node = node.child_by_field_name("parameters")
        params = []
        if params_node:
            params = self._extract_typed_parameters(params_node, content)

        # Extract return type
        return_type = None
        return_type_node = node.child_by_field_name("return_type")
        if return_type_node:
            type_node = (
                return_type_node.named_children[0] if return_type_node.named_children else None
            )
            if type_node:
                return_type = content[type_node.start_byte : type_node.end_byte]

        # Check for async
        is_async = self._has_modifier(node, content, "async")

        sig_parts = []
        if is_async:
            sig_parts.append("async")

        sig_parts.append("function")

        # Parameters
        param_strs = [f"{p['name']}: {p.get('type', 'any')}" for p in params]
        sig_parts.append(f"({', '.join(param_strs)})")

        if return_type:
            sig_parts.append(f": {return_type}")

        return " ".join(sig_parts)

    def _extract_jsdoc(self, node: Node, content: str) -> Optional[str]:
        """Extract JSDoc comment above a node."""
        lines = content.splitlines()
        line_idx = node.start_point[0]

        if line_idx > 0:
            # Look for JSDoc comment above the symbol
            for i in range(line_idx - 1, max(0, line_idx - 10), -1):
                line = lines[i].strip()
                if line.endswith("*/"):
                    # Found end of JSDoc, extract it
                    doc_lines = []
                    for j in range(i, max(0, i - 20), -1):
                        doc_lines.insert(0, lines[j])
                        if lines[j].strip().startswith("/**"):
                            return "\n".join(doc_lines)
                    break
                elif line and not line.startswith("*") and not line.startswith("//"):
                    # Non-comment line, stop looking
                    break

        return None

    def _convert_declarations_to_symbols(
        self, declarations: Dict[str, Any], file_path: str, content: str
    ) -> List[Dict[str, Any]]:
        """Convert declaration file declarations to symbol format."""
        symbols = []

        for decl_type, items in declarations.items():
            if decl_type in [
                "interfaces",
                "types",
                "functions",
                "classes",
                "modules",
                "enums",
                "variables",
            ]:
                for item in items:
                    symbol_info = {
                        "symbol": item["name"],
                        "kind": decl_type.rstrip("s"),  # Remove plural
                        "signature": item.get("signature", item["name"]),
                        "line": item["line"],
                        "span": item.get("span", (item["line"], item["line"] + 1)),
                        "scope": item.get("scope"),
                        "doc": item.get("doc"),
                    }

                    # Add type-specific information
                    if decl_type == "interfaces":
                        symbol_info.update(
                            {
                                "type_parameters": item.get("type_parameters", []),
                                "extends": item.get("extends", []),
                                "properties": item.get("properties", []),
                                "methods": item.get("methods", []),
                            }
                        )
                    elif decl_type == "types":
                        symbol_info.update(
                            {
                                "type_parameters": item.get("type_parameters", []),
                                "type_definition": item.get("type_definition"),
                            }
                        )
                    elif decl_type == "functions":
                        symbol_info.update(
                            {
                                "type_parameters": item.get("type_parameters", []),
                                "parameters": item.get("parameters", []),
                                "return_type": item.get("return_type"),
                                "is_async": item.get("is_async", False),
                                "is_generator": item.get("is_generator", False),
                            }
                        )
                    elif decl_type == "classes":
                        symbol_info.update(
                            {
                                "type_parameters": item.get("type_parameters", []),
                                "extends": item.get("extends"),
                                "implements": item.get("implements", []),
                                "properties": item.get("properties", []),
                                "methods": item.get("methods", []),
                            }
                        )
                    elif decl_type == "enums":
                        symbol_info.update({"members": item.get("members", [])})
                    elif decl_type == "variables":
                        symbol_info.update(
                            {
                                "type": item.get("type"),
                                "declaration_kind": item.get("kind", "const"),
                            }
                        )

                    symbols.append(symbol_info)

        return symbols

    # Inherited methods from JavaScript plugin with type enhancements
    def _extract_imports(self, root: Node, content: str) -> List[Dict[str, Any]]:
        """Extract import statements with type information."""
        imports = []

        for node in self._walk_tree(root):
            # ES6 imports
            if node.type in ["import_statement", "import_declaration"]:
                source_node = node.child_by_field_name("source")
                if source_node:
                    source = content[source_node.start_byte : source_node.end_byte].strip("\"'`")

                    # Check if it's a type-only import
                    is_type_only = False
                    for child in node.children:
                        if (
                            not child.is_named
                            and content[child.start_byte : child.end_byte].strip() == "type"
                        ):
                            is_type_only = True
                            break

                    # Extract imported names with type information
                    imported_names = []
                    default_import = None
                    namespace_import = None

                    import_clause = node.child_by_field_name("import_clause")
                    if import_clause:
                        for child in import_clause.named_children:
                            if child.type == "identifier":
                                default_import = content[child.start_byte : child.end_byte]
                            elif child.type == "namespace_import":
                                name_node = child.child_by_field_name("name")
                                if name_node:
                                    namespace_import = content[
                                        name_node.start_byte : name_node.end_byte
                                    ]
                            elif child.type == "named_imports":
                                for spec in child.named_children:
                                    if spec.type == "import_specifier":
                                        name_node = spec.child_by_field_name("name")
                                        alias_node = spec.child_by_field_name("alias")
                                        if name_node:
                                            name = content[
                                                name_node.start_byte : name_node.end_byte
                                            ]
                                            if alias_node:
                                                alias = content[
                                                    alias_node.start_byte : alias_node.end_byte
                                                ]
                                                imported_names.append(f"{name} as {alias}")
                                            else:
                                                imported_names.append(name)

                    import_info = {
                        "source": source,
                        "line": node.start_point[0] + 1,
                        "type": "esm",
                        "is_type_only": is_type_only,
                    }

                    if default_import:
                        import_info["default"] = default_import
                    if namespace_import:
                        import_info["namespace"] = namespace_import
                    if imported_names:
                        import_info["names"] = imported_names

                    imports.append(import_info)

        return imports

    def _extract_exports(self, root: Node, content: str) -> List[Dict[str, Any]]:
        """Extract export statements with type information."""
        exports = []

        for node in self._walk_tree(root):
            # ES6 exports
            if node.type in ["export_statement", "export_declaration"]:
                # Check for type-only export
                is_type_only = False
                for child in node.children:
                    if (
                        not child.is_named
                        and content[child.start_byte : child.end_byte].strip() == "type"
                    ):
                        is_type_only = True
                        break

                # Check for default export
                is_default = False
                for child in node.children:
                    if (
                        not child.is_named
                        and content[child.start_byte : child.end_byte].strip() == "default"
                    ):
                        is_default = True
                        break

                # Extract what's being exported
                declaration = node.child_by_field_name("declaration")
                exported_names = []

                if declaration:
                    # Direct export
                    if declaration.type in [
                        "function_declaration",
                        "class_declaration",
                        "interface_declaration",
                        "type_alias_declaration",
                        "enum_declaration",
                    ]:
                        name_node = declaration.child_by_field_name("name")
                        if name_node:
                            name = content[name_node.start_byte : name_node.end_byte]
                            exported_names.append(
                                {
                                    "name": name,
                                    "kind": declaration.type.replace("_declaration", ""),
                                    "is_default": is_default,
                                }
                            )
                else:
                    # Named exports (export { foo, bar })
                    for child in node.named_children:
                        if child.type == "export_clause":
                            for spec in child.named_children:
                                if spec.type == "export_specifier":
                                    name_node = spec.child_by_field_name("name")
                                    alias_node = spec.child_by_field_name("alias")
                                    if name_node:
                                        name = content[name_node.start_byte : name_node.end_byte]
                                        export_name = name
                                        if alias_node:
                                            export_name = content[
                                                alias_node.start_byte : alias_node.end_byte
                                            ]
                                        exported_names.append(
                                            {
                                                "name": export_name,
                                                "original": (name if alias_node else None),
                                                "kind": "named",
                                            }
                                        )

                if exported_names:
                    exports.append(
                        {
                            "names": exported_names,
                            "is_type_only": is_type_only,
                            "line": node.start_point[0] + 1,
                        }
                    )

        return exports

    def _store_module_info(
        self, path: Path, imports: List[Dict], exports: List[Dict], file_id: int
    ) -> None:
        """Store module dependency information in SQLite."""
        if not self._sqlite_store:
            return

        # TODO: Implement enhanced import/export storage with type information

    def _walk_tree(self, node: Node) -> List[Node]:
        """Walk the tree and yield all nodes."""
        nodes = [node]
        for child in node.children:
            nodes.extend(self._walk_tree(child))
        return nodes

    def _symbol_to_def(self, symbol: Dict[str, Any], file_path: str, content: str) -> SymbolDef:
        """Convert internal symbol representation to SymbolDef with type information."""
        return {
            "symbol": symbol["symbol"],
            "kind": symbol["kind"],
            "language": self.lang,
            "signature": symbol["signature"],
            "doc": symbol.get("doc"),
            "defined_in": file_path,
            "line": symbol.get("line", 1),
            "span": symbol.get("span", (symbol.get("line", 1), symbol.get("line", 1) + 1)),
            "type": symbol.get("type"),
            "type_parameters": symbol.get("type_parameters"),
            "parameters": symbol.get("parameters"),
            "return_type": symbol.get("return_type"),
        }

    def _has_modifier(self, node: Node, content: str, modifier: str) -> bool:
        """Check if a node has a specific modifier."""
        for child in node.children:
            if not child.is_named:
                text = content[child.start_byte : child.end_byte].strip()
                if text == modifier:
                    return True
        return False

    def _get_declaration_kind(self, node: Node, content: str) -> str:
        """Get the declaration keyword (var, let, const)."""
        for child in node.children:
            if not child.is_named:
                text = content[child.start_byte : child.end_byte].strip()
                if text in ["var", "let", "const"]:
                    return text
        return "const"

    def _is_exported(self, node: Node) -> bool:
        """Check if a node is exported."""
        # Check if parent is an export statement
        parent = node.parent
        while parent:
            if parent.type in ["export_statement", "export_declaration"]:
                return True
            parent = parent.parent
        return False

    # Enhanced plugin methods
    def getDefinition(self, symbol: str) -> SymbolDef | None:
        """Get the definition of a symbol with type information."""
        # First check cache
        for file_path, symbols in self._symbol_cache.items():
            for sym_def in symbols:
                if sym_def["symbol"] == symbol or sym_def["symbol"].endswith(f".{symbol}"):
                    return sym_def

        # Search in declaration files
        for file_path, declarations in self._declaration_cache.items():
            type_info = self._declaration_handler.get_type_information(symbol, Path(file_path))
            if type_info:
                return self._convert_type_info_to_symbol_def(type_info, file_path)

        # Search in all supported files
        patterns = ["*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cjs", "*.d.ts"]

        for pattern in patterns:
            for path in Path(".").rglob(pattern):
                try:
                    # Skip excluded directories
                    if any(
                        part in path.parts for part in ["node_modules", "dist", "build", ".next"]
                    ):
                        continue

                    if not self._tsconfig_parser.is_file_included(path):
                        continue

                    content = path.read_text(encoding="utf-8")
                    shard = self.indexFile(path, content)

                    for sym in shard["symbols"]:
                        if sym["symbol"] == symbol or sym["symbol"].endswith(f".{symbol}"):
                            return self._symbol_to_def(sym, str(path), content)
                except Exception:
                    continue

        return None

    def _convert_type_info_to_symbol_def(
        self, type_info: Dict[str, Any], file_path: str
    ) -> SymbolDef:
        """Convert type information to SymbolDef."""
        decl = type_info["declaration"]
        return {
            "symbol": decl["name"],
            "kind": type_info["type"],
            "language": self.lang,
            "signature": decl.get("signature", decl["name"]),
            "doc": decl.get("doc"),
            "defined_in": file_path,
            "line": decl["line"],
            "span": decl.get("span", (decl["line"], decl["line"] + 1)),
            "type": decl.get("type_definition"),
            "type_parameters": decl.get("type_parameters"),
            "parameters": decl.get("parameters"),
            "return_type": decl.get("return_type"),
        }

    def findReferences(self, symbol: str) -> list[Reference]:
        """Find all references to a symbol with enhanced TypeScript support."""
        refs: List[Reference] = []
        seen: Set[Tuple[str, int]] = set()

        patterns = ["*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cjs", "*.d.ts"]

        for pattern in patterns:
            for path in Path(".").rglob(pattern):
                try:
                    # Skip excluded directories
                    if any(
                        part in path.parts for part in ["node_modules", "dist", "build", ".next"]
                    ):
                        continue

                    if not self._tsconfig_parser.is_file_included(path):
                        continue

                    content = path.read_text(encoding="utf-8")

                    # Enhanced reference search considering TypeScript syntax
                    lines = content.splitlines()
                    for i, line in enumerate(lines):
                        # Look for whole word matches
                        import re

                        # Standard identifier references
                        pattern_std = r"\b" + re.escape(symbol) + r"\b"
                        if re.search(pattern_std, line):
                            line_no = i + 1
                            key = (str(path), line_no)
                            if key not in seen:
                                refs.append(Reference(file=str(path), line=line_no))
                                seen.add(key)

                        # Type references (e.g., : Symbol, <Symbol>)
                        pattern_type = (
                            r":\s*" + re.escape(symbol) + r"\b|<" + re.escape(symbol) + r">"
                        )
                        if re.search(pattern_type, line):
                            line_no = i + 1
                            key = (str(path), line_no)
                            if key not in seen:
                                refs.append(Reference(file=str(path), line=line_no))
                                seen.add(key)

                        # Import/export references
                        pattern_import = r"(?:import|export).*\b" + re.escape(symbol) + r"\b"
                        if re.search(pattern_import, line):
                            line_no = i + 1
                            key = (str(path), line_no)
                            if key not in seen:
                                refs.append(Reference(file=str(path), line=line_no))
                                seen.add(key)

                except Exception:
                    continue

        return refs

    def search(self, query: str, opts: SearchOpts | None = None) -> Iterable[SearchResult]:
        """Search for code snippets with enhanced TypeScript support."""
        limit = 20
        if opts and "limit" in opts:
            limit = opts["limit"]

        # Semantic search using type information if available
        if opts and opts.get("semantic") and self._semantic_indexer:
            try:
                results = self._semantic_indexer.search(query, limit=limit)
                for result in results:
                    # Extract snippet from the source file
                    try:
                        file_path = Path(result["file"])
                        if file_path.exists():
                            content = file_path.read_text(encoding="utf-8")
                            lines = content.splitlines()
                            line_num = result["line"]

                            # Get context around the symbol
                            start_line = max(0, line_num - 3)
                            end_line = min(len(lines), line_num + 3)
                            snippet_lines = lines[start_line:end_line]
                            snippet = "\n".join(snippet_lines)

                            yield SearchResult(
                                file=result["file"],
                                start_line=line_num,
                                end_line=line_num,
                                snippet=snippet,
                            )
                    except Exception as e:
                        logger.debug(
                            f"Error creating snippet for {result.get('file', 'unknown')}: {e}"
                        )
                        continue
                return
            except Exception as e:
                logger.debug(f"Semantic search failed: {e}")
                # Fall back to fuzzy search

        # Use fuzzy indexer for non-semantic search
        yield from self._indexer.search(query, limit=limit)

    def get_indexed_count(self) -> int:
        """Return the number of indexed files."""
        return len(self._symbol_cache)

    def get_type_information(self, symbol: str, file_path: Path) -> Optional[Dict[str, Any]]:
        """Get comprehensive type information for a symbol."""
        # Check type engine first
        symbol_type = self._type_engine.get_symbol_type(symbol, file_path)
        if symbol_type:
            type_info = {"type": symbol_type, "source": "inference"}

            # Add additional information from type definitions
            type_def = self._type_engine.get_type_definition(symbol_type)
            if type_def:
                type_info["definition"] = type_def

            return type_info

        # Check declaration cache
        file_key = str(file_path)
        if file_key in self._declaration_cache:
            decl_info = self._declaration_handler.get_type_information(symbol, file_path)
            if decl_info:
                return decl_info

        return None

    def resolve_module_import(self, module_name: str, from_file: Path) -> Optional[Path]:
        """Resolve a module import to its file path."""
        return self._tsconfig_parser.resolve_module_path(module_name, from_file)

    def get_compiler_options(self, file_path: Path) -> Dict[str, Any]:
        """Get TypeScript compiler options for a file."""
        return self._tsconfig_parser.get_compiler_options(file_path)

    def analyze_type_compatibility(self, source_type: str, target_type: str) -> bool:
        """Analyze if source type is compatible with target type."""
        return self._type_engine.analyze_type_compatibility(source_type, target_type)
