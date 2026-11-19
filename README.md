# easyCC

给 AI 编程 Agent 用的独立环境。在你的电脑上开出互相隔离的小环境，每个环境有自己的网络出口、浏览器、时区和语言；Claude Code、Codex、Gemini CLI 等装进去就能用，代码仍然留在你自己的硬盘上。

官网和下载：**https://easycc.betac.cn**（功能介绍、每个功能的用法、各平台安装包）

![easyCC 客户端的环境列表](site/img/envs.webp)

开源、免费，不用注册账号。支持 macOS（Apple 芯片和 Intel）、Windows（预览）和 Linux。

## 它做什么

- **只能经代理出网**：iptables 默认拒绝所有出站，只放行 mihomo 连上游 socks5 / http 代理；其余 TCP 透明转进 mihomo，DNS 由 mihomo 接管（fake-ip，查询经代理发出），UDP、ICMP、IPv6 一律封锁。防火墙装不上就拒绝启动。支持**链式代理**（`RELAY_URL`：环境 → 中转 → 落地，域名在落地节点解析）。
- **检查通过才放行外网**：环境启动时外网先关着（防火墙 `CBOX_GATE` 链挡住环境里的程序访问 mihomo），`gate.py` 的关键检查全部有结果且通过才打开：防火墙、直连封锁、代理连接、透明转发出口一致、IPv6、DNS 接管、出口地区（**中国大陆出口一律不放行**，内置大陆网段表 + 两家定位交叉核对）、出口规则（固定 IP `EXPECT_IP`，或 `EXIT_RULE` = 同一国家（默认）/ 州省 / 城市 / 不限）、Anthropic 对出口的响应、DNS 出口（解析服务器在哪个国家；在大陆不放行，别国给出提示并建议链式代理）、系统时区、浏览器实际时区与语言。没通过时环境照常能用（终端、文件、本地服务），只是不能上网，后台按 15 秒到 5 分钟退避自动重试；放行中每 5 分钟复查，明确违规立即断网，只是超时连续 3 次才断。后台可手动暂停 / 恢复外网、「以当前出口为准」。
- **地区一致**：Ubuntu 24.04，时区、浏览器语言按出口 IP 自动匹配；每个环境有自己的网卡地址、主机名、字体组合和分辨率。
- **自带浏览器**：Firefox 或 Chrome 网页桌面（noVNC），锁定走代理，关闭 WebRTC、定位、DoH、遥测。
- **管理后台**（深色控制台）：流量链路图、出口 IP、环境检查评分、**在线修改 socks5 / http 代理**（先测试再应用，新代理不通自动恢复原配置）、**浏览并切换项目目录**、Kiteline 配对码、一键重置。
- **SSH**：VS Code / Cursor Remote-SSH 直接连。
- **Kiteline**：amd64 镜像自带（无界面模式），后台显示配对二维码和 6 位验证码。

## 客户端（环境管理器）

类似指纹浏览器的客户端：环境列表（分组、搜索、批量启动 / 停止 / 删除）、新建 / 编辑 / 复制环境、代理库（批量导入，检测出口 IP、国家、延迟、机房还是住宅）、任务队列和日志、一键打开环境后台 / 浏览器桌面 / 终端里的 Claude / VS Code。

**虚拟浏览器**：每个环境都带一个独立的 Firefox，和环境同一个出口 IP、时区、语言（锁定走代理，关了 WebRTC、定位）。也可以建只要浏览器的「浏览器环境」，不需要项目目录。链接可以这样送进环境浏览器打开（环境没启动会先启动）：
- 客户端里点「打开链接」，或者每行的链接按钮；
- 命令行 `./easycc open <环境> <链接>`；
- 「设置」里把「发送到 easyCC」拖到电脑浏览器的书签栏，看到链接点一下就能选环境打开。

```bash
./easycc ui                          # 打开客户端（以应用窗口打开，没有地址栏）
./manager/macos/make-app.sh              # 生成「easyCC.app」到 ~/Applications，可拖到程序坞
```

客户端和 `easycc` 命令共用 `~/.easycc` 里的数据，两边可以混着用。客户端需要 python3（Mac 上装好 Xcode 命令行工具即可）。

## 用 easycc 命令

```bash
./easycc build                       # 默认 amd64（带 Kiteline）；--arch arm64 原生更快但没有 Kiteline
./easycc create us1 --dir ~/code/myapp --proxy 'socks5://user:pass@1.2.3.4:1080'
                                         # 项目根目录默认是 ~/code（--root 可指定），后台里可以切换其下的任意项目
./easycc start us1                   # 自检通过才算启动成功
./easycc admin us1                   # 管理后台
./easycc claude us1                  # 在容器里跑 claude（tmux 里，可由 Kiteline 接管）
./easycc code us1                    # VS Code Remote-SSH 打开 /workspace
./easycc project us1                 # 换项目：Mac 上弹出访达选择窗口；根目录外的会改挂载并重启
```

自动选择运行时的顺序：Apple container → Docker → Podman。也可以用 `EASYCC_RUNTIME=docker` 指定。
数据放在 `~/.easycc/<实例>/`：`conf/config.env` 是配置，`home/` 里是浏览器、Claude 登录和 Kiteline 配对。

## 直接用 Docker Compose

```bash
cp .env.example .env    # 填 PROXY_URL
docker compose up -d --build
docker compose logs     # 里面有管理后台令牌、VNC 密码
```

管理后台地址：`http://127.0.0.1:7681/login?k=<ADMIN_TOKEN>`。所有端口都只监听本机。

## 访问环境里的开发服务（localhost）

每个环境的浏览器访问 `localhost` 就是这个环境自己的：Claude Code 在环境里跑 `npm run dev` 监听 3000，在这个环境的浏览器里打开 `http://localhost:3000` 即可。电脑本身的 localhost 不受影响。

可选（默认关）：环境设置「高级」里打开「也把环境里的端口转到电脑的 localhost」（或配置里 `FORWARD_LOCALHOST=1`），环境里的端口会自动转到电脑上（被占用时顺延），`easycc ports <环境>` 查看。

## 项目目录

容器挂载的是**项目根目录**（`/projects`），`/workspace` 指向当前项目。根目录下的项目可以在管理后台或用 `easycc project` 随时切换，不用重启。容器能看到根目录下的所有文件，所以根目录不要设成主目录：不写 `--root` 时，如果项目的上一级是主目录，就只挂项目本身。

## 注意

- 容器必须带 `--cap-add NET_ADMIN`，否则防火墙装不上，容器会拒绝启动。
- Kiteline 官方 Linux 版只有 x86_64。在 Apple 芯片 Mac 上，amd64 镜像经 Rosetta 运行，会比原生慢一些。
- 容器不在局域网里，手机要经 Kiteline 远程连接（中继）连进来。
- 机房 IP 更容易被风控，长期使用建议用住宅 IP（管理后台的环境检查会标出来）。

## 各平台安装包

| 平台 | 运行时 | 打包 |
| --- | --- | --- |
| macOS 26（Apple 芯片） | App 自带的 Apple container | `manager/macos/build-app.sh` |
| macOS 12–25（Apple 芯片） | App 自带的小虚拟机（Alpine + Docker，macOS 13 起用 Rosetta 跑 amd64 环境） | 同上（打包时带上 `dist/vm/arm64`） |
| macOS 12–25（Intel） | 同上，x86_64 版 | `APP_ARCH=x86_64 manager/macos/build-app.sh` |
| Linux | 系统里的 Docker 或 Podman | `tools/build-linux.sh` → `easycc-linux-*.tar.gz`，解压后 `./install.sh` |
| Windows（预览） | Windows 自带的 WSL2 + easyCC 自己的发行版 | `tools/build-windows.sh` → `easycc-windows-x64-*.zip` |

- 虚拟机系统：`tools/build-vm-image.sh arm64|amd64`（在 Linux 上做，不需要 root）。虚拟机程序 `vm/host/easycc-vm.swift`，虚拟机里的启动脚本和 vsock 代理在 `vm/guest/`。命令：`easycc vm start|stop|status`。
- 环境镜像是 OCI 包；Docker / Podman / 虚拟机载入前用 `tools/oci-to-docker.py` 转成 docker load 格式。

## 环境里的 AI agent

环境默认不预装 agent。环境后台「Agent」页一键安装 Claude Code、Codex、Gemini CLI、Cursor、Copilot、opencode、Qwen Code、Kimi，安装过程实时显示。
npm 程序包由电脑本机从国内镜像下载、放进环境的 npm 缓存再安装（环境自己的流量照样全走代理）。清单在 `image/rootfs/usr/local/lib/cbox/agents.py`。

**agent 知道自己在哪**：环境说明（`image/rootfs/usr/local/share/easycc/agent-guide.md`）每次启动由 `cbox-agentenv` 装进 Claude Code 的系统级 `/etc/claude-code/CLAUDE.md`，以及 Codex / Gemini / Qwen / opencode 的全局说明文件（只替换带标记的一段）。核心意思：这里只是 agent 的轻量运行环境（内存小、项目从电脑挂载），读写代码、git 在这里做；装依赖、构建、测试、开发服务器到电脑上做（`ssh host`，技能 `easycc-host`）；外网被关时如实告诉用户，不要绕过。`easycc-env` 输出实时现状（资源、项目在电脑上的路径、能否连电脑、外网状态），Claude Code 每个会话开始时由钩子（`/etc/claude-code/managed-settings.json`）自动带上。

## 资源伸缩与休眠

- **按负载伸缩**（Docker、Podman、easyCC 虚拟机）：CPU 持续用满、内存接近上限时自动加配额，最多到设置值的 2 倍，闲下来收回。只调配额，环境里看到的核数不变。
- **空闲休眠**（所有运行时）：没有 SSH / 桌面 / 后台连接、没有 agent 在跑、CPU 也闲着，超过设定时间（默认 30 分钟）就停下环境，内存全部还给电脑，数据都在。
- **自动唤醒**：点启动、打开链接、SSH 或 VS Code 连接时唤醒（`~/.easycc/ssh_config` 里的 `Match … exec` 调 `easycc wake`）。
- Apple 的虚拟化框架不会把客户机释放的内存还给 macOS，所以 Mac 上省内存靠休眠，不靠内存气球。

## 数据与隐私

- 环境数据都在本机的 `~/.easycc/`，项目目录是从电脑挂进环境的，代码不会上传到任何地方。
- 客户端默认每 10 分钟发一次匿名使用统计：随机设备号、系统版本、CPU 和内存大小、客户端版本、环境数量和用量、出口国家、装了哪些 agent。不发环境名、备注、项目、代理地址、出口 IP 和任何登录数据。「设置 → 隐私」里可以关掉；实现在 `manager/cbox_manager.py` 的 `telemetry_report`。

## 目录

| 目录 | 内容 |
| --- | --- |
| `easycc` | 命令行（bash）：建环境、启动、停止、SSH、端口转发等 |
| `manager/` | 客户端：本机管理服务（Python 标准库）+ 网页界面；`macos/` 是原生外壳，`linux/` 是 Linux 的启动器 |
| `image/` | 环境镜像：Dockerfile 和镜像里的脚本（防火墙、联网检查、环境后台、agent 安装） |
| `vm/` | macOS 12–25 用的小虚拟机（宿主程序和虚拟机里的启动脚本） |
| `tools/` | 打包和构建工具 |
| `site/` | 官网 |

## 参与

问题和建议请提 [Issue](https://github.com/hon6/easycc/issues)，也欢迎直接发 Pull Request。

## 许可

[MIT](LICENSE)
