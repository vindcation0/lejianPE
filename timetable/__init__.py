"""
乐健体育 · 课表时间占用数据模块 (Timetable & BusySlots)
======================================================
将课表由「课程信息模型」转为「时间占用模型 (TimeSlot)」，服务于抢课时间冲突检测。
"""
from .models import TimeSlot, Timetable
from .validator import ValidationError, validate_timetable_dict
from .normalizer import normalize_weekday, normalize_time, normalize_weeks, merge_intervals
from .compatibility import load_timetable_file, save_timetable_file, migrate_timetable_file
