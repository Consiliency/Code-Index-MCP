"""Sandbox supervisor with bounded IPC and explicit worker ownership."""

from __future__ import annotations

import logging
import os
import selectors
import signal
import subprocess
import threading
import time
import uuid
from typing import Any, List, Optional, cast

from mcp_server.core.errors import MCPError
from mcp_server.sandbox.capabilities import CapabilitySet
from mcp_server.sandbox.protocol import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_LINE_BYTES,
    Envelope,
    ProtocolError,
    decode,
    encode,
)

logger = logging.getLogger(__name__)


class SandboxCallError(MCPError):
    """Raised on the host when the worker returns an error envelope."""

    def __init__(self, payload: dict):
        self.error_type = payload.get("type", "Unknown")
        self.error_message = payload.get("message", "")
        super().__init__(f"{self.error_type}: {self.error_message}", details=payload)


class SandboxTimeout(MCPError):
    """Raised only after a timed-out worker has been terminated and reaped."""


class SandboxSupervisor:
    def __init__(self, worker_cmd: List[str], capabilities: CapabilitySet) -> None:
        self._worker_cmd = list(worker_cmd)
        self._capabilities = capabilities
        self._proc: Optional[subprocess.Popen] = None
        self._call_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._closed = False

    def _ensure_spawned(self) -> subprocess.Popen:
        with self._lifecycle_lock:
            if self._closed:
                raise SandboxCallError(
                    {"type": "SupervisorClosed", "message": "sandbox supervisor is closed"}
                )
            if self._proc is not None and self._proc.poll() is None:
                return self._proc
            if self._proc is not None:
                self._reap(self._proc)
            self._proc = subprocess.Popen(
                self._worker_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # Protocol error envelopes retain typed diagnostics. Arbitrary child
                # stderr is neither a trusted diagnostic nor a safe unbounded pipe.
                stderr=subprocess.DEVNULL,
                bufsize=0,
                start_new_session=os.name == "posix",
            )
            for stream in (self._proc.stdin, self._proc.stdout):
                os.set_blocking(stream.fileno(), False)
            return self._proc

    @property
    def worker_pid(self) -> Optional[int]:
        """Return the live worker PID without spawning a worker."""
        proc = self._proc
        return None if proc is None or proc.poll() is not None else proc.pid

    @property
    def is_worker_running(self) -> bool:
        return self.worker_pid is not None

    def worker_rss_bytes(self) -> int:
        pid = self.worker_pid
        if pid is None:
            return 0
        try:
            import psutil

            return int(psutil.Process(pid).memory_info().rss)
        except Exception as exc:
            raise RuntimeError(f"cannot measure sandbox worker ({type(exc).__name__})") from None

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        # Each POSIX worker owns a new process group, including its subprocesses.
        try:
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            elif proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif proc.poll() is None:
                proc.kill()
            proc.wait(timeout=1.0)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    def _discard(self, proc: subprocess.Popen) -> None:
        with self._lifecycle_lock:
            if self._proc is proc:
                self._proc = None
                self._reap(proc)

    def _wait(self, stream: Any, event: int, deadline: float) -> None:
        with selectors.DefaultSelector() as selector:
            selector.register(stream, event)
            while True:
                if self._closed or stream.closed:
                    raise OSError("sandbox transport closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SandboxTimeout("Sandbox request deadline exceeded")
                if selector.select(min(remaining, 0.05)):
                    return

    def _exchange(self, proc: subprocess.Popen, request: bytes, deadline: float) -> bytes:
        assert proc.stdin is not None and proc.stdout is not None
        offset = 0
        while offset < len(request):
            self._wait(proc.stdin, selectors.EVENT_WRITE, deadline)
            try:
                offset += os.write(proc.stdin.fileno(), request[offset : offset + 65536])
            except BlockingIOError:
                continue
        response = bytearray()
        while True:
            self._wait(proc.stdout, selectors.EVENT_READ, deadline)
            try:
                part = os.read(proc.stdout.fileno(), 65536)
            except BlockingIOError:
                continue
            if not part:
                raise SandboxCallError({"type": "WorkerExited", "message": "worker output closed"})
            response.extend(part)
            if len(response) > MAX_LINE_BYTES:
                raise SandboxCallError({"type": "ProtocolError", "message": "response too large"})
            if b"\n" in response:
                return bytes(response)

    def call(
        self,
        method: str,
        payload: dict,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        with self._call_lock:
            proc = self._ensure_spawned()
            call_id = uuid.uuid4().hex
            env = Envelope(v=1, id=call_id, kind="call", method=method, payload=payload)
            try:
                line = self._exchange(proc, encode(env), time.monotonic() + timeout)
                resp = decode(line)
                if resp.kind == "error" and resp.method == "startup" and not resp.id:
                    raise SandboxCallError(resp.payload)
                if resp.id != call_id:
                    raise SandboxCallError(
                        {"type": "ResponseMismatch", "message": "response request ID mismatch"}
                    )
                if resp.kind == "error":
                    raise SandboxCallError(resp.payload)
                if resp.kind != "result":
                    raise SandboxCallError(
                        {"type": "UnexpectedKind", "message": "expected a result envelope"}
                    )
                return cast(dict[str, Any], resp.payload)
            except BaseException as exc:
                self._discard(proc)
                if isinstance(exc, (OSError, ValueError, ProtocolError)):
                    raise SandboxCallError(
                        {"type": type(exc).__name__, "message": "sandbox transport failed"}
                    ) from None
                raise

    def close(self) -> None:
        # Do not wait for the request lock: close must interrupt blocked IPC.
        with self._lifecycle_lock:
            self._closed = True
            if self._proc is not None:
                proc, self._proc = self._proc, None
                self._reap(proc)
