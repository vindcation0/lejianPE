import sys, os, datetime
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

cfg = L.load_json(L.CONFIG, {})
tt = L.load_timetable()
cw = L.current_week(tt)
c = L.Client(L.load_json(L.STATE))
items = c.activities()

reasons = {"已报名": 0, "会员制": 0, "满员": 0, "时间冲突": 0, "无时间": 0, "非目标": 0}
kept = []
cache = {}

for it in items:
    name = it.get("name") or ""
    if it.get("isRegister"):
        reasons["已报名"] += 1
        continue
    if cfg.get("exclude_member", True) and L.is_member_only(it):
        reasons["会员制"] += 1
        continue
    if L.remaining(it) == 0:
        reasons["满员"] += 1
        continue
    a_slot = L.activity_slot(it)
    if a_slot is None:
        reasons["无时间"] += 1
        continue
    wk = L.week_of(datetime.date.fromtimestamp(int(it["startTime"]) / 1000), cw)
    if wk not in cache:
        cache[wk] = L.busy_for_week(cfg, tt, wk)
    hit = next((why for b, why in cache[wk] if L.overlaps(a_slot, b)), None)
    if hit:
        reasons["时间冲突"] += 1
        if len([x for x in kept if False]) < 0:
            pass
        continue
    if L.is_special(it, cfg) or any(k in name for k in cfg.get("sports") or []):
        kept.append(it)
    else:
        reasons["非目标"] += 1

print("活动总数:", len(items))
for k, v in reasons.items():
    print(f"  {k:6}: {v}")
print("  可抢   :", len(kept))
print()

print("被冲突过滤掉的例子：")
shown = 0
for it in items:
    if it.get("isRegister") or L.is_member_only(it) or L.remaining(it) == 0:
        continue
    a_slot = L.activity_slot(it)
    if not a_slot:
        continue
    wk = L.week_of(datetime.date.fromtimestamp(int(it["startTime"]) / 1000), cw)
    busy = cache.get(wk) or L.busy_for_week(cfg, tt, wk)
    hit = next((why for b, why in busy if L.overlaps(a_slot, b)), None)
    if hit:
        print(f"  {it.get('name')}  {L.slot_text(a_slot)}  <- 撞 {hit}")
        shown += 1
        if shown >= 6:
            break
