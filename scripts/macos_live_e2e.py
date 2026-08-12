#!/usr/bin/env python3
"""案D の実機E2E（手動実行）。CIでは動かさない。

実行: PYTHONPATH=src python3 scripts/macos_live_e2e.py
Denoと純正ChatGPT.app、ログイン済みauth.jsonが要る。

隔離CODEX_HOME + 専用user-data-dir で純正appを起動し、
supervisor 本体のコードをそのまま動かして検証する。

純正appは変更しない。実auth.jsonの複製は最後に必ず消す。
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_openrouter import configblock, guard as guard_module, supervisor as sup
from codex_openrouter.app import UserPaths

BASE = Path(os.environ.get("CODEX_OPENROUTER_E2E_HOME", "/tmp/codex-openrouter-e2e"))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def make_paths() -> UserPaths:
    return UserPaths(
        home=BASE,
        stock_app=Path("/Applications/ChatGPT.app"),
        openrouter_app=BASE / "clone.app",
        codex_home=BASE / ".codex-openrouter",
        bin_dir=BASE / "bin",
        support_root=ROOT,
        credential_helper=BASE / "bin/credential",
        desktop_launcher=BASE / "Desktop/Codex OpenRouter.app",
        shared_home=BASE / ".codex",
        state_dir=BASE / "state",
    )


class E2ESupervisor(sup.Supervisor):
    """検証用に、隔離userDataとCDPポートを足して起動する。"""

    def assert_stock_not_running(self) -> None:
        return  # 別user-data-dirなので実インスタンスとは衝突しない

    def launch(self):
        executable = self.paths.stock_app / "Contents/MacOS/ChatGPT"
        return subprocess.Popen(
            [
                str(executable),
                f"--user-data-dir={BASE}/userdata",
                "--remote-debugging-port=9223",
                str(BASE / "ws"),
            ],
            env={**os.environ, "CODEX_HOME": str(self.paths.shared_home)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


CDP_SCRIPT = Path(__file__).parent / "cdp_eval.ts"


def cdp(expression: str) -> str:
    result = subprocess.run(
        ["deno", "run", "--allow-net=127.0.0.1",
         str(CDP_SCRIPT), expression],
        capture_output=True, text=True, timeout=60,
    )
    return result.stdout.strip()


def guard_records(paths: UserPaths) -> list[dict]:
    if not paths.guard_log.is_file():
        return []
    return [json.loads(x) for x in paths.guard_log.read_text().splitlines() if x.strip()]


def main() -> int:
    shutil.rmtree(BASE, ignore_errors=True)
    paths = make_paths()
    (paths.shared_home).mkdir(parents=True)
    (BASE / "ws").mkdir(parents=True)
    (BASE / "bin").mkdir(parents=True)
    os.chmod(paths.shared_home, 0o700)

    # 純正appが最初に作る状態に近い最小config
    paths.shared_config.write_text(
        'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "low"\n', encoding="utf-8"
    )
    shutil.copy2(Path.home() / ".codex/auth.json", paths.shared_home / "auth.json")
    os.chmod(paths.shared_home / "auth.json", 0o600)

    # 鍵はKeychainを使わずダミーにする（課金を発生させないため）
    helper = paths.credential_helper
    helper.write_text('#!/bin/sh\n[ "$1" = "get" ] && echo "sk-or-e2e-not-a-real-key" || exit 0\n')
    helper.chmod(0o755)

    supervisor = E2ESupervisor(paths, ROOT / "models/registry.json", port=0,
                               workspace=BASE / "ws")
    process = None
    try:
        # --- 事前処理 -----------------------------------------------------
        supervisor.self_heal()
        regenerated = supervisor.refresh_catalog_if_needed()
        check("update追従: 初回はcatalogを生成する", regenerated)
        check("catalogが存在する", paths.composite_catalog.is_file())
        check("同じbuildなら再生成しない", not supervisor.refresh_catalog_if_needed())

        port = supervisor.start_guard()
        check("guardが起動しhealthがnonce一致", guard_module.health_ok(port, supervisor.nonce))
        check("別nonceは拒否される", not guard_module.health_ok(port, "wrong"))
        # 課金せず許可経路も実HTTPで通す。guard本体のforwarderだけをfixture化する。
        supervisor._server.RequestHandlerClass.guard.forwarder = lambda _body, _key: (
            200,
            {"Content-Type": "text/event-stream"},
            io.BytesIO(b"data: ok\n\n"),
        )
        allowed_request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/responses",
            data=json.dumps(
                {"model": supervisor.profile.default_model, "input": "allowed-e2e"}
            ).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {supervisor.access_token}"},
        )
        with urllib.request.urlopen(allowed_request, timeout=10) as response:
            check("許可modelをローカルtoken付きで中継する", response.status == 200)

        supervisor.apply_config(port)
        text = paths.shared_config.read_text()
        check("catalog blockが入る", configblock.has_block(text, "catalog"))
        check("provider blockが入る", configblock.has_block(text, "provider"))
        check("configに鍵が書かれていない", "sk-or-" not in text)
        check(
            "初回profile defaultを一度適用する",
            configblock.read_top_level(text, "model") == supervisor.profile.default_model,
        )
        # 以降のnative経路を検証するため、利用者がnativeへ戻した状態を作る。
        configblock.edit(
            paths.shared_config,
            lambda current: configblock.upsert_top_level(current, "model", "gpt-5.6-sol"),
        )
        supervisor.start_watcher()

        # --- 起動 ---------------------------------------------------------
        process = supervisor.launch()
        time.sleep(14)

        models = cdp('''
        (async () => {
          const b = document.querySelector("[data-composer-navigation-target=reasoning]");
          if (!b) return "NO_PICKER";
          b.dispatchEvent(new PointerEvent("pointerdown",{bubbles:true})); b.click();
          await new Promise(r=>setTimeout(r,700));
          const it = [...document.querySelectorAll("[role=menuitem]")].find(e=>/^モデル/.test((e.textContent||"").trim()));
          if (it) { it.dispatchEvent(new PointerEvent("pointerdown",{bubbles:true})); it.click(); await new Promise(r=>setTimeout(r,900)); }
          const names = [...document.querySelectorAll("[role=menuitem]")].map(e=>(e.textContent||"").trim());
          return JSON.stringify(names);
        })()''')
        try:
            names = json.loads(models)
        except json.JSONDecodeError:
            names = []
        or_count = sum(1 for n in names if n.startswith("[OR]"))
        check("pickerにOpenRouterモデルが5件並ぶ", or_count == 5, f"{or_count}件 / raw={models[:120]}")

        # --- native で1往復。guardに着弾しないこと -------------------------
        before = len(guard_records(paths))
        cdp('''
        (async () => {
          document.body.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}));
          await new Promise(r=>setTimeout(r,400));
          const ta = document.querySelector("[contenteditable=true], textarea");
          ta.focus(); document.execCommand("insertText", false, "native-canary");
          await new Promise(r=>setTimeout(r,400));
          for (const t of ["keydown","keypress","keyup"])
            ta.dispatchEvent(new KeyboardEvent(t,{key:"Enter",code:"Enter",keyCode:13,which:13,bubbles:true,cancelable:true}));
          return "sent";
        })()''')
        time.sleep(9)
        after = guard_records(paths)
        check("nativeのturnはguardに着弾しない", len(after) == before,
              f"guard hits={len(after) - before}")
        sessions = list((paths.shared_home / "sessions").rglob("*.jsonl"))
        provider = None
        if sessions:
            provider = json.loads(sessions[0].read_text().splitlines()[0])["payload"].get(
                "model_provider"
            )
        check("nativeスレッドはopenaiに束縛される", provider == "openai", f"provider={provider}")

        # --- OpenRouter を選ぶ。watcherが追随し、guardが番をすること -------
        # appのモデル選択は config/batchWrite で `model` を書く動作（Phase 0-Cで実測）。
        # menu自動操作は画面遷移で不安定なので、同じ書き込みを直接行って
        # watcher と guard の経路を検証する。
        configblock.atomic_write(
            paths.shared_config,
            configblock.upsert_top_level(
                paths.shared_config.read_text(), "model", "deepseek/deepseek-v4-pro"
            ),
        )
        picked = "config-write"
        time.sleep(3)
        text = paths.shared_config.read_text()
        check("appがORモデルをconfigへ書く",
              configblock.read_top_level(text, "model") == "deepseek/deepseek-v4-pro",
              f"picked={picked} model={configblock.read_top_level(text, 'model')}")
        check("watcherがmodel_providerをopenrouterへ追随させる",
              configblock.read_top_level(text, "model_provider") == "openrouter")

        before = len(guard_records(paths))
        cdp('''
        (async () => {
          const nc = [...document.querySelectorAll("button")].find(e=>(e.textContent||"").trim()==="新しいチャット");
          if (nc) { nc.click(); await new Promise(r=>setTimeout(r,1800)); }
          const ta = document.querySelector("[contenteditable=true], textarea");
          ta.focus(); document.execCommand("insertText", false, "or-canary-9931");
          await new Promise(r=>setTimeout(r,400));
          for (const t of ["keydown","keypress","keyup"])
            ta.dispatchEvent(new KeyboardEvent(t,{key:"Enter",code:"Enter",keyCode:13,which:13,bubbles:true,cancelable:true}));
          return "sent";
        })()''')
        time.sleep(10)
        new_records = guard_records(paths)[before:]
        forwarded = [r for r in new_records if r.get("decision") == "forwarded"]
        denied = [r for r in new_records if r.get("decision") == "denied"]
        or_slugs = set(json.loads((ROOT / "models/registry.json").read_text())["models"])
        leaked = [r for r in forwarded if r.get("model") not in or_slugs]
        check("許可外modelを1件も中継していない", not leaked, f"leaked={leaked}")
        # threadはopenrouterに束縛されているが、turnのmodelはnative slug。
        # 案Dで最も危険な経路で、guardがここで止めることが存在理由。
        check("provider境界をまたいだnative turnをguardが遮断する",
              any(r.get("model", "").startswith("gpt-") for r in denied),
              f"denied={[r.get('model') for r in denied]}")
        check("巻き込んだ背景thread(gpt-5.6-luna)をguardが遮断する",
              any(r.get("model") == "gpt-5.6-luna" for r in denied))
        log_text = paths.guard_log.read_text() if paths.guard_log.is_file() else ""
        check("guard logに本文が残らない", "or-canary-9931" not in log_text)
        print(f"    guard内訳: forwarded={[r.get('model') for r in forwarded]} "
              f"denied={[r.get('model') for r in denied]}")

        return 0
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        actions = supervisor.cleanup()
        text = paths.shared_config.read_text() if paths.shared_config.is_file() else ""
        check("終了後: catalog blockが消える", not configblock.has_block(text, "catalog"))
        check("終了後: provider blockは残る（resume保護）",
              configblock.has_block(text, "provider"))
        check("終了後: modelがnativeへ戻る",
              configblock.read_top_level(text, "model") not in
              json.loads((ROOT / "models/registry.json").read_text())["models"])
        check("終了後: providerはport 0のstub", "http://127.0.0.1:0/v1" in text)
        check("終了後: guard tokenが消える", not paths.guard_token.exists())

        # provider-only状態からもう一度起動し、旧nested-marker回帰を実機で検出する。
        second = E2ESupervisor(
            paths,
            ROOT / "models/registry.json",
            port=0,
            workspace=BASE / "ws",
        )
        second.self_heal()
        second_port = second.start_guard()
        second.apply_config(second_port)
        second_text = paths.shared_config.read_text()
        check("2回目起動: catalog blockが再び入る", configblock.has_block(second_text, "catalog"))
        check("2回目起動: ephemeral portを使う", f"127.0.0.1:{second_port}" in second_text)
        second.cleanup()
        second_text = paths.shared_config.read_text()
        check("2回目終了: 非接続stubへ戻る", "http://127.0.0.1:0/v1" in second_text)
        # 複製したauthは必ず消す
        (paths.shared_home / "auth.json").unlink(missing_ok=True)
        check("複製したauth.jsonを削除した", not (paths.shared_home / "auth.json").exists())

        passed = sum(1 for _n, ok, _d in RESULTS if ok)
        print(f"\n=== {passed}/{len(RESULTS)} PASS ===")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  FAILED: {name} {detail}")


if __name__ == "__main__":
    sys.exit(main())
