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
| 1 | picker に native GPT-5.6 と routed が両方出るか | **PASS** | 実機の picker に native `5.6 Sol` と curated 3件が並ぶ |
| 2 | `bin/doctor` | **PASS** | routed 5件、gateway route 一致、venv・secret mode 600 すべて OK |
| 3 | native GPT が router 経由でも動くか | **PASS** | `router.log` に `model=gpt-5.6-sol provider=openai status=200`。`codex exec -m gpt-5.6-sol` も 6秒で完了 |
| 4 | service 停止で native GPT が死ぬか | **PASS** | 追記3。停止中は 4202 が refused、native が `waiting for network` で無限リトライ |
| 5 | DeepSeek V4 Flash 0731 で `apply_patch` を通す | **PASS** | 追記3。実機 2回 + probe 6/6 |
| 6 | ChatGPT.app 更新時に catalog 自動再取得が効くか | 未実施 | app は `26.818.21641`/`6849` のまま。次回更新を待つ |

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
PYTHONPATH=src python3 -m unittest discover -s tests -v --buffer
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

---

# 追記2（2026-08-22）: effort に `max` が出ない件は OpenAI 側のバグだった

## 症状

curated 3件で `reasoningLevels` に `max` を入れても picker に出ない。調査の結果、
**native の `gpt-5.6-sol` でも同じく `max` が落ちていた**（catalog は6段
`low/medium/high/xhigh/max/ultra`、picker は5段 `軽/中/高/極高/Ultra`）。
routed 固有の問題でも catalog の設定ミスでもない。

## 原因（app のコードで特定）

`app.asar` → `/webview/assets/app-initial-DOX-K1rC.js`:

```js
Zje = hl([`none`,`minimal`,`low`,`medium`,`high`,`xhigh`,`max`,`ultra`])  // schema（全語彙）
Qje = [`low`,`medium`,`high`,`xhigh`,`ultra`]                             // ← default から max だけ欠落
Ju = { enabledReasoningEfforts: ku({
  agentAccess: `hidden`, default: Qje,
  description: `Reasoning effort levels available in model controls`,
  key: `enabled-reasoning-efforts`, schema: sl(Zje) }) }
```

`max` は schema に含まれるのに **既定値の配列からだけ抜けている**。catalog が正しく
`max` を宣言しても picker に出ない理由はこれ。

ラベル欠落ではない。`/webview/assets/ja-JP-*.js` に
`composer.mode.local.reasoning.max.label` = `最大` が定義済み。
（`low` だけ `composer.mode.local.reasoning.low.label.v2` という別キーなので grep 時は注意）

## 同一報告（upstream）

- [openai/codex#33805](https://github.com/openai/codex/issues/33805) — macOS の picker で
  GPT-5.6 Luna/Terra/Sol の Max が欠落。iOS では出る。Terra/Sol は Extra High から直接 Ultra へ飛ぶ
- [openai/codex#38338](https://github.com/openai/codex/issues/38338) — Linux でも同様
- [openai/codex#35763](https://github.com/openai/codex/issues/35763) — VS Code 拡張でも同様

## 対処（実機で解決を確認）

`~/.codex/config.toml` の `[desktop]` に既定値の上書きを1行入れる。marker block の外側だが
別領域なので codex-router とも自作実装とも衝突しない。

```toml
[desktop]
enabled-reasoning-efforts = ["low", "medium", "high", "xhigh", "max", "ultra"]
```

同セクションの `show-context-window-usage` が同じ `ku({... key:'...' ...})` 構造で機能して
いることから同経路と判断した。`agentAccess: "hidden"` は agent 向け API を塞ぐ指定であって
永続化層とは別、という読みが実機で裏づけられた。

**結果**: `最大` が全モデルで表示されるようになった。routed 3件だけでなく
**native の 5.6 Sol も 6段（軽/中/高/極高/最大/Ultra）に是正された**。

退避: `~/.local/share/codex-openrouter-trial/2026-08-22/config.toml.before-effort-setting`

## 最終的な effort ladder

app 側が直ったため、天井は vendor の実天井 `max` のままで良い。ラダーは連続にしてある
（OpenRouter は未対応値を近い値へ写像するので中間段の追加に実害はない）。

| model | `reasoningLevels` | `defaultEffort` |
| --- | --- | --- |
| `openrouter/deepseek/deepseek-v4-flash-0731` | low / medium / high / xhigh / max | high |
| `openrouter/deepseek/deepseek-v4-pro-0813` | high / xhigh / max | high |
| `openrouter/moonshotai/kimi-k3` | low / medium / high / xhigh / max | high |

## この調査で否定した仮説（再検証を避けるため）

| 仮説 | 判定 |
| --- | --- |
| 非連続ラダー（low→high→max）が描画を壊す | **否定**。upstream registry も DeepSeek に非連続を出荷している |
| 日本語ローカライズに `max` のラベルが無い | **否定**。`最大` は定義済み |
| routed entry に native 固有フィールドが足りない | **否定**。差分は `tool_mode` と速度 tier のみで effort 描画に無関係 |
| app が `max` を描画できない | **否定**。`kN()` は `max` を受理する。既定値配列の問題だった |

---

# 追記3（2026-08-22）: tool 互換の実証と gate 4 / gate 5

**結論から。gate 5 PASS、gate 4 PASS。** trial の賭け ——「Responses 契約を捨てて Chat Completions
へ落とせば DeepSeek でも `apply_patch` が通る」—— は実機で成立した。

## なぜ測定手段を自作したか

既存の `bin/test-model` では gate 5 を証明できない。probe は `type:"function"` + `strict:true` の
plain JSON function を `stream:false` で送るだけで（`src/compatibility-test.mjs:34-69`）、
**Codex が実際に送る freeform (`type:"custom"` + lark grammar) の経路を一度も踏まない**。
PASS しても必要条件どまり。

一方 LiteLLM には custom→function の bridge が実装済みだった
（`.venv/.../litellm/responses/litellm_completion_transformation/custom_tools.py`、
`transformation.py:1305,1390-1407`、`streaming_iterator.py:107,303`。docstring が Codex CLI を名指し）。
curated model は `litellm.yaml` で `use_chat_completions_api: true` なので、この bridge が必ず経路に入る。

外部報告（cc-switch / knightli の DeepSeek routing guide）は「Codex の OpenAI 固有 tool payload を
DeepSeek V4 は route を問わず拒否した」と言う。**その拒否を bridge が回避できるか**が gate 5 の正体で、
モデルの賢さの検証ではない。

## Phase 1: freeform apply_patch probe（repo 外）

`~/.local/share/codex-openrouter-trial/probes/apply-patch-probe.mjs`。app を挟まず bridge の往復だけを測る。
Codex と同形の `{type:"custom", name:"apply_patch", format:{type:"grammar", syntax:"lark", ...}}` を
`/responses` へ投げ、`output[]` に `custom_tool_call` が来て `input` が `*** Begin Patch` で始まり
`*** End Patch` で終わるかを見る。**stream / 非stream の両方**を測る（LiteLLM は別コード経路で、
ChatGPT.app は stream する）。

| model | 非stream | stream |
| --- | --- | --- |
| `openrouter/deepseek/deepseek-v4-flash-0731` | **PASS** (61B) | **PASS** (61B) |
| `openrouter/deepseek/deepseek-v4-pro-0813` | **PASS** (60B) | **PASS** (60B) |
| `openrouter/moonshotai/kimi-k3` | **PASS** (61B) | **PASS** (60B) |

**6/6。** 初回は stream 側が 0 長と出たが、これは probe のバグだった —— stream は同じ item を
`output_item.added`（`status:"in_progress"`, `input:""`）と `output_item.done`（完成形）で2回流すのに、
最初の一致を返していた。判定を「name ごとに最長の input を採る」へ直し、**保存済みの生レスポンスを
`--replay` で再判定**して確認した（再課金なし）。生レスポンスを毎回ファイルへ落としておくのは、
判定バグを同じターンの再購入なしに直せるという意味で価値がある。

対照に置いた native `commandcode/gpt-5.6-sol` は 409（`Provider commandcode is hidden`）で取れていない。
対照の目的は失敗時の切り分けなので、routed が全通した以上不要と判断し、環境を変える
`providers enable` はしていない。

### bridge が wire 上で何をしているか（実測）

forwarder に届いた時点の `apply_patch`:

```
type: "function"          （custom ではない）
parameters.properties: ["content"]
description: 708〜1331 B、"*** Begin Patch" と "Format:" を含む
format: null              （grammar は description へ畳まれて消える）
```

事実として bridge は経路に入っており、期待どおり動いている。

## gate 5: 実機（ChatGPT.app + DeepSeek V4 Flash）

**2回通った。**

1回目（`Update target.py` スレッド）。私が用意した検証用ディレクトリではなく **repo 直下**に
`target.py` を新規作成した（app の作業ディレクトリが repo root だったため）。内容:

```python
def farewell(name: str) -> str:
    return f"Goodbye, {name}!"
```

このターンのログには `apply_patch のパースエラーです。Add File 時に空でないコンテンツを渡す
フォーマットに問題があるようです。既存パターンに従って正しい構文で再試行します。` が残っている。
**freeform apply_patch は初回で文法を外し、自己修正して通した。** 通ることと一発で通ることは別、
というのがここでの実測。

2回目（クリーンな最小プロンプト）。`~/.local/share/codex-openrouter-trial/gate5/target.py` へ着弾:

```diff
 def greet(name):
     return f"Hello, {name}"
+
+
+def farewell(name):
+    return f"Goodbye, {name}"
```

| 項目 | 実測 |
| --- | --- |
| モデル | `openrouter-deepseek-deepseek-v4-flash-0731` のみ |
| リクエスト数 / 所要 | 8 / **31秒** |
| `tool_choice` | `auto`（書き換えなし） |
| `parallel_tool_calls` | **`true` を送信し、そのまま通過**（再スコープ後） |

1回目は 17 リクエスト / 15分かかった。差は文脈汚染 —— こちらの報告文がそのままプロンプトに入り、
モデルが「計画の裏取り（読み取りのみ）」を始めた。**gate の試行には補足なしの一文を投げる。**

## Phase 0b: patch の再スコープ（実測が既定を否定した）

一時的な capture block を forwarder へ入れ、**payload の形だけ**（本文は書かない）を 1 ターン記録した。
gate は env var ではなくファイルの存在にした —— macOS の LaunchAgent は固定 allowlist で環境変数を
書き出すため（`src/service-macos.mjs`）、shell の export は常駐 service に届かない。

観測（app の実トラフィック）:

| 項目 | 実測 | 旧 patch の挙動 |
| --- | --- | --- |
| `tool_choice` | app は `"auto"` を送る | 書き換え発生せず（**app に対しては no-op**） |
| `parallel_tool_calls` | app は **`true` を送る** | **削除していた** |
| `tool_choice: "required"` | `bin/test-model` の compat probe が送る | **`"auto"` へ潰していた**（3モデル分捕捉） |

つまり旧 patch の downgrade は app には効かず、効くのは compatibility probe と subagent payload relay
—— まさに上流 `auto-tool-choice` のコメントが「reseller 全体に掛けると壊れる」と警告していた対象だけ。

そこで前提そのものを上流へ直接当てた。`/api/v1/chat/completions` に
`provider {sort:"price", zdr:true, data_collection:"deny"}` + `tool_choice:"required"`:

| model | reasoning high | reasoning なし |
| --- | --- | --- |
| `deepseek/deepseek-v4-flash-0731` | 強制 call を履行 | 履行 |
| `deepseek/deepseek-v4-pro-0813` | 履行 | 履行 |
| `moonshotai/kimi-k3` | 履行 | 履行 |

**6/6 が受け付けた。「DeepSeek は thinking mode で forced tool choice を拒否する」は、この3モデルと
`sort:"price"` が引く endpoint（Sail Research / Modal）では成り立たない。** 前提が無く、害だけがある。

→ **tool 契約を書き換える処理をすべて per-model opt-in へ分離した。** 追記1 の block を次で置き換える:

```js
  // >>> codex-openrouter-trial:openrouter-provider-profiles >>>
  if (String(model.requestProfile || "").startsWith("openrouter-")) {
    const profile = String(model.requestProfile);
    const strict = profile.endsWith("-strict");
    payload.provider = {
      ...(payload.provider || {}),
      sort: "price",
      ...(profile.includes("-zdr") ? { zdr: true, data_collection: "deny" } : {}),
      ...(strict ? { require_parameters: true } : {}),
    };
    if (strict) delete payload.parallel_tool_calls;
    if (
      (strict || profile.endsWith("-autotool")) &&
      payload.tool_choice !== undefined &&
      payload.tool_choice !== "none"
    ) {
      payload.tool_choice = "auto";
    }
  }
  // <<< codex-openrouter-trial:openrouter-provider-profiles <<<
```

| profile | provider block | `tool_choice`→auto | `parallel_tool_calls` 削除 |
| --- | --- | --- | --- |
| `openrouter-zdr-floor`（既定） | ✓ | – | – |
| `openrouter-zdr-floor-autotool` | ✓ | ✓ | – |
| `openrouter-zdr-strict` | ✓ + `require_parameters` | ✓ | ✓ |

正本 `patches/apply-openrouter-provider-profiles.py` も更新済み（`--remove` を追加）。post-merge hook と
`codex-router-update` wrapper は現行のまま動作を確認した。

**再スコープ後の検証**: capture で 3モデルとも `tool_choice: "required" → "required"` の素通りを確認。
`bin/test-model` は Pro 4/4・Kimi 4/4・Flash 4/4（連続2回）PASS ——
**forced tool call が本物になった状態で全項目通過**した。

## 計画になかった実測 2 件

### router は全 routed request に app tool 18件を注入する

probe が tool 1件しか送っていないのに forwarder には 19件届いた。`router.mjs:2120` の
`mergeCodexAppTools` が、client が送っていない namespace も無条件で足す（`codex-app-tools.mjs:1227-1231`）。
コメント上は意図的で、routed model に native と同じ toolset を見せるための設計。

### 1 リクエストが約 521KB ある

`router.log` の `est_input` は「上流へ実際に送ったバイト数 ÷ 3.3」で、OpenRouter が input tokens を
0 で返すときの代替値（`response-usage.mjs:227` `estimateInputTokens`）。gate 5 のターンは
`est_input=157,885` ≈ **521KB / リクエスト**。2行の関数を足すのに 8 リクエスト分これが飛ぶ。

app が送る tool の内訳（実測 191件 / description だけで 58,704 B）:

| 出所 | tool 数 | description B |
| --- | --- | --- |
| `mcp__*` | 153 | 43,099 |
| `codex_app__*` + `plugin_management__*`（**router 注入**） | 18 | 6,609 |
| 素の Codex tool（`exec_command` / `apply_patch` 他） | 13 | 4,266 |
| `collaboration__*` | 6 | 2,995 |
| `image_gen__*` | 1 | 1,735 |

router 注入分は serialize して 31,351 B（≈9.5k tokens、**全体の約6%**）。
**遅さの主因は MCP の 153件**であって router 注入ではない。削るならそちら。

## gate 4: service 停止で native GPT が死ぬか → **死ぬ**

headless で実測した（GUI 自動操作はユーザー作業と干渉するため使わない）。
`codex` CLI は同じ `~/.codex/config.toml` を読むので、native を明示して同じ経路を叩ける。

```
codex exec --skip-git-repo-check -m gpt-5.6-sol "Reply with exactly OK."
```

| service | 結果 |
| --- | --- |
| running | `OK` を返して **6秒**で完了（exit 0） |
| stopped | 4202 が `Connection refused (os error 61)`。websocket 再接続 5回すべて失敗 → HTTPS フォールバックも失敗 → `Reconnecting... waiting for network` を無限ループ。**90秒で kill** |

停止窓（08:09:41–08:11:12 UTC）の `router.log` は**1行も出ていない**。native / routed とも
router に到達していない。**グレースフルな失敗ですらなく、native がネットワーク待ちでハングし続ける。**
既知のリスク「native GPT が router 依存になる」は実測で確定した。

## 前の記述の訂正 2 件

**1. `bin/stop` / `bin/start` は停止・起動の対ではない。** `bin/start` の実体は
`exec node src/start.mjs` で **foreground 実行**。LaunchAgent に載らないため `launchctl` から消え、
`bin/status` が `loaded:false` になる。実際にこれで「止めたはずの router が管理外で復活する」状態を
作ってしまった。正しい対は **`bin/control service stop` / `bin/control service start`**
（`src/control.mjs:2278` `handleService`）。

**2. `bin/test-model` の FAIL 表示は当てにならない。** `compatibility-test.mjs` の `streaming()` は
`detail` を `response.ok` だけで決めるため、**`ok:false` でも「stream text and completion event verified」
と表示する**。`--json` の `ok` を見ること。

## 撤去済み

capture block は Phase 1 完了時に外した（`apply-capture-block.py --remove`）。
`grep -c "codex-openrouter-trial:" src/api-forwarder.mjs` は **2**（provider-profile の marker のみ）、
`node --check` OK、`bin/status` は `ok:true, degraded:[]`。

trial 用の道具は repo 外に残してある:

| 場所 | 用途 |
| --- | --- |
| `patches/apply-openrouter-provider-profiles.py` | 再スコープ済み正本（`--remove` 付き） |
| `patches/apply-capture-block.py` | payload 形状 capture（冪等・`--remove` 付き。現在は未適用） |
| `probes/apply-patch-probe.mjs` | freeform apply_patch probe（`--replay` 付き） |
| `gui/app.sh` | ChatGPT.app の GUI driver。**使わない方針**（下記） |
| `2026-08-22/payload-capture.jsonl` | 50行の形状ログ |

`gui/app.sh` は作って動作もしたが（activate / clear / clipboard 貼り付け / `AXStandardWindow` 選択 /
`screencapture -R`）、**ユーザーが同じ Mac を使っている間はフォーカス奪取が干渉する**ため運用しない。
なお ChatGPT.app は Chromium で web content の AX ツリーを露出しないので、構造ベース操作は不可。
日本語 IME 有効時は `keystroke` の ASCII が仮名変換される（`Reply with exactly` →
`Replyウィテェぁctly`）ため、入力はクリップボード経由が必須だった。

## 再検証しなくてよいこと（追加分）

| 事項 | 結論 |
| --- | --- |
| `bin/test-model` で gate 5 を測れるか | **測れない**。probe は plain JSON function + `stream:false` で freeform 経路を踏まない |
| LiteLLM の custom→function bridge は実在するか | **実在し、経路に入っている**。`{content:string}` へ落として grammar を description へ畳む |
| DeepSeek / Kimi は `tool_choice:"required"` を拒否するか | **拒否しない**（3モデル × thinking 有無で 6/6 履行） |
| routed request の tool 数が多い理由 | router が app toolset 18件を無条件注入する仕様（`mergeCodexAppTools`） |
| 遅さの主因 | **MCP 153件**。router 注入は全体の約6% |
