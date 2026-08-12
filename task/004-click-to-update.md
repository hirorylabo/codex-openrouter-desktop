# 004: Desktop appのクリックだけで最新runtimeが載るようにする

作成日: 2026-08-12 / ブランチ: `codex/build-6396-adapter`

Status: **完了。自動検証PASS + 実機で全パス確認済み（v0.2.0として稼働中）**

## Context

`Codex OpenRouter.app` をクリックしても、ツール自身のruntime（`~/.local/share/codex-openrouter-desktop/current`）は古いまま起動する。反映には端末で `cd <repo> && ./codex-openrouter upgrade` が要る。

しかも間違えやすい。PATH上の `codex-openrouter` は隣に `src/` が無いため source root を導入済みツリーへ解決する。つまり `codex-openrouter upgrade` を素で打つと**自分自身を再インストールするだけ**で何も新しくならない。

判定方式は実測で裏を取った。`copy_support` が運ぶ対象の内容ハッシュは **29ファイル・5ms**、upgrade自体は約10秒（codesign --deep 1.9s ×2、build_icon 2.6s、swiftc 0.5s ×2 ほか）。10秒無表示は「無反応」と同じ体験になるので進行表示を併せて入れた。

## 変更詳細

### 判定（`upgrade.py`）

`copy_support` の対象名とignore集合を `SUPPORT_TREES` / `SUPPORT_FILES` / `IGNORED_PARTS` へ括り出し、**同じ定数**を使う `runtime_digest(root)` を追加。コピーする側と比較する側が同じ一覧を見ることが要点で、片方だけ増えると「変わったのに変わっていない」と判定される。相対path込みでsha256するので、追加・削除・改名も差として出る。

### source rootの記録（`manifest_document()`）

install-manifest を schema_version 4 にし、`source_root` と `source_digest` を追加。install と upgrade が同じ物を書くよう1関数にまとめた。**`source_root == support_root` のときは記録しない**（導入済みCLIから打つと自分自身が導入元になり、以後の自動更新が永久に「変化なし」と判定されるため）。同条件で `upgrade_command` が警告を出す。

### 自動upgrade（`auto_upgrade()` / `upgrade --if-needed`）

manifest の `source_root` を読み、ディレクトリでない・home配下でない・導入済みツリー自身なら何もしない。digestが一致しても何もしない。差分があるときだけ `upgrade(..., network_check=False)` を実行する。

- `upgrade()` に `network_check` を追加（`install()` と同じ形）。runtimeファイルの入れ替えに実課金のAPI往復は要らず、オフラインのクリックで失敗させたくない
- **失敗しても0を返す。** 起動は止めない。promotionのverifyが落ちれば自動rollbackが効く
- 失敗したdigestを `selfupdate.json` に記録し、内容が変わるまで再試行しない

### ランチャー（`codex-openrouter-app.zsh.in`）

`exec` の直前に `upgrade --if-needed` を挟み、`|| true` で起動を止めない。`exec` 側にも `PYTHONUNBUFFERED=1` を付けた。pipe越しだとblock bufferingされ、**セッション終了までログが0バイトのまま**で切り分けができなかった（今回の「無反応」調査でこれに当たった）。HUDへ行を届けるためにも必須。

走行中のファイルを置換するが、`atomic_promote` の切り替えは `os.replace` なので走行中のinodeは保持される。zshスクリプト自体も別inodeになるため安全。ただし `exec` 行の変更が効くのは次回クリックから。

### 進行表示（`CodexOpenRouterLauncher.swift`）

`waitUntilExit()` 後の `readDataToEndOfFile()` を `readabilityHandler` の逐次読みへ変更。`STATUS: updating` でHUD（spinner付きfloating window）を出し、`STATUS: launching` または純正ウィンドウの前面化成功で閉じる。出力がpipe bufferを超えたときに子がブロックする潜在的なデッドロックも同時に解消。純正appのポーリング上限は自動更新の分を見て60秒→180秒。

## Verification

- `PYTHONPATH=src python3 -m unittest discover -s tests` → **144件 PASS**（132 + 追加12件）
- `compileall` / `zsh -n` / `swiftc` / `secret_scan --tree --git-history` すべてPASS
- 実機ドライラン（`upgrade` をmock）: 現行manifestは schema 3 で `source_root` 未記録のため `auto_upgrade` は何もせず0を返す。digestは repo `401e3902` / installed `e03706c1` で正しく差分として出る

### 実機結果（2026-08-12）

初回反映は手動で1回だけ実施（自動更新のコード自体がまだ導入されていないため）。以降は `upgrade --if-needed` を実経路で回して確認した。

| 検証 | 結果 |
|---|---|
| 初回の手動 upgrade | `UPGRADE: PASS`、manifestが schema 4 + `source_root` 記録 |
| 差分なし | 0.5秒で素通し（出力なし） |
| 差分あり | `STATUS: updating` → `UPGRADE: PASS` → `STATUS: launching`、13.0秒 |
| 更新が失敗（`doctor.py` に構文エラーを注入） | promotion後のverifyで検出 → **自動rollback**、`exit=0` で起動は続行 |
| 失敗後の再実行 | 「同じ内容で失敗済みのためskip」0.13秒 |
| 失敗後の健全性 | installed digest不変（`401e3902`）、`doctor RESULT: PASS`（picker 10件） |
| 最終状態 | repo と installed のdigest一致、`selfupdate.json` は success |

設計の要である「更新に失敗しても起動は止まらない」を、実際に壊して確認した。Finderからのクリック操作そのもの（HUDの見た目と前面化）は未確認。

## スコープ外

PR #1レビューの指摘3〜6（install/upgradeの90行二重化、永続providerブロックと固定port 8791、`start_guard` の死んだnonce分岐、catalog blockの入れ子）。ランチャーのPythonが pyenv shim 経由で Finder 下では 3.9.6 に解決される件も別途。

> 2026-08-12追記: PR #1レビューの残件は[`task/005-pr1-integrated-lifecycle-refactor.md`](./005-pr1-integrated-lifecycle-refactor.md)で統合して解決した。FinderのPython解決は今回の5件には含めない。
