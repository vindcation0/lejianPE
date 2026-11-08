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

    def __init__(self, client: Any, cfg: Dict, timetable: Dict, week_calc_fn: Callable, audit_logger: Optional[Any] = None):
        self.client = client
        self.cfg = cfg
        self.timetable = timetable
        self.week_calc_fn = week_calc_fn
        self.audit_logger = audit_logger

    def reconcile(self) -> Dict[str, Any]:
        """
        拉取最新活动列表，核对当前所有教学周的实际报名状态。
        返回对账报告字典。
        """
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
                return {
                    "success": True,
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
            return {
                "success": False,
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
