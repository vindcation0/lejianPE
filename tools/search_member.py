import re, json, os

JS = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\apk\assets\apps\__UNI__DEAC2C5\www\app-service.js"
OUT = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\js_search.txt"

s = open(JS, encoding="utf-8", errors="ignore").read()

# 把 \u4f1a\u5458 这类转义还原成中文
def unesc(m):
    try:
        return chr(int(m.group(1), 16))
    except Exception:
        return m.group(0)

decoded = re.sub(r"\\u([0-9a-fA-F]{4})", unesc, s)

keywords = ["会员", "VIP", "vip", "member", "Member", "冲突", "课表",
            "timetable", "schedule", "必修", "forStudent", "priority"]

lines = []
for kw in keywords:
    idxs = [m.start() for m in re.finditer(re.escape(kw), decoded)]
    lines.append("=" * 78)
    lines.append(f"KEYWORD {kw!r}  hits={len(idxs)}")
    lines.append("=" * 78)
    for i in idxs[:6]:
        lines.append("-" * 78)
        lines.append(decoded[max(0, i - 250): i + 250].replace("\n", " "))
    lines.append("")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print("written:", OUT, len(lines), "lines")
