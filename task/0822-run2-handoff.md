# 0822 Run 2 ハンドオフ: 自作実装へ戻したが gate 1 で止まっている

作成日: 2026-08-22 / branch: `codex/openrouter-tool-bridge` @ `efe3957`（push 済み）

Status: **中断。環境は自作実装へ切り替え済みで稼働中。実機 gate 1 が `tool_bridge_error` で落ちる。**

---

## TL;DR

- codex-router trial は**撤去済み**。自作実装を promote して `launch` 済み。日常環境は自作実装に戻っている
- PR #24 は更新済みで **CI 11/11 pass / `mergeStateStatus: CLEAN`**（draft のまま）
- **実機 gate 1 が落ちる。** `codex exec` が送る tool wire を `prepare_document()` が拒否する
  （guard.log に `bridge-denied` / 268,080 bytes）
- 原因は**ほぼ特定できている**が、実 payload の捕獲は未了（次セッションの最初の一手）

---

## 1. 完了していること

### PR #24（`codex/openrouter-tool-bridge`）

| commit | 内容 |
| --- | --- |
| `92ef7ca` | merge: main の trial 記録を取り込む |
| `9333b69` | docs: 承認済み plan を `task/0822-toolbridge-fix-plan.md` へ保存 |
| `9e35ff3` | **fix: tool wire を実測で通る契約へ直し、lite 形式でも Bridge を起動させる** |
| `efe3957` | docs: CLI 動作確認の記録 |

CI は 11/11 pass。PR 本文も現状に合わせて更新済み。**draft のまま**（ready にはしていない）。

実装の中身と根拠は [`0822-toolbridge-fix-plan.md`](./0822-toolbridge-fix-plan.md)、
trial 側の実測は [`0822-codex-router-trial.md`](./0822-codex-router-trial.md) の追記3 にある。

### 検証で通っているもの

| 検証 | 結果 |
| --- | --- |
| unit tests | 377 OK |
| ruff 0.16.3 | pass |
| `./codex-openrouter check` | PASS / `tool_wire=compatible contract=3` |
| `doctor --secret-scan` | PASS / 「build 6849 は tool contract **3** で確認済み」 |
| wire（実装が出す tool 定義を直接 OpenRouter へ 4回） | **4/4**（旧実装の形は 0/4） |
| `models verify-tools`（実 API canary） | **`verified`**（4回中3回。外れ1回は structured canary の provider 抽選） |
| 生成 catalog | `deepseek/…` が `use_responses_lite=False` / `tool_mode=direct` / `apply_patch=freeform` |

### 環境の切り替え

1. `~/.local/share/codex-router/bin/disable` 実行済み。`~/.codex/config.toml` に codex-router の痕跡ゼロ、LaunchAgent も撤去
2. `./codex-openrouter upgrade` で promote 済み（`UPGRADE: PASS v0.2.1`）。installed も contract 3
3. `./codex-openrouter launch /private/tmp/codex-openrouter-e2e.R2` を**background で起動したまま**

---

## 2. いま止まっている場所

```
$ codex exec -m deepseek/deepseek-v4-flash-0731 "Run pwd and report the output. Nothing else."
ERROR: {"error":{"code":"tool_bridge_error","message":"unsupported Codex tool wire"}}
```

guard.log:

```json
{"model": "deepseek/deepseek-v4-flash-0731", "decision": "bridge-denied", "bytes": 268080, "t": 1787390930.373}
```

`guard.py:216-225` が `toolbridge.ToolBridgeError` を捕まえて 400 を返している。
**具体的な理由は client へ漏らさない設計**なので、エラー文字列からは特定できない。

### 有力な原因（未確定）

`codex` バイナリ（0.148.0）に含まれる tool type 文字列を調べた結果:

```
function / local_shell / namespace / web_search
```

`toolbridge.prepare_document()` の `add()` は `kind not in {"function","custom"}` で
`ToolBridgeError("未対応のCodex tool型です")` を投げる。よって **`local_shell` か `web_search` を
受け取って fail-closed している可能性が高い。**

ただし生成 catalog は次のとおりで、素直には出ないはずの値になっている:

| フィールド | routed (`deepseek/…`) | native (`gpt-5.6-sol`) |
| --- | --- | --- |
| `shell_type` | `shell_command` | `shell_command` |
| `supports_search_tool` | **`False`** | `True` |
| `web_search_tool_type` | **`text_and_image`**（中和対象外） | `text_and_image` |
| `tool_mode` | `direct` | `code_mode_only` |
| `experimental_supported_tools` | `[]` | — |

`web_search_tool_type` は `NATIVE_ONLY_FIELDS` にも `DIRECT_TOOL_FIELDS` にも入っておらず、
純正テンプレートの値をそのまま継いでいる。**`use_responses_lite` と同じ型の見落とし**の可能性がある。

**重要**: これは app 経由でも起きるとは限らない。app と CLI は送る tool 集合が違う
（app は 191 tool、CLI はもっと少ない）。**app 側は未確認。**

---

## 3. 次にやること（この順で）

### 3.1 実 payload を捕獲する（最優先・課金なし）

エラー文字列では特定できないので、codex が実際に送る body を捕まえる。
捕獲スクリプトは書いてある: `~/…/scratchpad/capture-upstream.py`（127.0.0.1:4399 で待ち受け、
1件受けたら書き出して終了）。**ただし前回は最後まで実行できていない。**

躓いた点を2つ記録しておく:

- `codex exec` は **stdin を待って固まる**（"Reading additional input from stdin..."）。`< /dev/null` が要る
- `codex exec` は **信頼済み git repo の中でしか動かない**（`Not inside a trusted directory`）。
  `/private/tmp/codex-openrouter-e2e.R2` の中から実行するか `--skip-git-repo-check` を付ける

```bash
cd /private/tmp/codex-openrouter-e2e.R2
python3 <scratchpad>/capture-upstream.py &
codex exec -m deepseek/deepseek-v4-flash-0731 \
  -c 'model_providers.openrouter.base_url="http://127.0.0.1:4399/v1"' \
  "Run pwd." < /dev/null
```

捕獲できたら `tools[]` の `type` を数え、`prepare_document()` に直接食わせて例外文を得る。
**推測で直さない。**

代替案（捕獲がどうしても通らない場合）: `guard.py` の `except ToolBridgeError` で
`str(exc)` を guard.log へ記録する変更を入れる。**client への応答は変えない**こと
（内部情報を漏らさない設計を壊さない）。デバッグ目的なら repo に入れて構わない。

### 3.2 判明した型に応じて設計判断する

`local_shell` / `web_search` を受け取ったときの選択肢は3つ。**どれも一長一短で、判断が要る。**

| 案 | 内容 | 懸念 |
| --- | --- | --- |
| A | catalog で出させない（`web_search_tool_type` を中和、`shell_type` を見直す） | 根本的。ただし Codex 側がフィールドを見て出すとは限らない |
| B | bridge で**落として** ToolMap に載せない | model がその tool を呼べなくなるだけで安全。ただし「送られたのに黙って消す」は現行の fail-closed 方針と衝突する |
| C | 素通しする | OpenRouter が理解しない型を投げることになる。**trial の実測では `type:"custom"` は status 200 で黙って捨てられた**ので、素通しは「動いたように見えて動かない」最悪の形になり得る |

現時点の傾向は **A を第一候補、B を保険**。C は trial の実測から採らない。

### 3.3 gate 1 / gate 2 をやり直す

workspace は `/private/tmp/codex-openrouter-e2e.R2`（git 初期化済み、`target.py` に `greet()` のみ、
baseline commit `f39f432`）。

- gate 1: `codex exec … "Run pwd and report the output. Nothing else."` → `exec_command` が通ること
- gate 2: `codex exec … "target.py に farewell(name) を追加して。"` → `apply_patch` が着弾すること

**gate の試行プロンプトは補足なしの一文にする。** trial で、こちらの報告文が混ざったターンは
モデルが読み取りだけで終わり 17リクエスト/15分かかった（クリーンな試行は 8/31秒）。

**headless で回す。** GUI 自動操作はユーザー作業と干渉するため使わない
（`~/.local/share/codex-openrouter-trial/gui/app.sh` は動くが運用しない方針）。

---

## 4. 現在の環境状態（そのまま引き継ぐ場合）

| 項目 | 状態 |
| --- | --- |
| supervisor | **`active: true`**、guard 稼働中、build 6849 |
| `launch` プロセス | **background で生存中**（pid 81646）。`/private/tmp/codex-openrouter-e2e.R2` を workspace に起動 |
| ChatGPT.app | **起動中**（UI 操作はしていない） |
| `~/.codex/config.toml` | 自作実装の marker block 2つ（catalog / provider）。codex-router の痕跡なし |
| installed runtime | contract 3（promote 済み） |
| rollback backup | `~/.local/share/codex-openrouter-desktop/state/upgrade-backups/20260822-182054-v0.2.1` |

### 止めたいとき

`launch` は `finally` で必ず cleanup する設計なので、**プロセスを終了させれば config も戻る**。

```bash
kill 81646        # または ChatGPT.app を終了する
```

その後 `~/.codex/config.toml` から marker block が消え、`supervisor.json` の `active` が false になる。

### codex-router trial へ戻したいとき

checkout は残っている（`~/.local/share/codex-router` @ `b01cf559`、patch marker 2件も生存）。
退避 config は `~/.local/share/codex-openrouter-trial/2026-08-22/config.toml.before-switchback`。
手順は [`0822-codex-router-trial.md`](./0822-codex-router-trial.md) の「復帰手順」。

**注意**: 併存はできない。戻すなら先に `launch` を止め、marker block を消してから
codex-router を enable すること。

---

## 5. 引き継ぎメモ

- **予測を書かない。** 実測していない行は「未実施」のまま置く。これは 0820 から一貫している方針
- **canary は1回で判定しない。** structured canary は provider 抽選で外れる（実測 3/4）。
  外れると freeform を一度も試さずに `unsupported` と出る（`toolcompat.py:333-337` の短絡）。
  この弱点は `0822-toolbridge-fix-plan.md` に記録済みで、直していない
- **`doctor` サブコマンドは installed の doctor バイナリを exec する**（`cli.py:517`）。
  repo の変更を見たいときは `./codex-openrouter check` を使う
- **`bin/start` は foreground 実行**（codex-router 側）。管理下の対は `bin/control service stop|start`
- `~/.local/share/codex-openrouter-trial/probes/apply-patch-probe.mjs` は
  `--replay` で保存済みレスポンスを再判定できる。判定バグを再課金なしで直せる
