"""npm package.json parser."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Set


class NpmPackageJsonParser:
    manifest_filename = "package.json"

    def parse(self, path: Path) -> Set[str]:
        manifest = path / self.manifest_filename
        if not manifest.exists():
            return set()
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError, OSError:
            return set()
        packages: Set[str] = set()
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            packages.update(data.get(section, {}).keys())
        return packages
