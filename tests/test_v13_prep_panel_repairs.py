"""Counterexamples from the independent four-seat PREP review."""

import hashlib
import io
import json
import stat
import subprocess
import sys
import tarfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from mcp_server import gateway
from mcp_server.artifacts.artifact_download import IndexArtifactDownloader
from mcp_server.artifacts.artifact_upload import IndexArtifactUploader
from mcp_server.artifacts.attestation import AttestationError, attest
from mcp_server.cli.tool_handlers import handle_reindex
from mcp_server.core.repo_resolver import RepoResolver
from mcp_server.storage.git_index_manager import IndexSyncResult
from mcp_server.storage.store_registry import StoreRegistry
from mcp_server.watcher_multi_repo import MultiRepositoryHandler
from tests.test_v13_data_storage import runtime
from tests.test_v13_prep_repairs import artifact_payload


@pytest.mark.parametrize("boundary", ["admission", "publication"])
def test_losing_generation_writer_cannot_poison_new_registration(runtime, monkeypatch, boundary):
    repo, registry, repo_id, _store, manager = runtime
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    replacement = []

    def replace_registration():
        assert registry.unregister_repository(repo_id)
        assert registry.register_repository(str(repo)) == repo_id
        replacement.append(registry.get(repo_id))

    if boundary == "admission":
        original = registry.begin_generation_mutation

        def begin(*args, **kwargs):
            replace_registration()
            return original(*args, **kwargs)

        monkeypatch.setattr(registry, "begin_generation_mutation", begin)
    else:

        def checkpoint(stage):
            if stage == "before_provenance":
                replace_registration()

        monkeypatch.setattr(manager, "_rebuild_checkpoint", checkpoint)
    result = manager.rebuild_repository_index(repo_id)
    assert result.action == "failed"
    assert replacement
    after = registry.get(repo_id)
    assert after.registration_id == replacement[0].registration_id
    assert after.staleness_reason == replacement[0].staleness_reason
    assert after.last_sync_error == replacement[0].last_sync_error


@pytest.mark.parametrize("operation", ["add", "modify", "delete", "rename_in", "rename_out"])
def test_incremental_sync_respects_committed_exclusions(runtime, operation):
    repo, registry, repo_id, _store, manager = runtime
    excluded = repo / "generated_pb2.py"
    included = repo / "ordinary.py"
    for index in range(12):
        (repo / f"stable{index}.py").write_text(f"stable_value_{index} = {index}\n")
    if operation in {"modify", "delete", "rename_in"}:
        excluded.write_text("excluded_sentinel = 1\n")
    if operation == "rename_out":
        included.write_text("excluded_sentinel = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic incremental baseline"], cwd=repo, check=True)
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    if operation in {"add", "modify"}:
        excluded.write_text("excluded_sentinel = 2\n")
    elif operation == "delete":
        excluded.unlink()
    elif operation == "rename_in":
        excluded.rename(included)
    else:
        included.rename(excluded)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic excluded change"], cwd=repo, check=True)
    result = manager.sync_repository_index(repo_id)
    assert result.action == "incremental_update", result.error
    assert registry.get(repo_id).staleness_reason is None
    store = manager._resolve_ctx(repo_id).sqlite_store
    assert bool(store.search_code_fts("excluded_sentinel")) == (operation == "rename_in")
    with store._get_connection() as connection:
        assert connection.execute("SELECT count(*) FROM fts_code").fetchone()[0] > 0
        assert not connection.execute(
            "SELECT fts_code.file_id FROM fts_code LEFT JOIN files "
            "ON files.id = fts_code.file_id WHERE files.id IS NULL"
        ).fetchall()


@pytest.mark.asyncio
async def test_health_failure_does_not_return_exception_text(monkeypatch):
    marker = "synthetic-private-exception-marker"
    monkeypatch.setattr(
        gateway,
        "health_checker",
        SimpleNamespace(get_overall_health=AsyncMock(side_effect=RuntimeError(marker))),
    )
    assert marker not in json.dumps(await gateway.detailed_health_check())


def test_multi_repository_handler_does_not_start_unused_thread(tmp_path):
    before = set(threading.enumerate())
    parent = SimpleNamespace(dispatcher=SimpleNamespace(), query_cache=None, path_resolver=None)
    handler = MultiRepositoryHandler("synthetic", tmp_path, parent, ctx=None)
    try:
        assert set(threading.enumerate()) == before
    finally:
        inner = getattr(handler, "_inner_handler", None)
        if inner is not None:
            inner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovering", [False, True])
async def test_full_generation_task_request_never_runs_synchronously(
    runtime, monkeypatch, recovering
):
    repo, registry, repo_id, _store, manager = runtime
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    if recovering:
        registry.update_staleness_reason(repo_id, "partial_index_failure")
    stores = StoreRegistry.for_registry(registry)
    resolver = RepoResolver(registry, stores)
    rebuild = MagicMock(
        return_value=IndexSyncResult(
            action="full_index", commit=registry.get(repo_id).last_indexed_commit, files_processed=1
        )
    )
    monkeypatch.setattr(manager, "rebuild_repository_index", rebuild)
    experimental = SimpleNamespace(is_task=True, run_task=AsyncMock())
    try:
        result = await handle_reindex(
            arguments={"repository": repo_id},
            dispatcher=manager.dispatcher,
            repo_resolver=resolver,
            git_index_manager=manager,
            request_experimental=experimental,
            task_registry=MagicMock(),
        )
        rebuild.assert_not_called()
        assert json.loads(result[0].text)["code"] == "task_scope_unsupported"
    finally:
        stores.shutdown()


def test_post_publication_trace_failure_preserves_committed_success(runtime, monkeypatch):
    _repo, registry, repo_id, _store, manager = runtime
    original = manager._write_force_full_exit_trace
    entered = []

    def fail_final_trace(info, update):
        if update.get("stage") == "force_full_completed":
            entered.append(True)
            raise OSError("synthetic trace write failure")
        return original(info, update)

    monkeypatch.setattr(manager, "_write_force_full_exit_trace", fail_final_trace)
    result = manager.sync_repository_index(repo_id, force_full=True)
    assert entered
    assert result.action == "full_index", result.error
    assert registry.get(repo_id).staleness_reason is None


@pytest.mark.parametrize("name", ["unexpected.txt", "nested/index.tar.gz", "../index.tar.gz"])
def test_untrusted_zip_rejects_unexpected_paths_before_extraction(tmp_path, name):
    with zipfile.ZipFile(tmp_path / "artifact.zip", "w") as archive:
        archive.writestr("artifact-metadata.json", "{}")
        archive.writestr(name, "synthetic")
    before = set(tmp_path.iterdir())
    with pytest.raises(ValueError):
        IndexArtifactDownloader(repo="synthetic/example")._extract_actions_artifact_zip(tmp_path)
    assert set(tmp_path.iterdir()) == before


def test_stable_container_tags_are_not_published_in_build_job():
    repo = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((repo / ".github/workflows/release-automation.yml").read_text())
    build = workflow["jobs"]["build-release"]
    step = next(step for step in build["steps"] if step.get("id") == "build-and-push")
    assert "latest" not in step["with"]["tags"]
    assert "inputs.version" not in step["with"]["tags"]
    assert "github.run_id" in step["with"]["tags"]


@pytest.mark.parametrize("damage", ["duplicate", "special", "expanded", "compressed", "extra"])
def test_actions_zip_preflight_rejects_bad_envelopes_without_writes(tmp_path, monkeypatch, damage):
    import mcp_server.artifacts.artifact_download as download

    with zipfile.ZipFile(tmp_path / "artifact.zip", "w") as archive:
        archive.writestr("artifact-metadata.json", "{}")
        member = zipfile.ZipInfo("index.tar.gz")
        if damage == "special":
            member.create_system = 3
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(member, b"synthetic")
        if damage == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("artifact-metadata.json", "other")
        if damage == "extra":
            archive.writestr("unexpected.txt", "other")
    if damage == "expanded":
        monkeypatch.setattr(download, "MAX_ACTIONS_PAYLOAD_BYTES", 4)
    if damage == "compressed":
        monkeypatch.setattr(download, "MAX_ACTIONS_ZIP_BYTES", 4)
    with pytest.raises(ValueError):
        IndexArtifactDownloader(repo="synthetic/example")._extract_actions_artifact_zip(tmp_path)
    assert {path.name for path in tmp_path.iterdir()} == {"artifact.zip"}


def test_actions_zip_accepts_exact_flat_payload(tmp_path):
    contents = {
        "index.tar.gz": b"synthetic archive",
        "artifact-metadata.json": b"{}",
        "artifact-metadata.json.attestation.jsonl": b"synthetic signature",
        "index.tar.gz.sha256": b"synthetic checksum",
    }
    with zipfile.ZipFile(tmp_path / "artifact.zip", "w") as archive:
        for name, payload in contents.items():
            archive.writestr(name, payload)
    IndexArtifactDownloader(repo="synthetic/example")._extract_actions_artifact_zip(tmp_path)
    assert all((tmp_path / name).read_bytes() == value for name, value in contents.items())


@pytest.mark.parametrize("failure", ["size", "timeout", "exit"])
def test_actions_download_bounds_and_reaps_child(tmp_path, monkeypatch, failure):
    import mcp_server.artifacts.artifact_download as download

    processes = []
    popen = subprocess.Popen
    script = "import os,time; os.write(1,b'x'*64); time.sleep(30)"
    if failure == "exit":
        script = "raise SystemExit(7)"

    def child(command, **kwargs):
        assert command[:2] == ["gh", "api"]
        process = popen([sys.executable, "-c", script], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(download.subprocess, "Popen", child)
    monkeypatch.setattr(download, "MAX_ACTIONS_ZIP_BYTES", 4)
    if failure == "timeout":
        monkeypatch.setattr(download.select, "select", lambda *args: ([], [], []))
    expected = {
        "size": ValueError,
        "timeout": subprocess.TimeoutExpired,
        "exit": subprocess.CalledProcessError,
    }[failure]
    with pytest.raises(expected):
        IndexArtifactDownloader(repo="synthetic/example").download_artifact(1, tmp_path / "out")
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed


@pytest.mark.parametrize("damage", ["size", "duplicate", "nested", "count", "negative"])
def test_release_asset_preflight_refuses_before_download(tmp_path, monkeypatch, damage):
    import mcp_server.artifacts.artifact_download as download

    assets = [{"name": "index.tar.gz", "size": 12}, {"name": "artifact-metadata.json", "size": 2}]
    if damage == "size":
        assets[0]["size"] = 2**40
    elif damage == "duplicate":
        assets.append(dict(assets[0]))
    elif damage == "nested":
        assets[0]["name"] = "../index.tar.gz"
    elif damage == "negative":
        assets[0]["size"] = -1
    else:
        assets.extend({"name": f"extra{index}", "size": 0} for index in range(3))
    requests = []

    def response(command, target, limit, deadline):
        requests.append(command)
        assert command[:3] == ["gh", "release", "view"]
        assert limit == 1024**2
        target.write(json.dumps({"assets": assets}).encode())

    monkeypatch.setattr(download, "_download_bounded", response)
    with pytest.raises(ValueError):
        IndexArtifactDownloader(repo="synthetic/example").download_release_artifact(
            "index-fixture", tmp_path / "out"
        )
    assert len(requests) == 1
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("failure", ["metadata_size", "asset_size", "timeout"])
def test_release_stream_bounds_reap_processes(tmp_path, monkeypatch, failure):
    import mcp_server.artifacts.artifact_download as download

    popen = subprocess.Popen
    processes = []
    assets = [{"name": "index.tar.gz", "size": 1}, {"name": "artifact-metadata.json", "size": 1}]

    def child(command, **kwargs):
        if failure == "metadata_size":
            script = "import os,time; os.write(1,b'x'*(1024**2+1)); time.sleep(30)"
        elif command[:3] == ["gh", "release", "view"]:
            script = "import sys; sys.stdout.write(" + repr(json.dumps({"assets": assets})) + ")"
        else:
            script = "import os,time; os.write(1,b'x'*64); time.sleep(30)"
        process = popen([sys.executable, "-c", script], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(download.subprocess, "Popen", child)
    monkeypatch.setattr(download, "MAX_ACTIONS_PAYLOAD_BYTES", 32)
    if failure == "timeout":
        monkeypatch.setattr(download.select, "select", lambda *args: ([], [], []))
    with pytest.raises(subprocess.TimeoutExpired if failure == "timeout" else ValueError):
        IndexArtifactDownloader(repo="synthetic/example").download_release_artifact(
            "index-fixture", tmp_path / "out"
        )
    assert processes and all(process.poll() is not None for process in processes)
    assert all(process.stdout.closed for process in processes)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("failure", ["expansion", "members", "write"])
def test_authenticated_tar_bounds_and_cleans_fresh_output(
    artifact_payload, tmp_path, monkeypatch, failure
):
    import mcp_server.artifacts.artifact_download as download

    downloader, payload, _metadata, verified = artifact_payload
    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / "existing"
    sentinel.write_text("untouched")
    if failure == "expansion":
        monkeypatch.setattr(download, "MAX_EXTRACTED_INDEX_BYTES", 4)
    elif failure == "members":
        monkeypatch.setattr(download, "MAX_INDEX_MEMBERS", 0)
    else:
        extract = tarfile.TarFile.extract

        def fail_after_write(self, *args, **kwargs):
            extract(self, *args, **kwargs)
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(tarfile.TarFile, "extract", fail_after_write)
    with pytest.raises(OSError if failure == "write" else ValueError):
        downloader._restore_downloaded_payload(payload, output)
    assert verified
    assert list(output.iterdir()) == [sentinel]
    assert sentinel.read_text() == "untouched"


@pytest.mark.parametrize("name", ["current.db", "./current.db", "a/../current.db"])
def test_tar_duplicate_or_parent_paths_refuse_and_cleanup(
    artifact_payload, tmp_path, monkeypatch, name
):
    from mcp_server.artifacts.artifact_upload import _metadata_bytes

    downloader, payload, metadata, _verified = artifact_payload
    archive = payload / "index.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in ("current.db", name):
            member = tarfile.TarInfo(path)
            member.size = 1
            tar.addfile(member, io.BytesIO(b"x"))
    metadata["checksum"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    subject = payload / "artifact-metadata.json"
    subject.write_bytes(_metadata_bytes(metadata))
    signed_digest = hashlib.sha256(subject.read_bytes()).hexdigest()

    def verify(command, **kwargs):
        assert command[:3] == ["gh", "attestation", "verify"]
        assert hashlib.sha256(Path(command[3]).read_bytes()).hexdigest() == signed_digest
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", verify)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="Unsafe archive member"):
        downloader._restore_downloaded_payload(payload, output)
    assert not list(output.iterdir())


@pytest.mark.parametrize("metadata_name", ["artifact-metadata.json", "custom-prepared.json"])
def test_uploaded_release_is_discovered_authenticated_and_restored(
    artifact_payload, tmp_path, monkeypatch, metadata_name
):
    downloader, payload, metadata, verified = artifact_payload
    prepared = payload / metadata_name
    if metadata_name != "artifact-metadata.json":
        prepared.write_bytes((payload / "artifact-metadata.json").read_bytes())
        prepared.with_suffix(".json.attestation.jsonl").write_bytes(
            (payload / "artifact-metadata.json.attestation.jsonl").read_bytes()
        )
    verify = subprocess.run
    uploaded = {}
    release = {}

    def gh(args, **kwargs):
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            assert args[3:] == [metadata["commit"], metadata["commit"]]
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["gh", "attestation", "verify"]:
            return verify(args, **kwargs)
        if args == ["gh", "--version"]:
            return subprocess.CompletedProcess(args, 0, "gh synthetic", "")
        if args[:2] == ["gh", "release"]:
            if args[2] == "create":
                release.update(
                    tag_name=args[3], created_at=metadata["timestamp"], target_commitish="main"
                )
            elif args[2] == "upload":
                uploaded.update(
                    {
                        Path(name).name: Path(name).read_bytes()
                        for name in args[args.index("--clobber") + 1 :]
                    }
                )
                release["assets"] = [
                    {"name": name, "size": len(data)} for name, data in uploaded.items()
                ]
            elif args[2] == "view":
                return subprocess.CompletedProcess(
                    args, 0, json.dumps({"assets": release["assets"]}), ""
                )
            else:
                pytest.fail("Unexpected release command")
        elif args[:2] == ["gh", "api"]:
            assert "--paginate" in args
            response = json.dumps(release) if args[2].endswith("/releases") else ""
            return subprocess.CompletedProcess(args, 0, response, "")
        else:
            pytest.fail("Unexpected external command")
        return subprocess.CompletedProcess(args, 0, "", "")

    def download(args, target, limit, deadline):
        assert args[3] == release["tag_name"]
        if args[:3] == ["gh", "release", "view"]:
            data = json.dumps({"assets": release["assets"]}).encode()
        else:
            assert args[:3] == ["gh", "release", "download"]
            assert args[-2:] == ["--output", "-"]
            data = uploaded[args[args.index("--pattern") + 1]]
        assert len(data) <= limit
        target.write(data)

    monkeypatch.setattr("mcp_server.artifacts.artifact_download._download_bounded", download)
    monkeypatch.setattr(subprocess, "run", gh)
    IndexArtifactUploader(repo="synthetic/example").upload_prepared(
        payload / "index.tar.gz",
        prepared,
        repo_id="fixture",
        tracked_branch="main",
        commit=metadata["commit"],
    )
    result = downloader.download_latest(
        output_dir=tmp_path / "download",
        index_location=tmp_path / "installed",
        tracked_branch="main",
        target_commit=metadata["commit"],
    )
    assert result.artifact["artifact_backend"] == "github_release"
    assert (tmp_path / "installed/current.db").is_file()
    assert (tmp_path / "installed/artifact-metadata.json").read_bytes() == (
        payload / "artifact-metadata.json"
    ).read_bytes()
    assert len(verified) == 3


def test_nondefault_signer_ref_requires_exact_digest_before_cli(tmp_path, monkeypatch):
    metadata = tmp_path / "artifact-metadata.json"
    metadata.write_text("{}")
    metadata.with_suffix(".json.attestation.jsonl").write_text("synthetic bundle")
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
    monkeypatch.setenv("MCP_ATTESTATION_SOURCE_REF", "refs/heads/synthetic")
    monkeypatch.delenv("MCP_ATTESTATION_SIGNER_DIGEST", raising=False)
    external = MagicMock()
    monkeypatch.setattr(subprocess, "run", external)
    with pytest.raises(AttestationError, match="exact signer digest"):
        attest(metadata, repo="synthetic/example")
    external.assert_not_called()


def test_installed_cli_prepares_once_and_uploads_only_matching_signed_identity(
    runtime, tmp_path, monkeypatch
):
    from click.testing import CliRunner

    from mcp_server.artifacts.artifact_upload import _metadata_bytes
    from mcp_server.artifacts.attestation import Attestation
    from mcp_server.cli.artifact_commands import artifact

    repo, registry, repo_id, _store, manager = runtime
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands._resolve_repository",
        lambda selector: registry.get(repo_id),
    )
    monkeypatch.setattr(
        "mcp_server.cli.artifact_commands.MultiRepositoryManager",
        lambda: SimpleNamespace(registry=registry, close=lambda: None),
    )
    monkeypatch.setattr(
        IndexArtifactUploader,
        "_detect_repository",
        lambda self, repo_path=None: "synthetic/example",
    )
    upload = MagicMock()
    signature = MagicMock(return_value=Attestation("", None, "", datetime.now(timezone.utc)))
    monkeypatch.setattr(IndexArtifactUploader, "upload_direct", upload)
    monkeypatch.setattr("mcp_server.artifacts.artifact_upload.attest", signature)
    metadata_path = tmp_path / "prepared-metadata.json"
    runner = CliRunner()
    result = runner.invoke(
        artifact,
        [
            "push",
            "--repository",
            repo_id,
            "--prepare-only",
            "--metadata-output",
            str(metadata_path),
        ],
    )
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output.splitlines()[-1])
    assert receipt["uploaded"] is False
    assert receipt["sha256"] == hashlib.sha256(metadata_path.read_bytes()).hexdigest()
    archive = Path(receipt["archive"])
    assert receipt["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    upload.assert_not_called()
    signature.assert_not_called()
    before = metadata_path.read_bytes()
    repeated = runner.invoke(
        artifact,
        [
            "push",
            "--repository",
            repo_id,
            "--prepare-only",
            "--metadata-output",
            str(metadata_path),
        ],
    )
    assert repeated.exit_code != 0
    assert metadata_path.read_bytes() == before
    command = [
        "push",
        "--repository",
        repo_id,
        "--prepared-archive",
        str(archive),
        "--prepared-metadata",
        str(metadata_path),
    ]
    published = runner.invoke(artifact, command)
    assert published.exit_code == 0, published.output
    upload.assert_called_once()
    signature.assert_called_once_with(metadata_path, repo="synthetic/example")
    assert registry.get(repo_id).artifact_health == "published"
    metadata = json.loads(before)
    metadata["repo_id"] = "other-repository"
    metadata_path.write_bytes(_metadata_bytes(metadata))
    refused = runner.invoke(artifact, command)
    assert refused.exit_code != 0
    assert "identity mismatch" in refused.output
    upload.assert_called_once()
    signature.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outer_failure", [False, True])
async def test_component_health_failures_are_redacted(monkeypatch, outer_failure):
    from mcp_server.metrics.health_check import ComponentHealthChecker

    checker = ComponentHealthChecker()
    checker._checks.clear()
    marker = "synthetic-private-component-failure"
    checker.register_health_check("synthetic", AsyncMock(side_effect=RuntimeError(marker)))
    if outer_failure:
        monkeypatch.setattr(checker, "check_component", AsyncMock(side_effect=RuntimeError(marker)))
    try:
        results = await checker.check_all_components()
        assert len(results) == 1
        assert results[0].message == "Health check failed"
        assert marker not in repr(results)
    finally:
        checker.shutdown()


def test_full_only_skips_release_and_actions_delta_names(tmp_path, monkeypatch):
    downloader = IndexArtifactDownloader(repo="synthetic/example")
    full = {"id": 3, "name": "index-synthetic-main-abc-full"}
    monkeypatch.setattr(
        downloader,
        "list_artifacts",
        lambda: [
            {"id": 1, "name": "index-synthetic-main-abc-delta"},
            {"id": 2, "name": "mcp-index-delta-abc"},
            full,
        ],
    )
    selected = MagicMock()
    monkeypatch.setattr(downloader, "download_selected_artifact", selected)
    downloader.download_latest(output_dir=tmp_path, full_only=True)
    selected.assert_called_once_with(full, output_dir=tmp_path, backup=True)


def test_artifact_schema_tracks_current_sqlite_migrations(runtime, monkeypatch):
    from importlib import resources

    from mcp_server.storage.sqlite_store import SQLiteStore

    _repo, registry, repo_id, _store, manager = runtime
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    actual = IndexArtifactUploader(repo="synthetic/example")._get_schema_version(
        registry.get(repo_id).index_path
    )
    migrations = resources.files("mcp_server.storage").joinpath("migrations")
    packaged = {
        str(int(path.name.split("_")[0]))
        for path in migrations.iterdir()
        if path.name.endswith(".sql")
    }
    assert packaged == {str(version) for version in range(1, SQLiteStore.SCHEMA_VERSION + 1)}
    assert actual == str(SQLiteStore.SCHEMA_VERSION)
    monkeypatch.setenv("INDEX_SCHEMA_VERSION", actual)
    downloader = IndexArtifactDownloader(repo="synthetic/example")
    metadata = {"compatibility": {"schema_version": actual, "embedding_model": None}}
    assert downloader.check_compatibility(metadata) == (True, [])
    monkeypatch.setenv("INDEX_SCHEMA_VERSION", "2")
    compatible, issues = downloader.check_compatibility(metadata)
    assert not compatible and any("newer" in issue for issue in issues)
    metadata["compatibility"]["schema_version"] = "999"
    monkeypatch.setenv("INDEX_SCHEMA_VERSION", "999")
    from mcp_server.core.errors import UnknownSchemaVersionError

    with pytest.raises(UnknownSchemaVersionError):
        downloader.check_compatibility(metadata)


def test_repository_retirement_joins_observer_before_closing_resources(tmp_path):
    from mcp_server.watcher_multi_repo import MultiRepositoryWatcher

    calls = []
    observer = SimpleNamespace(
        stop=lambda: calls.append("stop"),
        join=lambda: calls.append("joined"),
    )
    owner = SimpleNamespace(
        _watch_lock=threading.RLock(),
        observers={"synthetic": observer},
        watchers={},
        store_registry=SimpleNamespace(close=lambda repo_id: calls.append("store")),
        plugin_set_registry=SimpleNamespace(evict=lambda repo_id: calls.append("plugins")),
        semantic_indexer_registry=SimpleNamespace(evict=lambda repo_id: calls.append("vectors")),
        dispatcher=SimpleNamespace(
            evict_repository_state=lambda *args, **kwargs: calls.append("dispatcher")
        ),
    )
    MultiRepositoryWatcher._stop_repo_watcher(
        owner, "synthetic", SimpleNamespace(path=str(tmp_path))
    )
    assert calls == ["stop", "joined", "store", "plugins", "vectors", "dispatcher"]
