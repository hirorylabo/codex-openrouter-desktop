# 0820 ChatGPT.app build 6849 追従

作成日: 2026-08-20

対象branch: `codex/openrouter-tool-bridge`（PR #24）

Status: **pin昇格とtest更新まで完了。実機5-gate E2Eでの確認待ち。**

## Context

`task/0820-deepseek-completion-plan.md` の Phase 2（実機 Run 1）実行中に、
`/Applications/ChatGPT.app` が `26.814.41407` build `6720` →
`26.818.21641` build `6849` へ自動更新された。

launcherは `models/tool-wire-builds.json` に無いbuildを拒否して起動せず終了した。
これは設計どおりのfail-closedで、有料requestは発生していない。

```
モデルカタログを再生成しました（build 6849）
ERROR: ChatGPT build 6849 はtool契約の互換確認待ちです。純正ChatGPT.appは通常どおり利用できます。
```

## 調査結果: tool契約を壊す変更は見つからない

app停止中に読み取り専用で確認した。

| 観点 | 結果 |
| --- | --- |
| 署名 / 無改変 | 署名有効、ASARにpatch marker 0件 |
| cloneテンプレート | doctorは「既知のフィールドだけ」と報告。未知フィールドの新設なし |
| テンプレートのフィールド数 | 38 → **37** |
| 削除されたフィールド | `supports_parallel_tool_calls` |
| 追加されたキー | `model_messages.multi_agent`（値は `null`、中身なし） |
| `instructions_template` | **不変**（`model_messages` の +21B は `multi_agent: null` の分だけ） |
| その他のフィールド値 | 変更なし |
| doctor | `FAIL` はbuild pinの1件のみ。他は全てOK |

前世代snapshot `clone-template.json.previous`（PR #21で導入）があるため、
6720 → 6849 の差分を値まで機械的に取れた。

## `models verify-tools` の canary はbuild判定に使えない

`toolcompat` の canary は `_body()` が組んだ**自前のrequest**をTool Bridge経由で
OpenRouterへ送る。ChatGPT buildはcache keyにしか使われず、requestの中身には入らない。
したがってこのcanaryが失敗しても「そのbuildのtool wireが壊れた」証拠にはならない。

実際、build 6849に対して4回観測した結果は毎回違った。

| 実行 | structured | freeform |
| --- | --- | --- |
| `models verify-tools` | 成功 | 失敗（`partial` としてcache） |
| 追試1 | 成功 | 例外（変換済みcustom callのarguments契約が不正） |
| 追試2 | 成功 | 成功 |
| 追試3 | 失敗 | 成功 |

`deepseek/deepseek-v4-flash-0731` は単発canaryでtool callを安定して返さない。
`provider` はいずれも metadata に出ず、`candidate_count` は30だった。
**この揺れはmodel側のものであり、6849の回帰ではない。**

`state/tool-compatibility.json` には build 6849 / `partial` のcacheが残っている。
成功するまで回して`verified`に上書きするのは証拠の捏造なので行わない。UIの
tool状態表示が `partial` になるだけで、launcherの起動経路はこれで塞がらない。

## 変更詳細

`UPSTREAMS.md` の更新手順（最新buildへ昇格し、直前buildだけ残す）に従う。

| file | 変更 |
| --- | --- |
| `models/tool-wire-builds.json` | `6849` を昇格し `6720` を直前として残す。`6662` を落とす |
| `tests/fixtures/codex-tool-wire-6849.json` | 追加。契約に差分が見つからないため、shapeは6720と同一で `build` / `chatgpt_version` だけ更新 |
| `tests/fixtures/codex-tool-wire-6662.json` | 削除（参照がなくなるため） |
| `tests/test_toolbridge.py` | 既定fixtureと「最新+直前」の組を `("6849", "6720")` へ |

`src/` は変更していない。中和すべき新フィールドが無いため。

## 未決（実機gateの結果で決める）

6849は純正entryから `supports_parallel_tool_calls` を落としたが、repoは今も
OpenRouter entryへ付与している（`src/codex_openrouter/catalog.py` と
`models/registry.json`）。証拠なしに契約を変えないため、5-gate E2Eの
gate 5（parallel turn）の挙動を見てから判断する。

## Verification

```bash
PYTHONPATH=src python3 scripts/run_unit_tests.py      # 363 tests PASS
uvx ruff@0.16.3 check .                                # PASS
```

実機は `task/0820-deepseek-completion-plan.md` の Phase 2 / 3 に従う。
5-gate E2Eが通って初めて、6849のtool wireを実測で確認したことになる。
