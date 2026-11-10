import inspect
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from typing_extensions import NotRequired, TypedDict

if TYPE_CHECKING:
    from mcp_server.core.repo_context import RepoContext


class IndexShard(TypedDict):
    file: str
    symbols: list[dict]
    language: str
    chunks: NotRequired[list[dict]]
    metadata: NotRequired[dict]


class Reference:
    """A reference to a symbol occurrence in a file."""

    __slots__ = ("file", "start_line", "end_line")

    def __init__(
        self,
        file: str,
        start_line: int = 0,
        end_line: int = 0,
        *,
        line: int | None = None,
    ) -> None:
        object.__setattr__(self, "file", file)
        if line is not None:
            object.__setattr__(self, "start_line", line)
            object.__setattr__(self, "end_line", line)
        else:
            object.__setattr__(self, "start_line", start_line)
            object.__setattr__(self, "end_line", end_line)

    def __setattr__(self, name, value):
        raise AttributeError("Reference is immutable")

    def __eq__(self, other):
        if not isinstance(other, Reference):
            return NotImplemented
        return (
            self.file == other.file
            and self.start_line == other.start_line
            and self.end_line == other.end_line
        )

    def __hash__(self):
        return hash((self.file, self.start_line, self.end_line))

    def __repr__(self):
        return (
            f"Reference(file={self.file!r}, start_line={self.start_line}, end_line={self.end_line})"
        )


class SymbolDef(TypedDict):
    symbol: str
    kind: str
    language: str
    signature: str
    doc: str | None
    defined_in: str
    start_line: int
    end_line: int
    span: tuple[int, int]


class SearchResult(TypedDict):
    file: str
    start_line: int
    end_line: int
    snippet: str


class SearchOpts(TypedDict, total=False):
    semantic: bool
    limit: int
    source_type: str
    friction_categories: list[str]
    history_labels: list[str]
    history_repos: list[str]
    include_source_metadata: bool


class IPlugin(ABC):
    lang: str

    @property
    def language(self) -> str:
        """Return the language identifier."""
        return self.lang

    def bind(self, ctx: "RepoContext") -> None:  # type: ignore[name-defined]
        """P3 IF-0-P3-3: attach a RepoContext to this plugin instance.

        Default no-op. Plugins that need per-repo state (store, cache, etc.)
        override this; ``PluginFactory.create_plugin`` calls it post-construction
        iff the caller passed a context. Kept as a method rather than a
        constructor change so legacy fixtures that build plugins with no args
        keep working.
        """
        return None

    @abstractmethod
    def supports(self, path: str | Path) -> bool: ...

    @abstractmethod
    def indexFile(self, path: str | Path, content: str) -> IndexShard: ...

    @abstractmethod
    def getDefinition(self, symbol: str) -> SymbolDef | None: ...

    @abstractmethod
    def findReferences(self, symbol: str) -> Iterable[Reference]: ...

    @abstractmethod
    def search(self, query: str, opts: SearchOpts | None = None) -> Iterable[SearchResult]: ...


_NAMED_PARAMETER_KINDS = (
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.KEYWORD_ONLY,
)


def _plugin_init_parameters(plugin_cls: type) -> Mapping[str, inspect.Parameter]:
    """Parameters of the class's own ``__init__`` (not a metaclass or ``__new__``)."""
    init = getattr(plugin_cls, "__init__", None)
    if init is None or init is object.__init__:
        return {}
    try:
        return inspect.signature(init).parameters
    except (TypeError, ValueError):
        return {}


def construct_plugin(plugin_cls: type, *, sqlite_store: Any = None, enable_semantic: bool = False):
    """Construct a plugin class once, passing only arguments its ``__init__`` takes.

    ``enable_semantic`` is passed only to an ``__init__`` that names it explicitly;
    ``**kwargs`` alone does not prove the option is accepted. Plugin-level semantic
    search is deprecated and off unless a caller opts in (#121). The constructor
    runs exactly once, so a ``TypeError`` raised inside it propagates instead of
    triggering a retry with different arguments.
    """
    parameters = _plugin_init_parameters(plugin_cls)
    named = {name for name, p in parameters.items() if p.kind in _NAMED_PARAMETER_KINDS}
    var_keyword = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
    kwargs: dict = {}
    if "sqlite_store" in named or var_keyword:
        kwargs["sqlite_store"] = sqlite_store
    if "enable_semantic" in named:
        kwargs["enable_semantic"] = bool(enable_semantic)
    return plugin_cls(**kwargs)
