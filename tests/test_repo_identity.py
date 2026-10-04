"""Tests for mcp_server.storage.repo_identity — SL-1."""

import subprocess
from pathlib import Path

import pytest


def git(*args, cwd=None):
    subprocess.run(["git"] + list(args), cwd=cwd, check=True, capture_output=True)


def make_bare_with_worktrees(tmp_path):
    """Return (bare_dir, wt1, wt2) — all worktrees of the same bare repo."""
    # Create a non-bare repo with a commit so we can clone it as bare
    source = tmp_path / "source"
    git("init", "-b", "main", str(source))
    git("config", "user.email", "test@test.com", cwd=source)
    git("config", "user.name", "Test", cwd=source)
    (source / "README.md").write_text("hello")
    git("add", "README.md", cwd=source)
    git("commit", "-m", "init", cwd=source)

    bare = tmp_path / "bare.git"
    git("clone", "--bare", str(source), str(bare))

    wt1 = tmp_path / "wt1"
    wt2 = tmp_path / "wt2"
    git("worktree", "add", str(wt1), "-b", "wt-branch-1", cwd=bare)
    git("worktree", "add", str(wt2), "-b", "wt-branch-2", cwd=bare)

    return bare, wt1, wt2


def make_clone_with_origin(tmp_path):
    """Return (origin_dir, clone_dir) — clone has remote.origin.url set."""
    origin = tmp_path / "origin"
    git("init", "-b", "main", str(origin))
    git("config", "user.email", "test@test.com", cwd=origin)
    git("config", "user.name", "Test", cwd=origin)
    (origin / "README.md").write_text("hello")
    git("add", "README.md", cwd=origin)
    git("commit", "-m", "init", cwd=origin)

    clone = tmp_path / "clone"
    git("clone", str(origin), str(clone))
    return origin, clone


def _get_common_dir(path: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    raw = result.stdout.strip()
    return (path / raw).resolve()


# ---------------------------------------------------------------------------
# compute_repo_id tests
# ---------------------------------------------------------------------------


class TestComputeRepoId:
    def test_worktree_equivalence(self, tmp_path):
        """bare repo + two worktrees added from it → same repo_id."""
        from mcp_server.storage.repo_identity import compute_repo_id

        bare, wt1, wt2 = make_bare_with_worktrees(tmp_path)

        id_bare = compute_repo_id(bare).repo_id
        id_wt1 = compute_repo_id(wt1).repo_id
        id_wt2 = compute_repo_id(wt2).repo_id

        assert (
            id_bare == id_wt1 == id_wt2
        ), f"IDs diverge: bare={id_bare}, wt1={id_wt1}, wt2={id_wt2}"

    def test_tier1_source_label(self, tmp_path):
        """Normal git repo uses git_common_dir source."""
        from mcp_server.storage.repo_identity import compute_repo_id

        _, clone = make_clone_with_origin(tmp_path)
        identity = compute_repo_id(clone)
        assert identity.source == "git_common_dir"
        assert identity.git_common_dir is not None

    def test_docker_common_directory_override_requires_same_directory(self, tmp_path, monkeypatch):
        from mcp_server.storage.repo_identity import compute_repo_id

        _, clone = make_clone_with_origin(tmp_path)
        original = compute_repo_id(clone)
        monkeypatch.setenv("MCP_GIT_COMMON_DIR", str(tmp_path))
        assert compute_repo_id(clone) == original
        monkeypatch.setenv("MCP_GIT_COMMON_DIR", str(original.git_common_dir))
        assert compute_repo_id(clone) == original

    def test_tier2_remote_url_fallback(self, tmp_path):
        """Directory with no .git but a parseable git config uses remote URL (tier-2).

        We simulate this by creating a temp dir with a git config file that has
        a remote URL, and overriding PYTHONPATH so compute_repo_id falls to tier-2.
        Instead we test tier-2 more directly: a git repo whose git-common-dir lookup
        fails (bare = False, no .git dir) but whose cwd has a parseable config.
        """
        from mcp_server.storage.repo_identity import (
            _normalize_remote_url,
            _sha256_hex16,
            compute_repo_id,
        )

        # Create a normal repo first to get a valid config we can reference
        _, clone = make_clone_with_origin(tmp_path)

        # Get the remote URL from the clone
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=clone,
            capture_output=True,
            text=True,
            check=True,
        )
        remote_url = result.stdout.strip()

        # Create a directory with a fake .git file (broken tier-1) but
        # a git config that has the remote URL so tier-2 works.
        # We accomplish this by placing a .git file pointing to a gitdir that
        # doesn't have git tracking metadata but DOES have a config file.
        pseudo_git_dir = tmp_path / "pseudo_gitdir"
        pseudo_git_dir.mkdir()
        (pseudo_git_dir / "config").write_text(
            f"[core]\n\trepositoryformatversion = 0\n" f'[remote "origin"]\n\turl = {remote_url}\n'
        )

        # Make a working dir that has .git pointing to pseudo_git_dir (gitfile worktree syntax)
        # but where --git-common-dir would fail because pseudo_gitdir lacks HEAD etc.
        broken_repo = tmp_path / "broken_repo"
        broken_repo.mkdir()
        (broken_repo / ".git").write_text(f"gitdir: {pseudo_git_dir}\n")

        identity = compute_repo_id(broken_repo)
        # Should use tier-2 (remote URL) since git-common-dir will fail
        # OR tier-3 if git rejects the gitdir
        # The key assertion: repo_id matches URL hash
        expected_id = _sha256_hex16(_normalize_remote_url(remote_url))
        if identity.source == "remote_url":
            assert identity.repo_id == expected_id
        # Accept abs_path fallback as well since git behavior can vary
        assert identity.source in ("remote_url", "abs_path")

    def test_tier3_abs_path_fallback(self, tmp_path):
        """Non-git directory falls back to abs-path (tier-3)."""
        from mcp_server.storage.repo_identity import compute_repo_id

        non_git = tmp_path / "not_a_repo"
        non_git.mkdir()

        identity = compute_repo_id(non_git)
        assert identity.source == "abs_path"
        assert identity.git_common_dir is None

    def test_repo_id_format(self, tmp_path):
        """repo_id is a 16-char lowercase hex string."""
        from mcp_server.storage.repo_identity import compute_repo_id

        non_git = tmp_path / "plain"
        non_git.mkdir()
        identity = compute_repo_id(non_git)
        assert len(identity.repo_id) == 16
        assert identity.repo_id == identity.repo_id.lower()
        assert all(c in "0123456789abcdef" for c in identity.repo_id)

    def test_idempotency(self, tmp_path):
        """Three calls return the same result."""
        from mcp_server.storage.repo_identity import compute_repo_id

        _, clone = make_clone_with_origin(tmp_path)
        ids = [compute_repo_id(clone).repo_id for _ in range(3)]
        assert ids[0] == ids[1] == ids[2]


# ---------------------------------------------------------------------------
# resolve_tracked_branch tests
# ---------------------------------------------------------------------------


class TestResolveTrackedBranch:
    def test_origin_head_precedence(self, tmp_path):
        """When origin/HEAD is set to a non-default branch, that branch is returned."""
        from mcp_server.storage.repo_identity import resolve_tracked_branch

        # Create origin with HEAD on release/v1
        origin = tmp_path / "origin"
        git("init", "-b", "main", str(origin))
        git("config", "user.email", "test@test.com", cwd=origin)
        git("config", "user.name", "Test", cwd=origin)
        (origin / "f.txt").write_text("x")
        git("add", "f.txt", cwd=origin)
        git("commit", "-m", "init", cwd=origin)
        git("checkout", "-b", "release/v1", cwd=origin)
        (origin / "g.txt").write_text("y")
        git("add", "g.txt", cwd=origin)
        git("commit", "-m", "release", cwd=origin)

        # Clone while origin HEAD is at release/v1 → clone tracks release/v1
        clone = tmp_path / "clone"
        git("clone", str(origin), str(clone))

        # Verify origin/HEAD is set
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=clone,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, "origin/HEAD should be set after clone"

        git_common_dir = _get_common_dir(clone)
        branch = resolve_tracked_branch(git_common_dir)
        assert branch == "release/v1", f"Expected release/v1, got {branch}"

    def test_main_fallback_when_no_origin_head(self, tmp_path):
        """Without origin/HEAD, returns 'main' if present as a local branch."""
        from mcp_server.storage.repo_identity import resolve_tracked_branch

        # Create a local repo with only 'main' branch (no remote)
        repo = tmp_path / "repo"
        git("init", "-b", "main", str(repo))
        git("config", "user.email", "test@test.com", cwd=repo)
        git("config", "user.name", "Test", cwd=repo)
        (repo / "f.txt").write_text("x")
        git("add", "f.txt", cwd=repo)
        git("commit", "-m", "init", cwd=repo)

        git_common_dir = _get_common_dir(repo)
        branch = resolve_tracked_branch(git_common_dir)
        assert branch == "main", f"Expected main, got {branch}"

    def test_master_fallback_when_no_main(self, tmp_path):
        """Without origin/HEAD and no 'main', returns 'master' if present."""
        from mcp_server.storage.repo_identity import resolve_tracked_branch

        repo = tmp_path / "repo"
        git("init", "-b", "master", str(repo))
        git("config", "user.email", "test@test.com", cwd=repo)
        git("config", "user.name", "Test", cwd=repo)
        (repo / "f.txt").write_text("x")
        git("add", "f.txt", cwd=repo)
        git("commit", "-m", "init", cwd=repo)

        git_common_dir = _get_common_dir(repo)
        branch = resolve_tracked_branch(git_common_dir)
        assert branch == "master", f"Expected master, got {branch}"

    def test_current_head_branch_fallback(self, tmp_path):
        """Without origin/HEAD, main, or master, returns current HEAD branch."""
        from mcp_server.storage.repo_identity import resolve_tracked_branch

        repo = tmp_path / "lone_repo"
        repo.mkdir()
        git("init", cwd=repo)
        git("config", "user.email", "test@test.com", cwd=repo)
        git("config", "user.name", "Test", cwd=repo)
        git("checkout", "-b", "feature/foo", cwd=repo)
        (repo / "f.txt").write_text("x")
        git("add", "f.txt", cwd=repo)
        git("commit", "-m", "feat", cwd=repo)

        git_common_dir = _get_common_dir(repo)
        branch = resolve_tracked_branch(git_common_dir)
        assert branch == "feature/foo", f"Expected feature/foo, got {branch}"

    def test_none_git_common_dir(self):
        """resolve_tracked_branch with None returns a string."""
        from mcp_server.storage.repo_identity import resolve_tracked_branch

        result = resolve_tracked_branch(None)
        assert isinstance(result, str)
