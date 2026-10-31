import sys, time, datetime
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

cfg = L.load_json(L.CONFIG, {})
c = L.Client(L.load_json(L.STATE))

items = None
for i in range(20):
    code, obj = c.call("POST", "education/app/activity/getActivityList",
                       {"state": 1, "page": 1, "size": 200,
                        "semesterId": c.s["semesterId"]}, timeout=5)
    if code == 200 and isinstance(obj, dict) and obj.get("code") == 0:
        d = obj.get("data") or {}
        items = d.get("items") or []
        print(f"第{i+1}次成功: total={d.get('total')} items={len(items)}")
        break
    msg = obj.get("message") if isinstance(obj, dict) else str(obj)[:50]
    print(f"第{i+1}次失败: http={code} {str(msg)[:60]}")
    time.sleep(0.8)

if items is None:
    print("20 次都失败")
    sys.exit(1)

rows = []
for it in items:
    if L.is_member_only(it):
        continue
    try:
        d = datetime.datetime.fromtimestamp(int(it["startTime"]) / 1000).date()
    except Exception:
        continue
    rows.append((d, it))

rows.sort(key=lambda x: (x[0], (L.activity_slot(x[1]) or (0, 0, 0))[1]))
print(f"\n非会员活动 {len(rows)} 条，按日期：\n")
for d, it in rows[:40]:
    a = L.activity_slot(it)
    when = L.slot_text(a)[:11] if a else "?"
    print(f"  {d} {when}  余{L.remaining(it):>3}/{it.get('limitPeopleNumber')}  {it.get('name')}")
