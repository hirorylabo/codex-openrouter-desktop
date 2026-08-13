from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess


class AppError(RuntimeError):
    pass


@dataclass(frozen=True)
class StockBuild:
    app: Path
    version: str
    build: str
    asar_sha256: str


@dataclass(frozen=True)
class UserPaths:
    home: Path
    stock_app: Path
    openrouter_app: Path
    codex_home: Path
    bin_dir: Path
    support_root: Path
    credential_helper: Path
    desktop_launcher: Path
    # 案D: 正本は純正appと共有する ~/.codex。openrouter_app/codex_home は
    # 旧clone方式の遺産で、migrate後は読み取り専用backupとしてのみ参照する。
    shared_home: Path
    state_dir: Path

    @classmethod
    def current(cls) -> "UserPaths":
        home = Path.home().resolve()
        if home.parent != Path("/Users") or home == Path("/Users"):
            raise AppError(f"安全なmacOS user homeを解決できません: {home}")
        return cls(
            home=home,
            stock_app=Path("/Applications/ChatGPT.app"),
            openrouter_app=home / "Applications/ChatGPT OpenRouter.app",
            codex_home=home / ".codex-openrouter",
            bin_dir=home / ".local/bin",
            support_root=home / ".local/share/codex-openrouter-desktop/current",
            credential_helper=home / ".local/bin/codex-openrouter-credential",
            desktop_launcher=home / "Desktop/Codex OpenRouter.app",
            shared_home=home / ".codex",
            state_dir=home / ".local/share/codex-openrouter-desktop/state",
        )

    @property
    def shared_config(self) -> Path:
        return self.shared_home / "config.toml"

    @property
    def composite_catalog(self) -> Path:
        return self.shared_home / "model-catalogs/codex-openrouter.json"

    @property
    def stock_codex(self) -> Path:
        return self.stock_app / "Contents/Resources/codex"

    @property
    def guard_log(self) -> Path:
        return self.state_dir / "guard.log"

    @property
    def installed_profile(self) -> Path:
        return self.state_dir / "profile.json"

    @property
    def supervisor_state(self) -> Path:
        return self.state_dir / "supervisor.json"

    @property
    def guard_token(self) -> Path:
        return self.state_dir / "guard-token"

    @property
    def install_manifest(self) -> Path:
        return self.state_dir / "install-manifest.json"

    @property
    def installed_registry(self) -> Path:
        """設定画面がmodelを足すたびに育つregistry。無ければ同梱registryを使う。"""
        return self.state_dir / "registry.json"

    @property
    def catalog_cache(self) -> Path:
        return self.state_dir / "model-catalog-cache.json"

    @property
    def catalog_cache_state(self) -> Path:
        return self.state_dir / "model-catalog-state.json"


def write_json(path: Path, document: dict) -> None:
    """runtime stateのJSONを0600で書く。中身は秘密値を含まない前提。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    path.chmod(0o600)


def installed_workspace(paths: UserPaths) -> Path:
    """install-manifestに記録されたworkspace。無ければ `~/Documents`。"""
    receipt = paths.install_manifest
    if receipt.is_file() and not receipt.is_symlink():
        try:
            saved = json.loads(receipt.read_text(encoding="utf-8")).get("workspace")
        except (OSError, json.JSONDecodeError, AttributeError):
            saved = None
        if isinstance(saved, str) and Path(saved).is_dir():
            return Path(saved)
    return paths.home / "Documents"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plist_value(app: Path, key: str) -> str:
    result = subprocess.run(
        ["/usr/libexec/PlistBuddy", "-c", f"Print :{key}", str(app / "Contents/Info.plist")],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise AppError(f"ChatGPT Info.plistから{key}を取得できません")
    return result.stdout.strip()


def detect_stock(app: Path = Path("/Applications/ChatGPT.app")) -> StockBuild:
    asar = app / "Contents/Resources/app.asar"
    if not app.is_dir() or not asar.is_file():
        raise AppError(f"公式ChatGPT.appが見つかりません: {app}")
    signature = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if signature.returncode != 0:
        raise AppError("公式ChatGPT.appの署名検証に失敗しました")
    return StockBuild(
        app=app,
        version=_plist_value(app, "CFBundleShortVersionString"),
        build=_plist_value(app, "CFBundleVersion"),
        asar_sha256=sha256(asar),
    )


def stock_build_id(app: Path = Path("/Applications/ChatGPT.app")) -> tuple[str, str]:
    """update検知用の (version, build)。

    ASAR hashは意図的に取らない。223MBのハッシュを毎回走らせないため、
    検知はInfo.plistの2値だけで行う。
    """
    if not app.is_dir():
        raise AppError(f"公式ChatGPT.appが見つかりません: {app}")
    return _plist_value(app, "CFBundleShortVersionString"), _plist_value(app, "CFBundleVersion")


def assert_apple_silicon() -> None:
    machine = os.uname().machine
    if machine != "arm64":
        raise AppError(f"Apple Silicon専用です: architecture={machine}")
