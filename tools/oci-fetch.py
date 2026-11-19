#!/usr/bin/env python3
"""从镜像仓库下载一个镜像，打成 OCI 镜像归档（tar），给 `container image load -i` / `docker load` 用。
只用标准库。用法：
  oci-fetch.py <镜像> <输出.tar> [--platform linux/arm64]
例：oci-fetch.py ghcr.io/apple/containerization/vminit:0.13.0 vminit.tar --platform linux/arm64
"""
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request

ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"])


def parse_ref(ref):
    if "/" not in ref.split(":")[0] or "." not in ref.split("/")[0] and ":" not in ref.split("/")[0] and ref.split("/")[0] != "localhost":
        ref = "docker.io/" + (ref if "/" in ref else "library/" + ref)
    host, rest = ref.split("/", 1)
    if "@" in rest:
        repo, tag = rest.split("@", 1)
    elif ":" in rest.rsplit("/", 1)[-1]:
        repo, tag = rest.rsplit(":", 1)
    else:
        repo, tag = rest, "latest"
    if host == "docker.io":
        host = "registry-1.docker.io"
    return host, repo, tag


class Registry:
    def __init__(self, host, repo):
        self.host, self.repo, self.token = host, repo, None

    def _open(self, url, accept=None, retry=True):
        req = urllib.request.Request(url, headers={"Accept": accept or ACCEPT, "User-Agent": "oci-fetch/1"})
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            return urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry:
                self._auth(e.headers.get("WWW-Authenticate", ""))
                return self._open(url, accept, False)
            if e.code == 429 and retry:
                time.sleep(5)
                return self._open(url, accept, False)
            raise

    def _auth(self, header):
        m = dict(re.findall(r'(\w+)="([^"]*)"', header))
        url = "%s?service=%s&scope=%s" % (m["realm"], m.get("service", ""), m.get("scope", "repository:%s:pull" % self.repo))
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.load(r)
        self.token = d.get("token") or d.get("access_token")

    def get(self, kind, ref, accept=None):
        with self._open("https://%s/v2/%s/%s/%s" % (self.host, self.repo, kind, ref), accept) as r:
            return r.read(), r.headers.get("Content-Type", "")

    def blob_to(self, digest, fh):
        with self._open("https://%s/v2/%s/blobs/%s" % (self.host, self.repo, digest), "*/*") as r:
            h = hashlib.sha256()
            while True:
                c = r.read(1 << 20)
                if not c:
                    break
                h.update(c)
                fh.write(c)
        if "sha256:" + h.hexdigest() != digest:
            raise ValueError("校验失败 %s" % digest)


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    ref, out = sys.argv[1], sys.argv[2]
    plat = sys.argv[sys.argv.index("--platform") + 1] if "--platform" in sys.argv else "linux/arm64"
    os_, arch = plat.split("/")[:2]
    host, repo, tag = parse_ref(ref)
    reg = Registry(host, repo)
    data, ctype = reg.get("manifests", tag)
    top = json.loads(data)
    if top.get("manifests"):
        cand = [m for m in top["manifests"] if m.get("platform", {}).get("os") == os_ and m.get("platform", {}).get("architecture") == arch]
        if not cand:
            sys.exit("没有 %s 平台的镜像" % plat)
        mdesc = cand[0]
        data, ctype = reg.get("manifests", mdesc["digest"])
    manifest = json.loads(data)
    mdigest = "sha256:" + hashlib.sha256(data).hexdigest()
    mtype = manifest.get("mediaType") or ctype.split(";")[0]
    tmp = out + ".blobs"
    os.makedirs(tmp, exist_ok=True)
    blobs = [manifest["config"]] + manifest["layers"]
    total = sum(b["size"] for b in blobs)
    done = 0
    for b in blobs:
        p = os.path.join(tmp, b["digest"].split(":")[1])
        if not (os.path.exists(p) and os.path.getsize(p) == b["size"]):
            with open(p, "wb") as f:
                reg.blob_to(b["digest"], f)
        done += b["size"]
        print("  %5.1f%%  %s" % (done * 100.0 / total, b["digest"][:19]), flush=True)
    full_ref = "%s/%s:%s" % ("docker.io" if host == "registry-1.docker.io" else host, repo, tag)
    index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
             "manifests": [{"mediaType": mtype, "digest": mdigest, "size": len(data),
                            "platform": {"os": os_, "architecture": arch},
                            "annotations": {"org.opencontainers.image.ref.name": full_ref, "io.containerd.image.name": full_ref}}]}
    with tarfile.open(out, "w") as tar:
        def add_bytes(name, b):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mtime = len(b), int(time.time())
            tar.addfile(ti, io.BytesIO(b))
        add_bytes("oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
        add_bytes("index.json", json.dumps(index).encode())
        add_bytes("blobs/sha256/" + mdigest.split(":")[1], data)
        for b in blobs:
            tar.add(os.path.join(tmp, b["digest"].split(":")[1]), "blobs/sha256/" + b["digest"].split(":")[1])
    for f in os.listdir(tmp):
        os.remove(os.path.join(tmp, f))
    os.rmdir(tmp)
    print("已生成 %s（%s，%.1f MB）" % (out, full_ref, os.path.getsize(out) / 1e6))


if __name__ == "__main__":
    main()
