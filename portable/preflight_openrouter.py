#!/usr/bin/env python3
"""Verify the key-level OpenRouter boundary without exposing the key."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from codex_openrouter.openrouter import OpenRouterError, validate_key_and_profile
from codex_openrouter.profile import ProfileError, resolve_profile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--credential-helper", required=True, type=Path)
    args = parser.parse_args()
    result = subprocess.run(
        [str(args.credential_helper.resolve()), "get"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    key = result.stdout
    if result.returncode != 0 or not key.startswith("sk-or-"):
        print("OpenRouter credential is unavailable from the Keychain helper", file=sys.stderr)
        return 1
    try:
        profile = resolve_profile(args.registry.resolve(), args.profile.resolve())
        validate_key_and_profile(key, set(profile.models))
    except (ProfileError, OpenRouterError) as error:
        print(f"OpenRouter preflight failed: {error}", file=sys.stderr)
        return 1
    print(
        f"OpenRouter preflight: the effective concrete-model set exactly matches "
        f"profile {profile.name} ({len(profile.models)} models)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
