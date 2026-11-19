"""环境里的 AI 编程 agent：清单、一键安装 / 卸载、安装日志。由 admin.py（root）调用。

- 环境默认不预装任何 agent，用户在环境后台挑选安装。
- 安装在 dev 用户下进行，程序放在 ~/.local（home 挂在电脑上，重启环境不丢）。
- 每次安装写日志 /run/cbox/agents/<id>.log、状态 <id>.json，页面轮询日志显示安装过程。
- 网络走环境的出口（透明代理），npm 官方源不通时自动换国内镜像源。
"""
import json
import os
import re
import subprocess
import time

RUN = "/run/cbox/agents"
DEFAULT_FILE = "/home/dev/.config/easycc/default-agent"
WANTED_FILE = "/home/dev/.config/easycc/agents.json"     # 用户装过的 agent：环境恢复到别的电脑后自动重装

NPM = '''
npm config set prefix "$HOME/.local" >/dev/null 2>&1
# 安装加速：请电脑本机从国内镜像下载程序包放进 ~/.cache/easycc/pkgs（环境的流量照样全走代理），再加进 npm 缓存
P="$HOME/.cache/easycc/pkgs/{id}"; mkdir -p "$HOME/.cache/easycc/pkg-requests"; rm -rf "$P"
printf '{{"npm":"{pkg}","at":%s}}' "$(date +%s)" > "$HOME/.cache/easycc/pkg-requests/{id}.json"
echo "==> 请电脑本机下载程序包（国内镜像，更快）"
# 电脑本机在下载就一直等（30 秒没进度才放弃），每 5 秒报一次进度；最多 30 分钟
w=0; lp=""; still=0; shown=0
while [ $w -lt 1800 ] && [ ! -f "$P/.done" ] && [ ! -f "$P/.error" ]; do
  sleep 1; w=$((w+1))
  pr="$(cat "$P/.progress" 2>/dev/null)"
  if [ -n "$pr" ] && [ "$pr" != "$lp" ]; then
    lp="$pr"; still=0; read -r pn pg pt <<< "$pr"
    if [ $((w - shown)) -ge 5 ] && [ "${{pt:-0}}" -gt 0 ]; then shown=$w; echo "   电脑本机下载中：$pn $(( pg * 100 / pt ))%（$(( pg >> 20 )) / $(( pt >> 20 )) MB）"; fi
  else
    still=$((still+1)); [ -z "$pr" ] && [ $w -ge 90 ] && break; [ $still -ge 30 ] && [ -n "$pr" ] && break
  fi
done
if [ -f "$P/.done" ] && ls "$P"/*.tgz >/dev/null 2>&1; then
  for t in "$P"/*.tgz; do npm cache add "$t" >/dev/null 2>&1 && echo "   已放入缓存：$(basename "$t")"; done
else
  echo "   电脑本机没下好（$(cat "$P/.error" 2>/dev/null || echo 超时)），直接经代理下载"
fi
rm -f "$HOME/.cache/easycc/pkg-requests/{id}.json"
echo "==> npm install -g {pkg}"
npm install -g {pkg} --prefer-offline --no-fund --no-audit --loglevel=http || {{
  echo "==> 官方源失败，改用国内镜像源重试"
  npm install -g {pkg} --no-fund --no-audit --loglevel=http --registry https://registry.npmmirror.com
}}
'''

AGENTS = [
    {"id": "claude", "name": "Claude Code", "vendor": "Anthropic", "bin": "claude", "color": "#d97757",
     "desc": "Anthropic 官方命令行编程助手，需要 Claude 账号或 API Key",
     "home": "https://docs.anthropic.com/claude-code",
     # 先用 npm（官方 npm 包；程序包由电脑本机从国内镜像预下载，快），不行再用官方安装脚本
     "install": NPM.format(pkg="@anthropic-ai/claude-code", id="claude") + '''
hash -r
if ! command -v claude >/dev/null; then
  f=$(mktemp)
  echo "==> npm 没装上，改用官方安装脚本 https://claude.ai/install.sh"
  if curl -q -fsSL --max-time 60 -o "$f" https://claude.ai/install.sh && head -c 2 "$f" | grep -q "#!"; then bash "$f"
  else echo "==> 官方安装脚本拿不到（出口地区不支持时 claude.ai 返回网页）"; fi
  rm -f "$f"
fi
''',
     "uninstall": 'rm -f ~/.local/bin/claude; rm -rf ~/.local/share/claude; npm uninstall -g @anthropic-ai/claude-code --prefix ~/.local >/dev/null 2>&1; true'},
    {"id": "codex", "name": "Codex CLI", "vendor": "OpenAI", "bin": "codex", "color": "#10a37f",
     "desc": "OpenAI 的命令行编程 agent，ChatGPT 账号或 API Key 登录",
     "home": "https://github.com/openai/codex",
     "install": NPM.format(pkg="@openai/codex", id="codex"), "uninstall": "npm uninstall -g @openai/codex --prefix ~/.local"},
    {"id": "gemini", "name": "Gemini CLI", "vendor": "Google", "bin": "gemini", "color": "#4285f4",
     "desc": "Google 开源的命令行 agent，Google 账号登录有免费额度",
     "home": "https://github.com/google-gemini/gemini-cli",
     "install": NPM.format(pkg="@google/gemini-cli", id="gemini"), "uninstall": "npm uninstall -g @google/gemini-cli --prefix ~/.local"},
    {"id": "cursor", "name": "Cursor CLI", "vendor": "Cursor", "bin": "cursor-agent", "color": "#8b8b8b",
     "desc": "Cursor 的命令行 agent（cursor-agent），用 Cursor 账号登录",
     "home": "https://cursor.com/cli",
     "install": '''
echo "==> 下载 https://cursor.com/install"
curl -q -fsSL --max-time 120 https://cursor.com/install -o /tmp/cursor-install.sh && head -c 2 /tmp/cursor-install.sh | grep -q "#!" || { echo "安装脚本下载失败"; exit 1; }
bash /tmp/cursor-install.sh; rc=$?; rm -f /tmp/cursor-install.sh; exit $rc
''', "uninstall": "rm -f ~/.local/bin/cursor-agent ~/.local/bin/agent; rm -rf ~/.local/share/cursor-agent; true"},
    {"id": "copilot", "name": "Copilot CLI", "vendor": "GitHub", "bin": "copilot", "color": "#a371f7",
     "desc": "GitHub Copilot 的命令行 agent，需要 Copilot 订阅",
     "home": "https://github.com/github/copilot-cli",
     "install": NPM.format(pkg="@github/copilot", id="copilot"), "uninstall": "npm uninstall -g @github/copilot --prefix ~/.local"},
    {"id": "opencode", "name": "opencode", "vendor": "SST", "bin": "opencode", "color": "#f0b429",
     "desc": "开源终端 agent，可接 Claude、OpenAI、DeepSeek 等各家模型",
     "home": "https://github.com/sst/opencode",
     "install": NPM.format(pkg="opencode-ai", id="opencode"), "uninstall": "npm uninstall -g opencode-ai --prefix ~/.local"},
    {"id": "qwen", "name": "Qwen Code", "vendor": "阿里通义", "bin": "qwen", "color": "#615ced",
     "desc": "通义千问的命令行编程 agent，国内网络友好，有免费额度",
     "home": "https://github.com/QwenLM/qwen-code",
     "install": NPM.format(pkg="@qwen-code/qwen-code", id="qwen"), "uninstall": "npm uninstall -g @qwen-code/qwen-code --prefix ~/.local"},
    {"id": "kimi", "name": "Kimi CLI", "vendor": "月之暗面", "bin": "kimi", "color": "#3b82f6",
     "desc": "Kimi 的命令行 agent（Python 版，用 uv 安装）",
     "home": "https://github.com/MoonshotAI/kimi-cli",
     "install": '''
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
  echo "==> 安装 uv（Python 包管理器）"
  curl -q -LsSf --max-time 120 https://astral.sh/uv/install.sh | sh || pip3 install --user --break-system-packages uv || exit 1
fi
echo "==> uv tool install kimi-cli"
uv tool install --python 3.13 kimi-cli || UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple uv tool install --python 3.13 kimi-cli
''', "uninstall": 'export PATH="$HOME/.local/bin:$PATH"; uv tool uninstall kimi-cli'},
]
BY_ID = {a["id"]: a for a in AGENTS}


def _dev(cmd, timeout=15):
    try:
        p = subprocess.run(["runuser", "-u", "dev", "--", "bash", "-lc", cmd], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def _state(aid):
    try:
        return json.load(open(os.path.join(RUN, aid + ".json")))
    except (OSError, ValueError):
        return {}


def _running(st):
    return st.get("status") == "running" and st.get("pid") and os.path.exists("/proc/%d" % st["pid"])


def versions():
    """一次性查所有 agent 是否装了、版本（只跑一个 shell）"""
    script = "; ".join('p=$(command -v %s) && echo "%s|$p|$(timeout 8 %s --version 2>/dev/null | head -1)"' % (a["bin"], a["id"], a["bin"])
                       for a in AGENTS)
    _, out = _dev("export PATH=$HOME/.local/bin:$PATH; " + script, 60)
    res = {}
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0] in BY_ID:
            m = re.search(r"\d+\.\d+[\w.\-]*", parts[2])
            res[parts[0]] = {"path": parts[1], "version": m.group(0) if m else parts[2][:40]}
    return res


_ver_cache = {"t": 0, "v": {}}


def list_agents(fresh=False):
    if fresh or time.time() - _ver_cache["t"] > 20:
        _ver_cache.update(t=time.time(), v=versions())
    vers = _ver_cache["v"]
    try:
        default = open(DEFAULT_FILE).read().strip()
    except OSError:
        default = ""
    out = []
    for a in AGENTS:
        st = _state(a["id"])
        status = "running" if _running(st) else ("installed" if a["id"] in vers else ("failed" if st.get("status") == "failed" else "none"))
        out.append({"id": a["id"], "icon": "/static/agents/%s.%s" % (a["id"], "svg" if a["id"] == "gemini" else "png"), "name": a["name"], "vendor": a["vendor"], "desc": a["desc"], "home": a["home"], "color": a["color"],
                    "bin": a["bin"], "status": status, "action": st.get("action", ""), "version": (vers.get(a["id"]) or {}).get("version", ""),
                    "startedAt": st.get("startedAt", 0), "endedAt": st.get("endedAt", 0), "exit": st.get("exit"),
                    "default": default == a["id"]})
    return out


def start(aid, action="install"):
    a = BY_ID.get(aid)
    if not a:
        return {"ok": False, "message": "没有这个 agent"}
    if _running(_state(aid)):
        return {"ok": False, "message": "%s 正在%s" % (a["name"], "安装" if _state(aid).get("action") == "install" else "卸载")}
    os.makedirs(RUN, exist_ok=True)
    log = os.path.join(RUN, aid + ".log")
    stf = os.path.join(RUN, aid + ".json")
    body = a["install"] if action == "install" else a["uninstall"]
    if action == "install":
        # 装完设成默认 agent（终端按钮打开它），并确认命令真的有了
        body += '\nexport PATH="$HOME/.local/bin:$PATH"; hash -r\nif command -v %s >/dev/null; then echo; echo "==> 安装完成：$(%s --version 2>/dev/null | head -1)"; mkdir -p ~/.config/easycc; echo %s > ~/.config/easycc/default-agent; else echo "==> 没找到 %s 命令，安装失败"; exit 1; fi\n' % (
            a["bin"], a["bin"], aid, a["bin"])
    else:
        body += '\necho "==> 已卸载"; [ "$(cat ~/.config/easycc/default-agent 2>/dev/null)" = %s ] && rm -f ~/.config/easycc/default-agent; true\n' % aid
    open(log, "w").write("[%s] 开始%s %s\n" % (time.strftime("%H:%M:%S"), "安装" if action == "install" else "卸载", a["name"]))
    os.chmod(log, 0o644)
    script = "set -o pipefail\nexport PATH=\"$HOME/.local/bin:$PATH\" NO_COLOR=1 CI=1\ncd ~\n" + body
    wrapper = '''runuser -u dev -- bash -lc %s >> %s 2>&1 < /dev/null; rc=$?
printf '{"status":"%%s","action":"%s","exit":%%d,"startedAt":%d,"endedAt":%%d}' "$([ $rc = 0 ] && echo done || echo failed)" $rc $(date +%%s) > %s''' % (
        _q(script), _q(log), action, int(time.time()), _q(stf))
    w = _wanted()
    if action == "install" and aid not in w:
        _set_wanted(w + [aid])
    elif action == "uninstall" and aid in w:
        _set_wanted([x for x in w if x != aid])
    p = subprocess.Popen(["bash", "-c", wrapper], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    json.dump({"status": "running", "action": action, "pid": p.pid, "startedAt": int(time.time())}, open(stf, "w"))
    _ver_cache["t"] = 0
    return {"ok": True, "message": "开始%s %s" % ("安装" if action == "install" else "卸载", a["name"])}


def _q(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def read_log(aid, offset=0):
    if aid not in BY_ID:
        return {"ok": False}
    path = os.path.join(RUN, aid + ".log")
    try:
        with open(path, "rb") as f:
            f.seek(max(0, offset))
            data = f.read(256 * 1024)
            size = f.tell()
    except OSError:
        data, size = b"", 0
    st = _state(aid)
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", data.decode("utf-8", "replace")).replace("\r\n", "\n")
    # 进度条用 \r 刷新同一行：只留每行最后一次
    text = "\n".join(l.split("\r")[-1] for l in text.split("\n"))
    return {"ok": True, "text": text, "offset": size, "running": _running(st), "status": st.get("status", ""), "exit": st.get("exit")}


def _wanted():
    try:
        return [a for a in json.load(open(WANTED_FILE)) if a in BY_ID]
    except (OSError, ValueError, TypeError):
        return []


def _set_wanted(lst):
    _dev("mkdir -p ~/.config/easycc && printf '%%s' %s > ~/.config/easycc/agents.json" % _q(json.dumps(sorted(set(lst)))))


def ensure_wanted():
    """环境启动时调用：装过的 agent 如果程序不在了（比如从云端恢复到新电脑），后台重新装上"""
    vers = versions()
    started = []
    for aid in _wanted():
        if aid not in vers and not _running(_state(aid)):
            start(aid, "install")
            started.append(aid)
    return started


def set_default(aid):
    if aid not in BY_ID:
        return {"ok": False, "message": "没有这个 agent"}
    _dev("mkdir -p ~/.config/easycc && echo %s > ~/.config/easycc/default-agent" % aid)
    return {"ok": True, "message": "终端按钮默认打开 %s" % BY_ID[aid]["name"]}
