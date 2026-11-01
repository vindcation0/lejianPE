"""重放抓到的登录信封，验证服务端是否接受旧信封（不看密码）。"""
import json, sys, ssl, urllib.request, urllib.error, time
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\tools")
from login_envelope import unwrap

BASE = "https://cpes.legym.cn/"
P = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\login_flows.jsonl"

rec = next(json.loads(l) for l in open(P, encoding="utf-8") if "manage/login" in l)
body = json.loads(rec["req_body"])
print("原始信封 t =", body["t"], " 距今",
      round((time.time() - body["t"] / 1000) / 60, 1), "分钟")

req = urllib.request.Request(BASE + "authorization/user/v2/manage/login",
                             data=json.dumps(body).encode(), method="POST")
req.add_header("Content-Type", "application/json")
req.add_header("User-Agent", "okhttp/3.12.1")

try:
    with urllib.request.urlopen(req, timeout=20,
                                context=ssl.create_default_context()) as r:
        text = r.read().decode("utf-8", "ignore")
        status = r.status
except urllib.error.HTTPError as e:
    text = e.read().decode("utf-8", "ignore")
    status = e.code

print("HTTP", status)
try:
    obj = json.loads(text)
    print("code =", obj.get("code"), " message =", obj.get("message"))
    d = obj.get("data")
    if isinstance(d, dict) and "pyd" in d:
        inner = unwrap(d)
        print("\n✅ 重放成功，拿到新凭据：")
        print("  accessToken 前缀:", str(inner.get("accessToken"))[:8], "…")
        print("  schoolId:", inner.get("schoolId"))
        print("  有效期至:", time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(inner["accessTokenExpires"] / 1000)))
    elif isinstance(obj.get("data"), dict):
        print("data 字段:", sorted(obj["data"].keys()))
    else:
        print("响应:", text[:300])
except Exception as e:
    print("解析失败:", e)
    print(text[:300])
