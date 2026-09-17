"""Release evidence consumption must not disguise changed runtime behavior."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import v13_pilot_budget as budget
from scripts import v13_release_candidate as release
from scripts.v13_pmcp_pilot import GOALS, PilotRefused, digest_file, digest_json
from scripts.v13_release_candidate import CandidateRefused, compare_candidate, verify_pilot


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo: Path) -> str:
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "synthetic candidate")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def candidate(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Synthetic")
    git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    files = {
        "pyproject.toml": '[project]\nname="index-it-mcp"\nversion="1.4.0"\ndependencies=["x==1"]\n',
        "uv.lock": 'version=1\n[[package]]\nname="index-it-mcp"\nversion="1.4.0"\nsource={editable="."}\n[[package]]\nname="x"\nversion="1"\n',
        "mcp_server/__init__.py": '__version__ = "1.4.0"\n',
        "mcp_server/gateway.py": "VALUE = 1\n",
        "Dockerfile.production": "FROM python:3.12\n",
        ".github/workflows/release-automation.yml": "default: 'v1.4.0'\n",
        "scripts/install-mcp-docker.sh": 'MCP_VERSION="v1.4.0"\n',
        "README.md": "Published history\n",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    base = commit(tmp_path)
    for name in ("pyproject.toml", "uv.lock", "mcp_server/__init__.py"):
        path = tmp_path / name
        path.write_text(path.read_text().replace("1.4.0", "1.4.1"))
    commit(tmp_path)
    return tmp_path, base


def test_version_only_candidate_preserves_runtime(candidate):
    repo, base = candidate
    result = compare_candidate(repo, base)
    assert result["old_version"] == "1.4.0"
    assert result["new_version"] == "1.4.1"
    assert result["source"] == git(repo, "rev-parse", "HEAD")
    assert result["runtime_package_files"] == 2


@pytest.mark.parametrize(
    "path,content",
    [
        ("mcp_server/gateway.py", "VALUE = 2\n"),
        ("mcp_server/new.py", "VALUE = 1\n"),
        ("mcp_server/__init__.py", '__version__ = "1.4.1"\nraise RuntimeError()\n'),
        ("mcp_server/__init__.py", '__version__ = "1.4.1" # changed structure\n'),
        ("Dockerfile.production", "FROM python:3.13\n"),
        ("config.yaml", "endpoint: changed\n"),
        ("scripts/v13_pmcp_pilot.py", "pass\n"),
        (".github/workflows/release-automation.yml", "default: 'v1.4.1'\npermissions: write-all\n"),
        ("scripts/install-mcp-docker.sh", 'MCP_VERSION="v1.4.1"\necho unsafe\n'),
    ],
)
def test_runtime_changes_cannot_reuse_live_proof(candidate, path, content):
    repo, base = candidate
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    commit(repo)
    with pytest.raises(CandidateRefused):
        compare_candidate(repo, base)


@pytest.mark.parametrize("path", ["pyproject.toml", "uv.lock"])
def test_dependency_changes_cannot_reuse_live_proof(candidate, path):
    repo, base = candidate
    target = repo / path
    target.write_text(
        target.read_text().replace("x==1", "x==2").replace('version="1"', 'version="2"')
    )
    commit(repo)
    with pytest.raises(CandidateRefused):
        compare_candidate(repo, base)


def test_deleted_runtime_file_cannot_reuse_live_proof(candidate):
    repo, base = candidate
    (repo / "mcp_server/gateway.py").unlink()
    commit(repo)
    with pytest.raises(CandidateRefused):
        compare_candidate(repo, base)


@pytest.mark.parametrize("path", ["README.md", "mcp_server/gateway.py", "new.py"])
def test_uncommitted_candidate_is_refused(candidate, path):
    repo, base = candidate
    (repo / path).write_text("dirty\n")
    with pytest.raises(CandidateRefused, match="dirty_candidate"):
        compare_candidate(repo, base)


def test_metadata_only_changes_are_reported(candidate):
    repo, base = candidate
    (repo / "README.md").write_text("Prepared candidate, not publication\n")
    workflow = repo / ".github/workflows/release-automation.yml"
    workflow.write_text(workflow.read_text().replace("1.4.0", "1.4.1"))
    commit(repo)
    result = compare_candidate(repo, base)
    assert "README.md" in result["changed_files"]


def test_missing_pilot_receipt_is_refused(tmp_path):
    with pytest.raises(CandidateRefused, match="pilot_evidence_missing_or_invalid"):
        verify_pilot(tmp_path)


def test_invalid_commit_input_is_refused(candidate):
    repo, _ = candidate
    with pytest.raises(CandidateRefused, match="invalid_source_identity"):
        compare_candidate(repo, "--help")


@pytest.mark.parametrize("damage", ["status", "path", "hash", "binding"])
def test_tampered_pilot_metadata_is_refused(tmp_path, damage):
    root = tmp_path / ".phase-loop/runs/v13-PILOT-366f6bca7765"
    root.mkdir(parents=True)
    manifest = root / "manifest.json"
    manifest.write_text("{}")
    receipt = {
        "terminal_status": "complete",
        "verification_status": "passed",
        "manifest_path": str(manifest.relative_to(tmp_path)),
        "manifest_file_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }
    if damage == "status":
        receipt["verification_status"] = "failed"
    elif damage == "path":
        receipt["manifest_path"] = "outside.json"
    elif damage == "hash":
        receipt["manifest_file_sha256"] = "0" * 64
    path = tmp_path / "docs/validation/v13/PILOT.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(receipt))
    with pytest.raises(CandidateRefused):
        verify_pilot(tmp_path)


@pytest.fixture
def renewed_candidate(candidate, monkeypatch):
    repo, _ = candidate
    (repo / ".gitignore").write_text(".phase-loop/\n")
    commit(repo)
    runs = repo / ".phase-loop/runs"
    root = runs / "v13-PILOT-PREP-synthetic-test"
    (root / "dist").mkdir(parents=True)
    (root / "dist/fixture.whl").write_bytes(b"synthetic unit-test wheel")
    (root / "constraints.txt").write_text("x==1\n")
    manifest = {
        "source": git(repo, "rev-parse", "HEAD"),
        "tree": git(repo, "rev-parse", "HEAD^{tree}"),
        "lock_sha256": digest_file(repo / "uv.lock"),
        "wheel": "fixture.whl",
        "wheel_sha256": digest_file(root / "dist/fixture.whl"),
        "constraints_sha256": digest_file(root / "constraints.txt"),
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    canonical = runs / "v13-PILOT-allowance-20260915"
    monkeypatch.setattr(budget, "RENEWED_ROOT", canonical)
    budget.BudgetLedger.initialize(
        canonical, digest_json(manifest), approval=budget.RENEWED_APPROVAL
    )
    ledger = budget.BudgetLedger(canonical, digest_json(manifest), approval=budget.RENEWED_APPROVAL)
    request = ledger.reserve("embedding", 100)
    ledger.finish(request, "success", 200)
    (root / "archive").mkdir()
    (root / "archive/ledger.sqlite").write_bytes((canonical / "ledger.sqlite").read_bytes())
    for kind in ("offline", "browser", "rehearsal", "live"):
        receipt_kind = "live" if kind == "rehearsal" else kind
        receipt = {
            "kind": receipt_kind,
            "source": manifest["source"],
            "wheel_sha256": manifest["wheel_sha256"],
            "manifest_sha256": digest_json(manifest),
            "goals": dict.fromkeys(GOALS[receipt_kind], True),
            "shutdown_seconds": [1],
            "surviving_children": [],
            "peak_rss_mib": 100,
            "budget": {**ledger.snapshot(), "elapsed_seconds": 1},
            "artifacts": [
                {
                    "role": "allowance_ledger",
                    "path": "archive/ledger.sqlite",
                    "sha256": digest_file(root / "archive/ledger.sqlite"),
                }
            ],
        }
        (root / f"{kind}.json").write_text(json.dumps(receipt))

    # Operational record reduction is tested with real fixtures in test_v13_pmcp_pilot.
    def verified_receipt(
        path, candidate_manifest, kind, *, expected_approval=None, receipt_bytes=None
    ):
        assert path == root and candidate_manifest == manifest
        if kind == "live":
            assert expected_approval == budget.RENEWED_APPROVAL
        return json.loads(receipt_bytes)

    monkeypatch.setattr(release, "verify_saved_receipt", verified_receipt)
    return repo, root, canonical


def test_fresh_candidate_validation_is_read_only_and_distinct(renewed_candidate):
    repo, root, canonical = renewed_candidate
    before = (canonical / "ledger.sqlite").read_bytes()
    result = release.verify_renewed_pilot(repo, root)
    assert result["evidence_scope"] == "fresh_exact_candidate_pilot"
    assert result["approval"] == budget.RENEWED_APPROVAL
    assert result["new_inference_requests"] == 0
    assert (canonical / "ledger.sqlite").read_bytes() == before


@pytest.mark.parametrize(
    "damage",
    [
        "source",
        "tree",
        "lock",
        "wheel",
        "constraints",
        "offline",
        "canonical",
        "archived",
        "approval",
        "dirty",
        "path",
        "symlink",
        "rehearsal_missing",
    ],
)
def test_renewed_candidate_rejects_stale_or_substituted_evidence(renewed_candidate, damage):
    repo, root, canonical = renewed_candidate
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if damage in {"source", "tree", "lock"}:
        manifest[{"lock": "lock_sha256"}.get(damage, damage)] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
    elif damage in {"wheel", "constraints"}:
        (root / ("dist/fixture.whl" if damage == "wheel" else "constraints.txt")).write_text(
            "tampered"
        )
    elif damage == "offline":
        (root / "offline.json").write_text("{}")
    elif damage == "canonical":
        (canonical / "ledger.sqlite").write_bytes(b"invalid")
    elif damage in {"archived", "approval", "path"}:
        path = root / "live.json"
        receipt = json.loads(path.read_text())
        if damage == "approval":
            receipt["budget"]["approval"] = budget.APPROVAL
        else:
            receipt["artifacts"][0]["sha256" if damage == "archived" else "path"] = (
                "0" * 64 if damage == "archived" else "../../../../pyproject.toml"
            )
        path.write_text(json.dumps(receipt))
    elif damage == "dirty":
        (repo / "README.md").write_text("changed")
    elif damage == "rehearsal_missing":
        (root / "rehearsal.json").unlink()
    else:
        alias = root.parent / "v13-PILOT-PREP-alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    with pytest.raises(CandidateRefused):
        release.verify_renewed_pilot(repo, root)


def test_renewed_candidate_cannot_ignore_failed_operational_reducer(renewed_candidate, monkeypatch):
    repo, root, _ = renewed_candidate

    def refused(*args, **kwargs):
        raise PilotRefused("rehearsal_is_not_live_acceptance")

    monkeypatch.setattr(release, "verify_saved_receipt", refused)
    with pytest.raises(CandidateRefused):
        release.verify_renewed_pilot(repo, root)


@pytest.fixture
def signing_candidate(candidate, monkeypatch):
    from mcp_server.artifacts.attestation import PREDICATE_TYPE

    repo, _ = candidate
    git(repo, "switch", "-qc", release.SIGNING_BRANCH)
    (repo / ".gitignore").write_text(".phase-loop/\n")
    workflow = repo / release.SIGNING_WORKFLOW
    workflow.write_bytes(
        (Path(release.__file__).resolve().parents[1] / release.SIGNING_WORKFLOW).read_bytes()
    )
    commit(repo)
    root = repo / ".phase-loop/runs/v13-PREP-signing-20260915"
    root.mkdir(parents=True)
    archive = root / "index.tar.gz"
    archive.write_bytes(b"synthetic checksum fixture, not an operational archive")
    metadata = {
        "repo_id": "synthetic",
        "tracked_branch": "main",
        "commit": "a" * 40,
        "schema_version": "2",
        "semantic_profile_hash": "lexical-only",
        "checksum": digest_file(archive),
        "compressed_size": archive.stat().st_size,
        "artifact_type": "full",
        "timestamp": "2026-09-17T00:00:00Z",
        "compatibility": {"schema_version": "2", "embedding_model": None},
    }
    from mcp_server.artifacts.artifact_upload import _metadata_bytes

    (root / "artifact-metadata.json").write_bytes(_metadata_bytes(metadata))
    (root / "artifact-metadata.json.attestation.jsonl").write_text("mock signature boundary")
    intent = release.claim_signing_dispatch(repo, root)
    dispatch = {
        "accepted": True,
        "dispatch_attempts": 1,
        "run_id": 123,
        "intent_sha256": digest_file(root / "dispatch-intent.json"),
    }
    (root / "dispatch.json").write_text(json.dumps(dispatch))
    monkeypatch.setenv("MCP_ATTESTATION_MODE", "enforce")
    monkeypatch.setenv("MCP_ATTESTATION_SOURCE_REF", intent["ref"])
    monkeypatch.setenv("MCP_ATTESTATION_SIGNER_DIGEST", intent["source"])
    run = {
        "id": 123,
        "run_attempt": 1,
        "head_sha": intent["source"],
        "head_branch": release.SIGNING_BRANCH,
        "path": release.SIGNING_WORKFLOW,
        "event": "workflow_dispatch",
        "repository": {"full_name": release.SIGNING_REPOSITORY},
        "status": "completed",
        "conclusion": "success",
    }
    jobs = {
        "jobs": [
            {
                "name": "Attest operator-supplied local index digest",
                "run_id": 123,
                "conclusion": "success",
                "started_at": "2026-09-17T00:00:00Z",
                "completed_at": "2026-09-17T00:00:07Z",
            }
        ]
    }
    verified = [
        {
            "verificationResult": {
                "signature": {
                    "certificate": {
                        "runInvocationURI": f"https://github.com/{release.SIGNING_REPOSITORY}/actions/runs/123/attempts/1"
                    }
                },
                "statement": {
                    "subject": [
                        {
                            "name": release.SIGNING_SUBJECT,
                            "digest": {"sha256": intent["subject_digest"]},
                        }
                    ],
                    "predicateType": PREDICATE_TYPE,
                    "predicate": {
                        "artifact_origin": "local",
                        "digest_origin": "operator-supplied",
                        "built_in_this_workflow": False,
                    },
                },
            }
        }
    ]
    calls = []
    state = {"verification_exit": 0, "dispatch_ids": [123]}
    original_run = subprocess.run

    def gh(args, **kwargs):
        if args[0] != "gh":
            return original_run(args, **kwargs)
        calls.append(args)
        if args[1] == "api":
            if "/actions/workflows/" in args[2]:
                assert "head_sha=" + intent["source"] in args
                assert "--paginate" in args and "--slurp" in args
                payload = [{"workflow_runs": [{"id": value} for value in state["dispatch_ids"]]}]
            else:
                payload = jobs if args[2].endswith("/jobs") else run
        else:
            assert args[:3] == ["gh", "attestation", "verify"]
            assert args[args.index("--source-ref") + 1] == intent["ref"]
            assert args[args.index("--signer-digest") + 1] == intent["source"]
            assert "--deny-self-hosted-runners" in args
            if state["verification_exit"]:
                return subprocess.CompletedProcess(args, state["verification_exit"], "", "")
            payload = verified
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", gh)
    return repo, root, run, jobs, verified, state, calls


def test_signing_proof_uses_production_verifier_and_never_dispatches(signing_candidate):
    repo, root, _, _, _, _, calls = signing_candidate
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    result = release.verify_signing_proof(repo, root)
    assert result["production_verifier"] == "passed"
    assert result["observed_candidate_dispatches"] == 1
    assert result["new_dispatches"] == 0
    assert len([args for args in calls if args[1] == "attestation"]) == 2
    assert not any("workflow" in args for args in calls)
    assert before == {path.name: path.read_bytes() for path in root.iterdir()}


@pytest.mark.parametrize("dispatch_ids", [[], [123, 124], [124]])
def test_signing_proof_rejects_missing_duplicate_or_other_dispatch(signing_candidate, dispatch_ids):
    repo, root, _, _, _, state, _ = signing_candidate
    state["dispatch_ids"] = dispatch_ids
    with pytest.raises(CandidateRefused, match="signing_dispatch_count_mismatch"):
        release.verify_signing_proof(repo, root)


def test_second_signing_claim_cannot_reset_or_replace_intent(signing_candidate):
    repo, root, *_ = signing_candidate
    before = (root / "dispatch-intent.json").read_bytes()
    with pytest.raises(CandidateRefused, match="already_claimed"):
        release.claim_signing_dispatch(repo, root)
    assert (root / "dispatch-intent.json").read_bytes() == before


@pytest.mark.parametrize("damage", ["archive", "size", "schema", "canonical", "replace"])
def test_signing_claim_checks_local_bytes_before_consuming_allowance(
    signing_candidate, monkeypatch, damage
):
    from mcp_server.artifacts import integrity_gate
    from mcp_server.artifacts.artifact_upload import _metadata_bytes

    repo, root, *_ = signing_candidate
    intent = root / "dispatch-intent.json"
    intent.unlink()
    path = root / "artifact-metadata.json"
    metadata = json.loads(path.read_bytes())
    if damage == "archive":
        (root / "index.tar.gz").write_bytes(b"changed archive")
    elif damage in {"size", "schema"}:
        metadata["compressed_size" if damage == "size" else "schema_version"] = 999999
        path.write_bytes(_metadata_bytes(metadata))
    elif damage == "canonical":
        path.write_text(json.dumps(metadata))
    else:
        validate = integrity_gate.validate_artifact_integrity

        def replaced(*args, **kwargs):
            result = validate(*args, **kwargs)
            replacement = root / "replacement.json"
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
            return result

        monkeypatch.setattr(integrity_gate, "validate_artifact_integrity", replaced)
    with pytest.raises(CandidateRefused):
        release.claim_signing_dispatch(repo, root)
    assert not intent.exists()


def test_renewed_hashes_bind_the_receipt_bytes_actually_validated(renewed_candidate, monkeypatch):
    import hashlib

    repo, root, _ = renewed_candidate
    path = root / "live.json"
    original = path.read_bytes()
    verify = release.verify_saved_receipt

    def replaced(*args, **kwargs):
        result = verify(*args, **kwargs)
        if args[2] == "live":
            replacement = root / "replacement.json"
            replacement.write_text('{"unvalidated": true}')
            replacement.replace(path)
        return result

    monkeypatch.setattr(release, "verify_saved_receipt", replaced)
    result = release.verify_renewed_pilot(repo, root)
    assert result["proof_sha256"]["live"] == hashlib.sha256(original).hexdigest()
    assert result["proof_sha256"]["live"] != digest_file(path)


def test_signing_claim_cli_does_not_dispatch_or_consume_pilot(monkeypatch, tmp_path, capsys):
    import sys

    monkeypatch.setattr(sys, "argv", ["validator", "--claim-signing-dispatch"])
    monkeypatch.setattr(release, "REPO", tmp_path)
    calls = []
    monkeypatch.setattr(
        release, "claim_signing_dispatch", lambda *args: calls.append(args) or {"claimed": True}
    )
    monkeypatch.setattr(
        release, "verify_pilot", lambda *args: pytest.fail("claim must not consume pilot")
    )
    release.main()
    assert json.loads(capsys.readouterr().out) == {"claimed": True}
    assert calls == [(tmp_path, tmp_path / ".phase-loop/runs/v13-PREP-signing-20260915")]


def test_renewed_cli_requires_signing_as_well_as_pilot(monkeypatch, tmp_path):
    import sys

    monkeypatch.setattr(sys, "argv", ["validator", "--renewed-pilot-root", str(tmp_path)])
    monkeypatch.setattr(release, "verify_pilot", lambda repo: {})
    monkeypatch.setattr(release, "verify_renewed_pilot", lambda repo, root: {})

    def missing_signing(repo, root):
        assert root == release.REPO / ".phase-loop/runs/v13-PREP-signing-20260915"
        raise CandidateRefused("signing_proof_missing")

    monkeypatch.setattr(release, "verify_signing_proof", missing_signing)
    with pytest.raises(SystemExit, match="signing_proof_missing"):
        release.main()


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "mode",
        "digest",
        "metadata",
        "archive",
        "failed",
        "ref",
        "source",
        "attempt",
        "second_dispatch",
        "verifier",
        "skip",
        "certificate_run",
        "subject",
        "predicate",
        "timeout",
        "image_job",
        "alternate_root",
    ],
)
def test_signing_acceptance_fails_closed(signing_candidate, monkeypatch, damage):
    repo, root, run, jobs, verified, state, _ = signing_candidate
    if damage == "missing":
        (root / "artifact-metadata.json.attestation.jsonl").unlink()
    elif damage in {"mode", "digest"}:
        path = root / "dispatch-intent.json"
        intent = json.loads(path.read_text())
        intent["mode" if damage == "mode" else "subject_digest"] = "wrong"
        path.write_text(json.dumps(intent))
    elif damage in {"metadata", "archive"}:
        (root / ("artifact-metadata.json" if damage == "metadata" else "index.tar.gz")).write_text(
            "tampered"
        )
    elif damage in {"failed", "ref", "source", "attempt"}:
        key, value = {
            "failed": ("conclusion", "failure"),
            "ref": ("head_branch", "main"),
            "source": ("head_sha", "0" * 40),
            "attempt": ("run_attempt", 2),
        }[damage]
        run[key] = value
    elif damage == "second_dispatch":
        path = root / "dispatch.json"
        dispatch = json.loads(path.read_text())
        dispatch["dispatch_attempts"] = 2
        path.write_text(json.dumps(dispatch))
    elif damage == "verifier":
        state["verification_exit"] = 1
    elif damage == "skip":
        monkeypatch.setenv("MCP_ATTESTATION_MODE", "skip")
    elif damage == "certificate_run":
        verified[0]["verificationResult"]["signature"]["certificate"][
            "runInvocationURI"
        ] = "https://example.invalid/other"
    elif damage == "subject":
        verified[0]["verificationResult"]["statement"]["subject"][0]["name"] = "index.tar.gz"
    elif damage == "predicate":
        verified[0]["verificationResult"]["statement"]["predicate"]["built_in_this_workflow"] = True
    elif damage == "timeout":
        jobs["jobs"][0]["completed_at"] = "2026-09-17T00:05:01Z"
    elif damage == "image_job":
        jobs["jobs"].append(
            {"name": "Sign an already-published container image", "conclusion": "success"}
        )
    else:
        root = root.parent / "v13-PREP-signing-second"
    with pytest.raises(CandidateRefused):
        release.verify_signing_proof(repo, root)
