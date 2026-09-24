"""Semantic stack readiness checks for CLI and gateway startup."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from urllib import error, request

from mcp_server.artifacts.semantic_namespace import SemanticNamespaceResolver
from mcp_server.artifacts.semantic_profiles import SemanticProfile, SemanticProfileRegistry
from mcp_server.config.env_vars import get_qdrant_api_key
from mcp_server.config.settings import Settings


class ServiceStatus(str, Enum):
    """Result status for a preflight check."""

    READY = "ready"
    UNREACHABLE = "unreachable"
    MISCONFIGURED = "misconfigured"
    DISABLED = "disabled"


@dataclass(frozen=True)
class CheckResult:
    """Single check result returned by preflight."""

    name: str
    status: ServiceStatus
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    fixes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the check passed."""
        return self.status == ServiceStatus.READY


@dataclass(frozen=True)
class EnrichmentModelResolution:
    """Resolved enrichment-model choice for a profile endpoint."""

    configured_model: str
    effective_model: str
    resolution_strategy: str
    available_models: List[str] = field(default_factory=list)
    models_probe_verified: bool = False
    probe_failure: Optional[str] = None

    @property
    def compatibility_considered(self) -> bool:
        """Whether the endpoint model list was consulted."""
        return self.models_probe_verified and bool(self.available_models)


@dataclass(frozen=True)
class SemanticWriteBlocker:
    """Structured fail-closed blocker for semantic vector writes."""

    code: str
    message: str
    failing_checks: List[Dict[str, Any]]
    remediation: List[str] = field(default_factory=list)
    can_write_semantic_vectors: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "failing_checks": list(self.failing_checks),
            "remediation": list(self.remediation),
            "can_write_semantic_vectors": self.can_write_semantic_vectors,
        }


@dataclass(frozen=True)
class SemanticPreflightReport:
    """Full readiness report for semantic stack usage."""

    overall_ready: bool
    strict_mode: bool
    qdrant: CheckResult
    embedding: CheckResult
    profiles: CheckResult
    enrichment: CheckResult
    collection: CheckResult
    can_write_semantic_vectors: bool
    blocker: Optional[SemanticWriteBlocker] = None
    warnings: List[str] = field(default_factory=list)
    effective_config: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize report for status APIs and JSON CLI output."""

        def _check_to_dict(check: CheckResult) -> Dict[str, Any]:
            return {
                "name": check.name,
                "status": check.status.value,
                "message": check.message,
                "details": check.details,
                "fixes": check.fixes,
            }

        return {
            "overall_ready": self.overall_ready,
            "strict_mode": self.strict_mode,
            "qdrant": _check_to_dict(self.qdrant),
            "embedding": _check_to_dict(self.embedding),
            "profiles": _check_to_dict(self.profiles),
            "enrichment": _check_to_dict(self.enrichment),
            "collection": _check_to_dict(self.collection),
            "can_write_semantic_vectors": self.can_write_semantic_vectors,
            "blocker": None if self.blocker is None else self.blocker.to_dict(),
            "warnings": list(self.warnings),
            "effective_config": dict(self.effective_config),
        }


@dataclass(frozen=True)
class CollectionBootstrapResult:
    """Outcome of active-profile collection bootstrap evaluation."""

    status: str
    message: str
    profile_id: Optional[str]
    collection_name: Optional[str]
    normalized_collection_name: Optional[str]
    qdrant_url: Optional[str]
    expected_dimension: Optional[int]
    expected_distance_metric: Optional[str]
    blocker: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "profile_id": self.profile_id,
            "collection_name": self.collection_name,
            "normalized_collection_name": self.normalized_collection_name,
            "qdrant_url": self.qdrant_url,
            "expected_dimension": self.expected_dimension,
            "expected_distance_metric": self.expected_distance_metric,
            "blocker": self.blocker,
        }


def _http_request_json(
    url: str,
    *,
    timeout_s: float,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    body = None
    merged_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        merged_headers.setdefault("Content-Type", "application/json")
    req = request.Request(url, data=body, headers=merged_headers, method=method)
    with request.urlopen(req, timeout=timeout_s) as response:
        content = response.read().decode("utf-8")
    return json.loads(content) if content else {}


def _http_get_json(
    url: str, timeout_s: float, headers: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    return _http_request_json(url, timeout_s=timeout_s, method="GET", headers=headers)


def _http_post_json(
    url: str,
    payload: Dict[str, Any],
    *,
    timeout_s: float,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    return _http_request_json(
        url,
        timeout_s=timeout_s,
        method="POST",
        payload=payload,
        headers=headers,
    )


def _read_http_error(exc: error.HTTPError) -> Tuple[int, str]:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = str(exc)
    return exc.code, body


def _redacted_auth_headers(api_key_env: str) -> Dict[str, str]:
    api_key = os.getenv(api_key_env)
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _qdrant_auth_headers() -> Dict[str, str]:
    api_key = get_qdrant_api_key()
    if not api_key:
        return {}
    return {"api-key": api_key}


def _missing_api_key_result(
    *,
    name: str,
    base_url: str,
    model: str,
    api_key_env: str,
    label: str,
) -> CheckResult:
    return CheckResult(
        name=name,
        status=ServiceStatus.MISCONFIGURED,
        message=f"{label} API key env var is missing",
        details={
            "base_url": base_url,
            "model": model,
            "api_key_env": api_key_env,
            "api_key_present": False,
            "failure_class": "missing_api_key_env",
        },
        fixes=[
            f"Set {api_key_env} before running semantic preflight",
            f"Re-run preflight after exporting the {label.lower()} API key env var",
        ],
    )


def _build_unavailable_result(
    *,
    name: str,
    message: str,
    failure_class: str,
    details: Optional[Dict[str, Any]] = None,
    fixes: Optional[List[str]] = None,
) -> CheckResult:
    return CheckResult(
        name=name,
        status=ServiceStatus.MISCONFIGURED,
        message=message,
        details={"failure_class": failure_class, **(details or {})},
        fixes=list(fixes or []),
    )


def _detect_model_failure(body: str) -> Optional[str]:
    lowered = body.lower()
    if "model" in lowered and (
        "not found" in lowered
        or "does not exist" in lowered
        or "unsupported" in lowered
        or "unknown" in lowered
    ):
        return "wrong_chat_model"
    return None


def _extract_model_ids(payload: Dict[str, Any]) -> List[str]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    model_ids = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id.strip():
            model_ids.append(model_id.strip())
    return model_ids


def resolve_enrichment_model(
    *,
    base_url: str,
    configured_model: str,
    api_key_env: str,
    timeout_s: float,
) -> EnrichmentModelResolution:
    """Resolve the effective enrichment model for the active endpoint.

    The current compatibility path is intentionally narrow: when the configured
    local default is the `chat` alias and the endpoint advertises exactly one
    served model, use that single served model as the effective target.
    """
    probe_url = base_url.rstrip("/") + "/models"
    try:
        payload = _http_request_json(
            probe_url,
            timeout_s=timeout_s,
            headers=_redacted_auth_headers(api_key_env),
        )
        model_ids = _extract_model_ids(payload)
    except error.HTTPError as exc:
        status_code, body = _read_http_error(exc)
        return EnrichmentModelResolution(
            configured_model=configured_model,
            effective_model=configured_model,
            resolution_strategy="models_probe_http_error",
            available_models=[],
            models_probe_verified=False,
            probe_failure=f"http_{status_code}:{body}",
        )
    except (error.URLError, TimeoutError, ValueError, OSError) as exc:
        return EnrichmentModelResolution(
            configured_model=configured_model,
            effective_model=configured_model,
            resolution_strategy="models_probe_unavailable",
            available_models=[],
            models_probe_verified=False,
            probe_failure=str(exc),
        )

    if configured_model in model_ids:
        return EnrichmentModelResolution(
            configured_model=configured_model,
            effective_model=configured_model,
            resolution_strategy="configured_model_served",
            available_models=model_ids,
            models_probe_verified=True,
        )

    if configured_model == "chat" and len(model_ids) == 1:
        return EnrichmentModelResolution(
            configured_model=configured_model,
            effective_model=model_ids[0],
            resolution_strategy="single_served_model_for_chat_alias",
            available_models=model_ids,
            models_probe_verified=True,
        )

    return EnrichmentModelResolution(
        configured_model=configured_model,
        effective_model=configured_model,
        resolution_strategy="no_compatible_model_found",
        available_models=model_ids,
        models_probe_verified=True,
    )


def check_qdrant(qdrant_url: str, timeout_s: float = 5.0) -> CheckResult:
    """Check Qdrant API reachability."""
    probe = qdrant_url.rstrip("/") + "/collections"
    try:
        payload = _http_get_json(probe, timeout_s=timeout_s, headers=_qdrant_auth_headers())
        collections = payload.get("result", {}).get("collections", [])
        return CheckResult(
            name="qdrant",
            status=ServiceStatus.READY,
            message="Qdrant is reachable",
            details={
                "url": qdrant_url,
                "collections": len(collections),
                "failure_class": None,
            },
        )
    except error.HTTPError as exc:
        status_code, body = _read_http_error(exc)
        fixes = [
            "Check the Qdrant server health and route",
            "Or set QDRANT_URL to a reachable server endpoint",
        ]
        if status_code in (401, 403):
            fixes.insert(0, "Set QDRANT_API_KEY to the server's API key (QDRANT__SERVICE__API_KEY)")
        return CheckResult(
            name="qdrant",
            status=ServiceStatus.UNREACHABLE,
            message=f"Qdrant probe failed at {qdrant_url} with HTTP {status_code}",
            details={
                "url": qdrant_url,
                "http_status": status_code,
                "error": body,
                "failure_class": "qdrant_unreachable",
            },
            fixes=fixes,
        )
    except (error.URLError, TimeoutError, ValueError, OSError) as exc:
        return CheckResult(
            name="qdrant",
            status=ServiceStatus.UNREACHABLE,
            message=f"Qdrant is not reachable at {qdrant_url}: {exc}",
            details={"url": qdrant_url, "error": str(exc), "failure_class": "qdrant_unreachable"},
            fixes=[
                "Run setup semantic to auto-start local Qdrant via Docker",
                "Or set QDRANT_URL to a reachable server endpoint",
            ],
        )


def check_openai_compatible(base_url: str, timeout_s: float = 5.0) -> CheckResult:
    """Check OpenAI-compatible embeddings endpoint reachability."""
    probe = base_url.rstrip("/") + "/models"
    try:
        payload = _http_get_json(probe, timeout_s=timeout_s)
        data = payload.get("data") or []
        model_ids = [item.get("id") for item in data if isinstance(item, dict)]
        return CheckResult(
            name="embedding_openai_compatible",
            status=ServiceStatus.READY,
            message="OpenAI-compatible embedding endpoint is reachable",
            details={"base_url": base_url, "models": model_ids[:5], "failure_class": None},
        )
    except error.HTTPError as exc:
        status_code, body = _read_http_error(exc)
        return CheckResult(
            name="embedding_openai_compatible",
            status=ServiceStatus.UNREACHABLE,
            message=f"OpenAI-compatible endpoint probe failed with HTTP {status_code}",
            details={
                "base_url": base_url,
                "http_status": status_code,
                "error": body,
                "failure_class": "embedding_endpoint_unreachable",
            },
            fixes=[
                "Verify the embedding endpoint route and auth configuration",
                "Or point OPENAI_API_BASE to a reachable OpenAI-compatible server",
            ],
        )
    except (error.URLError, TimeoutError, ValueError, OSError) as exc:
        return CheckResult(
            name="embedding_openai_compatible",
            status=ServiceStatus.UNREACHABLE,
            message=f"OpenAI-compatible endpoint is not reachable at {base_url}: {exc}",
            details={
                "base_url": base_url,
                "error": str(exc),
                "failure_class": "embedding_endpoint_unreachable",
            },
            fixes=[
                "Start vLLM or another OpenAI-compatible embeddings server",
                "Or set OPENAI_API_BASE to a reachable endpoint",
            ],
        )


def check_voyage_key_present() -> CheckResult:
    """Check voyage credential presence."""
    key = os.getenv("VOYAGE_API_KEY")
    if key:
        return CheckResult(
            name="embedding_voyage",
            status=ServiceStatus.READY,
            message="Voyage API key is configured",
            details={
                "api_key_env": "VOYAGE_API_KEY",
                "api_key_present": True,
                "failure_class": None,
            },
        )
    return CheckResult(
        name="embedding_voyage",
        status=ServiceStatus.MISCONFIGURED,
        message="Voyage API key is missing",
        details={
            "api_key_env": "VOYAGE_API_KEY",
            "api_key_present": False,
            "failure_class": "missing_api_key_env",
        },
        fixes=[
            "Set VOYAGE_API_KEY",
            "Use `op item get` to load the key from 1Password",
        ],
    )


def check_profile_registry(settings: Settings) -> CheckResult:
    """Validate semantic profile registry from effective settings."""
    try:
        registry = SemanticProfileRegistry.from_raw(
            settings.get_semantic_profiles_config(),
            settings.get_semantic_default_profile(),
            tool_version=settings.app_version,
        )
        profile_ids = list(registry.list().keys())
        return CheckResult(
            name="semantic_profiles",
            status=ServiceStatus.READY,
            message="Semantic profile configuration is valid",
            details={
                "default_profile": registry.default_profile,
                "profiles": profile_ids,
                "failure_class": None,
            },
        )
    except Exception as exc:
        return CheckResult(
            name="semantic_profiles",
            status=ServiceStatus.MISCONFIGURED,
            message=f"Semantic profile configuration is invalid: {exc}",
            details={"error": str(exc), "failure_class": "profile_registry_invalid"},
            fixes=[
                "Validate code-index-mcp.profiles.yaml syntax and profile IDs",
                "Validate SEMANTIC_PROFILES_JSON and SEMANTIC_DEFAULT_PROFILE",
            ],
        )


def resolve_profile_registry(
    settings: Settings,
    profile: Optional[str] = None,
) -> Tuple[CheckResult, Optional[SemanticProfileRegistry], Optional[SemanticProfile]]:
    """Return profile validation plus selected semantic profile when possible."""
    profile_check = check_profile_registry(settings)
    if not profile_check.ok:
        return profile_check, None, None

    registry = SemanticProfileRegistry.from_raw(
        settings.get_semantic_profiles_config(),
        settings.get_semantic_default_profile(),
        tool_version=settings.app_version,
    )
    selected_profile = registry.get(profile or settings.get_semantic_default_profile())
    return profile_check, registry, selected_profile


def check_enrichment_chat(
    *,
    base_url: str,
    model: str,
    api_key_env: str,
    timeout_s: float,
) -> CheckResult:
    """Send a minimal chat smoke to the enrichment endpoint."""
    if not os.getenv(api_key_env):
        return _missing_api_key_result(
            name="enrichment_chat",
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
            label="Enrichment",
        )

    resolution = resolve_enrichment_model(
        base_url=base_url,
        configured_model=model,
        api_key_env=api_key_env,
        timeout_s=timeout_s,
    )
    effective_model = resolution.effective_model

    try:
        payload = _http_post_json(
            base_url.rstrip("/") + "/chat/completions",
            {
                "model": effective_model,
                "messages": [{"role": "user", "content": "semantic preflight smoke"}],
                "max_tokens": 1,
            },
            timeout_s=timeout_s,
            headers=_redacted_auth_headers(api_key_env),
        )
        choices = payload.get("choices") or []
        if not isinstance(choices, list) or not choices:
            return _build_unavailable_result(
                name="enrichment_chat",
                message="Enrichment endpoint returned no chat choices",
                failure_class="chat_response_invalid",
                details={
                    "base_url": base_url,
                    "model": effective_model,
                    "configured_model": model,
                    "effective_model": effective_model,
                    "api_key_env": api_key_env,
                    "api_key_present": True,
                    "resolution_strategy": resolution.resolution_strategy,
                    "considered_model_ids": resolution.available_models,
                    "models_probe_verified": resolution.models_probe_verified,
                    "models_probe_failure": resolution.probe_failure,
                },
                fixes=[
                    "Verify the enrichment proxy implements OpenAI-compatible chat responses",
                ],
            )
        return CheckResult(
            name="enrichment_chat",
            status=ServiceStatus.READY,
            message="Enrichment chat smoke succeeded",
            details={
                "base_url": base_url,
                "model": effective_model,
                "configured_model": model,
                "effective_model": effective_model,
                "api_key_env": api_key_env,
                "api_key_present": True,
                "response_model": payload.get("model"),
                "resolution_strategy": resolution.resolution_strategy,
                "considered_model_ids": resolution.available_models,
                "models_probe_verified": resolution.models_probe_verified,
                "models_probe_failure": resolution.probe_failure,
                "failure_class": None,
            },
        )
    except error.HTTPError as exc:
        status_code, body = _read_http_error(exc)
        failure_class = _detect_model_failure(body) or "enrichment_request_rejected"
        message = (
            "Enrichment endpoint rejected the configured chat model"
            if failure_class == "wrong_chat_model"
            else f"Enrichment endpoint rejected the chat smoke with HTTP {status_code}"
        )
        return CheckResult(
            name="enrichment_chat",
            status=ServiceStatus.MISCONFIGURED,
            message=message,
            details={
                "base_url": base_url,
                "model": effective_model,
                "configured_model": model,
                "effective_model": effective_model,
                "api_key_env": api_key_env,
                "api_key_present": True,
                "http_status": status_code,
                "error": body,
                "resolution_strategy": resolution.resolution_strategy,
                "considered_model_ids": resolution.available_models,
                "models_probe_verified": resolution.models_probe_verified,
                "models_probe_failure": resolution.probe_failure,
                "failure_class": failure_class,
            },
            fixes=[
                "Verify the configured enrichment model exists on the target endpoint",
                "Or update SEMANTIC_ENRICHMENT_MODEL / profile metadata to a supported chat model",
            ],
        )
    except (error.URLError, TimeoutError, ValueError, OSError) as exc:
        return CheckResult(
            name="enrichment_chat",
            status=ServiceStatus.UNREACHABLE,
            message=f"Enrichment endpoint is not reachable at {base_url}: {exc}",
            details={
                "base_url": base_url,
                "model": effective_model,
                "configured_model": model,
                "effective_model": effective_model,
                "api_key_env": api_key_env,
                "api_key_present": True,
                "error": str(exc),
                "resolution_strategy": resolution.resolution_strategy,
                "considered_model_ids": resolution.available_models,
                "models_probe_verified": resolution.models_probe_verified,
                "models_probe_failure": resolution.probe_failure,
                "failure_class": "enrichment_endpoint_unreachable",
            },
            fixes=[
                "Start the enrichment proxy or repair its route",
                "Verify SEMANTIC_ENRICHMENT_BASE_URL points at the active OpenAI-compatible chat endpoint",
            ],
        )


def check_embedding_smoke(
    *,
    base_url: str,
    model: str,
    api_key_env: str,
    expected_dimension: int,
    timeout_s: float,
) -> CheckResult:
    """Send an embedding smoke and verify vector length."""
    if not os.getenv(api_key_env):
        return _missing_api_key_result(
            name="embedding_vector",
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
            label="Embedding",
        )

    try:
        payload = _http_post_json(
            base_url.rstrip("/") + "/embeddings",
            {
                "model": model,
                "input": ["semantic preflight smoke"],
            },
            timeout_s=timeout_s,
            headers=_redacted_auth_headers(api_key_env),
        )
        data = payload.get("data") or []
        if not isinstance(data, list) or not data:
            return _build_unavailable_result(
                name="embedding_vector",
                message="Embedding endpoint returned no vectors",
                failure_class="embedding_response_invalid",
                details={
                    "base_url": base_url,
                    "model": model,
                    "api_key_env": api_key_env,
                    "api_key_present": True,
                    "expected_dimension": expected_dimension,
                },
                fixes=[
                    "Verify the embedding endpoint implements OpenAI-compatible embeddings responses",
                ],
            )
        embedding = data[0].get("embedding") if isinstance(data[0], dict) else None
        actual_dimension = len(embedding) if isinstance(embedding, list) else None
        if actual_dimension != expected_dimension:
            return CheckResult(
                name="embedding_vector",
                status=ServiceStatus.MISCONFIGURED,
                message=(
                    "Embedding dimension mismatch: "
                    f"expected {expected_dimension}, got {actual_dimension}"
                ),
                details={
                    "base_url": base_url,
                    "model": model,
                    "api_key_env": api_key_env,
                    "api_key_present": True,
                    "expected_dimension": expected_dimension,
                    "actual_dimension": actual_dimension,
                    "failure_class": "embedding_dimension_mismatch",
                },
                fixes=[
                    "Point the profile at an embedding model with the expected vector dimension",
                    "Or update the semantic profile vector_dimension to match the model you intend to use",
                ],
            )
        return CheckResult(
            name="embedding_vector",
            status=ServiceStatus.READY,
            message="Embedding smoke succeeded",
            details={
                "base_url": base_url,
                "model": model,
                "api_key_env": api_key_env,
                "api_key_present": True,
                "expected_dimension": expected_dimension,
                "actual_dimension": actual_dimension,
                "failure_class": None,
            },
        )
    except error.HTTPError as exc:
        status_code, body = _read_http_error(exc)
        return CheckResult(
            name="embedding_vector",
            status=ServiceStatus.MISCONFIGURED,
            message=f"Embedding endpoint rejected the smoke request with HTTP {status_code}",
            details={
                "base_url": base_url,
                "model": model,
                "api_key_env": api_key_env,
                "api_key_present": True,
                "expected_dimension": expected_dimension,
                "http_status": status_code,
                "error": body,
                "failure_class": "embedding_request_rejected",
            },
            fixes=[
                "Verify the configured embedding model and endpoint route",
                "Confirm the endpoint accepts OpenAI-compatible embedding requests",
            ],
        )
    except (error.URLError, TimeoutError, ValueError, OSError) as exc:
        return CheckResult(
            name="embedding_vector",
            status=ServiceStatus.UNREACHABLE,
            message=f"Embedding endpoint is not reachable at {base_url}: {exc}",
            details={
                "base_url": base_url,
                "model": model,
                "api_key_env": api_key_env,
                "api_key_present": True,
                "expected_dimension": expected_dimension,
                "error": str(exc),
                "failure_class": "embedding_endpoint_unreachable",
            },
            fixes=[
                "Start the embedding endpoint or repair its route",
                "Verify SEMANTIC_EMBEDDING_BASE_URL points at the active embeddings endpoint",
            ],
        )


def check_qdrant_collection(
    *,
    qdrant_url: str,
    collection_name: str,
    expected_dimension: int,
    expected_distance: str,
    timeout_s: float,
    namespace_resolver: Optional[SemanticNamespaceResolver] = None,
) -> CheckResult:
    """Validate Qdrant collection identity and vector shape without mutating it."""
    resolver = namespace_resolver or SemanticNamespaceResolver()
    normalized_collection = resolver.normalize_collection_name(collection_name)
    try:
        payload = _http_get_json(
            qdrant_url.rstrip("/") + f"/collections/{collection_name}",
            timeout_s=timeout_s,
            headers=_qdrant_auth_headers(),
        )
        result = payload.get("result") or {}
        vectors = (((result.get("config") or {}).get("params") or {}).get("vectors")) or {}
        if isinstance(vectors, dict) and "size" not in vectors and len(vectors) == 1:
            vectors = next(iter(vectors.values()))

        actual_dimension = vectors.get("size") if isinstance(vectors, dict) else None
        actual_distance = (
            resolver.normalize_distance_metric(vectors.get("distance"))
            if isinstance(vectors, dict)
            else None
        )
        if (
            actual_dimension != expected_dimension
            or actual_distance != resolver.normalize_distance_metric(expected_distance)
        ):
            return CheckResult(
                name="qdrant_collection",
                status=ServiceStatus.MISCONFIGURED,
                message="Qdrant collection shape does not match the active profile",
                details={
                    "qdrant_url": qdrant_url,
                    "collection_name": collection_name,
                    "normalized_collection_name": normalized_collection,
                    "expected_dimension": expected_dimension,
                    "actual_dimension": actual_dimension,
                    "expected_distance_metric": resolver.normalize_distance_metric(
                        expected_distance
                    ),
                    "actual_distance_metric": actual_distance,
                    "failure_class": "collection_shape_mismatch",
                },
                fixes=[
                    "Rebuild or recreate the semantic collection with the active profile settings",
                    "Confirm the selected profile targets the intended collection namespace",
                ],
            )
        return CheckResult(
            name="qdrant_collection",
            status=ServiceStatus.READY,
            message="Qdrant collection matches the active profile",
            details={
                "qdrant_url": qdrant_url,
                "collection_name": collection_name,
                "normalized_collection_name": normalized_collection,
                "expected_dimension": expected_dimension,
                "actual_dimension": actual_dimension,
                "expected_distance_metric": resolver.normalize_distance_metric(expected_distance),
                "actual_distance_metric": actual_distance,
                "failure_class": None,
            },
        )
    except error.HTTPError as exc:
        status_code, body = _read_http_error(exc)
        if status_code == 404:
            return CheckResult(
                name="qdrant_collection",
                status=ServiceStatus.MISCONFIGURED,
                message="Qdrant collection is missing for the active semantic profile",
                details={
                    "qdrant_url": qdrant_url,
                    "collection_name": collection_name,
                    "normalized_collection_name": normalized_collection,
                    "expected_dimension": expected_dimension,
                    "expected_distance_metric": resolver.normalize_distance_metric(
                        expected_distance
                    ),
                    "http_status": status_code,
                    "error": body,
                    "failure_class": "collection_missing",
                },
                fixes=[
                    "Create or hydrate the expected semantic collection before vector writes",
                    "Confirm the selected profile namespace resolves to the intended collection",
                ],
            )
        return CheckResult(
            name="qdrant_collection",
            status=ServiceStatus.UNREACHABLE,
            message=f"Qdrant collection probe failed with HTTP {status_code}",
            details={
                "qdrant_url": qdrant_url,
                "collection_name": collection_name,
                "normalized_collection_name": normalized_collection,
                "http_status": status_code,
                "error": body,
                "failure_class": "qdrant_unreachable",
            },
            fixes=[
                "Check Qdrant server health and permissions",
                "Retry once the Qdrant API is reachable",
            ],
        )
    except (error.URLError, TimeoutError, ValueError, OSError) as exc:
        return CheckResult(
            name="qdrant_collection",
            status=ServiceStatus.UNREACHABLE,
            message=f"Qdrant collection is not reachable at {qdrant_url}: {exc}",
            details={
                "qdrant_url": qdrant_url,
                "collection_name": collection_name,
                "normalized_collection_name": normalized_collection,
                "error": str(exc),
                "failure_class": "qdrant_unreachable",
            },
            fixes=[
                "Run setup semantic to auto-start local Qdrant via Docker",
                "Or set QDRANT_URL to a reachable server endpoint",
            ],
        )


def _disabled_report(strict: bool) -> SemanticPreflightReport:
    disabled = CheckResult(
        name="semantic",
        status=ServiceStatus.DISABLED,
        message="Semantic search is disabled (SEMANTIC_SEARCH_ENABLED=false)",
    )
    blocker = SemanticWriteBlocker(
        code="semantic_disabled",
        message="Semantic vector writes are disabled until semantic search is enabled.",
        failing_checks=[
            {
                "name": "semantic",
                "status": disabled.status.value,
                "message": disabled.message,
                "failure_class": "semantic_disabled",
            }
        ],
        remediation=["Enable SEMANTIC_SEARCH_ENABLED=true to initialize semantic search"],
        can_write_semantic_vectors=False,
    )
    return SemanticPreflightReport(
        overall_ready=False,
        strict_mode=strict,
        qdrant=disabled,
        embedding=disabled,
        profiles=disabled,
        enrichment=disabled,
        collection=disabled,
        can_write_semantic_vectors=False,
        blocker=blocker,
        warnings=["Enable SEMANTIC_SEARCH_ENABLED=true to initialize semantic search"],
        effective_config={
            "semantic_enabled": False,
            "strict_mode": strict,
        },
    )


def _build_semantic_write_blocker(checks: List[CheckResult]) -> Optional[SemanticWriteBlocker]:
    failing_checks = [check for check in checks if not check.ok]
    if not failing_checks:
        return None

    primary = failing_checks[0]
    failing_payload = [
        {
            "name": check.name,
            "status": check.status.value,
            "message": check.message,
            "failure_class": check.details.get("failure_class"),
        }
        for check in failing_checks
    ]
    remediation: List[str] = []
    seen = set()
    for check in failing_checks:
        for fix in check.fixes:
            if fix not in seen:
                remediation.append(fix)
                seen.add(fix)
    blocker_code = str(primary.details.get("failure_class") or primary.status.value)
    return SemanticWriteBlocker(
        code=blocker_code,
        message=primary.message,
        failing_checks=failing_payload,
        remediation=remediation,
        can_write_semantic_vectors=False,
    )


def summarize_collection_bootstrap(
    preflight: Dict[str, Any],
    *,
    dry_run: bool = False,
) -> CollectionBootstrapResult:
    """Derive operator-facing collection bootstrap state from preflight."""
    effective = preflight.get("effective_config") or {}
    collection_name = effective.get("collection_name")
    normalized_collection_name = effective.get("normalized_collection_name")
    profile_id = effective.get("selected_profile")
    qdrant_url = effective.get("qdrant_url")
    expected_dimension = effective.get("vector_dimension")
    expected_distance_metric = effective.get("distance_metric")
    blocker = preflight.get("blocker") or {}

    if preflight.get("can_write_semantic_vectors"):
        return CollectionBootstrapResult(
            status="reused",
            message="Active semantic collection already matches the selected profile",
            profile_id=profile_id,
            collection_name=collection_name,
            normalized_collection_name=normalized_collection_name,
            qdrant_url=qdrant_url,
            expected_dimension=expected_dimension,
            expected_distance_metric=expected_distance_metric,
            blocker=None,
        )

    if blocker.get("code") == "collection_missing":
        return CollectionBootstrapResult(
            status="dry_run" if dry_run else "blocked",
            message=(
                "Dry run: active semantic collection would be created before vector writes"
                if dry_run
                else "Active semantic collection is missing and semantic writes remain blocked"
            ),
            profile_id=profile_id,
            collection_name=collection_name,
            normalized_collection_name=normalized_collection_name,
            qdrant_url=qdrant_url,
            expected_dimension=expected_dimension,
            expected_distance_metric=expected_distance_metric,
            blocker=dict(blocker),
        )

    return CollectionBootstrapResult(
        status="blocked",
        message="Collection bootstrap is not available for the current semantic blocker",
        profile_id=profile_id,
        collection_name=collection_name,
        normalized_collection_name=normalized_collection_name,
        qdrant_url=qdrant_url,
        expected_dimension=expected_dimension,
        expected_distance_metric=expected_distance_metric,
        blocker=dict(blocker) if blocker else None,
    )


def bootstrap_active_profile_collection(
    *,
    settings: Settings,
    profile: Optional[str] = None,
    timeout_s: Optional[float] = None,
) -> CollectionBootstrapResult:
    """Create the active-profile collection when the only blocker is missing collection."""
    timeout = float(
        timeout_s if timeout_s is not None else settings.semantic_preflight_timeout_seconds
    )
    namespace_resolver = SemanticNamespaceResolver()
    profile_check, _, selected_profile = resolve_profile_registry(settings, profile=profile)
    selected_profile_id = profile or settings.get_semantic_default_profile()
    qdrant_url = os.getenv("QDRANT_URL", f"http://{settings.qdrant_host}:{settings.qdrant_port}")

    if not profile_check.ok or selected_profile is None:
        blocker = _build_semantic_write_blocker([profile_check])
        return CollectionBootstrapResult(
            status="blocked",
            message="Semantic profile configuration must validate before collection bootstrap",
            profile_id=selected_profile_id,
            collection_name=None,
            normalized_collection_name=None,
            qdrant_url=qdrant_url,
            expected_dimension=None,
            expected_distance_metric=None,
            blocker=None if blocker is None else blocker.to_dict(),
        )

    collection_name = str(
        (selected_profile.build_metadata or {}).get("collection_name")
        or settings.semantic_collection_name
    )
    normalized_collection_name = namespace_resolver.normalize_collection_name(collection_name)
    vector_dimension = int(selected_profile.vector_dimension)
    distance_metric = namespace_resolver.normalize_distance_metric(selected_profile.distance_metric)

    qdrant_check = check_qdrant(qdrant_url, timeout_s=timeout)
    if not qdrant_check.ok:
        blocker = _build_semantic_write_blocker([qdrant_check])
        return CollectionBootstrapResult(
            status="blocked",
            message="Qdrant must be reachable before collection bootstrap can run",
            profile_id=selected_profile.profile_id,
            collection_name=collection_name,
            normalized_collection_name=normalized_collection_name,
            qdrant_url=qdrant_url,
            expected_dimension=vector_dimension,
            expected_distance_metric=distance_metric,
            blocker=None if blocker is None else blocker.to_dict(),
        )

    from qdrant_client import QdrantClient

    from mcp_server.utils.semantic_indexer import ensure_qdrant_collection

    try:
        client = QdrantClient(url=qdrant_url, api_key=get_qdrant_api_key(), timeout=timeout)
        ensure_result = ensure_qdrant_collection(
            client,
            collection_name=collection_name,
            expected_dimension=vector_dimension,
            distance_metric=distance_metric,
            allow_recreate=False,
        )
    except Exception as exc:
        return CollectionBootstrapResult(
            status="blocked",
            message=f"Failed to bootstrap the active semantic collection: {exc}",
            profile_id=selected_profile.profile_id,
            collection_name=collection_name,
            normalized_collection_name=normalized_collection_name,
            qdrant_url=qdrant_url,
            expected_dimension=vector_dimension,
            expected_distance_metric=distance_metric,
            blocker={
                "code": "collection_bootstrap_failed",
                "message": f"Failed to bootstrap the active semantic collection: {exc}",
            },
        )

    if ensure_result.status == "blocked":
        return CollectionBootstrapResult(
            status="blocked",
            message="Qdrant collection shape does not match the active semantic profile",
            profile_id=selected_profile.profile_id,
            collection_name=collection_name,
            normalized_collection_name=normalized_collection_name,
            qdrant_url=qdrant_url,
            expected_dimension=vector_dimension,
            expected_distance_metric=distance_metric,
            blocker={
                "code": "collection_shape_mismatch",
                "message": "Qdrant collection shape does not match the active semantic profile",
                "actual_dimension": ensure_result.actual_dimension,
                "actual_distance_metric": ensure_result.actual_distance_metric,
            },
        )

    return CollectionBootstrapResult(
        status=ensure_result.status,
        message=(
            "Created the active semantic collection for the selected profile"
            if ensure_result.status == "created"
            else "Reused the existing active semantic collection for the selected profile"
        ),
        profile_id=selected_profile.profile_id,
        collection_name=collection_name,
        normalized_collection_name=normalized_collection_name,
        qdrant_url=qdrant_url,
        expected_dimension=vector_dimension,
        expected_distance_metric=distance_metric,
        blocker=None,
    )


def run_semantic_preflight(
    settings: Settings,
    profile: Optional[str] = None,
    strict: bool = False,
    timeout_s: Optional[float] = None,
) -> SemanticPreflightReport:
    """Run semantic preflight checks and return structured report."""
    timeout = float(
        timeout_s if timeout_s is not None else settings.semantic_preflight_timeout_seconds
    )
    if not settings.semantic_search_enabled:
        return _disabled_report(strict)

    profile_check, _, selected_profile = resolve_profile_registry(settings, profile=profile)
    namespace_resolver = SemanticNamespaceResolver()

    selected_profile_id = profile or settings.get_semantic_default_profile()
    provider = ""
    embedding_base = ""
    embedding_model = ""
    embedding_api_key_env = "OPENAI_API_KEY"
    enrichment_base = ""
    enrichment_model = ""
    enrichment_api_key_env = "OPENAI_API_KEY"
    collection_name = settings.semantic_collection_name
    vector_dimension = None
    distance_metric = None

    if selected_profile is not None:
        provider = str(selected_profile.provider).lower()
        build_metadata = selected_profile.build_metadata or {}
        embedding_base = str(
            build_metadata.get("embedding_api_base")
            or build_metadata.get("openai_api_base")
            or settings.openai_api_base
            or "http://localhost:8001/v1"
        )
        embedding_model = str(
            build_metadata.get("embedding_model_name") or selected_profile.model_name
        )
        embedding_api_key_env = str(
            build_metadata.get("embedding_api_key_env")
            or build_metadata.get("openai_api_key_env")
            or "OPENAI_API_KEY"
        )
        enrichment_base = str(build_metadata.get("enrichment_api_base") or "")
        enrichment_model = str(build_metadata.get("enrichment_model_name") or "")
        enrichment_api_key_env = str(
            build_metadata.get("enrichment_api_key_env") or embedding_api_key_env
        )
        collection_name = str(
            build_metadata.get("collection_name") or settings.semantic_collection_name
        )
        vector_dimension = int(selected_profile.vector_dimension)
        distance_metric = str(selected_profile.distance_metric)

    if not profile_check.ok or selected_profile is None:
        enrichment_check = _build_unavailable_result(
            name="enrichment_chat",
            message="Enrichment chat smoke is unavailable until semantic profiles validate",
            failure_class="profile_registry_invalid",
            fixes=["Repair semantic profile configuration before re-running preflight"],
        )
        embedding_check = _build_unavailable_result(
            name="embedding_vector",
            message="Embedding smoke is unavailable until semantic profiles validate",
            failure_class="profile_registry_invalid",
            fixes=["Repair semantic profile configuration before re-running preflight"],
        )
        qdrant_url = os.getenv(
            "QDRANT_URL", f"http://{settings.qdrant_host}:{settings.qdrant_port}"
        )
        qdrant_check = check_qdrant(qdrant_url, timeout_s=timeout)
        collection_check = _build_unavailable_result(
            name="qdrant_collection",
            message="Qdrant collection validation is unavailable until semantic profiles validate",
            failure_class="profile_registry_invalid",
            fixes=["Repair semantic profile configuration before re-running preflight"],
        )
    else:
        if provider in {"openai_compatible", "openai-compatible", "openai", "qwen", "vllm"}:
            embedding_check = check_embedding_smoke(
                base_url=embedding_base,
                model=embedding_model,
                api_key_env=embedding_api_key_env,
                expected_dimension=int(vector_dimension or 0),
                timeout_s=timeout,
            )
        else:
            embedding_check = check_voyage_key_present()

        enrichment_check = check_enrichment_chat(
            base_url=enrichment_base,
            model=enrichment_model,
            api_key_env=enrichment_api_key_env,
            timeout_s=timeout,
        )
        qdrant_url = os.getenv(
            "QDRANT_URL", f"http://{settings.qdrant_host}:{settings.qdrant_port}"
        )
        qdrant_check = check_qdrant(qdrant_url, timeout_s=timeout)
        if qdrant_check.ok:
            collection_check = check_qdrant_collection(
                qdrant_url=qdrant_url,
                collection_name=collection_name,
                expected_dimension=int(vector_dimension or 0),
                expected_distance=str(distance_metric or "cosine"),
                timeout_s=timeout,
                namespace_resolver=namespace_resolver,
            )
        else:
            collection_check = CheckResult(
                name="qdrant_collection",
                status=ServiceStatus.UNREACHABLE,
                message="Qdrant collection validation skipped because Qdrant is unreachable",
                details={
                    "qdrant_url": qdrant_url,
                    "collection_name": collection_name,
                    "normalized_collection_name": namespace_resolver.normalize_collection_name(
                        collection_name
                    ),
                    "failure_class": "qdrant_unreachable",
                },
                fixes=list(qdrant_check.fixes),
            )

    checks = [profile_check, enrichment_check, embedding_check, qdrant_check, collection_check]
    blocker = _build_semantic_write_blocker(checks)
    can_write_semantic_vectors = blocker is None
    warnings: List[str] = [check.message for check in checks if not check.ok]

    try:
        import flashrank  # noqa: F401
    except ImportError:
        warnings.append(
            "flashrank not installed — reranking disabled. "
            "Install with: pip install flashrank  (or pip install -r requirements-semantic.txt)"
        )

    if strict and not can_write_semantic_vectors:
        warnings.append("Strict semantic mode enabled: semantic vector writes remain fail-closed")

    return SemanticPreflightReport(
        overall_ready=all(check.ok for check in checks),
        strict_mode=strict,
        qdrant=qdrant_check,
        embedding=embedding_check,
        profiles=profile_check,
        enrichment=enrichment_check,
        collection=collection_check,
        can_write_semantic_vectors=can_write_semantic_vectors,
        blocker=blocker,
        warnings=warnings,
        effective_config={
            "semantic_enabled": settings.semantic_search_enabled,
            "selected_profile": selected_profile_id,
            "provider": provider,
            "qdrant_url": qdrant_url,
            "vector_dimension": vector_dimension,
            "distance_metric": namespace_resolver.normalize_distance_metric(distance_metric),
            "collection_name": collection_name,
            "normalized_collection_name": namespace_resolver.normalize_collection_name(
                collection_name
            ),
            "embedding": {
                "base_url": embedding_base,
                "model": embedding_model,
                "api_key_env": embedding_api_key_env,
                "api_key_present": bool(os.getenv(embedding_api_key_env)),
            },
            "enrichment": {
                "base_url": enrichment_base,
                "model": enrichment_check.details.get("configured_model", enrichment_model),
                "configured_model": enrichment_check.details.get(
                    "configured_model", enrichment_model
                ),
                "effective_model": enrichment_check.details.get(
                    "effective_model", enrichment_model
                ),
                "resolution_strategy": enrichment_check.details.get("resolution_strategy"),
                "considered_model_ids": enrichment_check.details.get("considered_model_ids", []),
                "models_probe_verified": enrichment_check.details.get(
                    "models_probe_verified", False
                ),
                "api_key_env": enrichment_api_key_env,
                "api_key_present": bool(os.getenv(enrichment_api_key_env)),
            },
            "autostart_qdrant": settings.semantic_autostart_qdrant,
            "strict_mode": strict,
            "timeout_seconds": timeout,
        },
    )
