# 003: PR #1 レビュー指摘 1・2 の修正

作成日: 2026-08-12 / ブランチ: `codex/build-6396-adapter` / 対象: PR #1（v0.2.0 案D移行）

Status: **完了。自動検証PASS + 実機へ反映済み（`task/004` の初回upgradeで導入）**

### 検証結果（2026-08-12）

- `PYTHONPATH=src python3 -m unittest discover -s tests` → **132件 PASS**（130 + 追加2件）
- 新テストが旧実装で落ちることを確認: `AssertionError: 2.0656 not less than 0.5`（最初のチャンクがストリーム完了後にしか届かない）
- `python3 -m compileall -q src portable scripts` PASS
- `xcrun swiftc portable/launcher/CodexOpenRouterLauncher.swift` PASS（CI macos-compile と同じ）
- `python3 scripts/secret_scan.py --tree . --git-history` PASS、release archive のscanも PASS
- `plutil -replace CodexLauncherLog` → `PlistBuddy -c Print` で焼き込みを実測確認、`plutil -lint` OK

## Context

PR #1 のマージレビューで、マージ前に直すべき2件が出た。両方とも「案Dで未検証のまま残った経路」に属する。

### 1. guardのSSE中継が全バッファされる

`guard.py` の `_relay()` が使う `stream.read(8192)` は `http.client.HTTPResponse.read(amt)` で、**amtバイト溜まるかレスポンス完了まで返らない**（chunked時も `_read_chunked` がamtまで貯める）。

実測（0.1秒間隔で20チャンクを送るSSE上流に対して）:

```
read(8192):   t=2.07s bytes=310   ← 全部終わってから一度に
read1(8192):  t=0.00s / 0.11s / 0.21s  bytes=15 ずつ
```

実trafficでは 8KB 溜まるか turn 完了までトークンが1文字も出ない。task/002 が「guardの中継経路は課金回避のため実往復未実施」と記録した、まさにその経路。既存テストは中継先を `io.BytesIO` で差し替えているため（`tests/test_guard.py`）、この差を構造的に検出できない。

### 2. Desktopランチャー(Swift)が案D撤去の取り残し

`portable/launcher/CodexOpenRouterLauncher.swift` は `995615d`（初回OSS版）以来一度も更新されておらず、

- `cloneExecutablePath` が削除済みの `~/Applications/ChatGPT OpenRouter.app/...` を指す → 前面化の照合が永久にnil
- `launcherLogPath` が `~/.codex-openrouter/logs/launcher.log`。実際の出力先は `~/.local/share/codex-openrouter-desktop/state/logs/launcher.log` で、しかも `migrate` が旧`logs`を消す → エラーダイアログが存在しないパスを案内する

CIは `swiftc` のコンパイルしか見ないので落ちない。`7c79fca` が塞いだ「テンプレートの取り残しを `zsh -n` が検出できなかった」のと同型の穴が、Swift側にだけ残っていた。

## 変更詳細

### guard（`src/codex_openrouter/guard.py`）

- `_relay()` の読み出しを `read1` へ。`forwarder` は差し替え可能な公開注入点なので `read1` 非対応のstreamには `read` へ倒す
- `_Handler.disable_nagle_algorithm = True`。loopbackで小さい書き込みを繰り返すため、送信側の遅延を持ち込まない

### 中継の回帰テスト（`tests/test_guard.py`）

`StreamingRelayTests` を追加。**実 `HTTPResponse` を通す**ため自前のloopback上流を立てる。

- chunked の `text/event-stream` を 20回 × 0.1秒間隔で送る上流（総計約2.0秒）
- `guard_module.ENDPOINT` をその上流へ差し替え、`Guard` は既定の `forward_to_openrouter` を使う
- 最初のチャンクが 0.5秒以内に届くこと（総計2.0秒に対する余裕を取りjitter耐性を優先）
- 全チャンクが順序どおり結合して上流の送出内容と一致すること

### Swiftランチャー（`portable/launcher/CodexOpenRouterLauncher.swift` ほか）

- 旧clone参照を捨て、`NSRunningApplication.bundleURL` を `/Applications/ChatGPT.app` と突き合わせる。supervisorがbundle内executableを直接execしても `bundleURL` は .app を指すため
- 前面化のタイミングを修正。従来は `waitUntilExit()` の後（＝セッション終了後）で意味が無かった。`run()` 直後から 0.5秒間隔・最大60秒でポーリングし、見つかったら1回だけ activate する。プロセスが先に終了したらポーリングも止める
- log path を Info.plist 経由の単一情報源に。`CodexDefaultWorkspace` と同じ仕組みで `build_launcher()` が `UserPaths.state_dir` から `CodexLauncherLog` を書き、Swiftは読むだけにする（`portable/launcher/Info.plist` / `src/codex_openrouter/upgrade.py` / `src/codex_openrouter/install.py`）

### 再発防止（`tests/test_repository.py`）

`test_desktop_launcher_has_no_legacy_clone_references` を追加。既存の `test_no_source_references_a_deleted_repository_path` は `src/**/*.py` しか走査せず今回の取り残しを拾えなかったので、Swiftのentry pointへ検査を広げる。

## Verification

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
python3 -m compileall -q src portable scripts
python3 scripts/secret_scan.py --tree . --git-history
xcrun swiftc portable/launcher/CodexOpenRouterLauncher.swift -o /tmp/CodexOpenRouterLauncher
```

手動（実機）:

1. `codex-openrouter upgrade` でランチャーを再生成 → Desktopの `Codex OpenRouter.app` をクリック → 純正ウィンドウが前面に来る
2. credential helper を一時退避して起動 → エラーダイアログのlog pathが実在し、その中に `FAIL` 行がある
3. `codex-openrouter check` で catalog/provider block と model の整合が起動前後で崩れない

guardの実往復は今回スコープ外（ローカル上流テストで代替）。

## スコープ外

レビュー指摘 3（`install.py` と `upgrade.py` の90行二重化）、4（永続providerブロック + 固定port 8791 の露出面）、5（`start_guard` の死んだnonce分岐）、6（catalog blockのprovider block内への入れ子）。3 は次PRで扱う。

> 2026-08-12追記: 上記の残件は[`task/005-pr1-integrated-lifecycle-refactor.md`](./005-pr1-integrated-lifecycle-refactor.md)で統合して解決した。
