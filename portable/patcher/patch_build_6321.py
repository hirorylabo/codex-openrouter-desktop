#!/usr/bin/env python3
"""Strict build-6321 adaptation for the pinned Better Codex patcher.

The vendored upstream patcher remains responsible for ASAR extraction,
integrity checks, complete backups, atomic replacement, ad-hoc signing, and
rollback. This adapter replaces only three exact snippets in ChatGPT/Codex
Desktop 26.803.41515 build 6321.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


EXPECTED_UPSTREAM_SHA256 = (
    "ec63e9ba109ec171162c5bd846359ed727368eb3154b2cdeade123afeae3ffb4"
)
EXPECTED_VERSION = "26.803.41515"
EXPECTED_BUILD = "6321"
EXPECTED_UNPATCHED_ASAR_SHA256 = (
    "5f6e773aafd542d3cf09e10b5dca6cabd301d0a155f4b8ce870e3915fc3da25e"
)
CUSTOM_MARKER = b"__codexOpenRouterBuild6321PatchV1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_upstream(path: Path):
    actual = sha256(path)
    if actual != EXPECTED_UPSTREAM_SHA256:
        raise RuntimeError(
            "Pinned upstream patcher hash mismatch: "
            f"expected {EXPECTED_UPSTREAM_SHA256}, found {actual}"
        )
    spec = importlib.util.spec_from_file_location("pinned_better_codex_patcher", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load pinned upstream patcher: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def replace_once(module, source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise module.PatchError(
            f"build 6321 {label} exact snippet matched {count} times; "
            "refusing to patch an unsupported or modified bundle"
        )
    return source.replace(old, new, 1)


def install_build_6321_replacements(module, central: Path, picker: Path) -> str:
    originals = {path: path.read_text(encoding="utf-8") for path in {central, picker}}

    routing_old = (
        "},i=null){let a=Ren(e,n),o=Men(e,n?.source);"
        "if(!this.useHostRequestScheduler"
    )
    routing_new = (
        "},i=null){globalThis.__codexDesktopModelProvidersPatchV3=!0;"
        "globalThis.__codexOpenRouterBuild6321PatchV1=`CodexCustomProviderPickerSection`;"
        "e===`thread/start`&&t!=null&&typeof t===`object`&&"
        "t.modelProvider==null&&(t={...t,modelProvider:`openrouter`});"
        "e===`thread/list`&&(t=t!=null&&typeof t===`object`?"
        "t.modelProviders==null?{...t,modelProviders:[]}:t:{modelProviders:[]});"
        "let a=Ren(e,n),o=Men(e,n?.source);if(!this.useHostRequestScheduler"
    )

    filter_old = (
        "function dQr({additionalAvailableModels:e,authMethod:t,availableModels:n,"
        "model:r,useHiddenModels:i}){return e?.has(r.model)===!0||"
        "(i&&t!==`amazonBedrock`?n.has(r.model):!r.hidden)}"
    )
    filter_new = (
        "function dQr({additionalAvailableModels:e,authMethod:t,availableModels:n,"
        "model:r,useHiddenModels:i}){return e?.has(r.model)===!0||"
        "(i&&t===`amazonBedrock`?n.has(r.model):!r.hidden)}"
    )

    fallback_old = (
        "o=oe?.displayName??U.formatMessage({id:`composer.mode.local.model.custom`,"
        "defaultMessage:`Custom`,description:`Custom model from config`})"
    )
    fallback_new = "o=oe?.displayName??d"

    central_source = originals[central]
    central_source = replace_once(
        module, central_source, routing_old, routing_new, "thread routing"
    )

    if picker == central:
        picker_source = central_source
    else:
        picker_source = originals[picker]
    picker_source = replace_once(
        module, picker_source, filter_old, filter_new, "model visibility"
    )
    picker_source = replace_once(
        module, picker_source, fallback_old, fallback_new, "model label fallback"
    )

    central.write_text(central_source if picker != central else picker_source, encoding="utf-8")
    if picker != central:
        picker.write_text(picker_source, encoding="utf-8")

    final_source = central.read_text(encoding="utf-8")
    for marker in (
        "__codexDesktopModelProvidersPatchV3",
        "__codexOpenRouterBuild6321PatchV1",
        "modelProvider:`openrouter`",
        "i&&t===`amazonBedrock`?n.has(r.model):!r.hidden",
    ):
        if marker not in final_source:
            raise module.PatchError(f"Required build 6321 marker missing: {marker}")
    return "ChatGPT 26.803.41515 build 6321 OpenRouter dedicated app"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--allow-running", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    module = load_upstream(args.upstream.resolve())
    app = args.app.expanduser().resolve()
    info_path = app / "Contents" / "Info.plist"
    asar_path = app / "Contents" / "Resources" / "app.asar"
    if not info_path.is_file() or not asar_path.is_file():
        raise module.PatchError(f"Not a complete ChatGPT app bundle: {app}")

    info, _ = module.load_plist(info_path)
    version = str(info.get("CFBundleShortVersionString", ""))
    build = str(info.get("CFBundleVersion", ""))
    if (version, build) != (EXPECTED_VERSION, EXPECTED_BUILD):
        raise module.PatchError(
            f"Unsupported app version/build: {version} ({build}); "
            f"expected {EXPECTED_VERSION} ({EXPECTED_BUILD})"
        )

    custom_installed = module.contains_marker(asar_path, CUSTOM_MARKER)
    upstream_marker_present = module.contains_marker(asar_path, module.PATCH_MARKER)
    if custom_installed:
        print(f"OpenRouter build 6321 patch already installed: {app}")
        return 0
    if upstream_marker_present:
        raise module.PatchError(
            "A different provider patch is already present; rebuild from the stock bundle"
        )

    actual_asar = sha256(asar_path)
    if actual_asar != EXPECTED_UNPATCHED_ASAR_SHA256:
        raise module.PatchError(
            "Unpatched ASAR hash mismatch; refusing an ambiguous build-6321 patch: "
            f"{actual_asar}"
        )
    if module.asar_header_hash(asar_path) != module.asar_integrity_hash(info):
        raise module.PatchError("ASAR header integrity does not match Info.plist")

    provider_config = json.loads(args.config.expanduser().resolve().read_text(encoding="utf-8"))
    model_providers = provider_config.get("model_providers")
    if (
        provider_config.get("version") != 1
        or provider_config.get("default_provider") != "openrouter"
        or not isinstance(model_providers, dict)
        or not model_providers
        or set(model_providers.values()) != {"openrouter"}
    ):
        raise module.PatchError("Provider config must map a non-empty verified profile to OpenRouter")
    module.DEFAULT_PROVIDER_CONFIG = provider_config
    module.apply_supported_patch_variant = (
        lambda central, picker: install_build_6321_replacements(
            module, central, picker
        )
    )
    upstream_run = module.run

    def run_without_rolldown_reformat(command, *, cwd=None, label=None):
        if (
            command[:3] == ["npx", "--yes", module.PRETTIER_PACKAGE]
            and "--write" in command
        ):
            module.terminal_status(
                "SKIP",
                "Preserving the verified build-6321 Rolldown bundle layout.",
                "33",
                detail="Prettier is intentionally disabled only for this pinned build adapter",
            )
            return module.subprocess.CompletedProcess(command, 0, "", "")
        return upstream_run(command, cwd=cwd, label=label)

    module.run = run_without_rolldown_reformat

    module.stop_target_app_processes(app, args.allow_running)
    module.patch_app(
        app,
        args.config.expanduser().resolve(),
        args.backup_dir.expanduser().resolve(),
        overwrite_config=False,
    )
    if not module.contains_marker(asar_path, CUSTOM_MARKER):
        raise module.PatchError("Installed ASAR is missing the build 6321 marker")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
