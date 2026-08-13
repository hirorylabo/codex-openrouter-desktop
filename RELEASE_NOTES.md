# v0.2.0

**ASARパッチ方式を撤去し、純正appを一切変更しない方式へ切り替えた破壊的変更です。**

Codexは週2回以上更新され、そのたびにsemantic anchorの再解析・patched hash再生成・ad-hoc署名・candidate昇格が必要でした。実際に運用中、ChatGPT.appが `26.803.41515` から `26.803.61601` へ更新された時点でランチャーが起動しなくなりました。この障害クラスを構造的に無くします。

## 何が変わったか

`Codex OpenRouter.app` はランチャー専用バンドルになりました。管理画面の「OpenRouterで起動」を押すと事前処理のうえ純正 `/Applications/ChatGPT.app` を起動し、pickerにnativeとOpenRouterが両方並びます。終了時にcatalogを外すので、`ChatGPT.app` を直接起動したときはvanillaのままです。

- 正本が `~/.codex-openrouter` から純正appと共有の `~/.codex` へ移りました
- 専用cloneアプリは作りません。`~/Applications/ChatGPT OpenRouter.app` は不要です
- 特定buildへの固定がなくなりました。更新時はversion/buildの差分を見てcatalogを組み直すだけです

## モデル管理

`Codex OpenRouter.app` は小型の管理ランチャーになりました。開くと表示モデル数・既定モデル・使用workspaceが出て、**「OpenRouterで起動」を押すまでChatGPTは起動しません**。folderをdropした場合もworkspaceが変わるだけです。DockとAppメニューを持つ通常のmacOS appですが常駐はせず、画面を閉じるかOpenRouterセッションが終われば一緒に終了します。

「モデル設定…」・Appメニューの「設定…」・`⌘,` から、OpenRouterが配信しているモデルの一覧を開けます。任意slugの登録口は作っていません。純正pickerに「カスタム…」のような偽の行も足しません。

一覧はtool calling対応モデルだけを候補にし、価格（IN/OUT $/M）・公開日・7dトークン利用量・バッジを列で出します。公開日／利用量／入力価格／出力価格／名前で並べ替え、検索と`ZDRのみ`（既定ON）・`学習なしのみ`・`無料のみ`・`reasoningのみ`で絞り込めます。選択済みは常に先頭へ固定され、絞り込みでも消えません。

利用量は**接続数ではなくトークン総数**です。OpenRouterが公開しているのがそれだけで、しかも日次トップ50モデルに限られます。圏外は`—`と表示します（0ではありません）。

- 最低1モデルが必須です。既定モデルを外したら、新しい既定を明示選択するまで保存できません
- 「検証して保存」は選択中のmodelがAPI keyで呼び出せることを確認します。呼び出せないmodelがある・ネットワーク失敗・Keychain失敗では**1バイトも変更しません**
- 保存に成功すると「次回のOpenRouter起動から反映」と表示し、次の専用起動で既定モデルを一度だけ適用します
- OpenRouterモード稼働中は編集できません
- 画面はAPI keyを取得も表示もしません

Swift側にprofile・Keychain・Guardrailの判断は置いていません。更新窓口はCLIの3コマンドだけです。

```bash
codex-openrouter profile show --json
codex-openrouter models list --json
printf '%s' '{"schema_version":1,"models":["minimax/minimax-m3"],"default_model":"minimax/minimax-m3"}' \
  | codex-openrouter profile apply --stdin-json
```

applyはlifecycle lock内で registry整合性 → Keychain → 選択modelの呼び出し可否 の順に検証し、profile・supervisor state・install-manifest・導入済みregistry・旧catalogを単一transactionで置き換えます。promotion後の検証に落ちれば全対象が一括で戻ります。同じ選択の再保存はno-opで、既定モデルの再適用をarmしません。

## Guardrailを任意にしました

以前はOpenRouter Guardrailにexact allowlistを組み、API keyの実効model集合とprofileの**完全一致**を要求していました。モデルを足すたびにGuardrailの編集が要るため、追加体験を悪くしていました。

検証するのは「選択中のmodelがこのkeyで呼べるか」だけになりました。pickerに出るmodelが必ず呼べることは変わらず、model引退・rename・制限付きkeyも今までどおり検出します。失うのは「鍵が漏れても課金は選択中のmodelまで」という上限で、**spend limitがその役割を引き継ぎます**。Guardrailを併用しても動作します。

## ZDR強制をモデル単位にしました

guardは全リクエストへ無条件に `provider.zdr` を立てていました。ZDR稼働endpointを持たないモデルではこれが必ず失敗を招くため、実測でOpenRouterの無料モデル15件は**1件も呼び出せない**状態でした。

registryの `zdr_supported` を見て、ZDRで動くモデルにだけ立てるようにしました。ZDRなしのモデルを追加するときは確認シートが出て、追加後は管理画面に橙色で常時表示し、`doctor --network` が毎回WARNで報告し、純正pickerの説明文にも「ZDRなし」と入ります。**他のモデルのZDR強制は外れません。**

## そのほか

- 同梱registryのfallback価格が陳腐化していたため実測値へ更新しました（`deepseek-v4-pro` は 0.435→1.168、`z-ai/glm-5.2` は 0.07→0.50）
- 参照されていなかった `negative_model` 設定を削除しました
- cache価格やZDR価格を公開しないモデルを1件選んだだけで、**全モデルの価格がfallbackへ落ちる**不具合を直しました

## 起動時の自動更新

リポジトリから導入した場合、導入元のpathが `install-manifest.json` に記録されます。以降は「OpenRouterで起動」を押すたびに導入元と導入済みruntimeの内容ハッシュを比べ、**差分があるときだけ**自動でupgradeします。差分が無ければ何もせず起動します（実測0.5秒）。管理画面を開いただけでは更新しません。更新が要るときは進行状況の小窓が出て、実測13秒で反映されます。

- 自動経路では実課金のAPI往復を行いません
- **更新に失敗しても起動は止まりません。** `atomic_promote` のverifyが落ちれば直前の状態へ自動rollbackします。同じ内容で一度失敗したら、内容が変わるまで再試行しません
- 手動で `codex-openrouter upgrade` を打つ場合は、**リポジトリの `./codex-openrouter` を使ってください。** `PATH` 上の `codex-openrouter` は導入元を導入済みツリー自身へ解決するため、そのまま実行しても内容は新しくなりません（警告を表示します）

リリース版導入やリポジトリを削除した環境では、記録された導入元が無いので何もせず素通しします。

## 移行

```bash
codex-openrouter migrate
```

旧clone appを削除し、`[model_providers.openrouter]` を `~/.codex/config.toml` へ永続化し、旧homeを圧縮します。圧縮では `sessions` と memories/goals/state のsqliteを残し、ASARパッチ方式の `candidates` や旧clone appの `user-data` を削除します（`--keep-all` で抑止可）。

`[model_providers.openrouter]` は終了後も残ります。消すとOpenRouterで記録済みのthreadのresumeが `Model provider ... not found` でハードエラーになるためです。

## 受け入れる制限

- OpenRouterモデルを選んでいる間だけ `model_provider` が切り替わります。その間、appが自前で作る背景thread（ambient suggestions等、`gpt-5.6-luna` 固定）もOpenRouter側に束縛されるため、guardが遮断します。遮断された背景機能はOpenRouter利用中だけ動きません
- thread途中でprovider境界をまたぐモデル変更はエラーになります。新しいthreadを立て直してください

guardは許可集合以外を**1バイトも外へ出さずに**止めます。`codex-openrouter guard-log` で遮断されたmodelを確認できます。

## 保守性

- **Node.js/npm依存を全廃**しました。CIからnpm・semantic patcher tests・pinned source auditの3ステップが消えています
- テンプレートに埋まっていた1366行（doctor 595行 / refresh 771行）を `src/` の実モジュールへ移し、CIから直接unittestできるようにしました
- 初回インストールと更新を1本の経路へ統合しました（`portable/install.sh` は撤去）
- `copy_support`（運ぶ側）と `runtime_digest`（比べる側）が同じ定数を見るようにし、片方だけ対象が増えて検出漏れになる事故をテストで塞ぎました
- ランチャーが表示するlog pathを Info.plist 経由にし、Pythonの `UserPaths` を唯一の出所にしました

撤去したASARパッチ資産は archive tag `archive/asar-patch-003a0bc` に保全してあります。

非公式・無保証の実験版です。Apple Silicon macOS専用。
