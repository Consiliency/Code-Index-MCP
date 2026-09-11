#!/usr/bin/env python3
"""Exercise termination against the installed MCP entrypoint and synthetic registry."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import selectors
import signal
import socket
import subprocess
import time
from pathlib import Path

import psutil


def send(proc, method, params=None, request_id=None):
    message = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if request_id is not None:
        message["id"] = request_id
    proc.stdin.write(json.dumps(message).encode() + b"\n")
    proc.stdin.flush()


def receive(proc, request_id, timeout=60):
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            if not selector.select(min(0.1, deadline - time.monotonic())):
                continue
            block = os.read(proc.stdout.fileno(), 65536)
            if not block:
                raise AssertionError("server exited before response")
            buffer.extend(block)
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)
                payload = json.loads(line)
                if payload.get("id") == request_id:
                    assert "error" not in payload, payload
                    return payload["result"]
    raise AssertionError("server response deadline exceeded")


def run(root: Path, entrypoint: str, env: dict[str, str]):
    import mcp_server

    assert "site-packages" in Path(mcp_server.__file__).parts, "checkout imports are not acceptance"
    receipts = []
    fixture = root / "fixture"
    worker_source = fixture / "worker_synthetic.js"
    worker_source.write_text("export function PRIVATE_QUERY_SENTINEL_73051() { return 73051; }\n")
    for mode in ("sigterm", "sigint", "eof", "repeated", "partial_input", "inflight"):
        log = root / f"safety-{mode}.log"
        with log.open("wb") as stderr:
            proc = subprocess.Popen(
                [entrypoint, "stdio"],
                cwd=root,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
            )
            children = []
            try:
                send(
                    proc,
                    "initialize",
                    {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "safety-probe", "version": "1"},
                    },
                    1,
                )
                receive(proc, 1)
                send(proc, "notifications/initialized")
                send(
                    proc,
                    "tools/call",
                    {"name": "reindex", "arguments": {"repository": str(fixture)}},
                    2,
                )
                assert not receive(proc, 2).get("isError")
                # File-scoped JavaScript indexing exercises an actual plugin worker,
                # unlike Python's bounded in-process lexical fast path.
                worker_source.write_text(
                    f"export function PRIVATE_QUERY_SENTINEL_73051() {{ return {len(receipts)}; }}\n"
                )
                send(
                    proc,
                    "tools/call",
                    {
                        "name": "reindex",
                        "arguments": {"repository": str(fixture), "path": str(worker_source)},
                    },
                    20,
                )
                assert not receive(proc, 20).get("isError")
                send(proc, "tools/call", "PRIVATE_QUERY_SENTINEL_73051", 999)
                send(
                    proc,
                    "tools/call",
                    {
                        "name": "search_code",
                        "arguments": {
                            "query": "PRIVATE_QUERY_SENTINEL_73051",
                            "repository": str(fixture),
                        },
                    },
                    3,
                )
                response = receive(proc, 3)
                assert not response.get("isError")
                found = json.loads(
                    "".join(block["text"] for block in response["content"] if "text" in block)
                )
                assert found if isinstance(found, list) else found.get("results"), found
                children = psutil.Process(proc.pid).children(recursive=True)
                assert children, "no installed plugin child was exercised"
                connections = psutil.Process(proc.pid).net_connections(kind="tcp")
                ports = [
                    connection.laddr.port
                    for connection in connections
                    if connection.status == "LISTEN"
                ]
                assert all(
                    connection.laddr.ip == "127.0.0.1"
                    for connection in connections
                    if connection.status == "LISTEN"
                )
                admitted = False
                if mode == "inflight":
                    slow = fixture / "slow_synthetic.py"
                    slow.write_text(
                        "\n".join(f"def synthetic_{i}():\n    return {i}\n" for i in range(20000))
                    )
                    send(
                        proc,
                        "tools/call",
                        {
                            "name": "reindex",
                            "arguments": {"repository": str(fixture), "path": str(slow)},
                        },
                        4,
                    )
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if "index_publication_pending" in (root / "registry.json").read_text():
                            admitted = True
                            break
                        time.sleep(0.01)
                    assert admitted, "inflight writer was not observed admitted"
                start = time.monotonic()
                if mode == "eof":
                    proc.stdin.close()
                else:
                    if mode == "partial_input":
                        proc.stdin.write(b'{"jsonrpc":')
                        proc.stdin.flush()
                    proc.send_signal(signal.SIGINT if mode == "sigint" else signal.SIGTERM)
                    if mode == "repeated":
                        time.sleep(0.01)
                        if proc.poll() is None:
                            proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=18)
                duration = time.monotonic() - start
                if mode != "inflight":
                    assert proc.returncode == 0, (mode, proc.returncode)
                    assert duration < 8, (mode, duration)
                else:
                    assert proc.returncode in (0, 1), proc.returncode
                    assert duration < 17, duration
                live = [
                    child.pid
                    for child in children
                    if child.is_running() and child.status() != psutil.STATUS_ZOMBIE
                ]
                assert not live, live
                for port in ports:
                    with socket.socket() as listener:
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        listener.bind(("127.0.0.1", port))
                if mode == "inflight" and proc.returncode == 1:
                    assert "index_publication_pending" in (root / "registry.json").read_text()
                assert "PRIVATE_QUERY_SENTINEL_73051" not in log.read_text()
                receipts.append(
                    {
                        "mode": mode,
                        "exit_code": proc.returncode,
                        "exit_seconds": round(duration, 3),
                        "owned_children_observed": len(children),
                        "surviving_children": live,
                        "metrics_ports": ports,
                        "inflight_admitted": admitted,
                    }
                )
            finally:
                if proc.poll() is None:
                    descendants = psutil.Process(proc.pid).children(recursive=True)
                    for child in descendants:
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    proc.kill()
                    proc.wait(timeout=5)
                for stream in (proc.stdin, proc.stdout):
                    if stream is not None and not stream.closed:
                        stream.close()
        if mode == "inflight":
            slow.unlink()
    print(json.dumps({"installed_lifecycle": "passed", "uid": os.getuid(), "cases": receipts}))
    worker_source.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--entrypoint", default="index-it-mcp")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location(
        "runtime_probe", Path(__file__).with_name("installed_runtime_smoke.py")
    )
    probe = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(probe)
        run(args.root, args.entrypoint, probe.environment(args.root))
    finally:
        # Synthetic mounted directories must remain removable by the host fixture owner.
        for path in args.root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o777)


if __name__ == "__main__":
    main()
