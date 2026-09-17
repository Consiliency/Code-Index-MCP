"""Release workflow topology and protected-main policy tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
WORKFLOW_PATH = REPO / ".github" / "workflows" / "release-automation.yml"
PUBLISH_JOBS = (
    "preflight-publish",
    "claim-release",
    "build-release",
    "publish-release",
    "verify-container",
)


def _workflow() -> dict[str, object]:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _jobs() -> dict[str, dict[str, object]]:
    return _workflow()["jobs"]  # type: ignore[return-value]


def test_release_modes_keep_prepare_separate_from_publish() -> None:
    workflow = _workflow()
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]  # type: ignore[index]
    jobs = _jobs()

    assert inputs["mode"]["options"] == ["prepare", "publish"]
    assert inputs["mode"]["default"] == "prepare"
    assert jobs["prepare-release-pr"]["if"] == "inputs.mode == 'prepare'"
    assert jobs["create-release-pr"]["if"] == "inputs.mode == 'prepare'"
    assert jobs["create-release-pr"]["needs"] == "prepare-release-pr"
    assert "publish-release" not in jobs["prepare-release-pr"].get("needs", [])
    for name in PUBLISH_JOBS:
        assert "inputs.mode == 'publish'" in jobs[name]["if"]
        assert "github.ref == 'refs/heads/main'" in jobs[name]["if"]
        assert "prepare-release-pr" not in jobs[name].get("needs", [])


def test_publish_jobs_checkout_exact_main_with_full_history() -> None:
    for name in PUBLISH_JOBS:
        steps = _jobs()[name]["steps"]
        checkout = next(
            step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        assert checkout["with"]["fetch-depth"] == "0"
        assert checkout["with"]["ref"] == "${{ github.sha }}"


def test_every_release_mutation_has_an_adjacent_protected_main_guard() -> None:
    steps = _jobs()["publish-release"]["steps"]
    mutation_names = (
        "Create and push tag",
        "Create GitHub release",
        "Publish to PyPI",
    )
    build_steps = _jobs()["build-release"]["steps"]
    mutation_steps = [(build_steps, "Build and push container images")]
    mutation_steps.extend((steps, name) for name in mutation_names)

    for job_steps, mutation_name in mutation_steps:
        index = next(i for i, step in enumerate(job_steps) if step.get("name") == mutation_name)
        guard = job_steps[index - 1]
        assert guard["name"] == f"Guard protected main before {mutation_name.lower()}"
        script = guard["run"]
        assert "git merge-base --is-ancestor" in script
        assert 'test "$(git rev-parse HEAD)" = "${{ github.sha }}"' in script
        assert 'grep -Fxq "version = \\"$VERSION_NO_V\\"" pyproject.toml' in script
        assert 'grep -Fxq "__version__ = \\"$VERSION_NO_V\\"" mcp_server/__init__.py' in script


def _is_external_release_mutation(step: dict[str, object]) -> bool:
    uses = str(step.get("uses", ""))
    run = str(step.get("run", ""))
    push = (
        str(step.get("with", {}).get("push", "")).lower()
        if isinstance(step.get("with"), dict)
        else ""
    )
    return (
        ("docker/build-push-action@" in uses and push == "true")
        or "softprops/action-gh-release@" in uses
        or "gh release create" in run
        or "pypa/gh-action-pypi-publish@" in uses
        or "actions/delete-package-versions@" in uses
        or "cosign sign" in run
        or "docker buildx imagetools create" in run
        or step.get("name") == "Create and push tag"
        or step.get("name") == "Claim release attempt"
    )


def test_every_workflow_release_mutation_has_an_adjacent_protected_main_guard() -> None:
    mutations: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        for job_name, job in workflow.get("jobs", {}).items():
            steps = job.get("steps", [])
            for index, step in enumerate(steps):
                if not _is_external_release_mutation(step):
                    continue
                mutations.append(f"{path.name}:{job_name}:{step.get('name')}")
                assert index > 0
                guard = steps[index - 1]
                assert str(guard.get("name", "")).startswith("Guard protected main before")
                script = str(guard.get("run", ""))
                assert "git merge-base --is-ancestor" in script
                assert 'test "$(git rev-parse HEAD)" = "${{ github.sha }}"' in script
                assert 'grep -Fxq "version = \\"$VERSION_NO_V\\"" pyproject.toml' in script
                assert (
                    'grep -Fxq "__version__ = \\"$VERSION_NO_V\\"" mcp_server/__init__.py' in script
                )

                checkouts = [
                    candidate
                    for candidate in steps[:index]
                    if str(candidate.get("uses", "")).startswith("actions/checkout@")
                ]
                assert checkouts
                assert checkouts[-1].get("with", {}).get("fetch-depth") == "0"
                assert checkouts[-1].get("with", {}).get("ref") == "${{ github.sha }}"

    assert mutations


def test_package_version_deletion_is_classified_as_an_external_mutation() -> None:
    assert _is_external_release_mutation(
        {"uses": "actions/delete-package-versions@pinned", "name": "Delete versions"}
    )


def test_prepare_execution_is_read_only_and_pr_mutation_is_isolated() -> None:
    prepare = _jobs()["prepare-release-pr"]
    create_pr = _jobs()["create-release-pr"]
    auto_merge_job = _jobs()["enable-release-auto-merge"]
    text = yaml.safe_dump(
        {"prepare": prepare, "create_pr": create_pr, "auto_merge": auto_merge_job}
    )

    for forbidden in ("docker/build-push-action", "action-gh-release", "gh-action-pypi-publish"):
        assert forbidden not in text
    assert "git tag" not in text
    assert "gh release" not in text
    assert "gh workflow run" not in text

    assert prepare["permissions"] == {"contents": "read"}
    assert create_pr["permissions"] == {"contents": "read", "pull-requests": "write"}
    assert not any(
        str(step.get("uses", "")).startswith("actions/checkout@") for step in create_pr["steps"]
    )
    assert create_pr["steps"][0]["env"]["GH_REPO"] == "${{ github.repository }}"
    assert auto_merge_job["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
    }
    assert auto_merge_job["if"] == "inputs.mode == 'prepare' && inputs.auto_merge == 'true'"
    assert not any(
        str(step.get("uses", "")).startswith("actions/checkout@")
        for step in auto_merge_job["steps"]
    )
    assert auto_merge_job["steps"][0]["env"]["GH_REPO"] == "${{ github.repository }}"


def test_publish_permissions_are_job_scoped() -> None:
    workflow = _workflow()
    jobs = _jobs()

    assert workflow["permissions"] == {"contents": "read"}
    assert jobs["preflight-publish"]["permissions"] == {
        "contents": "read",
        "packages": "read",
    }
    assert jobs["build-release"]["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
    }
    assert jobs["publish-release"]["permissions"] == {
        "contents": "write",
        "id-token": "write",
    }
    assert jobs["verify-container"]["permissions"] == {"contents": "read", "packages": "read"}


def test_container_release_uses_signed_digest_without_mutable_tag_promotion():
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    assert "'publish'" in workflow["concurrency"]["group"]
    promote = jobs["verify-container"]
    assert promote["needs"] == ["build-release", "publish-release"]
    assert promote["env"]["IMAGE_DIGEST"] == "${{ needs.build-release.outputs.image_digest }}"
    build = jobs["build-release"]
    build_step = next(step for step in build["steps"] if step.get("id") == "build-and-push")
    assert "latest" not in build_step["with"]["tags"]
    assert "inputs.version" not in build_step["with"]["tags"]
    assert "github.run_id" in build_step["with"]["tags"]
    assert "github.run_attempt" in build_step["with"]["tags"]
    verify = next(
        step
        for step in promote["steps"]
        if step["name"] == "Verify released digest and source signature"
    )
    assert "cosign verify" in verify["run"]
    assert (
        '--certificate-identity "https://github.com/${GITHUB_REPOSITORY}/.github/workflows/release-automation.yml@refs/heads/main"'
        in verify["run"]
    )
    assert '--certificate-github-workflow-sha "${{ github.sha }}"' in verify["run"]
    mutation = promote["steps"][-1]["run"]
    assert '"${IMAGE_REF}@${IMAGE_DIGEST}"' in mutation
    assert "imagetools create" not in WORKFLOW_PATH.read_text()
    assert "--tag" not in mutation
    assert 'test "$actual" = "$IMAGE_DIGEST"' in mutation
    release = next(
        step for step in jobs["publish-release"]["steps"] if step["name"] == "Create GitHub release"
    )
    assert "image-reference.txt" in release["run"]
    assert "image-digest.txt" in release["run"]
    assert "gh release create" in release["run"]
    assert "--verify-tag" in release["run"]
    assert "--clobber" not in release["run"]
    create_line = next(
        line for line in release["run"].splitlines() if line.startswith("gh release create")
    )
    assert create_line == 'gh release create "$RELEASE_VERSION" "${flags[@]}"'
    assert "gh release upload" in release["run"]
    assert "gh release delete" not in release["run"]


def test_concurrent_version_tag_is_never_overwritten(tmp_path):
    script = _jobs()["verify-container"]["steps"][-1]["run"]
    digest = "sha256:" + "a" * 64
    competitor = "sha256:" + "b" * 64
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/bash\nset -eu\n"
        'test "$1 $2 $3" = "buildx imagetools inspect"\n'
        'test "$4" = "$IMAGE_REF@$IMAGE_DIGEST"\n'
        f"printf '%s' '{competitor}' > version-tag\n"
        f"printf '%s' '{{\"digest\":\"{digest}\"}}'\n"
    )
    docker.chmod(0o700)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "IMAGE_REF": "ghcr.io/fixture/image",
            "IMAGE_DIGEST": digest,
        },
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "version-tag").read_text() == competitor


def test_release_workflow_does_not_dispatch_downstream_workflows() -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "gh workflow run" not in text
    assert "workflow_call:" not in text
    assert "workflow_run:" not in text


@pytest.mark.parametrize("damage", [None, "advance", "tree", "retry", "missing"])
def test_publication_identity_is_checked_before_any_mutation(tmp_path, damage):
    step = _jobs()["validate-dispatch"]["steps"][0]
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.defpath,
        "MODE": "publish",
        "RELEASE_VERSION": "v1.4.1",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": "a" * 40,
        "EXPECTED_COMMIT": "a" * 40,
        "EXPECTED_TREE": "b" * 40,
        "GITHUB_REPOSITORY": "fixture/repo",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    if damage == "advance":
        env["GITHUB_SHA"] = "c" * 40
    elif damage == "tree":
        env["EXPECTED_TREE"] = "c" * 40
    elif damage == "retry":
        env["GITHUB_RUN_ATTEMPT"] = "2"
    elif damage == "missing":
        env["EXPECTED_COMMIT"] = ""
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nprintf '%s\\n' '" + "b" * 40 + "'\n")
    gh.chmod(0o700)
    result = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, timeout=5)
    assert (result.returncode == 0) is (damage is None)
    assert _jobs()["preflight-publish"]["needs"] == "validate-dispatch"


def test_release_claim_is_create_only_and_precedes_build_and_signing():
    jobs = _jobs()
    claim = jobs["claim-release"]
    assert claim["permissions"] == {"contents": "write"}
    assert claim["needs"] == "preflight-publish"
    assert jobs["build-release"]["needs"] == "claim-release"
    script = claim["steps"][-1]["run"]
    assert 'CLAIM="release-claims/$RELEASE_VERSION"' in script
    assert '"repos/$GITHUB_REPOSITORY/git/refs"' in script
    assert "--method POST" in script
    assert '-f ref="refs/tags/$CLAIM"' in script
    assert "--method PATCH" not in script and "--method DELETE" not in script
    assert 'test "$GITHUB_RUN_ATTEMPT" = "1"' in script
    assert "set -euo pipefail" in script


@pytest.mark.parametrize("job", PUBLISH_JOBS)
@pytest.mark.parametrize("attempt", ["1", "2", "", "invalid"])
def test_each_publish_job_rejects_reruns_before_any_other_step(job, attempt):
    step = _jobs()[job]["steps"][0]
    assert step["name"] == "Refuse publication retries"
    assert "uses" not in step and "env" not in step
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        env={"PATH": os.defpath, "GITHUB_RUN_ATTEMPT": attempt},
        capture_output=True,
        timeout=5,
    )
    assert (result.returncode == 0) is (attempt == "1")


def test_duplicate_release_claim_fails_without_replacing_first_owner(tmp_path):
    script = _jobs()["claim-release"]["steps"][-1]["run"]
    git = tmp_path / "git"
    git.write_text("#!/bin/sh\nprintf '%s\\n' '" + "b" * 40 + "'\n")
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\nfrom pathlib import Path\n"
        "assert sys.argv[1:3] == ['api', '--method'] and sys.argv[3] == 'POST'\n"
        "if sys.argv[4].endswith('/git/tags'):\n    print('c' * 40)\n"
        "elif sys.argv[4].endswith('/git/refs'):\n"
        "    try:\n        with Path('claim').open('x') as out: out.write(os.environ['GITHUB_RUN_ID'])\n"
        "    except FileExistsError:\n        sys.exit(1)\n"
        "else:\n    sys.exit(2)\n"
    )
    for path in (git, gh):
        path.chmod(0o700)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.defpath,
        "GITHUB_SHA": "a" * 40,
        "EXPECTED_COMMIT": "a" * 40,
        "EXPECTED_TREE": "b" * 40,
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REPOSITORY": "fixture/repo",
        "RELEASE_VERSION": "v1.4.1",
        "GITHUB_RUN_ID": "first",
    }
    first = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, timeout=5
    )
    second = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={**env, "GITHUB_RUN_ID": "second"},
        capture_output=True,
        timeout=5,
    )
    assert first.returncode == 0 and second.returncode != 0
    assert (tmp_path / "claim").read_text() == "first"


def test_manual_index_signer_only_receives_digest_with_five_minute_cap():
    workflow = yaml.load(
        (WORKFLOWS / "sign-published-image.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert set(workflow["on"]) == {"workflow_dispatch"}
    jobs = workflow["jobs"]
    signer = jobs["attest-local-index"]
    assert signer["if"] == "inputs.mode == 'index-attestation'"
    assert signer["timeout-minutes"] == "5"
    assert signer["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }
    steps = signer["steps"]
    assert len(steps) == 2
    assert "^[0-9a-f]{64}$" in steps[0]["run"]
    assert steps[0]["env"] == {"SUBJECT_DIGEST": "${{ inputs.subject_digest }}"}
    action = steps[1]
    assert action["uses"].startswith("actions/attest@")
    assert "subject-path" not in action["with"]
    assert action["with"]["subject-digest"] == "sha256:${{ inputs.subject_digest }}"
    assert action["with"]["create-storage-record"] == "false"
    assert action["with"]["push-to-registry"] == "false"
    import json

    assert json.loads(action["with"]["predicate"]) == {
        "artifact_origin": "local",
        "digest_origin": "operator-supplied",
        "built_in_this_workflow": False,
    }
    for name, job in jobs.items():
        if name != "attest-local-index":
            assert "inputs.mode == 'image'" in job["if"]
            assert "github.ref == 'refs/heads/main'" in job["if"]
