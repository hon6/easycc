#!/bin/bash
# 打官网下载用的 Mac 版：构建 → Developer ID 签名 → 苹果公证 → 盖章 → DMG。
# 别人下载、双击、拖进「应用程序」就能打开，不会提示「无法验证开发者」。
#
#   TEAM=<苹果开发者团队号> manager/macos/release.sh [arm64|x86_64]      默认 arm64
#
# 产物：dist/release/easyCC-<版本>-mac-<arm64|intel>.dmg 和 .sha256
#
# 签名和公证都交给 Xcode，用「Xcode → 设置 → 账户」里登录的付费开发者账号：
#   签名用苹果云端托管的「Developer ID Application」证书（钥匙串里不用有）；公证也走这个账号，不用另建密码或密钥。
# 这两样要用登录钥匙串，ssh 里跑不了：在 Mac 的桌面会话里运行。
#
# 做法：先把 App 里所有可执行文件按发行要求签一遍（临时签名 + 加固运行时 + 权限声明），手工拼一个 Xcode 归档，
# 交给 xcodebuild 导出：它换成 Developer ID 证书重签（加固运行时和权限声明原样保留，加上苹果的时间戳）并提交公证。
# 苹果的 container 运行时不在重签范围里：App 里带的是苹果的原版安装包（见 build-app.sh）。
set -euo pipefail
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
ARCH="${1:-arm64}"
case "$ARCH" in arm64) LABEL=arm64 ;; x86_64) LABEL=intel ;; *) echo "架构只能是 arm64 或 x86_64" >&2; exit 2 ;; esac
TEAM="${TEAM:?先设置 TEAM=苹果开发者团队号（Xcode → 设置 → 账户里能看到）}"
BUNDLE_ID="cn.betac.easycc"
NAME="easyCC"
VERSION="${VERSION:-$(cat "$HERE/VERSION")}"
WORK="$HOME/Library/Caches/easyCC-release/$ARCH"
OUT="${OUT:-$HERE/dist/release}"
DMG="$OUT/$NAME-$VERSION-mac-$LABEL.dmg"
mkdir -p "$WORK" "$OUT"
echo "==> $NAME ${VERSION}（${ARCH}），团队 $TEAM"

echo "==== 1/6 构建 ===="
rm -rf "$WORK/build"
APP_ARCH="$ARCH" VERSION="$VERSION" bash "$HERE/manager/macos/build-app.sh" "$WORK/build" 2>&1 | sed 's/^/    /'
APP="$WORK/build/$NAME.app"

echo "==== 2/6 签名准备（加固运行时）===="
# 扩展属性（来源标记、Finder 信息）会让 codesign 报 detritus not allowed，先清掉
xattr -cr "$APP"
# 静态库不参与运行，公证却会逐个检查里面的目标文件：删掉
find "$APP/Contents/Resources/python" -name '*.a' -delete 2>/dev/null || true
count=0
while IFS= read -r -d '' f; do
  file -b "$f" | grep -q 'Mach-O' || continue
  [ "$f" = "$APP/Contents/MacOS/EasyCC" ] && continue
  case "$f" in
    */vm/easycc-vm) codesign --force --options runtime --entitlements "$HERE/vm/host/easycc-vm.entitlements" --sign - "$f" 2>/dev/null ;;
    *) codesign --force --options runtime --sign - "$f" 2>/dev/null ;;
  esac
  count=$((count + 1))
done < <(find "$APP" -type f \( -perm -u+x -o -name '*.dylib' -o -name '*.so' \) -print0)
codesign --force --options runtime --sign - --identifier "$BUNDLE_ID" "$APP"
echo "    $count 个可执行文件和动态库 + App 本身"

echo "==== 3/6 拼 Xcode 归档 ===="
STAGE="$WORK/stage"; rm -rf "$STAGE"; mkdir -p "$STAGE"
ARCHIVE="$STAGE/$NAME.xcarchive"
mkdir -p "$ARCHIVE/Products/Applications"
ditto "$APP" "$ARCHIVE/Products/Applications/$NAME.app"
cat > "$ARCHIVE/Info.plist" <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>ApplicationProperties</key><dict>
    <key>ApplicationPath</key><string>Applications/$NAME.app</string>
    <key>Architectures</key><array><string>$ARCH</string></array>
    <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>Team</key><string>$TEAM</string>
  </dict>
  <key>ArchiveVersion</key><integer>2</integer>
  <key>CreationDate</key><date>$(date -u +%Y-%m-%dT%H:%M:%SZ)</date>
  <key>Name</key><string>$NAME</string>
  <key>SchemeName</key><string>$NAME</string>
</dict></plist>
P
# destination=upload：签完直接交给苹果公证
cat > "$STAGE/export.plist" <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>developer-id</string>
  <key>destination</key><string>upload</string>
  <key>teamID</key><string>$TEAM</string>
  <key>signingStyle</key><string>automatic</string>
</dict></plist>
P

echo "==== 4/6 Developer ID 签名并提交公证（包比较大，上传要几分钟）===="
xcodebuild -exportArchive -archivePath "$ARCHIVE" -exportOptionsPlist "$STAGE/export.plist" \
  -exportPath "$STAGE/upload" -allowProvisioningUpdates 2>&1 | { grep -vE 'IDEDistributionLogging|Progress [0-9]+%' || true; } | sed 's/^/    /'

echo "==== 5/6 等公证结果 ===="
# 公证通过后 -exportNotarizedApp 才会成功，导出的 App 已经盖好章（离线也能通过检查）
last=""
for i in $(seq 1 180); do
  if xcodebuild -exportNotarizedApp -archivePath "$ARCHIVE" -exportPath "$STAGE/notarized" > "$STAGE/notarize.log" 2>&1; then
    break
  fi
  msg=$(grep -i 'error' "$STAGE/notarize.log" | tail -1 | sed 's/^.*error: *//')
  case "$msg" in
    *nvalid*|*ejected*)
      echo "    公证没通过：$msg"
      echo "    详细原因：xcrun notarytool log <提交 ID>（ID 在 Xcode → Window → Organizer 里能看到）"
      exit 1 ;;
  esac
  [ "$msg" = "$last" ] || echo "    $(date +%H:%M:%S) $msg"
  last="$msg"
  [ "$i" -lt 180 ] || { echo "    等了一小时还没结果，稍后重跑本脚本" >&2; exit 1; }
  sleep 20
done
NAPP="$STAGE/notarized/$NAME.app"
echo "    $(xcrun stapler validate "$NAPP" 2>&1 | tail -1)"
codesign --verify --strict "$NAPP"
# 先存下来再判断：spctl | grep -q 时 grep 一匹配就退出，spctl 收到 SIGPIPE 返回失败，pipefail 下明明通过了也会被当成失败
GATEKEEPER="$(spctl -a -t exec -vv "$NAPP" 2>&1 || true)"
echo "$GATEKEEPER" | sed 's/^/    /'
case "$GATEKEEPER" in *"source=Notarized Developer ID"*) ;; *) echo "    系统没认出公证" >&2; exit 1 ;; esac

echo "==== 6/6 打 DMG ===="
rm -f "$DMG" "$DMG.sha256"
DSTAGE="$WORK/dmg"; rm -rf "$DSTAGE"; mkdir -p "$DSTAGE"
ditto "$NAPP" "$DSTAGE/$NAME.app"
ln -s /Applications "$DSTAGE/Applications"
hdiutil create -volname "$NAME $VERSION" -srcfolder "$DSTAGE" -fs HFS+ -format UDZO -imagekey zlib-level=9 -ov "$DMG" 2>&1 | { grep -v 'is deprecated' || true; } | sed 's/^/    /'
rm -rf "$DSTAGE"
# 装出来的再验一遍：挂载 → 看 DMG 里那份的签名和公证
MNT=$(mktemp -d)
hdiutil attach -nobrowse -readonly -mountpoint "$MNT" "$DMG" >/dev/null 2>&1
check=$(spctl -a -t exec -vv "$MNT/$NAME.app" 2>&1 || true)
hdiutil detach "$MNT" >/dev/null 2>&1
rmdir "$MNT" 2>/dev/null || true
case "$check" in *"source=Notarized Developer ID"*) ;; *) echo "    DMG 里的 App 没通过检查：$check" >&2; exit 1 ;; esac
(cd "$OUT" && shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256")
echo "==> 完成：${DMG}（$(du -h "$DMG" | cut -f1)）"
echo "    SHA-256：$(cut -d' ' -f1 "$DMG.sha256")"
