import sys, time, datetime
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

cfg = L.load_json(L.CONFIG, {})
c = L.Client(L.load_json(L.STATE))

items = None
for i in range(15):
    items = c.activities(req_timeout=5)
    if items:
        break
    time.sleep(0.4)

if not items:
    print("拉取失败")
    sys.exit(1)

reg = [it for it in items if it.get("isRegister")]
print(f"活动总数 {len(items)}，已报名 {len(reg)} 条：\n")
for it in sorted(reg, key=lambda x: int(x.get("startTime") or 0)):
    d = datetime.datetime.fromtimestamp(int(it["startTime"]) / 1000)
    a = L.activity_slot(it)
    print(f"  {d:%m-%d %H:%M}  {L.slot_text(a) if a else '?'}  {it.get('name')}")
    print(f"      id={it.get('id')}  isSign={it.get('isSign')}")

print()
normal = [x for x in reg if not L.is_special(x, cfg)]
print(f"普通活动 {len(normal)} / 上限 {cfg.get('max_normal')}")
byday = {}
for it in normal:
    d = datetime.date.fromtimestamp(int(it["startTime"]) / 1000)
    byday[d] = byday.get(d, 0) + 1
print("按天:", {str(k): v for k, v in sorted(byday.items())})
