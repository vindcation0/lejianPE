"""
服务端故障熔断器专项测试套件 (C01～C12)
=======================================
覆盖文档任务二要求的所有熔断机制测试。
"""
import time
import unittest
from unittest.mock import MagicMock

from core.circuit_breaker import CircuitBreaker, CircuitState, is_server_fault
from core.state_store import RegistrationStatus, StateStore


class CircuitBreakerTestSuite(unittest.TestCase):

    def setUp(self):
        self.breaker = CircuitBreaker(
            failure_threshold=3,
            base_cooldown=0.1,  # 测试环境加速冷却
            max_cooldown=2.0,
            jitter_max=0.0
        )

    def test_C01_single_timeout_records_failure(self):
        """C01: 单次网络超时 -> 记录失败，计数累加，但仍保持 CLOSED"""
        st = self.breaker.record_failure(http_status=-1, error=Exception("Timeout"))
        self.assertEqual(st, CircuitState.CLOSED)
        self.assertEqual(self.breaker.failure_count, 1)
        self.assertEqual(self.breaker.trip_count, 0)
        can, _ = self.breaker.can_attempt()
        self.assertTrue(can)

    def test_C02_consecutive_failures_reach_threshold_trips_to_open(self):
        """C02: 连续达到熔断阈值 -> 进入 OPEN"""
        self.breaker.record_failure(http_status=500)
        self.breaker.record_failure(http_status=503)
        st = self.breaker.record_failure(http_status=504)
        self.assertEqual(st, CircuitState.OPEN)
        self.assertEqual(self.breaker.trip_count, 1)

    def test_C03_open_state_blocks_new_requests(self):
        """C03: OPEN 状态 -> can_attempt 拒绝请求"""
        self.breaker.record_failure(http_status=500, force_trip=True)
        self.assertEqual(self.breaker.state, CircuitState.OPEN)

        can, reason = self.breaker.can_attempt()
        self.assertFalse(can)
        self.assertIn("OPEN", reason)

    def test_C04_open_state_cooldown_backoff(self):
        """C04: OPEN 状态 -> 冷却未完成时不频繁轮询"""
        self.breaker.record_failure(http_status=500, force_trip=True)
        rem = self.breaker.get_remaining_cooldown()
        self.assertGreater(rem, 0.0)

    def test_C05_cooldown_expiration_transitions_to_half_open(self):
        """C05: 冷却结束 -> 进入 HALF_OPEN"""
        self.breaker.record_failure(http_status=500, force_trip=True)
        time.sleep(0.15)  # 等待超过 base_cooldown 0.1s

        can, reason = self.breaker.can_attempt()
        self.assertTrue(can)
        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)

    def test_C06_half_open_success_recovers_to_closed(self):
        """C06: HALF_OPEN 成功 -> 恢复 CLOSED"""
        self.breaker.record_failure(http_status=500, force_trip=True)
        time.sleep(0.15)
        self.breaker.can_attempt()
        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)

        st = self.breaker.record_success(is_complete=True)
        self.assertEqual(st, CircuitState.CLOSED)
        self.assertEqual(self.breaker.failure_count, 0)
        can, _ = self.breaker.can_attempt()
        self.assertTrue(can)

    def test_C07_half_open_failure_trips_back_to_open_with_backoff(self):
        """C07: HALF_OPEN 失败 -> 重新进入 OPEN 且冷却时间倍增"""
        self.breaker.record_failure(http_status=500, force_trip=True)
        old_cooldown = self.breaker.current_cooldown
        time.sleep(0.15)
        self.breaker.can_attempt()

        # 半开探测失败
        st = self.breaker.record_failure(http_status=500)
        self.assertEqual(st, CircuitState.OPEN)
        self.assertEqual(self.breaker.trip_count, 2)
        self.assertGreater(self.breaker.current_cooldown, old_cooldown)

    def test_C08_half_open_allows_single_probe_no_concurrency(self):
        """C08: HALF_OPEN 只允许单次检查，拒绝并发探测"""
        self.breaker.record_failure(http_status=500, force_trip=True)
        time.sleep(0.15)

        can1, _ = self.breaker.can_attempt()
        self.assertTrue(can1)  # 第一次放行进行探测

        can2, reason2 = self.breaker.can_attempt()
        self.assertFalse(can2)  # 第二个请求被并发拦截
        self.assertIn("正在进行单次健康探测", reason2)

    def test_C09_health_check_incomplete_list_re_trips_to_open(self):
        """C09: 健康检查返回不完整列表 (complete=False) -> 不能恢复，重新进入 OPEN"""
        self.breaker.record_failure(http_status=500, force_trip=True)
        time.sleep(0.15)
        self.breaker.can_attempt()
        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)

        st = self.breaker.record_success(is_complete=False)
        self.assertEqual(st, CircuitState.OPEN)

    def test_C10_token_expired_401_does_not_trip_circuit(self):
        """C10: Token 失效 (401) -> 不属于服务端基础设施故障，不增加熔断计数"""
        self.assertFalse(is_server_fault(http_status=401))
        st = self.breaker.record_failure(http_status=401)
        self.assertEqual(st, CircuitState.CLOSED)
        self.assertEqual(self.breaker.failure_count, 0)

    def test_C11_refresh_token_expired_pause_does_not_loop_retry(self):
        """C11: 客户端 400 或业务错误不导致熔断器误触发"""
        self.assertFalse(is_server_fault(http_status=400))
        for _ in range(5):
            self.breaker.record_failure(http_status=400)
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.breaker.failure_count, 0)

    def test_C12_unknown_and_circuit_breaker_work_together(self):
        """C12: UNKNOWN + 熔断协同：熔断恢复不解除 UNKNOWN 安全屏障"""
        state_store = StateStore(":memory:")
        state_store.record_unknown("1001", "候选 A", "超时")
        self.assertTrue(state_store.has_unresolved())

        # 触发熔断
        self.breaker.record_failure(http_status=500, force_trip=True)
        self.assertEqual(self.breaker.state, CircuitState.OPEN)

        # 熔断冷却并恢复
        time.sleep(0.15)
        self.breaker.can_attempt()
        self.breaker.record_success(is_complete=True)
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)

        # 核心验证：熔断器恢复了，但 UNKNOWN 安全屏障仍然必须保持冻结！
        self.assertTrue(state_store.has_unresolved())
        state_store.close()

    # =========================================================================
    # Phase 1 强化测试 (C13～C23)：活动列表请求与熔断器集成
    # =========================================================================

    def test_C13_closed_state_incomplete_list_does_not_clear_failure_count(self):
        """C13: CLOSED 状态下接收到不完整列表 (complete=False)，严禁清零现有失败计数"""
        self.breaker.record_failure(http_status=500)
        self.breaker.record_failure(http_status=500)
        self.assertEqual(self.breaker.failure_count, 2)
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)

        # 接收到不完整列表（如由于网络残缺），调用 record_success(is_complete=False)
        self.breaker.record_success(is_complete=False)
        # 核心断言：失败计数严禁被清零！仍应为 2
        self.assertEqual(self.breaker.failure_count, 2)
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)

        # 随后再次发生一次 500 故障，累加达到 3，立即触发 OPEN
        self.breaker.record_failure(http_status=500)
        self.assertEqual(self.breaker.state, CircuitState.OPEN)
        self.assertEqual(self.breaker.trip_count, 1)

    def test_C14_activities_consecutive_three_500_trips_to_open(self):
        """C14: 活动列表连续 3 次返回 HTTP 500 基础设施故障，准确触发进入 OPEN"""
        from lejian import ActivityList

        # 模拟主循环中连续 3 次列表查询均遇到 500
        for i in range(3):
            items = ActivityList([], complete=False, total=0, fetched=0,
                                 error="第 1 页拉取失败 (HTTP 500)", http_code=500)
            if not getattr(items, "complete", True):
                code = getattr(items, "http_code", None)
                if code is not None and is_server_fault(code):
                    self.breaker.record_failure(http_status=code, error=Exception(items.error))

        self.assertEqual(self.breaker.state, CircuitState.OPEN)
        self.assertEqual(self.breaker.trip_count, 1)
        self.assertEqual(self.breaker.failure_count, 3)

    def test_C15_activities_consecutive_timeouts_trips_to_open(self):
        """C15: 活动列表连续 3 次网络超时 (HTTP -1)，准确触发进入 OPEN"""
        from lejian import ActivityList

        for i in range(3):
            items = ActivityList([], complete=False, total=0, fetched=0,
                                 error="第 1 页拉取失败 (HTTP -1)", http_code=-1)
            if not getattr(items, "complete", True):
                code = getattr(items, "http_code", None)
                if code is not None and is_server_fault(code):
                    self.breaker.record_failure(http_status=code, error=Exception(items.error))

        self.assertEqual(self.breaker.state, CircuitState.OPEN)
        self.assertEqual(self.breaker.trip_count, 1)

    def test_C16_gateway_errors_502_503_504_classified_as_server_fault(self):
        """C16: 网关错误 502、503、504 正确识别为基础设施故障，累加并触发熔断"""
        self.assertTrue(is_server_fault(502))
        self.assertTrue(is_server_fault(503))
        self.assertTrue(is_server_fault(504))

        self.breaker.record_failure(http_status=502)
        self.assertEqual(self.breaker.failure_count, 1)
        self.breaker.record_failure(http_status=503)
        self.assertEqual(self.breaker.failure_count, 2)
        self.breaker.record_failure(http_status=504)
        self.assertEqual(self.breaker.state, CircuitState.OPEN)

    def test_C17_activities_401_403_400_not_classified_as_server_fault(self):
        """C17: 401（未授权）、403（禁止访问）、400（参数错误）不计入基础设施故障，不触发熔断"""
        self.assertFalse(is_server_fault(401))
        self.assertFalse(is_server_fault(403))
        self.assertFalse(is_server_fault(400))

        # 连续多次 401 / 403 / 400 绝不能导致熔断器 OPEN
        for code in (401, 403, 400, 401, 400):
            st = self.breaker.record_failure(http_status=code)
            self.assertEqual(st, CircuitState.CLOSED)
            self.assertEqual(self.breaker.failure_count, 0)

    def test_C18_open_state_blocks_normal_polling_until_cooldown(self):
        """C18: OPEN 状态下严禁发起正常列表轮询，can_attempt 必须返回 False 并在冷却期内保持"""
        self.breaker.record_failure(http_status=500, force_trip=True)
        self.assertEqual(self.breaker.state, CircuitState.OPEN)

        for _ in range(5):
            can, msg = self.breaker.can_attempt()
            self.assertFalse(can)
            self.assertIn("OPEN", msg)

    def test_C19_half_open_probe_success_with_complete_list_recovers_to_closed(self):
        """C19: HALF_OPEN 单次健康探测成功且列表完整时，平滑恢复至 CLOSED 且清零失败计数"""
        from lejian import ActivityList

        self.breaker.record_failure(http_status=500, force_trip=True)
        time.sleep(0.15)
        can, msg = self.breaker.can_attempt()
        self.assertTrue(can)
        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)

        # 探测拉取到完整列表
        probe_items = ActivityList([{"id": "1"}], complete=True, total=1, fetched=1)
        self.assertTrue(probe_items.complete)
        st = self.breaker.record_success(is_complete=probe_items.complete)
        self.assertEqual(st, CircuitState.CLOSED)
        self.assertEqual(self.breaker.failure_count, 0)

        # 恢复后允许正常业务请求
        can_after, _ = self.breaker.can_attempt()
        self.assertTrue(can_after)

    def test_C20_half_open_probe_failure_retrips_to_open(self):
        """C20: HALF_OPEN 单次探测若发生故障 (500)，立即重新 OPEN 且退避冷却时间增加"""
        self.breaker.record_failure(http_status=500, force_trip=True)
        old_cooldown = self.breaker.current_cooldown
        time.sleep(0.15)
        self.breaker.can_attempt()
        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)

        # 探测遇到 500
        st = self.breaker.record_failure(http_status=500)
        self.assertEqual(st, CircuitState.OPEN)
        self.assertGreater(self.breaker.current_cooldown, old_cooldown)

    def test_C21_half_open_incomplete_list_retrips_to_open(self):
        """C21: HALF_OPEN 探测若返回残缺不完整列表 (complete=False)，绝不能恢复业务，重新进入 OPEN"""
        from lejian import ActivityList

        self.breaker.record_failure(http_status=500, force_trip=True)
        time.sleep(0.15)
        self.breaker.can_attempt()
        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)

        # 探测结果残缺
        incomplete_probe = ActivityList([{"id": "1"}], complete=False, total=10, fetched=1)
        st = self.breaker.record_success(is_complete=incomplete_probe.complete)
        self.assertEqual(st, CircuitState.OPEN)

        can, reason = self.breaker.can_attempt()
        self.assertFalse(can)
        self.assertIn("OPEN", reason)

    def test_C22_activities_inner_retry_does_not_cause_duplicate_accumulation(self):
        """C22: 验证 activities 即使配置内部多次重试，主轮询单次查询也只按 1 次失败计入熔断，杜绝重复累加"""
        from lejian import Client, ActivityList

        client = Client({"semesterId": "sem1", "schoolId": "sch1", "accessToken": "tok"})
        call_count = 0

        def mock_call(method, path, body=None, timeout=6):
            nonlocal call_count
            call_count += 1
            return 500, "Internal Server Error"

        client.call = mock_call

        # 执行一次包含内部 2 次重试 (总共 3 次尝试) 的 activities 调用
        items = client.activities(page_size=200, max_retries=2)
        self.assertEqual(call_count, 3)  # 内部尝试了 3 次 (1 首次 + 2 重试)
        self.assertFalse(items.complete)
        self.assertEqual(items.http_code, 500)

        # 外部主循环只将该次轮询计为 1 次熔断失败，绝不重复累加 3 次
        self.breaker.record_failure(http_status=items.http_code, error=Exception(items.error))
        self.assertEqual(self.breaker.failure_count, 1)  # 只有 1 次，不会瞬间触发熔断！
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)

    def test_C23_full_recovery_resumes_normal_polling(self):
        """C23: 熔断恢复为 CLOSED 后，允许后续正常轮询连续成功并不被误拦截"""
        # 先经历一次熔断
        self.breaker.record_failure(http_status=500, force_trip=True)
        time.sleep(0.15)
        self.breaker.can_attempt()
        self.breaker.record_success(is_complete=True)
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)

        # 恢复后连续 5 次成功请求
        for _ in range(5):
            can, msg = self.breaker.can_attempt()
            self.assertTrue(can)
            st = self.breaker.record_success(is_complete=True)
            self.assertEqual(st, CircuitState.CLOSED)
            self.assertEqual(self.breaker.failure_count, 0)

    # =========================================================================
    # Phase 1 补充验收测试 (C24～C30)：熔断器边界条件与重试语义核实
    # =========================================================================

    def test_C24_half_open_http_401_releases_probe_lock_and_preserves_cooldown(self):
        """C24: HALF_OPEN 状态下遇到 401，严禁计入基础设施故障，释放探测锁并保持退避时间不变"""
        self.breaker.record_failure(http_status=500, force_trip=True)
        self.assertEqual(self.breaker.state, CircuitState.OPEN)
        expected_cooldown = self.breaker.current_cooldown
        expected_trips = self.breaker.trip_count
        expected_failures = self.breaker.failure_count

        time.sleep(0.15)
        can, msg = self.breaker.can_attempt()
        self.assertTrue(can)
        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)
        self.assertTrue(self.breaker._half_open_probe_in_progress)

        # 探测遇到 401（Token 过期）
        st = self.breaker.record_failure(http_status=401)
        # 核心断言：
        # 1. 状态保持 HALF_OPEN，严禁被误置为 CLOSED，也不被当作基础设施故障切入 OPEN
        self.assertEqual(st, CircuitState.HALF_OPEN)
        # 2. 基础设施故障计数严禁增加
        self.assertEqual(self.breaker.failure_count, expected_failures)
        # 3. trip_count 严禁增加
        self.assertEqual(self.breaker.trip_count, expected_trips)
        # 4. 退避冷却时间严禁被指数倍增
        self.assertEqual(self.breaker.current_cooldown, expected_cooldown)
        # 5. 必须释放单次探测锁，避免永久挂起
        self.assertFalse(self.breaker._half_open_probe_in_progress)

        # 6. 探测锁释放后，再次调用 can_attempt 应当立即允许重新探测（等待 Token 刷新）
        can_next, _ = self.breaker.can_attempt()
        self.assertTrue(can_next)

    def test_C25_half_open_http_403_and_400_not_classified_as_server_fault(self):
        """C25: HALF_OPEN 状态下遇到 403 / 400，释放探测锁，不增加故障计数，不增加退避"""
        self.breaker.record_failure(http_status=500, force_trip=True)
        time.sleep(0.15)
        self.breaker.can_attempt()
        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)
        expected_failures = self.breaker.failure_count

        # 403 禁止访问
        st403 = self.breaker.record_failure(http_status=403)
        self.assertEqual(st403, CircuitState.HALF_OPEN)
        self.assertEqual(self.breaker.failure_count, expected_failures)
        self.assertFalse(self.breaker._half_open_probe_in_progress)

        # 再次获取探测机会测试 400
        self.breaker.can_attempt()
        st400 = self.breaker.record_failure(http_status=400)
        self.assertEqual(st400, CircuitState.HALF_OPEN)
        self.assertEqual(self.breaker.failure_count, expected_failures)
        self.assertFalse(self.breaker._half_open_probe_in_progress)

    def test_C26_half_open_401_recovers_to_closed_after_reauth_probe(self):
        """C26: HALF_OPEN 遇到 401 后完成重认证，再次健康探测成功且列表完整时平滑恢复 CLOSED"""
        from lejian import ActivityList

        self.breaker.record_failure(http_status=500, force_trip=True)
        time.sleep(0.15)
        self.breaker.can_attempt()
        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)

        # 第 1 次探测：401 失败
        self.breaker.record_failure(http_status=401)
        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)

        # 外部执行认证刷新后，第 2 次发起健康探测
        can, _ = self.breaker.can_attempt()
        self.assertTrue(can)

        # 探测成功获得完整列表
        probe_items = ActivityList([{"id": "act_1"}], complete=True, total=1, fetched=1)
        st = self.breaker.record_success(is_complete=probe_items.complete)
        self.assertEqual(st, CircuitState.CLOSED)
        self.assertEqual(self.breaker.failure_count, 0)

        # 恢复后正常请求畅通
        can_normal, _ = self.breaker.can_attempt()
        self.assertTrue(can_normal)

    def test_C27_client_errors_do_not_cause_infinite_backoff(self):
        """C27: 连续客户端异常 (401/403/400) 绝不会导致 trip_count 累加或冷却时间无限增长"""
        self.breaker.record_failure(http_status=500, force_trip=True)
        time.sleep(0.15)
        self.breaker.can_attempt()
        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)

        init_trip_count = self.breaker.trip_count
        init_cooldown = self.breaker.current_cooldown
        init_failures = self.breaker.failure_count

        # 模拟 10 次由于凭据或参数异常引起的失败响应
        for code in (401, 403, 400, 401, 403, 400, 401, 403, 400, 401):
            st = self.breaker.record_failure(http_status=code)
            self.assertEqual(st, CircuitState.HALF_OPEN)
            # 释放锁后允许下一次尝试
            can, _ = self.breaker.can_attempt()
            self.assertTrue(can)

        # 核心断言：退避次数和冷却时间完全没有因客户端异常而恶化
        self.assertEqual(self.breaker.trip_count, init_trip_count)
        self.assertEqual(self.breaker.current_cooldown, init_cooldown)
        self.assertEqual(self.breaker.failure_count, init_failures)

    def test_C28_activities_max_retries_zero_executes_single_http_call(self):
        """C28: activities(max_retries=0) 语义测试：允许且仅发起 1 次 HTTP 请求（0 次额外重试）"""
        from lejian import Client

        client = Client({"semesterId": "sem1", "schoolId": "sch1", "accessToken": "tok"})
        call_count = 0

        def mock_call(method, path, body=None, timeout=6):
            nonlocal call_count
            call_count += 1
            return 500, "Internal Server Error"

        client.call = mock_call

        items = client.activities(page_size=200, max_retries=0)
        # 核心断言：调用次数严格等于 1
        self.assertEqual(call_count, 1)
        self.assertFalse(items.complete)
        self.assertEqual(items.http_code, 500)

    def test_C29_activities_max_retries_one_executes_two_http_calls_on_failure(self):
        """C29: activities(max_retries=1) 语义测试：失败时发起 2 次 HTTP 请求（1 次首次 + 1 次重试）"""
        from lejian import Client

        client = Client({"semesterId": "sem1", "schoolId": "sch1", "accessToken": "tok"})
        call_count = 0

        def mock_call(method, path, body=None, timeout=6):
            nonlocal call_count
            call_count += 1
            return 500, "Internal Server Error"

        client.call = mock_call

        items = client.activities(page_size=200, max_retries=1)
        # 核心断言：1 首次 + 1 重试 = 2 次
        self.assertEqual(call_count, 2)
        self.assertFalse(items.complete)
        self.assertEqual(items.http_code, 500)

    def test_C30_main_grab_polling_mock_http_call_count_exact_match(self):
        """C30: 主抢课轮询单次轮询在故障时底层 HTTP 调用次数严格为 1，与熔断统计严格 1:1 对齐"""
        from lejian import Client, is_server_fault

        client = Client({"semesterId": "sem1", "schoolId": "sch1", "accessToken": "tok"})
        http_calls = 0

        def mock_call(method, path, body=None, timeout=6):
            nonlocal http_calls
            http_calls += 1
            return 502, "Bad Gateway"

        client.call = mock_call

        # 主抢课循环标准调用：max_retries=0
        items = client.activities(page_size=200, req_timeout=6, max_retries=0)
        # 底层 HTTP 调用严格为 1
        self.assertEqual(http_calls, 1)

        # 主循环根据返回结果驱动熔断器
        if not getattr(items, "complete", True):
            code = getattr(items, "http_code", None)
            if code is not None and is_server_fault(code):
                self.breaker.record_failure(http_status=code, error=Exception(items.error))

        # 熔断器统计次数也严格为 1，完全 1:1，无任何重复累计或隐式重试
        self.assertEqual(self.breaker.failure_count, 1)
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)


if __name__ == "__main__":
    unittest.main()
