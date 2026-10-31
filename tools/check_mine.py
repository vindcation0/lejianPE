import sys, time, datetime
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

cfg = L.load_json(L.CONFIG, {})
c = L.Client(L.load_json(L.STATE))
sem = c.s["semesterId"]

ENDPOINTS = [
    ("我的活动(全)", "education/app/activity/my/getActivityList",
     {"state": 0, "page": 1, "size": 50, "semesterId": sem}),
    ("我的活动(报名中)", "education/app/activity/my/getActivityList",
     {"state": 1, "page": 1, "size": 50, "semesterId": sem}),
    ("活动列表", "education/app/activity/getActivityList",
     {"state": 1, "page": 1, "size": 200, "semesterId": sem}),
]

found = False
for round_no in range(1, 41):
    for label, path, body in ENDPOINTS:
        code, obj = c.call("POST", path, body, timeout=6)
        if code == 200 and isinstance(obj, dict) and obj.get("code") == 0:
            d = obj.get("data") or {}
            items = d.get("items") or []
            print(f"[{datetime.datetime.now():%H:%M:%S}] {label} 成功 items={len(items)}")
            reg = [it for it in items if it.get("isRegister")]
            if label != "活动列表" or reg:
                print(f"  已报名 {len(reg)} 条：")
                for it in sorted(reg, key=lambda x: int(x.get("startTime") or 0)):
                    dt = datetime.datetime.fromtimestamp(int(it["startTime"]) / 1000)
                    a = L.activity_slot(it)
                    print(f"    {dt:%m-%d %H:%M}  {L.slot_text(a) if a else '?'}  {it.get('name')}")
                if reg:
                    found = True
                    break
        else:
            msg = obj.get("message") if isinstance(obj, dict) else str(obj)[:40]
            print(f"  [{datetime.datetime.now():%H:%M:%S}] {label} 失败 http={code} {str(msg)[:50]}")
        time.sleep(0.8)
    if found:
        break
    time.sleep(2)

if not found:
    print("\n40 轮都没拿到干净的报名数据，服务器仍未恢复")
