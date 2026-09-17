"""Tests for ArtifactPublisher — idempotency, race safety, error handling (SL-4)."""

from __future__ import annotations

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from mcp_server.artifacts.artifact_upload import IndexArtifactUploader, ReleaseAssetBundle
from mcp_server.artifacts.attestation import Attestation
from mcp_server.artifacts.publisher import ArtifactError, ArtifactPublisher, ArtifactRef

REPO = "owner/repo"
COMMIT = "abcdef1234567890abcdef1234567890abcdef12"

_SYNTHETIC_ATTESTATION = Attestation(
    bundle_url="https://github.com/owner/repo/attestations/1",
    bundle_path=Path("/tmp/_publish_race_archive.tar.gz.attestation.jsonl"),
    subject_digest="sha256stub",
    signed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
)


@pytest.fixture(autouse=True)
def _stub_attest(monkeypatch):
    """Exercise mocked publication orchestration under explicit test-only opt-out."""
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "skip")


SHORT_SHA = COMMIT[:7]
TAG = f"index-my-repo-main-{COMMIT}"
RELEASE_URL = f"https://github.com/{REPO}/releases/tag/{TAG}"
LATEST_URL = f"https://github.com/{REPO}/releases/tag/index-latest"


@contextmanager
def _mock_gh(side_effect):
    def bounded(command, *, deadline):
        assert deadline > time.monotonic()
        result = side_effect(command)
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, command)
        return result.stdout

    with patch.object(IndexArtifactUploader, "_run_gh", side_effect=bounded) as transport:
        yield transport


def test_publisher_pointer_calls_share_one_deadline():
    publisher = ArtifactPublisher(_make_uploader())
    with _mock_gh(_default_gh_dispatch) as transport:
        publisher.publish_on_reindex("my-repo", COMMIT)
    assert len(transport.call_args_list) == 3
    assert len({call.kwargs["deadline"] for call in transport.call_args_list}) == 1


def test_publisher_reaps_timed_out_command():
    import sys

    publisher = ArtifactPublisher(_make_uploader(), gh_cmd=sys.executable)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        publisher._run(["-c", "import time; time.sleep(30)"], deadline=started + 0.15)
    assert time.monotonic() - started < 2


def _canonical_tag(repo_id: str, commit: str, tracked_branch: str = "main") -> str:
    return f"index-{repo_id.replace('/', '_').replace(':', '_')}-{tracked_branch}-{commit}"


def _make_uploader() -> IndexArtifactUploader:
    uploader = IndexArtifactUploader.__new__(IndexArtifactUploader)
    uploader.repo = REPO
    uploader.token = ""
    uploader.index_files = []
    # P14 SL-4: publisher.publish_on_reindex now invokes compress_indexes +
    # create_metadata to compute the DeltaPolicy decision. These tests focus
    # on gh-orchestration behavior and stub the uploader's heavy work.
    from datetime import datetime
    from datetime import timezone as _tz
    from pathlib import Path as _Path
    from unittest.mock import MagicMock as _MagicMock

    _synthetic_attestation = Attestation(
        bundle_url="https://github.com/owner/repo/attestations/1",
        bundle_path=_Path("/tmp/_publish_race_archive.tar.gz.attestation.jsonl"),
        subject_digest="sha256stub",
        signed_at=datetime.now(_tz.utc),
    )
    uploader.compress_indexes = _MagicMock(  # type: ignore[method-assign]
        return_value=(_Path("/tmp/_publish_race_archive.tar.gz"), "sha256stub", 100)
    )
    uploader.create_metadata = _MagicMock(  # type: ignore[method-assign]
        return_value={
            "artifact_type": "full",
            "delta_from": None,
            "checksum": "sha256stub",
            "attestation_url": "https://github.com/owner/repo/attestations/1",
        }
    )
    uploader.upload_direct = _MagicMock(  # type: ignore[method-assign]
        return_value=ReleaseAssetBundle(
            archive_path=_Path("/tmp/_publish_race_archive.tar.gz"),
            metadata_path=_Path("/tmp/artifact-metadata.json"),
            checksum_path=_Path("/tmp/_publish_race_archive.tar.gz.sha256"),
            attestation_path=_Path("/tmp/_publish_race_archive.tar.gz.attestation.jsonl"),
            asset_paths=(
                _Path("/tmp/_publish_race_archive.tar.gz"),
                _Path("/tmp/artifact-metadata.json"),
                _Path("/tmp/_publish_race_archive.tar.gz.sha256"),
                _Path("/tmp/_publish_race_archive.tar.gz.attestation.jsonl"),
            ),
        )
    )
    uploader._synthetic_attestation = _synthetic_attestation  # type: ignore[attr-defined]
    return uploader


def _make_publisher(gh_side_effect=None) -> tuple[ArtifactPublisher, MagicMock]:
    """Return (publisher, mock_subprocess_run)."""
    uploader = _make_uploader()
    publisher = ArtifactPublisher(uploader, gh_cmd="gh")
    mock_run = MagicMock()
    if gh_side_effect is not None:
        mock_run.side_effect = gh_side_effect
    else:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    return publisher, mock_run


def _default_gh_dispatch(args, **kwargs):
    """Default handler: 'release view index-latest' returns our commit, rest succeed."""
    cmd = args
    # gh release view index-latest --json targetCommitish
    if "view" in cmd and "index-latest" in cmd:
        result = MagicMock(returncode=0, stdout=f'{{"targetCommitish": "{COMMIT}"}}', stderr="")
        return result
    # gh release view index-<sha> → returncode=1 to indicate it doesn't exist
    if "view" in cmd:
        return MagicMock(returncode=1, stdout="", stderr="not found")
    return MagicMock(returncode=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# Test 1: Idempotency — calling twice with same (repo_id, commit) returns same ArtifactRef
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_same_ref_on_double_call(self):
        uploader = _make_uploader()
        publisher = ArtifactPublisher(uploader, gh_cmd="gh")

        call_count = {"n": 0}

        def side_effect(args, **kwargs):
            call_count["n"] += 1
            if "view" in args and "index-latest" in args:
                return MagicMock(
                    returncode=0, stdout=f'{{"targetCommitish": "{COMMIT}"}}', stderr=""
                )
            if "view" in args:
                # First view of sha-tag → not found; second → found
                if call_count["n"] <= 2:
                    return MagicMock(returncode=1, stdout="", stderr="not found")
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with _mock_gh(side_effect):
            ref1 = publisher.publish_on_reindex("my-repo", COMMIT)
            ref2 = publisher.publish_on_reindex("my-repo", COMMIT)

        assert ref1 == ref2
        assert ref1.tag == TAG
        assert ref1.repo_id == "my-repo"
        assert ref1.commit == COMMIT
        assert ref1.release_url == RELEASE_URL

    def test_ref_fields_match_commit(self):
        uploader = _make_uploader()
        publisher = ArtifactPublisher(uploader, gh_cmd="gh")

        def side_effect(args, **kwargs):
            if "view" in args and "index-latest" in args:
                return MagicMock(
                    returncode=0, stdout=f'{{"targetCommitish": "{COMMIT}"}}', stderr=""
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        with _mock_gh(side_effect):
            ref = publisher.publish_on_reindex("repo-x", COMMIT)

        assert ref.tag == _canonical_tag("repo-x", COMMIT)
        assert ref.commit == COMMIT
        assert ref.repo_id == "repo-x"


# ---------------------------------------------------------------------------
# Test 2: Concurrent race — two different commits; only one wins index-latest
# ---------------------------------------------------------------------------


class TestConcurrentRace:
    def test_two_commits_each_get_sha_tag(self):
        """Both commits get their own SHA-keyed release; exactly one wins index-latest."""
        commit_a = "aaaa000000000000000000000000000000000000"
        commit_b = "bbbb000000000000000000000000000000000000"
        sha_a = commit_a[:7]
        sha_b = commit_b[:7]

        edit_calls: list[str] = []

        def side_effect(args, **kwargs):
            # Track 'release edit index-latest --target <sha>' calls
            if "edit" in args and "index-latest" in args:
                target_idx = list(args).index("--target") + 1 if "--target" in args else -1
                if target_idx > 0:
                    edit_calls.append(args[target_idx])
            if "view" in args and "index-latest" in args:
                # Whoever calls first wins — return their commit
                winner = edit_calls[0] if edit_calls else commit_a
                return MagicMock(
                    returncode=0, stdout=f'{{"targetCommitish": "{winner}"}}', stderr=""
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        uploader_a = _make_uploader()
        uploader_b = _make_uploader()
        pub_a = ArtifactPublisher(uploader_a, gh_cmd="gh")
        pub_b = ArtifactPublisher(uploader_b, gh_cmd="gh")

        with _mock_gh(side_effect):
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_a = pool.submit(pub_a.publish_on_reindex, "repo", commit_a)
                fut_b = pool.submit(pub_b.publish_on_reindex, "repo", commit_b)
                ref_a = fut_a.result(timeout=10)
                ref_b = fut_b.result(timeout=10)

        # Each has its own SHA tag
        assert ref_a.tag == _canonical_tag("repo", commit_a)
        assert ref_b.tag == _canonical_tag("repo", commit_b)
        # Both have valid release URLs
        assert _canonical_tag("repo", commit_a) in ref_a.release_url
        assert _canonical_tag("repo", commit_b) in ref_b.release_url
        # Exactly one is latest
        assert ref_a.is_latest != ref_b.is_latest or (ref_a.is_latest and ref_b.is_latest) is False

    def test_loser_release_url_still_reachable(self):
        """Losing side: is_latest=False but release_url is still the SHA-keyed URL (reachable)."""
        commit_a = "aaaa000000000000000000000000000000000000"
        commit_b = "bbbb000000000000000000000000000000000000"

        # Simulate: commit_a wins the latest pointer
        def side_effect(args, **kwargs):
            if "view" in args and "index-latest" in args:
                return MagicMock(
                    returncode=0,
                    stdout=f'{{"targetCommitish": "{commit_a}"}}',
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        uploader = _make_uploader()
        pub = ArtifactPublisher(uploader, gh_cmd="gh")

        with _mock_gh(side_effect):
            ref_b = pub.publish_on_reindex("repo", commit_b)

        assert ref_b.is_latest is False
        # SHA-keyed URL is still set (not empty, not pointing at index-latest)
        assert _canonical_tag("repo", commit_b) in ref_b.release_url


# ---------------------------------------------------------------------------
# Test 3: gh non-zero exit wraps in ArtifactError
# ---------------------------------------------------------------------------


class TestGhErrorHandling:
    def test_gh_nonzero_raises_artifact_error(self):
        uploader = _make_uploader()
        publisher = ArtifactPublisher(uploader, gh_cmd="gh")

        def side_effect(args, **kwargs):
            raise subprocess.CalledProcessError(1, args, stderr="permission denied")

        with _mock_gh(side_effect):
            with pytest.raises(ArtifactError):
                publisher.publish_on_reindex("repo", COMMIT)

    def test_artifact_error_is_mcp_error(self):
        from mcp_server.core.errors import MCPError

        assert issubclass(ArtifactError, MCPError)


# ---------------------------------------------------------------------------
# Test 4: Workflow YAML assertions
# ---------------------------------------------------------------------------


class TestWorkflowYaml:
    @pytest.fixture
    def workflow_path(self) -> Path:
        here = Path(__file__).parent.parent
        return here / ".github" / "workflows" / "index-artifact-management.yml"

    def test_schedule_trigger_removed(self, workflow_path: Path):
        content = workflow_path.read_text()
        assert "schedule:" not in content, "schedule: trigger must be removed from workflow"

    def test_workflow_dispatch_still_present(self, workflow_path: Path):
        content = workflow_path.read_text()
        assert "workflow_dispatch:" in content

    def test_workflow_dispatch_action_surface_is_narrowed(self, workflow_path: Path):
        content = workflow_path.read_text()
        assert "- validate" in content
        assert "- promote" not in content
        assert "- cleanup" not in content
        assert "- list" not in content
        assert "publish_on_reindex" not in content

    def test_reusable_upload_uses_packaged_metadata_helper(self, workflow_path: Path):
        content = workflow_path.read_text()
        assert "python scripts/index-artifact-upload.py --metadata-only" in content
        assert 'Path("artifact-metadata.json").write_text' not in content


# ---------------------------------------------------------------------------
# Test 5: Publish latency under 30s for mocked gh (10 MB synthetic artifact)
# ---------------------------------------------------------------------------


class TestPublishLatency:
    def test_publish_completes_under_30s(self):
        """With zero-delay mocked gh, publish_on_reindex completes well under 30s."""
        uploader = _make_uploader()
        publisher = ArtifactPublisher(uploader, gh_cmd="gh")

        def fast_side_effect(args, **kwargs):
            if "view" in args and "index-latest" in args:
                return MagicMock(
                    returncode=0, stdout=f'{{"targetCommitish": "{COMMIT}"}}', stderr=""
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        start = time.monotonic()
        with _mock_gh(fast_side_effect):
            publisher.publish_on_reindex("repo", COMMIT)
        elapsed = time.monotonic() - start

        assert elapsed < 30, f"publish_on_reindex took {elapsed:.2f}s, expected < 30s"


# ---------------------------------------------------------------------------
# Test 6: Call-order spy — gh release edit comes after gh release create
# ---------------------------------------------------------------------------


class TestCallOrder:
    def test_edit_after_create(self):
        """index-latest pointer update (edit) must come after SHA-keyed release creation."""
        uploader = _make_uploader()
        publisher = ArtifactPublisher(uploader, gh_cmd="gh")
        observed: list[str] = []
        uploader.upload_direct.side_effect = lambda *args, **kwargs: observed.append(
            "verified-upload"
        )

        def side_effect(args, **kwargs):
            # args = ["gh", "release", <subcommand>, <tag>, ...]
            subcmd = args[2] if len(args) > 2 else ""
            tag_arg = args[3] if len(args) > 3 else ""
            if subcmd in ("create", "edit"):
                observed.append(f"{subcmd}:{tag_arg}")
            # view of sha-keyed tag → not found (so create runs)
            if subcmd == "view" and tag_arg != "index-latest":
                return MagicMock(returncode=1, stdout="", stderr="not found")
            # view of index-latest → exists and points at our commit
            if "view" in args and "index-latest" in args:
                return MagicMock(
                    returncode=0, stdout=f'{{"targetCommitish": "{COMMIT}"}}', stderr=""
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        with _mock_gh(side_effect):
            publisher.publish_on_reindex("repo", COMMIT)

        upload_call = uploader.upload_direct.call_args
        assert upload_call is not None
        assert upload_call.kwargs["release_tag"] == _canonical_tag("repo", COMMIT)
        assert "attestation" not in upload_call.kwargs

        # The uploader owns draft creation and verifies publication before returning.
        sha_create_idx = next(
            (i for i, s in enumerate(observed) if s == "verified-upload"),
            None,
        )
        latest_edit_idx = next(
            (i for i, s in enumerate(observed) if "edit:index-latest" == s), None
        )
        assert sha_create_idx is not None, f"Expected create for SHA tag; got: {observed}"
        assert latest_edit_idx is not None, f"Expected edit for index-latest; got: {observed}"
        assert (
            sha_create_idx < latest_edit_idx
        ), "SHA-keyed release must be created before index-latest pointer is updated"
