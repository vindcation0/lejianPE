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


class ActivityList(list):
    """带元数据的活动列表模拟对象"""
    def __init__(self, items=None, complete=True, error=None, total=0, fetched=0, http_code=200):
        super().__init__(items or [])
        self.complete = complete
        self.error = error
        self.total = total
        self.fetched = fetched
        self.http_code = http_code


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


if __name__ == "__main__":
    unittest.main()
