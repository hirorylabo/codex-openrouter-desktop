# 0823 Run 4: C2 (PR→CI→merge→promote) 実行計画 — 並列検証による確度評価付き

Status: **完了。** PR #28 は 2026-08-23 13:08 に merge 済み。V1–V4 全 green。
promote も完了(installed source d9571f2 / build 6962)。

作成日: 2026-08-23 / branch: `fix/p1p2-canary-and-approvals` @ `95a8929`(未push 5 commits)
前提: `0822-p1p2-refactor-plan.md` の A/B フェーズ実装済み。canary 問題(max_output_tokens 64
では reasoning tokens に食われて arguments 途切れ)は `95a8929` で解決済み。

---

## TL;DR

- 残タスクは **C1(handoff更新)→ C2(PR→CI→merge→promote)** のみ。実装は完了している
- 本 plan は「そのまま出して良いか」の確度を、**独立した 4 本の検証ライン(V1–V4)を
  並列に走らせて**上げ、全て green なら最速経路(案α)で merge する
- どれか 1 本でも red が出たら、red の内容に応じて案β(修正込み)/案γ(段階的リリース)
  へ切替。判断基準は §3 のマトリクス

---

## 1. 現在の状態(2026-08-23 13:15 時点の実測)

| 項目 | 状態 | 根拠 |
| --- | --- | --- |
| 未push commits | 5 (`eb29cc4`..`95a8929`) | `git log origin/main..HEAD` |
| unit 全件 | **386 OK**(+74 subtests, Python 3.12 venv) | 2026-08-23 再実測 |
| ruff | **pass** | 同上 |
| verify-tools 連続2回 verified | **達成済み**(13:03 キャッシュ `verified`) | `tool-compatibility.json` |
| supervisor | inactive(native 状態) | `supervisor.json` |
| worktree | clean | `git status` |

### 未確定事項(plan 作成時点で判明している懸念)

1. **`zdr_supported: null` 時に provider ピン留めが効いていない疑義**
   `toolcompat._body()` は `if spec.get("zdr_supported", True)` 分岐だが、生成 catalog の
   deepseek spec は `zdr_supported: None`(キー自体は存在)。Python の `.get()` は**キーが
   あれば値(None)を返す**ため falsy 判定 → sort/zdr 固定がスキップされている可能性。
   実害は未確認(verified 2 回出ているので、当たらなくても通る provider だった可能性)。
   → V2 で実測する
2. **installed runtime が古い**(install-manifest: source d363380 / build 6849)。
   今回の修正群は promote するまで ChatGPT.app 側に反映されない
3. **CI 11 checks は未走行**(push 前のため)。local 386 OK との差分リスクは低いがゼロではない
4. **app 経由(191 tools)の web_search 送出は今回も未検証**(スコープ外として維持)

---

## 2. 並列検証ライン(4本)

コスト総額 ~$0.05 以内。V1/V3/V4 は課金なし。

### V1: CI 全チェック(自動)

- push 後の GitHub Actions 11 checks。local と同一条件のはずが、Python matrix 差分で
  落ちることが過去にある
- 合格条件: **11/11 pass**
- 所要: push 後 ~10 分。他ラインと並列で進む

### V2: canary の再現性・provider 固定の有効性実測(課金 ~$0.02)

- OpenRouter に canary body を直接 3 回投げる:
  (a) sort なし(zdr Supported=None の現状挙動)
  (b) `{"zdr": true, "sort": "price"}` 強制
  (c) `max_output_tokens: 256` + sort 強制(= 現行 `_body()` の意図した形)
- 目的: ① 256 上げが provider 抽選に依存せず安定するか(3/3 PING 着弾)。
  ② zdr_supported:null でも sort が効くべきか(効くなら後続 patch、効かないなら記録のみ)
- 合格条件: (c) が **3/3 成功**。(a)(b) は情報収集なので失敗しても可
- 所要: 数分。V1 と完全並列

### V3: gate 2 の最終形再実行(headless、課金 ~$0.05 未満)

- `/private/tmp/codex-openrouter-e2e.R2` で `-c approvals_reviewer` オーバーライド**なし**
  の farewell3(name) 追加。β'(guard alias)経路の回帰確認
- 合格条件: apply_patch 着弾 + guard.log が deepseek へ forwarded(native slug denied 維持)
- 所要: 数分。V1/V2 と並列

### V4: promote 後の環境整合(merge 後に実施、課金なし)

- `upgrade` → install-manifest の source_commit が merge SHA へ変わること
- launch 起動 → 停止(`pkill -f 'codex-openrouter launch'`)→ config.toml が
  `approvals_reviewer = "auto_review"` のまま維持されること(β' は config 不接触のため)
- 合格条件: manifest 更新 + config 不変
- 所要: ~5 分。C2 の一部として直列

---

## 3. 実行案の選択マトリクス

| 案 | 条件 | 内容 |
| --- | --- | --- |
| **案α: 最速経路(推奨)** | V1–V3 全 green | C1(doc更新)→ push → PR → V1 待ち → merge → upgrade → V4 →完了 |
| **案β: 修正込み** | V2(c) が 3/3 未達、または V3 red | red 原因を TDD で修正(例: `zdr_supported is not False` への分岐変更、max_output_tokens 再調整)→ 新 commit → 全 V 再実行 → 案αへ合流 |
| **案γ: 段階的リリース** | V1 で CI 差分が見つかり原因特定に >30分 | CI green commit 範囲だけ先に merge し、残りは別 PR に分割。install-manifest の digest 整合が崩れるため最終手段 |

判断タイミング: V1/V2/V3 は push 前に完結させる(V2/V3 だけなら push 不要)。V1 だけは
push 後にしか走らないため、push は「V2/V3 が green であること」を条件に行う。

---

## 4. 手順(案α採用時)

1. [ ] V2 実測(canary 3 パターン)→ 結果を本ファイル §5 に記録
2. [ ] V3 実行(gate 2 最終形)→ 結果記録
3. [ ] V2/V3 green を確認して push
4. [ ] PR 作成(title: `fix: canary短絡解除とauto-review対応の仕上げ`)→ V1 待ち
5. [ ] 11/11 green → merge(ruleset ci-guard 配下、PR 経由)
6. [ ] `upgrade` → V4(manifest + config 確認)
7. [ ] C1: run3-handoff §6 と p1p2-refactor-plan の Status を完了へ更新(別 docs PR or 同梱)
8. [ ] 本 plan の Status を完了へ

## 5. 検証結果記録欄

- V1 (CI): ✅ PR #28 で 11/11 SUCCESS(CI + CodeQL)
- V2 canary: (a) no-sort 3/3 PING (b) sort+zdr 3/3 PING (c) sort only 3/3 PING
  → zdr_supported:null でも provider ピン留め不要で安定。256 で全パターン健全。
  catalog 側修正は不要(plan §6 のスコープ外維持)
- V3 gate2: ✅ farewell3 追加成功(オーバーライドなし)。guard.log: deepseek forwarded /
  native slug (gpt-5.6-luna) denied 維持。launch 停止後 config 復帰も確認(provider=openai,
  approvals_reviewer=auto_review 不変)
- V4 promote後: ✅ install-manifest source=d9571f2 / build=6962 へ更新済み(2026-08-23 13:2x 時点で
  upgrade 済みだったことが判明)。check PASS(no persistent files changed)

## 6. スコープ外(明示)

- app 経由(191 tools)web_search 送出の実機検証(旧 P0、別フェーズ)
- `zdr_supported: null` の catalog 生成側修正(V2 の結果次第で後続 plan 化)
