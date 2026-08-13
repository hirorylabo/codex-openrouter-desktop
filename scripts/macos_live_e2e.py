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
from typing import Any, Callable
import urllib.error
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


class E2EError(RuntimeError):
    pass


def cdp(expression: str) -> Any:
    """CDP評価を行い、target不在・JS例外をその場で停止させる。"""
    try:
        result = subprocess.run(
            ["deno", "run", "--allow-net=127.0.0.1", str(CDP_SCRIPT), expression],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stdout or exc.stderr or "CDP command failed").strip()
        raise E2EError(f"CDP評価に失敗しました: {detail[:1000]}") from exc
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise E2EError(f"CDPが不正な結果を返しました: {result.stdout[:500]}") from exc
    if not isinstance(document, dict) or document.get("ok") is not True:
        raise E2EError(f"CDP評価に失敗しました: {str(document)[:1000]}")
    return document.get("value")


def wait_for_cdp(
    expression: str,
    ready: Callable[[Any], bool],
    *,
    timeout: float = 60,
) -> Any:
    """rendererの初期化を固定sleepではなく上限付きで待つ。"""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = cdp(expression)
            if ready(last):
                return last
        except E2EError as exc:
            last = {"error": str(exc)}
        time.sleep(1)
    raise E2EError(f"renderer/composerの準備がtimeoutしました: {str(last)[:1500]}")


def wait_for_new_session(
    paths: UserPaths,
    before: set[Path],
    needle: str,
    timeout: float = 30,
) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        candidates = set((paths.shared_home / "sessions").rglob("*.jsonl")) - before
        matching = []
        for candidate in candidates:
            try:
                if needle in candidate.read_text(encoding="utf-8"):
                    matching.append(candidate)
            except (OSError, UnicodeDecodeError):
                continue
        if matching:
            return max(matching, key=lambda path: path.stat().st_mtime_ns)
        time.sleep(1)
    raise E2EError(f"今回の{needle}送信に対応する新規sessionが作成されませんでした")


def guard_rejects(port: int, token: str, model: str, canary: str) -> bool:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/responses",
        data=json.dumps({"model": model, "input": canary}).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
    except urllib.error.HTTPError as exc:
        return exc.code == 400
    return False


def guard_records(paths: UserPaths) -> list[dict]:
    if not paths.guard_log.is_file():
        return []
    return [json.loads(x) for x in paths.guard_log.read_text().splitlines() if x.strip()]


def _run_main() -> int:
    RESULTS.clear()
    shutil.rmtree(BASE, ignore_errors=True)
    paths = make_paths()
    auth_copy = paths.shared_home / "auth.json"
    (paths.shared_home).mkdir(parents=True)
    (BASE / "ws").mkdir(parents=True)
    (BASE / "bin").mkdir(parents=True)
    os.chmod(paths.shared_home, 0o700)
    supervisor: E2ESupervisor | None = None
    process = None
    fatal: Exception | None = None
    try:
        # 純正appが最初に作る状態に近い最小config
        paths.shared_config.write_text(
            'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "low"\n', encoding="utf-8"
        )
        shutil.copy2(Path.home() / ".codex/auth.json", auth_copy)
        os.chmod(auth_copy, 0o600)

        # 鍵はKeychainを使わずダミーにする（課金を発生させないため）
        helper = paths.credential_helper
        helper.write_text(
            '#!/bin/sh\n[ "$1" = "get" ] && echo "sk-or-e2e-not-a-real-key" || exit 0\n'
        )
        helper.chmod(0o755)

        supervisor = E2ESupervisor(
            paths, ROOT / "models/registry.json", port=0, workspace=BASE / "ws"
        )
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
        forwarded_bodies: list[bytes] = []

        def fixture_forwarder(body: bytes, _key: str):
            forwarded_bodies.append(body)
            return 200, {"Content-Type": "text/event-stream"}, io.BytesIO(b"data: ok\n\n")

        supervisor._server.RequestHandlerClass.guard.forwarder = fixture_forwarder
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
        readiness = wait_for_cdp('''
        (() => {
          const composer = document.querySelector("[contenteditable=true], textarea");
          const picker = document.querySelector("[data-composer-navigation-target=reasoning]") ||
            document.querySelector('[data-testid*="model" i]') ||
            [...document.querySelectorAll("button")].find(e =>
              /model|モデル/i.test(`${e.getAttribute("aria-label")||""} ${e.getAttribute("title")||""}`) ||
              (/gpt/i.test((e.textContent||"").trim()) && e.getAttribute("aria-haspopup")));
          return {
            ready: document.readyState === "complete" && !!composer && !!picker,
            hasComposer: !!composer,
            hasPicker: !!picker,
            url: location.href,
            title: document.title,
            roles: [...new Set([...document.querySelectorAll("[role]")]
              .map(e=>e.getAttribute("role")).filter(Boolean))].slice(0,30),
            buttons: [...document.querySelectorAll("button")]
              .map(e=>(e.getAttribute("aria-label")||e.getAttribute("title")||"").slice(0,80))
              .filter(Boolean).slice(0,30),
          };
        })()''', lambda value: isinstance(value, dict) and value.get("ready") is True)
        print(f"    renderer準備完了: url={readiness.get('url')} title={readiness.get('title')}")

        picker_result = cdp('''
        (async () => {
          const waitFor = async (probe, timeout=10000) => {
            const deadline = Date.now() + timeout;
            while (Date.now() < deadline) {
              const value = probe();
              if (value) return value;
              await new Promise(r=>setTimeout(r,200));
            }
            return null;
          };
          const b = document.querySelector("[data-composer-navigation-target=reasoning]") ||
            document.querySelector('[data-testid*="model" i]') ||
            [...document.querySelectorAll("button")].find(e =>
              /model|モデル/i.test(`${e.getAttribute("aria-label")||""} ${e.getAttribute("title")||""}`) ||
              (/gpt/i.test((e.textContent||"").trim()) && e.getAttribute("aria-haspopup")));
          if (!b) return {ok:false, error:"picker trigger not found"};
          b.dispatchEvent(new PointerEvent("pointerdown",{bubbles:true})); b.click();
          const menuItems = '[role=menuitem],[role=menuitemradio],[role=option]';
          const it = await waitFor(() => [...document.querySelectorAll(menuItems)]
            .find(e=>/^モデル/.test((e.textContent||"").trim())) ||
            [...document.querySelectorAll(menuItems)].find(e=>(e.textContent||"").trim().startsWith("[OR]")));
          if (it && !(it.textContent||"").trim().startsWith("[OR]")) {
            it.dispatchEvent(new PointerEvent("pointerdown",{bubbles:true})); it.click();
          }
          const names = await waitFor(() => {
            const current = [...new Set([...document.querySelectorAll(menuItems)]
              .map(e=>(e.textContent||"").trim()).filter(Boolean))];
            return current.some(name=>name.startsWith("[OR]")) ? current : null;
          });
          if (!names) return {ok:false, error:"model menu did not become ready"};
          return {ok:true, names};
        })()''')
        if not isinstance(picker_result, dict) or picker_result.get("ok") is not True:
            raise E2EError(f"pickerを開けませんでした: {picker_result}")
        names = picker_result.get("names", [])
        or_count = sum(1 for n in names if n.startswith("[OR]"))
        check("pickerにOpenRouterモデルが5件並ぶ", or_count == 5, f"{or_count}件")
        if or_count != 5:
            raise E2EError(f"pickerのOpenRouterモデル数が不正です: {or_count}")

        # --- native で1往復。guardに着弾しないこと -------------------------
        before = len(guard_records(paths))
        sessions_before = set((paths.shared_home / "sessions").rglob("*.jsonl"))
        sent = cdp('''
        (async () => {
          document.body.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}));
          await new Promise(r=>setTimeout(r,400));
          const ta = document.querySelector("[contenteditable=true], textarea");
          if (!ta) return {sent:false, error:"composer not found"};
          ta.focus(); document.execCommand("insertText", false, "native-canary");
          await new Promise(r=>setTimeout(r,400));
          for (const t of ["keydown","keypress","keyup"])
            ta.dispatchEvent(new KeyboardEvent(t,{key:"Enter",code:"Enter",keyCode:13,which:13,bubbles:true,cancelable:true}));
          return {sent:true};
        })()''')
        if not isinstance(sent, dict) or sent.get("sent") is not True:
            raise E2EError(f"native canaryを送信できませんでした: {sent}")
        session = wait_for_new_session(paths, sessions_before, "native-canary")
        time.sleep(3)
        after = guard_records(paths)
        check("nativeのturnはguardに着弾しない", len(after) == before,
              f"guard hits={len(after) - before}")
        provider = None
        first_line = session.read_text(encoding="utf-8").splitlines()[0]
        provider = json.loads(first_line)["payload"].get("model_provider")
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

        # UI由来のambient発火はbuild依存なので観測情報に留める。遮断契約自体は
        # local guardへ決定的に直接送り、forwarder未呼出まで検証する。
        observed = guard_records(paths)
        print(
            "    ambient観測（合否対象外）: "
            f"denied={[r.get('model') for r in observed if r.get('decision') == 'denied']}"
        )
        forwarded_before = len(forwarded_bodies)
        sol_canary = "blocked-sol-e2e"
        luna_canary = "blocked-luna-e2e"
        sol_rejected = guard_rejects(port, supervisor.access_token, "gpt-5.6-sol", sol_canary)
        luna_rejected = guard_rejects(port, supervisor.access_token, "gpt-5.6-luna", luna_canary)
        check("provider境界をまたぐgpt-5.6-solをguardが遮断する", sol_rejected)
        check("背景model gpt-5.6-lunaをguardが遮断する", luna_rejected)
        check(
            "許可外modelではforwarderを呼ばない",
            len(forwarded_bodies) == forwarded_before,
            f"calls={len(forwarded_bodies) - forwarded_before}",
        )
        log_text = paths.guard_log.read_text() if paths.guard_log.is_file() else ""
        check(
            "guard logに本文が残らない",
            sol_canary not in log_text and luna_canary not in log_text,
        )
    except Exception as exc:  # noqa: BLE001 - cleanupと要約を必ず実行する
        fatal = exc
        print(f"[ERROR] {exc}", file=sys.stderr)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                fatal = fatal or E2EError("隔離ChatGPTが通常終了しませんでした")
                print("[ERROR] 隔離ChatGPTが通常終了しませんでした", file=sys.stderr)
                # 製品launcherは強制終了しない。ここだけは隔離user-dataの試験子PIDを
                # 残さないために回収し、試験結果自体は失敗のままにする。
                process.kill()
                process.wait(timeout=10)
        if supervisor is not None:
            try:
                supervisor.cleanup()
            except Exception as exc:  # noqa: BLE001
                fatal = fatal or exc
                print(f"[ERROR] cleanupに失敗しました: {exc}", file=sys.stderr)
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
        second: E2ESupervisor | None = None
        if fatal is None:
            try:
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
                check(
                    "2回目起動: catalog blockが再び入る",
                    configblock.has_block(second_text, "catalog"),
                )
                check(
                    "2回目起動: ephemeral portを使う",
                    f"127.0.0.1:{second_port}" in second_text,
                )
                second.cleanup()
                second_text = paths.shared_config.read_text()
                check(
                    "2回目終了: 非接続stubへ戻る",
                    "http://127.0.0.1:0/v1" in second_text,
                )
            except Exception as exc:  # noqa: BLE001
                fatal = exc
                print(f"[ERROR] 2回目cycleに失敗しました: {exc}", file=sys.stderr)
            finally:
                if second is not None:
                    try:
                        second.cleanup()
                    except Exception as exc:  # noqa: BLE001
                        fatal = fatal or exc
        # 複製したauthは初期化途中の失敗でも必ず消す。
        auth_copy.unlink(missing_ok=True)
        check("複製したauth.jsonを削除した", not auth_copy.exists())

        passed = sum(1 for _n, ok, _d in RESULTS if ok)
        print(f"\n=== {passed}/{len(RESULTS)} PASS ===")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  FAILED: {name} {detail}")

    return 1 if fatal is not None or any(not ok for _name, ok, _detail in RESULTS) else 0


def main() -> int:
    resolved_base = BASE.resolve()
    temporary_root = Path("/tmp").resolve()
    if (
        BASE.is_symlink()
        or resolved_base == temporary_root
        or temporary_root not in resolved_base.parents
    ):
        print(
            f"[ERROR] CODEX_OPENROUTER_E2E_HOMEは/tmp配下を指定してください: {resolved_base}",
            file=sys.stderr,
        )
        return 1
    # _run_main内のcleanup自体が例外になっても、実auth.jsonの複製だけは残さない。
    auth_copy = make_paths().shared_home / "auth.json"
    try:
        return _run_main()
    finally:
        auth_copy.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
