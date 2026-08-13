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
import tomllib
import urllib.error
import urllib.parse
import urllib.request

from . import catalog as catalog_module
from . import configblock
from .app import UserPaths
from .auth import CredentialStore
from .profile import active_registry, installed_profile
from .supervisor import CATALOG_BLOCK, PROVIDER_BLOCK, State

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


def check_config(
    doctor: Doctor,
    paths: UserPaths,
    registry_models: dict,
    all_registry_models: set[str],
) -> None:
    config = paths.shared_config
    if not doctor.expect(
        config.is_file(), f"共有configがあります: {config}", f"共有configがありません: {config}"
    ):
        return
    text = config.read_text(encoding="utf-8")
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        doctor.fail(f"共有configが不正なTOMLです: {exc}")
        return

    # B block は永続。消すとOpenRouter記録threadのresumeがハードエラーになる。
    doctor.expect(
        configblock.has_block(text, PROVIDER_BLOCK),
        "provider blockがあります（OpenRouter記録threadのresume保護）",
        "provider blockがありません。OpenRouterで記録したthreadのresumeが壊れます",
    )
    model = configblock.read_top_level(text, "model")
    provider = configblock.read_top_level(text, "model_provider")
    active = configblock.has_block(text, CATALOG_BLOCK)
    state = State.load(paths.supervisor_state)
    provider_root = document.get("model_providers")
    provider_table = (
        provider_root.get("openrouter") if isinstance(provider_root, dict) else None
    )
    if not isinstance(provider_table, dict):
        doctor.fail("model_providers.openrouterをTOMLとして解釈できません")
        return
    base_url = provider_table.get("base_url")
    auth = provider_table.get("auth")
    if not isinstance(auth, dict):
        doctor.fail("model_providers.openrouter.authがありません")
        return

    if active:
        doctor.ok("catalog blockがあります（ランチャー実行中）")
        expected_url = (
            f"http://127.0.0.1:{state.guard_port}/v1" if state.guard_port else None
        )
        doctor.expect(
            state.active and state.guard_port not in (None, 0),
            f"supervisor stateはactiveです（port {state.guard_port}）",
            "catalogがあるのにsupervisor stateがactiveではありません",
        )
        doctor.expect(
            base_url == expected_url,
            "active providerは実行中guardのephemeral portを指しています",
            f"active providerとguard portが一致しません: {base_url} != {expected_url}",
        )
        doctor.expect(
            auth.get("command") == "/bin/cat" and auth.get("args") == [str(paths.guard_token)],
            "active providerは起動ごとのローカルtokenで認証します",
            "active providerがローカルtoken認証ではありません",
        )
        doctor.expect(
            paths.guard_token.is_file()
            and paths.guard_token.stat().st_mode & 0o777 == 0o600,
            "guard tokenは0600で存在します",
            "activeなのにguard tokenが無いかmodeが0600ではありません",
        )
    else:
        doctor.ok("catalog blockはありません（純正起動はvanilla）")
        doctor.expect(
            model not in all_registry_models,
            "非稼働時のmodelはnativeです",
            f"catalogが無いのにmodelがOpenRouter slugです: {model}",
        )
        doctor.expect(
            not state.active and base_url == "http://127.0.0.1:0/v1",
            "inactive providerは非接続stubです",
            f"inactive providerが非接続stubではありません: {base_url}",
        )
        doctor.expect(
            auth.get("command") == "/usr/bin/false" and auth.get("args") == [],
            "inactive providerの認証は必ず失敗します",
            "inactive providerの認証がfail-closedではありません",
        )
        doctor.expect(
            not paths.guard_token.exists(),
            "inactive時にguard tokenは残っていません",
            f"inactive時にguard tokenが残っています: {paths.guard_token}",
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
    doctor.expect(
        str(paths.credential_helper) not in text,
        "provider認証は実API key helperを参照しません",
        "provider認証が実API key helperを参照しています",
    )


def check_manifest(doctor: Doctor, paths: UserPaths, profile) -> None:
    """install-manifestが導入済みprofileと同じ集合を指していること。

    設定画面のapplyはprofile・state・manifestを1つのtransactionで置き換える。
    ここがずれているのは、その外で誰かが片方だけ書き換えた合図。
    """
    receipt = paths.install_manifest
    if not receipt.is_file() or receipt.is_symlink():
        doctor.warn(f"install-manifestがありません: {receipt}")
        return
    try:
        document = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        doctor.fail(f"install-manifestを読み込めません: {exc}")
        return
    recorded = document.get("profile_digest") if isinstance(document, dict) else None
    if recorded is None:
        doctor.warn("install-manifestにprofile digestがありません。upgradeで記録されます")
        return
    doctor.expect(
        recorded == profile.digest,
        "install-manifestのprofile digestは導入済みprofileと一致します",
        "install-manifestと導入済みprofileが一致しません",
    )


def check_catalog(
    doctor: Doctor,
    paths: UserPaths,
    registry_models: dict,
    all_registry_models: set[str],
) -> None:
    path = paths.composite_catalog
    if not path.is_file():
        doctor.warn(
            "compositeカタログはまだありません"
            f"（初回起動時、またはモデル設定の変更後に再生成されます）: {path}"
        )
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        catalog_module.validate(document, registry_models, all_registry_models)
    except (json.JSONDecodeError, catalog_module.CatalogError) as exc:
        doctor.fail(f"compositeカタログが契約を満たしません: {exc}")
        return
    listed = [m for m in document["models"] if m.get("visibility") == "list"]
    doctor.ok(f"compositeカタログは契約を満たします（picker表示 {len(listed)}件）")


def check_guard(doctor: Doctor, paths: UserPaths) -> None:
    state = State.load(paths.supervisor_state)
    if not state.active:
        doctor.ok("supervisorはinactiveです（guard検査対象なし）")
        return
    if (
        not isinstance(state.guard_port, int)
        or not 1 <= state.guard_port <= 65535
        or not isinstance(state.guard_nonce, str)
        or not state.guard_nonce
    ):
        doctor.fail("active stateにguard port/nonceがありません")
        return
    port = state.guard_port
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    listening = probe.connect_ex(("127.0.0.1", port)) == 0
    probe.close()
    if not listening:
        doctor.fail(f"active stateなのにguardがport {port}で応答していません")
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
        document.get("ok") is True and document.get("nonce") == state.guard_nonce,
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
    # doctor自身の環境ではなく「起動されるappへ渡らないこと」を見る。
    # 利用者が他ツール用に OPENROUTER_API_KEY をexportしているのは正当なので、
    # それ自体をfailにすると導入できなくなる。
    launcher = paths.bin_dir / "codex-openrouter-app"
    if launcher.is_file():
        doctor.expect(
            "unset OPENROUTER_API_KEY" in launcher.read_text(encoding="utf-8"),
            "ランチャーは起動前にOPENROUTER_API_KEYを外します",
            "ランチャーがOPENROUTER_API_KEYを外していません。appへ鍵が渡ります",
        )
    if "OPENROUTER_API_KEY" in os.environ:
        doctor.warn(
            "shellがOPENROUTER_API_KEYをexportしています。"
            "ランチャーとsupervisorが起動前に外すのでappへは渡りません"
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
    # Guardrailは任意になったので、実効集合との完全一致は求めない。求めるのは
    # 「選択中のmodelが全て呼べる」方向だけ。
    missing = sorted(expected - concrete)
    doctor.expect(
        not missing,
        "選択中のmodelはすべてOpenRouter keyで呼び出せます",
        f"OpenRouter keyで呼び出せないmodelがあります: {missing}",
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
        spec = registry_models[model]
        if not spec.get("zdr_supported", True):
            # 利用者が明示的に選んだ非ZDR model。落とさずに、毎回それと分かるようにする。
            doctor.warn(
                f"{model} はZDRなしで動作します。"
                "送信内容がproviderに保持される可能性があります"
            )
            continue
        if not endpoints:
            doctor.fail(f"稼働中のZDR endpointがありません: {model}")
            continue
        tags = sorted({e["tag"] for e in endpoints})
        providers = {e.get("provider_name") for e in endpoints}
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
    # supervisorと同じ正本を見る。同梱registryのまま読むと、設定画面で足した
    # modelを「OpenRouterのslug」と認識できず、純正起動時にcatalogに無いmodelを
    # 指したままでもPASSしてしまう。
    registry = json.loads(
        active_registry(registry_path, paths).read_text(encoding="utf-8")
    )["models"]
    _profile_path, profile = installed_profile(registry_path, paths)
    registry_models = profile.registry
    doctor = Doctor()
    check_stock(doctor, paths)
    state = State.load(paths.supervisor_state)
    digest_matches = state.profile_digest == profile.digest
    legacy_without_installed_profile = (
        not paths.installed_profile.exists() and state.profile_digest is None
    )
    doctor.expect(
        digest_matches or legacy_without_installed_profile,
        f"導入済みprofile digestは一致します: {profile.name}",
        "supervisor stateと導入済みprofileが一致しません",
    )
    check_manifest(doctor, paths, profile)
    check_config(doctor, paths, registry_models, set(registry))
    check_catalog(doctor, paths, registry_models, set(registry))
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
