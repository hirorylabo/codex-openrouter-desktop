# codex-openrouter-desktop

> [!WARNING]
> **非公式・実験的なworkaroundです。OpenAIおよびOpenRouterの公認・提携製品ではありません。** 初版はApple Silicon macOS専用です。ChatGPT.appの更新で停止する可能性があり、無保証です。OpenAI、ChatGPT、Codex、OpenRouterおよび各モデル名は各権利者の商標です。

公式の署名済み`/Applications/ChatGPT.app`を**一切変更せず**、`~/.codex/config.toml`のmarker blockとローカルguardだけで、純正appのモデルピッカーにOpenRouterモデルを並べるCLIです。ASARパッチもcloneも作りません。`ChatGPT.app`、ASAR、API key、Cookie、履歴、userData、ログは配布物にもrepositoryにも含みません。

Desktopの`Codex OpenRouter.app`は小型の管理ランチャーです。開くと表示モデル数・既定モデル・workspaceを示す画面が出て、「OpenRouterで起動」を押したときだけ事前処理のうえ純正appが立ち上がり、pickerにnativeとOpenRouterが両方並びます。終了時にcatalogを外すので、`ChatGPT.app`を直接起動したときはvanillaのままです。

[English](./README.en.md)

## 対象と制限

- Apple Silicon macOSのみ。Windows、Linux、Intel Mac、Homebrewは未対応です。
- `v0.2.0`はprereleaseです。ASARパッチを撤去したため、**特定buildへの固定がなくなりました**。
- OpenRouterモデルを選んでいる間だけ`model_provider`が切り替わります。その間、appが自前で作る背景thread（ambient suggestions等、`gpt-5.6-luna`固定）もOpenRouter側に束縛されるため、guardが遮断します。遮断された背景機能はOpenRouter利用中だけ動きません（**巻き込み**）。
- thread途中でprovider境界をまたぐモデル変更はエラーになります。新しいthreadを立て直してください。
- OpenRouter API利用料は利用者負担です。`doctor --network`とcandidate検査でも少量の料金が発生する場合があります。

## 安全な取得

`curl | bash`は使用しません。GitHub CLIでReleaseを取得し、attestationとchecksumを確認します。

```bash
mkdir codex-openrouter-download && cd codex-openrouter-download
gh release download v0.2.0 \
  --repo hirorylabo/codex-openrouter-desktop \
  --pattern 'codex-openrouter-desktop-v0.2.0.tar.gz' \
  --pattern 'codex-openrouter-desktop-v0.2.0.spdx.json' \
  --pattern 'SHA256SUMS'
gh attestation verify codex-openrouter-desktop-v0.2.0.tar.gz \
  --repo hirorylabo/codex-openrouter-desktop
shasum -a 256 -c SHA256SUMS
tar -xzf codex-openrouter-desktop-v0.2.0.tar.gz
cd codex-openrouter-desktop-v0.2.0
```

sourceから使う場合も、Release archiveと同じallowlistを推奨します。

## 事前準備

1. 公式の署名済み`/Applications/ChatGPT.app`をインストールします。
2. Xcode Command Line Tools、Python 3.11以上、GitHub CLIを用意します。（v0.2.0でNode.js依存は撤去しました）
3. OpenRouterで次を設定します。
   - PrivacyでPrompt TrainingをOFFにし、無料公開endpointと1% data discountを使用しない。
   - Non-frontier ZDRをONにする。
   - Guardrailを作成し、使用profileのmodel IDだけをexact allowlistへ入れる。
   - OAuthで作成される最新の`codex-openrouter-desktop` keyへGuardrailを割り当てる。
   - API keyにspend limitを設定する。未設定はCLIが警告します。

既定profileのmodelは次の5件です。

- `deepseek/deepseek-v4-flash-0731`
- `deepseek/deepseek-v4-pro`
- `moonshotai/kimi-k3`
- `z-ai/glm-5.2`
- `minimax/minimax-m3`

## セットアップ

まずread-only checkを実行します。

```bash
./codex-openrouter check
```

標準はOpenRouter OAuth PKCEです。CLIは`127.0.0.1`のランダムportで一時callbackを待ち、S256 verifierをメモリ内だけに保持します。

```bash
./codex-openrouter setup --workspace "$HOME/Documents"
```

既存keyを使う場合は、echoなしのpaste方式を選べます。

```bash
./codex-openrouter setup --auth paste --workspace "$HOME/Documents"
```

keyはmacOS Keychainのservice `io.github.hirorylabo.codex-openrouter-desktop`へ保存されます。`--api-key`、`.env`、shell profile、config、logへ保存しません。実keyを取得するのはローカルguardだけです。Codexからguardへは起動ごとの別tokenを送り、実keyはloopbackを通りません。

## CLI

```text
codex-openrouter check
codex-openrouter setup [--workspace PATH] [--profile default|FILE] [--auth oauth|paste]
codex-openrouter launch [PATH]
codex-openrouter doctor [--network] [--runtime] [--secret-scan]
codex-openrouter migrate
codex-openrouter profile show --json
codex-openrouter profile apply --stdin-json
codex-openrouter guard-log [--clear]
codex-openrouter upgrade [--profile default|FILE] [--if-needed]
codex-openrouter rollback
codex-openrouter auth login|rotate|logout
```

セットアップ後は`$HOME/.local/bin`を`PATH`へ追加してください。

FinderでDesktopの「スタックを使用」がONの場合、launcherは「アプリケーション」stack内へ表示されます。直接見える位置へ置く場合はFinderの`表示 > スタックを使用`をOFFにしてください。launcherはproject固有icon、bundle署名、既定workspaceをsetup/upgradeごとに再生成します。

### 管理ランチャー

`Codex OpenRouter.app`はDockとAppメニューを持つ通常のmacOS appです。常駐はせず、管理画面を閉じるかOpenRouterセッションが終われば一緒に終了します。

- 開くと現在の表示モデル数・既定モデル・使用workspaceが出ます。
- 主ボタン「OpenRouterで起動」を押すまでChatGPTは起動しません。
- launcherへfolderをdropすると、そのfolderをworkspaceにした状態で管理画面が開きます。押すまで起動しないのは同じです。
- 副ボタン「モデル設定…」、Appメニューの「設定…」、`⌘,`のいずれからでもモデル設定画面が開きます。

### モデル設定画面

registry掲載の検証済みモデルをチェックボックスで出し入れし、選択済みモデルから既定モデルを1件指定します。任意slugの登録口はありません。

- 最低1モデルが必須です。既定モデルを外した場合は、新しい既定を明示選択するまで保存できません。
- 「OpenRouter Guardrailを開く」でGuardrail設定画面を開けます。
- 「検証して保存」はAPI keyの実効model集合との完全一致を確認してから保存します。不一致・ネットワーク失敗・Keychain失敗では**1バイトも変更しません**。
- 保存に成功すると「次回のOpenRouter起動から反映」と表示します。既定モデルは次の専用起動で一度だけ適用されます。
- OpenRouterモード稼働中は編集できません（「ChatGPT終了後に変更できます」と表示します）。
- 画面はAPI keyを取得も表示もしません。検証はPython CLIがKeychainから直接読み、値はUIへ渡りません。

### 純正appからOpenRouterモードへ切り替える

純正`ChatGPT.app`とOpenRouterモードは、同じapp・userData・`~/.codex/config.toml`を使うため同時起動しません。純正appの起動中に`Codex OpenRouter.app`をクリックすると、通常終了してOpenRouterモードで再起動するか確認します。

- 「OpenRouterで起動」を押した時点で確認し、押すまでは何も変更しません。
- 「キャンセル」では既存の純正appを前面へ戻し、configやguardを変更しません。
- 切り替える場合も通常終了だけを要求し、応答しないappを強制終了しません。
- CLIの`codex-openrouter launch`は確認UIを持たないため、純正appを終了してから実行してください。
- setup、upgrade、rollback、migrate、launchはユーザー単位で排他され、並行実行した2本目は共有状態を変更する前に停止します。

### クリック起動時の自動更新

リポジトリから導入した場合、導入元のpathが`install-manifest.json`へ記録されます。以降は`Codex OpenRouter.app`をクリックするたびに導入元と導入済みruntimeの内容ハッシュを比べ、**差分があるときだけ**自動でupgradeします（更新中は進行状況の小窓が出ます）。差分が無ければ何もせず起動します。

自動経路では実課金のAPI往復（`validate_key_and_profile`）を行いません。失敗しても起動は止まらず、`atomic_promote`のverifyが落ちれば直前の状態へ自動rollbackします。同じ内容で一度失敗したら、内容が変わるまで再試行しません。

手動で`codex-openrouter upgrade`を打つ場合は、**リポジトリの`./codex-openrouter`を使ってください。** `PATH`上の`codex-openrouter`は導入元を導入済みツリー自身へ解決するため、そのまま実行しても内容は新しくなりません（その場合は警告を表示します）。

## Profile

[`models/registry.json`](./models/registry.json)は、Codex metadataと実通信を検証したmodelだけを保持します。[`profiles/default.json`](./profiles/default.json)は表示model集合と既定modelを指定します。custom profileではregistry掲載modelの増減だけが可能で、任意slugは拒否されます。

```json
{
  "schema_version": 1,
  "name": "small",
  "models": ["minimax/minimax-m3"],
  "default_model": "minimax/minimax-m3"
}
```

API keyから見えるconcrete model集合がprofileと完全一致しない場合、導入を停止します。

正規化した導入済みprofileはruntime stateへ保存され、picker・guard・watcher・doctorが同じ集合を参照します。並び順の出所はregistryだけで、profile側の記述順やUIの操作順は結果に影響しません。通常upgradeとクリック時の自動upgradeはこのprofileを維持します。置き換えるのは明示的な`upgrade --profile default|FILE`とモデル設定画面の保存だけで、内容が変わった次の専用起動で`default_model`を一度だけ適用します。

モデル設定画面が使う更新窓口はCLIにもあります。Swift側はprofile・Keychain・Guardrailの判断を一切持たず、この2つを呼ぶだけです。

```bash
codex-openrouter profile show --json
printf '%s' '{"schema_version":1,"models":["minimax/minimax-m3"],"default_model":"minimax/minimax-m3"}' \
  | codex-openrouter profile apply --stdin-json
```

applyが受け付けるのは`schema_version`・`models`・`default_model`だけです。表示名・reasoning effort・並び順は変更できません。lifecycle lock内でregistry整合性とAPI keyの実効model集合を検証し、profile・supervisor state・install-manifest・旧catalogを単一transactionで置き換えます。検証に落ちれば全対象が元へ戻ります。同じ選択の再保存はno-opで、既定モデルの再適用をarmしません。

## 更新と移行

Codexは週2回以上更新されます。ASARパッチ方式はそのたびにanchor再解析とhash再生成が要り保守が破綻するため、v0.2.0で撤去しました。現在は起動のたびに`CFBundleShortVersionString`と`CFBundleVersion`を前回値と照合し、変わっていればcompositeカタログを組み直します（ASAR hashは取りません）。

v0.1.xからの移行:

```bash
codex-openrouter migrate
```

旧専用app`~/Applications/ChatGPT OpenRouter.app`を削除し、`[model_providers.openrouter]`を`~/.codex/config.toml`へ永続化し、旧`~/.codex-openrouter`を圧縮します。

圧縮では`sessions`（旧threadの記録）と`memories`/`goals`/`state`のsqliteを残し、ASARパッチ方式の`candidates`・旧clone appの`user-data`・`plugins`・logsを削除します。`--keep-all`で圧縮を抑止できます。実機では9.3GBが194MBになりました。

`[model_providers.openrouter]`は終了後も残ります。消すとOpenRouterで記録済みのthreadのresumeが`Model provider ... not found`でハードエラーになるためです。pickerからOpenRouterが消えるのはcatalog blockを外すからで、この2つは寿命が違います。

非稼働時のproviderは`127.0.0.1:0`と必ず失敗する認証commandを持つ非接続stubです。起動中だけguardのephemeral portと0600の起動tokenへ切り替え、通常終了ではstubへ戻してからguardを停止します。`SIGKILL`や電源断では次回の専用起動時にself-healします。その間も実API keyはloopbackへ送られませんが、同一ユーザー権限の悪性プロセスに対するpromptのローカル捕捉余地は残ります。

## 巻き込みの確認

```bash
codex-openrouter guard-log
```

guardが中継したmodelと遮断したmodelを集計します。遮断側に出るのが巻き込みです。Codexの更新で背景機能が増減するので、更新後にこれを見てください。現時点で判明しているのは`gpt-5.6-luna`（ambient suggestionsとその安全性分類）です。

guardは許可集合（[`models/registry.json`](./models/registry.json)の5モデル）以外を**1バイトも外へ出さずに**400で止めます。guard logにはmodel・判定・バイト数・時刻だけを記録し、本文と鍵は残しません。

## 価格表示

model名は固定し、説明欄で通常headline価格と稼働中ZDR endpointの項目別最安価格を分離表示します。価格は1M tokens当たりの参考値で、provider、routing、cache、割引後の実請求を保証しません。更新成功後24時間は再取得せず、失敗後は1時間backoffします。metadata契約変更時は自動追従せずfail-closedします。

## 認証の削除

```bash
codex-openrouter auth logout
```

明示確認後にローカルKeychain itemだけを削除します。OpenRouter側の失効は[Keys settings](https://openrouter.ai/settings/keys)で行ってください。Management API keyは要求しません。

## 開発

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src portable scripts
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py
python3 scripts/secret_scan.py --tree .
python3 scripts/build_release.py "v$(cat VERSION)" --dist /tmp/codex-openrouter-dist
```

実ChatGPT.appを使う手動検証は、隔離homeのE2Eを先に実行し、導入済みruntimeをupgradeした後でlauncherを2 cycle確認します。後者は各cycleでChatGPT.appを通常終了する対話操作を含みます。

```bash
PYTHONPATH=src python3 scripts/macos_live_e2e.py
scripts/macos_installed_e2e.zsh
```

セキュリティ報告は[`SECURITY.md`](./SECURITY.md)、第三者コードは[`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)を参照してください。
