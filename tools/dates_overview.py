import sys, datetime
from collections import defaultdict
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

cfg = L.load_json(L.CONFIG, {})
c = L.Client(L.load_json(L.STATE))
items = c.activities()

by_date = defaultdict(lambda: {"普通": [], "会员制": []})
for it in items:
    try:
        d = datetime.datetime.fromtimestamp(int(it["startTime"]) / 1000).date()
    except Exception:
        continue
    kind = "会员制" if L.is_member_only(it) else "普通"
    by_date[d][kind].append(it)

print(f"活动总数 {len(items)}\n")
print(f"{'日期':<12}{'周':<4}{'普通':>6}{'会员制':>8}   普通活动样例")
print("-" * 90)
for d in sorted(by_date):
    row = by_date[d]
    wd = "一二三四五六日"[d.weekday()]
    sample = "、".join(x.get("name", "")[:6] for x in row["普通"][:4]) or "—"
    print(f"{d} 周{wd}  {len(row['普通']):>6}{len(row['会员制']):>8}   {sample}")

print()
print("当前时间:", datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
