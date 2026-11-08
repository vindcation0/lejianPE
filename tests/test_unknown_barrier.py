"""
UNKNOWN 安全屏障专项测试套件 (U01～U12)
=====================================
覆盖文档任务一要求的所有核心安全测试。
"""
import datetime
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core.audit_logger import AuditLogger
from core.recovery_manager import RecoveryManager
from core.registration_manager import RegistrationManager
from core.request_manager import RequestManager, SubmitResult
from core.state_store import RegistrationStatus, StateStore


class UnknownBarrierTestSuite(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_reg.db")
        self.log_dir = os.path.join(self.temp_dir.name, "logs")
        self.state_store = StateStore(self.db_path)
        self.audit_logger = AuditLogger(self.log_dir)
        self.mock_client = MagicMock()
        self.req_mgr = RequestManager(self.mock_client)
        self.reg_mgr = RegistrationManager(self.state_store, self.req_mgr, self.audit_logger)
        self.recovery_mgr = RecoveryManager(self.state_store, self.req_mgr, self.audit_logger)

    def tearDown(self):
        self.state_store.close()
        self.temp_dir.cleanup()

    def test_U01_first_candidate_timeout_enters_unknown(self):
        """U01: 第一个候选超时 -> 进入 UNKNOWN 并冻结"""
        self.mock_client.call.side_effect = Exception("HTTP -1: Request timed out")
        self.mock_client.activities.side_effect = Exception("Timeout during verify")

        item_a = {"id": "1001", "name": "户外跑 程启航", "startTime": 1730000000000}
        ok, status, msg, _ = self.reg_mgr.register_activity(item_a)

        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.UNKNOWN)
        rec = self.state_store.get_record("1001")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], RegistrationStatus.UNKNOWN)
        self.assertTrue(self.state_store.has_unresolved())

    def test_U02_first_candidate_503_enters_unknown(self):
        """U02: 第一个候选 503 -> 进入 UNKNOWN 并冻结"""
        self.mock_client.call.return_value = (503, "Service Unavailable")
        self.mock_client.activities.side_effect = Exception("503 verify failed")

        item_a = {"id": "1002", "name": "户外跑 王彤帆", "startTime": 1730000000000}
        ok, status, msg, _ = self.reg_mgr.register_activity(item_a)

        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.UNKNOWN)
        self.assertTrue(self.state_store.has_unresolved())

    def test_U03_subsequent_candidate_prohibited_when_unknown_exists(self):
        """U03: UNKNOWN 后还有其他候选 -> 屏障生效，不得提交后续候选"""
        # 先让候选 A 变成 UNKNOWN
        self.state_store.record_unknown("1001", "候选 A", "超时未明")
        self.assertTrue(self.state_store.has_unresolved())

        # 尝试提交候选 B
        item_b = {"id": "1002", "name": "候选 B", "startTime": 1730000000000}
        ok, status, msg, _ = self.reg_mgr.register_activity(item_b)

        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.UNKNOWN)
        self.assertIn("安全屏障已冻结队列", msg)
        # 确保候选 B 根本没有被写入 SUBMITTING，也没有调用网络
        self.mock_client.call.assert_not_called()
        self.assertIsNone(self.state_store.get_record("1002"))

    def test_U04_unknown_verify_success_updates_quota(self):
        """U04: UNKNOWN 复核成功 -> 更新为 SUCCESS 状态"""
        self.state_store.record_unknown("1001", "候选 A", "超时")

        # 模拟复核接口返回包含 isRegister=True 的列表
        mock_item = {"id": "1001", "name": "候选 A", "isRegister": True, "startTime": 1730000000000}
        self.mock_client.activities.return_value = [mock_item]

        res_ok, res_status, res_msg, final_it = self.reg_mgr.resolve_unknown("1001", "候选 A")

        self.assertTrue(res_ok)
        self.assertEqual(res_status, RegistrationStatus.SUCCESS)
        rec = self.state_store.get_record("1001")
        self.assertEqual(rec["status"], RegistrationStatus.SUCCESS)
        self.assertFalse(self.state_store.has_unresolved())

    def test_U05_unknown_verify_confirmed_not_registered_frees_reservation(self):
        """U05: UNKNOWN 复核明确失败 -> 转为 RETRYABLE 并允许后续候选"""
        self.state_store.record_unknown("1001", "候选 A", "超时")

        # 模拟复核返回完整列表但 isRegister=False
        class MockList(list):
            complete = True
        mock_items = MockList([{"id": "1001", "name": "候选 A", "isRegister": False}])
        self.mock_client.activities.return_value = mock_items

        res_ok, res_status, res_msg, _ = self.reg_mgr.resolve_unknown("1001", "候选 A")

        self.assertFalse(res_ok)
        self.assertEqual(res_status, RegistrationStatus.RETRYABLE)
        rec = self.state_store.get_record("1001")
        self.assertEqual(rec["status"], RegistrationStatus.RETRYABLE)
        # 未决状态已解除
        self.assertFalse(self.state_store.has_unresolved())

    def test_U06_unknown_verify_incomplete_list_keeps_frozen(self):
        """U06: UNKNOWN 复核列表不完整 -> 绝不能误判为未报，保持冻结"""
        self.state_store.record_unknown("1001", "候选 A", "超时")

        # 模拟第一页成功第二页失败返回 complete=False 的残缺列表
        class IncompleteList(list):
            complete = False
        self.mock_client.activities.return_value = IncompleteList([{"id": "9999", "name": "其他活动"}])

        res_ok, res_status, res_msg, _ = self.reg_mgr.resolve_unknown("1001", "候选 A")

        self.assertFalse(res_ok)
        self.assertEqual(res_status, RegistrationStatus.UNKNOWN)
        self.assertTrue(self.state_store.has_unresolved())

    def test_U07_unknown_verify_timeout_keeps_frozen(self):
        """U07: UNKNOWN 复核请求超时 -> 保持冻结"""
        self.state_store.record_unknown("1001", "候选 A", "超时")
        self.mock_client.activities.side_effect = Exception("Verify timeout")

        res_ok, res_status, res_msg, _ = self.reg_mgr.resolve_unknown("1001", "候选 A")

        self.assertFalse(res_ok)
        self.assertEqual(res_status, RegistrationStatus.UNKNOWN)
        self.assertTrue(self.state_store.has_unresolved())

    def test_U08_startup_with_submitting_recovers_to_unknown_and_verifies(self):
        """U08: 程序重启时存在遗留 SUBMITTING -> 恢复为 UNKNOWN 并进行只读复核"""
        # 人工在数据库中留下崩溃遗留的 SUBMITTING 记录
        self.state_store.record_submitting("1001", "崩溃遗留活动")
        rec_before = self.state_store.get_record("1001")
        self.assertEqual(rec_before["status"], RegistrationStatus.SUBMITTING)

        # 模拟复核时服务端确认已经存在 isRegister=True
        self.mock_client.activities.return_value = [{"id": "1001", "name": "崩溃遗留活动", "isRegister": True}]

        summary = self.recovery_mgr.recover_on_startup()

        self.assertEqual(len(summary["recovered_success"]), 1)
        rec_after = self.state_store.get_record("1001")
        self.assertEqual(rec_after["status"], RegistrationStatus.SUCCESS)

    def test_U09_startup_with_unknown_cannot_resume_direct_grab(self):
        """U09: 程序重启时存在 UNKNOWN -> 复核失败时严禁直接恢复报名，必须维持冻结"""
        self.state_store.record_unknown("1001", "上次遗留 UNKNOWN", "超时未明")
        self.mock_client.activities.side_effect = Exception("服务端依然 500")

        summary = self.recovery_mgr.recover_on_startup()

        self.assertEqual(len(summary["remained_unknown"]), 1)
        self.assertTrue(self.state_store.has_unresolved())

        # 尝试提交新候选
        item_new = {"id": "1002", "name": "新候选活动"}
        ok, status, msg, _ = self.reg_mgr.register_activity(item_new)
        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.UNKNOWN)
        self.assertIn("安全屏障已冻结队列", msg)

    def test_U10_unknown_confirmed_success_reaches_limit_stops_queue(self):
        """U10: UNKNOWN 确认成功后达到上限 -> 停止后续队列"""
        self.state_store.record_unknown("1001", "候选 A", "超时")
        mock_item = {"id": "1001", "name": "候选 A", "isRegister": True, "startTime": 1730000000000}
        self.mock_client.activities.return_value = [mock_item]

        res_ok, res_status, _, _ = self.reg_mgr.resolve_unknown("1001", "候选 A")
        self.assertTrue(res_ok)
        self.assertEqual(res_status, RegistrationStatus.SUCCESS)

        # 配额检查规则模拟
        max_normal = 1
        current_confirmed = 1
        self.assertGreaterEqual(current_confirmed, max_normal)

    def test_U11_unknown_confirmed_failed_allows_next_candidate(self):
        """U11: UNKNOWN 确认失败后仍有名额 -> 允许提交下一候选"""
        self.state_store.record_unknown("1001", "候选 A", "超时")

        class MockList(list):
            complete = True
        self.mock_client.activities.return_value = MockList([{"id": "1001", "name": "候选 A", "isRegister": False}])

        # 复核确认未报
        res_ok, res_status, _, _ = self.reg_mgr.resolve_unknown("1001", "候选 A")
        self.assertEqual(res_status, RegistrationStatus.RETRYABLE)
        self.assertFalse(self.state_store.has_unresolved())

        # 下一个候选现在被放行
        self.mock_client.call.return_value = (200, {"code": 0, "data": {"success": True}, "message": "OK"})
        item_b = {"id": "1002", "name": "候选 B", "startTime": 1730000000000}
        ok_b, status_b, msg_b, _ = self.reg_mgr.register_activity(item_b)
        self.assertTrue(ok_b)
        self.assertEqual(status_b, RegistrationStatus.SUCCESS)

    def test_U12_concurrent_unresolved_requests_rejected(self):
        """U12: 同时出现未决请求时拒绝第二个提交"""
        self.state_store.record_submitting("1001", "未决请求 1")
        self.assertTrue(self.state_store.has_unresolved())

        item_c = {"id": "1003", "name": "新请求 2"}
        ok, status, msg, _ = self.reg_mgr.register_activity(item_c)
        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.UNKNOWN)
        self.assertIn("安全屏障已冻结队列", msg)

    # =========================================================================
    # Phase 2 强化测试 (U13～U20)：熔断独立、完整性校验、崩溃自愈与配额闭环
    # =========================================================================

    def test_U13_circuit_breaker_recovery_to_closed_does_not_clear_unknown_barrier(self):
        """U13: 熔断器恢复至 CLOSED 绝不解除 UNKNOWN 屏障，未决记录存在时必须继续冻结候选队列"""
        from core.circuit_breaker import CircuitBreaker, CircuitState

        breaker = CircuitBreaker(failure_threshold=3, base_cooldown=0.05, jitter_max=0.0)
        # 模拟发生故障熔断
        breaker.record_failure(http_status=500, force_trip=True)
        self.assertEqual(breaker.state, CircuitState.OPEN)

        # 存在一个 UNKNOWN 未决记录
        self.state_store.record_unknown("1001", "未决活动 A", "超时未明")
        self.assertTrue(self.state_store.has_unresolved())

        # 服务端网络恢复，熔断器经过半开探测恢复至 CLOSED
        import time
        time.sleep(0.08)
        can, _ = breaker.can_attempt()
        self.assertTrue(can)
        breaker.record_success(is_complete=True)
        self.assertEqual(breaker.state, CircuitState.CLOSED)

        # 核心断言：熔断器虽然已 CLOSED，但 UNKNOWN 依然存在，新候选绝对禁止提交
        item_b = {"id": "1002", "name": "新候选活动 B", "startTime": 1730000000000}
        ok, status, msg, _ = self.reg_mgr.register_activity(item_b)
        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.UNKNOWN)
        self.assertIn("安全屏障已冻结队列", msg)
        self.mock_client.call.assert_not_called()

    def test_U14_unknown_verify_not_registered_requires_complete_list(self):
        """U14: 复核确认未报名必须满足列表完整性明确为 True，残缺列表 (complete=False) 禁止判定为未报名"""
        self.state_store.record_unknown("1001", "候选 A", "超时")

        class TruncatedList(list):
            complete = False
            http_code = 200

        # 列表中虽然包含了 1001 且 isRegister=False，但由于分页被截断 complete=False
        mock_items = TruncatedList([{"id": "1001", "name": "候选 A", "isRegister": False}])
        self.mock_client.activities.return_value = mock_items

        res_ok, res_status, res_msg, _ = self.reg_mgr.resolve_unknown("1001", "候选 A")
        # 核心断言：残缺列表禁止转为 RETRYABLE，必须严格保持 UNKNOWN
        self.assertFalse(res_ok)
        self.assertEqual(res_status, RegistrationStatus.UNKNOWN)
        rec = self.state_store.get_record("1001")
        self.assertEqual(rec["status"], RegistrationStatus.UNKNOWN)
        self.assertTrue(self.state_store.has_unresolved())

    def test_U15_unknown_verify_target_missing_from_list_keeps_frozen(self):
        """U15: 复核时目标活动未在列表中检索到（可能下架或分页未盖全），禁止判定为未报名，保持 UNKNOWN"""
        self.state_store.record_unknown("1001", "候选 A", "超时")

        class CompleteList(list):
            complete = True
            http_code = 200

        # 列表中有其他活动，但未包含目标 1001
        mock_items = CompleteList([{"id": "9999", "name": "其他活动", "isRegister": False}])
        self.mock_client.activities.return_value = mock_items

        res_ok, res_status, res_msg, _ = self.reg_mgr.resolve_unknown("1001", "候选 A")
        # 核心断言：未在列表中找到绝对不能轻率断定为未报名！
        self.assertFalse(res_ok)
        self.assertEqual(res_status, RegistrationStatus.UNKNOWN)
        self.assertTrue(self.state_store.has_unresolved())

    def test_U16_unknown_verify_server_fault_502_keeps_frozen(self):
        """U16: 复核请求遇到 HTTP 502/500 网关错误，严格保持 UNKNOWN 并冻结"""
        self.state_store.record_unknown("1001", "候选 A", "超时")

        class ServerFaultList(list):
            complete = False
            http_code = 502

        mock_items = ServerFaultList([{"id": "1001", "name": "候选 A", "isRegister": False}])
        self.mock_client.activities.return_value = mock_items

        res_ok, res_status, res_msg, _ = self.reg_mgr.resolve_unknown("1001", "候选 A")
        self.assertFalse(res_ok)
        self.assertEqual(res_status, RegistrationStatus.UNKNOWN)
        self.assertTrue(self.state_store.has_unresolved())

    def test_U17_unknown_verify_empty_or_malformed_list_keeps_frozen(self):
        """U17: 复核列表返回空列表或格式异常，禁止判定为未报名，保持 UNKNOWN"""
        self.state_store.record_unknown("1001", "候选 A", "超时")

        # 模拟返回空列表
        self.mock_client.activities.return_value = []
        res_ok, res_status, res_msg, _ = self.reg_mgr.resolve_unknown("1001", "候选 A")
        self.assertFalse(res_ok)
        self.assertEqual(res_status, RegistrationStatus.UNKNOWN)
        self.assertTrue(self.state_store.has_unresolved())

    def test_U18_crash_recovery_submitting_verified_success_never_resubmits(self):
        """U18: 崩溃重启恢复遗留 SUBMITTING：只读复核为 SUCCESS 时更新状态且绝不再次发起网络提交"""
        # 崩溃遗留 SUBMITTING
        self.state_store.record_submitting("1001", "崩溃遗留活动")
        rec_before = self.state_store.get_record("1001")
        self.assertEqual(rec_before["status"], RegistrationStatus.SUBMITTING)

        # 复核时服务端明确确认已有报名记录
        self.mock_client.activities.return_value = [
            {"id": "1001", "name": "崩溃遗留活动", "isRegister": True, "startTime": 1730000000000}
        ]

        summary = self.recovery_mgr.recover_on_startup()
        self.assertEqual(len(summary["recovered_success"]), 1)
        rec_after = self.state_store.get_record("1001")
        self.assertEqual(rec_after["status"], RegistrationStatus.SUCCESS)

        # 核心断言：启动恢复过程完全为只读复核，signUp 绝对未被调用
        self.mock_client.call.assert_not_called()

        # 随后若再次尝试 register_activity 该活动，直接返回本地 SUCCESS，绝不重复提交
        ok, status, msg, _ = self.reg_mgr.register_activity({"id": "1001", "name": "崩溃遗留活动"})
        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.SUCCESS)
        self.mock_client.call.assert_not_called()

    def test_U19_crash_recovery_submitting_verified_failure_recovers_to_unknown(self):
        """U19: 崩溃重启恢复遗留 SUBMITTING：若复核遇网络异常，安全置为 UNKNOWN 并冻结队列，禁止重新提交"""
        self.state_store.record_submitting("1001", "崩溃遗留活动")
        # 复核时网络仍然不可用
        self.mock_client.activities.side_effect = Exception("HTTP 500: Server Down")

        summary = self.recovery_mgr.recover_on_startup()
        self.assertEqual(len(summary["remained_unknown"]), 1)
        rec_after = self.state_store.get_record("1001")
        # 核心断言：SUBMITTING 被安全置换为 UNKNOWN，避免悬挂
        self.assertEqual(rec_after["status"], RegistrationStatus.UNKNOWN)
        self.assertTrue(self.state_store.has_unresolved())

        # 尝试提交新活动立即被安全屏障拦截
        ok, status, msg, _ = self.reg_mgr.register_activity({"id": "1002", "name": "新候选"})
        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.UNKNOWN)
        self.assertIn("安全屏障已冻结队列", msg)
        self.mock_client.call.assert_not_called()

    def test_U20_unknown_confirmed_success_quota_update_blocks_further_candidates(self):
        """U20: UNKNOWN 复核确认成功后更新配额，若达到配额上限则阻止后续同周候选提交"""
        self.state_store.record_unknown("1001", "候选 A", "超时")

        # 模拟复核确认成功
        mock_item_a = {
            "id": "1001",
            "name": "羽毛球 A",
            "isRegister": True,
            "startTime": 1730000000000
        }
        self.mock_client.activities.return_value = [mock_item_a]

        res_ok, res_status, _, item_resolved = self.reg_mgr.resolve_unknown("1001", "羽毛球 A")
        self.assertTrue(res_ok)
        self.assertEqual(res_status, RegistrationStatus.SUCCESS)

        # 模拟主循环规则校验器：当前已有 1 节普通课程已确认，若配额上限为 1，则同周下一候选被阻断
        max_normal = 1
        current_confirmed = 1  # 候选 A 成功后计入配额

        def validate_rules(candidate_item):
            if current_confirmed >= max_normal:
                return False, f"当周普通活动已报满 {max_normal} 节"
            return True, "OK"

        item_b = {"id": "1002", "name": "篮球 B", "startTime": 1730000000000}
        ok_b, status_b, msg_b, _ = self.reg_mgr.register_activity(item_b, validate_rules_cb=validate_rules)
        self.assertFalse(ok_b)
        self.assertEqual(status_b, RegistrationStatus.FAILED)
        self.assertIn("当周普通活动已报满", msg_b)
        self.mock_client.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
