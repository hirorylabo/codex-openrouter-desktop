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
    upstream = manifest["upstream_patcher"]
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
