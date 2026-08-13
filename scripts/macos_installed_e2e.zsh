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
readonly LAUNCHER="$HOME/Desktop/Codex OpenRouter.app"
readonly LAUNCHER_EXECUTABLE="$LAUNCHER/Contents/MacOS/CodexOpenRouterLauncher"
readonly STOCK_EXECUTABLE="/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"
readonly WAIT_SECONDS="${CODEX_OPENROUTER_E2E_TIMEOUT:-180}"

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
  print -- "  7. 一覧が価格・公開日・7dトークン量つきで出て、並べ替えが効く"
  print -- "  8. 「ZDRのみ」が既定でONで、外すと候補が増える"
  print -- "  9. ZDRなしのモデルを選ぶと確認シートが出る（やめる=選択されない）"
  print -- " 10. 追加後、管理画面に「ZDRなしのモデルを N件使用中です」が橙色で出る"
  print -- " 11. 追加したモデルが純正pickerに出て、実際に応答する"
}

run_cycle() {
  local cycle="$1"
  print -- "\n=== launcher cycle ${cycle}/2 ==="
  print -- "launcherを起動します。管理画面の「OpenRouterで起動」を押してください。"
  print -- "純正ChatGPTが起動中の場合は、確認画面で安全なモード切替を選択してください。"
  /usr/bin/open "$LAUNCHER"
  wait_active
  doctor || fail "cycle ${cycle}: active doctorに失敗しました"
  print -- "[PASS] cycle ${cycle}: active（ephemeral port・0600 token・guard）"
  print -- "ChatGPT.appを通常のメニューまたは⌘Qで終了してください。"
  wait_inactive
  wait_launcher_exit
  doctor || fail "cycle ${cycle}: inactive doctorに失敗しました"
  print -- "[PASS] cycle ${cycle}: inactive（port 0 stub・tokenなし）"
}

[[ "$HOME" == /Users/* && "$HOME" != "/Users" ]] || fail "安全なmacOS homeを解決できません"
[[ -x "$CLI" ]] || fail "導入済みCLIがありません: $CLI"
[[ -d "$SUPPORT_ROOT/src/codex_openrouter" ]] || fail "導入済みruntimeがありません"
[[ -d "$LAUNCHER" ]] || fail "Desktop launcherがありません: $LAUNCHER"
[[ -x "$LAUNCHER_EXECUTABLE" ]] || fail "Desktop launcher executableがありません"
[[ -x "$STOCK_EXECUTABLE" ]] || fail "純正ChatGPT executableがありません"
[[ "$WAIT_SECONDS" == <-> && "$WAIT_SECONDS" -gt 0 ]] || fail "timeoutは正の整数にしてください"

check_profile_surface
check_catalog_surface
manual_checklist
run_cycle 1
run_cycle 2

print -- "\n=== installed launcher E2E PASS: 2/2 cycles ==="
