"""Regenerate the checked-in BAML v1 Python SDK with clean whitespace."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "baml_sdk"
VERSION = "0.23.0"

# baml-bridge 0.21+ records every call under BAML_HOME (~/.baml/btel) and
# uploads the recordings to Boundary when BOUNDARY_API_KEY or a saved
# `baml auth login` exists. `baml generate` always bakes `recording_level =
# "medium"` into the SDK's embedded policy, and baml.toml has no setting for
# it; it can only name the variable that overrides it (MCP_BAML_TELEMETRY).
# The bridge falls back to the baked level when that variable is unset or
# blank, so the baked level is the real default. Summarization prompts carry
# source code, so bake "off": recording then needs an explicit
# MCP_BAML_TELEMETRY=low|medium|high, whoever imports baml_sdk first.
BAKED_RECORDING_LEVEL = 'recording_level = \\"medium\\"'
SAFE_RECORDING_LEVEL = 'recording_level = \\"off\\"'


def bake_recording_default_off(sdk: Path) -> None:
    inlined = sdk / "_inlinedbaml.py"
    source = inlined.read_text(encoding="utf-8")
    if source.count(BAKED_RECORDING_LEVEL) != 1 or SAFE_RECORDING_LEVEL in source:
        raise SystemExit(
            f"{inlined}: expected exactly one baked {BAKED_RECORDING_LEVEL!r}; "
            "the generated telemetry policy changed, so re-check the recording default"
        )
    inlined.write_text(
        source.replace(BAKED_RECORDING_LEVEL, SAFE_RECORDING_LEVEL), encoding="utf-8"
    )


def main() -> int:
    toolchain = os.environ.get("BAML_TOOLCHAIN", "baml")
    version = subprocess.run(
        [toolchain, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if not version.endswith(VERSION):
        raise SystemExit(f"BAML toolchain {VERSION} required; found {version}")

    # Generation is offline and pinned to this repo; agent-run gates (make
    # agent-gate) must not depend on the toolchain's editor/agent skill install.
    subprocess.run(
        [toolchain, "generate", "--agent-skill-check", "off", "--project", str(ROOT)],
        check=True,
    )
    bake_recording_default_off(SDK)
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
