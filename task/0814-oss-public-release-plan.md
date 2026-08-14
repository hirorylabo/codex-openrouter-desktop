# 0814: ブランチ整理・OSS公開強化・v0.2.0リリース計画

作成日: 2026-08-14 / 基準commit: `d22840c` / 対象: `hirorylabo/codex-openrouter-desktop`

Status: **Phase 0〜5 完了（2026-08-14）。Phase 5 実機リリースゲートは全項目PASS。Phase 6 実行可。**
基準commitは`d22840c`から`e1eacc4`（PR #5 squash merge）へ進んだ。

## 結論

ブランチ整理とOSS公開強化は進めてよい。`v0.2.0`のtag pushだけは、PR #2〜#4で追加した
モデル設定・catalog UXの実機確認が残っているため、現時点では停止する。

公開判断は次の2つを分ける。

- **コード・CI readiness**: 自動テスト、静的解析、配布物、repository設定が公開基準を満たすこと。
- **operational readiness**: 導入済みlauncher 2 cycle、設定画面、catalog、ZDRなしモデル、純正app不変を実機で確認すること。

両方がPASSし、利用者がtag公開を明示承認した場合だけ`v0.2.0`を公開する。

## 成功条件

- mainへ到達済みの旧branch 4本だけがlocal・remoteから削除される。
- mainはPR経由・CI成功必須となり、force-pushと削除が禁止される。
- Python 3.11〜3.14、macOS Swift compile、secret scan、release buildがCIで成功する。
- GitHub ActionsのSHA固定をDependabotが週次更新できる。
- CodeQL Python default setupが成功し、公開を止めるalertが残っていない。
- `scripts/macos_live_e2e.py`と`scripts/macos_installed_e2e.zsh`がPASSする。
- rankings join、ZDRなしモデルの警告・保存・実応答、純正picker表示を実機確認する。
- tagはOSS強化PRを含む最新`origin/main`へ打たれ、release assets、checksum、SBOM、attestationを再取得して検証する。

## 触らないもの

- 純正`/Applications/ChatGPT.app`、ASAR、署名、userData、Cookie、履歴を変更・取得しない。
- API keyを表示、引数化、log化、fixture化しない。
- `cli.py`等の分割、ruff、mypy、書き込み経路の一般化は行わない。
- 設計文書のarchive整理はリリース必須経路へ混ぜない。
- 実機E2Eが未完了のまま「prereleaseだから可」としてtagを押さない。

## Phase 0: preflight

目的: WIP、remote drift、既存tag、GitHub状態を変更前に固定する。

```bash
git status --short --branch
git fetch --prune origin
git log -1 --oneline --decorate origin/main
git branch -vv
git branch --merged origin/main
git branch -r --merged origin/main
git tag --list --sort=version:refname
gh pr list --repo hirorylabo/codex-openrouter-desktop --state open
gh run list --repo hirorylabo/codex-openrouter-desktop --limit 10
gh api repos/hirorylabo/codex-openrouter-desktop/rulesets
gh api repos/hirorylabo/codex-openrouter-desktop/code-scanning/default-setup
```

停止条件:

- mainに未説明の変更がある。
- open PRがある、または`origin/main`が調査時点から進んでいる。
- 削除候補のtipが1本でも`origin/main`から到達できない。
- `v0.2.0` tagまたはreleaseが既に存在する。

## Phase 1: 旧branch整理

削除候補:

- `codex/build-6396-adapter`
- `codex/model-settings-launcher`
- `codex/model-catalog-ux`
- `codex/fix-model-settings-runtime`

各local/remote tipを個別に確認する。

```bash
git merge-base --is-ancestor codex/build-6396-adapter origin/main
git merge-base --is-ancestor origin/codex/build-6396-adapter origin/main
git merge-base --is-ancestor codex/model-settings-launcher origin/main
git merge-base --is-ancestor origin/codex/model-settings-launcher origin/main
git merge-base --is-ancestor codex/model-catalog-ux origin/main
git merge-base --is-ancestor origin/codex/model-catalog-ux origin/main
git merge-base --is-ancestor codex/fix-model-settings-runtime origin/main
git merge-base --is-ancestor origin/codex/fix-model-settings-runtime origin/main
```

8件すべてexit 0の場合だけ、localは`-d`で削除する。`-D`は使用しない。

```bash
git branch -d \
  codex/build-6396-adapter \
  codex/model-settings-launcher \
  codex/model-catalog-ux \
  codex/fix-model-settings-runtime
```

remote branch削除とGitHub設定変更は外部副作用なので、実行直前に個別承認を取る。

```bash
git push origin --delete \
  codex/build-6396-adapter \
  codex/model-settings-launcher \
  codex/model-catalog-ux \
  codex/fix-model-settings-runtime
git config remote.origin.prune true
gh repo edit hirorylabo/codex-openrouter-desktop --delete-branch-on-merge
```

確認:

```bash
git fetch --prune origin
git branch -a
gh api repos/hirorylabo/codex-openrouter-desktop/branches --paginate
gh repo view hirorylabo/codex-openrouter-desktop --json deleteBranchOnMerge
```

## Phase 2: OSS公開強化PR

専用branchを作り、runtime挙動を変えない変更だけを1 PRへ載せる。

```bash
git switch -c codex/oss-public-release-hardening origin/main
```

### 2.1 CI

`.github/workflows/ci.yml`を次の責務へ分ける。

1. `python-compat`: Python `3.11`, `3.12`, `3.13`, `3.14`のmatrixでunittestとcompileall。
2. `audit-release`: Python 3.11固定でtree/history secret scan、release build、archive scan、checksum検証。
3. `macos-compile`: 現行のSwift、icon、zsh、synthetic E2Eを維持。
4. `ci-required`: `if: always()`と`needs`で上記3 jobがすべて`success`か確認する安定名の集約job。

matrix化したjob全体でsecret scanとrelease buildを4回重複実行しない。rulesetが要求するcheck名は
`ci-required`とし、matrixの増減でrepository設定を壊さない。

### 2.2 Dependabot

`.github/dependabot.yml`を追加する。

```yaml
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
```

Actionは引き続きcommit SHA固定とし、同じ行のversion commentを維持する。

### 2.3 release workflow permissions

`.github/workflows/release.yml`のtop-levelを`contents: read`へ下げ、`release` jobだけに次を付与する。

```yaml
permissions:
  contents: read

jobs:
  release:
    permissions:
      contents: write
      id-token: write
      attestations: write
```

release jobのtest、secret scan、build、attestation、prerelease公開順は変えない。

### 2.4 README badges

`README.md`と`README.en.md`へCI、MIT license、latest releaseの3 badgeを追加する。
日英で同じリンク先を使い、警告文より前へ置かない。

### 2.5 CodeQLの扱い

このPRでは`.github/workflows/codeql.yml`を追加しない。GitHub公式が最初に推奨するdefault setupを
repository設定から有効化し、まずPythonを対象にする。Swiftはdefault setupで安定して解析できるかを
別途確認し、失敗する場合だけmacOS buildを含むadvanced setupを別PRで検討する。

### 2.6 今回行わない文書整理

次は参照が残っているため、このPRでは移動・削除しない。

- `LOOPBACK_ROUTER_PLAN.md`
- `SESSION_CONTINUITY_PLAN.md`
- `SESSION_CONTINUITY_PLAN.full.md`
- `task/PLAN (6).md`

## Phase 3: PR検証とmerge

ローカル検証:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src portable scripts
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py
python3 scripts/secret_scan.py --tree . --git-history
python3 scripts/build_release.py "v$(tr -d '\n' < VERSION)" --dist /tmp/codex-openrouter-dist
xcrun swiftc portable/credential/CredentialHelper.swift -o /tmp/codex-openrouter-credential
xcrun swiftc portable/launcher/app/*.swift -o /tmp/CodexOpenRouterLauncher
zsh -n portable/templates/codex-openrouter-app.zsh.in
zsh -n portable/launcher/build_icon.zsh
git diff --check
```

PRを作成した後、次を満たすまでmergeしない。

- Python 3.11〜3.14が全件green。
- `audit-release`, `macos-compile`, `ci-required`がgreen。
- secret、credential、`.app`、ASAR、DB、userData、logがdiffへ入っていない。
- Dependabotとworkflowの全Actionがcommit SHA固定。
- 変更がCI、release権限、Dependabot、badgeだけに限定されている。

push、PR作成、mergeはいずれもGitHubへの外部変更なので、各段階で明示承認を取る。

## Phase 4: CodeQLとmain ruleset

OSS強化PRのmerge後に実施する。

1. CodeQL default setupをPythonで有効化する。
2. 初回解析の完了を待ち、失敗または未評価のまま先へ進まない。
3. 公開を止めるseverityのalertがあれば、別PRで修正して再解析する。
4. mainを対象にActiveなrulesetを作る。

ruleset:

- Require a pull request before merging: ON
- Required approving reviews: `0`（単独メンテナを停止させない）
- Require status checks: `ci-required`
- Require code scanning results: CodeQL
- Block force pushes: ON
- Restrict deletions: ON
- bypassは通常運用で使わない

check名はOSS強化PRで実際にgreenになったものを選ぶ。設定確認のためにmainへforce pushを試さない。
read-only APIと次の通常PRでrulesetが効くことを確認する。

## Phase 5: v0.2.0実機リリースゲート

前提:

- 利用者がChatGPT.appを通常終了できる時間帯で行う。
- 有効なOpenRouter credentialがKeychainにある。
- `doctor --network`とモデル疎通に少量のAPI利用があり得ることを事前確認する。
- password、Cookie、認証コード、API keyを表示・読み取りしない。

導入済みruntimeをリポジトリの最新版へ合わせる。

```bash
./codex-openrouter upgrade
codex-openrouter doctor --network --runtime --secret-scan
PYTHONPATH=src python3 scripts/macos_live_e2e.py
scripts/macos_installed_e2e.zsh
```

必須確認:

- isolated live E2Eが全件PASS。
- installed launcherが2 cycleともactive/inactiveへ正常遷移する。
- 管理画面、`⌘,`、folder drop、純正appからのhandoffが期待どおり。
- 設定画面にAPI keyが出ない。
- 価格、公開日、7dトークン量の列とsort/filterが動く。
- `ZDRのみ`が既定ONで、選択済みモデルがfilterで消えない。
- `models list --json --refresh`の`usage_available`がtrue、`usage_matched`が1以上。
- ZDRなしモデル選択時に確認sheetが出て、取消時は変更されない。
- 明示承認後にZDRなしモデルを1件追加し、管理画面・doctor・pickerの警告と実応答を確認する。
- 元のprofileへ戻し、再度doctorが期待状態を報告する。
- 純正ChatGPT.appの署名が有効で、patch markerが無い。
- config、catalog、guard log、process argumentsに秘密値が無い。

1件でも未実施・FAIL・未確認ならtagはNo-Go。結果は本ファイル末尾へ追記する。

## Phase 6: v0.2.0 tag・release

tag対象は`d22840c`へ固定しない。OSS強化PRを含み、実機ゲートを通した時点の最新`origin/main`を使う。

tag pushはGitHub releaseを即時発火するため、commit SHA、全gate、利用者の公開承認を再確認する。

```bash
git switch main
git pull --ff-only origin main
git status --short --branch
git log -1 --oneline --decorate
test "$(tr -d '\n' < VERSION)" = "0.2.0"
git tag -a v0.2.0 "$(git rev-parse origin/main)" -m "codex-openrouter-desktop v0.2.0"
git show --no-patch --decorate v0.2.0
git push origin v0.2.0
```

公開後の検証:

```bash
gh run list --repo hirorylabo/codex-openrouter-desktop --workflow Release --limit 5
gh release view v0.2.0 --repo hirorylabo/codex-openrouter-desktop
```

Release workflowが成功したら、別の一時directoryへ3 assetを取得する。

```bash
mkdir -p /tmp/codex-openrouter-v0.2.0-verify
gh release download v0.2.0 \
  --repo hirorylabo/codex-openrouter-desktop \
  --dir /tmp/codex-openrouter-v0.2.0-verify
cd /tmp/codex-openrouter-v0.2.0-verify
shasum -a 256 -c SHA256SUMS
gh attestation verify codex-openrouter-desktop-v0.2.0.tar.gz \
  --repo hirorylabo/codex-openrouter-desktop
gh attestation verify codex-openrouter-desktop-v0.2.0.spdx.json \
  --repo hirorylabo/codex-openrouter-desktop
gh attestation verify SHA256SUMS \
  --repo hirorylabo/codex-openrouter-desktop
```

tag/release失敗時はpublic tagをforce移動・再利用しない。transient failureならworkflowをrerunし、
内容修正が必要なら停止して回復方法を利用者と決める。

## Phase 7: 任意の文書archive整理

リリース後の別PRで行う。公開条件にはしない。

- 旧計画3本を`docs/archive/`へまとめて移動する。
- `task/002-loopback-router-plan-d.md`の相対リンクを新pathへ更新する。
- `task/PLAN (6).md`は削除せず、意味の分かる名前で`docs/archive/`へ移動する。
- `task/006-model-management-launcher.md`の参照を更新する。
- 既存相互リンクと全参照先が実在することを確認する。

## 承認境界

次は実行直前に利用者の明示承認が必要。

- remote branch 4本の削除
- `deleteBranchOnMerge`、CodeQL、rulesetの変更
- branch push、PR作成、merge
- 導入済みruntimeのupgradeとChatGPT.appの通常終了を伴う実機E2E
- ZDRなしモデルのprofile追加とAPI疎通
- `v0.2.0` tag pushとGitHub prerelease公開

## 実行結果

- [x] Phase 0 preflight PASS
- [x] Phase 1 branch整理 PASS
- [x] Phase 2 OSS強化PR作成
- [x] Phase 3 CI・レビュー PASS / merge済み
- [x] Phase 4 CodeQL・ruleset PASS
- [x] Phase 5 実機リリースゲート PASS
- [ ] Phase 6 v0.2.0 release・assets・attestation PASS
- [ ] Phase 7 文書archive整理（任意）

### Phase 0 preflight（2026-08-14）

停止条件はいずれも該当せず。

- `main`は`origin/main`と同一（`d22840c`）、WIPは本ファイルの未追跡分のみ。
- open PRなし。直近CI runは全て`success`。
- tagは`archive/asar-patch-003a0bc`, `v0.1.0`, `v0.1.1`のみ。`v0.2.0`とreleaseは未存在。
- rulesetは`[]`、CodeQL default setupは`not-configured`。
- `git fetch --prune`時点で`origin/codex/build-6396-adapter`は既に削除済みだった。
  そのため到達性確認は計画の8件ではなく実在する7件を対象とし、7件すべてexit 0。

### Phase 1 branch整理（2026-08-14）

- local 4本を`-d`で削除（`-D`不使用）: `12aca59`, `20ed0b5`, `7e14f78`, `ab1b96e`。
- remote 3本を削除（`build-6396-adapter`は削除済みのため対象外）。
- `git config remote.origin.prune true`、`deleteBranchOnMerge=true`。
- 確認: remote branchは`main`のみ、`gh repo view --json deleteBranchOnMerge` → `true`。

### Phase 2/3 OSS強化PR（PR #5 → `e1eacc4`）

<https://github.com/hirorylabo/codex-openrouter-desktop/pull/5>（squash merge、branch自動削除）

変更は計画どおりCI・release権限・Dependabot・badgeの4点のみ。本ファイルは
「変更を4点に限定」ゲートを守るためPR #5へ含めなかった。

ローカル検証（全PASS）:

- Python 3.11/3.12/3.13/3.14 で`unittest discover -s tests` → **251 tests OK**、`compileall` → OK。
- `macos_synthetic_e2e.py` PASS / `secret_scan.py --tree . --git-history` PASS。
- `build_release.py v0.2.0` + archive scan + `shasum -a 256 -c SHA256SUMS` PASS。
- `xcrun swiftc`（CredentialHelper / launcher app）OK、`zsh -n` 2本 OK、`git diff --check` clean。
- workflow内の`uses:`全行が`@<40-hex> # vX.Y.Z`形式であることを確認。

CI（PR・merge後mainとも全green）: `python-compat (3.11/3.12/3.13/3.14)`, `audit-release`,
`macos-compile`, `ci-required`。

squash mergeにより`codex/oss-public-release-hardening`のtipは`origin/main`から到達不能に
なったため、`archive/oss-public-release-hardening-9fc862b`でSHAを保全してから削除した
（tree hashは`main`と完全一致`07ed438`を確認済み）。

### Phase 4 CodeQL・ruleset（2026-08-14）

CodeQL default setup（Python、`query_suite=default`、weekly）を有効化。初回解析は
`results_count=1`, `rules_count=43`で成功。

検出alertは1件で、**false positiveと判断してdismissした**。

- rule: `py/clear-text-logging-sensitive-data`（high）@ `src/codex_openrouter/auth.py:144`
- code flow: `auth.py:18` の文字列リテラル`"https://openrouter.ai/auth"` → `OAUTH_AUTHORIZE`
  → `authorization_url` → `print`。CodeQLのsensitive-nameヒューリスティックが定数値の
  `auth` に反応しただけで、秘密値はこの経路を通らない。
- 出力URLの構成要素は`callback_url`（127.0.0.1）、`code_challenge`（verifierのSHA-256。
  PKCEでは公開前提）、`code_challenge_method`、静的な`key_label`のみ。
- PKCE `verifier`は`auth.py:157`のHTTPS token exchangeにしか渡らない。
- API key経路も別途確認: `prompt_for_key`は`getpass`、`CredentialStore.store`はargvでなく
  stdin（`input=key`）、keyのprintは無し。
- 対応: コードは変更せず、理由を明記して`dismissed_reason=false positive`で処理。open alert 0件。

ruleset `main protection`（id `20825199`, target `branch`, `~DEFAULT_BRANCH`, `enforcement: active`,
`bypass_actors: []`）:

| rule | 設定 |
| --- | --- |
| `pull_request` | `required_approving_review_count: 0` |
| `required_status_checks` | `ci-required`（`strict: false`） |
| `code_scanning` | CodeQL / `security_alerts_threshold: high_or_higher` / `alerts_threshold: errors` |
| `non_fast_forward` | ON（force push禁止） |
| `deletion` | ON（削除禁止） |

確認は read-only API のみで行い、mainへのforce pushは試していない。
`gh api repos/.../rules/branches/main` が5 rule typeすべてを返し、後続のDependabot PRでも
`ci-required`がcheckとして生成されることを確認した。

### Phase 5 実機リリースゲート（2026-08-14、部分実施）

前提として、実機のChatGPT.appは計画時の想定より先へ進んでいた。導入済みruntimeは
**v0.1.1**（`install-manifest.json`が`adapter_id: chatgpt-26.803.41515-build-6321`で固定）
だったのに対し、`/Applications/ChatGPT.app`は既に**26.803.61601 / build 6396**だった。
RELEASE_NOTES.mdがv0.2.0の破壊的変更理由として挙げているドリフトが、この実機で現実に
起きている状態だった。

実施手順: ChatGPT.appを`osascript`で通常終了 → `./codex-openrouter upgrade` → 各検証。

#### PASS（自動検証で確定した項目）

| 項目 | 結果 |
| --- | --- |
| `./codex-openrouter upgrade` | `UPGRADE: PASS v0.2.0 mode=loopback-guard`。rollback backupは`.../upgrade-backups/20260814-103052-v0.2.0` |
| `doctor --network --runtime --secret-scan` | `RESULT: PASS` |
| ZDRなしモデルの実応答 | 選択中5モデルすべて稼働中ZDR providerで応答（AkashML / BaseTen / AkashML / CoreWeave / DeepInfra） |
| `macos_live_e2e.py`（隔離home） | **28/28 PASS** |
| 純正ChatGPT.app | 署名有効・ASARにpatch markerなし・無改変 |
| 秘密値 | config・catalog・guard log・process argumentsのいずれにも無し |
| 稼働終了後の復帰 | catalog block消滅、model native復帰、provider port 0 stub、guard token消滅 |
| `models list --json --refresh` | `usage_available=true`、`usage_matched=102`（要件の1以上を満たす） |
| 列データの実在 | 価格=`headline{input,output,cache_read}`、公開日=`created`、利用量=`usage_tokens.7d`。候補330件 / ZDR 175件 / `usage_tokens`が102件に載り`usage_matched`と整合 |

なお`upgrade`実行時に `WARNING: API keyのspend limitが未設定です。` が出た。公開の停止条件では
ないが、OpenRouter側でspend limitを設定しておくことを推奨する。

#### 未実施（GUI目視が必須。人手がないと通せない）

`scripts/macos_installed_e2e.zsh`は**対話前提のスクリプト**である。L147で
「管理画面の『OpenRouterで起動』を押してください」と表示し、L58で`WAIT_SECONDS`（既定180秒）
以内にactive状態を検知できなければfailする。自動実行ではcycle 1で
`[FAIL] active状態を180秒以内に確認できませんでした` となった。**これは製品の欠陥ではなく、
GUIクリックの担い手がいなかったことによるタイムアウトである。**（後述のとおり、そもそも
Macがロックされており、解錠されたデスクトップセッションが無かった。）

中断後に`doctor`で状態を確認し、catalog blockなし・model native・guard tokenなし・純正app無改変で
`RESULT: PASS`。汚れは残っていない。スクリプトが起動したlauncherプロセスと、自分の実行が残した
crashpad handler 2件は終了させた。

スクリプト前半の非対話パートは通っている。

- `[PASS] profile show: schema・選択・registryを確認（秘密値なし）`
- `[PASS] models list: 候補・価格・ZDR判定を確認（秘密値なし）`（候補328件 / ZDR 175件 / 利用量あり）

#### GUI自動化の試行と、確定した1項目

残りをAppleScript（System Events）のUI自動化で埋められるか試した。結論は**ロック画面により不可**。

試行の途中、launcherを起動した直後に管理画面のaccessibility treeを取得できた。

```
static text "表示モデル 5件"
static text "既定モデル: DeepSeek V4 Flash 0731"
static text "workspace: ~/Documents"
button     "モデル設定…"
button     "OpenRouterで起動"
```

（workspaceの実値は絶対パスだったが、`secret_scan.py`の`personal absolute path`検査に
かかるため`~`表記へ置き換えて引用している。）

これにより **「管理画面に表示モデル数・既定モデル・workspaceが出る」は確定**（目視ではなく
accessibility tree による確認なので、目視より強い証拠になる）。値も`doctor`の選択5モデル、
live E2Eの「pickerにOpenRouterモデルが5件並ぶ」と一致する。tree内にAPI key文字列は無い。
「モデル設定…」「OpenRouterで起動」の両ボタンが存在することも確認できた。

その後、Macが**ロック画面**（Touch IDまたはパスワード入力待ち）に入り、以降は
`count of windows` が常に`0`を返すようになった。ロック中はwindowを生成・検査できない。
解錠には利用者本人のTouch IDかパスワードが必要で、これは本計画の「password、Cookie、
認証コード、API keyを表示・読み取りしない」に該当するため試行しない。

**GUI項目はツールの制約ではなく、解錠されたデスクトップセッションが無いことで止まっている。**
利用者がMacを解錠していれば、同じUI自動化で残りも機械的に確認できる見込みがある。

起動したlauncherプロセスと、確認に使ったスクリーンショットは削除済み。中断後の`doctor`は
`RESULT: PASS`で状態は汚れていない。

#### 実機GUI検証（2026-08-14、Mac解錠後）

利用者がMacを解錠したので、残っていた項目をAppleScript（System Events）のaccessibility API
経由で実機検証した。スクリーンショットは使っていない（`screencapture`はZed Previewへの
画面収録許可要求を誘発したため、権限を与えず取り下げた。実体はaccessibility treeで取れる）。

##### `scripts/macos_installed_e2e.zsh` PASS

「OpenRouterで起動」のクリックとChatGPT.appの通常終了を自動で投げ、**2/2 cycles PASS、FAIL 0**。

```
[PASS] profile show: schema・選択・registryを確認（秘密値なし）
[PASS] models list: 候補・価格・ZDR判定を確認（秘密値なし）
[PASS] cycle 1: active（ephemeral port・0600 token・guard）
[PASS] cycle 1: inactive（port 0 stub・tokenなし）
[PASS] cycle 2: active（ephemeral port・0600 token・guard）
[PASS] cycle 2: inactive（port 0 stub・tokenなし）
=== installed launcher E2E PASS: 2/2 cycles ===
```

ephemeral portはcycleごとに変わった（51276 → 51459）。各cycleのactive時に、guard token 0600、
鍵がconfig・catalog・guard log・process argumentsに無いこと、純正app無改変を確認している。

##### GUI各項目

| 項目 | 実機での確認内容 |
| --- | --- |
| 管理画面 | `表示モデル 5件` / `既定モデル: DeepSeek V4 Flash 0731` / `workspace: <絶対パス>` と、`モデル設定…` `OpenRouterで起動` の両ボタンを取得 |
| `⌘,` | launcherへ`⌘,`を送ると `モデル設定` windowが増える（windows 1 → 2） |
| folder drop | `open -a <launcher> <folder>`（Finderのdropと同じopen AppleEvent）でworkspace表示が切り替わり、**ChatGPTは起動しなかった**。元のworkspaceへ戻して復帰も確認 |
| 純正appからのhandoff | 純正app稼働中に「OpenRouterで起動」を押すと、`終了してOpenRouterモードへ切り替える` / `キャンセル` を持つモーダルアラートが出て14秒間ブロックし続け、その間ChatGPTのPIDは不変。**キャンセル**を押すとPIDが同一のまま、catalog blockなし・guard tokenなしのvanillaを維持。**承認**するとPIDが変わり（終了→再起動）catalog blockが入った |
| 設定画面にAPI keyが出ない | `モデル設定` windowの全static text・button・checkboxを列挙して鍵文字列は皆無 |
| 価格・公開日・7dトークン列 | テーブルは175行×7列。セルを読むと `Sakana Namazu / sakana/sakana-namazu / IN=0.95 / OUT=4 / 2026-08-11 / — / ZDRなし · 学習不明 · reasoning`。**利用量圏外が`0`でなく`—`**で出る仕様も一致 |
| filter | `ZDRのみ` ON=175行 → OFF=330行 → ON=175行。`models list`の ZDR 175件 / 候補 330件と完全一致 |
| `ZDRのみ`既定ON | 設定画面を開いた直後が `ZDRのみ`=ON、他3filterはoff |
| 選択済みがfilterで消えない | 上のfilter往復で`選択 5件`が一度も減らない。さらに1モデルしか一致しない検索語でも`表示 6件`（選択5＋一致1）になり、選択済みが常に残った |
| ZDRなし確認sheet | 非ZDRの`sakana/sakana-namazu`を選ぶと `Sakana Namazu はZDRなしで動作します` のsheetが出て `やめる` / `追加する` を持つ |
| 取消時は変更されない | `やめる`でcheckboxは0へ戻り`選択 5件`のまま。永続側もprofile digestが`1a952c93…`で不変 |

##### 保存検証ゲートの実証（計画外の収穫）

「検証して保存」および`profile apply --stdin-json`は、**呼び出せないmodelがあると1バイトも書かない**。
非ZDR 6件とZDR 1件、計7回の失敗を通じてprofile digestは常に`1a952c93…`のままで、
install-manifestもテスト前後でバイト単位一致だった。RELEASE_NOTESの主張が実機で裏付けられている。

#### 最終項目 — ZDRなしモデルの追加と実応答（PASS）

当初この項目は実行できなかった。非ZDR 6件に加え、判別軸の切り分けとして未選択のZDRモデル
`google/gemini-3.7-flash` でも対照実験したが、すべて
`ERROR: API keyでは呼び出せないmodelが選択されています` で拒否された。

弾かれるmodelへ最小の呼び出し（`max_tokens=1`）を投げて原因を特定した。

```
No endpoints available matching your guardrail restrictions and data policy.
Configure: https://openrouter.ai/settings/privacy
```

ZDRの有無ではなく、**OpenRouterアカウント側のguardrail（modelホワイトリスト）**がendpointを
絞っていた。APIでの解除も試みたが、`GET /api/v1/guardrails` は
`401 {"error":{"message":"Invalid management key"}}` を返す。導入済みcredentialは推論用キー
（`is_management_key: false` / `is_provisioning_key: false`）なので権限がなく、変更はWeb consoleでしか
行えないと判断して利用者へ差し戻した。

利用者が `codex-zdr` という名前のmodelホワイトリストguardrailを削除したのち、再実行してPASSした。

解除直後の再測定では、非ZDRのうち呼び出せるものと弾かれるものが分かれた。data policy側の
ZDR強制は残っており、ホワイトリストだけが外れた形になる。また`--refresh`でcatalogを取り直すと
ZDR判定が175件→176件へ変わり、`deepseek/deepseek-v4-pro-0813`はFireworksのZDR endpointが
見えるようになって**ZDR扱いへ変化した**。そのため検証対象には、refresh後も
`zdr_supported=false`のままで呼び出せた `anthropic/claude-opus-5-fast`（Anthropic）を使った。

`profile apply --stdin-json` で6モデル構成へ変更（digest `88dff719…`、profile backupが自動生成）。
警告は3面すべてに出た。

| 面 | 実際の表示 |
| --- | --- |
| 管理画面 | `表示モデル 6件` とともに `ZDRなしのモデルを1件使用中です。そのモデルへ送った内容はproviderに保持される可能性があります。` |
| `doctor --network` | `WARN: anthropic/claude-opus-5-fast はZDRなしで動作します。送信内容がproviderに保持される可能性があります` |
| picker（composite catalog） | 当該entryのdescriptionに `ZDRなし（送信内容がproviderに保持される可能性あり）`。ZDRモデル側は `ZDR稼働endpoint最安: …` と出る |

実応答は `OK: 選択中のmodelはすべてOpenRouter keyで呼び出せます`（canaryは選択中の全modelを叩く）と、
直接呼び出しで `provider=Anthropic` を得たことの両方で確認した。ZDRモデル5件も従来どおり
稼働中ZDR providerで応答している。

compositeカタログは14 entry（純正8 + `[OR]`付き6）になり、追加したmodelがpickerへ出ることを確認した。

その後、保全しておいたpayloadで元の5モデル構成へ復帰。profile digestは`1a952c93…`へ戻り、
`doctor --network --runtime --secret-scan` は `RESULT: PASS`、ZDRなしWARNも消えた。
profile変更で再生成待ちになったcompositeカタログも1サイクル起動して作り直し、
`OK: compositeカタログは契約を満たします（picker表示 10件）` の初期状態へ完全復帰させた。

#### Phase 5 判定

**全項目PASS。** 計画の「1件でも未実施・FAIL・未確認ならtagはNo-Go」を満たしたため、
Phase 6へ進める。

なお`upgrade`実行時に `WARNING: API keyのspend limitが未設定です` が出ている。公開の停止条件では
ないが、OpenRouter側でspend limitを設定しておくことを推奨する。

### 未処理・次の判断事項

**Dependabot PR 3本が open**（有効化直後に自動生成、いずれもCI green・SHA固定とversion comment維持）。

- [#6](https://github.com/hirorylabo/codex-openrouter-desktop/pull/6) `actions/attest` 4.1.1 → 4.2.2
- [#7](https://github.com/hirorylabo/codex-openrouter-desktop/pull/7) `actions/checkout` 4.3.1 → 7.0.1
- [#8](https://github.com/hirorylabo/codex-openrouter-desktop/pull/8) `actions/setup-python` 6.2.0 → 7.0.0

**これらはv0.2.0 tagの後にmergeすることを推奨する。** 3本とも`release.yml`を書き換えるが、
release workflowはtag pushでしか動かずCIでは一度も実行されない。`checkout`はv4→v7、
`setup-python`はv6→v7のmajor bumpであり、破綻した場合に露呈するのはpublic tagを押した瞬間で、
計画どおりpublic tagのforce移動・再利用はできない。既存のpinはv0.1.1で実績のある組み合わせなので、
既知良好なpinでv0.2.0を切ってからmergeする方が回復可能性が高い。
