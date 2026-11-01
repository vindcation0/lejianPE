"""验证「一天只能抢一节」：同一天全排除，不同天正常可选。"""
import sys, datetime
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

cfg = L.load_json(L.CONFIG, {})
tt = L.load_timetable()
cw = L.current_week(tt)
c = L.Client(L.load_json(L.STATE))
items = c.activities()
now = datetime.datetime.now()


def mk(name, dt, minutes, aid):
    st = int(dt.timestamp() * 1000)
    return {
        "id": aid, "name": name, "startTime": st,
        "endTime": st + minutes * 60 * 1000,
        "limitPeopleNumber": 50, "registerPeople": 0, "isRegister": False,
        "startRegisterTime": int((now - datetime.timedelta(hours=1)).timestamp() * 1000),
        "endRegisterTime": int((now + datetime.timedelta(days=9)).timestamp() * 1000),
    }


FRI = datetime.datetime(2026, 10, 23)   # 已有一节户外跑在这天
NEXT_FRI = datetime.datetime(2026, 10, 30)  # 第9周

cases = [
    mk("跳绳 亢莉萍", FRI.replace(hour=19, minute=0), 60, "FAKE-SAME-DAY"),      # 同天，时间不冲突
    mk("跳绳 亢莉萍", NEXT_FRI.replace(hour=16, minute=0), 60, "FAKE-NEXT-WEEK"),  # 不同天
    mk("排球 贾建韬", NEXT_FRI.replace(hour=17, minute=10), 60, "FAKE-NEXT-WEEK-2"),  # 不同天、不同时段
]

picked = L.pick_targets(items + cases, cfg, tt, cw)
ids = {p[1]["id"] for p in picked}

print("已报：户外跑 10-23 17:40（第8周）")
print(f"每天上限 = {cfg.get('max_per_day', 1)}\n")
for cse in cases:
    d = datetime.datetime.fromtimestamp(cse["startTime"] / 1000)
    ok = cse["id"] in ids
    expect = cse["id"] != "FAKE-SAME-DAY"
    mark = "✅" if ok == expect else "❌"
    print(f"  {cse['name']:<12} {d:%m-%d %H:%M}  -> {'可选' if ok else '排除'} "
          f"(期望{'可选' if expect else '排除'}) {mark}")
