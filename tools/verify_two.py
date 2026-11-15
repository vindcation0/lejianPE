"""交叉验证真实报名结果：本地状态库 + 我的活动接口 + 活动列表。"""
import sqlite3
import sys
import json
import datetime

ROOT = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE"
sys.path.insert(0, ROOT)
import lejian as L  # noqa: E402

DB = ROOT + r"\data\registration.db"

print("=" * 66)
print("来源 1：本地状态库 data/registration.db")
print("=" * 66)
db = sqlite3.connect(DB)
cur = db.cursor()
tabs = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("表:", tabs)
for t in tabs:
    cols = [c[1] for c in cur.execute(f"PRAGMA table_info({t})")]
    n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"\n  [{t}] {n} 行  列={cols}")
    for row in cur.execute(f"SELECT * FROM {t} LIMIT 30"):
        rec = dict(zip(cols, row))
        if str(rec.get("state", "")).upper() in ("SUCCESS", "S") or "SUCCESS" in str(row):
            print("    >>>", rec)

print()
print("=" * 66)
print("来源 2：我的活动接口 my/getActivityList")
print("=" * 66)
c = L.Client(L.load_json(L.STATE))
sem = c.s["semesterId"]
for state in (1, 2, 3):
    for body in ({"state": state, "page": 1, "size": 50, "semesterId": sem},
                 {"state": state, "page": 1, "size": 50, "day": 30}):
        code, obj = c.call("POST", "education/app/activity/my/getActivityList",
                           body, timeout=10)
        if code == 200 and isinstance(obj, dict) and obj.get("code") == 0:
            items = (obj.get("data") or {}).get("items") or []
            print(f"  state={state} {'day' if 'day' in body else 'sem'} -> {len(items)} 条")
            for it in items:
                d = datetime.datetime.fromtimestamp(int(it["startTime"]) / 1000)
                nm = (it.get("name") or "").replace("\n", " ")
                print(f"    {d:%m-%d %H:%M}  {nm}  id={it.get('id')}")
            break
        else:
            msg = obj.get("message") if isinstance(obj, dict) else str(obj)[:40]
            print(f"  state={state} {'day' if 'day' in body else 'sem'} -> http={code} {str(msg)[:40]}")

print()
print("=" * 66)
print("来源 3：活动列表 isRegister")
print("=" * 66)
items = c.activities()
reg = [i for i in items if i.get("isRegister")]
print(f"  活动总数 {len(items)}，isRegister {len(reg)} 条")
for it in sorted(reg, key=lambda x: int(x["startTime"])):
    d = datetime.datetime.fromtimestamp(int(it["startTime"]) / 1000)
    nm = (it.get("name") or "").replace("\n", " ")
    print(f"    {d:%m-%d %H:%M}  {nm}  id={it.get('id')}")
