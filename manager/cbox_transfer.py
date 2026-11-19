"""环境导出 / 导入：一个环境打成一个 .cbox 文件（tar.gz）。兼容 Python 3.9（Mac 自带）。

包里：
  cbox-export.json   清单：版本、名称、类型、分组、备注、代理、项目路径、时区语言等设置、出口信息
  home/...           容器用户目录：浏览器配置（Cookie、登录状态）、Claude 登录、shell 配置等
不包括：项目代码（只记路径）、缓存、Claude 程序本体（启动时自动重装）、Kiteline 配对（设备身份不能复制）、
       SSH 主机密钥、端口、后台令牌和 VNC 密码（导入时重新生成）。
"""
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import time

FORMAT = "easycc-env"
VERSION = 1

# 相对用户目录，导出时总是跳过
ALWAYS_SKIP = [
    # agent 程序本体不打包（恢复后按 ~/.config/easycc/agents.json 自动重装），只带它们的登录和设置
    ".local/bin", ".local/lib/node_modules", ".local/share/claude", ".local/share/uv", ".local/share/cursor-agent",
    ".cache", ".local/state", ".vnc", ".npm", ".node-gyp",
    ".config/kiteline", ".Xauthority", ".dbus", ".ICEauthority", ".xsession-errors",
]
FIREFOX_SKIP = re.compile(r"^(\.mozilla|\.config/mozilla)/firefox/[^/]+/(cache2|startupCache|crashes|minidumps|datareporting|saved-telemetry-pings|storage/temporary)(/|$)")
# 「不含登录数据」时再跳过这些
LOGIN_PATHS = [".mozilla", ".config/mozilla", ".claude", ".claude.json", ".claude.json.backup", ".config/claude"]


def _skip(rel, with_login):
    for p in ALWAYS_SKIP + ([] if with_login else LOGIN_PATHS):
        if rel == p or rel.startswith(p + "/"):
            return True
    return bool(FIREFOX_SKIP.match(rel))


def export_env(info, conf, home, out_path, with_login=True, with_password=True, proxy_url=""):
    """info：客户端里的环境信息（env_info）；conf：config.env 内容；home：实例的 home 目录"""
    proxy = proxy_url or conf.get("PROXY_URL", "")
    if not with_password:
        proxy = re.sub(r"^([a-z0-9]+://[^:@/]+):[^@/]*@", r"\1@", proxy)
    manifest = {
        "format": FORMAT, "version": VERSION, "exportedAt": int(time.time()),
        "name": info["name"], "kind": info["kind"], "group": info["group"], "note": info["note"],
        "proxy": proxy, "proxyHasPassword": with_password and info["proxy"]["hasPass"],
        "path": info["path"], "root": info["root"], "project": info["project"],
        "settings": {"expectIP": info["expectIP"], "tzOverride": info["tzOverride"], "acceptLanguage": info["acceptLanguage"],
                     "screen": info["screen"], "cpus": info["cpus"], "memory": info["memory"], "autostartBrowser": info["autostartBrowser"]},
        "exit": info.get("exit") or {}, "withLogin": with_login,
    }
    files = size = 0
    with tarfile.open(out_path, "w:gz", compresslevel=6) as tar:
        data = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
        ti = tarfile.TarInfo("cbox-export.json")
        ti.size, ti.mtime, ti.mode = len(data), int(time.time()), 0o600
        tar.addfile(ti, io.BytesIO(data))
        for dirpath, dirnames, filenames in os.walk(home):
            rel_dir = os.path.relpath(dirpath, home)
            rel_dir = "" if rel_dir == "." else rel_dir
            dirnames[:] = [d for d in dirnames if not _skip(os.path.join(rel_dir, d) if rel_dir else d, with_login)]
            for fn in filenames:
                rel = os.path.join(rel_dir, fn) if rel_dir else fn
                full = os.path.join(dirpath, fn)
                # 只要普通文件；浏览器的锁文件、套接字、符号链接不要
                if _skip(rel, with_login) or os.path.islink(full) or not os.path.isfile(full) or fn in ("lock", ".parentlock", "parent.lock"):
                    continue
                try:
                    tar.add(full, arcname="home/" + rel, recursive=False)
                    files += 1
                    size += os.path.getsize(full)
                except OSError:
                    continue
    os.chmod(out_path, 0o600)
    return {"files": files, "bytes": size, "archive": os.path.getsize(out_path)}


def read_manifest(path):
    try:
        with tarfile.open(path, "r:gz") as tar:
            m = tar.getmember("cbox-export.json")
            data = json.load(tar.extractfile(m))
    except (tarfile.TarError, KeyError, ValueError, OSError):
        raise ValueError("不是 easycc 导出的环境文件")
    if data.get("format") not in (FORMAT, "claude-box-env"):     # 兼容改名前导出的文件
        raise ValueError("不是 easycc 导出的环境文件")
    if int(data.get("version", 0)) > VERSION:
        raise ValueError("这个文件来自更新版本的客户端，请先升级")
    with tarfile.open(path, "r:gz") as tar:
        names = [m for m in tar.getmembers() if m.name.startswith("home/") and m.isfile()]
    data["homeFiles"] = len(names)
    data["homeBytes"] = sum(m.size for m in names)
    return data


def extract_home(path, home):
    """把包里的 home/ 解到实例的 home 目录。只解普通文件和目录，拒绝绝对路径、..、链接、设备文件"""
    os.makedirs(home, exist_ok=True)
    base = os.path.realpath(home)
    n = 0
    with tarfile.open(path, "r:gz") as tar:
        for m in tar:
            if not m.name.startswith("home/"):
                continue
            rel = m.name[5:]
            if not rel or rel.startswith("/") or ".." in rel.split("/"):
                continue
            target = os.path.realpath(os.path.join(base, rel))
            if not target.startswith(base + os.sep):
                continue
            if m.isdir():
                os.makedirs(target, exist_ok=True)
            elif m.isfile():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tar.extractfile(m)
                with open(target, "wb") as f:
                    shutil.copyfileobj(src, f)
                os.chmod(target, (m.mode & 0o700) | 0o600)
                os.utime(target, (m.mtime, m.mtime))
                n += 1
    return n


def spool_upload(rfile, length, limit=4 << 30):
    """把上传的文件流写进临时文件（不整个读进内存）"""
    if length <= 0:
        raise ValueError("没有收到文件")
    if length > limit:
        raise ValueError("文件太大（超过 4 GB）")
    fd, path = tempfile.mkstemp(prefix="cbox-import-", suffix=".cbox")
    with os.fdopen(fd, "wb") as f:
        left = length
        while left > 0:
            chunk = rfile.read(min(1 << 20, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    os.chmod(path, 0o600)
    return path
