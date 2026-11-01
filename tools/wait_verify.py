import sys, time, datetime
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

c = L.Client(L.load_json(L.STATE))
deadline = time.time() + 600
last = None

while time.time() < deadline:
    code, obj = c.call("POST", "education/app/activity/getActivityList",
                       {"state": 1, "page": 1, "size": 200,
                        "semesterId": c.s["semesterId"]}, timeout=8)
    if code == 200 and isinstance(obj, dict) and obj.get("code") == 0:
        items = (obj.get("data") or {}).get("items") or []
        reg = [i for i in items if i.get("isRegister")]
        sig = tuple(sorted(i.get("id") for i in reg))
        if sig != last:
            last = sig
            print(f"[{datetime.datetime.now():%H:%M:%S}] 已报名 {len(reg)} 条：", flush=True)
            for it in sorted(reg, key=lambda x: int(x.get("startTime") or 0)):
                d = datetime.datetime.fromtimestamp(int(it["startTime"]) / 1000)
                nm = (it.get("name") or "").replace("\n", " ")
                print(f"    {d:%m-%d %H:%M}  {nm}", flush=True)
        time.sleep(2)
    else:
        time.sleep(1)
