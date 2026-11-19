from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Literal, NotRequired

from typing_extensions import TypedDict

SEARCH_SOURCE_METADATA_VERSION = "search_source_metadata.v1"
FRICTION_SOURCE_TYPE = "friction"
HISTORY_SOURCE_TYPE = "history"
FRICTION_CATEGORIES = (
    "extraction_hint",
    "fixme",
    "hack",
    "todo",
    "workaround",
    "wish",
)
HISTORY_ISSUE_TYPES = (
    "issue",
    "phase_complete",
    "reflection",
    "retrospective",
)

SourceType = Literal["friction", "history"]
FrictionCategory = Literal[
    "extraction_hint",
    "fixme",
    "hack",
    "todo",
    "workaround",
    "wish",
]
HistoryIssueType = Literal["issue", "phase_complete", "reflection", "retrospective"]


class FrictionMarker(TypedDict):
    source_type: Literal["friction"]
    category: FrictionCategory
    line: int
    description: str
    pattern: str
    reference: NotRequired[str]


class HistoryIssueRecord(TypedDict):
    source_type: Literal["history"]
    type: HistoryIssueType
    repo: str
    number: int
    title: str
    labels: list[str]
    state: str
    created_at: str
    updated_at: str
    url: str
    summary: str
    learnings: list[str]
    closed_at: NotRequired[str]


SearchSourceRecord = FrictionMarker | HistoryIssueRecord


class SearchSourceMetadataEnvelope(TypedDict):
    schema_version: Literal["search_source_metadata.v1"]
    records: list[SearchSourceRecord]


def _normalize_history_label(label: str) -> str:
    return " ".join(label.strip().lower().split())


def _normalize_history_repo(repo: str) -> str:
    return repo.strip().lower()


def _normalize_history_issue_type(issue_type: str) -> HistoryIssueType:
    normalized = issue_type.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized not in HISTORY_ISSUE_TYPES:
        raise ValueError(f"Unknown history issue type: {issue_type}")
    return normalized  # type: ignore[return-value]


def _normalize_string_list(values: Iterable[Any]) -> list[str]:
    normalized = {str(value).strip() for value in values if str(value).strip()}
    return sorted(normalized, key=str.lower)


def _sort_key(record: SearchSourceRecord) -> tuple[Any, ...]:
    if record["source_type"] == FRICTION_SOURCE_TYPE:
        friction = record
        return (
            0,
            int(friction["line"]),
            friction["category"],
            friction["description"],
            friction["pattern"],
            friction.get("reference", ""),
        )

    history = record
    return (
        1,
        history["repo"],
        int(history["number"]),
        history["type"],
        history["updated_at"],
        history["title"],
        "|".join(history["labels"]),
        history["summary"],
        "|".join(history["learnings"]),
    )


def normalize_friction_category(category: str) -> FrictionCategory:
    normalized = category.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized not in FRICTION_CATEGORIES:
        raise ValueError(f"Unknown friction category: {category}")
    return normalized  # type: ignore[return-value]


def normalize_friction_marker(record: dict[str, Any]) -> FrictionMarker:
    normalized: FrictionMarker = {
        "source_type": FRICTION_SOURCE_TYPE,
        "category": normalize_friction_category(str(record["category"])),
        "line": int(record["line"]),
        "description": str(record["description"]).strip(),
        "pattern": str(record["pattern"]).strip(),
    }
    reference = record.get("reference")
    if reference:
        normalized["reference"] = str(reference).strip()
    return normalized


def normalize_history_issue_record(record: dict[str, Any]) -> HistoryIssueRecord:
    labels_raw = record.get("labels") or []
    if not isinstance(labels_raw, list):
        raise TypeError("history labels must be a list")

    normalized: HistoryIssueRecord = {
        "source_type": HISTORY_SOURCE_TYPE,
        "type": _normalize_history_issue_type(str(record["type"])),
        "repo": _normalize_history_repo(str(record["repo"])),
        "number": int(record["number"]),
        "title": str(record["title"]).strip(),
        "labels": sorted(
            {_normalize_history_label(str(label)) for label in labels_raw if str(label).strip()}
        ),
        "state": str(record["state"]).strip().lower(),
        "created_at": str(record["created_at"]).strip(),
        "updated_at": str(record["updated_at"]).strip(),
        "url": str(record["url"]).strip(),
        "summary": str(record.get("summary") or record["title"]).strip(),
        "learnings": _normalize_string_list(record.get("learnings") or []),
    }
    closed_at = record.get("closed_at")
    if closed_at not in (None, ""):
        normalized["closed_at"] = str(closed_at).strip()
    return normalized


def normalize_source_record(record: dict[str, Any]) -> SearchSourceRecord:
    source_type = str(record.get("source_type") or "").strip().lower()
    if source_type == FRICTION_SOURCE_TYPE:
        return normalize_friction_marker(record)
    if source_type == HISTORY_SOURCE_TYPE:
        return normalize_history_issue_record(record)
    raise ValueError(f"Unknown source type: {record.get('source_type')}")


def build_source_metadata(records: Iterable[dict[str, Any]]) -> SearchSourceMetadataEnvelope | None:
    normalized = [normalize_source_record(record) for record in records]
    if not normalized:
        return None
    normalized.sort(key=_sort_key)
    return {
        "schema_version": SEARCH_SOURCE_METADATA_VERSION,
        "records": normalized,
    }


def merge_source_metadata(
    metadata: dict[str, Any] | None,
    records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    merged = deepcopy(metadata or {})
    envelope = build_source_metadata(records)
    if envelope is None:
        merged.pop("source_metadata", None)
        return merged
    merged["source_metadata"] = envelope
    return merged


def extract_matching_source_metadata(
    metadata: dict[str, Any] | None,
    *,
    source_type: str | None = None,
    friction_categories: Iterable[str] | None = None,
    history_labels: Iterable[str] | None = None,
    history_repos: Iterable[str] | None = None,
) -> SearchSourceMetadataEnvelope | None:
    if not metadata:
        return None
    envelope = metadata.get("source_metadata")
    if not isinstance(envelope, dict):
        return None
    if envelope.get("schema_version") != SEARCH_SOURCE_METADATA_VERSION:
        return None
    records = envelope.get("records")
    if not isinstance(records, list):
        return None

    normalized_categories = None
    if friction_categories is not None:
        normalized_categories = {
            normalize_friction_category(category) for category in friction_categories
        }

    normalized_history_labels = None
    if history_labels is not None:
        normalized_history_labels = {
            _normalize_history_label(label) for label in history_labels if str(label).strip()
        }

    normalized_history_repos = None
    if history_repos is not None:
        normalized_history_repos = {
            _normalize_history_repo(repo) for repo in history_repos if str(repo).strip()
        }

    matched: list[SearchSourceRecord] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            normalized = normalize_source_record(record)
        except KeyError, TypeError, ValueError:
            continue
        if source_type and normalized["source_type"] != source_type:
            continue
        if normalized["source_type"] == FRICTION_SOURCE_TYPE:
            if normalized_categories and normalized["category"] not in normalized_categories:
                continue
        if normalized["source_type"] == HISTORY_SOURCE_TYPE:
            if normalized_history_labels and not (
                set(normalized["labels"]) & normalized_history_labels
            ):
                continue
            if normalized_history_repos and normalized["repo"] not in normalized_history_repos:
                continue
        matched.append(normalized)

    if not matched:
        return None
    matched.sort(key=_sort_key)
    return {
        "schema_version": SEARCH_SOURCE_METADATA_VERSION,
        "records": matched,
    }
