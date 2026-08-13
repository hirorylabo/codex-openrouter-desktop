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
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from . import __version__
from .app import (
    UserPaths,
    assert_apple_silicon,
    detect_stock,
    installed_workspace,
    write_json,
)
from .auth import CredentialStore
from . import configblock
from .lifecycle import LifecycleLock, LifecycleLockError
from .openrouter import validate_key_and_profile
from .processes import process_pids
from .profile import ResolvedProfile, installed_profile
from .promotion import atomic_promote
from .supervisor import State, Supervisor, provider_block_body


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


# support rootへ運ぶもの。copy_support と runtime_digest が同じ一覧を見ることが
# 要点で、片方だけ増えると「変わったのに変わっていない」と判定される。
SUPPORT_TREES = ("src", "models", "profiles", "portable")
SUPPORT_FILES = ("codex-openrouter", "VERSION")
IGNORED_PARTS = frozenset({"node_modules", "__pycache__", ".generated", ".test-output", "dist"})


def copy_support(source_root: Path, target: Path) -> None:
    target.mkdir()
    ignored = shutil.ignore_patterns(*sorted(IGNORED_PARTS))
    for name in SUPPORT_TREES:
        shutil.copytree(
            source_root / name,
            target / name,
            copy_function=shutil.copy2,
            ignore=ignored,
        )
    for name in SUPPORT_FILES:
        shutil.copy2(source_root / name, target / name)
    (target / "codex-openrouter").chmod(0o755)


def runtime_digest(root: Path) -> str:
    """support rootへ運ばれる内容のsha256。

    相対pathも混ぜる。ファイルの追加・削除・改名も差として出したいため。
    installed側は実行のたびに `__pycache__` が増えるので、copy_support と同じ
    ignore集合で落とす。
    """
    digest = hashlib.sha256()
    entries: list[tuple[str, Path]] = []
    for name in SUPPORT_FILES:
        path = root / name
        if path.is_file():
            entries.append((name, path))
    for name in SUPPORT_TREES:
        base = root / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            relative = path.relative_to(root)
            if IGNORED_PARTS.intersection(relative.parts):
                continue
            if path.is_file() and not path.is_symlink():
                entries.append((str(relative), path))
    for relative, path in sorted(entries):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def manifest_document(
    source_root: Path,
    paths: UserPaths,
    stock,
    workspace: Path,
    source_commit: str,
    profile_digest: str,
) -> dict:
    """install-manifest.json の中身。installとupgradeで同じ物を書く。

    `source_root` は起動時の自動更新が「どこと比べるか」を決める唯一の手がかり。
    ただしインストール済みツリー自身は記録しない。PATH上の codex-openrouter は
    source root をインストール先へ解決するので（codex-openrouter の source_root()）、
    そこから upgrade を打つと source が自分自身になり、記録すると以後の自動更新が
    永久に「変化なし」と判定される。
    """
    document = {
        "schema_version": 5,
        "release_version": __version__,
        "source_commit": source_commit,
        "chatgpt_version": stock.version,
        "chatgpt_build": stock.build,
        "workspace": str(workspace),
        "mode": "loopback-guard",
        "profile_digest": profile_digest,
    }
    if source_root != paths.support_root:
        document["source_root"] = str(source_root)
        document["source_digest"] = runtime_digest(source_root)
    return document


def render_template(source: Path, target: Path, home: Path, python: str) -> None:
    rendered = source.read_text(encoding="utf-8").replace("@@USER_HOME@@", str(home)).replace(
        "@@PYTHON@@", python
    )
    if "@@" in rendered:
        raise UpgradeError(f"未解決template placeholderがあります: {source}")
    target.write_text(rendered, encoding="utf-8")
    target.chmod(0o755)


def launcher_sources(source_root: Path) -> list[str]:
    """ランチャーappのSwift source一式。

    ディレクトリを丸ごと見る。こことCIが個別のファイル名を持つと、片方だけ
    増減して黙ってビルド対象から漏れる。
    """
    directory = source_root / "portable/launcher/app"
    sources = sorted(str(path) for path in directory.glob("*.swift"))
    if not sources:
        raise UpgradeError(f"ランチャーのSwift sourceがありません: {directory}")
    return sources


def build_launcher(source_root: Path, target: Path, workspace: Path, log_path: Path) -> None:
    """Desktopに置くランチャー専用バンドルを組み立てる。

    純正appのcloneではない。管理画面を出し、利用者が押したときだけ事前処理をして
    /Applications/ChatGPT.app を起動する薄いバンドル。

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
            *launcher_sources(source_root),
            "-o",
            str(executable),
        ]
    )
    run(["/usr/bin/codesign", "--force", "--sign", "-", str(target)])
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(target)])


def promote_runtime(
    source_root: Path,
    paths: UserPaths,
    stock,
    workspace: Path,
    profile: ResolvedProfile,
) -> Path:
    """setup/upgrade共通のstaging・検証・atomic promotion。"""
    if paths.shared_config.is_file():
        # marker外衝突や壊れたTOMLは、何も置換する前に停止する。
        configblock.render_managed(
            paths.shared_config.read_text(encoding="utf-8"),
            provider_body=provider_block_body(0),
        )

    python = shutil.which("python3") or "/usr/bin/python3"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = paths.state_dir / "upgrade-backups" / f"{timestamp}-v{__version__}"
    for directory in (paths.bin_dir, paths.state_dir, paths.support_root.parent):
        directory.mkdir(parents=True, exist_ok=True)
    paths.state_dir.chmod(0o700)

    with tempfile.TemporaryDirectory(prefix="codex-openrouter-deploy-") as temporary:
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
        # 一時helperで保存済みのKeychain itemを、staged helperで確認する。
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
        write_json(stage_state / "profile.json", profile.as_json())
        state = State.load(paths.supervisor_state)
        if state.profile_digest != profile.digest:
            state.pending_default_model = True
        state.profile_digest = profile.digest
        state.active = False
        state.guard_port = None
        state.guard_nonce = None
        state.save(stage_state / "supervisor.json")
        write_json(
            stage_state / "install-manifest.json",
            manifest_document(
                source_root,
                paths,
                stock,
                workspace,
                source_commit,
                profile.digest,
            ),
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
        replacements.extend(
            (
                (stage_launcher, paths.desktop_launcher),
                (stage_state / "install-manifest.json", paths.install_manifest),
                (stage_state / "profile.json", paths.installed_profile),
                (stage_state / "supervisor.json", paths.supervisor_state),
            )
        )

        def verify_live() -> None:
            run([str(paths.credential_helper), "status"])
            if paths.shared_config.is_file():
                Supervisor(
                    paths,
                    paths.support_root / "models/registry.json",
                    profile=profile,
                ).self_heal()
            environment = {**os.environ, "PYTHONPATH": str(paths.support_root / "src")}
            run(
                [str(paths.bin_dir / "codex-openrouter-doctor"), "--secret-scan"],
                environment=environment,
            )
            if detect_stock(paths.stock_app) != stock:
                raise UpgradeError("導入中にstock appが変化しました")

        atomic_promote(replacements, backup_root, verify_live)
    return backup_root


def upgrade(
    source_root: Path,
    paths: UserPaths,
    profile_argument: str | None = None,
    *,
    network_check: bool = True,
) -> int:
    """runtime更新を共有lifecycle lock内で実行する。"""
    with LifecycleLock(paths):
        return _upgrade_unlocked(
            source_root,
            paths,
            profile_argument,
            network_check=network_check,
        )


def _upgrade_unlocked(
    source_root: Path,
    paths: UserPaths,
    profile_argument: str | None = None,
    *,
    network_check: bool = True,
) -> int:
    assert_apple_silicon()
    if not paths.bin_dir.is_dir():
        raise UpgradeError("既存installationがありません。先にsetupを実行してください")
    if process_pids(paths.stock_app / "Contents/MacOS/ChatGPT"):
        raise UpgradeError("ChatGPT.appを終了してからupgradeしてください")
    stock = detect_stock(paths.stock_app)
    _profile_path, profile = installed_profile(
        source_root / "models/registry.json", paths, argument=profile_argument
    )
    # runtimeファイルの入れ替えに実課金のAPI往復は要らない。自動経路では外す。
    if network_check:
        key = CredentialStore(paths.credential_helper).get()
        print("INFO: upgrade検証は少量のOpenRouter API利用料が発生する場合があります。")
        metadata = validate_key_and_profile(key, set(profile.models))
        if metadata.get("limit") is None:
            print("WARNING: API keyのspend limitが未設定です。")

    backup_root = promote_runtime(
        source_root, paths, stock, installed_workspace(paths), profile
    )
    print(f"UPGRADE: PASS v{__version__} mode=loopback-guard")
    print(f"rollback backup: {backup_root}")
    return 0


# ランチャーがHUDの出し入れに使う。Swift側と同じ文字列であること。
STATUS_UPDATING = "STATUS: updating"
STATUS_LAUNCHING = "STATUS: launching"


def _selfupdate_state(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def auto_upgrade(paths: UserPaths, profile_argument: str | None = None) -> int:
    """「OpenRouterで起動」の前段。差分があるときだけupgradeする。

    **失敗しても0を返す。** 起動そのものは止めない。promotionのverifyが落ちれば
    自動rollbackが効くので、runtimeは直前の動く状態に戻る。

    同じ内容で一度失敗したらskipする。壊れた作業中のツリーを掴んだとき、
    起動のたびに十数秒払って同じ失敗を繰り返さないため。
    """
    receipt = paths.install_manifest
    if not receipt.is_file() or receipt.is_symlink():
        return 0
    recorded = _selfupdate_state(receipt).get("source_root")
    if not isinstance(recorded, str) or not recorded:
        return 0
    source_root = Path(recorded)
    # 記録されたpathをそのまま実行するので、利用者のhome配下に限る。
    if not source_root.is_dir() or not source_root.is_relative_to(paths.home):
        return 0
    if source_root == paths.support_root:
        return 0

    try:
        source_digest = runtime_digest(source_root)
        installed_digest = runtime_digest(paths.support_root)
    except OSError:
        return 0
    if source_digest == installed_digest:
        return 0

    state_path = paths.state_dir / "selfupdate.json"
    state = _selfupdate_state(state_path)
    if state.get("result") == "failure" and state.get("digest") == source_digest:
        print(f"自動更新: 同じ内容で失敗済みのためskipします（{source_root}）")
        return 0

    print(STATUS_UPDATING, flush=True)
    print(f"自動更新: {source_root} の変更を反映します", flush=True)
    try:
        upgrade(source_root, paths, profile_argument, network_check=False)
        result = "success"
    except LifecycleLockError:
        # 起動中・別更新中は一時的な競合であり、同じdigestの恒久失敗にしない。
        print("自動更新: Codex OpenRouterが使用中のためskipします")
        print(STATUS_LAUNCHING, flush=True)
        return 0
    except Exception as error:  # 起動は止めない
        result = "failure"
        print(f"自動更新に失敗しました（起動は続行します）: {error}")
    write_json(state_path, {"schema_version": 1, "result": result, "digest": source_digest})
    print(STATUS_LAUNCHING, flush=True)
    return 0
