"""Typed public models for the local Python client."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from mcp_server.indexing.source_metadata import normalize_friction_category


class SourceType(str, Enum):
    FRICTION = "friction"
    HISTORY = "history"


def _normalize_source_type(value: SourceType | str | None) -> SourceType | None:
    if value is None:
        return None
    if isinstance(value, SourceType):
        return value
    return SourceType(value)


def _normalize_list(values: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(item.strip() for item in values if item and item.strip())
    return tuple(sorted(set(cleaned), key=str.lower))


@dataclass(frozen=True)
class ClientSearchOptions:
    query: str
    repository: str | None = None
    semantic: bool = False
    fuzzy: bool = False
    limit: int = 20
    source_type: SourceType | None = None
    friction_categories: tuple[str, ...] = field(default_factory=tuple)
    history_labels: tuple[str, ...] = field(default_factory=tuple)
    history_repos: tuple[str, ...] = field(default_factory=tuple)
    include_source_metadata: bool = False

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("'query' parameter is required")
        if self.limit < 1:
            raise ValueError("limit must be >= 1")
        object.__setattr__(self, "query", self.query.strip())
        object.__setattr__(self, "source_type", _normalize_source_type(self.source_type))
        object.__setattr__(
            self,
            "friction_categories",
            tuple(normalize_friction_category(item) for item in self.friction_categories if item),
        )
        object.__setattr__(self, "history_labels", _normalize_list(self.history_labels))
        object.__setattr__(self, "history_repos", _normalize_list(self.history_repos))


@dataclass(frozen=True)
class IndexUnavailable:
    readiness: dict[str, Any]
    message: str
    remediation: str | None = None
    code: str = "index_unavailable"
    safe_fallback: str = "native_search"


@dataclass(frozen=True)
class ClientSearchMatch:
    file: str
    line: int | None = None
    line_end: int | None = None
    symbol: str | None = None
    snippet: str | None = None
    last_indexed: Any = None
    source_metadata: dict[str, Any] | None = None
    semantic_source: str | None = None
    semantic_profile_id: str | None = None
    semantic_collection_name: str | None = None


@dataclass(frozen=True)
class ClientSearchResult:
    query: str
    results: tuple[ClientSearchMatch, ...] = field(default_factory=tuple)
    message: str | None = None
    readiness: dict[str, Any] | None = None
    index_unavailable: IndexUnavailable | None = None
    code: str | None = None
    safe_fallback: str | None = None
    remediation: str | None = None
    semantic_requested: bool = False
    semantic_source: str | None = None
    semantic_profile_id: str | None = None
    semantic_collection_name: str | None = None
    semantic_fallback_status: str | None = None
    semantic_readiness: dict[str, Any] | None = None
    source_type: SourceType | None = None
    friction_categories: tuple[str, ...] = field(default_factory=tuple)
    history_labels: tuple[str, ...] = field(default_factory=tuple)
    history_repos: tuple[str, ...] = field(default_factory=tuple)
    include_source_metadata: bool = False
    #: Readiness ``coverage`` when the published generation left languages
    #: out (Consiliency/Code-Index-MCP#117); None while coverage is complete.
    coverage: dict[str, Any] | None = None


@dataclass(frozen=True)
class ClientSymbolResult:
    symbol: str
    found: bool
    kind: str | None = None
    language: str | None = None
    signature: str | None = None
    doc: str | None = None
    defined_in: str | None = None
    line: int | None = None
    span: tuple[int, int] | None = None
    message: str | None = None
    readiness: dict[str, Any] | None = None
    index_unavailable: IndexUnavailable | None = None


@dataclass(frozen=True)
class ClientReindexResult:
    path: str | None
    mode: str | None
    mutation_performed: bool
    indexed_files: int | None = None
    durable_files: int | None = None
    lexical_rows: int | None = None
    ignored_files: int | None = None
    failed_files: int | None = None
    total_files: int | None = None
    by_language: dict[str, Any] | None = None
    message: str | None = None
    index_unavailable: IndexUnavailable | None = None


@dataclass(frozen=True)
class ClientStatusResult:
    status: str
    dispatcher_type: str
    statistics: dict[str, Any]
    health: dict[str, Any]
    readiness: dict[str, Any] | None = None
    index_unavailable: IndexUnavailable | None = None
