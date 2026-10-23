import re

path = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\apk\assets\apps\__UNI__DEAC2C5\www\app-service.js"
s = open(path, encoding="utf-8", errors="ignore").read()


def dump(marker, length, label):
    i = s.find(marker)
    print("=" * 78)
    print(f"{label}  @ {i}")
    print("=" * 78)
    if i < 0:
        print("NOT FOUND")
        print()
        return
    print(s[i:i + length].replace("\n", " "))
    print()


dump('"f6da":function', 2500, "MODULE f6da (auth / refreshToken)")
dump('"91a2":function', 6000, "MODULE 91a2 (courseCandidateList page)")
