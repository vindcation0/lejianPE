"""
兼容与迁移模块 (Compatibility)
==============================
处理旧版课表 (Schema v1, courses) 与新版 (Schema v2, busy_slots) 的双向兼容、
格式自动探测、安全迁移以及带备份的原子保存。
"""
import datetime
import json
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple, Union

from .models import TimeSlot, Timetable
from .normalizer import merge_intervals, normalize_time, normalize_weekday, normalize_weeks
from .validator import ValidationError, validate_timetable_dict


def from_schema1_course(c: Dict[str, Any]) -> TimeSlot:
    """从旧版课程字典提取纯时间占用记录，舍弃所有无关元数据"""
    weekday = normalize_weekday(c.get("weekday"))
    start = normalize_time(c.get("start"))
    end = normalize_time(c.get("end"))
    raw_weeks = c.get("weeks") or c.get("raw_weeks") or [[1, 16]]
    weeks = normalize_weeks(raw_weeks)
    return TimeSlot(weekday=weekday, start=start, end=end, weeks=weeks)


def from_schema1_dict(data: Dict[str, Any]) -> Timetable:
    """将旧版完整课表数据转为新版 Timetable 对象"""
    w1 = data.get("week1_monday")
    if not w1:
        # 若缺失则根据当前时间推算
        cw = int(data.get("current_week") or 8)
        today = datetime.date.today()
        monday = today - datetime.timedelta(days=today.weekday())
        w1 = (monday - datetime.timedelta(days=7 * (cw - 1))).isoformat()

    slots: List[TimeSlot] = []
    raw_courses = data.get("courses")
    if "courses" in data and not isinstance(raw_courses, list):
        raise ValidationError("courses 必须为列表")

    for idx, c in enumerate(raw_courses or []):
        try:
            slot = from_schema1_course(c)
            slots.append(slot)
        except Exception as e:
            # 严格校验：不允许静默忽略错误记录
            raise ValidationError(f"解析课程记录失败: {e}", index=idx) from e

    tt = Timetable(week1_monday=w1, busy_slots=slots, schema_version=2)
    tt.deduplicate()
    return tt


def load_timetable_file(path: str) -> Timetable:
    """
    自动识别旧版 (v1) 与新版 (v2) 课表并返回统一的 Timetable 对象。
    若文件不存在则返回默认空 Timetable。
    """
    if not os.path.exists(path):
        today = datetime.date.today()
        monday = today - datetime.timedelta(days=today.weekday())
        return Timetable(week1_monday=monday.isoformat(), busy_slots=[], schema_version=2)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValidationError(f"课表文件内容非合法字典: {path}")

    # 优先识别新版 busy_slots
    if "busy_slots" in data:
        validate_timetable_dict(data)
        return Timetable.from_dict(data)

    # 兼容识别旧版 courses
    if "courses" in data:
        return from_schema1_dict(data)

    raise ValidationError(f"课表文件既不含 busy_slots 也不含 courses: {path}")


def save_timetable_file(
    path: str,
    tt: Union[Timetable, Dict[str, Any]],
    backup: bool = True
) -> None:
    """
    安全保存课表：
    1. 转换为标准结构并进行全量校验
    2. 校验通过后创建 .bak 备份
    3. 通过临时文件原子替换，防止断电导致文件损坏
    """
    if isinstance(tt, Timetable):
        d = tt.to_dict()
    elif isinstance(tt, dict):
        d = dict(tt)
        if "schema_version" not in d:
            d["schema_version"] = 2
    else:
        raise ValidationError("保存对象必须为 Timetable 实例或字典")

    # 严格校验
    validate_timetable_dict(d)

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    # 备份已有旧文件
    if backup and os.path.exists(path):
        bak_path = path + ".bak"
        try:
            shutil.copy2(path, bak_path)
        except Exception as e:
            raise IOError(f"创建备份文件失败 ({bak_path}): {e}") from e

    # 原子写入
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise IOError(f"写入课表文件失败: {e}") from e


def migrate_timetable_file(path: str, backup: bool = True) -> Tuple[bool, str]:
    """
    对现有课表文件执行就地迁移升级：
    - 若已是 v2 则直接返回
    - 若是 v1 则备份并转为 v2，校验失败绝不覆盖旧文件
    """
    if not os.path.exists(path):
        return False, f"文件不存在: {path}"

    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except Exception as e:
            return False, f"JSON 解析失败: {e}"

    if not isinstance(data, dict):
        return False, "JSON 根节点非字典"

    if data.get("schema_version") == 2 and "busy_slots" in data:
        return True, "已经是 Schema v2 格式，无需迁移"

    if "courses" not in data:
        return False, "未识别到 courses 字段，无法迁移"

    try:
        new_tt = from_schema1_dict(data)
        save_timetable_file(path, new_tt, backup=backup)
        return True, f"已成功将 {len(data.get('courses', []))} 门课程转换为 {len(new_tt.busy_slots)} 条时间占用记录"
    except Exception as e:
        return False, f"迁移失败: {e}"
