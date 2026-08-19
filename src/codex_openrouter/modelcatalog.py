"""OpenRouterのライブcatalogを取り込み、registryエントリへ変換する。

同梱registryは元々5件を手で書いた正本だった。設定画面からmodelを足せるように
するため、**同じ内容をライブAPIから導出できる**ようにしたのがこのモジュール。
導出規則が同梱5件を再現することを [tests/test_modelcatalog.py](../../tests/test_modelcatalog.py)
で固定しているので、規則を壊すとテストが落ちる。

取得元と、それぞれ落ちたときの倒れ方:

  `/api/v1/models`          無認証。候補そのもの。落ちたらcacheのみで動く。
  `/api/v1/endpoints/zdr`   無認証。ZDR可否と最安ZDR価格。
  `/api/frontend/v1/all-providers`
                            **非公開API**。学習ポリシーのバッジにしか使わないので、
                            落ちたら `trains_on_data: null`（不明）にして続行する。
  `/api/v1/datasets/rankings-daily`
                            **API key必須**・日次トップ50のみ・500 req/日。
                            落ちたら利用量を出さないだけで候補一覧は出る。

利用量は「接続数」ではなく**トークン総数**である。OpenRouterが公開しているのが
それだけなので、UIも `models list --json` もトークンとして名乗る。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import time
from typing import Any
import urllib.parse

from . import pricing
from . import toolcompat
from .app import UserPaths, write_json

CACHE_SCHEMA_VERSION = 1

# OpenRouterが実際に返すeffortはこの7語（410件を走査して確認）。未知の語は
# 落とさず末尾へ回す。並び順を固定するのは、registryのeffortsが昇順だから。
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# registryのfallback価格と同じ3成分。`price_refresh.components` と揃える。
PRICE_COMPONENTS = {"input": "prompt", "output": "completion", "cache_read": "input_cache_read"}


class CatalogError(RuntimeError):
    pass


def contract(registry: dict) -> dict:
    try:
        return registry["catalog_refresh"]
    except KeyError as exc:
        raise CatalogError("registryにcatalog_refresh契約がありません") from exc


# --- 導出規則 --------------------------------------------------------------


def display_name(name: str) -> str:
    """`"DeepSeek: DeepSeek V4 Pro"` → `"DeepSeek V4 Pro"`。

    OpenRouterの `name` はベンダー名を前置する。pickerでは
    `[OpenRouter]` prefixが別途付くので、ここで二重の肩書きを落とす。
    """
    head, separator, tail = name.partition(": ")
    return tail if separator and tail else name


def ordered_efforts(raw: Any) -> list[str]:
    """APIは降順で返す。registryは昇順なので既知の語順へ並べ直す。"""
    if not isinstance(raw, list):
        return []
    known = [e for e in raw if e in EFFORT_ORDER]
    unknown = [e for e in raw if isinstance(e, str) and e not in EFFORT_ORDER]
    return sorted(known, key=EFFORT_ORDER.index) + unknown


def _price_label(raw: Any) -> str | None:
    """per-tokenの文字列を $/1M の表示文字列へ。範囲外・不正はNone。"""
    try:
        value = Decimal(str(raw)) * pricing.PRICE_SCALE
    except Exception:  # noqa: BLE001 - Decimalは複数の例外型を投げる
        return None
    if not (Decimal("0") <= value < Decimal("1000")):
        return None
    return pricing.decimal_label(value)


def _headline(document: Any) -> dict[str, str] | None:
    if not isinstance(document, dict):
        return None
    prices = {key: _price_label(document.get(api)) for key, api in PRICE_COMPONENTS.items()}
    if prices["input"] is None or prices["output"] is None:
        return None
    # cache_readを持たないmodelは244/410件中の残り。0扱いにすると嘘になるので
    # 「無料」ではなく「入力価格と同じ」でもなく、素直に0を入れる規約にする。
    prices["cache_read"] = prices["cache_read"] or "0"
    return prices


def _cheapest_zdr(endpoints: list[dict]) -> dict[str, dict[str, str]] | None:
    """成分ごとに最安の稼働ZDR endpointを選ぶ。

    **ZDRで動けるかと、ZDR価格を出せるかは別**である。cache価格を公開しない
    providerは珍しくない（実測で80 modelが該当）ので、揃わない成分は落として
    残りを返す。ここで全体をNoneにすると、実際にはZDRで動くmodelを
    「ZDRなし」と誤って表示し、guardがZDR強制を外してしまう。
    入力・出力の両方が無いときだけ価格なし扱いにする。
    """
    chosen: dict[str, dict[str, str]] = {}
    for key, api in PRICE_COMPONENTS.items():
        candidates: list[tuple[Decimal, str, str]] = []
        for endpoint in endpoints:
            prices = endpoint.get("pricing")
            provider = endpoint.get("provider_name")
            if not isinstance(prices, dict) or api not in prices:
                continue
            if not isinstance(provider, str) or not provider.strip():
                continue
            label = _price_label(prices[api])
            if label is not None:
                candidates.append((Decimal(label), provider.strip(), label))
        if not candidates:
            continue
        _value, provider, label = min(candidates, key=lambda item: (item[0], item[1]))
        chosen[key] = {"price": label, "provider": provider}
    if "input" not in chosen or "output" not in chosen:
        return None
    return chosen


def zdr_index(payload: Any) -> dict[str, list[dict]]:
    """稼働中(status==0)のZDR endpointをmodelごとに束ねる。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise CatalogError("ZDR endpoints APIにdata配列がありません")
    index: dict[str, list[dict]] = {}
    for endpoint in data:
        if not isinstance(endpoint, dict) or endpoint.get("status") != 0:
            continue
        model = endpoint.get("model_id")
        if isinstance(model, str):
            index.setdefault(model, []).append(endpoint)
    return index


def training_providers(payload: Any) -> set[str]:
    """`dataPolicy.training` が真のprovider表示名。

    非公開APIなので、形が違えば例外にして呼び出し側で「不明」へ倒す。
    """
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        raise CatalogError("providers APIがlistを返しませんでした")
    names = {
        provider.get("name")
        for provider in data
        if isinstance(provider, dict)
        and isinstance((provider.get("dataPolicy") or {}), dict)
        and (provider.get("dataPolicy") or {}).get("training") is True
    }
    return {name for name in names if isinstance(name, str)}


def usage_index(payload: Any, windows: list[int]) -> dict[str, dict[str, str]]:
    """rankings-dailyを `canonical_slug -> {"7d": "...", ...}` へ畳む。

    join keyは `model_permaslug`。modelsの `canonical_slug` と同じ形式なので
    そこで突き合わせる。合致しなかった行は捨てる（呼び出し側が件数を見る）。

    窓の起点は**payloadの最新日**であって今日ではない。この dataset は
    「直近の完了済みUTC日」までしか無く、遅延もする。今日を起点にすると、
    遅れた日だけ 1d が黙って0になる。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise CatalogError("rankings APIにdata配列がありません")

    rows: list[tuple[str, date, int]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        slug, day, tokens = row.get("model_permaslug"), row.get("date"), row.get("total_tokens")
        # `other` は上位50件圏外の合計行で、特定modelの利用量ではない。
        if not isinstance(slug, str) or slug == "other" or not isinstance(day, str):
            continue
        try:
            rows.append((slug, date.fromisoformat(day), int(tokens)))
        except (ValueError, TypeError):
            continue
    if not rows:
        return {}

    anchor = max(day for _slug, day, _amount in rows)
    totals: dict[str, dict[int, int]] = {}
    for slug, day, amount in rows:
        age = (anchor - day).days
        bucket = totals.setdefault(slug, dict.fromkeys(windows, 0))
        for window in windows:
            if age < window:
                bucket[window] += amount
    return {
        slug: {f"{window}d": str(bucket[window]) for window in windows}
        for slug, bucket in totals.items()
    }


def derive_row(
    model: dict,
    *,
    zdr_endpoints: list[dict],
    trains: bool | None,
    usage: dict[str, str] | None,
    codex_modalities: list[str],
) -> dict[str, Any] | None:
    """1件のライブmodelを候補行へ。tool非対応も状態付きで残す。"""
    identifier = model.get("id")
    if not isinstance(identifier, str) or identifier.startswith(("openrouter/", "~")):
        return None
    parameters = model.get("supported_parameters")
    tool_support, tool_reason = toolcompat.metadata_support(parameters)
    architecture = model.get("architecture") or {}
    inputs = [m for m in (architecture.get("input_modalities") or []) if isinstance(m, str)]
    outputs = architecture.get("output_modalities") or []
    if "text" not in inputs or "text" not in outputs:
        return None
    headline = _headline(model.get("pricing"))
    if headline is None:
        return None

    # ZDRで動けるかは「稼働endpointがあるか」で決まる。価格が引けたかとは独立。
    zdr = _cheapest_zdr(zdr_endpoints) if zdr_endpoints else None
    reasoning = model.get("reasoning") or {}
    efforts = ordered_efforts(reasoning.get("supported_efforts"))
    default_effort = reasoning.get("default_effort")
    return {
        "id": identifier,
        "display_name": display_name(str(model.get("name") or identifier)),
        "canonical_slug": model.get("canonical_slug") or identifier,
        "description": str(model.get("description") or "").strip(),
        "created": model.get("created"),
        "context_window": model.get("context_length"),
        "openrouter_modalities": inputs,
        "codex_modalities": [m for m in inputs if m in codex_modalities] or ["text"],
        "efforts": efforts,
        "default_effort": default_effort if default_effort in efforts else None,
        "supports_parallel_tool_calls": (
            isinstance(parameters, list) and "parallel_tool_calls" in parameters
        ),
        "supported_parameters": parameters,
        "tool_support": tool_support,
        "tool_support_reason": tool_reason,
        "tool_verified_at": None,
        "zdr_supported": bool(zdr_endpoints),
        "trains_on_data": trains,
        "free": headline["input"] == "0" and headline["output"] == "0",
        "headline": headline,
        "zdr": zdr,
        "usage_tokens": usage,
    }


def reasoning_note(row: dict) -> str:
    efforts = row["efforts"]
    if not efforts:
        return "Reasoningはprovider-controlled。OpenRouterがeffort段階を公開していないためUI選択なし。"
    listed = "/".join(efforts)
    if row["default_effort"]:
        return f"Reasoningは{listed}、既定{row['default_effort']}。"
    return f"Reasoningは{listed}。"


def entry_for(row: dict, curated: dict | None = None) -> dict[str, Any]:
    """候補行をregistryエントリへ。同梱registryにある説明文は上書きしない。

    `capability` と `reasoning_note` だけはAPIから導出しきれない（英語の
    `description` しか無い）。同梱5件の日本語はここで温存する。

    `fallback_headline` はオフライン時に使うsnapshotであって、ライブ値と一致し
    続ける保証は無い。OpenRouterのheadline価格は既定endpointの入れ替わりで動く
    （実測で `z-ai/glm-5.2` が1時間のうちに 0.5/3.15 → 0.63/1.98 へ変わった）。
    だから導出規則の回帰テストはfixtureに対して行う。ライブと突き合わせると
    価格の揺れで落ちてしまい、規則の壊れと区別が付かない。
    """
    entry: dict[str, Any] = {
        "display_name": row["display_name"],
        "canonical_slug": row["canonical_slug"],
        "context_window": row["context_window"],
        "openrouter_modalities": row["openrouter_modalities"],
        "codex_modalities": row["codex_modalities"],
        "efforts": row["efforts"],
        "default_effort": row["default_effort"],
        "supports_parallel_tool_calls": row["supports_parallel_tool_calls"],
        "tool_support": row["tool_support"],
        "tool_support_reason": row["tool_support_reason"],
        "capability": (curated or {}).get("capability") or row["description"],
        "reasoning_note": (curated or {}).get("reasoning_note") or reasoning_note(row),
        "zdr_supported": row["zdr_supported"],
        "fallback_headline": dict(row["headline"]),
    }
    if row["zdr"] is not None:
        entry["fallback_zdr"] = {key: dict(value) for key, value in row["zdr"].items()}
    return entry


# --- 組み立て --------------------------------------------------------------


def build(
    registry: dict,
    *,
    models_document: Any,
    zdr_document: Any,
    providers_document: Any = None,
    rankings_document: Any = None,
) -> dict[str, Any]:
    """取得済みの生documentから候補一覧を作る。ここはネットワークに触らない。"""
    settings = contract(registry)
    data = models_document.get("data") if isinstance(models_document, dict) else None
    if not isinstance(data, list) or not data:
        raise CatalogError("Models APIにdata配列がありません")
    endpoints = zdr_index(zdr_document)

    try:
        trainers = training_providers(providers_document) if providers_document else None
    except CatalogError:
        trainers = None

    usage: dict[str, dict[str, str]] = {}
    if rankings_document is not None:
        try:
            usage = usage_index(rankings_document, list(settings["usage_windows"]))
        except CatalogError:
            usage = {}

    codex_modalities = list(settings["codex_modalities"])
    rows: list[dict[str, Any]] = []
    matched = 0
    for model in data:
        if not isinstance(model, dict):
            continue
        served = endpoints.get(model.get("id"), [])
        # 学習ポリシーはprovider単位でしか公開されていない。modelがどのproviderで
        # 動くかを知るには per-model endpoints APIを329回叩く必要があるので、
        # ここで断定できるのはZDR endpointを持つmodelだけにする（ZDRなら保持も
        # 学習もしない）。それ以外は「不明」で出す。憶測でfalseとは言わない。
        if trainers is None or not served:
            trains: bool | None = None
        else:
            trains = bool({e.get("provider_name") for e in served} & trainers)
        found = usage.get(model.get("canonical_slug"))
        row = derive_row(
            model,
            zdr_endpoints=served,
            trains=trains,
            usage=found,
            codex_modalities=codex_modalities,
        )
        if row is None:
            continue
        if found:
            matched += 1
        rows.append(row)

    rows.sort(key=lambda row: -(row["created"] or 0))
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "models": rows,
        "usage_available": bool(usage),
        # joinが壊れたら0のままになる。UIに出さなくてもdoctorとlogで気づける。
        "usage_matched": matched,
        "training_policy_available": trainers is not None,
    }


def fetch(registry: dict, *, key: str | None = None, today: date | None = None) -> dict[str, Any]:
    """ライブ取得。必須2本が落ちたら例外、任意2本は静かに諦める。"""
    settings = contract(registry)
    models_document = pricing.fetch_json(settings["models_url"])
    zdr_document = pricing.fetch_json(settings["zdr_endpoints_url"])

    providers_document = None
    try:
        providers_document = pricing.fetch_json(settings["providers_url"])
    except pricing.PricingError:
        pass

    rankings_document = None
    if key:
        end = today or date.today()
        window = max(settings["usage_windows"])
        query = urllib.parse.urlencode(
            {
                "start_date": (end - timedelta(days=window)).isoformat(),
                "end_date": end.isoformat(),
                "period": "day",
            }
        )
        try:
            rankings_document = _authenticated_json(f"{settings['rankings_url']}?{query}", key)
        except pricing.PricingError:
            rankings_document = None

    document = build(
        registry,
        models_document=models_document,
        zdr_document=zdr_document,
        providers_document=providers_document,
        rankings_document=rankings_document,
    )
    document["fetched_at"] = round(time.time(), 3)
    return document


def _authenticated_json(url: str, key: str) -> dict:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": pricing.USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15.0) as response:
            payload = json.load(response)
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        raise pricing.PricingUnavailableError(f"OpenRouter APIへ到達できません: {url}") from exc
    if not isinstance(payload, dict):
        raise pricing.PricingError(f"OpenRouter APIがobject以外を返しました: {url}")
    return payload


def read_cache(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None
    models = document.get("models")
    if not isinstance(models, list):
        return None
    # multi-router開発版が同じschema_version=1で残したcacheを再利用しない。
    # profile schemaは互換のため据え置くが、候補cacheへ旧第2router行を残すと
    # 撤去後もUIへ出る。
    if any(
        isinstance(row, dict)
        and "router" in row
        for row in models
    ):
        return None
    return document


def load(
    paths: UserPaths,
    registry: dict,
    *,
    key: str | None = None,
    refresh: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """cache優先で候補一覧を返す。取得に失敗してもcacheがあれば止めない。

    `pricing.should_fetch` と同じTTL/backoffの考え方を使う。設定画面を開くたびに
    410件を取り直すと、OpenRouter側にもUIの体感にも無駄が出る。
    """
    cached = read_cache(paths.catalog_cache)
    stale = refresh or pricing.should_fetch(contract(registry), paths.catalog_cache_state)
    if cached is not None and not stale:
        return {**cached, "source": "cache"}

    try:
        document = fetch(registry, key=key, today=today)
    except pricing.PricingError as exc:
        pricing.write_refresh_state(paths.catalog_cache_state, "failure", time.time())
        if cached is not None:
            return {**cached, "source": "cache"}
        raise CatalogError(f"model catalogを取得できません: {exc}") from exc

    write_json(paths.catalog_cache, document)
    pricing.write_refresh_state(paths.catalog_cache_state, "success", time.time())
    return {**document, "source": "live"}
