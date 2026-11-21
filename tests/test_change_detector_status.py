"""Git name-status parsing in ChangeDetector (Consiliency/Code-Index-MCP#144)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcp_server.indexing.change_detector import ChangeDetector, ChangeInventoryError


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def detector(tmp_path):
    return ChangeDetector(tmp_path)


@pytest.mark.parametrize(
    "line, expected",
    [
        ("A\tsrc/a.py", ("src/a.py", "added", None)),
        ("M\tsrc/a.py", ("src/a.py", "modified", None)),
        ("D\tsrc/a.py", ("src/a.py", "deleted", None)),
        ("R095\tsrc/a.py\tsrc/b.py", ("src/b.py", "renamed", "src/a.py")),
        # Type change: the consumer re-derives whether the new entry is indexable.
        ("T\tsrc/a.py", ("src/a.py", "modified", None)),
        # Unmerged (index/worktree diffs only): content must be re-read.
        ("U\tsrc/a.py", ("src/a.py", "modified", None)),
        # Copy: only the destination is new.
        ("C075\tsrc/a.py\tsrc/b.py", ("src/b.py", "added", None)),
    ],
)
def test_status_lines_classify(detector, line, expected):
    change = detector._parse_status_line(line)
    assert change is not None
    assert (change.path, change.change_type, change.old_path) == expected


@pytest.mark.parametrize(
    "line", ["T\tLICENSE", "U\tLICENSE", "C100\tsrc/a.py\tLICENSE", "R100\tsrc/a.py", ""]
)
def test_unsupported_or_malformed_lines_are_ignored(detector, line):
    assert detector._parse_status_line(line) is None


@pytest.mark.parametrize("line", ["X\tsrc/a.py", "B\tsrc/a.py", "Z\tLICENSE"])
def test_unclassifiable_status_refuses_the_inventory(detector, line):
    with pytest.raises(ChangeInventoryError):
        detector._parse_status_line(line)


def test_committed_type_changes_are_reported(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "hello.py").write_text("print('hi')\n")
    (repo / "lib.py").write_text("x = 1\n")
    (repo / "link.py").symlink_to("hello.py")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    base = _git(repo, "rev-parse", "HEAD").strip()

    (repo / "lib.py").unlink()
    (repo / "lib.py").symlink_to("hello.py")
    (repo / "link.py").unlink()
    (repo / "link.py").write_text("y = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "swap types")

    changes = ChangeDetector(repo).get_changes_since_commit(base)

    assert sorted((c.path, c.change_type) for c in changes) == [
        ("lib.py", "modified"),
        ("link.py", "modified"),
    ]
