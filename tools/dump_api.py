import re

path = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\apk\assets\apps\__UNI__DEAC2C5\www\app-service.js"
s = open(path, encoding="utf-8", errors="ignore").read()


def dump(marker, length, label):
    i = s.find(marker)
    print("=" * 78)
    print(f"{label}   at {i}")
    print("=" * 78)
    if i < 0:
        print("NOT FOUND")
        return
    print(s[i:i + length].replace("\n", " "))
    print()


dump("dc6f:function", 4000, "MODULE dc6f (course selection API)")
dump('"8daa":function', 3500, "MODULE 8daa (http wrapper)")
