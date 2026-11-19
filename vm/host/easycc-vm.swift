// easyCC 虚拟机（macOS 12–25 上代替 Apple container）：用苹果虚拟化框架跑一台 Alpine + Docker 的小虚拟机。
//
//   easycc-vm run   --dir <状态目录> --kernel <内核> --initrd <启动盘> --rootfs <系统盘.img.gz> [--cpus N] [--memory MB] [--disk GB]
//   easycc-vm stop  --dir <状态目录>
//
// 状态目录里：disk.img（系统盘，首次从 rootfs.img.gz 展开成稀疏文件）、docker.sock（Mac 上的 docker 命令连它）、
// console.log（虚拟机串口输出）、vm.pid、ports（每行一个端口：Mac 本机 127.0.0.1:端口 → 虚拟机 127.0.0.1:端口）、guest.json（虚拟机 IP）。
// Mac 的 /Users 经 virtiofs 共享进虚拟机同一路径，所以 Docker 挂载项目目录的路径和 Mac 上一样。
// 和虚拟机通信都走 vsock（只有本机能连）：虚拟机里的 easycc-vsock-agent 收到 "DOCKER" / "TCP 端口" / "POWEROFF"。
import Foundation
import Virtualization

setvbuf(stdout, nil, _IOLBF, 0)
func log(_ s: String) { print("[easycc-vm \(ISO8601DateFormatter().string(from: Date()))] \(s)") }
func die(_ s: String) -> Never { log("错误：" + s); exit(1) }

var args: [String: String] = [:]
let argv = CommandLine.arguments
guard argv.count >= 2 else { die("用法：easycc-vm run|stop --dir 状态目录 ...") }
let cmd = argv[1]
var i = 2
while i < argv.count { if argv[i].hasPrefix("--"), i + 1 < argv.count { args[String(argv[i].dropFirst(2))] = argv[i + 1]; i += 2 } else { i += 1 } }
let dir: String = args["dir"] ?? ""
if dir.isEmpty { die("缺少 --dir") }
let fm = FileManager.default
try? fm.createDirectory(atPath: dir, withIntermediateDirectories: true)
let pidFile = dir + "/vm.pid"

// ---------- stop：让运行中的进程优雅关机 ----------
if cmd == "stop" {
    guard let s = try? String(contentsOfFile: pidFile, encoding: .utf8), let pid = Int32(s.trimmingCharacters(in: .whitespacesAndNewlines)), kill(pid, 0) == 0 else {
        log("虚拟机没在运行"); exit(0)
    }
    kill(pid, SIGTERM)
    for _ in 0..<900 { if kill(pid, 0) != 0 { log("已关机"); exit(0) }; usleep(100_000) }
    kill(pid, SIGKILL); log("关机超时，已强制结束"); exit(0)
}
guard cmd == "run" else { die("未知命令 \(cmd)") }

// 脱离启动它的程序（easyCC 客户端退出时虚拟机和环境照常在后台跑，和 Apple container 一样）
setsid()
signal(SIGHUP, SIG_IGN)

// 同一个状态目录只跑一台
if let s = try? String(contentsOfFile: pidFile, encoding: .utf8), let pid = Int32(s.trimmingCharacters(in: .whitespacesAndNewlines)), pid != getpid(), kill(pid, 0) == 0 {
    die("已经在运行（pid \(pid)）")
}
try? "\(getpid())".write(toFile: pidFile, atomically: true, encoding: .utf8)

guard let kernel = args["kernel"], let initrd = args["initrd"], let rootfsGz = args["rootfs"] else { die("缺少 --kernel / --initrd / --rootfs") }
let cpus = max(2, min(Int(args["cpus"] ?? "") ?? max(2, ProcessInfo.processInfo.processorCount / 2), VZVirtualMachineConfiguration.maximumAllowedCPUCount))
let memMB = UInt64(args["memory"] ?? "") ?? UInt64(min(8192, max(2048, Int(ProcessInfo.processInfo.physicalMemory >> 20) / 3)))
let diskGB = UInt64(args["disk"] ?? "") ?? 64

// ---------- 磁盘：系统盘（disk.img，从 rootfs.img.gz 展开，App 升级了虚拟机系统就整个换新）+ 数据盘（data.img，稀疏大文件，Docker 数据都在这） ----------
let disk = dir + "/disk.img"
let dataDisk = dir + "/data.img"
let stamp = dir + "/disk.version"
let wantVer = (try? String(contentsOfFile: (rootfsGz as NSString).deletingLastPathComponent + "/VERSION", encoding: .utf8)) ?? ""
let haveVer = (try? String(contentsOfFile: stamp, encoding: .utf8)) ?? ""
if fm.fileExists(atPath: disk) && !wantVer.isEmpty && haveVer != wantVer {
    log("虚拟机系统有新版本：换新系统盘（Docker 数据在数据盘上，不受影响）")
    try? fm.removeItem(atPath: disk)
}
if !fm.fileExists(atPath: disk) {
    log("首次启动：展开系统盘")
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/sh")
    p.arguments = ["-c", "/usr/bin/gunzip -c \"$0\" > \"$1.tmp\" && mv \"$1.tmp\" \"$1\"", rootfsGz, disk]
    try? p.run(); p.waitUntilExit()
    if p.terminationStatus != 0 { die("展开系统盘失败") }
    try? wantVer.write(toFile: stamp, atomically: true, encoding: .utf8)
}
if !fm.fileExists(atPath: dataDisk) { fm.createFile(atPath: dataDisk, contents: nil) }
if let h = FileHandle(forUpdatingAtPath: dataDisk) {
    let size = h.seekToEndOfFile()
    let want = diskGB << 30
    if size < want { h.truncateFile(atOffset: want) }   // 稀疏文件：用多少占多少
    h.closeFile()
}

// ---------- 虚拟机配置 ----------
let boot = VZLinuxBootLoader(kernelURL: URL(fileURLWithPath: kernel))
boot.initialRamdiskURL = URL(fileURLWithPath: initrd)
boot.commandLine = "console=hvc0 root=/dev/vda rw quiet easycc.share=\(dir)"
let cfg = VZVirtualMachineConfiguration()
cfg.bootLoader = boot
cfg.cpuCount = cpus
cfg.memorySize = memMB << 20

do {
    let att = try VZDiskImageStorageDeviceAttachment(url: URL(fileURLWithPath: disk), readOnly: false)
    let datt = try VZDiskImageStorageDeviceAttachment(url: URL(fileURLWithPath: dataDisk), readOnly: false)
    cfg.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: att), VZVirtioBlockDeviceConfiguration(attachment: datt)]
} catch { die("系统盘打不开：\(error.localizedDescription)") }

let net = VZVirtioNetworkDeviceConfiguration()
net.attachment = VZNATNetworkDeviceAttachment()
let macFile = dir + "/mac"
if let s = try? String(contentsOfFile: macFile, encoding: .utf8), let m = VZMACAddress(string: s.trimmingCharacters(in: .whitespacesAndNewlines)) {
    net.macAddress = m
} else {
    let m = VZMACAddress.randomLocallyAdministered()
    try? m.string.write(toFile: macFile, atomically: true, encoding: .utf8)
    net.macAddress = m
}
cfg.networkDevices = [net]

let share = VZVirtioFileSystemDeviceConfiguration(tag: "users")
share.share = VZSingleDirectoryShare(directory: VZSharedDirectory(url: URL(fileURLWithPath: "/Users"), readOnly: false))
var shares: [VZDirectorySharingDeviceConfiguration] = [share]
// macOS 13+ 的 Apple 芯片：Rosetta 共享给虚拟机，amd64 的环境镜像（含 Kiteline）也能跑
#if arch(arm64)
if #available(macOS 13.0, *) {
    if VZLinuxRosettaDirectoryShare.availability == .installed, let r = try? VZLinuxRosettaDirectoryShare() {
        let rs = VZVirtioFileSystemDeviceConfiguration(tag: "rosetta")
        rs.share = r
        shares.append(rs)
        log("已启用 Rosetta")
    } else {
        log("Rosetta 没装（softwareupdate --install-rosetta），只能跑 arm64 镜像")
    }
}
#endif
cfg.directorySharingDevices = shares

let console = VZVirtioConsoleDeviceSerialPortConfiguration()
fm.createFile(atPath: dir + "/console.log", contents: nil)
console.attachment = VZFileHandleSerialPortAttachment(fileHandleForReading: nil, fileHandleForWriting: FileHandle(forWritingAtPath: dir + "/console.log"))
cfg.serialPorts = [console]
cfg.socketDevices = [VZVirtioSocketDeviceConfiguration()]
cfg.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]
cfg.memoryBalloonDevices = [VZVirtioTraditionalMemoryBalloonDeviceConfiguration()]

do { try cfg.validate() } catch { die("配置不对：\(error.localizedDescription)") }

let vm = VZVirtualMachine(configuration: cfg)   // 用主队列
final class Delegate: NSObject, VZVirtualMachineDelegate {
    func guestDidStop(_ vm: VZVirtualMachine) { log("虚拟机已关机"); cleanup(); exit(0) }
    func virtualMachine(_ vm: VZVirtualMachine, didStopWithError error: Error) { log("虚拟机出错停止：\(error.localizedDescription)"); cleanup(); exit(1) }
}
let delegate = Delegate()
vm.delegate = delegate

func cleanup() {
    unlink(dir + "/docker.sock"); unlink(pidFile)
    try? fm.removeItem(atPath: dir + "/guest.json")
}

// ---------- 字节搬运 ----------
func pump(_ from: Int32, _ to: Int32, done: @escaping () -> Void) {
    Thread.detachNewThread {
        var buf = [UInt8](repeating: 0, count: 65536)
        while true {
            let n = read(from, &buf, buf.count)
            if n <= 0 { break }
            var off = 0
            while off < n { let w = buf.withUnsafeBytes { write(to, $0.baseAddress! + off, n - off) }; if w <= 0 { off = -1; break }; off += w }
            if off < 0 { break }
        }
        shutdown(to, SHUT_WR)
        done()
    }
}

var live: [ObjectIdentifier: VZVirtioSocketConnection] = [:]
let liveLock = NSLock()

/// 把一个已接受的本机连接接到虚拟机 vsock 1024，先发一行命令
func bridge(_ client: Int32, _ header: String) {
    DispatchQueue.main.async {
        guard let dev = vm.socketDevices.first as? VZVirtioSocketDevice, vm.state == .running else { close(client); return }
        dev.connect(toPort: 1024) { result in
            switch result {
            case .failure: close(client)
            case .success(let conn):
                let vfd = conn.fileDescriptor
                _ = header.withCString { write(vfd, $0, strlen($0)) }
                liveLock.lock(); live[ObjectIdentifier(conn)] = conn; liveLock.unlock()
                var left = 2
                let lock = NSLock()
                let fin = {
                    lock.lock(); left -= 1; let last = left == 0; lock.unlock()
                    if last { close(client); conn.close(); liveLock.lock(); live.removeValue(forKey: ObjectIdentifier(conn)); liveLock.unlock() }
                }
                pump(client, vfd, done: fin)
                pump(vfd, client, done: fin)
            }
        }
    }
}

func listenUnix(_ path: String) -> Int32 {
    unlink(path)
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let bytes = Array(path.utf8.prefix(103))
    withUnsafeMutableBytes(of: &addr.sun_path) { p in for (k, b) in bytes.enumerated() { p[k] = b }; p[bytes.count] = 0 }
    let ok = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) } }
    if ok != 0 { die("监听 \(path) 失败") }
    chmod(path, 0o600)
    listen(fd, 128)
    return fd
}

func listenTCP(_ port: UInt16) -> Int32? {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    var on: Int32 = 1
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &on, socklen_t(MemoryLayout<Int32>.size))
    var a = sockaddr_in()
    a.sin_family = sa_family_t(AF_INET); a.sin_port = port.bigEndian; a.sin_addr.s_addr = inet_addr("127.0.0.1")
    let ok = withUnsafePointer(to: &a) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) } }
    if ok != 0 { close(fd); return nil }
    listen(fd, 128)
    return fd
}

func acceptLoop(_ fd: Int32, header: String) {
    Thread.detachNewThread {
        while true {
            let c = accept(fd, nil, nil)
            if c < 0 { if errno == EINTR { continue }; break }
            var on: Int32 = 1
            setsockopt(c, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
            bridge(c, header)
        }
    }
}

// 端口转发：ports 文件里列的端口，Mac 本机 127.0.0.1:端口 → 虚拟机 127.0.0.1:端口（Docker 在虚拟机里发布到 127.0.0.1）
var tcpListeners: [UInt16: Int32] = [:]
var busyLogged = Set<UInt16>()
func syncPorts() {
    let want = Set(((try? String(contentsOfFile: dir + "/ports", encoding: .utf8)) ?? "")
        .split(whereSeparator: { $0 == "\n" || $0 == "," || $0 == " " }).compactMap { UInt16($0) })
    for (p, fd) in tcpListeners where !want.contains(p) { close(fd); tcpListeners.removeValue(forKey: p) }
    for p in want where tcpListeners[p] == nil {
        if let fd = listenTCP(p) { tcpListeners[p] = fd; busyLogged.remove(p); acceptLoop(fd, header: "TCP \(p)\n") }
        else if !busyLogged.contains(p) { busyLogged.insert(p); log("端口 \(p) 被占用（别的程序在用，过会儿再试）") }
    }
}

// ---------- 启动 ----------
signal(SIGPIPE, SIG_IGN)
let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
signal(SIGTERM, SIG_IGN)
term.setEventHandler {
    log("收到关机请求")
    // 先让虚拟机里停容器、正常关机；60 秒没关就强制停
    if let dev = vm.socketDevices.first as? VZVirtioSocketDevice, vm.state == .running {
        dev.connect(toPort: 1024) { r in
            if case .success(let conn) = r {
                _ = "POWEROFF\n".withCString { write(conn.fileDescriptor, $0, strlen($0)) }
                liveLock.lock(); live[ObjectIdentifier(conn)] = conn; liveLock.unlock()   // 连接留到关机完成，提前释放会把命令截断
            }
        }
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + 60) {
        log("正常关机超时，强制停止")
        vm.stop { _ in cleanup(); exit(0) }
    }
}
term.resume()
let intr = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
signal(SIGINT, SIG_IGN)
intr.setEventHandler { term.cancel(); kill(getpid(), SIGTERM) }
intr.resume()

log("启动虚拟机：\(cpus) 核 / \(memMB) MB / 磁盘 \(diskGB) GB")
vm.start { result in
    switch result {
    case .failure(let e): die("虚拟机启动失败：\(e.localizedDescription)")
    case .success:
        log("虚拟机已启动")
        acceptLoop(listenUnix(dir + "/docker.sock"), header: "DOCKER\n")
        syncPorts()
        Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in syncPorts() }
    }
}
RunLoop.main.run()
