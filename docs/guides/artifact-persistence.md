# Artifact Persistence and Recovery

Artifacts transport an authenticated full snapshot of a committed index. They
do not authorize indexing arbitrary working-tree edits or replacing a live
database in place. Multi-repo and STDIO remain beta: controlled rollout only.

## Register Before Recovery

The supported runtime model is many unrelated repositories, with one registered
worktree per git common directory, on its recorded tracked/default branch.
Use registered repository selectors for
generation-safe recovery and reconciliation:

```bash
mcp-index repository register /path/to/repo
mcp-index repository list -v
mcp-index artifact pull --latest --repository <name>
mcp-index artifact sync --repository <name>
mcp-index artifact workspace-status
```

Registration does not itself prove index readiness. Query tools return
`index_unavailable` with `safe_fallback: "native_search"` until the repository
is ready. A sibling worktree remains unsupported even though it shares the
registered repository's identity.

### Legacy Unregistered Destinations

Unregistered pull/recover supports only a fresh, unused lexical staging
destination. Existing `.mcp-index` state is never overwritten, even with
`--no-backup`; that legacy flag does not bypass generation admission. Register
the repository and retry with `--repository` instead of deleting live state.

Semantic artifacts require registered generation admission. Unregistered
`artifact sync` can inspect or bootstrap a legacy lexical baseline, but cannot
reconcile drift into a live index. Register before reconciliation. A downloaded
baseline alone is not evidence that the MCP can serve authoritative queries.

## Local Generations

The registry identifies the active SQLite generation through `index_path` and
`index_generation`; `.mcp-index/current.db` is not the universal runtime path.
Rebuilds and registered restores stage a new database, validate it, and publish
it with registration/generation compare-and-swap checks. Readers of the previous
generation drain before its resources are retired.

Semantic storage is generation- and profile-scoped, using the explicitly
configured file or server Qdrant backend. A shared server still uses distinct
collection namespaces for staged and published generations. Only the selected,
validated profile is promised; artifacts do not automatically contain every
configured embedding model.

Indexes and metadata are local runtime data, not source-controlled files. A
legacy `code_index.db` archive member is accepted only after metadata validation
and is mapped into the destination's canonical database during staging.

## Publication and Signing

Default `MCP_ATTESTATION_MODE=enforce` requires a signed prepared artifact:

```bash
mcp-index artifact push --repository <name> --prepare-only
# Sign the exact prepared metadata through the approved signing workflow.
mcp-index artifact push --repository <name> \
  --prepared-archive /path/to/archive.tar.gz \
  --prepared-metadata /path/to/artifact-metadata.json
```

See [attestation policy](../security/attestation.md) for signer identity,
sidecars, trust requirements, and explicitly reduced-assurance operator modes.
Preparing an archive is not uploading or publishing it. Neither watcher
publish-on-reindex nor `publish-workspace` silently signs artifacts: both refuse
automatic publication under enforcement and provide prepared-signing guidance.

The direct publisher creates an immutable SHA-keyed release asset set, verifies
the exact archive, canonical metadata, checksum and required attestation assets,
then promotes the release. An existing release is accepted only when its assets
match; failed or ambiguous remote mutations are not overwritten or deleted.
An `index-latest` pointer is updated only after successful asset verification.
This is an advisory discovery pointer, not a remote compare-and-swap primitive;
acceptance always validates the authenticated target identity. Latest selection
examines at most ten full candidates and stops on authentication, integrity or
installation errors. Identity mismatches alone permit another candidate.
Temporary workspace-publish archives are cleaned up after the attempt.

GitHub Actions artifact downloads remain a compatibility transport, not an
alternative trust policy. Normal acceptance tests use deterministic local
fixtures rather than paid inference or live publication. S3, GCS and Azure
provider placeholders are not supported production transports.

## Restore Validation

Downloads verify metadata authenticity, compressed bounds, archive integrity,
repository/branch/commit identity, schema and semantic compatibility before
publishing a generation. Wrong identity, stale commits, missing metadata, bad
checksums and unknown schemas fail closed. Registered restores reject
`--unsafe-allow-mismatched-artifact`; legacy unsafe staging is not runtime
readiness or accepted recovery evidence.

Normal publication produces full snapshots. Delta restore is refused because
an authenticated base-chain restore contract is not implemented. It does not
rewrite signed delta metadata into a full artifact or guess a base generation.
Legacy signed archives without the current canonical metadata and identity
contract must be prepared and signed again; signing alone does not migrate them.

For an explicit target, use `artifact recover --repository <name> --branch main
--commit <sha>`. The requested artifact must still match the registered target's
current accepted identity. Recover is not a bypass for arbitrary historical
state on a different current checkout.

## Default-Branch Updates

When the tracked branch advances, use registered `artifact sync` or reindex to
reconcile committed Git content. The writer checks the source before publication
and refuses dirty tracked files or unverifiable Git state. Commit or otherwise
resolve local edits deliberately; the tool never silently discards them.

Do not switch to a feature branch and expect automatic indexing of that branch.
Wrong-branch and sibling-worktree queries use native search until the supported
registered checkout is restored. Changing the registered checkout requires an
explicit unregister/register operation, not an artifact override.

## Workspace Operations

The historical [MRE2E validation](../validation/mre2e-evidence.md) uses a
deterministic local GitHub/CLI mock to exercise the two-repository lifecycle;
optional live-operator validation is separate and requires authorization.
[MRREADY](../validation/mrready-rollout-readiness.md) defines the controlled
rollout vocabulary, not production or fleet-wide acceptance.

`workspace-status` and repository status report rollout states such as `ready`,
`local_only`, `publish_failed`, `wrong_branch`, `stale_commit`, `missing_index`
and `partial_index_failure`. They are operational guidance, not query results.
`fetch-workspace` restores validated artifacts for registered repositories;
`reconcile-workspace` refreshes the resulting readiness. Publication status is
recorded only for the generation that actually completed the upload.

Validation must cover a real ready query after restore, a non-ready refusal,
and preservation of the previous generation on failure. Relevant suites include
`tests/test_artifact_commands.py`, `tests/test_artifact_download.py`,
`tests/test_artifact_integrity_gate.py`, `tests/test_multi_repo_artifact_coordinator.py`,
`tests/test_git_index_manager.py`, and `tests/test_semantic_indexer_registry.py`.
