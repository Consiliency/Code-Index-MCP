"""
Plugin discovery system for dynamic plugin loading.
Automatically discovers and registers available plugins.
"""

import importlib
import inspect
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

import yaml

from ..interfaces.plugin_interfaces import IPlugin
from ..storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class PluginDiscovery:
    """Discovers and loads plugins dynamically."""

    def __init__(self, plugin_dirs: List[str] = None):
        """
        Initialize plugin discovery.

        Args:
            plugin_dirs: List of directories to search for plugins
        """
        self.plugin_dirs = plugin_dirs or [os.path.join(os.path.dirname(__file__), "..", "plugins")]
        self.discovered_plugins: Dict[str, Dict[str, Any]] = {}
        self._plugin_cache: Dict[str, Type[IPlugin]] = {}

    def discover_plugins(self) -> Dict[str, Dict[str, Any]]:
        """
        Discover all available plugins.

        Returns:
            Dictionary mapping language to plugin info
        """
        self.discovered_plugins.clear()

        for plugin_dir in self.plugin_dirs:
            if not os.path.exists(plugin_dir):
                logger.warning(f"Plugin directory not found: {plugin_dir}")
                continue

            logger.info(f"Scanning plugin directory: {plugin_dir}")
            self._scan_directory(plugin_dir)

        logger.info(f"Discovered {len(self.discovered_plugins)} plugins")
        return self.discovered_plugins

    def _scan_directory(self, directory: str):
        """Scan a directory for plugins."""
        plugin_dir = Path(directory)

        # Look for plugin packages (directories with __init__.py)
        for item in plugin_dir.iterdir():
            if item.is_dir() and (item / "__init__.py").exists():
                self._check_plugin_package(item)

        # Also check for standalone plugin modules
        for item in plugin_dir.glob("*.py"):
            if item.name not in ["__init__.py", "plugin_factory.py"]:
                self._check_plugin_module(item)

    def _check_plugin_package(self, package_path: Path):
        """Check if a package contains a plugin."""
        # Look for plugin.yaml manifest
        manifest_path = package_path / "plugin.yaml"
        if manifest_path.exists():
            self._load_plugin_manifest(manifest_path, package_path)
        else:
            # Try to find plugin.py
            plugin_module = package_path / "plugin.py"
            if plugin_module.exists():
                self._auto_discover_plugin(package_path)

    def _check_plugin_module(self, module_path: Path):
        """Check if a module contains a plugin."""
        if module_path.stem.endswith("_plugin"):
            self._auto_discover_plugin(module_path, is_module=True)

    def _load_plugin_manifest(self, manifest_path: Path, plugin_path: Path):
        """Load plugin information from manifest file."""
        try:
            with open(manifest_path, "r") as f:
                manifest = yaml.safe_load(f)

            plugin_info = {
                "name": manifest.get("name"),
                "version": manifest.get("version", "1.0.0"),
                "description": manifest.get("description", ""),
                "languages": manifest.get("languages", []),
                "entry_point": manifest.get("entry_point", "plugin.Plugin"),
                "path": str(plugin_path),
                "manifest": manifest,
            }

            # Register for each supported language
            for language in plugin_info["languages"]:
                self.discovered_plugins[language.lower()] = plugin_info
                logger.info(f"Registered plugin for {language}: {plugin_info['name']}")

        except Exception as e:
            logger.error(f"Failed to load plugin manifest {manifest_path}: {e}")

    def _auto_discover_plugin(self, plugin_path: Path, is_module: bool = False):
        """Auto-discover plugin without manifest."""
        try:
            # Determine module name
            if is_module:
                module_name = f"mcp_server.plugins.{plugin_path.stem}"
            else:
                parent = plugin_path.parent.name
                module_name = f"mcp_server.plugins.{parent}.plugin"

            # Try to import the module
            module = importlib.import_module(module_name)

            # Find plugin class
            plugin_class = None
            for name, obj in inspect.getmembers(module):
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, IPlugin)
                    and obj != IPlugin
                    and not inspect.isabstract(obj)
                ):
                    plugin_class = obj
                    break

            if plugin_class:
                # Extract language from class or module
                language = self._extract_language(plugin_class, plugin_path)
                if language:
                    plugin_info = {
                        "name": plugin_class.__name__,
                        "version": getattr(plugin_class, "__version__", "1.0.0"),
                        "description": plugin_class.__doc__ or "",
                        "languages": [language],
                        "entry_point": f"{module_name}.{plugin_class.__name__}",
                        "path": str(plugin_path),
                        "class": plugin_class,
                    }

                    self.discovered_plugins[language.lower()] = plugin_info
                    logger.info(f"Auto-discovered plugin for {language}: {plugin_class.__name__}")

        except Exception as e:
            logger.debug(f"Failed to auto-discover plugin at {plugin_path}: {e}")

    def _extract_language(self, plugin_class: Type, plugin_path: Path) -> Optional[str]:
        """Extract language from plugin class or path."""
        # Check class attributes
        if hasattr(plugin_class, "language"):
            return plugin_class.language
        declared_language = getattr(plugin_class, "lang", None)
        if isinstance(declared_language, str) and declared_language:
            return declared_language
        if hasattr(plugin_class, "get_language"):
            try:
                # Create temporary instance
                temp = plugin_class(None)
                return temp.get_language()
            except Exception:
                pass

        # Extract from path/name
        path_str = str(plugin_path)
        language_map = {
            "python": "python",
            "javascript": "javascript",
            "js": "javascript",
            "java": "java",
            "go": "go",
            "rust": "rust",
            "csharp": "csharp",
            "c_sharp": "csharp",
            "swift": "swift",
            "kotlin": "kotlin",
            "typescript": "typescript",
            "c_plugin": "c",
            "cpp": "cpp",
            "dart": "dart",
            "html": "html",
            "css": "css",
            "markdown": "markdown",
            "plaintext": "plaintext",
        }

        for key, lang in language_map.items():
            if key in path_str.lower():
                return lang

        return None

    def load_plugin(
        self,
        language: str,
        sqlite_store: SQLiteStore = None,
        enable_semantic: bool = True,
    ) -> Optional[IPlugin]:
        """
        Load a plugin for the specified language.

        Args:
            language: Language identifier
            sqlite_store: SQLite storage instance
            enable_semantic: Enable semantic search features

        Returns:
            Plugin instance or None if not found
        """
        language = language.lower()

        # Check cache first
        cache_key = f"{language}:{id(sqlite_store)}:{enable_semantic}"
        if cache_key in self._plugin_cache:
            return self._plugin_cache[cache_key]

        # Get plugin info
        plugin_info = self.discovered_plugins.get(language)
        if not plugin_info:
            logger.warning(f"No plugin found for language: {language}")
            return None

        try:
            # Load plugin class if not already loaded
            if "class" not in plugin_info:
                module_path, class_name = plugin_info["entry_point"].rsplit(".", 1)
                module = importlib.import_module(module_path)
                plugin_class = getattr(module, class_name)
                plugin_info["class"] = plugin_class
            else:
                plugin_class = plugin_info["class"]

            # Create plugin instance
            if "semantic" in inspect.signature(plugin_class.__init__).parameters:
                plugin = plugin_class(sqlite_store, enable_semantic=enable_semantic)
            else:
                plugin = plugin_class(sqlite_store)

            # Cache the instance
            self._plugin_cache[cache_key] = plugin

            logger.info(f"Loaded plugin for {language}: {plugin_info['name']}")
            return plugin

        except Exception as e:
            logger.error(f"Failed to load plugin for {language}: {e}")
            return None

    def get_supported_languages(self) -> List[str]:
        """Get list of supported languages."""
        return sorted(list(self.discovered_plugins.keys()))

    def get_plugin_info(self, language: str) -> Optional[Dict[str, Any]]:
        """Get information about a plugin."""
        return self.discovered_plugins.get(language.lower())

    def reload_plugins(self):
        """Reload all plugins (clears cache and re-discovers)."""
        self._plugin_cache.clear()
        self.discover_plugins()
        logger.info("Plugins reloaded")

    def register_plugin(
        self,
        language: str,
        plugin_class: Type[IPlugin],
        name: str = None,
        version: str = "1.0.0",
    ):
        """
        Manually register a plugin.

        Args:
            language: Language identifier
            plugin_class: Plugin class
            name: Plugin name (defaults to class name)
            version: Plugin version
        """
        plugin_info = {
            "name": name or plugin_class.__name__,
            "version": version,
            "description": plugin_class.__doc__ or "",
            "languages": [language],
            "entry_point": f"{plugin_class.__module__}.{plugin_class.__name__}",
            "path": "manual",
            "class": plugin_class,
        }

        self.discovered_plugins[language.lower()] = plugin_info
        logger.info(f"Manually registered plugin for {language}: {plugin_info['name']}")


# Global plugin discovery instance
_discovery = None


def get_plugin_discovery() -> PluginDiscovery:
    """Get the global plugin discovery instance."""
    global _discovery
    if _discovery is None:
        _discovery = PluginDiscovery()
        _discovery.discover_plugins()
    return _discovery
