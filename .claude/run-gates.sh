#!/bin/bash
# Linuxで走らせられるgateを1回でまとめて実行する。ci.ymlのjobと対応させてある。
# 失敗は失敗のまま残す。retryもfallbackもしない。作業treeへは書き込まない。
# 対象fileが無いbranchではそのgateをSKIPする。
set -uo pipefail

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}" || exit 2
export PYTHONPATH=src

pass=0; fail=0; failed_names=()

gate() {
  local name="$1"; shift
  local out rc
  out=$("$@" 2>&1); rc=$?
  if [ "$rc" -eq 0 ]; then
    printf '  PASS  %s\n' "$name"; pass=$((pass + 1))
  else
    printf '  FAIL  %s (exit %d)\n' "$name" "$rc"
    fail=$((fail + 1)); failed_names+=("$name")
    printf '%s\n' "$out" | tail -20 | sed 's/^/        | /'
  fi
}

gate_if() {  # gate_if <path> <name> <cmd...>
  local path="$1"; shift
  if [ -e "$path" ]; then gate "$@"; else printf '  SKIP  %s (no %s)\n' "$1" "$path"; fi
}

# unit testの入口はbranchで違う。scripts/run_unit_tests.py があるbranchはそれを使う
# （外部通信を遮断するrunner）。無いbranchはci.ymlと同じ unittest discover を使う。
run_tests() {  # run_tests <python>
  if [ -f scripts/run_unit_tests.py ]; then
    "$1" scripts/run_unit_tests.py
  else
    "$1" -m unittest discover -s tests -v --buffer
  fi
}

echo "== unit tests + syntax: ci.yml python-compat matrix =="
for v in 3.11 3.12 3.13 3.14; do
  py=$(command -v "python$v") || { printf '  SKIP  python%s (not installed)\n' "$v"; continue; }
  gate "unit tests (py$v)" run_tests "$py"
  gate "compileall (py$v)" "$py" -m compileall -q src portable scripts
done

echo "== ci.yml lint / audit-release (archive build以外) =="
gate "ruff $(ruff --version 2>/dev/null | awk '{print $2}')" ruff check .
gate "secret scan (tree)"        python3 scripts/secret_scan.py --tree .
gate "secret scan (git history)" python3 scripts/secret_scan.py --tree . --git-history
gate_if scripts/check_upstreams.py \
  "upstream manifest" python3 scripts/check_upstreams.py --validate-only

echo "== ci.yml macos-compile のうちLinuxで走る分 =="
gate "synthetic E2E" python3 scripts/macos_synthetic_e2e.py
gate_if portable/templates/codex-openrouter-app.zsh.in \
  "zsh -n codex-openrouter-app.zsh.in" zsh -n portable/templates/codex-openrouter-app.zsh.in
gate_if portable/launcher/build_icon.zsh \
  "zsh -n build_icon.zsh" zsh -n portable/launcher/build_icon.zsh
gate_if scripts/macos_installed_e2e.zsh \
  "zsh -n macos_installed_e2e.zsh" zsh -n scripts/macos_installed_e2e.zsh

echo "== 作業tree =="
gate "git diff --check" git diff --check

echo
echo "Linuxでは走らない: xcrun swiftc launcher build / decoder compat / 実機E2E (ci.yml macos-14)。"
echo "ここでは実行しない: scripts/build_release.py (release相当)。"
echo
if [ "$fail" -eq 0 ]; then
  echo "ALL GATES PASS ($pass/$pass)"
  exit 0
fi
echo "GATES FAILED: $fail failed, $pass passed -> ${failed_names[*]}"
exit 1
