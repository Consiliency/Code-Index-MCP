"""Sandbox capability set — frozen dataclass defining resource limits.

The CapabilitySet fields were frozen in SL-0. SL-1 adds:
  * ``to_json`` / ``from_json`` for ferrying caps to the worker on the CLI.
  * The ``DEFAULT_DENY`` sentinel (no FS, no network, no SQLite).
  * ``SandboxViolation`` — raised inside the worker when the plugin attempts
    something the caps forbid. On the host it surfaces as ``SandboxCallError``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Literal, Tuple

from mcp_server.core.errors import MCPError


class SandboxViolation(MCPError):
    """Raised inside the worker when the plugin touches a forbidden resource.

    The host supervisor receives the failure as an ``error`` envelope and
    re-raises it as :class:`mcp_server.sandbox.supervisor.SandboxCallError`.
    """


@dataclass(frozen=True)
class CapabilitySet:
    fs_read: Tuple[Path, ...]
    fs_write: Tuple[Path, ...]
    env_allow: FrozenSet[str]
    network: bool = False
    sqlite: Literal["none", "readonly"] = "none"
    cpu_seconds: int = 30
    mem_mb: int = 2048

    def to_json(self) -> str:
        """Serialize to a JSON string suitable for a shell argv slot."""
        return json.dumps(
            {
                "fs_read": [str(p) for p in self.fs_read],
                "fs_write": [str(p) for p in self.fs_write],
                "env_allow": sorted(self.env_allow),
                "network": self.network,
                "sqlite": self.sqlite,
                "cpu_seconds": self.cpu_seconds,
                "mem_mb": self.mem_mb,
            }
        )

    @classmethod
    def from_json(cls, s: str) -> "CapabilitySet":
        d = json.loads(s)
        if not isinstance(d, dict) or d.get("sqlite", "none") not in {"none", "readonly"}:
            raise ValueError("Invalid sandbox SQLite capability")
        if not isinstance(d.get("network", False), bool):
            raise ValueError("Sandbox network capability must be boolean")
        return cls(
            fs_read=tuple(Path(p) for p in d.get("fs_read", ())),
            fs_write=tuple(Path(p) for p in d.get("fs_write", ())),
            env_allow=frozenset(d.get("env_allow", ())),
            network=bool(d.get("network", False)),
            sqlite=d.get("sqlite", "none"),
            cpu_seconds=int(d.get("cpu_seconds", 30)),
            mem_mb=int(d.get("mem_mb", 2048)),
        )


DEFAULT_DENY = CapabilitySet(
    fs_read=(),
    fs_write=(),
    env_allow=frozenset(),
    network=False,
    sqlite="none",
)
