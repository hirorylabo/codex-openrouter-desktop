"""OpenRouter modelのCodex direct-tool互換性を検査・cacheする。

OpenRouterの ``supported_parameters: ["tools"]`` はstructured function callingの
公称にすぎず、Codexが使うfreeform/custom toolまで保証しない。そこでmetadataの
宣言と、現行ChatGPT buildに対する低token canaryを別々に扱う。
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable
import urllib.error
import urllib.request

from . import __version__
from . import toolbridge

ENDPOINT = "https://openrouter.ai/api/v1/responses"
CACHE_SCHEMA_VERSION = 1
TOOL_CONTRACT_VERSION = toolbridge.TOOL_CONTRACT_VERSION
CACHE_TTL_SECONDS = 24 * 60 * 60
STATUSES = frozenset({"verified", "partial", "declared", "unknown", "unsupported"})


class ToolCompatibilityError(RuntimeError):
    """認証・rate limit・通信・API drift。非対応とは判定せず保存を止める。"""


def metadata_support(parameters: Any) -> tuple[str, str]:
    """OpenRouter metadataを、実測前のtool状態へ正規化する。"""
    if not isinstance(parameters, list) or not all(isinstance(item, str) for item in parameters):
        return "unknown", "OpenRouterのtool metadataを解釈できません"
    if "tools" in parameters:
        return "declared", "OpenRouterはstructured function calling対応を公称（未実測）"
    return "unsupported", "OpenRouter metadataがtools対応を公称していません"


def read_cache(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        return {"schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != CACHE_SCHEMA_VERSION
        or not isinstance(document.get("entries"), dict)
    ):
        return {"schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
    return document


def _atomic_write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def cached_result(
    path: Path,
    model: str,
    build: str,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    entry = read_cache(path)["entries"].get(model)
    current = time.time() if now is None else now
    if not isinstance(entry, dict):
        return None
    if entry.get("chatgpt_build") != build:
        return None
    if entry.get("tool_contract_version") != TOOL_CONTRACT_VERSION:
        return None
    if entry.get("status") not in STATUSES:
        return None
    verified_at = entry.get("verified_at")
    if not isinstance(verified_at, (int, float)) or current - verified_at >= CACHE_TTL_SECONDS:
        return None
    return dict(entry)


def support_for(
    model: str,
    spec: dict[str, Any],
    cache_path: Path,
    build: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    cached = cached_result(cache_path, model, build, now=now)
    if cached is not None:
        return {
            "tool_support": cached["status"],
            "tool_support_reason": cached["reason"],
            "tool_verified_at": cached["verified_at"],
            "tool_provider": cached.get("provider"),
            "tool_provider_attempt": cached.get("provider_attempt"),
            "tool_contract_version": cached.get("tool_contract_version"),
        }
    status = spec.get("tool_support")
    reason = spec.get("tool_support_reason")
    if status not in {"declared", "unknown", "unsupported"}:
        status, reason = metadata_support(spec.get("supported_parameters"))
    return {
        "tool_support": status,
        "tool_support_reason": reason,
        "tool_verified_at": None,
        "tool_provider": None,
        "tool_provider_attempt": None,
        "tool_contract_version": None,
    }


def annotate_models(
    rows: list[dict[str, Any]],
    cache_path: Path,
    build: str,
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for row in rows:
        copied = dict(row)
        identifier = copied.get("id")
        if isinstance(identifier, str):
            copied.update(support_for(identifier, copied, cache_path, build, now=now))
        annotated.append(copied)
    return annotated


def compatibility_digest(
    specs: dict[str, dict[str, Any]],
    cache_path: Path,
    build: str,
    *,
    now: float | None = None,
) -> str:
    """catalog説明へ入る有効なtool状態だけを安定digestへする。"""
    document = {
        model: support_for(model, spec, cache_path, build, now=now)
        for model, spec in specs.items()
    }
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _request(
    body: dict[str, Any], key: str
) -> tuple[int, dict[str, Any], toolbridge.RouterSummary | None]:
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": f"codex-openrouter-desktop/{__version__}",
            "X-OpenRouter-Metadata": "enabled",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.load(response)
            summary = (
                toolbridge.extract_router_metadata(payload)
                if isinstance(payload, dict)
                else None
            )
            return response.status, payload, summary
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read())
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        if isinstance(payload, dict):
            toolbridge.extract_router_metadata(payload)
        return error.code, payload, None
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        raise ToolCompatibilityError(
            f"OpenRouter tool canaryへ接続できません: {type(exc).__name__}"
        ) from exc


def _body(model: str, spec: dict[str, Any], *, freeform: bool) -> dict[str, Any]:
    name = "codex_freeform_probe" if freeform else "codex_structured_probe"
    tool: dict[str, Any]
    if freeform:
        tool = {
            "type": "custom",
            "name": name,
            "description": "Return the raw text PING and do not answer normally.",
        }
    else:
        tool = {
            "type": "function",
            "name": name,
            "description": "Return the fixed value PING and do not answer normally.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string", "enum": ["PING"]}},
                "required": ["value"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    body: dict[str, Any] = {
        "model": model,
        "input": f"Call {name} exactly once with PING. Do not return a message.",
        "tools": [tool],
        "tool_choice": {"type": tool["type"], "name": name},
        # 64ではreasoning tokensに食われてfunction_callのargumentsが
        # 途切れた(実測: `{"content": "` で打ち切り)。256で余裕を持つ。
        "max_output_tokens": 256,
    }
    if spec.get("zdr_supported", True):
        # provider抽選ノイズを消す。endpointが毎回変わると structured_outputs
        # 公称の有無で canary が揺れる（0822-toolbridge-fix-plan.md 実測 3/4）。
        # zdr との併用は 2026-08-22 に OpenRouter 実測済み（status 200 / PING着弾）。
        body["provider"] = {"zdr": True, "sort": "price"}
    return body


def _call_succeeded(document: dict[str, Any], *, freeform: bool) -> bool:
    output = document.get("output")
    if not isinstance(output, list):
        raise ToolCompatibilityError("OpenRouter Responses APIのoutput契約が変わりました")
    expected_type = "custom_tool_call" if freeform else "function_call"
    expected_name = "codex_freeform_probe" if freeform else "codex_structured_probe"
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != expected_type or item.get("name") != expected_name:
            continue
        raw = item.get("input") if freeform else item.get("arguments")
        if freeform:
            return isinstance(raw, str) and raw.strip() == "PING"
        if not isinstance(raw, str):
            return False
        try:
            return json.loads(raw) == {"value": "PING"}
        except json.JSONDecodeError:
            return False
    return False


def _probe(
    model: str,
    spec: dict[str, Any],
    key: str,
    *,
    freeform: bool,
    requester: Callable[[dict[str, Any], str], tuple],
) -> tuple[bool, toolbridge.RouterSummary | None]:
    prepared = toolbridge.prepare_document(_body(model, spec, freeform=freeform))
    response = requester(prepared.document, key)
    if not isinstance(response, tuple) or len(response) not in {2, 3}:
        raise ToolCompatibilityError("tool canary requesterの応答契約が不正です")
    status, document = response[:2]
    summary = response[2] if len(response) == 3 else None
    if status in {401, 403, 404, 408, 409, 429} or status >= 500:
        raise ToolCompatibilityError(f"{model} のtool canaryを判定できません: HTTP {status}")
    if status in {400, 422}:
        return False, summary
    if status != 200:
        raise ToolCompatibilityError(f"{model} のtool canaryが予期せず失敗しました: HTTP {status}")
    if not isinstance(document, dict):
        raise ToolCompatibilityError("OpenRouter Responses APIの応答契約が変わりました")
    try:
        restored, embedded_summary = toolbridge.transform_response_document(
            document, prepared.tool_map
        )
    except toolbridge.ToolBridgeError as exc:
        raise ToolCompatibilityError(f"tool canaryの復元に失敗しました: {exc}") from exc
    if embedded_summary is not None:
        summary = embedded_summary
    return _call_succeeded(restored, freeform=freeform), summary


def verify_models(
    models: list[str],
    specs: dict[str, dict[str, Any]],
    *,
    key: str,
    build: str,
    cache_path: Path,
    force: bool = False,
    now: float | None = None,
    requester: Callable[[dict[str, Any], str], tuple] = _request,
) -> list[dict[str, Any]]:
    """指定modelを検査する。全件判定できた時だけcacheを原子的に置換する。"""
    current = time.time() if now is None else now
    document = read_cache(cache_path)
    updated = deepcopy(document)
    results: list[dict[str, Any]] = []
    for model in models:
        spec = specs.get(model)
        if spec is None:
            raise ToolCompatibilityError(f"tool canary対象が候補一覧にありません: {model}")
        cached = None if force else cached_result(cache_path, model, build, now=current)
        if cached is not None:
            status, reason, verified_at = (
                cached["status"], cached["reason"], cached["verified_at"]
            )
            provider = cached.get("provider")
            provider_attempt = cached.get("provider_attempt")
        else:
            declared, metadata_reason = metadata_support(spec.get("supported_parameters"))
            declared = spec.get("tool_support", declared)
            metadata_reason = spec.get("tool_support_reason") or metadata_reason
            if declared == "unsupported":
                status, reason, verified_at = "unsupported", metadata_reason, None
                provider = provider_attempt = None
            else:
                structured, structured_summary = _probe(
                    model, spec, key, freeform=False, requester=requester
                )
                # 短絡しない。provider抽選でstructuredが外れてもfreeformは
                # 健全かもしれないので、常に両方測る（コストは+1リクエスト/回）。
                # structured & freeform → verified / structuredのみ → partial /
                # freeformのみ → partial / 両方失敗 → unsupported
                freeform, freeform_summary = _probe(
                    model, spec, key, freeform=True, requester=requester
                )
                if structured and freeform:
                    status = "verified"
                    reason = "Tool Bridge経由でstructured functionとfreeform toolを実測済み"
                elif structured:
                    status = "partial"
                    reason = "structured functionは成功、freeform toolは非互換"
                elif freeform:
                    status = "partial"
                    reason = "freeform toolは成功、structured functionは非互換"
                else:
                    status = "unsupported"
                    reason = "Codex direct structured function callを実測できません"
                verified_at = current
                summary = freeform_summary or structured_summary
                provider = summary.provider if summary is not None else None
                provider_attempt = (
                    summary.provider_attempt if summary is not None else None
                )
                updated["entries"][model] = {
                    "chatgpt_build": build,
                    "tool_contract_version": TOOL_CONTRACT_VERSION,
                    "status": status,
                    "reason": reason,
                    "verified_at": verified_at,
                }
                if provider is not None:
                    updated["entries"][model]["provider"] = provider
                if provider_attempt is not None:
                    updated["entries"][model]["provider_attempt"] = provider_attempt
        results.append(
            {
                "id": model,
                "tool_support": status,
                "tool_support_reason": reason,
                "tool_verified_at": verified_at,
                "tool_provider": provider,
                "tool_provider_attempt": provider_attempt,
                "tool_contract_version": (
                    TOOL_CONTRACT_VERSION if verified_at is not None else None
                ),
            }
        )
    if updated != document:
        _atomic_write(cache_path, updated)
    return results
