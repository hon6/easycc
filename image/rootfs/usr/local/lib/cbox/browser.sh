# 环境浏览器的公共函数（cbox-browser、cbox-browser-keeper 引用）
# 用哪个浏览器：/run/cbox/browser（cbox-net / cbox-userland 按环境配置 BROWSER 写，镜像里没有 Chrome 就是 firefox）
cbox_engine() { local b; b="$(cat /run/cbox/browser 2>/dev/null)"; [ "$b" = chrome ] && [ -x /usr/bin/google-chrome ] && echo chrome || echo firefox; }

# 判断浏览器在不在运行用的进程特征
cbox_browser_pat() { if [ "$1" = chrome ]; then echo '/opt/google/chrome/chrome( |$)'; else echo '/usr/lib/firefox/firefox-bin'; fi; }

# 浏览器没在运行时，清掉上次留下的配置锁：锁里记着主机名，环境换了机器身份（主机名变了）或容器重建后，
# 浏览器会以为配置正被「另一台电脑」使用而拒绝启动
cbox_clear_locks() {
  rm -f "$HOME/.config/google-chrome/SingletonLock" "$HOME/.config/google-chrome/SingletonSocket" "$HOME/.config/google-chrome/SingletonCookie"
  find "$HOME/.mozilla" "$HOME/.config/mozilla" -maxdepth 4 \( -name lock -o -name .parentlock \) -delete 2>/dev/null
  return 0
}

# 启动 Chrome：英文界面；代理、WebRTC、语言、定位等由策略 /etc/opt/chrome/policies/managed/easycc.json 锁定；时区跟 TZ
cbox_chrome() {
  local dir="$HOME/.config/google-chrome" fl
  fl=(--lang=en-US --no-first-run --no-default-browser-check --password-store=basic --disable-dev-shm-usage
      --start-maximized --user-data-dir="$dir")
  # 被关掉 / 重启过的不算崩溃：不弹「恢复页面」，按策略直接恢复上次的标签页
  [ -f "$dir/Default/Preferences" ] && sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/; s/"exited_cleanly":false/"exited_cleanly":true/' "$dir/Default/Preferences"
  [ -f "$HOME/.cache/easycc-chrome-nosandbox" ] && fl+=(--no-sandbox --test-type)
  LANGUAGE=en_US:en LANG=en_US.UTF-8 google-chrome "${fl[@]}" "$@"
}
