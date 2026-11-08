"""
Phase 4 结果报告重构与数据一致性测试套件 (R01～R15)
==================================================
验证第四轮优化 Phase 4 要求的报告统一数据源、数据一致性与安全状态机：
- R01: 报告 Submission 与 Verification 分离
- R02: HTTP 失败与熔断次数分离
- R03: 最终对账成功输出正确报名数量
- R04: 最终对账失败输出 UNVERIFIED
- R05: 最终对账数据不完整输出 UNVERIFIED
- R06: 存在 UNKNOWN 时不能输出 SUCCESS
- R07: 审计日志写入失败时报告显示 audit_degraded
- R08: 不同 run_id 的报告互不混淆
- R09: 历史 JSONL 格式兼容
- R10: 重复 event_id 不会重复累计
- R11: 本地状态与服务端对账结果不一致时明确提示
- R12: 历史真实抢课记录回放结果保持一致
- R13: 控制台与 Markdown 报告使用相同统计数据
- R14: 缺失 request_id 的历史事件仍能正常回放
- R15: 审计日志存在未配对 HTTP started 事件时报告显示异常
"""
import collections
import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from core.audit_logger import AuditLogger, AuditLogReader, EventName, EventType
from core.result_reporter import ReportStatus, ResultReporter
from core.state_store import RegistrationStatus, StateStore


class ReportConsistencyTestSuite(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.logs_dir = os.path.join(self.temp_dir.name, "logs")
        self.report_dir = os.path.join(self.logs_dir, "reports")
        self.db_path = os.path.join(self.temp_dir.name, "test_p4.db")
        os.makedirs(self.logs_dir, exist_ok=True)
        os.makedirs(self.report_dir, exist_ok=True)

        self.state_store = StateStore(self.db_path)
        self.audit_logger = AuditLogger(self.logs_dir, run_id="run_p4_001")
        self.reporter = ResultReporter(run_date=datetime.date(2026, 11, 8), run_id="run_p4_001")
        self.reporter.bind_audit_logger(self.audit_logger)

    def tearDown(self):
        self.state_store.close()
        self.temp_dir.cleanup()

    # ---------------- R01: 报告 Submission 与 Verification 分离 ----------------
    def test_R01_report_submission_and_verification_separated(self):
        """R01: 报告中 Submission 与 Verification 严格独立统计，Verification 绝不进入 Submission"""
        audit_summary = {
            "run_id": "run_p4_001",
            "submission_started_count": 6,
            "submission_succeeded_count": 2,
            "submission_failed_count": 1,
            "submission_unknown_count": 3,
            "verification_started_count": 2,
            "verification_confirmed_registered_count": 0,
            "verification_confirmed_unregistered_count": 2,
            "verification_remains_unknown_count": 0,
            "verification_avg_duration_ms": 120.5,
            "http_requests_total": 24,
            "http_requests_failed": 18
        }
        self.reporter.set_audit_summary(audit_summary)

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["attempts"]["total"], 6, "Submission 总数必须为 6")
        self.assertEqual(sm["verification"]["total"], 2, "Verification 总数必须为 2，不可合并入 Submission")

        console_txt = self.reporter.format_console_report()
        self.assertIn("真实提交次数     ：6 次", console_txt)
        self.assertIn("复核总调用次数   ：2 次", console_txt)

    # ---------------- R02: HTTP 失败与熔断次数分离 ----------------
    def test_R02_http_failures_and_circuit_trips_separated(self):
        """R02: HTTP 请求失败总数与熔断触发次数完全分离，不混淆概念"""
        audit_summary = {
            "http_requests_total": 30,
            "http_requests_succeeded": 12,
            "http_requests_failed": 18,
            "http_failures_by_code": {-1: 8, 500: 8, 502: 1, 503: 1},
            "circuit_trips": 1,
            "circuit_half_open_count": 2,
            "circuit_closed_count": 1,
            "outage_seconds": 45.0
        }
        self.reporter.set_audit_summary(audit_summary)

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["infrastructure"]["http_failed"], 18)
        self.assertEqual(sm["infrastructure"]["circuit_trips"], 1)

        console_txt = self.reporter.format_console_report()
        self.assertIn("HTTP 失败总计    ：18 次", console_txt)
        self.assertIn("熔断触发次数     ：1 次", console_txt)

    # ---------------- R03: 最终对账成功输出正确报名数量 ----------------
    def test_R03_reconciliation_success_outputs_correct_count(self):
        """R03: 服务端最终对账成功，无未决记录时，正确输出报名数量与成功结论"""
        reconcile_report = {
            "success": True,
            "total_registered": 2,
            "normal_registered": 2,
            "special_registered": 0,
            "items": [
                {"id": "act_01", "name": "户外跑 周天一", "startTime": 1730000000000, "isRegister": True},
                {"id": "act_02", "name": "户外跑 王彤帆", "startTime": 1730100000000, "isRegister": True}
            ],
            "by_week": {11: ["户外跑 周天一", "户外跑 王彤帆"]},
            "by_day": {"2026-11-10": ["户外跑 周天一"], "2026-11-12": ["户外跑 王彤帆"]},
            "exceeded": False
        }
        self.reporter.set_reconciliation_result(
            reconciled_items=reconcile_report["items"],
            unresolved_count=0,
            normal_registered=2,
            max_normal=2,
            reconciliation_report=reconcile_report
        )

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["reconciliation"]["confirmed_success"], 2)
        self.assertEqual(sm["verdict"]["status"], ReportStatus.SUCCESS)

        console_txt = self.reporter.format_console_report()
        self.assertIn("确认报名成功     ：2 节", console_txt)
        self.assertIn("[SUCCESS]", console_txt)

    # ---------------- R04: 最终对账失败输出 UNVERIFIED ----------------
    def test_R04_reconciliation_failure_outputs_unverified(self):
        """R04: 最终对账失败（如接口 500 或连接断开）时，结论必须输出 UNVERIFIED，绝不误判为 0 节成功"""
        failed_report = {
            "success": False,
            "error": "HTTP 500: Server Error during reconcile",
            "registered": [],
            "exceeded": False
        }
        self.reporter.set_reconciliation_result(
            reconciled_items=[],
            unresolved_count=0,
            reconciliation_report=failed_report
        )

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["verdict"]["status"], ReportStatus.UNVERIFIED)
        self.assertIn("对账未通过", sm["verdict"]["description"])

        console_txt = self.reporter.format_console_report()
        self.assertIn("[UNVERIFIED]", console_txt)
        self.assertNotIn("成功报名 0 节", console_txt)

    # ---------------- R05: 最终对账数据不完整输出 UNVERIFIED ----------------
    def test_R05_reconciliation_incomplete_outputs_unverified(self):
        """R05: 最终对账列表不完整 (complete=False) 时，必须输出 UNVERIFIED"""
        incomplete_report = {
            "success": False,
            "error": "对账列表不完整（仅获取 50/120 条），对账中止",
            "registered": [],
            "exceeded": False
        }
        self.reporter.set_reconciliation_result(
            reconciled_items=[],
            unresolved_count=0,
            reconciliation_report=incomplete_report
        )

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["verdict"]["status"], ReportStatus.UNVERIFIED)

    # ---------------- R06: 存在 UNKNOWN 时不能输出 SUCCESS ----------------
    def test_R06_unknown_presence_prevents_success_verdict(self):
        """R06: 即使有成功报名的活动，只要存在 1 条未决 UNKNOWN，结论严禁输出 SUCCESS，必须是 UNVERIFIED"""
        # 服务端已报 1 节，但仍有 1 节处于 UNKNOWN
        reconcile_report = {
            "success": True,
            "total_registered": 1,
            "normal_registered": 1,
            "items": [{"id": "act_01", "name": "太极拳", "isRegister": True}]
        }
        self.reporter.set_reconciliation_result(
            reconciled_items=reconcile_report["items"],
            unresolved_count=1,  # 存在未决请求
            reconciliation_report=reconcile_report
        )

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["verdict"]["status"], ReportStatus.UNVERIFIED)
        self.assertNotEqual(sm["verdict"]["status"], ReportStatus.SUCCESS)

        console_txt = self.reporter.format_console_report()
        self.assertIn("未完成对账 (存在未决 UNKNOWN)", console_txt)
        self.assertIn("【安全警告】", console_txt)
        self.assertNotIn("成功报名 1 节，全部限制合规", console_txt)

    # ---------------- R07: 审计日志写入失败时报告显示 audit_degraded ----------------
    def test_R07_audit_write_failure_shows_degraded_warning(self):
        """R07: 审计日志写故障时，报告明确标记 audit_degraded，提示审计完整性降级"""
        self.audit_logger.write_error_count = 2
        self.reporter.bind_audit_logger(self.audit_logger)

        sm = self.reporter.generate_summary()
        self.assertTrue(sm["audit_degraded"])
        self.assertEqual(sm["write_error_count"], 2)

        console_txt = self.reporter.format_console_report()
        self.assertIn("⚠️ 降级", console_txt)

        md_path = self.reporter.save_markdown_report(report_dir=self.report_dir)
        with open(md_path, "r", encoding="utf-8") as f:
            md_content = f.read()
        self.assertIn("⚠️ 降级", md_content)

    # ---------------- R08: 不同 run_id 的报告互不混淆 ----------------
    def test_R08_reports_with_different_run_ids_isolated(self):
        """R08: 不同 run_id 的运行事件在回放后输入 Reporter 严格隔离"""
        events = [
            {"schema_version": 1, "event_id": "e1", "run_id": "run_A", "event_type": "submission", "event_name": "submission_started"},
            {"schema_version": 1, "event_id": "e2", "run_id": "run_A", "event_type": "submission", "event_name": "submission_succeeded"},
            {"schema_version": 1, "event_id": "e3", "run_id": "run_B", "event_type": "submission", "event_name": "submission_started"},
            {"schema_version": 1, "event_id": "e4", "run_id": "run_B", "event_type": "submission", "event_name": "submission_failed"},
        ]

        summary_a = AuditLogReader.replay(events, run_id="run_A")
        summary_b = AuditLogReader.replay(events, run_id="run_B")

        rep_a = ResultReporter(run_id="run_A")
        rep_a.set_audit_summary(summary_a)
        sm_a = rep_a.generate_summary()

        rep_b = ResultReporter(run_id="run_B")
        rep_b.set_audit_summary(summary_b)
        sm_b = rep_b.generate_summary()

        self.assertEqual(sm_a["attempts"]["total"], 1)
        self.assertEqual(sm_a["attempts"]["explicit_success"], 1)
        self.assertEqual(sm_b["attempts"]["total"], 1)
        self.assertEqual(sm_b["attempts"]["explicit_failed"], 1)

    # ---------------- R09: 历史 JSONL 格式兼容 ----------------
    def test_R09_legacy_jsonl_log_replay_generates_valid_report(self):
        """R09: 历史格式日志 (含 list_fail, list_ok) 回放结果能够无缝输入 ResultReporter"""
        legacy_events = [
            {"schema_version": 0, "event_id": "leg_1", "event_type": "infrastructure", "event_name": "list_fail", "http_status": -1},
            {"schema_version": 0, "event_id": "leg_2", "event_type": "infrastructure", "event_name": "list_ok", "details": {"outage_sec": 30.0}},
        ]
        summary = AuditLogReader.replay(legacy_events)
        self.reporter.set_audit_summary(summary)

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["infrastructure"]["http_failed"], 1)
        self.assertEqual(sm["infrastructure"]["circuit_recoveries"], 1)

        console_txt = self.reporter.format_console_report()
        self.assertIn("HTTP -1 (超时)", console_txt)

    # ---------------- R10: 重复 event_id 不会重复累计 ----------------
    def test_R10_duplicate_event_ids_not_double_counted(self):
        """R10: 包含重复 event_id 的事件流经 AuditLogReader 幂等去重后，报告指标不虚增"""
        dup_event = {
            "schema_version": 1,
            "event_id": "evt_dup_999",
            "run_id": "run_p4_001",
            "event_type": "submission",
            "event_name": "submission_started"
        }
        events = [dup_event, dup_event, dup_event]
        summary = AuditLogReader.replay(events)
        self.reporter.set_audit_summary(summary)

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["attempts"]["total"], 1, "重复 event_id 在报告中必须统计为 1 而非 3")

    # ---------------- R11: 本地状态与服务端对账结果不一致时明确提示 ----------------
    def test_R11_state_store_and_reconciliation_discrepancy_reported(self):
        """R11: 本地 StateStore 记录有未决记录但对账声称全部成功时，报告明确提示未决风险"""
        # 在本地 StateStore 插入一条 UNKNOWN 记录
        self.state_store.reserve_submitting("act_ghost_01", "幽灵活动")
        self.state_store.record_unknown("act_ghost_01", "幽灵活动", error="网络超时")

        self.reporter.set_local_state_store(self.state_store)

        # 服务端对账返回成功 1 节（但幽灵活动并未被对账消除）
        reconcile_report = {
            "success": True,
            "total_registered": 1,
            "normal_registered": 1,
            "items": [{"id": "act_real_01", "name": "真实活动", "isRegister": True}]
        }
        self.reporter.set_reconciliation_result(
            reconciled_items=reconcile_report["items"],
            unresolved_count=len(self.state_store.get_unresolved_records()),
            reconciliation_report=reconcile_report
        )

        sm = self.reporter.generate_summary()
        self.assertTrue(sm["security_state"]["has_unresolved"])
        self.assertEqual(sm["verdict"]["status"], ReportStatus.UNVERIFIED)

        console_txt = self.reporter.format_console_report()
        self.assertIn("【安全警告】", console_txt)

    # ---------------- R12: 历史真实抢课记录回放结果保持一致 ----------------
    def test_R12_historical_20261108_replay_report_matches_ground_truth(self):
        """R12: 2026-11-08 历史抢课基准回放后生成报告，各段数据与真实物理记录 100% 一致"""
        historical_summary = {
            "run_id": "run_20261108_real",
            "candidate_count": 6,
            "candidate_rejected": 6,
            "submission_started_count": 6,
            "submission_succeeded_count": 2,
            "submission_failed_count": 1,
            "submission_unknown_count": 3,
            "verification_started_count": 2,
            "verification_confirmed_registered_count": 0,
            "verification_confirmed_unregistered_count": 2,
            "verification_remains_unknown_count": 0,
            "http_requests_total": 30,
            "http_requests_failed": 18,
            "http_failures_by_code": {-1: 8, 500: 8, 502: 1, 503: 1},
            "circuit_trips": 1,
            "outage_seconds": 24.0
        }
        self.reporter.set_audit_summary(historical_summary)

        # 最终对账确认成功 2 节（王彤帆、周天一），无未决记录
        reconcile_report = {
            "success": True,
            "total_registered": 2,
            "normal_registered": 2,
            "items": [
                {"id": "act_wang", "name": "户外跑 王彤帆", "isRegister": True},
                {"id": "act_zhou", "name": "户外跑 周天一", "isRegister": True}
            ]
        }
        self.reporter.set_reconciliation_result(
            reconciled_items=reconcile_report["items"],
            unresolved_count=0,
            normal_registered=2,
            reconciliation_report=reconcile_report
        )

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["attempts"]["total"], 6)
        self.assertEqual(sm["verification"]["total"], 2)
        self.assertEqual(sm["infrastructure"]["http_failed"], 18)
        self.assertEqual(sm["reconciliation"]["confirmed_success"], 2)
        self.assertIn(sm["verdict"]["status"], (ReportStatus.SUCCESS, ReportStatus.PARTIAL_SUCCESS))

        console_txt = self.reporter.format_console_report()
        self.assertIn("真实提交次数     ：6 次", console_txt)
        self.assertIn("复核总调用次数   ：2 次", console_txt)
        self.assertIn("HTTP 失败总计    ：18 次", console_txt)
        self.assertIn("确认报名成功     ：2 节", console_txt)

    # ---------------- R13: 控制台与 Markdown 报告使用相同统计数据 ----------------
    def test_R13_console_and_markdown_reports_use_same_data_source(self):
        """R13: 控制台文本报告与 Markdown 报告基于同一 generate_summary()，指标 100% 相同"""
        summary = {
            "run_id": "run_p4_001",
            "candidate_count": 5,
            "submission_started_count": 2,
            "submission_succeeded_count": 1,
            "submission_failed_count": 1,
            "submission_unknown_count": 0,
            "verification_started_count": 0,
            "http_requests_total": 10,
            "http_requests_failed": 3,
            "http_failures_by_code": {500: 3},
            "circuit_trips": 0
        }
        self.reporter.set_audit_summary(summary)
        self.reporter.set_reconciliation_result(
            reconciled_items=[{"id": "1", "name": "足球", "isRegister": True}],
            unresolved_count=0,
            normal_registered=1
        )

        console_txt = self.reporter.format_console_report()
        md_path = self.reporter.save_markdown_report(report_dir=self.report_dir)
        with open(md_path, "r", encoding="utf-8") as f:
            md_txt = f.read()

        # 验证关键数据双边完全一致
        self.assertIn("2 次", console_txt)
        self.assertIn("- **提交尝试总数**：2", md_txt)
        self.assertIn("1 节", console_txt)
        self.assertIn("- **服务端确认报名成功**：1 节", md_txt)
        self.assertIn("HTTP 500", console_txt)
        self.assertIn("HTTP 500", md_txt)

    # ---------------- R14: 缺失 request_id 的历史事件仍能正常回放 ----------------
    def test_R14_missing_request_id_in_events_replays_cleanly(self):
        """R14: 历史事件若无 request_id 字段，回放与报告生成正常，不报错"""
        events_without_req_id = [
            {"schema_version": 1, "event_id": "e_old_1", "event_type": "infrastructure", "event_name": "http_request_started"},
            {"schema_version": 1, "event_id": "e_old_2", "event_type": "infrastructure", "event_name": "http_request_succeeded", "http_status": 200, "duration_ms": 50.0}
        ]
        summary = AuditLogReader.replay(events_without_req_id)
        self.reporter.set_audit_summary(summary)

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["infrastructure"]["http_total"], 1)
        self.assertEqual(sm["infrastructure"]["http_succeeded"], 1)
        self.assertFalse(sm["audit_degraded"])

    # ---------------- R15: 审计日志存在未配对 HTTP started 事件时报告显示异常 ----------------
    def test_R15_unpaired_http_started_event_flags_audit_anomaly(self):
        """R15: 审计日志中若存在有 started 但无 succeeded/failed 的请求，报告准确标记审计异常与降级"""
        events_unpaired = [
            # 配对成功的请求 1
            {"schema_version": 1, "event_id": "e1", "request_id": "req_001", "event_type": "infrastructure", "event_name": "http_request_started"},
            {"schema_version": 1, "event_id": "e2", "request_id": "req_001", "event_type": "infrastructure", "event_name": "http_request_succeeded", "http_status": 200},
            # 未配对的请求 2 (可能因断电、崩溃丢失了响应事件)
            {"schema_version": 1, "event_id": "e3", "request_id": "req_002", "event_type": "infrastructure", "event_name": "http_request_started"},
        ]
        summary = AuditLogReader.replay(events_unpaired)
        self.reporter.set_audit_summary(summary)

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["unpaired_http_requests"], 1)
        self.assertTrue(sm["audit_degraded"])
        self.assertEqual(sm["verdict"]["status"], ReportStatus.UNVERIFIED)

        console_txt = self.reporter.format_console_report()
        self.assertIn("⚠️ 降级", console_txt)
        self.assertIn("[UNVERIFIED]", console_txt)


if __name__ == "__main__":
    unittest.main()
