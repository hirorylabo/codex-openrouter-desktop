#!/bin/zsh
# 導入済みlauncherを2 cycle実行する手動macOS E2E。
# ChatGPTの終了は利用者が通常のUIから行う。強制終了・秘密値表示はしない。

set -eu
setopt pipefail

readonly STATE_DIR="$HOME/.local/share/codex-openrouter-desktop/state"
readonly SUPPORT_ROOT="$HOME/.local/share/codex-openrouter-desktop/current"
readonly STATE_FILE="$STATE_DIR/supervisor.json"
readonly TOKEN_FILE="$STATE_DIR/guard-token"
readonly CLI="$HOME/.local/bin/codex-openrouter"
readonly SCRIPT_DIR="${0:A:h}"
readonly AUDITOR="$SCRIPT_DIR/macos_installed_e2e_audit.py"
readonly SESSIONS_ROOT="$HOME/.codex/sessions"
readonly LAUNCHER="$HOME/Desktop/Codex OpenRouter.app"
readonly LAUNCHER_BUNDLE_ID="local.codex.openrouter.launcher"
readonly LAUNCHER_EXECUTABLE="$LAUNCHER/Contents/MacOS/CodexOpenRouterLauncher"
readonly STOCK_EXECUTABLE="/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"
readonly KEYCHAIN_SERVICE="io.github.hirorylabo.codex-openrouter-desktop"
readonly WAIT_SECONDS="${CODEX_OPENROUTER_E2E_TIMEOUT:-180}"
readonly E2E_WORKSPACE="${1:-}"
typeset -g E2E_PROBE_CONTENT=""

fail() {
  print -u2 -- "[FAIL] $*"
  exit 1
}

state_field() {
  /usr/bin/plutil -extract "$1" raw -o - "$STATE_FILE" 2>/dev/null || print -- ""
}

stock_running() {
  [[ -n "$(process_pids "$STOCK_EXECUTABLE")" ]]
}

launcher_running() {
  [[ -n "$(process_pids "$LAUNCHER_EXECUTABLE")" ]]
}

process_pids() {
  PYTHONPATH="$SUPPORT_ROOT/src" python3 -m codex_openrouter.processes \
    --executable "$1"
}

doctor() {
  "$CLI" doctor --runtime --secret-scan
}

wait_active() {
  local deadline=$((SECONDS + WAIT_SECONDS))
  local active port mode
  while (( SECONDS < deadline )); do
    active="$(state_field active)"
    port="$(state_field guard_port)"
    mode=""
    [[ ! -f "$TOKEN_FILE" ]] || mode="$(/usr/bin/stat -f '%Lp' "$TOKEN_FILE")"
    if [[ "$active" == "true" && "$port" == <-> && "$port" -gt 0 &&
          "$mode" == "600" ]] && stock_running; then
      return 0
    fi
    /bin/sleep 1
  done
  fail "active状態を${WAIT_SECONDS}秒以内に確認できませんでした"
}

wait_inactive() {
  local deadline=$((SECONDS + WAIT_SECONDS))
  local active
  while (( SECONDS < deadline )); do
    active="$(state_field active)"
    if [[ "$active" == "false" && ! -e "$TOKEN_FILE" ]] && ! stock_running; then
      return 0
    fi
    /bin/sleep 1
  done
  fail "inactive cleanupを${WAIT_SECONDS}秒以内に確認できませんでした"
}

wait_launcher_exit() {
  local deadline=$((SECONDS + WAIT_SECONDS))
  while (( SECONDS < deadline )); do
    launcher_running || return 0
    /bin/sleep 1
  done
  fail "Desktop launcherの終了を${WAIT_SECONDS}秒以内に確認できませんでした"
}

check_profile_surface() {
  print -- "\n=== 設定画面が読む profile document ==="
  local document
  document="$("$CLI" profile show --json)" || fail "profile show --jsonに失敗しました"
  print -r -- "$document" | /usr/bin/python3 -c '
import json, sys
document = json.load(sys.stdin)
assert document["schema_version"] == 1, document["schema_version"]
assert document["profile"]["models"], "選択モデルが空です"
assert document["profile"]["default_model"] in document["profile"]["models"]
assert document["available"], "registryが空です"
' || fail "profile documentが契約を満たしません"
  # 設定画面には秘密値を出さない。出所であるdocumentの時点で持たせない。
  case "$document" in
    *sk-or-*) fail "profile documentに鍵が含まれています" ;;
  esac
  print -- "[PASS] profile show: schema・選択・registryを確認（秘密値なし）"
}

check_catalog_surface() {
  print -- "\n=== 設定画面が読む候補一覧 ==="
  local document
  document="$("$CLI" models list --json)" || fail "models list --jsonに失敗しました"
  print -r -- "$document" | /usr/bin/python3 -c '
import json, sys
document = json.load(sys.stdin)
assert document["schema_version"] == 1, document["schema_version"]
rows = document["models"]
assert rows, "候補が空です"
for row in rows:
    assert row["id"] and row["display_name"], row
    assert row["headline"]["input"] and row["headline"]["output"], row["id"]
    assert isinstance(row["zdr_supported"], bool), row["id"]
    # 学習ポリシーは「不明」を許すが、ZDRなら必ず学習しないと言えること。
    if row["zdr_supported"]:
        assert row["trains_on_data"] is False, row["id"]
    assert row.get("tool_support") in {
        "verified", "partial", "declared", "unknown", "unsupported"
    }, row["id"]
zdr = sum(1 for row in rows if row["zdr_supported"])
usage = document["usage_available"]
print(f"  候補 {len(rows)}件 / ZDR {zdr}件 / 利用量あり {usage}")
' || fail "catalog documentが契約を満たしません"
  case "$document" in
    *sk-or-*) fail "catalog documentに鍵が含まれています" ;;
  esac
  print -- "[PASS] models list: 候補・価格・ZDR判定を確認（秘密値なし）"
}

manual_checklist() {
  print -- "\n=== 実機で目視確認する項目 ==="
  print -- "  1. 管理画面に 表示モデル数・既定モデル・workspace が出る"
  print -- "  2. ⌘, とAppメニューの「設定…」でモデル設定画面が開く"
  print -- "  3. Desktop launcherへfolderをdropするとworkspaceだけ変わり、ChatGPTは起動しない"
  print -- "  4. 純正ChatGPT起動中にクリックすると切替確認が出る"
  print -- "  5. 設定画面のどこにもAPI keyが表示されない"
  print -- "  6. 終了後に codex-openrouter doctor が純正app無改変を報告する"
  print -- "  7. 一覧が価格・公開日・7dトークン量・Codex tool状態つきで出る"
  print -- "  8. 「ZDRのみ」が既定でONで、外すと候補が増える"
  print -- "  9. ZDRなしのモデルを選ぶと確認シートが出る（やめる=選択されない）"
  print -- " 10. 追加後、管理画面に「ZDRなしのモデルを N件使用中です」が橙色で出る"
  print -- " 11. 追加したモデルが純正pickerに出て、実際に応答する"
  print -- " 12. 検証済みモデルではproviderと検証時刻が表示される"
  print -- " 13. tool非対応は既定で隠れ、件数表示と明示承認で選択できる"
}

open_launcher() {
  if [[ -n "$E2E_WORKSPACE" ]]; then
    /usr/bin/open -b "$LAUNCHER_BUNDLE_ID" "$E2E_WORKSPACE" \
      || fail "exact bundle IDでlauncherを起動できませんでした（自動retry・別経路fallback禁止）"
  else
    /usr/bin/open "$LAUNCHER" || fail "Desktop launcherを起動できませんでした"
  fi
}

require_empty_workspace() {
  /usr/bin/python3 - "$E2E_WORKSPACE" <<'PY' \
    || fail "workspaceが空ではありません。内容を変更せず停止します: $E2E_WORKSPACE"
from pathlib import Path
import sys

raise SystemExit(any(Path(sys.argv[1]).iterdir()))
PY
}

wait_gate() {
  local gate="$1"
  local marker="$2"
  local expected_content="${3:-}"
  local deadline=$((SECONDS + WAIT_SECONDS))
  local output audit_status
  local -a arguments
  arguments=(
    --sessions-root "$SESSIONS_ROOT"
    --started-after "$4"
    --workspace "$E2E_WORKSPACE"
    --marker "$marker"
    --gate "$gate"
  )
  [[ -z "$expected_content" ]] || arguments+=(--expected-content "$expected_content")
  while (( SECONDS < deadline )); do
    stock_running || fail "$gate gate完了前にChatGPT.appが終了しました（cycle 2へ進みません）"
    if output="$(/usr/bin/python3 "$AUDITOR" "${arguments[@]}" 2>&1)"; then
      print -- "[PASS] $gate gate: $output"
      return 0
    else
      audit_status=$?
      (( audit_status == 2 )) || fail "$gate gate: $output（自動retry・fallbackなし、cycle 2へ進みません）"
    fi
    /bin/sleep 1
  done
  fail "$gate gateを${WAIT_SECONDS}秒以内に確認できませんでした（cycle 2へ進みません）"
}

run_tool_gates() {
  local started_after="$1"
  local run_tag="$(/bin/date +%s)_${$}_${RANDOM}"
  local pwd_marker="OR_E2E_PWD_C1_${run_tag}"
  local patch_marker="OR_E2E_PATCH_C1_${run_tag}"
  local namespace_marker="OR_E2E_NAMESPACE_C1_${run_tag}"
  E2E_PROBE_CONTENT="OR_TOOLBRIDGE_E2E_${run_tag}"

  require_empty_workspace
  print -- "\n=== cycle 1 tool gate 1/3: exact cwd ==="
  print -- "新しく開いた空のcomposerを使ってください。sidebarの既存taskは開かないでください。"
  print -- "次の1行を一度だけ送信し、完了までChatGPT.appを終了しないでください。"
  print -r -- "$pwd_marker。functions.exec_commandをちょうど1回だけ使い、cmdがpwdだけの空白なしcommandを実行してください。結果はその1行だけ返し、ほかのtool・retry・fallbackは禁止です。"
  wait_gate pwd "$pwd_marker" "" "$started_after"

  require_empty_workspace
  print -- "\n=== cycle 1 tool gate 2/3: apply_patch ==="
  print -- "次の1行を一度だけ送信してください。readbackはこのharnessが行います。"
  print -r -- "$patch_marker。functions.apply_patchをちょうど1回だけ使い、$E2E_WORKSPACE/toolbridge-e2e.txt を新規作成して $E2E_PROBE_CONTENT の1行だけを書いてください。ほかのtool・retry・fallback・toolでのreadbackは禁止です。"
  wait_gate apply-patch "$patch_marker" "$E2E_PROBE_CONTENT" "$started_after"

  print -- "\n=== cycle 1 tool gate 3/3: namespace child ==="
  print -- "次の1行を一度だけ送信してください。browser・search・Node REPLへの代替は禁止です。"
  print -r -- "$namespace_marker。functions.list_mcp_resourcesを空object引数でちょうど1回だけ使い、resources件数だけ返してください。ほかのtool・retry・fallbackは禁止です。"
  wait_gate namespace "$namespace_marker" "" "$started_after"
}

cleanup_probe() {
  [[ -n "$E2E_WORKSPACE" && -n "$E2E_PROBE_CONTENT" ]] || return 0
  local probe="$E2E_WORKSPACE/toolbridge-e2e.txt"
  /usr/bin/python3 - "$E2E_WORKSPACE" "$probe" "$E2E_PROBE_CONTENT" <<'PY' \
    || fail "cleanup前のmarker fileがexact一致しません。削除せず停止します"
from pathlib import Path
import sys

workspace = Path(sys.argv[1])
probe = Path(sys.argv[2])
expected = (sys.argv[3] + "\n").encode()
raise SystemExit(
    probe.is_symlink()
    or not probe.is_file()
    or probe.read_bytes() != expected
    or list(workspace.iterdir()) != [probe]
)
PY
  /bin/rm "$probe" || fail "検証済みE2E marker fileを削除できませんでした"
  require_empty_workspace
  print -- "[PASS] cleanup: 検証済みmarker fileだけを削除し、workspaceは空です"
}

check_keychain_status() {
  /usr/bin/security find-generic-password -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1 \
    || fail "Keychain itemをstatusだけで確認できませんでした"
  print -- "[PASS] Keychain: itemあり（値は取得・表示していません）"
}

run_cycle() {
  local cycle="$1"
  local started_after="$(/bin/date +%s)"
  print -- "\n=== launcher cycle ${cycle}/2 ==="
  print -- "launcherを起動します。管理画面の「OpenRouterで起動」を押してください。"
  print -- "純正ChatGPTが起動中の場合は、確認画面で安全なモード切替を選択してください。"
  open_launcher
  wait_active
  doctor || fail "cycle ${cycle}: active doctorに失敗しました"
  print -- "[PASS] cycle ${cycle}: active（ephemeral port・0600 token・guard）"
  if [[ "$cycle" == "1" && -n "$E2E_WORKSPACE" ]]; then
    run_tool_gates "$started_after"
  fi
  print -- "ChatGPT.appを通常のメニューまたは⌘Qで終了してください。"
  wait_inactive
  wait_launcher_exit
  doctor || fail "cycle ${cycle}: inactive doctorに失敗しました"
  print -- "[PASS] cycle ${cycle}: inactive（port 0 stub・tokenなし）"
}

(( $# <= 1 )) || fail "usage: scripts/macos_installed_e2e.zsh [EMPTY_WORKSPACE]"
[[ "$HOME" == /Users/* && "$HOME" != "/Users" ]] || fail "安全なmacOS homeを解決できません"
[[ -x "$CLI" ]] || fail "導入済みCLIがありません: $CLI"
[[ -d "$SUPPORT_ROOT/src/codex_openrouter" ]] || fail "導入済みruntimeがありません"
[[ -d "$LAUNCHER" ]] || fail "Desktop launcherがありません: $LAUNCHER"
[[ -x "$LAUNCHER_EXECUTABLE" ]] || fail "Desktop launcher executableがありません"
[[ -x "$STOCK_EXECUTABLE" ]] || fail "純正ChatGPT executableがありません"
[[ "$WAIT_SECONDS" == <-> && "$WAIT_SECONDS" -gt 0 ]] || fail "timeoutは正の整数にしてください"
if [[ -n "$E2E_WORKSPACE" ]]; then
  [[ "$E2E_WORKSPACE" == /* ]] || fail "workspaceは絶対pathで指定してください"
  [[ -d "$E2E_WORKSPACE" ]] || fail "workspace directoryがありません: $E2E_WORKSPACE"
  [[ -f "$AUDITOR" ]] || fail "JSONL auditorがありません: $AUDITOR"
  [[ -d "$SESSIONS_ROOT" ]] || fail "Codex sessions directoryがありません: $SESSIONS_ROOT"
  require_empty_workspace
fi

check_profile_surface
check_catalog_surface
manual_checklist
run_cycle 1
cleanup_probe
run_cycle 2
check_keychain_status
[[ -z "$E2E_WORKSPACE" ]] || require_empty_workspace

if [[ -n "$E2E_WORKSPACE" ]]; then
  print -- "\n=== installed launcher E2E PASS: lifecycle 2/2 / tool 3/3 / retry 0 ==="
else
  print -- "\n=== installed launcher E2E PASS: lifecycle 2/2 ==="
fi
