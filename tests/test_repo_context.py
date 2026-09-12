"""Tests for RepoContext dataclass (SL-1)."""

from __future__ import annotations

import typing
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_repo_info(**overrides):
    """Create a minimal RepositoryInfo for testing."""
    from datetime import datetime

    from mcp_server.storage.multi_repo_manager import RepositoryInfo

    defaults = dict(
        repository_id="abcdef1234567890",
        name="test-repo",
        path=Path("/tmp/test-repo"),
        index_path=Path("/tmp/test-repo/.index/code_index.db"),
        language_stats={},
        total_files=0,
        total_symbols=0,
        indexed_at=datetime(2024, 1, 1),
        priority=0,
        tracked_branch="main",
    )
    defaults.update(overrides)
    return RepositoryInfo(**defaults)


def _make_stub_store():
    """Return a MagicMock standing in for SQLiteStore."""
    return MagicMock(name="SQLiteStore")


# ---------------------------------------------------------------------------
# Test 1: field set + frozen
# ---------------------------------------------------------------------------


def test_is_dataclass_and_frozen():
    from mcp_server.core.repo_context import RepoContext

    assert is_dataclass(RepoContext)
    assert RepoContext.__dataclass_params__.frozen is True


def test_field_names():
    from mcp_server.core.repo_context import RepoContext

    expected = {
        "repo_id",
        "sqlite_store",
        "workspace_root",
        "tracked_branch",
        "registry_entry",
        "requested_path",
        "staging",
    }
    assert {f.name for f in fields(RepoContext)} == expected
    assert len(fields(RepoContext)) == 7


# ---------------------------------------------------------------------------
# Test 2: construction + frozen enforcement
# ---------------------------------------------------------------------------


def test_construction_and_attribute_access():
    from dataclasses import FrozenInstanceError

    from mcp_server.core.repo_context import RepoContext

    info = _make_repo_info()
    store = _make_stub_store()
    ctx = RepoContext(
        repo_id="abcdef1234567890",
        sqlite_store=store,
        workspace_root=Path("/tmp/test-repo/src"),
        tracked_branch="main",
        registry_entry=info,
    )

    assert ctx.repo_id == "abcdef1234567890"
    assert ctx.sqlite_store is store
    assert ctx.workspace_root == Path("/tmp/test-repo/src")
    assert ctx.tracked_branch == "main"
    assert ctx.registry_entry is info


def test_frozen_raises_on_direct_assignment():
    from dataclasses import FrozenInstanceError

    from mcp_server.core.repo_context import RepoContext

    info = _make_repo_info()
    ctx = RepoContext(
        repo_id="abcdef1234567890",
        sqlite_store=_make_stub_store(),
        workspace_root=Path("/tmp/test-repo"),
        tracked_branch="main",
        registry_entry=info,
    )

    with pytest.raises(FrozenInstanceError):
        ctx.repo_id = "x"  # type: ignore[misc]


def test_replace_returns_new_instance():
    from mcp_server.core.repo_context import RepoContext

    info = _make_repo_info()
    store = _make_stub_store()
    ctx = RepoContext(
        repo_id="abcdef1234567890",
        sqlite_store=store,
        workspace_root=Path("/tmp/test-repo"),
        tracked_branch="main",
        registry_entry=info,
    )

    new_ctx = replace(ctx, repo_id="new_id_0000000000000000")
    assert new_ctx.repo_id == "new_id_0000000000000000"
    assert new_ctx is not ctx
    # Peer fields use identity equality (same objects)
    assert new_ctx.sqlite_store is store
    assert new_ctx.workspace_root is ctx.workspace_root
    assert new_ctx.tracked_branch is ctx.tracked_branch
    assert new_ctx.registry_entry is info


# ---------------------------------------------------------------------------
# Test 3: mutation-through-reference
# ---------------------------------------------------------------------------


def test_mutation_through_registry_entry_reference():
    """Mutating the RepositoryInfo object is visible via ctx.registry_entry."""
    from mcp_server.core.repo_context import RepoContext

    info = _make_repo_info(priority=0)
    ctx = RepoContext(
        repo_id="abcdef1234567890",
        sqlite_store=_make_stub_store(),
        workspace_root=Path("/tmp/test-repo"),
        tracked_branch="main",
        registry_entry=info,
    )

    info.priority = 99
    assert ctx.registry_entry.priority == 99


# ---------------------------------------------------------------------------
# Test 4: sqlite_store non-Optional type hint
# ---------------------------------------------------------------------------


def test_sqlite_store_type_hint_is_not_optional():
    """Type annotation for sqlite_store must not include None/Optional."""
    from mcp_server.core.repo_context import RepoContext

    hints = typing.get_type_hints(RepoContext, include_extras=True)
    hint_str = str(hints["sqlite_store"])
    # Must reference SQLiteStore; must NOT allow None
    assert "SQLiteStore" in hint_str
    assert "None" not in hint_str
    assert "Optional" not in hint_str
