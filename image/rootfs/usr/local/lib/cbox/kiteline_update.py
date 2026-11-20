#!/usr/bin/env python3
"""环境里的 Kiteline 自动更新后怎么留住。

Kiteline 自己会更新（/opt/kiteline/kiteline/updater.py：下载、装依赖、换文件、重启），环境后台只负责替用户点「更新」
（admin.py 的 kiteline_auto_tick）。问题是 /opt/kiteline 在容器的系统盘里，而环境每次启动都重建容器，更新过的版本会丢，
回到镜像构建时的那一版。所以：

  snapshot   更新成功后把整个 /opt/kiteline 打包存到用户目录（~/.cache/easycc/kiteline/，重建容器后还在）
  restore    容器启动、Kiteline 起来之前：存的那份比镜像里的新，就解开换上（几秒钟，不用联网）

存的那份只对「同一次构建出来的镜像」有效（里面的虚拟环境依赖镜像的 Python 和系统库）：镜像重新构建过就丢掉重来，
重新构建本身会装上当时最新的 Kiteline。

用法：kiteline_update.py restore | snapshot | status（root 运行）"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

HOME = "/opt/kiteline"
BASE_STAMP = "/usr/local/share/easycc/kiteline-base"      # 镜像构建时写的：这次构建的标记 + 当时装的版本
STORE = "/home/dev/.cache/easycc/kiteline"
ARCHIVE = os.path.join(STORE, "kiteline.tar.gz")
META = os.path.join(STORE, "meta.json")


def parse_version(text):
    parts = []
    for piece in str(text or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def installed_version(home=HOME):
    try:
        m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', open(os.path.join(home, "kiteline", "app.py"), encoding="utf-8").read(), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


def base():
    """镜像构建时的标记：{"id": 这次构建的标记, "version": 构建时装的 Kiteline 版本}；老镜像没有这个文件，返回空"""
    try:
        d = json.load(open(BASE_STAMP, encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("id") else {}
    except (OSError, ValueError):
        return {}


def meta():
    try:
        d = json.load(open(META, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _chown_dev(path):
    try:
        shutil.chown(path, "dev")
    except (OSError, LookupError):
        pass


def _chown_tree(root):
    subprocess.run(["chown", "-R", "dev", root], capture_output=True)


def snapshot(log=print):
    """现在装的版本比镜像带的新、又还没存过：存一份。返回存下的版本号，没存返回空"""
    b, ver = base(), installed_version()
    if not b or not ver or parse_version(ver) <= parse_version(b.get("version")):
        return ""
    m = meta()
    if m.get("base") == b["id"] and m.get("version") == ver and os.path.exists(ARCHIVE):
        return ""
    os.makedirs(STORE, exist_ok=True)
    for p in (os.path.dirname(os.path.dirname(STORE)), os.path.dirname(STORE), STORE):
        _chown_dev(p)
    tmp = ARCHIVE + ".tmp"
    t = time.time()
    # 压缩级别 1：三百多兆的目录几秒钟打完；__pycache__ 一起带上，换上后不用重新编译
    r = subprocess.run(["tar", "-C", HOME, "-I", "gzip -1", "-cf", tmp, "."], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        log("保存 Kiteline %s 失败：%s" % (ver, (r.stderr or "").strip()[-200:]))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return ""
    os.replace(tmp, ARCHIVE)
    with open(META + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"base": b["id"], "version": ver, "savedAt": int(time.time()), "size": os.path.getsize(ARCHIVE)}, f)
    os.replace(META + ".tmp", META)
    _chown_dev(ARCHIVE)
    _chown_dev(META)
    log("Kiteline %s 已保存（%d MB，%.0f 秒），重建容器后不用重新下载" % (ver, os.path.getsize(ARCHIVE) >> 20, time.time() - t))
    return ver


def restore(log=print):
    """容器刚起来、Kiteline 还没启动时调：存的那份比现在的新就换上。返回换上的版本号，没换返回空"""
    b, m = base(), meta()
    if not m:
        return ""
    if not b or m.get("base") != b["id"]:
        # 镜像重新构建过（或者是没有标记的老镜像）：存的那份配不上现在的系统，丢掉
        shutil.rmtree(STORE, ignore_errors=True)
        log("镜像重新构建过，丢掉之前保存的 Kiteline %s" % m.get("version", ""))
        return ""
    cur, ver = installed_version(), m.get("version", "")
    if not os.path.exists(ARCHIVE) or parse_version(ver) <= parse_version(cur):
        return ""
    t = time.time()
    new = HOME + ".new"
    shutil.rmtree(new, ignore_errors=True)
    os.makedirs(new)
    r = subprocess.run(["tar", "-C", new, "-xzf", ARCHIVE], capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or installed_version(new) != ver:
        shutil.rmtree(new, ignore_errors=True)
        shutil.rmtree(STORE, ignore_errors=True)       # 坏包：删掉，之后自动更新会重新下载
        log("保存的 Kiteline %s 解不开，已删除，稍后重新更新" % ver)
        return ""
    # 容器的系统盘是 overlay：镜像里带的目录不能整个改名（EXDEV），只能清空里面再把新文件搬进去
    for name in os.listdir(HOME):
        path = os.path.join(HOME, name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    for name in os.listdir(new):
        shutil.move(os.path.join(new, name), os.path.join(HOME, name))
    shutil.rmtree(new, ignore_errors=True)
    _chown_tree(HOME)
    log("换上之前更新好的 Kiteline %s（镜像里是 %s，%.0f 秒）" % (ver, cur, time.time() - t))
    return ver


def status():
    b, m = base(), meta()
    return {"installed": installed_version(), "base": b.get("version", ""), "saved": m.get("version", "") if m.get("base") == b.get("id") else ""}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    say = lambda s: print("[cbox-kiteline %s] %s" % (time.strftime("%H:%M:%S"), s), flush=True)  # noqa: E731
    if cmd == "restore":
        restore(say)
    elif cmd == "snapshot":
        snapshot(say)
    elif cmd == "status":
        print(json.dumps(status(), ensure_ascii=False))
    else:
        sys.exit("用法：kiteline_update.py restore | snapshot | status")
