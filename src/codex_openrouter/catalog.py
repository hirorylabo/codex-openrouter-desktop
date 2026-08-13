"""composite model catalog（native全件 + OpenRouter 5件）を組み立てる。

`model_catalog_json` は置換専用でconfig load時にのみ適用される。ASARパッチ無しで
pickerにOpenRouterモデルを出せることは実機で確認済み。

catalogエントリの必須フィールド（`supported_reasoning_levels` 等）は増減するため、
OpenRouterエントリは最小JSONを書かずに **native entryをcloneして差し替える**。
cloneテンプレートはslug固定にしない（将来 `gpt-5.5` が消える）。
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any, Iterable

from . import pricing

OR_PREFIX = "[OR] "

# nativeだけが持つ能力フィールド。cloneが継ぐとOpenRouterモデルが
# native由来の機能を主張することになるので中和する。
NATIVE_ONLY_FIELDS = ("multi_agent_version",)

# cloneが継ぐと誤解を招く提示用フィールド。
RESET_FIELDS: dict[str, Any] = {
    "availability_nux": None,
    "upgrade": None,
    "additional_speed_tiers": [],
    "service_tiers": [],
}


class CatalogError(RuntimeError):
    pass


def bundled_models(codex: Path, codex_home: Path) -> list[dict]:
    """対象buildのbundled catalogを正本として取り出す。"""
    result = subprocess.run(
        [str(codex), "debug", "models", "--bundled"],
        env={"CODEX_HOME": str(codex_home), "PATH": "/usr/bin:/bin"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise CatalogError(f"bundled catalogを取得できません: {result.stderr.strip()[:200]}")
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CatalogError(f"bundled catalogがJSONではありません: {exc}") from exc
    models = document.get("models")
    if not isinstance(models, list) or not models:
        raise CatalogError("bundled catalogにmodelsがありません")
    return models


def clone_template(natives: list[dict]) -> dict:
    """`visibility: list` の最初のnative entryをcloneテンプレートにする。"""
    for model in natives:
        if model.get("visibility") == "list":
            return model
    raise CatalogError("visibility=listのnative entryがありません")


def build(
    natives: list[dict],
    registry_models: dict[str, dict],
    prices: dict | None = None,
    registry: dict | None = None,
) -> dict:
    """native全件 + OpenRouter 5件のcompositeを組む。

    `prices` を渡すと説明文へ headline と ZDR稼働endpoint最安を入れる。
    省略時は registry の capability 文だけになる。
    """
    template = clone_template(natives)
    max_priority = max((model.get("priority") or 0) for model in natives)
    entries = list(natives)

    for offset, (slug, spec) in enumerate(registry_models.items(), start=1):
        entry = json.loads(json.dumps(template))
        entry["slug"] = slug
        entry["display_name"] = OR_PREFIX + spec["display_name"]
        if prices is not None and registry is not None:
            entry["description"] = pricing.describe(slug, spec, prices, registry)
        else:
            entry["description"] = spec["capability"]
        entry["visibility"] = "list"
        entry["priority"] = max_priority + offset
        entry["supported_in_api"] = True

        efforts = spec.get("efforts") or []
        entry["supported_reasoning_levels"] = [
            {"effort": effort, "description": f"{effort} reasoning"} for effort in efforts
        ]
        entry["default_reasoning_level"] = spec.get("default_effort") or (
            efforts[0] if efforts else None
        )

        context_window = spec.get("context_window")
        if context_window:
            entry["context_window"] = context_window
            if "max_context_window" in entry:
                entry["max_context_window"] = context_window
        if "input_modalities" in entry:
            entry["input_modalities"] = spec.get("codex_modalities") or ["text"]
        entry["supports_parallel_tool_calls"] = bool(spec.get("supports_parallel_tool_calls"))

        for field, value in RESET_FIELDS.items():
            if field in entry:
                entry[field] = json.loads(json.dumps(value))
        for field in NATIVE_ONLY_FIELDS:
            if field in entry:
                entry[field] = None

        entries.append(entry)

    return {"models": entries}


def validate(
    document: dict,
    registry_models: dict[str, dict],
    all_registry_ids: Iterable[str] | None = None,
) -> None:
    """契約検証。1つでも落ちたら古いcatalogを維持する。"""
    models = document.get("models")
    if not isinstance(models, list) or not models:
        raise CatalogError("compositeにmodelsがありません")

    slugs = [model.get("slug") for model in models]
    if len(slugs) != len(set(slugs)):
        raise CatalogError("slugが重複しています")

    expected = set(registry_models)
    present = {slug for slug in slugs if slug in expected}
    missing = expected - present
    if missing:
        raise CatalogError(f"OpenRouterモデルが欠落しています: {sorted(missing)}")
    if all_registry_ids is not None:
        extra = (set(slugs) & set(all_registry_ids)) - expected
        if extra:
            raise CatalogError(f"profile外のOpenRouterモデルがあります: {sorted(extra)}")

    natives = [model for model in models if model.get("slug") not in expected]
    if not natives:
        raise CatalogError("nativeモデルが1件もありません")
    if not any(model.get("visibility") == "list" for model in natives):
        raise CatalogError("visibility=listのnativeモデルがありません")

    by_slug = {model.get("slug"): model for model in models}
    for slug, spec in registry_models.items():
        entry = by_slug[slug]
        efforts = [level.get("effort") for level in entry.get("supported_reasoning_levels") or []]
        if efforts != list(spec.get("efforts") or []):
            raise CatalogError(
                f"{slug} のeffortがregistryと一致しません: {efforts} != {spec.get('efforts')}"
            )
        if entry.get("visibility") != "list":
            raise CatalogError(f"{slug} がpickerに出ません: visibility={entry.get('visibility')}")


def write(document: dict, path: Path) -> Path:
    """検証済みcatalogを原子的に置換し、1世代前を残す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = path.with_suffix(path.suffix + ".previous")
        previous.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    return path


def generate(
    codex: Path,
    codex_home: Path,
    registry_path: Path,
    output: Path,
    price_state: Path | None = None,
    model_ids: Iterable[str] | None = None,
) -> Path:
    """取得 → 価格解決 → 組み立て → 契約検証 → 原子的置換。

    価格取得が失敗してもregistryのfallbackへ倒れるので、ここは止まらない。
    """
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    all_models = registry["models"]
    selected = tuple(model_ids) if model_ids is not None else tuple(all_models)
    registry_models = {model: all_models[model] for model in selected}
    prices = pricing.resolve(registry, price_state)
    document = build(bundled_models(codex, codex_home), registry_models, prices, registry)
    validate(document, registry_models, all_models)
    return write(document, output)
