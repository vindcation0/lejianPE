"""退掉指定日期的已报名活动。默认只退 2026-10-27 那节户外跑。"""
import sys, time, datetime
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

TARGET_DATE = datetime.date(2026, 10, 27)

cfg = L.load_json(L.CONFIG, {})
c = L.Client(L.load_json(L.STATE))


def fetch():
    for _ in range(25):
        items = c.activities(req_timeout=6)
        if items:
            return items
        time.sleep(0.8)
    return None


print(f"[{datetime.datetime.now():%H:%M:%S}] 拉取活动列表…")
items = fetch()
if not items:
    sys.exit("拉取失败，稍后重试")

reg = [it for it in items if it.get("isRegister")]
print(f"当前已报名 {len(reg)} 条：")
target = None
for it in sorted(reg, key=lambda x: int(x.get("startTime") or 0)):
    d = datetime.datetime.fromtimestamp(int(it["startTime"]) / 1000).date()
    a = L.activity_slot(it)
    name = (it.get("name") or "").replace("\n", " ")
    print(f"  {d} {L.slot_text(a) if a else '?'}  {name}  id={it.get('id')}")
    if d == TARGET_DATE:
        target = it

if not target:
    sys.exit(f"\n没找到 {TARGET_DATE} 的报名记录，未做任何操作")

name = (target.get("name") or "").replace("\n", " ")
print(f"\n准备退掉：{TARGET_DATE} {name}  id={target.get('id')}")

ok = False
for attempt in range(1, 11):
    code, obj = c.call("POST", "education/app/activity/cancelSignUp",
                       {"activityId": target["id"]}, timeout=8)
    msg = obj.get("message") if isinstance(obj, dict) else str(obj)[:60]
    success = isinstance(obj, dict) and obj.get("code") == 0 and obj.get("data")
    print(f"  第{attempt}次: http={code} code={obj.get('code') if isinstance(obj,dict) else '?'} {str(msg)[:50]} -> {'成功' if success else '未成功'}")
    if success:
        ok = True
        break
    time.sleep(1.5)

print("\n退课" + ("成功" if ok else "未成功"))

print(f"\n[{datetime.datetime.now():%H:%M:%S}] 复核…")
items2 = fetch()
if items2:
    reg2 = [it for it in items2 if it.get("isRegister")]
    print(f"现在已报名 {len(reg2)} 条：")
    for it in sorted(reg2, key=lambda x: int(x.get("startTime") or 0)):
        d = datetime.datetime.fromtimestamp(int(it["startTime"]) / 1000)
        a = L.activity_slot(it)
        nm = (it.get("name") or "").replace("\n", " ")
        print(f"  {d:%m-%d %H:%M}  {L.slot_text(a) if a else '?'}  {nm}")
else:
    print("复核拉取失败，请稍后手动确认")
