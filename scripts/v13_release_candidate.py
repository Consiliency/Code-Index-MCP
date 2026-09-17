"""Read-only validation of legacy and separately authorized fresh v13 evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tomllib
from datetime import datetime
from pathlib import Path

if __package__:
    from .v13_pilot_budget import RENEWED_APPROVAL, BudgetDenied, BudgetLedger
    from .v13_pmcp_pilot import (
        PilotRefused,
        digest_file,
        digest_json,
        validate_receipt,
        verify_saved_receipt,
    )
else:
    from v13_pilot_budget import RENEWED_APPROVAL, BudgetDenied, BudgetLedger
    from v13_pmcp_pilot import (
        PilotRefused,
        digest_file,
        digest_json,
        validate_receipt,
        verify_saved_receipt,
    )

REPO = Path(__file__).resolve().parents[1]
METADATA_FILES = {
    "README.md",
    "CHANGELOG.md",
    "docs/GETTING_STARTED.md",
    "docs/MCP_CONFIGURATION.md",
    "docs/SUPPORT_MATRIX.md",
    "docs/operations/v13-pmcp-pilot.md",
    "docs/operations/v13-release.md",
    "docs/status/V13_EXECUTION.md",
    "docs/validation/v13/PILOT.json",
    "docs/validation/v13/PREP.json",
    "specs/phase-plans-v13_reviews.md",
    "plans/manifest.json",
    "plans/phase-plan-v13-PILOT.md",
    "plans/phase-plan-v13-PREP.md",
    "scripts/v13_release_candidate.py",
    "tests/test_release_metadata.py",
    "tests/test_v13_release_candidate.py",
}
VERSION_TEXT_FILES = {
    ".github/workflows/release-automation.yml",
    "scripts/install-mcp-docker.sh",
    "scripts/install-mcp-docker.ps1",
}


class CandidateRefused(RuntimeError):
    """Candidate no longer qualifies to consume unchanged-runtime pilot proof."""


SIGNING_REPOSITORY = "Consiliency/Code-Index-MCP"
SIGNING_BRANCH = "codex/v13-audit-remediation"
SIGNING_WORKFLOW = ".github/workflows/sign-published-image.yml"
SIGNING_SUBJECT = "index-it-mcp-artifact-metadata.json"


def _signing_context(repo: Path, root: Path) -> dict:
    import yaml

    canonical = repo.resolve() / ".phase-loop/runs/v13-PREP-signing-20260915"
    if root.absolute() != canonical or root.resolve() != canonical:
        raise CandidateRefused("signing_root_outside_plan")
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise CandidateRefused("dirty_candidate")
    if _git(repo, "branch", "--show-current") != SIGNING_BRANCH:
        raise CandidateRefused("signing_branch_mismatch")
    for name in ("artifact-metadata.json", "index.tar.gz"):
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise CandidateRefused("signing_input_invalid")
    workflow = repo / SIGNING_WORKFLOW
    job = yaml.safe_load(workflow.read_text())["jobs"]["attest-local-index"]
    settings = job["steps"][-1]["with"]
    if (
        job["if"] != "inputs.mode == 'index-attestation'"
        or job["timeout-minutes"] != 5
        or job["runs-on"] != "ubuntu-latest"
        or job["permissions"] != {"contents": "read", "id-token": "write", "attestations": "write"}
        or settings["subject-name"] != SIGNING_SUBJECT
        or settings["subject-digest"] != "sha256:${{ inputs.subject_digest }}"
        or settings["push-to-registry"] is not False
        or settings["create-storage-record"] is not False
    ):
        raise CandidateRefused("signing_workflow_policy_mismatch")
    return {
        "approval": "v13-prep-178b8328-20260915-digest-signing",
        "repository": SIGNING_REPOSITORY,
        "source": _git(repo, "rev-parse", "HEAD"),
        "tree": _git(repo, "rev-parse", "HEAD^{tree}"),
        "ref": "refs/heads/" + SIGNING_BRANCH,
        "workflow": SIGNING_WORKFLOW,
        "workflow_sha256": digest_file(workflow),
        "mode": "index-attestation",
        "subject_name": SIGNING_SUBJECT,
        "subject_digest": digest_file(root / "artifact-metadata.json"),
        "archive_sha256": digest_file(root / "index.tar.gz"),
    }


def claim_signing_dispatch(repo: Path, root: Path) -> dict:
    """Record one intent before an operator dispatch; never dispatch or retry here."""
    intent = _signing_context(repo, root)
    try:
        with (root / "dispatch-intent.json").open("x", encoding="utf-8") as stream:
            json.dump(intent, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except FileExistsError:
        raise CandidateRefused("signing_dispatch_already_claimed") from None
    return intent


def verify_signing_proof(repo: Path, root: Path) -> dict:
    """Read-only verification of the one approved metadata-signing exercise."""
    from mcp_server.artifacts.attestation import PREDICATE_TYPE, AttestationError, attest
    from mcp_server.artifacts.integrity_gate import validate_artifact_integrity

    try:
        expected = _signing_context(repo, root)
        for name in (
            "dispatch-intent.json",
            "dispatch.json",
            "artifact-metadata.json.attestation.jsonl",
        ):
            if (root / name).is_symlink() or not (root / name).is_file():
                raise CandidateRefused("signing_proof_missing")
        intent = json.loads((root / "dispatch-intent.json").read_text())
        dispatch = json.loads((root / "dispatch.json").read_text())
        if intent != expected or (
            dispatch["intent_sha256"] != digest_file(root / "dispatch-intent.json")
            or dispatch["accepted"] is not True
            or type(dispatch["dispatch_attempts"]) is not int
            or dispatch["dispatch_attempts"] != 1
            or type(dispatch["run_id"]) is not int
            or dispatch["run_id"] <= 0
        ):
            raise CandidateRefused("signing_dispatch_mismatch")

        def gh_json(*args: str):
            completed = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)
            if completed.returncode:
                raise CandidateRefused("signing_verification_command_failed")
            return json.loads(completed.stdout)

        run_id = dispatch["run_id"]
        run = gh_json("api", f"repos/{SIGNING_REPOSITORY}/actions/runs/{run_id}")
        if (
            run["id"] != run_id
            or run["run_attempt"] != 1
            or run["head_sha"] != expected["source"]
            or run["head_branch"] != SIGNING_BRANCH
            or run["path"] != SIGNING_WORKFLOW
            or run["event"] != "workflow_dispatch"
            or run["repository"]["full_name"] != SIGNING_REPOSITORY
            or run["status"] != "completed"
            or run["conclusion"] != "success"
        ):
            raise CandidateRefused("signing_run_mismatch")
        pages = gh_json(
            "api",
            f"repos/{SIGNING_REPOSITORY}/actions/workflows/sign-published-image.yml/runs",
            "--method",
            "GET",
            "-f",
            f"head_sha={expected['source']}",
            "-f",
            f"branch={SIGNING_BRANCH}",
            "-f",
            "event=workflow_dispatch",
            "--paginate",
            "--slurp",
        )
        matching_runs = [item for page in pages for item in page["workflow_runs"]]
        if [item["id"] for item in matching_runs] != [run_id]:
            raise CandidateRefused("signing_dispatch_count_mismatch")
        jobs = gh_json("api", f"repos/{SIGNING_REPOSITORY}/actions/runs/{run_id}/attempts/1/jobs")[
            "jobs"
        ]
        signed = [
            job for job in jobs if job["name"] == "Attest operator-supplied local index digest"
        ]
        if len(signed) != 1 or any(
            job["conclusion"] != "skipped" for job in jobs if job not in signed
        ):
            raise CandidateRefused("signing_job_mismatch")
        job = signed[0]
        seconds = (
            datetime.fromisoformat(job["completed_at"]) - datetime.fromisoformat(job["started_at"])
        ).total_seconds()
        if job["conclusion"] != "success" or job["run_id"] != run_id or not 0 <= seconds <= 300:
            raise CandidateRefused("signing_job_failed_or_over_budget")
        if (
            os.environ.get("MCP_ATTESTATION_MODE", "enforce") != "enforce"
            or os.environ.get("MCP_ATTESTATION_SOURCE_REF") != expected["ref"]
            or os.environ.get("MCP_ATTESTATION_SIGNER_DIGEST") != expected["source"]
        ):
            raise CandidateRefused("signing_verifier_policy_mismatch")
        metadata_path = root / "artifact-metadata.json"
        attestation = attest(metadata_path, repo=SIGNING_REPOSITORY)
        verified = gh_json(
            "attestation",
            "verify",
            str(metadata_path),
            "--bundle",
            str(attestation.bundle_path),
            "--repo",
            SIGNING_REPOSITORY,
            "--source-ref",
            expected["ref"],
            "--signer-digest",
            expected["source"],
            "--source-digest",
            expected["source"],
            "--cert-identity",
            f"https://github.com/{SIGNING_REPOSITORY}/{SIGNING_WORKFLOW}@{expected['ref']}",
            "--predicate-type",
            PREDICATE_TYPE,
            "--deny-self-hosted-runners",
            "--format",
            "json",
        )
        if len(verified) != 1:
            raise CandidateRefused("signing_bundle_ambiguous")
        result = verified[0]["verificationResult"]
        certificate = result["signature"]["certificate"]
        statement = result["statement"]
        if (
            certificate["runInvocationURI"]
            != f"https://github.com/{SIGNING_REPOSITORY}/actions/runs/{run_id}/attempts/1"
            or statement["subject"]
            != [{"name": SIGNING_SUBJECT, "digest": {"sha256": expected["subject_digest"]}}]
            or statement["predicateType"] != PREDICATE_TYPE
            or statement["predicate"]
            != {
                "artifact_origin": "local",
                "digest_origin": "operator-supplied",
                "built_in_this_workflow": False,
            }
        ):
            raise CandidateRefused("signing_attestation_binding_mismatch")
        metadata = json.loads(metadata_path.read_text())
        if not validate_artifact_integrity(metadata, root / "index.tar.gz").passed:
            raise CandidateRefused("signing_archive_integrity_failed")
        return {
            **expected,
            "run_id": run_id,
            "run_attempt": 1,
            "job_seconds": seconds,
            "production_verifier": "passed",
            "observed_candidate_dispatches": 1,
            "new_dispatches": 0,
        }
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        subprocess.TimeoutExpired,
        AttestationError,
    ) as exc:
        raise CandidateRefused("signing_evidence_missing_or_invalid") from exc


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _tree(repo: Path, ref: str) -> dict[str, tuple[str, str, str]]:
    result = {}
    for entry in _git(repo, "ls-tree", "-rz", "--full-tree", ref).split("\0"):
        if entry:
            metadata, name = entry.split("\t", 1)
            result[name] = tuple(metadata.split())
    return result


def compare_candidate(repo: Path, source: str) -> dict:
    """Compare committed content, refusing all unlisted or behavioral changes."""
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source):
        raise CandidateRefused("invalid_source_identity")
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise CandidateRefused("dirty_candidate")
    head = _git(repo, "rev-parse", "HEAD")
    before, after = _tree(repo, source), _tree(repo, head)

    def text_at(ref: str, path: str) -> str:
        return subprocess.check_output(["git", "-C", str(repo), "show", f"{ref}:{path}"]).decode()

    old_project = tomllib.loads(text_at(source, "pyproject.toml"))
    new_project = tomllib.loads(text_at(head, "pyproject.toml"))
    old_version = old_project["project"].pop("version")
    new_version = new_project["project"].pop("version")
    if old_project != new_project or not all(
        isinstance(v, str) and re.fullmatch(r"\d+\.\d+\.\d+", v) for v in (old_version, new_version)
    ):
        raise CandidateRefused("project_contract_changed")
    locks = []
    for ref, expected in ((source, old_version), (head, new_version)):
        lock = tomllib.loads(text_at(ref, "uv.lock"))
        roots = [p for p in lock["package"] if p["name"] == "index-it-mcp"]
        if len(roots) != 1 or roots[0].pop("version") != expected:
            raise CandidateRefused("lock_root_version_mismatch")
        locks.append(lock)
    if locks[0] != locks[1]:
        raise CandidateRefused("dependency_lock_changed")

    changed = sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )
    for path in changed:
        if path in METADATA_FILES or (
            path.startswith(".dev-skills/handoffs/") and path.endswith(".md")
        ):
            continue
        if path not in {"pyproject.toml", "uv.lock", "mcp_server/__init__.py"} | VERSION_TEXT_FILES:
            raise CandidateRefused(f"runtime_content_changed:{path}")
        if path not in before or path not in after or before[path][:2] != after[path][:2]:
            raise CandidateRefused(f"runtime_file_identity_changed:{path}")
        if path in {"pyproject.toml", "uv.lock"}:
            continue
        old, new = text_at(source, path), text_at(head, path)
        if path == "mcp_server/__init__.py":
            assignment = f'__version__ = "{old_version}"'
            if old.count(assignment) != 1:
                raise CandidateRefused("runtime_version_assignment_invalid")
            expected = old.replace(assignment, f'__version__ = "{new_version}"', 1)
        else:
            expected = old.replace(old_version, new_version)
            if path.startswith("scripts/install-mcp-docker."):
                expected = expected.replace(
                    "Published release image (default)",
                    "Versioned release image (requires publication)",
                )
        if new != expected:
            raise CandidateRefused(f"non_version_content_changed:{path}")
    return {
        "source": head,
        "tree": _git(repo, "rev-parse", "HEAD^{tree}"),
        "pilot_source": source,
        "old_version": old_version,
        "new_version": new_version,
        "changed_files": changed,
        "runtime_package_files": sum(p.startswith("mcp_server/") for p in after),
        "runtime_comparison": "identical_except_version_assignment",
        "dependency_comparison": "identical_except_root_package_version",
    }


def verify_pilot(repo: Path) -> dict:
    """Validate archived original-source records without loading current bindings."""
    try:
        receipt = json.loads((repo / "docs/validation/v13/PILOT.json").read_text())
        if receipt["terminal_status"] != "complete" or receipt["verification_status"] != "passed":
            raise CandidateRefused("pilot_not_accepted")
        manifest_path = repo / receipt["manifest_path"]
        allowed = repo / ".phase-loop/runs/v13-PILOT-366f6bca7765"
        if manifest_path.resolve() != (allowed / "manifest.json").resolve():
            raise CandidateRefused("pilot_manifest_path_changed")
        if digest_file(manifest_path) != receipt["manifest_file_sha256"]:
            raise CandidateRefused("pilot_manifest_changed")
        manifest = json.loads(manifest_path.read_text())
        if (
            manifest["source"] != receipt["source_commit"]
            or manifest["tree"] != receipt["source_tree"]
            or digest_json(manifest) != receipt["manifest_binding_sha256"]
            or digest_file(allowed / "constraints.txt") != manifest["constraints_sha256"]
        ):
            raise CandidateRefused("pilot_binding_changed")
        wheel = Path(manifest["wheel"])
        if (
            wheel.name != str(wheel)
            or digest_file(allowed / "dist" / wheel) != manifest["wheel_sha256"]
        ):
            raise CandidateRefused("pilot_wheel_changed")
        proof = receipt["operational_proofs"]["live"]
        if (
            proof["path"] != str((allowed / "live.json").relative_to(repo))
            or digest_file(allowed / "live.json") != proof["sha256"]
        ):
            raise CandidateRefused("pilot_live_record_changed")
        result = verify_saved_receipt(allowed, manifest, "live")
        return {
            "source": manifest["source"],
            "manifest_sha256": digest_json(manifest),
            "live_sha256": proof["sha256"],
            "wheel_sha256": manifest["wheel_sha256"],
            "reserved_input_units": result["budget"]["reserved_input_units"],
            "new_inference_requests": 0,
            "evidence_scope": "original_pilot_source_only",
        }
    except (OSError, KeyError, TypeError, ValueError, PilotRefused) as exc:
        raise CandidateRefused("pilot_evidence_missing_or_invalid") from exc


def verify_renewed_pilot(repo: Path, root: Path) -> dict:
    """Validate fresh exact-candidate proof without widening version-only reuse."""
    root = root.absolute()
    runs = repo.resolve() / ".phase-loop" / "runs"
    if (
        root != root.resolve()
        or root.parent != runs
        or not re.fullmatch(r"v13-PILOT-PREP-[A-Za-z0-9-]+", root.name)
    ):
        raise CandidateRefused("renewed_root_outside_plan")
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise CandidateRefused("dirty_candidate")

    def checked_file(relative: str) -> Path:
        path = root / relative
        if Path(relative).is_absolute() or path != path.resolve() or not path.is_file():
            raise CandidateRefused("renewed_artifact_path_invalid")
        if not path.is_relative_to(root):
            raise CandidateRefused("renewed_artifact_path_invalid")
        return path

    try:
        manifest = json.loads(checked_file("manifest.json").read_text())
        identity = {
            "source": _git(repo, "rev-parse", "HEAD"),
            "tree": _git(repo, "rev-parse", "HEAD^{tree}"),
            "lock_sha256": digest_file(repo / "uv.lock"),
        }
        if any(manifest.get(key) != value for key, value in identity.items()):
            raise CandidateRefused("renewed_candidate_binding_changed")
        wheel_name = manifest["wheel"]
        if not isinstance(wheel_name, str) or Path(wheel_name).name != wheel_name:
            raise CandidateRefused("renewed_wheel_path_invalid")
        if (
            digest_file(checked_file("dist/" + wheel_name)) != manifest["wheel_sha256"]
            or digest_file(checked_file("constraints.txt")) != manifest["constraints_sha256"]
        ):
            raise CandidateRefused("renewed_artifact_binding_changed")
        offline = json.loads(checked_file("offline.json").read_text())
        validate_receipt(offline, manifest, "offline")
        for kind in ("browser", "rehearsal", "live"):
            saved = json.loads(checked_file(kind + ".json").read_text())
            for artifact in saved.get("artifacts", []):
                checked_file(artifact["path"])
        browser = verify_saved_receipt(root, manifest, "browser")
        verify_saved_receipt(root, manifest, "rehearsal")
        live = verify_saved_receipt(root, manifest, "live", expected_approval=RENEWED_APPROVAL)
        if live["budget"]["approval"] != RENEWED_APPROVAL:
            raise CandidateRefused("renewed_approval_mismatch")
        canonical = runs / "v13-PILOT-allowance-20260915"
        ledger = BudgetLedger(
            canonical, digest_json(manifest), approval=RENEWED_APPROVAL, read_only=True
        )
        archived = [item for item in live["artifacts"] if item["role"] == "allowance_ledger"]
        if len(archived) != 1 or (
            digest_file(canonical / "ledger.sqlite") != archived[0]["sha256"]
            or ledger.snapshot()
            != {key: value for key, value in live["budget"].items() if key != "elapsed_seconds"}
        ):
            raise CandidateRefused("renewed_canonical_ledger_mismatch")
        return {
            **identity,
            "manifest_sha256": digest_json(manifest),
            "wheel_sha256": manifest["wheel_sha256"],
            "approval": RENEWED_APPROVAL,
            "reserved_input_units": live["budget"]["reserved_input_units"],
            "proof_sha256": {
                kind: digest_file(root / f"{kind}.json")
                for kind in ("offline", "browser", "rehearsal", "live")
            },
            "browser_goal_count": len(browser["goals"]),
            "new_inference_requests": 0,
            "evidence_scope": "fresh_exact_candidate_pilot",
        }
    except (OSError, KeyError, TypeError, ValueError, PilotRefused, BudgetDenied) as exc:
        raise CandidateRefused("renewed_pilot_evidence_missing_or_invalid") from exc


def main() -> None:
    """Print metadata-only comparison; any missing proof exits unsuccessfully."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--renewed-pilot-root", type=Path)
    args = parser.parse_args()
    try:
        pilot = verify_pilot(REPO)
        if args.renewed_pilot_root is None:
            candidate = compare_candidate(REPO, pilot["source"])
            result = {"candidate": candidate, "consumed_pilot": pilot}
        else:
            result = {
                "renewed_candidate": verify_renewed_pilot(REPO, args.renewed_pilot_root),
                "signing_proof": verify_signing_proof(
                    REPO, REPO / ".phase-loop/runs/v13-PREP-signing-20260915"
                ),
                "original_pilot_read_only_control": pilot,
            }
        print(json.dumps(result, indent=2))
    except CandidateRefused as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
