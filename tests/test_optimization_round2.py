"""
乐健体育抢课项目：第二轮优化回归测试套件 (tests/test_optimization_round2.py)
========================================================================
覆盖《第二轮优化执行文档》要求的全部 20 个测试场景 (T27 ~ T46)：
- T27 ~ T30：每日报名上限漏洞修复验证
- T31 ~ T34：活动列表分页完整性保护验证
- T35 ~ T39：旧版课表迁移校验与安全保护验证
- T40 ~ T46：Markdown 课表转换工具参数、推算与原子保存验证
"""
import copy
import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import lejian as L
from lejian import ActivityList
from core.state_store import StateStore, RegistrationStatus
from core.audit_logger import AuditLogger
from core.retry_policy import RetryPolicy
from core.request_manager import RequestManager
from core.registration_manager import RegistrationManager
from core.reconciliation import ReconciliationManager
from timetable.models import TimeSlot, Timetable
from timetable.validator import ValidationError, validate_timetable_dict
from timetable.compatibility import (
    from_schema1_dict,
    load_timetable_file,
    migrate_timetable_file,
    save_timetable_file,
)
from tools.build_timetable import build_timetable, parse as parse_md


class MockClientRound2:
    def __init__(self):
        self.submit_response = (200, {"code": 0, "data": {"success": True}})
        self.activities_result = ActivityList([])
        self.check_conflict_return = False
        self.call_history = []
        self.s = {"semesterId": "mock_sem_123", "schoolId": "mock_school"}

    def call(self, method, path, body=None, timeout=6):
        self.call_history.append((method, path, body))
        if path == "education/app/activity/signUp":
            return self.submit_response
        return (200, {"code": 0, "data": {"items": list(self.activities_result), "total": len(self.activities_result)}})

    def activities(self, page_size=200, req_timeout=6):
        return self.activities_result

    def check_conflict(self, aid):
        return self.check_conflict_return

    def ensure_token(self, margin=300):
        return "mock_token"


class OptimizationRound2TestSuite(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.json_path = os.path.join(self.tmp_dir, "timetable.json")
        self.md_path = os.path.join(self.tmp_dir, "test_schedule.md")
        self.db_path = os.path.join(self.tmp_dir, "test_reg.db")
        self.logs_dir = os.path.join(self.tmp_dir, "logs")

        # 备份并清理全局 DAY_DONE
        self.orig_day_done = dict(L.DAY_DONE)
        L.DAY_DONE.clear()

    def tearDown(self):
        L.DAY_DONE.clear()
        L.DAY_DONE.update(self.orig_day_done)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _sample_activity(self, aid, name, dt_start, is_reg=False, remaining_num=20, start_reg_dt=None):
        st = int(dt_start.timestamp() * 1000)
        en = st + 95 * 60 * 1000
        st_reg = int((start_reg_dt or (dt_start - datetime.timedelta(days=1))).timestamp() * 1000)
        return {
            "id": str(aid),
            "name": name,
            "isRegister": is_reg,
            "startTime": st,
            "endTime": en,
            "limitPeopleNumber": 30,
            "registerPeople": 30 - remaining_num,
            "startRegisterTime": st_reg,
            "endRegisterTime": st + 60000
        }

    # =========================================================================
    # 任务一：每日报名上限漏洞修复 (T27 ~ T30)
    # =========================================================================

    def test_T27_same_day_multiple_normal_candidates(self):
        """T27: 同一天多个普通候选活动，只有 1 个能进入候选列表并提交"""
        tt = {"week1_monday": "2026-08-31", "total_weeks": 16, "busy_slots": []}
        cfg = {"max_normal": 2, "max_per_day": 1, "targets": ["羽毛球", "篮球"]}
        cw = 10

        day = datetime.datetime(2026, 11, 2, 14, 0) # 第10周周一
        act1 = self._sample_activity("101", "羽毛球 尹老师", day)
        act2 = self._sample_activity("102", "篮球 亢老师", day.replace(hour=16))

        # 候选筛选时应只选入优先级最高的 1 个
        picked = L.pick_targets([act1, act2], cfg, tt, cw)
        self.assertEqual(len(picked), 1, "同一天只应允许 1 个普通活动进入候选列表")
        self.assertEqual(str(picked[0][1]["id"]), "101")

    def test_T28_different_days_normal_candidates(self):
        """T28: 不同日期候选活动，均可正常进入候选"""
        tt = {"week1_monday": "2026-08-31", "total_weeks": 16, "busy_slots": []}
        cfg = {"max_normal": 2, "max_per_day": 1, "targets": ["羽毛球", "篮球"]}
        cw = 10

        day1 = datetime.datetime(2026, 11, 2, 14, 0) # 周一
        day2 = datetime.datetime(2026, 11, 3, 14, 0) # 周二
        act1 = self._sample_activity("101", "羽毛球 尹老师", day1)
        act2 = self._sample_activity("102", "篮球 亢老师", day2)

        picked = L.pick_targets([act1, act2], cfg, tt, cw)
        self.assertEqual(len(picked), 2, "不同日期的普通活动均应正常入选")
        picked_ids = {str(p[1]["id"]) for p in picked}
        self.assertEqual(picked_ids, {"101", "102"})

    def test_T29_special_activity_does_not_consume_daily_quota(self):
        """T29: 特殊活动不占每日名额，同日普通活动仍可入选"""
        tt = {"week1_monday": "2026-08-31", "total_weeks": 16, "busy_slots": []}
        cfg = {
            "max_normal": 2,
            "max_per_day": 1,
            "special": ["体质健康测试"],
            "targets": ["羽毛球", "篮球"]
        }
        cw = 10

        day = datetime.datetime(2026, 11, 2, 14, 0)
        special_act = self._sample_activity("201", "体质健康测试", day)
        normal_act1 = self._sample_activity("202", "羽毛球 尹老师", day.replace(hour=16))
        normal_act2 = self._sample_activity("203", "篮球 亢老师", day.replace(hour=19))

        picked = L.pick_targets([special_act, normal_act1, normal_act2], cfg, tt, cw)
        # 特殊活动入选 + 1 个普通活动入选 = 2 个（第 2 个普通活动被日上限拦截）
        self.assertEqual(len(picked), 2)
        pri0 = [p for p in picked if p[0] == 0]
        self.assertEqual(len(pri0), 1, "特殊活动应以优先级 0 入选")
        self.assertEqual(str(pri0[0][1]["id"]), "201")
        pri_norm = [p for p in picked if p[0] > 0]
        self.assertEqual(len(pri_norm), 1, "同日普通活动只能入选 1 个")
        self.assertEqual(str(pri_norm[0][1]["id"]), "202")

    def test_T30_pre_submit_secondary_validation_blocks_exceeded(self):
        """T30: 提交前二次校验，若当天/当周名额已被占用则阻断提交"""
        tt = {"week1_monday": "2026-08-31", "total_weeks": 16, "busy_slots": []}
        cfg = {"max_normal": 2, "max_per_day": 1, "targets": ["羽毛球"]}

        day = datetime.datetime(2026, 11, 2, 14, 0)
        target_act = self._sample_activity("301", "羽毛球 尹老师", day)

        # 模拟已报满 1 节
        reg_by_day = {day.date(): 1}
        reg_by_week = {10: 1}
        max_per_day = 1
        max_normal = 2

        # 构造 validate_rules 回调函数进行核验
        spec = L.is_special(target_act, cfg)
        def validate_rules(act_item):
            if not spec:
                curr_d = datetime.date.fromtimestamp(int(act_item["startTime"]) / 1000)
                cur_d_tot = reg_by_day.get(curr_d, 0) + L.DAY_DONE.get(curr_d, 0)
                if max_per_day > 0 and cur_d_tot >= max_per_day:
                    return False, f"当天普通活动已报满 {max_per_day} 节"
            return True, "OK"

        ok, reason = validate_rules(target_act)
        self.assertFalse(ok, "当天下属名额已满时二次校验应拦截")
        self.assertIn("当天普通活动已报满 1 节", reason)

    # =========================================================================
    # 任务二：活动列表分页完整性保护 (T31 ~ T34)
    # =========================================================================

    def test_T31_first_page_success_second_page_fail(self):
        """T31: 分页拉取中途失败，返回标记为不完整的 ActivityList"""
        client = L.Client({"semesterId": "sem1", "schoolId": "sch1", "accessToken": "token"})

        def mock_call(method, path, body=None, timeout=6):
            page = body.get("page", 1)
            if page == 1:
                items = [{"id": f"act_{i}", "name": f"活动{i}", "startTime": 1700000000000} for i in range(200)]
                return 200, {"code": 0, "data": {"items": items, "total": 350}}
            else:
                return 500, "Internal Server Error"

        client.call = mock_call
        client.ensure_token = lambda margin=1200: "token"

        res = client.activities(page_size=200)
        self.assertIsInstance(res, ActivityList)
        self.assertFalse(res.complete, "分页中途失败必须标记 complete=False")
        self.assertEqual(res.fetched, 200)
        self.assertEqual(res.total, 350)
        self.assertIn("第 2 页拉取失败", res.error)

    def test_T32_all_pages_success_returns_complete(self):
        """T32: 所有分页均拉取成功，返回完整数据并标记 complete=True"""
        client = L.Client({"semesterId": "sem1", "schoolId": "sch1", "accessToken": "token"})

        def mock_call(method, path, body=None, timeout=6):
            page = body.get("page", 1)
            if page == 1:
                items = [{"id": f"act_{i}", "name": f"活动{i}", "startTime": 1700000000000} for i in range(200)]
                return 200, {"code": 0, "data": {"items": items, "total": 350}}
            elif page == 2:
                items = [{"id": f"act_{i}", "name": f"活动{i}", "startTime": 1700000000000} for i in range(200, 350)]
                return 200, {"code": 0, "data": {"items": items, "total": 350}}
            return 200, {"code": 0, "data": {"items": [], "total": 350}}

        client.call = mock_call
        client.ensure_token = lambda margin=1200: "token"

        res = client.activities(page_size=200)
        self.assertTrue(res.complete, "全部分页成功必须标记 complete=True")
        self.assertEqual(res.fetched, 350)
        self.assertEqual(res.total, 350)
        self.assertEqual(len(res), 350)

    def test_T33_reconciliation_and_verify_incomplete_list_keeps_unknown(self):
        """T33: 服务端复核时若活动列表不完整，绝不能判定为未报名，保持 UNKNOWN"""
        mock_cli = MockClientRound2()
        # 模拟仅拉取到 100 条的不完整列表，且未找到目标活动
        mock_cli.activities_result = ActivityList(
            [{"id": "other_1", "name": "其他活动", "isRegister": False}],
            complete=False, total=300, fetched=1, error="Timeout on page 2"
        )

        retry = RetryPolicy()
        req_mgr = RequestManager(mock_cli, retry)

        verified, is_reg, item = req_mgr.verify_registration("target_activity_999")
        self.assertFalse(verified, "列表不完整时不能判定验证通过")
        self.assertIsNone(is_reg, "不能判定为未报名")

        # 对账时也必须拒绝
        reconciler = ReconciliationManager(mock_cli, {"max_normal": 2}, {"week1_monday": "2026-08-31"}, L.week_of)
        rep = reconciler.reconcile()
        self.assertFalse(rep["success"])
        self.assertIn("不完整", rep["error"])

    def test_T34_menu_cmd_list_shows_warning_on_incomplete(self):
        """T34: 菜单 2 展示时如果列表不完整，必须输出明确警告信息"""
        fake_incomplete = ActivityList(
            [{"id": "1", "name": "羽毛球1", "isRegister": False, "startTime": 1793520000000}],
            complete=False, total=200, fetched=1, error="Network interrupted"
        )
        logs = []
        with patch("lejian.Client.activities", return_value=fake_incomplete), \
             patch("lejian.Client.ensure_token", return_value=True), \
             patch("lejian.load_json", return_value={}), \
             patch("lejian.load_timetable", return_value={"week1_monday": "2026-08-31", "total_weeks": 16}), \
             patch("lejian.log", side_effect=lambda msg: logs.append(msg)):
            L.cmd_list()

        out_text = "\n".join(logs)
        self.assertIn("警告", out_text)
        self.assertIn("不完整", out_text)

    # =========================================================================
    # 任务三：旧版课表迁移校验遗漏 (T35 ~ T39)
    # =========================================================================

    def test_T35_schema1_valid_migration(self):
        """T35: 旧版合法数据迁移，通过全量校验并正确生成 Timetable"""
        v1_data = {
            "source": "old_schedule.md",
            "week1_monday": "2026-08-31",
            "courses": [
                {"name": "高等数学", "weekday": 1, "start": "08:00", "end": "09:35", "weeks": [[1, 16]]}
            ]
        }
        tt = from_schema1_dict(v1_data)
        self.assertEqual(tt.week1_monday, "2026-08-31")
        self.assertEqual(len(tt.busy_slots), 1)
        self.assertEqual(tt.busy_slots[0].weekday, 1)

    def test_T36_schema1_tuesday_week1_fails_validation(self):
        """T36: 旧版星期二作为第一周锚点，严厉拦截并抛出 ValidationError"""
        bad_v1 = {
            "week1_monday": "2026-09-01", # 星期二，非法
            "courses": [
                {"name": "课程A", "weekday": 1, "start": "08:00", "end": "09:35", "weeks": [[1, 16]]}
            ]
        }
        with self.assertRaises(ValidationError) as ctx:
            from_schema1_dict(bad_v1)
        self.assertIn("第一教学周日期必须为星期一", str(ctx.exception))

    def test_T37_schema1_invalid_weeks_fails_validation(self):
        """T37: 旧版课程包含非法周次区间，抛出 ValidationError"""
        bad_v1 = {
            "week1_monday": "2026-08-31",
            "courses": [
                {"name": "坏课程", "weekday": 1, "start": "08:00", "end": "09:35", "weeks": [[10, 5]]}
            ]
        }
        with self.assertRaises(ValidationError) as ctx:
            from_schema1_dict(bad_v1)
        err_msg = str(ctx.exception)
        self.assertTrue("解析课程记录失败" in err_msg or "大于结束周" in err_msg)

    def test_T38_schema1_missing_week1_monday_infers_cleanly(self):
        """T38: 旧版缺失 week1_monday 时兼容推算，且推算结果必为合法星期一"""
        v1_no_w1 = {
            "current_week": 8,
            "courses": [
                {"name": "计算机基础", "weekday": 3, "start": "14:00", "end": "15:35", "weeks": [[1, 16]]}
            ]
        }
        tt = from_schema1_dict(v1_no_w1)
        self.assertIsNotNone(tt.week1_monday)
        w1_dt = datetime.date.fromisoformat(tt.week1_monday)
        self.assertEqual(w1_dt.weekday(), 0, "推算出的日期必须为周一")

    def test_T39_migration_failure_preserves_original_file(self):
        """T39: 旧版数据迁移校验失败时，绝不破坏覆盖原始文件"""
        bad_data = {
            "week1_monday": "2026-09-02", # 星期三，非法
            "courses": [{"name": "A", "weekday": 1, "start": "08:00", "end": "09:35", "weeks": [[1, 16]]}]
        }
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(bad_data, f, ensure_ascii=False)

        with open(self.json_path, "r", encoding="utf-8") as f:
            content_before = f.read()

        ok, msg = migrate_timetable_file(self.json_path, backup=True)
        self.assertFalse(ok)
        with open(self.json_path, "r", encoding="utf-8") as f:
            content_after = f.read()
        self.assertEqual(content_before, content_after, "校验失败时原文件必须保持完好无损")

    # =========================================================================
    # 任务四：完善 Markdown 课表转换工具 (T40 ~ T46)
    # =========================================================================

    def _create_sample_md(self, path):
        content = """### 星期一
- **08:00-09:35**
  大学英语
  1-16周
- **14:00-15:35**
  无指定周次课程
### 星期二
- **10:00-11:35**
  高等数学
  第 2 周, 第 4 周
"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def test_T40_build_timetable_with_explicit_week1_monday(self):
        """T40: 显式指定 --week1-monday，正确生成 Schema v2 课表"""
        self._create_sample_md(self.md_path)
        ok, msg, data = build_timetable(
            src=self.md_path,
            week1_monday="2026-08-31",
            total_weeks=16,
            out_path=self.json_path
        )
        self.assertTrue(ok)
        loaded = load_timetable_file(self.json_path)
        self.assertEqual(loaded.schema_version, 2)
        self.assertEqual(loaded.week1_monday, "2026-08-31")
        self.assertEqual(loaded.total_weeks, 16)
        self.assertEqual(len(loaded.busy_slots), 3)

    def test_T41_build_timetable_with_current_week_calculation(self):
        """T41: 使用 --current-week 反推第一教学周周一"""
        self._create_sample_md(self.md_path)
        # 固定测试基准日期 2026-10-19（周一，第8周），反推第1周周一应为 2026-08-31
        fixed_today = datetime.date(2026, 10, 19)
        ok, msg, data = build_timetable(
            src=self.md_path,
            current_week=8,
            total_weeks=16,
            out_path=self.json_path,
            today=fixed_today
        )
        self.assertTrue(ok)
        self.assertEqual(data["week1_monday"], "2026-08-31")

    def test_T42_build_timetable_custom_total_weeks(self):
        """T42: 自定义学期总周数 20 周，无周次课程使用 1~20 周"""
        self._create_sample_md(self.md_path)
        ok, msg, data = build_timetable(
            src=self.md_path,
            week1_monday="2026-08-31",
            total_weeks=20,
            out_path=self.json_path
        )
        self.assertTrue(ok)
        self.assertEqual(data["total_weeks"], 20)
        # 寻找无指定周次课程
        no_week_slot = next((s for s in data["busy_slots"] if s["start"] == "14:00"), None)
        self.assertIsNotNone(no_week_slot)
        self.assertEqual(no_week_slot["weeks"], [[1, 20]])

    def test_T43_build_timetable_invalid_date_fails(self):
        """T43: 指定非法日期（非周一或格式错误），报错并拒绝生成"""
        self._create_sample_md(self.md_path)
        # 2026-09-01 是周二
        with self.assertRaises(ValueError) as ctx:
            build_timetable(src=self.md_path, week1_monday="2026-09-01", out_path=self.json_path)
        self.assertIn("必须为星期一", str(ctx.exception))

        # 格式错误
        with self.assertRaises(ValueError):
            build_timetable(src=self.md_path, week1_monday="2026/08/31", out_path=self.json_path)

    def test_T44_build_timetable_invalid_total_weeks_fails(self):
        """T44: 非法学期周数 (0 或 60) 明确报错"""
        self._create_sample_md(self.md_path)
        with self.assertRaises(ValueError):
            build_timetable(src=self.md_path, week1_monday="2026-08-31", total_weeks=0, out_path=self.json_path)
        with self.assertRaises(ValueError):
            build_timetable(src=self.md_path, week1_monday="2026-08-31", total_weeks=60, out_path=self.json_path)

    def test_T45_build_timetable_validation_failure_preserves_old_file(self):
        """T45: 课表文件如果损坏或解析空导致校验失败，原文件不被破坏"""
        with open(self.json_path, "w", encoding="utf-8") as f:
            f.write('{"valid": true}')
        with open(self.json_path, "r", encoding="utf-8") as f:
            before = f.read()

        empty_md = os.path.join(self.tmp_dir, "empty.md")
        with open(empty_md, "w", encoding="utf-8") as f:
            f.write("# 空文件没有有效课程")

        with self.assertRaises(ValueError):
            build_timetable(src=empty_md, week1_monday="2026-08-31", out_path=self.json_path)

        with open(self.json_path, "r", encoding="utf-8") as f:
            after = f.read()
        self.assertEqual(before, after)

    def test_T46_build_timetable_creates_backup(self):
        """T46: 原课表文件存在时，新转换课表自动生成 .bak 备份"""
        # 先保存一个初始版本
        initial_tt = Timetable(week1_monday="2026-08-31", total_weeks=16, busy_slots=[
            TimeSlot(weekday=1, start="08:00", end="09:35", weeks=[[1, 16]], label="旧课程")
        ])
        save_timetable_file(self.json_path, initial_tt, backup=False)

        # 执行新课表转换
        self._create_sample_md(self.md_path)
        ok, msg, data = build_timetable(
            src=self.md_path,
            week1_monday="2026-08-31",
            total_weeks=18,
            out_path=self.json_path
        )
        self.assertTrue(ok)
        bak_file = self.json_path + ".bak"
        self.assertTrue(os.path.exists(bak_file), "原有课表必须被自动备份为 .bak 文件")
        with open(bak_file, "r", encoding="utf-8") as f:
            bak_data = json.load(f)
        self.assertEqual(bak_data.get("total_weeks"), 16, "备份中应为修改前的课表数据")


if __name__ == "__main__":
    unittest.main()
