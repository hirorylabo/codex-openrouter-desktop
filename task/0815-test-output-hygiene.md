# 0815: テスト出力の衛生と、テストの書き方の明文化

## Context

`task/0815-cli-import-hardening.md` で範囲外にした宿題を片付ける。あわせて、その作業中に
判明した「テストの書き方を間違えると検証が空回りする」2つの型を文書へ落とす。

### 直す対象

**1. green runでも100行超が出て、その中に`FAIL:`が混ざる**

修正前の計測（追跡下259テスト）:

| | 行数 |
|---|---|
| stdout | 92 |
| stderr | 101 |

出どころは`doctor.py`の`ok()`/`fail()`/`warn()`（`print`）、`supervisor`の`STATUS:`、
`upgrade`の自動更新メッセージ、`auth`のKeychain保存とOAuth URLなど。いずれも
利用者向けには正しい出力で、テストがそれを捕捉していないだけ。

実害が出た。`0815-cli-import-hardening`の作業中に発生した**唯一のテスト失敗が
この出力に埋もれ**、`grep -E "^(ERROR|FAIL): test"` で掘り出す必要があった。
`FAIL:`という文字列がgreen runに並ぶので、目視でもgrepでも当てにならない。

**2. `ResourceWarning` 2件**

```
Exception ignored while calling deallocator <function _TemporaryFileCloser.__del__ ...>
ResourceWarning: Implicitly cleaning up <HTTPError 401: 'Unauthorized'>
ResourceWarning: Implicitly cleaning up <HTTPError 404: 'Not Found'>
```

`tests/test_repository.py`のHTTPError fixtureが閉じられていない。

## 変更詳細

### 1. `unittest --buffer` を全経路へ

テスト側を書き換えるのではなく、runnerの標準機能を使う。`--buffer`は各テストの
stdout/stderrを捕捉し、**失敗・エラーになったテストの分だけ**`Stdout:`/`Stderr:`として
その結果に紐づけて表示する。

これを選ぶ理由:

- 出力する側（`doctor`等）は利用者向けに正しいので、production の出力先を変えたくない。
- テスト個別に`redirect_stdout`を足すのは、対象が多く、書き忘れが再発する。
- 失敗時の文脈は**失われるどころか改善する**。従来は他テストの出力と時系列で混ざっていたが、
  `--buffer`は落ちたテストの直下にまとめて出す。

実測で確認した挙動:

```
test_passing_test_output_is_hidden ... ok          ← 出力は出ない
test_failing_test_output_is_shown ... FAIL
  Stdout:
  FAILING-TEST-CONTEXT                             ← 落ちた分だけ出る
```

反映先は「これから実行される手順」だけにする。`task/003`〜`task/007`と`task/0814`の
コマンド記載は、その時点で実際に流した記録なので書き換えない。

- `.github/workflows/ci.yml`
- `.github/workflows/release.yml`
- `.github/PULL_REQUEST_TEMPLATE.md`
- `CONTRIBUTING.md`
- `README.md` / `README.en.md`

`-v`は残す。CIログでテスト名と結果を追えることに価値があり、`--buffer`と併用しても
ノイズは増えない。

### 2. `ResourceWarning` の根本原因

**当初の見立ては誤りだった。** `HTTPError(url, code, msg, hdrs, fp=None)` が内部で
`tempfile.TemporaryFile()` を開くのだと考え、`fp`へ`io.BytesIO(b"")`を渡す修正を当てたが、
警告は消えなかった。Python 3.14の`urllib/error.py`を読むと`fp is None`のときに作るのは
`io.BytesIO()`で、tempfileではない。

実際の原因は継承関係にある。

```
HTTPError → urllib.response.addinfourl → addbase → tempfile._TemporaryFileWrapper
```

`_TemporaryFileWrapper`は`_TemporaryFileCloser`を持ち、その`__del__`が
`close_called`でなければ`ResourceWarning`を出す。つまり**`fp`に何を渡すかとは無関係で、
closeすることでしか消えない**。

`RepositoryTests.http_error()` ヘルパーを足し、`self.addCleanup(error.close)` で必ず閉じる。

なお`-W error::ResourceWarning`をCIゲートにする案は採らない。この警告は`__del__`の中で
発生するため、errorへ昇格させてもPythonが「Exception ignored」として握り潰し、
**テストは通ってしまう**（実測: 修正前のコードに対して3回とも`OK`）。ゲートとして機能しない。

### 3. `CONTRIBUTING.md` に「テストを書くときの約束」を新設

`0815-cli-import-hardening`で踏んだ2つの型を、次の人が踏まないように明文化する。

- **`mock.patch`は使う場所へ当てる。** module level importされた名前は import 時に
  利用側へ束縛されるので、定義側moduleへのpatchは素通りして本物が動く。
  定義側へ当てて良いのは、そのmodule内の関数を直接呼ぶテストだけ。
- **`_locked`ヘルパーをまるごとmockしたテストは、そのコマンドを検証したことにならない。**
  実際にこれで`rollback`と`migrate`の`NameError`を出荷した。
- `--buffer`を外さない理由。
- `HTTPError` fixtureは閉じる。

## 触らないもの

- production コードの出力先は変えない（`doctor.py`等の`print`はそのまま）。
- 過去のtask記録に書かれたコマンド（実行時の記録なので改変しない）。
- `task/0814-oss-public-release-plan.md`の引き継ぎ節へのlintゲート追記は、
  その節を作るPR #17側で行う。このPRのbaseには当該節がまだ無いため。

## Verification

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v --buffer
PYTHONPATH=src python3 -m compileall -q src portable scripts
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py
python3 scripts/secret_scan.py --tree . --git-history
python3 scripts/build_release.py "v$(cat VERSION)" --dist /tmp/codex-openrouter-dist
ruff check .
PYTHONPATH=src python3 -W always::ResourceWarning -m unittest discover -s tests   # 0件であること
```

## Status

実施済み（2026-08-15）。

## 実行結果

| 項目 | 修正前 | 修正後 |
|---|---|---|
| unittest stdout | 92行 | **0行** |
| unittest stderr | 101行 | **5行**（unittest自身の結果表示のみ） |
| `ResourceWarning` | 2件 | **0件** |
| テスト件数 | 259 | 259（全PASS） |

`--buffer`が失敗時に出力を残すことは、意図的に落ちるテストを作って確認済み。
成功したテストの`print`は出ず、失敗したテストの`print`だけが`Stdout:`として
その結果の直下に出た。
