# P1+P2 統合改善リファクタリング計画: canary頑健化とauto-review対応

作成日: 2026-08-22 / branch: `main` @ `34c1847`(PR #24 マージ後)

Status: **実装完了(2026-08-22)。** A1–C2 全タスク完遂。B0 の調査結果により
P2 は plan の α(config一時上書き)から **β'(catalog override + guard alias)へ
方針変更** — config を一切触らない解決。詳細は §2.1 追記と commit
`eb29cc4` / `950637b` を参照。

---

## 0. 背景と目的

PR #24 マージにより tool bridge は実機 gate 1/2 を通るまでになった。残る弱点は2つ:

| 弱点 | 深刻度 | 現状 |
| --- | --- | --- |
| **P1: canary の provider 抽選ノイズ** | 低(表示専用) | structured canary が `sort` なしで endpoint を引くため、22/30 の provider 外れを引くと freeform を試さずに `unsupported` 表示(`toolcompat.py` `_probe` 短絡)。実測 4回中1回で発生 |
| **P2: auto-review が patch を必ず拒否** | 中(app 利用者に影響) | `approvals_reviewer = "auto_review"`(config.toml top-level、**利用者または純正appが書いた値**)だと、patch 前審査モデル `codex-auto-review` が OpenRouter 非対応名のため `model_not_allowed` → 「unacceptable risk」拒否。CLI headless では `-c approvals_reviewer="user"` で回避済みだが、app 経由では未対処 |

両方とも「実測で確認された不具合」であり、推測ベースのリファクタリングではない。
**P2 は app 利用者の apply_patch を黙って壊す可能性があるため P1 より優先。**

---

## 1. 現状分析(実測済みの証拠)

### P1: canary 短絡の構造

`src/codex_openrouter/toolcompat.py`:

```
structured probe 失敗 (status 400/422)
  → freeform probe をスキップ (line 334-338 の短絡)
  → status = "unsupported"
```

- `_body()` (line 206) は `provider.sort` を指定しない → OpenRouter が毎回別 endpoint を抽選
- DeepSeek で structured_outputs 公称は 30 endpoint 中 22 (0822-toolbridge-fix-plan.md 実測)
- 外れ endpoint を引くと structured だけ失敗し、freeform は未試行
- 影響範囲: `models list` の description 表示のみ(gate はしない)。ただし「verified」表示が揺れるのは信頼を損なう

### P2: auto-review の構造

- `approvals_reviewer` は config.toml top-level key。codex 0.148.0 の有効値は `user` / `auto_review` / `guardian_subagent` の3つ(`off` は存在しない — gate 2b で実測)
- 現在の `~/.codex/config.toml` には `approvals_reviewer = "auto_review"` があり、これは codex-openrouter 側(marker block)ではなく**純正app/利用者側の設定**
- OR モデル選択中にこの値が `auto_review` だと、審査要求が `codex-auto-review` モデルへ行き、OpenRouter ルーティング外で 400 → patch 拒否
- supervisor は marker block でのみ config を管理し、marker 外の key は触らない設計(`configblock.py`: 「marker外の同名設定は利用者所有とみなす」)
- **衝突**: 利用者所有の設定を黙って書き換えるのは現行設計思想に反する。しかし書き換えないと OR モデル中の apply_patch が必ず失敗する

---

## 2. 設計判断(要承認)

### P1-A: canary に `provider.sort` を付けて endpoint 固定

`_body()` に追加:

```python
if spec.get("zdr_supported", True):
    body["provider"] = {"zdr": True}
# 追加: provider抽選ノイズを消す。ZDR指定と併用可能かは要実測
body["provider"]["sort"] = "price"  # または "throughput"
```

**リスク**: `zdr: true` と `sort` の併用が OpenRouter API で許可されるか未検証。
**代替**: 短絡をやめて structured 失敗時も freeform を試す(判定ロジック変更):

```python
# 現行: freeform を structured の条件付きでしか試さない
freeform, _ = _probe(...) if structured else (False, None)
# 変更: 常に両方試す。コストは canary 1回あたり +1 リクエスト
```

→ **提案: 両方やる**。`sort` 固定でノイズを減らしつつ、短絡解除で「外れ1回 = unsupported」を構造的に排除。コスト増は canary 実行時のみ(手動・低頻度)。

### P2 方針変更(B0 の結果): α → β'

B0 の codex バイナリ調査で以下を確認:

- `auto_review_model_override` は **model catalog の per-model フィールド**
  (codex バイナリの catalog schema 文字列群で確認)
- 審査モデル `codex-auto-review` 自体は bundled catalog 由来の
  `visibility: hide` entry で、自作 composite catalog にも同梱される

よって plan 当初の α(supervisor が config を一時上書き + restore)は不要。
採用した β':

1. catalog: OR entry に `auto_review_model_override = <自身のslug>` を設定
2. guard: `AUTO_REVIEW_ALIAS`(codex-auto-review)宛て request を
   `profile.default_model` へ書き換えて許可。未設定なら拒否(fail-closed 維持)
3. 利用者の `approvals_reviewer = "auto_review"` 設定は尊重され、
   config への書き込み・復元が一切不要になる

### P2: approvals_reviewer の扱い — 3案

| 案 | 内容 | 懸念 |
| --- | --- | --- |
| **α: launch 時に一時上書き + restore** | `apply_config()` で `approvals_reviewer` の現在値を state へ退避し `"user"` へ upsert。cleanup(finally)で元の値へ復元。既存の `saved_model`/`saved_provider` と同じパターン | 利用者の意図的な `auto_review` 選択を稼働中無視することになる。ただし現状その選択は OR モデルでは機能しないため「動かない設定を一時的に動く設定へ寄せる」と言える |
| β: catalog 中和で auto-review 対象外にする | catalog 側で何かしら auto-review を無効化するフィールドを探す(`node_repl_auto_review_required: False` はあるが apply_patch 用かは未確認) | フィールドの意味が未実証。効果の実測が必要 |
| γ: 何もしない(ドキュメントのみ) | README/task へ「OR モデル使用中は approvals_reviewer=user 推奨」と記載 | app 利用者がハマり続ける |

→ **提案: α を第一候補**。既存パターン(saved_model/saved_provider)との整合性が高く、cleanup で確実に戻る。β は α 実装前に 30 分だけ調査し、該当フィールドが見つかればそちらへ切替。

---

## 3. タスク分解(bite-sized)

### Phase A: P1 canary 頑健化

#### Task A1: `sort` 併用の実測検証(実装なし・課金 ~$0.01)

**目的**: `provider: {zdr: true, sort: "price"}` が OpenRouter で受け付けられることを curl で確認。

```bash
KEY=$(~/.local/bin/codex-openrouter-credential get)
curl -s https://openrouter.ai/api/v1/responses -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{...structured probe body + sort...}'
# Expected: status 200 または provider.sort に関する明示エラー
```

- 成功 → Task A2 へ
- 失敗 → `sort` 単独 or 短絡解除のみへ方針変更(A3 だけ実施)

#### Task A2: `_body()` に sort 追加(TDD)

**Files**: `src/codex_openrouter/toolcompat.py` (_body), `tests/test_toolcompat.py`

1. 失敗テスト: `_body()` が出す body に `provider.sort == "price"` があること(zdr 指定あり/なし両方)
2. 実装: zdr ブロックの直後に sort を追加
3. 全 test pass 確認 → commit `fix(toolcompat): canary requestにprovider sortを追加`

#### Task A3: 短絡解除 — structured 失敗でも freeform を試す(TDD)

**Files**: 同上

1. 失敗テスト: structured=False + freeform=True のとき `partial` になること(現行は unsupported)
   - 既存テスト `test_structured_failure_is_unsupported` を仕様変更として更新
2. 実装: line 334-338 の短絡を除去し常に両方 probe
3. 判定表をコメントで明記:
   ```
   structured & freeform → verified
   structured only       → partial (freeform非互換)
   freeform only         → partial (structured非互換)  ← 新規
   neither               → unsupported
   ```
4. 全 test pass → commit

#### Task A4: doctor 表示の整合確認

`doctor` が cache status をどう表示するか変わりがないことを確認(表示文言の変更は不要の想定)。`./codex-openrouter check` + unit 全件 PASS。

### Phase B: P2 approvals_reviewer 対応

#### Task B0: β 調査(30分上限)

catalog フィールド(`apply_patch_tool_type`, `node_repl_auto_review_required` 等)が
apply_patch の auto-review 発火を抑制するか、codex バイナリ strings と実機 1 回で確認。
- 見つかれば → B1 は catalog 中和へ変更
- 見つからなければ → α で確定

#### Task B1: supervisor による一時上書き + restore(TDD)

**Files**: `src/codex_openrouter/supervisor.py` (apply_config / cleanup), `src/codex_openrouter/state.py or 同等` (state field 追加), `tests/test_supervisor.py`

1. 失敗テスト: `apply_config()` 後に config.toml の `approvals_reviewer == "user"`、state に元値退避
2. 失敗テスト: cleanup 後に元値へ復元されている(元値なし = key ごと削除)
3. 実装: `saved_approvals_reviewer` state field + `apply_config()` 内 upsert + `_restore_selection_text()` または finally パスで復元
4. 注意: `approvals_reviewer` は string 値なので `configblock.upsert_top_level()` がそのまま使える
5. 全 test pass → commit

#### Task B2: 実機確認(headless gate で `-c` オーバーライドなし)

```bash
cd /private/tmp/codex-openrouter-e2e.R2
codex exec -m deepseek/deepseek-v4-flash-0731 -s workspace-write \
  "target.py に farewell2(name) を追加して。" < /dev/null
# Expected: auto_review のままでも patch が通る(B1 により user へ寄っているため)
```

#### Task B3: cleanup 復元の実機確認

launch 停止 → config.toml の `approvals_reviewer` が元値 `auto_review` へ戻っていること。

### Phase C: 仕上げ

#### Task C1: ハンドオフ/plan 更新

`task/0822-run3-handoff.md` §6 の残タスクを消化済みへ更新。本 plan の Status を完了へ。

#### Task C2: PR 作成 → CI → merge → promote

ruleset ci-guard 配下なので PR 経由。merge 後 `upgrade` で installed runtime 更新。

---

## 4. Verification(全体完了条件)

- [x] unit 全件 PASS(Python 3.11–14)— **386 OK**
- [x] ruff PASS
- [x] `models verify-tools` が連続 2 回 `verified`(短絡解除の効果確認)→ **達成(2026-08-23)**。
  ただし追加で max_output_tokens 64→256 が必要だった(reasoning tokens が arguments を
  打ち切る。`95a8929` 実測)
- [x] headless gate 2 が `-c approvals_reviewer` オーバーライドなしで通る(実測: farewell2 追加成功、審査要求は deepseek へ forwarded)
- [x] launch 停止後、config.toml が元へ復帰(β' は config 不接触のため approvals_reviewer は auto_review のまま維持)
- [x] CI green → PR merge → promote PASS → **PR #28 merge・promote 完了(2026-08-23、CI 11/11)**

## 5. コスト見積もり

- A1 実測: ~$0.01
- verify-tools 2 回: ~$0.02–0.05
- B2 gate: ~$0.10 未満(deepseek flash)
- 合計: **~$0.15 以内**

## 6. スコープ外(明示)

- web_search 翻訳の app 経由検証(旧 P0)は別フェーズ
- ENDPOINT 定数化(P3)は今回触らない
- catalog フィールドの追加中和は B0 で根拠が出た場合のみ
