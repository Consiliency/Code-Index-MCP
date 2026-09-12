"""Read-only, version-only consumption of the accepted v13 live pilot evidence."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

if __package__:
    from .v13_pmcp_pilot import PilotRefused, digest_file, digest_json, verify_saved_receipt
else:
    from v13_pmcp_pilot import PilotRefused, digest_file, digest_json, verify_saved_receipt

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


def main() -> None:
    """Print metadata-only comparison; any missing proof exits unsuccessfully."""
    try:
        pilot = verify_pilot(REPO)
        candidate = compare_candidate(REPO, pilot["source"])
        print(json.dumps({"candidate": candidate, "consumed_pilot": pilot}, indent=2))
    except CandidateRefused as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
