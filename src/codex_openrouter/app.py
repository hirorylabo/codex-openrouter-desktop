from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any


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


def load_adapter(index_path: Path, stock: StockBuild) -> dict[str, Any] | None:
    try:
        document = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError(f"adapter indexを読めません: {exc}") from exc
    if document.get("schema_version") != 1 or not isinstance(document.get("adapters"), list):
        raise AppError("adapter index schemaが不正です")
    matches = [
        adapter
        for adapter in document["adapters"]
        if isinstance(adapter, dict)
        and adapter.get("chatgpt_version") == stock.version
        and str(adapter.get("chatgpt_build")) == stock.build
        and adapter.get("stock_asar_sha256") == stock.asar_sha256
    ]
    if len(matches) > 1:
        raise AppError("同じstock buildへ複数のadapterが一致しました")
    return matches[0] if matches else None


def assert_apple_silicon() -> None:
    machine = os.uname().machine
    if machine != "arm64":
        raise AppError(f"Apple Silicon専用です: architecture={machine}")
