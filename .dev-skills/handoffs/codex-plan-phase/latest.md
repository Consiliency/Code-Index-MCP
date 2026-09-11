---
from: codex-plan-phase
timestamp: "2026-09-11T20:10:40Z"
repo: 448c2cf6
repo_root: /mnt/HC_Volume_105438154/worktrees/Code-Index-MCP-v13-audit-remediation
branch: codex/v13-audit-remediation
branch_slug: codex-v13-audit-remediation
commit: c215e75aec0e11d3071cc29d45131c93f2fff174
run_id: 20260911T201040Z-pilot-browser
artifact: /mnt/workspace/worktrees/Code-Index-MCP-v13-audit-remediation/plans/phase-plan-v13-PILOT.md
artifact_state: staged
next_skill: codex-execute-phase
next_command: codex-execute-phase plans/phase-plan-v13-PILOT.md
next_phase: PILOT
automation:
  status: planned
  verification_status: not_run
  human_required: false
  blocker_class: null
  required_human_inputs: []
  if_gates_produced: []
  issue_inventory: []
  issue_dispositions: []
---

# PILOT Browser And Readiness Checkpoint

FREEZE/DIST/STATE/SAFETY/DATA accepted; PILOT executing, not accepted.
The panel-approved roadmap remains unchanged at
178b8328d8e7dc76ddc0804d7b72d3ccddb55e23577a3d52cb7cbd70d5fd5308.
Outer phase-loop disabled under Consiliency/agent-harness#819.

Pushed c215e75aec0e11d3071cc29d45131c93f2fff174 repairs R07 with one
fresh Git status process per ordinary readiness classification. No cross-request
readiness cache or generation fence removal. Real SHA-1/SHA-256 edits, renames,
deletes, untracked paths, branch/detached/missing Git controls pass. Bare Git
retains metadata resolution but not ready query admission.
The 299-test regression first found bare resolution plus the old 79360 estimate
assertion; both repaired, their targeted tests pass. Full 299 rerun remains due.

Exact installed c215e75 wheel:
ec9d38b86c737d011ab8fb3e01fbed8249993083f5d7e1bd663781e5603dcf01.
Scratch .phase-loop/runs/v13-PILOT-c215e75aec0e/:
- prepare and all 13 offline PMCP goals passed, production metrics included.
- warm-diagnostic.json records 40 samples/class: symbol p95 60.663ms,
  lexical p95 100.681ms. Prior bca40bc symbol failed at 137.489ms PMCP
  and 125.915ms direct STDIO; thresholds unchanged (100/500ms).
- browser-profiled/ real Inspector 2.6.0: symbol query for ledger, ready
  catalog no-match, sibling unsupported/native-search refusal, disconnect/
  reconnect then catalog symbol query, no console errors.
- Admin Swagger displayed missing repository selector fields for /symbol,
  /search and /reindex, plus default 0.1.0 version. Four RED schema tests
  confirmed this. Source now exposes existing repository parameter and package
  version without changing auth, header precedence or readiness behavior.
- Initial admin launch failed without explicit semantic profile selection;
  driver now explicitly sets SEMANTIC_DEFAULT_PROFILE=legacy-default offline.
- Diagnostic browser resources stopped: admin 0.866s, Inspector 0.015s,
  PMCP 1.467s, no survivors. No browser acceptance receipt yet.

Current uncommitted phase-owned work: admin fix/tests, browser-session driver,
saved-receipt artifact hashing/confinement, plan/manifest/control updates.
76 admin/auth/DATA-query tests pass (57.33s). 108 focused driver/budget/readiness/
schema tests pass (9.50s); formatting and diff checks pass.
Planner literal validation passes, structural validator 2 lanes / 0 warnings.
Browser source changes invalidate prior candidate-dependent acceptance evidence.

Next:
1. Commit/push this phase checkpoint; prepare fresh exact-source wheel.
2. Run driver --mode browser --inspector <pinned launcher path>, interactively
   prove admin selector/query/no-match/refusal/reindex and Inspector flows.
   Actual UI session caps at 300s; stop only owned resources using browser/stop.
   Save screenshots to explicit allowed scratch paths and check actual images;
   older plugin-relative screenshots remain diagnostic links, not hashed proof.
3. Finish live driver with a loopback-only rehearsal before any AI endpoint
   contact. Freeze final corpus variants, envelopes, measured samples and
   lifecycle expectations before the first request. Current live mode still
   refuses proof_not_implemented. verify-live only reduces saved evidence.
4. Full standalone PILOT verification, operational receipts/docs, and IF/EC
   acceptance; PREP and SHIP remain downstream, not authorized by partial proof.

No live endpoint contacted, no actual allowance ledger initialized. The one
approved cumulative limit remains 100000 input units / 900 seconds / concurrency
one, synthetic local only. Current estimator is 81740 including framing.
Any changed request envelope/query counts need a derived-plan/estimate update
before admission; never create a fresh ledger to reset an expired allowance.
No version bump, final four-agent code panel, merge or publication.
Keep Fable, GPT-5.6 Sol, Grok 4.5, Gemini 3.1 Pro with reconciliation; no silent
substitutions. Never redispatch signing job 34597512666.
Draft Code-Index-MCP#97 stays open; primary dirty worktree and private indexes
remain untouched. Never prune worktrees or read ambient credentials/fleet data.
