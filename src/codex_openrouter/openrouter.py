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


def effective_models(key: str) -> set[str]:
    """このkeyで実際に呼べるmodel集合。alias・meta modelは数えない。"""
    document = _get_json("/models/user", key)
    data = document.get("data")
    if not isinstance(data, list):
        raise OpenRouterError("OpenRouter models/user responseにdata arrayがありません")
    return {
        item["id"]
        for item in data
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and not item["id"].startswith(("openrouter/", "~"))
    }


def validate_key_and_profile(key: str, expected_models: set[str]) -> dict[str, Any]:
    """profileのmodelがこのkeyで実際に呼べることを確かめる。

    以前はkeyの実効集合とprofileの**完全一致**を要求し、OpenRouter側でGuardrailを
    exact allowlistとして組むことを前提にしていた。Guardrailを任意にしたので、
    確かめるのは `profile ⊆ 実効集合` の向きだけにする。

    この向きは「pickerに出るmodelは必ず呼べる」を保つ。model引退・rename・
    制限付きkeyは今までどおり検出できる。捨てるのは逆向き（実効集合 ⊆ profile）で、
    それはGuardrailが担っていた「鍵が漏れても課金は選択中のmodelまで」という別の
    保護であり、spend limitが代わりになる。
    """
    key_document = _get_json("/key", key)
    key_data = key_document.get("data")
    if not isinstance(key_data, dict) or key_data.get("is_management_key") is True:
        raise OpenRouterError("completion用OpenRouter API keyではありません")

    missing = sorted(expected_models - effective_models(key))
    if missing:
        raise OpenRouterError(
            "API keyでは呼び出せないmodelが選択されています: " + ",".join(missing)
        )
    return key_data
