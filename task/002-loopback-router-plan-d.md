# 002: 案D 実装 + ASAR資産の撤去と保守性向上

作成日: 2026-08-12 / ブランチ: `codex/build-6396-adapter` / 対象: ChatGPT `26.803.61601` build `6396`

Status: **完了（Phase 1〜4）**

| Phase | 内容 | 結果 |
|---|---|---|
| 1 | archive tag / `configblock.py` / `catalog.py` / `UserPaths` 拡張 | 完了 |
| 2 | `guard.py` / `watcher.py` | 完了 |
| 3 | `supervisor.py` / ASAR資産撤去 / `upgrade.py`・`cli.py` 書き換え / CIからNode除去 | 完了 |
| 4 | 実機E2E 21件 / README / v0.2.0 | 完了 |

- archive tag `archive/asar-patch-003a0bc` を push 済み（撤去した資産のSHAを保全）
- unit 105 test PASS、secret scan（tree + git history）PASS、compileall PASS
- 実機E2E 21/21 PASS（`scripts/macos_live_e2e.py`）

実機E2Eで確認できた核心:

- picker に native 5 + OR 5 が並ぶ（ASARパッチ無し）
- **nativeのturnはguardに着弾しない**。threadは `openai` に束縛される
- watcherが `model_provider` を追随させる
- **provider境界をまたいだnative turn（`gpt-5.6-sol`）をguardが遮断した**。threadがopenrouter束縛でturnがnative slugという、案Dで最も危険な経路を実トラフィックで止めたことを確認
- **巻き込んだ背景thread（`gpt-5.6-luna`）もguardが遮断した**
- 終了後に catalog block が消え純正起動はvanilla、provider block は残る

### 残課題

- guard の **中継（forward）経路** は unit test と Phase 0-C の実測で確認済みだが、実機E2Eでは
  ダミー鍵を使っているため OpenRouter への実往復は未実施。課金が発生するため意図的に外した
- `configblock.edit` には理論上の lost update 窓が残る。純正appは自前lockを取らないため。
  実運用条件では計測上ゼロ、両者が同一マイクロ秒で開始する人工条件でのみ再現。
  失っても routing は安全側のままで誤送信にはならない

関連: [LOOPBACK_ROUTER_PLAN.md](../LOOPBACK_ROUTER_PLAN.md) §10（案Dの実測根拠）、[SESSION_CONTINUITY_PLAN.md](../SESSION_CONTINUITY_PLAN.md)（案A・退避経路）

## Context

現行は純正 ChatGPT.app の ASAR を semantic patch した clone を `~/Applications` に置き、専用 `~/.codex-openrouter` で動かしている。Codex は週2回以上更新され、そのたびに anchor 再解析・patched hash 再生成・ad-hoc署名・candidate昇格が要るため保守が破綻する。

代替の loopback proxy 方式（案B）は Phase 0 で不成立が確定した。`openai_base_url` は ChatGPT 認証下の native トラフィックも捕捉するため native を自前中継せざるを得ず、その中継が Cloudflare Bot Management に TLS fingerprint で遮断される（codex本体=rustls は 101、python/curl/node は一律 403）。回避には fingerprint 模倣が必要で、週2回より速く予測不能な追従を抱え込むうえ bot 対策の迂回そのものになるため採らない。

そこで **案D** を採る。native を一切中継せず、純正 app と `~/.codex` を共有したまま、OpenRouter モデルを選んだときだけ `model_provider` を切り替える。Phase 0-C の実機検証で成立を確認済み。

到達点: Desktop の `Codex OpenRouter.app` をクリックすると事前処理が走り、純正 app の picker に native + OpenRouter が両方並ぶ。純正 ChatGPT.app を直接起動したときは vanilla のまま。

## 実測で確定している前提（再調査不要）

| 事実 | 値 |
|---|---|
| composite catalog は ASAR パッチ無しで通る | picker に native 5 + OR 5 = 10件。`[OR] ` prefix で判別可 |
| bundled catalog の内訳 | `list` 5件 + `hide` 3件（旧記載の「list 7」は誤り） |
| catalog エントリに provider/base_url 相当は無い | 振り分けは transport 層のみ |
| provider の束縛時点 | **`thread/start` 時。thread の生存期間中は不変** |
| app が config へ書くもの | `model` と `model_reasoning_effort` のみ。`model_provider` は書かない |
| thread 生成の時点 | **最初の送信時**。選択→送信の間が丸ごとレース窓（実測 49秒） |
| thread内モデル変更 | 新 thread を作らない。同一 thread のまま |
| 安全側の失敗 | thread=openai + OR slug → chatgpt.com が HTTP 400。外部送信なし |
| 巻き込み | `gpt-5.6-luna` は app.asar にハードコードされ ambient suggestions 等が自前 thread を作る。provider 反転中はそれも openrouter に束縛され、**利用者本文を含む 43KB** が飛ぶ |
| 巻き込みは catalog で止まらない | OR entry の `multi_agent_version` を null にしても再現。config.toml にキーも無い |
| `wire_api` | `chat` は廃止。`responses` のみ |

**巻き込みは許容する**（利用者判断）。ただし guard は任意ではなく必須。

## 変更詳細

### アーキテクチャ

純正 `/Applications/ChatGPT.app` は一切変更しない。`Codex OpenRouter.app` はランチャー専用バンドル。

```
クリック
[1] self-heal   前回の残骸（marker・孤児プロセス・退避した model）を掃除
[2] 排他        純正app が既に起動中なら中止（config は起動時読み込みのため後入れ不可）
[3] update追従  version/build が前回と違えば composite catalog を再生成 → 契約検証
[4] port確保    空きポートを取り、guard の base_url を決める
[5] config      marker block を挿入（surgical・原子的・冪等）
[6] 常駐起動    guard と watcher を起動し health を nonce で確認
[7] app起動     純正 ChatGPT.app を起動し、終了まで待つ
[8] 後始末      guard/watcher 停止 → marker の一部を除去して vanilla に戻す
```

### config.toml の3ブロック（寿命が違う）

`~/.codex/config.toml` は純正 app が起動中に自分で書き換える。**全文レンダリングは使わない。** marker block による外科的な追記/削除のみ。

| ブロック | 内容 | 寿命 |
|---|---|---|
| A: catalog | `model_catalog_json` | ランチャー実行中のみ。終了時に**除去** |
| B: provider | `[model_providers.openrouter]` + `.auth` | **永続。除去しない** |
| C: 選択状態 | `model` / `model_provider` の巻き戻し | 終了時に native 既定へ戻す |

B を永続にするのは、OR 記録済み thread の resume で provider 定義が無いと `Model provider \`openrouter\` not found` になるため。A を外せば picker から OR は消えるので vanilla 体験と両立する。

C が要るのは、OR モデル選択中に終了すると `model` が OR slug のまま残るため。終了時に退避して native 既定へ戻し、次回ランチャー起動時に復元する。

### fail-safe

guard の base_url は localhost なので listener が居なければ connection refused。watcher が死ねば provider が反転せず OR slug は chatgpt.com が 400 で弾く。**どちらの死に方でも本文は外に出ない。** ポート横取りのみ [6] の nonce 付き health で検出し起動を中止する。

### 新規モジュール（すべて `src/codex_openrouter/`）

| ファイル | 責務 |
|---|---|
| `paths.py` | テンプレート各所に散っているパス定数を集約 |
| `configblock.py` | marker block A/B/C の挿入・除去・冪等性 |
| `catalog.py` | composite catalog 生成と契約検証。clone テンプレートは「`visibility: list` の最初の native entry」。native 専用フィールド（`multi_agent_version` 等）を中和 |
| `guard.py` | OpenRouter 専用ローカル guard。allowlist・SSE中継・Keychain・拒否ログ |
| `watcher.py` | `model` → `model_provider` 追随 |
| `supervisor.py` | ライフサイクル [1]〜[8] |

### 撤去（archive tag 後に削除）

- `portable/patcher/`、`portable/patcher-js/`（**Node/npm 依存が消える**）
- `adapters/index.json` と `app.py::load_adapter` / hash pinning
- `src/codex_openrouter/promotion.py`、`src/codex_openrouter/candidate.py`
- `portable/manifest.json`、`scripts/audit_upstream.py`
- `portable/templates/codex-openrouter-rebuild.zsh.in`
- `portable/render_runtime.py` の config.toml 全文レンダリング
- `profile.py::render_provider_mapping` と `desktop-model-providers.json`
- `tests/test_candidate_patcher.py`
- CI の「Semantic patcher tests」「Pinned source and license audit」「npm ci」
- `THIRD_PARTY_NOTICES.md` の vendored patcher 記述

### 保守性向上

1. `codex-openrouter-refresh.py.in` 771行 と `codex-openrouter-doctor.py.in` 595行、計 1366行 が `@@VAR@@` 置換テンプレートに埋まっており、テストは一度レンダリングして `spec_from_file_location` で読む形（`tests/test_refresh.py:28-31`）。ロジックを src の実モジュールへ移し `.in` は薄い shim にする
2. Node ツールチェーン全廃。CI が Python + Swift のみになる
3. パス定数の集約
4. doctor 再構成。`check_stock_and_clone` を削除、`check_config_and_catalog` / `check_network` / `check_secret_scan` を retarget、guard 疎通・watcher 生存・marker 整合を追加

### 移行 `codex-openrouter migrate`

1. 旧 clone `~/Applications/ChatGPT OpenRouter.app` を削除
2. 新ランチャー `Codex OpenRouter.app` を設置
3. B ブロックを `~/.codex/config.toml` に永続挿入
4. 旧 `~/.codex-openrouter` は読み取り専用で保存（旧 thread の記録があるため削除しない）

旧 home の thread の取り込みは今回のスコープ外。

## Phase

- **Phase 1**: archive tag → `configblock.py` / `catalog.py` 新設 → `UserPaths` 拡張
- **Phase 2**: `guard.py` → `watcher.py`
- **Phase 3**: `supervisor.py` → Swift ランチャーのターゲット変更 → `migrate` → **ASAR資産の撤去と `upgrade.py` / `cli.py` の書き換え** → CI から Node 除去 → refresh/doctor のロジックを src へ移設
- **Phase 4**: 実機検証 → README 差分 → v0.2.0（破壊的変更）

### 実装中に判明した順序の訂正

計画では Phase 1 で ASAR 資産を削除する予定だったが、依存を追ったところ単純な削除ではないことが分かったため Phase 3 へ移した。

- `promotion.py::atomic_promote` は **ASAR専用ではなく汎用の原子的ファイル置換**（`(source, target)` の列 + backup + verify）で、`upgrade.py` がツール自身のruntimeファイル更新に使っている。**撤去対象から外す**（計画の誤り）
- `candidate.py` を消すと `cli.py` の `update` / `rollback` / `upgrade` と `upgrade.py` が壊れる。`upgrade.py` は `adapters/index.json`・`portable/manifest.json`（vendored patcherのfetch）・clone bundle・`render_runtime.py` の全文レンダリングに依存しており、371行の書き換えが要る
- その書き換え先（新しいinstall/launch経路）は Phase 3 の `supervisor.py` とランチャー変更で初めて確定する。先に消すと2フェーズにわたって `update`/`upgrade` が代替なしで壊れた状態になる

archive tag は push 済みなので、先送りしても資産は失われない。

## Verification

### unit
- catalog: 契約検証が native欠落・OR不足・slug重複・effort不一致で落ちる。clone テンプレートが `visibility: list` の最初の native entry を選ぶ
- configblock: A/B/C の挿入→冪等→除去で元に戻る。app が書いた他セクションを壊さない
- watcher: OR slug → openrouter、native slug → openai、未知 slug → openai
- guard: OR 5モデルは通し、native slug と未知 slug は**送信せずに**拒否する

### 実機 E2E（Phase 0-C で確立した手順）
実 auth を複製した隔離 `CODEX_HOME` と専用 user-data-dir で純正 app を起動し、`--remote-debugging-port` 経由の CDP で renderer を操作する。accessibility / screen recording は不要。検証後に複製 auth を削除。

1. picker に native 5 + OR 5 = 10件が並ぶ
2. OR モデルで 1 タスク完了し ZDR 実 provider を確認
3. native モデルで 1 タスク完了し **guard に着弾しない**
4. 既存 thread の resume が通る
5. thread 途中で provider 境界をまたぐモデル変更がエラーになり **guard が本文を外に出さない**
6. 終了後に A ブロックが消え純正起動が vanilla になる。B ブロックは残る
7. ランチャー強制終了後、次回起動の self-heal が残骸を掃除する
8. update追従: 記録済み build を人為的にずらして catalog が再生成される

### その他
- 巻き込みスキャン: guard が拒否した model を一覧する CLI。Codex 更新のたびに増減を追う。既知は `gpt-5.6-luna`
- secret scan: 鍵が argv・config・log・userData・guard ログに残らない。`scripts/secret_scan.py` の対象に guard ログを追加

## 停止条件

- native の request が guard に着弾する
- guard が許可外 model の本文を1バイトでも外へ出す
- 既存 thread の resume または sidebar 表示が壊れる
- 終了後に純正起動が vanilla に戻らない
- `[model_providers.openrouter]` が除去され resume がハードエラーになる
- doctor・secret scan・CI のいずれかが失敗する

## 未決

- 巻き込みで壊れる背景機能の見せ方。現方針は「guard のログのみ、UI には出さない」
- OR モデル選択中の強制終了で C ブロックの復元に失敗した場合。self-heal で native 既定へ倒す想定
