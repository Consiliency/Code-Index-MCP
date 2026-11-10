"""C# plugin with comprehensive language support including generics, async/await, and LINQ."""

from __future__ import annotations

import logging
import os
import re
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
from .namespace_resolver import NamespaceResolver
from .nuget_integration import NuGetIntegration
from .type_analyzer import TypeAnalyzer

logger = logging.getLogger(__name__)


class Plugin(PluginWithSemanticSearch):
    """C# plugin with namespace resolution, type system analysis, and NuGet integration."""

    lang = "csharp"

    def __init__(
        self, sqlite_store: Optional[SQLiteStore] = None, enable_semantic: bool = False
    ) -> None:
        """Initialize the C# plugin with all analysis components."""

        # Initialize enhanced base class
        super().__init__(sqlite_store=sqlite_store, enable_semantic=enable_semantic)
        self._sqlite_store = sqlite_store

        # Initialize components
        self._indexer = FuzzyIndexer(sqlite_store=sqlite_store)
        self._namespace_resolver = NamespaceResolver()
        self._type_analyzer = TypeAnalyzer()
        self._nuget_integration = NuGetIntegration()
        self._repository_id = None

        # Caches for analysis results
        self._file_analysis_cache: Dict[str, Dict[str, Any]] = {}
        self._project_cache: Dict[str, Dict[str, Any]] = {}

        # Create or get repository if SQLite is enabled
        if self._sqlite_store:
            try:
                self._repository_id = self._sqlite_store.create_repository(
                    str(Path.cwd()), Path.cwd().name, {"language": "csharp"}
                )
            except Exception as e:
                logger.warning(f"Failed to create repository: {e}")
                self._repository_id = None

        # Pre-index existing files
        if os.getenv("MCP_SKIP_PLUGIN_PREINDEX", "false").lower() != "true":
            self._preindex()

    def _preindex(self) -> None:
        """Pre-index C# files and analyze projects."""
        logger.info("Pre-indexing C# files...")

        # Index .cs files
        for path in Path(".").rglob("*.cs"):
            try:
                text = path.read_text(encoding="utf-8")
                self._indexer.add_file(str(path), text)
            except Exception as e:
                logger.debug(f"Failed to pre-index {path}: {e}")
                continue

        # Analyze project files
        for project_path in Path(".").rglob("*.csproj"):
            try:
                self._analyze_project(str(project_path))
            except Exception as e:
                logger.debug(f"Failed to analyze project {project_path}: {e}")
                continue

        logger.info(f"Pre-indexed {self.get_indexed_count()} C# files")

    def supports(self, path: str | Path) -> bool:
        """Check if this plugin supports the given file."""
        path_obj = Path(path)
        return path_obj.suffix.lower() in {
            ".cs",
            ".csx",
            ".csproj",
            ".props",
            ".targets",
        }

    def indexFile(self, path: str | Path, content: str) -> IndexShard:
        """Index a C# file with comprehensive analysis."""
        if isinstance(path, str):
            path = Path(path)

        # Add to fuzzy indexer
        self._indexer.add_file(str(path), content)

        # Determine file type and analyze accordingly
        if path.suffix.lower() == ".csproj":
            return self._index_project_file(path, content)
        else:
            return self._index_source_file(path, content)

    def _index_source_file(self, path: Path, content: str) -> IndexShard:
        """Index a C# source file (.cs)."""
        symbols = []

        try:
            # Perform comprehensive analysis
            namespace_analysis = self._namespace_resolver.analyze_file(str(path), content)
            type_analysis = self._type_analyzer.analyze_types(content, str(path))

            # Cache analysis results
            self._file_analysis_cache[str(path)] = {
                "namespace": namespace_analysis,
                "types": type_analysis,
                "last_modified": path.stat().st_mtime if path.exists() else 0,
            }

            # Extract symbols from type analysis
            symbols.extend(self._extract_type_symbols(type_analysis))
            symbols.extend(self._extract_method_symbols(type_analysis))
            symbols.extend(self._extract_property_symbols(type_analysis))
            symbols.extend(self._extract_field_symbols(type_analysis))
            symbols.extend(self._extract_event_symbols(type_analysis))

            # Add namespace and using information as symbols
            if namespace_analysis["namespace"]:
                symbols.append(
                    {
                        "symbol": namespace_analysis["namespace"],
                        "kind": "namespace",
                        "line": 1,
                        "signature": f"namespace {namespace_analysis['namespace']}",
                        "doc": f"Namespace declaration for {namespace_analysis['namespace']}",
                    }
                )

            # Store file in SQLite if available
            file_id = None
            if self._sqlite_store and self._repository_id:
                import hashlib

                file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                file_id = self._sqlite_store.store_file(
                    self._repository_id,
                    str(path),
                    str(path.relative_to(Path.cwd()) if path.is_absolute() else path),
                    language="csharp",
                    size=len(content),
                    hash=file_hash,
                )

                # Store symbols in SQLite
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
                                documentation=symbol.get("doc"),
                            )

                            # Add metadata to fuzzy indexer
                            self._indexer.add_symbol(
                                symbol["symbol"],
                                str(path),
                                symbol["line"],
                                {
                                    "symbol_id": symbol_id,
                                    "file_id": file_id,
                                    "kind": symbol["kind"],
                                },
                            )
                        except Exception as e:
                            logger.error(f"Failed to store symbol {symbol['symbol']}: {e}")

            # Create semantic embeddings if enabled
            if self._enable_semantic and symbols:
                self.index_with_embeddings(path, content, symbols)

        except Exception as e:
            logger.error(f"Error indexing C# file {path}: {e}")
            # Fallback to basic symbol extraction
            symbols = self._extract_basic_symbols(content)

        return IndexShard(file=str(path), symbols=symbols, language=self.lang)

    def _index_project_file(self, path: Path, content: str) -> IndexShard:
        """Index a C# project file (.csproj)."""
        symbols = []

        try:
            # Analyze project and its packages
            project_analysis = self._analyze_project(str(path))

            # Create symbols for project metadata
            if project_analysis.get("target_framework"):
                symbols.append(
                    {
                        "symbol": f"TargetFramework:{project_analysis['target_framework']}",
                        "kind": "project_setting",
                        "line": 1,
                        "signature": f"<TargetFramework>{project_analysis['target_framework']}</TargetFramework>",
                        "doc": f"Target framework: {project_analysis['target_framework']}",
                    }
                )

            # Add package references as symbols
            for package in project_analysis.get("packages", []):
                symbols.append(
                    {
                        "symbol": package["name"],
                        "kind": "package_reference",
                        "line": 1,
                        "signature": f"<PackageReference Include=\"{package['name']}\" Version=\"{package['version']}\" />",
                        "doc": f"NuGet package: {package['name']} v{package['version']}",
                    }
                )

        except Exception as e:
            logger.error(f"Error indexing project file {path}: {e}")

        return IndexShard(file=str(path), symbols=symbols, language=self.lang)

    def _analyze_project(self, project_path: str) -> Dict[str, Any]:
        """Analyze a C# project file and its dependencies."""
        if project_path in self._project_cache:
            return self._project_cache[project_path]

        # Analyze NuGet packages
        package_analysis = self._nuget_integration.analyze_project_packages(project_path)

        # Analyze namespace resolution within project
        namespace_analysis = self._namespace_resolver.analyze_project_file(project_path)

        project_analysis = {
            "project_path": project_path,
            "target_framework": package_analysis.get("target_framework"),
            "packages": package_analysis.get("packages", []),
            "package_namespaces": list(package_analysis.get("package_namespaces", set())),
            "project_references": namespace_analysis.get("project_references", []),
            "global_usings": namespace_analysis.get("global_usings", []),
        }

        # Cache the results
        self._project_cache[project_path] = project_analysis

        return project_analysis

    def _extract_type_symbols(self, type_analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract type symbols from type analysis."""
        symbols = []

        for type_info in type_analysis.get("types", []):
            symbol = {
                "symbol": type_info["name"],
                "kind": type_info["kind"],
                "line": type_info["line"],
                "end_line": type_info.get("end_line", type_info["line"]),
                "signature": type_info["signature"],
                "doc": self._generate_type_documentation(type_info),
            }
            symbols.append(symbol)

        return symbols

    def _extract_method_symbols(self, type_analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract method symbols from type analysis."""
        symbols = []

        for method_info in type_analysis.get("methods", []):
            symbol = {
                "symbol": method_info["name"],
                "kind": "method",
                "line": method_info["line"],
                "signature": method_info["signature"],
                "doc": self._generate_method_documentation(method_info),
            }
            symbols.append(symbol)

        return symbols

    def _extract_property_symbols(self, type_analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract property symbols from type analysis."""
        symbols = []

        for prop_info in type_analysis.get("properties", []):
            symbol = {
                "symbol": prop_info["name"],
                "kind": "property",
                "line": prop_info["line"],
                "signature": prop_info["signature"],
                "doc": self._generate_property_documentation(prop_info),
            }
            symbols.append(symbol)

        return symbols

    def _extract_field_symbols(self, type_analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract field symbols from type analysis."""
        symbols = []

        for field_info in type_analysis.get("fields", []):
            symbol = {
                "symbol": field_info["name"],
                "kind": "field",
                "line": field_info["line"],
                "signature": field_info["signature"],
                "doc": f"Field of type {field_info['type']}",
            }
            symbols.append(symbol)

        return symbols

    def _extract_event_symbols(self, type_analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract event symbols from type analysis."""
        symbols = []

        for event_info in type_analysis.get("events", []):
            symbol = {
                "symbol": event_info["name"],
                "kind": "event",
                "line": event_info["line"],
                "signature": event_info["signature"],
                "doc": f"Event of type {event_info['type']}",
            }
            symbols.append(symbol)

        return symbols

    def _generate_type_documentation(self, type_info: Dict[str, Any]) -> str:
        """Generate documentation for a type."""
        doc_parts = [f"{type_info['kind'].title()}: {type_info['name']}"]

        if type_info.get("is_generic") and type_info.get("generic_parameters"):
            generic_params = []
            for param in type_info["generic_parameters"]:
                param_str = param["name"]
                if param.get("variance"):
                    param_str = f"{param['variance']} {param_str}"
                if param.get("constraints"):
                    constraints = [
                        c["type"] if c["type"] != "type" else c["value"]
                        for c in param["constraints"]
                    ]
                    if constraints:
                        param_str += f" : {', '.join(constraints)}"
                generic_params.append(param_str)

            doc_parts.append(f"Generic parameters: <{', '.join(generic_params)}>")

        if type_info.get("base_type"):
            doc_parts.append(f"Inherits from: {type_info['base_type']}")

        if type_info.get("implemented_interfaces"):
            doc_parts.append(f"Implements: {', '.join(type_info['implemented_interfaces'])}")

        if type_info.get("modifiers"):
            doc_parts.append(f"Modifiers: {', '.join(type_info['modifiers'])}")

        return " | ".join(doc_parts)

    def _generate_method_documentation(self, method_info: Dict[str, Any]) -> str:
        """Generate documentation for a method."""
        doc_parts = [f"Method: {method_info['name']}"]

        if method_info.get("return_type"):
            doc_parts.append(f"Returns: {method_info['return_type']}")

        if method_info.get("parameters"):
            param_strs = []
            for param in method_info["parameters"]:
                param_str = f"{param.get('type', 'object')} {param.get('name', 'param')}"
                if param.get("modifiers"):
                    param_str = f"{' '.join(param['modifiers'])} {param_str}"
                if param.get("default_value"):
                    param_str += f" = {param['default_value']}"
                param_strs.append(param_str)
            doc_parts.append(f"Parameters: ({', '.join(param_strs)})")

        if method_info.get("is_async"):
            doc_parts.append("Async method")

        if method_info.get("generic_parameters"):
            generic_params = [p["name"] for p in method_info["generic_parameters"]]
            doc_parts.append(f"Generic: <{', '.join(generic_params)}>")

        if method_info.get("modifiers"):
            doc_parts.append(f"Modifiers: {', '.join(method_info['modifiers'])}")

        return " | ".join(doc_parts)

    def _generate_property_documentation(self, prop_info: Dict[str, Any]) -> str:
        """Generate documentation for a property."""
        doc_parts = [f"Property: {prop_info['name']} : {prop_info['type']}"]

        access_parts = []
        if prop_info.get("has_getter"):
            access_parts.append("get")
        if prop_info.get("has_setter"):
            access_parts.append("set")

        if access_parts:
            doc_parts.append(f"Accessors: {{{'; '.join(access_parts)};}}")

        if prop_info.get("is_auto_property"):
            doc_parts.append("Auto-property")

        if prop_info.get("modifiers"):
            doc_parts.append(f"Modifiers: {', '.join(prop_info['modifiers'])}")

        return " | ".join(doc_parts)

    def _extract_basic_symbols(self, content: str) -> List[Dict[str, Any]]:
        """Fallback method for basic symbol extraction."""
        symbols = []
        lines = content.split("\n")

        # Basic patterns for C# symbols
        patterns = [
            (r"^\s*(?:public|private|protected|internal)?\s*class\s+(\w+)", "class"),
            (
                r"^\s*(?:public|private|protected|internal)?\s*interface\s+(\w+)",
                "interface",
            ),
            (r"^\s*(?:public|private|protected|internal)?\s*struct\s+(\w+)", "struct"),
            (r"^\s*(?:public|private|protected|internal)?\s*enum\s+(\w+)", "enum"),
            (
                r"^\s*(?:public|private|protected|internal)?\s*delegate\s+.+\s+(\w+)",
                "delegate",
            ),
            (
                r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?(?:async\s+)?\w+\s+(\w+)\s*\(",
                "method",
            ),
            (
                r"^\s*(?:public|private|protected|internal)?\s*\w+\s+(\w+)\s*\{",
                "property",
            ),
        ]

        for i, line in enumerate(lines):
            for pattern, kind in patterns:
                match = re.search(pattern, line)
                if match:
                    symbol_name = match.group(1)
                    symbols.append(
                        {
                            "symbol": symbol_name,
                            "kind": kind,
                            "line": i + 1,
                            "signature": line.strip(),
                            "doc": f"{kind.title()}: {symbol_name}",
                        }
                    )
                    break

        return symbols

    def getDefinition(self, symbol: str) -> SymbolDef | None:
        """Get the definition of a C# symbol."""
        # Search through cached analysis first
        for file_path, analysis in self._file_analysis_cache.items():
            type_analysis = analysis.get("types", {})

            # Check types
            for type_info in type_analysis.get("types", []):
                if type_info["name"] == symbol:
                    return SymbolDef(
                        symbol=symbol,
                        kind=type_info["kind"],
                        language=self.lang,
                        signature=type_info["signature"],
                        doc=self._generate_type_documentation(type_info),
                        defined_in=file_path,
                        line=type_info["line"],
                        span=(
                            type_info["line"],
                            type_info.get("end_line", type_info["line"]),
                        ),
                    )

            # Check methods
            for method_info in type_analysis.get("methods", []):
                if method_info["name"] == symbol:
                    return SymbolDef(
                        symbol=symbol,
                        kind="method",
                        language=self.lang,
                        signature=method_info["signature"],
                        doc=self._generate_method_documentation(method_info),
                        defined_in=file_path,
                        line=method_info["line"],
                        span=(method_info["line"], method_info["line"]),
                    )

            # Check properties
            for prop_info in type_analysis.get("properties", []):
                if prop_info["name"] == symbol:
                    return SymbolDef(
                        symbol=symbol,
                        kind="property",
                        language=self.lang,
                        signature=prop_info["signature"],
                        doc=self._generate_property_documentation(prop_info),
                        defined_in=file_path,
                        line=prop_info["line"],
                        span=(prop_info["line"], prop_info["line"]),
                    )

        # Fallback to file-based search
        for path in Path(".").rglob("*.cs"):
            try:
                content = path.read_text(encoding="utf-8")
                if symbol in content:
                    # Simple search for symbol definition
                    lines = content.split("\n")
                    for i, line in enumerate(lines):
                        if f" {symbol}" in line and any(
                            keyword in line
                            for keyword in [
                                "class ",
                                "interface ",
                                "struct ",
                                "enum ",
                                "delegate ",
                            ]
                        ):
                            return SymbolDef(
                                symbol=symbol,
                                kind="type",
                                language=self.lang,
                                signature=line.strip(),
                                doc=f"C# type: {symbol}",
                                defined_in=str(path),
                                line=i + 1,
                                span=(i + 1, i + 1),
                            )
            except Exception:
                continue

        return None

    def findReferences(self, symbol: str) -> list[Reference]:
        """Find all references to a C# symbol."""
        refs: list[Reference] = []
        seen: set[tuple[str, int]] = set()

        for path in Path(".").rglob("*.cs"):
            try:
                content = path.read_text(encoding="utf-8")
                lines = content.split("\n")

                for i, line in enumerate(lines):
                    # Look for symbol usage (simple text search with some context)
                    if symbol in line:
                        # Try to avoid false positives by checking word boundaries
                        if re.search(rf"\b{re.escape(symbol)}\b", line):
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

        # Enhanced search to handle C# specific patterns
        if self._is_csharp_pattern(query):
            # Handle specific C# patterns like generics, namespaces, etc.
            yield from self._search_csharp_patterns(query, limit)
        else:
            # Regular fuzzy search
            yield from self._indexer.search(query, limit=limit)

    def _is_csharp_pattern(self, query: str) -> bool:
        """Check if query contains C# specific patterns."""
        csharp_patterns = [
            r"<.*>",  # Generics
            r"async\s+",  # Async methods
            r"await\s+",  # Await expressions
            r"\w+\.\w+",  # Namespace/type references
            r"\.linq\b",  # LINQ references
            r"List<",  # Generic collections
            r"Task<",  # Task types
        ]

        for pattern in csharp_patterns:
            if re.search(pattern, query, re.IGNORECASE):
                return True

        return False

    def _search_csharp_patterns(self, query: str, limit: int) -> Iterable[SearchResult]:
        """Search for C# specific patterns."""
        results = []

        # Search in cached analysis
        for file_path, analysis in self._file_analysis_cache.items():
            type_analysis = analysis.get("types", {})

            # Search generic types
            for generic_usage in type_analysis.get("generic_types", []):
                usage_text = generic_usage.get("usage", "")
                if query.lower() in usage_text.lower():
                    results.append(
                        {
                            "file": file_path,
                            "line": generic_usage["line"],
                            "snippet": usage_text,
                        }
                    )

            # Search async patterns
            for async_pattern in type_analysis.get("async_patterns", []):
                pattern_text = async_pattern.get("pattern", "")
                if query.lower() in pattern_text.lower():
                    results.append(
                        {
                            "file": file_path,
                            "line": async_pattern["line"],
                            "snippet": pattern_text,
                        }
                    )

            # Search LINQ queries
            for linq_query in type_analysis.get("linq_queries", []):
                pattern_text = linq_query.get("pattern", "")
                if query.lower() in pattern_text.lower():
                    results.append(
                        {
                            "file": file_path,
                            "line": linq_query["line"],
                            "snippet": pattern_text,
                        }
                    )

        # Sort by relevance and limit results
        if results:
            results = sorted(results, key=lambda x: len(x["snippet"]))[:limit]

        for result in results:
            yield result

    def get_indexed_count(self) -> int:
        """Return the number of indexed files."""
        if hasattr(self._indexer, "_file_contents"):
            return len(self._indexer._file_contents)
        return 0

    def get_namespace_info(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Get namespace information for a file."""
        analysis = self._file_analysis_cache.get(file_path)
        return analysis.get("namespace") if analysis else None

    def get_type_info(self, type_name: str, file_path: str) -> Optional[Dict[str, Any]]:
        """Get type information for a specific type."""
        return self._type_analyzer.get_type_info(type_name, file_path)

    def get_available_namespaces(self, file_path: str) -> List[str]:
        """Get available namespaces for a file."""
        namespaces = []

        # Get from namespace analysis
        namespace_info = self.get_namespace_info(file_path)
        if namespace_info:
            namespaces.extend(namespace_info.get("using_statements", []))
            namespaces.extend(namespace_info.get("global_using", []))

        # Get from project packages
        project_files = list(Path(file_path).parent.rglob("*.csproj"))
        for project_file in project_files:
            project_analysis = self._project_cache.get(str(project_file))
            if project_analysis:
                namespaces.extend(project_analysis.get("package_namespaces", []))

        return list(set(namespaces))  # Remove duplicates

    def get_project_info(self, project_path: str) -> Optional[Dict[str, Any]]:
        """Get project information."""
        return self._project_cache.get(project_path)

    def analyze_async_patterns(self, file_path: str) -> List[Dict[str, Any]]:
        """Get async/await patterns from a file."""
        analysis = self._file_analysis_cache.get(file_path)
        if analysis and "types" in analysis:
            return analysis["types"].get("async_patterns", [])
        return []

    def analyze_linq_patterns(self, file_path: str) -> List[Dict[str, Any]]:
        """Get LINQ query patterns from a file."""
        analysis = self._file_analysis_cache.get(file_path)
        if analysis and "types" in analysis:
            return analysis["types"].get("linq_queries", [])
        return []
