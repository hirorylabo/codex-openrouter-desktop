"""初回インストールと更新を1本の経路にまとめる。

v0.1.xでは `portable/install.sh`（初回）と `upgrade.py`（更新）が別々に同じことを
していた。案Dでは純正appを触らないので両者の差は「既存targetがあるか」だけになり、
経路を2本持つ理由が無くなった。

staging → 検証 → `promotion.atomic_promote` で置換、という流れは共通。
verifyが落ちれば全targetが元へ戻る。
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from . import __version__
from .app import UserPaths, assert_apple_silicon, detect_stock, installed_workspace
from .auth import temporary_store
from .lifecycle import LifecycleLock
from .openrouter import validate_key_and_profile
from .processes import process_pids
from .upgrade import (
    promote_runtime,
    selected_profile,
)

# v0.1.xの成果物。案Dでは使わないので掃除する。
RETIRED_BINARIES = ("codex-openrouter-rebuild", "codex-openrouter-refresh")


class InstallError(RuntimeError):
    pass


def preflight(paths: UserPaths, source_root: Path, profile_argument: str | None):
    """書き込む前に落ちるべきものを全部落とす。"""
    assert_apple_silicon()
    if subprocess.run(
        ["/usr/bin/xcrun", "--find", "swiftc"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode != 0:
        raise InstallError("Xcode Command Line Toolsが必要です")
    if process_pids(paths.stock_app / "Contents/MacOS/ChatGPT"):
        raise InstallError("ChatGPT.appを終了してから実行してください")
    stock = detect_stock(paths.stock_app)

    _profile_path, profile = selected_profile(source_root, paths, profile_argument)
    return stock, profile


def install(
    source_root: Path,
    paths: UserPaths,
    profile_argument: str | None = None,
    workspace: Path | None = None,
    *,
    network_check: bool = True,
) -> int:
    """初回・更新処理を共有lifecycle lock内で実行する。"""
    with LifecycleLock(paths):
        return _install_unlocked(
            source_root,
            paths,
            profile_argument,
            workspace,
            network_check=network_check,
        )


def _install_unlocked(
    source_root: Path,
    paths: UserPaths,
    profile_argument: str | None = None,
    workspace: Path | None = None,
    *,
    network_check: bool = True,
) -> int:
    """lock取得済みの初回・更新共通処理。"""
    stock, profile = preflight(paths, source_root, profile_argument)
    first_time = not paths.support_root.exists()

    if network_check:
        # 初回setupでは恒久helperがまだ無い。一時helperも同じKeychain serviceを
        # 参照するため、導入順序へ依存せず検証できる。
        store, temporary = temporary_store(
            source_root / "portable/credential/CredentialHelper.swift"
        )
        try:
            key = store.get()
            print("INFO: 導入検証は少量のOpenRouter API利用料が発生する場合があります。")
            metadata = validate_key_and_profile(key, set(profile.models))
            if metadata.get("limit") is None:
                print("WARNING: API keyのspend limitが未設定です。")
        finally:
            temporary.cleanup()

    if workspace is None:
        workspace = installed_workspace(paths)
    workspace.mkdir(parents=True, exist_ok=True)
    backup_root = promote_runtime(source_root, paths, stock, workspace, profile)

    removed = cleanup_legacy(paths)
    verb = "INSTALL" if first_time else "UPGRADE"
    print(f"{verb}: PASS v{__version__} mode=loopback-guard")
    for item in removed:
        print(f"  掃除しました: {item}")
    print(f"  CLI: {paths.bin_dir / 'codex-openrouter'}")
    print(f"  Launcher: {paths.desktop_launcher}")
    print(f"  rollback backup: {backup_root}")
    return 0


def cleanup_legacy(paths: UserPaths) -> list[str]:
    """v0.1.xの成果物のうち、案Dで使わないものを消す。

    旧clone appと旧homeはここでは触らない（利用者データがあるため migrate の担当）。
    """
    removed: list[str] = []
    for name in RETIRED_BINARIES:
        target = paths.bin_dir / name
        if target.exists() or target.is_symlink():
            target.unlink()
            removed.append(str(target))
    patcher_root = paths.home / ".local/share/codex-openrouter-patcher"
    if patcher_root.is_dir():
        shutil.rmtree(patcher_root)
        removed.append(str(patcher_root))
    return removed
