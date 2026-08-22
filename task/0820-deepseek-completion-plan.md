# DeepSeek Tool Use 仕上げ計画（PR #24 / #25 の完了まで）

作成日: 2026-08-20

対象branch: `codex/openrouter-tool-bridge`（PR #24, Draft） / `claude/openrouter-tool-bridge-patch-qyxvmd`（PR #25）

base: `origin/main` (`16bc42746719c4bd1fd52e22e4c847faa8249c65`)

前提文書: `task/0820-deepseek-tool-use-handoff.md`（Required Changes と Do Not は引き続き有効）

Status: **Phase 0 進行中**（各Phaseの結果は末尾のResultへ実測後に追記する）

## Context

handoff の Required Changes 1〜3 は Claude Cloud が `c1ddc00` で実装済み（362 tests、CI 11/11 green）。
PR #25 は Cloud session 用の SessionStart hook と gate runner を `.claude/` に追加する独立PR。

計画作成時（2026-08-20）に判明した、handoff 記載と異なる実機状態:

| 項目 | handoff | 実測 | 影響 |
| --- | --- | --- | --- |
| Keychain item | `missing` | **PRESENT** | 実機gateのblockerは解消済み |
| installed runtime | source と digest 一致 | `source_commit=34ab136` / digest `b653d6a…` | `c1ddc00` のsrc変更を含まない。E2E前に `upgrade` 必須 |
| supervisor.json | — | `saved_model=gpt-5.6-sol` / `pending_default_model=false` / `active=false` | Required Change 1 を実機で試す初期条件が揃っている |
| guard.log | — | 最新3件に `tool_request` なし | 旧runtimeでの実行痕跡 |

`.claude/` は `scripts/build_release.py` の allowlist (`FILES` / `DIRECTORIES`) に無く、配布アーカイブには入らない。

## 変更詳細

### Phase 0 — 環境と観測の確定（有料request 0 / GUI 0）

1. `.claude/run-gates.sh` を fail-closed 化（PR #25）
   - SKIP を pass/fail のどちらにも数えず最終行にも出していなかった。container から `uv` が消えて
     python matrix が 3.11 だけに縮んでも `ALL GATES PASS` と出る。SKIP 件数と名前を最終行に出し、
     `REQUIRE_FULL_MATRIX=1` では非0で終わる。
   - ruff を PATH 任せにしていた。hook の pin が失敗して別version が走っても PASS になる。
     ci.yml と同じ `0.16.3` 以外は gate を実行せず FAIL。
   - `.claude/` の script は CI のどの job にも入らない。hook と runner の `bash -n`、
     `settings.json` の parse を先頭の gate に置く。
2. `.github/workflows/ci.yml` に `zsh -n scripts/macos_installed_e2e.zsh` を追加（PR #24）
   - `7ea3a49`（zsh の read-only parameter `status` 衝突）はまさにこの file のバグで、
     `c1ddc00` で更に +72 行変更されているのに CI の構文 gate から漏れていた。
3. `tests/test_repository.py::test_ci_parses_every_zsh_entry_point`
   - file 名の列挙は黙って漏れるため、tracked な `*.zsh` / `*.zsh.in` が全て ci.yml の
     `zsh -n` 対象に入っていることを検査する。旧 ci.yml では実際に失敗することを確認済み。
4. PR #25 を merge。

### Phase 1 — 実機前の local 検証（有料request 0 / GUI 0）

- branch を `c1ddc00` へ同期し、handoff の Verification To Run を全て実行。
- macOS 専用 gate（`xcrun swiftc` の launcher build と decoder compat）も手元で実行。
- `codex-openrouter upgrade` で installed runtime を `c1ddc00` へ promote し、
  `doctor --runtime --secret-scan` PASS と source/installed digest 一致を再確認。
- ここが全 PASS でなければ実機へ進まない。

### Phase 2 — 実機 Run 1（GUI 5 prompts / 有料request 発生）

- 事前: 親shell の `OPENROUTER_API_KEY` を外す。ChatGPT と launcher を停止。新しい空 workspace を作る。
- `scripts/macos_installed_e2e.zsh <空workspace>`、reasoning effort `high` + Auto Exacto、provider pin なし。
- 併せて確認する実機証拠:
  - 専用起動で DeepSeek が自動選択される（Required Change 1、`pending_default_model=false` から）
  - guard log に `tool_request` / `duration_ms` / 整数 token 数が出て、許可 key 集合を超えない（Required Change 2）
- 合格条件: `installed launcher E2E PASS: lifecycle 2/2 / tool 5/5 / retry 0`
- 失敗時は same-run retry も fallback もせず、JSONL から原因を報告して停止する。
- tool-compat cache は 24h で失効する。失効後の run では canary 2 request が加算される。

### Phase 3 — 実機 Run 2（Run 1 が 5/5 のときだけ）

- 別の新しい空 workspace と fresh chat で同一手順。
- 完成判定: 累計 lifecycle 4/4 / tool 10/10 / retry 0。

### Phase 4 — 仕上げ

- `task/0820-deepseek-tool-use-handoff.md` の Status を更新し、実機2 run の結果を記録。
- PR #24 に実機E2E結果（有料request数・GUI prompt数・retry数・secret非漏洩）を追記 → un-draft → merge。
- 挙動が変わった箇所だけ README を確認（`c1ddc00` で反映済み）。

### Phase 5 — 任意・別ゲート

- release 判断。Tool Bridge は機能追加のため `0.3.0` 相当。VERSION / RELEASE_NOTES / tag は
  Phase 4 の merge 後に独立して判断する。

## Verification

Phase 1 で通すもの:

```bash
PYTHONPATH=src python3 scripts/run_unit_tests.py
PYTHONPATH=src python3 -m compileall -q src portable scripts
PYTHONPATH=src python3 scripts/macos_synthetic_e2e.py
uvx ruff@0.16.3 check .
python3 scripts/secret_scan.py --tree .
python3 scripts/check_upstreams.py --validate-only
zsh -n scripts/macos_installed_e2e.zsh
git diff --check
xcrun swiftc portable/launcher/app/*.swift -o /tmp/CodexOpenRouterLauncher
xcrun swiftc -parse-as-library portable/launcher/app/ProfileBridge.swift \
  portable/tests/DecoderCompatTests.swift -o /tmp/decoder-compat && /tmp/decoder-compat
```

Phase 2/3 の合格条件は handoff の Real-device Gate に従う。

## Do Not

`task/0820-deepseek-tool-use-handoff.md` の Do Not をそのまま引き継ぐ。特に、
Run 1 の承認前に有料request・GUI起動・Keychain変更を行わない。失敗時に same-run retry、
fallback、別launcher経路を使わない。provider pin と release/tag を勝手に進めない。

## Result

実測後にのみ記入する。未実施の行に予測を書かない。

| Phase | 結果 |
| --- | --- |
| 0 | 進行中 |
| 1 | 未着手 |
| 2 | 未着手 |
| 3 | 未着手 |
| 4 | 未着手 |
| 5 | 未着手（別ゲート） |
