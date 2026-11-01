import sys, time
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

c = L.Client(L.load_json(L.STATE))
for i in range(4):
    t0 = time.time()
    code, obj = c.call("POST", "education/app/activity/getActivityList",
                       {"state": 1, "page": 1, "size": 200,
                        "semesterId": c.s["semesterId"]}, timeout=20)
    dt = time.time() - t0
    if code == 200 and isinstance(obj, dict) and obj.get("code") == 0:
        d = obj.get("data") or {}
        print(f"  [{time.strftime('%H:%M:%S')}] OK {dt:5.2f}s total={d.get('total')} items={len(d.get('items') or [])}")
        break
    msg = obj.get("message") if isinstance(obj, dict) else str(obj)[:50]
    print(f"  [{time.strftime('%H:%M:%S')}] 失败 {dt:5.2f}s http={code} {str(msg)[:50]}")
    time.sleep(0.5)
