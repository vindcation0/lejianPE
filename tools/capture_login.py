"""
一键重抓登录签名。

做的事情（全自动，除了最后你在 App 里登录那一下）：
  1. 连接模拟器并提权
  2. 注入 mitmproxy CA 到两个系统证书库
  3. 重启 zygote 让证书挂载对新进程生效
  4. 配置 adb reverse + 系统代理
  5. 启动抓包
  6. 清空乐健 App 数据，回到登录页
  7. 等你在 App 里用新密码登录
  8. 抓到登录请求后自动提取签名 → 写入 credential.json

用法：
    python tools\\capture_login.py
"""
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTURES = os.path.join(ROOT, "captures")
FLOWS = os.path.join(CAPTURES, "login_flows.jsonl")
ADDON = os.path.join(ROOT, "tools", "capture_login_addon.py")
PKG = "com.ledreamer.zz"

ADB_CANDIDATES = [
    r"D:\MuMuPlayer\nx_main\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\nx_main\adb.exe",
    r"C:\LDPlayer\LDPlayer9\adb.exe",
]
PORTS = ["16384", "5555", "7555"]
LOGIN_PATH = "authorization/user/v2/manage/login"


def find_adb():
    for p in ADB_CANDIDATES:
        if os.path.exists(p):
            return p
    from shutil import which
    return which("adb")


def sh(args, timeout=60):
    # 子进程输出统一按 UTF-8 解，避免中文 Windows 下按 GBK 解码变乱码
    return subprocess.run(args, capture_output=True, encoding="utf-8",
                          errors="ignore", timeout=timeout)


def banner(msg):
    print()
    print("=" * 60)
    print(msg)
    print("=" * 60, flush=True)


class Device:
    def __init__(self, adb):
        self.adb = adb
        self.serial = None

    def run(self, *args, timeout=60):
        return sh([self.adb, "-s", self.serial] + list(args), timeout)

    def shell(self, cmd, timeout=60):
        return self.run("shell", cmd, timeout=timeout)

    def connect(self):
        for p in PORTS:
            sh([self.adb, "connect", f"127.0.0.1:{p}"])
        out = sh([self.adb, "devices"]).stdout
        serials = [l.split("\t")[0] for l in out.splitlines() if "\tdevice" in l]
        if not serials:
            return False
        self.serial = serials[0]
        return True

    def root(self, retries=6):
        for _ in range(retries):
            self.run("root")
            time.sleep(2.5)
            sh([self.adb, "wait-for-device"])
            if not self.connect():
                time.sleep(2)
                continue
            out = self.shell("id").stdout
            if "uid=0" in out:
                return True
            time.sleep(2)
        return False


def main():
    adb = find_adb()
    if not adb:
        sys.exit("找不到 adb，请确认模拟器已安装")

    banner("步骤 1/6  连接模拟器并提权")
    dev = Device(adb)
    if not dev.connect():
        sys.exit("连不上模拟器。请确认 MuMu 已启动。")
    print("  设备:", dev.serial)
    if not dev.root():
        sys.exit("提权失败。")
    print("  root 成功")

    banner("步骤 2/6  注入证书")
    pem = os.path.expanduser(r"~\.mitmproxy\mitmproxy-ca-cert.pem")
    if not os.path.exists(pem):
        sys.exit("没有 mitmproxy CA，请先运行一次 mitmdump 生成")
    dev.run("push", pem, "/data/local/tmp/mitm.pem")
    dev.shell("rm -rf /data/local/tmp/cacerts; mkdir -p /data/local/tmp/cacerts")
    dev.shell("cp /apex/com.android.conscrypt/cacerts/* /data/local/tmp/cacerts/ "
              "2>/dev/null; cp /data/local/tmp/mitm.pem "
              "/data/local/tmp/cacerts/c8750f0d.0; chmod 644 /data/local/tmp/cacerts/*")
    dev.shell("mount -t tmpfs tmpfs /apex/com.android.conscrypt/cacerts && "
              "cp /data/local/tmp/cacerts/* /apex/com.android.conscrypt/cacerts/ 2>/dev/null")
    dev.shell("mount -t tmpfs tmpfs /system/etc/security/cacerts && "
              "cp /data/local/tmp/cacerts/* /system/etc/security/cacerts/ 2>/dev/null")
    n = dev.shell("ls /apex/com.android.conscrypt/cacerts | wc -l").stdout.strip()
    print(f"  证书数量: {n}")

    banner("步骤 3/6  重启 zygote 使挂载生效")
    dev.shell("setprop ctl.restart zygote")
    time.sleep(15)
    if not dev.connect():
        time.sleep(3)
        dev.connect()
    if not dev.root():
        sys.exit("zygote 重启后重新提权失败")
    mounts = dev.shell("mount | grep -c cacerts").stdout.strip()
    print(f"  生效挂载数: {mounts}")

    banner("步骤 4/6  启动抓包")
    os.makedirs(CAPTURES, exist_ok=True)
    if os.path.exists(FLOWS):
        os.remove(FLOWS)
    mitm = subprocess.Popen(
        ["mitmdump", "--listen-port", "8081", "-s", ADDON,
         "--set", "console_eventlog_verbosity=warn"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(4)
    print("  抓包进程 PID:", mitm.pid)
    dev.run("reverse", "tcp:8081", "tcp:8081")
    dev.shell("settings put global http_proxy 127.0.0.1:8081")
    print("  代理:", dev.shell("settings get global http_proxy").stdout.strip())

    banner("步骤 5/6  清空 App 数据，回到登录页")
    dev.shell(f"am force-stop {PKG}")
    print("  清空中…")
    dev.shell(f"pm clear {PKG}")
    time.sleep(2)
    dev.shell(f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1")
    print("  已启动 App，请等待登录页出现")

    banner("请你操作：在模拟器里登录")
    print("  1. 勾选《用户服务协议》与《隐私政策》")
    print("  2. 输入新密码")
    print("  3. 点击登录")
    print()
    print("  正在等待登录请求…（最多 10 分钟）")

    deadline = time.time() + 600
    got = False
    while time.time() < deadline:
        time.sleep(2)
        if not os.path.exists(FLOWS):
            continue
        try:
            for line in open(FLOWS, encoding="utf-8"):
                if LOGIN_PATH in line:
                    rec = json.loads(line)
                    if rec.get("status") == 200:
                        got = True
                        break
        except Exception:
            continue
        if got:
            break
        if int(time.time()) % 30 < 3:
            print(f"    等待中… 已抓 {sum(1 for _ in open(FLOWS, encoding='utf-8'))} 条", flush=True)

    mitm.terminate()
    try:
        dev.shell("settings put global http_proxy :0")
        dev.run("reverse", "--remove-all")
    except Exception:
        pass

    if not got:
        sys.exit("\n没抓到登录请求，未修改 credential.json。")

    banner("步骤 6/6  提取签名")
    r = sh([sys.executable, os.path.join(ROOT, "tools", "login_cli.py"),
            "--setup", FLOWS])
    print(r.stdout)
    if r.returncode != 0:
        print(r.stderr)
        sys.exit("提取失败")

    print("完成。现在可以用菜单 [9] 或 python tools\\login_cli.py 登录了。")


if __name__ == "__main__":
    main()
