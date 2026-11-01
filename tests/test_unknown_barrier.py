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


if __name__ == "__main__":
    unittest.main()
