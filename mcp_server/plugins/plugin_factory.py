"""Factory for creating language plugins.

This factory supports creating plugins for various programming languages and document formats,
including specialized plugins for:
- Programming languages: Python, JavaScript, TypeScript, C, C++, Java, Go, Rust, etc.
- Document formats: Markdown (.md, .markdown), Plain Text (.txt, .text, .log)
- Web technologies: HTML, CSS, Dart
- Other languages: Swift, Kotlin, C#

The factory will use specialized plugins when available, falling back to generic
tree-sitter based plugins for languages without specific implementations.
"""

import asyncio
import importlib.util
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Literal, Optional, Tuple, Type, Union

from ..plugin_base import construct_plugin
from ..storage.sqlite_store import SQLiteStore
from .generic_treesitter_plugin import GenericTreeSitterPlugin
from .language_registry import LANGUAGE_CONFIGS, get_language_by_extension

if TYPE_CHECKING:
    from ..core.repo_context import RepoContext
    from ..sandbox.capabilities import CapabilitySet

logger = logging.getLogger(__name__)


# Import specific plugins if they exist
# Can be either a plugin class or a factory function
SPECIFIC_PLUGINS: Dict[str, Union[Type, Callable]] = {}

# Dotted module paths for sandboxed-mode plugin construction. Each entry maps
# a language to the dotted module the worker subprocess imports. Kept in sync
# with SPECIFIC_PLUGINS by construction; see create_plugin's sandbox branch.
SPECIFIC_PLUGIN_MODULES: Dict[str, str] = {
    "python": "mcp_server.plugins.python_plugin",
    "javascript": "mcp_server.plugins.js_plugin",
    "typescript": "mcp_server.plugins.typescript_plugin",
    "c": "mcp_server.plugins.c_plugin",
    "cpp": "mcp_server.plugins.cpp_plugin",
    "dart": "mcp_server.plugins.dart_plugin",
    "html": "mcp_server.plugins.html_css_plugin",
    "css": "mcp_server.plugins.html_css_plugin",
    "java": "mcp_server.plugins.java_plugin",
    "go": "mcp_server.plugins.go_plugin",
    "rust": "mcp_server.plugins.rust_plugin",
    "c_sharp": "mcp_server.plugins.csharp_plugin",
    "csharp": "mcp_server.plugins.csharp_plugin",
    "swift": "mcp_server.plugins.swift_plugin",
    "kotlin": "mcp_server.plugins.kotlin_plugin",
    "markdown": "mcp_server.plugins.markdown_plugin",
}

AvailabilityState = Literal["enabled", "unsupported", "missing_extra", "disabled", "load_error"]

REQUIRED_PLUGIN_EXTRAS: Dict[str, Tuple[str, ...]] = {
    "java": ("javalang",),
}

EXTRA_REMEDIATION: Dict[str, str] = {
    "java": "Install the Java plugin extra with `uv sync --locked --extra java`.",
}


@dataclass(frozen=True)
class PluginAvailability:
    language: str
    state: AvailabilityState
    sandbox_supported: bool
    specific_plugin: bool
    plugin_module: Optional[str]
    availability_basis: str
    activation_mode: str
    required_extras: Tuple[str, ...] = ()
    remediation: Optional[str] = None
    error_type: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "state": self.state,
            "sandbox_supported": self.sandbox_supported,
            "specific_plugin": self.specific_plugin,
            "plugin_module": self.plugin_module,
            "availability_basis": self.availability_basis,
            "activation_mode": self.activation_mode,
            "required_extras": list(self.required_extras),
            "remediation": self.remediation,
            "error_type": self.error_type,
        }


class PluginUnavailableError(ValueError):
    """Expected plugin capability-unavailable state."""

    def __init__(self, availability: PluginAvailability):
        self.language = availability.language
        self.state = availability.state
        self.required_extras = availability.required_extras
        self.remediation = availability.remediation
        self.error_type = availability.error_type
        super().__init__(
            f"Plugin {availability.language!r} unavailable: {availability.state}"
            + (f" ({availability.remediation})" if availability.remediation else "")
        )

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "state": self.state,
            "availability_basis": "unknown",
            "activation_mode": "unavailable",
            "required_extras": list(self.required_extras),
            "remediation": self.remediation,
            "error_type": self.error_type,
        }


try:
    from .python_plugin import Plugin as PythonPlugin

    SPECIFIC_PLUGINS["python"] = PythonPlugin
except ImportError:
    logger.info("Python plugin not available, will use generic")

try:
    from .js_plugin import Plugin as JsPlugin

    SPECIFIC_PLUGINS["javascript"] = JsPlugin
except ImportError:
    logger.info("JavaScript plugin not available, will use generic")

try:
    from .typescript_plugin import Plugin as TypeScriptPlugin

    SPECIFIC_PLUGINS["typescript"] = TypeScriptPlugin
except ImportError:
    logger.info("TypeScript plugin not available, will use generic")

try:
    from .c_plugin import Plugin as CPlugin

    SPECIFIC_PLUGINS["c"] = CPlugin
except ImportError:
    logger.info("C plugin not available, will use generic")

try:
    from .cpp_plugin import Plugin as CppPlugin

    SPECIFIC_PLUGINS["cpp"] = CppPlugin
except ImportError:
    logger.info("C++ plugin not available, will use generic")

try:
    from .dart_plugin import Plugin as DartPlugin

    SPECIFIC_PLUGINS["dart"] = DartPlugin
except ImportError:
    logger.info("Dart plugin not available, will use generic")

try:
    from .html_css_plugin import Plugin as HtmlCssPlugin

    SPECIFIC_PLUGINS["html"] = HtmlCssPlugin
    SPECIFIC_PLUGINS["css"] = HtmlCssPlugin
except ImportError:
    logger.info("HTML/CSS plugin not available, will use generic")

# Specialized plugins for advanced language support
try:
    from .java_plugin import Plugin as JavaPlugin

    SPECIFIC_PLUGINS["java"] = JavaPlugin
except ImportError:
    logger.info("Java plugin not available, will use generic")

try:
    from .go_plugin import Plugin as GoPlugin

    SPECIFIC_PLUGINS["go"] = GoPlugin
except ImportError:
    logger.info("Go plugin not available, will use generic")

try:
    from .rust_plugin import Plugin as RustPlugin

    SPECIFIC_PLUGINS["rust"] = RustPlugin
except ImportError:
    logger.info("Rust plugin not available, will use generic")

try:
    from .csharp_plugin import Plugin as CSharpPlugin

    SPECIFIC_PLUGINS["c_sharp"] = CSharpPlugin
    SPECIFIC_PLUGINS["csharp"] = CSharpPlugin
except ImportError:
    logger.info("C# plugin not available, will use generic")

try:
    from .swift_plugin import Plugin as SwiftPlugin

    SPECIFIC_PLUGINS["swift"] = SwiftPlugin
except ImportError:
    logger.info("Swift plugin not available, will use generic")

try:
    from .kotlin_plugin import Plugin as KotlinPlugin

    SPECIFIC_PLUGINS["kotlin"] = KotlinPlugin
except ImportError:
    logger.info("Kotlin plugin not available, will use generic")

try:
    # Try simple text plugin first for config files
    from .simple_text_plugin import SimpleTextPlugin

    # Create a wrapper that provides the correct initialization
    def create_plaintext_plugin(sqlite_store=None, enable_semantic=False):
        return SimpleTextPlugin(sqlite_store)

    SPECIFIC_PLUGINS["plaintext"] = create_plaintext_plugin
except ImportError:
    logger.info("Plain text plugin not available, will use generic")

try:
    from .markdown_plugin import MarkdownPlugin

    SPECIFIC_PLUGINS["markdown"] = MarkdownPlugin
except ImportError:
    logger.info("Markdown plugin not available, will use generic")


class PluginFactory:
    """Factory for creating language plugins."""

    # Cache of in-flight/completed futures keyed by (language, repo_id).
    # Prevents duplicate cold-starts for concurrent requests to the same plugin.
    _async_cache: Dict[Tuple[str, str], "asyncio.Future"] = {}

    @staticmethod
    def _sandbox_enabled(sandbox_enabled: bool | None = None) -> bool:
        if sandbox_enabled is not None:
            return sandbox_enabled
        return os.environ.get("MCP_PLUGIN_SANDBOX_DISABLE") != "1"

    @classmethod
    def _missing_required_extras(cls, language: str) -> Tuple[str, ...]:
        return tuple(
            extra
            for extra in REQUIRED_PLUGIN_EXTRAS.get(language, ())
            if importlib.util.find_spec(extra) is None
        )

    @classmethod
    def get_plugin_availability(
        cls,
        language: str | None = None,
        *,
        sandbox_enabled: bool | None = None,
    ) -> list[dict] | dict:
        """Return stable plugin capability state rows for factory languages."""
        sandbox = cls._sandbox_enabled(sandbox_enabled)
        languages = [language] if language is not None else cls.get_supported_languages()
        rows: list[dict] = []

        for raw in languages:
            lang = raw.lower().replace("-", "_")
            known = lang in cls.get_supported_languages()
            plugin_module = SPECIFIC_PLUGIN_MODULES.get(lang)
            sandbox_supported = plugin_module is not None
            specific_plugin = lang in SPECIFIC_PLUGIN_MODULES or lang in SPECIFIC_PLUGINS
            availability_basis = "specific_plugin" if specific_plugin else "registry_only"
            required_extras = REQUIRED_PLUGIN_EXTRAS.get(lang, ())
            missing = cls._missing_required_extras(lang)

            if not known:
                state: AvailabilityState = "unsupported"
                availability_basis = "unknown"
                activation_mode = "unavailable"
                remediation = "Use one of PluginFactory.get_supported_languages()."
                error_type = "UnsupportedLanguage"
            elif sandbox and not sandbox_supported:
                state = "unsupported"
                activation_mode = "disabled_by_default"
                remediation = "Sandbox mode has no hardened plugin module for this language."
                error_type = "SandboxUnsupported"
            elif missing:
                state = "missing_extra"
                activation_mode = "extra_required"
                remediation = EXTRA_REMEDIATION.get(lang)
                error_type = "MissingOptionalDependency"
            else:
                state = "enabled"
                activation_mode = "default"
                remediation = None
                error_type = None

            rows.append(
                PluginAvailability(
                    language=lang,
                    state=state,
                    sandbox_supported=sandbox_supported,
                    specific_plugin=specific_plugin,
                    plugin_module=plugin_module,
                    availability_basis=availability_basis,
                    activation_mode=activation_mode,
                    required_extras=tuple(required_extras),
                    remediation=remediation,
                    error_type=error_type,
                ).to_dict()
            )

        return rows[0] if language is not None else rows

    @classmethod
    async def create_plugin_async(
        cls,
        language: str,
        ctx: "RepoContext",
        sqlite_store: Optional[SQLiteStore] = None,
        enable_semantic: bool = False,
    ):
        """Async plugin construction using the PluginLoader's ThreadPoolExecutor.

        Submits the synchronous ``create_plugin`` body (which already calls
        ``plugin.bind(ctx)``) to the shared executor so the event loop is never
        blocked.  Concurrent requests for the same (language, repo_id) share a
        single in-flight future.
        """
        language_norm = language.lower().replace("-", "_")
        # The flag is part of the key: a cached semantic-enabled instance must
        # never satisfy a later request with semantic disabled.
        cache_key = (language_norm, ctx.repo_id, bool(enable_semantic))

        loop = asyncio.get_event_loop()

        if cache_key in cls._async_cache:
            return await asyncio.shield(cls._async_cache[cache_key])

        # Reuse the executor owned by the global PluginLoader (4 workers).
        from ..plugin_system.loader import get_plugin_loader

        executor = get_plugin_loader()._executor

        fut: asyncio.Future = loop.run_in_executor(
            executor,
            lambda: cls.create_plugin(
                language_norm,
                sqlite_store=sqlite_store,
                enable_semantic=enable_semantic,
                ctx=ctx,
            ),
        )
        cls._async_cache[cache_key] = fut
        try:
            return await fut
        except Exception:
            cls._async_cache.pop(cache_key, None)
            raise

    @classmethod
    def create_plugin(
        cls,
        language: str,
        sqlite_store: Optional[SQLiteStore] = None,
        enable_semantic: bool = False,
        *,
        ctx: Optional["RepoContext"] = None,
        capabilities: Optional["CapabilitySet"] = None,
    ):
        """Create appropriate plugin for the language.

        Args:
            language: Language code (e.g., 'python', 'go', 'rust')
            sqlite_store: Optional SQLite storage
            enable_semantic: Deprecated plugin-level semantic search; off by default (#121)
            ctx: P3 IF-0-P3-5 — optional RepoContext. When provided, the factory
                calls ``plugin.bind(ctx)`` post-construction so the plugin can
                attach per-repo state. SL-1/SL-3/SL-5 preserve this hook through
                their rewrites.
            capabilities: P15 IF-0-P15-1 — optional CapabilitySet. When present
                the factory returns a :class:`SandboxedPlugin` that runs the
                plugin in an isolated subprocess. Sandboxing is default-on
                unless ``MCP_PLUGIN_SANDBOX_DISABLE=1`` is set.

        Returns:
            Plugin instance

        Raises:
            ValueError: If language is not supported
        """
        # Normalize language name
        language = language.lower().replace("-", "_")

        # SL-5 sandbox branch (default-on). Deactivated only when no capabilities
        # are passed AND MCP_PLUGIN_SANDBOX_DISABLE=1 is set explicitly.
        use_sandbox = capabilities is not None or (
            os.environ.get("MCP_PLUGIN_SANDBOX_DISABLE") != "1"
        )
        if use_sandbox:
            from ..sandbox.capabilities import CapabilitySet as _CapabilitySet
            from .sandboxed_plugin import SandboxedPlugin

            availability = cls.get_plugin_availability(language, sandbox_enabled=True)
            if availability["state"] != "enabled":
                raise PluginUnavailableError(
                    PluginAvailability(
                        language=availability["language"],
                        state=availability["state"],
                        sandbox_supported=availability["sandbox_supported"],
                        specific_plugin=availability["specific_plugin"],
                        plugin_module=availability["plugin_module"],
                        availability_basis=availability["availability_basis"],
                        activation_mode=availability["activation_mode"],
                        required_extras=tuple(availability["required_extras"]),
                        remediation=availability["remediation"],
                        error_type=availability["error_type"],
                    )
                )

            plugin_module = SPECIFIC_PLUGIN_MODULES.get(language)

            if capabilities is None:
                # Default caps: read-only access to the workspace, no writes,
                # no network, no sqlite, no env leakage.
                workspace: Tuple[Path, ...] = ()
                if ctx is not None and getattr(ctx, "workspace_root", None):
                    workspace = (Path(ctx.workspace_root),)
                capabilities = _CapabilitySet(
                    fs_read=workspace,
                    fs_write=(),
                    env_allow=frozenset(),
                    network=False,
                    sqlite="none",
                )

            plugin = SandboxedPlugin(plugin_module, capabilities)
            if ctx is not None:
                plugin.bind(ctx)
            return plugin

        plugin = None

        # Check for language-specific implementation first
        if language in SPECIFIC_PLUGINS:
            logger.info(f"Using specific plugin for {language}")
            plugin_class_or_factory = SPECIFIC_PLUGINS[language]
            # Check if it's a callable (factory function) or a class
            if callable(plugin_class_or_factory) and not isinstance(plugin_class_or_factory, type):
                # It's a factory function
                plugin = plugin_class_or_factory(
                    sqlite_store=sqlite_store, enable_semantic=enable_semantic
                )
            else:
                # It's a class: construct once with only the arguments its
                # __init__ names, so enable_semantic is honoured (#121).
                plugin = construct_plugin(
                    plugin_class_or_factory,
                    sqlite_store=sqlite_store,
                    enable_semantic=enable_semantic,
                )
        # Check if language is supported by generic plugin
        elif language in LANGUAGE_CONFIGS:
            availability = cls.get_plugin_availability(language, sandbox_enabled=False)
            if availability["state"] == "missing_extra":
                raise PluginUnavailableError(
                    PluginAvailability(
                        language=availability["language"],
                        state=availability["state"],
                        sandbox_supported=availability["sandbox_supported"],
                        specific_plugin=availability["specific_plugin"],
                        plugin_module=availability["plugin_module"],
                        availability_basis=availability["availability_basis"],
                        activation_mode=availability["activation_mode"],
                        required_extras=tuple(availability["required_extras"]),
                        remediation=availability["remediation"],
                        error_type=availability["error_type"],
                    )
                )
            logger.info(f"Using generic plugin for {language}")
            config = LANGUAGE_CONFIGS[language]
            plugin = GenericTreeSitterPlugin(config, sqlite_store, enable_semantic)
        else:
            # Language not supported
            supported = sorted(list(SPECIFIC_PLUGINS.keys()) + list(LANGUAGE_CONFIGS.keys()))
            raise ValueError(
                f"Unsupported language: {language}. " f"Supported languages: {', '.join(supported)}"
            )

        # IF-0-P3-5 post-construction hook
        if ctx is not None and hasattr(plugin, "bind"):
            plugin.bind(ctx)
        return plugin

    @classmethod
    def get_plugin(
        cls,
        language: str,
        sqlite_store: Optional[SQLiteStore] = None,
        enable_semantic: bool = False,
    ):
        """Alias for create_plugin for backward compatibility.

        Args:
            language: Language code (e.g., 'python', 'go', 'rust')
            sqlite_store: Optional SQLite storage
            enable_semantic: Deprecated plugin-level semantic search; off by default (#121)

        Returns:
            Plugin instance
        """
        return cls.create_plugin(language, sqlite_store, enable_semantic)

    @classmethod
    def create_plugin_for_file(
        cls,
        file_path: str | Path,
        sqlite_store: Optional[SQLiteStore] = None,
        enable_semantic: bool = False,
    ):
        """Create appropriate plugin based on file extension.

        Args:
            file_path: Path to the file
            sqlite_store: Optional SQLite storage
            enable_semantic: Deprecated plugin-level semantic search; off by default (#121)

        Returns:
            Plugin instance or None if no suitable plugin found
        """
        path = Path(file_path)
        extension = path.suffix.lower()

        # Get language from extension
        language = get_language_by_extension(extension)
        if language:
            return cls.create_plugin(language, sqlite_store, enable_semantic)

        logger.warning(f"No plugin found for extension: {extension}")
        return None

    @classmethod
    def get_supported_languages(cls) -> list[str]:
        """Get list of all supported languages."""
        # Combine specific and generic plugin languages
        all_languages = set(SPECIFIC_PLUGINS.keys())
        all_languages.update(LANGUAGE_CONFIGS.keys())
        return sorted(list(all_languages))

    @classmethod
    def get_language_info(cls, language: str) -> dict:
        """Get information about a language.

        Returns:
            Dictionary with language info or empty dict if not found
        """
        language = language.lower().replace("-", "_")

        if language in LANGUAGE_CONFIGS:
            config = LANGUAGE_CONFIGS[language].copy()
            config["has_specific_plugin"] = language in SPECIFIC_PLUGINS
            return config

        return {}

    @classmethod
    def create_all_plugins(
        cls, sqlite_store: Optional[SQLiteStore] = None, enable_semantic: bool = False
    ) -> Dict[str, object]:
        """Create plugins for all supported languages.

        Returns:
            Dictionary mapping language code to plugin instance
        """
        plugins = {}

        for language in cls.get_supported_languages():
            try:
                plugin = cls.create_plugin(language, sqlite_store, enable_semantic)
                plugins[language] = plugin
                logger.info(f"Created plugin for {language}")
            except PluginUnavailableError as e:
                logger.info(
                    "Skipping unavailable plugin for %s: %s",
                    language,
                    e.state,
                )
            except Exception as e:
                logger.error(f"Unexpected plugin construction failure for {language}: {e}")

        return plugins
