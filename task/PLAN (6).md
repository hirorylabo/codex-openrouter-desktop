# OpenRouterモデル管理UX・実装計画

## 方針

- `Codex OpenRouter.app`を小型の管理ランチャーに変更する。
- 純正モデルピッカーには実モデルだけを表示する。「カスタム…」という偽モデル行は追加しない。
- 「モデル追加」は、同梱された検証済みregistryからpickerへ追加・削除する意味とする。任意slug登録は対象外。
- macOS標準に合わせ、設定画面はランチャー内の「モデル設定…」、Appメニュー、`⌘,`から開く。[Apple Settings HIG](https://developer.apple.com/design/human-interface-guidelines/settings)

## UX

- ランチャー起動時に以下を表示する。
  - 現在の表示モデル数と既定モデル
  - 主ボタン「OpenRouterで起動」
  - 副ボタン「モデル設定…」
  - 使用workspace
- folder drop時はworkspaceを更新して画面を開き、起動ボタンを押すまでChatGPTを開始しない。
- 設定画面には検証済みモデルをチェックボックスで並べ、選択済みモデルから既定モデルを1件指定する。
- 最低1モデルを必須とし、既定モデルを外した場合は新しい既定を明示選択するまで保存不可にする。
- 「OpenRouter Guardrailを開く」と「検証して保存」を設置する。
- OpenRouterモード稼働中は編集を無効化し、「ChatGPT終了後に変更できます」と表示する。
- 保存成功後は「次回のOpenRouter起動から反映」と表示する。次の専用起動だけ新しい既定モデルを適用する。

## 更新インターフェース

- Swift側へprofile・Keychain・Guardrailロジックを複製せず、Python CLIを唯一の更新窓口にする。
- 次のCLIを追加する。
  - `codex-openrouter profile show --json`
  - `codex-openrouter profile apply --stdin-json`
- apply入力は`schema_version`、`models`、`default_model`だけとし、表示名・reasoning effort・並び順は変更不可にする。
- applyはlifecycle lock内で以下を実行する。
  1. installed registryとの整合性を検証
  2. Keychainからkeyを取得
  3. OpenRouter keyの実効モデル集合との完全一致を検証
  4. profile、supervisor state、manifest、古いcatalogの除去を単一transactionでpromotion
  5. 失敗時は全対象をrollback
- 同一profileの再保存はno-opとし、既定モデルの再適用をarmしない。
- profile変更後は古いcatalogを残さず、次回起動で選択モデルだけのcatalogを再生成する。
- ランチャーは通常のmacOS appとしてDockとAppメニューを持つが、常駐daemonにはしない。画面またはOpenRouterセッションが終了すればlauncherも終了する。

## 採用しない入口

- 純正picker末尾の「カスタム…」行：選択actionを受け取れず、実モデルとしてconfig・watcher・guardへ流れるため不採用。
- 設定専用app：1クリック起動は維持できるが、Desktop上のappと責務が分裂するため不採用。
- Option起動：発見性が低いため補助入口にも使用しない。
- メニューバー常駐：現在の単純なライフサイクルに対して過剰なため不採用。

## テストと受け入れ条件

- 空集合、未知slug、重複、選択外default、壊れたJSONをbyte-identicalで拒否する。
- Guardrailのmissing／extra、ネットワーク失敗、Keychain失敗ではprofile・state・manifest・catalogを変更しない。
- 成功時はdigestとmanifestが一致し、既定適用待ちが一度だけarmされる。
- promotion検証失敗時はprofile、state、manifest、旧catalogが一括復元される。
- 1モデル構成でpicker・guard・watcher・doctorが同じ1件だけを扱う。
- 設定変更後の次回起動でcatalogが再生成され、その後の再起動では有効なユーザー選択を維持する。
- Swift compile、CLI unit、全unit、compileall、secret scan、release build、synthetic E2E、実launcher 2 cycleを通す。
- 実機では、管理画面、`⌘,`、folder drop、純正appからのhandoff、設定中の秘密値非表示、純正`ChatGPT.app`無改変を確認する。
