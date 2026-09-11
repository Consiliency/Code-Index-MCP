from datetime import datetime
from pathlib import Path

from click.testing import CliRunner

from mcp_server.artifacts.multi_repo_artifact_coordinator import MultiRepoArtifactCoordinator
from mcp_server.cli.artifact_commands import _run_incremental_reconcile, artifact
from mcp_server.indexing.change_detector import FileChange
from mcp_server.storage.multi_repo_manager import MultiRepositoryManager, RepositoryInfo
from mcp_server.storage.sqlite_store import SQLiteStore


def _repo_info(repo_id: str, path: Path) -> RepositoryInfo:
    (path / ".git").mkdir(exist_ok=True)
    return RepositoryInfo(
        repository_id=repo_id,
        name=path.name,
        path=path,
        index_path=path / ".mcp-index" / "current.db",
        language_stats={},
        total_files=0,
        total_symbols=0,
        indexed_at=datetime.now(),
        current_commit="current-commit",
        last_indexed_commit="current-commit",
        tracked_branch="main",
        current_branch="main",
        git_common_dir=str(path / ".git"),
        artifact_enabled=True,
        active=True,
    )


def _write_ready_index(repo_info: RepositoryInfo) -> None:
    repo_info.index_path.parent.mkdir(parents=True, exist_ok=True)
    source = repo_info.path / "README.md"
    source.write_text("ready index fixture\n", encoding="utf-8")
    store = SQLiteStore(str(repo_info.index_path))
    repository_id = store.ensure_repository_row(repo_info.path, name=repo_info.name)
    store.store_file(
        repository_id,
        path=source,
        relative_path="README.md",
        language="markdown",
    )
    store.close()


def test_artifact_pull_confirms_local_restore(monkeypatch, tmp_path):
    runner = CliRunner()

    def _fake_download_latest(self, output_dir, backup=True, full_only=False, **kwargs):
        Path(".mcp-index").mkdir(exist_ok=True)
        Path(".mcp-index/current.db").write_text("db", encoding="utf-8")
        return type("Result", (), {"validation_reasons": []})()

    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands.IndexArtifactDownloader.download_latest",
        _fake_download_latest,
    )
    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands.IndexArtifactDownloader._detect_repository",
        lambda self: "owner/repo",
    )
    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands._print_reconcile_guidance",
        lambda: print("guidance"),
    )

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(artifact, ["pull", "--latest"])

    assert result.exit_code == 0
    assert "Local index files restored" in result.output
    assert "current.db" in result.output
    assert "guidance" in result.output


def test_artifact_pull_fails_when_no_index_restored(monkeypatch, tmp_path):
    runner = CliRunner()

    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands.IndexArtifactDownloader.download_latest",
        lambda self, output_dir, backup=True, full_only=False, **kwargs: None,
    )
    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands.IndexArtifactDownloader._detect_repository",
        lambda self: "owner/repo",
    )
    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands._print_reconcile_guidance",
        lambda: print("guidance"),
    )

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(artifact, ["pull", "--latest"])

    assert result.exit_code != 0
    assert "no local index files were restored" in result.output.lower()


def test_artifact_recover_confirms_local_restore(monkeypatch, tmp_path):
    runner = CliRunner()

    def _fake_recover(self, branch, commit, output_dir, backup=True, **kwargs):
        Path(".mcp-index").mkdir(exist_ok=True)
        Path(".mcp-index/artifact-metadata.json").write_text("{}", encoding="utf-8")
        return type("Result", (), {"validation_reasons": []})()

    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands.IndexArtifactDownloader.recover",
        _fake_recover,
    )
    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands.IndexArtifactDownloader._detect_repository",
        lambda self: "owner/repo",
    )

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(artifact, ["recover", "--branch", "main"])

    assert result.exit_code == 0
    assert "Local index files restored" in result.output
    assert "artifact-metadata.json" in result.output
    assert "Git drift could not be determined" in result.output


def test_artifact_sync_bootstraps_local_indexes(monkeypatch, tmp_path):
    runner = CliRunner()

    def _fake_download_latest(self, output_dir, backup=True, full_only=False, **kwargs):
        Path(".mcp-index").mkdir(exist_ok=True)
        Path(".mcp-index/.index_metadata.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands.IndexArtifactDownloader.download_latest",
        _fake_download_latest,
    )
    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands.IndexArtifactDownloader._detect_repository",
        lambda self: "owner/repo",
    )
    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands._print_reconcile_guidance",
        lambda: print("guidance"),
    )
    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands._get_local_drift",
        lambda: (
            type("Detector", (), {"should_use_incremental": lambda self, changes: False})(),
            [],
        ),
    )

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(artifact, ["sync"])

    assert result.exit_code == 0
    assert "Indexes synchronized!" in result.output
    assert ".index_metadata.json" in result.output
    assert "guidance" in result.output


def test_artifact_sync_reports_existing_local_drift(monkeypatch, tmp_path):
    runner = CliRunner()
    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands._print_reconcile_guidance",
        lambda: print("guidance"),
    )
    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands._get_local_drift",
        lambda: (
            type("Detector", (), {"should_use_incremental": lambda self, changes: False})(),
            [object()],
        ),
    )

    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        Path(".mcp-index").mkdir(exist_ok=True)
        Path(".mcp-index/current.db").write_text("db", encoding="utf-8")
        result = runner.invoke(artifact, ["sync"])

    assert result.exit_code == 0
    assert "guidance" in result.output
    assert "too large for automatic incremental sync" in result.output.lower()


def test_incremental_reconcile_requires_committed_registered_generation(monkeypatch, tmp_path):
    import subprocess

    from tests.test_git_index_manager import _make_git_repo

    repo = _make_git_repo(tmp_path)
    owner = MultiRepositoryManager(central_index_path=tmp_path / "registry.json")
    repo_id = owner.registry.register_repository(str(repo))
    monkeypatch.chdir(repo)
    monkeypatch.setattr("mcp_server.cli.artifact_commands.MultiRepositoryManager", lambda: owner)
    change = FileChange("hello.py", "modified")
    assert _run_incremental_reconcile([change])
    first = owner.registry.get(repo_id).index_path
    (repo / "hello.py").write_text("print('committed replacement')\n")
    assert not _run_incremental_reconcile([change])
    assert owner.registry.get(repo_id).index_path == first
    subprocess.run(["git", "add", "hello.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic committed drift"], cwd=repo, check=True)
    assert _run_incremental_reconcile([change])
    current = owner.registry.get(repo_id).index_path
    assert current != first
    store = SQLiteStore(str(current))
    try:
        assert store.search_code_fts("replacement")
    finally:
        store.close()
        owner.close()


def test_workspace_fetch_cli_prints_validation_truth(monkeypatch, tmp_path):
    runner = CliRunner()
    manager = MultiRepositoryManager(central_index_path=tmp_path / "registry.json")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_info = _repo_info("repo-1", repo_path)
    manager.registry.register(repo_info)

    _write_ready_index(repo_info)
    (repo_path / ".mcp-index" / "artifact-metadata.json").write_text(
        '{"commit":"recover123","tracked_branch":"main","branch":"main","checksum":"checksum-123","schema_version":"2","semantic_profile_hash":"'
        + ("a" * 64)
        + '"}',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands.MultiRepoArtifactCoordinator",
        lambda: MultiRepoArtifactCoordinator(manager),
    )
    monkeypatch.setattr(
        "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactDownloader.download_latest",
        lambda self, output_dir, backup=True, full_only=False, **kwargs: type(
            "Result",
            (),
            {
                "artifact": {"head_sha": "recover123", "id": 23, "name": "repo-artifact"},
                "installed_items": [str(repo_info.index_path)],
                "validation_reasons": [],
            },
        )(),
    )
    monkeypatch.setattr(
        "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactDownloader._detect_repository",
        lambda self: "owner/repo",
    )

    result = runner.invoke(artifact, ["fetch-workspace", "--repository", "repo-1"])

    assert result.exit_code == 0
    assert "validation_status: passed" in result.output
    assert "last_recovered_commit: recover123" in result.output
    assert "schema_version" in result.output
