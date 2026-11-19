#!/bin/bash
# 做 easyCC 虚拟机（macOS 12–25 上用）的系统：Alpine Linux + Docker。在 Linux 上运行，不需要 root。
#   tools/build-vm-image.sh <arm64|amd64> [输出目录]
# 产出：<输出目录>/<架构>/kernel（arm64 是解压后的 Image，amd64 是 bzImage）、initramfs.gz、rootfs.img.gz、VERSION
set -euo pipefail
ARCH="${1:?用法：$0 <arm64|amd64> [输出目录]}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${2:-$HERE/dist/vm}/$ARCH"
ALPINE="${ALPINE:-3.22}"
MIRROR="${ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}"
case "$ARCH" in arm64) A=aarch64 ;; amd64) A=x86_64 ;; *) echo "架构只能是 arm64 或 amd64" >&2; exit 2 ;; esac
WORK="${WORK:-${TMPDIR:-/tmp}/easycc-vm-build/$ARCH}"   # 放本机临时目录（项目目录可能在网络盘上，很慢）
rm -rf "$WORK"; mkdir -p "$WORK" "$OUT"
cd "$WORK"
say() { echo "==> $*"; }

say "下载 Alpine ${ALPINE}（${A}）"
REL="$(curl -fsSL "$MIRROR/v$ALPINE/releases/$A/latest-releases.yaml" | awk '/file: alpine-minirootfs/{print $2; exit}')"
curl -fsSL -o mini.tgz "$MIRROR/v$ALPINE/releases/$A/$REL"
HOSTA="$(uname -m)"
APKS="$(curl -fsSL "$MIRROR/v$ALPINE/main/$HOSTA/" | grep -o 'apk-tools-static-[^"]*\.apk' | head -1)"
curl -fsSL -o apks.apk "$MIRROR/v$ALPINE/main/$HOSTA/$APKS"
tar -xzf apks.apk sbin/apk.static 2>/dev/null
mkdir root; tar -xzf mini.tgz -C root 2>/dev/null || true

say "安装 Docker、内核和工具"
./sbin/apk.static --arch "$A" --root root --keys-dir etc/apk/keys \
  -X "$MIRROR/v$ALPINE/main" -X "$MIRROR/v$ALPINE/community" --no-scripts --no-cache --update-cache add \
  linux-virt busybox-static docker containerd runc iptables ip6tables e2fsprogs e2fsprogs-extra python3 tini \
  ca-certificates iproute2 kmod 2>&1 | grep -vE "^\(|errors updating directory permissions" || true
KVER="$(ls root/lib/modules | head -1)"
say "内核 $KVER"

say "放入 easyCC 启动脚本"
install -m 755 "$HERE/vm/guest/easycc-vm-init" root/sbin/easycc-vm-init
install -D -m 755 "$HERE/vm/guest/easycc-vsock-agent" root/usr/local/sbin/easycc-vsock-agent
echo "nameserver 223.5.5.5" > root/etc/resolv.conf
echo "easycc-vm" > root/etc/hostname
# 精简：文档、手册、内核本体（单独放在外面）
rm -rf root/usr/share/man root/usr/share/doc root/var/cache/apk/* root/boot/dtbs-virt
# 模块是 .ko.gz：有的 Linux 上 depmod 认不出压缩模块，统一解压后再生成依赖表
find "root/lib/modules/$KVER" -name '*.ko.gz' -exec gunzip -f {} +
depmod -b root "$KVER"
[ -s "root/lib/modules/$KVER/modules.dep" ] || { echo "depmod 没生成依赖表" >&2; exit 1; }

say "内核"
case "$ARCH" in
  arm64)
    # Alpine 的 arm64 内核是 EFI zboot（PE 外壳 + 压缩的 Image），苹果虚拟化框架要原始 Image：拆出来解压
    python3 - root/boot/vmlinuz-virt "$OUT/kernel" <<'PY'
import struct, sys, gzip, subprocess
d = open(sys.argv[1], "rb").read()
if d[4:8] == b"zimg":
    off, size = struct.unpack_from("<II", d, 8)
    comp = d[24:32].split(b"\0")[0].decode()
    payload = d[off:off + size]
    if comp == "gzip":
        img = gzip.decompress(payload)
    elif comp == "zstd":
        img = subprocess.run(["zstd", "-dc"], input=payload, capture_output=True, check=True).stdout
    else:
        raise SystemExit("不认识的压缩格式 " + comp)
else:
    img = d
open(sys.argv[2], "wb").write(img)
print("   Image %d MB" % (len(img) >> 20))
PY
    ;;
  amd64) cp root/boot/vmlinuz-virt "$OUT/kernel" ;;
esac
rm -f root/boot/vmlinuz-virt

say "启动盘（initramfs）：只放挂载根盘需要的驱动"
mkdir -p ird/bin ird/dev ird/proc ird/sys ird/newroot "ird/lib/modules/$KVER"
cp root/bin/busybox.static ird/bin/busybox
install -m 755 "$HERE/vm/guest/initramfs-init" ird/init
M="root/lib/modules/$KVER"
need="$(for m in virtio_blk ext4 crc32c_generic; do modprobe -S "$KVER" -d root --show-depends $m 2>/dev/null | awk '/^insmod/{print $2}'; done | sort -u)"
for f in $need; do rel="${f#*/lib/modules/$KVER/}"; mkdir -p "ird/lib/modules/$KVER/$(dirname "$rel")"; cp "root/lib/modules/$KVER/$rel" "ird/lib/modules/$KVER/$rel"; done
cp "$M"/modules.order "$M"/modules.builtin* "ird/lib/modules/$KVER/" 2>/dev/null || true
depmod -b ird "$KVER"
echo "   驱动：$(echo "$need" | wc -w) 个"
(cd ird && find . | cpio -o -H newc --quiet --owner=0:0) | gzip -9 > "$OUT/initramfs.gz"

say "系统盘（ext4）"
SIZE_MB=$(( $(du -sm root | cut -f1) * 13 / 10 + 300 ))
mke2fs -q -t ext4 -L easycc-root -d root -E root_owner=0:0 rootfs.img "${SIZE_MB}M"
gzip -c -6 rootfs.img > "$OUT/rootfs.img.gz"
# 版本号带系统盘内容的哈希：内容变了虚拟机就会换新系统盘（数据盘不动）
echo "alpine=$ALPINE kernel=$KVER built=$(date +%F) sha=$(sha256sum rootfs.img | cut -c1-12)" > "$OUT/VERSION"
ls -la "$OUT"
say "完成：$OUT"
