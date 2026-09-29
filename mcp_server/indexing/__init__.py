from .friction import FRICTION_PATTERN_CONFIGS, FrictionPatternConfig, extract_friction_markers
from .lock_registry import IndexingLockRegistry, lock_registry
from .source_metadata import (
    FRICTION_CATEGORIES,
    FRICTION_SOURCE_TYPE,
    HISTORY_ISSUE_TYPES,
    HISTORY_SOURCE_TYPE,
    SEARCH_SOURCE_METADATA_VERSION,
    FrictionCategory,
    FrictionMarker,
    HistoryIssueRecord,
    HistoryIssueType,
    SearchSourceMetadataEnvelope,
    SourceType,
    build_source_metadata,
    extract_matching_source_metadata,
    merge_source_metadata,
    normalize_friction_category,
    normalize_history_issue_record,
)

__all__ = [
    "FRICTION_CATEGORIES",
    "FRICTION_PATTERN_CONFIGS",
    "FRICTION_SOURCE_TYPE",
    "GitHubIssueFetchOptions",
    "HISTORY_ISSUE_TYPES",
    "HISTORY_SOURCE_TYPE",
    "IndexingLockRegistry",
    "FrictionCategory",
    "FrictionMarker",
    "FrictionPatternConfig",
    "HistoryIssueRecord",
    "HistoryIssueType",
    "SEARCH_SOURCE_METADATA_VERSION",
    "SearchSourceMetadataEnvelope",
    "SourceType",
    "build_source_metadata",
    "extract_issue_learnings",
    "extract_friction_markers",
    "extract_matching_source_metadata",
    "fetch_github_issues",
    "issue_history_dedupe_key",
    "lock_registry",
    "merge_source_metadata",
    "normalize_github_issue",
    "normalize_history_issue_record",
    "normalize_friction_category",
    "ReindexCheckpoint",
    "save",
    "load",
    "clear",
]

_CHECKPOINT_NAMES = {"ReindexCheckpoint", "save", "load", "clear"}
_GITHUB_ISSUE_NAMES = {
    "GitHubIssueFetchOptions",
    "extract_issue_learnings",
    "fetch_github_issues",
    "issue_history_dedupe_key",
    "normalize_github_issue",
}


def __getattr__(name: str):
    if name in _GITHUB_ISSUE_NAMES:
        from . import github_issues

        return getattr(github_issues, name)
    if name in _CHECKPOINT_NAMES:
        from . import checkpoint

        return getattr(checkpoint, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
