"""Regenerate the checked-in BAML v1 Python SDK with clean whitespace."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "baml_sdk"
VERSION = "0.20.1"


def main() -> int:
    toolchain = os.environ.get("BAML_TOOLCHAIN", "baml")
    version = subprocess.run(
        [toolchain, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if not version.endswith(VERSION):
        raise SystemExit(f"BAML toolchain {VERSION} required; found {version}")

    subprocess.run([toolchain, "generate", "--project", str(ROOT)], check=True)
    for path in SDK.rglob("*"):
        if path.suffix not in {".py", ".pyi"}:
            continue
        source = path.read_text(encoding="utf-8")
        normalized = "\n".join(line.rstrip() for line in source.splitlines()).rstrip("\n") + "\n"
        if normalized != source:
            path.write_text(normalized, encoding="utf-8")
    (SDK / ".gitignore").write_text(
        "# Generated SDK sources are checked in.\n"
        "__pycache__/\n"
        "*.pyc\n"
        ".baml-generator-output*\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
