"""Tree-sitter grammar cache management for tree-sitter-language-pack 1.20+.

Language-pack 1.20 no longer bundles parser libraries in its wheel. It downloads
them on first use into a versioned cache
(``<root>/tree-sitter-language-pack/v<version>/libs``). A cold cache without
network access raises ``DownloadError`` (surfaced by treesitter-chunker as
``LanguageNotFoundError``), so the service, CI and container/sandbox images
prefetch every grammar the registry needs ahead of time
(Consiliency/Code-Index-MCP#115).

The cache root resolves the same way the pack resolves it:

1. ``TREE_SITTER_LANGUAGE_PACK_CACHE_DIR`` when set,
2. otherwise ``$XDG_CACHE_HOME``,
3. otherwise ``$HOME/.cache``,
4. otherwise ``<passwd home of the running uid>/.cache``.

The pack refuses a cache directory that is not owned by the running uid or is
group- or other-writable, so a cache is per user. A warm cache loads without
write access. For images and other shared installs, export the prefetched
parser libraries to a shared directory and point
``TREE_SITTER_LANGUAGE_PACK_LIBS_DIR`` at it: the pack loads that directory
first, without the ownership check and without touching ``$HOME``.
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Environment variable the language pack reads for its cache root.
CACHE_DIR_ENV = "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"

#: Environment variable for a shared, read-only parser-library directory.
LIBS_DIR_ENV = "TREE_SITTER_LANGUAGE_PACK_LIBS_DIR"

#: Environment variable the pack reads (lazily) for its download manifest URL.
MANIFEST_URL_ENV = "TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL"

#: Manifest URL for network-denied processes. The pack's HTTP client rejects
#: the unknown scheme before it creates a socket, so even a download path the
#: Python guard cannot see fails without touching the network.
OFFLINE_MANIFEST_URL = "sandbox-offline://network-denied"

#: Pack entry points that download, write the cache or reconfigure it. A
#: network-denied process never calls them.
_OFFLINE_REFUSED = (
    "clean_cache",
    "configure",
    "download",
    "download_all",
    "download_group",
    "init",
    "manifest_groups",
    "manifest_languages",
    "prefetch",
)

#: Pack entry points that load a grammar by name; allowed only for grammars
#: already extracted in the cache or present in the shared libs dir.
_OFFLINE_GUARDED = ("get_language", "get_parser")

#: Pack grammar names whose library file uses a different C symbol name.
_LIBRARY_SYMBOL_NAMES = {"csharp": "c_sharp"}

#: Smallest snippet every grammar parses; we only need a tree, not valid code.
_PROBE_SOURCE = b"x\n"


def _passwd_home() -> Optional[Path]:
    try:
        import pwd

        home = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, AttributeError, OSError):
        return None
    return Path(home) if home else None


def _absolute(value: str, cwd: Optional[Path]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (cwd if cwd is not None else Path.cwd()) / path
    return path


def resolve_cache_root(
    environ: Optional[os._Environ | dict] = None, *, cwd: Optional[Path] = None
) -> Optional[Path]:
    """Return the absolute cache root the language pack uses, without importing it.

    Mirrors the pack: the override, then ``XDG_CACHE_HOME``, then
    ``$HOME/.cache``, then the running uid's passwd home. A relative value is
    resolved against ``cwd`` (default: the current directory), so a caller
    that is about to ``chdir`` keeps pointing at the same cache. Returns
    ``None`` only when no root can be derived at all.
    """
    env = os.environ if environ is None else environ
    override = (env.get(CACHE_DIR_ENV) or "").strip()
    if override:
        return _absolute(override, cwd)
    xdg = (env.get("XDG_CACHE_HOME") or "").strip()
    if xdg:
        return _absolute(xdg, cwd)
    home = (env.get("HOME") or "").strip()
    if home:
        return _absolute(home, cwd) / ".cache"
    passwd_home = _passwd_home()
    if passwd_home is not None:
        return passwd_home / ".cache"
    return None


def resolve_libs_dir(
    environ: Optional[os._Environ | dict] = None, *, cwd: Optional[Path] = None
) -> Optional[Path]:
    """Return the absolute shared parser-library directory, when configured."""
    env = os.environ if environ is None else environ
    value = (env.get(LIBS_DIR_ENV) or "").strip()
    return _absolute(value, cwd) if value else None


def _pack():
    import tree_sitter_language_pack

    return tree_sitter_language_pack


def configure_cache_dir(cache_dir: Optional[str | Path]) -> Optional[Path]:
    """Point the language pack (and child processes) at ``cache_dir``.

    Sets ``TREE_SITTER_LANGUAGE_PACK_CACHE_DIR`` for this process and its
    children and applies it to the already-imported pack. ``None`` keeps the
    current resolution. Returns the effective cache root.
    """
    if cache_dir is not None:
        root = Path(cache_dir).expanduser().resolve()
        os.environ[CACHE_DIR_ENV] = str(root)
        pack = _pack()
        pack.configure(pack.PackConfig(cache_dir=str(root)))
        return root
    return resolve_cache_root()


def effective_cache_dir() -> str:
    """Return the pack's versioned parser-library directory."""
    return str(_pack().cache_dir())


def libs_dir() -> Optional[Path]:
    """Return the shared parser-library directory, when configured."""
    return resolve_libs_dir()


def _library_names(directory: Path) -> set:
    names = set()
    if directory.is_dir():
        for entry in directory.iterdir():
            stem = entry.name.split(".", 1)[0]
            if stem.startswith("libtree_sitter_"):
                names.add(stem[len("libtree_sitter_") :])
            elif stem.startswith("tree_sitter_"):
                names.add(stem[len("tree_sitter_") :])
    return names


def get_required_grammars() -> List[str]:
    """Return every pack grammar name the language registry needs."""
    from mcp_server.plugins.language_registry import get_required_grammars as _required

    return _required()


def cached_grammars() -> List[str]:
    """Return grammar names loadable without a download (cache or libs dir)."""
    names = set(_pack().downloaded_languages())
    shared = libs_dir()
    if shared is not None:
        symbols = _library_names(shared)
        names.update(name for name in get_required_grammars() if _symbol(name) in symbols)
        names.update(symbols)
    return sorted(names)


def _symbol(name: str) -> str:
    return _LIBRARY_SYMBOL_NAMES.get(name, name)


class UnsafeLibsDirError(RuntimeError):
    """The shared parser-library directory is not safe to load or write."""


def _is_library_name(name: str) -> bool:
    return name.startswith(("libtree_sitter_", "tree_sitter_"))


def _owner_ok(st: os.stat_result) -> bool:
    return st.st_uid in (0, os.getuid())


def libs_dir_problems(directory: Optional[str | Path] = None) -> List[str]:
    """Return reasons ``directory`` (default: the configured libs dir) is unsafe.

    The pack loads every library in ``TREE_SITTER_LANGUAGE_PACK_LIBS_DIR``
    first, with no ownership check, into the server and every sandbox worker.
    The directory and each library must therefore be owned by root or the
    running user and must not be group- or other-writable, and neither may be
    a symlink. Returns ``[]`` when no libs dir is configured or it is safe.
    """
    import stat

    path = Path(directory).expanduser() if directory is not None else libs_dir()
    if path is None:
        return []
    problems: List[str] = []
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return [f"{path}: does not exist"]
    except OSError as exc:
        return [f"{path}: cannot stat ({exc})"]
    if stat.S_ISLNK(st.st_mode):
        return [f"{path}: is a symlink"]
    if not stat.S_ISDIR(st.st_mode):
        return [f"{path}: is not a directory"]
    if not _owner_ok(st):
        problems.append(f"{path}: owned by uid {st.st_uid}, not root or uid {os.getuid()}")
    if st.st_mode & 0o022:
        problems.append(f"{path}: group- or other-writable (mode {st.st_mode & 0o7777:o})")
    try:
        entries = sorted(path.iterdir())
    except OSError as exc:
        return problems + [f"{path}: cannot list ({exc})"]
    for entry in entries:
        if not _is_library_name(entry.name):
            continue
        est = os.lstat(entry)
        if stat.S_ISLNK(est.st_mode):
            problems.append(f"{entry}: is a symlink")
            continue
        if not _owner_ok(est):
            problems.append(f"{entry}: owned by uid {est.st_uid}, not root or uid {os.getuid()}")
        if est.st_mode & 0o022:
            problems.append(f"{entry}: group- or other-writable (mode {est.st_mode & 0o7777:o})")
    return problems


def _prepare_export_dir(destination: str | Path) -> Path:
    """Validate (and create, if missing) the export directory.

    Refuses symlinked path components, an existing directory that is not
    owned by root or the running user or is group-/other-writable (``/tmp``),
    and an existing directory that holds anything but parser libraries
    (``/``, ``/usr/lib``). Only directories created here get mode 0755.
    """
    import stat

    target = Path(os.path.abspath(Path(destination).expanduser()))
    if target == Path(target.anchor):
        raise UnsafeLibsDirError(f"refusing to export parser libraries into {target}")
    current = Path(target.anchor)
    created: List[Path] = []
    for part in target.parts[1:]:
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, 0o755)
            os.chmod(current, 0o755)  # independent of the umask
            created.append(current)
            continue
        if stat.S_ISLNK(st.st_mode):
            raise UnsafeLibsDirError(
                f"refusing to export through symlinked path component {current}; "
                "pass the resolved path"
            )
        if not stat.S_ISDIR(st.st_mode):
            raise UnsafeLibsDirError(f"{current} exists and is not a directory")
    if target not in created:
        problems = [
            problem
            for problem in libs_dir_problems(target)
            if not problem.startswith(f"{target}/")  # existing libs are replaced
        ]
        if problems:
            raise UnsafeLibsDirError("; ".join(problems))
        foreign = [
            entry.name
            for entry in target.iterdir()
            if not _is_library_name(entry.name) and not entry.name.startswith(".tree_sitter_tmp")
        ]
        if foreign:
            raise UnsafeLibsDirError(
                f"{target} already holds files other than parser libraries "
                f"({', '.join(sorted(foreign)[:5])}); export into a dedicated directory"
            )
    return target


def export_libs(destination: str | Path) -> List[str]:
    """Copy the cached parser libraries into a shared, world-readable directory.

    Point ``TREE_SITTER_LANGUAGE_PACK_LIBS_DIR`` at ``destination`` so any user
    loads them offline without a per-user cache. Each library is written to a
    temporary file in ``destination`` and atomically renamed into place, so a
    process that has the previous library mapped keeps a valid mapping.
    Returns the copied file names.
    """
    import shutil
    import stat
    import tempfile

    source = Path(effective_cache_dir())
    target = _prepare_export_dir(destination)
    copied = []
    for entry in sorted(source.iterdir()):
        if not entry.is_file() or entry.is_symlink():
            continue
        final = target / entry.name
        try:
            existing = os.lstat(final)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise UnsafeLibsDirError(f"{final} exists and is not a regular file")
        fd, tmp_name = tempfile.mkstemp(prefix=".tree_sitter_tmp.", dir=target)
        try:
            with os.fdopen(fd, "wb") as out, open(entry, "rb") as src:
                shutil.copyfileobj(src, out)
                out.flush()
                os.fchmod(out.fileno(), 0o644)
                os.fsync(out.fileno())
            os.replace(tmp_name, final)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
        copied.append(entry.name)
    return copied


def missing_grammars(languages: Optional[Iterable[str]] = None) -> List[str]:
    """Return requested grammars that are not in the local cache yet."""
    wanted = list(languages) if languages is not None else get_required_grammars()
    cached = set(cached_grammars())
    return [name for name in wanted if name not in cached]


#: Download attempts per prefetch, including the first (Consiliency/Code-Index-MCP#131).
PREFETCH_ATTEMPTS = 5
#: Backoff before the second attempt; doubles per attempt, capped at the maximum.
PREFETCH_BASE_DELAY_SECONDS = 4.0
PREFETCH_MAX_DELAY_SECONDS = 60.0
#: Wall-clock budget for one prefetch: no retry starts that would sleep past it.
#: It bounds the backoff, not a single attempt, which the pack may let hang.
PREFETCH_DEADLINE_SECONDS = 120.0

_HTTP_STATUS = re.compile(r"http status:\s*(\d{3})", re.IGNORECASE)
_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.IGNORECASE)
#: Transport failures worth retrying: connection resets, timeouts, refused
#: connections and DNS lookup failures (#132 review N4).
_TRANSIENT_TRANSPORT = re.compile(
    r"connection reset|reset by peer|connection aborted|broken pipe|"
    r"unexpected end of file|timed out|timeout|connection refused|"
    r"dns error|failed to lookup|temporary failure in name resolution|"
    r"name or service not known",
    re.IGNORECASE,
)
#: HTTP statuses worth retrying besides 5xx.
_TRANSIENT_HTTP_STATUSES = frozenset({408, 429})


class GrammarPrefetchError(RuntimeError):
    """Grammar download failed; the message names only the URL and the cause.

    The pack's own error text carries the full request URL, query string
    included, so it is never logged or re-raised verbatim.
    """

    def __init__(self, message: str, *, attempts: int, failure: "DownloadFailure") -> None:
        super().__init__(message)
        self.attempts = attempts
        self.failure = failure


@dataclass(frozen=True)
class DownloadFailure:
    """A classified grammar download failure, safe to log."""

    transient: bool
    reason: str
    url: Optional[str] = None
    status: Optional[int] = None

    def describe(self) -> str:
        where = f" for {self.url}" if self.url else ""
        status = f"HTTP {self.status}" if self.status is not None else self.reason
        return f"{status}{where}"


def _safe_url(message: str) -> Optional[str]:
    """Return the first URL in ``message`` without credentials, query or fragment."""
    match = _URL.search(message)
    if match is None:
        return None
    raw = match.group(0).rstrip(".,;:)")
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return None
    return f"{parts.scheme}://{host}{port}{parts.path}"


def classify_download_error(exc: BaseException) -> DownloadFailure:
    """Decide whether a language-pack download failure is worth retrying.

    Retried: HTTP 5xx, 408 and 429, connection resets, timeouts, refused
    connections and DNS lookup failures. Never retried: other HTTP statuses
    (4xx such as 404), checksum mismatches and anything unrecognized. The pack reports failures as ``RuntimeError("Download error:
    Failed to fetch manifest from <url>: http status: 504")`` and similar.
    """
    message = str(exc)
    url = _safe_url(message)
    if type(exc).__name__ == "ChecksumMismatchError" or "checksum" in message.lower():
        return DownloadFailure(False, "checksum mismatch", url)
    status_match = _HTTP_STATUS.search(message)
    if status_match is not None:
        status = int(status_match.group(1))
        transient = status in _TRANSIENT_HTTP_STATUSES or 500 <= status <= 599
        return DownloadFailure(transient, "http status", url, status)
    transport = _TRANSIENT_TRANSPORT.search(message)
    if transport is not None:
        return DownloadFailure(True, transport.group(0).lower(), url)
    return DownloadFailure(False, type(exc).__name__, url)


def _backoff_delay(attempt: int, jitter: Callable[[], float]) -> float:
    """Equal-jitter exponential backoff before attempt ``attempt + 1``."""
    ceiling = min(PREFETCH_MAX_DELAY_SECONDS, PREFETCH_BASE_DELAY_SECONDS * 2.0 ** (attempt - 1))
    return ceiling / 2 + jitter() * ceiling / 2


def prefetch(
    languages: Optional[Sequence[str]] = None,
    *,
    downloader: Optional[Callable[[List[str]], object]] = None,
    attempts: int = PREFETCH_ATTEMPTS,
    deadline_seconds: float = PREFETCH_DEADLINE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    jitter: Callable[[], float] = random.random,
) -> List[str]:
    """Download (if needed) and load every requested grammar.

    Defaults to every grammar the language registry advertises. The manifest
    and parser bundle come from GitHub release assets, so transient failures
    (HTTP 5xx, 408 and 429, connection resets, timeouts, refused connections
    and DNS lookup failures) are retried with bounded exponential backoff and
    jitter: at most ``attempts`` tries, and no retry starts that would sleep
    past ``deadline_seconds`` (Consiliency/Code-Index-MCP#131). The budget
    bounds the backoff, not one attempt, which the pack may let hang. Other failures,
    such as HTTP 404 or a checksum mismatch, fail on the first attempt.
    Retries log only the URL (no query string) and the status.

    Raises :class:`GrammarPrefetchError` when the grammars cannot be fetched.
    Returns the grammar names that were prefetched.
    """
    names = sorted(set(languages if languages is not None else get_required_grammars()))
    if not names:
        return names
    download = downloader if downloader is not None else _pack().prefetch
    started = clock()
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        try:
            download(names)
            return names
        except Exception as exc:  # noqa: BLE001 - classified below
            failure = classify_download_error(exc)
        if not failure.transient:
            raise GrammarPrefetchError(
                f"grammar download failed ({failure.describe()}); not retried",
                attempts=attempt,
                failure=failure,
            ) from None
        if attempt == attempts:
            raise GrammarPrefetchError(
                f"grammar download failed after {attempt} attempts ({failure.describe()})",
                attempts=attempt,
                failure=failure,
            ) from None
        delay = _backoff_delay(attempt, jitter)
        remaining = deadline_seconds - (clock() - started)
        if delay > remaining:
            raise GrammarPrefetchError(
                f"grammar download failed after {attempt} attempts within "
                f"{deadline_seconds:.0f}s ({failure.describe()})",
                attempts=attempt,
                failure=failure,
            ) from None
        logger.warning(
            "Grammar download attempt %d/%d failed (%s); retrying in %.1fs",
            attempt,
            attempts,
            failure.describe(),
            delay,
        )
        sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


GRAMMAR_UNAVAILABLE = "grammar_unavailable"
LIBS_DIR_UNSAFE = "libs_dir_unsafe"

_offline_grammars: Optional[frozenset] = None


class GrammarUnavailableError(RuntimeError):
    """A registry language's tree-sitter grammar cannot be loaded offline.

    Raised (or reported) instead of indexing the file without chunks, so a
    reindex never publishes a silently chunkless generation for that language
    (#116 review S3). The message starts with ``grammar_unavailable:``.
    """

    code = GRAMMAR_UNAVAILABLE

    def __init__(self, language: str, grammar: str) -> None:
        self.language = language
        self.grammar = grammar
        super().__init__(
            f"{GRAMMAR_UNAVAILABLE}: tree-sitter grammar {grammar!r} for language "
            f"{language!r} is not in the local grammar cache or libs dir. Run "
            "`mcp-index grammars prefetch` (or set TREE_SITTER_LANGUAGE_PACK_LIBS_DIR), "
            "then reindex."
        )


def _offline_grammar_set(refresh: bool = False) -> frozenset:
    global _offline_grammars
    if _offline_grammars is None or refresh:
        try:
            _offline_grammars = frozenset(cached_grammars())
        except Exception:  # noqa: BLE001 - no usable pack means nothing loads offline
            _offline_grammars = frozenset()
    return _offline_grammars


def grammars_for_language(language: Optional[str]) -> Tuple[str, ...]:
    """Return every pack grammar a registry language's plugin needs to load.

    Empty for languages that need none (``plaintext``, document-plugin
    languages such as ``markdown``) and for unknown codes. ``typescript``
    needs ``typescript``, ``tsx`` and ``javascript``: its plugin loads all
    three (Consiliency/Code-Index-MCP#117).
    """
    if not language:
        return ()
    from mcp_server.plugins.language_registry import (
        LANGUAGE_CONFIGS,
        PLUGIN_EXTRA_GRAMMARS_BY_LANGUAGE,
        PLUGIN_GRAMMARLESS_LANGUAGES,
        get_grammar_name,
    )

    if language not in LANGUAGE_CONFIGS or language in PLUGIN_GRAMMARLESS_LANGUAGES:
        return ()
    grammar = get_grammar_name(language)
    if not grammar:
        return ()
    names = [grammar]
    for extra in PLUGIN_EXTRA_GRAMMARS_BY_LANGUAGE.get(language, ()):
        if extra not in names:
            names.append(extra)
    return tuple(names)


def _missing_from(language: Optional[str], available: frozenset) -> List[str]:
    return [
        grammar
        for grammar in grammars_for_language(language)
        if grammar not in available and _symbol(grammar) not in available
    ]


def unavailable_grammar(language: Optional[str]) -> Optional[str]:
    """Return a pack grammar ``language`` needs that cannot load offline.

    ``None`` means the language needs no grammar (plaintext, unknown codes) or
    every grammar it needs (:func:`grammars_for_language`) is in the local
    cache or the shared libs dir. A miss re-reads the cache once, so a
    prefetch that ran after startup is picked up.
    """
    missing: List[str] = []
    for refresh in (False, True):
        missing = _missing_from(language, _offline_grammar_set(refresh=refresh))
        if not missing:
            return None
    return missing[0]


#: Coverage state of a published generation that left out languages whose
#: grammars were unavailable (Consiliency/Code-Index-MCP#117).
GRAMMAR_DEGRADED = "degraded"
#: Coverage state of a generation that indexed every admitted file.
GRAMMAR_COMPLETE = "complete"


@dataclass(frozen=True)
class GrammarAvailability:
    """Which registry languages cannot load their grammars, decided once.

    A staged build takes one snapshot when it starts and defers the listed
    languages for the whole build, so a grammar that arrives mid-build never
    leaves half a language in the generation (Consiliency/Code-Index-MCP#117).
    ``unavailable`` maps each such language to its missing grammars.
    """

    unavailable: Mapping[str, Tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def complete(self) -> bool:
        return not self.unavailable

    def defers(self, language: Optional[str]) -> bool:
        return bool(language) and language in self.unavailable

    def missing(self, language: Optional[str]) -> Tuple[str, ...]:
        return tuple(self.unavailable.get(language or "", ()))


def snapshot_availability(refresh: bool = True) -> GrammarAvailability:
    """Return the registry languages whose grammars cannot load offline now.

    Reads the cache and libs dir once (re-reading first when ``refresh``).
    Only loadability counts: an unsafe libs dir whose grammars all load is
    complete coverage, reported separately as ``libs_dir_unsafe``.
    """
    from mcp_server.plugins.language_registry import LANGUAGE_CONFIGS

    available = _offline_grammar_set(refresh=refresh)
    unavailable = {}
    for language in sorted(LANGUAGE_CONFIGS):
        missing = _missing_from(language, available)
        if missing:
            unavailable[language] = tuple(missing)
    return GrammarAvailability(MappingProxyType(unavailable))


def unavailable_languages(languages: Iterable[str], *, refresh: bool = False) -> dict:
    """Return ``{language: missing grammars}`` for those that cannot load.

    Unlike :func:`unavailable_grammar` this does not re-read the cache on a
    miss unless ``refresh``, so status surfaces can call it on every request.
    """
    available = _offline_grammar_set(refresh=refresh)
    result = {}
    for language in languages:
        missing = _missing_from(language, available)
        if missing:
            result[language] = tuple(missing)
    return result


def grammar_status(repositories_degraded: Optional[Sequence[str]] = None) -> dict:
    """Summarize grammar availability for ``get_status`` (offline, no loading).

    ``ready`` is false only when files in some language fail indexing: a
    missing grammar that only a document plugin's language names (markdown)
    affects nothing. An unsafe libs dir with every grammar available is its
    own state, ``libs_dir_unsafe``: indexing is unaffected, but the operator
    must fix the directory. ``repositories_degraded`` lists the registered
    repositories whose published generation left languages out
    (Consiliency/Code-Index-MCP#117).
    """
    from mcp_server.plugins.language_registry import LANGUAGE_CONFIGS

    try:
        missing = missing_grammars()
        error = None
    except Exception as exc:  # noqa: BLE001 - report, never raise from status
        missing = get_required_grammars()
        error = f"{type(exc).__name__}: {exc}"
    missing_set = frozenset(missing)
    languages = sorted(
        language
        for language in LANGUAGE_CONFIGS
        if any(grammar in missing_set for grammar in grammars_for_language(language))
    )
    libs_problems = libs_dir_problems()
    ready = not languages and error is None
    libs = libs_dir()
    if not ready:
        state = GRAMMAR_UNAVAILABLE
    elif libs_problems:
        state = LIBS_DIR_UNSAFE
    else:
        state = "ready"
    status = {
        "state": state,
        "ready": ready,
        "missing_grammars": sorted(missing_set),
        "languages": languages,
        "libs_dir": str(libs) if libs else None,
        "libs_dir_problems": libs_problems,
        "repositories_degraded": sorted(repositories_degraded or []),
        "remediation": None,
    }
    if error is not None:
        status["error"] = error
    if state == LIBS_DIR_UNSAFE:
        status["remediation"] = (
            "Every grammar loads, but TREE_SITTER_LANGUAGE_PACK_LIBS_DIR is unsafe "
            "(see libs_dir_problems): make it and its libraries owned by root or the "
            "service user and not group- or other-writable."
        )
    elif not ready:
        status["remediation"] = (
            "Run `mcp-index grammars prefetch` as the service user (or export "
            "libraries with --export-libs and set TREE_SITTER_LANGUAGE_PACK_LIBS_DIR "
            "to a directory only root or the service user can write), then reindex. "
            "Files in these languages fail indexing until then."
        )
    return status


@dataclass
class GrammarCheck:
    """Result of loading and parsing each requested grammar from the cache."""

    loaded: List[str] = field(default_factory=list)
    failed: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed


def verify(languages: Optional[Sequence[str]] = None, *, offline: bool = True) -> GrammarCheck:
    """Load and parse every requested grammar.

    With ``offline=True`` (default) a grammar missing from the cache is reported
    as a failure instead of triggering a first-use download.
    """
    from tree_sitter import Parser

    pack = _pack()
    names = sorted(set(languages if languages is not None else get_required_grammars()))
    cached = set(cached_grammars()) if offline else set()
    result = GrammarCheck()
    for name in names:
        if offline and name not in cached:
            result.failed[name] = "not in local grammar cache (run `mcp-index grammars prefetch`)"
            continue
        try:
            parser = Parser(pack.get_language(name))
            tree = parser.parse(_PROBE_SOURCE)
            if tree is None or tree.root_node is None:
                raise RuntimeError("parser returned no tree")
        except Exception as exc:  # noqa: BLE001 - report every grammar failure
            result.failed[name] = f"{type(exc).__name__}: {exc}"
            continue
        result.loaded.append(name)
    return result


class OfflineGrammarGuard:
    """Refuses language-pack downloads and cache writes in a network-denied process.

    Installed by the sandbox worker when its capabilities deny network. A
    grammar loads only when it is already extracted in the cache or present
    in the shared libs dir; anything else raises the pack's ``DownloadError``
    before native code runs, so a cold or rolled-over cache fails closed
    without a connection and without writing the host cache.
    """

    def __init__(self, pack) -> None:
        self._pack = pack
        self._available: Optional[set] = None

    def _loadable(self, name: str) -> bool:
        candidates = {name, _symbol(name)}
        try:
            from mcp_server.plugins.language_registry import get_grammar_name

            grammar = get_grammar_name(name)
        except Exception:  # noqa: BLE001 - registry lookup is best effort
            grammar = None
        if grammar:
            candidates.update({grammar, _symbol(grammar)})
        for refresh in (False, True):
            if self._available is None or refresh:
                self._available = set(cached_grammars())
            if candidates & self._available:
                return True
        return False

    def require(self, name: object) -> None:
        if not isinstance(name, str) or not self._loadable(name):
            raise self._pack.DownloadError(
                f"grammar {name!r} is not in the local grammar cache, and this "
                "process may not download it (network denied). Run "
                "`mcp-index grammars prefetch` on the host."
            )

    def refuse(self, entry_point: str) -> None:
        raise self._pack.DownloadError(
            f"tree_sitter_language_pack.{entry_point} is disabled in a "
            "network-denied process; grammars must be prefetched on the host."
        )


def install_offline_guard() -> OfflineGrammarGuard:
    """Make this process load grammars from the local cache only.

    Points the pack's manifest URL at :data:`OFFLINE_MANIFEST_URL` (no socket
    is ever created for it) and wraps the pack's public entry points so a
    grammar that is not already extracted raises ``DownloadError`` instead of
    downloading or extracting into the cache. Idempotent.
    """
    import functools

    pack = _pack()
    os.environ[MANIFEST_URL_ENV] = OFFLINE_MANIFEST_URL
    os.environ.pop("TREE_SITTER_LANGUAGE_PACK_TLS_ROOTS", None)
    existing = getattr(pack, "_mcp_offline_guard", None)
    if existing is not None:
        return existing
    guard = OfflineGrammarGuard(pack)
    modules = [pack]
    api = getattr(pack, "api", None)
    if api is not None:
        modules.append(api)

    def _guarded(original):
        @functools.wraps(original)
        def wrapper(name, *args, **kwargs):
            guard.require(name)
            return original(name, *args, **kwargs)

        return wrapper

    def _refused(entry_point):
        def wrapper(*_args, **_kwargs):
            guard.refuse(entry_point)

        wrapper.__name__ = entry_point
        return wrapper

    def _guarded_process(original):
        @functools.wraps(original)
        def wrapper(source, config, *args, **kwargs):
            language = getattr(config, "language", None)
            if language is None and isinstance(config, dict):
                language = config.get("language")
            if language is None and isinstance(config, str):
                import json

                try:
                    language = json.loads(config).get("language")
                except (ValueError, AttributeError):
                    language = None
            guard.require(language)
            return original(source, config, *args, **kwargs)

        return wrapper

    for module in modules:
        for entry_point in _OFFLINE_GUARDED:
            original = getattr(module, entry_point, None)
            if original is not None:
                setattr(module, entry_point, _guarded(original))
        for entry_point in _OFFLINE_REFUSED:
            if hasattr(module, entry_point):
                setattr(module, entry_point, _refused(entry_point))
        if hasattr(module, "process"):
            setattr(module, "process", _guarded_process(getattr(module, "process")))
    pack._mcp_offline_guard = guard
    return guard
