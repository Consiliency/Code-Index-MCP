"""Release metadata assertions for the prepared v1.4.1 contract.

Historical GARC soak target: v1.2.0-rc6.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

try:
    import tomllib
except ImportError:  # Python <3.11
    import tomli as tomllib


REPO = Path(__file__).parent.parent
EXPECTED_VERSION = "1.4.1"
EXPECTED_TAG = "v1.4.1"
DOCKER_INSTALLERS = (
    "scripts/install-mcp-docker.sh",
    "scripts/install-mcp-docker.ps1",
)


@pytest.mark.parametrize(
    "case",
    ["version", "latest", "local", "missing", "malformed", "wrong_namespace", "invalid_selector"],
)
def test_docker_installer_pins_release_digest_without_tag_fallback(tmp_path, case):
    image = "ghcr.io/consiliency/code-index-mcp@sha256:" + "a" * 64
    response = image
    if case == "malformed":
        response = "ghcr.io/consiliency/code-index-mcp:latest"
    elif case == "wrong_namespace":
        response = "ghcr.io/another/image@sha256:" + "a" * 64
    selector = {"latest": "latest", "local": "local-smoke", "invalid_selector": "../wrong"}.get(
        case, "v1.4.1"
    )
    script = REPO / "scripts/install-mcp-docker.sh"
    command = r"""
set -e
source "$INSTALLER"
curl() { printf '%s\n' "$@" > curl-args; printf '%s\n' "$FIXTURE_REF"; return "$FETCH_EXIT"; }
docker() { printf '%s\n' "$@" >> docker-args; }
pull_image
setup_mcp_json
"""
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=tmp_path,
        env={
            **os.environ,
            "INSTALLER": str(script),
            "MCP_VARIANT": selector,
            "FIXTURE_REF": response,
            "FETCH_EXIT": "22" if case == "missing" else "0",
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    if case in {"missing", "malformed", "wrong_namespace", "invalid_selector"}:
        assert result.returncode != 0
        assert not (tmp_path / "docker-args").exists()
        assert not (tmp_path / ".mcp.json").exists()
        return
    assert result.returncode == 0, result.stderr
    expected = "ghcr.io/consiliency/code-index-mcp:local-smoke" if case == "local" else image
    calls = (tmp_path / "docker-args").read_text().splitlines()
    assert calls == (["image", "inspect", expected] if case == "local" else ["pull", expected])
    config = json.loads((tmp_path / ".mcp.json").read_text())
    assert config["mcpServers"]["code-index"]["args"][-3:] == [expected, "index-it-mcp", "stdio"]
    if case != "local":
        url = (tmp_path / "curl-args").read_text().splitlines()[-1]
        selector_path = "latest/download" if case == "latest" else "download/v1.4.1"
        assert url.endswith(f"/releases/{selector_path}/image-reference.txt")


@pytest.mark.parametrize(
    "args", [[], ["--version"], ["setup"], ["upgrade"], ["search", "two words"]]
)
def test_generated_docker_launcher_keeps_selected_digest(tmp_path, args):
    source = _read_text("scripts/install-mcp-docker.sh")
    template = source.split("cat > /tmp/mcp-index << 'EOF'\n", 1)[1].split("\nEOF", 1)[0]
    image = "ghcr.io/consiliency/code-index-mcp@sha256:" + "a" * 64
    launcher = tmp_path / "launcher"
    launcher.write_text(template.replace("@MCP_IMAGE_REF@", image))
    result = subprocess.run(
        [
            "bash",
            "-c",
            'docker() { printf "%s\\n" "$@" > "$CALLS"; }; export -f docker; bash "$@"',
            "test",
            str(launcher),
            *args,
        ],
        cwd=tmp_path,
        env={**os.environ, "CALLS": str(tmp_path / "calls"), "MCP_VARIANT": "untrusted-tag"},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert image in calls and "untrusted-tag" not in calls
    if args == ["upgrade"]:
        assert calls == ["pull", image]
    else:
        assert calls[calls.index(image) + 1 :] == ["index-it-mcp", *(args or ["stdio"])]


def _read_text(relative_path: str) -> str:
    return (REPO / relative_path).read_text()


def test_runtime_version_matches_stable_contract():
    import mcp_server

    assert mcp_server.__version__ == EXPECTED_VERSION, (
        f"mcp_server.__version__ is {mcp_server.__version__!r}, " f"expected {EXPECTED_VERSION!r}"
    )


def test_pyproject_version_matches_stable_contract():
    with (REPO / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)

    assert data["project"]["version"] == EXPECTED_VERSION


def test_python_distribution_identity_is_frozen():
    with (REPO / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)

    assert data["project"]["name"] == "index-it-mcp"
    assert data["project"]["scripts"]["mcp-index"] == "mcp_server.cli:cli"
    assert data["project"]["scripts"]["index-it-mcp"] == "mcp_server.cli:cli"
    assert "code-index-mcp" not in data["project"]["scripts"]


def test_lock_root_version_matches_release():
    lock = tomllib.loads(_read_text("uv.lock"))
    roots = [p for p in lock["package"] if p["name"] == "index-it-mcp"]
    assert len(roots) == 1
    assert roots[0]["version"] == EXPECTED_VERSION
    assert roots[0]["source"] == {"editable": "."}


def test_readme_distribution_identity_remains_stable():
    readme = _read_text("README.md")

    assert "**Python distribution**: `index-it-mcp`" in readme
    assert "**Container image**: `ghcr.io/consiliency/code-index-mcp`" in readme
    assert "docs/status/public-package-identity.md" in readme


def test_documented_publish_dispatch_binds_accepted_identity():
    import yaml

    readme = _read_text("README.md")
    section = readme.split("### Creating Releases", 1)[1].split("### Automatic", 1)[0]
    command = next(
        line
        for line in section.replace("\\\n", " ").splitlines()
        if line.startswith("gh workflow run")
    )
    arguments = shlex.split(command)
    fields = dict(
        arguments[index + 1].split("=", 1) for index, value in enumerate(arguments) if value == "-f"
    )
    workflow = yaml.safe_load(_read_text(".github/workflows/release-automation.yml"))
    inputs = (workflow.get("on") or workflow[True])["workflow_dispatch"]["inputs"]
    assert set(fields) <= set(inputs)
    assert fields["expected_commit"] == "$ACCEPTED_MERGE_COMMIT"
    assert fields["expected_tree"] == "$ACCEPTED_MERGE_TREE"
    assert fields["mode"] == "publish"
    assert fields["version"] == EXPECTED_TAG
    assert "GitHub SLSA attestations" not in readme
    assert "Automatic redaction of detected secrets" not in readme


def test_changelog_has_prepared_unpublished_contract_section():
    changelog = _read_text("CHANGELOG.md")

    section = changelog.split(f"## [{EXPECTED_VERSION}] - 2026-09-12", 1)[1].split("\n## [", 1)[0]
    assert "Prepared" in section
    assert "Code-Index-MCP#97" in section
    assert "does not imply publication" in section


def test_release_workflow_separates_prepare_from_protected_main_publish():
    workflow = _read_text(".github/workflows/release-automation.yml")

    assert f"Exact version to prepare or publish (e.g., {EXPECTED_TAG})" in workflow
    assert f"default: '{EXPECTED_TAG}'" in workflow
    assert "default: 'prepare'" in workflow
    assert "prepare-release-pr:" in workflow
    assert "publish-release:" in workflow
    assert "inputs.mode == 'publish' && github.ref == 'refs/heads/main'" in workflow
    assert "Publication remains a separate protected-main workflow dispatch after merge" in workflow
    assert "gh workflow run" not in workflow
    assert 'grep -Fxq "version = \\"$VERSION_NO_V\\"" pyproject.toml' in workflow
    assert 'grep -Fxq "__version__ = \\"$VERSION_NO_V\\"" mcp_server/__init__.py' in workflow
    assert 'if [[ "$RELEASE_VERSION" == *-* ]]; then flags+=(--prerelease); fi' in workflow
    assert "verify-container:" in workflow
    assert "image-reference.txt" in workflow
    assert "imagetools create" not in workflow
    assert 'owner="${GITHUB_REPOSITORY_OWNER,,}"' in workflow
    assert "pypa/gh-action-pypi-publish@cef221092ed1bacb1cc03d23a2d87d1d172e277b" in workflow
    assert 'gh release create "$RELEASE_VERSION"' in workflow
    assert "--verify-tag" in workflow


def test_release_tag_is_not_reused_locally():
    result = subprocess.run(
        ["git", "tag", "-l", EXPECTED_TAG],
        capture_output=True,
        text=True,
        check=True,
    )
    if not result.stdout.strip():
        return

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    tag_commit = subprocess.run(
        ["git", "rev-parse", f"{EXPECTED_TAG}^{{commit}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert tag_commit == head, f"{EXPECTED_TAG} already identifies a different source commit"


def test_installers_and_download_helper_match_stable_identity_contract():
    for relative_path in DOCKER_INSTALLERS:
        text = _read_text(relative_path)
        assert EXPECTED_TAG in text
        assert "local-smoke" in text
        assert "ghcr.io/consiliency/code-index-mcp" in text
        assert "latest" in text

    shell = _read_text("scripts/install-mcp-docker.sh")
    powershell = _read_text("scripts/install-mcp-docker.ps1")
    download_helper = _read_text("scripts/download-release.py")

    assert 'MCP_VARIANT="${MCP_VARIANT:-v1.4.1}"' in shell
    assert 'MCP_VARIANT="${MCP_VARIANT:-local-smoke}"' not in shell
    assert 'param(\n    [string]$Variant = "v1.4.1"' in powershell
    assert "SET MCP_IMAGE_REF=@MCP_IMAGE_REF@" in powershell
    assert 'IF "%MCP_VARIANT%"=="" SET MCP_VARIANT=local-smoke' not in powershell

    # local-smoke stays available as a selectable dev option (just not the default).
    assert "local-smoke" in shell
    assert "make release-smoke-container" in shell
    assert "local-smoke" in powershell
    assert "make release-smoke-container" in powershell

    assert "Versioned release image (requires publication)" in shell
    assert "Versioned release image (requires publication)" in powershell

    readme = _read_text("README.md")
    quick_start = readme.split("## 🚀 Quick Start", 1)[1].split("## Using Against Many Repos", 1)[0]
    assert "after protected-main publication" in quick_start
    assert "image-reference.txt" in quick_start
    for installer in (shell, powershell):
        assert "image-reference.txt" in installer
        assert "no tag fallback is permitted" in installer

    for expected in (
        "index_it_mcp-",
        "asset_kind",
        "wheel",
        "sdist",
        "CHANGELOG",
        "sbom",
        "ViperJuice/Code-Index-MCP",
    ):
        assert expected in download_helper


def test_active_docs_separate_candidate_from_publication():
    for path in ("README.md", "docs/GETTING_STARTED.md", "docs/MCP_CONFIGURATION.md"):
        text = _read_text(path)
        assert "1.4.1" in text
        assert "prepared candidate" in text
        assert "docs/operations/v13-release.md" in text or "operations/v13-release.md" in text
        assert "July 10, 2026 collision check" not in text


def test_index_management_workflow_uses_repo_scoped_indexes_for_ci_uploads():
    workflow = _read_text(".github/workflows/index-management.yml")

    assert ".mcp-index/current.db" in workflow
    assert ".mcp-index/.index_metadata.json" in workflow
    assert "python scripts/index-artifact-upload.py --method direct" not in workflow
    assert '[ -f "code_index.db" ] && [ -f ".index_metadata.json" ]' not in workflow
