"""Tests for artifact freshness verification (IF-0-P12-5)."""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from mcp_server.artifacts.freshness import FreshnessVerdict, verify_artifact_freshness


def _meta(commit: str = "abc123", days_ago: int = 1) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"commit": commit, "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ")}


class TestFreshnessVerdictEnum:
    def test_str_values(self):
        assert FreshnessVerdict.FRESH == "fresh"
        assert FreshnessVerdict.STALE_COMMIT == "stale_commit"
        assert FreshnessVerdict.STALE_AGE == "stale_age"
        assert FreshnessVerdict.INVALID == "invalid"

    def test_json_serialisable(self):
        import json

        assert json.dumps(FreshnessVerdict.FRESH) == '"fresh"'


def test_freshness_uses_selected_repository_without_changing_cwd(tmp_path, monkeypatch):
    from tests.test_git_index_manager import _get_head_commit, _make_git_repo

    repo = _make_git_repo(tmp_path)
    head = _get_head_commit(repo)
    monkeypatch.chdir(tmp_path)
    assert (
        verify_artifact_freshness(_meta(commit=head), head, 14, repo_path=repo)
        is FreshnessVerdict.FRESH
    )


class TestFresh:
    def test_fresh_when_ancestor_and_within_age(self):
        meta = _meta(commit="abc123", days_ago=3)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            verdict = verify_artifact_freshness(meta, "HEAD", max_age_days=14)
        assert verdict is FreshnessVerdict.FRESH

    def test_fresh_at_boundary_age(self):
        # 13 days old should be FRESH with max_age_days=14.
        meta = _meta(commit="abc123", days_ago=13)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            verdict = verify_artifact_freshness(meta, "HEAD", max_age_days=14)
        assert verdict is FreshnessVerdict.FRESH


class TestStaleCommit:
    def test_stale_commit_when_not_ancestor(self):
        meta = _meta(commit="abc123", days_ago=1)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "git merge-base")
            verdict = verify_artifact_freshness(meta, "HEAD", max_age_days=14)
        assert verdict is FreshnessVerdict.STALE_COMMIT


class TestStaleAge:
    def test_stale_age_when_commit_is_ancestor_but_too_old(self):
        meta = _meta(commit="abc123", days_ago=15)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            verdict = verify_artifact_freshness(meta, "HEAD", max_age_days=14)
        assert verdict is FreshnessVerdict.STALE_AGE

    def test_stale_age_exactly_one_day_over(self):
        meta = _meta(commit="abc123", days_ago=15)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            verdict = verify_artifact_freshness(meta, "HEAD", max_age_days=14)
        assert verdict is FreshnessVerdict.STALE_AGE


class TestInvalid:
    def test_invalid_when_meta_missing_commit(self):
        meta = {"timestamp": "2024-01-01T00:00:00Z"}
        verdict = verify_artifact_freshness(meta, "HEAD", max_age_days=14)
        assert verdict is FreshnessVerdict.INVALID

    def test_invalid_when_meta_missing_timestamp(self):
        meta = {"commit": "abc123"}
        verdict = verify_artifact_freshness(meta, "HEAD", max_age_days=14)
        assert verdict is FreshnessVerdict.INVALID

    def test_invalid_when_meta_empty(self):
        verdict = verify_artifact_freshness({}, "HEAD", max_age_days=14)
        assert verdict is FreshnessVerdict.INVALID

    def test_invalid_when_timestamp_malformed(self):
        meta = {"commit": "abc123", "timestamp": "not-a-date"}
        verdict = verify_artifact_freshness(meta, "HEAD", max_age_days=14)
        assert verdict is FreshnessVerdict.INVALID


class TestDownloadSelectedArtifactFallback:
    """GitHub-outage: CalledProcessError/URLError/RuntimeError returns local-index + WARN."""

    def _make_downloader(self):
        from mcp_server.artifacts.artifact_download import IndexArtifactDownloader

        with patch.object(IndexArtifactDownloader, "_detect_repository", return_value="owner/repo"):
            return IndexArtifactDownloader(repo="owner/repo")

    def _artifact(self):
        return {"id": 1, "name": "index-main-abc123", "created_at": "2024-01-01T00:00:00Z"}

    @pytest.mark.parametrize(
        "exc",
        [
            subprocess.CalledProcessError(1, "gh"),
            RuntimeError("GitHub outage"),
        ],
    )
    def test_outage_exception_returns_local_result_with_warn(self, exc, tmp_path, caplog):
        downloader = self._make_downloader()
        with (
            patch.object(downloader, "download_artifact", side_effect=exc),
            caplog.at_level(logging.WARNING, logger="mcp_server.artifacts.artifact_download"),
        ):
            result = downloader.download_selected_artifact(
                self._artifact(), output_dir=tmp_path, backup=False
            )
        assert result.installed_items == []
        assert any(
            "warn" in r.message.lower()
            or "outage" in r.message.lower()
            or "local" in r.message.lower()
            for r in caplog.records
        )

    def test_attribute_error_reraises(self, tmp_path):
        """Programming errors must not be swallowed."""
        downloader = self._make_downloader()
        with patch.object(downloader, "download_artifact", side_effect=AttributeError("bug")):
            with pytest.raises(AttributeError):
                downloader.download_selected_artifact(
                    self._artifact(), output_dir=tmp_path, backup=False
                )

    def test_type_error_reraises(self, tmp_path):
        """Programming errors must not be swallowed."""
        downloader = self._make_downloader()
        with patch.object(downloader, "download_artifact", side_effect=TypeError("bug")):
            with pytest.raises(TypeError):
                downloader.download_selected_artifact(
                    self._artifact(), output_dir=tmp_path, backup=False
                )


class TestEnvVarMaxAgeDays:
    def test_default_max_age_days_is_14(self, monkeypatch):
        monkeypatch.delenv("MCP_ARTIFACT_MAX_AGE_DAYS", raising=False)
        meta = _meta(commit="abc123", days_ago=13)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            from pathlib import Path

            from mcp_server.artifacts.artifact_download import IndexArtifactDownloader

            with patch.object(
                IndexArtifactDownloader, "_detect_repository", return_value="owner/repo"
            ):
                downloader = IndexArtifactDownloader(repo="owner/repo")

            meta_path = Path("/tmp/artifact-metadata.json")
            import json

            meta_path.write_text(json.dumps(meta))
            with (
                patch.object(downloader, "download_artifact", return_value=Path("/tmp")),
                patch.object(downloader, "install_indexes", return_value=["code_index.db"]),
                patch(
                    "mcp_server.artifacts.artifact_download.json.loads",
                    return_value=meta,
                ),
                patch("pathlib.Path.exists", return_value=True),
                patch("pathlib.Path.read_text", return_value=json.dumps(meta)),
            ):
                result = downloader.download_selected_artifact(
                    {"id": 1, "name": "index-main-abc123", "created_at": "2024-01-01"},
                    output_dir=Path("/tmp"),
                    backup=False,
                )
            assert result.installed_items == ["code_index.db"]

    def test_env_var_max_age_days_honoured(self, monkeypatch):
        monkeypatch.setenv("MCP_ARTIFACT_MAX_AGE_DAYS", "7")
        meta = _meta(commit="abc123", days_ago=8)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            verdict = verify_artifact_freshness(meta, "HEAD", max_age_days=7)
        assert verdict is FreshnessVerdict.STALE_AGE
        monkeypatch.delenv("MCP_ARTIFACT_MAX_AGE_DAYS")
