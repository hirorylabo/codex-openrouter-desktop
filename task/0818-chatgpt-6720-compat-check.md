# 0818 ChatGPT.app build 6720 追従: node_repl 2フィールドの中和とテンプレートsnapshot

## Context

`/Applications/ChatGPT.app` が `26.810.52044` build `6662` → `26.814.41407` build `6720`
（codex-cli `0.148.0-alpha.9` → `0.148.0-alpha.15`）へ更新された。

**機能面の支障は無い。** 一方で `task/0818-chatgpt-6662-compat-check.md` で入れた
cloneテンプレートのdrift検知が設計どおり発火した。本taskはその発火への追従
（新フィールド2件の方針決定）と、次回更新で「値の差分」まで機械的に取れるようにする
snapshotの追加を扱う。

## 調査結果: 支障なし

app停止中に読み取り専用で確認した。機構の要はすべて健在。

| 観点 | 結果 |
| --- | --- |
| 署名 / 無改変 | `codesign --verify --deep --strict` rc=0、ASARにpatch marker 0件 |
| 実行ファイル | `Contents/MacOS/ChatGPT`・`Contents/Resources/codex` とも健在 |
| config key | `model_catalog_json` `model_providers` `wire_api` `ModelProviderAuthInfo` `timeout_ms` `refresh_interval_ms` `env_key` `base_url` すべてbinaryに存在 |
| bundled catalog | 8件。`visibility=list` は5件（sol / terra / luna / gpt-5.5 / gpt-5.2）で6662と同一集合 |
| cloneテンプレート | 先頭 `visibility=list` は `gpt-5.6-sol` のまま。フィールド 36 → **38** |
| 前提モデル | `NATIVE_FALLBACK_MODEL = gpt-5.6-sol`、guardが前提にするambient `gpt-5.6-luna` とも健在 |
| 再帰混入 | `--bundled` は `model_catalog_json` を無視する（sentinel注入で確認） |
| config往復 | `~/.codex/config.toml` はtomllibでparse可。catalog blockのinsert→removeがバイト単位で一致 |
| composite生成 | `build()` + `validate()` が通る（13件 / picker 10件） |
| 実機codexの受理 | `codex debug models -c model_catalog_json=<composite>` rc=0、OR 5件が `visibility=list` で往復 |
| doctor | PASS（WARN 2件: composite未生成 + 未知フィールド） |
| テスト | 263件中 262 PASS / 1 FAIL。FAILはdrift検知そのもの |

### 6662 → 6720 のbundled catalog完全diff

- 追加: `node_repl_disabled`, `node_repl_auto_review_required` — native 8件すべてに追加、値は全件 `false`
- 削除: 無し
- 既存フィールドの値変更: 8件すべてで0件（`base_instructions` も `tool_mode` も不変）

`tool_mode: code_mode_only` は6662時点で既にテンプレートに入っていた（今回の新規ではない）。

なお `task/0818-chatgpt-6662-compat-check.md` の「native entry 8件すべてに
`include_apps_usage_instructions: true` が新設された」は値について不正確で、
`codex-auto-review` だけ `false` だった。フィールドが8件すべてに新設された点は正しい。

### node_replとは何か

codex binaryの文字列から、`node_repl` はcodex**ローカル**のJavaScript REPL
（`node_repl.js` / code mode / `js_repl` feature）であり、ChatGPTアカウント側の機能ではない。
2つのフィールドはper-turn metadata（`x-codex-turn-metadata`）にも載る。

### 検知が働いた証跡

```
FAIL: test_installed_template_has_no_unknown_fields
  AssertionError: ['node_repl_auto_review_required', 'node_repl_disabled'] != []

WARN: cloneテンプレートに未知フィールドがあります（OpenRouter entryが継いでいます）:
      ['node_repl_auto_review_required', 'node_repl_disabled']
RESULT: PASS
```

開発者側はtest FAIL、利用者側はdoctor WARN（fail させない）で気づける。設計意図どおり。

## 中和値の判断

`node_repl_disabled` は**否定形**のフィールドで、既存の中和規約（「その能力を主張しない値へ倒す」）
がそのままでは適用できない。次を採った。

| フィールド | 中和値 | 理由 |
| --- | --- | --- |
| `node_repl_auto_review_required` | `False` | auto reviewはnative側の仕組み。ORモデルをそれでgateしない |
| `node_repl_disabled` | `False`（`True` にしない） | `True` は「JS REPLを無効化」。テンプレートは `tool_mode: code_mode_only` なので、ORモデルからツールを丸ごと奪う恐れがある |

結果として、今日レンダリングされるcatalogの中身は継承した場合と同一になる。中和で増えるのは2点だけ。

- `validate()` がOR entryの値を契約として強制する（生成時に検査される）
- 純正appが将来この値を反転させてもOR entryが追随しない（drift遮断）

## 変更詳細

### 1. `src/codex_openrouter/catalog.py` — 中和対象の追加

`NATIVE_ONLY_FIELDS` に2件追加。`node_repl_disabled` には「`True` にしてはいけない理由」を
コメントで残す。`KNOWN_TEMPLATE_FIELDS` に同2件を追加（36 → 38件）、build番号を6720へ。

### 2. `src/codex_openrouter/catalog.py` — テンプレートsnapshot

`snapshot_template()` / `read_snapshot()` / `template_field_drift()` を足す。

- payloadは `{"schema_version", "version", "build", "template"}`（約38KB）
- rotateは `write()` と同じ `.previous` 規約。ただし**同じbuildで組み直したときはrotateしない**。
  profile変更のたびにrotateすると `.previous` が同buildで埋まり、比較対象の旧buildが消える
- `template_field_drift` はフィールド名だけ返し、値は返さない。
  `base_instructions` のような大きな値をwarnに載せると読めなくなる

### 3. `src/codex_openrouter/app.py` — 保存先

`UserPaths.clone_template_snapshot` を足す（`state_dir / "clone-template.json"`）。
`state_dir` は `upgrade` のpromotion対象外なのでapp更新をまたいで残る。
`catalog.stale_paths()` には含めない（消したら比較対象が無くなる）。

### 4. `catalog.generate()` / `supervisor.py` — 書き出し経路

`generate()` に `snapshot` / `build_id` を足し、compositeの原子的置換が成功した**後**に書く。
呼び出しは `supervisor.refresh_catalog_if_needed` の1箇所だけで、そこは既に `(version, build)` を持つ。

### 5. `src/codex_openrouter/doctor.py` — 差分の可視化

`check_catalog_template` を拡張。未知フィールドがあっても早期returnせず、snapshotのbuildが
実機buildと違うときは `template_field_drift` のadded / removed / changedもWARNする。
app更新から次回起動までの窓（＝今回の状況）で効く。snapshotが無ければ何も言わない。

### 6. テスト

`tests/test_catalog.py` / `tests/test_doctor.py` にfixture更新と新規テストを追加。
`node_repl_disabled` が `True` にならないことをピン留めする。

## Verification

```bash
PYTHONPATH=src python3 -m unittest discover -s tests --buffer
python3 -m compileall -q src portable scripts
uvx ruff@0.16.3 check --select E9,F .   # 実行後 .ruff_cache を消す（secret_scanが落ちる）

# 変更後のdoctorを実機stateへ当てる（読み取り専用）。
# `./codex-openrouter doctor` は導入済みshimへ委譲するので変更が反映されない。
PYTHONPATH=src python3 -c "from pathlib import Path; \
from codex_openrouter.app import UserPaths; from codex_openrouter.doctor import run; \
raise SystemExit(run(UserPaths.current(), Path('models/registry.json')))"
```

実施結果:

- unittest **277件 PASS**（263 → +14）、compileall OK
- ruffは既存の9件のみ（`cli.py` / `lifecycle.py` / 他testsで、`cli.py` のF821はopen PR #18が直す）。
  今回触ったファイルには1件も無い。`.ruff_cache` は実行後に削除、`secret_scan --tree .` はPASS
- 変更後doctorが実機build 6720に対してPASSし、未知フィールドのWARNが消えて
  `OK: cloneテンプレートは既知のフィールドだけで構成されています` に戻る。
  snapshotはまだ無いのでsnapshot側のWARNも出ない
- 変異テスト: `node_repl_disabled` の中和値を `False` → `True` にすると
  `test_does_not_disable_the_local_js_repl` が落ちる。判断を実際にピン留めできている
- **実データでの再現**: 6662のbundledダンプでsnapshotを作り、実機6720と比べると
  `template_field_drift` が `{'added': ['node_repl_auto_review_required', 'node_repl_disabled']}`
  を返す。今回手作業で出した差分と一致する。`generate()` が置換後にrotate（`.previous` = 6662、
  current = 6720 / 26.814.41407）し、同じbuildで組み直しても `.previous` は6662のまま。
  snapshotは38,577 bytes / 0600

### 実機E2E（6662分と合流。app終了状態で実施）

6662のE2Eはlauncherのクリック前にappが更新されたため未完（`supervisor.json` はまだ
`6396` / `active=false`、`~/.codex/model-catalogs/` は空）。6720でまとめて実施する。

1. `codex-openrouter upgrade` — `install-manifest.json` の `chatgpt_version` が
   `26.814.41407` / `6720` へ、`source_commit` が新HEADへ更新されること
   （このフィールドは書くだけで誰も読まないので、6662のままでも機能影響は無かった）
2. Desktopの `Codex OpenRouter.app` → 「OpenRouterで起動」→ pickerにnative 5 + OR 5 の10件
3. `~/.codex/model-catalogs/codex-openrouter.json` が再生成され、OR entryの
   `include_apps_usage_instructions` / `node_repl_disabled` / `node_repl_auto_review_required`
   がいずれも `false`、`multi_agent_version` が `null` であること
4. `state/clone-template.json` がbuild `6720` で作られること（`.previous` はまだ出来ない。
   旧buildのsnapshotが無いため。実際の差分検知が効くのは次回のapp更新から）
5. `supervisor.json` の `version`/`build` が `26.814.41407` / `6720` になること
6. ORモデルで1ターン流し、`guard.log` に `decision: forwarded` が残ること
7. `codex-openrouter doctor --runtime --secret-scan` がPASS
8. 終了後に純正ChatGPT.appを単体起動しvanilla（catalog block無し）へ戻ること

## 別件（今回は触らない）

- `.gitignore` に `.ruff_cache/` が無く、ruff実行後に `scripts/secret_scan.py --tree .` が
  「personal absolute path」で落ちる
- `upgrade` が「API keyのspend limitが未設定です」と警告している。READMEは必須としている
- open PR #17 / #18 / #19 は未マージ（#18 が `cli.py` のF821を直す）

## Status

- 2026-08-18: 調査完了（支障なし）。実装とリポジトリ側のVerificationまで完了。
  実機E2E（上記8項目）はapp終了状態での操作が要るため未実施。
