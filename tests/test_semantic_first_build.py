"""Code blockers for a first semantic build (Consiliency/Code-Index-MCP#121).

C1: plugin-level semantic search is off by default everywhere and never enabled by
the dispatcher, plugin managers, loaders or the sandbox worker, so no language plugin
builds its own legacy ``SemanticIndexer``.
C3: ``SEMANTIC_SUMMARY_PASS_BUDGET`` bounds (or unbounds) the repo-scope summary passes.

No network, no Qdrant: the summarizer, preflight and semantic indexer are test doubles.
"""

from __future__ import annotations

import inspect
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, List

import pytest

from mcp_server.config.env_vars import (
    DEFAULT_SEMANTIC_SUMMARY_PASS_BUDGET,
    get_semantic_summary_pass_budget,
)
from mcp_server.core.repo_context import RepoContext
from mcp_server.dispatcher.dispatcher_enhanced import EnhancedDispatcher as Dispatcher
from mcp_server.indexing.summarization import ComprehensiveChunkWriter, SummaryGenerationResult
from mcp_server.plugin_base import IPlugin, SearchOpts, SearchResult, construct_plugin
from mcp_server.plugin_base_enhanced import PluginWithSemanticSearch
from mcp_server.plugins import plugin_factory
from mcp_server.plugins.memory_aware_manager import MemoryAwarePluginManager
from mcp_server.plugins.plugin_factory import PluginFactory
from mcp_server.storage.sqlite_store import SQLiteStore

PROBE_LANG = "probe_semantic_lang"


@pytest.fixture(autouse=True)
def _no_local_dotenv(monkeypatch):
    """Settings reads would load repo .env files into os.environ for the whole process."""
    monkeypatch.setattr("mcp_server.config.environment._DOTENV_LOADED", True)


class _LegacyIndexerRecorder:
    """Stands in for SemanticIndexer; records every construction."""

    created: List[dict] = []

    def __init__(self, *args, **kwargs):
        _LegacyIndexerRecorder.created.append(dict(kwargs))


class _ProbePlugin(PluginWithSemanticSearch):
    """Minimal plugin on the deprecated base, keeping the old ``True`` default.

    The worst case for callers: only an explicit ``enable_semantic=False`` keeps it off.
    """

    lang = PROBE_LANG

    def __init__(self, sqlite_store=None, enable_semantic: bool = True):
        super().__init__(sqlite_store=sqlite_store, enable_semantic=enable_semantic)

    def supports(self, path) -> bool:
        return str(path).endswith(".probe")

    def indexFile(self, path, content):
        return {"file": str(path), "symbols": [], "language": PROBE_LANG}

    def getDefinition(self, symbol):
        return None

    def findReferences(self, symbol):
        return []

    def _traditional_search(
        self, query: str, opts: SearchOpts | None = None
    ) -> Iterable[SearchResult]:
        return []


@pytest.fixture
def legacy_semantic_env(monkeypatch):
    """SEMANTIC_SEARCH_ENABLED=true, in-process plugins, recorded legacy indexers."""
    monkeypatch.setenv("SEMANTIC_SEARCH_ENABLED", "true")
    monkeypatch.setenv("MCP_PLUGIN_SANDBOX_DISABLE", "1")
    monkeypatch.setenv("QDRANT_HOST", ":memory:")
    monkeypatch.setitem(plugin_factory.SPECIFIC_PLUGINS, PROBE_LANG, _ProbePlugin)
    monkeypatch.setattr("mcp_server.plugin_base_enhanced.SemanticIndexer", _LegacyIndexerRecorder)
    monkeypatch.setattr(
        "mcp_server.plugins.typescript_plugin.plugin.SemanticIndexer", _LegacyIndexerRecorder
    )
    monkeypatch.setattr(_LegacyIndexerRecorder, "created", [])
    return _LegacyIndexerRecorder


def _assert_no_plugin_semantic(plugin) -> None:
    assert plugin._enable_semantic is False
    assert plugin._semantic_indexer is None


# ---------------------------------------------------------------------------
# C1: plugin-level semantic search stays off
# ---------------------------------------------------------------------------


class TestPluginLevelSemanticDisabled:
    def test_recorder_catches_the_legacy_path(self, legacy_semantic_env):
        """Control: a plugin built with semantic on does create a legacy indexer."""
        with pytest.warns(DeprecationWarning, match="Plugin-level semantic search"):
            plugin = PluginFactory.create_plugin(PROBE_LANG, None, enable_semantic=True)

        assert plugin._enable_semantic is True
        assert legacy_semantic_env.created == [
            {"collection": f"code-embeddings-{PROBE_LANG}", "qdrant_path": ":memory:"}
        ]

    def test_factory_passes_enable_semantic_false_to_plugin_classes(self, legacy_semantic_env):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            plugin = PluginFactory.create_plugin(PROBE_LANG, None, enable_semantic=False)

        _assert_no_plugin_semantic(plugin)
        assert legacy_semantic_env.created == []

    def test_lazy_loaded_plugin_has_semantic_disabled(self, legacy_semantic_env):
        dispatcher = Dispatcher(semantic_search_enabled=True, lazy_load=True)

        plugin = dispatcher._ensure_plugin_loaded(PROBE_LANG)

        _assert_no_plugin_semantic(plugin)
        assert legacy_semantic_env.created == []
        # The dispatcher's own generation-scoped semantic path stays enabled.
        assert dispatcher._semantic_enabled is True
        capabilities = {cap.name for cap in dispatcher._detect_plugin_capabilities(plugin)}
        assert "semantic_search" not in capabilities

    def test_eager_load_passes_semantic_disabled(self, legacy_semantic_env, monkeypatch):
        calls = []

        def fake_create_all_plugins(sqlite_store=None, enable_semantic=True):
            calls.append(enable_semantic)
            return {
                PROBE_LANG: PluginFactory.create_plugin(PROBE_LANG, sqlite_store, enable_semantic)
            }

        monkeypatch.setattr(PluginFactory, "create_all_plugins", fake_create_all_plugins)

        dispatcher = Dispatcher(semantic_search_enabled=True, lazy_load=False)

        assert calls == [False]
        _assert_no_plugin_semantic(dispatcher._lang_cache[PROBE_LANG])
        assert legacy_semantic_env.created == []

    def test_repository_aware_load_passes_semantic_disabled(self, legacy_semantic_env):
        dispatcher = Dispatcher(semantic_search_enabled=True, lazy_load=True)
        dispatcher._repo_plugin_loader = SimpleNamespace(
            get_required_plugins=lambda: [PROBE_LANG],
            get_priority_languages=lambda: [PROBE_LANG],
            log_loading_plan=lambda: None,
            mark_loaded=lambda _lang: None,
        )

        dispatcher._load_all_plugins()

        _assert_no_plugin_semantic(dispatcher._lang_cache[PROBE_LANG])
        assert legacy_semantic_env.created == []

    def test_memory_aware_manager_builds_plugins_with_semantic_disabled(self, legacy_semantic_env):
        manager = MemoryAwarePluginManager(max_memory_mb=4096)
        try:
            plugin = manager.get_plugin(PROBE_LANG)
        finally:
            manager.shutdown()

        _assert_no_plugin_semantic(plugin)
        assert legacy_semantic_env.created == []

    def test_typescript_plugin_honours_enable_semantic_false(
        self, legacy_semantic_env, monkeypatch
    ):
        pytest.importorskip("mcp_server.plugins.typescript_plugin.plugin")
        monkeypatch.setenv("VOYAGE_API_KEY", "fixture-not-a-key")
        try:
            plugin = PluginFactory.create_plugin("typescript", None, enable_semantic=False)
        except Exception as exc:  # pragma: no cover - grammar unavailable offline
            pytest.skip(f"TypeScript grammar unavailable: {type(exc).__name__}")

        assert plugin._semantic_indexer is None
        assert legacy_semantic_env.created == []


# ---------------------------------------------------------------------------
# C3: SEMANTIC_SUMMARY_PASS_BUDGET
# ---------------------------------------------------------------------------


class TestSemanticSummaryPassBudgetEnv:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("SEMANTIC_SUMMARY_PASS_BUDGET", raising=False)
        assert get_semantic_summary_pass_budget() == DEFAULT_SEMANTIC_SUMMARY_PASS_BUDGET == 8

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0", None),
            (" 0 ", None),
            ("1", 1),
            ("32", 32),
            (" 12 ", 12),
            ("", 8),
            ("   ", 8),
            ("-1", 8),
            ("abc", 8),
            ("2.5", 8),
        ],
    )
    def test_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv("SEMANTIC_SUMMARY_PASS_BUDGET", raw)
        assert get_semantic_summary_pass_budget() == expected


FIXTURE_FILES = 12


def _build_fixture_repo(tmp_path: Path) -> tuple[RepoContext, SQLiteStore, List[Path]]:
    """A repo with more single-chunk files than the default pass budget."""
    repo = tmp_path / "repo"
    repo.mkdir()
    store = SQLiteStore(str(tmp_path / "index.db"))
    repository_row = store.ensure_repository_row(repo, name="repo")
    paths: List[Path] = []
    for idx in range(FIXTURE_FILES):
        source = (repo / f"module_{idx:02d}.py").resolve()
        content = f"def func_{idx}():\n    return {idx}\n"
        source.write_text(content, encoding="utf-8")
        file_id = store.store_file(
            repository_row,
            path=source,
            relative_path=source.name,
            language="python",
            size=len(content),
            content_hash=f"hash-{idx}",
        )
        store.store_chunk(
            file_id=file_id,
            content=content,
            content_start=0,
            content_end=len(content),
            line_start=1,
            line_end=2,
            chunk_id=f"chunk-{idx:02d}",
            node_id=f"node-{idx:02d}",
            treesitter_file_id=f"tree-{idx:02d}",
            language="python",
            node_type="function_definition",
        )
        paths.append(source)
    ctx = RepoContext(
        repo_id="semantic-first-build-repo",
        sqlite_store=store,
        workspace_root=repo,
        tracked_branch="main",
        registry_entry=SimpleNamespace(
            tracked_branch="main",
            path=repo,
            name="repo",
            repository_id="semantic-first-build-repo",
        ),
    )
    return ctx, store, paths


class _FakeSemanticIndexer:
    """Loopback embedder: records the strict batch write instead of calling Qdrant."""

    def __init__(self):
        self.batches: List[List[Path]] = []

    def index_files_batch(self, paths, **kwargs):
        assert kwargs["require_summaries"] is True
        self.batches.append(list(paths))
        return {"files_indexed": len(paths), "files_failed": 0, "files_skipped": 0}


@pytest.fixture
def loopback_semantic(monkeypatch):
    """Fake summarizer writing authoritative summaries, fake preflight and embedder."""
    summarized_files: List[str] = []

    async def fake_summarize_file_chunks(self, **kwargs):
        store = SQLiteStore(self.db_path)
        try:
            for chunk in kwargs["chunks"]:
                store.store_chunk_summary(
                    chunk_hash=chunk["chunk_id"],
                    file_id=chunk["file_id"],
                    chunk_start=chunk["line_start"],
                    chunk_end=chunk["line_end"],
                    summary_text=f"summary for {chunk['chunk_id']}",
                    llm_model="loopback",
                    is_authoritative=True,
                    provider_name="loopback",
                    profile_id=self.summarization_config.get("profile_id"),
                    prompt_fingerprint=self._prompt_fingerprint(),
                )
        finally:
            store.close()
        summarized_files.append(kwargs["file_path"])
        written = len(kwargs["chunks"])
        return SummaryGenerationResult(
            chunks_attempted=written,
            summaries_written=written,
            authoritative_chunks=written,
            files_attempted=1,
            files_summarized=1,
        )

    monkeypatch.setattr(ComprehensiveChunkWriter, "can_summarize", lambda self: True)
    monkeypatch.setattr(
        ComprehensiveChunkWriter, "summarize_file_chunks", fake_summarize_file_chunks
    )
    monkeypatch.setattr(
        "mcp_server.setup.semantic_preflight.run_semantic_preflight",
        lambda **_kwargs: SimpleNamespace(
            to_dict=lambda: {"can_write_semantic_vectors": True, "blocker": None}
        ),
    )
    indexer = _FakeSemanticIndexer()
    monkeypatch.setattr(Dispatcher, "_get_semantic_indexer", lambda self, _ctx: indexer)
    return SimpleNamespace(indexer=indexer, summarized_files=summarized_files)


class TestSummaryPassBudgetRebuild:
    def test_default_budget_blocks_a_repo_larger_than_the_budget(
        self, tmp_path, monkeypatch, loopback_semantic
    ):
        monkeypatch.delenv("SEMANTIC_SUMMARY_PASS_BUDGET", raising=False)
        ctx, store, paths = _build_fixture_repo(tmp_path)
        try:
            result = Dispatcher([]).rebuild_semantic_for_paths(ctx, paths)
        finally:
            store.close()

        assert result["semantic_stage"] == "blocked_missing_summaries"
        assert result["summary_passes"] == 8
        assert result["summaries_written"] == 8
        assert result["summary_remaining_chunks"] == FIXTURE_FILES - 8
        assert result["summary_missing_chunks"] == FIXTURE_FILES - 8
        assert result["summary_continuation_required"] is True
        assert result["summary_scope_drained"] is False
        assert len(loopback_semantic.summarized_files) == 8
        # Strict pipeline: no vectors are written while summaries are missing.
        assert loopback_semantic.indexer.batches == []

    def test_unbounded_budget_summarizes_everything_then_embeds(
        self, tmp_path, monkeypatch, loopback_semantic
    ):
        monkeypatch.setenv("SEMANTIC_SUMMARY_PASS_BUDGET", "0")
        ctx, store, paths = _build_fixture_repo(tmp_path)
        try:
            result = Dispatcher([]).rebuild_semantic_for_paths(ctx, paths)
        finally:
            store.close()

        assert result["semantic_stage"] == "indexed"
        assert result["summary_passes"] == FIXTURE_FILES
        assert result["summaries_written"] == FIXTURE_FILES
        assert result["summary_remaining_chunks"] == 0
        assert result["summary_missing_chunks"] == 0
        assert result["summary_scope_drained"] is True
        assert result["summary_continuation_required"] is False
        assert sorted(loopback_semantic.summarized_files) == sorted(str(p) for p in paths)
        assert loopback_semantic.indexer.batches == [paths]
        assert result["semantic_indexed"] == FIXTURE_FILES

    def test_explicit_budget_is_honoured(self, tmp_path, monkeypatch, loopback_semantic):
        monkeypatch.setenv("SEMANTIC_SUMMARY_PASS_BUDGET", "3")
        ctx, store, paths = _build_fixture_repo(tmp_path)
        try:
            result = Dispatcher([]).rebuild_semantic_for_paths(ctx, paths)
        finally:
            store.close()

        assert result["semantic_stage"] == "blocked_missing_summaries"
        assert result["summary_passes"] == 3
        assert result["summary_remaining_chunks"] == FIXTURE_FILES - 3
        assert "3 bounded summary passes" in result["semantic_error"]
        assert loopback_semantic.indexer.batches == []

    def test_bounded_budget_caps_zero_progress_passes(
        self, tmp_path, monkeypatch, loopback_semantic
    ):
        """A summarizer making no progress must not exceed a bounded budget."""
        monkeypatch.setenv("SEMANTIC_SUMMARY_PASS_BUDGET", "1")
        limits: List[int] = []

        async def no_progress_process_scope(self, **kwargs):
            limits.append(kwargs["limit"])
            return SummaryGenerationResult(chunks_attempted=1, summaries_written=0)

        monkeypatch.setattr(ComprehensiveChunkWriter, "process_scope", no_progress_process_scope)
        ctx, store, paths = _build_fixture_repo(tmp_path)
        try:
            result = Dispatcher([]).rebuild_semantic_for_paths(ctx, paths)
        finally:
            store.close()

        assert result["summary_passes"] == 1
        assert limits == [64]
        assert result["semantic_stage"] == "blocked_missing_summaries"
        assert result["summary_continuation_required"] is True
        assert result["summary_remaining_chunks"] == FIXTURE_FILES
        assert "1 bounded summary passes" in result["semantic_error"]
        assert loopback_semantic.indexer.batches == []

    def test_unbounded_budget_ends_zero_progress_at_the_size_one_guard(
        self, tmp_path, monkeypatch, loopback_semantic
    ):
        monkeypatch.setenv("SEMANTIC_SUMMARY_PASS_BUDGET", "0")
        limits: List[int] = []

        async def no_progress_process_scope(self, **kwargs):
            limits.append(kwargs["limit"])
            return SummaryGenerationResult(chunks_attempted=1, summaries_written=0)

        monkeypatch.setattr(ComprehensiveChunkWriter, "process_scope", no_progress_process_scope)
        ctx, store, paths = _build_fixture_repo(tmp_path)
        try:
            result = Dispatcher([]).rebuild_semantic_for_paths(ctx, paths)
        finally:
            store.close()

        assert limits == [64, 32, 16, 8, 4, 2, 1]
        assert result["semantic_stage"] == "blocked_missing_summaries"
        assert result["summary_remaining_chunks"] == FIXTURE_FILES
        assert loopback_semantic.indexer.batches == []


# ---------------------------------------------------------------------------
# Defense in depth: defaults, stdio plugin manager, cache key, sandbox, constructor
# ---------------------------------------------------------------------------


def _default_of(func, name: str = "enable_semantic"):
    return inspect.signature(func).parameters[name].default


class TestPluginSemanticDefaultsOff:
    def test_factory_methods_default_semantic_off(self):
        for method in (
            PluginFactory.create_plugin,
            PluginFactory.create_plugin_async,
            PluginFactory.get_plugin,
            PluginFactory.create_plugin_for_file,
            PluginFactory.create_all_plugins,
        ):
            assert _default_of(method) is False, method.__name__
        plaintext = plugin_factory.SPECIFIC_PLUGINS.get("plaintext")
        if plaintext is not None and not isinstance(plaintext, type):
            assert _default_of(plaintext) is False

    def test_plugin_constructors_default_semantic_off(self):
        from mcp_server.document_processing.base_document_plugin import BaseDocumentPlugin
        from mcp_server.plugin_system.discovery import PluginDiscovery
        from mcp_server.plugins.generic_treesitter_plugin import GenericTreeSitterPlugin
        from mcp_server.plugins.specialized_plugin_base import SpecializedPluginBase

        classes = [
            PluginWithSemanticSearch,
            GenericTreeSitterPlugin,
            SpecializedPluginBase,
            BaseDocumentPlugin,
        ]
        classes += [
            cls for cls in plugin_factory.SPECIFIC_PLUGINS.values() if isinstance(cls, type)
        ]
        checked = 0
        for cls in classes:
            params = inspect.signature(cls.__init__).parameters
            if "enable_semantic" in params:
                assert params["enable_semantic"].default is False, cls
                checked += 1
        assert checked >= 6
        assert _default_of(PluginDiscovery.load_plugin) is False

    def test_factory_default_builds_no_legacy_indexer(self, legacy_semantic_env):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            plugin = PluginFactory.create_plugin(PROBE_LANG)

        _assert_no_plugin_semantic(plugin)
        assert legacy_semantic_env.created == []

    def test_base_default_builds_no_legacy_indexer(self, legacy_semantic_env):
        class _DefaultBase(_ProbePlugin):
            def __init__(self, sqlite_store=None):
                PluginWithSemanticSearch.__init__(self, sqlite_store=sqlite_store)

        _assert_no_plugin_semantic(_DefaultBase())
        assert legacy_semantic_env.created == []

    def test_typescript_default_builds_no_legacy_indexer(self, legacy_semantic_env, monkeypatch):
        pytest.importorskip("mcp_server.plugins.typescript_plugin.plugin")
        monkeypatch.setenv("VOYAGE_API_KEY", "fixture-not-a-key")
        try:
            plugin = PluginFactory.create_plugin("typescript")
        except Exception as exc:  # pragma: no cover - grammar unavailable offline
            pytest.skip(f"TypeScript grammar unavailable: {type(exc).__name__}")

        assert plugin._semantic_indexer is None
        assert legacy_semantic_env.created == []

    @pytest.mark.asyncio
    async def test_async_cache_key_includes_semantic_flag(self, legacy_semantic_env):
        ctx = SimpleNamespace(repo_id="semantic-cache-key-repo")
        keys = [(PROBE_LANG, ctx.repo_id, True), (PROBE_LANG, ctx.repo_id, False)]
        try:
            with pytest.warns(DeprecationWarning):
                enabled = await PluginFactory.create_plugin_async(
                    PROBE_LANG, ctx, enable_semantic=True
                )
            disabled = await PluginFactory.create_plugin_async(
                PROBE_LANG, ctx, enable_semantic=False
            )
            default = await PluginFactory.create_plugin_async(PROBE_LANG, ctx)
        finally:
            for key in keys:
                PluginFactory._async_cache.pop(key, None)

        assert enabled is not disabled
        assert enabled._enable_semantic is True
        _assert_no_plugin_semantic(disabled)
        assert default is disabled


class TestStdioPluginManagerPath:
    def test_initialize_plugin_passes_semantic_off(self, legacy_semantic_env, tmp_path):
        from mcp_server.plugin_system.models import (
            PluginConfig,
            PluginInfo,
            PluginInstance,
            PluginSystemConfig,
            PluginType,
        )
        from mcp_server.plugin_system.plugin_manager import PluginManager

        store = SQLiteStore(str(tmp_path / "index.db"))
        manager = PluginManager(
            config=PluginSystemConfig(auto_discover=False, auto_load=False),
            sqlite_store=store,
        )
        manager._registry.get_plugin = lambda _name: _ProbePlugin
        manager._instances["probe"] = PluginInstance(
            info=PluginInfo(
                name="probe",
                version="0",
                description="probe",
                author="tests",
                plugin_type=PluginType.LANGUAGE,
                language=PROBE_LANG,
                file_extensions=[".probe"],
                path=tmp_path,
                module_name="probe",
            ),
            config=PluginConfig(),
            instance=None,
        )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", DeprecationWarning)
                plugin = manager.initialize_plugin("probe", {})
        finally:
            store.close()

        _assert_no_plugin_semantic(plugin)
        assert legacy_semantic_env.created == []

    def test_discovery_load_plugin_passes_semantic_off(self, legacy_semantic_env):
        from mcp_server.plugin_system.discovery import PluginDiscovery

        discovery = PluginDiscovery.__new__(PluginDiscovery)
        discovery._plugin_cache = {}
        discovery.discovered_plugins = {PROBE_LANG: {"name": "probe", "class": _ProbePlugin}}

        plugin = discovery.load_plugin(PROBE_LANG)

        _assert_no_plugin_semantic(plugin)
        assert legacy_semantic_env.created == []


class _RecordingMeta(type):
    """Metaclass exposing ``(**kwargs)`` while ``__init__`` takes no enable_semantic."""

    def __call__(cls, **kwargs):
        return super().__call__(**kwargs)


class _KwargsFacadePlugin(IPlugin, metaclass=type("_M", (_RecordingMeta, type(IPlugin)), {})):
    lang = "kwargs_facade"

    def __init__(self, sqlite_store=None):
        self.sqlite_store = sqlite_store

    def supports(self, path):
        return False

    def indexFile(self, path, content):
        return {"file": str(path), "symbols": [], "language": self.lang}

    def getDefinition(self, symbol):
        return None

    def findReferences(self, symbol):
        return []

    def search(self, query, opts=None):
        return []


class TestCentralConstructorAndSandbox:
    def test_kwargs_facade_without_enable_semantic_is_constructed(self, monkeypatch):
        monkeypatch.setenv("MCP_PLUGIN_SANDBOX_DISABLE", "1")
        monkeypatch.setitem(plugin_factory.SPECIFIC_PLUGINS, "kwargs_facade", _KwargsFacadePlugin)

        plugin = PluginFactory.create_plugin("kwargs_facade", "store-sentinel")

        assert isinstance(plugin, _KwargsFacadePlugin)
        assert plugin.sqlite_store == "store-sentinel"

    def test_construct_plugin_ignores_semantic_for_classes_without_it(self):
        plugin = construct_plugin(_KwargsFacadePlugin, sqlite_store=None, enable_semantic=True)
        assert isinstance(plugin, _KwargsFacadePlugin)

    def test_sandbox_instantiate_passes_semantic_off_once(self):
        from mcp_server.sandbox import worker_main

        received = []

        class _LegacyDefault:
            def __init__(self, sqlite_store=None, enable_semantic=True):
                received.append((sqlite_store, enable_semantic))

        worker_main._instantiate(_LegacyDefault)

        assert received == [(None, False)]

    def test_sandbox_instantiate_does_not_retry_internal_type_error(self):
        from mcp_server.sandbox import worker_main

        received = []

        class _FailsInside:
            def __init__(self, sqlite_store=None, enable_semantic=True):
                received.append(enable_semantic)
                raise TypeError("raised inside the constructor")

        with pytest.raises(TypeError, match="inside the constructor"):
            worker_main._instantiate(_FailsInside)

        # One call, with semantic off; never retried into the True default.
        assert received == [False]

    def test_sandbox_instantiate_without_semantic_parameter(self):
        from mcp_server.sandbox import worker_main

        received = []

        class _StoreOnly:
            def __init__(self, sqlite_store=None):
                received.append(sqlite_store)

        class _NoArgs:
            def __init__(self):
                received.append("no-args")

        worker_main._instantiate(_StoreOnly)
        worker_main._instantiate(_NoArgs)

        assert received == [None, "no-args"]

    def test_construct_plugin_passes_positional_only_parameters(self):
        received = []

        class _PositionalOnly:
            def __init__(self, sqlite_store=None, enable_semantic=True, /):
                received.append((sqlite_store, enable_semantic))

        construct_plugin(_PositionalOnly, sqlite_store="store-sentinel", enable_semantic=False)
        construct_plugin(_PositionalOnly, sqlite_store="store-sentinel", enable_semantic=True)

        assert received == [("store-sentinel", False), ("store-sentinel", True)]

    def test_construct_plugin_fills_unknown_positional_only_defaults_in_order(self):
        received = []

        class _Mixed:
            def __init__(self, sqlite_store=None, mode="default-mode", enable_semantic=True, /):
                received.append((sqlite_store, mode, enable_semantic))

        class _TrailingUnknown:
            def __init__(self, sqlite_store=None, mode="default-mode", /, *, enable_semantic=True):
                received.append((sqlite_store, mode, enable_semantic))

        construct_plugin(_Mixed, sqlite_store="s", enable_semantic=False)
        construct_plugin(_TrailingUnknown, sqlite_store="s", enable_semantic=False)

        assert received == [("s", "default-mode", False), ("s", "default-mode", False)]

    def test_construct_plugin_rejects_unknown_required_positional_only(self):
        called = []

        class _NeedsConfig:
            def __init__(self, config, /, sqlite_store=None, enable_semantic=True):
                called.append(config)

        with pytest.raises(TypeError, match="positional-only argument 'config'"):
            construct_plugin(_NeedsConfig, sqlite_store=None, enable_semantic=False)
        assert called == []

    def test_construct_plugin_inspects_custom_new_when_init_is_inherited(self):
        received = []

        class _NewOnly:
            def __new__(cls, sqlite_store=None, enable_semantic=True):
                received.append((sqlite_store, enable_semantic))
                return super().__new__(cls)

        construct_plugin(_NewOnly, sqlite_store="s", enable_semantic=False)

        assert received == [("s", False)]

    def test_construct_plugin_inspects_factory_callable_signature(self):
        received = []

        def factory(sqlite_store=None, enable_semantic=True):
            received.append((sqlite_store, enable_semantic))
            return object()

        def kwargs_factory(**kwargs):
            received.append(dict(kwargs))
            return object()

        construct_plugin(factory, sqlite_store="s", enable_semantic=False)
        construct_plugin(kwargs_factory, sqlite_store="s", enable_semantic=True)

        # Bare **kwargs never implies plugin-level semantic support.
        assert received == [("s", False), {"sqlite_store": "s"}]

    def test_construct_plugin_passes_flag_to_custom_new_behind_kwargs_init(self):
        received = []

        class _NewAndKwargsInit:
            def __new__(cls, sqlite_store=None, enable_semantic=True, **_kwargs):
                received.append(enable_semantic)
                return super().__new__(cls)

            def __init__(self, **kwargs):
                pass

        construct_plugin(_NewAndKwargsInit, sqlite_store=None, enable_semantic=False)

        assert received == [False]

    def test_construct_plugin_rejects_partial_prebinding_semantic(self):
        import functools

        def factory(sqlite_store=None, enable_semantic=False):
            return object()

        with pytest.raises(TypeError, match="pre-binds enable_semantic=True"):
            construct_plugin(functools.partial(factory, None, True), enable_semantic=False)
        with pytest.raises(TypeError, match="pre-binds enable_semantic=True"):
            construct_plugin(
                functools.partial(factory, enable_semantic=True), enable_semantic=False
            )
        # A partial that binds nothing semantic still constructs.
        assert construct_plugin(functools.partial(factory, None), enable_semantic=False)

    def test_construct_plugin_refuses_plugin_that_enabled_semantic_anyway(self):
        class _Opaque:
            # Introspection sees no enable_semantic, but the plugin turns it on.
            def __init__(self, *args, **kwargs):
                self._enable_semantic = True

        with pytest.raises(RuntimeError, match="enabled plugin-level semantic search"):
            construct_plugin(_Opaque, sqlite_store=None, enable_semantic=False)

    def test_construct_plugin_guard_ignores_mock_attributes(self):
        from unittest.mock import MagicMock

        plugin = construct_plugin(MagicMock, sqlite_store=None, enable_semantic=False)

        assert isinstance(plugin, MagicMock)

    def test_sandbox_instantiate_positional_only_semantic_off(self):
        from mcp_server.sandbox import worker_main

        received = []

        class _PositionalOnly:
            def __init__(self, sqlite_store="unset", enable_semantic=True, /):
                received.append((sqlite_store, enable_semantic))

        worker_main._instantiate(_PositionalOnly)

        assert received == [(None, False)]


class TestTypeScriptDeprecatedOptIn:
    @pytest.fixture
    def typescript_plugin_cls(self, legacy_semantic_env, monkeypatch):
        module = pytest.importorskip("mcp_server.plugins.typescript_plugin.plugin")
        monkeypatch.setenv("VOYAGE_API_KEY", "fixture-not-a-key")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                module.Plugin(enable_semantic=False)
        except Exception as exc:  # pragma: no cover - grammar unavailable offline
            pytest.skip(f"TypeScript grammar unavailable: {type(exc).__name__}")
        return module.Plugin

    def test_explicit_opt_in_warns_and_builds_legacy_indexer(
        self, typescript_plugin_cls, legacy_semantic_env
    ):
        with pytest.warns(DeprecationWarning, match="Plugin-level semantic search"):
            plugin = typescript_plugin_cls(enable_semantic=True)

        assert plugin._semantic_indexer is not None
        assert legacy_semantic_env.created == [{"collection": f"typescript-{id(plugin)}"}]

    def test_opt_in_without_semantic_search_enabled_is_off_and_silent(
        self, typescript_plugin_cls, legacy_semantic_env, monkeypatch
    ):
        monkeypatch.setenv("SEMANTIC_SEARCH_ENABLED", "false")
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            plugin = typescript_plugin_cls(enable_semantic=True)

        assert plugin._semantic_indexer is None
        assert legacy_semantic_env.created == []

    def test_default_is_off_and_silent(self, typescript_plugin_cls, legacy_semantic_env):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            plugin = typescript_plugin_cls()

        assert plugin._semantic_indexer is None
        assert legacy_semantic_env.created == []
