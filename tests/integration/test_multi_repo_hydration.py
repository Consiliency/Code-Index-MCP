"""MRE2E fresh-state multi-repo hydration acceptance."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
from pathlib import Path

from mcp_server.artifacts.artifact_download import ArtifactDownloadResult
from mcp_server.artifacts.multi_repo_artifact_coordinator import MultiRepoArtifactCoordinator
from mcp_server.storage.multi_repo_manager import MultiRepositoryManager
from tests.fixtures.multi_repo import boot_test_server, build_production_matrix


def _result_files(search_response) -> set[str]:
    rows = search_response if isinstance(search_response, list) else search_response["results"]
    return {str(row.get("file", "")) for row in rows}


def test_multi_repo_workspace_hydration_restores_clean_state_and_query_truth(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "skip")  # publication transport is mocked below
    matrix = build_production_matrix(tmp_path)
    registry_path = tmp_path / "registry.json"
    snapshots: dict[str, dict[str, object]] = {}

    with boot_test_server(tmp_path, matrix.repos) as server:
        manager = MultiRepositoryManager(central_index_path=registry_path)

        for repo in matrix.repos:
            (repo / ".mcp-index" / ".index_metadata.json").write_text(
                json.dumps(
                    {
                        "semantic_profiles": {
                            "commercial_high": {
                                "compatibility_fingerprint": "commercial-fingerprint"
                            },
                            "oss_high": {"compatibility_fingerprint": "oss-fingerprint"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            repo_id = matrix.alpha.repo_id if repo == matrix.alpha.path else matrix.beta.repo_id
            server.seed_repo_index(repo_id, repo)

        matrix.alpha.write_file(
            "delete_me.py", "def mre2e_deleted_symbol():\n    return 'delete'\n"
        )
        matrix.alpha.commit_all("add delete target")
        server.seed_repo_index(matrix.alpha.repo_id, matrix.alpha.path)

        matrix.alpha.write_file(
            "alpha_added.py",
            "def mre2e_added_symbol():\n    return 'added'\n",
        )
        matrix.alpha.commit_all("add file to rename later")
        matrix.alpha.write_file(
            "alpha.py",
            "class P33AlphaWidget:\n"
            "    marker = 'mre2e_modified_token'\n\n"
            "def mre2e_modified_symbol():\n"
            "    return 'modified'\n",
        )
        matrix.alpha.rename_file(
            "alpha_added.py",
            "alpha_renamed.py",
            "rename alpha addition",
        )
        matrix.alpha.delete_file("delete_me.py", "delete stale symbol")
        server.seed_repo_index(matrix.alpha.repo_id, matrix.alpha.path)

        def _fake_compress(self, output_path, secure=True, **kwargs):
            output_path = Path(output_path)
            repo_path = Path(kwargs["repo_path"])
            index_location = Path(kwargs["index_location"])
            snapshot_db = index_location / "snapshot-current.db"
            source_db = index_location / "current.db"
            source_conn = sqlite3.connect(str(source_db))
            snapshot_conn = sqlite3.connect(str(snapshot_db))
            try:
                source_conn.backup(snapshot_conn)
            finally:
                snapshot_conn.close()
                source_conn.close()
            with tarfile.open(output_path, "w:gz") as tar:
                tar.add(snapshot_db, arcname="current.db")
                tar.add(index_location / ".index_metadata.json", arcname=".index_metadata.json")
            checksum = "checksum-" + repo_path.name
            metadata_path = index_location / ".index_metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            snapshots[repo_path.name] = {
                "archive_bytes": output_path.read_bytes(),
                "checksum": checksum,
                "metadata": metadata,
            }
            snapshot_db.unlink(missing_ok=True)
            return output_path, checksum, output_path.stat().st_size

        def _fake_metadata(
            self,
            checksum,
            size,
            secure=True,
            **kwargs,
        ):
            repo_id = kwargs["repo_id"]
            commit = kwargs["commit"]
            tracked_branch = kwargs["tracked_branch"]
            return {
                "repo_id": repo_id,
                "tracked_branch": tracked_branch,
                "branch": tracked_branch,
                "commit": commit,
                "checksum": checksum,
                "schema_version": "2",
                "semantic_profile_hash": "a" * 64,
                "compatibility": {
                    "schema_version": "2",
                    "semantic_profiles": {
                        "commercial_high": {"compatibility_fingerprint": "commercial-fingerprint"},
                        "oss_high": {"compatibility_fingerprint": "oss-fingerprint"},
                    },
                    "available_semantic_profiles": ["commercial_high", "oss_high"],
                },
            }

        monkeypatch.setattr(
            "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactUploader.compress_indexes",
            _fake_compress,
        )
        monkeypatch.setattr(
            "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactUploader.create_metadata",
            _fake_metadata,
        )
        monkeypatch.setattr(
            "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactUploader.upload_direct",
            lambda self, archive_path, metadata: None,
        )
        monkeypatch.setattr(
            "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactUploader._detect_repository",
            lambda self, repo_path=None: "owner/repo",
        )

        manager = MultiRepositoryManager(central_index_path=registry_path)
        publish_results = MultiRepoArtifactCoordinator(manager).publish_workspace(
            [matrix.alpha.repo_id, matrix.beta.repo_id]
        )
        assert all(result.success for result in publish_results), [
            result.error for result in publish_results
        ]

    for repo_path in matrix.repos:
        shutil.rmtree(repo_path / ".mcp-index")
        (repo_path / ".mcp-index").mkdir()

    with boot_test_server(tmp_path, matrix.repos, seed_indexes=False) as server:
        manager = MultiRepositoryManager(central_index_path=registry_path)

        def _fake_download_latest(self, output_dir, backup=True, full_only=False, **kwargs):
            repo_path = Path(kwargs["repo_path"])
            assert (
                kwargs["expected_owner"].registration_id
                == manager.registry.get(kwargs["repo_id"]).registration_id
            )
            repo_snapshot = snapshots[repo_path.name]
            archive_path = output_dir / "index-archive.tar.gz"
            archive_path.write_bytes(repo_snapshot["archive_bytes"])
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(repo_path / ".mcp-index")

            metadata = dict(repo_snapshot["metadata"])
            metadata.update(
                {
                    "repo_id": kwargs["repo_id"],
                    "tracked_branch": kwargs["tracked_branch"],
                    "branch": kwargs["tracked_branch"],
                    "commit": kwargs["target_commit"],
                    "checksum": repo_snapshot["checksum"],
                    "schema_version": "2",
                    "semantic_profile_hash": "a" * 64,
                    "compatibility": {
                        "schema_version": "2",
                        "semantic_profiles": {
                            "commercial_high": {
                                "compatibility_fingerprint": "commercial-fingerprint"
                            },
                            "oss_high": {"compatibility_fingerprint": "oss-fingerprint"},
                        },
                        "available_semantic_profiles": ["commercial_high", "oss_high"],
                    },
                }
            )
            (repo_path / ".mcp-index" / "artifact-metadata.json").write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )
            manager.registry.update_indexed_commit(
                kwargs["repo_id"], kwargs["target_commit"], branch=kwargs["tracked_branch"]
            )
            return ArtifactDownloadResult(
                artifact={
                    "name": f"{repo_path.name}-artifact",
                    "id": len(repo_path.name),
                    "head_sha": kwargs["target_commit"],
                },
                installed_items=[str(manager.registry.get(kwargs["repo_id"]).index_path)],
                validation_reasons=[],
            )

        monkeypatch.setattr(
            "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactDownloader.download_latest",
            _fake_download_latest,
        )
        monkeypatch.setattr(
            "mcp_server.artifacts.multi_repo_artifact_coordinator.IndexArtifactDownloader._detect_repository",
            lambda self, repo_path=None: "owner/repo",
        )

        coordinator = MultiRepoArtifactCoordinator(manager)
        fetch_results = coordinator.fetch_workspace([matrix.alpha.repo_id, matrix.beta.repo_id])
        reconcile_results = coordinator.reconcile_workspace(
            [matrix.alpha.repo_id, matrix.beta.repo_id]
        )

        assert all(result.success for result in fetch_results)
        assert all(result.success for result in reconcile_results)

        alpha_status = next(
            result for result in reconcile_results if result.repository_id == matrix.alpha.repo_id
        )
        assert alpha_status.details["artifact_health"] == "ready"
        assert alpha_status.details["validation_status"] == "passed"

    with boot_test_server(tmp_path, matrix.repos, seed_indexes=False) as hydrated_server:
        alpha_search = hydrated_server.call_tool(
            "search_code",
            {
                "query": "mre2e_modified_token",
                "repository": str(matrix.alpha.path),
                "semantic": False,
            },
        )
        renamed_search = hydrated_server.call_tool(
            "search_code",
            {
                "query": "mre2e_added_symbol",
                "repository": str(matrix.alpha.path),
                "semantic": False,
            },
        )
        deleted_lookup = hydrated_server.call_tool(
            "symbol_lookup",
            {"symbol": "mre2e_deleted_symbol", "repository": str(matrix.alpha.path)},
        )
        beta_search = hydrated_server.call_tool(
            "search_code",
            {
                "query": matrix.beta.token,
                "repository": str(matrix.beta.path),
                "semantic": False,
            },
        )

        assert all(matrix.alpha.path.name in file for file in _result_files(alpha_search))
        renamed_files = _result_files(renamed_search)
        assert any(file.endswith("alpha_renamed.py") for file in renamed_files)
        assert all("alpha_added.py" not in file for file in renamed_files)
        assert deleted_lookup["result"] == "not_found"
        assert deleted_lookup["readiness"]["state"] == "ready"
        assert all(matrix.beta.path.name in file for file in _result_files(beta_search))

        for repo in (matrix.alpha, matrix.beta):
            conn = sqlite3.connect(str(repo.path / ".mcp-index" / "current.db"))
            try:
                count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            finally:
                conn.close()
            assert count >= 1
