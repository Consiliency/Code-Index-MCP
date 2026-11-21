#!/usr/bin/env python3
import json
import subprocess
import time
import urllib.request
import urllib.parse
from pathlib import Path
from dataclasses import dataclass


@dataclass
class BenchmarkResult:
    category: str
    task: str
    tool: str
    latency_ms: float
    output_chars: int
    success: bool
    notes: str = ""


def run_native_rg(query: str, cwd: str) -> tuple[str, float]:
    start = time.perf_counter()
    proc = subprocess.run(
        ["rg", "-n", "--no-heading", "--context", "2", query],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    elapsed = (time.perf_counter() - start) * 1000
    return proc.stdout, elapsed


def run_native_glob(pattern: str, cwd: str) -> tuple[str, float]:
    start = time.perf_counter()
    proc = subprocess.run(
        ["find", ".", "-path", pattern], cwd=cwd, capture_output=True, text=True
    )
    elapsed = (time.perf_counter() - start) * 1000
    return proc.stdout, elapsed


def mcp_request(path: str, token: str) -> tuple[dict, float]:
    url = f"http://127.0.0.1:9123{path}"
    headers = {"Authorization": f"Bearer {token}"}
    req = urllib.request.Request(url, headers=headers)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        elapsed = (time.perf_counter() - start) * 1000
        return data, elapsed
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"error": str(e)}, elapsed


def get_mcp_token() -> str:
    req = urllib.request.Request(
        "http://127.0.0.1:9123/api/v1/auth/login",
        data=json.dumps({"username": "admin", "password": "admin123!"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())["access_token"]


def main():
    print("Starting Comprehensive Agent Efficiency Benchmark...")

    # Ensure server is running or we fail fast
    try:
        token = get_mcp_token()
    except Exception as e:
        print(f"Failed to get MCP token. Is the server running on 9123? Error: {e}")
        return

    cwd = str(Path.cwd())
    results: list[BenchmarkResult] = []

    tasks = [
        {
            "category": "A. Exact Symbol (Go-To-Def)",
            "name": "Find IndexArtifactDownloader class",
            "native_cmd": lambda: run_native_rg("class IndexArtifactDownloader", cwd),
            "mcp_path": "/symbol?symbol=IndexArtifactDownloader",
            "expected_file": "artifact_download.py",
        },
        {
            "category": "A. Exact Symbol (Go-To-Def)",
            "name": "Find _build_sqlite_baseline function",
            "native_cmd": lambda: run_native_rg("def _build_sqlite_baseline", cwd),
            "mcp_path": "/symbol?symbol=_build_sqlite_baseline",
            "expected_file": "index_management.py",
        },
        {
            "category": "B. Structural Discovery",
            "name": "Find SemanticIndexer usages",
            "native_cmd": lambda: run_native_rg("SemanticIndexer", cwd),
            "mcp_path": f"/search?q={urllib.parse.quote('SemanticIndexer')}&limit=10&mode=bm25",
            "expected_file": "semantic_indexer.py",  # Expecting it to be prominent
        },
        {
            "category": "C. Broad Concept Search",
            "name": "Concept: chunk truncation logic",
            "native_cmd": lambda: run_native_rg("truncate.*chunk|chunk.*truncate", cwd),
            "mcp_path": f"/search?q={urllib.parse.quote('chunk truncation logic')}&semantic=true&limit=5",
            "expected_file": "semantic_indexer.py",
        },
        {
            "category": "C. Broad Concept Search",
            "name": "Concept: artifact compatibility validation",
            "native_cmd": lambda: run_native_rg(
                "artifact.*compat|compat.*artifact", cwd
            ),
            "mcp_path": f"/search?q={urllib.parse.quote('artifact compatibility validation')}&semantic=true&limit=5",
            "expected_file": "artifact_download.py",  # Or integrity_gate
        },
    ]

    for t in tasks:
        print(f"\nRunning Task: {t['name']} [{t['category']}]")

        # Native
        out_native, lat_native = t["native_cmd"]()
        succ_native = t["expected_file"] in out_native
        res_native = BenchmarkResult(
            category=t["category"],
            task=t["name"],
            tool="Native (rg)",
            latency_ms=lat_native,
            output_chars=len(out_native),
            success=succ_native,
            notes="Noisy context" if succ_native and len(out_native) > 2000 else "",
        )
        results.append(res_native)

        # MCP
        out_mcp, lat_mcp = mcp_request(t["mcp_path"], token)
        out_mcp_str = json.dumps(out_mcp)
        succ_mcp = t["expected_file"] in out_mcp_str

        # For symbol lookup, if the list is empty, it's a fail
        if "symbol" in t["mcp_path"] and not out_mcp:
            succ_mcp = False

        res_mcp = BenchmarkResult(
            category=t["category"],
            task=t["name"],
            tool="MCP Server",
            latency_ms=lat_mcp,
            output_chars=len(out_mcp_str),
            success=succ_mcp,
            notes="Exact bounded AST" if "symbol" in t["mcp_path"] else "",
        )
        results.append(res_mcp)

    print("\n" + "=" * 80)
    print(
        f"{'Category':<30} | {'Task':<40} | {'Tool':<12} | {'Latency':<8} | {'Tokens(Ch)':<10} | {'Success'}"
    )
    print("-" * 115)
    for r in results:
        succ_mark = "✅" if r.success else "❌"
        print(
            f"{r.category[:28]:<30} | {r.task[:38]:<40} | {r.tool:<12} | {r.latency_ms:6.1f}ms | {r.output_chars:<10} | {succ_mark} {r.notes}"
        )


if __name__ == "__main__":
    main()
