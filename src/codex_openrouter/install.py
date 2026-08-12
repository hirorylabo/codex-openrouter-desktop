"""初回インストールと更新を1本の経路にまとめる。

v0.1.xでは `portable/install.sh`（初回）と `upgrade.py`（更新）が別々に同じことを
していた。案Dでは純正appを触らないので両者の差は「既存targetがあるか」だけになり、
経路を2本持つ理由が無くなった。

staging → 検証 → `promotion.atomic_promote` で置換、という流れは共通。
verifyが落ちれば全targetが元へ戻る。
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from . import __version__
from .app import UserPaths, assert_apple_silicon, detect_stock
from .auth import CredentialStore
from .openrouter import validate_key_and_profile
from .processes import process_pids
from .profile import resolve_profile
from .promotion import atomic_promote
from .supervisor import DEFAULT_PORT, Supervisor
from .upgrade import BINARIES, UpgradeError, build_launcher, copy_support, render_template, run

# v0.1.xの成果物。案Dでは使わないので掃除する。
RETIRED_BINARIES = ("codex-openrouter-rebuild", "codex-openrouter-refresh")


class InstallError(RuntimeError):
    pass


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    path.chmod(0o600)


def preflight(paths: UserPaths, source_root: Path, profile_argument: str):
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

    profile_path = (
        source_root / "profiles/default.json"
        if profile_argument == "default"
        else Path(profile_argument).expanduser().resolve()
    )
    profile = resolve_profile(source_root / "models/registry.json", profile_path)
    return stock, profile


def install(
    source_root: Path,
    paths: UserPaths,
    profile_argument: str = "default",
    workspace: Path | None = None,
    *,
    network_check: bool = True,
) -> int:
    """初回・更新の両方をこの1本で扱う。"""
    stock, profile = preflight(paths, source_root, profile_argument)
    first_time = not paths.support_root.exists()

    key = CredentialStore(paths.credential_helper).get() if paths.credential_helper.is_file() else None
    if key is None:
        raise InstallError(
            "Keychainにcredential helperがありません。先に codex-openrouter auth login を実行してください"
        )
    if network_check:
        print("INFO: 導入検証は少量のOpenRouter API利用料が発生する場合があります。")
        metadata = validate_key_and_profile(key, set(profile.models))
        if metadata.get("limit") is None:
            print("WARNING: API keyのspend limitが未設定です。")

    python = shutil.which("python3") or "/usr/bin/python3"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = paths.state_dir / "upgrade-backups" / f"{timestamp}-v{__version__}"

    receipt = paths.state_dir / "install-manifest.json"
    if workspace is None:
        workspace = paths.home / "Documents"
        if receipt.is_file() and not receipt.is_symlink():
            saved = json.loads(receipt.read_text(encoding="utf-8")).get("workspace")
            if isinstance(saved, str) and Path(saved).is_dir():
                workspace = Path(saved)
    workspace.mkdir(parents=True, exist_ok=True)

    for directory in (paths.bin_dir, paths.state_dir, paths.support_root.parent):
        directory.mkdir(parents=True, exist_ok=True)
    paths.state_dir.chmod(0o700)

    with tempfile.TemporaryDirectory(prefix="codex-openrouter-install-") as temporary:
        stage = Path(temporary).resolve()
        stage_support = stage / "support"
        stage_bin = stage / "bin"
        stage_state = stage / "state"
        stage_launcher = stage / "Codex OpenRouter.app"
        for directory in (stage_bin, stage_state):
            directory.mkdir()
        copy_support(source_root, stage_support)

        credential = stage_bin / "codex-openrouter-credential"
        run(
            [
                "/usr/bin/xcrun",
                "swiftc",
                str(source_root / "portable/credential/CredentialHelper.swift"),
                "-o",
                str(credential),
            ]
        )
        run([str(credential), "status"])

        commit = subprocess.run(
            ["/usr/bin/git", "-C", str(source_root), "rev-parse", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        _write_json(
            stage_state / "install-manifest.json",
            {
                "schema_version": 3,
                "release_version": __version__,
                "source_commit": commit.stdout.strip() or "release-archive",
                "chatgpt_version": stock.version,
                "chatgpt_build": stock.build,
                "workspace": str(workspace),
                "mode": "loopback-guard",
            },
        )

        shutil.copy2(source_root / "codex-openrouter", stage_bin / "codex-openrouter")
        (stage_bin / "codex-openrouter").chmod(0o755)
        for template, target in (
            ("codex-openrouter-doctor.py.in", "codex-openrouter-doctor"),
            ("codex-openrouter-app.zsh.in", "codex-openrouter-app"),
        ):
            render_template(
                source_root / "portable/templates" / template,
                stage_bin / target,
                paths.home,
                python,
            )
        run([python, "-m", "py_compile", str(stage_bin / "codex-openrouter-doctor")])
        run(["/bin/zsh", "-n", str(stage_bin / "codex-openrouter-app")])

        build_launcher(source_root, stage_launcher, workspace, paths.state_dir / "logs/launcher.log")

        replacements: list[tuple[Path, Path]] = [(stage_support, paths.support_root)]
        replacements.extend((stage_bin / name, paths.bin_dir / name) for name in BINARIES)
        replacements.append((stage_launcher, paths.desktop_launcher))
        replacements.append((stage_state / "install-manifest.json", receipt))

        def verify() -> None:
            environment = {**os.environ, "PYTHONPATH": str(paths.support_root / "src")}
            run(
                [str(paths.bin_dir / "codex-openrouter-doctor"), "--secret-scan"],
                environment=environment,
            )
            if detect_stock(paths.stock_app) != stock:
                raise UpgradeError("導入中にstock appが変化しました")

        atomic_promote(replacements, backup_root, verify)

    # B block（provider定義）は永続。ここで入れておけば、まだ一度も
    # ランチャーを使っていなくても旧OpenRouter threadのresumeが壊れない。
    if paths.shared_config.is_file():
        Supervisor(paths, source_root / "models/registry.json").ensure_provider_block(DEFAULT_PORT)

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
