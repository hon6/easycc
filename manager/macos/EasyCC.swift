// easyCC：原生 macOS 外壳。
// 启动 App 包里自带的管理服务（自带 Python、自带 Apple container），用 WKWebView 显示客户端界面。
// 用户装好 App 就能用，不需要另装 Docker、Python、Chrome。
import Cocoa
import WebKit

// 日志：~/Library/Application Support/easyCC/logs/app.log（页面加载、加载失败、网页里的 JS 错误）
let applogQueue = DispatchQueue(label: "cn.betac.easycc.applog")
func applog(_ s: String) {
    let line = "\(Date()) \(s)\n"
    // 不在主线程写：数据目录在外置盘时，系统第一次会弹「访问可移除宗卷」的授权，写文件要等用户点完才返回；
    // 在主线程等的话窗口不画也不响应，看起来像死机
    applogQueue.async {
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/easyCC/logs")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let f = dir.appendingPathComponent("app.log")
        if let h = try? FileHandle(forWritingTo: f) { h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); h.closeFile() }
        else { try? line.write(to: f, atomically: true, encoding: .utf8) }
    }
}

final class OpenExternal: NSObject, WKScriptMessageHandler {
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        if let s = m.body as? String, let u = URL(string: s), ["http", "https"].contains(u.scheme ?? "") { NSWorkspace.shared.open(u) }
    }
}

// 环境浏览器里复制的内容 → 电脑剪贴板
final class ClipOut: NSObject, WKScriptMessageHandler {
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        guard let s = m.body as? String else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
    }
}

final class JSLog: NSObject, WKScriptMessageHandler {
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) { applog("JS: \(m.body)") }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate, WKDownloadDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var server: Process?
    var extraWindows: [NSWindow] = []
    var titleObservers: [NSKeyValueObservation] = []
    let res = Bundle.main.resourceURL!

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let cfg = WKWebViewConfiguration()
        cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")
        // 网页里的报错写进日志，方便排查
        cfg.userContentController.add(JSLog(), name: "cboxlog")
        cfg.userContentController.add(OpenExternal(), name: "cboxopen")
        cfg.userContentController.add(ClipOut(), name: "cboxclip")
        cfg.userContentController.addUserScript(WKUserScript(source: """
          window.addEventListener('error', e => window.webkit.messageHandlers.cboxlog.postMessage('error: ' + e.message + ' @' + e.filename + ':' + e.lineno));
          window.addEventListener('unhandledrejection', e => window.webkit.messageHandlers.cboxlog.postMessage('promise: ' + (e.reason && e.reason.stack || e.reason)));
          """, injectionTime: .atDocumentStart, forMainFrameOnly: false))
        applog("启动")
        cfg.userContentController.add(DragRegions(), name: "cboxdrag")
        webView = MainWebView(frame: .zero, configuration: cfg)
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "easyCC"
        // 液态玻璃：窗口本身是系统毛玻璃（透出桌面），网页铺满整个窗口、背景透明；
        // 红绿灯挪进网页的悬浮侧栏里；顶部 52pt 当标题栏：空白处按住拖动、双击缩放，按钮照常点（网页把按钮位置报过来）
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.appearance = NSAppearance(named: .darkAqua)
        window.isOpaque = false
        window.backgroundColor = .clear
        window.minSize = NSSize(width: 960, height: 600)
        let glass = NSVisualEffectView()
        glass.material = .underWindowBackground
        glass.blendingMode = .behindWindow
        glass.state = .active
        webView.autoresizingMask = [.width, .height]
        glass.addSubview(webView)
        window.contentView = glass
        webView.frame = glass.bounds
        layoutTrafficLights()
        for n in [NSWindow.didResizeNotification, NSWindow.didEndLiveResizeNotification, NSWindow.didExitFullScreenNotification, NSWindow.didBecomeKeyNotification] {
            NotificationCenter.default.addObserver(forName: n, object: window, queue: .main) { [weak self] _ in self?.layoutTrafficLights() }
        }
        window.center()
        window.setFrameAutosaveName("EasyCCMain")
        window.makeKeyAndOrderFront(nil)
        webView.loadHTMLString(splash("正在启动…"), baseURL: nil)
        NSApp.activate(ignoringOtherApps: true)
        DispatchQueue.global().async { self.startServer() }
    }

    // 红绿灯：标题栏加高到 52pt，按钮放进悬浮侧栏左上角（参考 Electron 的 trafficLightPosition 做法；窗口变化后系统会复位，要重新摆）
    func layoutTrafficLights() {
        guard let close = window.standardWindowButton(.closeButton), let bar = close.superview, let container = bar.superview else { return }
        if window.styleMask.contains(.fullScreen) { return }
        let h: CGFloat = 52
        var f = container.frame
        f.size.height = h
        f.origin.y = window.frame.height - h
        container.frame = f
        bar.frame = NSRect(x: 0, y: 0, width: f.width, height: h)
        for (i, t) in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton].enumerated() {
            if let b = window.standardWindowButton(t) { b.setFrameOrigin(NSPoint(x: 22 + CGFloat(i) * 20, y: (h - b.frame.height) / 2 - 2)) }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ note: Notification) {
        server?.terminate()
    }

    // ---- 启动内置管理服务 ----
    func startServer() {
        let launcher = res.appendingPathComponent("bin/easycc-server")
        let p = Process()
        p.executableURL = launcher
        p.arguments = ["--no-open"]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle(forWritingAtPath: "/dev/null")
        do { try p.run() } catch {
            show("启动失败：\(error.localizedDescription)")
            return
        }
        server = p
        // 管理服务第一行输出：easycc 客户端：http://127.0.0.1:端口/login?k=令牌
        var buf = Data()
        let deadline = Date().addingTimeInterval(60)
        while Date() < deadline {
            let chunk = pipe.fileHandleForReading.availableData
            if chunk.isEmpty { if !p.isRunning { break }; continue }
            buf.append(chunk)
            if let s = String(data: buf, encoding: .utf8), let r = s.range(of: "http://127.0.0.1:[0-9]+/login\\?k=[A-Za-z0-9_-]+", options: .regularExpression) {
                let url = URL(string: String(s[r]))!
                applog("管理服务就绪，打开 \(url.host ?? ""):\(url.port ?? 0)")
                DispatchQueue.main.async { self.webView.load(URLRequest(url: url)) }
                // 后面的输出丢掉，避免管道写满卡住服务
                DispatchQueue.global().async { while p.isRunning { _ = pipe.fileHandleForReading.availableData } }
                return
            }
        }
        applog("管理服务没有输出地址：\(String(data: buf, encoding: .utf8) ?? "")")
        show("管理服务没有启动起来，日志：~/Library/Application Support/easyCC/logs/")
    }

    func show(_ msg: String) {
        DispatchQueue.main.async { self.webView.loadHTMLString(self.splash(msg), baseURL: nil) }
    }

    func splash(_ msg: String) -> String {
        return """
        <html><body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;flex-direction:column;
        background:transparent;color:rgba(255,255,255,.6);font:14px -apple-system,'PingFang SC';gap:18px">
        <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="#f2f3f5" stroke-width="1.4" style="filter:drop-shadow(0 2px 8px rgba(0,0,0,.4))">
        <path d="M12 2 21 7v10l-9 5-9-5V7z"/><path d="M12 7v10M7.5 9.5 12 12l4.5-2.5" opacity=".7"/></svg>
        <div style="letter-spacing:3px;color:#f4f4f6;font:600 15px ui-monospace,Menlo">easyCC</div><div>\(msg)</div></body></html>
        """
    }

    // ---- 新窗口：环境后台、浏览器桌面在 App 里开新窗口；外部网站交给系统浏览器 ----
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = action.request.url, let host = u.host, host != "127.0.0.1" && host != "localhost" {
            NSWorkspace.shared.open(u)
            return nil
        }
        let wv = WKWebView(frame: .zero, configuration: configuration)
        wv.uiDelegate = self
        wv.navigationDelegate = self
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1500, height: 960),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        w.title = "easyCC"
        w.appearance = NSAppearance(named: .darkAqua)
        w.contentView = wv
        w.isReleasedWhenClosed = false
        // 窗口标题跟着网页标题走（环境浏览器显示「环境名 浏览器」）
        titleObservers.append(wv.observe(\.title, options: [.new]) { v, _ in
            if let t = v.title, !t.isEmpty { v.window?.title = t }
        })
        w.cascadeTopLeft(from: window.frame.origin)
        w.center()
        w.makeKeyAndOrderFront(nil)
        extraWindows.append(w)
        return wv
    }

    func webViewDidClose(_ webView: WKWebView) {
        webView.window?.close()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        applog("加载失败 \(webView.url?.absoluteString ?? ""): \(error.localizedDescription)")
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        applog("打不开 \(webView.url?.absoluteString ?? ""): \(error.localizedDescription)")
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        applog("已加载 \(webView.url?.path ?? "")（\(webView.title ?? "")）")
        if let t = webView.title, !t.isEmpty, webView.window != window { webView.window?.title = t }
        // 把系统窗口的实际圆角告诉页面，悬浮侧栏、抽屉按它算同心圆角（macOS 27 是 16pt；读不到就用页面默认值）
        if webView == self.webView, let f = window.contentView?.superview, f.responds(to: NSSelectorFromString("_cornerRadius")),
           let r = (f.value(forKey: "_cornerRadius") as? NSNumber)?.doubleValue, r > 0, r < 60 {
            webView.evaluateJavaScript("document.documentElement.style.setProperty('--wr','\(Int(r))px')", completionHandler: nil)
        }
    }

    // ---- 文件选择（导入 .cbox）----
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.canChooseFiles = true
        panel.beginSheetModal(for: webView.window ?? window) { r in completionHandler(r == .OK ? panel.urls : nil) }
    }

    // ---- alert / confirm ----
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = message; a.addButton(withTitle: "好")
        a.beginSheetModal(for: webView.window ?? window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = message; a.addButton(withTitle: "确定"); a.addButton(withTitle: "取消")
        a.beginSheetModal(for: webView.window ?? window) { r in completionHandler(r == .alertFirstButtonReturn) }
    }

    // ---- 下载（导出环境）：存到「下载」文件夹 ----
    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if let r = navigationResponse.response as? HTTPURLResponse,
           (r.value(forHTTPHeaderField: "Content-Disposition") ?? "").contains("attachment") {
            decisionHandler(.download); return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        var dest = dir.appendingPathComponent(suggestedFilename)
        var i = 2
        while FileManager.default.fileExists(atPath: dest.path) {
            let base = (suggestedFilename as NSString).deletingPathExtension, ext = (suggestedFilename as NSString).pathExtension
            dest = dir.appendingPathComponent("\(base)-\(i).\(ext)"); i += 1
        }
        completionHandler(dest)
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let u = download.progress.fileURL { NSWorkspace.shared.activateFileViewerSelecting([u]) }
    }

    // ---- 菜单：复制粘贴、重新载入、退出 ----
    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 easyCC", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 easyCC", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "退出 easyCC", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        let pasteItem = edit.addItem(withTitle: "粘贴", action: #selector(smartPaste(_:)), keyEquivalent: "v")
        pasteItem.target = self
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "显示")
        view.addItem(withTitle: "重新载入", action: #selector(reloadPage), keyEquivalent: "r")
        viewItem.submenu = view
        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "窗口")
        win.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = win
        NSApp.mainMenu = main
        NSApp.windowsMenu = win
    }

    // ⌘V：环境浏览器窗口里，把电脑剪贴板送进环境再按 Ctrl+V；其它窗口照常粘贴
    @objc func smartPaste(_ sender: Any?) {
        if let wv = NSApp.keyWindow?.contentView as? WKWebView, wv.url?.path.hasSuffix("/easycc.html") == true {
            let text = NSPasteboard.general.string(forType: .string) ?? ""
            let js = (try? String(data: JSONSerialization.data(withJSONObject: [text]), encoding: .utf8)) ?? "[\"\"]"
            wv.evaluateJavaScript("window.easyccPaste && window.easyccPaste(\(js)[0])", completionHandler: nil)
            return
        }
        NSApp.sendAction(#selector(NSText.paste(_:)), to: nil, from: sender)
    }

    @objc func reloadPage() {
        ((NSApp.keyWindow?.contentView as? WKWebView) ?? webView).reload()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

// 主窗口的网页：顶部标题栏区域（网页报来的高度）里，不在按钮上的地方按下 = 拖动窗口，双击 = 缩放
var dragBand: CGFloat = 52
var dragHoles: [NSRect] = []

final class DragRegions: NSObject, WKScriptMessageHandler {
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        guard let s = m.body as? String, let d = s.data(using: .utf8),
              let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return }
        dragBand = CGFloat((o["h"] as? NSNumber)?.doubleValue ?? 52)
        dragHoles = ((o["r"] as? [[NSNumber]]) ?? []).compactMap { a in
            a.count == 4 ? NSRect(x: a[0].doubleValue, y: a[1].doubleValue, width: a[2].doubleValue, height: a[3].doubleValue) : nil }
    }
}

final class MainWebView: WKWebView {
    override func mouseDown(with e: NSEvent) {
        let p = convert(e.locationInWindow, from: nil)
        let top = isFlipped ? p.y : bounds.height - p.y          // 换成网页坐标（左上角为原点）
        let pt = NSPoint(x: p.x, y: top)
        if top < dragBand && !dragHoles.contains(where: { $0.contains(pt) }) {
            if e.clickCount == 2 {
                let act = UserDefaults.standard.string(forKey: "AppleActionOnDoubleClick") ?? "Maximize"
                if act == "Minimize" { window?.miniaturize(nil) } else if act != "None" { window?.zoom(nil) }
            } else {
                window?.performDrag(with: e)
            }
            return
        }
        super.mouseDown(with: e)
    }
}
