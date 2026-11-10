"""
Dynamic plugin loader with lifecycle management.
Handles plugin initialization, configuration, and cleanup.
"""

import asyncio
import gc
import importlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Dict, Optional, Type

from ..interfaces.plugin_interfaces import IPlugin
from ..storage.sqlite_store import SQLiteStore
from .discovery import PluginDiscovery
from .models import PluginConfig, PluginState

logger = logging.getLogger(__name__)


class PluginLoader:
    """Manages plugin lifecycle and loading."""

    def __init__(self, discovery: PluginDiscovery = None):
        """
        Initialize plugin loader.

        Args:
            discovery: Plugin discovery instance
        """
        self.discovery = discovery or PluginDiscovery()
        self.loaded_plugins: Dict[str, IPlugin] = {}
        self.plugin_states: Dict[str, PluginState] = {}
        self.plugin_configs: Dict[str, PluginConfig] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=4)

    def load_plugin(self, language: str, config: PluginConfig = None) -> Optional[IPlugin]:
        """
        Load a plugin with configuration.

        Args:
            language: Language identifier
            config: Plugin configuration

        Returns:
            Loaded plugin instance or None
        """
        with self._lock:
            # Check if already loaded
            if language in self.loaded_plugins:
                state = self.plugin_states.get(language, PluginState.UNKNOWN)
                if state == PluginState.ACTIVE:
                    return self.loaded_plugins[language]

            try:
                # Get plugin info
                plugin_info = self.discovery.get_plugin_info(language)
                if not plugin_info:
                    logger.warning(f"No plugin found for language: {language}")
                    return None

                # Apply configuration
                if config:
                    self.plugin_configs[language] = config
                else:
                    config = self.plugin_configs.get(language, PluginConfig())

                # Create plugin instance
                logger.info(f"Loading plugin for {language}...")
                self.plugin_states[language] = PluginState.LOADING

                # Get plugin class
                if "class" in plugin_info:
                    plugin_class = plugin_info["class"]
                else:
                    module_path, class_name = plugin_info["entry_point"].rsplit(".", 1)
                    module = importlib.import_module(module_path)
                    plugin_class = getattr(module, class_name)

                # Initialize plugin
                plugin = self._initialize_plugin(plugin_class, config)

                # Store and activate
                self.loaded_plugins[language] = plugin
                self.plugin_states[language] = PluginState.ACTIVE

                logger.info(f"Successfully loaded plugin for {language}")
                return plugin

            except Exception as e:
                logger.error(f"Failed to load plugin for {language}: {e}")
                self.plugin_states[language] = PluginState.ERROR
                return None

    def _initialize_plugin(self, plugin_class: Type[IPlugin], config: PluginConfig) -> IPlugin:
        """Initialize a plugin with configuration."""
        # Create SQLite store if needed
        sqlite_store = None
        if config.enable_storage:
            db_path = config.storage_path or f"/tmp/{plugin_class.__name__.lower()}.db"
            sqlite_store = SQLiteStore(db_path)

        # Check constructor signature
        import inspect

        sig = inspect.signature(plugin_class.__init__)
        params = sig.parameters

        # Build kwargs based on what the plugin accepts
        kwargs = {}
        if "sqlite_store" in params:
            kwargs["sqlite_store"] = sqlite_store
        if "enable_semantic" in params:
            # Plugin-level semantic search is deprecated; never enabled here (#121).
            kwargs["enable_semantic"] = False
        if "config" in params:
            kwargs["config"] = config.to_dict()

        # Create instance
        if sqlite_store and "sqlite_store" in params:
            plugin = plugin_class(sqlite_store, **kwargs)
        else:
            plugin = plugin_class(**kwargs)

        return plugin

    def unload_plugin(self, language: str):
        """
        Unload a plugin and clean up resources.

        Args:
            language: Language identifier
        """
        with self._lock:
            if language not in self.loaded_plugins:
                return

            logger.info(f"Unloading plugin for {language}...")
            self.plugin_states[language] = PluginState.UNLOADING

            try:
                plugin = self.loaded_plugins[language]

                # Call cleanup if available
                if hasattr(plugin, "cleanup"):
                    plugin.cleanup()

                # Close storage if any
                if hasattr(plugin, "sqlite_store") and plugin.sqlite_store:
                    plugin.sqlite_store.close()

                # Remove from loaded plugins
                del self.loaded_plugins[language]

                # Force garbage collection
                gc.collect()

                self.plugin_states[language] = PluginState.UNLOADED
                logger.info(f"Successfully unloaded plugin for {language}")

            except Exception as e:
                logger.error(f"Error unloading plugin for {language}: {e}")
                self.plugin_states[language] = PluginState.ERROR

    def reload_plugin(self, language: str, config: PluginConfig = None) -> Optional[IPlugin]:
        """
        Reload a plugin with optional new configuration.

        Args:
            language: Language identifier
            config: New plugin configuration

        Returns:
            Reloaded plugin instance
        """
        logger.info(f"Reloading plugin for {language}...")

        # Unload existing
        self.unload_plugin(language)

        # Load with new config
        return self.load_plugin(language, config)

    def load_all_plugins(self, configs: Dict[str, PluginConfig] = None):
        """
        Load all discovered plugins.

        Args:
            configs: Optional configurations per language
        """
        configs = configs or {}

        # Discover plugins if not done
        if not self.discovery.discovered_plugins:
            self.discovery.discover_plugins()

        languages = self.discovery.get_supported_languages()
        logger.info(f"Loading {len(languages)} plugins...")

        # Load in parallel
        futures = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            for language in languages:
                config = configs.get(language)
                future = executor.submit(self.load_plugin, language, config)
                futures.append((language, future))

        # Wait for all to complete
        for language, future in futures:
            try:
                future.result(timeout=10)
            except Exception as e:
                logger.error(f"Failed to load {language} plugin: {e}")

    def get_plugin(self, language: str) -> Optional[IPlugin]:
        """
        Get a loaded plugin.

        Args:
            language: Language identifier

        Returns:
            Plugin instance or None
        """
        plugin = self.loaded_plugins.get(language)
        if not plugin:
            # Try to load it
            plugin = self.load_plugin(language)
        return plugin

    def get_active_plugins(self) -> Dict[str, IPlugin]:
        """Get all active plugins."""
        with self._lock:
            return {
                lang: plugin
                for lang, plugin in self.loaded_plugins.items()
                if self.plugin_states.get(lang) == PluginState.ACTIVE
            }

    def get_plugin_state(self, language: str) -> PluginState:
        """Get the state of a plugin."""
        return self.plugin_states.get(language, PluginState.UNKNOWN)

    def set_plugin_config(self, language: str, config: PluginConfig):
        """
        Set configuration for a plugin.

        Args:
            language: Language identifier
            config: Plugin configuration
        """
        self.plugin_configs[language] = config

        # Reload if already loaded
        if language in self.loaded_plugins:
            self.reload_plugin(language, config)

    @contextmanager
    def plugin_context(self, language: str, config: PluginConfig = None):
        """
        Context manager for temporary plugin usage.

        Args:
            language: Language identifier
            config: Optional configuration
        """
        plugin = self.load_plugin(language, config)
        try:
            yield plugin
        finally:
            self.unload_plugin(language)

    async def load_plugin_async(
        self, language: str, config: PluginConfig = None
    ) -> Optional[IPlugin]:
        """
        Asynchronously load a plugin.

        Args:
            language: Language identifier
            config: Plugin configuration

        Returns:
            Loaded plugin instance
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, self.load_plugin, language, config)

    def cleanup(self):
        """Clean up all resources."""
        logger.info("Cleaning up plugin loader...")

        # Unload all plugins
        languages = list(self.loaded_plugins.keys())
        for language in languages:
            self.unload_plugin(language)

        # Shutdown executor
        self._executor.shutdown(wait=True)

        logger.info("Plugin loader cleanup complete")

    def get_statistics(self) -> Dict[str, Any]:
        """Get loader statistics."""
        with self._lock:
            state_counts = {}
            for state in PluginState:
                count = sum(1 for s in self.plugin_states.values() if s == state)
                state_counts[state.name] = count

            return {
                "total_discovered": len(self.discovery.discovered_plugins),
                "total_loaded": len(self.loaded_plugins),
                "active_plugins": sum(
                    1 for s in self.plugin_states.values() if s == PluginState.ACTIVE
                ),
                "state_distribution": state_counts,
                "languages": list(self.loaded_plugins.keys()),
            }


# Global plugin loader instance
_loader = None


def get_plugin_loader() -> PluginLoader:
    """Get the global plugin loader instance."""
    global _loader
    if _loader is None:
        from .discovery import get_plugin_discovery

        discovery = get_plugin_discovery()
        _loader = PluginLoader(discovery)
    return _loader
