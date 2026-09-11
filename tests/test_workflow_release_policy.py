"""Release workflow topology and protected-main policy tests."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
WORKFLOW_PATH = REPO / ".github" / "workflows" / "release-automation.yml"
PUBLISH_JOBS = ("preflight-publish", "build-release", "publish-release")


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
        or "pypa/gh-action-pypi-publish@" in uses
        or "actions/delete-package-versions@" in uses
        or "cosign sign" in run
        or step.get("name") == "Create and push tag"
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


def test_release_workflow_does_not_dispatch_downstream_workflows() -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "gh workflow run" not in text
    assert "workflow_call:" not in text
    assert "workflow_run:" not in text


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
