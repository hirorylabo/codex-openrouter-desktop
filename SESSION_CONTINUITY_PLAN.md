# Codex Unified Router: 統合picker・セッション継続計画（簡素版）

作成日: 2026-08-12 / 対象: ChatGPT `26.803.61601` build `6396`、`codex/build-6396-adapter`

状態: 実装着手GO。PROMOTE・v0.1.2公開はPhase 2完了後に再判定。

詳細版（初版・レビューloop記録込み）は `SESSION_CONTINUITY_PLAN.full.md`。本書はそれを実測で削ったもので、実装の正本は本書。

## 1. 決定

`Codex OpenRouter.app`をOpenRouter専用cloneから、純正nativeモデルとOpenRouter 5モデルを1つのpickerから選べる統合routerへ発展させる。ファイル名・ランチャーパス・入口は維持する。

- session/project正本: 純正と同じ `~/.codex`
- Electron userData: 純正と同じ `~/Library/Application Support/Codex`
- 純正 `/Applications/ChatGPT.app` は無変更のfallbackとして残す
- OpenRouter 5モデル: `deepseek/deepseek-v4-flash-0731`, `deepseek/deepseek-v4-pro`, `moonshotai/kimi-k3`, `z-ai/glm-5.2`, `minimax/minimax-m3`

## 2. 設計の中心: 非対称な危険への非対称な対策

危険なのは native request がOpenRouterへ流れる方向（ChatGPT promptとtokenの外部送信）だけで、逆方向は 404 が返るのみ。したがって**OpenRouter側だけを明示集合で閉じ、それ以外は`openai`**とする。両方向を対称に禁止しないことで、exact-N allowlist・per-build native契約・legacy thread救済・`turn/start` gateがすべて不要になる。

```
provider(model) = model ∈ OR5 ? "openrouter" : "openai"
```

この純関数を単一のApp Server request dispatcherに1箇所だけ注入する。

## 3. routing契約

| request | 処理 |
|---|---|
| `thread/list` | `modelProviders` 未指定なら `[]` を補う（空配列＝全provider。schema実測） |
| `thread/start` | `params.model` から `modelProvider` を決めて明示設定 |
| `thread/resume` | 同上。既存threadのmodelもこの関数を通す |
| `thread/fork` | 同上。Desktopは既に`model`と`config.model_reasoning_effort`を送っているので、`modelProvider`を足すだけで provider横断forkが成立する |
| `turn/start` | **何もしない** |

`turn/start`に手を入れない根拠: `TurnStartParams`に`modelProvider`が無く（installed buildのschemaで実測）、threadのproviderは start/resume/fork で確定済み。turnのmodelを別provider由来のslugに変えても、requestは元のproviderへ行き認証も切り替わらないため、誤routingも鍵の混線も起こらない。最悪でも不明modelエラー。Phase 2でcanary 1件だけ実測確認する。

既存threadのproviderは `session_meta.model_provider` に記録済み（rollout JSONLで実測）。したがって過去の `gpt-5.5` / `gpt-5.4` / `gpt-5.6` alias / `gpt-5.3-codex-spark` threadは、上記のelse規則でそのまま `openai` に解決され継続できる。

`desktop-model-providers.json` は `default_provider: "openai"`、`model_providers` にOR5だけを列挙する。

## 4. モデルカタログ

対象buildの `codex debug models --bundled` を正本とし、**bundled全件 + OR5 を追記**した composite を `~/.codex/model-catalogs/unified-router.json` へ原子的に生成する。nativeを抽出・選別しない。

- build 6396 のbundledは 8件（`gpt-5.6-sol/terra/luna`, `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.2` が `visibility: "list"`、`codex-auto-review` は `hide`）。picker想定は 7 + 5 = 12件。
- nativeのeffort・metadataはbundledのまま使い、repoに複製しない。build更新時は再生成するだけ。
- OR5は `models/registry.json` の既存契約とZDR endpoints検証を維持する。
- `profiles/*.json` は**OpenRouter専用のまま**。nativeをprofileに入れないことで、`validate_key_and_profile` の完全一致検査もdoctorのslug一致検査も無改造で通る。

## 5. shared configの扱い

`~/.codex/config.toml` は利用者の実設定（personality, notify, shell_environment_policy, projects, MCP, plugins）を保持しており、appも起動時に自分で書き換える。したがって全文レンダリングは使わず、**marker blockの追記/削除**だけで管理する。

- 先頭ブロック: `model_catalog_json`（top-level keyなのでtableより前に置く）
- 末尾ブロック: `[model_providers.openrouter]` と `[model_providers.openrouter.auth]`
- 各ブロックは `# >>> codex-unified-router` / `# <<< codex-unified-router` で囲む
- rollback = ブロック削除。preimage・全体hash・TOML mergeは不要
- top-level `model_provider` は設定しない
- 適用・削除はapp停止中に原子的置換で行い、冪等であること

## 6. 排他制御

`app.asar` に `requestSingleInstanceLock` / `second-instance` が存在するため、**userDataを共有した時点で2重起動はElectron標準機能が防ぐ**（2つ目は既存instanceへforwardして終了）。`~/.codex` の二重writerも同時に解決する。

専用lock file・`codex-stock-safe` launcher・process doctor matrixは実装しない。Phase 2で「純正を起動するとpatched側にforwardされる」ことを1回目視確認する。stale `SingletonLock` はChromium標準の回復に委ねる。

## 7. 認証と課金境界

- native: built-in `openai` provider + 既存ChatGPT sign-in。`model_providers.openai` は定義しない
- OpenRouter: `[model_providers.openrouter.auth]` のKeychain command helperのみ。`OPENROUTER_API_KEY` を環境・引数・config・logへ渡さない
- pickerとthread headerに provider prefix を表示する（label patch）
- 認証はprovider定義で分離されるため、鍵の混線は構造的に起きない。secret scanは維持する

## 8. セッション継続

- 同provider内: `thread/resume` + `turn/start` のmodel変更をそのまま許可
- provider横断: Desktopの既存fork操作でmodelを選ぶと、gateが`modelProvider`を付けて新threadを作る。`ThreadForkResponse.modelProvider` は required なので結果を検証できる。元threadは不変
- 専用の継続UI・命名規則・確認ダイアログは実装しない
- 旧 `~/.codex-openrouter` の移行はv1では行わない。読み取り専用backupとして保持する

## 9. 実装Phase

**Phase 1: patch と runtime**

1. `semantic_transform.mjs` のrouting injectionを、無条件`openrouter`固定からmodel mapping関数へ差し替える。対象は `thread/list` / `start` / `resume` / `fork` の4種
2. visibility patchは `authMethod` 反転をやめ、OR5を `additionalAvailableModels` 経由で出す形を先に試す。成立すればanchorは3→2になる。不成立ならprovider別述語（nativeは`availableModels`、customは`!hidden`）にする
3. composite catalog生成を `codex-openrouter-refresh` 側に追加する
4. config marker blockの適用・削除・冪等性を実装する
5. `render_provider_mapping` の `default_provider` と [patch_candidate.py:73](portable/patcher/patch_candidate.py:73) のOpenRouter専用guardを更新する（実質2箇所）
6. markerを `__codexUnifiedRouterBuild6396PatchV1` に更新する

**Phase 2: candidate実機検証（6件）**

1. pickerが 12件（native 7 + OR 5）で、providerが判別できる
2. `gpt-5.6-sol` で1タスク完了し、providerが `openai`
3. OR 1モデルで1タスク完了し、ZDR実providerを確認
4. 既存 `gpt-5.5` threadの resume が通る
5. ORモデルを指定したforkが成立し、元threadが不変
6. 再起動後もpicker選択・thread・projectが維持され、純正起動がpatched側へforwardされる

`turn/start` で別provider由来slugを指定した場合の挙動（エラーで止まり誤課金しない）を3の直後に1回だけ確認する。

**Phase 3: 昇格と公開**

1. 目視確認後に `PROMOTE`
2. 固定adapter化、patched hash再現性、rollback、再upgrade
3. README差分、CI、annotated tag、prerelease、asset検証

## 10. テスト

- semantic: routing / visibility / label の各anchorが0件・複数件なら停止
- unit: mapping純関数（OR5 → openrouter、native slug・未知slug・空 → openai）
- unit: config marker blockの追記・冪等・削除で元に戻ること
- synthetic: `thread/list` 全provider化、start/resume/forkへの`modelProvider`付与、fork時に元thread不変
- secret scan: 鍵がargs・config・log・userDataに残らない

## 11. 停止条件

- native modelのrequestがOpenRouterへ到達する
- OpenRouter keyがnative requestに、ChatGPT tokenがOpenRouter requestに載る
- 既存threadのresumeが失敗する、またはsession/projectがsidebarから欠落する
- config marker block削除で元のconfigに戻らない
- ASAR anchorが各1件でない、patched hashが再現しない
- doctor・secret scan・rollback・CIのいずれかが失敗する

停止時はPROMOTE・merge・tag・Releaseを行わず、candidateと秘密値除外済み診断を保持する。

## 12. 判定

- 実装着手: **GO**
- PROMOTE / v0.1.2公開: Phase 2の6件と`turn/start` canaryが全てPASSした時点で再判定

## 13. 実測で確認済みの前提

| 項目 | 実測値 |
|---|---|
| stock app | `26.803.61601` build `6396`、bundle ID `com.openai.codex` |
| userData | `~/Library/Application Support/Codex`、`SingletonLock` あり |
| single instance | `app.asar` に `requestSingleInstanceLock` / `second-instance` |
| shared config | top-level `model = "gpt-5.6-sol"`、`model_provider`・`model_catalog_json` 未設定 |
| bundled catalog | native 8件（`list` 7 + `hide` 1）。sol/terra = low..ultra、luna = low..max |
| App Server schema | `thread/start`・`resume`・`fork` は `model` + `modelProvider` を受ける。`turn/start` は `model` のみ。`ThreadForkResponse.modelProvider` は required。`thread/list` の `modelProviders` は空配列で全provider |
| Desktop fork実装 | `model` と `config.model_reasoning_effort` を送信、`modelProvider` は未送信 |
| session記録 | `session_meta.model_provider` にproviderが永続化されている |
| 現candidate | build 6396でanchor 3件が各1件一致、未昇格 |
