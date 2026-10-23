import re

JS = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\apk\assets\apps\__UNI__DEAC2C5\www\app-service.js"
OUT = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\activity_callers.txt"

s = open(JS, encoding="utf-8", errors="ignore").read()
s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)

out = []
for kw in ["getActivityList(", "checkConflict(", "activity.signUp(", "activityId:"]:
    idxs = [m.start() for m in re.finditer(re.escape(kw), s)]
    out.append("=" * 78)
    out.append(f"{kw}  hits={len(idxs)}")
    out.append("=" * 78)
    for i in idxs[:5]:
        out.append("-" * 78)
        out.append(s[max(0, i - 500): i + 700].replace("\n", " "))
    out.append("")

open(OUT, "w", encoding="utf-8").write("\n".join(out))
print("written:", OUT)
