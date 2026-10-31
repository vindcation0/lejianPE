"""
全局报名核对与超额告警模块 (ReconciliationManager)
=================================================
在单周抢满或观察窗口结束时，拉取服务端最终状态对账：
1. 统计实际 isRegister 活动总数与各周分布。
2. 校验是否超过配置的 max_normal 与 max_per_day。
3. 若发现超额，发出显著告警并立刻阻断任何后续提交，提示用户人工退课，避免误退或自动退课副作用。
"""
import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple


class ReconciliationManager:
    """报名结果核对器"""

    def __init__(self, client: Any, cfg: Dict, timetable: Dict, week_calc_fn: Callable):
        self.client = client
        self.cfg = cfg
        self.timetable = timetable
        self.week_calc_fn = week_calc_fn

    def reconcile(self) -> Dict[str, Any]:
        """
        拉取最新活动列表，核对当前所有教学周的实际报名状态。
        返回对账报告字典。
        """
        max_normal = int(self.cfg.get("max_normal", 2))
        max_per_day = int(self.cfg.get("max_per_day", 1))
        special_keywords = self.cfg.get("special") or []

        try:
            items = self.client.activities(page_size=200, req_timeout=6)
        except Exception as e:
            return {
                "success": False,
                "error": f"拉取对账列表失败: {e}",
                "registered": [],
                "exceeded": False
            }

        if not items:
            return {
                "success": False,
                "error": "活动列表为空",
                "registered": [],
                "exceeded": False
            }

        registered_items = [it for it in items if it.get("isRegister")]

        by_week: Dict[int, List[Dict]] = {}
        by_day: Dict[datetime.date, List[Dict]] = {}
        special_items: List[Dict] = []
        normal_items: List[Dict] = []

        for it in registered_items:
            name = (it.get("name") or "").replace("\n", " ").strip()
            is_spec = any(k in name for k in special_keywords)
            try:
                st = int(it.get("startTime") or 0)
                dt = datetime.datetime.fromtimestamp(st / 1000)
                d = dt.date()
                wk = self.week_calc_fn(d, self.timetable)
            except Exception:
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
            "by_week": {w: [it.get("name") for it in acts] for w, acts in by_week.items()},
            "by_day": {str(d): [it.get("name") for it in acts] for d, acts in by_day.items()},
            "exceeded": has_exceeded,
            "exceeded_weeks": {w: len(acts) for w, acts in exceeded_weeks.items()},
            "exceeded_days": {str(d): len(acts) for d, acts in exceeded_days.items()},
            "items": registered_items
        }

        return report
