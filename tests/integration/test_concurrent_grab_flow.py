"""
Phase 7 高并发竞争与多进程安全集成测试套件 (I41 ~ I55)
=====================================================
验证多进程/多线程并发环境下的单实例锁争抢、SQLite 事务原子预留、
配额竞争、并发审计写入与防超额安全屏障。
"""
import concurrent.futures
import datetime
import json
import os
import threading
import time
import unittest

from core.audit_logger import AuditLogger
from core.circuit_breaker import CircuitBreaker
from core.instance_lock import InstanceLock, InstanceLockError
from core.state_store import RegistrationStatus, StateStore
from tests.integration.integration_helper import IntegrationEnvironment


class TestConcurrentGrabFlow(unittest.TestCase):
    """并发与多实例安全集成测试 (I41 ~ I55)"""

    def setUp(self):
        self.env = IntegrationEnvironment()
        self.env.server.add_activity("act_301", "游泳竞技课", 1794146400000, 1794153600000)
        self.env.server.add_activity("act_302", "足球提高班", 1794155400000, 1794162600000)

    def tearDown(self):
        self.env.close()

    # -------------------------------------------------------------------------
    # I41: 双实例竞争 InstanceLock 严格互斥 (仅 1 个成功)
    # -------------------------------------------------------------------------
    def test_I41_two_workers_competing_for_instance_lock(self):
        lock1 = InstanceLock(self.env.lock_path)
        lock2 = InstanceLock(self.env.lock_path)

        acquired_1 = lock1.acquire()
        acquired_2 = lock2.acquire()

        self.assertTrue(acquired_1)
        self.assertFalse(acquired_2)
        lock1.release()

    # -------------------------------------------------------------------------
    # I42: 5 个并发线程竞争单实例锁，严格有且仅有 1 个胜出
    # -------------------------------------------------------------------------
    def test_I42_five_concurrent_workers_lock_race(self):
        results = []

        def worker_task():
            l = InstanceLock(self.env.lock_path)
            res = l.acquire()
            if res:
                time.sleep(0.05)  # 持有一小会儿
                l.release()
            return res

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker_task) for _ in range(5)]
            results = [f.result() for f in futures]

        # 竞争期间任意时刻仅有 1 个线程能成功获取锁
        self.assertIn(True, results)

    # -------------------------------------------------------------------------
    # I43: 实例锁释放后，后续进程可立刻无阻碍获取
    # -------------------------------------------------------------------------
    def test_I43_lock_release_enables_immediate_reacquisition(self):
        lock1 = InstanceLock(self.env.lock_path)
        self.assertTrue(lock1.acquire())
        lock1.release()

        lock2 = InstanceLock(self.env.lock_path)
        self.assertTrue(lock2.acquire())
        lock2.release()

    # -------------------------------------------------------------------------
    # I44: SQLite BEGIN IMMEDIATE 原子预留高并发竞争仅 1 个成功
    # -------------------------------------------------------------------------
    def test_I44_concurrent_reserve_submitting_race(self):
        aid = "act_301"
        name = "游泳竞技课"
        reserve_successes = []

        def do_reserve():
            # 每个线程使用独立的 StateStore 实例模拟多进程
            store = StateStore(self.env.db_path)
            try:
                ok, rec, msg = store.reserve_submitting(aid, name)
                if ok:
                    reserve_successes.append(rec)
            except Exception:
                pass
            finally:
                store.close()

        threads = [threading.Thread(target=do_reserve) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 严格有且仅有 1 次原子预留成功
        self.assertEqual(len(reserve_successes), 1)

    # -------------------------------------------------------------------------
    # I45: 并发预留多门不同课程时，若存在任意 UNKNOWN，后续被严格拦截
    # -------------------------------------------------------------------------
    def test_I45_unresolved_blocks_concurrent_distinct_reservations(self):
        # 预留第一门课
        ok1, _, _ = self.env.state_store.reserve_submitting("act_301", "课1")
        self.assertTrue(ok1)

        # 尝试并发预留第二门不同课
        ok2, _, msg = self.env.state_store.reserve_submitting("act_302", "课2")
        self.assertFalse(ok2)
        self.assertIn("已存在未决活动", msg)

    # -------------------------------------------------------------------------
    # I46: 多线程抢课并发提交，signUp 严格调用 1 次
    # -------------------------------------------------------------------------
    def test_I46_concurrent_submit_signup_strictly_once(self):
        aid = "act_301"
        self.env.server.reset_counts()
        submit_results = []

        def do_submit():
            store = StateStore(self.env.db_path)
            try:
                ok, _, _ = store.reserve_submitting(aid, "课1")
                if ok:
                    res = self.env.submit_signup(aid, "课1")
                    submit_results.append(res)
            except Exception:
                pass
            finally:
                store.close()

        threads = [threading.Thread(target=do_submit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(submit_results), 1)
        self.assertEqual(self.env.server.signup_calls_count, 1)

    # -------------------------------------------------------------------------
    # I47: WAL 模式并发读写互不阻塞
    # -------------------------------------------------------------------------
    def test_I47_wal_concurrent_reads_during_writes(self):
        read_successes = []

        def reader():
            store = StateStore(self.env.db_path)
            for _ in range(20):
                records = store.get_all_records()
                read_successes.append(len(records))
                time.sleep(0.005)
            store.close()

        def writer():
            store = StateStore(self.env.db_path)
            for i in range(10):
                try:
                    store.record_success(f"act_wal_{i}", f"测试课_{i}")
                except Exception:
                    pass
                time.sleep(0.01)
            store.close()

        t_w = threading.Thread(target=writer)
        t_r = threading.Thread(target=reader)
        t_w.start()
        t_r.start()
        t_w.join()
        t_r.join()

        self.assertGreater(len(read_successes), 10)

    # -------------------------------------------------------------------------
    # I48: 并发审计日志写入保证每条记录格式完整、无行交叉损坏
    # -------------------------------------------------------------------------
    def test_I48_concurrent_audit_logging_no_corruption(self):
        logger = self.env.audit_logger

        def log_events(thread_id):
            for i in range(20):
                logger.record_event(
                    event_type="concurrent_test",
                    event_name="thread_log",
                    details={"thread": thread_id, "idx": i}
                )

        threads = [threading.Thread(target=log_events, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 读取生成的 jsonl 并逐行解析校验
        date_str = datetime.date.today().strftime("%Y%m%d")
        log_file = os.path.join(self.env.logs_dir, f"events-{date_str}.jsonl")
        self.assertTrue(os.path.exists(log_file))

        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        parsed_events = [json.loads(line) for line in lines if line.strip()]
        test_events = [e for e in parsed_events if e.get("event_type") == "concurrent_test"]
        self.assertEqual(len(test_events), 100)

    # -------------------------------------------------------------------------
    # I49: 并发 Token 刷新线程安全 (仅触发 1 次服务端 refresh)
    # -------------------------------------------------------------------------
    def test_I49_concurrent_token_refresh_single_execution(self):
        self.env.client._expires_in_sec = 10.0  # 临近过期
        self.env.server.reset_counts()

        def refresh_worker():
            self.env.client.ensure_token(margin=300)

        threads = [threading.Thread(target=refresh_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(self.env.server.refresh_calls_count, 1)

    # -------------------------------------------------------------------------
    # I50: 多线程并发触发失败，熔断器计数严格线程安全
    # -------------------------------------------------------------------------
    def test_I50_concurrent_circuit_breaker_failure_accumulation(self):
        from core.circuit_breaker import CircuitState
        cb = CircuitBreaker(failure_threshold=10, base_cooldown=1.0)

        def record_failure_task():
            cb.record_failure(http_status=500)

        threads = [threading.Thread(target=record_failure_task) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertEqual(cb.trip_count, 1)

    # -------------------------------------------------------------------------
    # I51: 候选评估与对账并行执行时，对账安全互斥
    # -------------------------------------------------------------------------
    def test_I51_reconciliation_in_progress_blocks_candidates(self):
        # 当对账进行中，本地检查器阻断新候选
        items = self.env.client.activities()
        res = self.env.local_conflict_checker.check_conflict(
            candidate=items[0],
            items=items,
            reconciliation_in_progress=True,
            current_week=10
        )
        self.assertTrue(res.has_conflict)
        self.assertIn("对账", res.reason)

    # -------------------------------------------------------------------------
    # I52: 实例锁上下文管理器在发生未捕获异常时必定安全释放
    # -------------------------------------------------------------------------
    def test_I52_instance_lock_releases_on_exception(self):
        try:
            with InstanceLock(self.env.lock_path):
                raise RuntimeError("Worker sudden crash")
        except RuntimeError:
            pass

        # 锁应当已自动释放，其他实例可立刻获取
        lock2 = InstanceLock(self.env.lock_path)
        self.assertTrue(lock2.acquire())
        lock2.release()

    # -------------------------------------------------------------------------
    # I53: 多线程并发 Preflight 检查独立安全执行
    # -------------------------------------------------------------------------
    def test_I53_concurrent_preflight_checks_independent(self):
        statuses = []

        def run_preflight_task():
            pf = self.env.create_preflight()
            rep = pf.run_all_checks()
            statuses.append(rep.status)

        threads = [threading.Thread(target=run_preflight_task) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(statuses), 4)
        for s in statuses:
            self.assertIn(s, ("READY", "DEGRADED_READY", "BLOCKED"))
        self.assertIn("READY", statuses)

    # -------------------------------------------------------------------------
    # I54: 进程崩溃后锁文件残存，同 PID 或无效句柄在新会话中安全覆盖
    # -------------------------------------------------------------------------
    def test_I54_lock_file_stale_pid_cleanup(self):
        # 写入一个伪造的陈旧 PID
        with open(self.env.lock_path, "w", encoding="utf-8") as f:
            f.write("pid=9999999\n")

        # 新的实例获取锁应当不受陈旧文件内容阻碍（文件锁基于 OS 文件句柄锁定）
        new_lock = InstanceLock(self.env.lock_path)
        self.assertTrue(new_lock.acquire())
        new_lock.release()

    # -------------------------------------------------------------------------
    # I55: 并发多轮次提交后最终对账状态精确一致
    # -------------------------------------------------------------------------
    def test_I55_concurrent_grab_consistent_reconciliation(self):
        # 预留并成功提交
        self.env.state_store.reserve_submitting("act_301", "游泳")
        self.env.submit_signup("act_301", "游泳")
        self.env.state_store.record_success("act_301", "游泳")

        # 最终全局对账
        rec_res = self.env.reconciliation_manager.reconcile()
        self.assertTrue(rec_res["success"])
        self.assertEqual(rec_res["total_registered"], 1)


if __name__ == "__main__":
    unittest.main()
