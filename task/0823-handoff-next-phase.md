# 0823 新スレッド用ハンドオフ: P1+P2 完遂後の状態と次フェーズ

作成日: 2026-08-23 / branch: `main` @ `d9202ab`(PR #29 マージ後)

Status: **P1+P2 リファクタリングは完遂済み(PR #28 merge `d9571f2`、promote 済み)。
追加で P0(app 経由実機検証)も完遂。次フェーズの着手待ち。**

---

## TL;DR

- **PR #28 で P1(canary 頑健化)+P2(auto-review OR 経由化)が main 反映・promote 完了**
  - installed: v0.2.1 @ `d9571f2`(`install-manifest.json` 確認済み)
- **B0 調査により P2 は plan の α(config 一時上書き)から β'(catalog+guard 解決)へ方針変更**
  - config 不接触のため cleanup/restore 機構は不要になった
- **build 6962(ChatGPT.app 自動更新、codex-cli 0.149.0-alpha.4.1)対応も完了**
  - wire 捕獲 → fixture 追加 → allowlist 更新 → unit/check 全 green
- **P0(app 経由 computer-use 実測)も通過**: web_search 翻訳・apply_patch+auto-review が
  ChatGPT.app UI 上で実機動作確認済み

---

## 1. 現在の確定状態

### repo / runtime

| 項目 | 値 |
|---|---|
| main | `d9202ab`(PR #29 = Run 4 記録 + scratchpad gitignore) |
| installed runtime | v0.2.1 @ `d9571f2` |
| ChatGPT.app build | **6962**(codex-cli 0.149.0-alpha.4.1、v26.818.41509) |
| tool-wire-builds.json | 6849 / 6962 の 2 エントリ(6720 は外れた) |
| unit tests | **386 OK** / ruff pass |
| launch 状態 | 停止中(launch も ChatGPT.app も未稼働。再開は `python3.11 ./codex-openrouter launch <repo>`) |

### 主要 commit(PR #28 内)

- `950637b` feat: auto review審査をORモデル経由で完結させる(P2 本体)
- `eb29cc4` fix(toolcompat): canary requestに provider sort を追加し短絡を解除(A2+A3)
- `95a8929` fix(toolcompat): canaryのmax_output_tokensを64から256へ上げる
- `356c442` docs: 計画書へ実装完了と β' 方針変更を記録(C1)
- 6962 対応コミット(fixture `codex-tool-wire-6962`、allowlist、テスト修正)

---

## 2. 設計上の重要な決定事項(今後のコード変更時に必須)

### P2 = β' 方針(config 不接触)

- catalog: OR entry に **`auto_review_model_override = <自身の slug>`** を設定。
  codex バイナリの schema 文字列群(`supports_search_tool, use_responses_lite,
  auto_review_model_override, model_specialty...`)で per-model フィールドと確認済み
- guard: `AUTO_REVIEW_ALIAS = "codex-auto-review"`(guard.py 定数)宛て request を
  **profile.default_model へ書き換えて許可**。review_model 未設定なら拒否(fail-closed 維持)
- 利用者の `approvals_reviewer = "auto_review"` は尊重。有効値は
  `user` / `auto_review` / `guardian_subagent` の 3 つ(**`off` は不存在**を実測済み)

### P1 canary

- `_body()` に `provider={"zdr": True, "sort": "price"}`(A1 実測: 併用可、status 200)
- structured 失敗でも freeform を必ず試す(短絡解除)。freeform のみ成功は **partial** 判定
- **max_output_tokens=256 必須**: deepseek は reasoning tokens を出力に含むため
  64 だと arguments が `{"content": "` で打ち切られる(実測)。戻すな

### web_search 翻訳(Run 3 から継続)

- codex 型 web_search → `{"type":"openrouter:web_search"}` server tool へ翻訳
- config `web_search="disabled"` は native も無効化するため**不採用確定**
- app 経由では citation 表示なしで回答に織り込まれる仕様(OpenRouter server-side)

---

## 3. P0 実測結果(computer-use / cua-driver、2026-08-23)

ChatGPT.app UI を cua-driver で直接操作して検証:

| 検証 | 結果 |
|---|---|
| app 起動・OR routing | ✅ build 6962、全リクエスト deepseek へ forwarded |
| web_search 翻訳 | ✅ 「東京の天気」→ 気象庁出典の実天気回答、denied ゼロ |
| apply_patch + auto-review | ✅ farewell3 追加+動作確認まで完了、審査も OR 経由 |
| guard.log | ✅ app 経由 11 リクエスト全 forwarded / native slug のみ denied |

computer-use 運用メモ:

- CuaDriver.app に Accessibility + Screen Recording の TCC 許可済み(2026-08-23 付与)
- `cua-driver call get_window_state '{"pid":..., "window_id":..., ...}'` で AX tree 取得
  - element_index 単独は不可、**element_token**(snapshot_id:N 形式)が要る
  - type_text は `effect: unverifiable` が常態 → 必ず再 capture で value を確認
  - ChatGPT.app の window_id は起動毎に変わる(get_accessibility_tree で取り直し)

---

## 4. 次フェーズ候補(未着手)

### P1: catalog 自動更新の堅牢化

- `NATIVE_ONLY_FIELDS`/`KNOWN_TEMPLATE_FIELDS` の手動メンテがボトルネック
- bundled catalog の field drift 検出 → doctor 警告統合(`template_field_drift` 活用)
- 「継ぐか中和か」判定フローの runbook 化。規模: 1 日

### P2: マルチモデル profile の E2E 強化

- 現在の実測は deepseek flash 単一。複数 profile.models 時の auto-review 書き換え
  (default_model 固定)と canary 全モデル実行は未検証。規模: 半日〜1 日

---

## 5. 作業時の既知の手順・罠(過去 Run からの教訓)

1. **launch**: `python3.11 ./codex-openrouter launch /private/tmp/codex-openrouter-e2e.R2`
   (system python3 は 3.9 で拒否される)。verify-tools 等と lifecycle lock が競合するので
   同時実行不可 → 先に停止するか順番に
2. **gate コマンド**: `cd /private/tmp/codex-openrouter-e2e.R2 && codex exec -m
   deepseek/deepseek-v4-flash-0731 -s workspace-write "<prompt>" < /dev/null`
   (-c approvals_reviewer オーバーライドは β' 後不要)
3. **main 直 push 不可能**: ruleset ci-guard → 全変更 PR 経由
4. **テスト**: `PATH="$HOME/.local/bin:$PATH" PYTHONPATH=src python3.11 scripts/run_unit_tests.py`
   (386 基準)/ lint: `uvx ruff@0.16.3 check src tests`
5. **wire 捕獲**: scratchpad/capture-upstream.py(port 4399)+ base_url override。
   注意: scratchpad/ は gitignore 済み(PR #29)
6. **Hindsight memory は無効化済み**(2026-08-23、memory.provider=built-in)。
   embedding quota エラー頻発のため。記録は repo task/*.md へ

---

## 6. 関連ドキュメント

- `task/0822-p1p2-refactor-plan.md` — 計画+実装完了記録(β' 方針含む)
- `task/0822-run3-handoff.md` — web_search 翻訳の解決記録
- `task/0823-run4-c2-execution-plan.md` — Run 4 実行計画と検証結果
- `models/tool-wire-builds.json` — build allowlist(6849/6962)
- `tests/fixtures/codex-tool-wire-6962.json` — 6962 代表 3 ツール fixture
