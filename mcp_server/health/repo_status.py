"""Build per-repository health rows for the get_status surface."""

from pathlib import Path
from typing import Optional

from mcp_server.health.repository_readiness import (
    ReadinessClassifier,
    RepositoryReadiness,
    RepositoryReadinessState,
    SemanticReadiness,
    SemanticReadinessState,
)
from mcp_server.storage.multi_repo_manager import RepositoryInfo


def _normalize_staleness_reason(
    repo_info: RepositoryInfo,
    readiness: RepositoryReadiness,
    *,
    index_path_exists: bool,
    git_dir_exists: bool,
) -> str | None:
    if getattr(repo_info, "staleness_reason", None) == "partial_index_failure":
        return "partial_index_failure"
    if not git_dir_exists:
        return "missing_git_dir"
    if not index_path_exists:
        return "missing_index"
    if readiness.state != RepositoryReadinessState.READY:
        return readiness.state.value
    return None


def _classify_rollout_status(
    readiness: RepositoryReadiness,
    artifact_health: str | None,
    staleness_reason: str | None,
) -> str:
    if staleness_reason == "partial_index_failure":
        return "partial_index_failure"
    if (
        readiness.state == RepositoryReadinessState.WRONG_BRANCH
        or artifact_health == "wrong_branch"
    ):
        return "wrong_branch"
    if artifact_health == "publish_failed":
        return "publish_failed"
    if readiness.state != RepositoryReadinessState.READY:
        return readiness.state.value
    if artifact_health in {"ready", "published"}:
        return "ready"
    if artifact_health == "stale_commit":
        return "stale_commit"
    return "local_only"


def _rollout_remediation(
    rollout_status: str,
    readiness: RepositoryReadiness,
) -> str | None:
    if rollout_status == "ready":
        return None
    if rollout_status == "local_only":
        return (
            "Local runtime state is usable, but durable multi-repo rollout is not confirmed. "
            "Publish artifacts or fetch/reconcile workspace state before treating this repo as rollout-ready."
        )
    if rollout_status == "publish_failed":
        return (
            "Artifact publication failed. Re-run the publish path after fixing release or workflow access, "
            "then confirm workspace status reports ready."
        )
    if rollout_status == "partial_index_failure":
        return (
            "A required incremental mutation failed. Run a full reindex or hydrate a known-good artifact, "
            "then reconcile workspace status before trusting indexed results."
        )
    if rollout_status == "missing_index":
        return "Run reindex or fetch the latest artifact, then reconcile workspace status."
    if rollout_status == "stale_commit":
        return (
            "The local index is behind HEAD. Run reindex or fetch the matching artifact, "
            "then reconcile workspace status."
        )
    if rollout_status == "index_building":
        return "Wait for indexing to finish, then re-check repository or workspace status."
    return readiness.remediation


def build_health_row(
    repo_info: RepositoryInfo,
    readiness: Optional[RepositoryReadiness] = None,
    features: Optional[dict] = None,
    semantic_readiness: Optional[SemanticReadiness] = None,
    semantic_preflight: Optional[dict] = None,
) -> dict:
    if readiness is None:
        readiness = ReadinessClassifier.classify_registered(repo_info)
    if semantic_readiness is None:
        semantic_readiness = SemanticReadiness(
            state=SemanticReadinessState.ENRICHMENT_UNAVAILABLE,
            remediation="Semantic readiness is unavailable for this status surface.",
        )

    index_path_exists = bool(repo_info.index_path) and Path(repo_info.index_path).exists()
    git_dir_exists = bool(repo_info.git_common_dir) and Path(repo_info.git_common_dir).exists()
    artifact_health = getattr(repo_info, "artifact_health", None)
    staleness = _normalize_staleness_reason(
        repo_info,
        readiness,
        index_path_exists=index_path_exists,
        git_dir_exists=git_dir_exists,
    )
    rollout_status = _classify_rollout_status(readiness, artifact_health, staleness)
    rollout_remediation = _rollout_remediation(rollout_status, readiness)
    query_status = "ready" if readiness.ready else "index_unavailable"
    base_features = features or {
        "lexical": {"status": "available" if readiness.ready else "unavailable"},
        "semantic": {"status": "unavailable", "reason": "runtime_status_unavailable"},
        "graph": {"status": "unavailable", "reason": "runtime_status_unavailable"},
        "plugins": {"status": "unavailable", "reason": "runtime_status_unavailable"},
        "cross_repo": {"status": "unavailable", "reason": "runtime_status_unavailable"},
    }
    semantic_feature = dict(base_features.get("semantic") or {})
    runtime_status = semantic_feature.get("status")
    runtime_reason = semantic_feature.get("reason")
    semantic_feature.update(
        {
            "status": "available" if semantic_readiness.ready else "unavailable",
            "reason": semantic_readiness.code or runtime_reason,
            "runtime_status": runtime_status,
            "runtime_reason": runtime_reason,
            "readiness": semantic_readiness.to_dict(),
            "preflight": semantic_preflight
            or {
                "overall_ready": False,
                "can_write_semantic_vectors": False,
                "blocker": {
                    "code": "runtime_preflight_unavailable",
                    "message": "Active-profile semantic preflight has not been computed for this status surface.",
                    "failing_checks": [],
                    "remediation": [
                        "Run `setup semantic` or `mcp-index index check-semantic` for the active profile.",
                    ],
                    "can_write_semantic_vectors": False,
                },
            },
        }
    )
    base_features["semantic"] = semantic_feature

    return {
        "repo_id": repo_info.repository_id,
        "tracked_branch": repo_info.tracked_branch,
        "index_path_exists": index_path_exists,
        "git_dir_exists": git_dir_exists,
        "last_indexed_commit": repo_info.last_indexed_commit,
        "artifact_health": artifact_health,
        "staleness_reason": staleness,
        "readiness": readiness.state.value,
        "ready": readiness.ready,
        "readiness_code": readiness.code,
        "remediation": readiness.remediation,
        "semantic_readiness": semantic_readiness.state.value,
        "semantic_ready": semantic_readiness.ready,
        "semantic_readiness_code": semantic_readiness.code,
        "semantic_remediation": semantic_readiness.remediation,
        "rollout_status": rollout_status,
        "rollout_remediation": rollout_remediation,
        "query_status": query_status,
        "query_remediation": (
            None
            if readiness.ready
            else 'Use native search or follow the readiness remediation; query tools stay fail-closed with safe_fallback: "native_search".'
        ),
        "features": base_features,
        # Language coverage of the published generation (#117): additive, the
        # readiness state above stays ``ready`` while coverage is degraded.
        "coverage": readiness.coverage,
        "coverage_status": (readiness.coverage or {}).get("state"),
    }
