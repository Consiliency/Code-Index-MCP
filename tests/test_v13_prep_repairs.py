"""Construction-site regressions from the independent PREP review."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema
import pytest

from mcp_server.artifacts.freshness import FreshnessVerdict, verify_artifact_freshness
from mcp_server.cli.task_reindex import _record_reindexed_files
from mcp_server.core.ignore_patterns import IgnorePatternManager
from mcp_server.core.path_resolver import PathResolver
from mcp_server.storage.sqlite_store import SQLiteStore
from tests.test_v13_data_storage import runtime


def test_task_bookkeeping_does_not_import_ignored_files(tmp_path):
    (tmp_path / ".gitignore").write_text("private.env\n")
    target = tmp_path / "private.env"
    target.write_text("SYNTHETIC=value\n")
    store = SQLiteStore(str(tmp_path / "current.db"), path_resolver=PathResolver(tmp_path))
    try:
        assert _record_reindexed_files(store, tmp_path, target) == 0
        assert store.get_statistics()["files"] == 0
    finally:
        store.close()


def test_mcp_negation_cannot_clear_git_exclusion(tmp_path):
    (tmp_path / ".gitignore").write_text("private.env\n")
    (tmp_path / ".mcp-index-ignore").write_text("!private.env\n*.tmp\n!allowed.tmp\n")
    manager = IgnorePatternManager(tmp_path)
    assert manager.should_ignore(Path("private.env"))
    assert manager.should_ignore(Path("other.tmp"))
    assert not manager.should_ignore(Path("allowed.tmp"))


@pytest.mark.parametrize("tool", ["search_code", "symbol_lookup", "reindex", "summarize_sample"])
def test_generation_transition_matches_declared_schema(tool):
    from mcp_server.cli.stdio_runner import _build_tool_list
    from mcp_server.cli.tool_handlers import _resolution_transition_response

    schema = next(item.outputSchema for item in _build_tool_list() if item.name == tool)
    payload = json.loads(_resolution_transition_response(tool)[0].text)
    jsonschema.validate(payload, schema)
    assert payload["mutation_performed"] is False


@pytest.mark.parametrize("timestamp", ["2099-01-01T00:00:00Z", "2026-01-01", 123])
def test_freshness_rejects_future_naive_or_nontext_timestamp(monkeypatch, timestamp):
    ancestry = MagicMock()
    monkeypatch.setattr("mcp_server.artifacts.freshness.subprocess.run", ancestry)
    assert (
        verify_artifact_freshness({"commit": "a" * 40, "timestamp": timestamp}, "b" * 40, 1)
        is FreshnessVerdict.INVALID
    )
    ancestry.assert_not_called()


def test_freshness_preserves_timestamp_offset(monkeypatch):
    monkeypatch.setattr("mcp_server.artifacts.freshness.subprocess.run", MagicMock())
    stamp = (datetime.now(timezone.utc) - timedelta(hours=25)).astimezone(
        timezone(timedelta(hours=14))
    )
    assert (
        verify_artifact_freshness({"commit": "a" * 40, "timestamp": stamp.isoformat()}, "b" * 40, 1)
        is FreshnessVerdict.STALE_AGE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_owner", ["watcher", "summarizer"])
async def test_shutdown_error_still_drains_later_owners(monkeypatch, failed_owner):
    from unittest.mock import AsyncMock

    from mcp_server.cli import stdio_runner as runner

    for name, value in {
        "_shutdown_called": False,
        "_shutdown_task": None,
        "_file_watcher": None,
        "_indexing_thread": None,
        "_fts_rebuild_thread": None,
    }.items():
        monkeypatch.setattr(runner, name, value)
    watcher, dispatcher, stores, exporter = (MagicMock() for _ in range(4))
    summarizer = MagicMock()
    summarizer.stop = AsyncMock()
    failure = RuntimeError("private-sentinel-must-not-leak")
    if failed_owner == "watcher":
        watcher.stop.side_effect = failure
    else:
        summarizer.stop.side_effect = failure
    monkeypatch.setattr(runner, "_lazy_summarizer", summarizer)
    with pytest.raises(RuntimeError) as raised:
        await runner._graceful_shutdown(watcher, None, stores, exporter, dispatcher)
    assert "private-sentinel" not in str(raised.value)
    dispatcher.shutdown.assert_called_once()
    stores.shutdown.assert_called_once()
    exporter.stop.assert_called_once()


def test_failed_client_close_keeps_owner_but_refuses_service_reuse(monkeypatch):
    from mcp_server.client import IndexItClient

    stores, resolver, dispatcher = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(
        "mcp_server.client.initialize_stateless_services",
        lambda **kwargs: (stores, resolver, dispatcher, None, None),
    )
    client = IndexItClient()
    assert client.dispatcher is dispatcher
    dispatcher.shutdown.side_effect = RuntimeError("retirement failed")
    with pytest.raises(RuntimeError):
        client.close()
    assert client._dispatcher is dispatcher
    with pytest.raises(RuntimeError, match="close"):
        _ = client.dispatcher
    dispatcher.shutdown.side_effect = None
    client.close()
    assert client._dispatcher is None


@pytest.fixture
def artifact_payload(tmp_path, monkeypatch):
    from mcp_server.artifacts.artifact_download import IndexArtifactDownloader
    from mcp_server.artifacts.artifact_upload import _metadata_bytes

    monkeypatch.setenv("INDEX_SCHEMA_VERSION", "2")
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
    payload = tmp_path / "payload"
    payload.mkdir()
    database = tmp_path / "current.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE synthetic(value TEXT)")
    archive = payload / "index.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(database, arcname="current.db")
    metadata = {
        "repo_id": "fixture",
        "tracked_branch": "main",
        "commit": "a" * 40,
        "schema_version": "2",
        "semantic_profile_hash": "lexical-only",
        "checksum": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "artifact_type": "full",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "compatibility": {"schema_version": "2", "embedding_model": None},
    }
    metadata_path = payload / "artifact-metadata.json"
    metadata_path.write_bytes(_metadata_bytes(metadata))
    bundle = payload / "artifact-metadata.json.attestation.jsonl"
    bundle.write_text("synthetic signature boundary")
    verified_subjects = []
    signed_digest = hashlib.sha256(metadata_path.read_bytes()).hexdigest()

    def verify_cli(args, **kwargs):
        assert args[:3] == ["gh", "attestation", "verify"]
        subject = Path(args[3])
        verified_subjects.append(subject)
        valid = hashlib.sha256(subject.read_bytes()).hexdigest() == signed_digest
        return subprocess.CompletedProcess(args, 0 if valid else 1, "", "")

    monkeypatch.setattr("mcp_server.artifacts.attestation.subprocess.run", verify_cli)
    return IndexArtifactDownloader(repo="synthetic/example"), payload, metadata, verified_subjects


def test_restore_authenticates_metadata_and_uses_fresh_output(artifact_payload, tmp_path):
    downloader, payload, metadata, verified = artifact_payload
    output = tmp_path / "reused-output"
    output.mkdir()
    victim = tmp_path / "outside.json"
    victim.write_text("untouched")
    (output / "artifact-metadata.json").symlink_to(victim)
    (output / "stale-vector.jsonl").write_text("stale")
    restored = downloader._restore_downloaded_payload(
        payload, output, repo_id="fixture", tracked_branch="main", target_commit=metadata["commit"]
    )
    assert verified == [payload / "artifact-metadata.json"]
    assert restored.parent == output and restored != output
    assert victim.read_text() == "untouched"
    assert not (restored / "stale-vector.jsonl").exists()
    assert (restored / "current.db").is_file()
    assert json.loads((restored / "artifact-metadata.json").read_text()) == metadata


@pytest.mark.parametrize(
    "field",
    [
        "repo_id",
        "commit",
        "tracked_branch",
        "schema_version",
        "semantic_profile_hash",
        "compatibility",
    ],
)
def test_restore_rejects_identity_tampering_before_admission(artifact_payload, tmp_path, field):
    from mcp_server.artifacts.attestation import AttestationError

    downloader, payload, metadata, verified = artifact_payload
    metadata[field] = {
        "repo_id": "different-fixture",
        "commit": "b" * 40,
        "tracked_branch": "other",
        "schema_version": "3",
        "semantic_profile_hash": "b" * 64,
        "compatibility": {"schema_version": "2", "embedding_model": "different-model"},
    }[field]
    (payload / "artifact-metadata.json").write_text(json.dumps(metadata))
    output = tmp_path / "out"
    with pytest.raises(AttestationError):
        downloader._restore_downloaded_payload(payload, output, allow_unsafe=True)
    assert verified == [payload / "artifact-metadata.json"]
    assert not output.exists()


def test_checksum_sidecar_cannot_replace_signed_archive_binding(artifact_payload, tmp_path):
    downloader, payload, metadata, verified = artifact_payload
    archive = payload / "index.tar.gz"
    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(b"different archive content")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(replacement, arcname="current.db")
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert checksum != metadata["checksum"]
    (payload / "index.tar.gz.sha256").write_text(f"{checksum} index.tar.gz\n")
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="checksum"):
        downloader._restore_downloaded_payload(payload, output)
    assert verified == [payload / "artifact-metadata.json"]
    assert not output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["directory", "file"])
@pytest.mark.parametrize("timing", ["during_work", "after_publish", "no_cancel"])
async def test_task_cancellation_reports_actual_staged_publication(
    tmp_path, monkeypatch, mode, timing
):
    from types import SimpleNamespace

    from mcp.types import TaskMetadata

    from mcp_server.cli.task_reindex import run_reindex_task
    from mcp_server.storage.mcp_task_registry import MCPTaskRegistry
    from tests.fixtures.multi_repo import boot_test_server, build_temp_repo
    from tests.test_mcptasks_reindex import _FakeTask

    path, repo_id = build_temp_repo(tmp_path, "task-cancel")
    tasks = MCPTaskRegistry()
    task = _FakeTask((await tasks.create_task(TaskMetadata())).taskId)
    with boot_test_server(tmp_path, [path]) as server:
        resolver = server.repo_resolver
        ctx = resolver.resolve_ready(path)
        before = server.registry.get(repo_id)
        dispatcher = MagicMock()

        def index(current, *args, **kwargs):
            assert current.staging
            with current.sqlite_store._get_connection() as connection:
                connection.execute("UPDATE files SET language = 'synthetic'")
            if timing == "during_work":
                task.request_cancellation()
            return SimpleNamespace(error=None) if mode == "file" else {"indexed_files": 1}

        dispatcher.index_directory.side_effect = index
        dispatcher.index_file.side_effect = index
        if timing == "after_publish":
            mutate = resolver.mutate

            def publish_then_cancel(*args):
                result = mutate(*args)
                task.request_cancellation()
                return result

            monkeypatch.setattr(resolver, "mutate", publish_then_cancel)
        target = next(path.glob("*.py")) if mode == "file" else path
        result = await run_reindex_task(
            task=task,
            registry=tasks,
            dispatcher=dispatcher,
            ctx=ctx,
            active_store=ctx.sqlite_store,
            target_path=target,
            requested_path=str(target) if mode == "file" else None,
            repo_resolver=resolver,
        )
        published = timing != "during_work"
        assert bool(result.structuredContent.get("cancelled")) is (timing != "no_cancel")
        assert result.structuredContent["mutation_performed"] is published
        after = server.registry.get(repo_id)
        assert (after.index_generation != before.index_generation) is published
        assert resolver.classify(path).ready is published
        if timing == "no_cancel":
            assert result.structuredContent["durable_files"] >= 1
        else:
            assert (await tasks.get_record(task.task_id)).task.status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["symbol", "code"])
@pytest.mark.parametrize("api", ["coordinator", "compatibility"])
async def test_cross_repo_search_honors_all_scopes_on_real_index(runtime, surface, api):
    from mcp_server.dispatcher.cross_repo_coordinator import (
        CrossRepositoryCoordinator,
        CrossRepositorySearchCoordinator,
        SearchContext,
        SearchScope,
    )
    from mcp_server.storage.multi_repo_manager import MultiRepositoryManager

    repo, registry, repo_id, _store, manager = runtime
    sources = {
        "alpha.py": "def scope_alpha():\n    return 'scope_token'\n",
        "beta.py": "def scope_beta():\n    return 'scope_token'\n",
        "gamma.js": "function scope_gamma() { return 'scope_token'; }\n",
    }
    for name, content in sources.items():
        (repo / name).write_text(content)
    subprocess.run(["git", "add", *sources], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "Synthetic mixed language scope"], cwd=repo, check=True)
    manager.dispatcher._use_factory = True
    assert manager.rebuild_repository_index(repo_id).action == "full_index"
    owner = MultiRepositoryManager(central_index_path=registry.registry_path)
    try:
        if api == "coordinator":
            coordinator = CrossRepositoryCoordinator(
                owner, enable_semantic=False, enable_reranking=False
            )
            context = SearchContext(
                "scope_" if surface == "symbol" else "scope_token",
                surface,
                repositories=[repo_id],
                languages=["python", "javascript", "python"],
                file_patterns=["*.py", "*.js", "*"],
                rerank=False,
            )
            results = await coordinator.search(context)
            rows = [result.content for result in results]
            assert all(result.occurrences == 1 for result in results)
        else:
            coordinator = CrossRepositorySearchCoordinator(owner)
            scope = SearchScope(
                repositories=[repo_id],
                languages=["python", "javascript", "python"],
                file_types=[".py", ".js", ".py"],
            )
            result = await getattr(coordinator, "search_" + surface)(
                "scope_" if surface == "symbol" else "scope_token", scope=scope
            )
            rows = result.results
        assert {row["language"] for row in rows} == {"python", "javascript"}
        assert len(rows) == 3

        # A small result limit must not let other languages displace this match.
        if surface == "code":
            result = owner._search_code_in_repository(
                repo_id, "scope_token", None, 1, languages=["javascript"]
            )
            assert len(result.results) == 1 and result.results[0]["language"] == "javascript"
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_dependency_search_uses_manager_and_preserves_original_scope(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import mcp_server.dispatcher.cross_repo_coordinator as module

    manager = MagicMock()
    analyzer = MagicMock()
    analyzer.analyze = AsyncMock(return_value={"dependency"})
    factory = MagicMock(return_value=analyzer)
    monkeypatch.setattr(module, "DependencyGraphAnalyzer", factory)
    coordinator = module.CrossRepositoryCoordinator(manager, enable_reranking=False)
    context = module.SearchContext(
        "symbol", "symbol", repositories=["primary"], include_dependencies=True
    )
    result = SimpleNamespace(primary_repository="dependency", metadata={})
    coordinator.search = AsyncMock(return_value=[result])
    assert await coordinator.search_with_dependencies(context) == [result]
    factory.assert_called_once_with(manager)
    assert context.repositories == ["primary"]
    searched = coordinator.search.call_args.args[0]
    assert set(searched.repositories) == {"primary", "dependency"}
    assert result.metadata["from_dependency"] is True


@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        ("src/fileXpy", "*.py", False),
        ("src/file.py.bak", "*.py", False),
        ("src/b.py", "src/[ab].py", True),
        ("src/[draft].py", "src/[[]draft].py", True),
    ],
)
def test_cross_repo_patterns_follow_glob_semantics(path, pattern, expected):
    from mcp_server.dispatcher.cross_repo_coordinator import CrossRepositoryCoordinator

    assert CrossRepositoryCoordinator._match_pattern(None, path, pattern) is expected


def test_restore_constructor_failure_retires_created_dispatcher(tmp_path, monkeypatch):
    from mcp_server.artifacts.artifact_download import IndexArtifactDownloader

    dispatcher = MagicMock()
    monkeypatch.setattr(
        "mcp_server.dispatcher.dispatcher_enhanced.EnhancedDispatcher", lambda **kwargs: dispatcher
    )
    monkeypatch.setattr(
        "mcp_server.storage.git_index_manager.GitAwareIndexManager",
        MagicMock(side_effect=RuntimeError("constructor failure")),
    )
    downloader = IndexArtifactDownloader(repo="synthetic/example", registry=MagicMock())
    with pytest.raises(RuntimeError, match="constructor failure"):
        downloader._install_verified_generation("fixture", tmp_path, "a" * 40, None)
    dispatcher.shutdown.assert_called_once()


def test_strict_automatic_publish_refuses_before_compression_or_network(monkeypatch):
    from mcp_server.artifacts.publisher import ArtifactError, ArtifactPublisher

    monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
    uploader = MagicMock()
    uploader.repo = "synthetic/example"
    network = MagicMock()
    monkeypatch.setattr("mcp_server.artifacts.publisher.subprocess.run", network)
    with pytest.raises(ArtifactError, match="prepare-only"):
        ArtifactPublisher(uploader).publish_on_reindex("fixture", "a" * 40)
    uploader.compress_indexes.assert_not_called()
    uploader.upload_direct.assert_not_called()
    network.assert_not_called()


def test_publisher_tags_do_not_collide_on_short_commit_prefix(monkeypatch):
    from mcp_server.artifacts.publisher import ArtifactPublisher

    monkeypatch.setenv("MCP_ATTESTATION_MODE", "skip")
    uploader = MagicMock()
    uploader.repo = "synthetic/example"
    uploader.compress_indexes.return_value = (Path("synthetic.tar.gz"), "a" * 64, 1)
    publisher = ArtifactPublisher(uploader)
    for name in (
        "_get_latest_commit",
        "_ensure_sha_release",
        "_move_latest_pointer",
        "_check_is_latest",
    ):
        monkeypatch.setattr(publisher, name, MagicMock(return_value=None))
    first = publisher.publish_on_reindex("fixture", "abcdef0" + "1" * 33)
    second = publisher.publish_on_reindex("fixture", "abcdef0" + "2" * 33)
    assert first.tag != second.tag


def test_prepared_upload_restore_preserves_signed_metadata_bytes(
    artifact_payload, tmp_path, monkeypatch
):
    import shutil

    from mcp_server.artifacts.artifact_upload import IndexArtifactUploader
    from mcp_server.artifacts.attestation import Attestation

    downloader, payload, metadata, verified = artifact_payload
    signed_metadata = (payload / "artifact-metadata.json").read_bytes()
    attestation = Attestation(
        "https://github.com/synthetic/example/attestations",
        payload / "artifact-metadata.json.attestation.jsonl",
        hashlib.sha256(signed_metadata).hexdigest(),
        datetime.now(timezone.utc),
    )
    verify_signature = subprocess.run
    uploaded = tmp_path / "downloaded-release"
    uploaded.mkdir()

    def gh(args, **kwargs):
        if args[:3] == ["gh", "attestation", "verify"]:
            return verify_signature(args, **kwargs)
        assert args[:2] == ["gh", "release"]
        if args[2] == "upload":
            for name in args[args.index("--clobber") + 1 :]:
                shutil.copyfile(name, uploaded / Path(name).name)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", gh)
    monkeypatch.setattr(subprocess, "Popen", MagicMock(side_effect=AssertionError("Offline test")))
    uploader = IndexArtifactUploader(repo="synthetic/example")
    monkeypatch.setattr(uploader, "_ensure_gh_cli", lambda: None)
    monkeypatch.setattr(uploader, "_run_gh", lambda args, **kwargs: gh(args).stdout)

    def verify_assets(tag, names, *, deadline):
        assert names == {path.name for path in uploaded.iterdir()}

    monkeypatch.setattr(uploader, "_verify_release_assets", verify_assets)
    uploader.upload_direct(payload / "index.tar.gz", metadata, attestation=attestation)
    assert (uploaded / "artifact-metadata.json").read_bytes() == signed_metadata
    restored = downloader._restore_downloaded_payload(
        uploaded,
        tmp_path / "restored",
        repo_id="fixture",
        tracked_branch="main",
        target_commit=metadata["commit"],
    )
    assert (restored / "artifact-metadata.json").read_bytes() == signed_metadata
    assert len(verified) == 2


def test_archive_only_attestation_is_not_a_metadata_signature(
    artifact_payload, tmp_path, monkeypatch
):
    from mcp_server.artifacts.attestation import AttestationError

    downloader, payload, metadata, _verified = artifact_payload
    archive_digest = metadata["checksum"]

    def verify_archive_only(args, **kwargs):
        subject = Path(args[3])
        valid = hashlib.sha256(subject.read_bytes()).hexdigest() == archive_digest
        return subprocess.CompletedProcess(args, 0 if valid else 1, "", "")

    monkeypatch.setattr(subprocess, "run", verify_archive_only)
    with pytest.raises(AttestationError):
        downloader._restore_downloaded_payload(payload, tmp_path / "not-restored")
    assert not (tmp_path / "not-restored").exists()
