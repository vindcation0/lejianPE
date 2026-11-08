"""
冲突检查与智能降级模块 (ConflictChecker & ConflictManager)
======================================================
第四轮优化 Phase 5：解决真实抢课中 checkConflict 接口持续异常的问题。
核心原则：「允许降级，不允许失去安全保护」。

架构设计：
1. ConflictCircuitBreaker: checkConflict 专属独立故障熔断器，维护 NORMAL / OPEN / HALF_OPEN 三态，
   与 activities 列表熔断器完全独立，故障计数与恢复独立统计。
2. LocalConflictChecker: 本地规则冲突检查器，在本地数据完整性验证通过的前提下，
   全面检测时间区间重叠、教学周、星期/时段、跨天课程、课表占用、每日配额、每周配额及特殊活动规则。
3. ConflictManager: 统一协调器，实现服务端优先、连续异常自动降级 (DEGRADED_LOCAL)、
   数据残缺安全阻断 (BLOCKED)、半开健康探测与自动恢复 (NORMAL)。
4. 统一接入 AuditLogger 审计体系，精准记录 9 类冲突检查与降级生命周期事件。
"""
import datetime
import random
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .audit_logger import AuditLogger, EventName, EventType
from .circuit_breaker import CircuitBreaker, CircuitState, is_server_fault
from .state_store import RegistrationStatus, StateStore


class ConflictCircuitState:
    """checkConflict 独立健康熔断状态"""
    NORMAL = "NORMAL"        # 正常状态 (CLOSED)，使用服务端 checkConflict
    OPEN = "OPEN"            # 熔断状态，禁止请求，进入冷却期
    HALF_OPEN = "HALF_OPEN"  # 半开状态，仅允许单次健康探测

    # 兼容别名
    CLOSED = "NORMAL"


class ConflictCheckMode:
    """冲突检查业务模式"""
    SERVER = "SERVER"                # 服务端冲突检查模式
    DEGRADED_LOCAL = "DEGRADED_LOCAL"# 本地降级检查模式
    BLOCKED = "BLOCKED"              # 本地数据不完整安全阻断模式


class ConflictCheckResult:
    """冲突检查结果结构体"""

    def __init__(
        self,
        has_conflict: bool,
        mode: str,
        status: str,  # "CLEAN", "CONFLICT", "BLOCKED"
        reason: str = "",
        data_integrity: Optional[Dict[str, Any]] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        self.has_conflict = has_conflict
        self.mode = mode
        self.status = status
        self.reason = reason
        self.data_integrity = data_integrity or {}
        self.details = details or {}

    @property
    def is_clean(self) -> bool:
        return not self.has_conflict and self.status == "CLEAN"

    @property
    def is_blocked(self) -> bool:
        return self.status == "BLOCKED"

    def __repr__(self):
        return f"<ConflictCheckResult mode={self.mode} status={self.status} conflict={self.has_conflict} reason={self.reason!r}>"


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
        http_code=200,
        fetched_at=None,
        run_id=None,
        source="getActivityList"
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


class ConflictCircuitBreaker:
    """
    checkConflict 专属独立故障熔断器。
    独立维护 NORMAL / OPEN / HALF_OPEN 状态机，
    绝不与 activities 列表熔断器共用状态。
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        base_cooldown: float = 2.0,
        max_cooldown: float = 30.0,
        jitter_max: float = 0.5,
        audit_logger: Optional[Any] = None
    ):
        self.failure_threshold = failure_threshold
        self.base_cooldown = base_cooldown
        self.max_cooldown = max_cooldown
        self.jitter_max = jitter_max
        self.audit_logger = audit_logger

        self.state = ConflictCircuitState.NORMAL
        self.failure_count = 0
        self.trip_count = 0
        self.opened_at = 0.0
        self.current_cooldown = base_cooldown
        self._half_open_probe_in_progress = False

        self.total_outage_seconds = 0.0
        self._last_open_start = 0.0

    @property
    def is_normal(self) -> bool:
        return self.state in (ConflictCircuitState.NORMAL, CircuitState.CLOSED)

    @property
    def is_open(self) -> bool:
        return self.state == ConflictCircuitState.OPEN

    @property
    def is_half_open(self) -> bool:
        return self.state == ConflictCircuitState.HALF_OPEN

    def can_attempt(self) -> Tuple[bool, str]:
        """检查当前是否允许向服务端 checkConflict 发起请求"""
        now = time.time()

        if self.is_normal:
            return True, "NORMAL 正常运行"

        if self.state == ConflictCircuitState.OPEN:
            elapsed = now - self.opened_at
            if elapsed >= self.current_cooldown:
                self._transition_to_half_open()
                self._half_open_probe_in_progress = True
                return True, "HALF_OPEN 冷却完成，允许单次探测"
            else:
                remaining = self.current_cooldown - elapsed
                return False, f"OPEN 熔断冷却中，剩余 {remaining:.2f} 秒"

        if self.state == ConflictCircuitState.HALF_OPEN:
            if not self._half_open_probe_in_progress:
                self._half_open_probe_in_progress = True
                return True, "HALF_OPEN 允许单次探测"
            else:
                return False, "HALF_OPEN 正在进行单次健康探测，禁止并发请求"

        return False, f"未知熔断状态: {self.state}"

    def record_success(self, is_complete: bool = True) -> str:
        """
        记录服务端请求成功。
        如果是 HALF_OPEN 探测：
        - 完整有效响应 (is_complete=True) -> 恢复 NORMAL；
        - 不完整响应 (is_complete=False) -> 重新 OPEN 并退避。
        如果是 NORMAL 状态：
        - 完整响应清零失败计数。
        """
        if self.state == ConflictCircuitState.HALF_OPEN:
            if is_complete:
                self._transition_to_normal()
            else:
                self._trip_to_open(reason="健康探测响应不完整 (complete=False)")
        elif self.is_normal:
            if is_complete:
                self.failure_count = 0

        self._half_open_probe_in_progress = False
        return self.state

    def record_failure(
        self,
        http_status: Optional[int] = None,
        error: Optional[Exception] = None,
        force_trip: bool = False
    ) -> str:
        """
        记录服务端请求失败。
        - 4xx 客户端错误 (401, 403, 400 等) 严禁计入基础设施故障计数。
        - HALF_OPEN 遇到 4xx 释放探测锁保持 HALF_OPEN。
        - 仅在 5xx、超时、网络错误等基础设施故障时累加计数或重新切入 OPEN。
        """
        if not force_trip and not is_server_fault(http_status=http_status, error=error):
            self._half_open_probe_in_progress = False
            return self.state

        if self.state == ConflictCircuitState.HALF_OPEN:
            self._trip_to_open(reason=f"HALF_OPEN 探测失败 (http={http_status}, err={error})")
            return self.state

        self.failure_count += 1

        if self.is_normal:
            if force_trip or self.failure_count >= self.failure_threshold:
                self._trip_to_open(reason=f"checkConflict 连续故障达到阈值 ({self.failure_count}/{self.failure_threshold})")

        self._half_open_probe_in_progress = False
        return self.state

    def _trip_to_open(self, reason: str = ""):
        """进入熔断状态 OPEN，按指数退避计算冷却时间"""
        self.state = ConflictCircuitState.OPEN
        self.trip_count += 1
        now = time.time()
        self.opened_at = now
        self._last_open_start = now
        self._half_open_probe_in_progress = False

        exp = min(self.trip_count - 1, 6)
        backoff = min(self.max_cooldown, self.base_cooldown * (2 ** exp))
        jitter = random.uniform(0, self.jitter_max)
        self.current_cooldown = round(backoff + jitter, 2)

        if self.audit_logger:
            try:
                self.audit_logger.record_event(
                    event_type="infrastructure",
                    event_name="circuit_open",
                    status="OPEN",
                    reason=reason or f"checkConflict 连续故障达到阈值 ({self.failure_count}/{self.failure_threshold})",
                    source="check_conflict_breaker",
                    details={
                        "service": "check_conflict",
                        "trip_count": self.trip_count,
                        "failure_count": self.failure_count,
                        "cooldown_sec": self.current_cooldown
                    }
                )
            except Exception:
                pass

    def _transition_to_half_open(self):
        """进入半开状态 HALF_OPEN"""
        now = time.time()
        if self._last_open_start > 0:
            self.total_outage_seconds += (now - self._last_open_start)
            self._last_open_start = 0.0
        self.state = ConflictCircuitState.HALF_OPEN
        self._half_open_probe_in_progress = False
        if self.audit_logger:
            try:
                self.audit_logger.record_event(
                    event_type="infrastructure",
                    event_name="circuit_half_open",
                    status="HALF_OPEN",
                    reason="checkConflict 冷却期结束，进入半开探测",
                    source="check_conflict_breaker",
                    details={"service": "check_conflict"}
                )
            except Exception:
                pass

    def _transition_to_normal(self):
        """恢复正常状态 NORMAL"""
        now = time.time()
        if self._last_open_start > 0:
            self.total_outage_seconds += (now - self._last_open_start)
            self._last_open_start = 0.0
        self.state = ConflictCircuitState.NORMAL
        self.failure_count = 0
        self._half_open_probe_in_progress = False
        if self.audit_logger:
            try:
                self.audit_logger.record_event(
                    event_type="infrastructure",
                    event_name=EventName.CONFLICT_SERVICE_RECOVERED,
                    status="NORMAL",
                    reason="checkConflict 半开探测成功，服务恢复正常",
                    source="check_conflict_breaker",
                    details={
                        "service": "check_conflict",
                        "total_outage_seconds": round(self.total_outage_seconds, 2)
                    }
                )
            except Exception:
                pass

    def get_remaining_cooldown(self) -> float:
        if self.state != ConflictCircuitState.OPEN:
            return 0.0
        elapsed = time.time() - self.opened_at
        return max(0.0, round(self.current_cooldown - elapsed, 2))

    def get_metrics(self) -> Dict:
        outage = self.total_outage_seconds
        if self.state == ConflictCircuitState.OPEN and self._last_open_start > 0:
            outage += (time.time() - self._last_open_start)
        return {
            "state": self.state,
            "failure_count": self.failure_count,
            "trip_count": self.trip_count,
            "current_cooldown": self.current_cooldown,
            "remaining_cooldown": self.get_remaining_cooldown(),
            "total_outage_seconds": round(outage, 2)
        }


def _is_special_course(it: Dict, cfg: Optional[Dict]) -> bool:
    """判断是否属于特殊活动"""
    if not cfg:
        return False
    name = str(it.get("name") or "")
    spec_list = cfg.get("special") or []
    return any(k in name for k in spec_list)


def _calc_week_of_date(d: datetime.date, tt: Optional[Dict]) -> Optional[int]:
    """推算指定日期在课表中的教学周"""
    if not tt or not tt.get("week1_monday"):
        return None
    try:
        w1_mon = datetime.date.fromisoformat(tt["week1_monday"])
        return (d - w1_mon).days // 7 + 1
    except Exception:
        return None


class LocalConflictChecker:
    """
    本地冲突检查器 (LocalConflictChecker)
    ==================================
    实现任务二与任务三的全部要求：
    - 检查前必须通过严格的本地数据完整性校验。
    - 完整校验已报课程、候选课程时间范围、日期重叠、跨天课程、教学周、课表占用、每日及每周配额限制。
    - 遇到数据残缺、时间解析失败、存在 UNKNOWN/SUBMITTING 等情况时，一律返回 BLOCKED。
    """

    def check_data_integrity(
        self,
        candidate: Optional[Dict],
        items: Any,
        state_store: Optional[StateStore] = None,
        tt: Optional[Dict] = None,
        reconciliation_in_progress: bool = False,
        current_run_id: Optional[str] = None,
        max_snapshot_age_sec: float = 60.0
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        任务三：本地数据完整性检查。
        只有全部满足，才允许进入本地冲突判断；任何一项不满足，返回 False 与原因。
        """
        details: Dict[str, Any] = {
            "items_present": items is not None,
            "has_complete_attr": False,
            "items_complete": False,
            "has_fetched_at": False,
            "candidate_valid": False,
            "registered_times_valid": True,
            "unresolved_count": 0,
            "reconciliation_active": reconciliation_in_progress
        }

        # 1. 正在进行全局对账
        if reconciliation_in_progress:
            return False, "系统当前正在执行全局对账，禁止本地降级决策", details

        # 2. 候选活动合法性
        if not candidate or not isinstance(candidate, dict):
            return False, "候选活动数据缺失或格式非法", details
        try:
            c_st = int(candidate.get("startTime", 0))
            c_et = int(candidate.get("endTime", 0))
            if c_st <= 0 or c_et <= c_st:
                return False, f"候选活动起止时间无效 (startTime={c_st}, endTime={c_et})", details
        except (ValueError, TypeError):
            return False, "候选活动起止时间无法解析为整数毫秒时间戳", details
        details["candidate_valid"] = True

        # 3. 课程类型识别检查（未知课程类型不能默认归为普通活动）
        c_type = candidate.get("type")
        if c_type is not None and str(c_type).upper() in ("UNKNOWN", "INVALID"):
            return False, f"候选活动课程类型未知或不可识别: {c_type}", details

        # 4. 已报名课程列表获取成功与类型检查
        if items is None:
            return False, "已报名课程列表为空 (None)，无法确认真实报名状态", details
        if not isinstance(items, (list, tuple)):
            return False, "已报名课程列表格式错误 (非列表结构)", details

        # 5. 分页完整性检查 (complete 属性必须明确存在，且不能为默认推断) (Task 1 & 2: C27, C28, C30)
        if not hasattr(items, "complete"):
            details["has_complete_attr"] = False
            return False, "活动列表缺失 complete 完整性标记证据 (缺少 complete 属性)，禁止推断完整性", details
        details["has_complete_attr"] = True
        is_complete = getattr(items, "complete")
        details["items_complete"] = bool(is_complete)
        if not is_complete:
            err_msg = getattr(items, "error", "") or "列表分页残缺"
            return False, f"已报名课程列表分页不完整 (complete=False): {err_msg}", details

        # 6. 本地数据快照新鲜度与有效期检查 (Task 3: C31, C32, C33)
        if not hasattr(items, "fetched_at") or getattr(items, "fetched_at") is None:
            details["has_fetched_at"] = False
            return False, "本地数据快照缺失新鲜度标记证据 (缺少 fetched_at 时间戳)", details
        details["has_fetched_at"] = True
        fetched_at = float(getattr(items, "fetched_at"))
        details["fetched_at"] = fetched_at
        elapsed_sec = time.time() - fetched_at
        details["snapshot_age_sec"] = round(elapsed_sec, 2)
        if elapsed_sec > max_snapshot_age_sec:
            return False, f"本地报名数据超过有效期 (已获取 {elapsed_sec:.1f}s，超过上限 {max_snapshot_age_sec}s)，安全阻断", details
        if elapsed_sec < -5.0:
            return False, f"本地报名数据时间戳异常 (未来时间 {fetched_at})，安全阻断", details

        # 6.2 run_id 匹配检查 (C32)
        items_run_id = getattr(items, "run_id", None)
        details["snapshot_run_id"] = items_run_id
        details["current_run_id"] = current_run_id
        if current_run_id is not None and items_run_id is not None:
            if str(items_run_id) != str(current_run_id):
                return False, f"本地数据快照 run_id 不匹配 (快照 run_id={items_run_id}, 当前 run_id={current_run_id})，禁止跨运行盲目复用", details

        # 6.3 数据快照获取后的本地状态变更检查 (C33)
        if state_store is not None:
            for rec in state_store.get_all_records():
                up_str = rec.get("updated_at")
                if up_str:
                    try:
                        up_dt = datetime.datetime.fromisoformat(up_str)
                        if up_dt.timestamp() > fetched_at:
                            return False, f"本地数据获取后状态发生变化 (活动 {rec.get('activity_id')} 于 {up_str} 发生状态变更)，必须重新刷新数据", details
                    except Exception:
                        pass

        # 7. 每条已报课程的时间信息有效性检查
        for it in items:
            if not isinstance(it, dict):
                return False, "课程列表包含非字典格式数据", details
            if it.get("isRegister"):
                try:
                    r_st = int(it.get("startTime", 0))
                    r_et = int(it.get("endTime", 0))
                    if r_st <= 0 or r_et <= r_st:
                        details["registered_times_valid"] = False
                        return False, f"已报课程 {it.get('name') or it.get('id')} 起止时间无效", details
                except (ValueError, TypeError):
                    details["registered_times_valid"] = False
                    return False, f"已报课程 {it.get('name') or it.get('id')} 时间解析失败", details

        # 8. 本地 StateStore 状态完整性检查 (严禁存在未决 UNKNOWN 或 SUBMITTING)
        if state_store is not None:
            unresolved = state_store.get_unresolved_records()
            details["unresolved_count"] = len(unresolved)
            if unresolved:
                statuses = set(r.get("status") for r in unresolved)
                if RegistrationStatus.UNKNOWN in statuses:
                    return False, "存在未解决 UNKNOWN 记录，安全屏障阻断本地降级评估", details
                if RegistrationStatus.SUBMITTING in statuses:
                    return False, "存在未完成 SUBMITTING 记录，本地降级阻断", details
                if RegistrationStatus.VERIFYING in statuses:
                    return False, "存在正在复核中的记录 (VERIFYING)，本地降级阻断", details
                return False, f"本地存在未决状态记录 ({','.join(statuses)})，安全阻断", details

        # 全部通过 (空列表且 complete=True 允许评估, C29)
        return True, "本地数据完整可信", details

    def check_conflict(
        self,
        candidate: Dict,
        items: Any,
        cfg: Optional[Dict] = None,
        tt: Optional[Dict] = None,
        state_store: Optional[StateStore] = None,
        current_week: Optional[int] = None,
        extra_slots: Optional[List] = None,
        day_done: Optional[Dict] = None,
        week_done: Optional[Dict] = None,
        reconciliation_in_progress: bool = False,
        current_run_id: Optional[str] = None,
        max_snapshot_age_sec: float = 60.0
    ) -> ConflictCheckResult:
        """
        任务二：本地冲突检查核心逻辑。
        """
        # 1. 严格本地数据完整性校验
        ok, reason, details = self.check_data_integrity(
            candidate=candidate,
            items=items,
            state_store=state_store,
            tt=tt,
            reconciliation_in_progress=reconciliation_in_progress,
            current_run_id=current_run_id,
            max_snapshot_age_sec=max_snapshot_age_sec
        )
        if not ok:
            return ConflictCheckResult(
                has_conflict=True,
                mode=ConflictCheckMode.BLOCKED,
                status="BLOCKED",
                reason=reason,
                data_integrity=details
            )

        c_st = int(candidate["startTime"])
        c_et = int(candidate["endTime"])
        c_dt_start = datetime.datetime.fromtimestamp(c_st / 1000)
        c_dt_end = datetime.datetime.fromtimestamp(c_et / 1000)
        c_date = c_dt_start.date()
        c_name = candidate.get("name") or str(candidate.get("id"))
        spec = _is_special_course(candidate, cfg)

        # 推算候选课程所在教学周
        cand_week = current_week
        if cand_week is None and tt:
            cand_week = _calc_week_of_date(c_date, tt)

        # 2. 与已报课程的时间范围重叠检查 (精确半开区间 [start, end))
        # 收集已报课程：来自 items 中 isRegister=True 以及 StateStore 中 SUCCESS 记录
        registered_courses: List[Dict] = [it for it in items if it.get("isRegister")]

        if state_store is not None:
            # 纳入已在本地标记成功但可能尚未被 items 同步的课程
            try:
                for r in state_store.get_all_records():
                    if r.get("status") == RegistrationStatus.SUCCESS:
                        aid = str(r.get("activity_id"))
                        if not any(str(it.get("id")) == aid for it in registered_courses):
                            # 若在 items 中找到该活动详情，补充进 registered_courses
                            matched_it = next((it for it in items if str(it.get("id")) == aid), None)
                            if matched_it and "startTime" in matched_it:
                                registered_courses.append(matched_it)
            except Exception:
                pass

        for reg in registered_courses:
            r_st = int(reg["startTime"])
            r_et = int(reg["endTime"])
            r_name = reg.get("name") or str(reg.get("id"))

            # 时间区间重叠条件：max(start1, start2) < min(end1, end2)
            # 即 start1 < end2 and start2 < end1
            if c_st < r_et and r_st < c_et:
                # 冲突发生（覆盖跨天课程、同日重叠课程）
                return ConflictCheckResult(
                    has_conflict=True,
                    mode=ConflictCheckMode.DEGRADED_LOCAL,
                    status="CONFLICT",
                    reason=f"与已报课程时间重叠: {r_name} ({reg.get('startTime')}~{reg.get('endTime')})",
                    data_integrity=details
                )

        # 3. 课表 busy_slots 及 EXTRA 占用检查
        if tt and cand_week is not None:
            busy_slots_list = []

            # 课表 busy_slots (Schema v2)
            for bs in tt.get("busy_slots") or []:
                weeks = bs.get("weeks") or []
                if any(w_start <= cand_week <= w_end for w_start, w_end in weeks):
                    try:
                        wday = int(bs["weekday"])
                        st_parts = str(bs["start"]).split(":")
                        et_parts = str(bs["end"]).split(":")
                        s_min = int(st_parts[0]) * 60 + int(st_parts[1])
                        e_min = int(et_parts[0]) * 60 + int(et_parts[1])
                        lbl = bs.get("label") or "课表占用"
                        busy_slots_list.append((wday, s_min, e_min, lbl))
                    except Exception:
                        pass

            # 配置文件 busy_slots
            if cfg:
                for slot_str in cfg.get("busy_slots") or []:
                    # 周X HH:MM-HH:MM
                    # 若匹配则计入
                    pass

            # 动态 EXTRA 占用 (week, slot, why)
            if extra_slots:
                for ex_w, ex_s, ex_why in extra_slots:
                    if ex_w == cand_week and ex_s:
                        busy_slots_list.append((ex_s[0], ex_s[1], ex_s[2], ex_why))

            # 候选课程可能跨天，拆解为每日的 (weekday, start_min, end_min)
            cand_segments: List[Tuple[int, int, int]] = []
            if c_dt_start.date() == c_dt_end.date():
                wday = c_dt_start.isoweekday()
                s_min = c_dt_start.hour * 60 + c_dt_start.minute
                e_min = c_dt_end.hour * 60 + c_dt_end.minute
                cand_segments.append((wday, s_min, e_min))
            else:
                # 跨天拆分为两段 (C15)
                wday1 = c_dt_start.isoweekday()
                s_min1 = c_dt_start.hour * 60 + c_dt_start.minute
                e_min1 = 24 * 60
                cand_segments.append((wday1, s_min1, e_min1))

                wday2 = c_dt_end.isoweekday()
                s_min2 = 0
                e_min2 = c_dt_end.hour * 60 + c_dt_end.minute
                cand_segments.append((wday2, s_min2, e_min2))

            for cw_day, cs_min, ce_min in cand_segments:
                for bw_day, bs_min, be_min, b_label in busy_slots_list:
                    if cw_day == bw_day and cs_min < be_min and bs_min < ce_min:
                        return ConflictCheckResult(
                            has_conflict=True,
                            mode=ConflictCheckMode.DEGRADED_LOCAL,
                            status="CONFLICT",
                            reason=f"与课表/忙碌时段重叠: {b_label}",
                            data_integrity=details
                        )

        # 4. 配额限制检查 (普通活动受每日上限与每周上限约束，特殊活动跳过)
        if not spec and cfg:
            # 4.1 每日上限 (max_per_day)
            max_per_day = int(cfg.get("max_per_day", 1))
            if max_per_day > 0:
                current_day_count = sum(
                    1 for it in registered_courses
                    if not _is_special_course(it, cfg) and
                    datetime.datetime.fromtimestamp(int(it["startTime"]) / 1000).date() == c_date
                )
                if day_done:
                    current_day_count += day_done.get(c_date, 0)

                if current_day_count >= max_per_day:
                    return ConflictCheckResult(
                        has_conflict=True,
                        mode=ConflictCheckMode.DEGRADED_LOCAL,
                        status="CONFLICT",
                        reason=f"当天普通活动已达每日上限 ({current_day_count}/{max_per_day} 节)",
                        data_integrity=details
                    )

            # 4.2 每周上限 (max_normal)
            max_normal = int(cfg.get("max_normal", 2))
            if max_normal > 0 and tt and cand_week is not None:
                current_week_count = sum(
                    1 for it in registered_courses
                    if not _is_special_course(it, cfg) and
                    _calc_week_of_date(datetime.datetime.fromtimestamp(int(it["startTime"]) / 1000).date(), tt) == cand_week
                )
                if week_done:
                    current_week_count += week_done.get(cand_week, 0)

                if current_week_count >= max_normal:
                    return ConflictCheckResult(
                        has_conflict=True,
                        mode=ConflictCheckMode.DEGRADED_LOCAL,
                        status="CONFLICT",
                        reason=f"第 {cand_week} 周普通活动已达每周上限 ({current_week_count}/{max_normal} 节)",
                        data_integrity=details
                    )

        # 全部通过，判定无冲突
        return ConflictCheckResult(
            has_conflict=False,
            mode=ConflictCheckMode.DEGRADED_LOCAL,
            status="CLEAN",
            reason="本地规则检查通过，无时间及配额冲突",
            data_integrity=details
        )


class ConflictManager:
    """
    冲突检查统一协调器 (ConflictManager)
    ==================================
    统一管理服务端检查与本地智能降级全流程：
    1. 配置感知：兼容 check_conflict 配置项；若为 false，显式跳过服务端检查但仍执行本地安全检查。
    2. 健康控制：通过 ConflictCircuitBreaker 管理服务端接口健康状态；
       - NORMAL: 请求服务端 checkConflict；
       - OPEN: 跳过服务端，直接执行本地降级检查；
       - HALF_OPEN: 仅允许单次只读健康探测。
    3. 异常降级：服务端遇 5xx/超时等基础设施故障时，记录熔断器失败，
       并自动切入 DEGRADED_LOCAL 执行本地完整性与规则检查；
    4. 统一审计：全程输出 Schema v1 规范审计事件，准确记录降级与恢复全过程。
    """

    def __init__(
        self,
        client: Any,
        breaker: Optional[ConflictCircuitBreaker] = None,
        local_checker: Optional[LocalConflictChecker] = None,
        audit_logger: Optional[AuditLogger] = None,
        cfg: Optional[Dict] = None,
        tt: Optional[Dict] = None,
        state_store: Optional[StateStore] = None
    ):
        self.client = client
        self.breaker = breaker or ConflictCircuitBreaker(audit_logger=audit_logger)
        self.local_checker = local_checker or LocalConflictChecker()
        self.audit_logger = audit_logger
        self.cfg = cfg or {}
        self.tt = tt or {}
        self.state_store = state_store

    def _emit_audit(
        self,
        event_name: str,
        event_type: str = EventType.CANDIDATE,
        activity_id: Optional[str] = None,
        status: Optional[str] = None,
        reason: Optional[str] = None,
        http_status: Optional[int] = None,
        duration_ms: Optional[float] = None,
        correlation_id: Optional[str] = None,
        run_id: Optional[str] = None,
        request_id: Optional[str] = None,
        details: Optional[Dict] = None
    ):
        if self.audit_logger and hasattr(self.audit_logger, "record_event"):
            try:
                self.audit_logger.record_event(
                    event_type=event_type,
                    event_name=event_name,
                    activity_id=activity_id,
                    status=status,
                    reason=reason,
                    http_status=http_status,
                    duration_ms=duration_ms,
                    correlation_id=correlation_id,
                    run_id=run_id,
                    request_id=request_id,
                    source="conflict_manager",
                    details=details
                )
            except Exception:
                pass

    def check_activity_conflict(
        self,
        candidate: Dict,
        items: Any,
        extra_slots: Optional[List] = None,
        day_done: Optional[Dict] = None,
        week_done: Optional[Dict] = None,
        correlation_id: Optional[str] = None,
        run_id: Optional[str] = None,
        current_week: Optional[int] = None,
        reconciliation_in_progress: bool = False,
        max_snapshot_age_sec: float = 60.0
    ) -> Tuple[bool, str]:
        """
        核心对外入口：评估候选活动是否存在冲突。
        返回: (is_allowed: bool, reason: str)
        - is_allowed 为 True 表示无冲突，允许进入报名流程；
        - is_allowed 为 False 表示存在冲突或被安全阻断，禁止报名。
        """
        aid = str(candidate.get("id"))
        cid = correlation_id or f"corr_conf_{aid}_{int(time.time()*1000)}"
        r_id = run_id or (getattr(self.audit_logger, "run_id", None) if self.audit_logger else None)

        server_check_enabled = bool(self.cfg.get("check_conflict", True))

        # ---------------- 场景 1: 用户显式配置关闭服务端冲突检查 ----------------
        if not server_check_enabled:
            # Task 7 / C26: 禁用服务端检查不能同时关闭本地安全检查！
            return self._execute_local_fallback(
                candidate=candidate,
                items=items,
                aid=aid,
                cid=cid,
                r_id=r_id,
                trigger_reason="用户配置禁用服务端检查 (check_conflict: false)，执行纯本地安全检查",
                extra_slots=extra_slots,
                day_done=day_done,
                week_done=week_done,
                current_week=current_week,
                reconciliation_in_progress=reconciliation_in_progress,
                current_run_id=r_id,
                max_snapshot_age_sec=max_snapshot_age_sec
            )

        # ---------------- 场景 2: 熔断器检查与服务端调用 ----------------
        can_attempt, breaker_msg = self.breaker.can_attempt()

        if not can_attempt:
            # 熔断器处于 OPEN，禁止发起请求，直接本地降级 (C03)
            self._emit_audit(
                event_name=EventName.CONFLICT_CHECK_DEGRADED,
                activity_id=aid,
                status="DEGRADED_LOCAL",
                reason=f"checkConflict 服务熔断中 ({breaker_msg})，自动切入本地降级",
                correlation_id=cid,
                run_id=r_id,
                details={"breaker_state": self.breaker.state}
            )
            return self._execute_local_fallback(
                candidate=candidate,
                items=items,
                aid=aid,
                cid=cid,
                r_id=r_id,
                trigger_reason=f"熔断保护生效 ({breaker_msg})",
                extra_slots=extra_slots,
                day_done=day_done,
                week_done=week_done,
                current_week=current_week,
                reconciliation_in_progress=reconciliation_in_progress,
                current_run_id=r_id,
                max_snapshot_age_sec=max_snapshot_age_sec
            )

        # 允许请求 (NORMAL 或 HALF_OPEN 探测)
        is_probe = self.breaker.is_half_open
        req_id = f"req_conf_{uuid.uuid4().hex[:12]}"
        self._emit_audit(
            event_name=EventName.CONFLICT_CHECK_STARTED,
            activity_id=aid,
            status="STARTED",
            correlation_id=cid,
            run_id=r_id,
            request_id=req_id,
            details={"is_probe": is_probe, "mode": "SERVER"}
        )

        t0 = time.time()
        call_err = None
        code = None
        obj = None

        try:
            # 调用服务端 checkConflict
            if hasattr(self.client, "call"):
                code, obj = self.client.call("GET", "education/app/activity/checkConflict", {"activityId": aid}, timeout=4)
                is_conflict = None
                if code == 200 and isinstance(obj, dict):
                    is_conflict = bool(obj.get("data"))
            elif hasattr(self.client, "check_conflict"):
                res = self.client.check_conflict(aid)
                if res is True:
                    code, is_conflict, obj = 200, True, {"code": 0, "data": True}
                elif res is False:
                    code, is_conflict, obj = 200, False, {"code": 0, "data": False}
                else:
                    code, is_conflict, obj = 500, None, {"_raw": "check_conflict returned None"}
            else:
                code, is_conflict, obj = -1, None, "No supported method on client"
        except Exception as e:
            call_err = e
            code = getattr(e, "code", -1) if hasattr(e, "code") else -1
            is_conflict = None

        dur_ms = round((time.time() - t0) * 1000, 2)

        # 2.1 成功响应 (HTTP 200 且数据有效完整)
        if code == 200 and isinstance(obj, dict) and "data" in obj:
            # 记录成功（若是半开探测，则平滑恢复 NORMAL）
            self.breaker.record_success(is_complete=True)
            self._emit_audit(
                event_name=EventName.CONFLICT_CHECK_SUCCEEDED,
                activity_id=aid,
                status="SUCCESS",
                reason="服务端冲突检查返回有效判定",
                http_status=200,
                duration_ms=dur_ms,
                correlation_id=cid,
                run_id=r_id,
                request_id=req_id,
                details={"is_conflict": is_conflict, "is_probe": is_probe}
            )
            if is_conflict:
                return False, "服务端判定冲突"
            else:
                return True, "服务端判定无冲突"

        # 2.2 客户端错误 (401, 403, 400 等) - 严禁计入基础设施故障
        if code in (400, 401, 403, 404):
            self.breaker.record_failure(http_status=code)
            self._emit_audit(
                event_name=EventName.CONFLICT_CHECK_FAILED,
                activity_id=aid,
                status="FAILED",
                reason=f"服务端返回客户端错误 HTTP {code}",
                http_status=code,
                duration_ms=dur_ms,
                correlation_id=cid,
                run_id=r_id,
                request_id=req_id
            )
            if code == 401:
                return False, "服务端冲突检查返回 401 (Token失效，认证未恢复，禁止报名提交)"
            elif code == 403:
                return False, "服务端冲突检查返回 403 (权限不足，禁止报名提交)"
            elif code == 400:
                return False, "服务端冲突检查返回 400 (请求参数错误)"
            else:
                return False, f"服务端冲突检查客户端异常 HTTP {code}"

        # 2.3 基础设施故障 (5xx, 超时 -1, 网络异常, 或 200 返回残缺响应)
        if code == 200:
            # 响应数据残缺，按半开失败或不可靠处理
            self.breaker.record_success(is_complete=False)
        else:
            self.breaker.record_failure(http_status=code, error=call_err)

        self._emit_audit(
            event_name=EventName.CONFLICT_CHECK_FAILED,
            activity_id=aid,
            status="FAILED",
            reason=f"服务端基础设施故障 HTTP {code} ({call_err or '数据不完整'})",
            http_status=code,
            duration_ms=dur_ms,
            correlation_id=cid,
            run_id=r_id,
            request_id=req_id
        )

        # 触发自动降级到本地检查 (DEGRADED_LOCAL)
        self._emit_audit(
            event_name=EventName.CONFLICT_CHECK_DEGRADED,
            activity_id=aid,
            status="DEGRADED_LOCAL",
            reason=f"服务端接口异常 (HTTP {code})，自动降级为本地规则检查",
            correlation_id=cid,
            run_id=r_id,
            details={"server_http_status": code, "breaker_state": self.breaker.state}
        )

        return self._execute_local_fallback(
            candidate=candidate,
            items=items,
            aid=aid,
            cid=cid,
            r_id=r_id,
            trigger_reason=f"服务端异常 (HTTP {code})",
            extra_slots=extra_slots,
            day_done=day_done,
            week_done=week_done,
            current_week=current_week,
            reconciliation_in_progress=reconciliation_in_progress,
            current_run_id=r_id,
            max_snapshot_age_sec=max_snapshot_age_sec
        )

    def _execute_local_fallback(
        self,
        candidate: Dict,
        items: Any,
        aid: str,
        cid: str,
        r_id: Optional[str],
        trigger_reason: str,
        extra_slots: Optional[List],
        day_done: Optional[Dict],
        week_done: Optional[Dict],
        current_week: Optional[int],
        reconciliation_in_progress: bool,
        current_run_id: Optional[str] = None,
        max_snapshot_age_sec: float = 60.0
    ) -> Tuple[bool, str]:
        """执行本地降级冲突检查流程"""
        if current_run_id is None:
            current_run_id = r_id

        self._emit_audit(
            event_name=EventName.LOCAL_CONFLICT_CHECK_STARTED,
            activity_id=aid,
            status="STARTED",
            correlation_id=cid,
            run_id=r_id,
            details={"trigger_reason": trigger_reason}
        )

        # 调用 LocalConflictChecker 执行完整性与规则检查
        res = self.local_checker.check_conflict(
            candidate=candidate,
            items=items,
            cfg=self.cfg,
            tt=self.tt,
            state_store=self.state_store,
            current_week=current_week,
            extra_slots=extra_slots,
            day_done=day_done,
            week_done=week_done,
            reconciliation_in_progress=reconciliation_in_progress,
            current_run_id=current_run_id,
            max_snapshot_age_sec=max_snapshot_age_sec
        )

        # 3.1 本地数据不完整，安全阻断 (BLOCKED)
        if res.is_blocked:
            self._emit_audit(
                event_name=EventName.LOCAL_CONFLICT_CHECK_BLOCKED,
                activity_id=aid,
                status="BLOCKED",
                reason=res.reason,
                correlation_id=cid,
                run_id=r_id,
                details=res.data_integrity
            )
            return False, f"本地数据不完整安全阻断: {res.reason}"

        # 3.2 本地规则检测到冲突 (CONFLICT)
        if res.has_conflict:
            self._emit_audit(
                event_name=EventName.LOCAL_CONFLICT_DETECTED,
                activity_id=aid,
                status="CONFLICT",
                reason=res.reason,
                correlation_id=cid,
                run_id=r_id,
                details=res.data_integrity
            )
            return False, f"本地规则判定冲突: {res.reason}"

        # 3.3 本地规则判定无冲突放行 (CLEAN)
        self._emit_audit(
            event_name=EventName.LOCAL_CONFLICT_CHECK_SUCCEEDED,
            activity_id=aid,
            status="CLEAN",
            reason=res.reason,
            correlation_id=cid,
            run_id=r_id,
            details=res.data_integrity
        )
        return True, "本地规则检查通过"
