# 0822 codex-router を一定期間の日常運用に導入する（自作実装は保全）

作成日: 2026-08-22 / 対象: `main` / Status: **trial 稼働中**

> [!NOTE]
> 本 repo の実装コードは一切変更していない。追加したのはこの note 1ファイルのみ。
> 自作実装の install は idle のまま残してあり、`bin/disable` 一発で戻せる。

## Context

PR [#24](https://github.com/hirorylabo/codex-openrouter-desktop/pull/24) は実機 gate 2 で停止中。
原因は [`0821-opencodex-walkthrough.md`](./0821-opencodex-walkthrough.md) §1.5 のとおり、catalog が
純正テンプレートから `use_responses_lite = true` を継ぎ、Tool Bridge が一度も起動していなかったこと。

[`duolahypercho/codex-router`](https://github.com/duolahypercho/codex-router)（MIT / 2,649★ /
2026-07-19 作成）は同じ到達点（純正 ChatGPT.app 無改変のまま picker へ外部モデルを並べる）を
別の賭けで実現している。日常運用しながらその別解を実地評価する。

### 我々との設計差

| 軸 | 本 repo | codex-router |
| --- | --- | --- |
| wire | Responses 直送、tool wire だけ変換 | **LiteLLM で Chat Completions へ落とす** |
| `use_responses_lite` | `KNOWN_TEMPLATE_FIELDS` にあるだけで中和対象外（＝§1.5 のバグ） | **`false` を強制**（`src/catalog.mjs:592`） |
| `tool_mode` | `"direct"` を明示 | **削除**（"not a routed capability"） |
| 未知 build | fail-closed（OpenRouter 起動だけ止める。native は無事） | `codexVersion()` + fingerprint で **自動再取得**。失敗時は stale を警告付きで継続（fail-open） |
| service 停止時 | provider table は stub、**native は通常どおり** | `openai_base_url` が loopback のため **native も止まる** |
| 未知フィールド検知 | `KNOWN_TEMPLATE_FIELDS` の件数で検知できる | 相当する網なし |

`use_responses_lite: false` の強制は実機の生成 catalog で確認済み（後述の観測ログ）。
**中和案には実在の先例がある**が、flag 名が変われば黙って外れる点は変わらないため、
0821 §5 の案 B（wire 側で `additional_tools` も収集する）の優位は崩れていない。

## 導入内容

| 項目 | 値 |
| --- | --- |
| clone | `~/.local/share/codex-router` @ `47d67626cfca29c5112e65aa8226e7a50baa3308` |
| router version | `0.4.0-beta.4` |
| 導入方式 | `git clone` → `bin/install --prepare-only` → `bin/setup --providers openrouter,grok-oauth`。**`curl \| sh` は不使用** |
| provider | `openrouter`（key は保護ファイル `~/.codex/codex-router/openrouter-api-key.secret`、mode 600）、`grok-oauth`（既存 Grok CLI session を自動検出） |
| curated model | `openrouter/deepseek/deepseek-v4-flash-0731` / `-pro-0813` / `openrouter/moonshotai/kimi-k3`（いずれも ctx 1M、OpenRouter 公称値を採用） |
| picker 可視 | 上記3件 + `grok-oauth/grok-4.5` / `grok-4.6` = 5件 |
| picker 非表示 | `custom/qwen3.8-27b`（個人公開の匿名 HF endpoint。方針により除外）、`gpt-5.6-sol-1m` |
| ChatGPT.app | `26.818.21641` build `6849`（無改変） |

### 意図的に採らなかったもの

- **Cloudflare**: codex-router に provider 実装なし（`cloudflare` の出現は upstream 520-524 の
  retry 判定のみ）。`baseUrlEnv` 機構はあるが、macOS の LaunchAgent が環境変数を固定 allowlist で
  書き出すため（`src/service-macos.mjs`）shell の export は常駐 service に届かない。local patch が要るため見送り
- **Anthropic**: 別課金の platform key が必要なため見送り
- **匿名 gateway**（`opencode-free` / `kilo-free` / `custom/qwen3.8-27b`）: アカウント関係がなく
  Prompt Training OFF・ZDR・spend limit が一つも効かないため、日常運用には入れない
- **`bin/shim`**: `~/.local/bin/codex`（standalone への symlink）と衝突するため実行しない

## 依頼していない挙動（発見して対処したもの）

| # | 挙動 | 対処 |
| --- | --- | --- |
| 1 | Grok CLI の OAuth session を自動検出し catalog に載せる | 利用者が許容。picker に表示 |
| 2 | **model failover が既定 ON**。失敗時に別 provider へ黙って退避する | **`control failover off` で停止。** 黙ってモデルが入れ替わると gate 5 の計測が読めなくなるため。戻すのは `control failover on` |
| 3 | **vision bridge が既定 ON**。text-only model に画像を貼ると `grok-oauth/grok-4.6` が代理で読み、Grok quota を消費する | 現状 ON のまま。停止は `control vision-bridge off` |

## config.toml 外の footprint

`config.toml` の変更は marker block 3つによる**純粋な追記**（自作実装の `codex-openrouter:provider`
block は無傷）。ただし marker の外にも書き込みがある。

- `~/.codex/codex-router/`（state 一式。key・catalog・litellm.yaml・secret 2種）
- `~/.codex/skills/` に 5 skill（`codex-router` / `codex-router-media` / `codex-app-threads` /
  `codex-computer-use` / `codex-in-app-browser`）。所有は `managed-skills.json` が追跡
- `~/.codex/agents/router-model-grok-oauth-grok-4-5.toml`
- `~/Library/LaunchAgents/io.github.codex-router.plist`（loaded、port 4200/4201/4202/4203/4208/4210）

`config.toml` の sha256: 導入前 `a685da5e…bddc1d6` → 導入後 `6b8af423…effa58d6`。
本文は個人の project path と MCP endpoint を含むため本 note には載せない。
退避先: `~/.local/share/codex-openrouter-trial/2026-08-22/`（repo 外）。

## 観測ログ

実測した行だけ埋める。予測は書かない。

| # | 確認 | 結果 | 観測 |
| --- | --- | --- | --- |
| 1 | picker に native GPT-5.6 と routed が両方出るか | 未実施 | ChatGPT.app 起動待ち |
| 2 | `bin/doctor` | **PASS** | routed 5件、gateway route 一致、venv・secret mode 600 すべて OK |
| 3 | native GPT が router 経由でも動くか | 未実施 | |
| 4 | service 停止で native GPT が死ぬか | 未実施 | 設計理解の裏取り |
| 5 | DeepSeek V4 Flash 0731 で `apply_patch` を通す | 未実施 | gate 2 相当 |
| 6 | ChatGPT.app 更新時に catalog 自動再取得が効くか | 未実施 | 次回更新を待つ |

### 生成 catalog の実測（2026-08-22）

routed entry 5件すべてが次の値を持つ。§1.5 のバグに対する codex-router 側の答えが実機で確認できた。

```
use_responses_lite = False      # 純正テンプレートの true を強制中和
tool_mode          = (削除)
apply_patch_tool_type = freeform
```

### gate 5 の読み方（重要）

`openrouter` という文字列は codex-router のソース全体で `src/provider-onboarding.mjs`（setup の UX）
にしか現れない。**request 側の provider 選好を一切送っていない**（`require_parameters` も
`zdr` flag もなし）。よって 0821 §1.7 で特定した「candidate 30 の provider 抽選」ノイズは
codex-router 経由でも同じように出る。

→ **gate 5 は非対称に読む。1回通れば「Chat Completions に落とせば解ける」の実証になるが、
失敗は「解けない」の証拠にはならない。** 抽選ノイズとの切り分けが要る。

緩和策は patch なしで一つある: OpenRouter アカウント側の provider preferences で endpoint を絞る。

## 復帰手順

codex-router 自身の機構を使う。自作スクリプトは書かない。

```bash
~/.local/share/codex-router/bin/disable          # managed block 除去 + service 撤去
diff ~/.local/share/codex-openrouter-trial/2026-08-22/config.toml ~/.codex/config.toml
./codex-openrouter launch                        # 自作実装へ復帰
```

`disable` は model / provider / profile 設定を保持する。marker 外の `model_catalog_json` /
`openai_base_url` が残っていると自作 launcher が `configblock.py:169-176` で fail-closed するため、
`diff` で差分ゼロを確認してから戻すこと。

完全撤去する場合はさらに `bin/uninstall` → `rm -rf ~/.local/share/codex-router` →
`~/.codex/codex-router/openrouter-api-key.secret` の削除。

## 既知のリスク

- **native GPT が router 依存になる。** 常駐 service が落ちると純正モデルも使えない
- **update 時 fail-open。** native catalog 再取得に失敗すると stale capture を警告付きで使い続ける。
  routed の tool 契約が新 build で壊れていないかを検証する機構はない
- **curated model は codex-router の互換 test 対象外**（README 明記）＝動作未検証
- **trial 中は自作 launcher が起動しない。** marker 外の `model_catalog_json` を検出して
  fail-closed する設計どおりの挙動であり、故障ではない

## Verification

```bash
# 導入の健全性
~/.local/share/codex-router/bin/doctor
~/.local/share/codex-router/bin/status

# 本 repo の local gate が影響を受けていないこと
PYTHONPATH=src python3 scripts/run_unit_tests.py
uvx ruff@0.16.3 check .
```
