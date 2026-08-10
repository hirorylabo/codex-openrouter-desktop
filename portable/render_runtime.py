#!/usr/bin/env python3
"""Validate a profile and render non-secret runtime configuration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from codex_openrouter.profile import render_provider_mapping, resolve_profile


def atomic_write(path: Path, payload: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--output-home", required=True, type=Path)
    parser.add_argument("--runtime-home", type=Path)
    parser.add_argument("--credential-helper", required=True, type=Path)
    args = parser.parse_args()

    registry_path = args.registry.resolve()
    profile = resolve_profile(registry_path, args.profile.resolve())
    output_home = args.output_home.resolve()
    runtime_home = (
        args.runtime_home.resolve() if args.runtime_home is not None else output_home
    )
    credential_helper = args.credential_helper.resolve()
    template = args.template.read_text(encoding="utf-8")
    reasoning_line = (
        f'model_reasoning_effort = "{profile.default_effort}"'
        if profile.default_effort
        else ""
    )
    rendered = (
        template.replace("@@DEFAULT_MODEL@@", profile.default_model)
        .replace("@@REASONING_LINE@@", reasoning_line)
        .replace("@@CODEX_HOME@@", str(runtime_home))
        .replace("@@CREDENTIAL_HELPER@@", str(credential_helper))
    )
    if "@@" in rendered:
        raise ValueError("unresolved config template placeholder")

    registry_document = json.loads(registry_path.read_text(encoding="utf-8"))
    atomic_write(
        output_home / "registry.json",
        json.dumps(registry_document, indent=2, ensure_ascii=False) + "\n",
    )
    atomic_write(
        output_home / "profile.json",
        json.dumps(profile.as_json(), indent=2, ensure_ascii=False) + "\n",
    )
    atomic_write(
        output_home / "desktop-model-providers.json",
        json.dumps(render_provider_mapping(profile), indent=2, ensure_ascii=False) + "\n",
    )
    atomic_write(output_home / "config.toml", rendered)
    print(f"profile={profile.name} models={len(profile.models)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
