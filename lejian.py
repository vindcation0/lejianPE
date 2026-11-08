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
import datetime
import json
import os
import re
import shutil
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.instance_lock import InstanceLock, InstanceLockError
from core.state_store import StateStore, RegistrationStatus, StateStoreError
from core.audit_logger import AuditLogger, AuditLogReader
from core.retry_policy import RetryPolicy, HealthState
from core.request_manager import RequestManager, SubmitResult
from core.registration_manager import RegistrationManager
from core.recovery_manager import RecoveryManager
from core.reconciliation import ReconciliationManager
from core.circuit_breaker import CircuitBreaker, CircuitState, is_server_fault
from core.result_reporter import ResultReporter
from core.conflict_checker import (
    ConflictCircuitBreaker,
    ConflictCircuitState,
    ConflictCheckMode,
    ConflictCheckResult,
    LocalConflictChecker,
    ConflictManager
)

BASE = "https://cpes.legym.cn/"
STATE = os.path.join(ROOT, "state.json")
CONFIG = os.path.join(ROOT, "config.json")
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "registration.db")
LOCK_PATH = os.path.join(DATA_DIR, "grab.lock")
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


def save_json(path, obj, backup=True):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if backup and os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except Exception:
            pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    # 写入后完整性回读校验
    with open(tmp, "r", encoding="utf-8") as f:
        json.load(f)
    os.replace(tmp, path)


def _safe_print(text):
    try:
        print(text)
    except UnicodeEncodeError:
        try:
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            safe_text = text.encode(enc, errors="replace").decode(enc, errors="replace")
            print(safe_text)
        except Exception:
            pass
    except Exception:
        pass


def log(msg):
    line = f"[{datetime.datetime.now():%H:%M:%S.%f}"[:-3] + f"] {msg}"
    _safe_print(line)
    os.makedirs(LOGS, exist_ok=True)
    p = os.path.join(LOGS, f"run-{datetime.date.today():%Y%m%d}.log")
    with open(p, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- HTTP
def _request(method, path, params=None, body=None, form=False, auth=True,
             token=None, school_id=None, timeout=15, audit_logger=None,
             correlation_id=None, request_id=None):
    req_id = request_id or f"req_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    t_start = time.time()
    if audit_logger and hasattr(audit_logger, "record_event"):
        try:
            audit_logger.record_event(
                event_type="infrastructure",
                event_name="http_request_started",
                request_id=req_id,
                correlation_id=correlation_id,
                source="client_http",
                details={"method": method, "path": path}
            )
        except Exception:
            pass

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
    status_code = -1
    resp_text = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            status_code, resp_text = r.status, dec(r.read())
    except urllib.error.HTTPError as e:
        status_code, resp_text = e.code, dec(e.read())
    except Exception as e:
        status_code, resp_text = -1, repr(e)

    dur_ms = round((time.time() - t_start) * 1000, 2)
    if audit_logger and hasattr(audit_logger, "record_event"):
        try:
            if status_code == 200:
                audit_logger.record_event(
                    event_type="infrastructure",
                    event_name="http_request_succeeded",
                    request_id=req_id,
                    correlation_id=correlation_id,
                    http_status=200,
                    duration_ms=dur_ms,
                    source="client_http",
                    details={"method": method, "path": path}
                )
            else:
                audit_logger.record_event(
                    event_type="infrastructure",
                    event_name="http_request_failed",
                    request_id=req_id,
                    correlation_id=correlation_id,
                    http_status=status_code,
                    duration_ms=dur_ms,
                    reason=resp_text[:200] if isinstance(resp_text, str) else repr(resp_text)[:200],
                    source="client_http",
                    details={"method": method, "path": path}
                )
        except Exception:
            pass

    return status_code, resp_text


class ActivityList(list):
    """
    带有分页完整性元数据与状态码的活动列表。
    继承自内置 list，保证 100% 向后兼容所有以列表方式消费 activity 的调用。
    """
    def __init__(
        self,
        items=None,
        complete=True,
        total=0,
        fetched=0,
        error=None,
        http_code=None,
        fetched_at=None,
        run_id=None,
        source="getActivityList",
        session_scope_id=None,
        user_id=None
    ):
        super().__init__(items or [])
        self.complete = bool(complete)
        self.total = int(total)
        self.fetched = int(fetched)
        self.error = error
        self.http_code = http_code
        self.fetched_at = fetched_at if fetched_at is not None else time.time()
        self.run_id = run_id
        self.source = source
        self.session_scope_id = session_scope_id
        self.user_id = user_id

    @property
    def items(self):
        return list(self)

    def to_dict(self):
        return {
            "items": list(self),
            "complete": self.complete,
            "total": self.total,
            "fetched": self.fetched,
            "error": self.error,
            "http_code": self.http_code,
            "fetched_at": self.fetched_at,
            "run_id": self.run_id,
            "source": self.source,
            "session_scope_id": self.session_scope_id,
            "user_id": self.user_id
        }


class Client:
    def __init__(self, state, audit_logger=None):
        self.s = state
        self.audit_logger = audit_logger
        self._refresh_lock = threading.RLock()

    def get_session_scope_id(self) -> Optional[str]:
        """
        生成非敏感会话范围标识 (session_scope_id)。
        基于当前认证会话凭据派生截断哈希，绝不包含任何明文 Token 或密钥。
        当 Token 刷新或重新登录时，session_scope_id 自动变化，使旧快照自动失效。
        """
        token = ""
        if isinstance(self.s, dict):
            token = self.s.get("refreshToken") or self.s.get("accessToken") or ""
        if not token:
            return None
        import hashlib
        return "sess_" + hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:16]

    def get_user_scope_id(self) -> Optional[str]:
        """
        生成非敏感用户范围标识 (user_id / user_scope_id)。
        基于学号/账号/userId 派生截断哈希，绝不泄露敏感个人信息。
        """
        u = ""
        if isinstance(self.s, dict):
            u = self.s.get("userId") or self.s.get("studentId") or self.s.get("account") or self.s.get("schoolId") or ""
        if not u:
            return None
        import hashlib
        return "u_" + hashlib.sha256(str(u).encode("utf-8")).hexdigest()[:12]

    def expires_in(self):
        try:
            return int(self.s.get("accessTokenExpires") or 0) / 1000 - time.time()
        except Exception:
            return -1

    def refresh(self):
        with self._refresh_lock:
            rt = self.s.get("refreshToken")
            if not rt:
                raise RuntimeError("state.json 缺 refreshToken，请在 App 里重新登录后跑 tools/make_state.py")
            code, text = _request("GET", "authorization/user/refreshToken",
                                  params={"refreshToken": rt}, auth=False, audit_logger=self.audit_logger)
            try:
                obj = json.loads(text)
            except Exception as e:
                raise RuntimeError(f"刷新失败 HTTP {code}: 响应不是合法 JSON: {text[:200]}") from e

            if code != 200 or not isinstance(obj, dict) or obj.get("code") != 0 or not obj.get("data"):
                raise RuntimeError(f"刷新失败 HTTP {code}: {text[:200]}")

            d = obj["data"]
            if not isinstance(d, dict):
                raise RuntimeError(f"刷新返回的数据结构非法: {type(d)}")

            new_token = d.get("accessToken")
            if not new_token or not str(new_token).strip():
                raise RuntimeError("Token 刷新成功但响应中缺失 accessToken")

            new_rt = d.get("refreshToken") or rt
            try:
                new_exp = int(d.get("accessTokenExpires") or 0)
            except Exception:
                new_exp = 0

            if new_exp <= 0:
                raise RuntimeError("Token 刷新响应中缺失有效的 accessTokenExpires")

            # 构造新的状态副本，持久化成功后再更新内存中的 self.s，避免失败时状态不一致
            new_s = dict(self.s)
            new_s["accessToken"] = str(new_token).strip()
            new_s["refreshToken"] = str(new_rt).strip()
            new_s["accessTokenExpires"] = new_exp

            save_json(STATE, new_s, backup=True)
            self.s.update(new_s)

            log("token 已刷新至 " +
                datetime.datetime.fromtimestamp(new_exp / 1000).strftime("%m-%d %H:%M"))
            return self.s["accessToken"]

    def ensure_token(self, margin=1200):
        if self.expires_in() < margin:
            with self._refresh_lock:
                if self.expires_in() < margin:
                    self.refresh()
        return self.s["accessToken"]

    def post(self, path, body, timeout=15):
        self.ensure_token()
        return _request("POST", path, body=body, timeout=timeout,
                        token=self.s["accessToken"], school_id=self.s["schoolId"],
                        audit_logger=self.audit_logger)

    def get(self, path, params=None, timeout=15):
        self.ensure_token()
        return _request("GET", path, params=params, timeout=timeout,
                        token=self.s["accessToken"], school_id=self.s["schoolId"],
                        audit_logger=self.audit_logger)

    def call(self, method, path, body=None, timeout=15):
        code, text = (self.post(path, body, timeout) if method == "POST"
                      else self.get(path, body, timeout))
        try:
            return code, json.loads(text)
        except Exception:
            return code, {"_raw": text}

    # ---- 活动 ----
    def _note_fail(self, code):
        """只在状态变化时记录，避免刷屏；用于事后还原故障时段"""
        if getattr(self, "_last_fail", None) != code:
            self._last_fail = code
            self._fail_since = datetime.datetime.now()
            log(f"  [列表异常] HTTP {code}，开始记录")
            if self.audit_logger and hasattr(self.audit_logger, "record_event"):
                try:
                    self.audit_logger.record_event(
                        event_type="infrastructure",
                        event_name="list_fault_started",
                        http_status=code,
                        source="client_list",
                        details={"event": "list_fail", "http": code}
                    )
                except Exception:
                    pass
            else:
                try:
                    line = json.dumps({"t": datetime.datetime.now().isoformat(timespec="seconds"),
                                       "event": "list_fail", "http": code},
                                      ensure_ascii=False)
                    os.makedirs(LOGS, exist_ok=True)
                    with open(os.path.join(LOGS, f"events-{datetime.date.today():%Y%m%d}.jsonl"),
                              "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except Exception:
                    pass

    def _note_ok(self):
        if getattr(self, "_last_fail", None) is not None:
            since = getattr(self, "_fail_since", None)
            dur = (datetime.datetime.now() - since).total_seconds() if since else 0
            log(f"  [列表恢复] 中断约 {dur:.0f} 秒")
            self._last_fail = None
            if self.audit_logger and hasattr(self.audit_logger, "record_event"):
                try:
                    self.audit_logger.record_event(
                        event_type="infrastructure",
                        event_name="infrastructure_recovered",
                        source="client_list",
                        details={"event": "list_ok", "outage_sec": round(dur)}
                    )
                except Exception:
                    pass
            else:
                try:
                    line = json.dumps({"t": datetime.datetime.now().isoformat(timespec="seconds"),
                                       "event": "list_ok", "outage_sec": round(dur)},
                                      ensure_ascii=False)
                    with open(os.path.join(LOGS, f"events-{datetime.date.today():%Y%m%d}.jsonl"),
                              "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except Exception:
                    pass

    def activities(self, page_size=200, req_timeout=6, max_retries=0, max_attempts=None):
        """
        一次拉 200 条，分页拉取全部活动并跟踪分页完整性与状态码。

        参数语义规范:
        - max_retries: 失败后的额外重试次数。默认 0（即仅首次请求 1 次，不进行额外重试）。
          - max_retries=0: 允许，总共发起 1 次 HTTP 请求（1 首次 + 0 重试）。
          - max_retries=1: 允许，总共最多发起 2 次 HTTP 请求（1 首次 + 1 重试）。
        - max_attempts: 总尝试次数别名（若显式指定则优先使用：total_attempts = max(1, max_attempts)）。
        """
        if max_attempts is not None:
            total_attempts = max(1, int(max_attempts))
        else:
            total_attempts = 1 + max(0, int(max_retries))

        out, page = [], 1
        total = 0
        complete = True
        err_msg = None
        last_http_code = None

        while page <= 10:
            code, obj = None, None
            for attempt_idx in range(total_attempts):
                code, obj = self.call("POST", "education/app/activity/getActivityList",
                                      {"state": 1, "page": page, "size": page_size,
                                       "semesterId": self.s["semesterId"]},
                                       timeout=req_timeout)
                if code == 200 and isinstance(obj, dict) and obj.get("code") == 0:
                    break
                if attempt_idx < total_attempts - 1:
                    time.sleep(0.25)
            if code != 200 or not isinstance(obj, dict) or obj.get("code") != 0:
                self._note_fail(code)
                complete = False
                last_http_code = code
                err_msg = f"第 {page} 页拉取失败 (HTTP {code})"
                break
            self._note_ok()
            d = obj.get("data") or {}
            items = d.get("items") or []
            total = int(d.get("total") or 0)
            for it in items:
                for k in ("name", "address"):
                    if isinstance(it.get(k), str):
                        it[k] = re.sub(r"\s+", " ", it[k]).strip()
            out.extend(items)
            if len(out) >= total or not items:
                break
            page += 1

        if total > 0 and len(out) < total and complete:
            complete = False
            err_msg = f"未拉取全量活动 (已获取 {len(out)}/{total})"

        return ActivityList(
            out,
            complete=complete,
            total=total,
            fetched=len(out),
            error=err_msg,
            http_code=last_http_code,
            fetched_at=time.time(),
            run_id=getattr(self.audit_logger, "run_id", None) if hasattr(self, "audit_logger") and self.audit_logger else None,
            source="education/app/activity/getActivityList",
            session_scope_id=self.get_session_scope_id(),
            user_id=self.get_user_scope_id()
        )

    def check_conflict(self, activity_id):
        code, obj = self.call("GET", "education/app/activity/checkConflict",
                              {"activityId": activity_id}, timeout=4)
        if code != 200 or not isinstance(obj, dict):
            return None
        return bool(obj.get("data"))

    def check_conflict_detailed(self, activity_id, timeout=4):
        code, obj = self.call("GET", "education/app/activity/checkConflict",
                              {"activityId": activity_id}, timeout=timeout)
        is_conflict = None
        if code == 200 and isinstance(obj, dict):
            is_conflict = bool(obj.get("data"))
        return code, is_conflict, obj

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
        if st <= 0 or en <= st:
            return None
    except Exception:
        return None
    try:
        a = datetime.datetime.fromtimestamp(st / 1000)
        b = datetime.datetime.fromtimestamp(en / 1000)
        return (a.isoweekday(), a.hour * 60 + a.minute, b.hour * 60 + b.minute)
    except Exception:
        return None


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
    import timetable
    tt_obj = timetable.load_timetable_file(TIMETABLE)
    d = tt_obj.to_dict()
    # 兼容可能还在读 courses 的旧脚本
    d["courses"] = [
        {
            "name": s.label,
            "weekday": s.weekday,
            "start": s.start,
            "end": s.end,
            "weeks": s.weeks
        }
        for s in tt_obj.busy_slots
    ]
    return d


def week_of(date_obj, tt):
    """绝对锚点换算：第 1 周周一 = week1_monday，与运行日期无关"""
    d = datetime.date.fromisoformat(tt["week1_monday"])
    return (date_obj - d).days // 7 + 1


def current_week(arg1, arg2=None, today=None):
    """
    根据 timetable 里的 week1_monday 和日期（默认为系统今日）动态推算当前教学周。
    统一复用 week_of()。
    支持调用签名：
      current_week(tt, today=None)
      current_week(cfg, tt, today=None)  # 向下兼容旧调用，但严格不读 cfg['current_week']
    """
    if arg2 is not None:
        if isinstance(arg2, (datetime.date, datetime.datetime)):
            tt = arg1
            today = arg2
        else:
            tt = arg2
    else:
        tt = arg1

    if today is None:
        today = datetime.date.today()
    return week_of(today, tt)


def get_semester_status(tt, today=None):
    """
    返回学期状态元组: (status_text, week_num, total_weeks)
    - 开学前: status_text="尚未开学", week_num < 1
    - 正常教学周: status_text="正常教学周", 1 <= week_num <= total_weeks
    - 学期结束后: status_text="学期已结束", week_num > total_weeks
    """
    if today is None:
        today = datetime.date.today()
    cw = week_of(today, tt)
    tw = int(tt.get("total_weeks", 16))
    if cw < 1:
        return "尚未开学", cw, tw
    elif cw > tw:
        return "学期已结束", cw, tw
    else:
        return "正常教学周", cw, tw


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

    # 支持 Schema v2 的 busy_slots
    for slot in tt.get("busy_slots") or []:
        if any(a <= week <= b for a, b in (slot.get("weeks") or [])):
            try:
                label = slot.get("label") or "课表占用"
                busy.append(((slot["weekday"], hhmm(slot["start"]), hhmm(slot["end"])),
                             f"{label}（第{week}周）"))
            except Exception:
                pass

    # 兼容旧版 courses（若未包含 busy_slots）
    if not tt.get("busy_slots"):
        for c in tt.get("courses") or []:
            if any(a <= week <= b for a, b in (c.get("weeks") or [])):
                try:
                    busy.append(((c["weekday"], hhmm(c["start"]), hhmm(c["end"])),
                                 f"{c.get('name', '课表占用')}（第{week}周）"))
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
    # 候选筛选阶段的临时每日计数，防止同日多个普通活动同时入选
    temp_day_count = dict(day_count)

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
        d = None
        if max_per_day > 0:
            d = datetime.date.fromtimestamp(int(it["startTime"]) / 1000)
            if temp_day_count.get(d, 0) >= max_per_day:
                continue
        for idx, t in enumerate(targets):
            if match_target(it, t):
                label = t.get("keyword") or t.get("teacher") or t.get("time") or "?"
                picked.append((idx + 1, it, f"志愿{idx+1}:{label}"))
                if max_per_day > 0 and d is not None:
                    temp_day_count[d] = temp_day_count.get(d, 0) + 1
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
        try:
            c.refresh()
        except Exception as e:
            log(f"刷新失败: {e}，请在 App 中登录并重新同步")
    else:
        log("有效")


def cmd_list():
    st = load_json(STATE)
    cfg = load_json(CONFIG, {})
    c = Client(st)
    try:
        c.ensure_token()
    except Exception as e:
        log(f"[Token 警告] 无法拉取活动列表 (Token 刷新失败: {e})，请先重新同步 Token。")
        return
    items = c.activities()
    if not getattr(items, "complete", True):
        log(f"  [警告] 当前活动列表获取不完整（仅获取 {getattr(items, 'fetched', len(items))}/{getattr(items, 'total', '?')} 条，{getattr(items, 'error', '')}），仅显示部分结果。")
    tt = load_timetable()
    cw = current_week(tt)
    status_text, _, tw = get_semester_status(tt)
    week_desc = f"第 {cw} 教学周" if 1 <= cw <= tw else f"第 {cw} 周（{status_text}）"
    log(f"活动总数 {len(items)}  |  当前教学周：{week_desc}")

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
    if not st:
        sys.exit("没有 state.json，请先登录并跑 tools/make_state.py 或 tools/resync_from_app.py")

    os.makedirs(DATA_DIR, exist_ok=True)
    lock = InstanceLock(LOCK_PATH)
    if not lock.acquire():
        log("[错误] 已有抢课进程正在运行，拒绝启动第二个抢课实例！")
        return

    try:
        _cmd_grab_inner(cfg, st)
    finally:
        lock.release()


def _cmd_grab_inner(cfg, st):
    test_mode = "test" in sys.argv[2:]
    now_mode = "now" in sys.argv[2:]

    # 初始化可靠性系统与统一审计日志
    state_store = StateStore(DB_PATH)
    audit_logger = AuditLogger(LOGS)
    c = Client(st, audit_logger=audit_logger)
    retry_policy = RetryPolicy(base_delay=0.5, max_delay=8.0)
    circuit_breaker = CircuitBreaker(failure_threshold=3, base_cooldown=2.0, max_cooldown=30.0, audit_logger=audit_logger)
    reporter = ResultReporter(run_date=datetime.date.today(), run_id=audit_logger.run_id)
    reporter.bind_audit_logger(audit_logger)
    reporter.set_local_state_store(state_store)
    req_mgr = RequestManager(c, retry_policy, audit_logger=audit_logger)
    reg_mgr = RegistrationManager(state_store, req_mgr, audit_logger)
    recovery_mgr = RecoveryManager(state_store, req_mgr, audit_logger)
    conflict_breaker = ConflictCircuitBreaker(
        failure_threshold=3,
        base_cooldown=2.0,
        max_cooldown=30.0,
        jitter_max=0.5,
        audit_logger=audit_logger
    )
    local_conflict_checker = LocalConflictChecker()
    conflict_manager = ConflictManager(
        client=c,
        breaker=conflict_breaker,
        local_checker=local_conflict_checker,
        audit_logger=audit_logger,
        cfg=cfg,
        tt=None,
        state_store=state_store
    )

    try:
        c.ensure_token()
    except Exception as e:
        log(f"[Token 警告] 自动刷新 Token 失败: {e}")
        if not test_mode:
            log("请先重新登录或同步有效 Token (使用 tools/resync_from_app.py 或 tools/make_state.py)！")
            return

    # 1. 启动对账与异常恢复
    log("正在执行启动状态对账与未决记录恢复…")
    rec_sum = recovery_mgr.recover_on_startup()
    if rec_sum["recovered_success"]:
        log(f"  [启动恢复] 发现 {len(rec_sum['recovered_success'])} 条历史未决记录已在服务端成功：")
        for r in rec_sum["recovered_success"]:
            log(f"    - {r.get('activity_name')} (id={r.get('activity_id')})")
    if rec_sum["marked_retryable"]:
        log(f"  [启动恢复] 发现 {len(rec_sum['marked_retryable'])} 条历史未决记录经复核未报名，已转为可重试。")
    if rec_sum["remained_unknown"]:
        log(f"  [启动恢复] {len(rec_sum['remained_unknown'])} 条记录暂无法确认状态，保持 UNKNOWN。")
        log("  [安全屏障] 检测到未决记录，安全屏障已冻结新的报名提交，优先执行复核！")

    max_normal = int(cfg.get("max_normal", 2))
    dry = bool(cfg.get("dry_run", True))
    poll = max(int(cfg.get("poll_interval_ms", 500)), 300) / 1000
    retry_cool = 3.0

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
    conflict_manager.tt = tt
    cw = current_week(tt)
    status_text, _, tw = get_semester_status(tt)
    if cw < 1 or cw > tw:
        log(f"[学期状态提示] 当前为第 {cw} 周（{status_text}），第一教学周为 {tt.get('week1_monday')}，学期总周数 {tw} 周。")
    check_conf_cfg = cfg.get("check_conflict", True)
    if check_conf_cfg:
        log("  [配置模式] 冲突检查: 服务端优先 + 智能本地降级 (故障阈值: 3, 冷却基数: 2.0s)")
    else:
        log("  [配置模式] 冲突检查: 服务端检查已禁用 (check_conflict: false)，保持本地安全检查启用")
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

        # 检查服务端故障熔断状态 (C03, C04)
        can_call, breaker_msg = circuit_breaker.can_attempt()
        if not can_call:
            log(f"  [熔断保护] {breaker_msg}，暂停高频轮询")
            time.sleep(poll)
            continue

        try:
            c.ensure_token(300)
            items = c.activities(page_size=200, req_timeout=6, max_retries=0)
        except Exception as e:
            # 记录故障分类与熔断 (C01, C02)
            http_code = getattr(e, "code", -1) if hasattr(e, "code") else -1
            circuit_breaker.record_failure(http_status=http_code, error=e)
            reporter.record_http_failure(http_code)
            retry_policy.record_failure(http_status=http_code)
            backoff_delay = retry_policy.get_backoff_delay()
            log(f"  拉取活动失败: {e} [熔断: {circuit_breaker.state}, 健康: {retry_policy.state}]，退避 {backoff_delay:.2f} 秒")
            retry_policy.sleep_backoff()
            continue

        if not getattr(items, "complete", True):
            http_code = getattr(items, "http_code", None)
            err_msg = getattr(items, "error", "未知原因")

            # 区分故障类型：服务端基础设施故障 vs 客户端异常 vs 纯数据残缺
            if http_code is not None and is_server_fault(http_status=http_code):
                # 记录基础设施故障并累加熔断计数 (C01, C02)
                circuit_breaker.record_failure(http_status=http_code, error=Exception(err_msg))
                reporter.record_http_failure(http_code)
                retry_policy.record_failure(http_status=http_code)
                backoff_delay = retry_policy.get_backoff_delay()
                log(f"  [服务端故障] 活动列表拉取失败 (HTTP {http_code}): {err_msg} "
                    f"[熔断: {circuit_breaker.state}, 失败计数: {circuit_breaker.failure_count}/{circuit_breaker.failure_threshold}]，"
                    f"退避 {backoff_delay:.2f} 秒")
                retry_policy.sleep_backoff()
                continue
            elif http_code == 401:
                # 401 未授权 / Token 失效：不计入基础设施故障，交给 Token 刷新机制处理
                circuit_breaker.record_failure(http_status=401)
                reporter.record_http_failure(401)
                log(f"  [认证异常] 活动列表返回 HTTP 401 (Token 失效)，尝试触发刷新...")
                try:
                    c.refresh()
                except Exception as ref_err:
                    log(f"  [认证异常] Token 刷新失败: {ref_err}")
                time.sleep(1)
                continue
            elif http_code == 403:
                # 403 禁止访问：不计入基础设施故障，明确提示权限异常
                circuit_breaker.record_failure(http_status=403)
                reporter.record_http_failure(403)
                log(f"  [权限异常] 活动列表返回 HTTP 403 (禁止访问/权限不足): {err_msg}")
                time.sleep(poll)
                continue
            elif http_code == 400:
                # 400 请求参数错误：不计入基础设施故障
                circuit_breaker.record_failure(http_status=400)
                reporter.record_http_failure(400)
                log(f"  [参数异常] 活动列表返回 HTTP 400 (请求参数错误): {err_msg}")
                time.sleep(poll)
                continue
            else:
                # 纯数据残缺 (例如分页截断或 HTTP 200 数据不完整)
                circuit_breaker.record_success(is_complete=False)
                if http_code is not None and http_code != 200:
                    reporter.record_http_failure(http_code)
                log(f"  [列表完整性警告] 活动列表不完整 ({err_msg})，"
                    f"仅拉取到 {getattr(items, 'fetched', len(items))}/{getattr(items, 'total', '?')} 条，"
                    f"禁止基于不完整数据进行抢课决策，跳过本次轮询")
                time.sleep(poll)
                continue
        else:
            # 数据完整有效，恢复正常 (C06)
            circuit_breaker.record_success(is_complete=True)
            retry_policy.record_success()

        # ---------------- UNKNOWN 安全屏障：未决状态优先只读复核 ----------------
        unresolved_recs = state_store.get_unresolved_records()
        if unresolved_recs:
            target_unres = unresolved_recs[0]
            unres_aid = target_unres["activity_id"]
            unres_name = target_unres.get("activity_name") or unres_aid
            log(f"  [安全屏障] 检测到存在未决活动 {unres_name} (id={unres_aid})，冻结候选队列，正在执行服务端只读复核…")
            res_ok, res_status, res_msg, remote_item = reg_mgr.resolve_unknown(unres_aid, unres_name)
            if res_status == RegistrationStatus.SUCCESS:
                log(f"  [安全屏障解除] 未决活动 {unres_name} 经复核确认报名成功！")
                reporter.record_attempt(unres_aid, unres_name, "SUCCESS", "复核确认成功")
                it_for_quota = remote_item or next((x for x in items if str(x.get("id")) == str(unres_aid)), None) or target_unres
                if isinstance(it_for_quota, dict) and "startTime" in it_for_quota:
                    try:
                        act_w = week_of_item(it_for_quota)
                        act_d = datetime.date.fromtimestamp(int(it_for_quota["startTime"]) / 1000)
                        if is_special(it_for_quota, cfg):
                            special_done += 1
                        else:
                            already_in_items = any(str(x.get("id")) == str(unres_aid) and x.get("isRegister") for x in items)
                            if not already_in_items:
                                week_done[act_w] = week_done.get(act_w, 0) + 1
                                DAY_DONE[act_d] = DAY_DONE.get(act_d, 0) + 1
                        EXTRA.append((act_w, activity_slot(it_for_quota), "已抢:" + unres_name))
                    except Exception:
                        pass
            elif res_status == RegistrationStatus.RETRYABLE:
                log(f"  [安全屏障解除] 未决活动 {unres_name} 经复核确认未报名，释放潜在名额占用。")
                reporter.record_attempt(unres_aid, unres_name, "FAILED", "复核确认未报名")
            else:
                log(f"  [安全屏障保持] 未决活动 {unres_name} 暂无法确认真实状态，继续保持队列冻结。")
                time.sleep(poll)
                continue

            # 再次检查是否仍有未决记录
            if state_store.has_unresolved():
                time.sleep(poll)
                continue

        # 每个教学周已报的普通活动数（跨周互不干扰）
        reg_by_week = {}
        reg_by_day = {}
        for i in items:
            if not i.get("isRegister") or is_special(i, cfg):
                continue
            try:
                w = week_of_item(i)
                d_i = datetime.date.fromtimestamp(int(i["startTime"]) / 1000)
            except Exception:
                continue
            reg_by_week[w] = reg_by_week.get(w, 0) + 1
            reg_by_day[d_i] = reg_by_day.get(d_i, 0) + 1

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
                    f"已报 {dict(sorted(reg_by_week.items()))}）")
            time.sleep(poll)
            continue

        acted = False
        cap_blocked = 0
        for pri, it, why in open_now:
            aid, name = str(it.get("id")), it.get("name")
            a_slot = activity_slot(it)
            spec = is_special(it, cfg)
            wk = week_of_item(it)

            already = reg_by_week.get(wk, 0) + week_done.get(wk, 0)
            if not spec and already >= max_normal:
                cap_blocked += 1
                continue

            act_date = datetime.date.fromtimestamp(int(it["startTime"]) / 1000)
            already_day = reg_by_day.get(act_date, 0) + DAY_DONE.get(act_date, 0)
            max_per_day = int(cfg.get("max_per_day", 1))
            if not spec and max_per_day > 0 and already_day >= max_per_day:
                log(f"  [每日上限拦截] {name} 所在日期 {act_date} 已达每日上限 ({already_day}/{max_per_day} 节)，跳过提交")
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

            # 规则二次校验回调
            def validate_rules(act_item):
                now_check_ms = time.time() * 1000
                st_reg = int(act_item.get("startRegisterTime") or 0)
                en_reg = int(act_item.get("endRegisterTime") or 0)
                if st_reg > now_check_ms or (en_reg and en_reg < now_check_ms):
                    return False, "活动不在报名时间窗口内"
                if remaining(act_item) == 0:
                    return False, "名额已满"
                if cfg.get("exclude_member", True) and is_member_only(act_item):
                    return False, "会员制活动"
                if not spec:
                    curr_d = datetime.date.fromtimestamp(int(act_item["startTime"]) / 1000)
                    cur_d_tot = reg_by_day.get(curr_d, 0) + DAY_DONE.get(curr_d, 0)
                    if max_per_day > 0 and cur_d_tot >= max_per_day:
                        return False, f"当天普通活动已报满 {max_per_day} 节"
                    cur_w_tot = reg_by_week.get(wk, 0) + week_done.get(wk, 0)
                    if cur_w_tot >= max_normal:
                        return False, f"当周普通活动已报满 {max_normal} 节"
                # Phase 5: 冲突检查与智能降级
                conf_ok, conf_reason = conflict_manager.check_activity_conflict(
                    candidate=act_item,
                    items=items,
                    extra_slots=EXTRA,
                    day_done=DAY_DONE,
                    week_done=week_done,
                    correlation_id=f"corr_grab_{aid}_{int(time.time()*1000)}",
                    run_id=audit_logger.run_id if audit_logger else None,
                    current_week=wk,
                    current_session_scope_id=c.get_session_scope_id(),
                    current_user_id=c.get_user_scope_id()
                )
                if not conf_ok:
                    return False, conf_reason
                return True, "OK"

            ok, final_status, reason, final_item = reg_mgr.register_activity(
                it, validate_rules_cb=validate_rules
            )
            log(f"  报名 {name} -> [{final_status}] {reason}")
            reporter.record_attempt(aid, name, final_status, reason)

            if final_status == RegistrationStatus.UNKNOWN:
                # 核心安全屏障 (U01, U02, U03): 结果未知立即冻结整个候选队列，严禁继续提交下一候选！
                log(f"  [安全屏障触发] 活动 {name} 提交结果未知 (UNKNOWN)，安全屏障立即冻结后续候选队列！")
                acted = True
                break
            elif ok:
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
            log(f"本周 {max_normal} 节名额已满，开始最终全局对账…")
            break

        time.sleep(poll)

    # 抢课循环结束或满额时执行最终全局对账与报告输出
    reconciler = ReconciliationManager(c, cfg, tt, week_of, audit_logger=audit_logger, state_store=state_store)
    report = reconciler.reconcile()
    unresolved_cnt = len(state_store.get_unresolved_records())
    cb_metrics = circuit_breaker.get_metrics()

    # 从统一审计日志回放权威运行指标
    try:
        today_str = datetime.datetime.now().strftime("%Y%m%d")
        evt_path = audit_logger._get_event_path(today_str)
        run_events = AuditLogReader.read_events_from_file(evt_path)
        audit_summary = AuditLogReader.replay(run_events, run_id=audit_logger.run_id)
        reporter.set_audit_summary(audit_summary)
    except Exception as e:
        log(f"[审计回放提示] 读取当前运行事件回放失败: {e}")

    reporter.set_local_state_store(state_store)
    reporter.current_week = cw
    reporter.set_circuit_metrics(
        outage_seconds=cb_metrics["total_outage_seconds"],
        trip_count=cb_metrics["trip_count"]
    )
    reporter.set_reconciliation_result(
        reconciled_items=report.get("items", []) if report.get("success") else [],
        unresolved_count=unresolved_cnt,
        normal_registered=report.get("normal_registered", sum(week_done.values())),
        max_normal=max_normal,
        special_registered=report.get("special_registered", special_done),
        max_per_day=int(cfg.get("max_per_day", 1)),
        weekly_exceeded=bool(report.get("exceeded_weeks")),
        daily_exceeded=bool(report.get("exceeded_days")),
        reconciliation_report=report
    )

    # 控制台多段式统一报告 (5.4, 5.6)
    log("\n" + reporter.format_console_report())

    # 自动保存 Markdown 报告 (5.5, R11, R12)
    try:
        rpt_dir = os.path.join(LOGS, "reports")
        saved_rpt = reporter.save_markdown_report(report_dir=rpt_dir)
        log(f"已自动生成抢课结果报告文件: {saved_rpt}")
    except Exception as e:
        log(f"自动生成 Markdown 报告异常: {e}")


def cmd_login():
    """账号密码安全登录（命令行模式）"""
    from tools.login_cli import main as login_main
    login_main()


def cmd_reinit():
    """重新初始化认证（一键重新抓取、验证并更新凭据与会话）"""
    from core.auth_initializer import AuthInitializer
    AuthInitializer().reinitialize()


def cmd_setup():
    """运行系统配置向导（环境自检与首次初始化，支持 7 种系统状态自适应引导）"""
    from core.config_wizard import ConfigWizard
    wizard = ConfigWizard()
    ok = wizard.run_wizard(interactive=True)
    if not ok:
        sys.exit(1)


def cmd_doctor():
    """系统体检与只读健康诊断 (M4)"""
    from core.system_doctor import SystemDoctor
    is_json = "--json" in sys.argv
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    offline = "--offline" in sys.argv
    doctor = SystemDoctor(root_dir=ROOT)
    report = doctor.run_diagnostics(online_probe=(not offline))
    if is_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.format_report(verbose=verbose))


def cmd_refresh():
    """强制刷新 token（不等过期）"""
    st = load_json(STATE)
    if not st:
        sys.exit("没有 state.json，请先运行: python lejian.py login 或 python tools\\login_cli.py")
    c = Client(st)
    log(f"当前 token 剩余 {int(c.expires_in())} 秒")
    try:
        c.refresh()
        log("刷新成功，已写回 state.json")
    except Exception as e:
        log(f"刷新失败: {e}")
        log("说明 refreshToken 也已失效。建议：")
        log("  1. 运行 python lejian.py login 或双击 run.bat 选 [9] 进行命令行登录（推荐）")
        log("  2. 或在模拟器里重新登录后选 [a] 从 App 重新同步 Token（备用）")


def cmd_names():
    """列出所有非会员课程名，方便填 config.json 的 targets"""
    cfg = load_json(CONFIG, {})
    st = load_json(STATE)
    if not st:
        sys.exit("没有 state.json")
    c = Client(st)
    try:
        c.ensure_token()
    except Exception as e:
        log(f"[Token 警告] 无法拉取课程列表 (Token 刷新失败: {e})，请先重新同步 Token。")
        return
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
    today = datetime.date.today()
    cw = current_week(tt, today=today)
    total_weeks = int(tt.get("total_weeks", 16))
    status_text, _, _ = get_semester_status(tt, today=today)
    if 1 <= cw <= total_weeks:
        cw_display = f"第 {cw} 周"
    else:
        cw_display = f"第 {cw} 周（{status_text}）"

    log("=" * 50)
    log("                 课表与周次")
    log("=" * 50)
    log("")
    log(f"第一教学周周一：{tt.get('week1_monday', '-')}")
    log(f"学期总周数：{total_weeks}")
    log(f"当前日期：{today.isoformat()}")
    log(f"当前教学周：{cw_display}")
    log("")

    reg = {}
    st = load_json(STATE)
    if st:
        c = Client(st)
        try:
            c.ensure_token()
            for i in c.activities():
                if not i.get("isRegister"):
                    continue
                a = activity_slot(i)
                if a is None:
                    continue
                w = week_of(datetime.date.fromtimestamp(int(i["startTime"]) / 1000), tt)
                reg.setdefault(w, []).append((a, "已抢:" + (i.get("name") or "")))
        except Exception as e:
            log(f"（未能联网拉取已报活动: {e}，仅展示本地课表）")

    # 当前周
    log("---------------- 当前周 ----------------")
    busy_curr = busy_for_week(cfg, tt, cw) + reg.get(cw, [])
    sorted_curr = sorted(busy_curr, key=lambda x: (x[0][0], x[0][1]))
    log(f"第 {cw} 周占用 {len(sorted_curr)} 个时段：")
    for s, why in sorted_curr:
        log(f"  {slot_text(s)} <- {why}")
    log("")

    # 下一周
    next_w = cw + 1
    log("---------------- 下一周 ----------------")
    busy_next = busy_for_week(cfg, tt, next_w) + reg.get(next_w, [])
    sorted_next = sorted(busy_next, key=lambda x: (x[0][0], x[0][1]))
    log(f"第 {next_w} 周占用 {len(sorted_next)} 个时段：")
    for s, why in sorted_next:
        log(f"  {slot_text(s)} <- {why}")
    log("")
    log("=" * 50)


def cmd_editor():
    """启动本地可视化课表编辑器"""
    from tools.editor_server import run_editor
    run_editor()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"status": cmd_status, "list": cmd_list, "grab": cmd_grab,
     "timetable": cmd_timetable, "names": cmd_names,
     "refresh": cmd_refresh, "editor": cmd_editor,
     "login": cmd_login, "reinit": cmd_reinit,
     "setup": cmd_setup, "wizard": cmd_setup,
     "doctor": cmd_doctor, "--doctor": cmd_doctor}.get(cmd, cmd_status)()


if __name__ == "__main__":
    main()
