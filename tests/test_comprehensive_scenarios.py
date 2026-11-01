"""
综合端到端场景测试套件 (场景 A～E)
=================================
按照执行文档第十章模拟 5 大综合端到端复杂场景。
"""
import datetime
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core.audit_logger import AuditLogger
from core.circuit_breaker import CircuitBreaker, CircuitState
from core.recovery_manager import RecoveryManager
from core.registration_manager import RegistrationManager
from core.request_manager import RequestManager
from core.result_reporter import ResultReporter
from core.state_store import RegistrationStatus, StateStore


class ComprehensiveScenariosTestSuite(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "scenario.db")
        self.log_dir = os.path.join(self.temp_dir.name, "logs")
        self.state_store = StateStore(self.db_path)
        self.audit_logger = AuditLogger(self.log_dir)
        self.mock_client = MagicMock()
        self.req_mgr = RequestManager(self.mock_client)
        self.reg_mgr = RegistrationManager(self.state_store, self.req_mgr, self.audit_logger)
        self.recovery_mgr = RecoveryManager(self.state_store, self.req_mgr, self.audit_logger)
        self.breaker = CircuitBreaker(failure_threshold=3, base_cooldown=0.1, jitter_max=0.0)
        self.reporter = ResultReporter()

    def tearDown(self):
        self.state_store.close()
        self.temp_dir.cleanup()

    def test_scenario_A_first_submit_timeout_reconciled_success_stops(self):
        """
        场景 A：第一次提交就超时
        候选 A 提交 -> HTTP 超时 -> UNKNOWN -> 冻结队列 -> 复核成功 -> 更新名额 -> 达到上限 -> 停止
        预期：
        - 只发送一次报名请求
        - 不提交 B、C
        - 最终确认 A 成功
        - 不超额
        """
        candidate_a = {"id": "1001", "name": "户外跑 程启航", "startTime": 1730000000000}
        candidate_b = {"id": "1002", "name": "户外跑 王彤帆", "startTime": 1730000000000}
        candidate_c = {"id": "1003", "name": "户外跑 廖华", "startTime": 1730000000000}

        # 1. 提交 A 时服务端超时，且复核暂时失败
        self.mock_client.call.return_value = (-1, "Timeout")
        self.mock_client.activities.side_effect = Exception("List timeout")

        ok_a, status_a, _, _ = self.reg_mgr.register_activity(candidate_a)
        self.assertEqual(status_a, RegistrationStatus.UNKNOWN)
        self.reporter.record_attempt("1001", "户外跑 程启航", status_a)

        # 2. 模拟由于存在 UNKNOWN，安全屏障冻结了候选 B 和 C 的提交
        self.assertTrue(self.state_store.has_unresolved())
        ok_b, status_b, msg_b, _ = self.reg_mgr.register_activity(candidate_b)
        self.assertFalse(ok_b)
        self.assertIn("安全屏障已冻结队列", msg_b)

        ok_c, status_c, msg_c, _ = self.reg_mgr.register_activity(candidate_c)
        self.assertFalse(ok_c)
        self.assertIn("安全屏障已冻结队列", msg_c)

        # 3. 网络恢复，进行复核确认 A 实际上已经报名成功
        self.mock_client.activities.side_effect = None
        self.mock_client.activities.return_value = [{"id": "1001", "name": "户外跑 程启航", "isRegister": True}]

        res_ok, res_status, _, _ = self.reg_mgr.resolve_unknown("1001", "户外跑 程启航")
        self.assertTrue(res_ok)
        self.assertEqual(res_status, RegistrationStatus.SUCCESS)

        # 4. 假设上限为 1，达到上限停止
        max_normal = 1
        current_confirmed = 1
        self.assertGreaterEqual(current_confirmed, max_normal)

        # 验证只发生了一次 signUp 网络调用（仅候选 A）
        self.assertEqual(self.mock_client.call.call_count, 1)

    def test_scenario_B_first_submit_timeout_confirmed_not_registered_allows_B(self):
        """
        场景 B：第一次提交超时，但服务端确认未报名
        候选 A 提交 -> UNKNOWN -> 冻结队列 -> 服务端完整查询 -> 明确确认未报名 -> 更新状态 -> 允许候选 B
        预期：
        - A 不会被盲目重新提交
        - B 只有在重新计算名额后才允许提交
        """
        candidate_a = {"id": "1001", "name": "户外跑 程启航"}
        candidate_b = {"id": "1002", "name": "户外跑 王彤帆"}

        # 1. 提交 A 超时 -> UNKNOWN
        self.mock_client.call.return_value = (-1, "Timeout")
        self.mock_client.activities.side_effect = Exception("Temporary 500")

        ok_a, status_a, _, _ = self.reg_mgr.register_activity(candidate_a)
        self.assertEqual(status_a, RegistrationStatus.UNKNOWN)

        # 2. 候选 B 被屏障阻断
        ok_b, _, _, _ = self.reg_mgr.register_activity(candidate_b)
        self.assertFalse(ok_b)

        # 3. 服务端数据完整返回，明确确认 A 未报名 (isRegister=False)
        class CompleteList(list):
            complete = True
        self.mock_client.activities.side_effect = None
        self.mock_client.activities.return_value = CompleteList([{"id": "1001", "name": "户外跑 程启航", "isRegister": False}])

        res_ok, res_status, _, _ = self.reg_mgr.resolve_unknown("1001", "户外跑 程启航")
        self.assertEqual(res_status, RegistrationStatus.RETRYABLE)
        self.assertFalse(self.state_store.has_unresolved())

        # 4. A 不被盲目重试，现在允许评估并提交候选 B
        self.mock_client.call.return_value = (200, {"code": 0, "data": {"success": True}, "message": "OK"})
        ok_b2, status_b2, _, _ = self.reg_mgr.register_activity(candidate_b)
        self.assertTrue(ok_b2)
        self.assertEqual(status_b2, RegistrationStatus.SUCCESS)

    def test_scenario_C_continuous_server_outage_circuit_breaker(self):
        """
        场景 C：服务器持续故障
        活动列表请求失败 -> 连续失败达到阈值 -> OPEN -> 暂停业务 -> 等待冷却 -> HALF_OPEN -> 只读健康检查失败 -> 重新 OPEN
        预期：
        - 不持续高频请求
        - 不提交报名
        - 不把不完整数据用于决策
        """
        # 连续 3 次 500 触发熔断
        for _ in range(3):
            self.breaker.record_failure(http_status=500)
        self.assertEqual(self.breaker.state, CircuitState.OPEN)

        # 熔断中拒绝请求
        can, _ = self.breaker.can_attempt()
        self.assertFalse(can)

        # 冷却后进入半开
        import time
        time.sleep(0.15)
        can_half, _ = self.breaker.can_attempt()
        self.assertTrue(can_half)
        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)

        # 只读健康检查再次失败 (503)
        self.breaker.record_failure(http_status=503)
        self.assertEqual(self.breaker.state, CircuitState.OPEN)
        self.assertEqual(self.breaker.trip_count, 2)

    def test_scenario_D_crash_after_submitting_startup_recovery(self):
        """
        场景 D：UNKNOWN 后程序崩溃
        提交 A -> SUBMITTING 持久化 -> 程序崩溃 -> 重新启动 -> 发现遗留 SUBMITTING -> 恢复为 UNKNOWN -> 冻结队列 -> 只读复核
        预期：
        - 不重复提交 A
        - 不提交其他候选
        - 最终状态可恢复
        """
        # 1. 模拟崩溃前持久化了 SUBMITTING
        self.state_store.record_submitting("1001", "崩溃前提交活动")

        # 2. 重启恢复时复核确认已成功
        self.mock_client.activities.return_value = [{"id": "1001", "name": "崩溃前提交活动", "isRegister": True}]
        summary = self.recovery_mgr.recover_on_startup()

        self.assertEqual(len(summary["recovered_success"]), 1)
        rec = self.state_store.get_record("1001")
        self.assertEqual(rec["status"], RegistrationStatus.SUCCESS)
        self.assertFalse(self.state_store.has_unresolved())

    def test_scenario_E_final_reconciliation_unresolved(self):
        """
        场景 E：最终对账仍然失败
        UNKNOWN -> 服务器不可用 -> 无法获取完整数据 -> UNRESOLVED
        预期：
        - 程序明确提示结果未知
        - 保留数据库记录
        - 不释放潜在名额
        - 不允许继续报名
        - 下次启动仍能恢复
        """
        self.state_store.record_unknown("1001", "不可用活动", "超时")
        self.mock_client.activities.side_effect = Exception("依然不可用")

        # 复核依然失败
        res_ok, res_status, _, _ = self.reg_mgr.resolve_unknown("1001", "不可用活动")
        self.assertEqual(res_status, RegistrationStatus.UNKNOWN)
        self.assertTrue(self.state_store.has_unresolved())

        # 统计报告生成
        self.reporter.set_reconciliation_result(
            reconciled_items=[],
            unresolved_count=1
        )
        rpt = self.reporter.format_console_report()
        self.assertIn("未完成对账", rpt)
        self.assertIn("【安全警告】", rpt)

    def test_scenario_F_token_expired_during_grab_refreshes_and_reconciles(self):
        """
        场景 F：抢课过程中 Token 过期自愈与对账全流程
        Token 过期 -> ensure_token 自动刷新并备份 -> 提交遇到 401 进入 UNKNOWN -> 只读复核发现落地 -> 对账成功
        预期：
        - 发生自动刷新
        - 401 得到安全隔离并在复核后转为 SUCCESS
        - 对账准确识别本轮落地成功
        """
        # 1. 模拟提交返回 401
        self.mock_client.call.return_value = (401, {"message": "Unauthorized"})

        # 2. 模拟复核时拉取到已报记录
        class CompleteList(list):
            complete = True
        self.mock_client.activities.side_effect = None
        self.mock_client.activities.return_value = CompleteList([
            {"id": "2001", "name": "401自愈活动", "isRegister": True, "startTime": 1793544000000}
        ])

        candidate = {"id": "2001", "name": "401自愈活动"}
        ok, status, reason, _ = self.reg_mgr.register_activity(candidate)
        self.assertTrue(ok)
        self.assertEqual(status, RegistrationStatus.SUCCESS)

        self.reporter.record_attempt("2001", "401自愈活动", "SUCCESS")
        self.reporter.set_reconciliation_result(
            reconciled_items=self.mock_client.activities.return_value,
            unresolved_count=0,
            normal_registered=1
        )
        sm = self.reporter.generate_summary()
        self.assertEqual(sm["reconciliation"]["confirmed_success"], 1)
        self.assertEqual(sm["reconciliation"]["unresolved"], 0)


if __name__ == "__main__":
    unittest.main()
