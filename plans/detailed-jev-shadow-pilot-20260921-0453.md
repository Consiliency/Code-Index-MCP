---
title: Opt-in Jev shadow evaluation after correctness repairs
type: detailed
status: draft
owner_skill: codex-plan-detailed
created_at: "2026-09-21T04:53:34Z"
baseline_commit: 7f3d32ccdda1ad61d3efceee905c234e6ceddfda
execution_authorized: false
panel_approved: false
live_pilot_authorized: false
automation:
  suite_command: >-
    env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest
    tests/test_jev_reranker.py tests/test_jev_pilot.py
    tests/test_endpoint_reranker.py tests/test_rerank_contracts.py
    tests/test_rerank_consumer_migration.py tests/test_reranker_outcomes.py
    tests/test_bootstrap.py tests/test_multi_repo_bootstrap_order.py
    tests/test_tool_handlers_readiness.py tests/test_python_client_contract.py
    tests/smoke/test_mcpbase_stdio_smoke.py
    -m 'not requires_network and not benchmark' -q --no-cov -o log_cli=false
---

# Detailed Plan: Jev Shadow Pilot

## Task

Build an optional query-time Jev experiment behind explicit egress and repository
consent, using the existing EndpointReranker contract. Keep local retrieval,
embeddings, current reranking, and returned search results authoritative.
The first feature supports only off and shadow modes, not active promotion.

Dependencies:

1. `plans/detailed-reranker-correctness-20260921-0453.md`.
2. `plans/detailed-search-cache-index-isolation-20260921-0453.md`.
3. Complete the current v13 release process without adding this feature to
   the pending v1.4.1 candidate. Rebase/reconfirm source ownership afterward.

Authored outside Plan Mode as planning only. Neither implementation nor live
pilot execution is authorized by this document. No tests or API calls ran
during this planning pass.

## Research Summary And Limits

The previous assessment made 40 successful evaluation POSTs using hand-written
synthetic snippets only; responses identified `jev-1.13.0`. Ten implementation
queries with four candidates each had correct top-1 choices in both Noul and
Score modes. Four-candidate batch latency had a 162 ms median; one 20-candidate
probe took about 189 ms. Total reported usage was 50,072 input and 4,996 output
tokens, with approximately $0.0021 estimated input cost, not a verified bill.

This is not production code-search evidence. The lexical control was standalone
FTS5, not this application's complete retrieval pipeline; local cross-encoders
were not benchmarked. Lower ordering changed when batch order changed. A
correct candidate had zero Score confidence, so multiplying relevance by
confidence or dropping low-confidence candidates is not justified.

Evidence pointers:

- `/home/viperjuice/.local/state/code-index-mcp/research/jev-20260921/assessment.md`
- `results.jsonl`, `summary.json`, and `no_answer.jsonl` in that directory.
  The summary covers the original 36 requests, not the four later probes.
- [API](https://docs.typesafe.ai/api),
  [models](https://docs.typesafe.ai/models),
  [confidence](https://docs.typesafe.ai/confidence),
  [limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13),
  [data terms](https://docs.typesafe.ai/legal).

Recheck model availability, pricing, request schema, and account data terms
before live execution. The public legal benchmark and one injection-resistant
synthetic example are not code-quality or security acceptance.

## Frozen Boundary

`mcp_server/interfaces/rerank_contracts.py:33-34` freezes `rerank.v1`.
Lines 49-51 require stable candidate-ID correlation; lines 74-77 require one
response result per requested candidate regardless of `top_k`.
Lines 18-23 prohibit cross-provider/response score comparability assumptions.
`mcp_server/indexer/reranker.py:147-159` supplies the existing outcome enum.
Introduce no new public rerank outcome, tool name, embedding schema, or
readiness vocabulary. Internal shadow telemetry must not claim that the
user-visible result was successfully reranked.

## Implementation Decisions

### Typed Adapter And Bounded Transport

- Add a small Typesafe transport adapter, not a second reranking framework.
  It maps `rerank.v1` to `POST https://api.typesafe.ai/v1/systemone` and maps
  validated typed answers back through EndpointReranker.
- Pin `jev-1.13.0`; reject unexpected served model revisions. Use one Noul
  question per candidate with a versioned implementation-relevance rubric.
  Put query and candidate text in structured state as untrusted data.
  Instructions must explicitly reference `candidates.<stable_id>`; question
  keys alone do not provide that reference to the model.
- Validate answer identity/count/type and finite values in the documented
  Noul range. Unknown/missing/duplicate answers, model mismatch, HTTP errors,
  malformed JSON, and timeouts preserve the baseline. Do not synthesize answers,
  normalize against another provider, multiply by confidence, or filter results
  by a guessed no-answer threshold.
- Add explicit async-transport support to EndpointReranker while retaining its
  existing sync callback contract. Use existing `httpx.AsyncClient`, created
  and closed on the invocation loop, with an outer total deadline plus bounded
  connect/read/write/pool timeouts. Disable redirects, implicit environment
  proxy use, and retries. No new runtime dependency is required.
- The repair plan's remaining-deadline cap applies. Permit at most one active
  shadow request per dispatcher, no queue. Failure/skipped shadow work adds no
  replacement background task or alternate commercial-provider call.
- Cap the initial payload at 20 candidates, 2,000 UTF-8 bytes per candidate,
  2,000 query bytes, and 48 KiB for the entire serialized body. Truncate snippets
  only at valid character boundaries, with a recorded flag. Reject oversize
  requests rather than split batches and compare incompatible scores. These
  are conservative byte caps, not claims of exact tokenizer accounting.

### Explicit Consent And Shadow Semantics

- Add `MCP_JEV_MODE=off|shadow`, default off. Keep the existing
  `RERANKER_TYPE` and baseline reranker unchanged. There is no active mode.
- Dispatch requires an explicitly commercial inference profile, learned-model
  permission, `MCP_ALLOW_COMMERCIAL_EGRESS=1`, a nonempty
  `MCP_JEV_ALLOWED_REPO_IDS` allowlist of stable repository IDs, and
  `TYPESAFE_API_KEY`. All conditions must be true at the call boundary.
  Do not inherit the legacy unset-egress-means-allowed behavior.
- Resolve the repository ID through the current authorized RepoContext, not
  an unchecked caller string. Missing context, mixed repositories, stale
  generation, lexical-only/fleet-local profile, or missing consent skips
  shadow dispatch. Possession of a key is not permission to send private code.
- First scope is implementation-relevance code search through EnhancedDispatcher.
  Skip exact symbol/regex routes and document-only/mixed-document batches.
  Hybrid/cross-repo paths without this verified context remain unsupported;
  do not silently reuse a code rubric for documentation retrieval.
- Score only a bounded sample of the already selected baseline candidates.
  Preserve exact public result membership, order, scores, and metadata;
  shadow telemetry is separate. Do not expand production retrieval breadth.
- Do not add a judgment cache in this first feature. If later justified, its
  key must include query, model/rubric, full ordered batch contents, generation,
  and policy; observed batch dependence rules out per-candidate reuse.
- Record counts, timings, token usage, model/rubric versions, opaque run IDs,
  outcome, and payload hashes only. No raw query/source/secret logging or API
  response dumps. Public/synthetic evaluation fixtures may be separately
  retained with their provenance and license.

### Quality And Cost Qualification

Extend the existing reranking comparison utility with an explicit read-only
dataset mode; do not default to its historical `/app/code_index.db`.
Use at least 200 held-out, independently labeled implementation queries,
stratified across languages and hard negatives. Freeze labels before examining
Jev answers. Publish dataset/candidate-pool hashes and judge provenance.

Compare unchanged local retrieval, the configured hybrid baseline, one
explicitly available local reranker, and Jev on identical candidate pools.
Report recall at 20/40 before reranking, MRR, nDCG@10, per-stratum regressions,
latency p50/p95, failure/fallback rate, usage, and observed spend. Missing local
model dependencies are "not evaluated", not a win or permission to download.
Any local model install or baseline inference spend needs separate approval.

For a future promotion recommendation, require at least 5% relative nDCG@10
improvement over the best qualified baseline, positive paired-bootstrap lower
confidence bound, no greater than 2% absolute nDCG regression in any declared
stratum, p95 added latency <=500 ms, <=1% failed/invalid requests, and no
correctness/privacy regression. Record insufficient sample power honestly.
These are proposed gates for the experiment, not observed results.

Passing the pilot does not activate reranking. A separate approved change must
define final ordering relative to hybrid heuristics, candidate oversampling,
additional search routes, and any confidence/intent-routing policy.

## Changes

Five production/utility files; existing bootstrap paths are tested, not
duplicated with new provider construction.

| File | Entity | Action And Reason |
| --- | --- | --- |
| `mcp_server/indexer/jev_reranker.py` | adapter, rubric, policy factory | Create the provider-specific request/answer mapping and fail-closed consent policy. |
| `mcp_server/indexer/reranker.py` | `EndpointReranker` transport invocation | Modify to accept an explicitly async transport without breaking synchronous consumers. |
| `mcp_server/config/settings.py` | isolated Jev shadow settings and environment parsing | Add validated off/shadow configuration outside semantic profile/vector identity. |
| `mcp_server/dispatcher/dispatcher_enhanced.py` | common construction and eligible search finalization | Add one optional shadow hook using current RepoContext and baseline results. |
| `scripts/utilities/benchmark_reranking_comparison.py` | argument parsing, dataset runner, report/check functions | Modify for deterministic fixtures, bounded opt-in live evaluation, and machine-checkable evidence. |

Create `tests/test_jev_reranker.py` and `tests/test_jev_pilot.py`; extend endpoint,
consumer, bootstrap, and STDIO smoke tests named in the suite. Test both
`initialize_stateless_services` and the existing STDIO startup path so the
shared construction works in the app, not only with an injected unit-test object.

## Documentation And PMCP

Modify `docs/guides/inference-profiles.md` with explicit consent/defaults,
provider limitations, payload caps, and the rollback switch.
Create `docs/operations/jev-shadow-pilot.md` for the approved-corpus/budget
receipt, dataset protocol, PMCP secret injection, evidence locations, and UI
smoke steps. API keys remain in 1Password and process environment, never repo
configuration or logs. Use the existing Typesafe item in Consiliency Deploy
Secrets; do not print its value or duplicate it into a local file.

PMCP owns launching the chosen version, injecting secret references and these
explicit settings, and preserving repository identity. This repo owns policy
enforcement, request validation, scoring, fallback, telemetry, and evaluation.
This is not a fresh PMCP capability audit or an assertion that its UI already
exposes these new knobs.

## Verification

All commands below are future work, not executed during planning.

**J1:** Run the exact frontmatter `automation.suite_command`, using mocked
HTTP/denied network. Cover typed response failures, all consent permutations,
missing RepoContext, unsupported routes, no retries, cancellation/resource
closure, payload caps, injection-like text, redaction, unchanged public
responses, local-profile zero egress, and scorer changes causing no reindex.

**J2:** Add this utility CLI and exercise it against committed synthetic/public
fixtures in `tests/test_jev_pilot.py`. The new paths below are planned artifacts:

```sh
uv run --locked --extra dev python scripts/utilities/benchmark_reranking_comparison.py --dataset tests/fixtures/jev/heldout-synthetic.jsonl --offline --output /tmp/jev-offline-report.json
```

The offline mode uses injected recorded synthetic answers, never credentials
or live clients. It proves report math/budget enforcement, not Jev quality.

**J3, separately authorized live experiment:** same dataset mode with
`--live-jev --approval-file <receipt> --max-requests 220 --max-input-tokens 4000000 --max-seconds 900 --max-concurrency 1 --output <report>`.
The approval receipt binds model, code/dataset hashes, allowed repo IDs/corpus
visibility, provider data terms, and a USD ceiling (proposed $0.25). No retries.
Reserve a conservative input-token upper bound before each dispatch; settle
against response usage and stop on missing usage. Include all prompts/rubrics
and warmups. Enforce request/time/spend limits even on failed responses.
At exhaustion stop incomplete; never loosen limits to finish the sample.
Recheck pricing before approval; this proposed budget is not an authorization.

**J4, operational smoke and evidence check:** use an approved public fixture
repository through actual PMCP -> STDIO search, plus existing admin/Inspector
UI where available. Exercise off, shadow, policy denial, timeout, and outage;
verify baseline results and redacted status in both surfaces. Capture browser
console and screenshot evidence without secrets or source payloads.
Add `--check-evidence <directory>` to the comparison utility to validate the
receipt schema, exact candidate/dataset hashes, report completeness, and
required UI/STDIO artifacts. This checks evidence integrity, not the truth of
a screenshot. Missing UI/access evidence means operational acceptance pending.

Store J3/J4 operational artifacts outside the repo under
`~/.local/state/code-index-mcp/research/jev-pilot/<run-id>/`.
Before a runner relies on them, stamp a plan amendment referencing the approval,
exact-head evaluation report, and UI/STDIO receipts through the installed
detailed-plan executor's acceptance mechanism. Do not substitute offline unit
tests or the old synthetic assessment for these operational acceptance items.

## Acceptance Criteria

- [ ] J1 proves no outbound call without all explicit profile/egress/repository
  consent gates and preserves default/local-only behavior.
- [ ] J1 proves typed mapping, model pinning, bounds, cancellation, redaction,
  and unchanged baseline results on success and every failure.
- [ ] J1 proves no embedding/index identity change and both startup paths use
  the same policy; unsupported search routes remain unchanged.
- [ ] J2 proves reproducible report math and strict budget refusal without
  network, private corpus, or model downloads.
- [ ] J3 plus J4 evidence check accounts for all calls/usage and reports held-out
  quality against qualified baselines, explicitly pass/fail/inconclusive.
- [ ] J4 proves the approved path in actual STDIO and available app/browser
  surfaces, with exact-head evidence and no secrets; absent evidence stays open.

## Completion And Rollback

The deliverable is an implemented off-by-default adapter, bounded shadow mode,
and honest qualification report. A negative or inconclusive quality result is
a valid pilot outcome, not permission to promote. Private-source egress still
needs corpus-specific approval and account-terms validation.

Rollback is `MCP_JEV_MODE=off` and process restart; no vector migration, index
rebuild, data deletion, or commercial fallback. Do not reset the existing
release-pilot budget ledger or include this feature in the pending v1.4.1.
