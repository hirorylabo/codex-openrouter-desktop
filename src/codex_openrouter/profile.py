from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .app import UserPaths


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedProfile:
    name: str
    models: tuple[str, ...]
    default_model: str
    default_effort: str | None
    registry: dict[str, dict[str, Any]]

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "name": self.name,
            "models": list(self.models),
            "default_model": self.default_model,
            "default_effort": self.default_effort,
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.as_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"JSONを読み込めません: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProfileError(f"トップレベルはJSON objectである必要があります: {path}")
    return value


def _resolve(
    registry_document: dict[str, Any],
    profile_document: dict[str, Any],
    fallback_name: str,
) -> ResolvedProfile:
    if registry_document.get("schema_version") != 1:
        raise ProfileError("未対応のmodel registry schemaです")
    if profile_document.get("schema_version") != 1:
        raise ProfileError("未対応のprofile schemaです")

    registry = registry_document.get("models")
    if not isinstance(registry, dict) or not registry:
        raise ProfileError("model registryが空または不正です")
    models = profile_document.get("models")
    if not isinstance(models, list) or not models:
        raise ProfileError("profile.modelsは空でないarrayである必要があります")
    if not all(isinstance(model, str) and model.strip() == model and model for model in models):
        raise ProfileError("profile.modelsには空でない正規化済みmodel IDだけを指定してください")
    if len(models) != len(set(models)):
        raise ProfileError("profile.modelsに重複があります")

    unknown = sorted(set(models) - set(registry))
    if unknown:
        raise ProfileError(f"未検証モデルは選択できません: {', '.join(unknown)}")
    default_model = profile_document.get("default_model")
    if default_model not in models:
        raise ProfileError("default_modelはprofile.models内から選択してください")

    name = profile_document.get("name", fallback_name)
    if not isinstance(name, str) or not name.strip():
        raise ProfileError("profile.nameは空でない文字列である必要があります")
    default_effort = registry[default_model].get("default_effort")
    if default_effort is not None and not isinstance(default_effort, str):
        raise ProfileError(f"default effortが不正です: {default_model}")
    # 並び順の出所はregistryだけにする。profileやUIが順序を持つと、同じ選択でも
    # digestとpicker priorityが揺れる。
    selected = set(models)
    ordered = tuple(model for model in registry if model in selected)
    return ResolvedProfile(
        name=name.strip(),
        models=ordered,
        default_model=default_model,
        default_effort=default_effort,
        registry={model: registry[model] for model in ordered},
    )


def resolve_profile(registry_path: Path, profile_path: Path) -> ResolvedProfile:
    return _resolve(
        _load_json(registry_path), _load_json(profile_path), profile_path.stem
    )


# 設定画面から変更できるのはこの3keyだけ。表示名・reasoning effort・並び順は
# registryが持ち、入力経路を持たない。
APPLY_KEYS = frozenset({"schema_version", "models", "default_model"})


def parse_apply_payload(raw: str) -> dict[str, Any]:
    """設定画面が送るapply入力を、解決する前に構文だけ検証する。"""
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProfileError(f"applyの入力がJSONではありません: {exc}") from exc
    if not isinstance(document, dict):
        raise ProfileError("applyの入力はJSON objectである必要があります")
    unexpected = sorted(set(document) - APPLY_KEYS)
    if unexpected:
        raise ProfileError(f"applyで変更できない項目です: {', '.join(unexpected)}")
    return document


def resolve_apply_document(
    registry_document: dict[str, Any], payload: dict[str, Any], *, name: str
) -> ResolvedProfile:
    """apply入力をregistry documentへ突き合わせて解決する。

    profile nameは既存のものを引き継ぐ。UIはモデル集合と既定モデルだけを決める。
    documentを受け取るのは、model追加時にまだ書き出していないregistryへ
    突き合わせる必要があるため（書いてから検証すると、落ちたときに戻す対象が増える）。
    """
    document = {
        "schema_version": payload.get("schema_version"),
        "name": name,
        "models": payload.get("models"),
        "default_model": payload.get("default_model"),
    }
    return _resolve(registry_document, document, name)


def resolve_apply_payload(
    registry_path: Path, payload: dict[str, Any], *, name: str
) -> ResolvedProfile:
    return resolve_apply_document(_load_json(registry_path), payload, name=name)


def select_profile_path(
    *,
    argument: str | None,
    source_default: Path,
    installed: Path,
    legacy: Path,
) -> Path:
    """明示指定を優先し、省略時だけ導入済みprofileを維持する。"""
    if argument is not None:
        path = source_default if argument == "default" else Path(argument).expanduser().resolve()
    elif installed.is_file() and not installed.is_symlink():
        path = installed
    elif legacy.is_file() and not legacy.is_symlink():
        path = legacy
    else:
        path = source_default
    if not path.is_file() or path.is_symlink():
        raise ProfileError(f"profileが見つからないかsymlinkです: {path}")
    return path


def active_registry(source_registry: Path, paths: "UserPaths") -> Path:
    """いま正本として使うregistry。

    設定画面でmodelを足すと、選択分を実体化した registry が state_dir へ書かれる。
    それがあるならそちら、無ければ同梱registry。profileと同じく「導入済みが
    あれば維持する」規則に揃える。picker・guard・watcher・doctor・設定画面が
    全て同じ判断をするよう、選択規則はここ1箇所にする。
    """
    installed = paths.installed_registry
    if installed.is_file() and not installed.is_symlink():
        return installed
    return source_registry


def installed_profile(
    registry_path: Path, paths: "UserPaths", *, argument: str | None = None
) -> tuple[Path, ResolvedProfile]:
    """picker・guard・watcher・doctor・設定画面が共有するprofile選択規則。

    `registry_path` は**同梱**registryを指す。profileのfallbackは常にその隣の
    `profiles/default.json` に固定する。一方、突き合わせるregistry自体は
    導入済みがあればそちらを使う。この2つを別々に解決するのは、導入済み
    registryが `state_dir` にあり、その隣にprofileの正本が無いため。
    """
    selected = select_profile_path(
        argument=argument,
        source_default=registry_path.parent.parent / "profiles/default.json",
        installed=paths.installed_profile,
        legacy=paths.codex_home / "profile.json",
    )
    return selected, resolve_profile(active_registry(registry_path, paths), selected)
