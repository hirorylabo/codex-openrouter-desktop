# Upstream tracking

このprojectのruntime依存はこのrepositoryだけです。`codex-relay`をdependency、fork、
submodule、自動merge元にはしません。OpenAI公式のResponses仕様とOpenAI Codex sourceを
正本にし、`codex-relay`は変換境界と失敗fixtureを見直すための補助資料に限定します。

機械可読なpinは [`models/upstreams.json`](./models/upstreams.json) にあります。2026-08-20時点の参照は
次のとおりです。

| upstream | 参照commit | 参照対象 |
| --- | --- | --- |
| `MetaFARS/codex-relay` | `84586a12976400957747c0afcd61618e052bc97d`（release `v0.5.7`） | `src/translate.rs`, `src/stream.rs` |
| `openai/codex` | `fcdf2b501412d85efa3ce6bc217b8b51d7ed792a` | `codex-rs/tools/src/tool_spec.rs`, `codex-rs/core/src/tools/handlers/apply_patch.rs`, `codex-rs/core/src/client.rs`（classic / responses-lite の2形式） |
| `BerriAI/litellm` (MIT) | `litellm 1.x` の `responses/litellm_completion_transformation/custom_tools.py` | custom→functionの変換契約（`{content:string}`、grammarをdescriptionへ、strictを付けない） |
| `lidge-jun/opencodex` (MIT) | issue #1544 / #2106、`src/responses/tool-groups.ts` | top-level `tools` と lite `additional_tools` の**両方**からtool定義を集める考え方 |

## 採用した挙動

- namespace childをrequest内で一意なfunction名へ平坦化し、responseで元の
  `namespace` / `name`へ戻す。
- custom toolを必須string 1項目のplain functionへ変換し、responseで
  `custom_tool_call`へ戻す。field名は常に`content`。
  - **`strict` と `additionalProperties` は付けない。** 2026-08-22にOpenRouterへ
    直接測った結果、`strict:true` を付けた形は`apply_patch`を 0/4 でしか
    引き出せず、外すと 4/4 になった。DeepSeekで`structured_outputs`を公称する
    endpointは 22/30 しかなく、`sort:"price"`は残りを普通に引く。
  - custom toolの`format.definition`（lark）はdescriptionの末尾へ
    ` ```<syntax>` fence で畳む。Chat Completionsのfunctionにgrammarを
    載せる場所が無く、modelが読める唯一の場所がdescriptionのため。
  - **変換後もtool名を保つ。** namespace配下だけ `<namespace>__<name>` へ
    平坦化する。生成名（`codex_bridge_NNNN`）はmodelから意味を隠すため使わない。
- tool定義はtop-levelの`tools`と、responses-lite形式の`input[].additional_tools`の
  両方から集める。catalogの`use_responses_lite`に依存しない。
- `call_id`、`item_id`、`output_index`とSSEのdelta/done順序を保持する。
- 未知tool、重複名、不完全JSON、不正なSSE lifecycleを補正せずfail-closedにする。

実装はOpenAI公式仕様を基にこのrepositoryで独自に記述しています。現時点で
`codex-relay` / `litellm` / `opencodex` のsource codeはコピーしていません。
LiteLLMとopencodexからは**変換の契約と着眼点**だけを参照し、コードは各々の
構造に合わせて独自に書いています。将来コードをコピーする場合は、同じ変更で
出所・commit・license noticeを追加します。

## 採用しなかった機能

- Responses APIからChat Completionsへの変換
- 独自のsession/history/reasoning store、corpus記録
- OpenRouter以外のprovider abstractionとmodel quirk registry
- DSML本文漏れ、欠落`[DONE]`などの推測修復
- upstream binary、Rust/Python package、設定generator

## 更新手順

週次の `upstream-watch` workflowはrelease、branch HEAD、上記ファイルのSHA-256を
確認します。差分は自動mergeしません。workflowを失敗させ、変更ファイルと
「tool wire fixtureを再生成して実機canaryを通すこと」をstep summaryへ出します。

更新を取り込むときは、公式Responses仕様を先に確認し、最新版ChatGPT buildで
structured/custom/namespace/apply_patchの実機canaryを行います。合格後に
[`models/tool-wire-builds.json`](./models/tool-wire-builds.json)へ最新版を昇格し、直前build
だけを残します。古いbuildを先に削除してはいけません。

### 更新記録

- 2026-08-20: ChatGPT.appが build `6720` → `6849`（`26.818.21641`）へ更新されたため、
  `models/tool-wire-builds.json` を `6849` + 直前の `6720` へ昇格した。テンプレート差分は
  `supports_parallel_tool_calls` の削除と `model_messages.multi_agent`（値は `null`）の追加だけで、
  中和が要る新フィールドは無い。経緯と実測は `task/0820-chatgpt-6849-compat-check.md`。

- 2026-08-20: OpenAI Codexのpinを`fcdf2b501412d85efa3ce6bc217b8b51d7ed792a`へ更新。
  直前pinからの1 commitは`unified_exec/head_tail_buffer`の容量型変更だけで、参照する
  tool spec / apply_patch handlerのhashは不変だったため、tool wire fixtureは再生成していない。
