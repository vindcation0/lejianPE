"""
乐健体育 · 课外活动抢课工具
===========================
抢的是【课外】里的活动（羽毛球/篮球/足球…），不是体育主课程。

    python lejian.py status    查看 token 状态
    python lejian.py list      列出当前可抢活动（按规则过滤后）
    python lejian.py grab      执行抢课（dry_run 控制是否真提交）
    python lejian.py grab test 20 秒自测

规则：
  - 会员制活动一律跳过（名字含「会员制」或地址含「需办会员」）
  - 与 busy_slots 时间重叠的活动跳过
  - 每周最多 max_normal 节
  - 体质健康测试类不占名额（special）
依赖：仅标准库。
"""
import json, os, re, ssl, sys, time, datetime, urllib.request, urllib.error, urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = "https://cpes.legym.cn/"
STATE = os.path.join(ROOT, "state.json")
CONFIG = os.path.join(ROOT, "config.json")
CAPTURES = os.path.join(ROOT, "captures")
LOGS = os.path.join(ROOT, "logs")
SSL_CTX = ssl.create_default_context()


# ---------------------------------------------------------------- 基础设施
def dec(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def log(msg):
    line = f"[{datetime.datetime.now():%H:%M:%S.%f}"[:-3] + f"] {msg}"
    print(line)
    os.makedirs(LOGS, exist_ok=True)
    p = os.path.join(LOGS, f"run-{datetime.date.today():%Y%m%d}.log")
    with open(p, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- HTTP
def _request(method, path, params=None, body=None, form=False, auth=True,
             token=None, school_id=None):
    url = path if path.startswith("http") else BASE + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

    data, headers = None, {"User-Agent": "okhttp/3.12.1"}
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    if auth:
        headers["Authorization"] = "Bearer " + (token or "")
        headers["Organization"] = school_id or ""

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
            return r.status, dec(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dec(e.read())
    except Exception as e:
        return -1, repr(e)


class Client:
    def __init__(self, state):
        self.s = state

    def expires_in(self):
        try:
            return int(self.s.get("accessTokenExpires") or 0) / 1000 - time.time()
        except Exception:
            return -1

    def refresh(self):
        rt = self.s.get("refreshToken")
        if not rt:
            raise RuntimeError("state.json 缺 refreshToken，请在 App 里重新登录后跑 tools/make_state.py")
        code, text = _request("GET", "authorization/user/refreshToken",
                              params={"refreshToken": rt}, auth=False)
        obj = json.loads(text)
        if code != 200 or obj.get("code") != 0 or not obj.get("data"):
            raise RuntimeError(f"刷新失败 HTTP {code}: {text[:200]}")
        d = obj["data"]
        self.s["accessToken"] = d["accessToken"]
        self.s["refreshToken"] = d.get("refreshToken") or rt
        self.s["accessTokenExpires"] = d.get("accessTokenExpires")
        save_json(STATE, self.s)
        log("token 已刷新至 " +
            datetime.datetime.fromtimestamp(self.s["accessTokenExpires"] / 1000).strftime("%m-%d %H:%M"))
        return self.s["accessToken"]

    def ensure_token(self, margin=1200):
        if self.expires_in() < margin:
            self.refresh()
        return self.s["accessToken"]

    def post(self, path, body):
        self.ensure_token()
        return _request("POST", path, body=body,
                        token=self.s["accessToken"], school_id=self.s["schoolId"])

    def get(self, path, params=None):
        self.ensure_token()
        return _request("GET", path, params=params,
                        token=self.s["accessToken"], school_id=self.s["schoolId"])

    def call(self, method, path, body=None):
        code, text = (self.post(path, body) if method == "POST"
                      else self.get(path, body))
        try:
            return code, json.loads(text)
        except Exception:
            return code, {"_raw": text}

    # ---- 活动 ----
    def activities(self, page_size=100):
        """拉全部报名中的活动（分页）"""
        out, page = [], 1
        while page <= 10:
            code, obj = self.call("POST", "education/app/activity/getActivityList",
                                  {"state": 1, "page": page, "size": page_size,
                                   "semesterId": self.s["semesterId"]})
            if code != 200 or not isinstance(obj, dict):
                break
            d = obj.get("data") or {}
            items = d.get("items") or []
            for it in items:
                for k in ("name", "address"):
                    if isinstance(it.get(k), str):
                        it[k] = re.sub(r"\s+", " ", it[k]).strip()
            out.extend(items)
            total = d.get("total") or 0
            if len(out) >= total or not items:
                break
            page += 1
        return out

    def check_conflict(self, activity_id):
        code, obj = self.call("GET", "education/app/activity/checkConflict",
                              {"activityId": activity_id})
        if code != 200 or not isinstance(obj, dict):
            return None
        return bool(obj.get("data"))

    def sign_up(self, activity_id):
        code, obj = self.call("POST", "education/app/activity/signUp",
                              {"activityId": activity_id})
        if not isinstance(obj, dict):
            return False, f"HTTP {code}", obj
        if obj.get("code") == 0 and isinstance(obj.get("data"), dict):
            d = obj["data"]
            if d.get("success"):
                return True, "报名成功", obj
            return False, d.get("reason") or "报名失败", obj
        return False, obj.get("message") or f"HTTP {code}", obj


# ---------------------------------------------------------------- 时间
CN_WEEK = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
WEEK_NAME = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
RE_SLOT = re.compile(r"周([一二三四五六日天])\s*(\d{1,2}):(\d{2})\s*[~\-–—至]\s*(\d{1,2}):(\d{2})")


def parse_slot(text):
    """'周一 14:00-15:35' / '周一14:00~15:35' → (weekday, start_min, end_min)"""
    if not isinstance(text, str):
        return None
    m = RE_SLOT.search(text)
    if not m:
        return None
    return (CN_WEEK[m.group(1)],
            int(m.group(2)) * 60 + int(m.group(3)),
            int(m.group(4)) * 60 + int(m.group(5)))


def slot_text(s):
    return f"{WEEK_NAME[s[0]]} {s[1]//60:02d}:{s[1]%60:02d}~{s[2]//60:02d}:{s[2]%60:02d}"


def overlaps(a, b):
    return a[0] == b[0] and a[1] < b[2] and b[1] < a[2]


def activity_slot(it):
    """把活动的 startTime/endTime（毫秒时间戳）转成 (weekday, start_min, end_min)"""
    try:
        st = int(it.get("startTime"))
        en = int(it.get("endTime"))
    except Exception:
        return None
    a = datetime.datetime.fromtimestamp(st / 1000)
    b = datetime.datetime.fromtimestamp(en / 1000)
    return (a.isoweekday(), a.hour * 60 + a.minute, b.hour * 60 + b.minute)


def fmt_ms(v, f="%m-%d %H:%M"):
    try:
        return datetime.datetime.fromtimestamp(int(v) / 1000).strftime(f)
    except Exception:
        return "-"


# ---------------------------------------------------------------- 规则
def is_member_only(it):
    name = it.get("name") or ""
    addr = it.get("address") or ""
    return ("会员制" in name) or ("需办会员" in addr) or ("会员" in addr)


def is_special(it, cfg):
    name = it.get("name") or ""
    return any(k in name for k in cfg.get("special") or [])


def remaining(it):
    try:
        return int(it.get("limitPeopleNumber") or 0) - int(it.get("registerPeople") or 0)
    except Exception:
        return -1


TIMETABLE = os.path.join(ROOT, "timetable.json")
EXTRA = []          # 本次运行中已抢到的时段：(week, slot, why)
DAY_DONE = {}       # 本次运行中每天已抢节数：date -> n


def load_timetable():
    tt = load_json(TIMETABLE) or {"current_week": 8, "courses": []}
    if not tt.get("week1_monday"):
        cw = int(tt.get("current_week") or 8)
        today = datetime.date.today()
        monday = today - datetime.timedelta(days=today.weekday())
        tt["week1_monday"] = (monday - datetime.timedelta(days=7 * (cw - 1))).isoformat()
    return tt


def current_week(cfg, tt):
    return int(cfg.get("current_week") or tt.get("current_week") or 8)


def week_of(date_obj, tt):
    """绝对锚点换算：第 1 周周一 = week1_monday，与运行日期无关"""
    d = datetime.date.fromisoformat(tt["week1_monday"])
    return (date_obj - d).days // 7 + 1


def hhmm(t):
    h, m = str(t).split(":")
    return int(h) * 60 + int(m)


def busy_for_week(cfg, tt, week):
    """某个教学周里被占用的时段"""
    busy = []
    for item in cfg.get("busy_slots") or []:
        s = parse_slot(item)
        if s:
            busy.append((s, item))
        else:
            log(f"  [警告] busy_slots 无法解析: {item!r}")
    for c in tt.get("courses") or []:
        if any(a <= week <= b for a, b in (c.get("weeks") or [])):
            try:
                busy.append(((c["weekday"], hhmm(c["start"]), hhmm(c["end"])),
                             f"{c['name']}（第{week}周）"))
            except Exception:
                pass
    for wk, s, why in EXTRA:
        if wk == week:
            busy.append((s, why))
    return busy


def norm_targets(cfg):
    """把 targets 规范成 [{keyword, teacher, time}]，兼容旧的 sports 写法"""
    raw = cfg.get("targets")
    if raw is None:
        raw = cfg.get("sports") or []
    out = []
    for t in raw:
        if isinstance(t, str):
            out.append({"keyword": t, "teacher": None, "time": None})
        elif isinstance(t, dict):
            out.append({
                "keyword": t.get("keyword") or t.get("name") or "",
                "teacher": t.get("teacher"),
                "time": t.get("time"),
            })
    return out


def match_target(it, t):
    """判断活动是否命中某条志愿"""
    name = it.get("name") or ""
    kw = t.get("keyword")
    if kw and kw not in name:
        return False
    if t.get("teacher") and t["teacher"] not in name:
        return False
    if t.get("time"):
        a = activity_slot(it)
        if a is None:
            return False
        if f"{a[1]//60:02d}:{a[1]%60:02d}" != str(t["time"]).strip():
            return False
    return True


def pick_targets(items, cfg, tt, cw):
    """按规则筛选，返回 [(优先级, item, 原因)]"""
    targets = norm_targets(cfg)
    include_special = bool(cfg.get("special"))
    cache, picked = {}, []

    # 已报名的活动也要占用时间——否则同周同一时段的其它班会被误判为可抢
    reg_slots = {}
    for i in items:
        if not i.get("isRegister"):
            continue
        a = activity_slot(i)
        if a is None:
            continue
        try:
            w = week_of(datetime.date.fromtimestamp(int(i["startTime"]) / 1000), tt)
        except Exception:
            continue
        reg_slots.setdefault(w, []).append((a, "已报:" + (i.get("name") or "")))

    # 每天最多几节（学校规则：一天只能抢一节）
    max_per_day = int(cfg.get("max_per_day", 1))
    day_count = {}
    for i in items:
        if not i.get("isRegister") or is_special(i, cfg):
            continue
        try:
            d = datetime.date.fromtimestamp(int(i["startTime"]) / 1000)
        except Exception:
            continue
        day_count[d] = day_count.get(d, 0) + 1
    for d, n in DAY_DONE.items():
        day_count[d] = day_count.get(d, 0) + n

    for it in items:
        name = it.get("name") or ""
        if it.get("isRegister"):
            continue
        if cfg.get("exclude_member", True) and is_member_only(it):
            continue
        if remaining(it) == 0:
            continue
        a_slot = activity_slot(it)
        if a_slot is None:
            continue

        wk = week_of(datetime.date.fromtimestamp(int(it["startTime"]) / 1000), tt)
        if wk not in cache:
            cache[wk] = busy_for_week(cfg, tt, wk) + reg_slots.get(wk, [])
        if any(overlaps(a_slot, b) for b, _ in cache[wk]):
            continue

        if include_special and is_special(it, cfg):
            picked.append((0, it, "特殊"))
            continue
        if max_per_day > 0:
            d = datetime.date.fromtimestamp(int(it["startTime"]) / 1000)
            if day_count.get(d, 0) >= max_per_day:
                continue
        for idx, t in enumerate(targets):
            if match_target(it, t):
                label = t.get("keyword") or t.get("teacher") or t.get("time") or "?"
                picked.append((idx + 1, it, f"志愿{idx+1}:{label}"))
                break

    picked.sort(key=lambda x: (x[0], activity_slot(x[1])[1]))
    return picked


# ---------------------------------------------------------------- 命令
def cmd_status():
    st = load_json(STATE)
    if not st:
        sys.exit("没有 state.json，先跑: python tools/make_state.py")
    c = Client(st)
    sec = c.expires_in()
    log(f"token 剩余 {int(sec)} 秒 (约 {sec/3600:.1f} 小时)")
    if sec < 0:
        log("已过期，尝试刷新…")
        c.refresh()
    else:
        log("有效")


def cmd_list():
    st = load_json(STATE)
    cfg = load_json(CONFIG, {})
    c = Client(st)
    c.ensure_token()
    items = c.activities()
    tt = load_timetable()
    cw = current_week(cfg, tt)
    busy = busy_for_week(cfg, tt, cw)
    log(f"活动总数 {len(items)}  |  第 {cw} 教学周，已占用 {len(busy)} 个时段")
    for s, why in busy:
        log(f"  {slot_text(s)}  <- {why}")

    by_week = {}
    for i in items:
        if not i.get("isRegister") or is_special(i, cfg):
            continue
        w = week_of(datetime.date.fromtimestamp(int(i["startTime"]) / 1000), tt)
        by_week.setdefault(w, []).append(i)

    reg_special = [i for i in items if i.get("isRegister") and is_special(i, cfg)]
    cap = cfg.get("max_normal", 2)
    if by_week:
        for w in sorted(by_week):
            log(f"第{w}教学周 已报 {len(by_week[w])}/{cap} 节：")
            for i in by_week[w]:
                log(f"    {i.get('name')}  {fmt_ms(i.get('startTime'))}")
    else:
        log("没有已报的普通活动")
    if reg_special:
        log(f"额外（不占名额）已报 {len(reg_special)} 节：")
        for i in reg_special:
            log(f"    {i.get('name')}  {fmt_ms(i.get('startTime'))}")

    picked = pick_targets(items, cfg, tt, cw)
    log(f"符合条件可抢 {len(picked)} 个：")
    for pri, it, why in picked[:30]:
        a = activity_slot(it)
        log(f"  [{pri}] {it.get('name')}  {slot_text(a)}  "
            f"余{remaining(it)}/{it.get('limitPeopleNumber')}  "
            f"开抢{fmt_ms(it.get('startRegisterTime'))}  {why}")


def next_sunday(hh, mm, ss=0):
    now = datetime.datetime.now()
    days = (6 - now.weekday()) % 7
    target = (now + datetime.timedelta(days=days)).replace(
        hour=hh, minute=mm, second=ss, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=7)
    return target


def cmd_grab():
    cfg = load_json(CONFIG, {})
    st = load_json(STATE)
    c = Client(st)
    c.ensure_token()

    max_normal = int(cfg.get("max_normal", 2))
    dry = bool(cfg.get("dry_run", True))
    poll = max(int(cfg.get("poll_interval_ms", 500)), 300) / 1000
    retry_cool = 3.0
    test_mode = "test" in sys.argv[2:]
    now_mode = "now" in sys.argv[2:]

    if test_mode:
        dry = True          # 自检模式绝不提交
        watch_until = datetime.datetime.now() + datetime.timedelta(seconds=20)
        log("[自检模式] 立即开始，20 秒后结束，不会提交任何内容")
    elif now_mode:
        secs = int(cfg.get("now_seconds", 120))
        watch_until = datetime.datetime.now() + datetime.timedelta(seconds=secs)
        log(f"[立即模式] 不等周日，持续 {secs} 秒")
    else:
        def hms(k, d):
            return [int(x) for x in str(cfg.get(k, d)).split(":")]
        watch_from = next_sunday(*hms("watch_from", "13:59:00"))
        watch_until = next_sunday(*hms("watch_until", "16:00:00"))
        log(f"观察窗口 {watch_from:%m-%d %H:%M:%S} → {watch_until:%H:%M:%S}")
        while datetime.datetime.now() < watch_from:
            left = (watch_from - datetime.datetime.now()).total_seconds()
            if left > 60:
                log(f"距开始还有 {int(left)} 秒")
                time.sleep(min(left - 30, 60))
            else:
                time.sleep(max(min(left, 1), 0.05))

    tt = load_timetable()
    cw = current_week(cfg, tt)
    busy = busy_for_week(cfg, tt, cw)
    log(f"第 {cw} 教学周，已占用 {len(busy)} 个时段：")
    for s, why in busy:
        log(f"  {slot_text(s)}  <- {why}")

    week_done = {}
    special_done = 0
    last_try = {}
    polls = 0

    def week_of_item(it):
        return week_of(datetime.date.fromtimestamp(int(it["startTime"]) / 1000), tt)

    while datetime.datetime.now() < watch_until:
        polls += 1
        try:
            c.ensure_token(300)
            items = c.activities()
        except Exception as e:
            log(f"  拉取活动失败: {e}")
            time.sleep(poll)
            continue

        # 每个教学周已报的普通活动数（跨周互不干扰）
        reg_by_week = {}
        for i in items:
            if not i.get("isRegister") or is_special(i, cfg):
                continue
            try:
                w = week_of_item(i)
            except Exception:
                continue
            reg_by_week[w] = reg_by_week.get(w, 0) + 1

        picked = pick_targets(items, cfg, tt, cw)
        now_ms = time.time() * 1000

        open_now = []
        for pri, it, why in picked:
            if int(it.get("startRegisterTime") or 0) > now_ms:
                continue
            if int(it.get("endRegisterTime") or 0) < now_ms:
                continue
            open_now.append((pri, it, why))

        if not open_now:
            if polls % 20 == 0:
                log(f"  ...轮询中（第 {polls} 次，活动 {len(items)} 条，"
                    f"已报 {dict(sorted(reg_by_week.items()))}")
            time.sleep(poll)
            continue

        acted = False
        cap_blocked = 0
        for pri, it, why in open_now:
            aid, name = it.get("id"), it.get("name")
            a_slot = activity_slot(it)
            spec = is_special(it, cfg)
            wk = week_of_item(it)

            already = reg_by_week.get(wk, 0) + week_done.get(wk, 0)
            if not spec and already >= max_normal:
                cap_blocked += 1
                continue
            if time.time() - last_try.get(aid, 0) < retry_cool:
                continue

            if dry:
                log(f"[dry] 可抢: {name}  {slot_text(a_slot)}  第{wk}周  "
                    f"余{remaining(it)}  {why}")
                acted = True
                continue

            last_try[aid] = time.time()
            acted = True

            try:
                if c.check_conflict(aid):
                    log(f"  服务端判定冲突，跳过 {name}")
                    continue
            except Exception:
                pass

            ok, reason, _ = c.sign_up(aid)
            log(f"  报名 {name} -> {reason}")
            if ok:
                d = datetime.date.fromtimestamp(int(it["startTime"]) / 1000)
                if spec:
                    special_done += 1
                else:
                    week_done[wk] = week_done.get(wk, 0) + 1
                    DAY_DONE[d] = DAY_DONE.get(d, 0) + 1
                EXTRA.append((wk, a_slot, "已抢:" + name))
                now_n = reg_by_week.get(wk, 0) + week_done.get(wk, 0)
                log(f">>> 成功！第{wk}周 已报 {now_n}/{max_normal}，"
                    f"{d:%m-%d} 当天第 {DAY_DONE.get(d, 1)} 节，额外 {special_done}")

        if dry and acted:
            log("以上为 dry_run 结果，未真正提交")
            return

        if cap_blocked == len(open_now) and not any(is_special(p[1], cfg) for p in open_now):
            log(f"本周 {max_normal} 节名额已满，退出")
            return

        time.sleep(poll)

    log(f"观察窗口结束。本次抢到 {sum(week_done.values())} 节，额外 {special_done} 节")


def cmd_refresh():
    """强制刷新 token（不等过期）"""
    st = load_json(STATE)
    if not st:
        sys.exit("没有 state.json，先跑: python tools\\resync_from_app.py")
    c = Client(st)
    log(f"当前 token 剩余 {int(c.expires_in())} 秒")
    try:
        c.refresh()
        log("刷新成功，已写回 state.json")
    except Exception as e:
        log(f"刷新失败: {e}")
        log("说明 refreshToken 也失效了。需要：")
        log("  1. 在模拟器里重新登录乐健体育")
        log("  2. 双击 run.bat 选 [8] 从 App 重新同步")


def cmd_names():
    """列出所有非会员课程名，方便填 config.json 的 targets"""
    cfg = load_json(CONFIG, {})
    c = Client(load_json(STATE))
    c.ensure_token()
    items = c.activities()
    normal = [i for i in items if not is_member_only(i)]
    groups = {}
    for it in normal:
        groups.setdefault(it.get("name") or "?", []).append(it)

    log(f"非会员活动 {len(normal)} 条，共 {len(groups)} 个课程名")
    log("把想抢的名字填进 config.json 的 targets，按顺序即优先级")
    log("")
    for name in sorted(groups, key=lambda n: (-len(groups[n]), n)):
        rows = groups[name]
        slots = []
        for r in rows[:4]:
            a = activity_slot(r)
            if a:
                slots.append(f"{slot_text(a)[:11]} 余{remaining(r)}")
        log(f"  {name}  [{len(rows)}场]  {' | '.join(slots)}")


def cmd_timetable():
    cfg = load_json(CONFIG, {})
    tt = load_timetable()
    cw = current_week(cfg, tt)
    log(f"当前第 {cw} 教学周（第 1 周周一 = {tt['week1_monday']}，共 {len(tt.get('courses') or [])} 门课）")

    c = Client(load_json(STATE))
    c.ensure_token()
    reg = {}
    for i in c.activities():
        if not i.get("isRegister"):
            continue
        a = activity_slot(i)
        if a is None:
            continue
        w = week_of(datetime.date.fromtimestamp(int(i["startTime"]) / 1000), tt)
        reg.setdefault(w, []).append((a, "已抢:" + (i.get("name") or "")))

    for w in (cw, cw + 1):
        busy = busy_for_week(cfg, tt, w) + reg.get(w, [])
        log(f"第{w}周占用 {len(busy)} 个时段：")
        for s, why in sorted(busy, key=lambda x: (x[0][0], x[0][1])):
            log(f"  {slot_text(s)}  <- {why}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"status": cmd_status, "list": cmd_list, "grab": cmd_grab,
     "timetable": cmd_timetable, "names": cmd_names,
     "refresh": cmd_refresh}.get(cmd, cmd_status)()


if __name__ == "__main__":
    main()
