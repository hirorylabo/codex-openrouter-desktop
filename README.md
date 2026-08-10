# codex-openrouter-desktop

> [!WARNING]
> **非公式・実験的なworkaroundです。OpenAIおよびOpenRouterの公認・提携製品ではありません。** 初版はApple Silicon macOS専用です。ChatGPT.appの更新で停止する可能性があり、無保証です。OpenAI、ChatGPT、Codex、OpenRouterおよび各モデル名は各権利者の商標です。

公式の署名済み`/Applications/ChatGPT.app`を変更せず、利用者のMac内で専用clone、専用`CODEX_HOME`、専用Electron userDataを作り、OpenRouter custom providerへ接続するCLIです。`ChatGPT.app`、ASAR、API key、Cookie、履歴、userData、ログは配布物にもrepositoryにも含みません。

[English](./README.en.md)

## 対象と制限

- Apple Silicon macOSのみ。Windows、Linux、Intel Mac、Homebrewは未対応です。
- `v0.1.1`はprereleaseです。known buildはChatGPT `26.803.41515` build `6321`だけです。
- 未知buildのcandidate transformはbest-effortです。純正appへは書き込まず、目視承認なしに昇格しません。
- OpenRouter API利用料は利用者負担です。`doctor --network`とcandidate検査でも少量の料金が発生する場合があります。

## 安全な取得

`curl | bash`は使用しません。GitHub CLIでReleaseを取得し、attestationとchecksumを確認します。

```bash
mkdir codex-openrouter-download && cd codex-openrouter-download
gh release download v0.1.1 \
  --repo hirorylabo/codex-openrouter-desktop \
  --pattern 'codex-openrouter-desktop-v0.1.1.tar.gz' \
  --pattern 'codex-openrouter-desktop-v0.1.1.spdx.json' \
  --pattern 'SHA256SUMS'
gh attestation verify codex-openrouter-desktop-v0.1.1.tar.gz \
  --repo hirorylabo/codex-openrouter-desktop
shasum -a 256 -c SHA256SUMS
tar -xzf codex-openrouter-desktop-v0.1.1.tar.gz
cd codex-openrouter-desktop-v0.1.1
```

sourceから使う場合も、Release archiveと同じallowlistを推奨します。

## 事前準備

1. 公式の署名済み`/Applications/ChatGPT.app`をインストールします。
2. Xcode Command Line Tools、Python 3.11以上、Node.js/npm、GitHub CLIを用意します。
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
codex-openrouter update
codex-openrouter upgrade [--profile default|FILE]
codex-openrouter rollback
codex-openrouter auth login|rotate|logout
```

セットアップ後は`$HOME/.local/bin`を`PATH`へ追加してください。Desktopの`Codex OpenRouter.app`へfolderをdropして起動することもできます。

FinderでDesktopの「スタックを使用」がONの場合、launcherは「アプリケーション」stack内へ表示されます。直接見える位置へ置く場合はFinderの`表示 > スタックを使用`をOFFにしてください。launcherはproject固有icon、bundle署名、既定workspaceをsetup/upgradeごとに再生成します。

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

API keyから見えるconcrete model集合がprofileと完全一致しない場合、専用appへ書き込む前に停止します。

## 更新と未知build

```bash
codex-openrouter update
```

`update`は、known buildなら`upgrade`、未知buildならcandidate作成へ進みます。release archiveを更新した後、専用appを通常終了して次を実行すると、runtime・設定・known adapter appをstagingで検査してから切り替えます。

```bash
./codex-openrouter upgrade
```

upgradeは現行targetを削除せずbackupへ保持し、切替後doctorが失敗した場合は全targetを自動rollbackします。成功後も`codex-openrouter rollback`で直前のruntime一式へ戻せます。known buildはRelease同梱の[`adapters/index.json`](./adapters/index.json)でversion、build、stock/patched ASAR hash、markerを固定します。rebuildとinstallerはactive adapterとindexの完全一致からpatcherを解決し、build固有の値をruntime scriptへ重複させません。

未知buildでは純正appを変更せずcandidate cloneだけを作ります。lockfile固定のJS parserがrouting、model visibility、label fallbackのsemantic anchorを各1件だけ認識した場合に限りpatchします。

Candidateは署名、ASAR integrity、App Server model list、全model／公開effortの`provider.zdr=true` canary、実providerが稼働中ZDR endpointであることを検査します。その後、利用者がモデルピッカーとタスク開始を目視確認し、`PROMOTE`と入力した場合だけ昇格します。昇格後doctorが失敗すれば旧appと設定へ自動rollbackし、失敗candidateと秘密値除外済み診断bundleを保持します。診断情報は自動送信しません。

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
