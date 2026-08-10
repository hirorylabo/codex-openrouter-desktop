#!/usr/bin/env python3
"""Verify the pinned upstream source and Unlicense hashes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import urllib.request


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "portable/manifest.json").read_text(encoding="utf-8"))
    version_file = manifest.get("release_version_file")
    if manifest.get("schema_version") != 3 or version_file != "VERSION":
        raise RuntimeError("manifest must reference the root VERSION single source")
    upstream = manifest["upstream_patcher"]
    adapters = json.loads((root / "adapters/index.json").read_text(encoding="utf-8"))
    for adapter in adapters.get("adapters", []):
        patcher = (root / adapter["patcher"]).resolve()
        if not patcher.is_relative_to(root) or not patcher.is_file():
            raise RuntimeError(f"unsafe or missing adapter patcher: {adapter.get('patcher')}")
        source = patcher.read_text(encoding="utf-8")
        for expected in (
            adapter["chatgpt_version"],
            str(adapter["chatgpt_build"]),
            adapter["stock_asar_sha256"],
            adapter["marker"],
        ):
            if expected not in source:
                raise RuntimeError(f"adapter index and patcher differ: {adapter['id']}")
        if "--upstream-sha256" not in source:
            raise RuntimeError(f"adapter does not accept the manifest upstream hash: {adapter['id']}")
    for path in (
        root / "portable/install.sh",
        root / "portable/templates/codex-openrouter-rebuild.zsh.in",
        root / "portable/patcher/patch_candidate.py",
        root / "src/codex_openrouter/candidate.py",
    ):
        source = path.read_text(encoding="utf-8")
        if upstream["commit"] in source or upstream["source_sha256"] in source:
            raise RuntimeError(f"upstream pin is duplicated outside manifest: {path}")
    checks = (
        ("source", upstream["source_url"], upstream["source_sha256"]),
        ("Unlicense", upstream["license_url"], upstream["license_sha256"]),
    )
    for label, url, expected in checks:
        request = urllib.request.Request(url, headers={"User-Agent": "codex-openrouter-license-audit/1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            actual = hashlib.sha256(response.read()).hexdigest()
        if actual != expected:
            raise RuntimeError(f"{label} hash mismatch: {actual}")
        print(f"OK: {label} {actual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
