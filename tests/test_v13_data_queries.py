"""Live Git and generation admission controls for public query surfaces."""

import subprocess

import pytest

from mcp_server.health.repository_readiness import ReadinessClassifier
from mcp_server.storage.repository_registry import RepositoryRegistry
from mcp_server.storage.sqlite_store import SQLiteStore


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("change", ["dirty", "staged", "branch", "commit"])
def test_live_git_state_refuses_ready_for_both_object_formats(
    tmp_path, monkeypatch, object_format, change
):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "-b", "main", f"--object-format={object_format}")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    file = repo / "hello.py"
    file.write_text("hello = 1\n")
    git("add", ".")
    git("commit", "-m", "fixture")
    commit = git("rev-parse", "HEAD")
    monkeypatch.setenv("MCP_INDEX_STORAGE_PATH", str(tmp_path / "indexes"))
    registry = RepositoryRegistry(tmp_path / "registry.json")
    repo_id = registry.register_repository(str(repo))
    info = registry.get(repo_id)
    info.index_path.parent.mkdir(parents=True)
    store = SQLiteStore(str(info.index_path))
    try:
        row = store.ensure_repository_row(repo)
        store.store_file(row, file, "hello.py")
        registry.update_indexed_commit(repo_id, commit, branch="main")
        info = registry.get(repo_id)
        assert ReadinessClassifier.classify_registered(info).ready
        if change == "branch":
            git("switch", "-c", "feature")
        else:
            file.write_text("hello = 2\n")
            if change in {"staged", "commit"}:
                git("add", ".")
            if change == "commit":
                git("commit", "-m", "changed")
        readiness = ReadinessClassifier.classify_registered(info)
        assert not readiness.ready
        assert readiness.code == ("wrong_branch" if change == "branch" else "stale_commit")
    finally:
        store.close()
