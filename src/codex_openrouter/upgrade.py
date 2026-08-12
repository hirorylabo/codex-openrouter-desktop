"""runtimeファイルの原子的な更新。

案Dでは純正appを一切変更しないので、ASARパッチ・adapter・vendored patcherの
取得・cloneバンドルの差し替えは全て無くなった。ここで扱うのは
「このツール自身のruntimeファイル」だけ。

- support root（src / models / profiles / portable）
- ~/.local/bin の実行ファイルとKeychain helper
- Desktop の Codex OpenRouter.app（ランチャー専用バンドル）

置換は promotion.atomic_promote に任せる。verifyが落ちれば全部戻る。
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
from .app import UserPaths, detect_stock
from .auth import CredentialStore
from .openrouter import validate_key_and_profile
from .processes import process_pids
from .profile import resolve_profile
from .promotion import atomic_promote


class UpgradeError(RuntimeError):
    pass


BINARIES = (
    "codex-openrouter",
    "codex-openrouter-credential",
    "codex-openrouter-doctor",
    "codex-openrouter-app",
)


def run(command: list[str], *, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode != 0:
        raise UpgradeError(
            f"command failed ({result.returncode}): {Path(command[0]).name}\n{result.stdout[-4000:]}"
        )
    return result.stdout


def copy_support(source_root: Path, target: Path) -> None:
    target.mkdir()
    ignored = shutil.ignore_patterns(
        "node_modules", "__pycache__", ".generated", ".test-output", "dist"
    )
    for name in ("src", "models", "profiles", "portable"):
        shutil.copytree(
            source_root / name,
            target / name,
            copy_function=shutil.copy2,
            ignore=ignored,
        )
    for name in ("codex-openrouter", "VERSION"):
        shutil.copy2(source_root / name, target / name)
    (target / "codex-openrouter").chmod(0o755)


def render_template(source: Path, target: Path, home: Path, python: str) -> None:
    rendered = source.read_text(encoding="utf-8").replace("@@USER_HOME@@", str(home)).replace(
        "@@PYTHON@@", python
    )
    if "@@" in rendered:
        raise UpgradeError(f"未解決template placeholderがあります: {source}")
    target.write_text(rendered, encoding="utf-8")
    target.chmod(0o755)


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    path.chmod(0o600)


def build_launcher(source_root: Path, target: Path, workspace: Path, log_path: Path) -> None:
    """Desktopに置くランチャー専用バンドルを組み立てる。

    純正appのcloneではない。事前処理をしてから /Applications/ChatGPT.app を
    起動するだけの薄いバンドル。

    workspaceとlog pathはInfo.plistへ焼き込む。Swift側に同じpathを書くと、
    Pythonが出所であるはずのpathが二重定義になり、v0.2.0のように片方だけ
    取り残される。
    """
    executable = target / "Contents/MacOS/CodexOpenRouterLauncher"
    executable.parent.mkdir(parents=True)
    resources = target / "Contents/Resources"
    resources.mkdir()
    plist = target / "Contents/Info.plist"
    shutil.copy2(source_root / "portable/launcher/Info.plist", plist)
    for key in ("CFBundleShortVersionString", "CFBundleVersion"):
        run(["/usr/bin/plutil", "-replace", key, "-string", __version__, str(plist)])
    run(
        [
            str(source_root / "portable/launcher/build_icon.zsh"),
            str(source_root / "portable/launcher/CreateLauncherIcon.swift"),
            str(resources / "AppIcon.icns"),
        ]
    )
    for key, value in (
        ("CodexDefaultWorkspace", str(workspace)),
        ("CodexLauncherLog", str(log_path)),
    ):
        run(["/usr/bin/plutil", "-replace", key, "-string", value, str(plist)])
    run(
        [
            "/usr/bin/xcrun",
            "swiftc",
            str(source_root / "portable/launcher/CodexOpenRouterLauncher.swift"),
            "-o",
            str(executable),
        ]
    )
    run(["/usr/bin/codesign", "--force", "--sign", "-", str(target)])
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(target)])


def upgrade(source_root: Path, paths: UserPaths, profile_argument: str) -> int:
    if not paths.bin_dir.is_dir():
        raise UpgradeError("既存installationがありません。先にsetupを実行してください")
    if process_pids(paths.stock_app / "Contents/MacOS/ChatGPT"):
        raise UpgradeError("ChatGPT.appを終了してからupgradeしてください")
    stock = detect_stock(paths.stock_app)

    profile_path = (
        source_root / "profiles/default.json"
        if profile_argument == "default"
        else Path(profile_argument).expanduser().resolve()
    )
    profile = resolve_profile(source_root / "models/registry.json", profile_path)
    key = CredentialStore(paths.credential_helper).get()
    print("INFO: upgrade検証は少量のOpenRouter API利用料が発生する場合があります。")
    metadata = validate_key_and_profile(key, set(profile.models))
    if metadata.get("limit") is None:
        print("WARNING: API keyのspend limitが未設定です。")

    python = shutil.which("python3") or "/usr/bin/python3"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = paths.state_dir / "upgrade-backups" / f"{timestamp}-v{__version__}"
    workspace = paths.home / "Documents"
    receipt = paths.state_dir / "install-manifest.json"
    if receipt.is_file() and not receipt.is_symlink():
        saved = json.loads(receipt.read_text(encoding="utf-8")).get("workspace")
        if isinstance(saved, str) and Path(saved).is_dir():
            workspace = Path(saved)

    with tempfile.TemporaryDirectory(prefix="codex-openrouter-upgrade-") as temporary:
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

        commit_result = subprocess.run(
            ["/usr/bin/git", "-C", str(source_root), "rev-parse", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        source_commit = (
            commit_result.stdout.strip()
            if commit_result.returncode == 0 and commit_result.stdout.strip()
            else "release-archive"
        )
        _write_json(
            stage_state / "install-manifest.json",
            {
                "schema_version": 3,
                "release_version": __version__,
                "source_commit": source_commit,
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
        run(
            [
                python,
                "-m",
                "py_compile",
                str(stage_bin / "codex-openrouter-doctor"),
            ]
        )
        run(["/bin/zsh", "-n", str(stage_bin / "codex-openrouter-app")])

        build_launcher(source_root, stage_launcher, workspace, paths.state_dir / "logs/launcher.log")

        replacements: list[tuple[Path, Path]] = [(stage_support, paths.support_root)]
        replacements.extend((stage_bin / name, paths.bin_dir / name) for name in BINARIES)
        replacements.append((stage_launcher, paths.desktop_launcher))
        replacements.append(
            (stage_state / "install-manifest.json", paths.state_dir / "install-manifest.json")
        )

        def verify_live() -> None:
            environment = {**os.environ, "PYTHONPATH": str(paths.support_root / "src")}
            run([str(paths.bin_dir / "codex-openrouter-doctor"), "--secret-scan"], environment=environment)
            if detect_stock(paths.stock_app) != stock:
                raise UpgradeError("upgrade中にstock appが変化しました")

        atomic_promote(replacements, backup_root, verify_live)
    print(f"UPGRADE: PASS v{__version__} mode=loopback-guard")
    print(f"rollback backup: {backup_root}")
    return 0
