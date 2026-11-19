#!/usr/bin/env python3
"""把 OCI 镜像包（Apple container `image save` / 我们发布的环境镜像）转成 docker load 认的格式，边读边写，不占额外磁盘。
  oci-to-docker.py <OCI.tar> [--platform linux/amd64] [--tag easycc:latest] | docker load
做法：原样复制所有 blob，再补一个 manifest.json（顶层索引可能套了一层或多层 index，按平台挑出镜像清单）。
兼容 Python 3.9。"""
import argparse
import io
import json
import sys
import tarfile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--platform", default="linux/amd64")
    ap.add_argument("--tag", default="easycc:latest")
    a = ap.parse_args()
    want_os, want_arch = a.platform.split("/")[:2]

    small = {}
    with tarfile.open(a.src, "r:*") as t:
        for m in t:
            name = m.name[2:] if m.name.startswith("./") else m.name
            if m.isfile() and (name == "index.json" or (name.startswith("blobs/") and m.size < 64 * 1024)):
                small[name] = t.extractfile(m).read()

    def blob(digest):
        return json.loads(small["blobs/" + digest.replace(":", "/")])

    def resolve(desc):
        mt = desc.get("mediaType", "")
        if "index" in mt or "manifest.list" in mt:
            idx = blob(desc["digest"])
            for d in idx["manifests"]:
                p = d.get("platform") or {}
                if (not p or (p.get("os") == want_os and p.get("architecture") == want_arch)) and \
                        (d.get("annotations") or {}).get("vnd.docker.reference.type") != "attestation-manifest":
                    r = resolve(d)
                    if r:
                        return r
            return None
        return blob(desc["digest"])

    top = json.loads(small["index.json"])
    man = None
    for d in top["manifests"]:
        man = resolve(d)
        if man:
            break
    if not man:
        sys.exit("包里没有 %s 的镜像" % a.platform)
    path = lambda d: "blobs/" + d.replace(":", "/")  # noqa: E731
    manifest = [{"Config": path(man["config"]["digest"]), "RepoTags": [a.tag], "Layers": [path(l["digest"]) for l in man["layers"]]}]

    out = tarfile.open(fileobj=sys.stdout.buffer, mode="w|")
    with tarfile.open(a.src, "r|*") as t:
        for m in t:
            name = m.name[2:] if m.name.startswith("./") else m.name
            if not m.isfile() or not name.startswith("blobs/"):
                continue
            m.name = name
            out.addfile(m, t.extractfile(m))
    data = json.dumps(manifest).encode()
    ti = tarfile.TarInfo("manifest.json")
    ti.size = len(data)
    out.addfile(ti, io.BytesIO(data))
    out.close()


if __name__ == "__main__":
    main()
