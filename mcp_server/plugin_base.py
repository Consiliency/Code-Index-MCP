import inspect
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

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


def _construction_parameters(target: Any) -> list[inspect.Parameter]:
    """Parameters ``target(...)`` binds, without the leading ``self``/``cls``.

    For a class this is its own ``__init__`` (not a metaclass ``__call__``), or
    a custom ``__new__`` when ``__init__`` is ``object.__init__``. For any
    other callable (a factory function or entry point) it is the callable's
    own signature.
    """
    if isinstance(target, type):
        init = getattr(target, "__init__", None)
        if init is not None and init is not object.__init__:
            method = init
        elif getattr(target, "__new__", object.__new__) is not object.__new__:
            method = target.__new__
        else:
            return []
        try:
            parameters = list(inspect.signature(method).parameters.values())
        except (TypeError, ValueError):
            return []
        if parameters and parameters[0].kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            parameters = parameters[1:]  # self / cls
        return parameters
    try:
        return list(inspect.signature(target).parameters.values())
    except (TypeError, ValueError):
        return []


def construct_plugin(plugin_cls: Any, *, sqlite_store: Any = None, enable_semantic: bool = False):
    """Construct a plugin once, passing only arguments its constructor takes.

    ``plugin_cls`` may be a class (its ``__init__``, or a custom ``__new__`` when
    ``__init__`` is inherited from ``object``, is inspected) or a factory callable
    such as a discovered entry point (its own signature is inspected).

    ``enable_semantic`` is passed only to a constructor that names it explicitly;
    ``**kwargs`` alone does not prove the option is accepted. Positional-only
    parameters are passed positionally, in order. Plugin-level semantic
    search is deprecated and off unless a caller opts in (#121). The constructor
    runs exactly once, so a ``TypeError`` raised inside it propagates instead of
    triggering a retry with different arguments.
    """
    values = {"sqlite_store": sqlite_store, "enable_semantic": bool(enable_semantic)}
    parameters = _construction_parameters(plugin_cls)

    # Positional-only parameters can only be passed positionally, in order. Fill
    # known names with their values and unknown ones with their defaults, then
    # drop the trailing defaults; an unknown required one cannot be supplied.
    positional: list = []
    supplied = 0
    for parameter in parameters:
        if parameter.kind is not inspect.Parameter.POSITIONAL_ONLY:
            break
        if parameter.name in values:
            positional.append(values[parameter.name])
            supplied = len(positional)
        elif parameter.default is not inspect.Parameter.empty:
            positional.append(parameter.default)
        else:
            raise TypeError(
                f"{getattr(plugin_cls, '__qualname__', plugin_cls)!r} requires positional-only argument "
                f"{parameter.name!r}, which construct_plugin cannot supply"
            )
    positional = positional[:supplied]
    passed = {p.name for p in parameters[:supplied]}

    named = {p.name for p in parameters if p.kind in _NAMED_PARAMETER_KINDS}
    var_keyword = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters)
    kwargs: dict = {}
    if "sqlite_store" not in passed and ("sqlite_store" in named or var_keyword):
        kwargs["sqlite_store"] = sqlite_store
    if "enable_semantic" not in passed and "enable_semantic" in named:
        kwargs["enable_semantic"] = values["enable_semantic"]
    return plugin_cls(*positional, **kwargs)
