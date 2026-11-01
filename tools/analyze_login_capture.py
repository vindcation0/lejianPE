"""
分析一次真实登录抓包，反推 signDigital 的生成算法。

用法：
    python tools/analyze_login_capture.py captures/login.har
    python tools/analyze_login_capture.py captures/login.json

行为：
  1. 从 HAR / JSON 中找出 POST /authorization/user/v2/manage/login 的请求体
  2. 用 login_envelope 解出明文（userName / password / entrance / signDigital）
  3. 穷举常见拼接口径与哈希算法，比对 signDigital
  4. **只输出哪一种组合匹配**，绝不打印密码或签名原文

如果没有匹配，说明算法里可能还掺了设备指纹，需要改用 Frida 抓 hs() 的入参。
"""
import base64
import hashlib
import itertools
import json
import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from login_envelope import unwrap  # noqa: E402

LOGIN_PATH = "authorization/user/v2/manage/login"

SALTS = [
    "lgAI2021key~*21B",
    "402881ea7c39c5d5017c39d143a8062b",
    "legym", "LEGYM", "Legym", "legymAI", "lgAI2021", "lgAI2021key",
]


def load_request(path):
    """返回登录请求的明文 JSON"""
    raw = open(path, "rb").read()

    if path.lower().endswith(".jsonl"):
        for line in raw.decode("utf-8", "ignore").splitlines():
            if LOGIN_PATH not in line:
                continue
            rec = json.loads(line)
            body = (rec.get("req_body") or "").strip()
            if not body:
                continue
            obj = json.loads(body)
            return unwrap(obj) if "pyd" in obj else obj
        raise SystemExit(f"在 {path} 里没找到 {LOGIN_PATH} 的记录")

    if path.lower().endswith(".json"):
        obj = json.loads(raw)
        if "t" in obj and "pyd" in obj:
            return unwrap(obj)
        return obj

    har = json.loads(raw)
    entries = har.get("log", {}).get("entries", [])
    for e in entries:
        req = e.get("request", {})
        url = req.get("url", "")
        if LOGIN_PATH not in url:
            continue
        body = req.get("postData", {}).get("text") or ""
        if not body:
            continue
        if body.lstrip().startswith("{"):
            try:
                obj = json.loads(body)
            except Exception:
                continue
            if "pyd" in obj:
                return unwrap(obj)
            return obj
    raise SystemExit(f"在 {path} 里没找到 {LOGIN_PATH} 的请求体")


def build_candidates(payload):
    """穷举 toSign 组合与哈希算法"""
    u = payload.get("userName") or payload.get("phone") or payload.get("mobile") or ""
    p = payload.get("password") or ""
    e = payload.get("entrance")

    parts = {"u": u, "p": p, "e": e if e is not None else ""}
    orders = [
        ("u", "p", "e"),
        ("u", "p"),
        ("p", "u"),
        ("u", "e", "p"),
    ]

    bases = []
    for order in orders:
        s = "".join(str(parts[k]) for k in order)
        bases.append((f"toSign={'+'.join(order)}", s))

    algs = {"sha1": hashlib.sha1, "sha256": hashlib.sha256, "md5": hashlib.md5}

    for label, s in bases:
        data = s.encode()
        for aname, fn in algs.items():
            for case in ("lower", "upper"):
                # 无盐
                yield (f"{aname}({label})[{case}]", fn(data).hexdigest().upper()
                       if case == "upper" else fn(data).hexdigest())
                # 盐在后
                for salt in SALTS:
                    v = fn(data + salt.encode()).hexdigest()
                    yield (f"{aname}({label}+{salt!r})[{case}]",
                           v.upper() if case == "upper" else v)
                    # 盐在前
                    v2 = fn(salt.encode() + data).hexdigest()
                    yield (f"{aname}({salt!r}+{label})[{case}]",
                           v2.upper() if case == "upper" else v2)


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)

    payload = load_request(sys.argv[1])
    target = (payload.get("signDigital") or "").strip()

    print("=" * 66)
    print("明文载荷字段:", ", ".join(sorted(payload.keys())))
    print("signDigital 长度:", len(target))
    print("=" * 66)

    if not target:
        raise SystemExit("载荷里没有 signDigital，无法比对")

    hits = [(name, val) for name, val in build_candidates(payload) if val == target]

    if hits:
        print(f"\n✅ 命中 {len(hits)} 个组合：")
        for name, val in hits:
            print(f"   {name}")
        print("\n结论：signDigital 可由纯 Python 复现，无需设备指纹。")
    else:
        tried = sum(1 for _ in build_candidates(payload))
        print(f"\n❌ 已尝试 {tried} 种组合，均不匹配。")
        print("可能原因：")
        print("  1. hs() 内部还掺入了设备/包名/签名等指纹")
        print("  2. 拼接顺序或编码与假设不同")
        print("  3. 使用的是自定义 SHA1 变体（非标准实现）")
        print("\n下一步建议：Frida Hook cn.legym.ai.util.Rengis.hs 直接取入参与返回值。")


if __name__ == "__main__":
    main()
