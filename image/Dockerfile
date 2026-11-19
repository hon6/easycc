# easycc：专门跑 Claude Code 的隔离网络环境（通用镜像：Docker / Podman / OrbStack / Apple container 都能用）
# Ubuntu 24.04 + mihomo（唯一出口）+ iptables 防火墙 + Firefox（noVNC 网页桌面）+ sshd + 管理后台 + Kiteline（amd64）
# 运行时必须带 --cap-add NET_ADMIN（防火墙装不上会拒绝启动，绝不裸连）
# 基础镜像：easycc build 会先从国内镜像站下载 Ubuntu 官方底包（tools/base-image.py），载入成 easycc-base:24.04-<架构> 再传进来，
#           不依赖任何镜像仓库；单独用 docker build 时默认用 Canonical 在 AWS 公共仓库的同一份 Ubuntu
ARG BASE_IMAGE=public.ecr.aws/ubuntu/ubuntu:24.04
FROM ${BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
# 挂载目录的文件属主：Mac 默认 501/20，Linux 一般 1000/1000（easycc build 会自动传本机的）
ARG HOST_UID=501
ARG HOST_GID=20
# mihomo 版本（留空 = 下面固定的 v1.19.33，带官方 SHA-256 校验；改别的版本就不校验，只用于调试）
ARG MIHOMO_VERSION=
# Kiteline：auto = 只在 amd64 镜像里装（官方 Linux 版只有 x86_64）；1 = 必须装；0 = 不装
ARG WITH_KITELINE=auto
# Kiteline 依赖的 pip 源（留空 = 它默认的清华镜像）
ARG PIP_INDEX_URL=
# apt 源（easycc build 测速后传进来，如 http://mirrors.aliyun.com/ubuntu；留空 = Ubuntu 官方源）
ARG APT_MIRROR=
# Node.js 下载源（easycc build 测速后传进来；下不到再用官方 nodejs.org）
ARG NODE_MIRROR=https://nodejs.org/dist

# 先换 apt 源（http 即可：apt 自己校验签名；底包里还没有 CA 证书）
RUN if [ -n "$APT_MIRROR" ]; then \
      for f in /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list; do \
        [ -f "$f" ] && sed -i -E "s#^URIs: .*#URIs: ${APT_MIRROR}/#; s#https?://(archive|security|ports)\.ubuntu\.com/ubuntu(-ports)?/?#${APT_MIRROR}/#g" "$f"; \
      done; echo "apt 源：$APT_MIRROR"; fi

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl wget gnupg jq gzip xz-utils tzdata locales \
      iptables iproute2 iputils-ping dnsutils netcat-openbsd procps psmisc less util-linux \
      git openssh-client openssh-server vim nano unzip zip ripgrep tmux build-essential \
      python3 python3-pip python3-venv python3-qrcode \
      tigervnc-standalone-server tigervnc-tools novnc websockify openbox xterm \
      dbus-x11 x11-xserver-utils xdg-utils \
      fonts-noto-cjk fonts-noto-color-emoji fonts-dejavu \
 && sed -i 's/^# *\(en_US.UTF-8\)/\1/; s/^# *\(zh_CN.UTF-8\)/\1/' /etc/locale.gen && locale-gen \
 && rm -rf /var/lib/apt/lists/*

# 网络不稳时自动重试：之后所有 curl（含 nodesource 安装脚本里的）和 apt 都会重试
RUN printf 'retry = 6\nretry-all-errors\nretry-delay = 3\nconnect-timeout = 20\n' > /root/.curlrc \
 && printf 'Acquire::Retries "6";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' > /etc/apt/apt.conf.d/80-retries

# tools/prefetch-vendor.sh 提前下载的大文件（可以没有）：有就用本地的，没有才联网下载
COPY vendor/ /tmp/vendor/

# Firefox：Ubuntu 自带的是 snap 版，容器里跑不了，改用 Mozilla 官方 APT 源（有 amd64 / arm64）
# 有提前下好的包就只用它（不加 Mozilla 源：源里版本更新时 apt 会改去下载，国内很慢）
RUN deb="$(ls /tmp/vendor/firefox_*_$(dpkg --print-architecture).deb 2>/dev/null | head -1)"; \
    if [ -n "$deb" ]; then apt-get update && apt-get install -y --no-install-recommends "$deb"; \
    else install -d -m 0755 /etc/apt/keyrings \
      && curl -fsSL -o /etc/apt/keyrings/packages.mozilla.org.asc https://packages.mozilla.org/apt/repo-signing-key.gpg \
      && echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" > /etc/apt/sources.list.d/mozilla.list \
      && printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > /etc/apt/preferences.d/mozilla \
      && apt-get update && apt-get install -y --no-install-recommends firefox; fi \
 && rm -rf /var/lib/apt/lists/* && firefox --version

# Google Chrome（环境里可以选 Firefox 或 Chrome；Google 只出 amd64 的 Linux 版）：优先用 tools/prefetch-vendor.sh 下好的包，
# 没有就从 Google 官方 APT 源下载并按源里的 SHA-256 校验；下载失败不影响构建（这个镜像就只带 Firefox，环境里选 Chrome 会用 Firefox）
RUN set -e; if [ "$(dpkg --print-architecture)" = amd64 ]; then \
      deb="$(ls /tmp/vendor/google-chrome-stable_*_amd64.deb 2>/dev/null | head -1)"; \
      if [ -z "$deb" ]; then \
        line="$(curl -fsSL --retry 3 https://dl.google.com/linux/chrome/deb/dists/stable/main/binary-amd64/Packages 2>/dev/null \
          | awk -v RS= '/Package: google-chrome-stable\n/' | awk '/^Filename:/{f=$2} /^SHA256:/{s=$2} END{print f, s}')" || true; \
        if [ -n "${line% *}" ] && curl -fsSL --retry 3 -o /tmp/chrome.deb "https://dl.google.com/linux/chrome/deb/${line% *}" \
           && echo "${line#* }  /tmp/chrome.deb" | sha256sum -c -; then deb=/tmp/chrome.deb; fi; \
      fi; \
      if [ -n "$deb" ]; then \
        echo 'repo_add_once="false"' > /etc/default/google-chrome; \
        apt-get update && apt-get install -y --no-install-recommends "$deb"; \
        rm -f /etc/apt/sources.list.d/google-chrome.list /etc/cron.daily/google-chrome /tmp/chrome.deb; \
        rm -rf /var/lib/apt/lists/*; google-chrome --version; \
      else echo "!! 没下载到 Google Chrome，这个镜像只带 Firefox"; fi; \
    fi

# 字体库：一批常见的免费字体。每个环境只启用其中一部分、默认字体也各不相同（cbox-persona 按环境的种子选），
# 环境之间的字体列表和网页文字渲染是真的不一样，不靠伪造
RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-liberation fonts-liberation2 fonts-open-sans fonts-roboto-unhinted fonts-lato fonts-ubuntu fonts-inter \
      fonts-cantarell fonts-noto-core fonts-freefont-ttf fonts-crosextra-carlito fonts-crosextra-caladea fonts-texgyre \
      fonts-firacode fonts-jetbrains-mono fonts-hack fonts-noto-mono \
 && rm -rf /var/lib/apt/lists/* \
 && : > /etc/machine-id && rm -f /var/lib/dbus/machine-id    # machine-id 由 cbox-persona 按环境生成，镜像里不带

# Node.js 22（agent 和 MCP 工具链需要）：官方二进制，从镜像站下载并按官方 SHASUMS 校验
RUN set -eux; case "$(dpkg --print-architecture)" in amd64) n=x64 ;; arm64) n=arm64 ;; *) exit 1 ;; esac; \
    for m in $NODE_MIRROR https://nodejs.org/dist; do \
      line="$(curl -fsSL "$m/latest-v22.x/SHASUMS256.txt" | grep -E " node-v22[.0-9]+-linux-$n\.tar\.xz$" || true)"; [ -n "$line" ] || continue; \
      curl -fsSL -o /tmp/node.tar.xz "$m/latest-v22.x/${line##* }" || continue; \
      echo "${line%% *}  /tmp/node.tar.xz" | sha256sum -c - && break; rm -f /tmp/node.tar.xz; \
    done; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner --exclude='*/include' --exclude='*/share/doc' \
      --exclude='*/CHANGELOG.md' --exclude='*/README.md' --exclude='*/LICENSE'; \
    rm -f /tmp/node.tar.xz; node -v; npm -v

# mihomo：版本固定，官方文件的 SHA-256 写在这里；先试 GitHub 直连，再试国内常用的 GitHub 加速站（校验对不上就拒绝，不怕被换包）
ARG MIHOMO_SHA256_AMD64=386508bfb1c72c43b6a0eda73708ecdce82546581b0ebcb16366c53757c622b6
ARG MIHOMO_SHA256_ARM64=ba408b317080ee265c46cb923a50018c1b82cbd708551cdd46a1e3e307396187
ARG GITHUB_PROXIES="https://ghfast.top/ https://gh-proxy.com/"
RUN set -eu; arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) asset=amd64-compatible; want="$MIHOMO_SHA256_AMD64" ;; arm64) asset=arm64; want="$MIHOMO_SHA256_ARM64" ;; *) echo "不支持的架构 $arch"; exit 1 ;; esac; \
    ver="${MIHOMO_VERSION:-v1.19.33}"; [ "$ver" = v1.19.33 ] || want=""; \
    f="mihomo-linux-${asset}-${ver}.gz"; url="https://github.com/MetaCubeX/mihomo/releases/download/${ver}/${f}"; ok=""; \
    if [ -f "/tmp/vendor/$f" ]; then cp "/tmp/vendor/$f" /tmp/m.gz; ok=1; fi; \
    for p in "" $GITHUB_PROXIES; do \
      [ -n "$ok" ] && break; echo "下载 mihomo：${p}${url}"; \
      curl -fsSL --retry 2 -m 180 -o /tmp/m.gz "${p}${url}" || continue; \
      if [ -z "$want" ] || [ "$(sha256sum /tmp/m.gz | cut -d' ' -f1)" = "$want" ]; then ok=1; else echo "校验不对，换下一个"; fi; \
    done; \
    [ -n "$ok" ] || { echo "mihomo 下载失败"; exit 1; }; \
    [ -z "$want" ] || [ "$(sha256sum /tmp/m.gz | cut -d' ' -f1)" = "$want" ]; \
    gunzip -c /tmp/m.gz > /usr/local/bin/mihomo; rm -f /tmp/m.gz; chmod 755 /usr/local/bin/mihomo; /usr/local/bin/mihomo -v

# 用户：dev（日常使用，无 sudo，碰不到防火墙）、mihomo（唯一允许出网的身份）；需要 root 时从主机用 easycc root
RUN set -eux; \
    userdel -r ubuntu 2>/dev/null || true; \
    getent group "${HOST_GID}" >/dev/null || groupadd -g "${HOST_GID}" devgrp; \
    useradd -m -s /bin/bash -u "${HOST_UID}" -g "${HOST_GID}" dev; \
    usermod -p '*' dev; \
    useradd -r -s /usr/sbin/nologin -M mihomo; \
    rm -f /etc/ssh/ssh_host_*; \
    mkdir -p /projects /etc/mihomo /etc/cbox /run/cbox; chown dev:"${HOST_GID}" /projects; ln -s /projects /workspace; \
    cp -a /home/dev /opt/dev-skel

# Kiteline 电脑端（无界面模式），装到 /opt/kiteline，按官网公布的 SHA-256 校验
RUN set -eux; arch="$(dpkg --print-architecture)"; \
    if [ "$WITH_KITELINE" = 0 ] || { [ "$WITH_KITELINE" = auto ] && [ "$arch" != amd64 ]; }; then echo "跳过 Kiteline（$arch）"; exit 0; fi; \
    [ "$arch" = amd64 ] || { echo "Kiteline 官方 Linux 版只有 x86_64，请用 --platform linux/amd64 构建"; exit 1; }; \
    d="$(mktemp -d)"; cd "$d"; \
    if [ -f /tmp/vendor/Kiteline-Linux.tar.gz ]; then cp /tmp/vendor/Kiteline-Linux.tar.gz k.tgz; cp /tmp/vendor/Kiteline-Linux.tar.gz.sha256 k.sha256; else \
      curl -fsSL https://tars.betac.cn/download/Kiteline-Linux.tar.gz -o k.tgz; \
      curl -fsSL https://tars.betac.cn/download/Kiteline-Linux.tar.gz.sha256 -o k.sha256; fi; \
    [ "$(cut -d' ' -f1 k.sha256)" = "$(sha256sum k.tgz | cut -d' ' -f1)" ]; \
    tar -xzf k.tgz; \
    if [ -n "$PIP_INDEX_URL" ]; then export PIP_INDEX_URL; else unset PIP_INDEX_URL; fi; \
    mkdir -p home; HOME="$d/home" KITELINE_HOME=/opt/kiteline bash Kiteline/install.sh --update; \
    cd /; rm -rf "$d"; chown -R dev /opt/kiteline; \
    printf '#!/bin/bash\nexport KITELINE_HOME=/opt/kiteline\ncd /opt/kiteline && exec /opt/kiteline/venv/bin/python -m kiteline "$@"\n' > /usr/local/bin/kiteline; \
    chmod 755 /usr/local/bin/kiteline

# 桌面截图（x11-apps）、openbox 读 XDG 自启动（python3-xdg）
RUN apt-get update && apt-get install -y --no-install-recommends x11-apps netpbm python3-xdg && rm -rf /var/lib/apt/lists/*

COPY rootfs/ /
RUN rm -rf /tmp/vendor
# 系统里只留英文 locale（浏览器、桌面不带任何中文 / 中国相关的语言设置；中文字体 Noto CJK 保留，网页里的中日韩文字照常显示）
RUN sed -i 's/^zh_CN.UTF-8/# zh_CN.UTF-8/' /etc/locale.gen && locale-gen && ! locale -a | grep -qi zh
# 环境桌面里浏览器（Firefox / Chrome）一打开就最大化、无边框：打开桌面看到的就是一个浏览器
RUN chmod 755 /usr/local/sbin/* /usr/local/bin/cbox-* /usr/local/bin/easycc-env \
 && ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html \
 && grep -q '<applications>' /etc/xdg/openbox/rc.xml \
 && sed -i '0,/<applications>/s##<applications>\n  <application class="*irefox*" type="normal"><decor>no</decor><maximized>true</maximized></application>\n  <application class="Google-chrome" type="normal"><decor>no</decor><maximized>true</maximized></application>#' /etc/xdg/openbox/rc.xml

ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    DISPLAY=:1

# /projects 挂项目根目录；/workspace 是指向当前项目的链接（管理后台切换）
WORKDIR /projects
# 7681 管理后台 · 6080 浏览器桌面 · 22 SSH
EXPOSE 7681 6080 22
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s CMD grep -qx running /run/cbox/state
ENTRYPOINT ["/usr/local/sbin/cbox-init"]
