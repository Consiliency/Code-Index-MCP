"""Shared repository readiness contracts for query and status surfaces."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from mcp_server.storage.repo_identity import compute_repo_id
from mcp_server.storage.sqlite_store import evaluate_chunk_scheme
from mcp_server.utils.subprocess_env import get_full_env


class RepositoryReadinessState(str, Enum):
    READY = "ready"
    UNREGISTERED_REPOSITORY = "unregistered_repository"
    MISSING_INDEX = "missing_index"
    INDEX_EMPTY = "index_empty"
    STALE_COMMIT = "stale_commit"
    WRONG_BRANCH = "wrong_branch"
    INDEX_BUILDING = "index_building"
    UNSUPPORTED_WORKTREE = "unsupported_worktree"
    AMBIGUOUS_SELECTOR = "ambiguous_selector"
    CORRUPT_SQLITE = "corrupt_sqlite"
    INDEX_LOCKED = "index_locked"
    MISSING_SCHEMA = "missing_schema"
    MISSING_PROVENANCE = "missing_provenance"
    SCHEME_MISMATCH = "scheme_mismatch"
    INDEX_REBUILDING = "index_rebuilding"


class SemanticReadinessState(str, Enum):
    READY = "ready"
    ENRICHMENT_UNAVAILABLE = "enrichment_unavailable"
    SUMMARIES_MISSING = "summaries_missing"
    VECTORS_MISSING = "vectors_missing"
    VECTOR_DIMENSION_MISMATCH = "vector_dimension_mismatch"
    PROFILE_MISMATCH = "profile_mismatch"
    SEMANTIC_STALE = "semantic_stale"


@dataclass(frozen=True)
class RepositoryReadiness:
    state: RepositoryReadinessState
    repository_id: Optional[str] = None
    repository_name: Optional[str] = None
    registered_path: Optional[str] = None
    requested_path: Optional[str] = None
    tracked_branch: Optional[str] = None
    current_branch: Optional[str] = None
    current_commit: Optional[str] = None
    last_indexed_commit: Optional[str] = None
    index_path: Optional[str] = None
    remediation: Optional[str] = None

    @property
    def ready(self) -> bool:
        return self.state == RepositoryReadinessState.READY

    @property
    def code(self) -> Optional[str]:
        return None if self.ready else self.state.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "ready": self.ready,
            "code": self.code,
            "repository_id": self.repository_id,
            "repository_name": self.repository_name,
            "registered_path": self.registered_path,
            "requested_path": self.requested_path,
            "tracked_branch": self.tracked_branch,
            "current_branch": self.current_branch,
            "current_commit": self.current_commit,
            "last_indexed_commit": self.last_indexed_commit,
            "index_path": self.index_path,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class SemanticReadiness:
    state: SemanticReadinessState
    profile_id: Optional[str] = None
    compatibility_fingerprint: Optional[str] = None
    discovered_fingerprint: Optional[str] = None
    collection_name: Optional[str] = None
    discovered_collection_name: Optional[str] = None
    vector_dimension: Optional[int] = None
    discovered_vector_dimension: Optional[int] = None
    remediation: Optional[str] = None
    evidence: Optional[dict[str, Any]] = None

    @property
    def ready(self) -> bool:
        return self.state == SemanticReadinessState.READY

    @property
    def code(self) -> Optional[str]:
        return None if self.ready else self.state.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "ready": self.ready,
            "code": self.code,
            "profile_id": self.profile_id,
            "compatibility_fingerprint": self.compatibility_fingerprint,
            "discovered_fingerprint": self.discovered_fingerprint,
            "collection_name": self.collection_name,
            "discovered_collection_name": self.discovered_collection_name,
            "vector_dimension": self.vector_dimension,
            "discovered_vector_dimension": self.discovered_vector_dimension,
            "remediation": self.remediation,
            "evidence": self.evidence,
        }


def _index_file_signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _index_integrity_signature(
    index_path: Path,
) -> tuple[
    tuple[int, int, int, int] | None,
    tuple[int, int, int, int] | None,
    tuple[int, int, int, int] | None,
]:
    database = _index_file_signature(index_path)
    wal = _index_file_signature(Path(f"{index_path}-wal"))
    shm = _index_file_signature(Path(f"{index_path}-shm"))
    return (database, wal, shm)


_IndexSignature = tuple[
    tuple[int, int, int, int] | None,
    tuple[int, int, int, int] | None,
    tuple[int, int, int, int] | None,
]
_HEALTHY_INDEX_CACHE_SIZE = 256
_healthy_index_cache: OrderedDict[tuple[str, _IndexSignature], None] = OrderedDict()
_healthy_index_cache_lock = threading.Lock()


def _inspect_index_uncached(index_path: Path) -> Optional[RepositoryReadinessState]:
    required_tables = {"schema_version", "repositories", "files", "symbols", "code_chunks"}
    try:
        with sqlite3.connect(str(index_path)) as conn:
            integrity = conn.execute("PRAGMA quick_check").fetchone()
            if not integrity or integrity[0] != "ok":
                return RepositoryReadinessState.CORRUPT_SQLITE
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
            tables = {str(row[0]) for row in rows}
            if not required_tables.issubset(tables):
                return RepositoryReadinessState.MISSING_SCHEMA
            count = conn.execute(
                "SELECT COUNT(*) FROM files WHERE is_deleted = 0 OR is_deleted IS NULL"
            ).fetchone()
            if not count or int(count[0]) == 0:
                return RepositoryReadinessState.INDEX_EMPTY
            # CHUNKERSAFE Lane A: a half-rebuilt or mixed-scheme code_chunks table
            # passes the table-presence check but is not query-safe. Report a
            # non-ready state so the guard trip is visible at the readiness surface.
            scheme_status, _marker, _target = evaluate_chunk_scheme(conn)
            if scheme_status == "rebuilding":
                return RepositoryReadinessState.INDEX_REBUILDING
            if scheme_status in ("mismatch", "missing_marker"):
                return RepositoryReadinessState.SCHEME_MISMATCH
    except sqlite3.OperationalError as exc:
        # A "database is locked"/"database is busy" OperationalError is a
        # concurrent-writer contention signal over a busy-but-HEALTHY index, not
        # on-disk corruption. Classify it distinctly (and BEFORE the DatabaseError
        # catch-all, since OperationalError subclasses DatabaseError) so a
        # transient lock is never reported as CORRUPT_SQLITE and never triggers a
        # destructive quarantine/reindex. Any other OperationalError falls through
        # to the corruption fail-closed default.
        message = str(exc).lower()
        if "database is locked" in message or "database is busy" in message or "locked" in message:
            return RepositoryReadinessState.INDEX_LOCKED
        return RepositoryReadinessState.CORRUPT_SQLITE
    except sqlite3.DatabaseError:
        return RepositoryReadinessState.CORRUPT_SQLITE
    return None


def _inspect_index_snapshot(
    index_path: str,
    signature: _IndexSignature,
) -> Optional[RepositoryReadinessState]:
    key = (index_path, signature)
    with _healthy_index_cache_lock:
        if key in _healthy_index_cache:
            _healthy_index_cache.move_to_end(key)
            return None

    result = _inspect_index_uncached(Path(index_path))
    if result is not None:
        return result

    with _healthy_index_cache_lock:
        stale_keys = [
            cached_key for cached_key in _healthy_index_cache if cached_key[0] == index_path
        ]
        for stale_key in stale_keys:
            _healthy_index_cache.pop(stale_key, None)
        _healthy_index_cache[key] = None
        while len(_healthy_index_cache) > _HEALTHY_INDEX_CACHE_SIZE:
            _healthy_index_cache.popitem(last=False)
    return None


def _clear_index_inspection_cache() -> None:
    with _healthy_index_cache_lock:
        _healthy_index_cache.clear()


class ReadinessClassifier:
    """Classify registered and arbitrary repository paths without mutating state."""

    @classmethod
    def classify_registered(
        cls,
        repo_info: Any,
        requested_path: Optional[Path] = None,
        indexing_active: bool = False,
    ) -> RepositoryReadiness:
        registered_path = Path(repo_info.path).resolve(strict=False)
        index_path = (
            Path(repo_info.index_path).resolve(strict=False) if repo_info.index_path else None
        )
        cached_branch = getattr(repo_info, "current_branch", None)
        cached_commit = getattr(repo_info, "current_commit", None)
        live_git_required = bool(
            getattr(repo_info, "git_common_dir", None)
            and isinstance(cached_commit, str)
            and len(cached_commit) in {40, 64}
            and all(char in "0123456789abcdef" for char in cached_commit.lower())
        )
        if live_git_required:
            current_branch = _git_branch(registered_path)
            current_commit = _git_commit(registered_path)
        else:
            current_branch = cached_branch or _git_branch(registered_path)
            current_commit = cached_commit or _git_commit(registered_path)
        tracked_branch = getattr(repo_info, "tracked_branch", None)
        last_indexed_commit = getattr(repo_info, "last_indexed_commit", None)
        staleness_reason = getattr(repo_info, "staleness_reason", None)

        state = RepositoryReadinessState.READY
        remediation = None

        if live_git_required and (current_branch is None or current_commit is None):
            state = RepositoryReadinessState.UNREGISTERED_REPOSITORY
            remediation = "Restore the registered Git checkout or register an available repository."
        elif indexing_active or staleness_reason == "index_building":
            state = RepositoryReadinessState.INDEX_BUILDING
            remediation = "Wait for indexing to finish, then retry the request."
        elif tracked_branch and current_branch and tracked_branch != current_branch:
            state = RepositoryReadinessState.WRONG_BRANCH
            remediation = (
                f"Switch to the tracked branch '{tracked_branch}' or register the intended "
                "repository path."
            )
        elif index_path is None or not index_path.exists():
            state = RepositoryReadinessState.MISSING_INDEX
            remediation = "Run reindex or pull the latest artifact for this repository."
        else:
            db_state = cls._inspect_index(index_path)

            if db_state == RepositoryReadinessState.INDEX_LOCKED:
                state = RepositoryReadinessState.INDEX_LOCKED
                remediation = (
                    "The index is temporarily locked by a concurrent writer; the "
                    "index is healthy -- retry the query shortly (do not reindex)."
                )
            elif db_state == RepositoryReadinessState.CORRUPT_SQLITE:
                state = RepositoryReadinessState.CORRUPT_SQLITE
                remediation = "Run reindex to quarantine the corrupt bytes and rebuild the index."
            elif db_state == RepositoryReadinessState.MISSING_SCHEMA:
                state = RepositoryReadinessState.MISSING_SCHEMA
                remediation = "Run reindex to quarantine the incomplete schema and rebuild it."
            elif db_state == RepositoryReadinessState.INDEX_REBUILDING:
                state = RepositoryReadinessState.INDEX_REBUILDING
                remediation = (
                    "A chunk-scheme rebuild is in progress; wait for it to finalize "
                    "or resume reindex before querying code chunks."
                )
            elif db_state == RepositoryReadinessState.SCHEME_MISMATCH:
                state = RepositoryReadinessState.SCHEME_MISMATCH
                remediation = (
                    "The chunk-identity scheme is incompatible with the installed "
                    "chunker; run reindex to rebuild the index under the current scheme."
                )
            elif staleness_reason in {"index_publication_pending", "partial_index_failure"}:
                state = RepositoryReadinessState.MISSING_PROVENANCE
                remediation = "Run reindex to quarantine the interrupted generation and rebuild it."
            elif db_state == RepositoryReadinessState.INDEX_EMPTY:
                state = RepositoryReadinessState.INDEX_EMPTY
                remediation = "Run reindex to populate the repository index."
            elif last_indexed_commit is None or not str(last_indexed_commit).strip():
                state = RepositoryReadinessState.MISSING_PROVENANCE
                remediation = "Run reindex to quarantine the unproven index and rebuild it."
            elif current_commit and last_indexed_commit and current_commit != last_indexed_commit:
                state = RepositoryReadinessState.STALE_COMMIT
                remediation = "Run reindex to update the repository index to the current commit."
            elif live_git_required and not _tracked_tree_clean(registered_path):
                state = RepositoryReadinessState.STALE_COMMIT
                remediation = "Commit or discard tracked edits before querying the committed index."

        return RepositoryReadiness(
            state=state,
            repository_id=getattr(repo_info, "repository_id", None),
            repository_name=getattr(repo_info, "name", None),
            registered_path=str(registered_path),
            requested_path=(
                str(Path(requested_path).resolve(strict=False)) if requested_path else None
            ),
            tracked_branch=tracked_branch,
            current_branch=current_branch,
            current_commit=current_commit,
            last_indexed_commit=last_indexed_commit,
            index_path=str(index_path) if index_path else None,
            remediation=remediation,
        )

    @classmethod
    def classify_path(
        cls,
        registry: Any,
        selector: str | Path,
        indexing_active: bool = False,
    ) -> RepositoryReadiness:
        selector_str = str(selector)

        # 1. Exact ID match
        id_matches = [repo for repo in registry.list_all() if repo.repository_id == selector_str]
        if len(id_matches) > 1:
            return RepositoryReadiness(
                state=RepositoryReadinessState.AMBIGUOUS_SELECTOR,
                requested_path=selector_str,
                remediation="Ambiguous selector: matches multiple repository IDs.",
            )
        if len(id_matches) == 1:
            return cls.classify_registered(
                id_matches[0], requested_path=None, indexing_active=indexing_active
            )

        # 2. Exact registered name match
        name_matches = [repo for repo in registry.list_all() if repo.name == selector_str]
        if len(name_matches) > 1:
            return RepositoryReadiness(
                state=RepositoryReadinessState.AMBIGUOUS_SELECTOR,
                requested_path=selector_str,
                remediation="Ambiguous selector: matches multiple repository names.",
            )
        if len(name_matches) == 1:
            return cls.classify_registered(
                name_matches[0], requested_path=None, indexing_active=indexing_active
            )

        # 3. Path-based matching
        is_path_like = (
            isinstance(selector, Path)
            or Path(selector_str).expanduser().exists()
            or any(sep in selector_str for sep in (os.path.sep, "/"))
            or selector_str.startswith(".")
            or selector_str.startswith("~")
        )

        resolved_path = None
        if is_path_like:
            p = Path(selector).expanduser()
            if p.is_absolute():
                resolved_path = p.resolve()
            else:
                # Relative path: only resolve if it exists. No CWD fallback for nonexistent relative paths/names.
                if (Path.cwd() / p).exists() or p.exists():
                    resolved_path = p.resolve()
                else:
                    resolved_path = None

        if resolved_path is None:
            return RepositoryReadiness(
                state=RepositoryReadinessState.UNREGISTERED_REPOSITORY,
                requested_path=selector_str,
                remediation="Register this repository path before querying it.",
            )

        # Fast-path for exact registered path
        repo_id = registry.find_by_path(resolved_path)
        if repo_id:
            repo_info = registry.get(repo_id)
            if repo_info is not None:
                return cls.classify_registered(
                    repo_info, requested_path=resolved_path, indexing_active=indexing_active
                )

        git_root = _find_git_root(resolved_path)
        if git_root is None:
            return RepositoryReadiness(
                state=RepositoryReadinessState.UNREGISTERED_REPOSITORY,
                requested_path=str(resolved_path),
                remediation="Register this repository path before querying it.",
            )

        # Fast-path for exact registered path on git_root
        repo_id = registry.find_by_path(git_root)
        if repo_id:
            repo_info = registry.get(repo_id)
            if repo_info is not None:
                return cls.classify_registered(
                    repo_info, requested_path=resolved_path, indexing_active=indexing_active
                )

        try:
            git_common_dir = compute_repo_id(git_root).git_common_dir
        except Exception:
            git_common_dir = None

        exact_matches = []
        worktree_matches = []
        for repo in registry.list_all():
            reg_path = Path(repo.path).resolve()
            if git_root == reg_path:
                exact_matches.append(repo)
            else:
                if repo.git_common_dir and git_common_dir:
                    if Path(repo.git_common_dir).resolve() == Path(git_common_dir).resolve():
                        worktree_matches.append(repo)

        if exact_matches:
            if len(exact_matches) > 1:
                return RepositoryReadiness(
                    state=RepositoryReadinessState.AMBIGUOUS_SELECTOR,
                    requested_path=str(resolved_path),
                    remediation="Ambiguous path selector: matches multiple registered repositories.",
                )
            return cls.classify_registered(
                exact_matches[0], requested_path=resolved_path, indexing_active=indexing_active
            )
        elif worktree_matches:
            return cls._unsupported_worktree(worktree_matches[0], resolved_path)
        else:
            return RepositoryReadiness(
                state=RepositoryReadinessState.UNREGISTERED_REPOSITORY,
                requested_path=str(resolved_path),
                remediation="Register this repository path before querying it.",
            )

    @classmethod
    def classify_semantic_registered(
        cls,
        repo_info: Any,
        sqlite_store: Any,
    ) -> SemanticReadiness:
        profile = _current_semantic_profile()
        if profile is None:
            return SemanticReadiness(
                state=SemanticReadinessState.ENRICHMENT_UNAVAILABLE,
                remediation="Configure a semantic profile before treating semantic search as ready.",
            )

        metadata = _load_index_metadata(Path(repo_info.path))
        expected_collection = _profile_collection_name(profile)
        evidence = _semantic_evidence(sqlite_store, profile.profile_id, expected_collection)
        metadata_profile = _current_profile_metadata(metadata, profile.profile_id)
        discovered_fingerprint = _metadata_fingerprint(metadata_profile)
        discovered_collection = _metadata_string(metadata_profile, "collection_name")
        discovered_dimension = _metadata_int(metadata_profile, "model_dimension")
        remediation = "Run semantic summary/vector generation for the current profile before semantic queries."

        if evidence is None:
            return SemanticReadiness(
                state=SemanticReadinessState.ENRICHMENT_UNAVAILABLE,
                profile_id=profile.profile_id,
                compatibility_fingerprint=profile.compatibility_fingerprint,
                collection_name=expected_collection,
                vector_dimension=profile.vector_dimension,
                remediation="Semantic evidence tables are unavailable; repair the local index store.",
            )

        if int(evidence["missing_summaries"]) > 0:
            return SemanticReadiness(
                state=SemanticReadinessState.SUMMARIES_MISSING,
                profile_id=profile.profile_id,
                compatibility_fingerprint=profile.compatibility_fingerprint,
                collection_name=expected_collection,
                vector_dimension=profile.vector_dimension,
                remediation=remediation,
                evidence=evidence,
            )

        if int(evidence["missing_vectors"]) > 0 or int(evidence["vector_link_count"]) == 0:
            return SemanticReadiness(
                state=SemanticReadinessState.VECTORS_MISSING,
                profile_id=profile.profile_id,
                compatibility_fingerprint=profile.compatibility_fingerprint,
                collection_name=expected_collection,
                vector_dimension=profile.vector_dimension,
                remediation=remediation,
                evidence=evidence,
            )

        if discovered_dimension is not None and discovered_dimension != profile.vector_dimension:
            return SemanticReadiness(
                state=SemanticReadinessState.VECTOR_DIMENSION_MISMATCH,
                profile_id=profile.profile_id,
                compatibility_fingerprint=profile.compatibility_fingerprint,
                discovered_fingerprint=discovered_fingerprint,
                collection_name=expected_collection,
                discovered_collection_name=discovered_collection,
                vector_dimension=profile.vector_dimension,
                discovered_vector_dimension=discovered_dimension,
                remediation="Rebuild semantic vectors with the current embedding dimension.",
                evidence=evidence,
            )

        if (
            expected_collection
            and discovered_collection
            and expected_collection != discovered_collection
        ) or int(evidence["collection_mismatches"]) > 0:
            return SemanticReadiness(
                state=SemanticReadinessState.PROFILE_MISMATCH,
                profile_id=profile.profile_id,
                compatibility_fingerprint=profile.compatibility_fingerprint,
                discovered_fingerprint=discovered_fingerprint,
                collection_name=expected_collection,
                discovered_collection_name=discovered_collection,
                vector_dimension=profile.vector_dimension,
                discovered_vector_dimension=discovered_dimension,
                remediation="Rebuild semantic vectors so the current profile and collection agree.",
                evidence=evidence,
            )

        if metadata_profile is None or (
            discovered_fingerprint and discovered_fingerprint != profile.compatibility_fingerprint
        ):
            return SemanticReadiness(
                state=SemanticReadinessState.SEMANTIC_STALE,
                profile_id=profile.profile_id,
                compatibility_fingerprint=profile.compatibility_fingerprint,
                discovered_fingerprint=discovered_fingerprint,
                collection_name=expected_collection,
                discovered_collection_name=discovered_collection,
                vector_dimension=profile.vector_dimension,
                discovered_vector_dimension=discovered_dimension,
                remediation="Semantic summaries/vectors are stale for the current profile; rebuild them.",
                evidence=evidence,
            )

        return SemanticReadiness(
            state=SemanticReadinessState.READY,
            profile_id=profile.profile_id,
            compatibility_fingerprint=profile.compatibility_fingerprint,
            discovered_fingerprint=discovered_fingerprint,
            collection_name=expected_collection,
            discovered_collection_name=discovered_collection,
            vector_dimension=profile.vector_dimension,
            discovered_vector_dimension=discovered_dimension,
            evidence=evidence,
        )

    @classmethod
    def _unsupported_worktree(cls, repo_info: Any, requested_path: Path) -> RepositoryReadiness:
        return RepositoryReadiness(
            state=RepositoryReadinessState.UNSUPPORTED_WORKTREE,
            repository_id=getattr(repo_info, "repository_id", None),
            repository_name=getattr(repo_info, "name", None),
            registered_path=str(Path(repo_info.path).resolve(strict=False)),
            requested_path=str(requested_path),
            tracked_branch=getattr(repo_info, "tracked_branch", None),
            current_branch=getattr(repo_info, "current_branch", None),
            current_commit=getattr(repo_info, "current_commit", None),
            last_indexed_commit=getattr(repo_info, "last_indexed_commit", None),
            index_path=str(Path(repo_info.index_path).resolve(strict=False)),
            remediation=_worktree_remediation(),
        )

    @staticmethod
    def _unregistered(requested_path: Path) -> RepositoryReadiness:
        return RepositoryReadiness(
            state=RepositoryReadinessState.UNREGISTERED_REPOSITORY,
            requested_path=str(requested_path),
            remediation="Register this repository path before querying it.",
        )

    @staticmethod
    def _inspect_index(index_path: Path) -> Optional[RepositoryReadinessState]:
        resolved = index_path.resolve(strict=False)
        return _inspect_index_snapshot(str(resolved), _index_integrity_signature(resolved))

    @staticmethod
    def clear_index_inspection_cache() -> None:
        _clear_index_inspection_cache()


def _worktree_remediation() -> str:
    return "Use the registered path or unregister it before registering another worktree."


def _find_git_root(start: Path) -> Optional[Path]:
    current = start.expanduser().resolve(strict=False)
    if current.is_file():
        current = current.parent
    top_level = _run_git(["rev-parse", "--show-toplevel"], current)
    if top_level:
        return Path(top_level).resolve(strict=False)
    is_bare = _run_git(["rev-parse", "--is-bare-repository"], current)
    if is_bare == "true":
        return current
    while True:
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def _git_branch(path: Path) -> Optional[str]:
    return _run_git(["rev-parse", "--abbrev-ref", "HEAD"], path)


def _git_commit(path: Path) -> Optional[str]:
    return _run_git(["rev-parse", "HEAD"], path)


def _tracked_tree_clean(path: Path) -> bool:
    try:
        return (
            subprocess.run(
                ["git", "diff", "--quiet", "--no-ext-diff", "HEAD", "--"],
                cwd=path,
                capture_output=True,
                timeout=10,
                env=get_full_env(),
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _run_git(args: list[str], cwd: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            env=get_full_env(),
            timeout=10,
        )
        value = result.stdout.strip()
        return value if value and value != "HEAD" else None
    except (OSError, subprocess.SubprocessError):
        return None


def _current_semantic_profile() -> Any:
    try:
        from mcp_server.artifacts.semantic_profiles import SemanticProfileRegistry
        from mcp_server.config.settings import get_settings

        settings = get_settings()
        if not settings.semantic_search_enabled:
            return None

        registry = SemanticProfileRegistry.from_raw(
            settings.get_semantic_profiles_config(),
            settings.get_semantic_default_profile(),
            tool_version=settings.app_version,
        )
        return registry.get()
    except Exception:
        return None


def _load_index_metadata(repo_root: Path) -> dict[str, Any]:
    metadata_path = repo_root / ".index_metadata.json"
    if not metadata_path.exists():
        return {}
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _semantic_evidence(
    sqlite_store: Any,
    profile_id: str,
    expected_collection: Optional[str],
) -> Optional[dict[str, Any]]:
    getter = getattr(sqlite_store, "get_semantic_readiness_evidence", None)
    if not callable(getter):
        return None
    try:
        result = getter(profile_id, collection=expected_collection)
        return result if isinstance(result, dict) else None
    except Exception:
        return None


def _current_profile_metadata(
    metadata: dict[str, Any], profile_id: str
) -> Optional[dict[str, Any]]:
    try:
        from mcp_server.artifacts.semantic_profiles import get_primary_semantic_profile_metadata
    except Exception:
        return None

    discovered_profile_id, payload = get_primary_semantic_profile_metadata(metadata)
    if payload is None:
        return None
    if discovered_profile_id == profile_id:
        return payload

    semantic_profiles = metadata.get("semantic_profiles")
    if isinstance(semantic_profiles, dict):
        candidate = semantic_profiles.get(profile_id)
        if isinstance(candidate, dict):
            return candidate
    return None


def _profile_collection_name(profile: Any) -> Optional[str]:
    metadata = getattr(profile, "build_metadata", None) or {}
    value = metadata.get("collection_name")
    return str(value).strip() if isinstance(value, str) and value.strip() else None


def _metadata_string(metadata: Optional[dict[str, Any]], key: str) -> Optional[str]:
    if not metadata:
        return None
    value = metadata.get(key)
    return str(value).strip() if isinstance(value, str) and value.strip() else None


def _metadata_int(metadata: Optional[dict[str, Any]], key: str) -> Optional[int]:
    if not metadata:
        return None
    value = metadata.get(key)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _metadata_fingerprint(metadata: Optional[dict[str, Any]]) -> Optional[str]:
    if not metadata:
        return None
    raw = metadata.get("compatibility_fingerprint") or metadata.get("compatibility_hash")
    return str(raw).strip() if isinstance(raw, str) and raw.strip() else None
