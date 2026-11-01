"""扩大搜索面反推 signDigital：加入设备标识、编码、分隔符等维度。"""
import json, hashlib, itertools, sys, os
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\tools")
from login_envelope import unwrap

P = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\login_flows.jsonl"
rec = next(json.loads(l) for l in open(P, encoding="utf-8") if "manage/login" in l)
pl = unwrap(json.loads(rec["req_body"]))

F = {
    "u": str(pl["userName"]),
    "p": str(pl["password"]),
    "e": str(pl.get("entrance") or ""),
    "a": str(pl.get("authType") or ""),
    "b": str(pl.get("baseUrl") or ""),
    "d": os.environ.get("LJ_ANDROID_ID", ""),   # android_id（从环境变量注入，避免写死）
    "m": os.environ.get("LJ_MODEL", "SM-A5260"),   # model
    "r": os.environ.get("LJ_BRAND", "Samsung"),    # brand
    "k": "com.ledreamer.zz",                       # package
}
TARGET = str(pl["signDigital"]).lower()

ORDERS = [
    "upe", "up", "eup", "upea", "upeb", "upeabd",
    "upeabdkmr", "uped", "dupue"[:4], "upead", "upek", "upedkmr",
    "upem", "uper", "upebkmr",
]
SEPS = ["", ":", "|", ",", "_", "-"]
ENCS = ["utf-8", "utf-16-le", "utf-16-be", "gbk"]

SALTS = [
    "", "lgAI2021key~*21B", "402881ea7c39c5d5017c39d143a8062b",
    "legym", "LEGYM", "Legym", "legymAI", "lgAI2021",
]
WINDOWS = ["{}", "{}{}", "{}-{}"]

ALGS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256, "md5": hashlib.md5}

hits = []
tried = 0

for order in ORDERS:
    parts = [F[c] for c in order if c in F]
    if len(parts) != len(order):
        continue
    for sep in SEPS:
        base = sep.join(parts)
        for enc in ENCS:
            for salt in SALTS:
                for w in WINDOWS:
                    if w == "{}":
                        s = base
                    elif w == "{}{}":
                        s = base + salt
                        if not salt:
                            continue
                    else:
                        s = salt + "-" + base
                    try:
                        raw = s.encode(enc)
                    except Exception:
                        continue
                    for an, fn in ALGS.items():
                        tried += 1
                        h = fn(raw).hexdigest()
                        if h == TARGET:
                            hits.append((f"{an}({order}|sep={sep!r}|{enc}|salt={salt!r}|{w})"))
                        if an == "sha1" and h.upper() == TARGET.upper():
                            hits.append((f"{an}UPPER({order}|sep={sep!r}|{enc}|salt={salt!r}|{w})"))

print(f"尝试 {tried} 种组合")
if hits:
    print(f"\n✅ 命中 {len(hits)} 个：")
    for h in dict.fromkeys(hits):
        print("   ", h)
else:
    print("\n❌ 仍无命中")
    print("说明 hs() 的输入里还有未知成分（很可能是设备指纹或包签名摘要）")
