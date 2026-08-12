# 005: PR #1 5件統合リファクタリング

作成日: 2026-08-12 / ブランチ: `codex/build-6396-adapter` / 対象: PR #1

Status: **実装・自動検証完了。実機runtime更新とnetwork doctorは安全条件待ち**

## 結論

レビューで残っていた5件を、個別の条件分岐ではなく次の3境界へ統合した。

1. managed configのactive / inactive変換
2. 導入済みprofileを唯一の実行時source of truthとする経路
3. setup / upgrade共通のstaging・検証・atomic promotion

新しいdaemonや汎用frameworkは追加していない。純正`/Applications/ChatGPT.app`も変更しない。

## 変更

### 1. setup / upgradeの導入経路

- setupは一時credential helperで認証・profile検証・Keychain保存を終えてから導入する
- runtime、helper、launcher、manifest、profile、supervisor stateを`promote_runtime()`で一括stagingする
- promotion後に恒久helperの`status`、doctor、stock app不変を検証し、失敗時は全targetをrollbackする
- manifestをschema 5へ上げ、正規化profileのdigestを記録する
- 初回に新設されるprofile/stateも手動rollbackで削除できるよう、promotionへ明示的なtombstoneを追加する

### 2. managed configの単一変換

- `configblock.render_managed()`がcatalogとproviderを同じatomic edit内で再構成する
- 変換前後のTOML、marker数、marker外のprovider/catalog衝突をfail-closedで検査する
- catalogは最初のtableより前、providerは末尾へ置き、旧入れ子構造を正規化する
- inactive時はcatalogなし、`127.0.0.1:0`、`/usr/bin/false`認証の非接続stubを残す

### 3. guardの秘密情報境界

- guardはephemeral portで起動し、実API keyをKeychainから読む唯一のcomponentとする
- Codexからguardへは起動ごとのrandom tokenだけを送る
- tokenはmode 0600のfileへ保存し、providerは`/bin/cat`で読む
- guardはBearer tokenを検証してからrequest bodyを読むため、認証失敗requestは本文を中継しない
- cleanup順をwatcher停止、config inactive化と選択復元、state保存、guard停止、token削除に固定した

### 4. profileのsource of truth

- 正規化済みprofileを`state/profile.json`へ導入し、catalog、guard、watcher、doctorが同じmodel集合を使う
- profile省略の通常upgrade / auto-upgradeは導入済みprofileを維持する
- `setup --profile`または明示的な`upgrade --profile`だけがprofileを置換する
- digestが変わったときだけdefault model適用をarmし、最初の専用起動で解除する
- 旧環境は旧profile、同梱defaultの順で一度だけ移行する

### 5. doctor・文書・E2E

- version付きstateにprofile digest、default適用待ち、active状態、guard port/nonce、退避選択を保存する
- doctorはprofile、catalog、provider lifecycle、token mode、guard、secret、networkを同じ期待値から検査する
- live E2Eへ2回目のactive / cleanup cycleと、local token付き許可・拒否requestを追加した
- README日英、SECURITY、CONTRIBUTINGを非ASAR・非clone構成へ揃えた

## 検証

```bash
PYTHONPATH=src python3.11 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src portable scripts
python3 scripts/secret_scan.py --tree . --git-history
python3 scripts/build_release.py v0.2.0 --dist /tmp/codex-openrouter-dist
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py
```

- Python 3.11 unit: **164件 PASS**
- compileall / `git diff --check`: PASS
- source tree / release archive secret scan: PASS
- release tarball / SPDX / SHA256: PASS
- Swift credential helper / launcher compile、icon build、zsh syntax: PASS
- synthetic E2E: promotion、verify失敗時の自動rollback、手動rollbackを含めPASS

## 実機検証の残り

現在のCodex task自身が純正ChatGPT.app上で動作中のため、これを終了させる必要があるruntime upgradeとクリック起動はこのsession中に安全に実行できない。また、ローカルKeychainにOpenRouter credentialが無いため、`doctor --network`は認証を推測・再作成せず保留した。

次回、ChatGPT.appを終了し有効なcredentialを利用者が設定した状態で次を行う。

```bash
./codex-openrouter upgrade
codex-openrouter doctor --network --runtime --secret-scan
```

その後Desktop launcherを2 cycle実行し、inactive復帰、許可model中継、拒否model非送信をguard logと実画面で確認する。強制終了・電源断直後は次回self-healまで実port設定が残り得るが、token file削除後は認証に失敗し、実API keyはloopbackへ渡らない。
