"""Verify operator-signed local artifacts with GitHub OIDC attestations."""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mcp_server.core.errors import ArtifactError

logger = logging.getLogger(__name__)
PREDICATE_TYPE = "https://github.com/Consiliency/Code-Index-MCP/local-index-digest/v1"
SIGNER_WORKFLOW = ".github/workflows/sign-published-image.yml"


class AttestationError(ArtifactError):
    pass


@dataclass(frozen=True)
class Attestation:
    bundle_url: str
    bundle_path: Path | None
    subject_digest: str
    signed_at: datetime


def _attestation_mode() -> str:
    mode = os.environ.get("MCP_ATTESTATION_MODE", "enforce")
    if mode not in {"enforce", "warn", "skip"}:
        raise AttestationError("Invalid MCP_ATTESTATION_MODE; expected enforce, warn or skip")
    return mode


def _sha256_of(path: Path) -> str:
    sha256 = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _verify_signature(
    archive_path: Path,
    attestation: Attestation | None,
    *,
    expected_repo: str,
    gh_cmd: str,
) -> None:
    if (
        attestation is None
        or attestation.bundle_path is None
        or not attestation.bundle_path.is_file()
    ):
        raise AttestationError(
            "ATTESTATION_PREREQ: verified attestation sidecar required; "
            "preserve the prepared archive and use the manual digest-signing workflow"
        )
    if not archive_path.is_file():
        raise AttestationError("Attestation artifact file is missing")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", expected_repo):
        raise AttestationError("Invalid attestation repository policy")
    source_ref = os.environ.get("MCP_ATTESTATION_SOURCE_REF", "refs/heads/main")
    if not re.fullmatch(r"refs/(?:heads|tags)/[A-Za-z0-9_./-]+", source_ref):
        raise AttestationError("Invalid attestation source-ref policy")
    signer_digest = os.environ.get("MCP_ATTESTATION_SIGNER_DIGEST")
    if source_ref != "refs/heads/main" and not signer_digest:
        raise AttestationError("Nondefault attestation source ref requires an exact signer digest")
    if signer_digest and not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", signer_digest):
        raise AttestationError("Invalid attestation signer-digest policy")
    if attestation.subject_digest and attestation.subject_digest != _sha256_of(archive_path):
        raise AttestationError("Attestation artifact digest mismatch")
    workflow = f"{expected_repo}/{SIGNER_WORKFLOW}"
    args = [
        gh_cmd,
        "attestation",
        "verify",
        str(archive_path),
        "--bundle",
        str(attestation.bundle_path),
        "--repo",
        expected_repo,
        "--source-ref",
        source_ref,
        "--cert-identity",
        f"https://github.com/{workflow}@{source_ref}",
        "--predicate-type",
        PREDICATE_TYPE,
        "--deny-self-hosted-runners",
    ]
    if signer_digest:
        args.extend(["--signer-digest", signer_digest])
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AttestationError(f"Attestation verification failed ({type(exc).__name__})") from None
    if result.returncode != 0:
        raise AttestationError(f"Attestation verification failed (exit {result.returncode})")


def attest(artifact_path: Path, *, repo: str, gh_cmd: str = "gh") -> Attestation:
    """Consume a previously signed sidecar; never dispatch signing or display tokens."""
    mode = _attestation_mode()
    if mode == "skip":
        return Attestation("", None, "", datetime.now(timezone.utc))
    sidecar = artifact_path.with_suffix(artifact_path.suffix + ".attestation.jsonl")
    attestation = Attestation(
        bundle_url=f"https://github.com/{repo}/attestations",
        bundle_path=sidecar,
        subject_digest=_sha256_of(artifact_path),
        signed_at=datetime.now(timezone.utc),
    )
    try:
        _verify_signature(artifact_path, attestation, expected_repo=repo, gh_cmd=gh_cmd)
    except AttestationError as exc:
        if mode == "enforce":
            raise
        logger.warning("Attestation unavailable: %s", exc)
        return Attestation("", None, attestation.subject_digest, attestation.signed_at)
    return attestation


def verify_attestation(
    archive_path: Path,
    attestation: Attestation | None,
    *,
    expected_repo: str,
    gh_cmd: str = "gh",
) -> None:
    """Apply trusted operator policy regardless of untrusted archive metadata."""
    mode = _attestation_mode()
    if mode == "skip":
        return
    try:
        _verify_signature(archive_path, attestation, expected_repo=expected_repo, gh_cmd=gh_cmd)
    except AttestationError as exc:
        if mode == "enforce":
            raise
        logger.warning("Attestation unavailable: %s", exc)


@functools.lru_cache(maxsize=None)
def probe_gh_attestation_support() -> bool:
    """Return True iff gh attestation help exits 0. Cached per process."""
    try:
        result = subprocess.run(
            ["gh", "attestation", "--help"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def warn_if_gh_attestation_missing() -> None:
    """Report missing verification prerequisites without reading credentials."""
    if _attestation_mode() == "enforce" and not probe_gh_attestation_support():
        logger.warning(
            "ATTESTATION_PREREQ: gh attestation unavailable; enforce-mode artifact "
            "verification will fail closed"
        )
