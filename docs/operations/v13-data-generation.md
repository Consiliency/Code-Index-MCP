# Generation Operations

Applies to the v13 implementation candidate. Consult `docs/validation/v13/DATA.json`
for accepted evidence before relying on these changes in a deployment.

## Index And Recover

Use the registered checkout on its recorded tracked/default branch. Commit the
intended changes before indexing; do not transplant an index from another worktree.
Set `MCP_ALLOWED_ROOTS`, `MCP_REPO_REGISTRY` and `MCP_INDEX_STORAGE_PATH` explicitly
for isolated operation. Do not point an acceptance run at a fleet registry.

1. Register the intended repository with `mcp-index repository register <path>`.
2. Check `mcp-index repository status` and the query readiness response.
3. Use the `reindex` MCP tool for the registered repository. A stale/missing index
   requires a whole-repository rebuild; scoped repair cannot certify an unrelated
   commit or worktree.
4. Only consume indexed answers when readiness is `ready`. Otherwise use native
   search and retain the refusal reason while correcting branch, worktree or index.

Automatic watchers reconcile committed create/modify/delete/rename changes and
periodically sweep missed events. Untracked edits and dirty tracked contents are
not silently embedded. The generation writer handles ignored files and removals;
do not manually update file rows or delete SQLite sidecars.

After interruption, preserve the current registry, old generation and failed-stage
evidence. Stop or drain the owning runtime before maintenance, then retry the
registered rebuild. Never remove a live Qdrant lock or substitute a different
server. Metadata/provenance rejection needs corrected configuration or a validated
fresh generation, not a success stamp on existing vectors.

## Artifact Commands

`mcp-index artifact push --repository <name> --validate --compress-only` prepares
a uniquely named archive of the registered generation without uploading it.
Validation opens SQLite read-only and checks integrity and foreign keys. An
omitted repository resolves the registered current directory where possible.
The legacy unregistered local-file path is not fleet provisioning acceptance.

Dropping `--compress-only` requests an external publication, subject to separate
authorization and enforce-mode signing policy. `--skip-if-current` skips only a
commit already recorded as published, not a merely indexed commit. GitHub origin
selection follows the selected registered checkout, not the shell's unrelated
working directory. Prepared files are retained if signing or upload fails.

`artifact sync --repository <name>` reconciles the registered committed generation.
`artifact pull --latest --repository <name>` and workspace recovery require a
verified compatible artifact and use staged admission. An unsafe mismatch option
cannot override registered-generation identity or freshness. Report recovery from
the admitted registry commit, not the downloaded release header.

`local_only` means indexing succeeded without publication. `publish_failed` means
the artifact operation failed; it is not evidence that the local index failed.
`published` names the last acknowledged uploaded commit, which may become older
than current HEAD. Query readiness remains a separate gate in every case.

## Validation And Rollout

Run locked local gates and the DATA phase plan's named wheel/container, Qdrant and
offline suites. File/server Qdrant checks use synthetic deterministic vectors.
The estimate script `scripts/v13_pilot_estimate.py` is offline and reports the
serialized-byte upper bound; it does not spend the owner's inference allowance.

PILOT must separately enforce the 100,000-input-token, 900-second synthetic-only
local budget including retries, verify PMCP and browser workflows, measure latency
under indexing contention, and meet the five-second shutdown bound. No fleet
indexing, expanded support claim or publication is authorized by a local DATA pass.
