#!/bin/bash
# Claude Code on the web用のSessionStart hook。
# このrepoはstdlibだけで動くためpip installは無い。足りないのはtoolchainの方で、
# base imageには zsh が無く、PATH上の ruff はCIと別versionで、python は3.11しか
# 入っていない。ここを揃えないとgateがそのまま走らない。
# 冪等・非対話。remote session以外では何もしない。
set -euo pipefail

# ci.yml (lint job) と同じversion。ずれるとlint結果がCIと食い違う。
RUFF_PIN="0.16.3"
# ci.yml (python-compat matrix) と同じ。3.11はbase imageにある。
PY_EXTRA=(3.12 3.13 3.14)

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

log() { printf 'session-start: %s\n' "$1"; }

# 1. zsh: `zsh -n` の構文gateが使う。base imageには入っていない。
if ! command -v zsh >/dev/null 2>&1; then
  log "installing zsh"
  if command -v sudo >/dev/null 2>&1; then
    sudo apt-get install -y -qq zsh >/dev/null 2>&1 || true
  else
    apt-get install -y -qq zsh >/dev/null 2>&1 || true
  fi
fi
command -v zsh >/dev/null 2>&1 && log "zsh $(zsh --version | awk '{print $2}')" || log "WARN zsh unavailable"

# 2. ruff: base imageのruffはCIと別version。素の `ruff check` がCIと違う結果を
#    出すのを防ぐため、PATH上のruffごとpinへ揃える。
if command -v uv >/dev/null 2>&1; then
  if [ "$(ruff --version 2>/dev/null | awk '{print $2}')" != "$RUFF_PIN" ]; then
    log "pinning ruff to $RUFF_PIN"
    uv tool install "ruff==$RUFF_PIN" --force >/dev/null 2>&1 || true
  fi
  # `uvx ruff@x` を使う手順書のためにcacheも温めておく。
  uvx "ruff@$RUFF_PIN" --version >/dev/null 2>&1 || true
fi
log "ruff $(ruff --version 2>/dev/null | awk '{print $2}')"

# 3. python matrix: 3.12以降でしか出ない差分をpush前に見つけられるようにする。
if command -v uv >/dev/null 2>&1; then
  missing=()
  for v in "${PY_EXTRA[@]}"; do
    command -v "python$v" >/dev/null 2>&1 || missing+=("$v")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    log "installing python ${missing[*]}"
    uv python install "${missing[@]}" >/dev/null 2>&1 || true
  fi
fi
log "python $(python3 -V 2>&1 | awk '{print $2}') + $(for v in "${PY_EXTRA[@]}"; do command -v "python$v" >/dev/null 2>&1 && printf '%s ' "$v"; done)"

# 4. このrepoのcommandは全て PYTHONPATH=src 前提。毎回書かずに済むようにする。
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo 'export PYTHONPATH=src' >> "$CLAUDE_ENV_FILE"
  log "exported PYTHONPATH=src"
fi

# xcrun / swiftc はmacOS SDK専用。launcher build、decoder compat、実機E2Eは
# ci.ymlのmacos-14 jobとMac担当に残る。ここでは代替しない。
log "ready (macOS-only gates stay on the Mac side)"
