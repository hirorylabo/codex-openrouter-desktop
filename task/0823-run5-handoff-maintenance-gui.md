# 0823 Run 5 ハンドオフ: メンテナンスGUI実装(アカウント切替対応)

作成日: 2026-08-23 / branch: `main` @ `787c50a`(PR #32 マージ後)
実装ブランチ: **`docs/maintenance-gui-plan`**(plan push済み、PR未作成)

---

## TL;DR

- **やりたいこと**: デスクトップの既存ランチャー(`~/Desktop/Codex OpenRouter.app`)に
  「アカウント管理」GUIを追加。native一時切替でサインアウト/アカウント切替を可能にし、
  通常はハイブリッドモード(OR+native両方)を維持する
- **実装計画**: `task/0823-maintenance-gui-plan.md`(8タスク、TDD、~1日、課金$0)
  - planはbranch `docs/maintenance-gui-plan` にcommit済み(`d939286`)
  - **PR #33 は未作成**(`gh pr create`が承認待ちでブロックしたため)。最初にPR化してmergeすること
- **前スレッドからの引き継ぎ事項**: `task/0823-handoff-next-phase.md` 参照

## 背景: なぜ必要か

- `model_provider="openrouter"` 起動中、ChatGPT.appはサードパーティproviderモードと
  なりアカウントUI(ログアウト/サインイン)を隠す(app.asar解析で確認済みの仕様)
- メニューバーの「ログアウト」も無反応(ユーザー実機確認済み)
- native codex modelがlimitになったとき、別ChatGPTアカウントへ切り替えられない

## 解決設計

| モード | 動作 |
|---|---|
| 通常(ハイブリッド) | ORモデル + nativeモデル両方選択可(現状維持・デフォルト) |
| メンテナンス(native一時切替) | catalog/provider block退避→native起動→本来のサインインUI復活→restore |

コマンド: `codex-openrouter account status|switch-native|restore`(--json対応、Swiftから呼ぶ)
Swift側: MaintenanceWindow追加(確認ダイアログ→switch-native→案内→「復帰」ボタン→restore)

## タスク一覧(planの詳細コードを参照)

1. State に `maintenance_active` フラグ(TDD)
2. `maintenance.py` コア(switch_native/restore、block退避)(TDD)
3. CLI wiring — `account` サブコマンド3種(TDD)
4. switch-native時のapp再起動統合(process_pids検知→terminate→`open -a ChatGPT`)
5. doctor放置警告(maintenance_active中の警告)
6. Swift MaintenanceWindow(「アカウント管理…」ボタン+確認ダイアログ+「復帰」)
7. picker モデルカスタマイズ統合確認(ModelSettingsWindow保持検証)
8. 最終Verification(unit/ruff/実機cua-driver/PR→CI→merge→promote)

## 重要な既知事実

- config.toml操作は必ず `configblock.py` の marker block 方式を使う(手書き置換禁止)
- marker名は `catalog` / `provider`(`configblock._validate_marker` と一致させる)
- Swift→CLI呼び出し口は `ProfileBridge.run()`(既存)。`executableURL` はinstalled CLI
- ランチャー再ビルド: `python3.11 ./codex-openrouter upgrade`(swiftc + codesign込み)
- unit test: `PATH="$HOME/.local/bin:$PATH" PYTHONPATH=src python3.11 scripts/run_unit_tests.py`
  (386基準)/ lint: `uvx ruff@0.16.3 check src tests`
- main直push不可(ruleset ci-guard) → 全変更PR経由
- 実機確認は cua-driver 使用可能(TCC許可済み)。ChatGPT.appのwindow_idは起動毎に変わるので
  get_accessibility_tree で毎回取り直す。element_index単独不可、element_token必須
- Hindsight memory無効化済み(memory.provider=built-in)
- ChatGPT.app build 6962 / installed v0.2.1 @ d9571f2 / 現在launch停止中

## Verification(完了条件)

- [ ] unit全件PASS(386+)・ruff pass
- [ ] check/doctor PASS(通常モード)
- [ ] 実機: switch-native後、歯車ポップアップに「Log out」出現(cua-driverスクショ)
- [ ] 実機: 別アカウントサインイン→restore→ORモデル利用可
- [ ] 実機: native gptモデルも新アカウントで利用可(limit回避フロー成立)
- [ ] 実機: ModelSettingsのモデルカスタマイズが復帰後も保持
- [ ] PR→CI 11/11→merge→upgrade promote PASS

## 未確定・要判断事項

1. switch-native中はpickerからORモデルを消す(catalog blockも外す)— 推奨方針でplan固定済み
2. restore放置の自動復帰はしない(doctor警告のみ、YAGNI)
