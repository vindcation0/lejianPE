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
import lejian as L


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

    # ---------------------------------------------------- T22: 动态教学周计算
    def test_T22_dynamic_current_week_calculation(self):
        """T22: 动态教学周换算与边界测试 (基于 week1_monday 锚点与可注入日期)"""
        tt = {"week1_monday": "2026-08-31", "total_weeks": 16}

        # 核心锚点日期测试
        test_cases = [
            (datetime.date(2026, 8, 31), 1),
            (datetime.date(2026, 9, 1), 1),
            (datetime.date(2026, 9, 6), 1),
            (datetime.date(2026, 9, 7), 2),
            (datetime.date(2026, 10, 19), 8),
            (datetime.date(2026, 10, 26), 9),
            (datetime.date(2026, 11, 1), 9),
            (datetime.date(2026, 11, 2), 10),
        ]
        for dt, exp_week in test_cases:
            self.assertEqual(L.current_week(tt, today=dt), exp_week, f"Failed on {dt}")
            self.assertEqual(L.week_of(dt, tt), exp_week)

        # 边界与学期状态判断测试
        # 开学前
        before_term = datetime.date(2026, 8, 30)
        self.assertEqual(L.current_week(tt, today=before_term), 0)
        status, wk, tw = L.get_semester_status(tt, today=before_term)
        self.assertEqual(status, "尚未开学")
        self.assertEqual(wk, 0)
        self.assertEqual(tw, 16)

        # 正常教学期间
        mid_term = datetime.date(2026, 10, 28)
        self.assertEqual(L.current_week(tt, today=mid_term), 9)
        status, wk, tw = L.get_semester_status(tt, today=mid_term)
        self.assertEqual(status, "正常教学周")
        self.assertEqual(wk, 9)

        # 学期结束后
        after_term = datetime.date(2026, 12, 28)  # 超过 16 周
        self.assertEqual(L.current_week(tt, today=after_term), 18)
        status, wk, tw = L.get_semester_status(tt, today=after_term)
        self.assertEqual(status, "学期已结束")
        self.assertEqual(wk, 18)

        # 跨年测试
        cross_year = datetime.date(2027, 1, 4)
        self.assertEqual(L.current_week(tt, today=cross_year), 19)

        # 闰年测试 (2028 是闰年，2028-02-21 是周一)
        leap_tt = {"week1_monday": "2028-02-21", "total_weeks": 16}
        self.assertEqual(L.current_week(leap_tt, today=datetime.date(2028, 2, 21)), 1)
        self.assertEqual(L.current_week(leap_tt, today=datetime.date(2028, 2, 28)), 2)
        self.assertEqual(L.current_week(leap_tt, today=datetime.date(2028, 2, 29)), 2)
        self.assertEqual(L.current_week(leap_tt, today=datetime.date(2028, 3, 1)), 2)
        self.assertEqual(L.current_week(leap_tt, today=datetime.date(2028, 3, 6)), 3)

    # ---------------------------------------------------- T23: 旧配置兼容测试
    def test_T23_legacy_config_current_week_ignored(self):
        """T23: 旧配置 current_week:8 被完全忽略，当前周次由日期自动计算"""
        legacy_cfg = {"current_week": 8}
        tt = {"week1_monday": "2026-08-31", "total_weeks": 16}
        # 当日期为 2026-11-01 时，必须得到第 9 周，绝不能返回 8
        calc_week = L.current_week(legacy_cfg, tt, today=datetime.date(2026, 11, 1))
        self.assertEqual(calc_week, 9)

    # ---------------------------------------------------- T24: total_weeks 存取与旧版兼容
    def test_T24_total_weeks_persistence_and_defaults(self):
        """T24: total_weeks 的默认值、保存恢复与旧版兼容"""
        # 1. 默认总周数为 16
        empty_tt = Timetable(week1_monday="2026-08-31")
        self.assertEqual(empty_tt.total_weeks, 16)
        self.assertEqual(empty_tt.to_dict()["total_weeks"], 16)

        # 2. 保存 20 周后正确恢复
        tt_20 = Timetable(
            week1_monday="2026-08-31",
            total_weeks=20,
            busy_slots=[TimeSlot(weekday=1, start="08:00", end="09:35", weeks=[[1, 20]])]
        )
        save_timetable_file(self.test_json_path, tt_20, backup=False)
        loaded = load_timetable_file(self.test_json_path)
        self.assertEqual(loaded.total_weeks, 20)
        self.assertEqual(len(loaded.busy_slots), 1)
        self.assertEqual(loaded.busy_slots[0].weeks, [[1, 20]])

        # 3. 旧版没有 total_weeks 字段的 JSON 正常读取并默认 16
        old_no_tw = {
            "schema_version": 2,
            "week1_monday": "2026-08-31",
            "busy_slots": [{"weekday": 1, "start": "08:00", "end": "09:35", "weeks": [[1, 16]]}]
        }
        with open(self.test_json_path, "w", encoding="utf-8") as f:
            json.dump(old_no_tw, f, ensure_ascii=False)
        loaded_old = load_timetable_file(self.test_json_path)
        self.assertEqual(loaded_old.total_weeks, 16)

        # 4. 非法总周数拒绝保存 (0 或 54 或 非整数)
        for bad_tw in [0, -1, 54, "16"]:
            bad_data = {
                "schema_version": 2,
                "week1_monday": "2026-08-31",
                "total_weeks": bad_tw,
                "busy_slots": []
            }
            with self.assertRaises(ValidationError):
                validate_timetable_dict(bad_data)

    # ---------------------------------------------------- T25: 星期一严格校验
    def test_T25_week1_monday_validation(self):
        """T25: 校验第一教学周日期必须为星期一"""
        # 星期一合法: 2026-08-31 是周一
        valid_data = {
            "schema_version": 2,
            "week1_monday": "2026-08-31",
            "busy_slots": []
        }
        validate_timetable_dict(valid_data)

        # 星期二至星期日非法: 2026-09-01 (周二) ~ 2026-09-06 (周日)
        for d in range(1, 7):
            bad_date_str = f"2026-09-0{d}"
            bad_data = {
                "schema_version": 2,
                "week1_monday": bad_date_str,
                "busy_slots": []
            }
            with self.assertRaises(ValidationError) as ctx:
                validate_timetable_dict(bad_data)
            self.assertIn("第一教学周日期必须为星期一，请检查 week1_monday。", str(ctx.exception))

        # 非法日期格式
        with self.assertRaises(ValidationError):
            validate_timetable_dict({"schema_version": 2, "week1_monday": "2026/08/31", "busy_slots": []})

        # 保存非法星期一日期时原文件不受损
        good_tt = Timetable(week1_monday="2026-08-31", busy_slots=[])
        save_timetable_file(self.test_json_path, good_tt, backup=False)
        with open(self.test_json_path, "r", encoding="utf-8") as f:
            content_before = f.read()

        bad_tt_payload = {"schema_version": 2, "week1_monday": "2026-09-01", "busy_slots": []}
        with self.assertRaises(ValidationError):
            save_timetable_file(self.test_json_path, bad_tt_payload, backup=False)

        with open(self.test_json_path, "r", encoding="utf-8") as f:
            content_after = f.read()
        self.assertEqual(content_before, content_after)

    # ---------------------------------------------------- T26: 菜单 2 与菜单 6 职责分离测试
    def test_T26_cmd_list_cmd_timetable_output_separation(self):
        """T26: 验证菜单 2 (cmd_list) 与菜单 6 (cmd_timetable) 的职责分离与输出格式"""
        from unittest.mock import patch

        fake_cfg = {"max_normal": 2, "targets": ["羽毛球"]}
        fake_tt = {
            "week1_monday": "2026-08-31",
            "total_weeks": 16,
            "busy_slots": [
                {"weekday": 1, "start": "08:00", "end": "09:35", "weeks": [[1, 16]], "label": "大学英语"}
            ]
        }
        fake_activities = [
            {
                "id": "act1", "name": "羽毛球1班", "isRegister": False,
                "startTime": int(datetime.datetime(2026, 11, 2, 14, 0).timestamp() * 1000),
                "endTime": int(datetime.datetime(2026, 11, 2, 15, 35).timestamp() * 1000),
                "limitPeopleNumber": 30, "registerPeople": 10,
                "startRegisterTime": int(datetime.datetime(2026, 11, 1, 14, 0).timestamp() * 1000)
            }
        ]

        # 1. 验证 cmd_list 输出
        logs_list = []
        with patch("lejian.load_json", side_effect=lambda p, d=None: fake_cfg if "config" in p else {}), \
             patch("lejian.load_timetable", return_value=fake_tt), \
             patch("lejian.Client.activities", return_value=fake_activities), \
             patch("lejian.Client.ensure_token", return_value=True), \
             patch("lejian.log", side_effect=lambda msg: logs_list.append(msg)):
            L.cmd_list()

        list_output = "\n".join(logs_list)
        self.assertIn("活动总数", list_output)
        self.assertIn("教学周", list_output)
        self.assertIn("符合条件可抢", list_output)
        self.assertIn("羽毛球1班", list_output)
        self.assertNotIn("<- 大学英语", list_output)
        self.assertNotIn("已占用", list_output)

        # 2. 验证 cmd_timetable 输出
        logs_tt = []
        with patch("lejian.load_json", side_effect=lambda p, d=None: fake_cfg if "config" in p else {}), \
             patch("lejian.load_timetable", return_value=fake_tt), \
             patch("lejian.Client.activities", return_value=[]), \
             patch("lejian.Client.ensure_token", return_value=True), \
             patch("lejian.log", side_effect=lambda msg: logs_tt.append(msg)):
            L.cmd_timetable()

        tt_output = "\n".join(logs_tt)
        self.assertIn("课表与周次", tt_output)
        self.assertIn("第一教学周周一：2026-08-31", tt_output)
        self.assertIn("学期总周数：16", tt_output)
        self.assertIn("当前日期：", tt_output)
        self.assertIn("当前教学周：", tt_output)
        self.assertIn("---------------- 当前周 ----------------", tt_output)
        self.assertIn("---------------- 下一周 ----------------", tt_output)
        self.assertIn("周一 08:00~09:35 <- 大学英语", tt_output)
        self.assertNotIn("符合条件可抢", tt_output)


if __name__ == "__main__":
    unittest.main()
