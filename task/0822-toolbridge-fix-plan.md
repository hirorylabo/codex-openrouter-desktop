# 案A: PR #24 を実測ベースで直し、自作実装へ戻す

作成日: 2026-08-22 / 対象 branch: `codex/openrouter-tool-bridge`（PR #24, draft）/ 課金: 許容済み

## Context

0822 の codex-router trial は目的を達した ——「Responses 契約を捨てて Chat Completions へ落とせば
DeepSeek でも tool は通る」を実証（gate 5 PASS）。だが同時に **native GPT が常駐 service に完全依存**
することも実測した（gate 4: service 停止中は native が `waiting for network` で無限ハング）。
この不安定さを恒久的に飲みたくないため、**自作実装へ戻す（案A）** を選択した。

戻るにあたり、PR #24 が止まった原因を実測で詰め直した。結論は当初の理解と違う:

- 既知の原因（`0821-opencodex-walkthrough.md` §1.5）は「bridge が起動していなかった」1件だった
- **今回の実測で、起動しても効かない 2つ目の原因が見つかった。** 現行 bridge が生成する tool は
  `apply_patch` を **0/4** でしか引き出せない
- 0821 §5 の案B 7ステップのうち **2つは実測で否定された**（`require_parameters` と
  `supports_parallel_tool_calls` の扱い）
- **Chat Completions への全面移行は不要**。`/responses` 直送のままで通る

つまり案B をそのまま実行すると、1つ目だけ直して 2つ目で再び gate 2 に落ちる。この計画はそれを防ぐ。

---

## 今回の追加実測（すべて OpenRouter へ直接、`deepseek/deepseek-v4-flash-0731`、`provider {sort:"price", zdr:true, data_collection:"deny"}`）

### 1. OpenRouter は `type:"custom"` を無言で捨てる

`https://openrouter.ai/api/v1/responses` へ Codex と同形の custom(lark) tool を送った結果:

| 送った形 | 非stream | stream |
| --- | --- | --- |
| `type:"custom"` + `format:{grammar,lark}` | **tool call なし**（`reasoning` + `message` のみ、status 200、error なし） | **tool call なし** |
| bridged function（`content` param + description に grammar） | `function_call` / `*** Begin Patch` あり | （下記3で再測） |

**status 200・エラーなしで黙って落ちる。** §1.5 の「bridge が起動せず custom が素通しされた」が
実機の上流でどう見えるかがこれで確定した。gate 2 の `aborted` と完全に整合する。

### 2. 現行 bridge が出す形は 0/4（新発見）

`toolbridge._strict_function()` が custom tool から生成する function を、実物どおり再現して測った
（stream、`tool_choice:"auto"`、各4回）:

| 送る形 | `*** Begin Patch` を含む tool call |
| --- | --- |
| A. `name=apply_patch` / `field=content` / strict なし | **4/4** |
| B. `name=codex_bridge_0003` / `field=patch` / strict なし | 3/4 |
| C. **B + `strict:true` + `additionalProperties:false`（現行の実装そのもの）** | **0/4** |

B と C の差は `strict` だけ。C の payload は毎回 0〜1 byte で、実質何も返っていない。
`/responses` 側でも同じ形が 0 だった（stream/非stream とも）。

裏付け: 0821 §1.7 の実測で DeepSeek の `structured_outputs` は **22/30 endpoint** しか公称しておらず、
`sort:"price"` はその 8/30 を普通に引く。`strict:true` は structured outputs を要求するので、
引いた瞬間に壊れる。**`strict` を custom→function 変換に付けたのが 2つ目の root cause。**

### 3. description の散文が効く。grammar 単体は決め手ではない

| description | 成功 |
| --- | --- |
| 切り詰めた lark のみ | 3/4 |
| 完全な lark のみ | 3/4 |
| Codex の apply_patch 説明文（散文＋例）+ 完全な lark | **4/4** |
| Codex の説明文のみ（grammar なし） | **4/4** |

現行 `_description()` は元の description を保つので散文は生きているが、**`format.definition`（lark）は
一切読んでおらず捨てている**。grammar は付けるべきだが、これ単体では gate は通らなかったはず。

> n=4/セルの小標本で、`sort:"price"` の provider 抽選ノイズが乗る。3/4 と 4/4 の差は弱い。
> **強いのは 0/4 対 4/4（`strict` の有無）と、custom の 0 件**の2点だけ。計画はこの2点に依拠する。

---

## 根本原因は 2 つ

| # | 原因 | 症状 | 場所 |
| --- | --- | --- | --- |
| 1 | **bridge が起動しない**（既知・§1.5） | `use_responses_lite=true` を継ぐと tool は top-level `tools` ではなく `input[0].additional_tools` に載る。`prepare_document()` は `tools is None` で即 return | `toolbridge.py:208-210`、`catalog.py` の `NATIVE_ONLY_FIELDS` |
| 2 | **起動しても効かない**（新規） | custom→function 変換が `strict:true` + `additionalProperties:false` を付ける。実測 0/4 | `toolbridge.py:169-193` `_strict_function` |

1 だけ直すと 2 で落ちる。**両方を同じ変更セットで直す。**

---

## 方針

1. **`/responses` 直送を維持する。** bridged function は `/responses` で通ることを確認済み。
   Chat Completions への全面移行（LiteLLM 相当の自作）は不要で、これは大きなスコープ削減になる
2. **実測で通った形に寄せる。** LiteLLM の `custom_tools.py`（163行・MIT）が、我々が実測した
   wire と完全に一致する参照実装。`{content:string}` / grammar を description へ / strict なし /
   元の名前を保つ。**これを規範にする**
3. **fail-closed は維持する。** 変換不能な tool call を推測で補わない現行方針は変えない
4. **推測で足さない。** 案B の `require_parameters` は実測で不要かつ有害と判明したので入れない

---

## 変更詳細

### F1. bridge を lite 形式でも起動させる（原因1）

`toolbridge.prepare_document()` が top-level `tools` と `input[]` 内の
`{"type":"additional_tools","role":"developer","tools":[…]}` の **両方**を収集する。
opencodex の `collectResponsesToolGroups()`（MIT）と同じ考え方で、catalog の flag に依存しない。

- 変換後は元の置き場所へ書き戻す（lite なら `additional_tools` の中、classic なら top-level）
- 両方に tool がある場合は両方を1つの `ToolMap` に集約し、名前衝突は既存の `identities` 検査で拾う
- どちらにも無い場合だけ現行どおり `ToolMap()` を返す

### F2. `strict` と `additionalProperties` を外す（原因2・最重要）

`_strict_function()` の custom 分岐から `"strict": True` と `"additionalProperties": False` を削除する。
関数名も実態に合わせて `_bridged_function()` へ改名する。

`target.kind == "function"`（namespace 配下の通常 function）の分岐は `deepcopy` なので元の schema を
保つ。ここは触らない。

### F3. input field を `content` に統一する

`input_field` の `"patch" if name == "apply_patch" else "input"` をやめ、**常に `content`** にする。
実測で通った形であり、LiteLLM / codex-router と同一になる。`_unwrap_custom()` の契約検査
（`set(parsed) != {field_name}`）はそのまま使える。

### F4. lark grammar を description へ畳む

LiteLLM の `_grammar_suffix()` 相当を移植する。

```
\n\nFormat:\n```{format.syntax}\n{format.definition}\n```
```

`format` が無い / `definition` が空なら何も足さない。実測した wire（`hasFormatHeader: true`）と一致する。

### F5. tool 名を保つ（namespace は `__` で平坦化）

`codex_bridge_NNNN` への一律リネームをやめる。

| 対象 | 変換後の名前 |
| --- | --- |
| top-level custom | 元の名前のまま（`apply_patch`） |
| namespace 配下 | `<namespace>__<name>`（例 `codex_app__create_thread`） |
| top-level function | 現行どおり素通し |

codex-router が実機で使っている方式で、trial の capture でも `codex_app__automation_update` を
実測している。意味を保ちつつ衝突も避けられる。衝突検出（`identities` / `used`）と
`ToolBridgeError` による fail-closed は現行のまま残す。`TRANSFORMED_PREFIX` に依存した
`target_for_response()` の分岐は、`ToolMap.transformed` の実引きだけに置き換える。

### F6. `use_responses_lite` を中和する（F1 の保険）

`catalog.py` の `NATIVE_ONLY_FIELDS` に `"use_responses_lite": False` を追加する。
F1 があれば形式に依存しないが、**classic 形式のほうが実測の量が多い**（trial の 50行の capture は
全て classic）。既知の経路へ寄せる。`validate()` は `NATIVE_ONLY_FIELDS` を回すので検査は自動で付く。

### F7. `_description()` の日本語 prefix を外す

`Codex custom tool \`apply_patch\`。` の prefix は、名前を保つ F5 の後は情報を足さない。
元の description（Codex の apply_patch 説明文）だけを残し、F4 の grammar を末尾に足す。

---

## やらないこと（実測が否定した案B のステップ）

| 案B step | 判断 | 根拠 |
| --- | --- | --- |
| 2. `guard.py` に `provider.require_parameters = true` | **入れない** | `parallel_tool_calls` を公称する endpoint は 1/31 しかなく、`require_parameters` は満たせない候補を 404 で消す。OpenRouter は tools 付き request を自動で tool 対応 provider へ絞る |
| 5. `supports_parallel_tool_calls` を false へ | **保留** | app は `parallel_tool_calls: true` を送り、そのまま通ることを trial で実測した。未対応 endpoint でも OpenRouter は 404 にせず転送する |
| — `tool_choice` の `required → auto` downgrade | **入れない** | 3モデルとも thinking mode を含め `required` を履行（6/6）。潰すと compat probe と subagent relay の強制 call が壊れる |
| — Chat Completions への全面移行 | **不要** | bridged function は `/responses` で通る |

案B の残り（3. tool catalog nudge、4. 未宣言 tool guard、6. Run 1 再実行、7. `UPSTREAMS.md`）は有効。
4 は F1 が塞ぐ経路と重なるので、F1 の後に必要性を再判定する。

---

## 実装順

1. **回帰 test を先に置く**（F1・F2 の証拠を固定する）
   - lite 形式（`additional_tools`）の request で `has_tools` が真になること
   - 変換後の function に `strict` と `additionalProperties` が**無い**こと
   - `content` field / 元の名前 / `Format:` を含む description
2. F2・F3・F4・F7（`_strict_function` / `_description` まわり、1ファイル）
3. F5（命名。`target_for_response` と `_lookup_original` の連動を確認）
4. F1（`prepare_document` の収集元拡張。書き戻し位置に注意）
5. F6（`catalog.py`）
6. 既存 test の更新 —— `tests/test_toolbridge.py` は `strict is True` と `required == ["patch"]` を
   assert しているので、契約変更に合わせて書き換える（`test_function_passes_and_custom_namespace_become_strict_functions` 他）
7. `UPSTREAMS.md` に LiteLLM `custom_tools.py`（MIT）と opencodex（MIT）の参照を追加
8. 実機 Run 2（gate 1 から。gate 2 が本番）

## 触るファイル

| 場所 | 変更 |
| --- | --- |
| `src/codex_openrouter/toolbridge.py` | F1〜F5, F7（本体） |
| `src/codex_openrouter/catalog.py` | F6（`NATIVE_ONLY_FIELDS` に1行） |
| `tests/test_toolbridge.py` | 契約変更に伴う更新＋新規回帰 test |
| `tests/fixtures/` | lite 形式（`additional_tools`）の request fixture を追加 |
| `UPSTREAMS.md` | 参照実装の MIT notice |

`guard.py` は触らない（`require_parameters` を入れないため）。ENDPOINT も `/responses` のまま。

## Verification

```bash
# local gate
PYTHONPATH=src python3 -m unittest discover -s tests --buffer
uvx ruff@0.16.3 check .
env -u OPENROUTER_API_KEY ./codex-openrouter doctor --runtime --secret-scan

# 生成 catalog に use_responses_lite の中和が入ったか
python3 -c "import json;d=json.load(open('$HOME/.codex/model-catalogs/codex-openrouter.json'));\
[print(m['slug'], m.get('use_responses_lite'), m.get('tool_mode')) for m in d['models']]"

# bridge が起動しているか（forwarded 行に tool_request が付くか）
python3 -c "import json;rows=[json.loads(l) for l in open('$HOME/.local/share/codex-openrouter-desktop/state/guard.log') if l.strip()];\
print(sum(1 for r in rows if 'tool_request' in r), '/', len(rows))"
```

**wire レベルの回帰確認**（課金・小）: `~/.local/share/codex-openrouter-trial/probes/` に置いた
probe と同じ手口で、変換後の tool 定義をそのまま OpenRouter へ投げて `*** Begin Patch` を含む
call が返るかを 4回測る。**現行の形が 0/4 なので、ここが 4/4 に変わることが F2 の合格条件。**

**実機 gate**: Run 2 を gate 1 から。gate 2（`apply_patch`）が本番。gate 5 の教訓として、
試行のプロンプトは**補足なしの一文**にする（文脈が混ざると読み取りだけで終わる）。

## リスクと打ち切り条件

- **`strict` を外すと引数が不正な JSON で返り得る。** `_validate_function_arguments()` と
  `_unwrap_custom()` の既存検査で fail-closed する設計は維持する。壊れた call を推測で補わない
- **provider 抽選ノイズは消えない。** `require_parameters` を入れない判断なので、gate は
  1回の失敗で打ち切らず、成功／失敗の回数で読む。ただし**成功するまで回して記録を上書きしない**
- **F5 の改名は `TRANSFORMED_PREFIX` 前提の分岐に触る。** 既存 test が守っているので、
  test を先に赤くしてから直す
- **codex-router との併存不可。** 実機 Run の前に trial を撤去する必要がある
  （`bin/disable` → `config.toml` の差分ゼロ確認 → `./codex-openrouter launch`）。
  戻す順序は `0822-codex-router-trial.md` の「復帰手順」のまま

## Status

**実装完了・実機 Run 2 待ち**（2026-08-22）。

| step | 状態 |
| --- | --- |
| 1. 回帰 test（6件、赤→緑を確認） | **完了** |
| 2. F2/F3/F4/F7（strict除去・`content`・grammar・prefix除去） | **完了** |
| 3. F5 命名（元の名前を保ち namespace は `__` 平坦化） | **完了** |
| 4. F1 `additional_tools`（`_tool_group`） | **完了** |
| 5. F6 catalog（`use_responses_lite: False`） | **完了** |
| 6. 既存 test 更新 | **完了**（377 tests OK） |
| 7. `UPSTREAMS.md` | **完了** |
| 8. 実機 Run 2 | 未（promote と trial 撤去が先） |
| CLI 動作確認 | **完了**（`check` PASS / `verify-tools` 3回とも `verified`） |

### 計画に無かったが必要だった変更

**`TOOL_CONTRACT_VERSION` を 2 → 3 へ上げた。** wire を変えたので上げないと、
`toolcompat` が古い契約下で測った結果をそのまま再利用する。実際 `state/tool-compatibility.json`
には build 6849 / `partial` / **「structured functionは成功、freeform toolは非互換」**が
残っていた —— これは `strict:true` を付けていた頃の測定で、いまは誤り。
version を上げると `toolcompat.py:92` の判定で自動失効する。
`models/tool-wire-builds.json` と 2つの fixture も 3 へ揃えた。

fixture の custom tool に `format`（lark）を持たせた。従来は持っておらず、
**grammar を捨てている実装の欠陥を test が隠していた**。

### wire レベルの検証結果（F2 の合格条件）

実装が生成する tool 定義をそのまま OpenRouter の `/responses` へ stream で 4 回送った:

| 形 | 結果 |
| --- | --- |
| 旧実装（`codex_bridge_NNNN` / `patch` / `strict:true`） | **0/4** |
| 新実装（`apply_patch` / `content` / strict なし / grammar あり） | **4/4**（60〜61B の正しい patch、`{"content":…}` の unwrap も成立） |

### CLI での動作確認（2026-08-22、repo source）

```
./codex-openrouter check
  → CHECK: PASS  /  tool_wire=compatible contract=3
```

`models verify-tools`（実 API の canary。`prepare_document` を通る）:

| run | 結果 |
| --- | --- |
| 1 | `unsupported` —— **structured** canary が失敗。freeform は短絡で未試行 |
| 2〜4 | **`verified`** ——「Tool Bridge経由でstructured functionとfreeform toolを実測済み」 |

cache は contract 3 / `verified` に更新された。**修正前の同じ canary は `partial` /
「freeform toolは非互換」を記録していた**ので、CLI 経路でも freeform bridge が
通るようになったことが確認できた。

run 1 の `unsupported` は provider 抽選による structured canary の外れ。
0821 §1.7 でも「structuredは3/4成功」と実測しており、今回も 3/4 で一致する。
**canary は本質的に揺れるので 1回で判定しない。**

> `doctor` サブコマンドは installed の doctor バイナリを exec するため
> （`cli.py:517`）、repo の変更は反映されない。repo source を見るのは `check`。

### 判明した設計上の弱点（この変更の範囲外）

**structured canary が失敗すると freeform を一度も試さない**（`toolcompat.py:333-337`）。
structured probe は `strict:true` + `enum` を送り、`sort` を指定しないので毎回別の
endpoint を引く。DeepSeekで structured_outputs を公称するのは 22/30 なので、
外れを引くと **freeform が健全でも `unsupported` と表示される**。
`tool_support` は description 表示専用で可視性を gate しないため実害は小さいが、
「1回の外れで実態より悪く出る」性質は残る。対処するなら短絡をやめて両方測るか、
canary に `sort` を付けて endpoint を固定する。

### 次にやること

1. runtime を promote する（`./codex-openrouter` は
   `~/.local/share/codex-openrouter-desktop/current` を優先するため、
   現状の `doctor` は**旧コード**を見ている。「tool contract 2で確認済み」表示がその証拠）
2. codex-router trial を撤去する（併存不可。`bin/disable` → `config.toml` 差分ゼロ確認）
3. 実機 Run 2 を gate 1 から
