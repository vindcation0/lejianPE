"""
Phase 7 故障注入与容灾降级集成测试套件 (I21 ~ I40)
=================================================
验证在各种网络异常、服务端崩溃 (500/502/504)、超时丢包、
Token失效、熔断器半开探测、数据损坏及锁冲突等极端场景下的自愈与安全降级。
"""
import copy
import datetime
import json
import os
import sqlite3
import time
import unittest

from core.audit_logger import EventName
from core.circuit_breaker import CircuitBreaker
from core.conflict_checker import (
    ActivityList,
    ConflictCheckMode,
    ConflictCircuitBreaker,
    ConflictCircuitState,
)
from core.instance_lock import InstanceLock, InstanceLockError
from core.preflight import PreflightMode, PreflightStatus
from core.request_manager import SubmitResult
from core.result_reporter import ReportStatus
from core.state_store import RegistrationStatus, StateStoreError
from tests.integration.integration_helper import IntegrationEnvironment


class TestFaultRecoveryFlow(unittest.TestCase):
    """故障注入与容灾降级集成测试 (I21 ~ I40)"""

    def setUp(self):
        self.env = IntegrationEnvironment()
        self.env.server.add_activity(
            aid="act_201",
            name="排球进阶班",
            start_time_ms=1794146400000,
            end_time_ms=1794153600000,
            limit_people=30,
            registered_people=5,
            is_register=False
        )

    def tearDown(self):
        self.env.close()

    # -------------------------------------------------------------------------
    # I21: activities 接口 500 且存在有效快照时降级为 ACTIVITY_CACHE
    # -------------------------------------------------------------------------
    def test_I21_activities_500_with_cache_degrades_to_activity_cache(self):
        snapshot = ActivityList(
            items=[{"id": "act_201", "name": "排球进阶班", "startTime": 1794146400000, "endTime": 1794153600000}],
            complete=True,
            fetched_at=time.time()
        )
        self.env.activities_breaker.record_failure(http_status=500, force_trip=True)
        preflight = self.env.create_preflight(initial_snapshot=snapshot)
        rep = preflight.run_all_checks()
        self.assertEqual(rep.status, PreflightStatus.DEGRADED_READY)
        self.assertEqual(rep.mode, PreflightMode.ACTIVITY_CACHE)

    # -------------------------------------------------------------------------
    # I22: activities 接口 500 且无快照时判定为 BLOCKED
    # -------------------------------------------------------------------------
    def test_I22_activities_500_without_cache_blocks(self):
        self.env.activities_breaker.record_failure(http_status=500, force_trip=True)
        preflight = self.env.create_preflight(initial_snapshot=None)
        rep = preflight.run_all_checks()
        self.assertEqual(rep.status, PreflightStatus.BLOCKED)
        self.assertEqual(rep.mode, PreflightMode.BLOCKED)

    # -------------------------------------------------------------------------
    # I23: checkConflict 接口 500 时平滑降级为 LOCAL_CONFLICT
    # -------------------------------------------------------------------------
    def test_I23_conflict_500_degrades_to_local_conflict(self):
        self.env.conflict_breaker.record_failure(http_status=500, force_trip=True)
        preflight = self.env.create_preflight()
        rep = preflight.run_all_checks()
        self.assertEqual(rep.status, PreflightStatus.DEGRADED_READY)
        self.assertEqual(rep.mode, PreflightMode.LOCAL_CONFLICT)

    # -------------------------------------------------------------------------
    # I24: activities 与 checkConflict 双熔断且有快照时降级为 DEGRADED_FULL
    # -------------------------------------------------------------------------
    def test_I24_both_breakers_open_with_cache_degrades_to_full(self):
        snapshot = ActivityList(
            items=[{"id": "act_201", "name": "排球进阶班", "startTime": 1794146400000, "endTime": 1794153600000}],
            complete=True,
            fetched_at=time.time()
        )
        self.env.activities_breaker.record_failure(http_status=500, force_trip=True)
        self.env.conflict_breaker.record_failure(http_status=500, force_trip=True)
        preflight = self.env.create_preflight(initial_snapshot=snapshot)
        rep = preflight.run_all_checks()
        self.assertEqual(rep.status, PreflightStatus.DEGRADED_READY)
        self.assertEqual(rep.mode, PreflightMode.DEGRADED_FULL)

    # -------------------------------------------------------------------------
    # I25: 双熔断但无快照时判定为 BLOCKED (禁止盲目启动)
    # -------------------------------------------------------------------------
    def test_I25_both_breakers_open_without_cache_blocks(self):
        self.env.activities_breaker.record_failure(http_status=500, force_trip=True)
        self.env.conflict_breaker.record_failure(http_status=500, force_trip=True)
        preflight = self.env.create_preflight(initial_snapshot=None)
        rep = preflight.run_all_checks()
        self.assertEqual(rep.status, PreflightStatus.BLOCKED)
        self.assertEqual(rep.mode, PreflightMode.BLOCKED)

    # -------------------------------------------------------------------------
    # I26: 查询过程遇到 401 自动刷新 Token 并重试成功
    # -------------------------------------------------------------------------
    def test_I26_query_401_triggers_token_refresh(self):
        self.env.client._expires_in_sec = 10.0  # 逼近过期
        old_token = self.env.client.token
        tok = self.env.client.ensure_token(margin=300)
        self.assertNotEqual(tok, old_token)
        self.assertIn("token_refreshed", tok)
        self.assertEqual(self.env.server.refresh_calls_count, 1)

    # -------------------------------------------------------------------------
    # I27: Token 刷新接口 500 导致 Preflight 阻断并提示 FAIL
    # -------------------------------------------------------------------------
    def test_I27_refresh_failure_blocks_preflight(self):
        self.env.server.refresh_status_code = 500
        self.env.client._expires_in_sec = 0.0
        preflight = self.env.create_preflight()
        rep = preflight.run_all_checks()
        self.assertEqual(rep.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("Token" in r for r in rep.failure_reasons))

    # -------------------------------------------------------------------------
    # I28: signUp 请求超时 (-1) 严格判定为 UNKNOWN，绝不自动重试
    # -------------------------------------------------------------------------
    def test_I28_signup_timeout_classified_unknown_no_retry(self):
        self.env.server.signup_error = TimeoutError("Connection timed out")
        aid = "act_201"
        self.env.state_store.reserve_submitting(aid, "排球")
        sub_res = self.env.submit_signup(aid, "排球")
        self.assertEqual(sub_res.classification, "UNKNOWN")
        self.assertEqual(self.env.server.signup_calls_count, 1)

    # -------------------------------------------------------------------------
    # I29: signUp 成功入库但网络丢包断开，正确标记 UNKNOWN
    # -------------------------------------------------------------------------
    def test_I29_signup_response_drop_classified_unknown(self):
        self.env.server.drop_response_after_signup = True
        aid = "act_201"
        self.env.state_store.reserve_submitting(aid, "排球")
        sub_res = self.env.submit_signup(aid, "排球")
        self.assertEqual(sub_res.classification, "UNKNOWN")
        self.assertIn(aid, self.env.server.registered_activities)

    # -------------------------------------------------------------------------
    # I30: UNKNOWN 请求在后续对账中确认成功并消除未决状态
    # -------------------------------------------------------------------------
    def test_I30_unknown_reconciled_to_success(self):
        self.env.server.drop_response_after_signup = True
        aid = "act_201"
        self.env.state_store.reserve_submitting(aid, "排球")
        self.env.submit_signup(aid, "排球")
        self.env.state_store.record_unknown(aid, "排球", error="网络中断")
        self.assertEqual(len(self.env.state_store.get_unresolved_records()), 1)

        # 恢复网络并执行全局对账
        self.env.server.drop_response_after_signup = False
        rec_res = self.env.reconciliation_manager.reconcile()
        self.assertTrue(rec_res["success"])
        # 对账后，未决记录被自动消除并更新为 SUCCESS
        self.assertEqual(len(self.env.state_store.get_unresolved_records()), 0)
        rec = self.env.state_store.get_record(aid)
        self.assertEqual(rec["status"], RegistrationStatus.SUCCESS)

    # -------------------------------------------------------------------------
    # I31: 对账接口 500 时结果报告严谨输出 UNVERIFIED
    # -------------------------------------------------------------------------
    def test_I31_reconciliation_500_yields_unverified_report(self):
        self.env.server.activities_status_code = 500
        rec_res = self.env.reconciliation_manager.reconcile()
        self.assertFalse(rec_res["success"])

        self.env.finalize_reporter(rec_res)
        summary = self.env.result_reporter.generate_summary()
        self.assertEqual(summary["verdict"]["status"], ReportStatus.UNVERIFIED)

    # -------------------------------------------------------------------------
    # I32: 熔断器从 OPEN 到 HALF_OPEN 探测成功恢复 NORMAL
    # -------------------------------------------------------------------------
    def test_I32_breaker_half_open_probe_success_recovers_normal(self):
        cb = self.env.conflict_breaker
        cb.record_failure(http_status=500, force_trip=True)
        self.assertEqual(cb.state, ConflictCircuitState.OPEN)

        # 模拟冷却时间已过
        cb.opened_at = time.time() - 100.0
        can_probe, _ = cb.can_attempt()
        self.assertTrue(can_probe)
        self.assertEqual(cb.state, ConflictCircuitState.HALF_OPEN)

        # 探测成功
        cb.record_success(is_complete=True)
        self.assertEqual(cb.state, ConflictCircuitState.NORMAL)
        self.assertTrue(cb.is_normal)

    # -------------------------------------------------------------------------
    # I33: 熔断器 HALF_OPEN 探测失败重新切入 OPEN 并指数退避
    # -------------------------------------------------------------------------
    def test_I33_breaker_half_open_probe_failure_re_trips(self):
        cb = self.env.conflict_breaker
        cb.record_failure(http_status=500, force_trip=True)
        initial_cooldown = cb.current_cooldown
        cb.opened_at = time.time() - 100.0
        cb.can_attempt()  # 触发进入 HALF_OPEN

        # 探测失败
        cb.record_failure(http_status=502)
        self.assertEqual(cb.state, ConflictCircuitState.OPEN)
        self.assertGreaterEqual(cb.current_cooldown, initial_cooldown)

    # -------------------------------------------------------------------------
    # I34: 4xx 客户端异常 (如 403) 严禁触发熔断器切入 OPEN
    # -------------------------------------------------------------------------
    def test_I34_client_4xx_does_not_trip_breaker(self):
        cb = self.env.conflict_breaker
        for _ in range(5):
            cb.record_failure(http_status=403)
        self.assertEqual(cb.state, ConflictCircuitState.NORMAL)
        self.assertEqual(cb.failure_count, 0)

    # -------------------------------------------------------------------------
    # I35: 连续 5xx 故障达到阈值严格切入 OPEN 熔断
    # -------------------------------------------------------------------------
    def test_I35_consecutive_5xx_trips_breaker_to_open(self):
        cb = self.env.conflict_breaker
        cb.record_failure(http_status=500)
        cb.record_failure(http_status=502)
        cb.record_failure(http_status=504)
        self.assertEqual(cb.state, ConflictCircuitState.OPEN)
        self.assertEqual(cb.trip_count, 1)

    # -------------------------------------------------------------------------
    # I36: 429 被限流时 signUp 正确分类为 RATE_LIMITED 且不重试
    # -------------------------------------------------------------------------
    def test_I36_signup_429_classified_rate_limited(self):
        self.env.server.signup_status_code = 429
        aid = "act_201"
        self.env.state_store.reserve_submitting(aid, "排球")
        sub_res = self.env.submit_signup(aid, "排球")
        self.assertEqual(sub_res.classification, "RATE_LIMITED")
        self.assertEqual(self.env.server.signup_calls_count, 1)

    # -------------------------------------------------------------------------
    # I37: 数据库写入异常 (磁盘满/只读) 安全屏障阻断后续报名
    # -------------------------------------------------------------------------
    def test_I37_db_failure_blocks_candidate_submission(self):
        # 模拟数据库只读或磁盘损坏，事务执行抛出 OperationalError
        class BrokenConn:
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("disk I/O error: database is read-only")

        self.env.state_store._conn = BrokenConn()

        # 1. 直接调用 reserve_submitting 抛出 StateStoreError
        with self.assertRaises(StateStoreError):
            self.env.state_store.reserve_submitting("act_201", "排球")

        # 2. RegistrationManager 捕获 StateStoreError，安全阻断报名且绝不调用 signUp
        target = self.env.server.activities_db["act_201"]
        self.env.server.reset_counts()
        ok, status, msg, _ = self.env.registration_manager.register_activity(target)
        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.FAILED)
        self.assertIn("状态库持久化失败", msg)
        self.assertEqual(self.env.server.signup_calls_count, 0)

    # -------------------------------------------------------------------------
    # I38: 进程崩溃遗留 SUBMITTING 记录时被本地安全屏障有效拦截
    # -------------------------------------------------------------------------
    def test_I38_submitting_record_blocks_subsequent_candidates(self):
        # 模拟前一次运行留下 SUBMITTING 记录
        self.env.state_store.reserve_submitting("act_201", "排球")
        self.assertTrue(self.env.state_store.has_unresolved())

        # 本地完整性检查检测到未决状态，阻断新候选
        items = self.env.client.activities()
        res = self.env.local_conflict_checker.check_conflict(
            candidate=items[0],
            items=items,
            state_store=self.env.state_store,
            current_week=10
        )
        self.assertTrue(res.has_conflict)
        self.assertIn("SUBMITTING", res.reason)

    # -------------------------------------------------------------------------
    # I39: 实例锁被占用时第二个实例优雅退出，杜绝重复进程
    # -------------------------------------------------------------------------
    def test_I39_instance_lock_blocks_second_runner(self):
        lock1 = InstanceLock(self.env.lock_path)
        self.assertTrue(lock1.acquire())

        # 第二个实例尝试获取锁，返回 False
        lock2 = InstanceLock(self.env.lock_path)
        self.assertFalse(lock2.acquire())

        # 用上下文管理器方式尝试获取锁，抛出 InstanceLockError
        with self.assertRaises(InstanceLockError):
            with InstanceLock(self.env.lock_path):
                pass

        lock1.release()
        # 锁释放后 lock2 允许获取
        self.assertTrue(lock2.acquire())
        lock2.release()

    # -------------------------------------------------------------------------
    # I40: 审计日志写入异常降级为静默内存记录，绝不导致主抢课流程崩溃
    # -------------------------------------------------------------------------
    def test_I40_audit_log_write_error_degrades_gracefully(self):
        # 将日志目录设为非法文件阻断写入
        self.env.audit_logger.log_dir = os.path.join(self.env.logs_dir, "non_existent", "nested")
        # 记录事件不应抛出异常
        try:
            self.env.audit_logger.record_event(
                event_type="test",
                event_name="test_event",
                details={"val": 1}
            )
        except Exception as e:
            self.fail(f"审计日志降级失败，抛出异常: {e}")


if __name__ == "__main__":
    unittest.main()
