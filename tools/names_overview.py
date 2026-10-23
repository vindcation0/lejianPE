import sys, datetime
from collections import Counter, defaultdict
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

cfg = L.load_json(L.CONFIG, {})
tt = L.load_timetable()
c = L.Client(L.load_json(L.STATE))
items = c.activities()

normal = [i for i in items if not L.is_member_only(i)]
special = [i for i in items if L.is_special(i, cfg) and not L.is_member_only(i)]

print(f"非会员活动 {len(normal)} 条\n")

by_name = defaultdict(list)
for it in normal:
    by_name[it.get("name")].append(it)

print(f"{'课程名':<28}{'场次':>4}   时段 / 余量")
print("-" * 96)
for name in sorted(by_name, key=lambda n: (-len(by_name[n]), n)):
    rows = by_name[name]
    slots = []
    for r in rows[:4]:
        a = L.activity_slot(r)
        slots.append(f"{L.slot_text(a)[:11]} 余{L.remaining(r)}"
                     if a else f"余{L.remaining(r)}")
    more = f"  …另{len(rows)-4}场" if len(rows) > 4 else ""
    print(f"{name:<28}{len(rows):>4}   {' | '.join(slots)}{more}")

print()
print("体质健康测试类：")
for it in special:
    a = L.activity_slot(it)
    print(f"  {it.get('name')}  {L.slot_text(a) if a else '-'}  余{L.remaining(it)}")
