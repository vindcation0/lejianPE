"""把 URP 导出的课表 markdown 转成 timetable.json（带周次）。"""
import re, json, os, sys, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def parse(md_path):
    lines = open(md_path, encoding="utf-8").read().splitlines()
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
                weeks = [(1, 16)]

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


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\vindacation\Downloads\course_schedule.md"
    week = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    courses = parse(src)
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    week1 = monday - datetime.timedelta(days=7 * (week - 1))
    
    # 构建 Schema v2 busy_slots
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
        "current_week": week,
        "week1_monday": week1.isoformat(),
        "busy_slots": busy_slots,
        "courses": courses  # 保持向下兼容视图
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"解析 {len(courses)} 条，生成 {len(busy_slots)} 条时间占用段，写入 {OUT}\n")
    for c in courses:
        wk = ",".join(f"{a}-{b}" for a, b in c["weeks"])
        print(f"  周{c['weekday']} {c['start']}-{c['end']}  [{wk}周]  {c['name']}")


if __name__ == "__main__":
    main()
