import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from mcp_server.artifacts.manifest_v2 import WorkspaceArtifactManifest
from mcp_server.artifacts.multi_repo_artifact_coordinator import (
    MultiRepoArtifactCoordinator,
)
from mcp_server.cli.artifact_commands import artifact
from mcp_server.storage.multi_repo_manager import MultiRepositoryManager, RepositoryInfo
from mcp_server.storage.sqlite_store import SQLiteStore


def _repo_info(repo_id: str, path: Path) -> RepositoryInfo:
    (path / ".git").mkdir(exist_ok=True)
    commit = f"commit-{repo_id}"
    return RepositoryInfo(
        repository_id=repo_id,
        name=path.name,
        path=path,
        index_path=path / ".mcp-index" / "current.db",
        language_stats={},
        total_files=0,
        total_symbols=0,
        indexed_at=datetime.now(),
        current_commit=commit,
        last_indexed_commit=commit,
        current_branch="main",
        tracked_branch="main",
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


def test_workspace_manifest_roundtrip():
    manifest = WorkspaceArtifactManifest(
        workspace_id="ws",
        workspace_commit="abc",
        repositories=[{"repo_id": "r1", "name": "repo1"}],
    )
    payload = manifest.to_dict()
    restored = WorkspaceArtifactManifest.from_dict(payload)
    assert restored.workspace_id == "ws"
    assert restored.repositories[0]["repo_id"] == "r1"


def test_repository_registry_persists_artifact_state(tmp_path: Path):
    manager = MultiRepositoryManager(central_index_path=tmp_path / "registry.json")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_info = _repo_info("repo-1", repo_path)
    manager.registry.register(repo_info)

    manager.registry.update_artifact_state(
        "repo-1",
        last_published_commit="abc123",
        last_recovered_commit="def456",
        available_semantic_profiles=["commercial_high", "oss_high"],
        artifact_backend="github_actions",
        artifact_health="ready",
    )

    reloaded = MultiRepositoryManager(central_index_path=tmp_path / "registry.json")

    # The registry _load() re-keys legacy ids to the canonical sha256 derived
    # from the resolved path, so look the repo up by its new canonical id.
    all_repos = reloaded.registry.list_all()
    assert len(all_repos) == 1, "expected exactly one repository in the reloaded registry"
    stored = all_repos[0]
    assert stored is not None
    assert stored.last_published_commit == "abc123"
    assert stored.last_recovered_commit == "def456"
    assert stored.available_semantic_profiles == ["commercial_high", "oss_high"]
    assert stored.artifact_backend == "github_actions"
    assert stored.artifact_health == "ready"


def test_coordinator_fetch_updates_registry(monkeypatch, tmp_path: Path):
    manager = MultiRepositoryManager(central_index_path=tmp_path / "registry.json")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_info = _repo_info("repo-1", repo_path)
    manager.registry.register(repo_info)

    (repo_path / ".mcp-index").mkdir(exist_ok=True)
    (repo_path / ".mcp-index" / ".index_metadata.json").write_text(
        '{"semantic_profiles": {"commercial_high": {}, "oss_high": {}}}',
        encoding="utf-8",
    )

    artifact_metadata = {
        "commit": "recover123",
        "tracked_branch": "main",
        "branch": "main",
        "checksum": "checksum-123",
        "schema_version": "2",
        "semantic_profile_hash": "a" * 64,
        "compatibility": {
            "schema_version": "2",
            "semantic_profiles": {
                "commercial_high": {"compatibility_fingerprint": "commercial"},
                "oss_high": {"compatibility_fingerprint": "oss"},
            },
            "available_semantic_profiles": ["commercial_high", "oss_high"],
        },
    }

    def _fake_download_latest(self, output_dir, backup=True, full_only=False, **kwargs):
        _write_ready_index(repo_info)
        (repo_path / ".mcp-index" / "artifact-metadata.json").write_text(
            json.dumps(artifact_metadata),
            encoding="utf-8",
        )
        return type(
            "Result",
            (),
            {
                "artifact": {"head_sha": "recover123", "id": 17, "name": "repo-artifact"},
                "installed_items": [str(repo_info.index_path)],
                "validation_reasons": [],
            },
        )()

    monkeypatch.setattr(
        "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactDownloader.download_latest",
        _fake_download_latest,
    )
    monkeypatch.setattr(
        "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactDownloader._detect_repository",
        lambda self, repo_path=None: "owner/repo",
    )

    coordinator = MultiRepoArtifactCoordinator(manager)
    results = coordinator.fetch_workspace(["repo-1"])

    assert results[0].success is True
    stored = manager.get_repository_info("repo-1")
    assert stored is not None
    assert stored.last_recovered_commit == repo_info.last_indexed_commit
    assert stored.available_semantic_profiles == ["commercial_high", "oss_high"]
    assert stored.artifact_health == "ready"
    assert results[0].details["validation_status"] == "passed"
    assert results[0].details["validation"]["checksum"] == "checksum-123"
    assert results[0].details["validation"]["schema_version"] == "2"


def test_reconcile_workspace_marks_missing_or_ready(tmp_path: Path):
    manager = MultiRepositoryManager(central_index_path=tmp_path / "registry.json")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_info = _repo_info("repo-1", repo_path)
    manager.registry.register(repo_info)

    coordinator = MultiRepoArtifactCoordinator(manager)
    results = coordinator.reconcile_workspace(["repo-1"])
    assert results[0].details["artifact_health"] == "missing"
    assert results[0].details["validation_status"] == "missing"

    _write_ready_index(repo_info)
    (repo_path / ".mcp-index" / "artifact-metadata.json").write_text(
        json.dumps(
            {
                "commit": "recover123",
                "tracked_branch": "main",
                "branch": "main",
                "checksum": "checksum-123",
                "schema_version": "2",
                "semantic_profile_hash": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    results = coordinator.reconcile_workspace(["repo-1"])
    assert results[0].details["artifact_health"] == "ready"
    assert results[0].details["validation_status"] == "passed"


@pytest.mark.parametrize("upload_fails", [False, True])
def test_publish_workspace_records_actual_upload_outcome(monkeypatch, tmp_path: Path, upload_fails):
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "skip")
    manager = MultiRepositoryManager(central_index_path=tmp_path / "registry.json")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_info = _repo_info("repo-1", repo_path)
    manager.registry.register(repo_info)

    _write_ready_index(repo_info)
    (repo_path / ".mcp-index" / ".index_metadata.json").write_text(
        '{"semantic_profiles": {"commercial_high": {}, "oss_high": {}}}',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactUploader.compress_indexes",
        lambda self, output_path, secure=True, **kwargs: (Path(output_path), "checksum", 123),
    )
    monkeypatch.setattr(
        "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactUploader.create_metadata",
        lambda self, checksum, size, secure=True, **kwargs: {
            "repo_id": kwargs["repo_id"],
            "tracked_branch": kwargs["tracked_branch"],
            "branch": kwargs["tracked_branch"],
            "commit": kwargs["commit"],
            "checksum": checksum,
            "schema_version": "2",
            "semantic_profile_hash": "a" * 64,
        },
    )

    def upload(self, archive_path, metadata):
        if upload_fails:
            raise RuntimeError("synthetic-private-upload-error")

    monkeypatch.setattr(
        "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactUploader.upload_direct",
        upload,
    )
    monkeypatch.setattr(
        "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactUploader._detect_repository",
        lambda self, repo_path=None: "owner/repo",
    )

    results = MultiRepoArtifactCoordinator(manager).publish_workspace(["repo-1"])
    stored = manager.registry.get_repository("repo-1")
    if upload_fails:
        assert results[0].success is False
        assert stored.artifact_health == "publish_failed"
        assert stored.last_published_commit is None
        assert "synthetic-private-upload-error" not in results[0].error
        manager.close()
        return
    assert results[0].success is True
    assert results[0].details["artifact_backend"] == "github_release"
    assert results[0].details["artifact_health"] == "published"
    assert results[0].details["prepared_archive"].startswith("index-archive-")
    assert stored.last_published_commit == repo_info.last_indexed_commit
    assert stored.artifact_health == "published"
    assert results[0].details["validation"]["schema_version"] == "2"
    manager.close()


def test_enforced_workspace_publish_refuses_before_compression(monkeypatch, tmp_path):
    from unittest.mock import Mock

    monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
    manager = MultiRepositoryManager(central_index_path=tmp_path / "registry.json")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_info = _repo_info("repo-1", repo_path)
    manager.registry.register(repo_info)
    _write_ready_index(repo_info)
    compression = Mock()
    monkeypatch.setattr(
        "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactUploader.compress_indexes",
        compression,
    )
    try:
        result = MultiRepoArtifactCoordinator(manager).publish_workspace(["repo-1"])
        assert result[0].success is False
        assert result[0].details["code"] == "attestation_required"
        compression.assert_not_called()
        assert manager.registry.get("repo-1").last_published_commit is None
    finally:
        manager.close()


def test_workspace_publish_and_fetch_do_not_chdir(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "skip")
    manager = MultiRepositoryManager(central_index_path=tmp_path / "registry.json")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_info = _repo_info("repo-1", repo_path)
    manager.registry.register(repo_info)
    _write_ready_index(repo_info)

    monkeypatch.setattr(
        "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactUploader.compress_indexes",
        lambda self, output_path, secure=True, **kwargs: (Path(output_path), "checksum", 123),
    )
    monkeypatch.setattr(
        "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactUploader.create_metadata",
        lambda self, checksum, size, secure=True, **kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactUploader.upload_direct",
        lambda self, archive_path, metadata: None,
    )
    monkeypatch.setattr(
        "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactUploader._detect_repository",
        lambda self, repo_path=None: "owner/repo",
    )
    monkeypatch.setattr(
        "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactDownloader.download_latest",
        lambda self, output_dir, backup=True, full_only=False, **kwargs: type(
            "Result",
            (),
            {
                "artifact": {"head_sha": "recover123"},
                "validation_reasons": [],
                "installed_items": [str(repo_info.index_path)],
            },
        )(),
    )
    monkeypatch.setattr(
        "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactDownloader._detect_repository",
        lambda self, repo_path=None: "owner/repo",
    )

    coordinator = MultiRepoArtifactCoordinator(manager)
    original_chdir = os.chdir
    try:
        monkeypatch.setattr("os.chdir", lambda path: (_ for _ in ()).throw(AssertionError(path)))
        assert coordinator.publish_workspace(["repo-1"])[0].success is True
        assert coordinator.fetch_workspace(["repo-1"])[0].success is True
    finally:
        monkeypatch.setattr("os.chdir", original_chdir)


def test_workspace_status_cli_reports_registered_repositories(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    manager = MultiRepositoryManager(central_index_path=tmp_path / "registry.json")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_info = _repo_info("repo-1", repo_path)
    repo_info.available_semantic_profiles = ["commercial_high", "oss_high"]
    repo_info.artifact_health = "ready"
    repo_info.last_published_commit = "abc123"
    repo_info.last_recovered_commit = "def456"
    manager.registry.register(repo_info)

    _write_ready_index(repo_info)
    (repo_path / ".mcp-index" / "artifact-metadata.json").write_text(
        json.dumps(
            {
                "commit": "def456",
                "tracked_branch": "main",
                "branch": "main",
                "checksum": "checksum-123",
                "schema_version": "2",
                "semantic_profile_hash": "a" * 64,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands.MultiRepoArtifactCoordinator",
        lambda: MultiRepoArtifactCoordinator(manager),
    )

    result = runner.invoke(artifact, ["workspace-status"])

    assert result.exit_code == 0
    assert "Workspace manifest" in result.output
    assert "repo-1" in result.output
    assert "commercial_high" in result.output
    assert "last_published_commit: abc123" in result.output
    assert "last_recovered_commit: def456" in result.output
    assert "rollout_status: ready" in result.output
    assert "query_status: ready" in result.output
    assert "validation_status: passed" in result.output
