# Contributing

IssueやPull Requestは日本語・英語どちらでも歓迎します。まず`README.md`の非公式・実験版という前提と`SECURITY.md`を確認してください。

## Pull Request

1. 変更を小さく保ち、関連しないrefactorを混ぜないでください。
2. API key、auth、Cookie、DB、ASAR、`.app`、userData、logをfixtureへ入れないでください。
3. dependencyやGitHub Actionはversion tagではなくcommit SHAで固定してください。
4. 次を実行し、結果をPRへ記載してください。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v --buffer
PYTHONPATH=src python3 -m compileall -q src portable scripts
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py
python3 scripts/secret_scan.py --tree .
python3 scripts/build_release.py "v$(cat VERSION)" --dist /tmp/codex-openrouter-dist
uvx ruff@0.16.3 check .
```

`ruff`は設定を`ruff.toml`に置き、`E9`（構文・IOエラー）と`F`（pyflakes相当）だけを見ます。styleは対象外です。`compileall`は構文しか検査しないため、未定義名（`F821`）はここでしか止まりません。CIは同じversionを`astral-sh/ruff-action`から実行します。

## テストを書くときの約束

**`--buffer`を外さないでください。** テストが呼ぶ`doctor`・`supervisor`・`upgrade`は正常系でも標準出力へ書きます。素で流すとgreenのまま100行超が出て、その中に`doctor`由来の`FAIL:`まで混ざります。`--buffer`は出力を失敗したテストにだけ紐づけて表示するので、緑は静かなまま、落ちたときの文脈は残ります。

**`mock.patch`は「定義した場所」ではなく「使う場所」へ当ててください。** `cli`が`from .promotion import atomic_promote`のようにmodule levelでimportしている名前は、import時に`cli`へ束縛されます。`mock.patch.object(promotion, "atomic_promote")`は`cli`側の束縛を差し替えないため、**patchが黙って素通りして本物が動きます**。`cli`経由の経路を差し替えるなら`mock.patch.object(cli, "atomic_promote")`です。定義側moduleへ当てて良いのは、そのmodule内の関数を直接呼ぶテストだけです（module内のglobal参照は実行時に引き直されるため）。

**コマンドの`_locked`ヘルパーをまるごとmockしたテストは、そのコマンドを検証したことになりません。** wrapper側の排他だけを見たいなら、それが目的だと分かるテスト名にしてください。コマンド本体の検証は`tests/test_cli_commands.py`のように、helperを実際に走らせて確認します。この2つを混同した結果、`rollback`と`migrate`が`NameError`で落ちる状態を出荷しました（`task/0815-cli-import-hardening.md`）。

**`urllib.error.HTTPError`をfixtureで作ったら閉じてください。** `HTTPError`は`urllib.response.addbase`経由で`tempfile._TemporaryFileWrapper`を継承しており、closeしないままGCされると`ResourceWarning`を出します。`fp`に何を渡すかとは無関係です。

実ChatGPT.appや実credentialをCIへ追加しないでください。実機E2Eは、純正appの署名不変、managed configのactive/inactive往復、profileとpicker/guard/watcherの一致、guardの許可・拒否、secret scan、rollbackを別々に記録します。手動実行では、まず`PYTHONPATH=src python3 scripts/macos_live_e2e.py`、導入済みruntimeのupgrade後に`scripts/macos_installed_e2e.zsh`を使います。後者は2 cycleとも通常終了を待ち、強制終了しません。

セキュリティ問題はpublic issueではなくPrivate Vulnerability Reportingを使用してください。
