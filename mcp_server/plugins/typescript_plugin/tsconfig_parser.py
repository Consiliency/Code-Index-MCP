"""TypeScript configuration parser for tsconfig.json files."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class TSConfigParser:
    """Parser for TypeScript configuration files."""

    def __init__(self, project_root: Path | str):
        """Initialize the parser with a project root."""
        self.project_root = Path(project_root)
        self._config_cache: Dict[str, Dict[str, Any]] = {}

    def find_tsconfig(self, file_path: Path) -> Optional[Path]:
        """Find the nearest tsconfig.json file for a given TypeScript file."""
        current = file_path.parent if file_path.is_file() else file_path

        while current != current.parent:
            tsconfig_path = current / "tsconfig.json"
            if tsconfig_path.exists():
                return tsconfig_path
            current = current.parent

        # Fallback to project root
        root_config = self.project_root / "tsconfig.json"
        return root_config if root_config.exists() else None

    def parse_config(self, tsconfig_path: Path) -> Dict[str, Any]:
        """Parse a tsconfig.json file with support for extends."""
        config_key = str(tsconfig_path)
        if config_key in self._config_cache:
            return self._config_cache[config_key]

        try:
            with open(tsconfig_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            # Handle extends
            if "extends" in config:
                base_config = self._resolve_extends(tsconfig_path, config["extends"])
                merged_config = self._merge_configs(base_config, config)
            else:
                merged_config = config

            # Resolve paths
            merged_config = self._resolve_paths(tsconfig_path, merged_config)

            self._config_cache[config_key] = merged_config
            return merged_config

        except (json.JSONDecodeError, FileNotFoundError, OSError) as e:
            logger.warning(f"Failed to parse {tsconfig_path}: {e}")
            return self._get_default_config()

    def _resolve_extends(self, tsconfig_path: Path, extends: str) -> Dict[str, Any]:
        """Resolve the extends field in tsconfig.json."""
        if extends.startswith("."):
            # Relative path
            extended_path = (tsconfig_path.parent / extends).resolve()
            if not extended_path.suffix:
                extended_path = extended_path.with_suffix(".json")
        else:
            # Try to resolve as node module
            # For now, just use relative resolution
            extended_path = (tsconfig_path.parent / f"{extends}.json").resolve()

        if extended_path.exists():
            return self.parse_config(extended_path)
        else:
            logger.warning(f"Cannot resolve extends: {extends}")
            return {}

    def _merge_configs(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """Merge TypeScript configurations."""
        merged = base.copy()

        for key, value in override.items():
            if key == "compilerOptions" and key in merged:
                # Merge compiler options
                merged[key] = {**merged[key], **value}
            elif key == "include" or key == "exclude":
                # Override arrays completely
                merged[key] = value
            else:
                merged[key] = value

        return merged

    def _resolve_paths(self, tsconfig_path: Path, config: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve relative paths in the configuration."""
        if "compilerOptions" not in config:
            return config

        compiler_options = config["compilerOptions"]
        base_url = compiler_options.get("baseUrl")

        if base_url:
            # Convert relative baseUrl to absolute
            if not Path(base_url).is_absolute():
                compiler_options["baseUrl"] = str((tsconfig_path.parent / base_url).resolve())

        # Resolve paths mapping
        if "paths" in compiler_options:
            resolved_paths = {}
            base_path = Path(compiler_options.get("baseUrl", tsconfig_path.parent))

            for pattern, paths in compiler_options["paths"].items():
                resolved_list = []
                for path in paths:
                    if not Path(path).is_absolute():
                        resolved_list.append(str((base_path / path).resolve()))
                    else:
                        resolved_list.append(path)
                resolved_paths[pattern] = resolved_list

            compiler_options["paths"] = resolved_paths

        return config

    def _get_default_config(self) -> Dict[str, Any]:
        """Get default TypeScript configuration."""
        return {
            "compilerOptions": {
                "target": "es5",
                "module": "commonjs",
                "lib": ["dom", "dom.iterable", "es6"],
                "allowJs": True,
                "skipLibCheck": True,
                "esModuleInterop": True,
                "allowSyntheticDefaultImports": True,
                "strict": True,
                "forceConsistentCasingInFileNames": True,
                "moduleResolution": "node",
                "resolveJsonModule": True,
                "isolatedModules": True,
                "noEmit": True,
            },
            "include": ["**/*"],
            "exclude": ["node_modules"],
        }

    def get_compiler_options(self, file_path: Path) -> Dict[str, Any]:
        """Get compiler options for a specific file."""
        tsconfig_path = self.find_tsconfig(file_path)
        if tsconfig_path:
            config = self.parse_config(tsconfig_path)
            return config.get("compilerOptions", {})
        return self._get_default_config()["compilerOptions"]

    def get_include_patterns(self, file_path: Path) -> List[str]:
        """Get include patterns for a specific file."""
        tsconfig_path = self.find_tsconfig(file_path)
        if tsconfig_path:
            config = self.parse_config(tsconfig_path)
            return config.get("include", ["**/*"])
        return ["**/*"]

    def get_exclude_patterns(self, file_path: Path) -> List[str]:
        """Get exclude patterns for a specific file."""
        tsconfig_path = self.find_tsconfig(file_path)
        if tsconfig_path:
            config = self.parse_config(tsconfig_path)
            return config.get("exclude", ["node_modules"])
        return ["node_modules"]

    def is_file_included(self, file_path: Path) -> bool:
        """Check if a file should be included based on tsconfig patterns."""
        import fnmatch

        tsconfig_path = self.find_tsconfig(file_path)
        if not tsconfig_path:
            return True

        config = self.parse_config(tsconfig_path)
        include_patterns = config.get("include", ["**/*"])
        exclude_patterns = config.get("exclude", ["node_modules"])

        # Convert path to relative from tsconfig directory
        try:
            rel_path = file_path.relative_to(tsconfig_path.parent)
            rel_path_str = str(rel_path).replace("\\", "/")
        except ValueError:
            # File is outside project, include by default
            return True

        # Check exclude patterns first
        for pattern in exclude_patterns:
            if fnmatch.fnmatch(rel_path_str, pattern):
                return False

        # Check include patterns
        for pattern in include_patterns:
            if fnmatch.fnmatch(rel_path_str, pattern):
                return True

        # If no include patterns match, exclude by default
        return False

    def resolve_module_path(self, module_name: str, from_file: Path) -> Optional[Path]:
        """Resolve a module import to its file path using TypeScript resolution rules."""
        tsconfig_path = self.find_tsconfig(from_file)
        if not tsconfig_path:
            return None

        config = self.parse_config(tsconfig_path)
        compiler_options = config.get("compilerOptions", {})

        # Handle path mapping
        paths = compiler_options.get("paths", {})
        base_url = compiler_options.get("baseUrl")

        if base_url:
            base_path = Path(base_url)
        else:
            base_path = tsconfig_path.parent

        # Check path mappings first
        for pattern, mapped_paths in paths.items():
            if self._matches_pattern(module_name, pattern):
                for mapped_path in mapped_paths:
                    resolved = self._resolve_mapped_path(
                        module_name, pattern, mapped_path, base_path
                    )
                    if resolved and resolved.exists():
                        return resolved

        # Standard node module resolution
        return self._resolve_node_module(module_name, from_file.parent, base_path)

    def _matches_pattern(self, module_name: str, pattern: str) -> bool:
        """Check if a module name matches a path mapping pattern."""
        if "*" not in pattern:
            return module_name == pattern

        # Simple glob matching for now
        import re

        regex_pattern = pattern.replace("*", ".*")
        return bool(re.match(f"^{regex_pattern}$", module_name))

    def _resolve_mapped_path(
        self, module_name: str, pattern: str, mapped_path: str, base_path: Path
    ) -> Optional[Path]:
        """Resolve a path-mapped module."""
        if "*" in pattern and "*" in mapped_path:
            # Extract the wildcard part
            prefix, suffix = pattern.split("*", 1)
            if module_name.startswith(prefix) and module_name.endswith(suffix):
                wildcard = module_name[
                    len(prefix) : (len(module_name) - len(suffix) if suffix else len(module_name))
                ]
                resolved_path = mapped_path.replace("*", wildcard)
                return base_path / resolved_path
        elif "*" not in pattern:
            # Direct mapping
            return base_path / mapped_path

        return None

    def _resolve_node_module(
        self, module_name: str, start_dir: Path, base_path: Path
    ) -> Optional[Path]:
        """Resolve a module using Node.js resolution algorithm."""
        # Try relative/absolute imports first
        if module_name.startswith("."):
            resolved = (start_dir / module_name).resolve()

            # Try with common extensions
            for ext in [".ts", ".tsx", ".js", ".jsx", ".d.ts"]:
                if (resolved.with_suffix(ext)).exists():
                    return resolved.with_suffix(ext)

                # Try index files
                index_file = resolved / f"index{ext}"
                if index_file.exists():
                    return index_file

        # Try node_modules resolution
        current = start_dir
        while current != current.parent:
            node_modules = current / "node_modules" / module_name
            if node_modules.exists():
                # Check for package.json
                package_json = node_modules / "package.json"
                if package_json.exists():
                    try:
                        with open(package_json, "r") as f:
                            pkg_data = json.load(f)
                            types = pkg_data.get("types") or pkg_data.get("typings")
                            if types:
                                types_file = node_modules / types
                                if types_file.exists():
                                    return types_file

                            main = pkg_data.get("main", "index.js")
                            main_file = node_modules / main
                            if main_file.exists():
                                return main_file
                    except json.JSONDecodeError, OSError:
                        pass

                # Try index files
                for ext in [".d.ts", ".ts", ".tsx", ".js", ".jsx"]:
                    index_file = node_modules / f"index{ext}"
                    if index_file.exists():
                        return index_file

            current = current.parent

        return None
