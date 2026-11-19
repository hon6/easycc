#!/bin/bash
# 打包 easyCC Linux 版：自带 Python，解压后 ./install.sh 装到用户目录（不需要 root）。
#   tools/build-linux.sh [x86_64|aarch64] [输出目录]
# 产出：<输出目录>/easycc-linux-<架构>-<版本>.tar.gz
# 运行环境用系统里的 Docker 或 Podman（Linux 原生就有容器）；窗口用系统的 GTK WebKit，没有就用浏览器的应用窗口。
set -euo pipefail
ARCH="${1:-x86_64}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${2:-$HERE/dist}"
VERSION="$(cat "$HERE/VERSION")"
NAME="easycc-linux-$ARCH-$VERSION"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
D="$WORK/easycc"
mkdir -p "$D/bin" "$D/app" "$OUT"
say() { echo "==> $*"; }

say "独立 Python（${ARCH}）"
PY_TGZ="${PYTHON_TGZ:-$HOME/.cache/easycc-build/python-$ARCH-linux.tar.gz}"
if [ ! -f "$PY_TGZ" ]; then
  mkdir -p "$(dirname "$PY_TGZ")"
  U="$(curl -fsSL https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest | python3 -c "
import json,sys; d=json.load(sys.stdin)
print([a['browser_download_url'] for a in d['assets'] if a['name'].startswith('cpython-3.12') and a['name'].endswith('$ARCH-unknown-linux-gnu-install_only_stripped.tar.gz')][0])")"
  curl -fsSL -o "$PY_TGZ" "$U"
fi
tar -xzf "$PY_TGZ" -C "$D"
rm -rf "$D/python/lib/python3."*/test "$D/python/lib/python3."*/idlelib "$D/python/lib/python3."*/tkinter "$D/python/lib/python3."*/turtledemo

say "easycc 和客户端"
cp "$HERE/easycc" "$HERE/VERSION" "$D/app/"
cp -R "$HERE/manager" "$D/app/manager"
rm -rf "$D/app/manager/macos" "$D/app/manager/__pycache__"
cp -R "$HERE/image" "$D/app/image"; rm -rf "$D/app/image/vendor"/*   # 不带任何大文件：系统、软件、mihomo 都在构建时从国内镜像站下载
mkdir -p "$D/app/tools"; cp "$HERE/tools/oci-fetch.py" "$HERE/tools/oci-to-docker.py" "$HERE/tools/base-image.py" "$D/app/tools/"
cp "$HERE/manager/linux/easycc-shell.py" "$D/"
cp "$HERE/manager/macos/icon.svg" "$D/easycc.svg"

say "启动器"
cat > "$D/bin/easycc-server" <<'LAUNCH'
#!/bin/bash
# 启动管理服务（第一行输出客户端地址）
R="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
mkdir -p "$HOME/.easycc"
export PATH="$R/python/bin:$PATH" EASYCC_PYTHON="$R/python/bin/python3" LANG="${LANG:-C.UTF-8}"
exec "$R/python/bin/python3" -I "$R/app/manager/cbox_manager.py" "$@" 2>>"$HOME/.easycc/manager.log"
LAUNCH
cat > "$D/bin/easycc-app" <<'LAUNCH'
#!/bin/bash
# 打开 easyCC 窗口：系统有 GTK WebKit 就用原生窗口，否则用浏览器的应用窗口
R="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
if /usr/bin/python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null && \
   /usr/bin/python3 -c "import gi
for v in ('4.1','4.0'):
    try: gi.require_version('WebKit2', v); break
    except ValueError: pass
from gi.repository import WebKit2" 2>/dev/null; then
  exec /usr/bin/python3 "$R/easycc-shell.py"
fi
exec "$R/bin/easycc-server"          # 管理服务自己会用浏览器的应用窗口打开
LAUNCH
cat > "$D/bin/easycc" <<'LAUNCH'
#!/bin/bash
# 命令行：easycc list / start / claude …（和 Mac 上一样）
R="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
export PATH="$R/python/bin:$PATH" EASYCC_PYTHON="$R/python/bin/python3"
exec bash "$R/app/easycc" "$@"
LAUNCH
chmod +x "$D/bin/"*

say "安装脚本"
cat > "$D/install.sh" <<'INST'
#!/bin/bash
# 安装到 ~/.local/share/easycc（不需要 root），加桌面图标和命令 easycc
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${EASYCC_INSTALL_DIR:-$HOME/.local/share/easycc}"
if [ "$SRC" != "$DEST" ]; then rm -rf "$DEST"; mkdir -p "$(dirname "$DEST")"; cp -R "$SRC" "$DEST"; fi
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
ln -sf "$DEST/bin/easycc" "$HOME/.local/bin/easycc"
cp "$DEST/easycc.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/easycc.svg"
cat > "$HOME/.local/share/applications/easycc.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=easyCC
Comment=隔离环境管理器
Exec=$DEST/bin/easycc-app
Icon=easycc
Terminal=false
Categories=Development;Network;
StartupWMClass=easycc
EOF
update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
echo "已安装到 $DEST"
echo "从应用菜单打开 easyCC，或运行：$DEST/bin/easycc-app"
if ! command -v docker >/dev/null && ! command -v podman >/dev/null; then
  echo
  echo "还需要一个容器运行时（二选一）："
  echo "  Ubuntu / Debian：sudo apt install podman      （或 Docker：https://docs.docker.com/engine/install/）"
  echo "  Fedora：sudo dnf install podman"
  echo "  Arch：sudo pacman -S podman"
fi
INST
chmod +x "$D/install.sh"

say "打包"
mv "$D" "$WORK/$NAME"
tar -C "$WORK" -czf "$OUT/$NAME.tar.gz" "$NAME"
ls -la "$OUT/$NAME.tar.gz"
