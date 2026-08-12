from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


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


def resolve_profile(registry_path: Path, profile_path: Path) -> ResolvedProfile:
    registry_document = _load_json(registry_path)
    profile_document = _load_json(profile_path)
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

    name = profile_document.get("name", profile_path.stem)
    if not isinstance(name, str) or not name.strip():
        raise ProfileError("profile.nameは空でない文字列である必要があります")
    default_effort = registry[default_model].get("default_effort")
    if default_effort is not None and not isinstance(default_effort, str):
        raise ProfileError(f"default effortが不正です: {default_model}")
    return ResolvedProfile(
        name=name.strip(),
        models=tuple(models),
        default_model=default_model,
        default_effort=default_effort,
        registry={model: registry[model] for model in models},
    )


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
