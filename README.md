# codex-openrouter-desktop

> [!WARNING]
> **非公式・実験的なworkaroundです。OpenAIおよびOpenRouterの公認・提携製品ではありません。** 初版はApple Silicon macOS専用です。ChatGPT.appの更新で停止する可能性があり、無保証です。OpenAI、ChatGPT、Codex、OpenRouterおよび各モデル名は各権利者の商標です。

公式の署名済み`/Applications/ChatGPT.app`を**一切変更せず**、`~/.codex/config.toml`のmarker blockとローカルguardだけで、純正appのモデルピッカーにOpenRouterモデルを並べるCLIです。ASARパッチもcloneも作りません。`ChatGPT.app`、ASAR、API key、Cookie、履歴、userData、ログは配布物にもrepositoryにも含みません。

Desktopの`Codex OpenRouter.app`から起動すると、事前処理のうえ純正appが立ち上がり、pickerにnativeとOpenRouterが両方並びます。終了時にcatalogを外すので、`ChatGPT.app`を直接起動したときはvanillaのままです。

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

keyはmacOS Keychainのservice `io.github.hirorylabo.codex-openrouter-desktop`へ保存されます。`--api-key`、`.env`、shell profile、config、logへ保存しません。Codexからはcredential helperを使うcommand-backed authenticationで取得します。

## CLI

```text
codex-openrouter check
codex-openrouter setup [--workspace PATH] [--profile default|FILE] [--auth oauth|paste]
codex-openrouter launch [PATH]
codex-openrouter doctor [--network] [--runtime] [--secret-scan]
codex-openrouter migrate
codex-openrouter guard-log [--clear]
codex-openrouter upgrade [--profile default|FILE] [--if-needed]
codex-openrouter rollback
codex-openrouter auth login|rotate|logout
```

セットアップ後は`$HOME/.local/bin`を`PATH`へ追加してください。Desktopの`Codex OpenRouter.app`へfolderをdropして起動することもできます。

FinderでDesktopの「スタックを使用」がONの場合、launcherは「アプリケーション」stack内へ表示されます。直接見える位置へ置く場合はFinderの`表示 > スタックを使用`をOFFにしてください。launcherはproject固有icon、bundle署名、既定workspaceをsetup/upgradeごとに再生成します。

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

## 更新と移行

Codexは週2回以上更新されます。ASARパッチ方式はそのたびにanchor再解析とhash再生成が要り保守が破綻するため、v0.2.0で撤去しました。現在は起動のたびに`CFBundleShortVersionString`と`CFBundleVersion`を前回値と照合し、変わっていればcompositeカタログを組み直します（ASAR hashは取りません）。

v0.1.xからの移行:

```bash
codex-openrouter migrate
```

旧専用app`~/Applications/ChatGPT OpenRouter.app`を削除し、`[model_providers.openrouter]`を`~/.codex/config.toml`へ永続化し、旧`~/.codex-openrouter`を圧縮します。

圧縮では`sessions`（旧threadの記録）と`memories`/`goals`/`state`のsqliteを残し、ASARパッチ方式の`candidates`・旧clone appの`user-data`・`plugins`・logsを削除します。`--keep-all`で圧縮を抑止できます。実機では9.3GBが194MBになりました。

`[model_providers.openrouter]`は終了後も残ります。消すとOpenRouterで記録済みのthreadのresumeが`Model provider ... not found`でハードエラーになるためです。pickerからOpenRouterが消えるのはcatalog blockを外すからで、この2つは寿命が違います。

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
cd portable/patcher-js && npm ci --ignore-scripts && npm test
```

セキュリティ報告は[`SECURITY.md`](./SECURITY.md)、第三者コードは[`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)を参照してください。
