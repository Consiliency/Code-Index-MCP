---
title: Reranker correctness and bounded failure
type: detailed
status: draft
owner_skill: codex-plan-detailed
created_at: "2026-09-21T04:53:34Z"
baseline_commit: 7f3d32ccdda1ad61d3efceee905c234e6ceddfda
roadmap_ref: specs/phase-plans-v13.md
execution_authorized: false
panel_approved: false
automation:
  suite_command: >-
    env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest
    tests/test_rerank_contracts.py tests/test_endpoint_reranker.py
    tests/test_rerank_consumer_migration.py tests/test_reranker_outcomes.py
    tests/test_tool_handlers_readiness.py tests/test_python_client_contract.py
    tests/test_dispatcher.py tests/test_dispatcher_toctou.py
    -m 'not requires_network and not benchmark' -q --no-cov -o log_cli=false
---

# Detailed Plan: Reranker Correctness

## Task

Repair selected-chunk binding, score validation, and deadline/fallback behavior
before adding a scoring provider. This is the first bounded repair plan, not
a new roadmap or approval to execute. Authored outside Plan Mode at the user's
request; no product changes or verification commands ran during planning.

## Research Summary

At the baseline commit, a synthetic SQLite-backed dispatcher reproduction
returned the matching chunk at line 100 while scoring an unrelated larger
chunk at line 1. Endpoint scoring accepted NaN and sorted numeric strings
"2" and "10" lexicographically before float conversion. The tool handler has
a 10-second outer timeout, but the endpoint defaults to 30 seconds; cancelling
the await does not stop its synchronous transport.

Prior evidence:
`/home/viperjuice/.local/state/code-index-mcp/research/jev-20260921/assessment.md`.
These are prior assessment results, not tests run during planning. No Jev API,
new model dependency, paid inference, or indexing is needed for these repairs.

## Frozen Contracts

Read at the baseline:

- `mcp_server/interfaces/rerank_contracts.py:33-34`:
  `RERANK_CONTRACT_VERSION = "rerank.v1"`.
- Lines 49-51: "`candidate_id` is a STABLE, caller-assigned id. It is the only
  key used to correlate a request candidate with its response result".
- Lines 74-77: "`top_k` ... is a HINT ... it does NOT truncate the response
  `results` array, which must carry one result per requested candidate".
- Lines 18-23: scores are meaningful within one provider response, not across
  providers or responses.
- `mcp_server/indexer/reranker.py:147-159`: `not_configured`, `attempted`,
  `succeeded`, `failed`, `fallback_applied`, and `skipped_policy`.

No new wire version or outcome vocabulary. Partial failure stays representable
on the wire. The consumer policy intentionally becomes whole-batch fallback
when any candidate could not be scored; no synthetic zero scores.

## Implementation Decisions

### Selected Chunk

Carry the selected lexical chunk's indexed text and start/end lines into the
private rerank payload. For hits lacking that text, use the owning store's
existing `find_chunk_at_line`, verifying that its range contains the hit.
Never hydrate with the largest chunk. Semantic candidates retain their supplied
chunk text/provenance. Missing or inconsistent hydration uses the existing hit
snippet, not an unrelated chunk, filename-only document, or raw filesystem read.

Preserve caller-visible result objects, metadata, path guards, and generation
checks; strip private hydration fields from the public response. Scoring may
change order, not candidate identity. No oversampling, new symbol/regex route,
chunk-schema change, or hybrid postprocessing redesign belongs in this fix.

### Numeric And Failure Policy

Validate version, request correlation, unique request candidate IDs, exact
response-ID bijection, and declared counts before sorting. Scored values must
be finite JSON numbers, not strings, booleans, null, NaN, or infinity. Finite
negative scores remain valid. Sort numerically with request-order tie breaking.
Reject contradictory status/score pairs; unscored candidates have no score.

Keep the existing wire-fault exception convention. Provider-declared partial
or complete failure returns `Result.error`; consumers preserve the original
candidates/order and report `failed`. Only an actual alternate provider
invocation reports `fallback_applied`. Valid unchanged order is `attempted`;
a genuine reorder is `succeeded`. Empty input makes no transport call.
Use fixed safe error classifications, never provider response bodies or raw
exception strings that could echo source, queries, or credentials.

### Deadline And Resource Bound

Set the endpoint default to 0.75 seconds; retain `MCP_RERANK_TIMEOUT_S` for an
explicit override. Non-finite/non-positive values use the safe default. Remove
the current one-second minimum; never stretch a remaining deadline to a floor.

Capture an absolute monotonic deadline in `handle_search_code` before executor
submission. Pass it as an internal optional `execute_search_service` keyword,
not a new MCP parameter. Scope it with a context variable in the existing
reranker module and reset in `finally`. Copy that context when the sync bridge
creates a worker thread. Direct clients without a deadline retain the endpoint
cap. At dispatch use `min(configured_cap, remaining_search_time - 0.1)`; skip
optional scoring if no budget remains. The reserve does not promise to rescue
retrieval that already exceeded the tool deadline.

Allow at most one outstanding synchronous transport per endpoint instance,
with no waiting queue. Acquire capacity before dispatch and release only when
the actual callable exits, not when its await times out. While busy, return
baseline results promptly. A timed-out thread cannot safely be forcibly
cancelled: keep its reservation until exit and do not spawn replacements.
Production HTTP transports also need finite network timeouts.

Carry request-local diagnostic snapshots through the sync adapter; a late
completion cannot overwrite another call's outcome. Do not add retries,
detached retries, unbounded thread growth, or implicit commercial fallback.

## Changes

Five required production files, plus one conditional consumer adjustment:

| File | Entity | Action And Reason |
| --- | --- | --- |
| `mcp_server/dispatcher/dispatcher_enhanced.py` | `search`, chunk hydration, `_apply_reranker`, `_SyncDictEndpointReranker`, timeout helper | Modify selected-text/range propagation and truthful failure handling. |
| `mcp_server/interfaces/rerank_contracts.py` | `validate_rerank_response` | Modify boundary validation before numeric ordering. |
| `mcp_server/indexer/reranker.py` | `EndpointReranker`, `SyncRerankerAdapter`, `run_coroutine_sync`; internal deadline context | Modify deadline, bounded transport lifecycle, and request-local outcomes. |
| `mcp_server/client.py` | `execute_search_service` | Add internal deadline propagation without weakening readiness checks. |
| `mcp_server/cli/tool_handlers.py` | `handle_search_code` | Modify executor submission to carry elapsed tool budget. |
| `mcp_server/indexer/hybrid_search.py` | rerank consumer | Modify only if needed for whole-batch failure and diagnostic snapshots; preserve fusion/heuristics. |

Modify the first six test files named in the suite. Retain dispatcher and
generation-regression coverage. Use synthetic stores and injected transports.

## Documentation Impact

Modify `docs/guides/inference-profiles.md` and endpoint/contract docstrings for
the new default, all-or-original policy, and synchronous cancellation limit.
No changes to embeddings, PMCP provisioning, or public tool names.

## Dependencies And Order

1. Reconfirm HEAD/ownership; preserve the release and fourth-review repair
   worktrees. This plan is based on `7f3d32c`, not a moving branch.
2. Before execution on the release candidate, amend existing v13 PREP owned
   paths and verification scope through its normal review/amendment mechanism.
   This plan is not an approved scope amendment.
3. Add failing regressions, fix chunk identity and numeric handling, then
   deadline/lifecycle behavior. Run R1 then R2.
4. Reconcile the shared consumer with the cache/isolation plan. Feed combined
   exact-head evidence into EC-PREP-1/2/3 in `specs/phase-plans-v13.md`.
   Those broader criteria are not independently satisfied by this plan.

## Verification

Future commands only. Fixtures must deny network and use temporary stores;
environment flags alone are not an outbound-network guard.

**R1, narrow:**

```sh
env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests/test_rerank_contracts.py tests/test_endpoint_reranker.py tests/test_rerank_consumer_migration.py -m 'not requires_network and not benchmark' -q --no-cov -o log_cli=false
```

**R2:** Run the exact frontmatter `automation.suite_command`.

Cover unequal chunks in one file, multiple hits per file, stale/missing chunk,
ties, negatives, strings, bool/null/NaN/infinities, wrong versions/counts/IDs,
partial/all failures, empty input, executor delay, exhausted deadline, running
event loop, late completion, concurrency, and generation replacement.
Use fake clocks and event-controlled transports; release blocked fixtures in
cleanup. Assert actual transport-start counts and held capacity. Retain one
generously bounded end-to-end deadline regression to catch bridge hangs.

## Acceptance Criteria

- [ ] R1 proves scoring uses the matching chunk/ID, never an unrelated chunk
  or unauthorized file read.
- [ ] R1 proves validation precedes numeric ordering, malformed batches fail
  safely, and finite scores/ties order correctly.
- [ ] R1 proves partial/all failures preserve baseline order and membership
  with truthful outcomes and no zero-score promotion.
- [ ] R2 proves scoring respects the remaining tool budget and falls back
  without a new tool timeout when baseline retrieval still fits.
- [ ] R2 proves repeated timeouts permit only one outstanding synchronous
  call per instance and late completion cannot corrupt diagnostics.
- [ ] R2 proves readiness, explicit-semantic failure behavior, path/generation
  safety, Python clients, and the frozen contracts remain intact.

## Release Boundary

These are repair candidates for the pending release, subject to its scope
amendment, tests, four-seat review, and reconciliation. No Jev feature, private
egress, indexing, merge, or publication is authorized by this planning artifact.
Do not reuse old approval receipts for changed implementation bytes.
