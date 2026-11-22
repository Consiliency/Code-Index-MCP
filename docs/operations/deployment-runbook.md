# Deployment Runbook (v1.4.0 hardening preparation baseline)

## Overview

This runbook is the operator playbook for the Code-Index-MCP
`v1.4.0` hardening preparation baseline. It is not a publication record.
The supported deployment surfaces remain local-first and operator-owned:

- `uv sync --locked` plus the local checkout
- the published Python package `index-it-mcp`
- the container image `ghcr.io/consiliency/code-index-mcp`

Use [`../SUPPORT_MATRIX.md`](../SUPPORT_MATRIX.md) for support tiers and
[`../validation/ga-readiness-checklist.md`](../validation/ga-readiness-checklist.md)
for the canonical GA boundary. The shared support-tier labels are
`public-alpha`, `beta`, `GA`, `experimental`, `unsupported`, and
`disabled-by-default`.

The repository-topology contract remains unchanged: many unrelated
repositories, one registered worktree per git common directory,
tracked/default branch indexing only, and `index_unavailable` with
`safe_fallback: "native_search"` until readiness is `ready`.

## MRREADY Rollout Gate

MRREADY adds one rollout-facing operator gate for multi-repo deployment. Use
`mcp-index repository list -v`, `mcp-index repository status`, and
`mcp-index artifact workspace-status` before trusting indexed results across a
workspace.

| Rollout status | Meaning | Operator action |
|---|---|---|
| `ready` | Local runtime state, readiness, and artifact state agree. | Queries may use indexed results. |
| `local_only` | Local runtime state works, but durable rollout readiness is not yet proven. | Publish or fetch/reconcile workspace artifacts before broader rollout. |
| `publish_failed` | Artifact publication failed. | Repair the publish path, then rerun publish/reconcile checks. |
| `wrong_branch` | The checkout is not on the tracked/default branch. | Switch to the tracked branch or register the intended repo path. |
| `stale_commit` | The index is behind `HEAD`. | Reindex or fetch the matching artifact, then reconcile workspace state. |
| `missing_index` | No durable local index exists. | Reindex or fetch workspace state before querying. |
| `partial_index_failure` | A required incremental mutation failed and persisted a fail-closed marker. | Run a full reindex or hydrate a known-good artifact, then reconcile workspace state. |

Status surfaces are not query results. Query tools still return
`index_unavailable` with `safe_fallback: "native_search"` for non-ready cases.
The one exception is a staged rebuild whose owner is alive: queries keep
serving the published generation, labelled `stale_commit` with a `rebuild`
object while a newer commit is indexed; see the readiness notes in
[`../SUPPORT_MATRIX.md`](../SUPPORT_MATRIX.md) (#145).
Because multi-repo and STDIO remain beta, the current MRREADY verdict is
`controlled rollout only`.

**Related files**:

- Preflight validation:
  [`../../scripts/preflight_upgrade.sh`](../../scripts/preflight_upgrade.sh)
- Observability verification:
  [`observability-verification.md`](observability-verification.md)
- GA governance evidence:
  [`../validation/ga-governance-evidence.md`](../validation/ga-governance-evidence.md)
- GA E2E evidence:
  [`../validation/ga-e2e-evidence.md`](../validation/ga-e2e-evidence.md)
- GA operations evidence:
  [`../validation/ga-operations-evidence.md`](../validation/ga-operations-evidence.md)

## GAREL Stable-Surface Preparation Status

The current stable surface is prepared for downstream `GADISP` and keeps the
historical rc8 soak evidence intact:

- Branch protection remains enforced via `docs/validation/ga-governance-evidence.md`.
- The historical `v1.2.0-rc8` recut outcome is `recut succeeded`.
- Repo-owned package metadata, workflow defaults, installer helpers, and active
  customer docs now point at the unpublished `v1.4.0` candidate.
- `docs/validation/ga-release-evidence.md` remains intentionally absent until
  downstream `GADISP` dispatches and verifies the stable release.

## Public Alpha Release Gate Checklist

The current RC/public-alpha baseline remains blocked on these required gates:
the original P27-P33 production-readiness contract is still part of the
pre-release checklist, and the current follow-up RC keeps the same release-gate
shape while advancing the artifact identity to `v1.2.0-rc8`.

| Required job | Operator decision | Command/workflow source | Block/fallback behavior |
|---|---|---|---|
| Alpha Gate - Dependency Sync | Dependency metadata and `uv.lock` must match before release qualification. | `.github/workflows/lockfile-check.yml` -> `make alpha-dependency-sync` | Blocks qualification on failure. |
| Alpha Gate - Format And Lint | Formatting and lint posture must stay clean enough to trust the branch. | `.github/workflows/ci-cd-pipeline.yml` -> `make alpha-format-lint` | Blocks qualification on failure. |
| Alpha Gate - Unit And Release Smoke | Unit tests and release smoke must pass on the candidate build. | `.github/workflows/ci-cd-pipeline.yml` -> `make alpha-unit-release-smoke`, `make release-smoke` | Blocks qualification on failure. |
| Alpha Gate - Integration Smoke | Integration and slow-smoke coverage must stay green. | `.github/workflows/ci-cd-pipeline.yml` -> `make alpha-integration-smoke` | Blocks qualification on failure. |
| Alpha Gate - Production Multi-Repo Matrix | Multi-repo isolation and fail-closed readiness behavior must remain frozen. | `.github/workflows/ci-cd-pipeline.yml` -> `make alpha-production-matrix` | Blocks qualification on regression. |
| Alpha Gate - Docker Build And Smoke | The container image must build and pass smoke checks. | `.github/workflows/container-registry.yml` -> `make release-smoke-container` | Blocks qualification on failure. |
| Alpha Gate - Docs Truth | Public docs and release metadata must match the current contract. | `.github/workflows/ci-cd-pipeline.yml` -> `make alpha-docs-truth` | Blocks qualification on failure. |
| Alpha Gate - Required Gates Passed | The CI aggregator must report the required gate set as passed. | `.github/workflows/ci-cd-pipeline.yml` aggregator job | Blocks qualification on failure. |

## Release Governance and Channel Policy

Earlier RELGOV/GACLOSE evidence captured **manual enforcement** while `main`
was unprotected. GAGOV supersedes that baseline with **enforced via branch
protection** on `main`, but the historical manual enforcement wording remains
input evidence and should not be rewritten out of the record.

| Governance item | Current policy source | Disposition |
|---|---|---|
| Branch protection | `gh api repos/ViperJuice/Code-Index-MCP/branches/main` and `/protection` | `main` is protected and enforces the required GA gate contexts before merge. |
| Repository ruleset | `gh api repos/ViperJuice/Code-Index-MCP/rulesets` | No repository rulesets are configured; branch protection is the active enforcement surface. |
| Active RC contract | `docs/validation/ga-rc-evidence.md` and release `v1.2.0-rc8` | `v1.2.0-rc8` remains the active RC/public-alpha package contract. |
| GACLOSE prerequisites | `docs/validation/rc5-release-evidence.md`, `docs/validation/release-governance-evidence.md`, `docs/validation/secondary-tool-readiness-evidence.md`, and `docs/validation/ga-closeout-decision.md` | Preserve the historical inputs before choosing `stay on RC/public-alpha`, `cut a follow-up RC`, or `start a GA hardening roadmap`. |
| GitHub Latest | `gh api repos/ViperJuice/Code-Index-MCP/releases/latest` | GitHub Latest still points at `v2.15.0-alpha.1`; it is excluded from the RC/GA policy source until a final GA release changes that state. |
| Release automation | `.github/workflows/release-automation.yml` | Hyphenated versions are prereleases, prerelease dispatch requires `release_type=custom`, Docker latest is stable-only, and `auto_merge=false` is the RC default. |

## GAGOV Governance Status

As of the GAGOV probe on 2026-04-24, `main` is **enforced via branch
protection** with the exact GABASE gate list:

- `Alpha Gate - Dependency Sync`
- `Alpha Gate - Format And Lint`
- `Alpha Gate - Unit And Release Smoke`
- `Alpha Gate - Integration Smoke`
- `Alpha Gate - Production Multi-Repo Matrix`
- `Alpha Gate - Docker Build And Smoke`
- `Alpha Gate - Docs Truth`
- `Alpha Gate - Required Gates Passed`

The protection disposition recorded in
`docs/validation/ga-governance-evidence.md` is:

- Required status checks: `strict`
- Required pull request reviews: `1 approving review`
- Dismiss stale reviews: `true`
- Require conversation resolution: `true`
- Require linear history: `true`
- Enforce for administrators: `true`
- Repository rulesets: `none`

GitHub Latest still points at `v2.15.0-alpha.1`, while `v1.2.0-rc8` remains
the active RC/public-alpha contract. That split is intentional until a final GA
release changes the channel state. `auto_merge=false` remains the default RC
workflow policy, and Docker latest remains stable-only.

If a later probe shows branch protection drift, a missing required gate
context, or a GitHub product constraint that weakens this policy, stop release
qualification, refresh `docs/validation/ga-governance-evidence.md`, and route
the blocker through GAGOV/GAOPS before GARC dispatches another RC.

## GA Hardening Intake

Before GAGOV, GAE2E, GAOPS, GARC, or GAREL work begins, use
`docs/validation/ga-readiness-checklist.md` as the canonical GABASE checklist.
It freezes the release boundary, support tiers, required gates, evidence map,
rollback expectations, and non-GA surfaces for the active `v1.2.0-rc8`
RC/public-alpha baseline.

Refresh evidence ownership is:

- `docs/validation/ga-governance-evidence.md` -> `GAGOV`
- `docs/validation/ga-e2e-evidence.md` -> `GAE2E`
- `docs/validation/ga-operations-evidence.md` -> `GAOPS`
- `docs/validation/ga-rc-evidence.md` -> `GARC`
- `docs/validation/ga-final-decision.md` -> `GAREL`
- `docs/validation/ga-release-evidence.md` -> `GADISP`

The active post-remediation RC version is `v1.2.0-rc8`, and the next recut
target should advance beyond that version when GARECUT is replanned.

## GARC Follow-Up RC Procedure

GARC is the first GA-hardening phase allowed to dispatch Release Automation.
It may do so only after the repo-owned `rc8` contract surfaces, public docs,
installer helpers, and operator procedure all agree on `v1.2.0-rc8`.

### Pre-dispatch qualification

Before dispatch, the release operator must record all of the following in
`docs/validation/ga-rc-evidence.md`:

```bash
git status --short --branch
git fetch origin main --tags --prune
git rev-parse HEAD origin/main
git tag -l v1.2.0-rc8
git ls-remote --tags origin refs/tags/v1.2.0-rc8
gh workflow view "Release Automation"
```

Qualification passes only when:

1. `git status --short --branch` shows a clean expected release branch state.
2. `git rev-parse HEAD origin/main` reports the same commit for local `HEAD`
   and `origin/main`.
3. No local or remote `v1.2.0-rc8` tag already exists.
4. `docs/validation/ga-governance-evidence.md`,
   `docs/validation/ga-e2e-evidence.md`, and
   `docs/validation/ga-operations-evidence.md` are present as the current
   upstream inputs.
5. `gh workflow view "Release Automation"` confirms the release workflow is
   visible to the operator.

If any pre-dispatch probe fails, stop before release mutation, record the
blocked state in `docs/validation/ga-rc-evidence.md`, and leave the active
release channel unchanged.

Run the current clean-checkout release verification set before dispatch:

```bash
uv run pytest tests/smoke tests/docs tests/test_release_metadata.py
make release-smoke release-smoke-container
git tag -l v1.2.0-rc8
```

Keep `tests/docs/test_p34_public_alpha_recut.py` in this gate set so the
follow-up RC continues to preserve the original public-alpha recut contract.

### Dispatch and workflow observation

When qualification passes, dispatch exactly:

```bash
gh workflow run "Release Automation" -f version=v1.2.0-rc8 -f release_type=custom -f auto_merge=false
gh run list --workflow "Release Automation" --limit 10
gh run watch <run-id> --exit-status
gh run view <run-id> --json url,headSha,status,conclusion,jobs
gh release view v1.2.0-rc8 --repo ViperJuice/Code-Index-MCP --json tagName,isPrerelease,isDraft,publishedAt,targetCommitish,url,assets
```

The operator must record run URL, run ID, `headSha`, per-job conclusions,
release branch or PR disposition, GitHub release state, tag target, and any
PyPI or GHCR publication identity in `docs/validation/ga-rc-evidence.md`.

### RC-only channel policy

The follow-up RC remains a prerelease/public-alpha or beta channel operation:

- `release_type=custom` stays required for hyphenated versions.
- `auto_merge=false` remains the default unless a fresh governance decision
  explicitly approves an exception.
- GitHub Latest remains excluded from the RC policy source.
- Docker `latest` remains stable-only.
- No GA wording or stable-channel claims may be introduced by the `rc8` soak.

## GAOPS Operator Procedure Contract

The operator procedure contract in this runbook consumes:

- `docs/validation/ga-readiness-checklist.md`
- `docs/validation/ga-governance-evidence.md`
- `docs/validation/ga-e2e-evidence.md`
- `docs/validation/ga-operations-evidence.md`

GAOPS stays local-first. Operators own repo registration, allowed-roots
configuration, reindex decisions, readiness remediation, rollback execution,
incident containment, incident response, and support triage. This runbook does
not assume a hosted indexing service or a managed branch-repair path.

### Deployment preflight

Run preflight before any operator qualification step:

```bash
./scripts/preflight_upgrade.sh /path/to/staging.env
```

If the script exits non-zero, stop. If it emits warnings only, record them in
`docs/validation/ga-operations-evidence.md` as `metadata-only` validation, not
as proof that qualification succeeded.

### Deployment qualification

Qualification requires all of the following:

1. The required gate set is green.
2. `docs/validation/ga-governance-evidence.md` still describes enforced branch
   protection on `main`.
3. `docs/validation/ga-e2e-evidence.md` still reflects the current
   release-surface and readiness vocabulary.
4. Observability checks from
   [`observability-verification.md`](observability-verification.md) pass for the
   target environment.

Record the operator decision and evidence mode in
`docs/validation/ga-operations-evidence.md`.

### Rollback and containment

Rollback is operator-owned and environment-specific. The required contract is:

1. Stop further promotion immediately when gates, readiness, or observability
   checks regress.
2. Restore the previous known-good package, wheel, or image version in the
   operator-managed environment.
3. Re-run preflight and the observability procedure after the rollback.
4. Record the rollback trigger, restored version, and follow-up owner in
   `docs/validation/ga-operations-evidence.md`.

### Index rebuild and readiness remediation

When readiness is not `ready`, follow the fail-closed product contract instead
of improvising around it:

```bash
uv run python -m mcp_server.cli.server_commands
```

Then use the MCP or CLI reindex surface for the registered repository and
complete the required index rebuild until the repository returns `ready`. If
the tool returns
`index_unavailable` with `safe_fallback: "native_search"`, that is runtime
remediation only. It is not substitute release evidence.

### Incident response

When a production incident occurs:

1. Capture the failing symptom, affected repo or release surface, and timestamp.
2. Run the observability procedure to confirm health, logs, `/metrics`, and
   redaction behavior.
3. Determine whether the failure is a release gate problem, readiness/index
   problem, support-boundary issue, or external environment issue.
4. If the incident weakens governance assumptions or deployment procedure
   validity, route it through GAOPS before GARC or GAREL proceeds.

### Support triage boundary

Support triage stays bounded to supported deployment surfaces and the frozen
support matrix. Use `docs/SUPPORT_MATRIX.md` for row-level support claims and
`docs/validation/ga-readiness-checklist.md` for product-level release posture.

Route unsupported requests out of the GA path when they depend on:

- same-repo sibling worktrees
- non-default branch indexed routing
- unsupported or disabled-by-default plugin paths
- hosted-service assumptions that the local-first product contract does not own

## Stages

### Stage: dev

Single-node operator-owned validation before a shared environment is touched.

#### Pass criteria

| # | Criterion | Command | Expected result |
|---|---|---|---|
| 1 | Preflight is clean enough to qualify the environment. | `./scripts/preflight_upgrade.sh /path/to/staging.env` | Exit code 0 or warnings captured as metadata-only. |
| 2 | Core smoke and docs checks pass. | `uv run pytest tests/docs/test_gaops_operations_contract.py tests/test_preflight_upgrade.py -v --no-cov` | Exit code 0. |
| 3 | The environment can expose readiness and observability surfaces. | Follow `docs/operations/observability-verification.md` | Health and metrics procedure completes. |

#### Bake window

None. Dev is a pre-promotion validation stage.

#### Rollback trigger

Any failed gate, failed preflight, or failed observability prerequisite.

#### Rollback procedure

Stop promotion, keep the previous known-good operator environment in place, and
record the blocker in `docs/validation/ga-operations-evidence.md`.

### Stage: staging

Shared operator-owned staging environment.

#### Pass criteria

| # | Criterion | Command | Expected result |
|---|---|---|---|
| 1 | Required gate set still matches GAGOV. | Review this runbook and `docs/validation/ga-governance-evidence.md` | Gate list and enforcement posture unchanged. |
| 2 | E2E evidence still matches the candidate surface. | Review `docs/validation/ga-e2e-evidence.md` | Candidate matches the frozen RC/public-alpha surface. |
| 3 | Observability verification passes. | `uv run pytest tests/integration/obs/test_obs_smoke.py -v --no-cov` or the manual procedure | JSON logs, `/metrics`, and redaction checks succeed. |

#### Bake window

Long enough to confirm the target environment serves requests and emits expected
observability data.

#### Rollback trigger

Any regression in readiness, observability, or support-boundary posture.

#### Rollback procedure

Restore the previous known-good build in the operator-managed staging
environment, rerun preflight, and document the result.

### Stage: canary

Partial traffic validation in an operator-managed environment when such a stage
exists.

#### Pass criteria

| # | Criterion | Command | Expected result |
|---|---|---|---|
| 1 | The target environment stays within the supported deployment surface. | Follow the operator procedure contract in this runbook | No unsupported hosted-service assumptions are introduced. |
| 2 | Readiness remains `ready` for the affected repositories. | Reindex or query through the supported surface as needed | No unexpected `index_unavailable` regressions. |
| 3 | Observability checks continue to pass. | Follow `docs/operations/observability-verification.md` | Health, logs, and metrics remain valid. |

#### Bake window

Long enough to observe real traffic without expanding scope beyond the supported
surface.

#### Rollback trigger

Any customer-visible regression, readiness drift, or observability failure.

#### Rollback procedure

Return traffic to the prior build, validate rollback health, and record the
incident and containment decision.

### Stage: full-prod

Full promotion in an operator-managed environment.

#### Pass criteria

| # | Criterion | Command | Expected result |
|---|---|---|---|
| 1 | Preflight, governance, E2E, and observability evidence all remain current. | Review the four GA artifacts named in this runbook | All remain consistent with the promoted build. |
| 2 | Readiness and remediation vocabulary remain fail-closed. | Query the supported surface after promotion | `ready` works, and non-ready cases remain `index_unavailable` with `safe_fallback: "native_search"` when applicable. |
| 3 | Support triage boundaries remain accurate. | Review `docs/SUPPORT_MATRIX.md` and open incidents | No unsupported surface is presented as GA-backed behavior. |

#### Bake window

Long enough for the operator to confirm post-promotion stability in the chosen
environment.

#### Rollback trigger

Any release-surface regression that weakens the frozen support, readiness, or
observability contract.

#### Rollback procedure

Roll back to the prior known-good version, rerun preflight and observability
verification, and route any contract drift back through GAOPS before GARC or
GAREL proceeds.

## Preflight checklist (cross-link to scripts/preflight_upgrade.sh)

Before beginning any stage, run:

```bash
./scripts/preflight_upgrade.sh /path/to/staging.env
```

The script validates the target env file against the `preflight_env`
subcommand. Exit code 0 means there are no fatal errors. Warnings are
non-blocking, but they must be recorded as metadata-only evidence if they
influence an operator decision.
