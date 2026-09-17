"""Generator, generated client and installed runtime must use one exact BAML version."""

import re
import shutil
import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path


def test_baml_generator_runtime_and_generated_client_match():
    root = Path(__file__).resolve().parents[1]
    runtime = version("baml-py")
    generator = (root / "baml_src/generators.baml").read_text()
    assert re.search(r'version\s+"([^"]+)"', generator).group(1) == runtime
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert f"baml-py=={runtime}" in project["project"]["dependencies"]
    from mcp_server.indexing.baml_client.baml_client import inlinedbaml

    assert any(f'version "{runtime}"' in text for text in inlinedbaml.get_baml_files().values())


def test_baml_regeneration_and_formatting_are_reproducible(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "baml_src", tmp_path / "baml_src")
    generated = Path("mcp_server/indexing/baml_client/baml_client")
    subprocess.run(
        [str(Path(sys.executable).with_name("baml-cli")), "generate"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        [sys.executable, "-m", "black", "--line-length", "100", str(generated)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "isort",
            "--profile",
            "black",
            "--line-length",
            "100",
            str(generated),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=30,
    )
    expected = {p.name: p.read_bytes() for p in (root / generated).glob("*.py")}
    observed = {p.name: p.read_bytes() for p in (tmp_path / generated).glob("*.py")}
    assert observed == expected
