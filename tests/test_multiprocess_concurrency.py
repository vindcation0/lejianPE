"""
多进程并发与原子性测试套件 (M01～M07)
=====================================
使用 Python multiprocessing 真实独立进程，验证系统级单实例文件锁与 SQLite 状态原子性。
严禁使用线程模拟，确保在真实的独立操作系统进程间具备强互斥保护。
"""
import multiprocessing
import os
import shutil
import tempfile
import time
import unittest

from core.instance_lock import InstanceLock, InstanceLockError
from core.state_store import StateStore, RegistrationStatus, StateStoreError
from core.request_manager import RequestManager
from core.registration_manager import RegistrationManager
from core.recovery_manager import RecoveryManager
from core.audit_logger import AuditLogger


# =========================================================================
# 多进程顶层 Worker 函数（Windows spawn 模式兼容）
# =========================================================================

class _DummyMockClient:
    def __init__(self, activities_return=None):
        self.activities_return = activities_return if activities_return is not None else []
        self.call_history = []

    def call(self, method, path, body=None, timeout=6):
        self.call_history.append((method, path, body))
        return 200, {"code": 0, "data": {"success": True}}

    def activities(self, page_size=200, req_timeout=6):
        return self.activities_return


def _worker_acquire_lock(lock_path, hold_seconds, out_q):
    """尝试获取实例锁并在持有指定时间后释放退出"""
    lock = InstanceLock(lock_path)
    acquired = lock.acquire()
    out_q.put((acquired, os.getpid()))
    if acquired:
        time.sleep(hold_seconds)
        lock.release()


def _worker_lock_and_abrupt_exit(lock_path, ready_q):
    """获取锁后立即异常终止进程（模拟突然崩溃）"""
    lock = InstanceLock(lock_path)
    if lock.acquire():
        ready_q.put(True)
        # 给 multiprocessing.Queue 内部 feeder 线程足够时间完成 pipe flush
        time.sleep(0.1)
        # 异常退出，直接由 OS 终止进程，不走 Python 用户态清理
        os._exit(42)
    else:
        ready_q.put(False)
        time.sleep(0.1)
        os._exit(1)


def _worker_reserve_submitting(db_path, aid, name, delay_start, out_q):
    """尝试原子预留 SUBMITTING 记录"""
    if delay_start > 0:
        time.sleep(delay_start)
    store = StateStore(db_path)
    try:
        ok, rec, msg = store.reserve_submitting(aid, name)
        out_q.put((ok, rec.get("status") if rec else None, msg))
    except Exception as e:
        out_q.put((False, None, str(e)))
    finally:
        store.close()


def _worker_attempt_candidate_submission(db_path, candidate_item, out_q):
    """在独立进程中尝试提交新候选"""
    store = StateStore(db_path)
    audit = AuditLogger(os.path.join(os.path.dirname(db_path), "logs"))
    mock_client = _DummyMockClient()
    req_mgr = RequestManager(mock_client)
    reg_mgr = RegistrationManager(store, req_mgr, audit)

    try:
        ok, status, msg, _ = reg_mgr.register_activity(candidate_item)
        out_q.put((ok, status, msg, len(mock_client.call_history)))
    finally:
        store.close()


def _worker_startup_recovery(db_path, activities_return, out_q):
    """在独立进程中执行启动对账自愈"""
    store = StateStore(db_path)
    audit = AuditLogger(os.path.join(os.path.dirname(db_path), "logs"))
    mock_client = _DummyMockClient(activities_return=activities_return)
    req_mgr = RequestManager(mock_client)
    rec_mgr = RecoveryManager(store, req_mgr, audit)

    try:
        summary = rec_mgr.recover_on_startup()
        out_q.put((summary, len(mock_client.call_history)))
    finally:
        store.close()


def _worker_quota_candidate_submission(db_path, candidate_item, max_normal, out_q):
    """在独立进程中带配额校验提交候选"""
    store = StateStore(db_path)
    audit = AuditLogger(os.path.join(os.path.dirname(db_path), "logs"))
    mock_client = _DummyMockClient()
    req_mgr = RequestManager(mock_client)
    reg_mgr = RegistrationManager(store, req_mgr, audit)

    def validate_rules(it):
        # 检查数据库中已成功的记录数
        succ_recs = [r for r in store.get_all_records() if r.get("status") == RegistrationStatus.SUCCESS]
        if len(succ_recs) >= max_normal:
            return False, f"当周配额已满 ({len(succ_recs)}/{max_normal})"
        return True, "OK"

    try:
        ok, status, msg, _ = reg_mgr.register_activity(candidate_item, validate_rules_cb=validate_rules)
        out_q.put((ok, status, msg, len(mock_client.call_history)))
    finally:
        store.close()


def _worker_cmd_grab_entry(lock_path, state_file, config_file, out_q):
    """在独立进程中调用 cmd_grab 入口，验证锁被占用时 0 HTTP 调用"""
    import lejian
    from unittest.mock import patch

    lejian.LOCK_PATH = lock_path
    lejian.STATE = state_file
    lejian.CONFIG = config_file

    http_calls = []

    def mock_call(*args, **kwargs):
        http_calls.append(args)
        return 200, {"code": 0, "data": {}}

    def mock_activities(*args, **kwargs):
        http_calls.append(("activities", args))
        return []

    with patch.object(lejian.Client, "call", mock_call), \
         patch.object(lejian.Client, "activities", mock_activities), \
         patch.object(lejian.Client, "ensure_token", return_value=True):
        try:
            lejian.cmd_grab()
        except SystemExit:
            pass

    out_q.put(len(http_calls))


# =========================================================================
# 多进程并发测试类
# =========================================================================

class MultiprocessConcurrencyTestSuite(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.lock_path = os.path.join(self.tmp_dir, "lejian.lock")
        self.db_path = os.path.join(self.tmp_dir, "test_concurrent.db")
        self.logs_dir = os.path.join(self.tmp_dir, "logs")
        os.makedirs(self.logs_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_M01_two_processes_start_simultaneously(self):
        """M01: 两个独立 Python 进程同时启动抢锁，有且仅有一个能获得执行权"""
        q = multiprocessing.Queue()

        p1 = multiprocessing.Process(target=_worker_acquire_lock, args=(self.lock_path, 0.4, q))
        p2 = multiprocessing.Process(target=_worker_acquire_lock, args=(self.lock_path, 0.4, q))

        p1.start()
        p2.start()

        res1 = q.get(timeout=5)
        res2 = q.get(timeout=5)

        p1.join(timeout=3)
        p2.join(timeout=3)

        acquired_results = [res1[0], res2[0]]
        # 核心断言：一个获取成功，另一个被拒绝
        self.assertEqual(acquired_results.count(True), 1)
        self.assertEqual(acquired_results.count(False), 1)

    def test_M02_process_b_starts_while_process_a_holds_lock(self):
        """M02: 进程 A 持有锁期间，进程 B 启动被明确拒绝，进程 A 不受干扰"""
        lock_a = InstanceLock(self.lock_path)
        self.assertTrue(lock_a.acquire())

        q = multiprocessing.Queue()
        p_b = multiprocessing.Process(target=_worker_acquire_lock, args=(self.lock_path, 0.1, q))
        p_b.start()

        acquired_b, pid_b = q.get(timeout=5)
        p_b.join(timeout=3)

        # 核心断言：进程 B 明确被拒绝
        self.assertFalse(acquired_b)
        self.assertNotEqual(pid_b, os.getpid())

        # 进程 A 锁依然有效
        self.assertTrue(lock_a.is_locked)
        lock_a.release()
        self.assertFalse(lock_a.is_locked)

    def test_M03_process_a_abrupt_exit_releases_os_lock(self):
        """M03: 进程 A 异常崩溃退出后，操作系统级文件锁自动释放，进程 B 能够成功获取锁"""
        ready_q = multiprocessing.Queue()
        p_a = multiprocessing.Process(target=_worker_lock_and_abrupt_exit, args=(self.lock_path, ready_q))
        p_a.start()

        a_got_lock = ready_q.get(timeout=5)
        self.assertTrue(a_got_lock)
        p_a.join(timeout=3)
        # 确认进程 A 已经非正常死亡 (exitcode 42)
        self.assertEqual(p_a.exitcode, 42)

        # 进程 B 启动尝试获取锁
        out_q = multiprocessing.Queue()
        p_b = multiprocessing.Process(target=_worker_acquire_lock, args=(self.lock_path, 0.1, out_q))
        p_b.start()

        acquired_b, _ = out_q.get(timeout=5)
        p_b.join(timeout=3)

        # 核心断言：锁已被操作系统自动回收，进程 B 成功获取
        self.assertTrue(acquired_b)

    def test_M04_concurrent_submitting_reservations_are_strictly_atomic(self):
        """M04: 两个进程同时尝试预留不同活动的 SUBMITTING，SQLite 原子事务杜绝并发写入"""
        # 初始化数据库表结构
        init_store = StateStore(self.db_path)
        init_store.close()

        q = multiprocessing.Queue()
        # 进程 A 尝试预留 1001，进程 B 尝试预留 1002
        p_a = multiprocessing.Process(target=_worker_reserve_submitting, args=(self.db_path, "1001", "活动 A", 0.0, q))
        p_b = multiprocessing.Process(target=_worker_reserve_submitting, args=(self.db_path, "1002", "活动 B", 0.0, q))

        p_a.start()
        p_b.start()

        res_a = q.get(timeout=5)
        res_b = q.get(timeout=5)

        p_a.join(timeout=3)
        p_b.join(timeout=3)

        results = [res_a[0], res_b[0]]
        # 核心断言：在同一瞬间，有且仅有一个活动能被成功预留为 SUBMITTING
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)

        # 检查数据库中处于 SUBMITTING 的记录数 strictly 为 1
        check_store = StateStore(self.db_path)
        unres = check_store.get_unresolved_records()
        self.assertEqual(len(unres), 1)
        check_store.close()

    def test_M05_process_a_unknown_blocks_process_b(self):
        """M05: 进程 A 遗留 UNKNOWN 状态时，进程 B 启动被安全屏障阻断，无法提交任何新活动"""
        init_store = StateStore(self.db_path)
        init_store.record_unknown("1001", "未决活动 A", "超时未明")
        init_store.close()

        q = multiprocessing.Queue()
        candidate_b = {"id": "1002", "name": "新候选 B", "startTime": 1730000000000}
        p_b = multiprocessing.Process(target=_worker_attempt_candidate_submission, args=(self.db_path, candidate_b, q))
        p_b.start()

        ok, status, msg, calls = q.get(timeout=5)
        p_b.join(timeout=3)

        # 核心断言：进程 B 彻底被阻断，返回 UNKNOWN 且未发出任何底层 signUp HTTP 请求
        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.UNKNOWN)
        self.assertIn("安全屏障已冻结队列", msg)
        self.assertEqual(calls, 0)

    def test_M06_process_a_crashes_during_submitting_process_b_recovers_without_resubmit(self):
        """M06: 进程 A 在 SUBMITTING 阶段崩溃，进程 B 启动自动只读复核确认，绝不重复发送 signUp"""
        init_store = StateStore(self.db_path)
        init_store.record_submitting("1001", "崩溃活动 A")
        init_store.close()

        q = multiprocessing.Queue()
        # 模拟复核时服务端确认已有报名
        mock_activities = [{"id": "1001", "name": "崩溃活动 A", "isRegister": True, "startTime": 1730000000000}]
        p_b = multiprocessing.Process(target=_worker_startup_recovery, args=(self.db_path, mock_activities, q))
        p_b.start()

        summary, calls = q.get(timeout=5)
        p_b.join(timeout=3)

        # 核心断言：恢复成功，且 signUp 零调用
        self.assertEqual(len(summary["recovered_success"]), 1)
        self.assertEqual(calls, 0)

        # 数据库中状态已平滑转换为 SUCCESS
        check_store = StateStore(self.db_path)
        rec = check_store.get_record("1001")
        self.assertEqual(rec["status"], RegistrationStatus.SUCCESS)
        check_store.close()

    def test_M07_concurrent_attempts_after_weekly_limit_never_exceeds_quota(self):
        """M07: 达到每周上限后的并发尝试，多进程不能绕过每周配额，服务端报名数绝不超额"""
        init_store = StateStore(self.db_path)
        # 预置 1 节已成功报名的活动
        init_store.record_success("1001", "已报活动 1", http_status=200)
        init_store.close()

        q = multiprocessing.Queue()
        # 上限为 1，此时已满。进程 A 和进程 B 分别尝试提交活动 2 和活动 3
        cand_2 = {"id": "1002", "name": "并发候选 2", "startTime": 1730000000000}
        cand_3 = {"id": "1003", "name": "并发候选 3", "startTime": 1730000000000}

        p1 = multiprocessing.Process(target=_worker_quota_candidate_submission, args=(self.db_path, cand_2, 1, q))
        p2 = multiprocessing.Process(target=_worker_quota_candidate_submission, args=(self.db_path, cand_3, 1, q))

        p1.start()
        p2.start()

        res1 = q.get(timeout=5)
        res2 = q.get(timeout=5)

        p1.join(timeout=3)
        p2.join(timeout=3)

        # 核心断言：两个进程全部被配额规则阻断，未发出任何 signUp 请求
        self.assertFalse(res1[0])
        self.assertFalse(res2[0])
        self.assertEqual(res1[3], 0)
        self.assertEqual(res2[3], 0)

        # 最终数据库内 SUCCESS 数量依然严格为 1
        check_store = StateStore(self.db_path)
        successes = [r for r in check_store.get_all_records() if r["status"] == RegistrationStatus.SUCCESS]
        self.assertEqual(len(successes), 1)
        check_store.close()

    # =========================================================================
    # Phase 2 封版专项 (M08～M13)：相同 activityId 幂等性、状态原子性与入口锁
    # =========================================================================

    def test_M08_same_activity_duplicate_reservation_rejected(self):
        """M08: 相同 activityId 连续两次 reserve_submitting，第二次必须在原子事务内被拒绝"""
        store = StateStore(self.db_path)
        ok1, rec1, msg1 = store.reserve_submitting("1001", "活动 A")
        self.assertTrue(ok1)
        self.assertIsNotNone(rec1)
        self.assertEqual(rec1["status"], RegistrationStatus.SUBMITTING)

        # 第二次预留相同活动
        ok2, rec2, msg2 = store.reserve_submitting("1001", "活动 A")
        self.assertFalse(ok2)
        self.assertIn("当前活动已处于 SUBMITTING 状态，禁止重复预留", msg2)
        self.assertEqual(rec2["status"], RegistrationStatus.SUBMITTING)
        store.close()

    def test_M09_unknown_activity_reservation_rejected(self):
        """M09: 当前 activityId 为 UNKNOWN，再次尝试直接预留必须拒绝，必须先复核"""
        store = StateStore(self.db_path)
        store.record_unknown("1001", "活动 A", "超时未明")
        rec = store.get_record("1001")
        self.assertEqual(rec["status"], RegistrationStatus.UNKNOWN)

        # 尝试直接预留处于 UNKNOWN 的活动
        ok, res_rec, msg = store.reserve_submitting("1001", "活动 A")
        self.assertFalse(ok)
        self.assertIn("当前活动已处于 UNKNOWN 状态，必须先复核，禁止直接预留", msg)
        self.assertEqual(res_rec["status"], RegistrationStatus.UNKNOWN)
        store.close()

    def test_M10_retryable_activity_allowed_after_rule_revalidation(self):
        """M10: 当前 activityId 为 RETRYABLE，经过重新规则校验后允许预留并进入 SUBMITTING"""
        store = StateStore(self.db_path)
        store.record_unknown("1001", "活动 A", "超时未明")
        store.record_retryable("1001", "复核确认未报名")

        # 1. StateStore 原子事务内验证：RETRYABLE 状态允许原子预留为 SUBMITTING
        ok, rec, msg = store.reserve_submitting("1001", "活动 A")
        self.assertTrue(ok)
        self.assertEqual(rec["status"], RegistrationStatus.SUBMITTING)
        self.assertEqual(msg, "OK")

        # 2. 结合 RegistrationManager 验证规则二次校验全流程
        # 重置为 RETRYABLE
        store.record_retryable("1001", "复核确认未报名")
        audit = AuditLogger(self.logs_dir)
        mock_client = _DummyMockClient()
        req_mgr = RequestManager(mock_client)
        reg_mgr = RegistrationManager(store, req_mgr, audit)

        # 规则校验不通过 -> 阻断且 0 网络请求
        candidate = {"id": "1001", "name": "活动 A", "startTime": 1730000000000}
        ok_fail, st_fail, msg_fail, _ = reg_mgr.register_activity(
            candidate, validate_rules_cb=lambda it: (False, "名额已被抢光")
        )
        self.assertFalse(ok_fail)
        self.assertEqual(st_fail, RegistrationStatus.FAILED)
        self.assertIn("名额已被抢光", msg_fail)
        self.assertEqual(len(mock_client.call_history), 0)

        # 规则校验通过 -> 允许进入 SUBMITTING 并完成提交
        store.record_retryable("1001", "复核确认未报名")
        ok_succ, st_succ, msg_succ, _ = reg_mgr.register_activity(
            candidate, validate_rules_cb=lambda it: (True, "OK")
        )
        self.assertTrue(ok_succ)
        self.assertEqual(st_succ, RegistrationStatus.SUCCESS)
        self.assertEqual(store.get_record("1001")["status"], RegistrationStatus.SUCCESS)
        store.close()

    def test_M11_success_activity_reservation_rejected(self):
        """M11: 当前 activityId 为 SUCCESS，必须拒绝再次预留"""
        store = StateStore(self.db_path)
        store.record_success("1001", "活动 A", http_status=200)
        rec = store.get_record("1001")
        self.assertEqual(rec["status"], RegistrationStatus.SUCCESS)

        # 尝试再次预留已成功的活动
        ok, res_rec, msg = store.reserve_submitting("1001", "活动 A")
        self.assertFalse(ok)
        self.assertIn("当前活动已报名成功 (SUCCESS)，禁止重复预留", msg)
        self.assertEqual(res_rec["status"], RegistrationStatus.SUCCESS)
        store.close()

    def test_M12_sqlite_transaction_boundaries_and_rollback_atomicity(self):
        """M12: SQLite 事务边界检查：若预留事务中途发生错误，完整回滚且无中间提交，状态完全保全"""
        store = StateStore(self.db_path)
        store.record_unknown("1001", "活动 A", "超时未明")
        store.record_retryable("1001", "复核确认未报名")
        rec_before = store.get_record("1001")
        self.assertEqual(rec_before["status"], RegistrationStatus.RETRYABLE)
        attempts_before = rec_before.get("attempts", 1)

        # 模拟在 reserve_submitting 的原子事务内模拟写入异常
        store.record_submitting = lambda *args, **kwargs: (_ for _ in ()).throw(StateStoreError("模拟写盘中断"))

        with self.assertRaises(StateStoreError):
            store.reserve_submitting("1001", "活动 A")

        store.close()

        # 使用全新的独立连接重新读取数据库，检验是否发生了任何局部提交
        fresh_store = StateStore(self.db_path)
        rec_after = fresh_store.get_record("1001")
        # 核心断言：完全回滚，状态依然严格保持为 RETRYABLE，attempts 没有增加
        self.assertEqual(rec_after["status"], RegistrationStatus.RETRYABLE)
        self.assertEqual(rec_after.get("attempts", 1), attempts_before)
        fresh_store.close()

    def test_M13_entry_level_cmd_grab_second_process_zero_http_calls(self):
        """M13: 入口级单实例锁验证：主进程持有锁时，第二个独立进程执行 cmd_grab() 被立即拦截，HTTP 调用严格为零"""
        state_file = os.path.join(self.tmp_dir, "state.json")
        config_file = os.path.join(self.tmp_dir, "config.json")
        import json
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({"token": "fake_token_for_test", "schoolId": 1}, f)
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump({}, f)

        # 主进程获取锁
        lock = InstanceLock(self.lock_path)
        self.assertTrue(lock.acquire())

        # 第二个独立进程调用 cmd_grab 入口
        q = multiprocessing.Queue()
        p = multiprocessing.Process(
            target=_worker_cmd_grab_entry,
            args=(self.lock_path, state_file, config_file, q)
        )
        p.start()

        http_calls = q.get(timeout=5)
        p.join(timeout=3)

        # 核心断言：第二进程被 InstanceLock 拦截退出，HTTP 请求次数绝对为 0
        self.assertEqual(http_calls, 0)
        self.assertEqual(p.exitcode, 0)

        # 主进程释放锁
        lock.release()


if __name__ == "__main__":
    unittest.main()
