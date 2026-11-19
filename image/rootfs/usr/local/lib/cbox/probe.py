"""上游代理实测：绕过 mihomo，直接按协议一步步走一遍，告诉用户卡在哪一步。
以 mihomo 用户运行（防火墙只放行它连上游代理），代理信息从 stdin 读 JSON，结果 JSON 打到 stdout。"""
import base64
import json
import socket
import sys
import time

TARGETS = [("ip-api.com", 80, "/line/?fields=query"), ("api.ipify.org", 80, "/")]
TARGET_HOST, TARGET_PORT, TARGET_PATH = TARGETS[0]
SOCKS_REPLY = {1: "代理服务器内部错误", 2: "规则不允许", 3: "网络不可达", 4: "主机不可达", 5: "连接被拒绝",
               6: "TTL 过期", 7: "不支持的命令", 8: "不支持的地址类型"}


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("代理服务器提前断开")
        buf += chunk
    return buf


def main():
    global TARGET_HOST, TARGET_PORT, TARGET_PATH
    cfg = json.load(sys.stdin)
    for i, t in enumerate(TARGETS):
        TARGET_HOST, TARGET_PORT, TARGET_PATH = t
        res = attempt(dict(cfg))
        failed_at_target = not res["ok"] and res["steps"] and res["steps"][-1]["name"] in ("经代理连接目标网站", "HTTP CONNECT 隧道", "取出口 IP")
        if res["ok"] or not failed_at_target or i == len(TARGETS) - 1:
            print(json.dumps(res, ensure_ascii=False))
            return


def attempt(cfg):
    steps = []
    result = {"ok": False, "steps": steps, "ip": ""}

    def step(name, fn):
        t = time.time()
        try:
            detail = fn() or ""
            steps.append({"name": name, "ok": True, "ms": int((time.time() - t) * 1000), "detail": detail})
            return True
        except Exception as exc:  # noqa: BLE001
            steps.append({"name": name, "ok": False, "ms": int((time.time() - t) * 1000), "detail": str(exc) or type(exc).__name__})
            return False

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)

    def connect():
        sock.connect((cfg["ip"], int(cfg["port"])))
        return "%s:%s" % (cfg["ip"], cfg["port"])

    if not step("连接代理服务器（TCP）", connect):
        return result

    if cfg["type"] == "socks5":
        def greet():
            methods = b"\x00\x02" if cfg.get("user") else b"\x00"
            sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
            ver, method = recv_exact(sock, 2)
            if ver != 5:
                raise ValueError("对方不是 SOCKS5 服务（版本号 %d）" % ver)
            if method == 0xFF:
                raise ValueError("代理不接受任何认证方式（多半要用户名密码，或用户名密码不对应）")
            cfg["_method"] = method
            return "认证方式：%s" % ("用户名密码" if method == 2 else "无需认证")
        if not step("SOCKS5 握手", greet):
            return result
        if cfg["_method"] == 2:
            def auth():
                u, p = cfg.get("user", "").encode(), cfg.get("pass", "").encode()
                sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
                _, status = recv_exact(sock, 2)
                if status != 0:
                    raise ValueError("用户名或密码错误")
                return "通过"
            if not step("用户名密码认证", auth):
                return result

        def connect_target():
            host = TARGET_HOST.encode()
            sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + TARGET_PORT.to_bytes(2, "big"))
            head = recv_exact(sock, 4)
            if head[1] != 0:
                raise ValueError("代理拒绝连接目标：%s" % SOCKS_REPLY.get(head[1], "错误码 %d" % head[1]))
            atyp = head[3]
            n = 4 if atyp == 1 else 16 if atyp == 4 else recv_exact(sock, 1)[0]
            recv_exact(sock, n + 2)
            return "%s:%d（远程解析域名）" % (TARGET_HOST, TARGET_PORT)
        if not step("经代理连接目标网站", connect_target):
            return result
    else:
        def http_connect():
            req = "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n" % (TARGET_HOST, TARGET_PORT, TARGET_HOST, TARGET_PORT)
            if cfg.get("user"):
                cred = base64.b64encode(("%s:%s" % (cfg["user"], cfg.get("pass", ""))).encode()).decode()
                req += "Proxy-Authorization: Basic %s\r\n" % cred
            sock.sendall((req + "\r\n").encode())
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("代理服务器提前断开")
                data += chunk
            line = data.split(b"\r\n", 1)[0].decode(errors="replace")
            if " 200" not in line:
                raise ValueError("代理返回：%s%s" % (line, "（用户名或密码错误）" if " 407" in line else ""))
            return line
        if not step("HTTP CONNECT 隧道", http_connect):
            return result

    def fetch_ip():
        sock.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\nUser-Agent: curl/8.5.0\r\n\r\n" % (TARGET_PATH, TARGET_HOST)).encode())
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        body = data.split(b"\r\n\r\n", 1)[-1].decode(errors="replace").strip()
        if not body or len(body) > 64:
            raise ValueError("目标网站返回异常")
        result["ip"] = body
        return "出口 IP：%s" % body
    if step("取出口 IP", fetch_ip):
        result["ok"] = True
    sock.close()
    return result


if __name__ == "__main__":
    main()
