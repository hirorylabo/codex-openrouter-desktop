## 変更内容


## 検証

- [ ] Python unit tests
- [ ] semantic patcher tests
- [ ] secret scan
- [ ] 実ChatGPT.app、ASAR、credential、Cookie、DB、userData、logを追加していない
- [ ] dependencyとGitHub Actionはcommit SHA固定

## 実機E2E（adapter変更時のみ）

- [ ] stock署名・ASAR hash不変
- [ ] candidateの3 transformが各1件
- [ ] App Serverとprofileがexact一致
- [ ] ZDR canaryと実provider確認
- [ ] UI目視、更新、rollback
