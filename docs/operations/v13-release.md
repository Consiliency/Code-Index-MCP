# V13 Release Preparation And Acceptance

## Candidate Status

`1.4.1` is the prepared candidate for Code-Index-MCP#97. The latest release
verified on September 12, 2026 was `v1.4.0`, published July 19. This document and
the version bump do not claim that `1.4.1` has been published or deployed.
The canonical source plan is `specs/phase-plans-v13.md`; execution progress is
`docs/status/V13_EXECUTION.md`.

Distribution: `index-it-mcp`; commands: `mcp-index` and `index-it-mcp`.
Container: `ghcr.io/consiliency/code-index-mcp:v1.4.1`. The installer and Docker
examples target that exact tag and require its publication. Until then, use
`uv sync --locked` or the locally built `index_it_mcp-1.4.1-py3-none-any.whl`.
Never assume `latest` identifies this candidate before delivered acceptance.

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
are required before acceptance. The requested replacement allowances are pending;
the old ledger and consumed signing job are not reusable authorizations.

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
   `auto_merge=false`, and `--ref main` only after the SHIP pre-publication gate.
5. Record workflow/run, tag/source, wheel/sdist/image digests and attestations.
   Independently install registry-delivered artifacts outside the checkout and
   repeat critical PMCP, query, migration, non-root and lifecycle acceptance.
6. Record published, accepted, deployed and cohort-ready separately; close only
   qualified issues whose acceptance is actually proved.

Routine verification is local. No hosted prepare dispatch is required. Existing
release workflow action pins and protected-main guards remain unchanged.

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
