"""
Preflight 抢课前健康检查系统测试套件 (P01 ~ P28)
==============================================
覆盖 Phase 6 规定的所有 28 个验收测试：
- P01: 正常环境输出 READY。
- P02: checkConflict OPEN，但本地数据有效，输出 DEGRADED_READY。
- P03: Token 无效输出 BLOCKED。
- P04: Token 刷新失败输出 BLOCKED。
- P05: 刷新 Token 后旧快照失效。
- P06: activities 接口失败且无有效快照，BLOCKED。
- P07: 报名列表分页不完整，BLOCKED。
- P08: 快照超过有效期且刷新失败，BLOCKED。
- P09: 存在 UNKNOWN，BLOCKED。
- P10: 存在 SUBMITTING，BLOCKED。
- P11: 存在 VERIFYING，BLOCKED。
- P12: 仅存在 RETRYABLE，重新校验后允许继续。
- P13: StateStore 不可读，BLOCKED。
- P14: StateStore 不可写，BLOCKED。
- P15: InstanceLock 不可用，BLOCKED。
- P16: 存在其他抢课进程时明确报告。
- P17: 配额数据不完整，BLOCKED。
- P18: 特殊活动规则无法识别，BLOCKED。
- P19: 审计日志不可写，BLOCKED。
- P20: 审计日志测试不污染正式日志。
- P21: 系统时间偏差超出阈值，按规则阻断。
- P22: activities 和 checkConflict 的健康状态互不影响。
- P23: HALF_OPEN 探测并发仍然只有一次。
- P24: Preflight 不调用 signUp。
- P25: Preflight 不改变真实报名状态。
- P26: 重复执行 Preflight 不会产生数据库脏写。
- P27: Token 刷新并发受到保护。
- P28: 最终报告 run_id 与本次运行一致。
"""
import concurrent.futures
import copy
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.audit_logger import AuditLogger, EventName, EventType
from core.circuit_breaker import CircuitBreaker, CircuitState
from core.conflict_checker import (
    ActivityList,
    ConflictCircuitBreaker,
    ConflictState,
    LocalConflictChecker,
)
from core.instance_lock import InstanceLock
from core.preflight import (
    CheckItemStatus,
    PreflightChecker,
    PreflightMode,
    PreflightStatus,
)
from core.state_store import RegistrationStatus, StateStore


class MockClient:
    """Mock Client，严格模拟真实 Client 的各方法与并发锁"""
    def __init__(self, token="test_token_123", expires_in_sec=3600.0, user_id="user_test_01"):
        self.token = token
        self._expires_in_sec = expires_in_sec
        self.user_id = user_id
        self._session_id = "sess_" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        self._user_scope_id = "u_" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:12]
        self.s = {
            "accessToken": token,
            "refreshToken": "refresh_token_abc",
            "schoolId": "school_1",
            "semesterId": "semester_1",
            "userId": user_id
        }
        self._refresh_lock = threading.RLock()
        self.refresh_call_count = 0
        self.activities_call_count = 0
        self.signup_call_count = 0

    def expires_in(self) -> float:
        return self._expires_in_sec

    def get_session_scope_id(self) -> str:
        return self._session_id

    def get_user_scope_id(self) -> str:
        return self._user_scope_id

    def refresh(self) -> str:
        with self._refresh_lock:
            self.refresh_call_count += 1
            self.token = f"refreshed_token_{self.refresh_call_count}_{time.time()}"
            self._expires_in_sec = 3600.0
            self._session_id = "sess_" + hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:16]
            self.s["accessToken"] = self.token
            return self.token

    def activities(self, page_size=200, req_timeout=6, max_retries=0) -> ActivityList:
        self.activities_call_count += 1
        items = [
            {
                "id": "1001",
                "name": "篮球基础课",
                "startTime": int(time.time() * 1000) + 3600000,
                "endTime": int(time.time() * 1000) + 7200000,
                "isRegister": False
            }
        ]
        return ActivityList(
            items=items,
            complete=True,
            fetched_at=time.time(),
            session_scope_id=self._session_id,
            user_id=self._user_scope_id
        )

    def signUp(self, *args, **kwargs):
        self.signup_call_count += 1
        return {"code": 0, "msg": "OK"}


class TestPreflightSystem(unittest.TestCase):
    """Preflight 单元与集成测试用例 (P01 ~ P28)"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="preflight_test_")
        self.db_path = os.path.join(self.test_dir, "test_records.db")
        self.lock_path = os.path.join(self.test_dir, "grab.lock")
        self.logs_dir = os.path.join(self.test_dir, "logs")
        os.makedirs(self.logs_dir, exist_ok=True)

        self.state_store = StateStore(self.db_path)
        self.audit_logger = AuditLogger(self.logs_dir, run_id="run_preflight_unit_test")
        self.client = MockClient()
        self.activities_breaker = CircuitBreaker(failure_threshold=3, base_cooldown=2.0)
        self.conflict_breaker = ConflictCircuitBreaker(failure_threshold=3, base_cooldown=2.0)
        self.local_checker = LocalConflictChecker()

        self.cfg = {
            "max_normal": 2,
            "max_daily": 1,
            "special": ["游泳专项", "马拉松"]
        }
        self.tt = {
            "week1_monday": "2026-09-01",
            "total_weeks": 16,
            "busy_slots": []
        }

        # 构造有效快照
        self.valid_snapshot = ActivityList(
            items=[
                {
                    "id": "1001",
                    "name": "排球",
                    "startTime": int(time.time() * 1000) + 100000,
                    "endTime": int(time.time() * 1000) + 200000,
                    "isRegister": False
                }
            ],
            complete=True,
            fetched_at=time.time(),
            session_scope_id=self.client.get_session_scope_id(),
            user_id=self.client.get_user_scope_id(),
            run_id=self.audit_logger.run_id
        )

    def tearDown(self):
        try:
            shutil.rmtree(self.test_dir)
        except Exception:
            pass

    def _create_checker(self, **kwargs) -> PreflightChecker:
        params = {
            "client": self.client,
            "state_store": self.state_store,
            "audit_logger": self.audit_logger,
            "cfg": self.cfg,
            "tt": self.tt,
            "activities_breaker": self.activities_breaker,
            "conflict_breaker": self.conflict_breaker,
            "local_checker": self.local_checker,
            "lock_path": self.lock_path,
            "initial_snapshot": self.valid_snapshot,
            "run_id": self.audit_logger.run_id,
            "server_time_ref": time.time()
        }
        params.update(kwargs)
        return PreflightChecker(**params)

    # -------------------------------------------------------------------------
    # P01: 正常环境输出 READY
    # -------------------------------------------------------------------------
    def test_P01_normal_environment_ready(self):
        checker = self._create_checker()
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.READY)
        self.assertEqual(report.mode, PreflightMode.SERVER_MODE)
        self.assertEqual(len(report.failure_reasons), 0)

    # -------------------------------------------------------------------------
    # P02: checkConflict OPEN，但本地数据有效，输出 DEGRADED_READY
    # -------------------------------------------------------------------------
    def test_P02_check_conflict_open_local_valid_degraded_ready(self):
        # 模拟 checkConflict 连续故障导致 OPEN
        self.conflict_breaker.record_failure(http_status=500)
        self.conflict_breaker.record_failure(http_status=502)
        self.conflict_breaker.record_failure(http_status=503)
        self.assertEqual(self.conflict_breaker.state, ConflictState.OPEN)

        checker = self._create_checker()
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.DEGRADED_READY)
        self.assertEqual(report.mode, PreflightMode.LOCAL_FALLBACK)
        self.assertEqual(len(report.failure_reasons), 0)
        self.assertTrue(any("checkConflict" in w for w in report.warnings))

    # -------------------------------------------------------------------------
    # P03: Token 无效输出 BLOCKED
    # -------------------------------------------------------------------------
    def test_P03_token_invalid_blocked(self):
        self.client.s["accessToken"] = ""
        self.client.s["refreshToken"] = ""
        checker = self._create_checker()
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("认证" in r or "accessToken" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # P04: Token 刷新失败输出 BLOCKED
    # -------------------------------------------------------------------------
    def test_P04_token_refresh_failed_blocked(self):
        self.client._expires_in_sec = -10
        self.client.refresh = MagicMock(side_effect=RuntimeError("502 Bad Gateway 无法连接认证中心"))
        checker = self._create_checker()
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("自动刷新失败" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # P05: 刷新 Token 后旧快照失效
    # -------------------------------------------------------------------------
    def test_P05_token_refreshed_invalidates_old_snapshot(self):
        # 初始快照绑定旧会话
        old_session = self.client.get_session_scope_id()
        old_snapshot = copy.deepcopy(self.valid_snapshot)
        old_snapshot.session_scope_id = old_session

        # Token 发生刷新，生成新 session_scope_id
        self.client.refresh()
        new_session = self.client.get_session_scope_id()
        self.assertNotEqual(old_session, new_session)

        # 检查时若仍传入绑定旧 session 的快照且 client 无法拉取刷新
        self.client.activities = MagicMock(side_effect=RuntimeError("刷新失败"))
        checker = self._create_checker(initial_snapshot=old_snapshot)
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("会话已失效" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # P06: activities 接口失败且无有效快照，BLOCKED
    # -------------------------------------------------------------------------
    def test_P06_activities_failed_no_valid_snapshot_blocked(self):
        self.client.activities = MagicMock(side_effect=RuntimeError("500 Server Error"))
        checker = self._create_checker(initial_snapshot=None)
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("活动列表" in r or "快照" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # P07: 报名列表分页不完整，BLOCKED
    # -------------------------------------------------------------------------
    def test_P07_snapshot_pagination_incomplete_blocked(self):
        bad_snapshot = copy.deepcopy(self.valid_snapshot)
        bad_snapshot.complete = False
        checker = self._create_checker(initial_snapshot=bad_snapshot)
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("complete=False" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # P08: 快照超过有效期且刷新失败，BLOCKED
    # -------------------------------------------------------------------------
    def test_P08_snapshot_expired_and_refresh_failed_blocked(self):
        stale_snapshot = copy.deepcopy(self.valid_snapshot)
        stale_snapshot.fetched_at = time.time() - 120.0  # 超过 60 秒上限
        # 模拟刷新失败
        self.client.activities = MagicMock(side_effect=RuntimeError("Network Timeout"))

        checker = self._create_checker(initial_snapshot=stale_snapshot)
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("过期" in r and "刷新失败" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # P09: 存在 UNKNOWN，BLOCKED
    # -------------------------------------------------------------------------
    def test_P09_state_store_has_unknown_blocked(self):
        self.state_store.record_unknown("act_unk_1", "未知课程", error="网关超时 504")
        checker = self._create_checker()
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("UNKNOWN" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # P10: 存在 SUBMITTING，BLOCKED
    # -------------------------------------------------------------------------
    def test_P10_state_store_has_submitting_blocked(self):
        self.state_store.record_submitting("act_sub_1", "提交中课程")
        checker = self._create_checker()
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("SUBMITTING" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # P11: 存在 VERIFYING，BLOCKED
    # -------------------------------------------------------------------------
    def test_P11_state_store_has_verifying_blocked(self):
        self.state_store.record_submitting("act_ver_1", "复核中课程")
        self.state_store.record_verifying("act_ver_1")
        checker = self._create_checker()
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("VERIFYING" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # P12: 仅存在 RETRYABLE，重新校验后允许继续
    # -------------------------------------------------------------------------
    def test_P12_state_store_only_retryable_allowed_to_proceed(self):
        self.state_store.record_submitting("act_ret_1", "可重试课程")
        self.state_store.record_retryable("act_ret_1", reason="确认未报名")
        self.valid_snapshot.fetched_at = time.time() + 0.1
        checker = self._create_checker()
        report = checker.run_all_checks()
        # 仅存在 RETRYABLE 时允许启动，不会判定为 BLOCKED
        self.assertNotEqual(report.status, PreflightStatus.BLOCKED)
        self.assertEqual(report.status, PreflightStatus.READY)

    # -------------------------------------------------------------------------
    # P13: StateStore 不可读，BLOCKED
    # -------------------------------------------------------------------------
    def test_P13_state_store_unreadable_blocked(self):
        self.state_store._get_conn = MagicMock(side_effect=sqlite3.DatabaseError("database disk image is malformed"))
        checker = self._create_checker()
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("StateStore" in r and "不可读" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # P14: StateStore 不可写，BLOCKED
    # -------------------------------------------------------------------------
    def test_P14_state_store_unwritable_blocked(self):
        # 模拟写事务报错 (只读数据库)
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("attempt to write a readonly database")
        self.state_store._get_conn = MagicMock(return_value=mock_conn)

        checker = self._create_checker()
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("StateStore" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # P15: InstanceLock 不可用，BLOCKED
    # -------------------------------------------------------------------------
    def test_P15_instance_lock_unavailable_blocked(self):
        # 模拟锁路径目录不可写 / 权限拒绝
        bad_lock = MagicMock()
        bad_lock.acquire.side_effect = PermissionError("Permission denied")
        bad_lock.is_locked = False
        checker = self._create_checker(instance_lock=bad_lock)
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("锁文件" in r or "权限" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # P16: 存在其他抢课进程时明确报告
    # -------------------------------------------------------------------------
    def test_P16_other_process_running_reported(self):
        # 外部进程已持有锁
        other_lock = InstanceLock(self.lock_path)
        self.assertTrue(other_lock.acquire())
        try:
            checker = self._create_checker()
            report = checker.run_all_checks()
            self.assertEqual(report.status, PreflightStatus.BLOCKED)
            self.assertTrue(any("其他抢课进程正在运行" in r for r in report.failure_reasons))
        finally:
            other_lock.release()

    # -------------------------------------------------------------------------
    # P17: 配额数据不完整，BLOCKED
    # -------------------------------------------------------------------------
    def test_P17_quota_data_incomplete_blocked(self):
        # 缺少 max_normal
        bad_cfg = copy.deepcopy(self.cfg)
        bad_cfg["max_normal"] = -1
        checker = self._create_checker(cfg=bad_cfg)
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("max_normal" in r for r in report.failure_reasons))

        # 缺少 timetable 教学周锚点
        bad_tt = {}
        checker2 = self._create_checker(tt=bad_tt)
        report2 = checker2.run_all_checks()
        self.assertEqual(report2.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("week1_monday" in r for r in report2.failure_reasons))

    # -------------------------------------------------------------------------
    # P18: 特殊活动规则无法识别，BLOCKED
    # -------------------------------------------------------------------------
    def test_P18_special_activity_rule_invalid_blocked(self):
        bad_cfg = copy.deepcopy(self.cfg)
        bad_cfg["special"] = 123456  # 非合法列表类型
        checker = self._create_checker(cfg=bad_cfg)
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("特殊活动" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # P19: 审计日志不可写，BLOCKED
    # -------------------------------------------------------------------------
    def test_P19_audit_log_unwritable_blocked(self):
        # 模拟审计日志目录写权限失败
        bad_logger = AuditLogger(os.path.join(self.test_dir, "non_writable_logs"))
        # 模拟写探测文件抛出 PermissionError
        with patch("builtins.open", side_effect=PermissionError("Read-only file system")):
            checker = self._create_checker(audit_logger=bad_logger)
            report = checker.run_all_checks()
            self.assertEqual(report.status, PreflightStatus.BLOCKED)
            self.assertTrue(any("审计日志" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # P20: 审计日志测试不污染正式日志
    # -------------------------------------------------------------------------
    def test_P20_audit_log_probe_does_not_pollute_formal_log(self):
        checker = self._create_checker()
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.READY)

        # 检查日志目录，确保没有写入任何带有 probe 内容的垃圾正式事件
        date_str = datetime.date.today().strftime("%Y%m%d")
        event_file = os.path.join(self.logs_dir, f"events-{date_str}.jsonl")
        if os.path.exists(event_file):
            with open(event_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines:
                evt = json.loads(line)
                # 正式事件中不能出现探测专用的 probe 伪造事件名
                self.assertNotEqual(evt.get("event_name"), "probe")
                self.assertIn(evt.get("event_type"), [EventType.PREFLIGHT, EventType.INFRASTRUCTURE])

    # -------------------------------------------------------------------------
    # P21: 系统时间偏差超出阈值，按规则阻断
    # -------------------------------------------------------------------------
    def test_P21_system_time_drift_exceeds_threshold_blocked(self):
        # 模拟服务端时间与本地相差 15 秒（远超 5.0 秒阈值）
        bad_server_time = time.time() - 15.0
        checker = self._create_checker(server_time_ref=bad_server_time)
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("时间偏差" in r for r in report.failure_reasons))

    # -------------------------------------------------------------------------
    # P22: activities 和 checkConflict 的健康状态互不影响
    # -------------------------------------------------------------------------
    def test_P22_activities_and_conflict_breakers_independent(self):
        # 将 activities_breaker 熔断至 OPEN
        self.activities_breaker.record_failure(http_status=500)
        self.activities_breaker.record_failure(http_status=502)
        self.activities_breaker.record_failure(http_status=503)
        self.assertEqual(self.activities_breaker.state, CircuitState.OPEN)

        # 验证 conflict_breaker 依然保持 NORMAL
        self.assertEqual(self.conflict_breaker.state, ConflictState.NORMAL)

        # 反向验证：将 conflict_breaker 熔断至 OPEN，activities_breaker 保持独立
        cb2 = CircuitBreaker()
        conf2 = ConflictCircuitBreaker()
        conf2.record_failure(http_status=500)
        conf2.record_failure(http_status=500)
        conf2.record_failure(http_status=500)
        self.assertEqual(conf2.state, ConflictState.OPEN)
        self.assertEqual(cb2.state, CircuitState.CLOSED)

    # -------------------------------------------------------------------------
    # P23: HALF_OPEN 探测并发仍然只有一次
    # -------------------------------------------------------------------------
    def test_P23_half_open_probe_concurrency_only_one(self):
        # 熔断开启并度过冷却期转入 HALF_OPEN
        breaker = ConflictCircuitBreaker(failure_threshold=1, base_cooldown=0.01, jitter_max=0.0)
        breaker.record_failure(http_status=500)
        self.assertEqual(breaker.state, ConflictState.OPEN)
        time.sleep(0.02)

        allowed_count = 0
        def try_probe():
            nonlocal allowed_count
            allowed, _ = breaker.can_attempt()
            if allowed:
                allowed_count += 1

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(try_probe) for _ in range(8)]
            concurrent.futures.wait(futures)

        # 严格保证仅有 1 次探测许可被分配
        self.assertEqual(allowed_count, 1)

    # -------------------------------------------------------------------------
    # P24: Preflight 不调用 signUp
    # -------------------------------------------------------------------------
    def test_P24_preflight_does_not_call_signup(self):
        checker = self._create_checker()
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.READY)
        # 验证 signUp / submit_once 调用次数严格为 0
        self.assertEqual(self.client.signup_call_count, 0)

    # -------------------------------------------------------------------------
    # P25: Preflight 不改变真实报名状态
    # -------------------------------------------------------------------------
    def test_P25_preflight_does_not_alter_registration_records(self):
        # 先在 StateStore 插入初始记录
        self.state_store.record_success("act_orig_1", "原课1")
        self.state_store.record_failed("act_orig_2", "原课2", error="名额已满")
        self.valid_snapshot.fetched_at = time.time() + 0.1
        recs_before = self.state_store.get_all_records()

        checker = self._create_checker()
        report = checker.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.READY)

        recs_after = self.state_store.get_all_records()
        self.assertEqual(recs_before, recs_after)

    # -------------------------------------------------------------------------
    # P26: 重复执行 Preflight 不会产生数据库脏写
    # -------------------------------------------------------------------------
    def test_P26_repeated_preflight_no_dirty_writes(self):
        checker = self._create_checker()
        # 连续执行 5 次 Preflight
        for _ in range(5):
            report = checker.run_all_checks()
            self.assertEqual(report.status, PreflightStatus.READY)

        conn = self.state_store._get_conn()
        cur = conn.execute("SELECT COUNT(*) FROM registration_records;")
        cnt = cur.fetchone()[0]
        self.assertEqual(cnt, 0)

    # -------------------------------------------------------------------------
    # P27: Token 刷新并发受到保护
    # -------------------------------------------------------------------------
    def test_P27_token_refresh_concurrency_protected(self):
        self.client._expires_in_sec = -5  # 过期状态
        checker = self._create_checker()

        def do_auth_check():
            return checker.check_auth()

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(do_auth_check) for _ in range(10)]
            results = [f.result() for f in futures]

        # 所有线程都应该得到 OK
        for r in results:
            self.assertEqual(r.status, CheckItemStatus.OK)

        # 严格验证 refresh 仅被执行了 1 次（双重检查加锁生效）
        self.assertEqual(self.client.refresh_call_count, 1)

    # -------------------------------------------------------------------------
    # P28: 最终报告 run_id 与本次运行一致
    # -------------------------------------------------------------------------
    def test_P28_report_run_id_matches_current_run(self):
        expected_run_id = self.audit_logger.run_id
        checker = self._create_checker(run_id=expected_run_id)
        report = checker.run_all_checks()
        self.assertEqual(report.run_id, expected_run_id)

        # 检查报告 JSON 序列化结果
        json_str = report.to_json()
        report_dict = json.loads(json_str)
        self.assertEqual(report_dict["run_id"], expected_run_id)
        self.assertEqual(report_dict["status"], PreflightStatus.READY)
        self.assertEqual(report_dict["mode"], PreflightMode.SERVER_MODE)


if __name__ == "__main__":
    unittest.main()
