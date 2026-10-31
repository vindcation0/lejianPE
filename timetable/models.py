"""
时间占用数据模型 (TimeSlot & Timetable)
=======================================
定义 TimeSlot（时间占用段）与 Timetable（课表总结构）。
不保留课程名、教师、教室等非必要信息，专注于时间冲突检测。
"""
from typing import Any, Dict, List, Optional, Tuple


class TimeSlot:
    """单个时间占用记录"""

    def __init__(
        self,
        weekday: int,
        start: str,
        end: str,
        weeks: List[List[int]],
        label: Optional[str] = None
    ):
        self.weekday = int(weekday)
        self.start = str(start).strip()
        self.end = str(end).strip()
        self.weeks = [[int(a), int(b)] for a, b in weeks]
        self.label = label or "课表占用"

    @property
    def start_min(self) -> int:
        h, m = self.start.split(":")
        return int(h) * 60 + int(m)

    @property
    def end_min(self) -> int:
        h, m = self.end.split(":")
        return int(h) * 60 + int(m)

    def is_active_in_week(self, week_num: int) -> bool:
        """判断该时间段在指定教学周是否有效"""
        return any(a <= week_num <= b for a, b in self.weeks)

    def overlaps_time(self, weekday: int, start_min: int, end_min: int) -> bool:
        """仅判断星期和具体时段是否重叠"""
        if self.weekday != weekday:
            return False
        return self.start_min < end_min and self.end_min > start_min

    def conflict_with(self, week_num: int, weekday: int, start_min: int, end_min: int) -> bool:
        """
        三要素联合判断冲突：
        1. 星期相同
        2. 教学周匹配
        3. 时段重叠 (start < other_end and end > other_start)
        """
        if not self.is_active_in_week(week_num):
            return False
        return self.overlaps_time(weekday, start_min, end_min)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "weekday": self.weekday,
            "start": self.start,
            "end": self.end,
            "weeks": self.weeks
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TimeSlot":
        return cls(
            weekday=data["weekday"],
            start=data["start"],
            end=data["end"],
            weeks=data["weeks"],
            label=data.get("label") or data.get("name")
        )

    def __repr__(self):
        return f"<TimeSlot 周{self.weekday} {self.start}-{self.end} weeks={self.weeks}>"

    def __eq__(self, other):
        if not isinstance(other, TimeSlot):
            return False
        return (
            self.weekday == other.weekday
            and self.start == other.start
            and self.end == other.end
            and sorted(self.weeks) == sorted(other.weeks)
        )


class Timetable:
    """课表总结构 (Schema v2)"""

    def __init__(
        self,
        week1_monday: str,
        busy_slots: Optional[List[TimeSlot]] = None,
        schema_version: int = 2
    ):
        self.schema_version = schema_version
        self.week1_monday = str(week1_monday).strip()
        self.busy_slots: List[TimeSlot] = busy_slots or []

    def add_slot(self, slot: TimeSlot):
        self.busy_slots.append(slot)

    def deduplicate(self):
        """去除完全相同（星期、时间、周次相同）的冗余记录"""
        unique = []
        for s in self.busy_slots:
            if s not in unique:
                unique.append(s)
        self.busy_slots = unique

    def busy_for_week(self, week_num: int) -> List[Tuple[Tuple[int, int, int], str]]:
        """
        供抢课程序查询某教学周内所有占用的时段：
        返回: [((weekday, start_min, end_min), label), ...]
        """
        res = []
        for s in self.busy_slots:
            if s.is_active_in_week(week_num):
                res.append(((s.weekday, s.start_min, s.end_min), f"{s.label}（第{week_num}周）"))
        return res

    def has_conflict(self, week_num: int, weekday: int, start_min: int, end_min: int) -> bool:
        """快速判断指定周次、星期和时间范围是否与课表冲突"""
        return any(s.conflict_with(week_num, weekday, start_min, end_min) for s in self.busy_slots)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "week1_monday": self.week1_monday,
            "busy_slots": [s.to_dict() for s in self.busy_slots]
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Timetable":
        w1 = data.get("week1_monday", "2026-08-31")
        v = data.get("schema_version", 2)
        raw_slots = data.get("busy_slots", [])
        slots = [TimeSlot.from_dict(s) for s in raw_slots]
        return cls(week1_monday=w1, busy_slots=slots, schema_version=v)

    def __repr__(self):
        return f"<Timetable v{self.schema_version} week1={self.week1_monday} slots={len(self.busy_slots)}>"
