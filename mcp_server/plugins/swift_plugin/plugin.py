"""Swift language plugin implementation with advanced features."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ...plugin_base import IndexShard, SearchOpts, SearchResult
from ...storage.sqlite_store import SQLiteStore
from ..specialized_plugin_base import (
    BuildDependency,
    CrossFileReference,
    IBuildSystemIntegration,
    ICrossFileAnalyzer,
    IImportResolver,
    ImportInfo,
    ITypeAnalyzer,
    SpecializedPluginBase,
    TypeInfo,
)
from .module_analyzer import SwiftModuleAnalyzer
from .objc_bridge import ObjectiveCBridge
from .protocol_checker import SwiftProtocolChecker

logger = logging.getLogger(__name__)

# Swift-specific language configuration
SWIFT_CONFIG = {
    "code": "swift",
    "name": "Swift",
    "extensions": [".swift"],
    "symbols": [
        "class_declaration",
        "struct_declaration",
        "enum_declaration",
        "protocol_declaration",
        "extension_declaration",
        "function_declaration",
        "init_declaration",
        "deinit_declaration",
        "property_declaration",
        "subscript_declaration",
        "operator_declaration",
        "precedence_group_declaration",
        "associated_type_declaration",
        "import_declaration",
    ],
    "query": """
        ; Classes
        (class_declaration name: (type_identifier) @class) @class_def
        
        ; Structures
        (struct_declaration name: (type_identifier) @struct) @struct_def
        
        ; Enumerations
        (enum_declaration name: (type_identifier) @enum) @enum_def
        
        ; Protocols
        (protocol_declaration name: (type_identifier) @protocol) @protocol_def
        
        ; Extensions
        (extension_declaration type: (type_identifier) @extension) @extension_def
        
        ; Functions
        (function_declaration name: (simple_identifier) @function) @function_def
        
        ; Initializers
        (init_declaration) @init @init_def
        
        ; Deinitializers
        (deinit_declaration) @deinit @deinit_def
        
        ; Properties
        (property_declaration name: (pattern (simple_identifier) @property)) @property_def
        
        ; Computed properties
        (computed_property name: (simple_identifier) @computed_property) @computed_property_def
        
        ; Subscripts
        (subscript_declaration) @subscript @subscript_def
        
        ; Operators
        (operator_declaration operator: (custom_operator) @operator) @operator_def
        
        ; Precedence groups
        (precedence_group_declaration name: (simple_identifier) @precedence_group) @precedence_group_def
        
        ; Associated types
        (associated_type_declaration name: (type_identifier) @associated_type) @associated_type_def
        
        ; Type aliases
        (typealias_declaration name: (type_identifier) @typealias) @typealias_def
        
        ; Imports
        (import_declaration (identifier) @import) @import_def
        
        ; Property wrappers
        (attribute_list (attribute name: (user_type (type_identifier) @property_wrapper))) @property_wrapper_def
        
        ; Result builders
        (attribute_list (attribute name: (user_type (type_identifier) @result_builder))) @result_builder_def
        
        ; Protocol conformance
        (type_inheritance_clause (inheritance_constraint type: (type_identifier) @protocol_conformance))
        
        ; Generic constraints
        (generic_where_clause (conformance_constraint type: (type_identifier) @generic_constraint))
        
        ; Method calls
        (call_expression function: (navigation_expression target: (_) property: (navigation_suffix (simple_identifier) @method_call)))
        
        ; Property access
        (navigation_expression target: (_) property: (navigation_suffix (simple_identifier) @property_access))
        
        ; Type references
        (user_type (type_identifier) @type_reference)
        
        ; Variable declarations
        (property_declaration name: (pattern (simple_identifier) @variable)) @variable_def
        
        ; Parameter declarations
        (parameter name: (simple_identifier) @parameter) @parameter_def
        
        ; Closures
        (lambda_literal) @closure @closure_def
        
        ; Guard statements
        (guard_statement) @guard @guard_def
        
        ; Throw statements
        (throw_statement) @throw @throw_def
        
        ; Try expressions
        (try_expression) @try @try_def
        
        ; Async/await
        (await_expression) @await @await_def
        
        ; Actor declarations
        (actor_declaration name: (type_identifier) @actor) @actor_def
    """,
}


class SwiftImportResolver(IImportResolver):
    """Swift-specific import resolver."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.import_graph: Dict[str, Set[str]] = {}
        self.module_cache: Dict[str, Path] = {}

    def resolve_import(self, import_info: ImportInfo, current_file: Path) -> Optional[Path]:
        """Resolve Swift import to file path."""
        module_path = import_info.module_path

        # Check cache first
        if module_path in self.module_cache:
            return self.module_cache[module_path]

        # System frameworks (UIKit, Foundation, etc.)
        if self._is_system_framework(module_path):
            return None  # External dependency

        # Local module resolution
        resolved_path = self._resolve_local_module(module_path, current_file)
        if resolved_path:
            self.module_cache[module_path] = resolved_path

        return resolved_path

    def get_import_graph(self) -> Dict[str, Set[str]]:
        """Get the complete import dependency graph."""
        return self.import_graph.copy()

    def find_circular_dependencies(self) -> List[List[str]]:
        """Find circular dependencies in imports."""
        cycles = []
        visited = set()
        rec_stack = set()

        def dfs(node: str, path: List[str]) -> bool:
            if node in rec_stack:
                # Found cycle
                cycle_start = path.index(node)
                cycles.append(path[cycle_start:] + [node])
                return True

            if node in visited:
                return False

            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbor in self.import_graph.get(node, set()):
                if dfs(neighbor, path):
                    return True

            rec_stack.remove(node)
            path.pop()
            return False

        for node in self.import_graph:
            if node not in visited:
                dfs(node, [])

        return cycles

    def _is_system_framework(self, module_name: str) -> bool:
        """Check if module is a system framework."""
        system_frameworks = {
            "Foundation",
            "UIKit",
            "SwiftUI",
            "Combine",
            "CoreData",
            "CoreGraphics",
            "QuartzCore",
            "AVFoundation",
            "WebKit",
            "MapKit",
            "CoreLocation",
            "UserNotifications",
            "CloudKit",
        }
        return module_name in system_frameworks

    def _resolve_local_module(self, module_name: str, current_file: Path) -> Optional[Path]:
        """Resolve local module to file path."""
        # Look for Swift files with matching name
        search_dirs = [
            current_file.parent,  # Same directory
            self.project_root,  # Project root
            self.project_root / "Sources",  # SPM Sources directory
        ]

        for search_dir in search_dirs:
            if search_dir.exists():
                # Direct file match
                swift_file = search_dir / f"{module_name}.swift"
                if swift_file.exists():
                    return swift_file

                # Directory with same name
                module_dir = search_dir / module_name
                if module_dir.exists() and module_dir.is_dir():
                    # Look for main file
                    main_file = module_dir / f"{module_name}.swift"
                    if main_file.exists():
                        return main_file

                    # Look for any Swift file in the directory
                    swift_files = list(module_dir.glob("*.swift"))
                    if swift_files:
                        return swift_files[0]

        return None


class SwiftTypeAnalyzer(ITypeAnalyzer):
    """Swift-specific type analyzer."""

    def __init__(self):
        self.type_cache: Dict[str, TypeInfo] = {}
        self.protocol_conformances: Dict[str, Set[str]] = {}
        self.generic_constraints: Dict[str, Dict[str, str]] = {}

    def get_type_info(self, symbol: str, file_path: str) -> Optional[TypeInfo]:
        """Get type information for Swift symbol."""
        cache_key = f"{file_path}::{symbol}"

        if cache_key in self.type_cache:
            return self.type_cache[cache_key]

        # Analyze type from source
        type_info = self._analyze_type(symbol, file_path)
        if type_info:
            self.type_cache[cache_key] = type_info

        return type_info

    def find_implementations(self, protocol_name: str) -> List[Tuple[str, str]]:
        """Find all types that conform to a protocol."""
        implementations = []

        for type_name, protocols in self.protocol_conformances.items():
            if protocol_name in protocols:
                # Extract file path from type name if available
                if "::" in type_name:
                    file_path, actual_type = type_name.split("::", 1)
                    implementations.append((actual_type, file_path))
                else:
                    implementations.append((type_name, ""))

        return implementations

    def resolve_generic_type(self, type_expr: str, context: Dict[str, str]) -> str:
        """Resolve generic type expression."""
        # Handle basic generic substitution
        for generic_param, concrete_type in context.items():
            type_expr = type_expr.replace(f"<{generic_param}>", f"<{concrete_type}>")
            type_expr = type_expr.replace(generic_param, concrete_type)

        return type_expr

    def _analyze_type(self, symbol: str, file_path: str) -> Optional[TypeInfo]:
        """Analyze type information from source code."""
        try:
            content = Path(file_path).read_text()

            # Look for type declaration
            patterns = [
                rf"class\s+{re.escape(symbol)}\s*(<[^>]+>)?\s*:\s*([^{{]+)",
                rf"struct\s+{re.escape(symbol)}\s*(<[^>]+>)?\s*:\s*([^{{]+)",
                rf"enum\s+{re.escape(symbol)}\s*(<[^>]+>)?\s*:\s*([^{{]+)",
                rf"protocol\s+{re.escape(symbol)}\s*(<[^>]+>)?\s*:\s*([^{{]+)",
                rf"actor\s+{re.escape(symbol)}\s*(<[^>]+>)?\s*:\s*([^{{]+)",
            ]

            for pattern in patterns:
                match = re.search(pattern, content, re.MULTILINE)
                if match:
                    generic_params = []
                    if match.group(1):  # Generic parameters
                        generic_str = match.group(1)[1:-1]  # Remove < >
                        generic_params = [p.strip() for p in generic_str.split(",")]

                    super_types = []
                    if match.group(2):  # Inheritance/conformance
                        super_types = [t.strip() for t in match.group(2).split(",")]

                    # Determine if it's a protocol
                    is_interface = "protocol" in pattern

                    return TypeInfo(
                        type_name=symbol,
                        is_generic=bool(generic_params),
                        generic_params=generic_params,
                        is_nullable=False,  # Swift has optionals, not nullables
                        is_interface=is_interface,
                        super_types=super_types,
                    )

        except Exception as e:
            logger.warning(f"Failed to analyze type {symbol} in {file_path}: {e}")

        return None


class SwiftBuildSystem(IBuildSystemIntegration):
    """Swift Package Manager integration."""

    def __init__(self, project_root: Path):
        self.project_root = project_root

    def parse_build_file(self, build_file_path: Path) -> List[BuildDependency]:
        """Parse Package.swift file."""
        dependencies = []

        try:
            content = build_file_path.read_text()

            # Parse Swift Package Manager dependencies
            # Look for .package patterns
            package_patterns = [
                r'\.package\(url:\s*"([^"]+)",\s*from:\s*"([^"]+)"\)',
                r'\.package\(url:\s*"([^"]+)",\s*\.upToNextMajor\(from:\s*"([^"]+)"\)\)',
                r'\.package\(url:\s*"([^"]+)",\s*\.upToNextMinor\(from:\s*"([^"]+)"\)\)',
                r'\.package\(url:\s*"([^"]+)",\s*"([^"]+)"\.\.<"([^"]+)"\)',
            ]

            for pattern in package_patterns:
                matches = re.finditer(pattern, content)
                for match in matches:
                    url = match.group(1)
                    version = match.group(2)

                    # Extract package name from URL
                    name = self._extract_package_name(url)

                    dependencies.append(
                        BuildDependency(
                            name=name, version=version, registry="swift_package_manager"
                        )
                    )

            # Parse target dependencies
            target_deps = self._parse_target_dependencies(content)
            dependencies.extend(target_deps)

        except Exception as e:
            logger.warning(f"Failed to parse Package.swift: {e}")

        return dependencies

    def resolve_external_import(self, import_path: str) -> Optional[str]:
        """Resolve external package import."""
        # This would typically involve checking the package cache
        # For now, return None for external packages
        return None

    def get_project_structure(self) -> Dict[str, Any]:
        """Get Swift project structure."""
        structure = {
            "type": "swift_package",
            "sources": [],
            "tests": [],
            "resources": [],
            "targets": [],
        }

        # Standard SPM structure
        sources_dir = self.project_root / "Sources"
        tests_dir = self.project_root / "Tests"

        if sources_dir.exists():
            structure["sources"] = [str(f) for f in sources_dir.rglob("*.swift")]

        if tests_dir.exists():
            structure["tests"] = [str(f) for f in tests_dir.rglob("*.swift")]

        # Parse Package.swift for targets
        package_file = self.project_root / "Package.swift"
        if package_file.exists():
            try:
                content = package_file.read_text()
                targets = self._parse_targets(content)
                structure["targets"] = targets
            except Exception as e:
                logger.warning(f"Failed to parse targets: {e}")

        return structure

    def _extract_package_name(self, url: str) -> str:
        """Extract package name from Git URL."""
        # Remove .git suffix and extract last component
        name = url.split("/")[-1]
        if name.endswith(".git"):
            name = name[:-4]
        return name

    def _parse_target_dependencies(self, content: str) -> List[BuildDependency]:
        """Parse target dependencies from Package.swift."""
        dependencies = []

        # Look for .target dependencies
        target_pattern = r"\.target\([^)]*dependencies:\s*\[([^\]]+)\]"
        matches = re.finditer(target_pattern, content, re.DOTALL)

        for match in matches:
            deps_str = match.group(1)
            # Parse individual dependencies
            dep_pattern = r'"([^"]+)"'
            dep_matches = re.finditer(dep_pattern, deps_str)

            for dep_match in dep_matches:
                dep_name = dep_match.group(1)
                dependencies.append(
                    BuildDependency(name=dep_name, version="latest", registry="local_target")
                )

        return dependencies

    def _parse_targets(self, content: str) -> List[Dict[str, Any]]:
        """Parse target definitions from Package.swift."""
        targets = []

        # Simple regex-based parsing (could be improved with proper AST parsing)
        target_pattern = r'\.target\(\s*name:\s*"([^"]+)"[^)]*\)'
        matches = re.finditer(target_pattern, content)

        for match in matches:
            target_name = match.group(1)
            targets.append({"name": target_name, "type": "library"})  # Default assumption

        return targets


class SwiftCrossFileAnalyzer(ICrossFileAnalyzer):
    """Swift-specific cross-file analysis."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.file_cache: Dict[str, str] = {}

    def find_all_references(self, symbol: str, definition_file: str) -> List[CrossFileReference]:
        """Find all references to a symbol across Swift files."""
        references = []

        # Search all Swift files in the project
        for swift_file in self.project_root.rglob("*.swift"):
            if swift_file == Path(definition_file):
                continue  # Skip definition file

            try:
                content = self._get_file_content(str(swift_file))
                refs = self._find_symbol_references(symbol, content, str(swift_file))
                references.extend(refs)
            except Exception as e:
                logger.warning(f"Failed to analyze {swift_file}: {e}")

        return references

    def get_call_graph(self, function_name: str) -> Dict[str, Set[str]]:
        """Get call graph for a Swift function."""
        call_graph = {}

        # This would require more sophisticated analysis
        # For now, return empty graph
        return call_graph

    def analyze_impact(self, file_path: str) -> Dict[str, List[str]]:
        """Analyze impact of changes to a Swift file."""
        impact = {
            "direct_dependencies": [],
            "indirect_dependencies": [],
            "test_files": [],
        }

        # Find files that import this file
        file_name = Path(file_path).stem

        for swift_file in self.project_root.rglob("*.swift"):
            if str(swift_file) == file_path:
                continue

            try:
                content = self._get_file_content(str(swift_file))
                if f"import {file_name}" in content:
                    impact["direct_dependencies"].append(str(swift_file))
                elif file_name in content:
                    impact["indirect_dependencies"].append(str(swift_file))
            except Exception as e:
                logger.warning(f"Failed to analyze impact for {swift_file}: {e}")

        return impact

    def _get_file_content(self, file_path: str) -> str:
        """Get file content with caching."""
        if file_path not in self.file_cache:
            self.file_cache[file_path] = Path(file_path).read_text()
        return self.file_cache[file_path]

    def _find_symbol_references(
        self, symbol: str, content: str, file_path: str
    ) -> List[CrossFileReference]:
        """Find references to a symbol in file content."""
        references = []
        lines = content.split("\n")

        for line_num, line in enumerate(lines, 1):
            # Simple pattern matching (could be improved)
            if symbol in line and not line.strip().startswith("//"):
                # Determine reference type
                ref_type = self._determine_reference_type(line, symbol)

                references.append(
                    CrossFileReference(
                        symbol=symbol,
                        source_file=file_path,
                        target_file="",  # Would need definition lookup
                        line_number=line_num,
                        reference_type=ref_type,
                    )
                )

        return references

    def _determine_reference_type(self, line: str, symbol: str) -> str:
        """Determine the type of reference in a line."""
        _ = line.strip()

        if f"{symbol}(" in line:
            return "call"
        elif f"import {symbol}" in line:
            return "import"
        elif f": {symbol}" in line or f"<{symbol}>" in line:
            return "type_usage"
        elif "class " in line and f": {symbol}" in line:
            return "inherit"
        elif "extension " in line and f": {symbol}" in line:
            return "conform"
        else:
            return "reference"


class Plugin(SpecializedPluginBase):
    """Swift language plugin with comprehensive features."""

    lang = "swift"

    def __init__(self, sqlite_store: Optional[SQLiteStore] = None, enable_semantic: bool = False):
        """Initialize Swift plugin."""
        super().__init__(SWIFT_CONFIG, sqlite_store, enable_semantic)

        # Swift-specific components
        self.module_analyzer = SwiftModuleAnalyzer()
        self.protocol_checker = SwiftProtocolChecker()
        self.objc_bridge = ObjectiveCBridge()

        # Find project root
        self._project_root = self._find_project_root()

        # Cache for analyzed files
        self._analyzed_files: Set[str] = set()

    def supports(self, path: str | Path) -> bool:
        """Check if file is supported."""
        path_obj = Path(path)
        return path_obj.suffix.lower() in [".swift"]

    def _create_import_resolver(self) -> IImportResolver:
        """Create Swift import resolver."""
        return SwiftImportResolver(self._project_root or Path.cwd())

    def _create_type_analyzer(self) -> ITypeAnalyzer:
        """Create Swift type analyzer."""
        return SwiftTypeAnalyzer()

    def _create_build_system(self) -> IBuildSystemIntegration:
        """Create Swift build system integration."""
        return SwiftBuildSystem(self._project_root or Path.cwd())

    def _create_cross_file_analyzer(self) -> ICrossFileAnalyzer:
        """Create Swift cross-file analyzer."""
        return SwiftCrossFileAnalyzer(self._project_root or Path.cwd())

    def indexFile(self, path: str | Path, content: str) -> IndexShard:
        """Index Swift file with enhanced analysis."""
        # Get base index
        base_index = super().indexFile(path, content)

        # Enhance with Swift-specific analysis
        file_path = Path(path)

        try:
            # Analyze modules and imports
            imports = self.module_analyzer.analyze_imports(content)

            # Check protocol conformances
            conformances = self.protocol_checker.find_conformances(content)

            # Analyze Objective-C interop
            objc_info = self.objc_bridge.analyze_interop(content)

            # Add Swift-specific symbols to index
            swift_symbols = self._extract_swift_symbols(content, str(file_path))
            base_index["symbols"].extend(swift_symbols)

            # Store additional metadata
            base_index["metadata"] = {
                "imports": [imp.__dict__ for imp in imports],
                "protocol_conformances": conformances,
                "objc_interop": objc_info,
                "has_property_wrappers": "@" in content and "propertyWrapper" in content,
                "has_result_builders": "@" in content and "resultBuilder" in content,
                "has_actors": "actor " in content,
                "has_async_await": "async " in content or "await " in content,
            }

        except Exception as e:
            logger.warning(f"Enhanced Swift analysis failed for {path}: {e}")

        return base_index

    def search(self, query: str, opts: SearchOpts | None = None) -> Iterable[SearchResult]:
        """Enhanced search with Swift-specific features."""
        # Base search
        for result in super().search(query, opts):
            yield result

        # Swift-specific searches
        if opts and opts.get("semantic", False):
            # Protocol conformance search
            if query.startswith("protocol:"):
                protocol_name = query[9:]
                yield from self._search_protocol_conformances(protocol_name)

            # Property wrapper search
            elif query.startswith("@"):
                yield from self._search_property_wrappers(query[1:])

            # Module search
            elif query.startswith("import:"):
                module_name = query[7:]
                yield from self._search_module_usage(module_name)

    def _extract_swift_symbols(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Extract Swift-specific symbols."""
        symbols = []

        # Property wrappers
        property_wrappers = re.finditer(r"@(\w+)\s+(?:struct|class)\s+(\w+)", content)
        for match in property_wrappers:
            if match.group(1) in ["propertyWrapper", "resultBuilder"]:
                symbols.append(
                    {
                        "symbol": match.group(2),
                        "kind": match.group(1),
                        "language": "swift",
                        "signature": match.group(0),
                        "doc": None,
                        "defined_in": file_path,
                        "line": content[: match.start()].count("\n") + 1,
                        "span": (match.start(), match.end()),
                    }
                )

        # Actors
        actors = re.finditer(r"actor\s+(\w+)", content)
        for match in actors:
            symbols.append(
                {
                    "symbol": match.group(1),
                    "kind": "actor",
                    "language": "swift",
                    "signature": match.group(0),
                    "doc": None,
                    "defined_in": file_path,
                    "line": content[: match.start()].count("\n") + 1,
                    "span": (match.start(), match.end()),
                }
            )

        return symbols

    def _search_protocol_conformances(self, protocol_name: str) -> Iterable[SearchResult]:
        """Search for protocol conformances."""
        implementations = self.type_analyzer.find_implementations(protocol_name)

        for type_name, file_path in implementations:
            if file_path and Path(file_path).exists():
                try:
                    content = Path(file_path).read_text()
                    lines = content.split("\n")

                    for line_num, line in enumerate(lines, 1):
                        if protocol_name in line and ":" in line:
                            yield SearchResult(
                                file=file_path,
                                start_line=line_num,
                                end_line=line_num,
                                snippet=line.strip(),
                            )
                except Exception as e:
                    logger.warning(f"Failed to search conformances in {file_path}: {e}")

    def _search_property_wrappers(self, wrapper_name: str) -> Iterable[SearchResult]:
        """Search for property wrapper usage."""
        pattern = f"@{wrapper_name}"

        for swift_file in self._project_root.rglob("*.swift"):
            try:
                content = swift_file.read_text()
                lines = content.split("\n")

                for line_num, line in enumerate(lines, 1):
                    if pattern in line:
                        yield SearchResult(
                            file=str(swift_file),
                            start_line=line_num,
                            end_line=line_num,
                            snippet=line.strip(),
                        )
            except Exception as e:
                logger.warning(f"Failed to search property wrappers in {swift_file}: {e}")

    def _search_module_usage(self, module_name: str) -> Iterable[SearchResult]:
        """Search for module import and usage."""
        pattern = f"import {module_name}"

        for swift_file in self._project_root.rglob("*.swift"):
            try:
                content = swift_file.read_text()
                lines = content.split("\n")

                for line_num, line in enumerate(lines, 1):
                    if pattern in line:
                        yield SearchResult(
                            file=str(swift_file),
                            start_line=line_num,
                            end_line=line_num,
                            snippet=line.strip(),
                        )
            except Exception as e:
                logger.warning(f"Failed to search module usage in {swift_file}: {e}")

    def _find_project_root(self) -> Optional[Path]:
        """Find Swift project root by looking for Package.swift."""
        current = Path.cwd()

        while current != current.parent:
            if (current / "Package.swift").exists():
                return current
            if (current / ".git").exists():
                return current
            current = current.parent

        return Path.cwd()

    def _discover_build_files(self):
        """Discover Swift build files."""
        if self._project_root:
            package_swift = self._project_root / "Package.swift"
            if package_swift.exists():
                self._build_files = [package_swift]

            # Also look for Xcode project files
            _ = list(self._project_root.glob("*.xcodeproj"))
            _ = list(self._project_root.glob("*.xcworkspace"))

            # Note: We don't parse Xcode files yet, but we could extend this

    def analyze_imports(self, file_path: Path) -> List[ImportInfo]:
        """Analyze imports in a Swift file."""
        try:
            content = file_path.read_text()
            return self.module_analyzer.analyze_imports(content)
        except Exception as e:
            logger.warning(f"Failed to analyze imports in {file_path}: {e}")
            return []

    def get_protocol_conformances(self, file_path: str) -> Dict[str, List[str]]:
        """Get protocol conformances for types in a file."""
        try:
            content = Path(file_path).read_text()
            return self.protocol_checker.find_conformances(content)
        except Exception as e:
            logger.warning(f"Failed to analyze protocol conformances in {file_path}: {e}")
            return {}

    def get_objc_bridging_info(self, file_path: str) -> Dict[str, Any]:
        """Get Objective-C bridging information."""
        try:
            content = Path(file_path).read_text()
            return self.objc_bridge.analyze_interop(content)
        except Exception as e:
            logger.warning(f"Failed to analyze Objective-C interop in {file_path}: {e}")
            return {}
