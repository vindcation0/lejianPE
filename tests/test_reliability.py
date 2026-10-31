"""
自动化可靠性测试套件 (tests/test_reliability.py)
==============================================
覆盖《实施流程报告》第 4.14 节要求的全部 15 个测试场景 (T01 ~ T15)，
使用标准库 unittest 与 MockClient 模拟各种网络故障、网关异常、超时、崩溃与并发场景。
绝不向学校服务器发送真实重复请求。
"""
import datetime
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.instance_lock import InstanceLock, InstanceLockError
from core.state_store import StateStore, RegistrationStatus, StateStoreError
from core.audit_logger import AuditLogger
from core.retry_policy import RetryPolicy, HealthState
from core.request_manager import RequestManager, SubmitResult
from core.registration_manager import RegistrationManager
from core.recovery_manager import RecoveryManager
from core.reconciliation import ReconciliationManager


class MockClient:
    """模拟学校服务端的 Client"""

    def __init__(self):
        self.submit_response = (200, {"code": 0, "data": {"success": True}})
        self.activities_list = []
        self.query_response = (200, {"code": 0, "data": {"items": []}})
        self.refresh_called = False
        self.call_history = []

    def call(self, method, path, body=None, timeout=6):
        self.call_history.append((method, path, body))
        if path == "education/app/activity/signUp":
            return self.submit_response
        return self.query_response

    def activities(self, page_size=200, req_timeout=6):
        if isinstance(self.activities_list, Exception):
            raise self.activities_list
        return list(self.activities_list)

    def refresh(self):
        self.refresh_called = True
        return "new_access_token"

    def check_conflict(self, aid):
        return False


class ReliabilityTestSuite(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_reg.db")
        self.logs_dir = os.path.join(self.tmp_dir, "logs")
        self.lock_path = os.path.join(self.tmp_dir, "grab.lock")

        self.store = StateStore(self.db_path)
        self.audit = AuditLogger(self.logs_dir)
        self.mock_client = MockClient()
        self.retry_policy = RetryPolicy(base_delay=0.1, max_delay=1.0)
        self.req_mgr = RequestManager(self.mock_client, self.retry_policy)
        self.reg_mgr = RegistrationManager(self.store, self.req_mgr, self.audit)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # ---------------------------------------------------- T01
    def test_T01_submit_success_200(self):
        """T01: 提交返回 200 成功 -> SUCCESS"""
        self.mock_client.submit_response = (200, {"code": 0, "data": {"success": True}})
        item = {"id": "act_01", "name": "户外跑", "startRegisterTime": 0, "endRegisterTime": 9999999999999, "limitPeopleNumber": 50, "registerPeople": 10}

        ok, status, msg, _ = self.reg_mgr.register_activity(item)
        self.assertTrue(ok)
        self.assertEqual(status, RegistrationStatus.SUCCESS)

        rec = self.store.get_record("act_01")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], RegistrationStatus.SUCCESS)
        self.assertEqual(rec["attempts"], 1)

    # ---------------------------------------------------- T02
    def test_T02_submit_503_verify_success(self):
        """T02: 提交返回 503，复核 isRegister=true -> SUCCESS (核心实战场景)"""
        # 模拟提交报 503 网关错误
        self.mock_client.submit_response = (503, {"_raw": "Gateway Timeout / Connection Refused"})
        # 但服务端实际上已经写入报名 (isRegister=True)
        self.mock_client.activities_list = [
            {"id": "act_02", "name": "户外跑", "isRegister": True, "startTime": 1787673600000}
        ]
        item = {"id": "act_02", "name": "户外跑", "startRegisterTime": 0, "endRegisterTime": 9999999999999, "limitPeopleNumber": 50, "registerPeople": 10}

        ok, status, msg, _ = self.reg_mgr.register_activity(item)
        self.assertTrue(ok)
        self.assertEqual(status, RegistrationStatus.SUCCESS)
        self.assertIn("复核确认报名成功", msg)

        rec = self.store.get_record("act_02")
        self.assertEqual(rec["status"], RegistrationStatus.SUCCESS)

    # ---------------------------------------------------- T03
    def test_T03_submit_timeout_verify_timeout(self):
        """T03: 提交超时，复核请求也超时 -> UNKNOWN (严禁自动重试)"""
        self.mock_client.submit_response = (-1, {"_raw": "Timed out after 6 seconds"})
        # 复核接口也抛出异常或超时
        self.mock_client.activities_list = Exception("Activities network timeout")
        item = {"id": "act_03", "name": "户外跑", "startRegisterTime": 0, "endRegisterTime": 9999999999999, "limitPeopleNumber": 50, "registerPeople": 10}

        ok, status, msg, _ = self.reg_mgr.register_activity(item)
        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.UNKNOWN)

        rec = self.store.get_record("act_03")
        self.assertEqual(rec["status"], RegistrationStatus.UNKNOWN)

    # ---------------------------------------------------- T04
    def test_T04_submit_503_verify_not_registered(self):
        """T04: 提交返回 503，复核 isRegister=false -> RETRYABLE"""
        self.mock_client.submit_response = (503, {"_raw": "Service Unavailable"})
        self.mock_client.activities_list = [
            {"id": "act_04", "name": "户外跑", "isRegister": False, "startTime": 1787673600000}
        ]
        item = {"id": "act_04", "name": "户外跑", "startRegisterTime": 0, "endRegisterTime": 9999999999999, "limitPeopleNumber": 50, "registerPeople": 10}

        ok, status, msg, _ = self.reg_mgr.register_activity(item)
        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.RETRYABLE)

        rec = self.store.get_record("act_04")
        self.assertEqual(rec["status"], RegistrationStatus.RETRYABLE)

    # ---------------------------------------------------- T05
    def test_T05_verify_missing_activity_keeps_unknown(self):
        """T05: 复核活动列表缺失目标活动 -> UNKNOWN (不可判定为未报名)"""
        self.mock_client.submit_response = (-1, {"_raw": "Socket timeout"})
        # 列表中返回了其他活动，但缺失 act_05
        self.mock_client.activities_list = [
            {"id": "other_activity", "name": "篮球", "isRegister": False}
        ]
        item = {"id": "act_05", "name": "户外跑", "startRegisterTime": 0, "endRegisterTime": 9999999999999, "limitPeopleNumber": 50, "registerPeople": 10}

        ok, status, msg, _ = self.reg_mgr.register_activity(item)
        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.UNKNOWN)

        rec = self.store.get_record("act_05")
        self.assertEqual(rec["status"], RegistrationStatus.UNKNOWN)

    # ---------------------------------------------------- T06
    def test_T06_single_instance_lock(self):
        """T06: 两个抢课程序同时启动 -> 只有一个获得锁"""
        lock1 = InstanceLock(self.lock_path)
        lock2 = InstanceLock(self.lock_path)

        self.assertTrue(lock1.acquire(), "第一个实例应成功获取锁")
        self.assertFalse(lock2.acquire(), "第二个实例应被拒绝获取锁")

        lock1.release()
        self.assertTrue(lock2.acquire(), "第一个实例释放后，第二个实例可获取锁")
        lock2.release()

    # ---------------------------------------------------- T07
    def test_T07_crash_after_success_recovery(self):
        """T07: 报名成功后程序崩溃 -> 重启后从 SQLite 恢复 SUCCESS"""
        self.store.record_success("act_07", "户外跑", 200)

        # 模拟重启：新建 store 与 recovery_manager
        new_store = StateStore(self.db_path)
        rec = new_store.get_record("act_07")
        self.assertEqual(rec["status"], RegistrationStatus.SUCCESS)

        # 再次尝试抢课，应立即被拦截并返回 SUCCESS，禁止二次提交
        new_reg_mgr = RegistrationManager(new_store, self.req_mgr, self.audit)
        item = {"id": "act_07", "name": "户外跑"}
        ok, status, msg, _ = new_reg_mgr.register_activity(item)
        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.SUCCESS)
        self.assertIn("已存在报名成功记录", msg)
        new_store.close()

    # ---------------------------------------------------- T08
    def test_T08_crash_during_submitting_recovery(self):
        """T08: 提交过程中程序崩溃 -> 重启后先复核，不直接重试"""
        # 模拟崩溃遗留的 SUBMITTING 记录
        self.store.record_submitting("act_08", "户外跑")

        # 服务端实际已经存在报名
        self.mock_client.activities_list = [
            {"id": "act_08", "name": "户外跑", "isRegister": True}
        ]

        # 重启恢复管理器
        recovery_mgr = RecoveryManager(self.store, self.req_mgr, self.audit)
        summary = recovery_mgr.recover_on_startup()

        self.assertEqual(len(summary["recovered_success"]), 1)
        rec = self.store.get_record("act_08")
        self.assertEqual(rec["status"], RegistrationStatus.SUCCESS)

    # ---------------------------------------------------- T09
    def test_T09_consecutive_query_failures_backoff(self):
        """T09: 连续查询失败 -> 触发指数退避并进入 DEGRADED"""
        policy = RetryPolicy(base_delay=0.5, max_delay=4.0, degraded_threshold=2)

        policy.record_failure(500)
        self.assertEqual(policy.state, HealthState.NORMAL)
        d1 = policy.get_backoff_delay()
        self.assertGreaterEqual(d1, 0.5)

        policy.record_failure(502)
        self.assertEqual(policy.state, HealthState.DEGRADED)
        d2 = policy.get_backoff_delay()
        self.assertGreaterEqual(d2, 1.0)

        policy.record_success()
        self.assertEqual(policy.state, HealthState.RECOVERING)

    # ---------------------------------------------------- T10
    def test_T10_rate_limit_429(self):
        """T10: 收到 HTTP 429 -> 遵守限流要求"""
        policy = RetryPolicy(base_delay=0.5)
        # 指定 retry_after 3 秒
        delay = policy.get_backoff_delay(retry_after=3.0)
        self.assertEqual(delay, 3.0)

        # submit_once 返回 429
        self.mock_client.submit_response = (429, {"message": "Too Many Requests"})
        res = self.req_mgr.submit_once("education/app/activity/signUp", {"activityId": "act_10"})
        self.assertEqual(res.classification, "RATE_LIMITED")

    # ---------------------------------------------------- T11
    def test_T11_reconciliation_exceeded_alert(self):
        """T11: 抢到第三节 -> 检测超额并发出告警"""
        cfg = {"max_normal": 2, "max_per_day": 1, "special": []}
        tt = {"week1_monday": "2026-09-07"}

        def week_calc(d, t):
            return 8

        # 服务端已落 3 节同一周的普通课程
        self.mock_client.activities_list = [
            {"id": "a1", "name": "户外跑1", "isRegister": True, "startTime": 1793260800000},
            {"id": "a2", "name": "户外跑2", "isRegister": True, "startTime": 1793347200000},
            {"id": "a3", "name": "户外跑3", "isRegister": True, "startTime": 1793433600000},
        ]

        reconciler = ReconciliationManager(self.mock_client, cfg, tt, week_calc)
        report = reconciler.reconcile()

        self.assertTrue(report["success"])
        self.assertTrue(report["exceeded"])
        self.assertEqual(report["total_registered"], 3)
        self.assertIn(8, report["exceeded_weeks"])
        self.assertEqual(report["exceeded_weeks"][8], 3)

    # ---------------------------------------------------- T12
    def test_T12_no_duplicate_submit_for_success(self):
        """T12: 程序重复运行 -> 不重复提交已经 SUCCESS 的活动"""
        self.store.record_success("act_12", "户外跑", 200)
        item = {"id": "act_12", "name": "户外跑"}

        calls_before = len(self.mock_client.call_history)
        ok, status, msg, _ = self.reg_mgr.register_activity(item)

        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.SUCCESS)
        calls_after = len(self.mock_client.call_history)
        self.assertEqual(calls_before, calls_after, "禁止发起任何网络提交请求")

    # ---------------------------------------------------- T13
    def test_T13_sqlite_write_error_prevents_unsafe_submission(self):
        """T13: SQLite 写入失败 -> 不得继续进行不安全的提交"""
        # 损坏或锁定数据库模拟写入失败
        broken_db_path = os.path.join(self.tmp_dir, "readonly_dir", "reg.db")
        # 通过在只读目录中创建或 mock 抛出异常
        store = StateStore(":memory:")
        store.record_submitting = lambda *args, **kwargs: (_ for _ in ()).throw(StateStoreError("Disk full or IO error"))

        reg_mgr = RegistrationManager(store, self.req_mgr, self.audit)
        item = {"id": "act_13", "name": "户外跑", "startRegisterTime": 0, "endRegisterTime": 9999999999999}

        calls_before = len(self.mock_client.call_history)
        ok, status, msg, _ = reg_mgr.register_activity(item)

        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.FAILED)
        self.assertIn("状态库持久化失败", msg)
        calls_after = len(self.mock_client.call_history)
        self.assertEqual(calls_before, calls_after, "状态库失败时绝不可发出网络请求")

    # ---------------------------------------------------- T14
    def test_T14_token_expired_refresh_recovery(self):
        """T14: Token 过期 (401) -> 自动刷新并重试"""
        calls = []

        def mock_call(method, path, body=None, timeout=6):
            calls.append(path)
            if len(calls) == 1:
                return 401, {"message": "Unauthorized"}
            return 200, {"code": 0, "data": {"items": [{"id": "act_14"}]}}

        self.mock_client.call = mock_call
        code, data = self.req_mgr.request_query("GET", "education/app/activity/getActivityList")

        self.assertEqual(code, 200)
        self.assertTrue(self.mock_client.refresh_called, "应当调用 token refresh")

    # ---------------------------------------------------- T15
    def test_T15_window_closed_prohibits_submission(self):
        """T15: 活动报名窗口已关闭 -> 校验失败不允许提交"""
        now_ms = time.time() * 1000
        # 报名已在 10 分钟前关闭
        item = {
            "id": "act_15",
            "name": "户外跑",
            "startRegisterTime": now_ms - 2000000,
            "endRegisterTime": now_ms - 600000,
            "limitPeopleNumber": 50,
            "registerPeople": 10
        }

        def validate_window(act_item):
            cur_ms = time.time() * 1000
            en = int(act_item.get("endRegisterTime") or 0)
            if en and en < cur_ms:
                return False, "活动不在报名时间窗口内"
            return True, "OK"

        calls_before = len(self.mock_client.call_history)
        ok, status, msg, _ = self.reg_mgr.register_activity(item, validate_rules_cb=validate_window)

        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.FAILED)
        self.assertIn("活动不在报名时间窗口内", msg)
        calls_after = len(self.mock_client.call_history)
        self.assertEqual(calls_before, calls_after, "窗口关闭不得发起提交")


if __name__ == "__main__":
    unittest.main()
