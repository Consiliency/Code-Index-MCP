"""BAML source, generated SDK and installed bridge must use one exact version."""

import os
import shutil
import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest


def test_baml_generator_runtime_and_generated_client_match():
    root = Path(__file__).resolve().parents[1]
    runtime = version("baml-bridge")
    assert runtime == "0.20.1"
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert f"baml-bridge=={runtime}" in project["project"]["dependencies"]
    assert f'VERSION = "{runtime}"' in (root / "scripts/generate_baml_sdk.py").read_text()
    assert (root / "baml.toml").is_file()
    assert (
        f"BAML generation and runtime are pinned together at {runtime}"
        in (root / "docs/SUPPORT_MATRIX.md").read_text()
    )

    from baml_sdk import SummarizeChunkAlone_spec, SummarizeFileChunks_spec

    assert callable(SummarizeChunkAlone_spec)
    assert callable(SummarizeFileChunks_spec)


def test_baml_regeneration_and_formatting_are_reproducible(tmp_path):
    root = Path(__file__).resolve().parents[1]
    toolchain = os.environ.get("BAML_TOOLCHAIN") or shutil.which("baml")
    if not toolchain:
        if os.environ.get("BAML_REQUIRE_REGEN") == "1":
            pytest.fail("BAML 0.20.1 toolchain is required by the release gate")
        pytest.skip("matching BAML 0.20.1 toolchain is not installed")
    checked = subprocess.run(
        [toolchain, "--version"], capture_output=True, text=True, check=True, timeout=30
    )
    if not checked.stdout.strip().endswith("0.20.1"):
        if os.environ.get("BAML_REQUIRE_REGEN") == "1":
            pytest.fail("BAML 0.20.1 toolchain is required by the release gate")
        pytest.skip("matching BAML 0.20.1 toolchain is not installed")

    shutil.copytree(root / "baml_src", tmp_path / "baml_src")
    (tmp_path / "scripts").mkdir()
    shutil.copy2(root / "scripts/generate_baml_sdk.py", tmp_path / "scripts")
    shutil.copy2(root / "baml.toml", tmp_path / "baml.toml")
    subprocess.run(
        [sys.executable, str(tmp_path / "scripts/generate_baml_sdk.py")],
        cwd=tmp_path,
        env={**os.environ, "BAML_TOOLCHAIN": toolchain},
        check=True,
        capture_output=True,
        timeout=120,
    )
    expected = {
        path.relative_to(root / "baml_sdk"): path.read_bytes()
        for path in (root / "baml_sdk").rglob("*")
        if path.is_file()
        and (path.suffix in {".py", ".pyi"} or path.name in {".gitignore", "py.typed"})
    }
    observed = {
        path.relative_to(tmp_path / "baml_sdk"): path.read_bytes()
        for path in (tmp_path / "baml_sdk").rglob("*")
        if path.is_file()
        and (path.suffix in {".py", ".pyi"} or path.name in {".gitignore", "py.typed"})
    }
    assert observed == expected
