# 0823 アカウント切替対応計画: サードパーティproviderモード中のログアウト不可問題

作成日: 2026-08-23 / branch: `main` @ `92b70fc`(PR #31 後)

Status: **plan。実装前。**

---

## TL;DR

- `model_provider = "openrouter"` 起動中、ChatGPT.app はサードパーティ provider
  モードになり**アカウント UI(ログアウト/サインイン)を隠す**(仕様、app.asar 解析で確認)
- メニューバーの「hkをログアウト」は**効かないことをユーザーが実機確認済み**
  (メニュー項目自体は enabled で残るが、webview 側の auth state が
  サードパーティモードを維持するため無反応)
- native codex model が limit になったとき、別 ChatGPT アカウントへ切り替えられない
  → **OR モデルと native モデル双方の UX を守るため account 切替コマンドが必要**

## 原因(app.asar `app-initial-DwVrCWuo.js` の解析)

```
Rql() サイドバーフッター:
  p = authMethod === 'chatgpt' ? null : JKl(f)   // 非ChatGPT認証 → "OpenRouter"表示
  modelProviderName: p

wql() プロフィールドロップダウン:
  A = modelProviderName != null                   // ORモード: true
  W = !A && (requiresAuth || false)               // → false
  onLogOut = W && h != null ? pt : void 0         // → undefined = 項目非表示

auth state (uIr):
  authMethod = apiKey                             // config.tomlのmodel_providerが
                                                   // openai以外だとChatGPT tokenがあっても
                                                   // chatgpt扱いにならない
```

つまり **auth.json の ChatGPT トークンは無傷で残っている**。UI 層が
config.toml の `model_provider` を見て「API key モード」と判断し、
アカウント操作を全部隠しているだけ。

## 実測で確認済みのこと

| 確認 | 結果 |
|---|---|
| 歯車ポップアップにログアウトなし | ✅ 再現(新着情報/Chrome拡張/ショートカット/ヘルプのみ) |
| メニューバー「hkをログアウト」 | ❌ 無反応(ユーザー実機確認) |
| Settings → アカウント | ⚠️ 外部リンク扱い(セクション遷移しない) |
| auth.json の状態 | ✅ `auth_mode: chatgpt` / tokens 存在(破損なし) |

## 採用方針: 案1 `account` サブコマンド

既存の確立パターン(`_restore_selection_text` と同じ marker block +
top-level key 操作)で provider を一時的に native へ寄せ、アプリ本来の
サインイン UI を復活させる。

### コマンド設計

```
codex-openrouter account switch-native   # OR設定を退避してnativeモードでapp再起動
codex-openrouter account restore         # 退避したOR設定へ戻して再起動(または通常launch)
codex-openrouter account status          # 現在のモード・退避有無を表示
```

### 実装詳細(TDD)

1. **state 拡張**: `account_override_active: bool` を supervisor state へ追加
2. **switch-native**:
   - LifecycleLock 取得(ChatGPT.app 停止を要求 — assert_stock_not_running 流用)
   - `saved_provider_or` = 現在値("openrouter") + catalog/provider marker block を
     一時退避(configblock の upsert で `model_provider="openai"` へ)
   - catalog block も外す(`model_catalog_json` が OR catalog を指したままだと
     picker に OR モデルが出続けるため)。退避先は state へ記録
   - ChatGPT.app を純正 launch(supervisor 経由ではなく直接 LaunchServices)
   - ユーザーが UI でサインアウト→別アカウントでサインインするのを待つ
3. **restore**:
   - 退避した block/key を復元(state クリア)
   - 以降は通常 `codex-openrouter launch` と同じ
4. **doctor**: `account_override_active` 中はその旨を表示(うっかり放置の検知)

### Verification

- [ ] unit: switch/restore で config.toml の key と marker block が期待どおり出入りする
- [ ] 実機: switch-native 後に歯車ポップアップへ「Log out」が出現(cua-driver で確認)
- [ ] 実機: 別アカウントでのサインイン → restore → OR モデル利用可
- [ ] 実機: native gpt モデルも新アカウントで利用可(limit 回避フローの成立確認)

### リスクと緩和

| リスク | 緩和 |
|---|---|
| switch-native 中に通常 launch を叩くと二重管理になる | LifecycleLock で排他(state フラグを見て案内) |
| 退避中に crash して config が native のまま残る | doctor が `account_override_active` を警告して復旧手順を表示 |
| refresh_token の失効(サインアウトで古い token が消える) | 仕様として許容。切替前の警告文に明記 |

### 規模

- 単体テスト+実装: 半日
- cua-driver による実機 Verification: 1h
- 合計 ~半日〜1日 / 課金 $0(auth 操作のみのため)

---

## 未確定・要判断事項

1. switch-native 時に **catalog block も外すか**(picker から OR モデルを消す方が
   「本来の UI」に近い。残すなら OR 選択のまま native 認証という混在状態になる)
   → 推奨: 外す(picker 整合性優先)
2. `account switch-native` 中の guard は当然停止するが、**guard log への記録継続**
   要否 → 推奨: 不要(auth 操作は tool request を伴わない)
