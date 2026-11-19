#!/bin/bash
# 打包 easyCC Windows 版（预览）：Windows 自带的 WSL2 + easyCC 自己的小发行版（Alpine + Docker + Python），不需要 Docker Desktop。
#   tools/build-windows.sh [输出目录]
# 产出 easycc-windows-x64-<版本>.zip：解压后双击「安装 easyCC.cmd」。
# 架构：客户端服务、easycc 命令行、端口转发都跑在 WSL 发行版里（Linux 代码原样复用），WSL2 把端口自动转到 Windows 的 localhost；
#       Windows 这边只有启动器：导入发行版、启动服务、用 Edge 的应用窗口打开界面。
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$HERE/dist}"
VERSION="$(cat "$HERE/VERSION")"
ALPINE="${ALPINE:-3.22}"; MIRROR="${ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}"
WORK="${WORK:-${TMPDIR:-/tmp}/easycc-win-build}"
rm -rf "$WORK"; mkdir -p "$WORK" "$OUT"; cd "$WORK"
NAME="easycc-windows-x64-$VERSION"
P="$WORK/$NAME"; mkdir -p "$P/app"
say() { echo "==> $*"; }

say "WSL 发行版：Alpine $ALPINE + Docker + Python"
REL="$(curl -fsSL "$MIRROR/v$ALPINE/releases/x86_64/latest-releases.yaml" | awk '/file: alpine-minirootfs/{print $2; exit}')"
curl -fsSL -o mini.tgz "$MIRROR/v$ALPINE/releases/x86_64/$REL"
APKS="$(curl -fsSL "$MIRROR/v$ALPINE/main/$(uname -m)/" | grep -o 'apk-tools-static-[^"]*\.apk' | head -1)"
curl -fsSL -o apks.apk "$MIRROR/v$ALPINE/main/$(uname -m)/$APKS"; tar -xzf apks.apk sbin/apk.static 2>/dev/null
mkdir root; tar -xzf mini.tgz -C root 2>/dev/null || true
./sbin/apk.static --arch x86_64 --root root --keys-dir etc/apk/keys -X "$MIRROR/v$ALPINE/main" -X "$MIRROR/v$ALPINE/community" \
  --no-scripts --no-cache --update-cache add docker containerd runc iptables ip6tables bash python3 openssl openssh-keygen coreutils procps \
  iproute2 ca-certificates tzdata 2>&1 | grep -vE "^\(|errors updating directory permissions" || true
cat > root/etc/wsl.conf <<'EOF'
[boot]
command = /sbin/easycc-wsl-boot
[interop]
appendWindowsPath = false
[automount]
options = "metadata"
[user]
default = root
EOF
cat > root/sbin/easycc-wsl-boot <<'EOF'
#!/bin/sh
# 发行版启动时（root）：起 Docker
mkdir -p /var/log /etc/docker
[ -f /etc/docker/daemon.json ] || echo '{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"2"},"dns":["223.5.5.5","119.29.29.29","8.8.8.8"]}' > /etc/docker/daemon.json
pgrep -x dockerd >/dev/null || (dockerd --host unix:///var/run/docker.sock >> /var/log/dockerd.log 2>&1 &)
EOF
chmod 755 root/sbin/easycc-wsl-boot
echo "nameserver 223.5.5.5" > root/etc/resolv.conf
rm -rf root/usr/share/man root/usr/share/doc
# 发行版里的文件属主统一成 root（在普通用户下做的）
tar --numeric-owner --owner=0 --group=0 -C root -czf "$P/easycc-wsl.tar.gz" .
say "发行版 $(du -h "$P/easycc-wsl.tar.gz" | cut -f1)"

say "easyCC 程序（在 WSL 里运行）"
cp "$HERE/easycc" "$HERE/VERSION" "$P/app/"
cp -R "$HERE/manager" "$P/app/manager"; rm -rf "$P/app/manager/macos" "$P/app/manager/linux" "$P/app/manager/__pycache__"
cp -R "$HERE/image" "$P/app/image"; rm -rf "$P/app/image/vendor"/*   # 不带任何大文件：系统、软件、mihomo 都在构建时从国内镜像站下载
mkdir -p "$P/app/tools"; cp "$HERE/tools/oci-fetch.py" "$HERE/tools/oci-to-docker.py" "$HERE/tools/base-image.py" "$P/app/tools/"

say "Windows 启动器"
cat > "$P/easyCC.ps1" <<'EOF'
# easyCC 启动器：确认 WSL2、导入 easyCC 发行版、启动服务、用 Edge 应用窗口打开
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Data = Join-Path $env:LOCALAPPDATA "easyCC"
$Distro = "easyCC"
New-Item -ItemType Directory -Force -Path $Data | Out-Null
Add-Type -AssemblyName PresentationFramework
function Say($m) { [System.Windows.MessageBox]::Show($m, "easyCC") | Out-Null }

# 1) WSL2
& wsl.exe --status *> $null
if ($LASTEXITCODE -ne 0) {
  Say "easyCC 需要 Windows 自带的 WSL2。`n`n请右键「以管理员身份运行」PowerShell，执行：`n  wsl --install --no-distribution`n然后重启电脑，再打开 easyCC。"
  exit 1
}
# 2) 导入发行版（第一次）
$list = (& wsl.exe -l -q) -replace "`0", ""
if (-not ($list -match "^$Distro$")) {
  & wsl.exe --import $Distro (Join-Path $Data "wsl") (Join-Path $Here "easycc-wsl.tar.gz") --version 2
  if ($LASTEXITCODE -ne 0) { Say "导入 easyCC 运行环境失败，请确认 WSL2 可用（wsl --update）。"; exit 1 }
}
# 3) 启动服务（在 WSL 里）：Windows 路径换成 WSL 路径
$app = (& wsl.exe -d $Distro -- wslpath -a ((Join-Path $Here "app") -replace "\\", "/")).Trim()
$whome = (& wsl.exe -d $Distro -- wslpath -a ($env:USERPROFILE -replace "\\", "/")).Trim()
& wsl.exe -d $Distro -u root -- /sbin/easycc-wsl-boot
$psi = New-Object System.Diagnostics.ProcessStartInfo "wsl.exe", "-d $Distro -u root -- env EASYCC_HOME=$whome/.easycc EASYCC_WSL=1 HOME=/root python3 -I $app/manager/cbox_manager.py --no-open"
$psi.RedirectStandardOutput = $true; $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
$p = [System.Diagnostics.Process]::Start($psi)
$url = $null
while (-not $p.HasExited -and -not $url) {
  $line = $p.StandardOutput.ReadLine()
  if ($line -match "(http://127\.0\.0\.1:\d+/login\?k=[A-Za-z0-9_-]+)") { $url = $Matches[1] }
}
if (-not $url) { Say "easyCC 服务没有启动起来。"; exit 1 }
# 4) Edge 应用窗口（Windows 10/11 自带）
$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($edge) { Start-Process $edge "--app=$url --user-data-dir=`"$Data\edge`"" } else { Start-Process $url }
$p.WaitForExit()
EOF
cat > "$P/easyCC.cmd" <<'EOF'
@echo off
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0easyCC.ps1"
EOF
cat > "$P/安装 easyCC.cmd" <<'EOF'
@echo off
chcp 65001 >nul
set DEST=%LOCALAPPDATA%\easyCC\app
echo 正在安装到 %DEST% ...
robocopy "%~dp0." "%DEST%" /E /NFL /NDL /NJH /NJS >nul
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Programs')+'\easyCC.lnk');$s.TargetPath='%DEST%\easyCC.cmd';$s.WorkingDirectory='%DEST%';$s.WindowStyle=7;$s.Save();$d=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\easyCC.lnk');$d.TargetPath='%DEST%\easyCC.cmd';$d.WorkingDirectory='%DEST%';$d.WindowStyle=7;$d.Save()"
echo 已安装：开始菜单和桌面都有 easyCC。
start "" "%DEST%\easyCC.cmd"
EOF
sed -i 's/$/\r/' "$P/easyCC.cmd" "$P/安装 easyCC.cmd"
# Windows PowerShell 5.1 读 UTF-8 脚本要带 BOM，否则中文乱码
printf '\xef\xbb\xbf' | cat - "$P/easyCC.ps1" > "$P/easyCC.ps1.tmp" && mv "$P/easyCC.ps1.tmp" "$P/easyCC.ps1"

say "打包"
(cd "$WORK" && rm -f "$OUT/$NAME.zip" && python3 -c "
import os, sys, zipfile
z = zipfile.ZipFile(sys.argv[1], 'w', zipfile.ZIP_DEFLATED)
for d, _, fs in os.walk(sys.argv[2]):
    for f in fs:
        p = os.path.join(d, f); z.write(p, os.path.relpath(p, os.path.dirname(sys.argv[2])))
z.close()" "$OUT/$NAME.zip" "$NAME")
ls -la "$OUT/$NAME.zip"
