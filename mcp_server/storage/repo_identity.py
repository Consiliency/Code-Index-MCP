"""Single source of truth for repository identity and tracked-branch resolution.

NOTE: Tier-1 (git_common_dir) and Tier-2 (remote URL) ids may disagree across
environments where the clone lacks a .git directory (e.g. some shallow CI setups).
That divergence is accepted as out-of-scope for P1 and noted as a future hardening item.
"""

import hashlib
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepoIdentity:
    repo_id: str  # 16-hex sha256 prefix, lowercase
    git_common_dir: Optional[Path]  # resolved .git common dir; None if not-a-repo
    source: Literal["git_common_dir", "remote_url", "abs_path"]


def _sha256_hex16(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _run_git(args: list, cwd: Path) -> Optional[str]:
    """Run a git subcommand, return stripped stdout or None on error."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None
    except FileNotFoundError:
        logger.error("git binary not found in PATH")
        return None


def _normalize_remote_url(url: str) -> str:
    """Normalize a git remote URL for stable hashing.

    Lowercases scheme+host, strips trailing .git and trailing slashes.
    """
    url = url.strip()
    # Lowercase the scheme and host portion for http(s)/git/ssh URLs
    for scheme in ("https://", "http://", "git://", "ssh://"):
        if url.lower().startswith(scheme):
            rest = url[len(scheme) :]
            slash = rest.find("/")
            if slash != -1:
                url = scheme.lower() + rest[:slash].lower() + rest[slash:]
            else:
                url = scheme.lower() + rest.lower()
            break

    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url


def compute_repo_id(path: Path) -> RepoIdentity:
    """Compute a stable, worktree-aware identity for the repo rooted at *path*.

    Tier resolution (first success wins):
    1. git rev-parse --git-common-dir  → resolved POSIX path → sha256[:16]
    2. git config --get remote.origin.url  → normalized URL → sha256[:16]
    3. Path(path).resolve().as_posix()  → sha256[:16]
    """
    resolved_path = Path(path).resolve()

    # Tier 1: git common dir (same for all worktrees of one repo)
    raw = _run_git(["rev-parse", "--git-common-dir"], cwd=resolved_path)
    if raw is not None:
        common_dir = Path(raw)
        if not common_dir.is_absolute():
            common_dir = resolved_path / common_dir
        try:
            common_dir = common_dir.resolve(strict=False)
        except Exception:
            pass
        mounted_common = os.environ.get("MCP_GIT_COMMON_DIR")
        if mounted_common:
            mounted_path = Path(mounted_common)
            try:
                if mounted_path.is_absolute() and mounted_path.samefile(common_dir):
                    common_dir = mounted_path.resolve()
            except OSError:
                pass
        posix = common_dir.as_posix()
        return RepoIdentity(
            repo_id=_sha256_hex16(posix),
            git_common_dir=common_dir,
            source="git_common_dir",
        )

    # Tier 2: remote origin URL
    url = _run_git(["config", "--get", "remote.origin.url"], cwd=resolved_path)
    if url is not None:
        normalized = _normalize_remote_url(url)
        return RepoIdentity(
            repo_id=_sha256_hex16(normalized),
            git_common_dir=None,
            source="remote_url",
        )

    # Tier 3: absolute path
    posix = resolved_path.as_posix()
    return RepoIdentity(
        repo_id=_sha256_hex16(posix),
        git_common_dir=None,
        source="abs_path",
    )


def resolve_tracked_branch(git_common_dir: Optional[Path]) -> str:
    """Return the branch that should be treated as the canonical tracked branch.

    Resolution order:
    1. git symbolic-ref refs/remotes/origin/HEAD  → strip refs/remotes/origin/ prefix
    2. First of: main, master  (checked against local branch list)
    3. Current HEAD branch (git rev-parse --abbrev-ref HEAD)

    Returns an empty string only as a last resort (detached HEAD with no branches).
    """
    if git_common_dir is None:
        return ""

    cwd = git_common_dir

    # Step 1: origin/HEAD
    ref = _run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=cwd)
    if ref is not None:
        prefix = "refs/remotes/origin/"
        if ref.startswith(prefix):
            return ref[len(prefix) :]
        return ref

    # Step 2: prefer main / master from local branches
    branches_raw = _run_git(["branch", "--format=%(refname:short)"], cwd=cwd)
    if branches_raw:
        branches = [b.strip() for b in branches_raw.splitlines() if b.strip()]
        for preferred in ("main", "master"):
            if preferred in branches:
                return preferred

    # Step 3: current HEAD branch
    head = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if head and head != "HEAD":
        return head

    return ""
