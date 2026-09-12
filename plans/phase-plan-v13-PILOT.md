---
phase_loop_plan_version: 1
phase: PILOT
roadmap: specs/phase-plans-v13.md
roadmap_sha256: 178b8328d8e7dc76ddc0804d7b72d3ccddb55e23577a3d52cb7cbd70d5fd5308
automation:
  suite_command: "env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests/test_gateway_transport_boundary.py tests/test_repository_readiness.py tests/test_v13_pilot_budget.py tests/test_v13_pmcp_pilot.py tests/test_semantic_profile_settings.py tests/test_summarization.py tests/test_deployment_profiles.py tests/integration/test_sigterm_shutdown.py tests/test_v13_safety.py -q --no-cov"
---

# PILOT: Installed PMCP And Bounded Local Acceptance

## Context

Execute manually only after accepted DATA and SAFETY receipts. Roadmap, FREEZE
policy, audit inputs and accepted receipts remain read-only. The phase-loop CLI
entrypoint problem remains tracked by Consiliency/agent-harness#819; use standalone
verification, not the disabled outer loop. Two serial lanes, no implementation
agents, in the existing isolated feature worktree.

PMCP supports project-config provisioning, but its manifest does not pin the
candidate or Python. Explicit config does not suppress user/project overlays.
Isolate HOME, project, registry, roots, policy and locks. PMCP has no bundled
dashboard; browser evidence names actual MCP Inspector and HTTP admin UIs.

Offline prerequisites: JSON-only profiles omit summarizer settings; summary
sampling/BAML/commercial fallback need deployment-policy tests. Installed
in-flight shutdown is 15 seconds, exceeding PILOT's five. Reproduce and repair
before spending inference allowance. Plan validation makes no endpoint requests.

## Interface Freeze Gates

- [ ] IF-0-PILOT-1 - Exact-candidate PMCP provisioning, installed lifecycle, browser workflows, bounded inference provenance and frozen performance/resource evidence accepted.

## Frozen Operational Contract

Preserve entrypoints and .mcp.json mcpServers env configuration. CLI and PMCP
child share MCP_REPO_REGISTRY, MCP_INDEX_STORAGE_PATH and MCP_ALLOWED_ROOTS.
Optional MCP_CLIENT_SECRET uses a synthetic secret only in private fixture
config. Child imports the independently installed exact wheel, not checkout or
PYTHONPATH source. Core-wheel and production-extra installs are distinguished;
metrics ownership requires the locked production extra, not an unavailable
optional exporter. Reconnect and gateway.provision use that configured child.

Summary policy reuses existing deployment-profile helpers: explicit local
profiles cannot fall back to commercial providers or unbounded client sampling;
explicit lexical-only makes no learned requests. Preserve unset legacy behavior.
JSON enrichment metadata and explicit YAML resources resolve their stated
endpoint/model roles. A missing explicitly selected resource must fail clearly.

Approved allowance is cumulative across attempts: 100000 input tokens, 900
seconds from first admission, concurrency one, synthetic local inputs only.
Use endpoint-compatible tokenization or conservative serialized UTF-8 bytes
before every request, including retries/provenance probes. Durably reserve before
forwarding; failed requests consume reservations. Restart, ledger corruption,
denial and elapsed time cannot reset or refund the allowance. No redirects,
ambient HTTP proxies, arbitrary target URLs or inherited commercial keys.

Budget guard is an operational tool, not OS-level production egress confinement.
Its loopback routes forward only to the frozen local endpoint roles. Never log
request/response bodies, credentials or source. Record reported versus declared
model/revision, dimension, request counts, reservations and hashes. Unreported
immutable revisions stay unreported, not invented or inferred from model names.

Use the two-repository workload in scripts/v13_pilot_estimate.py plus one sibling
worktree. Before admission freeze corpus/queries/hashes, request envelopes and
sample counts in a run manifest. Exercise registration, reindex, no-match,
sibling/wrong-branch/stale refusal, committed modify/rename/delete, rebuild,
restart and interrupted-publication recovery. Runtime files stay untracked.

Measure 20 warm queries per class per repository (40 measured semantic queries).
Allow two semantic warmups and six lifecycle probes, at most 48 semantic tool
attempts. Freeze request envelopes at 88084 conservative input units: eight
summary requests, twelve document embeddings, 48 query embeddings and twenty
metadata/attestation probes. These bounds include retries, not extra allowance.
Run the same installed workflow against loopback synthetic providers first;
rehearsal evidence cannot satisfy live quality or latency acceptance. At least 20
successful ready queries per measured class overlap indexing another repo.
Record contention intervals and refused samples separately; fast refusals are
not successful search latency. Sample actual process-tree RSS during load.
Frozen bounds: symbol p95 <=100ms, search p95 <=500ms, shutdown <=5s,
process RSS <=2048MiB. No threshold tuning or allowance expansion.

Signal/EOF controls include admitted mutation, pending-fence preservation,
children, metrics-port reuse/contention, reconnect and repeat provisioning.
Deleting the only indexed source file must make the old generation unavailable;
an empty full rebuild may refuse rather than claim ready. Restore the synthetic
file, commit and rebuild before asserting ready recovery. Do not count that
negative control as a successful retrieval or relax the stale-generation fence.
File substantive upstream PMCP blockers with qualified bounded reproductions;
do not edit sister repos or waive readiness.

Live execution is an effect, not an automatically repeatable check. Repair
offline failures before admission. Repeat validation consumes bound receipts
without more inference. Candidate changes invalidate candidate-dependent proof.
An exhausted/expired allowance needs an explicit owner decision, not a new ledger.

## Lane Index & Dependencies

SL-0 — Configuration, policy, lifecycle and budget foundation
  Depends on: (none)
  Blocks: SL-1
  Parallel-safe: no

SL-1 — Installed operational driver, browser and documentation reducer
  Depends on: SL-0
  Blocks: (none)
  Parallel-safe: no

## Lanes

### SL-0 - Configuration, policy, lifecycle and budget foundation

- **Scope**: Repair local admission/shutdown prerequisites and enforce cumulative pilot allowance.
- **Depends on**: (none)
- **Owned files**: `mcp_server/gateway.py`, `tests/test_gateway_transport_boundary.py`, `mcp_server/health/repository_readiness.py`, `tests/test_repository_readiness.py`, `tests/test_v13_data_reconciliation.py`, `mcp_server/config/settings.py`, `mcp_server/indexing/summarization.py`, `mcp_server/cli/stdio_runner.py`, `scripts/safety_runtime_smoke.py`, `scripts/v13_pilot_estimate.py`, `scripts/v13_pilot_budget.py`, `tests/test_semantic_profile_settings.py`, `tests/test_summarization.py`, `tests/test_deployment_profiles.py`, `tests/integration/test_sigterm_shutdown.py`, `tests/test_v13_safety.py`, `tests/test_v13_pilot_budget.py`
- **Interfaces provided**: fresh-single-probe-readiness, complete-profile-resources, summary-policy-admission, five-second-owned-shutdown, durable-local-budget
- **Interfaces consumed**: freeze-contract (pre-existing), accepted-data-contract (pre-existing), accepted-safety-contract (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - test: Reproduce missing JSON endpoint, missing explicit YAML fallback and local-failure commercial/sampling/BAML paths using intercepted providers and synthetic keys; zero real requests.
  - impl: Reuse deployment-policy helpers across summary paths, retaining local model roles and clear failures. No generated BAML edits or dependency upgrades.
  - test: Strengthen actual installed SIGTERM/SIGINT/EOF/repeated/partial/in-flight controls to five seconds with real children and preserved publication fence.
  - impl: Bound owned shutdown without acknowledging unfinished work or abandoning children.
  - test: Budget exact-boundary/retry/concurrency/time/restart/corruption/route/log controls with loopback stubs.
  - test: Update the pure estimate assertion to include framing reservations; do not change DATA source/storage/query expectations or accepted receipts.
  - impl: Add durable reservation ledger and narrow local forwarding guard; derive pure estimate from workload/envelopes.
  - test: Preserve fresh SHA-1/SHA-256 commit, branch, tracked edit and missing/detached Git refusals with real repositories, including staged/unstaged/rename/delete and untracked controls; assert one Git subprocess per classification.
  - impl: Read fresh machine-framed Git status once per classification without cross-request caches or removing query/generation fences. Recheck installed warm symbol latency before any inference.
  - test: Installed Swagger omits the existing repository selector from symbol/search/reindex forms and reports the FastAPI default version; assert schema selectors and installed package version.
  - impl: Expose the existing optional repository query parameter and installed version in the admin schema, preserving header precedence, path/readiness checks, auth and direct-call compatibility.
  - verify: Focused admin-schema/readiness/policy/budget/lifecycle tests and formatting pass before any live inference.

### SL-1 - Installed operational driver, browser and documentation reducer

- **Scope**: Prove production-installed workflows and reduce all upstream evidence into a fail-closed verdict.
- **Depends on**: SL-0
- **Owned files**: `scripts/v13_pmcp_pilot.py`, `tests/test_v13_pmcp_pilot.py`, `scripts/release_smoke.py`, `scripts/installed_runtime_smoke.py`, `scripts/agent_validation.py`, `docs/operations/v13-pmcp-pilot.md`, `docs/SUPPORT_MATRIX.md`, `docs/status/V13_EXECUTION.md`, `docs/validation/v13/PILOT.json`
- **Interfaces provided**: pmcp-installed-receipts, browser-receipts, budgeted-contention-receipts, pilot-rollout-verdict
- **Interfaces consumed**: fresh-single-probe-readiness, complete-profile-resources, summary-policy-admission, five-second-owned-shutdown, durable-local-budget, freeze-contract (pre-existing), accepted-data-contract (pre-existing), accepted-safety-contract (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - test: Receipt reducer rejects candidate/manifest drift, missing goals, over-budget/threshold results, absent successful contention samples and missing browser evidence.
  - impl: Build candidate wheel and install through pinned uvx/Python outside checkout; provision installed PMCP with fresh HOME/project/config/policy/lockdir and explicit resources.
  - verify: No-inference CLI/SDK/PMCP lifecycle, handshake, metrics contention, reconnect/reprovision and two-repo/sibling controls; assert actual module/interpreter and child identities.
  - impl: Prepare pinned Inspector and intended FastAPI admin UI on isolated loopback ports.
  - verify: Use browser accessibility snapshots for query/no-match/refusal/error, reindex/reconnect actions; screenshots for visuals, then console checks. Hash evidence and distinguish Inspector/admin from unshipped PMCP dashboard.
  - impl: Verify local metadata, freeze workload and admit live requests through the budget guard only after offline gates.
  - verify: Correct retrieval, committed lifecycle, provenance, latency, process RSS and contention within all frozen limits.
  - impl: Stop only owned resources; preserve bounded evidence and file substantiated upstream issues.
  - verify: Exact-source local/installed/full regressions. Revalidate original blocked checks after repair, without automatically repeating inference.
  - impl: Reduce all results into PILOT receipt and provisioning/support verdict. No accepted EC or IF with a missing goal.

## Execution Notes

2026-09-11 browser amendment: Inspector exercised installed PMCP symbol queries
for both repos, ready no-match, unsupported sibling refusal, disconnect/reconnect
and a post-reconnect query with no console errors. Admin Swagger visibly omits
repository inputs on symbol/search/reindex and reports default version 0.1.0.
SL-0 owns the bounded admin schema correction and tests. The first isolated admin
launch also required explicit SEMANTIC_DEFAULT_PROFILE=legacy-default; SL-1
owns complete fixture configuration. These are diagnostic observations, not
PILOT acceptance. All owned browser processes stopped without survivors.

2026-09-11 R07 amendment: the production-extra installed offline driver passed
all 13 goals on bca40bc, including real metrics refusal/recovery and six lifecycle
controls. Diagnostic warm symbol p95 was 137.489ms through PMCP and 125.915ms
through direct STDIO, failing the unchanged 100ms bound. Lexical PMCP p95 was
179.631ms. Live inference remains unstarted. SL-0 serially owns the bounded
readiness probe repair and regression tests before resuming SL-1; no accepted
upstream receipt, query fence, roadmap byte or threshold changes are authorized.

2026-09-11 SL-0 checkpoint: 20 policy/configuration counterexamples reproduced
with provider interception and no real requests; repaired focused coverage is
191 passing tests. The installed core-wheel five-second check first failed at
six seconds against the old watchdog. After repair all six lifecycle controls
passed, including an admitted mutation exit at 3.071 seconds, failed exit status,
preserved pending fence and no child survivors. Core-wheel metrics are absent;
production-extra metrics ownership remains an explicit SL-1 requirement.
Budget loopback tests retain off-host socket denial and cover durable accounting,
deadline/rollback, failed attempts, unsettled restart, corruption, authentication,
redirect refusal and non-overlapping forwarding. No live allowance initialized.
The pure envelope estimate now includes framing overhead; the approved limits
and immutable roadmap have not changed. No PILOT EC or IF is accepted yet.

Read committed source, public installed PMCP package/CLI metadata, accepted
receipts, frozen audit inputs and synthetic fixtures created for v13.
Scratch read/write: `.phase-loop/runs/v13-PILOT-*/**`, `build/**`,
`index_it_mcp.egg-info/**`. Ignored handoffs remain ignored.
Browser-plugin output exception: read and archive only this run's generated
`/home/viperjuice/code/Code-Index-MCP/.playwright-mcp/v13-*.png` screenshots and
`/home/viperjuice/code/Code-Index-MCP/v13-*.png` diagnostic screenshots into owned
PILOT scratch. No source/config/private input in the primary checkout is allowed.
Screenshots are tool-generated outputs; retain them without staging the primary
checkout. Hash archived copies and inspect images before accepting visual proof.
Only credentials created for this fixture may be used; redact values from all
logs, screenshots and receipts. No ambient env files, PMCP user config, private
registries/indexes, raw fleet source or old private runs. Fresh browser context,
never an existing login session. Active plan and plans/manifest.json are control
outputs; upstream roadmap/contracts/receipts stay read-only. Never stage raw scratch.
Control outputs also include `.dev-skills/handoffs/codex-plan-phase/**` and
`.dev-skills/handoffs/codex-execute-phase/**`, retained as ignored metadata.
Accepted DATA proof inputs are read-only:
`.phase-loop/runs/v13-DATA-20260911-668bf75/verification.json` and
`.phase-loop/runs/v13-DATA-20260911-668bf75/verification.log`.

## Verification

- Structural plan validator, locked Python 3.12 refresh, frontmatter suite, changed-Python isort/black and git diff --check.
- `uv run --locked --extra dev python scripts/v13_pilot_estimate.py`
- `uv run --locked --extra dev python scripts/v13_pmcp_pilot.py --mode prepare`
- `uv run --locked --extra dev python scripts/v13_pmcp_pilot.py --mode offline`
- Actual isolated Playwright workflow and `scripts/v13_pmcp_pilot.py --mode verify-browser`.
- `uv run --locked --extra dev python scripts/v13_pmcp_pilot.py --mode live` only after manifest/budget admission, never automatically retried.
- `uv run --locked --extra dev python scripts/v13_pmcp_pilot.py --mode verify-live` for repeatable receipt checks without inference.
- `make agent-gate`, `make release-smoke-container`.
- Separate Git manager and broad offline suites exclude only network/benchmark markers, preserving skips and failures.

Wheel/PMCP nodes cap at 300s, container build at 900s, browser at 300s,
contention at 300s within total 900s inference allowance. Separate named nodes
record partial results/timeouts. Broad proof may exceed five minutes; report all
failures without reducing coverage. Runner evidence binds source/tree, artifacts,
commands/exit codes, time/environment, per-goal results, exclusions and operational
receipts. Final validation is read-only and makes no additional inference.

## Acceptance Criteria

- [ ] EC-PILOT-1 - proven by `scripts/v13_pmcp_pilot.py --mode offline` provisioning/query controls; falsified by ambient config, checkout imports, wrong resources or incorrect readiness.
- [ ] EC-PILOT-2 - proven by `tests/test_v13_pilot_budget.py` and `scripts/v13_pmcp_pilot.py --mode verify-live`; falsified by egress/budget/provenance violations, missing ready contention samples or threshold misses.
- [ ] EC-PILOT-3 - proven by actual browser actions and `scripts/v13_pmcp_pilot.py --mode verify-browser`; falsified by missing tested surfaces, error/refusal controls, screenshots or console checks.
- [ ] EC-PILOT-4 - proven by `scripts/v13_pmcp_pilot.py --mode offline` installed lifecycle and process/fence controls; falsified by over-five-second exit, survivors, stale handles or unsafe interrupted publication.

## Spec Closeout Plan

- schema: `spec_delta_closeout.v1`
- decision: `canonical_spec_update`
- target surfaces: `docs/operations/v13-pmcp-pilot.md`, `docs/SUPPORT_MATRIX.md`, `docs/status/V13_EXECUTION.md`
- evidence paths: `docs/validation/v13/PILOT.json`
- redaction posture: `metadata_only`
- downstream handling: PREP only after accepted receipt; no default reranker enablement, fleet expansion or release.

## External Inputs

Installed PMCP 2.7.3, Python 3.12, locked project dependencies; Qdrant image
`qdrant/qdrant@sha256:f1c7272cdac52b38c1a0e89313922d940ba50afd90d593a1605dbbc214e66ffb`.
MCP Inspector 2.6.0 with supported Node; verify CLI help before use.
Local hints http://ai:8001/v1 embedding and http://ai:8002/v1 enrichment.
Revalidate reported metadata before inference. Official Inspector docs discovered
through PMCP Context7:
https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/docs/2026-07-28/tools/inspector/web.mdx
