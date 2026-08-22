# 0822 codex-router trial ハンドオフ

作成日: 2026-08-22 / 対象: `main` / Status: **trial 稼働中・日常運用可**

詳細は [`0822-codex-router-trial.md`](./0822-codex-router-trial.md)。本書は再開用の要約。

## いま何が動いているか

`duolahypercho/codex-router` を日常運用に導入済み。純正 ChatGPT.app（`26.818.21641` / build `6849`）
の picker に native GPT-5.6 系と外部モデルが並んでいる。**自作実装のコードは無変更**、install も
idle のまま保全（`supervisor.json` の `active: false`）。

| 項目 | 値 |
| --- | --- |
| checkout | `~/.local/share/codex-router`（`bin/update` で更新。**wrapper 経由推奨**） |
| provider | `openrouter`（key は `~/.codex/codex-router/openrouter-api-key.secret`）、`grok-oauth` |
| picker | DeepSeek V4 Flash / V4 Pro / Kimi K3（OpenRouter）+ Grok 4.5 / 4.6（OAuth）+ native |
| failover | **off**（計測の純度確保のため。戻すのは `control failover on`） |
| vision bridge | on（`grok-oauth/grok-4.6` 経由。Grok quota を消費する） |

## 触ったファイル

repo 内は **`task/` の note だけ**。`src/` は無変更。

| 場所 | 内容 |
| --- | --- |
| `~/.local/share/codex-router/src/api-forwarder.mjs` | local patch（OpenRouter provider profile） |
| `~/.local/share/codex-router/.git/hooks/post-merge` | patch 自動再適用 |
| `~/.local/share/codex-openrouter-trial/patches/apply-openrouter-provider-profiles.py` | 冪等な適用スクリプト |
| `~/.local/bin/codex-router-update` | update wrapper |
| `~/.codex/codex-router/user-models.json` | curated 3件のメタデータ |
| `~/.codex/config.toml` | codex-router の marker block 3つ + `[desktop]` に1行 |
| `~/.local/share/codex-openrouter-trial/2026-08-22/` | 退避（repo 外） |

## 更新のしかた

```bash
codex-router-update
~/.local/share/codex-router/bin/control service restart
```

`bin/update` を直接叩いても post-merge hook が patch を当て直すが、dirty tree を理由に拒否される
ため wrapper 経由が正しい。**ChatGPT.app の更新では patch は壊れない**（native catalog が
自動再取得されるだけ）。

実機検証済み: `47d67626` → `b01cf559` の更新で `api-forwarder.mjs` 自体が変更されたが、
アンカー方式の挿入により patch は生存し、挿入位置は 799 行 → 818 行へ移動した。

## provider 選好の切り替え

`user-models.json` の `requestProfile` を書き換えて `bin/control apply`。

| profile | 送る `provider` |
| --- | --- |
| `openrouter-zdr-floor`（現在） | `zdr, data_collection:"deny", sort:"price"` |
| `openrouter-floor` | `sort:"price"` |
| `openrouter-zdr-strict` | 上 + `require_parameters:true`（**404 リスク**。切り分け用） |

いずれも `parallel_tool_calls` を削除し、`tool_choice` の強制を `auto` へ降格する。

## 戻し方

```bash
~/.local/share/codex-router/bin/disable                 # managed block 除去 + service 撤去
diff ~/.local/share/codex-openrouter-trial/2026-08-22/config.toml ~/.codex/config.toml
./codex-openrouter launch                               # 自作実装へ復帰
```

marker 外に `model_catalog_json` / `openai_base_url` が残っていると自作 launcher が
`configblock.py:169-176` で fail-closed する。`diff` で確認してから戻すこと。

## 次にやること

| # | 内容 | 状態 |
| --- | --- | --- |
| 1 | **gate 5**: DeepSeek V4 Flash で `apply_patch` を通す | **PASS**（実機2回 + probe 6/6）。追記3 |
| 2 | service を止めて native GPT が死ぬことの確認 | **PASS**。停止中は native が `waiting for network` で無限リトライ。追記3 |
| 3 | 次の ChatGPT.app 更新で catalog 自動再取得が効くか | 次回更新待ち（app は `6849` のまま） |
| 4 | patch を upstream へ PR | 未着手。**再スコープ後の block で出す**（追記3）。通れば patch/hook/wrapper が全て不要になる |
| 5 | `enabled-reasoning-efforts` の件を openai/codex へ報告 | 未着手（#33805 等に既報あり） |
| 6 | main の 5 commit を push | **完了** |

残っているのは 3・4・5 の 3 件のみ。3 は待ちで、4 と 5 は upstream への報告。

### gate 5 の結果

**通った。** 実機 2回（ChatGPT.app + DeepSeek V4 Flash が `apply_patch` でファイルを編集）と、
app を挟まない freeform probe 6/6（3モデル × stream/非stream）。`openrouter-zdr-strict` への
切り替えは不要だった。詳細は trial note の追記3。

gate の試行を投げるときは**補足なしの一文にする**。実測で、こちらの報告文が app のプロンプトへ
混入したターンはモデルが「読み取りのみ」に倒れ、17リクエスト / 15分かかった（クリーンな試行は
8リクエスト / 31秒）。

## 再検証しなくてよいこと

| 事項 | 結論 |
| --- | --- |
| OpenRouter の provider 選好を API key 単位で分ける | **不可**。per-request か account 全体のみ |
| tool 互換 provider への絞り込み | **OpenRouter 側で自動**。`require_parameters` は不要かつ 404 リスク |
| effort に `max` が出ない | **OpenAI 側のバグ**。`[desktop]` の `enabled-reasoning-efforts` で解決済み |
| `ultra` を使う案 | 不要になった。なお OpenRouter は `ultra` を受け付けない |
| 自作実装の PR #24 | draft のまま保持。`codex/openrouter-tool-bridge` @ `381c660` |
| `bin/test-model` で gate 5 を測れるか | **測れない**。probe は plain JSON function + `stream:false` で freeform 経路を踏まない |
| LiteLLM の custom→function bridge | **実在し経路に入る**。`{content:string}` へ落として grammar を description へ畳む |
| DeepSeek / Kimi は `tool_choice:"required"` を拒否するか | **拒否しない**（3モデル × thinking 有無で 6/6 履行）。既定 profile から downgrade を外した |
| service の停止・起動コマンド | **`bin/control service stop` / `bin/control service start`**。`bin/start` は foreground 実行で LaunchAgent に載らない |
| `bin/test-model` の FAIL 表示 | `detail` は `response.ok` だけで決まるので当てにならない。`--json` の `ok` を見る |
| routed request が遅い理由 | 1リクエスト約 521KB。tool 191件のうち **MCP が 153件**。router 注入分は約6% |
