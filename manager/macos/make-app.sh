#!/bin/bash
# 生成 Mac 应用「easyCC.app」（放进 ~/Applications，可以拖到程序坞）
# 双击打开：后台启动管理服务（不弹终端），再以应用窗口打开客户端
set -euo pipefail
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="${1:-$HOME/Applications}/easyCC.app"
mkdir -p "$DEST/Contents/MacOS" "$DEST/Contents/Resources"
cat > "$DEST/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>easyCC</string>
  <key>CFBundleDisplayName</key><string>easyCC</string>
  <key>CFBundleIdentifier</key><string>local.easycc.manager</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>easycc-launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSUIElement</key><false/>
</dict></plist>
PLIST
cat > "$DEST/Contents/MacOS/easycc-launcher" <<LAUNCH
#!/bin/bash
# 访达启动的程序没有终端的 PATH，补上常见位置（Docker、OrbStack、Homebrew、Apple container）
export PATH="/usr/local/bin:/opt/homebrew/bin:\$HOME/.orbstack/bin:/Applications/Docker.app/Contents/Resources/bin:/usr/bin:/bin:/usr/sbin:/sbin:\$PATH"
LOG="\$HOME/.easycc/manager.log"; mkdir -p "\$HOME/.easycc"
exec "$HERE/easycc" ui >>"\$LOG" 2>&1
LAUNCH
chmod +x "$DEST/Contents/MacOS/easycc-launcher"
# 图标：用 sips / iconutil 从 SVG 生成（失败就用系统默认图标）
if command -v sips >/dev/null && command -v iconutil >/dev/null && command -v qlmanage >/dev/null; then
  tmp="$(mktemp -d)"; cp "$HERE/manager/macos/icon.svg" "$tmp/icon.svg"
  if qlmanage -t -s 1024 -o "$tmp" "$tmp/icon.svg" >/dev/null 2>&1 && [ -f "$tmp/icon.svg.png" ]; then
    mkdir -p "$tmp/AppIcon.iconset"
    for s in 16 32 128 256 512; do
      sips -z $s $s "$tmp/icon.svg.png" --out "$tmp/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
      sips -z $((s*2)) $((s*2)) "$tmp/icon.svg.png" --out "$tmp/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
    done
    iconutil -c icns "$tmp/AppIcon.iconset" -o "$DEST/Contents/Resources/AppIcon.icns" || true
  fi
  rm -rf "$tmp"
fi
touch "$DEST"
echo "已生成：$DEST"
echo "双击打开，或拖到程序坞。第一次打开如果被拦：右键 → 打开。"
