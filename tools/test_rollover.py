"""模拟：周日出现第9周的新活动，本周(第8周)已报2节，是否会被错误地挡掉。"""
import sys, datetime
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

cfg = L.load_json(L.CONFIG, {})
tt = L.load_timetable()
c = L.Client(L.load_json(L.STATE))
items = c.activities()

now = datetime.datetime.now()


def fake(name, dt, minutes=60):
    st = int(dt.timestamp() * 1000)
    return {
        "id": "FAKE-" + name,
        "name": name,
        "startTime": st,
        "endTime": st + minutes * 60 * 1000,
        "limitPeopleNumber": 50,
        "registerPeople": 0,
        "isRegister": False,
        "startRegisterTime": int((now - datetime.timedelta(hours=1)).timestamp() * 1000),
        "endRegisterTime": int((now + datetime.timedelta(days=3)).timestamp() * 1000),
    }


# 第9周 周五 16:00（10-30），周五课表只有上午
wk9 = datetime.datetime(2026, 10, 30, 16, 0)
# 第9周 周二 08:00（10-27），第9周国家安全教育已结课
wk9_tue = datetime.datetime(2026, 10, 27, 8, 0)
# 第9周 周三 14:00（10-28），学科竞赛 5-16 周
wk9_wed = datetime.datetime(2026, 10, 28, 14, 0)

fakes = [
    fake("羽毛球", wk9),
    fake("篮球1", wk9_tue),
    fake("排球", wk9_wed),
]

picked = L.pick_targets(items + fakes, cfg, tt, L.current_week(tt))
fake_ids = {p[1]["id"] for p in picked if p[1]["id"].startswith("FAKE")}

print("第9周假活动通过筛选的情况：")
for f in fakes:
    ok = f["id"] in fake_ids
    print(f"  {f['name']:6} {datetime.datetime.fromtimestamp(f['startTime']/1000):%m-%d %H:%M}"
          f"  第{L.week_of(datetime.date.fromtimestamp(f['startTime']/1000), tt)}周 -> "
          f"{'通过' if ok else '被挡（冲突或非目标）'}")

# 名额统计
reg_by_week = {}
for i in items:
    if not i.get("isRegister") or L.is_special(i, cfg):
        continue
    w = L.week_of(datetime.date.fromtimestamp(int(i["startTime"]) / 1000), tt)
    reg_by_week[w] = reg_by_week.get(w, 0) + 1
print(f"\n已报名分布: {dict(sorted(reg_by_week.items()))}")
print(f"第8周已报 {reg_by_week.get(8,0)} 节，第9周已报 {reg_by_week.get(9,0)} 节")
print(f"→ 第9周还能抢 {cfg.get('max_normal',2) - reg_by_week.get(9,0)} 节（不会被第8周占用）")
