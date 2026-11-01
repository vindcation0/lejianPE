"""
命令行登录。

原理
----
登录请求体是信封 {"t": 毫秒时间戳, "pyd": AES(明文)}，密钥由 t 派生。
服务端会校验 t 的新鲜度，所以必须每次用**当前时间**重新封装。

而明文里的 signDigital = hs(账号+密码+entrance)，**不含时间**，是个常量。
因此只需从一次真实抓包里把它提取出来存好，之后每次登录：

    读账号+签名 → 向用户要密码 → 用当前 t 封装 → 提交 → 换到新 token

不需要模拟器，也不需要逆向 hs() 的算法。

setup（只做一次）
    python tools\\login_cli.py --setup captures/login_flows.jsonl

日常登录
    python tools\\login_cli.py

改动密码后需要重新 setup 一次。
"""
import argparse
import getpass
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from login_envelope import unwrap, wrap  # noqa: E402

BASE = "https://cpes.legym.cn/"
LOGIN = "authorization/user/v2/manage/login"
STATE = os.path.join(ROOT, "state.json")
CRED = os.path.join(ROOT, "credential.json")
CTX = ssl.create_default_context()


def dec(raw):
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def mask(s):
    s = str(s)
    return f"{s[:3]}****{s[-2:]}" if len(s) >= 6 else "***"


# ---------------------------------------------------------------- setup
def do_setup(flows_path):
    if not os.path.exists(flows_path):
        sys.exit(f"找不到 {flows_path}")
    payload = None
    for line in open(flows_path, encoding="utf-8"):
        if LOGIN not in line:
            continue
        rec = json.loads(line)
        if rec.get("status") != 200:
            continue
        try:
            payload = unwrap(json.loads(rec["req_body"]))
        except Exception:
            continue
    if not payload:
        sys.exit("抓包里没有成功的登录请求")

    cred = {
        "account": payload.get("userName") or payload.get("phone"),
        "signDigital": payload.get("signDigital"),
        "entrance": str(payload.get("entrance") or "1"),
        "capturedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not cred["account"] or not cred["signDigital"]:
        sys.exit("载荷里缺账号或签名")
    with open(CRED, "w", encoding="utf-8") as f:
        json.dump(cred, f, ensure_ascii=False, indent=2)
    print("已生成 credential.json（不含密码）")
    print(f"  账号     : {mask(cred['account'])}")
    print(f"  签名前缀 : {cred['signDigital'][:10]}…  长度={len(cred['signDigital'])}")
    print(f"  采集时间 : {cred['capturedAt']}")
    print("\n注意：密码一旦修改，签名即失效，需要重新 setup。")


# ---------------------------------------------------------------- 登录
def post_json(path, obj):
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "okhttp/3.12.1")
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            return r.status, dec(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dec(e.read())
    except Exception as e:
        return -1, repr(e)


def api_get(path, token, school):
    req = urllib.request.Request(BASE + path, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Organization", school or "")
    req.add_header("User-Agent", "okhttp/3.12.1")
    try:
        with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
            return json.loads(dec(r.read()))
    except Exception:
        return {}


def do_login(account, password, sign, entrance):
    payload = {
        "userName": account, "phone": account, "password": password,
        "entrance": entrance, "authType": "Bearer", "baseUrl": BASE,
        "page": "", "type": "", "signDigital": sign,
    }
    code, text = post_json(LOGIN, wrap(payload))
    if code != 200:
        return None, f"HTTP {code}: {text[:160]}"
    try:
        obj = json.loads(text)
    except Exception:
        return None, "响应不是 JSON"
    if obj.get("code") != 0:
        return None, f"code={obj.get('code')} {obj.get('message')}"
    d = obj.get("data") or {}
    if isinstance(d, dict) and "pyd" in d:
        d = unwrap(d)
    return d, None


def save_state(d):
    sem = api_get("education/semester/getCurrent", d["accessToken"],
                  d.get("schoolId") or "")
    sem_d = sem.get("data") or {}
    yr = str(sem_d.get("yearRange") or "")
    old = {}
    if os.path.exists(STATE):
        old = json.load(open(STATE, encoding="utf-8"))
    st = dict(old)
    st.update({
        "accessToken": d["accessToken"],
        "refreshToken": d.get("refreshToken"),
        "accessTokenExpires": d.get("accessTokenExpires"),
        "userId": d.get("id"),
        "schoolId": d.get("schoolId"),
        "semesterId": sem_d.get("id") or d.get("semesterId"),
        "year": yr[:4] if yr else str(d.get("year") or "2026"),
        "campusId": d.get("campusId"),
        "realName": d.get("realName"),
    })
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE)
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", metavar="FLOWS",
                    help="从抓包文件提取账号与签名，生成 credential.json")
    ap.add_argument("--no-save", action="store_true", help="只验证，不写 state.json")
    args = ap.parse_args()

    if args.setup:
        do_setup(args.setup)
        return

    if not os.path.exists(CRED):
        sys.exit("没有 credential.json。请先运行：\n"
                 "  python tools\\login_cli.py --setup captures\\login_flows.jsonl")

    cred = json.load(open(CRED, encoding="utf-8"))
    account = cred["account"]
    print(f"账号：{mask(account)}（采集于 {cred.get('capturedAt')}）")
    password = getpass.getpass("密码（不回显）：")
    if not password:
        sys.exit("已取消")

    print("登录中…")
    data, err = do_login(account, password, cred["signDigital"], cred["entrance"])
    if not data:
        print(f"\n❌ 登录失败：{err}")
        e = str(err)
        if "50004" in e or "签名" in e:
            print("  服务端判定签名不正确。")
            print("  签名由「账号 + 密码 + entrance」算出，密码一变就失效。")
            print("  请重新抓取签名：")
            print("      python tools\\capture_login.py")
            print("  或菜单 [b]")
        elif "密码" in e or "账号" in e:
            print("  请确认账号密码是否正确。")
        return

    print("\n✅ 登录成功")
    print(f"  姓名      : {data.get('realName')}")
    print(f"  schoolId  : {data.get('schoolId')}")
    print(f"  token前缀 : {str(data.get('accessToken'))[:8]}…")
    exp = data.get("accessTokenExpires")
    if exp:
        print(f"  有效期至  : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exp/1000))}")

    if not args.no_save:
        st = save_state(data)
        print(f"  已写入 state.json（semesterId={st.get('semesterId')}）")


if __name__ == "__main__":
    main()
