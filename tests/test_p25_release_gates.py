"""P25 release gate workflow contract tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).parent.parent

REQUIRED_GATES = {
    "Protected Evidence - Agent Gate",
    "Protected Evidence - Agent Gate Passed",
    "Alpha Gate - Docker Build And Smoke",
}


def _read(relative_path: str) -> str:
    return (REPO / relative_path).read_text(encoding="utf-8")


def _load_workflow(relative_path: str) -> dict[str, Any]:
    with (REPO / relative_path).open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=yaml.BaseLoader)


def _jobs(relative_path: str) -> dict[str, Any]:
    return _load_workflow(relative_path)["jobs"]


def _job_names(*workflow_paths: str) -> set[str]:
    names: set[str] = set()
    for workflow_path in workflow_paths:
        for job in _jobs(workflow_path).values():
            names.add(job["name"])
    return names


def _needs(job: dict[str, Any]) -> set[str]:
    raw = job.get("needs", [])
    if isinstance(raw, str):
        return {raw}
    return set(raw)


def _is_true(value: Any) -> bool:
    return str(value).lower() == "true"


def test_required_localci_gate_names_are_exposed():
    names = _job_names(
        ".github/workflows/ci-cd-pipeline.yml",
        ".github/workflows/container-registry.yml",
    )

    assert REQUIRED_GATES <= names


def test_main_ci_required_aggregator_depends_on_agent_gate():
    jobs = _jobs(".github/workflows/ci-cd-pipeline.yml")
    aggregator = jobs["agent-gate-passed"]

    assert aggregator["name"] == "Protected Evidence - Agent Gate Passed"
    assert _needs(aggregator) == {"agent-gate"}
    assert "continue-on-error" not in aggregator

    gate = jobs["agent-gate"]
    assert gate["name"] == "Protected Evidence - Agent Gate"
    assert "continue-on-error" not in gate


def test_informational_jobs_are_named_and_excluded_from_required_aggregator():
    for workflow_path in (
        ".github/workflows/ci-cd-pipeline.yml",
        ".github/workflows/container-registry.yml",
    ):
        jobs = _jobs(workflow_path)
        aggregator_needs = _needs(jobs.get("agent-gate-passed", {}))
        for job_id, job in jobs.items():
            if job["name"].startswith("Informational - "):
                assert job_id not in aggregator_needs
                assert _is_true(job.get("continue-on-error")) or (
                    "workflow_dispatch" in str(job.get("if", ""))
                    or "refs/tags/v" in str(job.get("if", ""))
                )
            elif "continue-on-error" in job:
                assert job["name"].startswith("Informational - ")


def test_container_alpha_gate_builds_and_smokes_without_push():
    jobs = _jobs(".github/workflows/container-registry.yml")
    gate = jobs["alpha-docker-build-smoke"]
    workflow_text = _read(".github/workflows/container-registry.yml")

    assert gate["name"] == "Alpha Gate - Docker Build And Smoke"
    assert "continue-on-error" not in gate
    assert "make release-smoke-container" in workflow_text
    assert "push: false" in workflow_text


def test_release_automation_refuses_before_mutating_or_publishing():
    jobs = _jobs(".github/workflows/release-automation.yml")
    workflow_text = _read(".github/workflows/release-automation.yml")

    assert jobs["validate-dispatch"]["name"] == "Validate release dispatch"
    assert jobs["prepare-release-pr"]["name"] == "Validate release preparation"
    assert jobs["create-release-pr"]["name"] == "Create release pull request"
    assert jobs["preflight-publish"]["name"] == "Verify protected main and run release gates"
    assert _needs(jobs["prepare-release-pr"]) == {"validate-dispatch"}
    assert _needs(jobs["create-release-pr"]) == {"prepare-release-pr"}
    assert _needs(jobs["preflight-publish"]) == {"validate-dispatch"}
    assert _needs(jobs["build-release"]) == {"preflight-publish"}
    assert _needs(jobs["publish-release"]) == {"build-release"}

    for job_id in ("preflight-publish", "build-release", "publish-release"):
        condition = jobs[job_id]["if"]
        assert "inputs.mode == 'publish'" in condition
        assert "github.ref == 'refs/heads/main'" in condition

    preflight = workflow_text.split("preflight-publish:", 1)[1].split("build-release:", 1)[0]
    assert preflight.index("make agent-gate") < preflight.index("make release-smoke")
    assert preflight.index("make release-smoke") < preflight.index("make release-smoke-container")

    prepare = yaml.safe_dump(
        {"prepare": jobs["prepare-release-pr"], "create": jobs["create-release-pr"]}
    )
    assert "docker/build-push-action" not in prepare
    assert "action-gh-release" not in prepare
    assert "gh-action-pypi-publish" not in prepare
    assert "release_type" not in workflow_text


def test_release_automation_marks_prerelease_and_keeps_latest_stable_only():
    workflow_text = _read(".github/workflows/release-automation.yml")

    assert "prerelease: ${{ contains(inputs.version, '-') }}" in workflow_text
    promotion = yaml.safe_load(workflow_text)["jobs"]["promote-container"]
    assert "publish-release" in promotion["needs"]
    step = next(
        step for step in promotion["steps"] if step["name"] == "Promote stable container tags"
    )
    assert 'tags=(--tag "${IMAGE_REF}:${RELEASE_VERSION}")' in step["run"]
    assert 'if [[ "$RELEASE_VERSION" != *-* ]]; then' in step["run"]
    assert 'tags+=(--tag "${IMAGE_REF}:latest")' in step["run"]
    assert 'owner="${GITHUB_REPOSITORY_OWNER,,}"' in workflow_text
    assert 'find docs -name "*.md" -exec sed -i' not in workflow_text


def test_attestation_private_alpha_fallback_is_explicit():
    surfaces = "\n".join(
        [
            _read(".github/workflows/ci-cd-pipeline.yml"),
            _read("docs/operations/user-action-runbook.md"),
        ]
    )

    assert "ATTESTATION_GH_TOKEN" in surfaces
    assert "private-alpha" in surfaces
    assert "informational skipped/warn" in surfaces
