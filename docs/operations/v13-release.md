# V13 Release Preparation And Acceptance

## Release Status

`1.5.1` is published. It is the corrective release for `1.5.0` and caps `mcp`
below 2. It is recorded as published, with delivered acceptance passed for the
wheel and image, PMCP offline, rehearsal and browser not run, and not deployed;
see "1.5.1 Delivered Acceptance" below. Install it with
`pip install index-it-mcp==1.5.1`.

`1.5.0` is published. It carries the treesitter-chunker 6 / tree-sitter 0.26
parser-stack upgrade (Code-Index-MCP#115, merged in #116) and the baml-bridge
0.23.0 alignment (Code-Index-MCP#119, merged in #120). It is recorded as
published, not accepted and not deployed; see "1.5.0 Delivered Acceptance"
below. Published `1.5.0` and `1.4.1` both set no upper bound on `mcp`: an
install that does not also constrain `mcp` below 2 (even `index-it-mcp==1.5.0`)
resolves `mcp` 2.x and `index-it-mcp stdio` fails at import
(`McpError` was removed from `mcp.shared.exceptions`). Release smoke and the
PMCP pilot installed under the lock (`mcp` 1.27.0), so neither saw it. `1.5.1`
caps `mcp>=1.27,<2`, and `release_smoke.py --unconstrained` (part of
`make release-smoke`) now installs the wheel with no lock constraints on Python
3.12 and starts STDIO. Porting to `mcp` 2.x is Code-Index-MCP#124. Yanking
`1.5.0` and `1.4.1` is a separate owner action after `1.5.1` is published and
accepted.

The previous fully accepted release, `v1.4.1` (Code-Index-MCP#97), was published
October 13, 2026 and delivered-accepted as recorded in
`docs/status/V13_EXECUTION.md`. The evidence and review narrative below records
how `1.4.1` was accepted. The Publication Gate was written for `1.4.1`; for
`1.5.0` the owner chose green CI, the existing reviews on #116 and #120, and
post-publish acceptance of the published wheel and image, and `1.5.1` used the
same delivered acceptance. Its dispatch, recovery and delivered-acceptance rules
are unchanged.
The canonical source plan is `specs/phase-plans-v13.md`; execution progress is
`docs/status/V13_EXECUTION.md`.

Distribution: `index-it-mcp`; commands: `mcp-index` and `index-it-mcp`.
Container: the signed `ghcr.io/consiliency/code-index-mcp@sha256:...` reference
published in the `v1.5.1` GitHub release's `image-reference.txt` asset. Installers
pin that exact reference; version/latest GHCR tags are left untouched. PyPI:
`pip install index-it-mcp==1.5.1`; `uv sync --locked` and the locally built
`index_it_mcp-1.5.1-py3-none-any.whl` remain development paths. Do not install
published `1.5.0` or `1.4.1` from PyPI without constraining `mcp` below 2 (see
below).
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
pilot and one digest-only signing validation on September 15, 2026. The pilot
allowance was consumed by its first admission; signing remains unused. The
old ledger and consumed signing job are not reusable authorizations.

The September 15 pilot admitted one 33-unit request before an unknown provider
transport outcome. Its ledger remains blocked and read-only. The owner approved
one replacement pilot on October 4. It uses only approval
`v13-prep-178b8328-20261004-synthetic-local` and the canonical
`.phase-loop/runs/v13-PILOT-allowance-20261004/` ledger: 100000 input units
including retries, 900 seconds from first admission, concurrency one. That
pilot failed the semantic search p95 limit and its allowance is consumed. The
owner later amended concurrency to one in-flight request per provider role and
conditionally approved one more pilot under
`v13-prep-178b8328-20261012-synthetic-local` and
`.phase-loop/runs/v13-PILOT-allowance-20261012/`. It runs only after every gate
and four new exact-candidate approvals pass. The
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

The owner's September 29 seat replacement sets the four exact review seats:
`gpt-6-sol` and `gpt-5.6-sol` through Codex subscriptions, `claude-opus-5-5`
through the tool-enabled Claude TUI adapter, and Gemini `Gemini 3.1 Pro (High)`.
Gemini 3.1 Pro (High) is no longer offered. On October 12 the owner set
`Gemini 3.8 Flash (High)` as the Gemini seat for future reviews.
Grok and Fable are not used. Each receives full candidate changes, then all peer
reports for reconciliation. No unavailable or degraded seat counts as approval;
configured and reported model identities remain distinguishable.
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
2. Verify the release PR head and current main; merge only that accepted head.
3. Create a clean publication worktree under `/mnt/workspace/worktrees` when
   available. Fetch protected main, confirm merge/source content and version
   inputs, and record independent publication authorization outside that tree.
4. Check PyPI, GHCR, GitHub release and tag state for collisions or partial effects.
   Dispatch `Release Automation` once with `mode=publish`, `version=v1.5.1`,
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
--wheel-sha256 <registry-sha256> --unconstrained --image-ref
<ghcr-name>@sha256:<registry-digest>`. `--unconstrained` also installs the
delivered wheel alone, with no lock constraints, as `pip install index-it-mcp`
would, and starts STDIO; lock-constrained checks cannot see an unbounded
dependency that breaks the published install. It resolves against public PyPI
with only a short allowlist of environment variables, so operator mirror,
offline or date-cutoff settings cannot make it pass on older versions. If it
fails in the release gates, the fix is a dependency bound in a new reviewed
change, never a rerun of the same dispatch.
For PMCP, run `v13_pmcp_pilot.py --mode prepare --root <owned-root> --wheel
<downloaded-wheel> --wheel-sha256 <registry-sha256>`, then offline, rehearsal and
browser modes against that manifest. These supplied-artifact paths never build
a replacement wheel or image. Record the registry digest independently before
passing it to the runner; a locally calculated checksum alone is not registry proof.
For an older image signed after publication by `sign-published-image.yml`, run
the delivered image check from its accepted source checkout and pass
`--image-signer-sha <protected-main-signing-workflow-commit>` with `--image-ref`.
The verifier checks the signer commit follows the image source, belongs to
protected-main history, and matches the image signature's workflow identity.

## 1.5.1 Delivered Acceptance

Recorded 2026-11-09 UTC on dev0 (Linux, Python 3.12.14) from a clean worktree at
`13470d0`. Status: published=true; accepted for the delivered wheel and image,
with PMCP offline, rehearsal and browser not run (partial); deployed=false. No
issue closure, cohort promotion or yank is claimed.

Published (Release Automation run 42550570039, `workflow_dispatch` on `main`;
validate, gates, claim, build, publish and verify-container jobs succeeded):

| Artifact | Identity |
| --- | --- |
| Tag `v1.5.1` | commit `13470d0988efc5c1707b9eff1974bc0266bb032e`, tree `ff369080bbad36edd1be581e50d9d2453a9864f5`; claim `refs/tags/release-claims/v1.5.1` exists |
| GitHub release | `v1.5.1`, not draft or prerelease, published 2026-11-09T02:36:16Z |
| PyPI wheel | `index_it_mcp-1.5.1-py3-none-any.whl`, sha256 `f344263181664bc1dc0a8909ea3ecc61dad7ef9b3dd5462d50b504c82bbc4f9e`; `Requires-Dist: mcp<2,>=1.27` |
| PyPI sdist | `index_it_mcp-1.5.1.tar.gz`, sha256 `728a93c510b70a3f0e9bd44f026265def987ccfffbac5796a90750fc9deb6f65` |
| Image | `ghcr.io/consiliency/code-index-mcp@sha256:68da715873c9c4d7427048cc496522bbd4896c92b627290a65e3f33522ea0339` |

Passed:

- Downloads: both PyPI files match the digests above and are byte-identical to
  the GitHub release assets. `image-reference.txt` and `image-digest.txt` name
  the image digest above.
- Image: pulled anonymously by digest. Labels are `v1.5.1` and revision
  `13470d0`; the image user is `mcp`. cosign v2.5.2 (the workflow's version)
  verified the keyless signature: identity
  `.../release-automation.yml@refs/heads/main`, issuer
  `https://token.actions.githubusercontent.com`, workflow SHA `13470d0`.
- Release smoke: `release_smoke.py --wheel-path <PyPI wheel> --wheel-sha256
  f344263... --unconstrained --image-ref <digest> --stdio --docker-network host`,
  with `TREE_SITTER_LANGUAGE_PACK_CACHE_DIR` set to a fresh, prefetched private
  cache. dev0 still cannot reach bridge-published container ports (a plain
  `python -m http.server` in the image answers inside the container but not from
  the host), so the container probe used host networking.
  - Locked wheel: source contract, schema, Python client, two STDIO sessions
    and lifecycle passed.
  - Unconstrained wheel: resolved `mcp` 1.30.0, then the raw initialize probe
    (8 tools) and two STDIO sessions passed.
  - Container: cosign, schema/prepare/Python as uid 999, HTTP queries before and
    after restart, log privacy, lifecycle and the safety probe passed.
- The 1.5.0 defect is fixed. In a fresh Python 3.12 venv, a plain
  `pip install index-it-mcp==1.5.1` with no constraints resolved `mcp` 1.30.0,
  and `pip check` found no broken requirements. A raw JSON-RPC probe of
  `index-it-mcp stdio` got `initialize` (protocol `2025-06-18`) and `tools/list`
  (8 tools), and the server exited 0.
- Co-install with `phase-loop-runtime==0.7.30` from PyPI: `uv pip check` is
  clean, and installed files match the wheel's `RECORD`. Resolved baml-bridge
  0.23.0, tree-sitter 0.26.0, treesitter-chunker 6.0.0,
  tree-sitter-language-pack 1.20.0 and mcp 1.30.0. `mcp-index --version`
  reports 1.5.1. `grammars prefetch` verified 47/47 grammars, and
  `grammars check` loaded 47 offline, 0 missing. A BAML import and offline
  request build left no `~/.baml`, and nothing else in the private HOME, with
  `MCP_BAML_TELEMETRY` unset or blank.
- PMCP prepare: `v13_pmcp_pilot.py --mode prepare --wheel <PyPI wheel>
  --wheel-sha256 f344263...` wrote a registry-origin manifest bound to
  `13470d0`/`ff36908`, installed version 1.5.1 (429 wheel files, 135 BAML files
  verified) and `pmcp 2.7.3` (the private PyPI `pmcp==2.7.3` tool used for
  1.5.0).

Not run:

- PMCP offline: the harness gap from 1.5.0 is still on `main`. The fixture in
  `scripts/v13_pmcp_pilot.py` sets a fresh `XDG_CACHE_HOME` and passes no
  prefetched grammar cache, so its network-denied sandbox workers cannot load
  grammars. The script is unchanged since #109.
- PMCP rehearsal and browser: skipped for the 1.5.0 reasons. They use the same
  fixture. dev0 cannot use bridge port publishing for the disposable Qdrant, and
  in host mode its gRPC port 6334 is held by dev0's live Qdrant. Browser mode
  also needs MCP Inspector 2.6.0 and an operator session.

Observation: the STDIO `initialize` result reports `serverInfo` as
`code-index-mcp-fast-search` with version `1.30.0`, the `mcp` SDK version, not
the package version.

Deployed: no. The dev0 code-index child still runs build `b79cc7c` and was not
changed.

## 1.5.0 Delivered Acceptance

Recorded 2026-11-08 on dev0 (Linux, Python 3.12.14) from a clean worktree at
`81980d0`. Status: published=true, accepted=false (partial), deployed=false. No
issue closure or cohort promotion is claimed.

Published (Release Automation run 42539192667, `workflow_dispatch` on `main`;
validate, gates, claim, build, publish and verify-container jobs succeeded):

| Artifact | Identity |
| --- | --- |
| Tag `v1.5.0` | commit `81980d01cb8df7909828d25487ff29f32410875f`, tree `5d43fdfb07a0e5d5393830242081323dd0c4ee70`; claim `refs/tags/release-claims/v1.5.0` exists |
| GitHub release | `v1.5.0`, not draft or prerelease, published 2026-11-09T00:29:00Z |
| PyPI wheel | `index_it_mcp-1.5.0-py3-none-any.whl`, sha256 `931f8ed7821935bbb9f2862ae1f6eecbd81578de637c3cc44b4bb6913af206e8` |
| PyPI sdist | `index_it_mcp-1.5.0.tar.gz`, sha256 `3f0bd752486b0c374e37bb96543677aee408fb99b6c4bb813a284ad37fd9e81e` |
| Image | `ghcr.io/consiliency/code-index-mcp@sha256:6ef6aa5432ec070eb865d75388cf0859ed10c77404b3767860d74838f2b4b156` |

Passed:

- Downloads: both files from PyPI (`/pypi/index-it-mcp/1.5.0/json` digests)
  match the values above and the GitHub release assets byte for byte.
  `image-reference.txt` and `image-digest.txt` name the image digest above.
- Image: pulled anonymously by digest. Labels are `v1.5.0` and revision
  `81980d0`; the image user is `mcp`. cosign v2.5.2 (the workflow's version,
  release checksum verified) verified the keyless signature: identity
  `.../release-automation.yml@refs/heads/main`, issuer
  `https://token.actions.githubusercontent.com`, workflow SHA `81980d0`.
- Release smoke, wheel and STDIO: `release_smoke.py --wheel-path <PyPI wheel>
  --wheel-sha256 931f8ed7... --stdio --image-ref <digest>` with
  `TREE_SITTER_LANGUAGE_PACK_CACHE_DIR` set to a fresh, prefetched private cache.
  Source contract, both entrypoints, schema, prepare, Python client, two STDIO
  sessions and installed lifecycle passed. The smoke installs with the
  repository lock's constraints.
- Release smoke, container: the bridge-network probe could not reach the
  container's published port, although the server logged healthy requests
  inside the container. A plain `python -m http.server` in the same image showed
  the same host-side connection reset, so this is dev0's Docker networking. One
  deliberate rerun with `--docker-network host` passed: cosign, offline BAML
  with blank telemetry, schema/prepare/Python as uid 999, HTTP queries before
  and after restart, log privacy, lifecycle and the safety probe.
- Co-install: a fresh Python 3.12 venv with `index-it-mcp==1.5.0` and
  `phase-loop-runtime==0.7.30` from PyPI. `uv pip check` is clean, and installed
  files match the wheel's `RECORD`. Resolved baml-bridge 0.23.0, tree-sitter
  0.26.0, treesitter-chunker 6.0.0, tree-sitter-language-pack 1.20.0 and
  mcp 2.3.0. `mcp-index --version` reports 1.5.0. `grammars prefetch` verified
  47/47 grammars into a private cache, and `grammars check` loaded 47 offline,
  0 missing. A BAML import and offline request build left no `~/.baml` with
  `MCP_BAML_TELEMETRY` unset or blank.
- PMCP prepare: `v13_pmcp_pilot.py --mode prepare --wheel <PyPI wheel>
  --wheel-sha256 931f8ed7...` wrote a registry-origin manifest bound to
  `81980d0`/`5d43fdf`, installed version 1.5.0 (429 wheel files, 135 BAML files
  verified) and `pmcp 2.7.3`. dev0's PMCP 2.7.3 tooling releases are archived
  symlinks that the launcher binder refuses, so PyPI `pmcp==2.7.3` was
  installed as a private uv tool. The live dev0 gateway was not used.

Failed or not run:

- Unconstrained PyPI install fails. The wheel declares `mcp>=1.0.0` with no
  upper bound, so `pip install index-it-mcp==1.5.0` (alone or with
  phase-loop-runtime) resolves `mcp` 2.3.0. `index-it-mcp stdio` then fails at
  import: `cannot import name 'McpError' from 'mcp.shared.exceptions'`. The
  `1.4.1` metadata has the same unbounded requirement. Adding `"mcp<2"`
  resolves `mcp` 1.30.0; `uv pip check` is then clean and the installed STDIO
  probe passes. Release smoke and the PMCP pilot did not catch this because both
  install with lock constraints (`mcp` 1.27.0). A corrective release needs a new
  owner-approved action; published artifacts are unchanged.
- PMCP offline failed. `reindex` returned `SandboxCallError`. The pilot fixture
  sets a fresh `XDG_CACHE_HOME` and passes no prefetched grammar cache, and
  network-denied sandbox workers cannot download grammars. This is the #122
  gap, still present in `v13_pmcp_pilot.py`. The same installed probe fails
  with a cold cache and passes with a prefetched cache.
- PMCP rehearsal and browser not run. Both use the same fixture. Rehearsal's
  disposable Qdrant cannot use bridge port publishing on dev0. In host mode
  its default gRPC port 6334 is held by dev0's live Qdrant. Browser mode also
  needs MCP Inspector 2.6.0 and an interactive operator session.

Deployed: no. The dev0 code-index child still runs build `b79cc7c` (main after #116,
before the baml-bridge 0.23.0 change), not the published 1.5.0 wheel, and was not changed.

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
collection or fleet indexing is authorized by this release. Later builds that include
Consiliency/Code-Index-MCP#114 bound retention instead: each successful publication
keeps the live generation plus `MCP_INDEX_GENERATION_RETENTION` (default 2) rollback
generations and prunes older ones.

The HTTP admin gateway requires external process supervision. Python cannot safely
abandon a stuck mutating worker while it owns the repository fence. Failed or
timed-out retirement keeps dependencies alive while draining; a five-second
process watchdog exits unsuccessfully if drainage cannot finish. A supervisor
must restart that process; readiness still refuses incomplete generations.
STDIO also uses bounded shutdown. Neither surface promises in-process recovery
from arbitrary stuck native plugin code.
