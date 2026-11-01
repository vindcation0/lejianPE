import sys, time
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

c = L.Client(L.load_json(L.STATE))
sem = c.s["semesterId"]

for rnd in range(12):
    for state in (1, 2, 3, 0):
        for body in ({"state": state, "page": 1, "size": 50, "semesterId": sem},
                     {"state": state, "page": 1, "size": 50, "day": 30}):
            code, obj = c.call("POST", "education/app/activity/my/getActivityList",
                               body, timeout=6)
            tag = f"state={state} {'day30' if 'day' in body else 'sem'}"
            if code == 200 and isinstance(obj, dict) and obj.get("code") == 0:
                d = obj.get("data") or {}
                items = d.get("items") or []
                print(f"OK {tag}: items={len(items)} total={d.get('total')}")
                for it in items:
                    print("   ", (it.get("name") or "").replace("\n", " "),
                          it.get("startTime"), "isRegister=", it.get("isRegister"))
                raise SystemExit(0)
            msg = obj.get("message") if isinstance(obj, dict) else str(obj)[:40]
            print(f"  {tag}: http={code} {str(msg)[:45]}")
            time.sleep(0.2)
    time.sleep(1)
print("12 轮都失败")
