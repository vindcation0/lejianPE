"""独立核验文档中对登录协议的断言（只读静态检查，不联网）。"""
import os, re

APK = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\apk"

CHECKS = [
    ("v2 登录端点", b"authorization/user/v2/manage/login"),
    ("v1 登录端点", b"authorization/user/manage/login"),
    ("signDigital 字段", b"signDigital"),
    ("LoginReqBean", b"LoginReqBean"),
    ("Rengis 类", b"cn/legym/ai/util/Rengis"),
    ("Rengis.hs 符号", b"Rengis"),
    ("SecurityRequestBodyConverter", b"SecurityRequestBodyConverter"),
    ("SecurityResponseBodyConverter", b"SecurityResponseBodyConverter"),
    ("getDynamicKey", b"getDynamicKey"),
    ("动态密钥盐 402881ea...", b"402881ea7c39c5d5017c39d143a8062b"),
    ("疑似 SHA1 盐 lgAI2021key", b"lgAI2021key"),
    ("entrance 字段", b"entrance"),
]

TARGETS = []
for root, _, files in os.walk(APK):
    for f in files:
        if f.endswith((".dex", ".so")):
            TARGETS.append(os.path.join(root, f))

print(f"扫描 {len(TARGETS)} 个 dex/so 文件\n")

results = {}
for label, needle in CHECKS:
    hits = []
    for p in TARGETS:
        try:
            with open(p, "rb") as fh:
                data = fh.read()
        except Exception:
            continue
        i = data.find(needle)
        if i >= 0:
            hits.append((os.path.relpath(p, APK), i))
    results[label] = hits

for label, _ in CHECKS:
    hits = results[label]
    mark = "✅" if hits else "❌"
    print(f"{mark} {label}")
    for path, off in hits[:4]:
        print(f"      {path}   @0x{off:x}")
    if not hits:
        print("      （未找到）")
