"""easycc 客户端：本机管理服务（类似指纹浏览器客户端的后端）。
只监听 127.0.0.1，只用 Python 标准库。和 easycc 命令共用 ~/.easycc 下的数据：
  <实例>/meta.env、<实例>/conf/config.env   easycc 的原有格式
  <实例>/profile.json                         客户端附加信息（分组、备注、代理库引用、最近出口）
  proxies.json                                代理库
  manager.json                                客户端设置
启动、停止、建镜像等耗时操作调用 easycc 命令，放进任务队列，界面轮询进度。
CBOX_MANAGER_FAKE=1 时不碰容器运行时（演示 / 测试界面用）。"""
import base64
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cbox_transfer  # noqa: E402
import cbox_cloud  # noqa: E402
try:
    VERSION = open(os.path.join(os.path.dirname(HERE), "VERSION")).read().strip()
except OSError:
    VERSION = "dev"
CLI = os.path.join(os.path.dirname(HERE), "easycc")
BASE = os.environ.get("EASYCC_HOME") or os.environ.get("CLAUDE_BOX_HOME") or os.path.expanduser("~/.easycc")
# 改名前的数据目录（~/.claude-box）自动搬过来
if not (os.environ.get("EASYCC_HOME") or os.environ.get("CLAUDE_BOX_HOME")) and not os.path.exists(BASE) \
        and os.path.isdir(os.path.expanduser("~/.claude-box")):
    os.rename(os.path.expanduser("~/.claude-box"), BASE)
FAKE = os.environ.get("CBOX_MANAGER_FAKE") == "1"
IS_MAC = platform.system() == "Darwin"
IS_WSL = os.environ.get("EASYCC_WSL") == "1"     # Windows 版：客户端跑在 easyCC 的 WSL 发行版里


def host_path(p):
    """Windows 版：界面里填的 Windows 路径（C:\\代码\\项目）换成 WSL 里的路径（/mnt/c/代码/项目）"""
    p = str(p or "").strip()
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if IS_WSL and m:
        return "/mnt/%s/%s" % (m.group(1).lower(), m.group(2).replace("\\", "/"))
    return p
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


# ---------------- 文件工具 ----------------

def read_env(path):
    """读 KEY='value' / KEY=value 格式（兼容 shell 的 '\\'' 转义）"""
    out = {}
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return out
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        out[m.group(1)] = unquote_shell(m.group(2).strip())
    return out


def unquote_shell(v):
    """'a'\\''b' → a'b；"x" → x；裸值原样（去掉行尾注释）"""
    if not v or v[0] not in "'\"":
        return v.split(" #", 1)[0].strip()
    out, i = [], 0
    while i < len(v):
        c = v[i]
        if c == "'":
            j = v.find("'", i + 1)
            if j < 0:
                out.append(v[i + 1:])
                break
            out.append(v[i + 1:j])
            i = j + 1
        elif c == '"':
            j = v.find('"', i + 1)
            out.append(v[i + 1:j if j >= 0 else len(v)])
            i = (j + 1) if j >= 0 else len(v)
        elif c == "\\" and i + 1 < len(v):
            out.append(v[i + 1])
            i += 2
        elif c.isspace():
            break
        else:
            out.append(c)
            i += 1
    return "".join(out)


def shq(v):
    return "'" + str(v).replace("'", "'\\''") + "'"


def update_env(path, updates, mode=0o600):
    """按键改写 env 文件，其它行（注释等）原样保留"""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        lines = []
    done = set()
    out = []
    for line in lines:
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and m.group(1) in updates:
            if m.group(1) not in done:
                out.append("%s=%s" % (m.group(1), shq(updates[m.group(1)])))
                done.add(m.group(1))
            continue
        out.append(line)
    for k, v in updates.items():
        if k not in done:
            out.append("%s=%s" % (k, shq(v)))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


_write_lock = threading.RLock()     # 设置、profile 是「读-改-写」：多个线程同时改会互相覆盖


def write_json(path, data):
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())   # 临时文件各用各的，同时写不会撞
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ---------------- 设置 ----------------

def settings():
    s = read_json(os.path.join(BASE, "manager.json"), {})
    s.setdefault("runtime", "auto")
    s.setdefault("arch", read_env(os.path.join(BASE, "settings.env")).get("ARCH", "amd64"))
    if not s.get("token"):
        s["token"] = secrets.token_urlsafe(24)
        os.makedirs(BASE, exist_ok=True)
        write_json(os.path.join(BASE, "manager.json"), s)
    return s


def save_settings(updates):
    with _write_lock:
        s = settings()
        s.update(updates)
        write_json(os.path.join(BASE, "manager.json"), s)
    if "arch" in updates:
        update_env(os.path.join(BASE, "settings.env"), {"ARCH": updates["arch"]}, 0o600)
    return s


APP_RES = os.path.abspath(os.path.join(HERE, "..", ".."))          # easyCC.app/Contents/Resources（从 App 里运行时）
VM_DIR = os.path.join(BASE, "vm")


def app_vm():
    """App 自带的虚拟机（macOS 12–25 / Intel 用）"""
    for d in (os.path.join(APP_RES, "vm"), "/Applications/easyCC.app/Contents/Resources/vm"):
        if os.access(os.path.join(d, "easycc-vm"), os.X_OK):
            return d
    return ""


def mac_major():
    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except (ValueError, IndexError):
        return 0


def runtime():
    """container（macOS 26 + Apple 芯片）/ vm（更早的 macOS，App 自带虚拟机）/ docker / podman"""
    s = settings().get("runtime", "auto")
    if s == "vm":
        return "vm" if app_vm() else ""
    if s != "auto":
        return s if shutil.which(s) else ""
    if IS_MAC and platform.machine() == "arm64" and mac_major() >= 26 and shutil.which("container"):
        return "container"
    if IS_MAC and app_vm():
        return "vm"
    for r in ("docker", "podman"):
        if shutil.which(r):
            return r
    return ""


def rt_bin(rt=None):
    """真正要执行的命令：vm 运行时用 App 自带的 docker 命令，连虚拟机的 docker.sock"""
    rt = rt or runtime()
    if rt == "vm":
        os.environ["DOCKER_HOST"] = "unix://" + os.path.join(VM_DIR, "docker.sock")
        b = os.path.join(APP_RES, "bin", "docker")
        return b if os.access(b, os.X_OK) else "docker"
    return rt


def _vm_data_id():
    try:
        st = os.stat(os.path.join(VM_DIR, "data.img"))
        return "%d-%d" % (st.st_ino, int(st.st_birthtime if hasattr(st, "st_birthtime") else st.st_ctime))
    except OSError:
        return ""


def vm_image_marked():
    try:
        return open(os.path.join(VM_DIR, "image-loaded")).read().strip() == _vm_data_id() != ""
    except OSError:
        return False


def vm_ensure_image(job):
    """vm 运行时启动环境前：虚拟机开起来、镜像在不在；不在就按设置里的地址下载载入（比在虚拟机里现构建快得多）"""
    if not vm_up():
        _emit(job, "▸ 启动 easyCC 虚拟机")
        r = subprocess.run(["bash", CLI, "vm", "start"], capture_output=True, text=True, env=cli_env(), timeout=180)
        if r.returncode != 0:
            _emit(job, (r.stdout + r.stderr).strip()[-300:])
            return False
    if subprocess.run([rt_bin("vm"), "image", "inspect", "easycc:latest"], capture_output=True, timeout=30).returncode == 0:
        return True
    if not settings().get("imageURL"):
        return True          # 没配下载地址：交给 easycc 在虚拟机里构建
    _emit(job, "▸ 虚拟机里还没有环境镜像，先下载")
    return _pull_image(job) == 0


def vm_up():
    return os.path.exists(os.path.join(VM_DIR, "docker.sock")) and os.path.exists(os.path.join(VM_DIR, "vm.pid"))


def cli_env():
    env = dict(os.environ)
    rt = runtime()
    if rt:
        env["EASYCC_RUNTIME"] = rt
    env["EASYCC_ARCH"] = settings().get("arch", "amd64")
    env["EASYCC_HOME"] = BASE
    return env


# ---------------- 运行状态 ----------------

_fake_running = set()
_state_cache = {"t": 0, "names": set(), "error": ""}


class _VMDown(Exception):
    pass


def running_names():
    if FAKE:
        return set(_fake_running), ""
    if time.time() - _state_cache["t"] < 2:
        return _state_cache["names"], _state_cache["error"]
    rt = runtime()
    names, err = set(), ""
    if not rt:
        err = ("没找到容器运行时（Docker / OrbStack / Podman / Apple container）" if IS_MAC else
               "没找到容器运行时（Podman 或 Docker）" if platform.system() == "Linux" else "没找到容器运行时")
    else:
        cmd = [rt, "ls", "-q"] if rt == "container" else [rt_bin(rt), "ps", "--format", "{{.Names}}"]
        try:
            if rt == "vm" and not vm_up():
                raise _VMDown()
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            if p.returncode == 0:
                names = {n.strip() for n in p.stdout.splitlines() if n.strip()}
            else:
                err = (p.stderr or "").strip().splitlines()[-1:] and (p.stderr or "").strip().splitlines()[-1] or "%s 没在运行" % rt
        except _VMDown:
            pass                       # 虚拟机没开：没有环境在运行，启动环境时会自动开
        except (OSError, subprocess.TimeoutExpired) as exc:
            err = "%s 不可用：%s" % (rt, exc)
    _state_cache.update(t=time.time(), names=names, error=err)
    return names, err


def rt_exec(name, args, timeout=150):
    """在容器里执行命令（root）"""
    if FAKE:
        time.sleep(1)
        return 0, "OK"
    rt = runtime()
    try:
        p = subprocess.run([rt_bin(rt), "exec", "easycc-" + name] + args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


# ---------------- 环境 ----------------

def inst_dir(name):
    return os.path.join(BASE, name)


def env_names():
    try:
        return sorted(n for n in os.listdir(BASE) if os.path.isfile(os.path.join(BASE, n, "meta.env")))
    except OSError:
        return []


def parse_proxy(url):
    u = urlsplit(url or "")
    try:
        port = u.port or 1080
    except ValueError:
        port = 1080
    t = (u.scheme or "socks5").lower()
    return {"type": "http" if t in ("http", "https") else "socks5", "host": u.hostname or "", "port": port,
            "user": unquote(u.username or ""), "pass": unquote(u.password or "")}


def build_proxy(d):
    auth = ""
    if d.get("user"):
        auth = quote(d["user"], safe="") + (":" + quote(d.get("pass", ""), safe="") if d.get("pass") else "") + "@"
    return "%s://%s%s:%s" % (d.get("type") or "socks5", auth, d["host"], int(d["port"]))


def env_info(name, running=None):
    d = inst_dir(name)
    meta = read_env(os.path.join(d, "meta.env"))
    conf = read_env(os.path.join(d, "conf", "config.env"))
    prof = read_json(os.path.join(d, "profile.json"), {})
    if running is None:
        running = ("easycc-" + name) in running_names()[0]
    p = parse_proxy(conf.get("PROXY_URL", ""))
    root = meta.get("ROOT_DIR") or meta.get("DIR", "")
    project = conf.get("PROJECT", "")
    job = active_job(name)
    return {
        "name": name, "kind": prof.get("kind", "dev"), "group": prof.get("group", ""), "note": prof.get("note", ""), "color": prof.get("color", ""),
        "createdAt": prof.get("createdAt") or int(os.path.getmtime(os.path.join(d, "meta.env"))),
        "lastOpened": prof.get("lastOpened", 0), "proxyId": prof.get("proxyId", ""),
        "proxy": {"type": p["type"], "host": p["host"], "port": p["port"], "user": p["user"], "hasPass": bool(p["pass"])},
        "exit": prof.get("exit") or {},
        "root": root, "project": project, "path": root.rstrip("/") + ("/" + project if project else ""),
        "pathShort": short_path(root.rstrip("/") + ("/" + project if project else "")),
        "ports": {"admin": meta.get("ADMIN_PORT", ""), "vnc": meta.get("PORT", ""), "ssh": meta.get("SSH_PORT", "")},
        "cpus": meta.get("CPUS", ""), "memory": meta.get("MEMORY", ""), "screen": conf.get("SCREEN", ""),
        "expectIP": conf.get("EXPECT_IP", ""), "tzOverride": conf.get("TZ_OVERRIDE", ""), "acceptLanguage": conf.get("ACCEPT_LANGUAGE", ""),
        "autostartBrowser": conf.get("AUTOSTART_BROWSER", "1") != "0",
        "browser": conf.get("BROWSER", "firefox") or "firefox",
        "status": ("busy" if job else "running" if running else "stopped"), "job": job,
        "sleeping": (not running) and os.path.exists(sleeping_flag(name)), "keepAwake": bool(prof.get("keepAwake")),
        "tabs": browser_tabs(name),
        "kiteline": _kl_cache.get(name) if running else None,
        "gate": _gate_cache.get(name) if running else None, "exitRule": conf.get("EXIT_RULE", "") or "country",
        "relay": re.sub(r"(//[^:/@]*:)[^@]*@", r"\1***@", conf.get("RELAY_URL", "")),
        "cloud": {"name": prof.get("cloudName", ""), "version": prof.get("cloudVersion", 0), "syncedAt": prof.get("cloudSyncedAt", 0)},
        "forwardLocalhost": conf.get("FORWARD_LOCALHOST", "0") == "1",
        "hostAccess": conf.get("HOST_ACCESS", "0") == "1", "hostSsh": conf.get("HOST_SSH", "0") == "1",
        "forwards": [p for p in forwarded_ports(name, running) if p.get("host")] if running else [],
        "adminURL": ("http://127.0.0.1:%s/login?k=%s" % (meta.get("ADMIN_PORT"), conf.get("ADMIN_TOKEN", ""))) if meta.get("ADMIN_PORT") else "",
        "desktopURL": ("http://127.0.0.1:%s/easycc.html?name=%s&password=%s" % (meta.get("PORT"), quote(name), quote(conf.get("VNC_PASSWORD", "")))) if meta.get("PORT") else "",
    }


def short_path(p):
    """主目录换成 ~，太长只留最后三段"""
    home = os.path.expanduser("~")
    if p == home or p.startswith(home + "/"):
        p = "~" + p[len(home):]
    parts = p.split("/")
    return p if len(parts) <= 4 else "…/" + "/".join(parts[-3:])


_fwd_restart = {}
INTERNAL_PORTS = {22, 53, 1053, 5901, 6080, 7681, 7682, 7890, 7892, 9090} | set(range(8765, 8771))


def parse_forward_list(v):
    out = {}
    for item in str(v or "").replace(" ", "").split(","):
        a, _, b = item.partition(":")
        if a.isdigit() and 0 < int(a) < 65536:
            out[int(a)] = int(b) if b.isdigit() and 0 < int(b) < 65536 else int(a)
    return out


def ports_info(name):
    """「端口」面板：已转发的（含没服务在监听的）+ 环境里检测到但没转发的"""
    d = inst_dir(name)
    conf = read_env(os.path.join(d, "conf", "config.env"))
    manual = parse_forward_list(conf.get("FORWARD_PORTS", ""))
    running = ("easycc-" + name) in running_names()[0]
    if FAKE:
        st = {"ok": True, "error": "", "at": time.time(), "ports": [], "detected": [{"port": 5173, "proc": "vite"}, {"port": 9229, "proc": "node"}]}
        for ep, hp in sorted(manual.items()):
            st["ports"].append({"env": ep, "host": hp if hp != 3000 else 3001, "wantHost": hp, "proc": "node" if ep == 3000 else "",
                                "listening": ep == 3000, "manual": True})
    else:
        st = read_json(os.path.join(d, "ports.json"), {}) or {}
        if running and time.time() - st.get("at", 0) > 20:
            forwarded_ports(name, running)   # 转发进程不在了：拉起来
    return {"running": running, "auto": conf.get("FORWARD_LOCALHOST", "0") == "1", "ok": st.get("ok", False), "error": st.get("error", ""),
            "ports": st.get("ports") or [{"env": ep, "host": None, "wantHost": hp, "proc": "", "listening": False, "manual": True}
                                         for ep, hp in sorted(manual.items())],
            "detected": st.get("detected", [])}


def ports_change(name, b):
    d = inst_dir(name)
    conf_path = os.path.join(d, "conf", "config.env")
    manual = parse_forward_list(read_env(conf_path).get("FORWARD_PORTS", ""))
    try:
        port = int(b.get("port") or 0)
        host = int(b.get("hostPort") or 0) or port
    except (TypeError, ValueError):
        return {"ok": False, "message": "端口要填数字"}
    if not 0 < port < 65536 or not 0 < host < 65536:
        return {"ok": False, "message": "端口要在 1–65535 之间"}
    if b.get("action") == "add":
        if port in INTERNAL_PORTS:
            return {"ok": False, "message": "%d 是环境内部用的端口（代理、远程桌面、后台、Kiteline 等），不能转发" % port}
        manual[port] = host
        msg = "已添加：环境 %d → 电脑 localhost:%d（几秒内生效，被占用会顺延）" % (port, host)
    elif b.get("action") == "remove":
        manual.pop(port, None)
        msg = "已停止转发 %d" % port
    else:
        return {"ok": False, "message": "未知操作"}
    update_env(conf_path, {"FORWARD_PORTS": ",".join(("%d" % e) if h == e else ("%d:%d" % (e, h)) for e, h in sorted(manual.items()))})
    return {"ok": True, "message": msg}


def forwarded_ports(name, running):
    """环境里转到电脑 localhost 的端口；运行中但转发进程不在了（App 重开、电脑睡眠后）就重新拉起"""
    st = read_json(os.path.join(inst_dir(name), "ports.json"), None)
    alive = bool(st) and time.time() - st.get("at", 0) < 20
    if running and not alive and not FAKE and time.time() - _fwd_restart.get(name, 0) > 30:
        _fwd_restart[name] = time.time()
        subprocess.Popen(["bash", CLI, "portfwd", name], env=cli_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    if FAKE and running:
        return [p for p in ports_info(name)["ports"]]
    return (st or {}).get("ports", []) if alive else []


def list_envs():
    names, err = running_names()
    envs = [env_info(n, ("easycc-" + n) in names) for n in env_names()]
    return {"envs": envs, "usage": usage_info(envs), "runtime": "演示模式" if FAKE else runtime(), "runtimeError": err,
            "groups": sorted({read_json(os.path.join(inst_dir(n), "profile.json"), {}).get("group", "") for n in env_names()} - {""})}


# ---------------- 环境浏览器的标签页数 ----------------
# 环境的 home 目录挂在电脑上，直接读 Firefox 的会话文件（mozlz4 = 魔数 + LZ4 块），不用进容器
_tabs_cache = {}


def _lz4_block(src, size):
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        tok = src[i]
        i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if off >= ml:
            dst += dst[start:start + ml]
        else:
            for k in range(ml):
                dst.append(dst[start + k])
        if len(dst) > size:
            break
    return bytes(dst)


def browser_tabs(name):
    """环境浏览器里开着的标签页数（下次打开会恢复的那些）；读不到返回 None"""
    import glob
    home = os.path.join(inst_dir(name), "home")
    files = []
    for base in (".config/mozilla/firefox", ".mozilla/firefox"):
        for sub in ("sessionstore-backups/recovery.jsonlz4", "sessionstore.jsonlz4"):
            files += glob.glob(os.path.join(home, base, "*", sub))
    if not files:
        return None
    f = max(files, key=lambda x: os.path.getmtime(x))
    mt = os.path.getmtime(f)
    c = _tabs_cache.get(name)
    if c and c[0] == f and c[1] == mt:
        return c[2]
    try:
        raw = open(f, "rb").read()
        if raw[:8] != b"mozLz40\0":
            return None
        size = int.from_bytes(raw[8:12], "little")
        data = json.loads(_lz4_block(raw[12:], size)[:size])
        n = sum(len(w.get("tabs") or []) for w in data.get("windows") or [])
    except (OSError, ValueError, IndexError, KeyError):
        return None
    _tabs_cache[name] = (f, mt, n)
    return n


# ---------------- 云端账号 ----------------
CLOUD = None


def cloud():
    global CLOUD
    if CLOUD is None:
        CLOUD = cbox_cloud.Cloud(BASE, VERSION)
    return CLOUD


def cloud_info():
    c = cloud()
    st = c.state()
    return {"url": c.url(), "loggedIn": c.logged_in(), "email": st.get("email", ""), "user": st.get("user") if c.logged_in() else None,
            "deviceId": st.get("deviceId", ""), "reportedAt": st.get("reportedAt", 0), "defaultURL": cbox_cloud.DEFAULT_URL}


def cloud_call(fn, *a):
    try:
        r = fn(*a)
        return dict(r, ok=True) if isinstance(r, dict) else {"ok": True, "data": r}
    except cbox_cloud.CloudError as e:
        return {"ok": False, "message": str(e), "status": e.status, "data": e.data}


def cloud_envs():
    r = cloud_call(cloud().list_envs)
    if not r.get("ok"):
        return r
    local = {}
    for n in env_names():
        prof = read_json(os.path.join(inst_dir(n), "profile.json"), {})
        k = prof.get("cloudName") or n
        cur = {"name": n, "version": prof.get("cloudVersion", 0), "syncedAt": prof.get("cloudSyncedAt", 0)}
        if k not in local or cur["version"] > local[k]["version"]:
            local[k] = cur
    for e in r["envs"]:
        e["local"] = local.get(e["name"])
    return r


def env_cloud_meta(info):
    ex = info.get("exit") or {}
    return {"kind": info["kind"], "group": info["group"], "note": info["note"], "exitIP": ex.get("ip", ""), "country": ex.get("country", ""),
            "location": ex.get("location", ""), "proxyType": info["proxy"]["type"], "proxyHost": info["proxy"]["host"],
            "cpus": info["cpus"], "memory": info["memory"], "device": cbox_cloud.computer_name()}


def _cloud_push(job):
    name, force = job["env"], job["arg"] == "force"
    c = cloud()
    if not c.logged_in():
        _emit(job, "还没登录 easyCC 账号")
        return 1
    info = env_info(name)
    conf = read_env(os.path.join(inst_dir(name), "conf", "config.env"))
    prof = read_json(os.path.join(inst_dir(name), "profile.json"), {})
    cname = prof.get("cloudName") or name
    if info["status"] == "running":
        _emit(job, "▸ 环境正在运行：浏览器正在写的数据可能不完整（最好停止后再同步）")
    _emit(job, "▸ 打包环境（配置、浏览器登录状态、agent 登录）")
    fd, tmp = tempfile.mkstemp(prefix="easycc-push-", suffix=".cbox")
    os.close(fd)
    try:
        r = cbox_transfer.export_env(info, conf, os.path.join(inst_dir(name), "home"), tmp, with_login=True, with_password=True)
        _emit(job, "  %d 个文件，压缩后 %s" % (r["files"], _mb(r["archive"])))
        _emit(job, "▸ 上传到云端 %s" % c.url())
        try:
            res = c.upload(cname, tmp, env_cloud_meta(info), None if force else (prof.get("cloudVersion") or None))
        except cbox_cloud.CloudError as e:
            if e.status == 409:
                cur = (e.data or {}).get("current") or {}
                _emit(job, "✗ 云端有别的电脑刚同步的新版本 v%s（%s），为避免覆盖已停止。要覆盖请在菜单里选「强制同步」，或先从云端恢复。" % (
                    cur.get("version", "?"), (cur.get("meta") or {}).get("device", "")))
            else:
                _emit(job, "✗ %s" % e)
            return 1
        env = res["env"]
        save_profile(name, {"cloudName": cname, "cloudVersion": env["version"], "cloudSyncedAt": int(time.time())})
        _emit(job, "✓ 已同步到云端：%s v%d%s" % (cname, env["version"], "（内容没变）" if res.get("unchanged") else ""))
        threading.Thread(target=cloud_report, daemon=True).start()
        return 0
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def cloud_fetch(cname):
    """从云端下载环境包，返回和「导入」一样的预览信息（前端用同一个导入窗口确认名称、项目目录）"""
    try:
        path, ver = cloud().download(cname)
    except cbox_cloud.CloudError as e:
        return {"ok": False, "message": str(e)}
    r = import_inspect(path)
    if r.get("ok"):
        r.update(cloudName=cname, cloudVersion=ver)
        _imports[r["tmp"]] = _imports[r["tmp"]][:2] + (time.time() + 1800,)
        _cloud_pending[r["tmp"]] = (cname, ver)
    return r


_cloud_pending = {}


def cloud_report():
    c = cloud()
    if not c.logged_in():
        return {"ok": False}
    o, v = cbox_cloud.os_info()
    try:
        du = shutil.disk_usage(BASE).free
    except OSError:
        du = 0
    dev = {"name": cbox_cloud.computer_name(), "os": o, "osVersion": v, "arch": platform.machine(), "version": VERSION,
           "runtime": "演示模式" if FAKE else runtime(), "cpus": os.cpu_count(), "memory": _host_mem(), "diskFree": du}
    envs = []
    data = list_envs()
    for e in data["envs"]:
        u = e.get("usage") or {}
        ex = e.get("exit") or {}
        prof = read_json(os.path.join(inst_dir(e["name"]), "profile.json"), {})
        envs.append({"name": e["name"], "kind": e["kind"], "group": e["group"], "note": e["note"], "status": e["status"],
                     "exitIP": ex.get("ip", ""), "country": ex.get("country", ""), "location": ex.get("location", ""),
                     "proxyType": e["proxy"]["type"], "proxyHost": e["proxy"]["host"], "projectName": os.path.basename(e["path"].rstrip("/")),
                     "cpus": e["cpus"], "memory": e["memory"], "agents": cbox_cloud.installed_agents(os.path.join(inst_dir(e["name"]), "home")),
                     "tabs": e.get("tabs"), "forwards": len(e.get("forwards") or []), "hostAccess": e["hostAccess"],
                     "createdAt": e["createdAt"], "lastOpened": e["lastOpened"], "cpu": u.get("cpu"), "memUsed": u.get("mem"), "disk": u.get("disk"),
                     "cloudVersion": prof.get("cloudVersion", 0)})
    try:
        return dict(c.report(dev, envs), ok=True)
    except cbox_cloud.CloudError as e:
        return {"ok": False, "message": str(e)}


# 匿名使用统计：不登录也上报装机量和用量概况，开源项目靠它知道有多少人在用、哪些功能常用。
# 只发一个随机设备号（和电脑、账号都没关联）+ 系统和资源概况；不发环境名、备注、项目、代理地址、出口 IP。设置里可关。
TELEMETRY_ENV_KEYS = ("kind", "status", "country", "proxyType", "cpus", "memory", "agents", "tabs", "createdAt", "lastOpened", "cpu", "memUsed", "disk")


def device_id():
    p = os.path.join(BASE, "device.json")
    d = read_json(p, {})
    if not re.match(r"^[0-9a-f]{32}$", str(d.get("id", ""))):
        d = {"id": secrets.token_hex(16), "createdAt": int(time.time())}
        write_json(p, d)
    return d["id"]


def telemetry_report():
    if FAKE or settings().get("telemetry", True) is False:
        return {"ok": False}
    did = device_id()
    o, v = cbox_cloud.os_info()
    dev = {"os": o, "osVersion": v, "arch": platform.machine(), "version": VERSION, "runtime": runtime(),
           "cpus": os.cpu_count(), "memory": _host_mem(), "lang": (os.environ.get("LANG") or "")[:20]}
    envs = []
    for e in list_envs()["envs"]:
        u = e.get("usage") or {}
        row = {"id": hashlib.sha256((did + e["name"]).encode()).hexdigest()[:16], "kind": e["kind"], "status": e["status"],
               "country": (e.get("exit") or {}).get("country", ""), "proxyType": e["proxy"]["type"], "cpus": e["cpus"], "memory": e["memory"],
               "agents": cbox_cloud.installed_agents(os.path.join(inst_dir(e["name"]), "home")), "tabs": e.get("tabs"),
               "createdAt": e["createdAt"], "lastOpened": e["lastOpened"], "cpu": u.get("cpu"), "memUsed": u.get("mem"), "disk": u.get("disk")}
        envs.append({k: row[k] for k in ("id",) + TELEMETRY_ENV_KEYS})
    try:
        return dict(cloud().call("POST", "/v1/telemetry", {"deviceId": did, "device": dev, "envs": envs}, timeout=20), ok=True)
    except cbox_cloud.CloudError as e:
        return {"ok": False, "message": str(e)}


def _cloud_loop():
    time.sleep(20)
    while True:
        for f in (cloud_report, telemetry_report):
            try:
                f()
            except Exception:  # noqa: BLE001  上报失败不影响客户端
                pass
        time.sleep(600)


# ---------------- agent 安装加速 ----------------
# 环境里点「安装」时，在用户目录写一个请求（~/.cache/easycc/pkg-requests/<id>.json）；这里由电脑本机从国内镜像下载
# npm 程序包（含对应平台的二进制包），校验后放到 ~/.cache/easycc/pkgs/<id>/，环境里加进 npm 缓存再安装。
# 只是电脑本机下载公开的软件包，环境自己的流量照样全部走代理。
PKG_MIRROR = "https://registry.npmmirror.com"
_pkg_busy = set()


def _npm_json(name, ver="latest"):
    import urllib.request
    req = urllib.request.Request("%s/%s/%s" % (PKG_MIRROR, name, ver), headers={"User-Agent": "easyCC/" + VERSION, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _pkg_fetch(home, aid, spec, arch):
    import base64
    import hashlib
    import urllib.request
    out = os.path.join(home, ".cache", "easycc", "pkgs", aid)
    try:
        shutil.rmtree(out, ignore_errors=True)
        os.makedirs(out)
        main = _npm_json(spec)
        want = [(spec, main)]
        plat = "linux-x64" if arch == "amd64" else "linux-arm64"
        for dep, ver in (main.get("optionalDependencies") or {}).items():
            if plat in dep and "musl" not in dep:
                if ver.startswith("npm:"):          # 别名：npm:@openai/codex@0.167.0-linux-x64
                    dep, _, ver = ver[4:].rpartition("@")
                want.append((dep, _npm_json(dep, ver)))
        for name, meta in want:
            dist = meta["dist"]
            url = dist["tarball"].replace("https://registry.npmjs.org", PKG_MIRROR)
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "easyCC/" + VERSION}), timeout=60) as r:
                total, buf, last = int(r.headers.get("Content-Length") or 0), [], 0
                for chunk in iter(lambda: r.read(1 << 18), b""):
                    buf.append(chunk)
                    if time.time() - last > 2:      # 进度给环境里的安装脚本看：有进度它就一直等
                        got = sum(len(c) for c in buf)
                        open(os.path.join(out, ".progress"), "w").write("%s %d %d" % (name, got, total))
                        last = time.time()
                data = b"".join(buf)
            algo, _, b64 = (dist.get("integrity") or "").partition("-")
            if algo in ("sha512", "sha256", "sha1") and base64.b64encode(hashlib.new(algo, data).digest()).decode() != b64:
                raise ValueError("%s 校验不对" % name)
            fn = os.path.join(out, "%s-%s.tgz" % (name.replace("@", "").replace("/", "-"), meta["version"]))
            with open(fn, "wb") as f:
                f.write(data)
        open(os.path.join(out, ".done"), "w").write(str(int(time.time())))
    except Exception as e:  # noqa: BLE001
        os.makedirs(out, exist_ok=True)
        open(os.path.join(out, ".error"), "w").write(str(e)[:200])
    finally:
        _pkg_busy.discard((home, aid))


def _pkg_loop():
    import glob
    while True:
        try:
            for req in glob.glob(os.path.join(BASE, "*", "home", ".cache", "easycc", "pkg-requests", "*.json")):
                home = req.split("/.cache/easycc/")[0]
                aid = os.path.basename(req)[:-5]
                if (home, aid) in _pkg_busy or os.path.exists(os.path.join(home, ".cache", "easycc", "pkgs", aid, ".done")):
                    continue
                try:
                    spec = json.load(open(req)).get("npm", "")
                except (OSError, ValueError):
                    continue
                if not re.fullmatch(r"(@[a-z0-9_.-]+/)?[a-z0-9_.-]+", spec or ""):
                    continue
                _pkg_busy.add((home, aid))
                threading.Thread(target=_pkg_fetch, args=(home, aid, spec, settings().get("arch", "amd64")), daemon=True).start()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)


# ---------------- Kiteline（经环境后台接口） ----------------
_kl_cache = {}


def env_admin(name, path, post=False, timeout=15):
    """调环境后台（容器里的 admin.py）的接口"""
    import urllib.request
    meta = read_env(os.path.join(inst_dir(name), "meta.env"))
    conf = read_env(os.path.join(inst_dir(name), "conf", "config.env"))
    req = urllib.request.Request("http://127.0.0.1:%s%s" % (meta.get("ADMIN_PORT"), path), data=b"{}" if post else None,
                                 headers={"Cookie": "cbox_admin=" + conf.get("ADMIN_TOKEN", ""), "X-CBox": "1", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def kiteline_info(name, renew=False):
    if FAKE:
        return {"installed": True, "running": True, "code": "382915", "expiry": int(time.time()) + 300, "qr": "", "devices": [{"name": "iPhone", "online": True}], "version": "demo", "sessions": 1}
    try:
        k = env_admin(name, "/api/kiteline/renew" if renew else "/api/kiteline", post=renew)
    except Exception as e:  # noqa: BLE001
        return {"installed": False, "reason": "环境后台连不上：%s" % e}
    devs = k.get("devices") or []
    _kl_cache[name] = {"devices": len(devs), "online": sum(1 for d in devs if d.get("online")), "installed": k.get("installed", False), "at": time.time()}
    return k


_gate_cache = {}


def gate_info(name):
    """环境的联网闸门：放行 / 关闭（原因）/ 暂停；列表里显示"""
    try:
        g = env_admin(name, "/api/gate")
    except Exception:  # noqa: BLE001
        return None
    bad = next((c for c in g.get("checks") or [] if c.get("state") not in ("passed", "warning")), None)
    _gate_cache[name] = {"open": bool(g.get("open")), "paused": bool(g.get("paused")), "reason": g.get("reason", ""),
                         "title": bad.get("title", "") if bad else "", "nextIn": g.get("nextIn"), "at": time.time()}
    return _gate_cache[name]


def _kl_loop():
    time.sleep(15)
    while True:
        try:
            names, _ = running_names()
            for n in env_names():
                if ("easycc-" + n) in names and time.time() - _kl_cache.get(n, {}).get("at", 0) > 50:
                    kiteline_info(n)
                if ("easycc-" + n) in names and time.time() - _gate_cache.get(n, {}).get("at", 0) > 25:
                    gate_info(n)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(20)


# ---------------- 资源占用 ----------------
# 后台线程采集：CPU / 内存每 5 秒（容器运行时的 stats），硬盘每 60 秒（du）。只在客户端界面开着时采集。
_usage = {"stats": {}, "disk": {}, "diskAt": 0, "wanted": 0, "started": False}
_usage_lock = threading.Lock()
_SIZE_UNITS = {"b": 1, "kb": 1e3, "kib": 1024, "mb": 1e6, "mib": 1 << 20, "gb": 1e9, "gib": 1 << 30, "tb": 1e12, "tib": 1 << 40}


def _parse_size(s):
    m = re.match(r"^\s*([\d.]+)\s*([A-Za-z]*)", str(s or ""))
    if not m:
        return 0
    return int(float(m.group(1)) * _SIZE_UNITS.get(m.group(2).lower() or "b", 1))


def _alloc_bytes(v):
    m = re.fullmatch(r"(\d+)([MG])", str(v or "").strip().upper())
    return int(m.group(1)) << (30 if m.group(2) == "G" else 20) if m else 0


def _collect_stats():
    rt = runtime()
    out = {}
    if FAKE:
        import random
        for n in _fake_running:          # 里面存的已经是容器名（easycc-<环境名>）
            out[n] = {"cpu": round(random.uniform(0.02, 0.6), 3), "mem": int(random.uniform(1.2, 2.4) * (1 << 30)), "memLimit": 0, "procs": 200}
        return out
    if rt == "container":
        p = subprocess.run([rt, "stats", "--no-stream", "--format", "json"], capture_output=True, text=True, timeout=20)
        for s in json.loads(p.stdout or "[]") if p.returncode == 0 else []:
            # cpuUtilization：用掉的核数（1.0 = 一个核跑满）
            out[s.get("id", "")] = {"cpu": float(s.get("cpuUtilization") or 0), "mem": int(s.get("memoryUsageBytes") or 0),
                                    "memLimit": int(s.get("memoryLimitBytes") or 0), "procs": int(s.get("numProcesses") or 0)}
    elif rt:
        p = subprocess.run([rt_bin(rt), "stats", "--no-stream", "--format", "{{json .}}"], capture_output=True, text=True, timeout=30)
        for line in p.stdout.splitlines() if p.returncode == 0 else []:
            try:
                s = json.loads(line)
            except ValueError:
                continue
            used, _, limit = str(s.get("MemUsage", "")).partition("/")
            out[s.get("Name", "")] = {"cpu": float(str(s.get("CPUPerc", "0")).rstrip("%") or 0) / 100, "mem": _parse_size(used),
                                      "memLimit": _parse_size(limit), "procs": int(s.get("PIDs") or 0)}
    return out


def _du(paths):
    paths = [p for p in paths if p and os.path.exists(p)]
    if not paths:
        return 0
    try:
        p = subprocess.run(["du", "-sk"] + paths, capture_output=True, text=True, timeout=120)
        return sum(int(l.split()[0]) for l in p.stdout.splitlines() if l.split() and l.split()[0].isdigit()) * 1024
    except (OSError, subprocess.TimeoutExpired):
        return 0


def _container_dirs(name):
    """容器自己的系统盘（Apple container：rootfs.ext4 等，按实际占用算）"""
    cands = [os.path.expanduser("~/Library/Application Support/easyCC/container/containers"),
             os.path.expanduser("~/Library/Application Support/com.apple.container/containers")]
    return [os.path.join(c, "easycc-" + name) for c in cands]


def _collect_disk():
    rt = runtime()
    out = {}
    docker_sz = {}
    if rt in ("docker", "podman", "vm") and not FAKE and (rt != "vm" or vm_up()):
        try:
            p = subprocess.run([rt_bin(rt), "ps", "-a", "-s", "--format", "{{.Names}}\t{{.Size}}"], capture_output=True, text=True, timeout=60)
            for line in p.stdout.splitlines():
                n, _, sz = line.partition("\t")
                docker_sz[n.strip()] = _parse_size(sz)          # "12.3MB (virtual 1.2GB)"：只算这个容器自己写的
        except (OSError, subprocess.TimeoutExpired):
            pass
    for n in env_names():
        system = docker_sz.get("easycc-" + n, 0) if docker_sz else _du(_container_dirs(n))
        data = _du([inst_dir(n)])                                 # 浏览器数据、Claude 登录、配置
        out[n] = {"system": system, "data": data, "total": system + data}
    return out


def _usage_loop():
    while True:
        if time.time() - _usage["wanted"] < 30:
            try:
                st = _collect_stats() if (FAKE or running_names()[0]) else {}
            except (OSError, ValueError, subprocess.TimeoutExpired):
                st = None
            if st is not None:
                with _usage_lock:
                    _usage["stats"] = st
            if time.time() - _usage["diskAt"] > 60:
                d = _collect_disk()
                with _usage_lock:
                    _usage["disk"], _usage["diskAt"] = d, time.time()
        time.sleep(5)


# ---------------- 弹性资源 ----------------
# Docker / Podman / easyCC 虚拟机里的 Docker：运行中按负载调每个环境的 CPU 配额和内存上限（docker update，不用重启）。
#   CPU：连续 30 秒用满配额 → 每次加半份，最多到设置值的 2 倍（且不超过机器核数）；闲 2 分钟（< 设置值的 30%）→ 回到设置值。
#   内存：用量超过上限的 85% → 上限加 50%，最多到设置值的 2 倍（且不超过机器内存的 75%）；用量回落到设置值 60% 以下 5 分钟 → 回到设置值。
# 只在设置值之上伸缩，不会比用户设的少（防止环境里的程序被 OOM 杀掉）；配额只是上限，没用到的 CPU / 内存本来就在宿主机手里。
# 环境里 nproc 看到的核数不变（配额不改 CPU 亲和性），机器身份不受影响。Apple container 不支持运行中改，跳过。
# 设置 elastic=false 关掉：所有环境回到设置值。
_elastic = {}            # 环境名 → {"id", "hot", "idle", "memIdle"}
_rt_caps = {}


def _rt_capacity(rt):
    """运行时看到的总核数 / 内存（虚拟机运行时是虚拟机的，不是 Mac 的）"""
    if rt not in _rt_caps or time.time() - _rt_caps[rt]["at"] > 300:
        try:
            p = subprocess.run([rt_bin(rt), "info", "--format", "{{.NCPU}} {{.MemTotal}}"], capture_output=True, text=True, timeout=15)
            c, m = p.stdout.split()[:2]
            _rt_caps[rt] = {"cpus": int(c), "mem": int(m), "at": time.time()}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return os.cpu_count() or 2, _host_mem()
    return _rt_caps[rt]["cpus"], _rt_caps[rt]["mem"]


def _elastic_tick(check_sleep=False):
    rt = runtime()
    if FAKE or not rt or (rt == "vm" and not vm_up()):
        return
    names, _ = running_names()
    envs = [n for n in env_names() if "easycc-" + n in names]
    for n in list(_elastic):
        if n not in envs:
            _elastic.pop(n, None)
    if not envs:
        return
    if rt == "container":          # Apple container 不能运行中改配额，只做空闲休眠
        if check_sleep:
            _sleep_tick(rt, envs, _collect_stats())
        return
    on = settings().get("elastic", True) is not False
    p = subprocess.run([rt_bin(rt), "inspect", "-f", "{{.Name}} {{.Id}} {{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}"] + ["easycc-" + n for n in envs],
                       capture_output=True, text=True, timeout=20)
    cur = {}
    for line in p.stdout.splitlines():
        f = line.split()
        if len(f) == 4 and f[2].isdigit() and f[3].isdigit():
            cur[f[0].lstrip("/")] = (f[1], int(f[2]) / 1e9, int(f[3]))
    stats = _collect_stats()
    if check_sleep:
        _sleep_tick(rt, envs, stats)
        names, _ = running_names()
        envs = [n for n in envs if "easycc-" + n in names]
    host_cpus, host_mem = _rt_capacity(rt)
    for n in envs:
        c = cur.get("easycc-" + n)
        s = stats.get("easycc-" + n)
        if not c or not s:
            continue
        cid, cpu_now, mem_now = c
        meta = read_env(os.path.join(inst_dir(n), "meta.env"))
        base_cpu = float(meta.get("CPUS") or 0) or cpu_now or 2
        base_mem = _alloc_bytes(meta.get("MEMORY")) or mem_now or (4 << 30)
        st = _elastic.get(n)
        if not st or st["id"] != cid:          # 新启动的容器：计数清零
            st = _elastic[n] = {"id": cid, "hot": 0, "idle": 0, "memIdle": 0}
        cpu_new, mem_new = cpu_now or base_cpu, mem_now or base_mem
        if not on:
            cpu_new, mem_new = base_cpu, base_mem
        else:
            cap_cpu = max(base_cpu, min(base_cpu * 2, host_cpus))
            if s["cpu"] >= cpu_new * 0.9:
                st["hot"], st["idle"] = st["hot"] + 1, 0
            elif s["cpu"] < base_cpu * 0.3:
                st["hot"], st["idle"] = 0, st["idle"] + 1
            else:
                st["hot"] = st["idle"] = 0
            if st["hot"] >= 3 and cpu_new < cap_cpu:
                cpu_new, st["hot"] = min(cap_cpu, cpu_new + max(1, base_cpu / 2)), 0
            elif st["idle"] >= 12 and cpu_new > base_cpu:
                cpu_new = base_cpu
            cap_mem = max(base_mem, min(base_mem * 2, int(host_mem * 0.75)))
            if s["mem"] > mem_new * 0.85 and mem_new < cap_mem:
                mem_new, st["memIdle"] = min(cap_mem, (int(mem_new * 1.5) >> 26) << 26), 0
            elif mem_new > base_mem and s["mem"] < base_mem * 0.6:
                st["memIdle"] += 1
                if st["memIdle"] >= 30:
                    mem_new, st["memIdle"] = base_mem, 0
            else:
                st["memIdle"] = 0
        st.update(cpus=cpu_new, mem=mem_new, baseCpus=base_cpu, baseMem=base_mem)
        if abs(cpu_new - cpu_now) < 0.01 and mem_new == mem_now:
            continue
        # 交换上限跟着内存走（docker run -m 默认交换上限 = 2 倍内存；改内存时交换上限不能比它小）
        r = subprocess.run([rt_bin(rt), "update", "--cpus", "%.2f" % cpu_new, "--memory", str(mem_new), "--memory-swap", str(mem_new * 2), "easycc-" + n],
                           capture_output=True, text=True, timeout=30)
        print("弹性资源 %s：CPU %.1f → %.1f 核，内存 %d → %d MB%s" % (n, cpu_now, cpu_new, mem_now >> 20, mem_new >> 20,
              "" if r.returncode == 0 else "（失败：%s）" % (r.stderr.strip()[-200:])), flush=True)


# ---------------- 空闲休眠 ----------------
# Mac 上的运行时（Apple container、easyCC 虚拟机）都基于苹果虚拟化框架：虚拟机用过的内存不会还给 Mac（气球也不行，实测过），
# 只有整台关掉才还。所以环境闲着超过设定时间就「休眠」（停掉，磁盘、登录状态、浏览器数据都在），内存全部还给电脑；
# 下次在客户端打开、SSH / VS Code 连接（ssh_config 里的 Match exec 调 easycc wake）时自动唤醒。
# 「闲着」= 没人连着（SSH / 浏览器桌面 / 管理后台都没有连接）+ 没有 agent 在跑 + CPU 低于 0.1 核，全部持续满设定时间。
# 有 agent 进程就不休眠（哪怕在等模型回复、CPU 很低）；单个环境可在 profile 里设 keepAwake 常开。
# 虚拟机运行时：所有环境都停了 10 分钟后，虚拟机本身也关掉。设置 autoSleep=0 关闭。
_IDLE_PROBE = r'''command -v ss >/dev/null || { echo busy; exit 0; }
c=$(ss -Htn state established '( sport = :22 or sport = :6080 or sport = :5901 or sport = :7681 )' 2>/dev/null | wc -l)
a=$(ps -eo args= | grep -cE '(^|/)(claude|codex|gemini|qwen|opencode|cursor-agent|copilot|kimi)( |$)')
echo "$c $a"'''
_idle = {}               # 环境名 → 开始闲着的时间
_vm_idle_since = [0.0]


def sleep_minutes():
    try:
        return max(0, int(settings().get("autoSleep", 30)))
    except (TypeError, ValueError):
        return 30


def sleeping_flag(name):
    return os.path.join(inst_dir(name), "sleeping")


def _sleep_tick(rt, envs, stats):
    mins = sleep_minutes()
    if not mins:
        _idle.clear()
        return
    now = time.time()
    for n in list(_idle):
        if n not in envs:
            _idle.pop(n, None)
    for n in envs:
        if active_job(n) or read_json(os.path.join(inst_dir(n), "profile.json"), {}).get("keepAwake"):
            _idle.pop(n, None)
            continue
        s = stats.get("easycc-" + n)
        code, out = rt_exec(n, ["sh", "-c", _IDLE_PROBE], 20)
        f = out.split()
        busy = code != 0 or len(f) < 2 or not s or s["cpu"] >= 0.1 or f[0] != "0" or f[1] != "0"
        if busy:
            _idle.pop(n, None)
            continue
        since = _idle.setdefault(n, now)
        if now - since >= mins * 60:
            _idle.pop(n, None)
            try:
                with open(sleeping_flag(n), "w") as fh:
                    fh.write(str(int(now)))
            except OSError:
                continue
            print("空闲休眠：%s 闲了 %d 分钟，停掉把内存还给电脑" % (n, mins), flush=True)
            start_job("sleep", n)


def _vm_sleep_tick():
    """虚拟机运行时：没有运行中的环境 10 分钟 → 关虚拟机"""
    if not sleep_minutes() or runtime() != "vm" or not vm_up() or running_names()[0] or _jobs_busy():
        _vm_idle_since[0] = 0
        return
    if not _vm_idle_since[0]:
        _vm_idle_since[0] = time.time()
    elif time.time() - _vm_idle_since[0] >= 600:
        _vm_idle_since[0] = 0
        print("空闲休眠：虚拟机里没有运行中的环境 10 分钟，关掉虚拟机", flush=True)
        subprocess.run(["bash", CLI, "vm", "stop"], capture_output=True, env=cli_env(), timeout=120)
        _state_cache["t"] = 0


def _jobs_busy():
    with _jobs_lock:
        return any(j["state"] in ("queued", "running") for j in _jobs.values())


def _elastic_loop():
    k = 0
    while True:
        time.sleep(10)
        k += 1
        try:
            _elastic_tick(k % 6 == 0)
            if k % 6 == 0:
                _vm_sleep_tick()
        except Exception as exc:  # noqa: BLE001
            print("弹性资源 / 空闲休眠出错：%s" % exc, flush=True)


def _host_mem():
    try:
        if IS_MAC:
            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3).stdout.strip())
        with open("/proc/meminfo") as f:
            return int(f.readline().split()[1]) * 1024
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


_host = {}


def usage_info(envs):
    """给环境列表加 usage，并算底部汇总"""
    _usage["wanted"] = time.time()
    if not _usage["started"]:
        _usage["started"] = True
        threading.Thread(target=_usage_loop, daemon=True).start()
    if not _host:
        _host.update(cpus=os.cpu_count() or 0, mem=_host_mem())
    with _usage_lock:
        stats, disk = dict(_usage["stats"]), dict(_usage["disk"])
    tot = {"cpuUsed": 0.0, "cpuAlloc": 0, "memUsed": 0, "memAlloc": 0, "disk": 0, "running": 0}
    for e in envs:
        s = stats.get("easycc-" + e["name"]) if e["status"] != "stopped" else None
        cpus = int(e["cpus"]) if str(e["cpus"]).isdigit() else 0
        el = _elastic.get(e["name"]) if s else None
        if el and el.get("cpus"):
            cpus = int(el["cpus"]) if float(el["cpus"]).is_integer() else round(el["cpus"], 1)   # 弹性调过的当前配额
        mem_alloc = (s or {}).get("memLimit") or _alloc_bytes(e["memory"])
        dk = disk.get(e["name"])
        e["usage"] = {"cpus": cpus, "memAlloc": mem_alloc, "elastic": bool(el and (el["cpus"] > el["baseCpus"] or el["mem"] > el["baseMem"])), "cpu": s["cpu"] if s else None, "mem": s["mem"] if s else None,
                      "disk": dk["total"] if dk else None, "diskSystem": dk["system"] if dk else None, "diskData": dk["data"] if dk else None}
        if dk:
            tot["disk"] += dk["total"]
        if e["status"] == "running":
            tot["running"] += 1
            tot["cpuAlloc"] += cpus
            tot["memAlloc"] += mem_alloc
            if s:
                tot["cpuUsed"] += s["cpu"]
                tot["memUsed"] += s["mem"]
    try:
        du = shutil.disk_usage(BASE)
        free, dtotal = du.free, du.total
    except OSError:
        free = dtotal = 0
    tot.update(hostCpus=_host["cpus"], hostMem=_host["mem"], diskFree=free, diskTotal=dtotal, ready=bool(disk) or not envs)
    return tot


def save_profile(name, updates):
    p = os.path.join(inst_dir(name), "profile.json")
    with _write_lock:
        prof = read_json(p, {})
        prof.update(updates)
        write_json(p, prof)


def validate_env_body(b, creating):
    errs = []
    name = str(b.get("name", "")).strip()
    if creating and not NAME_RE.match(name):
        errs.append("环境名只能用字母、数字、- 和 _（最长 40）")
    if creating and os.path.exists(inst_dir(name)):
        errs.append("已经有同名环境")
    px = b.get("proxy") or {}
    if b.get("proxyId"):
        lib = {p["id"]: p for p in proxies()}
        if b["proxyId"] not in lib:
            errs.append("选的代理已从代理库删除")
        else:
            px = lib[b["proxyId"]]
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", str(px.get("host", ""))):
        errs.append("代理地址要填 IP 或域名")
    try:
        if not 1 <= int(px.get("port") or 0) <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        errs.append("代理端口要在 1–65535 之间")
    if creating and b.get("kind", "dev") == "dev" and not str(b.get("dir", "")).strip():
        errs.append("请选择项目目录")
    if b.get("expectIP") and not re.fullmatch(r"[0-9a-fA-F.:]{3,45}", b["expectIP"]):
        errs.append("预期出口 IP 格式不对")
    if b.get("acceptLanguage") and not re.fullmatch(r"[A-Za-z0-9 ,;=.\-]{1,100}", b["acceptLanguage"]):
        errs.append("浏览器语言格式不对，例如 en-US, en")
    if b.get("memory") and not re.fullmatch(r"\d+[MG]", str(b["memory"])):
        errs.append("内存写成 4G 或 4096M")
    return errs, px


def proxy_url_from(b, px, name=None):
    """表单没填密码时沿用原密码（编辑）"""
    if not px.get("pass") and name and not b.get("proxyId"):
        cur = parse_proxy(read_env(os.path.join(inst_dir(name), "conf", "config.env")).get("PROXY_URL", ""))
        if cur["user"] == px.get("user", ""):
            px = dict(px, **{"pass": cur["pass"]})
    return build_proxy(px)


# 新建环境时没指定的分辨率 / CPU 核数：在常见配置里随机挑（环境之间屏幕大小、核数是真的不同）
PERSONA_SCREENS = ["1920x1080", "1920x1080", "1536x864", "1440x900", "1366x768", "1680x1050", "1600x900", "1280x800", "2560x1440", "1600x1000"]


def random_hw():
    import random
    cores = os.cpu_count() or 4
    cpus = [c for c in (2, 4, 4, 6, 8) if c <= max(2, cores // 2)] or [2]
    return {"screen": random.choice(PERSONA_SCREENS), "cpus": str(random.choice(cpus))}


def persona_reset(name):
    """换一组机器身份：新的种子（MAC、主机名、machine-id、字体、渲染）+ 随机分辨率和核数，重启环境后生效"""
    d = inst_dir(name)
    hw = random_hw()
    update_env(os.path.join(d, "conf", "config.env"), {"FP_SEED": secrets.token_hex(16), "SCREEN": hw["screen"]})
    update_env(os.path.join(d, "meta.env"), {"CPUS": hw["cpus"]}, 0o644)
    running = ("easycc-" + name) in running_names()[0]
    return {"ok": True, "message": "已换成新的机器身份（%s · %s 核）%s" % (hw["screen"], hw["cpus"], "，重启环境后生效" if running else "，下次启动生效")}


def create_env(b):
    hw = random_hw()
    b = dict(b, screen=b.get("screen") or hw["screen"], cpus=b.get("cpus") or hw["cpus"])
    errs, px = validate_env_body(b, True)
    if errs:
        return {"ok": False, "message": "；".join(errs)}
    name = b["name"].strip()
    kind = "browser" if b.get("kind") == "browser" else "dev"
    d = os.path.expanduser(host_path(b.get("dir", "")))
    if not d:
        # 浏览器环境不需要项目目录：给它一个独立的文件夹（下载的文件也在这），根目录就是它自己，互不可见
        d = os.path.join(BASE, "_files", name)
        os.makedirs(d, exist_ok=True)
        b = dict(b, root=d)
    args = [CLI, "create", name, "--dir", d, "--proxy", proxy_url_from(b, px)]
    for flag, key in (("--root", "root"), ("--expect-ip", "expectIP"), ("--tz", "tzOverride"), ("--lang", "acceptLanguage"),
                      ("--cpus", "cpus"), ("--memory", "memory"), ("--screen", "screen"), ("--browser", "browser")):
        if str(b.get(key, "")).strip():
            args += [flag, str(b[key]).strip()]
    if b.get("autostartBrowser") is False:
        args.append("--no-autostart-browser")
    p = subprocess.run(["bash"] + args, capture_output=True, text=True, env=cli_env(), timeout=30)
    if p.returncode != 0:
        return {"ok": False, "message": (p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout).strip() else "创建失败"}
    save_profile(name, {"kind": kind, "group": b.get("group", "").strip(), "note": b.get("note", "").strip(), "proxyId": b.get("proxyId", ""),
                        "createdAt": int(time.time())})
    if b.get("openUrl"):
        start_job("open-url", name, b["openUrl"])
    elif b.get("startNow"):
        start_job("start", name)
    return {"ok": True, "message": "已创建环境 %s" % name, "name": name}


def edit_env(name, b):
    if not os.path.isfile(os.path.join(inst_dir(name), "meta.env")):
        return {"ok": False, "message": "环境不存在"}
    errs, px = validate_env_body(b, False)
    if errs:
        return {"ok": False, "message": "；".join(errs)}
    d = inst_dir(name)
    conf_path = os.path.join(d, "conf", "config.env")
    old = read_env(conf_path)
    new_conf = {"PROXY_URL": proxy_url_from(b, px, name), "EXPECT_IP": b.get("expectIP", ""), "TZ_OVERRIDE": b.get("tzOverride", ""),
                "ACCEPT_LANGUAGE": b.get("acceptLanguage", "")}
    if b.get("exitRule") in ("country", "region", "city", "any"):
        new_conf["EXIT_RULE"] = b["exitRule"]
    if "relay" in b:          # 链式代理的中转：整串地址，留空 = 不用；带 *** 沿用原密码
        rv = str(b.get("relay") or "").strip()
        if rv and "://" not in rv:
            rv = "socks5://" + rv
        if rv and not re.fullmatch(r"(socks5|socks5h|http|https)://([^@\s]+@)?[A-Za-z0-9.-]+:\d{1,5}", rv):
            return {"ok": False, "message": "中转代理地址不对，例如 socks5://用户名:密码@1.2.3.4:1080"}
        if "***@" in rv:
            m_old = re.search(r"//([^:/@]*):([^@]*)@", old.get("RELAY_URL", ""))
            rv = rv.replace("***@", (m_old.group(2) if m_old else "") + "@")
        new_conf["RELAY_URL"] = rv
    if b.get("screen"):
        new_conf["SCREEN"] = b["screen"]
    if "autostartBrowser" in b:
        new_conf["AUTOSTART_BROWSER"] = "1" if b["autostartBrowser"] else "0"
    if b.get("browser") in ("firefox", "chrome"):
        new_conf["BROWSER"] = b["browser"]
    if "forwardLocalhost" in b:
        new_conf["FORWARD_LOCALHOST"] = "1" if b["forwardLocalhost"] else "0"
    update_env(conf_path, new_conf)
    meta_up = {k: str(b[f]) for k, f in (("CPUS", "cpus"), ("MEMORY", "memory")) if str(b.get(f, "")).strip()}
    if meta_up:
        update_env(os.path.join(d, "meta.env"), meta_up, 0o644)
    save_profile(name, {"group": b.get("group", "").strip(), "note": b.get("note", "").strip(), "proxyId": b.get("proxyId", ""),
                        **({"keepAwake": bool(b["keepAwake"])} if "keepAwake" in b else {})})
    running = ("easycc-" + name) in running_names()[0]
    msgs = ["已保存"]
    norm = lambda k, v: (v or "country") if k == "EXIT_RULE" else (v or "")   # noqa: E731
    net_changed = any(norm(k, old.get(k)) != norm(k, new_conf.get(k, old.get(k))) for k in ("PROXY_URL", "EXPECT_IP", "TZ_OVERRIDE", "ACCEPT_LANGUAGE", "EXIT_RULE", "RELAY_URL"))
    if running and net_changed:
        start_job("apply-net", name)
        msgs.append("正在热更新网络（新代理不通会保持断网，请看任务结果）")
    # 项目目录：根目录内直接切换，根目录外要换挂载（easycc project 会处理重启）
    newdir = os.path.expanduser(str(b.get("dir", "")).strip())
    if newdir and newdir.rstrip("/") != env_info(name, running)["path"].rstrip("/"):
        start_job("project", name, newdir)
        msgs.append("正在切换项目目录")
    if running and meta_up:
        msgs.append("CPU / 内存重启环境后生效")
    if running and new_conf.get("BROWSER") and (old.get("BROWSER") or "firefox") != new_conf["BROWSER"]:
        threading.Thread(target=rt_exec, args=(name, ["/usr/local/sbin/cbox-userland", "restart-browser"], 60), daemon=True).start()
        msgs.append("正在换成 %s（两个浏览器的登录数据各自独立）" % ("Chrome" if new_conf["BROWSER"] == "chrome" else "Firefox"))
    if running and "forwardLocalhost" in b and (old.get("FORWARD_LOCALHOST", "0") == "1") != bool(b["forwardLocalhost"]):
        subprocess.Popen(["bash", CLI, "portfwd", name], env=cli_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True, "message": "，".join(msgs)}


def clone_env(name, b):
    src = env_info(name)
    conf = read_env(os.path.join(inst_dir(name), "conf", "config.env"))
    new = str(b.get("name", "")).strip()
    body = {"name": new, "kind": src["kind"], "dir": src["path"] if src["kind"] == "dev" else "", "root": src["root"], "proxy": parse_proxy(conf.get("PROXY_URL", "")),
            "proxyId": src["proxyId"], "expectIP": src["expectIP"], "tzOverride": src["tzOverride"], "acceptLanguage": src["acceptLanguage"],
            "cpus": src["cpus"], "memory": src["memory"], "screen": src["screen"], "group": src["group"], "note": src["note"],
            "autostartBrowser": src["autostartBrowser"], "browser": src["browser"]}
    return create_env(body)


# ---------------- 任务队列 ----------------

_jobs = {}
_jobs_lock = threading.Lock()
_queue_lock = threading.Lock()   # 同一时间只跑一个启动 / 构建，避免把机器压垮


def active_job(name):
    with _jobs_lock:
        for j in _jobs.values():
            if j["env"] == name and j["state"] in ("queued", "running"):
                return {"id": j["id"], "kind": j["kind"], "state": j["state"], "line": j["lines"][-1] if j["lines"] else ""}
    return None


JOB_TITLES = {"sleep": "空闲休眠", "storage-move": "更改存储位置", "storage-clean": "清理缓存", "cloud-push": "同步到云端", "image-pull": "下载环境镜像", "open-url": "打开链接", "start": "启动", "stop": "停止", "restart": "重启", "rm": "删除", "build": "构建镜像", "apply-net": "热更新网络", "project": "切换项目"}


def start_job(kind, name="", arg=""):
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "kind": kind, "env": name, "arg": arg, "state": "queued", "lines": [], "code": None,
           "title": "%s%s" % (JOB_TITLES.get(kind, kind), (" · " + name) if name else ""), "createdAt": time.time(), "endedAt": 0}
    with _jobs_lock:
        _jobs[jid] = job
        # 只留最近 60 个
        for old in sorted(_jobs.values(), key=lambda j: j["createdAt"])[:-60]:
            if old["state"] not in ("queued", "running"):
                _jobs.pop(old["id"], None)
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return jid


def _emit(job, line):
    line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line.rstrip())
    if line:
        job["lines"].append(line)
        del job["lines"][:-400]


def _run_job(job):
    heavy = job["kind"] in ("start", "restart", "build", "project", "image-pull", "storage-move", "storage-clean") or (job["kind"] == "open-url" and ("easycc-" + job["env"]) not in running_names()[0])
    if heavy:
        _queue_lock.acquire()
    try:
        job["state"] = "running"
        if job["kind"] == "cloud-push":
            job["code"] = _cloud_push(job)
            return
        if job["kind"] == "storage-move" and not FAKE:
            job["code"] = _storage_move(job)
            return
        if job["kind"] == "storage-clean" and not FAKE:
            job["code"] = _storage_clean(job)
            return
        if FAKE:
            return _fake_job(job)
        if job["kind"] == "image-pull":
            job["code"] = _pull_image(job)
            return
        if job["kind"] in ("start", "restart", "open-url") and runtime() == "vm" and not vm_ensure_image(job):
            job["code"] = 1
            return
        if job["kind"] == "open-url":
            if ("easycc-" + job["env"]) not in running_names()[0]:
                _emit(job, "▸ 环境没在运行，先启动")
                p = subprocess.Popen(["bash", CLI, "start", job["env"]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     env=cli_env(), stdin=subprocess.DEVNULL, bufsize=1)
                for line in p.stdout:
                    _emit(job, line)
                if p.wait() != 0:
                    job["code"] = 1
                    return
                save_profile(job["env"], {"lastOpened": int(time.time())})
                refresh_exit(job["env"])
                time.sleep(3)   # 等桌面和浏览器起来
            _emit(job, "▸ 在环境浏览器中打开 %s" % job["arg"])
            code, out = rt_exec(job["env"], ["/usr/local/bin/cbox-browser", job["arg"]], 30)
            if out:
                _emit(job, out)
            job["code"] = code
            return
        if job["kind"] == "apply-net":
            code, out = rt_exec(job["env"], ["/usr/local/sbin/cbox-net", "apply"])
            for l in out.splitlines():
                _emit(job, l)
            job["code"] = code
            if code in (0, 3):
                refresh_exit(job["env"])
            gate_info(job["env"])
            return
        args = {"start": ["start", job["env"]], "stop": ["stop", job["env"]], "sleep": ["stop", job["env"]], "restart": ["restart", job["env"]],
                "rm": ["rm", job["env"], "--purge"], "build": ["build", "--arch", settings().get("arch", "amd64")],
                "project": ["project", job["env"], job["arg"]]}[job["kind"]]
        p = subprocess.Popen(["bash", CLI] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=cli_env(),
                             stdin=subprocess.DEVNULL, bufsize=1)
        for line in p.stdout:
            _emit(job, line)
        job["code"] = p.wait()
        if job["kind"] in ("start", "restart") and job["code"] == 0:
            save_profile(job["env"], {"lastOpened": int(time.time())})
            refresh_exit(job["env"])
    except Exception as exc:  # noqa: BLE001
        _emit(job, "出错：%s" % exc)
        job["code"] = 1
    finally:
        job["state"] = "done" if job["code"] == 0 else "failed"
        job["endedAt"] = time.time()
        _state_cache["t"] = 0
        if heavy:
            _queue_lock.release()


def _pull_image(job):
    """下载预构建的环境镜像（OCI 归档）→ 校验 → 载入运行时"""
    import hashlib
    import urllib.request
    st = settings()
    url, want = st.get("imageURL", ""), (st.get("imageSHA256") or "").lower()
    if not url:
        _emit(job, "没有设置镜像下载地址")
        return 1
    dest = os.path.join(BASE, "_image.tar")
    _emit(job, "▸ 下载 %s" % url)
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "easycc"}), timeout=60) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got, last = 0, 0
            while True:
                c = r.read(1 << 20)
                if not c:
                    break
                f.write(c)
                h.update(c)
                got += len(c)
                if time.time() - last > 1.5:
                    last = time.time()
                    _emit(job, "  %s / %s%s" % (_mb(got), _mb(total) if total else "?", "（%d%%）" % (got * 100 // total) if total else ""))
    except Exception as exc:  # noqa: BLE001
        _emit(job, "下载失败：%s" % exc)
        return 1
    if want and h.hexdigest() != want:
        _emit(job, "校验失败：文件不完整或被篡改")
        os.remove(dest)
        return 1
    _emit(job, "▸ 载入镜像（几十秒）")
    rt = runtime()
    if rt != "container":
        if rt == "vm" and not vm_up():
            _emit(job, "▸ 启动 easyCC 虚拟机")
            r = subprocess.run(["bash", CLI, "vm", "start"], capture_output=True, text=True, env=cli_env(), timeout=180)
            for l in (r.stdout + r.stderr).splitlines()[-3:]:
                _emit(job, "  " + l)
        # 我们发布的是 OCI 镜像包（索引套了一层），Docker / Podman 都统一先转成 docker load 格式
        conv = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(HERE), "tools", "oci-to-docker.py"), dest,
                                 "--platform", "linux/" + settings().get("arch", "amd64")], stdout=subprocess.PIPE)
        p = subprocess.run([rt_bin(rt), "load"], stdin=conv.stdout, capture_output=True, text=True)
        conv.wait()
        if p.returncode == 0 and rt == "vm":
            open(os.path.join(VM_DIR, "image-loaded"), "w").write(_vm_data_id())
    else:
        cmd = [rt, "image", "load", "-i", dest] if rt == "container" else [rt, "load", "-i", dest]
        p = subprocess.run(cmd, capture_output=True, text=True)
    for l in (p.stdout + p.stderr).splitlines()[-5:]:
        _emit(job, l)
    os.remove(dest)
    return p.returncode


def _mb(n):
    return "%.0f MB" % (n / 1048576.0) if n < 1 << 30 else "%.2f GB" % (n / 1073741824.0)


def _fake_job(job):
    steps = {"start": ["▸ 启动 easycc-%s" % job["env"], "▸ 等待网络自检…", "  [cbox-net] 防火墙已生效：直连外网全部拒绝", "  [cbox-net] 出口 IP：203.0.113.45", "▸ 管理后台已就绪"],
             "build": ["▸ 构建镜像 easycc:latest（linux/amd64，含 Kiteline）", "#5 [2/9] RUN apt-get update …", "#9 [6/9] RUN 安装 Kiteline …", "镜像构建完成"]}
    for l in steps.get(job["kind"], ["▸ %s" % job["title"]]):
        _emit(job, l)
        time.sleep(0.6)
    if job["kind"] == "open-url":
        _emit(job, "▸ 在环境浏览器中打开 %s" % job["arg"])
    if job["kind"] in ("start", "restart", "open-url"):
        _fake_running.add("easycc-" + job["env"])
        save_profile(job["env"], {"lastOpened": int(time.time())})
    elif job["kind"] in ("stop", "rm"):
        _fake_running.discard("easycc-" + job["env"])
        if job["kind"] == "rm":
            shutil.rmtree(inst_dir(job["env"]), ignore_errors=True)
    job["code"] = 0


def refresh_exit(name):
    """从环境里读出口信息存起来（列表里显示国家、IP）"""
    if FAKE:
        return
    code, out = rt_exec(name, ["cat", "/run/cbox/status.env"], 15)
    if code == 0:
        st = {}
        for line in out.splitlines():
            m = re.match(r"([A-Z_]+)=(.*)", line)
            if m:
                st[m.group(1)] = m.group(2).strip("'")
        save_profile(name, {"exit": {"ip": st.get("EXIT_IP", ""), "country": st.get("COUNTRY", ""), "location": st.get("LOCATION", ""),
                                     "timezone": st.get("TIMEZONE", ""), "at": int(time.time())}})


def jobs_payload():
    with _jobs_lock:
        js = sorted(_jobs.values(), key=lambda j: j["createdAt"], reverse=True)[:30]
        return [{"id": j["id"], "kind": j["kind"], "env": j["env"], "title": j["title"], "state": j["state"], "code": j["code"],
                 "createdAt": j["createdAt"], "endedAt": j["endedAt"], "last": j["lines"][-1] if j["lines"] else ""} for j in js]


def job_detail(jid):
    with _jobs_lock:
        j = _jobs.get(jid)
        return dict(j, lines=list(j["lines"])) if j else None


# ---------------- 代理库与检测（在本机直接按协议握手）----------------

def proxies():
    return read_json(os.path.join(BASE, "proxies.json"), [])


def save_proxies(lst):
    os.makedirs(BASE, exist_ok=True)
    write_json(os.path.join(BASE, "proxies.json"), lst)


def _recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            raise ConnectionError("代理服务器提前断开")
        buf += c
    return buf


def _tunnel(px, host, port, timeout=10):
    """经代理打通到 host:port 的 TCP 隧道，返回 (socket, 握手耗时 ms)"""
    t0 = time.time()
    s = socket.create_connection((px["host"], int(px["port"])), timeout=timeout)
    s.settimeout(timeout)
    if px.get("type", "socks5") == "socks5":
        methods = b"\x00\x02" if px.get("user") else b"\x00"
        s.sendall(b"\x05" + bytes([len(methods)]) + methods)
        ver, method = _recv_exact(s, 2)
        if ver != 5:
            raise ValueError("对方不是 SOCKS5 服务")
        if method == 0xFF:
            raise ValueError("代理不接受认证方式（多半要用户名密码）")
        if method == 2:
            u, p = px.get("user", "").encode(), px.get("pass", "").encode()
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            if _recv_exact(s, 2)[1] != 0:
                raise ValueError("用户名或密码错误")
        h = host.encode()
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + port.to_bytes(2, "big"))
        head = _recv_exact(s, 4)
        if head[1] != 0:
            raise ValueError("代理拒绝连接目标（错误码 %d）" % head[1])
        n = 4 if head[3] == 1 else 16 if head[3] == 4 else _recv_exact(s, 1)[0]
        _recv_exact(s, n + 2)
    else:
        req = "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n" % (host, port, host, port)
        if px.get("user"):
            req += "Proxy-Authorization: Basic %s\r\n" % base64.b64encode(("%s:%s" % (px["user"], px.get("pass", ""))).encode()).decode()
        s.sendall((req + "\r\n").encode())
        data = b""
        while b"\r\n\r\n" not in data:
            c = s.recv(4096)
            if not c:
                raise ConnectionError("代理服务器提前断开")
            data += c
        line = data.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 200" not in line:
            raise ValueError("代理返回 %s%s" % (line, "（用户名或密码错误）" if " 407" in line else ""))
    return s, int((time.time() - t0) * 1000)


def check_proxy(px):
    """检测代理：能否连通、延迟、出口 IP、国家 / 城市 / 时区 / 运营商 / 是否机房"""
    if FAKE:
        time.sleep(0.8)
        ok = not px["host"].startswith("10.")
        return {"ok": ok, "latency": 180 + len(px["host"]) * 7, "ip": "203.0.113.%d" % (40 + len(px["host"]) % 50), "country": "US",
                "countryName": "United States", "city": "Los Angeles", "timezone": "America/Los_Angeles", "isp": "Example Hosting LLC",
                "hosting": True, "error": "" if ok else "连接超时", "at": int(time.time())}
    res = {"ok": False, "at": int(time.time())}
    try:
        s, ms = _tunnel(px, "ip-api.com", 80)
        s.sendall(b"GET /json/?fields=status,message,query,country,countryCode,city,timezone,isp,org,hosting,proxy HTTP/1.1\r\n"
                  b"Host: ip-api.com\r\nConnection: close\r\nUser-Agent: curl/8.5.0\r\n\r\n")
        data = b""
        while True:
            c = s.recv(8192)
            if not c:
                break
            data += c
        s.close()
        body = json.loads(data.split(b"\r\n\r\n", 1)[1].decode(errors="replace"))
        res.update(ok=True, latency=ms, ip=body.get("query", ""), country=body.get("countryCode", ""), countryName=body.get("country", ""),
                   city=body.get("city", ""), timezone=body.get("timezone", ""), isp=body.get("isp", "") or body.get("org", ""),
                   hosting=bool(body.get("hosting")), error="")
    except ConnectionRefusedError:
        res["error"] = "连接被拒绝：地址或端口不对，或代理没开"
    except socket.timeout:
        res["error"] = "连接超时：代理不通或被墙"
    except socket.gaierror:
        res["error"] = "域名解析失败：代理地址写错了"
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc) or type(exc).__name__
    return res


def add_proxies(text, name="", group=""):
    """一行一个：socks5://user:pass@host:port、http://…、host:port:user:pass、host:port"""
    lst = proxies()
    added, bad = 0, []
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            p = parse_proxy(line)
        else:
            parts = line.split(":")
            if len(parts) in (2, 4):
                p = {"type": "socks5", "host": parts[0], "port": parts[1], "user": parts[2] if len(parts) == 4 else "",
                     "pass": parts[3] if len(parts) == 4 else ""}
            else:
                bad.append(line)
                continue
        try:
            p["port"] = int(p["port"])
        except (TypeError, ValueError):
            bad.append(line)
            continue
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", p["host"]) or not 1 <= p["port"] <= 65535:
            bad.append(line)
            continue
        lst.append(dict(p, id=uuid.uuid4().hex[:8], name=name if (name and added == 0) else "", group=group, check={}, createdAt=int(time.time())))
        added += 1
    save_proxies(lst)
    return {"ok": added > 0, "added": added, "bad": bad,
            "message": "已添加 %d 个代理%s" % (added, "，%d 行格式不对" % len(bad) if bad else "")}


def proxy_public(p):
    q = dict(p)
    q["hasPass"] = bool(q.pop("pass", ""))
    q["usedBy"] = [n for n in env_names() if read_json(os.path.join(inst_dir(n), "profile.json"), {}).get("proxyId") == p["id"]]
    return q


def check_proxies(ids):
    lst = proxies()
    targets = [p for p in lst if p["id"] in ids] if ids else lst
    results = {}

    def one(p):
        results[p["id"]] = check_proxy(p)
    threads = [threading.Thread(target=one, args=(p,)) for p in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(25)
    for p in lst:
        if p["id"] in results:
            p["check"] = results[p["id"]]
    save_proxies(lst)
    return {"ok": True, "results": results}


# ---------------- 导出 / 导入 ----------------

_downloads = {}   # 一次性下载链接：id → (文件, 下载名, 过期时间)
_imports = {}     # 上传待确认的导入：id → (文件, 清单)


def export_env_api(name, b):
    info = env_info(name)
    conf = read_env(os.path.join(inst_dir(name), "conf", "config.env"))
    out_dir = os.path.join(BASE, "_exports")
    os.makedirs(out_dir, exist_ok=True)
    os.chmod(out_dir, 0o700)
    fname = "%s-%s.cbox" % (name, time.strftime("%Y%m%d-%H%M"))
    out = os.path.join(out_dir, uuid.uuid4().hex + ".cbox")
    try:
        r = cbox_transfer.export_env(info, conf, os.path.join(inst_dir(name), "home"), out,
                                     with_login=b.get("withLogin", True) is not False, with_password=b.get("withPassword", True) is not False)
    except OSError as exc:
        return {"ok": False, "message": "导出失败：%s" % exc}
    did = secrets.token_urlsafe(16)
    _downloads[did] = (out, fname, time.time() + 300)
    return {"ok": True, "url": "/download/" + did, "filename": fname, "files": r["files"], "bytes": r["bytes"], "archive": r["archive"],
            "warning": "环境正在运行：浏览器正在写的数据可能不完整，最好先停止再导出" if info["status"] == "running" else ""}


def unique_name(name):
    base = re.sub(r"[^A-Za-z0-9_-]", "-", name or "env")[:34] or "env"
    if not os.path.exists(inst_dir(base)):
        return base
    for i in range(2, 100):
        if not os.path.exists(inst_dir("%s-%d" % (base, i))):
            return "%s-%d" % (base, i)
    return base + "-" + uuid.uuid4().hex[:4]


def import_inspect(path):
    try:
        m = cbox_transfer.read_manifest(path)
    except ValueError as exc:
        os.remove(path)
        return {"ok": False, "message": str(exc)}
    tid = uuid.uuid4().hex[:12]
    _imports[tid] = (path, m, time.time() + 1800)
    px = parse_proxy(m.get("proxy", ""))
    return {"ok": True, "tmp": tid, "name": m["name"], "suggestedName": unique_name(m["name"]), "kind": m.get("kind", "dev"),
            "group": m.get("group", ""), "note": m.get("note", ""), "path": m.get("path", ""), "pathExists": os.path.isdir(m.get("path", "")),
            "proxy": {"type": px["type"], "host": px["host"], "port": px["port"], "user": px["user"]}, "needPassword": bool(px["user"]) and not px["pass"],
            "withLogin": m.get("withLogin"), "files": m["homeFiles"], "bytes": m["homeBytes"], "exportedAt": m.get("exportedAt"), "exit": m.get("exit") or {}}


def import_commit(b):
    item = _imports.get(b.get("tmp", ""))
    if not item:
        return {"ok": False, "message": "上传的文件已过期，请重新选择"}
    path, m, _ = item
    px = parse_proxy(m.get("proxy", ""))
    if b.get("pass"):
        px["pass"] = b["pass"]
    st = m.get("settings") or {}
    kind = m.get("kind", "dev")
    body = {"name": str(b.get("name", "")).strip(), "kind": kind, "dir": str(b.get("dir", "")).strip(), "proxy": px,
            "group": m.get("group", ""), "note": m.get("note", ""), "expectIP": st.get("expectIP", ""), "tzOverride": st.get("tzOverride", ""),
            "acceptLanguage": st.get("acceptLanguage", ""), "screen": st.get("screen", ""), "cpus": st.get("cpus", ""), "memory": st.get("memory", ""),
            "autostartBrowser": st.get("autostartBrowser", True)}
    if kind == "dev" and body["dir"]:
        os.makedirs(os.path.expanduser(body["dir"]), exist_ok=True)
    r = create_env(body)
    if not r.get("ok"):
        return r
    name = r["name"]
    try:
        n = cbox_transfer.extract_home(path, os.path.join(inst_dir(name), "home"))
    except (OSError, tarfile_error()) as exc:
        return {"ok": False, "message": "环境已创建，但恢复用户数据失败：%s" % exc}
    prof = {"exit": m.get("exit") or {}, "importedAt": int(time.time())}
    if b.get("tmp") in _cloud_pending:
        cname, ver = _cloud_pending.pop(b["tmp"])
        taken = any(n != name and (read_json(os.path.join(inst_dir(n), "profile.json"), {}).get("cloudName") or n) == cname for n in env_names())
        if taken:
            # 本机已有环境对应这个云端条目：恢复出来的副本以后单独同步（用它自己的名字），两边不互相覆盖
            prof.update(cloudName=name, cloudVersion=0, restoredFromCloud=True)
        else:
            prof.update(cloudName=cname, cloudVersion=ver, cloudSyncedAt=int(time.time()), restoredFromCloud=True)
    save_profile(name, prof)
    _imports.pop(b["tmp"], None)
    try:
        os.remove(path)
    except OSError:
        pass
    if b.get("startNow"):
        start_job("start", name)
    return {"ok": True, "name": name, "message": "已%s环境 %s（恢复了 %d 个文件）" % ("从云端恢复" if prof.get("restoredFromCloud") else "导入", name, n)}


def tarfile_error():
    import tarfile
    return tarfile.TarError


def cleanup_transfers():
    now = time.time()
    for store in (_downloads, _imports):
        for k, v in list(store.items()):
            if v[-1] < now:
                store.pop(k, None)
                try:
                    os.remove(v[0])
                except OSError:
                    pass


# ---------------- 本机辅助：选文件夹、开终端 ----------------

def pick_folder(start="", title="选择项目目录"):
    if IS_WSL:
        # Windows 自带的选择文件夹窗口（经 WSL 互操作调 PowerShell），选好换成 WSL 路径
        ps = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        if os.path.exists(ps):
            p = subprocess.run([ps, "-NoProfile", "-STA", "-Command",
                                "Add-Type -AssemblyName System.Windows.Forms;$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                                "$d.Description='%s';" % title.replace("'", "") + "if($d.ShowDialog() -eq 'OK'){[Console]::OutputEncoding=[Text.Encoding]::UTF8;$d.SelectedPath}"],
                               capture_output=True, text=True, timeout=600)
            path = p.stdout.strip()
            return {"ok": bool(path), "path": host_path(path), "message": "" if path else "已取消"}
    if FAKE:
        return {"ok": True, "path": os.path.expanduser("~/code/new-project")}
    if IS_MAC:
        script = 'POSIX path of (choose folder with prompt "%s"%s)' % (title.replace('"', ""), 
            ' default location POSIX file "%s"' % start.replace('"', "") if start and os.path.isdir(start) else "")
        p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        return {"ok": p.returncode == 0, "path": p.stdout.strip().rstrip("/"), "message": "" if p.returncode == 0 else "已取消"}
    if shutil.which("zenity"):
        p = subprocess.run(["zenity", "--file-selection", "--directory", "--title=" + title], capture_output=True, text=True)
        return {"ok": p.returncode == 0, "path": p.stdout.strip(), "message": "" if p.returncode == 0 else "已取消"}
    return {"ok": False, "message": "这台电脑不支持弹出选择窗口，请直接填路径"}


def open_terminal(name, what):
    # 新开的终端用的是系统环境，找不到 App 自带的运行时：把路径等一起带上
    env = cli_env()
    pre = "export PATH=%s EASYCC_HOME=%s%s; clear; " % (shq(env.get("PATH", "")), shq(BASE),
                                                      (" EASYCC_RUNTIME=%s" % shq(env["EASYCC_RUNTIME"])) if env.get("EASYCC_RUNTIME") else "")
    cmd = pre + "%s %s %s" % (shq(CLI), "claude" if what == "claude" else "shell", name)
    if FAKE:
        return {"ok": True, "message": "（演示）在终端里运行：easycc %s %s" % (what, name)}
    if IS_MAC:
        osa = 'tell application "Terminal" to do script "%s"\ntell application "Terminal" to activate' % cmd.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(["osascript", "-e", osa])
        return {"ok": True, "message": "已在终端里打开"}
    if IS_WSL:
        # Windows Terminal（Win11 自带），没有就用系统的命令行窗口
        cmdexe = "/mnt/c/Windows/System32/cmd.exe"
        inner = "wsl.exe -d easyCC -u root -- bash -lc %s" % shq("export EASYCC_HOME=%s; %s; exec bash" % (shq(BASE), cmd))
        subprocess.Popen([cmdexe, "/c", "start", "", "wt.exe", inner], cwd="/mnt/c/")
        return {"ok": True, "message": "已在终端里打开"}
    for term in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"], ["konsole", "-e"], ["xterm", "-e"]):
        if shutil.which(term[0]):
            subprocess.Popen(term + ["bash", "-lc", cmd + "; exec bash"], env=cli_env())
            return {"ok": True, "message": "已在终端里打开"}
    return {"ok": False, "message": "没找到终端程序，请手动运行：easycc %s %s" % (what, name)}


def open_vscode(name):
    if FAKE:
        return {"ok": True, "message": "（演示）已用 VS Code 打开"}
    p = subprocess.run(["bash", CLI, "code", name], capture_output=True, text=True, env=cli_env(), timeout=30)
    return {"ok": p.returncode == 0, "message": "已用 VS Code / Cursor 打开" if p.returncode == 0 else (p.stderr or p.stdout).strip()[-200:]}


def system_info():
    rt = runtime()
    _, err = running_names()
    image = False
    if FAKE:
        image = True
    elif rt == "vm" and not vm_up():
        image = vm_image_marked()
    elif rt:
        try:
            image = subprocess.run([rt_bin(rt), "image", "inspect", "easycc:latest"], capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"runtime": "演示模式" if FAKE else rt, "runtimeSetting": settings().get("runtime", "auto"), "runtimeError": err, "image": image,
            "arch": settings().get("arch", "amd64"), "host": platform.platform(), "machine": platform.machine(), "dataDir": BASE,
            "available": [r for r in ("container", "docker", "podman") if shutil.which(r) or FAKE] + (["vm"] if app_vm() else []), "fake": FAKE,
            "port": settings().get("port"), "imageURL": settings().get("imageURL", ""), "imageSHA256": settings().get("imageSHA256", ""),
            "elastic": settings().get("elastic", True) is not False, "autoSleep": sleep_minutes(),
            "telemetry": settings().get("telemetry", True) is not False}


# ---------------- 存储位置与占用 ----------------
# 数据分两块：环境数据（BASE：各环境的主目录、虚拟机磁盘）和 Apple container 的数据（镜像、环境系统盘、构建缓存）。
# 「更改存储位置」把它们搬到用户选的文件夹，原来的路径换成指向新位置的符号链接：环境配置里记的路径都还有效，
# 搬回默认位置时再把真目录放回原处。同一块盘直接改名（瞬间完成）；跨盘逐个复制，保留稀疏文件（环境系统盘名义上 500 多 GB，
# 实际只占几 GB），复制完核对一遍再切换、删旧的。

SUP_DIR = os.path.expanduser("~/Library/Application Support/easyCC")
_store_cache = {"t": 0, "data": None}


def storage_items():
    """[(名称, 固定路径, 新位置里的子目录)]"""
    items = [("环境数据", BASE, "data")]
    if IS_MAC and (os.path.lexists(os.path.join(SUP_DIR, "container")) or (platform.machine() == "arm64" and mac_major() >= 26)):
        items.append(("镜像与系统盘", os.path.join(SUP_DIR, "container"), "container"))
    return items


def _alloc(path):
    """实际占用（按分配的块算，稀疏文件只算真写过的部分）"""
    total = 0
    if not os.path.isdir(path):
        try:
            return os.lstat(path).st_blocks * 512
        except OSError:
            return 0
    for root, dirs, files in os.walk(path):
        for n in files + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
            try:
                total += os.lstat(os.path.join(root, n)).st_blocks * 512
            except OSError:
                pass
    return total


def _is_default(fixed):
    """数据就在固定路径上（它和它的上一级都不是符号链接）"""
    return not os.path.islink(fixed) and not os.path.islink(os.path.dirname(fixed))


def _storage_root():
    """当前存储位置：环境数据是符号链接就是它指向的上一级，否则 None（默认位置）"""
    if os.path.islink(BASE):
        return os.path.dirname(os.path.realpath(BASE))
    return None


def storage_info(force=False):
    if not force and _store_cache["data"] and time.time() - _store_cache["t"] < 30:
        return _store_cache["data"]
    items, parts = [], {"sys": 0, "home": 0, "img": 0, "cache": 0, "vm": 0, "other": 0}
    nenv = 0
    for label, fixed, sub in storage_items():
        real = os.path.realpath(fixed)
        ok = os.path.isdir(real)
        size = _alloc(real) if ok else 0
        try:
            free = shutil.disk_usage(real).free if ok else 0
        except OSError:
            free = 0
        items.append({"label": label, "path": fixed, "real": real, "default": _is_default(fixed),
                      "ok": ok, "bytes": size, "free": free})
        if not ok:
            continue
        if sub == "container":
            cdir = os.path.join(real, "containers")
            for n in (os.listdir(cdir) if os.path.isdir(cdir) else []):
                s = _alloc(os.path.join(cdir, n))
                if n.startswith("easycc-"):
                    parts["sys"] += s
                    nenv += 1
                elif n == "buildkit":
                    parts["cache"] += s
                else:
                    parts["other"] += s
            img = _alloc(os.path.join(real, "snapshots")) + _alloc(os.path.join(real, "content"))
            parts["img"] += img
            parts["other"] += max(0, size - img - _alloc(cdir))
        else:
            for n in os.listdir(real):
                p = os.path.join(real, n)
                if n == "vm":
                    parts["vm"] += _alloc(p)            # easyCC 虚拟机（macOS 12–25）的系统盘和数据盘：镜像、环境系统盘都在里面
                elif os.path.isfile(os.path.join(p, "meta.env")):
                    parts["home"] += _alloc(p)
                else:
                    parts["other"] += _alloc(p)
    root = _storage_root()
    vm_used = runtime() == "vm"
    data = {"root": root, "default": all(i["default"] for i in items), "items": items, "envs": nenv,
            "parts": [{"key": "sys", "label": "环境系统盘", "bytes": parts["sys"]},
                      {"key": "home", "label": "环境主目录", "bytes": parts["home"]},
                      {"key": "img", "label": "镜像", "bytes": parts["img"]},
                      {"key": "cache", "label": "构建缓存", "bytes": parts["cache"]},
                      {"key": "vm", "label": "虚拟机" + ("" if vm_used else "（没在用）"), "bytes": parts["vm"]},
                      {"key": "other", "label": "其他", "bytes": parts["other"]}],
            "movable": not IS_WSL, "busy": any(j["kind"] in ("storage-move", "storage-clean") and j["state"] in ("queued", "running")
                                               for j in list(_jobs.values()))}
    _store_cache.update(t=time.time(), data=data)
    return data


def _gb(n):
    return "%.1f GB" % (n / (1 << 30))


def _existing_parent(p):
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    return p or "/"


def storage_plan(target):
    """检查能不能搬，返回 (提示, 计划)；计划为 None 表示不能搬"""
    if IS_WSL:
        return "Windows 版暂不支持更改存储位置", None
    target = str(target or "").strip()
    if target:
        target = os.path.abspath(os.path.expanduser(host_path(target)))
        if os.path.basename(target).lower() != "easycc":
            target = os.path.join(target, "easyCC")
    plan, need, notes = [], 0, []
    for label, fixed, sub in storage_items():
        if os.path.islink(fixed) and not os.path.exists(fixed):
            return "「%s」现在的位置 %s 不可用（外置盘没接上？），接上后再操作" % (label, os.readlink(fixed)), None
        cur = os.path.realpath(fixed)
        dst = os.path.join(target, sub) if target else fixed
        if not os.path.isdir(cur):
            # 还没有数据（比如还没用过 Apple container）：直接在新位置建空目录
            plan.append({"label": label, "fixed": fixed, "cur": None, "dst": dst, "mode": "new", "bytes": 0})
            continue
        if (dst == fixed and _is_default(fixed)) or (dst != fixed and os.path.realpath(dst) == cur):
            continue
        ep = _existing_parent(dst) if dst != fixed else os.path.dirname(fixed)
        dreal = os.path.normpath(os.path.join(os.path.realpath(ep), os.path.relpath(dst, ep)))
        if dst != fixed and ((dreal + "/").startswith(cur + "/") or (cur + "/").startswith(dreal + "/")):
            return "新位置不能放在现在的数据目录里面：%s" % cur, None
        if dst != fixed and os.path.exists(dst) and os.listdir(dst):
            return "新位置里已经有东西：%s（换一个文件夹，或者先把它清空）" % dst, None
        parent = _existing_parent(os.path.dirname(dst))
        if not os.access(parent, os.W_OK):
            return "没有权限写入 %s" % parent, None
        same = os.stat(cur).st_dev == os.stat(parent).st_dev
        size = 0 if same else _alloc(cur)
        need += size
        plan.append({"label": label, "fixed": fixed, "cur": cur, "dst": dst, "mode": "rename" if same else "copy", "bytes": size})
        notes.append("%s：%s → %s（%s）" % (label, cur, dst, "同一块盘，直接移动" if same else "复制 " + _gb(size)))
    if not plan:
        return "数据已经在这个位置了", None
    if need:
        parent = _existing_parent(target or BASE)
        free = shutil.disk_usage(parent).free
        if need * 1.05 + 1e9 > free:
            return "新位置空间不够：需要约 %s，只剩 %s" % (_gb(need * 1.05 + 1e9), _gb(free)), None
        if not _sparse_ok(parent):
            return "新位置所在磁盘不支持稀疏文件（exFAT、FAT 格式？），环境系统盘会按名义大小占满整盘。请换到 APFS / Mac OS 扩展格式的磁盘，或先把它格式化", None
    return "\n".join(notes), {"target": target, "items": plan, "need": need}


def _sparse_ok(d):
    try:
        fd, p = tempfile.mkstemp(prefix=".easycc-sparse-", dir=d)
    except OSError:
        return False
    try:
        os.ftruncate(fd, 1 << 30)
        return os.fstat(fd).st_blocks * 512 < (64 << 20)
    except OSError:
        return False
    finally:
        os.close(fd)
        os.unlink(p)


_ZERO = bytes(8 << 20)


def _copy_file(s, d, st, prog):
    with open(s, "rb") as fi, open(d, "wb") as fo:
        fdi, fdo, size, pos = fi.fileno(), fo.fileno(), st.st_size, 0
        os.ftruncate(fdo, size)
        seek = hasattr(os, "SEEK_DATA")
        while pos < size:
            start, end = pos, size
            if seek:
                try:
                    start = os.lseek(fdi, pos, os.SEEK_DATA)
                    end = os.lseek(fdi, start, os.SEEK_HOLE)
                except OSError as exc:
                    if exc.errno == 6:          # ENXIO：后面全是空洞
                        break
                    seek, start, end = False, pos, size
            off = start
            while off < end:
                buf = os.pread(fdi, min(len(_ZERO), end - off), off)
                if not buf:
                    end = off
                    break
                if buf != _ZERO[:len(buf)]:      # 全零的块不写，留成空洞
                    os.pwrite(fdo, buf, off)
                off += len(buf)
                prog(len(buf))
            pos = max(end, pos + 1) if end <= pos else end
    os.utime(d, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.chmod(d, st.st_mode & 0o7777)


def _copy_tree(src, dst, prog, update=False):
    """复制目录：保留符号链接、权限、修改时间、稀疏文件；update=True 时只补复制有变化的文件（第二遍）"""
    count = 0
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        droot = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(droot, exist_ok=True)
        for n in files + [x for x in dirs if os.path.islink(os.path.join(root, x))]:
            sp, dp = os.path.join(root, n), os.path.join(droot, n)
            st = os.lstat(sp)
            if update and os.path.lexists(dp):
                dt = os.lstat(dp)
                if (dt.st_mode & 0o170000) == (st.st_mode & 0o170000) and dt.st_size == st.st_size and dt.st_mtime_ns == st.st_mtime_ns:
                    continue
                if os.path.isdir(dp) and not os.path.islink(dp):
                    shutil.rmtree(dp)
                else:
                    os.unlink(dp)
            if os.path.islink(sp):
                os.symlink(os.readlink(sp), dp)
            elif os.path.isfile(sp):
                _copy_file(sp, dp, st, prog)
            else:
                continue                        # 套接字、管道：运行时才有，不用搬
            count += 1
    for root, dirs, files in os.walk(src, topdown=False):
        rel = os.path.relpath(root, src)
        droot = dst if rel == "." else os.path.join(dst, rel)
        try:
            st = os.stat(root)
            os.chmod(droot, st.st_mode & 0o7777)
            os.utime(droot, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass
    return count


def _tree_sum(path):
    n = size = 0
    for root, dirs, files in os.walk(path):
        for x in files + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
            st = os.lstat(os.path.join(root, x))
            if os.path.islink(os.path.join(root, x)) or os.path.isfile(os.path.join(root, x)):
                n += 1
                size += 0 if os.path.islink(os.path.join(root, x)) else st.st_size
    return n, size


def _container_cli():
    return shutil.which("container") or os.path.join(APP_RES, "runtime", "container", "bin", "container")


def _stop_everything(job):
    """停掉运行中的环境和运行时，返回原来在运行的环境名"""
    names = sorted(n[len("easycc-"):] for n in running_names()[0] if n.startswith("easycc-"))
    for n in names:
        _emit(job, "▸ 停止环境 %s" % n)
        p = subprocess.run(["bash", CLI, "stop", n], capture_output=True, text=True, env=cli_env(), timeout=180)
        if p.returncode != 0:
            raise RuntimeError("停止 %s 失败：%s" % (n, (p.stderr or p.stdout).strip()[-200:]))
    rt = runtime()
    if rt == "vm" and vm_up():
        _emit(job, "▸ 关闭 easyCC 虚拟机")
        subprocess.run(["bash", CLI, "vm", "stop"], capture_output=True, env=cli_env(), timeout=120)
    if IS_MAC and os.path.exists(_container_cli()) and subprocess.run([_container_cli(), "system", "status"], capture_output=True, timeout=20).returncode == 0:
        _emit(job, "▸ 关闭容器运行时")
        subprocess.run([_container_cli(), "system", "stop"], capture_output=True, timeout=120)
    return names


def _start_runtime(job):
    launcher = os.path.join(APP_RES, "bin", "easycc-server")
    if IS_MAC and platform.machine() == "arm64" and mac_major() >= 26 and os.access(launcher, os.X_OK):
        _emit(job, "▸ 启动容器运行时")
        p = subprocess.run([launcher, "--runtime-only"], capture_output=True, text=True, timeout=180)
        if p.returncode != 0:
            _emit(job, (p.stderr or p.stdout).strip()[-300:])


def _relink(fixed, dst):
    """固定路径换成指向 dst 的符号链接（原来是链接就换掉；真目录调用前已经搬走）"""
    if os.path.islink(fixed):
        os.unlink(fixed)
    os.makedirs(os.path.dirname(fixed), exist_ok=True)
    os.symlink(dst, fixed)


def _storage_move(job):
    msg, plan = storage_plan(job["arg"])
    if not plan:
        _emit(job, msg)
        return 1
    for l in msg.splitlines():
        _emit(job, l)
    was_running = _stop_everything(job)
    try:
        # 用户自己把整个 ~/Library/Application Support/easyCC 链到别处的：先恢复成本机的真目录（日志留在本机），只让 container 指过去
        if IS_MAC and os.path.islink(SUP_DIR) and any(i["fixed"].startswith(SUP_DIR + "/") for i in plan["items"]):
            old = os.path.realpath(SUP_DIR)
            os.unlink(SUP_DIR)
            os.makedirs(SUP_DIR)
            for n in os.listdir(old):
                if n != "container":
                    shutil.move(os.path.join(old, n), os.path.join(SUP_DIR, n))
            if os.path.isdir(os.path.join(old, "container")):
                os.symlink(os.path.join(old, "container"), os.path.join(SUP_DIR, "container"))
        total = sum(i["bytes"] for i in plan["items"]) or 1
        done = [0, time.time()]

        def prog(n):
            done[0] += n
            if time.time() - done[1] > 2:
                done[1] = time.time()
                _emit(job, "  已复制 %s / 约 %s（%d%%）" % (_gb(done[0]), _gb(total), min(99, done[0] * 100 // total)))

        old_dirs = []
        for it in plan["items"]:
            fixed, cur, dst = it["fixed"], it["cur"], it["dst"]
            if it["mode"] == "new":
                os.makedirs(dst, exist_ok=True)
                if dst != fixed:
                    _relink(fixed, dst)
                continue
            _emit(job, "▸ %s：%s" % (it["label"], "移动" if it["mode"] == "rename" else "复制到新位置"))
            if it["mode"] == "rename":
                if dst == fixed:                 # 搬回默认位置：fixed 现在是链接
                    os.unlink(fixed)
                    os.rename(cur, fixed)
                else:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    os.rename(cur, dst)
                    _relink(fixed, dst)
                continue
            tmp = dst + ".partial"
            if os.path.lexists(tmp):
                shutil.rmtree(tmp)
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            _copy_tree(cur, tmp, prog)
            _copy_tree(cur, tmp, prog, update=True)   # 复制期间有改动的补一遍
            a, b = _tree_sum(cur), _tree_sum(tmp)
            if a != b:
                shutil.rmtree(tmp, ignore_errors=True)
                raise RuntimeError("核对不一致（原 %d 个文件 %d 字节，新 %d 个文件 %d 字节），已放弃，数据还在原处" % (a[0], a[1], b[0], b[1]))
            _emit(job, "  核对通过：%d 个文件" % a[0])
            if dst == fixed:
                os.unlink(fixed)
                os.rename(tmp, fixed)
            else:
                os.rename(tmp, dst)
                if not os.path.islink(fixed):
                    os.rename(fixed, fixed + ".easycc-old")
                    cur = fixed + ".easycc-old"
                _relink(fixed, dst)
            old_dirs.append(cur)
        for d in old_dirs:
            _emit(job, "▸ 删除旧位置的数据 %s" % d)
            shutil.rmtree(d, ignore_errors=True)
        for it in plan["items"]:                # 旧位置留下的空「easyCC」文件夹也删掉
            par = os.path.dirname(it["cur"] or "")
            if os.path.basename(par).lower() == "easycc" and it["cur"] != it["fixed"]:
                try:
                    os.rmdir(par)
                except OSError:
                    pass
        _emit(job, "✓ 存储位置：%s" % (plan["target"] or "默认位置"))
        return 0
    except Exception as exc:  # noqa: BLE001
        _emit(job, "出错：%s" % exc)
        return 1
    finally:
        _store_cache["t"] = 0
        _start_runtime(job)
        for n in was_running:
            start_job("start", n)
        if was_running:
            _emit(job, "▸ 已重新启动原来在运行的环境：%s" % "、".join(was_running))


def _storage_clean(job):
    """清理构建缓存和没人用的旧镜像（环境、正在用的镜像都不动）"""
    before = storage_info(force=True)
    rt = runtime()
    if rt == "container":
        _emit(job, "▸ 删除构建缓存（构建用的 buildkit 容器，下次本机构建时自动重建）")
        subprocess.run([_container_cli(), "builder", "stop"], capture_output=True, timeout=120)
        p = subprocess.run([_container_cli(), "builder", "delete"], capture_output=True, text=True, timeout=300)
        if p.returncode != 0 and "not found" not in (p.stderr + p.stdout).lower():
            subprocess.run([_container_cli(), "rm", "-f", "buildkit"], capture_output=True, timeout=300)
        _emit(job, "▸ 删除旧版本的镜像")
        p = subprocess.run([_container_cli(), "image", "prune"], capture_output=True, text=True, timeout=600)
        _emit(job, (p.stdout + p.stderr).strip()[-300:])
    elif rt in ("docker", "podman", "vm"):
        if rt == "vm" and not vm_up():
            _emit(job, "easyCC 虚拟机没开，不用清理")
            return 0
        b = rt_bin(rt)
        _emit(job, "▸ 删除构建缓存")
        p = subprocess.run([b, "builder", "prune", "-af"], capture_output=True, text=True, timeout=600)
        _emit(job, (p.stdout + p.stderr).strip()[-200:])
        _emit(job, "▸ 删除旧版本的镜像")
        p = subprocess.run([b, "image", "prune", "-f"], capture_output=True, text=True, timeout=600)
        _emit(job, (p.stdout + p.stderr).strip()[-200:])
    cache = os.path.join(BASE, "_image.tar")
    if os.path.exists(cache):
        os.unlink(cache)
    after = storage_info(force=True)
    a, b = sum(i["bytes"] for i in before["items"]), sum(i["bytes"] for i in after["items"])
    _emit(job, "✓ 清理完成，释放 %s" % _gb(max(0, a - b)))
    return 0


# ---------------- Clash 订阅（手机用同一个代理出口） ----------------
# 按环境（或代理库里的代理）生成 Clash（mihomo）配置：所有流量走这个出口，域名交给代理那头解析（不漏 DNS）。
# 手机扫码导入订阅链接：链接在局域网里提供（本机的 Wi-Fi / 有线网卡 IP，不是虚拟机和本机之间的地址），
# 路径带随机令牌，只提供 /sub/<令牌>，别的一律 404；每个环境的链接固定不变（手机能定时更新），内容按当前代理现生成。

SUBS_FILE = os.path.join(BASE, "subs.json")
_sub_srv = {"server": None, "port": 0}
_VIRT_IF = re.compile(r"^(lo|bridge1\d\d|vmnet|vboxnet|docker|br-|veth|virbr|podman|cni|flannel|utun|tun|tap|wg|tailscale|zt|ipsec|ppp|awdl|llw|anpi|ap\d|gif|stf)")


def lan_ips():
    """本机在局域网里的 IPv4（排除回环、虚拟机/容器网桥、VPN 隧道、Clash 的假 IP 段）"""
    cands = []
    if IS_MAC or not shutil.which("ip"):
        out = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout if shutil.which("ifconfig") else ""
        iface = ""
        for line in out.splitlines():
            m = re.match(r"^(\S+?):? ", line)
            if m and not line.startswith(("\t", " ")):
                iface = m.group(1)
            m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", line)
            if m:
                cands.append((iface, m.group(1)))
    else:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            m = re.match(r"^\d+:\s+(\S+)\s+inet (\d+\.\d+\.\d+\.\d+)", line)
            if m:
                cands.append((m.group(1), m.group(2)))
    res = []
    for iface, ip in cands:
        if _VIRT_IF.match(iface) or ip.startswith(("127.", "169.254.", "198.18.", "198.19.")):
            continue
        a, b = (int(x) for x in ip.split(".")[:2])
        if not (a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or (a == 100 and 64 <= b <= 127)):
            continue                                   # 公网地址的网卡（少见）不拿来给手机用
        res.append({"iface": iface, "ip": ip})
    # 默认路由那块网卡排前面
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 53))
        prim = s.getsockname()[0]
        s.close()
        res.sort(key=lambda r: r["ip"] != prim)
    except OSError:
        pass
    return res


def _host_gateway_ips():
    """虚拟机 / 容器眼里的「本机」地址（环境的代理填了它，其实就是这台电脑上的代理）"""
    ips = {"host.internal", "host.docker.internal", "host.containers.internal", "127.0.0.1", "localhost"}
    out = subprocess.run(["ifconfig"] if IS_MAC else ["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True).stdout \
        if (IS_MAC or shutil.which("ip")) else ""
    for m in re.finditer(r"^(\S+?):?\s.*?(?:\n\s.*?)*?inet (\d+\.\d+\.\d+\.\d+)" if IS_MAC else r"^\d+:\s+(\S+)\s+inet (\d+\.\d+\.\d+\.\d+)", out, re.M):
        if re.match(r"^(bridge1\d\d|vmnet|docker|br-|virbr|podman|cni)", m.group(1)):
            ips.add(m.group(2))
    if IS_MAC:
        ips.add("192.168.64.1")            # Apple container / Virtualization 的默认网关
    return ips


def _yq(v):
    return json.dumps(str(v), ensure_ascii=False)     # JSON 字符串就是合法的 YAML 字符串


def _clash_proxy_lines(name, p, dialer=""):
    t = "http" if p["type"] == "http" else "socks5"
    L = ["  - name: %s" % _yq(name), "    type: %s" % t, "    server: %s" % _yq(p["host"]), "    port: %d" % int(p["port"])]
    if p.get("user"):
        L += ["    username: %s" % _yq(p["user"]), "    password: %s" % _yq(p.get("pass", ""))]
    if t == "socks5":
        L.append("    udp: true")
    if dialer:
        L.append("    dialer-proxy: %s" % _yq(dialer))
    return L


def clash_source(kind, ident):
    """(标题, 出口代理, 中转代理或 None, 出口说明)"""
    if kind == "env":
        if not NAME_RE.match(ident or "") or not os.path.isfile(os.path.join(inst_dir(ident), "meta.env")):
            return None
        conf = read_env(os.path.join(inst_dir(ident), "conf", "config.env"))
        prof = read_json(os.path.join(inst_dir(ident), "profile.json"), {})
        if not conf.get("PROXY_URL"):
            return None
        ex = prof.get("exit") or {}
        relay = parse_proxy(conf["RELAY_URL"]) if conf.get("RELAY_URL") else None
        return ident, parse_proxy(conf["PROXY_URL"]), relay, ex
    for p in proxies():
        if p.get("id") == ident:
            return (p.get("name") or "%s:%s" % (p["host"], p["port"])), dict(p), None, (p.get("check") or {})
    return None


def clash_config(kind, ident, lan_ip=""):
    src = clash_source(kind, ident)
    if not src:
        return None, []
    title, px, relay, ex = src
    warn, gw = [], _host_gateway_ips()
    for p in [px] + ([relay] if relay else []):
        if p["host"] in gw:
            if lan_ip:
                warn.append("代理地址 %s 是虚拟机看到的本机地址，已换成本机的局域网地址 %s；这个代理要监听局域网，手机才连得上" % (p["host"], lan_ip))
                p["host"] = lan_ip
            else:
                warn.append("代理地址 %s 是本机地址，手机连不上" % p["host"])
        elif re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", p["host"]):
            warn.append("代理地址 %s 是内网地址，手机要在同一个网络里才连得上" % p["host"])
    name = "easyCC · %s" % title
    where = " · ".join(x for x in (ex.get("ip", ""), ex.get("city", ""), ex.get("country", "") or ex.get("cc", "")) if x)
    L = ["# easyCC 生成：%s 的出口%s" % (title, "（%s）" % where if where else ""),
         "# 所有流量走这个出口，和 easyCC 环境用的是同一个出口 IP；域名交给代理那头解析，不漏 DNS",
         "# 生成时间 %s" % time.strftime("%Y-%m-%d %H:%M"),
         "mixed-port: 7890", "allow-lan: false", "mode: rule", "log-level: warning", "ipv6: false", "unified-delay: true",
         "dns:", "  enable: true", "  ipv6: false", "  enhanced-mode: fake-ip", "  fake-ip-range: 198.18.0.1/16",
         "  nameserver: [\"https://1.1.1.1/dns-query\", \"https://8.8.8.8/dns-query\"]",
         "  proxy-server-nameserver: [\"223.5.5.5\", \"119.29.29.29\"]",
         "proxies:"]
    if relay:
        L += _clash_proxy_lines("中转 · %s" % title, relay)
        warn.append("这个出口经中转代理连接（链式代理，用 dialer-proxy），需要 Clash Meta（mihomo）内核的客户端：Clash Verge Rev、Mihomo Party、FlClash、Stash 等")
    L += _clash_proxy_lines(name, px, ("中转 · %s" % title) if relay else "")
    L += ["proxy-groups:", "  - name: easyCC", "    type: select", "    proxies: [%s]" % _yq(name),
          "rules:",
          "  - IP-CIDR,127.0.0.0/8,DIRECT,no-resolve", "  - IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
          "  - IP-CIDR,172.16.0.0/12,DIRECT,no-resolve", "  - IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
          "  - IP-CIDR,169.254.0.0/16,DIRECT,no-resolve",
          "  - MATCH,easyCC"]
    return "\n".join(L) + "\n", warn


def _subs():
    return read_json(SUBS_FILE, {})


def sub_token(kind, ident, reset=False):
    subs = _subs()
    key = "%s:%s" % (kind, ident)
    tok = next((t for t, v in subs.items() if v.get("key") == key), None)
    if reset and tok:
        subs.pop(tok)
        tok = None
    if not tok:
        tok = secrets.token_urlsafe(18)
        subs[tok] = {"key": key, "createdAt": int(time.time())}
        write_json(SUBS_FILE, subs)
    return tok


class _SubHandler(BaseHTTPRequestHandler):
    server_version = "easycc"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        m = re.fullmatch(r"/sub/([A-Za-z0-9_-]{20,40})(?:\.yaml)?", urlsplit(self.path).path)
        v = _subs().get(m.group(1)) if m else None
        body, title = None, ""
        if v:
            kind, ident = v["key"].split(":", 1)
            body, _ = clash_config(kind, ident, self.server.server_address[0] if self.server.server_address[0] != "0.0.0.0" else
                                   (self.headers.get("Host", "").split(":")[0]))
            src = clash_source(kind, ident)
            title = src[0] if src else ""
        if not body:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/yaml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # 客户端里显示的订阅名；每 12 小时自动更新一次
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote("easyCC-%s.yaml" % title))
        self.send_header("profile-update-interval", "12")
        self.end_headers()
        self.wfile.write(data)


def sub_server_port():
    """局域网订阅服务：只提供 /sub/<令牌>。有订阅链接时才开（启动时、生成链接时）"""
    if _sub_srv["server"]:
        return _sub_srv["port"]
    want = int(settings().get("subPort") or 17790)
    for port in [want] + list(range(17791, 17810)):
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", port), _SubHandler)
        except OSError:
            continue
        _sub_srv.update(server=srv, port=port)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        if port != settings().get("subPort"):
            save_settings({"subPort": port})
        return port
    return 0


def clash_share(kind, ident, lan_ip="", reset=False):
    ips = lan_ips()
    ip = lan_ip if lan_ip and any(i["ip"] == lan_ip for i in ips) else (ips[0]["ip"] if ips else "")
    text, warn = clash_config(kind, ident, ip)
    if not text:
        return {"ok": False, "message": "没找到这个代理" if kind == "proxy" else "这个环境没有设置代理"}
    tok = sub_token(kind, ident, reset)
    port = sub_server_port() if ip else 0
    url = "http://%s:%d/sub/%s.yaml" % (ip, port, tok) if ip and port else ""
    if not ip:
        warn.append("没找到这台电脑的局域网地址（没连 Wi-Fi / 网线？），只能保存文件再传到手机")
    title = clash_source(kind, ident)[0]
    return {"ok": True, "yaml": text, "url": url, "ips": ips, "ip": ip, "warn": warn, "title": title,
            "filename": re.sub(r"[^\w.-]+", "_", "easyCC-%s.yaml" % title)}


def clash_save(kind, ident, lan_ip=""):
    r = clash_share(kind, ident, lan_ip)
    if not r.get("ok"):
        return r
    d = os.path.expanduser("~/Downloads")
    if IS_WSL:
        d = host_path(subprocess.run(["wslpath", "-u", "%USERPROFILE%"], capture_output=True, text=True).stdout.strip() or d)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, r["filename"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(r["yaml"])
    if IS_MAC:
        subprocess.Popen(["open", "-R", path])
    return {"ok": True, "message": "已保存到 %s" % short_path(path), "path": path}


# ---------------- HTTP ----------------

def token():
    return settings()["token"]


REDIRECT_PAGE = ('<!doctype html><meta charset="utf-8"><title>easyCC</title>'
                 '<body style="background:#0a0d16"><script>location.replace(%s)</script>'
                 '<noscript><meta http-equiv="refresh" content="0;url=%s"></noscript>')


class Handler(BaseHTTPRequestHandler):
    server_version = "cbox-manager"

    def log_message(self, fmt, *args):
        pass

    def authed(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        v = c["cbox_mgr"].value if "cbox_mgr" in c else ""
        return bool(v) and hmac.compare_digest(v, token())

    def send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        if u.path == "/login":
            k = (q.get("k") or [""])[0]
            if k and hmac.compare_digest(k, token()):
                # Lax：书签从别的网站跳过来（顶层 GET）时也带上登录；所有写操作都要自定义请求头，跨站发不了
                nxt = (q.get("next") or ["/"])[0]
                if not re.match(r"^/(open\?|$|#)", nxt):
                    nxt = "/"
                # 200 页面里设 Cookie 再跳转：WebKit（App 窗口、Safari）不保留 303 跳转响应里设的 Cookie
                return self.send(200, REDIRECT_PAGE % (json.dumps(nxt), json.dumps(nxt)), "text/html; charset=utf-8",
                                 headers={"Set-Cookie": "cbox_mgr=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000" % k})
            return self.send(403, "请从 easycc ui 打开客户端", "text/plain; charset=utf-8")
        if not self.authed():
            return self.send(403, "请从 easycc ui 打开客户端", "text/plain; charset=utf-8")
        if u.path == "/qrcode.js":
            return self.send(200, open(os.path.join(HERE, "web", "qrcode.js"), encoding="utf-8").read(), "text/javascript; charset=utf-8")
        if u.path in ("/", "/open"):
            return self.send(200, open(os.path.join(HERE, "web", "index.html"), encoding="utf-8").read(), "text/html; charset=utf-8")
        if u.path.startswith("/download/"):
            cleanup_transfers()
            item = _downloads.pop(u.path.rsplit("/", 1)[1], None)
            if not item or not os.path.exists(item[0]):
                return self.send(404, "下载链接已失效，请重新导出", "text/plain; charset=utf-8")
            path, fname, _ = item
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.path.getsize(path)))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote(fname))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
            os.remove(path)
            return
        if u.path == "/api/envs":
            return self.send(200, list_envs())
        if u.path == "/api/proxies":
            return self.send(200, {"proxies": [proxy_public(p) for p in proxies()]})
        if u.path == "/api/jobs":
            return self.send(200, {"jobs": jobs_payload()})
        if u.path.startswith("/api/jobs/"):
            j = job_detail(u.path.rsplit("/", 1)[1])
            return self.send(200 if j else 404, j or {"error": "没有这个任务"})
        if u.path == "/api/cloud":
            return self.send(200, cloud_info())
        if u.path == "/api/cloud/envs":
            return self.send(200, cloud_envs())
        if u.path == "/api/cloud/plans":
            return self.send(200, cloud_call(cloud().plans))
        if u.path == "/api/system":
            return self.send(200, system_info())
        m = re.match(r"^/api/envs/([A-Za-z0-9_-]+)/kiteline$", u.path)
        if m and m.group(1) in env_names():
            return self.send(200, kiteline_info(m.group(1), "renew=1" in (u.query or "")))
        m = re.match(r"^/api/envs/([A-Za-z0-9_-]+)/ports$", u.path)
        if m and m.group(1) in env_names():
            return self.send(200, ports_info(m.group(1)))
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.authed() or self.headers.get("X-CBox") != "1":
            return self.send(403, {"error": "未授权"})
        n = int(self.headers.get("Content-Length") or 0)
        if urlsplit(self.path).path == "/api/import/inspect":
            cleanup_transfers()
            try:
                path = cbox_transfer.spool_upload(self.rfile, n)
            except ValueError as exc:
                return self.send(200, {"ok": False, "message": str(exc)})
            return self.send(200, import_inspect(path))
        try:
            b = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            b = {}
        path = urlsplit(self.path).path
        m = re.match(r"^/api/envs/([A-Za-z0-9_-]+)/([a-z-]+)$", path)
        if path == "/api/envs":
            return self.send(200, create_env(b))
        if path == "/api/envs/batch":
            names = [x for x in b.get("names", []) if x in env_names()]
            action = b.get("action")
            if action not in ("start", "stop", "restart", "rm"):
                return self.send(200, {"ok": False, "message": "未知操作"})
            for x in names:
                start_job(action, x)
            return self.send(200, {"ok": True, "message": "已加入任务队列：%d 个环境" % len(names)})
        if m:
            name, act = m.groups()
            if name not in env_names():
                return self.send(200, {"ok": False, "message": "环境不存在"})
            if act in ("start", "stop", "restart", "rm"):
                return self.send(200, {"ok": True, "job": start_job(act, name)})
            if act == "edit":
                return self.send(200, edit_env(name, b))
            if act == "clone":
                return self.send(200, clone_env(name, b))
            if act == "terminal":
                return self.send(200, open_terminal(name, b.get("what", "shell")))
            if act == "vscode":
                return self.send(200, open_vscode(name))
            if act == "check":
                conf = read_env(os.path.join(inst_dir(name), "conf", "config.env"))
                r = check_proxy(parse_proxy(conf.get("PROXY_URL", "")))
                if r.get("ok"):
                    save_profile(name, {"exit": {"ip": r["ip"], "country": r["country"], "location": ", ".join(x for x in (r["city"], r["countryName"]) if x),
                                                 "timezone": r["timezone"], "at": r["at"]}})
                return self.send(200, r)
            if act == "open-url":
                url = str(b.get("url", "")).strip()
                if not re.match(r"^https?://\S+$", url):
                    return self.send(200, {"ok": False, "message": "只能打开 http:// 或 https:// 开头的链接"})
                return self.send(200, {"ok": True, "job": start_job("open-url", name, url)})
            if act == "persona-reset":
                return self.send(200, persona_reset(name))
            if act == "hostaccess":
                d = inst_dir(name)
                up = {}
                if "access" in b:
                    up["HOST_ACCESS"] = "1" if b["access"] else "0"
                    if not b["access"]:
                        up["HOST_SSH"] = "0"
                if "ssh" in b:
                    up["HOST_SSH"] = "1" if b["ssh"] else "0"
                    if b["ssh"]:
                        up["HOST_ACCESS"] = "1"
                update_env(os.path.join(d, "conf", "config.env"), up)
                if ("easycc-" + name) in running_names()[0]:
                    code, out = rt_exec(name, ["/usr/local/sbin/cbox-hostaccess", "sync", "--wait"], 60)
                    if code != 0:
                        return self.send(200, {"ok": False, "message": "设置失败：%s" % out[-200:]})
                on = up.get("HOST_ACCESS") == "1"
                return self.send(200, {"ok": True, "message": ("已授权 %s 访问本机：环境里 host.internal:端口 = 本机 localhost:端口" % name) if on else "已撤销 %s 访问本机" % name})
            if act == "ports":
                if b.get("action"):
                    return self.send(200, ports_change(name, b))
                return self.send(200, ports_info(name))
            if act == "export":
                return self.send(200, export_env_api(name, b))
            if act == "cloud-push":
                if not cloud().logged_in():
                    return self.send(200, {"ok": False, "message": "先登录 easyCC 账号"})
                return self.send(200, {"ok": True, "job": start_job("cloud-push", name, "force" if b.get("force") else "")})
            if act == "opened":
                save_profile(name, {"lastOpened": int(time.time())})
                return self.send(200, {"ok": True})
        if path == "/api/cloud/code":
            return self.send(200, cloud_call(cloud().send_code, str(b.get("email", "")).strip()))
        if path == "/api/cloud/login":
            r = cloud_call(cloud().login, str(b.get("email", "")).strip(), str(b.get("code", "")).strip())
            if r.get("ok"):
                r.pop("token", None)
                threading.Thread(target=cloud_report, daemon=True).start()
            return self.send(200, r)
        if path == "/api/cloud/logout":
            cloud().logout()
            return self.send(200, {"ok": True})
        if path == "/api/cloud/me":
            return self.send(200, cloud_call(cloud().me))
        if path == "/api/cloud/url":
            u2 = str(b.get("url", "")).strip().rstrip("/")
            if u2 and not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d+)?$", u2):
                return self.send(200, {"ok": False, "message": "地址格式不对，例如 https://easycc.betac.cn"})
            cloud().save(url=u2)
            return self.send(200, {"ok": True, "message": "已保存"})
        if path == "/api/cloud/report":
            return self.send(200, cloud_report())
        if path == "/api/cloud/redeem":
            return self.send(200, cloud_call(cloud().redeem, str(b.get("code", ""))[:64]))
        m = re.match(r"^/api/cloud/envs/([A-Za-z0-9_-]+)/(fetch|delete)$", path)
        if m:
            if m.group(2) == "fetch":
                return self.send(200, cloud_fetch(m.group(1)))
            return self.send(200, cloud_call(cloud().delete, m.group(1)))
        if path == "/api/import/commit":
            return self.send(200, import_commit(b))
        if path == "/api/proxies":
            return self.send(200, add_proxies(b.get("text", ""), b.get("name", ""), b.get("group", "")))
        if path == "/api/proxies/check":
            return self.send(200, check_proxies(b.get("ids") or []))
        if path == "/api/proxies/test":
            px = b.get("proxy") or {}
            if b.get("env") and not px.get("pass"):
                cur = parse_proxy(read_env(os.path.join(inst_dir(b["env"]), "conf", "config.env")).get("PROXY_URL", ""))
                if cur["user"] == px.get("user", ""):
                    px["pass"] = cur["pass"]
            if b.get("proxyId"):
                px = next((p for p in proxies() if p["id"] == b["proxyId"]), px)
            return self.send(200, check_proxy(px))
        if path == "/api/proxies/delete":
            ids = set(b.get("ids") or [])
            save_proxies([p for p in proxies() if p["id"] not in ids])
            return self.send(200, {"ok": True, "message": "已删除 %d 个代理" % len(ids)})
        if path == "/api/proxies/update":
            lst = proxies()
            for p in lst:
                if p["id"] == b.get("id"):
                    for k in ("name", "group"):
                        if k in b:
                            p[k] = str(b[k])[:60]
            save_proxies(lst)
            return self.send(200, {"ok": True})
        if path == "/api/pick-folder":
            return self.send(200, pick_folder(b.get("start", ""), str(b.get("title") or "选择项目目录")[:40]))
        if path == "/api/build":
            return self.send(200, {"ok": True, "job": start_job("build")})
        if path == "/api/image/pull":
            if not settings().get("imageURL"):
                return self.send(200, {"ok": False, "message": "还没有设置镜像下载地址"})
            return self.send(200, {"ok": True, "job": start_job("image-pull")})
        if path in ("/api/clash", "/api/clash/save"):
            kind = "proxy" if b.get("proxy") else "env"
            ident = str(b.get("proxy") or b.get("env") or "")
            if path == "/api/clash/save":
                return self.send(200, clash_save(kind, ident, str(b.get("ip") or "")))
            return self.send(200, clash_share(kind, ident, str(b.get("ip") or ""), bool(b.get("reset"))))
        if path == "/api/storage":
            return self.send(200, storage_info(force=bool(b.get("refresh"))))
        if path == "/api/storage/check":
            msg, plan = storage_plan(b.get("target", ""))
            names = sorted(n[len("easycc-"):] for n in running_names()[0] if n.startswith("easycc-"))
            return self.send(200, {"ok": bool(plan), "message": msg, "target": plan and plan["target"], "need": plan and plan["need"], "running": names})
        if path == "/api/storage/move":
            msg, plan = storage_plan(b.get("target", ""))
            if not plan:
                return self.send(200, {"ok": False, "message": msg})
            return self.send(200, {"ok": True, "job": start_job("storage-move", "", b.get("target", ""))})
        if path == "/api/storage/clean":
            return self.send(200, {"ok": True, "job": start_job("storage-clean")})
        if path == "/api/settings":
            up = {}
            if b.get("runtime") in ("auto", "docker", "podman", "container", "vm"):
                up["runtime"] = b["runtime"]
            if b.get("arch") in ("amd64", "arm64"):
                up["arch"] = b["arch"]
            if isinstance(b.get("elastic"), bool):
                up["elastic"] = b["elastic"]
            if b.get("autoSleep") in (0, 15, 30, 60, 120):
                up["autoSleep"] = b["autoSleep"]
            if isinstance(b.get("telemetry"), bool):
                up["telemetry"] = b["telemetry"]
            if "imageURL" in b:
                u = str(b["imageURL"]).strip()
                if u and not re.match(r"^https?://\S+$", u):
                    return self.send(200, {"ok": False, "message": "镜像地址要以 http:// 或 https:// 开头"})
                up["imageURL"] = u
            if "imageSHA256" in b:
                h = str(b["imageSHA256"]).strip().lower()
                if h and not re.fullmatch(r"[0-9a-f]{64}", h):
                    return self.send(200, {"ok": False, "message": "SHA-256 应该是 64 位十六进制"})
                up["imageSHA256"] = h
            save_settings(up)
            _state_cache["t"] = 0
            return self.send(200, {"ok": True, "message": "已保存"})
        self.send(404, {"error": "not found"})


def free_port(start):
    for p in range(start, start + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit("找不到空闲端口")


def main():
    if os.path.islink(BASE) and not os.path.exists(BASE):
        sys.exit("easyCC 的存储位置不可用：%s（外置盘没接上？接上后重新打开）" % os.readlink(BASE))
    os.makedirs(BASE, exist_ok=True)
    os.chmod(BASE, 0o700)
    saved = settings().get("port") or 17680
    port = int(os.environ.get("CBOX_MANAGER_PORT") or 0) or free_port(saved)
    if not os.environ.get("CBOX_MANAGER_PORT") and port != settings().get("port"):
        save_settings({"port": port})
    url = "http://127.0.0.1:%d/login?k=%s" % (port, token())
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    with open(os.path.join(BASE, "manager.url"), "w") as f:
        f.write(url)
    os.chmod(os.path.join(BASE, "manager.url"), 0o600)
    print("easycc 客户端：%s" % url, flush=True)
    if not FAKE or os.environ.get("EASYCC_CLOUD_URL"):
        threading.Thread(target=_cloud_loop, daemon=True).start()
    if _subs():
        threading.Thread(target=sub_server_port, daemon=True).start()   # 手机上的订阅要能随时更新
    threading.Thread(target=_pkg_loop, daemon=True).start()
    threading.Thread(target=_kl_loop, daemon=True).start()
    threading.Thread(target=_elastic_loop, daemon=True).start()
    if "--no-open" not in sys.argv:
        threading.Thread(target=open_window, args=(url,), daemon=True).start()
    server.serve_forever()


def open_window(url):
    """尽量以「应用窗口」打开（没有地址栏、标签页，像独立客户端），否则用默认浏览器"""
    time.sleep(0.3)
    candidates = []
    if IS_MAC:
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium", "Arc"):
            if os.path.isdir("/Applications/%s.app" % app) or os.path.isdir(os.path.expanduser("~/Applications/%s.app" % app)):
                candidates.append(["open", "-na", app, "--args", "--app=" + url, "--window-size=1440,900",
                                   "--user-data-dir=" + os.path.join(BASE, ".ui-profile")])
    else:
        for b in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"):
            if shutil.which(b):
                candidates.append([b, "--app=" + url, "--window-size=1440,900", "--user-data-dir=" + os.path.join(BASE, ".ui-profile")])
    for c in candidates:
        try:
            subprocess.Popen(c, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            continue
    webbrowser.open(url)


if __name__ == "__main__":
    main()
