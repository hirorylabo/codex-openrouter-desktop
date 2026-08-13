"""OpenRouterのheadline価格とZDR稼働endpoint最安価格を取得する。

v0.1.xでは `codex-openrouter-refresh` テンプレートに埋まっていたロジック。
案Dではcatalog生成側（[catalog.py](catalog.py)）が使うのでモジュールへ移した。

契約は [models/registry.json](../../models/registry.json) の `price_refresh` を正本とする。
ネットワークが不通でも registry の fallback へ倒し、catalog生成自体は止めない。
価格は全て「$/1M tokens」に正規化して扱う。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import time
from typing import Any
import urllib.error
import urllib.request

PRICE_SCALE = Decimal(1_000_000)
PRICE_NOTE = "価格はOpenRouter公表値で、実際の請求はOpenRouterの課金が正本。"
ZDR_ABSENT_NOTE = "ZDRなし（送信内容がproviderに保持される可能性あり）"
USER_AGENT = "codex-openrouter/2"


class PricingError(RuntimeError):
    pass


class PricingUnavailableError(PricingError):
    """一時的に取得できないだけ。fallbackへ倒してよい。"""


def _components(registry: dict) -> dict[str, tuple[str, str]]:
    raw = registry["price_refresh"]["components"]
    return {key: (value["label"], value["api_key"]) for key, value in raw.items()}


def decimal_label(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def validate_price(label: str, raw_value: object, *, scale: bool = True) -> Decimal:
    """live価格はper-token。registryのfallbackは既にper-Mなのでscaleしない。"""
    try:
        value = Decimal(str(raw_value))
    except (InvalidOperation, ValueError) as exc:
        raise PricingError(f"不正なOpenRouter価格 {label}: {raw_value}") from exc
    if scale:
        value *= PRICE_SCALE
    if not (Decimal("0") <= value < Decimal("1000")):
        raise PricingError(f"価格が範囲外です {label}: {value}")
    return value


def validate_provider_name(raw_value: object) -> str:
    provider = str(raw_value or "").strip()
    if not provider or len(provider) > 64:
        raise PricingError(f"不正なZDR provider名: {raw_value!r}")
    return provider


def fetch_json(url: str, timeout: float = 15.0) -> dict:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        raise PricingUnavailableError(f"OpenRouter APIへ到達できません: {url} ({exc})") from exc
    if not isinstance(payload, dict):
        raise PricingError(f"OpenRouter APIがobject以外を返しました: {url}")
    return payload


# prompt/completionは全modelが持つ。cache系は410件中244件しか公開していないので、
# 欠けていても失敗にしない。ここで例外にすると、cache価格を出さないmodelを1件
# 選んだだけで**全model**の価格がfallbackへ落ちる。
REQUIRED_PRICE_KEYS = frozenset({"prompt", "completion"})


def parse_headline(payload: dict, registry: dict) -> dict[str, dict[str, Decimal]]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise PricingError("Models APIにdata配列がありません")
    components = _components(registry)
    by_id = {item.get("id"): item for item in data if isinstance(item, dict)}
    prices: dict[str, dict[str, Decimal]] = {}
    for slug in registry["models"]:
        item = by_id.get(slug)
        if not isinstance(item, dict):
            raise PricingUnavailableError(f"Models APIに {slug} がありません")
        published = item.get("pricing")
        if not isinstance(published, dict):
            raise PricingUnavailableError(f"{slug} のheadline価格がありません")
        entry: dict[str, Decimal] = {}
        for key, (label, api_key) in components.items():
            if api_key not in published and api_key not in REQUIRED_PRICE_KEYS:
                entry[key] = Decimal(0)
                continue
            entry[key] = validate_price(f"headline {label} for {slug}", published.get(api_key))
        prices[slug] = entry
    return prices


def zdr_capable(spec: dict) -> bool:
    """registryが「このmodelはZDRで動く」と記録しているか。

    既定はTrue。`zdr_supported` を持たない旧registryは全modelがZDR前提だった。
    """
    return bool(spec.get("zdr_supported", True))


def parse_zdr(payload: dict, registry: dict) -> dict[str, dict[str, tuple[Decimal, str]]]:
    """稼働中(status==0)のZDR endpointだけを見て、成分ごとの最安を選ぶ。

    非ZDR modelはそもそもZDR価格を持たないので、集合から外すだけにする。
    ここで例外にすると、非ZDR modelを1件選んだだけで**全model**の価格が
    fallbackへ落ちる。
    """
    data = payload.get("data")
    if not isinstance(data, list):
        raise PricingError("ZDR endpoints APIにdata配列がありません")
    components = _components(registry)
    prices: dict[str, dict[str, tuple[Decimal, str]]] = {}
    for slug, spec in registry["models"].items():
        if not zdr_capable(spec):
            continue
        active = [
            item
            for item in data
            if isinstance(item, dict)
            and item.get("model_id") == slug
            and item.get("status") == 0
        ]
        if not active:
            raise PricingUnavailableError(f"{slug} に稼働中のZDR endpointがありません")
        per_component: dict[str, tuple[Decimal, str]] = {}
        for key, (label, api_key) in components.items():
            candidates: list[tuple[Decimal, str]] = []
            for endpoint in active:
                published = endpoint.get("pricing")
                if not isinstance(published, dict) or api_key not in published:
                    continue
                candidates.append(
                    (
                        validate_price(f"ZDR {label} for {slug}", published[api_key]),
                        validate_provider_name(endpoint.get("provider_name")),
                    )
                )
            if not candidates:
                if api_key in REQUIRED_PRICE_KEYS:
                    raise PricingUnavailableError(f"{slug} の稼働中ZDR {label} 価格がありません")
                # cache価格を公開しないZDR providerは珍しくない。成分を落とすだけにする。
                continue
            per_component[key] = min(candidates, key=lambda item: (item[0], item[1]))
        prices[slug] = per_component
    return prices


def fallback_prices(registry: dict) -> dict[str, Any]:
    """registryに焼き込んだ既知値。既にper-Mなのでscaleしない。"""
    components = _components(registry)
    headline: dict[str, dict[str, Decimal]] = {}
    zdr: dict[str, dict[str, tuple[Decimal, str]]] = {}
    for slug, spec in registry["models"].items():
        headline[slug] = {
            key: validate_price(f"fallback headline {key} for {slug}",
                                spec["fallback_headline"][key], scale=False)
            for key in components
        }
        published = spec.get("fallback_zdr")
        if not zdr_capable(spec) or not isinstance(published, dict):
            continue
        zdr[slug] = {
            key: (
                validate_price(f"fallback ZDR {key} for {slug}",
                               published[key]["price"], scale=False),
                validate_provider_name(published[key]["provider"]),
            )
            for key in components
            if key in published
        }
    return {"headline": headline, "zdr": zdr, "source": "fallback"}


def fetch_prices(registry: dict) -> dict[str, Any]:
    contract = registry["price_refresh"]
    return {
        "headline": parse_headline(fetch_json(contract["models_url"]), registry),
        "zdr": parse_zdr(fetch_json(contract["zdr_endpoints_url"]), registry),
        "source": "live",
    }


def _read_state(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def write_refresh_state(path: Path, result: str, now: float) -> None:
    """取得の成否と時刻だけを残す。price refreshとmodel catalogが共有する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps({"schema_version": 1, "result": result, "at": round(now, 3)}),
        encoding="utf-8",
    )
    tmp.chmod(0o600)
    tmp.replace(path)


def should_fetch(contract: dict, state_path: Path | None, now: float | None = None) -> bool:
    """成功はTTL、失敗はbackoffの間だけ再取得を控える。

    `contract` は `success_ttl_seconds` / `failure_backoff_seconds` を持つdict。
    price refreshは `price_refresh`、model catalogは `catalog_refresh` を渡す。
    """
    if state_path is None:
        return True
    state = _read_state(state_path)
    at = state.get("at")
    if not isinstance(at, (int, float)):
        return True
    now = time.time() if now is None else now
    window = (
        contract["success_ttl_seconds"]
        if state.get("result") == "success"
        else contract["failure_backoff_seconds"]
    )
    return (now - float(at)) >= float(window)


def resolve(registry: dict, state_path: Path | None = None) -> dict[str, Any]:
    """取得を試み、駄目ならfallbackへ倒す。catalog生成は止めない。"""
    if not should_fetch(registry["price_refresh"], state_path):
        return fallback_prices(registry)
    now = time.time()
    try:
        prices = fetch_prices(registry)
    except (PricingUnavailableError, PricingError):
        if state_path is not None:
            write_refresh_state(state_path, "failure", now)
        return fallback_prices(registry)
    if state_path is not None:
        write_refresh_state(state_path, "success", now)
    return prices


def describe(slug: str, spec: dict, prices: dict[str, Any], registry: dict) -> str:
    """v0.1.xと同じ組み立て。capability + reasoning + headline + ZDR最安。"""
    components = _components(registry)
    headline = prices["headline"][slug]
    headline_text = ", ".join(
        f"{label} ${decimal_label(headline[key])}/M" for key, (label, _api) in components.items()
    )
    zdr = prices["zdr"].get(slug)
    if not zdr:
        # 純正pickerの説明文からもZDRなしと分かるようにする。設定画面を開かないと
        # 気づけない状態にはしない。
        zdr_text = ZDR_ABSENT_NOTE
    else:
        zdr_text = "ZDR稼働endpoint最安: " + ", ".join(
            f"{label} ${decimal_label(zdr[key][0])}/M ({zdr[key][1]})"
            for key, (label, _api) in components.items()
            if key in zdr
        )
    return (
        f"{spec['capability']} {spec['reasoning_note']} {slug} via OpenRouter "
        f"({spec['canonical_slug']}). 通常headline: {headline_text}. "
        f"{zdr_text}. {PRICE_NOTE}"
    )
