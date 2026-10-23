import sys
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

cfg = L.load_json(L.CONFIG, {})
tt = L.load_timetable()

b8 = L.busy_for_week(cfg, tt, 8)
b9 = L.busy_for_week(cfg, tt, 9)


def has(busy, wd, sh, sm, eh, em):
    s = (wd, sh * 60 + sm, eh * 60 + em)
    return any(L.overlaps(s, b) for b, _ in busy)


print(f"第8周占用 {len(b8)} 个时段，第9周占用 {len(b9)} 个\n")

print("【第8周】")
print("  周一14:00 撞大学体育      :", has(b8, 1, 14, 0, 15, 35), "(期望 True)")
print("  周二08:00 撞国家安全教育  :", has(b8, 2, 8, 0, 9, 35), "(期望 True)")
print("  周五16:00 空闲            :", has(b8, 5, 16, 0, 17, 0), "(期望 False)")
print("  周日14:00 空闲            :", has(b8, 7, 14, 0, 15, 0), "(期望 False)")

print("\n【第9周】")
print("  周二08:00 国家安全教育结束 :", has(b9, 2, 8, 0, 9, 35), "(期望 False)")
print("  周二15:55 职规接棒         :", has(b9, 2, 15, 55, 17, 30), "(期望 True)")
print("  周一14:00 体育课仍是1-16周 :", has(b9, 1, 14, 0, 15, 35), "(期望 True)")
print("  周三14:00 学科竞赛5-16周   :", has(b9, 3, 14, 0, 15, 35), "(期望 True)")

print("\n【边界：紧邻不算冲突】")
a = (1, 9 * 60 + 55, 12 * 60 + 20)
b = (1, 12 * 60 + 20, 13 * 60)
print("  09:55-12:20 vs 12:20-13:00 :", L.overlaps(a, b), "(期望 False)")
