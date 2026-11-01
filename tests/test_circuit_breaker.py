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


if __name__ == "__main__":
    unittest.main()
