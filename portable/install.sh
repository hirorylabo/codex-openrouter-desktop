#!/bin/zsh
set -eu
setopt pipefail

readonly SCRIPT_DIR="${0:A:h}"
readonly REPO_ROOT="${SCRIPT_DIR:h}"
readonly MANIFEST="$REPO_ROOT/portable/manifest.json"

fail() {
  print -u2 -- "codex-openrouter installer: $*"
  exit 1
}

usage() {
  print -- "Usage: $0 --check | --install [--workspace /absolute/path] [--profile default|FILE]"
}

mode=""
workspace=""
profile_argument="default"
while (( $# > 0 )); do
  case "$1" in
    --check|--install)
      [[ -z "$mode" ]] || fail "--check と --install は同時に指定できません"
      mode="$1"
      ;;
    --workspace)
      shift
      (( $# > 0 )) || fail "--workspaceにはpathが必要です"
      workspace="$1"
      ;;
    --profile)
      shift
      (( $# > 0 )) || fail "--profileにはdefaultまたはJSON pathが必要です"
      profile_argument="$1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) fail "未知の引数: $1" ;;
  esac
  shift
done
[[ -n "$mode" ]] || { usage; exit 2; }

readonly USER_NAME="$(/usr/bin/id -un)"
readonly USER_HOME="$(/usr/bin/id -P "$USER_NAME" | /usr/bin/awk -F: '{print $9}')"
[[ "$USER_HOME" == /Users/* && "$USER_HOME" != /Users ]] || fail "安全なmacOS homeを解決できません"
readonly STOCK_APP="/Applications/ChatGPT.app"
readonly OPENROUTER_APP="$USER_HOME/Applications/ChatGPT OpenRouter.app"
readonly OPENROUTER_HOME="$USER_HOME/.codex-openrouter"
readonly BIN_DIR="$USER_HOME/.local/bin"
readonly SUPPORT_ROOT="$USER_HOME/.local/share/codex-openrouter-desktop/current"
readonly DESKTOP_APP="$USER_HOME/Desktop/Codex OpenRouter.app"
readonly REGISTRY="$REPO_ROOT/models/registry.json"
readonly ADAPTER_INDEX="$REPO_ROOT/adapters/index.json"
readonly STOCK_ASAR="$STOCK_APP/Contents/Resources/app.asar"

if [[ "$profile_argument" == default ]]; then
  profile="$REPO_ROOT/profiles/default.json"
else
  profile="${profile_argument:A}"
fi
[[ -f "$profile" && ! -L "$profile" ]] || fail "profileが見つからないかsymlinkです: $profile"
if [[ -z "$workspace" ]]; then
  workspace="$USER_HOME/Documents"
fi
workspace="${workspace:A}"
[[ -d "$workspace" ]] || fail "workspaceは既存directoryを指定してください: $workspace"

command_path() { command -v "$1" 2>/dev/null || true; }
readonly PYTHON="$(command_path python3)"
[[ -n "$PYTHON" ]] || fail "Python 3が見つかりません"
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || \
  fail "Python 3.11以上が必要です"
upstream_fields="$($PYTHON - "$MANIFEST" <<'PY'
import json, re, sys
d=json.load(open(sys.argv[1], encoding="utf-8"))["upstream_patcher"]
values=[d.get(k) for k in ("commit","source_sha256","license_sha256","source_url","license_url")]
if not all(isinstance(v,str) and v for v in values): raise SystemExit(1)
if not re.fullmatch(r"[0-9a-f]{40}", values[0]): raise SystemExit(1)
if not all(re.fullmatch(r"[0-9a-f]{64}", v) for v in values[1:3]): raise SystemExit(1)
if not all(v.startswith("https://") for v in values[3:]): raise SystemExit(1)
print("\t".join(values))
PY
)" || fail "portable manifestのupstream contractが不正です"
IFS=$'\t' read -r UPSTREAM_COMMIT UPSTREAM_SHA256 UPSTREAM_LICENSE_SHA256 \
  UPSTREAM_SOURCE_URL UPSTREAM_LICENSE_URL <<< "$upstream_fields"
readonly PATCH_ROOT="$USER_HOME/.local/share/codex-openrouter-patcher/$UPSTREAM_COMMIT"
[[ "$(/usr/bin/uname -m)" == arm64 ]] || fail "Apple Silicon macOS専用です"
[[ -n "$(command_path npx)" ]] || fail "Node.jsのnpxが必要です"
/usr/bin/xcrun --find swiftc >/dev/null 2>&1 || fail "Xcode Command Line Toolsが必要です"
[[ -d "$STOCK_APP" && -f "$STOCK_ASAR" ]] || fail "公式ChatGPT.appが見つかりません"
/usr/bin/codesign --verify --deep --strict "$STOCK_APP" >/dev/null 2>&1 || \
  fail "公式ChatGPT.appの署名検証に失敗しました。純正appには書き込まず停止します"

bundle_value() {
  /usr/libexec/PlistBuddy -c "Print :$2" "$1/Contents/Info.plist" 2>/dev/null
}
file_sha256() {
  /usr/bin/shasum -a 256 "$1" | /usr/bin/awk '{print $1}'
}

readonly DETECTED_VERSION="$(bundle_value "$STOCK_APP" CFBundleShortVersionString)"
readonly DETECTED_BUILD="$(bundle_value "$STOCK_APP" CFBundleVersion)"
readonly DETECTED_STOCK_ASAR="$(file_sha256 "$STOCK_ASAR")"

adapter_fields="$(PYTHONPATH="$REPO_ROOT/src" "$PYTHON" - "$ADAPTER_INDEX" "$DETECTED_VERSION" "$DETECTED_BUILD" "$DETECTED_STOCK_ASAR" "$REPO_ROOT" <<'PY'
import json, sys
from pathlib import Path
doc=json.load(open(sys.argv[1], encoding="utf-8"))
matches=[a for a in doc.get("adapters",[]) if a.get("chatgpt_version")==sys.argv[2] and str(a.get("chatgpt_build"))==sys.argv[3] and a.get("stock_asar_sha256")==sys.argv[4]]
if len(matches)!=1: raise SystemExit(1)
a=matches[0]
patcher=(Path(sys.argv[5]) / str(a.get("patcher", ""))).resolve()
if not patcher.is_relative_to(Path(sys.argv[5]).resolve()) or not patcher.is_file(): raise SystemExit(1)
print("\t".join(str(a[k]) for k in ("id","patched_asar_sha256","marker","patcher")))
PY
)" || fail "検証済みadapterがありません。codex-openrouter updateでcandidateを作成してください"
IFS=$'\t' read -r ADAPTER_ID EXPECTED_PATCHED_ASAR PATCH_MARKER PATCHER_RELATIVE <<< "$adapter_fields"

PYTHONPATH="$REPO_ROOT/src" "$PYTHON" - "$REGISTRY" "$profile" <<'PY'
import sys
from pathlib import Path
from codex_openrouter.profile import resolve_profile
p=resolve_profile(Path(sys.argv[1]), Path(sys.argv[2]))
print(f"profile={p.name} models={len(p.models)} default={p.default_model}")
PY
print -- "architecture=arm64"
print -- "ChatGPT=$DETECTED_VERSION build $DETECTED_BUILD"
print -- "stock_asar=$DETECTED_STOCK_ASAR"
print -- "adapter=$ADAPTER_ID"
print -- "profile=$profile"
print -- "workspace=$workspace"

[[ "$mode" == --install ]] || {
  print -- "CHECK: PASS (no files changed)"
  exit 0
}

targets=(
  "$OPENROUTER_APP"
  "$OPENROUTER_HOME"
  "$DESKTOP_APP"
  "$SUPPORT_ROOT"
  "$BIN_DIR/codex-openrouter"
  "$BIN_DIR/codex-openrouter-credential"
  "$BIN_DIR/codex-openrouter-refresh"
  "$BIN_DIR/codex-openrouter-doctor"
  "$BIN_DIR/codex-openrouter-app"
  "$BIN_DIR/codex-openrouter-rebuild"
  "$PATCH_ROOT"
)
for target in "${targets[@]}"; do
  [[ ! -e "$target" && ! -L "$target" ]] || fail "fresh setupは既存targetを上書きしません: $target"
done

# All checks below happen in /tmp before any application or persistent config write.
readonly TEMP_DIR="$(/usr/bin/mktemp -d /tmp/codex-openrouter-install.XXXXXX)"
cleanup() { /bin/rm -rf "$TEMP_DIR"; }
trap cleanup EXIT INT TERM
/usr/bin/xcrun swiftc "$SCRIPT_DIR/credential/CredentialHelper.swift" -o "$TEMP_DIR/codex-openrouter-credential"
"$TEMP_DIR/codex-openrouter-credential" status || \
  fail "KeychainにOpenRouter API keyがありません。先にcodex-openrouter auth loginを実行してください"
"$PYTHON" "$SCRIPT_DIR/preflight_openrouter.py" \
  --registry "$REGISTRY" --profile "$profile" \
  --credential-helper "$TEMP_DIR/codex-openrouter-credential"
/usr/bin/curl -fsSL "$UPSTREAM_SOURCE_URL" -o "$TEMP_DIR/patch_chatgpt_providers.py"
[[ "$(file_sha256 "$TEMP_DIR/patch_chatgpt_providers.py")" == "$UPSTREAM_SHA256" ]] || fail "pinned upstream source hash mismatch"
/usr/bin/curl -fsSL "$UPSTREAM_LICENSE_URL" -o "$TEMP_DIR/UPSTREAM-LICENSE"
[[ "$(file_sha256 "$TEMP_DIR/UPSTREAM-LICENSE")" == "$UPSTREAM_LICENSE_SHA256" ]] || fail "pinned upstream Unlicense hash mismatch"

/bin/mkdir -p "$USER_HOME/Applications" "$BIN_DIR" "${SUPPORT_ROOT:h}" "$PATCH_ROOT" \
  "$OPENROUTER_HOME/model-catalogs" "$OPENROUTER_HOME/logs"
/bin/chmod 700 "$OPENROUTER_HOME" "$OPENROUTER_HOME/logs"
/bin/mkdir -p "$SUPPORT_ROOT"
for directory in src models profiles adapters portable; do
  /usr/bin/ditto "$REPO_ROOT/$directory" "$SUPPORT_ROOT/$directory"
done
/usr/bin/install -m 755 "$REPO_ROOT/codex-openrouter" "$SUPPORT_ROOT/codex-openrouter"
/usr/bin/install -m 644 "$REPO_ROOT/VERSION" "$SUPPORT_ROOT/VERSION"
/usr/bin/install -m 755 "$TEMP_DIR/codex-openrouter-credential" "$BIN_DIR/codex-openrouter-credential"
/usr/bin/install -m 755 "$SUPPORT_ROOT/codex-openrouter" "$BIN_DIR/codex-openrouter"

PYTHONPATH="$SUPPORT_ROOT/src" "$PYTHON" "$SUPPORT_ROOT/portable/render_runtime.py" \
  --registry "$SUPPORT_ROOT/models/registry.json" \
  --profile "$profile" \
  --template "$SUPPORT_ROOT/portable/templates/config.toml.in" \
  --output-home "$OPENROUTER_HOME" \
  --credential-helper "$BIN_DIR/codex-openrouter-credential"
"$PYTHON" - "$ADAPTER_INDEX" "$ADAPTER_ID" "$OPENROUTER_HOME/adapter.json" <<'PY'
import json, os, sys, tempfile
from pathlib import Path
doc=json.load(open(sys.argv[1], encoding="utf-8"))
adapter=next(a for a in doc["adapters"] if a["id"]==sys.argv[2])
target=Path(sys.argv[3]); fd,name=tempfile.mkstemp(prefix=".adapter.",dir=target.parent)
with os.fdopen(fd,"w",encoding="utf-8") as f: json.dump(adapter,f,indent=2); f.write("\n"); f.flush(); os.fsync(f.fileno())
os.chmod(name,0o600); os.replace(name,target)
PY

/usr/bin/install -m 644 "$TEMP_DIR/patch_chatgpt_providers.py" "$PATCH_ROOT/patch_chatgpt_providers.py"
/usr/bin/install -m 644 "$TEMP_DIR/UPSTREAM-LICENSE" "$PATCH_ROOT/LICENSE"

render_template() {
  local source="$1" target="$2" target_mode="$3" rendered="$TEMP_DIR/${target:t}"
  /usr/bin/sed -e "s|@@USER_HOME@@|$USER_HOME|g" -e "s|@@PYTHON@@|$PYTHON|g" "$source" > "$rendered"
  /usr/bin/install -m "$target_mode" "$rendered" "$target"
}
render_template "$SCRIPT_DIR/templates/codex-openrouter-refresh.py.in" "$BIN_DIR/codex-openrouter-refresh" 755
render_template "$SCRIPT_DIR/templates/codex-openrouter-doctor.py.in" "$BIN_DIR/codex-openrouter-doctor" 755
render_template "$SCRIPT_DIR/templates/codex-openrouter-app.zsh.in" "$BIN_DIR/codex-openrouter-app" 755
render_template "$SCRIPT_DIR/templates/codex-openrouter-rebuild.zsh.in" "$BIN_DIR/codex-openrouter-rebuild" 755

"$BIN_DIR/codex-openrouter-rebuild"
"$BIN_DIR/codex-openrouter-refresh" --init

readonly LAUNCHER_BUILD="$TEMP_DIR/Codex OpenRouter.app"
/bin/mkdir -p "$LAUNCHER_BUILD/Contents/MacOS" "$LAUNCHER_BUILD/Contents/Resources"
/usr/bin/install -m 644 "$SCRIPT_DIR/launcher/Info.plist" "$LAUNCHER_BUILD/Contents/Info.plist"
launcher_version="$(/bin/cat "$REPO_ROOT/VERSION")"
/usr/bin/plutil -replace CFBundleShortVersionString -string "$launcher_version" "$LAUNCHER_BUILD/Contents/Info.plist"
/usr/bin/plutil -replace CFBundleVersion -string "$launcher_version" "$LAUNCHER_BUILD/Contents/Info.plist"
"$SCRIPT_DIR/launcher/build_icon.zsh" \
  "$SCRIPT_DIR/launcher/CreateLauncherIcon.swift" "$LAUNCHER_BUILD/Contents/Resources/AppIcon.icns"
/usr/bin/plutil -replace CodexDefaultWorkspace -string "$workspace" "$LAUNCHER_BUILD/Contents/Info.plist"
/usr/bin/xcrun swiftc "$SCRIPT_DIR/launcher/CodexOpenRouterLauncher.swift" -o "$LAUNCHER_BUILD/Contents/MacOS/CodexOpenRouterLauncher"
/usr/bin/codesign --force --sign - "$LAUNCHER_BUILD" >/dev/null
/usr/bin/codesign --verify --deep --strict "$LAUNCHER_BUILD"
/usr/bin/ditto "$LAUNCHER_BUILD" "$DESKTOP_APP"

source_commit="release-archive"
if /usr/bin/git -C "$REPO_ROOT" rev-parse --verify HEAD >/dev/null 2>&1; then
  source_commit="$(/usr/bin/git -C "$REPO_ROOT" rev-parse HEAD)"
fi
"$PYTHON" "$SCRIPT_DIR/write_install_manifest.py" \
  --target "$OPENROUTER_HOME/install-manifest.json" \
  --source-commit "$source_commit" \
  --release-version "$(/bin/cat "$REPO_ROOT/VERSION")" \
  --adapter-id "$ADAPTER_ID" \
  --chatgpt-version "$DETECTED_VERSION" \
  --chatgpt-build "$DETECTED_BUILD" \
  --stock-asar-sha256 "$DETECTED_STOCK_ASAR" \
  --workspace "$workspace"

print -- "INFO: 次のnetwork canaryは少量のOpenRouter API利用料が発生する場合があります。"
"$BIN_DIR/codex-openrouter-doctor" --network --secret-scan
print -- "INSTALL: PASS"
print -- "CLI: $BIN_DIR/codex-openrouter"
print -- "Launcher: $DESKTOP_APP"
