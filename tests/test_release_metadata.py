"""Release metadata assertions for the prepared v1.4.1 contract.

Historical GARC soak target: v1.2.0-rc6.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

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
    assert "prerelease: ${{ contains(inputs.version, '-') }}" in workflow
    assert "${{ env.IMAGE_REF }}:${{ inputs.version }}" in workflow
    assert 'owner="${GITHUB_REPOSITORY_OWNER,,}"' in workflow
    assert "pypa/gh-action-pypi-publish@cef221092ed1bacb1cc03d23a2d87d1d172e277b" in workflow
    assert "softprops/action-gh-release@718ea10b132b3b2eba29c1007bb80653f286566b" in workflow


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
    assert 'IF "%MCP_VARIANT%"=="" SET MCP_VARIANT=v1.4.1' in powershell
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
    assert "ghcr.io/consiliency/code-index-mcp:v1.4.1" in quick_start

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
