"""Kotlin language plugin for advanced code analysis and indexing."""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import tree_sitter

from ...storage.sqlite_store import SQLiteStore
from ..specialized_plugin_base import (
    IBuildSystemIntegration,
    ICrossFileAnalyzer,
    IImportResolver,
    ITypeAnalyzer,
    SpecializedPluginBase,
)
from .coroutines_analyzer import CoroutinesAnalyzer
from .java_interop import JavaInteropAnalyzer
from .null_safety import NullSafetyAnalyzer

logger = logging.getLogger(__name__)


class KotlinPlugin(SpecializedPluginBase):
    """
    Specialized Kotlin plugin with support for:
    - Null safety analysis
    - Coroutines and suspend functions
    - Extension functions tracking
    - Java interoperability analysis
    - Gradle build integration
    """

    def __init__(self, sqlite_store: Optional[SQLiteStore] = None, enable_semantic: bool = False):
        # Create Kotlin language config
        language_config = {
            "code": "kotlin",
            "name": "Kotlin",
            "extensions": [".kt", ".kts"],
            "symbols": [
                "class_declaration",
                "function_declaration",
                "object_declaration",
                "suspend_function",
                "data_class",
                "sealed_class",
                "extension_function",
            ],
        }

        super().__init__(language_config, sqlite_store, enable_semantic)
        self.language_name = "kotlin"
        self.file_extensions = [".kt", ".kts"]

        # Initialize specialized analyzers
        self.null_safety_analyzer = NullSafetyAnalyzer()
        self.coroutines_analyzer = CoroutinesAnalyzer()
        self.java_interop_analyzer = JavaInteropAnalyzer()

        # Kotlin-specific queries
        self.kotlin_queries = {
            "class_declarations": """
                (class_declaration
                    name: (type_identifier) @class.name
                ) @class.definition
            """,
            "function_declarations": """
                (function_declaration
                    name: (simple_identifier) @function.name
                    parameters: (function_value_parameters)? @function.parameters
                ) @function.definition
            """,
            "suspend_functions": """
                (function_declaration
                    (modifiers
                        (modifier) @suspend
                        (#eq? @suspend "suspend")
                    )
                    name: (simple_identifier) @function.name
                ) @suspend.function
            """,
            "extension_functions": """
                (function_declaration
                    receiver: (function_value_parameters
                        (function_value_parameter
                            type: (_) @receiver.type
                        )
                    )
                    name: (simple_identifier) @extension.name
                ) @extension.function
            """,
            "data_classes": """
                (class_declaration
                    (modifiers
                        (modifier) @data
                        (#eq? @data "data")
                    )
                    name: (type_identifier) @data.class.name
                ) @data.class
            """,
            "sealed_classes": """
                (class_declaration
                    (modifiers
                        (modifier) @sealed
                        (#eq? @sealed "sealed")
                    )
                    name: (type_identifier) @sealed.class.name
                ) @sealed.class
            """,
            "object_declarations": """
                (object_declaration
                    name: (type_identifier) @object.name
                ) @object.definition
            """,
            "companion_objects": """
                (companion_object
                    name: (type_identifier)? @companion.name
                ) @companion.object
            """,
            "properties": """
                (property_declaration
                    (variable_declaration
                        (simple_identifier) @property.name
                    )
                    type: (_)? @property.type
                ) @property.definition
            """,
            "nullable_types": """
                (nullable_type
                    type: (_) @nullable.type
                ) @nullable
            """,
            "safe_calls": """
                (navigation_expression
                    "?." @safe.call
                ) @safe.navigation
            """,
            "elvis_operators": """
                (elvis_expression
                    "?:" @elvis.operator
                ) @elvis
            """,
            "coroutine_calls": """
                (call_expression
                    (simple_identifier) @coroutine.function
                    (#match? @coroutine.function "(launch|async|runBlocking|withContext)")
                ) @coroutine.call
            """,
            "flow_operations": """
                (call_expression
                    (navigation_expression
                        (simple_identifier) @flow.operation
                        (#match? @flow.operation "(collect|map|filter|transform|flowOf)")
                    )
                ) @flow.call
            """,
            "annotations": """
                (annotation
                    (user_type
                        (type_identifier) @annotation.name
                    )
                ) @annotation
            """,
            "imports": """
                (import_header
                    (identifier) @import.path
                ) @import
            """,
            "lambda_expressions": """
                (lambda_literal
                    parameters: (lambda_parameters)? @lambda.params
                ) @lambda
            """,
            "when_expressions": """
                (when_expression
                    subject: (_)? @when.subject
                ) @when
            """,
            "try_catch": """
                (try_expression
                    body: (_) @try.body
                    (catch_block
                        parameter: (simple_identifier) @catch.param
                    )? @catch
                ) @try.catch
            """,
            "generic_declarations": """
                (type_parameters
                    (type_parameter
                        name: (type_identifier) @generic.param
                    )
                ) @generics
            """,
            "interface_declarations": """
                (class_declaration
                    (modifiers
                        (modifier) @interface
                        (#eq? @interface "interface")
                    )
                    name: (type_identifier) @interface.name
                ) @interface.definition
            """,
            "enum_classes": """
                (class_declaration
                    (modifiers
                        (modifier) @enum
                        (#eq? @enum "enum")
                    )
                    name: (type_identifier) @enum.name
                ) @enum.class
            """,
            "init_blocks": """
                (init_block) @init
            """,
            "constructor_declarations": """
                (primary_constructor
                    parameters: (class_parameters) @constructor.params
                ) @primary.constructor
                
                (secondary_constructor
                    parameters: (function_value_parameters) @constructor.params
                ) @secondary.constructor
            """,
            "delegation": """
                (delegation_specifier
                    "by" @delegation.keyword
                    delegate: (_) @delegate
                ) @delegation
            """,
            "infix_functions": """
                (function_declaration
                    (modifiers
                        (modifier) @infix
                        (#eq? @infix "infix")
                    )
                    name: (simple_identifier) @infix.function.name
                ) @infix.function
            """,
            "operator_functions": """
                (function_declaration
                    (modifiers
                        (modifier) @operator
                        (#eq? @operator "operator")
                    )
                    name: (simple_identifier) @operator.function.name
                ) @operator.function
            """,
            "lateinit_properties": """
                (property_declaration
                    (modifiers
                        (modifier) @lateinit
                        (#eq? @lateinit "lateinit")
                    )
                    (variable_declaration
                        (simple_identifier) @lateinit.property.name
                    )
                ) @lateinit.property
            """,
            "lazy_properties": """
                (property_declaration
                    (property_delegate
                        "by" @lazy.keyword
                        expression: (call_expression
                            (simple_identifier) @lazy.function
                            (#eq? @lazy.function "lazy")
                        )
                    )
                ) @lazy.property
            """,
            "destructuring": """
                (destructuring_declaration
                    (variable_declaration
                        (simple_identifier) @destructure.var
                    )
                ) @destructuring
            """,
            "type_aliases": """
                (type_alias
                    name: (type_identifier) @alias.name
                    type: (_) @alias.type
                ) @type.alias
            """,
            "const_properties": """
                (property_declaration
                    (modifiers
                        (modifier) @const
                        (#eq? @const "const")
                    )
                    (variable_declaration
                        (simple_identifier) @const.property.name
                    )
                ) @const.property
            """,
            "inline_functions": """
                (function_declaration
                    (modifiers
                        (modifier) @inline
                        (#eq? @inline "inline")
                    )
                    name: (simple_identifier) @inline.function.name
                ) @inline.function
            """,
            "crossinline_parameters": """
                (function_value_parameter
                    (modifiers
                        (modifier) @crossinline
                        (#eq? @crossinline "crossinline")
                    )
                    name: (simple_identifier) @crossinline.param
                ) @crossinline.parameter
            """,
            "noinline_parameters": """
                (function_value_parameter
                    (modifiers
                        (modifier) @noinline
                        (#eq? @noinline "noinline")
                    )
                    name: (simple_identifier) @noinline.param
                ) @noinline.parameter
            """,
            "reified_generics": """
                (type_parameter
                    (modifiers
                        (modifier) @reified
                        (#eq? @reified "reified")
                    )
                    name: (type_identifier) @reified.generic
                ) @reified.parameter
            """,
            "vararg_parameters": """
                (function_value_parameter
                    (modifiers
                        (modifier) @vararg
                        (#eq? @vararg "vararg")
                    )
                    name: (simple_identifier) @vararg.param
                ) @vararg.parameter
            """,
            "scope_functions": """
                (call_expression
                    (navigation_expression
                        (simple_identifier) @scope.function
                        (#match? @scope.function "(let|run|with|apply|also)")
                    )
                ) @scope.call
            """,
            "smart_casts": """
                (is_expression
                    expression: (_) @cast.expression
                    type: (_) @cast.type
                ) @smart.cast
            """,
            "return_at_labels": """
                (return_at
                    label: (simple_identifier) @return.label
                ) @labeled.return
            """,
            "break_continue_labels": """
                (break_at
                    label: (simple_identifier) @break.label
                ) @labeled.break
                
                (continue_at
                    label: (simple_identifier) @continue.label
                ) @labeled.continue
            """,
            "backing_fields": """
                (getter
                    body: (function_body
                        "field" @backing.field
                    )
                ) @getter.with.field
                
                (setter
                    body: (function_body
                        "field" @backing.field
                    )
                ) @setter.with.field
            """,
        }

    def extract_symbols(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Extract symbols from Kotlin code with advanced analysis."""
        try:
            if not self.parser:
                logger.warning("Tree-sitter Kotlin parser not available")
                return []

            tree = self.parser.parse(bytes(content, "utf8"))
            symbols = []

            # Basic symbol extraction
            basic_symbols = self._extract_basic_symbols(tree, content, file_path)
            symbols.extend(basic_symbols)

            # Advanced analysis
            null_safety_info = self.null_safety_analyzer.analyze(tree, content)
            coroutines_info = self.coroutines_analyzer.analyze(tree, content)
            java_interop_info = self.java_interop_analyzer.analyze(tree, content)

            # Gradle integration analysis
            gradle_info = self._analyze_gradle_integration(content, file_path)

            # Add specialized symbols
            symbols.extend(
                self._create_specialized_symbols(
                    null_safety_info,
                    coroutines_info,
                    java_interop_info,
                    gradle_info,
                    file_path,
                )
            )

            return symbols

        except Exception as e:
            logger.error(f"Error extracting Kotlin symbols from {file_path}: {e}")
            return []

    def _extract_basic_symbols(
        self, tree: tree_sitter.Tree, content: str, file_path: str
    ) -> List[Dict[str, Any]]:
        """Extract basic Kotlin symbols using tree-sitter queries."""
        symbols = []
        lines = content.split("\n")

        for query_name, query_str in self.kotlin_queries.items():
            try:
                query = self.language.query(query_str)
                captures = query.captures(tree.root_node)

                for node, capture_name in captures:
                    symbol = self._create_symbol_from_node(
                        node, capture_name, lines, file_path, query_name
                    )
                    if symbol:
                        symbols.append(symbol)

            except Exception as e:
                logger.debug(f"Query {query_name} failed: {e}")

        return symbols

    def _create_symbol_from_node(
        self,
        node: tree_sitter.Node,
        capture_name: str,
        lines: List[str],
        file_path: str,
        query_type: str,
    ) -> Optional[Dict[str, Any]]:
        """Create a symbol dictionary from a tree-sitter node."""
        try:
            start_line = node.start_point[0]
            end_line = node.end_point[0]

            # Get symbol text
            symbol_text = lines[start_line] if start_line < len(lines) else ""

            # Get context (surrounding lines)
            context_start = max(0, start_line - 2)
            context_end = min(len(lines), end_line + 3)
            context = "\n".join(lines[context_start:context_end])

            symbol = {
                "name": node.text.decode("utf-8") if node.text else "",
                "type": self._get_symbol_type(capture_name, query_type),
                "line_number": start_line + 1,
                "column": node.start_point[1],
                "end_line": end_line + 1,
                "end_column": node.end_point[1],
                "file_path": file_path,
                "context": context,
                "signature": symbol_text.strip(),
                "language": "kotlin",
                "capture_name": capture_name,
                "query_type": query_type,
                "node_type": node.type,
                "metadata": self._extract_node_metadata(node, lines),
            }

            return symbol

        except Exception as e:
            logger.debug(f"Error creating symbol from node: {e}")
            return None

    def _get_symbol_type(self, capture_name: str, query_type: str) -> str:
        """Determine the symbol type based on capture name and query type."""
        type_mapping = {
            "class.name": "class",
            "function.name": "function",
            "suspend.function": "suspend_function",
            "extension.name": "extension_function",
            "data.class.name": "data_class",
            "sealed.class.name": "sealed_class",
            "object.name": "object",
            "companion.name": "companion_object",
            "property.name": "property",
            "interface.name": "interface",
            "enum.name": "enum",
            "annotation.name": "annotation",
            "import.path": "import",
            "lambda": "lambda",
            "when": "when_expression",
            "try.catch": "try_catch",
            "generic.param": "generic_parameter",
            "init": "init_block",
            "constructor.params": "constructor",
            "delegation": "delegation",
            "infix.function.name": "infix_function",
            "operator.function.name": "operator_function",
            "lateinit.property.name": "lateinit_property",
            "lazy.property": "lazy_property",
            "destructuring": "destructuring_declaration",
            "alias.name": "type_alias",
            "const.property.name": "const_property",
            "inline.function.name": "inline_function",
            "crossinline.param": "crossinline_parameter",
            "noinline.param": "noinline_parameter",
            "reified.generic": "reified_generic",
            "vararg.param": "vararg_parameter",
            "scope.function": "scope_function",
            "smart.cast": "smart_cast",
            "return.label": "labeled_return",
            "break.label": "labeled_break",
            "continue.label": "labeled_continue",
            "backing.field": "backing_field",
        }

        return type_mapping.get(capture_name, query_type)

    def _extract_node_metadata(self, node: tree_sitter.Node, lines: List[str]) -> Dict[str, Any]:
        """Extract additional metadata from the node."""
        metadata = {
            "node_type": node.type,
            "children_count": node.child_count,
            "is_named": node.is_named,
        }

        # Extract modifiers if present
        modifiers = self._extract_modifiers(node)
        if modifiers:
            metadata["modifiers"] = modifiers

        # Extract type information
        type_info = self._extract_type_info(node)
        if type_info:
            metadata["type_info"] = type_info

        # Extract parameters for functions
        if node.type in [
            "function_declaration",
            "primary_constructor",
            "secondary_constructor",
        ]:
            params = self._extract_parameters(node)
            if params:
                metadata["parameters"] = params

        return metadata

    def _extract_modifiers(self, node: tree_sitter.Node) -> List[str]:
        """Extract modifiers from a node."""
        modifiers = []
        for child in node.children:
            if child.type == "modifiers":
                for modifier_child in child.children:
                    if modifier_child.type == "modifier":
                        modifiers.append(modifier_child.text.decode("utf-8"))
        return modifiers

    def _extract_type_info(self, node: tree_sitter.Node) -> Optional[str]:
        """Extract type information from a node."""
        for child in node.children:
            if "type" in child.type:
                return child.text.decode("utf-8")
        return None

    def _extract_parameters(self, node: tree_sitter.Node) -> List[Dict[str, Any]]:
        """Extract parameter information from function nodes."""
        parameters = []

        for child in node.children:
            if child.type in ["function_value_parameters", "class_parameters"]:
                for param_child in child.children:
                    if param_child.type in [
                        "function_value_parameter",
                        "class_parameter",
                    ]:
                        param_info = {"name": "", "type": "", "modifiers": []}

                        for param_part in param_child.children:
                            if param_part.type == "simple_identifier":
                                param_info["name"] = param_part.text.decode("utf-8")
                            elif "type" in param_part.type:
                                param_info["type"] = param_part.text.decode("utf-8")
                            elif param_part.type == "modifiers":
                                param_info["modifiers"] = self._extract_modifiers(param_part)

                        if param_info["name"]:
                            parameters.append(param_info)

        return parameters

    def _create_specialized_symbols(
        self,
        null_safety_info: Dict,
        coroutines_info: Dict,
        java_interop_info: Dict,
        gradle_info: Dict,
        file_path: str,
    ) -> List[Dict[str, Any]]:
        """Create specialized symbols from analysis results."""
        symbols = []

        # Null safety symbols
        for item in null_safety_info.get("nullable_usages", []):
            symbols.append(
                {
                    "name": f"null_safety_{item.get('name', 'unknown')}",
                    "type": "null_safety_usage",
                    "line_number": item.get("line", 1),
                    "file_path": file_path,
                    "context": item.get("context", ""),
                    "language": "kotlin",
                    "metadata": {
                        "safety_type": item.get("type"),
                        "analysis": "null_safety",
                    },
                }
            )

        # Coroutines symbols
        for item in coroutines_info.get("suspend_functions", []):
            symbols.append(
                {
                    "name": f"coroutine_{item.get('name', 'unknown')}",
                    "type": "coroutine_usage",
                    "line_number": item.get("line", 1),
                    "file_path": file_path,
                    "context": item.get("context", ""),
                    "language": "kotlin",
                    "metadata": {
                        "coroutine_type": item.get("type"),
                        "analysis": "coroutines",
                    },
                }
            )

        # Java interop symbols
        for item in java_interop_info.get("interop_usages", []):
            symbols.append(
                {
                    "name": f"java_interop_{item.get('name', 'unknown')}",
                    "type": "java_interop",
                    "line_number": item.get("line", 1),
                    "file_path": file_path,
                    "context": item.get("context", ""),
                    "language": "kotlin",
                    "metadata": {
                        "interop_type": item.get("type"),
                        "analysis": "java_interop",
                    },
                }
            )

        return symbols

    def _analyze_gradle_integration(self, content: str, file_path: str) -> Dict[str, Any]:
        """Analyze Gradle build integration patterns."""
        gradle_info = {
            "build_script_patterns": [],
            "dependency_patterns": [],
            "plugin_usages": [],
        }

        # Check if this is a build script
        if file_path.endswith((".gradle.kts", "build.gradle.kts")):
            gradle_info["is_build_script"] = True

            # Look for common Gradle patterns
            gradle_patterns = [
                r"plugins\s*\{",
                r"dependencies\s*\{",
                r"android\s*\{",
                r"kotlin\s*\{",
                r"implementation\s*\(",
                r"api\s*\(",
                r"testImplementation\s*\(",
                r"kapt\s*\(",
                r"buildscript\s*\{",
            ]

            for pattern in gradle_patterns:
                matches = re.finditer(pattern, content, re.MULTILINE)
                for match in matches:
                    line_num = content[: match.start()].count("\n") + 1
                    gradle_info["build_script_patterns"].append(
                        {"pattern": pattern, "line": line_num, "match": match.group()}
                    )

        return gradle_info

    def get_supported_queries(self) -> List[str]:
        """Return list of supported query types."""
        return list(self.kotlin_queries.keys()) + [
            "null_safety_analysis",
            "coroutines_analysis",
            "java_interop_analysis",
            "gradle_integration",
        ]

    def search_symbols(self, query: str, file_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """Search for symbols with Kotlin-specific patterns."""
        results = super().search_symbols(query, file_path)

        # Add Kotlin-specific search enhancements
        kotlin_patterns = [
            r"suspend\s+fun\s+" + re.escape(query),  # Suspend functions
            r"fun\s+\w+\." + re.escape(query),  # Extension functions
            r"data\s+class\s+" + re.escape(query),  # Data classes
            r"sealed\s+class\s+" + re.escape(query),  # Sealed classes
            r"object\s+" + re.escape(query),  # Objects
            r"companion\s+object",  # Companion objects
            r"by\s+lazy",  # Lazy properties
            r"lateinit\s+var",  # Late-init properties
            r"@\w*" + re.escape(query),  # Annotations
        ]

        # Search with Kotlin patterns
        for pattern in kotlin_patterns:
            pattern_results = self._search_with_pattern(pattern, file_path)
            results.extend(pattern_results)

        return results

    def _search_with_pattern(
        self, pattern: str, file_path: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Search using regex pattern."""
        # This would integrate with the actual search infrastructure
        # For now, return empty list as this requires integration with the storage layer
        return []

    def get_file_extensions(self) -> List[str]:
        """Return supported file extensions."""
        return self.file_extensions

    def get_language_name(self) -> str:
        """Return the language name."""
        return self.language_name

    # Abstract method implementations required by SpecializedPluginBase

    def _create_import_resolver(self) -> IImportResolver:
        """Create Kotlin-specific import resolver."""
        return KotlinImportResolver()

    def _create_type_analyzer(self) -> ITypeAnalyzer:
        """Create Kotlin-specific type analyzer."""
        return KotlinTypeAnalyzer()

    def _create_build_system(self) -> IBuildSystemIntegration:
        """Create Kotlin-specific build system integration (Gradle)."""
        return KotlinGradleBuildSystem()

    def _create_cross_file_analyzer(self) -> ICrossFileAnalyzer:
        """Create Kotlin-specific cross-file analyzer."""
        return KotlinCrossFileAnalyzer()


# Placeholder implementations for the required interfaces
# These would be fully implemented in a production system


class KotlinImportResolver(IImportResolver):
    """Kotlin import resolver."""

    def resolve_import(self, import_info, current_file: Path) -> Optional[Path]:
        """Resolve Kotlin import to file path."""
        # Placeholder implementation
        return None

    def get_import_graph(self) -> Dict[str, Set[str]]:
        """Get import dependency graph."""
        return {}

    def find_circular_dependencies(self) -> List[List[str]]:
        """Find circular dependencies."""
        return []


class KotlinTypeAnalyzer(ITypeAnalyzer):
    """Kotlin type analyzer."""

    def get_type_info(self, symbol: str, file_path: str):
        """Get type information for symbol."""
        return None

    def find_implementations(self, interface_name: str) -> List[Tuple[str, str]]:
        """Find interface implementations."""
        return []

    def resolve_generic_type(self, type_expr: str, context: Dict[str, str]) -> str:
        """Resolve generic type."""
        return type_expr


class KotlinGradleBuildSystem(IBuildSystemIntegration):
    """Kotlin Gradle build system integration."""

    def parse_build_file(self, build_file_path: Path):
        """Parse Gradle build file."""
        return []

    def resolve_external_import(self, import_path: str) -> Optional[str]:
        """Resolve external import."""
        return None

    def get_project_structure(self) -> Dict[str, Any]:
        """Get project structure."""
        return {}


class KotlinCrossFileAnalyzer(ICrossFileAnalyzer):
    """Kotlin cross-file analyzer."""

    def find_all_references(self, symbol: str, definition_file: str):
        """Find all references to symbol."""
        return []

    def get_call_graph(self, function_name: str) -> Dict[str, Set[str]]:
        """Get call graph."""
        return {}

    def analyze_impact(self, file_path: str) -> Dict[str, List[str]]:
        """Analyze change impact."""
        return {}
