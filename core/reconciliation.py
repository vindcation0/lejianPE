"""
全局报名核对与超额告警模块 (ReconciliationManager)
=================================================
在单周抢满或观察窗口结束时，拉取服务端最终状态对账：
1. 统计实际 isRegister 活动总数与各周分布。
2. 校验是否超过配置的 max_normal 与 max_per_day。
3. 若发现超额，发出显著告警并立刻阻断任何后续提交，提示用户人工退课，避免误退或自动退课副作用。
4. 区分合法完整空列表与异常空列表，避免误判对账失败。
5. 记录无法解析时间戳的异常活动，避免静默丢失潜在配额占用。
"""
import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple


class ReconciliationManager:
    """报名结果核对器"""

    def __init__(
        self,
        client: Any,
        cfg: Dict,
        timetable: Dict,
        week_calc_fn: Callable,
        audit_logger: Optional[Any] = None,
        state_store: Optional[Any] = None
    ):
        self.client = client
        self.cfg = cfg
        self.timetable = timetable
        self.week_calc_fn = week_calc_fn
        self.audit_logger = audit_logger
        self.state_store = state_store

    def reconcile(self, state_store: Optional[Any] = None) -> Dict[str, Any]:
        """
        拉取最新活动列表，核对当前所有教学周的实际报名状态。
        返回对账报告字典，并在对账成功且数据完整时同步解除 UNKNOWN 状态。
        """
        active_store = state_store or self.state_store
        run_id = getattr(self.audit_logger, "run_id", None) if self.audit_logger else None
        if self.audit_logger:
            try:
                self.audit_logger.record_event(
                    event_type="reconciliation",
                    event_name="reconciliation_started",
                    source="reconciliation"
                )
            except Exception:
                pass

        max_normal = int(self.cfg.get("max_normal", 2))
        max_per_day = int(self.cfg.get("max_per_day", 1))
        special_keywords = self.cfg.get("special") or []

        try:
            items = self.client.activities(page_size=200, req_timeout=6)
        except Exception as e:
            if self.audit_logger:
                try:
                    self.audit_logger.record_event(
                        event_type="reconciliation",
                        event_name="reconciliation_failed",
                        reason=f"拉取对账列表失败: {e}",
                        source="reconciliation"
                    )
                except Exception:
                    pass
            return {
                "success": False,
                "status": "FAILED",
                "run_id": run_id,
                "complete": False,
                "error": f"拉取对账列表失败: {e}",
                "registered": [],
                "exceeded": False
            }

        # 检查是否完整
        is_complete = bool(getattr(items, "complete", True))
        total_cnt = getattr(items, "total", len(items) if items is not None else 0)

        if not items:
            # 区分合法完整的空列表与非正常空数据
            if is_complete and total_cnt == 0:
                report = {
                    "success": True,
                    "status": "SUCCESS",
                    "run_id": run_id,
                    "complete": True,
                    "total_registered": 0,
                    "normal_registered": 0,
                    "special_registered": 0,
                    "by_week": {},
                    "by_day": {},
                    "exceeded": False,
                    "exceeded_weeks": {},
                    "exceeded_days": {},
                    "items": [],
                    "unparsed_items": []
                }
                # 若存在未决记录，在完整空列表下同步为 RETRYABLE
                if active_store is not None:
                    try:
                        for rec in active_store.get_unresolved_records():
                            aid = str(rec.get("activity_id"))
                            active_store.record_retryable(aid, "最终全局对账确认服务端未报名(空列表)")
                    except Exception:
                        pass
                return report

            return {
                "success": False,
                "status": "UNVERIFIED",
                "run_id": run_id,
                "complete": False,
                "error": f"活动列表为空或未通过完整性验证 (complete={is_complete}, total={total_cnt})",
                "registered": [],
                "exceeded": False
            }

        if not is_complete:
            err_msg = f"对账列表不完整（仅获取 {getattr(items, 'fetched', len(items))}/{getattr(items, 'total', '?')} 条，{getattr(items, 'error', '')}），对账中止"
            if self.audit_logger:
                try:
                    self.audit_logger.record_event(
                        event_type="reconciliation",
                        event_name="reconciliation_incomplete",
                        reason=err_msg,
                        source="reconciliation",
                        details={"fetched": getattr(items, 'fetched', len(items)), "total": getattr(items, 'total', 0)}
                    )
                except Exception:
                    pass
            return {
                "success": False,
                "status": "UNVERIFIED",
                "run_id": run_id,
                "complete": False,
                "error": err_msg,
                "registered": [],
                "exceeded": False
            }

        registered_items = [it for it in items if it.get("isRegister")]

        by_week: Dict[int, List[Dict]] = {}
        by_day: Dict[datetime.date, List[Dict]] = {}
        special_items: List[Dict] = []
        normal_items: List[Dict] = []
        unparsed_items: List[Dict] = []

        for it in registered_items:
            name = (it.get("name") or "").replace("\n", " ").strip()
            is_spec = any(k in name for k in special_keywords)
            try:
                st = int(it.get("startTime") or 0)
                if st <= 0:
                    raise ValueError(f"startTime 无效: {st}")
                dt = datetime.datetime.fromtimestamp(st / 1000)
                d = dt.date()
                wk = self.week_calc_fn(d, self.timetable)
            except Exception as e:
                unparsed_items.append({
                    "id": it.get("id"),
                    "name": name,
                    "reason": str(e)
                })
                if is_spec:
                    special_items.append(it)
                continue

            if is_spec:
                special_items.append(it)
            else:
                normal_items.append(it)
                by_week.setdefault(wk, []).append(it)
                by_day.setdefault(d, []).append(it)

        # 检查是否超额
        exceeded_weeks = {w: acts for w, acts in by_week.items() if len(acts) > max_normal}
        exceeded_days = {d: acts for d, acts in by_day.items() if len(acts) > max_per_day}
        has_exceeded = bool(exceeded_weeks or exceeded_days)

        report = {
            "success": True,
            "status": "SUCCESS",
            "run_id": run_id,
            "complete": True,
            "total_registered": len(registered_items),
            "normal_registered": len(normal_items),
            "special_registered": len(special_items),
            "unparsed_items": unparsed_items,
            "unparsed_count": len(unparsed_items),
            "by_week": {w: [it.get("name") for it in acts] for w, acts in by_week.items()},
            "by_day": {str(d): [it.get("name") for it in acts] for d, acts in by_day.items()},
            "exceeded": has_exceeded,
            "exceeded_weeks": {w: len(acts) for w, acts in exceeded_weeks.items()},
            "exceeded_days": {str(d): len(acts) for d, acts in exceeded_days.items()},
            "items": registered_items
        }

        # 全局对账成功且列表完整时，同步解除 StateStore 中的 UNKNOWN 状态
        if active_store is not None:
            try:
                registered_ids = {str(it.get("id")) for it in registered_items if it.get("id")}
                unresolved_records = active_store.get_unresolved_records()
                for rec in unresolved_records:
                    aid = str(rec.get("activity_id"))
                    act_name = rec.get("activity_name") or ""
                    if aid in registered_ids:
                        # 服务端确认存在该活动的有效报名：解除 UNKNOWN 并更新为 SUCCESS
                        active_store.record_success(aid, act_name, http_status=200)
                        if self.audit_logger and hasattr(self.audit_logger, "record_event"):
                            self.audit_logger.record_event(
                                event_type="verification",
                                event_name="verification_confirmed_registered",
                                activity_id=aid,
                                status="SUCCESS",
                                source="reconciliation",
                                details={"reason": "最终全局对账识别到服务端已报名"}
                            )
                    else:
                        # 仅在列表完整时才确认未报名并解除为 RETRYABLE
                        if is_complete:
                            active_store.record_retryable(aid, "最终全局对账确认服务端未报名")
                            if self.audit_logger and hasattr(self.audit_logger, "record_event"):
                                self.audit_logger.record_event(
                                    event_type="verification",
                                    event_name="verification_confirmed_unregistered",
                                    activity_id=aid,
                                    status="RETRYABLE",
                                    source="reconciliation",
                                    details={"reason": "最终全局对账确认服务端未报名"}
                                )
            except Exception as e:
                if self.audit_logger and hasattr(self.audit_logger, "record_event"):
                    self.audit_logger.record_event(
                        event_type="reconciliation",
                        event_name="reconciliation_failed",
                        reason=f"对账同步状态库异常: {e}",
                        source="reconciliation"
                    )

        if self.audit_logger:
            try:
                self.audit_logger.record_event(
                    event_type="reconciliation",
                    event_name="reconciliation_succeeded",
                    source="reconciliation",
                    details={
                        "total_registered": len(registered_items),
                        "normal_registered": len(normal_items),
                        "special_registered": len(special_items),
                        "exceeded": has_exceeded
                    }
                )
            except Exception:
                pass

        return report
