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

---

# 追記（2026-08-22）: 実使用で出た4件の修正と patch の自動継承

実使用で4件出た。いずれも codex-router の既定値と curation の保守的な既定メタデータに起因する。

| # | 症状 | 原因 | 対処 |
| --- | --- | --- | --- |
| 1 | Grok が 401（`xai rejected the OAuth session`） | `~/.grok/auth.json` が 2026-05-27 のもので期限切れ | `grok login --oauth`（完了） |
| 2 | effort が「高」1段しか選べない | curation の保守的既定。`reasoningLevels` が1件 | モデル別 ladder を設定 |
| 3 | ZDR もコスパ最適も効かない | codex-router は OpenRouter へ `provider` block を一切送らない | local patch |
| 4 | 表示名 `deepseek/deepseek-v4-flash-0731 (curated)` が切れる | `` `${upstreamId} (curated)` ``（`src/user-models.mjs:91`） | 手編集 |
| 5 | forced `tool_choice` 対策が無い | curated entry に `requestProfile` なし | 同 patch 内で対応 |

> [!WARNING]
> **`doctor` は期限切れの Grok session を「OK」と報告していた。** ファイルの存在だけを見て
> 有効性を検証していない。catalog の fail-open と同系統の弱点。

## OpenRouter 仕様の確認（公式 docs）

「ZDR用 key と ZDRなし key を作って切り替える」案は**成立しない**。

- provider 選好は **per-request か account 全体のどちらかで、API key 単位ではない**
- ZDR の request-level 指定は account 設定と **OR**。account が ON なら per-request で緩められず、
  OFF にすると codex-router は何も送らないので ZDR が全面的に消える
- `reasoning.effort` は `max`/`xhigh`/`high`/`medium`/`low`/`minimal`/`none` を受ける。
  **未対応値は 400 にならず近い値へ写像される**。ただし Codex 語彙の **`ultra` は受けない**

### tool 互換 provider の絞り込みは「自動」だった

> When you send a request with `tools` or `tool_choice`, OpenRouter makes a best effort to route to
> providers known to support tool use. ... even when `require_parameters` is false, `tools`,
> `response_format` (including structured outputs), and `verbosity` are used as a soft preference
> ... this preference never removes a model from your request's candidate list.

つまり tool 対応での絞り込みは既に自動で、しかも候補を空にしない安全側の実装。
**`require_parameters: true` を既定にしてはいけない。** 全パラメータへの hard 制約で、
満たす endpoint が無いと 404 になる（zed#36094 / mastra#2839 / continue#3849 等で多数報告）。

`/api/v1/models/{id}/endpoints` の実測（2026-08-22）:

| model | endpoints | `tools` | `tool_choice` | `parallel_tool_calls` | `structured_outputs` |
| --- | --- | --- | --- | --- | --- |
| `deepseek-v4-flash-0731` | 31 | 31/31 | 31/31 | **1/31**（Inceptron のみ） | 22/31 |
| `deepseek-v4-pro-0813` | 14 | 12/14 | 12/14 | **0/14** | 7/14 |
| `moonshotai/kimi-k3` | 15 | 13/15 | 11/15 | **0/15** | 14/15 |

0821 §1.7 の「30 endpoint 中 1つしか `parallel_tool_calls` を公称しない」の裏取りになっている。
`use_responses_lite: false` により Codex は classic 形式で送る側なので、`require_parameters: true`
を足すと Flash は 1件に潰れ、**Pro と Kimi K3 は 0件＝404** になり得る。よって profile 側で
`parallel_tool_calls` を落とす。

また OpenRouter は `deepseek-v4-flash-0731` を **1,310,720** と公称しているが、curation は
1,048,576 を保存していた（`autoCompact` ごと修正）。

## patch 本体

`~/.local/share/codex-router/src/api-forwarder.mjs` の `const endpoint = endpointForModel(model);`
の直前へ、**else-if チェーンの外の独立ブロック**として挿入する。チェーンを延ばすと upstream の
分岐変更に弱いため、単一アンカーへの挿入にした。

```js
  // >>> codex-openrouter-trial:openrouter-provider-profiles >>>
  if (String(model.requestProfile || "").startsWith("openrouter-")) {
    const profile = String(model.requestProfile);
    payload.provider = {
      ...(payload.provider || {}),
      sort: "price",
      ...(profile.includes("-zdr") ? { zdr: true, data_collection: "deny" } : {}),
      ...(profile.endsWith("-strict") ? { require_parameters: true } : {}),
    };
    delete payload.parallel_tool_calls;
    if (payload.tool_choice !== undefined && payload.tool_choice !== "none") {
      payload.tool_choice = "auto";
    }
  }
  // <<< codex-openrouter-trial:openrouter-provider-profiles <<<
```

`reasoning_effort` は意図的に触らない（OpenRouter が写像するため、推測写像を足さない）。

### profile の切り替え

`~/.codex/codex-router/user-models.json` の `requestProfile` を書き換えて `bin/control apply`。
接尾辞で判定するので分岐は増えない。

| profile | 送る `provider` | 想定 |
| --- | --- | --- |
| `openrouter-zdr-floor`（既定） | `zdr, data_collection:"deny", sort:"price"` | 日常 |
| `openrouter-floor` | `sort:"price"` | ZDR を外して候補を広げる |
| `openrouter-zdr-strict` | 上 + `require_parameters:true` | 抽選ノイズを潰す。**404 リスクあり**。gate 5 の切り分け用 |

`sort` を入れると **load balancing が無効化**され順次試行になる（`allow_fallbacks` は既定 true）。

## 自動継承の仕組み

`bin/update` の実装は次のとおり。

```
git fetch origin main
dirty tree → 拒否（--force なら git reset --hard HEAD で破棄）
main branch でなければ拒否
git merge --ff-only origin/main      ← post-merge hook が発火
installCurrentCheckout()
```

- **自動更新の trigger は存在しない**（`updateCheckout` の呼び出しは `bin/update` のみ）
- untracked file は dirty 判定に含めない（`--untracked-files=no`）
- **ChatGPT.app の verup は patch を壊さない。** 壊すのは codex-router 自身の更新だけ

3層で対応した。

| 層 | 実体 | 役割 |
| --- | --- | --- |
| 1 | `~/.local/share/codex-router/.git/hooks/post-merge` | merge 後に再適用。`.git/hooks/` は追跡外で `reset --hard` でも消えない |
| 2 | `~/.local/bin/codex-router-update` | patch を剥がす → `bin/update` → 再適用 → 検証。`trap` で失敗時も必ず再適用する |
| 3 | upstream への PR | マージされれば patch も hook も wrapper も不要になる（本命） |

適用スクリプトは `~/.local/share/codex-openrouter-trial/patches/apply-openrouter-provider-profiles.py`。
冪等で、アンカーが見つからなければ **stderr へ理由を出して非ゼロ終了**する（黙って素の状態で
走らせない）。`node --check` を通してから atomic に置換する。

> [!NOTE]
> git は `post-merge` の終了コードを**無視する**（merge の結果に影響できない）。
> よって層1は通知役で、最終的な検証責任は層2の wrapper が持つ。

### 実機で更新を跨いだ検証（2026-08-22）

`codex-router-update` を実行したところ、実際に upstream 更新が発生し、自動継承が実証された。

| 項目 | 結果 |
| --- | --- |
| 更新 | `47d67626` → `b01cf559`（`updated: true, reinstalled: true`） |
| 更新に含まれた変更 | **`f47bbca api-forwarder: preserve streamed usage from chat providers`** ＝ patch 対象ファイル自体が変更された |
| patch | post-merge hook が自動再適用（wrapper 側は "already applied"） |
| 挿入位置 | 799行 → **818行へ移動**。行番号ベースの `.patch` なら失敗していた |
| 構文 | `node --check` OK |
| service | 14:50:15 起動 / patch 14:49:36 ＝ patched code を読み込み済み |

## モデル別 effort ladder

各モデルの実 ladder は codex-router の checked-in registry（同じ upstream model を vendor 直結
経路で互換テスト済み）から採った。

| curated model | `reasoningLevels` | `defaultEffort` | 根拠 |
| --- | --- | --- | --- |
| `openrouter/deepseek/deepseek-v4-flash-0731` | `low` / `high` / `max` | `high` | `config/deepseek/deepseek-v4-flash.json` |
| `openrouter/deepseek/deepseek-v4-pro-0813` | `high` / `max` | `high` | `config/deepseek/deepseek-v4-pro.json`（**`low` を持たない**） |
| `openrouter/moonshotai/kimi-k3` | `low` / `high` / `max` | `max` | `config/kimi/api/kimi-k3.json` |

**今後モデルを追加したときの判定手順**:

1. codex-router の `config/` に同じ upstream model があれば、その `reasoningLevels` を採る
2. 無ければ vendor 公式 docs
3. どちらも無ければ `/api/v1/models` の `supported_parameters` に `reasoning_effort` があることを
   確認し、写像に委ねて `low` / `high` / `max` の保守的3段
4. `ultra` は入れない（OpenRouter が受けない）

## 撤去手順（追記分）

```bash
# patch だけ外す
rm ~/.local/share/codex-router/.git/hooks/post-merge
git -C ~/.local/share/codex-router checkout -- src/api-forwarder.mjs
~/.local/share/codex-router/bin/control service restart
rm ~/.local/bin/codex-router-update
```

trial ごと戻す場合は本文「復帰手順」を参照。
