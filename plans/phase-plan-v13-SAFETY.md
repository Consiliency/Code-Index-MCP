---
phase_loop_plan_version: 1
phase: SAFETY
roadmap: specs/phase-plans-v13.md
roadmap_sha256: 178b8328d8e7dc76ddc0804d7b72d3ccddb55e23577a3d52cb7cbd70d5fd5308
automation:
  suite_command: "env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests/test_v13_safety.py tests/test_artifact_attestation.py tests/test_attestation_probe.py tests/test_artifact_download.py tests/test_artifact_upload.py tests/test_artifact_lifecycle.py tests/security/test_plugin_sandbox.py tests/security/test_p24_sandbox_degradation.py tests/test_sandbox_default_on.py tests/integration/test_sigterm_shutdown.py tests/test_mcptasks_reindex.py tests/test_secret_redaction.py tests/test_server_commands.py tests/test_security.py tests/security/test_auth_boundary.py tests/security/test_auth_startup_wiring.py tests/test_gateway_auth_boundary.py tests/test_workflow_action_pins.py tests/test_workflow_release_policy.py -q --no-cov"
---

# SAFETY: Artifact Trust And Process Lifetime

## Context

Execute only after accepted IF-0-STATE-1. Consume the immutable v13 freeze and
DIST/STATE receipts. Main thread executes both lanes serially in the isolated
v13 worktree. Findings: C09, C13-C15, C19-C20, R04-R05. No real fleet indexing,
embedding inference, source/package publication or index upload is authorized
in this phase. The owner approved one manual signing-only OIDC proof with a
five-minute job cap. Only a synthetic artifact's digest and non-secret metadata
may leave the machine; no commercial provider is involved.

## Interface Freeze Gates

- [x] IF-0-SAFETY-1 - Verified artifact policy, supported capability guards, bounded installed process exit, metadata-only normal diagnostics and local ephemeral admin exposure.

## Frozen Interfaces

`MCP_ATTESTATION_MODE` is trusted operator configuration: enforce (default),
warn or skip; unknown values fail closed. Artifact metadata cannot disable
verification. All download paths verify before tar extraction and before active
installation. Verification binds archive digest, expected repository, approved
workflow identity, source ref and predicate type. No raw CLI stderr or credential
payload is surfaced. Signing-only predicates explicitly state local build and
operator-supplied digest, never GitHub-built SLSA provenance.

Use the existing manual signing workflow with an isolated digest-attestation
mode/job; preserve all existing protected-main image signing guards. No push/PR
trigger expansion. Default runtime trusts protected-main signing; the synthetic
pre-merge proof may explicitly pin its exact branch/source digest in its test
invocation, never by trusting downloaded metadata or broadening production
defaults. No automatic signing dispatch in a watcher or library method.
`attest()` consumes and verifies a previously signed bundle, or returns an
actionable prerequisite failure while preserving the exact prepared archive.
Unsigned publication must not proceed in enforce mode.

Capability guards cover builtins/io/pathlib opens, supported descriptors and
SQLite string/Path/file-URI forms, including roots and readonly mode. Only the
worker's own scratch directory is implicit, not all of /tmp. Native extensions,
pre-imported handles, subprocesses and malicious monkey-patching are explicitly
outside this cooperative guard's containment guarantee. Do not label it an
OS-enforced hostile-plugin sandbox.

STDIO termination stops admission, cancels the serve scope, stops task producers,
reaps owned plugin children and drains resources once. Signals and EOF converge;
repeated signals cannot skip ongoing cleanup or leave the transport running.
Timeout is not proof a thread stopped: retain ownership/fences until quiescent,
or terminate the owning service with a bounded fail-closed fallback before
reporting completion. Never release writer admission around a still-mutating
timed-out worker. Test actual processes, not only mock stop order.

Metrics default to disabled or loopback, have explicit per-process ownership,
expose the registry receiving tool counters, and release the socket on stop.
Port contention is visible and cannot borrow another process's endpoint.
Remote metrics require a protected explicitly configured surface, not an
unauthenticated standalone bind. HTTP remains local/ephemeral; restarts invalidate
sessions. Forwarded client identity is ignored unless a specifically trusted
proxy supplies it. No durable or multi-tenant authentication is introduced.

## Lane Index & Dependencies

SL-0 — Artifact trust and cooperative capability guards
  Depends on: (none)
  Blocks: SL-1
  Parallel-safe: no

SL-1 — Lifecycle, privacy, exposure and documentation reducer
  Depends on: SL-0
  Blocks: (none)
  Parallel-safe: no

## Lanes

### SL-0 - Artifact trust and cooperative capability guards

- **Scope**: Enforce trusted artifact policy and truthful, tested filesystem capabilities.
- **Owned files**: `mcp_server/artifacts/attestation.py`, `mcp_server/artifacts/artifact_download.py`, `mcp_server/artifacts/artifact_upload.py`, `mcp_server/artifacts/publisher.py`, `mcp_server/cli/artifact_commands.py`, `mcp_server/sandbox/caps_apply.py`, `mcp_server/sandbox/capabilities.py`, `mcp_server/sandbox/worker_main.py`, `tests/test_artifact_attestation.py`, `tests/test_attestation_probe.py`, `tests/test_artifact_download.py`, `tests/test_artifact_upload.py`, `tests/test_artifact_lifecycle.py`, `tests/test_artifact_commands.py`, `tests/security/test_plugin_sandbox.py`, `tests/security/test_p24_sandbox_degradation.py`, `tests/test_sandbox_default_on.py`, `tests/test_workflow_action_pins.py`, `tests/test_workflow_release_policy.py`, `.github/workflows/sign-published-image.yml`, `docs/security/attestation.md`, `docs/security/sandbox.md`, `tests/test_rate_limit_retry_after.py`, `tests/test_artifact_publish_rollback.py`, `tests/test_artifact_publish_race.py`, `tests/test_artifact_auto_delta.py`, `tests/security/test_artifact_attestation.py`, `tests/security/test_sl0_interface_surface.py`
- **Interfaces provided**: artifact-trust, capability-boundary
- **Interfaces consumed**: freeze-contract (pre-existing), state-receipt (pre-existing), dist-receipt (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - test: Promote unsigned/missing URL counterexample; assert absent/tampered/wrong signer/unknown mode cannot extract or publish. Keep explicit warn/skip controls separate.
  - impl: Remove unsupported local gh signing and token-display scope probe. Consume supported OIDC bundles with exact trusted policy; bound CLI waits and redact failures.
  - impl: Preserve prepared bytes across manual signing, make direct upload honor the same policy, and retain failed publication evidence rather than silently deleting releases.
  - impl: Add manually dispatched digest-only custom-predicate signing within the existing workflow, immutable action pin, job-scoped OIDC/attestation permissions, five-minute cap and no checkout/build/private artifact upload in the new job.
  - test: Check string/Path/bytes/io/FD opens and readonly SQLite paths, URI modes and root boundaries in disposable subprocesses; preserve plugin import compatibility and document native/subprocess limitations.
  - impl: Guard supported filesystem and SQLite forms using structured path/URI parsing. No broad tmp-directory read exemption or claim of malicious-code containment.
  - verify: Execute synthetic signing-only roundtrip once the exact workflow is pushed, independently verify good and negative controls, record metadata/digest/run identity. Missing live signing proof withholds the gate.

### SL-1 - Lifecycle, privacy, exposure and documentation reducer

- **Scope**: Bounded process and worker ownership, private diagnostics, local admin/metrics policy and final evidence reduction.
- **Owned files**: `mcp_server/cli/stdio_runner.py`, `mcp_server/cli/bootstrap.py`, `mcp_server/cli/server_commands.py`, `mcp_server/cli/task_reindex.py`, `mcp_server/cli/tool_handlers.py`, `mcp_server/storage/mcp_task_registry.py`, `mcp_server/storage/multi_repo_manager.py`, `mcp_server/sandbox/supervisor.py`, `mcp_server/dispatcher/dispatcher_enhanced.py`, `mcp_server/metrics/prometheus_exporter.py`, `mcp_server/security/auth_manager.py`, `mcp_server/security/security_middleware.py`, `mcp_server/security/models.py`, `mcp_server/core/logging.py`, `mcp_server/config/settings.py`, `mcp_server/gateway.py`, `mcp_server/client.py`, `mcp_server/watcher_multi_repo.py`, `mcp_server/watcher/ref_poller.py`, `mcp_server/watcher/sweeper.py`, `mcp_server/watcher/file_watcher.py`, `tests/test_watcher_sweep.py`, `tests/test_watcher.py`, `tests/test_sweeper_observability.py`, `tests/test_bootstrap.py`, `mcp_server/indexing/summarization.py`, `tests/test_v13_safety.py`, `tests/integration/test_sigterm_shutdown.py`, `tests/test_mcptasks_reindex.py`, `tests/test_secret_redaction.py`, `tests/test_server_commands.py`, `tests/test_security.py`, `tests/security/test_auth_boundary.py`, `tests/security/test_auth_startup_wiring.py`, `tests/test_gateway_auth_boundary.py`, `tests/test_dispatcher_fallback_timeout.py`, `tests/test_dispatcher.py`, `tests/test_git_index_manager.py`, `tests/test_watcher_multi_repo.py`, `tests/test_metrics.py`, `tests/security/test_metrics_auth.py`, `tests/test_summarization.py`, `tests/root_tests/test_logging.py`, `scripts/installed_runtime_smoke.py`, `scripts/release_smoke.py`, `scripts/safety_runtime_smoke.py`, `tests/smoke/test_release_smoke_contract.py`, `.env.example`, `Makefile`, `docs/security/auth-boundary.md`, `docs/contracts/v13-safety.md`, `docs/validation/v13/SAFETY.json`, `docs/validation/v13/SAFETY-signing.json`, `docs/status/V13_EXECUTION.md`, `docs/status/localci-validation-contract.md`, `docker/dockerfiles/Dockerfile.production`, `tests/test_mcpauth_stdio_contract.py`, `tests/test_singleton_reset.py`, `tests/test_p16_vocabulary.py`, `tests/test_structured_errors.py`, `tests/test_p24_dispatcher_degradation.py`
- **Depends on**: SL-0
- **Interfaces provided**: bounded-lifecycle, private-diagnostics, local-admin-policy, safety-receipt
- **Interfaces consumed**: artifact-trust, capability-boundary, freeze-contract (pre-existing), state-receipt (pre-existing), dist-receipt (pre-existing)
- **Parallel-safe**: no
- **Tasks**:
  - test: Actual installed STDIO initialization plus SIGTERM, SIGINT, EOF, repeated signals, blocked input and in-flight requests; record exit duration, descendants and open ports. Include partial worker response and timeout cleanup.
  - impl: Converge termination through one awaited lifetime owner; stop tasks/watchers before storage, terminate/reap sandbox processes with bounded I/O, and eliminate timeout-return while mutation continues. Preserve pending generation on interruption.
  - impl: Remove source/query/tool-argument content from normal logs and untrusted error diagnostics across STDIO and cross-repository calls; preserve tool name, duration, counts and typed error metadata.
  - test: Content sentinels on success and exception paths never appear in captured normal logs; no token values in signing diagnostics.
  - impl: Use the counter-owning metrics registry, loopback/disabled default and explicit bind result; close and reuse sockets correctly. Preserve HTTP authenticated metrics separately.
  - test: Two concurrent processes contend for the same metrics port without false ownership; stop/restart/rebind and correct counters.
  - impl: Keep default loopback HTTP and documented ephemeral identity/restart behavior; do not accept spoofed forwarded headers from untrusted peers.
  - impl: Apply the same proxy boundary to the production container; redact HTTP access-query strings and external SDK parse diagnostics. Repair obsolete reset/logging test assumptions without weakening process ownership or unsupported-plugin behavior.
  - verify: Run local gate, installed wheel/container lifecycle smoke, broad offline suite, phase suite and four EC reducers. Record every skip and operational receipt; do not emit IF-0-SAFETY-1 from unit tests alone.
  - impl: Reduce both lanes into the safety contract/receipt and execution status; next phase DATA remains responsible for vector/artifact staging, data retention and full query policy.

## Execution Notes

All consumed receipts and original roadmap/review/audit evidence are read-only.
Read allowlist: committed source/config/docs/tests; accepted phase receipts;
synthetic files and process logs created by this phase. The retained
`docs/status/review-evidence-2026-09-09/test_counterexamples.py` is immutable.
No existing private index, real repository registry or secret environment file
is an input. Safe gh metadata probes may report account/repo/workflow identity,
never token values. The signing probe uses only synthetic local bytes; raw
attestation bundles may be verified from phase-created scratch and are not
committed. Metadata-only receipts identify their hashes.

Control outputs: active plan, `plans/manifest.json`,
`.dev-skills/handoffs/codex-plan-phase/**`,
`.dev-skills/handoffs/codex-execute-phase/**`.
Ignored output allowlist: `.phase-loop/runs/v13-SAFETY-*/**` for synthetic
fixtures, bundle verification and stamped logs; `build/**` and
`index_it_mcp.egg-info/**` packaging scratch, never staged. Handoffs remain
ignored where already ignored. Skill reflections use runtime-resolved roots.
No outer phase-loop launch; preserve upstream agent-harness#819 evidence.

## Verification

- `uv sync --locked --python 3.12 --extra dev`
- `env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests/test_v13_safety.py tests/test_artifact_attestation.py tests/security/test_plugin_sandbox.py tests/integration/test_sigterm_shutdown.py -q --no-cov`
- `make agent-gate`
- `make release-smoke-container`
- `env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests/test_git_index_manager.py -m 'not requires_network and not benchmark' -q --no-cov`
- `env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests --ignore=tests/test_git_index_manager.py -m 'not requires_network and not benchmark' -q --no-cov`
- `git diff --check`

Frontmatter is the required phase suite. Installed lifecycle and live signing
receipts must be machine-checked in the stamped run, with exact candidate and
artifact identities. The signing command is separately authorized and capped;
not a routine hosted test. Group long suites and report all failures, preserving
prior failed evidence. Real browser and PMCP acceptance belong to PILOT.

## Acceptance Criteria

- [x] EC-SAFETY-1 - proven by `tests/test_artifact_attestation.py`, `tests/test_artifact_download.py` and the independently verified `docs/validation/v13/SAFETY-signing.json` receipt; falsified by unsigned extraction, wrong-producer acceptance or an unverified roundtrip.
- [x] EC-SAFETY-2 - proven by `tests/security/test_plugin_sandbox.py` and `docs/security/sandbox.md`; falsified by supported filesystem/SQLite forms escaping capability roots or permitting writes in readonly mode.
- [x] EC-SAFETY-3 - proven by `scripts/safety_runtime_smoke.py`, installed release smoke and `tests/test_v13_safety.py`; falsified by a surviving owned worker, unbounded exit, content sentinel in normal logs or falsely claimed metrics ownership.
- [x] EC-SAFETY-4 - proven by auth restart, loopback and trusted-proxy controls in `tests/test_v13_safety.py` and `tests/test_security.py`; falsified by surviving ephemeral sessions or spoofed client identity from an untrusted peer.

## Spec Closeout Plan

- schema: `spec_delta_closeout.v1`
- decision: `canonical_spec_update`
- target surfaces: `docs/security/attestation.md`, `docs/security/sandbox.md`, `docs/security/auth-boundary.md`, `docs/contracts/v13-safety.md`
- evidence paths: `docs/validation/v13/SAFETY.json`, `docs/validation/v13/SAFETY-signing.json`
- redaction posture: `metadata_only`
- downstream handling: DATA consumes accepted artifact trust and lifetime contracts; no support expansion before PILOT.

## External Inputs

Signing API researched through Context7/GitHub primary documentation on
2026-09-11: https://github.com/actions/attest and
https://cli.github.com/manual/gh_attestation_verify. Resolve the external action
version to an immutable GitHub commit when writing the job; retain its readable
version comment and verify the pin with the workflow supply-chain tests.
