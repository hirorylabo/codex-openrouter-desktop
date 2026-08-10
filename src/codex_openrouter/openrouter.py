from __future__ import annotations

import json
from typing import Any
import urllib.error
import urllib.request

from . import __version__


BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterError(RuntimeError):
    pass


def _get_json(path: str, key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        BASE_URL + path,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": f"codex-openrouter-desktop/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            document = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise OpenRouterError(
            f"OpenRouter APIへ接続できません: {type(exc).__name__}"
        ) from exc
    if not isinstance(document, dict):
        raise OpenRouterError("OpenRouter API responseがobjectではありません")
    return document


def validate_key_and_profile(key: str, expected_models: set[str]) -> dict[str, Any]:
    key_document = _get_json("/key", key)
    key_data = key_document.get("data")
    if not isinstance(key_data, dict) or key_data.get("is_management_key") is True:
        raise OpenRouterError("completion用OpenRouter API keyではありません")

    models_document = _get_json("/models/user", key)
    data = models_document.get("data")
    if not isinstance(data, list):
        raise OpenRouterError("OpenRouter models/user responseにdata arrayがありません")
    model_ids = {
        item.get("id")
        for item in data
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
    }
    if any(model.startswith("qwen/") or model.startswith("~qwen/") for model in model_ids):
        raise OpenRouterError("API keyの実効model集合がQwenを公開しています")
    available = {
        model
        for model in model_ids
        if not model.startswith("openrouter/") and not model.startswith("~")
    }
    if available != expected_models:
        missing = sorted(expected_models - available)
        extra = sorted(available - expected_models)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise OpenRouterError(
            "API keyの実効model集合がprofileと一致しません: " + " ".join(details)
        )
    return key_data
