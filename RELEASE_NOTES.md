# v0.2.1

v0.2.0 の保守更新です。破壊的変更はありません。中心は **純正 ChatGPT.app の2回の更新への追従**と、**出荷済みコマンド2本が落ちていた不具合の修正**です。

## 純正appの更新に追従しました

v0.2.0 で ASAR パッチを撤去し、純正appを一切変更しない方式へ移りました。ただし catalog の OpenRouter エントリは **native エントリを clone して差し替える**方式なので、純正appが catalog にフィールドを増やすと OpenRouter モデルがそれを黙って継ぎます。この期間に実際 2回起きました。

| build | 増えたフィールド | 対応 |
| --- | --- | --- |
| 6662 (`26.810.52044`) | `include_apps_usage_instructions` | Apps(Connectors) は ChatGPT アカウント側の機能なので中和する |
| 6720 (`26.814.41407`) | `node_repl_disabled` / `node_repl_auto_review_required` | native と同値へ固定し、将来の反転に追随させない |

いずれも中和済みで、**pickerとモデルの動作は 6720 で実機確認済み**です（native 5 + OpenRouter 5 の10件、OpenRouter モデルでの応答、guard の `forwarded`、終了後の vanilla 復帰まで）。

### 同じ見落としを繰り返さないようにしました

- **clone テンプレートの drift 検知**を入れました。テンプレートが既知のフィールド集合を超えたら、開発者にはテスト失敗、利用者には `doctor` の WARN で出ます。未知フィールドが即座に有害とは限らないので **fail にはしません**
- **テンプレートを1世代 snapshot** するようにしました。上の検知はフィールド名の増加しか見ないので、値だけが変わる更新を素通りします。`doctor` は snapshot を取った build と実機 build が違うとき、動いたフィールド名を報告します
- 中和値は **native 側の型を保つ**ようにしました。codex の catalog deserializer は型を要求し、bool フィールドを null にすると catalog 全体を拒否します。1件でも型を外すと **picker から OpenRouter モデルが丸ごと消えます**。組み上げた catalog を実機 codex が受理することをテストで確認するようにしました

`node_repl_disabled` のような否定形のフィールドは「無効化」側へ倒しません。テンプレートが `tool_mode: code_mode_only` である以上、OpenRouter モデルからツールを丸ごと奪いかねないためです。中和の目的は無効化ではなく、native 側の変化に追随させないことです。

## 修正

- **`codex-openrouter rollback` と `migrate` が `NameError` で落ちていました。** 関数ローカル import が wrapper 側に残ったまま使用箇所だけがヘルパーへ移っていたためです。両コマンドを直し、未定義名を CI の ruff（`E9,F`）で止めるようにしました。`compileall` は構文しか見ないので、このクラスの誤りはそこでしか止まりません
- **配布物の収集を git 追跡ファイルに限定しました。** `build_release.py` が filesystem を `rglob` で走査していたため、macOS のローカルビルドで `.gitignore` 済みの `.DS_Store` と空ディレクトリが archive へ入り得ました（公開済みの v0.2.0 は CI の fresh clone ビルドなので混入していません）
- `.ruff_cache` を `secret_scan` の除外対象に足しました。lint gate を回すと必ず出来て、中に開発者の home path を含むため、ローカルの `secret_scan.py --tree .` が必ず落ちていました

## そのほか

- テスト出力を `--buffer` で静かにしました。green run でも stdout 92行・stderr 101行が出て、その中に `doctor.py` 由来の `FAIL:` が混ざっており、本物の失敗が埋もれていました
- GitHub Actions の pin を更新しました（checkout / setup-python / attest）

## 導入・更新

リポジトリから導入している場合は、次に「OpenRouterで起動」を押したときに自動で反映されます。手動なら **リポジトリの `./codex-openrouter`** を使ってください。

```bash
./codex-openrouter upgrade
```

純正appの更新をまたいだ場合、旧 catalog は破棄され次回起動で組み直されます。

非公式・無保証の実験版です。Apple Silicon macOS 専用。
