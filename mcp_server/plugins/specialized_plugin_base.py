"""Base classes and interfaces for specialized language plugins."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from mcp_server.plugins.generic_treesitter_plugin import GenericTreeSitterPlugin
from mcp_server.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


@dataclass
class ImportInfo:
    """Information about an import statement."""

    module_path: str
    imported_names: List[str] = field(default_factory=list)
    alias: Optional[str] = None
    is_relative: bool = False
    line_number: int = 0
    resolved_path: Optional[str] = None


@dataclass
class TypeInfo:
    """Type information for a symbol."""

    type_name: str
    is_generic: bool = False
    generic_params: List[str] = field(default_factory=list)
    is_nullable: bool = False
    is_interface: bool = False
    super_types: List[str] = field(default_factory=list)


@dataclass
class CrossFileReference:
    """Reference to a symbol in another file."""

    symbol: str
    source_file: str
    target_file: str
    line_number: int
    reference_type: str  # 'call', 'inherit', 'implement', 'import'


@dataclass
class BuildDependency:
    """External dependency from build system."""

    name: str
    version: str
    group_id: Optional[str] = None  # For Maven/Gradle
    registry: Optional[str] = None  # For npm, cargo, etc
    is_dev_dependency: bool = False


class IImportResolver(ABC):
    """Interface for resolving imports and dependencies."""

    @abstractmethod
    def resolve_import(self, import_info: ImportInfo, current_file: Path) -> Optional[Path]:
        """Resolve an import to its actual file path."""

    @abstractmethod
    def get_import_graph(self) -> Dict[str, Set[str]]:
        """Get the complete import dependency graph."""

    @abstractmethod
    def find_circular_dependencies(self) -> List[List[str]]:
        """Find circular dependencies in the import graph."""


class ITypeAnalyzer(ABC):
    """Interface for analyzing type systems."""

    @abstractmethod
    def get_type_info(self, symbol: str, file_path: str) -> Optional[TypeInfo]:
        """Get type information for a symbol."""

    @abstractmethod
    def find_implementations(self, interface_name: str) -> List[Tuple[str, str]]:
        """Find all implementations of an interface. Returns (class_name, file_path) tuples."""

    @abstractmethod
    def resolve_generic_type(self, type_expr: str, context: Dict[str, str]) -> str:
        """Resolve a generic type expression in the given context."""


class IBuildSystemIntegration(ABC):
    """Interface for build system integration."""

    @abstractmethod
    def parse_build_file(self, build_file_path: Path) -> List[BuildDependency]:
        """Parse build configuration and extract dependencies."""

    @abstractmethod
    def resolve_external_import(self, import_path: str) -> Optional[str]:
        """Resolve an import from external dependencies."""

    @abstractmethod
    def get_project_structure(self) -> Dict[str, Any]:
        """Get the project structure from build configuration."""


class ICrossFileAnalyzer(ABC):
    """Interface for cross-file analysis."""

    @abstractmethod
    def find_all_references(self, symbol: str, definition_file: str) -> List[CrossFileReference]:
        """Find all references to a symbol across files."""

    @abstractmethod
    def get_call_graph(self, function_name: str) -> Dict[str, Set[str]]:
        """Get the call graph for a function."""

    @abstractmethod
    def analyze_impact(self, file_path: str) -> Dict[str, List[str]]:
        """Analyze impact of changes to a file."""


class SpecializedPluginBase(GenericTreeSitterPlugin):
    """Base class for specialized language plugins with advanced features."""

    def __init__(
        self,
        language_config: Dict[str, Any],
        sqlite_store: Optional[SQLiteStore] = None,
        enable_semantic: bool = False,
    ):
        """Initialize specialized plugin with enhanced capabilities."""
        super().__init__(language_config, sqlite_store, enable_semantic)

        # Component initialization (lazy loaded)
        self._import_resolver: Optional[IImportResolver] = None
        self._type_analyzer: Optional[ITypeAnalyzer] = None
        self._build_system: Optional[IBuildSystemIntegration] = None
        self._cross_file_analyzer: Optional[ICrossFileAnalyzer] = None

        # Caches
        self._import_cache: Dict[str, ImportInfo] = {}
        self._type_cache: Dict[str, TypeInfo] = {}
        self._reference_cache: Dict[str, List[CrossFileReference]] = {}

        # Project context
        self._project_root: Optional[Path] = None
        self._build_files: List[Path] = []

    # Component creation methods (to be overridden by subclasses)

    @abstractmethod
    def _create_import_resolver(self) -> IImportResolver:
        """Create language-specific import resolver."""

    @abstractmethod
    def _create_type_analyzer(self) -> ITypeAnalyzer:
        """Create language-specific type analyzer."""

    @abstractmethod
    def _create_build_system(self) -> IBuildSystemIntegration:
        """Create language-specific build system integration."""

    @abstractmethod
    def _create_cross_file_analyzer(self) -> ICrossFileAnalyzer:
        """Create language-specific cross-file analyzer."""

    # Lazy loading properties

    @property
    def import_resolver(self) -> IImportResolver:
        """Get or create import resolver."""
        if self._import_resolver is None:
            self._import_resolver = self._create_import_resolver()
        return self._import_resolver

    @property
    def type_analyzer(self) -> ITypeAnalyzer:
        """Get or create type analyzer."""
        if self._type_analyzer is None:
            self._type_analyzer = self._create_type_analyzer()
        return self._type_analyzer

    @property
    def build_system(self) -> IBuildSystemIntegration:
        """Get or create build system integration."""
        if self._build_system is None:
            self._build_system = self._create_build_system()
        return self._build_system

    @property
    def cross_file_analyzer(self) -> ICrossFileAnalyzer:
        """Get or create cross-file analyzer."""
        if self._cross_file_analyzer is None:
            self._cross_file_analyzer = self._create_cross_file_analyzer()
        return self._cross_file_analyzer

    # Enhanced plugin methods

    def getDefinition(self, symbol: str) -> Optional[Dict]:
        """Enhanced definition lookup with type information."""
        # First try base implementation
        definition = super().getDefinition(symbol)

        if definition:
            # Enhance with type information
            type_info = self.type_analyzer.get_type_info(symbol, definition.get("defined_in", ""))
            if type_info:
                definition["type_info"] = {
                    "type": type_info.type_name,
                    "is_generic": type_info.is_generic,
                    "generic_params": type_info.generic_params,
                    "super_types": type_info.super_types,
                }

            # Add import information
            if self._is_imported_symbol(symbol):
                import_info = self._get_import_info(symbol)
                if import_info:
                    definition["import_info"] = {
                        "module": import_info.module_path,
                        "is_external": import_info.resolved_path is None,
                    }

        return definition

    def findReferences(self, symbol: str) -> List[Dict]:
        """Find all references to a symbol across files."""
        references = []

        # Get definition location
        definition = self.getDefinition(symbol)
        if not definition:
            return references

        definition_file = definition.get("defined_in", "")

        # Find cross-file references
        cross_refs = self.cross_file_analyzer.find_all_references(symbol, definition_file)

        for ref in cross_refs:
            references.append(
                {
                    "file": ref.source_file,
                    "line": ref.line_number,
                    "type": ref.reference_type,
                    "symbol": ref.symbol,
                }
            )

        return references

    def get_project_dependencies(self) -> List[BuildDependency]:
        """Get all project dependencies from build files."""
        dependencies = []

        # Find build files
        self._discover_build_files()

        # Parse each build file
        for build_file in self._build_files:
            try:
                deps = self.build_system.parse_build_file(build_file)
                dependencies.extend(deps)
            except Exception as e:
                logger.warning(f"Failed to parse build file {build_file}: {e}")

        return dependencies

    def analyze_imports(self, file_path: Path) -> List[ImportInfo]:
        """Analyze imports in a file."""
        # This should be implemented by subclasses
        return []

    def _discover_build_files(self):
        """Discover build files in the project."""
        # To be overridden by subclasses

    def _is_imported_symbol(self, symbol: str) -> bool:
        """Check if a symbol is imported."""
        # To be implemented by subclasses
        return False

    def _get_import_info(self, symbol: str) -> Optional[ImportInfo]:
        """Get import information for a symbol."""
        # To be implemented by subclasses
        return None

    def invalidate_file(self, file_path: str):
        """Invalidate caches when a file changes."""
        # Clear relevant caches
        self._import_cache = {
            k: v for k, v in self._import_cache.items() if not k.startswith(file_path)
        }
        self._type_cache = {
            k: v for k, v in self._type_cache.items() if not k.startswith(file_path)
        }
        self._reference_cache.pop(file_path, None)
