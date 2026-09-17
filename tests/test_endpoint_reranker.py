"""Tests for RERANKEND lane R1 (IF-0-RERANKEND-1).

Covers:
    * ``EndpointReranker`` reorders candidates per a fake transport's scores;
    * stable ``candidate_id`` values survive request -> response reordering;
    * per-candidate partial failure is represented;
    * missing / unknown ids RAISE via ``validate_rerank_response``;
    * the sync/async bridge ``run_coroutine_sync`` runs an async rerank from
      synchronous code both WITH and WITHOUT an already-running event loop;
    * the Cohere ``/v2/rerank`` adapter builds a v2-shaped request;
    * in-process rerankers are selectable ONLY via the explicit standalone flag;
    * the canonical interface consolidation (single source of truth).

No network is performed anywhere: every transport is a fake.
"""

import asyncio
from dataclasses import replace

import pytest

from mcp_server.indexer.reranker import (
    COHERE_CURRENT_RERANK_MODEL,
    COHERE_LEGACY_RERANK_MODEL,
    CohereV2RerankAdapter,
    CrossEncoderReranker,
    EndpointReranker,
    FlashRankReranker,
    IReranker,
    RerankerFactory,
    RerankOutcome,
    RerankResult,
    SearchResult,
    SyncRerankerAdapter,
    VoyageReranker,
    run_coroutine_sync,
)
from mcp_server.interfaces import indexing_interfaces
from mcp_server.interfaces.rerank_contracts import (
    RerankContractError,
    RerankRequest,
)

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _results(n: int = 3):
    """Build ``n`` SearchResults with distinct, identifiable snippets."""
    letters = ["alpha", "beta", "gamma", "delta", "epsilon"]
    return [
        SearchResult(
            file_path=f"{letters[i]}.py",
            start_line=i + 1,
            end_line=i + 1,
            column=0,
            snippet=letters[i],
            match_type="exact",
            score=0.5,
        )
        for i in range(n)
    ]


def _fake_transport(scores_by_index, *, reorder=True, latency_ms=5.0):
    """Return a transport that scores candidates by their positional index.

    ``scores_by_index`` maps candidate index -> score (or ``None`` to mark a
    per-candidate FAILED result). Response results are optionally reordered to
    exercise id-stable correlation.
    """

    def transport(request_dict):
        req = RerankRequest.from_dict(request_dict)
        results = []
        scored = 0
        for idx, cand in enumerate(req.candidates):
            score = scores_by_index.get(idx)
            if score is None:
                status = RerankOutcome.FAILED
            else:
                status = RerankOutcome.SUCCEEDED
                scored += 1
            results.append(
                {
                    "candidate_id": cand.candidate_id,
                    "status": status.value,
                    "score": score,
                }
            )
        if reorder:
            results = list(reversed(results))
        return {
            "contract_version": req.contract_version,
            "request_id": req.request_id,
            "provider": "fake",
            "model_id": "fake-rerank-1",
            "model_revision": "2026-07-01",
            "results": results,
            "candidate_count": len(req.candidates),
            "scored_count": scored,
            "latency_ms": latency_ms,
        }

    return transport


# --------------------------------------------------------------------------
# Consolidation: single canonical interface
# --------------------------------------------------------------------------


def test_indexing_interfaces_reexports_canonical():
    """indexing_interfaces re-exports the canonical reranker symbols."""
    assert indexing_interfaces.IReranker is IReranker
    assert indexing_interfaces.RerankResult is RerankResult
    assert issubclass(EndpointReranker, IReranker)


@pytest.mark.asyncio
async def test_cache_key_covers_all_content_and_profile_inputs():
    from mcp_server.indexer.reranker import TFIDFReranker

    candidates = [replace(_results(1)[0], file_path=f"file-{i}.py") for i in range(11)]
    reranker = TFIDFReranker({"profile": "one"})
    before = await reranker._get_cache_key("query", candidates)
    assert before == await TFIDFReranker({"profile": "one"})._get_cache_key("query", candidates)
    assert before != await reranker._get_cache_key("query", candidates, top_k=1)
    changed_content = [replace(candidates[0], snippet="updated at the same line"), *candidates[1:]]
    assert before != await reranker._get_cache_key("query", changed_content)
    changed_last = [*candidates[:10], replace(candidates[10], context="generation two")]
    assert before != await reranker._get_cache_key("query", changed_last)
    bound = [replace(candidate, generation_key="repo:one:profile-a") for candidate in candidates]
    other_generation = [
        replace(candidate, generation_key="repo:two:profile-a") for candidate in candidates
    ]
    assert await reranker._get_cache_key("query", bound) != await reranker._get_cache_key(
        "query", other_generation
    )
    await reranker._cache_results("query", candidates, ["unbound"])
    assert await reranker._get_cached_results("query", candidates) is None
    await reranker._cache_results("query", bound, ["bound"])
    assert await reranker._get_cached_results("query", bound) == ["bound"]
    assert await reranker._get_cached_results("query", other_generation) is None
    reranker.config["profile"] = "two"
    assert before != await reranker._get_cache_key("query", candidates)


# --------------------------------------------------------------------------
# EndpointReranker: reorder + stable ids
# --------------------------------------------------------------------------


def test_endpoint_reranker_reorders_by_transport_scores():
    # Index 2 (gamma) gets the highest score, index 0 (alpha) the lowest, so the
    # output order genuinely differs from the input order.
    transport = _fake_transport({0: 0.1, 1: 0.5, 2: 0.9}, reorder=True)
    reranker = EndpointReranker(transport, provider="fake")

    result = run_coroutine_sync(reranker.rerank("q", _results(3), top_k=3))
    assert result.is_success
    rr = result.data
    assert isinstance(rr, RerankResult)

    order = [item.original_result.file_path for item in rr.results]
    assert order == ["gamma.py", "beta.py", "alpha.py"]
    assert reranker.last_outcome is RerankOutcome.SUCCEEDED
    assert rr.metadata["cross_provider_comparable"] is False


def test_stable_candidate_ids_survive_reordering():
    # Transport reverses result order; correlation must be by id, so the highest
    # score must still map to the right original SearchResult regardless.
    transport = _fake_transport({0: 0.1, 1: 0.2, 2: 0.99}, reorder=True)
    reranker = EndpointReranker(transport)

    result = run_coroutine_sync(reranker.rerank("q", _results(3), top_k=3))
    rr = result.data
    # gamma (index 2) had the top score -> must be first even though the
    # transport returned results reversed.
    assert rr.results[0].original_result.file_path == "gamma.py"
    assert rr.results[0].original_rank == 2
    assert rr.results[0].new_rank == 0


def test_per_candidate_partial_failure_represented():
    # Index 1 (beta) fails at the provider (score None -> FAILED).
    transport = _fake_transport({0: 0.9, 1: None, 2: 0.4}, reorder=False)
    reranker = EndpointReranker(transport)

    result = run_coroutine_sync(reranker.rerank("q", _results(3), top_k=3))
    rr = result.data

    assert "cand-1" in rr.metadata["partial_failures"]
    assert rr.metadata["candidate_statuses"]["cand-1"] == RerankOutcome.FAILED.value
    # Failed candidate is ordered LAST and carries an explanation.
    last = rr.results[-1]
    assert last.original_result.file_path == "beta.py"
    assert last.explanation and "failed" in last.explanation
    # Scored candidates keep their score-descending order ahead of the failure.
    assert [i.original_result.file_path for i in rr.results[:2]] == [
        "alpha.py",
        "gamma.py",
    ]


def test_missing_candidate_id_raises_contract_error():
    def transport(request_dict):
        req = RerankRequest.from_dict(request_dict)
        # Drop the last candidate from the response -> missing id.
        results = [
            {
                "candidate_id": c.candidate_id,
                "status": RerankOutcome.SUCCEEDED.value,
                "score": 0.5,
            }
            for c in req.candidates[:-1]
        ]
        return {
            "contract_version": req.contract_version,
            "request_id": req.request_id,
            "provider": "fake",
            "model_id": "m",
            "model_revision": "r",
            "results": results,
            "candidate_count": len(req.candidates),
            "scored_count": len(results),
        }

    reranker = EndpointReranker(transport)
    with pytest.raises(RerankContractError):
        run_coroutine_sync(reranker.rerank("q", _results(3), top_k=3))


def test_unknown_candidate_id_raises_contract_error():
    def transport(request_dict):
        req = RerankRequest.from_dict(request_dict)
        results = [
            {
                "candidate_id": c.candidate_id,
                "status": RerankOutcome.SUCCEEDED.value,
                "score": 0.5,
            }
            for c in req.candidates
        ]
        # Inject an id that was never requested.
        results.append(
            {
                "candidate_id": "cand-999",
                "status": RerankOutcome.SUCCEEDED.value,
                "score": 0.9,
            }
        )
        return {
            "contract_version": req.contract_version,
            "request_id": req.request_id,
            "provider": "fake",
            "model_id": "m",
            "model_revision": "r",
            "results": results,
            "candidate_count": len(req.candidates),
            "scored_count": len(results),
        }

    reranker = EndpointReranker(transport)
    with pytest.raises(RerankContractError):
        run_coroutine_sync(reranker.rerank("q", _results(3), top_k=3))


# --------------------------------------------------------------------------
# Sync/async bridge
# --------------------------------------------------------------------------


def test_run_coroutine_sync_without_running_loop():
    async def coro():
        await asyncio.sleep(0)
        return 41 + 1

    # No loop running in this thread -> fast path.
    assert run_coroutine_sync(coro()) == 42


def test_run_coroutine_sync_with_running_loop():
    # Drive the bridge from INSIDE a running event loop; it must not call
    # asyncio.run in the live loop but hand off to a worker thread.
    async def outer():
        async def inner():
            await asyncio.sleep(0)
            return "bridged"

        # We are inside a running loop here.
        return run_coroutine_sync(inner())

    assert asyncio.run(outer()) == "bridged"


def test_sync_reranker_adapter_drives_async_endpoint():
    transport = _fake_transport({0: 0.9, 1: 0.5, 2: 0.1}, reorder=True)
    adapter = SyncRerankerAdapter(EndpointReranker(transport))

    result = adapter.rerank("q", _results(3), top_k=3)
    assert result.is_success
    assert [i.original_result.file_path for i in result.data.results] == [
        "alpha.py",
        "beta.py",
        "gamma.py",
    ]


def test_sync_adapter_works_inside_running_loop():
    transport = _fake_transport({0: 0.9, 1: 0.5, 2: 0.1}, reorder=True)
    adapter = SyncRerankerAdapter(EndpointReranker(transport))

    async def outer():
        # Synchronous adapter.rerank invoked from within a running loop.
        return adapter.rerank("q", _results(3), top_k=3)

    result = asyncio.run(outer())
    assert result.is_success
    assert result.data.results[0].original_result.file_path == "alpha.py"


# --------------------------------------------------------------------------
# Cohere /v2/rerank adapter
# --------------------------------------------------------------------------


def test_cohere_adapter_builds_v2_shaped_request():
    captured = {}

    def send(body):
        captured["body"] = body
        # Cohere native response shape. Crucially, HONOR ``top_n`` as real
        # Cohere does (it truncates the response) so a bug that sets
        # top_n=top_k would produce a short response and fail contract
        # validation downstream.
        all_results = [
            {"index": 0, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.2},
            {"index": 2, "relevance_score": 0.5},
        ]
        return {
            "results": all_results[: body["top_n"]],
            "model_revision": "2026-06-01",
        }

    adapter = CohereV2RerankAdapter(send)  # default = current model
    assert adapter.model == COHERE_CURRENT_RERANK_MODEL

    reranker = EndpointReranker(adapter, provider="cohere")
    # Request top_k=2 but 3 candidates: the adapter must still ask Cohere to
    # score ALL candidates (top_n=3) so the response carries every candidate_id.
    result = run_coroutine_sync(reranker.rerank("find json", _results(3), top_k=2))
    assert result.is_success

    body = captured["body"]
    assert body["model"] == COHERE_CURRENT_RERANK_MODEL
    assert body["query"] == "find json"
    assert body["documents"] == ["alpha", "beta", "gamma"]
    assert body["top_n"] == 3  # full candidate count, NOT top_k

    # End-to-end through EndpointReranker: index 0 (alpha) scored highest;
    # EndpointReranker applies the top_k=2 truncation on its side.
    assert len(result.data.results) == 2
    assert result.data.results[0].original_result.file_path == "alpha.py"
    assert result.data.metadata["provider"] == "cohere"


def test_cohere_adapter_legacy_model_is_explicit_opt_in():
    def send(body):
        return {"results": []}

    # Default is the current model; legacy must be passed explicitly.
    assert CohereV2RerankAdapter(send).model == COHERE_CURRENT_RERANK_MODEL
    legacy = CohereV2RerankAdapter(send, model=COHERE_LEGACY_RERANK_MODEL)
    assert legacy.model == COHERE_LEGACY_RERANK_MODEL
    assert legacy.build_v2_request({"candidates": [], "query": "x"})["model"] == (
        COHERE_LEGACY_RERANK_MODEL
    )


# --------------------------------------------------------------------------
# In-process rerankers require an explicit standalone flag
# --------------------------------------------------------------------------


def test_in_process_reranker_requires_explicit_standalone_flag():
    factory = RerankerFactory()

    # Not selectable implicitly.
    with pytest.raises(ValueError, match="standalone"):
        factory.create_standalone_reranker("flashrank")

    # Selectable only with the explicit flag.
    fr = factory.create_standalone_reranker("flashrank", standalone_profile=True)
    assert isinstance(fr, FlashRankReranker)

    ce = factory.create_standalone_reranker("cross-encoder", standalone_profile=True)
    assert isinstance(ce, CrossEncoderReranker)


def test_in_process_standalone_unknown_name_raises():
    factory = RerankerFactory()
    with pytest.raises(ValueError, match="Unknown in-process"):
        factory.create_standalone_reranker("nope", standalone_profile=True)


# --------------------------------------------------------------------------
# Bridge timeout enforcement (no-running-loop fast path)
# --------------------------------------------------------------------------


def test_run_coroutine_sync_fast_path_enforces_timeout():
    """A slow coroutine + ``timeout=`` must raise on the no-running-loop path.

    This is the case that was previously unenforced: the fast path called
    ``run_until_complete(coro)`` and ignored ``timeout`` entirely.
    """

    async def slow():
        await asyncio.sleep(5.0)
        return "should never get here"

    # No loop running in this thread -> fast path; timeout must fire.
    with pytest.raises(asyncio.TimeoutError):
        run_coroutine_sync(slow(), timeout=0.05)


def test_run_coroutine_sync_fast_path_does_not_clobber_prior_loop():
    """The fast path must RESTORE the calling thread's pre-existing loop.

    Previously ``finally: set_event_loop(None)`` destroyed any non-running loop
    already bound to the thread.
    """
    prior = asyncio.new_event_loop()
    asyncio.set_event_loop(prior)
    try:

        async def coro():
            return 7

        assert run_coroutine_sync(coro()) == 7
        # The bridge's private loop must not have replaced ``prior``.
        assert asyncio.get_event_loop_policy().get_event_loop() is prior
    finally:
        asyncio.set_event_loop(None)
        prior.close()


# --------------------------------------------------------------------------
# EndpointReranker: sync transport runs OFF the event loop
# --------------------------------------------------------------------------


def test_endpoint_transport_runs_off_event_loop():
    """A blocking transport must not stall the running loop (no deadlock).

    The transport blocks until a concurrent loop task releases it. If the
    transport ran inline on the loop, the releasing task would never run and the
    gate would never be set -> deadlock.
    """
    import threading

    gate = threading.Event()

    def blocking_transport(request_dict):
        # Block until the still-responsive loop releases us.
        assert gate.wait(timeout=5.0), "transport not released -> event loop blocked"
        return _fake_transport({0: 0.9, 1: 0.5, 2: 0.1}, reorder=False)(request_dict)

    reranker = EndpointReranker(blocking_transport, provider="fake")

    async def outer():
        task = asyncio.create_task(reranker.rerank("q", _results(3), top_k=3))
        # Yield so rerank reaches the (off-loop) transport call.
        await asyncio.sleep(0.05)
        # Only reachable if the loop was NOT blocked by the transport.
        gate.set()
        return await task

    result = asyncio.run(outer())
    assert result.is_success
    assert result.data.results[0].original_result.file_path == "alpha.py"


# --------------------------------------------------------------------------
# Sync-trio reranker failure signal (last_error contract)
# --------------------------------------------------------------------------


def test_sync_reranker_last_error_signals_swallowed_failure():
    """A swallowed failure sets ``.last_error``; a success resets it to None.

    Also verifies redaction: secret-like substrings never reach ``last_error``.
    """
    # Bypass __init__ so the test needs no real voyageai client/API key.
    r = VoyageReranker.__new__(VoyageReranker)
    r._model = "rerank-2"
    r.last_error = None

    class _BoomClient:
        def rerank(self, **_kwargs):
            raise RuntimeError("auth failed: bearer sk-supersecrettoken123456")

    r._client = _BoomClient()
    cands = [{"snippet": "alpha"}, {"snippet": "beta"}]

    out = r.rerank("q", cands, top_k=2)
    # Original order preserved so callers don't break.
    assert out == cands[:2]
    # Failure signalled.
    assert r.last_error is not None
    assert "RuntimeError" in r.last_error
    # Secret scrubbed — no bearer token / api-key material leaks through.
    assert "sk-supersecrettoken123456" not in r.last_error
    assert "supersecrettoken" not in r.last_error

    class _OkResult:
        def __init__(self, index):
            self.index = index

    class _OkClient:
        def rerank(self, **_kwargs):
            class _Resp:
                results = [_OkResult(0), _OkResult(1)]

            return _Resp()

    r._client = _OkClient()
    out2 = r.rerank("q", cands, top_k=2)
    assert out2 == [cands[0], cands[1]]
    # A successful rerank clears the prior failure signal.
    assert r.last_error is None


# --------------------------------------------------------------------------
# Cohere adapter: out-of-range provider index is a hard error
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_index", [5, -1])
def test_cohere_adapter_raises_on_out_of_range_index(bad_index):
    def send(_body):
        # Malformed provider response: index outside the candidate range.
        return {"results": [{"index": bad_index, "relevance_score": 0.9}]}

    adapter = CohereV2RerankAdapter(send)
    request_dict = {
        "contract_version": "rerank.v1",
        "request_id": "r1",
        "query": "q",
        "candidates": [
            {"candidate_id": "cand-0", "text": "alpha"},
            {"candidate_id": "cand-1", "text": "beta"},
        ],
        "top_k": 2,
    }
    with pytest.raises(IndexError):
        adapter(request_dict)


# --------------------------------------------------------------------------
# CR2: EndpointReranker intrinsic default timeout (bounds DIRECT async
# consumers even without the dispatcher's external wait_for wrapper).
# --------------------------------------------------------------------------
def _slow_transport(latency_s: float):
    import time as _time

    def transport(request_dict):
        _time.sleep(latency_s)
        req = RerankRequest.from_dict(request_dict)
        return {
            "contract_version": req.contract_version,
            "request_id": req.request_id,
            "provider": "slow",
            "model_id": "slow-rerank-1",
            "model_revision": "2026-07-01",
            "results": [
                {
                    "candidate_id": c.candidate_id,
                    "status": RerankOutcome.SUCCEEDED.value,
                    "score": 0.5,
                }
                for c in req.candidates
            ],
            "candidate_count": len(req.candidates),
            "scored_count": len(req.candidates),
            "latency_ms": latency_s * 1000.0,
        }

    return transport


async def test_endpoint_reranker_intrinsic_default_timeout(monkeypatch):
    # No external wait_for wrapper: the EndpointReranker's own env-driven timeout
    # must still bound a slow transport. A sub-1s env value is clamped up to the
    # >=1s floor (INFERPOLISH item 4), so the intrinsic bound stays enabled.
    monkeypatch.setenv("MCP_RERANK_TIMEOUT_S", "0.05")
    reranker = EndpointReranker(_slow_transport(3.0), provider="slow")
    assert reranker._timeout == 1.0
    with pytest.raises(asyncio.TimeoutError):
        await reranker.rerank("q", _results(3))


async def test_endpoint_reranker_explicit_timeout_overrides_env(monkeypatch):
    monkeypatch.setenv("MCP_RERANK_TIMEOUT_S", "99")
    reranker = EndpointReranker(_slow_transport(1.0), provider="slow", timeout=0.05)
    assert reranker._timeout == 0.05
    with pytest.raises(asyncio.TimeoutError):
        await reranker.rerank("q", _results(3))


def test_endpoint_reranker_default_timeout_is_30s(monkeypatch):
    monkeypatch.delenv("MCP_RERANK_TIMEOUT_S", raising=False)
    reranker = EndpointReranker(_fake_transport({0: 0.1}), provider="fake")
    assert reranker._timeout == 30.0


# --------------------------------------------------------------------------
# INFERPOLISH item 4: EndpointReranker's own MCP_RERANK_TIMEOUT_S parsing must
# clamp to a >=1s floor (mirroring the dispatcher clamp) so a 0/negative env
# can never disable the intrinsic per-call timeout, and an unparseable value
# falls back to the 30s default.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["0", "0.0", "-5", "0.001"])
def test_endpoint_reranker_timeout_clamps_to_one_second_floor(monkeypatch, raw):
    monkeypatch.setenv("MCP_RERANK_TIMEOUT_S", raw)
    reranker = EndpointReranker(_fake_transport({0: 0.1}), provider="fake")
    assert reranker._timeout == 1.0


def test_endpoint_reranker_timeout_unparseable_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MCP_RERANK_TIMEOUT_S", "not-a-number")
    reranker = EndpointReranker(_fake_transport({0: 0.1}), provider="fake")
    assert reranker._timeout == 30.0


def test_endpoint_reranker_timeout_above_floor_is_preserved(monkeypatch):
    monkeypatch.setenv("MCP_RERANK_TIMEOUT_S", "12.5")
    reranker = EndpointReranker(_fake_transport({0: 0.1}), provider="fake")
    assert reranker._timeout == 12.5


# --------------------------------------------------------------------------
# INFERPOLISH item 5: a timed-out endpoint call must not publish outcome /
# diagnostics. Shared reranker state (last_outcome / last_diagnostics) is only
# assigned after a successful response is fully processed in the coroutine, so
# a late transport worker cannot mutate it after the bridge raised TimeoutError.
# --------------------------------------------------------------------------
def test_endpoint_reranker_timeout_does_not_publish_shared_state():
    import time as _time

    def slow_transport(request_dict):
        # Sleeps well past the tiny per-call timeout; if it "wins" late it must
        # still be unable to touch shared reranker state.
        _time.sleep(0.3)
        return {"results": []}

    reranker = EndpointReranker(slow_transport, provider="fake", timeout=0.01)

    with pytest.raises(asyncio.TimeoutError):
        run_coroutine_sync(reranker.rerank("find json", _results(2), top_k=2))

    assert reranker.last_outcome is None
    assert reranker.last_diagnostics is None


# --------------------------------------------------------------------------
# INFERPOLISH item 6: CohereV2RerankAdapter must report model_revision honestly
# -- pass through the provider's revision when present, else None (unknown),
# never echoing the configured ``model`` as a revision it isn't.
# --------------------------------------------------------------------------
def test_cohere_adapter_model_revision_none_when_response_omits_it():
    def send(body):
        # Cohere native response with NO model_revision field.
        return {
            "results": [
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.5},
            ]
        }

    adapter = CohereV2RerankAdapter(send)
    out = adapter(
        {
            "candidates": [
                {"candidate_id": "cand-0", "text": "a"},
                {"candidate_id": "cand-1", "text": "b"},
            ],
            "query": "q",
            "request_id": "r",
            "contract_version": "rerank.v1",
        }
    )

    # model_id honestly reports the configured model...
    assert out["model_id"] == COHERE_CURRENT_RERANK_MODEL
    # ...but model_revision is None (unknown), not the model echoed as a revision.
    assert out["model_revision"] is None


def test_cohere_adapter_model_revision_passes_through_when_present():
    def send(body):
        return {
            "results": [{"index": 0, "relevance_score": 0.9}],
            "model_revision": "2026-06-01",
        }

    adapter = CohereV2RerankAdapter(send)
    out = adapter(
        {
            "candidates": [{"candidate_id": "cand-0", "text": "a"}],
            "query": "q",
            "request_id": "r",
            "contract_version": "rerank.v1",
        }
    )

    assert out["model_revision"] == "2026-06-01"
