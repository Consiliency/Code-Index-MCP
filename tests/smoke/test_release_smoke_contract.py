"""P22 release smoke contract tests."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import tomllib
except ImportError:  # Python <3.11
    import tomli as tomllib


REPO = Path(__file__).resolve().parents[2]
GHCR_IMAGE = "ghcr.io/consiliency/code-index-mcp"
LEGACY_IMAGE_NAMESPACE = "viperjuice/code-index-mcp"
HELPER_PATHS = (
    "scripts/install-mcp-docker.sh",
    "scripts/install-mcp-docker.ps1",
    "scripts/setup-mcp-json.sh",
    "scripts/setup-mcp-json.ps1",
    "scripts/build-images.sh",
)
# Static doc/helper surfaces name the current owner-namespaced image directly.
STATIC_IMAGE_SURFACES = (
    "README.md",
    "docker/README.md",
    *HELPER_PATHS,
)
# Every workflow must be free of a hardcoded image namespace.
WORKFLOW_SURFACES = (
    ".github/workflows/ci-cd-pipeline.yml",
    ".github/workflows/release-automation.yml",
    ".github/workflows/container-registry.yml",
)
# Workflows that build, push, or sign the image must derive the namespace from
# the repository owner at runtime so an ownership transfer follows automatically.
OWNER_DERIVED_WORKFLOWS = (
    ".github/workflows/container-registry.yml",
    ".github/workflows/release-automation.yml",
)

# Every job that builds/pushes/signs an image must carry its OWN owner-derivation
# step (GITHUB_ENV is job-scoped), so assert the derive-step count per workflow —
# a file-level "present at least once" check would still pass if only one of the
# container jobs derived while the others silently resolved an empty namespace.
OWNER_DERIVED_STEP_COUNTS = {
    ".github/workflows/container-registry.yml": 3,  # alpha-build, push-manifests, sign
    ".github/workflows/release-automation.yml": 2,  # build-release, promote-container
}
_DERIVE_STEP = 'owner="${GITHUB_REPOSITORY_OWNER,,}"'


def _read(relative_path: str) -> str:
    return (REPO / relative_path).read_text(encoding="utf-8")


def test_release_smoke_entrypoints_exist():
    script = REPO / "scripts/release_smoke.py"
    assert script.exists(), "scripts/release_smoke.py is required"
    text = script.read_text(encoding="utf-8") + _read("scripts/installed_runtime_smoke.py")
    for mode in ("--wheel", "--stdio", "--container", "--all"):
        assert mode in text
    for contract in ("get_status", "index_unavailable", "safe_fallback", "native_search"):
        assert contract in text
    assert "unregistered_repository" in text
    for entrypoint in ('"mcp-index"', '"index-it-mcp"'):
        assert entrypoint in text
    assert '"code-index-mcp"' in text
    assert "boot_test_server" not in text
    assert "sys.path.insert" not in text
    assert "ClientSession" in text
    assert "site-packages" in text
    assert 'docker", "restart' in text

    makefile = _read("Makefile")
    assert re.search(r"^release-smoke:", makefile, re.MULTILINE)
    assert re.search(r"^release-smoke-container:", makefile, re.MULTILINE)
    assert "scripts/release_smoke.py --wheel --stdio" in makefile
    assert "scripts/release_smoke.py --container" in makefile


def test_delivered_wheel_never_builds_and_checks_registry_digest(tmp_path, monkeypatch):
    from scripts import release_smoke as smoke

    wheel = tmp_path / "index_it_mcp-1.4.1-py3-none-any.whl"
    wheel.write_bytes(b"registry fixture")
    calls = []
    monkeypatch.setattr(smoke, "_run", lambda command, **kwargs: calls.append(command))

    def reached_install(_):
        raise RuntimeError("reached install without build")

    monkeypatch.setattr(
        smoke.venv, "EnvBuilder", lambda **kwargs: SimpleNamespace(create=reached_install)
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        smoke.smoke_wheel(wheel, "0" * 64)
    assert calls == []
    with pytest.raises(RuntimeError, match="reached install"):
        smoke.smoke_wheel(wheel, hashlib.sha256(wheel.read_bytes()).hexdigest())
    assert calls and all(command[:2] != ["uv", "build"] for command in calls)


def test_delivered_image_never_builds_and_requires_immutable_reference(tmp_path, monkeypatch):
    from scripts import release_smoke as smoke

    image = GHCR_IMAGE + "@sha256:" + "a" * 64
    calls = []
    monkeypatch.setattr(smoke.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        smoke.subprocess,
        "check_output",
        lambda command, **kwargs: (
            json.dumps([image]) if "{{json .RepoDigests}}" in command else "sha256:" + "b" * 64
        ),
    )

    def run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["docker", "run"]:
            raise RuntimeError("reached delivered runtime")

    monkeypatch.setattr(smoke, "_run", run)
    with pytest.raises(ValueError, match="immutable"):
        smoke.smoke_container(GHCR_IMAGE + ":latest")
    assert not calls
    with pytest.raises(RuntimeError, match="delivered runtime"):
        smoke.smoke_container(image)
    assert calls[0] == ["docker", "pull", image]
    assert all(command[:2] != ["docker", "build"] for command in calls)


def test_delivered_smoke_cli_cannot_fall_back_to_partial_build(monkeypatch):
    from scripts import release_smoke as smoke

    monkeypatch.setattr(
        smoke.sys, "argv", ["smoke", "--all", "--image-ref", GHCR_IMAGE + "@sha256:" + "a" * 64]
    )
    with pytest.raises(SystemExit, match="cannot mix"):
        smoke.main()


def test_pyproject_has_console_script_and_build_dependency():
    with (REPO / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)

    scripts = data["project"]["scripts"]
    assert scripts["mcp-index"] == "mcp_server.cli:cli"
    assert scripts["index-it-mcp"] == "mcp_server.cli:cli"
    assert "code-index-mcp" not in scripts

    dev_deps = data["project"]["optional-dependencies"]["dev"]
    assert any(dep.startswith("build>=") for dep in dev_deps)
    assert "storage/migrations/*.sql" in data["tool"]["setuptools"]["package-data"]["mcp_server"]


def test_workflows_reuse_shared_release_smoke_commands():
    workflows = {
        path.name: path.read_text(encoding="utf-8")
        for path in (REPO / ".github/workflows").glob("*.yml")
    }

    assert "make agent-gate" in workflows["ci-cd-pipeline.yml"]
    assert "make release-smoke" in workflows["release-automation.yml"]
    assert "make release-smoke-container" in workflows["container-registry.yml"]
    assert "pip install build wheel" not in workflows["release-automation.yml"]


def test_alpha_docs_truth_and_release_preflight_cover_p34_and_container_smoke():
    makefile = _read("Makefile")
    release_automation = _read(".github/workflows/release-automation.yml")

    alpha_docs_truth = makefile.split("alpha-docs-truth:", 1)[1].split(
        "alpha-production-matrix:", 1
    )[0]
    assert "tests/docs/test_p34_public_alpha_recut.py" in alpha_docs_truth

    publish_preflight = release_automation.split("preflight-publish:", 1)[1].split(
        "build-release:", 1
    )[0]
    assert "make agent-gate" in publish_preflight
    assert "make release-smoke" in publish_preflight
    assert "make release-smoke-container" in publish_preflight
    assert publish_preflight.index("make agent-gate") < publish_preflight.index(
        "make release-smoke"
    )
    assert publish_preflight.index("make release-smoke") < publish_preflight.index(
        "make release-smoke-container"
    )


def test_ghcr_image_namespace_is_owner_derived_across_release_surfaces():
    stale_patterns = (
        "ghcr.io/code-index-mcp/mcp-index",
        "IMAGE_NAME: ${{ github.repository }}",
        "DOCKER_IMAGE_NAME: ${{ github.repository }}",
    )
    offenders: dict[str, list[str]] = {}

    def note(path: str, problem: str) -> None:
        offenders.setdefault(path, []).append(problem)

    # No surface may carry the frozen legacy namespace or a stale image variant.
    for relative_path in (*STATIC_IMAGE_SURFACES, *WORKFLOW_SURFACES):
        text = _read(relative_path)
        for pattern in stale_patterns:
            if pattern in text:
                note(relative_path, f"stale pattern {pattern}")
        if LEGACY_IMAGE_NAMESPACE in text:
            note(relative_path, f"legacy namespace {LEGACY_IMAGE_NAMESPACE}")

    # Docs must name the current owner-namespaced image directly.
    for relative_path in ("README.md", "docker/README.md"):
        if GHCR_IMAGE not in _read(relative_path):
            note(relative_path, f"missing {GHCR_IMAGE}")

    # Workflows must not hardcode the current namespace either -- they derive it.
    for relative_path in WORKFLOW_SURFACES:
        if GHCR_IMAGE in _read(relative_path):
            note(relative_path, "hardcoded current namespace; expected owner derivation")

    # Build/push/sign workflows must derive the namespace from the repo owner --
    # once per image-consuming job, not merely once per file.
    for relative_path in OWNER_DERIVED_WORKFLOWS:
        text = _read(relative_path)
        derive_count = text.count(_DERIVE_STEP)
        expected = OWNER_DERIVED_STEP_COUNTS.get(relative_path)
        if expected is not None:
            if derive_count != expected:
                note(
                    relative_path,
                    f"expected {expected} owner-derivation steps "
                    f"(one per image-consuming job), found {derive_count}",
                )
        elif derive_count == 0:
            note(relative_path, "missing owner-derivation step")
        if "${{ env.IMAGE_REF }}" not in text:
            note(relative_path, "image tags not derived from env.IMAGE_REF")

    assert offenders == {}


def test_docs_and_helpers_do_not_reference_retired_image_variants():
    stale = {}
    for relative_path in ("README.md", "docker/README.md", *HELPER_PATHS):
        text = _read(relative_path)
        hits = re.findall(r"mcp-index:(?:minimal|standard)", text)
        if hits:
            stale[relative_path] = hits

    assert stale == {}
