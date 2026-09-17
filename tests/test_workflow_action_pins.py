"""Supply-chain policy for external GitHub Actions references."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
IMMUTABLE_REF = re.compile(r"^[0-9a-f]{40}$")
USES_LINE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?P<action>[^\s@]+)@(?P<ref>[^\s#]+)\s+#\s+(?P<version>\S+)\s*$"
)
WORKFLOW_CENSUS = {
    "ci-cd-pipeline.yml": ({"pull_request", "push", "workflow_dispatch"}, 7),
    "container-registry.yml": ({"pull_request", "push", "workflow_dispatch"}, 4),
    "index-artifact-management.yml": ({"workflow_call", "workflow_dispatch"}, 2),
    "index-management.yml": ({"workflow_dispatch"}, 1),
    "lockfile-check.yml": ({"workflow_dispatch"}, 1),
    "maintenance.yml": ({"pull_request", "push"}, 1),
    "mcp-index.yml": ({"workflow_dispatch"}, 2),
    "release-automation.yml": ({"workflow_dispatch"}, 9),
    "sign-published-image.yml": ({"workflow_dispatch"}, 2),
}


def _external_uses(value: object) -> bool:
    return isinstance(value, str) and not value.startswith("./") and "/" in value


def _walk_steps(value: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(value, dict):
        if _external_uses(value.get("uses")):
            found.append(value)
        for child in value.values():
            found.extend(_walk_steps(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_steps(child))
    return found


def test_every_external_action_uses_an_immutable_commit() -> None:
    offenders: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        for step in _walk_steps(workflow):
            action, ref = str(step["uses"]).rsplit("@", 1)
            if not IMMUTABLE_REF.fullmatch(ref):
                offenders.append(f"{path.name}: {action}@{ref}")

    assert offenders == []


def test_every_external_action_pin_has_a_readable_version_comment() -> None:
    offenders: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "uses:" not in line or line.lstrip().startswith("#"):
                continue
            match = USES_LINE.match(line)
            if match is None or not IMMUTABLE_REF.fullmatch(match.group("ref")):
                offenders.append(f"{path.name}:{line_number}: {line.strip()}")

    assert offenders == []


def test_no_workflow_uses_a_mutable_trivy_ref() -> None:
    refs: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        for step in _walk_steps(workflow):
            action, ref = str(step["uses"]).rsplit("@", 1)
            if action == "aquasecurity/trivy-action":
                refs.append(ref)

    assert refs
    assert all(IMMUTABLE_REF.fullmatch(ref) for ref in refs)


def test_workflow_pinning_does_not_expand_triggers_or_hosted_jobs() -> None:
    observed: dict[str, tuple[set[str], int]] = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        observed[path.name] = (set(workflow["on"]), len(workflow["jobs"]))

    assert observed == WORKFLOW_CENSUS
