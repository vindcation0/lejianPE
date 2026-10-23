import re

path = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\apk\assets\apps\__UNI__DEAC2C5\www\app-service.js"
s = open(path, encoding="utf-8", errors="ignore").read()

# 抓所有形如 "前缀/xxx/yyy" 的字符串常量
pat = re.compile(r'"((?:education|authorization|auth|sports-core|running|activitie|user|student|semester|course|login|logout|token)[a-zA-Z0-9_/\-]*)"')
found = sorted(set(m.group(1) for m in pat.finditer(s)))

print(f"共 {len(found)} 条：")
print()
for f in found:
    print(f)

print()
print("=" * 70)
print("含 refresh / login / token 的：")
print("=" * 70)
for f in found:
    if any(k in f.lower() for k in ("refresh", "login", "token", "logout")):
        print("  ", f)
