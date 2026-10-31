"""
课表与时间占用数据模型测试套件 (tests/test_timetable.py)
======================================================
覆盖《统一时间占用数据结构改造执行说明》第十节要求的全量测试场景 (T01 ~ T18)，
以及可视化编辑器 HTTP API 接口集成测试。
"""
import copy
import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import timetable
from timetable.models import TimeSlot, Timetable
from timetable.normalizer import (
    merge_intervals,
    normalize_date,
    normalize_time,
    normalize_weekday,
    normalize_weeks,
)
from timetable.validator import ValidationError, validate_timetable_dict
from timetable.compatibility import (
    from_schema1_course,
    from_schema1_dict,
    load_timetable_file,
    migrate_timetable_file,
    save_timetable_file,
)
from tools.editor_server import EditorServer, find_free_port


class TimetableTestSuite(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.test_json_path = os.path.join(self.tmp_dir, "test_timetable.json")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # ---------------------------------------------------- T01
    def test_T01_single_course_conversion(self):
        """T01: 正常单门课程时间转换 -> 正确生成时间段"""
        course = {
            "name": "大学体育III_24",
            "weekday": 1,
            "start": "14:00",
            "end": "15:35",
            "weeks": [[1, 16]]
        }
        slot = from_schema1_course(course)
        self.assertEqual(slot.weekday, 1)
        self.assertEqual(slot.start, "14:00")
        self.assertEqual(slot.end, "15:35")
        self.assertEqual(slot.weeks, [[1, 16]])
        self.assertEqual(slot.start_min, 14 * 60)
        self.assertEqual(slot.end_min, 15 * 60 + 35)

    # ---------------------------------------------------- T02
    def test_T02_multiple_courses_conversion(self):
        """T02: 多门课程转换 -> 生成多个时间段"""
        data = {
            "week1_monday": "2026-08-31",
            "courses": [
                {"name": "高等数学", "weekday": 1, "start": "08:00", "end": "09:35", "weeks": [[1, 16]]},
                {"name": "大学英语", "weekday": 2, "start": "10:00", "end": "11:35", "weeks": [[1, 8]]},
                {"name": "线性代数", "weekday": 3, "start": "14:00", "end": "15:35", "weeks": [[2, 16]]}
            ]
        }
        tt = from_schema1_dict(data)
        self.assertEqual(len(tt.busy_slots), 3)
        self.assertEqual([s.weekday for s in tt.busy_slots], [1, 2, 3])

    # ---------------------------------------------------- T03
    def test_T03_ignore_course_name(self):
        """T03: 忽略课程名称 -> 不影响时间数据"""
        c1 = {"name": "课程A", "weekday": 1, "start": "08:00", "end": "09:35", "weeks": [[1, 16]]}
        c2 = {"name": "课程B_完全不同的名字", "weekday": 1, "start": "08:00", "end": "09:35", "weeks": [[1, 16]]}
        s1 = from_schema1_course(c1)
        s2 = from_schema1_course(c2)
        self.assertEqual(s1.to_dict(), s2.to_dict())

    # ---------------------------------------------------- T04
    def test_T04_ignore_teacher_metadata(self):
        """T04: 忽略教师姓名及无关元数据 -> 不影响时间数据"""
        c1 = {
            "name": "物理实验", "teacher": "张教授", "room": "理学楼101",
            "credit": 3, "weekday": 4, "start": "14:00", "end": "16:00", "weeks": [[1, 8]]
        }
        s1 = from_schema1_course(c1)
        d = s1.to_dict()
        self.assertNotIn("teacher", d)
        self.assertNotIn("room", d)
        self.assertNotIn("credit", d)
        self.assertEqual(d["weekday"], 4)
        self.assertEqual(d["start"], "14:00")

    # ---------------------------------------------------- T05
    def test_T05_weekday_mapping(self):
        """T05: 周一至周日映射 -> 正确转换 1~7 整数"""
        cases = [
            ("周一", 1), ("星期一", 1), (1, 1), ("1", 1),
            ("周二", 2), ("星期二", 2), (2, 2),
            ("周三", 3), ("周四", 4), ("周五", 5),
            ("周六", 6), ("周日", 7), ("周天", 7), ("星期天", 7)
        ]
        for inp, exp in cases:
            self.assertEqual(normalize_weekday(inp), exp, f"Failed on {inp}")

    # ---------------------------------------------------- T06
    def test_T06_time_parsing(self):
        """T06: 具体时间解析 -> 统一转为 24小时制 HH:MM"""
        self.assertEqual(normalize_time("8:00"), "08:00")
        self.assertEqual(normalize_time("08:00"), "08:00")
        self.assertEqual(normalize_time(" 14:05 "), "14:05")
        self.assertEqual(normalize_time("23:59"), "23:59")

    # ---------------------------------------------------- T07
    def test_T07_odd_even_weeks_parsing(self):
        """T07: 单双周解析 -> 保留有效周次区间"""
        odd_weeks = normalize_weeks("单周", total_weeks=8)
        self.assertEqual(odd_weeks, [[1, 1], [3, 3], [5, 5], [7, 7]])

        even_weeks = normalize_weeks("双周", total_weeks=8)
        self.assertEqual(even_weeks, [[2, 2], [4, 4], [6, 6], [8, 8]])

    # ---------------------------------------------------- T08
    def test_T08_discontinuous_weeks(self):
        """T08: 不连续周次 -> 正确转换与合并区间"""
        weeks = normalize_weeks([[1, 4], [8, 12]])
        self.assertEqual(weeks, [[1, 4], [8, 12]])

        # 重叠相邻合并
        merged = merge_intervals([[1, 4], [5, 8], [10, 12]])
        self.assertEqual(merged, [[1, 8], [10, 12]])

    # ---------------------------------------------------- T09
    def test_T09_invalid_time_fails(self):
        """T09: 不合法时间 -> 校验失败"""
        # 开始时间晚于结束时间
        bad1 = {"schema_version": 2, "week1_monday": "2026-08-31",
                "busy_slots": [{"weekday": 1, "start": "10:00", "end": "08:00", "weeks": [[1, 16]]}]}
        with self.assertRaises(ValidationError) as ctx:
            validate_timetable_dict(bad1)
        self.assertIn("必须早于结束时间", str(ctx.exception))

        # 小时或分钟不合法
        bad2 = {"schema_version": 2, "week1_monday": "2026-08-31",
                "busy_slots": [{"weekday": 1, "start": "25:00", "end": "26:00", "weeks": [[1, 16]]}]}
        with self.assertRaises(ValidationError):
            validate_timetable_dict(bad2)

    # ---------------------------------------------------- T10
    def test_T10_invalid_weekday_fails(self):
        """T10: 不合法星期 -> 校验失败"""
        bad = {"schema_version": 2, "week1_monday": "2026-08-31",
               "busy_slots": [{"weekday": 8, "start": "08:00", "end": "09:35", "weeks": [[1, 16]]}]}
        with self.assertRaises(ValidationError) as ctx:
            validate_timetable_dict(bad)
        self.assertIn("weekday 必须为 1~7", str(ctx.exception))

    # ---------------------------------------------------- T11
    def test_T11_invalid_weeks_fails(self):
        """T11: 不合法周次 -> 校验失败"""
        # 起始周大于结束周
        bad1 = {"schema_version": 2, "week1_monday": "2026-08-31",
                "busy_slots": [{"weekday": 1, "start": "08:00", "end": "09:35", "weeks": [[10, 5]]}]}
        with self.assertRaises(ValidationError) as ctx:
            validate_timetable_dict(bad1)
        self.assertIn("不能大于结束周", str(ctx.exception))

        # 周次为空
        bad2 = {"schema_version": 2, "week1_monday": "2026-08-31",
                "busy_slots": [{"weekday": 1, "start": "08:00", "end": "09:35", "weeks": []}]}
        with self.assertRaises(ValidationError):
            validate_timetable_dict(bad2)

    # ---------------------------------------------------- T12
    def test_T12_schema1_json_migration(self):
        """T12: 旧版 JSON 迁移 -> 时间信息不丢失并生成备份"""
        old_data = {
            "source": "old.md",
            "current_week": 8,
            "week1_monday": "2026-08-31",
            "courses": [
                {"name": "国家安全教育", "weekday": 2, "start": "08:00", "end": "09:35", "weeks": [[1, 8]]},
                {"name": "大学英语", "weekday": 2, "start": "09:55", "end": "11:30", "weeks": [[1, 16]]}
            ]
        }
        with open(self.test_json_path, "w", encoding="utf-8") as f:
            json.dump(old_data, f, ensure_ascii=False, indent=2)

        ok, msg = migrate_timetable_file(self.test_json_path, backup=True)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(self.test_json_path + ".bak"))

        new_tt = load_timetable_file(self.test_json_path)
        self.assertEqual(new_tt.schema_version, 2)
        self.assertEqual(len(new_tt.busy_slots), 2)
        self.assertEqual(new_tt.busy_slots[0].weekday, 2)
        self.assertEqual(new_tt.busy_slots[0].start, "08:00")
        self.assertEqual(new_tt.busy_slots[0].weeks, [[1, 8]])

    # ---------------------------------------------------- T13
    def test_T13_schema2_json_read(self):
        """T13: 新版 JSON 读取 -> 正常读取"""
        new_data = {
            "schema_version": 2,
            "week1_monday": "2026-08-31",
            "busy_slots": [
                {"weekday": 1, "start": "08:00", "end": "09:35", "weeks": [[1, 16]]}
            ]
        }
        with open(self.test_json_path, "w", encoding="utf-8") as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2)

        tt = load_timetable_file(self.test_json_path)
        self.assertEqual(tt.schema_version, 2)
        self.assertEqual(len(tt.busy_slots), 1)
        self.assertEqual(tt.busy_slots[0].start, "08:00")

    # ---------------------------------------------------- T14
    def test_T14_overlap_detection(self):
        """T14: 时间段重叠 -> 正确检测冲突"""
        slot = TimeSlot(weekday=1, start="14:00", end="15:35", weeks=[[1, 16]])
        # 14:30~16:00 与 14:00~15:35 重叠
        self.assertTrue(slot.conflict_with(week_num=5, weekday=1, start_min=14 * 60 + 30, end_min=16 * 60))
        # 15:35~17:00 紧挨着，不重叠
        self.assertFalse(slot.conflict_with(week_num=5, weekday=1, start_min=15 * 60 + 35, end_min=17 * 60))

    # ---------------------------------------------------- T15
    def test_T15_different_weekday_no_conflict(self):
        """T15: 星期不同 -> 不产生冲突"""
        slot = TimeSlot(weekday=1, start="14:00", end="15:35", weeks=[[1, 16]])
        # 周二同时间段不冲突
        self.assertFalse(slot.conflict_with(week_num=5, weekday=2, start_min=14 * 60, end_min=15 * 60 + 35))

    # ---------------------------------------------------- T16
    def test_T16_week_mismatch_no_conflict(self):
        """T16: 教学周不匹配 -> 不产生冲突 (例如第1-8周的课，第9周不冲突)"""
        slot = TimeSlot(weekday=2, start="08:00", end="09:35", weeks=[[1, 8]])
        # 第 8 周冲突
        self.assertTrue(slot.conflict_with(week_num=8, weekday=2, start_min=8 * 60, end_min=9 * 60 + 35))
        # 第 9 周结课，不冲突！
        self.assertFalse(slot.conflict_with(week_num=9, weekday=2, start_min=8 * 60, end_min=9 * 60 + 35))

    # ---------------------------------------------------- T17
    def test_T17_same_weekday_same_week_overlap_conflicts(self):
        """T17: 同星期、同周次、时间重叠 -> 产生冲突"""
        tt = Timetable(
            week1_monday="2026-08-31",
            busy_slots=[
                TimeSlot(weekday=3, start="14:00", end="15:35", weeks=[[1, 16]])
            ]
        )
        has_conf = tt.has_conflict(week_num=8, weekday=3, start_min=15 * 60, end_min=16 * 60)
        self.assertTrue(has_conf)

    # ---------------------------------------------------- T18
    def test_T18_migration_failure_preserves_old_file(self):
        """T18: 迁移校验失败 -> 旧文件保持不变，绝不覆盖破坏"""
        bad_old = {
            "source": "bad.md",
            "week1_monday": "2026-08-31",
            "courses": [
                # 缺失合法时间
                {"name": "坏课程", "weekday": 1, "start": "非法时间", "end": "09:35", "weeks": [[1, 16]]}
            ]
        }
        with open(self.test_json_path, "w", encoding="utf-8") as f:
            json.dump(bad_old, f, ensure_ascii=False, indent=2)

        with open(self.test_json_path, "r", encoding="utf-8") as f:
            content_before = f.read()

        ok, msg = migrate_timetable_file(self.test_json_path)
        self.assertFalse(ok)

        # 坏数据应该拒绝迁移或保持原样
        with open(self.test_json_path, "r", encoding="utf-8") as f:
            content_after = f.read()
        self.assertEqual(content_before, content_after)

    # ---------------------------------------------------- T19 & T20 & T21: Web Editor API
    def test_T19_T20_T21_web_editor_api(self):
        """T19-T21: 可视化编辑器本地 API 测试 (鉴权、读取、保存)"""
        import threading
        # 准备一个临时的初始课表文件
        initial_data = {
            "schema_version": 2,
            "week1_monday": "2026-08-31",
            "busy_slots": [
                {"weekday": 2, "start": "10:00", "end": "11:35", "weeks": [[1, 16]]}
            ]
        }
        with open(self.test_json_path, "w", encoding="utf-8") as f:
            json.dump(initial_data, f, ensure_ascii=False, indent=2)

        port = find_free_port(19200)
        token = "test_token_123456"
        server = EditorServer(("127.0.0.1", port), token, timetable_path=self.test_json_path)
        t = threading.Thread(target=server.serve_forever)
        t.daemon = True
        t.start()

        try:
            base_url = f"http://127.0.0.1:{port}"

            # T21: 无 Token 访问报 403
            req = urllib.request.Request(f"{base_url}/api/load")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 403)

            # T19: 带 Token 正常读取 GET /api/load
            req_ok = urllib.request.Request(f"{base_url}/api/load?token={token}")
            with urllib.request.urlopen(req_ok) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIn("week1_monday", data)

            # T20: 带 Token 保存 POST /api/save
            payload = {
                "schema_version": 2,
                "week1_monday": "2026-08-31",
                "busy_slots": [
                    {"weekday": 1, "start": "08:00", "end": "09:35", "weeks": [[1, 16]]}
                ]
            }
            save_req = urllib.request.Request(
                f"{base_url}/api/save?token={token}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(save_req) as resp:
                self.assertEqual(resp.status, 200)
                res = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(res["success"])
                self.assertEqual(res["count"], 1)

        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
