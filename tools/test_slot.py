import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lejian import parse_slot, slot_text, overlaps

cases = [
    "成都校区-趣味田径-2025-周天一-周一14:00~15:35",
    "羽毛球-周二08:00~09:35",
    "周天10:00~11:35",
    "篮球 周三 19:00-20:35",
    "没有时段的课程名",
]

for c in cases:
    s = parse_slot(c)
    print(f"{c!r:55} -> {slot_text(s) if s else 'None'}")

print()
a = parse_slot("周一14:00~15:35")
b = parse_slot("周一15:00~16:35")
c = parse_slot("周一15:35~17:10")
print("14:00-15:35 vs 15:00-16:35 overlap:", overlaps(a, b), "(expect True)")
print("14:00-15:35 vs 15:35-17:10 overlap:", overlaps(a, c), "(expect False)")
