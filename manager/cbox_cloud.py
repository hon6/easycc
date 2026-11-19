"""easyCC 云端账号：登录、环境同步到云端 / 从云端恢复、定时上报。兼容 Python 3.9（Mac 自带）。

账号服务见 server/app.py。登录凭证存在 ~/.easycc/cloud.json（权限 600）。
"""
import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://easycc.betac.cn"
AGENT_BINS = {"claude": "claude", "codex": "codex", "gemini": "gemini", "cursor": "cursor-agent", "copilot": "copilot",
              "opencode": "opencode", "qwen": "qwen", "kimi": "kimi"}


class CloudError(Exception):
    def __init__(self, msg, status=0, data=None):
        Exception.__init__(self, msg)
        self.status, self.data = status, data or {}


class Cloud:
    def __init__(self, base_dir, version="dev"):
        self.path = os.path.join(base_dir, "cloud.json")
        self.version = version

    # ---------- 状态 ----------
    def state(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self, **kw):
        st = self.state()
        st.update(kw)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        return st

    def url(self):
        return (self.state().get("url") or os.environ.get("EASYCC_CLOUD_URL") or DEFAULT_URL).rstrip("/")

    def logged_in(self):
        return bool(self.state().get("token"))

    # ---------- HTTP ----------
    def call(self, method, path, body=None, data=None, headers=None, timeout=30, raw=False):
        h = {"User-Agent": "easyCC/%s" % self.version}
        tok = self.state().get("token")
        if tok:
            h["Authorization"] = "Bearer " + tok
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(self.url() + path, data=data, headers=h, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                j = json.loads(e.read() or b"{}")
            except ValueError:
                j = {}
            if e.code == 401 and tok:
                self.save(token="", user=None)
            raise CloudError(j.get("error") or "云端返回 %d" % e.code, e.code, j)
        except (urllib.error.URLError, OSError) as e:
            raise CloudError("连不上云端（%s）：%s" % (self.url(), getattr(e, "reason", e)))
        if raw:
            return resp
        with resp:
            return json.loads(resp.read() or b"{}")

    # ---------- 登录 ----------
    def send_code(self, email):
        return self.call("POST", "/v1/auth/email/code", {"email": email})

    def login(self, email, code):
        r = self.call("POST", "/v1/auth/email/verify", {"email": email, "code": code, "device": device_brief(self.version)})
        self.save(token=r["token"], deviceId=r["deviceId"], user=r["user"], email=r["user"]["email"], loginAt=int(time.time()))
        return r

    def logout(self):
        try:
            if self.logged_in():
                self.call("POST", "/v1/auth/logout", {})
        except CloudError:
            pass
        self.save(token="", user=None)

    def me(self):
        r = self.call("GET", "/v1/me")
        self.save(user=r["user"])
        return r["user"]

    # ---------- 云端环境 ----------
    def list_envs(self):
        return self.call("GET", "/v1/envs")

    def upload(self, name, path, meta, if_match=None, progress=None):
        """有 openssl 就本机加密后直传云存储（不经过服务器，快）；服务器没开云存储或本机没 openssl 时走服务器中转"""
        ossl = shutil.which("openssl")
        if ossl:
            try:
                return self._upload_direct(name, path, meta, if_match, ossl, progress)
            except CloudError as e:
                if not (e.status == 404 and (e.data or {}).get("code") == "no_cdn"):
                    raise
        import base64
        size = os.path.getsize(path)
        h = {"Content-Type": "application/octet-stream", "Content-Length": str(size),
             "X-EasyCC-Meta": base64.b64encode(json.dumps(meta, ensure_ascii=False).encode()).decode()}
        if if_match:
            h["If-Match"] = str(if_match)
        with open(path, "rb") as f:
            return self.call("PUT", "/v1/envs/" + name, data=f, headers=h, timeout=600)

    def _upload_direct(self, name, path, meta, if_match, ossl, progress=None):
        size, digest = os.path.getsize(path), _sha256_file(path)
        r = self.call("POST", "/v1/envs/%s/upload" % name, {"size": size, "sha256": digest, "meta": meta, "ifMatch": if_match or None})
        if r.get("unchanged"):
            return r
        fd, enc = tempfile.mkstemp(prefix="easycc-up-", suffix=".bin")
        os.close(fd)
        try:
            _openssl_ctr(ossl, r["key"], r["iv"], path, enc)
            up = r["upload"]
            with open(enc, "rb") as f:
                body = _Reader(f, size, progress)
                req = urllib.request.Request(up["url"], data=body, method=up.get("method", "PUT"),
                                             headers=dict(up["headers"], **{"Content-Length": str(size)}))
                try:
                    urllib.request.urlopen(req, timeout=1800).read()
                except urllib.error.HTTPError as e:
                    raise CloudError("上传到云存储失败（%d）：%s" % (e.code, e.read()[:200].decode("utf-8", "replace")), e.code)
                except (urllib.error.URLError, OSError) as e:
                    raise CloudError("上传到云存储失败：%s" % getattr(e, "reason", e))
        finally:
            os.remove(enc)
        return self.call("POST", "/v1/envs/%s/commit" % name, {"uploadId": r["uploadId"]}, timeout=60)

    def download(self, name, progress=None):
        ossl = shutil.which("openssl")
        resp = self.call("GET", "/v1/envs/%s/data" % name, raw=True, timeout=600, headers={"X-EasyCC-CDN": "1"} if ossl else None)
        fd, path = tempfile.mkstemp(prefix="easycc-cloud-", suffix=".cbox")
        os.close(fd)
        if "json" in (resp.headers.get("Content-Type") or ""):
            # 存在云存储上：从 CDN 临时链接下载密文，本机解密，按服务器记的 SHA-256 校验
            with resp:
                j = json.loads(resp.read())
            enc = path + ".enc"
            try:
                with urllib.request.urlopen(urllib.request.Request(j["cdn"]["url"], headers={"User-Agent": "easyCC/%s" % self.version}), timeout=60) as r2:
                    _save(r2, enc, int(j.get("size") or 0), progress)
                _openssl_ctr(ossl, j["cdn"]["key"], j["cdn"]["iv"], enc, path)
            except (urllib.error.URLError, OSError) as e:
                raise CloudError("从云存储下载失败：%s" % getattr(e, "reason", e))
            finally:
                if os.path.exists(enc):
                    os.remove(enc)
            if _sha256_file(path) != j["sha256"]:
                os.remove(path)
                raise CloudError("下载的环境包校验不对，请重试")
            os.chmod(path, 0o600)
            return path, int(j.get("version") or 0)
        with resp:
            _save(resp, path, int(resp.headers.get("Content-Length") or 0), progress)
        os.chmod(path, 0o600)
        return path, int(resp.headers.get("X-EasyCC-Version") or 0)

    def plans(self):
        return self.call("GET", "/v1/plans")

    def redeem(self, code):
        r = self.call("POST", "/v1/redeem", {"code": code})
        st = self.state()
        if st.get("user") is not None:
            st["user"]["subscription"] = r.get("subscription")
            self.save(user=st["user"])
        return r

    def delete(self, name):
        return self.call("DELETE", "/v1/envs/" + name)

    def report(self, device, envs):
        r = self.call("POST", "/v1/report", {"device": device, "envs": envs}, timeout=20)
        st = self.state()
        if st.get("user") and "subscription" in r:
            st["user"]["subscription"] = r["subscription"]
            self.save(user=st["user"], reportedAt=int(time.time()))
        return r


def _sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _openssl_ctr(ossl, key_hex, iv_hex, src, dst):
    """AES-256-CTR 加解密（同一个操作）：用系统自带的 openssl，Python 标准库没有 AES"""
    p = subprocess.run([ossl, "enc", "-aes-256-ctr", "-K", key_hex, "-iv", iv_hex, "-in", src, "-out", dst], capture_output=True, text=True)
    if p.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
        raise CloudError("本机加解密失败：%s" % (p.stderr.strip()[-200:] or "openssl 出错"))


def _save(resp, path, total, progress=None):
    got = 0
    with open(path, "wb") as f:
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            f.write(chunk)
            got += len(chunk)
            if progress:
                progress(got, total)


class _Reader:
    """上传时边读边报进度"""
    def __init__(self, f, total, progress):
        self.f, self.total, self.got, self.progress = f, total, 0, progress

    def read(self, n=-1):
        b = self.f.read(n if n and n > 0 else 1 << 20)
        self.got += len(b)
        if self.progress and b:
            self.progress(self.got, self.total)
        return b


def computer_name():
    if platform.system() == "Darwin":
        try:
            n = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=3).stdout.strip()
            if n:
                return n
        except (OSError, subprocess.TimeoutExpired):
            pass
    return socket.gethostname()


def os_info():
    s = platform.system()
    if s == "Darwin":
        return "macOS", platform.mac_ver()[0]
    if s == "Windows":
        return "Windows", platform.version()
    try:
        d = dict(l.strip().split("=", 1) for l in open("/etc/os-release") if "=" in l)
        return "Linux", d.get("PRETTY_NAME", "").strip('"')
    except OSError:
        return "Linux", platform.release()


def device_brief(version):
    o, v = os_info()
    return {"name": computer_name(), "os": "%s %s" % (o, v), "version": version}


def installed_agents(home):
    b = os.path.join(home, ".local", "bin")
    return [aid for aid, binname in AGENT_BINS.items() if os.path.lexists(os.path.join(b, binname))]
