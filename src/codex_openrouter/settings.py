"""モデル設定の唯一の更新窓口。

ランチャーの設定画面はSwiftで描くが、profile・Keychain・Guardrailの判断は
一切持たない。ここを通してしか導入済みprofileは変わらない。Swift側へ同じ判断を
複製すると、片方だけ更新された時に「UIでは通ったのに実体は違う」状態になる。

applyは lifecycle lock 内で次の順に進む。1つでも落ちれば1バイトも書き換えない。

  1. installed registryとの整合性検証（未知slug・重複・空・選択外default）
  2. 同一profileならno-op（既定モデルの再適用をarmしない）
  3. Keychainからkey取得
  4. OpenRouter keyの実効モデル集合との完全一致検証
  5. profile・supervisor state・install-manifest・旧catalogを単一transactionで置換
  6. promotion後の検証に落ちれば全対象をrollback
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import tempfile
from typing import Any

from . import catalog
from .app import UserPaths, installed_workspace, write_json
from .auth import CredentialStore
from .lifecycle import LifecycleLock
from .openrouter import validate_key_and_profile
from .processes import ProcessError, process_pids
from .profile import (
    ProfileError,
    ResolvedProfile,
    parse_apply_payload,
    resolve_apply_payload,
    resolve_profile,
    select_profile_path,
)
from .promotion import atomic_promote
from .supervisor import State

GUARDRAIL_URL = "https://openrouter.ai/settings/guardrails"
DOCUMENT_SCHEMA_VERSION = 1


class SettingsError(RuntimeError):
    pass


def _registry_models(registry_path: Path) -> dict[str, dict[str, Any]]:
    try:
        document = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsError(f"model registryを読み込めません: {registry_path}: {exc}") from exc
    models = document.get("models") if isinstance(document, dict) else None
    if not isinstance(models, dict) or not models:
        raise SettingsError("model registryが空または不正です")
    return models


def _installed_profile(registry_path: Path, paths: UserPaths) -> tuple[Path, ResolvedProfile]:
    selected = select_profile_path(
        argument=None,
        source_default=registry_path.parent.parent / "profiles/default.json",
        installed=paths.installed_profile,
        legacy=paths.codex_home / "profile.json",
    )
    return selected, resolve_profile(registry_path, selected)


def _stale_catalogs(paths: UserPaths) -> list[Path]:
    candidates = (
        paths.composite_catalog,
        catalog.previous_path(paths.composite_catalog),
    )
    return [path for path in candidates if path.is_file() and not path.is_symlink()]


def openrouter_is_running(paths: UserPaths) -> bool:
    """OpenRouterモードが本当に動いているか。

    stateの`active`だけでは足りない。SIGKILLや電源断の後は次の専用起動で
    self-healするまでtrueのまま残るので、それだけを見ると設定画面が永久に
    編集不可になる。activeなら純正appも動いているはずなので、両方を見る。
    """
    if not State.load(paths.supervisor_state).active:
        return False
    try:
        return bool(process_pids(paths.stock_app / "Contents/MacOS/ChatGPT"))
    except ProcessError:
        return True


def show_document(paths: UserPaths, registry_path: Path) -> dict[str, Any]:
    """設定画面の描画に必要な事実だけを返す。秘密値は含まない。"""
    registry = _registry_models(registry_path)
    _selected, profile = _installed_profile(registry_path, paths)
    active = openrouter_is_running(paths)
    return {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "profile": {**profile.as_json(), "digest": profile.digest},
        "available": [
            {
                "id": model,
                "display_name": spec.get("display_name", model),
                "capability": spec.get("capability", ""),
                "efforts": list(spec.get("efforts") or []),
                "default_effort": spec.get("default_effort"),
                "context_window": spec.get("context_window"),
            }
            for model, spec in registry.items()
        ],
        "openrouter_active": active,
        "editable": not active,
        "workspace": str(installed_workspace(paths)),
        "guardrail_url": GUARDRAIL_URL,
    }


def apply_payload(paths: UserPaths, registry_path: Path, raw: str) -> dict[str, Any]:
    """設定画面のapply入力を反映する。構文検証はlockの外で済ませる。"""
    payload = parse_apply_payload(raw)
    with LifecycleLock(paths):
        return _apply_locked(paths, registry_path, payload)


def _load_manifest(paths: UserPaths) -> dict[str, Any]:
    receipt = paths.install_manifest
    if not receipt.is_file() or receipt.is_symlink():
        raise SettingsError(
            f"install-manifestがありません: {receipt}。先にsetupを実行してください"
        )
    try:
        document = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsError(f"install-manifestを読み込めません: {exc}") from exc
    if not isinstance(document, dict):
        raise SettingsError("install-manifestのトップレベルがobjectではありません")
    return document


def verify_promotion(
    paths: UserPaths, registry_path: Path, profile: ResolvedProfile
) -> None:
    """promotion後に、4つの対象が同じprofileを指していることを確かめる。"""
    try:
        promoted = resolve_profile(registry_path, paths.installed_profile)
    except ProfileError as exc:
        raise SettingsError(f"promotion後のprofileを解決できません: {exc}") from exc
    if promoted.digest != profile.digest:
        raise SettingsError("promotion後のprofile digestが一致しません")
    state = State.load(paths.supervisor_state)
    if state.profile_digest != profile.digest:
        raise SettingsError("promotion後のsupervisor stateがprofileと一致しません")
    if not state.pending_default_model:
        raise SettingsError("既定モデルの適用待ちがarmされていません")
    if _load_manifest(paths).get("profile_digest") != profile.digest:
        raise SettingsError("promotion後のinstall-manifestがprofileと一致しません")
    stale = _stale_catalogs(paths)
    if stale:
        raise SettingsError(f"旧catalogが残っています: {[str(path) for path in stale]}")


def _apply_locked(
    paths: UserPaths, registry_path: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    manifest = _load_manifest(paths)
    current_path, current = _installed_profile(registry_path, paths)
    profile = resolve_apply_payload(registry_path, payload, name=current.name)
    state = State.load(paths.supervisor_state)

    if (
        current_path == paths.installed_profile
        and current.digest == profile.digest
        and state.profile_digest == profile.digest
        and manifest.get("profile_digest") == profile.digest
    ):
        # 同じ選択の再保存で既定モデルを再適用すると、利用者がpickerで選び直した
        # モデルが次回起動で黙って戻る。
        return {
            "schema_version": DOCUMENT_SCHEMA_VERSION,
            "result": "unchanged",
            "profile": {**profile.as_json(), "digest": profile.digest},
            "pending_default_model": bool(state.pending_default_model),
        }

    key = CredentialStore(paths.credential_helper).get()
    validate_key_and_profile(key, set(profile.models))

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # upgrade-backups とは分ける。`codex-openrouter rollback` は「runtimeをupgrade前へ
    # 戻す」ためのもので、モデル選択の取り消しではない。
    backup_root = paths.state_dir / "profile-backups" / f"{timestamp}-{profile.digest[:12]}"
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.chmod(0o700)

    with tempfile.TemporaryDirectory(prefix="codex-openrouter-profile-") as temporary:
        stage = Path(temporary).resolve()
        write_json(stage / "profile.json", profile.as_json())
        # active・guard port・保存済み選択は触らない。ここは「次回起動へ効かせる」
        # 変更であって、いま動いているセッションの状態ではない。
        replace(
            state, profile_digest=profile.digest, pending_default_model=True
        ).save(stage / "supervisor.json")
        write_json(stage / "install-manifest.json", {**manifest, "profile_digest": profile.digest})

        replacements: list[tuple[Path | None, Path]] = [
            (stage / "profile.json", paths.installed_profile),
            (stage / "supervisor.json", paths.supervisor_state),
            (stage / "install-manifest.json", paths.install_manifest),
        ]
        replacements.extend((None, stale) for stale in _stale_catalogs(paths))

        atomic_promote(
            replacements,
            backup_root,
            lambda: verify_promotion(paths, registry_path, profile),
        )

    return {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "result": "applied",
        "profile": {**profile.as_json(), "digest": profile.digest},
        "pending_default_model": True,
        "backup": str(backup_root),
    }
