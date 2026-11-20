#!/bin/bash
# 打包「easyCC.app」：用户装好就能用，不需要另装 Docker / Python / Chrome。
# 在 Mac 上运行（需要 Xcode 命令行工具里的 swiftc，只有打包的机器需要）。
#
#   manager/macos/build-app.sh [输出目录]          默认输出到 dist/
#   APP_ARCH=x86_64 manager/macos/build-app.sh ...   Intel 版（没有 Apple container，只用自带虚拟机；材料用 x86_64 的）
#
# 需要的材料（默认在 ~/.claude-box-dev/dl，可用环境变量指定）：
#   CONTAINER_PKG   Apple container 安装包（container-x.y.z-installer-signed.pkg，苹果签名）
#   PYTHON_TGZ      独立 Python（python-build-standalone 的 aarch64-apple-darwin install_only_stripped）
#   KATA_TAR        Linux 内核包（kata-static-*-arm64.tar.zst，container 默认用的那个）
#   VMINIT_TAR      vminit 系统镜像（tools/oci-fetch.py 拉的 OCI 归档）
set -euo pipefail
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
DL="${DL:-$HOME/.claude-box-dev/dl}"
APP_ARCH="${APP_ARCH:-arm64}"                    # arm64（Apple 芯片）/ x86_64（Intel）
case "$APP_ARCH" in arm64) VMARCH=arm64 ;; x86_64) VMARCH=amd64 ;; *) echo "APP_ARCH 只能是 arm64 或 x86_64" >&2; exit 2 ;; esac
CONTAINER_PKG="${CONTAINER_PKG:-$DL/container.pkg}"
if [ "$APP_ARCH" = arm64 ]; then PYTHON_TGZ="${PYTHON_TGZ:-$DL/python.tar.gz}"; else PYTHON_TGZ="${PYTHON_TGZ:-$DL/python-x86_64.tar.gz}"; fi
KATA_TAR="${KATA_TAR:-$DL/kata.tar.zst}"
VMINIT_TAR="${VMINIT_TAR:-$DL/vminit.tar}"
BUILDER_TAR="${BUILDER_TAR:-$DL/builder.tar}"   # 可选：镜像构建器，带上后本机构建不用再连 ghcr
OUT="${1:-$HERE/dist}"
APP="$OUT/easyCC.app"
VERSION="${VERSION:-$(cat "$HERE/VERSION" 2>/dev/null || echo 0.1.0)}"

if [ "$APP_ARCH" = arm64 ]; then
  for f in "$CONTAINER_PKG" "$PYTHON_TGZ" "$KATA_TAR" "$VMINIT_TAR"; do [ -f "$f" ] || { echo "缺少材料：$f" >&2; exit 1; }; done
  pkgutil --check-signature "$CONTAINER_PKG" | grep -q "Apple Inc" || { echo "container 安装包不是苹果签名的，拒绝打包" >&2; exit 1; }
else
  [ -f "$PYTHON_TGZ" ] || { echo "缺少材料：${PYTHON_TGZ}（python-build-standalone 的 x86_64-apple-darwin install_only）" >&2; exit 1; }
fi

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
echo "==> 准备 $APP"
rm -rf "$APP"
R="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$R/bin" "$R/runtime/kernel" "$R/runtime/images" "$R/app"

echo "==> 原生外壳"
swiftc -O -target "$APP_ARCH-apple-macos12.0" -o "$APP/Contents/MacOS/EasyCC" "$HERE/manager/macos/EasyCC.swift" -framework Cocoa -framework WebKit

if [ "$APP_ARCH" = arm64 ]; then        # Apple container 只支持 Apple 芯片 + macOS 26
# 原样带苹果的安装包（它自己是公证过的），不把里面的程序放进 App 包：App 做 Developer ID 签名时包里的程序都会被重签，
# 而苹果这些程序的网络、虚拟化权限只认苹果的签名，重签就不能用了。启动器首次运行时把它解到「应用程序支持」目录里用。
echo "==> Apple container 运行时（带苹果的原版安装包，首次启动时解开）"
pkgutil --expand-full "$CONTAINER_PKG" "$work/pkg"
CBIN="$work/pkg/Payload/bin/container"
cp "$CONTAINER_PKG" "$R/runtime/container.pkg"
shasum -a 256 "$CONTAINER_PKG" | cut -c1-16 > "$R/runtime/container.id"
VMINIT_REF="$("$CBIN" system property list 2>/dev/null | awk -F'"' '/^\[vminit\]/{f=1} f&&/^image/{print $2; exit}')"
[ -n "$VMINIT_REF" ] || VMINIT_REF="ghcr.io/apple/containerization/vminit:0.54.0"

echo "==> Linux 内核"
KBIN="$("$CBIN" system property list 2>/dev/null | awk -F'"' '/^binaryPath/{print $2; exit}')"
[ -n "$KBIN" ] || { echo "读不到内核路径" >&2; exit 1; }
mkdir -p "$work/kata"
if tar -xf "$KATA_TAR" -C "$work/kata" "./$KBIN" 2>/dev/null || tar -xf "$KATA_TAR" -C "$work/kata" "$KBIN" 2>/dev/null; then
  cp "$work/kata/$KBIN" "$R/runtime/kernel/vmlinux"
elif [ -f "${KERNEL_BIN:-$DL/vmlinux}" ]; then      # 系统自带的 tar 解不了 zstd 时：用事先解好的内核（默认找材料目录里的 vmlinux）
  cp "${KERNEL_BIN:-$DL/vmlinux}" "$R/runtime/kernel/vmlinux"
else
  echo "解不开内核包（tar 不支持 zstd？）：用 KERNEL_BIN=已解出的 vmlinux 指定" >&2; exit 1
fi

echo "==> 系统镜像 $VMINIT_REF"
cp "$VMINIT_TAR" "$R/runtime/images/vminit.tar"
echo "$VMINIT_REF" > "$R/runtime/images/vminit.ref"
if [ -f "$BUILDER_TAR" ]; then
  cp "$BUILDER_TAR" "$R/runtime/images/builder.tar"
  "$CBIN" system property list 2>/dev/null | awk -F'"' '/^\[build\]/{f=1} f&&/^image/{print $2; exit}' > "$R/runtime/images/builder.ref"
fi
fi

echo "==> 独立 Python"
tar -xzf "$PYTHON_TGZ" -C "$R"          # 解出来是 python/
rm -rf "$R/python/lib/python3."*/test "$R/python/lib/python3."*/idlelib "$R/python/lib/python3."*/tkinter "$R/python/lib/python3."*/turtledemo
"$R/python/bin/python3" -c 'import ssl, json, http.server, tarfile; print("   Python", __import__("sys").version.split()[0], "可用")'

echo "==> easycc 和客户端"
cp "$HERE/easycc" "$HERE/VERSION" "$R/app/"
cp -R "$HERE/manager" "$R/app/manager"
rm -rf "$R/app/manager/macos" "$R/app/manager/__pycache__"
cp -R "$HERE/image" "$R/app/image"
rm -rf "$R/app/image/vendor"/*   # 不带任何大文件：系统、软件、mihomo 都在构建时从国内镜像站下载
mkdir -p "$R/app/tools" && cp "$HERE/tools/oci-fetch.py" "$HERE/tools/oci-to-docker.py" "$HERE/tools/base-image.py" "$R/app/tools/"

# macOS 12–25 用的虚拟机运行时（Alpine + Docker）：tools/build-vm-image.sh 做好放在 VM_SRC；docker 命令行静态包 DOCKER_TGZ
VM_SRC="${VM_SRC:-$DL/vm/$VMARCH}"; DOCKER_TGZ="${DOCKER_TGZ:-$DL/docker-$VMARCH.tgz}"
if [ -f "$VM_SRC/rootfs.img.gz" ] && [ -f "$DOCKER_TGZ" ]; then
  echo "==> easyCC 虚拟机（macOS 12–25）"
  mkdir -p "$R/vm" "$R/bin"
  cp "$VM_SRC/kernel" "$VM_SRC/initramfs.gz" "$VM_SRC/rootfs.img.gz" "$VM_SRC/VERSION" "$R/vm/"
  swiftc -O -target "$APP_ARCH-apple-macos12.0" -o "$R/vm/easycc-vm" "$HERE/vm/host/easycc-vm.swift" -framework Virtualization 2>&1 | grep -v warning | grep -E "error" && exit 1
  codesign -f -s - --entitlements "$HERE/vm/host/easycc-vm.entitlements" "$R/vm/easycc-vm"
  tar -xzf "$DOCKER_TGZ" -C "$work" docker/docker && cp "$work/docker/docker" "$R/bin/docker"
else
  echo "==> 没有虚拟机材料（${VM_SRC}、${DOCKER_TGZ}），这个 App 只能在 macOS 26 上用"
fi

echo "==> 启动器"
cat > "$R/bin/easycc-server" <<'LAUNCH'
#!/bin/bash
# 由 EasyCC 原生外壳调用：拉起 App 自带的容器运行时，再启动管理服务（第一行输出客户端地址）
#   easycc-server --runtime-only   只拉起容器运行时（客户端更改存储位置后用）
R="$(cd "$(dirname "$0")/.." && pwd)"
SUP="$HOME/Library/Application Support/easyCC"
# 存储位置在外置盘、盘没接上：提示接上后重试（不能在这时建空目录，否则数据「消失」）
while :; do
  miss=""
  for d in "$HOME/.easycc" "$SUP" "$SUP/container"; do [ -L "$d" ] && [ ! -e "$d" ] && miss="$(readlink "$d")" && break; done
  [ -z "$miss" ] && break
  [ "${1:-}" = --runtime-only ] && { echo "存储位置不可用：$miss" >&2; exit 1; }
  b="$(osascript -e "button returned of (display dialog \"easyCC 的存储位置不可用：\n$miss\n\n外置盘没接上？接上后点「重试」。\" buttons {\"退出\", \"重试\"} default button \"重试\" with icon caution with title \"easyCC\")" 2>/dev/null)"
  [ "$b" = 重试 ] || exit 1
done
mkdir -p "$SUP/container" "$SUP/logs" "$HOME/.easycc"
exec 2>>"$SUP/logs/launcher.log"
echo "---- $(date '+%F %T') 启动 ${1:-}" >&2
export LANG="${LANG:-en_US.UTF-8}"
# Apple container 只在 macOS 26 + Apple 芯片上用；更早的系统用自带虚拟机（启动环境时才开）
MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
# 运行时解到内置盘上的固定位置：App 包里带的是苹果的原版安装包，解出来的程序保持苹果的签名。
# 不放进 $SUP：用户可能把数据（$SUP）挪到外置盘，而 container 的网络插件在外置盘上建不起虚拟网络（vmnet 1001，实测）。
# 首次启动、或者 App 更新后带的安装包变了，就重新解一次（十来秒）。
RT="$HOME/Library/Application Support/easyCC-runtime/container"
# 停服务：服务卡住时 container system stop 会一直不返回，最多等 30 秒
stop_rt() { [ -x "$1" ] || return 0; "$1" system stop >&2 & local p=$!; ( sleep 30; kill $p 2>/dev/null ) & local w=$!; wait $p; kill $w 2>/dev/null; }
rm -rf "$SUP/runtime" 2>/dev/null       # 0.9.3 测试版解在这里过
if [ -f "$R/runtime/container.pkg" ] && [ "$(uname -m)" = arm64 ] && [ "$MAJOR" -ge 26 ] \
   && [ "$(cat "$RT/.pkg-id" 2>/dev/null)" != "$(cat "$R/runtime/container.id" 2>/dev/null)" ]; then
  echo "解开容器运行时（首次启动或 App 更新后）" >&2
  tmp="$(dirname "$RT")/.expand.$$"; rm -rf "$tmp" "$RT.new"; mkdir -p "$(dirname "$RT")"
  if /usr/sbin/pkgutil --expand-full "$R/runtime/container.pkg" "$tmp" >&2 && [ -x "$tmp/Payload/bin/container" ]; then
    mv "$tmp/Payload" "$RT.new"
    rm -f "$RT.new/bin/uninstall-container.sh" "$RT.new/bin/update-container.sh"
    xattr -dr com.apple.quarantine "$RT.new" 2>/dev/null
    cp "$R/runtime/container.id" "$RT.new/.pkg-id"
    # 旧版本的服务还在跑：先停掉再换文件（运行中的环境会停，App 更新运行时才会走到这里）
    stop_rt "$RT/bin/container"
    rm -rf "$RT"; mv "$RT.new" "$RT"
  else
    echo "容器运行时解包失败" >&2
  fi
  rm -rf "$tmp"
fi
export PATH="$RT/bin:$R/python/bin:/usr/bin:/bin:/usr/sbin:/sbin"
[ -d "$R/bin" ] && export PATH="$R/bin:$PATH"
C="$RT/bin/container"
if [ -x "$C" ] && [ "$(uname -m)" = arm64 ] && [ "$MAJOR" -ge 26 ]; then
  # 服务是别的位置的程序注册的（0.9.2 以前运行时在 App 包里）：那些文件已经不在了，停掉，下面按现在的位置重新注册
  P="$SUP/container/apiserver/apiserver.plist"
  # 比较真实路径：数据目录可能是指向外置盘的软链接，服务登记的是解析后的路径
  if [ -f "$P" ] && [ "$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:0" "$P" 2>/dev/null)" != "$(cd "$RT/bin" && pwd -P)/container-apiserver" ]; then
    echo "容器服务注册的是旧位置，重新注册" >&2
    stop_rt "$C"
    # 旧版在两个域里都可能留着服务（apiserver 和它拉起的网络、镜像等辅助服务），程序文件已经不在了：全部注销
    for l in $(launchctl list | awk '{print $3}' | grep '^com\.apple\.container\.'); do launchctl remove "$l" 2>/dev/null; done
    for dom in user gui; do
      for l in $(launchctl print "$dom/$(id -u)" 2>/dev/null | grep -oE 'com\.apple\.container\.[A-Za-z0-9._-]+' | sort -u); do
        launchctl bootout "$dom/$(id -u)/$l" 2>/dev/null
      done
    done
    rm -f "$P"
  fi
  if ! "$C" system status >/dev/null 2>&1; then
    # 服务装到「用户」域（user/UID）：从 App 里启动默认进图形会话域，那里的后台服务读不了外置盘，存储位置在外置盘时会起不来
    if [ -f "$P" ]; then
      launchctl bootout "gui/$(id -u)/com.apple.container.apiserver" 2>/dev/null
      launchctl bootstrap "user/$(id -u)" "$P" 2>/dev/null
      for i in 1 2 3 4 5 6 7 8 9 10; do "$C" system status >/dev/null 2>&1 && break; sleep 1; done
    fi
    # 第一次用（还没有 plist），或者上面没起来：让 container 自己装，它会写好 plist 并补齐其余服务
    # 自带的 --timeout 在服务起不来时不管用，自己看着：最多等 90 秒
    "$C" system start --install-root "$RT" --app-root "$SUP/container" --log-root "$SUP/logs" \
      --disable-kernel-install --timeout 90 >&2 & sp=$!
    ( sleep 90; kill $sp 2>/dev/null ) & wdp=$!
    wait $sp || echo "容器运行时启动失败" >&2
    kill $wdp 2>/dev/null
    # container 自己注册的服务在图形会话域：存储位置在外置盘时那里读写不了，改注册到用户域
    if ! "$C" system status >/dev/null 2>&1 && [ -f "$P" ]; then
      echo "改到用户域重新注册容器服务" >&2
      launchctl bootout "gui/$(id -u)/com.apple.container.apiserver" 2>/dev/null
      launchctl bootstrap "user/$(id -u)" "$P" 2>/dev/null
      for i in $(seq 1 45); do "$C" system status >/dev/null 2>&1 && break; sleep 1; done
      "$C" system status >/dev/null 2>&1 || echo "容器运行时启动失败" >&2
    fi
  fi
  # 内核：已经装过会报「已存在」，忽略
  "$C" system kernel set --binary "$R/runtime/kernel/vmlinux" >/dev/null 2>&1 || true
  "$C" image inspect "$(cat "$R/runtime/images/vminit.ref")" >/dev/null 2>&1 || "$C" image load -i "$R/runtime/images/vminit.tar" >&2
  if [ -f "$R/runtime/images/builder.tar" ] && ! "$C" image inspect "$(cat "$R/runtime/images/builder.ref")" >/dev/null 2>&1; then
    "$C" image load -i "$R/runtime/images/builder.tar" >&2 &
  fi
fi
[ "${1:-}" = --runtime-only ] && { "$C" system status >/dev/null 2>&1 || [ "$MAJOR" -lt 26 ]; exit; }
# 编译缓存（__pycache__）放到应用程序支持目录：往 App 包里写文件会让签名失效
exec "$R/python/bin/python3" -I -X pycache_prefix="$SUP/pycache" "$R/app/manager/cbox_manager.py" "$@"
LAUNCH
chmod +x "$R/bin/easycc-server"

echo "==> 图标"
if command -v qlmanage >/dev/null && command -v iconutil >/dev/null; then
  cp "$HERE/manager/macos/icon.svg" "$work/icon.svg"
  if qlmanage -t -s 1024 -o "$work" "$work/icon.svg" >/dev/null 2>&1 && [ -f "$work/icon.svg.png" ]; then
    mkdir -p "$work/AppIcon.iconset"
    for s in 16 32 128 256 512; do
      sips -z $s $s "$work/icon.svg.png" --out "$work/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
      sips -z $((s*2)) $((s*2)) "$work/icon.svg.png" --out "$work/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
    done
    iconutil -c icns "$work/AppIcon.iconset" -o "$R/AppIcon.icns" || true
  fi
fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>easyCC</string>
  <key>CFBundleDisplayName</key><string>easyCC</string>
  <key>CFBundleIdentifier</key><string>cn.betac.easycc</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>EasyCC</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSArchitecturePriority</key><array><string>$APP_ARCH</string></array>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

echo "==> 签名（只签我们自己的外壳；苹果的 container 二进制保持原签名，它的网络权限依赖苹果签名）"
codesign --force -s - "$APP/Contents/MacOS/EasyCC"
xattr -cr "$APP" 2>/dev/null || true
du -sh "$APP"
echo "已生成：$APP"
