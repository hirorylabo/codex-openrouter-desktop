#!/usr/bin/env python3
"""Write a non-secret installation receipt with protected permissions."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--adapter-id", required=True)
    parser.add_argument("--chatgpt-version", required=True)
    parser.add_argument("--chatgpt-build", required=True)
    parser.add_argument("--stock-asar-sha256", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()

    payload = {
        "schema_version": 2,
        "release_version": args.release_version,
        "source_commit": args.source_commit,
        "adapter_id": args.adapter_id,
        "chatgpt_version": args.chatgpt_version,
        "chatgpt_build": args.chatgpt_build,
        "stock_asar_sha256": args.stock_asar_sha256,
        "workspace": args.workspace,
    }
    args.target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".install-manifest.", dir=args.target.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, args.target)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
