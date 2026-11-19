---
name: easycc-host
description: 在 easyCC 环境里，需要安装依赖、构建、跑测试、启动开发服务器、使用项目的语言运行时或平台工具链（npm/pip/cargo/go/Xcode/Docker 等）时使用：通过 ssh host 到用户电脑上的项目目录执行，而不是在资源很少的环境容器里执行。
---

# 在用户电脑上执行项目命令

easyCC 环境只是 agent 的运行环境：内存小，项目目录从电脑挂载进来。项目的依赖、构建、测试、服务都在用户电脑上跑。

## 1. 先看现状

```bash
easycc-env
```

重点看这几行：

- **电脑上的项目路径**：`/workspace` 对应电脑上的哪个目录。
- **SSH 控制本机**：是否已授权。
- **到电脑执行**：给出的命令格式（已经带上了电脑的登录 shell，brew、nvm 等装的工具才能找到）。

## 2. 在电脑上执行

推荐写法：把命令经标准输入交给电脑的**登录 shell**（macOS 是 `zsh -l`，Linux 是 `bash -l`）。命令原样写，不用为引号转义，也找得到 brew、nvm 装的工具：

```bash
ssh host 'zsh -l' <<'EOF'
cd "/Users/alice/code/my-app" || exit 1
npm install
npm test 2>&1 | tail -50
EOF
```

- 先确认电脑上有需要的运行时：在上面的写法里执行 `node -v; python3 --version; brew --version`。
- 电脑上缺运行时或工具时，先问用户再装，并说明要装什么、用什么方式装（例如 Homebrew）。
- 输出很长的命令，加 `2>&1 | tail -50`，或者重定向到电脑上的日志文件再按需查看。
- 路径换算：环境里 `/workspace/src/a.ts`，在电脑上就是「电脑上的项目路径/src/a.ts」。改文件仍然在环境里直接改 `/workspace`（同一份文件），只把**执行**放到电脑上。

## 3. 长时间运行的服务（开发服务器、监听进程）

```bash
ssh host 'zsh -l' <<'EOF'
cd "<电脑上的项目路径>" || exit 1
nohup npm run dev > /tmp/easycc-dev.log 2>&1 &
echo $!
EOF
```

- 记下打印出的进程号，用完后 `ssh host 'kill <进程号>'` 停掉。
- 先看一眼日志确认起来了：`ssh host 'tail -20 /tmp/easycc-dev.log'`。报「Address already in use」（端口被占用）就换一个端口，电脑上可能有别的程序在用。
- 在环境里访问它：`curl http://host.internal:<端口>`；需要看页面时用 `cbox-browser http://host.internal:<端口>`，同时告诉用户也可以直接在电脑浏览器打开 `http://localhost:<端口>`。
- 查看日志：`ssh host 'tail -50 /tmp/easycc-dev.log'`。

## 4. SSH 没授权、连不上时

不要退回到在环境里装依赖、跑构建。先告诉用户怎么授权：

1. 打开 easyCC 环境后台，在「总览 → 访问本机」点「一键授权访问本机」，再打开「允许 SSH 控制本机」。
2. Mac 还要在「系统设置 → 通用 → 共享」里打开「远程登录」。

用户暂时不想授权时，把要执行的命令按顺序整理好（写上在哪个目录执行），请用户在电脑终端里执行，再把输出贴回来。

## 注意

- 电脑是用户的主力机器：只做当前任务需要的事。删除文件、改系统设置、全局安装、动别的项目之前，先问用户。
- 不要把凭据、密钥从电脑复制到环境里，也不要从环境复制到电脑上。
