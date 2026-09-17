# V13 Release Preparation And Acceptance

## Candidate Status

`1.4.1` is the prepared candidate for Code-Index-MCP#97. The latest release
verified on September 12, 2026 was `v1.4.0`, published July 19. This document and
the version bump do not claim that `1.4.1` has been published or deployed.
The canonical source plan is `specs/phase-plans-v13.md`; execution progress is
`docs/status/V13_EXECUTION.md`.

Distribution: `index-it-mcp`; commands: `mcp-index` and `index-it-mcp`.
Container: the signed `ghcr.io/consiliency/code-index-mcp@sha256:...` reference
published in the `v1.4.1` GitHub release's `image-reference.txt` asset. Installers
pin that exact reference; version/latest GHCR tags are left untouched. Until then, use
`uv sync --locked` or the locally built `index_it_mcp-1.4.1-py3-none-any.whl`.
The installer's `latest` selector means the latest GitHub release, not a mutable
GHCR tag. It refuses a missing or malformed reference without falling back.

## Evidence Boundary

PILOT is accepted with an independently installed pre-bump candidate. Its source,
wheel, live provider records and immutable allowance remain bound to that run.
`scripts/v13_release_candidate.py` compares package content and structured
dependency metadata before consuming the original-source evidence. It refuses
runtime changes and validates archived live records read-only. This is not a
new live run of the version-bumped wheel.

The first candidate (6310af8) was rejected during review. Subsequent runtime
repairs are NOT version-only, so that comparator must refuse inheritance of
the old pilot. A fresh candidate-bound pilot and metadata-signature validation
are required before acceptance. The owner approved one replacement synthetic-local
pilot and one digest-only signing validation on September 15, 2026. These remain
unused until recorded first admission and accepted dispatch, respectively. The
old ledger and consumed signing job are not reusable authorizations.

The renewed pilot uses only approval
`v13-prep-178b8328-20260915-synthetic-local` and the canonical
`.phase-loop/runs/v13-PILOT-allowance-20260915/` ledger: 100000 input units
including retries, 900 seconds from first admission, concurrency one. The
distinct `--renewed-pilot-root` validator requires matching clean source, wheel,
lock, offline/browser/rehearsal/live proof and the canonical ledger. The legacy
version-only comparator must still reject these runtime repairs. Signing is
limited to one five-minute `index-attestation` job receiving only the canonical
metadata digest. Complete all code fixes and prerequisite checks before either
effect; neither permits an automatic retry after failure or uncertain acceptance.
`scripts/v13_release_candidate.py --claim-signing-dispatch` performs local
canonical metadata/schema/identity/size/checksum validation, then durably claims
the one attempt. It does not dispatch. Changed or invalid local inputs must
leave no claim, and an existing claim is never replaced or reset.
`--inspect-signing-claim` checks a persisted complete claim read-only against the
current candidate and inputs after an ambiguous local write/directory-sync
failure. It reports no dispatch authority and does not query GitHub. Reconcile
actual remote dispatch state and the owner's one-shot allowance separately;
matching local bytes are never permission for an automatic second attempt.

Live and loopback-provider receipts require a hash-bound resource measurement
artifact with each owned process and exactly one Qdrant container, including
identity, cgroup, shutdown duration, peak RSS, exit state and survivor census.
Aggregates must match those records and Qdrant's native start/stop output. A
missing container record cannot pass by retaining only aggregate goal flags.

Fresh candidate-specific local full, installed PMCP, container, Qdrant,
loopback-provider and browser checks are required in PREP. Real inference is not
a repeatable release check: the original 900-second cumulative allowance has
expired. Runtime or dependency repairs invalidate relevant live proof and need
a fresh bounded owner decision; no ledger reset or automatic re-admission.

The four exact review seats are Fable `claude-fable-5`, Sol `gpt-5.6-sol`,
Grok `grok-4.5`, and Gemini `Gemini 3.1 Pro`. Each receives full candidate changes,
then all peer reports for reconciliation. No unavailable or degraded seat counts
as approval; configured and reported model identities remain distinguishable.
Fable uses only first-party subscription TUI/self-PTY execution.
On September 17 the owner authorized the manual pointer-based four-seat route
for this release, conditional on verified scoped read-only access. Reviewers
receive short briefs and file pointers, not injected source bundles. This does
not waive any seat, exact-candidate coverage, or cross-model reconciliation.

Final review receipts live outside the frozen source tree and are referenced
from qualified PR comments and the SHIP handoff. The tracked PREP status is not
an approval substitute. Any candidate edit after review requires renewed review.

## Publication Gate

1. Accept every PREP goal, resolve blocking findings and bind all four reconciled
   reviews and local checks to the same candidate content.
2. Verify Code-Index-MCP#97 head and current main; merge only that accepted head.
3. Create a clean publication worktree under `/mnt/workspace/worktrees` when
   available. Fetch protected main, confirm merge/source content and version
   inputs, and record independent publication authorization outside that tree.
4. Check PyPI, GHCR, GitHub release and tag state for collisions or partial effects.
   Dispatch `Release Automation` once with `mode=publish`, `version=v1.4.1`,
   `auto_merge=false`, `expected_commit=<recorded-main-sha>`,
   `expected_tree=<accepted-tree-sha>`, and `--ref main` only after the SHIP gate.
5. Record workflow/run, tag/source, wheel/sdist/image digests and attestations.
   Independently install registry-delivered artifacts outside the checkout and
   repeat critical PMCP, query, migration, non-root and lifecycle acceptance.
6. Record published, accepted, deployed and cohort-ready separately; close only
   qualified issues whose acceptance is actually proved.

Routine verification is local. No hosted prepare dispatch is required. Existing
release workflow action pins and protected-main guards remain enforced.
The first job refuses commit/tree drift and reruns. After local gates the
workflow creates `refs/tags/release-claims/<version>` once, recording run/source/tree.
An existing claim prevents all downstream publication, including after a partial
failure; never delete/update it to retry. The publish workflow then uploads a unique candidate image reference,
signs and verifies its digest, and publishes the Python/GitHub artifacts. Only
then does the final read-only job verify that exact digest again. GHCR tag
promotion has no create-only/CAS guarantee, so this workflow never writes
version/latest tags. Concurrent package writers cannot make this release
overwrite their tags. The GitHub release includes `image-reference.txt` and
`image-digest.txt`; digest signature verification also requires the accepted
workflow commit. A failed
intermediate step may leave a candidate image or partial release; it never
authorizes automatic redispatch or substitution of an unsigned image.
GitHub release creation and asset upload are separate create-only operations:
no update/clobber flags and no automatic draft deletion on upload failure.
An incomplete published release is preserved for owner-directed recovery.

Delivered acceptance uses `release_smoke.py --wheel-path <downloaded-wheel>
--wheel-sha256 <registry-sha256> --image-ref <ghcr-name>@sha256:<registry-digest>`.
For PMCP, run `v13_pmcp_pilot.py --mode prepare --root <owned-root> --wheel
<downloaded-wheel> --wheel-sha256 <registry-sha256>`, then offline, rehearsal and
browser modes against that manifest. These supplied-artifact paths never build
a replacement wheel or image. Record the registry digest independently before
passing it to the runner; a locally calculated checksum alone is not registry proof.

## Recovery And Rollout

On interruption, inspect exact workflow and registry metadata before any effect.
Do not blindly redispatch, retag, overwrite, yank or delete published artifacts.
If any artifact is published but acceptance fails, record `published=true` and
`accepted=false`, stop issue closure and cohort promotion, and preserve evidence.
A corrective release or rollback requires a new bounded owner-approved action.
Retaining the previous deployed version is distinct from deleting a release.

Controlled rollout only. Multi-repo and STDIO remain beta; lexical readiness is
not semantic readiness. The accepted pilot covers synthetic local inputs on
Linux/Python 3.12.12, PMCP 2.7.3 and Inspector 2.6.0. It does not establish other
platforms, broad retrieval quality, immutable provider revisions or default
reranking safety. Fleet indexing needs a separate budget and cohort decision.

Index authority covers committed content on the registered tracked branch only.
Tracked local edits refuse indexed queries until committed and reconciled; use
native search during that interval. Failed staged builds also remain fenced until
a successful rebuild. Unregistered watcher contexts refuse mutation explicitly.
Older registries that recorded `main` for an actual `master` repository need
re-registration with the correct tracked branch.

Old and failed generations are retained deliberately. Rebuilds snapshot the whole
committed tree and may copy retained vectors; capacity and latency must be measured
before scaling. The first generation rebuild after upgrading legacy vector storage
may re-embed the corpus and needs a separate inference budget. No automatic garbage
collection or fleet indexing is authorized by this release.

The HTTP admin gateway requires external process supervision. Python cannot safely
abandon a stuck mutating worker while it owns the repository fence. Failed or
timed-out retirement keeps dependencies alive while draining; a five-second
process watchdog exits unsuccessfully if drainage cannot finish. A supervisor
must restart that process; readiness still refuses incomplete generations.
STDIO also uses bounded shutdown. Neither surface promises in-process recovery
from arbitrary stuck native plugin code.
