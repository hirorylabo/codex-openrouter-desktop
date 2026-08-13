# Contributing

IssueやPull Requestは日本語・英語どちらでも歓迎します。まず`README.md`の非公式・実験版という前提と`SECURITY.md`を確認してください。

## Pull Request

1. 変更を小さく保ち、関連しないrefactorを混ぜないでください。
2. API key、auth、Cookie、DB、ASAR、`.app`、userData、logをfixtureへ入れないでください。
3. dependencyやGitHub Actionはversion tagではなくcommit SHAで固定してください。
4. 次を実行し、結果をPRへ記載してください。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src portable scripts
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py
python3 scripts/secret_scan.py --tree .
python3 scripts/build_release.py "v$(cat VERSION)" --dist /tmp/codex-openrouter-dist
```

実ChatGPT.appや実credentialをCIへ追加しないでください。実機E2Eは、純正appの署名不変、managed configのactive/inactive往復、profileとpicker/guard/watcherの一致、guardの許可・拒否、secret scan、rollbackを別々に記録します。手動実行では、まず`PYTHONPATH=src python3 scripts/macos_live_e2e.py`、導入済みruntimeのupgrade後に`scripts/macos_installed_e2e.zsh`を使います。後者は2 cycleとも通常終了を待ち、強制終了しません。

セキュリティ問題はpublic issueではなくPrivate Vulnerability Reportingを使用してください。
