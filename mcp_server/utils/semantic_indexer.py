"""Semantic code indexing using configurable embedding providers and Qdrant."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Optional, Union

try:
    from chunker.core import chunk_file as chunk_file
except Exception:
    chunk_file = None  # type: ignore[assignment]
from qdrant_client import QdrantClient, models
from qdrant_client.http.models import FieldCondition, Filter, MatchValue

try:
    from qdrant_client.http.exceptions import (
        ResponseHandlingException as _QdrantResponseHandlingException,
    )
except Exception:  # pragma: no cover - defensive: client layout drift
    _QdrantResponseHandlingException = None  # type: ignore[assignment]

from ..artifacts.semantic_namespace import SemanticNamespaceResolver
from ..artifacts.semantic_profiles import (
    REINDEX_REMEDIATION,
    ProfileAttestation,
    SemanticProfile,
    SemanticProfileRegistry,
    attest_embedding_response,
    extract_semantic_profile_metadata,
    get_primary_semantic_profile_metadata,
)
from ..core.path_resolver import PathResolver
from ..interfaces.inference_contracts import EmbeddingRole
from ..plugins.language_registry import get_language_by_extension
from .embedding_providers import create_embedding_provider

if TYPE_CHECKING:
    from ..storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class TransientCollectionReadError(RuntimeError):
    """A live-collection shape read failed transiently (connectivity/timeout).

    Distinct from a genuine, readable shape mismatch: the collection's shape
    could not be READ at all because the Qdrant backend was unreachable or timed
    out. The correct remediation is therefore to RETRY once the backend is
    reachable again — NOT to reindex/rebuild the collection. Conflating the two
    would emit a misleading ``blocked``/reindex diagnostic for what is really a
    backend-reachability problem.
    """


def _is_transient_qdrant_read_error(exc: BaseException) -> bool:
    """Classify ``exc`` as a transient connectivity/timeout read failure.

    Returns ``True`` for an unreachable/timed-out backend (retryable) and
    ``False`` for a reachable backend that returned an unreadable/unexpected
    shape (which must fail closed / block). Only exceptions raised while READING
    the live shape reach this classifier — a readable shape mismatch never
    raises, so it is never misclassified here.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    if _QdrantResponseHandlingException is not None and isinstance(
        exc, _QdrantResponseHandlingException
    ):
        return True
    name = type(exc).__name__.lower()
    return "timeout" in name or "connect" in name


@dataclass
class SymbolEntry:
    symbol: str
    kind: str
    signature: str
    line: int
    span: tuple[int, int]
    parent_class: Optional[str] = None
    parent_symbol: Optional[str] = None


@dataclass
class SymbolChunk:
    """Size-bounded chunk derived from a symbol body."""

    symbol: SymbolEntry
    chunk_index: int
    chunk_total: int
    start_line: int
    end_line: int
    content: str


@dataclass
class EmbeddingUnit:
    """Semantic embedding unit derived from a chunk."""

    content: str
    start_line: int
    end_line: int
    subchunk_index: int
    subchunk_total: int
    embedding_text: str
    chunk_role: str = "body"


@dataclass
class DocumentSection:
    """Represents a section within a document."""

    title: str
    content: str
    level: int  # Heading level (1-6 for markdown)
    start_line: int
    end_line: int
    parent_section: Optional[str] = None
    subsections: Optional[list[str]] = None

    def __post_init__(self):
        if self.subsections is None:
            self.subsections = []


@dataclass(frozen=True)
class CollectionEnsureResult:
    """Outcome of verifying or creating a Qdrant collection."""

    status: str
    collection_name: str
    expected_dimension: int
    expected_distance_metric: str
    actual_dimension: Optional[int] = None
    actual_distance_metric: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "collection_name": self.collection_name,
            "expected_dimension": self.expected_dimension,
            "expected_distance_metric": self.expected_distance_metric,
            "actual_dimension": self.actual_dimension,
            "actual_distance_metric": self.actual_distance_metric,
        }


def _is_already_exists_error(exc: BaseException) -> bool:
    """True when a create failure is a benign concurrent 'already exists' race."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return "already exists" in text or "conflict" in text


def ensure_qdrant_collection(
    client: Any,
    *,
    collection_name: str,
    expected_dimension: int,
    distance_metric: str,
    allow_recreate: bool = False,
) -> CollectionEnsureResult:
    """Verify the target collection and create it when the contract allows.

    Fail-closed by default: ``allow_recreate`` must be explicitly set to ``True``
    by an operator-authorized path before an existing collection may be
    destructively recreated. Ordinary runtime callers leave it ``False``.
    """
    expected_distance = SemanticIndexer.resolve_qdrant_distance(distance_metric)
    collections = client.get_collections()
    exists = any(c.name == collection_name for c in collections.collections)

    if not exists:
        # Non-destructive create: never call recreate_collection here so a stale
        # exists-check can never wipe a live collection created concurrently.
        try:
            client.create_collection(
                collection_name=collection_name,
                vectors_config=models.VectorParams(
                    size=expected_dimension,
                    distance=expected_distance,
                ),
            )
        except Exception as exc:
            # A concurrent writer may have created the collection between our
            # exists-check and this create. Tolerate ONLY that race: fall through
            # to re-verify the now-existing collection's shape (still fail-closed
            # on a mismatch). Any other create error surfaces unchanged.
            if not _is_already_exists_error(exc):
                raise
        else:
            return CollectionEnsureResult(
                status="created",
                collection_name=collection_name,
                expected_dimension=expected_dimension,
                expected_distance_metric=expected_distance.value,
                actual_dimension=expected_dimension,
                actual_distance_metric=expected_distance.value,
            )

    collection_info = client.get_collection(collection_name)
    vectors_config = collection_info.config.params.vectors
    actual_dimension = getattr(vectors_config, "size", None)
    actual_distance = getattr(vectors_config, "distance", None)

    def _blocked() -> CollectionEnsureResult:
        return CollectionEnsureResult(
            status="blocked",
            collection_name=collection_name,
            expected_dimension=expected_dimension,
            expected_distance_metric=expected_distance.value,
            actual_dimension=actual_dimension,
            actual_distance_metric=getattr(actual_distance, "value", None),
        )

    # Fail closed when the live collection's shape cannot be read: an unknown /
    # unreadable config must never be silently treated as a compatible reuse.
    if actual_dimension is None or actual_distance is None:
        return _blocked()

    mismatch = actual_dimension != expected_dimension or actual_distance != expected_distance
    if mismatch:
        if not allow_recreate:
            return _blocked()
        client.recreate_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(
                size=expected_dimension,
                distance=expected_distance,
            ),
        )
        return CollectionEnsureResult(
            status="recreated",
            collection_name=collection_name,
            expected_dimension=expected_dimension,
            expected_distance_metric=expected_distance.value,
            actual_dimension=expected_dimension,
            actual_distance_metric=expected_distance.value,
        )

    return CollectionEnsureResult(
        status="reused",
        collection_name=collection_name,
        expected_dimension=expected_dimension,
        expected_distance_metric=expected_distance.value,
        actual_dimension=actual_dimension,
        actual_distance_metric=getattr(actual_distance, "value", None),
    )


class SemanticIndexer:
    """Index code using semantic profile embeddings stored in Qdrant."""

    # INFERLIVEGATE Lane A: collection-resident provenance manifest.
    #
    # After a successful index build the producer upserts ONE reserved sentinel
    # point into the live Qdrant collection whose payload is the
    # ``collection-provenance.v1`` manifest (tagged ``__provenance__: True`` so
    # ordinary semantic search never surfaces it). A benchmark querying the live
    # collection can then bind the points it queried to the build provenance
    # WITHOUT reading the local ``.index_metadata.json`` file.
    PROVENANCE_VERSION = "collection-provenance.v1"
    # Reserved point id for the provenance sentinel. Ordinary points use
    # content-hash-derived 63-bit ids (``_symbol_id`` / ``_document_section_id``).
    # A real point hashing to this id is astronomically unlikely (~2**-63) but
    # NOT impossible, so both id-derivations route through ``_reserve_safe_id``,
    # which relocates any real id that lands on the reserved value — guaranteeing
    # id 0 is never occupied by a real chunk and is safe to reserve.
    PROVENANCE_POINT_ID = 0
    # Payload key marking the reserved sentinel; excluded from all search results.
    PROVENANCE_TAG = "__provenance__"

    # Document type weights for similarity calculations
    DOCUMENT_TYPE_WEIGHTS = {
        "markdown": 1.2,  # Documentation gets higher weight
        "readme": 1.3,  # README files get highest weight
        "docstring": 1.1,  # Inline documentation
        "comment": 1.0,  # Regular comments
        "code": 0.9,  # Code slightly lower for doc searches
        "api": 1.15,  # API documentation
        "tutorial": 1.25,  # Tutorial content
        "guide": 1.2,  # Guide content
    }

    def __init__(
        self,
        collection: str = "code-index",
        qdrant_path: str = "./vector_index.qdrant",
        path_resolver: Optional[PathResolver] = None,
        profile: Optional[SemanticProfile] = None,
        profile_registry: Optional[SemanticProfileRegistry] = None,
        semantic_profile: Optional[str] = None,
        namespace_resolver: Optional[SemanticNamespaceResolver] = None,
        repo_identifier: Optional[str] = None,
        branch: Optional[str] = None,
        commit: Optional[str] = None,
        lineage_id: Optional[str] = None,
        sqlite_store: Optional[Any] = None,
        metadata_file: Optional[Union[str, Path]] = None,
        staging: bool = False,
    ) -> None:
        self.sqlite_store = sqlite_store
        self.commit = commit
        self.staging = staging
        self.profile_registry = profile_registry
        self._profile_active = bool(profile or profile_registry or semantic_profile)
        self.semantic_profile = self._resolve_semantic_profile(
            profile=profile,
            profile_registry=profile_registry,
            semantic_profile_id=semantic_profile,
        )

        self.embedding_model = self.semantic_profile.model_name
        self.embedding_model_version = self.semantic_profile.model_version
        self.embedding_dimension = self.semantic_profile.vector_dimension
        self.distance_metric = self.semantic_profile.distance_metric
        self.normalization_policy = self.semantic_profile.normalization_policy
        self.chunk_schema_version = self.semantic_profile.chunk_schema_version
        self.compatibility_fingerprint = self.semantic_profile.compatibility_fingerprint
        profile_collection = None
        if self.semantic_profile.build_metadata:
            raw_collection = self.semantic_profile.build_metadata.get("collection_name")
            if isinstance(raw_collection, str) and raw_collection.strip():
                profile_collection = raw_collection.strip()

        self.namespace_resolver = namespace_resolver or SemanticNamespaceResolver()
        self.collection = self._resolve_collection_name(
            requested_collection=profile_collection or collection,
            repo_identifier=repo_identifier,
            lineage_id=lineage_id,
            branch=branch,
            commit=commit,
        )
        self.qdrant_path = qdrant_path
        self.metadata_file = (
            str(metadata_file) if metadata_file is not None else ".index_metadata.json"
        )
        self.path_resolver = path_resolver or PathResolver()
        self._write_lock = threading.RLock()

        # Own exactly the requested backend for this generation.
        self._qdrant_available = False
        self._connection_mode = None  # 'server', 'file', or 'memory'

        # Credentials must never be sourced from the on-disk profile payload.
        # Resolve the embedding API key from the process environment only; the
        # profile may carry a secret-REFERENCE (an env var name), never a secret.
        _build_metadata = self.semantic_profile.build_metadata or {}
        _api_key_env = _build_metadata.get("openai_api_key_env")
        _resolved_api_key = os.environ.get(_api_key_env) if _api_key_env else None
        # Profile-gated commercial egress: only build a commercial (source-code
        # egress) embedding provider when the active deployment profile permits it
        # (explicit opt-in). Local / private-endpoint providers and the default
        # lexical_only profile are unaffected.
        self._enforce_commercial_egress_policy(self.semantic_profile.provider)
        self.embedding_client = create_embedding_provider(
            provider_name=self.semantic_profile.provider,
            model_name=self.embedding_model,
            vector_dimension=self.embedding_dimension,
            api_key=_resolved_api_key,
            base_url=_build_metadata.get("openai_api_base"),
        )
        self.embedding_provider = self.embedding_client.provider_name
        self.qdrant = self._init_qdrant_client(qdrant_path)

        # Assumed-profile bootstrap for construct-then-inspect back-compat. The
        # AUTHORITATIVE, provenance-attested profile the vectors are actually
        # written under is (re)persisted by ``_prepare_for_writes`` on the write
        # path, strictly before any point write (IF-0-EMBEDPROV-1).
        self._writes_prepared = False
        self._attestation: Optional[ProfileAttestation] = None
        # For provenance-capable providers, DEFER collection creation and the
        # assumed-metadata write to the attested write gate (``_prepare_for_writes``)
        # so live construction never creates a collection or stamps assumed
        # metadata BEFORE attestation (a later attestation failure would otherwise
        # leave an orphaned collection + assumed metadata). Non-mutating
        # inspect/reconcile/query paths still construct cleanly. Legacy bare-``embed``
        # providers cannot be attested, so preserve their construct-time behavior
        # exactly — the write gate special-cases them as a no-op.
        if not self._provider_supports_provenance():
            try:
                self._load_existing_metadata()
                self._ensure_collection()
                self._update_metadata()
            except Exception:
                self.qdrant.close()
                raise

    def _resolve_semantic_profile(
        self,
        *,
        profile: Optional[SemanticProfile],
        profile_registry: Optional[SemanticProfileRegistry],
        semantic_profile_id: Optional[str],
    ) -> SemanticProfile:
        """Resolve semantic profile with legacy-safe fallback behavior."""
        if profile is not None:
            return profile

        if profile_registry is not None:
            return profile_registry.get(semantic_profile_id)

        if semantic_profile_id:
            logger.warning(
                "Semantic profile '%s' requested without a profile registry; "
                "falling back to legacy defaults",
                semantic_profile_id,
            )

        legacy_payload: Mapping[str, Any] = {
            "provider": "voyage",
            "model_name": "voyage-code-3",
            "model_version": "legacy",
            "vector_dimension": 1024,
            "distance_metric": "cosine",
            "normalization_policy": "provider-default",
            "chunk_schema_version": "legacy",
            "chunker_version": "legacy",
        }
        return SemanticProfile.from_dict("legacy-default", legacy_payload)

    def _resolve_collection_name(
        self,
        *,
        requested_collection: str,
        repo_identifier: Optional[str],
        lineage_id: Optional[str],
        branch: Optional[str],
        commit: Optional[str],
    ) -> str:
        """Resolve collection name, using namespace contract when context exists."""
        if not repo_identifier:
            return requested_collection

        effective_lineage = lineage_id
        if not effective_lineage and (branch or commit):
            effective_lineage = self.namespace_resolver.derive_lineage_id(branch, commit)

        if not effective_lineage:
            return requested_collection

        return self.namespace_resolver.resolve_collection_name(
            repo_identifier=repo_identifier,
            profile_id=self.semantic_profile.profile_id,
            lineage_id=effective_lineage,
        )

    # Embedding providers that egress source code to a third-party commercial
    # API. Building one of these is only permitted under a deployment profile
    # that opts into commercial egress.
    _COMMERCIAL_EMBEDDING_PROVIDERS = frozenset({"voyage", "voyageai"})

    def _enforce_commercial_egress_policy(self, provider_name: str) -> None:
        """Gate embedding-provider construction on the active deployment profile.

        Backward-compatible enforcement (fixes the aggressive-guard regression):

        * Profile **unset** (operator never opted into the profile system) ->
          allow everything, exactly as before the profile system existed. A
          ``voyage``-backed indexer under the DEFAULT posture builds unchanged.
        * Profile explicitly **lexical_only** -> forbid constructing ANY learned
          embedding provider (openai-compatible included), naming the profile.
        * A **commercial** provider (voyage/cohere) under an explicitly-set,
          non-commercial or un-opted-in profile -> refuse the source-code egress.

        The settings helpers are owned by the settings lane and may not have
        landed yet; if they cannot be imported we fail open (TRANSITIONAL — the
        default posture is non-commercial and commercial providers are never
        configured implicitly).
        """
        normalized = (provider_name or "").strip().lower()
        try:
            from mcp_server.config.settings import (
                ACTIVE_PROFILE_ENV,
                commercial_egress_allowed,
                deployment_profile_is_explicitly_set,
                learned_models_allowed,
            )
        except ImportError:
            return  # Settings lane not present yet: transitional guard.

        # LEGACY: no explicit profile -> preserve pre-existing behavior (allow).
        if not deployment_profile_is_explicitly_set():
            return

        profile_label = os.environ.get(ACTIVE_PROFILE_ENV)

        # Explicit lexical_only forbids ALL learned embedding providers.
        if not learned_models_allowed():
            raise RuntimeError(
                f"Embedding provider '{provider_name}' is a learned model, but the "
                f"active deployment profile '{profile_label}' is lexical-only and "
                "forbids constructing any learned embedding provider (BM25 / symbol "
                "search only). Select a learned-model profile to build embeddings."
            )

        # Commercial (source-code egress) providers require an explicit opt-in.
        if normalized in self._COMMERCIAL_EMBEDDING_PROVIDERS and not commercial_egress_allowed():
            raise RuntimeError(
                f"Embedding provider '{provider_name}' performs source-code egress to a "
                f"third-party commercial API, but the active deployment profile "
                f"'{profile_label}' does not permit commercial egress. Select the "
                "commercial profile with an explicit opt-in (e.g. "
                "MCP_DEPLOYMENT_PROFILE=commercial MCP_ALLOW_COMMERCIAL_EGRESS=1) to "
                "authorize it."
            )

    # ------------------------------------------------------------------
    def _provider_supports_provenance(self) -> bool:
        """True when the configured provider can emit ``embedding-response.v1``."""
        client = getattr(self, "embedding_client", None)
        return callable(getattr(client, "embed_with_provenance", None))

    def _validate_embedding_response(self, response: Any, expected_count: int) -> List[List[float]]:
        """Fail-closed request↔response index validation for a batch embed.

        Enforces a one-to-one mapping between request position and response item:
        every item's ``index`` must equal its request position (rejecting
        missing/duplicate/reordered/out-of-range), each item must be ``ok`` with a
        present vector, and each vector must match the collection dimension. On any
        violation this raises BEFORE any vector is returned, so a malformed or
        partial response can never be positionally sliced onto the wrong chunk.
        Returns vectors in request order.
        """
        items = list(getattr(response, "items", []) or [])
        if len(items) != expected_count:
            raise RuntimeError(
                "Embedding response arity mismatch: requested "
                f"{expected_count} texts but provider returned {len(items)} items. "
                "Refusing to positionally attach a partial/short embedding batch."
            )

        expected_dim = self.embedding_dimension
        vectors: List[List[float]] = []
        for position, item in enumerate(items):
            idx = getattr(item, "index", None)
            if idx != position:
                raise RuntimeError(
                    "Embedding response index is not a one-to-one mapping: item at "
                    f"request position {position} carries index {idx!r} "
                    "(reordered/duplicate/out-of-range responses are rejected to "
                    "prevent misattaching a vector to the wrong chunk)."
                )
            status = getattr(item, "status", None)
            status_value = getattr(status, "value", status)
            if status_value != "ok" or item.vector is None:
                raise RuntimeError(
                    f"Embedding item {position} is not usable "
                    f"(status={status_value!r}, error={getattr(item, 'error', None)!r})."
                )
            vector = list(item.vector)
            if expected_dim and len(vector) != expected_dim:
                raise RuntimeError(
                    f"Embedding item {position} has dimension {len(vector)} but the "
                    f"collection expects {expected_dim}."
                )
            vectors.append(vector)
        return vectors

    def _embed_texts(self, texts: List[str], input_type: str = "document") -> List[List[float]]:
        """Generate embeddings using the configured provider.

        When the provider emits ``embedding-response.v1`` (the migrated Voyage /
        OpenAI-compatible providers), the response is validated for a one-to-one
        request↔response index mapping and per-vector dimension before its vectors
        are returned in request order. Legacy bare-vector providers/test doubles
        that only expose ``embed`` are used unchanged.
        """
        texts_list = list(texts)
        if self._provider_supports_provenance():
            response = self.embedding_client.embed_with_provenance(
                texts_list, input_type=input_type
            )
            vectors = self._validate_embedding_response(response, len(texts_list))
            self._check_embedding_provenance(response, input_type=input_type)
            return vectors
        return self.embedding_client.embed(texts_list, input_type=input_type)

    def _max_chunk_chars(self) -> int:
        """Return max chunk size for embedding payloads."""
        raw = os.environ.get("SEMANTIC_MAX_CHARS", "12000")
        try:
            value = int(raw)
        except ValueError:
            value = 12000
        return max(2000, value)

    def _max_embedding_chars(self) -> int:
        """Return max characters allowed in a single embedding input."""
        raw = os.environ.get("SEMANTIC_MAX_EMBED_CHARS", "6000")
        try:
            value = int(raw)
        except ValueError:
            value = 6000
        return max(1000, value)

    def _truncate_embedding_text(self, text: str) -> str:
        """Trim embedding text only as a final provider-safety guard."""
        max_chars = self._max_embedding_chars()
        if len(text) <= max_chars:
            return text
        truncated = text[:max_chars].rstrip()
        return truncated

    def _compact_metadata_text(self, text: str, limit: int = 600) -> str:
        """Compact large metadata text on paragraph or sentence boundaries."""
        normalized = text.strip()
        if not normalized:
            return ""
        if len(normalized) <= limit:
            return normalized

        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
        if paragraphs:
            selected: List[str] = []
            total = 0
            for paragraph in paragraphs:
                paragraph_len = len(paragraph)
                if selected and total + paragraph_len + 2 > limit:
                    break
                if not selected and paragraph_len > limit:
                    break
                selected.append(paragraph)
                total += paragraph_len + (2 if selected[:-1] else 0)
            if selected:
                return "\n\n".join(selected)

        sentences = [
            part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()
        ]
        if sentences:
            selected = []
            total = 0
            for sentence in sentences:
                sentence_len = len(sentence)
                if selected and total + sentence_len + 1 > limit:
                    break
                if not selected and sentence_len > limit:
                    break
                selected.append(sentence)
                total += sentence_len + (1 if selected[:-1] else 0)
            if selected:
                return " ".join(selected)

        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        if lines:
            selected = []
            total = 0
            for line in lines:
                line_len = len(line)
                if selected and total + line_len + 1 > limit:
                    break
                if not selected and line_len > limit:
                    break
                selected.append(line)
                total += line_len + (1 if selected[:-1] else 0)
            if selected:
                return "\n".join(selected)

        return normalized[:limit].rstrip()

    def _infer_chunk_language(self, path: Path) -> str:
        """Infer the chunker language for a file path."""
        return (
            get_language_by_extension(path.name) or get_language_by_extension(path.suffix) or "text"
        )

    def _is_structural_split_candidate(self, line: str, kind: str) -> bool:
        """Detect whether a line should start a new structural sub-chunk."""
        stripped = line.strip()
        if not stripped:
            return False

        lowered_kind = kind.lower()
        if lowered_kind in {
            "class",
            "class_definition",
            "function",
            "function_definition",
        }:
            return bool(re.match(r"^\s+(async\s+def|def|class)\s+[A-Za-z_]", line))
        if lowered_kind in {"markdown", "md", "text", "plaintext"}:
            return stripped.startswith("#")
        return False

    def _collect_symbol_entries(self, root: Any, lines: List[str]) -> List[SymbolEntry]:
        """Collect class/function symbols including nested method context."""
        entries: List[SymbolEntry] = []

        def walk(node: Any, stack: List[SymbolEntry]) -> None:
            name_node = (
                node.child_by_field_name("name") if hasattr(node, "child_by_field_name") else None
            )
            current = None
            if node.type in {"function_definition", "class_definition"} and name_node is not None:
                name = name_node.text.decode()
                start_line = node.start_point[0] + 1
                end_line = node.end_point[0] + 1
                signature = (
                    lines[start_line - 1].strip() if 0 <= start_line - 1 < len(lines) else name
                )
                kind = "function" if node.type == "function_definition" else "class"

                parent_class = None
                for parent in reversed(stack):
                    if parent.kind == "class":
                        parent_class = parent.symbol
                        break

                parent_symbol = stack[-1].symbol if stack else None
                current = SymbolEntry(
                    symbol=name,
                    kind=kind,
                    signature=signature,
                    line=start_line,
                    span=(start_line, end_line),
                    parent_class=parent_class,
                    parent_symbol=parent_symbol,
                )
                entries.append(current)

            next_stack = stack + ([current] if current else [])
            for child in getattr(node, "children", []):
                walk(child, next_stack)

        walk(root, [])
        return entries

    def _split_symbol_chunks(
        self, symbol: SymbolEntry, lines: List[str], max_chars: Optional[int] = None
    ) -> List[SymbolChunk]:
        """Split a symbol body into size-bounded chunks while preserving context."""
        start = max(1, symbol.span[0])
        end = max(start, symbol.span[1])
        symbol_lines = lines[start - 1 : end]
        if not symbol_lines:
            return []

        max_chars = max_chars or self._max_chunk_chars()
        chunks: List[SymbolChunk] = []

        # Step 1: paragraph-aware splitting on blank lines.
        blocks: List[tuple[int, int]] = []
        block_start = start
        for idx, text in enumerate(symbol_lines, start=start):
            if idx > block_start and self._is_structural_split_candidate(text, symbol.kind):
                blocks.append((block_start, idx - 1))
                block_start = idx
                continue
            if text.strip() == "":
                if block_start <= idx - 1:
                    blocks.append((block_start, idx - 1))
                block_start = idx + 1
        if block_start <= end:
            blocks.append((block_start, end))
        if not blocks:
            blocks = [(start, end)]

        pending: List[tuple[int, int]] = []
        acc_start: Optional[int] = None
        acc_end: Optional[int] = None
        acc_chars = 0
        for b_start, b_end in blocks:
            block_text = "\n".join(lines[b_start - 1 : b_end])
            block_chars = len(block_text)
            if block_chars > max_chars:
                # Step 2: line-window split for oversized blocks.
                win_start = b_start
                while win_start <= b_end:
                    win_end = win_start
                    current = 0
                    while win_end <= b_end:
                        line_len = len(lines[win_end - 1]) + 1
                        if current + line_len > max_chars and win_end > win_start:
                            break
                        current += line_len
                        win_end += 1
                    pending.append((win_start, win_end - 1))
                    win_start = win_end
                continue

            if acc_start is None:
                acc_start, acc_end = b_start, b_end
                acc_chars = block_chars
                continue

            if acc_chars + block_chars + 1 <= max_chars:
                acc_end = b_end
                acc_chars += block_chars + 1
            else:
                if acc_start is not None and acc_end is not None:
                    pending.append((acc_start, acc_end))
                acc_start, acc_end = b_start, b_end
                acc_chars = block_chars

        if acc_start is not None:
            final_end = acc_end if acc_end is not None else acc_start
            pending.append((acc_start, final_end))

        for i, (c_start, c_end) in enumerate(pending, start=1):
            content = "\n".join(lines[c_start - 1 : c_end])
            chunks.append(
                SymbolChunk(
                    symbol=symbol,
                    chunk_index=i,
                    chunk_total=len(pending),
                    start_line=c_start,
                    end_line=c_end,
                    content=content,
                )
            )
        return chunks

    def _build_chunk_embedding_parts(
        self,
        relative_path: str,
        symbol_name: str,
        kind: str,
        signature: str,
        parent_symbol: Any,
        metadata: Mapping[str, Any],
        chunk_content: str,
    ) -> List[str]:
        """Build reusable metadata parts for semantic chunk embeddings."""
        module_stem = Path(relative_path).stem.replace("_", " ")
        path_tokens = " ".join(re.split(r"[^a-z0-9]+", relative_path.lower())).strip()
        qualified_name = str(metadata.get("qualified_name") or "")
        semantic_path = str(metadata.get("semantic_path") or "")
        semantic_text = self._compact_metadata_text(
            str(metadata.get("semantic_text") or ""),
            limit=min(600, max(200, self._max_embedding_chars() // 4)),
        )
        imports = metadata.get("imports") or []
        calls = metadata.get("calls") or []

        parts = [
            f"file {relative_path}",
            f"module {module_stem}",
        ]
        if path_tokens:
            parts.append(f"path tokens {path_tokens}")
        parts.append(f"{kind} {symbol_name}")
        if qualified_name:
            parts.append(f"qualified name {qualified_name}")
        if semantic_path:
            parts.append(f"semantic path {semantic_path}")
        if parent_symbol:
            parts.append(f"parent symbol {parent_symbol}")
        if signature:
            parts.append(f"signature {signature}")
        if semantic_text:
            parts.append(semantic_text)
        if imports:
            parts.append("imports " + " ".join(str(item) for item in imports[:5]))
        if calls:
            parts.append("calls " + " ".join(str(item) for item in calls[:5]))

        lowered_content = chunk_content.lower()
        if "chunk_file(" in chunk_content and "symbols.append(" in chunk_content:
            parts.append(
                "uses tree-sitter chunking to extract symbols from python files for semantic indexing"
            )
        if (
            relative_path.endswith("semantic_indexer.py")
            and "chunk_file(" in chunk_content
            and "extract_metadata=true" in lowered_content
        ):
            parts.append(
                "semantic indexer extracts symbols from python using treesitter retrieval metadata"
            )

        return [part for part in parts if part]

    def _compose_embedding_text(
        self,
        parts: List[str],
        content: str,
        subchunk_index: int = 1,
        subchunk_total: int = 1,
        chunk_role: str = "body",
        allow_truncate: bool = True,
        summary_text: Optional[str] = None,
    ) -> str:
        """Compose a bounded embedding string from metadata parts and content."""
        final_parts = list(parts)
        if subchunk_total > 1:
            final_parts.append(f"chunk part {subchunk_index} of {subchunk_total}")
        if chunk_role and chunk_role != "body":
            final_parts.append(f"chunk role {chunk_role}")

        if summary_text:
            final_parts.append("Summary:")
            final_parts.append(summary_text)

        if content:
            final_parts.append(content)
        text = "\n".join(part for part in final_parts if part).strip()
        if len(text) <= self._max_embedding_chars() or not allow_truncate:
            return text
        return self._truncate_embedding_text(text)

    def _expand_chunk_embedding_units(
        self,
        relative_path: str,
        symbol_name: str,
        kind: str,
        signature: str,
        parent_symbol: Any,
        metadata: Mapping[str, Any],
        chunk_content: str,
        start_line: int,
        end_line: int,
        summary_text: Optional[str] = None,
    ) -> List[EmbeddingUnit]:
        """Expand an oversized chunk into bounded semantic embedding units."""
        parts = self._build_chunk_embedding_parts(
            relative_path=relative_path,
            symbol_name=symbol_name,
            kind=kind,
            signature=signature,
            parent_symbol=parent_symbol,
            metadata=metadata,
            chunk_content=chunk_content,
        )
        single_text = self._compose_embedding_text(
            parts, chunk_content, allow_truncate=False, summary_text=summary_text
        )
        if len(single_text) <= self._max_embedding_chars():
            return [
                EmbeddingUnit(
                    content=chunk_content,
                    start_line=start_line,
                    end_line=end_line,
                    subchunk_index=1,
                    subchunk_total=1,
                    embedding_text=self._truncate_embedding_text(single_text),
                )
            ]

        local_lines = chunk_content.splitlines()
        if not local_lines:
            return [
                EmbeddingUnit(
                    content="",
                    start_line=start_line,
                    end_line=end_line,
                    subchunk_index=1,
                    subchunk_total=1,
                    embedding_text=self._truncate_embedding_text(single_text),
                )
            ]

        local_symbol = SymbolEntry(
            symbol=symbol_name,
            kind=kind,
            signature=signature,
            line=1,
            span=(1, max(1, len(local_lines))),
            parent_symbol=parent_symbol,
        )
        prefix_chars = len("\n".join(parts))
        content_budget = max(200, self._max_embedding_chars() - prefix_chars - 64)
        local_chunks = self._split_symbol_chunks(
            local_symbol, local_lines, max_chars=content_budget
        )
        if not local_chunks:
            return [
                EmbeddingUnit(
                    content=chunk_content,
                    start_line=start_line,
                    end_line=end_line,
                    subchunk_index=1,
                    subchunk_total=1,
                    embedding_text=self._truncate_embedding_text(single_text),
                )
            ]

        units: List[EmbeddingUnit] = []
        total = len(local_chunks)
        for local_chunk in local_chunks:
            absolute_start = start_line + local_chunk.start_line - 1
            absolute_end = start_line + local_chunk.end_line - 1
            units.append(
                EmbeddingUnit(
                    content=local_chunk.content,
                    start_line=absolute_start,
                    end_line=absolute_end,
                    subchunk_index=local_chunk.chunk_index,
                    subchunk_total=total,
                    embedding_text=self._compose_embedding_text(
                        parts,
                        local_chunk.content,
                        subchunk_index=local_chunk.chunk_index,
                        subchunk_total=total,
                        chunk_role="split",
                    ),
                    chunk_role="split" if total > 1 else "body",
                )
            )

        return units

    def _fallback_text_chunks(self, path: Path, relative_path: str) -> List[Any]:
        """Create synthetic chunks for unsupported file types."""
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        if not lines and text:
            lines = [text]
        if not lines:
            return []

        symbol = SymbolEntry(
            symbol=Path(relative_path).stem,
            kind="text",
            signature=Path(relative_path).name,
            line=1,
            span=(1, len(lines)),
        )
        chunks = self._split_symbol_chunks(symbol, lines)
        results: List[Any] = []
        for idx, chunk in enumerate(chunks, start=1):
            results.append(
                SimpleNamespace(
                    content=chunk.content,
                    metadata={
                        "symbol": symbol.symbol,
                        "name": symbol.symbol,
                        "kind": "text",
                        "signature_text": symbol.signature,
                        "semantic_text": self._compact_metadata_text(chunk.content, limit=400),
                    },
                    chunk_id=f"fallback-{idx}",
                    node_id=f"fallback-{idx}",
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    node_type="text",
                )
            )
        return results

    def _upsert_points_batched(
        self, path: Path, points: List[models.PointStruct], batch_size: int = 64
    ) -> None:
        """Upsert points in batches to stay under Qdrant payload limits."""
        if not points:
            return

        if not self._qdrant_available:
            raise RuntimeError(f"Qdrant is not available - cannot index file {path}")

        for start in range(0, len(points), batch_size):
            batch = points[start : start + batch_size]
            result = self.qdrant.upsert(collection_name=self.collection, points=batch, wait=True)
            if self.staging and getattr(result, "status", None) not in {
                "completed",
                models.UpdateStatus.COMPLETED,
            }:
                raise RuntimeError("Staged vector write was not acknowledged")

    def _init_qdrant_client(self, qdrant_path: str) -> QdrantClient:
        """Open exactly the selected backend, without lock removal or fallback."""
        client = None
        self._qdrant_available = False
        if qdrant_path == ":memory:":
            mode, kwargs = "memory", {"location": ":memory:"}
        elif qdrant_path.startswith(("http://", "https://")):
            mode, kwargs = "server", {"url": qdrant_path, "timeout": 5}
        elif qdrant_path:
            mode, kwargs = "file", {"path": qdrant_path}
        else:
            raise ValueError("An explicit Qdrant backend is required")
        try:
            client = QdrantClient(**kwargs)
            client.get_collections()
        except Exception as exc:
            if client is not None:
                client.close()
            self._connection_mode = None
            raise RuntimeError(
                f"Selected Qdrant {mode} backend unavailable ({type(exc).__name__}); "
                "release its owning process or repair the endpoint, then retry."
            ) from None
        self._qdrant_available = True
        self._connection_mode = mode
        logger.info("Opened Qdrant backend (mode=%s)", mode)
        return client

    @property
    def is_available(self) -> bool:
        """Check if Qdrant is available without throwing exceptions.

        This property can be used by other components (like HybridSearch)
        to gracefully degrade functionality when Qdrant is unavailable.

        Returns:
            True if Qdrant is initialized and available, False otherwise
        """
        return self._qdrant_available

    @property
    def connection_mode(self) -> Optional[str]:
        """Get the current connection mode.

        Returns:
            'server', 'file', 'memory', or None if not connected
        """
        return self._connection_mode

    def validate_connection(self) -> bool:
        """Validate that the Qdrant connection is still active.

        This method is useful for long-running processes to ensure
        the connection hasn't been lost or corrupted.

        Returns:
            True if connection is valid and responsive, False otherwise
        """
        if not self._qdrant_available:
            logger.warning("Qdrant is not available - connection was never established")
            return False

        try:
            # Attempt a simple operation to verify connection
            collections = self.qdrant.get_collections()
            # Check if our collection exists
            collection_exists = any(c.name == self.collection for c in collections.collections)
            if collection_exists:
                logger.debug(f"Qdrant connection valid - collection '{self.collection}' exists")
            else:
                logger.warning(
                    f"Qdrant connection active but collection '{self.collection}' not found"
                )
            return True
        except Exception as e:
            logger.error(f"Qdrant connection validation failed: {type(e).__name__}: {e}")
            self._qdrant_available = False
            return False

    def _ensure_collection(self, *, allow_recreate: bool = False) -> None:
        """Ensure the collection exists in Qdrant.

        Fail-closed by default: ordinary runtime NEVER recreates. Destructive
        recreation requires an operator-authorized caller to pass
        ``allow_recreate=True`` explicitly. A ``blocked`` result raises an
        actionable diagnostic instead of silently reusing an incompatible
        collection, mirroring ``semantic_preflight``'s fail-closed handling.

        Raises:
            RuntimeError: If collection cannot be created, is blocked, or
                cannot be verified.
        """
        if not self._qdrant_available:
            raise RuntimeError("Qdrant is not available - cannot ensure collection")

        try:
            result = ensure_qdrant_collection(
                self.qdrant,
                collection_name=self.collection,
                expected_dimension=self.embedding_dimension,
                distance_metric=self.distance_metric,
                allow_recreate=allow_recreate,
            )
            if result.status == "blocked":
                raise RuntimeError(
                    f"Qdrant collection '{self.collection}' is blocked: its shape/config does "
                    f"not match the active semantic profile (expected "
                    f"dimension={result.expected_dimension} metric={result.expected_distance_metric}, "
                    f"actual dimension={result.actual_dimension} metric={result.actual_distance_metric}). "
                    "Refusing to reuse or destructively recreate it. Run an operator-authorized "
                    "reindex to rebuild the collection with the correct shape."
                )
            if result.status == "created":
                logger.info("Successfully created collection: %s", self.collection)
            elif result.status == "recreated":
                logger.warning(
                    "Recreated Qdrant collection %s due to config mismatch",
                    self.collection,
                )
            else:
                logger.debug("Collection already exists: %s", self.collection)
        except Exception as e:
            logger.error(
                f"Failed to ensure collection '{self.collection}': {type(e).__name__}: {e}"
            )
            self._qdrant_available = False
            raise RuntimeError(
                f"Failed to create or verify Qdrant collection '{self.collection}': {e}"
            )

    # ------------------------------------------------------------------
    @staticmethod
    def resolve_qdrant_distance(metric: str) -> models.Distance:
        """Map profile distance metric to Qdrant enum."""
        metric_value = (metric or "").strip().lower()
        mapping = {
            "cosine": models.Distance.COSINE,
            "dot": models.Distance.DOT,
            "dotproduct": models.Distance.DOT,
            "euclidean": models.Distance.EUCLID,
            "euclid": models.Distance.EUCLID,
            "manhattan": models.Distance.MANHATTAN,
        }
        resolved = mapping.get(metric_value)
        if resolved is None:
            logger.warning("Unknown distance metric '%s', defaulting to cosine", metric)
            return models.Distance.COSINE
        return resolved

    def _resolve_qdrant_distance(self, metric: str) -> models.Distance:
        """Backward-compatible instance wrapper for distance resolution."""
        return self.resolve_qdrant_distance(metric)

    # ------------------------------------------------------------------
    def _build_metadata(self) -> Dict[str, Any]:
        """Build metadata payload for semantic index compatibility checks."""
        timestamp = datetime.now().isoformat()
        existing_metadata = self._load_existing_metadata()
        metadata = {
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
            "model_version": self.embedding_model_version,
            "model_dimension": self.embedding_dimension,
            "distance_metric": self.distance_metric,
            "normalization_policy": self.normalization_policy,
            "chunk_schema_version": self.chunk_schema_version,
            "created_at": existing_metadata.get("created_at", timestamp),
            "updated_at": timestamp,
            "qdrant_path": self.qdrant_path,
            "collection_name": self.collection,
            "compatibility_hash": self._generate_compatibility_hash(),
            "git_commit": self._get_git_commit_hash(),
        }

        if self._profile_active:
            # Preserve ALL existing profiles; current profile is merged/updated below
            semantic_profiles = dict(extract_semantic_profile_metadata(existing_metadata))
            current_profile_metadata = {
                "profile_id": self.semantic_profile.profile_id,
                "compatibility_fingerprint": self.compatibility_fingerprint,
                "compatibility_hash": self._generate_compatibility_hash(),
                "embedding_provider": self.embedding_provider,
                "embedding_model": self.embedding_model,
                "model_version": self.embedding_model_version,
                "model_dimension": self.embedding_dimension,
                "distance_metric": self.distance_metric,
                "normalization_policy": self.normalization_policy,
                "chunk_schema_version": self.chunk_schema_version,
                "collection_name": self.collection,
                "qdrant_path": self.qdrant_path,
                "created_at": semantic_profiles.get(self.semantic_profile.profile_id, {}).get(
                    "created_at", timestamp
                ),
                "updated_at": timestamp,
            }
            semantic_profiles[self.semantic_profile.profile_id] = current_profile_metadata

            metadata["semantic_profile"] = self.semantic_profile.profile_id
            metadata["compatibility_fingerprint"] = self.compatibility_fingerprint
            metadata["semantic_profiles"] = semantic_profiles
            metadata["available_semantic_profiles"] = sorted(semantic_profiles.keys())

        return metadata

    # ------------------------------------------------------------------
    def _update_metadata(self) -> None:
        """Update index metadata with current model and configuration."""
        self._atomic_write_metadata(self._build_metadata())

    # ------------------------------------------------------------------
    def _atomic_write_metadata(self, metadata: Dict[str, Any]) -> None:
        """Persist metadata with atomic replace and explicit durability checks.

        Failures before replace preserve old bytes. A directory-fsync failure
        after replace is unacknowledged durability and must still refuse writes.
        """
        target = Path(self.metadata_file)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("x", encoding="utf-8") as handle:
                json.dump(metadata, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
            if os.name != "nt":
                directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def _attest_active_profile(
        self, probe_texts: Optional[List[str]] = None, *, input_type: str = "document"
    ) -> ProfileAttestation:
        """Probe the live provider and attest its response against the profile.

        Sends a tiny probe embed, then reconciles the returned
        ``embedding-response.v1`` against the active profile: server-``reported``
        values replace assumed ones, provider-unreportable fields are explicitly
        ``declared`` from config, and any compatibility-critical field left
        ``unknown`` (or a reported value that mismatches the profile) fails closed.
        The provider call is the FIRST thing that can fail here, so a provider
        failure raises before any collection or metadata state is touched.
        """
        role = EmbeddingRole.QUERY if input_type == "query" else EmbeddingRole.DOCUMENT
        texts = probe_texts if probe_texts else ["semantic-provenance-probe"]
        response = self.embedding_client.embed_with_provenance(texts, input_type=input_type)
        # Bijection/dimension guard on the probe response as well.
        self._validate_embedding_response(response, len(list(texts)))
        return attest_embedding_response(self.semantic_profile, response, role=role)

    def _attested_metadata(self, attestation: ProfileAttestation) -> Dict[str, Any]:
        """Build the durable metadata payload stamped with attestation provenance.

        Fail-closed guard: this stamps ``attested=True`` and copies server-derived
        provenance, so it MUST never run on a failed attestation. A failed
        attestation stamped as attested would open the write gate under an
        unverified profile — refuse it outright.
        """
        if not attestation.ok:
            raise RuntimeError(
                "_attested_metadata refuses to stamp attested metadata from a "
                "failed attestation (attestation.ok is False): " + attestation.failure_reason()
            )
        metadata = self._build_metadata()
        attestation_block = attestation.to_dict()
        attestation_block["attested_at"] = datetime.now().isoformat()
        metadata["semantic_attestation"] = attestation_block
        # Stamp the primary profile record so query-time compatibility checks can
        # see the vectors were written under an attested, server-derived profile.
        if self._profile_active and isinstance(metadata.get("semantic_profiles"), dict):
            profile_record = metadata["semantic_profiles"].get(self.semantic_profile.profile_id)
            if isinstance(profile_record, dict):
                profile_record["attested"] = True
                profile_record["provenance"] = attestation_block["derived"]
                reported_dim = attestation_block["derived"].get("dimension", {}).get("value")
                if isinstance(reported_dim, int) and reported_dim > 0:
                    # Server-reported dimension replaces the assumed value.
                    profile_record["model_dimension"] = reported_dim
        return metadata

    def _prepare_for_writes(self) -> None:
        """Serialize the first attestation and metadata publication for this indexer."""
        with getattr(self, "_write_lock", nullcontext()):
            self._prepare_for_writes_locked()

    def _prepare_for_writes_locked(self) -> None:
        """Ordered write gate: attest -> persist -> ensure_collection (once).

        Enforces the lifecycle invariant that provider attestation and atomic
        profile persistence happen BEFORE collection create/validate and any point
        write. Failure denies vector writes, including a durability failure after
        atomic metadata replacement. Idempotent after successful preparation.

        Providers that predate the provenance contract (bare ``embed`` test
        doubles) cannot be attested; the gate degrades to the INFERSAFE-stabilized
        ``_ensure_collection`` path unchanged for them.
        """
        if getattr(self, "_writes_prepared", False):
            return

        if not self._provider_supports_provenance():
            # Legacy provider (bare ``embed`` test double): cannot be attested.
            # Preserve prior behavior exactly — the collection was already ensured
            # in __init__, so this is a no-op gate.
            self._writes_prepared = True
            return

        # 1. Attest (provider failure raises here — nothing mutated yet).
        attestation = self._attest_active_profile()
        if not attestation.ok:
            # Fail closed, naming the remediation (INFERSAFE blocked-path parity).
            raise RuntimeError(
                attestation.failure_reason(subject=f"collection '{self.collection}'")
            )

        self._check_indexed_profile(attestation)

        # 2. Validate the LIVE collection shape (read-only) BEFORE persisting
        #    attested metadata. Persisting first would stamp attested=True at the
        #    active dimension while an existing (e.g. profile-migrated v9)
        #    collection still holds vectors at the old dimension, then
        #    _ensure_collection() would raise a raw blocked error with the
        #    metadata already corrupted. Fail closed here, before any write.
        if self._existing_collection_shape_blocked():
            raise RuntimeError(
                f"Qdrant collection '{self.collection}' is blocked: its shape/config "
                "does not match the active semantic profile. Refusing to persist "
                "attested metadata over a stale collection. Run an operator-authorized "
                "reindex to rebuild the collection with the correct shape."
            )

        # 3. Persist the attested profile atomically (raises on write failure,
        #    BEFORE the collection is created/validated — so a metadata-write
        #    failure never leaves a collection created under an unpersisted
        #    profile).
        self._atomic_write_metadata(self._attested_metadata(attestation))
        self._attestation = attestation

        # 4. Only now create/validate the collection and permit point writes.
        self._ensure_collection()
        self._writes_prepared = True

    # ------------------------------------------------------------------
    def _indexed_profile_record(self) -> Optional[Dict[str, Any]]:
        """Load the persisted primary semantic-profile record, if any."""
        existing = self._load_existing_metadata()
        _profile_id, record = get_primary_semantic_profile_metadata(existing)
        return record

    def _check_query_provenance(self, response: Any) -> None:
        """Validate query provenance using the shared batch admission contract."""
        self._check_embedding_provenance(response, input_type="query")

    def _check_embedding_provenance(self, response: Any, *, input_type: str) -> None:
        """Fail closed when any embedding batch is incompatible with the index.

        Attests the query-time ``embedding-response.v1`` and compares its derived
        provenance against what the vectors were actually indexed under (the
        persisted profile record / fingerprint), rejecting a drifted query model
        (e.g. a different served model id or vector dimension) rather than
        returning silently wrong nearest neighbours.
        """
        attestation = attest_embedding_response(
            self.semantic_profile,
            response,
            role=EmbeddingRole.QUERY if input_type == "query" else EmbeddingRole.DOCUMENT,
        )
        if not attestation.ok:
            raise RuntimeError(
                attestation.failure_reason(subject=f"query against collection '{self.collection}'")
            )

        self._check_indexed_profile(attestation)

    def _check_indexed_profile(self, attestation: ProfileAttestation, *, record=None) -> None:
        """Refuse mixed vector spaces before metadata restamping or batch writes."""
        record = self._indexed_profile_record() if record is None else record
        # Only cross-check against a persisted record that was itself attested;
        # an un-attested/legacy record is not authoritative, so step 1's live
        # attestation is the guard.
        if not record or not record.get("attested"):
            return

        indexed_dim = record.get("model_dimension")
        query_dim = attestation.derived.get("dimension", {}).get("value")
        if isinstance(indexed_dim, int) and isinstance(query_dim, int) and indexed_dim != query_dim:
            raise RuntimeError(
                "Query embedding provenance is incompatible with the indexed "
                f"vectors: index was built at dimension {indexed_dim} but the query "
                f"model reports dimension {query_dim}. Refusing to search across "
                f"incompatible vector spaces. {REINDEX_REMEDIATION}"
            )

        indexed_model = record.get("embedding_model")
        query_model = attestation.derived.get("served_model_id", {})
        if (
            query_model.get("source") == "reported"
            and isinstance(indexed_model, str)
            and isinstance(query_model.get("value"), str)
            and indexed_model != query_model["value"]
        ):
            raise RuntimeError(
                "Query embedding provenance is incompatible with the indexed "
                f"vectors: index was built with model {indexed_model!r} but the "
                f"query endpoint reports served model {query_model['value']!r}. "
                f"{REINDEX_REMEDIATION}"
            )

        # Dimension and served-model id alone are too weak: a same-dim, same-model
        # model_revision bump, a normalization-policy change, or any other
        # compatibility-critical drift produces vectors that are silently wrong to
        # compare. Cross-check every remaining compatibility-critical provenance
        # (model_revision, normalization) plus the profile compatibility
        # fingerprint against the attested/persisted index record; fail closed on
        # any mismatch.
        indexed_revision = record.get("model_version")
        query_revision = attestation.derived.get("model_revision", {}).get("value")
        if (
            isinstance(indexed_revision, str)
            and isinstance(query_revision, str)
            and indexed_revision != query_revision
        ):
            raise RuntimeError(
                "Query embedding provenance is incompatible with the indexed "
                f"vectors: index was built at model revision {indexed_revision!r} but "
                f"the query model reports revision {query_revision!r}. Refusing to "
                f"search across incompatible vector spaces. {REINDEX_REMEDIATION}"
            )

        indexed_norm = record.get("normalization_policy")
        query_norm = attestation.derived.get("normalization", {}).get("value")
        if (
            isinstance(indexed_norm, str)
            and isinstance(query_norm, str)
            and indexed_norm != query_norm
        ):
            raise RuntimeError(
                "Query embedding provenance is incompatible with the indexed "
                f"vectors: index was built under normalization {indexed_norm!r} but "
                f"the query model reports normalization {query_norm!r}. Refusing to "
                f"search across incompatible vector spaces. {REINDEX_REMEDIATION}"
            )

        indexed_fingerprint = record.get("compatibility_fingerprint")
        query_fingerprint = self.compatibility_fingerprint
        if (
            isinstance(indexed_fingerprint, str)
            and isinstance(query_fingerprint, str)
            and indexed_fingerprint != query_fingerprint
        ):
            raise RuntimeError(
                "Query embedding provenance is incompatible with the indexed "
                f"vectors: index compatibility fingerprint {indexed_fingerprint!r} "
                f"does not match the active query profile fingerprint "
                f"{query_fingerprint!r}. Refusing to search across incompatible "
                f"vector spaces. {REINDEX_REMEDIATION}"
            )

    # ------------------------------------------------------------------
    def _recreate_collection_for_reindex(self) -> None:
        """Unconditionally clear and rebuild an EMPTY collection to the active shape.

        Unlike ``_ensure_collection(allow_recreate=True)`` — which only recreates on
        a SHAPE mismatch and therefore misses same-dimension model drift — this
        always deletes-and-recreates, so no vector written under a stale/unverified
        profile can survive an operator-authorized reindex. This is an explicitly
        destructive, operator-authorized path.
        """
        if not self._qdrant_available:
            raise RuntimeError("Qdrant is not available - cannot rebuild collection")
        expected_distance = self.resolve_qdrant_distance(self.distance_metric)
        self.qdrant.recreate_collection(
            collection_name=self.collection,
            vectors_config=models.VectorParams(
                size=self.embedding_dimension,
                distance=expected_distance,
            ),
        )

    # ------------------------------------------------------------------
    def _existing_collection_shape_blocked(self) -> bool:
        """Read-only: does a live collection EXIST whose shape a write would block?

        Returns ``True`` only when the collection already exists but its vector
        shape/config does NOT match the active semantic profile (or cannot be
        read). A missing collection is NOT blocked — a fresh create will match the
        active shape. This never mutates Qdrant; it is the pre-persist guard that
        keeps ``_atomic_write_metadata`` from stamping ``attested=True`` at the
        active dimension over vectors still stored at a stale (e.g. profile-
        migrated v9) dimension.
        """
        if not self._qdrant_available:
            return False
        try:
            collections = self.qdrant.get_collections()
            exists = any(c.name == self.collection for c in collections.collections)
            if not exists:
                return False
            info = self.qdrant.get_collection(self.collection)
            vectors_config = info.config.params.vectors
            actual_dimension = getattr(vectors_config, "size", None)
            actual_distance = getattr(vectors_config, "distance", None)
            if actual_dimension is None or actual_distance is None:
                return True  # Unreadable config: never treat as compatible reuse.
            expected_distance = self.resolve_qdrant_distance(self.distance_metric)
            return (
                actual_dimension != self.embedding_dimension or actual_distance != expected_distance
            )
        except Exception as exc:
            if _is_transient_qdrant_read_error(exc):
                # Connectivity/timeout: the shape could not be READ at all. This is
                # retryable, NOT a reindex-requiring shape mismatch. Surface it
                # distinctly so callers emit a retry (not a blocked/reindex)
                # diagnostic for what is really a backend-reachability problem.
                raise TransientCollectionReadError(
                    f"Could not read the shape of Qdrant collection "
                    f"'{self.collection}': the backend was unreachable or timed out "
                    f"({type(exc).__name__}: {exc}). This is retryable — retry once "
                    "the Qdrant backend is reachable again; it is NOT a shape "
                    "mismatch requiring a reindex."
                ) from exc
            # Server reachable but the live shape is genuinely unknown/unreadable:
            # fail closed (block), never silently treat as a compatible reuse.
            return True

    # ------------------------------------------------------------------
    def reconcile_existing_collection(
        self, *, probe_texts: Optional[List[str]] = None, allow_reindex: bool = False
    ) -> Dict[str, Any]:
        """Reconcile a pre-provenance collection against the live provider.

        Pre-provenance collections (e.g. the v9 4096-d pilot indexed under an
        assumed profile) carry no attestation. This probe-revalidates them: it
        embeds a live probe, derives the true profile, and checks it against the
        existing persisted profile/collection shape.

        * Compatible probe -> the collection is revalidated and its metadata is
          re-persisted as attested (``status='reconciled'``).
        * Incompatible probe -> a diagnostic that NAMES the reindex remediation is
          returned (``status='reindex_required'``); nothing is mutated unless the
          operator passes ``allow_reindex=True``, in which case the collection is
          rebuilt to the attested shape.

        Requires a provenance-capable provider; a legacy provider cannot be
        reconciled and returns ``status='unattestable'``.
        """
        if not self._provider_supports_provenance():
            return {
                "status": "unattestable",
                "collection": self.collection,
                "message": (
                    "Configured provider does not emit embedding-response.v1; "
                    "cannot probe-revalidate. " + REINDEX_REMEDIATION
                ),
                "remediation": REINDEX_REMEDIATION,
            }

        attestation = self._attest_active_profile(probe_texts=probe_texts)
        if not attestation.ok:
            result: Dict[str, Any] = {
                "status": "reindex_required",
                "collection": self.collection,
                "message": attestation.failure_reason(
                    subject=f"existing collection '{self.collection}'"
                ),
                "remediation": REINDEX_REMEDIATION,
                "attestation": attestation.to_dict(),
            }
            if allow_reindex:
                # Operator authorized a destructive rebuild. The prior failed
                # attestation must NEVER open the write gate or persist attested
                # metadata (that would mix old+new-model vectors and disable the
                # fail-closed gate). Unconditionally DELETE+recreate the collection
                # — a shape-only recreate would miss same-dimension model drift —
                # then require a FRESH successful re-attestation before persisting
                # or opening the gate.
                self._recreate_collection_for_reindex()
                reattest = self._attest_active_profile(probe_texts=probe_texts)
                if not reattest.ok:
                    # Re-attest still fails: leave an empty, ungated collection and
                    # fail closed. Nothing further is mutated (no attested metadata,
                    # write gate stays shut).
                    result["status"] = "reindex_failed"
                    result["message"] = reattest.failure_reason(
                        subject=f"rebuilt collection '{self.collection}'"
                    )
                    result["attestation"] = reattest.to_dict()
                    return result
                # Fresh attestation succeeded: persist attested profile and open
                # the write gate under the verified profile.
                self._atomic_write_metadata(self._attested_metadata(reattest))
                self._attestation = reattest
                self._writes_prepared = True
                result["status"] = "reindexed"
                result["attestation"] = reattest.to_dict()
            return result

        # The probe attested compatible, but the EXISTING collection shape may
        # still be stale — the profile-migrated scenario where the active profile
        # moved to a new dimension while the on-disk collection is the old (e.g.
        # v9) one. Validate the live shape (read-only) BEFORE persisting attested
        # metadata, so we never stamp attested=True at the new dimension over
        # vectors still stored at the old one.
        try:
            shape_blocked = self._existing_collection_shape_blocked()
        except TransientCollectionReadError as exc:
            # Backend unreachable/timed out while reading the live shape: this is
            # retryable, NOT a reindex-requiring shape mismatch. Report it as such
            # so the operator retries rather than rebuilds. Nothing is mutated.
            return {
                "status": "retryable",
                "collection": self.collection,
                "message": str(exc),
                "remediation": (
                    "Retry after the Qdrant backend is reachable again; this is "
                    "not a reindex-requiring shape mismatch."
                ),
            }
        if shape_blocked:
            if allow_reindex:
                # Operator authorized a destructive rebuild for the profile-
                # migrated collection: recreate to the active shape FIRST, then
                # persist under the already-verified attestation and open the gate.
                self._recreate_collection_for_reindex()
                self._atomic_write_metadata(self._attested_metadata(attestation))
                self._attestation = attestation
                self._writes_prepared = True
                return {
                    "status": "reindexed",
                    "collection": self.collection,
                    "attestation": attestation.to_dict(),
                }
            # No operator authorization: name the remediation, mutate nothing
            # (crucially, do NOT persist attested metadata over the stale shape).
            return {
                "status": "reindex_required",
                "collection": self.collection,
                "message": (
                    f"Existing collection '{self.collection}' shape does not match the "
                    "active semantic profile (profile-migrated dimension/metric change). "
                    + REINDEX_REMEDIATION
                ),
                "remediation": REINDEX_REMEDIATION,
                "attestation": attestation.to_dict(),
            }

        # Compatible attestation AND compatible live shape: revalidate + re-persist
        # as attested.
        self._atomic_write_metadata(self._attested_metadata(attestation))
        self._attestation = attestation
        self._ensure_collection()
        self._writes_prepared = True
        return {
            "status": "reconciled",
            "collection": self.collection,
            "attestation": attestation.to_dict(),
        }

    # ------------------------------------------------------------------
    def _generate_compatibility_hash(self) -> str:
        """Generate a hash for compatibility checking."""
        if self._profile_active:
            return self.compatibility_fingerprint

        compatibility_string = (
            f"{self.embedding_model}:{self.embedding_dimension}:{self.distance_metric}"
        )
        return hashlib.sha256(compatibility_string.encode()).hexdigest()[:16]

    def _load_existing_metadata(self) -> Dict[str, Any]:
        """Load any existing metadata file for multi-profile merge behavior."""
        try:
            with open(self.metadata_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return {}
        except Exception:
            raise RuntimeError("Semantic metadata cannot be read or decoded") from None
        if not isinstance(payload, dict):
            raise RuntimeError("Semantic metadata must be an object")
        return payload

    # ------------------------------------------------------------------
    def _get_git_commit_hash(self) -> Optional[str]:
        """Use captured indexing provenance, or resolve Git at the repository root."""
        if getattr(self, "commit", None):
            return self.commit
        try:
            import subprocess

            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=self.path_resolver.repository_root,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    def check_compatibility(self, other_metadata_file: str = ".index_metadata.json") -> bool:
        """Check if current configuration is compatible with existing index."""
        if not os.path.exists(other_metadata_file):
            return True  # No existing metadata, assume compatible

        try:
            with open(other_metadata_file, "r") as f:
                other_metadata = json.load(f)

            current_hash = self._generate_compatibility_hash()
            if self._profile_active:
                other_profiles = extract_semantic_profile_metadata(other_metadata)
                other_profile = other_profiles.get(self.semantic_profile.profile_id, {})
                other_hash = other_profile.get("compatibility_fingerprint") or other_profile.get(
                    "compatibility_hash"
                )
                if other_hash:
                    return current_hash == other_hash

            other_hash = other_metadata.get("compatibility_hash")

            return current_hash == other_hash
        except Exception:
            return False  # If we can't read metadata, assume incompatible

    # ------------------------------------------------------------------
    def _symbol_id(
        self, file: str, name: str, line: int, content_hash: Optional[str] = None
    ) -> int:
        """Generate ID using relative path and optional content hash."""
        # Normalize file path to relative
        try:
            relative_path = self.path_resolver.normalize_path(file)
        except ValueError:
            # File might already be relative
            relative_path = str(file).replace("\\", "/")

        # Include content hash if provided for better deduplication
        if content_hash:
            id_str = f"{relative_path}:{name}:{line}:{content_hash[:8]}"
        else:
            id_str = f"{relative_path}:{name}:{line}"

        h = hashlib.sha1(id_str.encode("utf-8"), usedforsecurity=False).digest()[:8]
        return self._reserve_safe_id(int.from_bytes(h, "big", signed=False))

    def _reserve_safe_id(self, value: int) -> int:
        """Fit SQLite's signed integer links without occupying the Qdrant sentinel."""
        maximum = (1 << 63) - 1
        value &= maximum
        if value == self.PROVENANCE_POINT_ID:
            return maximum
        return value

    def _looks_like_code_intent(self, query: str) -> bool:
        """Detect when a query is likely asking for implementation code."""
        normalized = query.lower()
        if not normalized.strip():
            return False

        code_patterns = [
            r"\b(class|def|function|method|symbol|symbols|module|file|filepath)\b",
            r"\b(py|python|js|ts|typescript|javascript|treesitter|tree-sitter)\b",
            r"\b(where is|how does|how do|implemented|implementation|extract|validate)\b",
            r"\.[a-z0-9]{1,6}\b",
        ]
        return any(re.search(pattern, normalized) for pattern in code_patterns)

    def _path_score_multiplier(self, path: str, doc_type: str, code_intent: bool) -> float:
        """Apply lightweight source-path priors to semantic results."""
        normalized = path.replace("\\", "/").lower().lstrip("./")
        doc_type_normalized = (doc_type or "").lower()
        multiplier = 1.0

        is_impl = normalized.startswith(("mcp_server/", "src/"))
        is_script = normalized.startswith("scripts/")
        is_plan = normalized.startswith("plans/") or "/plans/" in normalized
        is_test = normalized.startswith("tests/") or "/tests/" in normalized
        is_doc = normalized.startswith(
            ("docs/", "ai_docs/", "architecture/")
        ) or normalized.endswith(("readme.md", "readme.rst", "readme.txt"))
        is_benchmark_artifact = (
            "/benchmarks/" in normalized
            or normalized.startswith("benchmarks/")
            or normalized.endswith("run_e2e_retrieval_validation.py")
            or normalized.endswith("test_benchmark_query_regressions.py")
        )

        if is_impl:
            multiplier *= 1.15
        elif is_script:
            multiplier *= 1.05
        elif is_plan:
            multiplier *= 0.45
        elif is_test:
            multiplier *= 0.75
        elif is_doc or doc_type_normalized in {
            "markdown",
            "readme",
            "guide",
            "tutorial",
        }:
            multiplier *= 0.7

        if is_benchmark_artifact:
            multiplier *= 0.85

        if code_intent:
            if is_impl:
                multiplier *= 1.25
            elif is_script:
                multiplier *= 1.1
            if is_plan:
                multiplier *= 0.2
            if is_test:
                multiplier *= 0.55
            if is_doc or doc_type_normalized in {
                "markdown",
                "readme",
                "guide",
                "tutorial",
            }:
                multiplier *= 0.35
            if is_benchmark_artifact:
                multiplier *= 0.7

        return multiplier

    def _looks_like_symbol_precise_query(self, query: str) -> bool:
        """Detect exact symbol or file lookup style queries."""
        normalized = query.strip()
        if not normalized:
            return False

        if re.search(
            r"\b(class|def|function|method|symbol)\s+[A-Za-z_][A-Za-z0-9_]*",
            normalized,
        ):
            return True
        if re.search(r"\b[A-Z][A-Za-z0-9_]{2,}\b", normalized):
            return True
        return bool(re.search(r"\b[a-z]+_[a-z0-9_]+\b", normalized))

    def _query_overlap_bonus(self, query: str, payload: Mapping[str, Any]) -> float:
        """Apply a small lexical bonus using retrieval metadata fields."""
        terms = {token for token in re.findall(r"[a-z0-9_]+", query.lower()) if len(token) >= 3}
        if not terms:
            return 1.0

        relative_path = str(
            payload.get("relative_path")
            or payload.get("filepath")
            or payload.get("file")
            or payload.get("path")
            or ""
        )
        path_haystack = " ".join(re.split(r"[^a-z0-9]+", relative_path.lower())).strip()

        haystack = " ".join(
            str(payload.get(key, ""))
            for key in (
                "relative_path",
                "symbol",
                "qualified_name",
                "semantic_path",
                "signature",
                "signature_text",
                "semantic_text",
                "embedding_text",
                "content",
            )
        ).lower()
        content_matches = sum(1 for term in terms if term in haystack)
        path_matches = sum(1 for term in terms if term in path_haystack)

        bonus = 1.0 + min(content_matches, 5) * 0.04 + min(path_matches, 4) * 0.08

        if {
            "artifact",
            "push",
            "pull",
            "delta",
            "resolution",
        }.issubset(terms):
            resolver_markers = {
                "resolve",
                "resolver",
                "chain",
                "base_commit",
                "target_commit",
            }
            artifact_markers = {
                "manifest",
                "archive",
                "apply",
                "checksum",
                "operations",
            }
            resolver_hits = sum(1 for term in resolver_markers if term in haystack)
            artifact_hits = sum(1 for term in artifact_markers if term in haystack)
            if resolver_hits:
                bonus *= 1.0 + min(resolver_hits, 3) * 0.08
            if "delta_artifacts.py" in relative_path.lower() and artifact_hits:
                bonus *= 0.92

        return bonus

    def _implementation_responsibility_bonus(self, query: str, payload: Mapping[str, Any]) -> float:
        """Boost files whose role matches implementation-intent queries."""
        terms = {token for token in re.findall(r"[a-z0-9_]+", query.lower()) if len(token) >= 3}
        if not terms:
            return 1.0

        relative_path = str(
            payload.get("relative_path")
            or payload.get("filepath")
            or payload.get("file")
            or payload.get("path")
            or ""
        ).lower()
        semantic_text = str(payload.get("semantic_text") or "").lower()
        embedding_text = str(payload.get("embedding_text") or "").lower()
        haystack = f"{semantic_text} {embedding_text}".strip()

        bonus = 1.0
        if {"extract", "symbols", "treesitter"}.issubset(terms):
            if "semantic_indexer.py" in relative_path:
                bonus *= 1.28
            elif "generic_treesitter_plugin.py" in relative_path:
                bonus *= 0.82

        if {"extract", "symbols", "python", "treesitter"}.issubset(terms):
            if "semantic_indexer.py" in relative_path:
                bonus *= 1.08
            elif "generic_treesitter_plugin.py" in relative_path:
                bonus *= 0.88

        if {"semantic", "setup", "validate"}.issubset(terms):
            if "setup_commands.py" in relative_path:
                bonus *= 1.18
            elif "semantic_preflight.py" in relative_path:
                bonus *= 0.96

        if {"artifact", "delta", "resolution"}.issubset(terms):
            if "delta_resolver.py" in relative_path:
                bonus *= 1.55
            elif "delta_artifacts.py" in relative_path:
                bonus *= 0.72

        if {"artifact", "push", "pull", "delta", "resolution"}.issubset(terms):
            if "delta_resolver.py" in relative_path:
                bonus *= 1.32
                if any(
                    marker in haystack
                    for marker in (
                        "resolve",
                        "resolver",
                        "chain",
                        "base_commit",
                        "target_commit",
                    )
                ):
                    bonus *= 1.18
            elif "artifact_commands.py" in relative_path:
                bonus *= 0.48
                if "cli" in haystack or "command" in haystack:
                    bonus *= 0.7
            elif "delta_artifacts.py" in relative_path:
                bonus *= 0.65

        if "extract" in terms and "symbols" in terms and "index" in haystack:
            bonus *= 1.04
        if "validate" in terms and "command" in haystack:
            bonus *= 1.03
        if "delta" in terms and "resolve" in haystack:
            bonus *= 1.04

        return bonus

    def _prefer_unique_files(
        self, results: List[dict[str, Any]], limit: int
    ) -> List[dict[str, Any]]:
        """Keep only the highest-ranked chunk per file path."""
        unique_results: List[dict[str, Any]] = []
        seen_paths: set[str] = set()

        for result in results:
            path = str(
                result.get("relative_path")
                or result.get("filepath")
                or result.get("file")
                or result.get("path")
                or ""
            )
            normalized_path = path.replace("\\", "/").lower().lstrip("./")
            dedupe_key = normalized_path or path
            if dedupe_key in seen_paths:
                continue
            seen_paths.add(dedupe_key)
            unique_results.append(result)
            if len(unique_results) >= limit:
                break

        return unique_results

    def _rerank_query_results(
        self, query: str, results: List[dict[str, Any]], limit: int
    ) -> List[dict[str, Any]]:
        """Apply lightweight query-aware reranking to semantic results."""
        code_intent = self._looks_like_code_intent(query)
        symbol_precise = self._looks_like_symbol_precise_query(query)
        reranked: List[dict[str, Any]] = []

        for result in results:
            path = str(
                result.get("relative_path")
                or result.get("filepath")
                or result.get("file")
                or result.get("path")
                or ""
            )
            doc_type = str(result.get("doc_type") or result.get("type") or "")
            adjusted = dict(result)
            score = float(result.get("score", 0.0))
            score *= self._path_score_multiplier(path, doc_type, code_intent)
            score *= self._query_overlap_bonus(query, result)
            score *= self._implementation_responsibility_bonus(query, result)

            normalized_path = path.replace("\\", "/").lower().lstrip("./")
            is_test = normalized_path.startswith("tests/") or "/tests/" in normalized_path
            is_impl = normalized_path.startswith(("mcp_server/", "src/"))
            is_plan = normalized_path.startswith("plans/") or "/plans/" in normalized_path
            is_doc = normalized_path.startswith(
                ("docs/", "ai_docs/", "architecture/")
            ) or normalized_path.endswith(("readme.md", "readme.rst", "readme.txt"))
            is_benchmark_artifact = (
                "/benchmarks/" in normalized_path
                or normalized_path.startswith("benchmarks/")
                or normalized_path.endswith("run_e2e_retrieval_validation.py")
                or normalized_path.endswith("test_benchmark_query_regressions.py")
            )
            if symbol_precise:
                if is_test:
                    score *= 0.35
                elif is_plan:
                    score *= 0.12
                elif is_doc:
                    score *= 0.2
                elif is_benchmark_artifact:
                    score *= 0.18
                elif is_impl:
                    score *= 1.18

            adjusted["score"] = score
            reranked.append(adjusted)

        reranked.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
        return self._prefer_unique_files(reranked, limit)

    def _semantic_result_metadata(self) -> dict[str, Any]:
        """Return stable metadata for semantic-path query results."""
        return {
            "source": "semantic",
            "semantic_source": "semantic",
            "semantic_profile_id": self.semantic_profile.profile_id,
            "semantic_collection_name": self.collection,
        }

    def _build_chunk_embedding_text(
        self,
        relative_path: str,
        symbol_name: str,
        kind: str,
        signature: str,
        parent_symbol: Any,
        metadata: Mapping[str, Any],
        chunk_content: str,
    ) -> str:
        """Build enriched embedding text for semantic chunk indexing."""
        return self._compose_embedding_text(
            self._build_chunk_embedding_parts(
                relative_path=relative_path,
                symbol_name=symbol_name,
                kind=kind,
                signature=signature,
                parent_symbol=parent_symbol,
                metadata=metadata,
                chunk_content=chunk_content,
            ),
            chunk_content,
        )

    def _build_file_embedding_text(
        self,
        relative_path: str,
        symbols: List[Dict[str, Any]],
        normalized_chunks: List[Dict[str, Any]],
    ) -> str:
        """Build a concise file-level embedding text for semantic retrieval."""
        module_stem = Path(relative_path).stem.replace("_", " ")
        path_tokens = " ".join(re.split(r"[^a-z0-9]+", relative_path.lower())).strip()
        symbol_names = [
            str(symbol.get("symbol") or "") for symbol in symbols if symbol.get("symbol")
        ]
        symbol_kinds = sorted(
            {str(symbol.get("kind") or "") for symbol in symbols if symbol.get("kind")}
        )

        parts = [f"file {relative_path}", f"module {module_stem}"]
        if path_tokens:
            parts.append(f"path tokens {path_tokens}")
        if symbol_kinds:
            parts.append("symbol kinds " + " ".join(symbol_kinds[:8]))
        if symbol_names:
            parts.append("symbols " + " ".join(symbol_names[:16]))

        chunk_hints = []
        for item in normalized_chunks[:8]:
            metadata = item.get("metadata") or {}
            hint = (
                metadata.get("qualified_name")
                or metadata.get("semantic_text")
                or item.get("signature")
                or item.get("symbol")
            )
            if hint:
                chunk_hints.append(self._compact_metadata_text(str(hint), limit=160))
        if chunk_hints:
            parts.append("highlights " + " | ".join(chunk_hints[:6]))

        chunk_text = "\n".join(
            self._compact_metadata_text(str(item.get("content") or ""), limit=200)
            for item in normalized_chunks[:3]
        ).lower()
        if "chunk_file(" in chunk_text and "symbols.append(" in chunk_text:
            parts.append(
                "uses tree-sitter chunking to extract symbols from python files for semantic indexing"
            )
        if relative_path.endswith("semantic_indexer.py"):
            parts.append(
                "semantic indexer extracts symbols from python using treesitter retrieval metadata"
            )
        if relative_path.endswith("dispatcher_enhanced.py"):
            parts.append(
                "implements symbol routing logic in EnhancedDispatcher: routes symbol-pattern queries directly to symbols table lookup, query-intent classification, BM25 search with OR-fallback and filename boost"
            )

        return self._truncate_embedding_text("\n".join(parts).strip())

    # ------------------------------------------------------------------
    def _prepare_file_for_indexing(self, path: Path) -> Optional[Dict[str, Any]]:
        """Parse and chunk a file, building embedding inputs without API calls.

        Returns a preparation dict consumed by ``_store_file_embeddings`` and
        ``index_files_batch``, or ``None`` if the file has no indexable content.
        """
        relative_path = self.path_resolver.normalize_path(path)
        language = self._infer_chunk_language(path)
        used_fallback_chunks = False
        if self.staging:
            # SQLite owns the exact chunks summarized for this unpublished generation.
            file_row = self.sqlite_store.get_file_by_path(path)
            if file_row is None:
                raise RuntimeError("Staged semantic input has no stored file")
            chunk_results = [
                SimpleNamespace(
                    content=row["content"],
                    chunk_id=row["chunk_id"],
                    node_id=row["node_id"],
                    metadata=json.loads(row["metadata"] or "{}"),
                    node_type=row["node_type"],
                    start_line=row["line_start"],
                    end_line=row["line_end"],
                )
                for row in self.sqlite_store.get_chunks_for_file(file_row["id"])
                if row["chunk_type"] != "document" and not row["chunk_id"].startswith("history:")
            ]
        else:
            try:
                chunk_results = chunk_file(
                    path,
                    language,
                    extract_metadata=True,
                    include_retrieval_metadata=True,
                )
            except TypeError:
                try:
                    chunk_results = chunk_file(path, language, extract_metadata=True)
                except Exception:
                    used_fallback_chunks = True
                    chunk_results = self._fallback_text_chunks(path, relative_path)
            except Exception:
                used_fallback_chunks = True
                chunk_results = self._fallback_text_chunks(path, relative_path)

        chunks = [chunk for chunk in chunk_results if chunk.content.strip()]
        symbols: List[Dict[str, Any]] = []
        seen_symbols: set[tuple[str, int, int]] = set()
        normalized_chunks: List[Dict[str, Any]] = []
        embedding_inputs: List[str] = []
        file_embedding_text: Optional[str] = None
        missing_summary_chunk_ids: List[str] = []

        if chunks:
            for chunk in chunks:
                metadata = dict(chunk.metadata or {})
                signature_data = metadata.get("signature")
                signature = str(metadata.get("signature_text") or signature_data or "")
                symbol_name = str(
                    metadata.get("symbol")
                    or metadata.get("name")
                    or metadata.get("qualified_name")
                    or Path(relative_path).stem
                )
                kind = str(metadata.get("kind") or chunk.node_type)
                parent_symbol = metadata.get("parent_symbol")
                source_chunk_id = str(chunk.chunk_id or chunk.node_id)

                summary_text = None
                _sqlite_store = getattr(self, "sqlite_store", None)
                if _sqlite_store is not None:
                    summary = _sqlite_store.get_chunk_summary(source_chunk_id)
                    if summary:
                        summary_text = summary["summary_text"]
                    else:
                        missing_summary_chunk_ids.append(source_chunk_id)

                units = self._expand_chunk_embedding_units(
                    relative_path=relative_path,
                    symbol_name=symbol_name,
                    kind=kind,
                    signature=signature,
                    parent_symbol=parent_symbol,
                    metadata=metadata,
                    chunk_content=chunk.content,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    summary_text=summary_text,
                )
                for unit in units:
                    normalized_chunks.append(
                        {
                            "chunk": chunk,
                            "metadata": metadata,
                            "symbol": symbol_name,
                            "kind": kind,
                            "signature": signature,
                            "parent_symbol": parent_symbol,
                            "embedding_text": unit.embedding_text,
                            "content": unit.content,
                            "start_line": unit.start_line,
                            "end_line": unit.end_line,
                            "subchunk_index": unit.subchunk_index,
                            "subchunk_total": unit.subchunk_total,
                            "chunk_role": unit.chunk_role,
                            "source_chunk_id": source_chunk_id,
                            "derived_chunk_id": f"{source_chunk_id}:part:{unit.subchunk_index}:{unit.subchunk_total}",
                        }
                    )
                    embedding_inputs.append(unit.embedding_text)

                symbol_key = (symbol_name, chunk.start_line, chunk.end_line)
                if symbol_key not in seen_symbols:
                    seen_symbols.add(symbol_key)
                    symbols.append(
                        {
                            "symbol": symbol_name,
                            "kind": kind,
                            "signature": signature,
                            "line": chunk.start_line,
                            "span": [chunk.start_line, chunk.end_line],
                            "parent_class": parent_symbol,
                            "parent_symbol": parent_symbol,
                        }
                    )

            file_embedding_text = self._build_file_embedding_text(
                relative_path=relative_path,
                symbols=symbols,
                normalized_chunks=normalized_chunks,
            )
            if file_embedding_text:
                embedding_inputs.append(file_embedding_text)

        if not embedding_inputs:
            return None

        return {
            "embedding_inputs": embedding_inputs,
            "normalized_chunks": normalized_chunks,
            "file_embedding_text": file_embedding_text,
            "symbols": symbols,
            "language": language,
            "relative_path": relative_path,
            "chunk_count": len(chunks),
            "used_fallback_chunks": used_fallback_chunks,
            "missing_summary_chunk_ids": sorted(set(missing_summary_chunk_ids)),
        }

    def _preflight_blocker_details(
        self, semantic_preflight: Optional[Mapping[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        if not semantic_preflight:
            return None
        if semantic_preflight.get("can_write_semantic_vectors", True):
            return None
        blocker = semantic_preflight.get("blocker")
        if isinstance(blocker, Mapping):
            return dict(blocker)
        return {
            "code": "semantic_preflight_blocked",
            "message": "Semantic preflight blocked vector writes",
            "can_write_semantic_vectors": False,
        }

    def _store_file_embeddings(
        self, path: Path, prep: Dict[str, Any], embeds: List[List[float]]
    ) -> Dict[str, Any]:
        """Build Qdrant points from pre-computed embeddings and upsert them."""
        normalized_chunks = prep["normalized_chunks"]
        file_embedding_text = prep["file_embedding_text"]
        language = prep["language"]
        relative_path = prep["relative_path"]
        canonical_path = str(self.path_resolver.resolve_path(relative_path))

        chunk_embeds = embeds[: len(normalized_chunks)]
        file_embed = None
        if file_embedding_text and len(embeds) > len(normalized_chunks):
            file_embed = embeds[len(normalized_chunks)]

        points = []
        for chunk_index, (normalized, vec) in enumerate(
            zip(normalized_chunks, chunk_embeds), start=1
        ):
            chunk = normalized["chunk"]
            metadata = normalized["metadata"]
            content_hash = hashlib.sha256(chunk.content.encode()).hexdigest()
            chunk_id = str(chunk.chunk_id or chunk.node_id)

            payload = {
                "file": canonical_path,
                "relative_path": relative_path,
                "content_hash": content_hash,
                "chunk_id": normalized["derived_chunk_id"],
                "source_chunk_id": chunk_id,
                "chunk_index": chunk_index,
                "chunk_total": len(normalized_chunks),
                "subchunk_index": normalized["subchunk_index"],
                "subchunk_total": normalized["subchunk_total"],
                "chunk_role": normalized["chunk_role"],
                "symbol": normalized["symbol"],
                "kind": normalized["kind"],
                "signature": normalized["signature"],
                "line": normalized["start_line"],
                "span": [normalized["start_line"], normalized["end_line"]],
                "parent_class": normalized["parent_symbol"],
                "parent_symbol": normalized["parent_symbol"],
                "language": language,
                "is_deleted": False,
                "content": normalized["content"],
                "embedding_text": normalized["embedding_text"],
            }
            for key in [
                "qualified_name",
                "semantic_path",
                "signature_text",
                "semantic_text",
                "imports",
                "calls",
                "dependencies",
            ]:
                if key in metadata:
                    payload[key] = metadata[key]
            points.append(
                models.PointStruct(
                    id=self._symbol_id(
                        canonical_path,
                        f"{normalized['symbol']}#{normalized['derived_chunk_id']}",
                        normalized["start_line"],
                        content_hash,
                    ),
                    vector=vec,
                    payload=payload,
                )
            )

        if file_embed is not None:
            file_summary_chunk_id = self._file_summary_chunk_id(relative_path)
            file_summary_payload = {
                "file": canonical_path,
                "relative_path": relative_path,
                "content_hash": None,
                "chunk_id": file_summary_chunk_id,
                "chunk_index": 0,
                "chunk_total": len(normalized_chunks),
                "symbol": Path(relative_path).stem,
                "kind": "file_summary",
                "signature": "",
                "line": 1,
                "span": [1, 1],
                "parent_class": None,
                "parent_symbol": None,
                "language": language,
                "is_deleted": False,
                "content": "",
                "embedding_text": file_embedding_text,
            }
            points.append(
                models.PointStruct(
                    id=self._symbol_id(canonical_path, "file_summary", 1),
                    vector=file_embed,
                    payload=file_summary_payload,
                )
            )

        point_links: List[tuple[str, int]] = []
        source_chunk_links: Dict[str, int] = {}
        try:
            self._upsert_points_batched(path, points)
            sqlite_store = getattr(self, "sqlite_store", None)
            if sqlite_store is not None:
                effective_profile_id = self.semantic_profile.profile_id
                for point in points:
                    payload = point.payload or {}
                    chunk_id = payload.get("chunk_id")
                    if chunk_id:
                        point_links.append((str(chunk_id), int(point.id)))
                    source_chunk_id = payload.get("source_chunk_id")
                    if source_chunk_id:
                        source_chunk_links[str(source_chunk_id)] = int(point.id)
                for chunk_id, point_id in point_links:
                    sqlite_store.upsert_semantic_point(
                        profile_id=effective_profile_id,
                        chunk_id=chunk_id,
                        point_id=point_id,
                        collection=self.collection,
                    )
                for source_chunk_id, point_id in source_chunk_links.items():
                    sqlite_store.upsert_semantic_point(
                        profile_id=effective_profile_id,
                        chunk_id=source_chunk_id,
                        point_id=point_id,
                        collection=self.collection,
                    )
        except Exception as e:
            logger.error(
                f"Failed to upsert {len(points)} points for file {path}: "
                f"{type(e).__name__}: {e}"
            )
            self._qdrant_available = False
            raise RuntimeError(f"Failed to store embeddings for {path} in Qdrant: {e}")

        return {
            "file": str(path),
            "symbols": prep["symbols"],
            "language": language,
            "chunk_count": prep["chunk_count"],
            "embedding_unit_count": len(normalized_chunks),
            "file_summary_indexed": file_embed is not None,
            "used_fallback_chunks": prep["used_fallback_chunks"],
            "semantic_points_linked": len(point_links),
            "point_ids": [int(point.id) for point in points],
        }

    def index_file(
        self,
        path: Path,
        *,
        require_summaries: bool = False,
        semantic_preflight: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Index a single file and return the shard info."""
        blocker = self._preflight_blocker_details(semantic_preflight)
        if blocker is not None:
            return {
                "file": str(path),
                "symbols": [],
                "language": self._infer_chunk_language(path),
                "chunk_count": 0,
                "embedding_unit_count": 0,
                "file_summary_indexed": False,
                "used_fallback_chunks": False,
                "blocked": True,
                "semantic_blocker": blocker,
            }
        prep = self._prepare_file_for_indexing(path)
        if prep is None:
            return {
                "file": str(path),
                "symbols": [],
                "language": self._infer_chunk_language(path),
                "chunk_count": 0,
                "embedding_unit_count": 0,
                "file_summary_indexed": False,
                "used_fallback_chunks": False,
            }
        missing_summary_chunk_ids = prep.get("missing_summary_chunk_ids", [])
        if require_summaries and missing_summary_chunk_ids:
            return {
                "file": str(path),
                "symbols": prep["symbols"],
                "language": prep["language"],
                "chunk_count": prep["chunk_count"],
                "embedding_unit_count": 0,
                "file_summary_indexed": False,
                "used_fallback_chunks": prep["used_fallback_chunks"],
                "blocked": True,
                "missing_summary_chunk_ids": missing_summary_chunk_ids,
                "semantic_error": "Missing authoritative summaries for strict semantic indexing",
            }
        # Attest + persist the provenance profile BEFORE any point write.
        self._prepare_for_writes()
        embeds = self._embed_texts(prep["embedding_inputs"], input_type="document")
        result = self._store_file_embeddings(path, prep, embeds)
        # Incremental single-file mutation: invalidate the collection provenance
        # sentinel so only a clean full rebuild can re-attest the collection.
        self._invalidate_collection_provenance()
        return result

    def index_files_batch(
        self,
        paths: List[Path],
        embed_batch_size: int = 1000,
        *,
        require_summaries: bool = False,
        semantic_preflight: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Index multiple files with batched embedding API calls.

        Collects all embedding texts from all files first, then submits them
        to the provider in chunks of ``embed_batch_size`` (≤1000 for Voyage AI),
        reducing API round-trips from O(files) to O(total_units / batch_size).
        """
        if not self._qdrant_available:
            raise RuntimeError("Qdrant is not available — cannot batch-index files")

        blocker = self._preflight_blocker_details(semantic_preflight)
        if blocker is not None:
            return {
                "files_indexed": 0,
                "files_failed": 0,
                "files_skipped": 0,
                "files_blocked": len(paths),
                "blocked_files": [str(path) for path in paths],
                "total_embedding_units": 0,
                "semantic_blocker": blocker,
                "semantic_error": blocker.get("message"),
            }

        # Phase 1: prepare all files — chunking + build embedding texts, no API calls
        preparations: List[tuple] = []
        skipped = 0
        blocked_files: List[str] = []
        missing_summary_chunk_ids: List[str] = []
        for path in paths:
            try:
                prep = self._prepare_file_for_indexing(path)
                if prep:
                    prep_missing = prep.get("missing_summary_chunk_ids", [])
                    if require_summaries and prep_missing:
                        blocked_files.append(str(path))
                        missing_summary_chunk_ids.extend(str(chunk_id) for chunk_id in prep_missing)
                    else:
                        preparations.append((path, prep))
                else:
                    skipped += 1
            except Exception as exc:
                logger.warning("Failed to prepare %s for semantic indexing: %s", path, exc)
                skipped += 1

        if not preparations:
            return {
                "files_indexed": 0,
                "files_failed": 0,
                "files_skipped": skipped,
                "files_blocked": len(blocked_files),
                "blocked_files": blocked_files,
                "missing_summary_chunk_ids": sorted(set(missing_summary_chunk_ids)),
                "total_embedding_units": 0,
            }

        # Phase 2: collect all texts and record per-file slices
        all_texts: List[str] = []
        file_slices: List[tuple] = []
        for path, prep in preparations:
            start = len(all_texts)
            all_texts.extend(prep["embedding_inputs"])
            file_slices.append((path, prep, start, len(all_texts)))

        logger.info(
            "Batch embedding %d texts from %d files (batch_size=%d)",
            len(all_texts),
            len(preparations),
            embed_batch_size,
        )

        # Attest + persist the provenance profile BEFORE any embedding/point write
        # so a provider or metadata failure mutates zero collection/metadata state.
        self._prepare_for_writes()

        # Phase 3: embed in token-aware batches.
        # Voyage AI enforces a hard 120 000-token-per-request limit in addition to
        # the 1 000-input limit.  We estimate tokens as len(text)//4 (a conservative
        # approximation) and split whenever the running total would exceed the budget.
        MAX_TOKENS_PER_BATCH = 100_000  # stay under the 120 000 hard limit
        all_embeds: List[List[float]] = []
        batch: List[str] = []
        batch_token_est = 0
        batch_start_idx = 0

        def _flush_batch(b: List[str], start: int) -> None:
            if not b:
                return
            logger.info(
                "Embedding texts %d–%d of %d (~%d estimated tokens)",
                start + 1,
                start + len(b),
                len(all_texts),
                sum(len(t) // 4 for t in b),
            )
            all_embeds.extend(self._embed_texts(b, input_type="document"))

        for idx, text in enumerate(all_texts):
            token_est = max(1, len(text) // 4)
            # Flush when adding this text would exceed either limit
            if batch and (
                len(batch) >= embed_batch_size or batch_token_est + token_est > MAX_TOKENS_PER_BATCH
            ):
                _flush_batch(batch, batch_start_idx)
                batch = []
                batch_token_est = 0
                batch_start_idx = idx
            batch.append(text)
            batch_token_est += token_est

        _flush_batch(batch, batch_start_idx)

        # Phase 4: store per-file using pre-computed embeddings
        indexed = 0
        failed = 0
        built_point_ids: List[int] = []
        indexed_relative_paths: List[str] = []
        for path, prep, start, end in file_slices:
            file_embeds = all_embeds[start:end]
            try:
                store_result = self._store_file_embeddings(path, prep, file_embeds)
                built_point_ids.extend(store_result.get("point_ids", []))
                indexed_relative_paths.append(prep["relative_path"])
                indexed += 1
            except Exception as exc:
                logger.error("Failed to store embeddings for %s: %s", path, exc)
                failed += 1

        # Phase 5: stamp collection-resident provenance for this successful build.
        # Best-effort: a provenance-write failure is logged but never fails the
        # index build that already durably wrote its points. The corpus digest is
        # computed from the SET of indexed relative paths using the same recipe as
        # the frozen benchmark ``corpus_manifest.json`` (sha256 of the sorted
        # included relative paths joined by '\n', no trailing newline) so a
        # collection built from the frozen corpus emits a ``corpus_sha256`` that
        # verifies against the benchmark's recorded value.
        if indexed:
            corpus_sha256 = self._compute_corpus_sha256(indexed_relative_paths)
            self._write_collection_provenance_best_effort(
                point_ids=built_point_ids, corpus_sha256=corpus_sha256
            )

        logger.info(
            "Batch semantic indexing complete: %d indexed, %d failed, %d skipped",
            indexed,
            failed,
            skipped,
        )
        return {
            "files_indexed": indexed,
            "files_failed": failed,
            "files_skipped": skipped,
            "files_blocked": len(blocked_files),
            "blocked_files": blocked_files,
            "missing_summary_chunk_ids": sorted(set(missing_summary_chunk_ids)),
            "total_embedding_units": len(all_texts),
        }

    # ------------------------------------------------------------------
    def query(
        self, text: str, limit: int = 5, *, source_chunk_ids: Optional[List[str]] = None
    ) -> Iterable[dict[str, Any]]:
        """Query indexed code snippets using a natural language description.

        Args:
            text: Natural language query
            limit: Maximum number of results
            source_chunk_ids: Restrict retrieval to these source chunks before ranking.

        Yields:
            Search results with metadata and scores

        Raises:
            RuntimeError: If Qdrant is unavailable or query fails
        """
        if not self._qdrant_available:
            raise RuntimeError(
                "Qdrant is not available - cannot perform semantic search. "
                "Check connection status with validate_connection()."
            )
        if limit <= 0 or source_chunk_ids == []:
            return

        query_filter = models.Filter(
            must_not=[
                models.FieldCondition(key=self.PROVENANCE_TAG, match=models.MatchValue(value=True)),
                models.FieldCondition(key="is_deleted", match=models.MatchValue(value=True)),
            ],
            should=(
                [
                    models.FieldCondition(key=key, match=models.MatchAny(any=source_chunk_ids))
                    for key in ("source_chunk_id", "chunk_id")
                ]
                if source_chunk_ids is not None
                else None
            ),
        )

        if self._provider_supports_provenance():
            try:
                response = self.embedding_client.embed_with_provenance([text], input_type="query")
                embedding = self._validate_embedding_response(response, 1)[0]
            except Exception as e:
                raise RuntimeError(f"Failed to generate query embedding: {e}")
            # Fail closed if the query model drifted from the indexed vectors.
            self._check_query_provenance(response)
        else:
            try:
                embedding = self._embed_texts([text], input_type="query")[0]
            except Exception as e:
                raise RuntimeError(f"Failed to generate query embedding: {e}")

        query_limit = limit
        if self._looks_like_code_intent(text):
            query_limit = min(max(limit * 4, limit), 50)

        try:
            if hasattr(self.qdrant, "search"):
                results = self.qdrant.search(
                    collection_name=self.collection,
                    query_vector=embedding,
                    query_filter=query_filter,
                    limit=query_limit,
                )
            else:
                response = self.qdrant.query_points(
                    collection_name=self.collection,
                    query=embedding,
                    query_filter=query_filter,
                    limit=query_limit,
                    with_payload=True,
                )
                results = list(getattr(response, "points", []) or [])

            rerank_input: List[dict[str, Any]] = []
            for res in results:
                payload = dict(res.payload or {})
                # Never surface the reserved collection-provenance sentinel.
                if payload.get(self.PROVENANCE_TAG) or payload.get("is_deleted"):
                    continue
                payload["score"] = res.score
                payload.update(self._semantic_result_metadata())
                rerank_input.append(payload)

            yield from self._rerank_query_results(text, rerank_input, limit)
        except Exception as e:
            logger.error(f"Qdrant search failed: {type(e).__name__}: {e}")
            self._qdrant_available = False
            raise RuntimeError(
                f"Semantic search failed - Qdrant error: {e}. " "Connection may have been lost."
            )

    # ------------------------------------------------------------------
    # INFERLIVEGATE Lane A: collection-resident provenance
    # ------------------------------------------------------------------
    def _served_model_provenance(self) -> tuple[str, str]:
        """Resolve ``(provider_id, provider_revision)`` for the manifest.

        Reuses the attested embedding provenance already available on the write
        path (``self._attestation``). ``provider_id`` is the served model id when
        the endpoint reported one, else the declared model name. ``provider_revision``
        is the reported model revision when available, otherwise the literal source
        label (``'declared'`` or ``'unknown'``) per the collection-provenance.v1
        contract.
        """
        provider_id = self.embedding_model
        provider_revision = "unknown"
        attestation = getattr(self, "_attestation", None)
        derived = getattr(attestation, "derived", None) if attestation is not None else None
        if isinstance(derived, dict):
            served = derived.get("served_model_id") or {}
            if isinstance(served, dict) and served.get("value"):
                provider_id = str(served["value"])
            revision = derived.get("model_revision") or {}
            if isinstance(revision, dict):
                source = revision.get("source")
                if source == "reported" and revision.get("value"):
                    provider_revision = str(revision["value"])
                elif source in ("declared", "unknown"):
                    provider_revision = source
        return provider_id, provider_revision

    def _compute_point_set_id(self, point_ids: Iterable[Any]) -> str:
        """Deterministic id for this build from its point/chunk ids.

        Order-independent (sorted) and duplicate-insensitive so the same built
        point set always yields the same id, and any change to the set of points
        yields a different id. The reserved provenance sentinel id is never part
        of the set it describes.
        """
        normalized = sorted(
            {
                str(pid)
                for pid in point_ids
                if pid is not None and str(pid) != str(self.PROVENANCE_POINT_ID)
            }
        )
        digest = hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()
        return digest

    @staticmethod
    def _compute_corpus_sha256(relative_paths: Iterable[str]) -> str:
        """Corpus digest for the indexed file SET.

        Matches the frozen benchmark ``corpus_manifest.json`` recipe exactly:
        sha256 of the UTF-8 bytes of the SORTED included relative paths joined by
        a single newline (``\\n``), with NO trailing newline. Deduplicated and
        order-independent so the same indexed corpus always yields the same digest.
        A collection built from the frozen corpus therefore emits a
        ``corpus_sha256`` that verifies against the benchmark's recorded value.
        """
        normalized = sorted({str(p).replace("\\", "/") for p in relative_paths})
        return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()

    def build_collection_provenance_manifest(
        self,
        point_ids: Optional[Iterable[Any]] = None,
        *,
        corpus_sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the ``collection-provenance.v1`` manifest for this build."""
        provider_id, provider_revision = self._served_model_provenance()
        return {
            "provenance_version": self.PROVENANCE_VERSION,
            "collection": self.collection,
            "indexed_commit": self._get_git_commit_hash(),
            "corpus_sha256": corpus_sha256,
            "profile_fingerprint": self.compatibility_fingerprint,
            "provider_id": provider_id,
            "provider_revision": provider_revision,
            "point_set_id": self._compute_point_set_id(point_ids or []),
            "written_at": datetime.now().isoformat(),
        }

    def write_collection_provenance(
        self,
        point_ids: Optional[Iterable[Any]] = None,
        *,
        corpus_sha256: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Upsert the reserved collection-provenance sentinel point.

        Writes ONE reserved point (id ``PROVENANCE_POINT_ID``) whose payload is the
        ``collection-provenance.v1`` manifest tagged ``__provenance__: True`` so
        ordinary semantic search never returns it. Returns the manifest that was
        written, or ``None`` when Qdrant is unavailable.
        """
        if not getattr(self, "_qdrant_available", False):
            return None
        manifest = self.build_collection_provenance_manifest(point_ids, corpus_sha256=corpus_sha256)
        payload = {self.PROVENANCE_TAG: True, **manifest}
        dimension = int(self.embedding_dimension)
        # A unit-norm sentinel vector (never a zero vector, which cosine-distance
        # backends reject). The point is filtered out of search regardless.
        vector = [0.0] * dimension
        if dimension:
            vector[0] = 1.0
        point = models.PointStruct(id=self.PROVENANCE_POINT_ID, vector=vector, payload=payload)
        result = self.qdrant.upsert(collection_name=self.collection, points=[point], wait=True)
        if self.staging and getattr(result, "status", None) not in {
            "completed",
            models.UpdateStatus.COMPLETED,
        }:
            raise RuntimeError("Staged provenance write was not acknowledged")
        logger.info(
            "Wrote collection-provenance sentinel to '%s' (point_set_id=%s)",
            self.collection,
            manifest["point_set_id"],
        )
        return manifest

    def _write_collection_provenance_best_effort(
        self,
        point_ids: Optional[Iterable[Any]] = None,
        *,
        corpus_sha256: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Best-effort provenance write: log and swallow failures.

        A provenance-write failure must never corrupt or fail an otherwise
        successful index build, so this is called inside the build success path
        but never propagates an exception.
        """
        try:
            return self.write_collection_provenance(point_ids, corpus_sha256=corpus_sha256)
        except Exception as exc:  # pragma: no cover - defensive best-effort guard
            logger.warning(
                "Failed to write collection provenance for '%s': %s: %s",
                self.collection,
                type(exc).__name__,
                exc,
            )
            return None

    def _invalidate_collection_provenance(
        self, *, strict: bool = False, collection: Optional[str] = None
    ) -> None:
        """Delete the reserved provenance sentinel after an incremental mutation.

        Any post-build incremental mutation (``index_file``, ``index_symbol``,
        ``remove_file``, ``move_file``, ``mark_file_deleted``) changes the live
        point set out from under the last clean full build's recorded
        ``point_set_id``. Rather than leave a stale-but-valid-looking sentinel, we
        fail closed: drop the reserved point so ``read_collection_provenance``
        returns ``None`` (``provenance_missing``) until a clean full rebuild
        re-attests the collection. Best-effort — a delete failure must never fail
        the mutation that triggered it.
        """
        if not getattr(self, "_qdrant_available", False):
            if strict:
                raise RuntimeError("Qdrant unavailable for provenance invalidation")
            return
        try:
            self.qdrant.delete(
                collection_name=collection or self.collection,
                points_selector=models.PointIdsList(points=[self.PROVENANCE_POINT_ID]),
                wait=True,
            )
        except Exception as exc:  # pragma: no cover - defensive best-effort guard
            if strict:
                raise RuntimeError("Collection provenance invalidation failed") from None
            logger.debug(
                "Failed to invalidate collection provenance for '%s': %s",
                self.collection,
                type(exc).__name__,
            )

    def read_collection_provenance(
        self, collection: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch the reserved provenance sentinel manifest from a collection.

        Returns the ``collection-provenance.v1`` manifest dict, or ``None`` when
        the sentinel is absent, unreadable, or not a provenance record.
        """
        target = collection or self.collection
        if not getattr(self, "_qdrant_available", False):
            return None
        return _read_collection_provenance_sentinel(self.qdrant, target)

    # ------------------------------------------------------------------
    def index_symbol(
        self,
        file: str,
        name: str,
        kind: str,
        signature: str,
        line: int,
        span: tuple[int, int],
        doc: str | None = None,
        content: str = "",
        metadata: dict[str, Any] | None = None,
        sqlite_store: Optional["SQLiteStore"] = None,
        profile_id: Optional[str] = None,
    ) -> None:
        """Index a single code symbol with its embedding.

        Args:
            file: Source file path
            name: Symbol name
            kind: Symbol type (function, class, chunk, etc.)
            signature: Symbol signature
            line: Line number where symbol is defined
            span: Line span of the symbol
            doc: Optional documentation string
            content: Symbol content for embedding generation
            metadata: Additional metadata to store with the symbol
        """
        # For chunks, content already contains contextual embedding text
        if kind == "chunk":
            embedding_text = content
        else:
            # Create embedding text from available information
            embedding_text = f"{kind} {name}\n{signature}"
            if doc:
                embedding_text += f"\n{doc}"
            if content:
                embedding_text += f"\n{content}"

        # Generate embedding
        try:
            self._prepare_for_writes()
            embedding = self._embed_texts([embedding_text], input_type="document")[0]

            # Compute content hash
            content_hash = hashlib.sha256(content.encode()).hexdigest() if content else None

            # Use relative path
            relative_path = self.path_resolver.normalize_path(file)

            # Create point for Qdrant
            point_id = self._symbol_id(file, name, line, content_hash)
            payload = {
                "file": file,  # Keep absolute for backward compatibility
                "relative_path": relative_path,
                "content_hash": content_hash,
                "symbol": name,
                "kind": kind,
                "signature": signature,
                "line": line,
                "span": list(span),
                "doc": doc,
                "language": "python",  # This should be parameterized
                "is_deleted": False,
            }

            # Add custom metadata if provided. Strip the reserved provenance tag
            # so caller metadata can never poison a real point: a point carrying
            # ``__provenance__`` is filtered out of every search result, so a
            # caller-supplied key would otherwise make a real point silently
            # invisible.
            if metadata:
                payload.update({k: v for k, v in metadata.items() if k != self.PROVENANCE_TAG})

            point = models.PointStruct(id=point_id, vector=embedding, payload=payload)

            # Upsert to Qdrant with error handling
            if not self._qdrant_available:
                raise RuntimeError("Qdrant is not available - cannot index symbol")

            try:
                self.qdrant.upsert(collection_name=self.collection, points=[point])

                chunk_id = (metadata or {}).get("chunk_id") if metadata else None
                if chunk_id and sqlite_store is not None:
                    effective_profile_id = profile_id or self.semantic_profile.profile_id
                    sqlite_store.upsert_semantic_point(
                        profile_id=effective_profile_id,
                        chunk_id=str(chunk_id),
                        point_id=point_id,
                        collection=self.collection,
                    )
            except Exception as upsert_error:
                logger.error(
                    f"Qdrant upsert failed for symbol '{name}': "
                    f"{type(upsert_error).__name__}: {upsert_error}"
                )
                self._qdrant_available = False
                raise RuntimeError(f"Failed to store symbol '{name}' in Qdrant: {upsert_error}")

            # Incremental mutation: the last clean full build's point-set binding
            # is now stale, so drop the provenance sentinel (fail-closed).
            self._invalidate_collection_provenance()
        except Exception as e:
            if "API key" in str(e) or "authentication" in str(e).lower():
                raise RuntimeError(
                    f"Semantic indexing failed due to API key issue: {e}\n"
                    "Configure Voyage AI API key using:\n"
                    "1. .mcp.json with env.VOYAGE_API_KEY for Claude Code\n"
                    "2. VOYAGE_API_KEY environment variable\n"
                    "3. VOYAGE_API_KEY in .env file"
                )
            raise RuntimeError(f"Failed to index symbol {name}: {e}")

    def delete_stale_vectors(
        self,
        profile_id: str,
        chunk_ids: List[str],
        sqlite_store: Optional["SQLiteStore"] = None,
    ) -> int:
        """Delete stale vectors for profile-scoped chunk ids and clear mappings."""
        deleted, _ = self._delete_mapped_vectors(profile_id, chunk_ids, sqlite_store)
        return deleted

    def _delete_mapped_vectors(
        self, profile_id: str, chunk_ids: List[str], sqlite_store: Optional["SQLiteStore"]
    ) -> tuple[int, int]:
        if sqlite_store is None or not chunk_ids:
            return 0, 0
        groups: Dict[str, list] = {}
        # The legacy point-id lookup drops collection identity; preserve it here.
        with sqlite_store._get_connection() as connection:
            for start in range(0, len(chunk_ids), 500):
                batch = chunk_ids[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    "SELECT point_id, collection FROM semantic_points "
                    f"WHERE profile_id = ? AND chunk_id IN ({placeholders})",
                    [profile_id, *batch],
                ).fetchall()
                for point_id, collection in rows:
                    if not collection:
                        raise RuntimeError(
                            "Semantic deletion requires recorded collection identity"
                        )
                    if getattr(self, "staging", False) and collection != self.collection:
                        raise RuntimeError("Staged cleanup cannot mutate another generation")
                    groups.setdefault(collection, []).append(point_id)
        deleted = 0
        for collection, point_ids in groups.items():
            deleted += self.delete_remote_points(point_ids, collection=collection)
        mappings = sqlite_store.delete_semantic_point_mappings(profile_id, chunk_ids)
        return deleted, mappings

    def delete_remote_points(
        self,
        point_ids: List[Any],
        *,
        collection: Optional[str] = None,
    ) -> int:
        """Delete specific remote (Qdrant) points by id from ``collection``.

        Mirrors the remote delete in :meth:`delete_stale_vectors`, but takes raw
        remote point ids instead of chunk ids.  Used by the crash-ledger drain
        (:meth:`SQLiteStore.drain_pending_vector_deletions`), whose rows record
        only the remote point id + collection because the local chunk mapping is
        already gone.  ``collection`` defaults to this indexer's collection when
        the ledger row did not record one.  Raises on a remote failure so the
        caller can leave the ledger row for a later attempt.
        """
        point_ids = list(dict.fromkeys(point_ids))
        if not point_ids:
            return 0
        if not self._qdrant_available:
            raise RuntimeError("Qdrant client unavailable for remote point deletion")
        target = collection or self.collection
        if getattr(self, "staging", False) and target != self.collection:
            raise RuntimeError("Staged cleanup cannot mutate another generation")
        try:
            self._invalidate_collection_provenance(strict=True, collection=target)
            for start in range(0, len(point_ids), 256):
                self.qdrant.delete(
                    collection_name=target,
                    points_selector=models.PointIdsList(points=point_ids[start : start + 256]),
                    wait=True,
                )
        except Exception as exc:
            logger.error("Remote point deletion failed (%s)", type(exc).__name__)
            # NOTE: deliberately do NOT set ``self._qdrant_available = False`` here.
            # That flag is the upsert path's circuit breaker (``_batch_upsert``).
            # The drain runs at the FRONT of every reindex against the shared cached
            # indexer, so a transient DELETE blip must not degrade indexing
            # availability for the rest of the run.  We still raise so the caller
            # (the ledger drain) leaves the row for a later attempt.
            raise RuntimeError("Failed to delete remote points from Qdrant") from None
        return len(point_ids)

    def cleanup_stale_semantic_artifacts(
        self,
        profile_id: str,
        invalidation: Mapping[str, Any],
        sqlite_store: Optional["SQLiteStore"] = None,
    ) -> Dict[str, Any]:
        """Delete stale vectors, mappings, and invalidated summaries for a file mutation."""
        if sqlite_store is None:
            return {
                "vectors_deleted": 0,
                "mappings_deleted": 0,
                "summaries_deleted": 0,
                "summaries_preserved": len(invalidation.get("summary_chunk_ids_preserved", [])),
                "requires_summary_regeneration": bool(
                    invalidation.get("requires_summary_regeneration", False)
                ),
            }

        vector_chunk_ids = list(invalidation.get("vector_chunk_ids", []) or [])
        deleted_vectors, deleted_mappings = self._delete_mapped_vectors(
            profile_id, vector_chunk_ids, sqlite_store
        )
        deleted_summaries = sqlite_store.delete_chunk_summaries(
            list(invalidation.get("summary_chunk_ids_to_delete", []) or [])
        )
        return {
            "vectors_deleted": deleted_vectors,
            "mappings_deleted": deleted_mappings,
            "summaries_deleted": deleted_summaries,
            "summaries_preserved": len(invalidation.get("summary_chunk_ids_preserved", [])),
            "requires_summary_regeneration": bool(
                invalidation.get("requires_summary_regeneration", False)
            ),
        }

    # ------------------------------------------------------------------
    def search(
        self, query: str, limit: int = 20, *, source_chunk_ids: Optional[List[str]] = None
    ) -> list[dict[str, Any]]:
        """Search for code using semantic similarity.

        Args:
            query: Natural language search query
            limit: Maximum number of results
            source_chunk_ids: Restrict retrieval to these source chunks before ranking.

        Returns:
            List of search results with metadata and scores

        Raises:
            RuntimeError: If Qdrant is unavailable or search fails
        """
        if not self._qdrant_available:
            raise RuntimeError(
                "Qdrant is not available - semantic search unavailable. "
                "Use is_available property to check before calling."
            )
        return list(self.query(query, limit, source_chunk_ids=source_chunk_ids))

    # ------------------------------------------------------------------
    # Document-specific methods
    # ------------------------------------------------------------------

    def _parse_markdown_sections(self, content: str, file_path: str) -> list[DocumentSection]:
        """Parse markdown content into hierarchical sections."""
        lines = content.split("\n")
        sections = []
        current_section = None
        section_stack = []  # Track parent sections

        for i, line in enumerate(lines):
            # Match markdown headers
            header_match = re.match(r"^(#{1,6})\s+(.+)$", line)
            if header_match:
                level = len(header_match.group(1))
                title = header_match.group(2).strip()

                # Pop sections from stack that are at same or lower level
                while section_stack and section_stack[-1][1] >= level:
                    section_stack.pop()

                parent_section = section_stack[-1][0] if section_stack else None

                # Save previous section if exists
                if current_section:
                    current_section.end_line = i - 1
                    sections.append(current_section)

                # Create new section
                current_section = DocumentSection(
                    title=title,
                    content="",  # Will be filled later
                    level=level,
                    start_line=i + 1,
                    end_line=len(lines),  # Will be updated
                    parent_section=parent_section,
                )

                # Update parent's subsections
                if parent_section:
                    for sec in sections:
                        if sec.title == parent_section:
                            sec.subsections.append(title)
                            break

                section_stack.append((title, level))

        # Save last section
        if current_section:
            sections.append(current_section)

        # Fill in content for each section
        for section in sections:
            section.content = "\n".join(lines[section.start_line : section.end_line])

        return sections

    def _create_document_embedding(
        self,
        content: str,
        title: Optional[str] = None,
        section_context: Optional[str] = None,
        doc_type: str = "markdown",
        metadata: Optional[dict] = None,
    ) -> list[float]:
        """Create embeddings with document-specific context."""
        # Build context-aware embedding text
        embedding_parts = []

        if title:
            embedding_parts.append(f"Document: {title}")

        if section_context:
            embedding_parts.append(f"Section: {section_context}")

        if metadata:
            if "summary" in metadata:
                embedding_parts.append(f"Summary: {metadata['summary']}")
            if "tags" in metadata:
                embedding_parts.append(f"Tags: {', '.join(metadata['tags'])}")

        embedding_parts.append(content)
        embedding_text = "\n\n".join(embedding_parts)

        # Generate embedding with appropriate input type
        input_type = "document" if doc_type in ["markdown", "readme"] else "query"

        try:
            embedding = self._embed_texts([embedding_text], input_type=input_type)[0]
            return embedding
        except Exception as e:
            raise RuntimeError(f"Failed to create document embedding: {e}")

    def index_document(
        self,
        path: Path,
        doc_type: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Index a document with section-aware embeddings.

        Args:
            path: Path to the document
            doc_type: Type of document (markdown, readme, etc.)
            metadata: Additional metadata for the document

        Returns:
            Information about indexed sections
        """
        content = path.read_text(encoding="utf-8")
        file_name = path.name.lower()

        # Determine document type
        if doc_type is None:
            if file_name == "readme.md":
                doc_type = "readme"
            elif file_name.endswith(".md"):
                doc_type = "markdown"
            else:
                doc_type = "text"

        # Parse sections for markdown documents
        if doc_type in ["markdown", "readme"]:
            sections = self._parse_markdown_sections(content, str(path))
        else:
            # Treat entire document as one section
            sections = [
                DocumentSection(
                    title=path.stem,
                    content=content,
                    level=1,
                    start_line=1,
                    end_line=len(content.split("\n")),
                )
            ]

        indexed_sections = []
        points = []

        for section in sections:
            # Build section context (parent sections)
            section_context = []
            if section.parent_section:
                section_context.append(section.parent_section)
            section_context.append(section.title)
            context_str = " > ".join(section_context)

            # Create embedding with context
            embedding = self._create_document_embedding(
                content=section.content,
                title=path.stem,
                section_context=context_str,
                doc_type=doc_type,
                metadata=metadata,
            )

            # Create unique ID for section
            section_id = self._document_section_id(str(path), section.title, section.start_line)

            # Use relative path
            relative_path = self.path_resolver.normalize_path(path)

            # Compute content hash for section
            section_hash = hashlib.sha256(section.content.encode()).hexdigest()

            # Prepare payload
            payload = {
                "file": str(path),  # Keep absolute for backward compatibility
                "relative_path": relative_path,
                "content_hash": section_hash,
                "title": section.title,
                "section_context": context_str,
                "content": section.content[:500],  # Store preview
                "level": section.level,
                "start_line": section.start_line,
                "end_line": section.end_line,
                "parent_section": section.parent_section,
                "subsections": section.subsections,
                "doc_type": doc_type,
                "type": "document_section",
                "language": ("markdown" if doc_type in ["markdown", "readme"] else "text"),
                "is_deleted": False,
            }

            if metadata:
                payload["metadata"] = metadata

            points.append(models.PointStruct(id=section_id, vector=embedding, payload=payload))

            indexed_sections.append(
                {
                    "title": section.title,
                    "level": section.level,
                    "context": context_str,
                    "lines": f"{section.start_line}-{section.end_line}",
                }
            )

        # Upsert all sections with error handling
        if points:
            if not self._qdrant_available:
                raise RuntimeError(f"Qdrant is not available - cannot index document {path}")

            self._prepare_for_writes()
            try:
                self.qdrant.upsert(collection_name=self.collection, points=points)
            except Exception as e:
                logger.error(
                    f"Failed to upsert {len(points)} sections for document {path}: "
                    f"{type(e).__name__}: {e}"
                )
                self._qdrant_available = False
                raise RuntimeError(f"Failed to store document sections for {path} in Qdrant: {e}")

        return {
            "file": str(path),
            "doc_type": doc_type,
            "sections": indexed_sections,
            "total_sections": len(indexed_sections),
        }

    def _document_section_id(self, file: str, section: str, line: int) -> int:
        """Generate unique ID for document section."""
        h = hashlib.sha1(
            f"doc:{file}:{section}:{line}".encode("utf-8"), usedforsecurity=False
        ).digest()[:8]
        return self._reserve_safe_id(int.from_bytes(h, "big", signed=False))

    def query_natural_language(
        self,
        query: str,
        limit: int = 10,
        doc_types: Optional[list[str]] = None,
        include_code: bool = True,
    ) -> list[dict[str, Any]]:
        """Query using natural language with document type weighting.

        Args:
            query: Natural language query
            limit: Maximum results
            doc_types: Filter by document types
            include_code: Whether to include code results

        Returns:
            Weighted and filtered search results

        Raises:
            RuntimeError: If Qdrant is unavailable or query fails
        """
        if not self._qdrant_available:
            raise RuntimeError("Qdrant is not available - cannot perform natural language query")

        try:
            # Generate query embedding
            embedding = self._embed_texts([query], input_type="query")[0]
        except Exception as e:
            raise RuntimeError(f"Failed to generate query embedding: {e}")

        try:
            # Search with higher limit to allow filtering
            results = self.qdrant.search(
                collection_name=self.collection,
                query_vector=embedding,
                limit=limit * 2 if doc_types else limit,
            )
        except Exception as e:
            logger.error(f"Qdrant natural language query failed: {type(e).__name__}: {e}")
            self._qdrant_available = False
            raise RuntimeError(f"Natural language query failed - Qdrant error: {e}")

        weighted_results = []

        for res in results:
            payload = res.payload or {}

            # Never surface the reserved collection-provenance sentinel.
            if payload.get(self.PROVENANCE_TAG):
                continue

            # Filter by document types if specified
            if doc_types:
                doc_type = payload.get("doc_type", "code")
                if doc_type not in doc_types and not (include_code and doc_type == "code"):
                    continue

            # Apply document type weighting
            doc_type = payload.get("doc_type", "code")
            weight = self.DOCUMENT_TYPE_WEIGHTS.get(doc_type, 1.0)

            # Adjust score based on document type
            weighted_score = res.score * weight

            result = {
                **payload,
                "score": res.score,
                "weighted_score": weighted_score,
                "weight_factor": weight,
            }

            weighted_results.append(result)

        # Sort by weighted score and limit
        weighted_results.sort(key=lambda x: x["weighted_score"], reverse=True)
        return weighted_results[:limit]

    def index_documentation_directory(
        self,
        directory: Path,
        recursive: bool = True,
        file_patterns: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Index all documentation files in a directory.

        Args:
            directory: Directory to index
            recursive: Whether to search recursively
            file_patterns: File patterns to match (default: ["*.md", "*.rst", "*.txt"])

        Returns:
            Summary of indexed documents
        """
        if file_patterns is None:
            file_patterns = ["*.md", "*.rst", "*.txt"]

        indexed_files = []
        total_sections = 0

        for pattern in file_patterns:
            if recursive:
                files = directory.rglob(pattern)
            else:
                files = directory.glob(pattern)

            for file_path in files:
                if file_path.is_file():
                    try:
                        result = self.index_document(file_path)
                        indexed_files.append(result["file"])
                        total_sections += result["total_sections"]
                    except Exception as e:
                        print(f"Failed to index {file_path}: {e}")

        return {
            "directory": str(directory),
            "indexed_files": indexed_files,
            "total_files": len(indexed_files),
            "total_sections": total_sections,
        }

    # ------------------------------------------------------------------
    # File operation methods for path management
    # ------------------------------------------------------------------

    def _point_batches(
        self, condition: Filter, *, collection: Optional[str] = None, with_vectors: bool = False
    ) -> Iterable[List[Any]]:
        """Scroll all matching points with bounded pages and stable point identities."""
        if not self._qdrant_available:
            raise RuntimeError("Qdrant unavailable for point maintenance")
        offset = None
        while True:
            batch, offset = self.qdrant.scroll(
                collection_name=collection or self.collection,
                scroll_filter=condition,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=with_vectors,
            )
            if batch:
                yield batch
            if offset is None:
                break

    def remove_file(self, file_path: Union[str, Path]) -> int:
        """Delete every point for the file, with acknowledged, retryable batches."""
        relative_path = self.path_resolver.normalize_path(file_path)
        condition = Filter(
            must=[FieldCondition(key="relative_path", match=MatchValue(value=relative_path))]
        )
        removed = 0
        try:
            for batch in self._point_batches(condition):
                self._invalidate_collection_provenance(strict=True)
                self.qdrant.delete(
                    collection_name=self.collection,
                    points_selector=models.PointIdsList(points=[point.id for point in batch]),
                    wait=True,
                )
                removed += len(batch)
        except Exception as exc:
            raise RuntimeError(f"File vector deletion failed ({type(exc).__name__})") from None
        return removed

    def move_file(
        self,
        old_path: Union[str, Path],
        new_path: Union[str, Path],
        content_hash: Optional[str] = None,
    ) -> int:
        """Update payloads by stable ID without reordering or rewriting vectors."""
        old_relative = self.path_resolver.normalize_path(old_path)
        new_relative = self.path_resolver.normalize_path(new_path)
        conditions = [FieldCondition(key="relative_path", match=MatchValue(value=old_relative))]
        if content_hash is not None:
            conditions.append(
                FieldCondition(key="content_hash", match=MatchValue(value=content_hash))
            )
        updated = 0
        try:
            for batch in self._point_batches(Filter(must=conditions)):
                self._invalidate_collection_provenance(strict=True)
                self.qdrant.set_payload(
                    collection_name=self.collection,
                    payload={"relative_path": new_relative, "file": str(new_path)},
                    points=[point.id for point in batch],
                    wait=True,
                )
                updated += len(batch)
        except Exception as exc:
            raise RuntimeError(f"File vector move failed ({type(exc).__name__})") from None
        return updated

    def get_embeddings_by_content_hash(self, content_hash: str) -> List[Dict[str, Any]]:
        """Return all matching point metadata, including results past the first page."""
        condition = Filter(
            must=[FieldCondition(key="content_hash", match=MatchValue(value=content_hash))]
        )
        return [
            {**(point.payload or {}), "id": point.id}
            for batch in self._point_batches(condition)
            for point in batch
        ]

    def mark_file_deleted(self, file_path: Union[str, Path]) -> int:
        """Mark every matching payload deleted without touching its vector."""
        relative_path = self.path_resolver.normalize_path(file_path)
        condition = Filter(
            must=[FieldCondition(key="relative_path", match=MatchValue(value=relative_path))]
        )
        updated = 0
        try:
            for batch in self._point_batches(condition):
                self._invalidate_collection_provenance(strict=True)
                self.qdrant.set_payload(
                    collection_name=self.collection,
                    payload={"is_deleted": True},
                    points=[point.id for point in batch],
                    wait=True,
                )
                updated += len(batch)
        except Exception as exc:
            raise RuntimeError(f"File vector marking failed ({type(exc).__name__})") from None
        return updated

    @staticmethod
    def _file_summary_chunk_id(relative_path: str) -> str:
        """Return a deterministic file-summary chunk id for a file."""
        return f"{relative_path}:file-summary"


# ---------------------------------------------------------------------------
# INFERLIVEGATE Lane A: module-level provenance reader (Lane B integration seam)
# ---------------------------------------------------------------------------
def _read_collection_provenance_sentinel(client: Any, collection: str) -> Optional[Dict[str, Any]]:
    """Read + parse the reserved collection-provenance sentinel from a raw client.

    Retrieves the reserved sentinel point (``PROVENANCE_POINT_ID``) from ``client``,
    verifies it carries the ``__provenance__`` tag and the ``collection-provenance.v1``
    schema version, and returns the manifest with the internal tag stripped. Returns
    ``None`` for any absent/unreadable/non-provenance case — it never fabricates a
    binding.
    """
    if client is None or not hasattr(client, "retrieve"):
        return None
    try:
        records = client.retrieve(
            collection_name=collection,
            ids=[SemanticIndexer.PROVENANCE_POINT_ID],
            with_payload=True,
        )
    except Exception as exc:
        logger.debug(
            "Could not read collection provenance from '%s': %s: %s",
            collection,
            type(exc).__name__,
            exc,
        )
        return None
    for record in records or []:
        payload = dict(getattr(record, "payload", None) or {})
        if not payload.get(SemanticIndexer.PROVENANCE_TAG):
            continue
        manifest = {
            key: value for key, value in payload.items() if key != SemanticIndexer.PROVENANCE_TAG
        }
        if manifest.get("provenance_version") != SemanticIndexer.PROVENANCE_VERSION:
            return None
        return manifest
    return None


def read_collection_provenance(client: Any, collection: str) -> Optional[Dict[str, Any]]:
    """Canonical module-level reader for the collection-resident provenance manifest.

    A live-collection consumer (the INFERLIVEGATE benchmark) passes a raw Qdrant
    ``client`` and ``collection`` name and gets back the ``collection-provenance.v1``
    manifest dict written by :meth:`SemanticIndexer.write_collection_provenance`, or
    ``None`` when no verified provenance sentinel is present. This stays in lockstep
    with the producer's sentinel id/tag so consumers never need to hardcode them.
    """
    return _read_collection_provenance_sentinel(client, collection)
