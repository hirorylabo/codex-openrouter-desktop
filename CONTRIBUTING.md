# Contributing

IssueやPull Requestは日本語・英語どちらでも歓迎します。まず`README.md`の非公式・実験版という前提と`SECURITY.md`を確認してください。

## Pull Request

1. 変更を小さく保ち、関連しないrefactorを混ぜないでください。
2. API key、auth、Cookie、DB、ASAR、`.app`、userData、logをfixtureへ入れないでください。
3. dependencyやGitHub Actionはversion tagではなくcommit SHAで固定してください。
4. 次を実行し、結果をPRへ記載してください。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
cd portable/patcher-js && npm ci --ignore-scripts && npm test
python3 scripts/secret_scan.py --tree .
```

実ChatGPT.appをCIへ追加しないでください。build adapterの実機E2Eは、stock署名/hash不変、candidateのみへのpatch、App Server、UI、network canary、rollbackを別々に記録します。

セキュリティ問題はpublic issueではなくPrivate Vulnerability Reportingを使用してください。
