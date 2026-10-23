import re

path = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\apk\assets\apps\__UNI__DEAC2C5\www\app-service.js"
s = open(path, encoding="utf-8", errors="ignore").read()


def show(label, marker, before=400, after=900, limit=6):
    print("=" * 78)
    print(label)
    print("=" * 78)
    idxs = [m.start() for m in re.finditer(re.escape(marker), s)]
    print(f"hits: {len(idxs)}")
    for i in idxs[:limit]:
        print("-" * 78)
        print(s[max(0, i - before): i + after].replace("\n", " "))
    print()


show("BASE_URL_LIST definition", "BASE_URL_LIST")
show("signUpCourse callers", "signUpCourse")
show("getCourseSelectionList callers", "getCourseSelectionList", 300, 700, 3)
