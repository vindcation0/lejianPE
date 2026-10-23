import re

JS = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\apk\assets\apps\__UNI__DEAC2C5\www\app-service.js"
OUT = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\activity_api.txt"

s = open(JS, encoding="utf-8", errors="ignore").read()
s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)

lines = []


def show(label, marker, before=300, after=1200, limit=4):
    idxs = [m.start() for m in re.finditer(re.escape(marker), s)]
    lines.append("=" * 78)
    lines.append(f"{label}  ({marker!r})  hits={len(idxs)}")
    lines.append("=" * 78)
    for i in idxs[:limit]:
        lines.append("-" * 78)
        lines.append(s[max(0, i - before): i + after].replace("\n", " "))
    lines.append("")


show("activity signUp", "activityId")
show("activitie base", 'activitie"')
show("education/activity", "education/activity", 200, 900)
show("checkConflict", "checkConflict", 200, 900)

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("written:", OUT)
