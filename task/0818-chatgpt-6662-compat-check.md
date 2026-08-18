# 0818 ChatGPT.app build 6662 追従: clone中和の型安全化とテンプレートdrift検知

## Context

`/Applications/ChatGPT.app` が `26.803.61601` build `6396` → `26.810.52044` build `6662`
（codex-cli `0.147.0-alpha.6.5` → `0.148.0-alpha.9`）へ更新された。
v0.2.0 でASARパッチを撤去し build 非依存を設計目標にしているが、`catalog.build()` は
**native entry を clone して差し替える**方式のため、bundled catalog に増えたフィールドを
OpenRouter entry が黙って継ぐ。更新のたびにこの継承が無検査で通ることが構造的な弱点になっている。

本 task は (1) 今回の互換性チェック結果の記録、(2) 継承の中和を型安全にする、
(3) 次の app 更新でこの弱点が自動的に可視化されるようにする、の3点を扱う。

## 調査結果: 支障なし

ChatGPT.app を起動したまま、読み取り専用で確認した。機構の要はすべて健在。

| 観点 | 結果 |
| --- | --- |
| 署名 / 無改変 | `codesign --verify --deep --strict` OK、ASARに patch marker 無し |
| 実行ファイル | `Contents/MacOS/ChatGPT`、`Contents/Resources/codex` とも健在 |
| config key | codex binary に `model_catalog_json` `model_providers` `wire_api` `ModelProviderAuthInfo(command/timeout_ms/refresh_interval_ms)` すべて存在 |
| bundled catalog | 8件。`visibility=list` は 5件（sol / terra / luna / gpt-5.5 / gpt-5.2）で 6396 と同一集合 |
| clone テンプレート | `visibility=list` 先頭の `gpt-5.6-sol` が健在 |
| 前提モデル | `NATIVE_FALLBACK_MODEL = gpt-5.6-sol`、guard が前提にする ambient `gpt-5.6-luna` とも存在 |
| 再帰混入 | `--bundled` は `model_catalog_json` を無視する（composite が native として戻らない） |
| config 往復 | 現行 `~/.codex/config.toml` の activate → deactivate レンダリングがバイト単位で現状と一致 |
| composite 生成 | 新 build の bundled から生成 + `validate()` が通る（13件 / picker 10件） |
| doctor | 導入済み runtime で PASS |
| テスト | unittest 253件 PASS、synthetic E2E PASS |
| guard | codex binary の `/v1/models` は Ollama/OSS provider 専用。guard の GET 404 は無害 |

### schema 差分は1点だけ

native entry 8件すべてに `include_apps_usage_instructions: true` が新設された。
値の差分も全 entry でこれだけ。clone 経由で OR entry がこれを継ぐと、OR モデルの
instructions に「## Apps (Connectors)」節（`app://{connector_id}` の説明）が入る。

Apps(Connectors) は ChatGPT アカウント側の機能なので、`multi_agent_version` と同じ
「native だけが持つ能力フィールド」として中和する方針とした。

### 実装を左右する実測

既存の中和機構 `NATIVE_ONLY_FIELDS` は **値を `None` にする**タプルだった。ここへ素朴に
`include_apps_usage_instructions` を足すと、codex 0.148 が catalog 全体を拒否する:

```
Error: failed to parse model_catalog_json path `…`
       as JSON: invalid type: null, expected a boolean
```

`false` なら受理される（`codex debug models -c model_catalog_json=<candidate>` が rc=0、
OR 5件が `include_apps_usage_instructions=false` / `multi_agent_version=null` で往復）。
テンプレートの bool フィールドは現在9個あり、今後も同じ罠を踏み得る。
よって中和機構を「フィールド → 中和値」の対応に変え、native 側の型を保てる形にする。

## 変更詳細

### 1. `src/codex_openrouter/catalog.py` — 中和の型安全化

`NATIVE_ONLY_FIELDS` をタプルから `dict[str, Any]` へ変え、`RESET_FIELDS` と同じ
「フィールド → 値」の形に揃える。代入も同じ deep-copy 経路にする。
`include_apps_usage_instructions: False` を追加。

### 2. `src/codex_openrouter/catalog.py` — テンプレート drift 検知

build 6662 の clone テンプレートが持つ 36 フィールドを `KNOWN_TEMPLATE_FIELDS` として持ち、
`unknown_template_fields(natives)` が未知フィールドを返す。判定だけを行い、
中和するか継がせるかの方針は人間が決める。

### 3. `src/codex_openrouter/catalog.py` — 中和の契約化

`validate()` に「OpenRouter entry の `NATIVE_ONLY_FIELDS` が中和済みであること」を足す。
比較は JSON 表現で行う。`0 == False` を同一視すると bool の中和漏れを見逃すため。
`validate()` は `generate()` が原子的置換の直前に呼ぶので、契約を破った catalog は
そもそも書かれない。

### 4. `src/codex_openrouter/upgrade.py` — 中和を実際に効かせる

**計画時に見落としていた穴。** 中和規則を変えても、純正appのbuildが同じだと
`refresh_catalog_if_needed` は素通りするため、古い規則で組んだ catalog が使われ続ける。
`upgrade` の promotion に「旧 composite catalog を消す」replacement を足し、
次回起動で組み直させる。`settings.py` の profile 変更時と同じ `(None, target)` 削除規約。

これに伴い `settings._stale_catalogs` を `catalog.stale_paths()` へ移した（呼び出し側が
2箇所になったため）。settings 側の挙動は変えていない。

### 5. `src/codex_openrouter/doctor.py` — 利用者側の可視化

`check_catalog_template` を足す。導入済み build の clone テンプレートに未知フィールドが
無いことを見て、あれば **warn**。未知フィールドは即座に有害とは限らずレビューを促す
signal なので fail にはしない。`bundled_models()` の取得失敗も warn に倒し、
純正app不在は `check_stock` の担当なので二重に落とさない。

あわせて `Doctor` が `failures` と対称に `warnings` を記録するようにした
（従来は print するだけで、warn の内容をテストから表明できなかった）。

### 6. `tests/test_catalog.py` / `tests/test_doctor.py`

- fixture を build 6662 の形に更新
- `unknown_template_fields()` の単体テスト
- OR entry が `include_apps_usage_instructions is False`、かつ中和値が `None` に
  ならないこと（今回の障害クラスを直接ピン留めする）
- `InstalledBuildTests`（純正app不在の CI は skip）に2件
  - 実機 build のテンプレートに未知フィールドが無いこと
  - **生成した composite を実機 codex が受理すること**（一時 `CODEX_HOME` で
    `codex debug models -c model_catalog_json=<tmp>` が rc=0）。
    null/bool 不整合はこのテストだけが捕まえられる

## Verification

```bash
PYTHONPATH=src python3 -m unittest discover -s tests --buffer
python3 -m compileall -q src portable scripts
uvx ruff@0.16.3 check --select E9,F .

# 変更後のdoctorを実機stateへ当てる（読み取り専用）。
# `./codex-openrouter doctor` は**導入済み**の
# `~/.local/bin/codex-openrouter-doctor` へ委譲するので、変更は反映されない。
PYTHONPATH=src python3 -c "from pathlib import Path; \
from codex_openrouter.app import UserPaths; from codex_openrouter.doctor import run; \
raise SystemExit(run(UserPaths.current(), Path('models/registry.json')))"
```

実施結果:

- unittest 263件 PASS（+10件）、compileall OK
- ruff は既存の9件のみ。今回触ったファイルには1件も無い
  （`cli.py` の F821 は open PR #18 が直す対象）
- 変更後 doctor が実機 build 6662 に対して PASS し、
  `OK: cloneテンプレートは既知のフィールドだけで構成されています` が出る
- 既知集合から1件抜いて drift を再現すると、実機でも WARN のみで fail しない
- 変異テスト: `include_apps_usage_instructions` の中和値を `False` → `None` へ戻すと
  新テスト2件（型の表明 / 実機codexの受理）が落ちる。障害クラスを実際に捕まえている

### 実機E2E（未実施 / ChatGPT.app の終了が必要）

本 task は「ChatGPT.app を終了せずに」という条件下で実施したため以下は残タスク。

1. ChatGPT.app を通常のメニュー/⌘Q で終了
2. `codex-openrouter upgrade` で新 runtime を導入。このとき
   `~/.codex/model-catalogs/codex-openrouter.json`（と `.previous`）が消えること。
   `install-manifest.json` の `chatgpt_version` も 6662 へ更新される
   （現在 6396 のままだが機能への影響は無い）
3. Desktop の `Codex OpenRouter.app` から起動 → picker に native 5 + OR 5 の10件
4. catalog が組み直され、OR entry の `include_apps_usage_instructions` が `false`、
   `supervisor.json` の `version`/`build` が 6662 になっていること
5. OR モデルで1ターン流し、`guard.log` に `decision: forwarded` が残ること
   （Apps 節の中和後に応答が壊れていないことの確認を兼ねる）
6. `codex-openrouter doctor --runtime --secret-scan` が PASS
7. 終了後に純正 ChatGPT.app を単体起動し vanilla（catalog block 無し）に戻ること

## 補足

`task/0814-oss-public-release-plan.md` の引き継ぎ節が言及する `ruff.toml` / CI の
lint job / `task/0815-*.md` は **未マージの PR #18・#19 に存在する**。main には無い。
本 task は main から分岐しているため、これらとは独立にレビューできる。

## Status

- 2026-08-18: 調査完了（支障なし）。変更を実装し、リポジトリ側の Verification まで完了。
  実機E2E（上記7項目）は ChatGPT.app 終了が必要なため未実施。
