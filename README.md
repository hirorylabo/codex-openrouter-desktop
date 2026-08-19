# codex-openrouter-desktop

> [!WARNING]
> **非公式・実験的なworkaroundです。OpenAIおよびOpenRouterの公認・提携製品ではありません。** 初版はApple Silicon macOS専用です。ChatGPT.appの更新で停止する可能性があり、無保証です。OpenAI、ChatGPT、Codex、OpenRouterおよび各モデル名は各権利者の商標です。

[![CI](https://github.com/hirorylabo/codex-openrouter-desktop/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/hirorylabo/codex-openrouter-desktop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Latest release](https://img.shields.io/github/v/release/hirorylabo/codex-openrouter-desktop?include_prereleases&sort=semver)](https://github.com/hirorylabo/codex-openrouter-desktop/releases)

公式の署名済み`/Applications/ChatGPT.app`を**一切変更せず**、`~/.codex/config.toml`のmarker blockとローカルguardだけで、純正appのモデルピッカーにOpenRouterモデルを並べるCLIです。ASARパッチもcloneも作りません。`ChatGPT.app`、ASAR、API key、Cookie、履歴、userData、ログは配布物にもrepositoryにも含みません。

Desktopの`Codex OpenRouter.app`は小型の管理ランチャーです。開くと表示モデル数・既定モデル・workspaceを示す画面が出て、「OpenRouterで起動」を押したときだけ事前処理のうえ純正appが立ち上がり、pickerにnativeとOpenRouterが両方並びます。終了時にcatalogを外すので、`ChatGPT.app`を直接起動したときはvanillaのままです。

[English](./README.en.md)

## 対象と制限

- Apple Silicon macOSのみ。Windows、Linux、Intel Mac、Homebrewは未対応です。
- `v0.2.0`はprereleaseです。ASAR/catalogは特定buildへ固定しません。tool wireだけは壊れたcallを実行しないため、実機canary済みの**最新版＋直前build**を互換保証します。未知buildではOpenRouter起動だけを止め、純正appは通常利用できます。
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
   - **API keyにspend limitを設定する。** 未設定はCLIが警告します。
   - （任意）Guardrailで使用modelを絞る。v0.2.0からは必須ではありません。

> **Guardrailを必須にしなくなった経緯**
> 以前はGuardrailにexact allowlistを組み、API keyの実効model集合とprofileの
> **完全一致**を要求していました。モデル追加のたびにGuardrailを編集する必要があり、
> 追加体験を悪くしていたためやめました。
>
> いま検証するのは「選択中のmodelがこのkeyで呼べるか」だけです。pickerに出る
> modelが必ず呼べることは変わらず、model引退・rename・制限付きkeyも今までどおり
> 検出します。失うのは「鍵が漏れても課金は選択中のmodelまで」という上限で、
> **spend limitがその役割を引き継ぎます**。Guardrailを併用しても動作します。

既定profileは`deepseek/deepseek-v4-flash-0731`のみで、既定reasoning effortは
`high`です。他のOpenRouterモデルは設定画面から追加できます。

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
codex-openrouter models list --json [--refresh]
codex-openrouter models verify-tools --stdin-json
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

OpenRouterが配信しているモデルの一覧から選びます。任意slugの入力口はありません。

一覧にはtool非対応を含むOpenRouterモデルを残し、`Codex tool / provider`列で互換状態・検証providerを明示します。検証時刻とprovider試行番号はtooltipで確認できます。ほかの列は価格（IN/OUT $/M）・公開日・7dトークン利用量・バッジです。

- `verified`: Tool Bridge経由でstructured functionとfreeform toolの両方を実測済み
- `partial`: structured functionは成功、freeform toolは非互換
- `declared`: OpenRouterが`tools`対応を公称、未実測
- `unknown`: metadataを解釈不能
- `unsupported`: `tools`非対応、またはdirect function callを実測できない

- 並び替え: モデル名・入力価格・出力価格・公開日・7dトークン利用量の列名をクリック。同じ列を再クリックすると降順 / 昇順が切り替わります。
- 絞り込み: モデル名検索、`ZDRのみ`（既定ON）、`学習なしのみ`、`無料のみ`、`reasoningのみ`、`tool非対応も表示（N件）`
- 選択済みは常に先頭へ固定され、絞り込みでも消えません。

**トークン利用量について。** OpenRouterが公開しているのは接続数ではなく**トークン総数**で、しかも日次トップ50モデルに限られます。圏外のモデルは`—`と表示します（0ではありません）。取得には`/api/v1/datasets/rankings-daily`を使い、1日1回だけ取得してキャッシュします。

- 最低1モデルが必須です。既定モデルを外した場合は、新しい既定を明示選択するまで保存できません。
- 「OpenRouter Guardrailを開く」でGuardrail設定画面を開けます（任意）。
- 「検証して保存」は選択中のmodelがAPI keyで呼び出せることを確認します。未検査の新規モデルは、少額課金の確認後に実運用と同じTool Bridge wireでstructured/freeformのcanaryを実行します。認証失敗、429、5xx、通信失敗では非対応と決めつけず、profileもtool cacheも変更しません。
- `partial` / `unsupported`も選択できますが、`exec`・browser・search・`apply_patch`が動かない可能性を表示し、exact model IDの明示承認後だけ保存します。
- 保存に成功すると「次回のOpenRouter起動から反映」と表示します。既定モデルは次の専用起動で一度だけ適用されます。
- OpenRouterモード稼働中は編集できません（「ChatGPT終了後に変更できます」と表示します）。
- 画面はAPI keyを取得も表示もしません。検証はPython CLIがKeychainから直接読み、値はUIへ渡りません。

#### ZDRなしのモデルを追加する場合

ZDR強制はモデル単位です。ZDR稼働endpointを持つモデルには従来どおり`provider.zdr`を強制し、持たないモデルには立てません。以前は全リクエストへ無条件に立てていたため、ZDR endpointのないモデル（無料モデルを含む）は**1件も呼び出せませんでした**。

ZDRなしのモデルを選ぶと確認シートが出ます。追加した場合は次のとおりです。

- 管理画面に「ZDRなしのモデルを N件使用中です」と橙色で常時表示します。
- `codex-openrouter doctor --network`が毎回WARNで報告します。
- 純正pickerの説明文にも「ZDRなし（送信内容がproviderに保持される可能性あり）」と入ります。
- **他のモデルのZDR強制は外れません。**

### 純正appからOpenRouterモードへ切り替える

純正`ChatGPT.app`とOpenRouterモードは、同じapp・userData・`~/.codex/config.toml`を使うため同時起動しません。純正appの起動中に管理画面の「OpenRouterで起動」を押すと、通常終了してOpenRouterモードで再起動するか確認します。

- 「OpenRouterで起動」を押した時点で確認し、押すまでは何も変更しません。
- 「キャンセル」では既存の純正appを前面へ戻し、configやguardを変更しません。
- 切り替える場合も通常終了だけを要求し、応答しないappを強制終了しません。
- CLIの`codex-openrouter launch`は確認UIを持たないため、純正appを終了してから実行してください。
- setup、upgrade、rollback、migrate、launchはユーザー単位で排他され、並行実行した2本目は共有状態を変更する前に停止します。

### 起動時の自動更新

リポジトリから導入した場合、導入元のpathが`install-manifest.json`へ記録されます。以降は「OpenRouterで起動」を押すたびに導入元と導入済みruntimeの内容ハッシュを比べ、**差分があるときだけ**自動でupgradeします（更新中は進行状況の小窓が出ます）。差分が無ければ何もせず起動します。管理画面を開いただけでは更新しません。

自動経路では実課金のAPI往復（`validate_key_and_profile`）を行いません。失敗しても起動は止まらず、`atomic_promote`のverifyが落ちれば直前の状態へ自動rollbackします。同じ内容で一度失敗したら、内容が変わるまで再試行しません。

手動で`codex-openrouter upgrade`を打つ場合は、**リポジトリの`./codex-openrouter`を使ってください。** `PATH`上の`codex-openrouter`は導入元を導入済みツリー自身へ解決するため、そのまま実行しても内容は新しくなりません（その場合は警告を表示します）。

旧multi-provider開発版からのupgradeでは、OpenRouterの追加modelを維持したまま、廃止provider固有のregistry entryとmanaged provider設定を原子的に除きます。廃止providerのKeychain itemは読み出しも削除もしません。

## Profile

[`models/registry.json`](./models/registry.json)は同梱の初期registryです。設定画面でmodelを足すと、選択分を実体化した導入済みregistryが`~/.local/share/codex-openrouter-desktop/state/registry.json`へ書かれ、以降はそちらが正本になります。[`profiles/default.json`](./profiles/default.json)は表示model集合と既定modelを指定します。custom profileでは正本registry掲載modelの増減だけが可能で、任意slugは拒否されます。

```json
{
  "schema_version": 1,
  "name": "small",
  "models": ["minimax/minimax-m3"],
  "default_model": "minimax/minimax-m3"
}
```

選択中のmodelがAPI keyで呼び出せない場合、導入を停止します。keyが余分なmodelを見せていることは問題にしません（Guardrailは任意）。

正規化した導入済みprofileはruntime stateへ保存され、picker・guard・watcher・doctorが同じ集合を参照します。並び順の出所はregistryだけで、profile側の記述順やUIの操作順は結果に影響しません。通常upgradeと起動時の自動upgradeはこのprofileを維持します。置き換えるのは明示的な`upgrade --profile default|FILE`とモデル設定画面の保存だけで、内容が変わった次の専用起動で`default_model`を一度だけ適用します。

モデル設定画面が使う更新窓口はCLIにもあります。Swift側はprofile・Keychain・Guardrailの判断を一切持たず、次の4つを呼ぶだけです。

```bash
codex-openrouter profile show --json
codex-openrouter models list --json
printf '%s' '{"schema_version":1,"models":["deepseek/deepseek-v4-flash-0731"]}' \
  | codex-openrouter models verify-tools --stdin-json
printf '%s' '{"schema_version":1,"models":["minimax/minimax-m3"],"default_model":"minimax/minimax-m3"}' \
  | codex-openrouter profile apply --stdin-json
```

`profile show`はネットワークに触らず、導入済みの選択だけを即座に返します。候補一覧は`models list`が返し、こちらはOpenRouter APIを引いて1日キャッシュします（`--refresh`でTTLを無視）。取得に失敗してもキャッシュがあれば止まりません。

applyはこの3項目に加え、optionalな`tool_risk_acknowledged`（exact model ID配列）を受け付けます。schemaは互換追加なので1のままです。表示名・reasoning effort・並び順は変更できません。tool検査結果は`state/tool-compatibility.json`へmodel ID・ChatGPT build・tool契約versionをkeyに24時間cacheし、取得できた場合だけ検証providerと試行番号も保存します。lifecycle lock内でregistry整合性と「選択中のmodelがkeyで呼び出せること」を検証し、profile・supervisor state・install-manifest・旧catalogを単一transactionで置き換えます。検証に落ちれば全対象が元へ戻ります。

OpenRouter modelのcomposite catalogは`tool_mode: "direct"`、`node_repl_disabled: true`、`supports_search_tool: false`、`experimental_supported_tools: []`を明示します。[OpenAIのmodel guidance](https://developers.openai.com/api/docs/guides/latest-model)に沿ってGPT-5.6専用のCode Mode／hosted searchを継承せず、通常のdirect tool callとして評価するためです。native modelのcatalog entryは変更しません。

### 最小Tool Bridge

guard内のprotocol処理は[`src/codex_openrouter/toolbridge.py`](./src/codex_openrouter/toolbridge.py)へ分離しています。通常functionはそのまま通し、namespace childとcustom toolだけをrequest内で一意なstrict functionへ変換します。`apply_patch`のstring fieldは`patch`、それ以外のcustomは`input`です。OpenRouterのSSEは`call_id`・`item_id`・`output_index`を保ったまま元のnamespace/custom eventへ復元します。未知tool名、不完全JSON、欠落delta/done、途中切断は推測修復せずfail-closedです。

toolを含むrequestでは価格sortを指定せず、既定の[Auto Exacto](https://openrouter.ai/docs/guides/routing/auto-exacto)を利用します。併せて`X-OpenRouter-Metadata: enabled`を送り、最終chunkからprovider・試行番号・候補数・statusだけをguard logへ残します。`openrouter_metadata`本体、pipeline、prompt、tool argumentsはCodexにもlogにも残しません。cache hitや認証・rate limit・5xxでmetadataが無いことはtool非対応判定に使いません。

`codex-relay`はruntime依存ではありません。参照commit、参照file hash、採用・不採用の境界は[`UPSTREAMS.md`](./UPSTREAMS.md)に固定し、週次CIは差分を報告するだけで自動mergeしません。

同梱registryに無いmodelを選ぶと、`models list`のエントリから導入済みregistry（`~/.local/share/codex-openrouter-desktop/state/registry.json`）を実体化し、**同じtransactionへ載せます**。片方だけ進むと「選んだmodelがregistryに無い」状態で次の起動に入るためです。registryのエントリはライブAPIから毎回導出し直し、同梱registryが持つ日本語の説明文だけを残します。

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
PYTHONPATH=src python3 scripts/run_unit_tests.py
PYTHONPATH=src python3 -m compileall -q src portable scripts
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py
uvx ruff@0.16.3 check .
xcrun swiftc portable/launcher/app/*.swift -o /tmp/CodexOpenRouterLauncher
xcrun swiftc -parse-as-library \
  portable/launcher/app/ProfileBridge.swift \
  portable/tests/DecoderCompatTests.swift -o /tmp/decoder-compat && /tmp/decoder-compat
python3 scripts/secret_scan.py --tree .
python3 scripts/check_upstreams.py --validate-only
python3 scripts/build_release.py "v$(cat VERSION)" --dist /tmp/codex-openrouter-dist
```

`compileall`は構文だけを確認します。未定義名（`F821`）を止めるのは`ruff`です。
unit test runnerはloopback以外のsocket接続を遮断し、差し替え漏れによる実通信を失敗にします。
CLIのJSON fieldを増減したら`tests/fixtures/launcher-*.json`とdecoder compatを同時に更新してください。

実ChatGPT.appを使う手動検証は、隔離homeのE2Eを先に実行し、導入済みruntimeをupgradeした後でlauncherを2 cycle確認します。後者は各cycleでChatGPT.appを通常終了する対話操作を含みます。

```bash
PYTHONPATH=src python3 scripts/macos_live_e2e.py
scripts/macos_installed_e2e.zsh
```

セキュリティ報告は[`SECURITY.md`](./SECURITY.md)、第三者コードは[`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)を参照してください。
