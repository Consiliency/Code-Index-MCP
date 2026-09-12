# V13 Manual Execution

Updated 2026-09-12. Branch: `codex/v13-audit-remediation`.
Worktree: `/mnt/workspace/worktrees/Code-Index-MCP-v13-audit-remediation`.

## Progress

| Phase | Status | Evidence |
| --- | --- | --- |
| FREEZE | Accepted; owner decisions approved | `docs/validation/v13/FREEZE.json` |
| DIST | Accepted | `docs/validation/v13/DIST.json`; IF-0-DIST-1 |
| STATE | Accepted | `docs/validation/v13/STATE.json`; IF-0-STATE-1 |
| SAFETY | Accepted | `docs/validation/v13/SAFETY.json`; IF-0-SAFETY-1 |
| DATA | Accepted | `docs/validation/v13/DATA.json`; IF-0-DATA-1 |
| PILOT | Executing; not accepted | Installed offline/browser baseline passed; live record-validation repair precedes inference |
| PREP | Not started | No version bump or implementation code review claimed |
| SHIP | Not started | No merge, publication or issue closure claimed |

The four-seat reconciled roadmap remains unchanged at SHA-256
`178b8328d8e7dc76ddc0804d7b72d3ccddb55e23577a3d52cb7cbd70d5fd5308`.
Its planning acceptance and the Grok requested-route/self-description caveat
remain in `specs/phase-plans-v13_reviews.md`.

## FREEZE Work

- Preserved the seven original roadmap/review/audit inputs byte-for-byte in this isolated worktree.
- Added `plans/phase-plan-v13-FREEZE.md`, with three serial lanes and explicit ownership.
- Added `docs/contracts/v13-freeze.json`: 33 finding mappings, all 19 application-table policies, shared ownership/admission and operational evidence contracts.
- Added offline structural and acceptance checks plus 13 passing contract tests.
- Installed locked dependencies in this worktree's own Python 3.12 environment.
- After the owner's explicit approval, reran acceptance successfully and emitted IF-0-FREEZE-1.
- Original main checkout, its index, unrelated worktrees and original v9 phase-loop history were not modified.

The independent verification helper (not the outer phase-loop runner) produced
`.phase-loop/runs/v13-FREEZE-20260911/verification.json` and its sealed log.
Summary: **13 passed in 1.98s; structural exit 0; acceptance exit 0**.
The earlier owner-decision blocker is resolved, not bypassed.

## Approved Owner Decisions

1. Synthetic-only local inference pilot: 100,000 total embedding input tokens including retries; 15 minutes maximum; no commercial egress or real fleet indexes.
2. Manual signing-only GitHub OIDC attestation job: five-minute cap; no new routine hosted CI and no private source/index uploads. Keep enforce-mode verification. Proposed mechanism follows the [GitHub attestation contract](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations); do not claim the signing workflow performed a local build.

The owner's reply "Approved" on 2026-09-11 explicitly accepted both proposals.
Approval is recorded in the contract and receipt; the original acceptance
command and standalone verification helper passed. Further publication/recovery
decisions remain in their phase's explicit gates.

The upstream startup warning remains tracked by [agent-harness#819](https://github.com/Consiliency/agent-harness/issues/819).
Normal closeout recovered and blocked synthetic invalid inputs in both tested
import orders earlier; this is not a reason to bypass candidate-specific checks.

## DIST Checkpoint

- SQL migrations are packaged, loaded as installed resources, reconciled transactionally, and checked for fresh/partial-upgrade/reopen parity.
- Preopened pooled connections refresh schema visibility; in-memory stores retain their database across connection boundaries.
- Real isolated wheel STDIO and configured non-root container HTTP workflows cover registration, indexing, queries, no match, refusal and restart.
- Corrected staged reindex output schemas and HTTP symbol line bounds exposed by those installed workflows.
- BAML generator/runtime/client are pinned and reproducible; historical tests, workflow census, changed-path routing and support/coverage documentation are aligned.
- The first stamped run recorded 2,975 broad-suite passes and 313 phase-suite passes, but rejected acceptance because the separately invoked Git integration command selected zero tests. Its evidence remains intact under `.phase-loop/runs/v13-DIST-20260911-final/`.
- Explicit integration selection exposed a pooled-schema regression; the focused reproducer and all nine Git integration tests now pass (16 combined tests). A new full stamped run must accept the final candidate, including wheel/image hashes, before IF-0-DIST-1 is produced.
- Subsequent installed checks exposed a source-only skip cache suppressing unchanged files in an empty staged database. Destination-content validation and a two-store regression now pass; the installed wheel passes again. This is a narrow distribution prerequisite, not acceptance of STATE's full generation/cache contract.

## Resume

DIST final candidate `3e99c40` passed all seven recorded commands, the locked
environment refresh and the 314-test phase suite. Broad offline baseline:
2,977 passed, 153 skipped, 31 deselected. Separately selected Git integration:
9 passed. Installed wheel and configured non-root image passed real workflows
and restart. Artifact hashes and exclusions are in the DIST receipt. Earlier
failed attempts remain preserved and are not accepted evidence.

Read `docs/validation/v13/DATA.json` and
`.dev-skills/handoffs/codex-execute-phase/latest.md` in this worktree.
Current phase: PILOT, finish installed provisioning and bounded operational acceptance.
Command: `codex-execute-phase plans/phase-plan-v13-PILOT.md`.
Do not restart v9 or infer v13 completion from old primary-checkout state.

## PILOT Checkpoint

The installed candidate `cc795c88cfe48e558a037123fb54a9beb2831dba` passed
all 13 offline PMCP goals and the actual Inspector/admin browser workflows:
two-repository queries, ready no-match, sibling refusal, admin reindex, and
disconnect/reconnect. Browser services stopped within 1.12 seconds with no
surviving children; recorded peak RSS was 1821.67 MiB. The Inspector had no
console errors before teardown; admin's expected sibling refusal produced HTTP
503. Two console messages appeared during intentional service teardown and
their details were not retained. These are bounded diagnostic observations,
not fleet or live-inference acceptance.

Its standalone baseline passed the local gate, non-root container, 19 Qdrant
controls per mode, 140 Git manager tests, 3279 broad offline tests (150 skipped,
31 deselected), and 529 phase tests. The overall artifact is rejected because
the formatting selection incorrectly included the immutable audit-input Python
file. That input remains unchanged; executable source/test formatting is the
correct scope. Evidence is preserved under
`.phase-loop/runs/v13-PILOT-cc795c88cfe4/offline-verification-resolved/`.

A separate RED counterexample demonstrated that hash-valid placeholders could
pass saved live-result validation without actual ledger/provenance records.
The repair reopens archived SQLite read-only, compares durable accounting and
request-class envelopes, reconstructs all 120 measured query observations and
contention counts, and checks workload, provider/revision, corpus and point-set
bindings. The driver archives those inputs before producing a final receipt.
All 96 focused budget/receipt tests pass, including inconsistent-record controls
and byte-preservation checks. A fresh candidate rehearsal and complete evidence
run are required before PILOT acceptance.

No live endpoint has been contacted and the actual allowance has never been
initialized. Its canonical root remains `.phase-loop/runs/v13-PILOT-allowance`;
100000 cumulative units, 900 seconds from first admission and concurrency one
remain unchanged. No threshold change, additional signing dispatch, version
bump, final code panel, merge, publication or fleet indexing has occurred.

## STATE Implementation History

The local implementation adds durable reload/mutate/fsync registry transactions,
registration identities and generation publication, cross-process writer locks,
generation-specific rebuild files, current-context query guards, draining pools,
watcher reconciliation and generation/profile/content-aware cache identities.
Scoped Python/STDIO/HTTP/task reindex uses a durable pending fence. Exceptions and
unclean outcomes cannot publish a successful generation. Active SQLite sidecars
are retained, including after failed force-full attempts; automatic destructive
rollback is removed.

Focused controls include actual spawned writers, an external reader holding an
old SQLite transaction during publication, a publisher killed before provenance,
constructor/shutdown races, external registration changes, nested/cancelled pool
borrows, real query-cache hits across generation changes, and scoped write fences.
The first broad offline run had 3,010 passes and two test assumptions to update.
Those assumptions were corrected; the latest registry/manager/watcher group had
216 passes and the new cache/mutation boundary group had 42 passes. These are
development results, not sealed exact-candidate acceptance. The next broad run
had 3,017 passes and seven failures from lightweight contexts lacking the new
generation property. A shared store-based identity preserves that compatibility;
105 targeted retrieval/reranker/state tests now pass. The local `make agent-gate`
also passes, including installed-wheel verification and 265 production/readiness
tests. Final stamped full-suite verification is still required.

The stamped `112c9e0` attempt passed the local gate and expanded phase suite but
is not accepted: the broad run had 3,023 passes, 153 skips, 31 deselections and
one older Cohere cache test that expected reuse without generation identity.
The test now covers both bound and unbound candidates, retaining metadata and
checking actual provider call counts. Its focused group passed 46 tests with
seven skips. The original failed evidence remains preserved; all original
stamped checks must pass on the repaired candidate before STATE closes.

DATA must implement staged vector/artifact restore and semantic handle retirement.
Until then, unsafe legacy extraction into the active directory is disabled and
semantic cache binding changes return unavailable without closing borrowed
handles. SAFETY retains ownership of timed-out worker termination. No live index,
inference, browser, final code-panel, version bump or publication is claimed.

## STATE Accepted

Candidate `645421f35f4f17e38a9090b0f2d6bc408568fb22` passed all seven stamped
commands, the locked environment refresh and 433 expanded phase tests. The
broad offline suite passed 3,025 tests, with 153 skips and 31 deselections.
The boundary group passed 45 tests. Local gate groups passed 51, 38, 117 and
265 tests; the independently installed wheel completed two real SDK sessions.
The standalone artifact validator returned `ok=true` with no findings.

The accepted receipt binds the exact source/tree, plan, wheel and verification
hashes. The prior rejected run remains intact. Only this bounded state contract
is accepted: SAFETY/DATA/PILOT/PREP/SHIP work and their gates remain outstanding.
Live GitHub check found no open Code-Index-MCP issues; findings remain roadmap
items. Draft Code-Index-MCP#97 is open and has not been merged.

## SAFETY Implementation

Artifact verification now checks trusted policy before extraction/publication,
preserves prepared bytes, and consumes supported GitHub OIDC bundles. The single
approved digest-only signing job succeeded in seven seconds on source `7251903`;
the image job was skipped. Local verification accepted the synthetic archive and
rejected seven negative controls. The first CLI verification exposed incompatible
flags; the repaired invocation binds the exact workflow through certificate
identity. No additional signing job was dispatched and no archive was uploaded.
Metadata-only evidence is in `docs/validation/v13/SAFETY-signing.json`.

Supported filesystem and SQLite capability forms are guarded, with explicit
cooperative rather than hostile-code containment limits. STDIO signals and EOF
share an awaited lifetime owner, sandbox transport deadlines reap workers, and
timed-out mutation threads retain ownership until drained or service fail-stop.
Metrics have loopback ownership and socket release; forwarded HTTP identity
requires trusted peers. Normal diagnostics omit query and exception content.

Development checks passed 185 artifact/capability cases and the expanded focused
lifecycle groups. An independently installed core wheel passed two SDK sessions
and six lifecycle cases, each with a real plugin child and no surviving child.
Normal exits took under one second; the admitted long reindex failed closed at
the 15-second service deadline. These diagnostic results do not close SAFETY:
the final candidate must pass all stamped commands, including updated content
sentinels, crypto re-verification and the non-root container/restart checks.

The first stamped SAFETY candidate `a23d372` is rejected, with all evidence
preserved under `.phase-loop/runs/v13-SAFETY-20260911-a23d372/`. Its 84 focused
tests, signing recheck, local gate, installed wheel, 140 Git manager tests and
214 phase tests passed. The broad suite had 2,928 passes, eight failures,
150 skips and 31 deselections. The container restart probe sent a JSON refresh
token to the existing query-parameter endpoint and received 422 instead of
exercising session rejection. No container acceptance was granted.

The broad failures exposed two malformed structured-state logging references,
stale argument-logging/singleton-reset expectations, a missing historical docs
link and a metrics test leaking its replacement exporter. Those are repaired;
177 focused tests pass. Additional privacy controls first reproduced nine
failures covering legacy symbol search, summaries, transport logs and background
exceptions. The repairs also remove HTTP access-query strings, preserve actual
socket peers in the container, and cover malformed SDK input. The expanded
installed probes and a new complete stamped run remain required.

### Accepted SAFETY Candidate

The complete replacement run on `b99e754` passed all ten commands, locked
environment refresh and 223 phase tests. Focused safety: 93 passed; Git
manager: 140 passed; broad offline: 2,944 passed, 150 skipped, 31 deselected.
Local gate, actual wheel, configured non-root container and signing recheck
passed. Both installed runtimes exercised six lifecycle cases with real plugin
children and no survivors; container HTTP restart/proxy/privacy controls passed.
The separate fixture-cleanup failure was repaired and the full command rerun.

The independent evidence validator returned `ok=true` with no findings.
`docs/validation/v13/SAFETY.json` binds exact source, plan, wheel, container,
signing and verification hashes and emits IF-0-SAFETY-1. The immutable roadmap
is unchanged. DATA is next; PILOT still owes the frozen performance target,
live inference, PMCP and browser proof. No version bump, final code panel,
merge or publication has occurred.

## DATA Candidate

Generation-scoped semantic leases, acknowledged paginated Qdrant maintenance,
per-batch/restart provenance, retained-row SQLite snapshots, committed-input
reconciliation and verified staged artifact restore are implemented. Export
streams mapped portable vectors; it does not copy live Qdrant files. Query
surfaces recheck live Git and current generation, including SHA-256 repositories,
sibling worktrees, no-match and mid-query replacement. Source filters match and
rank before limiting. Legacy cross-repository semantic mode refuses unsupported
requests; owned Python client resources close explicitly.

Caller integration now uses registered origins and generations, records actual
publication outcomes, preserves local-only indexing, and counts dispatcher-owned
rows without importing unfiltered working-tree files. Async reindex recovery
retains thread ownership without blocking signal handling. Actual `master`
branch identity is preserved. Tests now isolate both the registry and index root.
An earlier rejected gate exposed default host-registry metadata through a legacy
fixture; no fleet indexing was requested, and that run is not isolation evidence.

Development checks include 151 branch/HTTP/query controls, 67 storage/export
controls and 156 repaired Git/reindex fixture controls. The third broad diagnostic
run passed 3,129 tests with two incomplete SQL-count mocks, 150 skips and 31
deselections. Its corresponding local gate found a fixture claiming a different
branch without switching Git. All three fixture defects were corrected and the
156-test group passed. These overlapping counts are not summed or accepted as a
final gate. Earlier rejected diagnostic logs remain under the allowed DATA paths.

The actual installed wheel and non-root image passed Python query, STDIO/HTTP
restart and lifecycle probes. The in-flight fail-stop still takes about 15 seconds;
PILOT must meet the frozen five-second threshold. The synthetic offline estimate
is 79,360 input-token upper-bound units including reserved retries, with 20,640
remaining under the owner's cap; no inference requests have been made.

Next: seal a source candidate, rerun every original phase command through the
verification helper, and produce DATA acceptance only if all gates pass. PMCP,
browser, live inference, final four-seat review, version bump and SHIP remain
pending. No extra OIDC job was dispatched and the roadmap bytes are unchanged.

### DATA Acceptance Repair

Candidate `dd0c763` passed fourteen stamped commands, locked refresh and 358
phase tests. Broad offline: 3,135 passed, 150 skipped, 31 deselected; separate
Git manager: 140 passed. Wheel, non-root container and both Qdrant modes passed.
That run is preserved but rejected for acceptance: final source review found
C18 semantic source filters still returned lexical FTS results.

Four dispatcher regressions reproduced missing semantic matches and silent
lexical success when the semantic provider was unavailable or failed. The
repair selects all matching SQLite source identities and applies them inside
Qdrant before rank/limit, retaining split chunks and excluding deleted/sentinel
points. Five new vector controls pass locally; all fifteen maintenance/filter
controls pass on the pinned disposable server. Eighteen source-filter/history
controls pass with real SQLite and file-backed vectors. No live inference.

The expanded candidate `c0713a7` also fixed generation-aware semantic readiness,
signed SQLite/Qdrant point IDs and bound standalone manager stores. Its wheel,
container, 73 vector/provenance tests, 19 file/server controls per mode and 140
Git manager tests passed. Its broad run failed six tests (3,144 passed, 150
skipped, 31 deselected), and the phase suite failed three (374 passed).
That receipt is rejected, not accepted by its other successful commands.

The shared synthetic fixture committed live .mcp-index WAL/SHM files via its
blanket Git add. Closing pooled handles correctly changed those tracked files
and tripped the final dirty-source fence. The fixture now locally excludes its
runtime directory; a new test checks it never stages generation files. All 37
selected reconciliation/watcher tests pass, with two benchmark cases deselected.
No production fence was relaxed. Another complete exact-candidate run is required.

### DATA Accepted

Source `668bf754d1728d07e113c8560acb6e5cf79a9b3f` passed fourteen commands,
locked environment refresh and 378 phase tests. Broad offline: 3,151 passed,
150 skipped, 31 deselected. Separate Git manager: 140 passed. Real Qdrant
file/server nodes each passed 19 controls; wheel and non-root image passed
actual query/restart/privacy and six lifecycle cases, with no surviving children.
The independent validator returned `ok=true`, no findings or diagnostics.

The DATA receipt binds exact source/tree, plan, contract, wheel, image and
verification hashes, records all four ECs and produces IF-0-DATA-1. Prior
rejected attempts remain preserved. PILOT now owns installed PMCP, interactive
browser and budgeted local inference acceptance, including the still-unmet
five-second in-flight shutdown threshold. No version bump, final code panel,
merge, publication or extra signing job has occurred.
All original phase gates must pass again on the repaired committed candidate.

Expanded production semantic probes also reproduced readiness checking the
legacy metadata/collection instead of the generation owner, and unsigned
hash-derived point IDs overflowing SQLite. Readiness now consumes the owner's
pure generation locator; new vector IDs fit positive signed 63-bit links while
source chunk IDs remain unchanged. Boundary roundtrips pass in both SQLite and
real file-backed Qdrant. Public semantic rebuild/restore/restart verification is
passing through the real Python client and STDIO/HTTP handlers. Missing or
incomplete generation metadata refuses readiness despite legacy root metadata.
The standalone Git manager now reuses generation-bound pooled stores instead
of comparing the Python identities of separate registry snapshots. This exposed
one test hashing the pre-checkpoint main database without its committed WAL;
its setup now checkpoints before the byte-preservation comparison.
All 140 Git manager tests pass. The disposable server now passes 19 explicit
maintenance, filtered-ranking and signed-ID controls. These are diagnostic
results, not phase acceptance; the repaired source still needs stamped gates.
