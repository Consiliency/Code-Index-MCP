"""Local programmatic Python client for indexed repository access."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from mcp_server.cli.bootstrap import initialize_stateless_services
from mcp_server.core.repo_context import RepoContext
from mcp_server.core.repo_resolver import RepoResolver, run_repository_mutation
from mcp_server.dispatcher.protocol import DispatcherProtocol
from mcp_server.health.repository_readiness import ReadinessClassifier

from .client_types import (
    ClientReindexResult,
    ClientSearchMatch,
    ClientSearchOptions,
    ClientSearchResult,
    ClientStatusResult,
    ClientSymbolResult,
    IndexUnavailable,
)


class ClientValidationError(ValueError):
    def __init__(self, error: str, details: str, *, allowed_categories: list[str] | None = None):
        super().__init__(details)
        self.error = error
        self.details = details
        self.allowed_categories = allowed_categories or []


def _as_tuple(values: Sequence[str] | str | None) -> tuple[str, ...]:
    if values in (None, "", []):
        return ()
    if isinstance(values, str):
        return tuple(item.strip() for item in values.split(",") if item.strip())
    return tuple(str(item).strip() for item in values if str(item).strip())


def build_search_options(
    *,
    query: str,
    repository: str | Path | None = None,
    semantic: bool = False,
    fuzzy: bool = False,
    limit: int = 20,
    source_type: str | None = None,
    friction_categories: Sequence[str] | str | None = None,
    history_labels: Sequence[str] | str | None = None,
    history_repos: Sequence[str] | str | None = None,
    include_source_metadata: bool = False,
) -> ClientSearchOptions:
    try:
        return ClientSearchOptions(
            query=query,
            repository=str(repository) if repository is not None else None,
            semantic=semantic,
            fuzzy=fuzzy,
            limit=limit,
            source_type=source_type,  # type: ignore[arg-type]
            friction_categories=_as_tuple(friction_categories),
            history_labels=_as_tuple(history_labels),
            history_repos=_as_tuple(history_repos),
            include_source_metadata=include_source_metadata,
        )
    except ValueError as exc:
        if str(exc) == "'query' parameter is required":
            raise ClientValidationError(
                "Invalid query",
                "'query' parameter is required",
            ) from exc
        if "Unknown friction category" in str(exc):
            from mcp_server.indexing.source_metadata import FRICTION_CATEGORIES

            raise ClientValidationError(
                "Invalid friction categories",
                str(exc),
                allowed_categories=list(FRICTION_CATEGORIES),
            ) from exc
        if str(exc).startswith("'") or "SourceType" in str(exc):
            raise ClientValidationError(
                "Invalid source type",
                "source_type must be 'friction' or 'history' when provided",
            ) from exc
        raise


def _index_unavailable(readiness: Any) -> IndexUnavailable:
    return IndexUnavailable(
        readiness=readiness.to_dict(),
        message="Indexed search is available only when repository readiness is ready.",
        remediation=readiness.remediation,
    )


def _generation_unavailable(repo_resolver, ctx) -> IndexUnavailable | None:
    if isinstance(repo_resolver, RepoResolver) and (
        ctx is None or not repo_resolver.is_current(ctx)
    ):
        return IndexUnavailable(
            readiness={},
            message="Repository generation changed during query admission or execution.",
            remediation="Re-check readiness and retry against the current generation.",
        )
    return None


def _unavailable_search(options: ClientSearchOptions, unavailable: IndexUnavailable):
    return ClientSearchResult(
        query=options.query,
        message=unavailable.message,
        readiness=unavailable.readiness,
        index_unavailable=unavailable,
        code=unavailable.code,
        safe_fallback=unavailable.safe_fallback,
        remediation=unavailable.remediation,
        semantic_requested=options.semantic,
        source_type=options.source_type,
        friction_categories=options.friction_categories,
        history_labels=options.history_labels,
        history_repos=options.history_repos,
        include_source_metadata=options.include_source_metadata,
    )


def _resolve_readiness(
    repo_resolver: RepoResolver,
    repository: str | None,
    *,
    workspace_root: Path | None = None,
) -> Any:
    if repository:
        target = repository
    else:
        target = str(workspace_root or Path.cwd())
    readiness = repo_resolver.classify(target)

    registry = getattr(repo_resolver, "_registry", None)
    repo_id = getattr(readiness, "repository_id", None)
    if registry is not None and repo_id is not None:
        try:
            registry.update_git_state(repo_id)
        except Exception:
            return readiness
        return repo_resolver.classify(target)

    return readiness


def _resolve_ctx(
    repo_resolver: RepoResolver,
    repository: str | None,
    *,
    workspace_root: Path | None = None,
) -> RepoContext | None:
    if repository:
        target = repository
    else:
        target = str(workspace_root or Path.cwd())
    try:
        return (
            repo_resolver.resolve_ready(target)
            if isinstance(repo_resolver, RepoResolver)
            else repo_resolver.resolve(target)
        )
    except Exception:
        return None


def _search_kwargs(options: ClientSearchOptions) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "semantic": options.semantic,
        "fuzzy": options.fuzzy,
        "limit": options.limit,
    }
    if (
        options.source_type is not None
        or options.friction_categories
        or options.history_labels
        or options.history_repos
        or options.include_source_metadata
    ):
        kwargs["source_type"] = options.source_type.value if options.source_type else None
        kwargs["friction_categories"] = list(options.friction_categories)
        if options.history_labels:
            kwargs["history_labels"] = list(options.history_labels)
        if options.history_repos:
            kwargs["history_repos"] = list(options.history_repos)
        kwargs["include_source_metadata"] = options.include_source_metadata
    return kwargs


def _search_match_from_raw(raw: Any) -> ClientSearchMatch:
    if isinstance(raw, dict):
        return ClientSearchMatch(
            file=str(raw.get("file", "")),
            line=raw.get("line"),
            line_end=raw.get("line_end"),
            symbol=raw.get("symbol"),
            snippet=raw.get("snippet"),
            last_indexed=raw.get("last_modified"),
            source_metadata=raw.get("source_metadata"),
            semantic_source=raw.get("semantic_source"),
            semantic_profile_id=raw.get("semantic_profile_id"),
            semantic_collection_name=raw.get("semantic_collection_name"),
        )
    return ClientSearchMatch(
        file=str(getattr(raw, "file", "")),
        line=getattr(raw, "line", None),
        line_end=getattr(raw, "line_end", None),
        symbol=getattr(raw, "symbol", None),
        snippet=getattr(raw, "snippet", None),
        last_indexed=getattr(raw, "last_modified", None),
        source_metadata=getattr(raw, "source_metadata", None),
        semantic_source=getattr(raw, "semantic_source", None),
        semantic_profile_id=getattr(raw, "semantic_profile_id", None),
        semantic_collection_name=getattr(raw, "semantic_collection_name", None),
    )


def execute_search_service(
    *,
    dispatcher: DispatcherProtocol,
    repo_resolver: RepoResolver | None,
    options: ClientSearchOptions,
    workspace_root: Path | None = None,
    ctx: RepoContext | None = None,
) -> ClientSearchResult:
    readiness = (
        _resolve_readiness(repo_resolver, options.repository, workspace_root=workspace_root)
        if repo_resolver is not None
        else None
    )
    if readiness is not None and not readiness.ready:
        unavailable = _index_unavailable(readiness)
        return _unavailable_search(options, unavailable)

    if ctx is None and repo_resolver is not None:
        ctx = _resolve_ctx(repo_resolver, options.repository, workspace_root=workspace_root)
    unavailable = _generation_unavailable(repo_resolver, ctx)
    if unavailable is not None:
        return _unavailable_search(options, unavailable)
    semantic_readiness = None
    if options.semantic and ctx is not None:
        semantic_readiness = ReadinessClassifier.classify_semantic_registered(
            ctx.registry_entry,
            ctx.sqlite_store,
        )
        if not semantic_readiness.ready:
            return ClientSearchResult(
                query=options.query,
                message="Semantic search requested, but enriched semantic vectors are not ready.",
                code="semantic_not_ready",
                semantic_requested=True,
                semantic_source="semantic",
                semantic_profile_id=semantic_readiness.profile_id,
                semantic_collection_name=semantic_readiness.collection_name,
                semantic_fallback_status="refused_not_ready",
                semantic_readiness=semantic_readiness.to_dict(),
                readiness=readiness.to_dict() if readiness is not None else None,
                source_type=options.source_type,
                friction_categories=options.friction_categories,
                history_labels=options.history_labels,
                history_repos=options.history_repos,
                include_source_metadata=options.include_source_metadata,
            )

    if ctx is None:
        raise RuntimeError("Repository context could not be resolved")

    try:
        raw_results = list(dispatcher.search(ctx, options.query, **_search_kwargs(options)))
    except Exception:
        unavailable = _generation_unavailable(repo_resolver, ctx)
        if unavailable is not None:
            return _unavailable_search(options, unavailable)
        if options.semantic:
            return ClientSearchResult(
                query=options.query,
                message="Semantic search failed at runtime; lexical fallback was not used.",
                code="semantic_search_failed",
                semantic_requested=True,
                semantic_source="semantic",
                semantic_profile_id=getattr(semantic_readiness, "profile_id", None),
                semantic_collection_name=getattr(semantic_readiness, "collection_name", None),
                semantic_fallback_status="failed_runtime",
                semantic_readiness=(
                    semantic_readiness.to_dict() if semantic_readiness is not None else None
                ),
                readiness=readiness.to_dict() if readiness is not None else None,
                source_type=options.source_type,
                friction_categories=options.friction_categories,
                history_labels=options.history_labels,
                history_repos=options.history_repos,
                include_source_metadata=options.include_source_metadata,
            )
        raise

    unavailable = _generation_unavailable(repo_resolver, ctx)
    if unavailable is not None:
        return _unavailable_search(options, unavailable)
    return ClientSearchResult(
        query=options.query,
        results=tuple(_search_match_from_raw(item) for item in raw_results),
        message=None if raw_results else "No results found in index",
        readiness=readiness.to_dict() if readiness is not None else None,
        semantic_requested=options.semantic,
        semantic_source="semantic" if options.semantic else None,
        semantic_profile_id=getattr(semantic_readiness, "profile_id", None),
        semantic_collection_name=getattr(semantic_readiness, "collection_name", None),
        semantic_fallback_status="not_attempted" if options.semantic else None,
        semantic_readiness=(
            semantic_readiness.to_dict() if semantic_readiness is not None else None
        ),
        source_type=options.source_type,
        friction_categories=options.friction_categories,
        history_labels=options.history_labels,
        history_repos=options.history_repos,
        include_source_metadata=options.include_source_metadata,
    )


def search_result_for_gateway(result: ClientSearchResult) -> list[dict[str, Any]]:
    return [
        {
            key: value
            for key, value in {
                "file": item.file,
                "line": item.line,
                "line_end": item.line_end,
                "symbol": item.symbol,
                "snippet": item.snippet,
                "last_indexed": item.last_indexed,
                "source_metadata": item.source_metadata,
            }.items()
            if value is not None
        }
        for item in result.results
    ]


class IndexItClient:
    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        registry_path: str | Path | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self.registry_path = Path(registry_path).resolve() if registry_path else None
        self._repo_resolver: RepoResolver | None = None
        self._dispatcher: DispatcherProtocol | None = None
        self._owned_store_registry = None

    def __enter__(self) -> IndexItClient:
        self._ensure_services()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._owned_store_registry is not None:
            shutdown = getattr(self._dispatcher, "shutdown", None)
            try:
                if shutdown is not None:
                    shutdown()
            finally:
                self._owned_store_registry.shutdown()
            self._owned_store_registry = None
        self._repo_resolver = None
        self._dispatcher = None

    def _ensure_services(self) -> None:
        if self._dispatcher is not None and self._repo_resolver is not None:
            return
        self._owned_store_registry, self._repo_resolver, self._dispatcher, _, _ = (
            initialize_stateless_services(registry_path=self.registry_path)
        )

    @property
    def dispatcher(self) -> DispatcherProtocol:
        self._ensure_services()
        assert self._dispatcher is not None
        return self._dispatcher

    @property
    def repo_resolver(self) -> RepoResolver:
        self._ensure_services()
        assert self._repo_resolver is not None
        return self._repo_resolver

    def search_code(self, options: ClientSearchOptions) -> ClientSearchResult:
        return execute_search_service(
            dispatcher=self.dispatcher,
            repo_resolver=self.repo_resolver,
            options=options,
            workspace_root=self.workspace_root,
        )

    def symbol_lookup(
        self,
        symbol: str,
        *,
        repository: str | Path | None = None,
    ) -> ClientSymbolResult:
        readiness = _resolve_readiness(
            self.repo_resolver,
            str(repository) if repository is not None else None,
            workspace_root=self.workspace_root,
        )
        if readiness is not None and not readiness.ready:
            return ClientSymbolResult(
                symbol=symbol,
                found=False,
                message="Indexed search is available only when repository readiness is ready.",
                readiness=readiness.to_dict(),
                index_unavailable=_index_unavailable(readiness),
            )
        ctx = _resolve_ctx(
            self.repo_resolver,
            str(repository) if repository is not None else None,
            workspace_root=self.workspace_root,
        )
        unavailable = _generation_unavailable(self.repo_resolver, ctx)
        if unavailable is not None:
            return ClientSymbolResult(
                symbol=symbol,
                found=False,
                index_unavailable=unavailable,
                message=unavailable.message,
            )
        if ctx is None:
            return ClientSymbolResult(
                symbol=symbol, found=False, message="Repository context could not be resolved"
            )
        try:
            result = self.dispatcher.lookup(ctx, symbol)
        except Exception:
            unavailable = _generation_unavailable(self.repo_resolver, ctx)
            if unavailable is None:
                raise
            return ClientSymbolResult(
                symbol=symbol,
                found=False,
                index_unavailable=unavailable,
                message=unavailable.message,
            )
        unavailable = _generation_unavailable(self.repo_resolver, ctx)
        if unavailable is not None:
            return ClientSymbolResult(
                symbol=symbol,
                found=False,
                index_unavailable=unavailable,
                message=unavailable.message,
            )
        if not result:
            return ClientSymbolResult(
                symbol=symbol,
                found=False,
                message=f"Symbol '{symbol}' not found in index",
                readiness=readiness.to_dict() if readiness is not None else None,
            )
        if isinstance(result, dict):
            return ClientSymbolResult(
                symbol=result.get("symbol", symbol),
                found=True,
                kind=result.get("kind"),
                language=result.get("language"),
                signature=result.get("signature"),
                doc=result.get("doc"),
                defined_in=result.get("defined_in"),
                line=result.get("line") or result.get("start_line"),
                span=result.get("span"),
                readiness=readiness.to_dict() if readiness is not None else None,
            )
        return ClientSymbolResult(
            symbol=getattr(result, "symbol", symbol),
            found=True,
            kind=getattr(result, "kind", None),
            language=getattr(result, "language", None),
            signature=getattr(result, "signature", None),
            doc=getattr(result, "doc", None),
            defined_in=getattr(result, "defined_in", None),
            line=getattr(result, "line", getattr(result, "start_line", None)),
            span=getattr(result, "span", None),
            readiness=readiness.to_dict() if readiness is not None else None,
        )

    def reindex(
        self,
        *,
        path: str | Path | None = None,
        repository: str | Path | None = None,
    ) -> ClientReindexResult:
        scope = str(repository) if repository is not None else (str(path) if path else None)
        readiness = _resolve_readiness(
            self.repo_resolver, scope, workspace_root=self.workspace_root
        )
        if readiness is not None and not readiness.ready:
            return ClientReindexResult(
                path=str(path) if path is not None else scope,
                mode=None,
                mutation_performed=False,
                message="reindex is available only when repository readiness is ready.",
                index_unavailable=_index_unavailable(readiness),
            )
        ctx = _resolve_ctx(self.repo_resolver, scope, workspace_root=self.workspace_root)
        if ctx is None:
            return ClientReindexResult(
                path=str(path) if path is not None else scope,
                mode=None,
                mutation_performed=False,
                message="Repository context could not be resolved",
            )
        target_path = Path(path).expanduser() if path is not None else ctx.workspace_root
        if target_path.is_file():
            run_repository_mutation(
                self.repo_resolver,
                ctx,
                lambda current: self.dispatcher.index_file(current, target_path),
            )
            return ClientReindexResult(
                path=str(target_path),
                mode="file",
                mutation_performed=True,
                indexed_files=1,
                message=f"Reindexed file: {target_path}",
            )

        def index_directory(current):
            stats = self.dispatcher.index_directory(current, target_path, recursive=True)
            stats["lexical_rows"] = current.sqlite_store.rebuild_fts_code()
            return stats

        stats = run_repository_mutation(self.repo_resolver, ctx, index_directory)
        lexical_rows = stats["lexical_rows"]
        return ClientReindexResult(
            path=str(target_path),
            mode="merge",
            mutation_performed=True,
            indexed_files=stats.get("indexed_files"),
            ignored_files=stats.get("ignored_files"),
            failed_files=stats.get("failed_files"),
            total_files=stats.get("total_files"),
            by_language=stats.get("by_language"),
            lexical_rows=lexical_rows,
        )

    def get_status(
        self,
        *,
        repository: str | Path | None = None,
    ) -> ClientStatusResult:
        readiness = _resolve_readiness(
            self.repo_resolver,
            str(repository) if repository is not None else None,
            workspace_root=self.workspace_root,
        )
        if readiness is not None and not readiness.ready:
            return ClientStatusResult(
                status="index_unavailable",
                dispatcher_type=self.dispatcher.__class__.__name__,
                statistics={},
                health={},
                readiness=readiness.to_dict(),
                index_unavailable=_index_unavailable(readiness),
            )
        ctx = _resolve_ctx(
            self.repo_resolver,
            str(repository) if repository is not None else None,
            workspace_root=self.workspace_root,
        )
        if ctx is None:
            return ClientStatusResult(
                status="unknown",
                dispatcher_type=self.dispatcher.__class__.__name__,
                statistics={},
                health={},
                readiness=readiness.to_dict() if readiness is not None else None,
            )
        stats = self.dispatcher.get_statistics(ctx)
        health = self.dispatcher.health_check(ctx)
        return ClientStatusResult(
            status=health.get("status", "operational"),
            dispatcher_type=self.dispatcher.__class__.__name__,
            statistics=stats,
            health=health,
            readiness=readiness.to_dict() if readiness is not None else None,
        )


def open_client(
    *,
    workspace_root: str | Path | None = None,
    registry_path: str | Path | None = None,
) -> IndexItClient:
    return IndexItClient(workspace_root=workspace_root, registry_path=registry_path)
