import sys, json, hashlib
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\tools")
from login_envelope import unwrap

P = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\login_flows.jsonl"

rec = None
for line in open(P, encoding="utf-8"):
    if "manage/login" in line:
        rec = json.loads(line)
        break

body = json.loads(rec["req_body"])
payload = unwrap(body)

print("=== 请求明文（敏感字段已打码）===")
for k in sorted(payload):
    v = payload[k]
    if k in ("password",):
        print(f"  {k:<12} = <已隐藏>  长度={len(str(v))}")
    elif k == "signDigital":
        print(f"  {k:<12} = {str(v)[:10]}...  长度={len(str(v))}")
    elif k == "phone" or k == "userName":
        s = str(v)
        print(f"  {k:<12} = {s[:3]}****{s[-2:]}  长度={len(s)}")
    else:
        print(f"  {k:<12} = {v!r}")

print("\n=== 响应 ===")
resp = json.loads(rec["resp_body"])
print("  code =", resp.get("code"), " message =", resp.get("message"))
d = resp.get("data")
if isinstance(d, dict):
    print("  data 字段 =", sorted(d.keys()))
    inner = None
    if "pyd" in d and "t" in d:
        try:
            inner = unwrap(d)
            print("  解密成功，字段 =", sorted(inner.keys()))
            for k, v in inner.items():
                if isinstance(v, str) and len(v) > 24:
                    print(f"    {k:<22} = {v[:8]}... (len={len(v)})")
                else:
                    print(f"    {k:<22} = {v!r}")
        except Exception as e:
            print("  解密失败:", e)
