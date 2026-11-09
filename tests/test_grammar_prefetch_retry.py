"""Retry policy of ``mcp-index grammars prefetch`` (Consiliency/Code-Index-MCP#131).

The fake downloader raises errors shaped exactly like tree-sitter-language-pack
1.20's native downloader (a ``RuntimeError`` whose text carries the full URL),
and a fake clock makes the backoff observable without sleeping.
"""

from __future__ import annotations

import logging

import pytest
from click.testing import CliRunner

from mcp_server.utils import grammar_cache
from mcp_server.utils.grammar_cache import (
    PREFETCH_ATTEMPTS,
    PREFETCH_DEADLINE_SECONDS,
    GrammarPrefetchError,
    classify_download_error,
)

MANIFEST = (
    "https://github.com/xberg-io/tree-sitter-language-pack/releases/download/v1.20.0/parsers.json"
)
SECRET = "token=super-secret-value"


def _http_error(status: int) -> RuntimeError:
    return RuntimeError(
        f"Download error: Failed to fetch manifest from {MANIFEST}?{SECRET}: http status: {status}"
    )


class _FakeDownloader:
    def __init__(self, *outcomes: BaseException | None) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[list[str]] = []

    def __call__(self, names: list[str]) -> None:
        self.calls.append(list(names))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if outcome is not None:
            raise outcome


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _prefetch(downloader, clock, **kwargs):
    return grammar_cache.prefetch(
        ["python", "rust"],
        downloader=downloader,
        sleep=clock.sleep,
        clock=clock,
        jitter=kwargs.pop("jitter", lambda: 1.0),
        **kwargs,
    )


def test_a_504_then_success_passes_after_one_retry(caplog):
    downloader = _FakeDownloader(_http_error(504), None)
    clock = _FakeClock()

    with caplog.at_level(logging.WARNING, logger="mcp_server.utils.grammar_cache"):
        assert _prefetch(downloader, clock) == ["python", "rust"]

    assert downloader.calls == [["python", "rust"], ["python", "rust"]]
    assert len(clock.sleeps) == 1
    assert "attempt 1/5 failed (HTTP 504 for " + MANIFEST + ")" in caplog.text
    assert "super-secret-value" not in caplog.text


def test_a_persistent_504_fails_after_the_attempt_bound_within_the_time_bound(caplog):
    downloader = _FakeDownloader(_http_error(504))
    clock = _FakeClock()

    with caplog.at_level(logging.WARNING, logger="mcp_server.utils.grammar_cache"):
        with pytest.raises(GrammarPrefetchError) as raised:
            _prefetch(downloader, clock)

    assert len(downloader.calls) == PREFETCH_ATTEMPTS == 5
    assert raised.value.attempts == 5
    assert raised.value.failure.status == 504
    assert (
        str(raised.value) == f"grammar download failed after 5 attempts (HTTP 504 for {MANIFEST})"
    )
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    # Exponential backoff, worst-case jitter: 4 + 8 + 16 + 32 seconds.
    assert clock.sleeps == [4.0, 8.0, 16.0, 32.0]
    assert clock.now <= PREFETCH_DEADLINE_SECONDS
    assert caplog.text.count("retrying in") == 4
    assert "super-secret-value" not in caplog.text + str(raised.value)


def test_jitter_keeps_each_delay_within_half_to_full_backoff():
    clock = _FakeClock()
    with pytest.raises(GrammarPrefetchError):
        _prefetch(_FakeDownloader(_http_error(503)), clock, jitter=lambda: 0.0)
    assert clock.sleeps == [2.0, 4.0, 8.0, 16.0]


def test_a_404_fails_immediately_without_retrying():
    downloader = _FakeDownloader(_http_error(404))
    clock = _FakeClock()

    with pytest.raises(GrammarPrefetchError) as raised:
        _prefetch(downloader, clock)

    assert len(downloader.calls) == 1
    assert clock.sleeps == []
    assert raised.value.attempts == 1
    assert str(raised.value) == f"grammar download failed (HTTP 404 for {MANIFEST}); not retried"


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("Checksum mismatch for 'python': expected abc, got def"),
        type("ChecksumMismatchError", (RuntimeError,), {})("bundle digest differs"),
        _http_error(403),
        RuntimeError("Download error: grammar 'nope' is not in the download manifest"),
    ],
)
def test_checksum_mismatches_and_other_failures_are_never_retried(error):
    downloader = _FakeDownloader(error)
    clock = _FakeClock()

    with pytest.raises(GrammarPrefetchError):
        _prefetch(downloader, clock)

    assert len(downloader.calls) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize(
    "error",
    [
        _http_error(429),
        _http_error(500),
        _http_error(502),
        RuntimeError(
            f"Download error: Failed to fetch manifest from {MANIFEST}: "
            "io: Connection reset by peer (os error 104)"
        ),
        RuntimeError(f"Download error: Failed to download {MANIFEST}: io: timed out"),
    ],
)
def test_transient_failures_are_retried(error):
    downloader = _FakeDownloader(error, None)
    clock = _FakeClock()

    assert _prefetch(downloader, clock) == ["python", "rust"]
    assert len(downloader.calls) == 2


def test_no_retry_starts_past_the_deadline():
    downloader = _FakeDownloader(_http_error(504))
    clock = _FakeClock()

    with pytest.raises(GrammarPrefetchError, match=r"after 2 attempts within 10s"):
        _prefetch(downloader, clock, deadline_seconds=10)

    assert len(downloader.calls) == 2
    assert clock.sleeps == [4.0]


def test_classification_strips_credentials_query_and_fragment_from_urls():
    failure = classify_download_error(
        RuntimeError(
            "Download error: Failed to download "
            "https://user:pass@example.test:8443/a/parsers.tar.zst?sig=abc#frag: http status: 504"
        )
    )
    assert failure.transient
    assert failure.url == "https://example.test:8443/a/parsers.tar.zst"
    assert failure.describe() == "HTTP 504 for https://example.test:8443/a/parsers.tar.zst"


def test_cli_reports_a_sanitized_failure(monkeypatch, tmp_path):
    from mcp_server.cli.grammar_commands import grammars

    monkeypatch.setattr(grammar_cache, "configure_cache_dir", lambda cache_dir: tmp_path)

    class _Pack:
        @staticmethod
        def prefetch(names):
            raise _http_error(404)

    monkeypatch.setattr(grammar_cache, "_pack", lambda: _Pack)

    result = CliRunner().invoke(grammars, ["prefetch", "--language", "python"])

    assert result.exit_code == 1
    assert "Grammar prefetch failed: GrammarPrefetchError: grammar download failed" in result.output
    assert "HTTP 404" in result.output
    assert "super-secret-value" not in result.output
