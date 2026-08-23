# 0823 次フェーズ検討: P1(catalog堅牢化) / P2(マルチモデルE2E)

作成日: 2026-08-23 / branch: `main` @ `3e6fb60`(PR #30 後)
前提: P1+P2 リファクタリング完遂(PR #28)、P0 app 経由実機検証済み。
詳細は `task/0823-handoff-next-phase.md` 参照。

---

## 候補比較

| 軸 | A: catalog堅牢化 | B: マルチモデルE2E |
|---|---|---|
| 動機 | build追従の手動メンテがボトルネック | β'のauto-review書き換えが単一モデルでしか実測していない |
| リスク性 | 予防投資(壊れてはいない) | **潜在バグの可能性あり**(default_model固定の設計依存) |
| 規模 | 1日 | 半日〜1日 |
| 依存 | なし | なし |

**推奨: B を先に。** 理由:

1. guard.resolve_model() は審査要求を常に default_model へ写像する。複数モデル構成で
   「flashで作業中の審査がpro宛てになる」等のコスト/能力ミスマッチが起こる。これは
   バグではなく設計だが、**実測ゼロの領域**
2. catalog堅牢化(A)は現状動いているものの改善であり、緊急性がない
3. Bの過程で判明する課題(例: 審査先をリクエスト元modelへ寄せる拡張)が
   A側のcatalog設計にも影響しうる → 先にBの方が手戻りが少ない

---

## Phase B: マルチモデル E2E 強化(推奨着手順)

### B-1: 単体テスト整備(TDD, ~2h)

- [ ] `guard.resolve_model()` / `allows()` の review_model 複数モデル時挙動テスト
      (既存テストは単一モデル前提の可能性 → tests/test_guard.py を確認して拡充)
- [ ] profile 3 モデル(default_model=flash)での Guard 生成テスト(supervisor 配線)
- 判定基準: 既存仕様(default_model 固定)がテストで固定されること

### B-2: 実測準備(~1h)

- [ ] profiles/multi.json 作成(flash + pro の 2 モデル、default=flash)
- [ ] launch + check で multi profile が正しく catalog/guard に反映されるか確認
- コスト: $0(リクエストなし)

### B-3: gate 実測(~1h, ~$0.05)

- [ ] gate 1: exec_command が flash で通ること
- [ ] gate 2: apply_patch 審査要求が default_model(flash)へ書き換わって通ること
      (guard.log で model 書換を確認)
- [ ] `-m deepseek/deepseek-v4-pro` で作業させた場合も審査が flash へ寄ることを確認
      (ここで「pro で作業→flash で審査」の能力差が問題にならないか観察)
- 判断ポイント: 能力ミスマッチが顕在化したら「審査先 = リクエスト元 model」への
  拡張(P3候補)を起票するかユーザーに相談

### B-4: verify-tools 全モデル canary(~1h, ~$0.10)

- [ ] `models verify-tools --stdin-json` に registry 全 5 モデルを渡す
- [ ] 各モデルの tool_support 結果を記録(verified/partial/unsupported の分布)
- [ ] kimi-k3 / glm-5.2 / minimax-m3 は未実測モデルなので初めての互換データになる

### B-5: 記録・PR

- [ ] 結果を task/*.md へ記録、PR → CI → merge

---

## Phase A: catalog 自動更新の堅牢化(B 完了後)

### A-1: drift 検出の自動化強化(TDD)

- 現状: doctor が unknown_template_fields(名前増加)と template_field_drift(snapshot比)を
  警告。ただし「値だけ変わる」更新は素通り(catalog.py:335 の既知制限)
- [ ] 値ドリフト検出: snapshot 比較に型/必須性の変化を追加するか、値ハッシュを取るか設計から
- [ ] doctor の警告に「次のアクション(再capture方法)」を含める

### A-2: 再 capture フローの runbook 化+自動化

- [ ] bundled catalog 更新時の手順を scripts/capture-template.sh 化
      (build 差分表示 → snapshot 更新 → unit/check → PR)
- [ ] ChatGPT.app 自動更新(build 変更)を検知したら doctor が具体的コマンドを案内する

### A-3: tool-wire-builds.json の保守ポリシー

- 6849/6720 のように古い build をいつ落とすかのルール明文化(現状: 最新+直前)
- fixture の representative ツール選定基準を文書化

---

## コスト上限

- Phase B: ~$0.15(gate 実測 + canary 5 モデル)
- Phase A: $0(実ネットなし)

---

## 未確定・要判断事項

1. **B-3 の能力ミスマッチ**: flash で審査が回る設計で運用開始して良いか。
   問題が出たら P3(審査先=リクエスト元 model)として別計画
2. **verify-tools の全モデル canary**: kimi/glm/minimax が unsupported でも
   registry に残すのか(表示上の問題か、除外すべきか)は結果を見てから判断
