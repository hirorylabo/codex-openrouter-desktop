# 0819 OrcaRouterを第2のrouterとして追加する計画

作成日: 2026-08-19 / 対象: `main` / Status: **中止・OrcaRouter実装は撤去済み**

> [!IMPORTANT]
> この計画は実機評価で中止した。OpenRouter版DeepSeek V4 Flash 0731が
> structured function callを生成できても、GPT-5.6由来のCode Mode/freeform契約を
> 守れないことが分かったため、後続の「OrcaRouter撤去とOpenRouter tool互換最適化」
> へ移行した。以下は採用しない設計の監査記録として残す。

## Context

現行ツールはOpenRouter専用で、endpoint、API key、provider table、候補一覧、ZDR判定、
catalog、watcher、guard、doctor、Swift UIに単一router前提が分散している。

OrcaRouterを第2のrouterとして同じ純正ChatGPT.appのpickerへ追加し、OpenRouterと共存させる。
純正appの変更、ASAR patch、clone、API keyのconfig/logへの保存は行わない。既存の案D、
loopback guard、marker block、atomic promotion、stock appのvanilla復帰を維持する。

OrcaRouterの公式資料は、OpenAI互換のResponses API、tool calling、streaming、provider-prefixed
model ID、PKCEを示している。

- https://docs.orcarouter.ai/introduction
- https://docs.orcarouter.ai/getting-started/models
- https://docs.orcarouter.ai/api-reference/responses/create-a-response
- https://docs.orcarouter.ai/getting-started/sign-in-with-orcarouter

## Go判断

設計方向は妥当で、**契約確定と検証fixtureを先に追加するP0はGO**。

ただし、次の契約が未確定のままP1実装へ進むこと、またはP1完了を実機利用可能と扱うことはNo-Goとする。

1. 内部catalog IDとrouterへ送るupstream IDの対応
2. router別Keychain・認証・profile検証
3. router別pricing/catalog refresh
4. supervisor・cleanup・self-heal・doctorのprovider分岐
5. OrcaRouter候補一覧のResponses対応判定
6. `gateway`保持ポリシーとResponsesの`store`挙動

## 不変条件

- `/Applications/ChatGPT.app`とその署名・ASARは変更しない。
- 実API keyはKeychainからguard/CLIだけが取得し、config、catalog、argv、log、userDataへ渡さない。
- 非稼働時はOpenRouter/OrcaRouterの両provider tableを`127.0.0.1:0` + `/usr/bin/false`のstubにする。
- 稼働中はprovider tableを同一guardのephemeral portへ向ける。access tokenはguard単位で1つ、
  実API keyはrouterごとにKeychain accountを分離する。
- guardは認証前にrequest bodyを読まず、許可外model、未知path、router不一致、namespace不一致を
  upstreamへ1 byteも送らない。
- provider tableは永続、catalog blockは稼働中だけとする。終了時はcatalogを外し、provider tableを残す。
- 既存OpenRouterだけのprofile・installのdigestと挙動を変えない。
- 既存registry entryの`router`省略はOpenRouter、`upstream_id`省略はregistry keyとして後方互換にする。

## Router契約

### 固定router定義

`src/codex_openrouter/routers.py`にfrozen dataclassのrouter定義を置き、endpointやprefixを他の
モジュールへ再ハードコードしない。

| field | OpenRouter | OrcaRouter |
|---|---|---|
| `name` | `openrouter` | `orcarouter` |
| `provider_table` | `openrouter` | `orcarouter` |
| `guard_path` | `/v1` | `/orca/v1` |
| `endpoint` | `https://openrouter.ai/api/v1/responses` | `https://api.orcarouter.ai/v1/responses` |
| `catalog_prefix` | `` | `orca/` |
| `picker_prefix` | `[OR] ` | `[ORCA] ` |
| `key_prefix` | `sk-or-` | `sk-orca-` |
| `keychain_account` | `NSUserName()` | `NSUserName() + ".orcarouter"` |
| `forces_zdr` | `true` | `false` |

`catalog_prefix`は表示とslug衝突回避のためだけに使う。router判定は文字列prefixではなく、
registry entryの`router` fieldで行う。

### IDの二層契約

registry keyとpickerへ渡すcatalog IDは内部ID、upstreamへ送るmodelは`upstream_id`とする。

```json
{
  "orca/anthropic/claude-opus-4.7": {
    "router": "orcarouter",
    "upstream_id": "anthropic/claude-opus-4.7",
    "display_name": "Claude Opus 4.7",
    "data_retention": "gateway"
  }
}
```

- OpenRouter既存entryは`router`省略を`openrouter`、`upstream_id`省略をregistry keyとして読む。
- `settings.materialize_registry`、catalog cache、pricing cacheのjoin keyはraw slug単独にしない。
  最低でも`(router, upstream_id)`または内部IDを使う。
- 同じupstream IDが両routerに存在しても、内部IDの重複を許さない。
- guard logには内部model IDとrouterを残し、upstream requestにはupstream IDだけを入れる。
- `profile.digest`はprofileの内部ID集合から計算し、既存OpenRouter-only profileのdigestは変えない。

### 保持ポリシー

registryの正本を次の3値にする。

```text
data_retention = zdr | gateway | none
```

- `zdr`: router/provider endpointがZDRを保証するmodel。OpenRouterの`provider.zdr`だけを注入する。
- `gateway`: OrcaRouter自身はprompt/outputを保存しないと説明しているが、upstream providerの保持は
  そのproviderのポリシーに従う。ZDRとは表示しない。
- `none`: ZDRの根拠が無いmodel。選択時は警告と確認を要求する。
- 旧entryは`data_retention`が無ければ、旧`zdr_supported`から補完する。
- `zdr_supported`は互換出力として`data_retention == "zdr"`から導出する。
- `gateway`に対するprovider.zdrの注入は禁止する。

OrcaRouter Responses APIの`store`について、P0で実requestを確認する。`store=false`を安全に強制でき、
Codexのthread継続契約を壊さないならOrca routeで強制する。強制できない場合は`gateway`表示を維持し、
上流保持未確認をUIとdoctorで明示する。「gateway = end-to-end ZDR」とは表示しない。

## P0: 契約確定とfixture（P1着手条件）

実装前に、秘密を含まないfixtureと契約テストを作る。live APIの結果件数をそのまま固定せず、
候補、除外、価格、context、endpoint capabilityを代表する最小fixtureにする。

### OrcaRouter API契約の確認

- `/v1/models`をkeylessとkey付きで比較し、keyless 200を必須契約にしない。
- 公式docsの例では`Authorization`付きで、`supported_endpoint_types`は`openai`と記載されている。
  現在の調査で`openai-response`が返る場合も、それだけを唯一の判定根拠にしない。
- Responses対応の判定は、API fieldの正規化、既知契約、選択時canaryの三段階でfail-closedにする。
  未知fieldや契約変更時は候補を自動採用せず、doctorへWARN/FAILを出す。
- `text` input/output、pricing、context length、tool calling、reasoningの実際のshapeを確認する。
- 代表modelでResponsesのtool calling、streaming、reasoning effort `low/medium/high`を確認する。
- `GET /v1/models`で見えることと、Responsesを実際に呼べることを同一視しない。
- APIの応答modelがraw IDか、canonical IDか、namespaceを返すかを記録する。

### P0の成果物

- `tests/fixtures/orcarouter-models.json`
- 必要なら`tests/fixtures/orcarouter-models-keyed.json`（秘密は含めない）
- router契約・ID変換・endpoint capabilityのunit test
- 仕様確認結果と未確認項目を本taskのStatusへ追記

P0が終わるまで、OrcaRouter候補を同梱registryへ追加しない。

## P1: backend基盤（UIなし）

P1の成功条件は、CLI・registry・guard・watcher・config・doctorだけで、明示した内部IDを
router別upstreamへ安全に送れること。UIとOAuthはP2/P3へ分離する。

### registry/profile/settings

- registry schemaへ`router`、`upstream_id`、`data_retention`を追加する。
- 既存OpenRouter entryを無印として読み、同じ内容のprofile digestを維持する。
- `settings.materialize_registry`をrouter awareにする。
- router別候補を同じ内部ID集合へ統合し、raw ID衝突を拒否する。
- mixed profileを許可するが、選択profileをrouter別にグループ化して検証する。
- applyの全検証が完了するまでprofile、installed registry、supervisor state、manifest、旧catalogを
  1 byteも変更しない。
- router別keyが1つでも欠ける場合、promotion前に停止する。
- 片方のrouter検証失敗時に、もう片方のkeyやregistryだけを保存しない。

### auth/keychain

- `CredentialStore`とSwift helperにrouter引数を追加する。省略時はOpenRouterとして既存呼び出しを維持する。
- key prefix検証は相互排他的にする。`sk-orca-`をOpenRouter keyとして受け付けない。
- `setup`、`auth login`、`auth rotate`、`auth logout`、`check`にrouter選択を追加する。
- mixed profileのsetup/upgradeは必要なrouterのkeyを全て要求する。
- keyの値・hash以外の秘密情報を出力しない。UIには存在・検証状態だけを返す。
- OpenRouterは既存の`/key` + 実効model検証を維持する。
- OrcaRouterはkey付き`/v1/models`を取得し、選択中の`upstream_id`が存在することを確認する。
  それだけで呼出可能と断定せず、代表modelのResponses canaryを別ゲートにする。
- credit limit/expiryをAPIから読めない場合は、静的なコンソール案内として表示する。

### guard

[src/codex_openrouter/guard.py](../src/codex_openrouter/guard.py)を単一port・複数routeへ変更する。

- `Guard`は`routes: dict[str, Route]`を受け取る。
- routeごとに許可内部ID集合、Keychain provider、upstream endpoint、ZDR注入可否、ID変換を持つ。
- POST pathを厳密に`/v1/responses`または`/orca/v1/responses`へ限定する。
- pathとmodelのrouterが一致しなければ、key取得・forward前に拒否する。
- JSON modelを内部IDで検証し、routeの`upstream_id`へ書き換えてからforwardする。
- `zdr`だけprovider.zdrを注入し、`gateway`/`none`では既存provider指定を尊重する。
- routeごとのforwarderをテストで差し替え可能にし、endpointとkeyの取り違えを検証する。
- logへ`router`を追加する。本文、key、response本文は記録しない。
- health endpointは既存nonce契約を維持し、unknown pathは404または安全な4xxにする。

### watcher/supervisor/config

- watcherの入力を`dict[internal_model_id, provider_table]`にする。未知modelは`openai`へ倒す。
- `Supervisor.apply_config`のdefault providerをmodelのrouterから決める。
- cleanup/self-healはOpenRouterだけでなくOrcaRouter選択もnativeへ戻す。
- `saved_provider`、`model_provider`、fallback判定を一般化する。
- `provider_block_body`は両provider tableを生成する。active時は両方が同一guardの別pathを指す。
- inactive時も両tableを非接続stubへ戻す。
- 起動前に`OPENROUTER_API_KEY`と`ORCA_KEY`の両方を環境から除去する。
- 既存provider tableがmarker外にある場合は、OpenRouterとOrcaRouterの双方で変更せず停止する。

### doctor

- provider table、base URL、guard path、auth command、stub、active stateをrouter別に検査する。
- network検査はrouter別keyを取得し、選択中modelをrouter別upstream IDへ変換する。
- OpenRouterは既存ZDR canaryとgeneration metadata確認を維持する。
- OrcaRouterは`provider.zdr`無しのResponses canaryと応答model・tool call・streamingを検査する。
- `gateway`はZDR PASSと表示せず、OrcaRouter自身とupstream保持の境界をWARNで明示する。
- `obsidian/`判定は非公開prefixの暫定分類であることを表示する。取得失敗時に安全を意味するfalseへ倒さない。
- `gpt-5.6-luna`、`codex-auto-review`は`owned_by`に依存せず明示denyする。
- secret scanへ`sk-orca-`と`ORCA_KEY`を追加する。

## P2: 候補一覧とSwift UI

- OpenRouter sourceとOrcaRouter sourceをrouter単位で取得し、cacheもrouter単位で分離する。
- cacheのjoinはraw ID単独でなく、routerとupstream IDを含める。
- `models list --json`のschemaへ`router`、`dataRetention`、`uncensored`、`uncensoredKnown`をoptionalで追加する。
- 古いCLIを新しいlauncherが読んでも画面全体を落とさない。
- `ModelOption`、`CatalogEntry`、table表示へrouter badge、router filter、retention表示を追加する。
- `ZDRのみ`は`data_retention == "zdr"`だけとする。
- `gatewayも含める`を追加し、gatewayをZDRと同じ安全ラベルにしない。
- uncensored filterは「既知のuncensored prefix」のfilterと表示し、非該当を安全と断定しない。
- OrcaRouter keyの入口を追加する。key値はUIへ返さない。
- consoleボタンはrouterごとに出し分ける。
- 保存前にrouter別key・model availability・Responses capabilityをCLI側で検証する。

## P3: OAuth、docs、実機仕上げ

- OrcaRouter OAuthはOpenRouterのendpointを流用しない。
- `https://www.orcarouter.ai/.well-known/openid-configuration`からauthorization/token endpointを取得する。
- S256 PKCE、memory-only verifier、random state、callback時のstate照合、single-use codeを実装する。
- OAuth完了後の`sk-orca-...`だけをOrcaRouter Keychain accountへ保存する。
- README / README.en / CLI help / UI copyへ、router差、gateway retention、upstream保持、料金責任を反映する。
- 実機E2E完了までtag、release、公開、mergeを行わない。

## 移行と後方互換

- 既存のinstalled registryに`router`が無ければOpenRouterとして読む。
- 既存のinstalled profileは再解決してもmodel集合とdigestを変えない。
- OpenRouter-only installでOrcaRouter keyが無くても、既存機能の起動・cleanup・doctorが壊れない。
- OrcaRouterの同梱curated model追加はprofileへ自動追加しない。
- 旧provider blockのOpenRouter thread resumeを維持する。
- OrcaRouter provider tableを永続化した後、catalogを外して純正起動に戻しても両provider定義を残す。
- promotion失敗時はprofile、registry、state、manifest、catalog、provider状態を全て旧状態へ戻す。
- 旧CLI・新launcher、旧launcher・新CLIの一時的な組み合わせで、秘密を表示せずfail-closedにする。

## Verification

### repository

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v --buffer
PYTHONPATH=src python3 -m compileall -q src portable scripts
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py
python3 scripts/secret_scan.py --tree .
uvx ruff@0.16.3 check .
git diff --check
```

localhost bindを含むtestはsandbox外の通常Mac権限で実行する。sandboxの`PermissionError`を実装失敗と
判定しない。

### 新規unit/contract tests

- registry旧形式の`router`省略と`upstream_id`省略
- OpenRouter/OrcaRouterの内部ID・upstream ID変換
- raw ID衝突とnamespace衝突の拒否
- catalog cacheのrouter分離
- router path → route → endpoint/key/upstream IDの対応
- `/v1`と`/orca/v1`のcross-route拒否
- unknown path、missing model、unknown model、unauthenticated body非読込
- OpenRouterのZDR注入、OrcaRouter gateway/noneの無注入
- `store`のOrca route挙動とthread継続契約
- mixed profileのrouter別key検証、片方失敗時のatomic rollback
- key prefix、Keychain account、環境変数secret scan
- `gpt-5.6-luna`と`codex-auto-review`の候補・registry・guard deny
- `owned_by == custom`だけに依存しない候補除外
- `data_retention` 3値と`zdr_supported`導出
- 公式schemaが`openai`の場合、`openai-response`の場合、未知の場合のfail-closed
- OrcaRouter models listがkeylessで失敗または形を変えた場合のcache/fallback
- provider table二つのinsert、idempotent update、cleanup、self-heal、resume

### 実機E2E

実keyはユーザー操作または既存Keychainから取得し、出力へ表示・保存しない。実課金canaryは明示承認の
ある環境だけで行う。

1. OpenRouter-only profileで既存のpicker、turn、ZDR、vanilla復帰が変わらない。
2. OrcaRouter-only profileで`[ORCA]`モデルが表示され、Responses turnが成功する。
3. mixed profileでOpenRouter modelとOrcaRouter modelをそれぞれ送信し、key/endpointを取り違えない。
4. OrcaRouter tool calling、streaming、reasoning effortを確認する。
5. native turnがguardへ着弾しない。
6. `gpt-5.6-luna` / `codex-auto-review`の背景threadがOrcaRouterへ流れない。
7. router境界をまたぐmodel変更、同一threadのprovider境界、応答modelのraw/namespace差を確認する。
8. 既存OpenRouter threadと新規OrcaRouter threadのresume/sidebar表示を確認する。
9. 終了後にcatalog blockが消え、両provider tableが非接続stubとして残る。
10. launcher強制終了後のself-healでmodel/provider/state/tokenが復旧する。
11. stock `/Applications/ChatGPT.app`を直接起動したときはvanillaである。
12. `doctor --runtime --network --secret-scan`がrouter別に正しい結果を出す。
13. OrcaRouter keyを削除した状態で、OpenRouter-only profileは動作し、Orca選択はfail-closedになる。
14. upgrade途中の検証失敗で全対象がrollbackされる。

## 停止条件

- native本文がguardまたはOrcaRouterへ送信される。
- pathとmodelのrouter不一致がforwardされる。
- 内部IDがraw IDのまま間違ったrouterへ送られる。
- OpenRouter keyとOrcaRouter keyが取り違えられる。
- keyless models listや`owned_by`だけを根拠に安全性・Responses対応を断定する。
- `gateway`をZDRとして表示する、またはupstream保持を隠す。
- provider table、catalog、profile、state、manifestの一部だけが更新される。
- 既存OpenRouter threadのresume、vanilla復帰、stock app invarianceが壊れる。
- config、argv、log、catalog、userDataへkeyが残る。
- doctor、secret scan、CI、実機E2Eの必須gateが失敗する。

## 承認境界

- 本taskはplan作成のみ。実装、commit、push、PR、merge、tag、releaseは含まない。
- 実keyの新規発行、OAuth承認、実課金canary、provider console設定変更は各操作直前に別途承認する。
- branch削除、GitHub設定変更、公開、releaseは実機gate完了後も個別承認を要する。

## Status

- 2026-08-19: 前回の条件付きGOレビューを反映して作成。
- 2026-08-19: P0確認、P1/P2/P3実装、repository verificationまで完了。**実機E2Eは未実施。**

### P0で確認した事実（2026-08-19、keyless取得）

`GET https://api.orcarouter.ai/v1/models` は**keyでも無しでも200**を返し、192件が載った。
ただしdocsは「アカウントがアクセスできるmodelだけ」と書いているので、keyless 200は契約にしない。
実装はkeyがあれば付け、無ければ付けずに試し、落ちたらrouter別cacheへ倒す。

1. **`supported_endpoint_types` はdocsと実APIで食い違う。** docsの例は `["openai"]` だが、
   実APIは192件中71件で `openai-response` を返す。既知の値は
   `openai / openai-response / openai-video / anthropic / gemini / embeddings / image-generation` の7つ。
   → どちらか一方を唯一の根拠にせず、`routers.responses_capability` で
   supported / unsupported / unknown へ正規化する。未知の値が1つでも混ざれば unknown にし、
   候補へ自動採用しない。unknownの件数は候補documentに残してdoctorがWARNを出す。

2. **`owned_by` は判定に使えない。** 192件中88件が `custom` で、主力の
   `deepseek/deepseek-v4-pro` も巻き込み対象の `gpt-5.6-luna` も同じ `custom`。
   → 除外は `owned_by` ではなく、ID一致のdeny listと `supported_endpoint_types` で行う。

3. **`gpt-5.6-luna` と `codex-auto-review` は実在する。** 両方ともbare ID（namespace無し）で、
   `gpt-5.6-luna` は `openai-response` を名乗る。`openai/gpt-5.6-luna` も別途存在する。
   → 候補・registry・guardの3箇所で明示deny。namespace付きの `orca/openai/gpt-5.6-luna` は
   純正appが出すslugではないので許可対象のまま。

4. **`architecture.output_modalities` が null / 空で返るmodelが実在する**
   （`deepseek/deepseek-v4-pro`、`openai/gpt-5.5` 系など）。
   → 不明を「textを出せない」とは読まない。入力にtextがありResponsesを名乗るなら候補に残し、
   実際に話せるかはcanaryゲートへ回す。ここで落とすと主力modelが丸ごと消える。

5. **reasoning effortは `low / medium / high` の3値のみ。** OpenAPIのenumがそれだけで、
   per-model情報は `/v1/models` に無い。`minimal` も `xhigh` も `max` も無い。
   → registryの `orcarouter_catalog_refresh.reasoning_efforts` に1箇所だけ持つ。
   既定は `medium`（OpenAI Responsesの既定に合わせた）。

6. **`store` は強制できない。** OpenAPIは `store` を
   「Whether the upstream may store the request/response. **Allowed by default**;
   channel setting `disable_store` can override.」と定義する。既定が保存許可で、
   上書きはchannel設定側にある。実requestでの確認にはkeyが要り、未実施。
   → 計画の「強制できない場合」の分岐を採用。`store` は**送らない**（送って強制した
   ふりをしない）。表示は `gateway` を維持し、上流保持が未確認であることをUI・picker説明文・
   doctorで明示する。

7. **価格は `prompt` / `completion`（per-token）と `*_per_million` の両方が返る。**
   cache readの単価は**どのmodelも公開していない**。
   → per-tokenを正本にする（丸めの出所を1つにする）。公開されていないcache成分は
   `Router.publishes_cache_price` で落とし、0として表示しない。

8. **OIDC discoveryが `http://` を返す。** 実測で
   `https://www.orcarouter.ai/.well-known/openid-configuration` は
   `{"authorization_endpoint":"http://www.orcarouter.ai/auth", "token_endpoint":"http://www.orcarouter.ai/api/v1/auth/keys", "issuer":"http://www.orcarouter.ai"}`
   を返す（docsの例は https）。token endpointはAPI keyそのものを返すので、
   平文で話せばkeyが漏れる。
   → discoveryの内容は入力であって信頼の根拠にしない。schemeは常にhttpsへ固定し、
   hostは `www.orcarouter.ai` に限る。外れたらfail-closed。

9. **`obsidian/` prefixは現時点のkeyless listに存在しない。** 実在するprefixは
   openai / google / qwen / anthropic / deepseek / kling / z-ai / minimax / orcarouter /
   grok / kimi / meta / tencent / orca と、bare IDが2件。
   → uncensored分類は「既知prefixに一致するか」の暫定分類として実装し、
   非該当を安全とは表示しない。分類できなかった場合は不明のままにし、falseへ倒さない。

10. **同じupstream IDが両routerに存在する。** 例: `z-ai/glm-5.2`、
    `deepseek/deepseek-v4-pro`、`moonshotai/kimi-k3` 相当。
    → join keyをraw slug単独にしない。内部IDを正本にし、cacheもrouter単位で分ける。

### P0で確認できていない項目（実keyが要る）

実keyの発行・OAuth承認・実課金canaryは承認境界の対象なので実施していない。

- key付き `/v1/models` とkeyless の差分（件数・可視範囲）
- 代表modelのResponses実往復（tool calling / streaming / reasoning effort `low/medium/high`）
- 応答modelがraw IDで返るかnamespace付きで返るか
- `store=false` を送ったときの実挙動と、Codexのthread継続契約への影響
- credit limit / expiry を読む手段の有無
- **3階層のslugを純正pickerが扱えるか。** OrcaRouterの内部IDは
  `orca/anthropic/claude-opus-4.7` のように `/` を2つ含む。既存のOpenRouter slugは1つで、
  実機で通ることを確認済み。2つでも文字列としては同じはずだが、`model_catalog_json` の
  deserializerとconfigの `model` 値で確認していない。実機E2Eの2番で最初に見る項目。
  もし扱えない場合は `catalog_prefix` を `orca-` のような区切り無しへ変えれば済む
  （registryの `router` fieldで判定しているので、prefixの変更は表示とslug衝突回避にしか
  影響しない）。

実装はいずれも「未確認」を前提にfail-closedへ倒してある。doctorの
`check_orcarouter_network` が実keyでこの5点を検査する（tool call / streaming は
課金を抑えるため代表1件のみ。未検査のmodelは名指しでWARNへ出す）。

### 実装（このrepositoryへの変更）

- 新規: `src/codex_openrouter/routers.py`（router契約・ID二層・保持ポリシー・
  endpoint capabilityの単一の出所）、`src/codex_openrouter/orcarouter.py`（API取得口）。
- guardを単一port・複数routeへ変更。判断順は 認証 → path→route → 許可集合 →
  upstream ID書き換え → ZDR注入可否。未知pathは本文を読まずに404。
- provider tableは常に両router分を出す。active時は同一guardの別path、inactive時は
  両方 `127.0.0.1:0` + `/usr/bin/false` のstub。
- Keychainはrouterごとにaccountを分離（serviceは共通、接尾辞のみ変更）。
  key prefix検証は相互排他。CLIとSwift helperの両方で拒否する。
- `settings` / `install` / `upgrade` のkey検証を `auth.validate_profile_keys` へ集約。
  mixed profileでは全routerの鍵が揃うまでネットワークへ行かない。
- Swift UI: routerバッジ列・routerフィルタ・`gatewayも含める`・
  `既知uncensored prefixを隠す`・router別コンソール・API key入力（stdin経由）。
- 新規fixture: `tests/fixtures/orcarouter-models.json`（実取得の代表11件）、
  `orcarouter-models-contract.json`（合成の契約edge case）、
  `launcher-*.json`（Swift decoderのcompat固定）。

### Verification（実施済み）

```text
PYTHONPATH=src python3 -m unittest discover -s tests --buffer   → 403 tests OK
PYTHONPATH=src python3 -m compileall -q src portable scripts     → OK
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py            → PASS
python3 scripts/secret_scan.py --tree .                          → PASS
uvx ruff@0.16.3 check .                                          → All checks passed
git diff --check                                                 → OK
xcrun swiftc portable/launcher/app/*.swift                       → build OK
xcrun swiftc -parse-as-library ProfileBridge.swift DecoderCompatTests.swift → DECODER COMPAT: PASS
```

テストが実ネットワークへ出ていないことも確認した（loopback以外のconnectを塞いだ状態で全件通過）。
以前は `test_settings.ModelAdditionTests` 等が差し替え漏れで実APIを叩いていたので、
候補一覧の差し替えを `SettingsTestCase.apply` の既定に入れて塞いだ。

### 残っているもの

- **実機E2E（計画の14項目）は未実施。** 実keyとGUI操作が要る。
- 実keyでの `store` 挙動確認、Responses canary、応答model形式の確定。
- 同梱registryへのOrcaRouter curated modelの追加は行っていない（計画どおり、
  profileへ自動追加しない方針）。
- commit / push / PR / tag / release は未実施（承認境界）。

## 引き継ぎ（2026-08-19時点）

### 現在の状態

| 項目 | 状態 |
|---|---|
| `main` | `16bc427`（v0.2.1リリース準備）。本taskの変更は**未commit**で working tree に載っている |
| 実装 | P0（契約確定・fixture）／P1（backend）／P2（候補一覧・Swift UI）／P3（OAuth・docs）まで完了 |
| repository verification | 全て通過。406 tests / ruff / compileall / synthetic E2E / secret scan / swift build / decoder compat / `zsh -n` / `git diff --check` |
| 実機E2E | **未実施**。計画の14項目すべて。実keyとGUI操作が要る |
| 同梱registry | OpenRouter 5件のまま。OrcaRouterのcurated modelは追加していない（計画どおり） |
| commit / push / PR / tag / release | 未実施（承認境界） |

### 次にこのrepositoryへ触るときの注意

**1. router契約の出所は1箇所しかない**

endpoint・guard path・picker prefix・key prefix・Keychain accountの接尾辞・
ZDR強制可否・cache価格の公開有無は、すべて `src/codex_openrouter/routers.py` の
frozen dataclassが正本。他のmoduleへ再ハードコードしない。

**写しが1つだけある。** `portable/credential/CredentialHelper.swift` の
`routerCredentials` は同じ対応表をSwiftで持つ（Pythonを読めないため）。
routerを足すときは**必ず両方を同時に**直すこと。片方だけだと `get` が別routerの
鍵を返す。Swift側にもその旨のコメントを置いてある。

**2. routerの判定に文字列prefixを使わない**

判定は registry entry の `router` field で行う。`catalog_prefix`（`orca/`）は
表示とslug衝突回避のためだけにある。内部IDから推測するコードを1箇所でも足すと、
`orca/` を名乗るOpenRouter modelが現れた瞬間に取り違える
（`profile.validate_registry_entries` がその形のregistryを拒否している）。

**3. `gateway` を `zdr` と同じ枠へ入れない**

保持ポリシーの正本は `data_retention`（`zdr` / `gateway` / `none`）。
`zdr_supported` は `data_retention == "zdr"` からの導出値でしかない。
`spec.get("zdr_supported")` を直接読むコードを新しく書かないこと
（既存の読み出しは全て `routers.zdr_supported` を経由している）。

`gateway` は「routerは保存しないと説明しているが、上流providerの保持は
providerのポリシー次第」の意味で、end-to-end ZDRではない。UI・picker説明文・
doctorのいずれもZDRとしては表示しない。guardは `gateway` へ `provider.zdr` を
注入しない（`guard.Route.__post_init__` がZDRを強制できないrouterへのZDR指定を
起動時に例外で落とす）。

**4. テストは実ネットワークへ出さない**

`settings` の候補取得は `modelcatalog.load_all`。差し替えるならそこ。
`SettingsTestCase.apply` が既定で `load_all` を差し替えるようにしてあるので、
新しいテストは何もしなくてよい。**差し替え漏れは緑のまま通る**（実APIが応答するため）
ので、疑わしいときはloopback以外のconnectを塞いで全件回すと分かる。

```python
real_connect = socket.socket.connect
def guard_connect(self, address, *a, **k):
    if address[0] not in ("127.0.0.1", "::1", "localhost"):
        raise RuntimeError(f"real network connection attempted: {address}")
    return real_connect(self, address, *a, **k)
socket.socket.connect = guard_connect
```

**5. `models list` / `profile show` のfieldを増減したらfixtureも直す**

`tests/fixtures/launcher-*.json` をSwiftの `portable/tests/DecoderCompatTests.swift`
（CIの `macos-compile` job）が読む。fixtureが古いままだとSwift harnessは古い形を
検査し続けて緑になるので、`tests/test_routers.py` の `LauncherContractTests` が
「実装が出すkey集合」と「fixtureのkey集合」の一致を固定している。片方だけ直すと
そこが落ちる。

出力の `schema_version` は **1のまま上げない**。追加fieldは全てoptionalで、
古いCLIと新しいランチャーの組み合わせでも画面を落とさない設計にしてある
（upgradeはCLIとlauncherを同じtransactionで置き換えるが、その瞬間に片方だけ
新しい組み合わせが実在しうる）。上げるのは既存fieldの意味を変えるときだけ。

**6. secret scanに引っかからないダミー鍵の長さ**

`scripts/secret_scan.py` の汎用パターンは `sk-` の後ろ24文字以上を拾う。
テストへ書くダミー鍵は `sk-orca-abcdefgh` のように短くする
（doctor側は `sk-orca-` + 8文字以上を見るので、これで両方の条件を満たす）。
実行時に組み立てる形（`router.key_prefix + "a" * 40`）なら literal が現れないので
長くてもよい。

**7. OrcaRouterの契約は動く前提で書いてある**

`supported_endpoint_types` が未知の値を返し始めたら、候補は自動採用されずに
unknownへ倒れ、doctorがWARNを出す（`routers.KNOWN_ENDPOINT_TYPES`）。
静かに壊れるのではなく、候補が減ってWARNが増える形になる。その時は
既知値の一覧を更新するか、判定規則そのものを見直す。

reasoning effortの3値と既定は `models/registry.json` の
`orcarouter_catalog_refresh` にある。docsのenumが変わったらここを直す。

### 実機E2Eで最初に見るべきもの

計画の14項目の順序どおりで構わないが、**2番の前に1つ確認したい未知がある**。

OrcaRouterの内部IDは `orca/anthropic/claude-opus-4.7` と `/` を2つ含む。
既存のOpenRouter slugは1つで実機確認済みだが、2つは未確認。
`model_catalog_json` のdeserializerとconfigの `model` 値の両方で通ることを見る。

通らない場合の逃げ道は用意してある。`routers.ORCAROUTER.catalog_prefix` を
`orca-` のような区切り無しへ変えれば済む。routerの判定はprefixではなく registry の
`router` field で行っているので、prefixの変更は表示とslug衝突回避にしか影響しない。
ただし内部IDが変わるので、既に保存したprofileがあれば選び直しになる。

### 参照

- P0で確認した事実と未確認項目: 本文書の「Status」節
- router契約: [src/codex_openrouter/routers.py](../src/codex_openrouter/routers.py)
- 契約テスト: [tests/test_routers.py](../tests/test_routers.py)
- 実取得fixture: [tests/fixtures/orcarouter-models.json](../tests/fixtures/orcarouter-models.json)（2026-08-19 keyless、代表11件）
- 合成fixture: [tests/fixtures/orcarouter-models-contract.json](../tests/fixtures/orcarouter-models-contract.json)（実データにまだ現れていない契約edge case）
- 前回の引き継ぎ: [task/0814-oss-public-release-plan.md](0814-oss-public-release-plan.md) の「引き継ぎ（2026-08-15時点）」

## 中止記録（2026-08-19）

- OrcaRouterのprovider、guard route、認証、候補取得、UI、fixture、docsをworking treeから撤去した。
- 導入済みprofileは撤去前に`deepseek/deepseek-v4-flash-0731`単独へ戻し、既定effortは`high`とした。
- OrcaRouterの返金申請は利用者側で完了した。ローカルKeychain itemは引き続き保持し、値の読み出し、削除、remote key失効、console変更は行わない。
- 後続実装はOpenRouter modelだけを`tool_mode: direct`へ分離し、structured/freeform canaryと5段階のtool互換表示を追加する。
- commit / push / PR / merge / tag / releaseは引き続き未実施。

### 後続実装の現在地

- repositoryはOpenRouter専用構造へ戻し、Flash 0731単独・既定effort `high`へ変更済み。
- 実keyでのcanary結果は`partial`（structured function成功、freeform tool非互換）。
- upgrade時に旧multi-provider registryを移行し、OpenRouterの追加modelを維持しながら廃止provider固有entryを原子的に除く。
- 現在のCodexが純正appを使用中のため、導入済みruntimeのupgradeとlauncher 2 cycleはこのtask終了後の対話操作として残る。

## 後続: OpenRouter専用・最小Tool Bridge（2026-08-20時点）

Status: **source実装とrepository verificationは完了。導入済みruntimeと実課金E2Eは未実施。**

- `src/codex_openrouter/toolbridge.py`へHTTP・Keychain・profileから独立した純粋変換層を追加した。
  通常functionは維持し、namespace childとcustomだけをrequest固有のstrict functionへ変換して
  SSE/JSON responseで復元する。未知tool、重複名、不完全JSON、欠落done、途中切断はfail-closed。
- tool requestだけRouter Metadataを要求し、provider・attempt・candidate count・statusだけを抽出する。
  metadata本文、pipeline、prompt、tool argumentsはCodexにもlogにも残さない。provider価格sortは追加せず
  OpenRouterのAuto Exacto既定routingを維持する。
- tool cacheとlauncherへ検証provider・attempt・tool contract versionをoptional fieldとして追加した。
  CLI JSON schema versionは1のまま、旧fixtureを含むSwift decoder互換を維持する。
- `models/tool-wire-builds.json`でChatGPT build 6720と6662だけを互換保証する。未知buildでは
  Keychainへ触れる前にOpenRouter起動だけを止め、純正ChatGPT利用は妨げない。
- `UPSTREAMS.md`と`models/upstreams.json`へcodex-relay/OpenAI Codexの参照commitとfile hashを固定した。
  週次workflowは差分レポートだけを出し、自動mergeしない。
- unit test runnerはloopback以外のDNS/socket接続を拒否する。324 tests、ruff、compileall、
  synthetic E2E、secret scan、Swift launcher/credential build、decoder compat、`zsh -n`、
  upstream manifest、`git diff --check`が全てPASS。
- sourceの`./codex-openrouter check`はChatGPT 26.814.41407 build 6720、tool contract 2、
  Flash 0731単独profile、stock app無変更でPASSした。

導入済みruntimeの内容digestはsourceと異なるため、runtime upgrade、少額課金canary、
実機shell/apply_patch/browser、session JSONL照合、launcher 2 cycleはまだ実施していない。
OpenRouter Keychain itemは2026-08-20のread-only checkでは`available`だったが、値は読み出していない。

## 引き継ぎ（2026-08-20・独立CLI実機E2E）

### Gitと検証状態

| 項目 | 状態 |
|---|---|
| branch | `codex/openrouter-tool-bridge` |
| 機能commit | `688c4bf`（Tool Bridge、tool互換UI、catalog/profile、fixture・tests） |
| CI/upstream commit | `fd361a3`（外部通信遮断runner、週次監視、Release allowlist） |
| docs commit | 本節とREADMEを含む次のcommit。独立CLI起動用handoffへ確定HEADを記録する |
| repository tests | loopbackだけを許可し、外部socketを遮断したrunnerで325 tests PASS |
| push / PR / tag / release | 未実施。今回も行わない |

`scripts/build_release.py`のroot allowlistへ`UPSTREAMS.md`を追加済み。
`tests/test_maintenance.py`がこの文書を必須Release fileとして固定するため、
tracked fileなのにarchiveへ入らない退行はunit testで止まる。

### 実機の現在値

```text
ChatGPT=26.814.41407 build 6720
tool_wire=compatible contract=2
profile models=1 default=deepseek/deepseek-v4-flash-0731
reasoning effort=high
catalog_block=absent
provider_block=present
model=gpt-5.6-sol
model_provider=openai
keychain=available（値は未読）
source digest=b0ba094b406d5f433653991974a59ff78959a1e4f0e5aca464499afd25ac6433
installed digest=2f25c7da197885fd2d3bfe7aa4c5881a763ff324793bca50d9b399f73a37ce8e
```

### 独立CLIが行う順序

1. native providerへ固定されたCLI taskで、この節と一時handoffに記録された確定HEADを読む。
2. `git status`がcleanで、Computer Useのread-only smoke testが成功するまでDesktopを終了しない。
3. 成功後、Desktopを通常終了する。強制終了しない。
4. OAuthのgrantとOpenRouter側の有限spend limit確認はユーザーへ操作を渡す。値を読まない。
5. repositoryの`./codex-openrouter upgrade`を使う。PATH上の導入済みCLIからupgradeしない。
6. source/installed digest一致と`doctor --runtime --secret-scan`を確認してから、有料canaryを各1回だけ実行する。
7. `mktemp -d`のworkspaceでshell、`apply_patch`、browser namespaceを確認し、session JSONLとRouter Metadataを照合する。
8. `scripts/macos_installed_e2e.zsh`を2 cycle実行し、最後にinactive stub、token削除、catalog解除、stock app無改変を確認する。

認証失敗、429、5xx、通信失敗では有料requestを自動再試行せず、profileとtool cacheを変更しない。
upgradeのatomic verification失敗は自動rollbackへ任せる。起動・終了・config復元・stock app不変条件が
壊れた場合だけ証拠を保存して停止し、手動rollbackはユーザー確認後に行う。
