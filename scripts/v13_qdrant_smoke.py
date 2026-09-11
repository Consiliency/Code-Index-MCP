#!/usr/bin/env python3
"""Isolated file/server Qdrant acceptance using deterministic synthetic vectors."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

IMAGE = "qdrant/qdrant@sha256:f1c7272cdac52b38c1a0e89313922d940ba50afd90d593a1605dbbc214e66ffb"
ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("file", "server"), required=True)
    args = parser.parse_args()
    started = datetime.now(timezone.utc).isoformat()
    env = os.environ.copy()
    env.update(SEMANTIC_SEARCH_ENABLED="false", MCP_TEST_MODE="1", QDRANT_USE_SERVER="false")
    env.pop("V13_TEST_QDRANT_URL", None)
    env.pop("V13_TEST_QDRANT_CONTAINER", None)
    container = None
    server_version = None
    try:
        if args.mode == "server":
            name = "v13-qdrant-" + uuid.uuid4().hex[:12]
            container = subprocess.check_output(
                [
                    "docker",
                    "run",
                    "--pull",
                    "never",
                    "--detach",
                    "--rm",
                    "--name",
                    name,
                    "--publish",
                    "127.0.0.1::6333",
                    "--env",
                    "QDRANT__TELEMETRY_DISABLED=true",
                    IMAGE,
                ],
                text=True,
                timeout=30,
            ).strip()
            ports = json.loads(
                subprocess.check_output(
                    ["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", container],
                    text=True,
                    timeout=10,
                )
            )
            endpoint = "http://127.0.0.1:" + ports["6333/tcp"][0]["HostPort"]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    with urlopen(endpoint, timeout=2) as response:
                        server_version = json.load(response)["version"]
                    break
                except (OSError, URLError, KeyError, json.JSONDecodeError):
                    time.sleep(0.2)
            if not server_version:
                raise RuntimeError("Disposable Qdrant did not become ready")
            env["V13_TEST_QDRANT_URL"] = endpoint
            env["V13_TEST_QDRANT_CONTAINER"] = container

        with tempfile.TemporaryDirectory(prefix="v13-qdrant-proof-") as tmp:
            report = Path(tmp) / "junit.xml"
            command = [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_v13_data_vectors.py",
                "-k",
                "maintenance",
                "-q",
                "--no-cov",
                "-o",
                "log_cli=false",
                "--junitxml",
                str(report),
            ]
            result = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=300,
                check=False,
            )
            evidence = ROOT / ".phase-loop" / "runs" / ("v13-DATA-qdrant-" + uuid.uuid4().hex)
            evidence.mkdir(parents=True)
            (evidence / "pytest.log").write_bytes(result.stdout)
            if report.exists():
                (evidence / "junit.xml").write_bytes(report.read_bytes())
            cases = list(ET.parse(report).iter("testcase")) if report.exists() else []
            outcomes = [
                {
                    "name": case.attrib["name"],
                    "seconds": float(case.attrib.get("time", 0)),
                    "passed": not any(
                        child.tag in {"failure", "error", "skipped"} for child in case
                    ),
                }
                for case in cases
            ]
            passed = (
                result.returncode == 0
                and len(outcomes) == 11
                and all(item["passed"] for item in outcomes)
            )
            print(
                json.dumps(
                    {
                        "proof": "qdrant-" + args.mode,
                        "status": "passed" if passed else "failed",
                        "started_at": started,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "client_version": version("qdrant-client"),
                        "server_version": server_version,
                        "image": IMAGE if container else None,
                        "exit_code": result.returncode,
                        "cases": outcomes,
                        "output_sha256": hashlib.sha256(result.stdout).hexdigest(),
                        "output_path": str((evidence / "pytest.log").relative_to(ROOT)),
                        "inference_requests": 0,
                        "inputs": "synthetic deterministic vectors only",
                    }
                ),
                flush=True,
            )
            if not passed:
                raise SystemExit(1)
    finally:
        if container:
            subprocess.run(
                ["docker", "stop", "--time", "2", container],
                stdout=subprocess.DEVNULL,
                check=True,
                timeout=15,
            )


if __name__ == "__main__":
    main()
