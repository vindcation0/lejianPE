import sys, datetime
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

cfg = L.load_json(L.CONFIG, {})
tt = L.load_timetable()
c = L.Client(L.load_json(L.STATE))

items = c.activities()
print("拉取到", len(items), "条")

dates = {}
for it in items:
    try:
        d = datetime.datetime.fromtimestamp(int(it["startTime"]) / 1000).date()
    except Exception:
        continue
    dates[str(d)] = dates.get(str(d), 0) + 1
print("日期分布:", dict(sorted(dates.items())))
print()

rows = []
for it in items:
    if L.is_member_only(it):
        continue
    try:
        d = datetime.datetime.fromtimestamp(int(it["startTime"]) / 1000).date()
    except Exception:
        continue
    if d < datetime.date(2026, 10, 26):
        continue
    rows.append((d, it))

rows.sort(key=lambda x: (x[0], (L.activity_slot(x[1]) or (0, 0, 0))[1]))
print(f"10-26 之后的非会员活动 {len(rows)} 条：")
for d, it in rows[:30]:
    a = L.activity_slot(it)
    when = L.slot_text(a)[:11] if a else "?"
    print(f"  {d} {when}  余{L.remaining(it):>3}/{it.get('limitPeopleNumber')}  {it.get('name')}")
