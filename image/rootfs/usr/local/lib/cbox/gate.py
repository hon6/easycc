#!/usr/bin/env python3
"""联网闸门检查（root 运行，由 cbox-net 调用）：关键项全部有证据且通过，才允许环境里的程序经代理上网。

检查期间闸门是关着的（防火墙 CBOX_GATE 链挡住 dev 等用户访问 mihomo），只有 root 和检查专用身份
（uid 65533，不对应任何账号）能经代理出去；任何一项失败或拿不到结果，闸门保持关闭。
结果写到 /run/cbox/gate.json，退出码 0 = 全部通过，3 = 有关键项没通过。

  gate.py           做一遍检查（状态：passed 通过 / failed 明确违规 / unknown 没拿到证据，比如超时）
  gate.py --rebase  先清掉保存的出口基准（「以当前出口为准」），再检查

出口规则（/etc/cbox/config.env）：
  EXPECT_IP=1.2.3.4                出口必须是这个 IP（固定 IP）
  EXIT_RULE=country|region|city|any 动态 IP 允许变化的范围，默认 country：
                                    第一次通过时记下出口所在国家 / 州省 / 城市作为基准（/etc/cbox/exit-base.json），
                                    之后只有在范围内变化才放行；换了代理服务器会重新记基准。
中国大陆出口一律不放行（内置大陆网段表 + IP 定位），任何规则都不能豁免；查不到出口所在地区也不放行。
"""
import ipaddress
import json
import os
import pwd
import secrets
import re
import subprocess
import sys
import time

RUN = "/run/cbox"
CONF = "/etc/cbox/config.env"
BASE = "/etc/cbox/exit-base.json"
HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = "http://127.0.0.1:7890"
CHECKER = 65533          # 检查专用身份：受透明转发约束，但不受闸门约束
IP_URLS = ("https://api4.ipify.org", "https://ipv4.icanhazip.com", "https://ipinfo.io/ip",
           "http://ip-api.com/line/?fields=query", "https://api.ipify.org")


def load_env(path):
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip())
            if m:
                v = m.group(2).strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                out[m.group(1)] = v
    except OSError:
        pass
    return out


def run(cmd, timeout=15, uid=None):
    if uid is not None:
        cmd = ["setpriv", "--reuid", str(uid), "--regid", str(uid), "--clear-groups"] + cmd
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 124, str(exc)


def curl(url, proxy=True, uid=None, timeout=10, extra=()):
    cmd = ["curl", "-q", "-fsS", "--max-time", str(timeout)] + (["-x", PROXY] if proxy else ["--noproxy", "*"]) + list(extra) + [url]
    return run(cmd, timeout + 3, uid)


def exit_ip(proxy=True, uid=None):
    for u in IP_URLS:
        code, out = curl(u, proxy, uid)
        if code == 0 and re.fullmatch(r"[0-9a-fA-F.:]+", out):
            return out
    return ""


def geo_lookup(ip):
    """出口所在地：ipinfo.io，查不到再用 ip-api.com（都经代理）"""
    try:
        g = json.load(open(os.path.join(RUN, "geo.json")))
        if g.get("ip") == ip and time.time() - g.get("at", 0) < 120 and g.get("cc"):
            return g      # cbox-net 刚查过
    except (OSError, ValueError):
        pass
    code, out = curl("https://ipinfo.io/%s/json" % ip, timeout=12)
    try:
        j = json.loads(out) if code == 0 else {}
    except ValueError:
        j = {}
    if j.get("country"):
        return {"ip": ip, "cc": j["country"].upper(), "region": j.get("region", ""), "city": j.get("city", ""),
                "tz": j.get("timezone", ""), "src": "ipinfo", "at": time.time()}
    code, out = curl("http://ip-api.com/json/%s?fields=status,countryCode,regionName,city,timezone" % ip, timeout=12)
    try:
        j = json.loads(out) if code == 0 else {}
    except ValueError:
        j = {}
    if j.get("countryCode"):
        return {"ip": ip, "cc": j["countryCode"].upper(), "region": j.get("regionName", ""), "city": j.get("city", ""),
                "tz": j.get("timezone", ""), "src": "ip-api", "at": time.time()}
    return {}


def second_opinion(ip, geo):
    """另一家定位服务的国家（交叉核对，查不到返回空，不影响结论）"""
    if geo.get("src") == "ipinfo":
        code, out = curl("http://ip-api.com/json/%s?fields=countryCode" % ip, timeout=8)
        key = "countryCode"
    else:
        code, out = curl("https://ipinfo.io/%s/country" % ip, timeout=8)
        return out.strip().upper() if code == 0 and re.fullmatch(r"[A-Za-z]{2}", out.strip()) else ""
    try:
        return (json.loads(out).get(key) or "").upper() if code == 0 else ""
    except ValueError:
        return ""


def dns_resolvers():
    """网站看到的 DNS 解析服务器（和 DNS 泄漏检测网站同一个原理）：让几个随机域名经代理被解析，再问对方是谁来查的。
    返回 ([(ip, 国家代码或空, 说明)], ECS 子网或空, 来源)；都查不到返回 None"""
    code, tid = curl("https://bash.ws/id", timeout=10)
    tid = tid.strip()
    if code == 0 and re.fullmatch(r"[0-9a-f]{8,64}", tid):
        for i in range(3):
            curl("http://%d.%s.bash.ws" % (i, tid), timeout=6)
        code, out = curl("https://bash.ws/dnsleak/test/%s?json" % tid, timeout=12)
        try:
            items = json.loads(out) if code == 0 else []
        except ValueError:
            items = []
        res = [(x.get("ip", ""), (x.get("country") or "").upper()[:2], "%s %s" % (x.get("country_name", ""), x.get("asn", "")))
               for x in items if isinstance(x, dict) and x.get("type") == "dns" and x.get("ip")]
        if res:
            return res, "", "bash.ws"
    # 备用：ip-api 的 EDNS 检测（子域名必须是 32 位随机十六进制）
    code, out = curl("http://%s.edns.ip-api.com/json" % secrets.token_hex(16), timeout=12)
    try:
        j = json.loads(out) if code == 0 else {}
    except ValueError:
        j = {}
    d = j.get("dns") or {}
    if d.get("ip"):
        ecs = (j.get("edns") or {}).get("ip", "")
        return [(d["ip"], country_of(d["ip"]), d.get("geo", ""))], ecs, "edns.ip-api.com"
    return None


def country_of(ip):
    code, out = curl("https://ipinfo.io/%s/country" % ip, timeout=8)
    out = out.strip().upper()
    if code == 0 and re.fullmatch(r"[A-Z]{2}", out):
        return out
    code, out = curl("http://ip-api.com/json/%s?fields=countryCode" % ip, timeout=8)
    try:
        return (json.loads(out).get("countryCode") or "").upper() if code == 0 else ""
    except ValueError:
        return ""


_nets = None


def in_mainland(ip):
    global _nets
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version != 4:
        return False
    if _nets is None:
        _nets = []
        for line in open(os.path.join(HERE, "mainland-ipv4.txt")):
            line = line.strip()
            if line and not line.startswith("#"):
                n = ipaddress.ip_network(line, strict=False)
                _nets.append((int(n.network_address), int(n.broadcast_address)))
    x = int(a)
    return any(lo <= x <= hi for lo, hi in _nets)


def norm(s):
    return " ".join((s or "").split()).lower()


def main():
    rebase = "--rebase" in sys.argv
    c = load_env(CONF)
    st = load_env(os.path.join(RUN, "status.env"))
    checks = []

    def add(cid, title, ok, evidence, remedy=""):
        # ok：True 通过 / False 明确不通过 / None 没拿到证据 / "warn" 有风险提示但不挡外网
        checks.append({"id": cid, "title": title,
                       "state": "passed" if ok is True else "failed" if ok is False else "warning" if ok == "warn" else "unknown",
                       "evidence": evidence, "remedy": "" if ok is True else remedy, "at": time.strftime("%F %T")})

    # 1. 防火墙
    code, out = run(["/usr/local/sbin/cbox-firewall", "check"])
    add("firewall", "防火墙", code == 0, "出站默认拒绝，只有 mihomo 能连上游代理" if code == 0 else (out or "规则缺失"), "重启环境")

    # 2. 绕开代理直连
    code, _ = curl("https://1.1.1.1", proxy=False, uid=pwd.getpwnam("mihomo").pw_uid, timeout=5)
    add("direct", "直连外网封锁", code != 0, "绕开代理直连 1.1.1.1 失败（已封锁）" if code != 0 else "能直连外网，防火墙失效", "重启环境")

    # 3. 代理出口
    ip = exit_ip(True)
    add("proxy", "代理连接", True if ip else None, "经代理访问外网正常，出口 %s" % ip if ip else "经代理访问外网失败（代理不通或超时）",
        "检查代理是否可用，或在「网络」里换一个代理")

    # 4. 透明转发出口一致（不认代理设置的程序也只能走代理）
    if ip:
        ip_t = exit_ip(False, CHECKER)
        same = ip_t == ip or (ip_t and (":" in ip_t) != (":" in ip))
        add("transparent", "出口一致", True if same else (False if ip_t else None), "不认代理设置的程序出口也是 %s" % (ip_t or "?") if same
            else "透明转发出口 %s，代理出口 %s" % (ip_t or "获取失败", ip), "重新应用网络；仍不一致说明代理服务商对不同连接给了不同出口")
    else:
        add("transparent", "出口一致", None, "代理不通，无法比较")

    # 5. IPv6
    code, _ = run(["curl", "-q", "-6", "-fsS", "--noproxy", "*", "--max-time", "5", "https://api6.ipify.org"], 8, CHECKER)
    add("ipv6", "IPv6 封锁", code != 0, "IPv6 出站已封锁" if code != 0 else "IPv6 能直连出去，会泄漏真实地址", "重启环境")

    # 6. DNS 接管
    code, out = run(["dig", "+short", "+time=3", "+tries=1", "example.com"], 8, CHECKER)
    fake = out.splitlines()[-1] if out else ""
    add("dns", "DNS 接管", fake.startswith("198.18."),
        "系统 DNS 由 mihomo 接管（假 IP %s），真实查询经代理发出" % fake if fake.startswith("198.18.") else "DNS 没有被接管：%s" % (out or "无结果"),
        "重启环境")

    # 7. 出口地区（中国大陆不放行，查不到也不放行）
    geo = geo_lookup(ip) if ip else {}
    if geo:
        json.dump(geo, open(os.path.join(RUN, "geo.json"), "w"))
    where = " / ".join(x for x in (geo.get("cc"), geo.get("region"), geo.get("city")) if x)
    second = second_opinion(ip, geo) if geo else ""
    if not ip:
        add("region", "出口地区", None, "代理不通，无法确认出口地区")
    elif not geo:
        add("region", "出口地区", None, "查不到出口 %s 所在地区（定位服务经代理访问失败）" % ip, "稍后会自动重试，或换一个代理")
    elif second and second != geo["cc"]:
        add("region", "出口地区", False, "两家定位服务对出口 %s 的国家结论不一致（%s：%s，另一家：%s），无法确认真实位置" % (ip, geo.get("src"), geo["cc"], second),
            "这类 IP 的位置在各家数据库里不一致，网站也可能判成别的国家：建议换一个位置明确的节点")
    elif geo["cc"] == "CN" or second == "CN" or in_mainland(ip):
        add("region", "出口地区", False, "出口 %s 在中国大陆（%s），不放行" % (ip, where or "命中大陆网段表"),
            "代理没有真正出境：换一个境外节点，或检查代理服务商的设置")
    else:
        add("region", "出口地区", True, "出口 %s 位于 %s" % (ip, where))

    # 8. 出口规则
    expect = c.get("EXPECT_IP", "").strip()
    rule = (c.get("EXIT_RULE", "") or "country").strip().lower()
    proxy_id = st.get("PROXY", "")
    region_ok = checks[-1]["state"] == "passed"
    if not ip or not geo:
        add("rule", "出口规则", None, "出口未确认，无法核对规则")
    elif not region_ok and not expect:
        add("rule", "出口规则", None, "出口地区没通过，不核对也不记基准")
    elif expect:
        add("rule", "出口规则", ip == expect, "固定 IP：出口 %s%s预期 %s" % (ip, " 与" if ip == expect else " 不是", expect),
            "代理换了出口：联系代理服务商，或在「网络」里修改预期 IP")
    elif rule == "any":
        add("rule", "出口规则", True, "动态 IP，不限地区")
    else:
        if rebase:
            try:
                os.remove(BASE)
            except OSError:
                pass
        try:
            base = json.load(open(BASE))
        except (OSError, ValueError):
            base = {}
        if base.get("proxy") != proxy_id or not base.get("cc"):
            base = {"proxy": proxy_id, "ip": ip, "cc": geo["cc"], "region": geo.get("region", ""), "city": geo.get("city", ""),
                    "src": geo.get("src", ""), "at": time.strftime("%F %T")}
            tmp = BASE + ".tmp"
            json.dump(base, open(tmp, "w"), ensure_ascii=False)
            os.replace(tmp, BASE)
            add("rule", "出口规则", True, "已记下出口基准：%s（之后出口只能在同一%s内变化）" % (
                where, {"city": "城市", "region": "州省"}.get(rule, "国家 / 地区")))
        else:
            levels = {"country": ("cc",), "region": ("cc", "region"), "city": ("cc", "region", "city")}.get(rule, ("cc",))
            if base.get("src") and geo.get("src") != base.get("src"):
                levels = ("cc",)      # 两家定位服务的州省 / 城市写法不一样，只比国家
            diff = [k for k in levels if norm(geo.get(k)) != norm(base.get(k))]
            bw = " / ".join(x for x in (base.get("cc"), base.get("region"), base.get("city")) if x)
            add("rule", "出口规则", not diff, "出口 %s 在基准范围内（基准 %s）" % (where, bw) if not diff
                else "出口从 %s 变到了 %s，超出允许范围" % (bw, where),
                "代理换了地区：换回原地区的节点；确认要用新地区，在「网络」里点「以当前出口为准」")

    # 8b. Anthropic 自己怎么看这个出口：不带有效密钥请求 API，正常地区回 401；地区不支持回 403
    if ip:
        code, out = run(["curl", "-q", "-sS", "--max-time", "12", "-x", PROXY, "-o", "/dev/null", "-w", "%{http_code}",
                         "-H", "x-api-key: easycc-probe", "-H", "anthropic-version: 2023-06-01", "-H", "content-type: application/json",
                         "-d", "{}", "https://api.anthropic.com/v1/messages"], 16)
        if out in ("401", "400", "404", "405"):
            add("anthropic", "Anthropic 地区", True, "api.anthropic.com 正常响应（HTTP %s），出口地区受支持" % out)
        elif out == "403":
            add("anthropic", "Anthropic 地区", False, "api.anthropic.com 拒绝了这个出口（HTTP 403，多为地区不受支持）",
                "换一个 Claude 支持地区的节点")
        else:
            add("anthropic", "Anthropic 地区", None, "api.anthropic.com 没有正常响应（%s）" % (out if out and out != "000" else "超时"))
    else:
        add("anthropic", "Anthropic 地区", None, "代理不通，无法确认")

    # 8c. DNS 出口：网站（和 DNS 泄漏检测）看到的解析服务器在哪
    CHAIN = "建议改用链式代理：先连一个中转节点，再由落地节点（住宅 IP 等）出去，域名在落地节点那边解析。在「网络」里填「中转代理」即可"
    if not ip or not geo:
        add("dns_exit", "DNS 出口", None, "出口未确认，无法检测 DNS 出口")
    else:
        r = dns_resolvers()
        if not r:
            add("dns_exit", "DNS 出口", None, "DNS 泄漏检测服务经代理访问失败，无法确认解析服务器位置", "稍后会自动重试")
        else:
            res, ecs, src = r
            desc = "、".join("%s（%s）" % (rip, info.strip() or rcc or "未知") for rip, rcc, info in res[:3])
            cn = [rip for rip, rcc, _ in res if rcc == "CN" or in_mainland(rip)]
            other = sorted({rcc for _, rcc, _ in res if rcc and rcc != geo["cc"]})
            if cn or (ecs and in_mainland(ecs.split("/")[0])):
                add("dns_exit", "DNS 出口", False, "域名解析经过中国大陆的服务器（%s）%s，网站能据此判断你在国内" % (
                    desc, "，并带上了国内网段 %s" % ecs if ecs and in_mainland(ecs.split("/")[0]) else ""), CHAIN)
            elif other:
                add("dns_exit", "DNS 出口", "warn", "解析服务器在 %s，出口在 %s（%s）：DNS 泄漏检测会看到两个国家" % (
                    "、".join(other), geo["cc"], desc), CHAIN)
            else:
                add("dns_exit", "DNS 出口", True, "解析服务器与出口同在 %s：%s（%s 检测）" % (geo["cc"], desc, src))

    # 9. 系统时区、语言与出口一致
    tz_want = c.get("TZ_OVERRIDE", "").strip() or geo.get("tz", "")
    tz_sys = open("/etc/timezone").read().strip() if os.path.exists("/etc/timezone") else ""
    env_sh = open("/etc/profile.d/cbox-env.sh").read() if os.path.exists("/etc/profile.d/cbox-env.sh") else ""
    tz_env = (re.search(r"^export TZ='([^']*)'", env_sh, re.M) or [None, ""])[1]
    if not tz_want:
        add("timezone", "系统时区", None, "查不到出口所在时区，当前 %s" % (tz_sys or "未设置"), "稍后会自动重试")
    else:
        ok = tz_sys == tz_want and tz_env == tz_want
        add("timezone", "系统时区", ok, "系统时区 %s，与出口所在地一致" % tz_sys if ok else "系统时区 %s，出口所在时区 %s" % (tz_sys or "?", tz_want),
            "重新应用网络会按出口重新设置")

    # 10. 浏览器（Firefox 或 Chrome）：锁定走代理、关 WebRTC、语言与出口一致；正在运行的浏览器时区也要对
    try:
        engine = open("/run/cbox/browser").read().strip() or "firefox"
    except OSError:
        engine = "firefox"
    al = st.get("ACCEPT_LANGUAGE", "")
    bad = []
    try:
        if engine == "chrome":
            pol = json.load(open("/etc/opt/chrome/policies/managed/easycc.json"))
            if (pol.get("ProxySettings") or {}).get("ProxyServer") != "socks5://127.0.0.1:7890":
                bad.append("代理没有锁定")
            if pol.get("WebRtcIPHandling") != "disable_non_proxied_udp":
                bad.append("WebRTC 没关")
            want = [x.strip().split(";")[0] for x in al.split(",") if x.strip()]
            if al and pol.get("ForcedLanguages") != want:
                bad.append("语言与出口不一致")
        else:
            pol = json.load(open("/etc/firefox/policies/policies.json"))["policies"]
            pref = pol.get("Preferences", {})
            px = pol.get("Proxy", {})
            if not (px.get("Locked") and px.get("SOCKSProxy") == "127.0.0.1:7890" and px.get("UseProxyForDNS")):
                bad.append("代理没有锁定")
            if pref.get("media.peerconnection.enabled", {}).get("Value") is not False:
                bad.append("WebRTC 没关")
            if al and pref.get("intl.accept_languages", {}).get("Value") != al:
                bad.append("语言与出口不一致")
    except (OSError, ValueError, KeyError, AttributeError):
        bad = ["浏览器策略缺失"]
    code, pids = run(["pgrep", "-o", "-u", "dev", "-f", "/opt/google/chrome/chrome( |$)" if engine == "chrome" else "/usr/lib/firefox/firefox-bin"])
    ff_tz = []
    for pid in pids.split()[:1]:       # 主进程（容器里 root 读不了别的用户进程的环境变量，用 dev 身份读）
        _, env = run(["runuser", "-u", "dev", "--", "sh", "-c", "tr '\\0' '\\n' < /proc/%s/environ" % pid], 5)
        ff_tz += [e[3:] for e in env.splitlines() if e.startswith("TZ=")]
    if tz_want and ff_tz and any(t != tz_want for t in ff_tz):
        bad.append("正在运行的浏览器时区是 %s" % ff_tz[0])
    add("browser", "浏览器时区与语言", not bad,
        "%s 锁定走代理（远程 DNS），已关 WebRTC，语言 %s%s" % ("Chrome" if engine == "chrome" else "Firefox", al, "，时区 %s" % ff_tz[0] if ff_tz else "")
        if not bad else "；".join(bad), "重新应用网络（会重启环境里的浏览器，标签页会恢复）")

    passed = all(x["state"] in ("passed", "warning") for x in checks)
    first_bad = next((x for x in checks if x["state"] not in ("passed", "warning")), None)
    res = {"open": passed, "checkedAt": time.strftime("%F %T"), "checkedTs": int(time.time()), "exitIP": ip,
           "reason": "" if passed else "%s%s：%s" % (first_bad["title"], "未通过" if first_bad["state"] == "failed" else "无法确认", first_bad["evidence"]),
           "checks": checks}
    tmp = os.path.join(RUN, "gate.json.tmp")
    json.dump(res, open(tmp, "w"), ensure_ascii=False)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(RUN, "gate.json"))
    if ip:
        open(os.path.join(RUN, "last-exit-ip"), "w").write("%s %d\n" % (ip, time.time()))
    print(json.dumps(res, ensure_ascii=False))
    sys.exit(0 if passed else 3)


if __name__ == "__main__":
    main()
