# 0822 Run 3: gate 1 / gate 2 通過。web_search翻訳を実装して解決

作成日: 2026-08-22 / branch: `codex/openrouter-tool-bridge` @ `befc4d4`（push 済み）

Status: **解決。gate 1 (exec_command)・gate 2 (apply_patch着弾) ともに実測で通過。
PR #24 へ commit 済み、CI 確認待ち。draft のまま。**

---

## TL;DR

- Run 2 の原因は**実 payload 捕獲で確定**: codex 0.148.0 が送る tools[23] 中の
  `{"type":"web_search","external_web_access":false,...}` を `prepare_document()` が
  fail-closed していた。`local_shell` は**来ていなかった**（ハンドオフの推測を訂正）
- **案A(catalog中和)/案B(黙って除去)は採らなかった**:
  - config top-level `web_search = "disabled"` を実測したところ codex は確かに送らなく
    なるが、これはプロバイダ無関係のグローバル設定で **native gpt-5.6-sol の web search
    も死ぬ**ため却下
  - OpenRouter は codex 型 web_search を status 200 で黙って捨てる（trial 実測）ので素通し不可
- **採用: web_search型 → OpenRouter server tool への翻訳**。
  `{"type":"openrouter:web_search"}` を足し、応答側で結果 item を除去する
- commit `befc4d4`: unit 381 OK（+4）、ruff pass、gate 1/2 実機通過

---

## 1. 実測の記録

### 捕獲（scratchpad/capture-upstream.py）

- `codex exec` は stdin 待ちで固まる → `< /dev/null` が要る
- 信頼済み git repo 内でのみ動く → `/private/tmp/codex-openrouter-e2e.R2` から実行
- 捕獲物: `/tmp/captured-toolwire.run1.json`（242,623 bytes, tools 23件）
  - function 12 / namespace 9 / custom 1 (apply_patch) / **web_search 1**
  - namespace は既存実装で対応済みだった（Run 2 ハンドオフの懸念より状況は良かった）
  - `prepare_document()` へ直接食わせ → `ToolBridgeError: 未対応のCodex tool型です: 'web_search'`

### OpenRouter server tool の検証（課金 ~$0.02）

| 形態 | 結果 |
| --- | --- |
| 単独 | ✅ model が自発的に検索 → Python 3.14.7 を url_citation 5件付きで回答 |
| function tool 併用 | ✅ shell 要求→function_call、検索質問→server tool を呼び分け |
| streaming（codex 同形式） | ✅ SSE 正常。`response.output_item.added` に `openrouter:web_search`、annotation、`web_search_requests:1` 課金計上まで確認 |

コスト: 1検索あたり約 $0.007。

### 補足実測

- `--disable web_search` feature flag は効かない（deprecated、デフォルト有効）
- 有効なノブは config.toml top-level `web_search = "live"/"indexed"/"cached"/"disabled"`
  （ただし native も無効化されるので本実装では使わない）
- Keychain の鍵は account `hk.orcarouter`。orcarouter 鍵（sk-orca…）ではなく
  credential helper（~/.local/bin/codex-openrouter-credential）経由で sk-or-v1- 鍵を取得する

---

## 2. 実装内容（commit befc4d4）

`src/codex_openrouter/toolbridge.py`:

- request 側: `prepare_document()` で `{"type":"web_search"}` を除去し、末尾へ
  `{"type":"openrouter:web_search"}` を1件だけ追加（重複定義は1つに潰す）
- response 側: `transform_response_document()` / `SSEBridge._added()` / `_output_done()`
  で `openrouter:web_search` output item を除去
- SSE 進行 event（`response.web_search_call.in_progress/searching/completed`）も除去
  （codex の期待する item lifecycle と異なるため。検索経過は reasoning/message として届く）
- 検索の実体は OpenRouter 側（engine auto = native or Exa）。model には検索能力がある
  状態になるので「送ったのに消えた」不整合がない

tests:

- `WebSearchTranslationTests` 4件追加（request翻訳・重複潰し・response除去・SSE除去）
- 既存2テストの拒否対象を `local_shell` へ変更（web_search はもう拒否されないため）

---

## 3. gate 実行結果

workspace: `/private/tmp/codex-openrouter-e2e.R2`（git 初期化済み、baseline `f39f432`）

| gate | コマンド | 結果 |
| --- | --- | --- |
| gate 1 | `codex exec -m deepseek/deepseek-v4-flash-0731 "Run pwd and report the output. Nothing else."` | ✅ `exec_command` 着弾、`/private/tmp/codex-openrouter-e2e.R2` を報告 |
| gate 2 | `codex exec -m deepseek/deepseek-v4-flash-0731 -s workspace-write "target.py に farewell(name) を追加して。"` | ✅ `apply_patch` 着弾、`farewell(name)` 追加を確認 |

guard.log: `bridge-denied` は過去1件のみ（Run 2 のもの）。以降すべて `forwarded`。

---

## 4. gate 2 で躓いた点（次回のための記録）

`apply_patch` 自体は bridge を通るが、codex 側の別レイヤーで2段階に引っかかった:

1. **auto-review**: `approvals_reviewer = "auto_review"`（config.toml）だと patch 前審査が
   `codex-auto-review` モデルで走り、OpenRouter 非対応名なので `model_not_allowed` →
   「unacceptable risk」として拒否。`-c approvals_reviewer="user"` で回避できる
   （`off` という値は存在しない。選択肢は user/auto_review/guardian_subagent）
2. **sandbox**: `codex exec` のデフォルトは read-only。patch には `-s workspace-write` が要る

つまり headless gate の正しい形は:

```bash
cd /private/tmp/codex-openrouter-e2e.R2
codex exec -m <model> -s workspace-write -c 'approvals_reviewer="user"' "<prompt>" < /dev/null
```

app（ChatGPT.app）経由では利用者の approval 設定に従うので、この問題は CLI gate 固有。

---

## 5. 現在の環境状態

| 項目 | 状態 |
| --- | --- |
| supervisor | `active: true`、guard 稼働中 |
| launch プロセス | background 生存中（proc_b90ee0099934）。workspace は R2 |
| ChatGPT.app | 強制終了後に launch が再起動している状態 |
| `~/.codex/config.toml` | catalog block + provider block 復元済み、`model_provider = "openrouter"` |

注意: repo の unit test 全件実行中に maintenance test が upgrade/rollback シミュレーションを
行い、一度 supervisor inactive + config 復元が起きた。テスト後に `launch` の再実行が必要。

止めたいとき: `pkill -f 'codex-openrouter launch'`（finally で config が戻る）

---

## 6. 残っていること

> **[2026-08-23 解決済み]** 後続の `0823-run4-c2-execution-plan.md` で完遂。
> PR #28 として merge・promote 済み(verify-tools 連続2回 verified、CI 11/11、
> gate 2 オーバーライドなしで通過)。app 経由の web_search 送出検証のみスコープ外として維持。

- ~~PR #24 の CI 確認~~ → PR #28 として 11/11 SUCCESS
- ~~draft → ready 判断~~ → merge 完了
- ~~`models verify-tools` canary 再実施~~ → 連続2回 verified(max_output_tokens 256 へ
  上げることが必須だった。64 では reasoning tokens に食われて arguments が途切れた)
- app 経由(191 tools)での web_search 型送出は今回未検証。CLI と同じ型集合なら同じ翻訳で通る
