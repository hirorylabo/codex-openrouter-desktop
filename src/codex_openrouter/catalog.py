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

from . import pricing, toolcompat

OR_PREFIX = "[OR] "

SNAPSHOT_SCHEMA_VERSION = 1

# nativeだけが持つ能力フィールド。cloneが継ぐとOpenRouterモデルが
# native由来の機能を主張することになるので中和する。
#
# 中和値はnative側の型を保つこと。codexのcatalog deserializerはフィールドごとに
# 型を要求し、boolフィールドをnullにすると `invalid type: null, expected a boolean`
# でcatalog全体を拒否する（build 6662 / codex-cli 0.148.0-alpha.9で実測）。
# 1件でも型を外すとpickerからOpenRouterモデルが丸ごと消えるため、
# ここは「フィールド → 中和値」の対応で持つ。
NATIVE_ONLY_FIELDS: dict[str, Any] = {
    "multi_agent_version": None,
    # Apps(Connectors)はChatGPTアカウント側の機能。build 6662で新設された。
    "include_apps_usage_instructions": False,
    # auto reviewはnative側の仕組み。ORモデルをそれでgateしない。build 6720で新設。
    "node_repl_auto_review_required": False,
    # GPT-5.6向けCode Modeを継承しない。router modelはdirect tool callだけを公開する。
    "node_repl_disabled": True,
    # `true` を継ぐとcodexはresponses-lite形式で送り、tool定義がtop-levelの
    # `tools` ではなく `input[0].additional_tools` に載る。toolbridgeは両形式を
    # 扱えるが（`_tool_group`）、classic形式のほうが実測量が多いので既知の
    # 経路へ寄せる。実機gate 2が落ちた原因の1つがこの継承だった。
    "use_responses_lite": False,
}

DIRECT_TOOL_FIELDS: dict[str, Any] = {
    "tool_mode": "direct",
    "supports_search_tool": False,
    "experimental_supported_tools": [],
}

# cloneが継ぐと誤解を招く提示用フィールド。
RESET_FIELDS: dict[str, Any] = {
    "availability_nux": None,
    "upgrade": None,
    "additional_speed_tiers": [],
    "service_tiers": [],
}

# build 6720のcloneテンプレートが持つフィールド。純正appの更新でここに無い
# フィールドが現れたら、cloneがそれを黙って継いでいる合図になる。
# 「取り得る全フィールド」ではなく「いまテンプレートが実際に持つもの」を数える。
KNOWN_TEMPLATE_FIELDS = frozenset(
    {
        "additional_speed_tiers",
        "apply_patch_tool_type",
        "availability_nux",
        "base_instructions",
        "comp_hash",
        "context_window",
        "default_reasoning_level",
        "default_reasoning_summary",
        "default_verbosity",
        "description",
        "display_name",
        "effective_context_window_percent",
        "experimental_supported_tools",
        "include_apps_usage_instructions",
        "include_plugin_usage_instructions",
        "include_skills_usage_instructions",
        "input_modalities",
        "max_context_window",
        "model_messages",
        "multi_agent_version",
        "node_repl_auto_review_required",
        "node_repl_disabled",
        "priority",
        "service_tiers",
        "shell_type",
        "slug",
        "support_verbosity",
        "supported_in_api",
        "supported_reasoning_levels",
        "supports_image_detail_original",
        "supports_parallel_tool_calls",
        "supports_search_tool",
        "tool_mode",
        "truncation_policy",
        "upgrade",
        "use_responses_lite",
        "visibility",
        "web_search_tool_type",
    }
)


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


def unknown_template_fields(natives: list[dict]) -> list[str]:
    """cloneテンプレートに現れた未知フィールドを返す。

    cloneは`NATIVE_ONLY_FIELDS`と`RESET_FIELDS`に挙げたものだけを中和し、
    残りはそのままOpenRouter entryへ引き継ぐ。純正appが能力フィールドを増やすと
    OpenRouterモデルがそれを黙って主張することになるので、ここで検出だけする。

    継がせるか中和するかは人間が決める。判定はしない。
    """
    return sorted(set(clone_template(natives)) - KNOWN_TEMPLATE_FIELDS)


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
            description = pricing.describe(slug, spec, prices, registry)
        else:
            description = spec["capability"]
        tool_status = spec.get("tool_support", "unknown")
        tool_reason = spec.get("tool_support_reason") or "Codex tool互換は未確認です"
        entry["description"] = (
            f"{description}\n\nCodex tool: {tool_status} — {tool_reason}"
        )
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
        # auto review審査(`codex-auto-review`)をこのentry自身へ向ける。
        # overrideなしの審査要求はguardが許可しないので、これがないと
        # approvals_reviewer="auto_review"環境でapply_patchが必ず拒否される
        # （0822 gate 2実測）。guard側も対応するalias許可を持つ。
        entry["auto_review_model_override"] = slug

        for source in (RESET_FIELDS, NATIVE_ONLY_FIELDS):
            for field, value in source.items():
                if field in entry:
                    entry[field] = json.loads(json.dumps(value))
        for field, value in DIRECT_TOOL_FIELDS.items():
            entry[field] = json.loads(json.dumps(value))

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
        for field, neutral in NATIVE_ONLY_FIELDS.items():
            # JSON表現で比べる。`0 == False` を同一視すると、boolを要求する
            # フィールドの中和漏れを見逃す。
            if field in entry and json.dumps(entry[field]) != json.dumps(neutral):
                raise CatalogError(
                    f"{slug} の {field} が中和されていません: {entry[field]!r} != {neutral!r}"
                )
        for field, expected_value in DIRECT_TOOL_FIELDS.items():
            if json.dumps(entry.get(field)) != json.dumps(expected_value):
                raise CatalogError(
                    f"{slug} の {field} がdirect tool契約と一致しません: "
                    f"{entry.get(field)!r} != {expected_value!r}"
                )


def previous_path(path: Path) -> Path:
    """`write` が残す1世代前のcatalog path。

    profile変更時はこれも一緒に消す。片方だけ残すと、次回起動で組み直したcatalogと
    profile外モデルを含む世代が同じディレクトリに並ぶ。
    """
    return path.with_suffix(path.suffix + ".previous")


def stale_paths(path: Path) -> list[Path]:
    """再生成させるために消すべきcatalogファイル。

    profile変更のほか、cloneの中和規則が変わったときにも使う。純正appのbuildが
    同じだと `refresh_catalog_if_needed` は素通りするので、消しておかないと
    古い規則で組んだcatalogがそのまま使われ続ける。
    """
    candidates = (path, previous_path(path))
    return [
        candidate
        for candidate in candidates
        if candidate.is_file() and not candidate.is_symlink()
    ]


def _write_atomic(document: dict, path: Path) -> Path:
    """JSONを原子的に置換する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    return path


def write(document: dict, path: Path) -> Path:
    """検証済みcatalogを原子的に置換し、1世代前を残す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = previous_path(path)
        previous.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return _write_atomic(document, path)


def read_snapshot(path: Path) -> dict | None:
    """cloneテンプレートのsnapshotを読む。無い・壊れているならNone。

    snapshotは診断の補助でしかないので、読めないことをエラーにしない。
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or not isinstance(document.get("template"), dict):
        return None
    return document


def snapshot_template(natives: list[dict], path: Path, *, version: str, build: str) -> Path:
    """cloneテンプレートを1世代残す。

    次のapp更新で、フィールドの増減だけでなく値の変化まで機械的に取れるようにする。
    `unknown_template_fields` は名前の増加しか見ないので、値だけが変わる更新は素通りする。

    `.previous` には常に「1つ前のbuild」を置きたいので、buildが変わっていないときは
    rotateしない。profile変更のたびにrotateすると `.previous` が同じbuildで埋まり、
    比較対象の旧buildが消える。
    """
    current = read_snapshot(path)
    if current is not None and current.get("build") != build:
        previous_path(path).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return _write_atomic(
        {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "version": version,
            "build": build,
            "template": clone_template(natives),
        },
        path,
    )


def template_field_drift(snapshot: dict, natives: list[dict]) -> dict[str, list[str]]:
    """snapshotのテンプレートと現在のテンプレートの、フィールド名の差分。

    値そのものは返さない。`base_instructions` のように毎buildで変わる大きな値を
    そのまま診断へ載せると読めなくなる。
    """
    old = snapshot.get("template") or {}
    new = clone_template(natives)
    changed = [
        field
        for field in set(old) & set(new)
        if json.dumps(old[field], sort_keys=True) != json.dumps(new[field], sort_keys=True)
    ]
    return {
        "added": sorted(set(new) - set(old)),
        "removed": sorted(set(old) - set(new)),
        "changed": sorted(changed),
    }


def generate(
    codex: Path,
    codex_home: Path,
    registry_path: Path,
    output: Path,
    price_state: Path | None = None,
    model_ids: Iterable[str] | None = None,
    snapshot: Path | None = None,
    build_id: tuple[str, str] | None = None,
    tool_compatibility: Path | None = None,
) -> Path:
    """取得 → 価格解決 → 組み立て → 契約検証 → 原子的置換。

    価格取得が失敗してもregistryのfallbackへ倒れるので、ここは止まらない。

    `snapshot` と `build_id` を渡すと、置換が成功した後にcloneテンプレートを残す。
    次のapp更新でテンプレートの差分を取るためのもので、catalogの生成自体には要らない。
    書けなくても起動を止めない。
    """
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    all_models = registry["models"]
    selected = tuple(model_ids) if model_ids is not None else tuple(all_models)
    registry_models = {model: dict(all_models[model]) for model in selected}
    if tool_compatibility is not None and build_id is not None:
        _version, build_number = build_id
        for model, spec in registry_models.items():
            spec.update(toolcompat.support_for(model, spec, tool_compatibility, build_number))
    prices = pricing.resolve(registry, price_state)
    natives = bundled_models(codex, codex_home)
    document = build(natives, registry_models, prices, registry)
    validate(document, registry_models, all_models)
    written = write(document, output)
    if snapshot is not None and build_id is not None:
        version, build_number = build_id
        try:
            snapshot_template(natives, snapshot, version=version, build=build_number)
        except OSError:
            # catalogは既に置換済み。診断用のsnapshotが書けなかっただけで
            # 例外を上げると、supervisorがbuildを記録できず毎回組み直しになる。
            pass
    return written
