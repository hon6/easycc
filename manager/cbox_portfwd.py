#!/usr/bin/env python3
"""电脑端的端口转发（每个运行中的环境一个，后台常驻）。兼容 Python 3.9。

1. 固定映射（Apple container）：本机端口 → 环境发布出来的 unix 套接字（管理后台 / 浏览器桌面 / SSH）
     127.0.0.1:7681=unix:/路径/admin.sock
2. 端口映射（像 VS Code 的「端口」面板）：用户添加的端口写在环境配置里 FORWARD_PORTS='8000,3000:13000'
   （环境端口[:电脑端口]），这里每隔几秒读一次，电脑上的 localhost:端口 经隧道转到环境里的端口；
   电脑端口被占用就往后找空闲的。同时经隧道列出环境里正在监听的端口（给界面当「检测到的端口」），
   --auto 时检测到的也自动转发。结果写进 --state，客户端读它显示。
     --tunnel unix:/路径/tunnel.sock 或 tcp:127.0.0.1:7782   --token 令牌   --ports-file 环境的 config.env

3. 访问本机（环境配置 HOST_ACCESS=1 时）：在环境网关地址上开中转（--relay-port），只接受这个环境的 IP + 令牌，
   转到电脑本机的 localhost:端口；同时把电脑的本地证书导出到环境配置目录 ca/，HOST_SSH=1 时把环境的专用公钥
   加进 ~/.ssh/authorized_keys（撤销时删掉）。

用法：cbox_portfwd.py --pidfile f --state ports.json --tunnel … --token … --ports-file … [--relay-port N] [--auto] [固定映射…]
"""
import asyncio
import getpass
import glob
import json
import os
import platform
import re
import subprocess
import signal
import socket
import sys
import time

POLL = 3


async def pipe(r, w):
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            w.close()
        except OSError:
            pass


class Forwarder:
    def __init__(self, tunnel, token, state, ports_file="", auto=False, relay_port=0, relay_bind="", relay_peer=""):
        # relay_bind / relay_peer：easyCC 虚拟机运行时，中转开在 Mac 面向虚拟机的地址上，环境的连接经虚拟机 NAT 过来、来源是虚拟机地址
        self.force_bind, self.force_peer = relay_bind, relay_peer
        self.tunnel, self.token, self.state = tunnel, token, state
        self.ports_file, self.auto_mode = ports_file, auto
        self.relay_port, self.relay, self.relay_bind, self.env_ip = relay_port, None, "", ""
        self.certs_at = 0
        self.conf_dir = os.path.dirname(ports_file) if ports_file else ""
        self.name = os.path.basename(os.path.dirname(self.conf_dir)) if self.conf_dir else ""
        self.fwd = {}       # 环境端口 → (电脑端口, [servers], 想要的电脑端口)
        self.reserved = set()

    def conf_flag(self, key):
        try:
            return re.search(r"^%s=['\"]?1['\"]?\s*$" % key, open(self.ports_file, encoding="utf-8").read(), re.M) is not None
        except OSError:
            return False

    # ---------- 访问本机 ----------
    async def relay_handle(self, r, w):
        peer = (w.get_extra_info("peername") or ("",))[0]
        want = self.force_peer or self.env_ip
        if self.relay_bind != "127.0.0.1" and want and peer != want:
            w.close()   # 只认这个环境
            return
        try:
            line = (await asyncio.wait_for(r.readline(), 8)).decode(errors="replace").split()
            if len(line) != 2 or line[0] != self.token or not line[1].isdigit():
                raise ConnectionError("令牌不对")
            port = int(line[1])
            try:
                tr, tw = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 5)
            except OSError:
                tr, tw = await asyncio.wait_for(asyncio.open_connection("::1", port), 5)
        except (OSError, asyncio.TimeoutError, ConnectionError):
            w.close()
            return
        w.write(b"OK\n")
        await w.drain()
        await asyncio.gather(pipe(r, tw), pipe(tr, w))

    async def sync_host_access(self, gateway, env_ip):
        on = self.conf_flag("HOST_ACCESS")
        if on and self.relay_port and gateway and not self.relay:
            self.env_ip = env_ip
            for bind in ([self.force_bind] if self.force_bind else [gateway, "127.0.0.1"]):   # 宿主机上有这个网关地址就绑它（Apple container、Linux）；否则 Docker Desktop 经 host.docker.internal 到本机回环
                try:
                    self.relay = await asyncio.start_server(self.relay_handle, bind, self.relay_port)
                    self.relay_bind = bind
                    break
                except OSError:
                    continue
            self.write_host_json()
        if not on and self.relay:
            self.relay.close()
            self.relay, self.relay_bind = None, ""
        if on and time.time() - self.certs_at > 3600:
            self.export_certs()
        if not on and self.certs_at:
            self.clear_certs()
        self.sync_ssh_key(on and self.conf_flag("HOST_SSH"))

    def write_host_json(self):
        try:
            with open(os.path.join(self.conf_dir, "host.json"), "w") as f:
                json.dump({"user": getpass.getuser(), "os": platform.system()}, f)
        except OSError:
            pass

    def export_certs(self):
        """电脑的本地证书：mkcert 根证书、系统钥匙串里自己装的证书（macOS）、本机自装的根证书（Linux）、~/.easycc/ca/ 里放的"""
        out = os.path.join(self.conf_dir, "ca")
        os.makedirs(out, exist_ok=True)
        pems = []
        home = os.path.expanduser("~")
        for f in [os.path.join(home, "Library/Application Support/mkcert/rootCA.pem"), os.path.join(home, ".local/share/mkcert/rootCA.pem")] + \
                glob.glob(os.path.join(os.environ.get("EASYCC_HOME") or os.path.join(home, ".easycc"), "ca", "*.[pc][er][mt]")) + \
                glob.glob("/usr/local/share/ca-certificates/**/*.crt", recursive=True):
            try:
                pems += re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", open(f).read(), re.S)
            except OSError:
                pass
        if platform.system() == "Darwin":
            try:
                txt = subprocess.run(["security", "find-certificate", "-a", "-p", "/Library/Keychains/System.keychain"],
                                     capture_output=True, text=True, timeout=15).stdout
                pems += re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", txt, re.S)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for old in glob.glob(os.path.join(out, "host-*.crt")):
            os.remove(old)
        seen = set()
        for i, pem in enumerate(pems):
            if pem in seen:
                continue
            seen.add(pem)
            with open(os.path.join(out, "host-%02d.crt" % i), "w") as f:
                f.write(pem + "\n")
        open(os.path.join(out, ".ready"), "w").close()
        self.certs_at = time.time()

    def clear_certs(self):
        out = os.path.join(self.conf_dir, "ca")
        for f in glob.glob(os.path.join(out, "host-*.crt")) + [os.path.join(out, ".ready")]:
            try:
                os.remove(f)
            except OSError:
                pass
        self.certs_at = 0

    def sync_ssh_key(self, on):
        """第 2 档：环境的专用公钥放进 ~/.ssh/authorized_keys（只允许经本机回环来的连接，即我们的中转）；关掉就删"""
        ak = os.path.expanduser("~/.ssh/authorized_keys")
        mark = "easycc:%s" % self.name
        try:
            lines = open(ak).read().splitlines()
        except OSError:
            lines = []
        keep = [l for l in lines if not l.rstrip().endswith(mark)]
        if on:
            try:
                pub = open(os.path.join(self.conf_dir, "host_ssh.pub")).read().split()
            except OSError:
                pub = []
            if len(pub) >= 2:
                keep.append('from="127.0.0.1,::1",no-agent-forwarding,no-X11-forwarding %s %s %s' % (pub[0], pub[1], mark))
        if keep != lines:
            os.makedirs(os.path.dirname(ak), exist_ok=True)
            os.chmod(os.path.dirname(ak), 0o700)
            with open(ak + ".tmp", "w") as f:
                f.write("\n".join(keep) + ("\n" if keep else ""))
            os.chmod(ak + ".tmp", 0o600)
            os.replace(ak + ".tmp", ak)

    def manual_ports(self):
        """读 FORWARD_PORTS='8000,3000:13000' → {环境端口: 想要的电脑端口}"""
        try:
            text = open(self.ports_file, encoding="utf-8").read()
        except OSError:
            return {}
        m = re.search(r"^FORWARD_PORTS=['\"]?([0-9:, ]*)", text, re.M)
        out = {}
        for item in (m.group(1) if m else "").replace(" ", "").split(","):
            if not item:
                continue
            a, _, b = item.partition(":")
            if a.isdigit() and 0 < int(a) < 65536:
                out[int(a)] = int(b) if b.isdigit() and 0 < int(b) < 65536 else int(a)
        return out

    async def open_tunnel(self):
        kind, addr = self.tunnel.split(":", 1)
        if kind == "unix":
            return await asyncio.open_unix_connection(addr)
        host, port = addr.rsplit(":", 1)
        return await asyncio.open_connection(host, int(port))

    async def request(self, cmd):
        r, w = await asyncio.wait_for(self.open_tunnel(), 5)
        w.write(("%s %s\n" % (self.token, cmd)).encode())
        await w.drain()
        return r, w

    async def list_ports(self):
        r, w = await self.request("LIST")
        try:
            line = await asyncio.wait_for(r.readline(), 8)
            return json.loads(line.decode() or "[]")
        finally:
            w.close()

    def connect_handler(self, env_port):
        async def handle(cr, cw):
            try:
                r, w = await self.request("CONNECT %d" % env_port)
                ok = await asyncio.wait_for(r.readline(), 8)
                if ok.strip() != b"OK":
                    raise ConnectionError("环境里连不上端口 %d" % env_port)
            except (OSError, asyncio.TimeoutError, ConnectionError):
                cw.close()
                return
            await asyncio.gather(pipe(cr, w), pipe(r, cw))
        return handle

    @staticmethod
    def port_free(p):
        for fam, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            s = socket.socket(fam, socket.SOCK_STREAM)
            try:
                s.bind((host, p))
            except OSError as exc:
                if fam == socket.AF_INET6 and exc.errno in (99, 47, 49):   # 没有 IPv6 回环
                    continue
                return False
            finally:
                s.close()
        return True

    async def listen(self, env_port, want_host):
        """电脑上优先用指定的端口（默认同号），被占用就往后找"""
        candidates = [want_host] + list(range(max(want_host + 1, 1024), min(want_host + 200, 65535)))
        for hp in candidates:
            if hp in self.reserved or not self.port_free(hp):
                continue
            servers = []
            try:
                servers.append(await asyncio.start_server(self.connect_handler(env_port), "127.0.0.1", hp))
                try:   # 浏览器常把 localhost 解析成 ::1，也监听一份
                    servers.append(await asyncio.start_server(self.connect_handler(env_port), "::1", hp))
                except OSError:
                    pass
                return hp, servers
            except OSError:
                for s in servers:
                    s.close()
        return None, []

    async def sync_loop(self):
        while True:
            err, detected, info = "", [], {}
            try:
                info = await self.list_ports()
                if isinstance(info, list):          # 旧版环境
                    info = {"ports": info}
                detected = info.get("ports", [])
            except Exception as exc:  # noqa: BLE001
                err = "连不上环境（%s）" % (str(exc) or type(exc).__name__)
            try:
                await self.sync_host_access(info.get("gateway", ""), info.get("ip", ""))
            except Exception as exc:  # noqa: BLE001
                err = err or "访问本机：%s" % exc
            procs = {p["port"]: p.get("proc", "") for p in detected}
            want = dict(self.manual_ports())
            if self.auto_mode:
                for p in procs:
                    want.setdefault(p, p)
            try:
                for ep in list(self.fwd):
                    hp, servers, wh = self.fwd[ep]
                    if ep not in want or want[ep] != wh:
                        for srv in servers:
                            srv.close()
                        self.reserved.discard(hp)
                        del self.fwd[ep]
                for ep, wh in want.items():
                    if ep in self.fwd:
                        continue
                    hp, servers = await self.listen(ep, wh)
                    if hp:
                        self.reserved.add(hp)
                        self.fwd[ep] = (hp, servers, wh)
            except Exception as exc:  # noqa: BLE001
                err = err or str(exc)
            self.write_state(not err, err, want, procs)
            await asyncio.sleep(2)

    def write_state(self, ok, err, want, procs):
        if not self.state:
            return
        manual = self.manual_ports()
        ports = []
        for ep in sorted(want):
            hp = self.fwd.get(ep, (None,))[0]
            ports.append({"env": ep, "host": hp, "wantHost": want[ep], "proc": procs.get(ep, ""), "listening": ep in procs,
                          "manual": ep in manual})
        data = {"ok": ok, "error": err, "at": int(time.time()), "pid": os.getpid(), "auto": self.auto_mode, "ports": ports,
                "hostAccess": {"on": bool(self.relay), "bind": self.relay_bind, "port": self.relay_port, "certs": len(glob.glob(os.path.join(self.conf_dir, "ca", "host-*.crt")))},
                "detected": [{"port": p, "proc": procs[p]} for p in sorted(procs) if p not in want]}
        tmp = self.state + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.state)


def fixed_handler(target):
    async def handle(r, w):
        try:
            if target.startswith("unix:"):
                ur, uw = await asyncio.open_unix_connection(target[5:])
            else:
                host, port = target.split(":", 1)[1].rsplit(":", 1)
                ur, uw = await asyncio.open_connection(host, int(port))
        except OSError:
            w.close()
            return
        await asyncio.gather(pipe(r, uw), pipe(ur, w))
    return handle


async def main(opts, fixed):
    servers = []
    for hostport, target in fixed:
        host, port = hostport.rsplit(":", 1)
        servers.append(await asyncio.start_server(fixed_handler(target), host, int(port)))
    tasks = [s.serve_forever() for s in servers]
    if opts.get("tunnel") and opts.get("token"):
        tasks.append(Forwarder(opts["tunnel"], opts["token"], opts.get("state"), opts.get("ports-file", ""), opts.get("auto") == "1",
                               int(opts.get("relay-port") or 0), opts.get("relay-bind", ""), opts.get("relay-peer", "")).sync_loop())
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    args, opts, fixed = sys.argv[1:], {}, []
    while args:
        a = args.pop(0)
        if a in ("--pidfile", "--state", "--tunnel", "--token", "--ports-file", "--relay-port", "--relay-bind", "--relay-peer"):
            opts[a[2:]] = args.pop(0)
        elif a == "--auto":
            opts["auto"] = "1"
        else:
            hp, target = a.split("=", 1)
            fixed.append((hp, target if ":" in target.split("/")[0] else "unix:" + target))
    if opts.get("pidfile"):
        with open(opts["pidfile"], "w") as f:
            f.write(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        asyncio.run(main(opts, fixed))
    finally:
        for k in ("pidfile", "state"):
            if opts.get(k):
                try:
                    os.remove(opts[k])
                except OSError:
                    pass
