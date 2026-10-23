import re, sys

path = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\apk\assets\apps\__UNI__DEAC2C5\www\app-service.js"
s = open(path, encoding="utf-8", errors="ignore").read()
print("file length:", len(s))
print()

for kw in [
    "optional/course",
    "education/optional",
    "option/course",
    "selectCourse",
    "course/select",
    "choiceCourse",
    "elective",
]:
    idxs = [m.start() for m in re.finditer(re.escape(kw), s)]
    print("=" * 70)
    print(f"KEYWORD {kw!r}: {len(idxs)} hits")
    for i in idxs[:6]:
        print("-" * 70)
        print(s[max(0, i - 300): i + 300].replace("\n", " "))
    print()
