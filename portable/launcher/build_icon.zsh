#!/bin/zsh
set -eu

[[ $# -eq 2 ]] || { print -u2 -- "Usage: $0 RENDERER.swift OUTPUT.icns"; exit 2; }
readonly SOURCE="${1:A}"
readonly OUTPUT="${2:A}"
[[ -f "$SOURCE" && ! -L "$SOURCE" ]] || { print -u2 -- "icon sourceが不正です: $SOURCE"; exit 1; }

readonly TEMP_DIR="$(/usr/bin/mktemp -d /tmp/codex-openrouter-icon.XXXXXX)"
cleanup() { /bin/rm -rf "$TEMP_DIR"; }
trap cleanup EXIT INT TERM
readonly ICONSET="$TEMP_DIR/AppIcon.iconset"
/bin/mkdir -p "$ICONSET" "${OUTPUT:h}"
/usr/bin/xcrun swiftc -module-cache-path "$TEMP_DIR/module-cache" \
  "$SOURCE" -o "$TEMP_DIR/render-icon"
"$TEMP_DIR/render-icon" "$TEMP_DIR/base.png"

for specification in \
  '16 icon_16x16.png' \
  '32 icon_16x16@2x.png' \
  '32 icon_32x32.png' \
  '64 icon_32x32@2x.png' \
  '128 icon_128x128.png' \
  '256 icon_128x128@2x.png' \
  '256 icon_256x256.png' \
  '512 icon_256x256@2x.png' \
  '512 icon_512x512.png' \
  '1024 icon_512x512@2x.png'; do
  size="${specification%% *}"
  name="${specification#* }"
  /usr/bin/sips -z "$size" "$size" "$TEMP_DIR/base.png" --out "$ICONSET/$name" >/dev/null
done
/usr/bin/iconutil -c icns "$ICONSET" -o "$OUTPUT"
[[ -s "$OUTPUT" ]] || { print -u2 -- "icns生成に失敗しました"; exit 1; }
