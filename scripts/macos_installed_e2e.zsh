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

run_cycle() {
  local cycle="$1"
  print -- "\n=== launcher cycle ${cycle}/2 ==="
  print -- "launcherを起動します。確認画面が出た場合は安全なモード切替を選択してください。"
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

run_cycle 1
run_cycle 2

print -- "\n=== installed launcher E2E PASS: 2/2 cycles ==="
