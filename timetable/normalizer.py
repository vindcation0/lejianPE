"""
数据标准化模块 (Normalizer)
===========================
将星期、时间、周次等各种输入格式转换为标准表示：
- 星期: 1~7 的整数
- 时间: 24 小时制 "HH:MM"
- 周次: 规范化闭区间集合 [[start, end], ...]
"""
import datetime
import re
from typing import Any, List, Union

CN_DAYS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
    "周一": 1, "周二": 2, "周三": 3, "周四": 4, "周五": 5, "周六": 6, "周日": 7, "周天": 7,
    "星期一": 1, "星期二": 2, "星期三": 3, "星期四": 4, "星期五": 5, "星期六": 6, "星期日": 7, "星期天": 7,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
    "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7
}


def normalize_weekday(val: Any) -> int:
    """标准化星期，返回 1..7 整数"""
    if isinstance(val, int) and 1 <= val <= 7:
        return val

    s = str(val).strip().lower()
    if s in CN_DAYS:
        return CN_DAYS[s]

    m = re.search(r"[一二三四五六日天1-7]", s)
    if m and m.group(0) in CN_DAYS:
        return CN_DAYS[m.group(0)]

    raise ValueError(f"无法识别的星期格式: {val!r}")


def normalize_time(val: Any) -> str:
    """标准化时间，返回 'HH:MM' 字符串"""
    s = str(val).strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        raise ValueError(f"时间格式不合法（应为 HH:MM）: {val!r}")

    h, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间数值越界 (00:00-23:59): {val!r}")

    return f"{h:02d}:{minute:02d}"


def merge_intervals(intervals: List[List[int]]) -> List[List[int]]:
    """合并排序重叠或相邻的闭区间"""
    if not intervals:
        return []

    # 规范化并排序
    cleaned = []
    for pair in intervals:
        if len(pair) != 2:
            raise ValueError(f"周次区间格式无效: {pair!r}")
        a, b = int(pair[0]), int(pair[1])
        if a > b:
            raise ValueError(f"周次区间起点大于终点: [{a}, {b}]")
        if a < 1:
            raise ValueError(f"周次必须大于等于 1: [{a}, {b}]")
        cleaned.append((a, b))

    cleaned = sorted(set(cleaned))
    merged = []
    for a, b in cleaned:
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))

    return [[x, y] for x, y in merged]


def normalize_weeks(weeks_spec: Any, total_weeks: int = 16) -> List[List[int]]:
    """
    将各种周次描述标准化为闭区间集合。
    支持:
    - "单周" / "odd"
    - "双周" / "even"
    - "每周" / "all"
    - "1-8周, 10-16周" 文本描述
    - 离散数字列表 [1, 3, 5]
    - 区间列表 [[1, 8], [10, 16]]
    """
    if isinstance(weeks_spec, str):
        s = weeks_spec.strip().lower()
        if s in ("单周", "单", "odd"):
            return [[w, w] for w in range(1, total_weeks + 1, 2)]
        if s in ("双周", "双", "even"):
            return [[w, w] for w in range(2, total_weeks + 1, 2)]
        if s in ("每周", "全周", "all", "every"):
            return [[1, total_weeks]]

        # 尝试提取 "1-16周" 或 "第1周, 第3周"
        pairs = []
        for a, b in re.findall(r"(\d{1,2})\s*-\s*(\d{1,2})", s):
            pairs.append([int(a), int(b)])
        for single in re.findall(r"(?:第|\b)(\d{1,2})(?:周|\b)", s):
            # 过滤掉已被区间匹配的单个数字
            val = int(single)
            if not any(a <= val <= b for a, b in pairs):
                pairs.append([val, val])

        if pairs:
            return merge_intervals(pairs)
        raise ValueError(f"无法解析的周次字符串: {weeks_spec!r}")

    if isinstance(weeks_spec, (list, tuple)):
        if not weeks_spec:
            raise ValueError("周次集合不能为空")

        # 判断是否为纯数字离散列表 [1, 2, 3]
        if all(isinstance(x, (int, str)) and str(x).isdigit() for x in weeks_spec):
            nums = sorted(set(int(x) for x in weeks_spec))
            intervals = []
            for n in nums:
                if intervals and n == intervals[-1][1] + 1:
                    intervals[-1][1] = n
                else:
                    intervals.append([n, n])
            return intervals

        # 默认为区间列表 [[1, 8], [9, 16]]
        return merge_intervals([[int(x[0]), int(x[1])] for x in weeks_spec])

    raise ValueError(f"不受支持的周次格式: {weeks_spec!r}")


def normalize_date(val: Any) -> str:
    """确保日期为 YYYY-MM-DD 格式"""
    if isinstance(val, (datetime.date, datetime.datetime)):
        return val.strftime("%Y-%m-%d")

    s = str(val).strip()
    try:
        d = datetime.date.fromisoformat(s)
        return d.isoformat()
    except Exception as e:
        raise ValueError(f"日期格式不合法 (应为 YYYY-MM-DD): {val!r}") from e
