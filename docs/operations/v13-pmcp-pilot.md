# V13 PMCP Pilot Operations

## Status And Scope

PILOT accepted on 2026-09-12 for source 366f6bca7765d545498f41b02014ae8ba105d2e2.
This does not authorize fleet indexing or claim publication. Consult
`docs/validation/v13/PILOT.json` and `docs/status/V13_EXECUTION.md` for the
accepted artifact identities and remaining PREP/SHIP gates.

PMCP is the provisioning entry point. The tested connection is PMCP Streamable
HTTP to its downstream installed index-it-mcp STDIO child. FastAPI is a separate,
authenticated admin surface, not this package's MCP HTTP transport. MCP Inspector
is the tested browser client; PMCP does not supply the dashboard tested here.

## Exact Installation And Configuration

The pilot prepares a wheel from a clean source candidate and installs it through
an isolated uvx cache outside the checkout. Use its manifest's uvx_prefix, wheel
SHA-256 and constraints file for candidate validation. The candidate still has
historical package metadata 1.4.0; the published 1.4.0 package is NOT the v13
candidate. PREP must select a new version and SHIP must verify registry delivery
before that published version replaces the candidate-wheel reference.

Pin the Python interpreter and distribution, then launch index-it-mcp stdio.
The production extra supplies actual metrics/auth dependencies exercised by the
pilot; a core-wheel pass is not a metrics-exporter pass. Keep optional learned
reranking disabled unless a separate retrieval gate accepts it.

Configure the command, arguments and environment through the project's .mcp.json
mcpServers entry. A package manifest alone does not pin the candidate or Python,
and an explicit PMCP project config does not suppress ambient user overlays.
The pilot uses isolated HOME, project, config, policy and lock directories.
Inspect effective provisioning before introducing an existing user configuration.

CLI registration and the downstream child must share these values:

| Setting | Requirement |
| --- | --- |
| MCP_REPO_REGISTRY | Same explicit registry path |
| MCP_INDEX_STORAGE_PATH | Same owned index root |
| MCP_ALLOWED_ROOTS | Only approved repository roots |
| MCP_DEPLOYMENT_PROFILE | lexical_only initially; fleet_local only for approved inference |
| SEMANTIC_SEARCH_ENABLED | false for lexical operation |
| SEMANTIC_DEFAULT_PROFILE | Explicit matching profile; legacy-default for the lexical fixture |
| MCP_AUTO_INDEX | false during controlled admission |
| MCP_METRICS_PORT | Unset to disable; 0 for an ephemeral loopback port or an explicit owned local port |
| MCP_CLIENT_SECRET | Optional shared secret injected privately; requires handshake before queries |

Never commit credentials or reuse synthetic fixture credentials. Admin JWT auth
is independent of the STDIO handshake. Retain the loopback binding and explicit
proxy trust boundary; this pilot does not approve shared multi-tenant HTTP.

Register a selected worktree with repository register --no-auto-sync --no-artifacts
using the same installed command prefix and environment as the child. Explicit
registration does not make an index ready. Provision via gateway.provision, discover
the downstream tool IDs, complete handshake when configured, then reindex the
approved synthetic repository and inspect readiness before querying.

One git common directory has one registered worktree. Sibling worktrees, wrong
branches and stale commits must refuse indexed queries with native_search guidance.
A ready no-match result is different from an unavailable index.

## Local Inference Admission

The local roles are embedding at http://ai:8001/v1 and enrichment at
http://ai:8002/v1. Verify the actual model catalog, vector dimension and recorded
provenance; do not infer a served revision from the model name. An unreported
immutable revision remains explicitly unreported.

JSON profiles need complete embedding and enrichment resources, including
endpoint, model and private key-environment references when authentication is used.
Local-only policy must not fall through to commercial providers, BAML defaults or
unbounded client sampling. Missing explicitly selected resources fail closed.

The pilot's request guard is an operational budgeting tool, not OS-level egress
confinement. It permits only the frozen local roles, serializes inference and
reserves conservative serialized input units before forwarding, including retries.
Failed requests consume reservations; redirects and ambient proxies are refused.

The approved cumulative allowance is 100000 input units, 900 seconds from first
admission and concurrency one. The canonical ledger is
.phase-loop/runs/v13-PILOT-allowance. Never initialize a second ledger, reset it,
refund failed attempts or rerun live inference as an ordinary test.

## Evidence And Repeatable Checks

prepare, offline, rehearsal and browser modes use fresh owned fixture directories.
rehearsal substitutes loopback synthetic providers and cannot prove live quality.
Live admission follows green installed, browser and local regression gates.

The frozen workload has two unrelated repositories and a sibling worktree.
It tests retrieval, committed changes, rename/delete/rebuild/restart, provenance,
and 20 warm queries per class per repository, with at least 20 successful samples
per class fully overlapping another repository's indexing interval. Refusals are
not fast successful samples.

Limits remain symbol p95 <=100ms, search p95 <=500ms, shutdown <=5s and observed
process-tree RSS <=2048MiB. Do not tune these limits after measurement.

verify-browser and verify-live consume saved evidence without inference.
Live verification reopens the archived ledger read-only, reconciles request
envelopes and accounting, reconstructs raw sample timing/contention, and checks
workload/model/collection bindings. SQLite mapping count and unique vector point
count are separate: multiple mappings may legitimately reference one point.

Source changes invalidate affected candidate evidence. Preserve rejected and
interrupted runs; do not turn partial logs or hash-valid placeholders into
acceptance. An expired or exhausted allowance requires an explicit owner decision.

## Observed Results

On Linux/Python 3.12.12 with PMCP 2.7.3, the installed candidate passed all 13
offline goals and eight browser goals using MCP Inspector 2.6.0 and Swagger.
The live run used 24267 conservative input units across 82 admitted requests
in 44.579 seconds. It passed 40 measured queries per class, with 26 symbol,
22 lexical and 26 semantic successes overlapping another repository's indexing.
p95 was 74.903ms symbol, 94.606ms lexical and 470.511ms semantic; peak process-tree
RSS was 430.75MiB and the slowest live shutdown was 2.171 seconds. No survivors.

The embedding endpoint reported Qwen/Qwen3-Embedding-8B, dimension 4096.
The enrichment endpoint reported cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit. Immutable
revision was unreported. Both final collections carried verified corpus,
commit, profile and point-set bindings, with three SQLite mappings referencing
two unique vector points per repository. This is bounded workflow evidence,
not broad retrieval-quality or immutable provider-build certification.

Full offline baseline: 3308 passed, 150 skipped, 31 deselected; Git manager
140 passed separately; Qdrant 19 passed in each backend; final phase suite
558 passed. An earlier full-gate process received SIGTERM without a final
artifact. Its owned container was stopped and its partial log preserved;
a fresh observed run passed every node. No failed attempt is accepted proof.

## Rollout And Recovery

Even accepted PILOT evidence permits only a controlled rollout decision. It does
not index the fleet, expand platform/language support, approve learned reranking,
or establish broad retrieval quality from a two-file synthetic corpus.

On shutdown during an admitted mutation, retain the pending publication fence;
a nonzero process exit is expected if work did not finish. Do not remove active
SQLite sidecars, break Qdrant locks or claim readiness for an old generation.
Stop only resources owned by the run and preserve evidence.

Publication requires the versioned PREP candidate, four requested review seats,
cross-model reconciliation, exact merge/release identity checks and controlled
SHIP dispatch. Published-but-unaccepted artifacts block cohort promotion; preserve
them and seek a corrective-release decision, never silently retry, yank or delete.
