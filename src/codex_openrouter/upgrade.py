from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

from . import __version__
from .app import UserPaths, detect_stock, load_adapter, sha256
from .auth import CredentialStore
from .openrouter import validate_key_and_profile
from .processes import process_pids
from .profile import resolve_profile
from .promotion import atomic_promote


class UpgradeError(RuntimeError):
    pass


CONTROLLED_HOME_FILES = (
    "adapter.json",
    "config.toml",
    "profile.json",
    "registry.json",
    "desktop-model-providers.json",
    "model-catalogs/openrouter.json",
    "price-refresh-state.json",
    "install-manifest.json",
)
BINARIES = (
    "codex-openrouter",
    "codex-openrouter-credential",
    "codex-openrouter-refresh",
    "codex-openrouter-doctor",
    "codex-openrouter-app",
    "codex-openrouter-rebuild",
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
    for name in ("src", "models", "profiles", "adapters", "portable"):
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


def fetch_verified(url: str, expected: str, target: Path) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": f"codex-openrouter-desktop/{__version__}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise UpgradeError(f"pinned download hash mismatch: {actual}")
    target.write_bytes(payload)
    target.chmod(0o644)


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    path.chmod(0o600)


def upgrade(source_root: Path, paths: UserPaths, profile_argument: str) -> int:
    if not paths.openrouter_app.is_dir() or not paths.codex_home.is_dir():
        raise UpgradeError("既存installationがありません。先にsetupを実行してください")
    active_executable = paths.openrouter_app / "Contents/MacOS/ChatGPT"
    if process_pids(active_executable):
        raise UpgradeError("専用appを通常終了してからupgradeしてください")
    stock = detect_stock(paths.stock_app)
    adapter = load_adapter(source_root / "adapters/index.json", stock)
    if adapter is None:
        raise UpgradeError("未知buildはupgradeできません。codex-openrouter updateでcandidateを作成してください")
    profile_path = (
        paths.codex_home / "profile.json"
        if profile_argument == "default"
        else Path(profile_argument).expanduser().resolve()
    )
    profile = resolve_profile(source_root / "models/registry.json", profile_path)
    key = CredentialStore(paths.credential_helper).get()
    print("INFO: upgrade検証は少量のOpenRouter API利用料が発生する場合があります。")
    metadata = validate_key_and_profile(key, set(profile.models))
    if metadata.get("limit") is None:
        print("WARNING: API keyのspend limitが未設定です。")

    manifest = json.loads((source_root / "portable/manifest.json").read_text(encoding="utf-8"))
    upstream = manifest["upstream_patcher"]
    python = shutil.which("python3") or "/usr/bin/python3"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = paths.codex_home / "upgrade-backups" / f"{timestamp}-v{__version__}"
    receipt = paths.codex_home / "install-manifest.json"
    workspace = paths.home / "Documents"
    if receipt.is_file() and not receipt.is_symlink():
        saved = json.loads(receipt.read_text(encoding="utf-8")).get("workspace")
        if isinstance(saved, str) and Path(saved).is_dir():
            workspace = Path(saved)

    with tempfile.TemporaryDirectory(prefix="codex-openrouter-upgrade-") as temporary:
        stage = Path(temporary).resolve()
        stage_support = stage / "support"
        stage_home = stage / "home"
        stage_bin = stage / "bin"
        stage_patch = stage / "patch"
        stage_launcher = stage / "Codex OpenRouter.app"
        for directory in (stage_home, stage_bin, stage_patch):
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
        run(
            [
                python,
                str(source_root / "portable/render_runtime.py"),
                "--registry",
                str(source_root / "models/registry.json"),
                "--profile",
                str(profile_path),
                "--template",
                str(source_root / "portable/templates/config.toml.in"),
                "--output-home",
                str(stage_home),
                "--runtime-home",
                str(paths.codex_home),
                "--credential-helper",
                str(paths.credential_helper),
            ],
            environment={**os.environ, "PYTHONPATH": str(source_root / "src")},
        )
        _write_json(stage_home / "adapter.json", adapter)
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
            stage_home / "install-manifest.json",
            {
                "schema_version": 2,
                "release_version": __version__,
                "source_commit": source_commit,
                "adapter_id": adapter["id"],
                "chatgpt_version": stock.version,
                "chatgpt_build": stock.build,
                "stock_asar_sha256": stock.asar_sha256,
                "workspace": str(workspace),
            },
        )

        shutil.copy2(source_root / "codex-openrouter", stage_bin / "codex-openrouter")
        (stage_bin / "codex-openrouter").chmod(0o755)
        for template, target in (
            ("codex-openrouter-refresh.py.in", "codex-openrouter-refresh"),
            ("codex-openrouter-doctor.py.in", "codex-openrouter-doctor"),
            ("codex-openrouter-app.zsh.in", "codex-openrouter-app"),
            ("codex-openrouter-rebuild.zsh.in", "codex-openrouter-rebuild"),
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
                str(stage_bin / "codex-openrouter-refresh"),
                str(stage_bin / "codex-openrouter-doctor"),
            ]
        )
        run(["/bin/zsh", "-n", str(stage_bin / "codex-openrouter-app")])
        run(["/bin/zsh", "-n", str(stage_bin / "codex-openrouter-rebuild")])

        fetch_verified(
            upstream["source_url"],
            upstream["source_sha256"],
            stage_patch / "patch_chatgpt_providers.py",
        )
        fetch_verified(
            upstream["license_url"],
            upstream["license_sha256"],
            stage_patch / "LICENSE",
        )

        active_adapter = json.loads((paths.codex_home / "adapter.json").read_text(encoding="utf-8"))
        candidate_app: Path | None = None
        if active_adapter != adapter:
            candidate_app = stage / "ChatGPT OpenRouter Candidate.app"
            run(["/usr/bin/ditto", str(paths.stock_app), str(candidate_app)])
            patcher = (source_root / adapter["patcher"]).resolve()
            if not patcher.is_relative_to(source_root) or not patcher.is_file():
                raise UpgradeError("adapter patcher pathが不正です")
            run(
                [
                    python,
                    str(patcher),
                    "--app",
                    str(candidate_app),
                    "--config",
                    str(stage_home / "desktop-model-providers.json"),
                    "--backup-dir",
                    str(stage / "patch-backup"),
                    "--upstream",
                    str(stage_patch / "patch_chatgpt_providers.py"),
                    "--upstream-sha256",
                    upstream["source_sha256"],
                ]
            )
            if (
                sha256(candidate_app / "Contents/Resources/app.asar")
                != adapter["patched_asar_sha256"]
            ):
                raise UpgradeError("known adapter candidateのpatched hashが一致しません")
            run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(candidate_app)])
        elif (
            sha256(paths.openrouter_app / "Contents/Resources/app.asar")
            != adapter["patched_asar_sha256"]
        ):
            raise UpgradeError("active appがknown adapterのpatched hashと一致しません")

        environment = {
            **os.environ,
            "CODEX_OPENROUTER_HOME": str(stage_home),
            "CODEX_OPENROUTER_RUNTIME_HOME": str(paths.codex_home),
            "CODEX_OPENROUTER_APP": str(candidate_app or paths.openrouter_app),
            "CODEX_OPENROUTER_CREDENTIAL": str(credential),
            "CODEX_OPENROUTER_SUPPORT_ROOT": str(stage_support),
            "PYTHONPATH": str(stage_support / "src"),
        }
        run([str(stage_bin / "codex-openrouter-refresh"), "--init"], environment=environment)
        run(
            [
                str(stage_bin / "codex-openrouter-doctor"),
                "--network",
                "--secret-scan",
            ],
            environment=environment,
        )

        replacements: list[tuple[Path, Path]] = [(stage_support, paths.support_root)]
        replacements.extend((stage_bin / name, paths.bin_dir / name) for name in BINARIES)
        replacements.append((stage_launcher, paths.desktop_launcher))
        launcher_executable = stage_launcher / "Contents/MacOS/CodexOpenRouterLauncher"
        launcher_executable.parent.mkdir(parents=True)
        launcher_resources = stage_launcher / "Contents/Resources"
        launcher_resources.mkdir()
        launcher_plist = stage_launcher / "Contents/Info.plist"
        shutil.copy2(source_root / "portable/launcher/Info.plist", launcher_plist)
        for key in ("CFBundleShortVersionString", "CFBundleVersion"):
            run(
                [
                    "/usr/bin/plutil",
                    "-replace",
                    key,
                    "-string",
                    __version__,
                    str(launcher_plist),
                ]
            )
        run(
            [
                str(source_root / "portable/launcher/build_icon.zsh"),
                str(source_root / "portable/launcher/CreateLauncherIcon.swift"),
                str(launcher_resources / "AppIcon.icns"),
            ]
        )
        run(
            [
                "/usr/bin/plutil",
                "-replace",
                "CodexDefaultWorkspace",
                "-string",
                str(workspace),
                str(launcher_plist),
            ]
        )
        run(
            [
                "/usr/bin/xcrun",
                "swiftc",
                str(source_root / "portable/launcher/CodexOpenRouterLauncher.swift"),
                "-o",
                str(launcher_executable),
            ]
        )
        run(["/usr/bin/codesign", "--force", "--sign", "-", str(stage_launcher)])
        run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(stage_launcher)])
        replacements.append(
            (
                stage_patch,
                paths.home
                / ".local/share/codex-openrouter-patcher"
                / upstream["commit"],
            )
        )
        replacements.extend(
            (stage_home / name, paths.codex_home / name)
            for name in CONTROLLED_HOME_FILES
        )
        if candidate_app is not None:
            replacements.append((candidate_app, paths.openrouter_app))

        def verify_live() -> None:
            run([str(paths.bin_dir / "codex-openrouter-doctor"), "--secret-scan"])
            if detect_stock(paths.stock_app) != stock:
                raise UpgradeError("upgrade中にstock appが変化しました")

        atomic_promote(replacements, backup_root, verify_live)
    print(f"UPGRADE: PASS v{__version__} adapter={adapter['id']}")
    print(f"rollback backup: {backup_root}")
    return 0
