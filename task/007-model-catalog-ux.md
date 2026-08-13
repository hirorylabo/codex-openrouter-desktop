# 007 モデル追加UX（ライブcatalog・価格表示・フィルタ・ZDRのモデル単位化）

## Context

現状は同梱registryの検証済み5モデルをチェックボックスで出し入れするだけで、モデルを増やすには
registry.jsonを手で書く必要がある。利用者は「Guardrailなしを既定にして、話題のモデルを簡単に
追加したい。価格を見たい。ZDR・学習なし・freeで絞りたい」ことを求めている。

実データを確認した結果、要求の一部は前提が違っていた。実装前提として先に記録する。

| 確認したこと | 実測値 |
|---|---|
| `/api/v1/models` 全モデル | 410件 |
| tool calling可 | 343件 |
| ZDR稼働endpointあり | 273件 |
| **ZDR かつ tools（現状の実質候補）** | **174件** |
| reasoning effort公開 | 57件 |
| free(0円)モデル | 19件 |
| **free かつ ZDR** | **0件** |
| ZDR endpointを持つproviderのうち学習するもの | 0件（学習するのはDeepSeek/Liquid/Nvidiaの3社のみ） |

ここから3つの帰結がある。

1. **freeモデルは現状1件も呼べない。** `guard.py` の `prepare()` が全リクエストへ
   `provider.zdr=true` を無条件に立てるため、ZDR endpointを持たないfreeモデルは必ず失敗する。
   → 利用者の判断で **ZDR強制をモデル単位へ変更する**。
2. **「データ学習なし」フィルタはZDR下では1行も減らさない。** ZDRを出すproviderに学習するものが
   ないため。ZDRを外せる新設計では意味を持つので、フィルタではなく**バッジ+フィルタ両方**で出す。
3. **「接続数」はOpenRouterが公開していない。** 公開されるのは
   `/api/v1/datasets/rankings-daily` のトークン総数で、日次トップ50のみ・API key必須・500 req/日。
   → **「トークン利用量」としてラベルし**、圏外は `—` を出す。

あわせて既存の不具合を2件見つけたので同時に直す。

- `models/registry.json` の `fallback_headline` が陳腐化している。
  `deepseek/deepseek-v4-pro` は 0.435→**1.168**（2.7倍）、`z-ai/glm-5.2` は 0.07→**0.50**（7倍）。
  オフライン時にこの値が価格として表示される。
- `registry.json` の `negative_model` はコードのどこからも参照されていない死んだ設定。

## 方針

registryを手書きの正本から、**ライブcatalogから導出できるもの**へ変える。導出規則は現registryの
5件を1バイトも変えずに再現できることを回帰テストで固定する（下の「導出規則」は全て実データで検証済み）。

「モデル追加」は、catalog cacheから **installed registryへエントリを実体化する**操作とする。
これによりcatalog生成・価格・guard・watcher・doctorの下流は既存のまま動く。

## 変更詳細

### 1. Guardrailを任意にする（exact match → subset）

- `src/codex_openrouter/openrouter.py`: `available != expected_models` の判定を
  `expected_models - available`（missingのみ）へ緩める。`extra` 側の失敗を廃止する。
- 同ファイルのQwen判定を削除する。あれはGuardrailが実際に効いているかを見る canary であって
  Qwen自体の排除ではない。Guardrailなしでは常にQwenが見えるため、残すと必ず失敗する。
- `src/codex_openrouter/doctor.py`（`concrete == expected` の箇所）も同じくsubsetへ。
- `models/registry.json` から死んでいる `negative_model` を削除する。
- `README.md` / `README.en.md` の導入手順3からGuardrail必須を外し、**spend limitを必須**へ格上げする。
  Guardrailが担っていた「鍵が漏れても課金は5モデルまで」を失うので、代替の歯止めを明示する。

失うもの・残るものを手順書に明記する。`profile ⊆ key実効集合` は残るので「pickerに出るモデルは
必ず呼べる」は維持され、モデル引退・rename・制限付きkeyは今までどおり検出できる。

### 2. ZDR強制をモデル単位にする

- registryの各モデルに `zdr_supported: bool` を追加する（`/endpoints/zdr` の status==0 から追加時に確定）。
  profile schemaは**変更しない**。ZDRの可否はモデルの性質であって選択の性質ではない。
- `src/codex_openrouter/guard.py`: `Guard.__init__` に `zdr_models: Iterable[str]` を追加。
  `prepare()` は既にbodyをparse済みなので `document.get("model")` を見て、その集合に入るときだけ
  `provider["zdr"] = True` を立てる。
- `src/codex_openrouter/supervisor.py:230` の `Guard(...)` へ `ResolvedProfile.registry` 由来の集合を渡す。
- `src/codex_openrouter/doctor.py`: ZDR canaryを `zdr_supported` のモデルだけに限定し、
  非ZDRモデルは**毎回WARNで理由付きで報告する**（プロンプトがproviderに保持されうる旨）。
- `src/codex_openrouter/pricing.py`: `parse_zdr()` は現状ZDR endpointが無いモデルで
  `PricingUnavailableError` を投げるため、**非ZDRモデルを1件選ぶと全モデルの価格がfallbackへ落ちる**。
  ZDR価格をモデル単位で任意にする。
- `src/codex_openrouter/catalog.py`: 非ZDRモデルのdescriptionへ「ZDRなし」を入れ、
  設定画面だけでなく純正pickerからも見えるようにする。

### 3. catalog取得（新規 `src/codex_openrouter/modelcatalog.py`）

`pricing.py` の `fetch_json` / `should_fetch` / `_read_state` / `_write_state` のTTL方式を再利用する。

- `/api/v1/models`（無認証）と `/api/v1/endpoints/zdr`（無認証）を取得。
- `/api/frontend/v1/all-providers` から `dataPolicy.training` を取得して学習バッジに使う。
  **これは非公開のfrontend APIなので**、失敗時はバッジを「不明」にして機能全体は止めない。
- `/api/v1/datasets/rankings-daily`（**API key必須**、`period=day`、直近30日）を1日1回だけ取得し、
  1d/7d/30dのトークン総和を作る。join keyは `model_permaslug` ↔ `canonical_slug`。
- cacheは `~/.local/share/codex-openrouter-desktop/model-catalog-cache.json`（0600）。
  成功TTL 86400秒 / 失敗backoff 3600秒は `registry.json` に `catalog_refresh` として契約を置く。

**導出規則**（現registryの5件で全て一致することを確認済み）:

| registryの項目 | 導出元 |
|---|---|
| `display_name` | `name` から先頭の `"ベンダー: "` を除去 |
| `canonical_slug` | `canonical_slug`（5件とも一致） |
| `context_window` | `context_length` |
| `openrouter_modalities` | `architecture.input_modalities` |
| `codex_modalities` | 上記 ∩ `{text, image}` |
| `efforts` | `reasoning.supported_efforts` を昇順へ反転 |
| `default_effort` | `reasoning.default_effort` |
| `supports_parallel_tool_calls` | `supported_parameters` に `parallel_tool_calls` を含むか（5件とも一致） |
| `zdr_supported` | `/endpoints/zdr` に status==0 のentryがあるか |
| `capability` | `description` を要約（唯一導出しきれない項目。既存5件は現在の日本語を維持） |
| `fallback_headline` / `fallback_zdr` | 追加時のライブ値をsnapshot |

### 4. モデル追加を1トランザクションに載せる

- `src/codex_openrouter/app.py` に `installed_registry` パスを追加する。
- `src/codex_openrouter/settings.py` の `_apply_locked()`: `models` に installed registry 未収録の
  idが含まれるとき、catalog cacheからエントリを実体化して **installed registryも同じ
  `atomic_promote` のtransactionへ載せる**。検証失敗時は registry も一緒に戻る。
  `verify_promotion()` に「promotion後のregistryが選択を全て含む」判定を足す。
- `APPLY_KEYS` は `schema_version` / `models` / `default_model` のまま**増やさない**。
  表示名・effort・価格の入力口は引き続き持たせない。
- catalog cacheに無いidは従来どおり「未検証モデルは選択できません」で落とす。

### 5. CLI（`src/codex_openrouter/cli.py`）

- `codex-openrouter models list --json [--refresh]` を追加する。全候補と価格・公開日・
  トークン利用量・ZDR/学習/free/reasoningの各フラグを返す。秘密値は含めない。
- `profile show --json` の契約は**変えない**。ネットワークに依存させず即座に現在の選択を返す。
  設定画面は先に `profile show` で描画し、`models list` を非同期で流し込む。

### 6. 設定画面（`portable/launcher/app/`）

- `ModelSettingsWindow.swift` の縦積みチェックボックスを `NSTableView` へ置き換える。
  列: 選択 / モデル / IN $/M / OUT $/M / 公開日 / トークン量(7d) / バッジ。
  `NSSortDescriptor` で価格・公開日・利用量ソート。選択済みは常に先頭グループへ固定して、
  ソートやフィルタで今の選択が視界から消えないようにする。
- 絞り込み行: 検索フィールド +「ZDRのみ」「学習なしのみ」「無料のみ」「reasoning対応のみ」。
  既定は**ZDRのみON**（安全側）。
- テーブルのdata sourceは新規 `ModelCatalogTable.swift` へ分ける。ソート・フィルタ・列描画は
  設定画面の保存フローとは別の責務で、1ファイルに混ぜると読めなくなるため。
- `ProfileBridge.swift` に `CatalogEntry` と `models list` 呼び出しを追加する。
- **非ZDRモデルを選ぶときは確認シートを出す**。プロンプトがproviderに保持されうることを明記し、
  管理画面にも選択中である旨のバッジを常時出す。既定の安全性を下げる変更なので、黙って通さない。

## Verification

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src portable scripts
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py
python3 scripts/secret_scan.py --tree .
xcrun swiftc portable/launcher/app/*.swift -o /tmp/CodexOpenRouterLauncher
PYTHONPATH=src python3 -m codex_openrouter models list --json --refresh | head
```

新規テスト:

- **導出規則が同梱registryの5件を再現する**（実APIのsnapshot fixtureに対して）。導出が壊れたら落ちる。
- 非ZDRモデルを含む選択で、他モデルの価格がfallbackへ落ちないこと。
- `Guard.prepare()` がZDRモデルにだけ `provider.zdr` を立てること。
- `validate_key_and_profile` が extra を許し missing で落ちること。
- catalog cacheのTTL・オフラインfallback・rankings欠損時の `—`。

更新が要る既存テスト: `tests/test_profile_auth.py`（exact match / Qwen）、`tests/test_guard.py`、
`tests/test_doctor.py`。

実機:

```bash
scripts/macos_installed_e2e.zsh
```

設定画面でソート・フィルタ、非ZDRモデルの追加と確認シート、保存後の `doctor` 警告、
純正pickerでの価格表示までを目視する。

## 未検証・リスク

- **rankings の join は未検証。** `model_permaslug` ↔ `canonical_slug` の対応はドキュメントの例と
  形式が一致するだけで、実データで突き合わせていない（API keyが要るため）。実装初手で確認する。
- rankingsは日次トップ50のみ。圏外モデルは利用量が出ない。
- `/api/frontend/v1/all-providers` は非公開APIで、予告なく壊れうる。失敗時は「不明」へ倒す。
- ZDRのモデル単位化はアプリ全体の不変条件をひとつ落とす。利用者の明示的な判断だが、
  doctorとUIで毎回可視化することが条件。
- headline価格は呼び出しごとに揺れる（`z-ai/glm-5.2` で `/models` と `?category=programming` が
  別値を返した）。表示は「参考値」と明示し、課金の正本はOpenRouter側であることを既存の
  `PRICE_NOTE` と同じ文言で維持する。

## Status

着手前。PR #2（`codex/model-settings-launcher`）の上に積むstacked branch
`codex/model-catalog-ux` で実装する。
