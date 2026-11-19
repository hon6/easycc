#!/bin/bash
# 把构建镜像要下载的大文件提前下好，放进 image/vendor/。构建时优先用这里的文件，没有才联网下载。
# 用途：在网络好的机器上跑一次，国内机器构建就不会卡在 Mozilla / GitHub 下载上。
#   tools/prefetch-vendor.sh [amd64|arm64]     默认 amd64
set -euo pipefail
ARCH="${1:-amd64}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
V="$HERE/image/vendor"
mkdir -p "$V"
dl() { curl -fL --retry 5 --retry-all-errors --retry-delay 3 -o "$1.part" "$2" && mv "$1.part" "$1"; }

echo "==> Firefox（Mozilla 官方 APT 源，${ARCH}）"
idx="$(curl -fsSL --retry 5 "https://packages.mozilla.org/apt/dists/mozilla/main/binary-$ARCH/Packages")"
# 取最新的 firefox 主包（不是语言包）
read -r ff_file ff_sha < <(printf '%s\n' "$idx" | awk -v RS= '/\nPackage: firefox\n|^Package: firefox\n/' |
  awk '/^Version:/{v=$2} /^Filename:/{f=$2} /^SHA256:/{s=$2} END{print f, s}')
[ -n "$ff_file" ] || { echo "找不到 firefox 包" >&2; exit 1; }
rm -f "$V"/firefox_*.deb
dl "$V/firefox_latest_$ARCH.deb" "https://packages.mozilla.org/apt/$ff_file"
echo "$ff_sha  $V/firefox_latest_$ARCH.deb" | sha256sum -c -

if [ "$ARCH" = amd64 ]; then
  echo "==> Google Chrome（Google 官方 APT 源，只有 amd64；按源里的 SHA-256 校验）"
  read -r gc_file gc_sha < <(curl -fsSL --retry 5 "https://dl.google.com/linux/chrome/deb/dists/stable/main/binary-amd64/Packages" |
    awk -v RS= '/Package: google-chrome-stable\n/' | awk '/^Filename:/{f=$2} /^SHA256:/{s=$2} END{print f, s}')
  [ -n "$gc_file" ] || { echo "找不到 google-chrome-stable 包" >&2; exit 1; }
  rm -f "$V"/google-chrome-stable_*.deb
  dl "$V/google-chrome-stable_latest_amd64.deb" "https://dl.google.com/linux/chrome/deb/$gc_file"
  echo "$gc_sha  $V/google-chrome-stable_latest_amd64.deb" | sha256sum -c -
fi

echo "==> mihomo"
ver="$(curl -fsSL --retry 5 https://api.github.com/repos/MetaCubeX/mihomo/releases/latest | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"
case "$ARCH" in amd64) asset=amd64-compatible ;; arm64) asset=arm64 ;; esac
rm -f "$V"/mihomo-linux-$asset-*.gz
dl "$V/mihomo-linux-$asset-$ver.gz" "https://github.com/MetaCubeX/mihomo/releases/download/$ver/mihomo-linux-$asset-$ver.gz"
gzip -t "$V/mihomo-linux-$asset-$ver.gz"

if [ "$ARCH" = amd64 ]; then
  echo "==> Kiteline（官网安装包，按官网公布的 SHA-256 校验）"
  dl "$V/Kiteline-Linux.tar.gz" https://tars.betac.cn/download/Kiteline-Linux.tar.gz
  curl -fsSL --retry 5 -o "$V/Kiteline-Linux.tar.gz.sha256" https://tars.betac.cn/download/Kiteline-Linux.tar.gz.sha256
  [ "$(cut -d' ' -f1 "$V/Kiteline-Linux.tar.gz.sha256")" = "$(sha256sum "$V/Kiteline-Linux.tar.gz" | cut -d' ' -f1)" ]
fi
ls -la "$V"
echo "完成。构建时会自动使用这些文件。"
