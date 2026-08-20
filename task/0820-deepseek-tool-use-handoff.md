# DeepSeek V4 Flash 0731 Tool Use 最適化 handoff

作成日: 2026-08-20

対象branch: `codex/openrouter-tool-bridge`

base: `origin/main` (`16bc42746719c4bd1fd52e22e4c847faa8249c65`)

実装HEAD（このhandoff追加前）: `7ea3a4977904d87e7aff0bcc7ca6ad5bff8d5ba6`

Status: **Claude Cloudでの最小実装とlocal検証待ち。実機E2Eは未合格扱い。**

## Goal

OpenRouterの`deepseek/deepseek-v4-flash-0731`をCodex Desktopで使う際、
core direct toolを正確・再現可能・観測可能にする。

完成条件は、専用launcherでDeepSeekが自動選択され、5種類の代表tool taskを
2回のfresh runでexact監査できること。browser、search、Node REPL、Code Modeを
native GPTと同等にすることは今回のgoalではない。

既存の3-tool E2E合格だけを最終目的にしない。正確性を先に固定し、その後に
latency・token・provider routingを安全なmetadataだけで評価する。

## Current State

### Git

- working treeはhandoff追加前にclean。
- open PRは0件。
- branchは`origin/main`より次の5 commits進んでいる。
  - `688c4bf` Tool Bridge、tool互換UI、catalog/profile、tests
  - `fd361a3` 外部通信遮断runner、upstream監視
  - `34ab136` OrcaRouter中止とOpenRouter専用方針
  - `96e3bf3` launcher更新と実機E2Eのfail-closed化
  - `7ea3a49` zshのreadonly予約変数`status`衝突修正

### 2026-08-20のread-only実機確認

```text
ChatGPT=26.814.41407 build 6720
tool contract=2
profile models=1
default=deepseek/deepseek-v4-flash-0731
default effort=high
openrouter_active=false
stock model=gpt-5.6-sol / provider=openai
ChatGPT process=stopped
launcher process=stopped
source digest=b653d6ab209417b21af01f5234b638f5bffe196f22bb97a6edb98b1b471aa14d
installed digest=b653d6ab209417b21af01f5234b638f5bffe196f22bb97a6edb98b1b471aa14d
doctor --runtime --secret-scan=PASS
Keychain=missing
```

doctorのWARNは、親shellに`OPENROUTER_API_KEY`がexportされている1件。launcherと
supervisorは起動前に外す。値を読んだりhandoff・logへ書いたりしないこと。

`state/tool-compatibility.json`にはbuild 6720 / contract 2 / `verified` /
provider `DeepInfra` / attempt 2のcacheが残る。ただしKeychainは現在missingであり、
このcacheをfresh認証・provider固定・実機E2E合格の証拠にしない。

`install-manifest.json`の`source_commit`は`34ab136`のままだが、content digestは
sourceとinstalledで一致する。manifestのcommit値だけからruntime内容を推定しない。

### 実装済み

- `src/codex_openrouter/toolbridge.py`
  - 通常functionはpassthrough。
  - namespace childとcustom toolだけをrequest固有のstrict functionへ変換・復元。
  - unknown tool、不完全JSON、欠落done、途中切断をfail-closed。
  - tool contract versionは2。
- `src/codex_openrouter/guard.py`
  - tool requestだけRouter Metadataを要求。
  - provider、attempt、candidate count、statusだけを抽出。
  - Auto Exactoを維持し、provider pinや価格sortを追加しない。
- `src/codex_openrouter/toolcompat.py`
  - structured functionとfreeform/customの2 canaryを24時間cache。
- `scripts/macos_installed_e2e.zsh`
  - lifecycle 2 cycle。
  - `pwd`、`apply_patch`、`list_mcp_resources`の3 gates。
  - wrong cwd、resume、余分なcall、retry、fallbackをJSONL監査で拒否。

### 未完了

- fresh runの最終行
  `installed launcher E2E PASS: lifecycle 2/2 / tool 3/3 / retry 0`
  は、修正版HEADでは未確認。実機E2Eは未合格として扱う。
- 単一model profileでも、cleanup後のnative modelと
  `pending_default_model=false`の組み合わせでは、次の専用起動時にDeepSeekが
  自動選択されない。
- 依存する複数tool call、parallel tool call、最終回答の正確性はlive auditor未対応。
- tool requestのlatencyとtoken usageをprivacy-safeに集計できない。

## Start Here

```bash
git status --short --branch
git log --oneline origin/main..HEAD
sed -n '1,400p' task/0820-deepseek-tool-use-handoff.md
PYTHONPATH=src python3 scripts/run_unit_tests.py
```

最初の3コマンドでbranch・差分・handoffが一致しない場合は、編集せず原因を報告する。

## Files To Read First

1. `README.md`の「モデル設定画面」「最小Tool Bridge」「開発」
2. `src/codex_openrouter/supervisor.py`の`apply_config`
3. `src/codex_openrouter/toolbridge.py`
4. `src/codex_openrouter/guard.py`
5. `src/codex_openrouter/toolcompat.py`
6. `scripts/macos_installed_e2e.zsh`
7. `scripts/macos_installed_e2e_audit.py`
8. 対応する`tests/test_*.py`

`task/0819-orcarouter-second-router-plan.md`は履歴資料。末尾に古いruntime状態が混在するため、
現在値は本handoffを優先する。

## Files Likely To Edit

- `src/codex_openrouter/supervisor.py`
- `src/codex_openrouter/guard.py`
- 必要な場合だけ`src/codex_openrouter/toolbridge.py`
- `scripts/macos_installed_e2e.zsh`
- `scripts/macos_installed_e2e_audit.py`
- 上記に直接対応するtests
- 挙動が変わった箇所だけ`README.md` / `README.en.md`

## Required Changes

### 1. 単一model時の自動選択

profileのmodel数が1件のときだけ、専用OpenRouter起動ごとにそのmodelと
`model_provider=openrouter`を選択する。複数model profileでは既存の
`pending_default_model`契約を維持する。

追加test:

- 単一model + native current model + pending falseでもDeepSeekへ切り替わる。
- 複数model + pending falseでは既存modelを勝手に変えない。
- cleanup後はnative model/providerへ戻る。

### 2. privacy-safe tool telemetry

toolを含むupstream requestについて、既存の安全なrouter fieldsに次を追加する。

- `tool_request=true`
- `duration_ms`
- responseに存在する整数のinput/output/cache token数

禁止する記録:

- prompt、response本文
- tool名、tool arguments、tool output
- API key、authorization、guard token
- `openrouter_metadata`本文、pipeline

metadataが無い認証失敗・429・5xxをtool非対応に分類しない。整数以外や未知shapeは
推測せず省略する。追加fieldごとに非漏洩testを置く。

### 3. 5-gate E2E auditor

既存3 gatesを維持し、次の2 gatesを追加する。

4. 先行するread結果を次のtool callへ使う、依存した2-call turn
5. 相互に独立したread-only commandを2件呼ぶparallel turn

各gateで以下をexact監査する。

- `originator=Codex Desktop`
- `model_provider=openrouter`
- exact workspace
- expected tool count、name、arguments、output
- task完了後の最終回答
- markerは1回だけ
- retry、fallback、余分なtool callなし

parallel gateはcall順に依存せず集合で比較する。filesystemを競合更新するcallは使わない。

## Verification Already Run

2026-08-20、このhandoff追加前に次を確認した。

```text
./codex-openrouter check                              PASS
installed profile show --json                       PASS
installed doctor --runtime --secret-scan             PASS (WARN 1)
source/installed runtime_digest                       MATCH
exact ChatGPT pgrep                                   exit 1
exact launcher pgrep                                  exit 1
```

機能branchの過去記録にはrepository test PASSがあるが、handoff追加後・追加実装後の
current resultとして再利用しない。

## Verification To Run

Claude Cloud側で、外部課金requestなしに以下を通す。

```bash
PYTHONPATH=src python3 scripts/run_unit_tests.py
PYTHONPATH=src python3 -m compileall -q src portable scripts
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py
uvx ruff@0.16.3 check .
python3 scripts/secret_scan.py --tree .
python3 scripts/check_upstreams.py --validate-only
zsh -n scripts/macos_installed_e2e.zsh
git diff --check
```

macOS toolchainが使える環境では追加する。

```bash
xcrun swiftc portable/launcher/app/*.swift -o /tmp/CodexOpenRouterLauncher
xcrun swiftc -parse-as-library \
  portable/launcher/app/ProfileBridge.swift \
  portable/tests/DecoderCompatTests.swift \
  -o /tmp/decoder-compat && /tmp/decoder-compat
```

## Real-device Gate

Claude CloudではGUI・local Keychain・stock ChatGPT.appを仮定しない。local検証完了後、
Mac側で別の明示承認を得て実施する。

- baselineはreasoning effort `high` + Auto Exacto。provider pinなし。
- Run 1: 新しい空workspace、5 GUI prompts、lifecycle 2/2、same-run retry禁止。
- Run 1が5/5の場合だけ、別承認のfresh Run 2へ進む。
- 完成判定: lifecycle 4/4、tool 10/10、retry 0。
- correctnessが不足する場合だけ`max`、correctness維持で速度・tokenが悪い場合だけ
  `low`を問題gateに限定して比較する。A/Bは別承認、最大4 prompts。

OpenRouter Responsesはstatelessなので、比較runではfresh chatを使い、履歴再送による
input token増加をtelemetryから分離する。

一次資料:

- https://openrouter.ai/deepseek/deepseek-v4-flash-0731
- https://openrouter.ai/docs/guides/routing/auto-exacto
- https://openrouter.ai/docs/guides/features/router-metadata
- https://openrouter.ai/docs/api-reference/responses/overview

## Known Failures / Blockers

- 現在のKeychain statusは`missing`。live canaryや有料E2Eを開始できない。
- 過去にexact bundle openが
  `LSCopyApplicationURLsForBundleIdentifier() failed`で停止した。登録操作や別経路openを
  自動fallbackにしない。
- zshの`status`予約変数衝突は`7ea3a49`で修正済み。再発testを維持する。
- `apply_patch`指定gateをshell write成功で代替してPASS扱いしない。
- browser/search/Node REPLをnamespace child検証の代替にしない。

## Do Not

- `/Applications/ChatGPT.app`、ASAR、署名、userDataを変更しない。
- API keyの値を読み出す、表示する、logへ残す、PRへ含めることをしない。
- 有料request、GUI起動、Keychain変更を自動で行わない。
- providerを単発の`DeepInfra attempt 2`だけで固定しない。
- Auto Exactoへ価格sortを追加しない。
- browser、search、Node REPL、Code Modeを有効化して互換扱いしない。
- unknown toolや壊れたJSONを推測修復しない。
- 同一runでretry、別launcher経路、shell fallbackを行わない。
- OrcaRouterコードを復活させない。
- 無関係なrefactor、release、tag、mergeを行わない。

## Next Steps

1. 現行testをbaselineとして実行する。
2. 単一model自動選択testを先に追加し、最小修正で通す。
3. telemetryの非漏洩testを追加し、最小fieldだけ実装する。
4. auditorの依存・parallel・最終回答testを追加し、5-gate harnessへ反映する。
5. 全local verificationを通し、diffとsecret scanを再確認する。
6. docsは実際に変わった挙動だけ更新する。
7. commit・pushしてPRへ結果を追記する。実機gateはMac担当へ戻す。

## Required Final Report

日本語で次だけを簡潔に報告する。

- 結論: local実装完了 / blocker
- 変更fileと理由
- test結果（passed数を含む）
- 未実施の実機gateと理由
- 有料request数、GUI prompt数、retry数
- secret非漏洩確認
- commit SHAとPR URL
- Mac担当が最初に実行するexact command

## Claude Cloud Start Prompt

```text
/goal
OpenRouter DeepSeek V4 Flash 0731のCodex Desktop direct tool利用を、
自動選択・privacy-safe telemetry・5-gate監査まで最小差分で仕上げる。

<read first>
task/0820-deepseek-tool-use-handoff.mdをsource of truthとして全文読む。
README.mdとhandoff記載の実装・testだけを確認する。

<tasks>
handoffのRequired Changes 1〜3をtest-firstで実装し、全local verificationを通す。

<do not>
有料request、GUI、Keychain、stock app、provider pin、OrcaRouter、release、mergeには触れない。
失敗時にfallbackや同一run retryをしない。

<final report>
handoffのRequired Final Report形式で報告し、実機gateはMac担当へ戻す。
```
