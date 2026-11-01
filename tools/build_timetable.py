"""把 URP 导出的课表 markdown 转成 timetable.json（带周次与安全校验）。"""
import argparse
import datetime
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from timetable.compatibility import save_timetable_file
from timetable.validator import ValidationError

OUT = os.path.join(ROOT, "timetable.json")
DAYS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}


def merge(ranges):
    ranges = sorted(set(ranges))
    out = []
    for a, b in ranges:
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return [list(x) for x in out]


def hhmm_to_min(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def parse(md_path, total_weeks=16):
    if not os.path.exists(md_path):
        raise FileNotFoundError(f"Markdown 课表文件不存在: {md_path}")

    with open(md_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    courses, cur_day, i = [], None, 0

    while i < len(lines):
        line = lines[i]
        m = re.match(r"^###\s*星期([一二三四五六日天])", line)
        if m:
            cur_day = DAYS[m.group(1)]
            i += 1
            continue

        m = re.match(r"^-\s*\*\*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", line)
        if m and cur_day:
            start, end = m.group(1), m.group(2)
            block, j = [line], i + 1
            while j < len(lines):
                nxt = lines[j]
                if re.match(r"^###", nxt) or re.match(r"^-\s*\*\*\d{1,2}:\d{2}", nxt) \
                        or nxt.strip().startswith("---"):
                    break
                if nxt.strip():
                    block.append(nxt)
                j += 1

            text = "\n".join(block)
            body = "\n".join(block[1:])

            weeks = []
            for a, b in re.findall(r"(\d{1,2})\s*-\s*(\d{1,2})\s*周", body):
                weeks.append((int(a), int(b)))
            for a in re.findall(r"第\s*(\d{1,2})\s*周", body):
                weeks.append((int(a), int(a)))
            if not weeks:
                weeks = [(1, total_weeks)]

            bolds = [b.strip() for b in re.findall(r"\*\*([^*]+)\*\*", text)]
            name = next((b for b in bolds
                         if not re.search(r"\d{1,2}:\d{2}", b) and "周" not in b), "")
            if not name:
                name = next((b for b in bolds if not re.search(r"\d{1,2}:\d{2}", b)),
                            f"{start}-{end}")

            courses.append({
                "name": name,
                "weekday": cur_day,
                "start": start,
                "end": end,
                "weeks": merge(weeks),
                "raw_weeks": sorted(set(weeks)),
            })
            i = j
            continue

        i += 1

    return courses


def build_timetable(src, week1_monday=None, current_week=None, total_weeks=16,
                    out_path=OUT, today=None):
    """
    转换 Markdown 为 Schema v2 课表并原子保存。
    返回 (success: bool, message: str, data: dict)。
    """
    if not src or not os.path.exists(src):
        raise FileNotFoundError(f"Markdown 课表文件不存在: {src}")

    if total_weeks < 1 or total_weeks > 53:
        raise ValueError(f"学期总周数超出合法范围 (1~53): {total_weeks}")

    if week1_monday and current_week:
        raise ValueError("参数冲突: --week1-monday 与 --current-week 互斥，请仅指定其中之一")

    if not week1_monday and current_week is None:
        raise ValueError("缺少日期锚点: 必须指定 --week1-monday 或 --current-week")

    if current_week is not None:
        if current_week < 1:
            raise ValueError(f"当前教学周不合法 (必须 >= 1): {current_week}")
        ref_today = today if today is not None else datetime.date.today()
        monday = ref_today - datetime.timedelta(days=ref_today.weekday())
        week1_date = monday - datetime.timedelta(days=7 * (current_week - 1))
        calculated_w1 = week1_date.isoformat()
    else:
        calculated_w1 = str(week1_monday).strip()
        try:
            w1_dt = datetime.date.fromisoformat(calculated_w1)
        except Exception:
            raise ValueError(f"第一教学周日期格式错误 (需 YYYY-MM-DD): {calculated_w1}")
        if w1_dt.weekday() != 0:
            raise ValueError(f"第一教学周日期必须为星期一，当前日期 {calculated_w1} 是星期{w1_dt.weekday() + 1}")

    courses = parse(src, total_weeks=total_weeks)
    if not courses:
        raise ValueError(f"解析结果为空，未在 {src} 中提取到任何有效课程记录")

    busy_slots = []
    for c in courses:
        busy_slots.append({
            "weekday": c["weekday"],
            "start": c["start"],
            "end": c["end"],
            "weeks": c["weeks"]
        })

    data = {
        "schema_version": 2,
        "source": os.path.basename(src),
        "week1_monday": calculated_w1,
        "total_weeks": total_weeks,
        "busy_slots": busy_slots,
        "courses": courses  # 保持向下兼容视图
    }

    save_timetable_file(out_path, data, backup=True)
    return True, f"成功生成 {len(busy_slots)} 条时间占用段并保存至 {out_path}", data


def main():
    parser = argparse.ArgumentParser(
        description="把 URP 导出的课表 Markdown 转成带周次与校验的 timetable.json"
    )
    parser.add_argument("src", nargs="?", default=None, help="Markdown 课表文件路径")
    parser.add_argument("pos_week", nargs="?", type=int, default=None, help="兼容旧命令行位置参数：当前教学周")
    parser.add_argument("--src", dest="src_opt", default=None, help="Markdown 课表文件路径（选项形式）")

    anchor_group = parser.add_mutually_exclusive_group()
    anchor_group.add_argument("--week1-monday", dest="week1_monday", default=None,
                              help="第一教学周周一日期 (YYYY-MM-DD)")
    anchor_group.add_argument("--current-week", dest="current_week", type=int, default=None,
                              help="当前日期对应的教学周 (>=1)")

    parser.add_argument("--total-weeks", dest="total_weeks", type=int, default=16,
                        help="学期总周数 (1~53，默认 16)")
    parser.add_argument("--out", dest="out", default=OUT,
                        help="输出文件路径，默认 timetable.json")

    args = parser.parse_args()

    input_src = args.src_opt or args.src
    if not input_src:
        default_down = os.path.expanduser(r"~\Downloads\course_schedule.md")
        if os.path.exists(default_down):
            input_src = default_down
        elif os.path.exists("course_schedule.md"):
            input_src = "course_schedule.md"
        else:
            print("[错误] 未指定输入文件，且默认路径未找到 course_schedule.md\n用法: python tools/build_timetable.py <path/to/schedule.md> --week1-monday YYYY-MM-DD")
            sys.exit(1)

    # 兼容位置参数周次
    cw = args.current_week
    if cw is None and not args.week1_monday and args.pos_week is not None:
        cw = args.pos_week

    try:
        ok, msg, data = build_timetable(
            src=input_src,
            week1_monday=args.week1_monday,
            current_week=cw,
            total_weeks=args.total_weeks,
            out_path=args.out
        )
        print(f"\n[成功] {msg}")
        print(f"第一教学周周一：{data['week1_monday']}")
        print(f"学期总周数：{data['total_weeks']}")
        print(f"生成时间占用段：{len(data['busy_slots'])} 条\n")
        for c in data.get("courses", []):
            wk = ",".join(f"{a}-{b}" for a, b in c["weeks"])
            print(f"  周{c['weekday']} {c['start']}-{c['end']}  [{wk}周]  {c['name']}")
    except (FileNotFoundError, ValueError, ValidationError) as e:
        print(f"\n[错误] 课表转换失败: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n[未知错误] 课表生成异常: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
