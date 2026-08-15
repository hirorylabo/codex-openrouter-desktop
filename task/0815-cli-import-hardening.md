# 0815: cliの遅延importで壊れた2コマンドを直し、未定義名をCIで止める

## Context

`task/0814-oss-public-release-plan.md` の引き継ぎを受けた保守性の仕上げとして
リポジトリを調べた際、**出荷済みのコマンドが2本 `NameError` で落ちる**ことが分かった。

```
$ codex-openrouter rollback
（"ROLLBACK" と入力して確認したあと）
NameError: name 'atomic_promote' is not defined

$ codex-openrouter migrate
NameError: name 'Supervisor' is not defined
```

### 原因

`cli.py` は各コマンドを「wrapper（`UserPaths` 取得と `LifecycleLock`）」と
「`_locked` ヘルパー（実処理）」に分けている。`73c8fb1`（起動切替の排他）でこの分割を
入れたとき、**関数ローカルの import は wrapper 側に残り、使用箇所だけが helper へ移った**。

```python
def rollback_command(_args):
    from .promotion import atomic_promote, rollback_replacements   # ← ここで束縛
    paths = UserPaths.current()
    with LifecycleLock(paths):
        return _rollback_locked(paths)                             # ← 別scope

def _rollback_locked(paths):
    ...
    atomic_promote(rollback_replacements(source_backup), ...)      # ← 見えない
```

関数ローカル import は module global を束縛しないので、helper からは参照できない。
同じ形が `migrate_command` → `_migrate_locked`（`Supervisor`）にもある。

発火位置が悪い。`rollback` は利用者が `ROLLBACK` と入力して確認した**あと**、
`migrate` は旧専用app削除の確認**手前**で落ちる。前者は upgrade が壊れたときの復旧経路そのもの。

### なぜ検知できなかったか

- **テスト**: `tests/test_lifecycle_lock.py` が `_rollback_locked` / `_migrate_locked` を
  `mock.patch.object` で丸ごと差し替えている。lock の検証が目的なので中身は一度も実行されない。
  `cli.py` のコマンド関数23本のうち、テストから実際に実行されるのは補助関数と `main` だけで、
  `check` / `launch` / `profile` / `models` / `guard-log` / `upgrade` は参照ゼロ。
  253件のテストはすべてその下の層（`promotion` / `supervisor` / `settings` …）に当たっている。
- **CI**: 静的検査は `python3 -m compileall` のみ。これは構文しか見ないので未定義名は原理的に検出できない。
  リポジトリに linter の設定・導入は一切なかった。

## 変更詳細

### 1. `src/codex_openrouter/cli.py` — 遅延importをmodule levelへ集約

関数ローカル import 15箇所を module level へ移す。根拠:

- `cli` を import している module は**ゼロ**。循環importによる遅延理由が存在しない。
- `main()` は dispatch 前に問題の7 module を無条件 import している
  （`install` / `lifecycle` / `modelcatalog` / `promotion` / `settings` / `supervisor` / `upgrade` を
  例外catch用に読み、そのあと `args.func(args)` を呼ぶ）。
  つまりコマンド関数が動く時点で全 module は `sys.modules` にあり、**遅延は起動コストを1マイクロ秒も節約していない**。
- entry pointは3経路（`./codex-openrouter`、`python3 -m codex_openrouter.cli`、
  `portable/templates/codex-openrouter-app.zsh.in`）とも `main()` を通ることを確認済み。

module参照で使うものは既存の `from . import configblock` に倣い
`from . import modelcatalog` / `from . import settings` の形にし、
例外クラスは `modelcatalog.CatalogError` / `settings.SettingsError` として参照する。

### 2. 実際に実行する回帰テスト `tests/test_cli_commands.py`（新規）

`_rollback_locked` / `_migrate_locked` を mock せずに走らせる。
今回の `NameError` は wrapper 側だけを見ていると再現しないため、helper を直接呼ぶ経路を持つ。

- `rollback`: upgrade backup を1件置き、確認入力を `ROLLBACK` として実行、
  ファイルが upgrade 前へ戻ることと rollback 直前 backup が残ることを確認する。
- `migrate`: `self_heal` 済みの状態で旧homeの圧縮まで通し、`MIGRATE: PASS` に到達することを確認する。

### 3. 未使用importの除去

ruff/pyflakes が検出した実在の未使用import 3件。

- `src/codex_openrouter/lifecycle.py:7` — `pathlib.Path`
- `tests/test_profile_auth.py:4` — `os`
- `tests/test_repository.py:3` — `importlib.util`

### 4. `ruff.toml`（新規）と CI の `lint` job

```toml
target-version = "py311"
extend-include = ["codex-openrouter"]   # 拡張子の無いentry point
[lint]
select = ["E9", "F"]
```

`E9`（構文・IOエラー）と `F`（pyflakes相当）だけに絞る。styleルールは入れない。

CI は `astral-sh/ruff-action` を commit SHA で固定して使う。

- action の SHA は既存の Dependabot（`package-ecosystem: github-actions`）が自動追従する。
  新しい ecosystem も requirements ファイルも増えない。
- action は download した ruff binary の sha256 を検証する。
- ruff 本体の version は workflow 側に明示的に固定する（Dependabot の対象外なので、
  更新は意図的・レビュー可能な操作になる）。

`ci-required` の `needs` と集約ループへ `lint` を足す。
required check 名は `ci-required` のままなので **main ruleset の変更は不要**。

### tool選定の根拠（実測、2026-08-15）

対象は追跡下のPython 87ファイル。「今回の3件を実際に検出するか」を軸に比較した。

| tool | 検出 | 全体の出力 | 判断 |
|---|---|---|---|
| **ruff 0.16.3** `--select F` | 3件すべて (F821) | 9件（すべて実在、誤検知0、13ms） | **採用** |
| pyflakes 3.4.0 | 3件すべて | 9件（ruff と完全一致） | ruffがFルールとしてpyflakesを内包。上位互換 |
| pylint 4.0.7 `E0602` | 3件すべて | 3件 | 検出はできるが遅く、設定面が重い |
| mypy 2.3.1 | 3件すべて | 17件（型の粗さ14件が混在） | 導入コストが目的に見合わない |
| ty 0.0.72 | — | 36件 | 0.0.x のpreview。ゲートには使えない |
| pyrefly 1.2.0 | — | 46件（大半がtestsのsys.path由来のimport解決失敗） | 追加設定が必要 |

ruff の `F` ルールは pyflakes の再実装で、本リポジトリでは**出力が9件とも完全一致**した。
pyflakes は純Python・依存ゼロという利点があるが、CI で `pip install` する経路が
Dependabot の現行設定の外に出る。ruff は action 経由なら既存の更新機構に乗る。

`ruff check` の既定ルール（`E4,E7,E9,F`）は134件出る。大半が import 並び替え（I001）や
`# noqa` の整理（RUF100）で、今回の目的と無関係なので採らない。
`E4` を足すと tests の `E402` が5件出るが、これは `sys.path` を通してから
`codex_openrouter` を import するための意図的な配置なので除外した。

### 5. `CONTRIBUTING.md`

PR前に流すコマンド一覧へ lint を追加する。

## 触らないもの

- 挙動の変更は「`rollback` と `migrate` が落ちなくなる」ことだけ。他のコマンドの出力・引数・
  ファイル配置は一切変えない。
- テスト出力の衛生（green run に `FAIL:` が84行出る件、`ResourceWarning` 2件）は
  今回の範囲外。別PRへ回す。
- `models/registry.json`、`profiles/`、`portable/` 配下、release workflow には触れない。
- `build_release.py` の `FILES` に `ruff.toml` は足さない。配布物には含めない。

## Verification

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src portable scripts
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py
python3 scripts/secret_scan.py --tree .
python3 scripts/build_release.py "v$(cat VERSION)" --dist /tmp/codex-openrouter-dist
ruff check .
```

加えて、修正前のコードに対して新しい回帰テストが**落ちること**を確認する
（表明が空回りしていないことの確認）。

## Status

実施済み（2026-08-15）。結果は「実行結果」節を参照。

## 実行結果

### 表明が空回りしていないことの確認

`src/codex_openrouter/cli.py` だけを修正前（`main` の内容）へ戻して新テストを流した。

```
ERROR: test_migrate_keeps_the_legacy_home_intact_with_keep_all
NameError: name 'Supervisor' is not defined
ERROR: test_migrate_persists_the_provider_block_and_compacts_the_legacy_home
NameError: name 'Supervisor' is not defined
ERROR: test_rollback_restores_the_pre_upgrade_tree
NameError: name 'atomic_promote' is not defined
Ran 6 tests ... FAILED (errors=3)
```

残る3件（確認入力の不一致、復元対象なし、shared config欠如）は修正前後どちらでも通る。
いずれも `NameError` の行へ到達する前に `CliError` で抜ける経路なので、これは期待どおり。

### 途中で見つけたこと: hoistがmockの当て先を変える

`tests/test_profile_auth.py` の
`test_fresh_setup_validates_once_then_installs_without_permanent_helper` が1件落ちた。

このテストは `mock.patch.object(install_module, "_install_unlocked")` と、**定義側のmodule**へ
patchしていた。関数ローカル import は呼び出しのたびに属性を引き直すので、これで差し替えが効いていた。
module level import にすると `cli._install_unlocked` は import 時に元の関数へ束縛されるため、
patchが素通りし、本物の `preflight` が動いてこのマシンで起動中の ChatGPT.app を検出して落ちた。

`mock.patch` の原則どおり **使う側**（`cli`）へ patch する形へ直した。
同じ形の patch は他に2箇所あるが、いずれも `upgrade_module.auto_upgrade` /
`install_module.install` を直接呼ぶテストで、module内のglobal参照なので影響しない
（`tests/test_maintenance.py` の5箇所、`tests/test_lifecycle_lock.py:82`）。

### 検証

| 項目 | 結果 |
|---|---|
| unittest | 259件 PASS（既存253 + 新規6） |
| `compileall` | PASS |
| synthetic E2E | PASS |
| secret scan（tree + git history） | PASS |
| `build_release.py` + archive secret scan + SHA256SUMS | PASS |
| `ruff check .`（`ruff.toml`） | All checks passed |

配布物は 81 entry。公開済み v0.2.0 の 80 entry に対する差は
`tests/test_cli_commands.py` の1件だけで、`ruff.toml` と `task/` は同梱されていない
（root の `FILES` allowlist に無いため）。

### 残した宿題

テスト出力の衛生は今回の範囲外とした。green run のまま stdout に84行・stderr に93行が出て、
その中に `doctor.py` 由来の `FAIL:` が含まれる。実際、この作業中に発生した唯一のテスト失敗も
その出力に埋もれて、`grep` で掘り出す必要があった。`HTTPError` fixture の `ResourceWarning` 2件も同様。
別PRで扱う。
