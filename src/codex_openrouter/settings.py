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

from . import catalog, modelcatalog, toolcompat
from .app import AppError, UserPaths, installed_workspace, stock_build_id, write_json
from .auth import CredentialStore
from .lifecycle import LifecycleLock
from .openrouter import validate_key_and_profile
from .processes import ProcessError, process_pids
from .profile import (
    ProfileError,
    ResolvedProfile,
    active_registry,
    installed_profile,
    parse_apply_payload,
    resolve_apply_document,
    resolve_profile,
)
from .promotion import atomic_promote
from .supervisor import State

GUARDRAIL_URL = "https://openrouter.ai/settings/guardrails"
DOCUMENT_SCHEMA_VERSION = 1


class SettingsError(RuntimeError):
    pass


def _chatgpt_build(paths: UserPaths) -> str:
    """実機buildを返す。隔離unit testだけは保存済みstateへ倒す。"""
    try:
        return stock_build_id(paths.stock_app)[1]
    except AppError:
        return State.load(paths.supervisor_state).build or "unknown"


def _read_registry(registry_path: Path) -> dict[str, Any]:
    try:
        document = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsError(f"model registryを読み込めません: {registry_path}: {exc}") from exc
    models = document.get("models") if isinstance(document, dict) else None
    if not isinstance(models, dict) or not models:
        raise SettingsError("model registryが空または不正です")
    return document


def _registry_models(registry_path: Path) -> dict[str, dict[str, Any]]:
    return _read_registry(registry_path)["models"]


def materialize_registry(
    source_document: dict[str, Any],
    catalog_document: dict[str, Any],
    requested: list[str],
    known: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """選択中のmodelだけを載せたregistry documentを作る。

    エントリはライブcatalogから毎回derive し直し、同梱registryにある説明文だけ
    上書きせずに残す。こうすると、同梱側で価格やeffortが直っても、既にmodelを
    足した利用者へ自然に届く。

    `known` は既に手元にあるエントリ（導入済みregistry）。catalogから消えたmodelの
    受け皿にする。これが無いと、前に足したmodelが一時的にcatalogから消えている間、
    **無関係なmodelの追加まで巻き添えで失敗する**。

    並び順は「同梱registryにあるものが先、その後に追加分をcatalog順」。追加した
    瞬間に既存利用者のpicker順が入れ替わらないようにする。
    """
    rows = {row["id"]: row for row in catalog_document.get("models", [])}
    bundled = source_document["models"]
    inherited = {**bundled, **(known or {})}
    unknown = sorted(model for model in requested if model not in rows and model not in inherited)
    if unknown:
        raise SettingsError(
            "OpenRouterの候補に無いmodelです: " + ", ".join(unknown)
        )

    selected = set(requested)
    ordered = [model for model in bundled if model in selected]
    ordered += [model for model in rows if model in selected and model not in bundled]
    ordered += [model for model in inherited if model in selected and model not in ordered]

    models: dict[str, Any] = {}
    for model in ordered:
        row = rows.get(model)
        if row is None:
            # catalogに出ていない（引退・一時的な欠落）が、手元のエントリは知っている。
            # 既知のものをそのまま使い、選択を落とさない。
            models[model] = inherited[model]
            continue
        models[model] = modelcatalog.entry_for(row, bundled.get(model))
    return {
        key: value for key, value in source_document.items() if key != "models"
    } | {"models": models}


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
    """設定画面の描画に必要な事実だけを返す。秘密値は含まない。

    ここはネットワークに触らない。設定画面はまずこれで即座に現在の選択を描き、
    候補一覧（`models list`）を後から流し込む。
    """
    registry = _registry_models(active_registry(registry_path, paths))
    _selected, profile = installed_profile(registry_path, paths)
    active = openrouter_is_running(paths)
    build = _chatgpt_build(paths)
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
                "zdr_supported": bool(spec.get("zdr_supported", True)),
                **toolcompat.support_for(
                    model, spec, paths.tool_compatibility, build
                ),
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
    """promotion後に、5つの対象が同じprofileを指していることを確かめる。

    registryも対象に入る。profileだけ先に見えてregistryが古いと、
    「選んだmodelがregistryに無い」状態で次の起動に入ってしまう。
    """
    try:
        promoted = resolve_profile(active_registry(registry_path, paths), paths.installed_profile)
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
    stale = catalog.stale_paths(paths.composite_catalog)
    if stale:
        raise SettingsError(f"旧catalogが残っています: {[str(path) for path in stale]}")


def _target_registry(
    paths: UserPaths, registry_path: Path, requested: Any
) -> tuple[dict[str, Any], bool]:
    """要求されたmodelを全て載せたregistry documentと、書き換えが要るかを返す。

    既に手元のregistryで足りるならネットワークへ行かない。設定画面での
    「選択の付け外し」は大半がこの経路で、そこへ410件の取得を挟むと遅いだけ。
    """
    current_path = active_registry(registry_path, paths)
    current = _read_registry(current_path)
    if not isinstance(requested, list) or not all(isinstance(m, str) for m in requested):
        # 形の不正はprofile側の検証に任せる。ここでは触らず素通しする。
        return current, False
    if set(requested) <= set(current["models"]) and current_path == paths.installed_registry:
        return current, False

    source = _read_registry(registry_path)
    if set(requested) <= set(source["models"]) and current_path == registry_path:
        # 同梱registryのままで足りる。導入済みregistryを作る意味がない。
        return source, False
    try:
        available = modelcatalog.load(paths, source)
    except modelcatalog.CatalogError as exc:
        raise SettingsError(str(exc)) from exc
    # 手元のエントリも渡す。前に足したmodelが一時的にcatalogから消えていても、
    # 無関係なmodelの追加を巻き添えで失敗させない。
    return materialize_registry(source, available, requested, current["models"]), True


def _apply_locked(
    paths: UserPaths, registry_path: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    manifest = _load_manifest(paths)
    current_path, current = installed_profile(registry_path, paths)
    registry_document, registry_changed = _target_registry(
        paths, registry_path, payload.get("models")
    )
    profile = resolve_apply_document(registry_document, payload, name=current.name)
    state = State.load(paths.supervisor_state)

    if (
        not registry_changed
        and current_path == paths.installed_profile
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

    added = sorted(set(profile.models) - set(current.models))
    build = _chatgpt_build(paths)
    needs_canary = [
        model
        for model in added
        if toolcompat.support_for(
            model, registry_document["models"][model], paths.tool_compatibility, build
        )["tool_support"]
        in {"declared", "unknown"}
    ]
    if needs_canary:
        toolcompat.verify_models(
            needs_canary,
            registry_document["models"],
            key=key,
            build=build,
            cache_path=paths.tool_compatibility,
        )

    acknowledged = set(payload.get("tool_risk_acknowledged") or [])
    if not acknowledged <= set(profile.models):
        raise SettingsError("tool_risk_acknowledgedに選択外のmodelがあります")
    risky = {
        model
        for model in added
        if toolcompat.support_for(
            model, registry_document["models"][model], paths.tool_compatibility, build
        )["tool_support"]
        in {"partial", "unsupported"}
    }
    missing_ack = sorted(risky - acknowledged)
    if missing_ack:
        raise SettingsError(
            "Codex tool非互換リスクの明示承認が必要です: " + ", ".join(missing_ack)
        )

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
        # catalog digestだけは消す。このtransactionでcatalog自体を消すので、
        # 残すと「存在しないcatalogがこのprofileで作られている」という嘘になる。
        replace(
            state,
            profile_digest=profile.digest,
            pending_default_model=True,
            catalog_profile_digest=None,
            catalog_tool_digest=None,
        ).save(stage / "supervisor.json")
        write_json(stage / "install-manifest.json", {**manifest, "profile_digest": profile.digest})

        if registry_changed:
            # profileより先にregistryが要る。順序はatomic_promoteが握るが、
            # 同じtransactionに入れないと「選んだmodelがregistryに無い」窓ができる。
            write_json(stage / "registry.json", registry_document)

        replacements: list[tuple[Path | None, Path]] = [
            (stage / "profile.json", paths.installed_profile),
            (stage / "supervisor.json", paths.supervisor_state),
            (stage / "install-manifest.json", paths.install_manifest),
        ]
        if registry_changed:
            replacements.append((stage / "registry.json", paths.installed_registry))
        replacements.extend(
            (None, stale) for stale in catalog.stale_paths(paths.composite_catalog)
        )

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
