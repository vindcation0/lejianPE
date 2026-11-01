import os, re

APK = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\apk"
FILES = [
    (r"lib\arm64-v8a\libsportaction.so", 0x28821),
    (r"lib\arm64-v8a\libdexvmp.so", 0x6a5f2),
]


def strings_near(data, start, before=400, after=700):
    lo = max(0, start - before)
    hi = min(len(data), start + after)
    chunk = data[lo:hi]
    parts = re.findall(rb"[\x20-\x7e]{4,}", chunk)
    return [p.decode() for p in parts]


for rel, off in FILES:
    p = os.path.join(APK, rel)
    data = open(p, "rb").read()
    print("=" * 70)
    print(rel, f"@0x{off:x}")
    print("=" * 70)
    for s in strings_near(data, off):
        print("   ", s)
    print()
