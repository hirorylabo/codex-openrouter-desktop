# 006 OpenRouterモデル管理UX（ランチャー管理画面 + profile CLI）

## Context

`task/PLAN (6).md` の実装。`Codex OpenRouter.app` を小型の管理ランチャーへ変更し、
純正pickerに出すモデル集合の編集口を1つだけ用意する。

前提となる設計判断（PLANより）:

- 純正モデルピッカーには実モデルだけを出す。「カスタム…」行は追加しない。
- 「モデル追加」は同梱の検証済みregistryからpickerへ出し入れする意味に限定する。任意slugは対象外。
- Swift側へprofile・Keychain・Guardrailロジックを複製しない。Python CLIを唯一の更新窓口にする。
- ランチャーは通常のmacOS app（Dock・Appメニュー・`⌘,`）にするが常駐daemonにはしない。

## 変更詳細

### 1. profileの正規化（`src/codex_openrouter/profile.py`）

- `models` を **registry順へ正規化**してから `ResolvedProfile` を作る。並び順はregistryが唯一の出所になり、
  UIからは変更できない（PLAN「並び順は変更不可」）。picker priorityもこの順で決まる。
- `parse_apply_payload()` / `resolve_apply_payload()` を追加。apply入力は
  `schema_version` / `models` / `default_model` の3keyだけを受け付け、それ以外のkeyは拒否する。
  表示名・reasoning effortはregistry由来のままで、入力経路を持たない。

### 2. 唯一の更新窓口（新規 `src/codex_openrouter/settings.py`）

- `show_document(paths, registry_path)`: 検証済みregistry全件・現在の選択・既定モデル・
  OpenRouterモード稼働中か・workspace・Guardrail URLをJSONで返す。秘密値は一切含めない。
- `apply_payload(paths, registry_path, raw_json)`: lifecycle lock内で
  1. installed registryとの整合性を検証
  2. 同一profileならno-op（既定モデル再適用をarmしない）
  3. Keychainからkeyを取得
  4. OpenRouter keyの実効モデル集合との完全一致を検証
  5. profile・supervisor state・install-manifest・旧catalog（`.previous` 含む）を
     `atomic_promote` の単一transactionでpromotion
  6. verify失敗時は全対象をrollback

### 3. CLI（`src/codex_openrouter/cli.py`）

- `codex-openrouter profile show --json`
- `codex-openrouter profile apply --stdin-json`

`--json` / `--stdin-json` は必須フラグにする。人間向け出力を後から足しても
Swift側のparserが壊れないようにするため。

### 4. catalogの再生成条件（`src/codex_openrouter/supervisor.py`）

- `State` に `catalog_profile_digest` を足し、schema versionを3へ上げる。
- `refresh_catalog_if_needed()` は version/build に加えて **profile digestの変化**でも再生成する。
  applyがcatalogを消すのが主経路だが、`upgrade --profile` など別経路でprofileが変わっても
  picker・guard・watcher・doctorが同じ集合を見る状態へ自己回復する。

### 5. ランチャー（`portable/launcher/app/*.swift`）

単一ファイルを責務ごとに分割し、`portable/launcher/app/` 配下をまとめてコンパイルする。

- `main.swift`: entry point（top-level codeは `main.swift` にしか置けない）
- `LauncherApp.swift`: NSApplicationDelegate、メニュー、純正appからのhandoff、進行HUD
- `LauncherPanel.swift`: 管理画面（表示モデル数・既定モデル・workspace・2ボタン）
- `ModelSettingsWindow.swift`: モデル設定画面
- `ProfileBridge.swift`: `profile show` / `profile apply` の呼び出しとJSON復号

UX:

- 起動時・folder drop時はどちらも管理画面を出すだけで、ChatGPTは起動しない。
- 「OpenRouterで起動」で従来の起動経路（純正app稼働中は確認 → 通常終了待ち → helper）に入る。
- 「モデル設定…」・Appメニュー・`⌘,` で設定画面。最低1モデル必須、既定モデルを外したら
  新しい既定を明示選択するまで保存不可。
- OpenRouterモード稼働中は編集を無効化し「ChatGPT終了後に変更できます」を表示する。
- 保存成功後は「次回のOpenRouter起動から反映」を表示する。
- `Info.plist` から `LSUIElement` を外し、Dockとメニューを持つ通常appにする。

## Verification

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src portable scripts
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py
python3 scripts/secret_scan.py --tree .
python3 scripts/build_release.py "v$(cat VERSION)" --dist /tmp/codex-openrouter-dist
xcrun swiftc portable/launcher/app/*.swift -o /tmp/CodexOpenRouterLauncher
```

受け入れ条件（PLAN）:

- 空集合、未知slug、重複、選択外default、壊れたJSON、余分なkeyをbyte-identicalで拒否する。
- Guardrailのmissing／extra、ネットワーク失敗、Keychain失敗ではprofile・state・manifest・catalogを変更しない。
- 成功時はdigestとmanifestが一致し、既定適用待ちが一度だけarmされる。
- promotion検証失敗時はprofile、state、manifest、旧catalogが一括復元される。
- 1モデル構成でpicker・guard・watcher・doctorが同じ1件だけを扱う。
- 設定変更後の次回起動でcatalogが再生成され、その後の再起動では有効なユーザー選択を維持する。

実機確認（対話）:

```bash
PYTHONPATH=src python3 scripts/macos_live_e2e.py
scripts/macos_installed_e2e.zsh
```

管理画面、`⌘,`、folder drop、純正appからのhandoff、設定中の秘密値非表示、
純正`ChatGPT.app`無改変を目視で確認する。

## Status

実装完了・レビュー1巡目まで反映済み（HEAD = `804325e`）。

通したもの: unittest 209件、compileall、synthetic E2E、secret scan（tree + archive）、
release build、`swiftc portable/launcher/app/*.swift`、`zsh -n` 3本、
一時ディレクトリでの `build_launcher`（plutil / icon / swiftc / codesign --verify）、
実state に対する読み取り専用の `profile show --json` と `doctor`（RESULT: PASS）。

レビュー1巡目で直したもの:

- SIGKILL後の`active`残骸で設定画面が永久に編集不可になる問題（`openrouter_is_running`）
- `⌘,`の再表示でチェックが巻き戻る問題（読み込み済みなら再読込しない）
- 読み込み失敗後にhandoffキャンセルで「モデル設定…」が有効へ戻る問題
- doctorへ install-manifest の profile digest 照合を追加

未実施: 実機の対話確認。`scripts/macos_installed_e2e.zsh` の
`manual_checklist` に列挙した6項目（管理画面・`⌘,`・folder drop・純正appからの
handoff・秘密値非表示・純正app無改変）と launcher 2 cycle は、導入済みruntimeを
`./codex-openrouter upgrade` で更新したうえで利用者が実行する。
