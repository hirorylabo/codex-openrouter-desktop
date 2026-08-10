from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

from .app import AppError, UserPaths, assert_apple_silicon, detect_stock, load_adapter
from .auth import (
    AuthenticationError,
    CredentialStore,
    key_hash,
    oauth_key,
    prompt_for_key,
    temporary_store,
)
from .openrouter import OpenRouterError, validate_key_and_profile
from .profile import ProfileError, ResolvedProfile, resolve_profile


class CliError(RuntimeError):
    pass


def root() -> Path:
    value = os.environ.get("CODEX_OPENROUTER_SOURCE_ROOT")
    if not value:
        raise CliError("source rootが設定されていません")
    return Path(value).resolve()


def profile_path(argument: str, paths: UserPaths) -> Path:
    if argument != "default":
        path = Path(argument).expanduser().resolve()
    elif (paths.codex_home / "profile.json").is_file():
        path = paths.codex_home / "profile.json"
    else:
        path = root() / "profiles/default.json"
    if not path.is_file() or path.is_symlink():
        raise CliError(f"profileが見つからないかsymlinkです: {path}")
    return path


def resolved_profile(argument: str, paths: UserPaths) -> tuple[Path, ResolvedProfile]:
    selected = profile_path(argument, paths)
    return selected, resolve_profile(root() / "models/registry.json", selected)


def credential_store(paths: UserPaths) -> tuple[CredentialStore, object | None]:
    if paths.credential_helper.is_file() and os.access(paths.credential_helper, os.X_OK):
        return CredentialStore(paths.credential_helper), None
    store, temporary = temporary_store(root() / "portable/credential/CredentialHelper.swift")
    return store, temporary


def obtain_key(method: str) -> str:
    return oauth_key() if method == "oauth" else prompt_for_key()


def wait_for_openrouter_policy(method: str) -> None:
    if method != "oauth":
        return
    print("OAuthで作成した最新のcodex-openrouter-desktop keyへ、OpenRouter画面でGuardrailを割り当ててください。")
    print("Guardrails: https://openrouter.ai/settings/guardrails")
    print("Keys: https://openrouter.ai/settings/keys")
    print("Privacy/ZDR: https://openrouter.ai/settings/privacy")
    input("exact model allowlist・Non-frontier ZDR・training/logging設定を確認後、Enter: ")


def warn_limit(metadata: dict) -> None:
    if metadata.get("limit") is None:
        print("WARNING: このAPI keyにはspend limitが設定されていません。OpenRouter Keys画面で設定を推奨します。")


def check_command(args: argparse.Namespace) -> int:
    paths = UserPaths.current()
    assert_apple_silicon()
    stock = detect_stock(paths.stock_app)
    selected, profile = resolved_profile(args.profile, paths)
    adapter = load_adapter(root() / "adapters/index.json", stock)
    print(f"architecture=arm64")
    print(f"ChatGPT={stock.version} build {stock.build}")
    print(f"stock_asar={stock.asar_sha256}")
    print(f"profile={selected} models={len(profile.models)} default={profile.default_model}")
    if adapter:
        print(f"adapter={adapter['id']} strategy={adapter['patch_strategy']}")
    else:
        print("adapter=unknown (update will create a candidate only)")
    if paths.credential_helper.is_file():
        print(f"keychain={'available' if CredentialStore(paths.credential_helper).exists() else 'missing'}")
    else:
        print("keychain=helper-not-installed")
    print("CHECK: PASS (no persistent files changed)")
    return 0


def setup_command(args: argparse.Namespace) -> int:
    paths = UserPaths.current()
    assert_apple_silicon()
    stock = detect_stock(paths.stock_app)
    adapter = load_adapter(root() / "adapters/index.json", stock)
    if adapter is None:
        raise CliError(
            "未知buildです。v0.1.0では先に既存installationからcodex-openrouter updateを実行し、candidateを目視承認してください"
        )
    selected, profile = resolved_profile(args.profile, paths)
    store, temporary = credential_store(paths)
    try:
        if store.exists():
            key = store.get()
            print("既存のmacOS Keychain credentialを検証します。")
        else:
            key = obtain_key(args.auth)
            wait_for_openrouter_policy(args.auth)
        metadata = validate_key_and_profile(key, set(profile.models))
        warn_limit(metadata)
        if not store.exists():
            store.store(key)
            print("OpenRouter API keyをmacOS Keychainへ保存しました。")
    finally:
        if temporary is not None:
            temporary.cleanup()
    command = [
        str(root() / "portable/install.sh"),
        "--install",
        "--workspace",
        str(Path(args.workspace).expanduser().resolve()),
        "--profile",
        str(selected),
    ]
    print("INFO: setup終盤のnetwork canaryは少量のOpenRouter API利用料が発生する場合があります。")
    return subprocess.run(command).returncode


def delegate(executable: Path, arguments: list[str]) -> int:
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise CliError(f"未インストールです: {executable}")
    return subprocess.run([str(executable), *arguments]).returncode


def auth_command(args: argparse.Namespace) -> int:
    paths = UserPaths.current()
    store, temporary = credential_store(paths)
    try:
        if args.auth_action == "logout":
            if not store.exists():
                print("macOS KeychainにOpenRouter credentialはありません。")
                return 0
            digest = key_hash(store.get())[:12]
            confirmation = input("ローカルKeychain credentialだけを削除します。LOGOUTと入力して確認: ")
            if confirmation != "LOGOUT":
                raise CliError("logoutを中止しました")
            store.delete()
            print(f"ローカルcredentialを削除しました (key fingerprint sha256:{digest})。")
            print("OpenRouter側のkey失効は https://openrouter.ai/settings/keys で行ってください。")
            return 0

        _, profile = resolved_profile("default", paths)
        if args.auth_action == "rotate" and not store.exists():
            raise CliError("rotate対象のローカルcredentialがありません。auth loginを使用してください")
        key = obtain_key(args.method)
        wait_for_openrouter_policy(args.method)
        metadata = validate_key_and_profile(key, set(profile.models))
        warn_limit(metadata)
        store.store(key)
        print("OpenRouter API keyをmacOS Keychainへ保存しました。")
        return 0
    finally:
        if temporary is not None:
            temporary.cleanup()


def rollback_command(_args: argparse.Namespace) -> int:
    paths = UserPaths.current()
    backup_root = paths.home / "Applications/ChatGPT OpenRouter Backups"
    backups = sorted(backup_root.glob("*.app"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not backups:
        raise CliError(f"復元可能なbackup appがありません: {backup_root}")
    if paths.openrouter_app.exists():
        active_process = subprocess.run(
            ["/bin/ps", "-axo", "command="], text=True, stdout=subprocess.PIPE
        ).stdout
        if str(paths.openrouter_app / "Contents/MacOS/ChatGPT") in active_process:
            raise CliError("専用appを通常終了してからrollbackしてください")
    source = backups[0]
    metadata = None
    marker = "ChatGPT OpenRouter.pre-candidate-"
    if source.name.startswith(marker) and source.name.endswith(".app"):
        timestamp = source.name[len(marker) : -len(".app")]
        candidate_metadata = backup_root / f"metadata-pre-candidate-{timestamp}"
        if not candidate_metadata.is_dir():
            raise CliError(f"candidate backupと組になるmetadataがありません: {candidate_metadata}")
        metadata = candidate_metadata
    confirmation = input(f"{source.name} を復元します。ROLLBACKと入力して確認: ")
    if confirmation != "ROLLBACK":
        raise CliError("rollbackを中止しました")
    timestamp = subprocess.check_output(["/bin/date", "+%Y%m%d-%H%M%S"], text=True).strip()
    if paths.openrouter_app.exists():
        preserved = backup_root / f"ChatGPT OpenRouter.failed-before-rollback-{timestamp}.app"
        paths.openrouter_app.rename(preserved)
    subprocess.run(["/usr/bin/ditto", str(source), str(paths.openrouter_app)], check=True)
    if metadata is not None:
        from .candidate import restore_live_state

        restore_live_state(paths, metadata)
    print(f"復元しました: {source}")
    return delegate(paths.bin_dir / "codex-openrouter-doctor", [])


def update_command(args: argparse.Namespace) -> int:
    from .candidate import update

    return update(root(), UserPaths.current(), args.profile)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-openrouter")
    subcommands = parser.add_subparsers(dest="command", required=True)

    check = subcommands.add_parser("check")
    check.add_argument("--profile", default="default")
    check.set_defaults(func=check_command)

    setup = subcommands.add_parser("setup")
    setup.add_argument("--workspace", default=str(Path.home() / "Documents"))
    setup.add_argument("--profile", default="default")
    setup.add_argument("--auth", choices=("oauth", "paste"), default="oauth")
    setup.set_defaults(func=setup_command)

    launch = subcommands.add_parser("launch")
    launch.add_argument("path", nargs="?", default=os.getcwd())
    launch.set_defaults(func=lambda args: delegate(UserPaths.current().bin_dir / "codex-openrouter-app", [args.path]))

    doctor = subcommands.add_parser("doctor")
    doctor.add_argument("--network", action="store_true")
    doctor.add_argument("--runtime", action="store_true")
    doctor.add_argument("--secret-scan", action="store_true")
    doctor.set_defaults(
        func=lambda args: delegate(
            UserPaths.current().bin_dir / "codex-openrouter-doctor",
            [flag for flag, enabled in (("--network", args.network), ("--runtime", args.runtime), ("--secret-scan", args.secret_scan)) if enabled],
        )
    )

    update_parser = subcommands.add_parser("update")
    update_parser.add_argument("--profile", default="default")
    update_parser.set_defaults(func=update_command)

    rollback = subcommands.add_parser("rollback")
    rollback.set_defaults(func=rollback_command)

    auth = subcommands.add_parser("auth")
    auth_subcommands = auth.add_subparsers(dest="auth_action", required=True)
    for action in ("login", "rotate"):
        child = auth_subcommands.add_parser(action)
        child.add_argument("--method", choices=("oauth", "paste"), default="oauth")
        child.set_defaults(func=auth_command)
    logout = auth_subcommands.add_parser("logout")
    logout.set_defaults(func=auth_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return int(args.func(args))
    except (AppError, AuthenticationError, OpenRouterError, ProfileError, CliError, OSError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
