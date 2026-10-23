"""从模拟器里的 App 重新同步 token 到 state.json（用于 refreshToken 也失效时）。"""
import os
import subprocess
import sys
import sqlite3
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "captures", "DCStorage")
STATE = os.path.join(ROOT, "state.json")
PKG = "com.ledreamer.zz"

ADB_CANDIDATES = [
    r"D:\MuMuPlayer\nx_main\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\nx_main\adb.exe",
    r"C:\LDPlayer\LDPlayer9\adb.exe",
]
PORTS = ["16384", "7555", "5555"]


def find_adb():
    for p in ADB_CANDIDATES:
        if os.path.exists(p):
            return p
    from shutil import which
    return which("adb")


def run(args):
    return subprocess.run(args, capture_output=True, text=True, errors="ignore")


def main():
    adb = find_adb()
    if not adb:
        sys.exit("找不到 adb。请确认模拟器已安装，或把 adb 加入 PATH。")
    print(f"adb: {adb}")

    for port in PORTS:
        run([adb, "connect", f"127.0.0.1:{port}"])

    out = run([adb, "devices"]).stdout
    serials = [l.split("\t")[0] for l in out.splitlines()
               if "\tdevice" in l and l.strip()]
    if not serials:
        sys.exit("没有连上设备。请先启动模拟器并打开乐健体育。")
    serial = serials[0]
    print(f"设备: {serial}")

    print("尝试提权…")
    run([adb, "-s", serial, "root"])
    run([adb, "-s", serial, "wait-for-device"])

    os.makedirs(os.path.dirname(DB), exist_ok=True)
    r = run([adb, "-s", serial, "pull",
             f"/data/data/{PKG}/databases/DCStorage", DB])
    if "1 file pulled" not in (r.stdout or "") and not os.path.exists(DB):
        sys.exit(f"拉取失败：{r.stdout} {r.stderr}")

    con = sqlite3.connect(DB)
    user = None
    for (v,) in con.execute("SELECT value FROM DC_18286065_storage WHERE key='app-store'"):
        user = json.loads(v)["app"]["userInfo"]
        break
    if not user:
        sys.exit("数据库里没有 app-store，请确认已在 App 里登录。")

    import datetime
    st_path = os.path.join(ROOT, "state.json")
    old = {}
    if os.path.exists(st_path):
        old = json.load(open(st_path, encoding="utf-8"))

    year_range = (user.get("yearRange") or "")
    state = {
        "accessToken": user["accessToken"],
        "refreshToken": user["refreshToken"],
        "accessTokenExpires": user["accessTokenExpires"],
        "userId": user["id"],
        "schoolId": user["schoolId"],
        "semesterId": user.get("semesterId"),
        "year": (year_range[:4] if year_range else "2026"),
        "mobile": user.get("mobile"),
        "realName": user.get("realName"),
        "organizationUserNumber": user.get("organizationUserNumber"),
        "campusId": user.get("campusId"),
    }
    with open(st_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    exp = datetime.datetime.fromtimestamp(state["accessTokenExpires"] / 1000)
    print(f"\n已更新 state.json")
    print(f"  schoolId={state['schoolId']}")
    print(f"  token 有效期至 {exp:%Y-%m-%d %H:%M:%S}")


if __name__ == "__main__":
    main()
