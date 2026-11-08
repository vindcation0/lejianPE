"""
第四轮优化 Phase 5：checkConflict 智能降级测试套件 (C01 ~ C26)
============================================================
涵盖 26 个针对 checkConflict 独立健康管理、智能降级、本地冲突判断、
数据完整性安全阻断及配置兼容性的全量测试用例。
"""
import collections
import datetime
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from core.audit_logger import AuditLogger, AuditLogReader, EventName, EventType
from core.circuit_breaker import CircuitBreaker, CircuitState
from core.conflict_checker import (
    ActivityList,
    ConflictCheckMode,
    ConflictCheckResult,
    ConflictCircuitBreaker,
    ConflictCircuitState,
    ConflictManager,
    LocalConflictChecker
)
from core.registration_manager import RegistrationManager
from core.request_manager import RequestManager
from core.result_reporter import ResultReporter
from core.state_store import RegistrationStatus, StateStore


class ConflictFallbackTestSuite(unittest.TestCase):
    """Phase 5 全量验收测试集 (C01 ~ C26)"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_phase5_")
        self.logs_dir = os.path.join(self.test_dir, "logs")
        os.makedirs(self.logs_dir, exist_ok=True)
        self.audit_logger = AuditLogger(self.logs_dir, run_id="run_test_phase5")
        self.state_store = StateStore(":memory:")
        self.mock_client = MagicMock()

        # 课表参考：2026-09-07 为第 1 周周一
        self.timetable = {
            "week1_monday": "2026-09-07",
            "busy_slots": [
                {
                    "weekday": 1,
                    "start": "10:00",
                    "end": "11:35",
                    "weeks": [[1, 16]],
                    "label": "大学英语"
                }
            ]
        }
        self.cfg = {
            "check_conflict": True,
            "max_normal": 2,
            "max_per_day": 1,
            "special": ["体质测试", "定向越野"]
        }

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # =========================================================================
    # 任务一：checkConflict 独立健康管理 (C01 ~ C08)
    # =========================================================================

    def test_C01_check_conflict_consecutive_5xx_trips_breaker(self):
        """C01: checkConflict 连续 5xx 独立触发熔断"""
        breaker = ConflictCircuitBreaker(failure_threshold=3, base_cooldown=2.0, audit_logger=self.audit_logger)
        self.assertTrue(breaker.is_normal)
        self.assertEqual(breaker.state, ConflictCircuitState.NORMAL)

        # 连续 3 次 5xx
        breaker.record_failure(http_status=500)
        self.assertTrue(breaker.is_normal)
        self.assertEqual(breaker.failure_count, 1)

        breaker.record_failure(http_status=502)
        self.assertTrue(breaker.is_normal)
        self.assertEqual(breaker.failure_count, 2)

        breaker.record_failure(http_status=503)
        self.assertTrue(breaker.is_open)
        self.assertEqual(breaker.state, ConflictCircuitState.OPEN)
        self.assertEqual(breaker.trip_count, 1)

    def test_C02_check_conflict_failure_does_not_affect_activities_breaker(self):
        """C02: checkConflict 故障不影响 activities 熔断器"""
        activities_breaker = CircuitBreaker(failure_threshold=3)
        conflict_breaker = ConflictCircuitBreaker(failure_threshold=3)

        # 触发 checkConflict 熔断
        for _ in range(3):
            conflict_breaker.record_failure(http_status=500)

        self.assertTrue(conflict_breaker.is_open)
        # activities 熔断器必须完好如初
        self.assertEqual(activities_breaker.state, CircuitState.CLOSED)
        self.assertEqual(activities_breaker.failure_count, 0)

        # 反之亦然
        for _ in range(3):
            activities_breaker.record_failure(http_status=500)
        self.assertEqual(activities_breaker.state, CircuitState.OPEN)

    def test_C03_open_state_forbids_normal_conflict_requests(self):
        """C03: OPEN 状态禁止正常冲突请求"""
        breaker = ConflictCircuitBreaker(failure_threshold=1, base_cooldown=10.0)
        breaker.record_failure(http_status=500)
        self.assertTrue(breaker.is_open)

        can_call, reason = breaker.can_attempt()
        self.assertFalse(can_call)
        self.assertIn("OPEN", reason)

        # manager 在 OPEN 状态下禁止发出 HTTP 请求
        manager = ConflictManager(self.mock_client, breaker=breaker, audit_logger=self.audit_logger, cfg=self.cfg)
        cand = {
            "id": "act_01",
            "name": "羽毛球",
            "startTime": 1731132000000,
            "endTime": 1731137700000
        }
        items = ActivityList([], complete=True)
        manager.check_activity_conflict(cand, items)
        self.mock_client.call.assert_not_called()

    def test_C04_half_open_allows_only_single_probe(self):
        """C04: HALF_OPEN 只允许一次探测"""
        breaker = ConflictCircuitBreaker(failure_threshold=1, base_cooldown=0.01, jitter_max=0.0)
        breaker.record_failure(http_status=500)
        self.assertTrue(breaker.is_open)

        breaker.opened_at = time.time() - 1.0  # 模拟冷却结束
        # 第一次尝试：转换为 HALF_OPEN 并允许探测
        can1, _ = breaker.can_attempt()
        self.assertTrue(can1)
        self.assertTrue(breaker.is_half_open)

        # 第二次并发尝试：正在探测中，禁止请求
        can2, reason2 = breaker.can_attempt()
        self.assertFalse(can2)
        self.assertIn("禁止并发", reason2)

    def test_C05_half_open_success_recovers_to_normal(self):
        """C05: HALF_OPEN 成功恢复 NORMAL"""
        breaker = ConflictCircuitBreaker(failure_threshold=1, base_cooldown=0.01, jitter_max=0.0, audit_logger=self.audit_logger)
        breaker.record_failure(http_status=500)
        breaker.opened_at = time.time() - 1.0  # 模拟冷却完成
        breaker.can_attempt()
        self.assertTrue(breaker.is_half_open)

        # 探测成功且数据完整
        state = breaker.record_success(is_complete=True)
        self.assertEqual(state, ConflictCircuitState.NORMAL)
        self.assertTrue(breaker.is_normal)
        self.assertEqual(breaker.failure_count, 0)

    def test_C06_half_open_failure_re_opens(self):
        """C06: HALF_OPEN 失败重新 OPEN"""
        breaker = ConflictCircuitBreaker(failure_threshold=1, base_cooldown=1.0)
        breaker.record_failure(http_status=500)
        breaker.opened_at = time.time() - 2.0  # 模拟冷却完成
        breaker.can_attempt()
        self.assertTrue(breaker.is_half_open)

        # 探测遇到 500 故障
        state = breaker.record_failure(http_status=500)
        self.assertEqual(state, ConflictCircuitState.OPEN)
        self.assertTrue(breaker.is_open)
        # 退避冷却时间应递增
        self.assertGreaterEqual(breaker.current_cooldown, 2.0)

    def test_C07_401_does_not_accumulate_infrastructure_failure(self):
        """C07: 401 不累计基础设施故障"""
        breaker = ConflictCircuitBreaker(failure_threshold=3)
        for _ in range(5):
            breaker.record_failure(http_status=401)
        self.assertEqual(breaker.failure_count, 0)
        self.assertTrue(breaker.is_normal)

    def test_C08_403_does_not_accumulate_infrastructure_failure(self):
        """C08: 403 不累计基础设施故障"""
        breaker = ConflictCircuitBreaker(failure_threshold=3)
        for _ in range(5):
            breaker.record_failure(http_status=403)
        self.assertEqual(breaker.failure_count, 0)
        self.assertTrue(breaker.is_normal)

    # =========================================================================
    # 任务二与三：本地冲突检查与数据完整性 (C09 ~ C20)
    # =========================================================================

    def test_C09_local_complete_data_no_conflict_allows_evaluation(self):
        """C09: 本地完整数据无冲突时允许继续评估"""
        checker = LocalConflictChecker()
        # 候选活动：2026-11-09 14:00 ~ 15:35
        # 对应毫秒戳: 2026-11-09 14:00 (1731132000000) ~ 15:35 (1731137700000)
        dt_cand_s = datetime.datetime(2026, 11, 9, 14, 0)
        dt_cand_e = datetime.datetime(2026, 11, 9, 15, 35)
        cand = {
            "id": "act_101",
            "name": "篮球",
            "startTime": int(dt_cand_s.timestamp() * 1000),
            "endTime": int(dt_cand_e.timestamp() * 1000)
        }

        # 已报活动：2026-11-09 16:00 ~ 17:35 (同日但不重合)
        dt_reg_s = datetime.datetime(2026, 11, 9, 16, 0)
        dt_reg_e = datetime.datetime(2026, 11, 9, 17, 35)
        items = ActivityList([
            {
                "id": "act_201",
                "name": "羽毛球",
                "isRegister": True,
                "startTime": int(dt_reg_s.timestamp() * 1000),
                "endTime": int(dt_reg_e.timestamp() * 1000)
            }
        ], complete=True)

        cfg = {"max_normal": 2, "max_per_day": 2}
        res = checker.check_conflict(cand, items, cfg=cfg, tt=self.timetable, state_store=self.state_store)
        self.assertTrue(res.is_clean)
        self.assertFalse(res.has_conflict)
        self.assertEqual(res.status, "CLEAN")
        self.assertEqual(res.mode, ConflictCheckMode.DEGRADED_LOCAL)

    def test_C10_local_complete_data_with_conflict_blocks(self):
        """C10: 本地完整数据存在冲突时阻断"""
        checker = LocalConflictChecker()
        # 候选活动：2026-11-09 14:00 ~ 15:35
        dt_cand_s = datetime.datetime(2026, 11, 9, 14, 0)
        dt_cand_e = datetime.datetime(2026, 11, 9, 15, 35)
        cand = {
            "id": "act_101",
            "name": "篮球",
            "startTime": int(dt_cand_s.timestamp() * 1000),
            "endTime": int(dt_cand_e.timestamp() * 1000)
        }

        # 已报活动：2026-11-09 15:00 ~ 16:35 (15:00 ~ 15:35 重叠)
        dt_reg_s = datetime.datetime(2026, 11, 9, 15, 0)
        dt_reg_e = datetime.datetime(2026, 11, 9, 16, 35)
        items = ActivityList([
            {
                "id": "act_201",
                "name": "足球",
                "isRegister": True,
                "startTime": int(dt_reg_s.timestamp() * 1000),
                "endTime": int(dt_reg_e.timestamp() * 1000)
            }
        ], complete=True)

        res = checker.check_conflict(cand, items, cfg=self.cfg, tt=self.timetable, state_store=self.state_store)
        self.assertTrue(res.has_conflict)
        self.assertEqual(res.status, "CONFLICT")
        self.assertIn("时间重叠", res.reason)

    def test_C11_local_incomplete_list_blocks(self):
        """C11: 本地课程列表不完整时阻断"""
        checker = LocalConflictChecker()
        cand = {
            "id": "act_101",
            "name": "排球",
            "startTime": 1731132000000,
            "endTime": 1731137700000
        }
        # complete=False 模拟分页残缺
        items = ActivityList([], complete=False, error="未拉取全量活动 (已获取 10/50)")
        res = checker.check_conflict(cand, items, cfg=self.cfg, tt=self.timetable, state_store=self.state_store)
        self.assertTrue(res.is_blocked)
        self.assertTrue(res.has_conflict)
        self.assertEqual(res.mode, ConflictCheckMode.BLOCKED)
        self.assertIn("不完整", res.reason)

    def test_C12_local_time_parsing_failure_blocks(self):
        """C12: 本地课程时间解析失败时阻断"""
        checker = LocalConflictChecker()
        # 1. 候选活动无有效起止时间
        cand_invalid = {"id": "act_bad", "name": "坏时间活动", "startTime": "abc", "endTime": 1731137700000}
        items = ActivityList([], complete=True)
        res1 = checker.check_conflict(cand_invalid, items)
        self.assertTrue(res1.is_blocked)

        # 2. 候选活动 endTime <= startTime
        cand_reverse = {"id": "act_rev", "name": "逆序时间", "startTime": 2000, "endTime": 1000}
        res2 = checker.check_conflict(cand_reverse, items)
        self.assertTrue(res2.is_blocked)

        # 3. 已报课程时间解析失败
        cand_ok = {"id": "act_ok", "name": "正常候选", "startTime": 1731132000000, "endTime": 1731137700000}
        items_bad = ActivityList([
            {"id": "reg_bad", "name": "已报损坏", "isRegister": True, "startTime": None, "endTime": 100}
        ], complete=True)
        res3 = checker.check_conflict(cand_ok, items_bad)
        self.assertTrue(res3.is_blocked)

    def test_C13_unknown_in_state_store_blocks_local_degradation(self):
        """C13: 存在 UNKNOWN 时本地降级阻断"""
        checker = LocalConflictChecker()
        self.state_store.record_unknown("act_unres", "未决户外跑", "网络超时")
        cand = {"id": "act_cand", "name": "新候选", "startTime": 1731132000000, "endTime": 1731137700000}
        items = ActivityList([], complete=True)

        res = checker.check_conflict(cand, items, state_store=self.state_store)
        self.assertTrue(res.is_blocked)
        self.assertIn("UNKNOWN", res.reason)

    def test_C14_submitting_in_state_store_blocks_local_degradation(self):
        """C14: 存在 SUBMITTING 时本地降级阻断"""
        checker = LocalConflictChecker()
        self.state_store.reserve_submitting("act_sub", "正在提交中")
        cand = {"id": "act_cand", "name": "新候选", "startTime": 1731132000000, "endTime": 1731137700000}
        items = ActivityList([], complete=True)

        res = checker.check_conflict(cand, items, state_store=self.state_store)
        self.assertTrue(res.is_blocked)
        self.assertIn("SUBMITTING", res.reason)

    def test_C15_cross_day_course_conflict_detected_correctly(self):
        """C15: 跨天课程冲突判断正确"""
        checker = LocalConflictChecker()
        # 候选活动：周一 23:00 ~ 周二 01:00 (跨天)
        dt_cand_s = datetime.datetime(2026, 11, 9, 23, 0)
        dt_cand_e = datetime.datetime(2026, 11, 10, 1, 0)
        cand = {
            "id": "act_cross",
            "name": "通宵定向",
            "startTime": int(dt_cand_s.timestamp() * 1000),
            "endTime": int(dt_cand_e.timestamp() * 1000)
        }

        # 已报活动：周二 00:30 ~ 02:00 (与候选跨天后半段重合)
        dt_reg_s = datetime.datetime(2026, 11, 10, 0, 30)
        dt_reg_e = datetime.datetime(2026, 11, 10, 2, 0)
        items = ActivityList([
            {
                "id": "act_tue",
                "name": "晨练",
                "isRegister": True,
                "startTime": int(dt_reg_s.timestamp() * 1000),
                "endTime": int(dt_reg_e.timestamp() * 1000)
            }
        ], complete=True)

        res = checker.check_conflict(cand, items, state_store=self.state_store)
        self.assertTrue(res.has_conflict)
        self.assertEqual(res.status, "CONFLICT")
        self.assertIn("时间重叠", res.reason)

    def test_C16_different_teaching_weeks_do_not_conflict(self):
        """C16: 不同教学周课程不会错误判定冲突"""
        checker = LocalConflictChecker()
        # 候选活动：第 10 周 周一 14:00 (2026-11-09)
        dt_cand_s = datetime.datetime(2026, 11, 9, 14, 0)
        dt_cand_e = datetime.datetime(2026, 11, 9, 15, 35)
        cand = {
            "id": "act_w10",
            "name": "排球第10周",
            "startTime": int(dt_cand_s.timestamp() * 1000),
            "endTime": int(dt_cand_e.timestamp() * 1000)
        }

        # 已报活动：第 11 周 周一 14:00 (2026-11-16) - 虽同为周一时段，但跨周
        dt_reg_s = datetime.datetime(2026, 11, 16, 14, 0)
        dt_reg_e = datetime.datetime(2026, 11, 16, 15, 35)
        items = ActivityList([
            {
                "id": "act_w11",
                "name": "排球第11周",
                "isRegister": True,
                "startTime": int(dt_reg_s.timestamp() * 1000),
                "endTime": int(dt_reg_e.timestamp() * 1000)
            }
        ], complete=True)

        res = checker.check_conflict(cand, items, cfg=self.cfg, tt=self.timetable, state_store=self.state_store)
        self.assertTrue(res.is_clean)
        self.assertFalse(res.has_conflict)

    def test_C17_daily_quota_remains_enforced(self):
        """C17: 每日配额仍然有效"""
        checker = LocalConflictChecker()
        cfg = {"max_per_day": 1, "max_normal": 2}
        dt_cand_s = datetime.datetime(2026, 11, 9, 16, 0)
        dt_cand_e = datetime.datetime(2026, 11, 9, 17, 35)
        cand = {
            "id": "act_cand",
            "name": "羽毛球2",
            "startTime": int(dt_cand_s.timestamp() * 1000),
            "endTime": int(dt_cand_e.timestamp() * 1000)
        }

        # 当天上午已报 1 节普通课程
        dt_reg_s = datetime.datetime(2026, 11, 9, 8, 0)
        dt_reg_e = datetime.datetime(2026, 11, 9, 9, 35)
        items = ActivityList([
            {
                "id": "act_reg1",
                "name": "羽毛球1",
                "isRegister": True,
                "startTime": int(dt_reg_s.timestamp() * 1000),
                "endTime": int(dt_reg_e.timestamp() * 1000)
            }
        ], complete=True)

        res = checker.check_conflict(cand, items, cfg=cfg, tt=self.timetable, state_store=self.state_store)
        self.assertTrue(res.has_conflict)
        self.assertIn("每日上限", res.reason)

    def test_C18_weekly_quota_remains_enforced(self):
        """C18: 每周配额仍然有效"""
        checker = LocalConflictChecker()
        cfg = {"max_normal": 2, "max_per_day": 2}
        # 第 10 周 周三
        dt_cand_s = datetime.datetime(2026, 11, 11, 14, 0)
        dt_cand_e = datetime.datetime(2026, 11, 11, 15, 35)
        cand = {
            "id": "act_cand",
            "name": "篮球",
            "startTime": int(dt_cand_s.timestamp() * 1000),
            "endTime": int(dt_cand_e.timestamp() * 1000)
        }

        # 第 10 周已报满 2 节普通课程 (周一、周二各 1 节)
        dt1 = datetime.datetime(2026, 11, 9, 14, 0)
        dt2 = datetime.datetime(2026, 11, 10, 14, 0)
        items = ActivityList([
            {"id": "r1", "name": "足球", "isRegister": True, "startTime": int(dt1.timestamp()*1000), "endTime": int((dt1+datetime.timedelta(hours=1)).timestamp()*1000)},
            {"id": "r2", "name": "排球", "isRegister": True, "startTime": int(dt2.timestamp()*1000), "endTime": int((dt2+datetime.timedelta(hours=1)).timestamp()*1000)}
        ], complete=True)

        res = checker.check_conflict(cand, items, cfg=cfg, tt=self.timetable, state_store=self.state_store)
        self.assertTrue(res.has_conflict)
        self.assertIn("每周上限", res.reason)

    def test_C19_special_activity_rules_not_bypassed(self):
        """C19: 特殊活动规则不被绕过"""
        checker = LocalConflictChecker()
        cfg = {"max_normal": 1, "max_per_day": 1, "special": ["体质测试"]}
        dt = datetime.datetime(2026, 11, 9, 14, 0)
        items = ActivityList([
            {"id": "r1", "name": "常规乒乓球", "isRegister": True, "startTime": int(dt.timestamp()*1000), "endTime": int((dt+datetime.timedelta(hours=1)).timestamp()*1000)}
        ], complete=True)

        # 候选活动为特殊活动（体质测试），不占用普通配额
        dt_spec = datetime.datetime(2026, 11, 9, 16, 0)
        cand_spec = {
            "id": "act_spec",
            "name": "大学体质测试",
            "startTime": int(dt_spec.timestamp()*1000),
            "endTime": int((dt_spec+datetime.timedelta(hours=1)).timestamp()*1000)
        }
        res_spec = checker.check_conflict(cand_spec, items, cfg=cfg, tt=self.timetable, state_store=self.state_store)
        self.assertTrue(res_spec.is_clean)

        # 未知课程类型不能被默认归为普通活动而绕过检查
        cand_unknown_type = dict(cand_spec)
        cand_unknown_type["type"] = "UNKNOWN"
        res_unk = checker.check_conflict(cand_unknown_type, items, cfg=cfg, tt=self.timetable, state_store=self.state_store)
        self.assertTrue(res_unk.is_blocked)

    def test_C20_local_data_recovery_allows_re_evaluation(self):
        """C20: 本地数据恢复完整后允许重新评估"""
        checker = LocalConflictChecker()
        cand = {"id": "act_01", "name": "健美操", "startTime": 1731132000000, "endTime": 1731137700000}

        # 第一次评估：列表残缺 -> 阻断
        items_incomplete = ActivityList([], complete=False, error="网络抖动残缺")
        res1 = checker.check_conflict(cand, items_incomplete, cfg=self.cfg, tt=self.timetable, state_store=self.state_store)
        self.assertTrue(res1.is_blocked)

        # 第二次评估：数据重新拉取完整 -> 放行
        items_complete = ActivityList([], complete=True)
        res2 = checker.check_conflict(cand, items_complete, cfg=self.cfg, tt=self.timetable, state_store=self.state_store)
        self.assertTrue(res2.is_clean)

    # =========================================================================
    # 任务四与五：自动降级、恢复与安全提交边界 (C21 ~ C25)
    # =========================================================================

    def test_C21_server_recovery_switches_back_to_normal(self):
        """C21: 服务端恢复后切回 NORMAL"""
        breaker = ConflictCircuitBreaker(failure_threshold=1, base_cooldown=0.01, jitter_max=0.0, audit_logger=self.audit_logger)
        manager = ConflictManager(self.mock_client, breaker=breaker, audit_logger=self.audit_logger, cfg=self.cfg, tt=self.timetable)

        # 1. 模拟服务端 500 触发熔断
        self.mock_client.call.return_value = (500, {"_raw": "Gateway Error"})
        cand = {"id": "act_01", "name": "网球", "startTime": 1731132000000, "endTime": 1731137700000}
        items = ActivityList([], complete=True)

        ok1, reason1 = manager.check_activity_conflict(cand, items)
        self.assertTrue(ok1)  # 自动降级为本地检查成功放行
        self.assertTrue(breaker.is_open)

        # 2. 冷却完成后服务端恢复正常
        breaker.opened_at = time.time() - 1.0  # 模拟冷却完成
        self.mock_client.call.return_value = (200, {"code": 0, "data": False})
        ok2, reason2 = manager.check_activity_conflict(cand, items)
        self.assertTrue(ok2)
        self.assertIn("服务端判定无冲突", reason2)
        self.assertTrue(breaker.is_normal)

    def test_C22_local_degraded_events_accurately_recorded(self):
        """C22: 本地降级事件准确记录"""
        breaker = ConflictCircuitBreaker(failure_threshold=1, base_cooldown=10.0, audit_logger=self.audit_logger)
        breaker.record_failure(http_status=500)  # 强制进入 OPEN

        manager = ConflictManager(self.mock_client, breaker=breaker, audit_logger=self.audit_logger, cfg=self.cfg, tt=self.timetable)
        cand = {"id": "act_ev", "name": "健美操", "startTime": 1731132000000, "endTime": 1731137700000}
        items = ActivityList([], complete=True)

        manager.check_activity_conflict(cand, items)

        # 读取事件验证
        today_str = datetime.date.today().strftime("%Y%m%d")
        log_file = os.path.join(self.logs_dir, f"events-{today_str}.jsonl")
        events = AuditLogReader.read_events_from_file(log_file)
        event_names = [e["event_name"] for e in events]

        self.assertIn(EventName.CONFLICT_CHECK_DEGRADED, event_names)
        self.assertIn(EventName.LOCAL_CONFLICT_CHECK_STARTED, event_names)
        self.assertIn(EventName.LOCAL_CONFLICT_CHECK_SUCCEEDED, event_names)

    def test_C23_check_conflict_failures_counted_independently(self):
        """C23: checkConflict 请求失败独立统计"""
        reporter = ResultReporter(run_date=datetime.date.today())
        # 记录 checkConflict 失败
        reporter.record_conflict_failure(500)
        reporter.record_conflict_failure(500)
        reporter.record_conflict_failure(502)

        sm = reporter.generate_summary()
        # activities 的 HTTP 失败依然为 0
        self.assertEqual(sm["infrastructure"]["http_failed"], 0)
        # conflict 专用失败字典准确记录
        self.assertEqual(sm["conflict_check"]["failures_breakdown"][500], 2)
        self.assertEqual(sm["conflict_check"]["failures_breakdown"][502], 1)

    def test_C24_local_check_does_not_increment_http_requests(self):
        """C24: 本地检查不增加 HTTP 请求次数"""
        breaker = ConflictCircuitBreaker(failure_threshold=1, base_cooldown=10.0)
        breaker.record_failure(http_status=500)

        manager = ConflictManager(self.mock_client, breaker=breaker, cfg=self.cfg, tt=self.timetable)
        cand = {"id": "act_01", "name": "跑步", "startTime": 1731132000000, "endTime": 1731137700000}
        items = ActivityList([], complete=True)

        for _ in range(5):
            manager.check_activity_conflict(cand, items)

        # mock_client.call 绝对没有被调用
        self.assertEqual(self.mock_client.call.call_count, 0)

    def test_C25_final_signup_still_single_submission(self):
        """C25: 最终 signUp 仍然只有一次调用"""
        req_mgr = RequestManager(self.mock_client)
        reg_mgr = RegistrationManager(self.state_store, req_mgr, self.audit_logger)

        breaker = ConflictCircuitBreaker(failure_threshold=1, base_cooldown=10.0)
        breaker.record_failure(http_status=500)
        conflict_mgr = ConflictManager(self.mock_client, breaker=breaker, cfg=self.cfg, tt=self.timetable, state_store=self.state_store)

        cand = {"id": "act_sign", "name": "足球单次提交", "startTime": 1731132000000, "endTime": 1731137700000}
        items = ActivityList([], complete=True)

        def rule_cb(it):
            ok, msg = conflict_mgr.check_activity_conflict(it, items)
            return ok, msg

        self.mock_client.call.return_value = (200, {"code": 0, "data": {"success": True}})
        ok, status, msg, _ = reg_mgr.register_activity(cand, validate_rules_cb=rule_cb)

        self.assertTrue(ok)
        self.assertEqual(status, RegistrationStatus.SUCCESS)
        # 仅有一次 signUp 提交调用
        self.assertEqual(self.mock_client.call.call_count, 1)
        self.assertEqual(self.mock_client.call.call_args[0][1], "education/app/activity/signUp")

    # =========================================================================
    # 任务七：配置兼容性 (C26)
    # =========================================================================

    def test_C26_config_backward_compatibility(self):
        """C26: 配置文件旧格式兼容"""
        # 1. 显式 check_conflict: false 时跳过服务端请求，但本地冲突依然拦截
        cfg_false = {"check_conflict": False, "max_normal": 2}
        cand = {"id": "act_01", "name": "篮球", "startTime": 1731132000000, "endTime": 1731137700000}
        items_conflict = ActivityList([
            {"id": "act_02", "name": "重叠课程", "isRegister": True, "startTime": 1731132000000, "endTime": 1731137700000}
        ], complete=True)

        mgr_false = ConflictManager(self.mock_client, cfg=cfg_false, tt=self.timetable)
        ok, reason = mgr_false.check_activity_conflict(cand, items_conflict)
        self.assertFalse(ok)
        self.assertIn("冲突", reason)
        self.mock_client.call.assert_not_called()

        # 2. 缺省或 check_conflict: true 保持向后兼容
        cfg_default = {"max_normal": 2}
        self.mock_client.call.return_value = (200, {"code": 0, "data": False})
        mgr_default = ConflictManager(self.mock_client, cfg=cfg_default, tt=self.timetable)
        ok_def, _ = mgr_default.check_activity_conflict(cand, ActivityList([], complete=True))
        self.assertTrue(ok_def)
        self.assertEqual(self.mock_client.call.call_count, 1)

    # =========================================================================
    # Phase 5 最终封版补充用例 (C27 ~ C38)
    # =========================================================================

    def test_C27_items_missing_complete_attribute_blocked(self):
        """C27: items 缺少 complete 属性，必须 BLOCKED"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        checker = LocalConflictChecker()

        # 普通 list 没有 complete 属性
        plain_items = [{"id": "act_02", "name": "已有课程", "isRegister": True, "startTime": 1731140000000, "endTime": 1731145000000}]
        ok, msg, details = checker.check_data_integrity(cand, plain_items)
        self.assertFalse(ok)
        self.assertFalse(details["has_complete_attr"])
        self.assertIn("缺失 complete 完整性标记证据", msg)

        # check_conflict 也必须返回 BLOCKED
        res = checker.check_conflict(cand, plain_items)
        self.assertTrue(res.is_blocked)
        self.assertEqual(res.status, "BLOCKED")

        # ConflictManager 降级时也阻断
        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": False}, tt=self.timetable)
        ok_mgr, reason_mgr = mgr.check_activity_conflict(cand, plain_items)
        self.assertFalse(ok_mgr)
        self.assertIn("complete", reason_mgr)

    def test_C28_incomplete_pagination_blocked(self):
        """C28: 分页不完整 (complete=False)，必须 BLOCKED"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        checker = LocalConflictChecker()

        incomplete_items = ActivityList([
            {"id": "act_02", "name": "已有课程", "isRegister": True, "startTime": 1731140000000, "endTime": 1731145000000}
        ], complete=False, error="分页未拉取全量")

        ok, msg, details = checker.check_data_integrity(cand, incomplete_items)
        self.assertFalse(ok)
        self.assertTrue(details["has_complete_attr"])
        self.assertFalse(details["items_complete"])
        self.assertIn("分页不完整", msg)

        res = checker.check_conflict(cand, incomplete_items)
        self.assertTrue(res.is_blocked)
        self.assertEqual(res.status, "BLOCKED")

    def test_C29_empty_list_with_complete_true_allows_evaluation(self):
        """C29: 完整且可信的空报名列表允许本地评估"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        checker = LocalConflictChecker()

        # 空列表且 complete=True、带新鲜时间戳
        empty_items = ActivityList([], complete=True, fetched_at=time.time())
        ok, msg, details = checker.check_data_integrity(cand, empty_items)
        self.assertTrue(ok)
        self.assertTrue(details["has_complete_attr"])
        self.assertTrue(details["items_complete"])

        # 本地规则评估应正常放行 (无冲突)
        res = checker.check_conflict(cand, empty_items, tt=self.timetable, cfg={"max_normal": 2})
        self.assertTrue(res.is_clean)
        self.assertEqual(res.status, "CLEAN")

        # ConflictManager 降级模式下也正常放行
        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": False}, tt=self.timetable)
        ok_mgr, reason_mgr = mgr.check_activity_conflict(cand, empty_items)
        self.assertTrue(ok_mgr)
        self.assertIn("本地规则检查通过", reason_mgr)

    def test_C30_empty_list_with_complete_false_blocked(self):
        """C30: 空列表但 complete=False，必须 BLOCKED"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        checker = LocalConflictChecker()

        empty_incomplete = ActivityList([], complete=False, error="拉取失败返回空")
        ok, msg, details = checker.check_data_integrity(cand, empty_incomplete)
        self.assertFalse(ok)
        self.assertIn("complete=False", msg)

        res = checker.check_conflict(cand, empty_incomplete)
        self.assertTrue(res.is_blocked)
        self.assertEqual(res.status, "BLOCKED")

    def test_C31_expired_local_snapshot_blocked(self):
        """C31: 本地报名数据超过有效期，必须 BLOCKED"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        checker = LocalConflictChecker()

        # 数据生成时间为 120 秒前，超过 60s 上限
        expired_items = ActivityList([], complete=True, fetched_at=time.time() - 120.0)
        ok, msg, details = checker.check_data_integrity(cand, expired_items, max_snapshot_age_sec=60.0)
        self.assertFalse(ok)
        self.assertIn("超过有效期", msg)

        res = checker.check_conflict(cand, expired_items, max_snapshot_age_sec=60.0)
        self.assertTrue(res.is_blocked)
        self.assertIn("超过有效期", res.reason)

        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": False}, tt=self.timetable)
        ok_mgr, reason_mgr = mgr.check_activity_conflict(cand, expired_items, max_snapshot_age_sec=60.0)
        self.assertFalse(ok_mgr)
        self.assertIn("超过有效期", reason_mgr)

    def test_C32_mismatched_run_id_blocked(self):
        """C32: 本地数据 run_id 不匹配，必须 BLOCKED"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        checker = LocalConflictChecker()

        old_items = ActivityList([], complete=True, fetched_at=time.time(), run_id="run_old_20261108_01")
        ok, msg, details = checker.check_data_integrity(cand, old_items, current_run_id="run_new_20261108_02")
        self.assertFalse(ok)
        self.assertIn("run_id 不匹配", msg)

        res = checker.check_conflict(cand, old_items, current_run_id="run_new_20261108_02")
        self.assertTrue(res.is_blocked)
        self.assertIn("run_id 不匹配", res.reason)

    def test_C33_state_change_after_snapshot_requires_revalidation(self):
        """C33: 本地数据获取后状态发生变化，必须重新验证"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        checker = LocalConflictChecker()

        # 快照获取时间为 2 秒前
        t_snapshot = time.time() - 2.0
        items = ActivityList([], complete=True, fetched_at=t_snapshot)

        # 快照获取之后，本地 StateStore 记录了新的活动状态变更
        time.sleep(0.01)
        self.state_store.record_success("act_prev", "篮球", http_status=200)

        ok, msg, details = checker.check_data_integrity(cand, items, state_store=self.state_store)
        self.assertFalse(ok)
        self.assertIn("本地数据获取后状态发生变化", msg)

        res = checker.check_conflict(cand, items, state_store=self.state_store)
        self.assertTrue(res.is_blocked)
        self.assertEqual(res.status, "BLOCKED")

    def test_C34_401_unauthorized_blocks_submission_without_local_fallback(self):
        """C34: 401 认证未恢复时，禁止进入报名提交且不计入基础设施熔断"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        self.mock_client.call.return_value = (401, {"code": 401, "msg": "Unauthorized"})

        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": True}, tt=self.timetable, audit_logger=self.audit_logger)
        items = ActivityList([], complete=True)

        ok, reason = mgr.check_activity_conflict(cand, items)
        self.assertFalse(ok)
        self.assertIn("401", reason)
        self.assertIn("认证未恢复", reason)

        # 熔断器绝不进入 OPEN，失败计数不累加
        self.assertEqual(mgr.breaker.state, ConflictCircuitState.NORMAL)
        self.assertEqual(mgr.breaker.failure_count, 0)

        # 审计日志：没有本地检查启动事件 (禁止盲目本地降级)
        today_str = datetime.date.today().strftime("%Y%m%d")
        log_file = os.path.join(self.logs_dir, f"events-{today_str}.jsonl")
        events = AuditLogReader.read_events_from_file(log_file) if os.path.exists(log_file) else []
        event_names = [e["event_name"] for e in events]
        self.assertNotIn(EventName.LOCAL_CONFLICT_CHECK_STARTED, event_names)

    def test_C35_403_forbidden_blocks_submission(self):
        """C35: 403 权限异常时，禁止报名提交"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        self.mock_client.call.return_value = (403, {"code": 403, "msg": "Forbidden"})

        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": True}, tt=self.timetable, audit_logger=self.audit_logger)
        items = ActivityList([], complete=True)

        ok, reason = mgr.check_activity_conflict(cand, items)
        self.assertFalse(ok)
        self.assertIn("403", reason)
        self.assertIn("权限不足", reason)

        self.assertEqual(mgr.breaker.state, ConflictCircuitState.NORMAL)
        self.assertEqual(mgr.breaker.failure_count, 0)

    def test_C36_400_bad_request_does_not_accumulate_infra_failure(self):
        """C36: 400 参数异常不累计基础设施熔断次数"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        self.mock_client.call.return_value = (400, {"code": 400, "msg": "Bad Request"})

        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": True}, tt=self.timetable, audit_logger=self.audit_logger)
        items = ActivityList([], complete=True)

        # 连续触发 5 次 400
        for _ in range(5):
            ok, reason = mgr.check_activity_conflict(cand, items)
            self.assertFalse(ok)
            self.assertIn("400", reason)

        # 熔断器依然为 NORMAL，基础设施失败计数严格为 0
        self.assertEqual(mgr.breaker.failure_count, 0)
        self.assertEqual(mgr.breaker.state, ConflictCircuitState.NORMAL)

    def test_C37_only_retryable_allows_local_evaluation(self):
        """C37: 只有 RETRYABLE 时，完成重新校验后允许进入本地评估"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}

        # 写入一条 RETRYABLE 记录 (已被确认未报名，可重试)
        self.state_store.record_submitting("act_reconciled", "排球")
        self.state_store.record_retryable("act_reconciled", reason="全局对账确认未报名")

        # 对账后拉取最新报名列表
        time.sleep(0.01)
        items = ActivityList([], complete=True, fetched_at=time.time())

        # 状态检查：未决列表中不包含 RETRYABLE
        unresolved = self.state_store.get_unresolved_records()
        self.assertEqual(len(unresolved), 0)

        # 本地冲突评估放行
        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": False}, tt=self.timetable, state_store=self.state_store)
        ok, reason = mgr.check_activity_conflict(cand, items)
        self.assertTrue(ok)
        self.assertIn("本地规则检查通过", reason)

    def test_C38_unknown_with_retryable_still_blocked(self):
        """C38: 存在 UNKNOWN 时，即使同时存在 RETRYABLE，也必须 BLOCKED"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}

        # 一条 RETRYABLE
        self.state_store.record_submitting("act_reconciled", "排球")
        self.state_store.record_retryable("act_reconciled", reason="全局对账确认未报名")

        # 另一条未决 UNKNOWN
        self.state_store.record_submitting("act_unknown", "足球")
        self.state_store.record_unknown("act_unknown", "足球", error="网络超时未决")

        items = ActivityList([], complete=True, fetched_at=time.time())

        # 本地冲突评估必须安全阻断
        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": False}, tt=self.timetable, state_store=self.state_store)
        ok, reason = mgr.check_activity_conflict(cand, items)
        self.assertFalse(ok)
        self.assertIn("UNKNOWN", reason)
        self.assertIn("安全屏障阻断", reason)

    # =========================================================================
    # Phase 5 最终补充用例 (C39 ~ C48)：HALF_OPEN 恢复与快照上下文安全
    # =========================================================================

    def test_C39_half_open_probe_401_releases_probe_permit(self):
        """C39: HALF_OPEN 探测返回 401，探测许可正确释放"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        self.mock_client.call.return_value = (401, {"code": 401, "msg": "Token expired"})

        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": True}, tt=self.timetable, audit_logger=self.audit_logger)
        mgr.breaker.state = ConflictCircuitState.HALF_OPEN
        mgr.breaker._half_open_probe_in_progress = False

        items = ActivityList([], complete=True)
        ok, reason = mgr.check_activity_conflict(cand, items)
        self.assertFalse(ok)
        self.assertIn("401", reason)

        # 状态保持 HALF_OPEN，且探测许可正确释放 (False)
        self.assertEqual(mgr.breaker.state, ConflictCircuitState.HALF_OPEN)
        self.assertFalse(mgr.breaker._half_open_probe_in_progress)
        self.assertEqual(mgr.breaker.failure_count, 0)

    def test_C40_auth_recovered_half_open_re_probes_and_recovers_to_normal(self):
        """C40: 认证恢复后，HALF_OPEN 可以重新进行健康探测并恢复 NORMAL"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": True}, tt=self.timetable, audit_logger=self.audit_logger)
        mgr.breaker.state = ConflictCircuitState.HALF_OPEN
        mgr.breaker._half_open_probe_in_progress = False

        # 1. 第一次探测遇到 401
        self.mock_client.call.return_value = (401, {"code": 401, "msg": "Token expired"})
        items = ActivityList([], complete=True)
        ok1, reason1 = mgr.check_activity_conflict(cand, items)
        self.assertFalse(ok1)
        self.assertEqual(mgr.breaker.state, ConflictCircuitState.HALF_OPEN)
        self.assertFalse(mgr.breaker._half_open_probe_in_progress)

        # 2. 认证恢复 (Token 刷新成功)，再次探测返回 200 有效响应
        self.mock_client.call.return_value = (200, {"code": 0, "data": False})
        ok2, reason2 = mgr.check_activity_conflict(cand, items)
        self.assertTrue(ok2)
        self.assertIn("服务端判定无冲突", reason2)
        # 熔断器平滑恢复 NORMAL
        self.assertEqual(mgr.breaker.state, ConflictCircuitState.NORMAL)
        self.assertFalse(mgr.breaker._half_open_probe_in_progress)

    def test_C41_half_open_probe_403_does_not_recover_normal_and_releases_lock(self):
        """C41: HALF_OPEN 探测返回 403，不错误恢复 NORMAL，也不永久卡死"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        self.mock_client.call.return_value = (403, {"code": 403, "msg": "Forbidden"})

        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": True}, tt=self.timetable, audit_logger=self.audit_logger)
        mgr.breaker.state = ConflictCircuitState.HALF_OPEN
        mgr.breaker._half_open_probe_in_progress = False

        items = ActivityList([], complete=True)
        ok, reason = mgr.check_activity_conflict(cand, items)
        self.assertFalse(ok)
        self.assertIn("403", reason)

        # 绝不误恢复为 NORMAL
        self.assertEqual(mgr.breaker.state, ConflictCircuitState.HALF_OPEN)
        # 探测许可必须释放，避免永久卡死
        self.assertFalse(mgr.breaker._half_open_probe_in_progress)
        can_retry, _ = mgr.breaker.can_attempt()
        self.assertTrue(can_retry)

    def test_C42_half_open_probe_400_no_infra_accumulation_and_can_reschedule(self):
        """C42: HALF_OPEN 探测返回 400，不累计基础设施故障，并能够重新调度探测"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        self.mock_client.call.return_value = (400, {"code": 400, "msg": "Bad request"})

        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": True}, tt=self.timetable, audit_logger=self.audit_logger)
        mgr.breaker.state = ConflictCircuitState.HALF_OPEN
        mgr.breaker._half_open_probe_in_progress = False

        items = ActivityList([], complete=True)
        ok, reason = mgr.check_activity_conflict(cand, items)
        self.assertFalse(ok)
        self.assertIn("400", reason)

        self.assertEqual(mgr.breaker.failure_count, 0)
        self.assertEqual(mgr.breaker.state, ConflictCircuitState.HALF_OPEN)
        self.assertFalse(mgr.breaker._half_open_probe_in_progress)
        can_retry, _ = mgr.breaker.can_attempt()
        self.assertTrue(can_retry)

    def test_C43_half_open_concurrent_requests_only_one_probes_others_degrade(self):
        """C43: HALF_OPEN 多个并发请求仍然最多只有一个实际探测，其余请求自动降级"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        items = ActivityList([], complete=True)

        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": True}, tt=self.timetable, audit_logger=self.audit_logger)
        mgr.breaker.state = ConflictCircuitState.HALF_OPEN
        mgr.breaker._half_open_probe_in_progress = False

        # 模拟第一个并发请求已经获取了探测许可并在执行中
        can1, msg1 = mgr.breaker.can_attempt()
        self.assertTrue(can1)
        self.assertTrue(mgr.breaker._half_open_probe_in_progress)

        # 第二个并发请求调用 check_activity_conflict
        # 此时探测进行中，第二个请求禁止发起服务端调用，自动切入本地降级
        ok2, reason2 = mgr.check_activity_conflict(cand, items)
        self.assertTrue(ok2)
        self.assertIn("本地规则检查通过", reason2)
        # 服务端接口未被并发调用
        self.mock_client.call.assert_not_called()

    def test_C44_different_users_snapshots_cannot_be_mixed(self):
        """C44: 不同用户的快照不能混用"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        checker = LocalConflictChecker()

        items_userA = ActivityList([], complete=True, fetched_at=time.time(), user_id="u_alice_123")
        ok, msg, details = checker.check_data_integrity(cand, items_userA, current_user_id="u_bob_456")
        self.assertFalse(ok)
        self.assertIn("用户范围不匹配", msg)

        res = checker.check_conflict(cand, items_userA, current_user_id="u_bob_456")
        self.assertTrue(res.is_blocked)
        self.assertEqual(res.status, "BLOCKED")

        # ConflictManager 降级时也严格阻断
        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": False}, tt=self.timetable, user_id="u_bob_456")
        ok_mgr, reason_mgr = mgr.check_activity_conflict(cand, items_userA)
        self.assertFalse(ok_mgr)
        self.assertIn("用户范围不匹配", reason_mgr)

    def test_C45_old_snapshot_invalid_after_session_switch(self):
        """C45: 认证会话切换后，旧快照必须失效"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        checker = LocalConflictChecker()

        items_old_sess = ActivityList([], complete=True, fetched_at=time.time(), session_scope_id="sess_old_token_111")
        ok, msg, details = checker.check_data_integrity(cand, items_old_sess, current_session_scope_id="sess_new_token_222")
        self.assertFalse(ok)
        self.assertIn("认证会话不匹配", msg)

        res = checker.check_conflict(cand, items_old_sess, current_session_scope_id="sess_new_token_222")
        self.assertTrue(res.is_blocked)
        self.assertEqual(res.status, "BLOCKED")

        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": False}, tt=self.timetable, session_scope_id="sess_new_token_222")
        ok_mgr, reason_mgr = mgr.check_activity_conflict(cand, items_old_sess)
        self.assertFalse(ok_mgr)
        self.assertIn("认证会话不匹配", reason_mgr)

    def test_C46_snapshot_complete_true_only_when_all_pages_succeed(self):
        """C46: 快照 complete=True 只有在所有分页成功后才能成立"""
        from lejian import Client
        fake_state = {"semesterId": "sem_2026", "refreshToken": "test_ref", "userId": "user_01"}
        client = Client(fake_state)

        # 1. 模拟分页拉取：总计 20 条，第 1 页成功 (10条)，第 2 页失败 (500)
        client.call = MagicMock(side_effect=[
            (200, {"code": 0, "data": {"items": [{"id": f"act_{i}", "startTime": 1731132000000, "endTime": 1731137700000} for i in range(10)], "total": 20}}),
            (500, {"code": -1, "msg": "Internal Server Error"})
        ])
        incomplete_list = client.activities()
        self.assertFalse(incomplete_list.complete)
        self.assertEqual(incomplete_list.fetched, 10)
        self.assertEqual(incomplete_list.total, 20)
        self.assertIn("第 2 页拉取失败", incomplete_list.error)

        # 2. 模拟全部分页均成功
        client.call = MagicMock(side_effect=[
            (200, {"code": 0, "data": {"items": [{"id": f"act_{i}", "startTime": 1731132000000, "endTime": 1731137700000} for i in range(10)], "total": 20}}),
            (200, {"code": 0, "data": {"items": [{"id": f"act_{i}", "startTime": 1731132000000, "endTime": 1731137700000} for i in range(10, 20)], "total": 20}})
        ])
        complete_list = client.activities()
        self.assertTrue(complete_list.complete)
        self.assertEqual(complete_list.fetched, 20)
        self.assertEqual(complete_list.total, 20)
        self.assertIsNone(complete_list.error)

    def test_C47_refresh_failed_cannot_reuse_expired_snapshot(self):
        """C47: 活动列表刷新失败后，不得沿用过期快照放行"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        expired_items = ActivityList([], complete=True, fetched_at=time.time() - 120.0)

        # 模拟刷新失败 (activities 返回 complete=False)
        self.mock_client.activities = MagicMock(return_value=ActivityList([], complete=False, error="刷新超时失败"))

        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": False}, tt=self.timetable)
        ok, reason = mgr.check_activity_conflict(cand, expired_items, max_snapshot_age_sec=60.0)

        self.assertFalse(ok)
        self.mock_client.activities.assert_called_once()
        # 绝不放行，严格阻断
        self.assertTrue("安全阻断" in reason or "分页不完整" in reason or "超过有效期" in reason)

    def test_C48_same_run_id_but_different_session_scope_id_blocked(self):
        """C48: 相同 run_id 但不同 session_scope_id 必须 BLOCKED"""
        cand = {"id": "act_01", "name": "羽毛球", "startTime": 1731132000000, "endTime": 1731137700000}
        checker = LocalConflictChecker()

        items = ActivityList([], complete=True, fetched_at=time.time(), run_id="run_batch_2026", session_scope_id="sess_scope_AAA")
        ok, msg, details = checker.check_data_integrity(
            cand, items, current_run_id="run_batch_2026", current_session_scope_id="sess_scope_BBB"
        )
        self.assertFalse(ok)
        self.assertIn("认证会话不匹配", msg)

        res = checker.check_conflict(
            cand, items, current_run_id="run_batch_2026", current_session_scope_id="sess_scope_BBB"
        )
        self.assertTrue(res.is_blocked)
        self.assertEqual(res.status, "BLOCKED")

        mgr = ConflictManager(self.mock_client, cfg={"check_conflict": False}, tt=self.timetable, session_scope_id="sess_scope_BBB")
        ok_mgr, reason_mgr = mgr.check_activity_conflict(cand, items, run_id="run_batch_2026")
        self.assertFalse(ok_mgr)
        self.assertIn("认证会话不匹配", reason_mgr)


if __name__ == "__main__":
    unittest.main()
