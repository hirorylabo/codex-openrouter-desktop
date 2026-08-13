## 変更内容


## 検証

- [ ] `PYTHONPATH=src python3 -m unittest discover -s tests`
- [ ] `PYTHONPATH=src python3 -m compileall -q src portable scripts`
- [ ] `PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py`
- [ ] `python3 scripts/secret_scan.py --tree .`（release archiveも通す場合は `--archive`）
- [ ] `xcrun swiftc portable/launcher/app/*.swift` と `zsh -n` で入口を解析
- [ ] 実ChatGPT.app、ASAR、credential、Cookie、DB、userData、logを追加していない
- [ ] dependencyとGitHub Actionはcommit SHA固定

## 実機E2E（supervisor・ランチャー・profile経路を変えた場合）

- [ ] `scripts/macos_live_e2e.py`（隔離home）
- [ ] `scripts/macos_installed_e2e.zsh`（導入済みlauncherを2 cycle）
- [ ] 純正ChatGPT.appの署名有効・patch marker無し（`codex-openrouter doctor`）
- [ ] pickerに出るOpenRouterモデルが導入済みprofileと一致
- [ ] 秘密値がUI・config・catalog・guard logに出ない
