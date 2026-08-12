# v0.2.0

**ASARパッチ方式を撤去し、純正appを一切変更しない方式へ切り替えた破壊的変更です。**

Codexは週2回以上更新され、そのたびにsemantic anchorの再解析・patched hash再生成・ad-hoc署名・candidate昇格が必要でした。実際に運用中、ChatGPT.appが `26.803.41515` から `26.803.61601` へ更新された時点でランチャーが起動しなくなりました。この障害クラスを構造的に無くします。

## 何が変わったか

`Codex OpenRouter.app` はランチャー専用バンドルになりました。クリックすると事前処理のうえ純正 `/Applications/ChatGPT.app` を起動し、pickerにnativeとOpenRouterが両方並びます。終了時にcatalogを外すので、`ChatGPT.app` を直接起動したときはvanillaのままです。

- 正本が `~/.codex-openrouter` から純正appと共有の `~/.codex` へ移りました
- 専用cloneアプリは作りません。`~/Applications/ChatGPT OpenRouter.app` は不要です
- 特定buildへの固定がなくなりました。更新時はversion/buildの差分を見てcatalogを組み直すだけです

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

撤去したASARパッチ資産は archive tag `archive/asar-patch-003a0bc` に保全してあります。

非公式・無保証の実験版です。Apple Silicon macOS専用。
