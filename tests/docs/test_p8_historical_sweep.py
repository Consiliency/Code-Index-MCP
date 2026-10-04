"""Tests for P8 SL-3: historical docs sweep — banner, delete, triage log."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
SWEEP_DIRS = [
    REPO_ROOT / "docs" / "implementation",
    REPO_ROOT / "docs" / "status",
    REPO_ROOT / "docs" / "validation",
]
TRIAGE_LOG = REPO_ROOT / "docs" / "HISTORICAL-ARTIFACTS-TRIAGE.md"
BANNER_REGEX = re.compile(
    r"^> \*\*Historical artifact \u2014 as-of 2026-04-(18|24|25), may not reflect current behavior\*\*\s*$"
)
VALID_AS_OF_DATES = {"2026-04-18", "2026-04-24", "2026-04-25"}
VALID_DISPOSITIONS = {"deleted", "bannered", "rewritten"}


def _get_deleted_set() -> set[str]:
    # Frozen historical deletions survive merge, shallow clones, and source archives.
    return {
        "docs/status/PMCP_FLEET_PILOT.md",
        "docs/status/pmcp-pilot/query-evidence.json",
        "docs/status/pmcp-pilot/repository-readiness-evidence.json",
        "docs/status/pmcp-pilot/startup-evidence.json",
    }


def test_historical_deleted_paths_remain_absent():
    assert all(not (REPO_ROOT / path).exists() for path in _get_deleted_set())


def _get_present_set() -> set[str]:
    present = set()
    for d in SWEEP_DIRS:
        for f in d.glob("*.md"):
            lines = f.read_text(encoding="utf-8").splitlines()
            if lines and BANNER_REGEX.match(lines[0]):
                present.add(str(f.relative_to(REPO_ROOT)))
    return present


def test_banner_on_all_non_deleted_files():
    deleted = _get_deleted_set()
    present = _get_present_set()
    failures = []
    for rel_path in sorted(present):
        abs_path = REPO_ROOT / rel_path
        lines = abs_path.read_text(encoding="utf-8").splitlines()
        if not lines:
            failures.append(f"{rel_path}: file is empty")
            continue
        if not BANNER_REGEX.match(lines[0]):
            failures.append(f"{rel_path}: line 1 does not match banner regex. Got: {lines[0]!r}")
    assert not failures, "Banner check failures:\n" + "\n".join(failures)


def test_triage_log_exists():
    assert TRIAGE_LOG.exists(), f"Triage log not found: {TRIAGE_LOG}"


def test_triage_log_header_row():
    content = TRIAGE_LOG.read_text(encoding="utf-8")
    assert (
        "| Path | Disposition | Rationale | As-of |" in content
    ), "Triage log missing required header row: '| Path | Disposition | Rationale | As-of |'"


def test_triage_log_has_three_h3_subsections():
    content = TRIAGE_LOG.read_text(encoding="utf-8")
    h3_sections = re.findall(r"^### ", content, re.MULTILINE)
    assert (
        len(h3_sections) == 3
    ), f"Expected 3 H3 subsections in triage log, found {len(h3_sections)}"


def test_triage_log_row_count_matches_union():
    deleted = _get_deleted_set()
    present = _get_present_set()
    union = deleted | present
    expected_count = len(union)

    content = TRIAGE_LOG.read_text(encoding="utf-8")
    # Count data rows: lines starting with | that contain a path (not header or separator)
    data_rows = [
        line
        for line in content.splitlines()
        if line.startswith("| ")
        and not line.startswith("| Path")
        and "---" not in line
        and line.strip() != "|"
    ]
    actual_count = len(data_rows)
    assert actual_count == expected_count, (
        f"Triage log row count {actual_count} != union size {expected_count}. "
        f"Deleted: {len(deleted)}, Present: {len(present)}"
    )


def test_triage_log_paths_match_union():
    deleted = _get_deleted_set()
    present = _get_present_set()
    union = deleted | present

    content = TRIAGE_LOG.read_text(encoding="utf-8")
    # Extract paths from triage rows
    triage_paths = set()
    for line in content.splitlines():
        if line.startswith("| ") and not line.startswith("| Path") and "---" not in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2:
                path_cell = parts[1].strip()
                # Remove backtick formatting if present
                path_cell = path_cell.strip("`")
                if path_cell:
                    triage_paths.add(path_cell)

    phantom = triage_paths - union
    missing = union - triage_paths
    errors = []
    if phantom:
        errors.append(f"Phantom rows (in triage but not in union): {sorted(phantom)}")
    if missing:
        errors.append(f"Missing rows (in union but not in triage): {sorted(missing)}")
    assert not errors, "\n".join(errors)


def test_triage_log_valid_dispositions():
    content = TRIAGE_LOG.read_text(encoding="utf-8")
    failures = []
    for line in content.splitlines():
        if line.startswith("| ") and not line.startswith("| Path") and "---" not in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                disposition = parts[2].strip()
                if disposition and disposition not in VALID_DISPOSITIONS:
                    failures.append(f"Invalid disposition {disposition!r} in row: {line!r}")
    assert not failures, "Invalid dispositions:\n" + "\n".join(failures)


def test_triage_log_asof_date():
    content = TRIAGE_LOG.read_text(encoding="utf-8")
    failures = []
    for line in content.splitlines():
        if line.startswith("| ") and not line.startswith("| Path") and "---" not in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 5:
                asof = parts[4].strip()
                if asof and asof not in VALID_AS_OF_DATES:
                    failures.append(f"Wrong As-of date {asof!r} in row: {line!r}")
    assert not failures, "As-of date failures:\n" + "\n".join(failures)
