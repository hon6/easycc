#!/usr/bin/env python3
"""easyCC 的 Linux 窗口（GTK + WebKitGTK，系统自带的组件，不用另装浏览器）。
启动自带的管理服务（bin/easycc-server），拿到地址后在窗口里打开；环境后台、浏览器桌面开新窗口，外部网站交给系统浏览器。
用系统的 python3 运行（要有 PyGObject 和 WebKit2GTK 4.1/4.0，主流桌面发行版默认都有）。"""
import os
import re
import signal
import subprocess
import sys
import threading

import gi

for v in ("4.1", "4.0"):
    try:
        gi.require_version("WebKit2", v)
        break
    except ValueError:
        continue
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "bin", "easycc-server")
LOCAL = re.compile(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?/")


class Shell:
    def __init__(self):
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        self.ctx = WebKit2.WebContext.get_default()
        self.ctx.connect("download-started", self.on_download)
        self.windows = []
        self.main = self.new_window(1440, 900, "easyCC")
        self.main.connect("destroy", self.quit)
        self.main_view.load_html(SPLASH % "正在启动…", None)
        self.proc = None
        threading.Thread(target=self.start_server, daemon=True).start()

    def make_view(self, related=None):
        v = WebKit2.WebView.new_with_related_view(related) if related else WebKit2.WebView.new_with_context(self.ctx)
        s = v.get_settings()
        s.set_enable_developer_extras(True)
        s.set_javascript_can_access_clipboard(True)      # 环境浏览器的复制粘贴
        s.set_javascript_can_open_windows_automatically(True)
        v.connect("create", self.on_create)
        v.connect("decide-policy", self.on_policy)
        return v

    def new_window(self, w, h, title, view=None):
        win = Gtk.Window(title=title)
        win.set_default_size(w, h)
        win.set_icon_name("easycc")
        view = view or self.make_view()
        view.connect("notify::title", lambda v, _p: win.set_title(v.get_title() or "easyCC"))
        view.connect("close", lambda v: win.destroy())
        win.add(view)
        win.show_all()
        if not self.windows:
            self.main_view = view
        self.windows.append(win)
        win.connect("destroy", lambda w_: self.windows.remove(w_) if w_ in self.windows else None)
        return win

    # 新窗口：本机地址（环境后台、浏览器桌面）在 App 里开；外部网站交给系统浏览器
    def on_create(self, view, action):
        uri = action.get_request().get_uri() or ""
        if uri and not LOCAL.match(uri) and not uri.startswith("about:"):
            subprocess.Popen(["xdg-open", uri])
            return None
        nv = self.make_view(view)
        self.new_window(1500, 960, "easyCC", nv)
        return nv

    def on_policy(self, view, decision, kind):
        if kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri() or ""
            if uri.startswith("http") and not LOCAL.match(uri):
                subprocess.Popen(["xdg-open", uri])
                decision.ignore()
                return True
        return False

    def on_download(self, ctx, download):
        def dest(d, suggested):
            folder = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or os.path.expanduser("~")
            d.set_destination("file://" + os.path.join(folder, suggested))
            return True
        download.connect("decide-destination", dest)

    def start_server(self):
        try:
            self.proc = subprocess.Popen([SERVER, "--no-open"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        except OSError as e:
            GLib.idle_add(self.main_view.load_html, SPLASH % ("启动失败：%s" % e), None)
            return
        for line in self.proc.stdout:
            m = re.search(r"http://127\.0\.0\.1:\d+/login\?k=[A-Za-z0-9_-]+", line)
            if m:
                GLib.idle_add(self.main_view.load_uri, m.group(0))
                break
        else:
            GLib.idle_add(self.main_view.load_html, SPLASH % "管理服务没有启动起来，日志：~/.easycc/manager.log", None)
            return
        for _ in self.proc.stdout:      # 后面的输出丢掉，免得管道写满
            pass

    def quit(self, *_):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass
        Gtk.main_quit()


SPLASH = """<html><body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:18px;
background:#0a0d16;color:#9aa6ba;font:14px sans-serif"><svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="#5ccfe6" stroke-width="1.4">
<path d="M12 2 21 7v10l-9 5-9-5V7z"/><path d="M12 7v10M7.5 9.5 12 12l4.5-2.5" opacity=".7"/></svg>
<div style="letter-spacing:3px;color:#eef3fa;font:600 15px monospace">easyCC</div><div>%s</div></body></html>"""

if __name__ == "__main__":
    GLib.set_prgname("easycc")
    Shell()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    Gtk.main()
    sys.exit(0)
