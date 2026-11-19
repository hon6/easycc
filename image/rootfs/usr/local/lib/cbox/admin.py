"""easyCC 管理后台（root 运行，容器内 7681 端口，主机上只映射到 127.0.0.1）。
引导页、出口 IP、上游代理实测、环境干净度检查、Kiteline 配对码、一键重置。只用标准库（二维码用系统的 python3-qrcode）。"""
import hmac
import io
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.request
from urllib.parse import quote, unquote
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agents  # noqa: E402

RUN = os.environ.get("CBOX_RUN", "/run/cbox")
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 7681
PROXY = "http://127.0.0.1:7890"
APP_STORE = "https://apps.apple.com/app/id6825244029"
KITELINE_CFG = "/home/dev/.config/kiteline/config.json"
CONF_FILE = os.environ.get("CBOX_CONF", "/etc/cbox/config.env")
PROJECTS = os.environ.get("CBOX_PROJECTS", "/projects")


def load_env(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
                if m:
                    v = m.group(2).strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                        v = v[1:-1]
                    out[m.group(1)] = v
    except OSError:
        pass
    return out


def conf():
    c = {k: v for k, v in os.environ.items() if k.isupper()}
    c.update(load_env(CONF_FILE))
    return c


def token():
    t = conf().get("ADMIN_TOKEN", "")
    if t:
        return t
    p = os.path.join(RUN, "admin-token")
    try:
        return open(p).read().strip()
    except OSError:
        t = secrets.token_urlsafe(18)
        with open(p, "w") as f:
            f.write(t)
        os.chmod(p, 0o600)
        print("管理后台令牌：%s" % t, flush=True)
        return t


def sh(cmd, timeout=20, user=None, stdin=None):
    """运行命令，返回 (退出码, 输出)。user=dev / mihomo 时降权运行"""
    if user == "dev":
        cmd = ["runuser", "-u", "dev", "--"] + cmd
    elif user == "mihomo":
        cmd = ["setpriv", "--reuid", "mihomo", "--regid", "mihomo", "--init-groups"] + cmd
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError as exc:
        return 127, str(exc)


def curl(url, proxy=True, timeout=12, user=None, extra=()):
    cmd = ["curl", "-q", "-fsS", "--max-time", str(timeout)] + (["-x", PROXY] if proxy else ["--noproxy", "*"]) + list(extra) + [url]
    return sh(cmd, timeout + 3, user)


def get_json(url, timeout=12):
    code, out = curl(url, timeout=timeout)
    if code != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def exit_ip(proxy=True, user=None):
    for u in ("https://api4.ipify.org", "https://ipv4.icanhazip.com", "https://ipinfo.io/ip", "http://ip-api.com/line/?fields=query",
              "https://api.ipify.org", "https://ifconfig.me/ip"):
        code, out = curl(u, proxy=proxy, user=user, timeout=10)
        if code == 0 and re.fullmatch(r"[0-9a-fA-F.:]+", out):
            return out
    return ""


def mihomo_secret():
    try:
        m = re.search(r"^secret:\s*(\S+)", open("/etc/mihomo/config.yaml").read(), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


def mihomo_delay():
    url = "http://127.0.0.1:9090/proxies/exit/delay?timeout=6000&url=https%3A%2F%2Fwww.gstatic.com%2Fgenerate_204"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + mihomo_secret()})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.load(r).get("delay")
    except Exception:  # noqa: BLE001
        return None


# ---------------- 状态 ----------------

def status():
    c = conf()
    st = load_env(os.path.join(RUN, "status.env"))
    state = open(os.path.join(RUN, "state")).read().strip() if os.path.exists(os.path.join(RUN, "state")) else "unknown"
    _, mihomo = sh(["pgrep", "-x", "mihomo"])
    _, claude_v = sh(["bash", "-lc", "claude --version 2>/dev/null"], 10, "dev")
    installing = False
    os_name = load_env("/etc/os-release").get("PRETTY_NAME", "")
    try:
        proxy = json.load(open(os.path.join(RUN, "proxy.json")))
        proxy_desc = "%s://%s%s:%s" % (proxy["type"], (proxy["user"] + ":***@") if proxy.get("user") else "", proxy["host"], proxy["port"])
    except (OSError, ValueError, KeyError):
        proxy_desc = ""
    return {
        "state": state, "killswitch": os.path.exists(os.path.join(RUN, "killswitch")), "gate": gate_state(),
        "exitIP": st.get("EXIT_IP", ""), "location": st.get("LOCATION", ""), "country": st.get("COUNTRY", ""),
        "timezone": st.get("TIMEZONE", ""), "acceptLanguage": st.get("ACCEPT_LANGUAGE", ""), "started": st.get("STARTED", ""),
        "expectIP": c.get("EXPECT_IP", ""), "proxy": proxy_desc, "mihomo": bool(mihomo),
        "claude": claude_v or ("安装中…" if installing else ""), "os": os_name, "arch": os.uname().machine,
        "hostname": os.uname().nodename, "kiteline": os.path.exists("/usr/local/bin/kiteline"),
        "instance": c.get("INSTANCE", ""), "hostDir": c.get("HOST_DIR", ""),
        "vncPort": c.get("HOST_VNC_PORT", ""), "sshPort": c.get("HOST_SSH_PORT", ""), "vncPassword": c.get("VNC_PASSWORD", ""),
        "runtime": c.get("CBOX_RUNTIME", ""), "applied": st.get("APPLIED", ""),
        "project": current_project(), "hostRoot": c.get("HOST_ROOT", "") or c.get("HOST_DIR", ""),
        "uptime": int(time.time() - os.stat("/proc/1").st_ctime) if os.path.exists("/proc/1") else 0,
    }


# ---------------- 配置读写（/etc/cbox/config.env，重建容器后保留）----------------

def write_conf(updates):
    """按键更新 config.env，其它行原样保留；值一律单引号包起来"""
    lines = []
    try:
        lines = open(CONF_FILE, encoding="utf-8").read().splitlines()
    except OSError:
        pass
    keys = set(updates)
    out = [l for l in lines if not re.match(r"\s*(%s)=" % "|".join(map(re.escape, keys)), l)]
    for k, v in updates.items():
        out.append("%s='%s'" % (k, str(v).replace("'", "'\\''")))
    os.makedirs(os.path.dirname(CONF_FILE), exist_ok=True)
    with open(CONF_FILE + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(CONF_FILE + ".tmp", 0o600)
    os.replace(CONF_FILE + ".tmp", CONF_FILE)


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
    return "%s://%s%s:%s" % (d["type"], auth, d["host"], d["port"])


def get_config():
    c = conf()
    p = parse_proxy(c.get("PROXY_URL", ""))
    return {"type": p["type"], "host": p["host"], "port": p["port"], "user": p["user"], "hasPass": bool(p["pass"]),
            "expectIP": c.get("EXPECT_IP", ""), "tzOverride": c.get("TZ_OVERRIDE", ""), "acceptLanguage": c.get("ACCEPT_LANGUAGE", ""),
            "exitRule": c.get("EXIT_RULE", "") or "country", "allowDirect": c.get("ALLOW_DIRECT_CIDRS", ""),
            "relay": mask_url(c.get("RELAY_URL", ""))}


def mask_url(url):
    """中转地址给页面看：密码换成 ***"""
    if not url:
        return ""
    p = parse_proxy(url)
    return build_proxy(dict(p, **{"pass": "***" if p["pass"] else ""}))


def relay_from_body(b):
    """中转代理：整串地址；留空 = 不用链式代理；带 *** 的表示沿用原来的密码"""
    v = str(b.get("relay", "") or "").strip()
    if not v:
        return ""
    if "://" not in v:
        v = "socks5://" + v
    p = parse_proxy(v)
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", p["host"] or "") or not 1 <= int(p["port"]) <= 65535:
        raise ValueError("中转代理地址不对，例如 socks5://用户名:密码@1.2.3.4:1080")
    if p["pass"] == "***":
        old = parse_proxy(conf().get("RELAY_URL", ""))
        p["pass"] = old["pass"] if old["host"] == p["host"] else ""
    return build_proxy(p)


def proxy_from_body(b):
    """校验表单；密码留空且 keepPass 时沿用现在的密码"""
    t = b.get("type", "socks5")
    host = str(b.get("host", "")).strip()
    try:
        port = int(b.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if t not in ("socks5", "http"):
        raise ValueError("代理类型只能是 SOCKS5 或 HTTP")
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
        raise ValueError("服务器地址不对：填 IP 或域名")
    if not 1 <= port <= 65535:
        raise ValueError("端口要在 1–65535 之间")
    user = str(b.get("user", ""))
    pw = str(b.get("pass", ""))
    if not pw and b.get("keepPass"):
        cur = parse_proxy(conf().get("PROXY_URL", ""))
        if cur["user"] == user:
            pw = cur["pass"]
    return {"type": t, "host": host, "port": port, "user": user, "pass": pw}


def test_proxy(b):
    try:
        d = proxy_from_body(b)
    except ValueError as exc:
        return {"ok": False, "steps": [{"name": "检查填写", "ok": False, "detail": str(exc)}]}
    try:
        relay = relay_from_body(b)
    except ValueError as exc:
        return {"ok": False, "steps": [{"name": "检查填写", "ok": False, "detail": str(exc)}]}
    # 链式代理时环境只能直连中转：测中转这一跳，落地节点在「保存并应用」后由联网检查验证
    code, out = sh(["/usr/local/sbin/cbox-net", "test", relay or build_proxy(d)], 60)
    try:
        return json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "steps": [{"name": "运行检测", "ok": False, "detail": out or "检测失败（%d）" % code}]}


_apply_lock = threading.Lock()


def apply_config(b):
    try:
        d = proxy_from_body(b)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    expect = str(b.get("expectIP", "")).strip()
    tz = str(b.get("tzOverride", "")).strip()
    lang = str(b.get("acceptLanguage", "")).strip()
    if expect and not re.fullmatch(r"[0-9a-fA-F.:]{3,45}", expect):
        return {"ok": False, "message": "预期出口 IP 格式不对"}
    if tz and (".." in tz or not os.path.isfile("/usr/share/zoneinfo/" + tz)):
        return {"ok": False, "message": "时区不存在，例如 America/Los_Angeles、Asia/Tokyo"}
    if lang and not re.fullmatch(r"[A-Za-z0-9 ,;=.\-]{1,100}", lang):
        return {"ok": False, "message": "浏览器语言格式不对，例如 en-US, en"}
    try:
        relay = relay_from_body(b)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    rule = str(b.get("exitRule", "") or conf().get("EXIT_RULE", "") or "country")
    if rule not in EXIT_RULES:
        return {"ok": False, "message": "出口变化规则不对"}
    if not _apply_lock.acquire(blocking=False):
        return {"ok": False, "message": "上一次应用还没完成"}
    try:
        c = conf()
        old = {k: c.get(k, "") for k in ("PROXY_URL", "EXPECT_IP", "TZ_OVERRIDE", "ACCEPT_LANGUAGE", "EXIT_RULE", "RELAY_URL")}
        write_conf({"PROXY_URL": build_proxy(d), "EXPECT_IP": expect, "TZ_OVERRIDE": tz, "ACCEPT_LANGUAGE": lang, "EXIT_RULE": rule,
                    "RELAY_URL": relay})
        code, out = sh(["/usr/local/sbin/cbox-net", "apply"], 200)
        if code != 0:
            reason = gate_reason() if code == 3 else (out.splitlines() or ["未知错误"])[-1]
            # 新配置不通：恢复原配置，避免把自己锁在外面
            write_conf(old)
            code2, _ = sh(["/usr/local/sbin/cbox-net", "apply"], 200)
            return {"ok": False, "message": "新配置不可用：%s。%s" % (reason, "已恢复原配置" if code2 == 0 else "恢复原配置也失败了，请检查网络"),
                    "restored": code2 == 0}
        after = load_env(os.path.join(RUN, "status.env"))
        restarted = after.get("BROWSER_RESTARTED") == "1"
        return {"ok": True, "message": "已应用：出口 IP %s，时区 %s%s" % (after.get("EXIT_IP", ""), after.get("TIMEZONE", ""),
                                                                    "，浏览器已按新时区 / 语言重启" if restarted else "")}
    finally:
        _apply_lock.release()


# ---------------- 联网闸门 ----------------

EXIT_RULES = ("country", "region", "city", "any")


def gate_state():
    try:
        g = json.load(open(os.path.join(RUN, "gate.json")))
    except (OSError, ValueError):
        g = {"open": False, "checks": [], "reason": "还没做过联网检查"}
    _, fw = sh(["/usr/local/sbin/cbox-firewall", "gate", "status"])
    g["open"] = fw == "open"            # 以防火墙实际状态为准
    g["paused"] = os.path.exists(os.path.join(RUN, "paused"))
    try:
        nxt = int(open(os.path.join(RUN, "gate-next")).read().strip())
        g["nextIn"] = max(0, nxt - int(time.time()))
    except (OSError, ValueError):
        pass
    try:
        g["base"] = json.load(open("/etc/cbox/exit-base.json"))
    except (OSError, ValueError):
        g["base"] = None
    c = conf()
    g["rule"] = "static" if c.get("EXPECT_IP") else (c.get("EXIT_RULE") or "country")
    return g


def gate_reason():
    try:
        return json.load(open(os.path.join(RUN, "gate.json"))).get("reason") or "联网检查没通过"
    except (OSError, ValueError):
        return "联网检查没通过"


def gate_action(act, body):
    if act == "recheck":
        if body.get("rebase"):              # 以当前出口为准：清掉地区基准再来
            try:
                os.remove("/etc/cbox/exit-base.json")
            except OSError:
                pass
        # 放行中只重做检查；关着时完整重新应用（可能是代理、时区要重设）
        is_open = sh(["/usr/local/sbin/cbox-firewall", "gate", "status"])[1] == "open"
        cmd = ["/usr/local/sbin/cbox-net", "recheck" if is_open and not body.get("rebase") else "apply"]
    elif act == "pause":
        cmd = ["/usr/local/sbin/cbox-net", "pause"]
    elif act == "resume":
        cmd = ["/usr/local/sbin/cbox-net", "resume"]
    else:
        return {"ok": False, "message": "未知操作"}
    code, out = sh(cmd, 200)
    g = gate_state()
    msg = {"pause": "外网已暂停：已有连接已断开，终端、文件照常可用"}.get(act) or (
        "联网检查全部通过，外网已放行" if code == 0 else g.get("reason") or (out.splitlines() or ["失败"])[-1])
    return {"ok": code == 0 or act == "pause", "message": msg, "gate": g}


# ---------------- 项目目录 ----------------

def current_project():
    try:
        t = os.readlink("/workspace")
    except OSError:
        return ""
    rel = os.path.relpath(t, PROJECTS)
    return "" if rel == "." else rel


def safe_rel(rel):
    rel = str(rel or "").strip("/")
    full = os.path.realpath(os.path.join(PROJECTS, rel))
    if full != PROJECTS and not full.startswith(PROJECTS + "/"):
        raise ValueError("只能选项目根目录里面的文件夹")
    return os.path.relpath(full, PROJECTS) if full != PROJECTS else "", full


def list_projects(rel):
    try:
        rel, full = safe_rel(rel)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    dirs = []
    try:
        with os.scandir(full) as it:
            for e in it:
                if e.name.startswith(".") or e.name in ("node_modules", "__pycache__") or not e.is_dir(follow_symlinks=True):
                    continue
                p = os.path.join(full, e.name)
                kinds = [k for k, f in (("git", ".git"), ("node", "package.json"), ("python", "pyproject.toml"),
                                        ("claude", "CLAUDE.md")) if os.path.exists(os.path.join(p, f))]
                try:
                    mtime = int(e.stat().st_mtime)
                except OSError:
                    mtime = 0
                dirs.append({"name": e.name, "kinds": kinds, "mtime": mtime})
                if len(dirs) >= 500:
                    break
    except OSError as exc:
        return {"ok": False, "message": "读不了这个目录：%s" % exc.strerror}
    dirs.sort(key=lambda x: x["name"].lower())
    c = conf()
    return {"ok": True, "path": rel, "dirs": dirs, "current": current_project(),
            "hostRoot": c.get("HOST_ROOT", "") or c.get("HOST_DIR", ""), "mounted": os.path.ismount(PROJECTS)}


def set_project(rel):
    try:
        rel, full = safe_rel(rel)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    code, out = sh(["/usr/local/sbin/cbox-project", "set", rel], 15)
    if code != 0:
        return {"ok": False, "message": out or "切换失败"}
    busy = sh(["pgrep", "-u", "dev", "-x", "claude"])[0] == 0
    return {"ok": True, "project": rel, "message": "当前项目：/projects/%s%s" % (rel, "。已经开着的 claude 还在旧目录，新开一个才会用新项目" if busy else "")}


def make_dir(rel, name):
    name = str(name or "").strip()
    if not re.fullmatch(r"[^/\\\x00]{1,120}", name) or name in (".", ".."):
        return {"ok": False, "message": "文件夹名不合法"}
    try:
        rel, full = safe_rel(rel)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    target = os.path.join(full, name)
    if os.path.exists(target):
        return {"ok": False, "message": "已经有同名文件夹"}
    code, out = sh(["mkdir", target], 10, "dev")
    return {"ok": code == 0, "message": "已创建 %s" % name if code == 0 else out or "创建失败"}


# ---------------- 环境浏览器 ----------------

def browser_open(url):
    url = str(url or "").strip()
    if not re.match(r"^https?://[^\s]+$", url):
        return {"ok": False, "message": "只能打开 http:// 或 https:// 开头的链接"}
    code, out = sh(["/usr/local/bin/cbox-browser", url], 20)
    return {"ok": code == 0, "message": "已在环境浏览器中打开" if code == 0 else (out or "打开失败")}


# ---------------- 访问本机 ----------------

def host_access_status():
    code, out = sh(["/usr/local/sbin/cbox-hostaccess", "status"], 10)
    try:
        return json.loads(out)
    except ValueError:
        return {"enabled": False, "ssh": False, "certs": [], "error": out}


def host_access_set(b):
    up = {}
    if "access" in b:
        up["HOST_ACCESS"] = "1" if b["access"] else "0"
        if not b["access"]:
            up["HOST_SSH"] = "0"
    if "ssh" in b:
        up["HOST_SSH"] = "1" if b["ssh"] else "0"
        if b["ssh"]:
            up["HOST_ACCESS"] = "1"
    if not up:
        return {"ok": False, "message": "没有要改的"}
    write_conf(up)
    code, out = sh(["/usr/local/sbin/cbox-hostaccess", "sync", "--wait"], 60)
    st = host_access_status()
    if code != 0:
        return dict(st, ok=False, message="设置失败：%s" % (out.splitlines()[-1] if out else code))
    msg = ("已授权访问本机：环境里访问 host.internal:端口 = 电脑本机的 localhost:端口（导入本地证书 %d 张）" % len(st.get("certs") or [])) if st.get("enabled") else "已撤销访问本机"
    if st.get("ssh"):
        msg += "；SSH 已授权：环境里 ssh host 即登录电脑本机"
    return dict(st, ok=True, message=msg)


# ---------------- 上游代理实测 ----------------

def check_proxy():
    try:
        raw = open(os.path.join(RUN, "proxy.json")).read()
    except OSError:
        return {"ok": False, "steps": [{"name": "读取代理配置", "ok": False, "detail": "容器还没完成启动"}]}
    code, out = sh(["python3", "-I", os.path.join(HERE, "probe.py")], 40, "mihomo", raw)
    try:
        res = json.loads(out)
    except ValueError:
        res = {"ok": False, "steps": [{"name": "运行检测", "ok": False, "detail": out or "检测程序出错（%d）" % code}]}
    res["delay"] = mihomo_delay()
    res["mihomo"] = sh(["pgrep", "-x", "mihomo"])[0] == 0
    return res


# ---------------- 环境干净度检查 ----------------

def check_env():
    items = []

    def add(name, level, detail, hint=""):
        items.append({"name": name, "level": level, "detail": detail, "hint": hint})

    code, out = sh(["/usr/local/sbin/cbox-firewall", "check"])
    add("防火墙", "pass" if code == 0 else "fail", "出站默认拒绝，只有 mihomo 能连上游代理" if code == 0 else out or "规则缺失",
        "" if code == 0 else "重启实例")

    code, _ = curl("https://1.1.1.1", proxy=False, timeout=5, user="mihomo")
    add("直连外网", "pass" if code != 0 else "fail", "已封锁（绕开代理直连 1.1.1.1 失败）" if code != 0 else "能直连，防火墙失效！")

    ip_proxy = exit_ip(True)
    ip_trans = exit_ip(False, "dev")
    same_family = (":" in ip_proxy) == (":" in ip_trans)
    if ip_proxy and (ip_trans == ip_proxy or (ip_trans and not same_family)):
        add("出口一致", "pass", "带代理和不带代理的程序出口都是 %s" % ip_proxy)
    else:
        add("出口一致", "fail", "代理出口 %s，透明转发出口 %s" % (ip_proxy or "获取失败", ip_trans or "获取失败"))

    code, _ = sh(["curl", "-q", "-6", "-fsS", "--noproxy", "*", "--max-time", "5", "https://api6.ipify.org"], 8, "dev")
    add("IPv6", "pass" if code != 0 else "fail", "已封锁" if code != 0 else "IPv6 能直连出去，会泄漏真实地址")

    _, out = sh(["dig", "+short", "+time=3", "+tries=1", "example.com"], 8, "dev")
    fake = out.splitlines()[-1] if out else ""
    if fake.startswith("198.18."):
        add("DNS 不泄漏", "pass", "系统 DNS 查询被 mihomo 接管（返回假 IP %s），真实查询经代理发出" % fake)
    else:
        add("DNS 不泄漏", "fail", "DNS 查询没有被接管：%s" % (out or "无结果"))

    dns = get_json("https://edns.ip-api.com/json")
    if dns and dns.get("dns"):
        add("DNS 出口", "info", "网站看到的 DNS 服务器：%s（%s）" % (dns["dns"].get("ip", ""), dns["dns"].get("geo", "")))

    geo = get_json("https://ipinfo.io/json") or {}
    tz_sys = open("/etc/timezone").read().strip() if os.path.exists("/etc/timezone") else ""
    tz_ip = geo.get("timezone", "")
    if tz_ip and tz_sys == tz_ip:
        add("时区匹配", "pass", "系统和浏览器时区 %s，与出口 IP 所在地一致" % tz_sys)
    elif tz_ip:
        add("时区匹配", "warn", "系统时区 %s，出口 IP 所在时区 %s" % (tz_sys, tz_ip), "重置网络会按出口 IP 重新设置（配置里有 TZ_OVERRIDE 时以它为准）")
    else:
        add("时区匹配", "warn", "查不到出口 IP 的时区，当前 %s" % tz_sys)

    ip = ip_proxy or geo.get("ip", "")
    rep = get_json("https://api.ipapi.is/?q=%s" % ip) if ip else None
    if not isinstance(rep, dict) or rep.get("error"):
        rep = None
    d = lambda v: v if isinstance(v, dict) else {}   # noqa: E731  各字段有时是字符串
    if rep:
        flags = [n for k, n in (("is_datacenter", "机房 IP"), ("is_proxy", "已知代理"), ("is_vpn", "VPN"),
                                ("is_tor", "Tor"), ("is_abuser", "有滥用记录")) if rep.get(k)]
        company = d(rep.get("company")).get("name", "") or d(rep.get("asn")).get("org", "")
        ctype = d(rep.get("company")).get("type", "")
        detail = "%s · %s%s" % (company or "未知运营商", {"isp": "家庭宽带 / 运营商", "hosting": "机房", "business": "企业",
                                                        "education": "教育网", "government": "政府"}.get(ctype, ctype or "类型未知"),
                                ("；标记：" + "、".join(flags)) if flags else "；无风险标记")
        level = "fail" if rep.get("is_tor") or rep.get("is_abuser") else "warn" if flags else "pass"
        add("IP 信誉", level, detail, "机房 / 代理 IP 更容易被风控，长期使用建议家庭宽带（住宅）IP" if level != "pass" else "")
    else:
        ipa = get_json("http://ip-api.com/json/%s?fields=isp,org,proxy,hosting,mobile" % ip) if ip else None
        if ipa:
            flags = [n for k, n in (("hosting", "机房 IP"), ("proxy", "代理 / VPN")) if ipa.get(k)]
            add("IP 信誉", "warn" if flags else "pass", "%s%s" % (ipa.get("isp", ""), ("；标记：" + "、".join(flags)) if flags else "；无风险标记"))
        else:
            add("IP 信誉", "warn", "信誉查询服务暂时不可用")

    code, out = curl("https://api.anthropic.com/v1/models", timeout=12, extra=["-o", "/dev/null", "-w", "%{http_code}"])
    add("Claude API", "pass" if out in ("401", "403", "200") else "fail",
        "api.anthropic.com 可达（HTTP %s）" % out if out in ("401", "403", "200") else "api.anthropic.com 不可达（%s）" % (out or code))
    code, out = sh(["curl", "-q", "-sS", "-L", "--max-time", "15", "-x", PROXY, "-o", "/dev/null", "-w", "%{http_code} %{url_effective}",
                    "https://claude.ai/login"], 18)
    if "unavailable" in out:
        add("claude.ai 地区", "fail", "claude.ai 提示所在地区不可用（%s）" % out, "换一个受支持地区的出口 IP")
    elif out.startswith(("2", "3")):
        add("claude.ai 地区", "pass", "claude.ai 正常打开")
    else:
        add("claude.ai 地区", "info", "claude.ai 返回 %s（403 多为人机验证，在浏览器里打开即可）" % (out.split(" ")[0] or "超时"))

    try:
        engine = open("/run/cbox/browser").read().strip() or "firefox"
    except OSError:
        engine = "firefox"
    try:
        if engine == "chrome":
            cp = json.load(open("/etc/opt/chrome/policies/managed/easycc.json"))
            ok = (cp.get("ProxySettings") or {}).get("ProxyServer") == "socks5://127.0.0.1:7890" and cp.get("WebRtcIPHandling") == "disable_non_proxied_udp"
        else:
            pol = open("/etc/firefox/policies/policies.json").read()
            ok = '"media.peerconnection.enabled"' in pol and '"Locked": true' in pol.replace('"Locked":true', '"Locked": true')
    except (OSError, ValueError):
        ok = False
    add("浏览器", "pass" if ok else "warn", "%s（英文界面）锁定走代理（远程 DNS），已关 WebRTC、定位、DoH、遥测" % ("Chrome" if engine == "chrome" else "Firefox")
        if ok else "浏览器策略缺失")

    osr = load_env("/etc/os-release").get("PRETTY_NAME", "")
    add("系统", "info", "%s · %s · 主机名 %s · 语言头 %s" % (osr, os.uname().machine, os.uname().nodename,
                                                       load_env(os.path.join(RUN, "status.env")).get("ACCEPT_LANGUAGE", "")))
    worst = "fail" if any(i["level"] == "fail" for i in items) else "warn" if any(i["level"] == "warn" for i in items) else "pass"
    return {"overall": worst, "items": items, "checkedAt": time.strftime("%F %T")}


# ---------------- Kiteline ----------------

def kiteline_call(path, body=None):
    cfg = json.load(open(KITELINE_CFG))
    m = cfg.get("mobile") or {}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:%s%s" % (m.get("port") or 8765, path), data=data,
                                 headers={"X-CCP-Token": m.get("token", ""), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def kiteline(renew=False):
    if not os.path.exists("/usr/local/bin/kiteline"):
        if os.uname().machine in ("x86_64", "amd64"):
            reason = "这个镜像构建时没有装 Kiteline（WITH_KITELINE=0）。重新构建：easycc build"
        else:
            reason = ("Kiteline 官方 Linux 版只有 x86_64，当前是 %s 镜像，所以没有装。"
                      "需要的话改用 amd64 镜像：easycc build --arch amd64（Apple 芯片 Mac 上经 Rosetta 运行）" % os.uname().machine)
        return {"installed": False, "reason": reason}
    running = sh(["pgrep", "-u", "dev", "-f", "python.* -m kiteline"])[0] == 0
    if not running:
        return {"installed": True, "running": False}
    try:
        pair = kiteline_call("/api/desktop/pairing", {"renew": renew})
        st = kiteline_call("/api/desktop/status")
    except Exception as exc:  # noqa: BLE001
        return {"installed": True, "running": True, "starting": True, "error": str(exc)}
    return {"installed": True, "running": True, "code": pair.get("code"), "expiry": pair.get("expiry"), "qr": pair.get("qr", ""),
            "link": pair.get("link", ""), "remote": st.get("remote") or {}, "remoteEnabled": st.get("remoteEnabled"),
            "devices": st.get("devices") or [], "version": st.get("version", ""), "sessions": len(st.get("sessions") or [])}


_qr_cache = {}


def qr_svg(text):
    if text in _qr_cache:
        return _qr_cache[text]
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf)
        svg = buf.getvalue().decode()
    except Exception:  # noqa: BLE001
        svg = ""
    _qr_cache[text] = svg
    return svg


# ---------------- 重置 ----------------

_reset_lock = threading.Lock()


def reset(scope):
    if not _reset_lock.acquire(blocking=False):
        return {"ok": False, "message": "上一个重置还没做完"}
    try:
        if scope == "network":
            try:
                os.remove(os.path.join(RUN, "killswitch"))
            except OSError:
                pass
            code, out = sh(["/usr/local/sbin/cbox-net", "apply"], 200)
            if code == 0:
                st = load_env(os.path.join(RUN, "status.env"))
                return {"ok": True, "message": "网络已重连，联网检查通过，出口 IP %s" % st.get("EXIT_IP", "")}
            return {"ok": False, "message": "网络已重新应用，但外网没放行：%s" % (gate_reason() if code == 3 else (out.splitlines() or ["未知错误"])[-1])}
        if scope in ("browser", "claude", "kiteline", "agents", "all"):
            code, out = sh(["/usr/local/sbin/cbox-userland", "reset", scope], 120)
            names = {"browser": "浏览器数据已清空（登录状态、缓存、历史）", "claude": "Claude Code 已退出登录并清空本地数据",
                     "kiteline": "Kiteline 配对已清空，请重新扫码", "agents": "已卸载所有 agent",
                     "all": "已恢复初始状态（项目目录未动），需要的 agent 请到「Agent」页重新安装"}
            return {"ok": code == 0, "message": names[scope] if code == 0 else out}
        return {"ok": False, "message": "未知操作"}
    finally:
        _reset_lock.release()


# ---------------- HTTP ----------------

LOGIN_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>easyCC</title><body style="font:15px system-ui;max-width:420px;margin:15vh auto;padding:0 16px">
<h2>easyCC 管理后台</h2><p>请用 <code>easycc admin &lt;实例名&gt;</code> 打开，或输入令牌：</p>
<form action="/login"><input name="k" style="width:100%;padding:8px;font-size:15px" autofocus>
<p><button style="padding:8px 18px">进入</button></p></form>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "cbox-admin"

    def log_message(self, fmt, *args):
        pass

    def authed(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        v = c["cbox_admin"].value if "cbox_admin" in c else self.headers.get("X-CBox-Token", "")
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
        if u.path == "/login":
            k = (parse_qs(u.query).get("k") or [""])[0]
            if k and hmac.compare_digest(k, token()):
                # 200 页面里设 Cookie 再跳转：Safari / WebKit 不保留 303 跳转响应里设的 Cookie
                nxt = (parse_qs(u.query).get("next") or ["/"])[0]
                nxt = nxt if re.match(r"^/(#[a-z]+)?$", nxt) else "/"          # 只允许跳到本页的某个标签（#ag 等）
                return self.send(200, '<!doctype html><meta charset="utf-8"><title>easyCC</title><body style="background:#0a0d16">'
                                      '<script>location.replace(%s)</script><noscript><meta http-equiv="refresh" content="0;url=/"></noscript>' % json.dumps(nxt),
                                 "text/html; charset=utf-8",
                                 headers={"Set-Cookie": "cbox_admin=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000" % k})
            return self.send(200, LOGIN_PAGE, "text/html; charset=utf-8")
        m = re.match(r"^/static/agents/([a-z0-9-]+)\.(png|svg)$", u.path)
        if m and os.path.exists(os.path.join(HERE, "static", "agents", m.group(1) + "." + m.group(2))):
            return self.send(200, open(os.path.join(HERE, "static", "agents", m.group(1) + "." + m.group(2)), "rb").read(),
                             "image/svg+xml" if m.group(2) == "svg" else "image/png", headers={"Cache-Control": "max-age=86400"})
        if u.path == "/static/miniprogram-qr.jpg":
            return self.send(200, open(os.path.join(HERE, "static", "miniprogram-qr.jpg"), "rb").read(), "image/jpeg")
        if not self.authed():
            if u.path.startswith("/api/"):
                return self.send(401, {"error": "未登录"})
            return self.send(200, LOGIN_PAGE, "text/html; charset=utf-8")
        if u.path == "/":
            return self.send(200, open(os.path.join(HERE, "index.html"), encoding="utf-8").read(), "text/html; charset=utf-8")
        if u.path == "/api/status":
            return self.send(200, status())
        if u.path == "/api/kiteline":
            return self.send(200, kiteline())
        if u.path == "/api/gate":
            return self.send(200, gate_state())
        if u.path == "/api/config":
            return self.send(200, get_config())
        if u.path == "/api/projects":
            return self.send(200, list_projects((parse_qs(u.query).get("path") or [""])[0]))
        if u.path == "/api/hostaccess":
            return self.send(200, host_access_status())
        if u.path == "/api/agents":
            return self.send(200, {"agents": agents.list_agents("fresh" in u.query)})
        m = re.match(r"^/api/agents/([a-z0-9-]+)/log$", u.path)
        if m:
            return self.send(200, agents.read_log(m.group(1), int((parse_qs(u.query).get("offset") or ["0"])[0] or 0)))
        if u.path == "/api/appstore-qr.svg":
            return self.send(200, qr_svg(APP_STORE), "image/svg+xml")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urlsplit(self.path)
        # 只接受页面里 fetch 发的请求（带自定义头，跨站表单带不上）
        if not self.authed() or self.headers.get("X-CBox") != "1":
            return self.send(401, {"error": "未登录"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            body = {}
        if u.path == "/api/check/proxy":
            return self.send(200, check_proxy())
        if u.path == "/api/check/env":
            return self.send(200, check_env())
        if u.path == "/api/proxy/test":
            return self.send(200, test_proxy(body))
        if u.path == "/api/proxy/apply":
            return self.send(200, apply_config(body))
        if u.path == "/api/project":
            return self.send(200, set_project(body.get("path", "")))
        if u.path == "/api/project/mkdir":
            return self.send(200, make_dir(body.get("path", ""), body.get("name", "")))
        if u.path == "/api/hostaccess":
            return self.send(200, host_access_set(body))
        if u.path == "/api/browser/open":
            return self.send(200, browser_open(body.get("url", "")))
        if u.path == "/api/kiteline/renew":
            return self.send(200, kiteline(True))
        m = re.match(r"^/api/agents/([a-z0-9-]+)/(install|uninstall|default)$", u.path)
        if m:
            if m.group(2) == "default":
                return self.send(200, agents.set_default(m.group(1)))
            return self.send(200, agents.start(m.group(1), m.group(2)))
        if u.path == "/api/reset":
            return self.send(200, reset(body.get("scope", "")))
        m = re.match(r"^/api/gate/(recheck|pause|resume)$", u.path)
        if m:
            return self.send(200, gate_action(m.group(1), body))
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    token()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
