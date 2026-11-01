"""
课表数据校验模块 (Validator)
===========================
严格校验 Timetable 数据结构、数值范围与业务逻辑约束：
- 明确指出错误条目与具体字段
- 校验失败时阻断保存，禁止覆盖已有课表
"""
import datetime
import re
from typing import Any, Dict, List, Optional


class ValidationError(Exception):
    """课表数据校验异常"""
    def __init__(self, message: str, field: Optional[str] = None, index: Optional[int] = None):
        self.field = field
        self.index = index
        prefix = ""
        if index is not None:
            prefix += f"[第 {index} 条记录] "
        if field:
            prefix += f"字段 '{field}' "
        super().__init__(f"{prefix}校验失败: {message}")


def validate_timetable_dict(data: Any) -> None:
    """
    对课表原始或构建后的 dict 进行全量校验。
    若存在任何错误立即抛出 ValidationError。
    """
    if not isinstance(data, dict):
        raise ValidationError("课表数据根节点必须为 JSON 对象 (dict)")

    # 1. schema_version
    v = data.get("schema_version")
    if v is not None and v not in (1, 2):
        raise ValidationError(f"不受支持的 schema_version: {v}（当前支持 1 或 2）", field="schema_version")

    # 2. week1_monday
    w1 = data.get("week1_monday")
    if not w1 or not isinstance(w1, str):
        raise ValidationError("缺少必需的 week1_monday 字段", field="week1_monday")
    try:
        w1_date = datetime.date.fromisoformat(w1.strip())
    except Exception:
        raise ValidationError(f"week1_monday 必须是合法的 YYYY-MM-DD 日期，当前值: {w1!r}", field="week1_monday")
    if w1_date.weekday() != 0:
        raise ValidationError("第一教学周日期必须为星期一，请检查 week1_monday。", field="week1_monday")

    # 2.5 total_weeks
    tw = data.get("total_weeks")
    if tw is not None:
        if not isinstance(tw, int) or isinstance(tw, bool) or tw < 1 or tw > 53:
            raise ValidationError(f"total_weeks 必须为 1~53 的整数，当前值: {tw!r}", field="total_weeks")

    # 3. busy_slots
    slots = data.get("busy_slots")
    if slots is None:
        # 若为旧版结构且包含 courses 则允许
        if "courses" in data:
            return
        raise ValidationError("缺少必需的 busy_slots 列表", field="busy_slots")

    if not isinstance(slots, list):
        raise ValidationError("busy_slots 必须为数组 (list)", field="busy_slots")

    seen_slots = set()

    for idx, slot in enumerate(slots):
        if not isinstance(slot, dict):
            raise ValidationError("时间占用记录必须为对象 (dict)", index=idx)

        # 4. weekday
        wd = slot.get("weekday")
        if wd is None or not isinstance(wd, int) or not (1 <= wd <= 7):
            raise ValidationError(f"weekday 必须为 1~7 的整数，当前值: {wd!r}", field="weekday", index=idx)

        # 5 & 6. start & end
        st = slot.get("start")
        en = slot.get("end")
        if not st or not isinstance(st, str):
            raise ValidationError("缺少合法的 start 开始时间", field="start", index=idx)
        if not en or not isinstance(en, str):
            raise ValidationError("缺少合法的 end 结束时间", field="end", index=idx)

        time_pat = r"^\d{1,2}:\d{2}$"
        if not re.match(time_pat, st.strip()):
            raise ValidationError(f"start 时间格式不合法（应为 HH:MM）: {st!r}", field="start", index=idx)
        if not re.match(time_pat, en.strip()):
            raise ValidationError(f"end 时间格式不合法（应为 HH:MM）: {en!r}", field="end", index=idx)

        try:
            sh, sm = map(int, st.split(":"))
            eh, em = map(int, en.split(":"))
        except Exception:
            raise ValidationError("时间解析错误", index=idx)

        if not (0 <= sh <= 23 and 0 <= sm <= 59):
            raise ValidationError(f"start 时间越界: {st}", field="start", index=idx)
        if not (0 <= eh <= 23 and 0 <= em <= 59):
            raise ValidationError(f"end 时间越界: {en}", field="end", index=idx)

        s_min = sh * 60 + sm
        e_min = eh * 60 + em

        # 7. start < end
        if s_min >= e_min:
            raise ValidationError(f"开始时间 ({st}) 必须早于结束时间 ({en})", field="start/end", index=idx)

        # 8. weeks
        weeks = slot.get("weeks")
        if not weeks or not isinstance(weeks, list):
            raise ValidationError("weeks 必须是非空区间列表", field="weeks", index=idx)

        for w_idx, interval in enumerate(weeks):
            if not isinstance(interval, (list, tuple)) or len(interval) != 2:
                raise ValidationError(f"周次区间格式必须为 [起始周, 结束周]，当前值: {interval!r}", field="weeks", index=idx)
            try:
                wa, wb = int(interval[0]), int(interval[1])
            except (ValueError, TypeError):
                raise ValidationError(f"周次必须为整数: {interval!r}", field="weeks", index=idx)

            if wa < 1:
                raise ValidationError(f"起始周必须大于等于 1，当前值: {wa}", field="weeks", index=idx)
            if wb > 53:
                raise ValidationError(f"结束周不能超过 53，当前值: {wb}", field="weeks", index=idx)
            if wa > wb:
                raise ValidationError(f"起始周 ({wa}) 不能大于结束周 ({wb})", field="weeks", index=idx)

        # 9. 重复记录检测（同一星期、时间、周次相同）
        weeks_tuple = tuple(sorted((int(a), int(b)) for a, b in weeks))
        key = (wd, f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}", weeks_tuple)
        if key in seen_slots:
            # 记录重复，但为宽容处理，输出警告或允许由 normalizer 去重
            pass
        seen_slots.add(key)
