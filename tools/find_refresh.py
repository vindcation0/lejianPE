import re

path = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\apk\assets\apps\__UNI__DEAC2C5\www\app-service.js"
s = open(path, encoding="utf-8", errors="ignore").read()

for kw in ['refreshToken:function', '"refresh"', 'refreshToken', '/login', 'authorization/login']:
    idxs = [m.start() for m in re.finditer(re.escape(kw), s)]
    print("=" * 74)
    print(f"{kw!r} -> {len(idxs)} hits")
    for i in idxs[:4]:
        print("-" * 74)
        print(s[max(0, i - 350): i + 350].replace("\n", " "))
    print()
