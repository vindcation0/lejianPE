"""服务器不稳定时的应急抢课：高密度重试 + 直接提交。"""
import sys, time, datetime
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE")
import lejian as L

cfg = L.load_json(L.CONFIG, {})
tt = L.load_timetable()
c = L.Client(L.load_json(L.STATE))
cw = L.current_week(cfg, tt)
max_normal = int(cfg.get("max_normal", 2))

deadline = time.time() + 1800
got = 0
no_items = 0

print(f"开始，上限 {max_normal} 节，截止 {datetime.datetime.fromtimestamp(deadline):%H:%M:%S}")

while time.time() < deadline and got < max_normal:
    items = None
    for _ in range(8):
        items = c.activities(req_timeout=5)
        if items:
            break
        no_items += 1
        time.sleep(0.2)

    if not items:
        time.sleep(0.2)
        continue

    picked = L.pick_targets(items, cfg, tt, cw)
    now_ms = time.time() * 1000
    open_now = [p for p in picked
                if int(p[1].get("startRegisterTime") or 0) <= now_ms
                <= int(p[1].get("endRegisterTime") or 1 << 62)]

    if not open_now:
        print(f"  [{datetime.datetime.now():%H:%M:%S}] 拉取成功 {len(items)} 条，暂无符合条件（空转 {no_items}）")
        time.sleep(0.3)
        continue

    for pri, it, why in open_now:
        aid, name = it.get("id"), it.get("name")
        d = datetime.date.fromtimestamp(int(it["startTime"]) / 1000)
        print(f"  [{datetime.datetime.now():%H:%M:%S}] 提交 {name} {L.slot_text(L.activity_slot(it))} {d} 余{L.remaining(it)}")
        ok, reason, raw = c.sign_up(aid)
        print(f"        -> {reason}")
        if ok:
            got += 1
            L.DAY_DONE[d] = L.DAY_DONE.get(d, 0) + 1
            L.EXTRA.append((cw, L.activity_slot(it), "已抢:" + name))
            print(f"  >>> 成功 {got}/{max_normal}")
            break
        time.sleep(0.15)

    time.sleep(0.2)

print(f"结束：抢到 {got} 节")
