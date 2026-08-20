# 0820 ChatGPT.app build 6849 追従

作成日: 2026-08-20

対象branch: `codex/openrouter-tool-bridge`（PR #24）

Status: **pin昇格・workspace受け渡しの修復まで完了。実機5-gate E2Eでの確認待ち。**

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

## 実機で見つかった回帰: `--open-project` が効かない

**build 6849では、`--open-project <path>` を渡してもChatGPTが別のprojectを開く。**
直前に使っていたprojectが復元され、渡したpathは無視される。

確認手順と結果:

| 確認 | 結果 |
| --- | --- |
| `ps -o args=` | `ChatGPT --open-project /private/tmp/codex-openrouter-e2e.RUN1` が確かに渡っている |
| 画面 | sidebarとcomposerのchipは直前に使っていた別projectのまま |
| gate 1のsession | `session_meta.cwd` が 利用者が直前に開いていた別project になり、auditorがfail-closedで拒否 |
| 既知pathでの追試 | supervisorを止めた素の状態で `--open-project <このrepoのpath>` を渡しても、開くのは同じ別project |

pathがtemp配下だから、appが知らないfolderだから、という条件依存ではない。
**渡したprojectを開かない**という一律の挙動である。

影響は実機E2Eだけではない。launcherのworkspace受け渡し（folder drop / Open With）は
`src/codex_openrouter/supervisor.py` の `--open-project` に依存しているため、
**6849では利用者がdropしたfolderがChatGPTへ届かない**。

### 効く経路: LaunchServiceのopen document

`open -b <ChatGPTのbundle id> <folder>` は同じbuild 6849でもprojectを切り替える。
実測では、送った直後にsidebarの先頭が渡したfolderに変わり「チャットはありません」になった。
壊れているのは起動引数の `--open-project` だけで、document open経路は生きている。

`ファイル` menuにも `フォルダーを開く` があるが、これはmodalのfolder pickerで、
自動化にも利用者の手順にも向かない。

### 副次的に判明したこと

- 実機のguard logに残った今回のforwarded 2件には `tool_request` / `duration_ms` /
  token数が付いていない。`tool_map.has_tools` が偽だったことになる。tool宣言の
  渡り方が変わった可能性があるが、workspace側で止まっているため未確認。
- macOSの入力ソースが日本語IMEのとき、System Eventsの
  `keystroke "a" using command down` が「あ」に化ける。GUI駆動では `key code` を使う。

## 変更詳細

`UPSTREAMS.md` の更新手順（最新buildへ昇格し、直前buildだけ残す）に従う。

| file | 変更 |
| --- | --- |
| `models/tool-wire-builds.json` | `6849` を昇格し `6720` を直前として残す。`6662` を落とす |
| `tests/fixtures/codex-tool-wire-6849.json` | 追加。契約に差分が見つからないため、shapeは6720と同一で `build` / `chatgpt_version` だけ更新 |
| `tests/fixtures/codex-tool-wire-6662.json` | 削除（参照がなくなるため） |
| `tests/test_toolbridge.py` | 既定fixtureと「最新+直前」の組を `("6849", "6720")` へ |
| `src/codex_openrouter/supervisor.py` | 起動後に `open -b <bundle id> <workspace>` でworkspaceを届ける。`--open-project` は古いbuildで有効なので残す |
| `tests/test_supervisor.py` | 上記のtest（exact引数・未指定時は送らない・retry後のfail-closed・bundle idはInfo.plistから） |

catalog中和は不要だった（新フィールドが無いため）。`supervisor.py` の変更は
tool契約ではなく、6849で壊れたworkspace受け渡しの修復である。

### workspace受け渡しの実装

- bundle idは値をハードコードせず `Contents/Info.plist` の `CFBundleIdentifier` から読む。
- appがprocessとして見える前にopenを投げるとLaunchServicesが2つ目のinstanceを
  起こしうるため、起動を確認してから送る。
- 上限 `WORKSPACE_DELIVERY_SECONDS` まで再試行し、届かなければ起動ごと止める。
  workspaceが届かないまま続けると、利用者から見て「dropしたfolderと違うprojectが
  開く」だけになるため、黙って劣化させない。

## 未決（実機gateの結果で決める）

6849は純正entryから `supports_parallel_tool_calls` を落としたが、repoは今も
OpenRouter entryへ付与している（`src/codex_openrouter/catalog.py` と
`models/registry.json`）。証拠なしに契約を変えないため、5-gate E2Eの
gate 5（parallel turn）の挙動を見てから判断する。

## Verification

```bash
PYTHONPATH=src python3 scripts/run_unit_tests.py      # 370 tests PASS
uvx ruff@0.16.3 check .                                # PASS
```

実機は `task/0820-deepseek-completion-plan.md` の Phase 2 / 3 に従う。
5-gate E2Eが通って初めて、6849のtool wireを実測で確認したことになる。
