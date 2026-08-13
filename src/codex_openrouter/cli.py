from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .app import AppError, UserPaths, assert_apple_silicon, detect_stock, stock_build_id
from .auth import (
    AuthenticationError,
    CredentialStore,
    key_hash,
    oauth_key,
    prompt_for_key,
    temporary_store,
)
from . import configblock
from .lifecycle import LifecycleLock
from .openrouter import OpenRouterError, validate_key_and_profile
from .processes import ProcessError, process_pids
from .profile import ProfileError, ResolvedProfile, resolve_profile, select_profile_path


class CliError(RuntimeError):
    pass


def root() -> Path:
    value = os.environ.get("CODEX_OPENROUTER_SOURCE_ROOT")
    if not value:
        raise CliError("source rootが設定されていません")
    return Path(value).resolve()


def profile_path(argument: str | None, paths: UserPaths) -> Path:
    return select_profile_path(
        argument=argument,
        source_default=root() / "profiles/default.json",
        installed=paths.installed_profile,
        legacy=paths.codex_home / "profile.json",
    )


def resolved_profile(argument: str | None, paths: UserPaths) -> tuple[Path, ResolvedProfile]:
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
    version, build = stock_build_id(paths.stock_app)
    selected, profile = resolved_profile(args.profile, paths)
    print("architecture=arm64")
    print(f"ChatGPT={version} build {build}  (純正appは変更しません)")
    print(f"profile={selected} models={len(profile.models)} default={profile.default_model}")
    print(f"shared_home={paths.shared_home}")
    config = paths.shared_config
    if config.is_file():
        text = config.read_text(encoding="utf-8")
        print(f"catalog_block={'present' if configblock.has_block(text, 'catalog') else 'absent'}")
        print(f"provider_block={'present' if configblock.has_block(text, 'provider') else 'absent'}")
        print(f"model={configblock.read_top_level(text, 'model')}")
        print(f"model_provider={configblock.read_top_level(text, 'model_provider')}")
    else:
        print("shared config=missing (純正appを一度起動してください)")
    if paths.credential_helper.is_file():
        print(f"keychain={'available' if CredentialStore(paths.credential_helper).exists() else 'missing'}")
    else:
        print("keychain=helper-not-installed")
    print("CHECK: PASS (no persistent files changed)")
    return 0


def setup_command(args: argparse.Namespace) -> int:
    paths = UserPaths.current()
    with LifecycleLock(paths):
        return _setup_locked(args, paths)


def _setup_locked(args: argparse.Namespace, paths: UserPaths) -> int:
    assert_apple_silicon()
    detect_stock(paths.stock_app)
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
    from .install import _install_unlocked

    return _install_unlocked(
        root(),
        paths,
        args.profile,
        workspace=Path(args.workspace).expanduser().resolve(),
        network_check=False,
    )


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

        _, profile = resolved_profile(None, paths)
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
    """runtimeファイルをupgrade前へ戻す。

    案Dでは純正appを触らないので、復元対象はこのツール自身のファイルだけ。
    """
    from .promotion import atomic_promote, rollback_replacements

    paths = UserPaths.current()
    with LifecycleLock(paths):
        return _rollback_locked(paths)


def _rollback_locked(paths: UserPaths) -> int:
    upgrade_root = paths.state_dir / "upgrade-backups"
    backups = sorted(
        (
            path
            for path in upgrade_root.glob("*")
            if path.is_dir() and (path / "promotion.json").is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not backups:
        raise CliError("復元可能なupgrade backupがありません")
    if process_pids(paths.stock_app / "Contents/MacOS/ChatGPT"):
        raise CliError("ChatGPT.appを終了してからrollbackしてください")
    source_backup = backups[0]
    confirmation = input(
        f"{source_backup.name} のupgrade前状態へ戻します。ROLLBACKと入力して確認: "
    )
    if confirmation != "ROLLBACK":
        raise CliError("rollbackを中止しました")
    timestamp = subprocess.check_output(["/bin/date", "+%Y%m%d-%H%M%S"], text=True).strip()
    rollback_backup = upgrade_root / f"manual-rollback-{timestamp}"

    def verify() -> None:
        from .supervisor import Supervisor

        registry_path = paths.support_root / "models/registry.json"
        selected = select_profile_path(
            argument=None,
            source_default=paths.support_root / "profiles/default.json",
            installed=paths.installed_profile,
            legacy=paths.codex_home / "profile.json",
        )
        profile = resolve_profile(registry_path, selected)
        if paths.shared_config.is_file():
            Supervisor(paths, registry_path, profile=profile).self_heal()
        if subprocess.run([str(paths.bin_dir / "codex-openrouter-doctor")]).returncode != 0:
            raise CliError("復元後doctorに失敗しました")

    atomic_promote(rollback_replacements(source_backup), rollback_backup, verify)
    print(f"upgrade前状態を復元しました: {source_backup}")
    print(f"rollback直前状態のbackup: {rollback_backup}")
    return 0


def launch_command(args: argparse.Namespace) -> int:
    """事前処理をしてから純正appを起動し、終了したら後始末する。"""
    from .supervisor import Supervisor

    paths = UserPaths.current()
    assert_apple_silicon()
    workspace = Path(args.path).expanduser().resolve() if args.path else None
    _selected, profile = resolved_profile(None, paths)
    return Supervisor(
        paths,
        root() / "models/registry.json",
        profile=profile,
        workspace=workspace,
    ).run()


def migrate_command(args: argparse.Namespace) -> int:
    """旧clone方式(v0.1.x)から案Dへ移行する。

    旧 ~/.codex-openrouter は消さない。OpenRouterで記録した旧threadがあるため、
    読み取り専用のbackupとして残す。
    """
    from .supervisor import Supervisor

    paths = UserPaths.current()
    with LifecycleLock(paths):
        return _migrate_locked(args, paths)


def _migrate_locked(args: argparse.Namespace, paths: UserPaths) -> int:
    actions: list[str] = []
    if process_pids(paths.openrouter_app / "Contents/MacOS/ChatGPT"):
        raise CliError("旧専用appを終了してからmigrateしてください")

    if not paths.shared_config.is_file():
        raise CliError(
            f"{paths.shared_config} がありません。純正ChatGPT.appを一度起動してください。"
        )

    _selected, profile = resolved_profile(None, paths)
    supervisor = Supervisor(paths, root() / "models/registry.json", profile=profile)
    supervisor.self_heal()
    actions.append("[model_providers.openrouter] を ~/.codex/config.toml へ永続化しました")

    if paths.openrouter_app.exists():
        confirmation = input(
            f"旧専用app {paths.openrouter_app} を削除します。MIGRATEと入力して確認: "
        )
        if confirmation != "MIGRATE":
            raise CliError("migrateを中止しました")
        shutil.rmtree(paths.openrouter_app)
        actions.append(f"旧専用appを削除しました: {paths.openrouter_app}")

    if paths.codex_home.is_dir():
        if getattr(args, "keep_all", False):
            actions.append(f"旧home {paths.codex_home} はそのまま残しました")
        else:
            freed = compact_legacy_home(paths.codex_home)
            actions.append(
                f"旧home を sessions だけ残して圧縮しました（{freed} 解放 / {paths.codex_home}）"
            )
    for message in actions:
        print(f"- {message}")
    print("MIGRATE: PASS")
    return 0


# 案Dで参照されなくなる旧home配下。sessionsとhistoryだけ残す。
LEGACY_DISPOSABLE = (
    "candidates",      # ASARパッチ方式のcandidate clone群。大半の容量はここ
    "user-data",       # 旧clone appのElectron userData
    "plugins",
    "computer-use",
    "upgrade-backups",
    "logs",
    "cache",
    "tmp",
    "backups",
    "archived_worktrees",
    "worktrees",
)


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}B"
        size /= 1024
    return f"{size:.1f}GB"


def compact_legacy_home(home: Path) -> str:
    """旧homeを sessions 中心に絞る。sessionsとhistoryには触らない。"""
    freed = 0
    for name in LEGACY_DISPOSABLE:
        target = home / name
        if not target.is_dir() or target.is_symlink():
            continue
        for path in target.rglob("*"):
            if path.is_file() and not path.is_symlink():
                freed += path.stat().st_size
        shutil.rmtree(target)
    # logsのsqliteだけ消す。memories/goals/stateは小さく、かつ利用者の内容なので残す。
    for path in home.glob("logs_*.sqlite*"):
        if path.is_file():
            freed += path.stat().st_size
            path.unlink()
    return _human(freed)


def guard_log_command(args: argparse.Namespace) -> int:
    """guardが弾いたmodelを集計する（巻き込みスキャン）。

    Codexの更新ごとに、巻き込まれる背景機能の増減を追うために使う。
    """
    paths = UserPaths.current()
    if not paths.guard_log.is_file():
        print(f"guard logがありません: {paths.guard_log}")
        return 0
    denied: dict[str, int] = {}
    forwarded: dict[str, int] = {}
    for line in paths.guard_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        bucket = denied if record.get("decision") == "denied" else forwarded
        model = str(record.get("model"))
        bucket[model] = bucket.get(model, 0) + 1
    print(f"guard log: {paths.guard_log}")
    print("--- 中継した (OpenRouter) ---")
    for model, count in sorted(forwarded.items(), key=lambda item: -item[1]):
        print(f"  {count:6d}  {model}")
    print("--- 遮断した (巻き込み) ---")
    for model, count in sorted(denied.items(), key=lambda item: -item[1]):
        print(f"  {count:6d}  {model}")
    if not denied:
        print("  (なし)")
    if args.clear:
        paths.guard_log.unlink()
        print("guard logを消去しました。")
    return 0


def upgrade_command(args: argparse.Namespace) -> int:
    from .upgrade import auto_upgrade, upgrade

    paths = UserPaths.current()
    if args.if_needed:
        return auto_upgrade(paths, args.profile)
    source_root = root()
    if source_root == paths.support_root:
        # PATH上の codex-openrouter は source root をインストール先へ解決する。
        # そこから打つと自分自身を再インストールするだけで何も新しくならない。
        print(
            "WARNING: インストール済みツリー自身をsourceにしています。"
            "新しい内容を反映するにはリポジトリの ./codex-openrouter を使ってください。"
        )
    return upgrade(source_root, paths, args.profile)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-openrouter")
    subcommands = parser.add_subparsers(dest="command", required=True)

    check = subcommands.add_parser("check")
    check.add_argument("--profile", default=None)
    check.set_defaults(func=check_command)

    setup = subcommands.add_parser("setup")
    setup.add_argument("--workspace", default=str(Path.home() / "Documents"))
    setup.add_argument("--profile", default=None)
    setup.add_argument("--auth", choices=("oauth", "paste"), default="oauth")
    setup.set_defaults(func=setup_command)

    launch = subcommands.add_parser("launch")
    launch.add_argument("path", nargs="?", default=os.getcwd())
    launch.set_defaults(func=launch_command)

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

    migrate = subcommands.add_parser("migrate")
    migrate.add_argument("--keep-all", action="store_true",
                         help="旧 ~/.codex-openrouter を圧縮せずそのまま残す")
    migrate.set_defaults(func=migrate_command)

    guard_log = subcommands.add_parser("guard-log")
    guard_log.add_argument("--clear", action="store_true")
    guard_log.set_defaults(func=guard_log_command)

    upgrade_parser = subcommands.add_parser("upgrade")
    upgrade_parser.add_argument("--profile", default=None)
    upgrade_parser.add_argument(
        "--if-needed",
        action="store_true",
        help="導入元に差分があるときだけ更新する（ランチャーがクリック時に使う）",
    )
    upgrade_parser.set_defaults(func=upgrade_command)

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
    from .install import InstallError
    from .lifecycle import LifecycleLockError
    from .promotion import PromotionError
    from .supervisor import SupervisorError
    from .upgrade import UpgradeError

    try:
        args = build_parser().parse_args(argv)
        return int(args.func(args))
    except (
        AppError,
        AuthenticationError,
        configblock.ConfigBlockError,
        InstallError,
        LifecycleLockError,
        OpenRouterError,
        ProfileError,
        ProcessError,
        PromotionError,
        SupervisorError,
        UpgradeError,
        CliError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # ランチャーは `python3 -m codex_openrouter.cli launch` で入る。
    # このguardが無いとmoduleを読み込むだけで何もせず終了する。
    os.environ.setdefault(
        "CODEX_OPENROUTER_SOURCE_ROOT", str(Path(__file__).resolve().parents[2])
    )
    raise SystemExit(main())
