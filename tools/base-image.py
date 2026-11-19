#!/usr/bin/env python3
"""环境镜像的系统底包：从国内镜像站下载 Ubuntu 官方底包（ubuntu-base，约 30 MB），校验后打成 OCI 镜像包。
不依赖 Docker Hub / 镜像仓库（国内基本连不上），只走普通 HTTP 下载；几个镜像站按速度挑，海外用户自动用官方源。

  base-image.py base --arch amd64 --out base.tar [--tag easycc-base:24.04-amd64]   下载底包 → OCI 镜像包
  base-image.py apt  --arch amd64        输出最快的 apt 源（http://…/ubuntu 或 …/ubuntu-ports）
  base-image.py node                     输出最快的 Node.js 下载源

兼容 Python 3.9，只用标准库。
"""
import argparse
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import threading
import time
import urllib.request

UA = {"User-Agent": "easyCC"}
CDIMAGE = os.environ.get("EASYCC_UBUNTU_BASE_MIRRORS", "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-cdimage https://mirrors.ustc.edu.cn/ubuntu-cdimage "
                         "https://mirrors.aliyun.com/ubuntu-cdimage https://mirrors.huaweicloud.com/ubuntu-cdimage https://cdimage.ubuntu.com").split()
APT = os.environ.get("EASYCC_APT_MIRRORS", "http://mirrors.tuna.tsinghua.edu.cn http://mirrors.ustc.edu.cn http://mirrors.aliyun.com "
                     "http://mirrors.huaweicloud.com OFFICIAL").split()
NODE = os.environ.get("EASYCC_NODE_MIRRORS", "https://mirrors.tuna.tsinghua.edu.cn/nodejs-release https://mirrors.ustc.edu.cn/node "
                      "https://mirrors.huaweicloud.com/nodejs https://nodejs.org/dist").split()
REL = "/ubuntu-base/releases/24.04/release"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def probe(url, timeout=6):
    """取前 64 KB，返回用时（秒）；不通返回 None"""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=dict(UA, Range="bytes=0-65535"))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(65536)
            if r.status not in (200, 206):
                return None
        return time.time() - t0
    except Exception:  # noqa: BLE001
        return None


def fastest(cands):
    """[(前缀, 测试地址)] → 最快的前缀（并发测，几秒内出结果）"""
    res = {}

    def run(m, u):
        res[m] = probe(u)
    ths = [threading.Thread(target=run, args=c, daemon=True) for c in cands]   # 守护线程：DNS 卡住的不拖住退出
    for t in ths:
        t.start()
    end, first = time.time() + 8, None
    while time.time() < end and any(t.is_alive() for t in ths):
        if first is None and any(v is not None for v in list(res.values())):
            first = time.time()
        if first and time.time() - first > 1.5:      # 已经有能用的，再等 1.5 秒比一比就行
            break
        time.sleep(0.1)
    ok = sorted((t, m) for m, t in list(res.items()) if t is not None)
    return ok[0][1] if ok else None


def get(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def apt_mirror(arch):
    path = "/ubuntu" if arch == "amd64" else "/ubuntu-ports"
    cands = []
    for m in APT:
        base = ("http://archive.ubuntu.com/ubuntu" if arch == "amd64" else "http://ports.ubuntu.com/ubuntu-ports") if m == "OFFICIAL" else m.rstrip("/") + path
        cands.append((base, base + "/dists/noble/Release"))
    return fastest(cands)


def node_mirror():
    return fastest([(m.rstrip("/"), m.rstrip("/") + "/latest-v22.x/SHASUMS256.txt") for m in NODE])


def oci_archive(layer_path, arch, tag, out):
    """把 rootfs.tar.gz 原样当成一层，打成 OCI 镜像包（Apple container / Podman 直接 load；Docker 经 oci-to-docker.py）"""
    h_gz, h_raw, size = hashlib.sha256(), hashlib.sha256(), 0
    with open(layer_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h_gz.update(chunk)
            size += len(chunk)
    with gzip.open(layer_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h_raw.update(chunk)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    config = json.dumps({
        "architecture": arch, "os": "linux", "created": created,
        "config": {"Env": ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"], "Cmd": ["/bin/bash"]},
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + h_raw.hexdigest()]},
        "history": [{"created": created, "created_by": "easyCC：Ubuntu 官方底包 %s" % os.path.basename(layer_path)}],
    }, separators=(",", ":")).encode()
    cfg_d = hashlib.sha256(config).hexdigest()
    manifest = json.dumps({
        "schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": "sha256:" + cfg_d, "size": len(config)},
        "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": "sha256:" + h_gz.hexdigest(), "size": size}],
    }, separators=(",", ":")).encode()
    man_d = hashlib.sha256(manifest).hexdigest()
    full = tag if "/" in tag.split(":")[0] else "docker.io/library/" + tag
    index = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [{
        "mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + man_d, "size": len(manifest),
        "platform": {"architecture": arch, "os": "linux"},
        "annotations": {"io.containerd.image.name": full, "org.opencontainers.image.ref.name": full}}]}).encode()

    def add(t, name, data=None, path=None):
        if path:
            ti = t.gettarinfo(path, arcname=name)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            with open(path, "rb") as f:
                t.addfile(ti, f)
            return
        ti = tarfile.TarInfo(name)
        ti.size, ti.mtime, ti.mode = len(data), int(time.time()), 0o644
        t.addfile(ti, io.BytesIO(data))

    tmp = out + ".part"
    with tarfile.open(tmp, "w") as t:
        add(t, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
        add(t, "index.json", index)
        add(t, "blobs/sha256/" + man_d, manifest)
        add(t, "blobs/sha256/" + cfg_d, config)
        add(t, "blobs/sha256/" + h_gz.hexdigest(), path=layer_path)
    os.replace(tmp, out)


def base(arch, out, tag, cache):
    m = fastest([(c.rstrip("/"), c.rstrip("/") + REL + "/SHA256SUMS") for c in CDIMAGE])
    if not m:
        sys.exit("连不上 Ubuntu 镜像站：%s" % " ".join(CDIMAGE))
    sums = get(m + REL + "/SHA256SUMS").decode()
    found = []
    for line in sums.splitlines():
        mm = re.match(r"([0-9a-f]{64}) [ *]?(ubuntu-base-24\.04(?:\.(\d+))?-base-%s\.tar\.gz)$" % arch, line.strip())
        if mm:
            found.append((int(mm.group(3) or 0), mm.group(2), mm.group(1)))
    if not found:
        sys.exit("镜像站上没有 %s 的 Ubuntu 24.04 底包" % arch)
    _, fn, want = max(found)
    os.makedirs(cache, exist_ok=True)
    dest = os.path.join(cache, fn)
    if not (os.path.exists(dest) and hashlib.sha256(open(dest, "rb").read()).hexdigest() == want):
        url = m + REL + "/" + fn
        log("▸ 下载 %s" % url)
        h, got, last = hashlib.sha256(), 0, 0
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r, open(dest + ".part", "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            for chunk in iter(lambda: r.read(1 << 18), b""):
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if total and time.time() - last > 2:
                    log("  %d%%  %.1f / %.1f MB" % (got * 100 // total, got / 1048576, total / 1048576))
                    last = time.time()
        if h.hexdigest() != want:
            os.remove(dest + ".part")
            sys.exit("底包校验不对（SHA-256），请重试")
        os.replace(dest + ".part", dest)
    log("▸ 底包 %s 校验通过（SHA-256），打成镜像包" % fn)
    oci_archive(dest, arch, tag, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("base", "apt", "node"))
    ap.add_argument("--arch", default="amd64", choices=("amd64", "arm64"))
    ap.add_argument("--out")
    ap.add_argument("--tag", default="")
    ap.add_argument("--cache", default=os.path.expanduser("~/.easycc/_cache"))
    a = ap.parse_args()
    if a.cmd == "base":
        if not a.out:
            sys.exit("缺少 --out")
        base(a.arch, a.out, a.tag or "easycc-base:24.04-" + a.arch, a.cache)
    else:
        m = apt_mirror(a.arch) if a.cmd == "apt" else node_mirror()
        if not m:
            sys.exit("所有%s源都连不上" % ("apt " if a.cmd == "apt" else "Node.js "))
        print(m)


if __name__ == "__main__":
    main()
