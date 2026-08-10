#!/usr/bin/env python3
"""Apply the semantic fallback only to an isolated ChatGPT candidate clone."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


UPSTREAM_MARKER = b"__codexDesktopModelProvidersPatchV3"
CUSTOM_MARKER = b"__codexOpenRouterSemanticCandidateV1"
VISIBILITY_MARKER = b"__codexOpenRouterSemanticVisibilityV1"
LABEL_MARKER = b"__codexOpenRouterSemanticLabelV1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_upstream(path: Path, expected_sha256: str):
    if sha256(path) != expected_sha256:
        raise RuntimeError("Pinned upstream patcher hash mismatch")
    spec = importlib.util.spec_from_file_location("pinned_provider_patcher", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the pinned upstream patcher")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def marker_count(path: Path, marker: bytes) -> int:
    return path.read_bytes().count(marker)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--upstream-sha256", required=True)
    parser.add_argument("--transform", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--stock-hash", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--adapter-output", type=Path, required=True)
    args = parser.parse_args()

    app = args.app.expanduser().resolve()
    candidate_root = args.candidate_root.expanduser().resolve()
    try:
        app.relative_to(candidate_root)
    except ValueError as exc:
        raise RuntimeError("Candidate app must stay below the candidate root") from exc
    if app == Path("/Applications/ChatGPT.app").resolve() or app.is_symlink():
        raise RuntimeError("Refusing to patch the stock app or a symlink")
    asar = app / "Contents/Resources/app.asar"
    if not asar.is_file() or sha256(asar) != args.stock_hash:
        raise RuntimeError("Candidate ASAR is not an exact copy of the detected stock ASAR")

    provider_config = json.loads(args.config.read_text(encoding="utf-8"))
    if provider_config.get("default_provider") != "openrouter":
        raise RuntimeError("Candidate provider config is not OpenRouter-only")
    mappings = provider_config.get("model_providers")
    if not isinstance(mappings, dict) or not mappings or set(mappings.values()) != {"openrouter"}:
        raise RuntimeError("Candidate provider mappings are invalid")

    module = load_upstream(args.upstream.resolve(), args.upstream_sha256)
    module.DEFAULT_PROVIDER_CONFIG = provider_config

    def semantic_variant(central: Path, picker: Path) -> str:
        result = subprocess.run(
            [str(args.node), str(args.transform), str(central), str(picker)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise module.PatchError(result.stderr.strip() or "semantic transform failed")
        return f"semantic candidate for ChatGPT {args.version} build {args.build}"

    module.apply_supported_patch_variant = semantic_variant
    module.patch_app(app, args.config.resolve(), args.backup_dir.resolve(), overwrite_config=False)

    for marker in (UPSTREAM_MARKER, CUSTOM_MARKER, VISIBILITY_MARKER, LABEL_MARKER):
        if marker_count(asar, marker) != 1:
            raise RuntimeError(f"Candidate marker is not present exactly once: {marker!r}")
    patched_hash = sha256(asar)
    adapter = {
        "schema_version": 1,
        "id": f"local-{args.version}-build-{args.build}-{args.stock_hash[:12]}",
        "chatgpt_version": args.version,
        "chatgpt_build": args.build,
        "stock_asar_sha256": args.stock_hash,
        "patched_asar_sha256": patched_hash,
        "patch_strategy": "semantic-candidate",
        "marker": CUSTOM_MARKER.decode("ascii"),
        "upstream_marker": UPSTREAM_MARKER.decode("ascii"),
        "transform_markers": [
            CUSTOM_MARKER.decode("ascii"),
            VISIBILITY_MARKER.decode("ascii"),
            LABEL_MARKER.decode("ascii"),
        ],
    }
    args.adapter_output.parent.mkdir(parents=True, exist_ok=True)
    args.adapter_output.write_text(json.dumps(adapter, indent=2) + "\n", encoding="utf-8")
    args.adapter_output.chmod(0o600)
    print(json.dumps(adapter, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
