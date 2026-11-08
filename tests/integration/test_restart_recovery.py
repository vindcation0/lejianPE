"""
Phase 7 进程崩溃重启与状态恢复集成测试套件 (I56 ~ I70)
=====================================================
验证进程异常崩溃、断网重连、断电重启等场景下，
未决状态 (SUBMITTING / UNKNOWN / VERIFYING) 的自动发现、服务端真实求证与终态对齐。
"""
import copy
import datetime
import json
import os
import time
import unittest

from core.preflight import PreflightMode, PreflightStatus
from core.recovery_manager import RecoveryManager
from core.state_store import RegistrationStatus, StateStore
from tests.integration.integration_helper import IntegrationEnvironment


class TestRestartRecoveryFlow(unittest.TestCase):
    """崩溃重启与状态自愈集成测试 (I56 ~ I70)"""

    def setUp(self):
        self.env = IntegrationEnvironment()
        self.env.server.add_activity("act_401", "网球初级班", 1794146400000, 1794153600000)
        self.env.server.add_activity("act_402", "击剑提高班", 1794155400000, 1794162600000)

    def tearDown(self):
        self.env.close()

    # -------------------------------------------------------------------------
    # I56: 进程在 SUBMITTING 期间崩溃，重启时被 RecoveryManager 准确识别
    # -------------------------------------------------------------------------
    def test_I56_submitting_crash_scanned_on_startup(self):
        aid = "act_401"
        self.env.state_store.reserve_submitting(aid, "网球初级班")
        self.assertEqual(len(self.env.state_store.get_unresolved_records()), 1)

        # 模拟服务实际已成功录入
        self.env.server.registered_activities.add(aid)

        summary = self.env.recovery_manager.recover_on_startup()
        self.assertEqual(len(summary["recovered_success"]), 1)
        self.assertEqual(summary["recovered_success"][0]["activity_id"], aid)

    # -------------------------------------------------------------------------
    # I57: 遗留 SUBMITTING 经复核确认服务端已报名，自愈恢复为 SUCCESS
    # -------------------------------------------------------------------------
    def test_I57_submitting_verified_registered_becomes_success(self):
        aid = "act_401"
        self.env.state_store.reserve_submitting(aid, "网球初级班")
        self.env.server.registered_activities.add(aid)

        self.env.recovery_manager.recover_on_startup()

        rec = self.env.state_store.get_record(aid)
        self.assertEqual(rec["status"], RegistrationStatus.SUCCESS)
        self.assertEqual(len(self.env.state_store.get_unresolved_records()), 0)

    # -------------------------------------------------------------------------
    # I58: 遗留 SUBMITTING 经复核确认服务端未报名，安全更新为 RETRYABLE
    # -------------------------------------------------------------------------
    def test_I58_submitting_verified_unregistered_becomes_retryable(self):
        aid = "act_401"
        self.env.state_store.reserve_submitting(aid, "网球初级班")
        # 服务端无此活动报名记录
        if aid in self.env.server.registered_activities:
            self.env.server.registered_activities.remove(aid)

        summary = self.env.recovery_manager.recover_on_startup()
        self.assertEqual(len(summary["marked_retryable"]), 1)

        rec = self.env.state_store.get_record(aid)
        self.assertEqual(rec["status"], RegistrationStatus.RETRYABLE)
        self.assertEqual(len(self.env.state_store.get_unresolved_records()), 0)

    # -------------------------------------------------------------------------
    # I59: 遗留 UNKNOWN 经复核确认已成功，恢复为 SUCCESS
    # -------------------------------------------------------------------------
    def test_I59_unknown_verified_registered_becomes_success(self):
        aid = "act_401"
        self.env.state_store.record_unknown(aid, "网球初级班", error="网络中断未获响应")
        self.env.server.registered_activities.add(aid)

        summary = self.env.recovery_manager.recover_on_startup()
        self.assertEqual(len(summary["recovered_success"]), 1)

        rec = self.env.state_store.get_record(aid)
        self.assertEqual(rec["status"], RegistrationStatus.SUCCESS)

    # -------------------------------------------------------------------------
    # I60: 遗留 UNKNOWN 经复核确认未成功，标记为 RETRYABLE
    # -------------------------------------------------------------------------
    def test_I60_unknown_verified_unregistered_becomes_retryable(self):
        aid = "act_401"
        self.env.state_store.record_unknown(aid, "网球初级班", error="502 Bad Gateway")

        summary = self.env.recovery_manager.recover_on_startup()
        self.assertEqual(len(summary["marked_retryable"]), 1)

        rec = self.env.state_store.get_record(aid)
        self.assertEqual(rec["status"], RegistrationStatus.RETRYABLE)

    # -------------------------------------------------------------------------
    # I61: 复核接口本身故障 (500) 时维持 UNKNOWN，严禁轻率误放行
    # -------------------------------------------------------------------------
    def test_I61_verification_failure_keeps_unknown_intact(self):
        aid = "act_401"
        self.env.state_store.record_unknown(aid, "网球初级班", error="网络超时")
        self.env.server.activities_status_code = 500  # 复核接口异常

        summary = self.env.recovery_manager.recover_on_startup()
        self.assertEqual(len(summary["remained_unknown"]), 1)

        rec = self.env.state_store.get_record(aid)
        self.assertEqual(rec["status"], RegistrationStatus.UNKNOWN)
        self.assertEqual(len(self.env.state_store.get_unresolved_records()), 1)

    # -------------------------------------------------------------------------
    # I62: Preflight 检测到存在 UNKNOWN 记录时安全阻断 (BLOCKED)
    # -------------------------------------------------------------------------
    def test_I62_preflight_blocks_on_unknown_record(self):
        self.env.state_store.record_unknown("act_401", "网球", error="未知")
        preflight = self.env.create_preflight()
        rep = preflight.run_all_checks()
        self.assertEqual(rep.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("UNKNOWN" in r for r in rep.failure_reasons))

    # -------------------------------------------------------------------------
    # I63: Preflight 检测到存在进行中 SUBMITTING 记录时安全阻断 (BLOCKED)
    # -------------------------------------------------------------------------
    def test_I63_preflight_blocks_on_submitting_record(self):
        self.env.state_store.reserve_submitting("act_401", "网球")
        preflight = self.env.create_preflight()
        rep = preflight.run_all_checks()
        self.assertEqual(rep.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("SUBMITTING" in r for r in rep.failure_reasons))

    # -------------------------------------------------------------------------
    # I64: Preflight 检测到存在复核中 VERIFYING 记录时安全阻断 (BLOCKED)
    # -------------------------------------------------------------------------
    def test_I64_preflight_blocks_on_verifying_record(self):
        self.env.state_store.reserve_submitting("act_401", "网球")
        self.env.state_store.record_verifying("act_401")
        preflight = self.env.create_preflight()
        rep = preflight.run_all_checks()
        self.assertEqual(rep.status, PreflightStatus.BLOCKED)
        self.assertTrue(any("VERIFYING" in r for r in rep.failure_reasons))

    # -------------------------------------------------------------------------
    # I65: 启动恢复消除未决状态后，Preflight 顺利通过输出 READY
    # -------------------------------------------------------------------------
    def test_I65_recovery_clears_barrier_enables_preflight_ready(self):
        aid = "act_401"
        self.env.state_store.record_unknown(aid, "网球", error="网络中断")
        # 1. 恢复前 Preflight 被阻断
        rep1 = self.env.create_preflight().run_all_checks()
        self.assertEqual(rep1.status, PreflightStatus.BLOCKED)

        # 2. 执行恢复 (服务端确认未报名 -> RETRYABLE)
        self.env.recovery_manager.recover_on_startup()

        # 3. 恢复后 Preflight 顺利通过为 READY
        rep2 = self.env.create_preflight().run_all_checks()
        self.assertEqual(rep2.status, PreflightStatus.READY)

    # -------------------------------------------------------------------------
    # I66: 历史多次异常遗留的多条 UNKNOWN 记录可批量完整核对
    # -------------------------------------------------------------------------
    def test_I66_batch_recovery_multiple_unknowns(self):
        self.env.state_store.record_unknown("act_401", "网球", error="err1")
        self.env.state_store.record_unknown("act_402", "击剑", error="err2")
        self.env.server.registered_activities.add("act_401")  # 401 成功，402 未成

        summary = self.env.recovery_manager.recover_on_startup()
        self.assertEqual(len(summary["recovered_success"]), 1)
        self.assertEqual(len(summary["marked_retryable"]), 1)
        self.assertEqual(len(self.env.state_store.get_unresolved_records()), 0)

    # -------------------------------------------------------------------------
    # I67: 崩溃发生在复核本身期间 (VERIFYING 状态)，重启可重新自愈
    # -------------------------------------------------------------------------
    def test_I67_verifying_crash_recovered_on_subsequent_start(self):
        aid = "act_401"
        self.env.state_store.reserve_submitting(aid, "网球")
        self.env.state_store.record_verifying(aid)
        self.env.server.registered_activities.add(aid)

        summary = self.env.recovery_manager.recover_on_startup()
        self.assertEqual(len(summary["recovered_success"]), 1)
        rec = self.env.state_store.get_record(aid)
        self.assertEqual(rec["status"], RegistrationStatus.SUCCESS)

    # -------------------------------------------------------------------------
    # I68: RETRYABLE 状态记录不阻塞 Preflight 正常启动
    # -------------------------------------------------------------------------
    def test_I68_retryable_does_not_block_preflight(self):
        self.env.state_store.record_retryable("act_401", "待重试")
        rep = self.env.create_preflight().run_all_checks()
        self.assertEqual(rep.status, PreflightStatus.READY)

    # -------------------------------------------------------------------------
    # I69: 启动恢复完整发射审计事件
    # -------------------------------------------------------------------------
    def test_I69_recovery_emits_audit_events(self):
        aid = "act_401"
        self.env.state_store.record_unknown(aid, "网球", error="测试")
        self.env.recovery_manager.recover_on_startup()

        date_str = datetime.date.today().strftime("%Y%m%d")
        log_file = os.path.join(self.env.logs_dir, f"events-{date_str}.jsonl")
        self.assertTrue(os.path.exists(log_file))

        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        events = [json.loads(l) for l in lines]
        event_names = [e.get("event_name") for e in events]
        self.assertIn("verification_started", event_names)
        self.assertIn("startup_marked_retryable", event_names)

    # -------------------------------------------------------------------------
    # I70: 崩溃-重启-恢复-继续抢课端到端完整闭环
    # -------------------------------------------------------------------------
    def test_I70_crash_recovery_resume_full_cycle(self):
        # 1. 第一门课在提交期间因断网产生 UNKNOWN
        aid1 = "act_401"
        self.env.state_store.reserve_submitting(aid1, "网球初级班")
        self.env.state_store.record_unknown(aid1, "网球初级班", error="Connection dropped")

        # 2. 进程重启：Preflight 先行阻断，保护现场
        rep_pre = self.env.create_preflight().run_all_checks()
        self.assertEqual(rep_pre.status, PreflightStatus.BLOCKED)

        # 3. 执行启动恢复：服务端核对确认第一门课未报名成功 -> RETRYABLE
        summary = self.env.recovery_manager.recover_on_startup()
        self.assertEqual(len(summary["marked_retryable"]), 1)

        # 4. 再次 Preflight：屏障解除，状态为 READY
        rep_ready = self.env.create_preflight().run_all_checks()
        self.assertEqual(rep_ready.status, PreflightStatus.READY)

        # 5. 继续正常抓取第二门课程
        aid2 = "act_402"
        self.env.state_store.reserve_submitting(aid2, "击剑提高班")
        sub_res = self.env.submit_signup(aid2, "击剑提高班")
        self.assertEqual(sub_res.classification, "SUCCESS")
        self.env.state_store.record_success(aid2, "击剑提高班")

        # 6. 全局最终对账
        rec_res = self.env.reconciliation_manager.reconcile()
        self.assertTrue(rec_res["success"])
        self.assertEqual(rec_res["total_registered"], 1)

        # 7. 终态报告输出 (因存在第一门课 RETRYABLE 且第二门课成功，判定为 PARTIAL_SUCCESS)
        self.env.finalize_reporter(rec_res)
        summary_rep = self.env.result_reporter.generate_summary()
        self.assertEqual(summary_rep["verdict"]["status"], "PARTIAL_SUCCESS")


if __name__ == "__main__":
    unittest.main()
