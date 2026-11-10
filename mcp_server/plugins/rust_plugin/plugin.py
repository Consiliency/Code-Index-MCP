"""Rust language plugin with advanced features."""

import logging
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
from ...storage.sqlite_store import SQLiteStore
from ..generic_treesitter_plugin import GenericTreeSitterPlugin
from .cargo_integration import CargoIntegration
from .module_resolver import RustModuleResolver
from .trait_analyzer import RustTraitAnalyzer

logger = logging.getLogger(__name__)


class SpecializedPluginBase(GenericTreeSitterPlugin):
    """Base class for specialized language plugins."""

    def __init__(
        self,
        language_config: Dict[str, Any],
        sqlite_store: Optional[SQLiteStore] = None,
        enable_semantic: bool = False,
    ) -> None:
        super().__init__(language_config, sqlite_store, enable_semantic)
        self._specialized_features = {}

    def add_feature(self, name: str, feature: Any) -> None:
        """Add a specialized feature to this plugin."""
        self._specialized_features[name] = feature

    def get_feature(self, name: str) -> Any:
        """Get a specialized feature by name."""
        return self._specialized_features.get(name)


class RustPlugin(SpecializedPluginBase):
    """Rust language plugin with module resolution, trait analysis, and Cargo integration."""

    # Rust-specific tree-sitter query for comprehensive symbol extraction
    RUST_QUERY = """
    ; Functions
    (function_item
      name: (identifier) @function.name) @function
    
    ; Methods in impl blocks
    (impl_item
      body: (declaration_list
        (function_item
          name: (identifier) @method.name))) @method
    
    ; Structs
    (struct_item
      name: (type_identifier) @struct.name) @struct
    
    ; Enums
    (enum_item
      name: (type_identifier) @enum.name) @enum
    
    ; Traits
    (trait_item
      name: (type_identifier) @trait.name) @trait
    
    ; Type aliases  
    (type_item
      name: (type_identifier) @type_alias.name) @type_alias
    
    ; Constants
    (const_item
      name: (identifier) @constant.name) @constant
    
    ; Statics
    (static_item
      name: (identifier) @static.name) @static
    
    ; Modules
    (mod_item
      name: (identifier) @module.name) @module
    
    ; Use declarations
    (use_declaration
      argument: (use_tree) @use) @use_decl
    
    ; Macros
    (macro_definition
      name: (identifier) @macro.name) @macro
    
    ; Impl blocks
    (impl_item
      trait: (type_identifier)? @impl.trait
      type: (_) @impl.type) @impl
    """

    def __init__(
        self, sqlite_store: Optional[SQLiteStore] = None, enable_semantic: bool = False
    ) -> None:
        """Initialize Rust plugin with specialized features."""
        config = {
            "code": "rust",
            "name": "Rust",
            "extensions": [".rs"],
            "symbols": [
                "function_item",
                "struct_item",
                "enum_item",
                "trait_item",
                "impl_item",
                "mod_item",
                "type_alias",
                "const_item",
                "static_item",
                "macro_definition",
            ],
            "query": self.RUST_QUERY,
        }

        super().__init__(config, sqlite_store, enable_semantic)

        # Initialize specialized features
        workspace_root = Path.cwd()
        self.add_feature("module_resolver", RustModuleResolver(workspace_root))
        self.add_feature("trait_analyzer", RustTraitAnalyzer())
        self.add_feature("cargo_integration", CargoIntegration(workspace_root))

        # Cache for macro expansions
        self._macro_cache = {}

        # Additional indexing for Rust-specific features
        self._trait_impls = {}  # trait -> list of implementing types
        self._module_tree = {}  # module hierarchy

        logger.info("Initialized Rust plugin with specialized features")

    def indexFile(self, path: str | Path, content: str) -> IndexShard:
        """Index a Rust file with enhanced analysis."""
        # First, do the base indexing
        shard = super().indexFile(path, content)

        # Add Rust-specific analysis
        if isinstance(path, str):
            path = Path(path)

        # Analyze traits and implementations
        trait_analyzer = self.get_feature("trait_analyzer")
        analysis = trait_analyzer.analyze_file(content)

        # Store trait implementations
        for impl_info in analysis["implementations"]:
            if impl_info.trait_name:
                if impl_info.trait_name not in self._trait_impls:
                    self._trait_impls[impl_info.trait_name] = []
                self._trait_impls[impl_info.trait_name].append(
                    {
                        "type": impl_info.target_type,
                        "file": str(path),
                        "line": impl_info.line_number,
                    }
                )

        # Analyze module structure
        module_resolver = self.get_feature("module_resolver")
        mod_decls = module_resolver.find_mod_declarations(content)
        for mod_name, line_num in mod_decls:
            self._module_tree[mod_name] = {
                "file": str(path),
                "line": line_num,
                "parent": self._get_parent_module(path),
            }

        # Check if this is a Cargo.toml file
        if path.name == "Cargo.toml":
            cargo = self.get_feature("cargo_integration")
            crate_info = cargo.parse_cargo_toml(path)
            if crate_info:
                logger.info(f"Indexed Cargo.toml for crate: {crate_info.name}")

        # Add enhanced symbols with Rust-specific metadata
        enhanced_symbols = []
        for symbol in shard["symbols"]:
            enhanced_symbol = symbol.copy()

            # Add trait bounds for generic functions
            if symbol["kind"] in ["function", "method"]:
                bounds = self._extract_trait_bounds(content, symbol["line"])
                if bounds:
                    enhanced_symbol["trait_bounds"] = bounds

            # Add lifetime information
            if symbol["kind"] in ["function", "struct", "impl"]:
                lifetimes = self._extract_lifetimes(content, symbol["line"])
                if lifetimes:
                    enhanced_symbol["lifetimes"] = lifetimes

            enhanced_symbols.append(enhanced_symbol)

        shard["symbols"] = enhanced_symbols
        shard["rust_analysis"] = analysis

        return shard

    def getDefinition(self, symbol: str) -> SymbolDef | None:
        """Get definition with module resolution support."""
        # First try the base implementation
        definition = super().getDefinition(symbol)
        if definition:
            return definition

        # Try module resolution if symbol contains ::
        if "::" in symbol:
            module_resolver = self.get_feature("module_resolver")
            parts = symbol.split("::")

            # Try to resolve the module path
            for ext in self.file_extensions:
                for path in Path(".").rglob(f"*{ext}"):
                    resolved = module_resolver.resolve_module_path(path, "::".join(parts[:-1]))
                    if resolved and resolved.exists():
                        try:
                            content = resolved.read_text(encoding="utf-8")
                            # Look for the last part of the symbol
                            if self.parser:
                                tree = self.parser.parse(content.encode("utf-8"))
                                symbols = self._extract_symbols(tree, content)
                                for sym in symbols:
                                    if sym["symbol"] == parts[-1]:
                                        return SymbolDef(
                                            symbol=symbol,
                                            kind=sym["kind"],
                                            language="rust",
                                            signature=sym["signature"],
                                            doc=None,
                                            defined_in=str(resolved),
                                            line=sym["line"],
                                            span=(
                                                sym["line"],
                                                sym.get("end_line", sym["line"]),
                                            ),
                                        )
                        except Exception:
                            continue

        return None

    def findReferences(self, symbol: str) -> list[Reference]:
        """Find references with use statement analysis."""
        refs = super().findReferences(symbol)

        # Add references from use statements
        module_resolver = self.get_feature("module_resolver")

        for ext in self.file_extensions:
            for path in Path(".").rglob(f"*{ext}"):
                try:
                    content = path.read_text(encoding="utf-8")
                    use_stmts = module_resolver.find_use_statements(content)

                    for use_path, line_num in use_stmts:
                        # Check if symbol is imported
                        if symbol in use_path:
                            refs.append(Reference(file=str(path), line=line_num))
                except Exception:
                    continue

        return refs

    def search(self, query: str, opts: SearchOpts | None = None) -> Iterable[SearchResult]:
        """Enhanced search with Rust-specific features."""
        # Get base results
        results = list(super().search(query, opts))

        # Check if searching for trait implementations
        if query.startswith("impl:"):
            trait_name = query[5:].strip()
            impl_results = self._search_trait_implementations(trait_name)
            results.extend(impl_results)

        # Check if searching for crate dependencies
        elif query.startswith("dep:"):
            dep_name = query[4:].strip()
            dep_results = self._search_dependencies(dep_name)
            results.extend(dep_results)

        # Check if searching for macros
        elif query.startswith("macro:"):
            macro_name = query[6:].strip()
            macro_results = self._search_macros(macro_name)
            results.extend(macro_results)

        return results

    def _search_trait_implementations(self, trait_name: str) -> List[SearchResult]:
        """Search for implementations of a specific trait."""
        results = []

        if trait_name in self._trait_impls:
            for impl_info in self._trait_impls[trait_name]:
                results.append(
                    SearchResult(
                        file=impl_info["file"],
                        start_line=impl_info["line"],
                        end_line=impl_info["line"],
                        column=0,
                        text=f"impl {trait_name} for {impl_info['type']}",
                        score=1.0,
                    )
                )

        return results

    def _search_dependencies(self, dep_name: str) -> List[SearchResult]:
        """Search for crate dependencies."""
        results = []
        cargo = self.get_feature("cargo_integration")

        for cargo_path in cargo.find_cargo_tomls():
            crate_info = cargo.parse_cargo_toml(cargo_path)
            if crate_info and dep_name in crate_info.dependencies:
                results.append(
                    SearchResult(
                        file=str(cargo_path),
                        start_line=1,
                        end_line=1,
                        column=0,
                        text=f"dependency: {dep_name} = {crate_info.dependencies[dep_name]}",
                        score=1.0,
                    )
                )

        return results

    def _search_macros(self, macro_name: str) -> List[SearchResult]:
        """Search for macro definitions and usage."""
        results = []
        pattern = re.compile(rf"\b{re.escape(macro_name)}!\s*[\(\[{{]")

        for ext in self.file_extensions:
            for path in Path(".").rglob(f"*{ext}"):
                try:
                    content = path.read_text(encoding="utf-8")
                    lines = content.split("\n")

                    for i, line in enumerate(lines):
                        if pattern.search(line):
                            results.append(
                                SearchResult(
                                    file=str(path),
                                    start_line=i + 1,
                                    end_line=i + 1,
                                    column=(line.index(macro_name) if macro_name in line else 0),
                                    text=line.strip(),
                                    score=0.9,
                                )
                            )
                except Exception:
                    continue

        return results

    def _extract_trait_bounds(self, content: str, line_num: int) -> List[str]:
        """Extract trait bounds from a function or impl block."""
        lines = content.split("\n")
        if line_num - 1 >= len(lines):
            return []

        bounds = []

        # Look for where clause
        where_pattern = re.compile(r"where\s+(.*?)(?:\{|;)", re.DOTALL)
        text = "\n".join(lines[line_num - 1 : min(line_num + 10, len(lines))])

        match = where_pattern.search(text)
        if match:
            where_clause = match.group(1)
            # Parse individual bounds
            bound_parts = re.split(r",\s*(?![^<>]*>)", where_clause)
            for part in bound_parts:
                if ":" in part:
                    bounds.append(part.strip())

        return bounds

    def _extract_lifetimes(self, content: str, line_num: int) -> List[str]:
        """Extract lifetime parameters from a definition."""
        lines = content.split("\n")
        if line_num - 1 >= len(lines):
            return []

        line = lines[line_num - 1]
        lifetime_pattern = re.compile(r"'(\w+)(?:\s*[:,])")

        lifetimes = []
        for match in lifetime_pattern.finditer(line):
            lifetime = match.group(1)
            if lifetime not in ["static"]:  # Exclude 'static
                lifetimes.append(f"'{lifetime}")

        return lifetimes

    def _get_parent_module(self, file_path: Path) -> Optional[str]:
        """Get the parent module for a file."""
        # Check if this is in a module directory
        if file_path.parent.name != "src" and (file_path.parent / "mod.rs").exists():
            return file_path.parent.name

        # Check if this is a submodule
        if file_path.stem != "mod" and file_path.stem != "lib" and file_path.stem != "main":
            return None

        return None

    def get_crate_info(self, crate_name: str) -> Optional[Dict[str, Any]]:
        """Get information about a specific crate."""
        cargo = self.get_feature("cargo_integration")
        crate_info = cargo.find_crate_by_name(crate_name)

        if crate_info:
            return {
                "name": crate_info.name,
                "version": crate_info.version,
                "path": str(crate_info.path),
                "dependencies": crate_info.dependencies,
                "features": crate_info.features,
            }

        return None

    def analyze_trait_hierarchy(self) -> Dict[str, List[str]]:
        """Get the complete trait hierarchy."""
        trait_analyzer = self.get_feature("trait_analyzer")
        all_traits = []

        # Collect all traits from indexed files
        for ext in self.file_extensions:
            for path in Path(".").rglob(f"*{ext}"):
                try:
                    content = path.read_text(encoding="utf-8")
                    analysis = trait_analyzer.analyze_file(content)
                    all_traits.extend(analysis["traits"])
                except Exception:
                    continue

        return trait_analyzer.get_trait_hierarchy(all_traits)
