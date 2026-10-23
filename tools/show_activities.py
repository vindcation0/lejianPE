import json
from datetime import datetime

P = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\activity_probe.json"
OUT = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\activity_parsed.txt"

data = json.load(open(P, encoding="utf-8"))
lines = []

for case in data:
    if len(case["response"]) < 500:
        continue
    obj = json.loads(case["response"])
    d = obj.get("data") or {}
    items = d.get("items") or []
    lines.append("=" * 78)
    lines.append(f"case: {case['label']}   items={len(items)}   total={d.get('total')}")
    lines.append("=" * 78)

    if items:
        lines.append("字段清单：")
        lines.append("  " + ", ".join(sorted(items[0].keys())))
        lines.append("")

    for it in items:
        pt = it.get("performTime")
        try:
            pt_s = datetime.fromtimestamp(int(pt) / 1000).strftime("%m-%d %H:%M")
        except Exception:
            pt_s = str(pt)
        lines.append("-" * 70)
        lines.append(f"name      : {it.get('name')}")
        lines.append(f"id        : {it.get('id')}")
        lines.append(f"performTime: {pt_s}  (raw={pt})")
        for k in ("state", "stateStr", "maxNumber", "registerNumber", "surplus",
                  "isRegister", "isSign", "signUpState", "address", "place",
                  "activityType", "type", "isMember", "memberOnly", "topicalId",
                  "topicalName", "schoolId", "semesterId"):
            if k in it:
                lines.append(f"{k:10}: {it.get(k)}")

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("written:", OUT, len(lines), "lines")
