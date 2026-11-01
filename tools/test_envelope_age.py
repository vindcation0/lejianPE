"""测试登录信封的时效性：用更早的 t 重新封装同一份载荷，看服务端是否接受。

载荷在内存中处理，全程不打印密码。
"""
import json, sys, ssl, time, urllib.request, urllib.error
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\tools")
from login_envelope import unwrap, wrap

BASE = "https://cpes.legym.cn/"
P = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\login_flows.jsonl"

rec = next(json.loads(l) for l in open(P, encoding="utf-8") if "manage/login" in l)
payload = unwrap(json.loads(rec["req_body"]))   # 仅在内存中使用

NOW_MS = int(time.time() * 1000)
AGES = [
    ("刚刚", 0),
    ("1 小时前", 3600),
    ("1 天前", 86400),
    ("7 天前", 7 * 86400),
    ("30 天前", 30 * 86400),
]


def attempt(env):
    req = urllib.request.Request(BASE + "authorization/user/v2/manage/login",
                                 data=json.dumps(env).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "okhttp/3.12.1")
    try:
        with urllib.request.urlopen(req, timeout=20,
                                    context=ssl.create_default_context()) as r:
            obj = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        try:
            obj = json.loads(e.read().decode("utf-8", "ignore"))
        except Exception:
            return f"HTTP {e.code}", None
    except Exception as e:
        return repr(e)[:40], None
    if obj.get("code") == 0 and isinstance(obj.get("data"), dict) \
            and "pyd" in obj["data"]:
        return "成功", unwrap(obj["data"])
    return f"code={obj.get('code')} {obj.get('message')}", None


print(f"{'信封年龄':<10} {'结果'}")
print("-" * 60)
for label, age in AGES:
    t = NOW_MS - age * 1000
    env = wrap(payload, t)
    res, data = attempt(env)
    extra = ""
    if data:
        exp = data.get("accessTokenExpires", 0)
        extra = "  token有效期至 " + time.strftime("%m-%d %H:%M",
                                                time.localtime(exp / 1000))
    print(f"{label:<10} {res}{extra}")
    time.sleep(1.5)
