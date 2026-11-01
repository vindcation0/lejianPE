"""验证：已抢的课时段会阻止同一时段其它班被再次选中。"""
import sys, datetime
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

cfg = L.load_json(L.CONFIG, {})
tt = L.load_timetable()
cw = L.current_week(tt)
c = L.Client(L.load_json(L.STATE))
items = c.activities()

now = datetime.datetime.now()


def mk(name, dt, minutes, register=False, aid=None):
    st = int(dt.timestamp() * 1000)
    return {
        "id": aid or ("FAKE-" + name),
        "name": name,
        "startTime": st,
        "endTime": st + minutes * 60 * 1000,
        "limitPeopleNumber": 50,
        "registerPeople": 0,
        "isRegister": register,
        "startRegisterTime": int((now - datetime.timedelta(hours=1)).timestamp() * 1000),
        "endRegisterTime": int((now + datetime.timedelta(days=5)).timestamp() * 1000),
    }


day = datetime.datetime(2026, 10, 30)  # 第9周 周五
A = mk("羽毛球 尹远洪", day.replace(hour=16, minute=0), 60, register=True, aid="GOT-A")
B = mk("篮球1 周天一", day.replace(hour=16, minute=30), 60, aid="FAKE-B")   # 与 A 重叠 30 分钟
C = mk("跳绳 亢莉萍", day.replace(hour=17, minute=10), 60, aid="FAKE-C")   # 与 A 不重叠

picked = L.pick_targets(items + [A, B, C], cfg, tt, cw)
ids = {p[1]["id"] for p in picked}

print("场景：已抢 羽毛球 16:00-17:00（第9周），再评估两个候选\n")
print(f"  篮球1 16:30-17:30（重叠30分钟） : {'被排除 ✅' if 'FAKE-B' not in ids else '被选中 ❌'}")
print(f"  跳绳 17:10-18:10（不重叠）      : {'可选 ✅' if 'FAKE-C' in ids else '被误排除 ❌'}")

# 再验证：同一时段的另一节课（不同老师）
D = mk("羽毛球 张三", day.replace(hour=16, minute=0), 60, aid="FAKE-D")
picked2 = L.pick_targets(items + [A, D], cfg, tt, cw)
ids2 = {p[1]["id"] for p in picked2}
print(f"  羽毛球 张三 16:00-17:00（完全重合）: {'被排除 ✅' if 'FAKE-D' not in ids2 else '被选中 ❌'}")
