"""案Dの健全性検査。

v0.1.xのdoctorはASAR hash・patch marker・clone appの署名・adapter.jsonを
突き合わせていたが、案Dでは純正appを一切変更しないので全て不要になった。
代わりに「configのmarker blockが正しいか」「catalogが契約を満たすか」
「guardが番をしているか」を見る。

**hash固定はしない。** Codexが週2回更新される前提なので、特定buildへ固定した
時点で更新のたびに壊れる。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from . import catalog as catalog_module
from . import configblock
from .app import UserPaths
from .auth import CredentialStore
from .supervisor import CATALOG_BLOCK, DEFAULT_PORT, PROVIDER_BLOCK

ENDPOINT = "https://openrouter.ai/api/v1/responses"
KEY_PATTERN = re.compile(r"sk-or-[A-Za-z0-9_\-]{8,}")


class Doctor:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, message: str) -> None:
        print(f"OK: {message}")

    def fail(self, message: str) -> None:
        print(f"FAIL: {message}")
        self.failures.append(message)

    def warn(self, message: str) -> None:
        print(f"WARN: {message}")

    def expect(self, condition: bool, ok_message: str, fail_message: str) -> bool:
        if condition:
            self.ok(ok_message)
        else:
            self.fail(fail_message)
        return condition


def authenticated_json(url: str, key: str, timeout: float = 30.0) -> dict:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("OpenRouter APIがobject以外を返しました")
    return payload


def request_model(key: str, model: str, effort: str | None, tags: list[str]):
    """ZDR強制のcanary。実providerを確かめるため生成IDも返す。"""
    provider: dict[str, object] = {"zdr": True}
    if tags:
        provider["order"] = tags
        provider["allow_fallbacks"] = False
    body: dict[str, object] = {
        "model": model,
        "input": "Return exactly OK.",
        "max_output_tokens": 64,
        "provider": provider,
    }
    if effort is not None:
        body["reasoning"] = {"effort": effort}
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-OpenRouter-Metadata": "enabled",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, json.load(response), response.headers.get("X-Generation-Id")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read()), error.headers.get("X-Generation-Id")
        except json.JSONDecodeError:
            return error.code, {}, error.headers.get("X-Generation-Id")


def generation_metadata(key: str, generation_id: str) -> dict:
    url = "https://openrouter.ai/api/v1/generation?id=" + urllib.parse.quote(generation_id)
    last: dict = {}
    for attempt in range(10):
        try:
            data = authenticated_json(url, key).get("data", {})
            if isinstance(data, dict) and data.get("provider_name"):
                return data
            last = data if isinstance(data, dict) else {}
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        if attempt < 9:
            time.sleep(2)
    return last


# --- 検査 -----------------------------------------------------------------


def check_stock(doctor: Doctor, paths: UserPaths) -> None:
    if not doctor.expect(
        paths.stock_app.is_dir(),
        f"公式ChatGPT.appがあります: {paths.stock_app}",
        f"公式ChatGPT.appがありません: {paths.stock_app}",
    ):
        return
    signed = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(paths.stock_app)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    doctor.expect(signed, "公式ChatGPT.appの署名は有効です", "公式ChatGPT.appの署名検証に失敗しました")
    # 案Dは純正appへ書き込まない。念のため痕跡が無いことを見る。
    marker = subprocess.run(
        ["/usr/bin/grep", "-aFq", "__codexOpenRouter",
         str(paths.stock_app / "Contents/Resources/app.asar")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode
    doctor.expect(
        marker != 0,
        "公式ASARにpatch markerはありません（無改変）",
        "公式ASARにpatch markerがあります。純正appが改変されています",
    )


def check_config(doctor: Doctor, paths: UserPaths, registry_models: dict) -> None:
    config = paths.shared_config
    if not doctor.expect(
        config.is_file(), f"共有configがあります: {config}", f"共有configがありません: {config}"
    ):
        return
    text = config.read_text(encoding="utf-8")

    # B block は永続。消すとOpenRouter記録threadのresumeがハードエラーになる。
    doctor.expect(
        configblock.has_block(text, PROVIDER_BLOCK),
        "provider blockがあります（OpenRouter記録threadのresume保護）",
        "provider blockがありません。OpenRouterで記録したthreadのresumeが壊れます",
    )
    model = configblock.read_top_level(text, "model")
    provider = configblock.read_top_level(text, "model_provider")
    if configblock.has_block(text, CATALOG_BLOCK):
        doctor.ok("catalog blockがあります（ランチャー実行中）")
    else:
        doctor.ok("catalog blockはありません（純正起動はvanilla）")
        doctor.expect(
            model not in registry_models,
            "非稼働時のmodelはnativeです",
            f"catalogが無いのにmodelがOpenRouter slugです: {model}",
        )
    expected = "openrouter" if model in registry_models else "openai"
    doctor.expect(
        provider in (None, expected),
        f"model_providerはmodelと整合します: model={model} provider={provider}",
        f"model_providerがmodelと矛盾します: model={model} provider={provider} 期待={expected}",
    )
    doctor.expect(
        not KEY_PATTERN.search(text),
        "configにOpenRouter keyはありません",
        "configにOpenRouter keyが書かれています",
    )


def check_catalog(doctor: Doctor, paths: UserPaths, registry_models: dict) -> None:
    path = paths.composite_catalog
    if not path.is_file():
        doctor.warn(f"compositeカタログはまだありません（初回起動時に生成されます）: {path}")
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        catalog_module.validate(document, registry_models)
    except (json.JSONDecodeError, catalog_module.CatalogError) as exc:
        doctor.fail(f"compositeカタログが契約を満たしません: {exc}")
        return
    listed = [m for m in document["models"] if m.get("visibility") == "list"]
    doctor.ok(f"compositeカタログは契約を満たします（picker表示 {len(listed)}件）")


def check_guard(doctor: Doctor, paths: UserPaths, port: int = DEFAULT_PORT) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    listening = probe.connect_ex(("127.0.0.1", port)) == 0
    probe.close()
    if not listening:
        doctor.ok(f"guardは停止中でport {port} は空いています")
        return
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/__guard/health", timeout=2
        ) as response:
            document = json.load(response)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        doctor.fail(f"port {port} を別のプロセスが使用しています。guardを起動できません")
        return
    doctor.expect(
        document.get("ok") is True,
        f"guardがport {port} で応答しています",
        f"port {port} の応答がguardのものではありません",
    )


def check_secret_scan(doctor: Doctor, paths: UserPaths) -> None:
    leaked: list[str] = []
    for path in (paths.shared_config, paths.guard_log, paths.composite_catalog):
        if path.is_file() and KEY_PATTERN.search(path.read_text(encoding="utf-8", errors="replace")):
            leaked.append(str(path))
    doctor.expect(not leaked, "鍵はconfig・catalog・guard logに残っていません",
                  f"鍵が残っています: {leaked}")
    table = subprocess.run(
        ["/bin/ps", "-axo", "command="], text=True, stdout=subprocess.PIPE
    ).stdout
    doctor.expect(
        not KEY_PATTERN.search(table),
        "鍵はprocess argumentsにありません",
        "鍵がprocess argumentsに露出しています",
    )
    doctor.expect(
        "OPENROUTER_API_KEY" not in os.environ,
        "OPENROUTER_API_KEYは環境にありません",
        "OPENROUTER_API_KEYが環境に設定されています（Keychain経由のみにしてください）",
    )


def check_network(doctor: Doctor, paths: UserPaths, registry_models: dict) -> None:
    try:
        key = CredentialStore(paths.credential_helper).get()
    except Exception as exc:  # noqa: BLE001
        doctor.fail(f"KeychainからOpenRouter keyを取得できません: {exc}")
        return

    expected = set(registry_models)
    try:
        available = authenticated_json("https://openrouter.ai/api/v1/models/user", key).get(
            "data", []
        )
        zdr_data = authenticated_json("https://openrouter.ai/api/v1/endpoints/zdr", key).get(
            "data", []
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        doctor.fail(f"OpenRouter APIへ到達できません: {exc}")
        return

    concrete = {
        item.get("id")
        for item in available
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and not item["id"].startswith(("openrouter/", "~"))
    }
    doctor.expect(
        concrete == expected,
        "OpenRouter keyの実効model集合はregistryと完全一致します",
        f"OpenRouter keyの実効model集合が不正です: {sorted(concrete)}",
    )

    for model in expected:
        endpoints = [
            e
            for e in zdr_data
            if isinstance(e, dict)
            and e.get("model_id") == model
            and e.get("status") == 0
            and isinstance(e.get("tag"), str)
        ]
        if not endpoints:
            doctor.fail(f"稼働中のZDR endpointがありません: {model}")
            continue
        tags = sorted({e["tag"] for e in endpoints})
        providers = {e.get("provider_name") for e in endpoints}
        spec = registry_models[model]
        effort = spec.get("default_effort")
        status, body, generation_id = request_model(key, model, effort, tags)
        if status != 200:
            doctor.fail(f"{model} のZDR canaryが失敗しました: HTTP {status}")
            continue
        actual = body.get("model")
        if actual not in (model, spec.get("canonical_slug")):
            doctor.fail(f"{model} の応答modelが一致しません: {actual}")
            continue
        metadata = generation_metadata(key, generation_id) if generation_id else {}
        provider_name = metadata.get("provider_name")
        doctor.expect(
            provider_name in providers,
            f"{model} は稼働中ZDR providerで応答しました: {provider_name}",
            f"{model} の実providerがZDR集合外です: {provider_name}",
        )


def run(
    paths: UserPaths,
    registry_path: Path,
    *,
    network: bool = False,
    runtime: bool = False,
    secret_scan: bool = False,
) -> int:
    registry_models = json.loads(registry_path.read_text(encoding="utf-8"))["models"]
    doctor = Doctor()
    check_stock(doctor, paths)
    check_config(doctor, paths, registry_models)
    check_catalog(doctor, paths, registry_models)
    if runtime:
        check_guard(doctor, paths)
    if secret_scan:
        check_secret_scan(doctor, paths)
    if network:
        print("INFO: network canaryは少量のOpenRouter API利用料が発生する場合があります。")
        check_network(doctor, paths, registry_models)
    if doctor.failures:
        print(f"RESULT: FAIL ({len(doctor.failures)}件)")
        return 1
    print("RESULT: PASS")
    return 0
