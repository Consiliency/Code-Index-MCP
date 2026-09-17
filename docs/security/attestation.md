# Artifact Attestation And Signing

Index archives are built locally. GitHub signs the operator-supplied digest of
canonical `artifact-metadata.json`, which binds the repository, branch, commit,
schema, semantic profile and archive SHA-256. It uses
a custom predicate; this is **not evidence that GitHub built or inspected the
index**, and is not SLSA build provenance. Container image signing remains a
separate protected-main workflow mode.

## Prepare, Sign, Upload

1. Prepare once, preserving the archive and its metadata:
   `python -m mcp_server.artifacts.artifact_upload --repo OWNER/REPO --prepare-only --output index.tar.gz --metadata-output artifact-metadata.json`.
   For registered repositories the installed entry point is
   `mcp-index artifact push --repository NAME --prepare-only --metadata-output artifact-metadata.json`.
2. Explicitly dispatch `sign-published-image.yml` in the trusted signing
   repository with `mode=index-attestation` and the printed `sha256` digest of
   the metadata file, NOT the separate `archive_sha256`.
   The manual job is capped at five minutes. It has no checkout, index build,
   source upload or private archive upload. Only the digest and descriptive
   metadata are sent. Signing is never dispatched by a watcher or library.
3. Download the bundle using `gh attestation download artifact-metadata.json --repo OWNER/REPO --predicate-type https://github.com/Consiliency/Code-Index-MCP/local-index-digest/v1`.
   Rename the resulting digest-named JSONL file to
   `artifact-metadata.json.attestation.jsonl`. Keep the exact prepared archive
   AND metadata bytes; preparation refuses to overwrite existing metadata.
4. Upload with `python -m mcp_server.artifacts.artifact_upload --repo OWNER/REPO --prepared-archive index.tar.gz --prepared-metadata artifact-metadata.json`.
   The equivalent registered-repository command is
   `mcp-index artifact push --repository NAME --prepared-archive ARCHIVE --prepared-metadata artifact-metadata.json`.
   This verifies the metadata attestation and its archive checksum without
   recompression or changing signed metadata. Upload
   failure retains local bytes and partial releases for explicit recovery;
   it does not delete diagnostic evidence or report successful publication.

The signing repository must provide the approved workflow. Its OIDC identity
must match the expected repository and workflow used by the verifier. A local
personal access token cannot create that workflow identity. Automatic reindex
publication in enforce mode stops before compression or remote calls; use the
explicit prepare and upload flow. Later preparations use unique archive names so they cannot
overwrite an archive awaiting signature.
Workspace-wide automatic publication also refuses before compression in enforce
mode; prepare and upload each repository explicitly. `artifact pull --latest`
discovers index Release assets as well as legacy Actions artifacts; assetless
pointer releases are not downloadable payloads.

Pre-1.4.1 archive-only attestations do not authenticate repository identity and
are refused in enforce mode. Use `--metadata-only` with the preserved archive's
`--checksum`, `--size`, repository identity, commit and original index context
to create a fresh metadata file, then sign that file and use the prepared upload
flow. Do not regenerate identity from a different checkout or weaken verification
to reuse an old signature.
Restores return a new private `verified-*` directory beneath the requested
output directory. They never overlay prior files, and tar links/special files
and an embedded `artifact-metadata.json` are rejected.
The untrusted outer Actions ZIP is limited to four flat payload files, 2 GiB
downloaded and expanded bytes, 1 MiB metadata, 4 MiB attestation and 1 KiB checksum.
Downloads have a five-minute bound. Unexpected, duplicate, linked or ambiguous
members are refused before expansion. No archive upload or signing is automatic.

## Verification Policy

`MCP_ATTESTATION_MODE` accepts exactly:

- `enforce` (default): missing metadata/bundle, digest mismatch, invalid producer
  or failed verification raises `AttestationError` before extraction/upload.
- `warn`: explicit operator opt-out; logs a metadata-only warning and continues.
- `skip`: explicit operator opt-out; no attestation verification.

Unknown modes fail closed. Metadata URLs and `allow_unsafe` cannot disable the
configured policy. Enforce mode verifies the metadata bundle, digest, repository,
`.github/workflows/sign-published-image.yml` identity, source ref and custom
predicate using `gh attestation verify`; self-hosted signers are rejected.
The default trusted source ref is `refs/heads/main`. Operator configuration
`MCP_ATTESTATION_SOURCE_REF` and `MCP_ATTESTATION_SIGNER_DIGEST` can
narrow an intentional pre-merge synthetic test to an exact branch/head. A
nondefault source ref requires an exact signer digest. Never
derive these trusted values from downloaded metadata.
The signed checksum is authoritative; a checksum sidecar cannot replace it.

`attest()` only consumes and verifies an existing sidecar. There is no local
`gh attestation sign` call or credential-display probe. The CLI must support
`gh attestation verify` with the configured policy flags. Calls have a bounded
30-second wait; failures do not expose raw CLI stdout/stderr or tokens. Network
or trust-material unavailability is a verification failure, not implicit
permission to skip.

Primary references: [GitHub attestation action](https://github.com/actions/attest)
and [GitHub CLI verification policy](https://cli.github.com/manual/gh_attestation_verify).
