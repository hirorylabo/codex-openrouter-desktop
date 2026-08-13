# Third-party notices

このプロジェクトは現在サードパーティのソースを vendor / 同梱していない。

v0.2.0 で ASAR パッチ方式を撤去した際に、以下への依存が無くなった。

- **Better Codex App Custom Provider Support**（The Unlicense、pinned commit `4e19e474330dc5266eb814e425410127aa7c1a4e`）— setup 時に hash 検証つきで取得していた upstream patcher
- **Acorn / acorn-walk / MagicString** — 未知 build 向け candidate transform が使っていた JavaScript parser

現在は純正 `/Applications/ChatGPT.app` を一切変更せず、`~/.codex/config.toml` の
marker block とローカル guard だけで OpenRouter モデルを扱うため、これらは不要になった。
撤去前の状態は archive tag `archive/asar-patch-003a0bc` に保全されている。

実行時に利用するのは macOS 標準のコマンド（`codesign`、`plutil`、`PlistBuddy`、`swiftc` 等）と
Python 標準ライブラリのみ。
