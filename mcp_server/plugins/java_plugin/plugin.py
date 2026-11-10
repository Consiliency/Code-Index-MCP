"""Java language plugin for comprehensive code analysis."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import javalang

from mcp_server.plugin_base import (
    IndexShard,
    Reference,
    SearchOpts,
    SearchResult,
    SymbolDef,
)
from mcp_server.plugins.specialized_plugin_base import (
    CrossFileReference,
    IBuildSystemIntegration,
    ICrossFileAnalyzer,
    IImportResolver,
    ImportInfo,
    ITypeAnalyzer,
    SpecializedPluginBase,
)
from mcp_server.storage.sqlite_store import SQLiteStore

from .build_system import JavaBuildSystemIntegration
from .import_resolver import JavaImportInfo, JavaImportResolver
from .type_analyzer import JavaTypeAnalyzer, JavaTypeInfo

logger = logging.getLogger(__name__)


class JavaCrossFileAnalyzer(ICrossFileAnalyzer):
    """Cross-file reference analyzer for Java."""

    def __init__(self, plugin: "Plugin"):
        self.plugin = plugin
        self.reference_cache: Dict[str, List[CrossFileReference]] = {}

    def find_all_references(self, symbol: str, definition_file: str) -> List[CrossFileReference]:
        """Find all references to a symbol across files."""
        if symbol in self.reference_cache:
            return self.reference_cache[symbol]

        references = []

        # Search in all indexed files
        for file_path, symbols in self.plugin._file_symbols.items():
            if file_path == definition_file:
                continue

            try:
                content = Path(file_path).read_text(encoding="utf-8")

                # Parse the file
                tree = javalang.parse.parse(content)

                # Find method calls
                for _, node in tree.filter(javalang.tree.MethodInvocation):
                    if node.member == symbol:
                        line_no = self._get_line_number(content, node.position)
                        references.append(
                            CrossFileReference(
                                symbol=symbol,
                                source_file=file_path,
                                target_file=definition_file,
                                line_number=line_no,
                                reference_type="call",
                            )
                        )

                # Find type references
                for _, node in tree.filter(javalang.tree.ReferenceType):
                    if node.name == symbol:
                        line_no = self._get_line_number(content, node.position)
                        references.append(
                            CrossFileReference(
                                symbol=symbol,
                                source_file=file_path,
                                target_file=definition_file,
                                line_number=line_no,
                                reference_type="type",
                            )
                        )

                # Find inheritance
                for _, node in tree.filter(javalang.tree.ClassDeclaration):
                    if node.extends and node.extends.name == symbol:
                        line_no = self._get_line_number(content, node.position)
                        references.append(
                            CrossFileReference(
                                symbol=symbol,
                                source_file=file_path,
                                target_file=definition_file,
                                line_number=line_no,
                                reference_type="inherit",
                            )
                        )

                    if node.implements:
                        for impl in node.implements:
                            if impl.name == symbol:
                                line_no = self._get_line_number(content, node.position)
                                references.append(
                                    CrossFileReference(
                                        symbol=symbol,
                                        source_file=file_path,
                                        target_file=definition_file,
                                        line_number=line_no,
                                        reference_type="implement",
                                    )
                                )

            except Exception as e:
                logger.warning(f"Failed to analyze references in {file_path}: {e}")

        self.reference_cache[symbol] = references
        return references

    def _get_line_number(self, content: str, position: tuple) -> int:
        """Convert javalang position to line number."""
        if not position:
            return 0
        return position[0] if isinstance(position, tuple) else 0

    def get_call_graph(self, function_name: str) -> Dict[str, Set[str]]:
        """Get the call graph for a function."""
        call_graph = {}
        visited = set()  # Track visited functions to prevent infinite recursion

        def _build_graph(func_name: str):
            if func_name in visited:
                return
            visited.add(func_name)

            # Find all calls from this function
            definition = self.plugin.getDefinition(func_name)
            if not definition:
                return

            file_path = definition.get("defined_in")
            if not file_path:
                return

            try:
                content = Path(file_path).read_text(encoding="utf-8")
                tree = javalang.parse.parse(content)

                # Find the method declaration
                for _, method in tree.filter(javalang.tree.MethodDeclaration):
                    if method.name == func_name:
                        calls = set()

                        # Find all method calls within this method
                        for _, call in method.filter(javalang.tree.MethodInvocation):
                            calls.add(call.member)

                        call_graph[func_name] = calls

                        # Recursively build graph
                        for called_func in calls:
                            if called_func not in visited:
                                _build_graph(called_func)

            except Exception as e:
                logger.warning(f"Failed to build call graph for {func_name}: {e}")

        _build_graph(function_name)
        return call_graph

    def analyze_impact(self, file_path: str) -> Dict[str, List[str]]:
        """Analyze impact of changes to a file."""
        impact = {
            "direct_dependents": [],
            "transitive_dependents": [],
            "affected_tests": [],
        }

        # Get all symbols defined in this file
        symbols = self.plugin._file_symbols.get(file_path, [])

        # Find files that reference these symbols
        dependent_files = set()

        for symbol in symbols:
            refs = self.find_all_references(symbol["symbol"], file_path)
            for ref in refs:
                dependent_files.add(ref.source_file)

                # Check if it's a test file
                if "test" in ref.source_file.lower():
                    impact["affected_tests"].append(ref.source_file)

        impact["direct_dependents"] = list(dependent_files)

        # Find transitive dependents (simplified)
        transitive = set()
        for dep_file in dependent_files:
            sub_impact = self.analyze_impact(dep_file)
            transitive.update(sub_impact["direct_dependents"])

        impact["transitive_dependents"] = list(transitive - dependent_files)

        return impact


class Plugin(SpecializedPluginBase):
    """Java language plugin with full support for Java ecosystem."""

    def __init__(
        self, sqlite_store: Optional[SQLiteStore] = None, enable_semantic: bool = False
    ) -> None:
        # Java language configuration
        language_config = {
            "code": "java",
            "name": "Java",
            "extensions": [".java"],
            "symbols": [
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
                "method_declaration",
                "field_declaration",
                "constructor_declaration",
            ],
        }

        # Additional Java-specific state - initialize BEFORE calling super().__init__
        self._file_symbols: Dict[str, List[Dict]] = {}
        self._package_structure: Dict[str, Set[str]] = {}
        self._import_statements: Dict[str, List[JavaImportInfo]] = {}

        # Initialize base class
        super().__init__(language_config, sqlite_store, enable_semantic)

        # Pre-index Java files
        self._preindex()

    def _create_import_resolver(self) -> IImportResolver:
        """Create Java import resolver."""
        return JavaImportResolver(self._project_root or Path.cwd())

    def _create_type_analyzer(self) -> ITypeAnalyzer:
        """Create Java type analyzer."""
        return JavaTypeAnalyzer(self._project_root or Path.cwd())

    def _create_build_system(self) -> IBuildSystemIntegration:
        """Create Java build system integration."""
        return JavaBuildSystemIntegration(self._project_root or Path.cwd())

    def _create_cross_file_analyzer(self) -> ICrossFileAnalyzer:
        """Create Java cross-file analyzer."""
        return JavaCrossFileAnalyzer(self)

    def _preindex(self) -> None:
        """Pre-index all Java files in the project."""
        self._project_root = Path.cwd()

        # Discover build files
        self._discover_build_files()

        # Index Java files
        for java_file in self._project_root.rglob("*.java"):
            try:
                if not self._should_index_file(java_file):
                    continue

                content = java_file.read_text(encoding="utf-8")
                self.indexFile(str(java_file), content)

            except Exception as e:
                logger.warning(f"Failed to pre-index {java_file}: {e}")

    def _should_index_file(self, file_path: Path) -> bool:
        """Check if file should be indexed."""
        # Skip common non-source directories
        skip_dirs = {"target", "build", ".gradle", "out", "bin", ".idea", ".settings"}

        for parent in file_path.parents:
            if parent.name in skip_dirs:
                return False

        return True

    def _discover_build_files(self):
        """Discover build files in the project."""
        self._build_files = []

        # Look for Maven POM
        pom = self._project_root / "pom.xml"
        if pom.exists():
            self._build_files.append(pom)

            # Also check for multi-module POMs
            for child_pom in self._project_root.glob("*/pom.xml"):
                self._build_files.append(child_pom)

        # Look for Gradle build files
        for gradle_file in ["build.gradle", "build.gradle.kts"]:
            build_file = self._project_root / gradle_file
            if build_file.exists():
                self._build_files.append(build_file)

                # Check for multi-module builds
                for child_build in self._project_root.glob(f"*/{gradle_file}"):
                    self._build_files.append(child_build)

        # Look for settings files
        for settings_file in ["settings.gradle", "settings.gradle.kts"]:
            if (self._project_root / settings_file).exists():
                self._build_files.append(self._project_root / settings_file)

    def supports(self, path: str | Path) -> bool:
        """Check if this plugin supports the given file."""
        path = Path(path)
        return path.suffix == ".java"

    def indexFile(self, path: str | Path, content: str) -> IndexShard:
        """Index a Java file."""
        path = str(path)
        symbols = []

        try:
            # Parse with javalang
            tree = javalang.parse.parse(content)

            # Extract package
            package = tree.package.name if tree.package else ""

            # Index imports
            imports = []
            for import_decl in tree.imports:
                import_info = JavaImportInfo(
                    module_path=import_decl.path,
                    is_static=import_decl.static,
                    is_wildcard=import_decl.wildcard,
                    line_number=import_decl.position[0] if import_decl.position else 0,
                )
                imports.append(import_info)

                # Add to import resolver (only if it's been created)
                if hasattr(self, "_import_resolver") and self._import_resolver:
                    self.import_resolver.add_import(path, import_info)

            self._import_statements[path] = imports

            # Index classes
            for _, node in tree.filter(javalang.tree.ClassDeclaration):
                symbol = self._create_symbol_from_class(node, package, path)
                symbols.append(symbol)

                # Analyze type (only if type analyzer is available)
                if hasattr(self, "_type_analyzer") and self._type_analyzer:
                    self.type_analyzer.analyze_file(path, content)

            # Index interfaces
            for _, node in tree.filter(javalang.tree.InterfaceDeclaration):
                symbol = self._create_symbol_from_interface(node, package, path)
                symbols.append(symbol)

            # Index methods
            for _, node in tree.filter(javalang.tree.MethodDeclaration):
                symbol = self._create_symbol_from_method(node, package, path)
                symbols.append(symbol)

            # Index fields
            for _, node in tree.filter(javalang.tree.FieldDeclaration):
                for declarator in node.declarators:
                    symbol = self._create_symbol_from_field(node, declarator, package, path)
                    symbols.append(symbol)

        except Exception as e:
            logger.warning(f"Failed to parse {path} with javalang: {e}")
            # Fallback to regex-based parsing
            symbols = self._fallback_index(content, path)

        # Store symbols for cross-reference
        self._file_symbols[path] = symbols

        # Update package structure
        if package:
            if package not in self._package_structure:
                self._package_structure[package] = set()
            self._package_structure[package].add(path)

        # Also use base class indexing for text search
        _ = super().indexFile(path, content)

        return IndexShard(file=path, symbols=symbols, language="java")

    def _create_symbol_from_class(self, node, package: str, file_path: str) -> Dict:
        """Create symbol definition from class declaration."""
        full_name = f"{package}.{node.name}" if package else node.name

        # Build signature
        signature_parts = []

        # Add modifiers
        if node.modifiers:
            signature_parts.extend(node.modifiers)

        signature_parts.append("class")
        signature_parts.append(node.name)

        # Add generics
        if node.type_parameters:
            params = [p.name for p in node.type_parameters]
            signature_parts.append(f"<{', '.join(params)}>")

        # Add extends
        if node.extends:
            signature_parts.append("extends")
            signature_parts.append(node.extends.name)

        # Add implements
        if node.implements:
            signature_parts.append("implements")
            impl_names = [i.name for i in node.implements]
            signature_parts.append(", ".join(impl_names))

        return {
            "symbol": full_name,
            "kind": "class",
            "language": "java",
            "signature": " ".join(signature_parts),
            "doc": self._extract_javadoc(node),
            "defined_in": file_path,
            "line": node.position[0] if node.position else 0,
            "span": ((node.position[0], node.position[0] + 1) if node.position else (0, 1)),
        }

    def _create_symbol_from_interface(self, node, package: str, file_path: str) -> Dict:
        """Create symbol definition from interface declaration."""
        full_name = f"{package}.{node.name}" if package else node.name

        signature_parts = []
        if node.modifiers:
            signature_parts.extend(node.modifiers)

        signature_parts.append("interface")
        signature_parts.append(node.name)

        if node.type_parameters:
            params = [p.name for p in node.type_parameters]
            signature_parts.append(f"<{', '.join(params)}>")

        if node.extends:
            signature_parts.append("extends")
            extends_names = [e.name for e in node.extends]
            signature_parts.append(", ".join(extends_names))

        return {
            "symbol": full_name,
            "kind": "interface",
            "language": "java",
            "signature": " ".join(signature_parts),
            "doc": self._extract_javadoc(node),
            "defined_in": file_path,
            "line": node.position[0] if node.position else 0,
            "span": ((node.position[0], node.position[0] + 1) if node.position else (0, 1)),
        }

    def _create_symbol_from_method(self, node, package: str, file_path: str) -> Dict:
        """Create symbol definition from method declaration."""
        # Get containing class
        parent_class = self._find_parent_type(node)
        class_name = parent_class.name if parent_class else ""

        full_name = (
            f"{package}.{class_name}.{node.name}" if package else f"{class_name}.{node.name}"
        )

        # Build signature
        signature_parts = []

        if node.modifiers:
            signature_parts.extend(node.modifiers)

        # Return type
        if node.return_type:
            signature_parts.append(self._type_to_string(node.return_type))
        else:
            signature_parts.append("void")

        signature_parts.append(node.name)

        # Parameters
        params = []
        if node.parameters:
            for param in node.parameters:
                param_str = f"{self._type_to_string(param.type)} {param.name}"
                params.append(param_str)

        signature_parts.append(f"({', '.join(params)})")

        # Throws
        if node.throws:
            signature_parts.append("throws")
            throws = [t.name for t in node.throws]
            signature_parts.append(", ".join(throws))

        return {
            "symbol": node.name,  # Use simple name for methods
            "kind": "method",
            "language": "java",
            "signature": " ".join(signature_parts),
            "doc": self._extract_javadoc(node),
            "defined_in": file_path,
            "line": node.position[0] if node.position else 0,
            "span": ((node.position[0], node.position[0] + 1) if node.position else (0, 1)),
            "full_name": full_name,
        }

    def _create_symbol_from_field(self, node, declarator, package: str, file_path: str) -> Dict:
        """Create symbol definition from field declaration."""
        # Get containing class
        parent_class = self._find_parent_type(node)
        class_name = parent_class.name if parent_class else ""

        full_name = (
            f"{package}.{class_name}.{declarator.name}"
            if package
            else f"{class_name}.{declarator.name}"
        )

        # Build signature
        signature_parts = []

        if node.modifiers:
            signature_parts.extend(node.modifiers)

        signature_parts.append(self._type_to_string(node.type))
        signature_parts.append(declarator.name)

        return {
            "symbol": declarator.name,
            "kind": "field",
            "language": "java",
            "signature": " ".join(signature_parts),
            "doc": self._extract_javadoc(node),
            "defined_in": file_path,
            "line": node.position[0] if node.position else 0,
            "span": ((node.position[0], node.position[0] + 1) if node.position else (0, 1)),
            "full_name": full_name,
        }

    def _find_parent_type(self, node) -> Optional[Any]:
        """Find the parent class/interface of a node."""
        # This is simplified - would need proper AST traversal
        return None

    def _type_to_string(self, type_node) -> str:
        """Convert type node to string representation."""
        if hasattr(type_node, "name"):
            base = type_node.name

            # Handle generic arguments
            if hasattr(type_node, "arguments") and type_node.arguments:
                args = [self._type_to_string(arg) for arg in type_node.arguments]
                base += f"<{', '.join(args)}>"

            # Handle array dimensions
            if hasattr(type_node, "dimensions") and type_node.dimensions:
                base += "[]" * len(type_node.dimensions)

            return base

        return str(type_node)

    def _extract_javadoc(self, node) -> Optional[str]:
        """Extract Javadoc comment for a node."""
        # Simplified - would need to parse actual Javadoc comments
        return None

    def _fallback_index(self, content: str, file_path: str) -> List[Dict]:
        """Fallback regex-based indexing."""
        symbols = []

        # Extract package
        package_match = re.search(r"package\s+([\w.]+)\s*;", content)
        package = package_match.group(1) if package_match else ""

        # Find class declarations
        class_pattern = r"(?:public\s+)?(?:abstract\s+)?(?:final\s+)?class\s+(\w+)"
        for match in re.finditer(class_pattern, content):
            class_name = match.group(1)
            full_name = f"{package}.{class_name}" if package else class_name

            symbols.append(
                {
                    "symbol": full_name,
                    "kind": "class",
                    "language": "java",
                    "signature": match.group(0),
                    "doc": None,
                    "defined_in": file_path,
                    "line": content[: match.start()].count("\n") + 1,
                    "span": (
                        content[: match.start()].count("\n") + 1,
                        content[: match.end()].count("\n") + 1,
                    ),
                }
            )

        # Find method declarations
        method_pattern = r"(?:public|private|protected)?\s*(?:static)?\s*(?:final)?\s*(?:synchronized)?\s*(?:[\w<>,\s]+)\s+(\w+)\s*\([^)]*\)"
        for match in re.finditer(method_pattern, content):
            method_name = match.group(1)

            # Skip constructors and keywords
            if method_name in ["if", "while", "for", "switch", "catch", "try"]:
                continue

            symbols.append(
                {
                    "symbol": method_name,
                    "kind": "method",
                    "language": "java",
                    "signature": match.group(0).strip(),
                    "doc": None,
                    "defined_in": file_path,
                    "line": content[: match.start()].count("\n") + 1,
                    "span": (
                        content[: match.start()].count("\n") + 1,
                        content[: match.end()].count("\n") + 1,
                    ),
                }
            )

        return symbols

    def getDefinition(self, symbol: str) -> Optional[SymbolDef]:
        """Get enhanced definition with Java-specific information."""
        definition = super().getDefinition(symbol)

        if definition:
            # Enhance with Java-specific info
            type_info = self.type_analyzer.get_type_info(symbol, definition.get("defined_in", ""))

            if type_info and isinstance(type_info, JavaTypeInfo):
                definition["java_info"] = {
                    "access": type_info.access_modifier,
                    "modifiers": {
                        "static": type_info.is_static,
                        "final": type_info.is_final,
                        "abstract": type_info.is_abstract,
                    },
                    "implements": type_info.implements,
                    "annotations": type_info.annotations,
                }

        return definition

    def findReferences(self, symbol: str) -> Iterable[Reference]:
        """Find all references including cross-file references."""
        references = list(super().findReferences(symbol))

        # Add cross-file references
        definition = self.getDefinition(symbol)
        if definition:
            cross_refs = self.cross_file_analyzer.find_all_references(
                symbol, definition.get("defined_in", "")
            )

            for ref in cross_refs:
                references.append(Reference(file=ref.source_file, line=ref.line_number))

        return references

    def search(self, query: str, opts: SearchOpts | None = None) -> Iterable[SearchResult]:
        """Enhanced search with Java-aware features."""
        # First use base search
        results = list(super().search(query, opts))

        # If query looks like a qualified name, try exact match
        if "." in query:
            # Try as fully qualified class name
            if query in self.type_analyzer.type_registry:
                _ = self.type_analyzer.type_registry[query]

                # Find file containing this type
                for file_path, symbols in self._file_symbols.items():
                    for symbol in symbols:
                        if symbol["symbol"] == query:
                            results.insert(
                                0,
                                SearchResult(
                                    file=file_path,
                                    start_line=symbol["line"],
                                    end_line=symbol["line"],
                                    snippet=symbol["signature"],
                                ),
                            )
                            break

        return results

    def analyze_imports(self, file_path: Path) -> List[ImportInfo]:
        """Analyze imports in a Java file."""
        str_path = str(file_path)
        return self._import_statements.get(str_path, [])

    def get_package_structure(self) -> Dict[str, Set[str]]:
        """Get the package structure of the project."""
        return self._package_structure.copy()

    def get_class_hierarchy(self, class_name: str) -> Dict[str, Any]:
        """Get the complete class hierarchy for a given class."""
        hierarchy = {
            "class": class_name,
            "extends": [],
            "implements": [],
            "extended_by": [],
            "implemented_by": [],
        }

        type_info = self.type_analyzer.get_type_info(class_name, "")
        if type_info:
            hierarchy["extends"] = type_info.super_types

            if isinstance(type_info, JavaTypeInfo):
                hierarchy["implements"] = type_info.implements

        # Find classes that extend this one
        for type_name, info in self.type_analyzer.type_registry.items():
            if class_name in info.super_types:
                hierarchy["extended_by"].append(type_name)

            if isinstance(info, JavaTypeInfo) and class_name in info.implements:
                hierarchy["implemented_by"].append(type_name)

        return hierarchy
