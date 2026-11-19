"""SL-2 tests: verify requirements consolidation into pyproject.toml.

These tests confirm that:
- No stale requirements-*.txt files remain
- pyproject.toml has the correct requires-python value
- AGENTS.md does not contain a stale Python 3.8 version claim
- uv.lock exists (pyproject.toml is the sole dependency source of truth)
"""

import tomllib
from pathlib import Path

REPO = Path(__file__).parent.parent
ACTIVE_WORKFLOW_GLOBS = (".github/workflows/*.yml", ".github/workflows/*.yaml")
ACTIVE_DOCKERFILE_GLOBS = ("docker/dockerfiles/Dockerfile*",)
STALE_DEPENDENCY_PATTERNS = (
    "requirements.txt",
    "requirements-production.txt",
    "requirements-semantic.txt",
    "requirements-core.txt",
    "requirements*.txt",
    "pip install -r",
    "hashFiles('**/requirements*.txt')",
)


def test_no_requirements_txt_files():
    stale = list(REPO.glob("requirements*.txt"))
    assert stale == [], f"Stale requirements files: {stale}"


def test_pyproject_parseable_toml():
    with open(REPO / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    assert "project" in data, "pyproject.toml missing [project] section"


def test_pyproject_requires_python():
    with open(REPO / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    assert data["project"]["requires-python"] == ">=3.14"
    versions = [
        c.rsplit("::", 1)[1].strip()
        for c in data["project"]["classifiers"]
        if c.startswith("Programming Language :: Python :: 3.")
    ]
    assert versions == ["3.14"]


def test_agents_md_no_stale_python_claim():
    text = (REPO / "AGENTS.md").read_text()
    # The stale claim is the exact string "# Python Version: 3.8+"
    assert (
        "# Python Version: 3.8+" not in text
    ), "AGENTS.md still contains stale '# Python Version: 3.8+' claim"


def test_uv_lock_exists():
    assert (REPO / "uv.lock").exists(), "uv.lock does not exist"


def test_active_workflows_do_not_reference_requirements_files():
    stale = {}
    for pattern in ACTIVE_WORKFLOW_GLOBS:
        for path in REPO.glob(pattern):
            text = path.read_text()
            hits = [needle for needle in STALE_DEPENDENCY_PATTERNS if needle in text]
            if hits:
                stale[str(path.relative_to(REPO))] = hits

    assert stale == {}, f"Active workflows reference removed requirements files: {stale}"


def test_dockerfiles_do_not_reference_requirements_files():
    stale = {}
    for pattern in ACTIVE_DOCKERFILE_GLOBS:
        for path in REPO.glob(pattern):
            text = path.read_text()
            hits = [needle for needle in STALE_DEPENDENCY_PATTERNS if needle in text]
            if hits:
                stale[str(path.relative_to(REPO))] = hits

    assert stale == {}, f"Dockerfiles reference removed requirements files: {stale}"


def test_pyproject_has_no_dead_python_version_markers():
    """requires-python is >=3.14, so markers for older interpreters are dead."""
    import re

    text = (REPO / "pyproject.toml").read_text()
    for match in re.finditer(r"python_version\s*<\s*[\"'](3\.\d+)[\"']", text):
        major, minor = (int(part) for part in match.group(1).split("."))
        assert (major, minor) > (3, 14), f"dead marker in pyproject.toml: {match.group(0)}"
