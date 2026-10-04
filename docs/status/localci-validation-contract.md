# LOCALCI Validation Contract

Audit date: 2026-07-06 UTC
Phase plan: `plans/phase-plan-v9-LOCALCI.md`

## Command Contract

| Command | Contract |
| --- | --- |
| `agent-doctor` | Reports local prerequisites and offload metadata without printing secrets. |
| `agent-fast` | Cheap and offline by default validation gate. |
| `agent-gate` | Pre-PR gate mapped to the substantive protected CI suite. |
| `agent-full` | Extends `agent-gate` with heavier validation such as container smoke. |
| `agent-fix` | Deterministic local formatting fixes only. |
| `agent-affected` | docs-only changes route to `agent-fast`; source, workflow, package, lockfile, or unknown changes route to `agent-gate`. |

## Offload And Fail-Closed Semantics

- Offload is explicit and fail-closed.
- `AGENT_REMOTE_HOST` is opt-in only and fails closed when `ssh` is unavailable
  or a remote command is not configured.
- `AGENT_USE_DAGGER=1` is opt-in only and fails closed when `dagger` or
  `AGENT_DAGGER_COMMAND` is unavailable.
- Hosted fallback is not allowed.

## workflow classification

| Workflow | Classification | Notes |
| --- | --- | --- |
| `ci-cd-pipeline.yml` | protected-evidence | Collapsed protected pre-PR validation to `make agent-gate`. |
| `container-registry.yml` | protected-evidence | Keeps container build and smoke as distinct protected evidence. |
| `index-artifact-management.yml` | manual-only | Operator validation only. |
| `index-management.yml` | manual-only | Pull-request hosted rebuild removed. |
| `lockfile-check.yml` | retired | Replaced by `make agent-fast` and `make agent-gate`. |
| `maintenance.yml` | offloaded | External worker notification only. |
| `mcp-index.yml` | manual-only | Pull-request hosted rebuild removed. |
| `release-automation.yml` | manual-only | Manual release workflow still runs `make agent-gate` before mutation. |
| `sign-published-image.yml` | manual-only | Existing one-job manual signer; no routine trigger added by v13. |

## hosted-work reduction summary

- Protected CI now consumes `make agent-gate` instead of a second hosted alpha
  matrix vocabulary.
- Separate hosted lockfile and index rebuild pull-request jobs were removed from
  routine events.
- Container smoke remains protected evidence because it exercises a distinct
  surface from the repo-local Python gate.

## Verification Commands

Verification commands used for LOCALCI:

```bash
uv run pytest tests/test_agent_validation.py -q --no-cov
make agent-doctor
make agent-fast
uv run pytest tests/docs/test_localci_agent_validation_docs.py -q --no-cov
uv run pytest tests/test_localci_workflow_posture.py tests/test_p25_release_gates.py tests/smoke/test_release_smoke_contract.py -q --no-cov
uv run pytest tests/docs/test_localci_validation_contract.py tests/docs/test_localci_agent_validation_docs.py tests/test_localci_workflow_posture.py tests/test_agent_validation.py -q --no-cov
```

No self-hosted runner registration was performed.
No GitHub secret mutation was performed.
No coverage threshold change was performed.
No hosted coverage upload was performed.

## V13 Verification Fidelity

The gate additionally selects SQLite migration/upgrade, BAML parity,
MCP response-schema parity, historical inventory and immutable workflow-census
regressions. `agent-affected` includes tracked deletions and untracked files,
handles names with spaces, and fails closed when Git inspection fails.
Wheel smoke installs locked dependencies outside the checkout and uses the
actual MCP SDK; container smoke uses the configured non-root startup and a
disposable mounted Git repository. Neither permits fake dispatchers.

Coverage policy is unchanged: the normal `pytest.ini` discovery gate has a
35 percent floor, not 80 percent. `make coverage` and `make coverage-baseline`
are bounded reporting selections, not full-suite or 80-percent certifications;
they override default addopts and do not impose that normal-discovery floor.
`--no-cov` runs prove tests only. Broad offline audit commands explicitly include
integration/slow tests and exclude network, benchmark and separately run Git
integration nodes; authenticated/provider/platform omissions must be recorded.
The larger aspirational coverage goal must not be reported as achieved.
