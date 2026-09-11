# Artifact Attestation And Signing

Index archives are built locally. GitHub signs an operator-supplied digest with
a custom predicate; this is **not evidence that GitHub built or inspected the
index**, and is not SLSA build provenance. Container image signing remains a
separate protected-main workflow mode.

## Prepare, Sign, Upload

1. Prepare once, preserving the archive and its metadata:
   `python -m mcp_server.artifacts.artifact_upload --repo OWNER/REPO --prepare-only --output index.tar.gz --metadata-output artifact-metadata.json`.
2. Explicitly dispatch `sign-published-image.yml` in the trusted signing
   repository with `mode=index-attestation` and the printed SHA-256 digest.
   The manual job is capped at five minutes. It has no checkout, index build,
   source upload or private archive upload. Only the digest and descriptive
   metadata are sent. Signing is never dispatched by a watcher or library.
3. Download the bundle using `gh attestation download index.tar.gz --repo OWNER/REPO --predicate-type https://github.com/Consiliency/Code-Index-MCP/local-index-digest/v1`.
   Rename the resulting digest-named JSONL file to
   `index.tar.gz.attestation.jsonl`. Keep the exact prepared archive bytes.
4. Upload with `python -m mcp_server.artifacts.artifact_upload --repo OWNER/REPO --prepared-archive index.tar.gz --prepared-metadata artifact-metadata.json`.
   This checks the checksum and attestation without recompression. Upload
   failure retains local bytes and partial releases for explicit recovery;
   it does not delete diagnostic evidence or report successful publication.

The signing repository must provide the approved workflow. Its OIDC identity
must match the expected repository and workflow used by the verifier. A local
personal access token cannot create that workflow identity. Automatic reindex
publication without a signed sidecar fails closed; use the explicit prepare
and upload flow. Later preparations use unique archive names so they cannot
overwrite an archive awaiting signature.

## Verification Policy

`MCP_ATTESTATION_MODE` accepts exactly:

- `enforce` (default): missing archive/bundle, digest mismatch, invalid producer
  or failed verification raises `AttestationError` before extraction/upload.
- `warn`: explicit operator opt-out; logs a metadata-only warning and continues.
- `skip`: explicit operator opt-out; no attestation verification.

Unknown modes fail closed. Metadata URLs and `allow_unsafe` cannot disable the
configured policy. Enforce mode verifies the bundle, digest, repository,
`.github/workflows/sign-published-image.yml` identity, source ref and custom
predicate using `gh attestation verify`; self-hosted signers are rejected.
The default trusted source ref is `refs/heads/main`. Operator configuration
`MCP_ATTESTATION_SOURCE_REF` and optional `MCP_ATTESTATION_SIGNER_DIGEST` can
narrow an intentional pre-merge synthetic test to an exact branch/head. Never
derive these trusted values from downloaded metadata.

`attest()` only consumes and verifies an existing sidecar. There is no local
`gh attestation sign` call or credential-display probe. The CLI must support
`gh attestation verify` with the configured policy flags. Calls have a bounded
30-second wait; failures do not expose raw CLI stdout/stderr or tokens. Network
or trust-material unavailability is a verification failure, not implicit
permission to skip.

Primary references: [GitHub attestation action](https://github.com/actions/attest)
and [GitHub CLI verification policy](https://cli.github.com/manual/gh_attestation_verify).
